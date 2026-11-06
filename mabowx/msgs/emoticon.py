"""Resolve a quoted sticker to its original, MD5-verified image.

This is a read-only, bounded fallback for WeChat's missing sticker download UI.
It never reads account keys, writes process memory, or persists XML/CDN URLs.
The result is a *candidate*: the caller MUST compare it with the quoted UI
thumbnail before exposing it to a chat. A display name and reply text do not
prove which conversation owns an XML document found in process memory.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
import hashlib
import http.client
import io
import ipaddress
import math
import ntpath
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
from typing import Any, Iterable
from urllib.parse import urlsplit
import warnings
import xml.etree.ElementTree as ET


_MD5 = re.compile(r"[0-9a-fA-F]{32}\Z")
_COMPACT = re.compile(
    r"[^:\s]+:[0-9]+:[0-9]+:([0-9a-fA-F]{32}):[^:\r\n]*:[0-9]+\Z"
)
_EMOJI_OPEN = re.compile(rb"<emoji\b(?:[^\"'<>]|\"[^\"]*\"|'[^']*')*>")
_MEDIA_HOSTS = frozenset({
    "wxapp.tc.qq.com", "vweixinf.tc.qq.com", "emoji.qpic.cn", "mmbiz.qpic.cn",
})
_EXTENSIONS = {"GIF": ".gif", "PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
# Serialize the expensive scan/download path. Resolved XML is deliberately not
# cached: a later, identical reply can refer to a different message.
_RESOLVE_LOCK = threading.Lock()
_DNS_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="emoticon-dns")
_DNS_SLOTS = threading.BoundedSemaphore(2)


@dataclass(frozen=True)
class EmoticonLimits:
    max_scan_bytes: int = 3 * 1024**3  # Shared by both passes, not per pass.
    chunk_bytes: int = 4 * 1024**2
    max_xml_bytes: int = 64 * 1024
    max_xml_candidates: int = 4096
    max_urls: int = 8
    max_download_bytes: int = 20 * 1024**2
    max_pixels: int = 16_000_000
    max_total_pixels: int = 128_000_000
    max_frames: int = 500

    def __post_init__(self) -> None:
        for value in self.__dict__.values():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("Emoticon limits must be positive integers")
        if self.chunk_bytes > 16 * 1024**2 or self.max_xml_bytes > 1024**2:
            raise ValueError("Emoticon block limits are too large")


@dataclass(frozen=True)
class QuoteEmoticonCandidate:
    resource_md5: str
    svrid: str
    fromusr: str
    chatusr: str
    reference_format: str
    displayname_matches_ui: bool = False
    displayname_present: bool = False

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return self.resource_md5, self.svrid, self.fromusr, self.chatusr


@dataclass(frozen=True)
class QuoteEmoticonResolution:
    status: str
    path: Path | None = None
    resource_md5: str = ""
    svrid: str = ""
    candidate: QuoteEmoticonCandidate | None = None
    audit: dict[str, Any] = field(default_factory=dict)


class EmoticonResolutionError(Exception):
    """A fixed error code; exception text must never contain a CDN URL."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(status)


@dataclass
class _Budget:
    deadline: float
    limits: EmoticonLimits
    scan_bytes: int = 0
    scans: int = 0
    xml_candidates: int = 0

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise EmoticonResolutionError("deadline_exceeded")
        return remaining

    def consume(self, count: int) -> None:
        self.remaining()
        if self.scan_bytes + count > self.limits.max_scan_bytes:
            raise EmoticonResolutionError("scan_limit_exceeded")
        self.scan_bytes += count

    def document(self) -> None:
        self.remaining()
        self.xml_candidates += 1
        if self.xml_candidates > self.limits.max_xml_candidates:
            raise EmoticonResolutionError("candidate_limit_exceeded")


def _xml_root(raw: str | bytes, max_bytes: int) -> ET.Element | None:
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not encoded or len(encoded) > max_bytes:
        return None
    upper = encoded.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        return None
    try:
        return ET.fromstring(encoded)
    except (ET.ParseError, ValueError):
        return None


def _single_text(node: ET.Element, name: str) -> str | None:
    matches = node.findall(name)
    if len(matches) != 1 or len(matches[0]):
        return None
    return matches[0].text or ""


def _parse_quote_document(
    raw: str | bytes, content: str, quote_nickname: str, max_bytes: int,
) -> tuple[QuoteEmoticonCandidate, tuple[str, ...]] | None:
    root = _xml_root(raw, max_bytes)
    if root is None:
        return None
    if root.tag == "msg":
        nodes = root.findall("appmsg")
        if len(nodes) != 1:
            return None
        root = nodes[0]
    if root.tag != "appmsg":
        return None
    title = _single_text(root, "title")
    # QuoteMessage strips the UIA reply's edges, including the U+2005 that
    # WeChat can append after a mention. Apply the same normalization to both
    # inputs; preserve every internal character. The collector still checks
    # every equivalent title and rejects different reference identities.
    reply_title = content.strip()
    if title is None or not reply_title or title.strip() != reply_title:
        return None
    refs = root.findall("refermsg")
    if len(refs) != 1:
        raise EmoticonResolutionError("invalid_reference")
    ref = refs[0]
    # WeChat's XML uses a contact nickname while UIA can expose a remark or
    # group alias. Self-authored references may have an empty displayname.
    # Never filter candidates with this field: doing so can hide ambiguity.
    if len(ref.findall("displayname")) > 1:
        raise EmoticonResolutionError("invalid_reference")
    displayname = _single_text(ref, "displayname") or ""
    if _single_text(root, "type") != "57" or _single_text(ref, "type") != "47":
        raise EmoticonResolutionError("unsupported_reference")
    svrid = _single_text(ref, "svrid") or ""
    fromusr = _single_text(ref, "fromusr") or ""
    chatusr = _single_text(ref, "chatusr") or ""
    if (not re.fullmatch(r"[1-9][0-9]{0,19}", svrid)
            or not fromusr or not chatusr or max(len(fromusr), len(chatusr)) > 256):
        raise EmoticonResolutionError("invalid_reference")
    original = _single_text(ref, "content")
    if original is None:
        raise EmoticonResolutionError("invalid_reference")
    original = original.strip()
    nested = _xml_root(original, max_bytes)
    urls: tuple[str, ...] = ()
    if nested is not None:
        emojis = list(nested.iter("emoji"))
        if nested.tag not in {"msg", "emoji"} or len(emojis) != 1:
            raise EmoticonResolutionError("invalid_reference")
        emoji = emojis[0]
        md5 = emoji.get("md5", "")
        if not _MD5.fullmatch(md5):
            raise EmoticonResolutionError("invalid_reference")
        md5 = md5.lower()
        url = emoji.get("cdnurl", "")
        if is_allowed_emoticon_url(url):
            urls = (url,)
        reference_format = "emoji_xml"
    else:
        compact = _COMPACT.fullmatch(original)
        if compact is None:
            raise EmoticonResolutionError("unsupported_reference")
        md5 = compact.group(1).lower()
        reference_format = "compact_emotion_reference"
    return QuoteEmoticonCandidate(
        md5, svrid, fromusr, chatusr, reference_format,
        displayname_matches_ui=bool(displayname and quote_nickname and displayname == quote_nickname),
        displayname_present=bool(displayname),
    ), urls


def parse_quote_reference(
    raw: str | bytes, *, content: str, quote_nickname: str,
    max_xml_bytes: int = 64 * 1024,
) -> QuoteEmoticonCandidate | None:
    """Match reply titles after the same edge trimming used by UIA parsing.

    Nonmatching documents return None. A matching but unsupported or malformed
    reference raises a fixed-code EmoticonResolutionError. Displayname matching
    is only metadata because UI remarks and XML nicknames can differ. Internal
    whitespace and punctuation remain significant; this is not fuzzy matching.
    """
    parsed = _parse_quote_document(raw, content, quote_nickname, max_xml_bytes)
    return parsed[0] if parsed else None


def is_allowed_emoticon_url(url: str) -> bool:
    """Accept only exact Tencent media hosts and their scheme's default port."""
    if (not isinstance(url, str) or not url or len(url) > 8192 or "\\" in url
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)):
        return False
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in _MEDIA_HOSTS
            and parsed.username is None and parsed.password is None
            and not parsed.fragment
            and parsed.port in {None, 443 if parsed.scheme == "https" else 80}
        )
    except ValueError:
        return False


def _overlap_blocks(
    chunks: Iterable[tuple[bytes, bool]], overlap: int, budget: _Budget,
) -> Iterable[bytes]:
    tail = b""
    for data, contiguous in chunks:
        budget.remaining()
        block = (tail if contiguous else b"") + data
        tail = block[-overlap:]
        yield block


def _matching_documents(data: bytes, limit: int, budget: _Budget):
    # Look for document starts, not occurrences of a potentially one-character
    # reply. Searching backwards 64KiB at every short-title hit is quadratic.
    # Comparing parsed titles also accepts CDATA and numeric/XML entities.
    start = data.find(b"<appmsg")
    while start >= 0:
        budget.remaining()
        end = data.find(b"</appmsg>", start, min(len(data), start + limit))
        if end >= 0:
            yield data[start:end + 9]
        start = data.find(b"<appmsg", start + 7)


def _collect_references(reader, content: str, nickname: str, budget: _Budget):
    candidates: dict[tuple[str, str, str, str], QuoteEmoticonCandidate] = {}
    urls: set[str] = set()
    for block in _overlap_blocks(reader.chunks(budget), budget.limits.max_xml_bytes, budget):
        for raw in _matching_documents(block, budget.limits.max_xml_bytes, budget):
            parsed = _parse_quote_document(raw, content, nickname, budget.limits.max_xml_bytes)
            if parsed is None:
                continue
            budget.document()
            candidate, found_urls = parsed
            candidates[candidate.identity] = candidate
            if len(candidates) > 1:
                raise EmoticonResolutionError("ambiguous_reference")
            urls.update(found_urls)
            if len(urls) > budget.limits.max_urls:
                raise EmoticonResolutionError("candidate_limit_exceeded")
    # A full pass is required even after finding a match. Otherwise a later
    # region could contain another conversation's equally plausible reply.
    budget.remaining()
    if not candidates:
        raise EmoticonResolutionError("reference_not_found")
    return next(iter(candidates.values())), urls


def _collect_cdn_urls(reader, resource_md5: str, budget: _Budget) -> set[str]:
    urls: set[str] = set()
    needles = (resource_md5.encode("ascii"), resource_md5.upper().encode("ascii"))
    for block in _overlap_blocks(reader.chunks(budget), budget.limits.max_xml_bytes, budget):
        seen: set[int] = set()
        for needle in needles:
            position = block.find(needle)
            while position >= 0:
                budget.remaining()
                start = block.rfind(b"<emoji", max(0, position - 16384), position + 1)
                if start >= 0 and start not in seen:
                    seen.add(start)
                    budget.document()
                    match = _EMOJI_OPEN.match(block, start, min(len(block), start + 16384))
                    if match is not None:
                        raw = match.group()
                        if not raw.endswith(b"/>"):
                            raw += b"</emoji>"
                        root = _xml_root(raw, 16384)
                        if root is not None and root.get("md5", "").lower() == resource_md5:
                            url = root.get("cdnurl", "")
                            if is_allowed_emoticon_url(url):
                                urls.add(url)
                                if len(urls) > budget.limits.max_urls:
                                    raise EmoticonResolutionError("candidate_limit_exceeded")
                position = block.find(needle, position + len(needle))
    budget.remaining()
    return urls


def _process_identity(pid: int) -> tuple[int, float]:
    import psutil

    process = psutil.Process(pid)
    if (process.name().casefold() != "weixin.exe"
            or ntpath.basename(process.exe()).casefold() != "weixin.exe"
            or any(arg.startswith("--type=") for arg in process.cmdline())
            or not process.is_running()):
        raise EmoticonResolutionError("process_identity_mismatch")
    return pid, process.create_time()


class _WindowsMemoryReader:
    """A process handle with no write, injection, or suspend permissions."""

    def __init__(self, pid: int):
        if sys.platform != "win32":
            raise EmoticonResolutionError("unsupported_platform")
        import ctypes
        from ctypes import wintypes

        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise EmoticonResolutionError("unsupported_platform")
        self.identity = _process_identity(pid)
        self._ctypes = ctypes

        class MemoryInfo(ctypes.Structure):
            _fields_ = [
                ("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD), ("_alignment", wintypes.DWORD),
                ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("_alignment2", wintypes.DWORD),
            ]

        self._memory_info = MemoryInfo
        self._kernel = kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        kernel.VirtualQueryEx.restype = ctypes.c_size_t
        kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        kernel.ReadProcessMemory.restype = wintypes.BOOL
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        self._handle = kernel.OpenProcess(0x0410, False, pid)
        if not self._handle:
            raise EmoticonResolutionError("process_access_denied")
        try:
            self.assert_identity()
        except Exception:
            self.close()
            raise

    def assert_identity(self) -> None:
        from ctypes import wintypes
        exit_code = wintypes.DWORD()
        if (not self._kernel.GetExitCodeProcess(self._handle, self._ctypes.byref(exit_code))
                or exit_code.value != 259 or _process_identity(self.identity[0]) != self.identity):
            raise EmoticonResolutionError("process_identity_changed")

    def chunks(self, budget: _Budget) -> Iterable[tuple[bytes, bool]]:
        c = self._ctypes
        info = self._memory_info()
        address = 0
        budget.scans += 1
        while address < 0x7FFFFFFFFFFF:
            budget.remaining()
            if not self._kernel.VirtualQueryEx(self._handle, address, c.byref(info), c.sizeof(info)):
                # ERROR_INVALID_PARAMETER is Windows' normal end-of-address-space
                # signal. Other failures cannot prove a complete scan.
                if c.get_last_error() != 87:
                    raise EmoticonResolutionError("memory_scan_incomplete")
                break
            base, size = int(info.BaseAddress or 0), int(info.RegionSize)
            if size <= 0 or base + size <= address:
                raise EmoticonResolutionError("memory_scan_incomplete")
            address = base + size
            if not (info.State == 0x1000 and info.Type == 0x20000 and info.Protect in {4, 8, 0x40}):
                continue
            contiguous = False
            for offset in range(0, size, budget.limits.chunk_bytes):
                count = min(budget.limits.chunk_bytes, size - offset)
                budget.consume(count)
                buffer = c.create_string_buffer(count)
                received = c.c_size_t()
                ok = self._kernel.ReadProcessMemory(self._handle, base + offset, buffer, count, c.byref(received))
                budget.remaining()
                if not ok or received.value != count:
                    # Pages may change during a live scan; silently skipping
                    # them would allow an unseen conflicting quote.
                    raise EmoticonResolutionError("memory_scan_incomplete")
                yield buffer.raw, contiguous
                contiguous = True
        self.assert_identity()

    def close(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


def _public_address(host: str, port: int, budget: _Budget) -> str:
    # getaddrinfo has no portable deadline. Limit its outstanding workers as
    # well as the caller's wait, so a stalled resolver cannot accumulate work.
    if not _DNS_SLOTS.acquire(timeout=budget.remaining()):
        raise EmoticonResolutionError("deadline_exceeded")
    try:
        future = _DNS_POOL.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except Exception:
        _DNS_SLOTS.release()
        raise
    future.add_done_callback(lambda _future: _DNS_SLOTS.release())
    try:
        addresses = future.result(timeout=budget.remaining())
    except FutureTimeout:
        raise EmoticonResolutionError("deadline_exceeded") from None
    ips = [item[4][0] for item in addresses]
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise EmoticonResolutionError("cdn_address_rejected")
    return ips[0]


def _download_original(url: str, budget: _Budget) -> bytes:
    if not is_allowed_emoticon_url(url):
        raise EmoticonResolutionError("cdn_url_rejected")
    parsed = urlsplit(url)
    host = parsed.hostname
    port = 443 if parsed.scheme == "https" else 80
    ip = _public_address(host, port, budget)
    timeout = min(5.0, budget.remaining())
    connection = (
        http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
        if parsed.scheme == "https" else http.client.HTTPConnection(host, port, timeout=timeout)
    )
    # Pin the validated public address while retaining the hostname for Host,
    # TLS SNI and certificate checks. No second DNS lookup or environment proxy.
    connection._create_connection = lambda _address, timeout=None, source_address=None: socket.create_connection(
        (ip, port), min(5.0, budget.remaining()), source_address,
    )
    response = None
    deadline_timer = None
    try:
        connection.connect()
        transport = connection.sock

        def stop_transport() -> None:
            # shutdown interrupts a socket.makefile() reader too; close alone
            # does not interrupt a response that owns another socket reference.
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        deadline_timer = threading.Timer(budget.remaining(), stop_transport)
        deadline_timer.daemon = True
        deadline_timer.start()
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        connection.request("GET", target, headers={"Accept": "image/*", "Connection": "close"})
        if connection.sock is not None:
            connection.sock.settimeout(min(5.0, budget.remaining()))
        response = connection.getresponse()
        if response.status != 200:
            raise EmoticonResolutionError("cdn_http_error")  # Redirects are never followed.
        if response.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise EmoticonResolutionError("cdn_encoding_rejected")
        length = response.getheader("Content-Length")
        if length is not None:
            try:
                expected = int(length)
            except ValueError:
                raise EmoticonResolutionError("cdn_length_invalid") from None
            if expected < 0 or expected > budget.limits.max_download_bytes:
                raise EmoticonResolutionError("download_limit_exceeded")
        parts, size = [], 0
        while True:
            budget.remaining()
            # HTTP/1.0/Connection:close may detach the socket from connection.
            raw_socket = getattr(getattr(response.fp, "raw", None), "_sock", None)
            if raw_socket is not None:
                raw_socket.settimeout(min(5.0, budget.remaining()))
            chunk = response.read1(min(65536, budget.limits.max_download_bytes + 1 - size))
            budget.remaining()
            if not chunk:
                break
            size += len(chunk)
            if size > budget.limits.max_download_bytes:
                raise EmoticonResolutionError("download_limit_exceeded")
            parts.append(chunk)
        return b"".join(parts)
    finally:
        if deadline_timer is not None:
            deadline_timer.cancel()
        if response is not None:
            response.close()
        connection.close()


def _validate_image(data: bytes, resource_md5: str, budget: _Budget) -> dict[str, Any]:
    from PIL import Image

    budget.remaining()
    if not data or len(data) > budget.limits.max_download_bytes:
        raise EmoticonResolutionError("download_limit_exceeded")
    if hashlib.md5(data, usedforsecurity=False).hexdigest() != resource_md5:
        raise EmoticonResolutionError("resource_md5_mismatch")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as original:
                if original.format not in _EXTENSIONS:
                    raise EmoticonResolutionError("unsupported_image_format")
                image_format = original.format
                if original.width * original.height > budget.limits.max_pixels:
                    raise EmoticonResolutionError("image_limit_exceeded")
                original.verify()
            with Image.open(io.BytesIO(data)) as image:
                frames, total_pixels = 0, 0
                dimensions = image.size
                for index in range(budget.limits.max_frames + 1):
                    budget.remaining()
                    try:
                        image.seek(index)
                    except EOFError:
                        break
                    pixels = image.width * image.height
                    total_pixels += pixels
                    if (index >= budget.limits.max_frames or pixels <= 0
                            or pixels > budget.limits.max_pixels
                            or total_pixels > budget.limits.max_total_pixels):
                        raise EmoticonResolutionError("image_limit_exceeded")
                    image.load()
                    frames += 1
                budget.remaining()
        return {"format": image_format.lower(), "extension": _EXTENSIONS[image_format],
                "size": list(dimensions), "frames": frames, "bytes": len(data)}
    except EmoticonResolutionError:
        raise
    except Exception:
        raise EmoticonResolutionError("invalid_image") from None


def _cached_original(directory: Path, md5: str, budget: _Budget):
    for extension in _EXTENSIONS.values():
        path = directory / (md5 + extension)
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as source:
                data = source.read(budget.limits.max_download_bytes + 1)
            info = _validate_image(data, md5, budget)
            if info["extension"] == extension:
                return path, info
        except EmoticonResolutionError as exc:
            if exc.status == "deadline_exceeded":
                raise
        except OSError:
            continue
    return None


def _persist_original(directory: Path, md5: str, data: bytes, extension: str, budget: _Budget) -> Path:
    budget.remaining()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (md5 + extension)
    if target.is_symlink():
        raise EmoticonResolutionError("cache_path_rejected")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".emoticon-", suffix=".tmp", dir=directory, delete=False) as file:
            temporary = Path(file.name)
            file.write(data)
            file.flush()
        budget.remaining()
        os.replace(temporary, target)
        temporary = None
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def resolve_quote_emoticon(
    *, pid: int, content: str, quote_nickname: str, chat_name: str,
    output_dir: Path, timeout: float = 15.0, limits: EmoticonLimits | None = None,
) -> QuoteEmoticonResolution:
    """Find one unambiguous reference and return its verified original image.

    `pid` must come from the message's owning WeChat window. `chat_name` is
    required context, not proof that the process XML belongs to that chat.
    Even `status="ready"` requires the caller's live UI visual verification.
    All expected failures are returned as fixed statuses with no URL leakage.
    """
    started = time.monotonic()
    if (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
            or not isinstance(content, str) or not content.strip() or len(content) > 4096
            or not isinstance(quote_nickname, str) or len(quote_nickname) > 512
            or not isinstance(chat_name, str) or not chat_name or len(chat_name) > 512
            or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60):
        return QuoteEmoticonResolution(status="invalid_request")
    budget = _Budget(started + timeout, limits or EmoticonLimits())
    candidate = None
    reader = None
    acquired = False
    audit: dict[str, Any] = {"requires_ui_verification": True}
    status, result_path = "internal_error", None
    try:
        acquired = _RESOLVE_LOCK.acquire(timeout=budget.remaining())
        if not acquired:
            raise EmoticonResolutionError("deadline_exceeded")
        budget.remaining()
        reader = _WindowsMemoryReader(pid)
        candidate, urls = _collect_references(reader, content, quote_nickname, budget)
        audit.update(displayname_matches_ui=candidate.displayname_matches_ui,
                     displayname_present=candidate.displayname_present)
        reader.assert_identity()
        directory = Path(output_dir).expanduser().resolve()
        cached = _cached_original(directory, candidate.resource_md5, budget)
        if cached:
            result_path, info = cached
            audit.update(info, cache_hit=True)
        else:
            if not urls:
                urls = _collect_cdn_urls(reader, candidate.resource_md5, budget)
            reader.assert_identity()
            if not urls:
                raise EmoticonResolutionError("cdn_not_found")
            last_error = "cdn_unavailable"
            for url in sorted(urls):
                budget.remaining()
                try:
                    data = _download_original(url, budget)
                    info = _validate_image(data, candidate.resource_md5, budget)
                    reader.assert_identity()
                    result_path = _persist_original(directory, candidate.resource_md5, data, info["extension"], budget)
                    audit.update(info, cache_hit=False)
                    break
                except EmoticonResolutionError as exc:
                    if exc.status in {"deadline_exceeded", "process_identity_changed"}:
                        raise
                    last_error = exc.status
                except Exception:
                    # HTTP exceptions can embed signed URLs. Never retain or
                    # stringify them, including in the audit or a logger.
                    last_error = "cdn_unavailable"
            if result_path is None:
                raise EmoticonResolutionError(last_error)
        reader.assert_identity()
        budget.remaining()
        status = "ready"
    except EmoticonResolutionError as exc:
        status = exc.status
        result_path = None
    except Exception:
        status = "resolution_failed"
        result_path = None
    finally:
        if reader is not None:
            reader.close()
        if acquired:
            _RESOLVE_LOCK.release()
    audit.update(scan_bytes=budget.scan_bytes, scans=budget.scans,
                 xml_candidates=budget.xml_candidates, elapsed_ms=round((time.monotonic() - started) * 1000, 1))
    audit.pop("extension", None)
    return QuoteEmoticonResolution(
        status=status, path=result_path,
        resource_md5=candidate.resource_md5 if candidate else "",
        svrid=candidate.svrid if candidate else "", candidate=candidate, audit=audit,
    )
