#!/usr/bin/env python3
"""Linux-owned runtime supervisor. Private stdin/stdout is its only host API."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.services.execution_contracts import ExecutionError, canonical
from app.services.opencode_runtime import OpenCodeRuntime, ENGINE_VERSION
from app.services.opencode_mcp import HostMCP

MAX_FRAME = 8 * 1024 * 1024


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def install_root():
    return Path.home() / ".local/share/mabobot/opencode"


def installed(*, check=False):
    path = install_root() / "current.json"
    if not path.is_file():
        return {"status": "missing", "installed": False, "runtime": "wsl"}
    value = json.loads(path.read_text())
    binary = Path(value["binary"])
    if not binary.is_relative_to(install_root() / "releases") or not binary.is_file():
        raise ExecutionError("installation_invalid", "OpenCode 安装文件缺失，请重新安装")
    if check:
        checksum = file_hash(binary)
        version = subprocess.check_output([str(binary), "--version"], text=True, timeout=20).strip()
        if checksum != value["sha256"] or version != "opencode v" + ENGINE_VERSION:
            raise ExecutionError("version_mismatch", "OpenCode 安装校验失败，请重新安装固定版本")
    return {**value, "status": "ready", "installed": True, "runtime": "wsl",
            "distribution": os.getenv("WSL_DISTRO_NAME", "")}


def required_binary():
    value = installed(check=True)
    if not value.get("installed", bool(value.get("binary"))):
        raise ExecutionError("engine_missing", "请先安装 OpenCode，再配置账号或启动任务")
    return value["binary"]


class Worker:
    def __init__(self, project_id):
        self.project_id = project_id
        self.runtime, self.mcp, self.login_process = None, None, None
        self.write_lock, self.lifecycle_lock, self.callback_lock = threading.Lock(), threading.Lock(), threading.Lock()
        self.callbacks, self.closing = {}, threading.Event()
        self.login_state = {"status": "not_checked", "url": "", "user_code": ""}

    def send(self, message):
        data = canonical(message)
        if len(data.encode()) > MAX_FRAME:
            raise ValueError("Frame too large")
        with self.write_lock:
            sys.stdout.write(data + "\n")
            sys.stdout.flush()

    def callback(self, method, params):
        key, target = uuid.uuid4().hex, queue.Queue()
        with self.callback_lock:
            self.callbacks[key] = target
        try:
            self.send({"event": "mcp", "callback_id": key, "params": {"method": method, "params": params}})
            response = target.get(timeout=600)
            if "error" in response:
                raise ValueError("Host disconnected")
            return response["result"]
        finally:
            with self.callback_lock:
                self.callbacks.pop(key, None)

    def profile(self, profile_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", profile_id):
            raise ValueError("Invalid profile")
        # Short, private Unix paths avoid AF_UNIX's path limit and NTFS modes.
        base = Path.home() / ".mbo/o" / self.project_id
        root = base / hashlib.sha256(profile_id.encode()).hexdigest()[:10]
        for directory in (base, root, *(root / n for n in ("data", "config", "state", "cache", "auth-control"))):
            if any(p.is_symlink() for p in (directory, *directory.parents)):
                raise ValueError("Profile contains a symlink")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.stat().st_mode & 0o077:
                raise ValueError("Profile must be private")
        env = {k: os.environ[k] for k in ("PATH", "LANG", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY") if k in os.environ}
        env.update({f"XDG_{n}_HOME": str(root / n.lower()) for n in ("DATA", "CONFIG", "STATE", "CACHE")})
        env.update(OPENCODE_DISABLE_CHANNEL_DB="true", OPENCODE_DISABLE_PROJECT_CONFIG="true",
                   OPENCODE_DISABLE_CLAUDE_CODE="true", OPENCODE_DISABLE_AUTOUPDATE="true")
        return root, env

    def dispatch(self, method, p):
        if method == "status":
            return installed(check=p.get("check", False))
        if method == "save_credential":
            from app.services.opencode_profile_config import write_credential
            root, _ = self.profile(p["profile_id"])
            write_credential(root, p["credential_id"], p["api_key"])
            return {"saved": True}
        if method == "inventory":
            from app.services.opencode_profile_config import oauth_inventory
            root, _ = self.profile(p["profile_id"])
            return oauth_inventory(root, required_binary(), p["profile_id"], p.get("model") or "gpt-5.4")
        if method == "start":
            with self.lifecycle_lock:
                if self.runtime or self.closing.is_set():
                    raise ValueError("Worker already started or closing")
                options = p.get("options") or {}
                adapter_dir = None
                binary = p.get("test_binary")
                if options.get("wsl_binary"):
                    binary = options["wsl_binary"]
                    if file_hash(binary) != options["binary_sha256"]:
                        raise ExecutionError("release_changed", "原任务的 OpenCode 发布文件已变化")
                elif options.get("binary"):
                    from app.services.engine_releases import verify_release
                    binary, adapter_dir = verify_release(options)
                if not binary:
                    binary = required_binary()
                if p.get("test_root"):
                    root = Path(p["test_root"])
                else:
                    root, _ = self.profile(p["profile_id"])
                self.mcp = HostMCP(generation=p["generation"], profile_id=p["profile_id"], catalog=p["catalog"])
                self.mcp.dispatch = self.callback
                self.runtime = OpenCodeRuntime(binary=binary, profile_root=root, profile_id=p["profile_id"],
                    generation=p["generation"], models=p["models"], mcp=self.mcp,
                    providers=p.get("test_providers"), model_catalog=p.get("test_model_catalog"), adapter_dir=adapter_dir,
                    connection=options.get("connection"))
                self.runtime.start()
                return {"root": str(self.runtime.root), "plugin_sha256": self.runtime.plugin_sha256,
                        "capabilities": self.runtime.ensure_ready()}
        if method == "login_start":
            if self.login_process:
                raise ValueError("Login already started")
            root, env = self.profile(p["profile_id"])
            binary = required_binary()
            self.login_process = subprocess.Popen([binary, "auth", "login", "openai", "--method", "chatgpt-headless"],
                cwd=root / "auth-control", env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", start_new_session=True)
            self.login_state.update(status="waiting", started=time.time())
            threading.Thread(target=self._read_login, daemon=True).start()
            return dict(self.login_state)
        if method == "login_status":
            return dict(self.login_state)
        if method == "terminal":
            root, env = self.profile(p["profile_id"])
            binary = required_binary()
            from app.services.opencode_profile_config import terminal_profile
            wrapper = terminal_profile(root, binary, p['profile_id'], env, connection=p.get('connection'),
                model=p.get('model', ''), effort=p.get('reasoning_effort') or 'default')
            return {'wrapper_path': wrapper}
        if method == "workspace":
            from app.services.restricted_executor import RestrictedExecutor
            executor = RestrictedExecutor(p["workspace"], writable=p["writable"],
                public_network=p.get("public_network", False), cancelled=self.closing.is_set,
                timeout=p.get("timeout", 20), max_workspace_bytes=p.get("max_workspace_bytes", 128 * 1024 * 1024))
            return executor.execute(p["argv"])
        if not self.runtime:
            raise ValueError("Runtime not started")
        if method == "rpc":
            native = p["method"]
            if native not in {"session/new", "session/prompt", "session/set_config_option"}:
                raise ValueError("Unsupported native method")
            params = dict(p["params"])
            if native == "session/new":
                params.update(cwd=str(self.runtime.root / "control"), mcpServers=[self.mcp.config])
            return self.runtime.rpc(native, params, timeout=min(3600, p.get("timeout", 35)))
        if method == "bridge":
            return self.runtime.bridge(p["method"], timeout=min(600, p.get("timeout", 10)), **p.get("kwargs", {}))
        if method == "ready":
            return self.runtime.ensure_ready()
        if method == "notify" and p["method"] == "session/cancel":
            self.runtime.notify(p["method"], p["params"])
            return {}
        if method == "updates":
            session = p["session"]
            self.runtime.updates(session, (lambda value: self.send({"event": "update", "params": {"session": session, "value": value}})) if p["enabled"] else None)
            return {}
        raise ValueError("Unsupported worker method")

    def _read_login(self):
        from app.services.opencode_auth_output import parse_device_login_line
        process = self.login_process
        try:
            for line in process.stdout:
                self.login_state.update(parse_device_login_line(line))
            process.wait()
            self.login_state.update(status="connected" if process.returncode == 0 else "failed", user_code="")
        finally:
            process.stdout.close()

    def handle(self, message):
        try:
            result = self.dispatch(message["method"], message.get("params", {}))
            self.send({"id": message["id"], "result": result})
        except Exception as exc:
            self.send({"id": message["id"], "error": {"code": getattr(exc, "code", "native_error"),
                "message": str(exc) if isinstance(exc, ExecutionError) else "OpenCode Linux 操作失败，请检查安装和运行环境"}})

    def close(self):
        self.closing.set()
        with self.callback_lock:
            for target in self.callbacks.values():
                target.put({"error": "Host disconnected"})
        with self.lifecycle_lock:
            if self.runtime:
                self.runtime.stop()
            if self.mcp:
                self.mcp.close()
        if self.login_process and self.login_process.poll() is None:
            os.killpg(self.login_process.pid, signal.SIGTERM)
            try:
                self.login_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.login_process.pid, signal.SIGKILL)
                self.login_process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{8}", args.project_id):
        raise ValueError("Invalid project")
    worker = Worker(args.project_id)
    pool = ThreadPoolExecutor(16, thread_name_prefix="native-control")
    prompts = ThreadPoolExecutor(8, thread_name_prefix="native-prompts")
    try:
        while True:
            line = sys.stdin.readline(MAX_FRAME + 1)
            if not line or len(line.encode()) > MAX_FRAME:
                break
            message = json.loads(line)
            if "callback_id" in message:
                with worker.callback_lock:
                    target = worker.callbacks.get(message["callback_id"])
                if target:
                    target.put(message)
            else:
                long_call = (message.get("method") in {"start", "workspace"} or
                    (message.get("method") == "rpc" and message.get("params", {}).get("method") == "session/prompt"))
                (prompts if long_call else pool).submit(worker.handle, message)
    finally:
        worker.close()
        pool.shutdown(wait=False, cancel_futures=True)
        prompts.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
