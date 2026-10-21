"""Lens hydration decoder adapted from sbigg/identify/extractor.py.

Keep each title bound to its actual URL; never fuzzy-pair unrelated DOM cards.
"""
from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

RECORD_KEY = "2003"


def unescape_google(value: str) -> str:
    return unescape(value)

def _object_span(html: str, pivot: int) -> tuple[int, int] | None:
    """从 ``pivot`` 往前找到最近的 ``{``，再按括号配对返回完整对象区间。"""
    start = html.rfind("{", 0, pivot)
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(html)):
        char = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return start, index + 1
    return None


def _first_image_url(node: Any) -> str | None:
    """在嵌套数组里找第一条像图片直链的字符串。"""
    if isinstance(node, str):
        return node if node.startswith("http") and _looks_like_image(node) else None
    if isinstance(node, list):
        for child in node:
            found = _first_image_url(child)
            if found:
                return found
    return None


def _looks_like_image(url: str) -> bool:
    low = url.lower()
    if "encrypted-tbn" in low or "gstatic.com" in low:
        return True
    path = urlsplit(low).path
    return bool(re.search(r"\.(jpe?g|png|webp|gif|avif|bmp)$", path)) or any(
        token in path for token in ("/img/", "/image", "media_id", "/photo", "/uploads/")
    )


def parse_records(html: str) -> list[dict[str, Any]]:
    """解析所有 ``2003`` 结构化记录（保持页面顺序）。"""
    records: list[dict[str, Any]] = []
    for match in re.finditer(rf'"{RECORD_KEY}"\s*:\s*\[', html):
        span = _object_span(html, match.start())
        if not span:
            continue
        try:
            payload = json.loads(html[span[0] : span[1]])
        except (ValueError, TypeError):
            continue
        entries = payload.get(RECORD_KEY)
        if not isinstance(entries, list) or len(entries) < 4:
            continue
        url = entries[2] if isinstance(entries[2], str) else None
        title = entries[3] if isinstance(entries[3], str) else None
        source_name = entries[12] if len(entries) > 12 and isinstance(entries[12], str) else None
        domain = entries[17] if len(entries) > 17 and isinstance(entries[17], str) else None
        if not url and not title:
            continue
        records.append(
            {
                "url": unescape_google(url) if url else None,
                "title": unescape_google(title) if title else None,
                "source_name": source_name,
                "domain": (domain or "").lower() or None,
                "origin_image": _first_image_url(entries[1] if len(entries) > 1 else None)
                or _first_image_url(entries[0] if entries else None),
            }
        )
    return records



def public_link(value: str | None) -> str | None:
    try:
        parts = urlsplit(value or "")
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        if parts.hostname in {"google.com", "www.google.com", "lens.google.com"}:
            return None
        return value if len(value) <= 4096 else None
    except ValueError:
        return None


class _Cards(HTMLParser):
    def __init__(self):
        super().__init__()
        self.records = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs = dict(attrs)
            self.current = {"url": attrs.get("href"), "title": ""} if "LBcIee" in attrs.get("class", "").split() else None

    def handle_data(self, data):
        if self.current is not None:
            self.current["title"] += " " + data.strip()

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.records.append(self.current)
            self.current = None


def extract_results(html: str) -> list[dict]:
    records = parse_records(html)
    # Only genuine result-card anchors qualify for the DOM fallback.
    cards = _Cards()
    cards.feed(html)
    records.extend(cards.records)
    seen = set()
    results = []
    for record in records:
        url = public_link(record.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({
            "title": str(record.get("title") or "")[:1000],
            "url": url,
            "source": str(record.get("source_name") or urlsplit(url).hostname)[:200],
            "image_url": public_link(record.get("origin_image")),
        })
        if len(results) >= 200:
            break
    return results
