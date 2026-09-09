"""mabowx 公开 API。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from mabowx.core.locks import uilock
from mabowx.listener import ListenerManager
from mabowx.listener_monitor import ListenerWindowMonitor
from mabowx.logger import wxlog
from mabowx.param import WxParam, WxResponse
from mabowx.ui.main import WeChatLoginWnd, WeChatMainWnd


class WeChat:
    """微信主对象。

    V1 当前里程碑只实现 A 类基础能力；B/C/D/E/F/G 将在后续里程碑逐步开放。
    """

    def __init__(
        self,
        nickname: str | None = None,
        start_listener: bool = False,
        debug: bool = False,
        resize: bool = True,
        version: str = "微信",
        **kwargs: Any,
    ) -> None:
        wxlog.set_debug(debug)
        self.nickname = nickname
        self.debug = debug
        self.version_name = version
        self.myinfo: dict[str, str] | None = None
        self.listener_manager = ListenerManager()
        # ``listen`` 是稳定的公开字典，同时有后台同步线程读取。
        # 内部的增删/补注册必须原子化，避免 RemoveListenChat 刚停止监听，
        # 同步线程又凭旧快照把同名监听复活。
        self._listen_registry_lock = threading.RLock()
        # 初始不宣称监听循环已运行：Mabobot 恢复“已存在窗口”时会直接写
        # wx.listen，然后调用 StartListening()。只有 StartListening 或
        # AddListenChat 真正启动轮询线程后才把状态置 True。
        self._listener_started = False
        # Mabobot 会直接读写 wx.listen 并检查监听线程状态。
        self.listen: dict = {}
        self._listener_is_listening = False
        self._dispose_lock = threading.RLock()
        self._dispose_event = threading.Event()
        self._disposed = False
        # 监听注册表同步线程：Mabobot 可能直接向 wx.listen 写入已存在的
        # (Chat, callback)，且只调用一次 StartListening。这里持续把未注册
        # 的条目补进 listener_manager，避免第二/后续会话漏监听。
        self._listen_sync_stop = threading.Event()
        self._listen_sync_thread = threading.Thread(
            target=self._listen_registry_sync_loop,
            name="mabowx-listen-registry-sync",
            daemon=True,
        )
        self._listen_sync_thread.start()
        self.main_wnd = WeChatMainWnd(
            nickname=nickname,
            debug=debug,
            resize=resize,
            version=version,
        )
        self.listener_window_monitor = ListenerWindowMonitor(
            self.main_wnd,
            self.listener_manager,
        )
        self.listener_window_monitor.start()

    def __repr__(self) -> str:
        return f"<mabowx WeChat pid={self.main_wnd.pid}>"

    @property
    def path(self) -> str | None:
        return self.main_wnd.path

    @property
    def dir(self) -> str | None:
        return self.main_wnd.dir

    @property
    def wechat_version(self) -> str | None:
        return self.main_wnd.wechat_version

    @uilock
    def Show(self) -> None:
        """显示并激活微信主窗口。"""
        self.main_wnd.show()

    def IsOnline(self) -> bool:
        """判断微信是否已登录。"""
        return bool(self.main_wnd.exists())

    def KeepRunning(self) -> None:
        """保持当前 Python 进程运行，直到微信退出。"""
        try:
            while not self._dispose_event.is_set() and self.IsOnline():
                self._dispose_event.wait(1)
        except KeyboardInterrupt:
            pass

    def Dispose(self, timeout: float = 3.0, wait: bool = False) -> None:
        """释放 mabowx 后台监听资源，但不关闭微信客户端窗口。"""
        with self._dispose_lock:
            if self._disposed:
                return
            self._disposed = True
            self._dispose_event.set()
            self._listen_sync_stop.set()
        if self._listen_sync_thread.is_alive():
            self._listen_sync_thread.join(timeout=timeout)
        self.listener_window_monitor.stop(timeout=timeout)
        with self._listen_registry_lock:
            self.listen.clear()
            self._listener_started = False
            self._listener_is_listening = False
        self.listener_manager.shutdown(timeout=timeout, wait=wait)
        wxlog.info("mabowx 后台监听资源已释放")

    def ShutDown(self) -> None:
        """关闭微信进程。"""
        self.main_wnd.shutdown()

    # ------------------------------------------------------------------
    # M2：会话与窗口切换
    # ------------------------------------------------------------------

    def GetSession(self):
        """获取当前会话列表。"""
        return self.main_wnd.get_session_box().get_session()

    def ChatWith(
        self,
        who: str,
        exact: bool = True,
        force: bool = False,
        force_wait: float | int = 0.5,
    ) -> WxResponse:
        """打开指定聊天窗口。"""
        return self.main_wnd.get_session_box().switch_chat(
            who=who,
            exact=exact,
            force=force,
            force_wait=force_wait,
        )

    @uilock
    def GetSubWindow(self, nickname: str):
        """获取指定昵称的独立聊天子窗口。"""
        return self.main_wnd.get_sub_wnd(nickname)

    @uilock
    def GetAllSubWindow(self):
        """获取所有独立聊天子窗口。"""
        return self.main_wnd.get_all_sub_wnds()

    def GetListenChat(self, nickname: str, *, include_open_window: bool = True):
        """Return the live registered listener chat for ``nickname``.

        The public method hides listener-manager/registry layout from hosts.
        When requested, a detached window that survived a process reconnect is
        returned so :meth:`BindListenChat` can attach it without reopening UI.
        """
        nickname = str(nickname or "").strip()
        if not nickname:
            return None
        chat = self.listener_manager.get_chat(nickname)
        core = getattr(chat, "core", None)
        if chat is not None and core is not None and core.exists():
            return chat
        with self._listen_registry_lock:
            entry = self.listen.get(nickname)
        if isinstance(entry, (tuple, list)) and entry:
            chat = entry[0]
        else:
            chat = entry
        core = getattr(chat, "core", None)
        if chat is not None and core is not None and core.exists():
            return chat
        if not include_open_window:
            return None
        chat = self.main_wnd.get_sub_wnd(nickname, force_refresh=True)
        core = getattr(chat, "core", None)
        return chat if core is not None and core.exists() else None

    def GetListenNames(self, *, include_open_windows: bool = True) -> list[str]:
        """Return a fast, UIA-free snapshot of listener/detached-window names."""
        names = set(self.listener_manager.active_names())
        with self._listen_registry_lock:
            names.update(str(name or "").strip() for name in self.listen)
        if include_open_windows:
            names.update(self.main_wnd.get_sub_window_names())
        names.discard("")
        return sorted(names)

    def IsListening(self) -> bool:
        """Whether the listener loop is enabled and has live tasks."""
        if not self._listener_started or not self._listener_is_listening:
            return False
        return self.listener_manager.is_running()

    def EnsureListening(self) -> None:
        """Start any registered listener tasks that are not running."""
        if not self.IsListening():
            self.StartListening()

    def GetListenerStatus(self) -> dict[str, Any]:
        """Return the library-owned listener snapshot used by health APIs."""
        return {
            "actual": self.listener_manager.running_names(),
            "registered": self.listener_manager.active_names(),
            "windows": self.main_wnd.get_sub_window_names(),
            "running": self.IsListening(),
            "message_delivery": self.listener_manager.delivery_status(),
            "window_auto_repair": self.listener_window_monitor.status(),
        }

    def SwitchToChat(self) -> bool:
        """切换到聊天页面。"""
        return self.main_wnd.switch_chat_page()

    def SwitchToContact(self) -> bool:
        """切换到通讯录页面。"""
        return self.main_wnd.switch_contact_page()

    def OpenSeparateWindow(self, who: str) -> WxResponse:
        """在独立窗口打开指定会话。"""
        return self.main_wnd.open_separate_window(who)

    @uilock
    def GetMyGroupNickname(self, who: str) -> str:
        """只读返回当前账号在指定群里的昵称。"""
        who = str(who or "").strip()
        if not who:
            raise ValueError("群名称不能为空")
        chat = self.GetListenChat(who, include_open_window=True)
        if chat is not None:
            bound_name = str(getattr(chat, "who", "") or "").strip()
            if bound_name != who:
                raise RuntimeError(
                    f"监听窗口绑定错位：期望={who!r}，实际={bound_name!r}"
                )
            return chat.GetMyGroupNickname()
        response = self.ChatWith(who, exact=True)
        if not response.is_success:
            raise RuntimeError(response["message"])
        info = self.ChatInfo()
        if (
            str(info.get("chat_name") or "").strip() != who
            or str(info.get("chat_type") or "").lower() != "group"
        ):
            raise RuntimeError(f"群聊校验失败：{who}")
        return self.main_wnd.get_chatbox().get_group_my_nickname()

    # ------------------------------------------------------------------
    # M3：消息发送
    # ------------------------------------------------------------------

    def _switch_and_validate(
        self,
        who: str | None,
        exact: bool,
        max_retries: int,
    ) -> WxResponse:
        if not who:
            title = self.main_wnd.get_current_chat_name()
            if title:
                return WxResponse.success()
            return WxResponse.failure("当前没有打开任何聊天窗口")
        for attempt in range(1, max_retries + 1):
            response = self.ChatWith(who, exact=exact)
            if not response.is_success:
                wxlog.warning(f"切换聊天对象失败，第 {attempt}/{max_retries} 次: {response['message']}")
                continue
            title = self.main_wnd.get_current_chat_name() or ""
            if exact and title == who:
                return WxResponse.success()
            if not exact and who in title:
                return WxResponse.success()
            wxlog.warning(f"切换聊天对象校验失败: title={title!r}, 第 {attempt}/{max_retries} 次")
        return WxResponse.failure("切换聊天对象失败，为避免发送错误，取消发送")

    def _resolve_listener_chat(self, who: str | None):
        """监听优先：如果有匹配的监听独立窗口，直接使用该窗口发送。

        这是为了避免 AddListenChat 之后又切回主窗口操作，导致目标漂移。
        """
        if who:
            chat = self.listener_manager.get_chat(who)
            if chat is not None and chat.core.exists():
                return chat, None
            return None, None
        count = self.listener_manager.active_count()
        if count == 1:
            chat = self.listener_manager.only_chat()
            if chat is not None and chat.core.exists():
                return chat, None
        if count > 1:
            return None, WxResponse.failure("存在多个监听窗口，who=None 无法确定发送目标")
        return None, None

    @uilock
    def SendMsg(
        self,
        msg: str,
        who: str | None = None,
        clear: bool = True,
        at: str | list[str] | None = None,
        exact: bool = False,
        max_retries: int = 3,
    ) -> WxResponse:
        """可靠发送文本，并把目标选择、精确 @ 和一次安全重试封装在库内。

        只有底层明确返回失败时才重试一次。异常可能发生在微信已经执行
        动作之后，因此异常路径绝不自动重试，避免重复发送。
        """
        if not msg:
            return WxResponse.failure("消息内容不能为空")
        listener_chat, error = self._resolve_listener_chat(who)
        if error is not None:
            return error

        wxlog.info(
            f"发送文本消息: who={who or '当前聊天'!r} "
            f"preview={msg[:30]!r} at={bool(at)}"
        )
        attempts: list[dict[str, Any]] = []

        if listener_chat is not None:
            route = "listener_chat"

            def operation():
                return listener_chat.SendMsg(msg=msg, clear=clear, at=at)
        else:
            route = "main_window"

            def operation():
                response = self._switch_and_validate(who, exact, max_retries)
                if not response.is_success:
                    return response
                return self.main_wnd.get_chatbox().send_text(
                    msg=msg,
                    clear=clear,
                    at=at,
                )

        def run_attempt(attempt_route: str):
            started_at = time.monotonic()
            try:
                response = operation()
            except Exception as exc:
                attempts.append({
                    "route": attempt_route,
                    "success": False,
                    "elapsed_ms": round((time.monotonic() - started_at) * 1000, 1),
                    "response": None,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                return None, exc
            payload = (
                response.to_dict()
                if callable(getattr(response, "to_dict", None))
                else dict(response) if isinstance(response, dict) else str(response)
            )
            attempts.append({
                "route": attempt_route,
                "success": bool(response),
                "elapsed_ms": round((time.monotonic() - started_at) * 1000, 1),
                "response": payload,
                "error": None,
            })
            return response, None

        response, raised = run_attempt(route)
        if raised is not None:
            return WxResponse.error(str(raised), {"route": route, "attempts": attempts})
        if response:
            return WxResponse.success(
                "消息已发送",
                {"route": route, "attempts": attempts},
            )

        # An explicit false response proves no send was confirmed.  Refresh the
        # same target once, then retry without changing routes.
        try:
            if listener_chat is not None:
                listener_chat.Show()
            else:
                self.Show()
        except Exception as exc:
            wxlog.warning(f"发送重试前激活窗口失败: who={who!r} error={exc}")
        retry_route = f"{route}_retry"
        response, raised = run_attempt(retry_route)
        data = {"route": retry_route, "attempts": attempts}
        if raised is not None:
            return WxResponse.error(str(raised), data)
        if response:
            return WxResponse.success("消息已发送", data)
        message = "消息发送失败"
        if isinstance(response, dict):
            message = str(response.get("message") or message)
        return WxResponse.failure(message, data)

    @uilock
    def SendFiles(
        self,
        filepath,
        who: str | None = None,
        exact: bool = False,
        max_retries: int = 3,
    ) -> WxResponse:
        """发送一个或多个文件。"""
        if isinstance(filepath, (list, tuple)):
            filepaths = list(filepath)
        else:
            filepaths = [filepath]
        if not filepaths:
            return WxResponse.failure("文件列表不能为空")

        listener_chat, error = self._resolve_listener_chat(who)
        if error is not None:
            return error
        wxlog.info(
            f"发送文件消息: who={who or '当前聊天'!r} files={[Path(p).name for p in filepaths]}"
        )
        if listener_chat is not None:
            return listener_chat.SendFiles(filepaths)
        response = self._switch_and_validate(who, exact, max_retries)
        if not response.is_success:
            return response
        return self.main_wnd.get_chatbox().send_files(filepaths)

    # ------------------------------------------------------------------
    # 以下为 V1 后续里程碑接口占位。现在调用会显式抛出 NotImplementedError。
    # ------------------------------------------------------------------

    def _not_ready(self, feature: str) -> None:
        raise NotImplementedError(f"{feature} 尚未在当前里程碑实现")

    def GetAllMessage(self):
        """获取当前聊天窗口的所有可见消息。"""
        return self.main_wnd.get_chatbox().get_messages()

    def GetNewMessage(self):
        """获取当前聊天窗口自上次缓存之后的新消息。"""
        return self.main_wnd.get_chatbox().get_new_messages()

    def GetNextNewMessage(
        self,
        filter_mute: bool = False,
        callback=None,
        timeout: float | None = 10.0,
    ) -> dict[str, list]:
        """轮询会话列表，返回第一个出现新消息的会话。

        只检查带 ``[N条]`` 未读标记的会话，不会逐个打开所有会话。
        """
        import re
        import time as _time

        unread_re = re.compile(r"\[\d+条\]")
        deadline = None if timeout is None else _time.monotonic() + timeout

        def _emit(chat_name: str, messages: list) -> dict[str, list]:
            if callback is not None:
                for message in messages:
                    try:
                        callback(message)
                    except Exception:
                        pass
            return {chat_name: messages}

        while deadline is None or _time.monotonic() < deadline:
            # 1) 当前聊天页如果已有消息缓存，优先检查新消息。
            current_title = self.main_wnd.get_current_chat_name()
            if current_title:
                current_box = self.main_wnd.get_chatbox()
                if current_box._used_msg_ids:
                    current_messages = current_box.get_new_messages()
                    if current_messages:
                        return _emit(current_title, current_messages)

            # 2) 只扫描带未读条数标记的可见会话。
            for session in self.GetSession():
                name = session.name
                if not name or name in WxParam.SPECIAL_SESSION_NAME:
                    continue
                full = session.full_name or ""
                if not unread_re.search(full):
                    continue
                if filter_mute and "消息免打扰" in full:
                    continue
                response = self.ChatWith(name, exact=True)
                if not response.is_success:
                    continue
                messages = self.main_wnd.get_chatbox().get_new_messages()
                if messages:
                    return _emit(name, messages)
            _time.sleep(WxParam.LISTEN_INTERVAL)
        return {}

    def GetHistoryMessage(
        self,
        n: int,
        callback=None,
        interval: float = 0.2,
        speed: int = 1,
        goback: bool = True,
        timeout: float | None = None,
    ) -> list:
        """获取当前聊天历史消息。"""
        return self.main_wnd.get_chatbox().get_history_messages(
            n=n,
            callback=callback,
            interval=interval,
            speed=speed,
            goback=goback,
            timeout=timeout,
        )

    def _ensure_listen_window(self, nickname: str):
        """返回用于监听的独立窗口 Chat 对象。

        窗口打开策略完全由 mabowx 负责：可见精确会话先单击+双击，
        搜索定位和右键菜单仅作降级路径，最终必须验证真实 HWND。
        """
        chat = self.main_wnd.get_sub_wnd(nickname, force_refresh=True)
        if chat.core.exists():
            return chat, None

        # OpenSeparateWindow 内部已经负责显示主窗口、必要时切聊天页和
        # 恢复会话列表布局，这里不要再重复切换。
        response = self.OpenSeparateWindow(nickname)
        if not response.is_success:
            return None, response

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            chat = self.main_wnd.get_sub_wnd(nickname, force_refresh=True)
            if chat.core.exists():
                # 独立窗口出现后立即拉大一次，让微信注册更多可见 UIA 控件。
                try:
                    chat.core.auto_resize()
                except Exception as exc:
                    wxlog.warning(f"独立窗口 resize 失败: {nickname}: {exc}")
                return chat, None
            time.sleep(0.25)
        return None, WxResponse.failure(f"独立窗口打开超时：{nickname}")

    @uilock
    def AddListenChat(self, nickname, callback):
        """打开独立窗口并开始监听该聊天的新消息。

        成功时返回该聊天的 Chat 对象；同时写入
        ``wx.listen[name] = (chat, callback)``。
        """
        if isinstance(nickname, (list, tuple)):
            result = {}
            failed = []
            for name in nickname:
                chat = self.AddListenChat(name, callback)
                if isinstance(chat, WxResponse) or chat is None:
                    failed.append(name)
                else:
                    result[name] = chat
            if failed:
                return WxResponse.failure("部分监听添加失败", {"failed": failed, "added": result})
            return result
        if not callable(callback):
            return WxResponse.failure("callback 必须是可调用对象")
        with self._listen_registry_lock:
            if nickname in self.listener_manager.active_names():
                existing = self.listener_manager.get_chat(nickname)
                existing_core = getattr(existing, "core", None)
                listener = self.listener_manager.listeners.get(nickname)
                if (
                    existing is not None
                    and existing_core is not None
                    and existing_core.exists()
                    and listener is not None
                    and listener.is_alive
                ):
                    self.listen[nickname] = (existing, callback)
                    listener.callback = callback
                    return existing
                # 监听线程还在宽限期内，但真实独立窗口已经消失。先移除
                # 旧任务，再按正常路径重新打开窗口；不能返回失效 Chat。
                self.listener_manager.remove(nickname)
                self.listen.pop(nickname, None)
                wxlog.warning(f"已清理失效监听对象，准备重新打开: {nickname!r}")

        started = time.monotonic()
        chat, error = self._ensure_listen_window(nickname)
        window_ready = time.monotonic()
        if error is not None:
            return error
        self._prime_listen_cache(chat)
        baseline_ready = time.monotonic()
        with self._listen_registry_lock:
            # 打开窗口/prime 期间可能有另一个调用已完成添加，二次检查。
            existing = self.listener_manager.get_chat(nickname)
            if existing is not None:
                self.listen[nickname] = (existing, callback)
                listener = self.listener_manager.listeners.get(nickname)
                if listener is not None:
                    listener.callback = callback
                return existing
            if self.listener_manager.add(nickname, chat, callback):
                self.listen[nickname] = (chat, callback)
                self._listener_started = True
                self._listener_is_listening = True
                wxlog.info(
                    f"监听任务已添加: {nickname!r} chat_exists={chat.core.exists()} "
                    f"active={self.listener_manager.active_names()} "
                    f"window_ms={(window_ready - started) * 1000:.1f} "
                    f"baseline_ms={(baseline_ready - window_ready) * 1000:.1f} "
                    f"total_ms={(time.monotonic() - started) * 1000:.1f}"
                )
                return chat
        wxlog.error(f"监听任务添加失败: {nickname!r}")
        return WxResponse.failure("监听任务添加失败")

    @uilock
    def BindListenChat(self, nickname, chat, callback):
        """将已存在的独立窗口绑定到监听任务。

        Mabobot 会在 Win32 确认窗口重建后把新的 ``Chat`` 写回监听注册表。
        旧实现只更新公开字典，存活的轮询线程仍持有旧 UIA 对象。此入口在
        不停止线程、不清空回调队列的前提下完成热重绑。
        """
        if not nickname or chat is None:
            return WxResponse.failure("监听名称和 Chat 不能为空")
        if not callable(callback):
            return WxResponse.failure("callback 必须是可调用对象")
        core = getattr(chat, "core", None)
        if core is None or not core.exists():
            return WxResponse.failure(f"监听窗口不存在：{nickname}")

        self._prime_listen_cache(chat)
        with self._listen_registry_lock:
            listener = self.listener_manager.listeners.get(nickname)
            if listener is not None and listener.is_alive:
                if not self.listener_manager.rebind(nickname, chat, callback):
                    return WxResponse.failure(f"监听任务热重绑失败：{nickname}")
            else:
                if listener is not None:
                    self.listener_manager.remove(nickname)
                if not self.listener_manager.add(nickname, chat, callback):
                    return WxResponse.failure(f"监听任务绑定失败：{nickname}")
            self.listen[nickname] = (chat, callback)
            self._listener_started = True
            self._listener_is_listening = True
        wxlog.info(f"监听窗口已绑定到轮询任务: {nickname!r}")
        return chat

    def _prime_listen_cache(self, chat, timeout: float = 4.0) -> None:
        """等待独立窗口消息列表注册出来后再 prime。

        窗口刚创建时 ``chat_message_list`` 可能还没有子项；如果此时 prime
        会得到空集合，监听线程随后会把整个窗口的旧消息都当成新消息回调。
        """
        try:
            box = chat.core.get_chatbox()
            # Chat construction already captured an existing populated window.
            # Only newly materialising/empty lists need the readiness wait.
            if box._visible_control_snapshot:
                return
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if box.get_visible_messages():
                    break
                time.sleep(0.25)
            box.prime_message_cache(
                settle_time=0.8,
                interval=0.15,
                stable_rounds=3,
            )
        except Exception as exc:
            wxlog.warning(f"初始化监听消息缓存失败: {chat.core.who}: {exc}")

    @uilock
    def RemoveListenChat(self, nickname: str, close_window: bool = True) -> WxResponse:
        """移除监听。"""
        with self._listen_registry_lock:
            # 先撤销公开注册意图，再停止线程；同步线程持同一把锁，不能用
            # Remove 前的旧快照把监听重新加回来。
            registered = self.listen.pop(nickname, None) is not None
            removed = self.listener_manager.remove(nickname)
        if removed or registered:
            if close_window:
                chat = self.main_wnd._sub_chat_cache.get(nickname)
                if chat is not None and chat.core.exists():
                    chat.Close()
                self.main_wnd._sub_chat_cache.pop(nickname, None)
            wxlog.info(f"监听任务已移除: {nickname!r} close_window={close_window}")
            return WxResponse.success()
        wxlog.warning(f"移除监听任务失败，未找到: {nickname!r}")
        return WxResponse.failure(f"未找到监听对象：{nickname}")

    def _listen_registry_sync_loop(self) -> None:
        while not self._listen_sync_stop.is_set():
            try:
                with self._listen_registry_lock:
                    # StopListening(remove=False) intentionally keeps the
                    # public registry so a later StartListening() can reuse
                    # the same detached windows.  It must not, however, let
                    # this background synchronizer immediately resurrect the
                    # tasks that were just stopped.  That race created a
                    # second poller for every chat after a reconnect.
                    entries = (
                        list(self.listen.items())
                        if self._listener_started
                        else []
                    )
                    for name, entry in entries:
                        if isinstance(entry, (tuple, list)) and entry:
                            chat, callback = entry[0], entry[1] if len(entry) > 1 else None
                        else:
                            chat, callback = entry, None
                        if chat is None or not callable(callback):
                            continue
                        listener = self.listener_manager.listeners.get(name)
                        if listener is not None and listener.is_alive:
                            if listener.chat is not chat:
                                registry_live = getattr(
                                    getattr(chat, "core", None),
                                    "exists",
                                    lambda: False,
                                )()
                                listener_live = getattr(
                                    getattr(listener.chat, "core", None),
                                    "exists",
                                    lambda: False,
                                )()
                                if registry_live:
                                    self._prime_listen_cache(chat)
                                    self.listener_manager.rebind(name, chat, callback)
                                    wxlog.info(f"监听注册表已将新窗口热重绑到任务: {name!r}")
                                elif listener_live:
                                    # ChatListener._try_rebind 已经取得新对象，而
                                    # 公开注册表仍是旧对象时，以线程内对象为准。
                                    self.listen[name] = (listener.chat, callback)
                                    wxlog.debug(f"监听注册表已同步线程重绑窗口: {name!r}")
                            if listener.callback is not callback:
                                listener.callback = callback
                            continue
                        if not getattr(getattr(chat, "core", None), "exists", lambda: False)():
                            continue
                        if listener is not None and not listener.is_alive:
                            self.listener_manager.remove(name)
                        self._prime_listen_cache(chat)
                        if self.listener_manager.add(name, chat, callback):
                            self._listener_is_listening = True
                            wxlog.info(
                                f"监听注册表同步启动: {name!r} "
                                f"active={self.listener_manager.active_names()}"
                            )
            except Exception as exc:
                wxlog.warning(f"监听注册表同步失败: {exc}")
            self._listen_sync_stop.wait(0.5)

    def StartListening(self) -> None:
        """启动监听线程，并补注册 ``wx.listen`` 中的存量条目。

        Mabobot 恢复流程会把已存在的独立窗口直接写回 ``wx.listen``，
        再调用 StartListening；因此这里不能只改布尔状态，必须把字典里
        尚未进入 listener_manager 的 ``(Chat, callback)`` 真正拉起轮询。
        """
        with self._listen_registry_lock:
            self._listener_started = True
            self._listener_is_listening = True
            for name, entry in list(self.listen.items()):
                try:
                    if isinstance(entry, (tuple, list)) and entry:
                        chat, callback = entry[0], entry[1] if len(entry) > 1 else None
                    else:
                        chat, callback = entry, None
                    if chat is None or not callable(callback):
                        continue
                    if self.listener_manager.get_chat(name) is not None:
                        continue
                    if not getattr(getattr(chat, "core", None), "exists", lambda: False)():
                        continue
                    self._prime_listen_cache(chat)
                    self.listener_manager.add(name, chat, callback)
                except Exception as exc:
                    wxlog.warning(f"补注册监听任务失败: {name}: {exc}")

    def StopListening(self, remove: bool = True) -> None:
        """停止所有监听任务。"""
        with self._listen_registry_lock:
            if remove:
                # 与单项移除相同，先撤销注册意图再等待监听线程退出。
                self.listen.clear()
            self.listener_manager.stop_all(remove=remove)
            self._listener_started = False
            self._listener_is_listening = False

    # ------------------------------------------------------------------
    # 公开兼容 API
    # ------------------------------------------------------------------

    @uilock
    def ChatInfo(self) -> dict[str, str | bool]:
        """Mabobot 兼容：返回主窗口当前聊天信息。"""
        return self.main_wnd.get_current_chat_info()

    @property
    def ChatBox(self):
        """Mabobot 兼容：返回主窗口当前聊天页 ChatBox。"""
        return self.main_wnd.get_chatbox()

    @uilock
    def GetCurrentChat(self) -> dict[str, str]:
        """返回当前聊天基础信息。"""
        info = self.ChatInfo()
        return {"chat_name": str(info.get("chat_name") or ""), "chat_type": str(info.get("chat_type") or "")}

    @uilock
    def GetMyInfo(self) -> dict[str, str]:
        """获取当前登录账号信息。"""
        if self.myinfo is None:
            self.myinfo = self.main_wnd.get_my_info()
        return dict(self.myinfo or {})

    def SendUrlCard(self, url: str, friends, message: str | None = None, timeout: int = 10) -> WxResponse:
        """兼容占位：链接卡片发送暂未实现。"""
        return WxResponse.failure("SendUrlCard 尚未实现")

    def GetAllRecentGroups(self, *args, **kwargs):
        """兼容占位：暂未实现最近群聊扫描。"""
        return []

    def GetAllFriends(self, *args, **kwargs):
        """兼容占位：暂未实现好友列表扫描。"""
        return []


class Chat:
    """独立聊天窗口对象。M2 起实现。"""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("Chat 将在 M2 会话里程碑实现")


class LoginWnd:
    """登录窗口门面。"""

    def __init__(self, debug: bool = False):
        self.wnd = WeChatLoginWnd(debug=debug)

    def open(self):
        return self.wnd.open()

    def login(self) -> bool:
        return self.wnd.login()

    def shutdown(self) -> None:
        self.wnd.shutdown()
