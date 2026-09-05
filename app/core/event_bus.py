"""
事件总线系统 - 核心架构
负责事件的发布、订阅和分发
"""

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import threading
import uuid
import queue
import time

from sqlalchemy.orm import Session
from app.models.assistant_policy import AssistantChatPolicy
from app.models.user_permission import WeChatUser
from app.utils.bot_mentions import (
    bot_names_for_user,
    find_bot_mention,
    tickle_self_flags,
)


def _parse_sender_blacklist(raw_value: Any) -> List[str]:
    """Parse a per-chat sender blacklist stored as JSON array or newline text."""
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except Exception:
        parsed = str(raw_value).splitlines()
    if not isinstance(parsed, list):
        return []
    return [str(item or "").strip() for item in parsed if str(item or "").strip()]


class EventType(str, Enum):
    """事件类型定义"""
    # 微信消息事件
    TEXT_MESSAGE_RECEIVED = "text_message_received"
    IMAGE_MESSAGE_RECEIVED = "image_message_received"
    LINK_MESSAGE_RECEIVED = "link_message_received"
    QUOTE_MESSAGE_RECEIVED = "quote_message_received"  # 保留原有的通用引用事件
    QUOTE_TEXT_MESSAGE_RECEIVED = "quote_text_message_received"  # 新增：引用文字消息
    QUOTE_IMAGE_MESSAGE_RECEIVED = "quote_image_message_received"  # 新增：引用图片消息
    QUOTE_VIDEO_MESSAGE_RECEIVED = "quote_video_message_received"
    EMOTION_MESSAGE_RECEIVED = "emotion_message_received"
    VOICE_MESSAGE_RECEIVED = "voice_message_received"
    VIDEO_MESSAGE_RECEIVED = "video_message_received"
    FILE_MESSAGE_RECEIVED = "file_message_received"
    LOCATION_MESSAGE_RECEIVED = "location_message_received"
    MERGE_MESSAGE_RECEIVED = "merge_message_received"
    PERSONAL_CARD_MESSAGE_RECEIVED = "personal_card_message_received"
    NOTE_MESSAGE_RECEIVED = "note_message_received"
    TICKLE_MESSAGE_RECEIVED = "tickle_message_received"
    OTHER_MESSAGE_RECEIVED = "other_message_received"
    CHATBOT_FOLLOWUP_APPROVED = "chatbot_followup_approved"

    # 插件协作事件
    SUMMARY_COMPLETED = "summary_completed"

    # 系统事件
    PLUGIN_LOADED = "plugin_loaded"
    PLUGIN_UNLOADED = "plugin_unloaded"
    SYSTEM_STARTUP = "system_startup"
    SYSTEM_SHUTDOWN = "system_shutdown"
    WECHAT_RECONNECTED = "wechat_reconnected"

    # 用户配置事件
    USER_CONFIG_UPDATED = "user_config_updated"
    PLUGIN_CONFIG_UPDATED = "plugin_config_updated"


@dataclass
class Event:
    """事件对象"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    type: EventType = EventType.TEXT_MESSAGE_RECEIVED
    data: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    timestamp: float = field(default_factory=lambda: __import__('time').time())


@dataclass
class EventListener:
    """事件监听器"""
    id: str
    plugin_name: str
    event_type: EventType
    handler: Callable[[Event], Any]
    order_index: int = 0
    enabled: bool = True
    propagation: str = "continue"
    # Stable product-facing identity. Runtime listener ids may change after a
    # reload, while routing overrides must continue to target the same handler.
    listener_key: str = ""
    handler_name: str = ""
    order_source: str = "routing_order"
    trigger_spec: Dict[str, Any] = field(default_factory=dict)
    owner_kind: str = "plugin"
    permission_key: str = ""
    display_name: str = ""


class EventBus:
    """事件总线核心类"""

    def __init__(self, db_session_factory: Callable[[], Session]):
        self.logger = logging.getLogger(__name__)
        self._listeners: Dict[EventType, List[EventListener]] = {}
        self._event_queue = asyncio.Queue()
        self._running = False
        self._processor_task: Optional[asyncio.Task] = None
        self._lock = threading.RLock()
        self.db_session_factory = db_session_factory
        # 全局上下文字典：用于共享跨插件/系统的服务实例（例如 feishu_service）
        self.context: Dict[str, Any] = {}
        self._stats = {
            'events_published': 0,
            'events_processed': 0,
            'listeners_count': 0
        }

        # Per-user async processing infrastructure
        self._user_queues: Dict[str, queue.Queue] = {}  # {chat_name: Queue}
        self._user_workers: Dict[str, threading.Thread] = {}  # {chat_name: Thread}
        self._user_locks: Dict[str, threading.Lock] = {}  # {chat_name: Lock}
        self._user_last_activity: Dict[str, float] = {}  # {chat_name: timestamp}
        # Updated when a real inbound message is enqueued, even if that chat's
        # worker is currently blocked in an LLM call. Follow-up replies use the
        # sequence to discard stale Judge/model results.
        self._chat_ingress_sequences: Dict[str, int] = {}
        self._chat_latest_ingress: Dict[str, Dict[str, Any]] = {}

        # Configuration
        self._max_concurrent_users = 50  # Maximum concurrent user workers
        self._user_queue_maxsize = 100  # Per-user queue size limit
        self._worker_idle_timeout = 300  # Worker cleanup timeout (5 minutes)

    def subscribe(
        self,
        event_type: EventType,
        handler: Callable[[Event], Any],
        plugin_name: str,
        order_index: int = 0,
        listener_id: Optional[str] = None,
        propagation: str = "continue",
        listener_key: str = "",
        handler_name: str = "",
        order_source: str = "routing_order",
        trigger_spec: Optional[Dict[str, Any]] = None,
        owner_kind: str = "plugin",
        permission_key: str = "",
        display_name: str = "",
    ) -> str:
        """订阅事件"""
        with self._lock:
            if listener_id is None:
                listener_id = f"{plugin_name}_{event_type}_{uuid.uuid4().hex[:8]}"

            listener = EventListener(
                id=listener_id,
                plugin_name=plugin_name,
                event_type=event_type,
                handler=handler,
                order_index=order_index,
                propagation=propagation,
                listener_key=listener_key or listener_id,
                handler_name=handler_name or getattr(handler, "__name__", "handler"),
                order_source=order_source,
                trigger_spec=dict(trigger_spec or {}),
                owner_kind=("core" if owner_kind == "core" else "plugin"),
                permission_key=str(permission_key or ""),
                display_name=str(display_name or ""),
            )

            if event_type not in self._listeners:
                self._listeners[event_type] = []

            self._listeners[event_type].append(listener)
            self._listeners[event_type].sort(
                key=lambda x: (x.owner_kind == "core", x.order_index, x.listener_key)
            )

            self._stats['listeners_count'] += 1

            self.logger.debug(
                f"Plugin '{plugin_name}' subscribed to {event_type} "
                f"(order: {order_index}, propagation: {propagation})"
            )
            return listener_id

    def unsubscribe(self, listener_id: str) -> bool:
        """取消订阅"""
        with self._lock:
            for event_type, listeners in self._listeners.items():
                for i, listener in enumerate(listeners):
                    if listener.id == listener_id:
                        listeners.pop(i)
                        self._stats['listeners_count'] -= 1
                        self.logger.debug(f"Unsubscribed listener {listener_id} from {event_type}")
                        return True
            return False

    def unsubscribe_plugin(self, plugin_name: str) -> int:
        """取消插件的所有订阅"""
        with self._lock:
            count = 0
            for event_type, listeners in self._listeners.items():
                listeners_to_remove = [l for l in listeners if l.plugin_name == plugin_name]
                for listener in listeners_to_remove:
                    listeners.remove(listener)
                    count += 1

            self._stats['listeners_count'] -= count
            if count > 0:
                self.logger.debug(f"Unsubscribed {count} listeners for plugin '{plugin_name}'")
            return count

    def _create_user_queue(self, chat_name: str) -> None:
        """Create queue and worker thread for a specific user"""
        with self._lock:
            # Check if we've reached max concurrent users
            if len(self._user_workers) >= self._max_concurrent_users:
                self.logger.warning(
                    f"Max concurrent users ({self._max_concurrent_users}) reached. "
                    f"Cannot create worker for '{chat_name}'"
                )
                # Try to cleanup idle workers
                self._cleanup_idle_workers()

            if chat_name not in self._user_queues:
                # Create queue
                self._user_queues[chat_name] = queue.Queue(maxsize=self._user_queue_maxsize)
                self._user_locks[chat_name] = threading.Lock()
                self._user_last_activity[chat_name] = time.time()

                # Create and start worker thread
                worker = threading.Thread(
                    target=self._user_worker,
                    args=(chat_name,),
                    name=f"EventBus-Worker-{chat_name}",
                    daemon=True
                )
                self._user_workers[chat_name] = worker
                worker.start()

                self.logger.debug(f"Created worker thread for user '{chat_name}'")

    def _enqueue_user_event(self, chat_name: str, event: Event) -> None:
        """Enqueue event to user-specific queue"""
        if self._is_inbound_message_event(event):
            with self._lock:
                sequence = self._chat_ingress_sequences.get(chat_name, 0) + 1
                self._chat_ingress_sequences[chat_name] = sequence
                event.data["_chat_seq"] = sequence
                self._chat_latest_ingress[chat_name] = {
                    "sequence": sequence,
                    "event_type": event.type.value,
                    "timestamp": float(event.timestamp or time.time()),
                    "sender": str(event.data.get("sender") or ""),
                    "message": str(event.data.get("message") or ""),
                    "message_id": str(event.data.get("message_id") or ""),
                    "bot_mentioned": bool(event.data.get("bot_mentioned", False)),
                    "bot_mention_name": str(event.data.get("bot_mention_name") or ""),
                }

        # Ensure user queue exists
        if chat_name not in self._user_queues:
            self._create_user_queue(chat_name)

        # Update last activity
        self._user_last_activity[chat_name] = time.time()

        # Add event to user's queue
        try:
            self._user_queues[chat_name].put(event, block=False)
            self.logger.debug(f"Enqueued event {event.type} for user '{chat_name}'")
        except queue.Full:
            self.logger.warning(
                f"User queue full for '{chat_name}' (size: {self._user_queue_maxsize}). "
                f"Dropping event {event.type}"
            )

    @staticmethod
    def _is_inbound_message_event(event: Event) -> bool:
        return event.type in {
            EventType.TEXT_MESSAGE_RECEIVED,
            EventType.IMAGE_MESSAGE_RECEIVED,
            EventType.LINK_MESSAGE_RECEIVED,
            EventType.QUOTE_MESSAGE_RECEIVED,
            EventType.QUOTE_TEXT_MESSAGE_RECEIVED,
            EventType.QUOTE_IMAGE_MESSAGE_RECEIVED,
            EventType.QUOTE_VIDEO_MESSAGE_RECEIVED,
            EventType.EMOTION_MESSAGE_RECEIVED,
            EventType.VOICE_MESSAGE_RECEIVED,
            EventType.VIDEO_MESSAGE_RECEIVED,
            EventType.FILE_MESSAGE_RECEIVED,
            EventType.LOCATION_MESSAGE_RECEIVED,
            EventType.MERGE_MESSAGE_RECEIVED,
            EventType.PERSONAL_CARD_MESSAGE_RECEIVED,
            EventType.NOTE_MESSAGE_RECEIVED,
            EventType.TICKLE_MESSAGE_RECEIVED,
            EventType.OTHER_MESSAGE_RECEIVED,
        }

    def get_chat_ingress_state(self, chat_name: str) -> Dict[str, Any]:
        """Return a thread-safe snapshot of the newest received chat event."""
        with self._lock:
            latest = dict(self._chat_latest_ingress.get(chat_name, {}))
            latest.setdefault(
                "sequence",
                int(self._chat_ingress_sequences.get(chat_name, 0)),
            )
            return latest

    def _user_worker(self, chat_name: str) -> None:
        """Worker thread for processing a specific user's events"""
        self.logger.debug(f"Worker thread started for user '{chat_name}'")

        while self._running:
            try:
                # Get event from user's queue with timeout
                event = self._user_queues[chat_name].get(timeout=1.0)

                # Update last activity
                self._user_last_activity[chat_name] = time.time()

                # Process event using existing sync logic
                self._process_event_sync(event)

                # Mark task as done
                self._user_queues[chat_name].task_done()

            except queue.Empty:
                # Check if we should cleanup due to idle timeout
                idle_time = time.time() - self._user_last_activity.get(chat_name, time.time())
                if idle_time > self._worker_idle_timeout:
                    self.logger.debug(
                        f"Worker for '{chat_name}' idle for {idle_time:.1f}s, cleaning up"
                    )
                    self._cleanup_user_worker(chat_name)
                    break
                continue

            except Exception as e:
                self.logger.error(f"Error in user worker for '{chat_name}': {e}", exc_info=True)

        self.logger.debug(f"Worker thread stopped for user '{chat_name}'")

    def _cleanup_user_worker(self, chat_name: str) -> None:
        """Cleanup resources for a specific user worker"""
        with self._lock:
            if chat_name in self._user_queues:
                del self._user_queues[chat_name]
            if chat_name in self._user_workers:
                del self._user_workers[chat_name]
            if chat_name in self._user_locks:
                del self._user_locks[chat_name]
            if chat_name in self._user_last_activity:
                del self._user_last_activity[chat_name]

            self.logger.debug(f"Cleaned up worker resources for user '{chat_name}'")

    def _cleanup_idle_workers(self) -> None:
        """Cleanup all idle workers that have exceeded timeout"""
        current_time = time.time()
        idle_users = []

        with self._lock:
            for chat_name, last_activity in self._user_last_activity.items():
                idle_time = current_time - last_activity
                if idle_time > self._worker_idle_timeout:
                    idle_users.append(chat_name)

        for chat_name in idle_users:
            # Check if queue is empty before cleanup
            if chat_name in self._user_queues and self._user_queues[chat_name].empty():
                self._cleanup_user_worker(chat_name)


    def publish(self, event: Event) -> None:
        """发布事件 - 根据用户路由到不同队列"""
        self._stats['events_published'] += 1
        self.logger.debug(f"Publishing event {event.type} from {event.source}")

        # Check if this is a user event
        chat_name = event.data.get("chat_name")

        if chat_name:
            # User event: route to user-specific queue for async processing
            self.logger.debug(f"Routing event {event.type} to user queue for '{chat_name}'")
            self._enqueue_user_event(chat_name, event)
        else:
            # System event: process immediately (synchronous)
            self.logger.debug(f"Processing system event {event.type} synchronously")
            self._process_event_sync(event)

    def _process_event_sync(self, event: Event) -> None:
        """同步处理事件（原publish方法的核心逻辑）"""
        # 获取对应事件类型的监听器
        with self._lock:
            listeners = self._listeners.get(event.type, [])
            active_listeners = [l for l in listeners if l.enabled]

        # 如果事件中有chat_name，则进行权限检查
        chat_name = event.data.get("chat_name")
        if chat_name:
            db = self.db_session_factory()
            try:
                user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat_name).first()
                if user:
                    sender = str(event.data.get("sender") or "").strip()
                    sender_blacklist = _parse_sender_blacklist(getattr(user, "sender_blacklist", None))
                    if sender and sender in sender_blacklist:
                        self.logger.info(
                            "Sender '%s' is blacklisted for chat '%s'; skipping all plugin listeners for %s.",
                            sender,
                            chat_name,
                            event.type.value,
                        )
                        return

                    # 获取机器人名称用于@检测
                    from app.services.config_service import get_setting
                    bot_name = get_setting("WECHAT_BOT_NAME", "刘局")

                    # 检查消息内容是否@了机器人
                    message_content = event.data.get("message", "")
                    chat_type = event.data.get("chat_type", "")
                    bot_names = bot_names_for_user(user, bot_name)
                    mention_name = find_bot_mention(
                        message_content,
                        bot_names,
                    )
                    is_mentioned = mention_name is not None
                    event.data["bot_mentioned"] = is_mentioned
                    event.data["bot_mention_name"] = mention_name or ""
                    if bool(event.data.get("is_tickle")):
                        tickle_is_from_self, tickle_is_to_self = tickle_self_flags(
                            event.data.get("tickle_from"),
                            event.data.get("tickle_to"),
                            bot_names,
                        )
                        event.data["tickle_is_from_self"] = tickle_is_from_self
                        event.data["tickle_is_to_self"] = tickle_is_to_self
                    sequence = int(event.data.get("_chat_seq") or 0)
                    if sequence:
                        with self._lock:
                            latest = self._chat_latest_ingress.get(chat_name)
                            if latest and int(latest.get("sequence") or 0) == sequence:
                                latest["bot_mentioned"] = is_mentioned
                                latest["bot_mention_name"] = mention_name or ""

                    # 获取该用户的权限配置
                    user_permissions = {p.plugin_name: p for p in user.permissions}

                    # 支持多级目录插件名：既匹配完整键（如 feishu/xxx），也匹配末级简名（如 xxx）
                    def _has_permission_and_mention_check(listener: EventListener) -> bool:
                        if listener.owner_kind == "core":
                            policy = (
                                db.query(AssistantChatPolicy)
                                .filter(AssistantChatPolicy.user_id == user.id)
                                .first()
                            )
                            return bool(policy and policy.enabled)

                        # 首先检查是否有权限
                        permission_config = None
                        permission_name = listener.permission_key or listener.plugin_name
                        plugin_base = permission_name.rsplit('/', 1)[-1]
                        if permission_name in user_permissions:
                            permission_config = user_permissions[permission_name]
                        else:
                            # 检查末级简名匹配
                            if plugin_base in user_permissions:
                                permission_config = user_permissions[plugin_base]
                            else:
                                # Feature permissions such as Weekly#push or Weekly#admin
                                # should also allow the base plugin listener to inspect
                                # the message and decide whether to consume it.
                                for permission_key, permission in user_permissions.items():
                                    permission_base = str(permission_key).split("#", 1)[0].rsplit("/", 1)[-1]
                                    if permission_base in {permission_name, plugin_base}:
                                        permission_config = permission
                                        break

                        if not permission_config:
                            return False  # 没有权限

                        # 检查@触发条件
                        if permission_config.require_mention:
                            # 优先检查是否存在“会话期权限豁免”
                            if self._check_session_permission(chat_name, permission_name):
                                # 存在有效会话，豁免@检查
                                pass
                            # 否则执行标准的@检查
                            elif chat_type == "group" and not is_mentioned:
                                self.logger.debug(f"Plugin '{permission_name}' requires mention but not mentioned for user '{chat_name}'")
                                return False
                            # 私聊不需要@，群聊需要@且已经@了才通过

                        return True

                    # 筛选出有权限且满足@条件的监听器
                    final_listeners = [l for l in active_listeners if _has_permission_and_mention_check(l)]

                    allowed_plugins = set(user_permissions.keys())
                    self.logger.debug(
                        f"User '{chat_name}' has permissions: {allowed_plugins}. Mentioned: {is_mentioned}. Executing {len(final_listeners)} listeners after permission and mention check."
                    )
                else:
                    # Unknown chats are not authorized implicitly.  Discovery
                    # and authorization are administrator-owned operations.
                    final_listeners = []
                    self.logger.info(
                        "Chat '%s' is not managed; denying all message handlers",
                        chat_name,
                    )
            finally:
                db.close()
        else:
            # 如果事件不涉及特定用户（如系统事件），则所有监听器都有权限
            final_listeners = active_listeners

        # 同步执行所有有权限的监听器
        for listener in final_listeners:
            if (
                listener.owner_kind == "core"
                and isinstance(getattr(event, "data", None), dict)
                and event.data.get("_consumed") is True
            ):
                self.logger.debug(
                    "Skipping core fallback '%s' because an earlier plugin consumed %s",
                    listener.plugin_name,
                    event.type.value,
                )
                continue
            try:
                self.logger.debug(f"Executing handler for {listener.plugin_name} for user {chat_name}")
                # 为本次调用注入 wx 代理：当插件调用发送相关方法时自动标记已消费
                original_wx = event.context.get("wx")
                proxy_installed = False

                if original_wx is not None:
                    class _WxConsumeProxy:
                        def __init__(
                            self,
                            wx_obj,
                            evt,
                            plugin_name: str,
                            owner_kind: str,
                            display_name: str,
                        ):
                            self._wx = wx_obj
                            self._evt = evt
                            self._plugin_name = plugin_name
                            self._owner_kind = owner_kind
                            self._display_name = display_name

                        def _mark_consumed(self):
                            try:
                                if isinstance(getattr(self._evt, 'data', {}), dict):
                                    self._evt.data['_consumed'] = True
                            except Exception:
                                pass

                        def _get_bot_display_name(self) -> str:
                            bot_display_name = None
                            try:
                                my_info = self._wx.my_info()
                                if my_info and isinstance(my_info, dict):
                                    bot_display_name = my_info.get('display_name') or my_info.get('name')
                            except Exception:
                                pass

                            if not bot_display_name:
                                from app.services.config_service import get_setting
                                bot_display_name = get_setting("WECHAT_BOT_NAME", "刘局")

                            return bot_display_name

                        def _get_plugin_display_name(self) -> str:
                            plugin_base = (self._plugin_name or "").rsplit("/", 1)[-1]
                            if self._owner_kind == "core":
                                return self._display_name or self._get_bot_display_name()

                            try:
                                config_path = Path("app/plugins") / self._plugin_name / "config.json"
                                with open(config_path, "r", encoding="utf-8") as f:
                                    config = json.load(f)

                                display_name = (
                                    config.get("display_name")
                                    or config.get("name")
                                    or plugin_base
                                )
                                if display_name:
                                    return str(display_name)
                            except Exception:
                                pass

                            return plugin_base or "plugin"

                        def _save_bot_response(self, chat_name: str, message: str, sender_name: Optional[str] = None):
                            """自动保存插件发出的消息到chat_log"""
                            try:
                                from app.plugins.builtin_chat_logger.main import chat_logger_plugin
                                if chat_logger_plugin:
                                    log_sender = sender_name or self._get_plugin_display_name()
                                    chat_logger_plugin.save_bot_response(chat_name, log_sender, message)
                            except Exception as e:
                                # 记录失败不应影响主流程,静默处理
                                pass

                        def send_message(self, *args, **kwargs):
                            # 支持 silent/skip_log 参数跳过自动记录
                            silent = kwargs.pop('silent', False) or kwargs.pop('skip_log', False)
                            log_sender = (
                                kwargs.pop('log_sender', None)
                                or kwargs.pop('log_name', None)
                                or kwargs.pop('log_speaker', None)
                            )
                            result_inner = getattr(self._wx, 'send_message')(*args, **kwargs)
                            if result_inner and not silent:  # 发送成功且未设置静音
                                self._mark_consumed()
                                # 自动记录插件回复
                                if len(args) >= 2:
                                    self._save_bot_response(args[0], args[1], log_sender)
                                elif 'chat_name' in kwargs and 'message' in kwargs:
                                    self._save_bot_response(kwargs['chat_name'], kwargs['message'], log_sender)
                            elif result_inner and silent:
                                # 静音模式下只标记已消费，不记录日志
                                self._mark_consumed()
                            return result_inner

                        def quote_message(self, *args, **kwargs):
                            silent = kwargs.pop('silent', False) or kwargs.pop('skip_log', False)
                            log_sender = (
                                kwargs.pop('log_sender', None)
                                or kwargs.pop('log_name', None)
                                or kwargs.pop('log_speaker', None)
                            )
                            result_inner = getattr(self._wx, 'quote_message')(*args, **kwargs)
                            if result_inner:
                                self._mark_consumed()
                                if not silent:
                                    if len(args) >= 3:
                                        self._save_bot_response(args[0], args[2], log_sender)
                                    elif 'chat_name' in kwargs and 'message' in kwargs:
                                        self._save_bot_response(
                                            kwargs['chat_name'],
                                            kwargs['message'],
                                            log_sender,
                                        )
                            return result_inner

                        def send_files(self, *args, **kwargs):
                            kwargs.pop('silent', False)
                            kwargs.pop('skip_log', False)
                            kwargs.pop('log_sender', None)
                            kwargs.pop('log_name', None)
                            kwargs.pop('log_speaker', None)
                            result_inner = getattr(self._wx, 'send_files')(*args, **kwargs)
                            if result_inner:
                                self._mark_consumed()
                            return result_inner

                        def send_url_card(self, *args, **kwargs):
                            silent = kwargs.pop('silent', False) or kwargs.pop('skip_log', False)
                            log_sender = (
                                kwargs.pop('log_sender', None)
                                or kwargs.pop('log_name', None)
                                or kwargs.pop('log_speaker', None)
                            )
                            result_inner = getattr(self._wx, 'send_url_card')(*args, **kwargs)
                            if result_inner and not silent:
                                self._mark_consumed()
                                # URL卡片发送也记录
                                if len(args) >= 1:
                                    chat_name = args[0]
                                    # 尝试提取标题作为记录内容
                                    title = args[2] if len(args) >= 3 else kwargs.get('title', '[链接卡片]')
                                    self._save_bot_response(chat_name, f"[链接] {title}", log_sender)
                                elif 'chat_name' in kwargs:
                                    chat_name = kwargs.get('chat_name')
                                    title = kwargs.get('title', '[链接卡片]')
                                    if chat_name:
                                        self._save_bot_response(chat_name, f"[链接] {title}", log_sender)
                            elif result_inner and silent:
                                self._mark_consumed()
                            return result_inner

                        def __getattr__(self, name):
                            return getattr(self._wx, name)

                    try:
                        event.context['wx'] = _WxConsumeProxy(
                            original_wx,
                            event,
                            listener.plugin_name,
                            listener.owner_kind,
                            listener.display_name,
                        )
                        proxy_installed = True
                    except Exception:
                        proxy_installed = False

                try:
                    result = listener.handler(event)
                finally:
                    if proxy_installed:
                        try:
                            event.context['wx'] = original_wx
                        except Exception:
                            pass
                self._stats['events_processed'] += 1
            except Exception as e:
                self.logger.error(f"Error in event handler {listener.plugin_name}: {e}")
                result = None
            # 仅当 Manifest 声明 stop_on_consumed，且处理器报告已消费时停止传播。
            if listener.propagation == "stop_on_consumed":
                consumed = False
                try:
                    if isinstance(result, bool):
                        consumed = result
                    elif isinstance(result, dict) and result.get('consumed') is True:
                        consumed = True
                    elif isinstance(getattr(event, 'data', {}), dict) and event.data.get('_consumed') is True:
                        consumed = True
                    elif isinstance(getattr(event, 'context', {}), dict) and event.context.get('_consumed') is True:
                        consumed = True
                except Exception:
                    consumed = False

                if consumed:
                    self.logger.debug(
                        f"Propagation stopped by plugin '{listener.id}' after handling {event.type.value}"
                    )
                    break

    async def publish_async(self, event: Event) -> None:
        """发布事件（异步）"""
        await self._event_queue.put(event)

    async def _process_events(self) -> None:
        """异步事件处理循环"""
        while self._running:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
                # 使用线程池执行同步的 publish 方法，避免阻塞主循环
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.publish, event)
                self._event_queue.task_done()
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                self.logger.error(f"Error processing event: {e}")

    async def start(self) -> None:
        """启动事件总线"""
        if self._running:
            return

        self._running = True
        self._processor_task = asyncio.create_task(
            self._process_events(), name="event-bus-processor"
        )
        self.logger.info("Event bus started")

        # 发布系统启动事件
        startup_event = Event(
            type=EventType.SYSTEM_STARTUP,
            source="event_bus",
            data={"timestamp": __import__('time').time()}
        )
        self.publish(startup_event)

    async def stop(self) -> None:
        """停止事件总线"""
        if not self._running:
            return

        # 发布系统关闭事件（同步处理）
        shutdown_event = Event(
            type=EventType.SYSTEM_SHUTDOWN,
            source="event_bus",
            data={"timestamp": __import__('time').time()}
        )
        self.publish(shutdown_event)

        # 等待所有异步入队的事件被处理完成
        await self._event_queue.join()

        # Wait for all user queues to finish processing
        self.logger.info("Waiting for user workers to finish...")
        with self._lock:
            user_queues_copy = list(self._user_queues.items())

        for chat_name, user_queue in user_queues_copy:
            try:
                user_queue.join()  # Wait for queue to be empty
                self.logger.debug(f"User queue for '{chat_name}' finished")
            except Exception as e:
                self.logger.error(f"Error waiting for user queue '{chat_name}': {e}")

        # 标记停止并等待拥有的协程退出，避免进程关闭时留下 pending task。
        self._running = False
        processor_task = self._processor_task
        self._processor_task = None
        if processor_task is not None:
            try:
                await asyncio.wait_for(processor_task, timeout=2.0)
            except asyncio.TimeoutError:
                processor_task.cancel()
                await asyncio.gather(processor_task, return_exceptions=True)

        # Cleanup all user workers
        with self._lock:
            remaining_users = list(self._user_workers.keys())

        for chat_name in remaining_users:
            self._cleanup_user_worker(chat_name)

        self.logger.info("Event bus stopped")

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        with self._lock:
            user_queue_sizes = {
                chat_name: q.qsize()
                for chat_name, q in self._user_queues.items()
            }
            active_workers = len(self._user_workers)

        return {
            **self._stats,
            'listeners_by_type': {
                event_type.value: len(listeners)
                for event_type, listeners in self._listeners.items()
            },
            'active_user_workers': active_workers,
            'user_queue_sizes': user_queue_sizes,
            'max_concurrent_users': self._max_concurrent_users
        }

    def get_listeners(self, event_type: Optional[EventType] = None) -> Dict[str, List[Dict[str, Any]]]:
        """获取监听器信息"""
        if event_type:
            listeners = self._listeners.get(event_type, [])
            return {
                event_type.value: [
                    {
                        'id': l.id,
                        'plugin_name': l.plugin_name,
                        'event_type': l.event_type.value,
                        'order_index': l.order_index,
                        'enabled': l.enabled,
                        'propagation': l.propagation,
                        'listener_key': l.listener_key,
                        'handler_name': l.handler_name,
                        'order_source': l.order_source,
                        'trigger_spec': dict(l.trigger_spec),
                        'owner_kind': l.owner_kind,
                        'permission_key': l.permission_key,
                        'display_name': l.display_name,
                    } for l in listeners
                ]
            }

        return {
            event_type.value: [
                {
                    'id': l.id,
                    'plugin_name': l.plugin_name,
                    'event_type': l.event_type.value,
                    'order_index': l.order_index,
                    'enabled': l.enabled,
                    'propagation': l.propagation,
                    'listener_key': l.listener_key,
                    'handler_name': l.handler_name,
                    'order_source': l.order_source,
                    'trigger_spec': dict(l.trigger_spec),
                    'owner_kind': l.owner_kind,
                    'permission_key': l.permission_key,
                    'display_name': l.display_name,
                } for l in listeners
            ]
            for event_type, listeners in self._listeners.items()
        }

    def enable_listener(self, listener_id: str) -> bool:
        """启用监听器"""
        with self._lock:
            for listeners in self._listeners.values():
                for listener in listeners:
                    if listener.id == listener_id:
                        listener.enabled = True
                        return True
            return False

    def disable_listener(self, listener_id: str) -> bool:
        """禁用监听器"""
        with self._lock:
            for listeners in self._listeners.values():
                for listener in listeners:
                    if listener.id == listener_id:
                        listener.enabled = False
                        return True
            return False

    def update_event_order(self, event_type: EventType, listener_keys: List[str]) -> bool:
        """Atomically apply the complete visible order for one event type."""
        with self._lock:
            listeners = self._listeners.get(event_type, [])
            reorderable = [listener for listener in listeners if listener.owner_kind == "plugin"]
            by_key = {listener.listener_key: listener for listener in reorderable}
            if len(by_key) != len(reorderable) or set(listener_keys) != set(by_key):
                return False
            for index, key in enumerate(listener_keys):
                by_key[key].order_index = index
                by_key[key].order_source = "routing_order"
            listeners.sort(
                key=lambda item: (
                    item.owner_kind == "core",
                    item.order_index,
                    item.listener_key,
                )
            )
            return True

    def get_listener_info(self, listener_id: str) -> Optional[Dict[str, Any]]:
        """获取监听器信息"""
        with self._lock:
            for event_type, listeners in self._listeners.items():
                for listener in listeners:
                    if listener.id == listener_id:
                        return {
                            "id": listener.id,
                            "plugin_name": listener.plugin_name,
                            "event_type": event_type,
                            "order_index": listener.order_index,
                            "enabled": listener.enabled,
                            "propagation": listener.propagation,
                            "listener_key": listener.listener_key,
                            "handler_name": listener.handler_name,
                            "order_source": listener.order_source,
                            "trigger_spec": dict(listener.trigger_spec),
                            "owner_kind": listener.owner_kind,
                            "permission_key": listener.permission_key,
                            "display_name": listener.display_name,
                        }
            return None

    # ──────────────────────────────────────────────
    # Session-Based Permission Elevation (会话期权限提升)
    # ──────────────────────────────────────────────

    def request_session_permission(self, chat_name: str, plugin_name: str, duration: int) -> None:
        """
        申请“会话模式”权限：允许指定用户在一段时间内无需@就能触发指定插件。
        适用于多轮对话场景（如游戏、向导、数据收集）。

        Args:
            chat_name: 用户标识 (WeChat Chat Name)
            plugin_name: 插件名称
            duration: 有效期（秒）
        """
        expiry = time.time() + duration

        with self._lock:
            # 懒加载初始化字典
            if not hasattr(self, '_session_permissions'):
                self._session_permissions: Dict[str, Dict[str, float]] = {}

            if chat_name not in self._session_permissions:
                self._session_permissions[chat_name] = {}

            self._session_permissions[chat_name][plugin_name] = expiry
            self.logger.debug(f"Granted session permission for user '{chat_name}' -> plugin '{plugin_name}' (duration: {duration}s)")

    def release_session_permission(self, chat_name: str, plugin_name: str) -> None:
        """
        结束“会话模式”：撤销临时权限。
        """
        with self._lock:
            if not hasattr(self, '_session_permissions'):
                return

            if chat_name in self._session_permissions:
                if plugin_name in self._session_permissions[chat_name]:
                    del self._session_permissions[chat_name][plugin_name]
                    self.logger.debug(f"Released session permission for user '{chat_name}' -> plugin '{plugin_name}'")

    def _check_session_permission(self, chat_name: str, plugin_name: str) -> bool:
        """检查是否存在有效的会话权限"""
        if not hasattr(self, '_session_permissions'):
            return False

        with self._lock:
            user_perms = self._session_permissions.get(chat_name)
            if not user_perms:
                return False

            expiry = user_perms.get(plugin_name)
            if not expiry:
                return False

            if time.time() < expiry:
                return True
            else:
                # 过期清理
                del user_perms[plugin_name]
                return False


# 全局事件总线实例
_event_bus_instance = None
_bus_lock = threading.Lock()

def get_event_bus(db_session_factory: Optional[Callable[[], Session]] = None) -> EventBus:
    """获取事件总线单例"""
    global _event_bus_instance
    if _event_bus_instance is None:
        with _bus_lock:
            if _event_bus_instance is None:
                if not db_session_factory:
                    raise ValueError("EventBus must be initialized with a db_session_factory")
                _event_bus_instance = EventBus(db_session_factory)
    return _event_bus_instance
