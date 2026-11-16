"""Profile-oriented console API across Codex and OpenCode."""
from __future__ import annotations

import uuid
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, ConfigDict, SecretStr, field_validator

from app.services import ai_profile_service
from app.services.execution_contracts import ExecutionError
from app.services.execution_service import get_execution_service

router = APIRouter()


class ProfileSelection(BaseModel):
    profile_id: str = Field(min_length=1, max_length=80)


class OpenCodeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
    model: str = Field(default="", max_length=160)
    reasoning_effort: str = Field(default="high", pattern=r"^[A-Za-z0-9_-]{1,40}$")
    auth_type: Literal["chatgpt", "api_key"] = "chatgpt"
    source: Literal["oauth", "catalog", "manual"] = "oauth"
    provider_name: str = Field(default="", max_length=100)
    catalog_provider: str = Field(default="", max_length=80)
    protocol: Literal["responses", "chat_completions", "anthropic"] = "responses"
    base_url: str = Field(default="", max_length=1000)
    api_key: SecretStr = SecretStr("")
    context_window: int | None = Field(default=None, ge=4096, le=10_000_000)
    supports_vision: bool = False

    @field_validator("model", "provider_name")
    @classmethod
    def validate_text(cls, value):
        value = value.strip()
        if any(c in value for c in '{}\r\n\x00'):
            raise ValueError("名称或模型 ID 包含无效字符")
        return value


def save_opencode(request, *, editing=False):
    from app.services.opencode_profile_service import opencode_profiles
    from app.services.opencode_profile_config import validate_connection
    from app.services.execution_contracts import digest
    service = get_execution_service()
    revision, config = service.config.read()
    previous = config['profiles'].get(request.name)
    if editing:
        if not previous or previous['engine'] != 'opencode' or previous.get('retiring') or previous.get('revoked'):
            raise HTTPException(404, 'OpenCode Profile 不存在或已停用')
    elif previous:
        raise HTTPException(409, 'Profile 名称已存在')
    old = (previous or {}).get('connection') or {'auth_type': 'chatgpt'}
    if editing and request.auth_type != old['auth_type']:
        raise HTTPException(400, '如需更换认证方式，请新建 Profile')
    connection = {'auth_type': request.auth_type, 'provider_id': 'openai', 'source': request.source}
    window = (old.get('context_window') if editing and 'context_window' not in request.model_fields_set
              else request.context_window)
    if window is not None:
        connection['context_window'] = window
    secret = request.api_key.get_secret_value()
    binding = (previous or {}).get('auth_binding_generation') or uuid.uuid4().hex
    model = request.model.removeprefix('openai/') if request.auth_type == 'chatgpt' else request.model
    if request.auth_type == 'api_key':
        if not model:
            raise HTTPException(400, '请填写模型 ID')
        if request.protocol == 'anthropic' and request.reasoning_effort != 'default':
            raise HTTPException(400, 'Anthropic 接口请使用模型默认推理设置')
        credential = uuid.uuid4().hex if secret else old.get('credential_id', '')
        connection.update(provider_id='mabobot', provider_name=request.provider_name or '自定义接口',
            protocol=request.protocol, base_url=request.base_url.strip().rstrip('/'), credential_id=credential,
            context_window=window or 128000, supports_vision=request.supports_vision,
            catalog_provider=request.catalog_provider)
        if not credential:
            raise HTTPException(400, '请填写 API Key')
    else:
        model = model or 'gpt-5.4'  # Bootstrap catalog only; the login wizard selects an account model before verification.
    changed_binding = bool(secret) or any(connection.get(k) != old.get(k) for k in ('auth_type', 'base_url', 'protocol'))
    if editing and changed_binding:
        with service.store.connect() as db:
            if db.execute('SELECT 1 FROM engine_config_pins WHERE profile_id=? AND released=0 LIMIT 1', (request.name,)).fetchone():
                raise HTTPException(409, '此 Profile 仍被已接受任务引用，请完成任务后更换连接或密钥')
        binding = uuid.uuid4().hex
    validate_connection(connection)
    if secret:
        opencode_profiles.save_credential(request.name, connection['credential_id'], secret)
    profile = {**(previous or {}), 'engine': 'opencode', 'generation': uuid.uuid4().hex,
        'auth_binding_generation': binding, 'auth_ref': 'opencode:' + request.name + ':' + binding,
        'account_key': ('opencode-api:' + digest(connection['base_url'])[:16]) if request.auth_type == 'api_key' else 'chatgpt-shared',
        'model': model, 'reasoning_effort': request.reasoning_effort, 'native_profile_id': request.name,
        'channel': ('api-' + request.protocol) if request.auth_type == 'api_key' else 'chatgpt-headless',
        'retiring': False, 'revoked': False, 'runtime_options': (previous or {}).get('runtime_options', {}), 'connection': connection}
    config['profiles'][request.name] = profile
    service.config.save(config, expected_revision=revision)
    return {'profile': {'name': request.name, 'model': model, 'auth_type': request.auth_type},
            'requires_login': request.auth_type == 'chatgpt' and not editing, 'requires_verification': True}


def error(exc):
    return HTTPException(409, str(exc))


@router.get("")
def list_profiles():
    return ai_profile_service.list_profiles()


@router.put("/default/selection")
def select_default(request: ProfileSelection):
    from app.services.codex_profile_service import CodexProfileError
    try:
        return ai_profile_service.select_default(request.profile_id)
    except (ValueError, ExecutionError, CodexProfileError) as exc:
        raise error(exc) from exc


@router.post("/opencode")
def create_opencode(request: OpenCodeProfile):
    from app.services.codex_profile_service import get_codex_profile_service, CodexProfileError
    try:
        if get_codex_profile_service().get_profile(request.name):
            raise HTTPException(409, "Profile 名称已被 Codex 使用")
        return save_opencode(request)
    except (ValueError, ExecutionError, CodexProfileError) as exc:
        raise error(exc) from exc


@router.post("/codex/{profile_id}/sync")
def sync_codex(profile_id: str):
    service = get_execution_service()
    if profile_id in service.config.read()[1]["profiles"]:
        try:
            ai_profile_service.register_codex_profile(service, profile_id)
        except (ValueError, ExecutionError) as exc:
            raise error(exc) from exc
    return {"synced": True}


@router.delete("/codex/{profile_id}")
async def delete_codex(profile_id: str):
    from app.api.endpoints.codex_profiles import delete_profile
    result = await delete_profile(profile_id)
    if not result.get("deleted"):
        return result
    service = get_execution_service()
    revision, config = service.config.read()
    replacement = result.get("replacement_default_profile_id", "")
    if replacement and config["defaults"].get("codex") == profile_id:
        ai_profile_service.register_codex_profile(service, replacement)
        revision, config = service.config.read()
    # Native deletion only succeeds after accepted references have drained.
    # Historical config versions retain their binding; the name can now be reused.
    config["profiles"].pop(profile_id, None)
    if config["defaults"].get("codex") == profile_id:
        config["defaults"]["codex"] = replacement
    for chat in config["chats"].values():
        if chat.get("profiles", {}).get("codex") == profile_id:
            chat["profiles"].pop("codex")
        if chat.get("image_profile_id") == profile_id:
            chat.pop("image_profile_id")
    for route in config["routes"].values():
        if route.get("profiles", {}).get("codex") == profile_id:
            route["profiles"].pop("codex")
    try:
        service.config.save(config, expected_revision=revision)
    except (ValueError, ExecutionError) as exc:
        raise error(exc) from exc
    return result


@router.patch("/opencode/{profile_id}")
def update_opencode(profile_id: str, request: OpenCodeProfile):
    if request.name != profile_id:
        raise HTTPException(400, "Profile 名称不能修改")
    try:
        return save_opencode(request, editing=True)
    except (ValueError, ExecutionError) as exc:
        raise error(exc) from exc


@router.delete("/opencode/{profile_id}")
def retire_opencode(profile_id: str):
    service = get_execution_service()
    revision, config = service.config.read()
    profile = config["profiles"].get(profile_id)
    if not profile or profile["engine"] != "opencode":
        raise HTTPException(404, "OpenCode Profile 不存在")
    if config["default_engine"] == "opencode" and config["defaults"]["opencode"] == profile_id:
        raise HTTPException(409, "请先选择其他默认 Profile，再停用此 Profile")
    profile["retiring"] = True
    if config["defaults"].get("opencode") == profile_id:
        config["defaults"]["opencode"] = ""
    try:
        service.config.save(config, expected_revision=revision)
    except (ValueError, ExecutionError) as exc:
        raise error(exc) from exc
    service.registry.retire("opencode", profile_id)
    return {"retiring": True}


@router.get('/opencode/catalog')
def opencode_catalog():
    from app.services.opencode_profile_config import PROVIDERS
    return {'providers': [{'id': key, 'label': value[0], 'base_url': value[1], 'protocol': value[2]}
                          for key, value in PROVIDERS.items()]}


@router.get('/opencode/{profile_id}/models')
def opencode_models(profile_id: str):
    from app.services.opencode_profile_service import opencode_profiles
    profile = get_execution_service().config.read()[1]['profiles'].get(profile_id)
    if not profile or profile['engine'] != 'opencode' or profile.get('retiring') or profile.get('revoked'):
        raise HTTPException(404, 'OpenCode Profile 不存在或已停用')
    if profile.get('connection', {}).get('auth_type') == 'api_key':
        raise HTTPException(400, 'API Profile 请从供应商目录选择模型')
    try:
        return opencode_profiles.inventory(profile_id, profile['model'])
    except (ValueError, ExecutionError) as exc:
        raise error(exc) from exc


@router.get('/opencode/{profile_id}/login/qr')
def opencode_qr(profile_id: str):
    from app.services.opencode_profile_service import opencode_profiles
    from app.services.auth_qr import qr_png
    state = opencode_profiles.status(profile_id)
    url = state.get('url') or ''
    parsed = urlsplit(url)
    if state['status'] != 'waiting' or parsed.scheme != 'https' or parsed.hostname not in {'auth.openai.com', 'chatgpt.com'}:
        raise HTTPException(404, '当前授权链接尚未就绪或已过期')
    return Response(qr_png(url), media_type='image/png', headers={'Cache-Control': 'no-store, max-age=0'})
