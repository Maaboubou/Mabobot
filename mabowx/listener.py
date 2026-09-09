"""独立窗口消息监听器。

设计目标：`WeChat.AddListenChat(nickname, callback)` 打开独立聊天窗口后，
在后台按固定间隔轮询 `Chat.GetNewMessage()`，并把新消息提交给回调。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from mabowx.core import uia
from mabowx.core.win32 import get_process_name
from mabowx.logger import wxlog
from mabowx.param import WxParam


def is_embedded_browser_control(control, process_name: str = "") -> bool:
    """判断顶层控件是否为微信内置浏览器。"""
    if control is None:
        return False
    try:
        class_name = str(getattr(control, "ClassName", "") or "")
        control_type = str(getattr(control, "ControlTypeName", "") or "")
        name = str(getattr(control, "Name", "") or "")
    except Exception:
        return False
    browser_shape = class_name == "Chrome_WidgetWin_0" or (
        control_type == "PaneControl" and name == "微信"
    )
    return browser_shape and process_name.casefold() == "wechatappex.exe"


def _embedded_browser_is_foreground() -> bool:
    try:
        foreground = uia.GetForegroundControl()
        top = foreground.GetTopLevelControl() if foreground is not None else None
        top = top or foreground
        pid = int(getattr(top, "ProcessId", 0) or 0)
        return is_embedded_browser_control(top, get_process_name(pid))
    except Exception:
        return False


class ChatListener:
    """管理一个独立聊天窗口的监听任务。"""

    def __init__(
        self,
        nickname: str,
        chat,
        callback: Callable,
        executor: ThreadPoolExecutor,
        interval: float | None = None,
    ) -> None:
        self.nickname = nickname
        self.chat = chat
        self.callback = callback
        self.executor = executor
        self.interval = interval if interval is not None else WxParam.LISTEN_INTERVAL
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._callback_lock = threading.Lock()
        self._callback_queue = deque()
        self._callback_worker_running = False
        self._last_poll_at: float | None = None
        self._last_poll_started = time.monotonic()
        self._last_poll_finished = self._last_poll_started
        self._last_delivery_at: float | None = None
        self._poll_errors = 0
        self._last_poll_error: str | None = None
        self._last_delivery_state = "starting"

    def delivery_status(self) -> dict:
        """Read cached state only, including a stalled poll or missing cursor."""
        core = getattr(self.chat, "core", None)
        box = getattr(core, "_chatbox", None)
        read_status = getattr(box, "message_delivery_status", None)
        status = read_status() if callable(read_status) else {"state": "starting"}
        age = max(0.0, time.monotonic() - self._last_poll_finished)
        if not self.is_alive:
            status["state"] = "stopped"
        elif age > max(120.0, self.interval * 5):
            status["state"] = "stalled"
        elif self._poll_errors:
            status["state"] = "poll_error"
        return {
            **status,
            "last_poll_at": self._last_poll_at,
            "last_poll_age_sec": round(age, 1),
            "last_delivery_at": self._last_delivery_at,
            "consecutive_poll_errors": self._poll_errors,
            "last_poll_error": self._last_poll_error,
            "callback_queue_size": len(self._callback_queue),
        }

    def _try_rebind(self) -> bool:
        """用主窗口重新枚举同名独立窗口，替换失效的 Chat/UIA 对象。"""
        try:
            old_core = getattr(self.chat, "core", None)
            root = getattr(old_core, "root", None)
            getter = getattr(root, "get_sub_wnd", None)
            if not callable(getter):
                return False
            try:
                # 监听窗口关闭后，旧 UIA 对象的 Exists() 可能短暂返回 True。
                # 强制从 Win32 顶层窗口重新枚举，绝不能把缓存对象当成重绑成功。
                fresh = getter(self.nickname, force_refresh=True)
            except TypeError:
                # 仅为第三方自定义 root 保留兼容；mabowx 主窗口支持强制刷新。
                fresh = getter(self.nickname)
            fresh_core = getattr(fresh, "core", None)
            if fresh_core is None or not fresh_core.exists():
                return False
            try:
                fresh_core.get_chatbox().prime_message_cache(
                    settle_time=0.6,
                    interval=0.15,
                    stable_rounds=2,
                )
            except Exception:
                pass
            self.chat = fresh
            wxlog.info(f"监听窗口 UIA 对象已重新绑定: {self.nickname!r}")
            return True
        except Exception as exc:
            wxlog.debug(f"监听窗口重新绑定尚未成功: {self.nickname}: {exc}")
            return False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name=f"mabowx-listen-{self.nickname}", daemon=True)
        self._thread.start()
        wxlog.info(f"监听线程已启动: {self.nickname!r} interval={self.interval}s")

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        wxlog.info(f"监听线程已停止: {self.nickname!r}")

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()

    def _run(self) -> None:
        missing_since: float | None = None
        last_rebind_attempt = 0.0
        while not self._stop_event.is_set():
            try:
                if not self.chat.core.exists():
                    now = time.monotonic()
                    if missing_since is None:
                        missing_since = now
                        wxlog.warning(
                            f"监听窗口暂时不可用，进入重绑宽限期: {self.nickname}"
                        )
                    if now - last_rebind_attempt >= max(0.5, self.interval):
                        last_rebind_attempt = now
                        if self._try_rebind():
                            missing_since = None
                            continue
                    if now - missing_since >= WxParam.LISTENER_WINDOW_MISSING_GRACE:
                        wxlog.warning(
                            f"监听窗口已关闭: {self.nickname} "
                            f"missing_for={now - missing_since:.1f}s"
                        )
                        break
                    self._stop_event.wait(self.interval)
                    continue
                missing_since = None
                self._last_poll_started = time.monotonic()
                messages = self.chat.GetNewMessage()
                self._last_poll_finished = time.monotonic()
                self._last_poll_at = time.time()
                self._poll_errors = 0
                self._last_poll_error = None
                status = self.delivery_status()
                if status["state"] != self._last_delivery_state:
                    wxlog.info(
                        f"监听消息交付状态变化: chat={self.nickname!r} "
                        f"from={self._last_delivery_state} to={status['state']} "
                        f"failures={status.get('recovery_failures', 0)} "
                        f"retry_in_sec={status.get('retry_in_sec', 0)}"
                    )
                    self._last_delivery_state = status["state"]
                if messages:
                    self._last_delivery_at = time.time()
                    wxlog.info(
                        f"监听消息交付批次: chat={self.nickname!r} count={len(messages)} "
                        f"poll_ms={(self._last_poll_finished - self._last_poll_started) * 1000:.1f} "
                        f"sequence={getattr(messages[0], 'delivery_sequence', None)}"
                        f"..{getattr(messages[-1], 'delivery_sequence', None)}"
                    )
                    # 哪个聊天来了新消息，先把该聊天的独立窗口切到前台，
                    # 再触发回调。
                    if _embedded_browser_is_foreground():
                        wxlog.debug(
                            f"检测到新消息，但微信内置浏览器正在前台，保留焦点: {self.nickname}"
                        )
                    else:
                        try:
                            self.chat.Show()
                            wxlog.debug(f"检测到新消息，独立窗口已切到前台: {self.nickname}")
                        except Exception as exc:
                            wxlog.warning(f"新消息窗口前台切换失败: {self.nickname}: {exc}")
                    for message in messages:
                        if self._stop_event.is_set():
                            break
                        self._submit_callback(message)
            except Exception as exc:
                self._poll_errors += 1
                self._last_poll_error = f"{type(exc).__name__}: {exc}"
                wxlog.exception(f"监听消息失败: {self.nickname}: {exc}")
            self._stop_event.wait(self.interval)

    def _submit_callback(self, message) -> None:
        """按聊天串行执行回调，同时保留不同聊天之间的并发能力。"""
        with self._callback_lock:
            self._callback_queue.append(message)
            if self._callback_worker_running:
                return
            self._callback_worker_running = True

        try:
            self.executor.submit(self._drain_callbacks)
        except Exception as exc:
            with self._callback_lock:
                self._callback_worker_running = False
            wxlog.error(f"监听消息回调提交失败: {self.nickname}: {exc}")

    def _drain_callbacks(self) -> None:
        while True:
            with self._callback_lock:
                if not self._callback_queue:
                    self._callback_worker_running = False
                    return
                message = self._callback_queue.popleft()

            try:
                result = self.callback(message, self.chat)
                if result == WxParam.CALLBACK_STOP_SIGN:
                    self._stop_event.set()
                    with self._callback_lock:
                        self._callback_queue.clear()
                        self._callback_worker_running = False
                    return
            except Exception as exc:
                wxlog.error(f"监听消息回调发生错误: {self.nickname}: {exc}")


class ListenerManager:
    """管理多个 ChatListener。"""

    def __init__(self, executor_workers: int | None = None) -> None:
        self.executor = ThreadPoolExecutor(
            max_workers=executor_workers or WxParam.LISTENER_EXCUTOR_WORKERS,
            thread_name_prefix="mabowx-callback",
        )
        self.listeners: dict[str, ChatListener] = {}
        self._lock = threading.RLock()
        self._shutdown = False

    def add(self, nickname: str, chat, callback: Callable) -> bool:
        with self._lock:
            if self._shutdown:
                return False
            if nickname in self.listeners:
                return False
            listener = ChatListener(nickname, chat, callback, self.executor)
            self.listeners[nickname] = listener
            listener.start()
            return True

    def remove(self, nickname: str, timeout: float = 3.0) -> bool:
        with self._lock:
            listener = self.listeners.pop(nickname, None)
            if listener is None:
                return False
            listener.stop(timeout=timeout)
            return True

    def rebind(self, nickname: str, chat, callback: Callable | None = None) -> bool:
        """把存活监听任务原子地切换到新 Chat/UIA 对象。"""
        with self._lock:
            listener = self.listeners.get(nickname)
            if listener is None or not listener.is_alive:
                return False
            listener.chat = chat
            if callable(callback):
                listener.callback = callback
            wxlog.info(f"监听任务已热重绑: {nickname!r}")
            return True

    def stop_all(self, remove: bool = True, timeout: float = 3.0) -> None:
        with self._lock:
            for listener in list(self.listeners.values()):
                listener.stop(timeout=timeout)
            if remove:
                self.listeners.clear()

    def shutdown(self, timeout: float = 3.0, wait: bool = False) -> None:
        """停止监听并关闭回调线程池，供进程退出/连接销毁使用。"""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self.stop_all(remove=True, timeout=timeout)
        self.executor.shutdown(wait=wait, cancel_futures=True)

    def active_names(self) -> list[str]:
        """Return registered listener names, including a task winding down."""
        with self._lock:
            return sorted(self.listeners)

    def running_names(self) -> list[str]:
        """Return only listener tasks whose polling threads are alive."""
        with self._lock:
            return sorted(
                name for name, listener in self.listeners.items() if listener.is_alive
            )

    def delivery_status(self) -> dict[str, dict]:
        with self._lock:
            listeners = list(self.listeners.items())
        return {name: listener.delivery_status() for name, listener in listeners}

    def get_chat(self, nickname: str):
        """获取监听中的 Chat 对象；未监听返回 None。"""
        with self._lock:
            listener = self.listeners.get(nickname)
            return listener.chat if listener is not None else None

    def active_count(self) -> int:
        with self._lock:
            return len(self.listeners)

    def is_running(self) -> bool:
        with self._lock:
            return any(listener.is_alive for listener in self.listeners.values())

    def only_chat(self):
        """只有一个监听任务时返回其 Chat，否则返回 None。"""
        with self._lock:
            if len(self.listeners) == 1:
                return next(iter(self.listeners.values())).chat
            return None
