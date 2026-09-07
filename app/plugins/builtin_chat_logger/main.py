"""
聊天记录插件
专门负责无差别记录所有聊天消息
"""

import base64
import logging
import os
import time
from datetime import datetime
from app.core.event_bus import Event, EventType
from app.assistant.chat_log import ChatLogManager
from app.plugins.builtin_chat_logger.image_understanding import understand_image
from app.utils.plugin_config import get_config

logger = logging.getLogger(__name__)


class ChatLoggerPlugin:
    """聊天记录插件主类"""

    @staticmethod
    def _archive_fields(event):
        data = event.data
        fields = {"source_message_id": str(data.get("archive_message_id") or data.get("message_id") or event.id),
                  "source_id_namespace": str(data.get("archive_source") or ("wx_action" if data.get("message_id") else "event_bus"))}
        if data.get("timestamp"):
            fields["received_at"] = datetime.fromtimestamp(float(data["timestamp"])).strftime("%Y-%m-%d %H:%M:%S")
        return fields

    def handle_other_message(self, event):
        data = event.data
        kind = str(data.get("message_type") or event.type.value.removesuffix("_message_received"))
        if not data.get("chat_name"):
            return False
        self.chat_log_manager.save_message(
            data["chat_name"], str(data.get("sender") or "系统"),
            str(data.get("message") or f"[{kind}]"),
            sender_id=str(data.get("sender_id") or ""), message_type=kind,
            metadata={key: data[key] for key in ("file_id", "file_name", "file_path", "file_size", "file_sha256", "file_status") if data.get(key) is not None},
            **self._archive_fields(event))
        return False

    def __init__(self, context):
        self.context = context
        self.chat_log_manager = ChatLogManager()

        # 从插件配置读取设置
        plugin_name = "builtin_chat_logger"
        self.log_message_types = get_config("log_message_types", plugin_name=plugin_name)
        self.max_log_size_mb = get_config("max_log_size_mb", plugin_name=plugin_name)
        self.max_log_days = get_config("max_log_days", plugin_name=plugin_name)

        logger.info(f"📝 ChatLogger插件初始化完成 - max_days: {self.max_log_days}")

        # 启动后台清理线程
        self._cleanup_thread_started = False
        self._cleanup_thread = None

    def is_image_enrichment_enabled(self) -> bool:
        """图片内容补充属于聊天记录能力，配置变更后即时生效。"""
        value = get_config(
            "image_enrichment_enabled",
            True,
            plugin_name="builtin_chat_logger",
        )
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def describe_image_for_chat(self, image_base64: str):
        """为不支持视觉输入的聊天模型复用聊天记录图片补充能力。"""
        if not self.is_image_enrichment_enabled():
            return None
        return understand_image(image_base64)

    def start_cleanup_scheduler(self):
        """启动定期清理任务"""
        def _cleanup_runner():
            # 启动时先运行一次清理
            try:
                self.chat_log_manager.cleanup_logs(
                    max_days=self.max_log_days,
                    max_size_mb=self.max_log_size_mb
                )
            except Exception as e:
                logger.error(f"📝 首次清理任务异常: {e}")

            while self._cleanup_thread_started:
                try:
                    # 每 24 小时运行一次
                    # 为避免阻塞，我们可以分段 sleep
                    for _ in range(24 * 60): # 24小时 * 60分钟
                        if not self._cleanup_thread_started:
                            return
                        time.sleep(60)

                    self.chat_log_manager.cleanup_logs(
                        max_days=self.max_log_days,
                        max_size_mb=self.max_log_size_mb
                    )
                except Exception as e:
                    logger.error(f"📝 定期清理任务异常: {e}")

        if not self._cleanup_thread_started:
            self._cleanup_thread_started = True
            self._cleanup_thread = self.context.workers.start("log-cleanup", _cleanup_runner)
            logger.info("📝 ChatLogger 清理线程已启动")

    def stop_cleanup_scheduler(self):
        """停止定期清理任务"""
        self._cleanup_thread_started = False
        logger.info("📝 ChatLogger 清理线程正在停止...")

    def handle_text_message(self, event: Event):
        """处理文本消息事件，无差别记录"""
        try:
            if "text" not in self.log_message_types:
                return False

            # 获取事件数据
            content = event.data.get("message", "")
            chat_name = event.data.get("chat_name", "")
            sender = event.data.get("sender", "")
            sender_id = event.data.get("sender_id", "")
            sender_remark = event.data.get("sender_remark", "")

            if not content.strip() or not chat_name or not sender:
                logger.debug(f"📝 跳过无效消息: chat={chat_name}, sender={sender}")
                return False

            # 无差别保存所有文本消息
            self.chat_log_manager.save_message(
                chat_name,
                sender,
                content,
                sender_id=sender_id,
                sender_remark=sender_remark,
                message_type="text",
                **self._archive_fields(event),
            )
            logger.debug(f"📝 记录文本消息: {chat_name} - {sender}")

            # 记录型插件不消费事件
            return False
        except Exception as e:
            logger.error(f"📝 记录文本消息失败: {e}")
            return False

    def handle_image_message(self, event: Event):
        """处理图片消息事件"""
        try:
            if "image" not in self.log_message_types:
                return False

            chat_name = event.data.get("chat_name", "")
            sender = event.data.get("sender", "")
            sender_id = event.data.get("sender_id", "")
            sender_remark = event.data.get("sender_remark", "")

            if not chat_name or not sender:
                logger.debug(f"📝 跳过无效图片消息: chat={chat_name}, sender={sender}")
                return False

            enrichment_enabled = self.is_image_enrichment_enabled()
            message_id = event.data.get("message_id", "")

            # 先记录原始图片消息，再由后台任务原位补充其内容。这样不会制造
            # 一个名为 OCR 的伪发送者，也不会改变聊天消息的先后顺序。
            row_id = self.chat_log_manager.save_message(
                chat_name,
                sender,
                "[图片]",
                sender_id=sender_id,
                sender_remark=sender_remark,
                message_type="image",
                **self._archive_fields(event),
                image_enrichment={"status": "pending"} if enrichment_enabled else {"status": "disabled"},
            )
            logger.debug(f"📝 记录图片消息: {chat_name} - {sender}")

            if enrichment_enabled and row_id:
                data = dict(event.data or {})
                wx_manager = (event.context or {}).get("wx")
                self.context.workers.start(
                    f"image-enrichment-{chat_name}-{message_id or time.time_ns()}",
                    self._process_image_enrichment,
                    args=(data, wx_manager, row_id),
                )

            return False
        except Exception as e:
            logger.error(f"📝 记录图片消息失败: {e}")
            return False

    def _process_image_enrichment(self, data, wx_manager, row_id: str) -> None:
        """后台下载并理解图片，然后更新原始聊天记录行。"""
        chat_name = str(data.get("chat_name") or "")
        file_path = str(data.get("file_path") or "")
        message_id = data.get("message_id")
        try:
            if (not file_path or not os.path.exists(file_path)) and wx_manager and message_id:
                file_path = wx_manager.download_image_message(chat_name, message_id) or ""

            if not file_path or not os.path.exists(file_path):
                self.chat_log_manager.update_image_enrichment(
                    chat_name,
                    row_id,
                    status="failed",
                    error="图片文件不可用或下载失败",
                )
                logger.warning("📝 图片内容补充跳过，图片文件不可用: %s", message_id)
                return

            with open(file_path, "rb") as image_file:
                image_base64 = base64.b64encode(image_file.read()).decode("utf-8")

            description = understand_image(image_base64)
            if not description:
                self.chat_log_manager.update_image_enrichment(
                    chat_name,
                    row_id,
                    status="failed",
                    error="图片理解模型未返回有效内容",
                )
                return

            self.chat_log_manager.update_image_enrichment(
                chat_name,
                row_id,
                description=description,
                status="completed",
            )
            logger.info("📝 图片内容已补充到原聊天记录: %s", chat_name)
        except Exception as e:
            self.chat_log_manager.update_image_enrichment(
                chat_name,
                row_id,
                status="failed",
                error=str(e),
            )
            logger.error("📝 图片内容补充失败: %s", e)

    def handle_link_message(self, event: Event):
        """处理链接消息事件"""
        try:
            if "link" not in self.log_message_types:
                return False

            content = event.data.get("message", "")
            chat_name = event.data.get("chat_name", "")
            sender = event.data.get("sender", "")
            sender_id = event.data.get("sender_id", "")
            sender_remark = event.data.get("sender_remark", "")

            if not content.strip() or not chat_name or not sender:
                logger.debug(f"📝 跳过无效链接消息: chat={chat_name}, sender={sender}")
                return False

            # 记录链接消息
            self.chat_log_manager.save_message(
                chat_name,
                sender,
                f"[分享链接] {content}",
                sender_id=sender_id,
                sender_remark=sender_remark,
                message_type="link",
                **self._archive_fields(event),
            )
            logger.debug(f"📝 记录链接消息: {chat_name} - {sender}")

            return False
        except Exception as e:
            logger.error(f"📝 记录链接消息失败: {e}")
            return False

    def handle_quote_message(self, event: Event):
        """处理引用消息事件"""
        try:
            if "quote" not in self.log_message_types:
                return False

            content = event.data.get("message", "")
            quote_content = event.data.get("quote_content", "") or ""
            quote_nickname = str(event.data.get("quote_nickname") or "").strip()
            chat_name = event.data.get("chat_name", "")
            sender = event.data.get("sender", "")
            sender_id = event.data.get("sender_id", "")
            sender_remark = event.data.get("sender_remark", "")

            if not content.strip() or not chat_name or not sender:
                logger.debug(f"📝 跳过无效引用消息: chat={chat_name}, sender={sender}")
                return False

            # 记录引用消息：保留被引用文字片段，否则后续 LLM 上下文只会看到“[引用消息] 核实一下”
            quote_content = str(quote_content).strip()
            if quote_content and quote_content not in {"[图片]", "图片", "视频", "[视频]", "动画表情", "[动画表情]"}:
                log_content = f"[引用消息] 引用：{quote_content}\n回复：{content}"
            else:
                log_content = f"[引用消息] {content}"
            self.chat_log_manager.save_message(
                chat_name,
                sender,
                log_content,
                sender_id=sender_id,
                sender_remark=sender_remark,
                message_type="quote",
                metadata={"quote_nickname": quote_nickname, "quote_content": quote_content,
                          "reply_to": event.data.get("quote_message_id")},
                **self._archive_fields(event),
            )
            logger.debug(f"📝 记录引用消息: {chat_name} - {sender}")

            return False
        except Exception as e:
            logger.error(f"📝 记录引用消息失败: {e}")
            return False

    def save_bot_response(self, chat_name: str, bot_name: str, response: str):
        """保存机器人回复消息"""
        try:
            if not response.strip() or not chat_name or not bot_name:
                logger.debug(f"📝 跳过无效机器人回复: chat={chat_name}, bot={bot_name}")
                return

            # 记录机器人回复
            self.chat_log_manager.save_message(
                chat_name,
                bot_name,
                response,
                is_bot=True,
            )
            logger.debug(f"📝 记录机器人回复: {chat_name} - {bot_name}")

        except Exception as e:
            logger.error(f"📝 记录机器人回复失败: {e}")


# 全局实例
chat_logger_plugin = None


def get_chat_logger_plugin():
    """返回当前聊天记录插件实例。"""
    return chat_logger_plugin


def describe_image_for_chat(image_base64: str):
    """供聊天机器人在主模型不支持视觉时复用图片内容补充。"""
    if chat_logger_plugin:
        return chat_logger_plugin.describe_image_for_chat(image_base64)
    enabled = get_config(
        "image_enrichment_enabled",
        True,
        plugin_name="builtin_chat_logger",
    )
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return None
    return understand_image(image_base64)


def handle_text_message(event: Event):
    """处理文本消息事件"""
    global chat_logger_plugin
    if chat_logger_plugin:
        return chat_logger_plugin.handle_text_message(event)
    return False


def handle_image_message(event: Event):
    """处理图片消息事件"""
    global chat_logger_plugin
    if chat_logger_plugin:
        return chat_logger_plugin.handle_image_message(event)
    return False


def handle_link_message(event: Event):
    """处理链接消息事件"""
    global chat_logger_plugin
    if chat_logger_plugin:
        return chat_logger_plugin.handle_link_message(event)
    return False


def handle_quote_message(event: Event):
    """处理引用消息事件"""
    global chat_logger_plugin
    if chat_logger_plugin:
        return chat_logger_plugin.handle_quote_message(event)
    return False


def handle_other_message(event: Event):
    if chat_logger_plugin:
        return chat_logger_plugin.handle_other_message(event)
    return False


def register(event_bus, subscribe, context):
    """插件注册函数"""
    global chat_logger_plugin

    logger.info("📝 Registering ChatLogger plugin...")

    # 初始化聊天记录插件
    chat_logger_plugin = ChatLoggerPlugin(context)

    # 启动清理任务
    chat_logger_plugin.start_cleanup_scheduler()
    context.health.register(lambda: {
        "status": "healthy" if chat_logger_plugin is not None and chat_logger_plugin._cleanup_thread_started else "degraded",
        "message": "消息记录与清理调度器运行正常" if chat_logger_plugin is not None and chat_logger_plugin._cleanup_thread_started else "消息记录清理调度器未运行",
        "cleanup_scheduler_alive": bool(chat_logger_plugin and chat_logger_plugin._cleanup_thread and chat_logger_plugin._cleanup_thread.is_alive()),
    })
    context.register_cleanup(unregister)

    # 订阅所有消息事件，使用高优先级确保最先处理
    subscribe(
        event_type=EventType.TEXT_MESSAGE_RECEIVED,
        handler=handle_text_message
        # 不指定优先级，使用配置文件中的值
    )

    subscribe(
        event_type=EventType.IMAGE_MESSAGE_RECEIVED,
        handler=handle_image_message
        # 不指定优先级，使用配置文件中的值
    )

    subscribe(
        event_type=EventType.LINK_MESSAGE_RECEIVED,
        handler=handle_link_message
        # 不指定优先级，使用配置文件中的值
    )

    subscribe(
        event_type=EventType.QUOTE_MESSAGE_RECEIVED,
        handler=handle_quote_message
        # 不指定优先级，使用配置文件中的值
    )

    logger.info("✅ ChatLogger 插件注册成功")
    for event_type in (
        EventType.EMOTION_MESSAGE_RECEIVED, EventType.VOICE_MESSAGE_RECEIVED,
        EventType.VIDEO_MESSAGE_RECEIVED, EventType.FILE_MESSAGE_RECEIVED,
        EventType.LOCATION_MESSAGE_RECEIVED, EventType.MERGE_MESSAGE_RECEIVED,
        EventType.PERSONAL_CARD_MESSAGE_RECEIVED, EventType.NOTE_MESSAGE_RECEIVED,
        EventType.TICKLE_MESSAGE_RECEIVED, EventType.OTHER_MESSAGE_RECEIVED,
    ):
        subscribe(event_type=event_type, handler=handle_other_message)


def unregister():
    """取消注册插件"""
    global chat_logger_plugin

    logger.info("📝 Unregistering ChatLogger plugin...")
    if chat_logger_plugin:
        chat_logger_plugin.stop_cleanup_scheduler()
    chat_logger_plugin = None
    logger.info("ChatLogger plugin unregistered")
