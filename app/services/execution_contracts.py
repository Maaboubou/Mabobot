"""Versioned host contracts. Identity is supplied by admission, never by a model.

JSON fields are stored as canonical strings so a frozen dataclass really is an
immutable snapshot (including nested model options and image policy).
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
import hashlib
import json
import time
import uuid
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class ExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


@dataclass(frozen=True)
class TaskContext:
    task_id: str
    run_id: str
    request_id: str
    source_kind: str
    source_chat_id: str
    actor_id: str
    component: str
    task_type: str
    delivery_chat_id: str
    delivery_owner: str
    authorization_ref: str
    permission_revision: str
    accepted_at: float
    deadline: float
    parent_run_id: str = ""
    purpose: str = "foreground"
    depth: int = 0
    contract_version: int = 1

    def __post_init__(self):
        if not all((self.task_id, self.run_id, self.request_id, self.actor_id,
                    self.authorization_ref, self.component, self.task_type)):
            raise ValueError("Task identity and host authorization are required")
        if not self.source_chat_id and self.source_kind != "system":
            raise ValueError("A chat task cannot inherit system authority")
        if self.delivery_chat_id and not self.delivery_owner:
            raise ValueError("A delivery must have one owner")
        if self.deadline <= self.accepted_at or self.depth not in (0, 1):
            raise ValueError("Invalid deadline or model nesting depth")
        if bool(self.parent_run_id) != bool(self.depth):
            raise ValueError("Only registered children may reserve child capacity")

    @classmethod
    def create(cls, *, timeout: float = 600, **identity):
        now = time.time()
        return cls(task_id=uuid.uuid4().hex, run_id=uuid.uuid4().hex,
                   accepted_at=now, deadline=now + timeout, **identity)

    def child(self, *, component: str, task_type: str) -> TaskContext:
        if self.depth:
            raise ExecutionError("nesting_limit", "Host model nesting is limited to one level")
        return TaskContext(**{**asdict(self), "task_id": uuid.uuid4().hex,
            "run_id": uuid.uuid4().hex, "request_id": uuid.uuid4().hex,
            "parent_run_id": self.run_id, "depth": 1, "component": component,
            "task_type": task_type, "purpose": "child", "delivery_chat_id": "",
            "delivery_owner": "", "accepted_at": time.time()})


@dataclass(frozen=True)
class RouteCandidate:
    card_id: str
    engine: str
    profile_id: str
    generation: str
    model: str
    account_key: str
    auth_ref: str
    parameters_json: str = "{}"
    channel: str = ""
    capability_revision: str = ""
    profile_config_json: str = "{}"

    def __post_init__(self):
        if self.engine not in {"codex", "opencode", "api"}:
            raise ValueError("Unknown execution engine")
        if not all((self.card_id, self.model, self.generation, self.account_key)):
            raise ValueError("Incomplete route candidate")
        params = json.loads(self.parameters_json)
        if not isinstance(params, dict):
            raise ValueError("Model parameters must be an object")
        if any(key in canonical(params).lower() for key in ('access_token', 'refresh_token', 'api_key')):
            raise ValueError("Snapshots hold credential references, not secrets")


@dataclass(frozen=True)
class RouteSnapshot:
    revision: str
    mode: str
    candidates: tuple[RouteCandidate, ...]
    max_attempts: int = 2
    output_contract_json: str = "{}"
    image_policy_json: str = "{}"
    tools: tuple[str, ...] = ()
    role_signature: str = ""
    tools_version: str = "1"
    config_version: str = "1"
    version: int = 1

    def __post_init__(self):
        if self.mode not in {"legacy", "direct", "tools"} or not self.candidates:
            raise ValueError("A route needs a processing mode and candidate chain")
        if self.max_attempts < 1 or (self.mode == "direct" and self.tools):
            raise ValueError("Invalid attempt budget or strict tool policy")
        if not isinstance(self.candidates, tuple) or not isinstance(self.tools, tuple):
            raise ValueError("Route collections must be immutable tuples")

    @classmethod
    def from_dict(cls, value):
        return cls(**{**value, "candidates": tuple(RouteCandidate(**c) for c in value["candidates"]),
                      "tools": tuple(value.get("tools", ()))})


@dataclass(frozen=True)
class InputEnvelope:
    instructions: str
    input_json: str
    assets_json: str = "[]"
    version: int = 1
    reply_options_json: str = "{}"

    @property
    def sha256(self):
        value = asdict(self)
        if self.reply_options_json == "{}":
            value.pop("reply_options_json")
        return digest(value)


@dataclass(frozen=True)
class ExecutionRequest:
    context: TaskContext
    route: RouteSnapshot
    envelope: InputEnvelope
    attempt_id: str
    candidate_index: int = 0

    @property
    def candidate(self):
        return self.route.candidates[self.candidate_index]


@dataclass(frozen=True)
class ExecutionResult:
    task_id: str
    run_id: str
    attempt_id: str
    status: str
    text: str = ""
    structured_json: str = "null"
    assets_json: str = "[]"
    target_model: str = ""
    actual_model: str = ""
    engine: str = ""
    profile_id: str = ""
    channel: str = ""
    usage_json: str = "null"
    usage_source: str = "unknown"
    error_code: str = ""
    error: str = ""
    operations: tuple[str, ...] = ()
    retry_after_seconds: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "operations", tuple(self.operations))


_context: ContextVar[TaskContext | None] = ContextVar("execution_task_context", default=None)
_handle = ContextVar("execution_run_handle", default=None)


def current_execution_context():
    return _context.get()


def current_run_handle():
    return _handle.get()


@contextmanager
def run_handle_scope(handle):
    token = _handle.set(handle)
    try:
        yield handle
    finally:
        _handle.reset(token)


@contextmanager
def execution_context(context: TaskContext):
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)
