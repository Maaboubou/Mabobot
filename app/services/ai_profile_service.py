"""The console's unified Profile view; native engine APIs remain independent."""
from __future__ import annotations

import uuid

from app.services.execution_contracts import ExecutionError, RouteCandidate, canonical


def profile_capabilities(service, profile_id, profile):
    candidate = RouteCandidate("probe", profile["engine"], profile_id, str(profile["generation"]),
        profile["model"], profile["account_key"], profile["auth_ref"],
        canonical({"reasoning_effort": profile.get("reasoning_effort", "high")}), profile.get("channel", ""))
    return {key: service.config.capability(candidate, key)["status"]
            for key in ("strict_generation", "tool_calling")}


def register_codex_profile(service, profile_id):
    """Mirror the native configuration when a Profile is selected or edited."""
    from app.services.codex_profile_service import get_codex_profile_service
    native = get_codex_profile_service().get_profile(profile_id)
    if not native or not native.get("available"):
        raise ValueError("Codex Profile 不存在或尚未完成认证")
    for _ in range(3):
        revision, config = service.config.read()
        old = config["profiles"].get(profile_id)
        if old and old["engine"] != "codex":
            raise ValueError("Profile 名称已被 OpenCode 使用，请使用不同名称")
        if old and (old.get("retiring") or old.get("revoked")):
            raise ValueError("此 Profile 已停用，请新建 Profile")
        binding = uuid.uuid4().hex
        profile = old or {"engine": "codex", "auth_binding_generation": binding,
            "auth_ref": "codex:" + profile_id + ":" + binding,
            # Until an account identity is available, share a conservative
            # subscription budget rather than treating every Profile as a new account.
            "account_key": "chatgpt-shared" if native.get("auth_type") == "chatgpt" else "codex:" + profile_id,
            "native_profile_id": profile_id, "channel": "codex", "retiring": False, "revoked": False}
        updated = {**profile, "model": native["model"],
            "reasoning_effort": native.get("reasoning_effort") or "high",
            "runtime_options": {key: native.get(key) for key in
                ("fast_mode", "context_window", "supports_web_search", "supports_vision")}}
        if old and updated == old:
            return old
        updated["generation"] = uuid.uuid4().hex
        config["profiles"][profile_id] = updated
        try:
            service.config.save(config, expected_revision=revision)
            return updated
        except ExecutionError as exc:
            if exc.code != "configuration_conflict":
                raise
    raise ExecutionError("configuration_conflict", "配置正在变化，请重试")


def list_profiles():
    from app.services.codex_profile_service import get_codex_profile_service, CodexProfileError
    from app.services.execution_service import get_execution_service
    from app.services.opencode_profile_service import opencode_profiles
    service = get_execution_service()
    revision, config = service.config.read()
    try:
        native = get_codex_profile_service().list_profiles()
    except CodexProfileError as exc:
        native = {"profiles": [], "warning": str(exc)}
    selected = (config["defaults"].get(config["default_engine"], "") if config["enabled"]
                else native.get("default_profile_id", ""))
    profiles = [{**item, "engine": "codex"} for item in native.get("profiles", [])
                if item.get("setup_status") != "pending"
                and not config["profiles"].get(item["name"], {}).get("retiring")]
    for name, profile in config["profiles"].items():
        if profile["engine"] != "opencode" or profile.get("retiring") or profile.get("revoked"):
            continue
        capabilities = profile_capabilities(service, name, profile)
        auth = opencode_profiles.status(name)
        connection = profile.get('connection', {})
        profiles.append({"name": name, "engine": "opencode", "model": profile["model"],
            "reasoning_effort": profile.get("reasoning_effort", "high"), "auth_type": connection.get('auth_type', 'chatgpt'),
            "auth_source": "device_code", "available": capabilities["tool_calling"] == "verified",
            "capabilities": capabilities, "authentication": auth,
            "provider_name": connection.get('provider_name') or "ChatGPT 登录", "setup_status": "ready",
            'base_url': connection.get('base_url', ''), 'protocol': connection.get('protocol', 'responses'),
            'profile_source': connection.get('source', 'oauth'), 'catalog_provider': connection.get('catalog_provider', ''),
            'context_window': connection.get('context_window', 0), 'supports_vision': connection.get('supports_vision', False)})
    for profile in profiles:
        profile["is_default"] = profile["name"] == selected
    return {**native, "disabled": False, "profiles": profiles, "default_profile_id": selected,
            "revision": revision}


def select_default(profile_id):
    from app.services.execution_service import get_execution_service
    from app.services.codex_profile_service import get_codex_profile_service
    service = get_execution_service()
    _, config = service.config.read()
    profile = config["profiles"].get(profile_id)
    if not profile or profile["engine"] == "codex":
        profile = register_codex_profile(service, profile_id)
        get_codex_profile_service().set_default_profile(profile_id)
    elif profile.get("retiring") or profile.get("revoked"):
        raise ValueError("此 Profile 已停用")
    elif profile_capabilities(service, profile_id, profile)["tool_calling"] != "verified":
        raise ValueError("请先完成 OpenCode 登录和连接验证")
    revision, config = service.config.read()
    config["defaults"][profile["engine"]] = profile_id
    config["default_engine"] = profile["engine"]
    # A native-only installation retains its original execution path.
    config["enabled"] = config["enabled"] or profile["engine"] == "opencode"
    service.config.save(config, expected_revision=revision)
    return {"default_profile_id": profile_id, "engine": profile["engine"]}


def prepare_chat_profile(profile_id):
    """Validate the existing chat picker and enable routing only when needed."""
    from app.services.execution_service import get_execution_service
    from app.services.codex_profile_service import get_codex_profile_service
    service = get_execution_service()
    revision, config = service.config.read()
    profile = config["profiles"].get(profile_id)
    if not profile or profile["engine"] == "codex":
        native = get_codex_profile_service().get_profile(profile_id)
        if not native:
            raise ValueError("Profile 不存在")
        if config["enabled"]:
            register_codex_profile(service, profile_id)
        return
    if profile.get("retiring") or profile.get("revoked"):
        raise ValueError("Profile 已停用")
    if profile_capabilities(service, profile_id, profile)["tool_calling"] != "verified":
        raise ValueError("请先完成 OpenCode 登录和连接验证")
    if not config["enabled"]:
        native_default = get_codex_profile_service().default_profile_id()
        if native_default:
            register_codex_profile(service, native_default)
        revision, config = service.config.read()
        config["defaults"]["codex"] = native_default
        config["enabled"] = True
        service.config.save(config, expected_revision=revision)


def chat_profile_selection(user_id, configured_profile):
    """Expose old engine-chat overrides in the single existing Profile picker."""
    if configured_profile:
        return configured_profile
    from app.services.engine_calls import routing_enabled
    if not routing_enabled():
        return None
    from app.services.execution_service import get_execution_service
    _, config = get_execution_service().config.read()
    chat = config["chats"].get("wechat:" + str(user_id), {})
    engine = chat.get("engine", "inherit")
    if engine == "inherit":
        engine = config["default_engine"]
        return chat.get("profiles", {}).get(engine) or None
    return chat.get("profiles", {}).get(engine) or config["defaults"].get(engine) or None


def sync_chat_selection(user_id, profile_id):
    from app.services.engine_calls import routing_enabled
    if not routing_enabled():
        return
    from app.services.execution_service import get_execution_service
    service = get_execution_service()
    for _ in range(3):
        revision, config = service.config.read()
        chat = config["chats"].setdefault("wechat:" + str(user_id), {})
        if profile_id:
            profile = config["profiles"].get(profile_id)
            if not profile:
                register_codex_profile(service, profile_id)
                continue
            chat["engine"] = profile["engine"]
            chat["profiles"] = {profile["engine"]: profile_id}
        else:
            chat.pop("engine", None)
            chat.pop("profiles", None)
        try:
            service.config.save(config, expected_revision=revision)
            return
        except ExecutionError as exc:
            if exc.code != "configuration_conflict":
                raise
    raise ExecutionError("configuration_conflict", "聊天 Profile 同步失败，请重试保存")
