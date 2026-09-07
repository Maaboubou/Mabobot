"""Manage non-secret Codex Profile metadata and isolated launchers."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from app.services.file_tools_runtime import _as_wsl_path, _default_use_wsl, get_file_tools_runtime
from app.services.wsl_probe_guard import (
    WslCircuitOpenError,
    WslTransportError,
    run_guarded_wsl_command,
)
from app.utils.subprocess_utils import apply_hidden_process_defaults


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_SCRIPT = PROJECT_ROOT / "scripts" / "file_tools" / "manage_codex_profiles.py"
DEFAULT_ASSISTANT_PROFILE_KEY = "ASSISTANT_CODEX_PROFILE_ID"
LEGACY_CURRENT_CODEX_PROFILE_ID = "__current__"
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")


class CodexProfileError(RuntimeError):
    pass


def _managed_profile_permission_roots(profile: dict[str, Any]) -> tuple[str, ...]:
    """Expose only non-secret skill assets from a validated managed CODEX_HOME."""
    name = str(profile.get("name") or "").strip()
    raw_home = str(profile.get("codex_home") or "").strip()
    if (
        not _PROFILE_NAME_RE.fullmatch(name)
        or not raw_home.startswith("/")
        or "\\" in raw_home
        or "\x00" in raw_home
    ):
        return ()
    codex_home = PurePosixPath(raw_home)
    if ".." in codex_home.parts or codex_home.parts[-3:] != (
        ".codex",
        "mabobot-profiles",
        name,
    ):
        return ()
    return (
        str(codex_home / "skills"),
        str(codex_home / "plugins" / "cache"),
    )


def public_profile(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    allowed = {
        "name",
        "auth_type",
        "auth_source",
        "setup_status",
        "model",
        "provider_name",
        "base_url",
        "reasoning_effort",
        "model_verbosity",
        "context_window",
        "supports_vision",
        "supports_web_search",
        "wire_api",
        "codex_bin",
        "wrapper_path",
        "config_path",
        "codex_home",
        "created_at",
        "account_email",
        "plan_type",
        "key_configured",
        "auth_configured",
        "auth_sync_status",
        "auth_sync_reason",
        "available",
    }
    profile = {key: deepcopy(value[key]) for key in allowed if key in value}
    if profile.get("auth_type") == "chatgpt":
        # Profiles created before auth-source tracking used device-code login.
        if profile.get("auth_source") not in {"device_code", "local_cache"}:
            profile["auth_source"] = "device_code"
    else:
        profile["auth_source"] = "api_key"
    if profile.get("setup_status") not in {"pending", "ready"}:
        profile["setup_status"] = "ready"
    required = ("name", "model", "wrapper_path")
    return profile if all(str(profile.get(key) or "").strip() for key in required) else None


class CodexProfileService:
    """Bridge to a stdlib-only helper that runs in the target Linux home."""

    def __init__(self, *, use_wsl: Optional[bool] = None) -> None:
        self.use_wsl = _default_use_wsl() if use_wsl is None else bool(use_wsl)
        self._cache_lock = threading.RLock()
        self._cache_until = 0.0
        self._cached_listing: Optional[dict[str, Any]] = None
        self._auth_sync_lock = threading.RLock()

    def _command(self, action: str) -> list[str]:
        script = _as_wsl_path(PROFILE_SCRIPT) if self.use_wsl else str(PROFILE_SCRIPT)
        command = (
            ["wsl.exe", "python3", "-X", "utf8", script]
            if self.use_wsl
            else [sys.executable, "-X", "utf8", script]
        )
        return [*command, "--action", action]

    def _run(self, action: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        try:
            command = self._command(action)
            run_kwargs = {
                "input": json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 30,
                "check": False,
            }
            run_kwargs = apply_hidden_process_defaults(run_kwargs)
            if self.use_wsl:
                result = run_guarded_wsl_command(command, runner=subprocess.run, **run_kwargs)
            else:
                result = subprocess.run(command, **run_kwargs)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CodexProfileError(f"无法访问本地 Codex Profile：{exc}") from exc
        lines = (result.stdout or "").strip().splitlines()
        if not lines:
            detail = (result.stderr or "Profile 管理脚本没有返回数据").strip()
            raise CodexProfileError(detail[-1500:])
        try:
            response = json.loads(lines[-1])
        except (ValueError, TypeError) as exc:
            raise CodexProfileError("Profile 管理脚本返回了无效数据") from exc
        if result.returncode != 0 or response.get("status") != "success":
            raise CodexProfileError(str(response.get("error") or "Codex Profile 操作失败"))
        data = response.get("data")
        if not isinstance(data, dict):
            raise CodexProfileError("Profile 管理脚本返回内容不完整")
        return data

    def list_profiles(self) -> dict[str, Any]:
        with self._cache_lock:
            stale = deepcopy(self._cached_listing) if self._cached_listing is not None else None
            cached = (
                deepcopy(self._cached_listing)
                if self._cached_listing is not None and time.monotonic() < self._cache_until
                else None
            )
        stale_reason = ""
        try:
            data = cached or self._run("list")
            stale_reason = str(data.get("_mabobot_stale_reason") or "")
        except CodexProfileError as exc:
            cause: Optional[BaseException] = exc
            wsl_unavailable = False
            while cause is not None:
                if isinstance(
                    cause,
                    (OSError, subprocess.TimeoutExpired, WslCircuitOpenError, WslTransportError),
                ):
                    wsl_unavailable = True
                    break
                cause = cause.__cause__
            if not self.use_wsl or not wsl_unavailable or stale is None:
                raise
            data = stale
            stale_reason = str(exc)
            data["_mabobot_stale_reason"] = stale_reason
        if cached is None:
            with self._cache_lock:
                self._cached_listing = deepcopy(data)
                self._cache_until = time.monotonic() + 3.0
        profiles = [profile for item in data.get("profiles") or [] if (profile := public_profile(item))]
        default_id = self.default_profile_id()
        available_names = [
            str(profile.get("name") or "") for profile in profiles if profile.get("available")
        ]
        if default_id not in available_names:
            migrated_default = available_names[0] if available_names else ""
            if default_id or migrated_default:
                self._write_default_profile(migrated_default)
            default_id = migrated_default
        for profile in profiles:
            profile["is_default"] = profile.get("name") == default_id
        local_auth = data.get("local_auth")
        if not isinstance(local_auth, dict):
            local_auth = {}
        return {
            "codex_home": str(data.get("codex_home") or ""),
            "local_auth": {
                "available": bool(local_auth.get("available")),
                "storage": str(local_auth.get("storage") or "unavailable"),
                "reason": str(local_auth.get("reason") or "本机登录不可导入"),
            },
            "default_profile_id": default_id,
            "profiles": profiles,
            "stale": bool(stale_reason),
            "stale_reason": stale_reason,
        }

    def create_profile(self, payload: dict[str, Any], *, codex_bin: str) -> dict[str, Any]:
        configured_bin = str(codex_bin or "").strip()
        runtime = get_file_tools_runtime(
            use_wsl=self.use_wsl,
            codex_bin=configured_bin,
        )
        codex = runtime.get("codex") if isinstance(runtime.get("codex"), dict) else {}
        resolved_bin = str(codex.get("path") or "").strip()
        if not codex.get("available") or not resolved_bin:
            detail = str(runtime.get("error") or "").strip()
            message = "无法解析当前 Linux/WSL 环境中的 Codex 可执行文件"
            if detail:
                message = f"{message}：{detail}"
            raise CodexProfileError(message)

        request = dict(payload)
        request["codex_bin"] = resolved_bin
        profile = public_profile(self._run("create", request))
        if not profile:
            raise CodexProfileError("新建 Profile 返回内容不完整")
        self.invalidate_cache()
        get_file_tools_runtime(use_wsl=self.use_wsl, force=True)
        # A ChatGPT profile becomes eligible only after its copied or newly
        # authorized account has been verified through its isolated runtime.
        if (
            profile.get("auth_type") != "chatgpt"
            and (bool(payload.get("make_default", True)) or not self.default_profile_id())
        ):
            self.set_default_profile(str(profile["name"]))
        return profile

    def import_local_auth(self, name: str) -> dict[str, Any]:
        return self.sync_local_auth_if_needed(name, force=True)

    def sync_local_auth_if_needed(self, name: str, *, force: bool = False) -> dict[str, Any]:
        """Refresh one file-backed login and retire any process holding its old token."""
        normalized = str(name or "").strip()
        with self._auth_sync_lock:
            profile = self.get_profile(normalized)
            if not profile:
                raise CodexProfileError("Codex Profile 不存在")
            if profile.get("auth_type") != "chatgpt" or profile.get("auth_source") != "local_cache":
                raise CodexProfileError("该 Profile 未选择导入本机 Codex 登录")
            if not force and profile.get("auth_sync_status") == "synced":
                return {
                    "name": normalized,
                    "imported": False,
                    "changed": False,
                    "auth_configured": bool(profile.get("auth_configured")),
                    "auth_sync_status": "synced",
                    "profile": profile,
                }
            result = self._run("import-local-auth", {"name": normalized})
            self.invalidate_cache()
            refreshed = self.get_profile(normalized)
        get_codex_runtime_registry().invalidate(normalized)
        return {
            **result,
            "profile": refreshed,
        }

    def clear_profile_auth(self, name: str) -> dict[str, Any]:
        result = self._run("clear-auth", {"name": str(name or "").strip()})
        self.invalidate_cache()
        get_codex_runtime_registry().invalidate(name)
        return result

    def update_profile(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        profile = public_profile(self._run("update", {**dict(payload), "name": name}))
        if not profile:
            raise CodexProfileError("更新 Profile 返回内容不完整")
        self.invalidate_cache()
        get_codex_runtime_registry().invalidate(str(profile["name"]))
        return profile

    def delete_profile(self, name: str) -> dict[str, Any]:
        """Delete a Profile and reset every first-party binding before removal."""
        normalized = str(name or "").strip()
        profile = self.get_profile(normalized)
        if not profile:
            raise CodexProfileError("Codex Profile 不存在")

        default_cleared = self.default_profile_id() == normalized
        replacement_default = ""
        if default_cleared:
            replacement_default = next(
                (
                    str(item.get("name") or "")
                    for item in self.list_profiles().get("profiles") or []
                    if item.get("name") != normalized and item.get("available")
                ),
                "",
            )
        chat_bindings_cleared = self._clear_chat_profile_bindings(normalized)
        model_bindings_cleared = self._clear_model_profile_bindings(normalized)

        get_codex_runtime_registry().invalidate(normalized)
        result = self._run("delete", {"name": normalized})
        self.invalidate_cache()
        if default_cleared:
            self._write_default_profile(replacement_default)
        return {
            "name": normalized,
            "deleted": bool(result.get("deleted", True)),
            "default_cleared": default_cleared,
            "replacement_default_profile_id": replacement_default,
            "chat_bindings_cleared": chat_bindings_cleared,
            "model_bindings_cleared": model_bindings_cleared,
        }

    @staticmethod
    def _clear_chat_profile_bindings(profile_id: str) -> int:
        from app.models.assistant_policy import AssistantChatPolicy
        from app.models.base import SessionLocal
        from app.models.user_permission import WeChatUser

        with SessionLocal() as db:
            try:
                policies = (
                    db.query(AssistantChatPolicy)
                    .filter(AssistantChatPolicy.codex_profile_id == profile_id)
                    .all()
                )
                if not policies:
                    return 0
                user_ids = [int(policy.user_id) for policy in policies]
                for policy in policies:
                    policy.codex_profile_id = None
                    policy.version = int(policy.version or 0) + 1
                for user in db.query(WeChatUser).filter(WeChatUser.id.in_(user_ids)).all():
                    user.policy_version = int(user.policy_version or 1) + 1
                db.commit()
                return len(policies)
            except Exception as exc:
                db.rollback()
                raise CodexProfileError(f"重置聊天中的 Profile 引用失败：{exc}") from exc

    @staticmethod
    def _clear_model_profile_bindings(profile_id: str) -> int:
        try:
            from app.services.llm_manager import get_llm_manager

            return int(get_llm_manager().unbind_codex_profile(profile_id))
        except Exception as exc:
            raise CodexProfileError(f"重置模型中的 Profile 引用失败：{exc}") from exc

    def get_profile(self, name: str) -> Optional[dict[str, Any]]:
        normalized = str(name or "").strip()
        if not normalized:
            return None
        return next(
            (item for item in self.list_profiles().get("profiles") or [] if item.get("name") == normalized),
            None,
        )

    @staticmethod
    def default_profile_id() -> str:
        try:
            from app.services.config_service import get_setting, update_setting

            value = str(get_setting(DEFAULT_ASSISTANT_PROFILE_KEY, "") or "").strip()
            if value == LEGACY_CURRENT_CODEX_PROFILE_ID:
                update_setting(DEFAULT_ASSISTANT_PROFILE_KEY, "")
                return ""
            return value
        except Exception:
            return ""

    @classmethod
    def resolve_assistant_profile_id(cls, configured_profile_id: Any) -> str:
        """Resolve an inherited or explicit chat choice to a managed Profile."""
        normalized = str(configured_profile_id or "").strip()
        if normalized == LEGACY_CURRENT_CODEX_PROFILE_ID:
            normalized = ""
        resolved = normalized or cls.default_profile_id()
        if not resolved:
            raise CodexProfileError("尚未配置默认 Codex Profile，请先新建并完成模型认证配置")
        return resolved

    def set_default_profile(self, name: str) -> None:
        normalized = str(name or "").strip()
        if not normalized:
            raise CodexProfileError("默认 Codex Profile 不能为空")
        profile = self.get_profile(normalized)
        if profile is None:
            raise CodexProfileError("Codex Profile 不存在")
        if not profile.get("available"):
            raise CodexProfileError("Codex Profile 尚未完成登录或密钥配置")
        self._write_default_profile(normalized)

    @staticmethod
    def _write_default_profile(name: str) -> None:
        from app.services.config_service import update_setting

        if not update_setting(DEFAULT_ASSISTANT_PROFILE_KEY, str(name or "").strip()):
            raise CodexProfileError("保存默认 Codex Profile 失败")

    def ensure_default_profile(self, name: str) -> None:
        if not self.default_profile_id():
            self.set_default_profile(name)

    def invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cached_listing = None
            self._cache_until = 0.0


class CodexProfileRuntimeRegistry:
    """Keep isolated Codex process pools per Profile; never switch global state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runtimes: dict[str, tuple[str, Any]] = {}

    def resolve(self, profile_id: str = "") -> tuple[Any, Optional[dict[str, Any]]]:
        from app.services.agent_runtime import CodexAgentRuntime

        service = get_codex_profile_service()
        normalized = service.resolve_assistant_profile_id(profile_id)
        profile = service.get_profile(normalized)
        if not profile:
            raise CodexProfileError(f"Codex Profile 不存在：{normalized}")
        if (
            profile.get("auth_source") == "local_cache"
            and profile.get("auth_sync_status") in {"missing", "outdated", "invalid"}
        ):
            synced = service.sync_local_auth_if_needed(normalized)
            profile = synced.get("profile") or service.get_profile(normalized)
            if not profile:
                raise CodexProfileError(f"Codex Profile 不存在：{normalized}")
        if not profile.get("available"):
            raise CodexProfileError(f"Codex Profile 尚未完成登录或密钥配置：{normalized}")
        signature = "|".join(
            str(profile.get(key) or "")
            for key in ("wrapper_path", "model", "reasoning_effort", "context_window", "created_at")
        )
        with self._lock:
            cached = self._runtimes.get(normalized)
            if cached and cached[0] == signature:
                return cached[1], profile
            previous = cached[1] if cached else None
            runtime = CodexAgentRuntime(
                codex_bin=str(profile["wrapper_path"]),
                permission_read_roots=_managed_profile_permission_roots(profile),
            )
            try:
                runtime.start()
            except Exception:
                runtime.stop()
                raise
            self._runtimes[normalized] = (signature, runtime)
        if previous is not None:
            previous.stop()
        return runtime, profile

    def invalidate(self, profile_id: str) -> None:
        with self._lock:
            cached = self._runtimes.pop(str(profile_id or "").strip(), None)
        if cached:
            cached[1].stop()

    def invalidate_chat(self, chat_id: str, *, reason: str = "manual_reset") -> None:
        with self._lock:
            runtimes = [item[1] for item in self._runtimes.values()]
        for runtime in runtimes:
            runtime.invalidate_chat(chat_id, reason=reason)

    def stop_all(self) -> None:
        with self._lock:
            values = list(self._runtimes.values())
            self._runtimes.clear()
        for _signature, runtime in values:
            runtime.stop()


_profile_service: Optional[CodexProfileService] = None
_runtime_registry: Optional[CodexProfileRuntimeRegistry] = None


def get_codex_profile_service() -> CodexProfileService:
    global _profile_service
    if _profile_service is None:
        _profile_service = CodexProfileService()
    return _profile_service


def get_codex_runtime_registry() -> CodexProfileRuntimeRegistry:
    global _runtime_registry
    if _runtime_registry is None:
        _runtime_registry = CodexProfileRuntimeRegistry()
    return _runtime_registry
