"""通用 UI 组件：菜单、对话框。

V1 先实现 K01/K02/K03；其余组件在后续里程碑加入。
"""

from __future__ import annotations

import time
from typing import Any

from mabowx.core import uia
from mabowx.core.clipboard import get_text, set_text
from mabowx.core.locks import uilock
from mabowx.core.win32 import (
    enum_windows_by_pid,
    force_foreground,
    get_foreground_window,
    get_process_name,
    get_window_owner,
    is_window,
    post_close_message,
    post_left_click,
)
from mabowx.logger import wxlog
from mabowx.param import WxParam, WxResponse

from .base import BaseUISubWnd, BaseUIWnd


class UpdateWindow(BaseUISubWnd):
    """WeChat's process-wide update modal.

    The dialog can make ordinary UIA clicks return successfully while WeChat
    silently ignores the requested chat operation.  Dismiss it by verified
    screen coordinates and confirm the dedicated HWND is gone.
    """

    _ui_cls_name = "mmui::UpdateWindow"

    def __init__(self, parent: BaseUIWnd, timeout: float = 0.2) -> None:
        self.root = parent
        self.parent = parent
        self.control = None
        self.HWND = None
        candidates = uia.find_top_level_controls(
            class_name=self._ui_cls_name,
            pid=getattr(parent, "pid", None),
            max_results=3,
        )
        if candidates:
            self.control = candidates[0]
            self._sync_hwnd()

    def ignore(self, timeout: float = 2.0) -> bool:
        """Choose "ignore this update" and verify the modal really closed."""
        if self.control is None or not self.exists():
            return True
        button = uia.find_descendant(
            self.control,
            control_type="ButtonControl",
            name="忽略本次更新",
            timeout=min(max(timeout, 0.1), 1.0),
        )
        if button is None:
            return False
        try:
            rect = button.BoundingRectangle
            left, top, right, bottom = (
                int(rect.left),
                int(rect.top),
                int(rect.right),
                int(rect.bottom),
            )
            if right <= left or bottom <= top:
                return False
            uia.click_screen((left + right) // 2, (top + bottom) // 2, wait=0.3)
        except Exception as exc:
            wxlog.warning(f"微信更新弹窗关闭失败: {exc}")
            return False

        deadline = time.monotonic() + max(timeout, 0.1)
        while time.monotonic() < deadline:
            if self.HWND and not is_window(self.HWND):
                wxlog.info("已忽略微信本次更新提示")
                return True
            if not self.exists():
                wxlog.info("已忽略微信本次更新提示")
                return True
            time.sleep(0.05)
        wxlog.warning("已点击“忽略本次更新”，但弹窗仍存在")
        return False


class Menu(BaseUISubWnd):
    """微信右键/弹出菜单。"""

    _ui_cls_name = "mmui::XMenu"
    # 微信 4.1.12 的同一个菜单在 UIA 层是 mmui::XMenu，但对应的真实
    # 顶层 Win32 窗口类是 Qt51514QWindowToolSaveBits。
    _native_window_classes = frozenset(
        {_ui_cls_name, "Qt51514QWindowToolSaveBits"}
    )

    def __init__(
        self,
        parent: BaseUIWnd,
        timeout: float = 2.0,
        anchor: tuple[int, int] | None = None,
        baseline_hwnds: set[int] | None = None,
        require_new: bool = False,
        expected_owner_hwnd: int | None = None,
    ) -> None:
        self.root = parent
        self.parent = parent
        self.anchor = anchor
        self.baseline_hwnds = {
            int(hwnd or 0) for hwnd in (baseline_hwnds or set()) if int(hwnd or 0)
        }
        self.require_new = bool(require_new)
        self.expected_owner_hwnd = int(expected_owner_hwnd or 0)
        self.control = self._find(timeout)
        try:
            self._captured_hwnd = int(
                getattr(self.control, "NativeWindowHandle", 0) or 0
            )
            self._captured_pid = int(getattr(self.control, "ProcessId", 0) or 0)
        except Exception:
            self._captured_hwnd = 0
            self._captured_pid = 0

    def _candidate_identity(self, control) -> tuple[bool, int]:
        """Validate a menu candidate and return its anchor distance."""

        try:
            if (
                control is None
                or str(getattr(control, "ClassName", "") or "") != self._ui_cls_name
                or not control.Exists(0)
                or bool(getattr(control, "IsOffscreen", False))
            ):
                return False, 2**63
            hwnd = int(getattr(control, "NativeWindowHandle", 0) or 0)
            parent_pid = int(getattr(self.parent, "pid", 0) or 0)
            control_pid = int(getattr(control, "ProcessId", 0) or 0)
            if parent_pid and control_pid != parent_pid:
                return False, 2**63
            if self.require_new and (not hwnd or hwnd in self.baseline_hwnds):
                return False, 2**63
            if (
                self.expected_owner_hwnd
                and get_window_owner(hwnd) != self.expected_owner_hwnd
            ):
                return False, 2**63
            rect = control.BoundingRectangle
            if int(rect.right - rect.left) <= 0 or int(rect.bottom - rect.top) <= 0:
                return False, 2**63
            distance = 0
            if self.anchor is not None:
                anchor_x, anchor_y = self.anchor
                dx = max(int(rect.left) - anchor_x, 0, anchor_x - int(rect.right))
                dy = max(int(rect.top) - anchor_y, 0, anchor_y - int(rect.bottom))
                distance = dx * dx + dy * dy
                if self.require_new and distance > 96**2:
                    return False, distance
            return True, distance
        except Exception:
            return False, 2**63

    def _captured_identity_is_current(self) -> bool:
        if not self.require_new:
            return self.exists()
        valid, _ = self._candidate_identity(self.control)
        if not valid:
            return False
        try:
            hwnd = int(getattr(self.control, "NativeWindowHandle", 0) or 0)
            pid = int(getattr(self.control, "ProcessId", 0) or 0)
            top = self.control.GetTopLevelControl()
            top_hwnd = int(getattr(top, "NativeWindowHandle", 0) or 0)
        except Exception:
            return False
        foreground_hwnd = int(get_foreground_window() or 0)
        return bool(
            hwnd
            and hwnd == self._captured_hwnd
            and top_hwnd == hwnd
            and (not self._captured_pid or pid == self._captured_pid)
            and (
                not self.expected_owner_hwnd
                or get_window_owner(hwnd) == self.expected_owner_hwnd
            )
            # Qt popup menus keep their owner (chat or media preview) as the
            # Win32 foreground window. Requiring the XMenu itself to be
            # foreground rejects a real, freshly opened menu on WeChat 4.x.
            and foreground_hwnd
            in {hwnd, int(self.expected_owner_hwnd or 0)}
        )

    def _search_roots(self) -> list[Any]:
        """菜单可能挂在独立窗口或主窗口下，两个范围都搜索。"""
        parent = getattr(self, "parent", None)
        roots = []
        if parent is not None:
            roots.append(parent)
            if getattr(parent, "root", None) is not None and parent.root is not parent:
                roots.append(parent.root)
        # 如果 parent 是 ChatBox，其 root 可能是 WeChatSubWnd，再向上找主窗口。
        if parent is not None and getattr(parent, "root", None) is not None:
            sub_root = parent.root
            main_root = getattr(sub_root, "root", None)
            if main_root is not None and main_root not in roots:
                roots.append(main_root)
        return [root for root in roots if root is not None and root.exists()]

    def _find(self, timeout: float):
        # 微信 4.x 的 XMenu 通常是独立顶层窗口。先枚举桌面直系子项，
        # 避免为了一个很小的菜单反复扫描整棵聊天窗口 UIA 树。
        parent_pid = getattr(self.parent, "pid", None)
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            foreground_hwnd = get_foreground_window()
            if foreground_hwnd:
                try:
                    foreground = uia.control_from_handle(foreground_hwnd)
                    valid, _ = self._candidate_identity(foreground)
                    if valid:
                        return foreground
                except Exception:
                    pass
            try:
                top_level = uia.find_top_level_controls(
                    self._ui_cls_name,
                    pid=int(parent_pid) if parent_pid else None,
                    max_results=10,
                )
            except Exception:
                top_level = []
            # Some UIAutomation builds omit Qt ToolSaveBits popups from the root
            # control's direct children. Win32 still exposes the exact top-level
            # HWND, so bind those same-process popup handles back into UIA and run
            # them through the identical class/PID/owner/anchor validation below.
            if parent_pid:
                try:
                    for window in enum_windows_by_pid(int(parent_pid)):
                        if (
                            window.visible
                            and window.class_name in self._native_window_classes
                        ):
                            try:
                                top_level.append(uia.control_from_handle(window.hwnd))
                            except Exception:
                                continue
                except Exception:
                    pass
            candidates = []
            seen_hwnds: set[int] = set()
            for control in top_level:
                try:
                    valid, distance = self._candidate_identity(control)
                    if valid:
                        hwnd = int(getattr(control, "NativeWindowHandle", 0) or 0)
                        if hwnd in seen_hwnds:
                            continue
                        seen_hwnds.add(hwnd)
                        candidates.append(
                            (0 if hwnd and hwnd == foreground_hwnd else 1, distance, control)
                        )
                except Exception:
                    continue
            if candidates:
                candidates.sort(key=lambda item: (item[0], item[1]))
                return candidates[0][2]

            # 身份严格模式原来只枚举一次，所以 select_option 只能在右键后
            # 固定睡眠 0.8 秒。改为在 timeout 内短轮询：菜单一出现就继续，
            # 慢机仍保留完整等待上限。
            if not self.require_new:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.02, remaining))

        for root in self._search_roots():
            control = uia.find_descendant(
                root.control,
                class_name=self._ui_cls_name,
                timeout=min(timeout, 1.0),
            )
            if control is not None:
                return control
        return uia.find_top_level_control(self._ui_cls_name, timeout=timeout)

    @property
    def option_controls(self) -> list[Any]:
        # _find() 已经拿到了精确菜单控件。菜单项应优先只在这棵很小的
        # 子树中查找；旧实现忽略 self.control，转而遍历聊天窗口、独立
        # 窗口和主窗口，真机上一轮可能耗费数秒甚至十几秒。
        if self.control is not None:
            try:
                direct = uia.find_controls(
                    self.control,
                    control_type="MenuItemControl",
                    max_results=50,
                    max_nodes=200,
                )
            except Exception:
                direct = []
            if direct:
                return direct

        # 兼容少数 UIA 树把菜单项挂回宿主窗口的微信版本。
        result: list[Any] = []
        for root in self._search_roots():
            result.extend(
                uia.find_controls(
                    root.control,
                    control_type="MenuItemControl",
                    class_name="mmui::XMenuView",
                    max_results=50,
                    max_nodes=1000,
                )
            )
        return result

    @property
    def option_names(self) -> list[str]:
        return [str(item.Name) for item in self.option_controls]

    @uilock
    def select(self, item: str) -> WxResponse:
        """选择指定菜单项。"""
        if not self._captured_identity_is_current():
            return WxResponse.failure("菜单窗口不存在")
        options = self.option_controls
        for option in options:
            if str(option.Name or "").strip() == item:
                try:
                    wxlog.debug(f"点击微信菜单项: {item!r}")
                    if self.require_new and self._captured_hwnd:
                        rect = option.BoundingRectangle
                        point = (
                            (int(rect.left) + int(rect.right)) // 2,
                            (int(rect.top) + int(rect.bottom)) // 2,
                        )
                        # The stock uiautomation mouse click can close a Qt
                        # ToolSaveBits menu without firing its action. Deliver
                        # the click synchronously to the already owner/PID/HWND
                        # verified popup instead.
                        if not post_left_click(
                            self._captured_hwnd,
                            point[0],
                            point[1],
                        ):
                            return WxResponse.error("无法向已校验菜单投递点击")
                    else:
                        option.Click(waitTime=0.3)
                    return WxResponse.success()
                except Exception as exc:
                    return WxResponse.error(f"点击菜单项失败: {exc}")
        available = [
            str(getattr(option, "Name", "") or "").strip()
            for option in options
            if str(getattr(option, "Name", "") or "").strip()
        ]
        wxlog.debug(f"微信菜单未找到选项 {item!r}: available={available!r}")
        return WxResponse.failure(
            f"未找到选项：{item}",
            data={"available_options": available},
        )

    def close(self) -> bool:
        """只定向关闭已校验的菜单 HWND，绝不向宿主聊天窗口发送 Esc。

        微信 4.x 的 XMenu UIA 对象有时复用聊天顶层 HWND；菜单已经自行
        消失后，向这个陈旧对象 SendKeys("{Esc}") 会把独立聊天窗口关闭。
        如果 Win32 类名不能确认是独立 XMenu，就保留它给下一次右键自然
        替换，不能冒险发送键盘或 WM_CLOSE。
        """
        if self.control is None:
            return False
        try:
            hwnd = int(getattr(self.control, "NativeWindowHandle", 0) or 0)
            control_class = str(getattr(self.control, "ClassName", "") or "")
            control_pid = int(getattr(self.control, "ProcessId", 0) or 0)
            top_control = self.control.GetTopLevelControl()
            top_hwnd = int(getattr(top_control, "NativeWindowHandle", 0) or 0)
        except Exception:
            return False

        # UIA 类、顶层 HWND、微信 PID 和宿主 HWND 四重校验。这样即使
        # Qt 的 ToolSaveBits 类也被其他浮层复用，也不会误关聊天或主窗。
        parent_pid = int(getattr(self.parent, "pid", 0) or 0)
        owner_hwnds = {
            int(getattr(owner, "HWND", 0) or 0)
            for owner in (self.parent, getattr(self.parent, "root", None))
            if owner is not None
        }
        if (
            control_class != self._ui_cls_name
            or not hwnd
            or top_hwnd != hwnd
            or (parent_pid and control_pid != parent_pid)
            or hwnd in owner_hwnds
        ):
            wxlog.debug("菜单身份校验失败，跳过 WM_CLOSE")
            return False

        if post_close_message(hwnd, self._native_window_classes):
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline:
                if not is_window(hwnd):
                    return True
                time.sleep(0.05)
            return not is_window(hwnd)
        wxlog.debug("菜单 HWND 无法通过独立 XMenu 校验，跳过 Esc/WM_CLOSE")
        return False


class WeChatDialog(BaseUISubWnd):
    """通用微信对话框。"""

    _dialog_classes = ("mmui::XDialog", "mmui::WeUIDialog", "#32770")

    def __init__(self, parent: BaseUIWnd, wait: float = 3.0) -> None:
        self.root = parent
        self.parent = parent
        self._wait = wait
        self._cached_control = None

    @property
    def control(self):
        """动态查找当前可见对话框。"""
        if self._cached_control is not None and self._cached_control.Exists(0):
            return self._cached_control
        self._cached_control = self._find()
        return self._cached_control

    def _find(self):
        deadline = time.monotonic() + self._wait
        while time.monotonic() < deadline:
            for class_name in self._dialog_classes:
                if self.parent is not None and self.parent.exists():
                    control = uia.find_descendant(
                        self.parent.control,
                        class_name=class_name,
                        timeout=0.5,
                    )
                    if control is not None:
                        return control
                control = uia.find_top_level_control(class_name, timeout=0.5)
                if control is not None:
                    return control
            time.sleep(0.2)
        return None

    def exists(self, wait: float = 0.0) -> bool:
        control = self.control
        return bool(control is not None and control.Exists(wait))

    def get_all_text(self) -> list[str]:
        """收集对话框内所有可读文本。"""
        if not self.exists():
            return []
        result: list[str] = []
        stack = [self.control]
        while stack:
            current = stack.pop()
            try:
                if current.ControlTypeName in ("TextControl", "EditControl", "ButtonControl"):
                    name = current.Name
                    if name:
                        result.append(str(name))
            except Exception:
                pass
            try:
                children = current.GetChildren()
            except Exception:
                children = []
            stack.extend(reversed(children))
        return result

    @uilock
    def click_button(self, text: str, move: bool = True) -> WxResponse:
        """点击对话框内指定文本的按钮。"""
        if not self.exists():
            return WxResponse.failure("对话框不存在")
        control = uia.find_descendant(
            self.control,
            control_type="ButtonControl",
            name=text,
            timeout=self._wait,
        )
        if control is None:
            return WxResponse.failure(f"未找到按钮：{text}")
        try:
            control.Click(simulateMove=move, waitTime=0.5)
            return WxResponse.success()
        except Exception as exc:
            return WxResponse.error(f"点击按钮失败: {exc}")


class SelectContactWnd(BaseUISubWnd):
    """转发/群聊等场景下的联系人选择窗口。"""

    _ui_cls_name = "mmui::SessionPickerWindow"

    def __init__(self, parent: BaseUIWnd, timeout: float = 2.0) -> None:
        self.root = parent
        self.parent = parent
        self.control = self._find(timeout)
        self._search_edit = None
        self._leave_edit = None
        self._confirm_btn = None
        self._cancel_btn = None
        self._cache_controls()

    def _find(self, timeout: float):
        roots = [self.parent]
        if getattr(self.parent, "root", None) is not None and self.parent.root is not self.parent:
            roots.append(self.parent.root)
        for root in roots:
            if not root.exists():
                continue
            control = uia.find_descendant(
                root.control,
                control_type="WindowControl",
                class_name=self._ui_cls_name,
                timeout=min(timeout, 1.0),
            )
            if control is not None:
                return control
        return uia.find_top_level_control(self._ui_cls_name, timeout=timeout)

    def _cache_controls(self) -> None:
        if self.control is None:
            return
        self._search_edit = uia.find_descendant(
            self.control,
            control_type="EditControl",
            class_name="mmui::XValidatorTextEdit",
            timeout=1.0,
        )
        self._leave_edit = uia.find_descendant(
            self.control,
            control_type="EditControl",
            class_name="mmui::ChatInputField",
            timeout=1.0,
        )
        self._confirm_btn = uia.find_descendant(
            self.control,
            control_type="ButtonControl",
            class_name="mmui::XOutlineButton",
            automation_id="confirm_btn",
            timeout=1.0,
        )
        self._cancel_btn = uia.find_descendant(
            self.control,
            control_type="ButtonControl",
            class_name="mmui::XOutlineButton",
            automation_id="cancel_btn",
            timeout=1.0,
        )

    def _center(self, control):
        try:
            rect = control.BoundingRectangle
            return int(rect.left + (rect.right - rect.left) // 2), int(rect.top + (rect.bottom - rect.top) // 2)
        except Exception:
            return None

    def search(self, keyword: str, interval: float = 0.1) -> None:
        edit = self._search_edit
        if edit is None and self.control is not None:
            self._cache_controls()
            edit = self._search_edit
        if edit is None:
            return
        point = self._center(edit)
        if point is None:
            return
        uia.click_screen(point[0], point[1], wait=0.4)
        edit.SendKeys("{Ctrl}a", waitTime=0.2)
        edit.SendKeys(keyword, interval=interval, waitTime=0.5)

    def select(self, target: str, timeout: float = 3.0) -> WxResponse:
        """在搜索结果中勾选目标联系人/群。"""
        if self.control is None:
            return WxResponse.failure("选择联系人窗口不存在")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            checkboxes = uia.find_controls(
                self.control,
                control_type="CheckBoxControl",
                class_name="mmui::SearchContactCellView",
                max_results=30,
            )
            for checkbox in checkboxes:
                try:
                    if checkbox.Name == target:
                        checkbox.Click(simulateMove=False, waitTime=0.4)
                        return WxResponse.success()
                except Exception:
                    continue
            time.sleep(0.2)
        return WxResponse.failure(f"未找到联系人：{target}")

    def add_message(self, content: str) -> WxResponse:
        """填写“给朋友留言”。"""
        if not content:
            return WxResponse.success()
        edit = self._leave_edit
        if edit is None and self.control is not None:
            self._cache_controls()
            edit = self._leave_edit
        if edit is None:
            return WxResponse.failure("未找到留言输入框")
        try:
            edit.GetValuePattern().SetValue(content)
            return WxResponse.success()
        except Exception:
            pass
        set_text(content)
        point = self._center(edit)
        if point is None:
            return WxResponse.failure("无法定位留言输入框")
        uia.click_screen(point[0], point[1], wait=0.3)
        edit.SendKeys("{Ctrl}v", waitTime=0.5)
        return WxResponse.success()

    @uilock
    def confirm(self) -> WxResponse:
        """勾选联系人后，点击“发送”按钮。

        用户确认：选中至少一个对象后发送按钮才会从灰色变为绿色可点。
        这里使用初始化时缓存的按钮坐标，避免选中后再次遍历 UIA 导致卡住。
        """
        if self.control is None or not self.exists():
            return WxResponse.failure("选择联系人窗口不存在")
        button = self._confirm_btn
        if button is None and self.control is not None:
            self._cache_controls()
            button = self._confirm_btn
        if button is None:
            return WxResponse.failure("未找到发送按钮")
        # 勾选后稍等按钮状态刷新，再按缓存坐标点击。
        time.sleep(0.6)
        point = self._center(button)
        if point is None:
            try:
                button.Click(simulateMove=False, waitTime=0.8)
                return WxResponse.success()
            except Exception as exc:
                return WxResponse.error(f"点击发送按钮失败: {exc}")
        uia.click_screen(point[0], point[1], wait=0.8)
        return WxResponse.success()

    def close(self) -> None:
        """优先点击取消按钮；Esc 在部分版本不会关闭该窗口。"""
        if self.control is None or not self.exists():
            return
        cancel = self._cancel_btn
        if cancel is None:
            cancel = uia.find_descendant(
                self.control,
                control_type="ButtonControl",
                class_name="mmui::XOutlineButton",
                automation_id="cancel_btn",
                timeout=0.6,
            )
        if cancel is not None:
            try:
                point = self._center(cancel)
                if point is not None:
                    uia.click_screen(point[0], point[1], wait=0.5)
                else:
                    cancel.Click(simulateMove=False, waitTime=0.5)
                return
            except Exception:
                pass
        try:
            self.control.SendKeys("{Esc}", waitTime=0.4)
        except Exception:
            pass
        # 最后兜底：只有确认该 HWND 确实是允许的弹窗顶层窗口时才发
        # WM_CLOSE。UIA 子控件的 NativeWindowHandle 可能返回主窗口
        # HWND，若不校验会误关微信主窗口。
        try:
            hwnd = int(self.control.NativeWindowHandle or 0)
            if hwnd:
                post_close_message(
                    hwnd,
                    {
                        "mmui::SessionPickerWindow",
                        "mmui::XDialog",
                        "mmui::WeUIDialog",
                        "#32770",
                    },
                )
        except Exception:
            pass


_PROFILE_LABEL_MAP = {
    "微信号": "wxid",
    "地区": "region",
    "签名": "signature",
    "个性签名": "signature",
    "昵称": "nickname",
}


def extract_profile_info(items: list[dict[str, Any]]) -> dict[str, str]:
    """从 ProfileWnd 的文本控件列表里提取结构化个人信息。

    ``items`` 每项需包含 ``name`` / ``automation_id`` / ``rect``；
    纯函数，便于 Linux 侧单测。
    """
    result: dict[str, str] = {}
    texts: list[dict[str, Any]] = []
    for item in items:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        aid = str(item.get("aid") or item.get("automation_id") or "")
        rect = item.get("rect") or [0, 0, 0, 0]
        entry = {"name": name, "aid": aid, "rect": [int(v) for v in rect]}
        texts.append(entry)
        if aid.endswith("display_name_text"):
            result["nickname"] = name
            result["display_name"] = name

    # 资料卡是“左侧标签 + 同行右侧值”布局；UIA 的 AutomationId 不含
    # 微信号/地区等语义，因此按垂直中心和左右位置配对。
    for key in texts:
        label = key["name"].rstrip("：").rstrip(":").strip()
        target = _PROFILE_LABEL_MAP.get(label)
        if not target:
            continue
        k_left, k_top, k_right, k_bottom = key["rect"]
        key_center_y = (k_top + k_bottom) / 2
        best = None
        for value in texts:
            if value is key:
                continue
            v_left, v_top, v_right, v_bottom = value["rect"]
            value_center_y = (v_top + v_bottom) / 2
            if abs(value_center_y - key_center_y) > max(24.0, (k_bottom - k_top) * 0.75):
                continue
            if v_left < k_right - 12:
                continue
            if best is None or v_left < best["rect"][0]:
                best = value
        if best is not None:
            result[target] = best["name"]
    return result


class ProfileWnd(BaseUISubWnd):
    """个人资料卡窗口（用于 sender_info 等）。

    微信 4.1.12 实测：点击群消息头像后出现的是
    ``WindowControl Name=Weixin ClassName=mmui::ProfileUniquePop``。
    """

    _ui_cls_name = "mmui::ProfileUniquePop"

    def __init__(self, parent: BaseUIWnd | None = None, timeout: float = 3.0) -> None:
        self.root = parent
        self.parent = parent
        self.control = self._find(timeout)

    def _find(self, timeout: float):
        roots = []
        if self.parent is not None:
            roots.append(self.parent)
            if getattr(self.parent, "root", None) is not None and self.parent.root is not self.parent:
                roots.append(self.parent.root)
        for root in roots:
            if not root.exists():
                continue
            control = uia.find_descendant(
                root.control,
                class_name=self._ui_cls_name,
                timeout=min(timeout, 1.0),
            )
            if control is not None:
                return control
        return uia.find_top_level_control(self._ui_cls_name, timeout=timeout)

    @property
    def info(self) -> dict[str, str]:
        if self.control is None or not self.exists():
            return {}
        items: list[dict[str, Any]] = []
        for control in uia.find_controls(
            self.control,
            control_type="TextControl",
            max_results=120,
        ):
            try:
                name = str(control.Name or "").strip()
                if not name:
                    continue
                rect = control.BoundingRectangle
                items.append(
                    {
                        "name": name,
                        "aid": str(control.AutomationId or ""),
                        "rect": [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)],
                    }
                )
            except Exception:
                continue
        return extract_profile_info(items)

    def close(self) -> None:
        if self.control is None or not self.exists():
            return
        try:
            self.control.SendKeys("{Esc}", waitTime=0.4)
        except Exception:
            pass
        try:
            hwnd = int(self.control.NativeWindowHandle or 0)
            if hwnd:
                post_close_message(
                    hwnd,
                    {"mmui::ProfileUniquePop", "mmui::XDialog", "mmui::WeUIDialog", "#32770"},
                )
        except Exception:
            pass


class WeChatBrowser(BaseUISubWnd):
    """微信内置浏览器（实际由 WeChatAppEx / Chrome 承载）。

    实现路径与用户观察一致：

    1. 点击卡片后出现 ``Chrome_WidgetWin_0`` 浏览器窗口
    2. 反复点击“更多”(AppMenuButton)，直到页面加载完成
    3. 菜单中出现“复制链接”(FlueMenuItemView) 后点击
    4. 从剪贴板读取 URL
    5. ``Ctrl+W`` 关闭内置浏览器页面
    """

    _ui_cls_name = "Chrome_WidgetWin_0"
    _ui_name = "微信"

    def __init__(self, timeout: float = 10.0) -> None:
        self.root = None
        self.parent = None
        self.control = self._find(timeout)
        self._hwnd = int(getattr(self.control, "NativeWindowHandle", 0) or 0)
        self._pid = int(getattr(self.control, "ProcessId", 0) or 0)

    def _find(self, timeout: float):
        deadline = time.monotonic() + timeout
        started_at = time.monotonic()
        attempts = 0
        max_candidates = 0
        observed = []
        while time.monotonic() < deadline:
            candidates = uia.find_top_level_controls(
                class_name=self._ui_cls_name,
                name=self._ui_name,
                max_results=20,
            )
            attempts += 1
            max_candidates = max(max_candidates, len(candidates))
            for control in candidates:
                detail = {}
                try:
                    pid = int(control.ProcessId or 0)
                    process_name = str(get_process_name(pid) or "").casefold()
                    detail.update(pid=pid, process_name=process_name)
                    if process_name == "wechatappex.exe":
                        more = uia.find_descendant(
                            control,
                            control_type="ButtonControl",
                            name="更多",
                            class_name="AppMenuButton",
                            timeout=0.4,
                        )
                        detail["more_button_found"] = more is not None
                        if more is not None:
                            wxlog.info(
                                f"链接浏览器识别成功: attempts={attempts} "
                                f"elapsed={time.monotonic() - started_at:.3f}s candidate={detail}"
                            )
                            return control
                except Exception as exc:
                    detail["probe_error"] = str(exc)[:160]
                    continue
                finally:
                    if detail not in observed and len(observed) < 12:
                        observed.append(detail)
            time.sleep(0.25)
        wxlog.warning(
            f"链接浏览器识别超时: timeout={timeout} attempts={attempts} "
            f"elapsed={time.monotonic() - started_at:.3f}s "
            f"expected_class={self._ui_cls_name!r} expected_name={self._ui_name!r} "
            f"max_candidates={max_candidates} observed={observed}"
        )
        return None

    def exists(self, wait: float = 0.0) -> bool:
        return bool(self.control is not None and self.control.Exists(wait))

    def _identity_is_safe(self) -> bool:
        """确认仍是构造时捕获的同一个微信内置浏览器顶层窗口。"""
        if not self.exists() or not self._hwnd or not self._pid:
            return False
        try:
            return (
                int(getattr(self.control, "NativeWindowHandle", 0) or 0) == self._hwnd
                and int(getattr(self.control, "ProcessId", 0) or 0) == self._pid
                and str(getattr(self.control, "ClassName", "") or "")
                == self._ui_cls_name
                and str(get_process_name(self._pid) or "").casefold()
                == "wechatappex.exe"
            )
        except Exception:
            return False

    def _find_option_fast(self, option: str):
        """零等待/短等待查找“复制链接”，优先浏览器 MenuBar。"""
        try:
            menu_bar = self.control.MenuBarControl()
            for name in (option, "Copy Link", "Copy link"):
                item = menu_bar.MenuItemControl(Name=name)
                if item.Exists(0):
                    return item
        except Exception:
            pass
        return self._find_menu_control(
            control_type="MenuItemControl",
            name=option,
            timeout=0.12,
        )

    def _find_menu_control(self, **criteria):
        """A menu rebuilt by Chromium may invalidate a node during traversal."""
        try:
            return uia.find_descendant(self.control, **criteria)
        except Exception as exc:
            code = getattr(exc, "hresult", exc.args[0] if exc.args else None)
            if code not in (-2147220991, -2147417848):
                raise
            wxlog.debug(f"浏览器菜单节点已失效，等待下一轮查找: {exc}")
            return None

    @uilock
    def select_options(self, option: str, timeout: float = 15.0) -> WxResponse:
        """高频点击“更多”，菜单里一出现目标项就立即点击。

        与 Mabobot 补丁一致的快路径：坐标直点 + 30-80ms 轮询；
        不再等待固定 0.6s，也不使用慢速 UIA 全树搜索。
        """
        if not self.exists():
            return WxResponse.failure("微信内置浏览器不存在")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            more = self._find_menu_control(
                control_type="ButtonControl",
                name="更多",
                class_name="AppMenuButton",
                timeout=0.25,
            )
            if more is not None:
                try:
                    rect = more.BoundingRectangle
                    uia.click_screen(
                        int(rect.left + (rect.right - rect.left) // 2),
                        int(rect.top + (rect.bottom - rect.top) // 2),
                        wait=0.05,
                    )
                except Exception:
                    more.Click(simulateMove=False, waitTime=0.05)
                for _ in range(10):
                    if time.monotonic() >= deadline:
                        break
                    item = self._find_option_fast(option)
                    try:
                        ready = item is not None and item.Exists(0)
                    except Exception as exc:
                        code = getattr(exc, "hresult", exc.args[0] if exc.args else None)
                        if code not in (-2147220991, -2147417848):
                            raise
                        ready = False
                    if ready:
                        try:
                            rect = item.BoundingRectangle
                            uia.click_screen(
                                int(rect.left + (rect.right - rect.left) // 2),
                                int(rect.top + (rect.bottom - rect.top) // 2),
                                wait=0.08,
                            )
                        except Exception:
                            item.Click(simulateMove=False, waitTime=0.08)
                        return WxResponse.success()
                    time.sleep(0.03)
                try:
                    self.control.SendKeys("{Esc}", waitTime=0.05)
                except Exception:
                    pass
            time.sleep(0.05)
        return WxResponse.failure(f"未找到浏览器菜单项：{option}")

    @uilock
    def copy_url(self, timeout: float = 15.0) -> WxResponse:
        """复制当前内置浏览器页面的 URL。"""
        response = self.select_options("复制链接", timeout=timeout)
        if not response.is_success:
            return response
        time.sleep(0.1)
        url = get_text().strip()
        if not url:
            return WxResponse.failure("复制链接后剪贴板为空")
        if not url.startswith(("http://", "https://")):
            return WxResponse.failure(f"剪贴板内容不是 URL: {url!r}")
        return WxResponse.success(data={"url": url})

    @uilock
    def close(self, max_tabs: int = 3) -> WxResponse:
        """只关闭构造时捕获的微信内置浏览器窗口。

        优先对已校验的 Chrome 顶层 HWND 发送 WM_CLOSE。只有该窗口仍然
        存在、PID/类名/进程名均未变化且能切到前台时，才允许用 Ctrl+W
        作为多标签页兜底，绝不把按键发给聊天窗口或微信主窗口。
        """
        if not self.exists():
            return WxResponse.success()
        if not self._identity_is_safe():
            return WxResponse.failure("浏览器窗口身份校验失败，已取消关闭")

        if post_close_message(self._hwnd, {self._ui_cls_name}):
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if not is_window(self._hwnd) or not self.exists():
                    return WxResponse.success()
                time.sleep(0.1)

        for _ in range(max_tabs):
            if not is_window(self._hwnd) or not self.exists():
                return WxResponse.success()
            if not self._identity_is_safe():
                return WxResponse.failure("浏览器窗口身份发生变化，已取消快捷键关闭")
            if not force_foreground(self._hwnd) or get_foreground_window() != self._hwnd:
                return WxResponse.failure("浏览器窗口无法安全切到前台，已取消快捷键关闭")
            try:
                self.control.SendKeys("{Ctrl}w", waitTime=0.8)
            except Exception as exc:
                return WxResponse.error(f"关闭内置浏览器失败: {exc}")
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if not is_window(self._hwnd) or not self.exists():
                    return WxResponse.success()
                time.sleep(0.1)
        return WxResponse.failure("关闭微信浏览器页面超时")
