"""Persistent, redacted platform authentication warnings for the dashboard."""
from __future__ import annotations

from contextlib import closing
import json
import logging
import os
from pathlib import Path
import sqlite3
import time

logger = logging.getLogger(__name__)
PLATFORMS = {"douyin": "抖音", "xiaohongshu": "小红书", "bilibili": "B站"}
REASONS = {
    "cookie_required": ("Cookie 需要刷新", "登录状态缺失或已失效。请在项目专用 Chrome 中打开该平台，完成登录或验证后重试。"),
    "cookie_unreadable": ("Cookie 读取失败", "无法读取浏览器 Cookie。请检查项目专用 Chrome 是否可用，再重试。"),
    "access_denied": ("访问校验失败", "平台拒绝访问，可能与 Cookie、风控或解析器适配有关，并不一定是 Cookie 过期。请在项目专用 Chrome 中完成验证后重试；仍失败时检查 yt-dlp 适配。"),
}


def _path() -> Path:
    return Path(os.getenv("MEDIA_AUTH_HEALTH_DB", "data/media_auth_health.sqlite3"))


def record_auth_state(platform: str, reason: str, *, checked_at: float | None = None) -> None:
    """Upsert one state per platform; empty reason means verified recovery.

    Store codes only, never stderr, URLs, cookie values or account information.
    The request start time prevents a slow old request overwriting newer evidence.
    """
    if platform not in PLATFORMS or reason not in {"", *REASONS}:
        return
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS platform_auth (
                platform TEXT PRIMARY KEY, reason TEXT NOT NULL, checked_at REAL NOT NULL)""")
            db.execute("""INSERT INTO platform_auth VALUES (?, ?, ?)
                ON CONFLICT(platform) DO UPDATE SET
                    reason=excluded.reason, checked_at=excluded.checked_at
                WHERE excluded.checked_at >= platform_auth.checked_at""",
                (platform, reason, time.time() if checked_at is None else checked_at))
    except Exception:
        logger.warning("保存媒体平台登录状态失败", exc_info=True)


def observe_ytdlp_result(platform: str, result, arguments: list[str], *, checked_at: float) -> None:
    if platform not in PLATFORMS:
        return
    if result.returncode == 0:
        # A cached CDN download doesn't prove that the site's authentication works.
        if "--dump-single-json" not in arguments or "--load-info-json" in arguments:
            return
        try:
            info = json.loads(result.stdout or "")
        except (ValueError, TypeError):
            return
        if isinstance(info, dict) and info.get("id") and (info.get("formats") or info.get("thumbnails")):
            record_auth_state(platform, "", checked_at=checked_at)
        return
    error = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    cookie_error = any(token in error for token in (
        "fresh cookies", "cookies have expired", "cookies are no longer valid",
        "login required", "login is required", "sign in to", "log in to",
        "cookie失效", "cookie 失效", "请先登录", "登录已过期",
    ))
    if any(token in error for token in ("http error 403", "403: forbidden", "http error 401")):
        reason = "access_denied"
    elif any(token in error for token in (
        "could not copy chrome cookie", "failed to decrypt", "could not find chrome cookies",
        "does not look like a netscape format cookies file",
    )):
        reason = "cookie_unreadable"
    elif cookie_error:
        reason = "cookie_required"
    else:
        return
    record_auth_state(platform, reason, checked_at=checked_at)


def active_auth_warnings() -> list[dict]:
    path = _path()
    if not path.exists():
        return []
    try:
        with closing(sqlite3.connect(path, timeout=5)) as db:
            rows = db.execute("SELECT platform, reason, checked_at FROM platform_auth WHERE reason != ''").fetchall()
        return [
            {"platform": platform, "title": f"{PLATFORMS[platform]} {REASONS[reason][0]}",
             "detail": REASONS[reason][1], "at": at}
            for platform, reason, at in rows if platform in PLATFORMS and reason in REASONS
        ]
    except Exception:
        logger.warning("读取媒体平台登录状态失败", exc_info=True)
        return []
