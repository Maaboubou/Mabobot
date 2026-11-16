"""Profile-scoped Codex Skill management."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from app.services.file_tools_runtime import _as_wsl_path, _default_use_wsl
from app.services.wsl_probe_guard import (
    WslCircuitOpenError,
    WslTransportError,
    run_guarded_wsl_command,
)
from app.utils.subprocess_utils import apply_hidden_process_defaults


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILL_SCRIPT = PROJECT_ROOT / "scripts" / "file_tools" / "manage_codex_skills.py"


class CodexSkillError(RuntimeError):
    def __init__(self, message: str, *, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


class CodexSkillService:
    """Bridge the web process to a Profile's WSL-local Skill directory."""

    def __init__(self, *, use_wsl: Optional[bool] = None, profile_service: Any = None) -> None:
        self.use_wsl = _default_use_wsl() if use_wsl is None else bool(use_wsl)
        self._profile_service = profile_service

    @property
    def profile_service(self) -> Any:
        if self._profile_service is None:
            from app.services.codex_profile_service import get_codex_profile_service

            self._profile_service = get_codex_profile_service()
        return self._profile_service

    def _command(self, action: str) -> list[str]:
        script = _as_wsl_path(SKILL_SCRIPT) if self.use_wsl else str(SKILL_SCRIPT)
        command = (
            ["wsl.exe", "python3", "-X", "utf8", script]
            if self.use_wsl
            else [sys.executable, "-X", "utf8", script]
        )
        return [*command, "--action", action]

    def _profile_payload(self, profile_id: str) -> dict[str, str]:
        from app.services.engine_deployment import enabled_engines
        if "codex" not in enabled_engines():
            raise CodexSkillError("此部署未启用 Codex", code="unavailable")
        normalized = str(profile_id or "").strip()
        profile = self.profile_service.get_profile(normalized)
        if not profile:
            raise CodexSkillError("Codex Profile 不存在", code="not_found")
        codex_home = str(profile.get("codex_home") or "").strip()
        if not codex_home:
            raise CodexSkillError("Codex Profile 缺少受管 CODEX_HOME", code="unsafe")
        return {"profile_name": normalized, "codex_home": codex_home}

    def _run(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        command = self._command(action)
        timeout = 330 if action == "install-github" else 30
        run_kwargs = {
            "input": json.dumps(payload, ensure_ascii=False),
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout,
            "check": False,
        }
        run_kwargs = apply_hidden_process_defaults(run_kwargs)
        try:
            if self.use_wsl:
                result = run_guarded_wsl_command(command, runner=subprocess.run, **run_kwargs)
            else:
                result = subprocess.run(command, **run_kwargs)
        except (OSError, subprocess.SubprocessError, WslCircuitOpenError, WslTransportError) as exc:
            raise CodexSkillError(f"无法访问 Profile Skill 存储：{exc}", code="unavailable") from exc
        lines = (result.stdout or "").strip().splitlines()
        if not lines:
            detail = (result.stderr or "Skill 管理脚本没有返回数据").strip()
            raise CodexSkillError(detail[-1500:], code="unavailable")
        try:
            response = json.loads(lines[-1])
        except (ValueError, TypeError) as exc:
            raise CodexSkillError("Skill 管理脚本返回了无效数据", code="unavailable") from exc
        if result.returncode != 0 or response.get("status") != "success":
            raise CodexSkillError(
                str(response.get("error") or "Codex Skill 操作失败"),
                code=str(response.get("code") or "invalid"),
            )
        data = response.get("data")
        if not isinstance(data, dict):
            raise CodexSkillError("Skill 管理脚本返回内容不完整", code="unavailable")
        return data

    def _request(self, profile_id: str, action: str, **payload: Any) -> dict[str, Any]:
        return self._run(action, {**self._profile_payload(profile_id), **payload})

    @staticmethod
    def _invalidate(profile_id: str) -> None:
        from app.services.codex_profile_service import get_codex_runtime_registry

        get_codex_runtime_registry().invalidate(profile_id)

    def list_skills(self, profile_id: str) -> dict[str, Any]:
        return self._request(profile_id, "list")

    def get_skill(self, profile_id: str, scope: str, name: str) -> dict[str, Any]:
        return self._request(profile_id, "get", scope=scope, name=name)

    def create_skill(
        self, profile_id: str, *, name: str, description: str, instructions: str
    ) -> dict[str, Any]:
        result = self._request(
            profile_id,
            "create",
            name=name,
            description=description,
            instructions=instructions,
        )
        self._invalidate(profile_id)
        return result

    def install_github_skill(
        self,
        profile_id: str,
        *,
        repository_url: str,
        skill_path: str,
        ref: str,
    ) -> dict[str, Any]:
        result = self._request(
            profile_id,
            "install-github",
            repository_url=repository_url,
            skill_path=skill_path,
            ref=ref,
        )
        self._invalidate(profile_id)
        return result

    def update_skill(self, profile_id: str, name: str, *, content: str) -> dict[str, Any]:
        result = self._request(profile_id, "update", name=name, content=content)
        self._invalidate(profile_id)
        return result

    def set_enabled(self, profile_id: str, name: str, *, enabled: bool) -> dict[str, Any]:
        result = self._request(profile_id, "set-enabled", name=name, enabled=enabled)
        self._invalidate(profile_id)
        return result

    def archive_skill(self, profile_id: str, name: str) -> dict[str, Any]:
        result = self._request(profile_id, "archive", name=name)
        self._invalidate(profile_id)
        return result

    def list_trash(self, profile_id: str) -> dict[str, Any]:
        return self._request(profile_id, "trash")

    def restore_skill(self, profile_id: str, trash_id: str) -> dict[str, Any]:
        result = self._request(profile_id, "restore", trash_id=trash_id)
        self._invalidate(profile_id)
        return result


_skill_service: Optional[CodexSkillService] = None


def get_codex_skill_service() -> CodexSkillService:
    global _skill_service
    if _skill_service is None:
        _skill_service = CodexSkillService()
    return _skill_service
