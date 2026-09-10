"""聊天输入框与发送。"""

from __future__ import annotations

import difflib
import hashlib
import math
import threading
import time
from pathlib import Path
from typing import Any

from mabowx.core import uia
from mabowx.core.selectors import runtime_selector
from mabowx.core.clipboard import set_files, set_text
from mabowx.core.locks import ui_transaction, uilock
from mabowx.core.operation_sequencer import OrderedOperationSequencer
from mabowx.core.win32 import enum_windows_by_pid, get_window_owner, post_right_click, post_middle_click, scroll_window, force_foreground, get_foreground_window
from mabowx.logger import wxlog
from mabowx.msgs import classify, make_message, parse_content
from mabowx.msgs.identity import attach_delivery_context
from mabowx.utils.tools import detect_message_direction
from mabowx.param import WxParam, WxResponse

from .base import BaseUISubWnd, BaseUIWnd


GROUP_SENDER_HEAD_CLASS = "mmui::ContactHeadView"
GROUP_SENDER_FOCUS_TIMEOUT_SEC = 0.22
AVATAR_MENU_NATIVE_CLASSES = frozenset(
    {"mmui::XMenu", "Qt51514QWindowToolSaveBits"}
)
MENTION_OBJECT_CHARACTER = "\ufffc"


def group_sender_head_point(control, direction: str | None) -> tuple[int, int] | None:
    """返回头像探测点（屏幕绝对坐标）。"""
    if direction not in {"friend", "self"}:
        return None
    try:
        rect = control.BoundingRectangle
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
    except Exception:
        return None
    if width < 110 or height < 35:
        return None
    # 头像命中点使用消息行左/右上角固定 (51, 30) 偏移。仅在异常窄行
    # 中向内收缩，正常微信消息行保持同一命中位置。
    horizontal = min(int(WxParam.DEFAULT_MESSAGE_XBIAS), max(20, width // 8))
    vertical = min(int(WxParam.DEFAULT_MESSAGE_YBIAS), height - 2)
    x = int(rect.left) + horizontal
    if direction == "self":
        x = int(rect.right) - horizontal
    return x, int(rect.top) + vertical


def group_sender_from_focused_control(
    message_control,
    focused_control,
    direction: str | None,
) -> str:
    """校验隐藏头像焦点控件并返回群成员显示名。

    微信 4.1.x 的消息 ``ListItem`` 没有 UIA 子节点；在头像处右键后，
    ``GetFocusedControl`` 会临时返回 ``ContactHeadView``，其 ``Name``
    就是群里显示的发送者。这里同时校验类名、顶层 HWND、矩形包含关系
    和左右方向，避免把其他窗口或消息菜单的焦点误认成发送者。
    """
    matched, name = message_avatar_from_focused_control(
        message_control,
        focused_control,
        direction,
    )
    return name if matched and name else ""


def message_avatar_from_focused_control(
    message_control,
    focused_control,
    direction: str | None,
) -> tuple[bool, str]:
    """校验一次头像焦点命中，并返回 ``(命中, 显示名)``。

    方向识别只需要确认真实的 ``ContactHeadView`` 位于消息对应一侧；
    群发送者解析则额外使用其 ``Name``。自己的头像在少数微信状态下可能
    暂时没有 Name，因此 Name 为空不能否定一个几何和窗口身份都有效的
    方向命中。
    """
    if message_control is None or focused_control is None:
        return False, ""
    if direction not in {"friend", "self"}:
        return False, ""
    try:
        if str(getattr(focused_control, "ClassName", "") or "") != GROUP_SENDER_HEAD_CLASS:
            return False, ""
        if str(getattr(focused_control, "ControlTypeName", "") or "") != "ButtonControl":
            return False, ""
        name = str(getattr(focused_control, "Name", "") or "").strip()

        message_rect = message_control.BoundingRectangle
        head_rect = focused_control.BoundingRectangle
        message_left = int(message_rect.left)
        message_top = int(message_rect.top)
        message_right = int(message_rect.right)
        message_bottom = int(message_rect.bottom)
        head_left = int(head_rect.left)
        head_top = int(head_rect.top)
        head_right = int(head_rect.right)
        head_bottom = int(head_rect.bottom)
        if head_right <= head_left or head_bottom <= head_top:
            return False, ""
        margin = 4
        if (
            head_left < message_left - margin
            or head_right > message_right + margin
            or head_top < message_top - margin
            or head_bottom > message_bottom + margin
        ):
            return False, ""
        width = message_right - message_left
        head_center = (head_left + head_right) / 2
        side_limit = min(240, max(110, int(width * 0.28)))
        if direction == "friend" and head_center > message_left + side_limit:
            return False, ""
        if direction == "self" and head_center < message_right - side_limit:
            return False, ""

        try:
            message_top_control = message_control.GetTopLevelControl()
            head_top_control = focused_control.GetTopLevelControl()
            message_hwnd = int(getattr(message_top_control, "NativeWindowHandle", 0) or 0)
            head_hwnd = int(getattr(head_top_control, "NativeWindowHandle", 0) or 0)
            if message_hwnd and head_hwnd and message_hwnd != head_hwnd:
                return False, ""
        except Exception:
            # 某些测试替身和旧 UIA Provider 没有顶层句柄；矩形/类名校验
            # 仍足以使用，真机对象则会走上面的严格分支。
            pass
        return True, name
    except Exception:
        return False, ""


def _message_signature_content(message) -> str:
    content = str(getattr(message, "content", "") or "")
    if getattr(message, "type", "") == "quote":
        nickname = str(getattr(message, "quote_nickname", "") or "")
        quoted = str(getattr(message, "quote_content", "") or "")
        return f"{content}\x1f{nickname}\x1f{quoted}"
    return content


def message_signature(message) -> str:
    return (
        f"{message.type}|{_message_signature_content(message)}|"
        f"{message.sender}|{message.time}|{message.attr}"
    )


def message_anchor_signature(message) -> str:
    """用于 UIA 尾锚点的稳定签名，不包含可能延迟变化的时间/发送者。"""
    return f"{message.type}|{_message_signature_content(message)}|{message.attr}"


def control_anchor_token(control) -> tuple[str, str]:
    """生成无需截图/发送者识别的轻量 UIA 锚点 token。

    RuntimeId 用于同一轮 UIA 生命周期内的精确匹配；类名与原始 Name
    用于控件虚拟化后 RuntimeId 改变时的内容匹配。扫描历史页只读取这
    三个 UIA 属性，避免逐条截图判断方向拖慢翻页。
    """
    try:
        runtime_id = "-".join(str(part) for part in control.GetRuntimeId())
    except Exception:
        runtime_id = ""
    class_name = str(getattr(control, "ClassName", "") or "")
    raw_name = str(getattr(control, "Name", "") or "")
    signature = f"{class_name}\x1f{raw_name}"
    if not runtime_id:
        try:
            rect = control.BoundingRectangle
            runtime_id = (
                f"rect:{int(rect.left)}:{int(rect.top)}:"
                f"{int(rect.right)}:{int(rect.bottom)}"
            )
        except Exception:
            runtime_id = ""
    return runtime_id, signature


def find_control_snapshot_overlap(
    current_snapshot: tuple[tuple[str, str], ...] | list[tuple[str, str]],
    previous_snapshot: tuple[tuple[str, str], ...] | list[tuple[str, str]],
) -> tuple[int, int, str] | None:
    """在历史页中寻找上一轮可见列表的可靠重叠项。

    返回 ``(previous_index, current_index, mode)``。先匹配完整 token；
    RuntimeId 被微信虚拟化重建后，要求连续两个原始签名相同，或单个签名
    在两侧都唯一。这样既允许“任意重叠消息”恢复，也避免连续的 ``?``、
    动画表情等同内容消息产生模糊命中。
    """
    current = [tuple(token) for token in current_snapshot]
    previous = [tuple(token) for token in previous_snapshot]
    if not current or not previous:
        return None

    for previous_index in range(len(previous) - 1, -1, -1):
        token = previous[previous_index]
        if not token[0]:
            continue
        for current_index in range(len(current) - 1, -1, -1):
            if current[current_index] == token:
                return previous_index, current_index, "runtime_id"

    # 两条相邻消息的原始签名序列足以消除绝大多数重复内容歧义。
    for previous_index in range(len(previous) - 1, 0, -1):
        pair = (previous[previous_index - 1][1], previous[previous_index][1])
        if not all(pair):
            continue
        for current_index in range(len(current) - 1, 0, -1):
            if pair == (
                current[current_index - 1][1],
                current[current_index][1],
            ):
                return previous_index, current_index, "signature_pair"

    previous_counts: dict[str, int] = {}
    current_counts: dict[str, int] = {}
    for _runtime_id, signature in previous:
        previous_counts[signature] = previous_counts.get(signature, 0) + 1
    for _runtime_id, signature in current:
        current_counts[signature] = current_counts.get(signature, 0) + 1
    for previous_index in range(len(previous) - 1, -1, -1):
        signature = previous[previous_index][1]
        if not signature or previous_counts.get(signature) != 1:
            continue
        if current_counts.get(signature) != 1:
            continue
        for current_index in range(len(current) - 1, -1, -1):
            if current[current_index][1] == signature:
                return previous_index, current_index, "unique_signature"
    return None


def _message_control_token(message) -> tuple[str, str]:
    token = getattr(message, "ui_anchor_token", None)
    if token is not None:
        return tuple(token)
    return (
        str(getattr(message, "id", "") or ""),
        message_anchor_signature(message),
    )


def messages_after_control_tail(messages: list, previous_snapshot) -> tuple[list, bool]:
    """Match the delivered tail without depending on its current avatar visibility.

    Only the previous tail proves the delivery boundary. An earlier overlapping
    row still needs the normal recovery path to avoid replaying delivered rows.
    """
    overlap = find_control_snapshot_overlap(
        tuple(_message_control_token(message) for message in messages),
        previous_snapshot,
    )
    if overlap is None or overlap[0] != len(previous_snapshot) - 1:
        return [], False
    return list(messages[overlap[1] + 1:]), True


def confirmed_append_prefix(previous_snapshot, current_snapshot) -> int:
    """Return an unchanged old prefix length, or zero to require a full read.

    This optimization is deliberately stricter than delivery deduplication:
    exact ordered suffix/prefix overlap, two unambiguous anchors, real runtime
    IDs, and no reuse of a previous row ID in the appended portion. Content-only
    matching and history recovery must never authorize skipping identification.
    """
    previous = tuple(tuple(token) for token in previous_snapshot)
    current = tuple(tuple(token) for token in current_snapshot)
    if len(previous) < 2 or len(current) < 3:
        return 0
    previous_ids = [token[0] for token in previous]
    current_ids = [token[0] for token in current]
    if any(not rid or rid.startswith("rect:") for rid in (*previous_ids, *current_ids)):
        return 0
    if len(set(previous_ids)) != len(previous_ids) or len(set(current_ids)) != len(current_ids):
        return 0
    try:
        start = previous.index(current[0])
    except ValueError:
        return 0
    overlap = previous[start:]
    boundary = len(overlap)
    if boundary < 2 or boundary >= len(current) or current[:boundary] != overlap:
        return 0
    if set(previous_ids).intersection(current_ids[boundary:]):
        return 0
    # Identical text/media placeholders cannot serve as unique boundary proof.
    for _rid, signature in overlap[-2:]:
        if (not signature or sum(token[1] == signature for token in previous) != 1
                or sum(token[1] == signature for token in current) != 1):
            return 0
    return boundary


def message_page_overlap_length(previous_page: list, current_page: list) -> int:
    """返回相邻历史页的最长“前页后缀 / 后页前缀”重叠长度。"""
    previous_tokens = [_message_control_token(message) for message in previous_page]
    current_tokens = [_message_control_token(message) for message in current_page]
    limit = min(len(previous_tokens), len(current_tokens))
    for length in range(limit, 0, -1):
        if previous_tokens[-length:] == current_tokens[:length]:
            return length

    previous_signatures = [token[1] for token in previous_tokens]
    current_signatures = [token[1] for token in current_tokens]
    for length in range(limit, 1, -1):
        if previous_signatures[-length:] == current_signatures[:length]:
            return length

    if previous_signatures and current_signatures:
        boundary = previous_signatures[-1]
        if (
            boundary
            and boundary == current_signatures[0]
            and previous_signatures.count(boundary) == 1
            and current_signatures.count(boundary) == 1
        ):
            return 1
    return 0


def merge_overlapping_message_pages(pages: list[list]) -> tuple[list, bool]:
    """按时间顺序合并相邻可见页；任何页间无重叠都保守失败。"""
    nonempty = [list(page) for page in pages if page]
    if not nonempty:
        return [], True
    merged = list(nonempty[0])
    for page in nonempty[1:]:
        overlap = message_page_overlap_length(merged, page)
        if overlap <= 0:
            return merged, False
        merged.extend(page[overlap:])
    return merged, True


def messages_before_history_overlap(page: list, frontier: list) -> list | None:
    """Find the older prefix even if new arrivals extend the page's bottom."""
    for signatures_only in (False, True):
        left = [_message_control_token(m) for m in page]
        right = [_message_control_token(m) for m in frontier]
        if signatures_only:
            left = [t[1] for t in left]
            right = [t[1] for t in right]
        candidates = []
        for index in range(len(left)):
            length = 0
            while index + length < len(left) and length < len(right) and left[index + length] == right[length]:
                length += 1
            if length and (not signatures_only or length >= 2 or
                           (left.count(right[0]) == right.count(right[0]) == 1)):
                candidates.append((length, index))
        if candidates:
            best = max(length for length, _ in candidates)
            matches = [index for length, index in candidates if length == best]
            if len(matches) == 1:
                return page[:matches[0]]
            return None
        # A buffered subset of the preceding page adds no older occurrences.
        if left and any(right[i:i + len(left)] == left for i in range(len(right))):
            return []
    return None


def messages_after_history_overlap(frontier: list, page: list) -> list | None:
    """Join a downward page even when UIA changes its buffered older prefix."""
    appended = messages_before_history_overlap(list(reversed(page)), list(reversed(frontier)))
    return None if appended is None else list(reversed(appended))


MESSAGE_SIGNATURE_TTL_SEC = 15.0
MUTABLE_MEDIA_ANCHOR_TYPES = frozenset({"image", "video", "voice", "file"})
ANCHOR_RECOVERY_MAX_FAILURES = 3
ANCHOR_RECOVERY_COOLDOWN_SEC = 60.0


def anchor_snapshot_summary(snapshot) -> dict[str, object]:
    """Compact diagnostics without copying whole chat messages into health JSON."""
    tokens = tuple(snapshot)
    def describe(token):
        return {"id": token[0], "signature_hash": hashlib.sha256(
            str(token[1]).encode("utf-8")).hexdigest()[:12]}
    return {
        "count": len(tokens),
        "first": describe(tokens[0]) if tokens else None,
        "last": describe(tokens[-1]) if tokens else None,
    }


def _anchor_type_attr(signature: str) -> tuple[str, str]:
    """从 ``type|content|attr`` 中取不受 content 内竖线影响的两端字段。"""
    msg_type, separator, _rest = str(signature or "").partition("|")
    _head, tail_separator, attr = str(signature or "").rpartition("|")
    if not separator or not tail_separator:
        return "", ""
    return msg_type, attr


def _is_mutating_self_media_anchor(previous_signature: str, current_signature: str) -> bool:
    """判断同一 RuntimeId 是否只是自发媒体从上传态变成最终态。"""
    previous_type, previous_attr = _anchor_type_attr(previous_signature)
    current_type, current_attr = _anchor_type_attr(current_signature)
    return bool(
        previous_type in MUTABLE_MEDIA_ANCHOR_TYPES
        and current_type in MUTABLE_MEDIA_ANCHOR_TYPES
        and (previous_attr == "self" or current_attr == "self")
    )


def filter_recent_signatures(
    messages: list,
    recent: dict[str, float],
    now: float,
    ttl: float = MESSAGE_SIGNATURE_TTL_SEC,
) -> list:
    """过滤短时间内已经出现过的消息签名。

    微信消息列表滚动时旧消息可能获得新 RuntimeId；只靠 id 去重会把
    旧消息误判为新消息。用最近签名做第二道短 TTL 过滤。
    """
    result: list = []
    for message in messages:
        signature = message_signature(message)
        last_seen = recent.get(signature)
        if last_seen is None or now - last_seen > ttl:
            result.append(message)
    return result


def diff_new_messages(
    messages: list,
    used_ids: set[str],
    last_signatures: dict[str, str],
) -> tuple[list, set[str], dict[str, str]]:
    """纯函数：根据 id + 内容签名计算新消息。

    返回 ``(new_messages, next_used_ids, next_signatures)``。
    """
    new_messages: list = []
    next_ids: set[str] = set()
    next_signatures: dict[str, str] = {}
    for message in messages:
        if not message.id:
            continue
        signature = message_signature(message)
        next_ids.add(message.id)
        next_signatures[message.id] = signature
        old_signature = last_signatures.get(message.id)
        if message.id not in used_ids:
            new_messages.append(message)
        elif old_signature is not None and old_signature != signature:
            new_messages.append(message)
    return new_messages, next_ids, next_signatures


def messages_after_anchor(
    messages: list,
    anchor_id: str,
    anchor_signature: str,
) -> tuple[list, bool]:
    """返回上一轮尾消息之后的消息。

    微信 4.x 会虚拟化消息列表：滚动、切换前台或打开内置浏览器后，
    可见旧消息可能获得全新的 RuntimeId。仅靠 RuntimeId/短时签名去重
    仍可能重放旧消息，因此以“上一轮可见列表的最后一条消息”为锚点。

    先匹配 RuntimeId + 内容签名；UIA 重建导致 RuntimeId 改变时，再按
    内容签名匹配最后一个同签名项。找不到锚点时返回空列表，调用方应
    暂停派发，而不是把整页虚拟化旧消息当成新消息。
    """
    if not anchor_id and not anchor_signature:
        return list(messages), True

    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if (
            str(getattr(message, "id", "") or "") == anchor_id
            and message_anchor_signature(message) == anchor_signature
        ):
            return list(messages[index + 1 :]), True

    # 自发图片/视频在发送过程中会原地经历“上传中 -> 最终时长/缩略图”变形，
    # RuntimeId 通常不变，但 content（偶尔连 type）会变。若仍坚持内容签名
    # 完全相等，紧随其后的朋友消息会在锚点缺失时被静默吞入新基线。
    if anchor_id:
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if str(getattr(message, "id", "") or "") != anchor_id:
                continue
            if _is_mutating_self_media_anchor(
                anchor_signature,
                message_anchor_signature(message),
            ):
                return list(messages[index + 1 :]), True

    if anchor_signature:
        for index in range(len(messages) - 1, -1, -1):
            if message_anchor_signature(messages[index]) == anchor_signature:
                return list(messages[index + 1 :]), True
    return [], False


def messages_after_previous_overlap(
    messages: list,
    previous_snapshot: tuple[tuple[str, str], ...] | list[tuple[str, str]],
) -> tuple[list, bool]:
    """尾锚点变形/重建后，用更早的可见重叠项恢复确定的新消息后缀。

    优先匹配 RuntimeId + 稳定签名，其次允许同 RuntimeId 的自发媒体变形，
    最后才用稳定签名匹配 UIA 整体重建。只返回匹配项之后的消息；完全没有
    重叠时仍保持保守抑制，避免把滚动到的旧页面整页重放。
    """
    if not messages or not previous_snapshot:
        return [], False

    current_snapshot = [
        (
            str(getattr(message, "id", "") or ""),
            message_anchor_signature(message),
        )
        for message in messages
    ]

    def _result_after(previous_index: int, current_index: int) -> tuple[list, bool]:
        candidates = list(messages[current_index + 1 :])
        # 更早的重叠项之后可能还夹着未变形的旧控件。按完整 token 逐个扣除，
        # 但不按内容签名全局去重；这样 RuntimeId 不同的连续相同文本仍会保留。
        remaining_old = list(previous_snapshot[previous_index + 1 :])
        filtered: list = []
        for message in candidates:
            token = (
                str(getattr(message, "id", "") or ""),
                message_anchor_signature(message),
            )
            try:
                old_index = remaining_old.index(token)
            except ValueError:
                filtered.append(message)
            else:
                remaining_old.pop(old_index)
        return filtered, True

    # 1) 最可靠：完整 RuntimeId + 稳定签名重叠。
    for previous_index in range(len(previous_snapshot) - 1, -1, -1):
        token = tuple(previous_snapshot[previous_index])
        for current_index in range(len(current_snapshot) - 1, -1, -1):
            if current_snapshot[current_index] == token:
                return _result_after(previous_index, current_index)

    # 2) 同一 RuntimeId 的自发媒体控件原地变形。
    for previous_index in range(len(previous_snapshot) - 1, -1, -1):
        previous_id, previous_signature = previous_snapshot[previous_index]
        if not previous_id:
            continue
        for current_index in range(len(current_snapshot) - 1, -1, -1):
            current_id, current_signature = current_snapshot[current_index]
            if current_id == previous_id and _is_mutating_self_media_anchor(
                previous_signature,
                current_signature,
            ):
                return _result_after(previous_index, current_index)

    # 3) UIA 整体重建：RuntimeId 全变时以稳定签名找最后一个重叠项。
    for previous_index in range(len(previous_snapshot) - 1, -1, -1):
        previous_signature = previous_snapshot[previous_index][1]
        if not previous_signature:
            continue
        for current_index in range(len(current_snapshot) - 1, -1, -1):
            if current_snapshot[current_index][1] == previous_signature:
                return _result_after(previous_index, current_index)
    return [], False


def text_similarity(left: str, right: str) -> float:
    if not left and not right:
        return 1.0
    return difflib.SequenceMatcher(None, left, right).ratio()


class ChatMoreInfoWnd:
    """群资料面板。

    群昵称读取是微信 UI 适配能力，必须由 mabowx 自己完成。宿主程序只
    读取字符串，不需要替换类、遍历 UIA 控件或拦截写入方法。
    """

    _more_button_aid = (
        "content_view.top_content_view.title_h_view.right_v_view."
        "right_content_h_view.right_content_v_view.right_ui_.more_button"
    )
    _panel_class_name = "mmui::ChatRoomMemberInfoView"

    def __init__(self, parent=None) -> None:
        self.parent = parent
        self.root = getattr(parent, "control", None)
        self.panel = None
        self.more_button = None
        self._opened_by_us = False
        self._open()

    def _find_open_panel(self, timeout: float = 0.3):
        if self.root is None:
            return None
        return uia.find_descendant(
            self.root,
            class_name=self._panel_class_name,
            timeout=timeout,
        )

    def _focus_target(self) -> int:
        hwnd = int(getattr(self.root, "NativeWindowHandle", 0) or 0)
        if not hwnd:
            raise RuntimeError("群昵称读取缺少目标窗口句柄")
        if get_foreground_window() != hwnd:
            force_foreground(hwnd)
        if get_foreground_window() != hwnd:
            raise RuntimeError("群昵称读取未能取得目标窗口焦点")
        return hwnd

    def _find_more_button(self):
        return uia.find_descendant(
            self.root, control_type="ButtonControl", name="聊天信息",
            automation_id=self._more_button_aid, timeout=0,
        )

    def _click_more(self, more) -> bool:
        self._focus_target()
        import win32gui
        hwnd = int(self.root.NativeWindowHandle)
        rect = more.BoundingRectangle
        point = (int((rect.left + rect.right) // 2), int((rect.top + rect.bottom) // 2))
        hit = win32gui.WindowFromPoint(point)
        if int(win32gui.GetAncestor(hit, 2) or hit) != hwnd:
            raise RuntimeError("聊天信息按钮被其他窗口遮挡")
        uia.click_screen(*point, wait=0.05)
        return True

    def _wait_panel(self, timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            panel = self._find_open_panel(timeout=0)
            if panel is not None or time.monotonic() >= deadline:
                return panel
            time.sleep(0.025)

    def _open(self) -> None:
        if self.root is None:
            return
        self._focus_target()
        existing = self._find_open_panel(timeout=0)
        if existing is not None:
            self.panel = existing
            return
        # Poll actual UI state instead of fixed sleeps. Recover once after
        # restoring the same window; check for late opening before clicking again.
        for attempt in range(2):
            if attempt:
                show = getattr(self.parent, "show", None)
                if callable(show):
                    show()
                hwnd = int(getattr(self.parent, "HWND", 0) or 0)
                if hwnd:
                    self.root = uia.control_from_handle(hwnd)
                existing = self._find_open_panel(timeout=0)
                if existing is not None:
                    self.panel = existing
                    self._opened_by_us = True
                    return
            more = self._find_more_button()
            if more is None:
                continue
            self.more_button = more
            try:
                self._click_more(more)
            except RuntimeError:
                if attempt:
                    raise
                continue
            panel = self._wait_panel(0.4 if not attempt else 1.2)
            if panel is not None:
                self.panel = panel
                self._opened_by_us = True
                return
        raise RuntimeError("聊天信息侧栏未打开（点击及同窗口恢复均未成功）")

    @staticmethod
    def _control_identity(control) -> tuple:
        try:
            runtime_id = tuple(int(value) for value in control.GetRuntimeId())
        except Exception:
            runtime_id = ()
        if runtime_id:
            return ("runtime", *runtime_id)
        try:
            rect = control.BoundingRectangle
            rectangle = (
                int(rect.left),
                int(rect.top),
                int(rect.right),
                int(rect.bottom),
            )
        except Exception:
            rectangle = ()
        try:
            return (
                "properties",
                str(getattr(control, "Name", "") or ""),
                str(getattr(control, "ClassName", "") or ""),
                str(getattr(control, "AutomationId", "") or ""),
                rectangle,
            )
        except Exception:
            return ("object", id(control))

    def get_item_control(self, item: str, *, max_tabs: int = 64):
        """Find a virtualized chat-info row using keyboard focus traversal.

        WeChat 4.1 renders the settings below the member grid, but those rows
        are not children of the exposed UIA tree.  They become real controls
        only while Tab moves focus through the side panel.  This is the same
        navigation used by the former application-side compatibility patch, now owned
        by mabowx and bounded to this exact chat window.
        """
        if self.root is None or self.panel is None:
            return None
        try:
            expected_hwnd = int(getattr(self.root, "NativeWindowHandle", 0) or 0)
        except Exception:
            expected_hwnd = 0
        try:
            self.root.SetFocus()
        except Exception:
            try:
                self.panel.SetFocus()
            except Exception:
                return None

        seen: set[tuple] = set()
        for _ in range(max(1, int(max_tabs))):
            try:
                self.root.SendKeys("{Tab}", waitTime=0.03)
                control = uia.get_focused_control()
            except Exception:
                continue
            if control is None:
                continue
            identity = self._control_identity(control)
            if identity in seen:
                break
            seen.add(identity)
            try:
                top = control.GetTopLevelControl()
                top_hwnd = int(getattr(top, "NativeWindowHandle", 0) or 0)
                if expected_hwnd and top_hwnd != expected_hwnd:
                    break
                name = str(getattr(control, "Name", "") or "").strip()
                control_type = str(
                    getattr(control, "ControlTypeName", "") or ""
                )
            except Exception:
                continue
            if name == item and control_type == "ButtonControl":
                return control
        return None

    def close(self) -> bool:
        """Close only the side panel opened by this instance.

        Never send Esc to the chat root: when panel opening failed, Esc closes
        the detached listener window itself.
        """
        if not self._opened_by_us or self.root is None:
            return True
        panel = self._find_open_panel(timeout=0)
        if panel is None:
            self._opened_by_us = False
            return True
        try:
            self._focus_target()
            more = self._find_more_button()
            if more is None:
                return False
            self._click_more(more)
        except Exception:
            return False
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if self._find_open_panel(timeout=0) is None:
                self._opened_by_us = False
                self.panel = None
                return True
            time.sleep(0.05)
        return False

    @staticmethod
    def _text_values(control, *, max_nodes: int = 80) -> list[str]:
        ignored = {"我在本群的昵称", "编辑", "进入", "返回"}
        result: list[str] = []
        for candidate in (control, *list(uia.iter_descendants(control, max_nodes=max_nodes))):
            try:
                name = str(getattr(candidate, "Name", "") or "").strip()
                control_type = str(
                    getattr(candidate, "ControlTypeName", "") or ""
                )
                class_name = str(getattr(candidate, "ClassName", "") or "")
            except Exception:
                continue
            if (
                name
                and name not in ignored
                and len(name) <= 64
                and (
                    control_type in {"TextControl", "EditControl"}
                    or class_name == "mmui::XTextView"
                )
                and name not in result
            ):
                result.append(name)
        return result

    def _nickname_setting_candidates(self) -> list[str]:
        """Read only the setting row labelled ``我在本群的昵称``."""
        if self.root is None:
            return []
        hwnd = self._focus_target()
        label = self.get_item_control("我在本群的昵称")
        if label is None:
            # Compatibility fallback for builds that expose the row normally.
            label = uia.find_descendant(
                self.root,
                control_type="ButtonControl",
                name="我在本群的昵称",
                timeout=0.5,
            )
        if label is None:
            return []

        if get_foreground_window() != hwnd:
            raise RuntimeError("群昵称读取期间目标窗口失去焦点")
        # Only values inside the exact nickname row are evidence. Walking up
        # to the panel can mistake a group name or member name for our nickname.
        return self._text_values(label)

    def get_my_nickname(self) -> str:
        candidates = self._nickname_setting_candidates()
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise RuntimeError(f"群昵称设置值不唯一: {candidates!r}")
        raise RuntimeError("未读取到“我在本群的昵称”")

    def set_my_nickname(self, value):
        raise NotImplementedError("当前版本仅支持读取群昵称")

    def set_group_name(self, value):
        return None

    def set_group_remark(self, value):
        return None

    def set_announcement(self, value):
        return None


class ChatBox(BaseUISubWnd):
    """聊天页面：输入框、发送按钮、消息列表。"""

    _ui_cls_name = "mmui::ChatMessagePage"

    def __init__(self, root: BaseUIWnd) -> None:
        self.root = root
        self.parent = None
        self.control = None
        self._input = None
        self._send_btn = None
        self._message_list = None
        self._last_time = ""
        self._used_msg_ids: set[str] = set()
        self._last_signatures: dict[str, str] = {}
        self._recent_signatures: dict[str, float] = {}
        self._tail_message_id = ""
        self._tail_message_signature = ""
        self._visible_message_snapshot: tuple[tuple[str, str], ...] = ()
        self._visible_control_snapshot: tuple[tuple[str, str], ...] = ()
        self._anchor_miss_snapshot: tuple[tuple[str, str], ...] | None = None
        self._anchor_miss_rounds = 0
        self._last_anchor_missing = False
        self._anchor_recovery_last_attempt = 0.0
        self._anchor_recovery_failures = 0
        self._last_anchor_recovery_result: dict[str, object] = {}
        self._anchor_missing_since: float | None = None
        self._anchor_wait_control_snapshot = ()
        self._message_read_lock = threading.Lock()
        self._delivery_direction_snapshot = None
        self._sent_texts: dict[str, float] = {}
        self._sent_filenames: dict[str, float] = {}
        self._cache_chat_name: str | None = None
        self._delivery_sequence = 0
        self._media_operation_sequencer = OrderedOperationSequencer()
        # Listening only needs the message list. Input/button/page discovery is
        # deferred until a send operation calls refresh() or input_control.
        self.control = getattr(root, "control", None)

    def init(self) -> None:
        if self.root is None or not self.root.exists():
            return
        self.control = uia.find_descendant(
            self.root.control,
            **runtime_selector(self.root, "chat.page"),
            timeout=2.0,
        )
        if self.control is None:
            self.control = self.root.control
        self._input = uia.find_descendant(
            self.root.control,
            **runtime_selector(self.root, "chat.input"),
            timeout=1.5,
        )
        self._send_btn = uia.find_descendant(
            self.root.control,
            **runtime_selector(self.root, "chat.send_button"),
            timeout=1.0,
        )
        self._message_list = uia.find_descendant(
            self.root.control,
            **runtime_selector(self.root, "chat.message_list"),
            timeout=1.0,
        )

    def _cached_control_is_in_root(self, control) -> bool:
        """快速校验缓存控件仍属于当前聊天顶层窗口。"""
        if control is None:
            return False
        try:
            if not control.Exists(0):
                return False
            root_control = getattr(self.root, "control", None)
            root_hwnd = int(
                getattr(self.root, "HWND", 0)
                or getattr(root_control, "NativeWindowHandle", 0)
                or 0
            )
            top = control.GetTopLevelControl()
            control_hwnd = int(getattr(top, "NativeWindowHandle", 0) or 0)
            return not (root_hwnd and control_hwnd) or root_hwnd == control_hwnd
        except Exception:
            return False

    def refresh(self, force: bool = False) -> None:
        """重新定位输入框/发送按钮/消息列表。

        搜索切换聊天后，旧 UIA Element 可能虽存在但已失效，
        因此发送前必须校验。缓存控件仍属于同一顶层窗口时直接复用，
        避免一次发送重复三遍 UIA 全树查询。
        """
        if not force and all(
            self._cached_control_is_in_root(control)
            for control in (self._input, self._send_btn, self._message_list)
        ):
            return
        self.init()

    @property
    def input_control(self):
        if self._input is None or not self._input.Exists(0):
            self.init()
        return self._input

    @property
    def editbox(self):
        """Mabobot 兼容别名：部分辅助函数直接访问 ``ChatBox.editbox``。"""
        return self.input_control

    @property
    def send_button(self):
        if self._send_btn is None or not self._send_btn.Exists(0):
            self._send_btn = uia.find_descendant(
                self.root.control,
                **runtime_selector(self.root, "chat.send_button"),
                timeout=1.0,
            )
        return self._send_btn

    @property
    def message_list(self):
        if getattr(self.root, "control", None) is None:
            return None
        if self._message_list is None or not self._message_list.Exists(0):
            self._message_list = uia.find_descendant(
                self.root.control,
                **runtime_selector(self.root, "chat.message_list"),
                timeout=1.0,
            )
        return self._message_list

    @property
    def who(self) -> str:
        getter = getattr(self.root, "get_current_chat_name", None)
        if getter is not None:
            try:
                current = getter() or ""
                if current:
                    return str(current).strip()
            except Exception:
                pass
        # 独立聊天窗口没有 get_current_chat_name()，但其顶层窗口包装器
        # 暴露了 who。旧逻辑直接返回空串，导致所有私聊来信 sender=''。
        try:
            current = getattr(self.root, "who", "") or ""
            if current:
                return str(current).splitlines()[0].strip()
        except Exception:
            pass
        return ""

    def get_info(self) -> dict[str, Any]:
        return {"chat_name": self.who, "exists": self.exists()}

    # ------------------------------------------------------------------
    # 输入框
    # ------------------------------------------------------------------

    def _activate_editbox(self) -> bool:
        inp = self.input_control
        if inp is None or not inp.Exists(0):
            return False
        inp.Click(simulateMove=False, waitTime=0.08)
        return True

    @uilock
    def clear_edit(self) -> WxResponse:
        """清空输入框。"""
        self.refresh()
        if not self._activate_editbox():
            return WxResponse.failure("输入框不存在")
        inp = self.input_control
        inp.SendKeys("{Ctrl}a", waitTime=0.05)
        inp.SendKeys("{BACK}", waitTime=0.08)
        deadline = time.monotonic() + 0.6
        value = self.get_edit_text()
        while value.strip() and time.monotonic() < deadline:
            time.sleep(0.03)
            value = self.get_edit_text()
        if not value.strip():
            return WxResponse.success()
        return WxResponse.failure(f"输入框未能清空: {value!r}")

    def get_edit_text(self) -> str:
        inp = self.input_control
        if inp is None or not inp.Exists(0):
            return ""
        try:
            return str(inp.GetValuePattern().Value or "")
        except Exception:
            # 注意：不能回退到 inp.Name，因为输入框 Name 是当前聊天标题。
            return ""

    def wait_quote_context(self, timeout: float = 3.0) -> WxResponse:
        """等待微信把当前引用草稿渲染成独立 ``ReferView``。

        微信 4.x 不会把引用预览写进输入框的 ValuePattern。以输入框文本
        是否非空判断引用态，会在空正文时必然等满超时；ReferView 才是
        可直接观察的完成信号。
        """
        root_control = getattr(self.root, "control", None)
        if root_control is None:
            return WxResponse.failure("聊天窗口不存在")
        control = uia.find_descendant(
            root_control,
            control_type="CustomControl",
            class_name="mmui::ReferView",
            timeout=max(0.0, float(timeout)),
        )
        if control is None:
            return WxResponse.failure("引用状态未出现")
        try:
            name = str(getattr(control, "Name", "") or "")
        except Exception:
            name = ""
        return WxResponse.success(data={"quote_context": name})

    def _paste_edit_text(
        self,
        text: str,
        *,
        verify: bool = True,
        replace: bool = False,
        verify_contains: bool = False,
    ) -> WxResponse:
        """在已刷新控件上粘贴文本；可用 Ctrl+A 原子替换现有内容。"""
        if not self._activate_editbox():
            return WxResponse.failure("输入框不存在")
        inp = self.input_control
        if replace:
            inp.SendKeys("{Ctrl}a", waitTime=0.05)
        set_text(text)
        time.sleep(0.03)
        inp.SendKeys("{Ctrl}v", waitTime=0.12)
        deadline = time.monotonic() + 0.8
        actual = self.get_edit_text()
        ratio = text_similarity(actual, text)
        matched = text in actual if verify_contains else ratio >= WxParam.SEND_CONTENT_RATIO
        while verify and not matched and time.monotonic() < deadline:
            time.sleep(0.03)
            actual = self.get_edit_text()
            ratio = text_similarity(actual, text)
            matched = text in actual if verify_contains else ratio >= WxParam.SEND_CONTENT_RATIO
        if verify:
            wxlog.debug(
                "输入框校验: "
                f"actual={actual!r} ratio={ratio:.3f} contains={text in actual}"
            )
            if not matched:
                return WxResponse.failure(
                    f"输入框内容校验失败: ratio={ratio:.3f}, actual={actual!r}"
                )
        return WxResponse.success()

    @uilock
    def set_edit_text(self, text: str, verify: bool = True) -> WxResponse:
        """把文本可靠地写入输入框，并校验相似度。"""
        self.refresh()
        return self._paste_edit_text(text, verify=verify)

    @uilock
    def append_edit_text(self, text: str, verify: bool = True) -> WxResponse:
        """在当前输入框/富文本上下文末尾追加文本。"""
        if not text:
            return WxResponse.success()
        self.refresh()
        # 不能先读取 current 再把 current + text 整段粘贴回去：粘贴动作
        # 本身就是追加，那样会重复已有草稿。引用预览和 @ 对象还可能是
        # ValuePattern 不完整表达的富文本，只验证新增正文确实出现即可。
        return self._paste_edit_text(
            text,
            verify=verify,
            verify_contains=True,
        )

    @uilock
    def append_quote_text(self, text: str, verify: bool = True) -> WxResponse:
        """在已经建立并聚焦的引用上下文中快速追加正文。

        ``select_option('引用')`` 会把焦点直接交给输入框。沿用这一
        交互，复用缓存控件并直接粘贴，避免再次刷新三类控件、点击输入框
        和等待固定的键盘间隔；SendKeys 会自行恢复输入框焦点。
        """
        if not text:
            return WxResponse.success()
        inp = self._input
        if inp is None:
            return self.append_edit_text(text, verify=verify)
        try:
            # SendKeys 自身会 SetFocus；quote() 外层持有同一 UI 锁，而且刚
            # 用 ReferView 校验过当前聊天，因此无需再做 Exists/焦点查询。
            set_text(text)
            inp.SendKeys("{Ctrl}v", waitTime=0.02)
        except Exception as exc:
            return WxResponse.error(f"引用正文写入失败: {exc}")
        if not verify:
            return WxResponse.success()

        deadline = time.monotonic() + 0.5
        actual = self.get_edit_text()
        while text not in actual and time.monotonic() < deadline:
            time.sleep(0.02)
            actual = self.get_edit_text()
        if text not in actual:
            return WxResponse.failure(f"引用正文写入校验失败: actual={actual!r}")
        wxlog.debug(f"引用正文校验: actual={actual!r}")
        return WxResponse.success()

    @uilock
    def send_quote_input(
        self,
        expected_text: str,
        completion_timeout: float = 0.5,
    ) -> WxResponse:
        """快速提交已聚焦的引用正文，并用输入框变化做短确认。"""
        expected_text = str(expected_text or "").strip()
        if not expected_text:
            return WxResponse.failure("引用回复内容不能为空")

        inp = self._input
        if inp is None:
            return self.send_current_input()

        button = self._send_btn
        if button is None:
            button = self.send_button
        try:
            if button is None or not button.IsEnabled:
                return WxResponse.failure("发送按钮未启用")
        except Exception:
            return WxResponse.failure("发送按钮状态不可用")

        self._mark_sent_text(expected_text)
        try:
            # 引用菜单和粘贴均已把输入框置于焦点；直接按回车，避免普通
            # 路径再次 refresh + Click。SendKeys 自身仍会 SetFocus 兜底。
            inp.SendKeys("{Enter}", waitTime=0.02)
        except Exception as exc:
            return WxResponse.error(f"引用回复回车发送失败: {exc}")

        deadline = time.monotonic() + max(0.0, float(completion_timeout))
        while True:
            current = self.get_edit_text()
            if expected_text not in current:
                return WxResponse.success()
            try:
                if button is not None and button.Exists(0) and not button.IsEnabled:
                    return WxResponse.success()
            except Exception:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # 与现有 send_current_input 一致：键盘投递成功后，UI 状态
                # 超时只是不再阻塞调用方，不把可能已发送的消息判成失败。
                return WxResponse.success()
            time.sleep(min(0.02, remaining))

    @uilock
    def send_current_input(self) -> WxResponse:
        """发送输入框当前内容（不清理、不覆盖）。"""
        self.refresh()
        sent_text = self.get_edit_text().strip()
        if not sent_text:
            return WxResponse.failure("输入框为空")
        if not self._wait_send_button_enabled():
            return WxResponse.failure("发送按钮未启用")
        self._mark_sent_text(sent_text)
        response = self._click_send()
        if not response.is_success:
            return response
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            btn = self.send_button
            if btn is not None and btn.Exists(0) and not btn.IsEnabled:
                break
            if not self.get_edit_text().strip():
                break
            time.sleep(0.2)
        return WxResponse.success()

    def _mark_sent_text(self, text: str) -> None:
        if text:
            self._sent_texts[text] = time.time()

    def _mark_sent_files(self, paths) -> None:
        for path in paths:
            self._sent_filenames[Path(path).name] = time.time()

    def _prune_sent_marks(self) -> None:
        now = time.time()
        self._sent_texts = {k: v for k, v in self._sent_texts.items() if now - v < 300}
        self._sent_filenames = {k: v for k, v in self._sent_filenames.items() if now - v < 300}

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    def _wait_send_button_enabled(self, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            btn = self.send_button
            if btn is not None and btn.Exists(0):
                try:
                    if btn.IsEnabled:
                        return True
                except Exception:
                    pass
            time.sleep(0.2)
        return False

    def _click_send(self) -> WxResponse:
        """按回车直接发送；输入框不可用时才回退点击发送按钮。"""
        inp = self.input_control
        if inp is not None and inp.Exists(0):
            try:
                inp.Click(simulateMove=False, waitTime=0.05)
                inp.SendKeys("{Enter}", waitTime=0.15)
                wxlog.debug("已按回车发送")
                return WxResponse.success()
            except Exception as exc:
                wxlog.warning(f"回车发送失败，准备点击发送按钮: {exc}")
        btn = self.send_button
        if btn is None or not btn.Exists(0):
            wxlog.warning("未找到发送按钮")
            return WxResponse.failure("未找到发送按钮")
        if not btn.IsEnabled:
            wxlog.warning("发送按钮不可用")
            return WxResponse.failure("发送按钮不可用")
        wxlog.log_control("点击发送按钮", btn, level="debug")
        btn.Click(simulateMove=False, waitTime=0.8)
        wxlog.debug("发送按钮已点击")
        return WxResponse.success()

    @uilock
    def send_text(
        self,
        msg: str,
        clear: bool = True,
        at: str | list[str] | None = None,
    ) -> WxResponse:
        """发送文本消息。"""
        self.refresh()
        if not msg:
            return WxResponse.failure("消息内容不能为空")
        if not self.exists():
            return WxResponse.failure("聊天窗口不存在")

        if at:
            response = self.clear_edit()
            if not response.is_success:
                return WxResponse.failure(f"无法清除输入框内容，发送失败: {response['message']}")
            response = self.input_at(at, exact=True, verify=True)
            if not response.is_success:
                self.clear_edit()
                return response
            members = list(response["data"].get("mentioned_users") or [])
            response = self._paste_edit_text(f" {msg}", verify=False)
            if response.is_success:
                draft = self.get_edit_text()
                object_count = draft.count(MENTION_OBJECT_CHARACTER)
                if object_count != len(members):
                    response = WxResponse.failure(
                        "@对象在发送前丢失: "
                        f"expected={len(members)} actual={object_count}"
                    )
                elif msg not in draft:
                    response = WxResponse.failure("发送前未能验证消息正文")
        else:
            # 普通文本可在一次焦点事务中 Ctrl+A + 粘贴完成替换。旧流程
            # 先 clear_edit 再 set_edit_text，会重复刷新控件和激活输入框，
            # 每段回复额外消耗约一秒。粘贴后的相似度校验仍阻止错发。
            response = self._paste_edit_text(msg, verify=True, replace=True)
        if not response.is_success:
            return response

        if not self._wait_send_button_enabled():
            return WxResponse.failure("发送按钮未启用")
        self._mark_sent_text(msg)
        response = self._click_send()
        if not response.is_success:
            return response

        # 微信发送成功后输入框通常自动清空。
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            btn = self.send_button
            if btn is not None and btn.Exists(0) and not btn.IsEnabled:
                break
            if not self.get_edit_text().strip():
                break
            time.sleep(0.2)

        if not clear:
            self.set_edit_text(msg, verify=False)
        return WxResponse.success()

    # ------------------------------------------------------------------
    # @ 成员
    # ------------------------------------------------------------------

    @uilock
    def input_at(
        self,
        members: str | list[str],
        *,
        exact: bool = True,
        verify: bool = True,
    ) -> WxResponse:
        """在输入框中 @ 一个或多个群成员，并验证富文本对象。"""
        if isinstance(members, str):
            members = [members]
        normalized = []
        for member in members:
            value = str(member or "").strip().lstrip("@").strip()
            if value and value not in normalized:
                normalized.append(value)
        if not normalized:
            return WxResponse.failure("@成员不能为空")
        for expected_count, member in enumerate(normalized, start=1):
            if not self._activate_editbox():
                return WxResponse.failure("输入框不存在")
            self.input_control.SendKeys("@", waitTime=0.1)
            matches = self._find_mention_items(member, timeout=3.0, exact=exact)
            if not matches:
                self.input_control.SendKeys("{Esc}", waitTime=0.2)
                return WxResponse.failure(f"未找到@成员：{member}")
            if exact and len(matches) != 1:
                self.input_control.SendKeys("{Esc}", waitTime=0.2)
                return WxResponse.failure(f"@成员精确匹配不唯一：{member}")
            matches[0].Click(simulateMove=False, waitTime=0.3)
            time.sleep(0.15)
            if verify:
                object_count = self.get_edit_text().count(MENTION_OBJECT_CHARACTER)
                if object_count != expected_count:
                    return WxResponse.failure(
                        f"@成员富文本校验失败：{member} "
                        f"expected={expected_count} actual={object_count}"
                    )
        return WxResponse.success(
            data={
                "mentioned_users": normalized,
                "verified_mention_objects": len(normalized) if verify else None,
            }
        )

    def _find_mention_items(
        self,
        member: str,
        timeout: float = 3.0,
        *,
        exact: bool = True,
    ) -> list:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            matches = []
            for control in uia.find_controls(
                self.root.control,
                class_name="mmui::ChatMentionList",
                max_results=3,
            ):
                for candidate in uia.iter_descendants(control, max_nodes=80):
                    try:
                        name = str(candidate.Name or "")
                        first = name.splitlines()[0].strip() if name else ""
                        matched = first == member if exact else member in first
                        if matched:
                            matches.append(candidate)
                    except Exception:
                        continue
            if matches:
                return matches
            time.sleep(0.25)
        return []

    def _find_mention_item(self, member: str, timeout: float = 3.0):
        """Backward-compatible single-item accessor."""
        matches = self._find_mention_items(member, timeout=timeout, exact=True)
        return matches[0] if len(matches) == 1 else None

    # ------------------------------------------------------------------
    # 文件
    # ------------------------------------------------------------------

    @uilock
    def send_files(self, filepaths) -> WxResponse:
        """一次性粘贴多个文件到输入框，只点击一次发送。"""
        self.refresh()
        paths: list[Path] = []
        for filepath in filepaths:
            path = Path(filepath)
            if not path.is_file():
                return WxResponse.failure(f"文件路径不存在: {filepath}")
            paths.append(path)
        if not paths:
            return WxResponse.failure("文件列表不能为空")
        if not self.exists():
            return WxResponse.failure("聊天窗口不存在")

        response = self.clear_edit()
        if not response.is_success:
            return WxResponse.failure(f"无法清除输入框内容: {response['message']}")

        try:
            set_files([str(path) for path in paths])
        except Exception as exc:
            return WxResponse.error(f"设置文件剪贴板失败: {exc}")

        if not self._activate_editbox():
            return WxResponse.failure("输入框不存在")
        self.input_control.SendKeys("{Ctrl}v", waitTime=0.8)
        time.sleep(1.0)

        if not self._wait_send_button_enabled():
            return WxResponse.failure("文件粘贴后发送按钮未启用")
        self._mark_sent_files(paths)
        response = self._click_send()
        if not response.is_success:
            return response
        # 等微信把附件从输入框拿走，避免连续操作时互相干扰。
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            btn = self.send_button
            if btn is not None and btn.Exists(0) and not btn.IsEnabled:
                break
            if not self.get_edit_text().strip():
                break
            time.sleep(0.2)
        wxlog.info(f"批量发送文件完成: {[p.name for p in paths]}")
        return WxResponse.success()

    @uilock
    def send_file(self, filepath: str | Path) -> WxResponse:
        """通过剪贴板 CF_HDROP 粘贴并发送单个文件。"""
        self.refresh()
        path = Path(filepath)
        if not path.is_file():
            return WxResponse.failure(f"文件路径不存在: {filepath}")
        if not self.exists():
            return WxResponse.failure("聊天窗口不存在")

        response = self.clear_edit()
        if not response.is_success:
            return WxResponse.failure(f"无法清除输入框内容: {response['message']}")

        try:
            set_files([str(path)])
        except Exception as exc:
            return WxResponse.error(f"设置文件剪贴板失败: {exc}")

        if not self._activate_editbox():
            return WxResponse.failure("输入框不存在")
        self.input_control.SendKeys("{Ctrl}v", waitTime=0.8)
        time.sleep(1.0)

        if not self._wait_send_button_enabled():
            return self._send_file_via_dialog(path)
        response = self._click_send()
        if response.is_success:
            return response
        return self._send_file_via_dialog(path)

    def set_group_my_nickname(self, value) -> None:
        """打开群资料并设置“我在本群的昵称”（尚未开放写入）。"""
        wnd = ChatMoreInfoWnd(self.root)
        return wnd.set_my_nickname(value)

    @uilock
    def get_group_my_nickname(self) -> str:
        """从群资料面板只读返回当前账号的群昵称。"""
        wnd = ChatMoreInfoWnd(self.root)
        try:
            return wnd.get_my_nickname()
        finally:
            wnd.close()

    def run_ordered_media_operation(self, message, operation):
        """Run one delayed media action in listener delivery order.

        Ordering is resolved before acquiring the global UI transaction.  This
        avoids a later request holding the UI lock while an earlier request is
        still waiting to enter the per-chat queue.
        """
        sequence = int(getattr(message, "delivery_sequence", 0) or 0)

        def _run():
            with ui_transaction(timeout=120.0):
                return operation()

        return self._media_operation_sequencer.run(self.who, sequence, _run)

    def _send_file_via_dialog(self, path: Path) -> WxResponse:
        """后备方案：点击“发送文件”按钮，通过 Windows 文件对话框选择。"""
        file_button = uia.find_descendant(
            self.root.control,
            control_type="ButtonControl",
            name="发送文件",
            timeout=1.0,
        )
        if file_button is None:
            return WxResponse.failure("未找到发送文件按钮")
        file_button.Click(simulateMove=False, waitTime=0.8)
        deadline = time.monotonic() + 5.0
        dialog = None
        while time.monotonic() < deadline:
            dialog = uia.find_top_level_control("#32770", timeout=0.5)
            if dialog is not None:
                break
            time.sleep(0.2)
        if dialog is None:
            return WxResponse.failure("文件对话框未出现")
        dialog.SendKeys(str(path), interval=0.02, waitTime=0.5)
        dialog.SendKeys("{Enter}", waitTime=1.0)
        return WxResponse.success()

    # ------------------------------------------------------------------
    # 消息读取
    # ------------------------------------------------------------------

    def is_group_chat(self) -> bool:
        """通过标题栏 (N) 判断当前是否为群聊。"""
        for control in uia.find_controls(
            self.root.control,
            control_type="TextControl",
            class_name="mmui::XTextView",
            max_results=20,
        ):
            try:
                automation_id = control.AutomationId or ""
                if automation_id.endswith("current_chat_count_label") and control.Name:
                    return True
            except Exception:
                continue
        return False

    def _probe_avatar_side(
        self,
        control,
        direction: str,
        *,
        focus_timeout: float = GROUP_SENDER_FOCUS_TIMEOUT_SEC,
    ) -> tuple[bool, str]:
        """在消息指定一侧命中真实头像控件；始终收尾本次弹出菜单。"""
        if direction not in {"friend", "self"}:
            return False, ""
        try:
            if not control.Exists(0):
                return False, ""
            root_hwnd = int(
                getattr(self.root, "HWND", 0)
                or getattr(getattr(self.root, "control", None), "NativeWindowHandle", 0)
                or 0
            )
            if not root_hwnd:
                return False, ""
            message_top = control.GetTopLevelControl()
            message_hwnd = int(getattr(message_top, "NativeWindowHandle", 0) or 0)
            if message_hwnd and message_hwnd != root_hwnd:
                return False, ""
            point = group_sender_head_point(control, direction)
            if point is None:
                return False, ""
        except Exception:
            return False, ""

        # Do not overwrite a menu the user already has open in this chat.  A
        # probe-created popup is selected below by exact PID + owner + UIA class
        # and by being absent from this pre-click visible set.
        root_pid = int(getattr(self.root, "pid", 0) or 0)
        baseline_menu_hwnds: set[int] = set()
        if root_pid:
            try:
                for window in enum_windows_by_pid(root_pid):
                    if (
                        not window.visible
                        or window.class_name not in AVATAR_MENU_NATIVE_CLASSES
                        or get_window_owner(window.hwnd) != root_hwnd
                    ):
                        continue
                    candidate = uia.control_from_handle(window.hwnd)
                    if str(getattr(candidate, "ClassName", "") or "") == "mmui::XMenu":
                        baseline_menu_hwnds.add(int(window.hwnd))
            except Exception:
                baseline_menu_hwnds = set()
        if baseline_menu_hwnds:
            wxlog.debug(
                "头像方向探测跳过: "
                f"chat={self.who!r} existing_menu={sorted(baseline_menu_hwnds)!r}"
            )
            return False, ""

        posted = False
        matched = False
        sender = ""
        try:
            control.SetFocus()
            posted = post_right_click(root_hwnd, point[0], point[1])
            if not posted:
                return False, ""
            # Read the avatar focus after right-click + middle-click.
            if not post_middle_click(root_hwnd, point[0], point[1]):
                return False, ""
            deadline = time.monotonic() + max(0.02, float(focus_timeout))
            while True:
                focused = uia.get_focused_control()
                matched, sender = message_avatar_from_focused_control(
                    control,
                    focused,
                    direction,
                )
                if matched:
                    return True, sender
                if time.monotonic() >= deadline:
                    return False, ""
                time.sleep(0.02)
        except Exception as exc:
            wxlog.debug(
                f"头像方向探测失败: chat={self.who!r} "
                f"direction={direction} error={exc}"
            )
            return False, ""
        finally:
            if posted:
                try:
                    from mabowx.ui.component import Menu

                    # Synchronous clicks have already returned. Only inspect
                    # a popup that still exists; do not wait for a dismissed one.
                    pending = [window for window in enum_windows_by_pid(root_pid)
                               if window.visible and window.hwnd not in baseline_menu_hwnds
                               and window.class_name in AVATAR_MENU_NATIVE_CLASSES
                               and get_window_owner(window.hwnd) == root_hwnd]
                    if pending:
                        Menu(
                            self.root, timeout=0.0 if matched else 0.25,
                            anchor=None, baseline_hwnds=baseline_menu_hwnds,
                            require_new=True, expected_owner_hwnd=root_hwnd,
                        ).close()
                except Exception:
                    pass

    def _detect_avatar_direction(self, control) -> tuple[str, str] | None:
        """按原版先左后右探测；保留三轮上限，避免失效控件阻塞监听。"""
        started = time.monotonic()
        for attempt in range(3):
            for direction in ("friend", "self"):
                matched, sender = self._probe_avatar_side(control, direction)
                if matched:
                    wxlog.debug(
                        "头像方向探测命中: "
                        f"chat={getattr(self, '_cache_chat_name', '')!r} direction={direction} sender={sender!r} "
                        f"anchor={control_anchor_token(control)!r} "
                        f"attempt={attempt + 1} "
                        f"elapsed_ms={(time.monotonic() - started) * 1000:.1f}"
                    )
                    return direction, sender
        wxlog.debug(
            "头像方向探测未命中: "
            f"chat={getattr(self, '_cache_chat_name', '')!r} elapsed_ms={(time.monotonic() - started) * 1000:.1f}"
        )
        return None

    def _identity_for(
        self, control, *, probe_avatar: bool = True,
    ) -> tuple[str | None, str, str]:
        """每次读取当前头像；方向、发送者和来源仅属于本次消息对象。"""
        class_name = str(getattr(control, "ClassName", "") or "")
        if class_name in ("mmui::ChatItemView", "mmui::ChatSystemInfoItemView", "mmui::ChatAppReaderItemView"):
            return None, "", "system"
        try:
            if probe_avatar:
                result = self._detect_avatar_direction(control)
                if result is not None:
                    return result[0], result[1], "avatar"
            hwnd = getattr(self.root, "HWND", None)
            direction = detect_message_direction(control, hwnd=int(hwnd) if hwnd else None)
            return direction, "", "visual" if direction else "fallback"
        except Exception:
            return None, "", "fallback"

    def _direction_for_message(self, msg_type: str, raw_name: str, content: str) -> str | None:
        """确定性方向判断：不依赖截图。

        规则：
        - 系统/时间/公众号消息返回 None，由类型层标记为 system；
        - mabowx 自己发送过的文本/文件名 -> self；
        - 文件进度消息 -> self；
        - 其余真人消息 -> friend。

        局限：用户在微信界面手动发送、且未经过 mabowx 的自发消息会按
        friend 处理；bot 正常收发流程不受影响。
        """
        self._prune_sent_marks()
        if msg_type in ("system", "official", "time"):
            return None
        if msg_type == "file" and content.startswith("进度"):
            return "self"
        if msg_type in ("text", "quote", "other") and raw_name in self._sent_texts:
            return "self"
        if content in self._sent_texts:
            return "self"
        if msg_type == "file" and content in self._sent_filenames:
            return "self"
        return "friend"

    def _sender_for(self, direction: str | None, msg_type: str) -> str:
        if direction == "self":
            return getattr(self.root, "nickname", None) or "我"
        if direction == "friend":
            if self.is_group_chat():
                # 群成员名需要在消息差分完成后，通过隐藏头像焦点控件补齐。
                # 此处保持空值，避免 sender 的延迟变化干扰尾锚点/重复检测。
                return ""
            return self.who
        if msg_type == "system":
            return "系统"
        if msg_type == "official":
            return "公众号"
        return ""

    def _extract_group_sender(self, message) -> str:
        """对单条群消息执行一次头像焦点探测。"""
        control = getattr(message, "control", None)
        direction = str(getattr(message, "direction", "") or "")
        if direction != "friend" or control is None:
            return ""
        # 方向探测已确定为左侧后，这里只需在同一侧补取可能暂时为空的 Name。
        for _attempt in range(2):
            matched, sender = self._probe_avatar_side(control, "friend")
            if matched:
                if sender:
                    return sender
            time.sleep(0.04)
        return ""

    @uilock
    def _resolve_group_senders(self, messages: list) -> list:
        """只为即将交付的群聊来信补齐 sender，不污染消息差分基线。"""
        if not messages or not self.is_group_chat():
            return messages
        candidates = [
            message
            for message in messages
            if str(getattr(message, "direction", "") or "") == "friend"
            and not str(getattr(message, "sender", "") or "").strip()
            and str(getattr(message, "type", "") or "")
            not in {"system", "time", "official"}
        ]
        if not candidates:
            return messages

        resolved = 0
        for message in candidates:
            sender = self._extract_group_sender(message)
            if sender:
                message.sender = sender
                resolved += 1
            else:
                wxlog.debug(
                    "群聊发送者未识别: "
                    f"chat={self.who!r} type={getattr(message, 'type', '')!r} "
                    f"content={str(getattr(message, 'content', '') or '')[:80]!r}"
                )
        if resolved:
            wxlog.debug(
                f"群聊发送者已识别: chat={self.who!r} "
                f"resolved={resolved}/{len(candidates)}"
            )
        return messages

    def _sync_chat_cache(self) -> None:
        current = self.who
        # 独立窗口重绘/切前台的极短时刻，标题控件可能读成空字符串。
        # 这不是切换聊天，不能清空消息基线，否则下一轮会重放整页消息。
        if not current and self._cache_chat_name:
            return
        if current != self._cache_chat_name:
            self._cache_chat_name = current
            self._last_time = ""
            self._used_msg_ids.clear()
            self._last_signatures.clear()
            self._recent_signatures.clear()
            self._tail_message_id = ""
            self._tail_message_signature = ""
            self._visible_message_snapshot = ()
            self._visible_control_snapshot = ()
            self._delivery_direction_snapshot = None
            self._anchor_miss_snapshot = None
            self._anchor_miss_rounds = 0
            self._last_anchor_missing = False
            self._anchor_recovery_failures = 0
            self._anchor_recovery_last_attempt = 0.0
            self._last_anchor_recovery_result = {}
            self._anchor_missing_since = None
            self._anchor_wait_control_snapshot = ()

    @uilock
    def get_messages(
        self,
        resolve_group_senders: bool = True,
        *,
        probe_avatar_direction: bool = True,
        controls: list | None = None,
        _confirmed_directions: dict | None = None,
    ) -> list:
        """读取当前可见消息并解析为消息对象。

        监听初始化可关闭头像探测，只建立轻量基线；正常读取与原版一样
        逐条探测当前头像，不以可复用的 RuntimeId 跳过身份识别。
        """
        if controls is None:
            self._sync_chat_cache()
        result: list = []
        for control in (self.get_visible_messages() if controls is None else controls):
            try:
                raw_name = str(getattr(control, "Name", "") or "")
                msg_cls = classify(control)
                msg_type = getattr(msg_cls, "type", "other")
                content = parse_content(msg_type, raw_name)
                anchor_token = control_anchor_token(control)
                if _confirmed_directions and anchor_token in _confirmed_directions:
                    # Only the delivery reader can supply a proven old prefix.
                    # Never reuse sender names or identify new rows from this map.
                    direction = _confirmed_directions[anchor_token]
                    avatar_sender, direction_source = "", "confirmed_prefix"
                elif probe_avatar_direction:
                    direction, avatar_sender, direction_source = self._identity_for(control)
                else:
                    # A startup baseline records order, not sender identity. Do
                    # not screenshot or walk the group header for old messages.
                    direction, avatar_sender, direction_source = None, "", "baseline"
                if direction is None:
                    direction = self._direction_for_message(msg_type, raw_name, content)
                    direction_source = "fallback"
                msg = make_message(control, self, direction, self._last_time)
                msg.ui_anchor_token = anchor_token
                msg.direction_source = direction_source
                if getattr(msg, "is_time", False):
                    self._last_time = msg.content
                    msg.sender = "系统"
                else:
                    if controls is not None and direction == "friend" and avatar_sender:
                        msg.sender = avatar_sender
                    else:
                        msg.sender = self._sender_for(direction, msg.type) if probe_avatar_direction else ""
                    if direction == "friend" and not msg.sender:
                        msg.sender = avatar_sender
                result.append(msg)
            except Exception as exc:
                try:
                    raw_cls = getattr(control, "ClassName", "")
                    raw_name = getattr(control, "Name", "")
                except Exception:
                    raw_cls, raw_name = "", ""
                wxlog.warning(
                    f"解析消息失败: class={raw_cls!r} name={raw_name[:120]!r} error={exc}"
                )
                continue
        if resolve_group_senders:
            self._resolve_group_senders(result)
        return result

    def _consume_messages(self, messages: list) -> list:
        """更新缓存并返回本轮真正位于旧尾锚点之后的消息。"""
        had_anchor = bool(self._tail_message_id or self._tail_message_signature)
        if had_anchor and not messages:
            self._last_anchor_missing = True
            if getattr(self, "_anchor_missing_since", None) is None:
                self._anchor_missing_since = time.monotonic()
            self._anchor_wait_control_snapshot = ()
            return []
        self._last_anchor_missing = False
        current_snapshot = tuple(
            (
                str(getattr(message, "id", "") or ""),
                message_anchor_signature(message),
            )
            for message in messages
        )
        current_control_snapshot = tuple(
            tuple(
                getattr(
                    message,
                    "ui_anchor_token",
                    (
                        str(getattr(message, "id", "") or ""),
                        message_anchor_signature(message),
                    ),
                )
            )
            for message in messages
        )
        after_anchor, anchor_found = messages_after_anchor(
            messages,
            self._tail_message_id,
            self._tail_message_signature,
        )
        recovered_from_overlap = False
        if had_anchor and not anchor_found:
            after_anchor, anchor_found = messages_after_control_tail(
                messages, getattr(self, "_visible_control_snapshot", ()),
            )
        if had_anchor and not anchor_found:
            after_overlap, overlap_found = messages_after_previous_overlap(
                messages,
                getattr(self, "_visible_message_snapshot", ()),
            )
            if overlap_found:
                after_anchor = after_overlap
                anchor_found = True
                recovered_from_overlap = True
        new_messages, next_ids, next_signatures = diff_new_messages(
            messages,
            self._used_msg_ids,
            self._last_signatures,
        )
        self._used_msg_ids = next_ids
        self._last_signatures = next_signatures

        if had_anchor:
            # 锚点存在时，消息顺序比 RuntimeId 差分更可靠；找不到锚点
            # 则说明当前多半是虚拟化旧页，整轮不派发。
            new_messages = after_anchor if anchor_found else []

        if messages and (not had_anchor or anchor_found):
            tail = messages[-1]
            self._tail_message_id = str(getattr(tail, "id", "") or "")
            self._tail_message_signature = message_anchor_signature(tail)
            self._visible_message_snapshot = current_snapshot
            self._visible_control_snapshot = current_control_snapshot
            self._anchor_miss_snapshot = None
            self._anchor_miss_rounds = 0
            self._anchor_recovery_failures = 0
            self._anchor_missing_since = None
            self._anchor_wait_control_snapshot = ()
        elif messages and had_anchor and not anchor_found:
            snapshot = tuple(
                (
                    str(getattr(message, "id", "") or ""),
                    message_anchor_signature(message),
                )
                for message in messages
            )
            if snapshot == self._anchor_miss_snapshot:
                self._anchor_miss_rounds += 1
            else:
                self._anchor_miss_snapshot = snapshot
                self._anchor_miss_rounds = 1
            self._last_anchor_missing = True
            if getattr(self, "_anchor_missing_since", None) is None:
                self._anchor_missing_since = time.monotonic()
            self._anchor_wait_control_snapshot = current_control_snapshot
            # 绝不能像旧实现那样在三轮后静默重建基线；那会把整批消息永久
            # 吞掉。由 get_new_messages 启动有界历史恢复，失败时保留旧锚点
            # 并退避重试，同时留下明确告警。
            if self._anchor_miss_rounds in {1, 3}:
                wxlog.warning(
                    "消息尾锚点缺失，保留旧基线等待有界历史恢复: "
                    f"chat={self.who!r} rounds={self._anchor_miss_rounds} "
                    f"baseline={anchor_snapshot_summary(self._visible_control_snapshot)} "
                    f"visible={anchor_snapshot_summary(current_control_snapshot)} "
                    f"retry_in_sec={self._anchor_retry_in():.1f}"
                )

        if recovered_from_overlap and new_messages:
            wxlog.debug(
                f"消息尾锚点变形，已通过前序可见重叠恢复候选消息: count={len(new_messages)}"
            )

        now = time.monotonic()
        old_recent = dict(self._recent_signatures)
        for message in messages:
            self._recent_signatures[message_signature(message)] = now
        # 已找到尾锚点时，“锚点之后”本身就是确定的新消息，不能再按
        # 内容签名过滤，否则同一分钟连续发送相同文本会被误吞。只有尚未
        # 建立可靠锚点的兼容路径才使用短 TTL 签名过滤。
        if not (had_anchor and anchor_found):
            new_messages = filter_recent_signatures(new_messages, old_recent, now)
        if len(self._recent_signatures) > 600:
            cutoff = now - MESSAGE_SIGNATURE_TTL_SEC
            self._recent_signatures = {
                sig: ts for sig, ts in self._recent_signatures.items() if ts >= cutoff
            }
        return new_messages

    def _adopt_visible_baseline(self, messages: list) -> None:
        """在成功恢复后把最终底部页原子地设为下一轮监听基线。"""
        self._delivery_direction_snapshot = None
        if not messages:
            return
        tail = messages[-1]
        self._tail_message_id = str(getattr(tail, "id", "") or "")
        self._tail_message_signature = message_anchor_signature(tail)
        self._visible_message_snapshot = tuple(
            (
                str(getattr(message, "id", "") or ""),
                message_anchor_signature(message),
            )
            for message in messages
        )
        self._visible_control_snapshot = tuple(
            _message_control_token(message) for message in messages
        )
        self._used_msg_ids = {
            str(getattr(message, "id", "") or "")
            for message in messages
            if str(getattr(message, "id", "") or "")
        }
        self._last_signatures = {
            str(getattr(message, "id", "") or ""): message_signature(message)
            for message in messages
            if str(getattr(message, "id", "") or "")
        }
        now = time.monotonic()
        for message in messages:
            self._recent_signatures[message_signature(message)] = now
        self._anchor_miss_snapshot = None
        self._anchor_miss_rounds = 0
        self._last_anchor_missing = False
        self._anchor_recovery_failures = 0
        self._anchor_missing_since = None
        self._anchor_wait_control_snapshot = ()

    def _anchor_retry_in(self) -> float:
        failures = getattr(self, "_anchor_recovery_failures", 0)
        delay = (ANCHOR_RECOVERY_COOLDOWN_SEC if failures >= ANCHOR_RECOVERY_MAX_FAILURES
                 else min(15.0, float(2 ** failures)))
        return max(0.0, getattr(self, "_anchor_recovery_last_attempt", 0.0)
                   + delay - time.monotonic())

    def message_delivery_status(self) -> dict[str, object]:
        """Only cached Python state: health checks must never acquire UI locks."""
        missing = self._last_anchor_missing
        failures = self._anchor_recovery_failures
        since = getattr(self, "_anchor_missing_since", None)
        return {
            "state": ("cooldown" if failures >= ANCHOR_RECOVERY_MAX_FAILURES else "recovering")
                     if missing else "healthy",
            "anchor_missing": missing,
            "blocked_for_sec": round(max(0.0, time.monotonic() - since), 1) if since is not None else 0.0,
            "recovery_failures": failures,
            "retry_in_sec": round(self._anchor_retry_in(), 1) if missing else 0.0,
            "last_recovery": dict(self._last_anchor_recovery_result),
            "baseline": anchor_snapshot_summary(self._visible_control_snapshot),
            "delivery_sequence": self._delivery_sequence,
        }

    def _prepare_recovered_messages(self, visible_page: list, messages: list) -> None:
        """Resolve only new occurrences while their historical page is visible."""
        for index, message in enumerate(messages):
            parsed = self._read_history_message(message, visible_page)
            messages[index] = parsed
            for row_index, row in enumerate(visible_page):
                if row is message:
                    visible_page[row_index] = parsed
                    break

    def _recover_messages_after_missing_anchor(
        self,
        *,
        max_pages: int = 128,
        timeout: float = 30.0,
        wheel_times: int = 4,
        settle_interval: float = 0.12,
    ) -> tuple[list | None, list, dict[str, object]]:
        """找到旧锚点后逐页向下重建消息，并在末尾重新读取最新页。

        返回 ``(recovered_or_none, final_visible, metrics)``。``None`` 表示
        无法证明页间连续，调用方必须保留旧锚点；空列表则表示恢复成功但
        没有新增。翻页期间到达的消息会在最后一次 ``End`` 后并入。
        """
        previous_message_snapshot = tuple(self._visible_message_snapshot)
        previous_control_snapshot = tuple(self._visible_control_snapshot)
        result: dict[str, object] = {
            "status": "not_started",
            "messages_recovered": 0,
            "collection_pages": 0,
            "collection_elapsed_ms": 0.0,
            "baseline": anchor_snapshot_summary(previous_control_snapshot),
            "max_pages": max_pages,
            "timeout_sec": timeout,
        }
        if not previous_control_snapshot:
            result["status"] = "missing_control_snapshot"
            return None, [], result

        probe = self.find_previous_snapshot_upward(
            previous_control_snapshot,
            max_pages=max_pages,
            timeout=timeout,
            wheel_times=wheel_times,
            settle_interval=settle_interval,
            no_progress_rounds=2,
            restore_latest=False,
        )
        result["probe"] = probe
        final_visible: list = []
        accumulated: list = []
        recovered_start: int | None = None
        collection_started = time.monotonic()
        collection_ok = bool(probe.get("found"))
        stationary_rounds = 0
        result["collection_scrolls"] = 0
        result["recent_pages"] = []

        try:
            if collection_ok:
                anchor_page = self.get_messages(
                    resolve_group_senders=False, probe_avatar_direction=False,
                )
                result["collection_pages"] = 1
                # A delivered long row can have its avatar far above the
                # viewport. Raw tail identity already proves the boundary;
                # do not scroll back through that old row just to reparse it.
                candidates, anchor_found = messages_after_control_tail(
                    anchor_page, previous_control_snapshot,
                )
                if anchor_found:
                    recovered_start = len(anchor_page) - len(candidates)
                overlap_anchor = find_control_snapshot_overlap(
                    tuple(_message_control_token(m) for m in anchor_page),
                    previous_control_snapshot,
                )
                if not anchor_found and overlap_anchor is not None:
                    self._prepare_recovered_messages(
                        anchor_page, [anchor_page[overlap_anchor[1]]],
                    )
                if not anchor_found:
                    candidates, anchor_found = messages_after_anchor(
                        anchor_page,
                        self._tail_message_id,
                        self._tail_message_signature,
                    )
                if not anchor_found:
                    candidates, anchor_found = messages_after_previous_overlap(
                        anchor_page,
                        previous_message_snapshot,
                    )
                if not anchor_found:
                    result["status"] = "collection_anchor_lost"
                    collection_ok = False
                else:
                    self._prepare_recovered_messages(anchor_page, candidates)
                    accumulated = list(anchor_page)

            # Resolving an offscreen row can itself scroll the list. Upward
            # and downward scroll counts therefore cannot be paired. Continue
            # until the bottom is stationary, with explicit page/time bounds.
            for _ in range(max_pages * 2 if collection_ok else 0):
                if time.monotonic() - collection_started >= timeout * 2:
                    result["status"] = "collection_timeout"
                    collection_ok = False
                    break
                with ui_transaction(timeout=0.75):
                    if self.message_list is None or not self.message_list.Exists(0):
                        result["status"] = "window_unavailable"
                        collection_ok = False
                        break
                    before_scroll = self._read_control_anchor_snapshot()
                    before_position = self._message_scroll_position()
                    self._scroll_message_list(-max(1, int(wheel_times)))
                    result["collection_scrolls"] = int(result["collection_scrolls"]) + 1
                self._wait_message_page(before_scroll, timeout=max(0.35, settle_interval))
                page = self.get_messages(
                    resolve_group_senders=False, probe_avatar_direction=False,
                )
                after_position = self._message_scroll_position()
                result["collection_pages"] = int(result["collection_pages"]) + 1
                appended = messages_after_history_overlap(accumulated, page)
                page_metrics = {
                    "page": result["collection_pages"],
                    "before": anchor_snapshot_summary(before_scroll),
                    "after": anchor_snapshot_summary(tuple(_message_control_token(m) for m in page)),
                    "appended": None if appended is None else len(appended),
                    "moved": before_position != after_position,
                    "row_y": [(entry[1], entry[2]) for entry in after_position] if isinstance(after_position, tuple) else [],
                }
                result["recent_pages"] = (list(result["recent_pages"]) + [page_metrics])[-8:]
                wxlog.debug(f"消息恢复翻页: chat={self.who!r} metrics={page_metrics}")
                if appended is None:
                    result["status"] = "collection_page_gap"
                    result["gap_before"] = anchor_snapshot_summary(tuple(_message_control_token(m) for m in accumulated[-12:]))
                    result["gap_after"] = anchor_snapshot_summary(tuple(_message_control_token(m) for m in page))
                    wxlog.debug(f"恢复翻页未能衔接: chat={self.who!r} "
                                f"before={[_message_control_token(m) for m in accumulated[-8:]]!r} "
                                f"after={[_message_control_token(m) for m in page]!r}")
                    collection_ok = False
                    break
                self._prepare_recovered_messages(page, appended)
                accumulated.extend(appended)
                stationary_rounds = stationary_rounds + 1 if not appended and before_position == after_position else 0
                if stationary_rounds >= 2:
                    result["bottom_confirmed"] = True
                    break
            else:
                if collection_ok:
                    result["status"] = "collection_max_pages"
                    collection_ok = False
        except Exception as exc:
            result["status"] = "collection_error"
            result["error"] = f"{type(exc).__name__}: {exc}"
            collection_ok = False
        finally:
            try:
                with ui_transaction(timeout=0.75):
                    self._return_to_latest(wait_time=0.1)
                time.sleep(max(0.12, float(settle_interval)))
                final_visible = self.get_messages(
                    resolve_group_senders=False, probe_avatar_direction=False,
                )
            except Exception as exc:
                result["status"] = "restore_error"
                result["restore_error"] = f"{type(exc).__name__}: {exc}"
                collection_ok = False

        result["collection_elapsed_ms"] = round(
            (time.monotonic() - collection_started) * 1000,
            1,
        )
        if not collection_ok or not final_visible or not accumulated:
            if result["status"] == "not_started":
                result["status"] = str(probe.get("status") or "not_found")
            return None, final_visible, result

        final_appended = messages_after_history_overlap(accumulated, final_visible)
        if final_appended is None:
            result["status"] = "concurrent_bottom_gap"
            result["gap_before"] = anchor_snapshot_summary(tuple(_message_control_token(m) for m in accumulated[-12:]))
            result["gap_after"] = anchor_snapshot_summary(tuple(_message_control_token(m) for m in final_visible))
            return None, final_visible, result
        self._prepare_recovered_messages(final_visible, final_appended)
        accumulated.extend(final_appended)

        if recovered_start is not None:
            recovered, anchor_found = accumulated[recovered_start:], True
        else:
            recovered, anchor_found = messages_after_anchor(
                accumulated,
                self._tail_message_id,
                self._tail_message_signature,
            )
        if not anchor_found:
            recovered, anchor_found = messages_after_previous_overlap(
                accumulated,
                previous_message_snapshot,
            )
        if not anchor_found:
            result["status"] = "merged_anchor_lost"
            return None, final_visible, result
        result["status"] = "found"
        result["messages_recovered"] = len(recovered)
        return list(recovered), final_visible, result

    def get_new_messages(self) -> list:
        """返回自上次读取后出现的新消息。

        微信 4.x 消息列表是虚拟化列表，RuntimeId 会复用。因此综合使用
        RuntimeId、内容签名和上一轮尾消息锚点，避免旧页被重复回调。
        """
        with self._message_read_lock:
            return self._get_new_messages_serialized()

    @uilock
    def _visible_list_is_unchanged(self) -> bool:
        """Only skip delivery work when the complete raw message list is unchanged.

        This is the existing delivery baseline, never a sender/direction cache.
        New or changed rows still receive fresh avatar identification.
        """
        if self._last_anchor_missing:
            waiting = getattr(self, "_anchor_wait_control_snapshot", ())
            if not waiting or self._anchor_retry_in() <= 0:
                return False
            return tuple(control_anchor_token(row) for row in self.get_visible_messages()) == waiting
        if not self._visible_control_snapshot:
            return False
        snapshot = tuple(control_anchor_token(row) for row in self.get_visible_messages())
        return snapshot == self._visible_control_snapshot

    def _get_new_messages_serialized(self) -> list:
        if self._visible_list_is_unchanged():
            return []
        visible_messages = self._read_delivery_messages()
        messages = self._consume_messages(visible_messages)
        # Keep direction evidence only for this exact visible delivery baseline.
        # No sender names or live controls are retained for this optimization.
        if not self._last_anchor_missing:
            self._delivery_direction_snapshot = (
                tuple(_message_control_token(m) for m in visible_messages),
                {
                    _message_control_token(m): m.direction for m in visible_messages
                    if getattr(m, "direction_source", "") in {"avatar", "confirmed_prefix"}
                    and getattr(m, "direction", None) in {"friend", "self"}
                },
            )

        if (
            self._last_anchor_missing
            and self._anchor_retry_in() <= 0
        ):
            wxlog.warning(
                "消息尾锚点开始有界恢复: "
                f"chat={self.who!r} attempt={self._anchor_recovery_failures + 1} "
                f"after_cooldown={self._anchor_recovery_failures >= ANCHOR_RECOVERY_MAX_FAILURES} "
                f"baseline={anchor_snapshot_summary(self._visible_control_snapshot)}"
            )
            try:
                recovered, final_visible, recovery_result = (
                    self._recover_messages_after_missing_anchor()
                )
            except Exception as exc:
                recovered, final_visible = None, []
                recovery_result = {"status": "recovery_exception", "error": f"{type(exc).__name__}: {exc}"}
                wxlog.exception(f"消息尾锚点恢复异常: chat={self.who!r}")
            # Start backoff after the attempt finishes, including exceptions.
            # A slow failed attempt must not immediately start another one.
            self._anchor_recovery_last_attempt = time.monotonic()
            self._last_anchor_recovery_result = recovery_result
            if recovered is not None:
                self._delivery_direction_snapshot = None
                visible_messages = final_visible
                messages = recovered
                self._adopt_visible_baseline(final_visible)
                wxlog.warning(
                    "消息尾锚点历史恢复成功: "
                    f"chat={self.who!r} count={len(messages)} metrics={recovery_result}"
                )
            else:
                self._anchor_recovery_failures += 1
                if final_visible:
                    self._anchor_wait_control_snapshot = tuple(_message_control_token(m) for m in final_visible)
                wxlog.error(
                    "消息尾锚点历史恢复失败，保留基线并定时重试: "
                    f"chat={self.who!r} failures={self._anchor_recovery_failures} "
                    f"retry_in_sec={self._anchor_retry_in():.1f} metrics={recovery_result}"
                )

        # RuntimeId belongs to a recyclable UI row, so it must never become the
        # durable identity used by an asynchronous plugin.  Attach a UUID to
        # every delivered occurrence.  Direct images additionally capture their
        # visual/neighbor identity before sender probing opens any context menu.
        context_candidates = [
            message
            for message in messages
            if str(getattr(message, "type", "") or "") != "image"
            or getattr(message, "media_target_identity", None) is None
        ]
        attach_delivery_context(visible_messages, context_candidates)
        for message in messages:
            self._delivery_sequence += 1
            message.delivery_sequence = self._delivery_sequence
        messages = self._resolve_group_senders(messages)
        for message in messages:
            target_error = str(getattr(message, "media_target_error", "") or "")
            if target_error:
                wxlog.warning(
                    "图片消息身份快照未建立，后续下载将失败关闭: "
                    f"chat={self.who!r} delivery_id="
                    f"{getattr(message, 'delivery_id', '')} error={target_error}"
                )
        return messages

    @uilock
    def _read_delivery_messages(self) -> list:
        """Identify all new rows; skip avatar work only for a proven old prefix."""
        evidence = getattr(self, "_delivery_direction_snapshot", None)
        if (self._last_anchor_missing or not evidence or not evidence[1]
                or evidence[0] != tuple(self._visible_control_snapshot)):
            return self.get_messages(resolve_group_senders=False)
        try:
            self._sync_chat_cache()
            if evidence[0] != tuple(self._visible_control_snapshot):
                return self.get_messages(resolve_group_senders=False)
            rows = self.get_visible_messages()
            snapshot = tuple(control_anchor_token(row) for row in rows)
            boundary = confirmed_append_prefix(evidence[0], snapshot)
            directions = {token: evidence[1][token] for token in snapshot[:boundary]
                          if token in evidence[1]}
            if not boundary or not directions:
                return self.get_messages(resolve_group_senders=False)
            messages = self.get_messages(
                resolve_group_senders=False, controls=rows,
                _confirmed_directions=directions,
            )
            # UIA can recycle rows even while our desktop mutex is held. Reject
            # partial parses and any concurrent list mutation before committing.
            parsed_snapshot = tuple(_message_control_token(m) for m in messages)
            after, found = messages_after_anchor(
                messages, self._tail_message_id, self._tail_message_signature,
            )
            if (parsed_snapshot != snapshot or not found or after != messages[boundary:]
                    or tuple(control_anchor_token(row) for row in self.get_visible_messages()) != snapshot):
                wxlog.debug("增量身份校验未通过，回退完整识别")
                return self.get_messages(resolve_group_senders=False)
            wxlog.debug(
                f"监听增量身份识别: chat={self.who!r} visible={len(rows)} "
                f"old_avatar_skipped={len(directions)} new_rows={len(rows) - boundary}"
            )
            return messages
        except Exception as exc:
            wxlog.debug(f"增量身份读取失败，回退完整识别: {exc}")
            return self.get_messages(resolve_group_senders=False)

    @uilock
    def prime_message_cache(
        self,
        settle_time: float = 0.0,
        interval: float = 0.15,
        stable_rounds: int = 1,
    ) -> None:
        """初始化消息缓存，等可见列表稳定后再开始监听。

        ``settle_time`` 是最长等待时间；默认参数仍只采样一次，以保持
        普通 ``Chat`` 构造的性能。监听窗口创建/重绑时会要求多轮稳定。
        """
        self._delivery_direction_snapshot = None
        deadline = time.monotonic() + max(0.0, float(settle_time))
        required = max(1, int(stable_rounds))
        previous: tuple[tuple[str, str], ...] | None = None
        stable = 0
        while True:
            messages = self.get_messages(
                resolve_group_senders=False,
                probe_avatar_direction=False,
            )
            self._consume_messages(messages)
            snapshot = tuple(
                (
                    str(getattr(message, "id", "") or ""),
                    message_anchor_signature(message),
                )
                for message in messages
            )
            if snapshot == previous:
                stable += 1
            else:
                previous = snapshot
                stable = 1
            if stable >= required or time.monotonic() >= deadline:
                return
            time.sleep(max(0.01, min(float(interval), deadline - time.monotonic())))

    def _scroll_message_list(self, wheel_times: int) -> None:
        """Short UI transaction shared by history reads and anchor recovery."""
        with ui_transaction(timeout=0.75):
            message_list = self.message_list
            if message_list is None or not message_list.Exists(0):
                raise RuntimeError("历史消息窗口不可用")
            hwnd = int(message_list.GetTopLevelControl().NativeWindowHandle or 0)
            expected = int(getattr(self.root, "HWND", 0) or 0)
            if expected and hwnd != expected:
                raise RuntimeError("历史消息窗口身份发生变化")
            if not scroll_window(hwnd, message_list.BoundingRectangle, wheel_times):
                raise RuntimeError("历史消息定向滚动失败")

    def _wait_message_page(self, previous, *, timeout: float, deadline=None):
        """Wait for a changed page to settle; never hold UI lock while sleeping."""
        end = time.monotonic() + max(0.0, timeout)
        if deadline is not None:
            end = min(end, deadline)
        previous = tuple(previous)
        last = None
        stable_since = time.monotonic()
        while True:
            with ui_transaction(timeout=0.5):
                current = self._read_control_anchor_snapshot()
                position = self._message_scroll_position()
            state = (current, position)
            now = time.monotonic()
            if state != last:
                stable_since = now
            # Qt animates rows while their tokens stay unchanged. Reading at
            # the first repeated token set produced stale/zero rectangles and
            # even sent recovery scrolling in the wrong direction.
            if current and state == last and now - stable_since >= 0.08:
                return current
            if now >= end:
                return current
            last = state
            time.sleep(min(0.025, max(0.0, end - time.monotonic())))

    def _message_scroll_position(self):
        with ui_transaction(timeout=0.5):
            rows = self.get_visible_messages()
            if not rows:
                return ()
            return tuple((control_anchor_token(c), int(c.BoundingRectangle.top),
                          int(c.BoundingRectangle.bottom)) for c in (rows[0], rows[-1]))

    def _ensure_history_message_visible(self, message):
        # UIA exposes buffered rows outside the viewport. Parsing their avatars
        # before scrolling them into view is both slow and unreliable.
        deadline = time.monotonic() + 2.0
        while True:
            with ui_transaction(timeout=0.5):
                expected = _message_control_token(message)
                # A COM element can keep the old name/RuntimeId after its row
                # disappears, yet return a zero rectangle. Reacquire from the
                # live list; never interpret (0,0,0,0) as 'above the viewport'.
                matches = [row for row in self.get_visible_messages()
                           if control_anchor_token(row) == expected]
                if len(matches) != 1:
                    raise RuntimeError(
                        f"历史消息在滚动期间已变化: matches={len(matches)} "
                        f"target={anchor_snapshot_summary((expected,))}"
                    )
                message.control = matches[0]
                viewport = self.message_list.BoundingRectangle
                row = message.control.BoundingRectangle
                if row.right <= row.left or row.bottom <= row.top:
                    raise RuntimeError(
                        f"历史消息控件矩形无效: id={expected[0]} "
                        f"rect={(row.left, row.top, row.right, row.bottom)}"
                    )
                point = group_sender_head_point(message.control, "friend")
                y = point[1] if point is not None else int((row.top + row.bottom) // 2)
                if viewport.top + 2 <= y < viewport.bottom - 2:
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("历史消息无法滚动到可见区域")
                self._scroll_message_list(1 if y < viewport.top + 2 else -1)
            self._wait_message_page((), timeout=0.35, deadline=deadline)

    def _read_history_message(self, message, page):
        # Full identity/media work is performed once, while this occurrence is
        # still on its page. Runtime IDs alone are not durable message identity.
        self._ensure_history_message_visible(message)
        with ui_transaction(timeout=0.75):
            token = _message_control_token(message)
            if control_anchor_token(message.control) != token:
                raise RuntimeError("历史消息在读取前已变化")
            self._last_time = str(getattr(message, "time", "") or self._last_time)
            parsed = self.get_messages(controls=[message.control], resolve_group_senders=False)
            if len(parsed) != 1 or _message_control_token(parsed[0]) != token:
                raise RuntimeError("历史消息无法确认身份")
            attach_delivery_context(page, parsed)
            from mabowx.utils.history_time import history_timestamp
            source_time = parsed[0].content if getattr(parsed[0], "is_time", False) else parsed[0].time
            parsed[0].time = history_timestamp(source_time)
            return parsed[0]

    def get_history_messages(
        self, n: int, callback=None, interval: float = 0.2,
        speed: int = 1, goback: bool = True, timeout: float | None = None,
    ) -> list:
        """Read newest-to-oldest, return oldest-to-newest, without moving a cursor.

        The count n excludes system/time messages; callbacks see every
        object, including the object that returns CALLBACK_STOP_SIGN.
        """
        if not isinstance(n, int) or not isinstance(speed, int):
            raise ValueError("n 和 speed 必须为整数")
        if not 1 <= speed <= 100 or not math.isfinite(interval) or interval < 0:
            raise ValueError("speed 必须为 1..100，interval 必须为非负有限数")
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("timeout 必须为非负有限数")
        if n <= 0:
            return []
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._message_read_lock:
            saved_time = self._last_time
            collected = []
            frontier = None
            count = 0
            no_progress = 0
            scroll_moved = True
            def check_deadline():
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("timed out while loading message history")
            try:
                with ui_transaction(timeout=0.75):
                    self.refresh()
                expected_chat = self.who
                while True:
                    check_deadline()
                    if self.who != expected_chat:
                        raise RuntimeError("读取历史消息期间聊天已切换")
                    page = self.get_messages(resolve_group_senders=False,
                                             probe_avatar_direction=False,
                                             controls=self.get_visible_messages())
                    if not page:
                        raise RuntimeError("历史消息页面为空，无法证明连续性")
                    new = page
                    if frontier is not None:
                        new = messages_before_history_overlap(page, frontier)
                        if new is None:
                            raise RuntimeError("历史消息页间不连续，停止读取")
                    for message in reversed(new):
                        check_deadline()
                        parsed = self._read_history_message(message, page)
                        collected.append(parsed)
                        if str(parsed.type) not in {"time", "system"}:
                            count += 1
                        stopped = callback is not None and callback(parsed) == WxParam.CALLBACK_STOP_SIGN
                        if stopped or count >= n:
                            return list(reversed(collected))
                    no_progress = no_progress + 1 if not new and not scroll_moved else 0
                    if no_progress >= 2:
                        return list(reversed(collected))
                    frontier = page
                    snapshot = tuple(_message_control_token(m) for m in page)
                    position = self._message_scroll_position()
                    self._scroll_message_list(speed)
                    self._wait_message_page(snapshot, timeout=max(0.2, interval), deadline=deadline)
                    scroll_moved = self._message_scroll_position() != position
            finally:
                self._last_time = saved_time
                if goback:
                    with ui_transaction(timeout=0.75):
                        self._return_to_latest(wait_time=0.1)

    def _read_control_anchor_snapshot(self) -> tuple[tuple[str, str], ...]:
        """在调用方持有 UI 锁时读取轻量可见锚点。"""
        if self.message_list is None or not self.message_list.Exists(0):
            return ()
        try:
            return tuple(
                control_anchor_token(control)
                for control in self.message_list.GetChildren()
            )
        except Exception:
            return ()

    @uilock
    def get_control_anchor_snapshot(self) -> tuple[tuple[str, str], ...]:
        """返回当前可见页的轻量锚点快照，供独立恢复窗口使用。"""
        return self._read_control_anchor_snapshot()

    def find_previous_snapshot_upward(
        self,
        previous_snapshot: tuple[tuple[str, str], ...] | list[tuple[str, str]],
        *,
        max_pages: int = 8,
        timeout: float = 4.0,
        wheel_times: int = 4,
        settle_interval: float = 0.12,
        no_progress_rounds: int = 2,
        restore_latest: bool = True,
    ) -> dict[str, object]:
        """有限地向上翻页寻找上一轮任意可靠重叠消息。

        此方法应由当前聊天的单一读取线程或专用恢复窗口调用，不能和同一
        窗口的其他滚动操作并发。每一页只在“读取快照 + 滚轮”期间持有 UI
        锁，页间等待会释放锁；同时受页数、总时长和页面不再变化三重上限
        保护，因此不会无限向上翻页。
        """
        max_pages = max(1, int(max_pages))
        timeout = max(0.1, float(timeout))
        wheel_times = max(1, int(wheel_times))
        settle_interval = max(0.0, float(settle_interval))
        no_progress_rounds = max(1, int(no_progress_rounds))
        previous_snapshot = tuple(tuple(token) for token in previous_snapshot)
        started = time.monotonic()
        deadline = started + timeout
        result: dict[str, object] = {
            "status": "not_found",
            "found": False,
            "match_mode": "",
            "previous_index": -1,
            "current_index": -1,
            "pages_scanned": 0,
            "scrolls": 0,
            "items_scanned": 0,
            "scan_elapsed_ms": 0.0,
            "lock_hold_total_ms": 0.0,
            "lock_hold_max_ms": 0.0,
            "restore_elapsed_ms": 0.0,
        }
        last_page_fingerprint: tuple | None = None
        repeated_pages = 0

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result["status"] = "timeout"
                    break
                try:
                    with ui_transaction(timeout=min(0.5, max(0.01, remaining))):
                        lock_started = time.monotonic()
                        try:
                            snapshot = self._read_control_anchor_snapshot()
                            result["pages_scanned"] = int(result["pages_scanned"]) + 1
                            result["items_scanned"] = int(
                                result["items_scanned"]
                            ) + len(snapshot)
                            overlap = find_control_snapshot_overlap(
                                snapshot,
                                previous_snapshot,
                            )
                            if overlap is not None:
                                previous_index, current_index, match_mode = overlap
                                result.update(
                                    {
                                        "status": "found",
                                        "found": True,
                                        "match_mode": match_mode,
                                        "previous_index": previous_index,
                                        "current_index": current_index,
                                    }
                                )
                                break

                            page_fingerprint = (
                                tuple(signature for _id, signature in snapshot),
                                self._message_scroll_position(),
                            )
                            if page_fingerprint == last_page_fingerprint:
                                repeated_pages += 1
                            else:
                                repeated_pages = 0
                                last_page_fingerprint = page_fingerprint
                            if repeated_pages >= no_progress_rounds:
                                result["status"] = "no_progress"
                                break
                            if int(result["pages_scanned"]) >= max_pages:
                                result["status"] = "max_pages"
                                break
                            if time.monotonic() >= deadline:
                                result["status"] = "timeout"
                                break
                            if self.message_list is None or not self.message_list.Exists(0):
                                result["status"] = "window_unavailable"
                                break
                            self._scroll_message_list(wheel_times)
                            result["scrolls"] = int(result["scrolls"]) + 1
                        finally:
                            lock_hold_ms = (time.monotonic() - lock_started) * 1000
                            result["lock_hold_total_ms"] = round(
                                float(result["lock_hold_total_ms"]) + lock_hold_ms,
                                1,
                            )
                            result["lock_hold_max_ms"] = round(
                                max(float(result["lock_hold_max_ms"]), lock_hold_ms),
                                1,
                            )
                except TimeoutError:
                    result["status"] = "ui_busy"
                    break
                self._wait_message_page(snapshot, timeout=max(0.2, settle_interval),
                                        deadline=deadline)
        except Exception as exc:
            result["status"] = "error"
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            result["scan_elapsed_ms"] = round(
                (time.monotonic() - started) * 1000,
                1,
            )
            if restore_latest:
                restore_started = time.monotonic()
                try:
                    with ui_transaction(timeout=0.75):
                        self._return_to_latest(wait_time=0.1)
                except Exception as exc:
                    result["restore_error"] = f"{type(exc).__name__}: {exc}"
                result["restore_elapsed_ms"] = round(
                    (time.monotonic() - restore_started) * 1000,
                    1,
                )
        return result

    def _return_to_latest(self, wait_time: float = 0.5) -> None:
        """把消息列表滚回最新位置。

        不点击消息列表内容，避免误触卡片/图片等消息；优先 SetFocus + End。
        """
        hwnd = int(getattr(self.root, "HWND", 0) or 0)
        if hwnd:
            import win32gui
            if self.message_list is None or not self.message_list.Exists(0):
                raise RuntimeError("消息列表不存在，无法返回最新位置")
            self.message_list.SetFocus()
            # Target End at this window; global SendKeys can reach a
            # different foreground window after a callback or avatar probe.
            win32gui.PostMessage(hwnd, 0x100, 0x23, 0)
            win32gui.PostMessage(hwnd, 0x101, 0x23, 0)
            time.sleep(max(0.15, float(wait_time)))
            return
        try:
            if self.message_list is not None and self.message_list.Exists(0):
                self.message_list.SetFocus()
                self.message_list.SendKeys(
                    "{End}",
                    waitTime=max(0.0, float(wait_time)),
                )
                return
        except Exception:
            pass
        try:
            if self.message_list is not None and self.message_list.Exists(0):
                self.message_list.WheelDown(wheelTimes=40, waitTime=0.1)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 消息列表（最小校验/旧接口）
    # ------------------------------------------------------------------

    @uilock
    def get_visible_messages(self) -> list[Any]:
        message_list = self.message_list
        if message_list is None:
            return []
        try:
            return list(message_list.GetChildren())
        except Exception:
            return []

    def last_message_names(self, limit: int = 5) -> list[str]:
        names = []
        for item in reversed(self.get_visible_messages()):
            try:
                name = str(item.Name or "")
                if name:
                    names.append(name)
            except Exception:
                continue
            if len(names) >= limit:
                break
        return names

    def has_text_in_messages(self, text: str) -> bool:
        for item in self.get_visible_messages():
            try:
                if text in str(item.Name or ""):
                    return True
            except Exception:
                continue
        return False
