"""Core Codex Assistant message handling and conversation orchestration."""

import json
import re
import base64
import copy
import os
import time
import logging
import threading
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime
from app.core.event_bus import Event, EventType
from app.services.config_service import get_setting
from app.services.codex_access_service import codex_access_service
from app.services.assistant_reply_gateway import (
    AssistantReplyError,
    CodexReplyRequest,
    get_assistant_reply_gateway,
)
from app.assistant.reply_completion import (
    is_serialized_reply_protocol,
    terminal_reply_output_schema,
    validate_plain_terminal_reply,
    validate_structured_terminal_reply,
)
from app.services.llm_manager import get_llm_manager
from app.assistant.chat_log import ChatLogManager
from app.assistant.context_manager import ChatContextManager
from app.assistant.memory_service import ChatMemoryService
from app.assistant.memory_config import (
    load_memory_config,
    memory_config_defaults,
    sanitize_memory_config,
    upgrade_memory_config_keys,
)
from app.assistant.role_manager import RoleManager
from app.assistant.judge_manager import JudgeManager
from app.utils.dashboard_events import append_dashboard_event
from app.utils.bot_mentions import (
    bot_names_for_user,
    bot_quote_names_for_user,
    find_bot_mention,
    strip_bot_mentions,
)
from app.utils.plugin_config import get_config
from app.utils.video_frames import extract_evenly_spaced_frames


logger = logging.getLogger(__name__)


_FOLLOWUP_POSITIVE_RELATIONS = frozenset(
    {
        "answer",
        "requested_update",
        "followup_question",
        "correction",
        "clarification",
        "elaboration",
    }
)
_FOLLOWUP_NEGATIVE_RELATIONS = frozenset(
    {
        "new_topic",
        "side_chat",
        "acknowledgement",
        "reaction",
        "already_answered",
        "unclear",
    }
)
_FOLLOWUP_RELATION_ALIASES = {
    "direct_answer": "answer",
    "response": "answer",
    "回答": "answer",
    "补充回答": "answer",
    "status_update": "requested_update",
    "result_update": "requested_update",
    "信息反馈": "requested_update",
    "结果反馈": "requested_update",
    "状态更新": "requested_update",
    "question": "followup_question",
    "追问": "followup_question",
    "纠正": "correction",
    "澄清": "clarification",
    "补充": "elaboration",
    "unrelated": "side_chat",
    "other": "side_chat",
    "旁聊": "side_chat",
    "新话题": "new_topic",
    "致谢": "acknowledgement",
    "附和": "acknowledgement",
    "表情": "reaction",
    "不明确": "unclear",
}

_QUOTED_BOT_POSITIVE_RELATIONS = frozenset(
    {
        "followup_question",
        "request",
        "correction",
        "clarification",
        "elaboration",
        "answer",
        "requested_update",
        "challenge",
    }
)
_QUOTED_BOT_NEGATIVE_RELATIONS = frozenset(
    {
        "side_chat",
        "new_topic",
        "acknowledgement",
        "reaction",
        "quote_only",
        "already_answered",
        "unclear",
    }
)
_QUOTED_BOT_RELATION_ALIASES = {
    **_FOLLOWUP_RELATION_ALIASES,
    "direct_question": "followup_question",
    "question_to_bot": "followup_question",
    "request_to_bot": "request",
    "command": "request",
    "请求": "request",
    "要求": "request",
    "challenge_to_bot": "challenge",
    "质疑": "challenge",
    "peer_chat": "side_chat",
    "talking_to_others": "side_chat",
    "quoted_side_chat": "side_chat",
    "和群友交谈": "side_chat",
    "quote_only": "quote_only",
    "仅引用": "quote_only",
}


class AssistantHandler:
    """Application-owned message handler for the first-class Assistant."""

    def __init__(self, context=None):
        self.runtime_context = context
        # 从插件配置中读取参数
        self.bot_name = get_setting("WECHAT_BOT_NAME", "刘局")  # 保留全局配置
        self.chat_log_manager = ChatLogManager()
        self.context_manager = ChatContextManager()
        self.memory_service = ChatMemoryService(
            self.chat_log_manager,
            self.context_manager,
        )
        self.role_manager = RoleManager()
        self.judge_manager = JudgeManager()

        # 从插件自己的 config.json 读取配置
        component_name = "assistant"
        self.codex_persistent_session_enabled = bool(
            get_config("codex_persistent_session_enabled", True, plugin_name=component_name)
        )
        codex_effort = str(
            get_config("codex_reasoning_effort", "inherit", plugin_name=component_name) or "inherit"
        ).strip().lower()
        self.codex_reasoning_effort = (
            codex_effort
            if codex_effort in {"inherit", "minimal", "low", "medium", "high", "xhigh", "max"}
            else "inherit"
        )
        codex_summary = str(
            get_config("codex_reasoning_summary", "inherit", plugin_name=component_name) or "inherit"
        ).strip().lower()
        self.codex_reasoning_summary = (
            codex_summary
            if codex_summary in {"inherit", "none", "auto", "concise", "detailed"}
            else "inherit"
        )
        codex_search_mode = str(
            get_config("codex_web_search_mode", "inherit", plugin_name=component_name) or "inherit"
        ).strip().lower()
        self.codex_web_search_mode = (
            codex_search_mode
            if codex_search_mode in {"inherit", "disabled", "cached", "indexed", "live"}
            else "inherit"
        )
        self.codex_turn_timeout_seconds = max(
            0,
            min(3600, int(get_config("codex_turn_timeout_seconds", 0, plugin_name=component_name) or 0)),
        )
        self.codex_max_turns_per_thread = max(
            0,
            min(10000, int(get_config("codex_max_turns_per_thread", 0, plugin_name=component_name) or 0)),
        )
        self.codex_exec_fallback_enabled = bool(
            get_config("codex_exec_fallback_enabled", True, plugin_name=component_name)
        )
        self.context_limit = int(get_config("context_limit", 30, plugin_name=component_name))
        self.max_context_tokens = int(get_config("max_context_tokens", 220000, plugin_name=component_name))
        self.context_window_auto_detect = bool(
            get_config("context_window_auto_detect", True, plugin_name=component_name)
        )
        self.context_safety_margin_tokens = int(
            get_config("context_safety_margin_tokens", 24576, plugin_name=component_name)
        )
        self.reserved_output_tokens = int(get_config("reserved_output_tokens", 8192, plugin_name=component_name))
        self.context_message_fetch_limit = int(get_config("context_message_fetch_limit", 300, plugin_name=component_name))
        self.context_window_strategy = str(get_config("context_window_strategy", "anchored_append", plugin_name=component_name) or "anchored_append")
        self.anchor_message_count = int(get_config("anchor_message_count", 300, plugin_name=component_name))
        self.anchor_rollover_prompt_tokens = int(get_config("anchor_rollover_prompt_tokens", 205000, plugin_name=component_name))
        self.memory_context_ratio = float(get_config("memory_context_ratio", 0.10, plugin_name=component_name))
        self.recent_context_ratio = float(get_config("recent_context_ratio", 0.35, plugin_name=component_name))
        self.ephemeral_context_ratio = float(get_config("ephemeral_context_ratio", 0.10, plugin_name=component_name))
        self.ephemeral_context_max_tokens = int(
            get_config("ephemeral_context_max_tokens", 16000, plugin_name=component_name)
        )
        memory_values = load_memory_config(
            lambda key, default: get_config(
                key, default, plugin_name=component_name
            )
        )
        for key, value in memory_values.items():
            setattr(self, key, value)
        self.default_role = get_config("default_role", "default", plugin_name=component_name)
        self.search_enabled = get_config("search_enabled", True, plugin_name=component_name)

        # 群聊触发配置。主动回复是否启用以及触发/冷却参数分别由聊天权限和 Judge 管理。
        self.allow_mention_trigger = get_config("allow_mention_trigger", True, plugin_name=component_name)

        logger.info(
            f"🔧 ChatBot group trigger config: allow_mention_trigger={self.allow_mention_trigger}; "
            "proactive state is managed per chat and timing is managed by Judge"
        )

        # Processing locks to prevent concurrent proactive replies
        self._processing_locks = {}  # {chat_name: timestamp}
        self._lock_timeout = 30  # seconds

        # Judge cooldown tracking - prevents excessive API calls after rejections
        self._judge_cooldowns = {}  # {chat::judge: {'time': timestamp, 'msg_count': int, 'total_count': int}}

        # Anchored append context cache: keep the exact dynamic message prefix
        # sent to the LLM so later calls can append to it and reuse prefix caches.
        self._anchored_contexts: Dict[str, Dict[str, Any]] = {}
        if context is not None:
            migration_notes = context.storage.migrate_legacy_directory(
                Path("data/chatbot_anchor_contexts"),
                storage_class="persistent",
                relative="anchor_contexts",
            )
            self._anchored_context_dir = context.storage.persistent_root / "anchor_contexts"
            if migration_notes:
                context.audit.record(
                    "storage_migration",
                    summary="聊天锚点上下文已迁移到插件标准存储目录",
                    details={"moved_files": len(migration_notes)},
                )
        else:
            self._anchored_context_dir = Path("data/chatbot_anchor_contexts")
        self._anchored_context_dir.mkdir(parents=True, exist_ok=True)

        # 注意：enabled_chats 权限检查已移至 EventBus 统一管理

        # 消息去重缓存，防止由于上游系统重复发布事件导致的多重回复
        self._message_dedup_cache = {}  # {(chat_name, sender, content_hash): timestamp}
        self._dedup_window = 3600.0  # 1小时去重窗口，防止重读历史消息导致重复回复

        # Per-chat follow-up sessions. Judge calls run outside the EventBus
        # worker; approved replies are published back into the same chat queue.
        self.event_bus = None
        self._followup_lock = threading.RLock()
        self._followup_sessions: Dict[str, Dict[str, Any]] = {}
        self._followup_executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="ChatBot-Followup",
        )
        self._followup_closed = False

        # Resume durable person extraction/projection queues even when a quiet
        # chat does not receive another message.  Normal event and stage due
        # thresholds are unchanged and still enforced by ChatMemoryService.
        self.memory_service.start_automatic_maintenance(
            self.chat_log_manager.get_chat_list,
            self._get_chat_memory_config,
            poll_minutes=max(
                1,
                int(getattr(self, "memory_automation_poll_minutes", 15) or 15),
            ),
        )

        logger.info(
            "Assistant 初始化完成 - bot_name=%s codex_persistent=%s effort=%s search=%s",
            self.bot_name,
            self.codex_persistent_session_enabled,
            self.codex_reasoning_effort,
            self.codex_web_search_mode,
        )

    def _is_search_enabled(self) -> bool:
        """动态读取网络搜索开关，使 Web 端配置修改无需重启插件即可生效。"""
        value = get_config("search_enabled", self.search_enabled, plugin_name="assistant")
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _bot_names_for_chat(self, chat_name: str) -> List[str]:
        """返回群级昵称别名；事件总线正常会预先完成匹配。"""
        try:
            from app.models.base import SessionLocal
            from app.models.user_permission import WeChatUser

            with SessionLocal() as db:
                user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat_name).first()
                if user:
                    return bot_names_for_user(user, self.bot_name)
        except Exception as exc:
            logger.debug("🤖 读取群内机器人别名失败: %s", exc)
        return [self.bot_name]

    def _bot_quote_names_for_chat(self, chat_name: str) -> List[str]:
        """Return names that can authoritatively identify a quoted bot row.

        Mention matching deliberately accepts historical aliases. Quote
        attribution is stricter: it uses the current auto-detected group
        nickname, the manual fallback and the account name, but never old
        aliases.
        """
        try:
            from app.models.base import SessionLocal
            from app.models.user_permission import WeChatUser

            with SessionLocal() as db:
                user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat_name).first()
                if user:
                    return bot_quote_names_for_user(user, self.bot_name)
        except Exception as exc:
            logger.debug("🤖 读取引用消息机器人身份失败: %s", exc)
        return [self.bot_name]

    @staticmethod
    def _normalize_quote_match_text(value: Any) -> str:
        return " ".join(str(value or "").replace("\r", "\n").split()).strip()

    def _quoted_text_matches_recent_bot_reply(self, chat_name: str, quote_content: str) -> bool:
        """Fallback for older ingress payloads that omit ``quote_nickname``."""
        quote = self._normalize_quote_match_text(quote_content)
        if not quote:
            return False

        truncated = quote.endswith("…") or quote.endswith("...")
        if quote.endswith("…"):
            prefix = quote[:-1].rstrip()
        elif quote.endswith("..."):
            prefix = quote[:-3].rstrip()
        else:
            prefix = quote
        try:
            messages = self.chat_log_manager.get_context_messages(chat_name, 60)
        except Exception as exc:
            logger.debug("🤖 读取近期机器人消息用于引用匹配失败: %s", exc)
            return False

        for message in reversed(messages):
            if not message.get("is_bot"):
                continue
            bot_text = self._normalize_quote_match_text(message.get("content"))
            if quote == bot_text:
                return True
            if truncated and len(prefix) >= 8 and bot_text.startswith(prefix):
                return True
        return False

    def _event_quotes_bot(self, event: Event) -> bool:
        """Return whether a text quote is attributable to this bot account."""
        quote_content = str(event.data.get("quote_content") or "").strip()
        if not quote_content or quote_content in {
            "[图片]",
            "图片",
            "视频",
            "[视频]",
            "动画表情",
            "[动画表情]",
        }:
            return False

        quote_nickname = str(event.data.get("quote_nickname") or "").strip().lstrip("@").strip()
        chat_name = str(event.data.get("chat_name") or "")
        if quote_nickname:
            return quote_nickname in self._bot_quote_names_for_chat(chat_name)
        return self._quoted_text_matches_recent_bot_reply(chat_name, quote_content)

    def _bot_display_name_for_chat(self, chat_name: str) -> str:
        names = self._bot_names_for_chat(chat_name)
        return names[0] if names else self.bot_name

    def _event_mentions_bot(self, event: Event) -> bool:
        if "bot_mentioned" in event.data:
            return bool(event.data.get("bot_mentioned"))
        return find_bot_mention(
            event.data.get("message", ""),
            self._bot_names_for_chat(str(event.data.get("chat_name") or "")),
        ) is not None

    def _ingress_mentions_bot(self, chat_name: str, ingress: Dict[str, Any]) -> bool:
        if "bot_mentioned" in ingress:
            return bool(ingress.get("bot_mentioned"))
        return find_bot_mention(
            ingress.get("message", ""),
            self._bot_names_for_chat(chat_name),
        ) is not None

    def reload_roles(self):
        """重新加载角色配置"""
        logger.info("🔄 Assistant 正在重新加载角色配置...")
        self.role_manager.reload_roles()
        logger.info("✅ Assistant 角色配置重新加载完成")

    def reload_judges(self):
        """重新加载 Judge 配置"""
        logger.info("🔄 Assistant 正在重新加载 Judge 配置...")
        self.judge_manager.reload_judges()
        logger.info("✅ Assistant Judge 配置重新加载完成")

    def handle_text_message(self, event: Event):
        """处理文本消息事件"""
        # Must exist before every early-return branch because the outer finally
        # always inspects it. Duplicate-event exits previously raised an
        # UnboundLocalError after successfully deciding to ignore the message.
        proactive_processing_acquired = False
        try:
            logger.info("🤖 Assistant received text message event")

            # 获取事件数据，保持与其他插件一致的数据结构
            content = event.data.get("message", "")
            chat_name = event.data.get("chat_name", "")
            sender = event.data.get("sender", "")
            chat_type = event.data.get("chat_type", "private")
            quote_content = event.data.get("quote_content", "") or ""
            has_quote_file = bool(event.data.get("has_quote_file"))
            quoted_file_status = str(event.data.get("quoted_file_status") or "").strip().lower()
            quoted_file_path = str(event.data.get("quoted_file_path") or "").strip()
            quoted_file_name = str(
                event.data.get("quoted_file_name") or quote_content or ""
            ).strip()
            followup_approved = bool(event.data.get("_followup_approved"))
            if has_quote_file:
                llm_content = self._build_quote_file_augmented_content(
                    content,
                    quoted_file_name,
                )
            else:
                llm_content = self._build_quote_augmented_content(content, quote_content)
            if llm_content != content:
                logger.info(
                    "🤖 Quote text context injected for %s: content='%s', quote='%s'",
                    chat_name,
                    str(content)[:50],
                    str(quote_content)[:120],
                )

            if self._is_sender_ignored(chat_name, sender):
                logger.info(f"🤖 Ignored blacklisted sender: chat={chat_name}, sender={sender}")
                return False

            if not followup_approved:
                # ---- 消息去重逻辑 ----
                import hashlib
                message_id = str(event.data.get("message_id") or "").strip()
                fingerprint_source = message_id or f"{content}|{quote_content}"
                content_hash = hashlib.md5(fingerprint_source.encode('utf-8')).hexdigest()
                dedup_key = (chat_name, sender, content_hash)
                now = time.time()
                if dedup_key in self._message_dedup_cache:
                    if now - self._message_dedup_cache[dedup_key] < self._dedup_window:
                        logger.warning(f"⚠️ 检测到重复消息事件，跳过处理: chat={chat_name}, sender={sender}, content='{content[:30]}...'")
                        return False
                self._message_dedup_cache[dedup_key] = now
                # 清理过期缓存
                expired_keys = [k for k, t in self._message_dedup_cache.items() if now - t > self._dedup_window * 2]
                for k in expired_keys:
                    del self._message_dedup_cache[k]
                # --------------------

            # Every accepted message may advance the asynchronous event-memory
            # cursor, even when the proactive judge later decides not to reply.
            memory_config = self._get_chat_memory_config(chat_name)
            if not followup_approved:
                self.memory_service.schedule(chat_name, memory_config)

            # 初始化变量，防止在finally块中访问未定义变量
            is_mention = False
            quoted_bot_approved = False

            # ✨ 检测是否为误识别的引用图片消息
            quote_detection = self._detect_misidentified_quote_image(content)
            if quote_detection:
                # 重构event.data，模拟引用图片消息
                logger.info(f"🔄 将误识别的text消息转发为quote_image事件处理")

                # 使用前缀作为实际消息内容
                event.data["message"] = quote_detection["prefix"] if quote_detection["prefix"] else content
                event.data["quote_content"] = "[图片]"
                event.data["has_quote_image"] = True

                # 直接调用引用图片处理方法
                return self.handle_quote_image_message(event)

            logger.info(f"🤖 Message from {sender} in {chat_name}: {content}")

            # 检查是否为@消息
            is_mention = self._event_mentions_bot(event)

            if has_quote_file and (quoted_file_status != "ready" or not quoted_file_path):
                if content and (
                    chat_type in {"private", "friend", "user"}
                    or is_mention
                ):
                    wx_manager = event.context.get("wx")
                    if wx_manager:
                        wx_manager.send_message(
                            chat_name,
                            "⚠️ 没有找到这条引用对应的可用文件，请重新发送文件后再引用一次",
                            silent=True,
                        )
                logger.warning(
                    "🤖 Quoted file unavailable: chat=%s name=%s status=%s",
                    chat_name,
                    quoted_file_name,
                    quoted_file_status or "unknown",
                )
                return False

            if followup_approved:
                if not self._followup_approval_is_current(event):
                    logger.info(
                        "🔗 Follow-up approval became stale before reply generation: %s",
                        chat_name,
                    )
                    return False
            elif is_mention:
                # Explicit triggers always own the turn. Cancelling here plus
                # ingress-sequence checks prevents an in-flight Judge from
                # producing a second reply.
                self._cancel_followup_pending(chat_name, reason="explicit_mention")
            elif chat_type == "group" and self._event_quotes_bot(event):
                # Quoting the bot is a directed signal, but not every quote asks
                # for a response.  A narrow semantic Judge distinguishes a real
                # question/request/correction from quoting the bot while talking
                # to other group members.
                role_name = self._get_user_role(chat_name)
                if not self._consult_quoted_bot_reply(event, role_name):
                    return False
                self._cancel_followup_pending(chat_name, reason="quoted_bot_message")
                quoted_bot_approved = True
                logger.info("📎 Quoted-bot reply trigger approved for %s", chat_name)
            elif chat_type == "group" and self._schedule_followup_candidate(
                event,
                llm_content,
            ):
                return False

            # 群聊触发策略：
            # 1) @开启 + 命中@：被动直通回复（不走Judge）
            # 2) 其余群聊场景：走主动Judge链路（包含 @关闭 时的@消息）
            should_use_group_judge = (
                chat_type == "group"
                and not followup_approved
                and not quoted_bot_approved
                and (not is_mention or not self.allow_mention_trigger)
            )
            if should_use_group_judge:
                # 主动回复仅由聊天级权限控制。
                if not self._check_proactive_permission(chat_name):
                    logger.debug(f"Proactive disabled for user {chat_name}")
                    return False

                # 2. 检查消息时效性 (避免回复历史消息/过久的消息)
                # Event timestamp is float seconds
                event_time = event.timestamp
                if time.time() - event_time > 3 * 60:  # 3分钟超时
                    logger.debug(f"⏳ Skipping proactive check for stale message (lag: {int(time.time() - event_time)}s)")
                    return False

                # 3. 获取用户角色与 Judge 配置。Judge 自带触发/冷却参数。
                role_name = self._get_user_role(chat_name)
                judge_name = self._get_user_judge(chat_name)
                if not judge_name:
                    logger.debug(f"⚖️ No judge binding for {chat_name}, proactive judge disabled")
                    return False

                judge_timing = self.judge_manager.get_judge_timing(judge_name)

                # 4. 分析聊天状态 (沉默时长 & 用于判断的上下文)
                scan_threshold = max(
                    judge_timing["trigger_msg_threshold"],
                    judge_timing["cooldown_msg_threshold"],
                )
                state = self._analyze_chat_state(chat_name, scan_threshold=scan_threshold)
                msg_count = state['msg_count']
                last_time = state['last_reply_time']

                # 5. 阈值检查
                trigger_msg_threshold = judge_timing["trigger_msg_threshold"]
                trigger_interval_minutes = judge_timing["trigger_interval_minutes"]
                if msg_count < trigger_msg_threshold:
                    logger.debug(f"🤫 Not enough messages for proactive Judge[{judge_name}]: {msg_count}/{trigger_msg_threshold}")
                    return False

                if last_time:
                    minutes_since = (datetime.now() - last_time).total_seconds() / 60
                    if minutes_since < trigger_interval_minutes:
                        logger.debug(f"🤫 Too soon for proactive Judge[{judge_name}]: {int(minutes_since)}/{trigger_interval_minutes} min")
                        return False

                # 6. 检查裁判冷却（防止频繁调用API）
                # 传入 last_time 以检查是否有其他插件（如Summary）在冷却期间插话
                if not self._check_judge_cooldown(chat_name, judge_name, msg_count, judge_timing, last_time):
                    return False

                # 7. 检查是否正在处理中（防止并发）
                if self._is_processing(chat_name):
                    logger.debug(f"🔒 Already processing proactive reply for {chat_name}, skipping")
                    return False

                # 8. 获取上下文消息 (用于提交给裁判)
                # 注意：这里需要足够的上下文给裁判看
                judge_context = self._get_judge_context_messages(chat_name, 20)

                # 9. 裁判机制 (DeepSeek)
                logger.info(f"⚖️ Consulting Judge '{judge_name}' for {chat_name} (msgs: {msg_count})")

                if not self._consult_judge(judge_context, role_name, judge_name):
                    # 裁判拒绝 -> 设置冷却
                    self._set_judge_cooldown(chat_name, judge_name, msg_count, judge_timing)
                    return False

                # 10. 设置处理锁
                self._set_processing(chat_name)
                proactive_processing_acquired = True

                # 裁判通过 -> 继续执行后续回复逻辑
                logger.info(f"📢 Proactive reply triggered for {chat_name}")



            # 记录开始时间
            start_time = time.time()

            # 基础内容检查 (如果是@消息或者已经通过了主动检查，都会走到这里)
            if not self._should_respond(content, chat_type):
                logger.info(f"🤖 Should not respond to message from {sender}")
                return False

            # 注意：权限检查已移至 EventBus 统一管理，此处不再检查 enabled_chats

            # 获取用户角色配置 (如果前面主动逻辑已获取，这里会重复但无害，或者优化下)
            role_name = self._get_user_role(chat_name)

            # 获取较长上下文，实际入模内容由 token 预算动态裁剪
            context_msgs = self.chat_log_manager.get_context_messages(
                chat_name,
                self._memory_source_fetch_limit(memory_config),
            )
            memory_context, memory_stats = self.memory_service.build_retrieval_context(
                chat_name,
                sender=sender,
                content=llm_content,
                recent_messages=context_msgs,
                config=memory_config,
            )

            # 构建消息数组（包含 system prompt 和变量替换）
            role_name = self._get_user_role(chat_name)
            messages = self._build_messages_array(
                chat_name,
                context_msgs,
                "",
                sender,
                llm_content,
                role_name,
                memory_config,
                memory_context=memory_context,
            )
            logger.info(
                "🧠 Retrieval context for %s: events=%s people=%s tokens≈%s vector=%s",
                chat_name,
                memory_stats.get("event_count"),
                memory_stats.get("people_count"),
                memory_stats.get("tokens", 0),
                memory_stats.get("vector_ready"),
            )

            verified_memory_trace = self._reconcile_memory_trace(
                memory_stats.get("trace"),
                messages,
            )

            if followup_approved and not self._followup_approval_is_current(event):
                logger.info(
                    "🔗 Follow-up approval became stale before main model call: %s",
                    chat_name,
                )
                self._discard_stale_followup_model_result(
                    chat_name,
                    invalidate_provider=False,
                )
                return False

            # 调用 LLM Manager
            response_attachments: List[Dict[str, Any]] = []
            input_files: List[Dict[str, Any]] = []
            if has_quote_file and quoted_file_status == "ready" and quoted_file_path:
                input_files.append(
                    {
                        "file_id": event.data.get("quoted_file_id"),
                        "name": quoted_file_name,
                        "path": quoted_file_path,
                        "size": event.data.get("quoted_file_size"),
                        "sha256": event.data.get("quoted_file_sha256"),
                    }
                )
            response = self._request_codex_reply(
                chat_name,
                role_name,
                messages,
                _mabobot_attachment_capture=response_attachments,
                _mabobot_input_files=input_files,
                _mabobot_memory_trace=verified_memory_trace,
            )
            if response_attachments:
                response = self._strip_internal_action_markers(response)

            display_suffix = ""

            # 发送回复
            wx_manager = event.context.get("wx")
            if wx_manager:
                # 方案优化: 为了不让 emoji (⚠️⛓️‍💥) 和 JSON 包装污染上下文
                has_response_text = bool(self._format_response_parts(response, self.role_manager.get_output_settings(role_name)))
                if followup_approved and not self._followup_approval_is_current(event):
                    logger.info(
                        "🔗 Discarding stale follow-up response before send: %s",
                        chat_name,
                    )
                    self._discard_stale_followup_model_result(chat_name)
                    return False
                if has_response_text:
                    result = self._send_response_parts(
                        wx_manager,
                        chat_name,
                        response,
                        role_name,
                        silent=True,
                        display_suffix=display_suffix,
                    )
                else:
                    result = bool(response_attachments)

                if result:
                    if response_attachments:
                        result = self._send_response_attachments(wx_manager, chat_name, response_attachments)
                    if not result:
                        logger.error("🤖 Failed to send Assistant attachment(s) to %s", chat_name)
                        return False
                    self._finalize_anchored_context(chat_name, response)
                    self._open_followup_window(
                        chat_name=chat_name,
                        chat_type=chat_type,
                        event=event,
                        context_messages=context_msgs,
                        response=response,
                        role_name=role_name,
                        automatic=followup_approved,
                    )
                    logger.info("🤖 Sent Assistant response to %s", chat_name)

                    # 记录 E2E 响应时间
                    duration = time.time() - start_time
                    try:
                        # 使用 "reply_latency" 作为 call_type, "system" 作为 model
                        get_llm_manager()._record_stats("assistant", "reply_latency", "system", None, duration)
                        logger.info(f"⏱️ E2E Reply Latency: {duration:.2f}s")
                    except Exception as e:
                        logger.warning(f"Failed to record latency: {e}")

                    # 清除裁判冷却（成功回复后重置）
                    self._clear_judge_cooldowns(chat_name)

                    return True
                else:
                    logger.error("🤖 Failed to send Assistant response to %s", chat_name)
            else:
                logger.error("🤖 WeChat manager not available")
            return False
        except Exception as e:
            logger.error(f"🤖 ChatBot处理文本消息失败: {e}")
            # 主动模式下出错不发送错误提示给用户，避免打扰
            if not self._event_mentions_bot(event):
                 return False

            wx_manager = event.context.get("wx")
            if wx_manager:
                wx_manager.send_message(chat_name, "⚠️ AI自动回复失败，请稍后再试～")
            return False
        finally:
            # 清除处理锁（仅本次事件曾拿到锁）
            if proactive_processing_acquired:
                self._clear_processing(chat_name)


    def _check_proactive_permission(self, chat_name: str) -> bool:
        """检查用户是否开启了主动回复权限"""
        try:
            from app.models.base import SessionLocal
            from app.models.assistant_policy import AssistantChatPolicy
            from app.models.user_permission import WeChatUser

            with SessionLocal() as db:
                user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat_name).first()
                if not user:
                    return False

                policy = db.query(AssistantChatPolicy).filter(
                    AssistantChatPolicy.user_id == user.id,
                    AssistantChatPolicy.enabled.is_(True),
                ).first()

                if policy and policy.proactive_enabled:
                    return True
            return False
        except Exception as e:
            logger.error(f"❌ Error checking proactive permission: {e}")
            return False

    def _get_followup_config(self, chat_name: str) -> Dict[str, Any]:
        permission = self._get_user_permission_config(chat_name)
        return {
            "enabled": bool(permission.get("followup_enabled", False)),
            "window_seconds": max(
                10,
                min(600, int(permission.get("followup_window_seconds") or 60)),
            ),
            "merge_seconds": max(
                1,
                min(30, int(permission.get("followup_merge_seconds") or 3)),
            ),
            "max_turns": max(
                1,
                min(10, int(permission.get("followup_max_turns") or 3)),
            ),
        }

    def _get_chat_ingress_state(self, chat_name: str) -> Dict[str, Any]:
        getter = getattr(self.event_bus, "get_chat_ingress_state", None)
        if not callable(getter):
            return {"sequence": 0}
        try:
            return getter(chat_name)
        except Exception as exc:
            logger.warning("🔗 Failed to read ingress state for %s: %s", chat_name, exc)
            return {"sequence": 0}

    def _cancel_followup_pending(self, chat_name: str, *, reason: str) -> None:
        with self._followup_lock:
            session = self._followup_sessions.pop(chat_name, None)
            if not session:
                return
            timer = session.get("timer")
            if timer:
                timer.cancel()
            session["generation"] = int(session.get("generation") or 0) + 1
        logger.debug("🔗 Follow-up window cancelled for %s: %s", chat_name, reason)

    def _open_followup_window(
        self,
        *,
        chat_name: str,
        chat_type: str,
        event: Event,
        context_messages: List[Dict[str, Any]],
        response: str,
        role_name: str,
        automatic: bool,
    ) -> None:
        if chat_type != "group":
            self._cancel_followup_pending(chat_name, reason="not_group")
            return

        config = self._get_followup_config(chat_name)
        if not config["enabled"]:
            self._cancel_followup_pending(chat_name, reason="disabled")
            return

        response_parts = self._format_response_parts(
            response,
            self.role_manager.get_output_settings(role_name),
        )
        reply_text = "\n".join(response_parts).strip()
        if not reply_text:
            self._cancel_followup_pending(chat_name, reason="empty_reply")
            return

        now = time.time()
        anchor_id = uuid.uuid4().hex
        automatic_turns = (
            int(event.data.get("_followup_auto_turns") or 0) + 1
            if automatic
            else 0
        )
        anchor_sender = str(event.data.get("sender") or "")
        anchor_content = self._build_quote_augmented_content(
            str(event.data.get("message") or ""),
            str(event.data.get("quote_content") or ""),
        )
        context_before = []
        for message in (context_messages or [])[-6:]:
            context_before.append(
                {
                    "sender": str(message.get("sender") or ""),
                    "content": str(message.get("content") or ""),
                    "time": str(message.get("time") or ""),
                }
            )

        with self._followup_lock:
            previous = self._followup_sessions.get(chat_name)
            if previous and previous.get("timer"):
                previous["timer"].cancel()
            self._followup_sessions[chat_name] = {
                "anchor_id": anchor_id,
                "reply_sent_at": now,
                "expires_at": now + config["window_seconds"],
                "anchor_sequence": int(event.data.get("_followup_snapshot_seq") or event.data.get("_chat_seq") or 0),
                "automatic_turns": automatic_turns,
                "role_name": role_name,
                "reply_text": reply_text,
                "context_before": context_before,
                "anchor_message": {
                    "sender": anchor_sender,
                    "content": anchor_content,
                    "time": str(event.data.get("time") or ""),
                },
                "window_seconds": config["window_seconds"],
                "merge_seconds": config["merge_seconds"],
                "max_turns": config["max_turns"],
                "pending_messages": [],
                "pending_first_at": 0.0,
                "generation": 0,
                "timer": None,
                "judge_inflight": False,
                "rerun_due": False,
                "judge_calls": 0,
                "last_judge_at": 0.0,
            }

        logger.info(
            "🔗 Follow-up window opened for %s: window=%ss merge=%ss turns=%s/%s",
            chat_name,
            config["window_seconds"],
            config["merge_seconds"],
            automatic_turns,
            config["max_turns"],
        )

    def _build_quoted_bot_judge_text(self, event: Event) -> str:
        """Build a focused addressee/intent prompt for a quote of the bot."""
        chat_name = str(event.data.get("chat_name") or "")
        sender = str(event.data.get("sender") or "未知")
        quote_nickname = str(event.data.get("quote_nickname") or self.bot_name)
        quote_content = str(event.data.get("quote_content") or "")
        content = str(event.data.get("message") or "")

        def clipped(value: Any, token_budget: int) -> str:
            return self.context_manager.truncate_text_to_budget(
                str(value or ""),
                token_budget,
                notice="该消息过长，已截断",
            )

        lines = [
            f"群聊：{chat_name}",
            f"机器人名称：{self.bot_name}",
            "",
            "邻近群聊上下文（只用于判断当前消息在对谁说话）：",
        ]
        for message in self._get_judge_context_messages(chat_name, 8):
            lines.append(
                f"[{message.get('sender') or '未知'}] "
                f"{clipped(message.get('content'), 100)}"
            )
        lines.extend(
            [
                "",
                "已确认当前用户通过微信引用功能引用了机器人本人发出的文字：",
                f"引用显示名：{quote_nickname}",
                f"引用原文：{clipped(quote_content, 240)}",
                "",
                "当前引用回复：",
                f"[{sender}] {clipped(content, 180)}",
                "",
                "引用机器人只说明用户选择了这段上下文，不自动等于要求机器人回答。",
                "判断当前回复真正的交流对象和意图：",
                "- followup_question：向机器人继续追问，包括“最新一代呢”“那现在呢”这类无问号的省略问句；",
                "- request：要求机器人查询、解释、执行或给出内容；",
                "- correction / clarification / challenge：纠正、澄清或质疑机器人，并期待机器人回应；",
                "- answer / requested_update / elaboration：回答机器人、反馈机器人要求的结果，或向机器人补充实质条件以继续讨论；",
                "- side_chat：只是借用机器人原话和其他群友交谈、@或点名其他人、替别人解释、群内泛评；",
                "- acknowledgement / reaction：纯感谢、收到、附和、笑声、表情或情绪反应；",
                "- quote_only：只有引用或复读，没有新增交流意图；",
                "- new_topic / unclear：另起话题或确实无法判断。",
                "relation 必须从以上分类中选择。只输出 JSON：",
                '{"relation":"分类","reason":"简短说明当前消息在对谁说、是否期待机器人回答"}',
            ]
        )
        return self.context_manager.truncate_text_to_budget(
            "\n".join(lines),
            2200,
            notice="机器人引用判定上下文达到预算上限",
        )

    def _normalize_quoted_bot_judge_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        raw_relation = str(
            result.get("relation")
            or result.get("dialogue_act")
            or result.get("message_relation")
            or ""
        ).strip().lower()
        relation = _QUOTED_BOT_RELATION_ALIASES.get(raw_relation, raw_relation)
        if relation in _QUOTED_BOT_POSITIVE_RELATIONS:
            should_reply = True
        elif relation in _QUOTED_BOT_NEGATIVE_RELATIONS:
            should_reply = False
        else:
            relation = "unclear"
            should_reply = False
        return {
            "should_reply": should_reply,
            "relation": relation,
            "reason": str(result.get("reason") or "No reason provided"),
        }

    def _consult_quoted_bot_reply(self, event: Event, role_name: str) -> bool:
        """Decide whether a quote of the bot is actually asking the bot to reply."""
        chat_name = str(event.data.get("chat_name") or "")
        decision = {
            "should_reply": False,
            "relation": "unclear",
            "reason": "invalid_json",
        }
        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是群聊引用回复意图分类器。系统已经确认被引用文字由机器人本人发送。"
                        "引用动作不是要求机器人回答的充分条件；必须判断当前消息是在继续问机器人，"
                        "还是只借用原话与其他群友交谈。只输出指定 JSON。"
                    ),
                },
                {"role": "user", "content": self._build_quoted_bot_judge_text(event)},
            ]
            raw = self._call_auxiliary_model(
                "followup_judge",
                messages,
                response_format={"type": "json_object"},
                _mabobot_chat_name=chat_name,
                _mabobot_role_name=role_name,
            )
            parsed = self._extract_first_json_object(raw)
            if isinstance(parsed, dict):
                decision = self._normalize_quoted_bot_judge_result(parsed)
        except Exception as exc:
            decision["reason"] = f"judge_error: {exc}"
            logger.warning("📎 Quoted-bot Judge failed for %s: %s", chat_name, exc)

        append_dashboard_event(
            "judge_decision",
            {
                "should_reply": decision["should_reply"],
                "reason": decision["reason"],
                "relation": decision["relation"],
                "atmosphere": "机器人引用判定",
                "role_name": role_name,
                "judge_name": "followup_judge",
                "chat_name": chat_name,
                "mode": "quoted_bot",
                "quote_nickname": str(event.data.get("quote_nickname") or ""),
            },
        )
        logger.info(
            "📎 Quoted-bot Judge for %s: reply=%s relation=%s reason=%s",
            chat_name,
            decision["should_reply"],
            decision["relation"],
            decision["reason"],
        )
        return bool(decision["should_reply"])

    def _schedule_followup_candidate(self, event: Event, content: str) -> bool:
        """Own an unmentioned group message when a reply-follow-up window is active."""
        chat_name = str(event.data.get("chat_name") or "")
        sender = str(event.data.get("sender") or "")
        if not chat_name or not sender or sender == self.bot_name or not str(content or "").strip():
            return False

        config = self._get_followup_config(chat_name)
        if not config["enabled"]:
            return False

        event_time = float(event.timestamp or time.time())
        event_sequence = int(event.data.get("_chat_seq") or 0)
        now = time.time()
        wx_manager = event.context.get("wx")
        wx_manager = getattr(wx_manager, "_wx", wx_manager)

        with self._followup_lock:
            session = self._followup_sessions.get(chat_name)
            if not session:
                return False
            if event_time > float(session.get("expires_at") or 0):
                self._followup_sessions.pop(chat_name, None)
                return False
            if event_time <= float(session.get("reply_sent_at") or 0):
                logger.debug(
                    "🔗 Ignoring pre-reply queued message for follow-up: %s seq=%s",
                    chat_name,
                    event_sequence,
                )
                return True
            if int(session.get("automatic_turns") or 0) >= config["max_turns"]:
                logger.info("🔗 Follow-up turn limit reached for %s", chat_name)
                return True

            session["window_seconds"] = config["window_seconds"]
            session["merge_seconds"] = config["merge_seconds"]
            session["max_turns"] = config["max_turns"]
            session["expires_at"] = min(
                float(session.get("expires_at") or now),
                float(session.get("reply_sent_at") or now) + config["window_seconds"],
            )
            session["generation"] = int(session.get("generation") or 0) + 1
            if not session.get("pending_messages"):
                session["pending_first_at"] = now
            session["pending_messages"].append(
                {
                    "sequence": event_sequence,
                    "event_timestamp": event_time,
                    "sender": sender,
                    "content": str(content),
                    "event_data": {
                        key: value
                        for key, value in event.data.items()
                        if key not in {"_consumed", "_followup_approved"}
                    },
                    "wx": wx_manager,
                }
            )
            session["pending_messages"] = session["pending_messages"][-8:]

            if int(session.get("judge_calls") or 0) >= 2:
                logger.debug("🔗 Follow-up Judge call cap reached for %s", chat_name)
                return True

            self._arm_followup_timer_locked(chat_name, session)

        logger.debug(
            "🔗 Follow-up candidate queued for %s: seq=%s merge=%ss",
            chat_name,
            event_sequence,
            config["merge_seconds"],
        )
        return True

    def _arm_followup_timer_locked(
        self,
        chat_name: str,
        session: Dict[str, Any],
    ) -> None:
        timer = session.get("timer")
        if timer:
            timer.cancel()
        if session.get("judge_inflight"):
            session["rerun_due"] = True
            session["timer"] = None
            return
        if not session.get("pending_messages"):
            session["timer"] = None
            return

        now = time.time()
        merge_seconds = float(session.get("merge_seconds") or 3)
        first_at = float(session.get("pending_first_at") or now)
        due_at = min(
            now + merge_seconds,
            first_at + merge_seconds * 2,
            float(session.get("expires_at") or (now + merge_seconds)),
        )
        last_judge_at = float(session.get("last_judge_at") or 0)
        if last_judge_at:
            due_at = min(
                max(due_at, last_judge_at + 8),
                float(session.get("expires_at") or due_at),
            )
        delay = max(0.05, due_at - now)
        generation = int(session.get("generation") or 0)
        anchor_id = str(session.get("anchor_id") or "")
        # Assistant is application-owned now and is intentionally constructed
        # without a PluginContext.  Own the short cancellable timer directly;
        # ``close()`` and every session replacement cancel the stored handle.
        timer = threading.Timer(
            delay,
            self._submit_followup_judge,
            args=(chat_name, anchor_id, generation),
        )
        timer.name = f"Assistant-Followup-{generation}-{time.time_ns()}"
        timer.daemon = True
        session["timer"] = timer
        try:
            timer.start()
        except Exception:
            session["timer"] = None
            raise

    def _submit_followup_judge(
        self,
        chat_name: str,
        anchor_id: str,
        generation: int,
    ) -> None:
        with self._followup_lock:
            if self._followup_closed:
                return
            session = self._followup_sessions.get(chat_name)
            if (
                not session
                or session.get("anchor_id") != anchor_id
                or int(session.get("generation") or 0) != generation
            ):
                return
            session["timer"] = None
            if session.get("judge_inflight"):
                session["rerun_due"] = True
                return
            if int(session.get("judge_calls") or 0) >= 2:
                return

            pending = list(session.get("pending_messages") or [])
            if not pending:
                return
            if float(pending[-1].get("event_timestamp") or 0) > float(
                session.get("expires_at") or 0
            ):
                session["pending_messages"] = []
                session["pending_first_at"] = 0.0
                return
            snapshot_sequence = int(pending[-1].get("sequence") or 0)
            ingress = self._get_chat_ingress_state(chat_name)
            if int(ingress.get("sequence") or 0) != snapshot_sequence:
                session["pending_messages"] = []
                session["pending_first_at"] = 0.0
                return
            if self._ingress_mentions_bot(chat_name, ingress):
                session["pending_messages"] = []
                session["pending_first_at"] = 0.0
                return

            session["pending_messages"] = []
            session["pending_first_at"] = 0.0
            session["judge_inflight"] = True
            session["rerun_due"] = False
            session["judge_calls"] = int(session.get("judge_calls") or 0) + 1
            session["last_judge_at"] = time.time()
            snapshot = {
                "anchor_id": anchor_id,
                "snapshot_sequence": snapshot_sequence,
                "role_name": str(session.get("role_name") or self.default_role),
                "context_before": list(session.get("context_before") or []),
                "anchor_message": dict(session.get("anchor_message") or {}),
                "reply_text": str(session.get("reply_text") or ""),
                "pending": pending,
                "automatic_turns": int(session.get("automatic_turns") or 0),
                "expires_at": float(session.get("expires_at") or 0),
            }

        try:
            self._followup_executor.submit(
                self._run_followup_judge,
                chat_name,
                snapshot,
            )
        except RuntimeError:
            with self._followup_lock:
                session = self._followup_sessions.get(chat_name)
                if session and session.get("anchor_id") == anchor_id:
                    session["judge_inflight"] = False

    def _run_followup_judge(
        self,
        chat_name: str,
        snapshot: Dict[str, Any],
    ) -> None:
        should_reply = False
        reason = ""
        relation = "unclear"
        target_index = 0
        try:
            judge_text = self._build_followup_judge_text(snapshot)
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是群聊连续对话的对话行为分类器。判断机器人回复后的候选消息中，"
                        "是否有消息在语义上承接当前人机话题。不要用是否有问号、是否再次@机器人，"
                        "或是否为陈述句来代替语义判断。只输出 JSON："
                        '{"relation":"分类","target_message_index":整数,'
                        '"reason":"简短原因"}。是否回复由 relation 唯一确定，不要另设决定字段。'
                    ),
                },
                {"role": "user", "content": judge_text},
            ]
            raw = self._call_auxiliary_model(
                "followup_judge",
                messages,
                response_format={"type": "json_object"},
                _mabobot_chat_name=chat_name,
                _mabobot_role_name=str(snapshot.get("role_name") or ""),
            )
            parsed = self._extract_first_json_object(raw)
            if isinstance(parsed, dict):
                decision = self._normalize_followup_judge_result(
                    parsed,
                    pending_count=len(snapshot.get("pending") or []),
                )
                should_reply = decision["should_reply"]
                target_index = decision["target_message_index"]
                relation = decision["relation"]
                reason = decision["reason"]
            else:
                reason = "invalid_json"
        except Exception as exc:
            reason = f"judge_error: {exc}"
            logger.warning("🔗 Follow-up Judge failed for %s: %s", chat_name, exc)

        append_dashboard_event(
            "judge_decision",
            {
                "should_reply": should_reply,
                "reason": reason,
                "relation": relation,
                "target_message_index": target_index,
                "atmosphere": "续聊判定",
                "role_name": str(snapshot.get("role_name") or ""),
                "judge_name": "followup_judge",
                "chat_name": chat_name,
                "mode": "followup",
            },
        )

        approved_event = None
        try:
            with self._followup_lock:
                session = self._followup_sessions.get(chat_name)
                if (
                    session
                    and session.get("anchor_id") == snapshot.get("anchor_id")
                ):
                    session["judge_inflight"] = False
                    ingress = self._get_chat_ingress_state(chat_name)
                    is_current = (
                        int(ingress.get("sequence") or 0)
                        == int(snapshot.get("snapshot_sequence") or 0)
                        and float(snapshot["pending"][-1].get("event_timestamp") or 0)
                        <= float(session.get("expires_at") or 0)
                        and not self._ingress_mentions_bot(chat_name, ingress)
                    )
                    if should_reply and is_current and snapshot.get("pending"):
                        target = snapshot["pending"][target_index - 1]
                        event_data = dict(target.get("event_data") or {})
                        event_data.update(
                            {
                                "_followup_approved": True,
                                "_followup_anchor_id": snapshot["anchor_id"],
                                "_followup_snapshot_seq": snapshot["snapshot_sequence"],
                                "_followup_auto_turns": snapshot["automatic_turns"],
                                "_followup_relation": relation,
                            }
                        )
                        approved_event = Event(
                            type=EventType.CHATBOT_FOLLOWUP_APPROVED,
                            source="assistant_followup",
                            data=event_data,
                            context={"wx": target.get("wx")},
                            timestamp=float(target.get("event_timestamp") or time.time()),
                        )
                    if session.get("pending_messages") and session.get("rerun_due"):
                        self._arm_followup_timer_locked(chat_name, session)
        finally:
            if approved_event is not None and self.event_bus is not None:
                self.event_bus.publish(approved_event)

    def _build_followup_judge_text(self, snapshot: Dict[str, Any]) -> str:
        def clipped(value: Any, token_budget: int) -> str:
            return self.context_manager.truncate_text_to_budget(
                str(value or ""),
                token_budget,
                notice="该消息过长，已截断",
            )

        lines = [
            f"机器人名称：{self.bot_name}",
            f"角色：{snapshot.get('role_name') or self.default_role}",
            "",
            "当前话题更早的少量上下文（只用于消歧）：",
        ]
        for message in snapshot.get("context_before") or []:
            lines.append(
                f"[{message.get('sender') or '未知'}] "
                f"{clipped(message.get('content'), 80)}"
            )
        anchor_message = snapshot.get("anchor_message") or {}
        lines.extend(
            [
                "",
                "直接触发机器人本轮回复的消息：",
                f"[{anchor_message.get('sender') or '未知'}] "
                f"{clipped(anchor_message.get('content'), 220)}",
                "",
                "机器人刚刚的回复：",
                f"[{self.bot_name}] {clipped(snapshot.get('reply_text'), 360)}",
                "",
                "回复后收到的新消息：",
            ]
        )
        for index, message in enumerate(snapshot.get("pending") or [], start=1):
            lines.append(
                f"{index}. [{message.get('sender') or '未知'}] "
                f"{clipped(message.get('content'), 120)}"
            )
        lines.extend(
            [
                "",
                "逐条判断候选消息与上述当前话题的关系，不要假定相邻消息都属于同一话题。",
                "以下关系算承接，应回复：answer（回答机器人）、requested_update（给出机器人刚要求或建议核实的结果/状态）、",
                "followup_question（继续追问）、correction（纠正）、clarification（澄清）、elaboration（补充实质信息）。",
                "直接回答、报告测量/尝试结果、补充机器人分析所需条件，即使是陈述句、没有问号、没有@，也属于承接；",
                "原提问者继续补充是强线索但不是硬条件，其他群友也可能直接回答机器人。",
                "中间夹入无关消息不会自动切断当前话题。分别判断每条候选消息；若有多条承接，选择序号最大的那条。",
                "以下关系不回复：new_topic（新话题）、side_chat（群友旁聊或引用别处话题）、acknowledgement（纯致谢/附和）、",
                "reaction（表情或纯情绪）、already_answered（群友已充分回答用户且没有留给机器人处理的内容）、unclear（确实无法判定）。",
                "群友回答机器人的问题或请求不属于 already_answered。",
                "relation 必须从上述分类中选一个。若存在承接消息，relation 填该消息的关系，target_message_index 填其序号；",
                "若不存在，relation 填一种不回复关系，target_message_index 填 0。",
            ]
        )
        return self.context_manager.truncate_text_to_budget(
            "\n".join(lines),
            3000,
            notice="续聊判定上下文达到预算上限",
        )

    def _normalize_followup_judge_result(
        self,
        result: Dict[str, Any],
        *,
        pending_count: int,
    ) -> Dict[str, Any]:
        """Normalize follow-up dialogue acts and make the decision internally consistent."""
        normalized = self._normalize_judge_result(result)
        raw_relation = str(
            result.get("relation")
            or result.get("dialogue_act")
            or result.get("message_relation")
            or ""
        ).strip().lower()
        relation = _FOLLOWUP_RELATION_ALIASES.get(raw_relation, raw_relation)

        if relation in _FOLLOWUP_POSITIVE_RELATIONS:
            should_reply = True
        elif relation in _FOLLOWUP_NEGATIVE_RELATIONS:
            should_reply = False
        else:
            # Backwards-compatible fallback for a provider that omits the new
            # relation field. New prompts always request a controlled value.
            relation = "unclear"
            should_reply = bool(normalized["should_reply"])

        raw_target = result.get(
            "target_message_index",
            result.get("target_index", result.get("message_index", 0)),
        )
        try:
            target_index = int(raw_target)
        except (TypeError, ValueError):
            target_index = 0

        pending_count = max(0, int(pending_count or 0))
        if should_reply:
            if not 1 <= target_index <= pending_count:
                # A single candidate is unambiguous. With interleaved messages,
                # never guess which event should own the generated reply.
                target_index = 1 if pending_count == 1 else 0
            if target_index == 0:
                should_reply = False
                relation = "unclear"
        else:
            target_index = 0

        return {
            "should_reply": should_reply,
            "target_message_index": target_index,
            "relation": relation,
            "reason": str(normalized["reason"]),
        }

    def _followup_approval_is_current(self, event: Event) -> bool:
        chat_name = str(event.data.get("chat_name") or "")
        anchor_id = str(event.data.get("_followup_anchor_id") or "")
        snapshot_sequence = int(event.data.get("_followup_snapshot_seq") or 0)
        if not chat_name or not anchor_id or snapshot_sequence <= 0:
            return False
        config = self._get_followup_config(chat_name)
        if not config["enabled"]:
            return False
        ingress = self._get_chat_ingress_state(chat_name)
        if int(ingress.get("sequence") or 0) != snapshot_sequence:
            return False
        if self._ingress_mentions_bot(chat_name, ingress):
            return False
        with self._followup_lock:
            session = self._followup_sessions.get(chat_name)
            return bool(
                session
                and session.get("anchor_id") == anchor_id
                and int(session.get("automatic_turns") or 0) < config["max_turns"]
            )

    def _discard_stale_followup_model_result(
        self,
        chat_name: str,
        *,
        invalidate_provider: bool = True,
    ) -> None:
        """Drop conversation state after a generated follow-up is not sent."""
        self._anchored_contexts.pop(chat_name, None)
        try:
            self._anchored_context_path(chat_name).unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("🔗 Failed to remove stale anchor for %s: %s", chat_name, exc)
        if invalidate_provider and self.codex_persistent_session_enabled:
            try:
                from app.services.agent_runtime import get_agent_runtime
                from app.services.codex_profile_service import get_codex_runtime_registry

                get_agent_runtime().invalidate_chat(
                    chat_name,
                    reason="discarded_followup",
                )
                get_codex_runtime_registry().invalidate_chat(
                    chat_name,
                    reason="discarded_followup",
                )
            except Exception as exc:
                logger.warning(
                    "🔗 Failed to invalidate stale Codex thread for %s: %s",
                    chat_name,
                    exc,
                )

    def close(self) -> None:
        with self._followup_lock:
            self._followup_closed = True
            sessions = list(self._followup_sessions.values())
            self._followup_sessions.clear()
        for session in sessions:
            timer = session.get("timer")
            if timer:
                timer.cancel()
        self._followup_executor.shutdown(wait=False, cancel_futures=True)
        self.memory_service.close()

    def _is_processing(self, chat_name: str) -> bool:
        """检查是否正在处理该聊天的主动回复"""
        if chat_name not in self._processing_locks:
            return False

        # 检查锁是否超时
        lock_time = self._processing_locks[chat_name]
        if time.time() - lock_time > self._lock_timeout:
            # 锁超时，清理
            del self._processing_locks[chat_name]
            logger.warning(f"⏰ Processing lock for {chat_name} expired (timeout: {self._lock_timeout}s)")
            return False

        return True

    def _set_processing(self, chat_name: str):
        """设置处理锁"""
        self._processing_locks[chat_name] = time.time()
        logger.debug(f"🔒 Set processing lock for {chat_name}")

    def _clear_processing(self, chat_name: str):
        """清除处理锁"""
        if chat_name in self._processing_locks:
            del self._processing_locks[chat_name]
            logger.debug(f"🔓 Cleared processing lock for {chat_name}")

    def _judge_cooldown_key(self, chat_name: str, judge_name: str) -> str:
        return f"{chat_name}::{judge_name}"

    def _clear_judge_cooldowns(self, chat_name: str):
        removed = False
        for key in list(self._judge_cooldowns.keys()):
            if key == chat_name or key.startswith(f"{chat_name}::"):
                del self._judge_cooldowns[key]
                removed = True
        if removed:
            logger.debug(f"❄️ Judge cooldown cleared for {chat_name} (successful reply)")

    def _check_judge_cooldown(
        self,
        chat_name: str,
        judge_name: str,
        current_msg_count: int,
        timing: Dict[str, int],
        last_reply_time: Optional[datetime] = None,
    ) -> bool:
        """检查裁判冷却是否已过期

        Args:
            chat_name: 聊天名称
            judge_name: Judge 名称
            current_msg_count: 当前消息数（距离上一条机器人消息）
            timing: Judge 触发/冷却参数
            last_reply_time: 上一次机器人回复的时间

        Returns:
            True if cooldown expired (can consult judge)
            False if still in cooldown
        """
        cooldown_key = self._judge_cooldown_key(chat_name, judge_name)
        if cooldown_key not in self._judge_cooldowns:
            return True  # No cooldown, can consult

        cooldown = self._judge_cooldowns[cooldown_key]
        cooldown_time = cooldown['time']
        cooldown_msg_count = int(cooldown.get('msg_count') or 0)
        cooldown_total_count = int(cooldown.get('total_count') or 0)

        # 1. 检查中间是否有机器人插话
        # 如果 last_reply_time 晚于 cooldown_time，说明在冷却期间机器人（或Summary插件）已经说过话了
        # 这会导致 current_msg_count 被重置，之前的 cooldown_msg_count 变得无意义
        # 此时应视为冷却失效（或者任务已完成），可以重新开始
        if last_reply_time:
             # datetime.fromtimestamp(cooldown_time) might be needed if they are diff types,
             # but cooldown_time is float (time.time())
             cooldown_dt = datetime.fromtimestamp(cooldown_time)
             if last_reply_time > cooldown_dt:
                 del self._judge_cooldowns[cooldown_key]
                 logger.debug(f"❄️ Judge[{judge_name}] cooldown cleared for {chat_name} (interleaved bot reply detected)")
                 return True

        # Check time condition
        cooldown_minutes = int(timing.get("cooldown_minutes", 1) or 0)
        cooldown_msg_threshold = int(timing.get("cooldown_msg_threshold", 0) or 0)
        time_elapsed = time.time() - cooldown_time
        time_ok = time_elapsed >= (cooldown_minutes * 60)

        # Use the cumulative log counter for cooldown deltas. current_msg_count comes
        # from a bounded scan window; once the bot's last reply falls outside that
        # window it can stop increasing and permanently pin the cooldown.
        current_total_count = self.chat_log_manager.count_messages(chat_name)
        if cooldown_total_count > 0 and current_total_count >= cooldown_total_count:
            new_msg_count = current_total_count - cooldown_total_count
            msg_count_ok = new_msg_count >= cooldown_msg_threshold
        else:
            # Backward-compatible fallback for in-memory cooldowns created before
            # total_count existed, or if cumulative counting is unavailable.
            new_msg_count = current_msg_count - cooldown_msg_count
            msg_count_ok = current_msg_count >= (cooldown_msg_count + cooldown_msg_threshold)

        if time_ok and msg_count_ok:
            # Cooldown expired, remove it
            del self._judge_cooldowns[cooldown_key]
            logger.debug(f"❄️ Judge[{judge_name}] cooldown expired for {chat_name}")
            return True

        logger.debug(
            f"❄️ Judge[{judge_name}] in cooldown for {chat_name}: "
            f"time={int(time_elapsed/60)}/{cooldown_minutes}min, "
            f"msgs={new_msg_count}/{cooldown_msg_threshold}"
        )
        return False

    def _set_judge_cooldown(self, chat_name: str, judge_name: str, current_msg_count: int, timing: Dict[str, int]):
        """设置裁判冷却

        Args:
            chat_name: 聊天名称
            judge_name: Judge 名称
            current_msg_count: 当前消息数
            timing: Judge 触发/冷却参数
        """
        cooldown_key = self._judge_cooldown_key(chat_name, judge_name)
        self._judge_cooldowns[cooldown_key] = {
            'time': time.time(),
            'msg_count': current_msg_count,
            'total_count': self.chat_log_manager.count_messages(chat_name),
        }
        logger.info(
            f"❄️ Judge[{judge_name}] cooldown set for {chat_name} "
            f"(will retry after {timing.get('cooldown_msg_threshold', 0)} msgs "
            f"AND {timing.get('cooldown_minutes', 1)} min)"
        )


    def handle_quote_video_message(self, event: Event):
        """处理引用视频；视觉问答与引用图片共用隔离流程。"""
        return self.handle_quote_image_message(event)

    def handle_quote_image_message(self, event: Event):
        """处理引用图片或视频消息事件 (Unified Flow)。"""
        try:
            start_time = time.time()

            content = event.data.get("message", "")
            chat_name = event.data.get("chat_name", "")
            sender = event.data.get("sender", "")
            chat_type = event.data.get("chat_type", "private")
            quote_content = event.data.get("quote_content", "")
            is_video_quote = bool(event.data.get("has_quote_video")) or "[视频]" in str(quote_content)
            media_label = "视频" if is_video_quote else "图片"
            logger.info("🤖 Assistant received quoted %s event", media_label)

            if self._is_sender_ignored(chat_name, sender):
                logger.info(f"🤖 Ignored blacklisted quote sender: chat={chat_name}, sender={sender}")
                return False

            # ---- 消息去重逻辑 (引用消息也需要) ----
            import hashlib
            # 引用消息的去重 key 增加 quote_content
            content_hash = hashlib.md5(f"{content}|{quote_content}".encode('utf-8')).hexdigest()
            dedup_key = (chat_name, sender, content_hash)
            now = time.time()
            if dedup_key in self._message_dedup_cache:
                if now - self._message_dedup_cache[dedup_key] < self._dedup_window:
                    logger.warning(f"⚠️ 检测到重复引用消息事件，跳过处理: chat={chat_name}, sender={sender}")
                    return False
            self._message_dedup_cache[dedup_key] = now
            # --------------------
            logger.info(f"🔍 Quote message details: sender={sender}, chat={chat_name}, content='{content[:50]}', quote_content='{quote_content}'")


            # 初始化变量，防止在finally块中访问未定义变量
            is_mention = False
            proactive_processing_acquired = False
            logger.info(f"✅ is_mention initialized to False")

            # 1. 验证是否为对应的媒体引用
            # 如果是通过兜底检测转发来的，跳过验证（我们已经通过正则确认了）
            expected_marker = "[视频]" if is_video_quote else "[图片]"
            expected_flag = "has_quote_video" if is_video_quote else "has_quote_image"
            logger.info(
                "🔍 Quote validation: media=%s quote_content=%r flag=%s",
                media_label,
                quote_content,
                bool(event.data.get(expected_flag)),
            )
            if expected_marker not in str(quote_content):
                # 检查是否为误识别消息转发（has_quote_image会被我们设置为True）
                if not event.data.get(expected_flag, False):
                    logger.warning(f"🤖 Quote content verification failed. Content: {quote_content[:100]}...")
                    return False
                else:
                    logger.info(f"🔧 跳过quote验证：这是误识别的引用图片消息（已通过正则确认)")
            else:
                logger.info(f"✅ Quote validation passed")

            # 2. 基础响应检查
            logger.info(f"🔍 Content check: should_respond={self._should_respond(content, chat_type)}")
            if not self._should_respond(content, chat_type):
                logger.info(f"🤖 Should not respond to quote message from {sender} (Empty or filtered)")
                return False
            logger.info(f"✅ Content check passed")

            # 3. 触发判断 (Active vs Passive)
            is_mention = self._event_mentions_bot(event)
            should_reply = False
            logger.info(f"🔍 Trigger check: is_mention={is_mention}, chat_type={chat_type}")

            wx_manager = event.context.get("wx")

            # A. 被动触发 (@ 或 私聊)
            # 兼容历史事件：私聊 chat_type 可能是 "private", "friend" 或 "user"。
            if chat_type in ["private", "friend", "user"] or (chat_type == "group" and is_mention and self.allow_mention_trigger):
                should_reply = True
                logger.info(f"🤖 Quote Trigger: Passive (Mention={is_mention}, Type={chat_type})")

            # B. 主动触发（群聊其余场景，包含 @关闭 时的@消息）
            elif chat_type == "group":
                if not self._check_proactive_permission(chat_name):
                    logger.debug(f"🤖 Quote Trigger: Proactive permission denied for {chat_name}")
                    return False

                if self._is_processing(chat_name):
                    logger.debug(f"🤖 Quote Trigger: Already processing")
                    return False

                role_name = self._get_user_role(chat_name)
                judge_name = self._get_user_judge(chat_name)
                if not judge_name:
                    logger.debug(f"⚖️ Quote Trigger: no judge binding for {chat_name}, proactive disabled")
                    return False
                judge_timing = self.judge_manager.get_judge_timing(judge_name)
                state = self._analyze_chat_state(
                    chat_name,
                    scan_threshold=max(judge_timing["trigger_msg_threshold"], judge_timing["cooldown_msg_threshold"]),
                )
                if not self._check_judge_cooldown(chat_name, judge_name, state['msg_count'], judge_timing, state['last_reply_time']):
                    logger.debug(f"🤖 Quote Trigger: Judge cooldown active")
                    return False

                judge_context = self._get_judge_context_messages(chat_name, 20)
                temp_last_msg = {"sender": sender, "content": f"{content} [引用了一个{media_label}]"}

                if not self._consult_judge(judge_context + [temp_last_msg], role_name, judge_name):
                    self._set_judge_cooldown(chat_name, judge_name, state['msg_count'], judge_timing)
                    logger.info(f"🤖 Quote Trigger: Judge rejected")
                    return False

                should_reply = True
                self._set_processing(chat_name)
                proactive_processing_acquired = True
                logger.info(f"📢 Proactive reply triggered for Quote Image in {chat_name}")
            else:
                logger.debug(f"🤖 Quote Trigger: conditions not met (Type={chat_type})")

            if not should_reply:
                return False

            # 4. 执行处理
            try:
                # 4.1 下载引用媒体。视频只在本地解码为四张抽帧。
                if is_video_quote:
                    visual_inputs = self._process_quoted_video_frames(event.data, wx_manager)
                else:
                    image_base64 = self._process_quoted_image(event.data, wx_manager)
                    visual_inputs = [
                        {
                            "base64": image_base64,
                            "position_percent": None,
                            "timestamp_seconds": None,
                        }
                    ] if image_base64 else []
                if not visual_inputs:
                    logger.warning(
                        "🤖 引用%s不可用，本次保持微信静默: chat=%s message_id=%s",
                        media_label,
                        chat_name,
                        event.data.get("message_id", ""),
                    )
                    return False

                # 4.2 获取上下文，实际入模内容由 token 预算动态裁剪
                memory_config = self._get_chat_memory_config(chat_name)
                context_msgs = self.chat_log_manager.get_context_messages(
                    chat_name,
                    self._memory_source_fetch_limit(memory_config),
                )
                self.memory_service.schedule(chat_name, memory_config)

                # 4.3 最终回复固定由 Codex 处理，图片始终作为本轮原始输入提交。
                # 引用图片问答必须把“当前问题指向随本条消息附带的图片”写进文本，
                # 否则像“真的吗 / 我问你这个”这类短问句很容易被长历史上下文带偏。
                chat_supports_vision = True
                image_description = ""

                if is_video_quote:
                    quote_visual_content = self._build_quote_video_augmented_content(
                        content,
                        visual_inputs,
                    )
                else:
                    quote_visual_content = self._build_quote_image_augmented_content(
                        content,
                        image_description=image_description,
                        image_available=chat_supports_vision,
                    )
                memory_context, memory_stats = self.memory_service.build_retrieval_context(
                    chat_name,
                    sender=sender,
                    content=quote_visual_content,
                    recent_messages=context_msgs,
                    config=memory_config,
                )
                # 4.4 构建消息（包含 system prompt 和变量替换）
                role_name = self._get_user_role(chat_name)
                messages = self._build_messages_array(
                    chat_name,
                    context_msgs,
                    "",
                    sender,
                    quote_visual_content,
                    role_name,
                    memory_config,
                    input_image_count=len(visual_inputs) if chat_supports_vision else 0,
                    memory_context=memory_context,
                )
                logger.info(
                    "🧠 Image retrieval context for %s: events=%s people=%s tokens≈%s",
                    chat_name,
                    memory_stats.get("event_count"),
                    memory_stats.get("people_count"),
                    memory_stats.get("tokens", 0),
                )
                if chat_supports_vision:
                    messages = self._attach_images_to_latest_user_message(
                        messages,
                        [str(item.get("base64") or "") for item in visual_inputs],
                    )

                # 4.6 将最终 Prompt 核验后的记忆审计随调用写入 LLM Records
                verified_memory_trace = self._reconcile_memory_trace(
                    memory_stats.get("trace"),
                    messages,
                )
                response_attachments: List[Dict[str, Any]] = []
                response = self._request_codex_reply(
                    chat_name,
                    role_name,
                    messages,
                    _mabobot_attachment_capture=response_attachments,
                    _mabobot_allow_image_input=chat_supports_vision,
                    _mabobot_memory_trace=verified_memory_trace,
                    _mabobot_web_search_mode="live" if is_video_quote else None,
                )
                if response_attachments:
                    response = self._strip_internal_action_markers(response)

                # 4.7 发送回复
                if wx_manager:
                    has_response_text = bool(self._format_response_parts(response, self.role_manager.get_output_settings(role_name)))
                    sent_response = (
                        self._send_response_parts(
                            wx_manager,
                            chat_name,
                            response,
                            role_name,
                            display_suffix="",
                        )
                        if has_response_text
                        else bool(response_attachments)
                    )
                    if sent_response:
                        if response_attachments:
                            sent_response = self._send_response_attachments(wx_manager, chat_name, response_attachments)
                        if not sent_response:
                            logger.error("🤖 Failed to send quoted-media attachment(s) to %s", chat_name)
                            return False
                        self._finalize_anchored_context(chat_name, response)
                        logger.info("🤖 Sent quoted-%s response to %s", media_label, chat_name)

                        # 记录 E2E 响应时间
                        duration = time.time() - start_time
                        try:
                            get_llm_manager()._record_stats("assistant", "reply_latency", "system", None, duration)
                            logger.info(f"⏱️ E2E Reply Latency (Quote): {duration:.2f}s")
                        except Exception as e:
                            logger.warning(f"Failed to record latency: {e}")

                        # 清除裁判冷却
                        self._clear_judge_cooldowns(chat_name)
                        return True
            finally:
                if proactive_processing_acquired:
                    self._clear_processing(chat_name)

            return False

        except Exception as e:
            logger.error("🤖 ChatBot 处理引用媒体失败: %s", e, exc_info=True)
            return False

    @staticmethod
    def _strip_quote_image_prompt_annotation(text: str) -> str:
        """移除只属于单轮引用视觉请求的内部提示注释。"""
        cleaned = str(text or "")
        markers = (
            "\n\n【当前引用图片】",
            "\n\n【重要】当前用户正在询问本条消息引用的图片。",
            "\n\n【当前引用视频】",
        )
        for marker in markers:
            if marker in cleaned:
                cleaned = cleaned.split(marker, 1)[0].rstrip()
        return cleaned

    @classmethod
    def _strip_image_content_parts(
        cls,
        messages: List[Dict],
        *,
        preserve_latest_user_annotation: bool = False,
    ) -> tuple[List[Dict], int]:
        """复制消息并移除历史图片块及单轮提示，避免污染持久上下文。"""
        cleaned = copy.deepcopy(list(messages or []))
        removed = 0
        image_part_types = {"image_url", "input_image", "image", "file"}
        latest_user_index = -1
        if preserve_latest_user_annotation:
            for index in range(len(cleaned) - 1, -1, -1):
                message = cleaned[index]
                if (
                    message.get("role") == "user"
                    and message.get("name") not in {"search_context", "memory_context"}
                ):
                    latest_user_index = index
                    break

        for index, message in enumerate(cleaned):
            content = message.get("content")
            preserve_annotation = index == latest_user_index
            if isinstance(content, str):
                if not preserve_annotation:
                    message["content"] = cls._strip_quote_image_prompt_annotation(content)
                continue
            if not isinstance(content, list):
                continue

            retained = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in image_part_types:
                    removed += 1
                    continue
                if (
                    not preserve_annotation
                    and isinstance(item, dict)
                    and item.get("type") == "text"
                ):
                    item["text"] = cls._strip_quote_image_prompt_annotation(
                        item.get("text") or ""
                    )
                retained.append(item)
            message["content"] = retained if retained else "[图片]"
        return cleaned, removed

    @classmethod
    def _attach_image_to_latest_user_message(
        cls,
        messages: List[Dict],
        image_base64: str,
    ) -> List[Dict]:
        """仅在隔离副本的最新用户消息上附加当前图片。"""
        return cls._attach_images_to_latest_user_message(
            messages,
            [image_base64] if image_base64 else [],
        )

    @classmethod
    def _attach_images_to_latest_user_message(
        cls,
        messages: List[Dict],
        images_base64: List[str],
    ) -> List[Dict]:
        """在本轮最新用户消息上附加一张或多张隔离图片。"""
        image_values = [str(value or "").strip() for value in images_base64 if str(value or "").strip()]
        if not messages or not image_values:
            return copy.deepcopy(list(messages or []))

        prepared, removed_history_images = cls._strip_image_content_parts(
            messages,
            preserve_latest_user_annotation=True,
        )
        image_parts = []
        for image_value in image_values:
            image_url = image_value
            if not image_url.startswith("data:image/"):
                image_url = f"data:image/jpeg;base64,{image_url}"
            image_parts.append({"type": "image_url", "image_url": {"url": image_url}})

        for msg in reversed(prepared):
            if msg.get("role") != "user" or msg.get("name") == "search_context":
                continue

            content = msg.get("content")
            if isinstance(content, list):
                content.extend(image_parts)
            elif content:
                msg["content"] = [
                    {"type": "text", "text": str(content)},
                    *image_parts,
                ]
            else:
                msg["content"] = image_parts
            logger.info(
                "🖼️ 引用视觉请求已隔离：移除历史图片=%s，当前图片=%s",
                removed_history_images,
                len(image_parts),
            )
            return prepared

        logger.warning("⚠️ 引用图片请求中没有可附加图片的用户消息")
        return prepared

    @staticmethod
    def _build_quote_image_augmented_content(
        content: str,
        *,
        image_description: str = "",
        image_available: bool = True,
    ) -> str:
        """给引用图片问答增加当前图片强绑定说明。

        目标：让最终模型明确知道“这个/真的吗/我问你这个”指的是当前消息附带的引用图片，
        历史聊天只能作为语气背景，不能拿旧图或旧话题来补主语。
        """
        content = str(content or "").strip()
        description = str(image_description or "").strip()
        if image_available:
            image_instruction = "请直接识别并回答随本条消息附带的当前图片。"
        elif description:
            image_instruction = f"（图片大致内容: {description}）"
        else:
            return content
        binding = (
            "【当前引用图片】用户问题指向本条消息绑定的当前图片。"
            f"{image_instruction}"
            "历史聊天只作为语气和背景参考，不要根据历史中的其他图片、"
            "其他‘这个/真的吗’或其他话题猜测。"
        )
        if not content:
            return binding
        if "【当前引用图片】" in content:
            return content
        return f"{content}\n\n{binding}"

    @staticmethod
    def _build_quote_video_augmented_content(
        content: str,
        visual_inputs: List[Dict[str, Any]],
    ) -> str:
        """绑定四张视频抽帧，并要求模型主动核查出处。"""
        content = str(content or "").strip()
        labels = []
        for index, item in enumerate(visual_inputs, start=1):
            try:
                percent = float(item.get("position_percent"))
                timestamp = float(item.get("timestamp_seconds"))
                labels.append(f"第{index}张 {percent:.1f}% / {timestamp:.1f}秒")
            except (TypeError, ValueError):
                labels.append(f"第{index}张")
        frame_note = "、".join(labels) or "4张等间隔抽帧"
        binding = (
            "【当前引用视频】本条消息附带的图片是同一个被引用视频的"
            f"四个时间分段中点抽帧（{frame_note}），按时间顺序排列。"
            "请结合四张图回答用户，但不要声称看到了抽帧之间的动作或听到了声音。"
            "无论用户是否明确追问出处，都要主动检查画面中的水印、账号名、"
            "标题、字幕、标志和地标等线索，并使用网页搜索尝试找到原始发布页或可靠出处。"
            "找到可靠出处时可自然地补充证据和直接链接；找不到时不得编造，"
            "也不要汇报检索步骤、缺少了哪些视觉线索，或追加模板化的免责说明。"
            "只有当用户明确询问出处时，才需要直接回答当前的查证结果。"
            "历史聊天只作为语气和背景参考，不要借用历史中的其他图片猜测。"
        )
        if not content:
            return binding
        if "【当前引用视频】" in content:
            return content
        return f"{content}\n\n{binding}"

    def _should_respond(self, content: str, chat_type: str) -> bool:
        """判断是否应该响应消息
        注意：@触发逻辑已统一移动到EventBus权限检查中处理
        此方法仅做基础内容检查
        """
        # 基础内容检查
        return bool(content.strip())

    def _get_user_role(self, chat_name: str) -> str:
        """获取用户的角色配置"""
        try:
            from app.models.base import SessionLocal
            from app.models.user_permission import WeChatUser
            from app.models.chatbot_role import UserChatBotRole, ChatBotRole

            with SessionLocal() as db:
                user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat_name).first()
                if user:
                    # 查询用户角色关联
                    user_role = db.query(UserChatBotRole).filter(UserChatBotRole.user_id == user.id).first()
                    if user_role:
                        role = db.query(ChatBotRole).filter(ChatBotRole.id == user_role.role_id).first()
                        if role:
                            role_name = role.name
                            logger.debug(f"🎭 User '{chat_name}' using role: {role_name}")
                            return role_name

                logger.debug(f"🎭 User '{chat_name}' using default role")
                return self.default_role
        except Exception as e:
            logger.error(f"🎭 Error getting user role: {e}")
            return self.default_role

    def _get_user_judge(self, chat_name: str) -> Optional[str]:
        """获取用户的 Judge 绑定。未绑定时返回 None（主动 Judge 禁用）。"""
        try:
            from app.models.base import SessionLocal
            from app.models.user_permission import WeChatUser
            from app.models.chatbot_judge import UserChatBotJudge, ChatBotJudge

            with SessionLocal() as db:
                user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat_name).first()
                if not user:
                    return None

                user_judge = db.query(UserChatBotJudge).filter(UserChatBotJudge.user_id == user.id).first()
                if not user_judge:
                    return None

                judge = db.query(ChatBotJudge).filter(ChatBotJudge.id == user_judge.judge_id).first()
                if not judge:
                    return None

                logger.debug(f"⚖️ User '{chat_name}' using judge: {judge.name}")
                return judge.name
        except Exception as e:
            logger.error(f"⚖️ Error getting user judge: {e}")
            return None

    def _clean_query(self, content: str, chat_name: str = "") -> str:
        """清理查询内容，移除@机器人名称"""
        return strip_bot_mentions(content, self._bot_names_for_chat(chat_name))

    def _build_quote_augmented_content(self, content: str, quote_content: str) -> str:
        """把文字引用内容显式放进本轮用户消息，避免 LLM 只看到“核实一下”。

        微信 UI 返回的 quote_content 有时只是预览片段（可能以 ... 结尾），
        但即使只是片段，也比完全丢失引用上下文更能让模型判断用户在问哪条消息。
        图片/视频等引用由专门流程处理，这里只增强文字引用。
        """
        content = str(content or "")
        quote = str(quote_content or "").strip()
        if not quote:
            return content

        non_text_markers = {"[图片]", "图片", "视频", "[视频]", "动画表情", "[动画表情]"}
        if quote in non_text_markers:
            return content

        # 避免上游已经把引用拼进 content 时重复塞一遍。
        if quote and quote in content:
            return content

        return (
            "【当前消息引用的原文片段】\n"
            f"{quote}\n\n"
            "【当前消息】\n"
            f"{content}"
        )

    @staticmethod
    def _build_quote_file_augmented_content(content: str, file_name: str) -> str:
        """Bind the latest request to one managed file without exposing its host path."""
        content = str(content or "").strip()
        name = str(file_name or "").strip() or "未命名文件"
        return (
            "【当前引用文件】\n"
            f"文件名：{name}\n"
            "该文件作为本轮输入附件提供；必须以它为处理对象，不要使用历史文件。\n\n"
            "【当前消息】\n"
            f"{content}"
        )



    def _format_chat_text(self, context_messages: List[Dict]) -> str:
        """统一的聊天记录格式化方法

        用于judge和chat回复,确保两者看到相同格式的上下文
        """
        if not context_messages:
            return "（暂无历史消息）"

        chat_lines = []
        for msg in context_messages:
            sender = msg.get('sender', 'User')
            content = msg.get('content', '')
            time_str = msg.get('time', '')
            if time_str:
                chat_lines.append(f"[{time_str}] [{sender}]: {content}")
            else:
                chat_lines.append(f"[{sender}]: {content}")

        return "\n".join(chat_lines)

    def _get_active_model_context_window(self, chat_name: str) -> int:
        """Use authoritative Codex runtime telemetry when it is available."""
        if self.context_window_auto_detect:
            profile_id = ""
            profile = None
            try:
                from app.services.codex_profile_service import (
                    CodexProfileService,
                    get_codex_profile_service,
                )

                profile_id = CodexProfileService.resolve_assistant_profile_id(
                    self._get_user_permission_config(chat_name).get("codex_profile_id")
                )
                if profile_id:
                    profile = get_codex_profile_service().get_profile(profile_id)
            except Exception as e:
                logger.debug("Codex Profile context metadata unavailable for %s: %s", chat_name, e)
            try:
                from app.services.agent_runtime import get_agent_runtime

                state = get_agent_runtime().state_store.get(chat_name) or {}
                context_window = int(state.get("model_context_window") or 0)
                state_profile = str(state.get("runtime_profile") or "")
                if (
                    context_window > 0
                    and state_profile == profile_id
                    and state.get("model_context_window_source") == "provider_usage"
                ):
                    return context_window
            except Exception as e:
                logger.debug("Codex context telemetry unavailable for %s: %s", chat_name, e)
                state = {}

            profile_context_window = int((profile or {}).get("context_window") or 0)
            if profile_context_window > 0:
                return profile_context_window
            state_context_window = int((state or {}).get("model_context_window") or 0)
            if state_context_window > 0 and str((state or {}).get("runtime_profile") or "") == profile_id:
                return state_context_window

        return 0

    def _effective_context_limits(
        self,
        chat_name: str,
        memory_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, int]:
        memory_config = memory_config or self._get_default_memory_config()
        configured_cap = max(4096, int(self.max_context_tokens or 220000))
        reserved = max(1024, int(self.reserved_output_tokens or 8192))
        safety = max(0, int(self.context_safety_margin_tokens or 0))
        model_window = self._get_active_model_context_window(chat_name)
        model_input_cap = (
            max(4096, model_window - reserved - safety)
            if model_window > 0
            else configured_cap
        )
        input_cap = min(configured_cap, model_input_cap)
        configured_rollover = max(
            4096,
            int(memory_config.get("anchor_rollover_prompt_tokens") or 205000),
        )
        rollover = min(configured_rollover, max(4096, input_cap - 4096))
        return {
            "model_context_window": model_window,
            "configured_cap": configured_cap,
            "input_cap": input_cap,
            "rollover": rollover,
            "reserved_output": reserved,
            "safety_margin": safety,
        }

    def _calculate_context_budgets(
        self,
        chat_name: str,
        search_results: str,
        content: str,
        memory_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, int]:
        """Calculate adaptive token budgets for long-context prompting."""
        memory_config = memory_config or self._get_default_memory_config()
        limits = self._effective_context_limits(chat_name, memory_config)
        # Keep room for the static role prompt, output contract and chat
        # serialization so the finished sliding prompt respects input_cap.
        available = max(1024, limits["input_cap"] - 4096)

        current_tokens = self.context_manager.estimate_tokens(content) + 128
        search_tokens = self.context_manager.estimate_tokens(search_results)
        ephemeral_cap = min(
            max(512, int(self.ephemeral_context_max_tokens or 16000)),
            max(512, int(available * max(0.01, self.ephemeral_context_ratio))),
        )
        ephemeral_used = min(ephemeral_cap, search_tokens + current_tokens)

        durable_budget = max(1024, available - ephemeral_used)
        configured_memory_cap = max(
            0,
            int(memory_config.get("memory_context_max_tokens") or 0),
        )
        memory_budget = 0
        if memory_config.get("memory_enabled", True):
            memory_ratio = max(0.0, self.memory_context_ratio)
            recent_ratio = max(0.0, self.recent_context_ratio)
            ratio_total = memory_ratio + recent_ratio or 1.0
            memory_budget = min(
                configured_memory_cap,
                max(512, int(durable_budget * (memory_ratio / ratio_total))),
            )
        recent_budget = max(1024, durable_budget - memory_budget)

        return {
            "available": available,
            "memory": max(0, memory_budget),
            "recent": max(1024, recent_budget),
            "ephemeral_cap": ephemeral_cap,
            **limits,
        }

    def _build_messages_array(
        self,
        chat_name: str,
        context_messages: List[Dict],
        search_results: str,
        sender: str,
        content: str,
        role_name: str,
        memory_config: Optional[Dict[str, Any]] = None,
        input_image_count: int = 0,
        memory_context: str = "",
    ) -> List[Dict]:
        """构建发送给 LLM 的消息数组（预算驱动的分层上下文）

        Args:
            chat_name: 聊天名称
            context_messages: 上下文消息列表
            search_results: 网络搜索结果（纯文本）
            sender: 发送者
            content: 消息内容
            role_name: 角色名称

        Returns:
            OpenAI 格式的消息数组
        """
        memory_config = memory_config or self._get_default_memory_config()

        if self._use_anchored_append_context(memory_config, role_name):
            return self._build_anchored_append_messages(
                chat_name=chat_name,
                context_messages=context_messages,
                search_results=search_results,
                sender=sender,
                content=content,
                role_name=role_name,
                memory_config=memory_config,
                input_image_count=input_image_count,
                memory_context=memory_context,
            )

        # 去重逻辑：如果最后一条历史消息与当前消息一致，则移除
        if context_messages and len(context_messages) > 0:
            last_msg = context_messages[-1]
            if last_msg.get('sender') == sender and last_msg.get('content', '').strip() == content.strip():
                logger.debug(f"🤖 Removed duplicated last message from context: {content[:20]}...")
                context_messages = context_messages[:-1]

        budgets = self._calculate_context_budgets(
            chat_name,
            search_results,
            content,
            memory_config,
        )
        bounded_memory = self.context_manager.truncate_text_to_budget(
            memory_context,
            budgets["memory"],
            notice="相关记忆达到滑动窗口预算上限",
        )
        recent_messages, recent_tokens = self.context_manager.select_recent_messages(
            context_messages,
            budgets["recent"],
        )
        recent_text = self.context_manager.format_messages(recent_messages)
        sections = []
        if bounded_memory:
            sections.append(bounded_memory)
        sections.append("## 最近原始聊天记录\n" + recent_text)
        context_text = "\n\n".join(sections)
        context_stats = {
            "memory_tokens": self.context_manager.estimate_tokens(bounded_memory),
            "recent_tokens": recent_tokens,
            "recent_messages": len(recent_messages),
        }

        search_text = (search_results or "").strip()
        if search_text and search_text != "无结果":
            search_budget = max(
                256,
                budgets["ephemeral_cap"] - self.context_manager.estimate_tokens(content) - 128,
            )
            search_text = self.context_manager.truncate_text_to_budget(
                search_text,
                search_budget,
                notice="搜索结果因上下文预算限制已截断",
            )
        else:
            search_text = ""

        context_text = (
            "【上下文使用规则】\n"
            "1. 当前用户消息和最近原始聊天优先级最高。\n"
            "2. 检索记忆只在与当前话题、称呼、关系或固定梗直接相关时使用。\n"
            "3. 不要为了显得记得很多而主动扯旧事、成员画像、历史事件或群内黑话。\n"
            "4. 如果当前问题是普通事实问答、搜索问答或新话题，检索记忆通常不需要出现在回复里。\n\n"
            f"{context_text}"
        )

        # 1. 准备变量字典。保持角色 Prompt 自己定义的 {chat_text}/{search_results} 结构。
        variables = {
            'chat_text': context_text,
            'search_results': search_text,
            'sender': sender,
            'content': content
        }

        # 2. 获取角色 prompt（已完成变量替换）。
        # 如果角色 Prompt 本身不使用动态占位符，则把动态资料追加在所有静态规则之后。
        # 这样静态人设、长期规则、输出规范可以尽量保持 100% 前缀一致，利于 LLM 前缀缓存。
        role_template = self.role_manager.roles.get(role_name, "")
        role_uses_dynamic_slots = self._role_prompt_uses_dynamic_slots(role_template)
        role_prompt = self.role_manager.get_role_prompt(role_name, variables=variables)
        if not role_uses_dynamic_slots:
            role_prompt = role_prompt + self._build_dynamic_input_block(context_text, search_text)

        # 3. 构建消息数组
        # System message: 角色 prompt（已包含所有替换后的内容）
        # User message: 当前用户消息（带时间戳）
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        messages = [
            {"role": "system", "content": role_prompt},
        ]

        output_contract = self._build_reply_completion_contract(role_name)
        messages.append({"role": "system", "content": output_contract})

        messages.append(
            {"role": "user", "content": f"[{now_str}] [{sender}]: {content}"}
        )

        logger.info(
            "🤖 构建消息数组完成: msgs=%s, memory_tokens≈%s, "
            "recent_tokens≈%s, recent_msgs=%s, max_context=%s",
            len(messages),
            context_stats.get("memory_tokens"),
            context_stats.get("recent_tokens"),
            context_stats.get("recent_messages"),
            self.max_context_tokens,
        )
        return messages

    def _use_anchored_append_context(self, memory_config: Dict[str, Any], role_name: str) -> bool:
        if str(memory_config.get("context_window_strategy") or "").strip().lower() != "anchored_append":
            return False

        role_template = self.role_manager.roles.get(role_name, "")
        if self._role_prompt_uses_dynamic_slots(role_template):
            logger.warning(
                "🤖 Anchored append context disabled for role '%s': role prompt uses dynamic slots",
                role_name,
            )
            return False

        return True

    def _build_anchored_append_messages(
        self,
        chat_name: str,
        context_messages: List[Dict],
        search_results: str,
        sender: str,
        content: str,
        role_name: str,
        memory_config: Dict[str, Any],
        input_image_count: int = 0,
        memory_context: str = "",
    ) -> List[Dict]:
        role_prompt = self.role_manager.get_role_prompt(role_name)
        messages = [{"role": "system", "content": role_prompt}]

        output_contract = self._build_reply_completion_contract(role_name)
        messages.append({"role": "system", "content": output_contract})

        dynamic_messages = self._get_anchored_dynamic_messages(
            chat_name=chat_name,
            context_messages=context_messages,
            sender=sender,
            content=content,
            memory_config=memory_config,
        )
        state = self._anchored_contexts.get(chat_name) or {}
        checkpoint_text = str(state.get("memory_checkpoint") or "").strip()
        if checkpoint_text:
            messages.append(
                {
                    "role": "system",
                    "name": "memory_checkpoint",
                    "content": (
                        "【冻结群聊记忆检查点】\n"
                        "此检查点在当前线程内保持不变。最近原始消息与当前用户消息优先；"
                        "仅在相关时使用记忆，不要提及检查点或系统结构。\n\n"
                        f"{checkpoint_text}"
                    ),
                }
            )

        search_text = self._prepare_search_tail(
            chat_name,
            search_results,
            content,
            memory_config,
        )
        search_message = None
        if search_text:
            search_message = {
                "role": "user",
                "name": "search_context",
                "content": (
                    "【本轮网络搜索资料】\n"
                    "以下资料只服务于后面紧接着的当前聊天消息。不要提搜索、资料来源或系统结构，"
                    "消化后直接按角色口吻接话。\n\n"
                    f"{search_text}"
                ),
            }

        memory_message = None
        bounded_memory = self.context_manager.truncate_text_to_budget(
            memory_context,
            max(0, int(memory_config.get("memory_context_max_tokens") or 0)),
            notice="相关记忆达到本轮预算上限",
        )
        if bounded_memory:
            memory_message = {
                "role": "user",
                "name": "memory_context",
                "content": (
                    "【本轮相关群聊记忆】\n"
                    "以下内容只服务于后面紧接着的当前聊天消息。最近原始聊天优先；"
                    "仅在直接相关时自然使用，不要提及检索、事件卡或记忆系统。\n\n"
                    f"{bounded_memory}"
                ),
            }

        def _build_full_prompt(dynamic_base: List[Dict]) -> tuple[List[Dict], List[Dict]]:
            dynamic_prompt = list(dynamic_base)
            if memory_message:
                dynamic_prompt = self._insert_context_before_current_user_message(
                    dynamic_prompt,
                    memory_message,
                )
            if search_message:
                dynamic_prompt = self._insert_context_before_current_user_message(
                    dynamic_prompt,
                    search_message,
                )
            return messages + dynamic_prompt, dynamic_prompt

        full_messages, dynamic_prompt_messages = _build_full_prompt(dynamic_messages)
        prompt_tokens, token_source = self._count_chat_prompt_tokens(
            full_messages,
            input_image_count=input_image_count,
        )

        limits = self._effective_context_limits(chat_name, memory_config)
        rollover_tokens = limits["rollover"]
        input_cap = limits["input_cap"]
        anchor_count = int(memory_config.get("anchor_message_count") or 300)
        if rollover_tokens > 0 and prompt_tokens >= rollover_tokens:
            logger.info(
                "🤖 Anchored context rollover for %s: prompt_tokens=%s source=%s >= %s; "
                "resetting to last %s messages",
                chat_name,
                prompt_tokens,
                token_source,
                rollover_tokens,
                anchor_count,
            )
            state = self._reset_anchored_context(
                chat_name=chat_name,
                context_messages=context_messages,
                sender=sender,
                content=content,
                anchor_count=anchor_count,
                total_count=self.chat_log_manager.count_messages(chat_name),
                memory_config=memory_config,
            )
            dynamic_messages = list(state.get("messages") or [])
            messages = messages[:2]
            checkpoint_text = str(state.get("memory_checkpoint") or "").strip()
            if checkpoint_text:
                messages.append(
                    {
                        "role": "system",
                        "name": "memory_checkpoint",
                        "content": (
                            "【冻结群聊记忆检查点】\n"
                            "此检查点在当前线程内保持不变。最近原始消息与当前用户消息优先；"
                            "仅在相关时使用记忆，不要提及检查点或系统结构。\n\n"
                            f"{checkpoint_text}"
                        ),
                    }
                )
            full_messages, dynamic_prompt_messages = _build_full_prompt(dynamic_messages)
            prompt_tokens, token_source = self._count_chat_prompt_tokens(
                full_messages,
                input_image_count=input_image_count,
            )

        if prompt_tokens > input_cap:
            dynamic_messages, prompt_tokens = self._trim_anchored_messages_to_cap(
                base_messages=messages,
                dynamic_messages=dynamic_messages,
                search_message=search_message,
                memory_message=memory_message,
                input_cap=input_cap,
                input_image_count=input_image_count,
            )
            state = self._anchored_contexts.get(chat_name)
            if state is not None:
                state["messages"] = list(dynamic_messages)
            full_messages, dynamic_prompt_messages = _build_full_prompt(dynamic_messages)
            prompt_tokens, token_source = self._count_chat_prompt_tokens(
                full_messages,
                input_image_count=input_image_count,
            )

        self._mark_anchored_pending(chat_name, dynamic_prompt_messages)

        logger.info(
            "🤖 构建锚定追加消息完成: msgs=%s, dynamic_msgs=%s, prompt_tokens=%s, "
            "token_source=%s, anchor_messages=%s, rollover=%s, input_cap=%s, model_window=%s",
            len(full_messages),
            len(dynamic_prompt_messages),
            prompt_tokens,
            token_source,
            memory_config.get("anchor_message_count"),
            rollover_tokens,
            input_cap,
            limits["model_context_window"],
        )
        return full_messages

    def _trim_anchored_messages_to_cap(
        self,
        *,
        base_messages: List[Dict],
        dynamic_messages: List[Dict],
        search_message: Optional[Dict],
        memory_message: Optional[Dict],
        input_cap: int,
        input_image_count: int,
    ) -> tuple[List[Dict], int]:
        """Drop oldest raw messages while preserving the current user turn."""
        trimmed = list(dynamic_messages)

        def build() -> List[Dict]:
            candidate = list(trimmed)
            if memory_message:
                candidate = self._insert_context_before_current_user_message(
                    candidate,
                    memory_message,
                )
            if search_message:
                candidate = self._insert_context_before_current_user_message(
                    candidate,
                    search_message,
                )
            return list(base_messages) + candidate

        tokens, _ = self._count_chat_prompt_tokens(
            build(),
            input_image_count=input_image_count,
        )
        while tokens > input_cap and len(trimmed) > 1:
            drop_count = max(1, min(len(trimmed) - 1, len(trimmed) // 8))
            trimmed = trimmed[drop_count:]
            tokens, _ = self._count_chat_prompt_tokens(
                build(),
                input_image_count=input_image_count,
            )

        if tokens > input_cap and search_message:
            # Search is optional; the current user message and role contract are not.
            search_message.clear()
            tokens, _ = self._count_chat_prompt_tokens(
                build(),
                input_image_count=input_image_count,
            )

        if tokens > input_cap and memory_message:
            # Retrieved memory is optional; never drop the triggering message.
            memory_message.clear()
            tokens, _ = self._count_chat_prompt_tokens(
                build(),
                input_image_count=input_image_count,
            )

        if tokens > input_cap:
            logger.error(
                "Anchored prompt remains above hard cap after trimming: tokens=%s cap=%s",
                tokens,
                input_cap,
            )
        else:
            logger.warning(
                "Anchored prompt trimmed to hard cap: remaining_messages=%s tokens=%s cap=%s",
                len(trimmed),
                tokens,
                input_cap,
            )
        return trimmed, tokens

    def _insert_context_before_current_user_message(
        self,
        dynamic_messages: List[Dict],
        context_message: Dict,
    ) -> List[Dict]:
        """Keep the real triggering chat message as the final user message."""
        if not dynamic_messages:
            return [context_message]

        last_msg = dynamic_messages[-1]
        if (
            last_msg.get("role") == "user"
            and last_msg.get("name") not in {"search_context", "memory_context"}
        ):
            return dynamic_messages[:-1] + [context_message, last_msg]

        return dynamic_messages + [context_message]

    def _prepare_search_tail(
        self,
        chat_name: str,
        search_results: str,
        content: str,
        memory_config: Dict[str, Any],
    ) -> str:
        search_text = (search_results or "").strip()
        if not search_text or search_text == "无结果":
            return ""

        available = self._effective_context_limits(chat_name, memory_config)["input_cap"]
        ephemeral_cap = min(
            max(512, int(self.ephemeral_context_max_tokens or 16000)),
            max(512, int(available * max(0.01, self.ephemeral_context_ratio))),
        )
        search_budget = max(
            256,
            ephemeral_cap - self.context_manager.estimate_tokens(content) - 128,
        )
        return self.context_manager.truncate_text_to_budget(
            search_text,
            search_budget,
            notice="搜索结果因上下文预算限制已截断",
        )

    def _get_anchored_dynamic_messages(
        self,
        chat_name: str,
        context_messages: List[Dict],
        sender: str,
        content: str,
        memory_config: Dict[str, Any],
    ) -> List[Dict]:
        total_count = self.chat_log_manager.count_messages(chat_name)
        state = self._anchored_contexts.get(chat_name) or self._load_anchored_context(chat_name)
        anchor_count = int(memory_config.get("anchor_message_count") or 300)

        if not state:
            state = self._reset_anchored_context(
                chat_name=chat_name,
                context_messages=context_messages,
                sender=sender,
                content=content,
                anchor_count=anchor_count,
                total_count=total_count,
                memory_config=memory_config,
            )
        else:
            if "memory_checkpoint" not in state:
                checkpoint_text, checkpoint_tokens = (
                    self.memory_service.get_checkpoint_text(
                        chat_name,
                        token_budget=int(memory_config["memory_checkpoint_max_tokens"]),
                    )
                )
                state["memory_checkpoint"] = checkpoint_text
                state["memory_checkpoint_tokens"] = checkpoint_tokens
                state["memory_checkpoint_created_at"] = datetime.now().isoformat(
                    timespec="seconds"
                )
            state = self._append_new_log_messages_to_anchor(
                chat_name=chat_name,
                state=state,
                sender=sender,
                content=content,
                total_count=total_count,
            )

        return list(state.get("messages") or [])

    def _reset_anchored_context(
        self,
        chat_name: str,
        context_messages: List[Dict],
        sender: str,
        content: str,
        anchor_count: int,
        total_count: int,
        memory_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        memory_config = memory_config or self._get_default_memory_config()
        source_messages = list(context_messages or [])[-anchor_count:]
        if not source_messages:
            source_messages = self.chat_log_manager.get_context_messages(chat_name, anchor_count)

        source_messages = self._ensure_current_message_present(source_messages, sender, content)
        formatted = self.chat_log_manager.format_messages_array(source_messages, bot_name=self.bot_name)
        checkpoint_text, checkpoint_tokens = self.memory_service.get_checkpoint_text(
            chat_name,
            token_budget=int(memory_config["memory_checkpoint_max_tokens"]),
        )
        state = {
            "messages": formatted,
            "log_count": total_count,
            "log_sequence": total_count,
            "anchor_message_count": anchor_count,
            "memory_checkpoint": checkpoint_text,
            "memory_checkpoint_tokens": checkpoint_tokens,
            "memory_checkpoint_created_at": datetime.now().isoformat(timespec="seconds"),
            "pending_messages": None,
        }
        self._anchored_contexts[chat_name] = state
        return state

    def _append_new_log_messages_to_anchor(
        self,
        chat_name: str,
        state: Dict[str, Any],
        sender: str,
        content: str,
        total_count: int,
    ) -> Dict[str, Any]:
        last_count = int(state.get("log_sequence") or state.get("log_count") or 0)
        if total_count < last_count:
            logger.warning(
                "🤖 Ignoring regressed chat count for %s: observed=%s < anchored=%s; "
                "repairing the cumulative floor and preserving the Codex prefix",
                chat_name,
                total_count,
                last_count,
            )
            total_count = self.chat_log_manager.ensure_minimum_count(
                chat_name,
                last_count,
            )

        delta = total_count - last_count
        if delta > 0:
            sequence_reader = getattr(
                self.chat_log_manager,
                "get_messages_after_sequence",
                None,
            )
            if callable(sequence_reader):
                new_messages = sequence_reader(
                    chat_name,
                    after_sequence=last_count,
                    through_sequence=total_count,
                    limit=max(delta, 1),
                )
            else:
                # Compatibility for injected/legacy ChatLogManager doubles.
                new_messages = self.chat_log_manager.get_context_messages(chat_name, delta)
            formatted = self.chat_log_manager.format_messages_array(new_messages, bot_name=self.bot_name)
            state["messages"] = list(state.get("messages") or []) + formatted
            state["log_count"] = total_count
            state["log_sequence"] = total_count

        state["messages"] = self._ensure_current_formatted_present(
            list(state.get("messages") or []),
            sender,
            content,
        )
        return state

    def _ensure_current_message_present(self, messages: List[Dict], sender: str, content: str) -> List[Dict]:
        if not content.strip():
            return messages
        if messages:
            last_msg = messages[-1]
            if last_msg.get("sender") == sender and str(last_msg.get("content", "")).strip() == content.strip():
                return messages
        return messages + [{
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sender": sender,
            "content": content,
        }]

    def _ensure_current_formatted_present(self, messages: List[Dict], sender: str, content: str) -> List[Dict]:
        if not content.strip():
            return messages
        expected_content = f"[{sender}]: {content}"
        if messages:
            last_msg = messages[-1]
            if last_msg.get("role") == "user" and str(last_msg.get("content", "")).strip() == expected_content.strip():
                return messages
        sender_name = self.chat_log_manager._sanitize_name(sender)
        return messages + [{"role": "user", "name": sender_name, "content": expected_content}]

    def _mark_anchored_pending(self, chat_name: str, dynamic_messages: List[Dict]) -> None:
        state = self._anchored_contexts.get(chat_name)
        if state is not None:
            state["pending_messages"] = copy.deepcopy(list(dynamic_messages))

    def _finalize_anchored_context(self, chat_name: str, response: str) -> None:
        state = self._anchored_contexts.get(chat_name)
        if not state:
            return
        pending = state.get("pending_messages")
        if not pending:
            return
        persistent_pending, removed_images = self._strip_image_content_parts(pending)
        if removed_images:
            logger.warning(
                "🧹 锚定上下文持久化前移除 %s 个临时图片块: %s",
                removed_images,
                chat_name,
            )
        state["messages"] = self._strip_ephemeral_context_messages(persistent_pending) + [
            {"role": "assistant", "content": response or ""}
        ]
        state["log_count"] = max(
            int(state.get("log_count") or 0),
            self.chat_log_manager.count_messages(chat_name),
        )
        state["log_sequence"] = max(
            int(state.get("log_sequence") or 0),
            int(state.get("log_count") or 0),
        )
        state["pending_messages"] = None
        self._save_anchored_context(chat_name, state)

    def _strip_ephemeral_context_messages(self, messages: List[Dict]) -> List[Dict]:
        """Drop per-turn tool context before persisting anchored chat state."""
        return [
            msg for msg in messages
            if msg.get("name") not in {"search_context", "memory_context"}
        ]

    def _count_chat_prompt_tokens(
        self,
        messages: List[Dict],
        *,
        input_image_count: int = 0,
    ) -> tuple[int, str]:
        """Count the current chat prompt with Codex's renderer."""
        try:
            token_count = get_assistant_reply_gateway().count_prompt_tokens(
                messages,
                native_web_search_enabled=self._is_search_enabled(),
                input_image_count=input_image_count,
            )
            return int(token_count), "codex_o200k_base"
        except Exception as e:
            logger.warning(f"⚠️ Accurate Codex prompt token count unavailable, using heuristic: {e}")

        return self._estimate_messages_tokens(messages), "heuristic_fallback"

    def _estimate_messages_tokens(self, messages: List[Dict]) -> int:
        total = 0
        for msg in messages or []:
            total += 4
            total += self.context_manager.estimate_tokens(msg.get("role", ""))
            total += self.context_manager.estimate_tokens(msg.get("name", ""))
            total += self.context_manager.estimate_tokens(msg.get("content", ""))
        return total

    def _anchored_context_path(self, chat_name: str) -> Path:
        safe_name = re.sub(r'[\\/:*?"<>|\s]+', "_", chat_name).strip("_")
        if not safe_name:
            safe_name = "unknown_chat"
        return self._anchored_context_dir / f"{safe_name}.json"

    def invalidate_memory_context(self, chat_name: str) -> None:
        """Apply an explicit memory/settings edit on the next model turn."""
        self.memory_service.invalidate(chat_name)
        self._anchored_contexts.pop(chat_name, None)
        try:
            self._anchored_context_path(chat_name).unlink(missing_ok=True)
        except Exception as e:
            logger.warning("Failed to invalidate anchored memory for %s: %s", chat_name, e)

    def _load_anchored_context(self, chat_name: str) -> Optional[Dict[str, Any]]:
        path = self._anchored_context_path(chat_name)
        if not path.exists():
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)

            state = payload.get("state") if isinstance(payload, dict) else None
            if not isinstance(state, dict) or not isinstance(state.get("messages"), list):
                return None

            cleaned_messages, removed_images = self._strip_image_content_parts(
                state.get("messages") or []
            )
            state["messages"] = cleaned_messages
            state["pending_messages"] = None
            state["log_sequence"] = int(
                state.get("log_sequence") or state.get("log_count") or 0
            )
            self._anchored_contexts[chat_name] = state
            if removed_images:
                logger.warning(
                    "🧹 已清理旧锚定上下文中的 %s 个历史图片块: %s",
                    removed_images,
                    chat_name,
                )
                self._save_anchored_context(chat_name, state)
            logger.info(
                "🤖 Loaded persisted anchored context for %s: messages=%s, log_sequence=%s, tokens≈%s",
                chat_name,
                len(state.get("messages") or []),
                state.get("log_sequence"),
                self._estimate_messages_tokens(state.get("messages") or []),
            )
            return state
        except Exception as e:
            logger.warning(f"⚠️ Failed to load anchored context for {chat_name}: {e}")
            return None

    def _save_anchored_context(self, chat_name: str, state: Dict[str, Any]) -> None:
        try:
            path = self._anchored_context_path(chat_name)
            path.parent.mkdir(parents=True, exist_ok=True)
            persistent_messages, _ = self._strip_image_content_parts(
                state.get("messages") or []
            )
            payload = {
                "version": 2,
                "chat_name": chat_name,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "state": {
                    "messages": self._strip_ephemeral_context_messages(
                        persistent_messages
                    ),
                    "log_count": int(state.get("log_count") or 0),
                    "log_sequence": int(
                        state.get("log_sequence") or state.get("log_count") or 0
                    ),
                    "anchor_message_count": int(state.get("anchor_message_count") or 0),
                    "memory_checkpoint": str(state.get("memory_checkpoint") or ""),
                    "memory_checkpoint_tokens": int(
                        state.get("memory_checkpoint_tokens") or 0
                    ),
                    "memory_checkpoint_created_at": state.get(
                        "memory_checkpoint_created_at"
                    ),
                    "pending_messages": None,
                },
            }
            tmp_path = path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.warning(f"⚠️ Failed to save anchored context for {chat_name}: {e}")

    def _role_prompt_uses_dynamic_slots(self, role_prompt: str) -> bool:
        """Return whether a role prompt embeds per-turn dynamic variables itself."""
        if not role_prompt:
            return False
        dynamic_slots = ("chat_text", "search_results", "sender", "content")
        return any(
            re.search(r"\{\{\s*" + re.escape(slot) + r"\s*\}\}", role_prompt)
            or re.search(r"\{\s*" + re.escape(slot) + r"\s*\}", role_prompt)
            for slot in dynamic_slots
        )

    def _build_dynamic_input_block(self, context_text: str, search_text: str) -> str:
        """Append per-turn inputs after the static prompt prefix for better cache locality."""
        search_section = (search_text or "").strip() or "（本轮无可用搜索结果）"
        return f"""

【动态输入资料】
以下资料每轮可能变化。当前用户消息以最后一条 user message 为准；最近原始聊天优先于摘要，搜索结果优先于长期记忆。
不要复述资料结构，不要提“长期记忆/搜索结果/上下文”这些来源名，直接像群友一样接话。

## 群聊上下文
{context_text}

## 网络搜索结果
{search_section}
"""

    def _build_human_like_output_contract(self, role_name: str) -> str:
        settings = self.role_manager.get_output_settings(role_name)
        if not settings.get("enabled"):
            return ""

        max_chars = settings.get("max_chars", 120)
        max_count = settings.get("max_count", 3)
        strip_period = settings.get("strip_trailing_period", True)
        period_rule = "每条消息结尾不要用句号或中文句号。" if strip_period else ""
        return f"""【微信回复输出协议】
你必须只返回合法 json，不要返回 Markdown、代码块、解释文本或额外字段。

JSON 格式示例：
{{
  "status": "answered",
  "messages": ["自然的一条微信回复"]
}}

status 只能是 answered、not_found、blocked：
- answered：当前回复已经给出答案或交付结果
- not_found：已合理尝试现有工具仍无法确认，并在消息中说明当前结论与尝试范围
- blocked：缺少继续所必需的用户输入或授权，并在消息中明确需要什么
不存在 continue 或 suppressed 状态，不得用空数组表示沉默。
messages 是你要发送到微信的消息数组。请像真实微信用户一样回复：短、自然、即时，不要写成文章。
能一句话说清就只返回 1 条。
只有在信息确实较多、或自然聊天节奏需要停顿时，才拆成 2 到 {max_count} 条。
通常不要超过 {max_count} 条。每条尽量短，目标约 {max_chars} 字以内，但不要机械截断句子。
不要为了凑条数而拆分，不要使用标题、列表、总结腔或客服腔。
{period_rule}
"""

    def _build_reply_completion_contract(self, role_name: str) -> str:
        terminal_contract = """【任务完成与终态规则】
一条用户消息对应当前一个 Codex turn。所有安全且已获授权的搜索、读取和其他工具调用，都必须在这个 turn 内完成后再给最终回复。
如果还有能实质推进原请求的安全工具动作，立即调用工具，不要先输出进度或结束本 turn。
只有以下情况可以结束：已经给出答案或交付结果；合理尝试后仍无法确认；确实缺少继续所必需的用户输入或授权。
最终回复是终态，不得说“我继续找”“正在搜索”“稍后回复”“查到再告诉你”等未来工作承诺。除非宿主明确提供了可持久化后台任务及任务 ID，否则不能声称会在本回复之后自行继续。
不要把 JSON 协议或空响应包装成一条字符串消息。被动触发和已经通过主动回复判断的请求都必须给出非空终态结果。"""
        output_contract = self._build_human_like_output_contract(role_name)
        return (
            f"{terminal_contract}\n\n{output_contract}"
            if output_contract
            else terminal_contract
        )

    def _get_human_like_response_format(self, role_name: str) -> Optional[Dict[str, str]]:
        settings = self.role_manager.get_output_settings(role_name)
        if not settings.get("enabled"):
            return None
        return {"type": "json_object"}

    def _request_codex_reply(
        self,
        chat_name: str,
        role_name: str,
        messages: List[Dict],
        **kwargs,
    ) -> str:
        response_format = self._get_human_like_response_format(role_name)
        requested_search_mode = str(kwargs.get("_mabobot_web_search_mode") or "").strip().lower()
        if requested_search_mode not in {"disabled", "cached", "indexed", "live"}:
            requested_search_mode = ""
        search_enabled = self._is_search_enabled() or bool(requested_search_mode)
        output_schema = None
        max_output_messages = 0
        if response_format:
            settings = self.role_manager.get_output_settings(role_name)
            max_output_messages = max(1, int(settings.get("max_count", 3) or 3))
            output_schema = terminal_reply_output_schema(max_output_messages)

        def call_codex(call_messages: List[Dict], *, retry: bool = False) -> str:
            from app.services.codex_profile_service import CodexProfileService

            result = get_assistant_reply_gateway().reply(
                CodexReplyRequest(
                    chat_name=chat_name,
                    role_name=role_name,
                    codex_profile_id=CodexProfileService.resolve_assistant_profile_id(
                        self._get_user_permission_config(chat_name).get("codex_profile_id")
                    ),
                    messages=call_messages,
                    persistent_session=self.codex_persistent_session_enabled,
                    retry=retry,
                    reasoning_effort=self.codex_reasoning_effort,
                    reasoning_summary=self.codex_reasoning_summary,
                    web_search_mode=(
                        requested_search_mode
                        or (self.codex_web_search_mode if search_enabled else "disabled")
                    ),
                    timeout_seconds=self.codex_turn_timeout_seconds,
                    max_turns=self.codex_max_turns_per_thread,
                    allow_exec_fallback=self.codex_exec_fallback_enabled,
                    output_schema=output_schema,
                    input_files=kwargs.get("_mabobot_input_files") or (),
                    allow_image_input=bool(kwargs.get("_mabobot_allow_image_input")),
                    memory_trace=(
                        kwargs.get("_mabobot_memory_trace")
                        if isinstance(kwargs.get("_mabobot_memory_trace"), dict)
                        else None
                    ),
                    history_mode=str(kwargs.get("_mabobot_history_mode") or "full"),
                    usage_capture=(
                        kwargs.get("_mabobot_usage_capture")
                        if isinstance(kwargs.get("_mabobot_usage_capture"), list)
                        else None
                    ),
                )
            )
            attachment_capture = kwargs.get("_mabobot_attachment_capture")
            if isinstance(attachment_capture, list):
                attachment_capture.clear()
                attachment_capture.extend(result.attachments)
            # Never run Markdown cleanup across a JSON envelope: underscores in
            # ``not_found`` or user-visible code/file names can otherwise be
            # mistaken for emphasis delimiters and corrupt valid JSON.
            return (
                str(result.text or "").strip()
                if response_format
                else self._strip_markdown(result.text)
            )

        def validate_terminal(response_text: str):
            if response_format:
                return validate_structured_terminal_reply(
                    response_text,
                    max_messages=max_output_messages,
                )
            return validate_plain_terminal_reply(response_text)

        def has_materialized_attachment() -> bool:
            attachment_capture = kwargs.get("_mabobot_attachment_capture")
            if not isinstance(attachment_capture, list):
                return False
            for attachment in attachment_capture:
                if not isinstance(attachment, dict):
                    continue
                raw_path = str(attachment.get("path") or "").strip()
                if raw_path:
                    path = Path(raw_path)
                    if path.exists() and path.is_file():
                        return True
            return False

        response = call_codex(messages)
        validation = validate_terminal(response)
        if validation.valid:
            return response

        if has_materialized_attachment():
            # The artifact is already a concrete terminal result. Do not ask the
            # model to regenerate it, and never leak malformed protocol/status
            # text alongside the valid file.
            logger.warning(
                "🤖 Suppressing invalid terminal text because attachment(s) already exist: "
                "chat=%s code=%s",
                chat_name,
                validation.code,
            )
            return ""

        retry_messages = messages + [
            {"role": "assistant", "content": response or ""},
            {
                "role": "user",
                "content": (
                    f"上一次最终回复被宿主的确定性终态校验拒绝，错误码：{validation.code}。"
                    "这不是让你汇报进度或换个说法结束。请继续完成原始用户请求；"
                    "如果仍有能推进任务的安全工具动作，现在执行。只有真正得到终态后再回复。"
                    + (
                        "只返回合法 json，格式为 "
                        "{\"status\":\"answered|not_found|blocked\","
                        "\"messages\":[\"一条自然微信回复\"]}。"
                        if response_format
                        else "返回非空的最终答案，不要承诺稍后继续。"
                    )
                ),
            },
        ]
        logger.warning(
            "🤖 Assistant terminal reply rejected; retrying once on the same Codex thread: "
            "chat=%s code=%s",
            chat_name,
            validation.code,
        )
        retry_response = call_codex(retry_messages, retry=True)
        retry_validation = validate_terminal(retry_response)
        if retry_validation.valid:
            return retry_response
        if has_materialized_attachment():
            logger.warning(
                "🤖 Suppressing invalid retry text because attachment(s) already exist: "
                "chat=%s code=%s",
                chat_name,
                retry_validation.code,
            )
            return ""
        raise AssistantReplyError(
            "Codex assistant failed deterministic terminal validation after one correction: "
            f"{retry_validation.code}"
        )

    def _is_valid_human_like_response(self, response: str) -> bool:
        return validate_structured_terminal_reply(response or "").valid

    def _strip_internal_action_markers(self, response: str) -> str:
        """移除模型误输出的内部动作占位文本，例如 [发送文件]。"""
        lines = []
        for line in str(response or "").splitlines():
            if line.strip() in ChatLogManager.INTERNAL_ACTION_MARKERS:
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    def _send_response_parts(
        self,
        wx_manager,
        chat_name: str,
        response: str,
        role_name: str,
        silent: bool = False,
        log_response: Optional[str] = None,
        display_suffix: str = "",
    ) -> bool:
        settings = self.role_manager.get_output_settings(role_name)
        parts = self._format_response_parts(response, settings)
        if not parts:
            return False

        clean_log_response = response if log_response is None else log_response
        log_parts = self._format_response_parts(clean_log_response, settings)

        interval = float(settings.get("interval_seconds", 0.0) or 0.0)
        send_session = getattr(wx_manager, "outbound_send_session", None)
        session_context = send_session() if callable(send_session) and len(parts) > 1 else nullcontext()
        with session_context:
            for index, part in enumerate(parts):
                if index > 0 and interval > 0:
                    time.sleep(interval)
                display_part = part
                if display_suffix and index == len(parts) - 1:
                    display_part = f"{display_part}{display_suffix}"
                if not wx_manager.send_message(chat_name, display_part, silent=silent):
                    return False
                if index < len(log_parts):
                    self._save_response_part_to_log(chat_name, log_parts[index])
        return True

    def _send_response_attachments(self, wx_manager, chat_name: str, attachments: List[Dict[str, Any]]) -> bool:
        """发送模型 provider 返回的文件附件。"""
        if not attachments:
            return True

        try:
            # The attachment collector writes into the artifact root selected by
            # the same per-chat access policy.  Re-resolve that trusted policy
            # here instead of accepting a root supplied by the model response.
            # This permits the current chat's isolated output directory while
            # still preventing one chat from sending another chat's files.
            access_context = codex_access_service.for_chat(chat_name, ensure=False)
            configured_root = Path(access_context.artifact_root)
            is_junction = getattr(configured_root, "is_junction", None)
            if configured_root.is_symlink() or (
                callable(is_junction) and is_junction()
            ):
                logger.error("🤖 聊天附件目录是链接，已拒绝发送: %s", configured_root)
                return False
            allowed_root = configured_root.resolve(strict=True)
            if not allowed_root.is_dir():
                return False
        except Exception:
            logger.exception("🤖 无法解析聊天附件的允许目录: %s", chat_name)
            return False
        allowed_suffixes = {
            ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
            ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx",
            ".rtf", ".odt", ".ods", ".odp", ".epub",
            ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
            ".xml", ".html", ".htm", ".log", ".sql",
            ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz",
            ".mp3", ".wav", ".m4a", ".flac", ".mp4", ".mov", ".webm", ".avi", ".mkv",
        }
        file_paths: List[str] = []
        seen = set()
        rejected_count = 0

        for attachment in attachments:
            if not isinstance(attachment, dict):
                rejected_count += 1
                continue
            if attachment.get("type") not in (None, "image", "file"):
                rejected_count += 1
                continue
            raw_path = attachment.get("path")
            if not raw_path:
                rejected_count += 1
                continue

            path = Path(str(raw_path)).expanduser()
            try:
                resolved = path.resolve()
                resolved.relative_to(allowed_root)
            except Exception:
                logger.warning(f"🤖 跳过不在允许目录内的附件: {raw_path}")
                rejected_count += 1
                continue

            if resolved.suffix.lower() not in allowed_suffixes:
                logger.warning(f"🤖 跳过不支持的附件类型: {resolved}")
                rejected_count += 1
                continue
            if not resolved.exists() or not resolved.is_file():
                logger.warning(f"🤖 跳过不存在的附件: {resolved}")
                rejected_count += 1
                continue
            if str(resolved) in seen:
                continue

            seen.add(str(resolved))
            file_paths.append(str(resolved))

        if rejected_count or not file_paths:
            logger.error(
                "🤖 拒绝发送不完整或无效的模型附件: chat=%s accepted=%s rejected=%s",
                chat_name,
                len(file_paths),
                rejected_count,
            )
            return False

        logger.info(f"🤖 Sending {len(file_paths)} model attachment(s) to {chat_name}")
        return bool(wx_manager.send_files(chat_name, file_paths))

    def _save_response_parts_to_log(self, chat_name: str, response: str, role_name: str) -> None:
        settings = self.role_manager.get_output_settings(role_name)
        for part in self._format_response_parts(response, settings):
            self._save_response_part_to_log(chat_name, part)

    def _save_response_part_to_log(self, chat_name: str, part: str) -> None:
        self.chat_log_manager.save_message(
            chat_name,
            self._bot_display_name_for_chat(chat_name),
            part,
            is_bot=True,
        )

    def _format_response_parts(self, response: str, settings: Dict[str, Any]) -> List[str]:
        text = self._strip_internal_action_markers(response)
        if not text:
            return []

        if not settings.get("enabled"):
            return [text]

        strip_period = bool(settings.get("strip_trailing_period", True))

        normalized = []
        for part in self._parse_model_response_parts(text):
            clean = self._clean_response_part(part)
            if clean in ChatLogManager.INTERNAL_ACTION_MARKERS:
                continue
            if strip_period:
                clean = re.sub(r"[。．.]+$", "", clean).strip()
            if clean:
                normalized.append(clean)
        if normalized:
            return normalized
        if text.lstrip().startswith(("{", "[")):
            return []
        return [text]



    def _parse_model_response_parts(self, text: str) -> List[str]:
        json_messages = self._extract_human_like_messages(text)
        if json_messages is not None:
            return json_messages

        # 1. 优先检查显式分隔符
        if "||SPLIT||" in text:
            return [part.strip() for part in text.split("||SPLIT||") if part.strip()]

        # 2. 检查 JSON 格式 (List 或包含 messages 的 Dict)
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [
                    str(item).strip()
                    for item in parsed
                    if str(item).strip() and not is_serialized_reply_protocol(item)
                ]
            if isinstance(parsed, dict) and isinstance(parsed.get("messages"), list):
                return [
                    str(item).strip()
                    for item in parsed["messages"]
                    if str(item).strip() and not is_serialized_reply_protocol(item)
                ]
        except Exception:
            pass

        # 3. 检查 JSONL 格式 (每行一个 JSON 对象)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > 1:
            jsonl_parts = []
            is_jsonl = True
            for line in lines:
                try:
                    item = json.loads(line)
                    if isinstance(item, str):
                        jsonl_parts.append(item.strip())
                    elif isinstance(item, dict):
                        value = item.get("text") or item.get("content") or item.get("message")
                        if value:
                            jsonl_parts.append(str(value).strip())
                        else:
                            is_jsonl = False; break
                    else:
                        is_jsonl = False; break
                except Exception:
                    is_jsonl = False; break
            
            if is_jsonl and jsonl_parts:
                return jsonl_parts

        return [text]

    def _extract_human_like_messages(self, raw_text: str) -> Optional[List[str]]:
        """解析 Human-like Output 的 JSON 包装协议。"""
        if not raw_text:
            return None

        text = self._sanitize_judge_response_text(raw_text)
        candidates = [text]
        snippet = self._extract_balanced_json_snippet(text)
        if snippet and snippet not in candidates:
            candidates.append(snippet)

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue

            if isinstance(parsed, dict):
                messages = parsed.get("messages")
            elif isinstance(parsed, list):
                messages = parsed
            else:
                continue

            if not isinstance(messages, list):
                continue

            parts = []
            for item in messages:
                if isinstance(item, dict):
                    value = item.get("text") or item.get("content") or item.get("message")
                else:
                    value = item
                clean = str(value or "").strip()
                if clean and not is_serialized_reply_protocol(clean):
                    parts.append(clean)

            if parts:
                return parts

        return None

    def _clean_response_part(self, text: str) -> str:
        clean = (text or "").strip()
        # 移除开头的无意义标点和空白
        clean = re.sub(r"^[，,。．.；;、\s]+", "", clean).strip()
        # 将连续的两个或更多换行符（空行）替换为单个换行符
        clean = re.sub(r"\n{2,}", "\n", clean)
        return clean



    def _process_quoted_image(self, message: Dict[str, Any], wx_manager) -> Optional[str]:
        """处理引用的图片，返回 base64 编码
        使用新的按需下载机制。
        """
        try:
            chat_name = message.get("chat_name", "")
            message_id = message.get("message_id")
            quote_image_path = message.get("quote_image_path")
            has_quote_image = message.get("has_quote_image", False)

            image_path = None

            # 1) 如果已经有图片路径，直接使用
            if quote_image_path and Path(quote_image_path).exists():
                image_path = quote_image_path
                logger.debug(f"🤖 使用已有引用图片路径: {image_path}")
            # 2) 如果有引用图片标记但没有路径，进行按需下载
            elif has_quote_image and wx_manager and message_id:
                try:
                    logger.info(f"🤖 开始按需下载引用图片: {chat_name}:{message_id}")
                    image_path = wx_manager.download_quote_image(chat_name, message_id=message_id)

                except Exception as e:
                    logger.error(f"🤖 按需下载引用图片失败: {e}")
                    image_path = None


            if not image_path or not Path(image_path).exists():
                # 引用图片必须精确来自当前引用消息。绝不能用“最近一张图”
                # 猜测，否则后台图片下载的前一张/后一张图都可能串入。
                logger.error(
                    "🤖 引用图片精确路径不存在或下载失败，已拒绝使用最近图片: "
                    "chat=%s message_id=%s requested_path=%s downloaded_path=%s",
                    chat_name,
                    message_id,
                    quote_image_path,
                    image_path,
                )
                return None

            # 读取并编码为base64
            with open(image_path, "rb") as f:
                image_base64 = base64.b64encode(f.read()).decode("utf-8")



            return image_base64
        except Exception as e:
            logger.error(f"🤖 处理引用图片失败: {e}")
            return None

    def _process_quoted_video_frames(
        self,
        message: Dict[str, Any],
        wx_manager,
    ) -> List[Dict[str, Any]]:
        """下载精确引用的视频，只返回四张本地抽帧的 base64。"""
        chat_name = str(message.get("chat_name") or "")
        message_id = message.get("message_id")
        requested_path = str(message.get("quote_video_path") or "").strip()
        video_path = requested_path if requested_path and Path(requested_path).is_file() else ""
        try:
            if not video_path and message.get("has_quote_video") and wx_manager and message_id:
                logger.info("🤖 开始按需下载引用视频: %s:%s", chat_name, message_id)
                video_path = str(
                    wx_manager.download_quote_video(chat_name, message_id=message_id)
                    or ""
                ).strip()
            if not video_path or not Path(video_path).is_file():
                logger.error(
                    "🤖 引用视频精确路径不存在或下载失败: "
                    "chat=%s message_id=%s requested_path=%s downloaded_path=%s",
                    chat_name,
                    message_id,
                    requested_path,
                    video_path,
                )
                return []

            with tempfile.TemporaryDirectory(prefix="mabobot_quote_video_") as frame_dir:
                samples = extract_evenly_spaced_frames(
                    video_path,
                    frame_dir,
                    count=4,
                    max_dimension=1280,
                )
                if len(samples) != 4:
                    raise RuntimeError(f"引用视频抽帧数量异常: {len(samples)}/4")
                visual_inputs = []
                for sample in samples:
                    encoded = base64.b64encode(sample.path.read_bytes()).decode("ascii")
                    visual_inputs.append(
                        {
                            "base64": encoded,
                            "position_percent": sample.position_percent,
                            "timestamp_seconds": sample.timestamp_seconds,
                            "duration_seconds": sample.duration_seconds,
                        }
                    )
                logger.info(
                    "🎬 引用视频已在后台抽取 4 帧: chat=%s duration=%.1fs",
                    chat_name,
                    samples[0].duration_seconds,
                )
                return visual_inputs
        except Exception as e:
            logger.error("🤖 处理引用视频抽帧失败: %s", e, exc_info=True)
            return []

    def _detect_misidentified_quote_image(self, content: str) -> Optional[Dict[str, str]]:
        """检测被误识别为text的引用图片消息

        Args:
            content: 消息内容

        Returns:
            如果检测到引用图片特征，返回 {"prefix": "实际内容", "quoted_sender": "被引用者"}
            否则返回 None
        """
        # 正则匹配: "前缀内容引用 xxx 的消息 : 图片"
        pattern = r'^(.*)引用\s+(.+?)\s+的消息\s*[:：]\s*(图片|\[图片\])$'
        match = re.match(pattern, content.strip(), re.DOTALL)

        if match:
            prefix = match.group(1).strip()
            quoted_sender = match.group(2).strip()

            logger.info(f"🔧 检测到误识别的引用图片消息: prefix='{prefix}', quoted='{quoted_sender}'")
            return {
                "prefix": prefix,
                "quoted_sender": quoted_sender
            }

        return None

    def _get_user_permission_config(self, chat_name: str) -> Dict[str, Any]:
        """Return the first-class assistant policy for a managed chat."""
        try:
            from app.models.base import SessionLocal
            from app.models.assistant_policy import AssistantChatPolicy
            from app.models.user_permission import WeChatUser

            with SessionLocal() as db:
                user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat_name).first()
                if not user:
                    return {}

                policy = db.query(AssistantChatPolicy).filter(
                    AssistantChatPolicy.user_id == user.id,
                    AssistantChatPolicy.enabled.is_(True),
                ).first()

                if policy:
                    return {
                        "proactive_enabled": policy.proactive_enabled,
                        "followup_enabled": bool(policy.followup_enabled),
                        "followup_window_seconds": int(policy.followup_window_seconds or 60),
                        "followup_merge_seconds": int(policy.followup_merge_seconds or 3),
                        "followup_max_turns": int(policy.followup_max_turns or 3),
                        "memory_profile": policy.memory_profile,
                        "ignored_senders": policy.ignored_senders,
                        "codex_profile_id": policy.codex_profile_id,
                    }
            return {}
        except Exception as e:
            logger.error(f"❌ Error getting user permission config: {e}")
            return {}

    @staticmethod
    def _default_codex_profile_id() -> str:
        try:
            from app.services.codex_profile_service import CodexProfileService

            return CodexProfileService.default_profile_id()
        except Exception:
            return ""

    def _get_ignored_senders(self, chat_name: str) -> List[str]:
        """Return normalized sender names ignored by this chat's assistant policy."""
        raw_value = self._get_user_permission_config(chat_name).get("ignored_senders")
        if not raw_value:
            return []

        try:
            parsed = json.loads(raw_value)
        except Exception:
            parsed = raw_value.splitlines()

        if not isinstance(parsed, list):
            return []

        ignored = []
        for item in parsed:
            sender = str(item or "").strip()
            if sender:
                ignored.append(sender)
        return ignored

    def _is_sender_ignored(self, chat_name: str, sender: str) -> bool:
        if not chat_name or not sender:
            return False
        return sender.strip() in self._get_ignored_senders(chat_name)

    def _get_default_memory_config(self) -> Dict[str, Any]:
        declared = memory_config_defaults()
        return {
            "context_message_fetch_limit": self.context_message_fetch_limit,
            "context_window_strategy": self.context_window_strategy,
            "anchor_message_count": self.anchor_message_count,
            "anchor_rollover_prompt_tokens": self.anchor_rollover_prompt_tokens,
            **{
                key: getattr(self, key, default)
                for key, default in declared.items()
            },
        }

    def _sanitize_memory_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        sanitized = dict(config)
        sanitized.update(sanitize_memory_config(config))
        for key, lower, upper, fallback in (
            ("context_message_fetch_limit", 20, 20000, self.context_message_fetch_limit),
            ("anchor_message_count", 20, 5000, self.anchor_message_count),
            ("anchor_rollover_prompt_tokens", 4096, 1000000, self.anchor_rollover_prompt_tokens),
        ):
            try:
                value = int(config.get(key, fallback))
            except (TypeError, ValueError):
                value = int(fallback)
            sanitized[key] = max(lower, min(upper, value))
        strategy = str(
            config.get("context_window_strategy")
            or self.context_window_strategy
            or "anchored_append"
        ).strip().lower()
        sanitized["context_window_strategy"] = (
            strategy if strategy in {"sliding", "anchored_append"} else "anchored_append"
        )
        return sanitized

    def _memory_source_fetch_limit(self, memory_config: Dict[str, Any]) -> int:
        """Reply construction only needs the configured recent raw window."""
        limit = int(memory_config.get("context_message_fetch_limit") or 300)
        return max(20, min(20000, limit))

    def _get_chat_memory_config(self, chat_name: str) -> Dict[str, Any]:
        """合并全局默认与该群/用户的 Memory Profile 覆盖项。"""
        config = self._get_default_memory_config()
        try:
            perm_config = self._get_user_permission_config(chat_name)
            raw_profile = perm_config.get("memory_profile")
            if raw_profile:
                profile = json.loads(raw_profile)
                if isinstance(profile, dict) and profile.get("enabled"):
                    overrides = upgrade_memory_config_keys(
                        profile.get("overrides")
                        if isinstance(profile.get("overrides"), dict)
                        else profile
                    )
                    for key in config.keys():
                        if key in overrides and overrides[key] is not None:
                            config[key] = overrides[key]
                    logger.debug(f"🧠 Memory profile applied for {chat_name}: {config}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load memory profile for {chat_name}: {e}")

        sanitized = self._sanitize_memory_config(config)
        # Bot replies are present in the same raw chat log as human messages.
        # They must never be learned back as a group member profile.
        sanitized["memory_person_excluded_sender_names"] = self._bot_names_for_chat(chat_name)
        sanitized["memory_person_excluded_sender_ids"] = []
        return sanitized

    def _analyze_chat_state(self, chat_name: str, scan_threshold: Optional[int] = None) -> Dict[str, Any]:
        """分析聊天状态：计算自上次机器人回复后的消息数和时间"""
        try:
            # 读取足够多的历史消息以找到机器人上次的回复
            # 扫描范围：阈值 * 2 或 至少100条
            threshold = 0 if scan_threshold is None else scan_threshold
            scan_limit = max(100, threshold * 3)
            messages = self.chat_log_manager.get_context_messages(chat_name, limit=scan_limit)

            if not messages:
                return {"msg_count": 0, "last_reply_time": None}

            # 倒序查找机器人的最后一条消息
            last_bot_index = -1
            for i in range(len(messages) - 1, -1, -1):
                # 简单匹配发送者名称 (可能需要更严谨的判断，但目前足够)
                if self._is_assistant_reply_record(messages[i]):
                    last_bot_index = i
                    break

            if last_bot_index == -1:
                # 范围内没找到机器人回复 -> 视为无限久
                # 返回当前消息总数作为计数
                msg_count = len(messages)
                last_time = None
            else:
                # 计数：最后一条机器人消息之后的消息数
                msg_count = len(messages) - 1 - last_bot_index

                # 解析时间
                last_time_str = messages[last_bot_index].get("time")
                try:
                    # 尝试解析标准格式
                    last_time = datetime.strptime(last_time_str, "%Y-%m-%d %H:%M:%S")
                except:
                    last_time = None

            return {"msg_count": msg_count, "last_reply_time": last_time}
        except Exception as e:
            logger.error(f"❌ Error analyzing chat state: {e}")
            return {"msg_count": 0, "last_reply_time": None}

    def _is_assistant_reply_record(self, message: Dict[str, Any]) -> bool:
        """判断聊天记录中的机器人消息是否应视为 Assistant 发言。"""
        if not message.get("is_bot") and message.get("sender") != self.bot_name:
            return False

        content = str(message.get("content") or "").strip()
        if not content:
            return False

        # summary_plus 和其他工具型插件也会通过同一个微信账号发言。
        # 这些工具输出不应重置角色主动 Judge 的消息计数，否则大群里频繁摘要会压住角色插话。
        if self.chat_log_manager._is_internal_action_message(message):
            return False
        summary_markers = ("📖 一句话总结", "🔑 关键要点", "🏷 标签")
        if any(marker in content for marker in summary_markers):
            return False

        return True

    def _is_tool_output_record(self, message: Dict[str, Any]) -> bool:
        """判断聊天记录中的机器人消息是否为工具型输出。"""
        if not message.get("is_bot") and message.get("sender") != self.bot_name:
            return False
        return not self._is_assistant_reply_record(message)

    def _get_judge_context_messages(self, chat_name: str, limit: int = 20) -> List[Dict[str, Any]]:
        """获取 Judge 上下文，保留工具摘要但避免误判为角色刚发言。"""
        raw_messages = self.chat_log_manager.get_context_messages(chat_name, limit * 3)
        normalized = [self._normalize_judge_context_message(msg) for msg in raw_messages]
        return normalized[-limit:]

    def _normalize_judge_context_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """把工具型机器人输出改写为背景消息，供 Judge 正确理解。"""
        if not self._is_tool_output_record(message):
            return message

        normalized = dict(message)
        content = str(normalized.get("content") or "").strip()
        normalized["sender"] = "工具摘要"
        normalized["content"] = f"【工具输出，不代表{self.bot_name}角色发言】{content}"
        return normalized

    def _build_judge_output_guard(self) -> str:
        """统一的 Judge 输出约束（无需手写在 Prompt 里）。"""
        return (
            "你必须只输出一个 JSON 对象，不要输出 Markdown、代码块、解释文本。"
            "JSON 必须包含以下字段："
            '{"should_reply": true/false, "reason": "string", "atmosphere": "string"}。'
            "其中 should_reply 必须是布尔值。"
        )

    def _sanitize_judge_response_text(self, raw_text: str) -> str:
        """清洗 Judge 原始输出，移除常见的不可见字符和 markdown 包裹。"""
        if not isinstance(raw_text, str):
            raw_text = str(raw_text or "")

        text = raw_text.strip().lstrip("\ufeff")
        text = text.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\u2060", "")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```\s*$", "", text)
        return text.strip()

    def _extract_balanced_json_snippet(self, text: str) -> Optional[str]:
        """提取第一个花括号平衡的 JSON 对象片段，忽略字符串内的大括号。"""
        start_idx = text.find("{")
        if start_idx == -1:
            return None

        depth = 0
        in_string = False
        escape = False

        for idx in range(start_idx, len(text)):
            char = text[idx]

            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start_idx : idx + 1]

        return None

    def _extract_first_json_object(self, raw_text: str) -> Optional[Dict[str, Any]]:
        """从模型回复中提取第一个可解析 JSON 对象。"""
        if not raw_text:
            return None

        text = self._sanitize_judge_response_text(raw_text)
        candidates: List[str] = [text]
        decoder = json.JSONDecoder()

        # 1) 优先提取 markdown json 代码块
        code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if code_block_match:
            candidates.insert(0, code_block_match.group(1).strip())

        # 2) 回退：提取第一个括号平衡的 JSON 对象
        balanced_json = self._extract_balanced_json_snippet(text)
        if balanced_json:
            candidates.append(balanced_json.strip())

        for candidate in candidates:
            try:
                return json.loads(candidate)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

            try:
                parsed_obj, _ = decoder.raw_decode(candidate.lstrip())
                if isinstance(parsed_obj, dict):
                    return parsed_obj
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return None

    def _normalize_judge_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """标准化 Judge 结果，容忍轻微字段偏差。"""
        raw_decision = result.get(
            "should_reply",
            result.get("reply", result.get("need_reply", result.get("decision", False)))
        )

        if isinstance(raw_decision, bool):
            should_reply = raw_decision
        elif isinstance(raw_decision, (int, float)):
            should_reply = raw_decision != 0
        elif isinstance(raw_decision, str):
            value = raw_decision.strip().lower()
            if value in {"true", "1", "yes", "y", "reply", "需要", "是"}:
                should_reply = True
            elif value in {"false", "0", "no", "n", "silent", "stay silent", "不需要", "否"}:
                should_reply = False
            else:
                should_reply = False
        else:
            should_reply = False

        reason = (
            result.get("reason")
            or result.get("decision_reason")
            or result.get("why")
            or "No reason provided"
        )
        atmosphere = result.get("atmosphere") or result.get("mood") or ""

        return {
            "should_reply": bool(should_reply),
            "reason": str(reason),
            "atmosphere": str(atmosphere),
        }

    def _consult_judge(self, context_messages: List[Dict], role_name: str, judge_name: str) -> bool:
        """咨询裁判是否应该插话（使用 LLM Manager）"""
        try:
            # 使用统一的格式化方法
            chat_text = self._format_chat_text(context_messages)

            # 根据用户绑定 Judge 渲染判断提示词（simple/template 双模式）
            prompt = self.judge_manager.get_judge_prompt(
                judge_name,
                variables={"chat_text": chat_text},
            )
            if not prompt:
                logger.warning(f"⚖️ Judge prompt empty for judge '{judge_name}'")
                return False

            # 使用 LLM Manager 调用 judge
            messages = [
                {"role": "system", "content": self._build_judge_output_guard()},
                {"role": "user", "content": prompt},
            ]

            # 尝试使用原生 JSON 模式
            # 注意：LiteLLM/DeepSeek 支持 response_format={"type": "json_object"}
            response_text = self._call_auxiliary_model(
                "judge",
                messages,
                response_format={"type": "json_object"},
            )

            logger.debug(f"⚖️ Judge raw response: {response_text}")

            # 解析 JSON 响应
            try:
                parsed_json = self._extract_first_json_object(response_text)
                if not parsed_json:
                    logger.error(
                        "❌ Failed to extract JSON from judge response. Raw repr: %r, sanitized repr: %r",
                        response_text,
                        self._sanitize_judge_response_text(response_text),
                    )
                    return False

                normalized = self._normalize_judge_result(parsed_json)
                should_reply = normalized["should_reply"]
                reason = normalized["reason"]
                atmosphere = normalized["atmosphere"]

                append_dashboard_event(
                    "judge_decision",
                    {
                        "should_reply": should_reply,
                        "reason": reason,
                        "atmosphere": atmosphere,
                        "role_name": role_name,
                        "judge_name": judge_name,
                    }
                )

                if should_reply:
                    logger.info(f"⚖️ Judge[{judge_name}] decided to REPLY: {reason}")
                else:
                    logger.info(f"⚖️ Judge[{judge_name}] decided to STAY SILENT: {reason}")

                return should_reply
            except (json.JSONDecodeError, AttributeError, ValueError) as e:
                logger.error(
                    "❌ Failed to parse judge response as JSON. Raw repr: %r, sanitized repr: %r, error: %s",
                    response_text[:500] if isinstance(response_text, str) else response_text,
                    self._sanitize_judge_response_text(response_text)[:500],
                    e,
                )
                return False

        except Exception as e:
            logger.error(f"❌ Judge consultation failed: {e}")
            return False

    def _call_auxiliary_model(
        self,
        task_type: str,
        messages: List[Dict],
        **kwargs,
    ) -> str:
        """调用 Judge 等辅助模型；此入口不允许生成最终对话回复。

        Args:
            task_type: 辅助任务类型（judge 或 followup_judge）
            messages: OpenAI 格式的消息数组
            **kwargs: 额外参数（如 response_format）

        Returns:
            模型返回的文本内容
        """
        if task_type not in {"judge", "followup_judge"}:
            raise ValueError(f"不支持的 Assistant 辅助模型任务: {task_type}")
        try:
            llm_manager = get_llm_manager()
            response = llm_manager.call(
                plugin_name="assistant",
                call_type=task_type,
                messages=messages,
                **kwargs
            )
            return self._strip_markdown(response)
        except Exception as e:
            logger.error("🤖 Assistant 辅助模型调用失败 (%s): %s", task_type, e)
            raise

    def _reconcile_memory_trace(
        self,
        memory_trace: Optional[Dict[str, Any]],
        messages: List[Dict],
    ) -> Optional[Dict[str, Any]]:
        """Verify trace entries against the final messages sent to the model."""
        if not memory_trace:
            return None
        trace = json.loads(json.dumps(memory_trace, ensure_ascii=False))
        memory_text = self._extract_injected_memory_text(messages)
        trace["final_prompt_verified"] = True

        if trace.get("enabled") is False:
            trace["tokens"] = 0
            return trace

        injected_events = []
        dropped_events = list(trace.get("dropped_events") or [])
        dropped_event_ids = {int(item.get("id") or 0) for item in dropped_events}
        for event in trace.get("events") or []:
            prompt_text = str(event.get("prompt_text") or "")
            if prompt_text and prompt_text in memory_text:
                injected_events.append(event)
                continue
            value = dict(event)
            value["prompt_text"] = ""
            value["drop_reason"] = "final_prompt_budget"
            event_id = int(value.get("id") or 0)
            if event_id not in dropped_event_ids:
                dropped_events.append(value)
                dropped_event_ids.add(event_id)
        trace["events"] = injected_events
        trace["dropped_events"] = dropped_events

        injected_people = []
        dropped_people = list(trace.get("dropped_people") or [])
        dropped_people_keys = {
            (str(item.get("name") or ""), int(item.get("source_event_id") or 0))
            for item in dropped_people
        }
        for person in trace.get("people") or []:
            prompt_text = str(person.get("prompt_text") or "")
            if prompt_text and prompt_text in memory_text:
                injected_people.append(person)
                continue
            value = dict(person)
            value["prompt_text"] = ""
            value["drop_reason"] = "final_prompt_budget"
            key = (
                str(value.get("name") or ""),
                int(value.get("source_event_id") or 0),
            )
            if key not in dropped_people_keys:
                dropped_people.append(value)
                dropped_people_keys.add(key)
        trace["people"] = injected_people
        trace["dropped_people"] = dropped_people

        stage = dict(trace.get("stage") or {})
        stage_prompt = str(stage.get("prompt_text") or "")
        if stage_prompt and stage_prompt in memory_text:
            stage["included"] = True
        else:
            actual_stage = self._extract_memory_section(
                memory_text,
                "## 当前阶段记忆",
                ("## 本轮相关人物资料", "## 检索到的相关历史事件"),
            )
            if actual_stage:
                stage["included"] = True
                stage["prompt_text"] = actual_stage
                stage["text"] = actual_stage.replace(
                    "## 当前阶段记忆",
                    "",
                    1,
                ).lstrip()
                stage["truncated"] = True
            else:
                stage["included"] = False
                stage["prompt_text"] = ""
                stage["text"] = ""
        trace["stage"] = stage
        trace["tokens"] = self.context_manager.estimate_tokens(memory_text)
        return trace

    @classmethod
    def _extract_injected_memory_text(cls, messages: List[Dict]) -> str:
        marker = "以下是系统按当前话题检索出的群聊记忆。"
        for message in messages:
            content = cls._llm_record_content_text(message.get("content"))
            if message.get("name") == "memory_context" and marker in content:
                return content[content.find(marker) :].strip()
        for message in messages:
            content = cls._llm_record_content_text(message.get("content"))
            start = content.find(marker)
            if start < 0:
                continue
            value = content[start:]
            boundaries = (
                "\n\n## 最近原始聊天记录",
                "\n\n【本轮网络搜索资料】",
                "\n\n【当前用户消息】",
            )
            end_positions = [
                value.find(boundary)
                for boundary in boundaries
                if value.find(boundary) >= 0
            ]
            if end_positions:
                value = value[: min(end_positions)]
            return value.strip()
        return ""

    @staticmethod
    def _extract_memory_section(
        memory_text: str,
        heading: str,
        next_headings: tuple[str, ...],
    ) -> str:
        start = memory_text.find(heading)
        if start < 0:
            return ""
        value = memory_text[start:]
        end_positions = [
            value.find(next_heading)
            for next_heading in next_headings
            if value.find(next_heading) >= 0
        ]
        if end_positions:
            value = value[: min(end_positions)]
        return value.strip()

    @classmethod
    def _llm_record_content_text(cls, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                value
                for value in (
                    cls._llm_record_content_text(item)
                    for item in content
                )
                if value
            )
        if isinstance(content, dict):
            if content.get("type") in {"image_url", "input_image"}:
                return ""
            for key in ("text", "input_text", "output_text", "content"):
                if key in content:
                    return cls._llm_record_content_text(content.get(key))
            return ""
        return str(content)

    def _strip_markdown(self, text: str) -> str:
        """移除 Markdown 和 HTML 格式"""
        fenced = re.fullmatch(r"\s*```(?:\w+)?\s*(.*?)\s*```\s*", text or "", flags=re.S)
        if fenced:
            text = fenced.group(1)
        # 1. 移除 Markdown 代码块
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        # 2. 移除行内代码
        text = re.sub(r"`([^`]*)`", r"\1", text)
        # 3. 移除加粗/斜体
        text = re.sub(r"(\*\*|\*|_|~~)(.*?)\1", r"\2", text)
        # 4. 移除标题标记
        text = re.sub(r"^#+\s*", "", text, flags=re.M)

        # 5. 处理 HTML 标签
        # 将 <br> 和 <br/> 转换为换行符
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        # 移除其他常见的 HTML 标签 (保留内容)
        text = re.sub(r"</?(?:p|div|span|strong|em|b|i|u)>", "", text, flags=re.I)
        # 移除任何剩余的 HTML 标签
        text = re.sub(r"<[^>]+>", "", text)

        return text.strip()
