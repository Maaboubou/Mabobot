"""Managed runtime building blocks for Summary Plus.

The module deliberately contains no platform-specific extraction logic.  It
demonstrates how Runtime API v2 plugins can share bounded work admission,
owned artifacts and explicit migration without creating unmanaged threads.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

from app.services.plugin_runtime import PluginContext, PluginStorage
from app.services.runtime_operations import OperationContext


logger = logging.getLogger(__name__)


class ArtifactLimitError(RuntimeError):
    """Raised when a generated/downloaded file exceeds the plugin policy."""


@dataclass(frozen=True)
class ProcessingResult:
    """Stable result vocabulary used by platform and management surfaces."""

    status: str
    handled: bool
    message: str = ""
    artifact: Optional[str] = None
    fallback: Optional[str] = None

    @classmethod
    def completed(cls, message: str = "处理完成", artifact: Optional[str] = None) -> "ProcessingResult":
        return cls("completed", True, message, artifact)

    @classmethod
    def skipped(cls, message: str, fallback: Optional[str] = None) -> "ProcessingResult":
        return cls("skipped", True, message, fallback=fallback)

    @classmethod
    def failed(cls, message: str) -> "ProcessingResult":
        return cls("failed", True, message)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "handled": self.handled,
            "message": self.message,
            "artifact": self.artifact,
            "fallback": self.fallback,
        }


@dataclass(frozen=True)
class DispatchDecision:
    accepted: bool
    reason: str
    operation_id: Optional[str] = None


def _tree_size(root: Path) -> tuple[int, int]:
    total = 0
    files = 0
    if not root.exists():
        return total, files
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            total += path.stat().st_size
            files += 1
        except OSError:
            continue
    return total, files


from app.services.plugin_config_context import ScopedConfigAttribute
from app.utils.plugin_config import get_config


class ArtifactManager:
    """Own Summary Plus generated files and enforce size/retention policy."""

    @ScopedConfigAttribute
    def max_artifact_bytes(self):
        return max(1, int(get_config("max_artifact_size_mb", 512, plugin_name="summary_plus"))) * 1024 * 1024

    CATEGORIES = {"videos", "images", "mindmaps", "subtitles", "audio", "metadata"}

    def __init__(
        self,
        storage: PluginStorage,
        *,
        max_artifact_size_mb: int = 512,
        quota_mb: int = 8192,
        retention_hours: int = 24,
        legacy_roots: Optional[Iterable[Path]] = None,
    ):
        self.storage = storage
        self.max_artifact_bytes = max(1, int(max_artifact_size_mb)) * 1024 * 1024
        self.quota_bytes = max(1, int(quota_mb)) * 1024 * 1024
        self.retention_seconds = max(1, int(retention_hours)) * 3600
        self.legacy_roots = tuple(Path(item) for item in (legacy_roots or ()))
        self._lock = threading.RLock()
        self.storage.temp_root.mkdir(parents=True, exist_ok=True)

    def category_dir(self, category: str) -> Path:
        normalized = str(category or "").strip().lower()
        if normalized not in self.CATEGORIES:
            raise ValueError(f"未知产物分类: {category}")
        path = self.storage.temp_root / normalized
        path.mkdir(parents=True, exist_ok=True)
        return path

    def category_path(self, category: str, filename: str) -> Path:
        name = Path(str(filename or "")).name
        if not name or name in {".", ".."}:
            raise ValueError("产物文件名无效")
        return self.category_dir(category) / name

    def current_usage(self) -> tuple[int, int]:
        return _tree_size(self.storage.temp_root)

    def assert_capacity(self, incoming_bytes: int = 0) -> None:
        incoming = max(0, int(incoming_bytes or 0))
        if incoming > self.max_artifact_bytes:
            raise ArtifactLimitError(
                f"单个文件超过 {self.max_artifact_bytes // (1024 * 1024)} MB 限制"
            )
        used, _ = self.current_usage()
        if used + incoming > self.quota_bytes:
            raise ArtifactLimitError(
                f"插件临时产物超过 {self.quota_bytes // (1024 * 1024)} MB 配额"
            )

    def validate_file(self, path: str | Path) -> Path:
        candidate = Path(path)
        try:
            resolved = candidate.resolve()
            resolved.relative_to(self.storage.temp_root.resolve())
        except (OSError, ValueError) as exc:
            raise ArtifactLimitError("产物不属于 Summary Plus 托管临时目录") from exc
        if not resolved.is_file():
            raise ArtifactLimitError("产物文件不存在")
        size = resolved.stat().st_size
        if size > self.max_artifact_bytes:
            raise ArtifactLimitError(
                f"单个文件超过 {self.max_artifact_bytes // (1024 * 1024)} MB 限制"
            )
        used, _ = self.current_usage()
        if used > self.quota_bytes:
            raise ArtifactLimitError(
                f"插件临时产物超过 {self.quota_bytes // (1024 * 1024)} MB 配额"
            )
        return resolved

    def cleanup_stale(self, *, now: Optional[float] = None) -> Dict[str, int]:
        cutoff = float(now or time.time()) - self.retention_seconds
        removed_files = 0
        removed_bytes = 0
        with self._lock:
            if not self.storage.temp_root.exists():
                return {"files": 0, "bytes": 0}
            for path in self.storage.temp_root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                    if stat.st_mtime >= cutoff:
                        continue
                    path.unlink()
                    removed_files += 1
                    removed_bytes += stat.st_size
                except OSError:
                    continue
            for path in sorted(
                (item for item in self.storage.temp_root.rglob("*") if item.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                try:
                    path.rmdir()
                except OSError:
                    pass
        return {"files": removed_files, "bytes": removed_bytes}

    def inventory(self) -> Dict[str, Any]:
        used, files = self.current_usage()
        legacy = []
        for root in self.legacy_roots:
            size, count = _tree_size(root)
            if size or count:
                legacy.append({"path": str(root), "bytes": size, "files": count})
        return {
            "managed_bytes": used,
            "managed_files": files,
            "quota_bytes": self.quota_bytes,
            "max_artifact_bytes": self.max_artifact_bytes,
            "retention_hours": self.retention_seconds // 3600,
            "legacy": legacy,
        }


class StorageMigrator:
    """Idempotent, non-destructive migration helpers for legacy plugin state."""

    def __init__(self, storage: PluginStorage, log: logging.Logger = logger):
        self.storage = storage
        self.logger = log
        self.notes: list[str] = []

    def copy_file_once(self, source: Path, destination: Path) -> Path:
        if destination.exists() or not source.is_file():
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".migrating")
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
        self.notes.append(f"已复制旧数据: {source} -> {destination}")
        self.logger.info(self.notes[-1])
        return destination

    def adopt_directory_once(self, source: Path, destination: Path) -> Path:
        """Adopt a legacy directory when an atomic rename is possible.

        Failure intentionally leaves the source untouched and returns it, so
        a locked browser profile never becomes an availability incident.
        """
        if destination.exists():
            return destination
        if not source.is_dir():
            destination.parent.mkdir(parents=True, exist_ok=True)
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(source, destination)
            self.notes.append(f"已接管旧目录: {source} -> {destination}")
            self.logger.info(self.notes[-1])
            return destination
        except OSError as exc:
            message = f"旧目录暂未迁移，将继续使用原路径: {source} ({exc})"
            self.notes.append(message)
            self.logger.warning(message)
            return source


class ManagedDispatcher:
    """Bounded admission and pool-aware execution through PluginContext."""

    def __init__(
        self,
        context: PluginContext,
        *,
        max_pending: int = 12,
        media_workers: int = 2,
        dedup_ttl_seconds: int = 300,
    ):
        self.context = context
        self.max_pending = max(1, min(int(max_pending), 100))
        self.dedup_ttl_seconds = max(1, int(dedup_ttl_seconds))
        self._admission = threading.BoundedSemaphore(self.max_pending)
        self._pools = {
            "browser": threading.BoundedSemaphore(1),
            "media": threading.BoundedSemaphore(max(1, min(int(media_workers), 8))),
            "heavy": threading.BoundedSemaphore(1),
        }
        self._lock = threading.RLock()
        self._inflight: set[str] = set()
        self._recent: Dict[str, float] = {}
        self._closed = False
        self._active = 0
        self._accepted = 0
        self._rejected = 0
        self._deduplicated = 0

    @staticmethod
    def _safe_url(url: str) -> str:
        try:
            parts = urlsplit(str(url or ""))
            return urlunsplit((parts.scheme, parts.netloc, parts.path[:300], "", ""))
        except ValueError:
            return ""

    @staticmethod
    def _dedup_key(kind: str, chat_name: str, url: str) -> str:
        raw = "\x00".join((kind, chat_name, url)).encode("utf-8", errors="replace")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _pool_for(kind: str) -> str:
        normalized = str(kind or "").lower()
        if normalized in {"browser", "web", "summary"}:
            return "browser"
        if normalized in {"bilibili_mindmap", "youtube", "asr", "mindmap"}:
            return "heavy"
        return "media"

    def _reserve(self, key: str) -> DispatchDecision:
        now = time.time()
        with self._lock:
            self._recent = {
                item: timestamp
                for item, timestamp in self._recent.items()
                if now - timestamp < self.dedup_ttl_seconds
            }
            if key in self._inflight or key in self._recent:
                self._deduplicated += 1
                return DispatchDecision(False, "duplicate")
            if self._closed:
                self._rejected += 1
                return DispatchDecision(False, "closed")
            if not self._admission.acquire(blocking=False):
                self._rejected += 1
                return DispatchDecision(False, "queue_full")
            self._inflight.add(key)
            self._accepted += 1
        return DispatchDecision(True, "accepted")

    def submit(
        self,
        kind: str,
        chat_name: str,
        url: str,
        target: Callable[[OperationContext], Any],
        *,
        title: Optional[str] = None,
    ) -> DispatchDecision:
        key = self._dedup_key(kind, chat_name, url)
        reserved = self._reserve(key)
        if not reserved.accepted:
            return reserved
        pool_name = self._pool_for(kind)
        pool = self._pools[pool_name]

        def run(operation: OperationContext) -> Any:
            acquired = False
            try:
                operation.progress(2, "等待可用执行槽", pool=pool_name)
                while not acquired:
                    operation.check_cancelled()
                    acquired = pool.acquire(timeout=0.25)
                with self._lock:
                    self._active += 1
                operation.progress(5, "开始处理")
                result = target(operation)
                operation.progress(98, "处理完成")
                if isinstance(result, ProcessingResult):
                    return result.as_dict()
                return result
            finally:
                if acquired:
                    pool.release()
                with self._lock:
                    self._active = max(0, self._active - 1)
                    self._inflight.discard(key)
                    self._recent[key] = time.time()
                self._admission.release()

        try:
            operation = self.context.tasks.submit(
                kind=f"summary_plus.{kind}",
                title=title or f"Summary Plus · {kind}",
                target=run,
                details={
                    "chat": str(chat_name or "")[:120],
                    "url": self._safe_url(url),
                    "pool": pool_name,
                },
            )
        except Exception:
            with self._lock:
                self._inflight.discard(key)
            self._admission.release()
            raise
        return DispatchDecision(True, "accepted", operation.get("operation_id"))

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "max_pending": self.max_pending,
                "pending": len(self._inflight),
                "active": self._active,
                "accepted": self._accepted,
                "rejected": self._rejected,
                "deduplicated": self._deduplicated,
                "closed": self._closed,
            }
