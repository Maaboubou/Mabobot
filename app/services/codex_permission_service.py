"""Single source of truth for intent, inheritance, enforcement and receipts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.codex_permission import CodexChatPermissionOverride, CodexPermissionDefault, CodexPermissionReceipt
from app.models.user_permission import WeChatUser
from app.schemas.codex_permission import ChatPermissionSet, PermissionValues
from app.services.codex_permission_runtime import RUNTIME_INSTANCE, active_workers, runtime_support
from app.services.codex_permission_instructions import PERMISSION_INSTRUCTIONS_VERSION

logger = logging.getLogger(__name__)
FIELDS = tuple(PermissionValues.model_fields)
NEW_DEFAULTS = PermissionValues().model_dump()
LEGACY_DEFAULTS = {**NEW_DEFAULTS, "public_network": False, "boundary_action": "deny"}
HARD_BOUNDARIES = [
    "文件限定当前聊天空间，其他聊天和宿主凭据不可访问",
    "本机、局域网、私有地址和 Unix Socket 不可访问",
    "凭据不传入 shell；运行资源和 Skills 只读",
    "人工审批不会进入微信，残留审批立即拒绝",
]
FIELD_SCHEMA = [
    {"key": "workspace_access", "label": "工作区文件", "help": "控制当前聊天空间的文件读写", "options": [{"value": "read_only", "label": "只读"}, {"value": "read_write", "label": "可读写"}]},
    {"key": "public_network", "label": "公共互联网", "help": "控制 shell 命令的受控公网访问", "options": [{"value": False, "label": "禁止公共互联网"}, {"value": True, "label": "允许公共互联网"}]},
    {"key": "online_research", "label": "在线研究工具", "help": "控制原生搜索、受控浏览器和审核下载", "options": [{"value": False, "label": "禁止"}, {"value": True, "label": "允许"}]},
    {"key": "boundary_action", "label": "越界处理", "help": "将审批请求交给自动审阅，依据宿主权限规则判断是否允许", "options": [{"value": "deny", "label": "直接拒绝越界操作"}, {"value": "auto_review", "label": "自动审阅越界操作"}]},
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(event: str, **fields) -> None:
    # Accept only identifiers and enums, never commands, URLs or raw exceptions.
    allowed = {"chat_id", "policy_version", "default_version", "policy_signature", "permission_profile", "approval_policy", "review_result", "tool_id", "reason_code", "duration_ms"}
    logger.info("codex_permission %s", json.dumps({"event": event, **{k: v for k, v in fields.items() if k in allowed}}, ensure_ascii=False))


class PermissionError(ValueError):
    pass


class PermissionConflict(PermissionError):
    def __init__(self, version):
        self.current_version = version
        super().__init__("权限默认已被其他页面更新，请刷新后重试")


def _decode(row, column: str, *, defaults=False):
    if row.schema_version != 1:
        raise PermissionError("不支持的权限配置版本")
    try:
        data = json.loads(getattr(row, column))
        model = PermissionValues.model_validate(data) if defaults else ChatPermissionSet.model_validate(data)
        if defaults and set(data) != set(FIELDS):
            raise ValueError("incomplete defaults")
        return model.model_dump() if defaults else model.model_dump(exclude_unset=True)
    except (ValueError, TypeError) as exc:
        raise PermissionError("权限配置损坏，已拒绝应用") from exc


@dataclass(frozen=True)
class EffectivePermission:
    scope: str
    settings_mode: str
    workspace_access: str
    public_network: bool
    online_research: bool
    boundary_action: str
    access_scope: str
    permission_profile: str
    approval_policy: str
    web_search_mode: str
    browser_allowed: bool
    reviewed_download_allowed: bool
    default_version: int
    policy_signature: str
    issues: tuple[str, ...] = ()
    schema_version: int = 1
    approvals_reviewer: str = "auto_review"
    private_network_allowed: bool = False
    human_approval_allowed: bool = False

    def public(self):
        return asdict(self)


def resolve_permission(defaults, overrides, *, is_group: bool, default_version=1, support=None) -> EffectivePermission:
    values = PermissionValues.model_validate(defaults).model_dump()
    changes = ChatPermissionSet.model_validate(overrides).model_dump(exclude_unset=True)
    values.update(changes)
    scope = values.get("access_scope", "isolated")
    if is_group and scope == "owner_full":
        raise PermissionError("群聊不能授予本机最大权限")
    support = runtime_support() if support is None else support
    issues = []
    if scope != "owner_full" and not (support.get("permission_profiles") and support.get("runtime_workspace_roots")):
        issues.append("permission_profile_unavailable")
    network = values["public_network"]
    action = values["boundary_action"]
    if network and not support.get("network_proxy") and scope != "owner_full":
        network = False
        issues.append("network_proxy_unavailable")
    if action == "auto_review" and not support.get("auto_review"):
        action = "deny"
        issues.append("auto_review_unavailable")
    research = values["online_research"]
    browser = research and support.get("browser", False) and values["workspace_access"] == "read_write"
    search = "live" if research and support.get("web_search", False) else "disabled"
    if research and (not browser or search == "disabled"):
        issues.append("online_research_partial" if browser or search != "disabled" else "online_research_unavailable")
    profile = ":danger-full-access" if scope == "owner_full" else "mabobot-isolated-{}-{}".format("rw" if values["workspace_access"] == "read_write" else "ro", "public" if network else "offline")
    effective = dict(scope="group" if is_group else "private", settings_mode="custom" if changes else "inherit",
                     workspace_access="read_write" if scope == "owner_full" else values["workspace_access"], public_network=True if scope == "owner_full" else network, online_research=bool(browser or search != "disabled"),
                     boundary_action=action, access_scope=scope, permission_profile=profile,
                     approval_policy="on-request" if action == "auto_review" else "never", web_search_mode=search,
                     browser_allowed=bool(browser), reviewed_download_allowed=bool(browser and values["workspace_access"] == "read_write"), default_version=default_version)
    # The requested values also participate: unsupported changes cannot reuse old threads.
    signature_data = {**effective, "requested": values, "schema_version": 1,
                      "permission_instructions_version": PERMISSION_INSTRUCTIONS_VERSION}
    signature_data.pop("settings_mode")
    signature = "sha256:" + hashlib.sha256(json.dumps(signature_data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return EffectivePermission(**effective, policy_signature=signature, issues=tuple(issues), private_network_allowed=scope == "owner_full")


def migrate_permissions(bind, *, existing_install=True):
    """One transactional, idempotent import. Never read legacy mode at runtime."""
    from app.services.codex_access_service import chat_scope_path
    with Session(bind) as db, db.begin():
        if db.get(CodexPermissionDefault, "group") is not None and db.get(CodexPermissionDefault, "private") is not None:
            return
        legacy = dict(LEGACY_DEFAULTS)
        legacy["online_research"] = str(os.getenv("CODEX_BROWSER_TOOL_ENABLED", "true")).lower() not in {"0", "false", "off"} or str(os.getenv("CODEX_PROXY_WEB_SEARCH", "disabled")) != "disabled"
        for scope in ("group", "private"):
            if db.get(CodexPermissionDefault, scope) is None:
                db.add(CodexPermissionDefault(scope_type=scope, version=1, schema_version=1, config_json=json.dumps(legacy if existing_install else NEW_DEFAULTS), updated_at=now()))
        for user in db.query(WeChatUser):
            if not user.codex_scope_key:
                user.codex_scope_key = chat_scope_path(user.chat_name).name
            if user.codex_access_mode == "owner_full":
                if user.is_group:
                    audit("migration_narrowed", chat_id=user.id, reason_code="group_owner_full")
                elif db.get(CodexChatPermissionOverride, user.id) is None:
                    db.add(CodexChatPermissionOverride(user_id=user.id, schema_version=1, overrides_json=json.dumps({"access_scope": "owner_full", "boundary_action": "auto_review"}), updated_at=now()))


class CodexPermissionService:
    def __init__(self, db: Session, *, support=None):
        self.db = db
        self.explicit_support = support
        self.support = runtime_support(db=db) if support is None else support

    def support_for(self, user):
        if self.explicit_support is not None:
            return self.explicit_support
        from app.services.codex_profile_service import CodexProfileService
        profile_id = (user.assistant_policy.codex_profile_id if user.assistant_policy else None) or CodexProfileService.default_profile_id()
        return runtime_support(profile_id or "", db=self.db)

    def defaults(self, scope_type):
        if scope_type not in {"group", "private"}:
            raise PermissionError("权限作用范围无效")
        row = self.db.get(CodexPermissionDefault, scope_type)
        # Before migration is invoked by the application, retain old permissions.
        return (row, _decode(row, "config_json", defaults=True) if row else dict(LEGACY_DEFAULTS))

    def describe_defaults(self, scope_type):
        row, values = self.defaults(scope_type)
        version = row.version if row else 1
        effective = resolve_permission(values, {}, is_group=scope_type == "group", default_version=version, support=self.support)
        return {"scope_type": scope_type, "schema_version": 1, "version": version, "values": values,
                "effective": effective.public(), "runtime_support": self.support, "apply_status": "unsupported" if effective.issues else "next_turn",
                "updated_at": row.updated_at if row else None, "affected_chats": self.db.query(WeChatUser).filter(WeChatUser.is_group == (scope_type == "group")).count()}

    def update_defaults(self, scope_type, request):
        try:
            row, values = self.defaults(scope_type)
            if row is None:
                raise PermissionError("权限迁移尚未完成，请重启服务")
            values.update(request.set.model_dump(exclude_unset=True))
            PermissionValues.model_validate(values)
            result = self.db.execute(update(CodexPermissionDefault).where(CodexPermissionDefault.scope_type == scope_type, CodexPermissionDefault.version == request.expected_version).values(config_json=json.dumps(values), version=request.expected_version + 1, updated_at=now()).execution_options(synchronize_session=False))
            if result.rowcount != 1:
                self.db.expire_all()
                raise PermissionConflict(self.db.get(CodexPermissionDefault, scope_type).version)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.db.expire_all()
        audit("defaults_updated", default_version=request.expected_version + 1, reason_code=scope_type)
        return self.describe_defaults(scope_type)

    def resolve(self, user):
        row, defaults = self.defaults("group" if user.is_group else "private")
        override = self.db.get(CodexChatPermissionOverride, user.id)
        changes = _decode(override, "overrides_json") if override else {}
        return resolve_permission(defaults, changes, is_group=bool(user.is_group), default_version=row.version if row else 1, support=self.support_for(user))

    def describe(self, user):
        row, defaults = self.defaults("group" if user.is_group else "private")
        override = self.db.get(CodexChatPermissionOverride, user.id)
        changes = _decode(override, "overrides_json") if override else {}
        effective = self.resolve(user)
        receipt = self.db.get(CodexPermissionReceipt, user.id)
        matched = receipt and receipt.runtime_instance in active_workers(self.db) and receipt.policy_signature == effective.policy_signature
        status = "unsupported" if effective.issues else receipt.status if matched else "next_turn"
        return {"settings_mode": "custom" if changes else "inherit", "defaults": defaults, "default_version": row.version if row else 1,
                "values": {**defaults, **changes}, "overrides": changes, "effective": effective.public(),
                "sources": {key: "chat" if key in changes else "default" for key in FIELDS}, "fields": FIELD_SCHEMA,
                "constraints": {"owner_full_allowed": not user.is_group, "boundaries": HARD_BOUNDARIES if effective.access_scope != "owner_full" else ["本机最大权限：可访问本机文件、命令及网络", "人工审批不会进入微信，残留审批立即拒绝"], "scope_display": "本机最大权限" if effective.access_scope == "owner_full" else "当前聊天独立空间"},
                "runtime_support": self.support_for(user), "runtime": {"status": status, "policy_signature": effective.policy_signature,
                "applied_at": receipt.applied_at if matched else None, "reason_codes": list(effective.issues) or ([receipt.reason_code] if matched and receipt.reason_code else [])}}

    def apply_chat(self, user, patch):
        _, defaults = self.defaults("group" if user.is_group else "private")
        row = self.db.get(CodexChatPermissionOverride, user.id)
        changes = _decode(row, "overrides_json") if row else {}
        if user.is_group:
            changes.pop("access_scope", None)
        if patch.reset_all or patch.settings_mode == "inherit":
            changes = {}
        else:
            for key in patch.reset_fields:
                changes.pop(key, None)
            changes.update(patch.set.model_dump(exclude_unset=True))
            if patch.mode is not None:
                changes["access_scope"] = patch.mode
        if user.is_group and changes.get("access_scope") == "owner_full":
            raise PermissionError("群聊不能授予本机最大权限")
        baseline = {**defaults, "access_scope": "isolated"}
        changes = {key: value for key, value in changes.items() if value != baseline[key]}
        ChatPermissionSet.model_validate(changes)
        if not changes:
            if row:
                self.db.delete(row)
        elif row:
            row.overrides_json = json.dumps(changes)
            row.updated_at = now()
        else:
            self.db.add(CodexChatPermissionOverride(user_id=user.id, schema_version=1, overrides_json=json.dumps(changes), updated_at=now()))
        self.db.flush()


def record_receipt(user_id, signature, *, status="effective", reason_code=None, runtime_instance=RUNTIME_INSTANCE):
    from app.models.base import SessionLocal
    if not user_id or not signature:
        return
    try:
        with SessionLocal() as db:
            if db.get(WeChatUser, user_id) is None:
                return
            row = db.get(CodexPermissionReceipt, user_id)
            if row is None:
                row = CodexPermissionReceipt(user_id=user_id)
                db.add(row)
            row.policy_signature, row.status = signature, status
            row.applied_at = now() if status == "effective" else None
            row.reason_code, row.runtime_instance = reason_code, runtime_instance
            db.commit()
        audit("runtime_applied" if status == "effective" else "runtime_failed", chat_id=user_id, policy_signature=signature, reason_code=reason_code)
    except Exception:
        logger.warning("Codex permission receipt could not be persisted")
