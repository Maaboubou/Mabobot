from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class PendingChoice(Generic[T]):
    chat_name: str
    sender_key: str
    choices: tuple[T, ...]
    expires_at: float


class SessionStore(Generic[T]):
    def __init__(self, *, ttl_seconds: int = 60, max_choices: int = 5, clock: Callable[[], float] = time.monotonic):
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_choices = max(1, min(5, int(max_choices)))
        self.clock = clock
        self._items: dict[tuple[str, str], PendingChoice[T]] = {}

    def put(self, chat_name: str, sender_key: str, choices: tuple[T, ...]) -> PendingChoice[T]:
        item = PendingChoice(
            chat_name=chat_name,
            sender_key=sender_key,
            choices=tuple(choices[: self.max_choices]),
            expires_at=self.clock() + self.ttl_seconds,
        )
        self._items[(chat_name, sender_key)] = item
        return item

    def get(self, chat_name: str, sender_key: str) -> PendingChoice[T] | None:
        key = (chat_name, sender_key)
        item = self._items.get(key)
        if item and item.expires_at > self.clock():
            return item
        if item:
            self._items.pop(key, None)
        return None

    def peek(self, chat_name: str, sender_key: str) -> PendingChoice[T] | None:
        return self._items.get((chat_name, sender_key))

    def pop(self, chat_name: str, sender_key: str) -> PendingChoice[T] | None:
        return self._items.pop((chat_name, sender_key), None)

    def has_chat(self, chat_name: str) -> bool:
        now = self.clock()
        expired = [key for key, item in self._items.items() if item.expires_at <= now]
        for key in expired:
            self._items.pop(key, None)
        return any(item.chat_name == chat_name for item in self._items.values())

    def clear(self) -> None:
        self._items.clear()

    def chats(self) -> set[str]:
        return {item.chat_name for item in self._items.values()}


def parse_choice(
    message: str,
    option_count: int,
    *,
    bot_mention_name: str | None = None,
) -> int | str | None:
    text = str(message or "").strip()
    mention_name = str(bot_mention_name or "").strip().lstrip("@").strip()
    if mention_name:
        text = re.sub(
            rf"^@{re.escape(mention_name)}(?=$|[\s\u2005,.，。!！?？:：;；、])[\s\u2005]*",
            "",
            text,
            count=1,
        ).strip()
    if text in {"取消", "算了", "不用了"} or text.casefold() == "cancel":
        return "cancel"
    if text.isdigit():
        value = int(text)
        if 1 <= value <= option_count:
            return value - 1
    return None
