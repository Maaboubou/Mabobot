"""Persistent Codex App Server client used by the chatbot.

The App Server process lives with the FastAPI application.  Each WeChat chat
is mapped to a persisted Codex thread and receives only messages that were not
already sent to that thread.  Other LLM call types continue to use the
stateless Codex CLI adapter.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from app.services.codex_job_manager import codex_job_manager
from app.services.codex_browser_tool import (
    BrowserToolContext,
    CodexBrowserToolError,
    CodexBrowserToolService,
    get_codex_browser_tool,
)
from app.services.file_tools_runtime import (
    build_codex_runtime_command,
    get_file_tools_runtime,
    runtime_permission_roots,
)
from app.utils.subprocess_utils import hidden_process_kwargs
from app.services.codex_proxy.client import (
    CODEX_APPROVAL_POLICY,
    CODEX_APPROVALS_REVIEWER,
    CodexProxyError,
    _auto_review_config_args,
    _artifact_output_dir_is_safe,
    _artifact_request_dir_is_safe,
    _as_bool,
    _as_runtime_path,
    _collect_artifact_attachments,
    _detect_runtime_file_commands,
    _direct_image_request_mode,
    _is_link_like,
    _materialize_codex_generated_image,
    _permission_profile_config_args,
    _prepare_artifact_output_dir,
    _cleanup_expired_artifacts,
    _content_to_text,
    _default_text_for_attachments,
    _stage_input_files,
    _successful_generated_image_paths,
    _write_image_url_to_file,
    _write_artifact_manifest,
    estimate_codex_usage,
    extract_image_urls,
    normalize_codex_web_search_mode,
    render_chat_prompt,
)


logger = logging.getLogger(__name__)


_EMPTY_RESPONSE_RECOVERY_PROMPT = (
    "This is a one-shot recovery turn for the immediately preceding user request. "
    "The preceding turn completed without an assistant final answer. Do not call any tools, "
    "do not browse or search, and do not continue analysis. Using only the conversation, "
    "images, and tool results already present in this same thread, answer the pending user "
    "request now. Return only the final assistant answer and follow the required output schema "
    "when one is configured."
)
_DYNAMIC_TOOL_BIND_TIMEOUT_SECONDS = 2.0


class CodexAppServerError(CodexProxyError):
    """Raised when the persistent Codex App Server cannot serve a turn."""


class _ResumeThreadError(CodexAppServerError):
    """Raised when a saved Codex thread can no longer be resumed."""


@dataclass
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Optional[str] = None


@dataclass
class _TurnTracker:
    completed: threading.Event = field(default_factory=threading.Event)
    usage_ready: threading.Event = field(default_factory=threading.Event)
    final_messages: List[str] = field(default_factory=list)
    unclassified_messages: List[str] = field(default_factory=list)
    commentary_messages: List[str] = field(default_factory=list)
    reasoning: List[str] = field(default_factory=list)
    generated_image_paths: List[str] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    usage_source: str = ""
    usage_segments: List[Dict[str, Any]] = field(default_factory=list)
    usage_signatures: Dict[str, int] = field(default_factory=dict)
    compacted: bool = False
    status: str = "running"
    error: Optional[str] = None
    request_id: str = ""
    thread_id: str = ""
    started_recorded: bool = False


@dataclass
class _MessageDelta:
    messages: List[Dict[str, Any]]
    stable_fingerprints: List[str]
    ephemeral_fingerprints: List[str]
    resume: bool
    rotation_reason: Optional[str] = None


@dataclass
class _ActiveDynamicToolContext:
    token: str
    browser: BrowserToolContext
    turn_ids: set[str] = field(default_factory=set)
    call_ids: set[str] = field(default_factory=set)
    history: Any = None
    history_exports: List[Path] = field(default_factory=list)


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _nonnegative_int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _message_fingerprint(message: Dict[str, Any]) -> str:
    """Fingerprint semantic text while intentionally excluding image bytes."""
    normalized = {
        "role": str(message.get("role") or "user"),
        "name": str(message.get("name") or ""),
        "content": _content_to_text(message.get("content")),
    }
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_ephemeral_message(message: Dict[str, Any]) -> bool:
    return str(message.get("name") or "") in {"search_context", "history_context"}


def _split_message_fingerprints(
    messages: Iterable[Dict[str, Any]],
) -> Tuple[List[str], List[str]]:
    stable: List[str] = []
    ephemeral: List[str] = []
    for message in messages:
        fingerprint = _message_fingerprint(message)
        if _is_ephemeral_message(message):
            ephemeral.append(fingerprint)
        else:
            stable.append(fingerprint)
    return stable, ephemeral


def _plan_message_delta(
    messages: List[Dict[str, Any]],
    state: Optional[Dict[str, Any]],
    *,
    model: str,
    reasoning_effort: str,
    retry: bool = False,
    rotate_tokens: int = 0,
    max_turns: int = 0,
    runtime_profile: str = "",
    access_signature: str = "",
    dynamic_tool_signature: str = "",
    max_compactions: int = 0,
    idle_rotate_seconds: int = 0,
    incremental_context: bool = False,
) -> _MessageDelta:
    """Return the unsent message suffix or explain why a new thread is needed."""
    stable, ephemeral = _split_message_fingerprints(messages)
    if not state or not state.get("thread_id"):
        return _MessageDelta(
            list(messages),
            stable,
            ephemeral,
            False,
            str((state or {}).get("pending_rotation_reason") or "no_saved_thread"),
        )
    if str(state.get("runtime_profile") or "") != str(runtime_profile or ""):
        return _MessageDelta(list(messages), stable, ephemeral, False, "runtime_profile_changed")
    if access_signature and str(state.get("access_signature") or "") != access_signature:
        return _MessageDelta(list(messages), stable, ephemeral, False, "access_policy_changed")
    if dynamic_tool_signature and str(state.get("dynamic_tool_signature") or "") != dynamic_tool_signature:
        return _MessageDelta(list(messages), stable, ephemeral, False, "toolset_changed")
    if str(state.get("model") or "") != model:
        return _MessageDelta(list(messages), stable, ephemeral, False, "model_changed")
    if str(state.get("reasoning_effort") or "") != reasoning_effort:
        return _MessageDelta(list(messages), stable, ephemeral, False, "reasoning_effort_changed")
    if max_turns > 0 and int(state.get("turn_count") or 0) >= max_turns:
        return _MessageDelta(list(messages), stable, ephemeral, False, "max_turns_reached")
    thread_input_tokens = int(
        state.get("thread_input_tokens")
        if state.get("thread_input_tokens") is not None
        else state.get("last_input_tokens")
        or 0
    )
    if rotate_tokens > 0 and thread_input_tokens >= rotate_tokens:
        return _MessageDelta(list(messages), stable, ephemeral, False, "soft_token_limit")
    if max_compactions > 0 and int(state.get("compaction_count") or 0) >= max_compactions:
        return _MessageDelta(list(messages), stable, ephemeral, False, "compaction_limit")
    if idle_rotate_seconds > 0:
        try:
            updated_at = datetime.fromisoformat(str(state.get("updated_at") or ""))
            if time.time() - updated_at.timestamp() >= idle_rotate_seconds:
                return _MessageDelta(list(messages), stable, ephemeral, False, "idle_timeout")
        except (TypeError, ValueError):
            pass

    if incremental_context:
        systems = [_message_fingerprint(m) for m in messages if m.get("role") in {"system", "developer"}]
        if state.get("context_policy") != "incremental":
            return _MessageDelta(list(messages), stable, ephemeral, False, "context_policy_changed")
        if systems != state.get("incremental_system_fingerprints"):
            return _MessageDelta(list(messages), stable, ephemeral, False, "role_instructions_changed")
        suffix = [m for m in messages if m.get("role") not in {"system", "developer"}]
        if retry:
            previous = set(state.get("incremental_input_fingerprints") or [])
            suffix = [m for m in suffix if _message_fingerprint(m) not in previous]
        return _MessageDelta(suffix, stable, ephemeral, True)

    sent_stable = list(state.get("stable_fingerprints") or [])
    if len(stable) < len(sent_stable) or stable[: len(sent_stable)] != sent_stable:
        return _MessageDelta(list(messages), stable, ephemeral, False, "message_prefix_changed")

    # Preserve original order: ephemeral search/history context normally sits
    # directly before the newly appended user message.
    suffix: List[Dict[str, Any]] = []
    stable_index = 0
    duplicate_retry_ephemeral = retry and ephemeral == list(
        state.get("last_ephemeral_fingerprints") or []
    )
    for message in messages:
        if _is_ephemeral_message(message):
            if not duplicate_retry_ephemeral:
                suffix.append(message)
            continue
        if stable_index >= len(sent_stable):
            suffix.append(message)
        stable_index += 1

    return _MessageDelta(suffix, stable, ephemeral, True)


def _normalize_app_server_usage(
    raw: Dict[str, Any],
    *,
    source: str = "codex_app_server.thread/tokenUsage/updated",
) -> Dict[str, Any]:
    """Normalize official and third-party App Server usage shapes."""
    if not isinstance(raw, dict):
        return {}
    nested_usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    payload = nested_usage or raw
    last = next(
        (
            value
            for value in (
                raw.get("last"),
                raw.get("lastUsage"),
                raw.get("last_usage"),
                payload,
            )
            if isinstance(value, dict)
        ),
        {},
    )
    total = next(
        (
            value
            for value in (
                raw.get("total"),
                raw.get("sessionTotal"),
                raw.get("session_total"),
                payload.get("sessionTotal"),
                payload.get("session_total"),
            )
            if isinstance(value, dict)
        ),
        {},
    )

    def number(value: Dict[str, Any], *keys: str) -> int:
        for key in keys:
            try:
                if value.get(key) is not None:
                    return max(0, int(value.get(key) or 0))
            except (TypeError, ValueError):
                continue
        return 0

    prompt_details = last.get("prompt_tokens_details")
    if not isinstance(prompt_details, dict):
        prompt_details = last.get("input_tokens_details")
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    completion_details = last.get("completion_tokens_details")
    if not isinstance(completion_details, dict):
        completion_details = last.get("output_tokens_details")
    if not isinstance(completion_details, dict):
        completion_details = {}

    cache_write_tokens = number(prompt_details, "cache_write_tokens", "cache_creation_tokens") or number(last, "cacheWriteTokens", "cache_write_tokens", "cache_creation_input_tokens")
    cache_data_available = any(
        key in last
        for key in (
            "cachedInputTokens",
            "cached_input_tokens",
            "cachedTokens",
            "cached_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "cachedContentTokenCount",
        )
    ) or any(key in prompt_details for key in ("cached_tokens", "cachedTokens"))

    input_tokens = number(
        last,
        "inputTokens",
        "input_tokens",
        "promptTokens",
        "prompt_tokens",
        "promptTokenCount",
    )
    anthropic_cache_read = number(last, "cache_read_input_tokens")
    anthropic_cache_write = number(last, "cache_creation_input_tokens")
    if anthropic_cache_read or anthropic_cache_write:
        input_tokens += anthropic_cache_read + anthropic_cache_write
    cached_tokens = number(
        last,
        "cachedInputTokens",
        "cached_input_tokens",
        "cachedTokens",
        "cached_tokens",
        "cachedContentTokenCount",
    ) or anthropic_cache_read
    cached_tokens = min(cached_tokens or number(prompt_details, "cached_tokens", "cachedTokens"), input_tokens)
    output_tokens = number(
        last,
        "outputTokens",
        "output_tokens",
        "completionTokens",
        "completion_tokens",
        "candidatesTokenCount",
    )
    reasoning_tokens = number(
        last,
        "reasoningOutputTokens",
        "reasoning_output_tokens",
        "reasoningTokens",
        "reasoning_tokens",
        "thoughtsTokenCount",
    )
    reasoning_tokens = min(
        reasoning_tokens or number(completion_details, "reasoning_tokens", "reasoningTokens"),
        output_tokens,
    )
    total_tokens = number(last, "totalTokens", "total_tokens", "totalTokenCount") or input_tokens + output_tokens
    if not any((input_tokens, output_tokens, total_tokens)):
        return {}

    usage: Dict[str, Any] = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_miss_input_tokens": max(input_tokens - cached_tokens, 0),
        "prompt_tokens_details": {"cached_tokens": cached_tokens, "cache_write_tokens": cache_write_tokens},
        "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        "estimated": False,
        "source": str(raw.get("source") or source),
        "cache_data_available": cache_data_available,
    }
    context_window = number(
        raw,
        "modelContextWindow",
        "model_context_window",
        "contextWindow",
        "context_window",
    ) or number(
        payload,
        "modelContextWindow",
        "model_context_window",
        "contextWindow",
        "context_window",
    )
    if context_window > 0:
        usage["model_context_window"] = context_window
    if total:
        total_input = number(
            total,
            "inputTokens",
            "input_tokens",
            "promptTokens",
            "prompt_tokens",
            "promptTokenCount",
        )
        total_cache_read = number(total, "cache_read_input_tokens")
        total_cache_write = number(total, "cache_creation_input_tokens")
        if total_cache_read or total_cache_write:
            total_input += total_cache_read + total_cache_write
        total_cached = min(
            number(
                total,
                "cachedInputTokens",
                "cached_input_tokens",
                "cachedTokens",
                "cached_tokens",
                "cachedContentTokenCount",
            )
            or total_cache_read,
            total_input,
        )
        total_output = number(
            total,
            "outputTokens",
            "output_tokens",
            "completionTokens",
            "completion_tokens",
            "candidatesTokenCount",
        )
        total_reasoning = min(
            number(
                total,
                "reasoningOutputTokens",
                "reasoning_output_tokens",
                "reasoningTokens",
                "reasoning_tokens",
                "thoughtsTokenCount",
            ),
            total_output,
        )
        usage["session_total"] = {
            "input_tokens": total_input,
            "cached_input_tokens": total_cached,
            "output_tokens": total_output,
            "reasoning_output_tokens": total_reasoning,
            "total_tokens": number(total, "totalTokens", "total_tokens", "totalTokenCount")
            or total_input + total_output,
        }
    return usage


def _accumulate_usage_total(
    previous: Optional[Dict[str, Any]],
    usage: Dict[str, Any],
) -> Dict[str, int]:
    """Add one normalized turn to a logical-session token total."""
    base = previous if isinstance(previous, dict) else {}
    prompt_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    completion_details = usage.get("completion_tokens_details")
    if not isinstance(completion_details, dict):
        completion_details = {}
    cached = int(prompt_details.get("cached_tokens") or 0)
    reasoning = int(completion_details.get("reasoning_tokens") or 0)
    values = {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "cached_input_tokens": cached,
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "reasoning_output_tokens": reasoning,
        "total_tokens": int(usage.get("total_tokens") or 0),
    }
    return {
        key: max(0, int(base.get(key) or 0)) + max(0, value)
        for key, value in values.items()
    }


def _context_remaining_percent(total_tokens: Optional[int], window: int) -> Optional[int]:
    """Match Codex CLI's user-controllable context estimate (12K baseline)."""
    if total_tokens is None or window <= 0:
        return None
    if window <= 12000:
        return 0
    used = max(0, total_tokens - 12000)
    remaining = max(0, window - 12000 - used)
    return min(100, int(remaining * 100 / (window - 12000) + 0.5))


def _merge_attempt_usage(usages: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge the provider-reported usage of one logical request's retry turns."""
    attempts = [dict(item) for item in usages if isinstance(item, dict) and item]
    if not attempts:
        return {}
    if len(attempts) == 1:
        return attempts[0]

    merged = dict(attempts[-1])
    merged["prompt_tokens"] = sum(int(item.get("prompt_tokens") or 0) for item in attempts)
    merged["completion_tokens"] = sum(
        int(item.get("completion_tokens") or 0) for item in attempts
    )
    merged["total_tokens"] = sum(int(item.get("total_tokens") or 0) for item in attempts)
    merged["cache_miss_input_tokens"] = sum(
        int(item.get("cache_miss_input_tokens") or 0) for item in attempts
    )
    merged["prompt_tokens_details"] = {
        "cache_write_tokens": sum(int((item.get("prompt_tokens_details") or {}).get("cache_write_tokens") or 0) for item in attempts),
        "cached_tokens": sum(
            int((item.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
            for item in attempts
            if isinstance(item.get("prompt_tokens_details"), dict)
        )
    }
    merged["completion_tokens_details"] = {
        "reasoning_tokens": sum(
            int((item.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
            for item in attempts
            if isinstance(item.get("completion_tokens_details"), dict)
        )
    }
    merged["estimated"] = any(bool(item.get("estimated")) for item in attempts)
    merged["cache_data_available"] = all(
        bool(item.get("cache_data_available")) for item in attempts
    )
    merged["source"] = "codex_app_server.empty_response_retry_aggregate"

    # A provider's latest cumulative session total already includes both turns.
    # Preserve it instead of summing cumulative values and double-counting history.
    for item in reversed(attempts):
        session_total = item.get("session_total")
        if isinstance(session_total, dict):
            merged["session_total"] = dict(session_total)
            break
    return merged


def _merge_usage_accuracy(previous: Any, current: Any) -> str:
    """Describe whether an accumulated total is reported, estimated or partial."""
    prior = str(previous or "").strip().lower()
    latest = str(current or "unknown").strip().lower()
    if prior not in {"reported", "estimated", "partial", "unknown"}:
        prior = ""
    if latest not in {"reported", "estimated", "partial", "unknown"}:
        latest = "unknown"
    if not prior:
        return latest
    if "partial" in {prior, latest} or "unknown" in {prior, latest}:
        return "partial" if prior != "unknown" or latest != "unknown" else "unknown"
    if "estimated" in {prior, latest}:
        return "estimated"
    return "reported"


class CodexThreadStateStore:
    """SQLite-backed chat-to-thread state shared by every runtime worker."""

    def __init__(self, path: Optional[Path] = None) -> None:
        configured_path = path or os.getenv("CODEX_RUNTIME_STATE_DB") or "data/codex_runtime.db"
        self.path = Path(configured_path)
        self.legacy_path = Path(
            os.getenv("CODEX_APP_SERVER_THREAD_STATE")
            or "data/codex_app_server_threads.json"
        )
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._migrate_legacy_json()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._lock:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS codex_thread_state (
                        chat_id TEXT PRIMARY KEY,
                        state_json TEXT NOT NULL,
                        codex_version TEXT,
                        schema_hash TEXT,
                        config_signature TEXT,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_codex_thread_state_updated "
                    "ON codex_thread_state(updated_at)"
                )

    def _migrate_legacy_json(self) -> None:
        if not self.legacy_path.exists() or self.legacy_path.resolve() == self.path.resolve():
            return
        try:
            payload = json.loads(self.legacy_path.read_text(encoding="utf-8"))
            states = payload.get("threads") if isinstance(payload, dict) else None
            if not isinstance(states, dict) or not states:
                return
            with self._lock, closing(self._connect()) as connection, connection:
                existing = connection.execute(
                    "SELECT COUNT(*) AS count FROM codex_thread_state"
                ).fetchone()
                if existing and int(existing["count"] or 0) > 0:
                    return
                now = datetime.now().isoformat()
                for chat_id, state in states.items():
                    if not isinstance(state, dict):
                        continue
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO codex_thread_state
                        (chat_id, state_json, codex_version, schema_hash, config_signature, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(chat_id),
                            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                            str(state.get("codex_version") or ""),
                            str(state.get("schema_hash") or ""),
                            str(state.get("config_signature") or ""),
                            str(state.get("updated_at") or now),
                        ),
                    )
            logger.info("Imported %s Codex thread states into %s", len(states), self.path)
        except Exception as exc:
            logger.warning("Failed to import Codex thread state %s: %s", self.legacy_path, exc)

    def get(self, chat_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT state_json FROM codex_thread_state WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchone()
        if not row:
            return None
        try:
            state = json.loads(row["state_json"])
            return state if isinstance(state, dict) else None
        except Exception:
            logger.warning("Invalid Codex thread state for chat %s", chat_id)
            return None

    def list_all(self) -> Dict[str, Dict[str, Any]]:
        """Return a detached snapshot for read-only monitoring APIs."""
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT chat_id, state_json FROM codex_thread_state ORDER BY updated_at DESC"
            ).fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            try:
                state = json.loads(row["state_json"])
            except Exception:
                continue
            if isinstance(state, dict):
                result[str(row["chat_id"])] = state
        return result

    def put(self, chat_id: str, state: Dict[str, Any]) -> None:
        payload = dict(state)
        updated_at = str(payload.get("updated_at") or datetime.now().isoformat())
        payload["updated_at"] = updated_at
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO codex_thread_state
                (chat_id, state_json, codex_version, schema_hash, config_signature, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    codex_version = excluded.codex_version,
                    schema_hash = excluded.schema_hash,
                    config_signature = excluded.config_signature,
                    updated_at = excluded.updated_at
                """,
                (
                    str(chat_id),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    str(payload.get("codex_version") or ""),
                    str(payload.get("schema_hash") or ""),
                    str(payload.get("config_signature") or ""),
                    updated_at,
                ),
            )

    def delete(self, chat_id: str) -> bool:
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM codex_thread_state WHERE chat_id = ?",
                (str(chat_id),),
            )
        return bool(cursor.rowcount)

    def request_rotation(self, chat_id: str, *, reason: str = "manual_reset") -> bool:
        """Detach the physical thread while retaining logical-session totals."""
        normalized = str(chat_id or "").strip()
        with self._lock:
            state = self.get(normalized)
            if not state:
                return False
            if not state.get("thread_id") and state.get("pending_rotation_reason"):
                return True
            zero_total = _accumulate_usage_total(None, {})
            state.update(
                {
                    "thread_id": None,
                    "last_turn_id": None,
                    "stable_fingerprints": [],
                    "last_input_stable_fingerprints": [],
                    "last_ephemeral_fingerprints": [],
                    "turn_count": 0,
                    "compaction_count": 0,
                    "thread_input_tokens": 0,
                    "session_total": zero_total,
                    "session_usage_accuracy": "unknown",
                    "thread_usage_accuracy": "unknown",
                    "continuity_status": "rotation_pending",
                    "pending_rotation_reason": str(reason or "manual_reset"),
                    "rotation_requested_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat(),
                }
            )
            self.put(normalized, state)
            return True


class CodexAppServerManager:
    """Own a Codex App Server process and multiplex persistent chat threads."""

    def __init__(
        self,
        *,
        codex_bin: Optional[str] = None,
        workdir: Optional[str] = None,
        state_path: Optional[Path] = None,
        state_store: Optional[CodexThreadStateStore] = None,
        instance_name: str = "interactive-1",
        codex_version: str = "",
        schema_hash: str = "",
        experimental_api: bool = False,
        permission_read_roots: Optional[Iterable[str]] = None,
        browser_tool: Optional[CodexBrowserToolService] = None,
    ) -> None:
        configured_bin = codex_bin or os.getenv("CODEX_PROXY_BIN")
        self.codex_bin = configured_bin or shutil.which("codex") or "codex"
        self.workdir = str(Path(workdir or os.getenv("CODEX_PROXY_WORKDIR") or Path.cwd()).resolve())
        self.use_wsl = os.name == "nt" and _as_bool(os.getenv("CODEX_PROXY_USE_WSL"), True)
        self.enabled = _as_bool(os.getenv("CODEX_APP_SERVER_ENABLED"), True)
        self.startup_timeout = _positive_int(os.getenv("CODEX_APP_SERVER_STARTUP_TIMEOUT"), 30)
        self.rotate_tokens = _nonnegative_int(
            os.getenv("CODEX_APP_SERVER_ROTATE_TOKENS", "220000"),
            220000,
        )
        self.max_compactions = _nonnegative_int(
            os.getenv("CODEX_APP_SERVER_MAX_COMPACTIONS", "2"),
            2,
        )
        self.idle_rotate_seconds = _nonnegative_int(
            os.getenv("CODEX_APP_SERVER_IDLE_ROTATE_SECONDS", "2592000"),
            2592000,
        )
        self.context_safety_tokens = _nonnegative_int(
            os.getenv("CODEX_APP_SERVER_CONTEXT_SAFETY_TOKENS", "32768"),
            32768,
        )
        self.state_store = state_store or CodexThreadStateStore(state_path)
        self.instance_name = str(instance_name or "codex")
        self.codex_version = str(codex_version or "")
        self.schema_hash = str(schema_hash or "")
        self.experimental_api = bool(experimental_api)
        self.permission_read_roots = tuple(
            dict.fromkeys(
                str(path).strip()
                for path in permission_read_roots or ()
                if str(path).strip().startswith("/")
            )
        )
        self.browser_tool = browser_tool or get_codex_browser_tool()
        self.dynamic_tool_specs = (
            self.browser_tool.dynamic_tool_specs() if self.browser_tool.enabled else []
        )
        from app.history.tools import dynamic_tool_specs as history_specs
        self.dynamic_tool_specs += history_specs()
        self.dynamic_tool_signature = self.browser_tool.tool_signature() + ":history-v4-local-files"

        self._lifecycle_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.RLock()
        self._turn_lock = threading.RLock()
        self._chat_locks_lock = threading.Lock()
        self._chat_locks: Dict[str, threading.Lock] = {}
        self._pending: Dict[int, _PendingRequest] = {}
        self._notification_lock = threading.RLock()
        self._dynamic_tool_lock = threading.RLock()
        self._dynamic_tool_condition = threading.Condition(self._dynamic_tool_lock)
        self._dynamic_tool_contexts: Dict[str, _ActiveDynamicToolContext] = {}
        self._notification_handlers: Dict[str, List[Callable[[Dict[str, Any]], None]]] = {}
        self._turns: Dict[str, _TurnTracker] = {}
        self._loaded_threads: Dict[str, str] = {}
        self._request_id = 0
        self._generation = 0
        self._proc: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stopping = False
        self._initialize_result: Dict[str, Any] = {}
        self._stderr_tail: List[str] = []
        self._last_error = ""

    def _command(self) -> List[str]:
        executable = self.codex_bin
        args = [executable, "app-server", "--listen", "stdio://"]
        args.extend(_auto_review_config_args())
        runtime_capabilities = (
            get_file_tools_runtime(use_wsl=True, codex_bin=executable)
            if self.use_wsl
            else None
        )
        permission_read_roots = (
            *runtime_permission_roots(runtime_capabilities),
            *self.permission_read_roots,
        )
        args.extend(
            _permission_profile_config_args(
                "mabobot-chat-isolated",
                runtime_uid=(runtime_capabilities or {}).get("uid"),
                runtime_read_roots=permission_read_roots,
            )
        )
        args.extend(["-c", "features.memories=false", "-c", "features.external_agent_memory_import=false"])
        return build_codex_runtime_command(
            args,
            use_wsl=self.use_wsl,
            snapshot=runtime_capabilities,
        )

    def is_running(self) -> bool:
        proc = self._proc
        return bool(proc is not None and proc.poll() is None)

    def status(self) -> Dict[str, Any]:
        proc = self._proc
        return {
            "name": self.instance_name,
            "enabled": self.enabled,
            "running": self.is_running(),
            "pid": getattr(proc, "pid", None),
            "generation": self._generation,
            "loaded_threads": len(self._loaded_threads),
            "user_agent": self._initialize_result.get("userAgent"),
            "codex_home": self._initialize_result.get("codexHome"),
            "codex_version": self.codex_version,
            "schema_hash": self.schema_hash,
            "api_surface": "experimental" if self.experimental_api else "stable",
            "approval_policy": CODEX_APPROVAL_POLICY,
            "approvals_reviewer": CODEX_APPROVALS_REVIEWER,
            "rotate_tokens": self.rotate_tokens,
            "max_compactions": self.max_compactions,
            "idle_rotate_seconds": self.idle_rotate_seconds,
            "last_error": self._last_error or None,
            "last_diagnostic": self._stderr_tail[-1] if self._stderr_tail else None,
            "browser_tool": self.browser_tool.status(),
            "dynamic_tool_count": sum(
                len(spec.get("tools") or []) if spec.get("type") == "namespace" else 1
                for spec in self.dynamic_tool_specs
            ),
        }

    def invalidate_chat(self, chat_id: str, *, reason: str = "manual_reset") -> None:
        """Forget a chat mapping so the next call starts a clean thread."""
        self.state_store.request_rotation(str(chat_id or ""), reason=reason)

    def delete_thread(self, thread_id: str, *, timeout: int = 30) -> None:
        """Permanently delete a persisted Codex thread from the local runtime."""
        normalized = str(thread_id or "").strip()
        if not normalized:
            return
        self._request("thread/delete", {"threadId": normalized}, timeout=timeout)
        self._loaded_threads.pop(normalized, None)

    def forget_loaded_thread(self, thread_id: str) -> None:
        """Drop a deleted thread from this worker's local resume cache."""
        self._loaded_threads.pop(str(thread_id or ""), None)

    def session_snapshot(
        self,
        active_jobs: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Build a public view of persisted chatbot threads and active turns."""
        jobs = active_jobs if active_jobs is not None else codex_job_manager.list_active()
        active_by_chat: Dict[str, Dict[str, Any]] = {}
        for job in jobs:
            if not isinstance(job, dict):
                continue
            chat_id = str(job.get("chat_id") or "").strip()
            if chat_id:
                active_by_chat[chat_id] = job

        states = self.state_store.list_all()
        chat_ids = set(states) | set(active_by_chat)
        sessions: List[Dict[str, Any]] = []
        for chat_id in chat_ids:
            state = states.get(chat_id, {})
            active = active_by_chat.get(chat_id, {})
            session_total = (
                state.get("session_total")
                if isinstance(state.get("session_total"), dict)
                else {}
            )
            lifetime_total = (
                state.get("lifetime_total")
                if isinstance(state.get("lifetime_total"), dict)
                else session_total
            )
            last_input = int(state.get("last_input_tokens") or 0)
            # Old states used aggregate billing here. Never present that as context.
            context_total = active.get("context_total_tokens") if active else state.get("context_total_tokens")
            context_input = int(context_total or 0)
            last_cached = min(int(state.get("last_cached_input_tokens") or 0), last_input)
            last_completion = int(
                state.get("last_completion_tokens")
                or max(int(state.get("last_total_tokens") or 0) - last_input, 0)
            )
            context_window = int(active.get("model_context_window") or state.get("model_context_window") or 0)
            usage_accuracy = str(state.get("usage_accuracy") or "").strip().lower()
            if usage_accuracy not in {"reported", "estimated", "unknown"}:
                usage_accuracy = (
                    "estimated"
                    if state.get("usage_estimated")
                    else "reported"
                    if state.get("usage_source") and int(state.get("last_total_tokens") or 0) > 0
                    else "unknown"
                )
            cache_available = bool(state.get("usage_cache_available", False))
            thread_usage_accuracy = str(
                state.get("thread_usage_accuracy") or usage_accuracy
            ).strip().lower()
            context_remaining_percent = _context_remaining_percent(context_total, context_window)
            context_usage_percent = 100 - context_remaining_percent if context_remaining_percent is not None else None
            cache_hit_rate = (
                round(last_cached / last_input, 6)
                if last_input > 0 and cache_available
                else None
            )
            sessions.append(
                {
                    "chat_id": chat_id,
                    "role_name": state.get("role_name"),
                    "thread_id": active.get("thread_id") or state.get("thread_id"),
                    "turn_id": active.get("turn_id") or state.get("last_turn_id"),
                    "request_id": active.get("request_id"),
                    "status": str(active.get("status") or "idle"),
                    "model": active.get("model") or state.get("last_model") or state.get("model"),
                    "reasoning_effort": active.get("reasoning_effort") or state.get("reasoning_effort"),
                    "reasoning_summary": active.get("reasoning_summary") or state.get("reasoning_summary"),
                    "web_search_mode": active.get("web_search_mode") or state.get("web_search_mode"),
                    "turn_count": int(state.get("turn_count") or 0),
                    "lifetime_turn_count": int(
                        state.get("lifetime_turn_count") or state.get("turn_count") or 0
                    ),
                    "thread_generation": int(state.get("thread_generation") or 0),
                    "compaction_count": int(state.get("compaction_count") or 0),
                    "lifetime_compaction_count": int(
                        state.get("lifetime_compaction_count")
                        or state.get("compaction_count")
                        or 0
                    ),
                    "last_input_tokens": last_input,
                    "context_input_tokens": context_input,
                    "last_cached_input_tokens": last_cached,
                    "last_cache_miss_input_tokens": max(last_input - last_cached, 0),
                    "last_completion_tokens": last_completion,
                    "last_reasoning_tokens": int(state.get("last_reasoning_tokens") or 0),
                    "last_total_tokens": int(state.get("last_total_tokens") or 0),
                    "cache_hit_rate": cache_hit_rate,
                    "model_context_window": context_window or None,
                    "model_context_window_source": state.get("model_context_window_source"),
                    "context_usage_percent": context_usage_percent,
                    "context_remaining_percent": context_remaining_percent,
                    "context_total_tokens": context_total,
                    "configured_context_window": state.get("configured_context_window"),
                    "auto_compact_token_limit": state.get("auto_compact_token_limit"),
                    "session_total": session_total,
                    "lifetime_total": lifetime_total,
                    "usage_source": state.get("usage_source"),
                    "usage_accuracy": usage_accuracy,
                    "thread_usage_accuracy": thread_usage_accuracy,
                    "session_usage_accuracy": state.get("session_usage_accuracy")
                    or usage_accuracy,
                    "lifetime_usage_accuracy": state.get("lifetime_usage_accuracy")
                    or usage_accuracy,
                    "usage_estimated": usage_accuracy == "estimated",
                    "usage_cache_available": cache_available,
                    "runtime_profile": active.get("config_profile")
                    or state.get("pending_runtime_profile")
                    or state.get("runtime_profile"),
                    "access_mode": active.get("access_mode") or state.get("access_mode"),
                    "backend": active.get("backend") or state.get("last_backend"),
                    "continuity_status": active.get("continuity_status")
                    or state.get("continuity_status")
                    or "synchronized",
                    "last_rotation_reason": state.get("last_rotation_reason"),
                    "last_rotation_at": state.get("last_rotation_at"),
                    "last_compacted_at": state.get("last_compacted_at"),
                    "fallback_count": int(state.get("fallback_count") or 0),
                    "last_fallback_at": state.get("last_fallback_at"),
                    "last_fallback_reason": state.get("last_fallback_reason"),
                    "schema_hash": state.get("schema_hash"),
                    "active_prompt_chars": int(active.get("prompt_chars") or 0),
                    "updated_at": state.get("updated_at") or active.get("started_at"),
                }
            )

        def updated_timestamp(item: Dict[str, Any]) -> float:
            value = item.get("updated_at")
            if isinstance(value, (int, float)):
                return float(value)
            try:
                return datetime.fromisoformat(str(value or "")).timestamp()
            except (TypeError, ValueError):
                return 0.0

        sessions.sort(key=updated_timestamp, reverse=True)
        current_thread_total = sum(
            int((item.get("session_total") or {}).get("total_tokens") or 0)
            for item in sessions
        )
        current_thread_cached = sum(
            int((item.get("session_total") or {}).get("cached_input_tokens") or 0)
            for item in sessions
        )
        logical_lifetime_total = sum(
            int((item.get("lifetime_total") or {}).get("total_tokens") or 0)
            for item in sessions
        )
        return {
            "sessions": sessions,
            "stats": {
                "session_count": len(sessions),
                "active_session_count": sum(1 for item in sessions if item["status"] != "idle"),
                "total_turn_count": sum(int(item.get("turn_count") or 0) for item in sessions),
                "lifetime_turn_count": sum(
                    int(item.get("lifetime_turn_count") or 0) for item in sessions
                ),
                "current_thread_total_tokens": current_thread_total,
                "current_thread_cached_tokens": current_thread_cached,
                "logical_lifetime_total_tokens": logical_lifetime_total,
                "fallback_session_count": sum(
                    1 for item in sessions if int(item.get("fallback_count") or 0) > 0
                ),
                "pending_replay_count": sum(
                    1
                    for item in sessions
                    if item.get("continuity_status") == "pending_replay"
                ),
                "unknown_usage_session_count": sum(
                    1
                    for item in sessions
                    if item.get("lifetime_usage_accuracy") in {"unknown", "partial"}
                ),
                "estimated_usage_session_count": sum(
                    1
                    for item in sessions
                    if item.get("lifetime_usage_accuracy") == "estimated"
                ),
            },
        }

    def start(self, timeout: Optional[int] = None) -> bool:
        if not self.enabled:
            logger.info("Codex App Server is disabled; stateless codex exec remains available")
            return False
        with self._lifecycle_lock:
            if self.is_running():
                return True
            self._stopping = False
            self._stderr_tail.clear()
            self._last_error = ""
            self._fail_all("Codex App Server restarting")
            command = self._command()
            popen_kwargs: Dict[str, Any] = {
                "cwd": self.workdir,
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
            }
            if os.name == "nt":
                popen_kwargs.update(hidden_process_kwargs(new_process_group=True))
            else:
                popen_kwargs["start_new_session"] = True
            try:
                proc = subprocess.Popen(command, **popen_kwargs)
            except FileNotFoundError as exc:
                raise CodexAppServerError(
                    "Codex CLI/App Server was not found; check CODEX_PROXY_BIN or WSL Codex installation"
                ) from exc

            self._proc = proc
            self._loaded_threads.clear()
            self._stdout_thread = threading.Thread(
                target=self._stdout_loop,
                args=(proc,),
                name="codex-app-server-stdout",
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._stderr_loop,
                args=(proc,),
                name="codex-app-server-stderr",
                daemon=True,
            )
            self._stdout_thread.start()
            self._stderr_thread.start()
            try:
                initialize_params: Dict[str, Any] = {
                    "clientInfo": {
                        "name": "mabobot",
                        "title": "mabobot",
                        "version": "2.0.0",
                    },
                }
                if self.experimental_api:
                    initialize_params["capabilities"] = {"experimentalApi": True}
                self._initialize_result = self._request(
                    "initialize",
                    initialize_params,
                    timeout=timeout or self.startup_timeout,
                    ensure_started=False,
                )
                self._notify("initialized", {})
            except Exception as exc:
                self._last_error = str(exc)
                self._stop_process(proc)
                if self._proc is proc:
                    self._proc = None
                raise
            self._generation += 1
            logger.info(
                "Codex App Server started: pid=%s generation=%s user_agent=%s",
                proc.pid,
                self._generation,
                self._initialize_result.get("userAgent", "unknown"),
            )
            return True

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stopping = True
            proc = self._proc
            self._proc = None
            if proc is not None:
                self._stop_process(proc)
            self._loaded_threads.clear()
            self._fail_all("Codex App Server stopped")
            logger.info("Codex App Server stopped")

    def _stop_process(self, proc: subprocess.Popen) -> None:
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is not None:
            return
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=3)
            return
        except Exception:
            pass
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass

    def _stdout_loop(self, proc: subprocess.Popen) -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            for raw_line in stream:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Ignoring non-JSON Codex App Server stdout: %s", line[:500])
                    continue
                self._handle_message(message)
        except Exception:
            if not self._stopping:
                logger.exception("Codex App Server stdout reader failed")
        finally:
            if self._proc is proc:
                self._proc = None
                if not self._stopping:
                    logger.warning("Codex App Server exited unexpectedly with code %s", proc.poll())
                    detail = self._stderr_tail[-1] if self._stderr_tail else ""
                    self._last_error = (
                        "Codex App Server exited unexpectedly"
                        + (f": {detail}" if detail else "")
                    )
                    self._fail_all(self._last_error)

    def _stderr_loop(self, proc: subprocess.Popen) -> None:
        stream = proc.stderr
        if stream is None:
            return
        try:
            for raw_line in stream:
                line = raw_line.strip()
                if line:
                    self._stderr_tail.append(line[:2000])
                    if len(self._stderr_tail) > 20:
                        self._stderr_tail = self._stderr_tail[-20:]
                    logger.debug("Codex App Server stderr: %s", line[:2000])
        except Exception:
            if not self._stopping:
                logger.debug("Codex App Server stderr reader ended", exc_info=True)

    def _handle_message(self, message: Dict[str, Any]) -> None:
        if "method" in message and "id" in message:
            self._handle_server_request(message)
            return
        if "id" in message:
            request_id = message.get("id")
            with self._pending_lock:
                pending = self._pending.get(request_id)
            if pending is None:
                return
            if isinstance(message.get("error"), dict):
                error = message["error"]
                pending.error = str(error.get("message") or error)
            else:
                pending.result = message.get("result")
            pending.event.set()
            return
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        self._handle_notification(method, params)

    def _handle_server_request(self, message: Dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        if method == "item/tool/call":
            worker = threading.Thread(
                target=self._execute_dynamic_tool_request,
                args=(dict(message),),
                name="codex-dynamic-tool",
                daemon=True,
            )
            worker.start()
            return
        # Eligible escalation requests are handled by Codex Auto-review before
        # they reach this client. Anything still surfaced here requires a
        # client or human decision, so keep the fallback fail-closed instead of
        # turning Auto-review into unconditional approval.
        if method.endswith("/requestApproval") or "approval" in method.lower():
            logger.warning(
                "Codex approval request reached the client after Auto-review; declining: %s",
                method,
            )
            response = {"id": request_id, "result": {"decision": "decline"}}
        elif "elicitation" in method.lower():
            response = {"id": request_id, "result": {"action": "decline"}}
        else:
            response = {
                "id": request_id,
                "error": {"code": -32601, "message": f"Unsupported client method: {method}"},
            }
        try:
            self._send(response)
        except Exception:
            logger.debug("Failed to reject Codex App Server request %s", method, exc_info=True)

    def _claim_dynamic_tool_context(
        self,
        *,
        thread_id: str,
        turn_id: str,
        call_id: str,
    ) -> _ActiveDynamicToolContext:
        if not thread_id or not turn_id or not call_id:
            raise CodexBrowserToolError("dynamic browser call identifiers are incomplete")
        deadline = time.monotonic() + _DYNAMIC_TOOL_BIND_TIMEOUT_SECONDS
        with self._dynamic_tool_condition:
            active = self._dynamic_tool_contexts.get(thread_id)
            if active is None:
                raise CodexBrowserToolError("browser call is not bound to an active chat turn")
            while not active.turn_ids:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexBrowserToolError(
                        "browser call arrived before its chat turn was bound"
                    )
                self._dynamic_tool_condition.wait(timeout=remaining)
                active = self._dynamic_tool_contexts.get(thread_id)
                if active is None:
                    raise CodexBrowserToolError(
                        "browser call is not bound to an active chat turn"
                    )
            if turn_id not in active.turn_ids:
                raise CodexBrowserToolError("browser call turn does not match the active chat turn")
            if call_id in active.call_ids:
                raise CodexBrowserToolError("duplicate browser call was rejected")
            active.call_ids.add(call_id)
            return active

    def _execute_dynamic_tool_request(self, message: Dict[str, Any]) -> None:
        request_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        thread_id = str(params.get("threadId") or "")
        turn_id = str(params.get("turnId") or "")
        call_id = str(params.get("callId") or "")
        namespace = params.get("namespace")
        tool = params.get("tool")
        started_at = time.monotonic()
        active: Optional[_ActiveDynamicToolContext] = None
        success = False
        try:
            active = self._claim_dynamic_tool_context(
                thread_id=thread_id,
                turn_id=turn_id,
                call_id=call_id,
            )
            codex_job_manager.record_event(
                active.browser.request_id,
                "dynamic_tool_started",
                {
                    "namespace": str(namespace or ""),
                    "tool": str(tool or ""),
                    "turn_id": turn_id,
                    "call_id": call_id,
                },
                current_item_type="dynamicToolCall",
                current_item_status="in_progress",
                current_item_summary=" / ".join(
                    part for part in (str(namespace or ""), str(tool or "")) if part
                ),
            )
            normalized_tool = str(tool or "")
            if str(namespace or "") == "history" or normalized_tool.startswith("history."):
                if active.history is None:
                    raise CodexBrowserToolError("history is unavailable for this task")
                try:
                    result = active.history.execute(normalized_tool.removeprefix("history."), params.get("arguments"))
                except (ValueError, TypeError) as exc:
                    raise CodexBrowserToolError(str(exc)) from exc
            else:
                result = self.browser_tool.execute(
                    namespace=namespace,
                    tool=tool,
                    arguments=params.get("arguments"),
                    context=active.browser,
                )
            output_text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            success = True
        except CodexBrowserToolError as exc:
            output_text = json.dumps(
                {"error": str(exc), "tool": str(tool or "")},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except Exception:
            logger.exception("Unexpected Codex browser tool failure")
            output_text = json.dumps(
                {
                    "error": "browser tool failed unexpectedly; inspect application logs",
                    "tool": str(tool or ""),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )

        response = {
            "id": request_id,
            "result": {
                "success": success,
                "contentItems": [{"type": "inputText", "text": output_text}],
            },
        }
        try:
            self._send(response)
        except Exception:
            logger.debug("Failed to return Codex dynamic tool result", exc_info=True)
        if active is not None:
            elapsed_ms = max(0, int((time.monotonic() - started_at) * 1000))
            codex_job_manager.record_event(
                active.browser.request_id,
                "dynamic_tool_completed" if success else "dynamic_tool_failed",
                {
                    "namespace": str(namespace or ""),
                    "tool": str(tool or ""),
                    "turn_id": turn_id,
                    "call_id": call_id,
                    "success": success,
                    "duration_ms": elapsed_ms,
                },
                current_item_status="completed" if success else "failed",
            )

    def _tracker(self, turn_id: str) -> _TurnTracker:
        with self._turn_lock:
            return self._turns.setdefault(turn_id, _TurnTracker())

    @staticmethod
    def _append_unique(values: List[str], value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)

    def _read_completed_item(self, tracker: _TurnTracker, item: Dict[str, Any]) -> None:
        item_type = str(item.get("type") or "")
        if item_type == "contextCompaction":
            tracker.compacted = True
            return
        if item_type == "agentMessage":
            phase = str(item.get("phase") or "").strip().lower()
            if phase == "final_answer":
                self._append_unique(tracker.final_messages, item.get("text"))
            elif phase == "commentary":
                self._append_unique(tracker.commentary_messages, item.get("text"))
            else:
                self._append_unique(tracker.unclassified_messages, item.get("text"))
        elif item_type == "reasoning":
            summary = item.get("summary")
            content = item.get("content")
            if isinstance(summary, list):
                for part in summary:
                    self._append_unique(tracker.reasoning, part)
            else:
                self._append_unique(tracker.reasoning, summary)
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        self._append_unique(tracker.reasoning, part.get("text"))
                    else:
                        self._append_unique(tracker.reasoning, part)
        for saved_path in _successful_generated_image_paths(item):
            self._append_unique(tracker.generated_image_paths, saved_path)

    def _collect_response_attachments(
        self,
        *,
        output_dir: Path,
        turn_trackers: Iterable[_TurnTracker],
        image_request_mode: Optional[str],
        request_id: str,
    ) -> List[Dict[str, Any]]:
        """Collect outputs and host-recover direct imagegen results when needed."""
        if not _artifact_output_dir_is_safe(output_dir):
            codex_job_manager.record_event(
                request_id,
                "artifact_boundary_violation",
                {},
            )
            raise CodexAppServerError("Codex artifact output directory became unsafe")
        attachments = _collect_artifact_attachments(output_dir)
        if not image_request_mode or any(
            attachment.get("type") == "image" for attachment in attachments
        ):
            return attachments

        generated_paths: List[str] = []
        for tracker in turn_trackers:
            for saved_path in tracker.generated_image_paths:
                if saved_path and saved_path not in generated_paths:
                    generated_paths.append(saved_path)
        if not generated_paths:
            return attachments

        selected_paths = (
            generated_paths
            if image_request_mode == "multiple"
            else generated_paths[-1:]
        )
        materialized_paths: List[Path] = []
        for index, saved_path in enumerate(selected_paths, start=1):
            materialized = _materialize_codex_generated_image(
                saved_path,
                output_dir=output_dir,
                index=index,
                use_wsl=self.use_wsl,
            )
            if materialized is not None:
                materialized_paths.append(materialized)

        if len(materialized_paths) != len(selected_paths):
            for path in materialized_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove partial recovered image: %s", path)
            codex_job_manager.record_event(
                request_id,
                "image_artifact_recovery_failed",
                {
                    "generated_path_count": len(generated_paths),
                    "selected_path_count": len(selected_paths),
                    "materialized_path_count": len(materialized_paths),
                    "request_mode": image_request_mode,
                },
            )
            raise CodexAppServerError(
                "Codex App Server generated an image but could not materialize its attachment"
                if not materialized_paths
                else "Codex App Server generated image output was only partially materialized"
            )

        attachments = _collect_artifact_attachments(output_dir)
        recovered_images = [
            attachment for attachment in attachments if attachment.get("type") == "image"
        ]
        if recovered_images:
            codex_job_manager.record_event(
                request_id,
                "image_artifact_recovered",
                {
                    "attachment_count": len(recovered_images),
                    "generated_path_count": len(generated_paths),
                    "request_mode": image_request_mode,
                },
            )
            logger.info(
                "Codex App Server recovered %s generated image attachment(s) for request %s",
                len(recovered_images),
                request_id,
            )
            return attachments

        codex_job_manager.record_event(
            request_id,
            "image_artifact_recovery_failed",
            {
                "generated_path_count": len(generated_paths),
                "selected_path_count": len(selected_paths),
                "materialized_path_count": len(materialized_paths),
                "request_mode": image_request_mode,
            },
        )
        raise CodexAppServerError(
            "Codex App Server generated an image but could not materialize its attachment"
        )

    @staticmethod
    def _public_item_event(item: Dict[str, Any]) -> Dict[str, Any]:
        item_type = str(item.get("type") or "unknown")
        payload: Dict[str, Any] = {
            "item_id": str(item.get("id") or ""),
            "item_type": item_type,
            "status": str(item.get("status") or ""),
        }

        def text(value: Any, limit: int = 600) -> str:
            normalized = " ".join(str(value or "").split())
            if len(normalized) > limit:
                return normalized[: limit - 1] + "…"
            return normalized

        def public_command(value: Any) -> str:
            command = text(value)
            # Commands are useful operational telemetry, but common inline
            # credential forms must never be persisted in the public timeline.
            command = re.sub(
                r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)\b\s*[=:]\s*)([^\s]+)",
                r"\1[已隐藏]",
                command,
            )
            command = re.sub(
                r"(?i)(--(?:api[_-]?key|token|password|secret)\s+)([^\s]+)",
                r"\1[已隐藏]",
                command,
            )
            command = re.sub(r"(?i)(authorization:\s*bearer\s+)([^\s]+)", r"\1[已隐藏]", command)
            return command

        duration = item.get("durationMs")
        if isinstance(duration, (int, float)):
            payload["duration_ms"] = max(0, int(duration))

        if item_type == "commandExecution":
            command = public_command(item.get("command"))
            if command:
                payload["command"] = command
                payload["summary"] = command
            if item.get("cwd"):
                payload["cwd"] = text(item.get("cwd"), 300)
            if isinstance(item.get("exitCode"), int):
                payload["exit_code"] = int(item["exitCode"])
            actions = item.get("commandActions") if isinstance(item.get("commandActions"), list) else []
            action_types = [str(action.get("type")) for action in actions if isinstance(action, dict) and action.get("type")]
            if action_types:
                payload["action_types"] = action_types[:10]
        elif item_type == "fileChange":
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            public_changes: List[Dict[str, Any]] = []
            additions = 0
            deletions = 0
            for change in changes[:30]:
                if not isinstance(change, dict):
                    continue
                diff = str(change.get("diff") or "")
                additions += sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
                deletions += sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
                public_changes.append(
                    {
                        "path": text(change.get("path"), 300),
                        "kind": str(change.get("kind") or "update"),
                    }
                )
            payload.update(
                {
                    "changes": public_changes,
                    "change_count": len(changes),
                    "additions": additions,
                    "deletions": deletions,
                }
            )
            paths = [change["path"] for change in public_changes if change.get("path")]
            if paths:
                payload["summary"] = "、".join(paths[:3]) + (f" 等 {len(changes)} 个文件" if len(changes) > 3 else "")
        elif item_type == "mcpToolCall":
            server = text(item.get("server"), 120)
            tool = text(item.get("tool"), 160)
            payload.update({"server": server, "tool": tool})
            payload["summary"] = " / ".join(part for part in (server, tool) if part)
            if isinstance(item.get("readOnlyHint"), bool):
                payload["read_only"] = item["readOnlyHint"]
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            content = result.get("content") if isinstance(result.get("content"), list) else []
            if result:
                payload["result_items"] = len(content)
            error = item.get("error") if isinstance(item.get("error"), dict) else {}
            if error.get("message"):
                payload["error"] = text(error["message"], 500)
        elif item_type == "dynamicToolCall":
            namespace = text(item.get("namespace"), 120)
            tool = text(item.get("tool"), 160)
            payload.update({"namespace": namespace, "tool": tool})
            payload["summary"] = " / ".join(part for part in (namespace, tool) if part)
            if isinstance(item.get("success"), bool):
                payload["success"] = item["success"]
            content_items = item.get("contentItems") if isinstance(item.get("contentItems"), list) else []
            if item.get("contentItems") is not None:
                payload["result_items"] = len(content_items)
        elif item_type == "webSearch":
            query = text(item.get("query"), 500)
            if query:
                payload["query"] = query
                payload["summary"] = query
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            if action.get("type"):
                payload["action"] = str(action["type"])
            results = item.get("results") if isinstance(item.get("results"), list) else []
            if item.get("results") is not None:
                payload["result_count"] = len(results)
        elif item_type == "imageGeneration":
            if item.get("savedPath"):
                payload["saved_path"] = text(item.get("savedPath"), 300)
                payload["summary"] = payload["saved_path"]
            payload["transparent_background"] = bool(item.get("transparentBackground"))
        elif item_type == "imageView":
            if item.get("path"):
                payload["path"] = text(item.get("path"), 300)
                payload["summary"] = payload["path"]
        elif item_type == "agentMessage":
            payload["phase"] = str(item.get("phase") or "")
            payload["text_chars"] = len(str(item.get("text") or ""))
        elif item_type == "userMessage":
            content = item.get("content") if isinstance(item.get("content"), list) else []
            payload["content_items"] = len(content)

        return payload

    def _record_turn_event(
        self,
        tracker: _TurnTracker,
        event_type: str,
        details: Optional[Dict[str, Any]] = None,
        **updates: Any,
    ) -> bool:
        if not tracker.request_id:
            return False
        codex_job_manager.record_event(
            tracker.request_id,
            event_type,
            details or {},
            **updates,
        )
        return True

    def _handle_notification(self, method: str, params: Dict[str, Any]) -> None:
        with self._notification_lock:
            handlers = list(self._notification_handlers.get(method, ()))
        for handler in handlers:
            try:
                handler(dict(params))
            except Exception:
                logger.exception("Codex notification handler failed: %s", method)

        turn_id = str(params.get("turnId") or "")
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        if not turn_id:
            turn_id = str(turn.get("id") or "")
        if not turn_id:
            thread_id = str(params.get("threadId") or params.get("thread_id") or "")
            if thread_id:
                with self._turn_lock:
                    matching = [
                        key
                        for key, value in self._turns.items()
                        if value.thread_id == thread_id and not value.completed.is_set()
                    ]
                if len(matching) == 1:
                    turn_id = matching[0]
        if not turn_id:
            return
        tracker = self._tracker(turn_id)
        if method == "turn/started":
            if not tracker.started_recorded:
                tracker.started_recorded = self._record_turn_event(
                    tracker,
                    "turn_started",
                    {"turn_id": turn_id, "thread_id": tracker.thread_id},
                    status="running",
                    current_item_type=None,
                )
        elif method == "item/started":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            public_item = self._public_item_event(item)
            self._record_turn_event(
                tracker,
                "item_started",
                public_item,
                current_item_type=public_item["item_type"],
                current_item_status="in_progress",
                current_item_summary=public_item.get("summary"),
            )
        elif method == "item/completed":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            self._read_completed_item(tracker, item)
            public_item = self._public_item_event(item)
            self._record_turn_event(
                tracker,
                "item_completed",
                public_item,
                current_item_type=public_item["item_type"],
                current_item_status=public_item.get("status") or "completed",
                current_item_summary=public_item.get("summary"),
            )
        elif method == "thread/compacted":
            tracker.compacted = True
            self._record_turn_event(
                tracker,
                "context_compaction",
                {"turn_id": turn_id, "thread_id": tracker.thread_id},
            )
        elif re.sub(r"[^a-z]", "", method.lower()).endswith("tokenusageupdated"):
            tracker.usage = next(
                (
                    value
                    for value in (
                        params.get("tokenUsage"),
                        params.get("token_usage"),
                        params.get("usage"),
                    )
                    if isinstance(value, dict)
                ),
                {},
            )
            tracker.usage_source = f"codex_app_server.{method}"
            tracker.usage_ready.set()
            normalized = _normalize_app_server_usage(
                tracker.usage,
                source=tracker.usage_source,
            )
            # last is one model request; total is cumulative across the thread.
            # Repeated notifications for the same total must not be charged twice.
            cumulative = normalized.get("session_total") or {}
            identity = cumulative or normalized
            signature = json.dumps([identity.get(key) for key in ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens", "total_tokens")])
            if normalized:
                segment = {**normalized, "billing_scope": "request" if cumulative else "codex_turn"}
                existing = tracker.usage_signatures.get(signature)
                if existing is None:
                    tracker.usage_signatures[signature] = len(tracker.usage_segments)
                    tracker.usage_segments.append(segment)
                else:
                    tracker.usage_segments[existing] = segment
            self._record_turn_event(
                tracker,
                "token_usage",
                {
                    "prompt_tokens": normalized.get("prompt_tokens", 0),
                    "completion_tokens": normalized.get("completion_tokens", 0),
                    "total_tokens": normalized.get("total_tokens", 0),
                },
                context_total_tokens=normalized.get("total_tokens") if normalized else None,
                model_context_window=normalized.get("model_context_window"),
                prompt_tokens=normalized.get("prompt_tokens", 0),
                completion_tokens=normalized.get("completion_tokens", 0),
                total_tokens=normalized.get("total_tokens", 0),
            )
        elif method == "error":
            error = params.get("error") if isinstance(params.get("error"), dict) else {}
            if not params.get("willRetry"):
                tracker.error = str(error.get("message") or error or "Codex turn failed")
            self._record_turn_event(
                tracker,
                "error",
                {
                    "message": str(error.get("message") or error or "Codex turn failed")[:1000],
                    "will_retry": bool(params.get("willRetry")),
                },
            )
        elif method == "turn/completed":
            for item in turn.get("items") or []:
                if isinstance(item, dict):
                    self._read_completed_item(tracker, item)
            if not tracker.usage:
                completed_usage = next(
                    (
                        value
                        for value in (
                            turn.get("tokenUsage"),
                            turn.get("token_usage"),
                            turn.get("usage"),
                            params.get("tokenUsage"),
                            params.get("token_usage"),
                            params.get("usage"),
                        )
                        if isinstance(value, dict)
                    ),
                    {},
                )
                if completed_usage:
                    tracker.usage = completed_usage
                    tracker.usage_source = "codex_app_server.turn/completed"
                    tracker.usage_ready.set()
            tracker.status = str(turn.get("status") or "completed")
            error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
            if error:
                tracker.error = str(error.get("message") or error)
            self._record_turn_event(
                tracker,
                "turn_completed",
                {
                    "turn_id": turn_id,
                    "status": tracker.status,
                    "error": tracker.error,
                },
                current_item_type=None,
                current_item_status=None,
            )
            tracker.completed.set()

    def _send(self, message: Dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                raise CodexAppServerError("Codex App Server is not running")
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise CodexAppServerError("Codex App Server connection closed") from exc

    def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._send({"method": method, "params": params or {}})

    def add_notification_handler(
        self,
        method: str,
        handler: Callable[[Dict[str, Any]], None],
    ) -> Callable[[], None]:
        """Register a lightweight notification callback and return its remover."""
        normalized = str(method or "").strip()
        if not normalized:
            raise ValueError("Notification method is required")
        with self._notification_lock:
            self._notification_handlers.setdefault(normalized, []).append(handler)

        def remove() -> None:
            with self._notification_lock:
                values = self._notification_handlers.get(normalized, [])
                if handler in values:
                    values.remove(handler)
                if not values:
                    self._notification_handlers.pop(normalized, None)

        return remove

    def _request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: int = 30,
        ensure_started: bool = True,
    ) -> Any:
        if ensure_started:
            self.start()
        with self._pending_lock:
            self._request_id += 1
            request_id = self._request_id
            pending = _PendingRequest()
            self._pending[request_id] = pending
        try:
            self._send({"id": request_id, "method": method, "params": params or {}})
            if not pending.event.wait(timeout=max(1, timeout)):
                raise CodexAppServerError(f"Codex App Server {method} timed out after {timeout}s")
            if pending.error:
                raise CodexAppServerError(f"Codex App Server {method} failed: {pending.error}")
            return pending.result
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def _fail_all(self, error: str) -> None:
        with self._pending_lock:
            pending_values = list(self._pending.values())
        for pending in pending_values:
            pending.error = error
            pending.event.set()
        with self._turn_lock:
            trackers = list(self._turns.values())
        for tracker in trackers:
            if not tracker.completed.is_set():
                tracker.error = error
                tracker.status = "failed"
                tracker.completed.set()

    def _activate_dynamic_tool_context(
        self,
        *,
        thread_id: str,
        browser_context: BrowserToolContext,
        history_enabled: bool = False,
        history_request_id: str = "",
        history_max_calls: int = 0,
        history_max_bytes: int = 0,
    ) -> str:
        if not self.dynamic_tool_specs:
            return ""
        token = uuid.uuid4().hex
        with self._dynamic_tool_lock:
            if thread_id in self._dynamic_tool_contexts:
                raise CodexAppServerError("Codex thread already has an active dynamic-tool context")
            self._dynamic_tool_contexts[thread_id] = _ActiveDynamicToolContext(
                token=token,
                browser=browser_context,
            )
            if history_enabled:
                from app.history.tools import history_session_for
                from app.history.files import prepare_files

                def files_provider(store, chat):
                    if not _artifact_request_dir_is_safe(browser_context.request_dir):
                        raise CodexAppServerError("History request directory failed safety validation")
                    result = prepare_files(store, chat, browser_context.request_dir)
                    self._dynamic_tool_contexts[thread_id].history_exports.append(Path(result['directory']))
                    result['directory'] = _as_runtime_path(Path(result['directory']), self.use_wsl)
                    result['instructions'] = ('Use native shell: read README.md in this directory, then run python3 archive.py overview. '
                                              'archive.sqlite3 contains all archived messages for this chat; use Python/SQL for complete analysis. '
                                              'Print only selected evidence or aggregates. members.json contains current names and aliases. '
                                              'These inputs are removed after this reply; save deliverable reports in the provided artifact output directory.')
                    return result

                self._dynamic_tool_contexts[thread_id].history = history_session_for(
                    browser_context.chat_id, history_request_id, browser_context.request_id,
                    max_calls=history_max_calls, max_bytes=history_max_bytes, files_provider=files_provider)
        return token

    def _bind_dynamic_tool_turn(self, thread_id: str, turn_id: str) -> None:
        with self._dynamic_tool_condition:
            active = self._dynamic_tool_contexts.get(thread_id)
            if active is not None and turn_id:
                active.turn_ids.add(turn_id)
                self._dynamic_tool_condition.notify_all()

    def _deactivate_dynamic_tool_context(self, thread_id: str, token: str) -> None:
        if not token:
            return
        active: Optional[_ActiveDynamicToolContext] = None
        with self._dynamic_tool_condition:
            candidate = self._dynamic_tool_contexts.get(thread_id)
            if candidate is not None and candidate.token == token:
                active = self._dynamic_tool_contexts.pop(thread_id)
                self._dynamic_tool_condition.notify_all()
        if active is None:
            return
        for folder in active.history_exports:
            try:
                if (_artifact_request_dir_is_safe(active.browser.request_dir)
                        and not _is_link_like(folder)
                        and folder.parent.resolve(strict=True) == active.browser.request_dir.resolve(strict=True)):
                    shutil.rmtree(folder)
            except OSError:
                logger.warning("Could not clean history working copy: %s", folder)
        scratch_root = active.browser.scratch_root
        request_dir = active.browser.request_dir
        try:
            if (
                _artifact_request_dir_is_safe(request_dir)
                and not _is_link_like(scratch_root)
                and scratch_root.parent.resolve(strict=True) == request_dir.resolve(strict=True)
                and scratch_root.is_dir()
            ):
                shutil.rmtree(scratch_root)
        except OSError:
            logger.warning("Could not clean browser scratch directory: %s", scratch_root)

    def _chat_lock(self, chat_id: str) -> threading.Lock:
        with self._chat_locks_lock:
            return self._chat_locks.setdefault(chat_id, threading.Lock())

    def _thread_config(
        self,
        reasoning_effort: str,
        web_search_mode: str,
        reasoning_summary: str,
    ) -> Dict[str, Any]:
        config: Dict[str, Any] = {
            "model_reasoning_effort": reasoning_effort,
            "web_search": web_search_mode,
            "approvals_reviewer": CODEX_APPROVALS_REVIEWER,
            "features.memories": False,
            "features.external_agent_memory_import": False,
        }
        if reasoning_summary != "inherit":
            config["model_reasoning_summary"] = reasoning_summary
            config["model_supports_reasoning_summaries"] = reasoning_summary != "none"
        return config

    @staticmethod
    def _thread_config_signature(config: Dict[str, Any]) -> str:
        return json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _runtime_config_signature(
        self,
        config: Dict[str, Any],
        *,
        workdir: Path,
        permission_profile: str,
        approval_policy: str,
        developer_instructions: str = "",
    ) -> str:
        return "|".join(
            (
                self._thread_config_signature(config),
                str(workdir.resolve()),
                permission_profile,
                approval_policy,
                self.dynamic_tool_signature,
                hashlib.sha256(developer_instructions.encode("utf-8")).hexdigest(),
            )
        )

    def _start_thread(
        self,
        *,
        model: str,
        reasoning_effort: str,
        web_search_mode: str,
        reasoning_summary: str,
        timeout: int,
        ephemeral: bool = False,
        sandbox: str = "workspace-write",
        workdir: Optional[Path] = None,
        permission_profile: str = "",
        approval_policy: str = CODEX_APPROVAL_POLICY,
        runtime_workspace_roots: Optional[List[Path]] = None,
        developer_instructions: str = "",
    ) -> str:
        runtime_workdir = _as_runtime_path(Path(workdir or self.workdir), self.use_wsl)
        thread_config = self._thread_config(
            reasoning_effort,
            web_search_mode,
            reasoning_summary,
        )
        execution_policy: Dict[str, Any]
        if permission_profile:
            execution_policy = {"permissions": permission_profile}
            if runtime_workspace_roots:
                execution_policy["runtimeWorkspaceRoots"] = [
                    _as_runtime_path(Path(path), self.use_wsl)
                    for path in runtime_workspace_roots
                ]
        else:
            execution_policy = {"sandbox": sandbox}
        result = self._request(
            "thread/start",
            {
                "model": model,
                "cwd": runtime_workdir,
                "approvalPolicy": approval_policy,
                "ephemeral": bool(ephemeral),
                "config": thread_config,
                "developerInstructions": developer_instructions,
                **({"dynamicTools": self.dynamic_tool_specs} if self.dynamic_tool_specs else {}),
                **execution_policy,
            },
            timeout=min(timeout, 120),
        )
        thread = result.get("thread") if isinstance(result, dict) else {}
        thread_id = str(thread.get("id") or "") if isinstance(thread, dict) else ""
        if not thread_id:
            raise CodexAppServerError("Codex App Server thread/start returned no thread id")
        self._loaded_threads[thread_id] = self._runtime_config_signature(
            thread_config,
            workdir=Path(workdir or self.workdir),
            permission_profile=permission_profile,
            approval_policy=approval_policy,
            developer_instructions=developer_instructions,
        )
        return thread_id

    def _resume_thread(
        self,
        thread_id: str,
        *,
        model: str,
        reasoning_effort: str,
        web_search_mode: str,
        reasoning_summary: str,
        timeout: int,
        sandbox: str = "workspace-write",
        workdir: Optional[Path] = None,
        permission_profile: str = "",
        approval_policy: str = CODEX_APPROVAL_POLICY,
        runtime_workspace_roots: Optional[List[Path]] = None,
        developer_instructions: str = "",
    ) -> None:
        thread_config = self._thread_config(
            reasoning_effort,
            web_search_mode,
            reasoning_summary,
        )
        config_signature = self._runtime_config_signature(
            thread_config,
            workdir=Path(workdir or self.workdir),
            permission_profile=permission_profile,
            approval_policy=approval_policy,
            developer_instructions=developer_instructions,
        )
        if self._loaded_threads.get(thread_id) == config_signature:
            return
        runtime_workdir = _as_runtime_path(Path(workdir or self.workdir), self.use_wsl)
        execution_policy: Dict[str, Any]
        if permission_profile:
            execution_policy = {"permissions": permission_profile}
            if runtime_workspace_roots:
                execution_policy["runtimeWorkspaceRoots"] = [
                    _as_runtime_path(Path(path), self.use_wsl)
                    for path in runtime_workspace_roots
                ]
        else:
            execution_policy = {"sandbox": sandbox}
        try:
            result = self._request(
                "thread/resume",
                {
                    "threadId": thread_id,
                    "model": model,
                    "cwd": runtime_workdir,
                    "approvalPolicy": approval_policy,
                    "config": thread_config,
                    "developerInstructions": developer_instructions,
                    **execution_policy,
                },
                timeout=min(timeout, 120),
            )
        except CodexAppServerError as exc:
            raise _ResumeThreadError(str(exc)) from exc
        thread = result.get("thread") if isinstance(result, dict) else {}
        resumed_id = str(thread.get("id") or "") if isinstance(thread, dict) else ""
        if resumed_id != thread_id:
            raise _ResumeThreadError("Codex App Server resumed an unexpected thread")
        self._loaded_threads[thread_id] = config_signature

    def _start_tracked_turn(
        self,
        *,
        request_id: str,
        thread_id: str,
        turn_input: List[Dict[str, Any]],
        turn_options: Dict[str, Any],
        timeout: int,
        recovery_attempt: bool = False,
    ) -> Tuple[str, _TurnTracker]:
        """Start one App Server turn and wait for its terminal notification."""
        result = self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": turn_input,
                **turn_options,
            },
            timeout=min(timeout, 120),
        )
        turn = result.get("turn") if isinstance(result, dict) else {}
        turn_id = str(turn.get("id") or "") if isinstance(turn, dict) else ""
        if not turn_id:
            raise CodexAppServerError("Codex App Server turn/start returned no turn id")
        self._bind_dynamic_tool_turn(thread_id, turn_id)

        tracker = self._tracker(turn_id)
        tracker.request_id = request_id
        tracker.thread_id = thread_id
        codex_job_manager.update(
            request_id,
            status="running",
            turn_id=turn_id,
            recovery_attempt=bool(recovery_attempt),
            _cancel_callback=lambda: self._interrupt_turn(thread_id, turn_id),
        )
        if not tracker.started_recorded:
            codex_job_manager.record_event(
                request_id,
                "turn_started",
                {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "recovery_attempt": bool(recovery_attempt),
                },
                status="running",
            )
            tracker.started_recorded = True

        try:
            if not tracker.completed.wait(timeout=timeout):
                self._interrupt_turn(thread_id, turn_id)
                raise CodexAppServerError(
                    f"Codex App Server turn timed out after {timeout}s"
                )
            if not tracker.usage:
                tracker.usage_ready.wait(timeout=2)
            if tracker.error or tracker.status not in {"completed", "complete"}:
                raise CodexAppServerError(
                    tracker.error
                    or f"Codex App Server turn ended with status {tracker.status}"
                )
            return turn_id, tracker
        except Exception:
            with self._turn_lock:
                self._turns.pop(turn_id, None)
            raise

    def _interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        try:
            self._request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
                timeout=10,
            )
        except Exception:
            logger.warning("Failed to interrupt Codex turn %s", turn_id, exc_info=True)

    def chat(
        self,
        request: Dict[str, Any],
        *,
        chat_id: str,
        role_name: Optional[str] = None,
        retry: bool = False,
        max_turns: int = 0,
    ) -> Dict[str, Any]:
        if not self.enabled:
            raise CodexAppServerError("Codex App Server is disabled")
        if not chat_id:
            raise CodexAppServerError("chat_id is required for persistent Codex threads")
        with self._chat_lock(chat_id):
            return self._chat_locked(
                request,
                chat_id=chat_id,
                role_name=role_name,
                retry=retry,
                max_turns=max(0, int(max_turns or 0)),
                ephemeral=bool(request.get("mabobot_fresh_context")),
            )

    def run(self, request: Dict[str, Any], *, profile_name: str = "batch") -> Dict[str, Any]:
        """Run one isolated turn on this long-lived process."""
        run_key = f"{profile_name}:{uuid.uuid4().hex}"
        extra_body = request.get("extra_body") or {}
        if _as_bool(request.get("codex_isolated_workdir", extra_body.get("codex_isolated_workdir")), False):
            # Create the cwd before thread/start, and clean it on startup failures
            # as well as completed/interrupted turns. Never mutate the caller.
            with tempfile.TemporaryDirectory(prefix="mabobot_codex_llm_") as directory:
                isolated = dict(request)
                isolated["codex_workdir"] = str(Path(directory).resolve())
                isolated["codex_isolated_workdir"] = False
                return self.run(isolated, profile_name=profile_name)
        with self._chat_lock(run_key):
            return self._chat_locked(
                request,
                chat_id=run_key,
                role_name=profile_name,
                retry=False,
                max_turns=1,
                ephemeral=True,
            )

    def read_rate_limits(self, timeout: int = 30) -> Dict[str, Any]:
        """Read account limits through the initialized shared connection."""
        result = self._request("account/rateLimits/read", {}, timeout=max(1, int(timeout)))
        return result if isinstance(result, dict) else {}

    def read_account(self, *, refresh_token: bool = False, timeout: int = 30) -> Dict[str, Any]:
        result = self._request(
            "account/read",
            {"refreshToken": bool(refresh_token)},
            timeout=max(1, int(timeout)),
        )
        return result if isinstance(result, dict) else {}

    def start_chatgpt_device_login(self, timeout: int = 30) -> Dict[str, Any]:
        result = self._request(
            "account/login/start",
            {"type": "chatgptDeviceCode"},
            timeout=max(1, int(timeout)),
        )
        return result if isinstance(result, dict) else {}

    def cancel_account_login(self, login_id: str, timeout: int = 30) -> Dict[str, Any]:
        result = self._request(
            "account/login/cancel",
            {"loginId": str(login_id or "")},
            timeout=max(1, int(timeout)),
        )
        return result if isinstance(result, dict) else {}

    def logout_account(self, timeout: int = 30) -> Dict[str, Any]:
        result = self._request("account/logout", {}, timeout=max(1, int(timeout)))
        return result if isinstance(result, dict) else {}

    def list_models(self, *, include_hidden: bool = False, timeout: int = 30) -> Dict[str, Any]:
        result = self._request(
            "model/list",
            {"limit": 200, "includeHidden": bool(include_hidden)},
            timeout=max(1, int(timeout)),
        )
        return result if isinstance(result, dict) else {}

    def _chat_locked(
        self,
        request: Dict[str, Any],
        *,
        chat_id: str,
        role_name: Optional[str],
        retry: bool,
        max_turns: int,
        ephemeral: bool,
    ) -> Dict[str, Any]:
        self.start()
        model = str(request.get("model") or os.getenv("CODEX_PROXY_MODEL") or "gpt-5.6-sol")
        extra_body = request.get("extra_body") if isinstance(request.get("extra_body"), dict) else {}
        reasoning_effort = str(
            request.get("reasoning_effort")
            or extra_body.get("reasoning_effort")
            or os.getenv("CODEX_PROXY_REASONING_EFFORT")
            or "high"
        )
        web_search_mode = normalize_codex_web_search_mode(
            request.get(
                "codex_web_search",
                request.get(
                    "web_search",
                    extra_body.get("codex_web_search", extra_body.get("web_search")),
                ),
            ),
            default=normalize_codex_web_search_mode(
                os.getenv("CODEX_PROXY_WEB_SEARCH"),
                "disabled",
            ),
        )
        web_search_enabled = web_search_mode != "disabled"
        reasoning_summary = str(
            request.get("codex_reasoning_summary")
            or extra_body.get("codex_reasoning_summary")
            or "inherit"
        ).strip().lower()
        if reasoning_summary not in {"inherit", "none", "auto", "concise", "detailed"}:
            reasoning_summary = "inherit"
        allow_image_input = _as_bool(
            request.get("mabobot_allow_image_input", extra_body.get("mabobot_allow_image_input")),
            False,
        )
        timeout = _positive_int(request.get("timeout"), 600)
        messages = request.get("messages") or []
        if not isinstance(messages, list):
            raise CodexAppServerError("messages must be a list")

        sandbox = str(
            request.get("codex_sandbox")
            or extra_body.get("codex_sandbox")
            or "workspace-write"
        ).strip().lower()
        if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            sandbox = "workspace-write"
        permission_profile = str(
            request.get("codex_permission_profile")
            or extra_body.get("codex_permission_profile")
            or ""
        ).strip()
        approval_policy = str(
            request.get("codex_approval_policy")
            or extra_body.get("codex_approval_policy")
            or CODEX_APPROVAL_POLICY
        ).strip().lower()
        if approval_policy not in {"never", "on-request"}:
            approval_policy = CODEX_APPROVAL_POLICY
        workdir = Path(
            request.get("codex_workdir")
            or extra_body.get("codex_workdir")
            or self.workdir
        ).resolve()
        roots_value = request.get(
            "codex_runtime_workspace_roots",
            extra_body.get("codex_runtime_workspace_roots"),
        )
        runtime_workspace_roots = [
            Path(item).resolve()
            for item in (roots_value if isinstance(roots_value, list) else [])
            if str(item or "").strip()
        ]
        output_schema = request.get("output_schema")
        if not isinstance(output_schema, dict):
            output_schema = extra_body.get("output_schema")
        if not isinstance(output_schema, dict):
            output_schema = None
        text_only = _as_bool(
            request.get("codex_text_only", extra_body.get("codex_text_only")), False
        )

        runtime_profile = str(request.get("codex_runtime_profile") or "").strip()
        state = self.state_store.get(chat_id)
        developer_instructions = "\n\n".join(
            text for message in messages
            if message.get("role") in {"system", "developer"}
            if (text := _content_to_text(message.get("content")))
        )
        migrating_instructions = bool(
            state and state.get("instruction_transport_version") != 1
        )
        incremental_context = bool(request.get("mabobot_incremental_context")) and not ephemeral
        access_signature = str(request.get("codex_access_signature") or "").strip()
        context_window_hint = _nonnegative_int(
            request.get(
                "codex_model_context_window",
                extra_body.get("codex_model_context_window"),
            ),
            0,
        )
        effective_rotate_tokens = self.rotate_tokens
        state_context_window = int((state or {}).get("model_context_window") or 0)
        state_matches_profile = str((state or {}).get("runtime_profile") or "") == runtime_profile
        effective_context_window = (
            state_context_window
            if state_matches_profile
            and state_context_window > 0
            and (state or {}).get("model_context_window_source") == "provider_usage"
            else context_window_hint
            or (state_context_window if state_matches_profile else 0)
        )
        if effective_context_window > 0:
            # Keep a final safety band even when an old environment override
            # was tuned for a previous 1M-context model.
            runtime_soft_limit = max(
                4096,
                effective_context_window - self.context_safety_tokens,
            )
            effective_rotate_tokens = (
                min(effective_rotate_tokens, runtime_soft_limit)
                if effective_rotate_tokens > 0
                else runtime_soft_limit
            )
        delta = _plan_message_delta(
            messages,
            None if ephemeral else state,
            model=model,
            reasoning_effort=reasoning_effort,
            retry=retry,
            rotate_tokens=0 if incremental_context else effective_rotate_tokens,
            max_turns=max_turns,
            runtime_profile=runtime_profile,
            access_signature=access_signature,
            dynamic_tool_signature=self.dynamic_tool_signature,
            max_compactions=0 if incremental_context else self.max_compactions,
            idle_rotate_seconds=self.idle_rotate_seconds,
            incremental_context=incremental_context,
        )
        thread_id = str(state.get("thread_id") or "") if delta.resume and state else ""
        if thread_id:
            try:
                self._resume_thread(
                    thread_id,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    web_search_mode=web_search_mode,
                    reasoning_summary=reasoning_summary,
                    timeout=timeout,
                    sandbox=sandbox,
                    workdir=workdir,
                    permission_profile=permission_profile,
                    approval_policy=approval_policy,
                    runtime_workspace_roots=runtime_workspace_roots,
                    developer_instructions=developer_instructions,
                )
            except _ResumeThreadError as exc:
                if migrating_instructions:
                    raise CodexAppServerError(
                        "Unable to migrate existing Codex thread instructions; history was preserved. "
                        "Check the remote Codex version and retry: " + str(exc)
                    ) from exc
                logger.warning(
                    "Codex thread %s for chat %s cannot be resumed (%s); rotating",
                    thread_id,
                    chat_id,
                    exc,
                )
                delta = _MessageDelta(
                    list(messages),
                    delta.stable_fingerprints,
                    delta.ephemeral_fingerprints,
                    False,
                    "resume_failed",
                )
                thread_id = ""

        archive_cursor = None
        if incremental_context:
            from app.history.tools import get_archive
            from app.history.context import render_recent
            from app.assistant.context_manager import ChatContextManager

            archive_cursor, archive_rows, omitted = get_archive().context_snapshot(
                chat_id, after=(state or {}).get("archive_cursor") if delta.resume else None
            )
            history_text = render_recent(archive_rows, ChatContextManager(), limit=None)
            if omitted:
                history_text = "新增/修订记录超过注入上限，以下仅为最近部分，需要时查完整档案。\n" + history_text
            # Keep the current request, search context and attachments untouched.
            # Replace only the rolling history block; the archive cursor never
            # enters the model prompt or advances before successful completion.
            delta.messages = [m for m in delta.messages if m.get("name") != "history_context"]
            if archive_rows:
                history = {"role": "user", "name": "history_context", "content": history_text}
                position = next((i for i, m in enumerate(delta.messages) if m.get("role") not in {"system", "developer"}), len(delta.messages))
                delta.messages.insert(position, history)
            elif not delta.resume:
                fallback_history = [m for m in messages if m.get("name") == "history_context"]
                delta.messages = fallback_history + delta.messages

        previous_generation = int((state or {}).get("thread_generation") or 0)
        if previous_generation <= 0 and state and state.get("thread_id"):
            previous_generation = 1
        thread_generation = (
            max(1, previous_generation)
            if delta.resume
            else max(
                1,
                previous_generation
                + (
                    1
                    if state
                    and (state.get("thread_id") or state.get("pending_rotation_reason"))
                    else 0
                ),
            )
        )

        if not thread_id:
            thread_id = self._start_thread(
                model=model,
                reasoning_effort=reasoning_effort,
                web_search_mode=web_search_mode,
                reasoning_summary=reasoning_summary,
                timeout=timeout,
                ephemeral=ephemeral,
                sandbox=sandbox,
                workdir=workdir,
                permission_profile=permission_profile,
                approval_policy=approval_policy,
                runtime_workspace_roots=runtime_workspace_roots,
                developer_instructions=developer_instructions,
            )
            logger.info(
                "Codex persistent thread started: chat=%s thread=%s reason=%s",
                chat_id,
                thread_id,
                delta.rotation_reason,
            )
        elif not delta.messages:
            raise CodexAppServerError("No new chatbot messages to send to the persistent Codex thread")

        request_id = uuid.uuid4().hex
        artifact_root = Path(
            request.get("codex_artifact_root")
            or extra_body.get("codex_artifact_root")
            or os.getenv("CODEX_PROXY_ARTIFACT_ROOT")
            or "tmp/images/codex"
        )
        if not artifact_root.is_absolute():
            artifact_root = Path(self.workdir) / artifact_root
        request_dir, output_dir = _prepare_artifact_output_dir(
            artifact_root,
            request_id,
            fail_closed=permission_profile == "mabobot-chat-isolated",
        )
        _cleanup_expired_artifacts(artifact_root)
        staged_input_files = _stage_input_files(
            request.get("mabobot_input_files", extra_body.get("mabobot_input_files")),
            request_dir=request_dir,
            use_wsl=self.use_wsl,
        )
        runtime_output_dir = _as_runtime_path(output_dir, self.use_wsl)
        browser_context: Optional[BrowserToolContext] = None
        if self.dynamic_tool_specs:
            if not _artifact_request_dir_is_safe(request_dir):
                raise CodexAppServerError("Codex browser request directory failed safety validation")
            browser_scratch_root = request_dir / ".browser"
            browser_scratch_root.mkdir()
            if _is_link_like(browser_scratch_root) or not browser_scratch_root.is_dir():
                raise CodexAppServerError("Codex browser scratch directory is unsafe")
            browser_context = BrowserToolContext(
                request_id=request_id,
                chat_id=chat_id,
                access_mode=str(request.get("codex_access_mode") or "isolated"),
                workdir=workdir,
                request_dir=request_dir,
                output_dir=output_dir,
                scratch_root=browser_scratch_root,
                runtime_workdir=_as_runtime_path(workdir, self.use_wsl),
                runtime_request_dir=_as_runtime_path(request_dir, self.use_wsl),
                runtime_output_dir=runtime_output_dir,
                use_wsl=self.use_wsl,
            )

        image_urls = extract_image_urls(delta.messages, allow_image_input=allow_image_input)
        image_request_mode = None if text_only else _direct_image_request_mode(
            delta.messages,
            has_image_input=bool(image_urls),
        )
        temporary_image_paths: List[Path] = []
        runtime_image_paths: List[str] = []
        image_staging_dir = (
            request_dir / "inputs"
            if permission_profile == "mabobot-chat-isolated"
            else None
        )
        for image_url in image_urls:
            image_path = _write_image_url_to_file(
                image_url,
                destination_dir=image_staging_dir,
            )
            if image_staging_dir is None and image_url.startswith("data:"):
                temporary_image_paths.append(image_path)
            runtime_image_paths.append(_as_runtime_path(image_path, self.use_wsl))

        prompt = render_chat_prompt(
            # Native instructions are configured on the thread, never quoted as user input.
            [m for m in delta.messages if m.get("role") not in {"system", "developer"}],
            native_instructions=True,
            artifact_output_dir=runtime_output_dir,
            native_web_search_enabled=web_search_enabled,
            input_image_count=len(runtime_image_paths),
            input_files=staged_input_files,
            available_file_commands=_detect_runtime_file_commands(self.use_wsl),
            text_only=text_only,
        )
        if delta.resume:
            prompt = (
                "Continue the existing conversation. The conversation block below contains only "
                "new framework messages; all earlier messages remain in this Codex thread.\n\n"
                + prompt
            )
        turn_input: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        turn_input.extend(
            {"type": "localImage", "path": path, "detail": "auto"}
            for path in runtime_image_paths
        )

        started_at = time.time()
        codex_job_manager.register(
            request_id,
            {
                "request_id": request_id,
                "status": "starting",
                "backend": "codex_app_server",
                "pool_worker": self.instance_name,
                "profile": role_name,
                "config_profile": runtime_profile or None,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "web_search": web_search_enabled,
                "web_search_mode": web_search_mode,
                "reasoning_summary": reasoning_summary,
                "max_turns": max_turns,
                "timeout": timeout,
                "chat_id": chat_id,
                "chat_name": str(request.get("codex_source_chat_name") or chat_id),
                "chat_type": str(request.get("codex_source_chat_type") or ""),
                "thread_id": thread_id,
                "thread_generation": thread_generation,
                "resumed": bool(delta.resume),
                "rotation_reason": None if delta.resume else delta.rotation_reason,
                "continuity_status": str(
                    (state or {}).get("continuity_status") or "synchronized"
                ),
                "workdir": str(workdir),
                "sandbox": None if permission_profile else sandbox,
                "permission_profile": permission_profile or None,
                "access_mode": request.get("codex_access_mode"),
                "approval_policy": approval_policy,
                "approvals_reviewer": CODEX_APPROVALS_REVIEWER,
                "message_count": len(delta.messages),
                "prompt_chars": len(prompt),
                "image_count": len(runtime_image_paths),
                "input_file_count": len(staged_input_files),
                "dynamic_tools": bool(self.dynamic_tool_specs),
                "browser_tool": bool(self.dynamic_tool_specs),
                "request_dir": str(request_dir),
                "output_dir": str(output_dir),
                "started_at": started_at,
            },
        )
        codex_job_manager.record_event(
            request_id,
            "queued",
            {"thread_id": thread_id, "profile": role_name},
        )
        turn_id = ""
        tracker: Optional[_TurnTracker] = None
        turn_ids: List[str] = []
        turn_trackers: List[_TurnTracker] = []
        recovery_prompt = ""
        dynamic_context_token = ""
        turn_options: Dict[str, Any] = {
            "model": model,
            "effort": reasoning_effort,
            "cwd": _as_runtime_path(workdir, self.use_wsl),
            "approvalPolicy": approval_policy,
            **(
                {"permissions": permission_profile}
                if permission_profile
                else {}
            ),
            **(
                {
                    "runtimeWorkspaceRoots": [
                        _as_runtime_path(path, self.use_wsl)
                        for path in runtime_workspace_roots
                    ]
                }
                if runtime_workspace_roots
                else {}
            ),
            **({"outputSchema": output_schema} if output_schema else {}),
            **(
                {"summary": reasoning_summary}
                if reasoning_summary != "inherit"
                else {}
            ),
        }
        try:
            if browser_context is not None:
                dynamic_context_token = self._activate_dynamic_tool_context(
                    thread_id=thread_id,
                    browser_context=browser_context,
                    history_enabled=bool(request.get("mabobot_history_enabled")),
                    history_request_id=str(request.get("mabobot_history_request_id") or ""),
                    history_max_calls=int(request.get("mabobot_history_max_calls") or 0),
                    history_max_bytes=int(request.get("mabobot_history_max_bytes") or 0),
                )
            turn_id, tracker = self._start_tracked_turn(
                request_id=request_id,
                thread_id=thread_id,
                turn_input=turn_input,
                turn_options=turn_options,
                timeout=timeout,
            )
            turn_ids.append(turn_id)
            turn_trackers.append(tracker)

            response_messages = tracker.final_messages or tracker.unclassified_messages
            text = response_messages[-1].strip() if response_messages else ""
            attachments = [] if text_only else self._collect_response_attachments(
                output_dir=output_dir,
                turn_trackers=turn_trackers,
                image_request_mode=image_request_mode,
                request_id=request_id,
            )
            _write_artifact_manifest(
                request_dir,
                request_id=request_id,
                backend="codex_app_server",
                model=model,
                attachments=attachments,
            )
            if not text and attachments:
                text = _default_text_for_attachments(attachments)
            if not text:
                logger.warning(
                    "Codex App Server returned no final answer; retrying once in the same "
                    "thread/profile without tools: chat=%s thread=%s profile=%s model=%s",
                    chat_id,
                    thread_id,
                    runtime_profile or role_name,
                    model,
                )
                recovery_prompt = _EMPTY_RESPONSE_RECOVERY_PROMPT
                recovery_search_disabled = False
                search_disable_error = ""
                if not ephemeral and web_search_mode != "disabled":
                    try:
                        self._resume_thread(
                            thread_id,
                            model=model,
                            reasoning_effort=reasoning_effort,
                            web_search_mode="disabled",
                            reasoning_summary=reasoning_summary,
                            timeout=timeout,
                            sandbox=sandbox,
                            workdir=workdir,
                            permission_profile=permission_profile,
                            approval_policy=approval_policy,
                            runtime_workspace_roots=runtime_workspace_roots,
                            developer_instructions=developer_instructions,
                        )
                        recovery_search_disabled = True
                    except Exception as exc:
                        search_disable_error = str(exc)
                        logger.warning(
                            "Could not disable native web search for empty-response recovery; "
                            "continuing with the no-tool recovery instruction: %s",
                            exc,
                        )
                codex_job_manager.record_event(
                    request_id,
                    "empty_response_retry",
                    {
                        "thread_id": thread_id,
                        "previous_turn_id": turn_id,
                        "config_profile": runtime_profile or None,
                        "model": model,
                        "same_thread": True,
                        "same_profile": True,
                        "web_search_disabled": recovery_search_disabled
                        or web_search_mode == "disabled",
                        "search_disable_error": search_disable_error or None,
                    },
                    status="running",
                    empty_response_retry_count=1,
                )
                try:
                    turn_id, tracker = self._start_tracked_turn(
                        request_id=request_id,
                        thread_id=thread_id,
                        turn_input=[{"type": "text", "text": recovery_prompt}],
                        turn_options=turn_options,
                        timeout=timeout,
                        recovery_attempt=True,
                    )
                    turn_ids.append(turn_id)
                    turn_trackers.append(tracker)
                finally:
                    if recovery_search_disabled:
                        try:
                            self._resume_thread(
                                thread_id,
                                model=model,
                                reasoning_effort=reasoning_effort,
                                web_search_mode=web_search_mode,
                                reasoning_summary=reasoning_summary,
                                timeout=timeout,
                                sandbox=sandbox,
                                workdir=workdir,
                                permission_profile=permission_profile,
                                approval_policy=approval_policy,
                                runtime_workspace_roots=runtime_workspace_roots,
                                developer_instructions=developer_instructions,
                            )
                        except Exception as exc:
                            self._loaded_threads.pop(thread_id, None)
                            logger.warning(
                                "Could not restore Codex thread search configuration after "
                                "empty-response recovery; the next turn will reload it: %s",
                                exc,
                            )
                            codex_job_manager.record_event(
                                request_id,
                                "empty_response_retry_restore_failed",
                                {"thread_id": thread_id, "message": str(exc)[:1000]},
                            )

                response_messages = tracker.final_messages or tracker.unclassified_messages
                text = response_messages[-1].strip() if response_messages else ""
                attachments = [] if text_only else self._collect_response_attachments(
                    output_dir=output_dir,
                    turn_trackers=turn_trackers,
                    image_request_mode=image_request_mode,
                    request_id=request_id,
                )
                _write_artifact_manifest(
                    request_dir,
                    request_id=request_id,
                    backend="codex_app_server",
                    model=model,
                    attachments=attachments,
                )
                if not text and attachments:
                    text = _default_text_for_attachments(attachments)
                if not text:
                    raise CodexAppServerError(
                        "Codex App Server returned an empty response after same-profile retry"
                    )

            usage_segments = []
            for attempt_tracker in turn_trackers:
                usage_segments.extend(attempt_tracker.usage_segments or [
                    _normalize_app_server_usage(attempt_tracker.usage,
                                                source=attempt_tracker.usage_source or "codex_app_server.turn")])
            # Keep the latest model request independent of total billable usage.
            final_context_usage = _normalize_app_server_usage(
                turn_trackers[-1].usage, source="codex_app_server.latest_request"
            ) if turn_trackers else {}
            usage = _merge_attempt_usage(usage_segments)
            if not usage:
                usage = estimate_codex_usage(
                    "\n\n".join(part for part in (prompt, recovery_prompt) if part),
                    text,
                )
                usage["estimated"] = bool(usage)
                usage["source"] = (
                    "codex_app_server_local_estimate_missing_notification"
                    if usage
                    else "codex_app_server_usage_unavailable"
                )
                usage["cache_data_available"] = False
            reported_context_window = int(usage.get("model_context_window") or 0)
            if effective_context_window > 0 and not usage.get("model_context_window"):
                usage["model_context_window"] = effective_context_window
            usage_available = any(
                int(usage.get(key) or 0) > 0
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            )
            usage_accuracy = (
                "estimated"
                if usage.get("estimated") and usage_available
                else "reported"
                if turn_trackers
                and all(attempt_tracker.usage for attempt_tracker in turn_trackers)
                and usage_available
                else "unknown"
            )
            previous_session_total = (
                state.get("session_total")
                if state and delta.resume and isinstance(state.get("session_total"), dict)
                else None
            )
            reported_session_total = usage.get("session_total")
            session_total = (
                dict(reported_session_total)
                if isinstance(reported_session_total, dict)
                else _accumulate_usage_total(previous_session_total, usage)
            )
            previous_lifetime_total = (
                state.get("lifetime_total")
                if state and isinstance(state.get("lifetime_total"), dict)
                else state.get("session_total")
                if state and isinstance(state.get("session_total"), dict)
                else None
            )
            lifetime_total = _accumulate_usage_total(previous_lifetime_total, usage)
            previous_usage_accuracy = str((state or {}).get("usage_accuracy") or "").strip()
            if not previous_usage_accuracy and state:
                previous_usage_accuracy = (
                    "estimated"
                    if state.get("usage_estimated")
                    else "reported"
                    if int(state.get("last_total_tokens") or 0) > 0
                    else "unknown"
                )
            session_usage_accuracy = (
                _merge_usage_accuracy(
                    (state or {}).get("session_usage_accuracy") or previous_usage_accuracy,
                    usage_accuracy,
                )
                if delta.resume
                else usage_accuracy
            )
            lifetime_usage_accuracy = _merge_usage_accuracy(
                (state or {}).get("lifetime_usage_accuracy") or previous_usage_accuracy,
                usage_accuracy,
            )
            prompt_details = usage.get("prompt_tokens_details")
            if not isinstance(prompt_details, dict):
                prompt_details = {}
            completion_details = usage.get("completion_tokens_details")
            if not isinstance(completion_details, dict):
                completion_details = {}
            reasoning_text = "\n\n".join(
                part
                for attempt_tracker in turn_trackers
                for part in attempt_tracker.reasoning
            ).strip()
            request_compacted = any(
                attempt_tracker.compacted for attempt_tracker in turn_trackers
            )

            assistant_fingerprint = _message_fingerprint(
                {"role": "assistant", "content": text}
            )
            previous_turn_count = int(state.get("turn_count") or 0) if state and delta.resume else 0
            previous_compaction_count = (
                int(state.get("compaction_count") or 0)
                if state and delta.resume
                else 0
            )
            now_iso = datetime.now().isoformat()
            state_payload = {
                "thread_id": thread_id,
                "thread_generation": thread_generation,
                "runtime_profile": runtime_profile or None,
                "model": model,
                "last_model": model,
                "reasoning_effort": reasoning_effort,
                "reasoning_summary": reasoning_summary,
                "web_search_mode": web_search_mode,
                "role_name": role_name,
                "stable_fingerprints": delta.stable_fingerprints + [assistant_fingerprint],
                "last_input_stable_fingerprints": delta.stable_fingerprints,
                "last_ephemeral_fingerprints": delta.ephemeral_fingerprints,
                "turn_count": previous_turn_count + 1,
                "lifetime_turn_count": int(
                    (state or {}).get("lifetime_turn_count")
                    or (state or {}).get("turn_count")
                    or 0
                )
                + 1,
                "compaction_count": previous_compaction_count + int(request_compacted),
                "lifetime_compaction_count": int(
                    (state or {}).get("lifetime_compaction_count")
                    or (state or {}).get("compaction_count")
                    or 0
                )
                + int(request_compacted),
                "last_compacted_at": (
                    now_iso
                    if request_compacted
                    else (state or {}).get("last_compacted_at")
                    if delta.resume
                    else None
                ),
                "last_input_tokens": int(usage.get("prompt_tokens") or 0),
                "thread_input_tokens": int(final_context_usage.get("prompt_tokens") or 0),
                "context_total_tokens": final_context_usage.get("total_tokens"),
                "configured_context_window": context_window_hint or None,
                "auto_compact_token_limit": context_window_hint * 9 // 10 if context_window_hint else None,
                "last_cached_input_tokens": int(prompt_details.get("cached_tokens") or 0),
                "last_cache_miss_input_tokens": int(usage.get("cache_miss_input_tokens") or 0),
                "last_completion_tokens": int(usage.get("completion_tokens") or 0),
                "last_reasoning_tokens": int(completion_details.get("reasoning_tokens") or 0),
                "last_total_tokens": int(usage.get("total_tokens") or 0),
                "model_context_window": (
                    usage.get("model_context_window")
                    or effective_context_window
                    or None
                ),
                "model_context_window_source": (
                    "provider_usage"
                    if reported_context_window > 0
                    or (
                        state_matches_profile
                        and state_context_window > 0
                        and (state or {}).get("model_context_window_source") == "provider_usage"
                    )
                    else "profile_metadata"
                    if context_window_hint > 0
                    else (state or {}).get("model_context_window_source")
                ),
                "session_total": session_total,
                "lifetime_total": lifetime_total,
                "usage_source": usage.get("source"),
                "usage_accuracy": usage_accuracy,
                "thread_usage_accuracy": usage_accuracy,
                "session_usage_accuracy": session_usage_accuracy,
                "lifetime_usage_accuracy": lifetime_usage_accuracy,
                "usage_estimated": usage_accuracy == "estimated",
                "usage_cache_available": bool(usage.get("cache_data_available", False)),
                "last_turn_id": turn_id,
                "last_backend": "codex_app_server",
                "continuity_status": "synchronized",
                "last_rotation_reason": (
                    (state or {}).get("last_rotation_reason")
                    if delta.resume
                    else delta.rotation_reason
                ),
                "last_rotation_at": (
                    (state or {}).get("last_rotation_at")
                    if delta.resume
                    else now_iso
                ),
                "fallback_count": int((state or {}).get("fallback_count") or 0),
                "last_fallback_at": (state or {}).get("last_fallback_at"),
                "last_fallback_reason": (state or {}).get("last_fallback_reason"),
                "instruction_transport_version": 1,
                "codex_version": self.codex_version,
                "schema_hash": self.schema_hash,
                "config_signature": self._runtime_config_signature(
                    self._thread_config(reasoning_effort, web_search_mode, reasoning_summary),
                    developer_instructions=developer_instructions,
                    workdir=workdir,
                    permission_profile=permission_profile,
                    approval_policy=approval_policy,
                ),
                "dynamic_tool_signature": self.dynamic_tool_signature,
                "access_signature": access_signature or None,
                "access_mode": request.get("codex_access_mode"),
                "created_at": (state or {}).get("created_at") or now_iso,
                "updated_at": now_iso,
            }
            if incremental_context:
                state_payload.update({
                    "context_policy": "incremental",
                    "thread_reusable": True,
                    "archive_cursor": archive_cursor,
                    "incremental_system_fingerprints": [_message_fingerprint(m) for m in messages if m.get("role") in {"system", "developer"}],
                    "incremental_input_fingerprints": [_message_fingerprint(m) for m in messages if m.get("role") not in {"system", "developer"}],
                })
            if request.get("mabobot_fresh_context"):
                # Keep the last reply visible in the dashboard without making
                # its thread resumable on the next reply.
                state_payload["context_policy"] = "fresh"
                state_payload["thread_reusable"] = False
                self.state_store.put(chat_id, state_payload)
            elif not ephemeral:
                self.state_store.put(chat_id, state_payload)
            codex_job_manager.update(
                request_id,
                status="completed",
                text_chars=len(text),
                attachment_count=len(attachments),
                prompt_tokens=usage.get("prompt_tokens", 0),
                cached_tokens=prompt_details.get("cached_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                usage_accuracy=usage_accuracy,
                compacted=request_compacted,
                continuity_status="synchronized",
            )
            logger.info(
                "Codex App Server turn finished: chat=%s thread=%s turn=%s resumed=%s "
                "elapsed=%.2fs input=%s cached=%s output=%s total=%s",
                chat_id,
                thread_id,
                turn_id,
                delta.resume,
                time.time() - started_at,
                usage.get("prompt_tokens", 0),
                prompt_details.get("cached_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
            )
            now = int(time.time())
            return {
                "id": f"chatcmpl-codex-thread-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "created": now,
                "model": model,
                "backend": "codex_app_server",
                "pool_worker": self.instance_name,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "thread_generation": thread_generation,
                "resumed": bool(delta.resume),
                "rotation_reason": None if delta.resume else delta.rotation_reason,
                "continuity_status": "synchronized",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": text,
                            **({"attachments": attachments} if attachments else {}),
                            **(
                                {"reasoning_content": reasoning_text, "reasoning": reasoning_text}
                                if reasoning_text
                                else {}
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
                "billing_usage_segments": usage_segments,
                "attachments": attachments,
            }
        except Exception as exc:
            if incremental_context:
                self.invalidate_chat(chat_id, reason="incomplete_turn")
            partial = []
            for tracker in turn_trackers:
                partial.extend(tracker.usage_segments or [_normalize_app_server_usage(tracker.usage)])
            if any(partial):
                exc.billing_response = {"model": model, "usage": _merge_attempt_usage(partial),
                                        "billing_usage_segments": partial}
            codex_job_manager.update(request_id, error=str(exc))
            codex_job_manager.record_event(
                request_id,
                "error",
                {"message": str(exc)[:1000]},
            )
            raise
        finally:
            self._deactivate_dynamic_tool_context(thread_id, dynamic_context_token)
            if ephemeral:
                self._loaded_threads.pop(thread_id, None)
                try:
                    self._request("thread/unsubscribe", {"threadId": thread_id}, timeout=10)
                except Exception:
                    logger.debug("Could not unload completed ephemeral thread %s", thread_id, exc_info=True)
            if turn_ids:
                with self._turn_lock:
                    for tracked_turn_id in turn_ids:
                        self._turns.pop(tracked_turn_id, None)
            for image_path in temporary_image_paths:
                try:
                    image_path.unlink(missing_ok=True)
                except Exception:
                    pass
            try:
                if _artifact_output_dir_is_safe(output_dir) and not any(output_dir.iterdir()):
                    output_dir.rmdir()
                if _artifact_request_dir_is_safe(request_dir) and not any(request_dir.iterdir()):
                    request_dir.rmdir()
            except Exception:
                pass
            active_job = codex_job_manager.get_active(request_id)
            if active_job:
                status = str(active_job.get("status") or "")
                if status == "completed":
                    codex_job_manager.record_event(
                        request_id,
                        "job_finished",
                        {"status": "completed"},
                    )
                    codex_job_manager.finish(request_id, status="completed")
                elif status in {"cancelling", "cancelled"}:
                    codex_job_manager.record_event(
                        request_id,
                        "job_finished",
                        {"status": "cancelled"},
                    )
                    codex_job_manager.finish(request_id, status="cancelled", error="cancelled")
                else:
                    final_error = str(active_job.get("error") or "Codex App Server turn failed")
                    codex_job_manager.record_event(
                        request_id,
                        "job_finished",
                        {"status": "failed", "error": final_error},
                    )
                    codex_job_manager.finish(
                        request_id,
                        status="failed",
                        error=final_error,
                    )
