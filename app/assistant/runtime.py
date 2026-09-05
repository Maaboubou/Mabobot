"""Application-owned lifecycle for the core Codex assistant."""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from app.assistant.handler import AssistantHandler
from app.core.event_bus import Event, EventBus, EventType


logger = logging.getLogger(__name__)

ASSISTANT_OWNER_ID = "assistant"


class AssistantRuntime:
    """Own the first-class assistant independently from PluginManager."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._event_bus: Optional[EventBus] = None
        self._handler: Optional[AssistantHandler] = None
        self._listener_ids: List[str] = []
        self._last_error = ""

    @property
    def handler(self) -> Optional[AssistantHandler]:
        with self._lock:
            return self._handler

    def _handle_text(self, event: Event):
        handler = self.handler
        return handler.handle_text_message(event) if handler is not None else False

    def _handle_quote_image(self, event: Event):
        handler = self.handler
        return handler.handle_quote_image_message(event) if handler is not None else False

    def _handle_quote_video(self, event: Event):
        handler = self.handler
        return handler.handle_quote_video_message(event) if handler is not None else False

    def start(self, event_bus: EventBus) -> bool:
        with self._lock:
            if self._handler is not None:
                return True
            self._event_bus = event_bus
            self._last_error = ""

        handler: Optional[AssistantHandler] = None
        listener_ids: List[str] = []
        try:
            handler = AssistantHandler(context=None)
            handler.event_bus = event_bus
            subscriptions = (
                (EventType.TEXT_MESSAGE_RECEIVED, self._handle_text, "text"),
                (EventType.QUOTE_IMAGE_MESSAGE_RECEIVED, self._handle_quote_image, "quote-image"),
                (EventType.QUOTE_VIDEO_MESSAGE_RECEIVED, self._handle_quote_video, "quote-video"),
                (EventType.QUOTE_TEXT_MESSAGE_RECEIVED, self._handle_text, "quote-text"),
                (EventType.CHATBOT_FOLLOWUP_APPROVED, self._handle_text, "followup-approved"),
            )
            for event_type, callback, key in subscriptions:
                listener_ids.append(
                    event_bus.subscribe(
                        event_type=event_type,
                        handler=callback,
                        plugin_name=ASSISTANT_OWNER_ID,
                        listener_key=f"assistant:{key}",
                        handler_name=callback.__name__,
                        order_index=0,
                        order_source="core_fixed_fallback",
                        owner_kind="core",
                        permission_key="assistant_policy",
                        display_name="AI 助手",
                    )
                )

            with self._lock:
                self._handler = handler
                self._listener_ids = listener_ids
            logger.info("Core Codex assistant started with %s listeners", len(listener_ids))
            return True
        except Exception as exc:
            for listener_id in listener_ids:
                event_bus.unsubscribe(listener_id)
            if handler is not None:
                try:
                    handler.close()
                except Exception:
                    logger.debug("Assistant cleanup after failed start also failed", exc_info=True)
            with self._lock:
                self._handler = None
                self._listener_ids = []
                self._last_error = str(exc)
            logger.exception("Core Codex assistant failed to start; plugins remain available")
            return False

    def stop(self) -> None:
        with self._lock:
            event_bus = self._event_bus
            listener_ids = list(self._listener_ids)
            handler = self._handler
            self._listener_ids = []
            self._handler = None
            self._event_bus = None
        if event_bus is not None:
            for listener_id in listener_ids:
                event_bus.unsubscribe(listener_id)
        if handler is not None:
            try:
                handler.close()
            except Exception:
                logger.exception("Core Codex assistant failed to close cleanly")

    def restart(self) -> bool:
        with self._lock:
            event_bus = self._event_bus
        if event_bus is None:
            return False
        self.stop()
        return self.start(event_bus)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            handler = self._handler
            error = self._last_error
            listener_count = len(self._listener_ids)
        return {
            "component": ASSISTANT_OWNER_ID,
            "status": "ready" if handler is not None else ("degraded" if error else "stopped"),
            "ready": handler is not None,
            "listener_count": listener_count,
            "pending_followups": len(handler._followup_sessions) if handler is not None else 0,
            "error": error,
        }

_assistant_runtime: Optional[AssistantRuntime] = None


def get_assistant_runtime() -> AssistantRuntime:
    global _assistant_runtime
    if _assistant_runtime is None:
        _assistant_runtime = AssistantRuntime()
    return _assistant_runtime


def get_assistant_handler() -> Optional[AssistantHandler]:
    """Return the live handler without exposing a plugin-style global."""
    return get_assistant_runtime().handler
