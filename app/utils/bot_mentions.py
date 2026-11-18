"""群内机器人 @ 别名的统一匹配逻辑。"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, List, Optional


_MENTION_SUFFIX = r"(?=$|[\s\u2005,.，。!！?？:：;；、])"


def _clean_name(value: Any) -> str:
    return str(value or "").strip().lstrip("@").strip()


def _stored_aliases(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        parsed = value
    else:
        try:
            parsed = json.loads(str(value or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = []
    if not isinstance(parsed, list):
        return []
    return [_clean_name(item) for item in parsed if _clean_name(item)]


def bot_names_for_user(user: Any, global_name: str) -> List[str]:
    """返回当前群可接受的 @ 别名，按展示优先级排序。

    微信群详情的精确读取值优先，但手动值与全局值仍保留为别名，
    让昵称变更期间的旧 @ 也能继续触发。
    """
    names: List[str] = []
    auto_enabled = bool(getattr(user, "bot_group_nickname_auto_enabled", True))
    detected = getattr(user, "bot_group_nickname_detected", None) if auto_enabled else None
    aliases = _stored_aliases(getattr(user, "bot_group_nickname_aliases", None)) if auto_enabled else []
    candidates: Iterable[Any] = (
        detected,
        getattr(user, "bot_group_nickname", None),
        *aliases,
        global_name,
    )
    for value in candidates:
        name = _clean_name(value)
        if name and name not in names:
            names.append(name)
    return names


def bot_quote_names_for_user(user: Any, global_name: str) -> List[str]:
    """Return authoritative names that may identify a quoted bot message.

    A quoted-message author is an identity signal, not an ``@`` compatibility
    signal.  Use the current nickname read from WeChat when automatic
    calibration is enabled, then the manual fallback and the account's global
    name.  Historical aliases remain useful for forgiving mention matching but
    are intentionally excluded from strong quote attribution.
    """
    names: List[str] = []
    auto_enabled = bool(getattr(user, "bot_group_nickname_auto_enabled", True))
    detected = getattr(user, "bot_group_nickname_detected", None) if auto_enabled else None
    for value in (detected, getattr(user, "bot_group_nickname", None), global_name):
        name = _clean_name(value)
        if name and name not in names:
            names.append(name)
    return names


def tickle_self_flags(
    actor: Any,
    target: Any,
    bot_names: Iterable[str],
) -> tuple[bool, bool]:
    """Return ``(from_self, to_self)`` for a structured tickle notice.

    WeChat may render the local account as ``我``/``你`` depending on the
    notice form.  Group notices normally use the current group nickname, so
    all authoritative/compatible bot names supplied by ``bot_names_for_user``
    are accepted as well.
    """

    actor_name = _clean_name(actor)
    target_name = _clean_name(target)
    own_names = {_clean_name(name) for name in bot_names if _clean_name(name)}
    from_self = actor_name == "我" or actor_name in own_names
    to_self = target_name in {"我", "你"} or target_name in own_names
    return from_self, to_self


def find_bot_mention(content: Any, names: Iterable[str]) -> Optional[str]:
    """返回消息中命中的机器人别名，未命中则返回 None。"""
    text = str(content or "")
    if not text:
        return None
    # 先比较长名称，防止“小助手”抢先命中“小助手Pro”。
    normalized = sorted({_clean_name(name) for name in names if _clean_name(name)}, key=len, reverse=True)
    for name in normalized:
        if re.search(rf"@{re.escape(name)}{_MENTION_SUFFIX}", text):
            return name
    return None


def strip_bot_mentions(content: Any, names: Iterable[str], *, preserve_if_empty: bool = True) -> str:
    """移除所有已知机器人 @ 别名及微信插入的分隔空白。"""
    original = str(content or "")
    cleaned = original
    normalized = sorted({_clean_name(name) for name in names if _clean_name(name)}, key=len, reverse=True)
    for name in normalized:
        cleaned = re.sub(
            rf"@{re.escape(name)}{_MENTION_SUFFIX}[\s\u2005]*",
            "",
            cleaned,
        )
    return cleaned.strip() or (original.strip() if preserve_if_empty else "")
