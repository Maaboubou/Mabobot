"""Build identities from host-owned chat records, not usage tags or MCP args."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import uuid

from app.services.execution_contracts import ExecutionError, TaskContext


@dataclass(frozen=True)
class AdmissionSource:
    chat_id: int
    actor: str
    request_id: str
    component: str
    source_kind: str = "chat"
    delivery_chat_id: int | None = None
    engine: str = "follow"
    profile_id: str = ""
    permission_key: str = ""


_source = ContextVar("engine_admission_source", default=None)


def current_admission_source():
    return _source.get()


@contextmanager
def admission_source(source):
    token = _source.set(source)
    try:
        yield
    finally:
        _source.reset(token)


@contextmanager
def event_admission_source(event, component, permission_key=""):
    chat_id = event.context.get("chat_id")
    source = None
    if chat_id:
        source = AdmissionSource(int(chat_id), str(event.data.get("sender") or event.data.get("chat_name") or ""),
            str(event.data.get("message_id") or event.id), component, permission_key=permission_key)
    with admission_source(source):
        yield


def _session_factory():
    from app.services.plugin_config_context import current_config_scope
    from app.models.base import SessionLocal
    scope = current_config_scope()
    return (scope.session_factory if scope else None) or SessionLocal


def _chat_id(value):
    if not str(value).startswith("wechat:"):
        raise ValueError("A stable WeChat chat ID is required")
    return int(str(value).split(":", 1)[1])


def authorize_context(context):
    from app.models.user_permission import WeChatUser, UserPermission
    if context.source_kind == "system":
        # Only an explicitly admitted console probe is a chatless system job.
        if context.component != "engine_probe" or context.actor_id != "administrator" or not context.authorization_ref.startswith("engine-probe:"):
            return False
        from app.services import execution_service
        service = execution_service._service
        if service is None:
            return False
        try:
            registered, _ = service.store.contracts(context.run_id)
            return registered == context
        except KeyError:
            return False
    try:
        source_id = _chat_id(context.source_chat_id)
        with _session_factory()() as db:
            source = db.query(WeChatUser).filter(WeChatUser.id == source_id).first()
            prefix = f"chat:{source_id}:"
            if not source or not source.listening_enabled or not context.authorization_ref.startswith(prefix):
                return False
            grant = context.authorization_ref[len(prefix):]
            if grant.split('#', 1)[0].rsplit('/', 1)[-1] != context.component.rsplit('/', 1)[-1]:
                return False
            if str(source.policy_version) != context.permission_revision:
                return False
            ignored = json.loads(source.sender_blacklist or "[]")
            if context.actor_id in ignored:
                return False
            if context.component in {"assistant", "schedule_role_review"}:
                allowed = source.assistant_enabled
            else:
                allowed = any(p.plugin_name in {grant, grant.rsplit('/', 1)[-1]} for p in source.permissions)
                if not allowed and '#' not in grant:
                    allowed = any(p.plugin_name.split('#', 1)[0].rsplit('/', 1)[-1] == grant.rsplit('/', 1)[-1] for p in source.permissions)
                if not allowed and context.component == "Weekly":
                    allowed = any(p.plugin_name.rsplit('/', 1)[-1] == 'Weekly#push' for p in source.permissions)
            if not allowed:
                return False
            if context.delivery_chat_id and context.delivery_chat_id != context.source_chat_id:
                from app.services.scheduled_tasks import get_scheduled_tasks
                get_scheduled_tasks().authorize(_chat_id(context.delivery_chat_id))
            return True
    except (ValueError, TypeError, KeyError):
        return False


def make_context(component, task_type, *, timeout=600, delivery=True, source=None):
    from app.models.user_permission import WeChatUser
    source = source or current_admission_source()
    if source is None or source.component != component:
        raise ExecutionError("source_missing", "This model task needs a trusted business admission source")
    with _session_factory()() as db:
        chat = db.query(WeChatUser).filter(WeChatUser.id == source.chat_id).first()
        if not chat:
            raise ExecutionError("source_missing", "Source chat no longer exists")
        context = TaskContext.create(request_id=source.request_id + ":" + task_type,
            source_kind=source.source_kind, source_chat_id=f"wechat:{chat.id}", actor_id=source.actor,
            component=component, task_type=task_type, delivery_chat_id=f"wechat:{source.delivery_chat_id or chat.id}" if delivery else "",
            delivery_owner=component if delivery else "", authorization_ref=f"chat:{chat.id}:{source.permission_key or component}",
            permission_revision=str(chat.policy_version), timeout=timeout,
            purpose="background" if source.source_kind == "scheduled" else "foreground")
    if not authorize_context(context):
        raise ExecutionError("permission_denied", "Task is not authorized for this chat")
    return context


def chat_name(context, *, delivery=False):
    from app.models.user_permission import WeChatUser
    with _session_factory()() as db:
        chat = db.query(WeChatUser).filter(WeChatUser.id == _chat_id(context.delivery_chat_id if delivery else context.source_chat_id)).first()
        if not chat:
            raise ExecutionError("source_missing", "Chat no longer exists")
        return chat.chat_name
