"""Codex-only reply boundary for the first-class chat assistant.

This module deliberately bypasses :meth:`LLMManager.call`.  Auxiliary assistant
tasks (Judge and media enrichment) still use the generic model router,
but a message that will be sent as the assistant's final reply must cross this
gateway and can therefore only be produced by the local Codex runtime.  The
gateway may still publish telemetry to LLMManager's history store; telemetry is
not a model-routing path.
"""

from __future__ import annotations

import copy
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence


logger = logging.getLogger(__name__)


class AssistantReplyError(RuntimeError):
    """Raised when Codex cannot produce a usable assistant reply."""


@dataclass(frozen=True)
class CodexReplyRequest:
    chat_name: str
    messages: Sequence[Dict[str, Any]]
    role_name: str = ""
    codex_profile_id: str = ""
    persistent_session: bool = True
    incremental_context: bool = False
    fresh_context: bool = False
    history_enabled: bool = False
    history_request_id: str = ""
    history_max_calls: int = 0
    history_max_bytes: int = 0
    retry: bool = False
    reasoning_effort: str = "inherit"
    reasoning_summary: str = "inherit"
    web_search_mode: str = "inherit"
    timeout_seconds: int = 0
    max_turns: int = 0
    allow_exec_fallback: bool = True
    output_schema: Optional[Dict[str, Any]] = None
    input_files: Sequence[Dict[str, Any]] = field(default_factory=tuple)
    allow_image_input: bool = False
    history_mode: str = "full"
    usage_capture: Optional[List[Dict[str, Any]]] = None


@dataclass(frozen=True)
class CodexReplyResult:
    text: str
    attachments: List[Dict[str, Any]]
    usage: Dict[str, Any]
    model: str
    backend: str
    duration_seconds: float
    raw_response: Dict[str, Any]


def _default_model() -> str:
    return str(os.getenv("CODEX_PROXY_MODEL") or "gpt-5.6-sol").strip()


def _read(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


class CodexReplyGateway:
    """Execute final assistant replies exclusively through CodexAgentRuntime."""

    _REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}
    _REASONING_SUMMARIES = {"none", "auto", "concise", "detailed"}
    _WEB_SEARCH_MODES = {"disabled", "cached", "indexed", "live"}

    def __init__(
        self,
        runtime: Any = None,
        *,
        model_resolver: Optional[Callable[[], str]] = None,
        runtime_resolver: Optional[Callable[[str], Any]] = None,
        telemetry_recorder: Optional[Callable[..., None]] = None,
    ) -> None:
        self._runtime = runtime
        self._model_resolver = model_resolver or _default_model
        self._runtime_resolver = runtime_resolver
        self._telemetry_recorder = telemetry_recorder

    @property
    def runtime(self) -> Any:
        if self._runtime is None:
            from app.services.agent_runtime import get_agent_runtime

            self._runtime = get_agent_runtime()
        return self._runtime

    @staticmethod
    def capabilities() -> Dict[str, bool]:
        """Capabilities are invariant because no generic provider is eligible."""
        return {
            "codex": True,
            "native_web_search": True,
            "vision": True,
            "local_files": True,
            "tool_calling": True,
        }

    @staticmethod
    def count_prompt_tokens(
        messages: Sequence[Dict[str, Any]],
        *,
        native_web_search_enabled: bool = False,
        input_image_count: int = 0,
    ) -> int:
        from app.services.codex_proxy.client import count_codex_prompt_tokens

        return int(
            count_codex_prompt_tokens(
                messages,
                native_web_search_enabled=bool(native_web_search_enabled),
                input_image_count=max(0, int(input_image_count or 0)),
            )
        )

    def reply(self, request: CodexReplyRequest) -> CodexReplyResult:
        chat_name = str(request.chat_name or "").strip()
        if not chat_name:
            raise AssistantReplyError("Codex assistant reply requires a managed chat")
        if not isinstance(request.messages, (list, tuple)) or not request.messages:
            raise AssistantReplyError("Codex assistant reply requires at least one message")

        requested_profile = str(request.codex_profile_id or "").strip()
        profile: Optional[Dict[str, Any]] = None
        runtime: Any = None
        if requested_profile or self._runtime is None:
            try:
                resolved = (
                    self._runtime_resolver(requested_profile)
                    if self._runtime_resolver
                    else self._resolve_profile_runtime(requested_profile)
                )
                if isinstance(resolved, tuple):
                    runtime, profile = resolved
                else:
                    runtime = resolved
            except Exception as exc:
                raise AssistantReplyError(f"Codex Profile is unavailable: {exc}") from exc
        else:
            # Explicit runtime injection is retained for isolated tests and
            # embedders; production calls always resolve a managed Profile.
            runtime = self.runtime

        model = str((profile or {}).get("model") or self._model_resolver() or "").strip()
        if not model:
            raise AssistantReplyError("Codex assistant model is not configured")

        payload: Dict[str, Any] = {
            "model": model,
            "messages": copy.deepcopy(list(request.messages)),
            "timeout": max(1, int(request.timeout_seconds or 600)),
            "extra_body": {},
        }
        if request.history_enabled:
            payload["mabobot_history_enabled"] = True
            payload["mabobot_history_request_id"] = request.history_request_id
            payload["mabobot_history_max_calls"] = request.history_max_calls
            payload["mabobot_history_max_bytes"] = request.history_max_bytes
        if request.incremental_context:
            payload["mabobot_incremental_context"] = True
        if request.fresh_context:
            payload["mabobot_fresh_context"] = True
        effort = str(request.reasoning_effort or "inherit").strip().lower()
        if effort == "inherit" and profile:
            effort = str(profile.get("reasoning_effort") or "inherit").strip().lower()
        if effort in self._REASONING_EFFORTS:
            payload["reasoning_effort"] = effort
        summary = str(request.reasoning_summary or "inherit").strip().lower()
        if summary in self._REASONING_SUMMARIES:
            payload["codex_reasoning_summary"] = summary
        search_mode = str(request.web_search_mode or "inherit").strip().lower()
        if search_mode in self._WEB_SEARCH_MODES:
            payload["codex_web_search"] = search_mode
        if request.output_schema:
            payload["output_schema"] = copy.deepcopy(request.output_schema)
        input_files = [
            copy.deepcopy(item)
            for item in request.input_files
            if isinstance(item, dict) and str(item.get("path") or "").strip()
        ]
        if input_files:
            payload["mabobot_input_files"] = input_files
        if request.allow_image_input:
            payload["mabobot_allow_image_input"] = True
            payload["extra_body"]["mabobot_allow_image_input"] = True
        resolved_profile = str((profile or {}).get("name") or requested_profile)
        if resolved_profile:
            payload["codex_runtime_profile"] = resolved_profile
        try:
            profile_context_window = int((profile or {}).get("context_window") or 0)
        except (TypeError, ValueError):
            profile_context_window = 0
        if profile_context_window >= 4096:
            # App Server token notifications are optional on third-party
            # providers. Persist the Profile metadata as the authoritative
            # context-window fallback for rotation and monitoring.
            payload["codex_model_context_window"] = profile_context_window

        started_at = time.monotonic()
        try:
            response = runtime.chat(
                payload,
                chat_id=chat_name,
                role_name=str(request.role_name or "").strip() or None,
                retry=bool(request.retry),
                max_turns=max(0, int(request.max_turns or 0)),
                allow_exec_fallback=bool(request.allow_exec_fallback),
                persistent_session=bool(request.persistent_session),
            )
        except Exception as exc:
            self._record_telemetry(
                request=request,
                model=model,
                response=getattr(exc, "billing_response", None),
                response_text="",
                response_time=time.monotonic() - started_at,
                backend="codex_runtime",
                success=False,
                error=str(exc),
            )
            raise AssistantReplyError(f"Codex assistant reply failed: {exc}") from exc
        duration = time.monotonic() - started_at

        normalized_response = response if isinstance(response, dict) else {}
        choices = _read(response, "choices", []) or []
        first_choice = choices[0] if choices else {}
        message = _read(first_choice, "message", {}) or {}
        text = str(_read(message, "content", "") or "").strip()
        attachments = self._extract_attachments(response)
        response_model = str(_read(response, "model", model) or model)
        backend = str(_read(response, "backend", "codex_runtime") or "codex_runtime")
        if not text and not attachments:
            self._record_telemetry(
                request=request,
                model=response_model,
                response=normalized_response,
                response_text="",
                response_time=duration,
                backend=backend,
                success=False,
                error="Codex assistant returned an empty response",
            )
            raise AssistantReplyError("Codex assistant returned an empty response")

        self._record_telemetry(
            request=request,
            model=response_model,
            response=normalized_response,
            response_text=text,
            response_time=duration,
            backend=backend,
            success=True,
        )

        return CodexReplyResult(
            text=text,
            attachments=attachments,
            usage=self._extract_usage(response),
            model=response_model,
            backend=backend,
            duration_seconds=duration,
            raw_response=copy.deepcopy(normalized_response),
        )

    def _record_telemetry(
        self,
        *,
        request: CodexReplyRequest,
        model: str,
        response: Optional[Dict[str, Any]],
        response_text: str,
        response_time: float,
        backend: str,
        success: bool,
        error: str = "",
    ) -> None:
        """Publish history without allowing telemetry failures to block replies."""
        if self._telemetry_recorder is None:
            return
        try:
            self._telemetry_recorder(
                messages=copy.deepcopy(list(request.messages)),
                response=copy.deepcopy(response) if isinstance(response, dict) else None,
                response_text=response_text,
                response_time=max(0.0, float(response_time or 0.0)),
                model_id=str(model or "unknown"),
                backend=str(backend or "codex_runtime"),
                success=bool(success),
                error=str(error or ""),
                metadata={
                    "chat_name": str(request.chat_name or ""),
                    "role_name": str(request.role_name or ""),
                    "history_mode": str(request.history_mode or "full"),
                    "_usage_capture": request.usage_capture,
                },
            )
        except Exception as exc:
            logger.warning("记录 Codex 回复调用历史失败：%s", exc)

    @staticmethod
    def _resolve_profile_runtime(profile_id: str) -> Any:
        from app.services.codex_profile_service import get_codex_runtime_registry

        return get_codex_runtime_registry().resolve(profile_id)

    @staticmethod
    def _extract_attachments(response: Any) -> List[Dict[str, Any]]:
        carriers: List[Any] = [response]
        choices = _read(response, "choices", []) or []
        if choices:
            first_choice = choices[0]
            message = _read(first_choice, "message", {}) or {}
            carriers.extend(
                [
                    first_choice,
                    message,
                    _read(first_choice, "additional_kwargs", {}),
                    _read(message, "additional_kwargs", {}),
                ]
            )

        attachments: List[Dict[str, Any]] = []
        seen_paths = set()
        for carrier in carriers:
            for item in _read(carrier, "attachments", []) or []:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "").strip()
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)
                attachments.append(copy.deepcopy(item))
        return attachments

    @staticmethod
    def _extract_usage(response: Any) -> Dict[str, Any]:
        usage = _read(response, "usage", {}) or {}
        if not isinstance(usage, dict):
            usage = {
                key: _read(usage, key)
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "input_tokens",
                    "output_tokens",
                )
                if _read(usage, key) is not None
            }
        return copy.deepcopy(usage)


_assistant_reply_gateway: Optional[CodexReplyGateway] = None


def get_assistant_reply_gateway() -> CodexReplyGateway:
    global _assistant_reply_gateway
    if _assistant_reply_gateway is None:
        def record_telemetry(**kwargs: Any) -> None:
            from app.services.llm_manager import get_llm_manager

            get_llm_manager().record_codex_reply(**kwargs)

        _assistant_reply_gateway = CodexReplyGateway(
            telemetry_recorder=record_telemetry,
        )
    return _assistant_reply_gateway
