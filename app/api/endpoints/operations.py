"""Unified managed-operation and plugin-runtime observability endpoints."""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.plugin_runtime import get_plugin_runtime_registry
from app.services.incident_service import get_incident_service
from app.services.runtime_operations import get_runtime_operation_service
from app.services.storage_service import get_storage_service


router = APIRouter()


class StoragePreviewRequest(BaseModel):
    retention_days: int = Field(7, ge=1, le=3650)


class StorageCleanupRequest(BaseModel):
    preview_id: str
    confirmation: str


class StorageTrashRequest(BaseModel):
    confirmation: str


_storage_submit_lock = threading.Lock()


def _storage_job(kind, title, target):
    operations = get_runtime_operation_service()
    with _storage_submit_lock:
        if any(row['status'] in {'queued', 'running', 'cancelling'}
               for row in operations.list(limit=100, owner='system:storage')):
            raise HTTPException(status_code=409, detail='已有存储任务正在执行，请等待完成')

        def run(context):
            result = target(context)
            operations.record_audit(category='storage', action=kind, target='managed_storage',
                                    status='failed' if result.get('error_count') else 'success',
                                    summary=title, after=result)
            return result

        return {'operation': operations.submit(owner='system:storage', kind=kind,
                                                title=title, target=run)}


@router.get("/")
def list_operations(
    limit: int = Query(100, ge=1, le=500),
    owner: Optional[str] = None,
) -> Dict[str, Any]:
    service = get_runtime_operation_service()
    return {
        "operations": service.list(limit=limit, owner=owner),
        "stats": service.stats(),
    }


@router.get("/runtime")
def runtime_overview() -> Dict[str, Any]:
    registry = get_plugin_runtime_registry()
    plugins = registry.snapshot()
    return {
        "plugin_runtime_api_version": 2,
        "plugins": plugins,
        "summary": {
            "total": len(plugins),
            "managed": len(plugins),
            "unhealthy": sum(
                (item.get("health") or {}).get("status") in {"unhealthy", "failed"}
                for item in plugins
            ),
        },
    }


@router.get("/incidents")
def list_incidents(
    limit: int = Query(50, ge=1, le=200),
    scan_lines: int = Query(10000, ge=100, le=50000),
    level: Optional[str] = None,
) -> Dict[str, Any]:
    incidents = get_incident_service().list(
        limit=limit,
        scan_lines=scan_lines,
        level=level,
    )
    return {
        "incidents": incidents,
        "summary": {
            "groups": len(incidents),
            "errors": sum(item.get("level") in {"ERROR", "CRITICAL"} for item in incidents),
            "occurrences": sum(int(item.get("count") or 0) for item in incidents),
        },
    }


@router.get("/audit")
def list_audit(
    limit: int = Query(100, ge=1, le=500),
    category: Optional[str] = None,
) -> Dict[str, Any]:
    records = get_runtime_operation_service().list_audit(
        limit=limit,
        category=category,
    )
    return {"records": records, "count": len(records)}


@router.get("/storage")
def storage_overview() -> Dict[str, Any]:
    return get_storage_service().overview()


@router.post("/storage/scan")
def scan_storage() -> Dict[str, Any]:
    return _storage_job('storage_scan', '扫描存储占用', get_storage_service().scan)


@router.post("/storage/cleanup-preview")
def storage_cleanup_preview(request: StoragePreviewRequest) -> Dict[str, Any]:
    return _storage_job('storage_preview', '预览缓存清理',
                        lambda context: get_storage_service().cleanup_preview(request.retention_days, context))


@router.post("/storage/cleanup")
def cleanup_storage(request: StorageCleanupRequest) -> Dict[str, Any]:
    return _storage_job('storage_cleanup', '缓存移入回收区',
                        lambda context: get_storage_service().cleanup_managed(
                            preview_id=request.preview_id, confirmation=request.confirmation))


@router.post("/storage/trash/{batch_id}/{action}")
def storage_trash_action(batch_id: str, action: str, request: StorageTrashRequest) -> Dict[str, Any]:
    if action not in {'restore', 'purge'}:
        raise HTTPException(status_code=422, detail='无效的回收区操作')
    return _storage_job('storage_' + action, '恢复缓存' if action == 'restore' else '永久删除回收文件',
                        lambda context: get_storage_service().trash_action(
                            batch_id, action=action, confirmation=request.confirmation))


@router.get("/{operation_id}")
def get_operation(operation_id: str) -> Dict[str, Any]:
    service = get_runtime_operation_service()
    operation = service.get(operation_id)
    if not operation:
        raise HTTPException(status_code=404, detail="操作不存在")
    return {
        "operation": operation,
        "events": service.events(operation_id),
    }


@router.post("/{operation_id}/cancel")
def cancel_operation(operation_id: str) -> Dict[str, Any]:
    result = get_runtime_operation_service().cancel(operation_id)
    if not result.get("success"):
        raise HTTPException(status_code=409, detail=result.get("message") or "无法取消操作")
    return result
