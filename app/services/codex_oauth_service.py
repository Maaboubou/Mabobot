"""Profile-scoped ChatGPT device-code authentication through Codex app-server."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from app.services.codex_app_server import CodexAppServerError, CodexAppServerManager
from app.services.codex_profile_service import CodexProfileError, get_codex_profile_service


DEVICE_LOGIN_TIMEOUT_SECONDS = 15 * 60


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _search_support(model: dict[str, Any]) -> bool | None:
    flags = [model.get(key) for key in (
        "supportsSearchTool", "supportsWebSearch", "supports_search_tool", "supports_web_search"
    ) if isinstance(model.get(key), bool)]
    return any(flags) if flags else None


def _public_models(result: Dict[str, Any]) -> list[dict[str, Any]]:
    """Expose account model metadata without leaking private app-server fields."""
    values = result.get("data") if isinstance(result.get("data"), list) else []
    models: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict) or item.get("hidden") is True:
            continue
        model_id = str(item.get("model") or item.get("id") or "").strip()
        if not model_id:
            continue
        efforts: list[str] = []
        for value in item.get("supportedReasoningEfforts") or []:
            if not isinstance(value, dict):
                continue
            effort = str(value.get("reasoningEffort") or value.get("effort") or "").strip()
            if effort:
                efforts.append(effort)
        models.append(
            {
                "id": model_id,
                "display_name": str(item.get("displayName") or model_id),
                "description": str(item.get("description") or ""),
                "default_reasoning_effort": str(item.get("defaultReasoningEffort") or ""),
                "supported_reasoning_efforts": list(dict.fromkeys(efforts)),
                "context_window": int(item.get("contextWindow") or 0),
                "input_modalities": [
                    str(value)
                    for value in item.get("inputModalities") or ["text", "image"]
                    if str(value)
                ],
                # Omitted metadata is unknown, not an explicit rejection.
                "supports_web_search": _search_support(item),
                "is_default": bool(item.get("isDefault")),
            }
        )
    return models


def _account_public(result: Dict[str, Any]) -> dict[str, Any]:
    """Return account labels only; access and refresh tokens never cross the API."""
    account = result.get("account") if isinstance(result.get("account"), dict) else {}
    return {
        "connected": account.get("type") == "chatgpt",
        "type": str(account.get("type") or ""),
        "email": str(account.get("email") or ""),
        "plan_type": str(account.get("planType") or ""),
    }


@dataclass
class _OAuthSession:
    profile_name: str
    manager: CodexAppServerManager
    login_id: str
    verification_url: str
    user_code: str
    started_at: str = field(default_factory=_iso_now)
    started_monotonic: float = field(default_factory=time.monotonic)
    status: str = "pending"
    error: str = ""
    account: Dict[str, Any] = field(default_factory=dict)
    models: list[dict[str, Any]] = field(default_factory=list)
    completed_at: str = ""
    completion: threading.Event = field(default_factory=threading.Event)
    completion_payload: Dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        elapsed = max(0, int(time.monotonic() - self.started_monotonic))
        return {
            "profile_name": self.profile_name,
            "login_id": self.login_id,
            "verification_url": self.verification_url if self.status == "pending" else "",
            "user_code": self.user_code if self.status == "pending" else "",
            "status": self.status,
            "error": self.error or None,
            "account": dict(self.account),
            "models": list(self.models),
            "started_at": self.started_at,
            "completed_at": self.completed_at or None,
            "expires_in": (
                max(0, DEVICE_LOGIN_TIMEOUT_SECONDS - elapsed)
                if self.status == "pending"
                else 0
            ),
        }


class CodexOAuthService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, _OAuthSession] = {}

    @staticmethod
    def _profile(name: str) -> dict[str, Any]:
        profile = get_codex_profile_service().get_profile(name)
        if not profile:
            raise CodexProfileError("Codex Profile 不存在")
        if profile.get("auth_type") != "chatgpt":
            raise CodexProfileError("该 Profile 不是 ChatGPT 官方登录模式")
        return profile

    @staticmethod
    def _manager(profile: Dict[str, Any]) -> CodexAppServerManager:
        return CodexAppServerManager(
            codex_bin=str(profile.get("wrapper_path") or ""),
            instance_name=f"oauth-{profile.get('name')}",
            experimental_api=False,
        )

    def _read_connected_account(
        self,
        profile_name: str,
        manager: CodexAppServerManager,
        *,
        refresh_token: bool,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Validate one isolated login and persist only public account metadata."""
        account = _account_public(manager.read_account(refresh_token=refresh_token))
        if not account.get("connected"):
            raise CodexAppServerError("Codex 未识别到可用的 ChatGPT 账号")
        models = _public_models(manager.list_models())
        if not models:
            raise CodexAppServerError("当前 ChatGPT 账号没有返回可用的 Codex 模型")

        profile = self._profile(profile_name)
        selected = str(profile.get("model") or "")
        available_ids = {str(item.get("id") or "") for item in models}
        if selected not in available_ids:
            default = next((item for item in models if item.get("is_default")), models[0])
            selected = str(default["id"])
        update: dict[str, Any] = {
            "account_email": account.get("email") or "",
            "plan_type": account.get("plan_type") or "",
            "model": selected,
        }
        selected_model = next((item for item in models if item.get("id") == selected), None)
        if selected_model:
            effort = str(selected_model.get("default_reasoning_effort") or "")
            supported = selected_model.get("supported_reasoning_efforts") or []
            if effort and effort in supported:
                update["reasoning_effort"] = effort
            if int(selected_model.get("context_window") or 0) >= 4096:
                update["context_window"] = int(selected_model["context_window"])
            update["supports_vision"] = "image" in (
                selected_model.get("input_modalities") or []
            )
            search_support = selected_model.get("supports_web_search")
            if isinstance(search_support, bool):
                update["supports_web_search"] = search_support

        profile_service = get_codex_profile_service()
        profile_service.update_profile(profile_name, update)
        if profile.get("setup_status") != "pending":
            profile_service.ensure_default_profile(profile_name)
        return account, models

    def finalize_setup(
        self,
        profile_name: str,
        payload: Dict[str, Any],
    ) -> dict[str, Any]:
        """Validate account-backed choices and make one wizard draft usable."""
        profile = self._profile(profile_name)
        if profile.get("setup_status") != "pending":
            raise CodexProfileError("该 Profile 不是待完成的创建向导")
        if not profile.get("auth_configured"):
            raise CodexProfileError("请先完成 ChatGPT 官方登录")

        status = self.account_status(profile_name)
        if status.get("status") != "connected":
            raise CodexProfileError("请先完成 ChatGPT 官方登录")
        models = status.get("models") if isinstance(status.get("models"), list) else []
        model_id = str(payload.get("model") or "").strip()
        selected = next((item for item in models if item.get("id") == model_id), None)
        if not selected:
            raise CodexProfileError("所选模型不在当前 ChatGPT 账号的可用目录中")

        supported_efforts = [
            str(value)
            for value in selected.get("supported_reasoning_efforts") or []
            if str(value)
        ]
        reasoning_effort = str(
            payload.get("reasoning_effort")
            or selected.get("default_reasoning_effort")
            or (supported_efforts[0] if supported_efforts else profile.get("reasoning_effort"))
            or "high"
        ).strip()
        if supported_efforts and reasoning_effort not in supported_efforts:
            raise CodexProfileError("所选模型不支持该推理强度")

        context_window = int(selected.get("context_window") or 0)
        if context_window < 4096:
            context_window = int(profile.get("context_window") or 128000)
        update = {
            "model": model_id,
            "reasoning_effort": reasoning_effort,
            "model_verbosity": str(payload.get("model_verbosity") or "inherit"),
            "fast_mode": bool(payload.get("fast_mode", False)),
            "context_window": context_window,
            "supports_vision": "image" in (selected.get("input_modalities") or []),
            "setup_status": "ready",
        }
        profile_service = get_codex_profile_service()
        search_support = selected.get("supports_web_search")
        if isinstance(search_support, bool):
            update["supports_web_search"] = search_support
        ready_profile = profile_service.update_profile(profile_name, update)
        try:
            if bool(payload.get("make_default", True)):
                profile_service.set_default_profile(profile_name)
            else:
                profile_service.ensure_default_profile(profile_name)
        except Exception:
            # Keep the wizard resumable if saving the global default fails.
            profile_service.update_profile(profile_name, {"setup_status": "pending"})
            raise
        ready_profile = profile_service.get_profile(profile_name) or ready_profile
        ready_profile["is_default"] = profile_service.default_profile_id() == profile_name
        return {
            "profile": ready_profile,
            "account": status.get("account") or {},
            "models": models,
            "default_profile_id": profile_service.default_profile_id(),
        }

    def import_local_auth(self, profile_name: str) -> dict[str, Any]:
        """Copy and validate the local file-backed Codex login in one Profile."""
        profile = self._profile(profile_name)
        if profile.get("auth_source") != "local_cache":
            raise CodexProfileError("该 Profile 未选择导入本机 Codex 登录")
        profile_service = get_codex_profile_service()
        profile_service.import_local_auth(profile_name)
        manager = self._manager(self._profile(profile_name))
        try:
            manager.start()
            account, models = self._read_connected_account(
                profile_name,
                manager,
                refresh_token=True,
            )
        except Exception:
            # A failed validation must not leave an unverified credential copy.
            profile_service.clear_profile_auth(profile_name)
            raise
        finally:
            manager.stop()
        return {
            "profile_name": profile_name,
            "status": "connected",
            "account": account,
            "models": models,
            "expires_in": 0,
        }

    def start_login(self, profile_name: str, *, force: bool = False) -> dict[str, Any]:
        profile = self._profile(profile_name)
        with self._lock:
            existing = self._sessions.get(profile_name)
            if existing and existing.status == "pending":
                return existing.public()
        if profile.get("auth_configured") and not force:
            return self.account_status(profile_name)
        if profile.get("auth_source") == "local_cache":
            return self.import_local_auth(profile_name)

        manager = self._manager(profile)
        completion = threading.Event()
        completion_payload: Dict[str, Any] = {}

        def completed(params: Dict[str, Any]) -> None:
            completion_payload.clear()
            completion_payload.update(params)
            completion.set()

        remove_handler = manager.add_notification_handler("account/login/completed", completed)
        try:
            manager.start()
            result = manager.start_chatgpt_device_login()
            login_id = str(result.get("loginId") or "").strip()
            verification_url = str(result.get("verificationUrl") or "").strip()
            user_code = str(result.get("userCode") or "").strip()
            parsed = urlsplit(verification_url)
            if (
                not login_id
                or not user_code
                or parsed.scheme != "https"
                or parsed.hostname != "auth.openai.com"
            ):
                raise CodexAppServerError("Codex 没有返回有效的官方设备授权信息")
        except Exception:
            remove_handler()
            manager.stop()
            raise

        session = _OAuthSession(
            profile_name=profile_name,
            manager=manager,
            login_id=login_id,
            verification_url=verification_url,
            user_code=user_code,
            completion=completion,
            completion_payload=completion_payload,
        )
        with self._lock:
            self._sessions[profile_name] = session
        threading.Thread(
            target=self._finish_login,
            args=(session, remove_handler),
            name=f"codex-oauth-{profile_name}",
            daemon=True,
        ).start()
        return session.public()

    def _finish_login(self, session: _OAuthSession, remove_handler) -> None:
        try:
            if not session.completion.wait(DEVICE_LOGIN_TIMEOUT_SECONDS):
                session.status = "expired"
                session.error = "设备授权已超时，请重新发起登录"
                try:
                    session.manager.cancel_account_login(session.login_id)
                except Exception:
                    pass
                return
            if session.status == "cancelled":
                return
            payload = dict(session.completion_payload)
            if not payload.get("success"):
                session.status = "failed"
                session.error = str(payload.get("error") or "ChatGPT 授权失败")
                return

            session.account, session.models = self._read_connected_account(
                session.profile_name,
                session.manager,
                refresh_token=True,
            )
            session.status = "connected"
        except Exception as exc:
            session.status = "failed"
            session.error = str(exc)
        finally:
            session.completed_at = _iso_now()
            remove_handler()
            session.manager.stop()

    def status(self, profile_name: str) -> dict[str, Any]:
        profile = self._profile(profile_name)
        with self._lock:
            session = self._sessions.get(profile_name)
            if session:
                return session.public()
        return {
            "profile_name": profile_name,
            "status": "connected" if profile.get("auth_configured") else "idle",
            "account": {
                "connected": bool(profile.get("auth_configured")),
                "email": str(profile.get("account_email") or ""),
                "plan_type": str(profile.get("plan_type") or ""),
            },
            "models": [],
            "expires_in": 0,
        }

    def verification_url(self, profile_name: str, login_id: str) -> str:
        self._profile(profile_name)
        with self._lock:
            session = self._sessions.get(profile_name)
            if (
                not session
                or session.status != "pending"
                or session.login_id != str(login_id or "")
            ):
                raise CodexProfileError("设备授权会话不存在或已过期")
            return session.verification_url

    def account_status(self, profile_name: str) -> dict[str, Any]:
        profile = self._profile(profile_name)
        if (
            profile.get("auth_source") == "local_cache"
            and profile.get("auth_sync_status") in {"missing", "outdated", "invalid"}
        ):
            synced = get_codex_profile_service().sync_local_auth_if_needed(profile_name)
            profile = synced.get("profile") or self._profile(profile_name)
        manager = self._manager(profile)
        try:
            manager.start()
            account = _account_public(manager.read_account(refresh_token=False))
            models = _public_models(manager.list_models()) if account.get("connected") else []
            if account.get("connected"):
                get_codex_profile_service().update_profile(
                    profile_name,
                    {
                        "account_email": account.get("email") or "",
                        "plan_type": account.get("plan_type") or "",
                    },
                )
            return {
                "profile_name": profile_name,
                "status": "connected" if account.get("connected") else "idle",
                "account": account,
                "models": models,
                "expires_in": 0,
            }
        finally:
            manager.stop()

    def list_models(self, profile_name: str) -> dict[str, Any]:
        status = self.account_status(profile_name)
        if status.get("status") != "connected":
            raise CodexProfileError("请先完成 ChatGPT 官方登录")
        return {
            "models": status.get("models") or [],
            "account": status.get("account") or {},
        }

    def cancel(self, profile_name: str) -> dict[str, Any]:
        self._profile(profile_name)
        with self._lock:
            session = self._sessions.get(profile_name)
        if not session or session.status != "pending":
            return self.status(profile_name)
        session.status = "cancelled"
        session.completed_at = _iso_now()
        session.completion.set()
        try:
            session.manager.cancel_account_login(session.login_id)
        finally:
            session.manager.stop()
        return session.public()

    def logout(self, profile_name: str) -> dict[str, Any]:
        profile = self._profile(profile_name)
        with self._lock:
            session = self._sessions.get(profile_name)
        if session and session.status == "pending":
            self.cancel(profile_name)
        profile_service = get_codex_profile_service()
        if profile.get("auth_source") == "local_cache":
            # Only remove the Profile-scoped copy. Logging out through Codex
            # could revoke or mutate the user's original local login.
            profile_service.clear_profile_auth(profile_name)
        else:
            manager = self._manager(profile)
            try:
                manager.start()
                manager.logout_account()
            finally:
                manager.stop()
        profile_service.update_profile(
            profile_name,
            {"account_email": "", "plan_type": ""},
        )
        with self._lock:
            self._sessions.pop(profile_name, None)
        return self.status(profile_name)

    def forget(self, profile_name: str) -> None:
        """Drop completed/cancelled in-memory state after its Profile is deleted."""
        with self._lock:
            self._sessions.pop(str(profile_name or "").strip(), None)


_oauth_service: Optional[CodexOAuthService] = None
_oauth_service_lock = threading.Lock()


def get_codex_oauth_service() -> CodexOAuthService:
    global _oauth_service
    if _oauth_service is None:
        with _oauth_service_lock:
            if _oauth_service is None:
                _oauth_service = CodexOAuthService()
    return _oauth_service
