"""Token-budgeted context helpers for the core Assistant.

This module renders bounded context. Durable raw messages live in
``app.history.store`` and are retrieved on demand.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


class ChatContextManager:
    """Estimate, trim and render chat context without owning memory state."""

    def estimate_tokens(self, text: Any) -> int:
        """Conservative token estimate that works well for mixed Chinese text."""
        if text is None:
            return 0
        value = str(text)
        if not value:
            return 0

        cjk_chars = sum(1 for ch in value if "\u4e00" <= ch <= "\u9fff")
        other_chars = len(value) - cjk_chars
        return max(1, cjk_chars + (other_chars + 3) // 4)

    def estimate_message_tokens(self, message: Dict[str, Any]) -> int:
        sender = message.get("sender", "")
        content = message.get("content", "")
        time_str = message.get("time", "")
        return self.estimate_tokens(f"[{time_str}] [{sender}]: {content}") + 4

    def truncate_text_to_budget(
        self,
        text: str,
        token_budget: int,
        notice: str = "内容因上下文预算限制已截断",
    ) -> str:
        if not text or token_budget <= 0:
            return ""
        if self.estimate_tokens(text) <= token_budget:
            return text

        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if self.estimate_tokens(text[:mid]) <= token_budget:
                low = mid
            else:
                high = mid - 1

        trimmed = text[:low].rstrip()
        if len(trimmed) < 20:
            return f"（{notice}）"
        return trimmed + f"\n\n（{notice}）"

    def format_messages(self, messages: List[Dict[str, Any]]) -> str:
        if not messages:
            return "（暂无近期原始聊天记录）"

        lines = []
        for message in messages:
            sender = message.get("sender", "User")
            content = message.get("content", "")
            time_str = message.get("time", "")
            cursor = message.get("_log_cursor")
            prefix = f"[#{cursor}] " if cursor else ""
            if time_str:
                lines.append(f"{prefix}[{time_str}] [{sender}]: {content}")
            else:
                lines.append(f"{prefix}[{sender}]: {content}")
        return "\n".join(lines)

    def select_recent_messages(
        self,
        messages: List[Dict[str, Any]],
        token_budget: int,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Take the newest messages that fit while preserving their order."""
        if token_budget <= 0 or not messages:
            return [], 0

        selected: List[Dict[str, Any]] = []
        used = 0
        for message in reversed(messages):
            cost = self.estimate_message_tokens(message)
            if selected and used + cost > token_budget:
                break
            selected.append(message)
            used += cost
            if used >= token_budget:
                break

        selected.reverse()
        return selected, used

    def select_prefix_messages(
        self,
        messages: List[Dict[str, Any]],
        token_budget: int,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Take the oldest contiguous prefix for cursor-based ingestion."""
        if token_budget <= 0 or not messages:
            return [], 0

        selected: List[Dict[str, Any]] = []
        used = 0
        for message in messages:
            cost = self.estimate_message_tokens(message)
            if selected and used + cost > token_budget:
                break
            selected.append(message)
            used += cost
            if used >= token_budget:
                break
        return selected, used
