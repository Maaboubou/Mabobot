"""会话列表与会话搜索切换。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from mabowx.core import uia
from mabowx.core.locks import uilock
from mabowx.core.win32 import post_left_click, post_double_click
from mabowx.logger import wxlog
from mabowx.param import WxParam, WxResponse

from .base import BaseUISubWnd, BaseUIWnd
from .component import Menu


def _first_line(text: str | None) -> str:
    if not text:
        return ""
    return text.splitlines()[0].strip()


@dataclass
class SessionInfo:
    name: str
    full_name: str
    automation_id: str
    control: Any


class SessionElement:
    """会话列表中的一项。"""

    def __init__(self, control, session_box: "SessionBox") -> None:
        self.control = control
        self.session_box = session_box

    def __repr__(self) -> str:
        return f"<mabowx SessionElement({self.name!r})>"

    @property
    def name(self) -> str:
        try:
            return _first_line(self.control.Name)
        except Exception:
            return ""

    @property
    def full_name(self) -> str:
        try:
            return str(self.control.Name)
        except Exception:
            return ""

    @property
    def automation_id(self) -> str:
        try:
            return str(self.control.AutomationId)
        except Exception:
            return ""

    def info(self) -> SessionInfo:
        return SessionInfo(self.name, self.full_name, self.automation_id, self.control)

    @uilock
    def roll_into_view(self) -> bool:
        try:
            pattern = self.control.GetScrollItemPattern()
            return bool(pattern.ScrollIntoView())
        except Exception:
            return False

    @uilock
    def click(self) -> None:
        self.control.Click(simulateMove=False, waitTime=0.6)

    @uilock
    def double_click(self) -> None:
        self.control.DoubleClick(simulateMove=False, waitTime=0.6)

    @uilock
    def right_click(self) -> None:
        self.control.RightClick(simulateMove=False, waitTime=0.2)

    @uilock
    def select_option(self, option: str) -> WxResponse:
        """右键本会话并选择菜单项。

        会话位置已经稳定后才调用；右键菜单通常在几百毫秒内出现，因此
        这里用短间隔轮询，菜单一出现立刻选择，不再固定等待。
        """
        self.right_click()
        deadline = time.monotonic() + WxParam.SEARCH_CHAT_TIMEOUT
        last_response = WxResponse.failure("右键菜单未出现")
        while time.monotonic() < deadline:
            menu = Menu(self.session_box, timeout=0.1)
            if menu.exists():
                wxlog.log_control("会话右键菜单已出现", menu.control, level="debug")
                response = menu.select(option)
                if response.is_success:
                    wxlog.info(f"会话右键菜单选择成功: {option!r}")
                    return response
                last_response = response
            time.sleep(0.04)
        wxlog.warning(f"会话右键菜单选择失败: {option!r} last={last_response.to_dict()}")
        return last_response


class SearchResultElement:
    """搜索弹层中的一项结果。"""

    def __init__(self, control) -> None:
        self.control = control

    def __repr__(self) -> str:
        return f"<mabowx SearchResultElement({self.name!r})>"

    @property
    def name(self) -> str:
        try:
            return str(self.control.Name)
        except Exception:
            return ""

    @property
    def class_name(self) -> str:
        try:
            return str(self.control.ClassName)
        except Exception:
            return ""

    @property
    def automation_id(self) -> str:
        try:
            return str(self.control.AutomationId)
        except Exception:
            return ""

    def get_all_text(self) -> list[str]:
        texts: list[str] = []
        for child in uia.iter_descendants(self.control, max_nodes=50):
            try:
                if child.ControlTypeName in ("TextControl", "ListItemControl", "ButtonControl"):
                    name = child.Name
                    if name:
                        texts.append(str(name))
            except Exception:
                continue
        return texts

    @uilock
    def click(self) -> None:
        try:
            rect = self.control.BoundingRectangle
            if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
                raise RuntimeError("搜索结果控件不可见")
        except Exception:
            raise RuntimeError("搜索结果控件不可见")
        self.control.Click(simulateMove=False, waitTime=0.4)


class SessionBox(BaseUISubWnd):
    """会话列表容器。"""

    _ui_cls_name = "mmui::ChatSessionList"

    def __init__(self, root: BaseUIWnd) -> None:
        self.root = root
        self.parent = None
        self.control = None
        self._list = None
        self._search_edit = None
        self.init()

    def init(self) -> None:
        if self.root is None or not self.root.exists():
            return
        self.control = uia.find_descendant(
            self.root.control,
            class_name=self._ui_cls_name,
            timeout=2.0,
        )
        if self.control is not None:
            self._list = uia.find_descendant(
                self.control,
                control_type="ListControl",
                automation_id="session_list",
                timeout=1.5,
            )

    def refresh(self) -> None:
        """Discard stale UIA handles and rediscover the current session list."""
        self.control = None
        self._list = None
        self._search_edit = None
        self.init()

    @property
    def list_control(self):
        if self._list is None or not self._list.Exists(0):
            self.init()
        return self._list

    def _search_field(self):
        if self._search_edit is None or not self._search_edit.Exists(0):
            group = uia.find_descendant(
                self.root.control,
                class_name="mmui::XSearchField",
                timeout=2.0,
            )
            if group is not None:
                self._search_edit = uia.find_descendant(
                    group,
                    control_type="EditControl",
                    class_name="mmui::XValidatorTextEdit",
                    timeout=1.0,
                )
        return self._search_edit

    # ------------------------------------------------------------------
    # 会话列表
    # ------------------------------------------------------------------

    @uilock
    def get_session(self) -> list[SessionElement]:
        """获取当前可见会话列表。"""
        if self.list_control is None or not self.list_control.Exists(0):
            return []
        try:
            children = self.list_control.GetChildren()
        except Exception:
            return []
        return [SessionElement(child, self) for child in children]

    @uilock
    def go_top(self) -> bool:
        """回到会话列表顶部。

        不能点击 List 中心：会话列表中心通常正好落在某个会话上，
        单击会打开该会话，甚至进入“服务号/公众号”等折叠目录，导致后续
        查找完全跑偏。优先使用 SetFocus + Home，失败则向上滚轮。
        """
        if self.list_control is None or not self.list_control.Exists(0):
            return False
        try:
            self.list_control.SetFocus()
            self.list_control.SendKeys("{Home}", waitTime=0.5)
            return True
        except Exception:
            pass
        try:
            self.list_control.WheelUp(wheelTimes=40, waitTime=0.1)
            return True
        except Exception:
            return False

    @uilock
    def roll_up(self, wheel_times: int = 3) -> None:
        if self.list_control is not None and self.list_control.Exists(0):
            self.list_control.WheelUp(wheelTimes=wheel_times, waitTime=0.2)

    @uilock
    def roll_down(self, wheel_times: int = 3) -> None:
        if self.list_control is not None and self.list_control.Exists(0):
            self.list_control.WheelDown(wheelTimes=wheel_times, waitTime=0.2)

    def _find_session_in_list(self, who: str, exact: bool = True) -> SessionElement | None:
        expected_auto_id = f"session_item_{who}"
        for _ in range(40):
            for item in self.get_session():
                name = item.name
                auto_id = item.automation_id
                if exact and (name == who or auto_id == expected_auto_id):
                    return item
                if not exact and (who in name or who in auto_id):
                    return item
            self.roll_down()
            time.sleep(0.25)
        wxlog.warning(f"在会话列表中未找到: {who!r} exact={exact}")
        return None

    def _find_visible_session(self, who: str, exact: bool = True) -> SessionElement | None:
        """在当前可见会话列表里查找目标；优先匹配 AutomationId。"""
        expected_auto_id = f"session_item_{who}"
        for item in self.get_session():
            name = item.name
            auto_id = item.automation_id
            if auto_id == expected_auto_id:
                return item
            if exact and name == who:
                return item
            if not exact and who in name:
                return item
        return None

    @staticmethod
    def _control_rect(control) -> tuple[int, int, int, int] | None:
        try:
            rect = control.BoundingRectangle
            return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        except Exception:
            return None

    def _wait_visible_session(
        self,
        who: str,
        timeout: float = 15.0,
        stable_readings: int = 2,
        poll_interval: float = 0.15,
    ) -> SessionElement | None:
        """等待会话列表把目标显示出来，并且位置已经稳定。

        搜索并点击精确结果后，微信会把目标会话滑到列表顶部；这个滑动
        是动画过程。如果刚看到目标就右键，点击坐标可能落在旧位置上，
        导致第一次打开独立窗口失败。因此必须连续多次读到相同矩形后才
        返回。
        """
        deadline = time.monotonic() + timeout
        last_rect: tuple[int, int, int, int] | None = None
        stable_count = 0
        last_count = -1
        while time.monotonic() < deadline:
            # 搜索弹层必须已经关闭，否则右键可能点到弹层而不是会话项。
            if self._search_popover(timeout=0.2):
                self._close_search_popover()

            sessions = self.get_session()
            if len(sessions) != last_count:
                last_count = len(sessions)
                wxlog.debug(f"等待会话列表稳定 {who!r}: visible_count={last_count}")

            item = self._find_visible_session(who, exact=True)
            if item is None:
                stable_count = 0
                last_rect = None
                time.sleep(poll_interval)
                continue

            rect = self._control_rect(item.control)
            if rect is None:
                stable_count = 0
                last_rect = None
            elif rect == last_rect:
                stable_count += 1
            else:
                stable_count = 1
                last_rect = rect

            if stable_count >= stable_readings:
                wxlog.info(
                    f"会话列表已定位并稳定: {who!r} rect={rect} "
                    f"stable={stable_count}/{stable_readings}"
                )
                return item
            time.sleep(poll_interval)

        wxlog.warning(f"等待会话列表稳定超时: {who!r} stable={stable_count} rect={last_rect}")
        return None

    def _clear_search_state(self) -> None:
        """关闭搜索弹层并清空搜索框，避免影响后续会话列表点击。"""
        self._close_search_popover()
        try:
            edit = self._search_field()
            if edit is not None and edit.Exists(0):
                edit.Click(simulateMove=False, waitTime=0.2)
                edit.SendKeys("{Ctrl}a", waitTime=0.15)
                edit.SendKeys("{BACK}", waitTime=0.2)
        except Exception:
            pass
        try:
            if self.root is not None and self.root.exists():
                self.root.control.SendKeys("{Esc}", waitTime=0.2)
        except Exception:
            pass

    def _switch_chat_by_session(self, who: str, exact: bool = True) -> WxResponse:
        self._clear_search_state()
        self.go_top()
        item = self._find_session_in_list(who, exact=exact)
        if item is None:
            return WxResponse.failure(f"未找到会话：{who}")
        item.click()
        if self._wait_chat_ready(timeout=3.0, expected=who, exact=exact):
            return WxResponse.success()
        return WxResponse.failure("会话点击后聊天页未就绪")

    # ------------------------------------------------------------------
    # 搜索切换
    # ------------------------------------------------------------------

    def _ui_search_content(self, keyword: str) -> bool:
        edit = self._search_field()
        if edit is None:
            return False
        edit.Click(simulateMove=False, waitTime=0.1)
        edit.SendKeys("{Ctrl}a", waitTime=0.05)
        edit.SendKeys(keyword, interval=0.015, waitTime=0.25)
        return True

    def _search_popover(self, timeout: float = 2.0):
        """搜索弹层既可能挂在主窗口下，也可能是顶层窗口，两种都找。"""
        result = []
        if self.root is not None and self.root.exists():
            result = uia.find_controls(
                self.root.control,
                class_name="mmui::SearchContentPopover",
                max_results=5,
            )
        if not result:
            result = uia.find_top_level_controls(
                class_name="mmui::SearchContentPopover",
                name="Weixin",
                pid=self.root.pid,
            )
        return result

    def _search_result_items(self, popover) -> list[SearchResultElement]:
        if popover is None or not popover.Exists(0):
            return []
        list_control = uia.find_descendant(
            popover,
            control_type="ListControl",
            automation_id="search_list",
            timeout=0.35,
        )
        if list_control is None:
            return []
        try:
            children = list_control.GetChildren()
        except Exception:
            return []
        return [SearchResultElement(child) for child in children]

    def search(self, keyword: str) -> list[SearchResultElement]:
        """搜索会话/联系人并返回可见结果。"""
        if not self._ui_search_content(keyword):
            return []
        deadline = time.monotonic() + WxParam.SEARCH_CHAT_TIMEOUT
        while time.monotonic() < deadline:
            popovers = self._search_popover(timeout=0.4)
            popover = popovers[0] if popovers else None
            items = self._search_result_items(popover)
            if items:
                return items
            time.sleep(0.1)
        return []

    def _click_search_result(self, who: str, exact: bool) -> WxResponse:
        items = self.search(who)
        wxlog.debug(f"搜索结果数量={len(items)}: " + " | ".join(
            f"{item.class_name}:{item.name!r}:{item.automation_id!r}" for item in items[:12]
        ))
        if not items:
            return WxResponse.failure("搜索无结果")

        def _match(item: SearchResultElement) -> bool:
            # 聊天对象结果的实际样式是 mmui::SearchContentCellView；
            # 搜索关键词/网络建议等样式不同，绝不能当作会话点击。
            if item.class_name != "mmui::SearchContentCellView":
                return False
            name = item.name.replace("\n", " ").strip()
            if not name:
                return False
            if name in ("搜索网络结果", "群聊", "聊天记录"):
                return False
            if exact:
                return name == who or item.automation_id == f"search_item_{who}"
            return who in name or who in item.automation_id

        target = next((item for item in items if _match(item)), None)
        if target is None:
            return WxResponse.failure(f"未找到联系人/群结果：{who}")
        wxlog.info(
            f"点击第一个匹配搜索结果: {target.name!r} class={target.class_name!r} "
            f"auto_id={target.automation_id!r}"
        )
        target.click()
        return WxResponse.success()

    def _close_search_popover(self) -> None:
        for popover in self._search_popover(timeout=0.3):
            try:
                if popover.Exists(0):
                    popover.SendKeys("{Esc}", waitTime=0.15)
            except Exception:
                pass

    def _switch_chat_by_search(self, who: str, exact: bool = True) -> WxResponse:
        response = self._click_search_result(who, exact=exact)
        if response.is_success:
            time.sleep(0.15)
            self._close_search_popover()
        return response

    # ------------------------------------------------------------------
    # 对外切换
    # ------------------------------------------------------------------

    @uilock
    def switch_chat(
        self,
        who: str,
        exact: bool = True,
        force: bool = False,
        force_wait: float | int = 0.5,
    ) -> WxResponse:
        """切换到指定聊天。"""
        if not who:
            return WxResponse.failure("未指定聊天对象")
        wxlog.info(f"切换聊天窗口: {who!r} exact={exact}")
        show = getattr(self.root, "show", None)
        if show is not None:
            show()
            time.sleep(0.4)
        switch_page = getattr(self.root, "switch_chat_page", None)
        if switch_page is not None:
            switch_page()
            time.sleep(0.5)
        ensure_layout = getattr(self.root, "ensure_session_list_visible", None)
        if ensure_layout is not None:
            ensure_layout()

        response = self._switch_chat_by_search(who, exact=exact)
        if response.is_success:
            if self._wait_chat_ready(timeout=3.0, expected=who, exact=exact):
                return response
        self._clear_search_state()

        if force:
            # force 模式：不判断结果，输入关键词后等待片刻直接回车。
            if self._ui_search_content(who):
                edit = self._search_field()
                time.sleep(float(force_wait))
                edit.SendKeys("{Enter}", waitTime=0.8)
                self._close_search_popover()
                return WxResponse.success(message="force 模式已回车，不保证目标正确")
            return WxResponse.failure("force 模式输入失败")

        return self._switch_chat_by_session(who, exact=exact)

    def _wait_chat_ready(
        self,
        timeout: float = 3.0,
        expected: str | None = None,
        exact: bool = True,
    ) -> bool:
        """等待聊天页完成切换，返回是否满足预期标题。

        优先等待标题出现；只有 root 不支持读取标题时才用 ChatMessagePage 兜底。
        """
        getter = getattr(self.root, "get_current_chat_name", None)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if getter is not None:
                    title = getter()
                    if title and expected is not None:
                        if (exact and title == expected) or (not exact and expected in title):
                            return True
                    elif title:
                        return True
                elif uia.find_descendant(
                    self.root.control,
                    class_name="mmui::ChatMessagePage",
                    timeout=0.4,
                ) is not None:
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

    def _separate_window_state(self, who: str) -> bool | None:
        """Return whether an exact detached HWND exists, or None if unsupported."""
        getter = getattr(self.root, "get_sub_window_names", None)
        if not callable(getter):
            return None
        try:
            return who in set(getter())
        except Exception:
            return False

    def _wait_separate_window(self, who: str, timeout: float = 4.0) -> bool | None:
        state = self._separate_window_state(who)
        if state is None or state:
            return state
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.15)
            if self._separate_window_state(who):
                return True
        return False

    def _click_detach_row(self, item: SessionElement, *, double: bool = False) -> None:
        """Deliver to the verified main HWND without global mouse delays."""
        hwnd = int(getattr(self.root, "HWND", 0) or 0)
        control = getattr(item, "control", None)
        if hwnd and control is not None:
            top = control.GetTopLevelControl()
            if int(getattr(top, "NativeWindowHandle", 0) or 0) != hwnd:
                raise RuntimeError("会话行不属于当前微信主窗口")
            rect = control.BoundingRectangle
            click = post_double_click if double else post_left_click
            if not click(hwnd, (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2):
                raise RuntimeError("会话行定向点击失败")
        elif double:
            item.double_click()
        else:
            item.click()

    def _open_by_double_click(self, item: SessionElement, who: str) -> bool | None:
        """Re-resolve the exact row after selection before sending the double click."""
        try:
            self._click_detach_row(item)
            # Selecting a conversation can move its virtualized row. Never
            # double-click the old row merely because its RuntimeId still exists.
            item = self._find_visible_session(who, exact=True)
            if item is None:
                return False
            self._click_detach_row(item)
            self._click_detach_row(item, double=True)
        except Exception as exc:
            wxlog.warning(f"会话双击打开独立窗口失败: {who!r}: {exc}")
            return False
        opened = self._wait_separate_window(who)
        if opened:
            wxlog.info(f"会话双击已创建独立窗口: {who!r}")
        return opened

    @uilock
    def open_separate_window(self, who: str) -> WxResponse:
        """用精确会话行打开独立窗口，并用 Win32 标题验证结果。

        优先使用经过验证的操作顺序：

        1. 先按 ``session_item_<who>`` 找当前可见精确会话；
        2. 对该行先单击、再双击；
        3. 找不到才精确搜索定位后重试；
        4. 双击未生效时再用右键菜单兼容路径。

        任何路径都必须真正观察到同名顶层 HWND 后才报成功。
        """
        if self._separate_window_state(who):
            return WxResponse.success(message=f"独立窗口已存在：{who}")
        # A visible exact row can be detached with targeted HWND messages even
        # when the main window is behind another chat. Keep foreground/layout
        # recovery for the case where the UIA session list is unavailable.
        self._close_search_popover()
        item = self._find_visible_session(who, exact=True)
        if item is not None:
            opened = self._open_by_double_click(item, who)
            if opened is not False:
                return WxResponse.success()

        show = getattr(self.root, "show", None)
        if show is not None:
            show()
            time.sleep(0.2)
        has_items = getattr(self.root, "_session_list_has_items", None)
        if has_items is None or not has_items():
            switch_page = getattr(self.root, "switch_chat_page", None)
            if switch_page is not None:
                switch_page()
                time.sleep(0.2)
        ensure_layout = getattr(self.root, "ensure_session_list_visible", None)
        if ensure_layout is not None:
            ensure_layout()

        existing = self._separate_window_state(who)
        if existing:
            return WxResponse.success(message=f"独立窗口已存在：{who}")

        # Do not search when the exact session row is already visible. Search
        # overlays can swallow the following double-click even after UIA says
        # they are closed; the old library intentionally used this fast path.
        self._close_search_popover()
        item = self._find_visible_session(who, exact=True)
        if item is not None:
            opened = self._open_by_double_click(item, who)
            if opened is not False:
                return WxResponse.success()

        response = self._switch_chat_by_search(who, exact=True)
        if response.is_success:
            item = self._wait_visible_session(who, timeout=3.0)
        else:
            # 搜索路径失败时，只尝试当前已经可见/选中的会话，不滚动列表。
            item = self._find_visible_session(who, exact=True)
        if item is None:
            return WxResponse.failure(f"未找到会话：{who}")

        opened = self._open_by_double_click(item, who)
        if opened is not False:
            return WxResponse.success()

        # Re-resolve the row because double-click may rebuild the session list.
        item = self._find_visible_session(who, exact=True) or item
        wxlog.info(f"右键会话列表项打开独立窗口: {who!r} auto_id={item.automation_id!r}")
        for option in ("独立窗口显示", "在独立窗口打开", "在独立窗口中打开"):
            response = item.select_option(option)
            if response.is_success:
                opened = self._wait_separate_window(who)
                if opened is not False:
                    return response
                return WxResponse.failure(f"菜单已点击但独立窗口未出现：{who}")
        return WxResponse.failure("未找到独立窗口菜单项")
