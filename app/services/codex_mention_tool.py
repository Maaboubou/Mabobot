"""Per-reply mention intent; only normal delivery may send the actual message."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import threading

INSTRUCTIONS = """需要提醒、点名回答或明确通知本群某位成员时，可自主调用 wechat.mention_reply，为本轮最终回复设置真实微信 @；无需每次回复都 @。使用聊天资料中的完整群昵称，不猜测身份。普通文本里的 @昵称 不会触发真正提醒。工具只设置本轮收件成员，不会立刻发送；调用后仍须正常输出完整最终回复。多次调用会替换名单，空列表取消；多人回复只在第一段 @ 一次。不支持 @所有人。工具返回 prepared 不代表已经发送，实际发送时会按昵称搜索群成员，优先精确匹配，备注不同则接受唯一搜索结果；多个候选或找不到会拒绝发送。"""
_context = ContextVar("wechat_reply_mentions", default=None)


@dataclass
class MentionPlan:
    chat: str
    users: tuple[str, ...] = ()
    lock: object = field(default_factory=threading.RLock)
    delivery_chat: str = ""


@contextmanager
def mention_context(plan):
    token = _context.set(plan)
    try:
        yield plan
    finally:
        _context.reset(token)


def current_mention_context(chat=None):
    plan = _context.get()
    return plan if plan is not None and (chat is None or plan.chat == chat) else None


def dynamic_tool_specs():
    return [{"type": "namespace", "name": "wechat", "description": "当前聊天回复的微信原生能力。", "tools": [{
        "type": "function", "name": "mention_reply", "description": INSTRUCTIONS,
        "inputSchema": {"type": "object", "properties": {
            "members": {"type": "array", "items": {"type": "string"}, "maxItems": 20,
                        "description": "本群成员的完整昵称；空列表取消本轮 @"}},
            "required": ["members"], "additionalProperties": False},
    }]}]


def normalize_members(members):
    if not isinstance(members, (list, tuple)) or len(members) > 20:
        raise ValueError("members 必须是最多 20 位成员的数组")
    normalized = []
    for member in members:
        if not isinstance(member, str):
            raise ValueError("成员昵称必须是字符串")
        name = member.strip()
        if not name or len(name) > 128 or any(ord(c) < 32 for c in name) or name.lstrip('@').casefold() in {"所有人", "全体成员", "all", "everyone"}:
            raise ValueError("请提供有效的具体成员昵称，不支持 @所有人")
        if name not in normalized:
            normalized.append(name)
    return tuple(normalized)


def execute(tool, arguments, plan, chat):
    if plan is None or plan.chat != chat:
        raise ValueError("此任务没有绑定当前聊天的回复，不能设置 @")
    args = json.loads(arguments) if isinstance(arguments, str) else arguments
    if tool != "mention_reply" or not isinstance(args, dict) or set(args) != {"members"}:
        raise ValueError("无效的微信 @ 工具参数")
    members = args["members"]
    if not isinstance(members, list) or len(members) > 20:
        raise ValueError("members 必须是最多 20 位成员的数组")
    normalized = normalize_members(members)
    with plan.lock:
        plan.users = tuple(normalized)
    return {"status": "prepared", "members": list(normalized), "delivery_chat": plan.delivery_chat or plan.chat,
            "message": "已设置本轮回复的 @ 成员；请继续输出完整最终回复，发送时将核验真实 @ 对象。"}
