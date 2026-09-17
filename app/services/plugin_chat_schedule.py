"""Chat-scoped schedules with durable admission and current push authorization."""

from contextlib import closing, contextmanager

import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.plugin_config_context import plugin_config_scope
from app.utils.plugin_config import get_config

logger = logging.getLogger(__name__)


class ChatSchedule:
    def __init__(self, context, event_bus, *, time_key, callback, weekday_key=None,
                 weekday_parser=None, month_day=None, execution_lock=None, ready=lambda: True):
        self.context, self.event_bus = context, event_bus
        self.plugin_name = context.plugin_id
        self.time_key, self.callback = time_key, callback
        self.weekday_key, self.weekday_parser = weekday_key, weekday_parser
        self.month_day, self.execution_lock = month_day, execution_lock
        self.ready = ready
        self.started = datetime.now(ZoneInfo("Asia/Shanghai"))
        self.path = context.storage.persistent_path("chat_schedule.sqlite3")
        with self._database() as db:
            db.execute("CREATE TABLE IF NOT EXISTS runs(chat_id INTEGER, slot TEXT, status TEXT, PRIMARY KEY(chat_id,slot))")

    @contextmanager
    def _database(self):
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                yield db

    def recipients(self):
        from app.models.user_permission import UserPermission, WeChatUser

        with self.event_bus.db_session_factory() as db:
            return [(int(user_id), name) for user_id, name in db.query(WeChatUser.id, WeChatUser.chat_name)
                    .join(UserPermission, UserPermission.user_id == WeChatUser.id)
                    .filter(UserPermission.plugin_name == self.plugin_name + "#push").distinct().all()]

    def slot(self, now):
        value = str(get_config(self.time_key, "09:00", plugin_name=self.plugin_name))
        hh, mm = map(int, value.split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if self.month_day and now.day != self.month_day:
            return None
        if self.weekday_key and now.weekday() != self.weekday_parser(get_config(self.weekday_key, "MON", plugin_name=self.plugin_name)):
            return None
        if now < target or (target < self.started and (self.started - target).total_seconds() > 300):
            return None
        return target.isoformat()

    def tick(self, now=None):
        now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
        if not self.event_bus.context.get("wx") or self.context.workers.stop_event.is_set():
            return
        for chat_id, chat_name in self.recipients():
            try:
                with plugin_config_scope(chat_id=chat_id, session_factory=self.event_bus.db_session_factory):
                    slot = self.slot(now)
                    if slot is None or not self.ready():
                        continue
                    if self.execution_lock and self.execution_lock.locked():
                        continue
                    with self._database() as db:
                        admitted = db.execute("INSERT OR IGNORE INTO runs VALUES(?,?,?)", (chat_id, slot, "queued")).rowcount == 1
                    if not admitted:
                        continue

                    def run(operation, chat_id=chat_id, chat_name=chat_name, slot=slot):
                        status, acquired = "failed", False
                        try:
                            operation.check_cancelled()
                            # Acquire inside the callback: cancellation before the
                            # task starts must never leave the plugin locked.
                            if self.execution_lock:
                                while not self.execution_lock.acquire(timeout=0.5):
                                    operation.check_cancelled()
                                    if self.context.workers.stop_event.is_set():
                                        status = "stopped"
                                        return {"skipped": True}
                                acquired = True
                            operation.check_cancelled()
                            if self.context.workers.stop_event.is_set():
                                status = "stopped"
                                return {"skipped": True}
                            if (chat_id, chat_name) not in self.recipients():
                                status = "revoked"
                                return {"skipped": True}
                            self.callback(chat_name, operation)
                            status = "completed"
                            return {"chat_id": chat_id, "slot": slot}
                        finally:
                            try:
                                with self._database() as db:
                                    db.execute("UPDATE runs SET status=? WHERE chat_id=? AND slot=?", (status, chat_id, slot))
                            finally:
                                if acquired:
                                    self.execution_lock.release()

                    try:
                        self.context.tasks.submit("scheduled_push", f"{self.plugin_name} · {chat_name}", run,
                                                  details={"chat_id": chat_id, "slot": slot})
                    except Exception:
                        with self._database() as db:
                            db.execute("DELETE FROM runs WHERE chat_id=? AND slot=?", (chat_id, slot))
                        raise
            except Exception:
                logger.exception("插件 %s 的聊天 %s 定时任务未启动", self.plugin_name, chat_id)

    def run(self):
        while not self.context.workers.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("插件 %s 的定时检查失败，下轮重试", self.plugin_name)
            self.context.workers.stop_event.wait(30)


def start_chat_schedule(context, event_bus, **kwargs):
    schedule = ChatSchedule(context, event_bus, **kwargs)
    return context.workers.start("chat-scheduler", schedule.run)
