"""Small single-flight helper for expensive idempotent operations."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Hashable, Optional, Tuple


_MISSING = object()


@dataclass
class _OperationState:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = _MISSING
    error: Optional[BaseException] = None


class InFlightDeduplicator:
    """Run one operation per key while callers share its result.

    Successful results are cached briefly so an HTTP retry arriving just after
    completion does not repeat the underlying UI operation.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 120.0,
        wait_timeout_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.wait_timeout_seconds = max(0.1, float(wait_timeout_seconds))
        self._clock = clock
        self._lock = threading.Lock()
        self._inflight: Dict[Hashable, _OperationState] = {}
        self._cache: Dict[Hashable, Tuple[float, Any]] = {}

    def run(
        self,
        key: Hashable,
        operation: Callable[[], Any],
        *,
        cache_validator: Optional[Callable[[Any], bool]] = None,
    ) -> Any:
        """Return the cached/shared result or run ``operation`` once."""

        with self._lock:
            self._prune_cache_locked()
            cached = self._cache.get(key)
            if cached is not None:
                result = cached[1]
                if cache_validator is None or cache_validator(result):
                    return result
                self._cache.pop(key, None)

            state = self._inflight.get(key)
            owner = state is None
            if owner:
                state = _OperationState()
                self._inflight[key] = state

        if not owner:
            if not state.event.wait(self.wait_timeout_seconds):
                raise TimeoutError(f"Timed out waiting for in-flight operation: {key!r}")
            if state.error is not None:
                # Preserve domain errors so every waiter gets the same retry
                # policy (a media identity refusal must not become HTTP 500).
                raise state.error
            if state.result is _MISSING:
                raise RuntimeError(f"In-flight operation completed without a result: {key!r}")
            return state.result

        try:
            result = operation()
            state.result = result
            if cache_validator is None or cache_validator(result):
                with self._lock:
                    self._cache[key] = (self._clock(), result)
            return result
        except BaseException as exc:
            state.error = exc
            raise
        finally:
            with self._lock:
                if self._inflight.get(key) is state:
                    self._inflight.pop(key, None)
            state.event.set()

    def invalidate(self, key: Hashable) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def _prune_cache_locked(self) -> None:
        if not self._cache:
            return
        cutoff = self._clock() - self.ttl_seconds
        expired = [key for key, (stored_at, _result) in self._cache.items() if stored_at < cutoff]
        for key in expired:
            self._cache.pop(key, None)
