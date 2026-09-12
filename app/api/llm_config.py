"""
LLM 配置管理 API
提供模型配置、任务模型路由、统计查询等接口
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, SecretStr, Field
from typing import Dict, List, Optional, Any, Literal
import asyncio
from datetime import date
from importlib.metadata import PackageNotFoundError, version as package_version
import logging
import math
import mimetypes
import os
from pathlib import Path
import re
import unicodedata
import time
from urllib.parse import urlsplit, urlunsplit

import litellm
from sqlalchemy.orm import Session

from app.models.base import get_db
from app.models.setting import Setting
from app.dependencies import get_plugin_manager_instance
from app.services.model_route_catalog_service import ModelRouteCatalogService
from app.services.model_registry_service import (
    DEFAULT_OPENAI_COMPATIBLE_BASES,
    get_model_registry_service,
)
from app.services.llm_manager import (
    GEMINI_3_SAMPLING_PARAMETERS,
    get_llm_manager,
    is_gemini_3_model_config,
    reload_llm_config,
)
from app.services.config_service import get_setting, update_setting
from app.services.codex_access_service import codex_access_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/llm", tags=["LLM Management"])

REDACTED_VALUE = "***hidden***"
SENSITIVE_MODEL_KEY_PATTERN = re.compile(
    r"(^|_)(api_?key|secret|token|password|passwd|credential|authorization|access_?key)(_|$)",
    re.IGNORECASE,
)
ENV_REFERENCE_PATTERN = re.compile(r"^env::([A-Za-z_][A-Za-z0-9_]*)$")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MODEL_ID_FORBIDDEN_PATTERN = re.compile(r"[\\/?#\x00-\x1f\x7f]")
IGNORED_IDENTIFIER_CHARACTERS = str.maketrans("", "", "\u200b\u200c\u200d\u2060\ufeff")
CLEARABLE_MODEL_FIELDS = frozenset({
    "api_key",
    "api_base",
    "custom_llm_provider",
    "provider",
    "extra_body",
    "response_format",
    "temperature",
    "max_tokens",
    "context_window_tokens",
    "max_input_tokens",
    "timeout",
    "max_retries",
    "codex_profile_id",
})

CATALOG_PROVIDER_LABELS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "gemini": "Google Gemini",
    "deepseek": "DeepSeek",
    "openrouter": "OpenRouter",
    "azure": "Azure OpenAI",
    "bedrock": "AWS Bedrock",
    "vertex_ai": "Google Vertex AI",
    "mistral": "Mistral AI",
    "groq": "Groq",
    "xai": "xAI",
    "perplexity": "Perplexity",
    "cohere_chat": "Cohere",
    "together_ai": "Together AI",
    "deepinfra": "DeepInfra",
    "fireworks_ai": "Fireworks AI",
    "ollama": "Ollama",
    "ollama_chat": "Ollama Chat",
}
CATALOG_PROVIDER_ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "azure": "AZURE_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "xai": "XAI_API_KEY",
    "perplexity": "PERPLEXITYAI_API_KEY",
    "cohere_chat": "COHERE_API_KEY",
    "together_ai": "TOGETHERAI_API_KEY",
    "deepinfra": "DEEPINFRA_API_KEY",
    "fireworks_ai": "FIREWORKS_AI_API_KEY",
}


def normalize_identifier_text(value: str) -> str:
    """Normalize pasted identifiers without ever touching secret values."""
    return (
        unicodedata.normalize("NFKC", str(value or ""))
        .translate(IGNORED_IDENTIFIER_CHARACTERS)
        .strip()
    )


def validate_model_id(value: str) -> str:
    """Validate the user-facing config alias used in URLs and task mappings."""
    model_id = normalize_identifier_text(value)
    if not model_id:
        raise ValueError("配置名称不能为空")
    if len(model_id) > 80:
        raise ValueError("配置名称不能超过 80 个字符")
    if model_id in {".", ".."} or MODEL_ID_FORBIDDEN_PATTERN.search(model_id):
        raise ValueError("配置名称不能包含 /、\\、?、# 或控制字符")
    return model_id


def _validate_environment_reference(value: str, label: str) -> None:
    if value.startswith("env::") and not ENV_REFERENCE_PATTERN.fullmatch(value):
        raise ValueError(f"{label}环境变量格式无效，应为 env::VAR_NAME")


def normalize_model_payload(payload: Dict[str, Any]) -> tuple[Dict[str, Any], set[str]]:
    """Trim and validate a model editor payload.

    Blank optional connection fields intentionally mean "remove this value" on
    updates. The returned set lets the caller distinguish clearing from omission.
    """
    normalized = dict(payload or {})
    cleared: set[str] = set()

    for key in ("model", "api_key", "api_base", "custom_llm_provider", "provider"):
        if key not in normalized or normalized[key] is None:
            continue
        value = str(normalized[key]).strip()
        if key in CLEARABLE_MODEL_FIELDS and not value:
            normalized.pop(key, None)
            cleared.add(key)
        else:
            normalized[key] = value

    # Environment references are identifiers, not secrets. Remove common
    # zero-width paste artifacts and normalize full-width ASCII before checking
    # them; direct API keys remain byte-for-byte unchanged apart from trim.
    for key in ("api_key", "api_base"):
        value = normalized.get(key)
        if isinstance(value, str):
            reference = normalize_identifier_text(value)
            if reference.startswith("env::"):
                normalized[key] = reference

    model_name = str(normalized.get("model") or "").strip()
    if not model_name:
        raise ValueError("模型名称不能为空")
    normalized["model"] = model_name

    api_key = normalized.get("api_key")
    if isinstance(api_key, str) and api_key != REDACTED_VALUE:
        _validate_environment_reference(api_key, "API 密钥")

    api_base = normalized.get("api_base")
    if isinstance(api_base, str):
        _validate_environment_reference(api_base, "API 地址")
        if not api_base.startswith("env::"):
            try:
                parts = urlsplit(api_base)
            except ValueError as exc:
                raise ValueError("API 地址不是有效 URL") from exc
            if parts.scheme not in {"http", "https"} or not parts.netloc:
                raise ValueError("API 地址必须以 http:// 或 https:// 开头")

    numeric_ranges = {
        "temperature": (0, 2),
        "max_tokens": (1, None),
        "context_window_tokens": (1, None),
        "max_input_tokens": (1, None),
        "timeout": (1, 3600),
        "max_retries": (0, 20),
    }
    for key, (minimum, maximum) in numeric_ranges.items():
        if key not in normalized or normalized[key] is None:
            continue
        value = normalized[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} 必须是数字")
        if not math.isfinite(float(value)) or value < minimum or (maximum is not None and value > maximum):
            range_text = f"{minimum}–{maximum}" if maximum is not None else f"大于等于 {minimum}"
            raise ValueError(f"{key} 必须在 {range_text} 范围内")

    for key in ("extra_body", "response_format"):
        if key in normalized and normalized[key] is not None and not isinstance(normalized[key], dict):
            raise ValueError(f"{key} 必须是 JSON 对象")
        if key in normalized and normalized[key] == {}:
            normalized.pop(key, None)
            cleared.add(key)

    reasoning_effort = normalized.get("codex_reasoning_effort")
    if reasoning_effort not in (None, "") and reasoning_effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Codex 推理强度必须是 low、medium、high、xhigh 或 max")

    credential_mode = normalized.get("credential_mode")
    if credential_mode not in (None, "") and credential_mode not in {"environment", "direct", "none"}:
        raise ValueError("凭据方式必须是 environment、direct 或 none")

    return normalized, cleared


def _infer_model_provider(config: Dict[str, Any]) -> str:
    explicit = str(config.get("custom_llm_provider") or config.get("provider") or "").strip().lower()
    model = str(config.get("model") or "").strip().lower()
    api_base = str(config.get("api_base") or "").strip().lower()
    if explicit in {"local_codex", "local_codex_cli"} or "/api/codex/" in api_base:
        return "local_codex"
    if explicit:
        return "compatible" if explicit == "custom_openai" else explicit
    if model.startswith("openrouter/") or "openrouter.ai" in api_base:
        return "openrouter"
    for provider in ("anthropic", "gemini", "deepseek", "azure", "bedrock", "vertex_ai", "mistral", "groq", "xai"):
        if model.startswith(f"{provider}/"):
            return provider
    if api_base:
        return "compatible"
    return "openai"


def model_management_metadata(
    config: Dict[str, Any],
    *,
    mapped_by: Optional[List[str]] = None,
    credential_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return non-secret editor hints alongside the redacted config."""
    provider = _infer_model_provider(config)
    api_key = str(config.get("api_key") or "").strip()
    environment_match = ENV_REFERENCE_PATTERN.fullmatch(api_key)
    is_local = provider == "local_codex"
    configured_credential_mode = str(config.get("credential_mode") or "").strip().lower()
    if is_local or (configured_credential_mode == "none" and not api_key):
        credential = {"mode": "none", "configured": False, "environment_variable": ""}
    elif environment_match:
        credential = {
            "mode": "environment",
            "configured": credential_status.get("configured", True) if credential_status is not None else True,
            "environment_variable": environment_match.group(1),
            "source": credential_status.get("source", "unknown") if credential_status is not None else "unknown",
        }
    elif api_key:
        credential = {"mode": "direct", "configured": True, "environment_variable": ""}
    else:
        credential = {"mode": "missing", "configured": False, "environment_variable": ""}
    references = list(mapped_by or [])
    return {
        "provider": provider,
        "credential": credential,
        "share_ready": credential["mode"] in {"environment", "none"},
        "has_custom_endpoint": bool(str(config.get("api_base") or "").strip()),
        "mapped_by": references,
        "mapping_count": len(references),
        "disabled_parameters": (
            sorted(GEMINI_3_SAMPLING_PARAMETERS)
            if is_gemini_3_model_config(config)
            else []
        ),
    }


def _model_mapping_references(mappings: Dict[str, Any], model_id: str) -> List[str]:
    references: List[str] = []
    for plugin_name, plugin_mappings in (mappings or {}).items():
        if not isinstance(plugin_mappings, dict):
            continue
        for call_type, mapping in plugin_mappings.items():
            if not isinstance(mapping, dict):
                continue
            if mapping.get("primary") == model_id or model_id in (mapping.get("fallback") or []):
                references.append(f"{plugin_name}.{call_type}")
    return references


def public_management_model_config(
    config: Dict[str, Any],
    *,
    model_id: str = "",
    mappings: Optional[Dict[str, Any]] = None,
    credential_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    public = public_model_config(config)
    public["_management"] = model_management_metadata(
        config,
        mapped_by=_model_mapping_references(mappings or {}, model_id) if model_id else [],
        credential_status=credential_status,
    )
    return public


def _catalog_model_name(provider: str, raw_name: str) -> str:
    name = str(raw_name or "").strip()
    if not name or provider == "openai" or name.startswith(f"{provider}/"):
        return name
    return f"{provider}/{name}"


def _catalog_info(provider: str, raw_name: str, canonical_name: str) -> Dict[str, Any]:
    cost_map = getattr(litellm, "model_cost", {}) or {}
    candidates = (canonical_name, raw_name)
    for candidate in candidates:
        info = cost_map.get(candidate)
        if isinstance(info, dict):
            return info
    return {}


def build_model_catalog(provider: str, query: str = "", limit: int = 120) -> List[Dict[str, Any]]:
    """Build a chat-model catalog from the installed LiteLLM release."""
    provider_key = str(provider or "").strip().lower()
    source_provider = "openai" if provider_key == "local_codex" else provider_key
    models_by_provider = getattr(litellm, "models_by_provider", {}) or {}
    raw_models = models_by_provider.get(source_provider, []) or []
    needle = str(query or "").strip().lower()
    catalog: List[Dict[str, Any]] = []
    seen = set()

    for raw_name in raw_models:
        raw_text = str(raw_name or "").strip()
        if provider_key == "local_codex" and not raw_text.lower().startswith(("gpt-", "o1", "o3", "o4")):
            continue
        canonical = _catalog_model_name(source_provider, raw_text)
        if not canonical or canonical in seen or (needle and needle not in canonical.lower()):
            continue
        info = _catalog_info(source_provider, raw_text, canonical)
        mode = str(info.get("mode") or "").lower()
        if mode and mode not in {"chat", "completion", "responses"}:
            continue
        deprecation = str(info.get("deprecation_date") or "").strip()
        deprecated = False
        if deprecation:
            try:
                deprecated = date.fromisoformat(deprecation[:10]) <= date.today()
            except ValueError:
                deprecated = False
        lower_name = canonical.lower()
        dated = bool(re.search(r"(?:19|20)\\d{2}[-_]?(?:0[1-9]|1[0-2])[-_]?(?:0[1-9]|[12]\\d|3[01])$", lower_name))
        preview = any(word in lower_name for word in ("preview", "experimental", "beta"))
        specialized = any(word in lower_name for word in ("audio", "realtime", "image", "embedding", "transcribe", "tts"))
        recommended = not (deprecated or dated or preview or specialized)
        catalog.append({
            "id": canonical,
            "provider": provider_key,
            "mode": mode or "chat",
            "recommended": recommended,
            "deprecated": deprecated,
            "max_input_tokens": info.get("max_input_tokens"),
            "max_output_tokens": info.get("max_output_tokens") or info.get("max_tokens"),
            "supports_vision": bool(info.get("supports_vision")),
            "supports_reasoning": bool(info.get("supports_reasoning")),
            "supports_web_search": bool(info.get("supports_web_search")),
            "supports_function_calling": bool(info.get("supports_function_calling")),
        })
        seen.add(canonical)

    catalog.sort(key=lambda item: (
        item["deprecated"],
        not item["recommended"],
        len(item["id"]),
        item["id"].lower(),
    ))
    return catalog[: max(1, min(int(limit or 120), 300))]


def list_catalog_providers() -> List[Dict[str, Any]]:
    models_by_provider = getattr(litellm, "models_by_provider", {}) or {}
    ignored = {"text-completion-openai", "ollama_chat"}
    providers = []
    for provider, models in models_by_provider.items():
        provider_key = str(provider or "").strip()
        if not provider_key or provider_key in ignored or not models:
            continue
        providers.append({
            "id": provider_key,
            "label": CATALOG_PROVIDER_LABELS.get(provider_key, provider_key.replace("_", " ").title()),
            "model_count": len(models),
            "environment_variable": CATALOG_PROVIDER_ENV_VARS.get(
                provider_key,
                f"{re.sub(r'[^A-Za-z0-9]+', '_', provider_key).upper()}_API_KEY",
            ),
        })
    providers.sort(key=lambda item: (item["label"].lower(), item["id"]))
    return providers


def get_litellm_version() -> str:
    try:
        return package_version("litellm")
    except PackageNotFoundError:
        return str(getattr(litellm, "__version__", "unknown") or "unknown")


def public_model_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deep-redacted model configuration for management APIs."""

    def redact(value: Any, key: str = "") -> Any:
        if SENSITIVE_MODEL_KEY_PATTERN.search(key):
            return REDACTED_VALUE if value not in (None, "", [], {}) else value
        if isinstance(value, dict):
            return {child_key: redact(child_value, str(child_key)) for child_key, child_value in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    return redact(config)


def merge_redacted_config(existing: Any, incoming: Any) -> Any:
    """Merge an editor payload while retaining values represented by redaction sentinels."""
    if incoming == REDACTED_VALUE:
        return existing
    if isinstance(incoming, dict):
        current = existing if isinstance(existing, dict) else {}
        merged = dict(current)
        for key, value in incoming.items():
            merged[key] = merge_redacted_config(current.get(key), value)
        return merged
    if isinstance(incoming, list):
        current = existing if isinstance(existing, list) else []
        return [
            merge_redacted_config(current[index] if index < len(current) else None, value)
            for index, value in enumerate(incoming)
        ]
    return incoming


def contains_redacted_value(value: Any) -> bool:
    if value == REDACTED_VALUE:
        return True
    if isinstance(value, dict):
        return any(contains_redacted_value(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_redacted_value(item) for item in value)
    return False


def public_proxy_config(proxy_url: str) -> Dict[str, Any]:
    """Hide embedded proxy credentials while preserving a useful status label."""
    value = str(proxy_url or "").strip()
    if not value:
        return {"proxy_url": "", "display_url": "", "enabled": False, "sensitive": False}
    try:
        parts = urlsplit(value)
    except ValueError:
        return {"proxy_url": "", "display_url": "已配置", "enabled": True, "sensitive": True}
    has_credentials = parts.username is not None or parts.password is not None
    if not has_credentials:
        return {"proxy_url": value, "display_url": value, "enabled": True, "sensitive": False}
    try:
        host = parts.hostname or ""
        port_value = parts.port
    except ValueError:
        return {"proxy_url": "", "display_url": "已配置", "enabled": True, "sensitive": True}
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{port_value}" if port_value else ""
    display = urlunsplit((parts.scheme, f"***:***@{host}{port}", parts.path, parts.query, parts.fragment))
    return {"proxy_url": "", "display_url": display, "enabled": True, "sensitive": True}


# ==================== Pydantic Models ====================

class ModelConfig(BaseModel):
    """模型配置"""
    model: str
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    custom_llm_provider: Optional[str] = None
    provider: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    context_window_tokens: Optional[int] = None
    max_input_tokens: Optional[int] = None
    timeout: Optional[int] = None
    max_retries: Optional[int] = None
    supports_vision: Optional[bool] = None
    billing_mode: Optional[Literal["auto", "free", "included", "unknown"]] = None
    cost_currency: Optional[str] = Field(default=None, pattern=r"^[A-Z]{3}$")
    input_cost_per_token: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    output_cost_per_token: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    cache_read_input_token_cost: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    cache_creation_input_token_cost: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    extra_body: Optional[Dict[str, Any]] = None
    enable_web_search: Optional[bool] = False
    response_format: Optional[Dict[str, Any]] = None
    codex_reasoning_effort: Optional[str] = None
    codex_web_search: Optional[bool] = None
    codex_sandbox: Optional[str] = None
    codex_isolated_workdir: Optional[bool] = None
    codex_profile_id: Optional[str] = None
    credential_mode: Optional[str] = None
    clear_fields: Optional[List[str]] = None


class ProxyConfig(BaseModel):
    """HTTP 代理配置"""
    proxy_url: str = ""   # 例: http://100.x.x.x:6688，空字符串 = 禁用


class SharedCredentialUpdate(BaseModel):
    """A local secret referenced by an env::NAME model configuration."""
    value: str


class TaskModelRoute(BaseModel):
    """单项任务的模型路由配置。"""
    primary: str
    fallback: Optional[List[str]] = []
    override_params: Optional[Dict[str, Any]] = {}


class UpdateMappingRequest(BaseModel):
    """更新任务模型路由请求。"""
    owner_id: str
    call_type: str
    mapping: TaskModelRoute


class ReorderRequest(BaseModel):
    """重新排序请求"""
    order: List[str]


class ModelCatalogDiscoveryRequest(BaseModel):
    """Connection details used only for an explicit model-catalog refresh."""
    provider: str = ""
    q: str = ""
    limit: int = 300
    model_id: str = ""
    api_base: str = ""
    credential_name: str = ""
    api_key: SecretStr = SecretStr("")
    refresh: bool = True


# ==================== 模型配置接口 ====================

@router.get("/models")
async def get_models(db: Session = Depends(get_db)):
    """获取所有模型配置"""
    try:
        llm_manager = get_llm_manager()
        models = llm_manager.config.get("models", {})
        
        mappings = llm_manager.config.get("plugin_mappings", {})
        safe_models = {
            model_id: public_management_model_config(
                config,
                model_id=model_id,
                mappings=mappings,
                credential_status=_model_shared_credential_status(config, db),
            )
            for model_id, config in models.items()
        }
        
        return {"status": "success", "data": safe_models}
    except Exception as e:
        logger.error(f"获取模型配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/models/reorder")
async def reorder_models(req: ReorderRequest):
    """重新排序模型"""
    try:
        llm_manager = get_llm_manager()
        llm_manager.reorder_models(req.order)
        return {"status": "success", "message": "模型顺序已更新"}
    except Exception as e:
        logger.error(f"模型重新排序失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _resolve_catalog_reference(value: Any, db: Session) -> str:
    """Resolve env:: references without exposing their values in API responses."""
    text = str(value or "").strip()
    match = ENV_REFERENCE_PATTERN.fullmatch(text)
    if not match:
        return text
    name = match.group(1)
    return str(get_setting(name, "", db=db) or os.getenv(name, "") or "").strip()


def _same_catalog_base(left: str, right: str) -> bool:
    try:
        left_parts = urlsplit(str(left or "").strip())
        right_parts = urlsplit(str(right or "").strip())
    except ValueError:
        return False
    return (
        left_parts.scheme.lower(),
        left_parts.netloc.lower(),
        left_parts.path.rstrip("/"),
    ) == (
        right_parts.scheme.lower(),
        right_parts.netloc.lower(),
        right_parts.path.rstrip("/"),
    )


def _resolve_catalog_connection(
    *,
    provider: str,
    model_id: str,
    api_base: str,
    credential_name: str,
    direct_api_key: str,
    db: Session,
) -> tuple[str, str, str]:
    """Resolve a discovery connection while preventing stored-key endpoint overrides."""
    provider_key = str(provider or "").strip().lower()
    requested_base = _resolve_catalog_reference(api_base, db)
    explicit_key = str(direct_api_key or "").strip()
    models = get_llm_manager().config.get("models", {})
    saved_config = models.get(str(model_id or "").strip())

    if isinstance(saved_config, dict):
        saved_provider = _infer_model_provider(saved_config)
        saved_base = _resolve_catalog_reference(saved_config.get("api_base"), db)
        saved_key = _resolve_catalog_reference(saved_config.get("api_key"), db)
        # A newly entered key belongs to the form's endpoint. Otherwise the
        # stored key may only be sent to the endpoint stored alongside it.
        if explicit_key:
            return provider_key or saved_provider, requested_base or saved_base, explicit_key
        return saved_provider, saved_base, saved_key

    if explicit_key:
        return provider_key, requested_base, explicit_key

    name = str(credential_name or "").strip() or CATALOG_PROVIDER_ENV_VARS.get(provider_key, "")
    stored_key = ""
    if name:
        normalized_name = _validate_credential_name(name)
        default_base = DEFAULT_OPENAI_COMPATIBLE_BASES.get(provider_key, "")
        safe_default_target = not requested_base or (default_base and _same_catalog_base(requested_base, default_base))
        if safe_default_target:
            stored_key = str(get_setting(normalized_name, "", db=db) or os.getenv(normalized_name, "") or "").strip()
    return provider_key, requested_base, stored_key


async def _build_dynamic_model_catalog(
    *,
    provider: str,
    query: str,
    limit: int,
    model_id: str,
    api_base: str,
    credential_name: str,
    direct_api_key: str,
    force_refresh: bool,
    db: Session,
) -> Dict[str, Any]:
    provider_key = str(provider or "").strip().lower()
    if not provider_key:
        return {
            "models": [],
            "discovery": {
                "status": "idle",
                "attempted": False,
                "models_count": 0,
                "message": "选择供应商后可同步服务端模型",
            },
        }

    resolved_provider, resolved_base, resolved_key = _resolve_catalog_connection(
        provider=provider_key,
        model_id=model_id,
        api_base=api_base,
        credential_name=credential_name,
        direct_api_key=direct_api_key,
        db=db,
    )
    registry = get_model_registry_service()
    discovery = await registry.discover(
        provider=resolved_provider,
        api_base=resolved_base,
        api_key=resolved_key,
        force_refresh=force_refresh,
    )
    static_models = build_model_catalog(resolved_provider, query=query, limit=300)
    models = registry.merge_catalog(
        provider=resolved_provider,
        static_models=static_models,
        discovery=discovery,
        query=query,
        limit=limit,
    )
    public_discovery = {key: value for key, value in discovery.items() if key != "models"}
    public_discovery["models_count"] = len(discovery.get("models") or [])
    return {"models": models, "discovery": public_discovery}


@router.get("/models/catalog")
async def get_model_catalog(
    provider: str = Query("", max_length=80),
    q: str = Query("", max_length=120),
    limit: int = Query(120, ge=1, le=300),
    model_id: str = Query("", max_length=80),
    refresh: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Return a live-aware catalog enriched by the installed LiteLLM metadata."""
    try:
        provider_key = str(provider or "").strip().lower()
        catalog = await _build_dynamic_model_catalog(
            provider=provider_key,
            query=q,
            limit=limit,
            model_id=model_id,
            api_base="",
            credential_name="",
            direct_api_key="",
            force_refresh=refresh,
            db=db,
        )
        return {
            "status": "success",
            "data": {
                "source": "dynamic-registry",
                "version": get_litellm_version(),
                "providers": list_catalog_providers(),
                **catalog,
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("获取动态模型目录失败: %s", exc)
        raise HTTPException(status_code=500, detail="无法读取模型目录") from exc


@router.post("/models/catalog/discover")
async def discover_model_catalog(
    request: ModelCatalogDiscoveryRequest,
    db: Session = Depends(get_db),
):
    """Explicitly refresh a provider catalog, including unsaved form credentials."""
    direct_api_key = request.api_key.get_secret_value()
    if len(request.provider) > 80 or len(request.q) > 120 or len(request.model_id) > 80:
        raise HTTPException(status_code=422, detail="模型目录查询参数过长")
    if len(request.api_base) > 1000 or len(request.credential_name) > 120 or len(direct_api_key) > 16384:
        raise HTTPException(status_code=422, detail="模型连接参数过长")
    if request.limit < 1 or request.limit > 300:
        raise HTTPException(status_code=422, detail="limit 必须在 1–300 之间")
    try:
        catalog = await _build_dynamic_model_catalog(
            provider=request.provider,
            query=request.q,
            limit=request.limit,
            model_id=request.model_id,
            api_base=request.api_base,
            credential_name=request.credential_name,
            direct_api_key=direct_api_key,
            force_refresh=bool(request.refresh),
            db=db,
        )
        return {
            "status": "success",
            "data": {
                "source": "dynamic-registry",
                "version": get_litellm_version(),
                "providers": list_catalog_providers(),
                **catalog,
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("刷新动态模型目录失败: %s", exc)
        raise HTTPException(status_code=500, detail="无法刷新模型目录") from exc


def _validate_credential_name(name: str) -> str:
    normalized = normalize_identifier_text(name)
    if len(normalized) > 120 or not ENV_NAME_PATTERN.fullmatch(normalized):
        raise HTTPException(status_code=422, detail="凭据名称只能包含字母、数字和下划线，且不能以数字开头")
    return normalized


def shared_credential_status(name: str, db: Session) -> Dict[str, Any]:
    """Return presence/source metadata without ever returning the secret."""
    normalized = _validate_credential_name(name)
    setting = db.query(Setting).filter(Setting.key == normalized).first()
    database_configured = bool(setting and str(setting.get_value() or "").strip())
    environment_configured = bool(str(os.getenv(normalized, "") or "").strip())
    source = "database" if database_configured else "environment" if environment_configured else "missing"
    return {
        "name": normalized,
        "configured": database_configured or environment_configured,
        "source": source,
        "source_label": {
            "database": "已保存在本机数据库",
            "environment": "已由 .env / 启动环境提供",
            "missing": "尚未配置密钥值",
        }[source],
    }


def _model_shared_credential_status(config: Dict[str, Any], db: Session) -> Optional[Dict[str, Any]]:
    match = ENV_REFERENCE_PATTERN.fullmatch(str(config.get("api_key") or "").strip())
    return shared_credential_status(match.group(1), db) if match else None


@router.get("/credentials/{name}")
def get_shared_credential_status(name: str, db: Session = Depends(get_db)):
    """Check whether a share-safe model credential can resolve locally."""
    return {"status": "success", "data": shared_credential_status(name, db)}


@router.put("/credentials/{name}")
def save_shared_credential(
    name: str,
    request: SharedCredentialUpdate,
    db: Session = Depends(get_db),
):
    """Store a model credential outside the shareable model JSON file."""
    normalized = _validate_credential_name(name)
    value = str(request.value or "").strip()
    if not value:
        raise HTTPException(status_code=422, detail="密钥值不能为空")
    if len(value) > 16384:
        raise HTTPException(status_code=422, detail="密钥值过长")

    setting = db.query(Setting).filter(Setting.key == normalized).first()
    if setting is None:
        setting = Setting(
            key=normalized,
            category="model_credentials",
            description="由模型连接页管理的本机共享凭据",
        )
        db.add(setting)
    setting.set_value(value)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("保存模型共享凭据失败: %s", exc)
        raise HTTPException(status_code=500, detail="无法保存本机凭据") from exc

    from app.services import config_service
    from app.services.settings_service import SettingsService

    config_service._settings_cache.pop(normalized, None)
    config_service._last_cache_time = 0
    SettingsService._get_from_db_cached.cache_clear()
    return {"status": "success", "data": shared_credential_status(normalized, db)}


@router.get("/models/{model_id}")
async def get_model(model_id: str, db: Session = Depends(get_db)):
    """获取单个模型配置"""
    try:
        llm_manager = get_llm_manager()
        models = llm_manager.config.get("models", {})
        
        if model_id not in models:
            raise HTTPException(status_code=404, detail=f"模型 {model_id} 不存在")
        
        return {
            "status": "success",
            "data": public_management_model_config(
                models[model_id],
                model_id=model_id,
                mappings=llm_manager.config.get("plugin_mappings", {}),
                credential_status=_model_shared_credential_status(models[model_id], db),
            ),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取模型配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/models/{model_id}")
async def add_model(model_id: str, config: ModelConfig):
    """添加新模型"""
    try:
        llm_manager = get_llm_manager()
        
        try:
            model_id = validate_model_id(model_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # 检查是否已存在
        if model_id in llm_manager.config.get("models", {}):
            raise HTTPException(status_code=400, detail=f"模型 {model_id} 已存在，请使用 PUT 更新")
        
        raw_payload = (
            config.model_dump(exclude_none=True)
            if hasattr(config, "model_dump")
            else config.dict(exclude_none=True)
        )
        raw_payload.pop("clear_fields", None)
        try:
            payload, _ = normalize_model_payload(raw_payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if contains_redacted_value(payload):
            raise HTTPException(status_code=422, detail="新增模型不能包含脱敏占位符，请重新填写凭据")
        llm_manager.update_model(model_id, payload)
        
        return {"status": "success", "message": f"模型 {model_id} 已添加"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"添加模型失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/models/{model_id}")
async def update_model(model_id: str, config: ModelConfig, new_id: Optional[str] = None):
    """更新模型配置"""
    try:
        llm_manager = get_llm_manager()
        
        if model_id not in llm_manager.config.get("models", {}):
            raise HTTPException(status_code=404, detail=f"模型 {model_id} 不存在")
        
        # The editor intentionally exposes only common fields. Merge instead
        # of replacing so provider-specific capabilities (vision, retries,
        # pricing, etc.) survive a Context Window edit.
        dump_kwargs = {"exclude_none": True, "exclude_unset": True}
        incoming = (
            config.model_dump(**dump_kwargs)
            if hasattr(config, "model_dump")
            else config.dict(**dump_kwargs)
        )
        requested_clear_fields = set(incoming.pop("clear_fields", []) or [])
        invalid_clear_fields = requested_clear_fields - CLEARABLE_MODEL_FIELDS
        if invalid_clear_fields:
            raise HTTPException(
                status_code=422,
                detail=f"不允许清除字段: {', '.join(sorted(invalid_clear_fields))}",
            )
        try:
            incoming, cleared_fields = normalize_model_payload(incoming)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # Validate the entire payload before renaming so a rejected edit never
        # leaves mappings pointing at a partially updated alias.
        if new_id:
            try:
                new_id = validate_model_id(new_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if new_id != model_id:
                if new_id in llm_manager.config.get("models", {}):
                    raise HTTPException(status_code=400, detail=f"新模型 ID {new_id} 已存在")
                llm_manager.rename_model(model_id, new_id)
                model_id = new_id

        existing = dict(llm_manager.config.get("models", {}).get(model_id, {}))
        merged = merge_redacted_config(existing, incoming)
        for key in cleared_fields | requested_clear_fields:
            merged.pop(key, None)
        # The editor uses custom_llm_provider as the canonical provider field.
        # Remove the legacy alias whenever the provider is explicitly changed.
        if (
            "custom_llm_provider" in incoming
            or "custom_llm_provider" in cleared_fields
            or "custom_llm_provider" in requested_clear_fields
        ):
            merged.pop("provider", None)
        llm_manager.update_model(model_id, merged)
        
        return {"status": "success", "message": f"模型 {model_id} 已更新"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新模型失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/models/{model_id}")
async def delete_model(model_id: str):
    """删除模型"""
    try:
        llm_manager = get_llm_manager()
        
        if model_id not in llm_manager.config.get("models", {}):
            raise HTTPException(status_code=404, detail=f"模型 {model_id} 不存在")
        
        llm_manager.delete_model(model_id)
        
        return {"status": "success", "message": f"模型 {model_id} 已删除"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除模型失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/models/{model_id}/test")
async def test_model_connectivity(model_id: str):
    """测试单个模型的连通性"""
    try:
        llm_manager = get_llm_manager()
        models = llm_manager.config.get("models", {})

        if model_id not in models:
            raise HTTPException(status_code=404, detail=f"模型 {model_id} 不存在")

        model_cfg = models[model_id]
        llm_manager.apply_proxy_env_vars()

        params = llm_manager._build_single_model_params(
            model_cfg=model_cfg,
            messages=[{"role": "user", "content": "Hello"}],
            caller_tools=None,
        )
        # _build_single_model_params 会带上 Mabobot 内部元数据用于主调用链的视觉降级判断；
        # 这里是模型连通性测试，直接调用 LiteLLM 前必须移除，避免透传到 OpenAI client。
        params.pop("_mabobot_supports_vision", None)
        params["drop_params"] = True

        t0 = time.perf_counter()
        try:
            if llm_manager._is_local_codex_model(model_cfg):
                response, response_time, response_text = await asyncio.to_thread(
                    llm_manager._call_local_codex,
                    model_cfg,
                    [{"role": "user", "content": "请只回复 OK，不要解释。"}],
                    params,
                    False,
                )
                latency_ms = int(response_time * 1000)
                actual_model = llm_manager._resolve_local_codex_model(response, model_cfg)
                return {
                    "status": "success",
                    "data": {
                        "model_id": model_id,
                        "configured_model": model_cfg.get("model", ""),
                        "actual_model": actual_model,
                        "ok": bool(str(response_text or "").strip()),
                        "latency_ms": latency_ms,
                        "message": "模型调用成功"
                    }
                }

            response = await asyncio.to_thread(litellm.completion, **params)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            actual_model = llm_manager._resolve_actual_model(response, model_cfg)

            return {
                "status": "success",
                "data": {
                    "model_id": model_id,
                    "configured_model": model_cfg.get("model", ""),
                    "actual_model": actual_model,
                    "ok": True,
                    "latency_ms": latency_ms,
                    "message": "模型调用成功"
                }
            }
        except Exception as e:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            error_msg = str(e).strip() or "未知错误"
            if len(error_msg) > 180:
                error_msg = error_msg[:180] + "..."

            return {
                "status": "success",
                "data": {
                    "model_id": model_id,
                    "configured_model": model_cfg.get("model", ""),
                    "actual_model": None,
                    "ok": False,
                    "latency_ms": latency_ms,
                    "message": f"{type(e).__name__}: {error_msg}"
                }
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"测试模型连通性失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 任务模型路由接口 ====================

@router.get("/route-owners")
async def get_route_owners(plugin_manager=Depends(get_plugin_manager_instance)):
    """返回核心组件与插件组成的模型路由主体目录。"""
    try:
        llm_manager = get_llm_manager()
        mappings = llm_manager.config.get("plugin_mappings", {})
        owners = ModelRouteCatalogService(plugin_manager).list_owners(mappings.keys())
        return {"status": "success", "data": owners}
    except Exception as exc:
        logger.error("获取模型路由主体失败: %s", exc)
        raise HTTPException(status_code=500, detail="无法读取模型路由主体目录") from exc

@router.get("/mappings")
async def get_mappings():
    """获取所有任务模型路由。"""
    try:
        llm_manager = get_llm_manager()
        mappings = llm_manager.config.get("plugin_mappings", {})
        return {"status": "success", "data": public_model_config(mappings)}
    except Exception as e:
        logger.error(f"获取任务模型路由失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/mappings/{owner_id}")
async def get_owner_mappings(owner_id: str):
    """获取单个路由主体的所有任务模型路由。"""
    try:
        llm_manager = get_llm_manager()
        mappings = llm_manager.config.get("plugin_mappings", {})
        
        if owner_id not in mappings:
            raise HTTPException(status_code=404, detail=f"路由主体 {owner_id} 没有配置")
        
        return {"status": "success", "data": public_model_config(mappings[owner_id])}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取任务模型路由失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/mappings/{owner_id}/{call_type}")
async def update_mapping(owner_id: str, call_type: str, mapping: TaskModelRoute):
    """更新一项任务模型路由。"""
    if owner_id in {"assistant", "builtin_chatbot"} and call_type == "chat":
        raise HTTPException(
            status_code=400,
            detail="AI 助手最终回复仅由 Codex 驱动，不能创建通用模型路由",
        )
    try:
        llm_manager = get_llm_manager()
        existing = (
            llm_manager.config.get("plugin_mappings", {})
            .get(owner_id, {})
            .get(call_type, {})
        )
        incoming = mapping.dict(exclude_none=True)
        llm_manager.update_mapping(
            owner_id,
            call_type,
            merge_redacted_config(existing, incoming),
        )
        
        return {"status": "success", "message": f"任务模型路由 {owner_id}.{call_type} 已更新"}
    except Exception as e:
        logger.error(f"更新任务模型路由失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/mappings/{owner_id}/{call_type}")
async def delete_mapping(owner_id: str, call_type: str):
    """删除一项任务模型路由。"""
    try:
        llm_manager = get_llm_manager()
        
        if owner_id not in llm_manager.config.get("plugin_mappings", {}):
            raise HTTPException(status_code=404, detail=f"路由主体 {owner_id} 没有配置")
        
        if call_type not in llm_manager.config["plugin_mappings"][owner_id]:
            raise HTTPException(status_code=404, detail=f"调用类型 {call_type} 不存在")
        
        del llm_manager.config["plugin_mappings"][owner_id][call_type]
        llm_manager.save_config()
        
        return {"status": "success", "message": f"任务模型路由 {owner_id}.{call_type} 已删除"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除任务模型路由失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 代理设置接口 ====================

@router.get("/proxy")
async def get_proxy():
    """获取当前 HTTP 代理配置"""
    try:
        proxy_url = get_setting("LLM_PROXY_URL", "")
        return {"status": "success", "data": public_proxy_config(proxy_url)}
    except Exception as e:
        logger.error(f"获取代理配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/proxy")
async def update_proxy(config: ProxyConfig):
    """保存 HTTP 代理配置"""
    try:
        proxy_url = config.proxy_url.strip()
        success = update_setting("LLM_PROXY_URL", proxy_url)
        if not success:
            raise HTTPException(status_code=500, detail="保存代理配置失败")

        # 立即将代理写入 os.environ，无需重启即可生效
        llm_manager = get_llm_manager()
        llm_manager.mark_proxy_dirty()   # 使缓存失效
        llm_manager.apply_proxy_env_vars()  # 立即重新应用

        public_proxy = public_proxy_config(proxy_url)
        if proxy_url:
            logger.info("🌐 LLM HTTP 代理已更新并生效: %s", public_proxy["display_url"])
        else:
            logger.info("🌐 LLM HTTP 代理已禁用（直连模式）")
        return {
            "status": "success",
            "message": f"代理已{'设置为 ' + public_proxy['display_url'] if proxy_url else '禁用'}",
            "data": public_proxy,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新代理配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))



class ProxyTestRequest(BaseModel):
    """代理连通性测试请求"""
    proxy_url: str = ""          # 留空 = 使用当前 DB 配置
    test_url: str = "https://api.openai.com/v1/models"  # 测试目标 URL


@router.post("/proxy/test")
async def test_proxy(req: ProxyTestRequest):
    """
    测试直连和代理连通性，返回延迟和状态。
    同时测试直连和（如果有代理）代理连接，方便对比。
    """
    import time
    try:
        import httpx
    except ImportError:
        raise HTTPException(status_code=500, detail="httpx 未安装，无法执行测试")

    # 确定代理 URL：优先使用请求中的，否则读 DB
    proxy_url = req.proxy_url.strip() if req.proxy_url.strip() else get_setting("LLM_PROXY_URL", "")
    test_url = req.test_url.strip() or "https://api.openai.com/v1/models"

    results = {}

    async def _probe(label: str, proxies: dict | None):
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxies=proxies, timeout=10, verify=False) as client:
                r = await client.get(test_url)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            # 401/403/429 仍然说明网络通了，视为成功
            ok = r.status_code < 500
            results[label] = {
                "ok": ok,
                "status_code": r.status_code,
                "latency_ms": latency_ms,
                "message": f"HTTP {r.status_code} ({latency_ms} ms)"
            }
        except Exception as exc:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            message = str(exc)[:120]
            if proxy_url:
                message = message.replace(proxy_url, public_proxy_config(proxy_url)["display_url"])
            results[label] = {
                "ok": False,
                "status_code": None,
                "latency_ms": latency_ms,
                "message": f"{type(exc).__name__}: {message}"
            }

    # 并发测试：直连 + 代理（如果有）
    import asyncio
    tasks = [_probe("direct", None)]
    if proxy_url:
        tasks.append(_probe("proxy", {"http://": proxy_url, "https://": proxy_url}))

    await asyncio.gather(*tasks)

    return {
        "status": "success",
        "test_url": test_url,
        "proxy_url": public_proxy_config(proxy_url)["display_url"] or None,
        "results": results
    }


# ==================== 配置管理接口 ====================

@router.post("/reload")
async def reload_config():
    """手动重新加载配置"""
    try:
        reload_llm_config()
        return {"status": "success", "message": "配置已重新加载"}
    except Exception as e:
        logger.error(f"重新加载配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config")
async def get_config():
    """获取完整配置（用于导出）"""
    try:
        llm_manager = get_llm_manager()
        config = llm_manager.get_config()
        
        # 隐藏敏感信息
        safe_config = {
            "models": {},
            "plugin_mappings": public_model_config(config.get("plugin_mappings", {})),
        }
        
        for model_id, model_config in config.get("models", {}).items():
            safe_config["models"][model_id] = public_model_config(model_config)
        
        return {"status": "success", "data": safe_config}
    except Exception as e:
        logger.error(f"获取配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 统计接口 ====================

@router.get("/usage/requests")
def get_usage_requests(
    period: str = Query("today", pattern="^(today|7d|30d|session|total)$"),
    subject: Optional[str] = None, task: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
):
    return {"status": "success", "data": get_llm_manager().usage_service.requests(
        period=period, subject=subject, task=task, limit=limit, offset=offset)}


@router.get("/usage")
def get_usage(
    period: str = Query("today", pattern="^(today|7d|30d|session|total)$"),
    view: str = Query("task", pattern="^(task|chat)$"),
    subject: Optional[str] = Query(None, max_length=500),
):
    """Read durable usage aggregates, independent of bounded call history."""
    return {"status": "success", "data": get_llm_manager().usage_service.query(
        period=period, view=view, subject=subject,
    )}


@router.get("/usage/series")
def get_usage_series(days: int = Query(7, ge=1, le=30)):
    """Daily usage totals for trend charts."""
    return {"status": "success", "data": get_llm_manager().usage_service.series(days=days)}


@router.get("/stats")
async def get_stats():
    """获取调用统计"""
    try:
        llm_manager = get_llm_manager()
        stats = llm_manager.get_stats()
        return {"status": "success", "data": stats}
    except Exception as e:
        logger.error(f"获取统计数据失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats/{plugin_name}")
async def get_plugin_stats(plugin_name: str):
    """获取单个插件的统计"""
    try:
        llm_manager = get_llm_manager()
        all_stats = llm_manager.get_stats()
        
        # 筛选出该插件的统计
        plugin_stats = {
            key: value 
            for key, value in all_stats.items() 
            if key.startswith(f"{plugin_name}.")
        }
        
        if not plugin_stats:
            return {"status": "success", "data": {}, "message": f"插件 {plugin_name} 暂无统计数据"}
        
        return {"status": "success", "data": plugin_stats}
    except Exception as e:
        logger.error(f"获取插件统计失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 调用历史 ====================

CALL_HISTORY_INLINE_IMAGE_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
})


def _public_call_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Return history content without exposing server-local attachment paths."""
    public_entry = dict(entry or {})
    public_attachments = []
    for index, item in enumerate(public_entry.get("response_attachments") or []):
        if not isinstance(item, dict):
            continue
        try:
            size = max(0, int(item.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        public_attachments.append({
            "index": index,
            "type": str(item.get("type") or "file"),
            "mime_type": str(item.get("mime_type") or "application/octet-stream"),
            "name": str(item.get("name") or "附件"),
            "size": size,
            "sha256": str(item.get("sha256") or ""),
        })
    public_entry["response_attachments"] = public_attachments
    return public_entry


def _history_path_is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(callable(is_junction) and is_junction())
    except OSError:
        return True


def _resolve_call_history_attachment(
    entry: Dict[str, Any],
    attachment_index: int,
) -> tuple[Path, str, str, bool]:
    """Resolve one recorded attachment inside its trusted per-chat artifact root."""
    attachments = entry.get("response_attachments") or []
    if not isinstance(attachments, list) or not 0 <= attachment_index < len(attachments):
        raise HTTPException(status_code=404, detail="响应附件不存在")
    attachment = attachments[attachment_index]
    if not isinstance(attachment, dict):
        raise HTTPException(status_code=404, detail="响应附件不存在")

    chat_name = str(entry.get("chat_name") or "").strip()
    raw_path = str(attachment.get("path") or "").strip()
    if not chat_name or not raw_path:
        raise HTTPException(status_code=404, detail="响应附件不存在")

    try:
        access_context = codex_access_service.for_chat(chat_name, ensure=False)
        configured_root = Path(access_context.artifact_root)
        if _history_path_is_link_like(configured_root):
            raise ValueError("unsafe artifact root")
        allowed_root = configured_root.resolve(strict=True)
        resolved = Path(raw_path).expanduser().resolve(strict=True)
        resolved.relative_to(allowed_root)
        if not resolved.is_file():
            raise ValueError("attachment is not a file")
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "拒绝读取不在聊天隔离输出目录内的调用历史附件: chat=%s index=%s",
            chat_name,
            attachment_index,
        )
        raise HTTPException(status_code=404, detail="响应附件不存在") from exc

    raw_name = str(attachment.get("name") or resolved.name).strip().replace("\\", "/")
    filename = raw_name.rsplit("/", 1)[-1] or resolved.name
    guessed_mime = mimetypes.guess_type(resolved.name)[0]
    mime_type = guessed_mime or "application/octet-stream"
    inline_image = resolved.suffix.lower() in CALL_HISTORY_INLINE_IMAGE_SUFFIXES
    return resolved, filename, mime_type, inline_image


@router.get("/call-history/summary")
async def get_call_history_summary():
    """获取调用历史概览：所有已记录的 plugin.call_type 列表"""
    try:
        llm_manager = get_llm_manager()
        summaries = llm_manager.get_call_history_summary()
        return {"status": "success", "data": summaries}
    except Exception as e:
        logger.error(f"获取调用历史概览失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/call-history")
async def get_call_history(plugin_name: str = None, call_type: str = None, limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    """获取调用历史详情，可按插件和调用类型筛选"""
    try:
        llm_manager = get_llm_manager()
        if plugin_name and call_type:
            page = llm_manager.get_call_history_summaries(
                plugin_name=plugin_name,
                call_type=call_type,
                limit=limit,
                offset=offset,
            )
            return {"status": "success", "data": page}

        history = llm_manager.get_call_history(
            plugin_name=plugin_name,
            call_type=call_type
        )
        public_history = {
            key: [_public_call_history_entry(entry) for entry in entries]
            for key, entries in history.items()
        }
        return {"status": "success", "data": public_history}
    except Exception as e:
        logger.error(f"获取调用历史失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/call-history/entry")
async def get_call_history_entry(plugin_name: str, call_type: str, index: int = Query(..., ge=0)):
    """获取单条完整调用历史内容。"""
    try:
        llm_manager = get_llm_manager()
        entry = llm_manager.get_call_history_entry(plugin_name, call_type, index)
        if entry is None:
            raise HTTPException(status_code=404, detail="调用记录不存在")
        return {"status": "success", "data": _public_call_history_entry(entry)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取调用历史详情失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/call-history/attachment")
async def get_call_history_attachment(
    plugin_name: str,
    call_type: str,
    index: int = Query(..., ge=0),
    attachment_index: int = Query(..., ge=0),
    download: bool = Query(False),
):
    """Preview or download a recorded response attachment within its chat boundary."""
    llm_manager = get_llm_manager()
    entry = llm_manager.get_call_history_entry(plugin_name, call_type, index)
    if entry is None:
        raise HTTPException(status_code=404, detail="调用记录不存在")
    path, filename, mime_type, inline_image = _resolve_call_history_attachment(
        entry,
        attachment_index,
    )
    return FileResponse(
        path=path,
        filename=filename,
        media_type=mime_type,
        content_disposition_type=("inline" if inline_image and not download else "attachment"),
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


# ==================== 健康检查 ====================

@router.get("/health")
async def health_check():
    """健康检查"""
    try:
        llm_manager = get_llm_manager()
        config = llm_manager.get_config()
        
        return {
            "status": "healthy",
            "models_count": len(config.get("models", {})),
            "plugins_count": len(config.get("plugin_mappings", {})),
            "configured": True
        }
    except Exception as e:
        logger.error(f"健康检查失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
