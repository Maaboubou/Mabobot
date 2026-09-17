#!/usr/bin/env python3
"""Inspect magnet links and render Mabobot-friendly resource reports."""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.event_bus import Event, EventType
from app.services.config_service import get_setting
from app.services.plugin_config_context import ScopedConfigAttribute
from app.utils.plugin_config import get_config


API_ENDPOINT = "https://whatslink.info/api/v1/link"
NUDENET_THRESHOLD = 0.12
NUDENET_INFERENCE_RESOLUTION = 960
NUDENET_LABELS = (
    "FEMALE_GENITALIA_COVERED",
    "FACE_FEMALE",
    "BUTTOCKS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_BREAST_EXPOSED",
    "ANUS_EXPOSED",
    "FEET_EXPOSED",
    "BELLY_COVERED",
    "FEET_COVERED",
    "ARMPITS_COVERED",
    "ARMPITS_EXPOSED",
    "FACE_MALE",
    "BELLY_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_COVERED",
    "FEMALE_BREAST_COVERED",
    "BUTTOCKS_COVERED",
)
NUDENET_BLUR_LABELS = frozenset(
    {
        "FEMALE_GENITALIA_COVERED",
        "BUTTOCKS_EXPOSED",
        "FEMALE_BREAST_EXPOSED",
        "FEMALE_GENITALIA_EXPOSED",
        "MALE_BREAST_EXPOSED",
        "ANUS_EXPOSED",
        "ARMPITS_EXPOSED",
        "BELLY_EXPOSED",
        "MALE_GENITALIA_EXPOSED",
        "ANUS_COVERED",
        "FEMALE_BREAST_COVERED",
        "BUTTOCKS_COVERED",
    }
)
MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
USER_AGENT = "WhatsLink-Mobile-HTML/1.0 (+local client)"
INFO_HASH_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[A-Z2-7a-z2-7]{32})$")
MAGNET_URI_RE = re.compile(r"magnet:\?[^\s\"'<>]+", re.IGNORECASE)
MAGNET_TRAILING_PUNCTUATION = ".,;!?，。；！？）》】}]"
TRIGGER_WORD = "验车"
TRIGGER_VALUE_RE = re.compile(
    rf"^{TRIGGER_WORD}\s*(?P<info_hash>[0-9a-fA-F]{{40}}|[A-Z2-7a-z2-7]{{32}})"
    r"(?=$|\s|[.,;!?，。；！？）》】}])"
)
LOCAL_DEPENDENCIES = Path(__file__).resolve().parent / ".deps"
PLUGIN_NAME = "magnet_check"
PLUGIN_DIR = Path(__file__).resolve().parent
BLUR_PIXEL_DIVISOR = 36
BLUR_RADIUS_RATIO = 0.035
BLUR_MINIMUM_RADIUS = 12

logger = logging.getLogger(__name__)


class QueryError(RuntimeError):
    """An HTTP or network error that still carries response details."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        data: Any = None,
        raw_text: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.data = data
        self.raw_text = raw_text


class NudityDetectionError(RuntimeError):
    """NudeNet could not initialize or returned an unusable response."""


class ScreenshotProcessingError(RuntimeError):
    """A screenshot could not be downloaded or converted into a blurred copy."""


@dataclass(frozen=True)
class NudityDecision:
    blur: bool
    reason: str
    score: float | None = None
    label: str | None = None
    failed_closed: bool = False


def validate_magnet(value: str) -> str:
    text = value.strip().strip('"').strip("'")
    if not text:
        raise ValueError("磁力链接不能为空。")

    candidates = MAGNET_URI_RE.findall(text)
    if not candidates:
        raise ValueError("请输入以 magnet:? 开头的完整磁力链接。")

    for candidate in candidates:
        candidate = candidate.rstrip(MAGNET_TRAILING_PUNCTUATION)
        parsed = urllib.parse.urlparse(candidate)
        for name, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            if name.lower() != "xt" or not item.lower().startswith("urn:btih:"):
                continue
            info_hash = item[len("urn:btih:") :]
            if not INFO_HASH_RE.fullmatch(info_hash):
                continue

            normalized_hash = info_hash.lower() if len(info_hash) == 40 else info_hash.upper()
            return f"magnet:?xt=urn:btih:{normalized_hash}"

    raise ValueError("磁力链接中未找到有效的 xt=urn:btih InfoHash。")


def magnet_from_trigger(value: str, bot_name: str = "", trigger_word: str = TRIGGER_WORD) -> str | None:
    """Build a normalized magnet from a leading ``验车 + InfoHash`` command."""

    if not isinstance(value, str):
        return None

    text = value.lstrip()
    mention = f"@{bot_name.strip()}" if bot_name and bot_name.strip() else ""
    if mention and text.startswith(mention):
        text = text[len(mention) :].lstrip()

    word = str(trigger_word or TRIGGER_WORD).strip()
    trigger_re = re.compile(
        rf"^{re.escape(word)}\s*(?P<info_hash>[0-9a-fA-F]{{40}}|[A-Z2-7a-z2-7]{{32}})"
        r"(?=$|\s|[.,;!?，。；！？）》】}])"
    )
    match = trigger_re.match(text)
    if match is None:
        return None

    info_hash = match.group("info_hash")
    normalized_hash = info_hash.lower() if len(info_hash) == 40 else info_hash.upper()
    return f"magnet:?xt=urn:btih:{normalized_hash}"


def parse_json_or_text(raw_text: str) -> Any:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return raw_text


def _urlopen(
    request: urllib.request.Request,
    *,
    timeout: float,
    use_system_proxy: bool,
) -> Any:
    if use_system_proxy:
        return urllib.request.urlopen(request, timeout=timeout)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


def query_whatslink(
    magnet: str,
    timeout: float = 30.0,
    *,
    use_system_proxy: bool = True,
) -> tuple[int, Any, str]:
    query = urllib.parse.urlencode({"url": magnet})
    request = urllib.request.Request(
        f"{API_ENDPOINT}?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )

    try:
        with _urlopen(
            request,
            timeout=timeout,
            use_system_proxy=use_system_proxy,
        ) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            raw_text = response.read().decode(charset, errors="replace")
            return response.status, parse_json_or_text(raw_text), raw_text
    except urllib.error.HTTPError as exc:
        charset = exc.headers.get_content_charset() or "utf-8"
        raw_text = exc.read().decode(charset, errors="replace")
        data = parse_json_or_text(raw_text)
        message = (
            data.get("error", f"HTTP {exc.code}")
            if isinstance(data, dict)
            else f"HTTP {exc.code}"
        )
        raise QueryError(
            str(message),
            status_code=exc.code,
            data=data,
            raw_text=raw_text,
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise QueryError(f"无法连接 whatslink.info：{reason}") from exc
    except TimeoutError as exc:
        raise QueryError(f"请求超时（{timeout:g} 秒）。") from exc


def create_nudenet_detector(
    inference_resolution: int = NUDENET_INFERENCE_RESOLUTION,
) -> Any:
    if LOCAL_DEPENDENCIES.is_dir():
        dependency_path = str(LOCAL_DEPENDENCIES)
        if dependency_path not in sys.path:
            sys.path.insert(0, dependency_path)

    try:
        from nudenet import NudeDetector
    except ImportError as exc:
        raise NudityDetectionError(
            "未安装 NudeNet，请在项目虚拟环境运行 "
            "python -m pip install -r app/plugins/magnet_check/requirements.txt。"
        ) from exc

    try:
        return NudeDetector(inference_resolution=inference_resolution)
    except Exception as exc:
        raise NudityDetectionError(f"NudeNet 初始化失败：{exc}") from exc


def nudity_decision_from_detections(
    detections: Any,
    threshold: float = NUDENET_THRESHOLD,
) -> NudityDecision:
    if not isinstance(detections, list):
        raise NudityDetectionError("NudeNet 未返回检测结果列表。")

    matches: list[tuple[float, str]] = []
    for detection in detections:
        if not isinstance(detection, dict):
            raise NudityDetectionError("NudeNet 检测项格式异常。")
        label = detection.get("class")
        score_value = detection.get("score")
        if not isinstance(label, str) or isinstance(score_value, bool):
            raise NudityDetectionError("NudeNet 检测项缺少 class 或 score。")
        try:
            score = float(score_value)
        except (TypeError, ValueError) as exc:
            raise NudityDetectionError("NudeNet 检测分数无效。") from exc
        if score != score or score in {float("inf"), float("-inf")}:
            raise NudityDetectionError("NudeNet 检测分数无效。")
        normalized_label = label.upper()
        if normalized_label in NUDENET_BLUR_LABELS and score >= threshold:
            matches.append((score, normalized_label))

    if not matches:
        return NudityDecision(False, "safe", 0.0)

    score, label = max(matches)
    return NudityDecision(True, "nudenet_match", score, label)


def detect_screenshot_urls(
    urls: list[str],
    timeout: float = 30.0,
    detector: Any | None = None,
    detector_error: str = "",
    *,
    threshold: float = NUDENET_THRESHOLD,
    use_system_proxy: bool = True,
) -> tuple[dict[str, NudityDecision], dict[str, bytes]]:
    unique_urls = list(dict.fromkeys(urls))
    if not unique_urls:
        return {}, {}

    if detector is None and not detector_error:
        try:
            detector = create_nudenet_detector()
        except NudityDetectionError as exc:
            detector_error = str(exc)

    decisions: dict[str, NudityDecision] = {}
    image_bytes_by_url: dict[str, bytes] = {}
    for url in unique_urls:
        try:
            image_bytes = download_screenshot(
                url,
                timeout=timeout,
                use_system_proxy=use_system_proxy,
            )
        except ScreenshotProcessingError as exc:
            decisions[url] = NudityDecision(
                True,
                str(exc),
                failed_closed=True,
            )
            continue

        image_bytes_by_url[url] = image_bytes
        if detector_error:
            decisions[url] = NudityDecision(
                True,
                detector_error,
                failed_closed=True,
            )
            continue

        try:
            detections = run_nudenet_detection(
                detector,
                image_bytes,
                candidate_threshold=threshold,
            )
            decisions[url] = nudity_decision_from_detections(
                detections,
                threshold=threshold,
            )
        except Exception as exc:
            decisions[url] = NudityDecision(
                True,
                f"NudeNet 检测失败：{exc}",
                failed_closed=True,
            )

    return decisions, image_bytes_by_url


def run_nudenet_detection(
    detector: Any,
    image_bytes: bytes,
    *,
    candidate_threshold: float = NUDENET_THRESHOLD,
    nms_iou_threshold: float = 0.45,
) -> list[dict[str, Any]]:
    """Run NudeNet with configurable pre-NMS sensitivity.

    NudeNet 3.4.2 hard-codes its internal candidate threshold to 0.20. Running
    the public ``detect`` method would therefore discard low-confidence
    candidates before this plugin can apply its own stricter policy. This
    implementation uses the detector's existing ONNX session while keeping
    preprocessing/postprocessing local and configurable.
    """

    import cv2
    import numpy as np

    threshold = min(1.0, max(0.01, float(candidate_threshold)))
    encoded = np.frombuffer(image_bytes, dtype=np.uint8)
    matrix = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if matrix is None or matrix.size == 0:
        raise NudityDetectionError("截图无法解码为 NudeNet 输入。")

    original_height, original_width = matrix.shape[:2]
    square_size = max(original_width, original_height)
    padded = cv2.copyMakeBorder(
        matrix,
        0,
        square_size - original_height,
        0,
        square_size - original_width,
        cv2.BORDER_CONSTANT,
    )
    input_width = int(getattr(detector, "input_width", NUDENET_INFERENCE_RESOLUTION))
    input_height = int(getattr(detector, "input_height", input_width))
    blob = cv2.dnn.blobFromImage(
        padded,
        1 / 255.0,
        (input_width, input_height),
        (0, 0, 0),
        swapRB=True,
        crop=False,
    )
    outputs = detector.onnx_session.run(
        None,
        {detector.input_name: blob},
    )
    if not outputs:
        raise NudityDetectionError("NudeNet 未返回推理张量。")

    raw_output = np.squeeze(outputs[0])
    if raw_output.ndim == 1:
        raw_output = raw_output[:, np.newaxis]
    rows = np.transpose(raw_output)
    boxes: list[list[float]] = []
    scores: list[float] = []
    class_ids: list[int] = []
    scale_x = square_size / input_width
    scale_y = square_size / input_height

    for row in rows:
        class_scores = row[4:]
        if class_scores.size == 0:
            continue
        class_id = int(np.argmax(class_scores))
        score = float(class_scores[class_id])
        if score < threshold:
            continue

        center_x, center_y, width, height = (float(value) for value in row[:4])
        left = (center_x - width / 2) * scale_x
        top = (center_y - height / 2) * scale_y
        box_width = width * scale_x
        box_height = height * scale_y
        left = max(0.0, min(left, float(original_width)))
        top = max(0.0, min(top, float(original_height)))
        box_width = max(0.0, min(box_width, original_width - left))
        box_height = max(0.0, min(box_height, original_height - top))
        if box_width <= 0 or box_height <= 0:
            continue

        class_ids.append(class_id)
        scores.append(score)
        boxes.append([left, top, box_width, box_height])

    if not boxes:
        return []

    detections: list[dict[str, Any]] = []
    # Apply NMS per class. Class-agnostic NMS can let a high-confidence benign
    # label suppress an overlapping sensitive candidate and reduce recall.
    for class_id in sorted(set(class_ids)):
        if class_id < 0 or class_id >= len(NUDENET_LABELS):
            continue
        source_indices = [
            index for index, value in enumerate(class_ids) if value == class_id
        ]
        class_boxes = [boxes[index] for index in source_indices]
        class_scores = [scores[index] for index in source_indices]
        kept = cv2.dnn.NMSBoxes(
            class_boxes,
            class_scores,
            score_threshold=threshold,
            nms_threshold=nms_iou_threshold,
        )
        for raw_index in np.asarray(kept).reshape(-1):
            index = source_indices[int(raw_index)]
            left, top, width, height = boxes[index]
            detections.append(
                {
                    "class": NUDENET_LABELS[class_id],
                    "score": scores[index],
                    "box": [int(left), int(top), int(width), int(height)],
                }
            )
    return detections


def download_screenshot(
    image_url: str,
    timeout: float = 30.0,
    *,
    use_system_proxy: bool = True,
) -> bytes:
    request = urllib.request.Request(
        image_url,
        headers={
            "Accept": "image/*",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        with _urlopen(
            request,
            timeout=timeout,
            use_system_proxy=use_system_proxy,
        ) as response:
            content_length = response.headers.get("Content-Length", "")
            try:
                declared_size = int(content_length)
            except (TypeError, ValueError):
                declared_size = 0
            if declared_size > MAX_SCREENSHOT_BYTES:
                raise ScreenshotProcessingError("截图超过 20 MB 限制。")
            image_bytes = response.read(MAX_SCREENSHOT_BYTES + 1)
    except ScreenshotProcessingError:
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise ScreenshotProcessingError(f"无法下载截图：{exc}") from exc

    if len(image_bytes) > MAX_SCREENSHOT_BYTES:
        raise ScreenshotProcessingError("截图超过 20 MB 限制。")
    if not image_bytes:
        raise ScreenshotProcessingError("截图内容为空。")
    return image_bytes


def blur_image_bytes(image_bytes: bytes, *, strength: float = 1.0) -> bytes:
    """Blur the complete screenshot at a relative strength from 0.1 to 1.0."""

    try:
        from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise ScreenshotProcessingError("未安装 Pillow，无法生成像素级模糊图。") from exc

    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            width, height = image.size
            if width < 1 or height < 1:
                raise ScreenshotProcessingError("截图尺寸无效。")

            normalized_strength = min(1.0, max(0.1, float(strength)))
            pixel_divisor = max(2, round(BLUR_PIXEL_DIVISOR * normalized_strength))
            reduced_size = (
                max(1, width // pixel_divisor),
                max(1, height // pixel_divisor),
            )
            reduced = image.resize(reduced_size, Image.Resampling.BILINEAR)
            pixelated = reduced.resize(image.size, Image.Resampling.NEAREST)
            radius = max(
                max(1, round(BLUR_MINIMUM_RADIUS * normalized_strength)),
                int(min(width, height) * BLUR_RADIUS_RATIO * normalized_strength),
            )
            blurred = pixelated.filter(ImageFilter.GaussianBlur(radius=radius))

            output = io.BytesIO()
            blurred.save(output, format="JPEG", quality=82, optimize=True)
            return output.getvalue()
    except ScreenshotProcessingError:
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        ValueError,
    ) as exc:
        raise ScreenshotProcessingError("截图不是可处理的图片格式。") from exc


def prepare_blurred_image_sources(
    decisions: dict[str, NudityDecision],
    image_bytes_by_url: dict[str, bytes] | None = None,
    timeout: float = 30.0,
    *,
    blur_strength: float = 1.0,
) -> tuple[dict[str, str], int]:
    cached_images = image_bytes_by_url or {}
    sources: dict[str, str] = {}
    failure_count = 0
    for url, decision in decisions.items():
        if not decision.blur:
            continue
        try:
            original = cached_images.get(url)
            if original is None:
                original = download_screenshot(url, timeout=timeout)
            blurred = blur_image_bytes(original, strength=blur_strength)
        except ScreenshotProcessingError:
            failure_count += 1
            continue
        encoded = base64.b64encode(blurred).decode("ascii")
        sources[url] = f"data:image/jpeg;base64,{encoded}"
    return sources, failure_count


def failed_closed_reasons(decisions: dict[str, NudityDecision]) -> list[str]:
    return list(
        dict.fromkeys(
            decision.reason
            for decision in decisions.values()
            if decision.failed_closed and decision.reason
        )
    )


def format_bytes(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return "--"

    units = ("B", "KB", "MB", "GB", "TB", "PB")
    size = float(value)
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    precision = 0 if index == 0 else 2
    return f"{size:.{precision}f} {units[index]}"


def format_count(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return "--"
    return str(value)


def is_http_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def screenshot_urls(data: Any) -> list[str]:
    if not isinstance(data, dict) or not isinstance(data.get("screenshots"), list):
        return []

    urls: list[str] = []
    for item in data["screenshots"]:
        if not isinstance(item, dict):
            continue
        url = item.get("screenshot")
        if is_http_url(url):
            urls.append(url)
    return urls


def render_screenshots(
    data: Any,
    nudity_decisions: dict[str, NudityDecision],
    blurred_image_sources: dict[str, str],
) -> str:
    urls = screenshot_urls(data)
    if not urls:
        return """
        <section class="screenshots" aria-labelledby="screenshots-title">
          <h2 id="screenshots-title">截图</h2>
          <div class="empty-state">暂无截图</div>
        </section>
        """

    items = []
    for index, url in enumerate(urls, start=1):
        escaped_url = html.escape(url, quote=True)
        decision = nudity_decisions.get(
            url,
            NudityDecision(True, "missing_decision", failed_closed=True),
        )
        if decision.blur:
            blurred_source = blurred_image_sources.get(url)
            if blurred_source:
                escaped_source = html.escape(blurred_source, quote=True)
                items.append(
                    f"""
                    <div class="screenshot blurred" role="img"
                         aria-label="截图 {index}，内容已模糊">
                      <img src="{escaped_source}" alt="">
                      <span class="blur-label">内容已模糊</span>
                    </div>
                    """
                )
            else:
                items.append(
                    f"""
                    <div class="screenshot hidden-content" role="img"
                         aria-label="截图 {index}，内容已隐藏">
                      <span class="blur-label">内容已隐藏</span>
                    </div>
                    """
                )
        else:
            items.append(
                f"""
                <a class="screenshot" href="{escaped_url}" target="_blank"
                   rel="noopener noreferrer" aria-label="查看截图 {index} 原图">
                  <img src="{escaped_url}" alt="截图 {index}" loading="lazy"
                       referrerpolicy="no-referrer">
                </a>
                """
            )

    return f"""
    <section class="screenshots" aria-labelledby="screenshots-title">
      <h2 id="screenshots-title">截图</h2>
      <div class="gallery">{''.join(items)}</div>
    </section>
    """


def build_html(
    *,
    data: Any,
    nudity_decisions: dict[str, NudityDecision] | None = None,
    blurred_image_sources: dict[str, str] | None = None,
    error_message: str = "",
) -> str:
    response = data if isinstance(data, dict) else {}
    decisions = nudity_decisions or {}
    blurred_sources = blurred_image_sources or {}
    api_error = response.get("error")
    if not error_message and api_error:
        error_message = str(api_error)

    resource_name = response.get("name")
    has_content = bool(resource_name) and not error_message

    if error_message:
        page_title = "查询失败"
        notice_title = "查询失败"
        notice_text = error_message
        notice_class = "error"
    elif not has_content:
        page_title = "未查到内容"
        notice_title = "未查到内容"
        notice_text = "whatslink.info 可能尚未收录该磁力链接。"
        notice_class = "warning"
    else:
        page_title = str(resource_name)
        notice_title = ""
        notice_text = ""
        notice_class = ""

    if has_content:
        main_content = f"""
        <header class="resource-header">
          <h1>{html.escape(page_title)}</h1>
        </header>

        <section class="stats" aria-label="资源概览">
          <article class="stat">
            <div class="stat-label">总大小</div>
            <div class="stat-value">{html.escape(format_bytes(response.get('size')))}</div>
          </article>
          <article class="stat">
            <div class="stat-label">文件数量</div>
            <div class="stat-value">{html.escape(format_count(response.get('count')))}</div>
          </article>
        </section>

        {render_screenshots(response, decisions, blurred_sources)}
        """
    else:
        main_content = f"""
        <section class="notice {notice_class}" role="alert">
          <span class="notice-mark" aria-hidden="true"></span>
          <div>
            <h1>{html.escape(notice_title)}</h1>
            <p>{html.escape(notice_text)}</p>
          </div>
        </section>
        """

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>{html.escape(page_title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #172029;
      --muted: #66727d;
      --line: #d9dfe2;
      --paper: #ffffff;
      --canvas: #f1f4f3;
      --accent: #087f5b;
      --danger: #b42318;
      --warning: #9a6700;
      --shadow: 0 8px 24px rgba(23, 32, 41, .07);
    }}
    * {{ box-sizing: border-box; }}
    html {{ background: var(--canvas); }}
    body {{
      min-width: 280px;
      margin: 0;
      background: var(--canvas);
      color: var(--ink);
      font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      line-height: 1.5;
      letter-spacing: 0;
    }}
    .accent-bar {{
      height: 6px;
      background: var(--accent);
    }}
    .page {{
      width: min(calc(100% - 24px), 820px);
      margin: 0 auto;
      padding: 22px 0 calc(48px + env(safe-area-inset-bottom));
    }}
    .resource-header {{
      padding: 4px 2px 20px;
      border-bottom: 1px solid #cbd3d6;
    }}
    h1 {{
      margin: 0;
      font-size: 28px;
      line-height: 1.24;
      overflow-wrap: anywhere;
    }}
    h2 {{
      margin: 0 0 14px;
      font-size: 19px;
      line-height: 1.3;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 16px;
    }}
    .stat {{
      min-width: 0;
      min-height: 96px;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--paper);
      box-shadow: var(--shadow);
    }}
    .stat-label {{
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }}
    .stat-value {{
      margin-top: 10px;
      font-size: 22px;
      font-weight: 800;
      line-height: 1.15;
      overflow-wrap: anywhere;
    }}
    .screenshots {{
      margin-top: 28px;
      padding-top: 22px;
      border-top: 1px solid #cbd3d6;
    }}
    .gallery {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
    }}
    .screenshot {{
      position: relative;
      display: block;
      width: 100%;
      aspect-ratio: 16 / 9;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #1f292f;
      box-shadow: var(--shadow);
    }}
    .screenshot img {{
      display: block;
      width: 100%;
      height: 100%;
      object-fit: contain;
    }}
    .screenshot.blurred {{
      user-select: none;
    }}
    .blur-label {{
      position: absolute;
      left: 8px;
      bottom: 8px;
      padding: 5px 8px;
      border-radius: 4px;
      background: rgba(16, 24, 30, .72);
      color: #ffffff;
      font-size: 12px;
      font-weight: 800;
      text-align: center;
      text-shadow: 0 1px 3px rgba(0, 0, 0, .55);
    }}
    .hidden-content {{
      background: #202a30;
      box-shadow: none;
    }}
    .hidden-content .blur-label {{
      inset: 0;
      display: grid;
      place-items: center;
      padding: 16px;
      border-radius: 0;
      background: transparent;
    }}
    .empty-state {{
      display: grid;
      min-height: 112px;
      place-items: center;
      border: 1px dashed #bfc9cd;
      border-radius: 6px;
      color: var(--muted);
      background: rgba(255, 255, 255, .52);
      font-size: 14px;
    }}
    .notice {{
      display: flex;
      align-items: flex-start;
      gap: 13px;
      margin-top: 20px;
      padding: 18px;
      border: 1px solid;
      border-radius: 6px;
      background: var(--paper);
    }}
    .notice-mark {{
      flex: 0 0 10px;
      width: 10px;
      height: 10px;
      margin-top: 10px;
      border-radius: 50%;
    }}
    .notice h1 {{ font-size: 22px; }}
    .notice p {{ margin: 7px 0 0; color: var(--muted); overflow-wrap: anywhere; }}
    .notice.error {{ border-color: #e5a39d; }}
    .notice.error .notice-mark {{ background: var(--danger); }}
    .notice.warning {{ border-color: #dfc27f; }}
    .notice.warning .notice-mark {{ background: var(--warning); }}
    @media (min-width: 720px) {{
      .page {{ padding-top: 34px; }}
      h1 {{ font-size: 34px; }}
      .gallery {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 340px) {{
      .page {{ width: min(calc(100% - 16px), 820px); }}
      h1 {{ font-size: 25px; }}
      .stat {{ padding: 13px; }}
      .stat-value {{ font-size: 19px; }}
    }}
  </style>
</head>
<body>
  <div class="accent-bar"></div>
  <main class="page">
    {main_content}
  </main>
</body>
</html>
"""


def _load_report_font(size: int, *, bold: bool = False) -> Any:
    """Load a Chinese-capable font without depending on a browser runtime."""

    from PIL import ImageFont

    windows_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    names = ("msyhbd.ttc", "simhei.ttf", "msyh.ttc") if bold else (
        "msyh.ttc",
        "simhei.ttf",
        "simsun.ttc",
    )
    candidates = [windows_dir / "Fonts" / name for name in names]
    candidates.extend(
        [
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
            if bold
            else Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ]
    )
    for candidate in candidates:
        try:
            if candidate.exists():
                return ImageFont.truetype(str(candidate), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _line_height(font: Any, padding: int = 8) -> int:
    box = font.getbbox("国Ag")
    return max(1, box[3] - box[1]) + padding


def _wrap_report_text(
    draw: Any,
    value: Any,
    font: Any,
    max_width: int,
    *,
    max_lines: int,
) -> list[str]:
    text = " ".join(str(value or "").split()) or "--"
    remaining = text
    lines: list[str] = []
    while remaining and len(lines) < max_lines:
        line = ""
        consumed = 0
        for index, char in enumerate(remaining):
            candidate = line + char
            if line and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
                break
            line = candidate
            consumed = index + 1
        if not line:
            line = remaining[0]
            consumed = 1
        lines.append(line.rstrip())
        remaining = remaining[consumed:].lstrip()

    if remaining and lines:
        ellipsis = "…"
        last = lines[-1]
        while last and draw.textbbox((0, 0), last + ellipsis, font=font)[2] > max_width:
            last = last[:-1]
        lines[-1] = (last.rstrip() + ellipsis) or ellipsis
    return lines


def _draw_rounded_card(
    image: Any,
    box: tuple[int, int, int, int],
    *,
    fill: str,
    outline: str = "#d9dfe2",
    radius: int = 12,
    width: int = 2,
) -> None:
    from PIL import ImageDraw

    ImageDraw.Draw(image).rounded_rectangle(
        box,
        radius=radius,
        fill=fill,
        outline=outline,
        width=width,
    )


def _decode_report_image(image_bytes: bytes) -> Any:
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(image_bytes)) as source:
        source.seek(0)
        return ImageOps.exif_transpose(source).convert("RGB")


def _paste_screenshot_card(
    report: Any,
    box: tuple[int, int, int, int],
    *,
    image_bytes: bytes | None,
    decision: NudityDecision,
    index: int,
    blur_strength: float = 1.0,
) -> None:
    from PIL import Image, ImageDraw, ImageOps

    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    card = Image.new("RGB", (width, height), "#202a30")
    label = ""

    if image_bytes:
        display_bytes = (
            blur_image_bytes(image_bytes, strength=blur_strength)
            if decision.blur
            else image_bytes
        )
        source = _decode_report_image(display_bytes)
        contained = ImageOps.contain(source, (width, height), Image.Resampling.LANCZOS)
        offset = ((width - contained.width) // 2, (height - contained.height) // 2)
        card.paste(contained, offset)
        if decision.blur:
            label = "内容已模糊"
    else:
        label = "内容已隐藏"

    radius = 12
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    report.paste(card, (left, top), mask)
    draw = ImageDraw.Draw(report)
    draw.rounded_rectangle(box, radius=radius, outline="#d9dfe2", width=2)

    badge_font = _load_report_font(18, bold=True)
    if label:
        label_box = draw.textbbox((0, 0), label, font=badge_font)
        label_width = label_box[2] - label_box[0]
        label_height = label_box[3] - label_box[1]
        if label == "内容已隐藏":
            label_x = left + (width - label_width) // 2
            label_y = top + (height - label_height) // 2
            draw.text((label_x, label_y), label, font=badge_font, fill="#ffffff")
        else:
            badge = (
                left + 12,
                bottom - label_height - 28,
                left + label_width + 34,
                bottom - 10,
            )
            draw.rounded_rectangle(badge, radius=7, fill="#172029")
            draw.text(
                (badge[0] + 11, badge[1] + 7),
                label,
                font=badge_font,
                fill="#ffffff",
            )

    number_font = _load_report_font(16, bold=True)
    number = str(index)
    number_box = draw.textbbox((0, 0), number, font=number_font)
    number_width = number_box[2] - number_box[0]
    circle = (right - 43, top + 11, right - 11, top + 43)
    draw.ellipse(circle, fill="#087f5b")
    draw.text(
        (circle[0] + (32 - number_width) // 2, circle[1] + 5),
        number,
        font=number_font,
        fill="#ffffff",
    )


def render_report_png(
    *,
    data: Any,
    output: Path,
    nudity_decisions: dict[str, NudityDecision] | None = None,
    image_bytes_by_url: dict[str, bytes] | None = None,
    error_message: str = "",
    max_screenshots: int = 6,
    blur_strength: float = 1.0,
) -> Path:
    """Render the result directly with Pillow for low-overhead, browser-free output."""

    from PIL import Image, ImageDraw

    response = data if isinstance(data, dict) else {}
    decisions = nudity_decisions or {}
    cached_images = image_bytes_by_url or {}
    api_error = response.get("error")
    if not error_message and api_error:
        error_message = str(api_error)

    resource_name = response.get("name")
    has_content = bool(resource_name) and not error_message
    urls = screenshot_urls(response)[: max(0, max_screenshots)] if has_content else []

    canvas_width = 900
    margin = 30
    content_width = canvas_width - margin * 2
    accent_height = 7
    title_font = _load_report_font(40, bold=True)
    heading_font = _load_report_font(25, bold=True)
    label_font = _load_report_font(18, bold=True)
    value_font = _load_report_font(30, bold=True)
    body_font = _load_report_font(20)
    muted_font = _load_report_font(17)

    sizing_image = Image.new("RGB", (canvas_width, 100), "white")
    sizing_draw = ImageDraw.Draw(sizing_image)
    if has_content:
        title_lines = _wrap_report_text(
            sizing_draw,
            resource_name,
            title_font,
            content_width,
            max_lines=3,
        )
        title_line_height = _line_height(title_font, 10)
        title_height = len(title_lines) * title_line_height
        stats_height = 116
        gallery_top_space = 78
        if urls:
            columns = 2 if len(urls) > 1 else 1
            gap = 14
            card_width = (content_width - gap * (columns - 1)) // columns
            card_height = round(card_width * 9 / 16)
            rows = (len(urls) + columns - 1) // columns
            gallery_height = rows * card_height + max(0, rows - 1) * gap
        else:
            columns = 1
            gap = 14
            card_width = content_width
            card_height = 120
            gallery_height = card_height
        extra_note_height = 36 if len(screenshot_urls(response)) > len(urls) else 0
        canvas_height = (
            accent_height
            + 34
            + title_height
            + 32
            + stats_height
            + gallery_top_space
            + gallery_height
            + extra_note_height
            + 38
        )
    else:
        title_lines = []
        canvas_height = 390

    report = Image.new("RGB", (canvas_width, canvas_height), "#f1f4f3")
    draw = ImageDraw.Draw(report)
    draw.rectangle((0, 0, canvas_width, accent_height), fill="#087f5b")

    if has_content:
        y = accent_height + 28
        line_height = _line_height(title_font, 10)
        for line in title_lines:
            draw.text((margin, y), line, font=title_font, fill="#172029")
            y += line_height
        y += 14
        draw.line((margin, y, canvas_width - margin, y), fill="#cbd3d6", width=2)
        y += 22

        card_gap = 14
        stat_width = (content_width - card_gap) // 2
        stats = (
            ("总大小", format_bytes(response.get("size"))),
            ("文件数量", format_count(response.get("count"))),
        )
        for index, (label, value) in enumerate(stats):
            left = margin + index * (stat_width + card_gap)
            stat_box = (left, y, left + stat_width, y + 116)
            _draw_rounded_card(report, stat_box, fill="#ffffff")
            draw.text((left + 20, y + 17), label, font=label_font, fill="#66727d")
            value_lines = _wrap_report_text(
                draw,
                value,
                value_font,
                stat_width - 40,
                max_lines=1,
            )
            draw.text((left + 20, y + 58), value_lines[0], font=value_font, fill="#172029")

        y += 116 + 36
        draw.line((margin, y, canvas_width - margin, y), fill="#cbd3d6", width=2)
        y += 24
        draw.text((margin, y), "资源截图", font=heading_font, fill="#172029")
        y += 48

        if urls:
            for index, url in enumerate(urls, start=1):
                row = (index - 1) // columns
                column = (index - 1) % columns
                left = margin + column * (card_width + gap)
                top = y + row * (card_height + gap)
                decision = decisions.get(
                    url,
                    NudityDecision(True, "missing_decision", failed_closed=True),
                )
                _paste_screenshot_card(
                    report,
                    (left, top, left + card_width, top + card_height),
                    image_bytes=cached_images.get(url),
                    decision=decision,
                    index=index,
                    blur_strength=blur_strength,
                )
            y += gallery_height
        else:
            empty_box = (margin, y, canvas_width - margin, y + 120)
            _draw_rounded_card(
                report,
                empty_box,
                fill="#f8faf9",
                outline="#bfc9cd",
            )
            empty = "暂无截图"
            empty_box_text = draw.textbbox((0, 0), empty, font=body_font)
            empty_width = empty_box_text[2] - empty_box_text[0]
            draw.text(
                (margin + (content_width - empty_width) // 2, y + 45),
                empty,
                font=body_font,
                fill="#66727d",
            )
            y += 120

        hidden_count = len(screenshot_urls(response)) - len(urls)
        if hidden_count > 0:
            draw.text(
                (margin, y + 14),
                f"为控制图片大小，另有 {hidden_count} 张截图未展示",
                font=muted_font,
                fill="#66727d",
            )
    else:
        notice_title = "查询失败" if error_message else "未查到内容"
        notice_text = error_message or "whatslink.info 可能尚未收录该磁力链接。"
        title_color = "#b42318" if error_message else "#9a6700"
        y = accent_height + 54
        draw.text((margin, y), notice_title, font=title_font, fill=title_color)
        y += 74
        notice_box = (margin, y, canvas_width - margin, canvas_height - 42)
        _draw_rounded_card(
            report,
            notice_box,
            fill="#ffffff",
            outline="#e5a39d" if error_message else "#dfc27f",
        )
        body_lines = _wrap_report_text(
            draw,
            notice_text,
            body_font,
            content_width - 54,
            max_lines=5,
        )
        body_y = y + 30
        for line in body_lines:
            draw.text((margin + 27, body_y), line, font=body_font, fill="#66727d")
            body_y += _line_height(body_font, 9)

    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report.save(output, format="PNG", optimize=True)
    return output


def write_html(output: Path, content: str) -> Path:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8", newline="\n")
    return output


class MagnetCheckPlugin:
    """Mabobot plugin adapter for automatic magnet-link inspection."""

    @ScopedConfigAttribute
    def trigger_word(self):
        return str(get_config('trigger_word', TRIGGER_WORD, plugin_name=PLUGIN_NAME) or TRIGGER_WORD).strip()

    @ScopedConfigAttribute
    def timeout(self):
        return max(3.0, float(get_config('timeout', plugin_name=PLUGIN_NAME, default=30.0) or 30.0))

    @ScopedConfigAttribute
    def max_screenshots(self):
        return max(1, min(12, int(get_config('max_screenshots', plugin_name=PLUGIN_NAME, default=6) or 6)))

    @ScopedConfigAttribute
    def use_system_proxy(self):
        return bool(get_config('use_system_proxy', plugin_name=PLUGIN_NAME, default=False))

    @ScopedConfigAttribute
    def enable_full_magnet_match(self):
        return bool(get_config('enable_full_magnet_match', plugin_name=PLUGIN_NAME, default=False))

    @ScopedConfigAttribute
    def detection_threshold(self):
        return min(0.5, max(0.05, float(get_config('detection_threshold', plugin_name=PLUGIN_NAME, default=NUDENET_THRESHOLD) or NUDENET_THRESHOLD)))

    @ScopedConfigAttribute
    def blur_strength(self):
        return min(1.0, max(0.1, float(get_config('blur_strength', plugin_name=PLUGIN_NAME, default=1.0) or 1.0)))

    def __init__(self) -> None:
        self.trigger_word = str(
            get_config("trigger_word", TRIGGER_WORD, plugin_name=PLUGIN_NAME) or TRIGGER_WORD
        ).strip()
        self.timeout = max(
            3.0,
            float(get_config("timeout", plugin_name=PLUGIN_NAME, default=30.0) or 30.0),
        )
        self.max_screenshots = max(
            1,
            min(
                12,
                int(
                    get_config(
                        "max_screenshots",
                        plugin_name=PLUGIN_NAME,
                        default=6,
                    )
                    or 6
                ),
            ),
        )
        self.use_system_proxy = bool(
            get_config(
                "use_system_proxy",
                plugin_name=PLUGIN_NAME,
                default=False,
            )
        )
        self.enable_full_magnet_match = bool(
            get_config(
                "enable_full_magnet_match",
                plugin_name=PLUGIN_NAME,
                default=False,
            )
        )
        self.bot_name = str(get_setting("WECHAT_BOT_NAME", "刘局") or "").strip()
        self.detection_threshold = min(
            0.5,
            max(
                0.05,
                float(
                    get_config(
                        "detection_threshold",
                        plugin_name=PLUGIN_NAME,
                        default=NUDENET_THRESHOLD,
                    )
                    or NUDENET_THRESHOLD
                ),
            ),
        )
        self.blur_strength = min(
            1.0,
            max(
                0.1,
                float(
                    get_config(
                        "blur_strength",
                        plugin_name=PLUGIN_NAME,
                        default=1.0,
                    )
                    or 1.0
                ),
            ),
        )
        configured_resolution = int(
            get_config(
                "inference_resolution",
                plugin_name=PLUGIN_NAME,
                default=NUDENET_INFERENCE_RESOLUTION,
            )
            or NUDENET_INFERENCE_RESOLUTION
        )
        self.inference_resolution = max(
            320,
            min(1280, round(configured_resolution / 32) * 32),
        )
        configured_output = str(
            get_config(
                "output_dir",
                plugin_name=PLUGIN_NAME,
                default="reports",
            )
            or "reports"
        ).strip()
        output_path = Path(configured_output)
        self.output_dir = output_path if output_path.is_absolute() else PLUGIN_DIR / output_path
        self._detector: Any | None = None
        self._detector_error = ""
        self._detector_init_lock = threading.Lock()
        self._detection_lock = threading.Lock()
        logger.info(
            "magnet_check 参数: detection_threshold=%.2f, inference_resolution=%d, blur_strength=%.0f%%",
            self.detection_threshold,
            self.inference_resolution,
            self.blur_strength * 100,
        )

    def extract_magnet(self, message: Any) -> str | None:
        """Return a normalized magnet from a full URI or a leading check command."""

        if not isinstance(message, str) or not message.strip():
            return None
        triggered_magnet = magnet_from_trigger(message, self.bot_name, self.trigger_word)
        if triggered_magnet is not None:
            return triggered_magnet
        if not self.enable_full_magnet_match:
            return None
        try:
            return validate_magnet(message)
        except ValueError:
            return None

    @ScopedConfigAttribute
    def inference_resolution(self):
        resolution = int(get_config("inference_resolution", NUDENET_INFERENCE_RESOLUTION, plugin_name="magnet_check") or NUDENET_INFERENCE_RESOLUTION)
        return max(320, min(1280, round(resolution / 32) * 32))

    def _get_detector(self) -> Any:
        resolution = self.inference_resolution
        with self._detector_init_lock:
            if self._detector is None or getattr(self, "_detector_resolution", None) != resolution:
                self._detector = create_nudenet_detector(inference_resolution=resolution)
                self._detector_resolution = resolution
            return self._detector

    def generate_report(self, magnet: str) -> Path:
        data: Any = {}
        decisions: dict[str, NudityDecision] = {}
        image_bytes_by_url: dict[str, bytes] = {}

        try:
            _, data, _ = query_whatslink(
                magnet,
                timeout=self.timeout,
                use_system_proxy=self.use_system_proxy,
            )
        except QueryError as exc:
            raise RuntimeError(f"whatslink 查询失败：{exc}") from exc

        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"whatslink 查询失败：{data['error']}")

        urls = screenshot_urls(data)[: self.max_screenshots]
        if urls:
            try:
                detector = self._get_detector()
            except NudityDetectionError as exc:
                raise RuntimeError(f"NudeNet 不可用：{exc}") from exc

            # NudeDetector/ONNX sessions are reused to avoid repeated model loads.
            # Serialize inference because plugin events may run in parallel per chat.
            with self._detection_lock:
                decisions, image_bytes_by_url = detect_screenshot_urls(
                    urls,
                    timeout=self.timeout,
                    detector=detector,
                    threshold=self.detection_threshold,
                    use_system_proxy=self.use_system_proxy,
                )

            failure_reasons = failed_closed_reasons(decisions)
            if failure_reasons:
                raise RuntimeError("资源截图检测失败：" + "；".join(failure_reasons))

        info_hash = magnet.rsplit(":", 1)[-1][:12]
        filename = (
            f"magnet_check_{info_hash}_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
        )
        return render_report_png(
            data=data,
            output=self.output_dir / filename,
            nudity_decisions=decisions,
            image_bytes_by_url=image_bytes_by_url,
            max_screenshots=self.max_screenshots,
            blur_strength=self.blur_strength,
        )

    def handle_text(self, event: Event) -> bool:
        message = event.data.get("message", "")
        magnet = self.extract_magnet(message)
        if magnet is None:
            return False

        chat_name = str(event.data.get("chat_name", "") or "").strip()
        wx = event.context.get("wx")
        logger.info("检测到磁力链接，开始查询资源信息: chat=%s", chat_name)

        try:
            image_path = self.generate_report(magnet)
            if wx is None or not chat_name:
                logger.warning("磁链报告已生成但微信实例或聊天名不可用: %s", image_path)
                return True
            sent = wx.send_files(chat_name, [str(image_path)])
            if sent is False:
                raise RuntimeError("微信文件发送接口返回失败")
            logger.info("磁链检查报告发送成功: chat=%s path=%s", chat_name, image_path)
        except Exception as exc:
            logger.error("magnet_check 处理失败: %s", exc, exc_info=True)
        return True


plugin: MagnetCheckPlugin | None = None


def handle_text(event: Event) -> bool:
    if plugin is None:
        return False
    return plugin.handle_text(event)


def register(event_bus: Any, subscribe: Any, context: Any) -> None:
    """Register the plugin with Mabobot's event bus."""

    del event_bus  # Reserved for future shared services.
    global plugin
    plugin = MagnetCheckPlugin()
    migration_notes = context.storage.migrate_legacy_directory(
        plugin.output_dir, storage_class="generated", relative="reports"
    )
    plugin.output_dir = context.storage.generated_root / "reports"
    plugin.output_dir.mkdir(parents=True, exist_ok=True)
    if migration_notes:
        context.audit.record(
            "storage_migration",
            summary="磁链检查报告已迁移到插件标准存储目录",
            details={"moved_files": len(migration_notes)},
        )
    context.health.register(lambda: {
        "status": "degraded" if plugin is not None and plugin._detector_error else "healthy" if plugin is not None else "unhealthy",
        "message": plugin._detector_error if plugin is not None and plugin._detector_error else "磁链检查服务已就绪" if plugin is not None else "磁链检查服务未初始化",
        "detector_loaded": bool(plugin and plugin._detector is not None),
    })
    context.register_cleanup(unregister)
    subscribe(
        event_type=EventType.TEXT_MESSAGE_RECEIVED,
        handler=handle_text,
    )
    subscribe(
        event_type=EventType.LINK_MESSAGE_RECEIVED,
        handler=handle_text,
    )
    logger.info(
        "magnet_check 插件注册成功（消息开头“%s + InfoHash”；完整磁链匹配=%s；NudeNet 阈值=%.2f，分辨率=%d）",
        plugin.trigger_word,
        "开启" if plugin.enable_full_magnet_match else "关闭",
        plugin.detection_threshold,
        plugin.inference_resolution,
    )


def unregister() -> None:
    global plugin
    plugin = None
    logger.info("magnet_check 插件已卸载")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="输入磁力链接，查询后生成手机版 HTML 资源摘要。"
    )
    parser.add_argument("magnet", nargs="?", help="可选；不传时运行后交互输入")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("whatslink_result.html"),
        help="输出 HTML 路径（默认：whatslink_result.html）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="请求超时秒数（默认：30）",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="生成后不自动打开浏览器",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    entered = args.magnet
    if entered is None:
        try:
            entered = input("请输入磁力链接，然后按 Enter 查询：\n> ")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。", file=sys.stderr)
            return 130

    try:
        magnet = validate_magnet(entered)
    except ValueError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2

    data: Any = {}
    nudity_decisions: dict[str, NudityDecision] = {}
    blurred_image_sources: dict[str, str] = {}
    error_message = ""
    exit_code = 0

    print("正在请求 whatslink.info ...")
    try:
        _, data, _ = query_whatslink(magnet, timeout=args.timeout)
    except QueryError as exc:
        data = exc.data if exc.data is not None else {}
        error_message = str(exc)
        exit_code = 1

    urls = screenshot_urls(data) if not error_message else []
    if urls:
        print(f"正在使用 NudeNet 检测 {len(urls)} 张截图 ...")
        nudity_decisions, image_bytes_by_url = detect_screenshot_urls(
            urls,
            timeout=args.timeout,
        )
        blurred_count = sum(decision.blur for decision in nudity_decisions.values())
        failed_count = sum(
            decision.failed_closed for decision in nudity_decisions.values()
        )
        print(f"截图检测完成：模糊 {blurred_count}/{len(nudity_decisions)} 张。")
        if failed_count:
            print(
                f"检测警告：{failed_count} 张无法确定，已按合规策略默认模糊。",
                file=sys.stderr,
            )
            for reason in failed_closed_reasons(nudity_decisions):
                print(f"检测失败原因：{reason}", file=sys.stderr)
        if blurred_count:
            print("正在生成重度整图模糊图 ...")
            blurred_image_sources, processing_failures = prepare_blurred_image_sources(
                nudity_decisions,
                image_bytes_by_url,
                timeout=args.timeout,
            )
            if processing_failures:
                print(
                    f"图片处理警告：{processing_failures} 张无法生成模糊副本，已直接隐藏。",
                    file=sys.stderr,
                )

    report = build_html(
        data=data,
        nudity_decisions=nudity_decisions,
        blurred_image_sources=blurred_image_sources,
        error_message=error_message,
    )
    output = write_html(args.output, report)

    print(f"HTML 已生成：{output}")
    if not args.no_open:
        opened = webbrowser.open(output.as_uri())
        if not opened:
            print("未能自动打开浏览器，请手动打开上述 HTML 文件。")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
