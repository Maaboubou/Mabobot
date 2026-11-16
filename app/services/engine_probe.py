"""Explicit single-connection probes; never run a task's fallback chain."""
from __future__ import annotations

from dataclasses import asdict
import json
import time
import uuid

from app.services.execution_contracts import ExecutionRequest, InputEnvelope, TaskContext, RouteCandidate, RouteSnapshot, canonical
from app.services.execution_runtime import RunHandle
from app.services.engine_adapters import create_adapter, result_for


def probe_profile(profile_id, *, model, reasoning_effort, mode):
    from app.services.execution_service import get_execution_service
    service = get_execution_service()
    revision, config = service.config.read()
    profile = config["profiles"][profile_id]
    from app.services.opencode_profile_config import snapshot_options, model_spec
    candidate = RouteCandidate("probe", profile["engine"], profile_id, str(profile["generation"]), model,
        profile["account_key"], profile["auth_ref"], canonical({"reasoning_effort": reasoning_effort}), profile.get("channel", ""),
        profile_config_json=canonical(snapshot_options(profile) if profile["engine"] == "opencode" else profile.get("runtime_options", {})))
    if candidate.engine == "codex" and mode != "direct":
        return {"status": "unsupported", "message": "Codex 工具模式请使用原 Codex 连接测试。"}
    marker = "MABOBOT_PROBE_" + uuid.uuid4().hex[:8]
    ctx = TaskContext.create(timeout=120, request_id=uuid.uuid4().hex, source_kind="system", source_chat_id="",
        actor_id="administrator", component="engine_probe", task_type=mode, delivery_chat_id="", delivery_owner="",
        authorization_ref="engine-probe:" + profile_id, permission_revision="1")
    route = RouteSnapshot(str(revision), mode, (candidate,), max_attempts=1)
    envelope = InputEnvelope("Reply exactly " + marker + ". Do not call tools.", canonical("Capability verification input"))
    service.store.admit(ctx, route)  # explicit, trusted console system admission
    service.store.bind_input(ctx.run_id, envelope)
    attempt_id = uuid.uuid4().hex
    service.store.dispatch(ctx.run_id, 0, attempt_id)
    request = ExecutionRequest(ctx, route, envelope, attempt_id)
    key = candidate.engine, candidate.profile_id, candidate.generation
    service.registry.register(*key, lambda: create_adapter(candidate, route))
    evidence = {}
    def run():
        with service.registry.acquire(key) as lease:
            adapter = lease.runtime
            handle = RunHandle(ctx, attempt_id, candidate.generation, persist_cancel=service.store.cancel, cancel=adapter.cancel)
            with service._lock:
                service._handles[ctx.run_id] = handle
            try:
                if candidate.engine == "codex":
                    result = service._validate_result(request, adapter.execute(request, handle))
                    evidence.update(getattr(handle, "direct_evidence", {}))
                    shape = evidence.get("outbound_shape", {})
                    verified = (result.status == "completed" and result.text.strip() == marker
                        and result.actual_model == model and shape.get("verified") is True
                        and shape.get("forwarded_count") == 1)
                elif mode == "direct":
                    evidence.update(adapter_version="1.0.0", adapter_sha256=adapter.runtime.plugin_sha256,
                        engine_version="2.3.0+mabobot.p0.1", transport="http")
                    result = service._validate_result(request, adapter.execute(request, handle))
                    shape = adapter.runtime.bridge("status", attempt_id=attempt_id).get("outbound_shape", {})
                    evidence["outbound_shape"] = shape
                    verified = result.status == "completed" and result.text.strip() == marker and shape.get("verified") is True
                else:
                    evidence.update(adapter_version="1.0.0", adapter_sha256=adapter.runtime.plugin_sha256,
                        engine_version="2.3.0+mabobot.p0.1", transport="http")
                    runtime = adapter.runtime
                    evidence["stage"] = "session_new"
                    session = runtime.rpc("session/new", {"cwd": str(runtime.root / "control"), "mcpServers": [adapter.mcp.config]})["sessionId"]
                    evidence["stage"] = "select_model"
                    selected = model_spec(candidate)
                    runtime.rpc("session/set_config_option", {"sessionId": session, "configId": "model", "value": selected["providerID"] + "/" + model})
                    evidence["stage"] = "select_effort"
                    if selected["variant"] != "default":
                        runtime.rpc("session/set_config_option", {"sessionId": session, "configId": "effort", "value": selected["variant"]})
                    calls = []
                    adapter.mcp.bind(session, handle, ("diagnostics__ping",), lambda name, args: (calls.append(name) or {"result": marker}))
                    runtime.bridge("bind_chat", session_id=session, tools=["mabobot_diagnostics__ping"],
                        instructions="Call the host diagnostics ping tool exactly once, then return its result.",
                        model=selected)
                    handle.bind(session)
                    try:
                        evidence["stage"] = "prompt"
                        response = runtime.rpc("session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": "Run the host ping verification."}]}, timeout=max(1, ctx.deadline-time.time()))
                        verified = response.get("stopReason") == "end_turn" and calls == ["diagnostics__ping"]
                        evidence["authenticated_mcp_roundtrip"] = verified
                        result = result_for(request, status="completed" if verified else "failed", actual_model=model)
                    finally:
                        adapter.mcp.retire(session)
                        runtime.bridge("retire_chat", session_id=session)
                service.store.finish_attempt(result)
                if candidate.engine == "opencode" and mode == "direct" and result.status != "interrupted_unknown":
                    adapter.acknowledge(attempt_id)
                return verified
            finally:
                handle.complete(None)
                with service._lock:
                    service._handles.pop(ctx.run_id, None)
    try:
        verified = service.scheduler.submit(ctx, candidate.account_key, run).result(timeout=135)
    except Exception as exc:
        service.store.cancel(ctx.run_id)
        service.store.finish_attempt(result_for(request, status="interrupted_unknown", error_code="probe_unconfirmed", error="Probe outcome requires reconciliation"))
        verified = False
        evidence["error"] = "Probe failed or its outcome is not confirmed; no fallback was attempted"
        evidence["error_type"] = type(exc).__name__
        evidence["error_code"] = getattr(exc, "code", "probe_unconfirmed")
    status = "verified" if verified else "not_verified"
    service.config.record_capability(candidate, "strict_generation" if mode == "direct" else "tool_calling", status, evidence)
    service.registry.reap()
    return {"status": status, "profile_id": profile_id, "model": model, "reasoning_effort": reasoning_effort,
            "mode": mode, "run_id": ctx.run_id, "evidence": evidence, "quota_remaining": None}


def probe_api_card(card_id):
    from app.services.execution_service import get_execution_service
    from app.services.llm_manager import get_llm_manager
    from app.services.engine_routing import MODEL_PARAMETERS
    from app.services.execution_contracts import digest
    from app.services.engine_adapters import APIAdapter
    service, manager = get_execution_service(), get_llm_manager()
    card = manager.config["models"][card_id]
    if card.get("kind") == "subscription" or manager._is_local_codex_model(card):
        raise ValueError("订阅模型请测试所选引擎 Profile")
    candidate = RouteCandidate(card_id, "api", "", digest(card), card["model"], card.get("account_key") or "api:" + card_id,
        "model:" + card_id, canonical({k: card[k] for k in MODEL_PARAMETERS if k in card}), card.get("provider") or card.get("custom_llm_provider") or "")
    ctx = TaskContext.create(timeout=90, request_id=uuid.uuid4().hex, source_kind="system", source_chat_id="", actor_id="administrator",
        component="engine_probe", task_type="direct", delivery_chat_id="", delivery_owner="", authorization_ref="engine-probe:api:" + card_id, permission_revision="1")
    marker = "MABOBOT_API_" + uuid.uuid4().hex[:8]
    route = RouteSnapshot("api-probe", "direct", (candidate,), max_attempts=1)
    envelope = InputEnvelope("Reply exactly " + marker, canonical("Capability verification input"))
    service.store.admit(ctx, route)
    service.store.bind_input(ctx.run_id, envelope)
    attempt = uuid.uuid4().hex
    service.store.dispatch(ctx.run_id, 0, attempt)
    adapter = APIAdapter(candidate, route)
    handle = RunHandle(ctx, attempt, candidate.generation, persist_cancel=service.store.cancel, cancel=adapter.cancel)
    request = ExecutionRequest(ctx, route, envelope, attempt)
    evidence = {"adapter_version": "1.0.0", "transport": "http_json", "single_connection": True}
    try:
        result = service.scheduler.submit(ctx, candidate.account_key,
            lambda: service._validate_result(request, adapter.execute(request, handle))).result(timeout=100)
        service.store.finish_attempt(result)
        verified = result.status == "completed" and result.text.strip() == marker and adapter.last_request_shape["verified"]
        evidence["outbound_shape"] = adapter.last_request_shape
    except Exception:
        verified = False
        service.store.cancel(ctx.run_id)
        service.store.finish_attempt(result_for(request, status="interrupted_unknown", error_code="probe_unconfirmed", error="Probe outcome requires reconciliation"))
        evidence["error"] = "Single-connection probe failed; no fallback was attempted"
    status = "verified" if verified else "not_verified"
    service.config.record_capability(candidate, "strict_generation", status, evidence)
    return {"status": status, "card_id": card_id, "run_id": ctx.run_id, "evidence": evidence}
