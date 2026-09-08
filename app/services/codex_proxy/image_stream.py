"""Opt-in collection of complete image artifacts while a Codex exec is running."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

from PIL import Image

from app.services.codex_job_manager import codex_job_manager
from app.services.codex_proxy.client import (
    _artifact_output_dir_is_safe, _codex_thread_id_from_event,
    _IMAGE_ATTACHMENT_SUFFIXES, _is_valid_image_file,
    _successful_generated_image_paths, _windows_path_for_wsl,
)

logger = logging.getLogger(__name__)


class CodexImageStream:
    """Drain process pipes independently of disk polling and serial image delivery."""

    def __init__(self, client, output_dir: Path, request_id: str,
                 on_image: Callable[[Path], None], poll_seconds: float = 1.0):
        self.client = client
        self.output_dir = output_dir
        self.request_id = request_id
        self.on_image = on_image
        self.poll_seconds = poll_seconds
        self.thread_id = ""
        self.event_paths: set[str] = set()
        self.seen: set[str] = set()
        self.versions: dict[str, tuple[int, int]] = {}
        self.observed: dict[str, tuple[int, int]] = {}
        self.queue = asyncio.Queue()
        self.stop = asyncio.Event()
        self.watcher = None
        self.sender = None

    def event(self, kind, **details):
        codex_job_manager.record_event(self.request_id, kind, details)

    async def communicate(self, proc, prompt: bytes):
        self.sender = asyncio.create_task(self._send())
        self.watcher = asyncio.create_task(self._watch())

        async def write():
            try:
                proc.stdin.write(prompt)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

        async def stdout():
            chunks = []
            while line := await proc.stdout.readline():
                chunks.append(line)
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                tid = _codex_thread_id_from_event(event)
                if tid and not self.thread_id:
                    self.thread_id = tid
                    self.event("image_stream_started", thread_id=tid)
                self.event_paths.update(_successful_generated_image_paths(event))
                if isinstance(event, dict) and event.get("type") in {"item.started", "item.completed"}:
                    item = event.get("item") or {}
                    if isinstance(item, dict):
                        self.event("codex_item_observed", phase=event["type"],
                                   item_type=item.get("type"), item_id=item.get("id"))
            return b"".join(chunks)

        tasks = [asyncio.create_task(coro) for coro in (stdout(), proc.stderr.read(), write(), proc.wait())]
        try:
            results = await asyncio.gather(*tasks)
            return results[0], results[1]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch(self):
        while True:
            try:
                await self.scan()
            except Exception:
                logger.exception("Image stream scan failed for %s; will retry", self.request_id)
            if self.stop.is_set():
                break
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def scan(self):
        # Never scan the whole profile: generated images must belong to this thread.
        sources = []
        if self.thread_id:
            root = await self.client._generated_images_root()
            if root:
                thread_root = root.replace("\\", "/").rstrip("/") + "/" + self.thread_id
                paths = list(await self.client._thread_generated_images(self.thread_id))
                paths.extend(sorted(self.event_paths))
                for raw in dict.fromkeys(path.replace("\\", "/") for path in paths):
                    if not raw.startswith(thread_root + "/") or "/../" in raw:
                        continue
                    host_root = self._host_path(thread_root)
                    sources.append((self._host_path(raw), host_root))
        if _artifact_output_dir_is_safe(self.output_dir):
            sources.extend((p, self.output_dir) for p in self.output_dir.rglob("*")
                           if p.suffix.lower() in _IMAGE_ATTACHMENT_SUFFIXES
                           and not p.name.startswith("stream-"))
        for path, root in sources:
            captured = await asyncio.to_thread(self._snapshot, path, root)
            if captured:
                saved, digest, generated_at = captured
                self.seen.add(digest)
                self.event("image_discovered", path=str(saved), sha256=digest,
                           generated_at=generated_at, discovered_at=time.time())
                self.queue.put_nowait(saved)

    def _host_path(self, raw):
        return (_windows_path_for_wsl(raw) if self.client.use_wsl and os.name == "nt"
                else Path(raw))

    def _snapshot(self, source: Path, root: Path):
        """Wait for stable files, validate a private copy, then retain it by content hash."""
        temp = None
        created = False
        try:
            if (root.is_symlink() or source.is_symlink() or not source.is_file()
                    or not source.resolve().is_relative_to(root.resolve())
                    or not _artifact_output_dir_is_safe(self.output_dir)):
                return None
            stat = source.stat()
            version = (stat.st_size, stat.st_mtime_ns)
            key = str(source)
            if self.versions.get(key) == version:
                return None
            previous = self.observed.get(key)
            self.observed[key] = version
            if previous != version or not 0 < stat.st_size <= 64 * 1024 * 1024:
                return None
            data = source.read_bytes()
            after = source.stat()
            if (after.st_size, after.st_mtime_ns) != version:
                return None
            digest = hashlib.sha256(data).hexdigest()
            if digest in self.seen:
                self.versions[key] = version
                return None
            temp = self.output_dir / (".stream-" + digest + source.suffix.lower())
            # Exclusive creation also rejects pre-existing links in the workspace.
            with temp.open("xb") as stream:
                created = True
                stream.write(data)
            if not _is_valid_image_file(temp):
                return None
            try:
                with Image.open(temp) as image:
                    image.load()
            except (OSError, ValueError):
                return None
            saved = self.output_dir / ("stream-" + digest + source.suffix.lower())
            os.replace(temp, saved)
            self.versions[key] = version
            return saved, digest, stat.st_mtime
        except OSError:
            return None
        finally:
            if created and temp is not None:
                temp.unlink(missing_ok=True)

    async def _send(self):
        while True:
            path = await self.queue.get()
            if path is None:
                return
            try:
                self.event("image_send_started", path=str(path), send_started_at=time.time())
                await asyncio.to_thread(self.on_image, path)
                self.event("image_sent", path=str(path), sent_at=time.time())
            except Exception as exc:
                self.event("image_send_failed", path=str(path), error=str(exc))
                logger.exception("Streaming image delivery failed for %s", self.request_id)

    async def finish(self):
        if self.watcher is None:
            return
        self.stop.set()
        await self.watcher
        # An image written immediately before exit may only have been observed once.
        await asyncio.sleep(self.poll_seconds)
        try:
            await self.scan()
        except Exception:
            logger.exception("Final image stream scan failed for %s", self.request_id)
        self.queue.put_nowait(None)
        await self.sender
        self.watcher = None
