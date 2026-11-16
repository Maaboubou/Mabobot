"""Unified environment-tool status and maintenance endpoints."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query

from app.services.system_tool_service import SystemToolError, get_system_tool_service


router = APIRouter(prefix="/api/system/tools", tags=["System Tools"])


@router.get("")
@router.get("/")
async def get_tool_overview(
    refresh: bool = Query(default=False),
) -> Dict[str, Any]:
    overview = await asyncio.to_thread(
        get_system_tool_service().overview,
        force_environment=refresh,
    )
    return {"status": "success", "data": overview}


@router.post("/check")
async def check_all_tools() -> Dict[str, Any]:
    service = get_system_tool_service()

    async def check_one(tool_id: str) -> tuple[str, str | None]:
        try:
            await asyncio.to_thread(service.check_tool, tool_id)
            return tool_id, None
        except Exception as exc:
            return tool_id, str(exc) or type(exc).__name__

    from app.services.engine_deployment import enabled_engines
    checked = await asyncio.gather(*(check_one(name) for name in
        (["codex"] if "codex" in enabled_engines() else []) + ["litellm", "yt-dlp"]))
    overview = await asyncio.to_thread(service.overview)
    overview["check_errors"] = {
        tool_id: error for tool_id, error in checked if error
    }
    return {"status": "success", "data": overview}


@router.post("/{tool_id}/check")
async def check_tool(tool_id: str) -> Dict[str, Any]:
    try:
        card = await asyncio.to_thread(get_system_tool_service().check_tool, tool_id)
        return {"status": "success", "data": card}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/{tool_id}/update")
async def start_tool_update(tool_id: str) -> Dict[str, Any]:
    try:
        operation = get_system_tool_service().start_update(tool_id)
        return {"status": "success", "data": {"operation": operation}}
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{tool_id}/rollback")
async def rollback_tool_update(tool_id: str) -> Dict[str, Any]:
    try:
        operation = get_system_tool_service().start_rollback(tool_id)
        return {"status": "success", "data": {"operation": operation}}
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{tool_id}/repair")
async def repair_tool(tool_id: str) -> Dict[str, Any]:
    try:
        operation = get_system_tool_service().start_repair(tool_id)
        return {"status": "success", "data": {"operation": operation}}
    except SystemToolError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
