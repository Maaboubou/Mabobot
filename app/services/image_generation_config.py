"""Versioned image channels and per-chat policies; credentials stay on the host."""
from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KEY = "IMAGE_GENERATION_CONFIG_SECRET"
CHAT_KEY = "IMAGE_GENERATION_CHAT_"
_lock = threading.RLock()
DEFAULT_MODEL = "gpt-image-2.6-flare"
MODELS = [
    "gpt-image-2.6-flare", "gpt-image-2.6-sunburst",
    "gpt-image-2.5-flare", "gpt-image-2.5-sunburst",
    "gpt-image-2.5-flare-2026-09-08", "gpt-image-2.5-sunburst-2026-09-08",
    "gpt-image-2", "gpt-image-2-2026-04-21", "gpt-image-1.5",
    "gpt-image-1-mini", "chatgpt-image-latest",
]
# Documented Responses models supporting image_generation. /models itself does
# not provide tool capability metadata. Live discovery intersects this list.
RESPONSE_MODELS = [
    "gpt-5.4-mini", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-astra", "gpt-6-luna", "gpt-6-sol",
    "chat-latest", "gpt-5.5", "gpt-5.5-pro", "gpt-5.4", "gpt-5.4-pro", "gpt-5.2", "gpt-5.2-pro",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-cyber",
]
PARAMETERS = {"size", "quality", "background", "output_format", "output_compression",
              "moderation", "input_fidelity", "stream", "partial_images", "n",
              "complexity", "frame_count", "frame_duration_ms"}
RESERVED = PARAMETERS | {"model", "prompt", "image", "images", "mask", "markup", "input",
                         "tools", "tool_choice", "previous_response_id", "store"}


class ImageConfigError(ValueError):
    pass


class ImageConfigConflict(ImageConfigError):
    pass


class ImageAnimationRequiresFrames(ImageConfigError):
    """Direct GIF output is unsupported; a static-frame workflow remains valid."""


class ImagePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["inherit", "native", "api"] = "inherit"
    # null inherits the queue; [] deliberately selects no channels.
    channel_ids: list[str] | None = Field(default=None, max_length=50)
    allow_request_override: bool | None = Field(default=None, strict=True)

    @field_validator("channel_ids")
    @classmethod
    def unique_ids(cls, values):
        if values is not None and (len(set(values)) != len(values) or any(
                not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", v) for v in values)):
            raise ValueError("渠道列表包含重复或无效 ID")
        return values


def validate_policy(value):
    try:
        return ImagePolicy.model_validate(value).model_dump()
    except ValueError as exc:
        raise ImageConfigError("图片生成策略格式不正确") from exc


def validate_parameters(params, *, official=False, model="", protocol="images", editing=False):
    if not isinstance(params, dict) or set(params) - PARAMETERS:
        raise ImageConfigError("生成参数包含未知字段，请使用高级参数")
    enums = {"background": {"auto", "opaque", "transparent"},
             "quality": {"auto", "low", "medium", "high", "xhigh", "max"},
             "output_format": {"png", "jpeg", "webp", "gif", "svg"},
             "complexity": {"low", "medium", "high"},
             "moderation": {"auto", "low"}, "input_fidelity": {"low", "high"}}
    for name, choices in enums.items():
        if name in params and params[name] not in choices:
            raise ImageConfigError(f"{name} 参数无效")
    for name, low, high in (("n", 1, 10), ("output_compression", 0, 100), ("partial_images", 0, 3),
                           ("frame_count", 2, 64), ("frame_duration_ms", 20, 2000)):
        if name in params and (type(params[name]) is not int or not low <= params[name] <= high):
            raise ImageConfigError(f"{name} 必须为 {low}–{high} 的整数")
    if "stream" in params and type(params["stream"]) is not bool:
        raise ImageConfigError("stream 必须为布尔值")
    if params.get("partial_images", 0) and not params.get("stream"):
        raise ImageConfigError("部分图片需要开启流式生成")
    size = params.get("size", "auto")
    if not isinstance(size, str) or size != "auto" and not re.fullmatch(r"\d{2,5}x\d{2,5}", size):
        raise ImageConfigError("尺寸应为 auto 或 宽x高")
    if params.get("background") == "transparent" and params.get("output_format", "png") == "jpeg":
        raise ImageConfigError("透明背景需要 PNG 或 WebP")
    fmt = params.get("output_format", "png")
    if fmt != "gif" and {"frame_count", "frame_duration_ms"} & params.keys():
        raise ImageConfigError("帧数和帧时长仅适用于 GIF")
    if fmt in {"gif", "svg"}:
        if protocol != "images" or editing:
            if fmt == "gif":
                raise ImageAnimationRequiresFrames("本次请求不能直接输出 GIF；可保留参考图生成静态帧，再合成动图")
            raise ImageConfigError("GIF / SVG 仅支持 Images 文生图，不能用于参考图编辑或 Responses")
        if params.get("stream") or params.get("partial_images") or "output_compression" in params:
            raise ImageConfigError("GIF / SVG 不支持流式、预览帧或压缩质量")
        if fmt == "svg" and params.get("n", 1) != 1:
            raise ImageConfigError("SVG 每次只能生成 1 张")
    if not official:
        return
    image26 = model.startswith("gpt-image-2.6-")
    if fmt == "gif" and not image26:
        raise ImageAnimationRequiresFrames("该模型不能直接输出 GIF；可用当前渠道生成静态帧，再合成动图")
    if (fmt in {"gif", "svg"} or "complexity" in params) and not image26:
        raise ImageConfigError("GIF、SVG 和复杂度需要 GPT Image 2.6 模型")
    if "complexity" in params and protocol != "images":
        raise ImageConfigError("复杂度设置需要 Images 接口")
    if "output_compression" in params and params.get("output_format", "png") not in {"jpeg", "webp"}:
        raise ImageConfigError("压缩质量仅适用于 JPEG 或 WebP")
    if model in {"gpt-image-1", "dall-e-2", "dall-e-3"}:
        raise ImageConfigError("该官方模型已停用，请选择在用的 GPT Image 模型")
    modern = model.startswith(("gpt-image-2", "chatgpt-image-latest"))
    if params.get("quality") in {"xhigh", "max"} and not model.startswith(("gpt-image-2.5-", "gpt-image-2.6-")):
        raise ImageConfigError("该模型不支持 xhigh / max 质量")
    if "input_fidelity" in params and (modern or model == "gpt-image-1-mini" and params["input_fidelity"] != "low"):
        raise ImageConfigError("该模型不支持所选 input_fidelity")
    if size != "auto":
        w, h = map(int, size.split("x"))
        if fmt == "gif":
            # GIF frames can be smaller than still images (e.g. 512x512).
            # The API enforces its aggregate frame/count pixel budget.
            if w <= 0 or h <= 0 or w % 16 or h % 16:
                raise ImageConfigError("GIF 每帧宽高必须为正数且是 16 的倍数")
        elif modern:
            if w % 16 or h % 16 or not 1/3 <= w/h <= 3 or max(w, h) > 3840 or not 655360 <= w*h <= 8294400:
                raise ImageConfigError("尺寸不符合该模型的分辨率限制")
        elif size not in {"1024x1024", "1024x1536", "1536x1024"}:
            raise ImageConfigError("该模型不支持自定义尺寸")


class ImageChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = False
    provider: Literal["openai", "compatible"] = "openai"
    protocol: Literal["images", "responses"] = "images"
    profile_id: str = Field(default="", max_length=48, pattern=r"^[a-zA-Z0-9_-]*$")
    base_url: str = Field(default="https://api.openai.com/v1", max_length=1000)
    api_key: str = Field(default="", max_length=4096, repr=False)
    model: str = Field(default=DEFAULT_MODEL, min_length=1, max_length=128)
    response_model: str = Field(default="", max_length=128)
    timeout: int = Field(default=300, ge=10, le=3600)
    parameters: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    extra_body: dict[str, Any] = Field(default_factory=dict, repr=False)
    # Official edits support JSON references; compatible services commonly use multipart.
    edit_transport: Literal["json", "multipart"] = "multipart"

    @model_validator(mode="after")
    def check(self):
        self.name = self.name.strip()
        self.model = self.model.strip()
        if not self.name or not self.model:
            raise ValueError("名称和模型不能为空")
        url = urlsplit(self.base_url)
        if not self.profile_id and (url.scheme not in {"http", "https"} or not url.hostname or url.username
                                   or url.password or url.query or url.fragment or any(c.isspace() for c in self.base_url)):
            raise ValueError("API 地址格式不正确")
        if self.provider == "openai" and not self.profile_id and self.base_url.rstrip("/") != "https://api.openai.com/v1":
            raise ValueError("非官方地址请选择兼容接口")
        if any(c in self.api_key for c in "\r\n\x00"):
            raise ValueError("API Key 格式不正确")
        if len(self.headers) > 32 or any(not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", k)
                                       or any(c in v for c in "\r\n\x00") for k, v in self.headers.items()):
            raise ValueError("请求头格式不正确")
        if {k.lower() for k in self.headers} & {"host", "content-length", "content-type", "connection", "transfer-encoding"}:
            raise ValueError("不能覆盖传输层请求头")
        if set(self.extra_body) & RESERVED:
            raise ValueError("高级参数与基础参数或任务字段冲突")
        if len(json.dumps(self.extra_body, ensure_ascii=False)) > 16000:
            raise ValueError("高级参数过长")
        if self.protocol == "responses" and not self.response_model.strip():
            raise ValueError("Responses 接口需要主模型")
        if self.provider == "openai" and (self.headers or self.extra_body):
            raise ValueError("自定义请求头和额外参数请使用兼容接口")
        validate_parameters(self.parameters, official=self.provider == "openai", model=self.model, protocol=self.protocol)
        if self.parameters.get("output_format") in {"gif", "svg"} and "prompt_rewriter" in self.extra_body:
            raise ValueError("GIF / SVG 不支持 prompt_rewriter")
        return self


def read_settings(db=None):
    if db is None:
        from app.models.base import SessionLocal
        with SessionLocal() as session:
            return read_settings(session)
    from app.models.setting import Setting
    row = db.query(Setting).filter_by(key=KEY).first()
    if not row:
        return {"version": 0, "mode": "native", "channels": [], "allow_request_override": False}
    try:
        data = json.loads(row.value)
        if data["mode"] not in {"native", "api"} or not isinstance(data["channels"], list):
            raise ValueError()
        data.setdefault("allow_request_override", False)
        if type(data["allow_request_override"]) is not bool:
            raise ValueError()
        return data
    except (ValueError, TypeError, KeyError) as exc:
        raise ImageConfigError("生图配置损坏，请检查配置") from exc


def public_settings(data):
    public = copy.deepcopy(data)
    for channel in public["channels"]:
        channel["key_configured"] = bool(channel.pop("api_key", ""))
        # Any custom header or body value may itself be a credential.
        channel["headers_configured"] = bool(channel.pop("headers", {}))
        channel["extra_body_configured"] = bool(channel.pop("extra_body", {}))
    public["models"] = MODELS
    public["response_models"] = RESPONSE_MODELS
    return public


def channel_summary(data):
    return [{k: c[k] for k in ("id", "name", "enabled", "model")} for c in data["channels"]]


def update_settings(db, payload):
    from app.models.setting import Setting
    from sqlalchemy.exc import IntegrityError
    with _lock:
        current = read_settings(db)
        if payload.get("version") != current["version"]:
            raise ImageConfigConflict("配置已变化，请刷新后重试")
        if payload.get("mode") not in {"native", "api"}:
            raise ImageConfigError("生图模式无效")
        allow_override = payload.get("allow_request_override", current.get("allow_request_override", False))
        if type(allow_override) is not bool:
            raise ImageConfigError("允许本次切换必须为布尔值")
        raw_channels = payload.get("channels")
        if not isinstance(raw_channels, list) or len(raw_channels) > 50:
            raise ImageConfigError("最多配置 50 个渠道")
        previous = {c["id"]: c for c in current["channels"]}
        channels = []
        for raw in raw_channels:
            if not isinstance(raw, dict):
                raise ImageConfigError("渠道格式不正确")
            raw = copy.deepcopy(raw)
            old = previous.get(raw.get("id"), {})
            for key in ("api_key", "headers", "extra_body"):
                # Omission preserves; an explicit empty value clears.
                raw.setdefault(key, old.get(key, "" if key == "api_key" else {}))
            for key in ("key_configured", "headers_configured", "extra_body_configured"):
                raw.pop(key, None)
            try:
                channel = ImageChannel.model_validate(raw).model_dump()
            except ValueError as exc:
                # Do not serialize Pydantic's input_value, which can contain keys.
                message = str(exc.errors()[0].get("msg", "渠道参数无效")) if hasattr(exc, "errors") else str(exc)
                raise ImageConfigError(message.removeprefix("Value error, ")) from None
            if channel["profile_id"]:
                from app.services.codex_profile_service import get_codex_profile_service
                profile = get_codex_profile_service().get_profile(channel["profile_id"])
                if not profile or profile.get("auth_type") != "api_key":
                    raise ImageConfigError("只能引用 API 类型的 Codex Profile")
                if channel["provider"] == "openai" and str(profile.get("base_url", "")).rstrip("/") != "https://api.openai.com/v1":
                    raise ImageConfigError("该 Profile 使用第三方地址，请选择兼容接口")
                channel["api_key"] = ""
            elif channel["enabled"] and channel["provider"] == "openai" and not channel["api_key"]:
                raise ImageConfigError("启用渠道前请配置 API Key")
            channels.append(channel)
        if len({c["id"] for c in channels}) != len(channels):
            raise ImageConfigError("渠道 ID 重复")
        data = {"version": current["version"] + 1, "mode": payload["mode"], "channels": channels,
                "allow_request_override": allow_override}
        row = db.query(Setting).filter_by(key=KEY).first()
        serialized = json.dumps(data, ensure_ascii=False)
        if row:
            changed = db.query(Setting).filter_by(id=row.id, value=row.value).update({"value": serialized}, synchronize_session=False)
            if not changed:
                db.rollback()
                raise ImageConfigConflict("配置已变化，请刷新后重试")
        else:
            db.add(Setting(key=KEY, value=serialized, category="image_generation"))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ImageConfigConflict("配置已变化，请刷新后重试") from None
        return public_settings(data)


def chat_policy(db, chat_id):
    from app.models.setting import Setting
    row = db.query(Setting).filter_by(key=CHAT_KEY + str(chat_id)).first()
    return validate_policy(json.loads(row.value)) if row else ImagePolicy().model_dump()


def set_chat_policy(db, chat_id, policy):
    from app.models.setting import Setting
    value = validate_policy(policy)
    row = db.query(Setting).filter_by(key=CHAT_KEY + str(chat_id)).first()
    if not row:
        row = Setting(key=CHAT_KEY + str(chat_id), category="image_generation")
        db.add(row)
    row.value = json.dumps(value)


def resolve_policy(*, chat_id=None, chat_name="", plugin_name="", db=None):
    """Capture configuration once per request; plugin settings already include chat overrides."""
    if db is None:
        from app.models.base import SessionLocal
        with SessionLocal() as session:
            return resolve_policy(chat_id=chat_id, chat_name=chat_name, plugin_name=plugin_name, db=session)
    data = read_settings(db)
    policy = ImagePolicy().model_dump()
    parent = ImagePolicy().model_dump()
    if plugin_name:
        from app.utils.plugin_config import get_config, get_plugin_config
        plugin = get_plugin_config(plugin_name)
        parent = validate_policy(plugin.get("image_generation", (plugin.get("config") or {}).get(
            "image_generation", ((plugin.get("config_schema") or {}).get("image_generation") or {}).get("default", parent))))
        policy = validate_policy(get_config("image_generation", policy, plugin_name=plugin_name))
    else:
        if chat_id is None and chat_name:
            from app.models.user_permission import WeChatUser
            user = db.query(WeChatUser.id).filter_by(chat_name=chat_name).first()
            chat_id = user.id if user else None
        if chat_id is not None:
            policy = chat_policy(db, chat_id)
    inherited_mode = data["mode"] if parent["mode"] == "inherit" else parent["mode"]
    mode = inherited_mode if policy["mode"] == "inherit" else policy["mode"]
    allow_override = data.get("allow_request_override", False)
    for scope in (parent, policy):
        if scope["allow_request_override"] is not None:
            allow_override = scope["allow_request_override"]
    # Plugin pipelines choose their generation branch before starting Codex.
    # Request-level switching applies to conversational Bot tasks only.
    if plugin_name:
        allow_override = False
    enabled = {c["id"]: c for c in data["channels"] if c["enabled"]}
    inherited_ids = parent["channel_ids"] if parent["channel_ids"] is not None else list(enabled)
    ids = policy["channel_ids"] if policy["channel_ids"] is not None else inherited_ids
    return {"mode": mode, "channels": [copy.deepcopy(enabled[i]) for i in ids if i in enabled],
            "version": data["version"], "allow_request_override": allow_override}


def runtime_image_mode(policy):
    """Tool availability is fixed when a Codex thread starts, unlike task selection."""
    return policy["mode"] + ("_switchable" if policy.get("allow_request_override") else "")
