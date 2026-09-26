"""
Dashboard structured event storage utilities.

This module provides a lightweight JSONL sink for dashboard-critical events
that should not depend on fragile log text parsing.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from mabobot_logging import LOG_PROFILES, ManagedRotatingFileHandler
from app.utils.logging_utils import read_log_lines

logger = logging.getLogger(__name__)
_writer_lock = threading.RLock()
_writer: Optional[ManagedRotatingFileHandler] = None


def _get_events_file() -> Path:
    """Return dashboard events file path."""
    # Imported lazily so the JSONL helpers stay importable without the full
    # database/config stack (tests and CLI tools read the log directly).
    from app.services.config_service import get_setting

    return Path(get_setting("DASHBOARD_EVENTS_FILE", "logs/dashboard_events.jsonl"))


def append_dashboard_event(event_type: str, payload: Dict[str, Any]) -> None:
    """
    Append one structured dashboard event as a JSONL line.

    Args:
        event_type: Event category, e.g. "judge_decision", "web_search"
        payload: Event data
    """
    try:
        events_file = _get_events_file()
        events_file.parent.mkdir(parents=True, exist_ok=True)

        event = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "event_type": event_type,
            "payload": payload,
        }
        global _writer
        with _writer_lock:
            if _writer is None or Path(_writer.baseFilename) != events_file.resolve():
                if _writer is not None:
                    _writer.close()
                _, max_bytes, backups, max_age = LOG_PROFILES["dashboard_events"]
                _writer = ManagedRotatingFileHandler(
                    events_file, max_bytes=max_bytes, backup_count=backups,
                    max_age_days=max_age,
                )
                _writer.setFormatter(logging.Formatter("%(message)s"))
            record = logging.LogRecord(
                "dashboard_events", logging.INFO, __file__, 0,
                json.dumps(event, ensure_ascii=False), (), None,
            )
            _writer.handle(record)
    except Exception as e:
        logger.warning(f"Failed to append dashboard event '{event_type}': {e}")


def get_latest_dashboard_event(event_type: str) -> Optional[Dict[str, Any]]:
    """
    Get latest structured event by type.

    Reads recent JSONL lines from bottom to top and returns the first match.
    """
    try:
        events_file = _get_events_file()
        if not events_file.exists():
            return None

        lines = read_log_lines(events_file, max_lines=5000, include_rotated=True).lines
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue

            if item.get("event_type") == event_type:
                return item
        return None
    except Exception as e:
        logger.warning(f"Failed to read latest dashboard event '{event_type}': {e}")
        return None


def get_recent_dashboard_events(event_type: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Get recent structured events by type, newest first.

    Args:
        event_type: Event category to read.
        limit: Maximum number of events to return.
    """
    try:
        events_file = _get_events_file()
        if not events_file.exists():
            return []

        lines = read_log_lines(events_file, max_lines=5000, include_rotated=True).lines
        events: List[Dict[str, Any]] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue

            if item.get("event_type") != event_type:
                continue

            events.append(item)
            if len(events) >= limit:
                break

        return events
    except Exception as e:
        logger.warning(f"Failed to read recent dashboard events '{event_type}': {e}")
        return []


def get_recent_events(
    limit: int = 50,
    event_types: Optional[List[str]] = None,
    max_bytes: int = 512 * 1024,
) -> List[Dict[str, Any]]:
    """Return recent events of every requested type, newest first.

    Only the tail of the event log is read so the timeline stays cheap even
    when the JSONL sink has been running for months.
    """
    try:
        events_file = _get_events_file()
        if not events_file.exists():
            return []

        budget = max(1, int(max_bytes))
        lines: List[str] = []
        candidates = [events_file] + [
            events_file.with_name(f"{events_file.name}.{index}")
            for index in range(1, LOG_PROFILES["dashboard_events"][2] + 1)
        ]
        for candidate in candidates:
            if budget <= 0:
                break
            try:
                with candidate.open("rb") as stream:
                    stream.seek(0, 2)
                    size = stream.tell()
                    size_to_read = min(size, budget)
                    stream.seek(size - size_to_read)
                    tail = stream.read(size_to_read).decode("utf-8", errors="ignore")
            except FileNotFoundError:
                continue
            part = tail.splitlines()
            if size > size_to_read and part:
                part = part[1:]
            lines.extend(reversed(part))
            budget -= size_to_read

        wanted = {str(item) for item in event_types} if event_types else None
        events: List[Dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            if wanted is not None and item.get("event_type") not in wanted:
                continue
            events.append(item)
            if len(events) >= max(1, min(int(limit), 500)):
                break
        return events
    except Exception as e:
        logger.warning(f"Failed to read recent dashboard events: {e}")
        return []
