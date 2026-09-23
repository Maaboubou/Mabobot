"""Dashboard counts of accepted plugin work, independent of output messages.

Summary Plus already persists one runtime operation per admitted request. Other
message handlers use (plugin, event) as a round identity; Weekly records only
actual per-chat runs, never its recurring scheduler checks. Old sends cannot be
reliably converted to rounds, so they are deliberately not backfilled.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any
import uuid

logger = logging.getLogger(__name__)


def _activity_path() -> Path:
    return Path(os.getenv("PLUGIN_ACTIVITY_DB", "data/plugin_activity.sqlite3"))


def record_plugin_trigger(plugin_id: str, round_id: str | None = None) -> None:
    """Best-effort persistence; duplicate sends in a round do not add triggers."""
    try:
        path = _activity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        with closing(sqlite3.connect(str(path), timeout=5)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS plugin_triggers (
                plugin_id TEXT NOT NULL, round_id TEXT NOT NULL, at TEXT NOT NULL,
                PRIMARY KEY (plugin_id, round_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS plugin_triggers_at ON plugin_triggers(at)")
            db.execute("INSERT OR IGNORE INTO plugin_triggers VALUES (?, ?, ?)",
                       (plugin_id, round_id or uuid.uuid4().hex, now.isoformat(timespec="seconds")))
            db.execute("DELETE FROM plugin_triggers WHERE at < ?",
                       ((now - timedelta(days=8)).isoformat(timespec="seconds"),))
    except Exception:
        logger.warning("记录插件触发统计失败", exc_info=True)


def record_message_trigger(plugin_id: str, event_id: str, *, observe: bool = False,
                           owner_kind: str = "plugin") -> None:
    # These plugins have more precise business admission points. In particular,
    # a duplicate-link/busy response is not a new Summary Plus processing round.
    if observe or owner_kind != "plugin" or plugin_id.rsplit("/", 1)[-1] in {
        "builtin_chat_logger", "summary_plus", "Weekly",
    }:
        return
    record_plugin_trigger(plugin_id, event_id)


def attach_plugin_activity(snapshot: dict[str, Any], plugins: dict[str, Any]) -> dict[str, Any]:
    hours = {row["at"]: row for row in snapshot.get("hourly", [])}
    for row in hours.values():
        row.pop("reply_senders", None)
        row["plugin_triggers"] = []
        row["plugin_triggers_available"] = True
    if not hours:
        return snapshot
    start = datetime.fromisoformat(min(hours) + ":00:00")
    end = datetime.fromisoformat(max(hours) + ":00:00") + timedelta(hours=1)
    counts: dict[tuple[str, str], int] = {}

    def add(hour: str, plugin_id: str, count: int) -> None:
        if hour in hours:
            key = (hour, plugin_id)
            counts[key] = counts.get(key, 0) + count

    try:
        path = _activity_path()
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                for hour, plugin_id, count in db.execute(
                    """SELECT substr(at, 1, 13), plugin_id, count(*) FROM plugin_triggers
                       WHERE at >= ? AND at < ? GROUP BY 1, 2""",
                    (start.isoformat(), end.isoformat()),
                ):
                    add(hour, plugin_id, count)
        runtime_path = Path(os.getenv("RUNTIME_OPERATIONS_DB", "data/runtime_operations.db"))
        if runtime_path.exists():
            # Read directly: constructing the runtime service would mark active
            # operations interrupted. Failed admitted work still counts as a trigger.
            with closing(sqlite3.connect(runtime_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                for (created_at,) in db.execute(
                    """SELECT created_at FROM runtime_operations WHERE owner = ?
                       AND created_at >= ? AND created_at < ?""",
                    ("plugin:summary_plus", start.timestamp(), end.timestamp()),
                ):
                    add(datetime.fromtimestamp(created_at).strftime("%Y-%m-%dT%H"), "summary_plus", 1)
    except (sqlite3.Error, OSError):
        logger.warning("读取插件触发统计失败", exc_info=True)
        for row in hours.values():
            row["plugin_triggers_available"] = False
        return snapshot

    for (hour, plugin_id), count in counts.items():
        plugin = plugins.get(plugin_id)
        if plugin is None or getattr(plugin, "kind", "plugin") != "plugin":
            continue
        config = getattr(plugin, "config", None) or {}
        name = config.get("display_name") or config.get("name") or plugin_id.rsplit("/", 1)[-1]
        hours[hour]["plugin_triggers"].append({"plugin_id": plugin_id, "name": str(name), "count": count})
    for row in hours.values():
        row["plugin_triggers"].sort(key=lambda item: (-item["count"], item["plugin_id"]))
    return snapshot
