from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.core.event_bus import Event, EventType
from app.services.llm_manager import get_llm_manager
from app.utils.plugin_config import get_config

from .browser_service import EbookBrowserError, EbookBrowserService
from .models import BookCandidate, BookRequest, SelectionResult, WorkChoice, select_candidate
from .policy import PolicyEngine, PolicyMatch
from .request_parser import BookRequestParser, RequestParseError
from .sessions import SessionStore, parse_choice


PLUGIN_NAME = "ebook_downloader"
logger = logging.getLogger(__name__)
plugin: Optional["EbookDownloaderPlugin"] = None
_CANDIDATE_QUOTE_PREFIXES = (
    "找到多个可能的作品，请回复序号：",
    "未找到中文版本，请确认是否下载以下外文版本：",
)


def _cfg(key: str, default: Any) -> Any:
    return get_config(key, default, plugin_name=PLUGIN_NAME)


class EbookDownloaderPlugin:
    def __init__(self, event_bus: Any, context: Any):
        self.event_bus = event_bus
        self.context = context
        self.trigger_keywords = tuple(
            str(item).strip()
            for item in (_cfg("trigger_keywords", ["找书", "下载电子书", "电子书"]) or [])
            if str(item).strip()
        )
        self.session_timeout = min(60, max(10, int(_cfg("session_timeout_seconds", 60) or 60)))
        self.max_choices = min(5, max(1, int(_cfg("max_choices", 5) or 5)))
        self.parse_confidence_threshold = min(
            1.0, max(0.0, float(_cfg("parse_confidence_threshold", 0.85) or 0.85))
        )
        self.selection_options = {
            "title_author_threshold": float(_cfg("title_author_threshold", 0.90) or 0.90),
            "title_only_threshold": float(_cfg("title_only_threshold", 0.96) or 0.96),
            "title_author_margin": float(_cfg("title_author_margin", 0.08) or 0.08),
            "title_only_margin": float(_cfg("title_only_margin", 0.12) or 0.12),
            "max_choices": self.max_choices,
        }
        bundled_policy = Path(__file__).with_name("download_policy_rules.json")
        custom_policy = str(_cfg("local_policy_path", "") or "").strip()
        self.policy = PolicyEngine.from_paths(bundled_policy, Path(custom_policy) if custom_policy else None)
        self.parser = BookRequestParser(get_llm_manager())
        self.browser = EbookBrowserService(
            temp_root=context.storage.temp_root,
            page_timeout=int(_cfg("page_timeout_seconds", 20) or 20),
            download_timeout=int(_cfg("download_timeout_seconds", 120) or 120),
            max_file_mb=int(_cfg("max_file_mb", 100) or 100),
        )
        self._task_lock = threading.Lock()
        self._session_lock = threading.RLock()
        self._sessions: SessionStore[tuple[BookRequest, WorkChoice]] = SessionStore(
            ttl_seconds=self.session_timeout,
            max_choices=self.max_choices,
        )
        self._session_timers: dict[tuple[str, str], Any] = {}
        self._closed = False

    @staticmethod
    def _message(event: Event) -> str:
        return str(event.data.get("message") or event.data.get("content") or "").strip()

    @staticmethod
    def _chat_name(event: Event) -> str:
        return str(event.data.get("chat_name") or "").strip()

    @staticmethod
    def _sender_key(event: Event) -> str:
        return str(
            event.data.get("sender_id")
            or event.data.get("sender")
            or event.data.get("sender_remark")
            or event.data.get("chat_name")
            or "unknown"
        ).strip()

    @staticmethod
    def _is_candidate_reply_event(event: Event) -> bool:
        if getattr(event, "type", None) != EventType.QUOTE_TEXT_MESSAGE_RECEIVED:
            return True
        quote_content = str(event.data.get("quote_content") or "").lstrip()
        return any(quote_content.startswith(prefix) for prefix in _CANDIDATE_QUOTE_PREFIXES)

    def _extract_query(self, message: str) -> str | None:
        for keyword in sorted(self.trigger_keywords, key=len, reverse=True):
            match = re.search(
                rf"(?:^|[\s\u2005])/?{re.escape(keyword)}(?=$|[\s\u2005:：])",
                message,
                flags=re.IGNORECASE,
            )
            if match:
                return message[match.end() :].lstrip(" \u2005:：,，").strip()
        return None

    @staticmethod
    def _send_text(wx: Any, chat_name: str, text: str) -> bool:
        return bool(wx and chat_name and wx.send_message(chat_name, text))

    def _policy_reply(self, wx: Any, chat_name: str, match: PolicyMatch | None = None) -> None:
        if match and match.decision == "review":
            self._send_text(wx, chat_name, "该请求需要人工确认，当前不能自动下载。")
        else:
            self._send_text(wx, chat_name, "该请求无法处理。")

    def handle_command(self, event: Event) -> bool:
        if self._closed:
            return False
        message = self._message(event)
        query = self._extract_query(message)
        if query is None:
            return False
        chat_name = self._chat_name(event)
        wx = event.context.get("wx")
        if not chat_name or not wx:
            return False
        if not query:
            self._send_text(wx, chat_name, "请在命令后提供书名、ISBN 或 DOI。")
            return True

        sender_key = self._sender_key(event)
        self._remove_session(chat_name, sender_key)
        raw_policy = self.policy.check_raw(query)
        if raw_policy.blocked:
            logger.info(
                "ebook_downloader 本地预检拦截 chat=%s sender=%s tier=%s",
                chat_name,
                sender_key,
                raw_policy.source_tier,
            )
            self._policy_reply(wx, chat_name, raw_policy)
            return True

        self.context.tasks.submit(
            "ebook_lookup",
            f"电子书检索 · {chat_name}",
            lambda operation: self._run_lookup(operation, query, chat_name, sender_key, wx),
            details={"chat_name": chat_name, "sender": sender_key},
        )
        return True

    def _run_lookup(self, operation: Any, query: str, chat_name: str, sender_key: str, wx: Any) -> dict[str, Any]:
        if not self._task_lock.acquire(blocking=False):
            self._send_text(wx, chat_name, "当前已有电子书任务在执行，请稍后再试。")
            return {"status": "busy"}
        try:
            operation.progress(10, "正在解析找书请求")
            try:
                request = self.parser.parse(query, chat_name=chat_name)
            except (RequestParseError, ValueError) as exc:
                logger.warning("ebook_downloader 模型解析关闭 chat=%s error=%s", chat_name, exc)
                self._send_text(wx, chat_name, "找书请求解析失败，请确认模型 Mapping 后重试。")
                return {"status": "parse_failed"}
            except Exception as exc:
                logger.error("ebook_downloader 模型调用失败 chat=%s error=%s", chat_name, exc, exc_info=True)
                self._send_text(wx, chat_name, "找书请求解析失败，请稍后重试。")
                return {"status": "parse_failed"}

            if not request.model_valid or not request.has_identity:
                self._send_text(wx, chat_name, "请至少提供书名、ISBN 或 DOI；只有作者无法准确找书。")
                return {"status": "invalid_request"}
            if request.policy_decision != "allow":
                self._policy_reply(
                    wx,
                    chat_name,
                    PolicyMatch("deny" if request.policy_decision == "deny" else "review"),
                )
                return {"status": "policy_blocked"}
            structured_policy = self.policy.check_request(request)
            if structured_policy.blocked:
                self._policy_reply(wx, chat_name, structured_policy)
                return {"status": "policy_blocked"}

            operation.progress(35, "正在共享浏览器中搜索")
            candidates = self.browser.search(
                request.search_queries,
                expected_isbn=request.isbn,
                expected_doi=request.doi,
            )
            allowed_candidates: list[BookCandidate] = []
            blocked_candidate: PolicyMatch | None = None
            for candidate in candidates:
                candidate_policy = self.policy.check_candidate(candidate)
                if candidate_policy.blocked:
                    blocked_candidate = blocked_candidate or candidate_policy
                    continue
                allowed_candidates.append(candidate)
            if candidates and not allowed_candidates and blocked_candidate:
                self._policy_reply(wx, chat_name, blocked_candidate)
                return {"status": "policy_blocked"}
            result = select_candidate(request, allowed_candidates, **self.selection_options)
            if (
                result.mode == "auto"
                and not (request.isbn or request.doi)
                and request.parse_confidence < self.parse_confidence_threshold
            ):
                result = SelectionResult(
                    "ambiguous",
                    choices=result.choices,
                    reason="请求解析置信度不足",
                )

            if result.mode == "auto" and result.selected:
                operation.progress(65, "已高置信度匹配，正在下载")
                return self._download_and_send(operation, request, result.selected, chat_name, wx)
            if result.mode in {"ambiguous", "foreign_confirmation"} and result.choices:
                operation.progress(90, "等待用户选择")
                self._create_session(chat_name, sender_key, request, result.choices, wx, result.mode)
                operation.progress(100, "已发送候选作品")
                return {"status": result.mode, "choices": len(result.choices)}
            messages = {
                "no_results": "没有找到可用的电子书结果。",
                "language_unavailable": "没有找到符合指定语言的版本。",
                "format_unavailable": "没有找到符合指定格式的版本。",
            }
            self._send_text(wx, chat_name, messages.get(result.mode, "没有找到足够准确的电子书结果。"))
            return {"status": result.mode}
        except EbookBrowserError as exc:
            logger.warning("ebook_downloader 浏览器流程失败 chat=%s error=%s", chat_name, exc)
            self._send_text(wx, chat_name, f"电子书处理失败：{exc}")
            return {"status": "browser_failed"}
        except Exception as exc:
            logger.error("ebook_downloader 任务失败 chat=%s error=%s", chat_name, exc, exc_info=True)
            self._send_text(wx, chat_name, "电子书处理失败，请稍后重试。")
            return {"status": "failed"}
        finally:
            self._task_lock.release()

    def _download_and_send(
        self,
        operation: Any,
        request: BookRequest,
        candidate: BookCandidate,
        chat_name: str,
        wx: Any,
    ) -> dict[str, Any]:
        final_policy = self.policy.check_candidate(candidate)
        if final_policy.blocked:
            self._policy_reply(wx, chat_name, final_policy)
            return {"status": "policy_blocked"}
        path = self.browser.download(candidate)
        operation.progress(90, "正在发送电子书文件")
        sent = bool(wx.send_files(chat_name, [str(path)]))
        if not sent:
            self._send_text(wx, chat_name, "电子书已下载，但文件发送失败。")
            return {"status": "send_failed", "path": str(path)}
        sent_bytes = path.stat().st_size if path.exists() else 0
        try:
            path.unlink(missing_ok=True)
            path.parent.rmdir()
        except OSError:
            logger.debug("清理已发送的电子书临时文件失败: %s", path, exc_info=True)
        operation.progress(100, "电子书文件已发送")
        return {
            "status": "sent",
            "format": candidate.normalized_format,
            "bytes": sent_bytes,
        }

    def _create_session(
        self,
        chat_name: str,
        sender_key: str,
        request: BookRequest,
        choices: tuple[WorkChoice, ...],
        wx: Any,
        mode: str,
    ) -> None:
        key = (chat_name, sender_key)
        with self._session_lock:
            self._remove_session_locked(chat_name, sender_key)
            payload = tuple((request, choice) for choice in choices[: self.max_choices])
            session = self._sessions.put(chat_name, sender_key, payload)
            self.event_bus.request_session_permission(chat_name, PLUGIN_NAME, self.session_timeout)
            timer = self.context.workers.start_timer(
                f"ebook-choice-{time.time_ns()}",
                self.session_timeout,
                self._expire_session,
                args=(chat_name, sender_key, session.expires_at),
            )
            self._session_timers[key] = timer

        prefix = "未找到中文版本，请确认是否下载以下外文版本：" if mode == "foreign_confirmation" else "找到多个可能的作品，请回复序号："
        lines = [prefix]
        for index, (_request, choice) in enumerate(payload, start=1):
            item = choice.candidate
            author = " / ".join(item.authors) if item.authors else "作者未知"
            language = item.language or "语言未知"
            formats = "/".join(value.upper() for value in choice.formats) or item.normalized_format.upper()
            year = f" · {item.year}" if item.year else ""
            lines.append(f"{index}. {item.title} · {author} · {language}{year} · {formats}")
        lines.append(
            f"请在 {self.session_timeout} 秒内回复 1-{len(payload)}（可引用本列表或带 @机器人），"
            "或回复“取消”。"
        )
        if not self._send_text(wx, chat_name, "\n".join(lines)):
            self._remove_session(chat_name, sender_key)

    def handle_session_reply(self, event: Event) -> bool:
        if self._closed:
            return False
        chat_name = self._chat_name(event)
        sender_key = self._sender_key(event)
        wx = event.context.get("wx")
        if not chat_name or not wx:
            return False
        if not self._is_candidate_reply_event(event):
            return False
        with self._session_lock:
            stale = self._sessions.peek(chat_name, sender_key)
            session = self._sessions.get(chat_name, sender_key)
            if not session:
                if stale:
                    self._remove_session_locked(chat_name, sender_key)
                return False
            selection = parse_choice(
                self._message(event),
                len(session.choices),
                bot_mention_name=event.data.get("bot_mention_name"),
            )
            if selection is None:
                return False
            self._remove_session_locked(chat_name, sender_key)

        if selection == "cancel":
            self._send_text(wx, chat_name, "已取消本次找书。")
            return True
        request, choice = session.choices[int(selection)]
        self.context.tasks.submit(
            "ebook_download_choice",
            f"电子书下载 · {chat_name}",
            lambda operation: self._run_selected(operation, request, choice.candidate, chat_name, wx),
            details={"chat_name": chat_name, "sender": sender_key, "choice": int(selection) + 1},
        )
        return True

    def _run_selected(
        self,
        operation: Any,
        request: BookRequest,
        candidate: BookCandidate,
        chat_name: str,
        wx: Any,
    ) -> dict[str, Any]:
        if not self._task_lock.acquire(blocking=False):
            self._send_text(wx, chat_name, "当前已有电子书任务在执行，请稍后重新发起。")
            return {"status": "busy"}
        try:
            operation.progress(20, "正在复核所选作品")
            request_policy = self.policy.check_request(request)
            candidate_policy = self.policy.check_candidate(candidate)
            if request_policy.blocked or candidate_policy.blocked:
                match = request_policy if request_policy.blocked else candidate_policy
                self._policy_reply(wx, chat_name, match)
                return {"status": "policy_blocked"}
            operation.progress(50, "正在下载所选电子书")
            return self._download_and_send(operation, request, candidate, chat_name, wx)
        except EbookBrowserError as exc:
            self._send_text(wx, chat_name, f"电子书处理失败：{exc}")
            return {"status": "browser_failed"}
        except Exception as exc:
            logger.error("ebook_downloader 候选下载失败 chat=%s error=%s", chat_name, exc, exc_info=True)
            self._send_text(wx, chat_name, "电子书处理失败，请稍后重试。")
            return {"status": "failed"}
        finally:
            self._task_lock.release()

    def _expire_session(self, chat_name: str, sender_key: str, expires_at: float) -> None:
        with self._session_lock:
            session = self._sessions.peek(chat_name, sender_key)
            if not session or session.expires_at != expires_at:
                return
            self._remove_session_locked(chat_name, sender_key, cancel_timer=False)

    def _remove_session(self, chat_name: str, sender_key: str) -> None:
        with self._session_lock:
            self._remove_session_locked(chat_name, sender_key)

    def _remove_session_locked(self, chat_name: str, sender_key: str, *, cancel_timer: bool = True) -> None:
        key = (chat_name, sender_key)
        self._sessions.pop(chat_name, sender_key)
        timer = self._session_timers.pop(key, None)
        if cancel_timer and timer:
            timer.cancel()
        if not self._sessions.has_chat(chat_name):
            self.event_bus.release_session_permission(chat_name, PLUGIN_NAME)

    def health(self) -> dict[str, Any]:
        return {
            "status": "healthy" if not self._closed else "unhealthy",
            "message": "电子书下载服务已就绪" if not self._closed else "电子书下载服务已停止",
            "active_tasks": int(self._task_lock.locked()),
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._session_lock:
            chats = self._sessions.chats()
            for timer in self._session_timers.values():
                timer.cancel()
            self._session_timers.clear()
            self._sessions.clear()
            for chat_name in chats:
                self.event_bus.release_session_permission(chat_name, PLUGIN_NAME)


def handle_command(event: Event) -> bool:
    return plugin.handle_command(event) if plugin else False


def handle_session_reply(event: Event) -> bool:
    return plugin.handle_session_reply(event) if plugin else False


def register(event_bus: Any, subscribe: Any, context: Any) -> None:
    global plugin
    plugin = EbookDownloaderPlugin(event_bus, context)
    context.health.register(plugin.health)
    context.register_cleanup(plugin.close)
    subscribe(EventType.TEXT_MESSAGE_RECEIVED, handle_command)
    subscribe(EventType.TEXT_MESSAGE_RECEIVED, handle_session_reply)
    subscribe(EventType.QUOTE_TEXT_MESSAGE_RECEIVED, handle_session_reply)
    logger.info("ebook_downloader 插件已注册")


def unregister() -> None:
    global plugin
    if plugin:
        plugin.close()
    plugin = None
