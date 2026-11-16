"""Host orchestration shared by the assistant, plugins and scheduled runs."""
from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from app.services.execution_contracts import (
    ExecutionError, ExecutionRequest, ExecutionResult, InputEnvelope, canonical, execution_context,
)
from app.services.execution_runtime import AccountScheduler, RunHandle, RuntimeRegistry
from app.services.execution_store import ExecutionStore, execution_db_path
from app.services.engine_routing import EngineConfigService


class ExecutionService:
    def __init__(self, store, *, authorize, adapter_factory, config=None, telemetry=None):
        self.store = store
        self.config = config or EngineConfigService(store)
        self.authorize = authorize
        self.adapter_factory = adapter_factory
        self.telemetry = telemetry
        self.registry = RuntimeRegistry(durable_pins=store.pinned)
        self.scheduler = AccountScheduler(active_check=self.require_active)
        self._handles, self._lock = {}, threading.RLock()
        self._maintenance_stop = threading.Event()
        self._maintenance_thread = threading.Thread(target=self._maintain, name="engine-reaper", daemon=True)
        self._maintenance_thread.start()
        self._recovery_workers = None

    def recover(self):
        from app.services.engine_deployment import recovery_pending
        if recovery_pending():
            return []
        self.store.finish_unused_admissions(expired_before=time.time())
        resumable = self.store.recover(self.authorize)
        if resumable:
            self._recovery_workers = ThreadPoolExecutor(2, thread_name_prefix="engine-recovery")
            for run_id in resumable:
                row = self.store.get(run_id)
                envelope = InputEnvelope(**json.loads(row["input_json"]))
                self._recovery_workers.submit(self.execute, run_id, envelope)
        return resumable

    def _maintain(self):
        while not self._maintenance_stop.wait(30):
            try:
                self.store.finish_unused_admissions(expired_before=time.time())
                idle = max(30, int(os.getenv("ENGINE_IDLE_SECONDS", "300")))
                self.registry.reap(idle_seconds=idle)
                from app.services import codex_profile_service
                if codex_profile_service._runtime_registry is not None:
                    codex_profile_service._runtime_registry.reap_idle(idle_seconds=idle)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Runtime idle reaping failed")

    def require_active(self, context):
        from app.services.engine_deployment import require_dispatch
        require_dispatch()
        self.store.require_active(context.run_id)
        if not self.authorize(context):
            self.store.cancel(context.run_id)
            raise ExecutionError("authorization_revoked", "Current host authorization no longer permits this task")

    def admit(self, context, route):
        from app.services.engine_deployment import require_dispatch
        require_dispatch()
        if not self.authorize(context):
            raise ExecutionError("permission_denied", "Task source or delivery is not authorized")
        return self.store.admit(context, route)

    def execute(self, run_id, envelope, *, emit_telemetry=True):
        context, route = self.store.contracts(run_id)
        self.store.bind_input(run_id, envelope)
        row = self.store.get(run_id)
        if row["status"] in {"completed", "skipped", "failed", "cancelled"}:
            with self.store.connect() as db:
                attempt = db.execute("SELECT result_json FROM engine_attempts WHERE run_id=? ORDER BY candidate_index DESC LIMIT 1", (run_id,)).fetchone()
            if attempt and attempt[0]:
                return ExecutionResult(**json.loads(attempt[0]))
        last = None
        for index, candidate in enumerate(route.candidates[:route.max_attempts]):
            attempt_started = time.monotonic()
            self.require_active(context)
            attempt_id = uuid.uuid5(uuid.NAMESPACE_URL, run_id + ":" + str(index)).hex
            dispatched = self.store.dispatch(run_id, index, attempt_id)
            if not dispatched["new"]:
                if dispatched.get("result_json"):
                    last = ExecutionResult(**json.loads(dispatched["result_json"]))
                    if last.status == "failed" and index + 1 < min(len(route.candidates), route.max_attempts):
                        continue
                    return last
                raise ExecutionError("reconciliation_required", "This attempt was already dispatched; inspect its original result")
            request = ExecutionRequest(context, route, envelope, attempt_id, index)
            adapter_used = []
            try:
                self.config.check_candidate(candidate, route.mode)
                key = candidate.engine, candidate.profile_id, candidate.generation
                _, current_config = self.config.read()
                current_generation = str(current_config["profiles"].get(candidate.profile_id, {}).get("generation", candidate.generation))
                self.registry.register(*key, lambda c=candidate, r=route: self.adapter_factory(c, r),
                    current=current_generation == candidate.generation)
                def run():
                    self.require_active(context)
                    with self.registry.acquire(key) as lease:
                        adapter = lease.runtime
                        adapter_used.append(adapter)
                        handle = RunHandle(context, attempt_id, lease.generation,
                            persist_cancel=self.store.cancel, cancel=adapter.cancel,
                            append=getattr(adapter, "append", None))
                        from app.assistant.task_supervisor import current_task
                        assistant_task = current_task()
                        if assistant_task is not None and context.component == "assistant" and not context.depth:
                            assistant_task.bind_execution(handle)
                        with self._lock:
                            self._handles[run_id] = handle
                        from app.services.schedule_context import current_schedule_context
                        schedule = current_schedule_context()
                        watcher_stop = threading.Event()
                        def watch():
                            while not watcher_stop.wait(0.2):
                                try:
                                    self.require_active(context)
                                    if schedule and schedule.cancelled and schedule.cancelled():
                                        raise ExecutionError("cancelled", "Scheduled run was cancelled")
                                except Exception:
                                    try:
                                        handle.cancel()
                                    except Exception:
                                        pass
                                    return
                        watcher = threading.Thread(target=watch, daemon=True, name="model-cancellation")
                        watcher.start()
                        try:
                            with execution_context(context):
                                result = adapter.execute(request, handle)
                            return self._validate_result(request, result)
                        finally:
                            watcher_stop.set()
                            watcher.join(timeout=1)
                            # Adapters return only after native execution and their
                            # tools drain; unknown adapters must quarantine sessions.
                            handle.complete(None)
                            if not handle.wait(5):
                                raise ExecutionError("interrupted_unknown", "Native execution ended before tools drained")
                            with self._lock:
                                self._handles.pop(run_id, None)
                last = self.scheduler.submit(context, candidate.account_key, run,
                    priority=100 if context.purpose == "foreground" else 20).result()
                retryable = route.mode == "direct" and last.status == "failed" and last.error_code in {
                    "invalid_json", "schema_invalid", "output_truncated", "capability_unavailable", "model_missing"}
            except ExecutionError as exc:
                status = "cancelled" if exc.code == "cancelled" else "interrupted_unknown" if exc.code in {
                    "interrupted_unknown", "reconciliation_required"} else "failed"
                last = self._failure(request, status, exc.code, str(exc))
                retryable = exc.retryable
            except Exception:
                # A transport exception after the dispatch boundary proves neither
                # a failure nor absence of tool side effects. No blind fallback.
                last = self._failure(request, "interrupted_unknown", "transport_unknown", "Engine disconnected; result requires reconciliation")
                retryable = False
            final = not retryable or index + 1 >= min(len(route.candidates), route.max_attempts)
            if last.error_code == "rate_limited":
                self.scheduler.backoff(candidate.account_key, last.retry_after_seconds or 30)
            if not self.authorize(context):
                self.store.cancel(run_id)
                final = True
            self.store.finish_attempt(last, final=final)
            if self.telemetry and emit_telemetry:
                try:
                    self.telemetry(context, route, candidate, last, time.monotonic() - attempt_started)
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception("Engine attempt telemetry failed")
            if adapter_used and route.mode == "direct" and last.status != "interrupted_unknown":
                acknowledge = getattr(adapter_used[0], "acknowledge", None)
                if acknowledge:
                    try:
                        acknowledge(attempt_id)
                    except Exception:
                        # Durable host result remains authoritative if ack is lost.
                        pass
            self.registry.reap()
            if final:
                return last
        return last

    @staticmethod
    def _failure(request, status, code, message):
        c, ctx = request.candidate, request.context
        return ExecutionResult(ctx.task_id, ctx.run_id, request.attempt_id, status,
            target_model=c.model, engine=c.engine, profile_id=c.profile_id, channel=c.channel,
            error_code=code, error=message)

    def _validate_result(self, request, result):
        if (result.task_id, result.run_id, result.attempt_id, result.engine, result.profile_id) != (
                request.context.task_id, request.context.run_id, request.attempt_id,
                request.candidate.engine, request.candidate.profile_id):
            raise ExecutionError("result_identity", "Engine returned a result for a different task")
        target_model = request.candidate.model
        if request.candidate.engine == 'api':
            for prefix in ('openai/', 'deepseek/', 'xai/'):
                if target_model.startswith(prefix):
                    target_model = target_model[len(prefix):]
                    break
        if result.actual_model and result.actual_model != target_model:
            return replace(result, status="failed", error_code="model_mismatch", error="Engine used a different model")
        if result.status != "completed":
            return result
        contract = json.loads(request.route.output_contract_json)
        if contract:
            try:
                structured = json.loads(result.text)
            except (ValueError, TypeError):
                return replace(result, status="failed", error_code="invalid_json", error="Model output is not valid JSON")
            if contract.get("schema"):
                from jsonschema import validate, ValidationError
                try:
                    validate(structured, contract["schema"])
                except ValidationError:
                    return replace(result, status="failed", error_code="schema_invalid", error="Model output does not satisfy the output contract")
            skipped = isinstance(structured, dict) and contract.get("skip_field") and structured.get(contract["skip_field"]) is False
            result = replace(result, structured_json=canonical(structured), status="skipped" if skipped else "completed")
        return result

    def cancel(self, run_id):
        self.store.cancel(run_id)
        with self._lock:
            handles = [h for key, h in self._handles.items() if key == run_id or h.context.parent_run_id == run_id]
        for handle in handles:
            handle.cancel()

    def handle(self, run_id):
        with self._lock:
            return self._handles.get(run_id)

    def cancel_profile(self, profile_id):
        with self.store.connect() as db:
            rows = db.execute("SELECT run_id,route_json FROM engine_runs WHERE status IN ('queued','preparing','running','waiting_child')").fetchall()
        for row in rows:
            snapshot = json.loads(row["route_json"])
            image_policy = json.loads(snapshot.get("image_policy_json", "{}"))
            if any(c["profile_id"] == profile_id for c in snapshot["candidates"]) or image_policy.get("native_profile_id") == profile_id:
                self.cancel(row["run_id"])

    def cancel_chat(self, chat):
        from app.services.execution_authority import chat_name
        with self.store.connect() as db:
            rows = db.execute("SELECT run_id FROM engine_runs WHERE status IN ('queued','preparing','running','waiting_child')").fetchall()
        for row in rows:
            context, _ = self.store.contracts(row["run_id"])
            if context.source_chat_id == chat or chat_name(context) == chat:
                self.cancel(context.run_id)

    def status(self):
        return {"scheduler": self.scheduler.status(), "runtimes": self.registry.status(), "runs": self.store.recent()}

    def close(self):
        self._maintenance_stop.set()
        self._maintenance_thread.join(timeout=2)
        with self._lock:
            handles = list(self._handles.values())
        for handle in handles:
            try:
                handle.cancel()
            except Exception:
                pass
        self.registry.shutdown()
        self.scheduler.close()
        if self._recovery_workers is not None:
            self._recovery_workers.shutdown(wait=True, cancel_futures=True)
        self.registry.reap(idle_seconds=0)


_service = None
_service_lock = threading.Lock()


def get_execution_service():
    global _service
    with _service_lock:
        if _service is None:
            from app.services.engine_adapters import create_adapter
            from app.services.execution_authority import authorize_context
            from app.services.engine_telemetry import record_attempt
            store = ExecutionStore(execution_db_path())
            _service = ExecutionService(store, authorize=authorize_context, adapter_factory=create_adapter, telemetry=record_attempt)
            _service.recover()
        return _service
