"""消息类型定义。"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Callable

from mabowx.core import uia
from mabowx.core.clipboard import clear, get_text, read_files, set_text
from mabowx.core.win32 import (
    force_foreground,
    get_foreground_window,
    is_window,
    post_close_message,
    post_left_click,
    post_right_click,
)
from mabowx.core.locks import ui_transaction, uilock
from mabowx.logger import wxlog
from mabowx.param import WxParam
from mabowx.utils.tools import (
    find_content_center,
    find_quote_media_center,
    quote_media_fallback_point,
)

from .base import BaseMessage, HumanMessage
from .media_diagnostics import (
    media_download_trace,
    media_event,
    media_foreground_snapshot,
    media_ui_snapshot,
)
from .identity import (
    FILE_COLOR_MAX_DISTANCE,
    FILE_DETAIL_MAX_DISTANCE,
    FILE_MATCH_PROFILE,
    FILE_MATCH_THRESHOLDS,
    FILE_VISUAL_MAX_DISTANCE,
    MediaFileMismatchError,
    MediaIdentityError,
    compare_target_to_file,
    control_fully_visible,
    select_media_candidate,
    verify_dispatched_candidate,
    verify_stable_candidate,
)


THUMBNAIL_COPY_TIMEOUT_SEC = 3.0
THUMBNAIL_COPY_MAX_ATTEMPTS = 1
PREPARED_MEDIA_CREDENTIAL_TTL_SEC = 1.5
MEDIA_PREVIEW_OPEN_TIMEOUT_SEC = 6.0
TICKLE_CONTENT_PATTERN = re.compile(
    r"^\s*(?P<actor>.+?)\s*拍了拍\s*(?P<target>.+?)\s*$"
)
TICKLE_QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "“": "”",
    "‘": "’",
}


def _run_ordered_media_operation(message, operation):
    """Sequence a delayed media action before entering the global UI lock."""
    with media_download_trace(message):
        media_event("queued")

        def tracked_operation():
            media_event("ui_acquired")
            return operation()

        try:
            parent = getattr(message, "parent", None)
            runner = getattr(parent, "run_ordered_media_operation", None)
            if callable(runner):
                result = runner(message, tracked_operation)
            else:
                with ui_transaction(timeout=120.0):
                    result = tracked_operation()
        except Exception as exc:
            media_event("failed", level="warning", error_type=type(exc).__name__, error=str(exc))
            raise
        media_event("completed")
        return result


class SystemMessage(BaseMessage):
    type = "system"
    attr = "system"


def _strip_tickle_quotes(value: str) -> str:
    value = str(value or "").strip()
    if len(value) >= 2 and TICKLE_QUOTE_PAIRS.get(value[0]) == value[-1]:
        return value[1:-1].strip()
    return value


def parse_tickle_content(content: str) -> dict[str, str] | None:
    """解析微信“拍一拍”系统提示，返回拍人者、被拍者及后缀。

    微信 4.1.12 群聊真机样本为 ``"Gerry" 拍了拍 "刘局"``。
    同时兼容弯引号、无引号的“拍了拍我/你/自己”和目标后的自定义后缀。
    普通文本只包含“拍了拍”但缺少两侧主体时不会命中。
    """
    match = TICKLE_CONTENT_PATTERN.fullmatch(str(content or ""))
    if match is None:
        return None
    actor = _strip_tickle_quotes(match.group("actor"))
    target_text = str(match.group("target") or "").strip()
    target = ""
    suffix = ""
    if target_text:
        closing = TICKLE_QUOTE_PAIRS.get(target_text[0])
        if closing:
            closing_index = target_text.find(closing, 1)
            if closing_index > 0:
                target = target_text[1:closing_index].strip()
                suffix = target_text[closing_index + 1 :].strip()
        if not target:
            for pronoun in ("自己", "我", "你"):
                if target_text.startswith(pronoun):
                    target = pronoun
                    suffix = target_text[len(pronoun) :].strip()
                    break
        if not target:
            target = _strip_tickle_quotes(target_text)
    if not actor or not target:
        return None
    return {"from": actor, "to": target, "suffix": suffix}


class TickleMessage(SystemMessage):
    """结构化的“拍一拍”系统消息；公开 ``type`` 仍为 ``system``。"""

    is_tickle = True

    def __init__(self, control=None, parent=None) -> None:
        super().__init__(control=control, parent=parent)
        raw = str(getattr(control, "Name", "") or "")
        parsed = parse_tickle_content(raw) or {}
        self.tickle_from = str(parsed.get("from") or "")
        self.tickle_to = str(parsed.get("to") or "")
        self.tickle_suffix = str(parsed.get("suffix") or "")


class TimeMessage(SystemMessage):
    """时间分隔条。按需求统一按系统消息处理。"""

    type = "system"
    is_time = True


class OfficialMessage(BaseMessage):
    type = "official"
    attr = "system"


def _preview_identity_is_safe(control, hwnd: int, expected_pid: int | None) -> bool:
    """Verify that a control is still the exact preview window we opened."""
    try:
        actual_hwnd = int(getattr(control, "NativeWindowHandle", 0) or 0)
        actual_pid = int(getattr(control, "ProcessId", 0) or 0)
        return bool(
            hwnd
            and actual_hwnd == hwnd
            and str(getattr(control, "ClassName", "") or "")
            == "mmui::PreviewWindow"
            and (not expected_pid or actual_pid == int(expected_pid))
        )
    except Exception:
        return False


def close_preview_window_safely(
    preview,
    expected_pid: int | None = None,
    wait_timeout: float = 2.0,
) -> bool:
    """Close only the captured media preview HWND.

    A targeted ``WM_CLOSE`` is attempted first.  ``Ctrl+W`` is allowed only if
    that exact HWND is still alive, can be rebound with the same class/PID, and
    is confirmed as the foreground window.  This prevents a delayed cleanup
    from ever closing a chat, the main WeChat window, or an unrelated browser.
    """
    try:
        hwnd = int(getattr(preview, "NativeWindowHandle", 0) or 0)
    except Exception:
        return False
    if not _preview_identity_is_safe(preview, hwnd, expected_pid):
        wxlog.warning("引用媒体预览窗口身份校验失败，已取消关闭")
        return False

    if post_close_message(hwnd, {"mmui::PreviewWindow"}):
        deadline = time.monotonic() + max(0.0, wait_timeout)
        while time.monotonic() < deadline:
            if not is_window(hwnd):
                return True
            time.sleep(0.08)
    if not is_window(hwnd):
        return True

    # WM_CLOSE did not destroy the preview. Rebind and re-check identity before
    # the keyboard fallback; never send Ctrl+W to whichever window is current.
    try:
        current = uia.control_from_handle(hwnd)
    except Exception as exc:
        wxlog.warning(f"引用媒体预览窗口重新绑定失败，已取消 Ctrl+W: {exc}")
        return False
    if not _preview_identity_is_safe(current, hwnd, expected_pid):
        wxlog.warning("引用媒体预览窗口身份已变化，已取消 Ctrl+W")
        return False
    if not force_foreground(hwnd) or get_foreground_window() != hwnd:
        wxlog.warning("引用媒体预览窗口无法确认前台身份，已取消 Ctrl+W")
        return False
    try:
        current.SendKeys("{Ctrl}w", waitTime=0.5)
    except Exception as exc:
        wxlog.warning(f"引用媒体预览窗口 Ctrl+W 关闭失败: {exc}")
        return False

    deadline = time.monotonic() + max(0.5, wait_timeout)
    while time.monotonic() < deadline:
        if not is_window(hwnd):
            return True
        time.sleep(0.08)
    return not is_window(hwnd)


def _wait_for_media_preview(
    pid: int | None,
    deadline: float,
    retry_click: Callable[[], bool] | None = None,
    retry_after: float = 2.0,
    baseline_hwnds: set[int] | None = None,
):
    """Wait for the preview and optionally repeat one identity-safe click.

    WeChat occasionally accepts a click without creating ``PreviewWindow``.
    A quote caller may therefore supply ``retry_click``; it is invoked at most
    once and is responsible for rebinding and validating the exact quote row.
    Ordinary image/video downloads keep their existing single-click behavior.
    """
    baseline = {int(hwnd or 0) for hwnd in (baseline_hwnds or set()) if int(hwnd or 0)}
    started = time.monotonic()
    polls = 0
    query_errors = 0
    last_query_error = None
    pid_mismatches = 0
    last_candidates = []
    retry_result = "not_attempted" if retry_click else "not_configured"

    def report(outcome):
        media_event(
            "preview_wait", level="info" if outcome == "opened" else "warning",
            outcome=outcome, polls=polls, wait_ms=round((time.monotonic() - started) * 1000),
            budget_ms=round(max(0.0, deadline - started) * 1000),
            baseline_hwnds=sorted(baseline), candidates=last_candidates,
            query_errors=query_errors, last_query_error=last_query_error,
            pid_mismatches=pid_mismatches,
            retry_result=retry_result,
        )

    retry_at = time.monotonic() + max(0.3, retry_after)
    retried = retry_click is None
    while time.monotonic() < deadline:
        query_diagnostics = {}
        controls = uia.find_top_level_controls(
            "mmui::PreviewWindow",
            pid=pid,
            max_results=5,
            diagnostics=query_diagnostics,
        )
        polls += 1
        query_errors += query_diagnostics.get("property_errors", 0)
        pid_mismatches += query_diagnostics.get("pid_mismatches", 0)
        if query_diagnostics.get("enumeration_error"):
            query_errors += 1
        if query_diagnostics.get("last_error"):
            last_query_error = query_diagnostics["last_error"]
        last_candidates = []
        new_controls = []
        for control in controls:
            try:
                hwnd = int(getattr(control, "NativeWindowHandle", 0) or 0)
            except Exception as exc:
                last_candidates.append({"rejected": "unreadable_handle", "error": str(exc)[:240]})
                continue
            safe = bool(hwnd and hwnd not in baseline and _preview_identity_is_safe(control, hwnd, pid))
            last_candidates.append({"hwnd": hwnd, "baseline": hwnd in baseline, "identity_safe": safe})
            if safe:
                new_controls.append(control)
        if len(new_controls) == 1:
            report("opened")
            return new_controls[0]
        if len(new_controls) > 1:
            report("ambiguous")
            raise MediaIdentityError(
                f"点击后同时出现 {len(new_controls)} 个新媒体预览窗口，拒绝猜测"
            )
        now = time.monotonic()
        if not retried and now >= retry_at:
            retried = True
            try:
                if retry_click and retry_click():
                    retry_result = "clicked"
                    wxlog.info("引用媒体预览未出现，已对同一引用消息安全重试一次")
                else:
                    retry_result = "identity_unconfirmed"
                    wxlog.warning("引用媒体预览未出现，且无法重新确认同一引用消息")
            except Exception as exc:
                retry_result = f"{type(exc).__name__}: {exc}"[:240]
                wxlog.warning(f"引用媒体预览安全重试失败: {exc}")
        time.sleep(0.3)
    report("timeout")
    return None


def download_media_via_preview(
    message: "HumanMessage",
    dir_path: str | Path | None = None,
    timeout: int = 30,
    already_clicked: bool = False,
    retry_click: Callable[[], bool] | None = None,
    baseline_preview_hwnds: set[int] | None = None,
) -> Path:
    """左键单击媒体消息打开 PreviewWindow，然后高频右键复制路径。

    微信在打开预览窗口时开始下载媒体；加载完成前右键菜单里没有
    “复制”。因此循环：右键 -> 找“复制” -> 没有就左键关闭菜单重试。
    成功返回微信本地文件路径；若指定 dir_path 则复制到该目录。
    """
    pid = getattr(message.parent, "pid", None)
    media_event(
        "preview_baseline", download_timeout_sec=timeout,
        preview_timeout_sec=MEDIA_PREVIEW_OPEN_TIMEOUT_SEC,
        already_clicked=already_clicked, retry_enabled=retry_click is not None,
    )
    if baseline_preview_hwnds is None:
        baseline_preview_hwnds = set()
        for existing in uia.find_top_level_controls(
            "mmui::PreviewWindow",
            pid=pid,
            max_results=5,
        ):
            try:
                hwnd = int(getattr(existing, "NativeWindowHandle", 0) or 0)
            except Exception:
                hwnd = 0
            if hwnd:
                baseline_preview_hwnds.add(hwnd)
    if baseline_preview_hwnds:
        media_event("preview_baseline_rejected", level="warning", baseline_hwnds=sorted(baseline_preview_hwnds))
        raise MediaIdentityError(
            "媒体操作开始前已有预览窗口，无法证明新预览属于当前消息"
        )

    if not already_clicked:
        media_event("prepare_target")
        prepare = getattr(message, "prepare_for_media_action", None)
        if callable(prepare):
            prepare()
        prepared_point = getattr(message, "prepared_media_action_point", None)
        point = (
            prepared_point()
            if callable(prepared_point)
            else find_content_center(message.control) or message._bias()
        )
        if point is None:
            raise RuntimeError("无法定位媒体消息")
        prepared_click = getattr(message, "click_prepared_media", None)
        media_event(
            "dispatch_click", point=point,
            route="prepared_hwnd" if callable(prepared_click) else "screen",
            prepared_row_rect=getattr(message, "_prepared_media_row_rect", None),
        )
        if callable(prepared_click):
            prepared_click(point, right=False)
            media_event("click_returned", foreground=media_foreground_snapshot())
            time.sleep(0.6)
        else:
            uia.click_screen(point[0], point[1], wait=0.6)
            media_event("click_returned", foreground=media_foreground_snapshot())

    deadline = time.monotonic() + timeout
    # Creating PreviewWindow should be quick even when the media itself still
    # needs time to load.  Do not hold the global WeChat UI transaction for the
    # full download timeout when the click did not open any preview at all.
    preview_deadline = min(
        deadline,
        time.monotonic() + MEDIA_PREVIEW_OPEN_TIMEOUT_SEC,
    )
    preview = _wait_for_media_preview(
        pid,
        preview_deadline,
        retry_click=retry_click,
        baseline_hwnds=baseline_preview_hwnds,
    )
    if preview is None:
        media_event("preview_timeout_snapshot", level="warning", snapshot=media_ui_snapshot(message))
        raise RuntimeError("媒体预览窗口未打开")

    try:
        media_event("validate_preview_origin")
        validate_origin = getattr(message, "validate_dispatched_media_origin", None)
        if callable(validate_origin):
            validate_origin()
        preview_hwnd = int(getattr(preview, "NativeWindowHandle", 0) or 0)

        # WeChat 4.x exposes media copying through the preview toolbar's
        # “更多” popup. The popup is a top-level mmui::XMenu whose native owner
        # remains the preview HWND; it is not a descendant of PreviewWindow and
        # it never becomes the Win32 foreground window. Prefer that proven
        # route before retaining the older preview right-click fallback.
        more_deadline = min(
            deadline,
            time.monotonic() + min(10.0, max(2.0, (deadline - time.monotonic()) * 0.5)),
        )
        media_event("copy_more_menu", preview_hwnd=preview_hwnd)
        try:
            return _copy_media_from_preview_more_menu(
                message,
                preview,
                preview_hwnd=preview_hwnd,
                expected_pid=pid,
                deadline=more_deadline,
                dir_path=dir_path,
            )
        except MediaIdentityError:
            raise
        except Exception as exc:
            wxlog.warning(f"预览工具栏复制失败，回退预览右键: {exc}")
            media_event("copy_context_menu", level="warning", error_type=type(exc).__name__, error=str(exc))

        while time.monotonic() < deadline:
            if not _preview_identity_is_safe(preview, preview_hwnd, pid):
                raise MediaIdentityError("媒体预览窗口身份在下载期间发生变化")
            rect = preview.BoundingRectangle
            center_x = int(rect.left + (rect.right - rect.left) // 2)
            center_y = int(rect.top + (rect.bottom - rect.top) * 0.55)
            clear()
            uia.right_click_screen(center_x, center_y, wait=0.5)
            time.sleep(0.7)
            copy_items = uia.find_controls(
                preview,
                control_type="MenuItemControl",
                name="复制",
                max_results=5,
            )
            if copy_items:
                copy_items[0].Click(simulateMove=False, waitTime=0.5)
                time.sleep(0.7)
                paths = read_files()
                if paths:
                    src = Path(paths[0])
                    if src.exists():
                        if dir_path is None:
                            return src
                        target_dir = Path(dir_path)
                        target_dir.mkdir(parents=True, exist_ok=True)
                        target = target_dir / src.name
                        shutil.copy2(src, target)
                        return target
            # 左键点击预览区域关闭右键菜单，准备下一轮重试。
            try:
                uia.click_screen(center_x, center_y, wait=0.3)
            except Exception:
                pass
            time.sleep(0.4)
        raise RuntimeError("媒体加载完成前未能复制到路径")
    finally:
        if not close_preview_window_safely(preview, expected_pid=pid):
            wxlog.warning("引用/媒体预览窗口未确认关闭；未向其他窗口发送快捷键")


def _copy_media_from_preview_more_menu(
    message: "HumanMessage",
    preview,
    *,
    preview_hwnd: int,
    expected_pid: int | None,
    deadline: float,
    dir_path: str | Path | None = None,
) -> Path:
    """Copy one previewed media file through its owner-bound “更多” menu."""

    from mabowx.ui.component import Menu

    if not _preview_identity_is_safe(preview, preview_hwnd, expected_pid):
        raise MediaIdentityError("媒体预览窗口身份无法确认")
    more_buttons = uia.find_controls(
        preview,
        control_type="ButtonControl",
        name="更多",
        max_results=3,
        max_nodes=500,
    )
    visible_buttons = []
    for button in more_buttons:
        try:
            rect = button.BoundingRectangle
            if (
                button.Exists(0)
                and not bool(getattr(button, "IsOffscreen", False))
                and int(rect.right) > int(rect.left)
                and int(rect.bottom) > int(rect.top)
            ):
                visible_buttons.append(button)
        except Exception:
            continue
    if len(visible_buttons) != 1:
        raise RuntimeError(f"媒体预览“更多”按钮数量异常: {len(visible_buttons)}")

    more_button = visible_buttons[0]
    last_error = ""
    while time.monotonic() < deadline:
        if not _preview_identity_is_safe(preview, preview_hwnd, expected_pid):
            raise MediaIdentityError("媒体预览窗口身份在复制期间发生变化")
        if get_foreground_window() != preview_hwnd:
            if not force_foreground(preview_hwnd) or get_foreground_window() != preview_hwnd:
                raise MediaIdentityError("媒体预览窗口无法安全切到前台")

        try:
            rect = more_button.BoundingRectangle
            anchor = (
                (int(rect.left) + int(rect.right)) // 2,
                (int(rect.top) + int(rect.bottom)) // 2,
            )
        except Exception as exc:
            raise RuntimeError(f"无法定位媒体预览“更多”按钮: {exc}") from exc

        baseline_hwnds: set[int] = set()
        for existing in uia.find_top_level_controls(
            "mmui::XMenu",
            pid=expected_pid,
            max_results=10,
        ):
            try:
                hwnd = int(getattr(existing, "NativeWindowHandle", 0) or 0)
            except Exception:
                hwnd = 0
            if hwnd:
                baseline_hwnds.add(hwnd)

        clear()
        more_button.Click(simulateMove=False, waitTime=0.2)
        menu = Menu(
            message.parent,
            timeout=min(1.2, max(0.1, deadline - time.monotonic())),
            anchor=anchor,
            baseline_hwnds=baseline_hwnds,
            require_new=True,
            expected_owner_hwnd=preview_hwnd,
        )
        try:
            if not _preview_identity_is_safe(preview, preview_hwnd, expected_pid):
                raise MediaIdentityError("打开复制菜单后媒体预览身份发生变化")
            response = menu.select("复制")
            if not response.is_success:
                last_error = response["message"]
            else:
                clipboard_deadline = min(deadline, time.monotonic() + 1.5)
                while time.monotonic() < clipboard_deadline:
                    paths = read_files()
                    if paths:
                        src = Path(paths[0])
                        if src.exists():
                            if dir_path is None:
                                return src
                            target_dir = Path(dir_path)
                            target_dir.mkdir(parents=True, exist_ok=True)
                            target = target_dir / src.name
                            try:
                                if src.resolve() == target.resolve():
                                    return src
                            except Exception:
                                pass
                            shutil.copy2(src, target)
                            return target
                        last_error = f"路径不存在: {src}"
                        break
                    time.sleep(0.05)
                if not last_error:
                    last_error = "复制后剪贴板未出现文件路径"
        finally:
            try:
                menu.close()
            except Exception:
                pass
        time.sleep(0.2)

    raise RuntimeError(f"预览菜单获取媒体文件失败: {last_error or '超时'}")


def download_via_copy_menu(
    message: "HumanMessage",
    dir_path: str | Path | None = None,
    timeout: int = 30,
    max_attempts: int | None = None,
) -> Path:
    """右键媒体消息 -> 复制，从 CF_HDROP 剪贴板读取路径并复制文件。"""
    from mabowx.ui.component import Menu

    deadline = time.monotonic() + timeout
    last_error = ""
    attempts = 0
    while time.monotonic() < deadline and (
        max_attempts is None or attempts < max(1, int(max_attempts))
    ):
        attempts += 1
        prepare = getattr(message, "prepare_for_media_action", None)
        if callable(prepare):
            prepare()
        else:
            activate = getattr(message, "_activate_source_window", None)
            if callable(activate) and not activate():
                raise RuntimeError("媒体消息所属聊天窗口未能安全激活")
        clear()
        prepared_point = getattr(message, "prepared_media_action_point", None)
        strict_media_menu = callable(prepared_point)
        point = (
            prepared_point()
            if strict_media_menu
            else find_content_center(message.control) or message._bias()
        )
        if point is None:
            raise RuntimeError("无法定位媒体消息")
        baseline_hwnds: set[int] = set()
        if strict_media_menu:
            parent_pid = getattr(message.parent, "pid", None)
            try:
                existing_menus = uia.find_top_level_controls(
                    "mmui::XMenu",
                    pid=parent_pid,
                    max_results=10,
                )
            except Exception:
                existing_menus = []
            for existing in existing_menus:
                try:
                    hwnd = int(getattr(existing, "NativeWindowHandle", 0) or 0)
                except Exception:
                    hwnd = 0
                if hwnd:
                    baseline_hwnds.add(hwnd)
        prepared_click = getattr(message, "click_prepared_media", None)
        if callable(prepared_click):
            prepared_click(point, right=True)
            time.sleep(0.4)
        else:
            uia.right_click_screen(point[0], point[1], wait=0.4)
        time.sleep(0.2)
        menu_kwargs = (
            {
                "baseline_hwnds": baseline_hwnds,
                "require_new": True,
                "expected_owner_hwnd": int(
                    message._target_identity().chat_hwnd or 0
                ),
            }
            if strict_media_menu
            else {}
        )
        menu = Menu(
            message.parent,
            timeout=min(0.8, max(0.1, deadline - time.monotonic())),
            anchor=point,
            **menu_kwargs,
        )
        try:
            validate_origin = getattr(message, "validate_dispatched_media_origin", None)
            if callable(validate_origin):
                validate_origin()
            response = menu.select("复制")
        except Exception:
            try:
                menu.close()
            except Exception:
                pass
            raise
        if not response.is_success:
            last_error = response["message"]
            try:
                menu.close()
            except Exception:
                pass
            time.sleep(0.2)
            continue
        last_error = ""
        clipboard_deadline = min(deadline, time.monotonic() + 1.2)
        while time.monotonic() < clipboard_deadline:
            paths = read_files()
            if paths:
                src = Path(paths[0])
                if src.exists():
                    # 微信的“复制”已经把缓存文件的真实路径放入
                    # CF_HDROP。调用方没有指定目录时直接返回该路径，
                    # 与预览下载路径保持一致，也避免无意义的二次复制。
                    if dir_path is None:
                        return src
                    target_dir = Path(dir_path)
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target = target_dir / src.name
                    try:
                        if src.resolve() == target.resolve():
                            return src
                    except Exception:
                        pass
                    shutil.copy2(src, target)
                    return target
                last_error = f"路径不存在: {src}"
                break
            time.sleep(0.05)
        if not last_error:
            last_error = "复制后剪贴板未出现文件路径"
        time.sleep(0.2)
    raise RuntimeError(f"获取媒体文件失败: {last_error or '超时'}")


class TextMessage(HumanMessage):
    type = "text"


class VoiceMessage(HumanMessage):
    type = "voice"


QUOTE_PATTERN = re.compile(
    r"^(.*)\s*引用\s+(.+?)\s+的消息\s*[:：]\s*(.*)$",
    re.DOTALL,
)


class QuoteMessage(HumanMessage):
    type = "quote"

    def __init__(self, control=None, parent=None) -> None:
        super().__init__(control=control, parent=parent)
        raw = str(getattr(control, "Name", "") or "")
        match = QUOTE_PATTERN.match(raw)
        if match:
            self.content = match.group(1).strip()
            self.quote_nickname = match.group(2).strip()
            self.quote_content = match.group(3).strip()
        else:
            self.content = raw
            self.quote_nickname = ""
            self.quote_content = ""

    @staticmethod
    def _control_is_clickable(control) -> bool:
        if control is None:
            return False
        try:
            rect = control.BoundingRectangle
            if int(rect.right - rect.left) <= 0 or int(rect.bottom - rect.top) <= 0:
                return False
            return bool(control.Exists(0))
        except Exception:
            return False

    def _same_quote(self, candidate) -> bool:
        return bool(
            getattr(candidate, "type", "") == self.type
            and getattr(candidate, "content", "") == self.content
            and getattr(candidate, "quote_nickname", "") == self.quote_nickname
            and getattr(candidate, "quote_content", "") == self.quote_content
            and (
                not getattr(self, "sender", "")
                or not getattr(candidate, "sender", "")
                or getattr(candidate, "sender", "") == self.sender
            )
        )

    def _refresh_visible_control(self) -> bool:
        """Rebind a delayed quote download to the exact visible quote row.

        Message controls are virtualized by WeChat.  If the cached control is
        stale, bind only when the visible quote is unambiguous; choosing the
        latest similar quote could download the wrong image.
        """
        getter = getattr(self.parent, "get_messages", None)
        if not callable(getter):
            return self._control_is_clickable(self.control)
        try:
            candidates = [
                candidate
                for candidate in getter()
                if self._same_quote(candidate)
                and self._control_is_clickable(getattr(candidate, "control", None))
            ]
        except Exception as exc:
            wxlog.warning(f"刷新引用消息控件失败: {exc}")
            return False
        if not candidates:
            return False

        original_id = str(getattr(self, "id", "") or "")
        exact_id = [
            candidate
            for candidate in candidates
            if original_id and str(getattr(candidate, "id", "") or "") == original_id
        ]
        if len(exact_id) == 1:
            chosen = exact_id[0]
        elif len(candidates) == 1:
            chosen = candidates[0]
        else:
            wxlog.warning(
                "可见区存在多条无法区分的相同引用消息，已拒绝猜测引用图片: "
                f"count={len(candidates)} content={self.content[:60]!r}"
            )
            return False
        self.control = chosen.control
        self.parent = chosen.parent
        self.control_class_name = getattr(chosen, "control_class_name", "")
        self.direction = getattr(chosen, "direction", self.direction)
        return True

    def click_quote(self) -> bool:
        """点击引用消息，打开被引用图片/视频的预览窗口。"""
        if not self._refresh_visible_control():
            return False
        if not self._activate_source_window():
            raise RuntimeError("引用消息所属聊天窗口未能安全激活，已取消点击")
        point = find_quote_media_center(self.control, self.direction)
        source = "thumbnail"
        if point is None:
            point = quote_media_fallback_point(self.control, self.direction)
            source = "geometry"
        if point is None:
            return False
        wxlog.debug(
            f"点击引用媒体: source={source} direction={self.direction} point={point}"
        )
        uia.click_screen(point[0], point[1], wait=0.6)
        return True

    def download_quote_media(self, dir_path: str | Path | None = None, timeout: int = 30) -> Path:
        """下载引用消息中的图片/视频。"""
        return _run_ordered_media_operation(
            self,
            lambda: self._download_quote_image_unlocked(
                dir_path=dir_path,
                timeout=timeout,
            ),
        )

    def download_quote_image(self, dir_path: str | Path | None = None, timeout: int = 30) -> Path:
        """保留旧公开 API；底层与引用视频共用媒体下载路径。"""
        return self.download_quote_media(dir_path=dir_path, timeout=timeout)

    def _download_quote_image_unlocked(
        self,
        dir_path: str | Path | None = None,
        timeout: int = 30,
    ) -> Path:
        pid = getattr(self.parent, "pid", None)
        baseline: set[int] = set()
        for existing in uia.find_top_level_controls(
            "mmui::PreviewWindow",
            pid=pid,
            max_results=5,
        ):
            try:
                hwnd = int(getattr(existing, "NativeWindowHandle", 0) or 0)
            except Exception:
                hwnd = 0
            if hwnd:
                baseline.add(hwnd)
        if baseline:
            raise MediaIdentityError(
                "引用图片操作开始前已有预览窗口，拒绝猜测预览归属"
            )
        if not self.click_quote():
            raise RuntimeError("无法点击引用消息")
        return download_media_via_preview(
            self,
            dir_path=dir_path,
            timeout=timeout,
            already_clicked=True,
            retry_click=self.click_quote,
            baseline_preview_hwnds=baseline,
        )


class ImageMessage(HumanMessage):
    type = "image"

    def _target_identity(self):
        target = getattr(self, "media_target_identity", None)
        if target is not None:
            return target
        reason = str(getattr(self, "media_target_error", "") or "").strip()
        if reason:
            raise MediaIdentityError(f"接收时图片身份快照失败: {reason}")
        raise MediaIdentityError(
            "图片消息没有监听阶段生成的身份快照，拒绝对缓存 UIA 控件执行点击"
        )

    def _fresh_visible_messages(self) -> list:
        getter = getattr(self.parent, "get_messages", None)
        if not callable(getter):
            raise MediaIdentityError("图片所属聊天不支持重新枚举消息")
        try:
            return list(getter(resolve_group_senders=False) or [])
        except TypeError:
            return list(getter() or [])
        except Exception as exc:
            raise MediaIdentityError(f"重新枚举图片消息失败: {exc}") from exc

    def _bind_fresh_candidate(self):
        target = self._target_identity()
        current_chat = str(getattr(self.parent, "who", "") or "").strip()
        if target.chat_name and current_chat and current_chat != target.chat_name:
            raise MediaIdentityError(
                f"聊天窗口已从 {target.chat_name!r} 变为 {current_chat!r}"
            )
        visible = self._fresh_visible_messages()
        candidate = select_media_candidate(target, visible)
        self.control = candidate.control
        self.parent = candidate.parent
        self.control_class_name = getattr(candidate, "control_class_name", "")
        self.direction = getattr(candidate, "direction", self.direction)
        return candidate

    def prepare_for_media_action(self) -> bool:
        """Rebind and double-check the exact thumbnail immediately before click."""

        target = self._target_identity()
        candidate = self._bind_fresh_candidate()
        message_list = getattr(self.parent, "message_list", None)
        if not control_fully_visible(candidate.control, message_list):
            try:
                pattern = candidate.control.GetScrollItemPattern()
                pattern.ScrollIntoView()
            except Exception as exc:
                raise MediaIdentityError(f"目标图片未完整可见且无法安全滚入: {exc}") from exc
            time.sleep(0.12)
            candidate = self._bind_fresh_candidate()
            message_list = getattr(self.parent, "message_list", None)
            if not control_fully_visible(candidate.control, message_list):
                raise MediaIdentityError("目标图片滚动后仍未完整位于消息视口")

        if not self._activate_source_window():
            raise MediaIdentityError("目标图片所属聊天窗口未能安全切到前台")

        # Bringing the chat to the foreground can rebuild RecyclerListView.
        # Enumerate once more, then require two stable visual/geometry samples.
        candidate = self._bind_fresh_candidate()
        message_list = getattr(self.parent, "message_list", None)
        if not control_fully_visible(candidate.control, message_list):
            raise MediaIdentityError("窗口激活后目标图片离开了完整可见区域")
        visual = verify_stable_candidate(target, candidate)
        if visual.rect is None:
            raise MediaIdentityError("点击前没有取得缩略图的绝对矩形")
        try:
            row = candidate.control.BoundingRectangle
            row_rect = (
                int(row.left),
                int(row.top),
                int(row.right),
                int(row.bottom),
            )
        except Exception as exc:
            raise MediaIdentityError(f"点击前无法读取图片消息矩形: {exc}") from exc
        media_rect = tuple(int(value) for value in visual.rect)
        if not (
            row_rect[0] <= media_rect[0] < media_rect[2] <= row_rect[2]
            and row_rect[1] <= media_rect[1] < media_rect[3] <= row_rect[3]
        ):
            raise MediaIdentityError("缩略图矩形已离开目标消息行")
        self.control = candidate.control
        self._prepared_media_point = (
            (media_rect[0] + media_rect[2]) // 2,
            (media_rect[1] + media_rect[3]) // 2,
        )
        self._prepared_media_row_rect = row_rect
        self._prepared_media_at = time.monotonic()
        return True

    def prepared_media_action_point(self) -> tuple[int, int]:
        """Consume the just-validated thumbnail point unless its row moved."""

        target = self._target_identity()
        point = getattr(self, "_prepared_media_point", None)
        expected_row = getattr(self, "_prepared_media_row_rect", None)
        prepared_at = float(getattr(self, "_prepared_media_at", 0.0) or 0.0)
        if (
            point is None
            or expected_row is None
            or time.monotonic() - prepared_at > PREPARED_MEDIA_CREDENTIAL_TTL_SEC
        ):
            raise MediaIdentityError("缩略图点击凭据不存在或已经过期")
        try:
            if not self.control.Exists(0):
                raise MediaIdentityError("缩略图控件在点击前已失效")
            row = self.control.BoundingRectangle
            current_row = (
                int(row.left),
                int(row.top),
                int(row.right),
                int(row.bottom),
            )
            top = self.control.GetTopLevelControl()
            current_hwnd = int(getattr(top, "NativeWindowHandle", 0) or 0)
        except MediaIdentityError:
            raise
        except Exception as exc:
            raise MediaIdentityError(f"点击前最终布局校验失败: {exc}") from exc
        if current_row != expected_row:
            delta = tuple(
                current - expected
                for current, expected in zip(current_row, expected_row)
            )
            raise MediaIdentityError(
                "缩略图消息行在校验后发生滚动或重排: "
                f"expected={expected_row} current={current_row} delta={delta}"
            )
        if target.chat_hwnd and current_hwnd != target.chat_hwnd:
            raise MediaIdentityError("缩略图控件在点击前离开原聊天窗口")
        if target.chat_hwnd and get_foreground_window() != target.chat_hwnd:
            raise MediaIdentityError("原聊天窗口在点击前失去前台焦点")
        if not (
            current_row[0] <= point[0] < current_row[2]
            and current_row[1] <= point[1] < current_row[3]
        ):
            raise MediaIdentityError("缩略图点击点已离开目标消息行")
        return int(point[0]), int(point[1])

    def validate_dispatched_media_origin(self) -> bool:
        """Confirm the exact menu/preview still originates from the target row."""

        target = self._target_identity()
        point = getattr(self, "_prepared_media_point", None)
        expected_row = getattr(self, "_prepared_media_row_rect", None)
        if point is None or expected_row is None:
            raise MediaIdentityError("点击投递后缺少原消息几何凭据")
        try:
            if not self.control.Exists(0):
                raise MediaIdentityError("点击投递后原缩略图控件已失效")
            row = self.control.BoundingRectangle
            current_row = (
                int(row.left),
                int(row.top),
                int(row.right),
                int(row.bottom),
            )
        except MediaIdentityError:
            raise
        except Exception as exc:
            raise MediaIdentityError(f"点击投递后无法校验原消息矩形: {exc}") from exc
        if current_row != expected_row:
            delta = tuple(
                current - expected
                for current, expected in zip(current_row, expected_row)
            )
            raise MediaIdentityError(
                "点击投递后原消息行发生滚动或重排: "
                f"expected={expected_row} current={current_row} delta={delta}"
            )
        visual = verify_dispatched_candidate(target, self)
        media_rect = visual.rect
        if media_rect is None or not (
            media_rect[0] <= int(point[0]) < media_rect[2]
            and media_rect[1] <= int(point[1]) < media_rect[3]
        ):
            raise MediaIdentityError("点击投递后原点击点不再属于目标缩略图")
        return True

    def click_prepared_media(self, point: tuple[int, int], *, right: bool) -> None:
        """Dispatch the validated click only to the captured chat HWND."""

        verified_point = self.prepared_media_action_point()
        normalized_point = int(point[0]), int(point[1])
        if normalized_point != verified_point:
            raise MediaIdentityError("缩略图点击点在投递前发生变化")
        target = self._target_identity()
        if not target.chat_hwnd:
            raise MediaIdentityError("缩略图目标缺少可定向投递的聊天 HWND")
        poster = post_right_click if right else post_left_click
        if not poster(target.chat_hwnd, verified_point[0], verified_point[1]):
            raise MediaIdentityError("无法把缩略图点击定向投递给原聊天窗口")

    def _verify_downloaded_path(self, path: str | Path) -> Path:
        target = self._target_identity()
        target_visual = target.visual
        self._last_media_candidate_path = str(path)
        try:
            result = compare_target_to_file(target, path)
        except Exception as exc:
            raise MediaIdentityError(f"下载图片无法完成身份校验: {exc}") from exc
        self._last_media_verification = {
            "matched": result.matched,
            "phash_distance": result.phash_distance,
            "color_distance": result.color_distance,
            "detail_distance": result.detail_distance,
            "variant": result.variant,
            "match_rule": result.match_rule,
            "match_profile": FILE_MATCH_PROFILE,
            "match_thresholds": FILE_MATCH_THRESHOLDS,
            "delivery_id": target.delivery_id,
            "raw_message_id": target.raw_message_id,
            "route": str(getattr(self, "_last_media_route", "") or ""),
            "target_width": getattr(target_visual, "width", None),
            "target_height": getattr(target_visual, "height", None),
            "target_aspect_ratio": getattr(target_visual, "aspect_ratio", None),
            "target_variance": getattr(target_visual, "variance", None),
            "target_rect": getattr(target_visual, "rect", None),
            "phash_threshold": FILE_VISUAL_MAX_DISTANCE,
            "color_threshold": FILE_COLOR_MAX_DISTANCE,
            "detail_threshold": FILE_DETAIL_MAX_DISTANCE,
            "variant_metrics": [
                {
                    "variant": name,
                    "phash_distance": phash_distance,
                    "color_distance": color_distance,
                    "detail_distance": detail_distance,
                }
                for name, phash_distance, color_distance, detail_distance
                in result.variant_metrics
            ],
        }
        if not result.matched:
            raise MediaFileMismatchError(
                "下载结果与接收时缩略图不一致，拒绝绑定: "
                f"phash_distance={result.phash_distance} "
                f"color_distance={result.color_distance} "
                f"detail_distance={result.detail_distance} variant={result.variant}"
            )
        return Path(path)

    def download(self, dir_path: str | Path | None = None, timeout: int = 30) -> Path:
        """One quick thumbnail copy, then a newly identified preview window."""
        return _run_ordered_media_operation(
            self,
            lambda: self._download_unlocked(dir_path=dir_path, timeout=timeout),
        )

    def _download_unlocked(
        self,
        dir_path: str | Path | None = None,
        timeout: int = 30,
    ) -> Path:
        self._last_media_candidate_path = ""
        self._last_media_verification = {}
        # In WeChat 4.x the thumbnail context menu's “复制” action places image
        # pixels on the clipboard, not a CF_HDROP file path. The supported file
        # route is the newly opened preview's owner-bound “更多 → 复制” menu.
        self._last_media_route = "preview"
        path = download_media_via_preview(
            self,
            dir_path=dir_path,
            timeout=max(1.0, float(timeout)),
        )
        media_event("verify_downloaded_file")
        return self._verify_downloaded_path(path)


class VideoMessage(HumanMessage):
    type = "video"

    def download(self, dir_path: str | Path | None = None, timeout: int = 30) -> Path:
        """单击视频打开预览窗口，点“保存”后从剪贴板获取微信本地路径。"""
        return _run_ordered_media_operation(
            self,
            lambda: download_media_via_preview(
                self,
                dir_path=dir_path,
                timeout=timeout,
            ),
        )


class FileMessage(HumanMessage):
    type = "file"

    def _refresh_visible_control(self, chat_name: str = "") -> bool:
        """窗口/消息 UIA 重绘后重新绑定同一张可见文件卡片。"""
        original_content = str(getattr(self, "content", "") or "").strip()
        original_direction = str(getattr(self, "direction", "") or "").strip()

        def bind_from(chatbox) -> bool:
            getter = getattr(chatbox, "get_messages", None)
            if not callable(getter):
                return False
            try:
                messages = getter() or []
            except Exception:
                return False
            for candidate in reversed(messages):
                if str(getattr(candidate, "type", "") or "") != self.type:
                    continue
                if str(getattr(candidate, "content", "") or "").strip() != original_content:
                    continue
                candidate_direction = str(
                    getattr(candidate, "direction", "") or ""
                ).strip()
                if (
                    original_direction
                    and candidate_direction
                    and candidate_direction != original_direction
                ):
                    continue
                control = getattr(candidate, "control", None)
                if find_content_center(control) is None:
                    continue
                self.control = control
                self.parent = getattr(candidate, "parent", None) or chatbox
                return True
            return False

        current_parent = getattr(self, "parent", None)
        if bind_from(current_parent):
            return True

        # 右键菜单消失时微信可能重建独立聊天窗口。旧消息仍指向旧 ChatBox，
        # 因此从 WeChatSubWnd.root 回到主窗口，按进入下载前保存的聊天名
        # 强制枚举新子窗口，再在新 ChatBox 中精确匹配文件名和消息方向。
        sub_root = getattr(current_parent, "root", None)
        main_root = getattr(sub_root, "root", None)
        get_sub_wnd = getattr(main_root, "get_sub_wnd", None)
        if not callable(get_sub_wnd):
            return False
        target_chat = str(chat_name or "").strip()
        if not target_chat:
            try:
                target_chat = str(getattr(current_parent, "who", "") or "").strip()
            except Exception:
                target_chat = ""
        if not target_chat:
            return False
        try:
            fresh_chat = get_sub_wnd(target_chat, force_refresh=True)
            fresh_core = getattr(fresh_chat, "core", None)
            if fresh_core is None or not fresh_core.exists():
                return False
            return bind_from(fresh_core.get_chatbox())
        except Exception:
            return False

    def _copy_path_to_clipboard(self, timeout: float = 10.0) -> str | None:
        """右键文件卡片 -> 复制，从 CF_HDROP 剪贴板读取微信本地路径。"""
        from mabowx.ui.component import Menu

        deadline = time.monotonic() + timeout
        started_at = time.monotonic()
        last_error = ""
        attempt = 0
        try:
            chat_name = str(getattr(self.parent, "who", "") or "").strip()
        except Exception:
            chat_name = ""
        while time.monotonic() < deadline:
            attempt += 1
            # 防止“复制”未真正更新剪贴板时误读上一次文件操作留下的
            # CF_HDROP，从而把错误文件归档到当前消息。
            clear()
            # 文件消息的 ListItem 覆盖整行。截图算法会把时间标签、滚动条
            # 等离散像素也算入 bbox，真机上曾得到整行中心的空白坐标，右键
            # 因而完全不弹菜单。消息方向偏移点稳定落在实际文件卡片内，应
            # 优先使用；只有方向暂不可判定时才用截图中心兜底。
            point = self._bias() or find_content_center(self.control)
            if point is None:
                if self._refresh_visible_control(chat_name):
                    point = self._bias() or find_content_center(self.control)
                    if point is not None:
                        wxlog.debug(
                            f"文件消息控件已重新绑定: attempt={attempt} chat={chat_name!r}"
                        )
            if point is None:
                last_error = "文件消息控件暂时不可用"
                wxlog.debug(
                    f"文件消息暂时无法定位，等待窗口重绑: attempt={attempt} "
                    f"chat={chat_name!r}"
                )
                time.sleep(0.25)
                continue
            uia.right_click_screen(point[0], point[1], wait=0.4)
            time.sleep(0.2)
            menu = Menu(
                self.parent,
                timeout=min(0.8, max(0.1, deadline - time.monotonic())),
                anchor=point,
            )
            response = menu.select("复制")
            if not response.is_success:
                last_error = response["message"]
                wxlog.debug(
                    f"文件菜单暂不能复制: attempt={attempt} error={last_error}"
                )
                # “复制”在文件尚未下载完成时可能不存在。必须先关闭本轮
                # 菜单再重试，否则下一次右键可能落在旧菜单上。
                try:
                    menu.close()
                except Exception:
                    pass
                time.sleep(0.2)
                continue

            # 微信写入 CF_HDROP 可能略晚于菜单关闭；短轮询比固定等待更快，
            # 同时不会把剪贴板尚未就绪误判为失败。
            last_error = ""
            clipboard_deadline = min(deadline, time.monotonic() + 1.2)
            while time.monotonic() < clipboard_deadline:
                paths = read_files()
                if paths:
                    candidate = Path(paths[0])
                    if candidate.exists():
                        wxlog.info(
                            "文件菜单复制成功: "
                            f"attempt={attempt} elapsed={time.monotonic() - started_at:.3f}s "
                            f"name={candidate.name!r}"
                        )
                        return str(candidate)
                    last_error = f"路径不存在: {candidate}"
                time.sleep(0.08)
            if not last_error:
                last_error = "复制后剪贴板未出现文件路径"
            time.sleep(0.15)

        wxlog.warning(
            "文件菜单复制超时: "
            f"attempts={attempt} elapsed={time.monotonic() - started_at:.3f}s "
            f"last_error={last_error or '未知'}"
        )
        return None

    @uilock
    def download(self, dir_path: str | Path | None = None, force_click: bool = False, timeout: int = 30) -> Path:
        """下载文件消息并在同一 UI 事务内恢复文本剪贴板。"""
        if not self.exists():
            raise RuntimeError("文件消息已失效")
        try:
            original_clipboard = get_text()
        except Exception:
            original_clipboard = ""
        try:
            source = self._copy_path_to_clipboard(timeout=timeout)
        finally:
            try:
                set_text(original_clipboard)
            except Exception as exc:
                wxlog.warning(f"恢复文件下载前的文本剪贴板失败: {exc}")
        if not source:
            raise RuntimeError("获取文件路径失败，请确认微信已开启文件自动下载")
        src = Path(source)
        if not src.exists():
            raise RuntimeError(f"微信文件路径不存在: {src}")
        target_dir = Path(dir_path) if dir_path else Path(WxParam.DEFAULT_SAVE_PATH)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / src.name
        try:
            shutil.copy2(src, target)
        except Exception as exc:
            raise RuntimeError(f"复制文件失败: {exc}") from exc
        return target


class LocationMessage(HumanMessage):
    type = "location"


class LinkMessage(HumanMessage):
    type = "link"


class CardMessage(HumanMessage):
    """链接/公众号卡片消息。

    微信 4.1.12 中，普通链接卡片和公众号卡片使用相同的 UIA 结构。
    对外类型对齐为 ``link``，Mabobot 的 internal API 会据此发布
    ``LINK_MESSAGE_RECEIVED`` 事件，summary_plus 才能处理。
    """

    type = "link"
    mtype = "link"

    @staticmethod
    def _control_is_clickable(control) -> bool:
        if control is None:
            return False
        try:
            rect = control.BoundingRectangle
            if int(rect.right - rect.left) <= 0 or int(rect.bottom - rect.top) <= 0:
                return False
            return bool(control.Exists(0))
        except Exception:
            return False

    def _refresh_visible_control(self) -> bool:
        """按内容重新绑定当前可见卡片，避免持久缓存失效 UIA 对象。

        微信 4.x 的消息列表会虚拟化并复用 RuntimeId。卡片收到后若又有
        新消息进入，回调时保存的 UIA control 可能已变成 0x0，甚至一次
        ``Exists`` 调用就阻塞十几秒。执行动作前必须从当前可见消息重新
        找到同一张卡片，不能继续点击旧对象。
        """
        getter = getattr(self.parent, "get_messages", None)
        if not callable(getter):
            # 兼容手工构造的消息对象；正常监听消息都有 ChatBox parent。
            return self.exists()
        try:
            messages = getter()
        except Exception as exc:
            wxlog.warning(f"刷新链接卡片控件失败: {exc}")
            return False
        for candidate in reversed(messages):
            if (
                getattr(candidate, "type", "") == self.type
                and getattr(candidate, "content", "") == self.content
                and self._control_is_clickable(getattr(candidate, "control", None))
            ):
                self.control = candidate.control
                self.parent = candidate.parent
                self.control_class_name = getattr(candidate, "control_class_name", "")
                return True
        return False

    def _visible_click_point(self) -> tuple[int, int] | None:
        """把方向点击点约束到消息列表与卡片行的真实可见交集。"""
        point = self._bias()
        if point is None:
            return None
        try:
            row = self.control.BoundingRectangle
            message_list = getattr(self.parent, "message_list", None)
            if message_list is None:
                return point
            viewport = message_list.BoundingRectangle
            left = max(int(row.left), int(viewport.left))
            top = max(int(row.top), int(viewport.top))
            right = min(int(row.right), int(viewport.right))
            bottom = min(int(row.bottom), int(viewport.bottom))
            if right <= left or bottom <= top:
                return None
            margin_x = min(6, max(0, (right - left - 1) // 3))
            margin_y = min(6, max(0, (bottom - top - 1) // 3))
            return (
                min(max(int(point[0]), left + margin_x), right - 1 - margin_x),
                min(max(int(point[1]), top + margin_y), bottom - 1 - margin_y),
            )
        except Exception:
            return point

    @uilock
    def _click_visible_card(self) -> None:
        if not self._activate_source_window():
            raise RuntimeError("卡片所属聊天窗口未能安全激活")
        point = self._visible_click_point()
        if point is None:
            raise RuntimeError("卡片当前没有可点击的可见区域")
        uia.click_screen(point[0], point[1], wait=0.5)

    @uilock
    def get_url(self, timeout: float = 15.0) -> str:
        """点击卡片，从微信内置浏览器复制并返回 URL。"""
        if not self._refresh_visible_control():
            raise RuntimeError("卡片消息当前不可见或已失效")
        from mabowx.ui.component import WeChatBrowser

        original_clipboard = get_text()
        browser = None
        try:
            # 可见消息列表会保留上下边缘被裁切的 ListItem。直接点击这种
            # 控件时，方向偏移点可能落到标题栏/窗口外而没有任何效果。
            # 先滚到完整可见位置，再重新绑定一次被微信虚拟化重绘的控件。
            if self.roll_into_view():
                time.sleep(0.15)
            if not self._refresh_visible_control():
                raise RuntimeError("卡片滚动后控件已失效")
            self._click_visible_card()
            browser = WeChatBrowser(timeout=timeout)
            if not browser.exists():
                raise RuntimeError("微信内置浏览器未打开")
            response = browser.copy_url(timeout=timeout)
        finally:
            if browser is not None:
                close_response = browser.close()
                if not close_response.is_success:
                    wxlog.warning(f"微信内置浏览器清理未完成: {close_response['message']}")
            try:
                set_text(original_clipboard)
            except Exception as exc:
                wxlog.warning(f"恢复链接解析前的文本剪贴板失败: {exc}")
        if not response.is_success:
            raise RuntimeError(response["message"])
        return str(response["data"]["url"])


class EmotionMessage(HumanMessage):
    type = "emotion"


class MergeMessage(HumanMessage):
    type = "merge"


class PersonalCardMessage(HumanMessage):
    type = "personal_card"


class NoteMessage(HumanMessage):
    type = "note"


class MiniAppMessage(HumanMessage):
    type = "miniapp"


class OtherMessage(HumanMessage):
    type = "other"
