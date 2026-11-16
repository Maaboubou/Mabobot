from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request

from app.services.codex_job_manager import codex_job_manager

router = APIRouter(prefix="/api/codex/jobs", tags=["Codex Jobs"])


async def _read_chat_id(request: Request) -> str:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from exc
    chat_id = str(payload.get("chat_id") or "").strip() if isinstance(payload, dict) else ""
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id is required")
    return chat_id


@router.get("")
async def list_codex_jobs(recent_limit: int = Query(default=50, ge=0, le=200)) -> Dict[str, Any]:
    """Return active and recent Codex jobs with runtime and session state."""
    from app.services.engine_deployment import enabled_engines
    if "codex" not in enabled_engines():
        return {"status": "success", "data": {"disabled": True, "active": codex_job_manager.list_active(),
            "recent": codex_job_manager.list_recent(limit=recent_limit), "stats": codex_job_manager.stats(),
            "sessions": [], "session_stats": {}, "runtime": {"enabled": False, "running": False,
            "last_error": "此部署未启用 Codex"}, "upgrade": {"disabled": True}, "file_tools": {"disabled": True}}}
    from app.services.agent_runtime import get_agent_runtime

    active = codex_job_manager.list_active()
    runtime = get_agent_runtime()
    session_data = runtime.session_snapshot(active)
    from app.services.codex_upgrade_service import get_codex_upgrade_service
    from app.services.file_tools_runtime import get_file_tools_runtime

    return {
        "status": "success",
        "data": {
            "active": active,
            "recent": codex_job_manager.list_recent(limit=recent_limit),
            "stats": codex_job_manager.stats(),
            "sessions": session_data["sessions"],
            "session_stats": session_data["stats"],
            "runtime": runtime.status(),
            "upgrade": get_codex_upgrade_service().status(),
            "file_tools": await asyncio.to_thread(get_file_tools_runtime),
        },
    }


@router.get("/running")
async def list_running_codex_jobs() -> Dict[str, Any]:
    """Compatibility endpoint for active jobs only."""
    return {"status": "success", "data": codex_job_manager.list_active()}


@router.get("/runtime")
async def get_codex_runtime_status() -> Dict[str, Any]:
    """Return lifecycle, pool and protocol status for Codex."""
    from app.services.agent_runtime import get_agent_runtime

    return {
        "status": "success",
        "data": get_agent_runtime().status(),
    }


@router.post("/runtime/refresh")
async def refresh_codex_runtime() -> Dict[str, Any]:
    from app.services.agent_runtime import get_agent_runtime

    runtime = get_agent_runtime()
    activated = await asyncio.to_thread(runtime.refresh, force=True)
    return {
        "status": "success",
        "data": {"activated": activated, "runtime": runtime.status()},
    }


@router.post("/runtime/select")
async def select_codex_runtime(request: Request) -> Dict[str, Any]:
    """Validate and activate a WSL-native Codex executable path."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from exc
    path = str(payload.get("path") or "").strip() if isinstance(payload, dict) else ""

    from app.services.agent_runtime import get_agent_runtime
    from app.services.codex_proxy.client import _detect_runtime_file_commands
    from app.services.file_tools_runtime import (
        _validate_wsl_codex_path,
        get_file_tools_runtime,
        persist_wsl_codex_bin,
    )

    try:
        normalized = _validate_wsl_codex_path(path)
        candidate = await asyncio.to_thread(
            get_file_tools_runtime,
            use_wsl=True,
            codex_bin=normalized,
            force=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if candidate.get("status") != "ready" or not candidate.get("codex", {}).get("available"):
        raise HTTPException(status_code=422, detail="该路径不是可执行的 WSL Codex")

    runtime = get_agent_runtime()
    previous = runtime.probe.codex_bin
    selected = await asyncio.to_thread(runtime.switch_codex_bin, normalized)
    if not selected:
        detail = runtime.status().get("last_error") or "Codex 兼容性检查失败"
        raise HTTPException(status_code=409, detail=detail)
    try:
        await asyncio.to_thread(persist_wsl_codex_bin, normalized)
    except Exception as exc:
        await asyncio.to_thread(runtime.switch_codex_bin, previous)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _detect_runtime_file_commands.cache_clear()
    from app.services.codex_upgrade_service import get_codex_upgrade_service

    get_codex_upgrade_service().invalidate_installation_cache()
    snapshot = await asyncio.to_thread(get_file_tools_runtime, force=True)
    return {
        "status": "success",
        "data": {
            "selected": normalized,
            "runtime": runtime.status(),
            "file_tools": snapshot,
        },
    }


@router.get("/file-tools")
async def get_file_tools_status() -> Dict[str, Any]:
    from app.services.file_tools_runtime import get_file_tools_runtime

    return {"status": "success", "data": await asyncio.to_thread(get_file_tools_runtime)}


@router.post("/file-tools/refresh")
async def refresh_file_tools_status() -> Dict[str, Any]:
    from app.services.codex_proxy.client import _detect_runtime_file_commands
    from app.services.file_tools_runtime import refresh_file_tools_runtime

    _detect_runtime_file_commands.cache_clear()
    snapshot = await asyncio.to_thread(refresh_file_tools_runtime)
    return {"status": "success", "data": snapshot}


@router.get("/app-server", include_in_schema=False)
async def get_codex_app_server_status() -> Dict[str, Any]:
    return await get_codex_runtime_status()


@router.get("/upgrade")
async def get_codex_upgrade_status() -> Dict[str, Any]:
    from app.services.codex_upgrade_service import get_codex_upgrade_service

    return {"status": "success", "data": get_codex_upgrade_service().status()}


@router.post("/upgrade/check")
async def check_codex_update() -> Dict[str, Any]:
    from app.services.codex_upgrade_service import CodexUpgradeError, get_codex_upgrade_service

    try:
        result = await asyncio.to_thread(get_codex_upgrade_service().check_latest)
        return {"status": "success", "data": result}
    except CodexUpgradeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/upgrade/start")
async def start_codex_update() -> Dict[str, Any]:
    from app.services.codex_upgrade_service import CodexUpgradeError, get_codex_upgrade_service

    try:
        return {"status": "success", "data": get_codex_upgrade_service().start_update()}
    except CodexUpgradeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/upgrade/rollback")
async def rollback_codex_update() -> Dict[str, Any]:
    from app.services.codex_upgrade_service import CodexUpgradeError, get_codex_upgrade_service

    try:
        return {"status": "success", "data": get_codex_upgrade_service().start_rollback()}
    except CodexUpgradeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/events/{request_id}")
async def get_codex_job_events(
    request_id: str,
    limit: int = Query(default=100, ge=1, le=500),
) -> Dict[str, Any]:
    job = codex_job_manager.get_record(request_id)
    if not job:
        raise HTTPException(status_code=404, detail="Codex job not found")
    return {
        "status": "success",
        "data": {
            "job": job,
            "events": codex_job_manager.list_events(request_id, limit=limit),
        },
    }


@router.post("/sessions/reset")
async def reset_codex_session(request: Request) -> Dict[str, Any]:
    chat_id = await _read_chat_id(request)
    active = [job for job in codex_job_manager.list_active() if str(job.get("chat_id") or "") == chat_id]
    if active:
        raise HTTPException(status_code=409, detail="该会话仍有运行中的任务，请先中断任务")
    from app.services.agent_runtime import get_agent_runtime

    get_agent_runtime().invalidate_chat(chat_id, reason="manual_reset")
    return {"status": "success", "data": {"chat_id": chat_id, "reset": True}}


@router.post("/sessions/delete")
async def delete_codex_session(request: Request) -> Dict[str, Any]:
    normalized = await _read_chat_id(request)
    active = [
        job
        for job in codex_job_manager.list_active()
        if str(job.get("chat_id") or "") == normalized
    ]
    if active:
        raise HTTPException(status_code=409, detail="该会话仍有运行中的任务，请先中断任务")

    from app.services.agent_runtime import get_agent_runtime

    try:
        result = await asyncio.to_thread(get_agent_runtime().delete_chat, normalized)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"删除 Codex 会话失败：{exc}") from exc
    if not result.get("deleted"):
        raise HTTPException(status_code=404, detail="Codex 会话不存在或已删除")
    return {"status": "success", "data": result}


@router.post("/sessions/interrupt")
async def interrupt_codex_session(request: Request) -> Dict[str, Any]:
    chat_id = await _read_chat_id(request)
    active = [job for job in codex_job_manager.list_active() if str(job.get("chat_id") or "") == chat_id]
    if not active:
        raise HTTPException(status_code=404, detail="该会话当前没有运行中的任务")
    results = [await codex_job_manager.cancel(str(job.get("request_id") or "")) for job in active]
    return {"status": "success", "data": {"chat_id": chat_id, "results": results}}


@router.get("/{request_id}")
async def get_codex_job(request_id: str) -> Dict[str, Any]:
    job = codex_job_manager.get_record(request_id)
    if job:
        return {"status": "success", "data": job}
    raise HTTPException(status_code=404, detail="Codex job not found")


@router.post("/{request_id}/cancel")
async def cancel_codex_job(request_id: str) -> Dict[str, Any]:
    result = await codex_job_manager.cancel(request_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("message") or "Codex job not found")
    return {"status": "success", "data": result}
