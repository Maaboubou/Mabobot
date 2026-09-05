"""Product-facing message routing model for the Web automation workbench."""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Mapping, Optional

from sqlalchemy.orm import Session

from app.core.event_bus import EventType
from app.core.plugin_manifest import config_value, describe_listener_trigger
from app.models.user_permission import WeChatUser


MESSAGE_EVENT_META: Dict[EventType, Dict[str, str]] = {
    EventType.TEXT_MESSAGE_RECEIVED: {"label": "文本消息", "icon": "bi-chat-text"},
    EventType.IMAGE_MESSAGE_RECEIVED: {"label": "图片消息", "icon": "bi-image"},
    EventType.LINK_MESSAGE_RECEIVED: {"label": "链接消息", "icon": "bi-link-45deg"},
    EventType.QUOTE_MESSAGE_RECEIVED: {"label": "通用引用", "icon": "bi-reply"},
    EventType.QUOTE_TEXT_MESSAGE_RECEIVED: {"label": "引用文本", "icon": "bi-chat-quote"},
    EventType.QUOTE_IMAGE_MESSAGE_RECEIVED: {"label": "引用图片", "icon": "bi-images"},
    EventType.QUOTE_VIDEO_MESSAGE_RECEIVED: {"label": "引用视频", "icon": "bi-camera-video-fill"},
    EventType.EMOTION_MESSAGE_RECEIVED: {"label": "表情消息", "icon": "bi-emoji-smile"},
    EventType.VOICE_MESSAGE_RECEIVED: {"label": "语音消息", "icon": "bi-mic"},
    EventType.VIDEO_MESSAGE_RECEIVED: {"label": "视频消息", "icon": "bi-camera-video"},
    EventType.FILE_MESSAGE_RECEIVED: {"label": "文件消息", "icon": "bi-file-earmark"},
    EventType.LOCATION_MESSAGE_RECEIVED: {"label": "位置消息", "icon": "bi-geo-alt"},
    EventType.MERGE_MESSAGE_RECEIVED: {"label": "合并消息", "icon": "bi-collection"},
    EventType.PERSONAL_CARD_MESSAGE_RECEIVED: {"label": "名片消息", "icon": "bi-person-vcard"},
    EventType.NOTE_MESSAGE_RECEIVED: {"label": "通知消息", "icon": "bi-sticky"},
    EventType.OTHER_MESSAGE_RECEIVED: {"label": "其他消息", "icon": "bi-inbox"},
}


class AutomationRoutingError(ValueError):
    """Raised when a routing edit cannot be applied safely."""


def _plugin_base(plugin_name: str) -> str:
    return str(plugin_name or "").rsplit("/", 1)[-1]


class AutomationRoutingService:
    """Build and mutate the effective per-event listener routing graph."""

    def __init__(self, plugin_manager: Any, db: Optional[Session] = None):
        self.plugin_manager = plugin_manager
        self.event_bus = plugin_manager.event_bus
        self.db = db

    def _chat_context(self, chat_id: Optional[int], mentioned: bool) -> Dict[str, Any]:
        context: Dict[str, Any] = {
            "selected_chat_id": chat_id,
            "mentioned": bool(mentioned),
            "chat": None,
            "permissions": {},
        }
        if self.db is None or chat_id is None:
            return context
        chat = self.db.query(WeChatUser).filter(WeChatUser.id == chat_id).first()
        if chat is None:
            raise AutomationRoutingError("选择的聊天不存在")
        context["chat"] = chat
        context["permissions"] = {permission.plugin_name: permission for permission in chat.permissions}
        return context

    @staticmethod
    def _permission_for(plugin_name: str, permissions: Mapping[str, Any]) -> Optional[Any]:
        base = _plugin_base(plugin_name)
        if plugin_name in permissions:
            return permissions[plugin_name]
        if base in permissions:
            return permissions[base]
        for permission_name, permission in permissions.items():
            permission_base = str(permission_name).split("#", 1)[0].rsplit("/", 1)[-1]
            if permission_base in {plugin_name, base}:
                return permission
        return None

    def _scope_state(self, listener: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
        if not listener.get("enabled", True):
            return {"eligible": False, "reason": "监听器已停用", "condition": "已停用"}
        chat = context.get("chat")
        if chat is None:
            return {"eligible": True, "reason": "", "condition": ""}

        spec = listener.get("trigger_spec") or {}
        scope = spec.get("scope") or {}
        selected_chat_type = "group" if bool(chat.is_group) else "user"
        allowed_chat_types = scope.get("chat_types") or ["group", "user"]
        if selected_chat_type not in allowed_chat_types:
            label = "群聊" if selected_chat_type == "group" else "私聊"
            return {
                "eligible": False,
                "reason": f"插件声明不处理{label}",
                "condition": f"不适用于{label}",
            }

        plugin = self.plugin_manager.get_plugin_info(str(listener.get("plugin_name") or ""))
        chat_name_key = scope.get("chat_name_config_key")
        if plugin is not None and chat_name_key:
            configured_chat = str(config_value(plugin.config or {}, str(chat_name_key)) or "").strip()
            if configured_chat and configured_chat != str(chat.chat_name):
                return {
                    "eligible": False,
                    "reason": f"仅处理配置中的聊天：{configured_chat}",
                    "condition": "非目标聊天",
                }

        permission = self._permission_for(listener["plugin_name"], context.get("permissions", {}))
        if permission is None:
            return {"eligible": False, "reason": "未分配到该聊天", "condition": "未分配"}
        plugin_base = _plugin_base(listener["plugin_name"])
        if (
            plugin_base not in {"assistant", "builtin_chatbot"}
            and bool(chat.is_group)
            and bool(permission.require_mention)
            and not bool(context.get("mentioned"))
        ):
            return {"eligible": False, "reason": "该群聊要求 @ 机器人", "condition": "需要 @"}
        condition = "已满足 @ 条件" if bool(chat.is_group) and bool(permission.require_mention) else ""
        return {"eligible": True, "reason": "", "condition": condition}

    def _capability_meta(self, plugin_name: str) -> Dict[str, Any]:
        plugin = self.plugin_manager.get_plugin_info(plugin_name)
        if plugin is None:
            return {
                "display_name": plugin_name,
                "description": "",
                "status": "unknown",
                "icon": "bi-puzzle",
            }
        config = plugin.config or {}
        category_icons = {
            "ai": "bi-stars",
            "content": "bi-file-richtext",
            "language": "bi-translate",
            "image": "bi-image",
            "finance": "bi-graph-up-arrow",
            "logistics": "bi-box-seam",
            "tool": "bi-tools",
            "utility": "bi-lightning-charge",
        }
        if plugin.enabled and plugin.loaded:
            status = "running"
        elif not plugin.enabled:
            status = "disabled"
        else:
            status = "error"
        return {
            "display_name": str(config.get("display_name") or plugin.name or plugin_name),
            "description": plugin.description or str(config.get("description") or ""),
            "status": status,
            "icon": str((config.get("ui") or {}).get("icon") or category_icons.get(config.get("category"), "bi-puzzle")),
        }

    @staticmethod
    def _scope_summary(listener: Mapping[str, Any], config: Mapping[str, Any]) -> str:
        scope = (listener.get("trigger_spec") or {}).get("scope") or {}
        chat_types = set(scope.get("chat_types") or ["group", "user"])
        if chat_types == {"group"}:
            summary = "仅群聊"
        elif chat_types == {"user"}:
            summary = "仅私聊"
        else:
            summary = "群聊与私聊"
        chat_name_key = scope.get("chat_name_config_key")
        if chat_name_key:
            chat_name = str(config_value(config, str(chat_name_key)) or "").strip()
            if chat_name:
                summary += f" · {chat_name}"
        return summary

    def _event_items(
        self,
        event_type: EventType,
        context: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        listeners = [
            listener
            for listener in self.event_bus.get_listeners(event_type).get(event_type.value, [])
            if listener.get("owner_kind", "plugin") == "plugin"
        ]
        items: List[Dict[str, Any]] = []
        eligible_rank = 0
        for absolute_rank, listener in enumerate(listeners, start=1):
            scope = self._scope_state(listener, context)
            if scope["eligible"]:
                eligible_rank += 1
            meta = self._capability_meta(listener["plugin_name"])
            propagation = str(listener.get("propagation") or "continue")
            can_block = propagation == "stop_on_consumed"
            plugin = self.plugin_manager.get_plugin_info(str(listener.get("plugin_name") or ""))
            plugin_config = plugin.config if plugin is not None else {}
            trigger = describe_listener_trigger(
                listener.get("trigger_spec") or {},
                plugin_config,
            )
            item = {
                **listener,
                **meta,
                **scope,
                "rank": absolute_rank,
                "eligible_rank": eligible_rank if scope["eligible"] else None,
                "can_block": can_block,
                "propagation": propagation,
                "listener_title": (listener.get("trigger_spec") or {}).get("title", ""),
                "trigger": trigger,
                "scope_summary": self._scope_summary(listener, plugin_config),
            }
            items.append(item)
        return items

    def _chats(self) -> List[Dict[str, Any]]:
        if self.db is None:
            return []
        chats = self.db.query(WeChatUser).order_by(WeChatUser.chat_name).all()
        return [
            {
                "id": chat.id,
                "chat_name": chat.chat_name,
                "display_name": chat.chat_name,
                "is_group": bool(chat.is_group),
            }
            for chat in chats
        ]

    @staticmethod
    def _signature(routes: Mapping[str, Iterable[Mapping[str, Any]]]) -> str:
        parts = []
        for event_type, listeners in sorted(routes.items()):
            for listener in listeners:
                parts.append(
                    f"{event_type}|{listener.get('plugin_name')}|{listener.get('listener_key')}|"
                    f"{listener.get('order_index')}|{int(bool(listener.get('enabled')))}"
                )
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]

    def overview(self, chat_id: Optional[int] = None, mentioned: bool = True) -> Dict[str, Any]:
        context = self._chat_context(chat_id, mentioned)
        routes: Dict[str, List[Dict[str, Any]]] = {}
        event_types: List[Dict[str, Any]] = []
        blocker_count = 0
        listener_count = 0
        for event_type, meta in MESSAGE_EVENT_META.items():
            items = self._event_items(event_type, context)
            if not items:
                continue
            routes[event_type.value] = items
            blockers = sum(1 for item in items if item["can_block"])
            listener_count += len(items)
            blocker_count += blockers
            event_types.append(
                {
                    "id": event_type.value,
                    "label": meta["label"],
                    "icon": meta["icon"],
                    "listener_count": len(items),
                    "eligible_count": sum(1 for item in items if item["eligible"]),
                    "blocker_count": blockers,
                }
            )
        selected_chat = context.get("chat")
        return {
            "event_types": event_types,
            "routes": routes,
            "chats": self._chats(),
            "context": {
                "chat_id": selected_chat.id if selected_chat is not None else None,
                "chat_name": selected_chat.chat_name if selected_chat is not None else None,
                "chat_display_name": selected_chat.chat_name if selected_chat is not None else "全部聊天",
                "is_group": bool(selected_chat.is_group) if selected_chat is not None else None,
                "mentioned": bool(mentioned),
            },
            "summary": {
                "event_count": len(event_types),
                "listener_count": listener_count,
                "blocker_count": blocker_count,
            },
            "signature": self._signature(routes),
        }

    def _event_listeners(self, event_type_value: str) -> tuple[EventType, List[Dict[str, Any]]]:
        try:
            event_type = EventType(event_type_value)
        except ValueError as exc:
            raise AutomationRoutingError("未知的消息事件类型") from exc
        if event_type not in MESSAGE_EVENT_META:
            raise AutomationRoutingError("该事件不允许在消息路由中排序")
        listeners = [
            listener
            for listener in self.event_bus.get_listeners(event_type).get(event_type.value, [])
            if listener.get("owner_kind", "plugin") == "plugin"
        ]
        if not listeners:
            raise AutomationRoutingError("该事件当前没有可排序的监听器")
        listener_keys = [str(listener.get("listener_key") or "") for listener in listeners]
        if len(listener_keys) != len(set(listener_keys)):
            raise AutomationRoutingError("当前事件存在重复监听器标识，请重新加载相关插件")
        return event_type, listeners

    def apply_order(
        self,
        event_type_value: str,
        listener_keys: List[str],
        expected_signature: Optional[str] = None,
    ) -> Dict[str, Any]:
        event_type, listeners = self._event_listeners(event_type_value)
        current_keys = [str(listener.get("listener_key") or "") for listener in listeners]
        if len(listener_keys) != len(set(listener_keys)):
            raise AutomationRoutingError("排序列表包含重复监听器")
        if set(listener_keys) != set(current_keys):
            raise AutomationRoutingError("监听器集合已经变化，请刷新页面后重试")
        if expected_signature:
            live_signature = self.overview()["signature"]
            if live_signature != expected_signature:
                raise AutomationRoutingError("路由已被其他操作修改，请刷新后重试")

        previous_order = self.plugin_manager.routing_order.event_order(event_type.value)
        try:
            self.plugin_manager.routing_order.replace_event(event_type.value, listener_keys)
            if not self.event_bus.update_event_order(event_type, listener_keys):
                raise RuntimeError("运行时监听器集合已经变化")
        except Exception:
            if previous_order:
                self.plugin_manager.routing_order.replace_event(event_type.value, previous_order)
            raise
        ordered = [
            listener
            for listener in self.event_bus.get_listeners(event_type).get(event_type.value, [])
            if listener.get("owner_kind", "plugin") == "plugin"
        ]
        return {
            "message": "执行顺序已保存并立即生效",
            "event_type": event_type.value,
            "listeners": ordered,
        }
