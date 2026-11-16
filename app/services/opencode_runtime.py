"""Pinned ACP process and same-runtime Unix bridge. No private HTTP discovery."""
from __future__ import annotations

from collections import deque
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import socket
import subprocess
import threading
import time

from app.services.execution_contracts import ExecutionError, canonical

ENGINE_VERSION = "2.3.0+mabobot.p0.1"
ADAPTER_VERSION = "1.0.0"
ADAPTER_DIR = Path(__file__).with_name("opencode")


def _pidfd_open(pid):
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    # Some standalone Python builds omit this binding although Linux/glibc
    # provides it. Keep PID-reuse protection rather than falling back to kill(pid).
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.pidfd_open
    function.argtypes, function.restype = [ctypes.c_int, ctypes.c_uint], ctypes.c_int
    result = function(pid, 0)
    if result < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open failed")
    return result


def _pidfd_signal(fd, sig):
    if hasattr(signal, "pidfd_send_signal"):
        return signal.pidfd_send_signal(fd, sig)
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.pidfd_send_signal
    function.argtypes, function.restype = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint], ctypes.c_int
    if function(fd, sig, None, 0) < 0:
        raise OSError(ctypes.get_errno(), "pidfd_send_signal failed")


class OpenCodeRuntime:
    def __init__(self, *, binary, profile_root, profile_id, generation, models, mcp,
                 providers=None, model_catalog=None, adapter_dir=None, connection=None):
        self.binary = str(Path(shutil.which(binary) or binary).resolve())
        self.profile_root = Path(profile_root).resolve()
        self.profile_id, self.generation = profile_id, str(generation)
        self.models, self.mcp = models, mcp
        self.root = self.profile_root / "runtimes" / hashlib.sha256(str(generation).encode()).hexdigest()[:12]
        self.process, self.server_pidfd, self._owner_lock = None, None, None
        self._write_lock, self._ready_lock, self._responses_lock = threading.Lock(), threading.Lock(), threading.RLock()
        self._responses, self._updates = {}, {}
        self._counter, self._ready_failure_at = 0, 0
        self.stderr = deque(maxlen=100)
        if os.name != "posix" or not Path("/proc/self/status").exists():
            raise ExecutionError("platform_unsupported", "Managed OpenCode requires Linux/WSL with PID fd support")
        if not models:
            raise ValueError("At least one host-authorized model is required")
        from app.services.opencode_profile_config import runtime_connection
        managed_providers, credential_env = runtime_connection(self.profile_root, connection, models)
        if managed_providers:
            providers = managed_providers
        for directory in (self.profile_root, self.root, *(self.root / n for n in ("config", "state", "cache", "control", "bridge", "adapter"))):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink() or directory.stat().st_mode & 0o077:
                raise ExecutionError("unsafe_runtime_directory", "OpenCode runtime directories must be private (0700)")
        if len(os.fsencode(self.root / "bridge" / "bridge.sock")) >= 104:
            raise ExecutionError("runtime_path_too_long", "Choose a shorter private OPENCODE_PROFILE_ROOT")
        adapter_dir = Path(adapter_dir) if adapter_dir else ADAPTER_DIR
        for name in ("plugin.mjs", "package.json"):
            target = self.root / "adapter" / name
            content = (adapter_dir / name).read_bytes()
            if target.exists() and target.read_bytes() != content:
                raise ExecutionError("adapter_changed", "Create a new runtime generation for an adapter upgrade")
            if not target.exists():
                target.write_bytes(content)
        self.plugin_sha256 = hashlib.sha256((self.root / "adapter" / "plugin.mjs").read_bytes()).hexdigest()
        tools = ["mabobot_" + t["name"] for t in mcp.catalog]
        primary = models[0]
        config = {
            "update": "disable", "model": primary["providerID"] + "/" + primary["id"] + "#" + primary["variant"],
            "plugins": [str(self.root / "adapter")],
            "agents": {"direct-runner": {"system": "Mabobot host controlled agent", "mode": "primary"}},
            "permissions": [{"action": "*", "resource": "*", "effect": "deny"},
                            *({"action": name, "resource": "*", "effect": "allow"} for name in tools)],
            "policies": [{"action": "provider.use", "resource": "*", "effect": "deny"},
                         *({"action": "provider.use", "resource": p, "effect": "allow"} for p in sorted({m["providerID"] for m in models})),
                         {"action": "skill.use", "resource": "*", "effect": "deny"},
                         {"action": "mcp.use", "resource": "*", "effect": "deny"},
                         {"action": "mcp.use", "resource": "mabobot", "effect": "allow"}],
        }
        if providers:
            config["providers"] = providers
        else:
            # The pinned OpenAI plugin defaults agent sessions to WebSocket,
            # while transient generation uses HTTP. Keep both production
            # paths on the outbound transport verified by this adapter.
            config["providers"] = {"openai": {"settings": {"transport": "http"}}}
        from app.services.opencode_profile_config import apply_context_window
        apply_context_window(config, connection, models)
        (self.root / "config" / "opencode.json").write_text(canonical(config), encoding="utf-8")
        (self.root / "models.json").write_text(canonical(model_catalog or {}), encoding="utf-8")
        self.env = {k: os.environ[k] for k in ("PATH", "LANG", "SYSTEMROOT", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY") if k in os.environ}
        self.env.update({f"XDG_{name}_HOME": str(self.root / name.lower()) for name in ("CONFIG", "STATE", "CACHE")})
        self.env.update({"XDG_DATA_HOME": str(self.profile_root / "data"),
            "MABOBOT_OPENCODE_RUNTIME": str(self.root), "MABOBOT_OPENCODE_PROFILE": profile_id,
            "MABOBOT_OPENCODE_GENERATION": str(generation), "MABOBOT_OPENCODE_MODELS": canonical(models),
            "MABOBOT_OPENCODE_MODEL": primary["id"] if primary["providerID"] == "openai" else "",
            "MABOBOT_OPENCODE_VARIANT": primary["variant"],
            "OPENCODE_CONFIG_DIR": str(self.root / "config"), "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_DISABLE_MODELS_FETCH": "true", "OPENCODE_MODELS_PATH": str(self.root / "models.json"),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "true", "OPENCODE_DISABLE_CLAUDE_CODE": "true",
            "OPENCODE_DISABLE_FFF": "true", "OPENCODE_DISABLE_CHANNEL_DB": "true"})
        self.env.update(credential_env)
        if not providers and model_catalog is None:
            self.env.pop("OPENCODE_MODELS_PATH")

    def start(self):
        if self.process and self.process.poll() is None:
            return
        import fcntl
        self._owner_lock = (self.root / "owner.lock").open("a")
        try:
            fcntl.flock(self._owner_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._owner_lock.close()
            self._owner_lock = None
            raise ExecutionError("runtime_owned", "Another host owns this runtime generation") from exc
        version = subprocess.check_output([self.binary, "--version"], env=self.env, text=True, timeout=20).strip()
        if version != "opencode v" + ENGINE_VERSION:
            raise ExecutionError("version_mismatch", "OpenCode must use the verified retain-patched build")
        self.process = subprocess.Popen([self.binary, "acp"], cwd=self.root / "control", env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, start_new_session=True, close_fds=True)
        process = self.process
        self.reader = threading.Thread(target=self._read, args=(process,), name="opencode-acp", daemon=True)
        self.reader.start()
        threading.Thread(target=lambda: self.stderr.extend(process.stderr), daemon=True).start()
        self.handshake = self.rpc("initialize", {"protocolVersion": 1, "clientCapabilities": {},
            "clientInfo": {"name": "mabobot", "version": ADAPTER_VERSION}})
        capabilities = self.ensure_ready()
        pid = int(capabilities["pid"])
        parent = next(line.split()[1] for line in Path(f"/proc/{pid}/status").read_text().splitlines() if line.startswith("PPid:"))
        if int(parent) != process.pid:
            raise ExecutionError("runtime_identity", "Bridge does not belong to this ACP runtime")
        self.server_pidfd = _pidfd_open(pid)

    def _read(self, process):
        try:
            for line in process.stdout:
                message = json.loads(line)
                if "id" in message and "method" not in message:
                    with self._responses_lock:
                        target = self._responses.get(message["id"])
                    if target:
                        target.put(message)
                elif message.get("method") == "session/update":
                    params = message.get("params", {})
                    with self._responses_lock:
                        callback = self._updates.get(params.get("sessionId"))
                    if callback:
                        callback(params.get("update", {}))
                elif "id" in message:
                    # Native approvals, filesystem and terminal operations are
                    # never delegated to the host. Only authenticated MCP tools.
                    result = {"outcome": {"outcome": "cancelled"}} if message.get("method") == "session/request_permission" else None
                    self._send({"jsonrpc": "2.0", "id": message["id"], **({"result": result} if result else
                        {"error": {"code": -32601, "message": "Native host operations disabled"}})})
        except (ValueError, OSError):
            pass
        finally:
            with self._responses_lock:
                for target in self._responses.values():
                    target.put({"error": {"message": "ACP connection closed"}})

    def _send(self, message):
        with self._write_lock:
            if not self.process or self.process.poll() is not None:
                raise ExecutionError("interrupted_unknown", "OpenCode process is not connected")
            self.process.stdin.write(canonical(message) + "\n")
            self.process.stdin.flush()

    def rpc(self, method, params, timeout=35):
        with self._responses_lock:
            self._counter += 1
            request_id = self._counter
            target = self._responses[request_id] = queue.Queue()
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            message = target.get(timeout=timeout)
            if "error" in message:
                from app.services.opencode_input import native_error_code, native_error_detail
                raise ExecutionError(native_error_code(message["error"]), native_error_detail(method, message["error"]))
            return message["result"]
        except queue.Empty as exc:
            raise ExecutionError("interrupted_unknown", "OpenCode request confirmation timed out") from exc
        finally:
            with self._responses_lock:
                self._responses.pop(request_id, None)

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def bridge(self, method, *, timeout=10, **kwargs):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(timeout)
            client.connect(str(self.root / "bridge" / "bridge.sock"))
            client.sendall((canonical({"method": method, **kwargs}) + "\n").encode())
            with client.makefile("rb") as stream:
                data = stream.readline(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ExecutionError("output_limit", "Bridge response exceeds the host limit")
            response = json.loads(data)
        if "error" in response:
            raise ExecutionError("bridge_rejected", str(response["error"])[:300])
        return response["result"]

    def ensure_ready(self):
        with self._ready_lock:
            if time.monotonic() - self._ready_failure_at < 5:
                raise ExecutionError("bridge_unavailable", "Bridge initialization is in backoff")
            try:
                capabilities = self.bridge("capabilities")
            except OSError:
                self.rpc("session/new", {"cwd": str(self.root / "control"), "mcpServers": [self.mcp.config]})
                deadline = time.monotonic() + 15
                while True:
                    try:
                        capabilities = self.bridge("capabilities")
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self._ready_failure_at = time.monotonic()
                            raise ExecutionError("bridge_unavailable", "Bridge did not initialize")
                        time.sleep(0.05)
            if any(capabilities.get(k) != v for k, v in {"protocol": 1, "adapter": ADAPTER_VERSION,
                    "engine": ENGINE_VERSION, "profile": self.profile_id, "runtime_id": self.generation}.items()):
                raise ExecutionError("bridge_identity", "Bridge capability handshake mismatch")
            return capabilities

    def updates(self, session, callback):
        with self._responses_lock:
            if callback is None:
                self._updates.pop(session, None)
            else:
                self._updates[session] = callback

    def stop(self):
        if self.server_pidfd is not None:
            try:
                _pidfd_signal(self.server_pidfd, signal.SIGTERM)
            except ProcessLookupError:
                pass
            os.close(self.server_pidfd)
            self.server_pidfd = None
        if self.process:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
            if hasattr(self, "reader"):
                self.reader.join(timeout=2)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
            self.process = None
        if self._owner_lock:
            self._owner_lock.close()
            self._owner_lock = None
