"""Capture explicitly requested quote images independently of model queues."""

from concurrent.futures import Future, ThreadPoolExecutor
import hashlib
import logging
from pathlib import Path
import shutil
import threading
import time

from app.core.event_bus import EventType, _parse_sender_blacklist
from app.models.assistant_policy import AssistantChatPolicy
from app.models.user_permission import WeChatUser


logger = logging.getLogger(__name__)
PREFETCH_CONTEXT_KEY = "quote_image_prefetch"


class QuoteImagePrefetch:
    def __init__(self, handler, db_session_factory, directory=None):
        self.handler = handler
        self.db_session_factory = db_session_factory
        self.directory = Path(directory or Path(__file__).resolve().parents[2] / "data/quote_image_prefetch")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="quote-image-prefetch")
        self._slots = threading.BoundedSemaphore(8)
        self._lock = threading.Lock()
        self._pending = {}
        self._closed = False
        # Only files owned by this cache are expired; WeChat originals remain.
        for path in self.directory.glob("*"):
            if path.is_file() and time.time() - path.stat().st_mtime > 86400:
                path.unlink(missing_ok=True)

    def _allowed(self, event):
        data = event.data
        chat = str(data.get("chat_name") or "")
        sender = str(data.get("sender") or "").strip()
        with self.db_session_factory() as db:
            user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat).first()
            if user is None:
                return False
            policy = db.query(AssistantChatPolicy).filter(AssistantChatPolicy.user_id == user.id).first()
            if not policy or not policy.enabled:
                return False
            if sender in _parse_sender_blacklist(user.sender_blacklist):
                return False
        if self.handler._is_sender_ignored(chat, sender):
            return False
        chat_type = data.get("chat_type", "")
        if not self.handler._should_respond(data.get("message", ""), chat_type):
            return False
        return chat_type in {"private", "friend", "user"} or (
            chat_type == "group" and self.handler.allow_mention_trigger
            and self.handler._event_mentions_bot(event)
        )

    def submit(self, event):
        if event.type != EventType.QUOTE_IMAGE_MESSAGE_RECEIVED or event.source != "wx_bot_internal":
            return
        if not event.data.get("message_id") or event.data.get("quote_image_path"):
            return
        key = (str(event.data.get("chat_name") or ""), str(event.data["message_id"]))
        with self._lock:
            if self._closed:
                return
            now = time.monotonic()
            self._pending = {k: v for k, v in self._pending.items()
                             if not v[1].done() or now - v[0] < 600}
            if key in self._pending:
                event.context[PREFETCH_CONTEXT_KEY] = self._pending[key][1]
                return
            if not self._slots.acquire(blocking=False):
                logger.warning("引用图片预取队列已满: chat=%s message_id=%s", *key)
                return
            try:
                future = self._pool.submit(self._capture, event, key)
            except Exception:
                self._slots.release()
                raise
            future.add_done_callback(lambda _future: self._slots.release())
            self._pending[key] = (now, future)
            event.context[PREFETCH_CONTEXT_KEY] = future

    def _capture(self, event, key):
        temporary = None
        try:
            if self._closed or not self._allowed(event):
                return None
            wx = event.context.get("wx")
            if wx is None:
                return None
            started = time.monotonic()
            logger.info("引用图片开始提前保存: chat=%s message_id=%s", *key)
            source = wx.download_quote_image(key[0], message_id=key[1])
            if not source or not Path(source).is_file():
                raise RuntimeError("原引用图片未能下载")
            source = Path(source)
            digest = hashlib.sha256("\x1f".join(key).encode()).hexdigest()
            suffix = source.suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
                suffix = ".image"
            destination = self.directory / (digest + suffix)
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            shutil.copyfile(source, temporary)
            temporary.replace(destination)
            logger.info("引用图片已提前保存: chat=%s message_id=%s elapsed_ms=%.0f",
                        *key, (time.monotonic() - started) * 1000)
            return {"path": str(destination), "status": "ready"}
        except Exception as exc:
            logger.warning("引用图片提前保存失败: chat=%s message_id=%s error=%s", *key, exc)
            return {"status": "failed", "error": str(exc)}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def close(self):
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)


def resolve_quote_image_prefetch(event):
    future = event.context.get(PREFETCH_CONTEXT_KEY)
    if not isinstance(future, Future):
        return
    try:
        result = future.result(timeout=135)
    except Exception as exc:
        result = {"status": "failed", "error": type(exc).__name__}
    if result is None:
        return
    event.data["quote_image_prefetch_attempted"] = True
    if result.get("status") == "ready":
        event.data["quote_image_path"] = result["path"]
    else:
        event.data["quote_image_prefetch_error"] = result.get("error", "预取失败")
