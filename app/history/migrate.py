"""Idempotent, model-free imports from retained Mabobot data and WeChat exports.

Run: python -m app.history.migrate --project .
Additional export: --input path.json --chat CHAT (ciphertalk-extracted-v2)
Normalized JSONL: --input path.jsonl --chat CHAT
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from app.history.store import ArchiveStore, encode


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def import_rows(store, chat, rows, source):
    totals = {"read": 0, "inserted": 0, "linked": 0, "unchanged": 0, "conflicts": 0}
    batch = []
    def flush():
        stats = store.append_many(chat, batch, source=source)
        for key in totals:
            if key != "read":
                totals[key] += stats[key]
        batch.clear()
    for index, row in enumerate(rows, 1):
        row = dict(row)
        row.setdefault("_source_record_id", str(row.get("id") or row.get("source_id") or index))
        batch.append(row)
        totals["read"] += 1
        if len(batch) >= 1000:
            flush()
    if batch:
        flush()
    return {"chat": chat, "source": source, **totals}


def jsonl_rows(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def import_file(store, path, chat):
    digest = file_digest(path)
    if path.suffix.lower() == ".jsonl":
        rows = jsonl_rows(path)
    else:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        if document.get("format") != "ciphertalk-extracted-v2":
            raise ValueError("unsupported export; expected ciphertalk-extracted-v2 or normalized JSONL")
        def normalized():
            for i, row in enumerate(document["messages"], 1):
                if not row.get("platformMessageId"):
                    raise ValueError(f"export message {i} has no platformMessageId")
                yield {"time": row["time"], "sender": row.get("senderName") or row.get("sender") or "系统",
                       "sender_id": row.get("sender") or "", "source_id": str(row["platformMessageId"]),
                       "content": str(row.get("content") or ""),
                       "message_type": row.get("mappedTypeName") or row.get("chatlabTypeName") or "unknown",
                       "metadata": {"original_export": str(path), "original_index": i,
                                    "type": row.get("type"), "sub_type": row.get("subType")}}
        rows = normalized()
    result = import_rows(store, chat, rows, "file:" + digest)
    return {**result, "path": str(path), "sha256": digest}


def migrate_project(project: Path, root: Path | None = None):
    data = project / "data"
    store = ArchiveStore(root or data / "chat_archive")
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "sources": [],
              "coverage_claim": "observed_sources_only; date endpoints do not prove completeness",
              "legacy_data_deleted": False, "llm_calls": 0}
    full_chats = set()
    manifests = set((data / "chat_archive" / "imports").glob("*/manifest.json"))
    manifests.update((data / "memory_experiments").glob("*/manifest.json"))
    for manifest in sorted(manifests):
        document = json.loads(manifest.read_text(encoding="utf-8"))
        path = manifest.parent / "source_messages.jsonl"
        if not path.exists():
            continue
        expected = document.get("sha256") or document.get("artifacts", {}).get("source_messages", {}).get("sha256")
        if expected and file_digest(path) != expected:
            raise ValueError(f"source checksum mismatch: {path}")
        chat = document["chat_name"]
        result = import_file(store, path, chat)
        report["sources"].append(result)
        full_chats.add(chat)
        print(encode(result), flush=True)
    for path in sorted((data / "chat_logs").glob("*.jsonl")):
        result = import_file(store, path, path.stem)
        report["sources"].append(result)
        print(encode(result), flush=True)
    legacy = data / "chat_memory.db"
    if legacy.exists():
        db = sqlite3.connect(legacy.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        try:
            if "memory_sources" in tables:
                for source in db.execute("SELECT * FROM memory_sources"):
                    if source["chat_name"] in full_chats:
                        continue
                    path = Path(source["source_path"])
                    if path.exists() and path.suffix == ".jsonl":
                        report["sources"].append(import_file(store, path, source["chat_name"]))
            if "memory_person_source_messages" in tables:
                groups = db.execute("SELECT DISTINCT chat_name,source_namespace FROM memory_person_source_messages").fetchall()
                for chat, namespace in groups:
                    if namespace == "historical_jsonl" and chat in full_chats:
                        continue
                    def evidence():
                        for row in db.execute("SELECT * FROM memory_person_source_messages WHERE chat_name=? AND source_namespace=? ORDER BY id", (chat, namespace)):
                            item = {"_source_record_id": str(row["id"]), "time": row["message_time"],
                                    "sender": row["sender_name"], "sender_id": row["sender_external_id"],
                                    "content": row["content"], "source_id": row["source_id"],
                                    "evidence_only": True, "source_namespace": namespace,
                                    "legacy_cursor": row["source_cursor"]}
                            if namespace == "live_chat_log_sequence":
                                item["log_sequence"] = row["source_cursor"]
                            yield item
                    result = import_rows(store, chat, evidence(), "legacy_person:" + namespace)
                    report["sources"].append(result)
                    print(encode(result), flush=True)
            if "memory_event_messages" in tables:
                groups = db.execute("SELECT DISTINCT chat_name,source_namespace FROM memory_events").fetchall()
                for chat, namespace in groups:
                    if chat in full_chats and namespace.startswith("history:"):
                        continue
                    def evidence():
                        for row in db.execute("SELECT m.*,e.id AS parent FROM memory_event_messages m JOIN memory_events e ON e.id=m.event_id WHERE e.chat_name=? AND e.source_namespace=? ORDER BY e.id,m.ordinal", (chat, namespace)):
                            item = json.loads(row["message_json"])
                            if not item.get("time"):
                                continue
                            item.update(_source_record_id=f"{row['parent']}:{row['ordinal']}", evidence_only=True,
                                        legacy_event_id=row["parent"], source_namespace=namespace)
                            yield item
                    result = import_rows(store, chat, evidence(), "legacy_event:" + namespace)
                    report["sources"].append(result)
                    print(encode(result), flush=True)
            if "memory_corrections" in tables:
                for correction in db.execute("SELECT * FROM memory_corrections"):
                    payload = dict(correction)
                    store.annotate(correction["chat_name"], f"legacy_correction:{correction['id']}", {"kind": "manual_correction", "legacy": payload})
                    if correction["status"] != "active":
                        continue
                    # Tie correction constraints to retained source evidence,
                    # without turning old generated claims into raw messages.
                    refs = db.execute("SELECT message_json FROM memory_event_messages WHERE event_id=?", (correction["target_event_id"],)).fetchall()
                    for ref in refs:
                        source_row = json.loads(ref[0])
                        if not source_row.get("time"):
                            continue
                        candidates = store.search(correction["chat_name"], query=str(source_row.get("content") or "")[:200],
                                                  sender=str(source_row.get("sender") or ""), limit=100)
                        for identity in candidates["ids"]:
                            item = store.read(correction["chat_name"], identity)[0]
                            if item["content"] == source_row.get("content") and item.get("time") == source_row.get("time"):
                                constraint = {"reason": correction["reason"], "corrected_claim": correction["corrected_claim"], "false_claims": json.loads(correction["false_claims_json"] or "[]")}
                                if item.get("correction") != constraint:
                                    store.revise(correction["chat_name"], identity, correction=constraint)
            if "memory_person_aliases" in tables:
                for alias in db.execute("SELECT * FROM memory_person_aliases WHERE source='manual' AND status='confirmed'"):
                    store.annotate(alias["chat_name"], f"legacy_alias:{alias['id']}", {"kind": "confirmed_alias", "legacy": dict(alias)})
        finally:
            db.close()
    report["coverage"] = store.coverage()
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    destination = store.root / "migration-report.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(encode({"report": str(destination), "coverage": report["coverage"]}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--chat")
    parser.add_argument("--export-readable", type=Path)
    args = parser.parse_args()
    if args.input:
        if not args.chat:
            parser.error("--chat is required for an input export")
        print(encode(import_file(ArchiveStore(args.root or args.project / "data/chat_archive"), args.input, args.chat)))
    elif args.export_readable:
        if not args.chat:
            parser.error("--chat is required for a readable export")
        ArchiveStore(args.root or args.project / "data/chat_archive").export_readable(args.chat, args.export_readable)
    else:
        migrate_project(args.project, args.root)


if __name__ == "__main__":
    main()
