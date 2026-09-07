"""Persistent, owner-aware operation registry for core services and plugins.

The registry is deliberately generic.  Codex keeps its protocol-specific job
store, while backups, plugin maintenance and future plugin jobs use this
service so the management console has one lifecycle vocabulary.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from contextvars import copy_context
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger(__name__)

FINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
SENSITIVE_KEY_PATTERN = re.compile(
    r"(secret|token|password|passwd|api_?key|credential|cookie|authorization|dsn|proxy_url)",
    re.IGNORECASE,
)


class OperationCancelled(RuntimeError):
    """Raised cooperatively when an operation has been cancelled."""


class OperationContext:
    def __init__(self, service: "RuntimeOperationService", operation_id: str, cancel_event: threading.Event):
        self._service = service
        self.operation_id = operation_id
        self._cancel_event = cancel_event

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise OperationCancelled("操作已取消")

    def progress(self, percent: int, message: str, **details: Any) -> None:
        self._service.update_progress(
            self.operation_id,
            percent=max(0, min(int(percent), 100)),
            message=str(message or ""),
            details=details or None,
        )


class RuntimeOperationService:
    """Run managed background operations and persist their public history."""

    def __init__(self, database_path: Optional[Path] = None, retention_days: int = 30):
        configured = database_path or os.getenv("RUNTIME_OPERATIONS_DB") or "data/runtime_operations.db"
        self.database_path = Path(configured)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = max(1, int(retention_days))
        self._lock = threading.RLock()
        self._active: Dict[str, Dict[str, Any]] = {}
        self._initialize_store()
        self._mark_interrupted()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize_store(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_operations (
                    operation_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    updated_at REAL NOT NULL,
                    ended_at REAL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_operations_recent "
                "ON runtime_operations(status, updated_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_operations_owner "
                "ON runtime_operations(owner, updated_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_operation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_operation_events "
                "ON runtime_operation_events(operation_id, id DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_audit (
                    audit_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    before_json TEXT,
                    after_json TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_audit_recent "
                "ON runtime_audit(created_at DESC)"
            )

    def _mark_interrupted(self) -> None:
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE runtime_operations
                SET status = 'interrupted',
                    message = '应用重启前操作尚未完成',
                    error = COALESCE(error, 'operation interrupted by process restart'),
                    ended_at = ?, updated_at = ?
                WHERE status IN ('queued', 'running', 'cancelling')
                """,
                (now, now),
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _decode(value: Optional[str], default: Any) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default

    def _public(self, operation: Dict[str, Any]) -> Dict[str, Any]:
        public = {
            key: value
            for key, value in operation.items()
            if not str(key).startswith("_") and key not in {"thread", "cancel_event", "target"}
        }
        started_at = public.get("started_at")
        if isinstance(started_at, (int, float)):
            public["elapsed_seconds"] = round((public.get("ended_at") or time.time()) - started_at, 3)
        return public

    def _persist(self, operation: Dict[str, Any]) -> None:
        public = self._public(operation)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO runtime_operations (
                    operation_id, owner, kind, title, status, progress, message,
                    details_json, result_json, error, created_at, started_at,
                    updated_at, ended_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    status=excluded.status, progress=excluded.progress,
                    message=excluded.message, details_json=excluded.details_json,
                    result_json=excluded.result_json, error=excluded.error,
                    started_at=excluded.started_at, updated_at=excluded.updated_at,
                    ended_at=excluded.ended_at
                """,
                (
                    public["operation_id"], public["owner"], public["kind"], public["title"],
                    public["status"], int(public.get("progress") or 0),
                    str(public.get("message") or ""), self._json(public.get("details") or {}),
                    self._json(public["result"]) if public.get("result") is not None else None,
                    public.get("error"), public["created_at"], public.get("started_at"),
                    public["updated_at"], public.get("ended_at"),
                ),
            )

    def _event(self, operation_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO runtime_operation_events "
                "(operation_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (operation_id, event_type, self._json(payload or {}), time.time()),
            )

    def submit(
        self,
        *,
        owner: str,
        kind: str,
        title: str,
        target: Callable[[OperationContext], Any],
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        operation_id = uuid.uuid4().hex
        now = time.time()
        cancel_event = threading.Event()
        operation: Dict[str, Any] = {
            "operation_id": operation_id,
            "owner": str(owner or "system"),
            "kind": str(kind or "operation"),
            "title": str(title or kind or "后台操作"),
            "status": "queued",
            "progress": 0,
            "message": "等待执行",
            "details": dict(details or {}),
            "result": None,
            "error": None,
            "created_at": now,
            "started_at": None,
            "updated_at": now,
            "ended_at": None,
            "_cancel_event": cancel_event,
            "_target": target,
        }
        thread = threading.Thread(
            target=copy_context().run,
            args=(self._run, operation_id),
            name=f"operation-{str(kind)[:24]}-{operation_id[:8]}",
            daemon=True,
        )
        operation["_thread"] = thread
        with self._lock:
            self._active[operation_id] = operation
            self._persist(operation)
            self._event(operation_id, "queued", operation.get("details"))
        thread.start()
        return self._public(operation)

    def _run(self, operation_id: str) -> None:
        with self._lock:
            operation = self._active.get(operation_id)
            if not operation:
                return
            if operation["_cancel_event"].is_set():
                self._finish(operation_id, "cancelled", error="操作在开始前已取消")
                return
            operation.update(status="running", message="正在执行", started_at=time.time(), updated_at=time.time())
            self._persist(operation)
            self._event(operation_id, "started")
            target = operation["_target"]
            context = OperationContext(self, operation_id, operation["_cancel_event"])

        try:
            result = target(context)
            context.check_cancelled()
            self._finish(operation_id, "completed", result=result)
        except OperationCancelled as exc:
            self._finish(operation_id, "cancelled", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - operation boundary
            logger.exception("Runtime operation %s failed", operation_id)
            self._finish(operation_id, "failed", error=str(exc))

    def update_progress(
        self,
        operation_id: str,
        *,
        percent: int,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            operation = self._active.get(operation_id)
            if not operation:
                return
            operation["progress"] = max(int(operation.get("progress") or 0), int(percent))
            operation["message"] = message
            operation["updated_at"] = time.time()
            if details:
                operation.setdefault("details", {}).update(details)
            self._persist(operation)
            self._event(operation_id, "progress", {"progress": operation["progress"], "message": message, **(details or {})})

    def _finish(self, operation_id: str, status: str, *, result: Any = None, error: Optional[str] = None) -> None:
        with self._lock:
            operation = self._active.pop(operation_id, None)
            if not operation:
                return
            now = time.time()
            operation.update(
                status=status,
                progress=100 if status == "completed" else int(operation.get("progress") or 0),
                message="已完成" if status == "completed" else ("已取消" if status == "cancelled" else "执行失败"),
                result=result,
                error=error,
                updated_at=now,
                ended_at=now,
            )
            self._persist(operation)
            self._event(operation_id, status, {"error": error} if error else {})
            self._trim()

    def cancel(self, operation_id: str) -> Dict[str, Any]:
        with self._lock:
            operation = self._active.get(operation_id)
            if not operation:
                return {"success": False, "message": "操作不存在或已经结束"}
            operation["_cancel_event"].set()
            operation.update(status="cancelling", message="正在取消", updated_at=time.time())
            self._persist(operation)
            self._event(operation_id, "cancel_requested")
            return {"success": True, "operation_id": operation_id}

    def cancel_owner(self, owner: str) -> int:
        with self._lock:
            ids = [operation_id for operation_id, item in self._active.items() if item.get("owner") == owner]
        for operation_id in ids:
            self.cancel(operation_id)
        return len(ids)

    def active_count(self, owner: Optional[str] = None) -> int:
        """Return the number of in-process operations, optionally for one owner."""
        with self._lock:
            if owner is None:
                return len(self._active)
            return sum(1 for item in self._active.values() if item.get("owner") == owner)

    def _row(self, row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            "operation_id": row["operation_id"], "owner": row["owner"], "kind": row["kind"],
            "title": row["title"], "status": row["status"], "progress": row["progress"],
            "message": row["message"], "details": self._decode(row["details_json"], {}),
            "result": self._decode(row["result_json"], None), "error": row["error"],
            "created_at": row["created_at"], "started_at": row["started_at"],
            "updated_at": row["updated_at"], "ended_at": row["ended_at"],
        }
        return self._public(result)

    def get(self, operation_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            active = self._active.get(operation_id)
            if active:
                return self._public(active)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM runtime_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return self._row(row) if row else None

    def list(self, *, limit: int = 100, owner: Optional[str] = None) -> List[Dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        query = "SELECT * FROM runtime_operations"
        params: List[Any] = []
        if owner:
            query += " WHERE owner = ?"
            params.append(owner)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(bounded)
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row(row) for row in rows]

    def events(self, operation_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT event_type, payload_json, created_at FROM runtime_operation_events "
                "WHERE operation_id = ? ORDER BY id DESC LIMIT ?",
                (operation_id, bounded),
            ).fetchall()
        return [
            {"type": row["event_type"], "details": self._decode(row["payload_json"], {}), "created_at": row["created_at"]}
            for row in reversed(rows)
        ]

    @classmethod
    def _redact(cls, value: Any, key: str = "") -> Any:
        if key and SENSITIVE_KEY_PATTERN.search(key):
            return {"configured": bool(value), "redacted": True}
        if isinstance(value, dict):
            return {str(item_key): cls._redact(item, str(item_key)) for item_key, item in value.items()}
        if isinstance(value, list):
            return [cls._redact(item) for item in value[:200]]
        if isinstance(value, str) and len(value) > 2000:
            return value[:2000] + "…"
        return value

    def record_audit(
        self,
        *,
        category: str,
        action: str,
        target: str,
        status: str = "success",
        summary: str = "",
        before: Any = None,
        after: Any = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        record = {
            "audit_id": uuid.uuid4().hex,
            "category": str(category or "system"),
            "action": str(action or "change"),
            "target": str(target or "system"),
            "status": str(status or "success"),
            "summary": str(summary or action or "change")[:1000],
            "before": self._redact(before),
            "after": self._redact(after),
            "details": self._redact(details or {}),
            "created_at": time.time(),
        }
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO runtime_audit (
                    audit_id, category, action, target, status, summary,
                    before_json, after_json, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["audit_id"], record["category"], record["action"],
                    record["target"], record["status"], record["summary"],
                    self._json(record["before"]) if before is not None else None,
                    self._json(record["after"]) if after is not None else None,
                    self._json(record["details"]), record["created_at"],
                ),
            )
        return record

    def list_audit(self, *, limit: int = 100, category: Optional[str] = None) -> List[Dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        query = "SELECT * FROM runtime_audit"
        params: List[Any] = []
        if category:
            query += " WHERE category = ?"
            params.append(category)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(bounded)
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            {
                "audit_id": row["audit_id"], "category": row["category"],
                "action": row["action"], "target": row["target"],
                "status": row["status"], "summary": row["summary"],
                "before": self._decode(row["before_json"], None),
                "after": self._decode(row["after_json"], None),
                "details": self._decode(row["details_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def stats(self) -> Dict[str, Any]:
        rows = self.list(limit=500)
        counts: Dict[str, int] = {}
        owners: Dict[str, int] = {}
        for item in rows:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
            owners[item["owner"]] = owners.get(item["owner"], 0) + 1
        with self._lock:
            active_count = len(self._active)
        return {"active_count": active_count, "by_status": counts, "by_owner": owners}

    def _trim(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
        with closing(self._connect()) as connection, connection:
            old = connection.execute(
                "SELECT operation_id FROM runtime_operations WHERE ended_at IS NOT NULL AND ended_at < ?",
                (cutoff,),
            ).fetchall()
            ids = [row["operation_id"] for row in old]
            if not ids:
                return
            placeholders = ",".join("?" for _ in ids)
            connection.execute(f"DELETE FROM runtime_operation_events WHERE operation_id IN ({placeholders})", ids)
            connection.execute(f"DELETE FROM runtime_operations WHERE operation_id IN ({placeholders})", ids)


_service: Optional[RuntimeOperationService] = None
_service_lock = threading.Lock()


def get_runtime_operation_service() -> RuntimeOperationService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = RuntimeOperationService()
    return _service
