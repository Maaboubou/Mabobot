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
        if profile_service is None:
            from app.services.codex_profile_service import get_codex_profile_service
            profile_service = get_codex_profile_service()
        self.profiles = profile_service
        self.use_wsl = bool(profile_service.use_wsl)

    def _run(self, action, payload=None):
        if os.name == "nt" and not self.use_wsl:
            raise TerminalError("真实终端需要 Linux / WSL，请启用当前项目的 WSL Codex 运行环境")
        script = _as_wsl_path(TERMINAL_SCRIPT) if self.use_wsl else str(TERMINAL_SCRIPT)
        command = (["wsl.exe", "python3", "-X", "utf8", script] if self.use_wsl
                   else [sys.executable, "-X", "utf8", script])
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
                    "dependencies": [], "message": "真实终端需要 Linux / WSL，请启用项目的 WSL Codex 运行环境"}
        result = self._run("status")
        result["runtime"] = "wsl" if self.use_wsl else "linux"
        return result

    def list_sessions(self):
        return self._run("list")

    def create(self, profile_id, full_access=True):
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

    def stop(self, identifier):
        self._validate_id(identifier)
        return self._run("stop", {"id": identifier})

    def stop_all(self):
        return self._run("stop-all")

    @staticmethod
    def _validate_id(identifier):
        if not isinstance(identifier, str) or not _SESSION_RE.fullmatch(identifier):
            raise TerminalNotFound("终端会话不存在")

    def get_connection(self, identifier):
        """Private bridge metadata. Never return this dictionary to the browser."""
        self._validate_id(identifier)
        return self._run("connection", {"id": identifier})


_service = None
_service_lock = threading.Lock()


def get_terminal_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = TerminalService()
        return _service
