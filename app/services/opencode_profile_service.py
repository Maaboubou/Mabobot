"""Native OpenCode profile login. Tokens stay entirely in the upstream store."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid

from app.services.execution_contracts import ExecutionError


class OpenCodeProfileService:
    def __init__(self):
        self._lock = threading.RLock()
        self._logins = {}

    @staticmethod
    def root(profile_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", profile_id):
            raise ValueError("Invalid Profile ID")
        return Path(os.getenv("OPENCODE_PROFILE_ROOT", "data/opencode")).absolute() / profile_id

    @staticmethod
    def binary(profile_id=None):
        from app.services.engine_deployment import require_engine
        require_engine("opencode")
        if profile_id:
            from app.services.execution_service import get_execution_service
            options = get_execution_service().config.read()[1]["profiles"].get(profile_id, {}).get("runtime_options")
            if options:
                from app.services.engine_releases import verify_release
                return str(verify_release(options)[0])
        configured = os.getenv("OPENCODE_BIN", "opencode")
        binary = shutil.which(configured)
        if not binary and configured == "opencode":
            from app.services.opencode_installation import status
            native = status()
            binary = native.get("binary") if native.get("installed") and native.get("runtime") == "linux" else None
        if not binary:
            raise ExecutionError("engine_missing", "OpenCode executable is not installed")
        return binary

    def environment(self, profile_id):
        root = self.root(profile_id)
        for directory in (root, *(root / n for n in ("data", "config", "state", "cache", "auth-control"))):
            if any(part.is_symlink() for part in (directory, *directory.parents)):
                raise ExecutionError("unsafe_profile", "Profile directories cannot contain symbolic links")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.stat().st_mode & 0o077:
                raise ExecutionError("unsafe_profile", "OpenCode Profile directories must be private (0700)")
        env = {k: os.environ[k] for k in ("PATH", "LANG", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY") if k in os.environ}
        env.update({f"XDG_{key}_HOME": str(root / key.lower()) for key in ("DATA", "CONFIG", "STATE", "CACHE")})
        env.update(OPENCODE_DISABLE_CHANNEL_DB="true", OPENCODE_DISABLE_PROJECT_CONFIG="true",
                   OPENCODE_DISABLE_CLAUDE_CODE="true", OPENCODE_DISABLE_AUTOUPDATE="true")
        return root, env

    def login(self, profile_id):
        from app.services.execution_service import get_execution_service
        service = get_execution_service()
        _, config = service.config.read()
        profile = config["profiles"].get(profile_id)
        if not profile or profile["engine"] != "opencode" or profile.get("retiring") or profile.get("revoked"):
            raise ExecutionError("profile_unavailable", "OpenCode Profile is not available")
        if profile.get("connection", {}).get("auth_type") == "api_key":
            raise ValueError("API Profile 请编辑 API Key，无需 ChatGPT 登录")
        with service.store.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM engine_config_pins WHERE engine='opencode' AND profile_id=? AND released=0", (profile_id,)).fetchone()[0]
        if count:
            raise ExecutionError("profile_in_use", "Finish or cancel accepted work before changing the account binding")
        with self._lock:
            existing = self._logins.get(profile_id)
            if existing and existing["status"] == "waiting":
                return self.status(profile_id)
            revision, config = service.config.read()
            binding = uuid.uuid4().hex
            config["profiles"][profile_id].update(generation=uuid.uuid4().hex,
                auth_binding_generation=binding, auth_ref="opencode:" + profile_id + ":" + binding)
            service.config.save(config, expected_revision=revision)
            service.registry.retire("opencode", profile_id)
            from app.services.opencode_wsl import use_wsl
            if use_wsl():
                return self._login_wsl(profile_id)
            root, env = self.environment(profile_id)
            process = subprocess.Popen([self.binary(profile_id), "auth", "login", "openai", "--method", "chatgpt-headless"],
                cwd=root / "auth-control", env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", start_new_session=True, close_fds=True)
            record = {"process": process, "status": "waiting", "url": "", "user_code": "", "started": time.time()}
            self._logins[profile_id] = record
            def read():
                from app.services.opencode_auth_output import parse_device_login_line
                try:
                    for line in process.stdout:
                        fields = parse_device_login_line(line)
                        with self._lock:
                            if record["status"] == "waiting":
                                record.update(fields)
                    process.wait()
                    with self._lock:
                        if record["status"] == "waiting":
                            record["status"] = "connected" if process.returncode == 0 else "failed"
                        record["user_code"] = ""
                finally:
                    process.stdout.close()
            threading.Thread(target=read, daemon=True, name="opencode-native-login").start()
        return self.status(profile_id)

    def save_credential(self, profile_id, credential_id, secret):
        from app.services.opencode_wsl import use_wsl, call_worker
        if use_wsl():
            return call_worker('save_credential', {'profile_id': profile_id, 'credential_id': credential_id, 'api_key': secret})
        from app.services.opencode_profile_config import write_credential
        root, _ = self.environment(profile_id)
        write_credential(root, credential_id, secret)
        return {'saved': True}

    def inventory(self, profile_id, model):
        from app.services.opencode_wsl import use_wsl, call_worker
        if use_wsl():
            return call_worker('inventory', {'profile_id': profile_id, 'model': model}, timeout=90)
        from app.services.opencode_profile_config import oauth_inventory
        root, _ = self.environment(profile_id)
        return oauth_inventory(root, self.binary(profile_id), profile_id, model)

    def _login_wsl(self, profile_id):
        from app.services.opencode_process_bridge import ProcessBridge
        from app.services.opencode_wsl import worker_command
        transport = ProcessBridge(worker_command())
        try:
            state = transport.request("login_start", {"profile_id": profile_id}, timeout=40)
        except Exception:
            transport.close()
            raise
        record = {**state, "transport": transport, "process": transport.process}
        self._logins[profile_id] = record
        def poll():
            deadline = time.monotonic() + 600
            try:
                while time.monotonic() < deadline:
                    with self._lock:
                        if record["status"] != "waiting":
                            return
                    value = transport.request("login_status", timeout=10)
                    with self._lock:
                        if record["status"] == "waiting":
                            record.update(value)
                    if value["status"] != "waiting":
                        return
                    time.sleep(1)
            except Exception:
                pass
            finally:
                with self._lock:
                    if record["status"] == "waiting":
                        record["status"] = "failed"
                    record["user_code"] = ""
                transport.close()
        threading.Thread(target=poll, daemon=True, name="opencode-wsl-login").start()
        return self.status(profile_id)

    def status(self, profile_id):
        with self._lock:
            record = self._logins.get(profile_id)
            return {k: record[k] for k in ("status", "url", "user_code", "started")} if record else {"status": "not_checked", "url": "", "user_code": ""}

    def cancel(self, profile_id):
        with self._lock:
            record = self._logins.get(profile_id)
            if record and record["status"] == "waiting":
                record["status"] = "cancelled"
                record["user_code"] = ""
                if record.get("transport"):
                    # Drain the Linux-owned login before another login may
                    # reuse this Profile's account store.
                    record["transport"].close()
                    return self.status(profile_id)
                try:
                    os.killpg(record["process"].pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        return self.status(profile_id)


opencode_profiles = OpenCodeProfileService()
