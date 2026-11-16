"""Windows control plane; native OpenCode and its credentials stay inside WSL."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess

from app.services.execution_contracts import ExecutionError
from app.services.opencode_process_bridge import ProcessBridge

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def use_wsl():
    return os.name == "nt"


def selected_distribution():
    target = PROJECT_ROOT / "data/opencode-wsl-target.json"
    if target.is_file():
        value = json.loads(target.read_text(encoding="utf-8-sig")).get("distribution")
        if not isinstance(value, str) or not value or len(value) > 128 or any(c in value for c in "\r\n\x00"):
            raise ValueError("Invalid OpenCode WSL distribution")
        return value
    return None


def worker_command(distribution=None):
    from app.services.file_tools_runtime import _as_wsl_path
    script = PROJECT_ROOT / "scripts" / "launcher" / "opencode_worker.py"
    project_id = hashlib.sha256(str(PROJECT_ROOT).lower().encode()).hexdigest()[:8]
    if use_wsl():
        if distribution is None:
            distribution = selected_distribution()
        prefix = ["wsl.exe"]
        if distribution:
            if not isinstance(distribution, str) or len(distribution) > 128 or any(c in distribution for c in "\r\n\x00"):
                raise ValueError("Invalid OpenCode WSL distribution")
            prefix += ["--distribution", distribution]
        return [*prefix, "--exec", "python3", "-u", _as_wsl_path(script), "--project-id", project_id]
    import sys
    return [sys.executable, "-u", str(script), "--project-id", project_id]


def call_worker(method, params=None, *, timeout=40):
    bridge = ProcessBridge(worker_command())
    try:
        return bridge.request(method, params, timeout=timeout)
    finally:
        bridge.close()


def pinned_options(options):
    if options or not use_wsl():
        return options
    from app.services.opencode_installation import status
    installed = status()
    if not installed.get("installed"):
        raise ExecutionError("engine_missing", "请先点击安装 OpenCode，完成后再提交任务")
    return {"wsl_binary": installed["binary"], "binary_sha256": installed["sha256"],
            **({"wsl_distribution": installed["distribution"]} if installed.get("distribution") else {})}


class WslOpenCodeRuntime:
    def __init__(self, *, profile_id, generation, models, mcp, options=None, command=None, test_config=None):
        self.profile_id, self.generation, self.models, self.mcp = profile_id, generation, models, mcp
        self.options = options or {}
        self.command = command or worker_command(distribution=self.options.get("wsl_distribution"))
        self.test_config = test_config or {}
        self.transport, self.process = None, None
        self._updates = {}

    def _callback(self, event, params):
        if event == "mcp":
            return self.mcp.dispatch(params["method"], params.get("params", {}))
        if event == "update":
            callback = self._updates.get(params["session"])
            if callback:
                callback(params["value"])

    def start(self):
        if self.process and self.process.poll() is None:
            return
        from app.services.engine_deployment import require_engine
        require_engine("opencode")
        self.transport = ProcessBridge(self.command, callback=self._callback)
        self.process = self.transport.process
        try:
            value = self.transport.request("start", {"profile_id": self.profile_id, "generation": self.generation,
                "models": self.models, "catalog": list(self.mcp.catalog), "options": self.options,
                **self.test_config}, timeout=90)
            self.root = PurePosixPath(value["root"])
            self.plugin_sha256 = value["plugin_sha256"]
            self.capabilities = value["capabilities"]
        except Exception:
            self.stop()
            raise

    def rpc(self, method, params, timeout=35):
        return self.transport.request("rpc", {"method": method, "params": params, "timeout": timeout}, timeout=timeout + 5)

    def bridge(self, method, *, timeout=10, **kwargs):
        return self.transport.request("bridge", {"method": method, "kwargs": kwargs, "timeout": timeout}, timeout=timeout + 5)

    def ensure_ready(self):
        return self.transport.request("ready")

    def notify(self, method, params):
        return self.transport.request("notify", {"method": method, "params": params})

    def updates(self, session, callback):
        if callback is None:
            self._updates.pop(session, None)
        else:
            self._updates[session] = callback
        self.transport.request("updates", {"session": session, "enabled": callback is not None})

    def stop(self):
        if self.transport:
            self.transport.close()
        self.transport, self.process = None, None
