"""Validate generated GIFs and static SVGs without changing their original bytes."""
from __future__ import annotations

import io
import math
import re
import xml.etree.ElementTree as ET

from PIL import Image

MAX_MEDIA_BYTES = 50 * 1024 * 1024
MAX_FRAME_PIXELS = 40_000_000
MAX_ANIMATION_PIXELS = 128_000_000  # Local decode budget, separate from API limits.
SVG_NS = "http://www.w3.org/2000/svg"
SVG_ELEMENTS = set("""svg g defs symbol use path rect circle ellipse line polyline polygon
    text tspan textPath title desc style linearGradient radialGradient stop clipPath mask
    pattern marker filter feBlend feColorMatrix feComponentTransfer feComposite
    feConvolveMatrix feDiffuseLighting feDisplacementMap feDistantLight feDropShadow
    feFlood feFuncA feFuncB feFuncG feFuncR feGaussianBlur feMerge feMergeNode
    feMorphology feOffset fePointLight feSpecularLighting feSpotLight feTile feTurbulence""".split())


def inspect_gif(data):
    if not data or len(data) > MAX_MEDIA_BYTES:
        raise ValueError("GIF 为空或超过大小限制")
    if not data.endswith(b";"):
        raise ValueError("接口返回的 GIF 动画不完整")
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format != "GIF":
                raise ValueError("接口没有返回 GIF 文件")
            size, frames, duration, pixels = im.size, 0, 0, 0
            while True:
                try:
                    im.seek(frames)
                except EOFError:
                    break
                pixels += im.width * im.height
                if frames >= 64 or im.width * im.height > MAX_FRAME_PIXELS or pixels > MAX_ANIMATION_PIXELS:
                    raise ValueError("GIF 超过本地帧数或解码像素限制")
                im.load()  # Decode every frame: a good first frame is insufficient.
                duration += int(im.info.get("duration", 0))
                frames += 1
            if frames < 2:
                raise ValueError("接口返回了单帧 GIF，没有生成动画")
            return size, {"frames": frames, "duration_ms": duration}
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("接口返回的 GIF 动画损坏") from exc


def _svg_value_is_local(value):
    # Disallow CSS escapes/comments/imports, then allow only fragment URL refs.
    if any(token in value for token in ("\\", "@", "/*")):
        return False
    if re.search(r"(?:https?|data|javascript|file):|//|(?:image-set|expression)\s*\(", value, re.I):
        return False
    remainder = re.sub(r'''url\(\s*["']?\#[\w.:-]+["']?\s*\)''', "", value, flags=re.I)
    return re.search(r"url\s*\(", remainder, re.I) is None


def inspect_svg(data):
    if not data or len(data) > MAX_MEDIA_BYTES:
        raise ValueError("SVG 为空或超过大小限制")
    try:
        source = data.decode("utf-8-sig")
        if re.search(r"<!\s*(DOCTYPE|ENTITY)|<\?xml-stylesheet", source, re.I):
            raise ValueError("SVG 不能包含外部声明")
        root = ET.fromstring(source)
        if root.tag not in {"svg", f"{{{SVG_NS}}}svg"}:
            raise ValueError("返回内容不是 SVG")
        for index, element in enumerate(root.iter()):
            tag = element.tag.removeprefix(f"{{{SVG_NS}}}")
            if index > 100000 or tag not in SVG_ELEMENTS:
                raise ValueError("SVG 包含不支持的非静态内容")
            if tag == "style" and not _svg_value_is_local(element.text or ""):
                raise ValueError("SVG 样式不能引用外部资源")
            for name, value in element.attrib.items():
                name = name.removeprefix("{http://www.w3.org/1999/xlink}").removeprefix("{http://www.w3.org/XML/1998/namespace}")
                if (name.startswith("{") or name.lower().startswith("on") or name == "base"
                        or name == "href" and not re.fullmatch(r"#[\w.:-]+", value)
                        or not _svg_value_is_local(value)):
                    raise ValueError("SVG 不能包含脚本或外部资源")
        # ViewBox dimensions are used for vector previews when width/height
        # are percentages or physical units. Do not rasterize the deliverable.
        viewbox = re.split(r"[\s,]+", root.get("viewBox", "").strip())
        try:
            if len(viewbox) == 4:
                numbers = [float(v) for v in viewbox]
                width, height = numbers[2:]
                if not all(math.isfinite(v) for v in numbers):
                    raise ValueError()
            else:
                width, height = (float(root.get(k, "").removesuffix("px")) for k in ("width", "height"))
        except ValueError:
            raise ValueError("SVG 尺寸无效") from None
        if not all(math.isfinite(v) and 0 < v <= 100000 for v in (width, height)):
            raise ValueError("SVG 尺寸无效")
        return max(1, round(width)), max(1, round(height))
    except (ET.ParseError, UnicodeError, TypeError, OverflowError) as exc:
        raise ValueError("接口返回的 SVG 格式无效") from exc


def image_mime(kind):
    return "image/svg+xml" if kind == "svg" else "image/" + kind


def compose_gif_bytes(sources, *, frame_duration_ms=200, loop=0, cancelled=None):
    """Encode existing static frames only. No model, network call or visual synthesis."""
    if type(frame_duration_ms) is not int or not 20 <= frame_duration_ms <= 2000:
        raise ValueError("每帧时长必须为 20–2000 毫秒的整数")
    if type(loop) is not int or not 0 <= loop <= 65535:
        raise ValueError("循环次数必须为 0–65535 的整数，0 表示无限循环")
    frames, size = [], None
    try:
        for data in sources:
            if cancelled and cancelled():
                raise ValueError("动图合成已取消")
            if len(frames) >= 64 or not data or len(data) > MAX_MEDIA_BYTES:
                raise ValueError("动图帧数或文件大小超过限制")
            with Image.open(io.BytesIO(data)) as im:
                if im.format not in {"PNG", "JPEG", "WEBP"} or getattr(im, "n_frames", 1) != 1:
                    raise ValueError("请先提供独立的 PNG、JPEG 或 WebP 静态帧")
                if size is not None and im.size != size:
                    raise ValueError("各帧尺寸必须一致，请先用本地工具统一尺寸")
                size = im.size
                pixels = im.width * im.height
                if pixels > MAX_FRAME_PIXELS or pixels * (len(frames) + 1) > MAX_ANIMATION_PIXELS:
                    raise ValueError("动图超过本地解码像素限制")
                frames.append(im.convert("RGBA"))
        if len(frames) < 2:
            raise ValueError("至少需要 2 张不同的静态帧")
        output = io.BytesIO()
        frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:],
                       duration=frame_duration_ms, loop=loop, disposal=2, optimize=False)
        data = output.getvalue()
        actual_size, details = inspect_gif(data)
        if cancelled and cancelled():
            raise ValueError("动图合成已取消")
        return data, {"format": "gif", "width": actual_size[0], "height": actual_size[1], **details}
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("静态帧损坏或无法编码为 GIF") from exc
    finally:
        for frame in frames:
            frame.close()
