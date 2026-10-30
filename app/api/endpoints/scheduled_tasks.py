"""Operator console API for scheduled tasks (same trust boundary as other console APIs)."""
from typing import Any
import hashlib

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app.services.scheduled_tasks import ScheduleActor, ScheduleConflict, ScheduleDenied, get_scheduled_tasks
from app.services.scheduled_task_runtime import get_scheduled_runtime, scoped_file
from app.services.schedule_rules import preview, validate_schedule, describe

router = APIRouter()
ADMIN = ScheduleActor(name="Web 管理员", admin=True)


class Definition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str | None = Field(default=None, max_length=120)
    prompt: str | None = Field(default=None, max_length=20000)
    schedule: dict[str, Any] | None = None
    chat_id: int | None = None
    creator: str | None = Field(default=None, max_length=128)
    delivery_chat_id: int | None = None
    profile_id: str | None = Field(default=None, max_length=128)
    timeout_seconds: int | None = Field(default=None, ge=30, le=3600)
    misfire_grace_seconds: int | None = Field(default=None, ge=0, le=604800)
    resources: list[str] | None = Field(default=None, max_length=20)
    monitor: dict[str, Any] | None = None


class CreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: Definition
    request_id: str = Field(min_length=8, max_length=128)


class UpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: Definition
    expected_version: int = Field(ge=1)
    feedback: str = Field(default="Web 编辑", max_length=20000)


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    restore_version: int | None = None
    run_id: str | None = None
    until: str | None = None
    at: str | None = None
    request_id: str | None = Field(default=None, max_length=128)


def call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ScheduleConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ScheduleDenied as exc:
        raise HTTPException(403, str(exc)) from exc
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc


def check_resources(data, chat_id):
    if data.get("resources"):
        from app.services.codex_access_service import codex_access_service
        try:
            access = codex_access_service.for_id(chat_id, ensure=False)
        except RuntimeError as exc:
            raise ScheduleDenied(str(exc)) from exc
        data["resources"] = [str(scoped_file(path, access.workdir).relative_to(access.workdir.resolve())) for path in data["resources"]]
    return data


@router.get("")
def list_tasks(history: bool = False):
    return {"tasks": call(get_scheduled_tasks().list, ADMIN, history=history), "scheduler": get_scheduled_runtime().status(), "settings": get_scheduled_tasks().settings()}


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    expected_version: int = Field(ge=1)


@router.get("/settings")
def get_settings():
    return get_scheduled_tasks().settings()


@router.patch("/settings")
def update_settings(request: SettingsRequest):
    return call(get_scheduled_tasks().set_enabled, request.enabled, ADMIN, expected_version=request.expected_version)


@router.patch("/chats/{chat_id}/settings")
def update_chat_settings(chat_id: int, request: SettingsRequest):
    return call(get_scheduled_tasks().set_chat_enabled, chat_id, request.enabled, ADMIN, expected_version=request.expected_version)


@router.get("/options")
def options():
    from app.models.base import SessionLocal
    from app.models.user_permission import WeChatUser
    with SessionLocal() as db:
        return {"chats": [{"id": u.id, "name": u.chat_name, "enabled": bool(u.assistant_enabled and u.listening_enabled), "schedule_settings": get_scheduled_tasks().chat_settings(u.id)}
                          for u in db.query(WeChatUser).order_by(WeChatUser.chat_name).all()]}


@router.post("/preview")
def preview_schedule(schedule: dict[str, Any]):
    now = get_scheduled_tasks().clock()
    rule = call(validate_schedule, schedule, now)
    return {"schedule": rule, "description": describe(rule), "upcoming": preview(rule, now)}


@router.post("")
def create_task(request: CreateRequest):
    data = request.definition.model_dump(exclude_unset=True)
    call(check_resources, data, data.get("chat_id"))
    key = "web:" + hashlib.sha256(request.request_id.encode()).hexdigest()
    return call(get_scheduled_tasks().create, data, ADMIN, source_key=key, feedback="Web 创建")


@router.get("/{task_id}")
def get_task(task_id: str):
    return call(get_scheduled_tasks().get, task_id, ADMIN)


@router.patch("/{task_id}")
def update_task(task_id: str, request: UpdateRequest):
    service = get_scheduled_tasks()
    task = call(service.get, task_id, ADMIN)
    data = request.definition.model_dump(exclude_unset=True)
    call(check_resources, data, task["chat_id"])
    return call(service.update, task_id, data, ADMIN, expected_version=request.expected_version, feedback=request.feedback)


@router.post("/{task_id}/actions/{action}")
def task_action(task_id: str, action: str, request: ActionRequest):
    service = get_scheduled_tasks()
    task = call(service.get, task_id, ADMIN)
    if task["version"] != request.expected_version:
        raise HTTPException(409, "任务已被修改，请刷新后重试")
    if task["status"] == "deleted":
        raise HTTPException(422, "任务已删除")
    if action == "restore":
        return call(service.restore, task_id, request.restore_version, ADMIN, expected_version=request.expected_version)
    if action == "redeliver":
        run = call(service.run, request.run_id)
        if run["task_id"] != task_id or run["status"] in {"queued", "running"}:
            raise HTTPException(422, "执行记录不匹配或任务尚未结束")
        call(get_scheduled_runtime().deliver_run, run["id"], retry=True)
        return service.run(run["id"])
    return call(service.action, task_id, action, ADMIN, expected_version=request.expected_version, request_key=request.request_id, until=request.until, at=request.at)


@router.get("/{task_id}/runs/{run_id}/attachments/{index}")
def download_attachment(task_id: str, run_id: str, index: int):
    service = get_scheduled_tasks()
    call(service.get, task_id, ADMIN)
    run = call(service.run, run_id)
    attachments = (run["result"] or {}).get("attachments", [])
    if run["task_id"] != task_id or not 0 <= index < len(attachments):
        raise HTTPException(404, "附件不存在")
    from app.services.codex_access_service import codex_access_service
    try:
        access = codex_access_service.for_id(run["definition"]["chat_id"], ensure=False)
    except RuntimeError as exc:
        raise HTTPException(403, str(exc)) from exc
    path = call(scoped_file, attachments[index]["path"], access.artifact_root)
    return FileResponse(path, filename=attachments[index].get("name") or path.name)
