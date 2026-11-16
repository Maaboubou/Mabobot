"""Native adapters. Provider identity and actual results never use global last-model state."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import threading
import time

from app.services.execution_contracts import ExecutionError, ExecutionResult, canonical, run_handle_scope
from app.services.opencode_usage import normalize_usage as normalize_opencode_usage


def result_for(request, *, status="completed", text="", actual_model="", usage=None, **values):
    ctx, candidate = request.context, request.candidate
    return ExecutionResult(ctx.task_id, ctx.run_id, request.attempt_id, status, text=text,
        target_model=candidate.model, actual_model=actual_model, engine=candidate.engine,
        profile_id=candidate.profile_id, channel=candidate.channel, usage_json=canonical(usage),
        usage_source="engine_reported" if usage is not None else "unknown", **values)


class CodexAdapter:
    def __init__(self, candidate, route, runtime=None):
        self.candidate, self.route, self.runtime = candidate, route, runtime
        self._owns_runtime = runtime is None
        self._direct_runtime = None
        self._start_lock = threading.Lock()

    def start(self):
        # Registry entries are shared by Profile/generation across task modes.
        # Initialize the required native path lazily for each actual request.
        pass

    def _start_agent(self):
        if self.runtime is None:
            from app.services.codex_profile_service import get_codex_profile_service, _managed_profile_permission_roots
            from app.services.agent_runtime import CodexAgentRuntime
            profile = get_codex_profile_service().get_profile(self.candidate.profile_id)
            if not profile or not profile.get("available"):
                raise ExecutionError("profile_unavailable", "Pinned Codex account is unavailable")
            options = json.loads(self.candidate.profile_config_json)
            self.runtime = CodexAgentRuntime(codex_bin=profile["wrapper_path"],
                managed_profile_id=self.candidate.profile_id, permission_read_roots=_managed_profile_permission_roots(profile),
                service_tier="fast" if options.get("fast_mode") else "default",
                model_supports_web_search=bool(options.get("supports_web_search", profile.get("supports_web_search"))))
            self.runtime.start()

    def stop(self):
        if self.runtime is not None and self._owns_runtime:
            self.runtime.stop()
        if self._direct_runtime is not None:
            self._direct_runtime.stop()

    def force_stop(self):
        if self.runtime is not None:
            self.runtime.stop()
        if self._direct_runtime is not None:
            self._direct_runtime.stop()

    def execute(self, request, handle):
        if request.route.mode == "direct":
            return self._direct(request, handle)
        with self._start_lock:
            self._start_agent()
        from app.services.execution_authority import chat_name
        name = chat_name(request.context) if request.context.source_chat_id else ""
        data = json.loads(request.envelope.input_json)
        messages = [{"role": "system", "content": request.envelope.instructions}]
        messages.extend(data if isinstance(data, list) else [{"role": "user", "content": data if isinstance(data, str) else canonical(data)}])
        payload = {"model": request.candidate.model, "messages": messages,
            "timeout": max(1, int(request.context.deadline - time.time())),
            **json.loads(request.candidate.parameters_json), "codex_source_chat_name": name,
            "codex_runtime_profile": request.candidate.profile_id}
        context_window = json.loads(request.candidate.profile_config_json).get("context_window")
        if context_window:
            payload["codex_model_context_window"] = context_window
        assets = json.loads(request.envelope.assets_json)
        if assets:
            payload["mabobot_input_files"] = assets
            payload["mabobot_allow_image_input"] = True
        with run_handle_scope(handle):
            if request.context.depth or request.context.component != "assistant":
                response = self.runtime.run(payload, profile_name="child" if request.context.depth else "batch", allow_exec_fallback=False)
            else:
                response = self.runtime.chat(payload, chat_id=name, allow_exec_fallback=False)
        message = ((response.get("choices") or [{}])[0].get("message") or {})
        from app.services.assistant_reply_gateway import CodexReplyGateway
        assets = CodexReplyGateway._extract_attachments(response)
        return result_for(request, text=str(message.get("content") or ""),
            actual_model=str(response.get("model") or ""), usage=response.get("usage"), assets_json=canonical(assets))

    def _direct(self, request, handle):
        from app.services.codex_direct import direct_spec
        direct_spec(request)  # Reject unsupported parameters before starting Codex.
        with self._start_lock:
            if self._direct_runtime is None:
                from app.services.codex_direct_runtime import CodexDirectRuntime
                from app.services.codex_profile_service import get_codex_profile_service
                profile = get_codex_profile_service().get_profile(request.candidate.profile_id)
                if not profile or not profile.get("available"):
                    raise ExecutionError("profile_unavailable", "Pinned Codex account is unavailable")
                self._direct_runtime = CodexDirectRuntime(profile["wrapper_path"])
                try:
                    self._direct_runtime.start()
                except Exception:
                    self._direct_runtime.stop()
                    self._direct_runtime = None
                    raise
        handle.direct_runtime = self._direct_runtime
        # Per-request evidence must not be shared by concurrent calls.
        native = self._direct_runtime.execute(request, handle)
        handle.direct_evidence = native.get("evidence", {})
        shape = handle.direct_evidence.get("outbound_shape", {})
        status = native.get("status", "failed")
        if status == "completed" and not (shape.get("verified") is True and shape.get("forwarded_count") == 1):
            raise ExecutionError("outbound_unverified", "Codex direct result has no verified outbound request")
        return result_for(request, status=status, text=native.get("text", ""), actual_model=native.get("actual_model", ""),
            usage=native.get("usage"), error_code=native.get("error_code", ""), error=native.get("error", ""),
            retry_after_seconds=native.get("retry_after_seconds"))

    @staticmethod
    def cancel(handle):
        direct = getattr(handle, "direct_runtime", None)
        if direct is not None:
            direct.cancel(handle)
            return
        manager = getattr(handle, "native_manager", None)
        if manager and handle.session_id and handle.turn_id:
            manager._request("turn/interrupt", {"threadId": handle.session_id, "turnId": handle.turn_id}, timeout=10)

    @staticmethod
    def append(handle, text):
        manager = getattr(handle, "native_manager", None)
        if not manager or not handle.turn_id:
            raise ExecutionError("not_started", "Native turn has not started")
        return manager._request("turn/steer", {"threadId": handle.session_id, "expectedTurnId": handle.turn_id,
            "input": [{"type": "text", "text": text}]}, timeout=15)


class OpenCodeAdapter:
    def __init__(self, candidate, route, *, runtime=None, mcp=None):
        self.candidate, self.route, self.runtime, self.mcp = candidate, route, runtime, mcp

    def start(self):
        if self.runtime is None:
            from app.services.execution_service import get_execution_service
            from app.services.opencode_runtime import OpenCodeRuntime
            from app.services.opencode_mcp import HostMCP
            from app.services.engine_tools import tool_catalog
            service = get_execution_service()
            self.mcp = HostMCP(generation=self.candidate.generation, profile_id=self.candidate.profile_id,
                catalog=tool_catalog(), authorize=service.authorize)
            params = json.loads(self.candidate.parameters_json)
            from app.services.opencode_profile_config import model_spec
            models = [model_spec(self.candidate)]
            from app.services.llm_manager import get_llm_manager
            for card in get_llm_manager().config.get("models", {}).values():
                if card.get("kind") == "subscription" and models[0]["providerID"] == "openai":
                    spec = {"providerID": "openai", "id": card["model"], "variant": card.get("reasoning_effort") or "high"}
                    if spec not in models:
                        models.append(spec)
            from app.services.engine_deployment import require_engine
            require_engine("opencode")
            options = json.loads(self.candidate.profile_config_json)
            from app.services.opencode_wsl import use_wsl, WslOpenCodeRuntime
            if use_wsl():
                self.runtime = WslOpenCodeRuntime(profile_id=self.candidate.profile_id,
                    generation=self.candidate.generation, models=models, mcp=self.mcp, options=options)
                self.runtime.start()
                return
            from app.services.opencode_profile_service import opencode_profiles
            binary, adapter_dir = None, None
            if options.get("binary"):
                from app.services.engine_releases import verify_release
                binary, adapter_dir = verify_release(options)
            else:
                binary = opencode_profiles.binary()
            self.runtime = OpenCodeRuntime(binary=binary, adapter_dir=adapter_dir,
                profile_root=Path(os.getenv("OPENCODE_PROFILE_ROOT", "data/opencode")) / self.candidate.profile_id,
                profile_id=self.candidate.profile_id, generation=self.candidate.generation, models=models, mcp=self.mcp,
                connection=options.get("connection"))
        self.runtime.start()

    def stop(self):
        if self.runtime:
            self.runtime.stop()
        if self.mcp:
            self.mcp.close()

    def is_alive(self):
        return self.runtime is not None and self.runtime.process is not None and self.runtime.process.poll() is None

    def execute(self, request, handle):
        capabilities = self.runtime.ensure_ready()
        handle.generation = capabilities["generation"]
        if request.route.mode == "direct":
            if not capabilities.get('strict_generation'):
                raise ExecutionError('capability_unavailable', '当前 OpenCode 运行时未验证直接生成能力')
            return self._direct(request, handle)
        return self._chat(request, handle)

    def _direct(self, request, handle):
        params = json.loads(request.candidate.parameters_json)
        data = json.loads(request.envelope.input_json)
        if not isinstance(data, str) or json.loads(request.envelope.assets_json):
            raise ExecutionError("capability_unavailable", "Strict bridge currently supports verified text inputs only", retryable=True)
        from app.services.opencode_profile_config import model_spec
        model = model_spec(request.candidate)
        payload = {"attempt_id": request.attempt_id, "task_id": request.context.task_id,
            "run_id": request.context.run_id, "snapshot_version": request.route.revision,
            "instructions": request.envelope.instructions, "input": data, "model": model,
            "deadline": int(request.context.deadline * 1000),
            "max_tokens": params.get("max_tokens"), "temperature": params.get("temperature")}
        if len(canonical(payload).encode()) > 2 * 1024 * 1024 - 100:
            raise ExecutionError("input_limit", "Input exceeds the bridge limit; explicit splitting is required")
        handle.bridge_attempt_id = request.attempt_id
        row = self.runtime.bridge("submit", request=payload)
        cancel_at = None
        while row["status"] not in {"completed", "failed", "cancelled", "unknown"}:
            if row.get("session_id") and not handle.session_id:
                handle.bind(row["session_id"])
            if time.time() >= request.context.deadline or handle.cancel_requested.is_set():
                if cancel_at is None:
                    self.cancel(handle)
                    cancel_at = time.monotonic()
                if time.monotonic() - cancel_at > 5:
                    return result_for(request, status="interrupted_unknown", error_code="cancel_unconfirmed", error="Native cancellation is not confirmed")
            time.sleep(0.05)
            row = self.runtime.bridge("status", attempt_id=request.attempt_id)
        native = row.get("result") or {}
        status = "interrupted_unknown" if row["status"] == "unknown" else row["status"]
        result = result_for(request, status=status, text=str(native.get("text") or ""),
            actual_model=request.candidate.model if status == "completed" else "",
            usage=normalize_opencode_usage(native.get("usage", native.get("tokens"))), error_code=(row.get("error_code") or "native_" + status) if status != "completed" else "",
            error="Native generation did not complete" if status != "completed" else "",
            retry_after_seconds=row.get("retry_after_seconds"))
        # Store the result before acknowledging/cleaning the native session.
        from app.services.execution_service import get_execution_service
        # The orchestrator performs durable completion; ack is deferred to it.
        return result

    def acknowledge(self, attempt_id):
        self.runtime.bridge("ack", attempt_id=attempt_id)

    def _chat(self, request, handle):
        from app.services.execution_service import get_execution_service
        from app.services.engine_tools import HostToolSession
        service = get_execution_service()
        session = self.runtime.rpc("session/new", {"cwd": str(self.runtime.root / "control"), "mcpServers": [self.mcp.config]})["sessionId"]
        params = json.loads(request.candidate.parameters_json)
        from app.services.opencode_profile_config import model_spec
        model = model_spec(request.candidate)
        self.runtime.rpc("session/set_config_option", {"sessionId": session, "configId": "model", "value": model["providerID"] + "/" + model["id"]})
        if model["variant"] != "default":
            self.runtime.rpc("session/set_config_option", {"sessionId": session, "configId": "effort", "value": model["variant"]})
        tools = HostToolSession(request, handle)
        self.mcp.bind(session, handle, request.route.tools, tools.execute)
        self.runtime.bridge("bind_chat", session_id=session, tools=["mabobot_" + t for t in request.route.tools],
            instructions=request.envelope.instructions + "\n\n" + tools.delivery_instructions(),
            model=model)
        handle.bind(session)
        chunks = []
        context_metrics = {}
        def update(value):
            if value.get("sessionUpdate") == "usage_update":
                from app.services.opencode_usage import context_usage
                context_metrics.update(context_usage(value))
            if value.get("sessionUpdate") == "agent_message_chunk":
                content = value.get("content", {})
                if content.get("type") == "text":
                    chunks.append(str(content.get("text", "")))
        self.runtime.updates(session, update)
        # New native sessions receive only host-visible history in this task's
        # input. Private reasoning and unfinished native tool rounds never move.
        data = json.loads(request.envelope.input_json)
        from app.services.opencode_input import prepare_prompt
        try:
            prompt = prepare_prompt(data, tools.browser.request_dir)
            if any(part['type'] == 'image' for part in prompt):
                connection = json.loads(request.candidate.profile_config_json).get('connection') or {}
                if connection.get('auth_type') == 'api_key' and not connection.get('supports_vision'):
                    raise ExecutionError('vision_unavailable', '此 API Profile 未启用图片输入能力')
            service.store.activate_session_head(request.context)
            response = self.runtime.rpc("session/prompt", {"sessionId": session,
                "prompt": prompt}, timeout=max(1, request.context.deadline - time.time()))
            status = "cancelled" if response.get("stopReason") == "cancelled" else "completed"
            if response.get("stopReason") in {"max_tokens", "max_turn_requests"}:
                status = "failed"
            usage = normalize_opencode_usage(response.get("usage"))
            service.store.save_session(request.context, request.candidate, {"native_session_id": session,
                "cwd": str(self.runtime.root / "control"), "runtime_generation": request.candidate.generation,
                "permission_revision": request.context.permission_revision, "role_signature": request.route.role_signature,
                "requires_rebuild": True, "last_completed_run": request.context.run_id,
                "usage": usage, **context_metrics})
            reply, assets = "".join(chunks), tools.assets
            error_code = "output_truncated" if status == "failed" else ""
            error = ""
            if status == "completed":
                from app.services.codex_delivery import parse_delivery, DeliveryError
                try:
                    reply, selected = parse_delivery(reply)
                    assets = tools.delivery_assets(selected)
                except (DeliveryError, ExecutionError) as exc:
                    # Model completion is known; a rejected final file is a
                    # delivery-validation failure, not an unknown native call.
                    status, reply, assets = "failed", "", []
                    error_code, error = "invalid_artifact_output", str(exc)
            return result_for(request, status=status, text=reply, actual_model=request.candidate.model,
                usage=usage, assets_json=canonical(assets), error_code=error_code, error=error)
        finally:
            self.mcp.retire(session)
            self.runtime.updates(session, None)
            tools.close()
            try:
                self.runtime.bridge("retire_chat", session_id=session)
            except (OSError, ExecutionError):
                pass

    def cancel(self, handle):
        if getattr(handle, "bridge_attempt_id", ""):
            self.runtime.bridge("cancel", attempt_id=handle.bridge_attempt_id)
        elif handle.session_id:
            self.mcp.retire(handle.session_id)
            self.runtime.notify("session/cancel", {"sessionId": handle.session_id})


class APIAdapter:
    def __init__(self, candidate, route):
        self.candidate, self.route = candidate, route

    def start(self):
        pass

    def stop(self):
        pass

    def cancel(self, handle):
        # Synchronous provider requests cannot promise a native stop. The host
        # still blocks late delivery and any further attempts immediately.
        pass

    def execute(self, request, handle):
        from app.services.llm_manager import get_llm_manager
        from app.services.execution_contracts import digest
        manager = get_llm_manager()
        card = manager.config["models"].get(request.candidate.card_id)
        if not card or digest(card) != request.candidate.generation:
            raise ExecutionError("configuration_unavailable", "Frozen API connection is no longer available")
        if request.route.mode == "tools":
            raise ExecutionError("capability_unavailable", "API tool orchestration is not enabled", retryable=True)
        import httpx
        provider = card.get("provider") or card.get("custom_llm_provider") or "openai"
        if provider not in {"openai", "openai_compatible", "deepseek", "xai"}:
            raise ExecutionError("capability_unavailable", "This API provider has no verified direct HTTP adapter", retryable=True)
        data = json.loads(request.envelope.input_json)
        model = request.candidate.model
        for prefix in ("openai/", "deepseek/", "xai/"):
            if model.startswith(prefix):
                model = model[len(prefix):]
                break
        body = {"model": model, "messages": [
            {"role": "system", "content": request.envelope.instructions},
            {"role": "user", "content": data if isinstance(data, str) else canonical(data)}],
            **{key: card[key] for key in ("temperature", "max_tokens", "reasoning_effort", "response_format") if card.get(key) is not None}}
        extra = card.get("extra_body") or {}
        if extra:
            raise ExecutionError("capability_unavailable", "Custom API body fields need a separate outbound-contract verification", retryable=True)
        encoded = canonical(body).encode()
        self.last_request_shape = {"verified": True, "model": model, "roles": ["system", "user"],
            "instruction_hash": digest(request.envelope.instructions), "input_hash": digest(data), "tools": []}
        defaults = {"openai": "https://api.openai.com/v1", "deepseek": "https://api.deepseek.com/v1", "xai": "https://api.x.ai/v1"}
        base = manager._resolve_env(card.get("api_base") or defaults.get(provider, ""))
        if not base:
            raise ExecutionError("configuration_unavailable", "OpenAI-compatible API requires an explicit endpoint")
        key = manager._resolve_env(card.get("api_key") or "")
        if time.time() >= request.context.deadline or handle.cancel_requested.is_set():
            raise ExecutionError("cancelled", "API generation was cancelled or expired")
        with httpx.Client(timeout=max(1, request.context.deadline - time.time()), follow_redirects=False) as client:
            with client.stream("POST", base.rstrip("/") + "/chat/completions", content=encoded,
                    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}) as response:
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 8 * 1024 * 1024:
                        raise ExecutionError("output_limit", "API response exceeded the host output limit")
                    if time.time() >= request.context.deadline or handle.cancel_requested.is_set():
                        raise ExecutionError("cancelled", "API generation was cancelled or expired")
                    chunks.append(chunk)
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "30")
            return result_for(request, status="failed", error_code="rate_limited", error="API account is rate limited",
                retry_after_seconds=min(3600, max(1, float(retry))) if retry.isdigit() else 30)
        if response.status_code in {401, 403}:
            return result_for(request, status="failed", error_code="authentication_failed", error="API authentication failed")
        if not response.is_success:
            return result_for(request, status="failed", error_code="provider_error", error="API rejected the generation request")
        value = json.loads(b"".join(chunks))
        choice = value["choices"][0]
        actual = str(value.get("model") or "")
        return result_for(request, text=choice.get("message", {}).get("content") or "", actual_model=actual,
            usage=value.get("usage"), status="failed" if choice.get("finish_reason") == "length" else "completed",
            error_code="output_truncated" if choice.get("finish_reason") == "length" else "")


def create_adapter(candidate, route):
    return {"codex": CodexAdapter, "opencode": OpenCodeAdapter, "api": APIAdapter}[candidate.engine](candidate, route)
