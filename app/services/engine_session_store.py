"""Single native-session store with a read-only legacy Codex projection."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3


class EngineSessionStore:
    @staticmethod
    def initialize(connection, path):
        old = connection.execute("SELECT type FROM sqlite_master WHERE name='codex_thread_state'").fetchone()
        if old and old[0] == "table":
            # sqlite backup includes committed WAL pages; never copy a live db file.
            backup = Path(str(path) + ".pre-engine-sessions.bak")
            if not backup.exists():
                with sqlite3.connect(backup) as target:
                    connection.backup(target)
                backup.chmod(0o600)
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS engine_sessions (
                chat_id TEXT NOT NULL, engine TEXT NOT NULL, profile_id TEXT NOT NULL,
                purpose TEXT NOT NULL, state_json TEXT NOT NULL,
                PRIMARY KEY(chat_id, engine, profile_id, purpose)
            );
            CREATE TABLE IF NOT EXISTS codex_session_projection (
                chat_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, purpose TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS engine_chat_aliases (
                chat_name TEXT PRIMARY KEY, stable_id TEXT NOT NULL
            );
        """)
        connection.execute("BEGIN IMMEDIATE")
        if old and old[0] == "table":
            for row in connection.execute("SELECT chat_id,state_json FROM codex_thread_state").fetchall():
                try:
                    state = json.loads(row[1])
                except (TypeError, ValueError):
                    continue
                EngineSessionStore.put(connection, row[0], state, host_context=False)
            connection.execute("ALTER TABLE codex_thread_state RENAME TO codex_thread_state_legacy_v1")
        connection.execute("DROP VIEW IF EXISTS codex_thread_state")
        connection.execute("""CREATE VIEW codex_thread_state AS
            SELECT p.chat_id,s.state_json,
                   json_extract(s.state_json,'$.codex_version') AS codex_version,
                   json_extract(s.state_json,'$.schema_hash') AS schema_hash,
                   json_extract(s.state_json,'$.config_signature') AS config_signature,
                   COALESCE(json_extract(s.state_json,'$.updated_at'),'') AS updated_at
            FROM engine_sessions s JOIN codex_session_projection p
              ON s.chat_id=COALESCE((SELECT stable_id FROM engine_chat_aliases WHERE chat_name=p.chat_id),p.chat_id)
                 AND s.profile_id=p.profile_id AND s.purpose=p.purpose
            WHERE s.engine='codex'""")

    @staticmethod
    def put(connection, chat_id, state, *, profile_id=None, purpose="foreground", publish=True, host_context=True):
        from app.services.execution_contracts import current_execution_context
        context = current_execution_context() if host_context else None
        stable_id = EngineSessionStore._identity(connection, chat_id, context)
        if context is not None:
            purpose = context.purpose
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='engine_session_heads'").fetchone():
                head = connection.execute("SELECT run_id FROM engine_session_heads WHERE chat_id=? AND purpose=?", (stable_id, purpose)).fetchone()
                if head and head[0] != context.run_id:
                    return False
        profile = str(profile_id if profile_id is not None else state.get("runtime_profile") or "")
        connection.execute("INSERT INTO engine_sessions VALUES(?, 'codex',?,?,?) ON CONFLICT(chat_id,engine,profile_id,purpose) DO UPDATE SET state_json=excluded.state_json",
            (stable_id, profile, purpose, json.dumps(state, ensure_ascii=False, separators=(",", ":"))))
        if publish and purpose == "foreground":
            connection.execute("INSERT INTO codex_session_projection VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET profile_id=excluded.profile_id,purpose=excluded.purpose", (str(chat_id), profile, purpose))
        return True

    @staticmethod
    def _identity(connection, chat_id, context=None):
        name = str(chat_id)
        if context is not None and context.source_chat_id:
            stable = context.source_chat_id
            connection.execute("INSERT INTO engine_chat_aliases VALUES(?,?) ON CONFLICT(chat_name) DO UPDATE SET stable_id=excluded.stable_id", (name, stable))
            if name != stable:
                connection.execute("INSERT OR IGNORE INTO engine_sessions SELECT ?,engine,profile_id,purpose,state_json FROM engine_sessions WHERE chat_id=?", (stable, name))
                connection.execute("DELETE FROM engine_sessions WHERE chat_id=?", (name,))
            return stable
        row = connection.execute("SELECT stable_id FROM engine_chat_aliases WHERE chat_name=?", (name,)).fetchone()
        return row[0] if row else name

    @staticmethod
    def get(connection, chat_id, *, profile_id=None, purpose="foreground"):
        from app.services.execution_contracts import current_execution_context
        context = current_execution_context()
        stable_id = EngineSessionStore._identity(connection, chat_id, context)
        if context is not None:
            purpose = context.purpose
        if profile_id is None:
            row = connection.execute("SELECT state_json FROM codex_thread_state WHERE chat_id=?", (str(chat_id),)).fetchone()
        else:
            row = connection.execute("SELECT state_json FROM engine_sessions WHERE chat_id=? AND engine='codex' AND profile_id=? AND purpose=?", (stable_id, profile_id, purpose)).fetchone()
        return json.loads(row[0]) if row else None
