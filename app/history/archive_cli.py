"""Standalone local archive helper; Python standard library only.

Copy lives beside archive.sqlite3. Arbitrary analysis can also use sqlite3
directly; this helper is a convenience, not the only allowed interface.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(__file__).with_name("archive.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("overview")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--sender", default="")
    search.add_argument("--start", default="", help="Inclusive UTC ISO time")
    search.add_argument("--end", default="", help="Exclusive UTC ISO time")
    search.add_argument("--limit", type=int, default=30)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--chars", type=int, default=300, help="Excerpt characters; 0 for full text")
    sql = commands.add_parser("sql")
    sql.add_argument("query", help="Read-only SQL; all tables are scoped to this chat")
    sql.add_argument("--limit", type=int, default=100, help="Printed rows; 0 prints all")
    export = commands.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        if args.command == "overview":
            manifest = {row[0]: json.loads(row[1]) for row in db.execute("SELECT * FROM manifest")}
            print(encode(manifest))
            print(encode({"months": [dict(row) for row in db.execute(
                "SELECT substr(occurred_at,1,7) AS utc_month,count(*) AS messages FROM messages GROUP BY utc_month ORDER BY utc_month")]}))
        elif args.command == "search":
            clauses, params = ["(instr(lower(content),lower(?))>0 OR instr(lower(image_description),lower(?))>0)"], [args.query, args.query]
            if args.sender:
                members = db.execute("SELECT member_id FROM member_names WHERE name=?", (args.sender,)).fetchall()
                if len(members) > 1:
                    raise SystemExit("Ambiguous sender alias; inspect members.json and select a current nickname.")
                names = [row[0] for row in db.execute("SELECT name FROM member_names WHERE member_id=?", (members[0][0],))] if members else [args.sender]
                clauses.append("sender IN (" + ",".join("?" for _ in names) + ")")
                params.extend(names)
            for key, operator in (("start", ">="), ("end", "<")):
                value = getattr(args, key)
                if value:
                    clauses.append("occurred_at" + operator + "?")
                    params.append(value)
            query = "SELECT id,time,sender,is_bot,content,image_description,correction FROM messages WHERE " + " AND ".join(clauses) + " ORDER BY occurred_at DESC,id DESC LIMIT ? OFFSET ?"
            for row in db.execute(query, [*params, max(1, args.limit), max(0, args.offset)]):
                item = dict(row)
                if args.chars > 0:
                    hit = item["content"].lower().find(args.query.lower())
                    offset = max(0, hit - 80)
                    item["content_length"] = len(item["content"])
                    item["content"] = item["content"][offset:offset + args.chars]
                    item["offset"] = offset
                    item["image_description"] = item["image_description"][:args.chars]
                print(encode(item))
        elif args.command == "sql":
            cursor = db.execute(args.query)
            rows = cursor.fetchall() if args.limit == 0 else cursor.fetchmany(max(1, args.limit) + 1)
            more = args.limit > 0 and len(rows) > args.limit
            for row in rows[:args.limit] if more else rows:
                print(encode(dict(row)))
            if more:
                print(encode({"more": True, "message": "Increase --limit, aggregate in SQL, or write results to a file."}))
        else:
            count = 0
            # Refuse accidental replacement of the input database or an existing
            # analysis artifact; the caller can choose a new output path.
            with args.output.open("x", encoding="utf-8", newline="\n") as output:
                for row in db.execute("SELECT * FROM messages ORDER BY occurred_at,id"):
                    item = dict(row)
                    item["metadata"] = json.loads(item["metadata"])
                    item["correction"] = json.loads(item["correction"])
                    output.write(encode(item) + "\n")
                    count += 1
            print(encode({"messages": count, "output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
