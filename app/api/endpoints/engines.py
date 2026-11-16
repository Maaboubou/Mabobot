"""Versioned, offline-capable engine management. Native Codex APIs stay Codex-only."""
from __future__ import annotations

import copy
from dataclasses import asdict
import json
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.services.execution_contracts import ExecutionError, digest
from app.services.execution_service import get_execution_service

router = APIRouter()


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    config: dict


class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    engine: Literal["codex", "opencode"]
    model: str = Field(min_length=1, max_length=160)
    reasoning_effort: str = Field(default="high", max_length=30)
    account_key: str = Field(min_length=1, max_length=100)
    native_profile_id: str = Field(default="", max_length=80)


class RoutePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: str = ""
    component: str
    task_type: str


class ConvertCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_digest: str
    engine_policy: Literal["follow", "codex", "opencode"] = "follow"


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=160)
    reasoning_effort: str = Field(default="high", max_length=30)
    mode: Literal["direct", "tools"]


class OperationResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["completed", "failed"]
    evidence: str = Field(min_length=8, max_length=2000)


def _http_error(exc):
    return HTTPException(409 if getattr(exc, "code", "") in {"configuration_conflict", "profile_in_use"} else 400, str(exc))


@router.get("")
def status():
    service = get_execution_service()
    revision, config = service.config.read()
    from app.services.opencode_profile_service import opencode_profiles
    import shutil
    import os
    from app.services.execution_contracts import RouteCandidate, canonical
    capabilities = {}
    for key, p in config["profiles"].items():
        candidate = RouteCandidate("probe", p["engine"], key, str(p["generation"]), p["model"],
            p["account_key"], p["auth_ref"], canonical({"reasoning_effort": p.get("reasoning_effort", "high")}), p.get("channel", ""))
        capabilities[key] = {name: service.config.capability(candidate, name)["status"]
                             for name in ("strict_generation", "tool_calling")}
    from app.services.engine_deployment import deployment_status
    deployment_info = deployment_status()
    return {"version": 1, "revision": revision, "config": config, **service.status(),
        "deployment": deployment_info,
        "capabilities": capabilities,
        "installed": {engine: value["installed"] for engine, value in deployment_info["engines"].items()},
        "authentication": {key: opencode_profiles.status(key) for key, p in config["profiles"].items() if p["engine"] == "opencode"}}


@router.get("/deployment")
def deployment():
    from app.services.engine_deployment import deployment_status
    return deployment_status(check_versions=True)


@router.get("/profiles/{profile_id}/skills")
def profile_skills(profile_id: str):
    profile = get_execution_service().config.read()[1]["profiles"].get(profile_id)
    if not profile:
        raise HTTPException(404, "Profile 不存在")
    if profile["engine"] == "opencode":
        return {"engine": "opencode", "supported": False, "skills": [],
                "reason": "受管 OpenCode 禁用原生 Skills 自动加载；任务规则与工具由宿主提供"}
    from app.services.codex_skill_service import get_codex_skill_service, CodexSkillError
    try:
        return {"engine": "codex", "supported": True,
                **get_codex_skill_service().list_skills(profile.get("native_profile_id") or profile_id)}
    except CodexSkillError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.put("/settings")
def save_settings(request: SettingsUpdate):
    service = get_execution_service()
    _, previous = service.config.read()
    config = copy.deepcopy(request.config)
    try:
        for key, profile in config.get("profiles", {}).items():
            old = previous["profiles"].get(key)
            if old is None:
                raise ValueError("请通过 Profile 登记接口创建账号")
            if profile.get("engine") != old.get("engine") or profile.get("runtime_options", {}) != old.get("runtime_options", {}):
                raise ValueError("引擎及运行文件不能通过浏览器设置修改；更新使用宿主部署工具")
            if profile.get("connection") != old.get("connection"):
                raise ValueError("请通过 Profile 编辑入口修改连接")
            if old and {k: v for k, v in profile.items() if k not in {"retiring", "revoked", "generation"}} != {
                    k: v for k, v in old.items() if k not in {"retiring", "revoked", "generation"}}:
                profile["generation"] = uuid.uuid4().hex
                if profile.get("auth_ref") != old.get("auth_ref") or profile.get("native_profile_id") != old.get("native_profile_id"):
                    with service.store.connect() as db:
                        pins = db.execute("SELECT COUNT(*) FROM engine_config_pins WHERE profile_id=? AND released=0", (key,)).fetchone()[0]
                    if pins:
                        raise ExecutionError("profile_in_use", "账号绑定仍被已接受任务引用；请先撤销并核对任务")
        removed = set(previous["profiles"]) - set(config.get("profiles", {}))
        if removed:
            raise ValueError("请使用 Profile 停用接口；不能直接删除配置版本")
        revision = service.config.save(config, expected_revision=request.expected_revision)
        for key, profile in config["profiles"].items():
            if profile.get("revoked") and not previous["profiles"].get(key, {}).get("revoked"):
                service.cancel_profile(key)
                service.registry.retire(profile["engine"], key, revoke=True)
        return {"revision": revision, "config": config}
    except (ValueError, ExecutionError) as exc:
        raise _http_error(exc) from exc


@router.post("/profiles")
def create_profile(request: ProfileCreate):
    service = get_execution_service()
    _, config = service.config.read()
    if request.id in config["profiles"]:
        raise HTTPException(409, "Profile ID 已存在")
    if request.engine == "codex":
        from app.services.codex_profile_service import get_codex_profile_service
        native = get_codex_profile_service().get_profile(request.native_profile_id or request.id)
        if not native or not native.get("available"):
            raise HTTPException(400, "请选择已配置的 Codex Profile")
    binding = uuid.uuid4().hex
    config["profiles"][request.id] = {"engine": request.engine, "generation": uuid.uuid4().hex,
        "auth_binding_generation": binding, "account_key": request.account_key,
        "auth_ref": request.engine + ":" + request.id + ":" + binding,
        "model": request.model, "reasoning_effort": request.reasoning_effort,
        "native_profile_id": request.native_profile_id or request.id,
        "runtime_options": {key: native.get(key) for key in ("fast_mode", "context_window", "supports_web_search", "supports_vision")} if request.engine == "codex" else {},
        "channel": "chatgpt-headless" if request.engine == "opencode" else "codex",
        "retiring": False, "revoked": False}
    try:
        revision = service.config.save(config, expected_revision=request.expected_revision)
    except (ValueError, ExecutionError) as exc:
        raise _http_error(exc) from exc
    return {"revision": revision, "profile": config["profiles"][request.id]}


@router.get("/profiles/{profile_id}/references")
def profile_references(profile_id: str):
    service = get_execution_service()
    _, config = service.config.read()
    with service.store.connect() as db:
        pins = [dict(row) for row in db.execute("SELECT run_id,engine,generation FROM engine_config_pins WHERE profile_id=? AND released=0", (profile_id,))]
    return {"pins": pins, "defaults": [engine for engine, value in config["defaults"].items() if value == profile_id],
        "chats": [key for key, value in config["chats"].items() if profile_id in value.get("profiles", {}).values() or value.get("image_profile_id") == profile_id],
        "routes": [key for key, value in config["routes"].items() if profile_id in value.get("profiles", {}).values()],
        "physical_deletion": "保留原生账号与状态；引用释放后由受控清理处理"}


@router.delete("/profiles/{profile_id}")
def retire_profile(profile_id: str, expected_revision: int):
    service = get_execution_service()
    _, config = service.config.read()
    if profile_id not in config["profiles"]:
        raise HTTPException(404, "Profile 不存在")
    config["profiles"][profile_id]["retiring"] = True
    try:
        revision = service.config.save(config, expected_revision=expected_revision)
    except ExecutionError as exc:
        raise _http_error(exc) from exc
    service.registry.retire(config["profiles"][profile_id]["engine"], profile_id)
    return {"revision": revision, "retiring": True, **profile_references(profile_id)}


@router.post("/profiles/{profile_id}/login")
def login(profile_id: str):
    from app.services.opencode_profile_service import opencode_profiles
    try:
        return opencode_profiles.login(profile_id)
    except (ExecutionError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.get("/profiles/{profile_id}/login")
def login_status(profile_id: str):
    from app.services.opencode_profile_service import opencode_profiles
    return opencode_profiles.status(profile_id)


@router.delete("/profiles/{profile_id}/login")
def cancel_login(profile_id: str):
    from app.services.opencode_profile_service import opencode_profiles
    return opencode_profiles.cancel(profile_id)


@router.post("/profiles/{profile_id}/test")
def test_connection(profile_id: str, request: ProbeRequest):
    service = get_execution_service()
    if profile_id not in service.config.read()[1]["profiles"]:
        raise HTTPException(404, "Profile 不存在")
    from app.services.engine_probe import probe_profile
    try:
        return probe_profile(profile_id, **request.model_dump())
    except (ExecutionError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.get("/cards")
def cards():
    from app.services.llm_manager import get_llm_manager
    manager = get_llm_manager()
    return {"cards": [{"id": key, "model": card["model"], "kind": card.get("kind", "legacy"),
                       "provider": card.get("provider") or card.get("custom_llm_provider"),
                       "digest": digest(card)} for key, card in manager.config["models"].items()],
            "mappings": copy.deepcopy(manager.config["plugin_mappings"])}


@router.get("/chats")
def chats():
    from app.models.base import SessionLocal
    from app.models.user_permission import WeChatUser
    with SessionLocal() as db:
        return [{"id": "wechat:" + str(row.id), "name": row.chat_name} for row in db.query(WeChatUser).order_by(WeChatUser.chat_name)]


@router.get("/cards/{card_id}/conversion")
def preview_conversion(card_id: str):
    from app.services.llm_manager import get_llm_manager
    manager = get_llm_manager()
    if card_id not in manager.config["models"]:
        raise HTTPException(404, "模型卡不存在")
    report = get_execution_service().config.conversion_preview(card_id, manager.config["models"], manager.config["plugin_mappings"])
    report["expected_digest"] = digest(manager.config["models"][card_id])
    return report


@router.post("/cards/{card_id}/test")
def test_api_card(card_id: str):
    from app.services.engine_probe import probe_api_card
    try:
        return probe_api_card(card_id)
    except KeyError as exc:
        raise HTTPException(404, "模型卡不存在") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/cards/{card_id}/conversion")
def convert_card(card_id: str, request: ConvertCard):
    from app.services.llm_manager import get_llm_manager
    manager = get_llm_manager()
    with manager._config_lock:
        card = manager.config["models"].get(card_id)
        if not card or digest(card) != request.expected_digest:
            raise HTTPException(409, "模型配置已变化，请重新预览")
        if not manager._is_local_codex_model(card) and card.get("kind") != "subscription":
            raise HTTPException(400, "普通 API 卡不能按名称自动转换为订阅卡")
        if card.get("codex_profile_id"):
            from app.services.codex_profile_service import get_codex_profile_service
            profile = get_codex_profile_service().get_profile(card["codex_profile_id"])
            if not profile or profile.get("auth_type") != "chatgpt":
                raise HTTPException(400, "此卡的原连接不是 ChatGPT 订阅")
        # Back up only this existing config file within its protected directory;
        # no OAuth state or new credentials export is created.
        backup = manager.models_path.with_name("llm_models.before-subscription-" + request.expected_digest[:12] + ".json")
        if not backup.exists():
            backup.write_text(json.dumps(manager.config["models"], ensure_ascii=False, indent=2), encoding="utf-8")
            backup.chmod(0o600)
        card.update(kind="subscription", engine_policy=request.engine_policy)
        if "codex_reasoning_effort" in card and "reasoning_effort" not in card:
            card["reasoning_effort"] = card["codex_reasoning_effort"]
        manager.save_config()
    return {"converted": True, "id": card_id, "references_preserved": True}


@router.post("/routes/preview")
def preview_route(request: RoutePreview):
    from app.services.llm_manager import get_llm_manager
    manager, service = get_llm_manager(), get_execution_service()
    mapping = manager._get_mapping(request.component, request.task_type)
    if not mapping:
        raise HTTPException(404, "任务未配置模型路由")
    try:
        route = service.config.resolve(**request.model_dump(), models=manager.config["models"], mapping=mapping)
        return {"snapshot": asdict(route), "capabilities": [service.config.capability(c,
            "strict_generation" if route.mode == "direct" else "tool_calling") for c in route.candidates]}
    except (ValueError, ExecutionError) as exc:
        raise _http_error(exc) from exc


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    service = get_execution_service()
    try:
        service.store.get(run_id)
    except KeyError as exc:
        raise HTTPException(404, "任务不存在") from exc
    service.cancel(run_id)
    return {"cancel_requested": True, "status": service.store.get(run_id)["status"]}


@router.get("/runs/{run_id}")
def run_details(run_id: str):
    service = get_execution_service()
    try:
        row = service.store.get(run_id)
    except KeyError as exc:
        raise HTTPException(404, "任务不存在") from exc
    # Diagnostics expose structure/hashes, not input prose or credentials.
    with service.store.connect() as db:
        attempts = [dict(a) for a in db.execute("SELECT attempt_id,candidate_index,status,dispatched,result_json FROM engine_attempts WHERE run_id=?", (run_id,))]
        operations = [dict(o) for o in db.execute("SELECT operation_id,kind,business_key,status,updated FROM engine_operations WHERE run_id=?", (run_id,))]
    for attempt in attempts:
        result = json.loads(attempt.pop("result_json") or "null")
        if result:
            attempt["result"] = {k: result[k] for k in ("status", "target_model", "actual_model", "engine", "profile_id", "channel", "usage_json", "usage_source", "error_code")}
    return {"run_id": run_id, "status": row["status"], "cancel_requested": row["cancel_requested"],
            "delivery_status": row["delivery_status"], "route": json.loads(row["route_json"]),
            "input_hash": row["input_hash"], "input_length": len(row["input_json"] or ""), "attempts": attempts, "operations": operations}


@router.get("/operations/{operation_id}")
def operation_details(operation_id: str):
    service = get_execution_service()
    with service.store.connect() as db:
        row = db.execute("SELECT * FROM engine_operations WHERE operation_id=?", (operation_id,)).fetchone()
    if not row:
        raise HTTPException(404, "操作不存在")
    result = json.loads(row["result_json"] or "{}")
    return {key: row[key] for key in ("operation_id", "run_id", "kind", "business_key", "status", "updated")} | {
        "result": {key: result[key] for key in ("assets", "paths", "missing", "reconciliation") if key in result}}


@router.post("/operations/{operation_id}/reconcile")
def reconcile_operation(operation_id: str, request: OperationResolution):
    service = get_execution_service()
    with service.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM engine_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if not row:
            raise HTTPException(404, "操作不存在")
        if row["kind"] != "image_job" or row["status"] != "unknown" or service.handle(row["run_id"]):
            raise HTTPException(409, "仅可核对已经停止且结果未知的图片操作")
        context, _ = service.store.contracts(row["run_id"])
        if service.scheduler.is_running(row["run_id"]) or not service.authorize(context):
            raise HTTPException(409, "任务仍在执行或授权已经撤回")
        result = json.loads(row["result_json"] or "{}")
        if request.outcome == "completed":
            from pathlib import Path
            from app.services.image_generation_service import inspect_image
            paths = result.get("paths", [])
            if not paths or result.get("missing", 1) != 0:
                raise HTTPException(409, "尚无完整的已登记产物，不能标记完成")
            for name in paths:
                path = Path(name)
                if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
                    raise HTTPException(409, "已登记图片校验失败")
                inspect_image(path.read_bytes())
        elif result.get("paths"):
            raise HTTPException(409, "已存在部分产物，不能将整个操作标记为失败")
        import time
        result.update(status=request.outcome, reconciliation={"evidence": request.evidence, "at": time.time(), "source": "administrator"})
        db.execute("UPDATE engine_operations SET status=?,result_json=?,updated=? WHERE operation_id=? AND status='unknown'",
                   (request.outcome, json.dumps(result, ensure_ascii=False), time.time(), operation_id))
    return operation_details(operation_id)
