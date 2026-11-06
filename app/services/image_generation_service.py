"""Host-owned Images/Responses calls, ordered failover and validated raster delivery."""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx
from PIL import Image

from app.services.image_generation_config import (
    ImageAnimationRequiresFrames, ImageConfigError, PARAMETERS, validate_parameters,
)
from app.services.image_generation_media import inspect_gif, inspect_svg

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_RESPONSE_BYTES = 160 * 1024 * 1024


class ImageGenerationError(RuntimeError):
    def __init__(self, message, *, paths=(), attempts=(), provider_details=None):
        super().__init__(message)
        self.paths = list(paths)
        self.attempts = list(attempts)
        self.provider_details = provider_details or {}


class ImageGenerationCancelled(ImageGenerationError):
    pass


class ImageGenerationDeadlineExceeded(ImageGenerationError):
    """A cumulative deadline, distinct from transport inactivity and size limits."""

    def __init__(self, message="生图任务超时，服务端结果未知", *, outcome_unknown=True, **kwargs):
        super().__init__(message, **kwargs)
        self.outcome_unknown = outcome_unknown


class ImageDeliveryError(ImageGenerationError):
    pass


class ImageChannelsExhausted(ImageGenerationError):
    """The allowed API queue finished; callers may fill missing images natively."""


def native_fallback_count(policy, request, error):
    """Only queue exhaustion permits fallback, never cancellation or delivery errors."""
    if not isinstance(error, ImageChannelsExhausted):
        return 0
    # A queue rejected entirely before HTTP is a request/configuration error,
    # not authorization to start a different, potentially billed generator.
    if error.attempts and all(a.get("error_type") in {
            ImageConfigError.__name__, ImageAnimationRequiresFrames.__name__,
    } for a in error.attempts):
        return 0
    channels = [c for c in policy["channels"] if c.get("enabled") is True]
    target = request.count if request.count is not None else int(
        channels[0].get("parameters", {}).get("n", 1) if channels else 1)
    return max(0, target - len(error.paths))


def inspect_image(data):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ImageGenerationError("图片为空或超过大小限制")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > 40_000_000:
                raise ValueError()
            kind, size = image.format.lower(), image.size
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
        return kind, size
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageGenerationError("返回内容不是有效的 PNG、JPEG 或 WebP 图片") from exc


def inspect_output(data, expected_format):
    try:
        if expected_format == "gif":
            size, animation = inspect_gif(data)
            return "gif", size, {"format": "gif", **animation}
        if expected_format == "svg":
            return "svg", inspect_svg(data), {"format": "svg"}
        kind, size = inspect_image(data)
        return kind, size, {"format": kind}
    except ValueError as exc:
        raise ImageGenerationError(str(exc)) from exc


@dataclass(frozen=True)
class ImageInput:
    data: bytes = field(repr=False)
    name: str = "reference.png"

    @classmethod
    def from_bytes(cls, data, name="reference.png"):
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ImageGenerationError("参考图片为空或过大")
        try:
            with Image.open(io.BytesIO(data)) as im:
                if im.width * im.height > 40_000_000:
                    raise ImageGenerationError("参考图片分辨率过大")
                if im.format not in {"PNG", "JPEG", "WEBP"}:
                    output = io.BytesIO()
                    im.convert("RGBA").save(output, format="PNG")
                    data, name = output.getvalue(), Path(name).stem + ".png"
                else:
                    name = Path(name).stem + "." + im.format.lower()
        except (OSError, Image.DecompressionBombError) as exc:
            raise ImageGenerationError("参考图片格式无效") from exc
        inspect_image(data)
        return cls(data, name)

    @property
    def mime(self):
        return "image/" + inspect_image(self.data)[0]

    def reference(self):
        return {"image_url": f"data:{self.mime};base64,{base64.b64encode(self.data).decode()}"}


@dataclass
class ImageRequest:
    prompt: str
    images: list[ImageInput] = field(default_factory=list)
    mask: ImageInput | None = None
    markup: ImageInput | None = None
    count: int | None = None
    parameters: dict = field(default_factory=dict)
    timeout: float | None = None
    cancelled: Callable[[], bool] | None = None
    actor: str = ""

    def check(self):
        if not isinstance(self.prompt, str) or not self.prompt.strip() or len(self.prompt) > 32000:
            raise ImageGenerationError("图片描述应为 1–32000 个字符")
        if self.count is not None and (type(self.count) is not int or not 1 <= self.count <= 10):
            raise ImageGenerationError("单次生成数量应为 1–10")
        if len(self.images) > 16:
            raise ImageGenerationError("最多支持 16 张参考图")
        for image in self.images:
            inspect_image(image.data)
        if self.mask and self.markup or (self.mask or self.markup) and not self.images:
            raise ImageGenerationError("蒙版和标注不能同时使用，且需要参考图")
        overlay = self.mask or self.markup
        if overlay:
            kind, size = inspect_image(overlay.data)
            if kind != "png" or size != inspect_image(self.images[0].data)[1]:
                raise ImageGenerationError("蒙版或标注需要与首张参考图等大的 PNG")
            with Image.open(io.BytesIO(overlay.data)) as im:
                if "A" not in im.getbands() or im.getchannel("A").getextrema()[0] == 255:
                    raise ImageGenerationError("蒙版或标注需要包含透明区域")
        validate_parameters(self.parameters)


@dataclass
class ImageResult:
    paths: list[Path]
    attempts: list[dict]


def _connection(channel):
    if channel.get("profile_id"):
        from app.services.codex_profile_service import get_codex_profile_service
        result = get_codex_profile_service().image_api_connection(channel["profile_id"])
        url, key = result["base_url"], result["api_key"]
    else:
        url, key = channel["base_url"], channel["api_key"]
    if channel["provider"] == "openai" and url.rstrip("/") != "https://api.openai.com/v1":
        raise ImageGenerationError("官方渠道的连接地址已变化")
    headers = {"Authorization": "Bearer " + key} if key else {}
    # Case-insensitive replacement permits a custom authentication scheme.
    headers = httpx.Headers(headers)
    headers.update(channel.get("headers") or {})
    return url.rstrip("/"), headers


def _parameters(channel, request, count):
    params = {**channel.get("parameters", {}), **request.parameters, "n": count}
    fmt = params.get("output_format", "png")
    if "output_format" in request.parameters and fmt != channel.get("parameters", {}).get("output_format", "png"):
        # A task changing format should not inherit incompatible defaults from
        # the previous format. Explicit task values still undergo validation.
        irrelevant = {"frame_count", "frame_duration_ms"} if fmt != "gif" else set()
        if fmt in {"gif", "svg"}:
            irrelevant |= {"stream", "partial_images", "output_compression"}
        for name in irrelevant - request.parameters.keys():
            params.pop(name, None)
        if (fmt == "gif" or channel.get("parameters", {}).get("output_format") == "gif") and "size" not in request.parameters:
            params["size"] = "auto"
    if not request.images:
        params.pop("input_fidelity", None)
    if not params.get("stream"):
        params.pop("partial_images", None)
    validate_parameters(params, official=channel["provider"] == "openai", model=channel["model"],
                        protocol=channel["protocol"], editing=bool(request.images))
    if fmt in {"gif", "svg"}:
        params.pop("stream", None)
    if fmt in {"gif", "svg"} and "prompt_rewriter" in (channel.get("extra_body") or {}):
        raise ImageConfigError("GIF / SVG 不支持 prompt_rewriter")
    if request.markup and channel["provider"] == "openai" and not channel["model"].startswith(("gpt-image-2.5-", "gpt-image-2.6-")):
        raise ImageConfigError("该模型不支持标注输入")
    return params


def _request_body(channel, request, count):
    params = _parameters(channel, request, count)
    extras = channel.get("extra_body") or {}
    if channel["protocol"] == "responses":
        params.pop("n", None)
        stream = params.pop("stream", False)
        content = [{"type": "input_text", "text": request.prompt}]
        content += [{"type": "input_image", **im.reference()} for im in request.images]
        tool = {"type": "image_generation", "model": channel["model"],
                "action": "edit" if request.images else "generate", **params}
        if request.mask:
            tool["input_image_mask"] = request.mask.reference()
        if request.markup:
            # Markup is an Images API capability, not a Responses tool parameter.
            raise ImageConfigError("标注输入需要 Images 接口")
        body = {**extras, "model": channel["response_model"], "input": [{"role": "user", "content": content}],
                "tools": [tool], "tool_choice": {"type": "image_generation"}, "stream": stream, "store": False}
        if request.actor and channel["provider"] == "openai":
            body["safety_identifier"] = hashlib.sha256(request.actor.encode()).hexdigest()
        return "/responses", {"json": body}
    body = {**extras, **params, "model": channel["model"], "prompt": request.prompt}
    if request.actor and channel["provider"] == "openai":
        body["safety_identifier"] = hashlib.sha256(request.actor.encode()).hexdigest()
    if not request.images:
        return "/images/generations", {"json": body}
    if channel.get("edit_transport") == "json":
        body["images"] = [im.reference() for im in request.images]
        if request.mask:
            body["mask"] = request.mask.reference()
        if request.markup:
            body["markup"] = request.markup.reference()
        return "/images/edits", {"json": body}
    files = [("image[]", (im.name, im.data, im.mime)) for im in request.images]
    if request.mask:
        files.append(("mask", (request.mask.name, request.mask.data, request.mask.mime)))
    if request.markup:
        files.append(("markup", (request.markup.name, request.markup.data, request.markup.mime)))
    data = {k: v if isinstance(v, str) else json.dumps(v, separators=(",", ":")) for k, v in body.items()}
    return "/images/edits", {"data": data, "files": files}


def _cancelled(request):
    if request.cancelled and request.cancelled():
        raise ImageGenerationCancelled("生图任务已取消")


def _outputs(payload):
    """Normalize complete outputs only; previews never become delivered files."""
    kind = payload.get("type", "")
    if kind in {"error", "response.failed", "response.incomplete"} or payload.get("error"):
        raise ImageGenerationError("接口返回生成失败")
    if kind in {"image_generation.completed", "image_edit.completed"}:
        yield payload
    elif kind == "response.output_item.done":
        item = payload.get("item") or {}
        if item.get("type") == "image_generation_call" and item.get("status") == "completed" and item.get("result"):
            yield {"b64_json": item["result"]}
    elif kind == "response.completed":
        yield from _outputs(payload.get("response") or {})
    elif not kind:
        if payload.get("status") in {"failed", "incomplete", "cancelled"}:
            raise ImageGenerationError("接口未完成图片生成")
        for item in payload.get("data") or []:
            if isinstance(item, dict):
                yield item
        for item in payload.get("output") or []:
            if item.get("type") == "image_generation_call" and item.get("status") == "completed" and item.get("result"):
                yield {"b64_json": item["result"]}


def _read_payloads(response, request, deadline):
    streaming = "text/event-stream" in response.headers.get("content-type", "")
    total = 0
    buffer = b""
    if not streaming:
        chunks = []
        for chunk in response.iter_bytes():
            _cancelled(request)
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ImageGenerationError("响应超过大小限制")
            if time.monotonic() > deadline:
                raise ImageGenerationDeadlineExceeded()
            chunks.append(chunk)
        yield json.loads(b"".join(chunks))
        return
    data_lines = []
    for chunk in response.iter_bytes():
        _cancelled(request)
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ImageGenerationError("响应超过大小限制")
        if time.monotonic() > deadline:
            raise ImageGenerationDeadlineExceeded()
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip())
            elif not line and data_lines:
                data = b"\n".join(data_lines)
                data_lines = []
                if data != b"[DONE]":
                    yield json.loads(data)
    if buffer.startswith(b"data:"):
        data_lines.append(buffer[5:].strip())
    if data_lines and b"\n".join(data_lines) != b"[DONE]":
        yield json.loads(b"\n".join(data_lines))


def _download_image(url, request, timeout):
    # Returned asset URLs are untrusted. Reuse the public, DNS-pinned downloader;
    # never forward the upstream API credentials to an asset host.
    from app.services.codex_browser_proxy import PinnedPublicProxy
    from app.services.codex_browser_tool import PublicUrlGuard
    guard = PublicUrlGuard()
    guard.validate_public_url(url)
    deadline = time.monotonic() + min(60, timeout)
    with PinnedPublicProxy(guard.resolve_public_endpoint) as proxy:
        with httpx.Client(proxy=proxy.playwright_settings["server"], trust_env=False,
                          timeout=min(60, timeout), follow_redirects=False) as client:
            for _ in range(5):
                with client.stream("GET", url) as response:
                    if response.is_redirect:
                        url = str(response.url.join(response.headers["location"]))
                        guard.validate_public_url(url)
                        continue
                    if response.status_code != 200:
                        raise ImageGenerationError("图片下载失败")
                    chunks, length = [], 0
                    for chunk in response.iter_bytes():
                        _cancelled(request)
                        if time.monotonic() > deadline:
                            raise ImageGenerationDeadlineExceeded("图片下载超时，服务端结果未知")
                        length += len(chunk)
                        if length > MAX_IMAGE_BYTES:
                            raise ImageGenerationError("图片下载超过大小限制")
                        chunks.append(chunk)
                    return b"".join(chunks)
    raise ImageGenerationError("图片下载重定向过多")


def _image_bytes(item, request, timeout):
    if item.get("b64_json"):
        encoded = item["b64_json"]
        if not isinstance(encoded, str) or len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 4:
            raise ImageGenerationError("图片数据超过大小限制")
        return base64.b64decode(encoded, validate=True)
    if item.get("url"):
        return _download_image(item["url"], request, timeout)
    raise ImageGenerationError("接口未返回图片数据")


def _provider_http_error(response, request, deadline):
    # Error messages can echo prompts, credentials or signed URLs. Persist only
    # recognized machine codes/parameter names and a shaped request identifier.
    details = {}
    request_id = response.headers.get("x-request-id", "")
    if re.fullmatch(r"req_[a-fA-F0-9]{16,64}", request_id):
        details["provider_request_id"] = request_id
    codes = {"moderation_blocked", "content_policy_violation", "invalid_request_error", "image_generation_user_error",
             "invalid_value", "unsupported_value", "invalid_image", "invalid_image_format", "invalid_image_url",
             "model_not_found", "invalid_api_key", "insufficient_quota", "rate_limit_exceeded",
             "billing_hard_limit_reached", "server_error", "permission_denied"}
    types = {"invalid_request_error", "image_generation_user_error", "authentication_error",
             "permission_error", "rate_limit_error", "server_error"}
    try:
        data = bytearray()
        for chunk in response.iter_bytes(chunk_size=8192):
            _cancelled(request)
            if len(data) + len(chunk) > 64 * 1024 or time.monotonic() > deadline:
                break
            data.extend(chunk)
        payload = json.loads(data)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            for field, allowed in (("code", codes), ("type", types),
                                   ("param", PARAMETERS | {"model", "prompt", "image", "images", "mask", "markup", "input"})):
                value = error.get(field)
                if isinstance(value, str) and value in allowed:
                    details["provider_error_" + field] = value
    except (ValueError, TypeError, httpx.HTTPError):
        pass
    message = f"接口 HTTP {response.status_code}"
    if details.get("provider_error_code") or details.get("provider_error_type"):
        message += "（" + (details.get("provider_error_code") or details["provider_error_type"]) + "）"
    return ImageGenerationError(message, provider_details=details)


def _call(channel, request, count, timeout, client_factory):
    deadline = time.monotonic() + timeout
    base, headers = _connection(channel)
    endpoint, kwargs = _request_body(channel, request, count)
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise ImageGenerationDeadlineExceeded("渠道超时", outcome_unknown=False)
    with client_factory(timeout=httpx.Timeout(timeout, connect=min(20, timeout)), follow_redirects=False) as client:
        with client.stream("POST", base + endpoint, headers=headers, **kwargs) as response:
            if response.status_code >= 300:
                raise _provider_http_error(response, request, deadline)
            for payload in _read_payloads(response, request, deadline):
                if not isinstance(payload, dict):
                    raise ImageGenerationError("接口响应格式无效")
                for item in _outputs(payload):
                    yield _image_bytes(item, request, max(1, deadline - time.monotonic()))


def _failure_details(exc):
    """Classify failures without persisting exception text, URLs or response bodies."""
    categories = (
        (httpx.ProxyError, "代理连接失败", False),
        (httpx.ConnectError, "无法连接生图接口，请检查网络、代理或 TLS 连接", False),
        ((httpx.ReadError, httpx.RemoteProtocolError), "接收接口响应时连接中断，服务端结果未知", True),
        (httpx.WriteError, "发送请求时连接中断，服务端结果未知", True),
        (httpx.DecodingError, "接口响应解压失败", True),
        (httpx.LocalProtocolError, "请求协议或请求头格式无效", False),
        (httpx.RequestError, "接口传输失败，服务端结果未知", True),
        (json.JSONDecodeError, "接口返回的内容不是有效 JSON", True),
        (UnicodeDecodeError, "接口响应文字编码无效", True),
        (binascii.Error, "接口返回的图片编码无效", True),
        (OSError, "本地图片文件处理失败", True),
    )
    details = {"error": "图片请求处理失败", "error_type": type(exc).__name__}
    for kinds, message, unknown in categories:
        if isinstance(exc, kinds):
            details["error"] = message
            if unknown:
                details["outcome_unknown"] = True
            break
    return details


def generate_images(policy, request: ImageRequest, output_dir: Path, *, on_image=None,
                    progress=None, client_factory=httpx.Client):
    """Each enabled channel is attempted once; only incomplete work falls through."""
    paths, attempts = [], []
    try:
        return _generate_images(policy, request, output_dir, paths=paths, attempts=attempts,
                                on_image=on_image, progress=progress, client_factory=client_factory)
    except ImageGenerationError as exc:
        # Cancellation can occur between channels or while receiving a stream.
        # Every exit must retain complete files already written by this request.
        exc.paths = list(paths)
        exc.attempts = list(attempts)
        raise
    except ImageConfigError as exc:
        raise ImageGenerationError(str(exc), paths=paths, attempts=attempts) from exc
    except Exception as exc:
        # In particular, callback/storage failures outside a channel must not
        # hide completed files or expose secret-bearing exception messages.
        raise ImageGenerationError(_failure_details(exc)["error"], paths=paths, attempts=attempts) from exc


def _generate_images(policy, request, output_dir, *, paths, attempts, on_image, progress, client_factory):
    request.check()
    if policy["mode"] != "api":
        raise ImageGenerationError("当前为 Codex 内置模式")
    channels = [c for c in policy["channels"] if c.get("enabled") is True]
    if not channels:
        _cancelled(request)
        raise ImageChannelsExhausted("没有已启用的生图 API 渠道")
    _cancelled(request)
    target = request.count if request.count is not None else int(channels[0].get("parameters", {}).get("n", 1))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    hashes = set()
    deadline = time.monotonic() + request.timeout if request.timeout else float("inf")
    for channel in channels:
        _cancelled(request)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ImageGenerationDeadlineExceeded(
                "生图任务总时限已到", outcome_unknown=any(a.get("outcome_unknown") for a in attempts))
        started = time.monotonic()
        attempt = {"channel_id": channel["id"], "channel": channel["name"], "model": channel["model"],
                   "protocol": channel["protocol"], "operation": "edit" if request.images else "generate",
                   "status": "running", "images": 0, "output_sizes": [], "outputs": []}
        attempts.append(attempt)
        try:
            # These validated standard parameters reflect task overrides, not just
            # the current saved defaults. Never record custom bodies or headers.
            attempt["parameters"] = _parameters(channel, request, target - len(paths))
            if progress:
                progress(dict(attempt))
            calls = target - len(paths) if channel["protocol"] == "responses" else 1
            channel_deadline = min(deadline, started + channel["timeout"])
            for _ in range(calls):
                _cancelled(request)
                timeout = channel_deadline - time.monotonic()
                if timeout <= 0:
                    raise ImageGenerationDeadlineExceeded("渠道超时", outcome_unknown=False)
                for data in _call(channel, request, target - len(paths), timeout, client_factory):
                    _cancelled(request)
                    kind, size, details = inspect_output(data, attempt["parameters"].get("output_format", "png"))
                    digest = hashlib.sha256(data).hexdigest()
                    if digest in hashes:
                        continue
                    path = output_dir / f"image-{uuid.uuid4().hex}.{kind}"
                    path.write_bytes(data)
                    paths.append(path)
                    hashes.add(digest)
                    attempt["images"] += 1
                    attempt["output_sizes"].append(f"{size[0]}x{size[1]}")
                    attempt["outputs"].append({**details, "width": size[0], "height": size[1]})
                    if on_image:
                        # Delivery errors must never trigger another billed generation.
                        try:
                            on_image(path)
                        except Exception as exc:
                            raise ImageDeliveryError("图片已保存，但交付失败", paths=paths, attempts=attempts) from exc
                    if len(paths) >= target:
                        break
                if len(paths) >= target:
                    break
            attempt["status"] = "completed" if len(paths) >= target else "partial"
            if len(paths) < target:
                attempt["error"] = "返回图片数量不足"
        except ImageGenerationCancelled:
            attempt["status"] = "cancelled"
            raise
        except ImageDeliveryError:
            attempt.update(status="failed", error="图片已保存，但交付失败")
            raise
        except ImageGenerationDeadlineExceeded as exc:
            attempt.update(status="failed", error=str(exc), error_type=type(exc).__name__)
            if exc.outcome_unknown:
                attempt["outcome_unknown"] = True
        except (httpx.TimeoutException, TimeoutError) as exc:
            attempt.update(status="failed", error="请求超时，服务端结果未知", outcome_unknown=True,
                           error_type=type(exc).__name__)
        except (ImageGenerationError, ImageConfigError) as exc:
            attempt.update(status="failed", error=str(exc), error_type=type(exc).__name__)
            if isinstance(exc, ImageGenerationError):
                attempt.update(exc.provider_details)
        except Exception as exc:
            # No raw request, response or secret-bearing exception is recorded.
            attempt.update(status="failed", **_failure_details(exc))
        finally:
            attempt["elapsed"] = round(time.monotonic() - started, 3)
            logger.info("Image API channel=%s model=%s status=%s images=%s elapsed=%s size=%s quality=%s error_type=%s error=%s provider_request_id=%s",
                        channel["id"], channel["model"], attempt["status"], attempt["images"], attempt["elapsed"],
                        attempt.get("parameters", {}).get("size", "auto"),
                        attempt.get("parameters", {}).get("quality", "auto"),
                        attempt.get("error_type", ""), attempt.get("error", ""), attempt.get("provider_request_id", ""))
            if progress:
                progress(dict(attempt))
        if len(paths) >= target:
            return ImageResult(paths, attempts)
    errors = "；".join(f"{a['channel']}：{a.get('error', '失败')}" for a in attempts)
    message = f"生图未完成（{len(paths)}/{target}）" + (f"：{errors}" if errors else "：任务超时")
    if time.monotonic() >= deadline:
        raise ImageGenerationDeadlineExceeded(message, paths=paths, attempts=attempts,
                                              outcome_unknown=any(a.get("outcome_unknown") for a in attempts))
    raise ImageChannelsExhausted(message, paths=paths, attempts=attempts)
