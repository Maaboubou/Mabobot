"""Fail-closed identity helpers for delayed media UI operations.

WeChat 4.x renders messages in a virtualized recycler.  A UIA RuntimeId is an
identifier for a currently materialized row, not a durable message identifier.
This module therefore keeps two identities separate:

* ``delivery_id`` identifies the event delivered to Mabobot.
* ``MediaTargetIdentity`` describes the visible image that may later be clicked.

The target description combines surrounding message order with a visual
fingerprint of the thumbnail.  A delayed action is allowed only after a fresh
enumeration produces one unambiguous visual match.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


CONTEXT_BEFORE_COUNT = 8
CONTEXT_AFTER_COUNT = 4
VISUAL_REBIND_MAX_DISTANCE = 12
VISUAL_REBIND_COLOR_MAX_DISTANCE = 24.0
VISUAL_REBIND_DETAIL_MAX_DISTANCE = 28.0
VISUAL_STABLE_MAX_DISTANCE = 5
VISUAL_STABLE_COLOR_MAX_DISTANCE = 12.0
# File verification is a secondary guard after the exact row has already been
# rebound, stabilised and clicked. WeChat may aggressively crop, resize and
# recompress the displayed thumbnail, so this stage deliberately uses a much
# wider multi-signal envelope than the live-row checks above.
FILE_MATCH_PROFILE = "relaxed-v2"
FILE_VISUAL_MAX_DISTANCE = 34
FILE_COLOR_MAX_DISTANCE = 56.0
FILE_DETAIL_MAX_DISTANCE = 62.0
FILE_LOW_COLOR_VISUAL_MAX_DISTANCE = 40
FILE_LOW_COLOR_MAX_DISTANCE = 18.0
FILE_LOW_COLOR_DETAIL_MAX_DISTANCE = 34.0
FILE_PANORAMA_ASPECT_MIN = 1.8
FILE_PANORAMA_VISUAL_MAX_DISTANCE = 36
FILE_PANORAMA_COLOR_MAX_DISTANCE = 62.0
FILE_PANORAMA_DETAIL_MAX_DISTANCE = 70.0
NARROW_THUMBNAIL_ASPECT_MAX = 0.42
FILE_NARROW_VISUAL_MAX_DISTANCE = 38
FILE_NARROW_COLOR_MAX_DISTANCE = 35.0
FILE_NARROW_DETAIL_MAX_DISTANCE = 52.0
FILE_NO_DETAIL_VISUAL_MAX_DISTANCE = 28
FILE_NO_DETAIL_COLOR_MAX_DISTANCE = 44.0
FILE_MATCH_THRESHOLDS = {
    "standard": {
        "phash": FILE_VISUAL_MAX_DISTANCE,
        "color": FILE_COLOR_MAX_DISTANCE,
        "detail": FILE_DETAIL_MAX_DISTANCE,
    },
    "low_color": {
        "phash": FILE_LOW_COLOR_VISUAL_MAX_DISTANCE,
        "color": FILE_LOW_COLOR_MAX_DISTANCE,
        "detail": FILE_LOW_COLOR_DETAIL_MAX_DISTANCE,
    },
    "narrow": {
        "max_aspect": NARROW_THUMBNAIL_ASPECT_MAX,
        "phash": FILE_NARROW_VISUAL_MAX_DISTANCE,
        "color": FILE_NARROW_COLOR_MAX_DISTANCE,
        "detail": FILE_NARROW_DETAIL_MAX_DISTANCE,
    },
    "panorama": {
        "min_aspect": FILE_PANORAMA_ASPECT_MIN,
        "phash": FILE_PANORAMA_VISUAL_MAX_DISTANCE,
        "color": FILE_PANORAMA_COLOR_MAX_DISTANCE,
        "detail": FILE_PANORAMA_DETAIL_MAX_DISTANCE,
    },
}
LISTENER_MEDIA_CAPTURE_TIMEOUT_SEC = 1.2
ACTION_MEDIA_STABLE_TIMEOUT_SEC = 1.2
MEDIA_FOREGROUND_THRESHOLDS = (24, 14)


class MediaIdentityError(RuntimeError):
    """The requested media row/file could not be proven to be the target."""

    def __init__(self, message: str, *, code: str = "image_ui_identity_changed"):
        super().__init__(message)
        self.code = code


class MediaFileMismatchError(MediaIdentityError):
    """A downloaded file failed the final thumbnail-to-file comparison."""


@dataclass(frozen=True)
class MediaVisualFingerprint:
    """Compact visual description shared by UI thumbnails and image files."""

    phash: str
    color_grid: tuple[int, ...]
    width: int
    height: int
    variance: float
    rect: tuple[int, int, int, int] | None = None
    detail_grid: tuple[int, ...] = ()

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height if self.height else 0.0


@dataclass(frozen=True)
class MediaTargetIdentity:
    """Immutable description captured when an image event is delivered."""

    delivery_id: str
    raw_message_id: str
    chat_name: str
    chat_hwnd: int
    message_type: str
    direction: str
    content: str
    control_class_name: str
    before: tuple[str, ...]
    after: tuple[str, ...]
    observed_index: int
    observed_count: int
    observed_at: float
    visual: MediaVisualFingerprint | None


@dataclass(frozen=True)
class MediaVisualMatch:
    """Result of comparing the captured thumbnail with a downloaded image."""

    matched: bool
    phash_distance: int | None
    color_distance: float | None
    variant: str
    detail_distance: float | None = None
    variant_metrics: tuple[tuple[str, int, float, float | None], ...] = ()
    match_rule: str = ""


def ensure_delivery_id(message: Any) -> str:
    """Return an application-owned occurrence ID, creating it once per object."""

    current = str(getattr(message, "delivery_id", "") or "").strip()
    if current:
        return current
    current = uuid.uuid4().hex
    message.delivery_id = current
    return current


def message_context_token(message: Any) -> str:
    """Return a stable, non-RuntimeId token for visible-order matching."""

    msg_type = str(getattr(message, "type", "") or "")
    direction = str(
        getattr(message, "direction", "")
        or getattr(message, "attr", "")
        or ""
    )
    content = " ".join(str(getattr(message, "content", "") or "").split())
    if msg_type == "quote":
        quote_nickname = " ".join(
            str(getattr(message, "quote_nickname", "") or "").split()
        )
        quote_content = " ".join(
            str(getattr(message, "quote_content", "") or "").split()
        )
        content = f"{content}\x1f{quote_nickname}\x1f{quote_content}"
    return f"{msg_type}\x1f{direction}\x1f{content}"


def _runtime_id(control: Any) -> str:
    try:
        return "-".join(str(part) for part in control.GetRuntimeId())
    except Exception:
        return ""


def _control_rect(control: Any) -> tuple[int, int, int, int] | None:
    try:
        rect = control.BoundingRectangle
        result = (
            int(rect.left),
            int(rect.top),
            int(rect.right),
            int(rect.bottom),
        )
        if result[2] <= result[0] or result[3] <= result[1]:
            return None
        return result
    except Exception:
        return None


def _control_top_hwnd(control: Any) -> int:
    try:
        top = control.GetTopLevelControl()
        return int(getattr(top, "NativeWindowHandle", 0) or 0)
    except Exception:
        return 0


def _dominant_background(image) -> tuple[int, int, int]:
    image = image.convert("RGB")
    width, height = image.size
    step = max(1, int(((width * height) / 30000) ** 0.5))
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for y in range(0, height, step):
        for x in range(0, width, step):
            red, green, blue = image.getpixel((x, y))
            key = red // 8, green // 8, blue // 8
            values = buckets.setdefault(key, [0, 0, 0, 0])
            values[0] += 1
            values[1] += red
            values[2] += green
            values[3] += blue
    if not buckets:
        return 255, 255, 255
    count, red, green, blue = max(buckets.values(), key=lambda values: values[0])
    return red // count, green // count, blue // count


def _largest_foreground_component(
    mask: Sequence[Sequence[bool]],
    *,
    dilation_radius: int = 2,
) -> tuple[int, int, int, int] | None:
    """Locate the likely rectangular media block in a sampled foreground mask.

    Projecting rows and columns independently can select only a dense patch
    *inside* a low-contrast photo. Joining nearby foreground pixels preserves
    the photo-shaped component while a short sender-name glyph run stays small.
    """

    height = len(mask)
    width = len(mask[0]) if height else 0
    if not width:
        return None

    expanded = [[False] * width for _ in range(height)]
    radius = max(0, int(dilation_radius))
    foreground_points: list[tuple[int, int]] = []
    for y, row in enumerate(mask):
        for x, value in enumerate(row):
            if not value:
                continue
            foreground_points.append((x, y))
            for expanded_y in range(max(0, y - radius), min(height, y + radius + 1)):
                expanded_row = expanded[expanded_y]
                for expanded_x in range(max(0, x - radius), min(width, x + radius + 1)):
                    expanded_row[expanded_x] = True
    if not foreground_points:
        return None

    visited = [[False] * width for _ in range(height)]
    best: tuple[float, int, int, int, int] | None = None
    for seed_y in range(height):
        for seed_x in range(width):
            if not expanded[seed_y][seed_x] or visited[seed_y][seed_x]:
                continue
            stack = [(seed_x, seed_y)]
            visited[seed_y][seed_x] = True
            left = right = seed_x
            top = bottom = seed_y
            while stack:
                x, y = stack.pop()
                left = min(left, x)
                right = max(right, x)
                top = min(top, y)
                bottom = max(bottom, y)
                for next_x, next_y in (
                    (x - 1, y),
                    (x + 1, y),
                    (x, y - 1),
                    (x, y + 1),
                ):
                    if (
                        0 <= next_x < width
                        and 0 <= next_y < height
                        and expanded[next_y][next_x]
                        and not visited[next_y][next_x]
                    ):
                        visited[next_y][next_x] = True
                        stack.append((next_x, next_y))

            box_width = right - left + 1
            box_height = bottom - top + 1
            if box_width < 6 or box_height < 8:
                continue
            component_points = [
                (x, y)
                for x, y in foreground_points
                if left <= x <= right and top <= y <= bottom
            ]
            original_count = len(component_points)
            area = box_width * box_height
            if original_count < 18:
                continue
            # Do not let a huge component made from sparse noise win by area.
            occupancy = min(1.0, original_count / max(1.0, area * 0.10))
            score = area * occupancy
            original_left = min(point[0] for point in component_points)
            original_top = min(point[1] for point in component_points)
            original_right = max(point[0] for point in component_points) + 1
            original_bottom = max(point[1] for point in component_points) + 1
            candidate = (
                score,
                original_left,
                original_top,
                original_right,
                original_bottom,
            )
            if best is None or candidate > best:
                best = candidate
    if best is None:
        return None
    return best[1], best[2], best[3], best[4]


def _locate_media_thumbnail(image, direction: str | None):
    """Return ``(crop, box)`` for the media block in a full message row.

    ``box`` is relative to the supplied row image.  It is ``None`` when no
    trustworthy foreground component can be isolated; callers that only need
    pixels may still inspect the conservative side-region crop, while click
    callers must reject the missing box.

    The avatar strip is excluded first.  Dense foreground row/column runs then
    remove chat background, sender text and whitespace without assuming a fixed
    thumbnail size.
    """

    image = image.convert("RGB")
    width, height = image.size
    if width < 40 or height < 32:
        return image, (0, 0, width, height)

    avatar_margin = min(88, max(8, int(width * 0.065)))
    content_span = min(680, max(100, int(width * 0.60)))
    if direction == "friend":
        left = avatar_margin
        right = min(width - 2, left + content_span)
    elif direction == "self":
        right = width - avatar_margin
        left = max(2, right - content_span)
    else:
        left = max(2, int(width * 0.12))
        right = min(width - 2, int(width * 0.88))
    if right - left < 32:
        left, right = 0, width

    region = image.crop((left, 0, right, height))
    region_width, region_height = region.size
    background = _dominant_background(image)
    sample_step = 2
    sampled_width = max(1, math.ceil(region_width / sample_step))
    sampled_height = max(1, math.ceil(region_height / sample_step))
    bg_red, bg_green, bg_blue = background
    distances = [[0] * sampled_width for _ in range(sampled_height)]
    for sample_y, y in enumerate(range(0, region_height, sample_step)):
        for sample_x, x in enumerate(range(0, region_width, sample_step)):
            red, green, blue = region.getpixel((x, y))
            distances[sample_y][sample_x] = (
                abs(red - bg_red)
                + abs(green - bg_green)
                + abs(blue - bg_blue)
            )

    component = None
    component_area = 0
    for threshold in MEDIA_FOREGROUND_THRESHOLDS:
        mask = [
            [distance >= threshold for distance in row]
            for row in distances
        ]

        candidate = _largest_foreground_component(mask)
        if candidate is None:
            continue
        candidate_width = candidate[2] - candidate[0]
        candidate_height = candidate[3] - candidate[1]
        # A lower contrast threshold can expose the subtle white boundary of a
        # screenshot thumbnail.  If it instead turns almost the entire side
        # region into one component, the background estimate was not reliable;
        # never use that component as a click credential.
        if (
            candidate_width >= sampled_width * 0.90
            and candidate_height >= sampled_height * 0.80
        ):
            continue
        candidate_area = candidate_width * candidate_height
        if candidate_area > component_area:
            component = candidate
            component_area = candidate_area

    if component is None:
        return region, None
    column_start, row_start, column_end, row_end = component

    pad = 2
    crop_box = (
        max(0, column_start * sample_step - pad),
        max(0, row_start * sample_step - pad),
        min(region_width, column_end * sample_step + pad),
        min(region_height, row_end * sample_step + pad),
    )
    cropped = region.crop(crop_box)
    if cropped.width < 12 or cropped.height < 20:
        return region, None
    full_box = (
        left + crop_box[0],
        crop_box[1],
        left + crop_box[2],
        crop_box[3],
    )
    return cropped, full_box


def extract_media_thumbnail(image, direction: str | None):
    """Crop the likely image block from a full-width WeChat message row."""

    thumbnail, _ = _locate_media_thumbnail(image, direction)
    return thumbnail


@lru_cache(maxsize=8)
def _dct_basis(size: int, low_size: int) -> tuple[tuple[float, ...], ...]:
    basis: list[tuple[float, ...]] = []
    for frequency in range(low_size):
        alpha = math.sqrt(1 / size) if frequency == 0 else math.sqrt(2 / size)
        basis.append(
            tuple(
                alpha
                * math.cos(((2 * position + 1) * frequency * math.pi) / (2 * size))
                for position in range(size)
            )
        )
    return tuple(basis)


def perceptual_hash_image(image, hash_size: int = 8, highfreq_factor: int = 4) -> str:
    """Compute a DCT perceptual hash without an external imagehash package."""

    from PIL import Image

    size = hash_size * highfreq_factor
    grayscale = image.convert("L").resize((size, size), Image.Resampling.LANCZOS)
    pixels = tuple(float(value) for value in grayscale.getdata())
    basis = _dct_basis(size, hash_size)
    coefficients: list[float] = []
    for vertical in range(hash_size):
        vertical_basis = basis[vertical]
        for horizontal in range(hash_size):
            horizontal_basis = basis[horizontal]
            total = 0.0
            for y in range(size):
                row_offset = y * size
                weighted_y = vertical_basis[y]
                for x in range(size):
                    total += pixels[row_offset + x] * horizontal_basis[x] * weighted_y
            coefficients.append(total)
    values = sorted(coefficients[1:])
    median = values[len(values) // 2] if values else 0.0
    bits = 0
    for coefficient in coefficients:
        bits = (bits << 1) | int(coefficient > median)
    return f"{bits:0{hash_size * hash_size // 4}x}"


def color_grid_signature(image, grid_size: int = 4) -> tuple[int, ...]:
    """Return a small RGB layout signature robust to thumbnail resizing."""

    from PIL import Image, ImageStat

    resized = image.convert("RGB").resize(
        (grid_size * 8, grid_size * 8), Image.Resampling.LANCZOS
    )
    cell = 8
    values: list[int] = []
    for row in range(grid_size):
        for column in range(grid_size):
            tile = resized.crop(
                (column * cell, row * cell, (column + 1) * cell, (row + 1) * cell)
            )
            values.extend(int(value) for value in ImageStat.Stat(tile).mean[:3])
    return tuple(values)


def fingerprint_image(
    image,
    *,
    rect: tuple[int, int, int, int] | None = None,
) -> MediaVisualFingerprint:
    """Build a fingerprint and reject visually empty/placeholder captures."""

    from PIL import ImageStat

    image = image.convert("RGB")
    variance = float(sum(ImageStat.Stat(image).var[:3]))
    quantized = image.resize((32, 32)).quantize(colors=32)
    colors = quantized.getcolors(maxcolors=32) or []
    if image.width < 12 or image.height < 20 or len(colors) < 3 or variance < 18.0:
        raise MediaIdentityError("图片缩略图尚未形成可识别的稳定画面")
    return MediaVisualFingerprint(
        phash=perceptual_hash_image(image),
        color_grid=color_grid_signature(image),
        width=int(image.width),
        height=int(image.height),
        variance=variance,
        rect=rect,
        detail_grid=color_grid_signature(image, grid_size=8),
    )


def capture_media_control_visual(
    control: Any,
    direction: str | None,
) -> MediaVisualFingerprint:
    """Capture and fingerprint one message thumbnail through its exact HWND."""

    from mabowx.core.win32 import capture_window_rect, get_window_rect

    rect = _control_rect(control)
    if rect is None:
        raise MediaIdentityError("图片消息控件没有可点击矩形")
    hwnd = _control_top_hwnd(control)
    if not hwnd:
        raise MediaIdentityError("图片消息控件缺少所属窗口 HWND")
    try:
        window_rect = tuple(int(value) for value in get_window_rect(hwnd))
    except Exception as exc:
        raise MediaIdentityError(f"无法读取图片所属窗口矩形: {exc}") from exc
    tolerance = 1
    if not (
        rect[0] >= window_rect[0] - tolerance
        and rect[1] >= window_rect[1] - tolerance
        and rect[2] <= window_rect[2] + tolerance
        and rect[3] <= window_rect[3] + tolerance
    ):
        raise MediaIdentityError("图片消息行未完整位于所属聊天窗口，拒绝截取局部画面")
    image = capture_window_rect(hwnd, rect)
    thumbnail, relative_box = _locate_media_thumbnail(image, direction)
    if relative_box is None:
        raise MediaIdentityError("无法从图片消息行中隔离出可安全点击的缩略图")
    media_rect = (
        rect[0] + relative_box[0],
        rect[1] + relative_box[1],
        rect[0] + relative_box[2],
        rect[1] + relative_box[3],
    )
    return fingerprint_image(thumbnail, rect=media_rect)


def phash_distance(left: str, right: str) -> int:
    if not left or not right or len(left) != len(right):
        raise ValueError("感知哈希不可比较")
    return (int(left, 16) ^ int(right, 16)).bit_count()


def color_grid_distance(left: Sequence[int], right: Sequence[int]) -> float:
    if not left or len(left) != len(right):
        return float("inf")
    return sum(abs(int(a) - int(b)) for a, b in zip(left, right)) / len(left)


def visual_distance(
    left: MediaVisualFingerprint,
    right: MediaVisualFingerprint,
) -> tuple[int, float]:
    return (
        phash_distance(left.phash, right.phash),
        color_grid_distance(left.color_grid, right.color_grid),
    )


def _capture_stable_visual(
    control: Any,
    direction: str,
    *,
    timeout: float,
    interval: float,
    capture: Callable[[Any, str | None], MediaVisualFingerprint],
) -> MediaVisualFingerprint:
    deadline = time.monotonic() + max(0.05, timeout)
    previous: MediaVisualFingerprint | None = None
    last_error = ""
    while time.monotonic() < deadline:
        try:
            current = capture(control, direction)
            if previous is not None:
                distance, color_distance = visual_distance(previous, current)
                rect_stable = previous.rect == current.rect
                if (
                    distance <= VISUAL_STABLE_MAX_DISTANCE
                    and color_distance <= VISUAL_STABLE_COLOR_MAX_DISTANCE
                    and rect_stable
                ):
                    return current
            previous = current
        except Exception as exc:
            last_error = str(exc)
            previous = None
        time.sleep(max(0.01, min(interval, deadline - time.monotonic())))
    raise MediaIdentityError(last_error or "图片缩略图或布局在采样期间持续变化")


def build_media_target_identity(
    message: Any,
    visible_messages: Sequence[Any],
    observed_index: int,
    *,
    capture: Callable[[Any, str | None], MediaVisualFingerprint] | None = None,
    capture_timeout: float = LISTENER_MEDIA_CAPTURE_TIMEOUT_SEC,
) -> MediaTargetIdentity:
    """Capture an immutable target before the listener yields to async plugins."""

    capture = capture or capture_media_control_visual
    delivery_id = ensure_delivery_id(message)
    messages = list(visible_messages)
    if not 0 <= observed_index < len(messages) or messages[observed_index] is not message:
        raise MediaIdentityError("无法在当前可见消息序列中定位图片事件")

    parent = getattr(message, "parent", None)
    root = getattr(parent, "root", None)
    chat_hwnd = int(
        getattr(root, "HWND", 0)
        or getattr(getattr(root, "control", None), "NativeWindowHandle", 0)
        or 0
    )
    chat_name = str(getattr(parent, "who", "") or "").strip()
    direction = str(getattr(message, "direction", "") or "")
    visual = _capture_stable_visual(
        getattr(message, "control", None),
        direction,
        timeout=capture_timeout,
        interval=0.06,
        capture=capture,
    )
    tokens = [message_context_token(item) for item in messages]
    return MediaTargetIdentity(
        delivery_id=delivery_id,
        raw_message_id=str(getattr(message, "id", "") or ""),
        chat_name=chat_name,
        chat_hwnd=chat_hwnd,
        message_type=str(getattr(message, "type", "") or ""),
        direction=direction,
        content=str(getattr(message, "content", "") or ""),
        control_class_name=str(
            getattr(message, "control_class_name", "")
            or getattr(getattr(message, "control", None), "ClassName", "")
            or ""
        ),
        before=tuple(tokens[max(0, observed_index - CONTEXT_BEFORE_COUNT) : observed_index]),
        after=tuple(tokens[observed_index + 1 : observed_index + 1 + CONTEXT_AFTER_COUNT]),
        observed_index=int(observed_index),
        observed_count=len(messages),
        observed_at=time.time(),
        visual=visual,
    )


def attach_delivery_context(
    visible_messages: Sequence[Any],
    delivered_messages: Iterable[Any],
    *,
    capture: Callable[[Any, str | None], MediaVisualFingerprint] | None = None,
) -> None:
    """Attach UUIDs to all deliveries and visual identities to direct images."""

    visible = list(visible_messages)
    indexes = {id(message): index for index, message in enumerate(visible)}
    for message in delivered_messages:
        ensure_delivery_id(message)
        if str(getattr(message, "type", "") or "") != "image":
            continue
        index = indexes.get(id(message))
        if index is None:
            message.media_target_error = "图片事件不在当前可见序列中"
            continue
        try:
            message.media_target_identity = build_media_target_identity(
                message,
                visible,
                index,
                capture=capture,
            )
            message.media_target_error = ""
        except Exception as exc:
            # Keep forwarding the event.  A later download will fail closed with
            # this reason instead of silently selecting a different row.
            message.media_target_identity = None
            message.media_target_error = str(exc)


def _suffix_match(expected: Sequence[str], actual: Sequence[str]) -> int:
    count = 0
    for left, right in zip(reversed(expected), reversed(actual)):
        if left != right:
            break
        count += 1
    return count


def _prefix_match(expected: Sequence[str], actual: Sequence[str]) -> int:
    count = 0
    for left, right in zip(expected, actual):
        if left != right:
            break
        count += 1
    return count


def _candidate_basic_match(target: MediaTargetIdentity, candidate: Any) -> bool:
    if str(getattr(candidate, "type", "") or "") != target.message_type:
        return False
    direction = str(getattr(candidate, "direction", "") or "")
    if target.direction and direction and direction != target.direction:
        return False
    if str(getattr(candidate, "content", "") or "") != target.content:
        return False
    control = getattr(candidate, "control", None)
    if control is None or _control_rect(control) is None:
        return False
    try:
        if not control.Exists(0):
            return False
    except Exception:
        return False
    if target.chat_hwnd:
        candidate_hwnd = _control_top_hwnd(control)
        if candidate_hwnd and candidate_hwnd != target.chat_hwnd:
            return False
    return True


def select_media_candidate(
    target: MediaTargetIdentity,
    visible_messages: Sequence[Any],
    *,
    capture: Callable[[Any, str | None], MediaVisualFingerprint] | None = None,
) -> Any:
    """Return the sole visual/context match, otherwise raise and never guess."""

    if target.visual is None:
        raise MediaIdentityError("接收时没有取得图片缩略图身份，拒绝延迟点击")
    capture = capture or capture_media_control_visual
    messages = list(visible_messages)
    tokens = [message_context_token(item) for item in messages]
    matches: list[dict[str, Any]] = []
    diagnostics = []
    for index, candidate in enumerate(messages):
        if not _candidate_basic_match(target, candidate):
            continue
        try:
            visual = capture(getattr(candidate, "control", None), target.direction)
            distance, color_distance = visual_distance(target.visual, visual)
            detail_distance = (
                color_grid_distance(target.visual.detail_grid, visual.detail_grid)
                if target.visual.detail_grid and visual.detail_grid
                else None
            )
        except Exception as exc:
            diagnostics.append({"index": index, "rejected": "capture_failed", "error": type(exc).__name__})
            continue
        diagnostics.append({"index": index, "phash": distance,
                            "color": round(color_distance, 2), "detail": detail_distance})
        if (
            distance > VISUAL_REBIND_MAX_DISTANCE
            or color_distance > VISUAL_REBIND_COLOR_MAX_DISTANCE
            or (
                detail_distance is not None
                and detail_distance > VISUAL_REBIND_DETAIL_MAX_DISTANCE
            )
        ):
            continue
        before = tokens[max(0, index - CONTEXT_BEFORE_COUNT) : index]
        after = tokens[index + 1 : index + 1 + CONTEXT_AFTER_COUNT]
        before_match = _suffix_match(target.before, before)
        after_match = _prefix_match(target.after, after)
        matches.append(
            {
                "candidate": candidate,
                "distance": distance,
                "color_distance": color_distance,
                "detail_distance": detail_distance,
                "before_match": before_match,
                "after_match": after_match,
                "raw_id": _runtime_id(getattr(candidate, "control", None))
                == target.raw_message_id,
            }
        )

    from mabowx.msgs.media_diagnostics import media_event
    media_event("candidate_selection", visible_count=len(messages), matching_count=len(matches),
                candidates=diagnostics[:12], candidate_count=len(diagnostics))
    if not matches:
        raise MediaIdentityError("当前可见区没有与接收缩略图一致的图片消息", code="target_not_visible")
    if len(matches) == 1:
        return matches[0]["candidate"]

    matches.sort(
        key=lambda item: (
            item["before_match"] + item["after_match"],
            item["before_match"],
            item["after_match"],
            -item["distance"],
            -item["color_distance"],
            int(item["raw_id"]),
        ),
        reverse=True,
    )
    best, second = matches[0], matches[1]
    best_context = best["before_match"] + best["after_match"]
    second_context = second["before_match"] + second["after_match"]
    if best_context > second_context:
        return best["candidate"]
    if best["distance"] + 3 <= second["distance"]:
        return best["candidate"]
    raise MediaIdentityError(
        f"可见区存在 {len(matches)} 条无法唯一绑定的相似图片消息", code="identity_ambiguous"
    )


def control_fully_visible(control: Any, message_list: Any) -> bool:
    """Return whether the complete message row is inside the list viewport."""

    control_rect = _control_rect(control)
    list_rect = _control_rect(message_list)
    if control_rect is None or list_rect is None:
        return False
    return bool(
        control_rect[0] >= list_rect[0]
        and control_rect[1] >= list_rect[1]
        and control_rect[2] <= list_rect[2]
        and control_rect[3] <= list_rect[3]
    )


def verify_stable_candidate(
    target: MediaTargetIdentity,
    candidate: Any,
    *,
    capture: Callable[[Any, str | None], MediaVisualFingerprint] | None = None,
    timeout: float = ACTION_MEDIA_STABLE_TIMEOUT_SEC,
) -> MediaVisualFingerprint:
    """Sample the chosen row twice immediately before a click."""

    capture = capture or capture_media_control_visual
    visual = _capture_stable_visual(
        getattr(candidate, "control", None),
        target.direction,
        timeout=timeout,
        interval=0.05,
        capture=capture,
    )
    if target.visual is None:
        raise MediaIdentityError("目标缺少接收时缩略图")
    distance, color_distance = visual_distance(target.visual, visual)
    detail_distance = (
        color_grid_distance(target.visual.detail_grid, visual.detail_grid)
        if target.visual.detail_grid and visual.detail_grid
        else None
    )
    if (
        distance > VISUAL_REBIND_MAX_DISTANCE
        or color_distance > VISUAL_REBIND_COLOR_MAX_DISTANCE
        or (
            detail_distance is not None
            and detail_distance > VISUAL_REBIND_DETAIL_MAX_DISTANCE
        )
    ):
        raise MediaIdentityError(
            "点击前缩略图身份变化（"
            f"感知距离 {distance}，颜色距离 {color_distance:.1f}，"
            f"细节距离 {detail_distance}）"
        )
    if target.chat_hwnd:
        hwnd = _control_top_hwnd(getattr(candidate, "control", None))
        if not hwnd or hwnd != target.chat_hwnd:
            raise MediaIdentityError("点击前图片控件已离开原聊天窗口")
    return visual


def verify_dispatched_candidate(
    target: MediaTargetIdentity,
    candidate: Any,
    *,
    capture: Callable[[Any, str | None], MediaVisualFingerprint] | None = None,
) -> MediaVisualFingerprint:
    """Revalidate the origin row after its exact menu/preview has appeared."""

    if target.visual is None:
        raise MediaIdentityError("目标缺少接收时缩略图")
    capture = capture or capture_media_control_visual
    visual = capture(getattr(candidate, "control", None), target.direction)
    distance, color_distance = visual_distance(target.visual, visual)
    detail_distance = (
        color_grid_distance(target.visual.detail_grid, visual.detail_grid)
        if target.visual.detail_grid and visual.detail_grid
        else None
    )
    if (
        distance > VISUAL_REBIND_MAX_DISTANCE
        or color_distance > VISUAL_REBIND_COLOR_MAX_DISTANCE
        or (
            detail_distance is not None
            and detail_distance > VISUAL_REBIND_DETAIL_MAX_DISTANCE
        )
    ):
        raise MediaIdentityError(
            "点击投递后原消息缩略图身份变化（"
            f"感知距离 {distance}，颜色距离 {color_distance:.1f}，"
            f"细节距离 {detail_distance}）"
        )
    if target.chat_hwnd:
        hwnd = _control_top_hwnd(getattr(candidate, "control", None))
        if not hwnd or hwnd != target.chat_hwnd:
            raise MediaIdentityError("点击投递后图片控件已离开原聊天窗口")
    return visual


def _image_variants(image, target_aspect: float):
    """Yield full and likely WeChat-thumbnail crops for file verification."""

    from PIL import Image, ImageOps

    image = image.convert("RGB")
    yield "full", image
    if target_aspect <= 0 or image.width <= 0 or image.height <= 0:
        return
    source_aspect = image.width / image.height
    if abs(source_aspect - target_aspect) / max(source_aspect, target_aspect) < 0.04:
        return
    target_width = 320
    target_height = max(32, int(round(target_width / target_aspect)))
    yield "center_crop", ImageOps.fit(
        image,
        (target_width, target_height),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )
    if source_aspect > target_aspect:
        # The source is wider than the bubble. Vary the horizontal anchor;
        # changing the vertical anchor here would produce identical crops.
        crop_centers = (
            ("left_crop", (0.0, 0.5)),
            ("left_center_crop", (0.25, 0.5)),
            ("right_center_crop", (0.75, 0.5)),
            ("right_crop", (1.0, 0.5)),
        )
    else:
        # Tall screenshots can be biased toward their header or lower content.
        crop_centers = (
            ("top_crop", (0.5, 0.0)),
            ("upper_center_crop", (0.5, 0.25)),
            ("lower_center_crop", (0.5, 0.75)),
            ("bottom_crop", (0.5, 1.0)),
        )
    for name, centering in crop_centers:
        yield name, ImageOps.fit(
            image,
            (target_width, target_height),
            method=Image.Resampling.LANCZOS,
            centering=centering,
        )

    contained = ImageOps.contain(
        image,
        (target_width, target_height),
        method=Image.Resampling.LANCZOS,
    )
    for name, background in (
        ("contain_light", (245, 245, 245)),
        ("contain_dark", (24, 24, 24)),
    ):
        canvas = Image.new("RGB", (target_width, target_height), background)
        canvas.paste(
            contained,
            (
                (target_width - contained.width) // 2,
                (target_height - contained.height) // 2,
            ),
        )
        yield name, canvas


def _file_match_rule(
    target_visual: MediaVisualFingerprint,
    distance: int,
    color_distance: float,
    detail_distance: float | None,
) -> str | None:
    """Return the relaxed match rule satisfied by one file variant."""

    # Preserve the old high-confidence path as a named rule for audit logs.
    if distance <= 22 and (color_distance <= 40.0 or distance <= 11):
        return "legacy_strict"

    if detail_distance is None:
        if (
            distance <= FILE_NO_DETAIL_VISUAL_MAX_DISTANCE
            and color_distance <= FILE_NO_DETAIL_COLOR_MAX_DISTANCE
        ):
            return "relaxed_no_detail"
        return None

    if (
        distance <= FILE_LOW_COLOR_VISUAL_MAX_DISTANCE
        and color_distance <= FILE_LOW_COLOR_MAX_DISTANCE
        and detail_distance <= FILE_LOW_COLOR_DETAIL_MAX_DISTANCE
    ):
        return "relaxed_low_color"

    if (
        target_visual.aspect_ratio <= NARROW_THUMBNAIL_ASPECT_MAX
        and distance <= FILE_NARROW_VISUAL_MAX_DISTANCE
        and color_distance <= FILE_NARROW_COLOR_MAX_DISTANCE
        and detail_distance <= FILE_NARROW_DETAIL_MAX_DISTANCE
    ):
        return "relaxed_narrow"

    if (
        target_visual.aspect_ratio >= FILE_PANORAMA_ASPECT_MIN
        and distance <= FILE_PANORAMA_VISUAL_MAX_DISTANCE
        and color_distance <= FILE_PANORAMA_COLOR_MAX_DISTANCE
        and detail_distance <= FILE_PANORAMA_DETAIL_MAX_DISTANCE
    ):
        return "relaxed_panorama"

    if (
        NARROW_THUMBNAIL_ASPECT_MAX < target_visual.aspect_ratio < FILE_PANORAMA_ASPECT_MIN
        and distance <= FILE_VISUAL_MAX_DISTANCE
        and color_distance <= FILE_COLOR_MAX_DISTANCE
        and detail_distance <= FILE_DETAIL_MAX_DISTANCE
    ):
        return "relaxed_standard"
    return None


def compare_target_to_file(
    target: MediaTargetIdentity,
    file_path: str | Path,
) -> MediaVisualMatch:
    """Compare downloaded pixels with the thumbnail captured at receipt."""

    if target.visual is None:
        return MediaVisualMatch(
            False,
            None,
            None,
            "missing_target",
            match_rule="missing_target",
        )
    from PIL import Image

    with Image.open(file_path) as opened:
        variants = list(_image_variants(opened.copy(), target.visual.aspect_ratio))
    best: tuple[int, float, float | None, str] | None = None
    best_match: tuple[int, float, float | None, str, str] | None = None
    variant_metrics: list[tuple[str, int, float, float | None]] = []
    for name, image in variants:
        try:
            candidate = fingerprint_image(image)
            distance, color_distance = visual_distance(target.visual, candidate)
            detail_distance = (
                color_grid_distance(target.visual.detail_grid, candidate.detail_grid)
                if target.visual.detail_grid and candidate.detail_grid
                else None
            )
        except Exception:
            continue
        variant_metrics.append((name, distance, color_distance, detail_distance))
        result = (distance, color_distance, detail_distance, name)
        result_key = (
            result[0],
            result[1],
            result[2] if result[2] is not None else float("inf"),
        )
        best_key = (
            best[0],
            best[1],
            best[2] if best is not None and best[2] is not None else float("inf"),
        ) if best is not None else None
        if best is None or (best_key is not None and result_key < best_key):
            best = result
        match_rule = _file_match_rule(
            target.visual,
            distance,
            color_distance,
            detail_distance,
        )
        if match_rule:
            matched_result = (*result, match_rule)
            match_key = (
                best_match[0],
                best_match[1],
                best_match[2]
                if best_match is not None and best_match[2] is not None
                else float("inf"),
            ) if best_match is not None else None
            if best_match is None or (match_key is not None and result_key < match_key):
                best_match = matched_result
    if best is None:
        return MediaVisualMatch(
            False,
            None,
            None,
            "unreadable",
            variant_metrics=tuple(variant_metrics),
            match_rule="unreadable",
        )
    matched = best_match is not None
    if best_match is not None:
        distance, color_distance, detail_distance, name, match_rule = best_match
    else:
        distance, color_distance, detail_distance, name = best
        match_rule = "rejected"
    return MediaVisualMatch(
        matched,
        distance,
        color_distance,
        name,
        detail_distance,
        tuple(variant_metrics),
        match_rule,
    )
