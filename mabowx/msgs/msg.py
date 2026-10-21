"""消息类型识别与解析。"""

from __future__ import annotations

import re

from mabowx.param import WxParam

from . import mtype
from .mattr import FriendMessage, SelfMessage

TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
DATE_TIME_RE = re.compile(
    r"^(?:\d{1,4}[/-]\d{1,2}[/-]\d{1,2}|(?:\d{4}年)?\d{1,2}月\d{1,2}日|昨天|前天|今天|星期[一二三四五六日天])\s+\d{1,2}:\d{2}$"
)
VOICE_RE = re.compile(r"^\[语音\]|^语音\s*\d", re.IGNORECASE)
IMAGE_RE = re.compile(r"^(?:\[图片\]|图片\b)", re.IGNORECASE)
VIDEO_RE = re.compile(r"^(?:\[视频\]|视频\b)", re.IGNORECASE)
FILE_RE = re.compile(r"^文件\s*\n?", re.IGNORECASE)
LOCATION_RE = re.compile(r"^(?:\[位置\]|位置\s*\S)", re.IGNORECASE)
LINK_RE = re.compile(r"^\[链接\]", re.IGNORECASE)
EMOTION_RE = re.compile(r"^(?:\[动画表情\]|动画表情)", re.IGNORECASE)
MERGE_RE = re.compile(r"^(?:\[聊天记录\]|聊天记录)", re.IGNORECASE)
PERSONAL_CARD_RE = re.compile(r"^\[名片\]", re.IGNORECASE)
NOTE_RE = re.compile(r"^(?:\[笔记\]|笔记\b|微信笔记)", re.IGNORECASE)
MINIAPP_RE = re.compile(r"^(?:\[小程序\]|小程序|\[视频号\]|视频号)", re.IGNORECASE)
QUOTE_RE = mtype.QUOTE_PATTERN


_CLASS_TO_TYPE = {
    "mmui::ChatVoiceItemView": mtype.VoiceMessage,
    "mmui::ChatNoteCardItemView": mtype.NoteMessage,
    "mmui::ChatPersonalCardItemView": mtype.PersonalCardMessage,
    "mmui::ChatSystemInfoItemView": mtype.SystemMessage,
    "mmui::ChatAppReaderItemView": mtype.OfficialMessage,
}
_TICKLE_SYSTEM_CLASSES = {
    "mmui::ChatItemView",
    "mmui::ChatSystemInfoItemView",
}


def classify(control) -> type[mtype.BaseMessage]:
    """根据 ClassName + Name 判断消息对象类型。

    校准来源：微信 4.1.12.55 实测样本。
    """
    class_name = str(getattr(control, "ClassName", "") or "")
    name = str(getattr(control, "Name", "") or "")
    stripped = name.strip()

    # 真机样本（微信 4.1.12）：ListItemControl / mmui::ChatItemView，
    # Name='"Gerry" 拍了拍 "刘局"'。只在系统消息形态的控件上进行
    # 全串解析，避免把用户发送的“他拍了拍我”普通文本误分成系统消息。
    if (
        class_name in _TICKLE_SYSTEM_CLASSES
        and mtype.parse_tickle_content(name) is not None
    ):
        return mtype.TickleMessage

    if class_name == "mmui::ChatTextItemView":
        if QUOTE_RE.search(name):
            return mtype.QuoteMessage
        return mtype.TextMessage

    if class_name in _CLASS_TO_TYPE:
        return _CLASS_TO_TYPE[class_name]

    if class_name == "mmui::ChatBubbleReferItemView":
        # 实测 4.1.12 中图片/视频/动画表情/引用都会使用该类名，
        # 必须继续用 Name 前缀区分。
        if IMAGE_RE.match(stripped):
            return mtype.ImageMessage
        if VIDEO_RE.match(stripped):
            return mtype.VideoMessage
        if EMOTION_RE.match(stripped):
            return mtype.EmotionMessage
        if QUOTE_RE.search(name):
            return mtype.QuoteMessage
        return mtype.QuoteMessage

    if class_name == "mmui::ChatBubbleItemView":
        if FILE_RE.match(stripped):
            return mtype.FileMessage
        if IMAGE_RE.match(stripped):
            return mtype.ImageMessage
        if VIDEO_RE.match(stripped):
            return mtype.VideoMessage
        if LOCATION_RE.match(stripped):
            return mtype.LocationMessage
        if LINK_RE.match(stripped):
            # 普通链接卡片和公众号卡片统一为 card，后续统一提取 URL。
            return mtype.CardMessage
        if EMOTION_RE.match(stripped):
            return mtype.EmotionMessage
        if MERGE_RE.match(stripped):
            return mtype.MergeMessage
        if NOTE_RE.match(stripped):
            return mtype.NoteMessage
        if MINIAPP_RE.match(stripped):
            return mtype.MiniAppMessage
        return mtype.OtherMessage

    if class_name == "mmui::ChatItemView":
        if TIME_RE.match(stripped) or DATE_TIME_RE.match(stripped):
            return mtype.TimeMessage
        return mtype.OtherMessage
    return mtype.OtherMessage


def parse_content(msg_type: str, raw: str) -> str:
    """把控件原始 Name 转成消息内容。"""
    if msg_type in ("text", "quote", "system", "official", "other"):
        return raw
    if msg_type == "file":
        # 原始格式：文件\n<文件名>\n<大小>\n<来源>
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        return lines[1] if len(lines) >= 2 else raw
    if msg_type == "time":
        return raw.strip()
    # 图片/视频/语音等占位类型
    return raw


def make_message(
    control,
    parent,
    direction: str | None,
    time_text: str,
) -> mtype.BaseMessage:
    """从 UIA 控件构造消息对象。"""
    raw = str(getattr(control, "Name", "") or "")
    cls = classify(control)
    if direction == "friend" and issubclass(cls, mtype.HumanMessage):
        cls = type(f"Friend{cls.__name__}", (FriendMessage, cls), {})
    elif direction == "self" and issubclass(cls, mtype.HumanMessage):
        cls = type(f"Self{cls.__name__}", (SelfMessage, cls), {})
    msg = cls(control=control, parent=parent)
    # QuoteMessage parses the reply text and quoted payload separately in its
    # constructor.  Overwriting content here used to erase quote_content and
    # made the main program unable to cache an on-demand quoted-image download.
    if msg.type != "quote":
        msg.content = parse_content(msg.type, raw)
    else:
        msg.quote_nickname = str(getattr(msg, "quote_nickname", "") or "")
        msg.quote_content = str(getattr(msg, "quote_content", "") or "")
    msg.mtype = msg.type
    msg.sender_remark = ""
    msg.direction = direction
    # attr 表示消息方向或系统标记。
    if issubclass(cls, FriendMessage):
        msg.attr = "friend"
    elif issubclass(cls, SelfMessage):
        msg.attr = "self"
    elif issubclass(cls, mtype.SystemMessage):
        msg.attr = "system"
    else:
        msg.attr = direction or "unknown"
    msg.time = time_text
    identity_content = msg.content
    if msg.type == "quote":
        identity_content = (
            f"{msg.content}|{msg.quote_nickname}|{msg.quote_content}"
        )
    msg.id = _make_id(control, msg.type, identity_content)
    if WxParam.MESSAGE_HASH:
        msg.hash = msg._make_hash(f"{msg.type}|{raw}|{msg.time}|{msg.direction}")
    return msg


def _make_id(control, msg_type: str, content: str) -> str:
    try:
        runtime_id = "-".join(str(part) for part in control.GetRuntimeId())
    except Exception:
        runtime_id = ""
    try:
        rect = control.BoundingRectangle
        rect_id = f"{rect.left}-{rect.top}-{rect.right}-{rect.bottom}"
    except Exception:
        rect_id = ""
    if runtime_id:
        return runtime_id
    return f"{msg_type}|{rect_id}|{content[:20]}"
