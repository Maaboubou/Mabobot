"""Freshness of WeChat-triggered actions; receipt time is never source time."""
from contextvars import ContextVar
from datetime import datetime
import math
import time

MAX_SOURCE_AGE_SECONDS = 600
_active_source = ContextVar("wechat_action_source", default=None)


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


class StaleSourceMessage(RuntimeError):
    pass


def require_current_source_fresh():
    source = _active_source.get()
    if source is not None and not source_is_fresh(source[0]):
        raise StaleSourceMessage("WeChat source message expired or has no reliable timestamp")


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
        self._source = (event.data.get("source_sent_at"),) if has_source_policy(event) else None

    def __getattr__(self, name):
        target = getattr(self._wx, name)
        if name not in self.ACTIONS or self._source is None:
            return target
        def action(*args, **kwargs):
            token = _active_source.set(self._source)
            try:
                require_current_source_fresh()
                return target(*args, **kwargs)
            finally:
                _active_source.reset(token)
        return action
