"""Win32-only health and geometry repair for detached listener windows."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from copy import deepcopy

from mabowx.core.locks import ui_transaction
from mabowx.core.win32 import (
    enum_windows_by_pid,
    get_foreground_window,
    get_monitor_info,
    get_window_geometry,
    get_window_info,
    is_hung_window,
    is_windows,
    restore_window_no_activate,
)
from mabowx.core.window_health import (
    advance_window_repair_confirmation,
    build_window_observation,
    choose_window_recovery_rect,
    is_undersized_window_rect,
    is_usable_window_rect,
    window_observation_fingerprint,
)
from mabowx.logger import wxlog
from mabowx.param import WxParam
from mabowx.ui.base import compute_auto_resize_size
from mabowx.ui.main import is_wechat_qt_window_class


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


class ListenerWindowMonitor:
    """Repair confirmed offscreen and undersized detached listener windows.

    The monitor never traverses UIA, activates a window, closes a window, or
    recreates a listener.  It changes geometry only after the same HWND has
    been observed with unhealthy geometry for multiple scans.
    """

    def __init__(self, main_wnd, listener_manager) -> None:
        self.main_wnd = main_wnd
        self.listener_manager = listener_manager
        self.interval = _env_float(
            "MABOWX_LISTENER_WINDOW_REPAIR_INTERVAL_SEC",
            _env_float("WECHAT_LISTENER_WINDOW_AUTO_REPAIR_INTERVAL_SEC", 5.0, 1.0),
            1.0,
        )
        self.confirmations = _env_int(
            "MABOWX_LISTENER_WINDOW_REPAIR_CONFIRMATIONS",
            _env_int("WECHAT_LISTENER_WINDOW_AUTO_REPAIR_CONFIRMATION_PROBES", 2, 2),
            2,
        )
        self.retry_cooldown = _env_float(
            "MABOWX_LISTENER_WINDOW_REPAIR_RETRY_COOLDOWN_SEC",
            _env_float("WECHAT_LISTENER_WINDOW_AUTO_REPAIR_RETRY_COOLDOWN_SEC", 30.0, 5.0),
            5.0,
        )
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending: dict[str, dict] = {}
        self._last_valid_rect: dict[str, dict] = {}
        self._observations: dict[str, dict] = {}
        self._transitions = deque(maxlen=100)
        self._sequence = 0
        self._last_check_at = 0.0
        self._last_repair_at = 0.0
        self._last_result: dict = {}

    def start(self) -> None:
        if not is_windows():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="mabowx-listener-window-monitor",
                daemon=True,
            )
            self._thread.start()
        wxlog.info(
            "监听窗口 Win32 几何监控已启动: "
            f"interval={self.interval}s confirmations={self.confirmations}"
        )

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.run_once()
            except Exception as exc:
                wxlog.error(f"监听窗口几何监控异常: {exc}")

    def _window_payload(self, window) -> dict:
        return {
            "hwnd": int(window.hwnd),
            "title": str(window.title or ""),
            "class_name": str(window.class_name or ""),
            "pid": int(window.pid),
            **self._geometry(int(window.hwnd)),
        }

    def _geometry(self, hwnd: int) -> dict:
        geometry = get_window_geometry(hwnd)
        geometry["undersized"] = bool(
            getattr(self.main_wnd, "resize_enabled", True)
            and geometry.get("visible")
            and not geometry.get("iconic")
            and is_undersized_window_rect(
                geometry.get("window_rect"), self._fallback_rect(geometry)
            )
        )
        return geometry

    def _fallback_rect(self, geometry: dict | None = None) -> dict:
        try:
            monitors = get_monitor_info()
            if not monitors:
                return {}
            rect = (geometry or {}).get("window_rect") or {}

            def monitor_score(item):
                x, y = item["WorkPosition"]
                overlap = max(0, min(rect.get("right", 0), x + item["WorkWidth"])
                              - max(rect.get("left", 0), x)) * max(
                    0, min(rect.get("bottom", 0), y + item["WorkHeight"])
                    - max(rect.get("top", 0), y))
                return overlap, item["WorkHeight"]

            monitor = max(monitors, key=monitor_score)
            left, top = tuple(monitor.get("WorkPosition") or (0, 0))
            available_width = int(monitor.get("WorkWidth") or 0)
            available_height = int(monitor.get("WorkHeight") or 0)
            width, height = compute_auto_resize_size(
                *WxParam.CHAT_WINDOW_SIZE, available_width, available_height
            )
            # Win32 constrains real top-level windows to the monitor. Do not
            # compare their actual height with the requested 6000px UIA size.
            width = min(width, available_width)
            height = min(height, available_height)
            if width <= 0 or height <= 0:
                return {}
            return {
                "left": int(left),
                "top": int(top),
                "right": int(left) + width,
                "bottom": int(top) + height,
            }
        except Exception:
            return {}

    def _record_observations(
        self,
        expected: list[str],
        by_title: dict[str, list[dict]],
        now: float,
    ) -> dict[str, dict]:
        current: dict[str, dict] = {}
        with self._lock:
            expected_set = set(expected)
            for stale in list(self._observations):
                if stale not in expected_set:
                    self._observations.pop(stale, None)
            for name in expected:
                observation = {
                    "who": name,
                    "observed_at": now,
                    **build_window_observation(by_title.get(name, [])),
                }
                previous = self._observations.get(name)
                self._observations[name] = observation
                current[name] = deepcopy(observation)
                if (
                    previous is not None
                    and window_observation_fingerprint(previous)
                    == window_observation_fingerprint(observation)
                ):
                    continue
                self._sequence += 1
                transition = {
                    "sequence": self._sequence,
                    "at": now,
                    "who": name,
                    "from_state": previous.get("state") if previous else None,
                    "to_state": observation.get("state"),
                    "previous_observed_at": (
                        previous.get("observed_at") if previous else None
                    ),
                    "current": deepcopy(observation),
                }
                self._transitions.append(transition)
                wxlog.info(f"监听窗口状态变化: {transition}")
        return current

    def _repair(self, name: str, target: dict, recovery_rect: dict) -> dict:
        hwnd = int(target.get("hwnd") or 0)
        pid = int(target.get("pid") or 0)
        result = {
            "who": name,
            "hwnd": hwnd,
            "pid": pid,
            "recovery_rect": dict(recovery_rect or {}),
            "success": False,
            "reason": "",
        }
        if not hwnd or not is_usable_window_rect(recovery_rect):
            result["reason"] = "missing_window_or_recovery_rect"
            return result
        info = get_window_info(hwnd)
        if info is None:
            result["reason"] = "window_disappeared"
            return result
        if (
            info.pid != pid
            or info.title != name
            or not is_wechat_qt_window_class(info.class_name)
        ):
            result["reason"] = "window_identity_changed"
            return result
        if is_hung_window(hwnd):
            result["reason"] = "window_message_thread_hung"
            return result
        before = self._geometry(hwnd)
        result["before"] = before
        if not (before.get("unrecoverable_offscreen") or before.get("undersized")):
            result.update(success=True, reason="already_recovered", after=before)
            return result
        foreground_before = get_foreground_window()
        changed = restore_window_no_activate(hwnd, recovery_rect)
        time.sleep(0.1)
        after = self._geometry(hwnd)
        foreground_after = get_foreground_window()
        result.update({
            "after": after,
            "foreground_before": foreground_before,
            "foreground_after": foreground_after,
            "foreground_unchanged": foreground_before == foreground_after,
            "success": bool(
                changed
                and after.get("visible")
                and not after.get("iconic")
                and not after.get("unrecoverable_offscreen")
                and not after.get("undersized")
                and is_usable_window_rect(after.get("window_rect"))
                and is_usable_window_rect(after.get("normal_rect"))
            ),
        })
        result["reason"] = "recovered" if result["success"] else "verification_failed"
        return result

    def run_once(self) -> dict:
        now = time.time()
        expected = self.listener_manager.active_names()
        pid = int(getattr(self.main_wnd, "pid", 0) or 0)
        windows = [
            self._window_payload(window)
            for window in enum_windows_by_pid(pid)
            if window.title and is_wechat_qt_window_class(window.class_name)
        ] if pid and is_windows() else []
        by_title: dict[str, list[dict]] = {}
        for window in windows:
            by_title.setdefault(str(window.get("title") or ""), []).append(window)
            normal = window.get("normal_rect") or {}
            current = window.get("window_rect") or {}
            candidate = normal if is_usable_window_rect(normal) else current
            if (is_usable_window_rect(candidate)
                    and not is_undersized_window_rect(candidate, self._fallback_rect(window))):
                self._last_valid_rect[str(window.get("title") or "")] = dict(candidate)

        result = {
            "checked_at": now,
            "expected": expected,
            "window_count": len(windows),
            "suspected": [],
            "repairs": [],
            "status": "idle" if not expected else "checked",
        }
        result["observations"] = self._record_observations(expected, by_title, now)
        for name in expected:
            matches = by_title.get(name, [])
            if len(matches) != 1 or not matches[0].get("visible"):
                self._pending.pop(name, None)
                continue
            target = matches[0]
            previous = self._pending.get(name)
            state, due = advance_window_repair_confirmation(
                previous,
                hwnd=int(target.get("hwnd") or 0),
                unhealthy=bool(target.get("unrecoverable_offscreen") or target.get("undersized")),
                now=now,
                threshold=self.confirmations,
            )
            if not state:
                self._pending.pop(name, None)
                continue
            if previous and previous.get("hwnd") == state["hwnd"]:
                for key in ("next_attempt_at", "last_attempt_at", "last_error"):
                    if key in previous:
                        state[key] = previous[key]
            self._pending[name] = state
            result["suspected"].append({"who": name, **state})
            if not due or float(state.get("next_attempt_at") or 0) > now:
                continue

            peer_rects = []
            fallback = self._fallback_rect(target)
            for window in windows:
                if int(window.get("hwnd") or 0) == int(target.get("hwnd") or 0):
                    continue
                normal = window.get("normal_rect") or {}
                current = window.get("window_rect") or {}
                candidate = normal if is_usable_window_rect(normal) else current
                if not is_undersized_window_rect(candidate, fallback):
                    peer_rects.append(candidate)
            preferred = self._last_valid_rect.get(name)
            if is_undersized_window_rect(preferred, fallback):
                preferred = None
            recovery = choose_window_recovery_rect(
                preferred,
                peer_rects,
                fallback,
            )
            if target.get("undersized") and fallback:
                # Keep this window on its monitor and move a bottom-edge
                # shrunken window up so the restored area is actually usable.
                current = target["window_rect"]
                width = max(current["right"] - current["left"],
                            recovery["right"] - recovery["left"])
                height = max(current["bottom"] - current["top"],
                             recovery["bottom"] - recovery["top"])
                left = max(fallback["left"], min(current["left"], fallback["right"] - width))
                top = max(fallback["top"], min(current["top"], fallback["bottom"] - height))
                recovery = dict(left=left, top=top, right=left + width, bottom=top + height)
            try:
                with ui_transaction(timeout=0.1):
                    repair = self._repair(name, target, recovery)
            except TimeoutError:
                state["last_error"] = "ui_busy"
                continue
            state["last_attempt_at"] = time.time()
            result["repairs"].append(repair)
            if repair.get("success"):
                self._pending.pop(name, None)
                self._last_repair_at = time.time()
                result["status"] = "recovered"
                wxlog.warning(
                    f"监听窗口位置/尺寸已恢复: who={name!r} hwnd={repair.get('hwnd')}"
                )
            else:
                state["last_error"] = repair.get("reason")
                state["next_attempt_at"] = time.time() + self.retry_cooldown
                result["status"] = "repair_failed"
                wxlog.error(
                    f"监听窗口位置/尺寸恢复失败: who={name!r} reason={repair.get('reason')}"
                )
        with self._lock:
            self._last_check_at = now
            self._last_result = deepcopy(result)
        return result

    def status(self) -> dict:
        now = time.time()
        thread = self._thread
        with self._lock:
            return {
                "enabled": is_windows(),
                "always_on": True,
                "running": bool(thread and thread.is_alive()),
                "mode": "win32_geometry_only",
                "interval_sec": self.interval,
                "confirmation_probes": self.confirmations,
                "last_check_at": self._last_check_at or None,
                "last_check_age_sec": (
                    round(max(0.0, now - self._last_check_at), 1)
                    if self._last_check_at
                    else None
                ),
                "last_repair_at": self._last_repair_at or None,
                "pending": deepcopy(self._pending),
                "observation_audit": {
                    "mode": "semantic_state_changes_only",
                    "tracked": deepcopy(self._observations),
                    "recent_transitions": deepcopy(list(self._transitions)[-20:]),
                },
                "last_result": deepcopy(self._last_result),
            }
