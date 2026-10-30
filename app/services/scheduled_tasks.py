"""Durable task definitions, versioned edits and atomic scheduling admission."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import json
import os
import sqlite3
import threading
import time
import uuid

from app.services.schedule_rules import describe, display_time, instant, latest_fire, next_fire, preview, validate_schedule


class ScheduleError(ValueError):
    pass


class ScheduleConflict(ScheduleError):
    pass


class ScheduleDenied(ScheduleError):
    pass


@dataclass(frozen=True)
class ScheduleActor:
    chat_id: int = 0
    name: str = ""
    admin: bool = False


def chat_directory(chat_id=None, chat_name=None):
    """Resolve identity from the managed chat table, never from model arguments."""
    from app.models.base import SessionLocal
    from app.models.user_permission import WeChatUser
    with SessionLocal() as db:
        user = db.get(WeChatUser, chat_id) if chat_id else db.query(WeChatUser).filter_by(chat_name=chat_name).first()
        if user is None:
            raise ScheduleDenied("聊天不存在或已删除")
        policy = user.assistant_policy
        def names(raw):
            try:
                value = json.loads(raw or "[]")
                return [str(x).strip() for x in value] if isinstance(value, list) else []
            except (ValueError, TypeError):
                return [x.strip() for x in str(raw or "").splitlines() if x.strip()]
        return {"id": user.id, "name": user.chat_name, "enabled": bool(policy and policy.enabled and user.listening_enabled),
                "profile_id": (policy.codex_profile_id or "") if policy else "",
                "blocked_names": names(user.sender_blacklist) + names(policy.ignored_senders if policy else None)}


class ScheduledTaskService:
    def __init__(self, path=None, *, directory=chat_directory, clock=time.time, reviewer=None):
        self.path = Path(path or os.getenv("SCHEDULED_TASKS_DB") or Path(__file__).resolve().parents[2] / "data" / "scheduled_tasks.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.directory, self.clock = directory, clock
        self.reviewer = reviewer
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS scheduled_tasks(
                    id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, creator TEXT NOT NULL,
                    status TEXT NOT NULL, version INTEGER NOT NULL, next_run REAL,
                    definition TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    source_key TEXT UNIQUE
                );
                CREATE INDEX IF NOT EXISTS scheduled_due ON scheduled_tasks(status,next_run);
                CREATE TABLE IF NOT EXISTS scheduled_monitor_state(
                    task_id TEXT NOT NULL, monitor_key TEXT NOT NULL, revision INTEGER NOT NULL,
                    state TEXT NOT NULL, fingerprint TEXT NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(task_id,monitor_key)
                );
                CREATE TABLE IF NOT EXISTS scheduled_monitor_events(
                    task_id TEXT NOT NULL, monitor_key TEXT NOT NULL, event_key TEXT NOT NULL,
                    run_id TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(task_id,monitor_key,event_key)
                );
                CREATE TABLE IF NOT EXISTS scheduled_goal_events(
                    task_id TEXT NOT NULL, goal_key TEXT NOT NULL, event_key TEXT NOT NULL,
                    occurred_at REAL NOT NULL, source TEXT NOT NULL, run_id TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(task_id,goal_key,event_key)
                );
                CREATE INDEX IF NOT EXISTS scheduled_chat ON scheduled_tasks(chat_id,created);
                CREATE TABLE IF NOT EXISTS scheduled_versions(
                    task_id TEXT NOT NULL, version INTEGER NOT NULL, definition TEXT NOT NULL,
                    actor TEXT NOT NULL, feedback TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(task_id,version)
                );
                CREATE TABLE IF NOT EXISTS scheduled_runs(
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, version INTEGER NOT NULL,
                    occurrence TEXT NOT NULL, scheduled_at REAL NOT NULL, trigger TEXT NOT NULL,
                    status TEXT NOT NULL, definition TEXT NOT NULL, owner TEXT,
                    created REAL NOT NULL, started REAL, finished REAL, error TEXT,
                    result TEXT, usage TEXT, thread_id TEXT, turn_id TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(task_id,occurrence)
                );
                CREATE TABLE IF NOT EXISTS scheduled_attempts(
                    run_id TEXT NOT NULL, attempt INTEGER NOT NULL, category TEXT NOT NULL,
                    error TEXT NOT NULL, created REAL NOT NULL, retry_at REAL,
                    PRIMARY KEY(run_id,attempt)
                );
                CREATE INDEX IF NOT EXISTS scheduled_runs_task ON scheduled_runs(task_id,created);
                CREATE TABLE IF NOT EXISTS scheduled_deliveries(
                    id TEXT PRIMARY KEY, run_id TEXT, task_id TEXT NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL, error TEXT, created REAL NOT NULL,
                    UNIQUE(run_id,kind,payload)
                );
                CREATE TABLE IF NOT EXISTS scheduled_message_links(
                    delivery_id TEXT NOT NULL, task_id TEXT NOT NULL, run_id TEXT,
                    chat_id INTEGER NOT NULL, version INTEGER NOT NULL, creator TEXT NOT NULL,
                    title TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(delivery_id,task_id,version)
                );
                CREATE INDEX IF NOT EXISTS scheduled_message_chat ON scheduled_message_links(chat_id,created);
                CREATE TABLE IF NOT EXISTS scheduled_chat_permissions(chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL, version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS scheduled_settings(id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL, version INTEGER NOT NULL);
                INSERT OR IGNORE INTO scheduled_settings VALUES(1,1,1);
                CREATE TABLE IF NOT EXISTS scheduled_lease(id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT, expires REAL);
            """)

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def chat_settings(self, chat_id, db=None):
        if db is None:
            with self.db() as connection:
                return self.chat_settings(chat_id, connection)
        row = db.execute("SELECT enabled,version FROM scheduled_chat_permissions WHERE chat_id=?", (chat_id,)).fetchone()
        return {"enabled": bool(row["enabled"]) if row else False, "version": row["version"] if row else 1}

    def context_enabled(self, context):
        return bool(context and not context.scheduled and self.settings()["enabled"] and self.chat_settings(context.actor.chat_id)["enabled"])

    def _definition_enabled(self, db, definition):
        return all(self.chat_settings(definition[key], db)["enabled"] for key in ("chat_id", "delivery_chat_id"))

    def set_chat_enabled(self, chat_id, enabled, actor, *, expected_version):
        if not actor.admin:
            raise ScheduleDenied("只有 Web 管理员可以修改聊天计划任务权限")
        self.directory(chat_id=chat_id)
        with self.db(True) as db:
            settings = self.chat_settings(chat_id, db)
            if settings["version"] != expected_version:
                raise ScheduleConflict("聊天权限已被修改，请刷新后重试")
            if settings["enabled"] == enabled:
                return settings
            version = settings["version"] + 1
            db.execute("INSERT INTO scheduled_chat_permissions VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled,version=excluded.version", (chat_id, enabled, version))
            now = self.clock()
            # Include destinations: revoking a chat also revokes deliveries to it.
            affected = "(json_extract(definition,'$.chat_id')=? OR json_extract(definition,'$.delivery_chat_id')=?)"
            if not enabled:
                db.execute("UPDATE scheduled_runs SET status='cancelled',cancel_requested=1,finished=?,error='聊天计划任务权限已关闭' WHERE status='queued' AND " + affected, (now, chat_id, chat_id))
                db.execute("UPDATE scheduled_runs SET cancel_requested=1 WHERE status='running' AND " + affected, (chat_id, chat_id))
            else:
                for task in db.execute("SELECT * FROM scheduled_tasks WHERE status='active' AND " + affected, (chat_id, chat_id)).fetchall():
                    definition = json.loads(task["definition"])
                    if not self._definition_enabled(db, definition):
                        continue
                    due = next_fire(definition["schedule"], now, definition.get("next_exception"))
                    if due is None and task["next_run"] is not None:
                        self._insert_run(db, task, task["next_run"], f"scheduled:{task['next_run']:.6f}", "scheduled", status="missed", error="聊天权限关闭期间已过期，未补跑")
                    db.execute("UPDATE scheduled_tasks SET next_run=?,status=?,updated=? WHERE id=?", (due, "active" if due else "completed", now, task["id"]))
            return {"enabled": enabled, "version": version}

    def settings(self):
        with self.db() as db:
            row = db.execute("SELECT enabled,version FROM scheduled_settings WHERE id=1").fetchone()
            return {"enabled": bool(row["enabled"]), "version": row["version"]}

    def require_enabled(self, db=None):
        enabled = db.execute("SELECT enabled FROM scheduled_settings WHERE id=1").fetchone()[0] if db else self.settings()["enabled"]
        if not enabled:
            raise ScheduleDenied("计划任务功能已关闭，请在 Web 计划任务页面开启总开关")

    def set_enabled(self, enabled, actor, *, expected_version):
        if not actor.admin:
            raise ScheduleDenied("只有 Web 管理员可以修改计划任务总开关")
        with self.db(True) as db:
            row = db.execute("SELECT * FROM scheduled_settings WHERE id=1").fetchone()
            if row["version"] != expected_version:
                raise ScheduleConflict("总开关已被修改，请刷新后重试")
            if bool(row["enabled"]) == enabled:
                return {"enabled": enabled, "version": row["version"]}
            now = self.clock()
            db.execute("UPDATE scheduled_settings SET enabled=?,version=version+1 WHERE id=1", (enabled,))
            if not enabled:
                db.execute("UPDATE scheduled_runs SET status='cancelled',cancel_requested=1,finished=?,error='计划任务总开关已关闭' WHERE status='queued'", (now,))
                db.execute("UPDATE scheduled_runs SET cancel_requested=1 WHERE status='running'")
            else:
                for task in db.execute("SELECT * FROM scheduled_tasks WHERE status='active'").fetchall():
                    definition = json.loads(task["definition"])
                    due = next_fire(definition["schedule"], now, definition.get("next_exception"))
                    if due is None and task["next_run"] is not None:
                        self._insert_run(db, task, task["next_run"], f"scheduled:{task['next_run']:.6f}", "scheduled", status="missed", error="总开关关闭期间已过期，未补跑")
                    db.execute("UPDATE scheduled_tasks SET next_run=?,status=?,updated=? WHERE id=?", (due, "active" if due else "completed", now, task["id"]))
            return {"enabled": enabled, "version": row["version"] + 1}

    def _row(self, db, task_id, actor, write=False):
        if not actor.admin:
            self.require_enabled(db)
            if not self.chat_settings(actor.chat_id, db)["enabled"]:
                raise ScheduleDenied("此聊天未开启计划任务权限")
        row = db.execute("SELECT * FROM scheduled_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or (not actor.admin and row["chat_id"] != actor.chat_id):
            raise ScheduleDenied("当前聊天没有此任务")
        if write and not actor.admin and (not actor.name.strip() or row["creator"] != actor.name.strip()):
            raise ScheduleDenied("只有创建者可以修改、删除或执行此任务")
        if write and not actor.admin:
            self.authorize(row["chat_id"], actor.name)
        return row

    def authorize(self, chat_id, creator=None):
        if not self.chat_settings(chat_id)["enabled"]:
            raise ScheduleDenied("此聊天未开启计划任务权限，请在 Web 计划任务页面勾选开启")
        chat = self.directory(chat_id=chat_id)
        if not chat["enabled"]:
            raise ScheduleDenied("所属聊天的 AI 助手或监听已关闭")
        if creator and creator.strip() in chat.get("blocked_names", []):
            raise ScheduleDenied("任务创建者已被当前聊天禁用")
        return chat

    def _public(self, row):
        item = dict(row)
        definition = json.loads(item.pop("definition"))
        item.pop("source_key", None)
        item.update(definition)
        exception = item.get("next_exception")
        if exception and max(exception["original_at"], exception.get("replacement_at") or 0) < self.clock():
            item.pop("next_exception", None)
        if definition.get('monitor', {}) and definition['monitor'].get('goal'):
            with self.db() as db:
                item['goal_progress'] = self._goal_progress(db, item['id'], definition['monitor'])
            if item['status'] == 'completed' and not item['goal_progress']['complete']:
                item['goal_progress']['stopped_reason'] = '排期已结束，目标尚未完成'
        item["schedule_text"] = describe(definition["schedule"])
        item["upcoming"] = preview(definition["schedule"], self.clock(), exception=definition.get("next_exception")) if item["status"] == "active" else []
        return item

    def list(self, actor, *, history=False):
        with self.db() as db:
            rows = db.execute("SELECT * FROM scheduled_tasks WHERE (? OR chat_id=?) "
                              "AND (? OR status IN ('active','paused')) ORDER BY created DESC LIMIT 1000",
                              (actor.admin, actor.chat_id, history)).fetchall()
            items = []
            for row in rows:
                item = self._public(row)
                run = db.execute("SELECT id,status,error,started,finished,version FROM scheduled_runs WHERE task_id=? ORDER BY created DESC LIMIT 1", (row["id"],)).fetchone()
                item["last_run"] = dict(run) if run else None
                if run:
                    item["last_run"]["deliveries"] = [dict(r) for r in db.execute("SELECT kind,status,error FROM scheduled_deliveries WHERE run_id=?", (run["id"],))]
                items.append(item)
            return items

    def get(self, task_id, actor):
        with self.db() as db:
            item = self._public(self._row(db, task_id, actor))
            original = db.execute("SELECT feedback FROM scheduled_versions WHERE task_id=? AND version=1", (task_id,)).fetchone()
            item["original_request"] = original["feedback"] if original else ""
            item["versions"] = [{**dict(r), "definition": json.loads(r["definition"])} for r in db.execute(
                "SELECT * FROM scheduled_versions WHERE task_id=? ORDER BY version DESC LIMIT 100", (task_id,))]
            item["runs"] = [self._run_dict(db, r) for r in db.execute(
                "SELECT * FROM scheduled_runs WHERE task_id=? ORDER BY created DESC LIMIT 50", (task_id,))]
            item["notices"] = [dict(r) for r in db.execute("SELECT id,payload,status,error FROM scheduled_deliveries WHERE task_id=? AND kind='notice' ORDER BY created DESC LIMIT 20", (task_id,))]
            if item.get('monitor'):
                item['monitor_state'] = self._monitor_state(db, task_id, item['monitor']['key'], item['monitor'].get('goal'))
            return item

    @staticmethod
    def _run_dict(db, row):
        item = dict(row)
        for key in ("definition", "result", "usage"):
            item[key] = json.loads(item[key]) if item[key] else None
        item["attempts"] = [dict(r) for r in db.execute("SELECT * FROM scheduled_attempts WHERE run_id=? ORDER BY attempt", (item["id"],))]
        item["deliveries"] = [dict(r) for r in db.execute("SELECT * FROM scheduled_deliveries WHERE run_id=? ORDER BY created,id", (item["id"],))]
        return item

    def _definition(self, data, actor, old=None):
        allowed = {"title", "prompt", "schedule", "creator", "chat_id", "delivery_chat_id", "profile_id",
                   "timeout_seconds", "misfire_grace_seconds", "resources", "monitor"}
        if set(data) - allowed:
            raise ScheduleError("存在不支持的任务字段")
        if old and "chat_id" in data and data["chat_id"] != old["chat_id"]:
            raise ScheduleError("不能移动任务的所属聊天，请新建任务")
        if not actor.admin and any(k in data for k in ("creator", "chat_id", "delivery_chat_id", "profile_id")):
            raise ScheduleDenied("创建者、聊天及模型配置由后台绑定，只能在 Web 管理端修改")
        item = {"timeout_seconds": 600, "misfire_grace_seconds": 3600, "profile_id": "", "resources": [], **(old or {}), **data}
        if not old:
            item["chat_id"] = int(data.get("chat_id") or actor.chat_id)
            item["creator"] = str((data.get("creator") or "Web 管理员") if actor.admin else actor.name).strip()
            item["delivery_chat_id"] = int(data.get("delivery_chat_id") or item["chat_id"])
        for key, limit in (("title", 120), ("prompt", 20000), ("creator", 128)):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ScheduleError(f"{key} 不能为空，且不能超过 {limit} 字符")
            item[key] = value.strip()
        chat = self.authorize(item["chat_id"], item["creator"])
        destination = self.authorize(item["delivery_chat_id"])
        item["chat_name"], item["delivery_chat_name"] = chat["name"], destination["name"]
        if not old or "schedule" in data:
            item["schedule"] = validate_schedule(item.get("schedule"), self.clock())
        for key, low, high in (("timeout_seconds", 30, 3600), ("misfire_grace_seconds", 0, 7 * 86400)):
            if type(item[key]) is not int or not low <= item[key] <= high:
                raise ScheduleError(f"{key} 必须在 {low} 到 {high} 之间")
        if not isinstance(item["profile_id"], str) or len(item["profile_id"]) > 128:
            raise ScheduleError("无效的 Profile")
        if not isinstance(item["resources"], list) or len(item["resources"]) > 20 or any(not isinstance(p, str) or not p or len(p) > 2000 for p in item["resources"]):
            raise ScheduleError("资源引用须为最多 20 个工作区内的文件路径")
        from app.services.schedule_monitor import validate_monitor
        item['monitor'] = validate_monitor(item.get('monitor'))
        goal = (item['monitor'] or {}).get('goal')
        if goal:
            if item['schedule']['kind'] == 'once':
                raise ScheduleError('有限事件目标需要周期检查排期，不能使用一次性安排')
            previous = ((old or {}).get('monitor') or {}).get('goal')
            if previous and previous['key'] == goal['key'] and any(previous[k] != goal[k] for k in ('start_at','unit','acceptance')):
                raise ScheduleError('同一批目标不能更改起点、计数单位或验收标准；新目标请使用新的 goal.key')
        return item

    def _version(self, db, task_id, version, definition, actor, feedback):
        db.execute("INSERT INTO scheduled_versions VALUES(?,?,?,?,?,?)",
                   (task_id, version, json.dumps(definition, ensure_ascii=False), actor.name or "Web 管理员", str(feedback or "")[:20000], self.clock()))

    def _link_message(self, db, delivery_id, definition, *, version, run_id=None, task_id=None):
        db.execute("INSERT OR IGNORE INTO scheduled_message_links VALUES(?,?,?,?,?,?,?,?)",
                   (delivery_id, task_id or definition["id"], run_id,
                    definition["delivery_chat_id"] if run_id else definition["chat_id"],
                    version, definition["creator"], definition["title"], self.clock()))

    def prepare_receipts(self, notice_ids, actor, text=None):
        """Persist a natural acknowledgement before sending; consolidate a turn."""
        with self.db(True) as db:
            rows = []
            for notice_id in dict.fromkeys(notice_ids):
                row = db.execute("SELECT * FROM scheduled_deliveries WHERE id=? AND kind='notice'", (notice_id,)).fetchone()
                if not row:
                    raise ScheduleError("确认记录不存在")
                self._row(db, row["task_id"], actor, True)
                rows.append(row)
            first = next((row for row in rows if row["status"] == "pending"), rows[0])
            if first["status"] == "pending":
                payload = text.strip() if text and text.strip() else "好，已经安排好了。" if len(rows) > 1 else first["payload"]
                # Older pending receipts must not leak the retired detailed format.
                if not text and payload.startswith("✅ 计划任务"):
                    payload = "好，已经记下了。"
                db.execute("UPDATE scheduled_deliveries SET payload=? WHERE id=?", (payload, first["id"]))
                for row in rows:
                    if row["id"] == first["id"] or row["status"] != "pending":
                        continue
                    db.execute("UPDATE scheduled_deliveries SET status='bundled',payload=? WHERE id=?", (payload, row["id"]))
                    db.execute("INSERT OR IGNORE INTO scheduled_message_links SELECT ?,task_id,run_id,chat_id,version,creator,title,created FROM scheduled_message_links WHERE delivery_id=?", (first["id"], row["id"]))
            return first["id"]

    def conversation_links(self, actor, query=""):
        """Only acknowledged messages in this destination; never expose another chat's prompt."""
        self.require_enabled()
        self.authorize(actor.chat_id, actor.name)
        with self.db() as db:
            rows = db.execute("SELECT l.*,d.payload,d.kind FROM scheduled_message_links l JOIN scheduled_deliveries d ON d.id=l.delivery_id WHERE l.chat_id=? AND d.status='sent' AND l.created>=? ORDER BY l.created DESC LIMIT 50",
                              (actor.chat_id, self.clock() - 30 * 86400)).fetchall()
            items = [dict(row) for row in rows]
            # A quoted older result takes precedence over unrelated newer ones.
            items.sort(key=lambda r: bool(len(r["payload"].strip()) >= 8 and r["payload"].strip() in query), reverse=True)
            return [{**r, "payload": Path(r["payload"]).name if r["kind"] == "file" else r["payload"][:1200]} for r in items[:8]]

    def _notice(self, db, item, action, feedback="", modifier=""):
        # Detailed definitions and feedback remain in the version audit, never
        # in a mandatory chat receipt. This is only a generation-failure fallback.
        text = "好，已经记下了。" if action == "创建成功" else "好，已经调整好了。"
        notice_id = uuid.uuid4().hex
        db.execute("INSERT INTO scheduled_deliveries(id,task_id,kind,payload,status,created) VALUES(?,?,'notice',?,'pending',?)",
                   (notice_id, item["id"], text, self.clock()))
        self._link_message(db, notice_id, item, version=item["version"])
        return notice_id

    def _review(self, definition):
        from app.services.schedule_role_policy import review_definition
        return (self.reviewer or review_definition)(definition)

    def create(self, data, actor, *, source_key=None, feedback="", notify=False):
        self.require_enabled()
        with self.db() as db:
            existing = db.execute("SELECT id FROM scheduled_tasks WHERE source_key=?", (source_key,)).fetchone() if source_key else None
        review = None if existing else self._review(self._definition(data, actor))
        now = self.clock()
        with self.db(True) as db:
            self.require_enabled(db)
            if source_key:
                previous = db.execute("SELECT * FROM scheduled_tasks WHERE source_key=?", (source_key,)).fetchone()
                if previous:
                    self._row(db, previous["id"], actor)
                    item = self._public(previous)
                    notice = db.execute("SELECT id FROM scheduled_deliveries WHERE task_id=? AND kind='notice' ORDER BY created LIMIT 1", (item["id"],)).fetchone()
                    return {"task": item, "notice_id": notice["id"] if notice else None, "existing": True}
            definition = self._definition(data, actor)
            definition["role_review"] = review
            if db.execute("SELECT COUNT(*) FROM scheduled_tasks WHERE chat_id=? AND status IN ('active','paused')", (definition["chat_id"],)).fetchone()[0] >= 100:
                raise ScheduleError("每个聊天最多保留 100 个未结束任务")
            task_id = "T-" + uuid.uuid4().hex[:12]
            due = next_fire(definition["schedule"], now)
            if due is None:
                raise ScheduleError("排期没有未来触发时间，请调整起止日期")
            db.execute("INSERT INTO scheduled_tasks VALUES(?,?,?,'active',1,?,?,?,?,?)",
                       (task_id, definition["chat_id"], definition["creator"], due, json.dumps(definition, ensure_ascii=False), now, now, source_key))
            self._version(db, task_id, 1, definition, actor, feedback)
            self._queue_monitor_baseline(db, db.execute('SELECT * FROM scheduled_tasks WHERE id=?', (task_id,)).fetchone())
            item = self._public(self._row(db, task_id, actor))
            notice = self._notice(db, item, "创建成功") if notify else None
            return {"task": item, "notice_id": notice}

    def update(self, task_id, changes, actor, *, expected_version, feedback="", notify=False):
        with self.db() as db:
            before = self._row(db, task_id, actor, True)
            if before["version"] != expected_version:
                raise ScheduleConflict("任务已被修改，请先重新读取最新版本后合并要求")
            if before["status"] == "deleted":
                raise ScheduleError("任务已删除")
            candidate = self._definition(changes, actor, json.loads(before["definition"]))
        review = self._review(candidate)
        with self.db(True) as db:
            row = self._row(db, task_id, actor, True)
            if row["version"] != expected_version:
                raise ScheduleConflict("任务已被修改，请先重新读取最新版本后合并要求")
            if row["status"] == "deleted":
                raise ScheduleError("任务已删除")
            old = json.loads(row["definition"])
            definition = self._definition(changes, actor, old)
            definition["role_review"] = review
            due, status = row["next_run"], row["status"]
            if "schedule" in changes:
                due = next_fire(definition["schedule"], self.clock())
                definition.pop("next_exception", None)
                if status != "paused":
                    status = "active" if due is not None else "completed"
                else:
                    due = None
                db.execute("UPDATE scheduled_runs SET status='cancelled',finished=?,error='排期已修改，取消旧排队任务' WHERE task_id=? AND status='queued'", (self.clock(), task_id))
            monitor = definition.get('monitor') or {}
            goal = monitor.get('goal')
            if goal:
                progress = self._goal_progress(db, task_id, monitor)
                if progress['complete']:
                    status, due = 'completed', None
                    definition.pop('pause', None)
                    db.execute("UPDATE scheduled_runs SET status='cancelled',finished=?,error='目标已达成' WHERE task_id=? AND status='queued'", (self.clock(), task_id))
                elif status == 'completed' and 'monitor' in changes:
                    due = next_fire(definition['schedule'], self.clock(), definition.get('next_exception'))
                    status = 'active' if due is not None else 'completed'
            version = row["version"] + 1
            db.execute("UPDATE scheduled_tasks SET creator=?,definition=?,version=?,next_run=?,status=?,updated=? WHERE id=?",
                       (definition["creator"], json.dumps(definition, ensure_ascii=False), version, due, status, self.clock(), task_id))
            self._version(db, task_id, version, definition, actor, feedback)
            self._queue_monitor_baseline(db, db.execute('SELECT * FROM scheduled_tasks WHERE id=?', (task_id,)).fetchone())
            item = self._public(self._row(db, task_id, actor))
            notice = self._notice(db, item, "已更新", feedback, actor.name) if notify else None
            return {"task": item, "notice_id": notice}

    def restore(self, task_id, version, actor, *, expected_version, notify=False):
        with self.db() as db:
            self._row(db, task_id, actor, True)
            row = db.execute("SELECT definition FROM scheduled_versions WHERE task_id=? AND version=?", (task_id, version)).fetchone()
            if not row:
                raise ScheduleError("历史版本不存在")
            definition = json.loads(row["definition"])
        # Ownership and delivery authority are deliberately not rolled back.
        changes = {k: definition[k] for k in ("title", "prompt", "schedule", "timeout_seconds", "misfire_grace_seconds", "resources")}
        changes['monitor'] = definition.get('monitor')
        return self.update(task_id, changes, actor, expected_version=expected_version,
                           feedback=f"恢复到 v{version} 的任务内容与时间规则", notify=notify)

    def action(self, task_id, action, actor, *, expected_version=None, request_key=None, until=None, at=None, _auto_owner=None):
        resume_checked_at = self.clock()
        if until is not None and action != "pause":
            raise ScheduleError("until 只用于定时暂停")
        if at is not None and action != "reschedule_next":
            raise ScheduleError("at 只用于单次改期")
        review = None
        if action in {"resume", "run_now"}:
            self.require_enabled()
            with self.db() as db:
                before = self._row(db, task_id, actor, True)
                if expected_version is not None and before["version"] != expected_version:
                    raise ScheduleConflict("任务已被修改，请刷新后重试")
                if before["status"] == "deleted":
                    raise ScheduleError("任务已删除")
                expected_version = before["version"]
                definition = json.loads(before["definition"])
                if (definition.get('monitor') or {}).get('goal') and self._goal_progress(db, task_id, definition['monitor'])['complete']:
                    raise ScheduleError('目标已经完成；如需继续，请明确增加目标数量或建立新一批目标')
            self.authorize(definition["chat_id"], definition["creator"])
            self.authorize(definition["delivery_chat_id"])
            review = self._review(definition)
        with self.db(True) as db:
            row = self._row(db, task_id, actor, True)
            if expected_version is not None and row["version"] != expected_version:
                raise ScheduleConflict("任务已被修改，请刷新后重试")
            if row["status"] == "deleted":
                raise ScheduleError("任务已删除")
            now = self.clock()
            definition = json.loads(row["definition"])
            if action in {"resume", "run_now"}:
                self.require_enabled(db)
                if not self._definition_enabled(db, definition):
                    raise ScheduleDenied("聊天计划任务权限未开启")
            if _auto_owner:
                lease = db.execute("SELECT * FROM scheduled_lease WHERE id=1").fetchone()
                pause = definition.get("pause", {})
                if (not lease or lease["owner"] != _auto_owner or lease["expires"] <= now
                        or row["status"] != "paused" or pause.get("reason") != "timed"
                        or pause.get("until", float("inf")) > now):
                    raise ScheduleConflict("自动恢复已失效")
            if action in {"skip_next", "reschedule_next", "clear_next_change"}:
                if row["status"] != "active" and not (action == "clear_next_change" and row["status"] == "completed"):
                    raise ScheduleError("请先恢复任务，再调整下一次执行")
                old_exception = definition.get("next_exception")
                if action == "clear_next_change":
                    definition.pop("next_exception", None)
                else:
                    if old_exception and max(old_exception["original_at"], old_exception.get("replacement_at") or 0) > now:
                        raise ScheduleError("已有待生效的单次调整，请先取消该调整")
                    original = row["next_run"]
                    if original is None or original <= now:
                        raise ScheduleConflict("下一次触发已到期，请刷新后重试")
                    replacement = instant(at) if action == "reschedule_next" else None
                    if replacement is not None:
                        if replacement <= now:
                            raise ScheduleError("单次改期须为未来时间")
                        from datetime import datetime
                        from zoneinfo import ZoneInfo
                        local_date = datetime.fromtimestamp(replacement, ZoneInfo(definition["schedule"]["timezone"])).date().isoformat()
                        rule = definition["schedule"]
                        if not rule.get("start_date", "0001-01-01") <= local_date <= rule.get("end_date", "9999-12-31"):
                            raise ScheduleError("改期不能超出任务起止日期")
                    definition["next_exception"] = {"original_at": original, "replacement_at": replacement}
                    self._insert_run(db, row, original, f"exception:{row['version'] + 1}", action,
                                     status="skipped", error="用户跳过本次" if replacement is None else "用户将本次改期至 " + display_time(replacement))
                due = next_fire(definition["schedule"], now, definition.get("next_exception"))
                status = "active" if due is not None else "completed"
                db.execute("UPDATE scheduled_tasks SET definition=?,next_run=?,status=?,version=version+1,updated=? WHERE id=?",
                           (json.dumps(definition, ensure_ascii=False), due, status, now, task_id))
                self._version(db, task_id, row["version"] + 1, definition, actor, action)
                return {"task": self._public(self._row(db, task_id, actor))}
            if action == "run_now":
                self.require_enabled(db)
                self.authorize(row["chat_id"], row["creator"])
                self.authorize(json.loads(row["definition"])["delivery_chat_id"])
                occurrence = "manual:" + (request_key or uuid.uuid4().hex)
                prior = db.execute("SELECT id FROM scheduled_runs WHERE task_id=? AND occurrence=?", (task_id, occurrence)).fetchone()
                if prior:
                    return {"run_id": prior["id"], "status": "accepted"}
                if db.execute("SELECT 1 FROM scheduled_runs WHERE task_id=? AND status IN ('queued','running')", (task_id,)).fetchone():
                    raise ScheduleConflict("此任务已经在排队或执行中")
                return {"run_id": self._insert_run(db, row, now, occurrence, "manual"), "status": "queued"}
            if action == "stop":
                db.execute("UPDATE scheduled_runs SET cancel_requested=1 WHERE task_id=? AND status IN ('queued','running')", (task_id,))
                return {"status": "stop_requested"}
            statuses = {"pause": "paused", "resume": "active", "delete": "deleted", "cancel": "deleted"}
            if action not in statuses:
                raise ScheduleError("不支持的操作")
            due = row["next_run"]
            if action == "resume":
                resume_now = resume_checked_at if _auto_owner else now
                due = next_fire(definition["schedule"], resume_now, definition.get("next_exception"))
                if _auto_owner:
                    # A normal tick may be a few seconds late; include its boundary
                    # once, but never backfill the paused period after downtime.
                    latest = latest_fire(definition["schedule"], resume_now, definition.get("next_exception"))
                    if latest is not None and latest >= definition["pause"]["until"] and resume_now - latest <= 5:
                        due = latest
                if due is None:
                    if not _auto_owner:
                        raise ScheduleError("排期已结束，请修改触发时间或立即执行")
                    statuses["resume"] = "completed"
                definition.pop("pause", None)
                definition.pop("role_block", None)
                definition["role_review"] = review
            elif action == "pause":
                stamp = instant(until) if until is not None else None
                if stamp is not None and stamp <= now:
                    raise ScheduleError("自动恢复时间必须在未来")
                definition["pause"] = {"reason": "timed" if stamp is not None else "user", "until": stamp}
                due = None
            elif action in {"delete", "cancel"}:
                definition.pop("pause", None)
                definition.pop("next_exception", None)
                due = None
            db.execute("UPDATE scheduled_tasks SET status=?,next_run=?,version=version+1,definition=?,updated=? WHERE id=?",
                       (statuses[action], due, json.dumps(definition, ensure_ascii=False), now, task_id))
            self._version(db, task_id, row["version"] + 1, definition, actor, "定时暂停到期，自动恢复" if _auto_owner else action)
            if action in {"pause", "delete", "cancel"}:
                # Pause leaves running work alone; stop/delete/cancel request interruption.
                db.execute("UPDATE scheduled_runs SET status='cancelled',finished=?,error='任务已暂停或删除' WHERE task_id=? AND status='queued'", (now, task_id))
            if action in {"delete", "cancel"}:
                db.execute("UPDATE scheduled_runs SET cancel_requested=1 WHERE task_id=? AND status='running'", (task_id,))
            return {"task": self._public(self._row(db, task_id, actor))}

    def pending_resumes(self, owner):
        with self.db() as db:
            self.require_enabled(db)
            lease = db.execute("SELECT * FROM scheduled_lease WHERE id=1").fetchone()
            if not lease or lease["owner"] != owner or lease["expires"] <= self.clock():
                return []
            return [dict(row) for row in db.execute(
                "SELECT * FROM scheduled_tasks WHERE status='paused' AND json_extract(definition,'$.pause.reason')='timed' "
                "AND json_extract(definition,'$.pause.until')<=? ORDER BY updated LIMIT 100", (self.clock(),))
                if self._definition_enabled(db, json.loads(row["definition"]))]

    def resume_due(self, row, owner):
        try:
            return self.action(row["id"], "resume", ScheduleActor(admin=True, name="自动恢复"),
                               expected_version=row["version"], _auto_owner=owner)
        except ScheduleConflict:
            return None  # A newer user action or lease handoff wins.
        except Exception as exc:
            # Do not repeatedly call the model for an inaccessible/blocked task.
            with self.db(True) as db:
                current = db.execute("SELECT * FROM scheduled_tasks WHERE id=?", (row["id"],)).fetchone()
                if not current or current["version"] != row["version"] or current["status"] != "paused":
                    return None
                definition = json.loads(current["definition"])
                definition["pause"] = {"reason": "resume_blocked", "until": None,
                                       "error": "自动恢复未通过检查，请处理后手动恢复：" + str(exc)[:1000]}
                db.execute("UPDATE scheduled_tasks SET definition=?,version=version+1,updated=? WHERE id=?",
                           (json.dumps(definition, ensure_ascii=False), self.clock(), row["id"]))
                self._version(db, row["id"], row["version"] + 1, definition,
                              ScheduleActor(admin=True, name="自动恢复"), "自动恢复受阻，保持暂停")
            return None

    def _queue_monitor_baseline(self, db, row):
        definition = json.loads(row['definition'])
        config = definition.get('monitor')
        rule = definition['schedule']
        # Window/bounded schedules obey their first scheduled slot for baseline too.
        if rule['kind'] == 'window' or rule.get('start_date') or rule.get('end_date'):
            return
        if (config and config['initial'] == 'baseline' and row['status'] == 'active'
                and db.execute('SELECT enabled FROM scheduled_settings WHERE id=1').fetchone()[0]
                and self._definition_enabled(db, json.loads(row['definition']))
                and not self._monitor_state(db, row['id'], config['key'])['revision']):
            self._insert_run(db, row, self.clock(), 'baseline:' + str(row['version']), 'baseline')

    def _insert_run(self, db, row, scheduled_at, occurrence, trigger, *, status="queued", error=None):
        run_id = uuid.uuid4().hex
        db.execute("INSERT OR IGNORE INTO scheduled_runs(id,task_id,version,occurrence,scheduled_at,trigger,status,definition,created,finished,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (run_id, row["id"], row["version"], occurrence, scheduled_at, trigger, status, row["definition"], self.clock(), self.clock() if status != "queued" else None, error))
        return run_id

    def lease(self, owner):
        now = self.clock()
        with self.db(True) as db:
            row = db.execute("SELECT * FROM scheduled_lease WHERE id=1").fetchone()
            if row and row["owner"] != owner and row["expires"] > now:
                return False
            if not row or row["owner"] != owner:
                db.execute("UPDATE scheduled_runs SET status=CASE WHEN result IS NULL THEN 'interrupted' ELSE 'completed' END,finished=?,error='执行进程中断，结果及交付状态需核对；未自动重做' WHERE status='running'", (now,))
                db.execute("UPDATE scheduled_deliveries SET status='unknown',error='发送确认前进程中断' WHERE status='sending'")
            db.execute("INSERT INTO scheduled_lease VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,expires=excluded.expires", (owner, now + 45))
            return True

    def admit_due(self, owner):
        now = self.clock()
        with self.db(True) as db:
            if not db.execute("SELECT enabled FROM scheduled_settings WHERE id=1").fetchone()[0]:
                return
            lease = db.execute("SELECT * FROM scheduled_lease WHERE id=1").fetchone()
            if not lease or lease["owner"] != owner or lease["expires"] <= now:
                return
            for row in db.execute("SELECT * FROM scheduled_tasks WHERE status='active' AND next_run<=? ORDER BY next_run LIMIT 100", (now,)).fetchall():
                definition = json.loads(row["definition"])
                if not self._definition_enabled(db, definition):
                    continue
                rule = definition["schedule"]
                scheduled_at = max(row["next_run"], latest_fire(rule, now, definition.get("next_exception")) or row["next_run"])
                status, error = "queued", None
                if now - scheduled_at > definition["misfire_grace_seconds"]:
                    status, error = "missed", "超过补跑时效"
                elif db.execute("SELECT 1 FROM scheduled_runs WHERE task_id=? AND status IN ('queued','running')", (row["id"],)).fetchone():
                    status, error = "skipped", "上次执行尚未结束，本次不重叠执行"
                self._insert_run(db, row, scheduled_at, f"scheduled:{scheduled_at:.6f}", "scheduled", status=status, error=error)
                due = next_fire(rule, now, definition.get("next_exception"))
                db.execute("UPDATE scheduled_tasks SET next_run=?,status=?,updated=? WHERE id=?", (due, "active" if due else "completed", now, row["id"]))

    def claim(self, owner):
        with self.db(True) as db:
            if not db.execute("SELECT enabled FROM scheduled_settings WHERE id=1").fetchone()[0]:
                return
            lease = db.execute("SELECT * FROM scheduled_lease WHERE id=1").fetchone()
            if not lease or lease["owner"] != owner or lease["expires"] <= self.clock():
                return None
            db.execute("UPDATE scheduled_runs SET status='cancelled',finished=? WHERE status='queued' AND cancel_requested=1", (self.clock(),))
            row = db.execute("SELECT * FROM scheduled_runs r WHERE status='queued' AND NOT EXISTS (SELECT 1 FROM scheduled_attempts a WHERE a.run_id=r.id AND a.retry_at>?) ORDER BY created LIMIT 1", (self.clock(),)).fetchone()
            if not row:
                return None
            if not self._definition_enabled(db, json.loads(row["definition"])):
                db.execute("UPDATE scheduled_runs SET status='cancelled',cancel_requested=1,finished=?,error='聊天计划任务权限未开启' WHERE id=?", (self.clock(), row["id"]))
                return None
            db.execute("UPDATE scheduled_runs SET status='running',owner=?,started=? WHERE id=?", (owner, self.clock(), row["id"]))
            return self._run_dict(db, db.execute("SELECT * FROM scheduled_runs WHERE id=?", (row["id"],)).fetchone())

    def cancelled(self, run_id, owner):
        with self.db() as db:
            row = db.execute("SELECT cancel_requested,status FROM scheduled_runs WHERE id=?", (run_id,)).fetchone()
            lease = db.execute("SELECT * FROM scheduled_lease WHERE id=1").fetchone()
            return not row or row["cancel_requested"] or row["status"] != "running" or not lease or lease["owner"] != owner or lease["expires"] <= self.clock()

    def finish(self, run_id, status, *, error=None, result=None, usage=None):
        with self.db(True) as db:
            db.execute("UPDATE scheduled_runs SET status=?,finished=?,error=?,result=COALESCE(?,result),usage=COALESCE(?,usage) WHERE id=? AND status='running'",
                       (status, self.clock(), str(error)[:4000] if error else None, json.dumps(result, ensure_ascii=False) if result is not None else None,
                        json.dumps(usage) if usage is not None else None, run_id))

    def fail_or_retry(self, run, error, *, category, transient=False, status="failed", notice_text=None, at_users=()):
        """Persist bounded retries or one final notice, fenced by the live lease."""
        from app.services.schedule_failures import failure_text
        with self.db(True) as db:
            now = self.clock()
            lease = db.execute("SELECT * FROM scheduled_lease WHERE id=1").fetchone()
            saved = db.execute("SELECT * FROM scheduled_runs WHERE id=?", (run["id"],)).fetchone()
            if (not lease or lease["owner"] != run["owner"] or lease["expires"] <= now
                    or not saved or saved["status"] != "running" or saved["owner"] != run["owner"]
                    or saved["cancel_requested"] or saved["result"]):
                return "unchanged"
            task = db.execute("SELECT * FROM scheduled_tasks WHERE id=?", (run["task_id"],)).fetchone()
            attempt = db.execute("SELECT COUNT(*) FROM scheduled_attempts WHERE run_id=?", (run["id"],)).fetchone()[0] + 1
            # thread_id is recorded BEFORE sending turn/start. Even a missing
            # start acknowledgement is uncertain, so it must never be replayed.
            uncertain = bool(saved["thread_id"])
            retry = (transient and not uncertain and attempt <= 2 and task
                     and task["version"] == run["version"] and task["status"] in {"active", "completed"}
                     and db.execute("SELECT enabled FROM scheduled_settings WHERE id=1").fetchone()[0]
                     and self._definition_enabled(db, run["definition"]))
            retry_at = now + (30 if attempt == 1 else 120) if retry else None
            db.execute("INSERT INTO scheduled_attempts VALUES(?,?,?,?,?,?)", (run["id"], attempt, category, str(error)[:4000], now, retry_at))
            if retry:
                db.execute("UPDATE scheduled_runs SET status='queued',owner=NULL,error=? WHERE id=?", (str(error)[:4000], run["id"]))
                return "retry_queued"
            text = notice_text or failure_text(run["definition"]["title"], category, uncertain=uncertain and transient, exhausted=attempt > 1)
            # Same task/category in an hour gets one chat notice; every run
            # still retains its own exact error and suppression marker in Web.
            prior = db.execute("SELECT 1 FROM scheduled_deliveries d JOIN scheduled_runs r ON r.id=d.run_id WHERE d.task_id=? AND d.kind='failure' AND d.created>=? AND d.status IN ('pending','sending','sent','unknown') AND json_extract(r.result,'$.failure_category')=? LIMIT 1", (run["task_id"], now - 3600, category)).fetchone()
            result = {"text": text, "attachments": [], "failure_notice": True, "failure_category": category, "notification_suppressed": bool(prior)}
            if at_users and notice_text:
                from app.services.codex_mention_tool import normalize_members
                result["at_users"] = list(normalize_members(at_users))
            db.execute("UPDATE scheduled_runs SET status=?,finished=?,error=?,result=? WHERE id=?", (status, now, str(error)[:4000], json.dumps(result, ensure_ascii=False), run["id"]))
            if not prior:
                delivery_id = uuid.uuid4().hex
                db.execute("INSERT INTO scheduled_deliveries(id,run_id,task_id,kind,payload,status,created) VALUES(?,?,?,'failure',?,'pending',?)", (delivery_id, run["id"], run["task_id"], text, now))
                self._link_message(db, delivery_id, run["definition"], version=run["version"], run_id=run["id"], task_id=run["task_id"])
            return "notified" if not prior else "suppressed"

    def pending_failure_runs(self):
        with self.db() as db:
            if not db.execute("SELECT enabled FROM scheduled_settings WHERE id=1").fetchone()[0]:
                return []
            rows = db.execute("SELECT DISTINCT d.run_id,t.definition FROM scheduled_deliveries d JOIN scheduled_tasks t ON t.id=d.task_id JOIN scheduled_runs r ON r.id=d.run_id WHERE (d.kind='failure' OR json_extract(r.result,'$.monitor')=1) AND d.status='pending' AND t.status!='deleted' ORDER BY d.created LIMIT 50").fetchall()
            return [r["run_id"] for r in rows if self._definition_enabled(db, json.loads(r["definition"]))][:10]

    @staticmethod
    def _goal_progress(db, task_id, config):
        from app.services.schedule_monitor import monitor_scope
        goal = config['goal']
        rows = db.execute('SELECT event_key,occurred_at,source FROM scheduled_goal_events WHERE task_id=? AND goal_key=? ORDER BY occurred_at,event_key',
                          (task_id, monitor_scope(config['key'], goal))).fetchall()
        count = len(rows)
        return {'target_count': goal['target_count'], 'completed_count': count,
                'remaining': max(0, goal['target_count'] - count), 'complete': count >= goal['target_count'],
                'unit': goal['unit'], 'completed_keys': [r['event_key'] for r in rows],
                'events': [dict(r) for r in rows]}

    @staticmethod
    def _monitor_state(db, task_id, key, goal=None):
        from app.services.schedule_monitor import monitor_scope
        scope = monitor_scope(key, goal)
        row = db.execute('SELECT revision,state,fingerprint,updated FROM scheduled_monitor_state WHERE task_id=? AND monitor_key=?', (task_id, scope)).fetchone()
        state = dict(row) if row else {'revision': 0, 'state': '', 'fingerprint': '', 'updated': None}
        last = db.execute("SELECT id,result,finished FROM scheduled_runs WHERE task_id=? AND json_extract(definition,'$.monitor.key')=? AND COALESCE(json_extract(definition,'$.monitor.goal.key'),'')=? AND json_extract(result,'$.monitor')=1 AND json_extract(result,'$.text')!='' ORDER BY finished DESC LIMIT 1", (task_id, key, goal['key'] if goal else '')).fetchone()
        state['last_result'] = ({'text': json.loads(last['result'])['text'][:4000], 'at': last['finished'],
                                 'deliveries': [r[0] for r in db.execute('SELECT status FROM scheduled_deliveries WHERE run_id=?', (last['id'],))]}
                                if last else None)
        if goal:
            state['goal_progress'] = ScheduledTaskService._goal_progress(db, task_id, {'key':key, 'goal':goal})
        return state

    def monitor_state(self, run):
        with self.db() as db:
            task = db.execute('SELECT version,status FROM scheduled_tasks WHERE id=?', (run['task_id'],)).fetchone()
            if not task or task['version'] != run['version'] or task['status'] == 'deleted':
                raise InterruptedError('任务已修改，旧监控执行已取消')
            return self._monitor_state(db, run['task_id'], run['definition']['monitor']['key'], run['definition']['monitor'].get('goal'))

    def save_result(self, run, text, attachments, usage, *, role_block=None, observation=None, at_users=()):
        from app.services.codex_mention_tool import normalize_members
        members = normalize_members(at_users)
        result = {"text": text, "attachments": attachments}
        with self.db(True) as db:
            self.require_enabled(db)
            if not self._definition_enabled(db, run["definition"]):
                raise InterruptedError("聊天计划任务权限已关闭")
            lease = db.execute("SELECT * FROM scheduled_lease WHERE id=1").fetchone()
            if not lease or lease['owner'] != run['owner'] or lease['expires'] <= self.clock():
                raise InterruptedError('调度租约已失效，执行结果需核对')
            if observation is not None:
                from app.services.schedule_monitor import validate_observation, validate_goal_observation, monitor_scope
                validate_observation(observation['outcome'])
                config = run['definition']['monitor']
                goal = config.get('goal')
                scope = monitor_scope(config['key'], goal)
                if goal:
                    validate_goal_observation(goal, observation['outcome'], self.clock())
                task = db.execute('SELECT version,status FROM scheduled_tasks WHERE id=?', (run['task_id'],)).fetchone()
                if not task or task['version'] != run['version'] or task['status'] == 'deleted':
                    raise InterruptedError('任务已修改或暂停，旧监控结果未提交')
                current = self._monitor_state(db, run['task_id'], config['key'], goal)
                if current['revision'] != observation['revision']:
                    raise InterruptedError('监控状态已更新，旧执行未覆盖新进度')
                baseline = current['revision'] == 0 and config['initial'] == 'baseline'
                events = observation['outcome']['events']
                fresh = []
                if goal:
                    remaining = current['goal_progress']['remaining']
                    if not remaining:
                        raise InterruptedError('目标已完成，旧执行不得增加进度')
                    events = sorted(events, key=lambda e: (instant(e['occurred_at']), e['key']))
                if baseline:
                    for key in observation['outcome'].get('observed_keys', []):
                        db.execute('INSERT OR IGNORE INTO scheduled_monitor_events VALUES(?,?,?,?,?)',
                                   (run['task_id'], scope, key.strip(), run['id'], self.clock()))
                for event in events:
                    if goal and len(fresh) >= remaining:
                        break
                    inserted = db.execute('INSERT OR IGNORE INTO scheduled_monitor_events VALUES(?,?,?,?,?)',
                                          (run['task_id'], scope, event['key'], run['id'], self.clock())).rowcount
                    if inserted and not baseline:
                        if goal:
                            counted = db.execute('INSERT OR IGNORE INTO scheduled_goal_events VALUES(?,?,?,?,?,?,?)',
                                (run['task_id'], scope, event['key'], instant(event['occurred_at']), event['source'], run['id'], self.clock())).rowcount
                            if not counted:
                                continue
                        fresh.append(event)
                text = '\n\n'.join(event['text'].strip() for event in fresh)
                result = {'text': text, 'attachments': [], 'monitor': True, 'events': fresh,
                          'quiet_reason': ('baseline' if baseline else 'duplicate' if events else 'no_change') if not fresh else None,
                          'state_revision': current['revision'] + 1}
                if goal:
                    result['goal_progress'] = self._goal_progress(db, run['task_id'], config)
                    if result['goal_progress']['complete']:
                        text += f"\n\n已完成约定的 {goal['target_count']} {goal['unit']}，这项安排已结束。"
                        result['text'] = text
                attachments = []
                db.execute('INSERT INTO scheduled_monitor_state VALUES(?,?,?,?,?,?) ON CONFLICT(task_id,monitor_key) DO UPDATE SET revision=excluded.revision,state=excluded.state,fingerprint=excluded.fingerprint,updated=excluded.updated',
                           (run['task_id'], scope, current['revision'] + 1, observation['outcome']['state'], observation.get('fingerprint', ''), self.clock()))
            if members and text:
                result["at_users"] = list(members)
            saved = db.execute("UPDATE scheduled_runs SET result=?,usage=? WHERE id=? AND status='running' AND result IS NULL AND cancel_requested=0 AND owner=?",
                               (json.dumps(result, ensure_ascii=False), json.dumps(usage), run["id"], run['owner'])).rowcount
            if not saved:
                raise InterruptedError('任务已停止，未创建交付记录')
            payloads = [("text", text)] if text else []
            payloads += [("file", str(a["path"])) for a in attachments]
            for kind, payload in payloads:
                db.execute("INSERT OR IGNORE INTO scheduled_deliveries(id,run_id,task_id,kind,payload,status,created) VALUES(?,?,?,?,?,'pending',?)",
                           (uuid.uuid4().hex, run["id"], run["task_id"], kind, payload, self.clock()))
                delivery = db.execute("SELECT id FROM scheduled_deliveries WHERE run_id=? AND kind=? AND payload=?", (run["id"], kind, payload)).fetchone()
                self._link_message(db, delivery["id"], run["definition"], version=run["version"], run_id=run["id"], task_id=run["task_id"])

            if observation is not None:
                db.execute("UPDATE scheduled_runs SET status=?,finished=? WHERE id=?", ('completed' if text else 'no_change', self.clock(), run['id']))

            if result.get('goal_progress', {}).get('complete'):
                definition = dict(run['definition'])
                definition.pop('pause', None)
                definition.pop('next_exception', None)
                db.execute("UPDATE scheduled_tasks SET status='completed',next_run=NULL,version=version+1,definition=?,updated=? WHERE id=?",
                           (json.dumps(definition, ensure_ascii=False), self.clock(), run['task_id']))
                self._version(db, run['task_id'], run['version'] + 1, definition,
                              ScheduleActor(name='系统', admin=True), '已完成有限目标，自动结束调度')
                db.execute("UPDATE scheduled_runs SET status='cancelled',finished=?,error='目标已完成' WHERE task_id=? AND status='queued'",
                           (self.clock(), run['task_id']))

            if role_block:
                # Fenced by lease/cancellation checks above. A stale run must
                # never pause a newer edit or resurrect a deleted task.
                row = db.execute("SELECT * FROM scheduled_tasks WHERE id=?", (run["task_id"],)).fetchone()
                if row and row["version"] == run["version"] and row["status"] != "deleted":
                    definition = json.loads(row["definition"])
                    definition["pause"] = {"reason": "role", "until": None}
                    definition["role_block"] = {**role_block, "run_id": run["id"], "at": self.clock()}
                    db.execute("UPDATE scheduled_tasks SET status='paused',next_run=NULL,version=version+1,definition=?,updated=? WHERE id=?", (json.dumps(definition, ensure_ascii=False), self.clock(), run["task_id"]))
                    self._version(db, run["task_id"], row["version"] + 1, definition, ScheduleActor(name="系统", admin=True), "角色限制自动暂停：" + role_block["reason"])
                    db.execute("UPDATE scheduled_runs SET status='cancelled',finished=?,error='角色限制已暂停任务' WHERE task_id=? AND status='queued'", (self.clock(), run["task_id"]))
                db.execute("UPDATE scheduled_runs SET status='role_blocked',finished=?,error=? WHERE id=?", (self.clock(), role_block["reason"], run["id"]))

    def deliver(self, delivery_id, send, *, retry=False):
        """A missing send acknowledgement is unknown, never an automatic replay."""
        with self.db(True) as db:
            self.require_enabled(db)
            row = db.execute("SELECT * FROM scheduled_deliveries WHERE id=?", (delivery_id,)).fetchone()
            if not row:
                raise ScheduleError("发送记录不存在")
            task = db.execute("SELECT definition,status FROM scheduled_tasks WHERE id=?", (row["task_id"],)).fetchone()
            if not task or not self._definition_enabled(db, json.loads(task["definition"])):
                raise ScheduleDenied("聊天计划任务权限未开启，禁止交付")
            if row["status"] == "sent":
                return True
            if task["status"] == "deleted":
                raise ScheduleDenied("任务已取消，禁止交付")
            if row["status"] not in ({"pending", "failed"} if retry else {"pending"}):
                return False
            db.execute("UPDATE scheduled_deliveries SET status='sending',error=NULL WHERE id=?", (delivery_id,))
        status, error = "unknown", "发送结果未确认"
        try:
            # WeChat may have acted even when it returns false: keep conservative.
            success = bool(send(dict(row)))
            status = "sent" if success else "unknown"
            return success
        except ScheduleDenied as exc:
            status, error = "failed", str(exc)  # Validation failed before a send.
            return False
        finally:
            with self.db(True) as db:
                db.execute("UPDATE scheduled_deliveries SET status=?,error=? WHERE id=?", (status, None if status == "sent" else error, delivery_id))

    def run(self, run_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM scheduled_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ScheduleError("执行记录不存在")
            return self._run_dict(db, row)


_service = None
_service_lock = threading.Lock()


def get_scheduled_tasks():
    global _service
    with _service_lock:
        if _service is None:
            _service = ScheduledTaskService()
    return _service
