"""Classified storage inventory and recoverable cleanup for managed data."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


class StorageError(RuntimeError):
    pass


class StorageService:
    CATEGORY_PATHS = {
        "models": ["data/models"],
        "generated": [
            "data/daily_reports", "data/weekly_reports",
            "data/chat_summaries",
        ],
        "chat_archive": [
            "data/database.db", "data/chat_archive", "data/chat_logs",
        ],
        "diagnostics": [
            "data/llm_call_history.jsonl",
            "data/corrupt_telemetry_20260728_0034", "logs",
        ],
        "managed_plugins": ["data/plugins", "tmp/plugins"],
        "temporary": ["tmp"],
        "backups": ["data/system_backups", "data/backups"],
    }

    def __init__(self, project_root: Optional[Path] = None):
        self.project_root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        self.cache_path = self.project_root / "data" / "storage_inventory.json"
        self.trash_root = self.project_root / "data" / "system_trash"
        self._lock = threading.RLock()

    @staticmethod
    def _usage(path: Path, excluded: Set[Path]) -> Tuple[int, int, int]:
        try:
            resolved = path.resolve()
        except OSError:
            return 0, 0, 0
        if resolved in excluded:
            return 0, 0, 0
        if path.is_file():
            try:
                return path.stat().st_size, 1, 0
            except OSError:
                return 0, 0, 0
        if not path.is_dir():
            return 0, 0, 0
        bytes_total = 0
        files = 0
        directories = 1
        try:
            entries = list(os.scandir(path))
        except OSError:
            return 0, 0, 0
        for entry in entries:
            item = Path(entry.path)
            try:
                item_resolved = item.resolve()
            except OSError:
                continue
            if item_resolved in excluded or entry.is_symlink():
                continue
            if entry.is_file(follow_symlinks=False):
                try:
                    bytes_total += entry.stat(follow_symlinks=False).st_size
                    files += 1
                except OSError:
                    pass
            elif entry.is_dir(follow_symlinks=False):
                child_bytes, child_files, child_dirs = StorageService._usage(item, excluded)
                bytes_total += child_bytes
                files += child_files
                directories += child_dirs
        return bytes_total, files, directories

    def _path(self, relative: str) -> Path:
        path = (self.project_root / relative).resolve()
        try:
            path.relative_to(self.project_root)
        except ValueError as exc:
            raise StorageError("存储路径越过项目目录") from exc
        return path

    def scan(self, operation: Any = None) -> Dict[str, Any]:
        with self._lock:
            rows = []
            # tmp/plugins is a child of tmp. Exclude it from the broad temporary
            # category so totals remain additive instead of double counted.
            managed_temp = self._path("tmp/plugins")
            for index, (category, relative_paths) in enumerate(self.CATEGORY_PATHS.items(), 1):
                bytes_total = files = directories = 0
                paths = []
                for relative in relative_paths:
                    path = self._path(relative)
                    if not path.exists():
                        continue
                    excluded = {managed_temp} if category == "temporary" else set()
                    size, file_count, directory_count = self._usage(path, excluded)
                    bytes_total += size
                    files += file_count
                    directories += directory_count
                    paths.append(relative)
                rows.append(
                    {
                        "category": category,
                        "paths": paths,
                        "bytes": bytes_total,
                        "files": files,
                        "directories": directories,
                    }
                )
                if operation:
                    operation.check_cancelled()
                    operation.progress(
                        int(index / len(self.CATEGORY_PATHS) * 95),
                        f"正在统计 {category}",
                    )
            result = {
                "scanned_at": datetime.now(timezone.utc).isoformat(),
                "categories": rows,
                "total_classified_bytes": sum(item["bytes"] for item in rows),
                "total_classified_files": sum(item["files"] for item in rows),
            }
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.cache_path)
            return result

    def overview(self) -> Dict[str, Any]:
        if not self.cache_path.exists():
            return {
                "scanned_at": None,
                "categories": [],
                "scan_required": True,
                "cleanup": self.cleanup_preview(),
            }
        try:
            result = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result = {"scanned_at": None, "categories": [], "scan_required": True}
        result["cleanup"] = self.cleanup_preview()
        return result

    def _managed_cleanup_candidates(self, retention_days: int) -> Iterable[Path]:
        cutoff = time.time() - max(0, int(retention_days)) * 86400
        roots = [self._path("tmp/plugins")]
        managed_data = self._path("data/plugins")
        if managed_data.exists():
            roots.extend(path for path in managed_data.glob("*/cache") if path.is_dir())
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    if path.stat().st_mtime <= cutoff:
                        yield path
                except OSError:
                    continue

    def cleanup_preview(self, retention_days: int = 7) -> Dict[str, Any]:
        files = []
        bytes_total = 0
        for path in self._managed_cleanup_candidates(retention_days):
            try:
                size = path.stat().st_size
                relative = path.resolve().relative_to(self.project_root).as_posix()
            except (OSError, ValueError):
                continue
            files.append({"path": relative, "bytes": size})
            bytes_total += size
        files.sort(key=lambda item: item["bytes"], reverse=True)
        return {
            "retention_days": max(0, int(retention_days)),
            "files": len(files),
            "bytes": bytes_total,
            "sample": files[:20],
            "scope": "managed_plugin_cache_and_temporary",
            "recoverable": True,
        }

    def cleanup_managed(self, *, retention_days: int = 7, confirmation: str) -> Dict[str, Any]:
        if confirmation.strip() != "清理托管缓存":
            raise StorageError("请输入“清理托管缓存”确认操作")
        with self._lock:
            candidates = list(self._managed_cleanup_candidates(retention_days))
            trash = self.trash_root / datetime.now().strftime("%Y%m%d-%H%M%S")
            moved = []
            bytes_total = 0
            for source in candidates:
                try:
                    relative = source.resolve().relative_to(self.project_root)
                    size = source.stat().st_size
                except (OSError, ValueError):
                    continue
                destination = trash / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                moved.append(relative.as_posix())
                bytes_total += size
            return {
                "moved_to_trash": len(moved),
                "bytes": bytes_total,
                "trash_path": str(trash),
                "recoverable": True,
                "retention_days": retention_days,
            }


_service: Optional[StorageService] = None
_service_lock = threading.Lock()


def get_storage_service() -> StorageService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = StorageService()
    return _service
