"""Host controller for strict Codex calls; credentials and relay stay with Codex."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time

from app.services.codex_direct import direct_spec
from app.services.execution_contracts import ExecutionError
from app.services.opencode_process_bridge import ProcessBridge


def worker_command():
    script = Path(__file__).resolve().parents[2] / "scripts/launcher/codex_direct_worker.py"
    if os.name == "nt":
        from app.services.file_tools_runtime import _as_wsl_path
        return ["wsl.exe", "--exec", "python3", "-u", _as_wsl_path(script)]
    return [sys.executable, "-u", str(script)]


class CodexDirectRuntime:
    def __init__(self, launcher, *, subscription=False):
        self.launcher, self.subscription = launcher, subscription
        self.bridge = None
        self._handles = {}
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self.bridge is None:
                self.bridge = ProcessBridge(worker_command(), callback=self._event)

    def _event(self, event, value):
        if event == "codex_direct_bind":
            with self._lock:
                handle = self._handles.get(value["attempt_id"])
                if handle and (value["turn_id"] or not handle.turn_id):
                    handle.bind(value["session_id"], value["turn_id"])

    def execute(self, request, handle):
        spec = direct_spec(request)
        spec["subscription"] = self.subscription or bool(json.loads(request.candidate.profile_config_json).get("subscription_model"))
        with self._lock:
            self._handles[request.attempt_id] = handle
        try:
            if handle.cancel_requested.is_set():
                raise ExecutionError("cancelled", "Direct generation was cancelled")
            return self.bridge.request("generate", {"attempt_id": request.attempt_id,
                "launcher": self.launcher, "spec": spec}, timeout=max(1, spec["deadline"] - time.time()) + 45)
        finally:
            with self._lock:
                self._handles.pop(request.attempt_id, None)

    def cancel(self, handle):
        if self.bridge:
            self.bridge.request("cancel", {"attempt_id": handle.attempt_id}, timeout=5)

    def stop(self):
        if self.bridge:
            self.bridge.close()
            self.bridge = None
