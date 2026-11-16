"""Business admission helpers and the old string-returning LLM compatibility seam."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import copy
from dataclasses import asdict, replace
import json
import os
from pathlib import Path

from app.services.execution_contracts import ExecutionError, InputEnvelope, canonical

_admissions = ContextVar("engine_business_admissions", default=None)


@contextmanager
def model_admission_scope():
    token = _admissions.set({})
    try:
        yield
    finally:
        try:
            finish_unused_admissions()
        finally:
            _admissions.reset(token)


@contextmanager
def business_model_scope(business_key):
    from app.services.execution_authority import current_admission_source, admission_source
    from app.services.execution_contracts import digest
    source = current_admission_source()
    if source is not None:
        source = replace(source, request_id=source.request_id + ":" + digest(business_key)[:24])
    with model_admission_scope(), admission_source(source):
        yield


def finish_unused_admissions():
    admissions = _admissions.get() or {}
    if not admissions:
        return
    from app.services.execution_service import get_execution_service
    store = get_execution_service().store
    store.finish_unused_admissions(list(admissions.values()))


def routing_enabled():
    from app.services.execution_store import execution_db_path
    if not Path(execution_db_path()).exists():
        return False
    from app.services.execution_service import get_execution_service
    return get_execution_service().config.read()[1]["enabled"]


def begin_model_task(manager, component, task_type, *, output_contract=None):
    if not routing_enabled():
        return None
    from app.services.execution_service import get_execution_service
    from app.services.execution_authority import make_context
    service = get_execution_service()
    _, config = service.config.read()
    route_config = config["routes"].get(component + "." + task_type, {})
    mapping = manager._get_mapping(component, task_type)
    if not mapping or route_config.get("mode", mapping.get("mode", "legacy")) == "legacy":
        return None
    admissions = _admissions.get()
    key = component, task_type
    if admissions is not None and key in admissions:
        return admissions[key]
    context = make_context(component, task_type, timeout=int(route_config.get("timeout", 600)))
    route = service.config.resolve(chat_id=context.source_chat_id, component=component, task_type=task_type,
        models=copy.deepcopy(manager.config["models"]), mapping=copy.deepcopy(mapping), output_contract=output_contract)
    run_id, _ = service.admit(context, route)
    if admissions is not None:
        admissions[key] = run_id
    return run_id


def call_model_result(manager, component, task_type, messages, **kwargs):
    run_id = kwargs.pop("_mabobot_run_id", None) or begin_model_task(manager, component, task_type,
        output_contract=kwargs.pop("_mabobot_output_contract", None))
    if run_id is None:
        return None
    from app.services.execution_service import get_execution_service
    service = get_execution_service()
    instructions = kwargs.pop("_mabobot_instructions", None)
    data = kwargs.pop("_mabobot_input", None)
    if instructions is None:
        instructions = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") in {"system", "developer"})
    if data is None:
        input_messages = [m for m in messages if m.get("role") not in {"system", "developer"}]
        if len(input_messages) == 1 and input_messages[0].get("role") == "user" and isinstance(input_messages[0].get("content"), str):
            data = input_messages[0]["content"]
        else:
            # Preserve message roles as input data without upgrading them into
            # trusted instructions or throwing away a caller's conversation.
            data = canonical(input_messages)
    if not instructions:
        raise ExecutionError("instructions_missing", "Direct generation requires explicit task instructions")
    result = service.execute(run_id, InputEnvelope(instructions, canonical(data)))
    capture = kwargs.get("_mabobot_result_capture")
    if isinstance(capture, list):
        capture.append(asdict(result))
    if service.store.get(run_id)["cancel_requested"]:
        raise ExecutionError("cancelled", "Generation completed after cancellation; delivery is blocked")
    return result
