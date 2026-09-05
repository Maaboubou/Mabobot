"""Bounded, best-effort media diagnostics; never perform UI actions."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import time
import uuid

from mabowx.core import win32
from mabowx.logger import wxlog


_current_trace = ContextVar("media_download_trace", default=None)


def _probe(operation):
    try:
        return operation()
    except Exception as exc:
        return {"probe_error": f"{type(exc).__name__}: {exc}"[:240]}


@contextmanager
def media_download_trace(message, *, request_id=None, chat=None, message_id=None):
    """Keep request correlation local to this execution, not the cached message."""
    if _current_trace.get() is not None:
        yield
        return
    trace = {
        "request_id": request_id or uuid.uuid4().hex,
        "chat": chat,
        "message_id": message_id,
        "started": time.monotonic(),
        "stage": "queued",
    }
    try:
        target = getattr(message, "media_target_identity", None)
        parent = getattr(message, "parent", None)
        trace.update({
            "chat": chat or getattr(target, "chat_name", None) or getattr(parent, "who", None),
            "message_id": message_id or getattr(message, "delivery_id", None),
            "raw_runtime_id": getattr(message, "id", None),
            "sequence": getattr(message, "delivery_sequence", None),
            "message_type": getattr(message, "type", None),
            "expected_pid": getattr(parent, "pid", None),
            "chat_hwnd": getattr(target, "chat_hwnd", None),
        })
    except Exception as exc:
        trace["probe_error"] = f"{type(exc).__name__}: {exc}"[:240]
    token = _current_trace.set(trace)
    try:
        yield
    finally:
        _current_trace.reset(token)


def media_event(event, *, level="info", **details):
    """Do not let stale controls, formatting or log I/O mask the real failure."""
    try:
        trace = _current_trace.get()
        payload = dict(trace or {})
        if trace is not None:
            payload["elapsed_ms"] = round((time.monotonic() - trace["started"]) * 1000)
            payload.pop("started", None)
            if event not in {"failed", "completed", "preview_timeout_snapshot"}:
                trace["stage"] = event
                payload["stage"] = event
        payload.update(details)
        payload["event"] = event
        # Pass a fully formatted string so the logger's size cap also applies.
        getattr(wxlog, level)("媒体下载诊断 " + json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        pass


def _window_summary(info):
    if info is None:
        return None
    # Window titles and chat contents are unnecessary for window identity.
    return {key: getattr(info, key) for key in ("hwnd", "pid", "class_name", "visible", "rect")}


def media_foreground_snapshot():
    return _probe(lambda: _window_summary(win32.get_window_info(win32.get_foreground_window())))


def media_ui_snapshot(message):
    """Capture only bounded metadata on failure, without screenshots or UI trees."""
    def collect():
        parent = getattr(message, "parent", None)
        target = getattr(message, "media_target_identity", None)
        pid = getattr(parent, "pid", None)
        hwnd = getattr(target, "chat_hwnd", None)
        control = getattr(message, "control", None)

        def row_rect():
            rect = control.BoundingRectangle
            return [int(getattr(rect, key)) for key in ("left", "top", "right", "bottom")]

        def windows():
            items = win32.enum_windows_by_pid(pid)
            # Put previews first so a busy process cannot hide them behind the cap.
            items.sort(key=lambda item: item.class_name != "mmui::PreviewWindow")
            return {"total": len(items), "items": [_window_summary(item) for item in items[:12]]}

        return {
            "foreground": media_foreground_snapshot(),
            "chat_geometry": _probe(lambda: win32.get_window_geometry(hwnd)) if hwnd else None,
            "chat_hung": _probe(lambda: win32.is_hung_window(hwnd)) if hwnd else None,
            "process_windows": _probe(windows) if pid else None,
            "control_exists": _probe(lambda: control.Exists(0)) if control is not None else None,
            "current_row_rect": _probe(row_rect) if control is not None else None,
            "prepared_row_rect": getattr(message, "_prepared_media_row_rect", None),
            "prepared_point": getattr(message, "_prepared_media_point", None),
        }

    return _probe(collect)
