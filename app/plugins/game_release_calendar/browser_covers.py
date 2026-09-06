"""Fetch cover artwork through Mabobot's existing shared Chrome on failure."""
from __future__ import annotations

import time
from urllib.parse import urlsplit

MAX_BYTES = 5 * 1024 * 1024


def _check_cancelled(cancelled):
    if cancelled and cancelled():
        raise InterruptedError("cover download cancelled")


def _validate_url(url, guard):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.port not in (None, 443):
        raise ValueError("cover URL must be public HTTPS on port 443")
    return guard.validate_public_url(url)


def _read_image(context, url, guard, cancelled):
    """Isolated context: validate redirects, block active requests and subresources."""
    blocked = []

    def route_request(route):
        request = route.request
        try:
            _check_cancelled(cancelled)
            if request.method != "GET" or request.resource_type != "document":
                route.abort()
                return
            _validate_url(request.url, guard)
            route.continue_()
        except Exception as exc:
            blocked.append(exc)
            route.abort()

    context.route("**/*", route_request)
    page = context.new_page()
    page.set_default_timeout(20000)
    _check_cancelled(cancelled)
    try:
        response = page.goto(url, wait_until="load", timeout=20000)
    except Exception:
        if blocked:
            raise blocked[0]
        raise
    _check_cancelled(cancelled)
    if blocked:
        raise blocked[0]
    if response is None or response.status != 200:
        raise ValueError(f"Chrome cover HTTP status {response.status if response else 'missing'}")
    _validate_url(response.url, guard)
    headers = response.headers
    if not headers.get("content-type", "").lower().startswith("image/"):
        raise ValueError("Chrome response is not an image")
    if int(headers.get("content-length") or 0) > MAX_BYTES:
        raise ValueError("Chrome cover exceeds 5 MB")
    data = response.body()
    _check_cancelled(cancelled)
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Chrome cover is empty or exceeds 5 MB")
    return data


def fetch_browser_cover(url, cancelled=None):
    from playwright.sync_api import sync_playwright
    from app.services.codex_browser_tool import PublicUrlGuard
    from app.services.shared_chrome import get_shared_chrome_operation_lock, load_shared_chrome_settings

    _check_cancelled(cancelled)
    guard = PublicUrlGuard()
    _validate_url(url, guard)
    lock = get_shared_chrome_operation_lock()
    deadline = time.monotonic() + 30
    while not lock.acquire(timeout=0.2):
        _check_cancelled(cancelled)
        if time.monotonic() >= deadline:
            raise TimeoutError("shared Chrome is busy")
    try:
        _check_cancelled(cancelled)
        settings = load_shared_chrome_settings()
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{settings.debug_port}", timeout=10000,
            )
            context = browser.new_context(
                accept_downloads=False, service_workers="block", java_script_enabled=False,
            )
            try:
                return _read_image(context, url, guard, cancelled)
            finally:
                # Close only our context. Never close/restart the shared browser.
                context.close()
    finally:
        lock.release()


def fetch_cover_with_fallback(url, cancelled=None):
    from .render import _fetch_cover

    try:
        return _fetch_cover(url, cancelled=cancelled), {"transport": "https"}
    except InterruptedError:
        raise
    except Exception as direct_error:
        _check_cancelled(cancelled)
        try:
            data = fetch_browser_cover(url, cancelled=cancelled)
        except InterruptedError:
            raise
        except Exception as browser_error:
            raise RuntimeError(f"HTTPS: {direct_error}; shared Chrome: {browser_error}") from browser_error
        return data, {"transport": "shared_chrome", "direct_error": str(direct_error)}
