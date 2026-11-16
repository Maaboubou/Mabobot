"""Durable admission, dispatch boundaries, operations and engine session heads.

Unknown dispatches are never replayed. This store does not send WeChat messages;
delivery continues to belong to the existing business outbox.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from app.services.execution_contracts import (
    ExecutionError, ExecutionResult, InputEnvelope, RouteSnapshot, TaskContext, canonical, digest,
)

TERMINAL = frozenset({"completed", "skipped", "failed", "cancelled", "interrupted_unknown"})


def execution_db_path():
    return os.getenv("ENGINE_EXECUTION_DB") or os.getenv("CODEX_RUNTIME_STATE_DB") or "data/codex_runtime.db"


class ExecutionStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS engine_runs (
                    run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                    request_key TEXT UNIQUE NOT NULL, admission_hash TEXT NOT NULL,
                    context_json TEXT NOT NULL, route_json TEXT NOT NULL,
                    input_json TEXT, input_hash TEXT, status TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    delivery_status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS engine_attempts (
                    attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    candidate_index INTEGER NOT NULL, request_hash TEXT NOT NULL,
                    status TEXT NOT NULL, dispatched REAL, result_json TEXT,
                    UNIQUE(run_id, candidate_index)
                );
                CREATE TABLE IF NOT EXISTS engine_config_pins (
                    run_id TEXT NOT NULL, engine TEXT NOT NULL, profile_id TEXT NOT NULL,
                    generation TEXT NOT NULL, released INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(run_id, engine, profile_id, generation)
                );
                CREATE TABLE IF NOT EXISTS engine_operations (
                    operation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    kind TEXT NOT NULL, business_key TEXT NOT NULL, input_hash TEXT NOT NULL,
                    status TEXT NOT NULL, result_json TEXT, updated REAL NOT NULL,
                    UNIQUE(run_id, kind, business_key)
                );
                CREATE TABLE IF NOT EXISTS engine_sessions (
                    chat_id TEXT NOT NULL, engine TEXT NOT NULL, profile_id TEXT NOT NULL,
                    purpose TEXT NOT NULL, state_json TEXT NOT NULL,
                    PRIMARY KEY(chat_id, engine, profile_id, purpose)
                );
                CREATE TABLE IF NOT EXISTS engine_session_controls (
                    chat_id TEXT NOT NULL, engine TEXT NOT NULL, profile_id TEXT NOT NULL,
                    purpose TEXT NOT NULL, cutoff REAL NOT NULL,
                    PRIMARY KEY(chat_id, engine, profile_id, purpose)
                );
                CREATE TABLE IF NOT EXISTS engine_session_heads (
                    chat_id TEXT NOT NULL, purpose TEXT NOT NULL, run_id TEXT NOT NULL,
                    PRIMARY KEY(chat_id, purpose)
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def admit(self, context: TaskContext, route: RouteSnapshot) -> tuple[str, bool]:
        # Dedup is scoped to the authenticated entry, not a global user string.
        key = digest([context.source_kind, context.source_chat_id, context.component,
                      context.task_type, context.request_id])
        identity = asdict(context)
        for key_to_remove in ("task_id", "run_id", "accepted_at", "deadline"):
            identity.pop(key_to_remove)
        signature = digest(identity)
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT run_id,admission_hash FROM engine_runs WHERE request_key=?", (key,)).fetchone()
            if row:
                if row["admission_hash"] != signature:
                    raise ExecutionError("identity_conflict", "Request ID belongs to a different admission")
                return row["run_id"], False
            db.execute("INSERT INTO engine_runs VALUES(?,?,?,?,?,?,NULL,NULL,'queued',0,?,?,?)",
                       (context.run_id, context.task_id, key, signature, canonical(asdict(context)),
                        canonical(asdict(route)), "pending" if context.delivery_chat_id else "not_required", now, now))
            for candidate in route.candidates:
                db.execute("INSERT OR IGNORE INTO engine_config_pins VALUES(?,?,?,?,0)",
                           (context.run_id, candidate.engine, candidate.profile_id, candidate.generation))
            image_policy = json.loads(route.image_policy_json)
            if image_policy.get("native_profile_id") and image_policy.get("native_runtime_generation"):
                db.execute("INSERT OR IGNORE INTO engine_config_pins VALUES(?,?,?,?,0)",
                    (context.run_id, "codex", image_policy["native_profile_id"], image_policy["native_runtime_generation"]))
            if context.component == "assistant":
                db.execute("INSERT INTO engine_session_heads VALUES(?,?,?) ON CONFLICT(chat_id,purpose) DO UPDATE SET run_id=excluded.run_id",
                           (context.source_chat_id, context.purpose, context.run_id))
        return context.run_id, True

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM engine_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return dict(row)

    def contracts(self, run_id):
        row = self.get(run_id)
        return TaskContext(**json.loads(row["context_json"])), RouteSnapshot.from_dict(json.loads(row["route_json"]))

    def bind_input(self, run_id: str, envelope: InputEnvelope):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT input_hash,status FROM engine_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            if row["input_hash"] and row["input_hash"] != envelope.sha256:
                raise ExecutionError("input_conflict", "Accepted input is immutable")
            if row["status"] not in {"queued", "preparing"} and not row["input_hash"]:
                raise ExecutionError("invalid_state", "Input cannot be bound after dispatch")
            db.execute("UPDATE engine_runs SET input_json=?,input_hash=?,updated=? WHERE run_id=?",
                       (canonical(asdict(envelope)), envelope.sha256, time.time(), run_id))

    def require_active(self, run_id, *, db=None):
        row = db.execute("SELECT * FROM engine_runs WHERE run_id=?", (run_id,)).fetchone() if db else self.get(run_id)
        if not row:
            raise KeyError(run_id)
        context = json.loads(row["context_json"])
        if row["cancel_requested"]:
            raise ExecutionError("cancelled", "Task cancellation requested")
        if row["status"] in TERMINAL:
            raise ExecutionError("invalid_state", "Task is already terminal")
        if context["deadline"] <= time.time():
            raise ExecutionError("deadline_exceeded", "Task deadline exceeded")
        if context.get("parent_run_id"):
            self.require_active(context["parent_run_id"], db=db)
        return row

    def dispatch(self, run_id: str, index: int, attempt_id: str) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM engine_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if old:
                if old["run_id"] != run_id or old["candidate_index"] != index:
                    raise ExecutionError("attempt_conflict", "Attempt belongs to another request")
                return {**dict(old), "new": False}
            row = self.require_active(run_id, db=db)
            if not row["input_hash"]:
                raise ExecutionError("input_missing", "Input preparation has not completed")
            route = RouteSnapshot.from_dict(json.loads(row["route_json"]))
            if not 0 <= index < min(len(route.candidates), route.max_attempts):
                raise ExecutionError("retry_budget", "Attempt exceeds frozen candidate budget")
            count = db.execute("SELECT COUNT(*) FROM engine_attempts WHERE run_id=?", (run_id,)).fetchone()[0]
            if count != index:
                raise ExecutionError("attempt_order", "Candidates must execute in frozen order")
            previous = db.execute("SELECT status FROM engine_attempts WHERE run_id=? ORDER BY candidate_index DESC LIMIT 1", (run_id,)).fetchone()
            if previous and previous[0] != "failed":
                raise ExecutionError("reconciliation_required", "Previous attempt is not a confirmed failure")
            request_hash = digest([row["input_hash"], json.loads(row["route_json"]), index])
            db.execute("INSERT INTO engine_attempts VALUES(?,?,?,?, 'running',?,NULL)",
                       (attempt_id, run_id, index, request_hash, time.time()))
            db.execute("UPDATE engine_runs SET status='running',updated=? WHERE run_id=?", (time.time(), run_id))
        return {"attempt_id": attempt_id, "status": "running", "new": True}

    def finish_attempt(self, result: ExecutionResult, *, final=True):
        if result.status not in TERMINAL:
            raise ValueError("A completed attempt must have a terminal status")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT a.*,r.task_id,r.cancel_requested FROM engine_attempts a JOIN engine_runs r USING(run_id) WHERE attempt_id=?", (result.attempt_id,)).fetchone()
            if not row or row["run_id"] != result.run_id or row["task_id"] != result.task_id:
                raise ExecutionError("result_identity", "Result does not belong to this attempt")
            encoded = canonical(asdict(result))
            if row["result_json"] and row["result_json"] != encoded:
                raise ExecutionError("result_conflict", "Attempt result is already recorded")
            db.execute("UPDATE engine_attempts SET status=?,result_json=? WHERE attempt_id=?", (result.status, encoded, result.attempt_id))
            if final:
                status = "cancelled" if row["cancel_requested"] else result.status
                db.execute("UPDATE engine_runs SET status=?,updated=? WHERE run_id=?", (status, time.time(), result.run_id))
                # Unknown native execution still protects its configuration.
                if result.status != "interrupted_unknown":
                    db.execute("UPDATE engine_config_pins SET released=1 WHERE run_id=?", (result.run_id,))

    def cancel(self, run_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE engine_runs SET cancel_requested=1,delivery_status=CASE WHEN delivery_status IN ('sent','not_required') THEN delivery_status ELSE 'blocked' END,updated=? WHERE run_id=?", (time.time(), run_id))
            children = db.execute("SELECT run_id,context_json FROM engine_runs WHERE status NOT IN ('completed','skipped','failed','cancelled')").fetchall()
            for child in children:
                if json.loads(child["context_json"]).get("parent_run_id") == run_id:
                    db.execute("UPDATE engine_runs SET cancel_requested=1,delivery_status='blocked' WHERE run_id=?", (child["run_id"],))
            undispatched = db.execute("SELECT run_id FROM engine_runs r WHERE cancel_requested=1 AND status IN ('queued','preparing') AND NOT EXISTS (SELECT 1 FROM engine_attempts a WHERE a.run_id=r.run_id) AND NOT EXISTS (SELECT 1 FROM engine_operations o WHERE o.run_id=r.run_id AND o.status IN ('running','unknown'))").fetchall()
            for row in undispatched:
                db.execute("UPDATE engine_runs SET status='cancelled' WHERE run_id=?", (row[0],))
                db.execute("UPDATE engine_config_pins SET released=1 WHERE run_id=?", (row[0],))

    def recover(self, authorized) -> list[str]:
        """Return only never-dispatched, complete, still-authorized admissions."""
        resumable = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM engine_runs WHERE status NOT IN ('completed','skipped','failed','cancelled','interrupted_unknown')").fetchall()
            for row in rows:
                ctx = TaskContext(**json.loads(row["context_json"]))
                attempted = db.execute("SELECT 1 FROM engine_attempts WHERE run_id=?", (ctx.run_id,)).fetchone()
                if not attempted and not row["input_json"]:
                    db.execute("""UPDATE engine_runs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'skipped' END,
                        delivery_status=CASE WHEN cancel_requested=1 THEN 'blocked' ELSE 'not_required' END,updated=? WHERE run_id=?""",
                        (time.time(), ctx.run_id))
                    db.execute("UPDATE engine_config_pins SET released=1 WHERE run_id=?", (ctx.run_id,))
                elif not attempted and row["input_json"] and not ctx.depth and not row["cancel_requested"] and ctx.deadline > time.time() and authorized(ctx):
                    resumable.append(ctx.run_id)
                else:
                    db.execute("UPDATE engine_runs SET status='interrupted_unknown' WHERE run_id=?", (ctx.run_id,))
            db.execute("UPDATE engine_attempts SET status='interrupted_unknown' WHERE status='running'")
            db.execute("UPDATE engine_operations SET status='unknown' WHERE status='running'")
        return resumable

    def claim_operation(self, run_id, kind, business_key, input_value):
        if not business_key:
            raise ValueError("A side effect needs an explicit business key")
        input_hash = digest(input_value)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM engine_operations WHERE run_id=? AND kind=? AND business_key=?", (run_id, kind, business_key)).fetchone()
            if row:
                if row["input_hash"] != input_hash:
                    raise ExecutionError("operation_conflict", "Business key is bound to another input")
                return dict(row), False
            self.require_active(run_id, db=db)
            op_id = uuid.uuid4().hex
            db.execute("INSERT INTO engine_operations VALUES(?,?,?,?,?,'running',NULL,?)", (op_id, run_id, kind, business_key, input_hash, time.time()))
        return {"operation_id": op_id, "run_id": run_id, "status": "running"}, True

    def finish_business_run(self, run_id, status, *, delivery_status=None):
        if status not in TERMINAL:
            raise ValueError("Invalid business outcome")
        with self.connect() as db:
            db.execute("UPDATE engine_runs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE ? END,updated=? WHERE run_id=?", (status, time.time(), run_id))
            if status != "interrupted_unknown":
                db.execute("UPDATE engine_config_pins SET released=1 WHERE run_id=?", (run_id,))
            if delivery_status:
                db.execute("UPDATE engine_runs SET delivery_status=CASE WHEN cancel_requested=1 THEN 'blocked' ELSE ? END WHERE run_id=?", (delivery_status, run_id))

    def finish_unused_admissions(self, run_ids=None, *, expired_before=None):
        """Close only reservations that have never received input or dispatched.

        Scope cleanup supplies explicit IDs; maintenance supplies a deadline.
        Check and update under one write transaction to avoid racing dispatch.
        """
        if run_ids is None and expired_before is None:
            return []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT run_id,context_json FROM engine_runs
                WHERE status IN ('queued','preparing') AND input_json IS NULL
                AND NOT EXISTS (SELECT 1 FROM engine_attempts a WHERE a.run_id=engine_runs.run_id)""").fetchall()
            selected = set(run_ids) if run_ids is not None else None
            closed = []
            for row in rows:
                if selected is not None and row["run_id"] not in selected:
                    continue
                if expired_before is not None and json.loads(row["context_json"])["deadline"] > expired_before:
                    continue
                db.execute("""UPDATE engine_runs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'skipped' END,
                    delivery_status=CASE WHEN cancel_requested=1 THEN 'blocked' ELSE 'not_required' END,updated=? WHERE run_id=?""",
                    (time.time(), row["run_id"]))
                db.execute("UPDATE engine_config_pins SET released=1 WHERE run_id=?", (row["run_id"],))
                closed.append(row["run_id"])
            return closed

    def delivery_status(self, run_id, status):
        if status not in {"not_required", "pending", "sending", "partial", "sent", "failed", "blocked", "uncertain"}:
            raise ValueError("Invalid delivery state")
        with self.connect() as db:
            db.execute("UPDATE engine_runs SET delivery_status=?,updated=? WHERE run_id=?", (status, time.time(), run_id))

    def finish_operation(self, operation_id, status, result):
        if status not in {"completed", "failed", "unknown", "cancelled", "partial"}:
            raise ValueError("Invalid operation outcome")
        with self.connect() as db:
            db.execute("UPDATE engine_operations SET status=?,result_json=?,updated=? WHERE operation_id=? AND status IN ('running','unknown','partial')", (status, canonical(result), time.time(), operation_id))

    def activate_session_head(self, context):
        with self.connect() as db:
            existing = db.execute("SELECT r.created FROM engine_session_heads h JOIN engine_runs r ON h.run_id=r.run_id WHERE h.chat_id=? AND h.purpose=?", (context.source_chat_id, context.purpose)).fetchone()
            if existing and existing[0] > context.accepted_at:
                return False
            db.execute("INSERT INTO engine_session_heads VALUES(?,?,?) ON CONFLICT(chat_id,purpose) DO UPDATE SET run_id=excluded.run_id", (context.source_chat_id, context.purpose, context.run_id))
        return True

    def save_session(self, context, candidate, state):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            control = db.execute("SELECT cutoff FROM engine_session_controls WHERE chat_id=? AND engine=? AND profile_id=? AND purpose=?",
                (context.source_chat_id, candidate.engine, candidate.profile_id, context.purpose)).fetchone()
            if control and context.accepted_at <= control[0]:
                return False
            head = db.execute("SELECT run_id FROM engine_session_heads WHERE chat_id=? AND purpose=?", (context.source_chat_id, context.purpose)).fetchone()
            if not head or head[0] != context.run_id:
                return False
            if candidate.engine == "opencode" and "usage" in state:
                previous = db.execute("SELECT state_json FROM engine_sessions WHERE chat_id=? AND engine=? AND profile_id=? AND purpose=?",
                    (context.source_chat_id, candidate.engine, candidate.profile_id, context.purpose)).fetchone()
                old = json.loads(previous[0]) if previous else {}
                totals = dict(old.get("lifetime_usage") or {})
                turns = old.get("recorded_turn_count", 0)
                unknown = old.get("unknown_usage_turns", int(bool(old) and "usage" not in old))
                if old.get("last_completed_run") != state.get("last_completed_run"):
                    turns += 1
                    usage = state.get("usage") or {}
                    unknown += int(usage.get("total_tokens") is None)
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"):
                        if usage.get(key) is not None:
                            totals[key] = totals.get(key, 0) + usage[key]
                state = {**state, "lifetime_usage": totals, "recorded_turn_count": turns, "unknown_usage_turns": unknown}
            db.execute("INSERT INTO engine_sessions VALUES(?,?,?,?,?) ON CONFLICT(chat_id,engine,profile_id,purpose) DO UPDATE SET state_json=excluded.state_json", (context.source_chat_id, candidate.engine, candidate.profile_id, context.purpose, canonical(state)))
        return True

    def manage_opencode_session(self, chat_id, profile_id, *, delete=False):
        """Reset/remove one foreground session without erasing archive or task history."""
        key = (chat_id, 'opencode', profile_id, 'foreground')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state_json FROM engine_sessions WHERE chat_id=? AND engine=? AND profile_id=? AND purpose=?', key).fetchone()
            if not row:
                raise KeyError('会话不存在或已删除')
            for run in db.execute("SELECT context_json,route_json FROM engine_runs WHERE status NOT IN ('completed','failed','skipped','cancelled')"):
                context, route = json.loads(run['context_json']), json.loads(run['route_json'])
                if context['source_chat_id'] == chat_id and context['purpose'] == 'foreground' and any(
                        c['engine'] == 'opencode' and c['profile_id'] == profile_id for c in route['candidates']):
                    raise ExecutionError('session_busy', '该会话仍有运行中或待核对的任务，请先中断或处理任务')
            cutoff = time.time()
            db.execute('INSERT INTO engine_session_controls VALUES(?,?,?,?,?) ON CONFLICT(chat_id,engine,profile_id,purpose) DO UPDATE SET cutoff=excluded.cutoff', (*key, cutoff))
            old = json.loads(row[0])
            if delete:
                db.execute('DELETE FROM engine_sessions WHERE chat_id=? AND engine=? AND profile_id=? AND purpose=?', key)
            else:
                state = {'requires_rebuild': True, 'reset_at': cutoff,
                    'last_completed_run': old.get('last_completed_run', ''),
                    'model_context_window': old.get('model_context_window'), 'usage': None,
                    'context_tokens': None, 'recorded_turn_count': 0, 'unknown_usage_turns': 0,
                    'lifetime_usage': {}}
                db.execute('UPDATE engine_sessions SET state_json=? WHERE chat_id=? AND engine=? AND profile_id=? AND purpose=?', (canonical(state), *key))
        return {'chat_id': chat_id, 'profile_id': profile_id, 'deleted': delete, 'reset': not delete}

    def load_session(self, chat_id, engine, profile_id, purpose="foreground"):
        with self.connect() as db:
            row = db.execute("SELECT state_json FROM engine_sessions WHERE chat_id=? AND engine=? AND profile_id=? AND purpose=?", (chat_id, engine, profile_id, purpose)).fetchone()
        return json.loads(row[0]) if row else None

    def pinned(self, engine, profile_id, generation):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM engine_config_pins WHERE engine=? AND profile_id=? AND generation=? AND released=0", (engine, profile_id, generation)).fetchone()[0]

    def recent(self, limit=50):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT run_id,task_id,status,cancel_requested,delivery_status,created,updated,route_json FROM engine_runs ORDER BY created DESC LIMIT ?", (min(200, max(1, limit)),))]
