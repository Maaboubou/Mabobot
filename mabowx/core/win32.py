"""Win32 窗口与进程辅助函数。

非 Windows 平台可以导入本模块，但调用 Windows 专有函数会抛出 RuntimeError。
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable

try:
    import psutil
except ImportError:  # pragma: no cover - Linux 纯逻辑测试环境可能未安装
    psutil = None  # type: ignore[assignment]


def is_windows() -> bool:
    return sys.platform == "win32" or os.name == "nt"


def _require_win32():
    if not is_windows():
        raise RuntimeError("mabowx Win32 功能只能在 Windows 上使用")
    import win32api  # noqa: F401
    import win32con  # noqa: F401
    import win32gui  # noqa: F401
    import win32process  # noqa: F401

    return win32api, win32con, win32gui, win32process


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    class_name: str
    visible: bool
    rect: tuple[int, int, int, int]

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]


def enum_windows_by_pid(pid: int) -> list[WindowInfo]:
    """枚举指定进程的所有顶层窗口。"""
    _, _, win32gui, win32process = _require_win32()
    result: list[WindowInfo] = []

    def _callback(hwnd: int, _param: object) -> bool:
        try:
            _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return True
        if found_pid != pid:
            return True
        try:
            result.append(_window_info(hwnd, pid))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_callback, None)
    return result


def _window_info(hwnd: int, pid: int) -> WindowInfo:
    _, _, win32gui, _ = _require_win32()
    title = win32gui.GetWindowText(hwnd)
    class_name = win32gui.GetClassName(hwnd)
    visible = bool(win32gui.IsWindowVisible(hwnd))
    rect = win32gui.GetWindowRect(hwnd)
    return WindowInfo(int(hwnd), int(pid), title or "", class_name, visible, tuple(rect))


def get_window_info(hwnd: int) -> WindowInfo | None:
    """读取指定 HWND 的当前顶层窗口身份。

    ``IsWindow`` 只能说明句柄此刻仍被某个窗口占用；窗口关闭后 Windows
    可能很快复用同一个数值。监听窗口缓存因此还必须核对 PID、标题和窗口
    类，不能把被其他窗口复用的 HWND 当成原聊天仍然存活。
    """
    if not is_windows():
        return None
    try:
        _, _, win32gui, win32process = _require_win32()
        if not win32gui.IsWindow(hwnd):
            return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return _window_info(int(hwnd), int(pid))
    except Exception:
        return None


def get_window_geometry(hwnd: int) -> dict[str, object]:
    """Return current/restore geometry, including Qt's -32000 failure state."""
    from .window_health import (
        is_offscreen_sentinel_rect,
        is_unrecoverable_offscreen_window,
    )

    result: dict[str, object] = {
        "visible": False,
        "iconic": False,
        "show_cmd": 0,
        "window_rect": {},
        "normal_rect": {},
        "offscreen_sentinel": False,
        "normal_offscreen_sentinel": False,
        "unrecoverable_offscreen": False,
    }
    if not is_windows() or not hwnd:
        return result
    try:
        _, _, win32gui, _ = _require_win32()
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        placement = win32gui.GetWindowPlacement(hwnd)
        normal = placement[4]
        window_rect = {
            "left": int(left),
            "top": int(top),
            "right": int(right),
            "bottom": int(bottom),
        }
        normal_rect = {
            "left": int(normal[0]),
            "top": int(normal[1]),
            "right": int(normal[2]),
            "bottom": int(normal[3]),
        }
        iconic = bool(win32gui.IsIconic(hwnd))
        result.update({
            "visible": bool(win32gui.IsWindowVisible(hwnd)),
            "iconic": iconic,
            "show_cmd": int(placement[1]),
            "window_rect": window_rect,
            "normal_rect": normal_rect,
            "offscreen_sentinel": is_offscreen_sentinel_rect(window_rect),
            "normal_offscreen_sentinel": is_offscreen_sentinel_rect(normal_rect),
            "unrecoverable_offscreen": is_unrecoverable_offscreen_window(
                window_rect=window_rect,
                normal_rect=normal_rect,
                iconic=iconic,
            ),
        })
    except Exception as exc:
        result["error"] = str(exc)
    return result


def is_hung_window(hwnd: int) -> bool:
    if not is_windows() or not hwnd:
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.IsHungAppWindow(int(hwnd)))
    except Exception:
        return False


def restore_window_no_activate(hwnd: int, rect: dict[str, int]) -> bool:
    """Restore one exact HWND without stealing foreground focus."""
    if not is_windows() or not hwnd:
        return False
    try:
        _, win32con, win32gui, _ = _require_win32()
        if not win32gui.IsWindow(hwnd):
            return False
        placement = win32gui.GetWindowPlacement(hwnd)
        normal = (
            int(rect["left"]),
            int(rect["top"]),
            int(rect["right"]),
            int(rect["bottom"]),
        )
        win32gui.SetWindowPlacement(
            hwnd,
            (0, win32con.SW_SHOWNOACTIVATE, placement[2], placement[3], normal),
        )
        width = normal[2] - normal[0]
        height = normal[3] - normal[1]
        flags = (
            win32con.SWP_NOZORDER
            | win32con.SWP_NOACTIVATE
            | win32con.SWP_SHOWWINDOW
            | win32con.SWP_NOOWNERZORDER
        )
        win32gui.SetWindowPos(
            hwnd,
            0,
            normal[0],
            normal[1],
            width,
            height,
            flags,
        )
        return True
    except Exception:
        return False


def find_window_for_process(
    pid: int,
    class_name: str | None = None,
    title: str | None = None,
    visible: bool | None = True,
) -> WindowInfo | None:
    """在指定进程的顶层窗口中查找匹配窗口。

    匹配策略：优先可见 + class/title 完全匹配；其次可见；最后任意。
    """
    wins = enum_windows_by_pid(pid)
    if not wins:
        return None
    if visible is not None:
        wins = [w for w in wins if w.visible == visible]
        if not wins and visible:
            wins = enum_windows_by_pid(pid)

    def score(w: WindowInfo) -> int:
        value = 0
        if class_name and w.class_name == class_name:
            value += 10
        if title and w.title == title:
            value += 5
        value += w.width * w.height
        return value

    return max(wins, key=score)


def is_window(hwnd: int) -> bool:
    """判断 HWND 是否仍然有效。"""
    if not is_windows():
        return False
    try:
        _, _, win32gui, _ = _require_win32()
        return bool(win32gui.IsWindow(hwnd))
    except Exception:
        return False


def is_window_visible(hwnd: int) -> bool:
    if not is_windows():
        return False
    try:
        _, _, win32gui, _ = _require_win32()
        return bool(win32gui.IsWindowVisible(hwnd))
    except Exception:
        return False


def force_foreground(hwnd: int) -> bool:
    """尽力把窗口切换到前台。

    直接 SetForegroundWindow 可能被 Windows 拒绝；先 AttachThreadInput
    再 BringWindowToTop / SetForegroundWindow 可提高成功率。
    """
    if not is_windows():
        return False
    _, win32con, win32gui, win32process = _require_win32()
    if not win32gui.IsWindow(hwnd):
        return False
    try:
        import win32api
        import win32process as wp

        foreground = win32gui.GetForegroundWindow()
        current_thread = win32api.GetCurrentThreadId()
        foreground_thread = wp.GetWindowThreadProcessId(foreground)[0] if foreground else 0
        if foreground_thread and foreground_thread != current_thread:
            try:
                wp.AttachThreadInput(current_thread, foreground_thread, True)
            except Exception:
                pass
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
        if foreground_thread and foreground_thread != current_thread:
            try:
                wp.AttachThreadInput(current_thread, foreground_thread, False)
            except Exception:
                pass
        return win32gui.GetForegroundWindow() == hwnd
    except Exception:
        return False


def get_foreground_window() -> int | None:
    _, _, win32gui, _ = _require_win32()
    hwnd = win32gui.GetForegroundWindow()
    return int(hwnd) if hwnd else None


def get_window_owner(hwnd: int) -> int | None:
    """Return the exact top-level owner of a popup window, if any."""

    if not is_windows() or not hwnd:
        return None
    try:
        _, win32con, win32gui, _ = _require_win32()
        if not win32gui.IsWindow(hwnd):
            return None
        owner = win32gui.GetWindow(int(hwnd), win32con.GW_OWNER)
        return int(owner) if owner else None
    except Exception:
        return None


def post_right_click(hwnd: int, screen_x: int, screen_y: int) -> bool:
    """向指定窗口同步定向投递右键，避免命中重叠窗口或延迟漂移。

    微信的多个独立聊天窗口常完全重叠。全局鼠标事件即使使用目标消息的
    屏幕坐标，也可能点到最上层的另一个聊天。这里先严格确认点位位于
    ``hwnd``，再转成客户区坐标，并让移动/按下/抬起消息在返回前完成
    窗口过程处理；不移动真实鼠标，也不切换用户当前的前台窗口。
    """
    if not is_windows():
        return False
    try:
        win32api, win32con, win32gui, _ = _require_win32()
        if not hwnd or not win32gui.IsWindow(hwnd):
            return False
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        x = int(screen_x)
        y = int(screen_y)
        if x < int(left) or x >= int(right) or y < int(top) or y >= int(bottom):
            return False
        client_x, client_y = win32gui.ScreenToClient(hwnd, (x, y))
        lparam = win32api.MAKELONG(int(client_x), int(client_y))
        flags = win32con.SMTO_ABORTIFHUNG | win32con.SMTO_BLOCK
        messages = (
            (win32con.WM_MOUSEMOVE, 0, lparam),
            (win32con.WM_RBUTTONDOWN, win32con.MK_RBUTTON, lparam),
            (win32con.WM_RBUTTONUP, 0, lparam),
            # SendMessage-ing RBUTTONUP does not make Windows synthesize the
            # WM_CONTEXTMENU that a physical right-click normally produces.
            # Qt's chat window opens its XMenu from this final message.
            (win32con.WM_CONTEXTMENU, int(hwnd), win32api.MAKELONG(x, y)),
        )
        for message, wparam, message_lparam in messages:
            result = win32gui.SendMessageTimeout(
                hwnd,
                message,
                wparam,
                message_lparam,
                flags,
                500,
            )
            if not result or not result[0]:
                return False
        return True
    except Exception:
        return False


def post_left_click(hwnd: int, screen_x: int, screen_y: int) -> bool:
    """向指定窗口的指定屏幕坐标同步定向投递一次左键点击。"""

    if not is_windows():
        return False
    try:
        win32api, win32con, win32gui, _ = _require_win32()
        if not hwnd or not win32gui.IsWindow(hwnd):
            return False
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        x = int(screen_x)
        y = int(screen_y)
        if x < int(left) or x >= int(right) or y < int(top) or y >= int(bottom):
            return False
        client_x, client_y = win32gui.ScreenToClient(hwnd, (x, y))
        lparam = win32api.MAKELONG(int(client_x), int(client_y))
        flags = win32con.SMTO_ABORTIFHUNG | win32con.SMTO_BLOCK
        messages = (
            (win32con.WM_MOUSEMOVE, 0),
            (win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON),
            (win32con.WM_LBUTTONUP, 0),
        )
        for message, wparam in messages:
            result = win32gui.SendMessageTimeout(
                hwnd,
                message,
                wparam,
                lparam,
                flags,
                500,
            )
            if not result or not result[0]:
                return False
        return True
    except Exception:
        return False


def set_foreground_window(hwnd: int) -> bool:
    """请求前台窗口，并尽力通过 ShowWindow 绕过焦点限制。"""
    _, win32con, win32gui, _ = _require_win32()
    if not win32gui.IsWindow(hwnd):
        return False
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    except Exception:
        pass
    return bool(win32gui.SetForegroundWindow(hwnd))


def show_window(hwnd: int, cmd: int | None = None) -> bool:
    _, win32con, win32gui, _ = _require_win32()
    if not win32gui.IsWindow(hwnd):
        return False
    return bool(win32gui.ShowWindow(hwnd, cmd if cmd is not None else win32con.SW_SHOW))


def can_post_close(hwnd: int, allowed_class_names: Iterable[str]) -> bool:
    """判断能否安全地向 hwnd 发送 WM_CLOSE。

    UIA 子控件的 NativeWindowHandle 经常返回其所属顶层窗口的 HWND；
    若不校验，弹窗关闭兜底可能误把微信主窗口一起关掉。

    只有同时满足以下条件才允许：
    - hwnd 存在且是顶层窗口（GetParent == 0）
    - 窗口类名在显式允许列表里

    ``mmui::MainWindow`` / ``mmui::ChatSingleWindow`` 永远不会被允许。
    """
    if not hwnd or not is_windows():
        return False
    try:
        _, _, win32gui, _ = _require_win32()
        if not win32gui.IsWindow(hwnd):
            return False
        # 有父窗口说明该 HWND 只是子控件，WM_CLOSE 可能冒泡到顶层窗口。
        if win32gui.GetParent(hwnd):
            return False
        class_name = win32gui.GetClassName(hwnd)
        allowed = {str(name) for name in allowed_class_names}
        return class_name in allowed
    except Exception:
        return False


def post_close_message(hwnd: int, allowed_class_names: Iterable[str]) -> bool:
    """安全地给弹窗 HWND 发送 WM_CLOSE；不允许的窗口返回 False。"""
    if not can_post_close(hwnd, allowed_class_names):
        return False
    try:
        import win32con

        _, _, win32gui, _ = _require_win32()
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        return True
    except Exception:
        return False


def move_window(hwnd: int, x: int, y: int, width: int, height: int, repaint: bool = True) -> bool:
    _, _, win32gui, _ = _require_win32()
    if not win32gui.IsWindow(hwnd):
        return False
    return bool(win32gui.MoveWindow(hwnd, x, y, width, height, repaint))


def set_window_pos(
    hwnd: int,
    *,
    x: int = 0,
    y: int = 0,
    width: int | None = None,
    height: int | None = None,
    flags: int | None = None,
) -> bool:
    _, win32con, win32gui, _ = _require_win32()
    if not win32gui.IsWindow(hwnd):
        return False
    if width is None or height is None:
        rect = win32gui.GetWindowRect(hwnd)
        width = width if width is not None else rect[2] - rect[0]
        height = height if height is not None else rect[3] - rect[1]
    if flags is None:
        flags = win32con.SWP_NOZORDER
    return bool(win32gui.SetWindowPos(hwnd, 0, x, y, width, height, flags))


def capture_window_rect(hwnd: int, bbox: tuple[int, int, int, int]):
    """抓取指定窗口矩形区域，返回 PIL RGB Image。

    使用 PrintWindow(PW_RENDERFULLCONTENT) 从窗口 DC 截图，即使窗口
    被部分遮挡也能拿到正确内容；这比 ImageGrab 更适合消息方向判断。

    必须先按完整窗口尺寸绘制，再裁剪 bbox。实测微信 Qt 窗口在
    PrintWindow 绘制时不遵守调用方的 viewport 偏移；直接向小尺寸
    bitmap 绘制会得到窗口顶部，而不是指定的消息行。身份校验和
    点击坐标都依赖截图位置正确，不能用 viewport 偏移优化内存。
    """
    if not is_windows():
        raise RuntimeError("mabowx 截图功能只能在 Windows 上使用")
    import ctypes

    import win32con
    import win32gui
    import win32ui
    from PIL import Image

    left, top, right, bottom = (int(v) for v in bbox)
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        raise ValueError(f"无效截图区域: {bbox}")

    win_left, win_top, win_right, win_bottom = win32gui.GetWindowRect(hwnd)
    window_width = win_right - win_left
    window_height = win_bottom - win_top
    if window_width <= 0 or window_height <= 0:
        raise RuntimeError(f"窗口尺寸无效: hwnd={hwnd}")

    # bbox 是屏幕绝对坐标，PrintWindow 使用窗口相对坐标。
    source_x = left - win_left
    source_y = top - win_top

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    # Qt may reset the DC origin while servicing PrintWindow. Render at the
    # actual window size/origin; crop pixels only after the render completes.
    bitmap.CreateCompatibleBitmap(mfc_dc, window_width, window_height)
    old_bitmap = save_dc.SelectObject(bitmap)
    hdc = save_dc.GetSafeHdc()
    saved_dc = 0
    try:
        # Keep pixels outside the window deterministic when bbox is one pixel
        # beyond a rounded UIA/window boundary.
        save_dc.PatBlt((0, 0), (window_width, window_height), win32con.BLACKNESS)
        saved_dc = int(save_dc.SaveDC() or 0)
        result = ctypes.windll.user32.PrintWindow(hwnd, hdc, 2)
        if saved_dc:
            save_dc.RestoreDC(saved_dc)
            saved_dc = 0
        if not result:
            # PrintWindow can fail for a temporarily busy window.  Preserve the
            # previous visible-window fallback, but copy only the intersecting
            # requested rectangle instead of the complete window.
            copy_left = max(left, win_left)
            copy_top = max(top, win_top)
            copy_right = min(right, win_right)
            copy_bottom = min(bottom, win_bottom)
            copy_width = max(0, copy_right - copy_left)
            copy_height = max(0, copy_bottom - copy_top)
            if copy_width and copy_height:
                save_dc.BitBlt(
                    (copy_left - win_left, copy_top - win_top),
                    (copy_width, copy_height),
                    mfc_dc,
                    (copy_left - win_left, copy_top - win_top),
                    win32con.SRCCOPY,
                )
        bits = bitmap.GetBitmapBits(True)
        # crop() owns its pixels and pads any region outside the window black.
        # Return only the requested region, without retaining the full bitmap.
        return Image.frombuffer(
            "RGB", (window_width, window_height), bits, "raw", "BGRX", 0, 1
        ).crop((source_x, source_y, source_x + width, source_y + height))
    finally:
        if saved_dc:
            try:
                save_dc.RestoreDC(saved_dc)
            except Exception:
                pass
        try:
            if old_bitmap is not None:
                save_dc.SelectObject(old_bitmap)
        except Exception:
            pass
        try:
            save_dc.DeleteDC()
        except Exception:
            pass
        try:
            mfc_dc.DeleteDC()
        except Exception:
            pass
        try:
            win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass
        try:
            win32gui.DeleteObject(bitmap.GetHandle())
        except Exception:
            pass


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    _, _, win32gui, _ = _require_win32()
    return tuple(win32gui.GetWindowRect(hwnd))


def get_monitor_info() -> list[dict[str, object]]:
    """返回显示器信息列表，每项含 Position 和 Size。"""
    _, _, win32gui, _ = _require_win32()
    result: list[dict[str, object]] = []

    def _callback(monitor: int, _dc: object, _rect: object, _data: object) -> bool:
        try:
            info = win32gui.GetMonitorInfo(monitor)
        except Exception:
            return True
        rect = info["Monitor"]
        work = info.get("Work") or rect
        result.append(
            {
                "Position": (rect[0], rect[1]),
                "Size": (rect[2] - rect[0], rect[3] - rect[1]),
                "Width": rect[2] - rect[0],
                "Height": rect[3] - rect[1],
                "WorkPosition": (work[0], work[1]),
                "WorkSize": (work[2] - work[0], work[3] - work[1]),
                "WorkWidth": work[2] - work[0],
                "WorkHeight": work[3] - work[1],
                "Device": info.get("Device", ""),
            }
        )
        return True

    win32gui.EnumDisplayMonitors(None, None, _callback, None)
    return result


def _require_psutil():
    if psutil is None:
        raise RuntimeError("psutil 未安装，无法执行进程操作")
    return psutil


def get_process_path(pid: int) -> str | None:
    """获取进程可执行文件完整路径。"""
    try:
        return _require_psutil().Process(pid).exe() or None
    except Exception:
        return None


def get_process_name(pid: int) -> str | None:
    """获取进程名。"""
    try:
        return _require_psutil().Process(pid).name() or None
    except Exception:
        return None


def get_version_by_path(path: str) -> str | None:
    """读取 Windows 文件的 FileVersion。"""
    if not is_windows():
        return None
    try:
        import win32api

        info = win32api.GetFileVersionInfo(path, "\\")
        ms = info["FileVersionMS"]
        ls = info["FileVersionLS"]
        return f"{win32api.HIWORD(ms)}.{win32api.LOWORD(ms)}.{win32api.HIWORD(ls)}.{win32api.LOWORD(ls)}"
    except Exception:
        return None


def kill_process_tree(pid: int, wait_seconds: float = 3.0) -> bool:
    """终止进程及其子进程。"""
    try:
        psutil_mod = _require_psutil()
        parent = psutil_mod.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except Exception:
                pass
        parent.terminate()
        _, alive = psutil_mod.wait_procs([parent, *children], timeout=wait_seconds)
        for proc in alive:
            try:
                proc.kill()
            except Exception:
                pass
        return True
    except Exception:
        return False


def wait_for_window(
    predicate: Callable[[], WindowInfo | None],
    timeout: float = 5.0,
    interval: float = 0.2,
) -> WindowInfo | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        win = predicate()
        if win is not None:
            return win
        time.sleep(interval)
    return None
