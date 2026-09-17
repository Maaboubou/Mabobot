"""Per-chat Codex access policy and durable workspace allocation.

Inbound chat text is not an authorization boundary.  This module resolves the
trusted policy stored by the Web administrator and turns it into runtime-only
Codex settings that user prompts cannot override.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from sqlalchemy.orm import object_session

from app.models.base import SessionLocal
from app.models.user_permission import WeChatUser
from app.services.wechat_file_store import PROJECT_ROOT, safe_path_component


ISOLATED_ACCESS = "isolated"
OWNER_FULL_ACCESS = "owner_full"
SUPPORTED_ACCESS_MODES = {ISOLATED_ACCESS, OWNER_FULL_ACCESS}
ISOLATED_PERMISSION_PROFILE = "mabobot-chat-isolated"
OWNER_PERMISSION_PROFILE = ":danger-full-access"
ACCESS_POLICY_VERSION = "chat-scope-v3-reviewed-downloads"


def normalize_codex_access_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in SUPPORTED_ACCESS_MODES else ISOLATED_ACCESS


def _scope_base() -> Path:
    configured = str(os.getenv("CODEX_CHAT_SCOPE_ROOT") or "").strip()
    root = Path(configured).expanduser() if configured else PROJECT_ROOT / "data" / "codex_chat_scopes"
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root.absolute()


def chat_scope_path(chat_name: str, *, root: Optional[Path] = None) -> Path:
    """Return a stable, collision-resistant and human-readable chat directory."""
    normalized_name = str(chat_name or "").strip()
    digest = hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()[:10]
    readable = safe_path_component(normalized_name, fallback="chat", max_length=72)
    base = Path(root or _scope_base()).absolute()
    # Keep the final component lexical so ensure_directories() can detect a
    # pre-existing link instead of resolving it into a newly trusted scope.
    return base / f"{readable}--{digest}"


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(callable(is_junction) and is_junction())
    except OSError:
        return True


def _ensure_unlinked_directory(path: Path) -> None:
    if _is_link_like(path):
        raise RuntimeError(f"Codex 隔离目录不能是符号链接或联接点：{path}")
    path.mkdir(parents=True, exist_ok=True)
    if _is_link_like(path) or not path.is_dir():
        raise RuntimeError(f"Codex 隔离目录不安全：{path}")


@dataclass(frozen=True)
class CodexAccessContext:
    chat_name: str
    is_group: bool
    mode: str
    scope_root: Path
    workdir: Path
    permission_profile: str
    approval_policy: str
    config_policy: str
    persistent_thread: bool
    policy_version: str = ACCESS_POLICY_VERSION
    user_id: Optional[int] = None
    permissions: Any = None

    @property
    def is_owner(self) -> bool:
        return self.mode == OWNER_FULL_ACCESS

    @property
    def label(self) -> str:
        return "管理员 · 最大权限" if self.is_owner else "隔离空间"

    @property
    def scope_kind(self) -> str:
        if self.is_owner:
            return "local_full"
        return "group_shared" if self.is_group else "private_isolated"

    @property
    def artifact_root(self) -> Path:
        if self.is_owner:
            configured = str(os.getenv("CODEX_PROXY_ARTIFACT_ROOT") or "tmp/images/codex")
            root = Path(configured)
            return root if root.is_absolute() else (PROJECT_ROOT / root).resolve()
        return self.scope_root / "requests"

    @property
    def signature(self) -> str:
        return "|".join(
            (
                self.policy_version,
                self.mode,
                str(self.workdir),
                self.permission_profile,
                self.approval_policy,
                self.config_policy,
                self.permissions.policy_signature if self.permissions else "",
            )
        )

    def ensure_directories(self) -> None:
        if self.is_owner:
            return
        for parent in (self.scope_root, *self.scope_root.parents):
            if _is_link_like(parent):
                raise RuntimeError("Codex 隔离目录不能是符号链接或联接点")
        _ensure_unlinked_directory(self.scope_root)
        _ensure_unlinked_directory(self.scope_root / "workspace")
        _ensure_unlinked_directory(self.scope_root / "requests")

    def apply(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Apply administrator-owned values after untrusted request construction."""
        result = dict(payload)
        result.pop("codex_sandbox", None)
        result.pop("codex_runtime_workspace_roots", None)
        result.update(
            {
                "codex_access_mode": self.mode,
                "codex_source_chat_name": self.chat_name,
                "codex_source_chat_type": "group" if self.is_group else "private",
                "codex_access_signature": self.signature,
                "codex_workdir": str(self.workdir),
                "codex_artifact_root": str(self.artifact_root),
                # Owner access uses the legacy sandbox mode. Isolated chats use
                # the fail-closed permission profile plus a runtime workspace
                # root on every App Server thread/turn.
                "codex_permission_profile": "" if self.is_owner else self.permission_profile,
                "codex_approval_policy": self.approval_policy,
                "codex_config_policy": self.config_policy,
                "codex_persistent_thread": self.persistent_thread,
            }
        )
        # The owner profile is deliberately unrestricted and must not receive a
        # restricted workspace root. Every other chat gets its stable scope as
        # the sole runtime workspace root.
        if self.is_owner:
            result["codex_sandbox"] = "danger-full-access"
        else:
            result["codex_runtime_workspace_roots"] = [str(self.scope_root)]
        if self.permissions is not None:
            from app.services.codex_permission_instructions import permission_instructions
            result.update({
                "codex_chat_user_id": self.user_id,
                "codex_permission_signature": self.permissions.policy_signature,
                "codex_web_search": self.permissions.web_search_mode,
                "codex_online_research": self.permissions.browser_allowed,
                "codex_reviewed_download": self.permissions.reviewed_download_allowed,
                "codex_workspace_access": self.permissions.workspace_access,
                "codex_permissions": self.permissions.public(),
                "codex_permission_instructions": permission_instructions(user_id=self.user_id,
                    permissions=self.permissions, scope_root=self.scope_root, workdir=self.workdir),
            })
        return result

    def public(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "scope_root": str(self.scope_root),
                "workdir": str(self.workdir),
                "artifact_root": str(self.artifact_root),
                "label": self.label,
                "scope_kind": self.scope_kind,
                "group_members_share_scope": bool(self.is_group and not self.is_owner),
                "skills_read_only": not self.is_owner,
                "local_command_network": "unrestricted" if self.is_owner else "disabled",
                "file_downloads": "reviewed_public_http_to_chat_outputs",
                "browser_access": "public_web_ephemeral",
                "browser_private_network": "blocked",
                "browser_local_files": "current_chat_scope_only",
                "persistent_thread": self.persistent_thread,
            }
        )
        payload.pop("policy_version", None)
        payload.pop("permissions", None)
        if self.permissions:
            payload.update(local_command_network="public_proxy" if self.permissions.public_network else "disabled",
                           browser_access="public_web_ephemeral" if self.permissions.browser_allowed else "disabled",
                           file_downloads="reviewed_public_http_to_chat_outputs" if self.permissions.reviewed_download_allowed else "disabled")
        return payload


class CodexAccessService:
    def __init__(self, *, scope_root: Optional[Path] = None) -> None:
        self.scope_root = Path(scope_root or _scope_base()).absolute()

    def for_id(self, user_id: int, *, ensure=True, support=None):
        with SessionLocal() as db:
            user = db.get(WeChatUser, user_id)
            if user is None:
                raise RuntimeError("聊天不存在，已拒绝应用权限")
            return self.for_user(user, ensure=ensure, support=support)

    def for_chat(self, chat_name: str, *, ensure: bool = True, support=None) -> CodexAccessContext:
        normalized_name = str(chat_name or "").strip()
        db = SessionLocal()
        try:
            user = db.query(WeChatUser).filter(WeChatUser.chat_name == normalized_name).first()
            if user is not None:
                return self.for_user(user, ensure=ensure, support=support)
        finally:
            db.close()

        # Names are discovery labels, never authority to reopen a deleted scope.
        raise RuntimeError("聊天尚未纳入权限管理，已拒绝分配 Codex 文件空间")

    def for_user(self, user: WeChatUser, *, ensure: bool = False, support=None) -> CodexAccessContext:
        from app.services.codex_permission_service import CodexPermissionService, LEGACY_DEFAULTS, resolve_permission
        db = object_session(user) if hasattr(user, "_sa_instance_state") else None
        permissions = CodexPermissionService(db, support=support).resolve(user) if db else resolve_permission(LEGACY_DEFAULTS, {}, is_group=bool(user.is_group), support=support)
        mode = permissions.access_scope
        is_group = bool(user.is_group)
        if is_group and mode == OWNER_FULL_ACCESS:
            mode = ISOLATED_ACCESS
        user_id = getattr(user, "id", None)
        scope_key = getattr(user, "codex_scope_key", None) or (f"chat-{user_id}" if user_id else None)
        if scope_key and (Path(scope_key).name != scope_key or scope_key in {".", ".."} or "/" in scope_key or "\\" in scope_key):
            raise RuntimeError("Codex 聊天空间标识无效")
        context = self._build(str(user.chat_name or ""), is_group=is_group, mode=mode, permissions=permissions, user_id=user_id, scope_key=scope_key)
        if ensure:
            context.ensure_directories()
        return context

    def _build(self, chat_name: str, *, is_group: bool, mode: str, permissions=None, user_id=None, scope_key=None) -> CodexAccessContext:
        if mode == OWNER_FULL_ACCESS and not is_group:
            return CodexAccessContext(
                chat_name=chat_name,
                is_group=False,
                mode=OWNER_FULL_ACCESS,
                scope_root=PROJECT_ROOT.resolve(),
                workdir=PROJECT_ROOT.resolve(),
                permission_profile=OWNER_PERMISSION_PROFILE,
                approval_policy=permissions.approval_policy if permissions else "never",
                config_policy="inherit",
                persistent_thread=True,
                permissions=permissions,
                user_id=user_id,
            )

        scope = self.scope_root / scope_key if scope_key else chat_scope_path(chat_name, root=self.scope_root)
        return CodexAccessContext(
            chat_name=chat_name,
            is_group=is_group,
            mode=ISOLATED_ACCESS,
            scope_root=scope,
            workdir=scope,
            permission_profile=permissions.permission_profile if permissions else ISOLATED_PERMISSION_PROFILE,
            approval_policy=permissions.approval_policy if permissions else "never",
            config_policy="isolated",
            # The App Server applies this permission profile to the chat's
            # stable cwd. Group members share the chat scope and logical thread;
            # separate chats retain separate filesystem boundaries.
            persistent_thread=True,
            permissions=permissions,
            user_id=user_id,
        )


codex_access_service = CodexAccessService()
