"""Invoke WeChat's own tray action when its Qt window is hidden internally."""

from __future__ import annotations

import time

from . import uia


def _shell_controls():
    import win32gui

    for class_name in (
        "Shell_TrayWnd",
        "NotifyIconOverflowWindow",
        "TopLevelWindowForOverflowXamlIsland",
    ):
        hwnd = win32gui.FindWindow(class_name, None)
        if hwnd and win32gui.IsWindowVisible(hwnd):
            yield from uia.iter_descendants(uia.control_from_handle(hwnd), max_nodes=400)


def _find_buttons():
    icons, expanders = [], []
    for control in _shell_controls():
        try:
            if control.ControlTypeName != "ButtonControl":
                continue
            name = str(control.Name or "").strip()
            auto_id = str(control.AutomationId or "")
            class_name = str(control.ClassName or "")
            # Exclude pinned taskbar buttons: only notification-area icons count.
            if name in {"微信", "Weixin", "WeChat"} and (
                auto_id == "NotifyItemIcon" or class_name == "ToolbarWindow32"
            ):
                icons.append(control)
            elif auto_id == "Chevron" or name in {
                "显示隐藏的图标", "显示隐藏图标", "Show hidden icons"
            }:
                expanders.append(control)
        except Exception:
            continue
    return icons, expanders


def invoke_wechat_tray() -> bool:
    """Request tray activation without global hotkeys or moving the pointer.

    Success means Invoke was accepted; the caller must verify its own main
    window's UIA tree. Ambiguous icons are never selected arbitrarily.
    """
    expander = None
    try:
        icons, expanders = _find_buttons()
        if not icons and len(expanders) == 1:
            expander = expanders[0]
            expander.GetInvokePattern().Invoke()
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                icons, _ = _find_buttons()
                if icons:
                    break
                time.sleep(0.1)
        if len(icons) != 1:
            return False
        icons[0].GetInvokePattern().Invoke()
        return True
    except Exception:
        return False
    finally:
        # Only collapse an overflow we opened, and only if it is still expanded.
        if expander is not None:
            try:
                pattern = expander.GetExpandCollapsePattern()
                if pattern.ExpandCollapseState == 1:
                    pattern.Collapse()
            except Exception:
                pass
