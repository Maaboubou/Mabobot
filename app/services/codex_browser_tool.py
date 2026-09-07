"""Host-managed, read-only Chromium tools for Codex App Server turns.

The Codex process never receives browser credentials or direct network access.
Instead, the application executes a small dynamic-tool surface in an ephemeral
Playwright context and binds every call to the current chat's managed request
directory.
"""

from __future__ import annotations

import hashlib
import importlib.util
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, Optional, Sequence
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import url2pathname

from app.services.codex_browser_proxy import PinnedPublicProxy, SafeBrowserProxyError


logger = logging.getLogger(__name__)


_BROWSER_TOOL_POLICY_VERSION = "public-readonly-v1"
_BROWSER_NAMESPACE = "wx_browser"
_READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_PASSIVE_SCHEMES = frozenset({"about", "blob", "data"})
_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".home.arpa")
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_SYNTHETIC_PROXY_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_PUBLIC_WEB_PORTS = frozenset({80, 443})
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")
_WSL_PATH_RE = re.compile(r"^/mnt/([a-zA-Z])(?:/(.*))?$")


class CodexBrowserToolError(RuntimeError):
    """A safe, model-visible browser tool failure."""


@dataclass(frozen=True)
class BrowserToolContext:
    request_id: str
    chat_id: str
    access_mode: str
    workdir: Path
    request_dir: Path
    output_dir: Path
    scratch_root: Path
    runtime_workdir: str
    runtime_request_dir: str
    runtime_output_dir: str
    use_wsl: bool = False


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(callable(is_junction) and is_junction())
    except OSError:
        return True


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _safe_stem(value: Any, fallback: str) -> str:
    normalized = _SAFE_STEM_RE.sub("-", str(value or "").strip()).strip(".-_")
    return (normalized or fallback)[:80]


def _runtime_path(path: Path, context: BrowserToolContext) -> str:
    resolved = path.resolve(strict=True)
    roots = (
        (context.request_dir.resolve(strict=True), context.runtime_request_dir),
        (context.workdir.resolve(strict=True), context.runtime_workdir),
    )
    for host_root, runtime_root in roots:
        try:
            relative = resolved.relative_to(host_root)
        except ValueError:
            continue
        if context.use_wsl:
            return str(PurePosixPath(runtime_root) / PurePosixPath(relative.as_posix()))
        return str(Path(runtime_root) / relative)
    raise CodexBrowserToolError("browser artifact escaped the current chat scope")


def _host_path_from_runtime(value: Any, *, use_wsl: bool) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise CodexBrowserToolError("html_path is required")
    if use_wsl and os.name == "nt":
        match = _WSL_PATH_RE.fullmatch(raw.replace("\\", "/"))
        if match:
            drive = match.group(1).upper()
            tail = (match.group(2) or "").replace("/", "\\")
            return Path(f"{drive}:\\{tail}")
    return Path(raw).expanduser()


def _file_path_from_url(url: str) -> Path:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "file":
        raise CodexBrowserToolError("not a file URL")
    if parsed.netloc not in {"", "localhost"}:
        raise CodexBrowserToolError("remote file URLs are blocked")
    return Path(url2pathname(unquote(parsed.path)))


def _optimized_public_image_url(url: str) -> str:
    """Request catalog-sized Microsoft artwork instead of multi-megabyte originals."""
    parsed = urlsplit(str(url or ""))
    if parsed.scheme.lower() not in {"http", "https"}:
        return url
    if (parsed.hostname or "").casefold() != "store-images.s-microsoft.com":
        return url
    if not parsed.path.startswith("/image/"):
        return url
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    changed = False
    for key, value in (("w", "360"), ("h", "540"), ("q", "82")):
        if key not in query:
            query[key] = value
            changed = True
    if not changed:
        return url
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


class PublicUrlGuard:
    """Reject private-network and active requests before Chromium sends them."""

    def __init__(
        self,
        *,
        resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
        doh_resolver: Optional[Callable[[str], Sequence[str]]] = None,
        cache_seconds: int = 60,
    ) -> None:
        self._resolver = resolver
        self._doh_resolver = doh_resolver or self._resolve_with_doh
        self._cache_seconds = max(1, int(cache_seconds))
        self._cache: Dict[tuple[str, int], tuple[float, tuple[str, ...]]] = {}

    @staticmethod
    def _validate_ip(value: str) -> str:
        try:
            address = ipaddress.ip_address(value.split("%", 1)[0])
        except ValueError as exc:
            raise CodexBrowserToolError("URL hostname did not resolve to a valid address") from exc
        if not address.is_global or address in _CGNAT_NETWORK:
            raise CodexBrowserToolError("private, local, reserved, and Tailscale addresses are blocked")
        return str(address)

    @staticmethod
    def _resolve_with_doh(host: str) -> Sequence[str]:
        """Resolve a hostname behind Fake-IP DNS without trusting the fake address."""
        try:
            import requests

            response = requests.get(
                "https://cloudflare-dns.com/dns-query",
                params={"name": host, "type": "A"},
                headers={"accept": "application/dns-json"},
                timeout=10,
            )
            response.raise_for_status()
            if len(response.content) > 256 * 1024:
                raise CodexBrowserToolError("HTTPS DNS response was unexpectedly large")
            payload = response.json()
        except CodexBrowserToolError:
            raise
        except Exception as exc:
            raise CodexBrowserToolError(
                f"could not verify synthetic DNS result for public hostname: {host}"
            ) from exc
        try:
            status = int(payload.get("Status") or 0) if isinstance(payload, dict) else -1
        except (TypeError, ValueError):
            status = -1
        if status != 0:
            raise CodexBrowserToolError(f"HTTPS DNS could not resolve public hostname: {host}")
        answers = payload.get("Answer") if isinstance(payload.get("Answer"), list) else []
        addresses = []
        for answer in answers:
            if not isinstance(answer, dict) or int(answer.get("type") or 0) not in {1, 28}:
                continue
            value = str(answer.get("data") or "").strip()
            try:
                ipaddress.ip_address(value)
            except ValueError:
                continue
            if value not in addresses:
                addresses.append(value)
        if not addresses:
            raise CodexBrowserToolError(f"HTTPS DNS returned no public addresses for: {host}")
        return addresses

    def _resolve_public(self, host: str, port: int) -> tuple[str, ...]:
        cache_key = (host.casefold(), port)
        cached = self._cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] <= self._cache_seconds:
            return cached[1]

        try:
            literal = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            literal = None
        if literal is not None:
            addresses = (self._validate_ip(str(literal)),)
            self._cache[cache_key] = (now, addresses)
            return addresses

        try:
            answers = self._resolver(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise CodexBrowserToolError(f"public hostname could not be resolved: {host}") from exc
        resolved = []
        for answer in answers:
            sockaddr = answer[4] if len(answer) > 4 else ()
            if not sockaddr:
                continue
            value = str(sockaddr[0])
            if value not in resolved:
                resolved.append(value)
        if not resolved:
            raise CodexBrowserToolError(f"public hostname returned no addresses: {host}")
        synthetic_only = all(
            ipaddress.ip_address(value.split("%", 1)[0]) in _SYNTHETIC_PROXY_NETWORK
            for value in resolved
        )
        verified = list(self._doh_resolver(host)) if synthetic_only else resolved
        if not verified:
            raise CodexBrowserToolError(f"public hostname returned no verified addresses: {host}")
        addresses = tuple(self._validate_ip(value) for value in verified)
        self._cache[cache_key] = (now, addresses)
        return addresses

    def validate_public_url(self, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw or len(raw) > 4096:
            raise CodexBrowserToolError("url must be a non-empty HTTP(S) URL")
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            raise CodexBrowserToolError("only public HTTP(S) URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise CodexBrowserToolError("URLs containing credentials are blocked")
        host = (parsed.hostname or "").strip().rstrip(".").casefold()
        if not host:
            raise CodexBrowserToolError("URL hostname is required")
        try:
            port = parsed.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise CodexBrowserToolError("URL port is invalid") from exc
        self.resolve_public_endpoint(host, port)
        return raw

    def resolve_public_endpoint(self, host: str, port: int) -> tuple[str, ...]:
        """Return validated public IPs for the exact web endpoint the proxy will dial."""
        normalized_host = str(host or "").strip().rstrip(".").casefold()
        if not normalized_host:
            raise CodexBrowserToolError("URL hostname is required")
        if normalized_host in _LOCAL_HOSTNAMES or normalized_host.endswith(_LOCAL_SUFFIXES):
            raise CodexBrowserToolError("local hostnames are blocked")
        try:
            normalized_port = int(port)
        except (TypeError, ValueError) as exc:
            raise CodexBrowserToolError("URL port is invalid") from exc
        if normalized_port not in _PUBLIC_WEB_PORTS:
            raise CodexBrowserToolError("only standard public web ports 80 and 443 are allowed")
        return self._resolve_public(normalized_host, normalized_port)

    def validate_request(
        self,
        url: str,
        *,
        method: str = "GET",
        allowed_file_roots: Iterable[Path] = (),
    ) -> None:
        normalized_method = str(method or "GET").upper()
        if normalized_method not in _READ_ONLY_METHODS:
            raise CodexBrowserToolError(f"browser request method is blocked: {normalized_method}")
        scheme = urlsplit(str(url or "")).scheme.lower()
        if scheme in _PASSIVE_SCHEMES:
            return
        if scheme == "file":
            file_path = _file_path_from_url(url)
            if any(_is_within(file_path, root) for root in allowed_file_roots):
                return
            raise CodexBrowserToolError("local file request is outside the current chat scope")
        self.validate_public_url(url)


class CodexBrowserToolService:
    """Execute the small browser namespace exposed through dynamic tools."""

    def __init__(
        self,
        *,
        enabled: Optional[bool] = None,
        max_concurrency: Optional[int] = None,
        resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
        doh_resolver: Optional[Callable[[str], Sequence[str]]] = None,
    ) -> None:
        self.enabled = _as_bool(
            os.getenv("CODEX_BROWSER_TOOL_ENABLED") if enabled is None else enabled,
            True,
        )
        concurrency = _bounded_int(
            max_concurrency
            if max_concurrency is not None
            else os.getenv("CODEX_BROWSER_MAX_CONCURRENCY"),
            default=1,
            minimum=1,
            maximum=4,
        )
        self.max_concurrency = concurrency
        self._slots = threading.BoundedSemaphore(concurrency)
        self._resolver = resolver
        self._doh_resolver = doh_resolver

    @staticmethod
    def dynamic_tool_specs() -> list[dict[str, Any]]:
        return [
            {
                "type": "namespace",
                "name": _BROWSER_NAMESPACE,
                "description": (
                    "Ephemeral, read-only Playwright Chromium for public web pages. "
                    "It has no saved login, blocks private/LAN/Tailscale addresses and write "
                    "requests, and stores browsing data only in this chat's temporary scope."
                ),
                "tools": [
                    {
                        "type": "function",
                        "name": "open",
                        "description": (
                            "Open a public HTTP(S) URL in Chromium, run page JavaScript, and save "
                            "the rendered DOM, visible text, and captured JSON responses for local "
                            "analysis. Use this when ordinary web search cannot see a dynamic list."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "url": {"type": "string", "description": "Public HTTP(S) URL."},
                                "wait_until": {
                                    "type": "string",
                                    "enum": ["domcontentloaded", "load", "networkidle"],
                                    "default": "domcontentloaded",
                                },
                                "wait_ms": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 15000,
                                    "default": 3000,
                                },
                                "wait_for_selector": {
                                    "type": "string",
                                    "description": "Optional CSS selector to await after navigation.",
                                },
                                "scroll_to_end": {"type": "boolean", "default": True},
                                "capture_json": {"type": "boolean", "default": True},
                                "capture_screenshot": {"type": "boolean", "default": False},
                            },
                            "required": ["url"],
                        },
                    },
                    {
                        "type": "function",
                        "name": "fetch_json_pages",
                        "description": (
                            "Fetch up to 25 same-origin public JSON page URLs in one ephemeral "
                            "Chromium session and save each exact response for local analysis. "
                            "Use this after open reveals a paginated JSON API; it is faster and "
                            "less prone to pagination drift than many separate open calls."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "urls": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 25,
                                    "items": {"type": "string"},
                                    "description": "Same-origin public JSON page URLs in fetch order.",
                                },
                                "wait_ms": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 3000,
                                    "default": 0,
                                },
                            },
                            "required": ["urls"],
                        },
                    },
                    {
                        "type": "function",
                        "name": "render_html",
                        "description": (
                            "Render an HTML file from the current chat workspace with Chromium. "
                            "Remote assets may come only from public HTTP(S) hosts. Produces a PDF "
                            "and/or full-page PNG in the chat output directory for delivery."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "html_path": {
                                    "type": "string",
                                    "description": "Absolute runtime path to an HTML file in this chat scope.",
                                },
                                "output_stem": {
                                    "type": "string",
                                    "description": "Safe base filename for rendered output.",
                                },
                                "pdf": {"type": "boolean", "default": True},
                                "screenshot": {"type": "boolean", "default": False},
                                "landscape": {"type": "boolean", "default": False},
                                "paper_format": {
                                    "type": "string",
                                    "enum": ["A4", "Letter", "Legal"],
                                    "default": "A4",
                                },
                                "wait_ms": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 30000,
                                    "default": 3000,
                                },
                                "optimize_images": {
                                    "type": "boolean",
                                    "default": True,
                                    "description": (
                                        "Use catalog-sized variants of supported public cover images "
                                        "to keep large PDF renders reliable."
                                    ),
                                },
                            },
                            "required": ["html_path"],
                        },
                    },
                ],
            }
        ]

    def tool_signature(self) -> str:
        payload = {
            "policy": _BROWSER_TOOL_POLICY_VERSION,
            "enabled": self.enabled,
            "tools": self.dynamic_tool_specs() if self.enabled else [],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def status(self) -> Dict[str, Any]:
        package_ready = importlib.util.find_spec("playwright") is not None
        return {
            "enabled": self.enabled,
            "package_ready": package_ready,
            "max_concurrency": self.max_concurrency,
            "policy": _BROWSER_TOOL_POLICY_VERSION,
            "public_http_only": True,
            "private_network_blocked": True,
            "allowed_public_ports": sorted(_PUBLIC_WEB_PORTS),
            "write_requests_blocked": True,
            "ephemeral_context": True,
            "synthetic_dns_validation": "https_doh",
            "dns_rebinding_protection": "pinned_loopback_proxy",
            "render_image_optimization": True,
        }

    def execute(
        self,
        *,
        namespace: Any,
        tool: Any,
        arguments: Any,
        context: BrowserToolContext,
    ) -> Dict[str, Any]:
        if not self.enabled:
            raise CodexBrowserToolError("browser tools are disabled by the administrator")
        normalized_namespace = str(namespace or "").strip()
        normalized_tool = str(tool or "").strip()
        if not normalized_namespace and normalized_tool.startswith(f"{_BROWSER_NAMESPACE}."):
            normalized_namespace, normalized_tool = normalized_tool.split(".", 1)
        if normalized_namespace != _BROWSER_NAMESPACE:
            raise CodexBrowserToolError("unknown dynamic tool namespace")
        if not isinstance(arguments, dict):
            raise CodexBrowserToolError("browser tool arguments must be an object")
        if not self._slots.acquire(timeout=5):
            raise CodexBrowserToolError("browser capacity is busy; retry shortly")
        try:
            if normalized_tool == "open":
                return self._open(arguments, context)
            if normalized_tool == "fetch_json_pages":
                return self._fetch_json_pages(arguments, context)
            if normalized_tool == "render_html":
                return self._render_html(arguments, context)
            raise CodexBrowserToolError(f"unknown browser tool: {normalized_tool}")
        finally:
            self._slots.release()

    def _call_directory(
        self,
        context: BrowserToolContext,
        *,
        tool: str,
        discriminator: str,
    ) -> Path:
        scratch_root = context.scratch_root
        if _is_link_like(scratch_root) or not scratch_root.is_dir():
            raise CodexBrowserToolError("browser scratch directory is unsafe")
        if not _is_within(scratch_root, context.request_dir):
            raise CodexBrowserToolError("browser scratch directory escaped the request scope")
        digest = hashlib.sha256(discriminator.encode("utf-8")).hexdigest()[:16]
        call_dir = scratch_root / f"{_safe_stem(tool, 'tool')}-{digest}"
        try:
            call_dir.mkdir()
        except FileExistsError as exc:
            raise CodexBrowserToolError("duplicate browser tool call was rejected") from exc
        if _is_link_like(call_dir) or not _is_within(call_dir, scratch_root):
            raise CodexBrowserToolError("browser call directory is unsafe")
        return call_dir

    def _install_network_guard(
        self,
        browser_context: Any,
        guard: PublicUrlGuard,
        *,
        allowed_file_roots: Iterable[Path] = (),
        optimize_images: bool = False,
    ) -> Dict[str, Any]:
        state: Dict[str, Any] = {"seen": 0, "blocked": [], "optimized_images": 0}
        roots = tuple(allowed_file_roots)
        max_requests = _bounded_int(
            os.getenv("CODEX_BROWSER_MAX_REQUESTS"),
            default=3000,
            minimum=100,
            maximum=10000,
        )

        def route_request(route: Any) -> None:
            request = route.request
            state["seen"] += 1
            try:
                if state["seen"] > max_requests:
                    raise CodexBrowserToolError("page request limit exceeded")
                guard.validate_request(
                    request.url,
                    method=request.method,
                    allowed_file_roots=roots,
                )
            except CodexBrowserToolError as exc:
                if len(state["blocked"]) < 100:
                    state["blocked"].append(
                        {
                            "url": str(request.url)[:1000],
                            "method": str(request.method),
                            "reason": str(exc),
                        }
                    )
                try:
                    route.abort("blockedbyclient")
                except Exception:
                    logger.debug("Could not abort blocked browser request", exc_info=True)
                return
            outbound_url = (
                _optimized_public_image_url(request.url)
                if optimize_images and str(request.method).upper() == "GET"
                else request.url
            )
            if outbound_url != request.url:
                state["optimized_images"] += 1
                route.continue_(url=outbound_url)
            else:
                route.continue_()

        browser_context.route("**/*", route_request)
        route_web_socket = getattr(browser_context, "route_web_socket", None)
        if callable(route_web_socket):
            try:
                # A routed socket never connects upstream unless the handler
                # explicitly calls connect_to_server(). Leave it mocked instead
                # of calling the synchronous close() inside Playwright's route
                # callback: that can strand its dispatcher in a GIL-holding spin
                # while a browser context is closing.
                # https://playwright.dev/python/docs/api/class-websocketroute
                route_web_socket("**/*", lambda websocket: None)
            except Exception:
                logger.debug("Could not install WebSocket blocker", exc_info=True)
        return state

    @staticmethod
    def _scroll_page(page: Any) -> None:
        stable_rounds = 0
        previous_height = 0
        for _ in range(20):
            height = int(page.evaluate("Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"))
            page.evaluate("window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight))")
            page.wait_for_timeout(350)
            new_height = int(page.evaluate("Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"))
            stable_rounds = stable_rounds + 1 if new_height == height == previous_height else 0
            previous_height = new_height
            if stable_rounds >= 2:
                break
        page.evaluate("window.scrollTo(0, 0)")

    def _open(self, arguments: Dict[str, Any], context: BrowserToolContext) -> Dict[str, Any]:
        guard = PublicUrlGuard(
            resolver=self._resolver,
            doh_resolver=self._doh_resolver,
        )
        target_url = guard.validate_public_url(arguments.get("url"))
        wait_until = str(arguments.get("wait_until") or "domcontentloaded")
        if wait_until not in {"domcontentloaded", "load", "networkidle"}:
            raise CodexBrowserToolError("wait_until is invalid")
        wait_ms = _bounded_int(arguments.get("wait_ms"), default=3000, minimum=0, maximum=15000)
        selector = str(arguments.get("wait_for_selector") or "").strip()
        if len(selector) > 500:
            raise CodexBrowserToolError("wait_for_selector is too long")
        scroll_to_end = _as_bool(arguments.get("scroll_to_end"), True)
        capture_json = _as_bool(arguments.get("capture_json"), True)
        capture_screenshot = _as_bool(arguments.get("capture_screenshot"), False)
        call_dir = self._call_directory(
            context,
            tool="open",
            discriminator=f"{time.time_ns()}:{target_url}",
        )

        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise CodexBrowserToolError(
                "Playwright is unavailable; repair Playwright Chromium in System Tools"
            ) from exc

        responses: list[dict[str, Any]] = []
        captured_total = 0
        max_body_bytes = _bounded_int(
            os.getenv("CODEX_BROWSER_MAX_JSON_RESPONSE_BYTES"),
            default=8 * 1024 * 1024,
            minimum=64 * 1024,
            maximum=32 * 1024 * 1024,
        )
        max_total_bytes = _bounded_int(
            os.getenv("CODEX_BROWSER_MAX_CAPTURE_BYTES"),
            default=32 * 1024 * 1024,
            minimum=1024 * 1024,
            maximum=128 * 1024 * 1024,
        )
        max_json_responses = _bounded_int(
            os.getenv("CODEX_BROWSER_MAX_JSON_RESPONSES"),
            default=80,
            minimum=1,
            maximum=300,
        )
        timeout_ms = _bounded_int(
            os.getenv("CODEX_BROWSER_NAVIGATION_TIMEOUT_MS"),
            default=90000,
            minimum=5000,
            maximum=180000,
        )

        with PinnedPublicProxy(guard.resolve_public_endpoint) as public_proxy, sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                raise CodexBrowserToolError(
                    "Chromium could not start; repair Playwright Chromium in System Tools"
                ) from exc
            try:
                browser_context = browser.new_context(
                    accept_downloads=False,
                    service_workers="block",
                    locale="en-US",
                    viewport={"width": 1440, "height": 1000},
                    proxy=public_proxy.playwright_settings,
                )
                network_state = self._install_network_guard(browser_context, guard)
                page = browser_context.new_page()
                page.set_default_timeout(timeout_ms)
                page.set_default_navigation_timeout(timeout_ms)

                def record_response(response: Any) -> None:
                    nonlocal captured_total
                    if len(responses) >= 2000:
                        return
                    content_type = str(response.headers.get("content-type", ""))
                    item: Dict[str, Any] = {
                        "url": str(response.url)[:4096],
                        "status": int(response.status),
                        "content_type": content_type[:300],
                    }
                    is_json = "json" in content_type.casefold()
                    if capture_json and is_json and sum("path" in value for value in responses) < max_json_responses:
                        try:
                            declared = int(response.headers.get("content-length", "0") or 0)
                        except (TypeError, ValueError):
                            declared = 0
                        if declared <= max_body_bytes and captured_total < max_total_bytes:
                            try:
                                body = response.body()
                                item["bytes"] = len(body)
                                if (
                                    0 < len(body) <= max_body_bytes
                                    and captured_total + len(body) <= max_total_bytes
                                ):
                                    index = sum("path" in value for value in responses) + 1
                                    destination = call_dir / f"response-{index:03d}.json"
                                    destination.write_bytes(body)
                                    captured_total += len(body)
                                    item["path"] = _runtime_path(destination, context)
                            except Exception as exc:
                                item["capture_error"] = str(exc)[:500]
                    responses.append(item)

                page.on("response", record_response)
                main_response = page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
                if selector:
                    page.wait_for_selector(selector, timeout=timeout_ms)
                if wait_ms:
                    page.wait_for_timeout(wait_ms)
                if scroll_to_end:
                    self._scroll_page(page)
                    page.wait_for_timeout(min(wait_ms, 3000))

                try:
                    body_text = page.locator("body").inner_text(timeout=10000)
                except Exception:
                    body_text = ""
                page_html = page.content()
                max_text_chars = _bounded_int(
                    os.getenv("CODEX_BROWSER_MAX_TEXT_CHARS"),
                    default=5_000_000,
                    minimum=100_000,
                    maximum=20_000_000,
                )
                if len(body_text) > max_text_chars:
                    body_text = body_text[:max_text_chars] + "\n[truncated by browser tool]"
                if len(page_html) > max_text_chars * 2:
                    page_html = page_html[: max_text_chars * 2] + "\n<!-- truncated by browser tool -->"

                html_path = call_dir / "page.html"
                text_path = call_dir / "page.txt"
                network_path = call_dir / "network.json"
                html_path.write_text(page_html, encoding="utf-8")
                text_path.write_text(body_text, encoding="utf-8")
                network_path.write_text(
                    json.dumps(responses, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                files: Dict[str, str] = {
                    "html": _runtime_path(html_path, context),
                    "text": _runtime_path(text_path, context),
                    "network": _runtime_path(network_path, context),
                }
                warnings: list[str] = []
                if capture_screenshot:
                    screenshot_path = call_dir / "page.png"
                    try:
                        page.screenshot(path=str(screenshot_path), full_page=True)
                        files["screenshot"] = _runtime_path(screenshot_path, context)
                    except Exception as exc:
                        warnings.append(f"full-page screenshot failed: {str(exc)[:300]}")

                result = {
                    "tool": f"{_BROWSER_NAMESPACE}.open",
                    "requested_url": target_url,
                    "final_url": page.url,
                    "status": int(main_response.status) if main_response is not None else None,
                    "title": page.title(),
                    "text_chars": len(body_text),
                    "html_chars": len(page_html),
                    "request_count": int(network_state["seen"]),
                    "blocked_request_count": len(network_state["blocked"]),
                    "blocked_requests": network_state["blocked"][:20],
                    "pinned_proxy": public_proxy.stats,
                    "json_response_count": sum("path" in item for item in responses),
                    "json_responses": [item for item in responses if "path" in item],
                    "files": files,
                    "preview": body_text[:12000],
                    "warnings": warnings,
                }
                metadata_path = call_dir / "metadata.json"
                metadata_path.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                result["files"]["metadata"] = _runtime_path(metadata_path, context)
                return result
            except PlaywrightTimeoutError as exc:
                raise CodexBrowserToolError(f"browser navigation timed out: {str(exc)[:500]}") from exc
            except PlaywrightError as exc:
                raise CodexBrowserToolError(f"browser navigation failed: {str(exc)[:500]}") from exc
            except SafeBrowserProxyError as exc:
                raise CodexBrowserToolError(f"public browser proxy failed: {str(exc)[:500]}") from exc
            finally:
                try:
                    browser.close()
                except Exception:
                    logger.debug("Could not close Chromium cleanly", exc_info=True)

    def _fetch_json_pages(
        self,
        arguments: Dict[str, Any],
        context: BrowserToolContext,
    ) -> Dict[str, Any]:
        raw_urls = arguments.get("urls")
        if not isinstance(raw_urls, list) or not 1 <= len(raw_urls) <= 25:
            raise CodexBrowserToolError("urls must contain between 1 and 25 JSON page URLs")
        guard = PublicUrlGuard(
            resolver=self._resolver,
            doh_resolver=self._doh_resolver,
        )
        target_urls = [guard.validate_public_url(value) for value in raw_urls]
        if len(set(target_urls)) != len(target_urls):
            raise CodexBrowserToolError("duplicate JSON page URLs are not allowed")
        origins = {
            (
                parsed.scheme.casefold(),
                (parsed.hostname or "").casefold(),
                parsed.port or (443 if parsed.scheme.casefold() == "https" else 80),
            )
            for target in target_urls
            for parsed in (urlsplit(target),)
        }
        if len(origins) != 1:
            raise CodexBrowserToolError("all JSON page URLs must use the same public origin")
        wait_ms = _bounded_int(arguments.get("wait_ms"), default=0, minimum=0, maximum=3000)
        call_dir = self._call_directory(
            context,
            tool="json-pages",
            discriminator=f"{time.time_ns()}:{'|'.join(target_urls)}",
        )
        max_body_bytes = _bounded_int(
            os.getenv("CODEX_BROWSER_MAX_JSON_RESPONSE_BYTES"),
            default=8 * 1024 * 1024,
            minimum=64 * 1024,
            maximum=32 * 1024 * 1024,
        )
        max_total_bytes = _bounded_int(
            os.getenv("CODEX_BROWSER_MAX_CAPTURE_BYTES"),
            default=32 * 1024 * 1024,
            minimum=1024 * 1024,
            maximum=128 * 1024 * 1024,
        )
        timeout_ms = _bounded_int(
            os.getenv("CODEX_BROWSER_NAVIGATION_TIMEOUT_MS"),
            default=90000,
            minimum=5000,
            maximum=180000,
        )

        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise CodexBrowserToolError(
                "Playwright is unavailable; repair Playwright Chromium in System Tools"
            ) from exc

        captured_total = 0
        items: list[dict[str, Any]] = []
        with PinnedPublicProxy(
            guard.resolve_public_endpoint
        ) as public_proxy, sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                raise CodexBrowserToolError(
                    "Chromium could not start; repair Playwright Chromium in System Tools"
                ) from exc
            try:
                browser_context = browser.new_context(
                    accept_downloads=False,
                    service_workers="block",
                    locale="en-US",
                    viewport={"width": 1280, "height": 720},
                    proxy=public_proxy.playwright_settings,
                )
                network_state = self._install_network_guard(browser_context, guard)
                page = browser_context.new_page()
                page.set_default_timeout(timeout_ms)
                page.set_default_navigation_timeout(timeout_ms)
                for index, target_url in enumerate(target_urls, start=1):
                    response = page.goto(
                        target_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    if response is None:
                        raise CodexBrowserToolError(
                            f"JSON page {index} returned no main response"
                        )
                    if wait_ms:
                        page.wait_for_timeout(wait_ms)
                    body = response.body()
                    if not body or len(body) > max_body_bytes:
                        raise CodexBrowserToolError(
                            f"JSON page {index} exceeded the per-response capture limit"
                        )
                    if captured_total + len(body) > max_total_bytes:
                        raise CodexBrowserToolError("JSON pages exceeded the total capture limit")
                    try:
                        payload = json.loads(body)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise CodexBrowserToolError(
                            f"JSON page {index} did not return valid JSON"
                        ) from exc
                    destination = call_dir / f"page-{index:03d}.json"
                    destination.write_bytes(body)
                    captured_total += len(body)
                    summary: Dict[str, Any] = {}
                    top_level_keys: list[str] = []
                    if isinstance(payload, dict):
                        top_level_keys = [str(key) for key in payload.keys()][:80]
                        for key, value in payload.items():
                            if value is None or isinstance(value, (str, int, float, bool)):
                                summary[str(key)] = value
                            elif isinstance(value, dict) and all(
                                nested is None or isinstance(nested, (str, int, float, bool))
                                for nested in value.values()
                            ):
                                encoded = json.dumps(value, ensure_ascii=False)
                                if len(encoded) <= 2000:
                                    summary[str(key)] = value
                    items.append(
                        {
                            "index": index,
                            "requested_url": target_url,
                            "final_url": response.url,
                            "status": int(response.status),
                            "content_type": str(
                                response.headers.get("content-type", "")
                            )[:300],
                            "bytes": len(body),
                            "path": _runtime_path(destination, context),
                            "json_type": type(payload).__name__,
                            "top_level_keys": top_level_keys,
                            "summary": summary,
                        }
                    )

                result = {
                    "tool": f"{_BROWSER_NAMESPACE}.fetch_json_pages",
                    "origin": next(iter(origins)),
                    "page_count": len(items),
                    "captured_bytes": captured_total,
                    "pages": items,
                    "request_count": int(network_state["seen"]),
                    "blocked_request_count": len(network_state["blocked"]),
                    "blocked_requests": network_state["blocked"][:20],
                    "pinned_proxy": public_proxy.stats,
                }
                metadata_path = call_dir / "metadata.json"
                metadata_path.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                result["metadata_path"] = _runtime_path(metadata_path, context)
                return result
            except PlaywrightTimeoutError as exc:
                raise CodexBrowserToolError(
                    f"JSON page navigation timed out: {str(exc)[:500]}"
                ) from exc
            except PlaywrightError as exc:
                raise CodexBrowserToolError(
                    f"JSON page navigation failed: {str(exc)[:500]}"
                ) from exc
            except SafeBrowserProxyError as exc:
                raise CodexBrowserToolError(
                    f"public browser proxy failed: {str(exc)[:500]}"
                ) from exc
            finally:
                try:
                    browser.close()
                except Exception:
                    logger.debug("Could not close Chromium cleanly", exc_info=True)

    def _resolve_html_path(self, raw_path: Any, context: BrowserToolContext) -> Path:
        candidate = _host_path_from_runtime(raw_path, use_wsl=context.use_wsl)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise CodexBrowserToolError("HTML file does not exist") from exc
        if _is_link_like(candidate) or not resolved.is_file():
            raise CodexBrowserToolError("HTML input must be a regular non-linked file")
        allowed_roots = (context.workdir, context.request_dir)
        if not any(_is_within(resolved, root) for root in allowed_roots):
            raise CodexBrowserToolError("HTML input is outside the current chat scope")
        if resolved.suffix.casefold() not in {".html", ".htm"}:
            raise CodexBrowserToolError("render_html accepts only .html or .htm files")
        max_bytes = _bounded_int(
            os.getenv("CODEX_BROWSER_MAX_HTML_BYTES"),
            default=20 * 1024 * 1024,
            minimum=64 * 1024,
            maximum=100 * 1024 * 1024,
        )
        if resolved.stat().st_size > max_bytes:
            raise CodexBrowserToolError("HTML input is too large")
        return resolved

    @staticmethod
    def _unique_output_path(output_dir: Path, stem: str, suffix: str) -> Path:
        for index in range(1, 1000):
            name = f"{stem}{suffix}" if index == 1 else f"{stem}-{index}{suffix}"
            candidate = output_dir / name
            if not candidate.exists() and not _is_link_like(candidate):
                return candidate
        raise CodexBrowserToolError("could not allocate a unique output filename")

    def _render_html(self, arguments: Dict[str, Any], context: BrowserToolContext) -> Dict[str, Any]:
        html_path = self._resolve_html_path(arguments.get("html_path"), context)
        create_pdf = _as_bool(arguments.get("pdf"), True)
        create_screenshot = _as_bool(arguments.get("screenshot"), False)
        if not create_pdf and not create_screenshot:
            raise CodexBrowserToolError("request PDF and/or screenshot output")
        output_stem = _safe_stem(arguments.get("output_stem"), f"{html_path.stem}-rendered")
        paper_format = str(arguments.get("paper_format") or "A4")
        if paper_format not in {"A4", "Letter", "Legal"}:
            raise CodexBrowserToolError("paper_format is invalid")
        landscape = _as_bool(arguments.get("landscape"), False)
        wait_ms = _bounded_int(arguments.get("wait_ms"), default=3000, minimum=0, maximum=30000)
        optimize_images = _as_bool(arguments.get("optimize_images"), True)
        if _is_link_like(context.output_dir) or not context.output_dir.is_dir():
            raise CodexBrowserToolError("chat output directory is unsafe")
        if not _is_within(context.output_dir, context.request_dir):
            raise CodexBrowserToolError("chat output directory escaped the request scope")
        call_dir = self._call_directory(
            context,
            tool="render",
            discriminator=f"{time.time_ns()}:{html_path}",
        )
        guard = PublicUrlGuard(
            resolver=self._resolver,
            doh_resolver=self._doh_resolver,
        )

        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise CodexBrowserToolError(
                "Playwright is unavailable; repair Playwright Chromium in System Tools"
            ) from exc

        timeout_ms = _bounded_int(
            os.getenv("CODEX_BROWSER_RENDER_TIMEOUT_MS"),
            default=120000,
            minimum=10000,
            maximum=240000,
        )
        with PinnedPublicProxy(guard.resolve_public_endpoint) as public_proxy, sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                raise CodexBrowserToolError(
                    "Chromium could not start; repair Playwright Chromium in System Tools"
                ) from exc
            try:
                browser_context = browser.new_context(
                    accept_downloads=False,
                    service_workers="block",
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 1000},
                    proxy=public_proxy.playwright_settings,
                )
                network_state = self._install_network_guard(
                    browser_context,
                    guard,
                    allowed_file_roots=(context.workdir, context.request_dir),
                    optimize_images=optimize_images,
                )
                page = browser_context.new_page()
                page.set_default_timeout(timeout_ms)
                page.set_default_navigation_timeout(timeout_ms)
                page.goto(html_path.as_uri(), wait_until="domcontentloaded", timeout=timeout_ms)
                page.eval_on_selector_all(
                    "img",
                    "elements => elements.forEach(element => { element.loading = 'eager'; })",
                )
                self._scroll_page(page)
                if wait_ms:
                    page.wait_for_timeout(wait_ms)
                warnings: list[str] = []
                try:
                    page.wait_for_function(
                        "Array.from(document.images).every(image => image.complete)",
                        timeout=min(timeout_ms, max(10000, wait_ms + 30000)),
                    )
                except PlaywrightTimeoutError:
                    warnings.append("some images did not finish loading before the render deadline")
                image_stats = page.evaluate(
                    "({total: document.images.length, loaded: Array.from(document.images).filter(i => i.complete && i.naturalWidth > 0).length})"
                )
                total_images = int((image_stats or {}).get("total") or 0)
                loaded_images = int((image_stats or {}).get("loaded") or 0)
                failed_images = max(0, total_images - loaded_images)
                if failed_images:
                    warnings.append(f"{failed_images} images failed to load before rendering")
                page.emulate_media(media="screen")
                files: Dict[str, str] = {}
                if create_pdf:
                    pdf_path = self._unique_output_path(context.output_dir, output_stem, ".pdf")
                    page.pdf(
                        path=str(pdf_path),
                        format=paper_format,
                        landscape=landscape,
                        print_background=True,
                        prefer_css_page_size=True,
                        margin={"top": "8mm", "right": "8mm", "bottom": "8mm", "left": "8mm"},
                    )
                    files["pdf"] = _runtime_path(pdf_path, context)
                if create_screenshot:
                    screenshot_path = self._unique_output_path(context.output_dir, output_stem, ".png")
                    page.screenshot(path=str(screenshot_path), full_page=True)
                    files["screenshot"] = _runtime_path(screenshot_path, context)

                result = {
                    "tool": f"{_BROWSER_NAMESPACE}.render_html",
                    "source": _runtime_path(html_path, context),
                    "files": files,
                    "image_count": total_images,
                    "loaded_image_count": loaded_images,
                    "failed_image_count": failed_images,
                    "optimized_image_request_count": int(network_state["optimized_images"]),
                    "blocked_request_count": len(network_state["blocked"]),
                    "blocked_requests": network_state["blocked"][:20],
                    "pinned_proxy": public_proxy.stats,
                    "warnings": warnings,
                }
                (call_dir / "metadata.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                return result
            except PlaywrightTimeoutError as exc:
                raise CodexBrowserToolError(f"HTML rendering timed out: {str(exc)[:500]}") from exc
            except PlaywrightError as exc:
                raise CodexBrowserToolError(f"HTML rendering failed: {str(exc)[:500]}") from exc
            except SafeBrowserProxyError as exc:
                raise CodexBrowserToolError(f"public browser proxy failed: {str(exc)[:500]}") from exc
            finally:
                try:
                    browser.close()
                except Exception:
                    logger.debug("Could not close Chromium cleanly", exc_info=True)


_service_lock = threading.Lock()
_service: Optional[CodexBrowserToolService] = None


def get_codex_browser_tool() -> CodexBrowserToolService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = CodexBrowserToolService()
    return _service


__all__ = [
    "BrowserToolContext",
    "CodexBrowserToolError",
    "CodexBrowserToolService",
    "PublicUrlGuard",
    "get_codex_browser_tool",
]
