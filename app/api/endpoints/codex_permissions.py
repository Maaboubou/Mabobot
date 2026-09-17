"""Permissions use the existing console API and transaction conventions."""

from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.models.base import get_db
from app.schemas.codex_permission import PermissionDefaultsPatch
from app.services.codex_permission_runtime import runtime_support
from app.services.codex_permission_service import (
    CodexPermissionService, FIELD_SCHEMA, HARD_BOUNDARIES, PermissionConflict, PermissionError,
)

router = APIRouter()


@router.get("/schema")
def permission_schema():
    return {"schema_version": 1, "fields": FIELD_SCHEMA, "runtime_support": runtime_support(),
            "hard_boundaries": HARD_BOUNDARIES, "apply_mode": "next_turn", "requires_thread_refresh": True}


@router.get("/defaults/{scope_type}")
def get_defaults(scope_type: Literal["group", "private"], db: Session = Depends(get_db)):
    try:
        return CodexPermissionService(db).describe_defaults(scope_type)
    except PermissionError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.patch("/defaults/{scope_type}")
def patch_defaults(scope_type: Literal["group", "private"], request: PermissionDefaultsPatch, db: Session = Depends(get_db)):
    try:
        return CodexPermissionService(db).update_defaults(scope_type, request)
    except PermissionConflict as exc:
        raise HTTPException(409, {"message": str(exc), "current_version": exc.current_version}) from exc
    except PermissionError as exc:
        raise HTTPException(422, str(exc)) from exc
