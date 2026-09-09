"""Durable LLM aggregates, independent of bounded request/response history.

Each request and its price snapshot is written atomically with aggregates.
Version 2 starts a new accounting period; old aggregates are discarded.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import sqlite3
import uuid
from decimal import Decimal
from app.services.llm_pricing import decimal

SUM_FIELDS = (
    "calls", "successes", "failures", "tokens", "token_calls", "estimated_calls",
    "input_tokens", "output_tokens", "input_calls", "output_calls", "cost_calls",
    "duration_total", "duration_calls", "cached_tokens", "cache_input_tokens",
    "cache_calls", "cache_write_tokens", "reasoning_tokens",
)


def number(value):
    try:
        result = float(value)
        return max(0, result) if math.isfinite(result) else 0
    except (TypeError, ValueError):
        return 0


def empty_metrics():
    return {**dict.fromkeys(SUM_FIELDS, 0), "costs": {}, "models": {}, "cost_statuses": {}, "cost_sources": {}, "last_call": None}


def merge_metrics(target, source):
    for field in SUM_FIELDS:
        target[field] += number(source.get(field))
    for field in ("costs", "models", "cost_statuses", "cost_sources"):
        for key, value in source.get(field, {}).items():
            target[field][key] = (str(Decimal(str(target[field].get(key, 0))) + Decimal(str(value)))
                                  if field == "costs" else target[field].get(key, 0) + number(value))
    latest = source.get("last_call")
    if latest and (not target["last_call"] or latest > target["last_call"]):
        target["last_call"] = latest
    return target


class LLMUsageService:
    def __init__(self, path, *, clock=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or datetime.now
        self.run_id = uuid.uuid4().hex
        self.run_started_at = self.clock().isoformat()
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS usage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS usage_aggregate (
                bucket TEXT NOT NULL, subject TEXT NOT NULL, task TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY (bucket, subject, task))""")
            db.execute("""CREATE TABLE IF NOT EXISTS usage_subject (
                key TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS usage_run (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS usage_request (
                id TEXT PRIMARY KEY, logical_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
                run_id TEXT NOT NULL, subject TEXT NOT NULL, task TEXT NOT NULL, payload TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS usage_request_scope ON usage_request(subject, task, recorded_at)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_request_time ON usage_request(recorded_at)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_request_logical ON usage_request(subject, task, logical_id)")
            db.execute("BEGIN IMMEDIATE")
            if self._meta(db, "schema_version") != "2":
                # Explicit product reset: no old usage or unlabelled amounts survive.
                for table in ("usage_aggregate", "usage_subject", "usage_run", "usage_request", "usage_meta"):
                    db.execute(f"DELETE FROM {table}")
                self._set_meta(db, "schema_version", "2")
                self._set_meta(db, "initialized_at", self.run_started_at)
            self._migrate_reply_counts(db)

    def _migrate_reply_counts(self, db):
        """Recount existing chat successes without changing usage or price history."""
        if self._meta(db, "reply_count_version") == "1":
            return
        counts = {}
        seen = set()
        for row in db.execute("SELECT * FROM usage_request WHERE task='assistant.chat' ORDER BY recorded_at, id"):
            payload = json.loads(row["payload"])
            key = (row["subject"], row["logical_id"])
            if not payload.get("success") or key in seen:
                continue
            seen.add(key)
            for bucket in ("total", f"day:{row['recorded_at'][:10]}", f"run:{row['run_id']}"):
                bucket_key = (bucket, row["subject"])
                counts[bucket_key] = counts.get(bucket_key, 0) + 1
        rows = db.execute("SELECT * FROM usage_aggregate WHERE task='assistant.chat'").fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            payload["successes"] = counts.get((row["bucket"], row["subject"]), 0)
            db.execute("UPDATE usage_aggregate SET payload=? WHERE bucket=? AND subject=? AND task=?",
                       (json.dumps(payload, ensure_ascii=False), row["bucket"], row["subject"], row["task"]))
        self._set_meta(db, "reply_count_version", "1")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _meta(db, key):
        row = db.execute("SELECT value FROM usage_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _set_meta(db, key, value):
        db.execute("INSERT OR REPLACE INTO usage_meta VALUES (?, ?)", (key, value))

    @staticmethod
    def _put(db, bucket, subject, task, metrics):
        row = db.execute("SELECT payload FROM usage_aggregate WHERE bucket=? AND subject=? AND task=?", (bucket, subject, task)).fetchone()
        if row:
            combined = merge_metrics(empty_metrics(), json.loads(row[0]))
            metrics = merge_metrics(combined, metrics)
        db.execute("INSERT OR REPLACE INTO usage_aggregate VALUES (?, ?, ?, ?)", (bucket, subject, task, json.dumps(metrics, ensure_ascii=False)))

    def record(self, *, task, subject, model, success, usage=None, duration=None, cost=None, currency=None,
               pricing=None, raw=None, connection=None, request_id=None, logical_id=None, provider_request_id=None,
               recorded_at=None, scope="request"):
        now = recorded_at or self.clock()
        request_id = request_id or uuid.uuid4().hex
        pricing = pricing or {"amount": str(cost) if cost is not None else None, "currency": currency,
                              "status": "estimated" if cost is not None else "unknown", "snapshot": {"source": "caller"}}
        metrics = empty_metrics()
        metrics.update(calls=1, successes=int(success), failures=int(not success), last_call=now.isoformat(), models={model: 1})
        usage = usage or {}
        if usage:
            input_value = usage.get("prompt_tokens", usage.get("input_tokens"))
            output_value = usage.get("completion_tokens", usage.get("output_tokens"))
            total = usage.get("total_tokens")
            if total is None and input_value is not None and output_value is not None:
                total = number(input_value) + number(output_value)
            if total is not None:
                metrics.update(tokens=number(total), token_calls=1, estimated_calls=int(bool(usage.get("estimated"))))
            if input_value is not None:
                metrics.update(input_tokens=number(input_value), input_calls=1)
            if output_value is not None:
                metrics.update(output_tokens=number(output_value), output_calls=1)
            cached = usage.get("cached_tokens")
            if cached is not None and usage.get("cache_data_available") is not False:
                cache_input = input_value
                if cache_input is None and usage.get("cache_miss_tokens") is not None:
                    cache_input = number(cached) + number(usage["cache_miss_tokens"])
                if cache_input is not None and number(cached) <= number(cache_input):
                    metrics.update(cached_tokens=number(cached), cache_input_tokens=number(cache_input), cache_calls=1)
        amount = decimal(pricing.get("amount"))
        currency = pricing.get("currency")
        if amount is not None and currency:
            metrics.update(costs={currency: str(amount)}, cost_calls=1)
        metrics["cost_statuses"] = {pricing.get("status", "unknown"): 1}
        metrics["cost_sources"] = {pricing.get("snapshot", {}).get("source", "none"): 1}
        metrics["cache_write_tokens"] = number(usage.get("cache_write_tokens"))
        metrics["reasoning_tokens"] = number(usage.get("reasoning_tokens"))
        payload = {"id": request_id, "logical_id": logical_id or request_id, "recorded_at": now.isoformat(),
                   "model": model, "connection": connection or {}, "success": bool(success), "usage": usage,
                   "raw_usage": raw or {}, "pricing": pricing, "duration": duration,
                   "provider_request_id": provider_request_id, "scope": scope}
        if duration is not None:
            metrics.update(duration_total=number(duration), duration_calls=1)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if task == "assistant.chat" and success:
                previous = db.execute(
                    "SELECT payload FROM usage_request WHERE subject=? AND task=? AND logical_id=?",
                    (subject["key"], task, logical_id or request_id),
                )
                if any(json.loads(row[0]).get("success") for row in previous):
                    metrics["successes"] = 0
            inserted = db.execute("INSERT OR IGNORE INTO usage_request VALUES (?, ?, ?, ?, ?, ?, ?)",
                                  (request_id, logical_id or request_id, now.isoformat(), self.run_id,
                                   subject["key"], task, json.dumps(payload, ensure_ascii=False, allow_nan=False)))
            if not inserted.rowcount:
                return
            db.execute("INSERT OR REPLACE INTO usage_subject VALUES (?, ?, ?)", (subject["key"], subject["name"], subject["kind"]))
            self._set_meta(db, "active_run", self.run_id)
            self._set_meta(db, "run_started_at", self.run_started_at)
            for bucket in ("total", f"day:{now.date().isoformat()}", f"run:{self.run_id}"):
                self._put(db, bucket, subject["key"], task, metrics)
            db.execute("INSERT OR REPLACE INTO usage_run VALUES (?, ?, ?)",
                       (self.run_id, self.run_started_at, now.isoformat()))
            # Prune once a day, preserving other live writers' runtime buckets.
            today = now.date().isoformat()
            if self._meta(db, "pruned_day") != today:
                cutoff = (now.date() - timedelta(days=30)).isoformat()
                db.execute("DELETE FROM usage_aggregate WHERE bucket >= 'day:' AND bucket < ?", (f"day:{cutoff}",))
                db.execute("""DELETE FROM usage_aggregate WHERE bucket IN (
                    SELECT 'run:' || id FROM usage_run WHERE updated_at < ?)""", (cutoff,))
                db.execute("DELETE FROM usage_run WHERE updated_at < ?", (cutoff,))
                self._set_meta(db, "pruned_day", today)

    def query(self, *, period="today", view="task", subject=None):
        if period not in {"today", "7d", "30d", "session", "total"} or view not in {"task", "chat"}:
            raise ValueError("Unsupported usage view")
        now = self.clock()
        with self._connect() as db:
            # Keep metadata and aggregates in the same read snapshot.
            db.execute("BEGIN")
            meta = dict(db.execute("SELECT key, value FROM usage_meta"))
            params = []
            if period in {"today", "7d", "30d"}:
                days = {"today": 1, "7d": 7, "30d": 30}[period]
                start = (now.date() - timedelta(days=days - 1)).isoformat()
                where = "a.bucket BETWEEN ? AND ?"
                params = [f"day:{start}", f"day:{now.date().isoformat()}"]
            else:
                start = None
                where = "a.bucket = ?"
                params = ["total" if period == "total" else f"run:{meta.get('active_run', self.run_id)}"]
            if subject:
                where += " AND a.subject = ?"
                params.append(subject)
            records = db.execute(f"""SELECT a.subject, a.task, a.payload, s.name, s.kind
                FROM usage_aggregate a JOIN usage_subject s ON a.subject=s.key WHERE {where}""", params).fetchall()
            subject_row = db.execute("SELECT * FROM usage_subject WHERE key=?", (subject,)).fetchone() if subject else None
            earliest = db.execute("SELECT MIN(bucket) FROM usage_aggregate WHERE bucket LIKE 'day:%'").fetchone()[0]
        totals = empty_metrics()
        groups = {}
        for record in records:
            metrics = json.loads(record["payload"])
            merge_metrics(totals, metrics)
            key = record["subject"] if view == "chat" else record["task"]
            if key not in groups:
                groups[key] = {"key": key, "name": record["name"] if view == "chat" else key,
                               "kind": record["kind"] if view == "chat" else "task", "metrics": empty_metrics()}
            merge_metrics(groups[key]["metrics"], metrics)
        for metrics in [totals, *(group["metrics"] for group in groups.values())]:
            metrics["costs"] = {key: float(value) for key, value in metrics["costs"].items()}
        return {
            "period": period, "view": view, "subject": dict(subject_row) if subject_row else None,
            "totals": totals, "rows": list(groups.values()),
            "metadata": {"chat_tracking_started_at": meta["initialized_at"],
                         "run_started_at": meta.get("run_started_at"),
                         "daily_available_from": max(
                             (now.date() - timedelta(days=30)).isoformat(),
                             min(meta["initialized_at"][:10], earliest[4:] if earliest else meta["initialized_at"][:10]),
                         ),
                         "range_start": start, "range_end": now.date().isoformat(),
                         "timezone": datetime.now().astimezone().tzname(), "updated_at": now.isoformat()},
        }

    def requests(self, *, period="today", subject=None, task=None, limit=20, offset=0):
        if period not in {"today", "7d", "30d", "session", "total"}:
            raise ValueError("Unsupported usage period")
        where, params = [], []
        now = self.clock()
        with self._connect() as db:
            db.execute("BEGIN")
            if period in {"today", "7d", "30d"}:
                days = {"today": 1, "7d": 7, "30d": 30}[period]
                where += ["recorded_at >= ?", "recorded_at < ?"]
                params += [(now.date() - timedelta(days=days - 1)).isoformat(), (now.date() + timedelta(days=1)).isoformat()]
            elif period == "session":
                where.append("run_id = ?")
                params.append(self._meta(db, "active_run") or self.run_id)
            for name, value in (("subject", subject), ("task", task)):
                if value:
                    where.append(f"{name} = ?")
                    params.append(value)
            sql = " AND ".join(where) or "1=1"
            total = db.execute(f"SELECT COUNT(*) FROM usage_request WHERE {sql}", params).fetchone()[0]
            scopes = db.execute(
                f"SELECT json_extract(payload, '$.scope') AS scope, COUNT(*) AS count FROM usage_request WHERE {sql} GROUP BY scope",
                params,
            ).fetchall()
            request_count = sum(row["count"] for row in scopes if row["scope"] == "request")
            unknown_count = sum(row["count"] for row in scopes if row["scope"] != "request")
            rows = db.execute(f"SELECT payload FROM usage_request WHERE {sql} ORDER BY recorded_at DESC, id DESC LIMIT ? OFFSET ?",
                              [*params, max(1, min(100, limit)), max(0, offset)]).fetchall()
        return {"rows": [json.loads(row[0]) for row in rows], "total": total,
                "request_count": request_count, "summary_count": unknown_count,
                "request_count_exact": unknown_count == 0}
