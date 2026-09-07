"""Durable JSONL journal with a rebuildable SQLite search projection.

Writers serialize through SQLite BEGIN IMMEDIATE, fsync the journal before
committing its projection, and recover unindexed journal tails after a crash.
The authority is application-owned; bots receive separate current-chat snapshots.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable
import uuid
from app.history.members import MemberDirectory, member_schema, nickname_id, project_member


DEFAULT_ROOT = Path("data/chat_archive")
LOCAL_TZ = timezone(timedelta(hours=8))


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalized_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("message time is required")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


class ArchiveStore(MemberDirectory):
    def __init__(self, root: Path | str = DEFAULT_ROOT):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "index.sqlite3"
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS messages(
                    seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
                    chat TEXT NOT NULL, occurred TEXT NOT NULL,
                    sender TEXT NOT NULL, sender_id TEXT NOT NULL,
                    content TEXT NOT NULL, kind TEXT NOT NULL, search_text TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS message_time ON messages(chat,occurred,seq);
                CREATE INDEX IF NOT EXISTS message_sender ON messages(chat,sender_id,occurred);
                CREATE TABLE IF NOT EXISTS aliases(
                    chat TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, PRIMARY KEY(chat,source,source_id));
                CREATE TABLE IF NOT EXISTS journal_files(
                    path TEXT PRIMARY KEY, offset INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS live_counters(chat TEXT PRIMARY KEY, high_water INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS changes(
                    seq INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
                    chat TEXT NOT NULL, message_id TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS changes_chat ON changes(chat,seq);
                CREATE INDEX IF NOT EXISTS changes_message ON changes(chat,message_id,seq);
                CREATE TABLE IF NOT EXISTS receipts(
                    chat TEXT NOT NULL, query_key TEXT NOT NULL, snapshot INTEGER NOT NULL,
                    result TEXT NOT NULL, uses INTEGER NOT NULL DEFAULT 1,
                    used_at TEXT NOT NULL, PRIMARY KEY(chat,query_key));
                CREATE TABLE IF NOT EXISTS audit(
                    seq INTEGER PRIMARY KEY, chat TEXT NOT NULL, request_id TEXT NOT NULL,
                    tool TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS annotations(
                    id TEXT PRIMARY KEY, chat TEXT NOT NULL, payload TEXT NOT NULL);
            """)
            member_schema(db)
            if "search_text" not in {row[1] for row in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN search_text TEXT NOT NULL DEFAULT ''")
                db.execute("UPDATE messages SET search_text=content")
                db.commit()
            # Trigram supports Chinese substrings >= 3 characters. Short queries
            # deliberately use literal instr(), never unicode61 word boundaries.
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(content, tokenize='trigram')")
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
            db.execute("BEGIN IMMEDIATE")
            self._recover(db)
            # Startup catch-up also covers messages written by an older worker
            # while the new directory was being prepared. Only names are read.
            for chat, name in db.execute('SELECT DISTINCT chat,sender FROM messages'):
                db.execute('INSERT OR IGNORE INTO chat_senders VALUES(?,?,?)',(nickname_id(chat,name),chat,name))
            db.commit()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.db_path, timeout=60)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    def _project(self, db, event):
        if db.execute("SELECT 1 FROM changes WHERE event_id=?", (event["event_id"],)).fetchone():
            return
        row = event["message"]
        if event.get("annotation"):
            db.execute("INSERT OR REPLACE INTO annotations VALUES(?,?,?)",
                       (row["id"], row["chat"], encode(row)))
            if row.get('kind') == 'chat_member':
                project_member(db,row)
                # A rename/merge is one journal line, so crash recovery cannot
                # apply the surviving member without its duplicate tombstone.
                for update in event.get('member_updates', []):
                    if update['chat'] != row['chat']:
                        raise ValueError('member merge cannot cross chats')
                    project_member(db,update)
        else:
            db.execute('INSERT OR IGNORE INTO chat_senders VALUES(?,?,?)',(nickname_id(row['chat'],row['sender']),row['chat'],row['sender']))
            search_text = row["content"]
            description = str((row.get("image_enrichment") or {}).get("description") or "")
            if description and description not in search_text:
                search_text += "\n" + description
            previous = db.execute("SELECT seq FROM messages WHERE id=?", (row["id"],)).fetchone()
            db.execute("""INSERT INTO messages(id,chat,occurred,sender,sender_id,content,kind,version,deleted,payload,search_text)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                occurred=excluded.occurred,sender=excluded.sender,sender_id=excluded.sender_id,
                content=excluded.content,kind=excluded.kind,version=excluded.version,
                deleted=excluded.deleted,payload=excluded.payload,search_text=excluded.search_text""",
                       (row["id"], row["chat"], row["occurred_at"], row["sender"],
                        row.get("sender_id", ""), row["content"], row.get("message_type", "text"),
                        row["version"], int(row.get("deleted", False)), encode(row), search_text))
            seq = previous[0] if previous else db.execute("SELECT seq FROM messages WHERE id=?", (row["id"],)).fetchone()[0]
            if self.fts:
                db.execute("DELETE FROM message_fts WHERE rowid=?", (seq,))
                if not row.get("deleted"):
                    db.execute("INSERT INTO message_fts(rowid,content) VALUES(?,?)", (seq, search_text))
            for source, source_id in row.get("source_refs", []):
                db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?,?)",
                           (row["chat"], source, source_id, row["id"]))
            if row.get("log_sequence"):
                db.execute("INSERT INTO live_counters VALUES(?,?) ON CONFLICT(chat) DO UPDATE SET high_water=max(high_water,excluded.high_water)", (row["chat"], int(row["log_sequence"])))
        db.execute("INSERT INTO changes(event_id,chat,message_id) VALUES(?,?,?)",
                   (event["event_id"], row["chat"], row["id"]))

    def _recover(self, db):
        """Only read journal bytes beyond committed offsets; preserve torn tails."""
        for path in sorted((self.root / "raw").glob("*/*.jsonl")):
            relative = path.relative_to(self.root).as_posix()
            saved = db.execute("SELECT offset FROM journal_files WHERE path=?", (relative,)).fetchone()
            offset = saved[0] if saved else 0
            if path.stat().st_size < offset:
                raise RuntimeError(f"archive journal was truncated: {relative}")
            if path.stat().st_size == offset:
                continue
            with path.open("rb+") as stream:
                stream.seek(offset)
                while line := stream.readline():
                    if not line.endswith(b"\n"):
                        # A crash may leave an unfinished append. Keep the bytes
                        # for diagnosis before removing this uncommitted tail.
                        quarantine = self.root / "recovery"
                        quarantine.mkdir(exist_ok=True)
                        (quarantine / f"{uuid.uuid4().hex}.partial").write_bytes(line)
                        stream.truncate(offset)
                        stream.flush()
                        os.fsync(stream.fileno())
                        break
                    self._project(db, json.loads(line))
                    offset = stream.tell()
            db.execute("INSERT OR REPLACE INTO journal_files VALUES(?,?)", (relative, offset))

    def _journal(self, db, events):
        if not events:
            return
        # A tiny durable marker avoids stat/read scans over the entire archive
        # on every live message. Under the SQLite writer lock, a marker whose
        # final event is indexed proves the previous append committed.
        marker = self.root / "pending.json"
        temporary = self.root / f"pending-{uuid.uuid4().hex}.tmp"
        with temporary.open("w", encoding="utf-8") as pending:
            pending.write(encode({"event_id": events[-1]["event_id"]}))
            pending.flush()
            os.fsync(pending.fileno())
        os.replace(temporary, marker)
        streams = {}
        heads = {}
        sizes = {}
        try:
            for event in events:
                row = event["message"]
                scope = hashlib.sha256(row["chat"].encode()).hexdigest()[:24]
                folder = self.root / "raw" / scope
                # The journal is partitioned by ingestion day, and each shard
                # capped near 1 MiB. occurred_at controls historical ordering.
                day = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
                if folder not in heads:
                    folder.mkdir(parents=True, exist_ok=True)
                    prefix = f"raw/{scope}/{day}-"
                    saved = db.execute("SELECT path,offset FROM journal_files WHERE path>=? AND path<? ORDER BY path DESC LIMIT 1", (prefix, prefix + "\uffff")).fetchone()
                    heads[folder] = self.root / saved["path"] if saved else folder / f"{day}-000001.jsonl"
                    sizes[heads[folder]] = saved["offset"] if saved else 0
                path = heads[folder]
                size = streams[path].tell() if path in streams else sizes.get(path, 0)
                if size >= 1024 * 1024:
                    number = int(path.stem.rsplit("-", 1)[1]) + 1
                    path = folder / f"{day}-{number:06d}.jsonl"
                    heads[folder] = path
                if path not in streams:
                    streams[path] = path.open("ab", buffering=0)
                streams[path].write((encode(event) + "\n").encode("utf-8"))
                self._project(db, event)
            for path, stream in streams.items():
                os.fsync(stream.fileno())
                db.execute("INSERT OR REPLACE INTO journal_files VALUES(?,?)",
                           (path.relative_to(self.root).as_posix(), stream.tell()))
        finally:
            for stream in streams.values():
                stream.close()

    def _recover_if_needed(self, db):
        marker = self.root / "pending.json"
        if marker.exists():
            event_id = json.loads(marker.read_text(encoding="utf-8"))["event_id"]
            if not db.execute("SELECT 1 FROM changes WHERE event_id=?", (event_id,)).fetchone():
                self._recover(db)

    def append_many(self, chat: str, rows: Iterable[dict], *, source: str) -> dict:
        if not chat or not source:
            raise ValueError("chat and source are required")
        stats = {"inserted": 0, "linked": 0, "unchanged": 0, "conflicts": 0, "ids": []}
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover_if_needed(db)
            events = []
            # Apply each event before the next row so duplicates within a batch
            # resolve to the same canonical message as duplicates across batches.
            for raw in rows:
                refs = []
                platform_id = str(raw.get("source_message_id") or raw.get("source_id") or "")
                if platform_id:
                    # UI/cache handles and export platform IDs are different
                    # identity domains. Never merge them by their spelling.
                    identity_source = raw.get("source_id_namespace") or ("wechat_export" if raw.get("source_id") else "wx_action")
                    if identity_source == "wx_action":
                        # Old image cache handles were reused for different
                        # deliveries. Qualify with occurrence time; the legacy
                        # row UUID remains the stronger cross-source identity.
                        platform_id = encode([platform_id, normalized_time(raw.get("occurred_at") or raw.get("time"))])
                    refs.append((str(identity_source), platform_id))
                if raw.get("id"):
                    refs.append(("legacy_uuid", str(raw["id"])))
                if raw.get("log_sequence"):
                    refs.append(("live_sequence", str(raw["log_sequence"])))
                provenance_id = str(raw.get("_source_record_id") or raw.get("id") or platform_id or "")
                if not provenance_id:
                    raise ValueError("a stable source record id is required")
                refs.append((source, provenance_id))
                ids = {match[0] for ref in refs if (match := db.execute(
                    "SELECT message_id FROM aliases WHERE chat=? AND source=? AND source_id=?",
                    (chat, *ref)).fetchone())}
                if len(ids) > 1:
                    raise ValueError("conflicting source ID mappings; migration must resolve them")
                existing = db.execute("SELECT payload FROM messages WHERE id=?", (next(iter(ids)),)).fetchone() if ids else None
                old = json.loads(existing[0]) if existing else None
                if old:
                    old["source_refs"] = [list(ref) for ref in sorted(set(map(tuple, old["source_refs"])))]
                row = dict(raw)
                row.pop("_source_record_id", None)
                row.update(id=old["id"] if old else uuid.uuid4().hex, chat=chat,
                           content=str(raw.get("content") or ""), sender=str(raw.get("sender") or "未知"),
                           occurred_at=normalized_time(raw.get("occurred_at") or raw.get("time")),
                           ingested_at=datetime.now(timezone.utc).isoformat(), version=1,
                           source_refs=[list(ref) for ref in sorted(set(refs))])
                if old:
                    merged = dict(old)
                    merged["source_refs"] = [list(ref) for ref in sorted(set(map(tuple, old["source_refs"])) | set(refs))]
                    # Never silently rewrite content/time from a lower-fidelity
                    # evidence copy. Preserve conflicting imports for review.
                    if row["content"] != old["content"] or row["occurred_at"] != old["occurred_at"]:
                        conflicts = list(old.get("import_conflicts", []))
                        conflict = {"source": source, "record": provenance_id, "content": row["content"], "time": row.get("time")}
                        if conflict not in conflicts:
                            conflicts.append(conflict)
                        merged["import_conflicts"] = conflicts
                        stats["conflicts"] += 1
                    for key in ("sender_id", "sender_remark", "message_type", "metadata", "image_enrichment", "source_message_id"):
                        if not merged.get(key) and row.get(key):
                            merged[key] = row[key]
                    if merged == old:
                        stats["unchanged"] += 1
                        stats["ids"].append(old["id"])
                        continue
                    row = merged
                    row["version"] = old["version"] + 1
                    stats["linked"] += 1
                else:
                    stats["inserted"] += 1
                event = {"event_id": uuid.uuid4().hex, "message": row}
                self._project(db, event)
                events.append(event)
                stats["ids"].append(row["id"])
            self._journal(db, events)
            db.commit()
        return stats

    def append(self, chat: str, row: dict, *, source: str = "live") -> str:
        return self.append_many(chat, [row], source=source)["ids"][0]

    def revise(self, chat: str, message_id: str, **updates) -> bool:
        allowed = {"content", "deleted", "metadata", "image_enrichment", "correction"}
        if set(updates) - allowed:
            raise ValueError("unsupported archive revision fields")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover_if_needed(db)
            found = db.execute("SELECT payload FROM messages WHERE chat=? AND id=?", (chat, message_id)).fetchone()
            if not found:
                found = db.execute("SELECT m.payload FROM aliases a JOIN messages m ON m.id=a.message_id WHERE a.chat=? AND a.source='legacy_uuid' AND a.source_id=?", (chat, message_id)).fetchone()
            if not found:
                return False
            row = json.loads(found[0])
            row.update(updates)
            row["version"] += 1
            self._journal(db, [{"event_id": uuid.uuid4().hex, "message": row}])
            db.commit()
            return True

    def annotate(self, chat: str, identity: str, payload: dict):
        """Preserve manual corrections/confirmed identity metadata, not AI profiles."""
        row = {"id": identity, "chat": chat, **payload}
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover_if_needed(db)
            old = db.execute("SELECT payload FROM annotations WHERE id=?", (identity,)).fetchone()
            if not old or json.loads(old[0]) != row:
                self._journal(db, [{"event_id": uuid.uuid4().hex, "message": row, "annotation": True}])
            db.commit()

    def recent(self, chat: str, limit: int = 50) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM messages WHERE chat=? AND deleted=0 ORDER BY occurred DESC,seq DESC LIMIT ?", (chat, max(0, min(limit, 20000)))).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def context_snapshot(self, chat: str, after: int | None = None) -> tuple[int, list[dict], bool]:
        """Atomically read a cursor and its initial/recent changed records.

        Bound exceptional backfills; callers must disclose omitted records.
        The cursor is acknowledged only after a successful model response.
        """
        with self.connection() as db:
            db.execute("BEGIN")
            cursor = db.execute("SELECT coalesce(max(seq),0) FROM changes WHERE chat=?", (chat,)).fetchone()[0]
            if after is None:
                rows = db.execute("SELECT payload FROM messages WHERE chat=? AND deleted=0 ORDER BY occurred DESC,seq DESC LIMIT 50", (chat,)).fetchall()
                return cursor, [json.loads(row[0]) for row in reversed(rows)], False
            rows = db.execute("""SELECT m.payload FROM messages m JOIN
                (SELECT message_id,max(seq) AS latest FROM changes WHERE chat=? AND seq>? AND seq<=? GROUP BY message_id) c
                ON m.id=c.message_id WHERE m.chat=? AND m.deleted=0
                ORDER BY c.latest DESC LIMIT 2001""", (chat, after, cursor, chat)).fetchall()
            return cursor, [json.loads(row[0]) for row in reversed(rows[:2000])], len(rows) > 2000

    def live_high_water(self, chat: str) -> int:
        with self.connection() as db:
            row = db.execute("SELECT high_water FROM live_counters WHERE chat=?", (chat,)).fetchone()
            if row:
                return row[0]
            # Upgrade existing projections lazily. Normal live allocations use
            # the tiny counter table, never recount the complete raw archive.
            value = db.execute("SELECT coalesce(max(CAST(source_id AS INTEGER)),0) FROM aliases WHERE chat=? AND source='live_sequence'", (chat,)).fetchone()[0]
            db.execute("INSERT OR IGNORE INTO live_counters VALUES(?,?)", (chat, value))
            db.commit()
            return value

    def snapshot(self, chat: str) -> int:
        with self.connection() as db:
            return db.execute("SELECT coalesce(max(seq),0) FROM changes WHERE chat=?", (chat,)).fetchone()[0]

    def search(self, chat: str, *, query: str = "", sender: str = "", start: str = "", end: str = "", offset: int = 0, limit: int = 6) -> dict:
        query, sender = query.strip(), sender.strip()
        if len(query) > 200 or len(sender) > 160 or offset < 0 or offset > 1000000:
            raise ValueError("search parameters exceed bounds")
        limit = max(1, min(limit, 100))
        key = encode([query, sender, start, end, offset, limit, "nickname-v3"])
        with self.connection() as db:
            db.execute("BEGIN")
            snapshot = db.execute("SELECT coalesce(max(seq),0) FROM changes WHERE chat=?", (chat,)).fetchone()[0]
            cached = db.execute("SELECT result FROM receipts WHERE chat=? AND query_key=? AND snapshot=?", (chat, key, snapshot)).fetchone()
            if cached:
                result = json.loads(cached[0])
                result["cache_hit"] = True
                db.commit()
                db.execute("UPDATE receipts SET uses=uses+1,used_at=? WHERE chat=? AND query_key=?", (datetime.now(timezone.utc).isoformat(), chat, key))
                db.commit()
                return result
            clauses, params = ["m.chat=?", "m.deleted=0"], [chat]
            previous = db.execute("SELECT result,snapshot FROM receipts WHERE chat=? AND query_key=?", (chat, key)).fetchone() if offset == 0 else None
            incremental = False
            previous_more = False
            if previous:
                old_snapshot = previous["snapshot"]
                # Only append-only changes can reuse the old top results.
                # Revisions, deletion, aliases and corrections force requery.
                revised = db.execute("""SELECT 1 FROM changes fresh WHERE fresh.chat=? AND fresh.seq>?
                    AND (EXISTS(SELECT 1 FROM changes old WHERE old.chat=fresh.chat AND old.message_id=fresh.message_id AND old.seq<=?)
                         OR EXISTS(SELECT 1 FROM annotations a WHERE a.id=fresh.message_id)) LIMIT 1""", (chat, old_snapshot, old_snapshot)).fetchone()
                if not revised:
                    old_result = json.loads(previous["result"])
                    previous_more = old_result.get("next_offset") is not None
                    old_ids = old_result["ids"]
                    placeholders = ",".join("?" for _ in old_ids) or "NULL"
                    clauses.append(f"(m.id IN ({placeholders}) OR m.id IN (SELECT message_id FROM changes WHERE chat=? AND seq>?))")
                    params.extend([*old_ids, chat, old_snapshot])
                    incremental = True
            if query:
                if self.fts and len(query) >= 3:
                    clauses.append("m.seq IN (SELECT rowid FROM message_fts WHERE message_fts MATCH ?)")
                    params.append('"' + query.replace('"', '""') + '"')
                clauses.append("instr(lower(m.search_text),lower(?))>0")
                params.append(query)
            if sender:
                members = self._resolve_members(db,chat,sender)
                if len(members)>1:
                    return {'ids':[], 'status':'ambiguous_sender', 'members':[{'current_name':m['current_name'],'aliases':m['aliases']} for m in members],
                            'snapshot':snapshot,'next_offset':None,'coverage':'unknown','cache_hit':False}
                names = [sender] if not members else list(dict.fromkeys([members[0]['current_name'],*members[0]['aliases']]))
                clauses.append('(m.sender_id=? OR m.sender IN ('+','.join('?' for _ in names)+'))')
                params.extend([sender,*names])
            if start:
                clauses.append("m.occurred>=?")
                params.append(normalized_time(start))
            if end:
                clauses.append("m.occurred<?")
                params.append(normalized_time(end))
            # Bounded VM work also caps a pathological short-query scan.
            ticks = [0]
            def bounded():
                ticks[0] += 1
                return int(ticks[0] > 3000)
            db.set_progress_handler(bounded, 10000)
            try:
                rows = db.execute("SELECT m.id FROM messages m WHERE " + " AND ".join(clauses) + " ORDER BY m.occurred DESC,m.seq DESC LIMIT ? OFFSET ?", (*params, limit + 1, offset)).fetchall()
            finally:
                db.set_progress_handler(None, 0)
            result = {"ids": [row[0] for row in rows[:limit]], "snapshot": snapshot,
                      "next_offset": offset + limit if len(rows) > limit or previous_more else None,
                      "coverage": "matching_results_only", "cache_hit": False, "incremental": incremental}
            db.commit()
            # Cache stores pointers only. Any change invalidates its snapshot,
            # including backfills, edits, deletion and manual corrections.
            db.execute("INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,coalesce((SELECT uses+1 FROM receipts WHERE chat=? AND query_key=?),1),?)",
                       (chat, key, snapshot, encode(result), chat, key, datetime.now(timezone.utc).isoformat()))
            db.execute("DELETE FROM receipts WHERE chat=? AND query_key NOT IN (SELECT query_key FROM receipts WHERE chat=? ORDER BY used_at DESC LIMIT 128)", (chat, chat))
            db.commit()
            return result

    def read(self, chat: str, message_id: str, *, before: int = 0, after: int = 0) -> list[dict]:
        with self.connection() as db:
            center = db.execute("SELECT * FROM messages WHERE chat=? AND id=? AND deleted=0", (chat, message_id)).fetchone()
            if not center:
                return []
            left = db.execute("SELECT payload FROM messages WHERE chat=? AND deleted=0 AND (occurred,seq)<(?,?) ORDER BY occurred DESC,seq DESC LIMIT ?", (chat, center["occurred"], center["seq"], min(50, max(0, before)))).fetchall()
            right = db.execute("SELECT payload FROM messages WHERE chat=? AND deleted=0 AND (occurred,seq)>(?,?) ORDER BY occurred,seq LIMIT ?", (chat, center["occurred"], center["seq"], min(50, max(0, after)))).fetchall()
            return [json.loads(row[0]) for row in reversed(left)] + [json.loads(center["payload"])] + [json.loads(row[0]) for row in right]

    def coverage(self, chat: str = "") -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT chat,count(*) AS messages,min(occurred) AS first,max(occurred) AS last,sum(deleted) AS deleted FROM messages" + (" WHERE chat=?" if chat else "") + " GROUP BY chat", (chat,) if chat else ())]

    def record_lookup(self, chat: str, request_id: str, tool: str, details: dict):
        with self.connection() as db:
            db.execute("INSERT INTO audit(chat,request_id,tool,details,created_at) VALUES(?,?,?,?,?)", (chat, request_id, tool, encode(details), datetime.now(timezone.utc).isoformat()))
            db.execute("DELETE FROM audit WHERE seq < (SELECT coalesce(max(seq),0)-10000 FROM audit)")
            db.commit()

    def export_rows(self, chat: str):
        """Stream the effective archive, retaining original sources and status."""
        with self.connection() as db:
            for row in db.execute("SELECT payload FROM messages WHERE chat=? ORDER BY occurred,seq", (chat,)):
                yield json.loads(row[0])

    def snapshot_to(self, destination: Path):
        """Copy exactly committed journal prefixes from one DB read snapshot.

        No writer pause and no independently-timed SQLite copy: restoration
        rebuilds the projection from these authoritative bytes.
        """
        destination.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("BEGIN")
            files = db.execute("SELECT path,offset FROM journal_files ORDER BY path").fetchall()
            for relative, length in files:
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with (self.root / relative).open("rb") as source, target.open("wb") as output:
                    remaining = length
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise RuntimeError("archive journal changed below its committed offset")
                        output.write(chunk)
                        remaining -= len(chunk)
            (destination / "snapshot.json").write_text(encode({"journal_files": len(files), "consistency": "committed_prefixes", "rebuild_index": True}), encoding="utf-8")

    def export_readable(self, chat: str, destination: Path):
        destination.mkdir(parents=True, exist_ok=True)
        manifest, stream, current = [], None, None
        try:
            for index, row in enumerate(self.export_rows(chat)):
                shard = index // 500
                if shard != current:
                    if stream:
                        stream.close()
                    filename = f"{shard:06d}.txt"
                    stream = (destination / filename).open("w", encoding="utf-8")
                    current = shard
                    manifest.append({"path": filename, "first": row["occurred_at"], "last": row["occurred_at"], "count": 0})
                manifest[-1]["last"] = row["occurred_at"]
                manifest[-1]["count"] += 1
                # JSON quoting prevents content from forging record boundaries.
                stream.write(f"message: {row['id']}\ntime: {row['occurred_at']}\nsender: {encode(row['sender'])}\nstatus: {'deleted' if row.get('deleted') else 'active'}\ncontent: {encode(row['content'])}\n\n")
        finally:
            if stream:
                stream.close()
        (destination / "manifest.json").write_text(encode({"chat": chat, "coverage": "imported_sources_only", "shards": manifest}), encoding="utf-8")
