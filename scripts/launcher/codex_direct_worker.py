#!/usr/bin/env python3
"""Private stdio supervisor; native generation and HTTP verification run in WSL."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.services.codex_direct import generate
from app.services.execution_contracts import canonical


def main():
    lock, write_lock = threading.Lock(), threading.Lock()
    cancellations, accepted = {}, set()
    pool = ThreadPoolExecutor(4, thread_name_prefix="codex-direct")

    def send(value):
        with write_lock:
            sys.stdout.write(canonical(value) + "\n")
            sys.stdout.flush()

    def run(message, cancelled):
        p = message["params"]
        def bind(session, turn):
            send({"event": "codex_direct_bind", "params": {"attempt_id": p["attempt_id"], "session_id": session, "turn_id": turn}})
        try:
            result = generate(p["launcher"], p["spec"], cancelled, bind)
            send({"id": message["id"], "result": result})
        except Exception:
            send({"id": message["id"], "error": {"code": "interrupted_unknown", "message": "Codex direct worker failed"}})

    try:
        for line in sys.stdin:
            if len(line.encode()) > 8 * 1024 * 1024:
                break
            message = json.loads(line)
            p = message.get("params") or {}
            attempt = p["attempt_id"]
            with lock:
                cancelled = cancellations.setdefault(attempt, threading.Event())
                if message["method"] == "cancel":
                    cancelled.set()  # Also remembers cancellation before submit.
                    send({"id": message["id"], "result": {"cancel_requested": True}})
                elif message["method"] == "generate" and attempt not in accepted:
                    accepted.add(attempt)
                    pool.submit(run, message, cancelled)
                else:
                    send({"id": message["id"], "error": {"code": "reconciliation_required", "message": "Attempt already accepted or unsupported operation"}})
    finally:
        for cancelled in cancellations.values():
            cancelled.set()
        pool.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    main()
