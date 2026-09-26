"""Own long replies independently of the per-chat EventBus worker.

Chat identity always remains the real WeChat chat. A task holds its own thread
snapshot; only the current task may publish that snapshot as the chat head.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from dataclasses import replace
import copy
import json
import logging
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

logger = logging.getLogger(__name__)
_task = ContextVar("assistant_task", default=None)


def current_task(chat=None):
    task = _task.get()
    return task if task is not None and (chat is None or task.chat == chat) else None


class TaskStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS assistant_tasks (
                    id TEXT PRIMARY KEY, chat TEXT NOT NULL, message_id TEXT,
                    status TEXT NOT NULL, background INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL, thread_id TEXT, turn_id TEXT,
                    state_json TEXT, result_json TEXT, error TEXT
                );
                CREATE INDEX IF NOT EXISTS assistant_tasks_chat ON assistant_tasks(chat, created);
                CREATE TABLE IF NOT EXISTS assistant_chat_heads (chat TEXT PRIMARY KEY, task_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS assistant_outbox (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, method TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, error TEXT
                );
            """)

    def connect(self):
        # closing() matters: sqlite's transaction context does not close handles.
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return _Connection(connection)

    def recover(self):
        # Never replay side effects after losing the local owner/acknowledgement.
        with self.connect() as db:
            db.execute("UPDATE assistant_tasks SET status='interrupted', error='应用重启，执行结果待核对', updated=? "
                       "WHERE status IN ('queued','running','background','network_wait','delivering')", (time.time(),))
            db.execute("UPDATE assistant_outbox SET status='uncertain', error='发送确认前应用重启' WHERE status='sending'")

    def create(self, task, message_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if message_id and db.execute("SELECT 1 FROM assistant_tasks WHERE chat=? AND message_id=? "
                "AND status NOT IN ('skipped','failed','interrupted') AND "
                "(thread_id IS NOT NULL OR status IN ('running','background','network_wait','delivering')) LIMIT 1",
                (task.chat, message_id)).fetchone():
                return False
            db.execute("INSERT INTO assistant_tasks(id,chat,message_id,status,created,updated) VALUES(?,?,?,'queued',?,?)",
                       (task.id, task.chat, message_id, time.time(), time.time()))
        return True

    def update(self, task_id, **values):
        allowed = {'status', 'background', 'thread_id', 'turn_id', 'state_json', 'result_json', 'error'}
        if not set(values) <= allowed:
            raise ValueError("Unknown task field")
        values['updated'] = time.time()
        with self.connect() as db:
            db.execute("UPDATE assistant_tasks SET " + ','.join(f'{key}=?' for key in values) + " WHERE id=?",
                       (*values.values(), task_id))

    def activate(self, task):
        with self.connect() as db:
            db.execute("INSERT INTO assistant_chat_heads(chat,task_id) VALUES(?,?) "
                       "ON CONFLICT(chat) DO UPDATE SET task_id=excluded.task_id", (task.chat, task.id))

    def is_head(self, task):
        with self.connect() as db:
            row = db.execute("SELECT task_id FROM assistant_chat_heads WHERE chat=?", (task.chat,)).fetchone()
            return row is not None and row[0] == task.id

    def save_state(self, task, state):
        payload = json.dumps(state, ensure_ascii=False)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE assistant_tasks SET state_json=?,updated=? WHERE id=?", (payload, time.time(), task.id))
            head = db.execute("SELECT task_id FROM assistant_chat_heads WHERE chat=?", (task.chat,)).fetchone()
            if head and head[0] == task.id:
                db.execute("""INSERT INTO codex_thread_state
                    (chat_id,state_json,codex_version,schema_hash,config_signature,updated_at) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(chat_id) DO UPDATE SET state_json=excluded.state_json,
                    codex_version=excluded.codex_version,schema_hash=excluded.schema_hash,
                    config_signature=excluded.config_signature,updated_at=excluded.updated_at""",
                    (task.chat, payload, state.get('codex_version'), state.get('schema_hash'),
                     state.get('config_signature'), state.get('updated_at') or str(time.time())))

    def completions(self, chat, exclude):
        with self.connect() as db:
            rows = db.execute("SELECT id,result_json FROM assistant_tasks WHERE chat=? AND background=1 "
                              "AND status IN ('completed','delivery_failed') AND result_json IS NOT NULL ORDER BY created DESC LIMIT 8", (chat,)).fetchall()
        return [(r['id'], json.loads(r['result_json'])) for r in reversed(rows) if r['id'] not in exclude]

    def recent(self, chat=None):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT id,chat,status,background,created,updated,thread_id,turn_id,error FROM assistant_tasks "
                + ("WHERE chat=? " if chat else "") + "ORDER BY created DESC LIMIT 20", (chat,) if chat else ()).fetchall()]

    def lookup(self, chat, prefix):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM assistant_tasks WHERE chat=? AND id LIKE ? LIMIT 2',
                                                (chat, prefix + '%')).fetchall()]

    def delivery(self, task, method, args, kwargs, send):
        delivery_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO assistant_outbox(id,task_id,method,payload,status,created) VALUES(?,?,?,?,'sending',?)",
                       (delivery_id, task.id, method, json.dumps({'args': args, 'kwargs': kwargs}, ensure_ascii=False, default=str), time.time()))
        status, error = 'uncertain', None
        try:
            result = send(*args, **kwargs)
            status = 'sent' if result else 'failed'
            return result
        except Exception as exc:
            error = str(exc)[:1000]
            raise
        finally:
            with self.connect() as db:
                db.execute("UPDATE assistant_outbox SET status=?,error=? WHERE id=?", (status, error, delivery_id))


class _Connection:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *args):
        try:
            self.db.__exit__(*args)
        finally:
            self.db.close()


class TaskScope:
    def __init__(self, store, chat, parent=None):
        self.store, self.chat = store, chat
        self.id = uuid.uuid4().hex
        self.parent = parent
        self.base_state = copy.deepcopy(parent.base_state) if parent else None
        self.state = copy.deepcopy(self.base_state)
        self.fork_required = parent is not None
        self.activated = False
        self.background = False
        self.detachable = True
        self.done = threading.Event()
        self.bound = threading.Event()
        self.manager = None
        self.thread_id = self.turn_id = ''
        self.started = time.monotonic()
        self.deadline = None
        self.error = None
        self.result_ready = False
        self.receipts = []
        self.lock = threading.RLock()

    def load_state(self, state_store):
        if not self.activated:
            if self.parent is None:
                self.base_state = copy.deepcopy(state_store.get(self.chat))
                self.state = copy.deepcopy(self.base_state)
            self.store.activate(self)
            self.activated = True
            if self.fork_required:
                pending = dict(self.state or {})
                pending.update(thread_id=None, last_turn_id=None, continuity_status='branch_pending')
                self.store.save_state(self, pending)
            self.store.update(self.id, status='running')
        return copy.deepcopy(self.state)

    def save_state(self, state):
        state = copy.deepcopy(state)
        state['background_receipts'] = list(dict.fromkeys([*(self.state or {}).get('background_receipts', []), *self.receipts]))[-100:]
        self.state = state
        self.store.save_state(self, state)

    def context_messages(self):
        messages = []
        internal_context = '内部任务上下文，仅用于衔接会话；对外回复不要提及任务编号或后台状态。\n'
        if self.parent:
            messages.append({'role': 'user', 'name': 'background_task_context', 'content':
                             internal_context + (f'本轮继续已完成任务 {self.parent.id[:8]}，沿用其会话历史。' if self.parent.done.is_set() else
                              f'后台任务 {self.parent.id[:8]} 由独立会话继续执行。本轮只处理当前新请求，避免重复执行后台任务。')})
        excluded = set((self.state or {}).get('background_receipts', [])) | {self.id}
        for task_id, result in self.store.completions(self.chat, excluded):
            self.receipts.append(task_id)
            messages.append({'role': 'user', 'name': 'background_task_result', 'content':
                             internal_context + f'后台任务 {task_id[:8]} 已完成，以下为结果摘要和产物记录：\n' + json.dumps(result, ensure_ascii=False)[:6000]})
        return messages

    def bind(self, manager, thread_id, turn_id):
        with self.lock:
            self.result_ready = False
            self.manager, self.thread_id, self.turn_id = manager, thread_id, turn_id
            self.store.update(self.id, thread_id=thread_id, turn_id=turn_id)
            self.bound.set()

    def completed_result(self, response):
        message = (response.get('choices') or [{}])[0].get('message') or {}
        with self.lock:
            self.result_ready = True
            self.store.update(self.id, result_json=json.dumps({
                'text': str(message.get('content') or '')[:12000],
                'attachments': response.get('attachments') or [],
            }, ensure_ascii=False), status='delivering')


class TaskWechatProxy:
    def __init__(self, wx, task):
        self._wx, self._task = wx, task

    def __getattr__(self, name):
        value = getattr(self._wx, name)
        if name not in {'send_message', 'send_files', 'send_url_card'}:
            return value

        def send(*args, **kwargs):
            return self._task.store.delivery(self._task, name, args, kwargs, value)
        return send


class AssistantTaskSupervisor:
    def __init__(self, state_store, threshold=None, max_workers=8):
        self.state_store = state_store
        self.store = TaskStore(state_store.path)
        self.store.recover()
        self.threshold = threshold
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='assistant-task')
        self._control_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='assistant-control')
        self._control_slots = threading.BoundedSemaphore(16)
        self._slots = threading.BoundedSemaphore(max_workers)
        self._lock = threading.RLock()
        self._active = {}
        self._closed = False

    def _seconds(self):
        if self.threshold is not None:
            return self.threshold()
        from app.utils.plugin_config import get_config
        return max(1, min(120, int(get_config('background_after_minutes', 10, plugin_name='assistant') or 10))) * 60

    @staticmethod
    def _control_text(event, handler=None):
        text = str(event.data.get('message') or '').strip()
        if '任务' in text and handler is not None and hasattr(handler, '_clean_query'):
            text = handler._clean_query(text, str(event.data.get('chat_name') or '')).strip()
        return text

    def dispatch(self, event, handler):
        chat = str(event.data.get('chat_name') or '')
        if self._closed:
            return False
        if event.data.get('_assistant_control_handled'):
            return True
        owner = getattr(handler, '__self__', None)
        controls_allowed = owner is None or not hasattr(owner, '_is_sender_ignored') or (
            not owner._is_sender_ignored(chat, str(event.data.get('sender') or ''))
            and (event.data.get('chat_type') != 'group' or
                 owner.allow_mention_trigger and owner._event_mentions_bot(event)))
        control_text = self._control_text(event, owner)
        command_event = replace(event, data={**event.data, 'message': control_text})
        command = self._command(command_event, chat) if controls_allowed else None
        if command is not None:
            return command
        self._slots.acquire()
        if self._closed:
            self._slots.release()
            return False
        with self._lock:
            parent = next((t for t in self._active.values() if t.chat == chat and t.background
                           and not t.done.is_set() and self.store.is_head(t)), None)
            continuation = re.fullmatch(r'任务\s+([0-9a-f]{8,32})\s+继续\s+(.+)', control_text, re.S)
            if continuation:
                rows = self.store.lookup(chat, continuation[1])
                if len(rows) == 1 and rows[0]['status'] == 'completed' and rows[0]['state_json']:
                    parent = TaskScope(self.store, chat)
                    parent.id = rows[0]['id']
                    parent.base_state = json.loads(rows[0]['state_json'])
                    parent.done.set()
            task = TaskScope(self.store, chat, parent)
            if not self.store.create(task, str(event.data.get('message_id') or event.id)):
                self._slots.release()
                return True
            self._active[task.id] = task
        # EventBus mutates/restores its context after each listener. Own a copy,
        # including this listener's wx consumption/logging proxy, for delayed sends.
        owned = replace(event, data=dict(event.data), context=dict(event.context))
        wx = owned.context.get('wx')
        if wx is not None:
            owned.context['wx'] = TaskWechatProxy(wx, task)
        context = copy_context()
        try:
            future = self._pool.submit(context.run, self._run, task, owned, handler)
        except Exception:
            with self._lock:
                self._active.pop(task.id, None)
            self._slots.release()
            raise
        while not task.done.wait(0.25):
            if time.monotonic() - task.started < self._seconds() or not task.bound.is_set() or not task.detachable:
                continue
            with self._lock, task.lock:
                # One background task globally leaves interactive pool capacity.
                # A second long task waits until that slot is actually released.
                if task.done.is_set() or task.result_ready or any(t.background and not t.done.is_set() for t in self._active.values()):
                    continue
                task.background = True
                self.store.update(task.id, status='background', background=1)
            logger.info('Assistant task detached: chat=%s task=%s elapsed=%.1fs', chat, task.id, time.monotonic() - task.started)
            return True
        return future.result()

    def _run(self, task, event, handler):
        token = _task.set(task)
        try:
            if self._closed:
                self.store.update(task.id, status='interrupted', error='助手停止')
                return False
            result = handler(event)
            status = 'failed' if task.error else 'completed' if result else 'delivery_failed' if task.result_ready else 'skipped'
            self.store.update(task.id, status=status, error=task.error or ('结果已生成，发送未确认' if status == 'delivery_failed' else None))
            return result
        except Exception as exc:
            self.store.update(task.id, status='failed', error=str(exc)[:2000])
            logger.exception('Assistant task failed: task=%s chat=%s', task.id, task.chat)
            raise
        finally:
            _task.reset(token)
            with self._lock, task.lock:
                task.done.set()
                self._active.pop(task.id, None)
            self._slots.release()

    def submit_control(self, event, db_session_factory, handler):
        """Authorize control messages before a bounded, separate ingress lane."""
        from app.core.event_bus import EventType, _parse_sender_blacklist
        from app.models.user_permission import WeChatUser
        from app.models.assistant_policy import AssistantChatPolicy
        if self._closed or event.type not in {EventType.TEXT_MESSAGE_RECEIVED, EventType.QUOTE_TEXT_MESSAGE_RECEIVED}:
            return
        text = self._control_text(event, handler)
        # Explicit task references only; ordinary conversational messages keep
        # the existing judge, mention and plugin propagation behavior.
        if not re.fullmatch(r'任务\s+[0-9a-f]{8,32}\s+(状态|取消|补充\s+.+)', text, re.S):
            return
        chat, sender = str(event.data.get('chat_name') or ''), str(event.data.get('sender') or '')
        with db_session_factory() as db:
            user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat).first()
            if not user or sender in _parse_sender_blacklist(user.sender_blacklist):
                return
            policy = db.query(AssistantChatPolicy).filter(AssistantChatPolicy.user_id == user.id).first()
            if not policy or not policy.enabled or handler._is_sender_ignored(chat, sender):
                return
        if event.data.get('chat_type') == 'group' and not (handler.allow_mention_trigger and handler._event_mentions_bot(event)):
            return
        if not self._control_slots.acquire(blocking=False):
            return
        owned = replace(event, data=dict(event.data), context=dict(event.context))
        owned.data['message'] = text
        try:
            future = self._control_pool.submit(self._command, owned, chat, False)
        except Exception:
            self._control_slots.release()
            raise
        future.add_done_callback(lambda _: self._control_slots.release())
        event.data['_assistant_control_handled'] = True

    def _command(self, event, chat, silent=True):
        match = re.fullmatch(r'任务\s+([0-9a-f]{8,32})\s+(状态|取消|补充\s+.+)', str(event.data.get('message') or '').strip(), re.S)
        if not match:
            return None
        prefix, action = match.groups()
        rows = self.store.lookup(chat, prefix)
        if len(rows) != 1:
            text = '没有找到该任务。'
        else:
            row = rows[0]
            with self._lock:
                active = self._active.get(row['id'])
            if action == '状态':
                labels = {'queued': '排队中', 'running': '执行中', 'background': '后台执行中', 'completed': '已完成',
                          'network_wait': '等待连接恢复', 'delivering': '正在发送结果', 'interrupted': '已中断，需核对结果',
                          'failed': '执行失败', 'skipped': '未触发回复', 'delivery_failed': '结果已生成，发送未确认'}
                text = f'任务 {prefix}：{labels.get(row["status"], row["status"])}'
            elif not active or not active.bound.is_set():
                text = '该任务当前没有可操作的执行轮次。'
            elif action == '取消':
                try:
                    active.manager._request('turn/interrupt', {'threadId': active.thread_id, 'turnId': active.turn_id}, timeout=10)
                    text = f'已请求取消任务 {prefix}。'
                except Exception:
                    text = f'任务 {prefix} 的取消尚未确认，请稍后查询状态。'
            else:
                try:
                    params = {'threadId': active.thread_id, 'expectedTurnId': active.turn_id,
                              'input': [{'type': 'text', 'text': action[2:].strip()}]}
                    self.store.delivery(active, 'turn/steer', (params,), {},
                        lambda body: active.manager._request('turn/steer', body, timeout=15))
                    text = f'已补充到任务 {prefix}。'
                except Exception as exc:
                    logger.warning('Task steer was not confirmed: task=%s error=%s', active.id, exc)
                    text = f'任务 {prefix} 暂未确认收到补充，请稍后查询状态。'
        wx = event.context.get('wx')
        if wx:
            wx.send_message(chat, text, **({'silent': True} if silent else {}))
        return True

    def status(self):
        return self.store.recent()

    def close(self):
        self._closed = True
        with self._lock:
            active = list(self._active.values())
        for task in active:
            if task.bound.is_set():
                task.manager._interrupt_turn(task.thread_id, task.turn_id)
        self._pool.shutdown(wait=False, cancel_futures=False)
        self._control_pool.shutdown(wait=False, cancel_futures=True)
