"""Read-only model discovery for saved channels and unsaved connection forms."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import date, datetime, timezone

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from typing import Literal

from app.services.image_generation_config import (
    DEFAULT_MODEL, MODELS, RESPONSE_MODELS, ImageChannel, ImageConfigError, read_settings,
)
from app.services.image_generation_service import _connection

_cache = {}
_lock = threading.Lock()
_RETIRED = {"gpt-image-1", "dall-e-2", "dall-e-3"}


class DiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel_id: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9_-]*$")
    provider: Literal["openai", "compatible"] = "openai"
    profile_id: str = Field(default="", max_length=48, pattern=r"^[A-Za-z0-9_-]*$")
    base_url: str = Field(default="https://api.openai.com/v1", max_length=1000)
    api_key: SecretStr | None = None
    headers: dict[str, str] | None = Field(default=None, max_length=32, repr=False)
    refresh: bool = False


def discovery_connection(request, db):
    old = next((c for c in read_settings(db)["channels"] if c["id"] == request.channel_id), {})
    same_target = bool(old) and old["profile_id"] == request.profile_id and (
        bool(request.profile_id) or old["base_url"].rstrip("/") == request.base_url.rstrip("/"))
    explicit_key = request.api_key.get_secret_value() if request.api_key is not None else None
    if not request.profile_id and not same_target and old and explicit_key is None and old.get("api_key"):
        raise ImageConfigError("连接地址已更改，请重新填写密钥或先保存配置")
    if not same_target and old.get("headers") and request.headers is None and not request.profile_id:
        raise ImageConfigError("连接地址已更改，请重新填写请求头或先保存配置")
    key = explicit_key if explicit_key is not None else old.get("api_key", "") if same_target else ""
    headers = request.headers if request.headers is not None else old.get("headers", {}) if same_target else {}
    try:
        channel = ImageChannel(name="模型目录", provider=request.provider, profile_id=request.profile_id,
                               base_url=request.base_url, api_key=key, headers=headers,
                               model=DEFAULT_MODEL).model_dump()
    except ValueError:
        raise ImageConfigError("请检查连接地址、密钥和请求头") from None
    try:
        base, headers = _connection(channel)
    except Exception:
        raise ImageConfigError("无法读取所选 Profile 的 API 连接") from None
    if request.provider == "openai" and not headers.get("authorization"):
        raise ImageConfigError("请先填写 API Key 或选择 API Profile")
    return base, headers


def _parse_catalog(payload, official):
    records = payload.get("data", payload.get("models")) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ImageConfigError("模型接口未返回有效列表")
    today = datetime.now(timezone.utc).date()
    all_models, images, responses = set(), set(), set()
    for record in records[:10000]:
        if isinstance(record, str):
            record = {"id": record}
        if not isinstance(record, dict):
            continue
        model = record.get("id", record.get("name"))
        if not isinstance(model, str) or not re.fullmatch(r"[^\s\x00-\x1f\x7f]{1,128}", model):
            continue
        if official:
            if model in _RETIRED:
                continue
            try:
                if record.get("shutdown_date") and date.fromisoformat(record["shutdown_date"]) <= today:
                    continue
            except (ValueError, TypeError):
                pass
        all_models.add(model)
        name = model.rsplit("/", 1)[-1] if not official else model
        metadata = record.get("architecture") or {}
        metadata = metadata if isinstance(metadata, dict) else {}
        outputs = metadata.get("output_modalities") or record.get("output_modalities") or []
        if (name.startswith("gpt-image-") or name == "chatgpt-image-latest"
                or not official and (record.get("mode") in {"image_generation", "image_edit"}
                                     or isinstance(outputs, list) and "image" in outputs)):
            images.add(model)
        if not official or any(model == base or re.fullmatch(re.escape(base) + r"-\d{4}-\d{2}-\d{2}", model)
                               for base in RESPONSE_MODELS):
            responses.add(model)
    def ordered(values, preferred):
        return sorted(values, key=lambda value: (preferred.index(value) if value in preferred else len(preferred), value))
    return {"models": ordered(images, MODELS), "response_models": ordered(responses - images, RESPONSE_MODELS),
            "other_models": sorted(all_models - images) if not official else [], "source": "live"}


def discover_models(request: DiscoveryRequest, db, *, client_factory=httpx.Client):
    base, headers = discovery_connection(request, db)
    fingerprint = hashlib.sha256((request.provider + "\n" + base + "\n" +
        json.dumps(dict(headers), sort_keys=True)).encode()).hexdigest()
    now = time.monotonic()
    with _lock:
        cached = _cache.get(fingerprint)
        if cached and now - cached[0] < 300 and not request.refresh:
            return {**cached[1], "source": "cache"}
    try:
        with client_factory(timeout=httpx.Timeout(15, connect=10), follow_redirects=False) as client:
            with client.stream("GET", base + "/models", headers=headers) as response:
                if response.status_code != 200:
                    raise ImageConfigError(f"获取模型失败（HTTP {response.status_code}）")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 8 * 1024 * 1024 or time.monotonic() - now > 20:
                        raise ImageConfigError("模型列表响应过大或超时")
                    chunks.append(chunk)
                result = _parse_catalog(json.loads(b"".join(chunks)), request.provider == "openai")
    except ImageConfigError:
        raise
    except httpx.TimeoutException:
        raise ImageConfigError("获取模型超时") from None
    except Exception:
        raise ImageConfigError("无法获取模型列表，请检查连接配置") from None
    with _lock:
        if len(_cache) >= 64:
            _cache.pop(next(iter(_cache)))
        _cache[fingerprint] = (time.monotonic(), result)
    return result
