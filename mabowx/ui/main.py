"""微信主窗口、登录窗口管理。"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover - Linux 纯逻辑测试环境可能未安装
    psutil = None  # type: ignore[assignment]

from mabowx.core import uia
from mabowx.core.win32 import (
    get_foreground_window,
    get_monitor_info,
    get_process_path,
    get_version_by_path,
    get_window_info,
    is_window,
    is_window_visible,
    kill_process_tree,
)
from mabowx.core.window_cache import WindowCache
from mabowx.logger import wxlog
from mabowx.param import WxParam, WxResponse

from .base import BaseUIWnd, compute_auto_resize_size
from .chatbox import ChatBox
from .navigationbox import NavigationBox
from .sessionbox import SessionBox

WECHAT_PROCESS_NAMES = {"weixin.exe", "wechat.exe", "weixin", "wechat"}


def is_wechat_qt_window_class(class_name: str) -> bool:
    """Match Qt's versioned top-level class used by WeChat 4.x."""
    value = str(class_name or "")
    return value.startswith("Qt") and value.endswith("QWindowIcon")


def compute_safe_titlebar_point(
    window_rect: tuple[int, int, int, int],
    titlebar_rect: tuple[int, int, int, int] | None = None,
) -> tuple[int, int] | None:
    """计算主窗口标题栏左侧安全点击坐标。

    微信 4.x 的标题栏是客户区内的 Qt 控件，右侧依次是：置顶、最小化、
    最大化、关闭。历史实现曾使用 ``right - 260``，该点正好落在最小化
    按钮上，会把微信最小化甚至关闭。

    本函数优先使用 UIA 中 ``mmui::TitleBar`` 的真实左边界，只点击其
    左侧空白区域；找不到 TitleBar 时使用左侧保守位置，并保证远离窗口
    右边缘的按钮簇。算不出安全点就返回 None，调用方应放弃点击。
    """
    try:
        left, top, right, bottom = (int(v) for v in window_rect)
    except Exception:
        return None
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return None

    # 默认点高度保持在标题栏内（4.1.12 实测标题栏高约 64 物理像素）。
    y = top + min(28, max(8, height // 6))
    if titlebar_rect is not None:
        try:
            tb_left, tb_top, tb_right, tb_bottom = (int(v) for v in titlebar_rect)
        except Exception:
            tb_left = tb_top = tb_right = tb_bottom = 0
        if tb_left > left and tb_top < tb_bottom and tb_right > tb_left:
            # 点击 TitleBar 左边界再往左 96px 的空白区域。
            min_x = left + 40
            max_x = tb_left - 48
            preferred = tb_left - 96
            if max_x < min_x:
                return None
            x = min(max_x, max(min_x, preferred))
            y = min(max(int(tb_top) + 8, y), int(tb_bottom) - 8)
            return x, y

    # 无 TitleBar 信息时的保守策略：只用左侧 200px 以内的点，
    # 并保证距离右边缘至少 420px，给最小化/最大化/关闭按钮留足余量。
    x = left + min(200, max(60, width // 6))
    if x > right - 420:
        return None
    return x, y


def _psutil():
    if psutil is None:
        raise RuntimeError("psutil 未安装，无法执行进程操作")
    return psutil


def _iter_wechat_processes() -> list[psutil.Process]:
    result: list[psutil.Process] = []
    psutil_mod = _psutil()
    for proc in psutil_mod.process_iter(["pid", "name", "exe"]):
        try:
            name = (proc.info.get("name") or "").lower()
            exe = (proc.info.get("exe") or "").lower()
        except Exception:
            continue
        if name in WECHAT_PROCESS_NAMES or (
            exe and ("weixin" in exe or "wechat" in exe)
        ):
            result.append(proc)
    return result


def kill_wechat_processes(wait_seconds: float = 3.0) -> None:
    """结束所有微信进程。"""
    for proc in _iter_wechat_processes():
        kill_process_tree(proc.pid, wait_seconds=wait_seconds)


def _cache_validator(entry: dict[str, Any]) -> bool:
    """HWND 必须仍然有效，PID 必须存活。"""
    hwnd = int(entry.get("hwnd") or 0)
    pid = int(entry.get("pid") or 0)
    if not hwnd or not is_window(hwnd):
        return False
    if pid and not _psutil().pid_exists(pid):
        return False
    return True


class WeChatMainWnd(BaseUIWnd):
    """微信主窗口。"""

    _ui_cls_name = "mmui::MainWindow"
    _ui_name = "微信"

    def __init__(
        self,
        nickname: str | None = None,
        debug: bool = False,
        resize: bool = True,
        version: str = "微信",
        cache_file: Path | str | None = None,
    ) -> None:
        self.nickname = nickname
        self.debug = debug
        self.resize_enabled = resize
        self.version_name = version
        self.control = None
        self.HWND = None
        self._cache = WindowCache(cache_file=cache_file, validator=_cache_validator)
        self._cache_key = nickname or "default"
        self._sub_chat_cache: dict[str, object] = {}
        self._auto_resize_done = False

        wxlog.set_debug(debug)
        control = self._load_from_cache() or self._find_live()
        self.control = control
        self._sync_hwnd()
        self._cache.put(
            self._cache_key,
            hwnd=self.HWND or 0,
            pid=self.pid or 0,
            class_name=self._ui_cls_name,
            name=self._ui_name,
        )
        wxlog.info(f"微信主窗口就绪: HWND={self.HWND}, PID={self.pid}")
        if resize:
            self.auto_resize()

    # ------------------------------------------------------------------
    # 子组件
    # ------------------------------------------------------------------

    def get_session_box(self) -> SessionBox:
        box = getattr(self, "_session_box", None)
        if box is None or not box.exists():
            box = SessionBox(self)
            self._session_box = box
        return box

    def get_navigation_box(self) -> NavigationBox:
        box = getattr(self, "_navigation_box", None)
        if box is None or not box.exists():
            box = NavigationBox(self)
            self._navigation_box = box
        return box

    def get_chatbox(self) -> ChatBox:
        """获取当前聊天页 ChatBox。"""
        box = getattr(self, "_chatbox", None)
        if box is None or not box.exists():
            box = ChatBox(self)
            self._chatbox = box
        return box

    def _find_avatar_point(self) -> tuple[int, int] | None:
        """定位主窗口左上角“我的头像”点击点。

        4.1.12 部分窗口状态下头像不会注册为 ``mmui::ContactHeadView``，
        但头像固定位于左侧导航栏上方空白区。用 MainTabBar 和第一个
        “微信”Tab 的几何位置按比例计算，只点击该安全空白区。
        """
        try:
            head = uia.find_descendant(
                self.control,
                control_type="ButtonControl",
                class_name="mmui::ContactHeadView",
                timeout=0.8,
            )
            if head is not None:
                rect = head.BoundingRectangle
                return (
                    int(rect.left + (rect.right - rect.left) // 2),
                    int(rect.top + (rect.bottom - rect.top) // 2),
                )
        except Exception:
            pass

        try:
            tabbar = uia.find_descendant(
                self.control,
                control_type="ToolBarControl",
                class_name="mmui::MainTabBar",
                timeout=1.0,
            )
            if tabbar is None:
                return None
            first_tab = uia.find_descendant(
                tabbar,
                control_type="ButtonControl",
                name="微信",
                class_name="mmui::XTabBarItem",
                timeout=0.8,
            )
            if first_tab is None:
                return None
            tb = tabbar.BoundingRectangle
            ft = first_tab.BoundingRectangle
            tb_left, tb_top, tb_right = int(tb.left), int(tb.top), int(tb.right)
            ft_top = int(ft.top)
            gap = ft_top - tb_top
            if gap <= 0:
                return None
            # 实测 4.1.12：头像中心约在导航栏宽度的 70% 处，
            # 垂直方向位于导航栏顶部到第一个 Tab 之间约 78.5% 处。
            avatar_x = tb_left + int((tb_right - tb_left) * 0.7)
            avatar_y = ft_top - max(20, int(gap * 0.215))
            if avatar_x < tb_left + 20 or avatar_x >= tb_right - 8:
                return None
            if avatar_y < tb_top + 20 or avatar_y >= ft_top - 8:
                return None
            return avatar_x, avatar_y
        except Exception:
            return None

    def get_my_info(self) -> dict[str, str]:
        """点击主窗口头像，读取个人资料弹层。

        优先使用 UIA 中的 ``mmui::ContactHeadView``；4.1.12 未注册该
        控件时，使用左侧导航栏上方头像坐标回退。
        """
        result: dict[str, str] = {
            "name": self.nickname or "",
            "nickname": self.nickname or "",
            "path": self.path or "",
            "version": self.wechat_version or "",
        }
        try:
            self.show()
        except Exception:
            pass
        point = self._find_avatar_point()
        if point is None:
            wxlog.warning("未定位到我的头像，返回基础信息兜底")
            return result
        wxlog.info(f"点击我的头像: {point}")
        try:
            uia.click_screen(point[0], point[1], wait=0.5)
            time.sleep(1.2)
            from mabowx.ui.component import ProfileWnd

            profile = ProfileWnd(self, timeout=3.0)
            if profile.control is None:
                return result
            try:
                info = profile.info
            finally:
                try:
                    profile.close()
                except Exception:
                    pass
            nickname = info.get("nickname") or ""
            if nickname:
                result["name"] = nickname
                result["nickname"] = nickname
            for source_key, target_key in (
                ("wxid", "wxid"),
                ("wxid", "id"),
                ("region", "region"),
                ("signature", "signature"),
            ):
                value = info.get(source_key) or ""
                if value:
                    result[target_key] = value
        except Exception as exc:
            wxlog.warning(f"读取我的信息失败: {exc}")
        return result

    def get_current_chat_name(self) -> str | None:
        """读取当前聊天标题。"""
        controls = uia.find_controls(
            self.control,
            control_type="TextControl",
            class_name="mmui::XTextView",
            max_results=20,
        )
        for control in controls:
            try:
                automation_id = control.AutomationId or ""
                if automation_id.endswith("current_chat_name_label") and control.Name:
                    return str(control.Name)
            except Exception:
                continue
        return None

    def get_current_chat_info(self) -> dict[str, str | bool]:
        """读取当前聊天页标题和群成员数，推断 chat_type。

        返回结构兼容 Mabobot 对 ``wx.ChatInfo()`` 的用法：
        ``chat_name`` / ``chat_type``（group 或 user）。
        """
        chat_name = self.get_current_chat_name() or ""
        chat_count = ""
        if chat_name:
            for control in uia.find_controls(
                self.control,
                control_type="TextControl",
                class_name="mmui::XTextView",
                max_results=50,
            ):
                try:
                    automation_id = control.AutomationId or ""
                    if automation_id.endswith("current_chat_count_label") and control.Name:
                        chat_count = str(control.Name)
                        break
                except Exception:
                    continue
        return {
            "chat_name": chat_name,
            "chat_type": "group" if chat_count else "user",
            "exists": bool(chat_name),
        }

    def switch_chat_page(self) -> bool:
        return self.get_navigation_box().switch_to_chat_page()

    def switch_contact_page(self) -> bool:
        return self.get_navigation_box().switch_to_contact_page()

    # ------------------------------------------------------------------
    # 独立聊天子窗口
    # ------------------------------------------------------------------

    def get_sub_window_names(self) -> list[str]:
        """Win32-only detached-chat snapshot; does not traverse UIA."""
        from mabowx.core.win32 import enum_windows_by_pid

        skip_titles = {"微信", "WeChat", "Weixin", "微信发送给"}
        try:
            windows = enum_windows_by_pid(self.pid or 0)
        except Exception:
            return []
        names = {
            str(window.title or "").splitlines()[0].strip()
            for window in windows
            if window.visible
            and window.title
            and window.title not in skip_titles
            and is_wechat_qt_window_class(window.class_name)
        }
        names.discard("")
        return sorted(names)

    def _find_sub_window_controls(self):
        """通过 Win32 枚举查找独立聊天窗口。

        先用 Win32 过滤出候选窗口，再逐个构建 UIA 控件；避免直接遍历
        UIA 根节点时被转发/选择联系人等模态窗口卡住。
        """
        from mabowx.core.win32 import enum_windows_by_pid

        result = []
        skip_titles = {"微信", "Weixin", "微信发送给"}
        for win in enum_windows_by_pid(self.pid or 0):
            if not win.visible or not win.title or win.title in skip_titles:
                continue
            if not is_wechat_qt_window_class(win.class_name):
                continue
            try:
                control = uia.control_from_handle(win.hwnd)
                if control.ClassName == "mmui::ChatSingleWindow":
                    result.append(control)
            except Exception:
                continue
        return result

    def _find_sub_window_control_by_title(self, nickname: str):
        """Return the exact detached chat UIA root together with its HWND.

        WeChat 4.x can expose a newly detached window through Win32 before UIA
        reports ``mmui::ChatSingleWindow``. The strict class filter then sees
        the real window but cannot bind it before the listener timeout.  Keep
        the Win32 handle because a generic UIA root may expose handle ``0``.
        """
        from mabowx.core.win32 import enum_windows_by_pid

        expected = str(nickname or "").strip()
        if not expected:
            return None
        try:
            windows = enum_windows_by_pid(self.pid or 0)
        except Exception:
            return None
        for win in windows:
            raw_title = str(win.title or "")
            if not raw_title:
                continue
            title = raw_title.splitlines()[0].strip()
            if (
                not win.visible
                or title != expected
                or not is_wechat_qt_window_class(win.class_name)
            ):
                continue
            try:
                control = uia.control_from_handle(win.hwnd)
                if control is None or not control.Exists(0):
                    continue
                control_pid = int(getattr(control, "ProcessId", 0) or 0)
                if self.pid and control_pid and control_pid != self.pid:
                    continue
                return control, int(win.hwnd)
            except Exception:
                continue
        return None

    def get_sub_wnd(self, nickname: str, force_refresh: bool = False):
        from mabowx.api.chat import Chat

        nickname = nickname.strip()
        cached = self._sub_chat_cache.get(nickname)
        if force_refresh:
            self._sub_chat_cache.pop(nickname, None)
        elif cached is not None:
            cached_core = getattr(cached, "core", None)
            cached_hwnd = int(getattr(cached_core, "HWND", 0) or 0)
            if (
                cached_core is not None
                and cached_core.exists()
                and cached_hwnd
                and is_window(cached_hwnd)
            ):
                return cached
            # UIA 的 Exists() 在窗口刚关闭时可能短暂返回 True；Win32 HWND
            # 才是顶层窗口是否仍然存在的最终依据，失效缓存必须立即丢弃。
            self._sub_chat_cache.pop(nickname, None)
        exact_window = self._find_sub_window_control_by_title(nickname)
        if exact_window is not None:
            exact_control, exact_hwnd = exact_window
            chat = Chat(
                WeChatSubWnd(
                    exact_control,
                    self,
                    expected_who=nickname,
                    expected_hwnd=exact_hwnd,
                )
            )
            self._sub_chat_cache[nickname] = chat
            return chat
        for control in self._find_sub_window_controls():
            try:
                name = str(control.Name or "")
                first_line = name.splitlines()[0].strip() if name else ""
                if first_line == nickname or nickname in name:
                    chat = Chat(WeChatSubWnd(control, self, expected_who=nickname))
                    self._sub_chat_cache[nickname] = chat
                    return chat
            except Exception:
                continue
        return Chat(WeChatSubWnd(None, self))

    def get_all_sub_wnds(self):
        from mabowx.api.chat import Chat

        result = []
        for control in self._find_sub_window_controls():
            try:
                result.append(Chat(WeChatSubWnd(control, self)))
            except Exception:
                continue
        return result

    def open_separate_window(self, who: str) -> WxResponse:
        """通过会话右键菜单打开独立窗口。"""
        return self.get_session_box().open_separate_window(who)

    def _load_from_cache(self):
        entry = self._cache.get(self._cache_key)
        if not entry:
            return None
        try:
            if not is_window_visible(int(entry["hwnd"])):
                from mabowx.core.tray import invoke_wechat_tray

                invoke_wechat_tray()
            control = uia.control_from_handle(int(entry["hwnd"]))
            if not control.Exists(0):
                return None
            if control.ClassName != self._ui_cls_name:
                return None
            wxlog.info(f"使用缓存的主窗口 HWND={entry['hwnd']}")
            return control
        except Exception as exc:
            wxlog.debug(f"缓存不可用: {exc}")
            return None

    def _find_live(self):
        try:
            return uia.find_main_window(name=self._ui_name, class_name=self._ui_cls_name)
        except Exception:
            # Hidden Qt windows can disappear from UIA entirely after closing
            # to the tray. Ask the application to reopen before searching again.
            from mabowx.core.tray import invoke_wechat_tray

            invoke_wechat_tray()
            # 兼容英文客户端或非标准标题
            return uia.find_main_window(class_name=self._ui_cls_name, timeout=3.0)

    @property
    def path(self) -> str | None:
        return get_process_path(self.pid) if self.pid else None

    @property
    def dir(self) -> str | None:
        path = self.path
        return str(Path(path).parent) if path else None

    @property
    def wechat_version(self) -> str | None:
        path = self.path
        return get_version_by_path(path) if path else None

    def show(self) -> None:
        already_foreground = False
        try:
            already_foreground = bool(
                self.HWND and get_foreground_window() == self.HWND
            )
        except Exception:
            pass
        if not already_foreground:
            super().show()
        self.dismiss_update_window()
        # 只在实例生命周期内自动拉大一次窗口；后续每次 show 都保持用户
        # 当前窗口位置/尺寸，避免自动化反复移动窗口造成“窗口不稳定”。
        self.auto_resize()
        self._wake_ui_tree()

    def dismiss_update_window(self) -> bool:
        """Dismiss WeChat's modal update prompt before UI transactions."""
        try:
            from mabowx.ui.component import UpdateWindow

            update = UpdateWindow(self, timeout=0.2)
            return update.ignore() if update.control is not None else True
        except Exception as exc:
            wxlog.warning(f"处理微信更新弹窗失败: {exc}")
            return False

    def auto_resize(self, force: bool = False) -> None:
        """自动调整窗口尺寸；默认每个实例只执行一次。"""
        if not self.resize_enabled:
            return
        if self._auto_resize_done and not force:
            return
        super().auto_resize()
        self._auto_resize_done = True

    def _preferred_resize_width(self) -> int | None:
        try:
            monitors = get_monitor_info()
            if not monitors:
                return None
            monitor = max(monitors, key=lambda item: int(item["Height"]))
            work_width = int(monitor.get("WorkWidth") or monitor.get("Width") or 1600)
            work_height = int(monitor.get("WorkHeight") or monitor.get("Height") or 1000)
            default_width, default_height = WxParam.CHAT_WINDOW_SIZE
            width, _ = compute_auto_resize_size(default_width, default_height, work_width, work_height)
            return width
        except Exception:
            return None

    def _session_list_has_items(self) -> bool:
        try:
            box = self.get_session_box()
            if box.control is None:
                box.init()
            list_control = box.list_control
            if list_control is None or not list_control.Exists(0):
                return False
            return bool(list_control.GetChildren())
        except Exception:
            return False

    def ensure_session_list_visible(self, timeout: float = 6.0) -> bool:
        """确保主窗口当前宽度足以同时显示会话列表和聊天页。

        微信 4.x 窗口过窄时会切换单栏布局，此时 session_list 读不到任何
        子项。若当前宽度小于动态安全宽度，先放宽窗口，再等待列表出现。
        """
        if self._session_list_has_items():
            return True
        try:
            self.get_session_box().refresh()
        except Exception:
            pass
        if self._session_list_has_items():
            return True
        try:
            rect = self.control.BoundingRectangle
            current_width = int(rect.right - rect.left)
            current_height = int(rect.bottom - rect.top)
        except Exception:
            return False
        preferred = self._preferred_resize_width()
        if preferred and current_width < preferred - 20:
            wxlog.warning(
                f"主窗口宽度过窄导致会话列表不可见，正在放宽: "
                f"{current_width} -> {preferred}"
            )
            try:
                self.set_window_size(preferred, current_height)
                time.sleep(0.8)
                box = self.get_session_box()
                if box is not None:
                    box.init()
            except Exception as exc:
                wxlog.warning(f"放宽主窗口失败: {exc}")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._session_list_has_items():
                return True
            try:
                self.get_session_box().refresh()
            except Exception:
                pass
            time.sleep(0.25)
        wxlog.warning(f"会话列表仍不可见: width={current_width}")
        return False

    def _wake_ui_tree(self, attempts: int = 3) -> None:
        """确保主窗口的 QWidget UIA 子树可用。

        微信 4.1 在部分窗口状态下，主窗口 UIA 只暴露
        MMUIRenderSubWindowHW。关闭到托盘后，ShowWindow 只能显示原生
        外壳；优先调用微信托盘动作恢复 Qt 自身的可见状态。

        安全约束：点击点必须由 ``compute_safe_titlebar_point`` 计算，
        优先读取 ``mmui::TitleBar`` 左边界；右侧是最小化/最大化/关闭
        按钮，任何情况下都不允许靠近。
        """
        for attempt in range(attempts):
            try:
                children = self.control.GetChildren()
                if any(getattr(child, "ClassName", "") == "QWidget" for child in children):
                    return
                if attempt == 0 and self._restore_from_tray():
                    return
                rect = self.control.BoundingRectangle
                window_rect = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
                titlebar = uia.find_descendant(
                    self.control,
                    class_name="mmui::TitleBar",
                    timeout=0.5,
                )
                titlebar_rect = None
                if titlebar is not None:
                    tb = titlebar.BoundingRectangle
                    titlebar_rect = (int(tb.left), int(tb.top), int(tb.right), int(tb.bottom))
                point = compute_safe_titlebar_point(window_rect, titlebar_rect)
                if point is None:
                    wxlog.warning(
                        f"未找到标题栏安全点击点，放弃唤醒 UIA 树: window_rect={window_rect} "
                        f"titlebar_rect={titlebar_rect}"
                    )
                    return
                safe_x, safe_y = point
                wxlog.info(
                    f"唤醒微信主窗口 UIA 树: 点击标题栏安全区域 ({safe_x}, {safe_y}) "
                    f"window_rect={window_rect} titlebar_rect={titlebar_rect}"
                )
                uia.click_screen(safe_x, safe_y, wait=0.6)
                time.sleep(0.5)
            except Exception:
                return

    def _restore_from_tray(self) -> bool:
        from mabowx.core.tray import invoke_wechat_tray

        if not self.HWND or not invoke_wechat_tray():
            return False
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                control = uia.control_from_handle(self.HWND)
                if control.ClassName == self._ui_cls_name and any(
                    getattr(child, "ClassName", "") == "QWidget"
                    for child in control.GetChildren()
                ):
                    self.control = control
                    for name in ("_session_box", "_navigation_box", "_chatbox"):
                        self.__dict__.pop(name, None)
                    wxlog.info(f"通过微信托盘恢复主窗口控件树: HWND={self.HWND}")
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def shutdown(self) -> None:
        """结束当前主窗口进程及其全部微信相关进程。"""
        pid = self.pid
        if pid:
            kill_process_tree(pid)
        kill_wechat_processes()
        self.control = None
        self.HWND = None


class WeChatSubWnd(BaseUIWnd):
    """独立聊天窗口。"""

    _ui_cls_name = "mmui::ChatSingleWindow"

    def __init__(
        self,
        control=None,
        root: WeChatMainWnd | None = None,
        expected_who: str | None = None,
        expected_hwnd: int | None = None,
    ) -> None:
        self.control = control
        self.root = root
        self._expected_who = str(expected_who or "").strip()
        self.HWND = int(expected_hwnd or 0) or None
        self.resize_enabled = bool(getattr(root, "resize_enabled", True))
        self._auto_resize_done = False
        self._sync_hwnd()

    def exists(self, wait: float = 0.0) -> bool:
        """同时校验 UIA 对象和真实顶层 HWND。

        微信窗口关闭后的短时间内，UIA ``Exists()`` 仍可能返回 True；若
        只信 UIA，监听器会把已经消失的窗口误判为健康并发生假重绑。
        """
        if not super().exists(wait):
            return False
        self._sync_hwnd()
        if not self.HWND or not is_window(self.HWND):
            return False

        # HWND 会被 Windows 复用；仅检查 IsWindow 会把复用后的其他微信
        # 窗口误认成旧聊天窗口，继而让监听线程形成“窗口健康、回调失效”的
        # 假状态。这里用一次轻量 Win32 直查核对完整身份。
        info = get_window_info(self.HWND)
        if info is None:
            return False
        expected_pid = int(getattr(self.root, "pid", 0) or self.pid or 0)
        expected_title = self.who
        if expected_pid and info.pid != expected_pid:
            return False
        if not is_wechat_qt_window_class(info.class_name) or not info.visible:
            return False
        if expected_title and info.title != expected_title:
            return False
        return True

    def show(self) -> None:
        super().show()
        dismiss = getattr(self.root, "dismiss_update_window", None)
        if callable(dismiss):
            dismiss()

    def auto_resize(self, force: bool = False) -> None:
        """独立窗口出现后拉大，默认只执行一次。"""
        if not self.resize_enabled:
            return
        if self._auto_resize_done and not force:
            return
        super().auto_resize()
        self._auto_resize_done = True

    @property
    def who(self) -> str:
        # An exact Win32 title match is authoritative.  Generic Qt UIA roots
        # may report ``Weixin`` or another container name instead of the chat.
        if self._expected_who:
            return self._expected_who
        try:
            name = str(self.control.Name or "")
            resolved = name.splitlines()[0].strip() if name else ""
            return resolved
        except Exception:
            return ""

    def get_chatbox(self):
        """获取独立窗口聊天页 ChatBox（每个子窗口实例缓存一个）。"""
        box = getattr(self, "_chatbox", None)
        if box is None or not box.exists():
            from mabowx.ui.chatbox import ChatBox

            box = ChatBox(self)
            self._chatbox = box
        return box

    def chat_info(self) -> dict[str, object]:
        """读取当前独立窗口聊天信息。"""
        if self.control is None or not self.exists():
            return {"chat_name": self.who, "exists": False}
        result: dict[str, object] = {
            "chat_name": self.who,
            "exists": True,
            "pid": self.pid,
            "hwnd": self.HWND,
        }
        for control in uia.find_controls(
            self.control,
            control_type="TextControl",
            class_name="mmui::XTextView",
            max_results=50,
        ):
            try:
                automation_id = control.AutomationId or ""
                if automation_id.endswith("current_chat_name_label") and control.Name:
                    result["chat_name"] = str(control.Name)
                elif automation_id.endswith("current_chat_count_label") and control.Name:
                    result["chat_count"] = str(control.Name)
            except Exception:
                continue
        result["chat_type"] = "group" if result.get("chat_count") else "user"
        return result

    def close(self) -> None:
        if self.control is None or not self.exists():
            return
        try:
            self.control.SendKeys("{Esc}", waitTime=0.5)
        except Exception:
            pass


class WeChatLoginWnd(BaseUIWnd):
    """微信登录窗口。"""

    _ui_cls_name = "mmui::LoginWindow"
    _ui_name = "微信"

    def __init__(self, debug: bool = False) -> None:
        self.debug = debug
        wxlog.set_debug(debug)
        self.control = uia.find_top_level_control(self._ui_cls_name, timeout=3.0)
        if self.control is not None:
            self._sync_hwnd()

    @property
    def path(self) -> str | None:
        return self._app_path()

    @staticmethod
    def _app_path() -> str | None:
        """定位微信可执行文件。"""
        env_progfiles = os.environ.get("ProgramFiles", r"C:\Program Files")
        env_progfiles_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        candidates = [
            r"D:\Weixin\Weixin.exe",
            Path(env_progfiles) / "Tencent" / "Weixin" / "Weixin.exe",
            Path(env_progfiles_x86) / "Tencent" / "WeChat" / "WeChat.exe",
            Path(env_progfiles) / "Tencent" / "WeChat" / "WeChat.exe",
        ]
        for proc in _iter_wechat_processes():
            exe = get_process_path(proc.pid)
            if exe and Path(exe).exists():
                return exe
        for candidate in candidates:
            path = Path(candidate)
            if path.exists():
                return str(path)
        return None

    def open(self, wait: float = 5.0) -> "WeChatLoginWnd":
        """启动微信并等待登录窗口出现。"""
        if self.exists():
            return self
        exe = self._app_path()
        if not exe:
            raise RuntimeError("未找到微信安装路径")
        os.startfile(exe)  # type: ignore[attr-defined]
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            self.control = uia.find_top_level_control(self._ui_cls_name, timeout=1.0)
            if self.control is not None:
                self._sync_hwnd()
                return self
            time.sleep(0.3)
        raise RuntimeError("等待微信登录窗口超时")

    def reopen(self) -> "WeChatLoginWnd":
        self.shutdown()
        time.sleep(1.0)
        return self.open()

    def shutdown(self) -> None:
        kill_wechat_processes()
        self.control = None
        self.HWND = None

    def login(self) -> bool:
        """点击登录/进入微信按钮。

        注意：仅在登录窗口存在时调用；需要用户在微信中已完成扫码。
        """
        if not self.exists():
            return False
        for name in ("进入微信", "登录"):
            button = uia.find_descendant(
                self.control, control_type="ButtonControl", name=name, timeout=1.0
            )
            if button is not None and button.IsEnabled:
                button.Click(waitTime=1.0)
                return True
        return False
