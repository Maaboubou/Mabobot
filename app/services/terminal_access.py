"""Discover this node's existing Tailscale Serve routes without changing them."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit

from app.utils.subprocess_utils import apply_hidden_process_defaults

_cache: dict[int, tuple[float, frozenset[str]]] = {}
_lock = threading.Lock()
_CACHE_SECONDS = 15
_DNS_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


def origin_key(value):
    """Normalize browser origins, rejecting credentials and URL components."""
    try:
        if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError()
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError()
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, TypeError):
        raise ValueError("invalid origin") from None


def _format_origin(scheme, host, port):
    authority = f"[{host}]" if ":" in host else host
    if port != (443 if scheme == "https" else 80):
        authority += f":{port}"
    return scheme + "://" + authority


def _proxy_matches(value, backend_port):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            return False
        return (parsed.port or 80) == backend_port
    except (ValueError, TypeError, AttributeError):
        return False


def _origins_from_config(status, serve, backend_port):
    if not isinstance(status, dict) or not isinstance(serve, dict) or status.get("BackendState") != "Running":
        return set()
    node = status.get("Self") or {}
    if not isinstance(node, dict):
        return set()
    dns = str(node.get("DNSName") or "").lower().rstrip(".")
    if not _DNS_NAME.fullmatch(dns) or "." not in dns:
        return set()
    aliases = {dns, dns.split(".", 1)[0]}
    hostname = str(node.get("HostName") or "").lower().rstrip(".")
    if _DNS_NAME.fullmatch(hostname):
        aliases.add(hostname)
    for value in node.get("TailscaleIPs") or []:
        try:
            aliases.add(str(ipaddress.ip_address(value)))
        except ValueError:
            pass
    tcp, web = serve.get("TCP") or {}, serve.get("Web") or {}
    if not isinstance(tcp, dict) or not isinstance(web, dict):
        return set()
    origins = set()
    for authority, entry in web.items():
        try:
            _, host, port = origin_key("http://" + authority)
            listener = tcp.get(str(port)) or {}
            if host != dns or not isinstance(entry, dict) or not isinstance(listener, dict):
                continue
            https = listener.get("HTTPS") is True
            http = listener.get("HTTP") is True
            if https == http:
                continue
            handlers = entry.get("Handlers") or {}
            root = handlers.get("/") if isinstance(handlers, dict) else None
            if not isinstance(root, dict) or not _proxy_matches(root.get("Proxy"), backend_port):
                continue
            # HTTPS uses this node's certificate name. HTTP Serve also supports
            # the node's short MagicDNS name and advertised local aliases.
            for alias in {dns} if https else aliases:
                origins.add(_format_origin("https" if https else "http", alias, port))
        except (ValueError, TypeError):
            continue
    return origins


def _tailscale_binary():
    binary = shutil.which("tailscale.exe" if os.name == "nt" else "tailscale")
    if binary:
        return binary
    if os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _read_tailscale_state():
    binary = _tailscale_binary()
    if not binary:
        return {}, {}
    results = []
    for arguments in (("status", "--json"), ("serve", "status", "--json")):
        result = subprocess.run([binary, *arguments], **apply_hidden_process_defaults({
            "capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace",
            "timeout": 4, "check": False,
        }))
        if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
            return {}, {}
        results.append(json.loads(result.stdout))
    return results[0], results[1]


def discover_serve_origins(backend_port):
    """Cache only validated route origins; failures never retain stale trust."""
    if type(backend_port) is not int or not 1 <= backend_port <= 65535:
        return set()
    with _lock:
        now = time.monotonic()
        cached = _cache.get(backend_port)
        if cached and now - cached[0] < _CACHE_SECONDS:
            return set(cached[1])
        try:
            status, serve = _read_tailscale_state()
            result = _origins_from_config(status, serve, backend_port)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
            result = set()
        if len(_cache) >= 16:
            _cache.clear()
        _cache[backend_port] = (time.monotonic(), frozenset(result))
        return result
