"""One disposable browser process per image, supervised by codex_lens_tool.

Upload/wait/Visual-matches handling adapted from sbigg/identify/pipeline.py.
The worker never follows search-result links or uses a personal browser profile.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from app.services.lens.browser_profile import (
    CONSENT_COOKIES,
    HEADERS,
    LAUNCH_ARGS,
    USER_AGENT,
)
from app.services.lens.parser import extract_results


class LensFailure(Exception):
    def __init__(self, status, **diagnostics):
        super().__init__(status)
        self.diagnostics = diagnostics


def allowed_request(url: str, method: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or parsed.port not in {None, 443} or parsed.username or parsed.password:
            return False
        if method in {"GET", "HEAD", "OPTIONS"}:
            return host in {"google.com", "www.google.com", "lens.google.com"} or any(
                host == suffix or host.endswith("." + suffix) for suffix in ("gstatic.com", "googleusercontent.com")
            )
        # Uploads only; do not relax the general wx_browser read-only policy.
        return method == "POST" and host in {"www.google.com", "lens.google.com"} and parsed.path.startswith(
            ("/v3/upload", "/upload", "/_/LensWebStandaloneUi/", "/search")
        )
    except ValueError:
        return False


def browser_executable() -> str | None:
    configured = os.getenv("CODEX_LENS_BROWSER_PATH", "").strip()
    if configured:
        return configured
    if os.name == "nt":
        for root in (os.getenv("PROGRAMFILES"), os.getenv("PROGRAMFILES(X86)"), os.getenv("LOCALAPPDATA")):
            if root:
                for relative in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
                    candidate = Path(root) / relative
                    if candidate.is_file():
                        return str(candidate)
    return None


def prepare_upload(source: Path, folder: Path) -> Path:
    from PIL import Image, ImageOps
    if source.stat().st_size > 20 * 1024 * 1024:
        raise LensFailure("image_too_large")
    with Image.open(source) as image:
        if image.width * image.height > 40_000_000:
            raise LensFailure("image_too_large")
        image = ImageOps.exif_transpose(image).convert("RGB")
        side = max(image.size)
        target = min(1600, max(640, side))
        if target != side:
            image = image.resize(tuple(max(1, round(n * target / side)) for n in image.size), Image.Resampling.LANCZOS)
        upload = folder / "upload.jpg"
        # Re-encoding also removes EXIF and unrelated metadata before upload.
        image.save(upload, "JPEG", quality=92)
    return upload


def block_reason(page) -> str | None:
    # Query strings and hidden/background CAPTCHA components are not proof
    # that Google has blocked the results. Never automate solving a challenge.
    parsed = urlsplit(page.url)
    if parsed.hostname in {"google.com", "www.google.com", "lens.google.com"} and parsed.path.startswith("/sorry/"):
        return "google_sorry_page"
    challenges = page.locator('form[action*="sorry"], iframe[src*="recaptcha"]')
    for index in range(challenges.count()):
        if challenges.nth(index).is_visible():
            return "visible_verification_challenge"
    return None


def check_blocked(page):
    reason = block_reason(page)
    if reason:
        parsed = urlsplit(page.url)
        raise LensFailure("google_blocked", block_reason=reason,
                          page_location=f"{parsed.hostname}{parsed.path}"[:300])


_READY = r'''() => document.documentElement.innerHTML.includes('"2003"') || document.querySelectorAll('a.LBcIee').length >= 3'''


def wait_cards(page) -> bool:
    check_blocked(page)
    try:
        page.wait_for_function(_READY, timeout=12000)
        return True
    except Exception as exc:
        if type(exc).__name__ != "TimeoutError":
            raise
        check_blocked(page)
        return False


def upload_image(page, upload: Path, progress):
    """Keep sbigg's full-load and upload-dialog retry behavior."""
    for attempt in range(2):
        progress("打开 Google 搜索页" if attempt == 0 else "重试：打开 Google 搜索页")
        page.goto("https://www.google.com/?hl=en&safe=active", wait_until="load", timeout=45000)
        check_blocked(page)
        progress("打开以图搜索上传框")
        page.locator('div[role="button"][aria-label*="Search by image"]').first.click(timeout=20000)
        progress("等待图片上传控件")
        upload_input = page.locator('input[type="file"][name="encoded_image"]').first
        try:
            upload_input.wait_for(state="attached", timeout=30000)
        except Exception as exc:
            if type(exc).__name__ != "TimeoutError" or attempt == 1:
                raise
            continue
        progress("提交图片文件")
        upload_input.set_input_files({"name": upload.name, "mimeType": "image/jpeg", "buffer": upload.read_bytes()})
        return


def search(source: Path, folder: Path) -> dict:
    # Do not silently substitute another driver: sbigg's verified runtime uses
    # Patchright, and changing it produces materially different Lens behavior.
    from patchright.sync_api import sync_playwright

    from app.services.codex_browser_proxy import PinnedPublicProxy
    from app.services.codex_browser_tool import PublicUrlGuard

    def progress(stage):
        (folder / "progress.txt").write_text(stage, encoding="utf-8")

    upload = prepare_upload(source, folder)
    guard = PublicUrlGuard()

    def resolve(host, port):
        if port != 443 or not allowed_request("https://" + host + "/", "GET"):
            raise LensFailure("network_target_denied")
        return guard.resolve_public_endpoint(host, port)

    progress("启动图片检索")
    with PinnedPublicProxy(resolve) as proxy, sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True, executable_path=browser_executable(), args=LAUNCH_ARGS)
        except Exception as exc:
            raise LensFailure("browser_unavailable") from exc
        try:
            context = browser.new_context(
                accept_downloads=False, service_workers="block", locale="en-US",
                viewport={"width": 1440, "height": 1000}, proxy=proxy.playwright_settings,
                user_agent=USER_AGENT, extra_http_headers=HEADERS,
            )
            context.add_cookies(CONSENT_COOKIES)

            def route_request(route):
                request = route.request
                if allowed_request(request.url, request.method):
                    route.continue_()
                else:
                    route.abort()

            context.route("**/*", route_request)
            if hasattr(context, "route_web_socket"):
                context.route_web_socket("**/*", lambda ws: ws.close())
            page = context.new_page()
            page.set_default_timeout(20000)
            page.set_default_navigation_timeout(30000)
            for attempt in range(2):
                upload_image(page, upload, progress)
                progress("等待上传后的结果页跳转")
                page.wait_for_url(lambda url: urlsplit(url).path == "/search" or urlsplit(url).hostname == "lens.google.com" or "/sorry/" in url,
                                  wait_until="domcontentloaded", timeout=30000)
                progress("等待匹配结果")
                if not wait_cards(page):
                    links = page.get_by_role("link", name="Visual matches", exact=True)
                    if links.count():
                        links.first.click()
                        wait_cards(page)
                old_position, stable = -1, 0
                for _ in range(16):
                    page.mouse.wheel(0, 720)
                    page.wait_for_timeout(300)
                    position = page.evaluate("window.scrollY")
                    stable = stable + 1 if position == old_position else 0
                    if stable >= 2:
                        break
                    old_position = position
                check_blocked(page)
                progress("解析标题和来源链接")
                html = page.content()
                if len(html) > 12 * 1024 * 1024:
                    raise LensFailure("response_too_large")
                results = extract_results(html)
                if results:
                    return {"status": "ok", "results": results}
            # A result marker with no decodable links is a parser failure, not evidence of no match.
            status = "parser_changed" if '"2003"' in html or "LBcIee" in html else "no_results"
            return {"status": status, "results": []}
        finally:
            browser.close()


def main():
    source, folder = Path(sys.argv[1]), Path(sys.argv[2])
    try:
        result = search(source, folder)
    except LensFailure as exc:
        result = {"status": str(exc), "results": [], **exc.diagnostics}
    except ImportError:
        result = {"status": "dependency_missing", "results": []}
    except Exception as exc:  # noqa: BLE001 - worker IPC must always return a typed failure
        result = {"status": "timeout" if "Timeout" in type(exc).__name__ else "search_failed",
                  "error_type": type(exc).__name__, "results": []}
    if result.get("status") != "ok":
        try:
            result["failed_stage"] = (folder / "progress.txt").read_text(encoding="utf-8")[:100]
        except OSError:
            pass
    temporary = folder / "result.tmp"
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary.replace(folder / "result.json")


if __name__ == "__main__":
    main()
