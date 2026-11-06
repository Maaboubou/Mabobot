"""Daily Codex-only update discovery, independent of open browser pages."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from app.services.codex_upgrade_service import CodexUpgradeService, _daily_check_slot, get_codex_upgrade_service


logger = logging.getLogger(__name__)


class CodexUpdateMonitor:
    def __init__(
        self, service: Optional[CodexUpgradeService] = None, *, interval: float = 30,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.service = service or get_codex_upgrade_service()
        self.interval = max(1, interval)
        self._clock = clock or time.time
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive() and not self._stop.is_set():
                return
            # A previous bounded network check may still be finishing after
            # stop(). Its own event remains set and its lease prevents overlap.
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._loop, args=(self._stop,), name="codex-update-monitor", daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)

    def tick(self) -> bool:
        # SQLite's daily slot and lease make this idempotent across restarts,
        # multiple web workers, and manual checks. No other tool is queried.
        return self.service.check_latest(scheduled=True) is not None

    def _wait_seconds(self) -> float:
        now = self._clock()
        return min(self.interval, max(0.01, _daily_check_slot(now) + 86400 - now))

    def _loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                # The service persists the failure while retaining the last
                # successful result. Manual retry remains available at once.
                logger.warning("Daily Codex version check failed", exc_info=True)
            stop.wait(self._wait_seconds())


_monitor: Optional[CodexUpdateMonitor] = None
_monitor_lock = threading.Lock()


def get_codex_update_monitor() -> CodexUpdateMonitor:
    global _monitor
    if _monitor is None:
        with _monitor_lock:
            if _monitor is None:
                _monitor = CodexUpdateMonitor()
    return _monitor
