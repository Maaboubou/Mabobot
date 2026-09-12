"""Compact incident aggregation over existing rotating application logs."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.utils.logging_utils import read_log_lines


LOG_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d+)?) "
    r"\[(?P<level>ERROR|WARNING|CRITICAL)\] (?P<component>[^:]+): (?P<message>.*)$"
)
VOLATILE_PATTERN = re.compile(
    r"\b(?:[0-9a-f]{8,}|\d{4,}|pid=\d+|request_id=[^\s]+|chat=[^\s,]+|sender=[^\s,]+)\b",
    re.IGNORECASE,
)
TIMESTAMP_FORMATS = ("%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%d %H:%M:%S")


def parse_log_time(value: Any) -> Optional[float]:
    """Return the epoch seconds for a log timestamp, or ``None`` when unparsable."""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


class IncidentService:
    def __init__(self, log_path: Path = Path("logs/app.log"), cache_seconds: float = 10.0):
        self.log_path = log_path
        self.cache_seconds = max(1.0, float(cache_seconds))
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached: List[Dict[str, Any]] = []

    @staticmethod
    def _fingerprint(component: str, message: str) -> tuple[str, str]:
        normalized = VOLATILE_PATTERN.sub("<value>", message.strip())
        normalized = re.sub(r"\s+", " ", normalized)[:500]
        digest = hashlib.sha256(f"{component}|{normalized}".encode("utf-8")).hexdigest()[:16]
        return digest, normalized

    def list(
        self,
        *,
        limit: int = 50,
        scan_lines: int = 10000,
        level: Optional[str] = None,
        within_hours: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Grouped log incidents, newest first.

        ``within_hours`` keeps only incidents whose last occurrence is recent.
        The scan still covers the whole window (and rotated files), so the
        ``count`` stays a lifetime count; only visibility ages out. Unparsable
        timestamps are kept rather than hidden.
        """
        now = time.time()
        with self._lock:
            if now - self._cached_at < self.cache_seconds:
                incidents = list(self._cached)
            else:
                incidents = self._scan(scan_lines=max(100, min(int(scan_lines), 50000)))
                self._cached = incidents
                self._cached_at = now
        if level:
            incidents = [item for item in incidents if item.get("level") == level.upper()]
        if within_hours:
            cutoff = now - max(0.0, float(within_hours)) * 3600
            incidents = [
                item for item in incidents
                if (parse_log_time(item.get("last_seen")) or cutoff + 1) >= cutoff
            ]
        return incidents[: max(1, min(int(limit), 200))]

    def _scan(self, scan_lines: int) -> List[Dict[str, Any]]:
        if not self.log_path.exists():
            return []
        result = read_log_lines(
            self.log_path,
            max_lines=scan_lines,
            include_rotated=True,
        )
        grouped: Dict[str, Dict[str, Any]] = {}
        for line in result.lines:
            match = LOG_PATTERN.match(line.rstrip("\r\n"))
            if not match:
                continue
            values = match.groupdict()
            fingerprint, normalized = self._fingerprint(values["component"], values["message"])
            current = grouped.get(fingerprint)
            if current is None:
                current = {
                    "fingerprint": fingerprint,
                    "level": values["level"],
                    "component": values["component"],
                    "message": values["message"][:1000],
                    "normalized": normalized,
                    "count": 0,
                    "first_seen": values["timestamp"],
                    "last_seen": values["timestamp"],
                }
                grouped[fingerprint] = current
            current["count"] += 1
            current["last_seen"] = values["timestamp"]
            if values["level"] == "CRITICAL" or (
                values["level"] == "ERROR" and current["level"] == "WARNING"
            ):
                current["level"] = values["level"]
            if len(values["message"]) < len(current["message"]):
                current["message"] = values["message"]
        incidents = list(grouped.values())
        incidents.sort(key=lambda item: (item["last_seen"], item["count"]), reverse=True)
        return incidents


_service: Optional[IncidentService] = None


def get_incident_service() -> IncidentService:
    global _service
    if _service is None:
        _service = IncidentService()
    return _service
