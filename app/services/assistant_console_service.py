"""Aggregated core Assistant console data and chat-level policy mutations."""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy.orm import Session, selectinload

from app.models.chatbot_judge import ChatBotJudge, UserChatBotJudge
from app.models.chatbot_role import ChatBotRole, UserChatBotRole
from app.models.assistant_policy import AssistantChatPolicy
from app.models.user_permission import WeChatUser
from app.services.capability_service import CapabilityService
from app.services.config_service import get_setting
from app.utils.bot_mentions import bot_names_for_user
from app.utils.plugin_config import get_plugin_setting


class AssistantConsoleError(ValueError):
    pass


def _json_object(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        parsed = value
    else:
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            parsed = str(value).splitlines()
    if not isinstance(parsed, list):
        return []
    return list(dict.fromkeys(str(item or "").strip() for item in parsed if str(item or "").strip()))


class AssistantConsoleService:
    def __init__(self, plugin_manager: Any, wechat_manager: Any, db: Session):
        self.plugin_manager = plugin_manager
        self.wechat_manager = wechat_manager
        self.db = db

    def _active_chat_names(self) -> set[str]:
        if not self.wechat_manager:
            return set()
        try:
            active = self.wechat_manager.get_listened_chats()
        except Exception:
            return set()
        if isinstance(active, dict):
            return set(active)
        if isinstance(active, list):
            return set(active)
        return set()

    def _roles(self) -> tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
        bindings = self.db.query(UserChatBotRole).all()
        counts: Dict[int, int] = {}
        for binding in bindings:
            counts[binding.role_id] = counts.get(binding.role_id, 0) + 1
        items = []
        by_id = {}
        for role in self.db.query(ChatBotRole).order_by(ChatBotRole.id).all():
            item = {
                "id": role.id,
                "name": role.name,
                "display_name": role.display_name,
                "description": role.description or "",
                "prompt": role.prompt,
                "output_split_enabled": bool(role.output_split_enabled),
                "output_max_chars": role.output_max_chars,
                "output_max_count": role.output_max_count,
                "output_strip_trailing_period": bool(role.output_strip_trailing_period),
                "output_interval_seconds": role.output_interval_seconds,
                "user_count": counts.get(role.id, 0),
            }
            items.append(item)
            by_id[role.id] = item
        return items, by_id

    def _judges(self) -> tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
        bindings = self.db.query(UserChatBotJudge).all()
        counts: Dict[int, int] = {}
        for binding in bindings:
            counts[binding.judge_id] = counts.get(binding.judge_id, 0) + 1
        items = []
        by_id = {}
        for judge in self.db.query(ChatBotJudge).order_by(ChatBotJudge.id).all():
            item = {
                "id": judge.id,
                "name": judge.name,
                "display_name": judge.display_name,
                "description": judge.description or "",
                "prompt": judge.prompt,
                "prompt_mode": judge.prompt_mode or "simple",
                "trigger_msg_threshold": judge.trigger_msg_threshold,
                "trigger_interval_minutes": judge.trigger_interval_minutes,
                "cooldown_msg_threshold": judge.cooldown_msg_threshold,
                "cooldown_minutes": judge.cooldown_minutes,
                "user_count": counts.get(judge.id, 0),
            }
            items.append(item)
            by_id[judge.id] = item
        return items, by_id

    def _model_summary(self) -> Dict[str, Any]:
        try:
            from app.services.llm_manager import get_llm_manager

            manager = get_llm_manager()
            with manager._config_lock:
                model_configs = copy.deepcopy(manager.config.get("models", {}))
                mappings = copy.deepcopy(
                    manager.config.get("plugin_mappings", {}).get("assistant", {})
                )
        except Exception:
            return {"models": [], "mappings": {}}

        models = [
            {
                "id": model_id,
                "model": config.get("model") or model_id,
                "provider": config.get("custom_llm_provider") or config.get("provider") or "",
            }
            for model_id, config in model_configs.items()
        ]
        auxiliary_tasks = {
            "judge",
            "followup_judge",
        }
        public_mappings = {
            call_type: {
                "primary": mapping.get("primary") or "",
                "fallback": list(mapping.get("fallback") or []),
            }
            for call_type, mapping in mappings.items()
            if call_type in auxiliary_tasks and isinstance(mapping, dict)
        }
        return {"models": models, "mappings": public_mappings}

    def overview(self) -> Dict[str, Any]:
        roles, roles_by_id = self._roles()
        judges, judges_by_id = self._judges()
        role_bindings = {
            binding.user_id: binding.role_id
            for binding in self.db.query(UserChatBotRole).all()
        }
        judge_bindings = {
            binding.user_id: binding.judge_id
            for binding in self.db.query(UserChatBotJudge).all()
        }
        active_names = self._active_chat_names()
        default_role_name = str(
            get_plugin_setting("assistant", "default_role", "default") or "default"
        )
        default_role = next((role for role in roles if role["name"] == default_role_name), None)
        global_bot_name = str(get_setting("WECHAT_BOT_NAME", "刘局") or "刘局")


        users = (
            self.db.query(WeChatUser)
            .options(
                selectinload(WeChatUser.permissions),
                selectinload(WeChatUser.assistant_policy),
            )
            .order_by(WeChatUser.id)
            .all()
        )
        chats = []
        for user in users:
            permission = user.assistant_policy
            role_id = role_bindings.get(user.id)
            judge_id = judge_bindings.get(user.id)
            bot_names = bot_names_for_user(user, global_bot_name)
            chats.append(
                {
                    "id": user.id,
                    "chat_name": user.chat_name,
                    "is_group": bool(user.is_group),
                    "is_listening": user.chat_name in active_names,
                    "enabled": bool(permission and permission.enabled),
                    "proactive_enabled": bool(permission.proactive_enabled) if permission else False,
                    "followup_enabled": bool(permission.followup_enabled) if permission else False,
                    "followup_window_seconds": int(permission.followup_window_seconds or 60) if permission else 60,
                    "followup_merge_seconds": int(permission.followup_merge_seconds or 3) if permission else 3,
                    "followup_max_turns": int(permission.followup_max_turns or 3) if permission else 3,
                    "ignored_senders": _json_list(permission.ignored_senders if permission else None),
                    "role": roles_by_id.get(role_id) or default_role,
                    "role_source": "chat" if role_id else "global",
                    "judge": judges_by_id.get(judge_id),
                    "bot_group_nickname": user.bot_group_nickname or "",
                    "bot_group_nickname_auto_enabled": bool(user.bot_group_nickname_auto_enabled),
                    "bot_group_nickname_detected": user.bot_group_nickname_detected or "",
                    "bot_group_nickname_checked_at": user.bot_group_nickname_checked_at or "",
                    "bot_group_nickname_effective": bot_names[0] if bot_names else global_bot_name,
                }
            )

        capability_service = CapabilityService(self.plugin_manager, self.db)
        capability = capability_service.get_capability("assistant")
        global_flags = {
            "default_role": default_role_name,
            "allow_mention_trigger": bool(get_plugin_setting("assistant", "allow_mention_trigger", True)),
            "search_enabled": bool(get_plugin_setting("assistant", "search_enabled", True)),
            "image_enrichment_enabled": bool(
                get_plugin_setting(
                    "builtin_chat_logger",
                    "image_enrichment_enabled",
                    True,
                )
            ),
        }
        enabled_chats = sum(1 for chat in chats if chat["enabled"])
        return {
            "capability": capability,
            "summary": {
                "chat_count": len(chats),
                "enabled_chat_count": enabled_chats,
                "proactive_chat_count": sum(1 for chat in chats if chat["enabled"] and chat["proactive_enabled"]),
                "role_count": len(roles),
                "judge_count": len(judges),
            },
            "global": global_flags,
            "models": self._model_summary(),
            "roles": roles,
            "judges": judges,
            "chats": chats,
        }

    def update_chat(
        self,
        user_id: int,
        changes: Mapping[str, Any],
        *,
        commit: bool = True,
    ) -> Dict[str, Any]:
        user = self.db.query(WeChatUser).filter(WeChatUser.id == user_id).first()
        if user is None:
            raise AssistantConsoleError("聊天不存在")

        changes = dict(changes)
        nickname_fields = {"bot_group_nickname", "bot_group_nickname_auto_enabled"}
        if nickname_fields.intersection(changes) and not user.is_group:
            raise AssistantConsoleError("群内机器人昵称只能配置于群聊")
        if "bot_group_nickname" in changes:
            nickname = str(changes.get("bot_group_nickname") or "").strip().lstrip("@").strip()
            if "\n" in nickname or "\r" in nickname:
                raise AssistantConsoleError("群内机器人昵称不能换行")
            user.bot_group_nickname = nickname or None
        if "bot_group_nickname_auto_enabled" in changes:
            user.bot_group_nickname_auto_enabled = bool(changes["bot_group_nickname_auto_enabled"])
        if not user.is_group and changes.get("proactive_enabled") is True:
            if changes.get("enabled") is False:
                changes["proactive_enabled"] = False
            else:
                raise AssistantConsoleError("私聊收到消息时会直接回复，不支持主动回复开关")

        # 主动回复只有一个用户入口：聊天级开关。开启时必须有 Judge；
        # 若界面没有显式选择，则沿用已有绑定或自动绑定默认 Judge。
        if (
            changes.get("enabled") is not False
            and user.is_group
            and changes.get("proactive_enabled") is True
            and changes.get("judge_id") is None
        ):
            current_binding = (
                self.db.query(UserChatBotJudge)
                .filter(UserChatBotJudge.user_id == user_id)
                .first()
            )
            if current_binding:
                changes["judge_id"] = current_binding.judge_id
            else:
                default_judge = (
                    self.db.query(ChatBotJudge)
                    .filter(ChatBotJudge.name == "default_judge")
                    .first()
                    or self.db.query(ChatBotJudge).order_by(ChatBotJudge.id).first()
                )
                if default_judge is None:
                    raise AssistantConsoleError("启用主动回复前需要先创建 Judge")
                changes["judge_id"] = default_judge.id

        # Validate referenced records before changing the current unit of work.
        # This keeps an invalid role/judge request from leaving pending permission
        # mutations in a request-scoped SQLAlchemy session.
        if changes.get("role_id") is not None and not self.db.query(ChatBotRole).filter(
            ChatBotRole.id == changes["role_id"]
        ).first():
            raise AssistantConsoleError("角色不存在")
        if changes.get("judge_id") is not None and not self.db.query(ChatBotJudge).filter(
            ChatBotJudge.id == changes["judge_id"]
        ).first():
            raise AssistantConsoleError("Judge 不存在")

        permission = (
            self.db.query(AssistantChatPolicy)
            .filter(AssistantChatPolicy.user_id == user_id)
            .first()
        )
        permission_fields = {
            "proactive_enabled",
            "followup_enabled",
            "followup_window_seconds",
            "followup_merge_seconds",
            "followup_max_turns",
            "codex_profile_id",
        }
        has_permission_changes = bool(
            permission_fields.intersection(changes) or "ignored_senders" in changes
        )
        explicitly_disabled = changes.get("enabled") is False
        should_enable = bool(
            changes.get("enabled") is True
            or (
                "enabled" not in changes
                and (
                    bool(permission and permission.enabled)
                    or has_permission_changes
                )
            )
        )
        if should_enable and permission is None:
            permission = AssistantChatPolicy(user_id=user_id, enabled=True)
            self.db.add(permission)
        if permission is not None:
            if explicitly_disabled:
                permission.enabled = False
            elif should_enable:
                permission.enabled = True
            for field_name in permission_fields.intersection(changes):
                value = changes[field_name]
                if field_name == "codex_profile_id":
                    value = str(value or "").strip() or None
                    if value:
                        from app.services.codex_profile_service import get_codex_profile_service

                        if get_codex_profile_service().get_profile(value) is None:
                            raise AssistantConsoleError("Codex Profile 不存在")
                setattr(permission, field_name, value)
            if "ignored_senders" in changes:
                permission.ignored_senders = (
                    json.dumps(_json_list(changes["ignored_senders"]), ensure_ascii=False)
                    if changes["ignored_senders"]
                    else None
                )
            permission.version = int(permission.version or 0) + 1

        reload_roles = False
        if "role_id" in changes:
            role_id = changes["role_id"]
            binding = self.db.query(UserChatBotRole).filter(UserChatBotRole.user_id == user_id).first()
            if role_id is None:
                if binding:
                    self.db.delete(binding)
            else:
                if binding:
                    binding.role_id = role_id
                else:
                    self.db.add(UserChatBotRole(user_id=user_id, role_id=role_id))
            reload_roles = True

        reload_judges = False
        if "judge_id" in changes:
            judge_id = changes["judge_id"]
            binding = self.db.query(UserChatBotJudge).filter(UserChatBotJudge.user_id == user_id).first()
            if judge_id is None:
                if binding:
                    self.db.delete(binding)
            else:
                if binding:
                    binding.judge_id = judge_id
                else:
                    self.db.add(UserChatBotJudge(user_id=user_id, judge_id=judge_id))
            reload_judges = True

        effect = {
            "chat_name": user.chat_name,
            "reload_roles": reload_roles,
            "reload_judges": reload_judges,
        }
        if not commit:
            return effect

        user.policy_version = int(user.policy_version or 1) + 1
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        self.apply_runtime_side_effects(effect)
        return effect

    @staticmethod
    def apply_runtime_side_effects(effect: Mapping[str, Any]) -> None:
        """Refresh non-authoritative runtime caches after a committed update."""

        reload_roles = bool(effect.get("reload_roles"))
        reload_judges = bool(effect.get("reload_judges"))
        if reload_roles or reload_judges:
            try:
                from app.assistant.runtime import get_assistant_handler

                handler = get_assistant_handler()
                if handler and reload_roles:
                    handler.reload_roles()
                if handler and reload_judges:
                    handler.reload_judges()
            except Exception:
                # The database is authoritative; a future request or plugin
                # restart will refresh the in-memory cache.
                pass
