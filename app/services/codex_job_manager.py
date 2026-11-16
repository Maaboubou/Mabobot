from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.utils.subprocess_utils import hidden_process_kwargs

logger = logging.getLogger(__name__)

_INTERNAL_KEYS = {
    "proc",
    "proc_args_preview",
    "runtime_output_path",
    "runtime_output_dir",
    "stdout_tail",
    "stderr_tail",
    "workdir",
    "request_dir",
    "output_dir",
}


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _public_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Return a JSON-safe/public snapshot of a Codex job."""
    snapshot = {k: v for k, v in job.items() if k not in _INTERNAL_KEYS and not str(k).startswith("_")}
    now = time.time()
    started_at = snapshot.get("started_at")
    ended_at = snapshot.get("ended_at")
    if isinstance(started_at, (int, float)):
        snapshot.setdefault("elapsed_seconds", round((ended_at or now) - started_at, 3))
    return snapshot


class CodexJobManager:
    """Codex job registry with persistent history, events and cancel support.

    It tracks both pooled App Server turns and independent execution processes,
    and keeps a bounded persistent history for the management panel.
    """

    def __init__(self, max_recent: int = 100, database_path: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self._active: Dict[str, Dict[str, Any]] = {}
        self.max_recent = max(10, int(max_recent))
        configured = database_path or os.getenv("CODEX_RUNTIME_STATE_DB") or "data/codex_runtime.db"
        self.database_path = Path(configured)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_store()

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
                CREATE TABLE IF NOT EXISTS codex_jobs (
                    request_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    job_json TEXT NOT NULL,
                    started_at REAL,
                    updated_at REAL NOT NULL,
                    ended_at REAL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_codex_jobs_recent "
                "ON codex_jobs(status, updated_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS codex_job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_codex_job_events_request "
                "ON codex_job_events(request_id, id DESC)"
            )

    @staticmethod
    def _serialize(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)

    def _persist_job(self, job: Dict[str, Any]) -> None:
        public = _public_job(job)
        updated_at = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO codex_jobs
                    (request_id, status, job_json, started_at, updated_at, ended_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    status = excluded.status,
                    job_json = excluded.job_json,
                    started_at = excluded.started_at,
                    updated_at = excluded.updated_at,
                    ended_at = excluded.ended_at
                """,
                (
                    str(public.get("request_id") or ""),
                    str(public.get("status") or "unknown"),
                    self._serialize(public),
                    public.get("started_at"),
                    updated_at,
                    public.get("ended_at"),
                ),
            )

    @staticmethod
    def _decode_job(row: sqlite3.Row) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(row["job_json"])
            return payload if isinstance(payload, dict) else None
        except (TypeError, json.JSONDecodeError):
            return None

    def register(self, request_id: str, info: Dict[str, Any]) -> None:
        with self._lock:
            job = dict(info)
            from app.services.execution_contracts import current_execution_context
            context = current_execution_context()
            if context is not None:
                job.setdefault("execution_run_id", context.run_id)
            job.setdefault("request_id", request_id)
            job.setdefault("status", "starting")
            job.setdefault("started_at", time.time())
            self._active[request_id] = job
            self._persist_job(job)

    def update(self, request_id: str, **updates: Any) -> None:
        with self._lock:
            if request_id in self._active:
                self._active[request_id].update(updates)
                self._persist_job(self._active[request_id])

    def record_event(
        self,
        request_id: str,
        event_type: str,
        details: Optional[Dict[str, Any]] = None,
        **job_updates: Any,
    ) -> None:
        now = time.time()
        event = dict(details or {})
        event.update({"type": str(event_type), "created_at": now})
        with self._lock:
            job = self._active.get(request_id)
            if job is not None:
                job.update(job_updates)
                job["progress_event"] = str(event_type)
                job["progress_at"] = now
                self._persist_job(job)
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    INSERT INTO codex_job_events
                        (request_id, event_type, event_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (request_id, str(event_type), self._serialize(event), now),
                )

    def list_events(self, request_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT event_json FROM codex_job_events
                WHERE request_id = ? ORDER BY id DESC LIMIT ?
                """,
                (str(request_id), bounded),
            ).fetchall()
        events: List[Dict[str, Any]] = []
        for row in reversed(rows):
            try:
                payload = json.loads(row["event_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def attach_process(self, request_id: str, proc: asyncio.subprocess.Process, **updates: Any) -> None:
        payload = dict(updates)
        payload.update({"proc": proc, "pid": getattr(proc, "pid", None), "status": updates.get("status", "running")})
        self.update(request_id, **payload)

    def finish(self, request_id: str, status: str = "completed", **updates: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._active.pop(request_id, None)
            if not job:
                return None
            ended_at = time.time()
            job.update(updates)
            job["status"] = status
            job.setdefault("ended_at", ended_at)
            started_at = job.get("started_at")
            if isinstance(started_at, (int, float)):
                job["duration_seconds"] = round(job["ended_at"] - started_at, 3)
            public = _public_job(job)
            self._persist_job(public)
            self._trim_history()
            return public

    def _trim_history(self) -> None:
        retention_days = _positive_int_env("CODEX_JOB_RETENTION_DAYS", 30)
        cutoff = time.time() - retention_days * 86400
        with closing(self._connect()) as connection, connection:
            expired = connection.execute(
                """
                SELECT request_id FROM codex_jobs
                WHERE status IN ('completed', 'failed', 'timeout', 'cancelled')
                  AND updated_at < ?
                """,
                (cutoff,),
            ).fetchall()
            expired_ids = [str(row["request_id"]) for row in expired]
            if expired_ids:
                placeholders = ",".join("?" for _ in expired_ids)
                connection.execute(
                    f"DELETE FROM codex_job_events WHERE request_id IN ({placeholders})",
                    expired_ids,
                )
                connection.execute(
                    f"DELETE FROM codex_jobs WHERE request_id IN ({placeholders})",
                    expired_ids,
                )

    def list_active(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [_public_job(job) for job in self._active.values()]

    def list_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        bounded = max(0, min(int(limit), 500))
        if bounded == 0:
            return []
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT job_json FROM codex_jobs
                WHERE status IN ('completed', 'failed', 'timeout', 'cancelled')
                ORDER BY COALESCE(ended_at, updated_at) DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [job for row in rows if (job := self._decode_job(row)) is not None]

    def get_active(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._active.get(request_id)
            return _public_job(job) if job else None

    def get_record(self, request_id: str) -> Optional[Dict[str, Any]]:
        active = self.get_active(request_id)
        if active:
            return active
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT job_json FROM codex_jobs WHERE request_id = ?",
                (str(request_id),),
            ).fetchone()
        return self._decode_job(row) if row else None

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            active = list(self._active.values())
        recent = self.list_recent(limit=self.max_recent)
        now = time.time()
        return {
            "active_count": len(active),
            "recent_count": len(recent),
            "oldest_active_age_seconds": round(max((now - j.get("started_at", now) for j in active), default=0), 3),
            "active_by_status": self._count_by(active, "status"),
            "recent_by_status": self._count_by(recent, "status"),
        }

    @staticmethod
    def _count_by(items: List[Dict[str, Any]], key: str) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for item in items:
            value = str(item.get(key) or "unknown")
            counts[value] = counts.get(value, 0) + 1
        return counts

    async def cancel(self, request_id: str) -> Dict[str, Any]:
        """Cancel an active Codex job and best-effort kill its process tree."""
        with self._lock:
            job = self._active.get(request_id)
            if not job:
                return {"success": False, "message": "job not found or already finished", "request_id": request_id}
            job["status"] = "cancelling"
            job["cancel_requested_at"] = time.time()
            self._persist_job(job)
            proc = job.get("proc")
            pid = job.get("pid")
            runtime_output_path = str(job.get("runtime_output_path") or "")
            use_wsl = bool(job.get("use_wsl"))
            cancel_callback = job.get("_cancel_callback")

        logger.warning("Cancelling Codex job %s pid=%s marker=%s", request_id, pid, runtime_output_path)
        self.record_event(request_id, "cancel_requested", {})
        if callable(cancel_callback):
            # App Server turns share one long-lived process, so cancelling the
            # subprocess would also abort unrelated chats. Interrupt only the
            # selected turn through its registered protocol callback.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, cancel_callback)
        else:
            await self._terminate_process_tree(proc, pid, runtime_output_path, use_wsl, request_id)
        self.finish(request_id, status="cancelled", cancelled_at=time.time())
        return {"success": True, "message": "cancel requested", "request_id": request_id}

    async def _terminate_process_tree(
        self,
        proc: Any,
        pid: Any,
        runtime_output_path: str,
        use_wsl: bool,
        request_id: str,
    ) -> None:
        if os.name == "nt":
            if pid:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=False,
                        **hidden_process_kwargs(),
                    )
                except Exception:
                    logger.exception("Codex job %s: taskkill failed", request_id)
            if use_wsl and runtime_output_path:
                marker = shlex.quote(runtime_output_path)
                kill_cmd = (
                    f"pkill -TERM -f {marker} 2>/dev/null || true; "
                    f"sleep 1; "
                    f"pkill -KILL -f {marker} 2>/dev/null || true"
                )
                try:
                    subprocess.run(
                        ["wsl.exe", "bash", "-lc", kill_cmd],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=False,
                        **hidden_process_kwargs(),
                    )
                except Exception:
                    logger.exception("Codex job %s: WSL child cleanup failed", request_id)
        else:
            if pid:
                try:
                    os.killpg(int(pid), 15)
                except Exception:
                    try:
                        if proc:
                            proc.terminate()
                    except ProcessLookupError:
                        pass
            try:
                if proc:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                    return
            except Exception:
                pass
            if pid:
                try:
                    os.killpg(int(pid), 9)
                except Exception:
                    try:
                        if proc:
                            proc.kill()
                    except ProcessLookupError:
                        pass

        try:
            if proc:
                await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass


codex_job_manager = CodexJobManager()
