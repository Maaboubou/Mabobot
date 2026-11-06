"""Verify a resolved emoticon against the exact quoted thumbnail.

Memory XML is a candidate source, not proof of the current chat. A downloaded
resource is exposed to the application only after this independent UI check.
No screenshots or animation frames are written to disk.
"""
from __future__ import annotations

from pathlib import Path
import time

from .identity import MediaIdentityError


def capture_quote_row(message):
    from mabowx.core.win32 import capture_window_rect, get_window_rect

    control = message.control
    rect = control.BoundingRectangle
    bounds = tuple(int(v) for v in (rect.left, rect.top, rect.right, rect.bottom))
    top = control.GetTopLevelControl()
    hwnd = int(getattr(top, "NativeWindowHandle", 0) or 0)
    if not hwnd or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        raise MediaIdentityError("引用表情控件没有有效的所属窗口或矩形")
    window = get_window_rect(hwnd)
    if not (bounds[0] >= window[0] and bounds[1] >= window[1]
            and bounds[2] <= window[2] and bounds[3] <= window[3]):
        raise MediaIdentityError("引用表情未完整显示，无法核对原文件")
    return capture_window_rect(hwnd, bounds).convert("RGB")


def match_quote_emoticon(row, direction: str, file_path: str | Path, *, timeout=4.0) -> dict:
    """Find a real animation frame in the quote block, with color verification.

    The thumbnail may be any frame and is usually a square crop. Matching it
    does not change the original file or turn the model input into a still.
    """
    import cv2
    import numpy as np
    from PIL import Image, ImageOps

    if direction not in {"friend", "self"}:
        raise MediaIdentityError("引用表情的消息方向无法确认")
    width, height = row.size
    if width < 150 or height < 60:
        raise MediaIdentityError("引用表情缩略图不可见")
    margin = max(65, int(width * 0.045))
    left, right = ((margin, min(width // 2 + 80, 800)) if direction == "friend"
                   else (max(width // 2 - 80, width - 800), width - margin))
    top = max(0, height - 110)
    search = np.asarray(row.crop((left, top, right, height - 5)).convert("RGB"))
    if min(search.shape[:2]) < 24:
        raise MediaIdentityError("引用表情缩略图区域过小")
    deadline = time.monotonic() + max(0.1, timeout)
    best = {"matched": False, "similarity": 0.0, "pixel_error": 255.0}
    with Image.open(file_path) as animation:
        frame_count = getattr(animation, "n_frames", 1)
        if frame_count > 1000 or animation.width * animation.height > 16_000_000:
            raise MediaIdentityError("表情超出视觉身份核对上限")
        indexes = {round(i * (frame_count - 1) / max(1, min(frame_count, 64) - 1))
                   for i in range(min(frame_count, 64))}
        for frame in sorted(indexes):
            if time.monotonic() >= deadline:
                break
            animation.seek(frame)
            rgba = animation.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (245, 245, 245, 255))
            background.alpha_composite(rgba)
            image = background.convert("RGB")
            for size in range(28, min(90, search.shape[0], search.shape[1]) + 1, 2):
                if time.monotonic() >= deadline:
                    break
                variants = [ImageOps.fit(image, (size, size), method=Image.Resampling.LANCZOS)]
                ratio = image.width / image.height
                if abs(ratio - 1.0) > 0.04:
                    dimensions = (size, max(20, round(size / ratio))) if ratio >= 1 else (max(20, round(size * ratio)), size)
                    variants.append(image.resize(dimensions, Image.Resampling.LANCZOS))
                for variant in variants:
                    template = np.asarray(variant)
                    th, tw = template.shape[:2]
                    if (th > search.shape[0] or tw > search.shape[1]
                            or float(template.astype(np.float32).std(axis=(0, 1)).max()) < 8):
                        continue
                    scores = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
                    _, score, _, point = cv2.minMaxLoc(scores)
                    x, y = point
                    actual = search[y:y + th, x:x + tw]
                    error = float(np.abs(actual.astype(np.float32) - template.astype(np.float32)).mean())
                    if score > best["similarity"]:
                        best = {"matched": False, "similarity": round(score, 4),
                                "pixel_error": round(error, 2), "frame": frame, "thumbnail_size": [tw, th]}
                    # Correlation alone can confuse two differently colored
                    # variants. Require both structure and absolute RGB match.
                    if score >= 0.82 and error <= 30:
                        return {"matched": True, "similarity": round(score, 4),
                                "pixel_error": round(error, 2), "frame": frame, "thumbnail_size": [tw, th]}
    return best
