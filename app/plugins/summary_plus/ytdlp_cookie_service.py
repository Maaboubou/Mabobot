"""Browser-cookie helpers for summary_plus yt-dlp platform downloads."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence

import websocket

from app.services.shared_chrome import get_shared_chrome_operation_lock
from app.services.shared_chrome_runtime import get_shared_chrome_runtime
from .background_page import BackgroundPage


_PLATFORM_COOKIE_DOMAINS = {
    "douyin": ("douyin.com", "iesdouyin.com"),
    "xiaohongshu": ("xiaohongshu.com", "xhslink.com", "xhslink.cn"),
}

_douyin_refresh_attempts: Dict[int, float] = {}


def refresh_douyin_browser_cookies(debug_port: int, logger: logging.Logger) -> bool:
    """Visit Douyin in the managed profile, with a cooldown shared by workers."""
    with get_shared_chrome_operation_lock():
        now = time.monotonic()
        if now - _douyin_refresh_attempts.get(debug_port, float('-inf')) < 60:
            logger.info("🍪 抖音 Cookie 刚尝试刷新过，跳过重复打开")
            return False
        _douyin_refresh_attempts[debug_port] = now
        page = None
        try:
            logger.info("🍪 抖音需要新鲜 Cookie，自动用项目 Chrome 访问抖音后重试")
            page = BackgroundPage(debug_port, "https://www.douyin.com/")
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if page.execute_script("return document.readyState") in {"interactive", "complete"}:
                    # Cookie initialization also runs asynchronously after load.
                    time.sleep(3)
                    return True
                time.sleep(0.5)
            logger.warning("⚠️ 抖音页面未完全加载，仍重新导出 Cookie 尝试一次")
            return True
        except Exception as exc:
            logger.warning("⚠️ 自动刷新抖音 Cookie 失败: %s", exc)
            return False
        finally:
            if page is not None:
                page.close()


def _read_debug_browser_cookies(
    debug_port: int,
    domains: Sequence[str],
) -> List[Dict[str, Any]]:
    with get_shared_chrome_operation_lock():
        runtime = get_shared_chrome_runtime(debug_port=debug_port)
        connection = runtime.connect_cdp(
            lambda endpoint: websocket.create_connection(endpoint, timeout=5, suppress_origin=True)
        )
        try:
            connection.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
            while True:
                payload = json.loads(connection.recv())
                if payload.get("id") != 1:
                    continue
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                cookies = payload.get("result", {}).get("cookies", [])
                if not isinstance(cookies, list):
                    return []
                return [
                    cookie
                    for cookie in cookies
                    if isinstance(cookie, dict)
                    and any(
                        (
                            cookie_domain := str(cookie.get("domain") or "")
                            .lstrip(".")
                            .casefold()
                        ) == domain
                        or cookie_domain.endswith(f".{domain}")
                        for domain in domains
                    )
                ]
        finally:
            connection.close()


def _write_netscape_cookie_file(cookies: Sequence[Dict[str, Any]], path: str) -> None:
    lines = ["# Netscape HTTP Cookie File", ""]
    for cookie in cookies:
        raw_domain = str(cookie.get("domain") or "")
        if not raw_domain:
            continue
        domain = f"#HttpOnly_{raw_domain}" if cookie.get("httpOnly") else raw_domain
        try:
            expires = max(0, int(float(cookie.get("expires") or 0)))
        except (TypeError, ValueError):
            expires = 0
        fields = (
            domain,
            "TRUE" if raw_domain.startswith(".") else "FALSE",
            str(cookie.get("path") or "/"),
            "TRUE" if cookie.get("secure") else "FALSE",
            str(expires),
            str(cookie.get("name") or "").replace("\t", ""),
            str(cookie.get("value") or "")
            .replace("\t", "")
            .replace("\r", "")
            .replace("\n", ""),
        )
        lines.append("\t".join(fields))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _browser_profile_path(user_data_dir: str, profile_dir: str) -> str:
    root = os.path.abspath(os.path.expandvars(os.path.expanduser(user_data_dir)))
    profile = str(profile_dir or "").strip()
    return os.path.join(root, profile) if profile else root


@contextmanager
def ytdlp_browser_cookie_args(
    *,
    platform: str,
    debug_port: int,
    user_data_dir: str,
    profile_dir: str,
    logger: logging.Logger,
) -> Iterator[List[str]]:
    """Yield yt-dlp cookie arguments and always remove exported live cookies."""
    domains = _PLATFORM_COOKIE_DOMAINS.get(platform)
    if not domains:
        raise ValueError(f"Unsupported cookie platform: {platform}")

    cookie_path = ""
    try:
        try:
            cookies = _read_debug_browser_cookies(debug_port, domains)
        except Exception as exc:
            cookies = []
            logger.warning(
                "⚠️ 无法从项目调试 Chrome 实时读取 %s Cookie，将尝试浏览器配置目录: %s",
                platform,
                exc,
            )

        if cookies:
            handle, cookie_path = tempfile.mkstemp(
                prefix=f"summary_plus_{platform}_",
                suffix=".cookies.txt",
            )
            os.close(handle)
            _write_netscape_cookie_file(cookies, cookie_path)
            logger.info(
                "🍪 已从项目调试 Chrome 临时导出 %s 域 Cookie（%s 条）",
                platform,
                len(cookies),
            )
            yield ["--cookies", cookie_path]
            return

        profile_path = _browser_profile_path(user_data_dir, profile_dir)
        logger.info(
            "🍪 调试会话无可用 %s Cookie，尝试 --cookies-from-browser 读取项目 Profile: %s",
            platform,
            profile_path,
        )
        yield ["--cookies-from-browser", f"chrome:{profile_path}"]
    finally:
        if cookie_path:
            try:
                os.remove(cookie_path)
            except OSError:
                logger.warning("⚠️ 删除 yt-dlp 临时 Cookie 文件失败: %s", cookie_path)
