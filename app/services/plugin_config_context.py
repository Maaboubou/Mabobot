"""Isolated, per-invocation plugin settings shared with managed background work."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock


@dataclass
class ConfigScope:
    chat_id: int | None
    session_factory: object = None
    snapshots: dict = field(default_factory=dict)
    attributes: dict = field(default_factory=dict)
    lock: object = field(default_factory=RLock, repr=False)

    def values(self, plugin_name, config, path=None):
        with self.lock:
            key = plugin_name
            if key not in self.snapshots:
                from app.models.base import SessionLocal
                from app.models.user_permission import WeChatUser
                from app.services.plugin_chat_config_service import (
                    PluginChatConfigError, PluginChatConfigService,
                )

                values = {k: deepcopy(v.get("default")) for k, v in config.get("config_schema", {}).items() if isinstance(v, dict)}
                values.update(deepcopy(config.get("config") or {}))
                values.update({k: deepcopy(config[k]) for k in values if k in config})
                if self.chat_id is not None and plugin_name not in {"assistant", "builtin_chatbot"}:
                    with (self.session_factory or SessionLocal)() as db:
                        if db.get(WeChatUser, self.chat_id) is None:
                            raise PluginChatConfigError("聊天不存在")
                        effective = PluginChatConfigService(db).describe(
                            self.chat_id, plugin_name, config, redact=False,
                        )["effective"]
                        values.update(effective)
                self.snapshots[key] = values
            return self.snapshots[key]


_active_scope = ContextVar("plugin_config_scope", default=None)


def current_config_scope():
    return _active_scope.get()


@contextmanager
def plugin_config_scope(*, chat_id=None, session_factory=None):
    if chat_id is not None and (not isinstance(chat_id, int) or isinstance(chat_id, bool)):
        raise ValueError("聊天 ID 必须是整数")
    token = _active_scope.set(ConfigScope(chat_id, session_factory))
    try:
        yield _active_scope.get()
    finally:
        _active_scope.reset(token)


@contextmanager
def chat_config_scope(chat_name, session_factory=None):
    """Select a persisted target for scheduled/replayed work, never a chat label override."""
    from app.models.base import SessionLocal
    from app.models.user_permission import WeChatUser
    from app.services.plugin_chat_config_service import PluginChatConfigError

    with (session_factory or SessionLocal)() as db:
        user = db.query(WeChatUser.id).filter_by(chat_name=chat_name).first()
        if user is None:
            raise PluginChatConfigError("聊天不存在")
        chat_id = int(user.id)
    current = current_config_scope()
    if current is not None and current.chat_id == chat_id:
        yield current
    else:
        with plugin_config_scope(chat_id=chat_id, session_factory=session_factory) as scope:
            yield scope


class ScopedConfigAttribute:
    """Read a legacy cached setting from the current invocation, never mutate it."""

    def __init__(self, getter):
        self.getter = getter

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        scope = current_config_scope()
        if scope is not None:
            key = (id(instance), self.name)
            with scope.lock:
                if key not in scope.attributes:
                    scope.attributes[key] = self.getter(instance)
                return scope.attributes[key]
        try:
            return instance.__dict__[self.name]
        except KeyError:
            raise AttributeError(self.name) from None

    def __set__(self, instance, value):
        instance.__dict__[self.name] = value
