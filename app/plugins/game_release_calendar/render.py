"""Deterministic Chinese calendar rendering and bounded evidence-linked covers.

Rendering is offline. ``attach_covers`` optionally downloads supplied artwork or
product metadata from verified official sources, without searching for games.

Network opt-in: GAME_CALENDAR_ALLOW_FAKE_DNS=1 permits the 198.18.0.0/15
benchmark DNS range ONLY for the explicit official/CDN hosts below. It is off
by default, never permits other private ranges, and retains TLS verification.
"""

from __future__ import annotations

import os
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from datetime import date, datetime, timedelta
from io import BytesIO
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from PIL import Image, ImageDraw, ImageFont, ImageOps

WIDTH = 1080
BG = "#086348"
CARD = "#07533D"
INK = "#F3FFF6"
MUTED = "#B0D5BE"
ACCENT = "#8FE3B3"
CREAM = "#F3FFF6"
BORDER = "#3A8B6D"
CARD_WIDTH = 984
CARD_HEIGHT = 270

_FAKE_DNS_DOMAINS = (
    "steampowered.com", "steamstatic.com", "xbox.com", "xboxservices.com",
    "nintendo.com", "nintendo.net", "playstation.com", "playstation.net",
    "epicgames.com", "konami.com", "square-enix.com", "igdb.com",
)
_FAKE_DNS_EXACT = {
    "lumiere-a.akamaihd.net", "images.ctfassets.net", "pbz.s-game.com",
    "xboxwire.thesourcemediaassets.com", "store-images.s-microsoft.com",
    "c1-ebgames.eb-cdn.com.au", "s.pacn.ws", "images.pushsquare.com", "staticctf.ubisoft.com",
    "www.jbhifi.com.au", "media.gamestop.com", "www.game.co.uk",
}


def _address_allowed(hostname: str, address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if ip.is_global:
        return True
    host = hostname.lower().rstrip(".")
    return (os.environ.get("GAME_CALENDAR_ALLOW_FAKE_DNS") == "1"
            and ip.version == 4 and ip in ipaddress.ip_network("198.18.0.0/15")
            and (host in _FAKE_DNS_EXACT or any(host == domain or host.endswith("." + domain)
                                             for domain in _FAKE_DNS_DOMAINS)))


def _fetch_resource(url: str, cancelled: Any = None, *, html: bool = False) -> tuple[bytes, str]:
    """HTTPS only, pin a validated public IP, and revalidate each redirect."""
    deadline = time.monotonic() + 10
    for _ in range(4):
        if cancelled and cancelled():
            raise InterruptedError("cover download cancelled")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.port not in (None, 443)):
            raise ValueError("cover URL must be public HTTPS on port 443")
        hostname = parsed.hostname
        # A subprocess bounds the otherwise uninterruptible OS DNS lookup;
        # subprocess.run kills and waits for it on timeout (no orphan workers).
        result = subprocess.run(
            [sys.executable, "-c", "import json,socket,sys; print(json.dumps(socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM)))", hostname],
            capture_output=True, text=True, check=True,
            timeout=max(0.001, deadline - time.monotonic()),
        )
        addresses = json.loads(result.stdout)
        if cancelled and cancelled():
            raise InterruptedError("cover download cancelled")
        if not addresses or any(not _address_allowed(hostname, a[4][0]) for a in addresses):
            raise ValueError("cover host resolves to a non-public address")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("cover download exceeded 10 seconds")
        # Connect to the checked address directly: a second DNS lookup must not
        # allow rebinding to an internal service. Retain hostname for TLS / Host.
        raw = socket.socket(addresses[0][0], socket.SOCK_STREAM)
        connection = http.client.HTTPSConnection(hostname, timeout=remaining)
        try:
            raw.settimeout(remaining)
            raw.connect(tuple(addresses[0][4]))
            raw.settimeout(max(0.001, deadline - time.monotonic()))
            connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=hostname)
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
            connection.request("GET", target, headers={"User-Agent": "MabobotReleaseCalendar/1.0", "Accept": "text/html" if html else "image/*"})
            connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise ValueError("cover redirect missing Location")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f"cover HTTP status {response.status}")
            content_type = (response.getheader("Content-Type") or "").lower()
            if not (content_type.startswith(("text/html", "application/xhtml+xml")) if html else content_type.startswith("image/")):
                raise ValueError("unexpected cover resource content type")
            maximum = 5 * 1024 * 1024
            if int(response.getheader("Content-Length") or 0) > maximum:
                raise ValueError("cover exceeds 5 MB")
            data = bytearray()
            while len(data) <= maximum:
                if cancelled and cancelled():
                    raise InterruptedError("cover download cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("cover download exceeded 10 seconds")
                if connection.sock:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, maximum + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > maximum:
                raise ValueError("cover exceeds 5 MB")
            return bytes(data), url
        finally:
            connection.close()
            raw.close()
    raise ValueError("too many cover redirects")


def _fetch_cover(url: str, cancelled: Any = None) -> bytes:
    return _fetch_resource(url, cancelled=cancelled)[0]


def _is_product_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return False
    if host == "store.steampowered.com":
        return bool(re.match(r"^/app/\d+(?:/|$)", parsed.path))
    if host == "store.epicgames.com":
        return bool(re.match(r"^/(?:[a-z]{2}(?:-[A-Za-z]{2})?/)?p/[^/]+", parsed.path))
    if host == "store.playstation.com":
        return bool(re.search(r"/(?:concept|product)/[^/]+", parsed.path))
    if host in ("konami.com", "www.konami.com"):
        return bool(re.match(r"^/games/castlevania/[^/]+/", parsed.path))
    if host == "www.jp.square-enix.com":
        return parsed.path.rstrip("/").casefold() == "/ffrs"
    if host == "pbz.s-game.com":
        return bool(re.fullmatch(r"/(?:m/)?(?:zh-CN|en-US)/index.html", parsed.path))
    for domain in ("playstation.com", "xbox.com"):
        if host == domain or host.endswith("." + domain):
            return bool(re.search(r"/games/[^/]+", parsed.path))
    if host == "nintendo.com" or host.endswith(".nintendo.com"):
        return bool(re.search(r"/(?:store/products|games/detail|Games/Nintendo-Switch(?:-2)?-games)/[^/]+", parsed.path))
    return False


class _ProductMetadata(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metadata: dict[str, str] = {}
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content")
            if content and key in ("og:title", "twitter:title", "og:image", "twitter:image", "twitter:image:src"):
                self.metadata.setdefault(key, content)
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data


def _metadata_cover(html: str, page_url: str, event: dict[str, Any]) -> str | None:
    parser = _ProductMetadata()
    parser.feed(html)

    def normalize(value: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())

    titles = [normalize(parser.metadata.get("og:title", "")),
              normalize(parser.metadata.get("twitter:title", "")), normalize(parser.title)]
    names = [normalize(str(event.get(key) or "")) for key in ("title_original", "title_zh")]
    names = [name for name in names if len(name) >= (4 if name.isascii() else 2)]
    if not any(name in page_title for name in names for page_title in titles):
        return None
    image = next((parser.metadata[key] for key in ("og:image", "twitter:image", "twitter:image:src") if parser.metadata.get(key)), None)
    if not image:
        return None
    url = urljoin(page_url, image)
    return url if urlsplit(url).scheme == "https" else None


def _discover_cover(event: dict[str, Any], cancelled: Any = None) -> tuple[str | None, str | None, list[dict[str, str]]]:
    attempts: list[dict[str, str]] = []
    urls = list(dict.fromkeys(str(source.get("url", "")) for source in event.get("sources", [])
                             if source.get("kind") == "official" and source.get("accessed") is True
                             and _is_product_url(str(source.get("url", "")))))[:2]
    for url in urls:
        try:
            data, final_url = _fetch_resource(url, cancelled=cancelled, html=True)
            if not _is_product_url(final_url):
                raise ValueError("product source redirected outside known product pages")
            image_url = _metadata_cover(data.decode("utf-8", errors="replace"), final_url, event)
            attempts.append({"url": url, "status": "matched" if image_url else "no_matching_metadata"})
            if image_url:
                return image_url, final_url, attempts
        except InterruptedError:
            raise
        except Exception as exc:
            attempts.append({"url": url, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    return None, None, attempts


def attach_covers(events: list[dict[str, Any]], output_dir: Path, cancelled: Any = None) -> dict[str, Any]:
    """Fetch supplied cover URLs with max three workers and mutate cover_path.

    Returns ``{downloaded, skipped, failed, cancelled, items: list}``;
    each item contains event id/title, status, and path or error. Failures are
    nonfatal: the renderer falls back to its typography layout. No searching.
    """
    cover_dir = Path(output_dir) / "covers"
    cover_dir.mkdir(parents=True, exist_ok=True)

    def download(index_event: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        index, event = index_event
        item = {"id": event.get("id", index), "title": event.get("title_zh") or event.get("title_original", "")}
        if cancelled and cancelled():
            return {**item, "status": "cancelled"}
        try:
            url = event.get("cover_url")
            source_url = None
            if not url:
                url, source_url, attempts = _discover_cover(event, cancelled=cancelled)
                item["discovery_attempts"] = attempts
            if not url:
                return {**item, "status": "skipped"}
            data = _fetch_cover(str(url), cancelled=cancelled)
            if cancelled and cancelled():
                raise InterruptedError("cover download cancelled")
            with Image.open(BytesIO(data)) as probe:
                if probe.width * probe.height > 25_000_000:
                    raise ValueError("cover exceeds 25 megapixels")
                probe.verify()
            path = cover_dir / (f"{index:03d}_" + hashlib.sha256(str(url).encode()).hexdigest()[:16] + ".jpg")
            with Image.open(BytesIO(data)) as source:
                rgb = source.convert("RGB")
                rgb.thumbnail((1200, 1600))
                rgb.save(path, "JPEG", quality=90)
            event["cover_path"] = str(path.resolve())
            if source_url:
                event["cover_url"] = url
                event["cover_source_url"] = source_url
            return {**item, "status": "downloaded", "path": str(path.resolve())}
        except InterruptedError:
            return {**item, "status": "cancelled"}
        except Exception as exc:
            return {**item, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(max_workers=3) as executor:
        items = list(executor.map(download, enumerate(events)))
    # Same artwork across regional/platform events; never copy release facts.
    available = {e["title_original"].casefold(): e for e in events if e.get("title_original") and e.get("cover_path")}
    for event, item in zip(events, items):
        original = available.get(str(event.get("title_original", "")).casefold())
        if original and not event.get("cover_path") and item["status"] != "cancelled":
            for key in ("cover_path", "cover_url", "cover_source_url"):
                if original.get(key):
                    event[key] = original[key]
            event["cover_reused_from_event_id"] = original["id"]
            item.update(status="reused", path=original["cover_path"], from_event_id=original["id"])
    return {**{status: sum(item["status"] == status for item in items)
               for status in ("downloaded", "reused", "skipped", "failed", "cancelled")}, "items": items}


@lru_cache(maxsize=96)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [os.environ.get("GAME_CALENDAR_BOLD_FONT" if bold else "GAME_CALENDAR_FONT", "")]
    if bold:
        candidates += ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                       "/mnt/c/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyhbd.ttc"]
    candidates += [os.environ.get("GAME_CALENDAR_FONT", ""),
                   "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                   "/mnt/c/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttc",
                   "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                   "/System/Library/Fonts/PingFang.ttc"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    raise RuntimeError("Chinese font unavailable; set GAME_CALENDAR_FONT to a CJK font file")


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int, limit: int = 0) -> list[str]:
    lines: list[str] = []
    for paragraph in str(text).splitlines() or [""]:
        line = ""
        for char in paragraph:
            if line and font.getlength(line + char) > width:
                if char in "，。！？；：、）》】」』”’…" and len(line) > 1:
                    lines.append(line[:-1].rstrip())
                    line = line[-1] + char
                else:
                    lines.append(line.rstrip())
                    line = char.lstrip()
            else:
                line += char
        if line:
            lines.append(line.rstrip())
    if limit and len(lines) > limit:
        lines = lines[:limit]
        while lines[-1] and font.getlength(lines[-1] + "…") > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def _lines(draw: ImageDraw.ImageDraw, lines: list[str], xy: tuple[int, int],
           font: ImageFont.FreeTypeFont, color: str, line_height: int) -> int:
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=font, fill=color, anchor="lt")
        y += line_height
    return y


def _platform_labels(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    labels: list[str] = []
    for value in values or []:
        value = str(value)
        lowered = value.casefold()
        if any(word in lowered for word in ("steam", "epic", "microsoft store", "windows", "pc")):
            value = "PC"
        else:
            for old, new in [("PlayStation 5", "PS5"), ("PlayStation 4", "PS4"),
                             ("Xbox Series X|S", "Xbox Series"), ("Xbox Series X/S", "Xbox Series"),
                             ("Nintendo Switch 2", "Switch 2"), ("Nintendo Switch", "Switch")]:
                value = value.replace(old, new)
        if value not in labels:
            labels.append(value)
    return labels


def _card(event: dict[str, Any], month: str, fonts: dict[str, Any]) -> Image.Image:
    """Every card has an identical frame and requires actual artwork."""
    title = str(event.get("title_zh") or event.get("title_original") or "未命名作品")
    original = str(event.get("title_original") or "")
    cover_path = event.get("cover_path")
    if not cover_path or not Path(cover_path).is_file():
        raise ValueError(f"Missing required cover image: {title}")
    try:
        with Image.open(cover_path) as source:
            # Preserve the complete artwork, including logos on landscape art.
            cover = ImageOps.contain(source.convert("RGB"), (150, 225), Image.Resampling.LANCZOS)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Invalid required cover image: {title}") from exc
    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), CARD)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, CARD_WIDTH - 1, CARD_HEIGHT - 1), radius=7, outline=BORDER, width=2)
    draw.rectangle((20, 22, 169, 246), fill="#063B2D")
    image.paste(cover, (20 + (150 - cover.width) // 2, 22 + (225 - cover.height) // 2))
    text_x, text_width = 196, CARD_WIDTH - 220
    title_font = _font(36, True)
    for size in range(36, 27, -1):
        title_font = _font(size, True)
        if title_font.getlength(title) <= text_width:
            break
    title_lines = _wrap(title, title_font, text_width, 2)
    title_height = 35 if len(title_lines) > 1 else 42
    _lines(draw, title_lines, (text_x, 22), title_font, INK, title_height)
    if original and original.casefold() != title.casefold():
        _lines(draw, _wrap(original, fonts["small"], text_width, 1),
               (text_x, 96 if len(title_lines) > 1 else 69), fonts["small"], MUTED, 25)
    summary = str(event.get("summary") or "")
    _lines(draw, _wrap(summary, fonts["body"], text_width, 2),
           (text_x, 125), fonts["body"], INK, 31)
    note = str(event.get("display_note") or "")
    if note:
        _lines(draw, _wrap(note, fonts["tiny"], text_width, 1),
               (text_x, 193), fonts["tiny"], ACCENT, 22)
    labels = _platform_labels(event.get("platforms"))
    label_font = fonts["pill"]
    for size in range(20, 11, -1):
        label_font = _font(size)
        if sum(label_font.getlength(label) + 22 for label in labels) + max(0, len(labels) - 1) * 8 <= text_width:
            break
    x = text_x
    for label in labels:
        width = int(label_font.getlength(label)) + 22
        if x + width > CARD_WIDTH - 24:
            raise ValueError(f"Too many platform labels for card: {title}")
        draw.rounded_rectangle((x, 226, x + width, 253), radius=4, fill="#206B50", outline=BORDER)
        draw.text((x + width / 2, 239), label, font=label_font, fill=INK, anchor="mm")
        x += width + 8
    return image


def render_calendar(events: list[dict[str, Any]], output_dir: Path, month: str,
                    as_of: str, *, max_height: int = 2800,
                    make_long_image: bool = True, make_pages: bool = False, title: str = "刘局推荐",
                    subtitle: str | None = None, **_: Any) -> list[Path]:
    """Return one continuous poster by default; pagination is an explicit export."""
    month_date = date.fromisoformat(month + "-01")
    if max_height < 900:
        raise ValueError("max_height must be at least 900 pixels")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    display_as_of = as_of
    try:
        timestamp = datetime.fromisoformat(as_of)
        display_as_of = timestamp.strftime("%Y-%m-%d %H:%M") if "T" in as_of else timestamp.strftime("%Y-%m-%d")
        if timestamp.utcoffset() == timedelta(hours=8):
            display_as_of += "（北京时间）"
    except (TypeError, ValueError):
        pass
    fonts = {"brand": _font(48, True), "heading": _font(45, True), "title": _font(36, True),
             "body": _font(23), "small": _font(19), "day": _font(43, True),
             "tiny": _font(18), "pill": _font(20)}
    ordered = sorted(events, key=lambda e: (e.get("date") or "9999", e.get("title_zh") or e.get("title_original") or ""))
    groups: list[tuple[str | None, list[tuple[dict[str, Any], Image.Image]]]] = []
    for event in ordered:
        day = event.get("date")
        day = str(day) if day and len(str(day)) == 10 else None
        if not groups or groups[-1][0] != day:
            groups.append((day, []))
        groups[-1][1].append((event, _card(event, month, fonts)))
    header, footer, section, gap = 207, 112, 76, 16

    def group_height(rows: list[Any]) -> int:
        return section + len(rows) * (CARD_HEIGHT + gap)

    pages: list[list[tuple[str | None, list[Any]]]] = [[]]
    used = header + footer
    for day, rows in groups:
        required = group_height(rows)
        if pages[-1] and used + required > max_height:
            pages.append([])
            used = header + footer
        remaining = list(rows)
        while remaining:
            capacity = (max_height - used - section) // (CARD_HEIGHT + gap)
            if capacity < 1:
                pages.append([])
                used = header + footer
                continue
            batch, remaining = remaining[:capacity], remaining[capacity:]
            pages[-1].append((day, batch))
            used += group_height(batch)
            if remaining:
                pages.append([])
                used = header + footer

    def draw_poster(page_groups: list[Any], page_number: int | None = None) -> tuple[Image.Image, dict[str, Any]]:
        height = header + footer + sum(group_height(rows) for _, rows in page_groups)
        if not page_groups:
            height = 900
        canvas = Image.new("RGB", (WIDTH, height), BG)
        draw = ImageDraw.Draw(canvas)
        draw.text((WIDTH // 2, 40), title, font=fonts["brand"], fill=INK, anchor="mt")
        heading = subtitle or f"{month_date.month}月重磅游戏发售信息"
        heading_font = fonts["heading"]
        for size in range(45, 20, -1):
            heading_font = _font(size, True)
            if heading_font.getlength(heading) <= CARD_WIDTH:
                break
        draw.text((WIDTH // 2, 105), heading, font=heading_font, fill=INK, anchor="mt")
        draw.text((WIDTH // 2, 165), str(month_date.year), font=fonts["small"], fill=MUTED, anchor="mt")
        draw.line((48, 202, WIDTH - 48, 202), fill=BORDER, width=2)
        layout: dict[str, Any] = {"width": WIDTH, "height": height, "header_count": 1, "sections": [], "cards": []}
        y = header
        for day, rows in page_groups:
            section_y = y
            if day:
                release_date = date.fromisoformat(day)
                day_text = f"{release_date.day}日"
                draw.text((49, y + 17), day_text, font=fonts["day"], fill=INK, anchor="lt")
                weekday_x = 49 + int(fonts["day"].getlength(day_text)) + 20
                draw.text((weekday_x, y + 35), "周" + "一二三四五六日"[release_date.weekday()], font=fonts["small"], fill=MUTED, anchor="lt")
            else:
                draw.text((49, y + 22), "本月日期待定", font=_font(31, True), fill=INK, anchor="lt")
            draw.text((1030, y + 35), f"{len(rows)} 款游戏", font=fonts["small"], fill=MUTED, anchor="rt")
            y += section
            layout["sections"].append({"date": day, "count": len(rows), "y": section_y})
            for event, card in rows:
                canvas.paste(card, (48, y))
                layout["cards"].append({"id": event.get("id"), "title": event.get("title_zh") or event.get("title_original"),
                                        "date": day, "x": 48, "y": y, "width": CARD_WIDTH, "height": CARD_HEIGHT})
                y += CARD_HEIGHT + gap
        if not page_groups:
            draw.text((WIDTH // 2, 400), "暂无已确认的发售信息", font=fonts["title"], fill=INK, anchor="mt")
        draw.line((48, height - footer + 13, WIDTH - 48, height - footer + 13), fill=BORDER, width=2)
        draw.text((48, height - footer + 32), f"资料截止：{display_as_of}", font=fonts["tiny"], fill=MUTED, anchor="lt")
        draw.text((48, height - footer + 66), "发售安排以官方后续公告为准", font=fonts["tiny"], fill=MUTED, anchor="lt")
        if page_number is not None and len(pages) > 1:
            draw.text((1032, height - footer + 66), f"{page_number} / {len(pages)}", font=fonts["tiny"], fill=MUTED, anchor="rt")
        return canvas, layout

    paths: list[Path] = []
    layouts: list[dict[str, Any]] = []
    for page_number, page_groups in enumerate(pages if make_pages or not make_long_image else [], 1):
        canvas, layout = draw_poster(page_groups, page_number)
        path = output_dir / f"calendar_{month}_{page_number:02d}.png"
        canvas.save(path, optimize=True)
        paths.append(path)
        layouts.append({"file": path.name, **layout})
    full_layout = None
    if make_long_image:
        canvas, full_layout = draw_poster(groups)
        full_path = output_dir / "calendar_full.png"
        canvas.save(full_path, optimize=True)
        if not make_pages:
            paths = [full_path]
    (output_dir / "layout.json").write_text(json.dumps({"card_size": [CARD_WIDTH, CARD_HEIGHT], "pages": layouts,
                                                       "full": full_layout}, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths
