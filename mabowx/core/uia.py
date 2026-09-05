"""UIA 基础能力。

Windows 专有；所有函数均为薄封装，方便后续加锁、重试和测试替身。
模块可以在非 Windows 平台导入，但调用函数会抛出明确的 RuntimeError。
"""

from __future__ import annotations

from typing import Any

import sys as _sys

if _sys.platform == "win32":  # pragma: no cover - Windows 专属
    try:
        import uiautomation as auto
    except ImportError:
        # 目标机可能没有单独安装 uiautomation，回退到 mabowx 内嵌的
        # Apache-2.0 上游副本。
        from .._vendor import uiautomation as auto
else:  # pragma: no cover - Linux 纯逻辑测试环境
    auto = None  # type: ignore[assignment]


def _require_uia() -> Any:
    if auto is None:
        raise RuntimeError("mabowx UIA 功能只能在 Windows 上使用")
    return auto


def find_main_window(
    name: str = "微信",
    class_name: str = "mmui::MainWindow",
    timeout: float = 5.0,
):
    """查找微信主窗口。"""
    auto_mod = _require_uia()
    # 优先按 Name + ClassName 查找。
    win = auto_mod.WindowControl(searchDepth=1, ClassName=class_name, Name=name)
    if win.Exists(timeout):
        return win
    # 英文/异常状态主窗口名称可能是 Weixin，按 ClassName 扫描顶层窗口。
    controls = find_top_level_controls(class_name=class_name, max_results=5)
    if controls:
        return controls[0]
    win = auto_mod.WindowControl(searchDepth=1, ClassName=class_name)
    if win.Exists(min(timeout, 3.0)):
        return win
    raise RuntimeError("未找到微信主窗口 mmui::MainWindow")


def find_top_level_control(
    class_name: str,
    name: str | None = None,
    timeout: float = 3.0,
):
    """按 ClassName 查找顶层 UIA 控件。"""
    auto_mod = _require_uia()
    kwargs: dict[str, Any] = {"searchDepth": 1, "ClassName": class_name}
    if name is not None:
        kwargs["Name"] = name
    control = auto_mod.Control(**kwargs)
    return control if control.Exists(timeout) else None


def control_from_handle(hwnd: int):
    """从 Win32 HWND 构造 UIA 控件。"""
    auto_mod = _require_uia()
    return auto_mod.ControlFromHandle(hwnd)


def click_screen(x: int, y: int, wait: float = 0.5) -> None:
    """在屏幕绝对坐标处单击。"""
    auto_mod = _require_uia()
    auto_mod.Click(x, y, waitTime=wait)


def right_click_screen(x: int, y: int, wait: float = 0.5) -> None:
    """在屏幕绝对坐标处右键单击。"""
    auto_mod = _require_uia()
    auto_mod.RightClick(x, y, waitTime=wait)


def get_focused_control():
    """返回当前 UIA 焦点控件。"""
    return _require_uia().GetFocusedControl()


def activate(control, wait: float = 0.6) -> None:
    """把指定控件所属窗口切到前台。"""
    control.SetActive(waitTime=wait)
    control.SwitchToThisWindow(waitTime=wait)


def describe(control) -> dict[str, Any]:
    """读取控件常用 UIA 属性。"""
    return {
        "control_type": control.ControlTypeName,
        "name": control.Name,
        "class_name": control.ClassName,
        "automation_id": control.AutomationId,
        "rect": list(control.BoundingRectangle),
        "enabled": bool(control.IsEnabled),
        "hwnd": int(control.NativeWindowHandle or 0),
        "pid": int(control.ProcessId or 0),
    }


def dump_tree(control, max_nodes: int = 500) -> list[str]:
    """深度优先导出 UIA 子树，返回可读行。"""
    lines: list[str] = []
    stack: list[tuple[Any, int]] = [(control, 0)]
    seen = 0
    while stack and seen < max_nodes:
        current, depth = stack.pop()
        try:
            info = describe(current)
        except Exception:
            info = {"error": "<unreadable>"}
        lines.append(
            f"{'  ' * depth}{info.get('control_type', '?')} "
            f"Name={info.get('name', '')!r} Class={info.get('class_name', '')!r} "
            f"AutoId={info.get('automation_id', '')!r} Rect={info.get('rect')}"
        )
        seen += 1
        try:
            children = current.GetChildren()
        except Exception:
            children = []
        for child in reversed(children):
            stack.append((child, depth + 1))
    return lines


def find_descendant(
    root,
    *,
    control_type: str | None = None,
    name: str | None = None,
    class_name: str | None = None,
    automation_id: str | None = None,
    timeout: float = 3.0,
):
    """按条件查找后代控件；返回 None 表示未找到。"""
    auto_mod = _require_uia()
    kwargs: dict[str, Any] = {"searchDepth": 0xFFFFFFFF}
    if control_type:
        kwargs["ControlType"] = getattr(auto_mod.ControlType, control_type)
    if name is not None:
        kwargs["Name"] = name
    if class_name is not None:
        kwargs["ClassName"] = class_name
    if automation_id is not None:
        kwargs["AutomationId"] = automation_id
    factory = getattr(root, control_type, None) if control_type else None
    if factory is not None:
        kwargs.pop("ControlType", None)
    else:
        factory = root.Control
    control = factory(**kwargs)
    return control if control.Exists(timeout) else None


def iter_descendants(root, max_nodes: int = 5000):
    """广度优先遍历后代控件。"""
    stack = list(reversed(list(root.GetChildren())))
    seen = 0
    while stack and seen < max_nodes:
        current = stack.pop()
        seen += 1
        yield current
        try:
            children = current.GetChildren()
        except Exception:
            children = []
        for child in reversed(children):
            stack.append(child)


def find_controls(
    root,
    *,
    control_type: str | None = None,
    name: str | None = None,
    class_name: str | None = None,
    automation_id: str | None = None,
    max_results: int = 50,
    max_nodes: int = 5000,
) -> list:
    """查找所有满足条件的后代控件。"""
    result: list[Any] = []
    for current in iter_descendants(root, max_nodes=max_nodes):
        try:
            if control_type and current.ControlTypeName != control_type:
                continue
            if name is not None and current.Name != name:
                continue
            if class_name and current.ClassName != class_name:
                continue
            if automation_id and current.AutomationId != automation_id:
                continue
        except Exception:
            continue
        result.append(current)
        if len(result) >= max_results:
            break
    return result


def find_top_level_controls(
    class_name: str,
    name: str | None = None,
    pid: int | None = None,
    max_results: int = 50,
    diagnostics: dict | None = None,
) -> list:
    """查找顶层控件；可选 diagnostics 保留原本被跳过的 UIA 错误。"""
    auto_mod = _require_uia()
    root = auto_mod.GetRootControl()
    result: list[Any] = []
    if diagnostics is not None:
        diagnostics.update(property_errors=0, pid_mismatches=0, enumeration_error=False, last_error=None)
    try:
        children = root.GetChildren()
    except Exception as exc:
        if diagnostics is not None:
            diagnostics.update(enumeration_error=True, last_error=f"{type(exc).__name__}: {exc}"[:240])
        return result
    for current in children:
        try:
            if class_name and current.ClassName != class_name:
                continue
            if name is not None and current.Name != name:
                continue
            if pid is not None and int(current.ProcessId or 0) != pid:
                if diagnostics is not None:
                    diagnostics["pid_mismatches"] += 1
                continue
        except Exception as exc:
            if diagnostics is not None:
                diagnostics["property_errors"] += 1
                diagnostics["last_error"] = f"{type(exc).__name__}: {exc}"[:240]
            continue
        result.append(current)
        if len(result) >= max_results:
            break
    return result
