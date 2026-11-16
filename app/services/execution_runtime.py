"""Leased generations, original-run control and bounded shared-account workers."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass, field
import threading
import time
from typing import Callable

from app.services.execution_contracts import ExecutionError, TaskContext


class RunHandle:
    """Cancellation is a request until both native work and tools have drained."""
    def __init__(self, context, attempt_id, generation, *, persist_cancel, cancel, append=None):
        self.context, self.attempt_id, self.generation = context, attempt_id, generation
        self.session_id = self.turn_id = ""
        self._persist_cancel, self._cancel, self._append = persist_cancel, cancel, append
        self.cancel_requested = threading.Event()
        self._terminal, self._drained = threading.Event(), threading.Event()
        self._drained.set()
        self._lock = threading.RLock()
        self._tool_count = 0
        self.result = None

    def bind(self, session_id, turn_id=""):
        with self._lock:
            self.session_id, self.turn_id = session_id, turn_id
            cancelled = self.cancel_requested.is_set()
        if cancelled:
            self._cancel(self)

    def cancel(self):
        with self._lock:
            if self.cancel_requested.is_set():
                return
            self._persist_cancel(self.context.run_id)
            self.cancel_requested.set()
        self._cancel(self)

    def append(self, text):
        if self.cancel_requested.is_set() or self._terminal.is_set():
            raise ExecutionError("invalid_state", "Cannot append to a cancelled or completed run")
        if self._append is None:
            raise ExecutionError("append_unsupported", "This engine requires a separately queued next turn")
        return self._append(self, text)

    @contextmanager
    def tool(self):
        with self._lock:
            if self.cancel_requested.is_set() or self._terminal.is_set():
                raise ExecutionError("cancelled", "Run no longer accepts tools")
            self._tool_count += 1
            self._drained.clear()
        try:
            yield self.context
        finally:
            with self._lock:
                self._tool_count -= 1
                if self._tool_count == 0:
                    self._drained.set()

    def complete(self, result):
        with self._lock:
            self.result = result
            self._terminal.set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        if not self._terminal.wait(timeout):
            return False
        remaining = None if deadline is None else max(0, deadline - time.monotonic())
        return self._drained.wait(remaining)

    @property
    def status(self):
        if self._terminal.is_set() and self._drained.is_set():
            return "cancelled" if self.cancel_requested.is_set() else "completed"
        return "cancel_requested" if self.cancel_requested.is_set() else "running"


@dataclass
class _Generation:
    key: tuple
    factory: Callable
    runtime: object = None
    pins: int = 0
    leases: int = 0
    retiring: bool = False
    revoked: bool = False
    last_used: float = field(default_factory=time.monotonic)
    start_lock: threading.Lock = field(default_factory=threading.Lock)


class RuntimeLease:
    def __init__(self, registry, entry):
        self.registry, self.entry = registry, entry
        self.runtime = entry.runtime
        self.generation = entry.key[-1]
        self._released = False

    def release(self):
        with self.registry._lock:
            if self._released:
                return
            self._released = True
            self.entry.leases -= 1
            self.entry.last_used = time.monotonic()
        self.registry.reap()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


class ConfigPin:
    def __init__(self, registry, entry):
        self.registry, self.entry, self._released = registry, entry, False

    def release(self):
        with self.registry._lock:
            if self._released:
                return
            self._released = True
            self.entry.pins -= 1
        self.registry.reap()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


class RuntimeRegistry:
    def __init__(self, *, durable_pins=lambda *key: 0):
        self._lock = threading.RLock()
        self._entries = {}
        self._durable_pins = durable_pins

    def register(self, engine, profile_id, generation, factory, *, current=True):
        key = engine, profile_id, generation
        with self._lock:
            if key in self._entries:
                return
            if current:
                for old_key, entry in self._entries.items():
                    if old_key[:2] == key[:2]:
                        entry.retiring = True
            self._entries[key] = _Generation(key, factory, retiring=not current)
        self.reap()

    def pin(self, key):
        with self._lock:
            entry = self._entries[key]
            if entry.revoked or entry.retiring:
                raise ExecutionError("profile_unavailable", "Profile no longer accepts new bindings")
            entry.pins += 1
        return ConfigPin(self, entry)

    def acquire(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.revoked:
                raise ExecutionError("profile_unavailable", "Frozen profile generation is unavailable")
            entry.leases += 1  # protects a concurrent cold start from reaping
        try:
            with entry.start_lock:
                if entry.runtime is not None and hasattr(entry.runtime, 'is_alive') and not entry.runtime.is_alive():
                    if entry.leases > 1:
                        raise ExecutionError("interrupted_unknown", "Disconnected runtime is still draining original tasks")
                    entry.runtime.stop()
                    entry.runtime = None
                if entry.runtime is None:
                    runtime = entry.factory()
                    try:
                        runtime.start()
                    except BaseException:
                        runtime.stop()
                        raise
                    entry.runtime = runtime
                if entry.revoked:
                    raise ExecutionError("authorization_revoked", "Account authorization was revoked")
            return RuntimeLease(self, entry)
        except BaseException:
            with self._lock:
                entry.leases -= 1
            self.reap()
            raise

    def retire(self, engine, profile_id, *, revoke=False):
        to_stop = []
        with self._lock:
            for key, entry in self._entries.items():
                if key[:2] == (engine, profile_id):
                    entry.retiring = True
                    entry.revoked |= revoke
                    if revoke and entry.runtime:
                        to_stop.append(entry.runtime)
        for runtime in to_stop:
            getattr(runtime, "force_stop", runtime.stop)()
        self.reap()

    def shutdown(self):
        with self._lock:
            values = [entry.runtime for entry in self._entries.values() if entry.runtime]
            for entry in self._entries.values():
                entry.runtime = None
        for runtime in values:
            getattr(runtime, "force_stop", runtime.stop)()

    def reap(self, *, idle_seconds=None):
        stopping = []
        with self._lock:
            for key, entry in list(self._entries.items()):
                if entry.leases or entry.pins or self._durable_pins(*key):
                    continue
                if entry.retiring:
                    self._entries.pop(key)
                    if entry.runtime:
                        stopping.append(entry.runtime)
                elif idle_seconds is not None and time.monotonic() - entry.last_used >= idle_seconds:
                    if entry.runtime:
                        stopping.append(entry.runtime)
                        entry.runtime = None
        for runtime in stopping:
            runtime.stop()

    def status(self):
        with self._lock:
            return [{"engine": k[0], "profile_id": k[1], "generation": k[2],
                     "running": e.runtime is not None, "pins": e.pins + self._durable_pins(*k),
                     "leases": e.leases, "retiring": e.retiring, "revoked": e.revoked}
                    for k, e in self._entries.items()]


@dataclass
class _Ticket:
    context: TaskContext
    account: str
    child: bool
    priority: int
    enqueued: float = field(default_factory=time.monotonic)
    sequence: int = 0
    future: Future = field(default_factory=Future)
    callback: object = None
    captured: object = None


class AccountScheduler:
    """Two worker lanes; one account budget across engines, fair per chat.

    The reserved lane is reachable only with a registered parent context. The
    callback must use an independent native Session/worker, not the parent's.
    """
    def __init__(self, *, normal_slots=2, child_slots=1, total_slots=3,
                 per_chat=1, max_queue=64, active_check=lambda ctx: None):
        if min(normal_slots, child_slots, per_chat, max_queue) < 1 or normal_slots + child_slots > total_slots:
            raise ValueError("Nested execution requires reserved capacity inside the account limit")
        self.normal_slots, self.child_slots = normal_slots, child_slots
        self.per_chat, self.max_queue = per_chat, max_queue
        self._condition = threading.Condition(threading.RLock())
        self._queue, self._active, self._chats, self._parents = [], Counter(), Counter(), {}
        self._sequence = 0
        self._closed = False
        self._active_check = active_check
        self._normal = ThreadPoolExecutor(normal_slots, thread_name_prefix="model-normal")
        self._child = ThreadPoolExecutor(child_slots, thread_name_prefix="model-child")
        self._lane_active = Counter()
        self._backoff = {}
        self._dispatcher = threading.Thread(target=self._dispatch_loop, name="model-dispatch", daemon=True)
        self._dispatcher.start()

    def submit(self, context, account_key, callback, *, priority=0):
        with self._condition:
            self._active_check(context)
            if self._closed or len(self._queue) >= self.max_queue:
                raise ExecutionError("queue_full", "Model queue is full or closed")
            child = bool(context.parent_run_id)
            if child:
                parent = self._parents.get(context.parent_run_id)
                if not parent or parent.depth or context.deadline > parent.deadline or context.source_chat_id != parent.source_chat_id or context.authorization_ref != parent.authorization_ref:
                    raise ExecutionError("invalid_parent", "Child capacity requires a live host parent")
            ticket = _Ticket(context, account_key, child, priority, sequence=self._sequence)
            self._sequence += 1
            ticket.callback, ticket.captured = callback, copy_context()
            self._queue.append(ticket)
            self._condition.notify_all()
        return ticket.future

    def _dispatch_loop(self):
        while True:
            with self._condition:
                if self._closed:
                    for ticket in self._queue:
                        ticket.future.cancel()
                    self._queue.clear()
                    return
                eligible = []
                for ticket in list(self._queue):
                    ctx = ticket.context
                    if ticket.future.cancelled():
                        self._queue.remove(ticket)
                        continue
                    try:
                        self._active_check(ctx)
                        if time.time() >= ctx.deadline:
                            raise ExecutionError("deadline_exceeded", "Task expired while queued")
                        if ticket.child and ctx.parent_run_id not in self._parents:
                            raise ExecutionError("invalid_parent", "Parent finished before its child dispatched")
                    except Exception as exc:
                        self._queue.remove(ticket)
                        ticket.future.set_exception(exc)
                        continue
                    capacity = self.child_slots if ticket.child else self.normal_slots
                    lane = ticket.account, ticket.child
                    chat = ticket.account, ticket.child, ctx.source_chat_id
                    if self._backoff.get(ticket.account, 0) <= time.monotonic() and self._lane_active[ticket.child] < capacity and self._active[lane] < capacity and self._chats[chat] < self.per_chat:
                        eligible.append(ticket)
                if not eligible:
                    self._condition.wait(0.1)
                    continue
                ticket = max(eligible, key=lambda t: (t.priority + (time.monotonic() - t.enqueued) / 10, -t.sequence))
                self._queue.remove(ticket)
                if not ticket.future.set_running_or_notify_cancel():
                    continue
                self._lane_active[ticket.child] += 1
                self._active[(ticket.account, ticket.child)] += 1
                self._chats[(ticket.account, ticket.child, ticket.context.source_chat_id)] += 1
                self._parents[ticket.context.run_id] = ticket.context
                pool = self._child if ticket.child else self._normal
                pool.submit(ticket.captured.run, self._run, ticket)

    def _run(self, ticket):
        try:
            self._active_check(ticket.context)
            ticket.future.set_result(ticket.callback())
        except BaseException as exc:
            ticket.future.set_exception(exc)
        finally:
            with self._condition:
                self._lane_active[ticket.child] -= 1
                self._active[(ticket.account, ticket.child)] -= 1
                self._chats[(ticket.account, ticket.child, ticket.context.source_chat_id)] -= 1
                self._parents.pop(ticket.context.run_id, None)
                self._condition.notify_all()

    def status(self):
        with self._condition:
            return {"queued": len(self._queue), "running": sum(self._active.values()),
                    "normal_slots": self.normal_slots, "child_slots": self.child_slots,
                    "quota_remaining": None}

    def is_running(self, run_id):
        with self._condition:
            return run_id in self._parents

    def backoff(self, account_key, seconds):
        with self._condition:
            self._backoff[account_key] = max(self._backoff.get(account_key, 0), time.monotonic() + min(3600, max(1, seconds)))
            self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._dispatcher.join(timeout=5)
        self._normal.shutdown(wait=True, cancel_futures=True)
        self._child.shutdown(wait=True, cancel_futures=True)
