"""Browser-cookie helpers for summary_plus yt-dlp platform downloads."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence

import requests
import websocket


_PLATFORM_COOKIE_DOMAINS = {
    "douyin": ("douyin.com", "iesdouyin.com"),
    "xiaohongshu": ("xiaohongshu.com", "xhslink.com", "xhslink.cn"),
}


def _read_debug_browser_cookies(
    debug_port: int,
    domains: Sequence[str],
) -> List[Dict[str, Any]]:
    response = requests.get(
        f"http://127.0.0.1:{debug_port}/json/version",
        timeout=3,
    )
    response.raise_for_status()
    socket_url = response.json().get("webSocketDebuggerUrl")
    if not isinstance(socket_url, str) or not socket_url.startswith("ws://"):
        raise RuntimeError("Chrome DevTools did not return a browser WebSocket URL")

    connection = websocket.create_connection(
        socket_url,
        timeout=5,
        suppress_origin=True,
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
