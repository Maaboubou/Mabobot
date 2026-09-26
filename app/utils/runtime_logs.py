"""Read the current structured runtime logs for the Web console."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from mabobot_logging import LOG_PROFILES, log_path
from app.utils.logging_utils import LogReadResult, read_log_lines


def source_path(source: str) -> Path:
    if source not in LOG_PROFILES:
        raise ValueError(f"Unknown log source: {source}")
    if source == "mabowx" and os.getenv("MABOWX_LOG_DIR"):
        return Path(os.environ["MABOWX_LOG_DIR"]) / "mabowx.jsonl"
    if source == "dashboard_events":
        from app.utils.dashboard_events import _get_events_file
        return _get_events_file()
    return log_path(source)


def source_paths(source: str) -> list[Path]:
    path = source_path(source)
    if source != "app":
        return [path]
    return [path] + sorted(path.parent.glob("app.[0-9]*.jsonl"))


def parse_record(line: str) -> dict[str, Any] | None:
    try:
        item = json.loads(line)
    except (ValueError, TypeError):
        return None
    return item if isinstance(item, dict) else None


def render_record(item: dict[str, Any]) -> str:
    if "event_type" in item and "payload" in item:
        timestamp = str(item.get("timestamp") or "")
        detail = json.dumps(item.get("payload"), ensure_ascii=False, default=str)
        return f"{timestamp} [EVENT] dashboard_events: {item['event_type']} {detail}\n"
    timestamp = str(item.get("ts") or "").replace("T", " ")
    level = str(item.get("level") or "INFO")
    logger = str(item.get("logger") or item.get("service") or "unknown")
    event = str(item.get("event") or "unclassified")
    identity = " ".join(
        f"{key}={item[key]}" for key in ("trace_id", "request_id", "message_id")
        if item.get(key)
    )
    suffix = f" [{event}]" if event != "unclassified" else ""
    if identity:
        suffix += f" {identity}"
    message = str(item.get("message") or "").replace("\r", "\\r").replace("\n", "\\n")
    exception = item.get("exception")
    if exception:
        message += " | exception=" + str(exception).replace("\r", "\\r").replace("\n", "\\n")
    return f"{timestamp} [{level}] {logger}:{suffix} {message}\n"


def read_runtime_logs(source: str, *, max_lines: int = 100,
                      search: str | None = None, plugin_name: str | None = None,
                      level: str | None = None, trace_id: str | None = None,
                      event: str | None = None, from_time: str | None = None,
                      to_time: str | None = None) -> tuple[str, LogReadResult]:
    paths = source_paths(source)
    required_text = f"app.plugins.{plugin_name}" if plugin_name and source == "app" else None
    filter_level = str(level or "").upper().strip()
    filter_trace = str(trace_id or "").strip()
    filter_event = str(event or "").strip()
    def boundary(raw: str | None) -> datetime | None:
        if not raw:
            return None
        parsed = datetime.fromisoformat(raw)
        return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed

    start = boundary(from_time)
    end = boundary(to_time)
    if start and end and start > end:
        raise ValueError("Start time must not be later than end time")

    def in_time_range(item: dict[str, Any]) -> bool:
        if start is None and end is None:
            return True
        try:
            stamp = datetime.fromisoformat(str(item.get("ts") or item.get("timestamp")))
            if stamp.tzinfo is not None:
                stamp = stamp.astimezone().replace(tzinfo=None)
            return (start is None or stamp >= start) and (end is None or stamp <= end)
        except ValueError:
            return False

    def matches(line: str) -> bool:
        item = parse_record(line)
        return bool(item and
                    (not filter_level or item.get("level", "EVENT") == filter_level) and
                    (not filter_trace or item.get("trace_id") == filter_trace) and
                    (not filter_event or filter_event.casefold() in
                     str(item.get("event") or item.get("event_type") or "").casefold()) and
                    in_time_range(item))

    results = [read_log_lines(
        path, max_lines=max_lines, search=search, required_text=required_text,
        include_rotated=True,
        line_filter=matches if filter_level or filter_trace or filter_event or start or end else None,
    ) for path in paths if path.is_file()]
    if not results:
        result = LogReadResult([], None, None, False, "tail")
    elif len(results) == 1:
        result = results[0]
    else:
        combined = [line for part in results for line in part.lines]
        combined.sort(key=lambda line: str((parse_record(line) or {}).get("ts") or ""))
        result = LogReadResult(
            lines=combined[-max_lines:],
            total_lines=sum(part.total_lines or 0 for part in results)
            if all(part.counts_exact for part in results) else None,
            filtered_count=sum(part.filtered_count or 0 for part in results)
            if all(part.counts_exact for part in results) else None,
            counts_exact=all(part.counts_exact for part in results),
            strategy="merged",
        )
    content = "".join(render_record(item) for line in result.lines
                      if (item := parse_record(line)) is not None)
    return content, result


def log_status() -> list[dict[str, Any]]:
    def stat(path: Path):
        try:
            return path.stat()
        except OSError:
            return None

    streams = []
    for source, (_, max_bytes, backups, max_age) in LOG_PROFILES.items():
        paths = source_paths(source)
        path = paths[0]
        candidates = [candidate for active in paths
                      for candidate in [active] + [active.with_name(f"{active.name}.{i}")
                      for i in range(1, backups + 1)]]
        existing = [(candidate, info) for candidate in candidates
                    if (info := stat(candidate)) is not None]
        active = [(candidate, info) for candidate in paths
                  if (info := stat(candidate)) is not None]
        current_path, current = max(active, key=lambda item: item[1].st_mtime) if active else (path, None)
        streams.append({
            "source": source,
            "active_file": str(current_path),
            "file_exists": current is not None,
            "active_bytes": sum(info.st_size for _, info in active),
            "total_bytes": sum(info.st_size for _, info in existing),
            "last_write": datetime.fromtimestamp(current.st_mtime).astimezone().isoformat(timespec="seconds")
            if current else None,
            "file_count": len(existing),
            "max_bytes": max_bytes,
            "backup_count": backups,
            "max_age_days": max_age,
            "status": "missing" if current is None else
                      ("rollover_pending" if any(info.st_size > max_bytes for _, info in active) else "ok"),
        })
    return streams
