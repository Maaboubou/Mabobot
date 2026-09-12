"""Single transactional boundary for chat, Assistant, Codex and plugin policy."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Mapping, Optional

from sqlalchemy.orm import Session, selectinload

from app.models.assistant_policy import AssistantChatPolicy
from app.models.chatbot_judge import UserChatBotJudge
from app.models.chatbot_role import UserChatBotRole
from app.models.user_permission import UserPermission, WeChatUser
from app.services.assistant_console_service import AssistantConsoleService, _json_list
from app.services.plugin_chat_config_service import PluginChatConfigError, PluginChatConfigService
from app.services.codex_access_service import (
    ISOLATED_ACCESS,
    OWNER_FULL_ACCESS,
    CodexAccessService,
    normalize_codex_access_mode,
)


RESERVED_PLUGIN_GRANTS = {"assistant", "builtin_chatbot"}


class ChatPolicyError(ValueError):
    pass


class ChatPolicyNotFound(ChatPolicyError):
    pass


class ChatPolicyConflict(ChatPolicyError):
    def __init__(self, current_version: int):
        super().__init__("聊天策略已被其他页面更新，请刷新后重试")
        self.current_version = current_version


def _fields(model: Any) -> Dict[str, Any]:
    if model is None:
        return {}
    fields_set = getattr(model, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(model, "__fields_set__", set())
    return {name: getattr(model, name) for name in fields_set}


def _clean_names(values: Optional[Iterable[Any]]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        name = str(value or "").strip()
        if name and name not in seen:
            result.append(name)
            seen.add(name)
    return result


class ChatPolicyService:
    def __init__(self, plugin_manager: Any, wechat_manager: Any, db: Session):
        self.plugin_manager = plugin_manager
        self.wechat_manager = wechat_manager
        self.db = db

    def _user(self, user_id: int, *, lock: bool = False) -> WeChatUser:
        query = self.db.query(WeChatUser).options(
            selectinload(WeChatUser.permissions),
            selectinload(WeChatUser.assistant_policy),
        )
        if lock:
            query = query.with_for_update()
        user = query.filter(WeChatUser.id == user_id).first()
        if user is None:
            raise ChatPolicyNotFound("聊天不存在")
        return user

    def get(self, user_id: int) -> Dict[str, Any]:
        user = self._user(user_id)
        policy = user.assistant_policy
        role = self.db.query(UserChatBotRole).filter_by(user_id=user.id).first()
        judge = self.db.query(UserChatBotJudge).filter_by(user_id=user.id).first()
        try:
            active = self.wechat_manager.get_listened_chats() if self.wechat_manager else []
        except Exception:
            active = []
        active_names = set(active.keys() if isinstance(active, dict) else active or [])
        codex = CodexAccessService().for_user(user, ensure=False).public()
        grants = [
            {
                "plugin_name": permission.plugin_name,
                "require_mention": bool(permission.require_mention),
            }
            for permission in sorted(user.permissions, key=lambda item: item.plugin_name)
            if permission.plugin_name not in RESERVED_PLUGIN_GRANTS
        ]
        return {
            "user_id": user.id,
            "version": int(user.policy_version or 1),
            "chat": {
                "chat_name": user.chat_name,
                "is_group": bool(user.is_group),
                "listening_enabled": bool(user.listening_enabled),
                "listening_active": user.chat_name in active_names,
                "sender_blacklist": _json_list(user.sender_blacklist),
                "bot_group_nickname": user.bot_group_nickname or "",
                "bot_group_nickname_auto_enabled": bool(user.bot_group_nickname_auto_enabled),
                "bot_group_nickname_detected": user.bot_group_nickname_detected or "",
            },
            "assistant": {
                "configured": policy is not None,
                "enabled": bool(policy and policy.enabled),
                "proactive_enabled": bool(policy and policy.proactive_enabled),
                "followup_enabled": bool(policy and policy.followup_enabled),
                "followup_window_seconds": int(policy.followup_window_seconds or 60) if policy else 60,
                "followup_merge_seconds": int(policy.followup_merge_seconds or 3) if policy else 3,
                "followup_max_turns": int(policy.followup_max_turns or 3) if policy else 3,
                "ignored_senders": _json_list(policy.ignored_senders if policy else None),
                "codex_profile_id": policy.codex_profile_id if policy else None,
                "role_id": role.role_id if role else None,
                "judge_id": judge.judge_id if judge else None,
            },
            "codex": codex,
            "plugin_grants": grants,
            "plugin_configs": PluginChatConfigService(self.db, self.plugin_manager).describe_all(user.id),
        }

    def update(self, user_id: int, request: Any) -> Dict[str, Any]:
        assistant_effect: Mapping[str, Any] = {}
        listening_change: Optional[bool] = None
        codex_changed = False
        chat_type_changed = False
        try:
            current_version = int(request.expected_version)
            # Reserve this version before reading/mutating the aggregate. SQLite
            # does not implement SELECT FOR UPDATE; a conditional write does.
            changed = self.db.query(WeChatUser).filter(
                WeChatUser.id == user_id,
                WeChatUser.policy_version == current_version,
            ).update({WeChatUser.policy_version: current_version + 1}, synchronize_session=False)
            if changed != 1:
                self.db.expire_all()
                current = self._user(user_id)
                raise ChatPolicyConflict(int(current.policy_version or 1))
            self.db.expire_all()
            user = self._user(user_id)

            chat_changes = _fields(request.chat)
            if "is_group" in chat_changes:
                requested_group = bool(chat_changes["is_group"])
                if bool(user.is_group) != requested_group:
                    user.is_group = requested_group
                    chat_type_changed = True
                    # Private and group chats resolve to different Codex scopes,
                    # even while both use the isolated access mode.
                    codex_changed = True
                    if requested_group and normalize_codex_access_mode(user.codex_access_mode) == OWNER_FULL_ACCESS:
                        user.codex_access_mode = ISOLATED_ACCESS
            if "listening_enabled" in chat_changes:
                requested_listening = bool(chat_changes["listening_enabled"])
                if bool(user.listening_enabled) != requested_listening:
                    user.listening_enabled = requested_listening
                    listening_change = requested_listening
            if "sender_blacklist" in chat_changes:
                names = _clean_names(chat_changes["sender_blacklist"])
                user.sender_blacklist = json.dumps(names, ensure_ascii=False) if names else None

            assistant_changes = _fields(request.assistant)
            if chat_type_changed and not user.is_group:
                # A chat converted to private no longer has a proactive/Judge
                # execution path. Clear those bindings in the same transaction.
                assistant_changes["proactive_enabled"] = False
                assistant_changes["judge_id"] = None
            for field in ("bot_group_nickname", "bot_group_nickname_auto_enabled"):
                if field in chat_changes:
                    assistant_changes[field] = chat_changes[field]
            if assistant_changes:
                assistant_effect = AssistantConsoleService(
                    self.plugin_manager,
                    self.wechat_manager,
                    self.db,
                ).update_chat(user.id, assistant_changes, commit=False)

            codex_changes = _fields(request.codex)
            if "mode" in codex_changes and codex_changes["mode"] is not None:
                mode = normalize_codex_access_mode(codex_changes["mode"])
                if user.is_group and mode == OWNER_FULL_ACCESS:
                    raise ChatPolicyError("群聊成员共享隔离空间，不能授予本机最大权限")
                if normalize_codex_access_mode(user.codex_access_mode) != mode:
                    user.codex_access_mode = mode
                    codex_changed = True

            if request.plugin_grants is not None:
                grants: Dict[str, bool] = {}
                for item in request.plugin_grants:
                    name = str(item.plugin_name or "").strip()
                    if name in RESERVED_PLUGIN_GRANTS:
                        raise ChatPolicyError("AI 助手是核心能力，不能作为插件授权")
                    if name in grants:
                        raise ChatPolicyError(f"插件授权重复：{name}")
                    base_name, separator, grant_variant = name.partition("#")
                    if separator and grant_variant != "push":
                        raise ChatPolicyError(f"不支持的插件授权类型：{name}")
                    info = (
                        self.plugin_manager.get_plugin_info(base_name)
                        if self.plugin_manager
                        else None
                    )
                    if info is None or getattr(info, "kind", "plugin") != "plugin":
                        raise ChatPolicyError(f"插件不存在：{name}")
                    grants[name] = bool(item.require_mention)
                for permission in list(user.permissions):
                    self.db.delete(permission)
                self.db.flush()
                for name, require_mention in grants.items():
                    self.db.add(
                        UserPermission(
                            user_id=user.id,
                            plugin_name=name,
                            require_mention=require_mention,
                        )
                    )

            if request.plugin_configs is not None:
                try:
                    PluginChatConfigService(self.db, self.plugin_manager).apply(user.id, request.plugin_configs)
                except PluginChatConfigError as exc:
                    raise ChatPolicyError(str(exc)) from exc
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        side_effect_errors: list[str] = []
        if listening_change is not None and self.wechat_manager:
            try:
                if self.wechat_manager.is_connected():
                    ok = (
                        self.wechat_manager.add_listen_chat(user.chat_name)
                        if listening_change
                        else self.wechat_manager.remove_listen_chat(user.chat_name)
                    )
                    if ok is False:
                        side_effect_errors.append("微信监听状态将在重连后自动同步")
            except Exception:
                side_effect_errors.append("微信监听状态将在重连后自动同步")
        if codex_changed:
            try:
                from app.services.agent_runtime import get_agent_runtime
                from app.services.codex_profile_service import get_codex_runtime_registry

                get_agent_runtime().invalidate_chat(
                    user.chat_name,
                    reason="chat_policy_changed",
                )
                get_codex_runtime_registry().invalidate_chat(
                    user.chat_name,
                    reason="chat_policy_changed",
                )
            except Exception:
                side_effect_errors.append("Codex 会话将在下次请求时刷新")
        if assistant_effect:
            AssistantConsoleService.apply_runtime_side_effects(assistant_effect)

        result = self.get(user_id)
        result["side_effect_warnings"] = side_effect_errors
        return result
