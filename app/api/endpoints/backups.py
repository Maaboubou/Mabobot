"""System backup and machine-migration endpoints."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.services.backup_service import BackupError, BackupOptions, get_backup_service
from app.services.runtime_operations import get_runtime_operation_service


router = APIRouter()


class BackupCreateRequest(BaseModel):
    profile: str = "state"
    include_models: bool = False
    include_diagnostics: bool = False
    include_machine_bound: bool = False
    include_generated: bool = True
    include_codex_profiles: bool = True


class RestorePrepareRequest(BaseModel):
    confirmation: str


class BackupDeleteRequest(BaseModel):
    confirmation: str


class RestoreCancelRequest(BaseModel):
    archive_name: str
    confirmation: str


@router.post("/cancel-restore")
def cancel_restore(request: RestoreCancelRequest) -> Dict[str, Any]:
    try:
        return get_backup_service().cancel_pending_restore(
            request.archive_name, confirmation=request.confirmation,
        )
    except (BackupError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/")
def overview() -> Dict[str, Any]:
    return get_backup_service().overview()


@router.post("/")
def create_backup(request: BackupCreateRequest) -> Dict[str, Any]:
    service = get_backup_service()
    options = BackupOptions(
        profile=request.profile,
        include_models=request.include_models,
        include_diagnostics=request.include_diagnostics,
        include_machine_bound=request.include_machine_bound,
        include_generated=request.include_generated,
        include_codex_profiles=request.include_codex_profiles,
    ).normalized()
    operation = get_runtime_operation_service().submit(
        owner="system:backup",
        kind="backup_create",
        title="创建状态备份" if options.profile == "state" else "创建完整迁移包",
        details={"profile": options.profile},
        target=lambda context: service.create_backup(options, context),
    )
    return {"operation": operation}


@router.post("/{archive_name}/validate")
def validate_backup(archive_name: str) -> Dict[str, Any]:
    service = get_backup_service()
    try:
        archive = service.archive_path(archive_name)
    except BackupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    operation = get_runtime_operation_service().submit(
        owner="system:backup",
        kind="backup_validate",
        title=f"校验备份 {archive.name}",
        details={"archive_name": archive.name},
        target=lambda context: service.validate_archive(archive, verify_files=True, operation=context),
    )
    return {"operation": operation}


@router.post("/{archive_name}/prepare-restore")
def prepare_restore(archive_name: str, request: RestorePrepareRequest) -> Dict[str, Any]:
    service = get_backup_service()
    try:
        archive = service.archive_path(archive_name)
    except BackupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    operation = get_runtime_operation_service().submit(
        owner="system:backup",
        kind="backup_prepare_restore",
        title=f"准备恢复 {archive.name}",
        details={"archive_name": archive.name},
        target=lambda context: service.prepare_restore(
            archive.name,
            confirmation=request.confirmation,
        ),
    )
    return {"operation": operation}


@router.delete("/{archive_name}")
def delete_backup(archive_name: str, request: BackupDeleteRequest) -> Dict[str, Any]:
    service = get_backup_service()
    try:
        service.archive_path(archive_name)
    except BackupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if request.confirmation.strip() != "删除备份":
        raise HTTPException(status_code=422, detail="请输入“删除备份”确认操作")
    try:
        return service.delete_archive(archive_name, confirmation=request.confirmation)
    except BackupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/import")
async def import_backup(
    request: Request,
    filename: str = Query(..., min_length=1, max_length=240),
) -> Dict[str, Any]:
    service = get_backup_service()
    try:
        destination = service.import_path(filename)
    except BackupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if destination.exists():
        raise HTTPException(status_code=409, detail="同名迁移包已经存在，请先重命名后再导入")
    max_bytes = max(1, int(os.getenv("SYSTEM_BACKUP_MAX_IMPORT_GB", "50"))) * 1024**3
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="迁移包超过允许的导入大小")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Content-Length 无效") from exc
    temporary = destination.with_suffix(destination.suffix + ".upload")
    written = 0
    try:
        with temporary.open("wb") as handle:
            async for chunk in request.stream():
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413, detail="迁移包超过允许的导入大小")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        validation = service.validate_archive(temporary, verify_files=False)
        if not validation["valid"]:
            raise BackupError("迁移包结构校验失败")
        os.replace(temporary, destination)
        return {
            "imported": True,
            "name": destination.name,
            "bytes": written,
            "security_warning": validation.get("security_warning"),
        }
    except HTTPException:
        raise
    except (OSError, BackupError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


@router.get("/{archive_name}/download")
def download_backup(archive_name: str) -> FileResponse:
    service = get_backup_service()
    try:
        archive = service.archive_path(archive_name)
    except BackupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=archive.name,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(archive.name)}"},
    )
