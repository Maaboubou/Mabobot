"""Host bridge to real Codex terminals in the Profile's Linux/WSL runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

from app.services.file_tools_runtime import _as_wsl_path
from app.services.wsl_probe_guard import run_guarded_wsl_command
from app.utils.subprocess_utils import apply_hidden_process_defaults

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TERMINAL_SCRIPT = PROJECT_ROOT / "scripts" / "file_tools" / "manage_terminal.py"
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
_SESSION_RE = re.compile(r"^[a-f0-9]{32}$")


class TerminalError(RuntimeError):
    pass


class TerminalNotFound(TerminalError):
    pass


class TerminalConflict(TerminalError):
    pass


class TerminalService:
    def __init__(self, profile_service=None):
        from app.services.engine_deployment import enabled_engines
        if profile_service is None and "codex" in enabled_engines():
            from app.services.codex_profile_service import get_codex_profile_service
            profile_service = get_codex_profile_service()
        self.profiles = profile_service
        self.use_wsl = bool(profile_service.use_wsl) if profile_service else os.name == "nt"

    def _run(self, action, payload=None, *, distribution=None):
        if os.name == "nt" and not self.use_wsl:
            raise TerminalError("真实终端需要 Linux / WSL，请在支持的引擎运行环境中部署服务")
        script = _as_wsl_path(TERMINAL_SCRIPT) if self.use_wsl else str(TERMINAL_SCRIPT)
        command = (["wsl.exe", "python3", "-X", "utf8", script] if self.use_wsl
                   else [sys.executable, "-X", "utf8", script])
        if self.use_wsl and distribution:
            command[1:1] = ["--distribution", distribution, "--exec"]
            payload = {**(payload or {}), "skip_legacy": True}
        command.extend(["--action", action])
        options = apply_hidden_process_defaults({
            "input": json.dumps(payload or {}, ensure_ascii=False), "capture_output": True,
            "text": True, "encoding": "utf-8", "errors": "replace", "timeout": 30, "check": False,
        })
        try:
            if self.use_wsl:
                result = run_guarded_wsl_command(command, runner=subprocess.run, **options)
            else:
                result = subprocess.run(command, **options)
            response = json.loads(result.stdout.strip().splitlines()[-1])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError, AttributeError):
            raise TerminalError("无法访问终端运行环境，请检查 Linux / WSL 是否可用") from None
        if not isinstance(response, dict):
            raise TerminalError("终端运行环境返回格式不正确")
        if result.returncode or response.get("status") != "success":
            error_type = {"not_found": TerminalNotFound, "conflict": TerminalConflict}.get(response.get("code"), TerminalError)
            raise error_type(str(response.get("error") or "终端操作失败"))
        data = response.get("data")
        if not isinstance(data, dict):
            raise TerminalError("终端运行环境返回内容不完整")
        return data

    def status(self):
        if os.name == "nt" and not self.use_wsl:
            return {"available": False, "runtime": "windows", "project_dir": str(PROJECT_ROOT),
                    "dependencies": [], "message": "真实终端需要 Linux / WSL，请在支持的引擎运行环境中部署服务"}
        try:
            result = self._run("status")
        except TerminalError:
            from app.services.opencode_wsl import selected_distribution as distribution
            if not self.use_wsl or not distribution():
                raise
            result = self._run("status", distribution=distribution())
        result["runtime"] = "wsl" if self.use_wsl else "linux"
        return result

    def list_sessions(self):
        from app.services.opencode_wsl import selected_distribution as distribution
        selected = distribution() if self.use_wsl else None
        if not selected:
            return self._run("list")
        try:
            legacy = self._run("list")["sessions"]
        except TerminalError:
            legacy = []
        native = self._run("list", distribution=selected)["sessions"]
        return {"sessions": list({row["id"]: row for row in [*legacy, *native]}.values())}

    def create(self, profile_id, full_access=True):
        from app.services.engine_deployment import require_engine, require_dispatch
        try:
            require_engine("codex")
            require_dispatch()
        except RuntimeError as exc:
            raise TerminalError(str(exc)) from exc
        if not isinstance(profile_id, str) or not _PROFILE_RE.fullmatch(profile_id):
            raise TerminalError("请选择有效的 Codex Profile")
        if type(full_access) is not bool:
            raise TerminalError("终端权限设置无效")
        try:
            profile = self.profiles.get_profile(profile_id)
            if profile and profile.get("auth_source") == "local_cache" and profile.get("auth_sync_status") in {"missing", "outdated", "invalid"}:
                profile = self.profiles.sync_local_auth_if_needed(profile_id).get("profile") or self.profiles.get_profile(profile_id)
        except Exception:
            raise TerminalError("无法读取所选 Codex Profile，请检查 Profile 配置") from None
        if not profile or not profile.get("available"):
            raise TerminalError("所选 Codex Profile 尚未完成登录或密钥配置")
        return self._run("create", {"profile_id": profile_id, "wrapper_path": profile["wrapper_path"],
                                    "full_access": full_access})

    def list_profiles(self):
        from app.services.engine_deployment import enabled_engines
        from app.services.execution_service import get_execution_service
        profiles = []
        if "codex" in enabled_engines() and self.profiles:
            listing = self.profiles.list_profiles()
            profiles.extend({"name": p.get("name") or p.get("id"), "engine": "codex"}
                            for p in listing.get("profiles", []) if p.get("available"))
        if "opencode" in enabled_engines():
            profiles.extend({"name": key, "engine": "opencode"} for key, p in
                            get_execution_service().config.read()[1]["profiles"].items()
                            if p["engine"] == "opencode" and not p.get("revoked") and not p.get("retiring"))
        return {"profiles": profiles}

    def create_engine(self, engine, profile_id, full_access=False):
        if engine == "codex":
            return self.create(profile_id, full_access)
        if engine != "opencode" or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", profile_id):
            raise TerminalError("引擎或 Profile 无效")
        if full_access:
            raise TerminalError("OpenCode 终端不支持 Codex 的完整访问开关")
        from app.services.engine_deployment import require_dispatch
        from app.services.execution_service import get_execution_service
        from app.services.opencode_profile_service import opencode_profiles
        try:
            require_dispatch()
            profile = get_execution_service().config.read()[1]["profiles"].get(profile_id)
            if not profile or profile["engine"] != engine or profile.get("revoked") or profile.get("retiring"):
                raise TerminalError("OpenCode Profile 不可用")
            from app.services.opencode_wsl import use_wsl, call_worker
            if use_wsl():
                from app.services.opencode_wsl import selected_distribution as distribution
                self.use_wsl = True
                provisioned = call_worker("terminal", {"profile_id": profile_id, "connection": profile.get("connection"),
                    "model": profile["model"], "reasoning_effort": profile.get("reasoning_effort") or "default"})
                return self._run("create", {"engine": engine, "profile_id": profile_id,
                    "wrapper_path": provisioned["wrapper_path"], "full_access": False}, distribution=distribution())
            binary = opencode_profiles.binary(profile_id)
            if profile.get("runtime_options"):
                from app.services.engine_releases import verify_release
                binary, _ = verify_release(profile["runtime_options"])
            root, env = opencode_profiles.environment(profile_id)
            from app.services.opencode_profile_config import terminal_profile
            wrapper = terminal_profile(root, binary, profile_id, env, connection=profile.get("connection"),
                model=profile['model'], effort=profile.get('reasoning_effort') or 'default')
            return self._run("create", {"engine": engine, "profile_id": profile_id,
                             "wrapper_path": str(wrapper), "full_access": False})
        except (RuntimeError, ValueError, OSError) as exc:
            raise TerminalError(str(exc)) from exc

    def stop(self, identifier):
        self._validate_id(identifier)
        selected = self._session_distribution(identifier)
        return self._run("stop", {"id": identifier}, **({"distribution": selected} if selected else {}))

    def stop_all(self):
        from app.services.opencode_wsl import selected_distribution as distribution
        if not self.use_wsl or not distribution():
            return self._run("stop-all")
        for session in self.list_sessions()["sessions"]:
            if session["status"] == "running":
                self.stop(session["id"])
        return {"status": "stopped"}

    def _session_distribution(self, identifier):
        if not self.use_wsl:
            return None
        path = PROJECT_ROOT / "data/terminal" / (identifier + ".json")
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 16000:
            record = json.loads(path.read_text(encoding="utf-8"))
            value = record.get("distribution")
            if value and isinstance(value, str) and len(value) <= 128 and not any(c in value for c in "\r\n\x00"):
                return value
        return None

    @staticmethod
    def _validate_id(identifier):
        if not isinstance(identifier, str) or not _SESSION_RE.fullmatch(identifier):
            raise TerminalNotFound("终端会话不存在")

    def get_connection(self, identifier):
        """Private bridge metadata. Never return this dictionary to the browser."""
        self._validate_id(identifier)
        selected = self._session_distribution(identifier)
        return self._run("connection", {"id": identifier}, **({"distribution": selected} if selected else {}))


_service = None
_service_lock = threading.Lock()


def get_terminal_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = TerminalService()
        return _service
