"""Freshness of WeChat-triggered actions; receipt time is never source time."""
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import math
import time

MAX_SOURCE_AGE_SECONDS = 600
_active_source = ContextVar("wechat_action_source", default=None)
_admitted_task = ContextVar("wechat_admitted_task", default=None)
DELIVERY_ACTIONS = frozenset({"send_message", "send_files", "send_url_card"})


@dataclass(frozen=True)
class ActionSource:
    event_id: str
    chat: str
    sent_at: object
    received_at: object
    time_kind: str

    @classmethod
    def from_event(cls, event):
        return cls(str(event.id), str(event.data.get("chat_name") or ""),
                   event.data.get("source_sent_at"), event.data.get("timestamp"),
                   str(event.data.get("source_time_kind") or "unknown"))


@contextmanager
def admitted_task_scope(task, event):
    """Only the supervisor, after its queue freshness check, grants this scope."""
    task.source = ActionSource.from_event(event) if has_source_policy(event) else None
    token = _admitted_task.set(task)
    try:
        yield
    finally:
        _admitted_task.reset(token)


def valid_source_time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    try:
        datetime.fromtimestamp(value)
    except (ValueError, OverflowError, OSError):
        return None
    return float(value)


def source_is_fresh(value, now=None):
    source = valid_source_time(value)
    now = time.time() if now is None else now
    return source is not None and 0 <= now - source <= MAX_SOURCE_AGE_SECONDS


def has_source_policy(event):
    return getattr(event, "source", "") == "wx_bot_internal" or event.data.get("source_time_policy") == "wechat_v1"


def event_is_fresh(event):
    return not has_source_policy(event) or source_is_fresh(event.data.get("source_sent_at"))


class ActionBlocked(RuntimeError):
    """An action was rejected locally before submission to WeChat."""


class StaleSourceMessage(ActionBlocked):
    pass


def require_current_source_fresh():
    active = _active_source.get()
    if active is None:
        return
    source, action, target_chat = active
    task = _admitted_task.get()
    if task is not None and task.source is not None and action in DELIVERY_ACTIONS:
        if source != task.source or target_chat != task.chat:
            raise ActionBlocked("任务发送被拦截：消息来源或目标聊天与已接单任务不匹配")
        if task.require_delivery_allowed():
            return
    now = time.time()
    if not source_is_fresh(source.sent_at, now):
        sent_at = valid_source_time(source.sent_at)
        age = now - sent_at if sent_at is not None else None
        raise StaleSourceMessage(
            "WeChat source message expired or has no reliable timestamp: "
            f"source_sent_at={source.sent_at!r} received_at={source.received_at!r} "
            f"source_time_kind={source.time_kind} source_age_seconds={age!r} "
            f"limit_seconds={MAX_SOURCE_AGE_SECONDS} action={action}"
        )


def archive_expired_event(event):
    """Preserve a queued original before suppressing its actions; no plugin calls."""
    if event.data.get("_source_policy_archived"):
        return
    from app.services.chat_logging_policy import is_chat_logging_enabled
    if not is_chat_logging_enabled(event.data.get("chat_name")):
        event.data["_source_policy_archived"] = True
        return
    from app.assistant.chat_log import ChatLogManager
    data = event.data
    source = valid_source_time(data.get("source_sent_at"))
    received = valid_source_time(data.get("timestamp"))
    metadata = {key: data[key] for key in (
        "quote_nickname", "quote_content", "quote_image_path", "quote_video_path",
        "file_id", "file_name", "file_path", "file_size", "file_sha256", "file_status",
        "quoted_file_id", "quoted_file_name", "quoted_file_path", "quoted_file_status",
    ) if data.get(key) is not None}
    metadata.update(archive_only=True, action_suppressed="source_time")
    metadata.update({key: data[key] for key in (
        "source_sent_at", "source_time_label", "source_time_kind", "history_recovered",
    ) if key in data})
    metadata["received_at"] = data.get("timestamp")
    # The canonical archive deduplicates source occurrences. Do not append a
    # second legacy JSONL projection if an observer already archived this event.
    saved = ChatLogManager().archive.append(data["chat_name"], {
        "id": event.id,
        "sender": str(data.get("sender") or ""), "content": str(data.get("message") or ""),
        "sender_id": str(data.get("sender_id") or ""),
        "sender_remark": str(data.get("sender_remark") or ""),
        "message_type": str(data.get("message_type") or ""),
        "source_message_id": str(data.get("archive_message_id") or data.get("message_id") or event.id),
        "source_id_namespace": str(data.get("archive_source") or "event_bus"),
        "time": datetime.fromtimestamp(received if received is not None else time.time()).strftime("%Y-%m-%d %H:%M:%S"),
        "occurred_at": datetime.fromtimestamp(source).strftime("%Y-%m-%d %H:%M:%S") if source is not None else "",
        "time_source": "platform" if source is not None else "received",
        "metadata": metadata,
    }, source="expired_wechat_event")
    if not saved:
        raise RuntimeError("Expired WeChat message archive failed")
    data["_source_policy_archived"] = True


class SourceMessageActionProxy:
    """Freeze the source timestamp with the wx reference retained by worker tasks."""
    ACTIONS = frozenset({"send_message", "quote_message", "send_files", "send_url_card",
                         "tickle", "SendMsg", "SendFiles", "SendUrlCard",
                         "resolve_link_url", "download_image_message",
                         "download_quote_image", "download_quote_video"})

    def __init__(self, wx, event):
        self._wx = wx
        self._source = ActionSource.from_event(event) if has_source_policy(event) else None

    def __getattr__(self, name):
        target = getattr(self._wx, name)
        if name not in self.ACTIONS or self._source is None:
            return target
        def action(*args, **kwargs):
            chat = args[0] if args else kwargs.get("chat_name", kwargs.get("who", ""))
            token = _active_source.set((self._source, name, str(chat)))
            try:
                require_current_source_fresh()
                return target(*args, **kwargs)
            finally:
                _active_source.reset(token)
        return action
