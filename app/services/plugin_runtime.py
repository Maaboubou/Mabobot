"""Plugin runtime protocol v2.

Every plugin receives :class:`PluginContext` from ``register`` and uses owned
storage, managed operations, health probes and cleanup hooks. Runtime v1 is
rejected at manifest validation instead of being loaded through a legacy path.
"""

from __future__ import annotations

import logging
import os
import shutil
from contextvars import copy_context
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional

from app.services.runtime_operations import OperationContext, get_runtime_operation_service


logger = logging.getLogger(__name__)
PLUGIN_RUNTIME_API_VERSION = 2
_health_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="plugin-health")


def _safe_plugin_id(plugin_id: str) -> str:
    normalized = str(plugin_id or "").strip().replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part and part not in {".", ".."}]
    safe = "__".join("".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in part) for part in parts)
    return safe or "plugin"


def _safe_relative(path: str | Path) -> Path:
    text = str(path or "").replace("\\", "/")
    candidate = PurePosixPath(text)
    if candidate.is_absolute() or not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("插件存储路径必须是安全的相对路径")
    return Path(*candidate.parts)


def _directory_size(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    if path.is_file():
        try:
            return path.stat().st_size, 1
        except OSError:
            return 0, 0
    total = 0
    files = 0
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        try:
            total += item.stat().st_size
            files += 1
        except OSError:
            continue
    return total, files


class PluginStorage:
    """Namespaced storage whose lifecycle and backup class are explicit."""

    def __init__(self, plugin_id: str, *, data_root: Path = Path("data/plugins"), temp_root: Path = Path("tmp/plugins")):
        self.plugin_id = plugin_id
        namespace = _safe_plugin_id(plugin_id)
        self.persistent_root = data_root / namespace / "persistent"
        self.generated_root = data_root / namespace / "generated"
        self.cache_root = data_root / namespace / "cache"
        self.machine_bound_root = data_root / namespace / "machine_bound"
        self.temp_root = temp_root / namespace

    @staticmethod
    def _path(root: Path, relative: str | Path, *, create_parent: bool) -> Path:
        resolved = root / _safe_relative(relative)
        if create_parent:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def persistent_path(self, relative: str | Path, *, create_parent: bool = True) -> Path:
        return self._path(self.persistent_root, relative, create_parent=create_parent)

    def cache_path(self, relative: str | Path, *, create_parent: bool = True) -> Path:
        return self._path(self.cache_root, relative, create_parent=create_parent)

    def generated_path(self, relative: str | Path, *, create_parent: bool = True) -> Path:
        """Return reproducible output included only when backups request it."""
        return self._path(self.generated_root, relative, create_parent=create_parent)

    def machine_bound_path(self, relative: str | Path, *, create_parent: bool = True) -> Path:
        """Return host-specific state which is excluded from portable backups.

        Browser profiles, hardware indexes and similar data belong here. They
        may be included only when an operator explicitly requests a
        machine-bound migration backup.
        """
        return self._path(self.machine_bound_root, relative, create_parent=create_parent)

    def temp_path(self, relative: str | Path, *, create_parent: bool = True) -> Path:
        return self._path(self.temp_root, relative, create_parent=create_parent)

    def clear_temporary(self) -> int:
        if not self.temp_root.exists():
            return 0
        _, files = _directory_size(self.temp_root)
        shutil.rmtree(self.temp_root)
        return files

    def migrate_legacy_directory(
        self,
        source: str | Path,
        *,
        storage_class: str,
        relative: str | Path = "files",
    ) -> List[str]:
        """Move a former plugin-local directory into namespaced storage.

        Destination files always win, so startup migration is repeatable and
        never overwrites data already written through Runtime API v2.
        """
        roots = {
            "persistent": self.persistent_root,
            "generated": self.generated_root,
            "cache": self.cache_root,
            "machine_bound": self.machine_bound_root,
            "temporary": self.temp_root,
        }
        if storage_class not in roots:
            raise ValueError(f"未知插件存储类型: {storage_class}")
        source_path = Path(source)
        if not source_path.exists():
            return []
        destination = self._path(roots[storage_class], relative, create_parent=True)
        notes: List[str] = []
        if source_path.is_file():
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source_path, destination)
                notes.append(f"{source_path} -> {destination}")
            return notes
        destination.mkdir(parents=True, exist_ok=True)
        for item in sorted(source_path.rglob("*")):
            if not item.is_file():
                continue
            target = destination / item.relative_to(source_path)
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(item, target)
            notes.append(f"{item} -> {target}")
        directories = [item for item in source_path.rglob("*") if item.is_dir()]
        for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            source_path.rmdir()
        except OSError:
            pass
        return notes

    def inventory(self) -> Dict[str, Any]:
        rows = []
        for storage_class, path, backed_up, portable in (
            ("persistent", self.persistent_root, True, True),
            ("generated", self.generated_root, True, True),
            ("cache", self.cache_root, False, True),
            ("machine_bound", self.machine_bound_root, False, False),
            ("temporary", self.temp_root, False, True),
        ):
            size, files = _directory_size(path)
            rows.append(
                {
                    "class": storage_class,
                    "path": str(path),
                    "bytes": size,
                    "files": files,
                    "backed_up": backed_up,
                    "portable": portable,
                }
            )
        return {"plugin_id": self.plugin_id, "entries": rows}


class OwnerTaskFacade:
    def __init__(self, plugin_id: str):
        self.plugin_id = plugin_id

    @property
    def _service(self):
        return get_runtime_operation_service()

    def submit(
        self,
        kind: str,
        title: str,
        target: Callable[[OperationContext], Any],
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._service.submit(
            owner=f"plugin:{self.plugin_id}",
            kind=kind,
            title=title,
            target=target,
            details=details,
        )

    def list(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self._service.list(limit=limit, owner=f"plugin:{self.plugin_id}")

    def cancel(self, operation_id: str) -> Dict[str, Any]:
        operation = self._service.get(operation_id)
        if not operation or operation.get("owner") != f"plugin:{self.plugin_id}":
            return {"success": False, "message": "操作不属于当前插件"}
        return self._service.cancel(operation_id)

    def active_count(self) -> int:
        return self._service.active_count(owner=f"plugin:{self.plugin_id}")


class ManagedTimerHandle:
    def __init__(self, thread: threading.Thread, cancel_event: threading.Event):
        self._thread = thread
        self._cancel_event = cancel_event

    def cancel(self) -> None:
        self._cancel_event.set()

    def is_alive(self) -> bool:
        return self._thread.is_alive()


class OwnerWorkerFacade:
    """Track plugin-owned helper threads and stop/join them on unload.

    Scheduled loops should also observe their plugin's own stop condition.  The
    runtime stop event gives new plugins a standard cooperative cancellation
    signal and ensures every started worker is visible to the platform.
    """

    def __init__(self, plugin_id: str):
        self.plugin_id = plugin_id
        self.stop_event = threading.Event()
        self._lock = threading.RLock()
        self._threads: Dict[str, threading.Thread] = {}
        self._failures: Dict[str, str] = {}
        self._completed = 0

    def start(
        self,
        name: str,
        target: Callable[..., Any],
        *,
        args: tuple[Any, ...] = (),
        kwargs: Optional[Dict[str, Any]] = None,
        daemon: bool = True,
    ) -> threading.Thread:
        if not callable(target):
            raise TypeError("worker target 必须可调用")
        worker_name = str(name or "worker").strip() or "worker"
        with self._lock:
            if self.stop_event.is_set():
                raise RuntimeError("插件 worker 已停止")
            existing = self._threads.get(worker_name)
            if existing and existing.is_alive():
                raise RuntimeError(f"插件 worker 已在运行: {worker_name}")

        def _runner() -> None:
            try:
                target(*args, **(kwargs or {}))
            except Exception as exc:  # noqa: BLE001 - plugin boundary
                with self._lock:
                    self._failures[worker_name] = str(exc)
                    if len(self._failures) > 20:
                        self._failures.pop(next(iter(self._failures)))
                logger.exception("Plugin %s worker %s failed", self.plugin_id, worker_name)
            finally:
                with self._lock:
                    current = threading.current_thread()
                    if self._threads.get(worker_name) is current:
                        self._threads.pop(worker_name, None)
                    self._completed += 1

        thread = threading.Thread(
            target=copy_context().run,
            args=(_runner,),
            name=f"plugin-{_safe_plugin_id(self.plugin_id)}-{worker_name}",
            daemon=daemon,
        )
        with self._lock:
            self._threads[worker_name] = thread
        thread.start()
        return thread

    def request_stop(self) -> None:
        self.stop_event.set()

    def start_timer(
        self,
        name: str,
        delay_seconds: float,
        target: Callable[..., Any],
        *,
        args: tuple[Any, ...] = (),
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> ManagedTimerHandle:
        cancel_event = threading.Event()

        def _wait_and_run() -> None:
            deadline = time.monotonic() + max(0.0, float(delay_seconds))
            while not cancel_event.is_set() and not self.stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    target(*args, **(kwargs or {}))
                    return
                cancel_event.wait(min(0.5, remaining))

        thread = self.start(name, _wait_and_run)
        return ManagedTimerHandle(thread, cancel_event)

    def close(self, join_timeout: float = 2.0) -> None:
        self.request_stop()
        with self._lock:
            threads = list(self._threads.values())
        deadline = time.monotonic() + max(0.0, join_timeout)
        for thread in threads:
            if thread is threading.current_thread() or not thread.is_alive():
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            rows = [
                {"name": name, "alive": thread.is_alive(), "error": self._failures.get(name)}
                for name, thread in self._threads.items()
            ]
        return {
            "active": sum(1 for item in rows if item["alive"]),
            "completed": self._completed,
            "failed": len(self._failures),
            "items": rows,
        }


class HealthFacade:
    def __init__(self, context: "PluginContext"):
        self._context = context

    def register(self, probe: Callable[[], Any]) -> None:
        if not callable(probe):
            raise TypeError("health probe 必须可调用")
        self._context._health_probe = probe

    def degraded(self, message: str) -> None:
        self._context._reported_health = {
            "status": "degraded", "message": str(message), "checked_at": time.time()
        }

    def healthy(self, message: str = "运行正常") -> None:
        self._context._reported_health = {
            "status": "healthy", "message": str(message), "checked_at": time.time()
        }


class AuditFacade:
    def __init__(self, plugin_id: str):
        self.plugin_id = plugin_id

    def record(
        self,
        action: str,
        *,
        target: str = "",
        summary: str = "",
        before: Any = None,
        after: Any = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return get_runtime_operation_service().record_audit(
            category="plugin",
            action=action,
            target=f"{self.plugin_id}:{target}" if target else self.plugin_id,
            summary=summary or action,
            before=before,
            after=after,
            details=details,
        )


class PluginContext:
    """Owned capabilities handed to a managed plugin at registration time."""

    def __init__(self, plugin_id: str, manifest: Dict[str, Any], plugin_path: Path):
        self.plugin_id = plugin_id
        self.manifest = dict(manifest)
        self.plugin_path = plugin_path
        from app.services.plugin_chat_config_service import ChatConfigFacade

        self.config = ChatConfigFacade(plugin_id, plugin_path)
        self.storage = PluginStorage(plugin_id)
        self.tasks = OwnerTaskFacade(plugin_id)
        self.workers = OwnerWorkerFacade(plugin_id)
        self.health = HealthFacade(self)
        self.audit = AuditFacade(plugin_id)
        self._health_probe: Optional[Callable[[], Any]] = None
        self._reported_health: Optional[Dict[str, Any]] = None
        self._cleanup_callbacks: List[Callable[[], Any]] = []
        self._closed = False
        self._lock = threading.RLock()

    def register_cleanup(self, callback: Callable[[], Any]) -> None:
        if not callable(callback):
            raise TypeError("cleanup callback 必须可调用")
        with self._lock:
            if self._closed:
                raise RuntimeError("插件上下文已经关闭")
            self._cleanup_callbacks.append(callback)

    def health_snapshot(self) -> Dict[str, Any]:
        started = time.perf_counter()
        if self._closed:
            return {"status": "stopped", "message": "插件上下文已关闭", "checked_at": time.time()}
        if self._health_probe is None:
            return self._reported_health or {
                "status": "healthy",
                "message": "插件已加载；未声明深度健康检查",
                "checked_at": time.time(),
            }
        try:
            timeout = max(
                1,
                min(int((self.manifest.get("health") or {}).get("timeout_seconds") or 5), 60),
            )
            future = _health_executor.submit(self._health_probe)
            raw = future.result(timeout=timeout)
            if isinstance(raw, dict):
                result = dict(raw)
            elif isinstance(raw, bool):
                result = {"status": "healthy" if raw else "unhealthy"}
            else:
                result = {"status": "healthy", "message": str(raw or "运行正常")}
            result.setdefault("status", "healthy")
            result.setdefault("message", "运行正常")
        except FutureTimeoutError:
            result = {"status": "unhealthy", "message": f"健康检查超过 {timeout} 秒"}
        except Exception as exc:  # noqa: BLE001 - plugin boundary
            result = {"status": "unhealthy", "message": str(exc)}
        result["checked_at"] = time.time()
        result["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    def close(self) -> List[str]:
        errors: List[str] = []
        with self._lock:
            if self._closed:
                return errors
            self._closed = True
            callbacks = list(reversed(self._cleanup_callbacks))
            self._cleanup_callbacks.clear()
        get_runtime_operation_service().cancel_owner(f"plugin:{self.plugin_id}")
        self.workers.request_stop()
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:  # noqa: BLE001 - cleanup boundary
                errors.append(str(exc))
                logger.warning("Plugin %s cleanup failed: %s", self.plugin_id, exc)
        self.workers.close()
        try:
            self.storage.clear_temporary()
        except OSError as exc:
            errors.append(str(exc))
        return errors


@dataclass
class RuntimeRecord:
    plugin_id: str
    api_version: int
    context: PluginContext
    registered_at: float


class PluginRuntimeRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._records: Dict[str, RuntimeRecord] = {}

    def register(self, plugin_id: str, manifest: Dict[str, Any], plugin_path: Path) -> PluginContext:
        self.unregister(plugin_id)
        api_version = int(manifest.get("plugin_api_version") or 0)
        if api_version != PLUGIN_RUNTIME_API_VERSION:
            raise ValueError(f"插件接口版本必须为 {PLUGIN_RUNTIME_API_VERSION}")
        context = PluginContext(plugin_id, manifest, plugin_path)
        with self._lock:
            self._records[plugin_id] = RuntimeRecord(
                plugin_id=plugin_id,
                api_version=api_version,
                context=context,
                registered_at=time.time(),
            )
        return context

    def unregister(self, plugin_id: str) -> List[str]:
        with self._lock:
            record = self._records.pop(plugin_id, None)
        return record.context.close() if record else []

    def get(self, plugin_id: str) -> Optional[PluginContext]:
        with self._lock:
            record = self._records.get(plugin_id)
            return record.context if record else None

    def snapshot(self, *, include_storage: bool = True) -> List[Dict[str, Any]]:
        with self._lock:
            records = list(self._records.values())
        result = []
        for record in records:
            result.append(
                {
                    "plugin_id": record.plugin_id,
                    "api_version": record.api_version,
                    "registered_at": record.registered_at,
                    "health": record.context.health_snapshot(),
                    "storage": record.context.storage.inventory() if include_storage else None,
                    "active_tasks": len(
                        [item for item in record.context.tasks.list(limit=100) if item.get("status") not in {"completed", "failed", "cancelled", "interrupted"}]
                    ),
                    "workers": record.context.workers.snapshot(),
                }
            )
        return result

    def backup_roots(self) -> List[Dict[str, Any]]:
        with self._lock:
            records = list(self._records.values())
        roots = []
        for record in records:
            sensitive = bool((record.context.manifest.get("backup") or {}).get("sensitive", False))
            roots.extend(
                [
                    {
                        "plugin_id": record.plugin_id,
                        "path": record.context.storage.persistent_root,
                        "storage_class": "persistent",
                        "portable": True,
                        "sensitive": sensitive,
                    },
                    {
                        "plugin_id": record.plugin_id,
                        "path": record.context.storage.generated_root,
                        "storage_class": "generated",
                        "portable": True,
                        "sensitive": sensitive,
                    },
                    {
                        "plugin_id": record.plugin_id,
                        "path": record.context.storage.machine_bound_root,
                        "storage_class": "machine_bound",
                        "portable": False,
                        "sensitive": sensitive,
                    },
                ]
            )
        return roots


_registry: Optional[PluginRuntimeRegistry] = None
_registry_lock = threading.Lock()


def get_plugin_runtime_registry() -> PluginRuntimeRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = PluginRuntimeRegistry()
    return _registry
