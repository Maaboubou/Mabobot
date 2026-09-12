"""Incremental chat-log index for dashboard trends and activity panels.

``/api/dashboard/stats`` used to re-read every JSONL chat log three times per
refresh.  That does not scale to a seven-day trend, so this module keeps one
in-memory index that only parses bytes appended since the previous pass and
prunes everything outside the trend window.

The index answers: messages received, AI replies, active chats (per hour and
per day), the last activity timestamp, minute-level recency for the dashboard
heartbeat, and today's busiest chats.  It deliberately ignores message bodies.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# A first scan of a very large archive only needs its tail: entries older than
# the trend window are discarded anyway.
MAX_INITIAL_TAIL_BYTES = 4 * 1024 * 1024
# Repeated dashboard polls inside this window reuse the previous scan.
SCAN_TTL_SECONDS = 10.0
# The duty desk also polls a five-second heartbeat; that path may scan sooner
# because a scan only ever reads bytes appended since the previous pass.
PULSE_SCAN_TTL_SECONDS = 5.0
PULSE_WINDOW_MINUTES = 60
TOP_CHAT_LIMIT = 6
WINDOW_HOURS = 24
WINDOW_DAYS = 7


@dataclass
class _ChatCounters:
    received: int = 0
    replies: int = 0
    last_at: str = ""


@dataclass
class _Bucket:
    received: int = 0
    replies: int = 0
    chats: Set[str] = field(default_factory=set)


@dataclass
class _FileState:
    offset: int = 0
    skip_partial_line: bool = False
    hours: Dict[str, _Bucket] = field(default_factory=dict)
    minutes: Dict[str, _Bucket] = field(default_factory=dict)
    days: Dict[str, _Bucket] = field(default_factory=dict)
    chats: Dict[str, _ChatCounters] = field(default_factory=dict)
    chats_day: str = ""


class ChatLogIndex:
    """Thread-safe, append-aware aggregation over ``*.jsonl`` chat logs."""

    def __init__(
        self,
        *,
        logs_dir_provider: Optional[Callable[[], Path]] = None,
        bot_name_provider: Optional[Callable[[], str]] = None,
        scan_ttl: float = SCAN_TTL_SECONDS,
    ) -> None:
        self._logs_dir_provider = logs_dir_provider or _default_logs_dir
        self._bot_name_provider = bot_name_provider or _default_bot_name
        self._scan_ttl = max(0.0, float(scan_ttl))
        self._lock = threading.RLock()
        self._files: Dict[str, _FileState] = {}
        self._last_scan = 0.0

    def reset(self) -> None:
        with self._lock:
            self._files.clear()
            self._last_scan = 0.0

    def snapshot(
        self,
        *,
        hours: int = WINDOW_HOURS,
        days: int = WINDOW_DAYS,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Return the refreshed aggregation for the trailing window."""
        hours = max(1, min(int(hours), 168))
        days = max(1, min(int(days), 30))
        moment = (now or datetime.now()).replace(microsecond=0)
        with self._lock:
            self._refresh_locked(moment)
            hourly = [
                {"at": key, **_merge_buckets(state.hours.get(key) for state in self._files.values())}
                for key in _hour_keys(moment, hours)
            ]
            daily = [
                {"date": key, **_merge_buckets(state.days.get(key) for state in self._files.values())}
                for key in _day_keys(moment, days)
            ]
            today_key = moment.date().isoformat()
            yesterday_key = (moment.date() - timedelta(days=1)).isoformat()
            today = _merge_buckets(state.days.get(today_key) for state in self._files.values())
            yesterday = _merge_buckets(state.days.get(yesterday_key) for state in self._files.values())
            top_chats, last_activity = self._today_chats_locked()

        reply_rate = (today["replies"] / today["received"]) if today["received"] else 0.0
        return {
            "generated_at": moment.isoformat(timespec="seconds"),
            "hours": hours,
            "days": days,
            "hourly": hourly,
            "daily": daily,
            "today": {
                "received": today["received"],
                "replies": today["replies"],
                "chats": today["chats"],
                "reply_rate": round(reply_rate, 4),
            },
            "yesterday": {
                "received": yesterday["received"],
                "replies": yesterday["replies"],
                "chats": yesterday["chats"],
            },
            "top_chats": top_chats,
            "last_activity_at": last_activity,
        }

    def today_totals(self, *, now: Optional[datetime] = None) -> Tuple[int, int, int]:
        """Cheap ``(messages, replies, active chats)`` for the current day."""
        moment = (now or datetime.now()).replace(microsecond=0)
        with self._lock:
            self._refresh_locked(moment)
            totals = _merge_buckets(
                state.days.get(moment.date().isoformat()) for state in self._files.values()
            )
        return totals["received"], totals["replies"], totals["chats"]

    def pulse(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Trailing-hour recency readout for the duty-desk heartbeat.

        The console polls this every few seconds to show that messages are
        still arriving, so it only aggregates the last ``PULSE_WINDOW_MINUTES``
        minute buckets and allows a fresher scan than ``snapshot``.
        """
        moment = (now or datetime.now()).replace(microsecond=0)
        with self._lock:
            self._refresh_locked(moment, scan_ttl=PULSE_SCAN_TTL_SECONDS)
            keys = _minute_keys(moment, PULSE_WINDOW_MINUTES)
            _, last_activity = self._today_chats_locked()

            def window(minutes: int) -> Dict[str, int]:
                selected = keys[-minutes:]
                return _merge_buckets(
                    state.minutes.get(key) for key in selected for state in self._files.values()
                )

            windows = {
                f"{minutes}m": window(minutes)
                for minutes in (1, 5, 15, PULSE_WINDOW_MINUTES)
            }
        return {
            "generated_at": moment.isoformat(timespec="seconds"),
            "last_activity_at": last_activity,
            "windows": windows,
        }

    # -- internals ---------------------------------------------------------

    def _refresh_locked(self, moment: datetime, *, scan_ttl: Optional[float] = None) -> None:
        now_monotonic = time.monotonic()
        ttl = self._scan_ttl if scan_ttl is None else max(0.0, float(scan_ttl))
        if ttl and now_monotonic - self._last_scan < ttl:
            return
        self._last_scan = now_monotonic

        try:
            logs_dir = Path(self._logs_dir_provider())
        except Exception as exc:  # pragma: no cover - config service failure
            logger.warning("读取聊天日志目录失败: %s", exc)
            return
        if not logs_dir.exists():
            return
        try:
            bot_name = str(self._bot_name_provider() or "")
        except Exception:
            bot_name = ""
        day_cutoff = (moment.date() - timedelta(days=WINDOW_DAYS)).isoformat()
        today_key = moment.date().isoformat()

        seen: Set[str] = set()
        for log_file in sorted(logs_dir.glob("*.jsonl")):
            key = str(log_file)
            seen.add(key)
            state = self._files.get(key)
            if state is None:
                state = self._files[key] = _FileState()
                try:
                    size = log_file.stat().st_size
                except OSError:
                    continue
                if size > MAX_INITIAL_TAIL_BYTES:
                    state.offset = size - MAX_INITIAL_TAIL_BYTES
                    state.skip_partial_line = True
            try:
                size = log_file.stat().st_size
            except OSError:
                continue
            if size < state.offset:
                # Rotated or rewritten: drop the stale counters, then re-read.
                state.hours.clear()
                state.minutes.clear()
                state.days.clear()
                state.chats.clear()
                state.chats_day = ""
                state.offset = 0
                state.skip_partial_line = False
            if size > state.offset:
                self._consume(state, log_file, bot_name=bot_name, day_cutoff=day_cutoff,
                              today_key=today_key)
            self._prune_state(state, moment, day_cutoff, today_key)

        for key in list(self._files):
            if key not in seen:
                del self._files[key]

    def _consume(
        self,
        state: _FileState,
        log_file: Path,
        *,
        bot_name: str,
        day_cutoff: str,
        today_key: str,
    ) -> None:
        try:
            with open(log_file, "rb") as handle:
                handle.seek(state.offset)
                skip_partial = state.skip_partial_line
                for raw_line in handle:
                    if skip_partial:
                        # The seek landed mid-line; drop the remainder of it.
                        skip_partial = False
                        if not raw_line.endswith(b"\n"):
                            continue
                    entry = _parse_entry(raw_line)
                    if entry is None:
                        continue
                    time_str = str(entry.get("time") or "")
                    day_key = time_str[:10]
                    if day_key < day_cutoff:
                        continue
                    hour_key = f"{day_key}T{time_str[11:13]}"
                    minute_key = f"{day_key}T{time_str[11:16]}"
                    chat_name = log_file.stem
                    sender = str(entry.get("sender") or "")
                    is_bot = entry.get("is_bot") is True or bool(bot_name and sender == bot_name)
                    _bump(state.hours, hour_key, chat_name, is_bot)
                    _bump(state.minutes, minute_key, chat_name, is_bot)
                    _bump(state.days, day_key, chat_name, is_bot)
                    if day_key == today_key:
                        if state.chats_day != today_key:
                            state.chats.clear()
                            state.chats_day = today_key
                        counters = state.chats.setdefault(chat_name, _ChatCounters())
                        if is_bot:
                            counters.replies += 1
                        else:
                            counters.received += 1
                        if time_str > counters.last_at:
                            counters.last_at = time_str
                state.skip_partial_line = False
                state.offset = handle.tell()
        except OSError as exc:
            logger.warning("读取聊天日志失败 %s: %s", log_file, exc)

    @staticmethod
    def _prune_state(state: _FileState, moment: datetime, day_cutoff: str, today_key: str) -> None:
        hour_floor = _hour_keys(moment, WINDOW_HOURS)[0]
        minute_floor = _minute_keys(moment, PULSE_WINDOW_MINUTES)[0]
        for key in [key for key in state.hours if key < hour_floor]:
            del state.hours[key]
        for key in [key for key in state.minutes if key < minute_floor]:
            del state.minutes[key]
        for key in [key for key in state.days if key < day_cutoff]:
            del state.days[key]
        if state.chats_day and state.chats_day != today_key:
            state.chats.clear()
            state.chats_day = ""

    def _today_chats_locked(self) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        merged: Dict[str, _ChatCounters] = {}
        for state in self._files.values():
            for chat_name, counters in state.chats.items():
                item = merged.setdefault(chat_name, _ChatCounters())
                item.received += counters.received
                item.replies += counters.replies
                if counters.last_at > item.last_at:
                    item.last_at = counters.last_at
        last_activity = max((item.last_at for item in merged.values() if item.last_at), default=None)
        top = sorted(
            merged.items(),
            key=lambda pair: (pair[1].received, pair[1].last_at),
            reverse=True,
        )[:TOP_CHAT_LIMIT]
        return (
            [
                {
                    "chat_name": chat_name,
                    "received": counters.received,
                    "replies": counters.replies,
                    "last_at": counters.last_at or None,
                }
                for chat_name, counters in top
            ],
            last_activity,
        )


def _parse_entry(raw_line: bytes) -> Optional[Dict[str, Any]]:
    try:
        entry = json.loads(raw_line.decode("utf-8", errors="ignore").strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(entry, dict):
        return None
    time_str = str(entry.get("time") or "")
    return entry if len(time_str) >= 16 else None


def _bump(buckets: Dict[str, _Bucket], key: str, chat_name: str, is_bot: bool) -> None:
    bucket = buckets.get(key)
    if bucket is None:
        bucket = buckets[key] = _Bucket()
    if is_bot:
        bucket.replies += 1
    else:
        bucket.received += 1
    bucket.chats.add(chat_name)


def _merge_buckets(buckets: Iterable[Optional[_Bucket]]) -> Dict[str, int]:
    received = replies = 0
    chats: Set[str] = set()
    for bucket in buckets:
        if bucket is None:
            continue
        received += bucket.received
        replies += bucket.replies
        chats |= bucket.chats
    return {"received": received, "replies": replies, "chats": len(chats)}


def _hour_keys(moment: datetime, hours: int) -> List[str]:
    end = moment.replace(minute=0, second=0)
    return [
        (end - timedelta(hours=offset)).strftime("%Y-%m-%dT%H")
        for offset in range(hours - 1, -1, -1)
    ]


def _minute_keys(moment: datetime, minutes: int) -> List[str]:
    end = moment.replace(second=0)
    return [
        (end - timedelta(minutes=offset)).strftime("%Y-%m-%dT%H:%M")
        for offset in range(minutes - 1, -1, -1)
    ]


def _day_keys(moment: datetime, days: int) -> List[str]:
    today = moment.date()
    return [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]


def _default_logs_dir() -> Path:
    from app.services.config_service import get_setting

    return Path(get_setting("CHAT_LOG_DIR", "data/chat_logs"))


def _default_bot_name() -> str:
    from app.services.config_service import get_setting

    return str(get_setting("WECHAT_BOT_NAME", "刘局") or "")


_index_lock = threading.Lock()
_index: Optional[ChatLogIndex] = None


def get_chat_log_index() -> ChatLogIndex:
    """Return the process-wide chat-log index."""
    global _index
    if _index is None:
        with _index_lock:
            if _index is None:
                _index = ChatLogIndex()
    return _index
