"""Chat attribution shared by message handlers and managed background work."""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Mapping

_usage_context = ContextVar("llm_usage_context", default=None)


@contextmanager
def usage_context(metadata: Mapping[str, Any]):
    token = _usage_context.set(dict(metadata))
    try:
        yield
    finally:
        _usage_context.reset(token)


def resolve_usage_subject(metadata=None):
    inherited = dict(_usage_context.get() or {})
    explicit = dict(metadata or {})
    if explicit.get("usage_scope") == "system" and not explicit.get("chat_name") and not explicit.get("user_id"):
        return {"key": "system", "name": "系统任务", "kind": "system"}
    if explicit.get("user_id") and explicit["user_id"] != inherited.get("user_id"):
        inherited = {}
    name = str(explicit.get("chat_name") or inherited.get("chat_name") or "").strip()
    # An explicit destination must never inherit a different chat's identity.
    if name and name != inherited.get("chat_name"):
        inherited = {}
    values = {**inherited, **{k: v for k, v in explicit.items() if v is not None and v != ""}}
    name = str(values.get("chat_name") or "").strip()
    user_id = values.get("user_id")
    kind = values.get("chat_type") or ("group" if values.get("is_group") else "user" if "is_group" in values else "unknown")
    if name and not user_id:
        try:
            from app.models.base import SessionLocal
            from app.models.user_permission import WeChatUser
            with SessionLocal() as db:
                user = db.query(WeChatUser.id, WeChatUser.is_group).filter(WeChatUser.chat_name == name).first()
                if user:
                    user_id, is_group = user
                    kind = "group" if is_group else "user"
        except Exception:
            # Missing/deleted chat records still have an explicit, honest fallback.
            pass
    if user_id:
        return {"key": f"wechat:{user_id}", "name": name or str(user_id), "kind": kind}
    if name:
        return {"key": f"name:{name}", "name": name, "kind": kind}
    if values.get("usage_scope") == "system":
        return {"key": "system", "name": "系统任务", "kind": "system"}
    return {"key": "unknown", "name": "未归属", "kind": "unknown"}
