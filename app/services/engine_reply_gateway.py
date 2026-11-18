"""Engine-neutral assistant boundary, retaining the Codex-only compatibility API."""
from __future__ import annotations

import copy
from dataclasses import asdict, replace
import json
import time

from app.services.assistant_reply_gateway import CodexReplyGateway, CodexReplyResult, AssistantReplyError
from app.services.execution_contracts import InputEnvelope, canonical, digest


def assistant_engine(chat_id):
    from app.services.engine_calls import routing_enabled
    if not routing_enabled():
        return "codex"
    from app.services.execution_service import get_execution_service
    _, config = get_execution_service().config.read()
    override = config["chats"].get("wechat:" + str(chat_id), {}).get("engine", "inherit")
    return config["default_engine"] if override == "inherit" else override


def legacy_reply_profile(configured_profile, *, chat_id=None):
    from app.services.engine_calls import routing_enabled
    if routing_enabled():
        # Keep inheritance unresolved until admission freezes the selected
        # Profile. The historic field name can now carry either engine.
        return str(configured_profile or "")
    if chat_id is None:
        from app.services.execution_authority import current_admission_source
        source = current_admission_source()
        chat_id = source.chat_id if source else None
    if chat_id is not None and assistant_engine(chat_id) == "opencode":
        return str(configured_profile or "")
    from app.services.codex_profile_service import CodexProfileService
    return CodexProfileService.resolve_assistant_profile_id(configured_profile)


def begin_assistant_task(*, role_signature="", native_profile_id="", timeout=600, delivery=True):
    from app.services.engine_calls import routing_enabled, _admissions
    if not routing_enabled():
        return None
    from app.services.execution_service import get_execution_service
    from app.services.execution_authority import make_context, current_admission_source
    from app.services.engine_tools import allowed_tools
    service = get_execution_service()
    admissions = _admissions.get()
    if admissions is not None and ("assistant", "chat") in admissions:
        return admissions[("assistant", "chat")]
    context = make_context("assistant", "chat", timeout=timeout, delivery=delivery)
    revision, config = service.config.read()
    chat = config["chats"].get(context.source_chat_id, {})
    source = current_admission_source()
    engine = source.engine if source.engine != "follow" else chat.get("engine", "inherit")
    if engine == "inherit":
        engine = config["default_engine"]
    explicit_profile = source.profile_id or native_profile_id
    if explicit_profile:
        profile = config["profiles"].get(explicit_profile)
        if not profile or profile["engine"] == "codex":
            from app.services.ai_profile_service import register_codex_profile
            register_codex_profile(service, explicit_profile)
            revision, config = service.config.read()
        profile = config["profiles"][explicit_profile]
        if source.engine not in {"follow", profile["engine"]}:
            raise AssistantReplyError("指定的 Profile 与执行引擎不一致")
        engine = profile["engine"]
    profile_id = explicit_profile or chat.get("profiles", {}).get(engine) or config["defaults"].get(engine)
    profile = config["profiles"].get(profile_id)
    if not profile:
        raise AssistantReplyError("所选聊天引擎尚未配置可用 Profile")
    models = {"assistant_default": {"kind": "subscription", "model": profile["model"],
                                   "reasoning_effort": profile.get("reasoning_effort") or "high"}}
    # Frozen image account comes from the original image choice, not engine.
    from app.services.image_generation_config import resolve_policy
    from app.services.image_generation_facade import freeze_image_policy
    from app.services.execution_authority import chat_name
    image_profile = chat.get("image_profile_id") or (native_profile_id if engine == "codex" else "")
    if not image_profile:
        from app.services.codex_profile_service import CodexProfileService
        image_profile = CodexProfileService.default_profile_id()
    image_policy = freeze_image_policy(resolve_policy(chat_name=chat_name(context)), native_profile_id=image_profile)
    route = service.config.resolve(chat_id=context.source_chat_id, component="assistant", task_type="chat",
        models=models, mapping={"primary": "assistant_default", "mode": "tools"},
        tools=allowed_tools(context, engine=engine), role_signature=role_signature, image_policy=image_policy, revision=revision,
        engine_override=engine, profile_override=profile_id)
    # Existing explicit Codex Profile bindings still win when no engine-chat
    # override exists. This does not affect ordinary API/plugin cards.
    if route.candidates[0].profile_id != profile_id:
        candidate = replace(route.candidates[0], profile_id=profile_id, generation=str(profile["generation"]),
                            auth_ref=profile["auth_ref"], account_key=profile["account_key"])
        route = replace(route, candidates=(candidate,))
    if engine == "codex":
        # Existing Codex tool calls keep their validated legacy contract. This
        # is not a declaration that Codex meets strict direct-generation rules.
        route = replace(route, mode="legacy")
    run_id, _ = service.admit(context, route)
    if admissions is not None:
        admissions[("assistant", "chat")] = run_id
    return run_id


class AssistantReplyGateway(CodexReplyGateway):
    def reply(self, request):
        from app.services.engine_calls import routing_enabled
        if not routing_enabled():
            return super().reply(request)
        from app.services.execution_service import get_execution_service
        from app.services.execution_authority import current_admission_source
        if current_admission_source() is None:
            # The old Codex-specific programmatic gateway keeps its original
            # semantics until its caller supplies a trusted admission source.
            return super().reply(request)
        started = time.monotonic()
        rules = "\n\n".join(str(m.get("content") or "") for m in request.messages if m.get("role") in {"system", "developer"})
        data = [copy.deepcopy(m) for m in request.messages if m.get("role") not in {"system", "developer"}]
        run_id = begin_assistant_task(role_signature=digest([request.role_name, rules]),
            native_profile_id=request.codex_profile_id, timeout=request.timeout_seconds or 600,
            delivery=request.reply_delivery)
        service = get_execution_service()
        if request.input_files:
            # Text-only OpenCode evidence must not silently discard attachments.
            _, route = service.store.contracts(run_id)
            if route.candidates[0].engine == "opencode":
                raise AssistantReplyError("此 OpenCode Profile 的图片输入尚未验证")
        # Preserve multimodal and history settings across the durable routing
        # boundary. Inline image parts are not file assets.
        reply_options = {
            # Native base composition identifies the persona by message name.
            # Keep structured instructions alongside the engine-neutral text.
            "instruction_messages": [copy.deepcopy(m) for m in request.messages
                                     if m.get("role") in {"system", "developer"}],
            "mabobot_allow_image_input": request.allow_image_input,
            "mabobot_history_enabled": request.history_enabled,
            "mabobot_history_request_id": request.history_request_id,
            "mabobot_history_max_calls": request.history_max_calls,
            "mabobot_history_max_bytes": request.history_max_bytes,
            "mabobot_incremental_context": request.incremental_context,
            "mabobot_fresh_context": request.fresh_context,
        }
        if request.output_schema is not None:
            reply_options["output_schema"] = request.output_schema
        result = service.execute(run_id, InputEnvelope(rules, canonical(data), canonical(list(request.input_files)),
            reply_options_json=canonical(reply_options)))
        if result.status not in {"completed", "skipped"} or service.store.get(run_id)["cancel_requested"]:
            raise AssistantReplyError(result.error or "本轮任务已取消或未完成", error_code=result.error_code)
        assets = json.loads(result.assets_json)
        if assets:
            from app.services.wechat_content_review import get_wechat_content_review
            reviewer = self._file_reviewer or get_wechat_content_review().review_file
            for asset in assets:
                reviewer(asset["path"], chat_name=request.delivery_chat_name or request.chat_name)
        raw = {"execution": asdict(result), "model": result.actual_model, "usage": json.loads(result.usage_json),
               "_mabobot_files": assets, "choices": [{"message": {"content": result.text}}]}
        from app.assistant.task_supervisor import current_task
        task = current_task()
        if task:
            task.completed_result(raw)
        if isinstance(request.usage_capture, list):
            request.usage_capture.append(raw)
        return CodexReplyResult(result.text, assets, json.loads(result.usage_json) or {}, result.actual_model,
            result.engine, time.monotonic() - started, raw,
            tuple(json.loads(result.structured_json).get("at_users", ())) if result.structured_json.startswith("{") else ())
