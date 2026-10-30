"""Codex Profile management without global runtime switching."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.services.codex_profile_service import CodexProfileError, get_codex_profile_service


router = APIRouter()


class CodexProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    auth_type: Literal["api_key", "chatgpt"] = "api_key"
    auth_source: Literal["device_code", "local_cache"] = "device_code"
    model: str = Field(default="", max_length=128)
    provider_name: str = Field(default="OpenAI Responses compatible", max_length=100)
    base_url: str = Field(default="", max_length=1000)
    api_key: str = Field(default="", max_length=4096)
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh", "max"] = "high"
    model_verbosity: Literal["inherit", "low", "medium", "high"] = "inherit"
    fast_mode: bool = False
    context_window: int = Field(default=128000, ge=4096, le=10_000_000)
    supports_vision: bool = False
    supports_web_search: bool = False
    make_default: bool = True
    setup_pending: bool = False


class CodexProfileUpdate(BaseModel):
    model: Optional[str] = Field(default=None, min_length=1, max_length=128)
    provider_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    base_url: Optional[str] = Field(default=None, min_length=1, max_length=1000)
    api_key: Optional[str] = Field(default=None, min_length=1, max_length=4096)
    reasoning_effort: Optional[Literal["minimal", "low", "medium", "high", "xhigh", "max"]] = None
    model_verbosity: Optional[Literal["inherit", "low", "medium", "high"]] = None
    fast_mode: Optional[bool] = None
    context_window: Optional[int] = Field(default=None, ge=4096, le=10_000_000)
    supports_vision: Optional[bool] = None
    supports_web_search: Optional[bool] = None


class DefaultProfileUpdate(BaseModel):
    profile_id: str = Field(min_length=1, max_length=48)


class OAuthStartRequest(BaseModel):
    force: bool = False


class CodexProfileFinalize(BaseModel):
    model: str = Field(min_length=1, max_length=128)
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh", "max"]
    model_verbosity: Literal["inherit", "low", "medium", "high"] = "inherit"
    fast_mode: bool = False
    make_default: bool = True


def _dump_set(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=True)
    return model.dict(exclude_unset=True)


def _error(exc: CodexProfileError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


@router.get("")
def list_profiles() -> Dict[str, Any]:
    try:
        result = get_codex_profile_service().list_profiles()
        return {
            **result,
            "profiles": [
                profile
                for profile in result.get("profiles") or []
                if profile.get("setup_status") != "pending"
            ],
        }
    except CodexProfileError as exc:
        raise _error(exc) from exc


@router.post("/{profile_id}/setup/finalize")
async def finalize_profile_setup(
    profile_id: str,
    request: CodexProfileFinalize,
) -> Dict[str, Any]:
    from app.services.codex_app_server import CodexAppServerError
    from app.services.codex_oauth_service import get_codex_oauth_service

    try:
        return await asyncio.to_thread(
            get_codex_oauth_service().finalize_setup,
            profile_id,
            _dump_set(request),
        )
    except (CodexProfileError, CodexAppServerError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{profile_id}/setup/cancel")
async def cancel_profile_setup(profile_id: str) -> Dict[str, Any]:
    from app.services.codex_app_server import CodexAppServerError
    from app.services.codex_oauth_service import get_codex_oauth_service

    service = get_codex_profile_service()
    profile = service.get_profile(profile_id)
    if not profile:
        return {"name": profile_id, "deleted": False}
    if profile.get("setup_status") != "pending":
        raise HTTPException(status_code=409, detail="只能取消尚未完成的 Profile 创建向导")
    oauth_service = get_codex_oauth_service()
    try:
        await asyncio.to_thread(oauth_service.cancel, profile_id)
        result = await asyncio.to_thread(service.delete_profile, profile_id)
        oauth_service.forget(profile_id)
        return result
    except (CodexProfileError, CodexAppServerError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{profile_id}/auth/sync")
async def sync_profile_local_auth(profile_id: str) -> Dict[str, Any]:
    """Force-copy and validate the current file-backed local Codex login."""
    from app.services.codex_app_server import CodexAppServerError
    from app.services.codex_oauth_service import get_codex_oauth_service

    try:
        return await asyncio.to_thread(
            get_codex_oauth_service().import_local_auth,
            profile_id,
        )
    except (CodexProfileError, CodexAppServerError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@router.post("")
async def create_profile(request: CodexProfileCreate) -> Dict[str, Any]:
    from app.services.agent_runtime import get_agent_runtime
    from app.services.codex_app_server import CodexAppServerError
    from app.services.codex_oauth_service import get_codex_oauth_service

    runtime = get_agent_runtime()
    codex_bin = str(runtime.probe.codex_bin or "").strip()
    if not codex_bin:
        raise HTTPException(status_code=409, detail="请先完成 Codex 可执行文件配置")
    service = get_codex_profile_service()
    try:
        profile = service.create_profile(
            _dump_set(request),
            codex_bin=codex_bin,
        )
        created_name = str(profile.get("name") or request.name).strip()
        oauth_status = None
        if request.auth_type == "chatgpt" and request.auth_source == "local_cache":
            try:
                oauth_status = await asyncio.to_thread(
                    get_codex_oauth_service().import_local_auth,
                    created_name,
                )
            except Exception:
                # Creation and import form one operation. Do not retain a
                # half-configured Profile when the copied login is invalid.
                try:
                    await asyncio.to_thread(service.delete_profile, created_name)
                finally:
                    get_codex_oauth_service().forget(created_name)
                raise
            profile = service.get_profile(created_name) or profile
        return {
            "profile": profile,
            "requires_login": request.auth_type == "chatgpt" and oauth_status is None,
            "oauth_status": oauth_status,
        }
    except (CodexProfileError, CodexAppServerError) as exc:
        raise _error(exc) from exc


@router.patch("/{profile_id}")
def update_profile(profile_id: str, request: CodexProfileUpdate) -> Dict[str, Any]:
    try:
        return get_codex_profile_service().update_profile(profile_id, _dump_set(request))
    except CodexProfileError as exc:
        raise _error(exc) from exc


@router.delete("/{profile_id}")
async def delete_profile(profile_id: str) -> Dict[str, Any]:
    from app.services.codex_app_server import CodexAppServerError
    from app.services.codex_oauth_service import get_codex_oauth_service

    service = get_codex_profile_service()
    profile = service.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Codex Profile 不存在")
    oauth_service = get_codex_oauth_service()
    try:
        if profile.get("auth_type") == "chatgpt":
            await asyncio.to_thread(oauth_service.cancel, profile_id)
        result = await asyncio.to_thread(service.delete_profile, profile_id)
        oauth_service.forget(profile_id)
        return result
    except (CodexProfileError, CodexAppServerError) as exc:
        raise _error(exc) from exc


@router.put("/default/selection")
def select_default_profile(request: DefaultProfileUpdate) -> Dict[str, Any]:
    try:
        service = get_codex_profile_service()
        service.set_default_profile(request.profile_id)
        return {"default_profile_id": service.default_profile_id()}
    except CodexProfileError as exc:
        raise _error(exc) from exc


@router.post("/{profile_id}/oauth/start")
async def start_profile_oauth(
    profile_id: str,
    request: OAuthStartRequest,
) -> Dict[str, Any]:
    from app.services.codex_app_server import CodexAppServerError
    from app.services.codex_oauth_service import get_codex_oauth_service

    try:
        return await asyncio.to_thread(
            get_codex_oauth_service().start_login,
            profile_id,
            force=request.force,
        )
    except (CodexProfileError, CodexAppServerError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{profile_id}/oauth")
async def get_profile_oauth(profile_id: str) -> Dict[str, Any]:
    from app.services.codex_oauth_service import get_codex_oauth_service

    try:
        return await asyncio.to_thread(get_codex_oauth_service().status, profile_id)
    except CodexProfileError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{profile_id}/oauth/qr")
def get_profile_oauth_qr(
    profile_id: str,
    login_id: str = Query(..., min_length=1, max_length=200),
) -> Response:
    from app.services.codex_oauth_service import get_codex_oauth_service

    try:
        target = get_codex_oauth_service().verification_url(profile_id, login_id)
        import cv2

        encoder = cv2.QRCodeEncoder_create()
        matrix = encoder.encode(target)
        matrix = cv2.copyMakeBorder(
            matrix,
            4,
            4,
            4,
            4,
            cv2.BORDER_CONSTANT,
            value=255,
        )
        scale = max(4, 288 // max(1, int(matrix.shape[0])))
        matrix = cv2.resize(
            matrix,
            (int(matrix.shape[1]) * scale, int(matrix.shape[0]) * scale),
            interpolation=cv2.INTER_NEAREST,
        )
        success, encoded = cv2.imencode(".png", matrix)
        if not success:
            raise RuntimeError("二维码编码失败")
    except CodexProfileError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"生成授权二维码失败：{exc}") from exc
    return Response(
        content=encoded.tobytes(),
        media_type="image/png",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.post("/{profile_id}/oauth/cancel")
async def cancel_profile_oauth(profile_id: str) -> Dict[str, Any]:
    from app.services.codex_oauth_service import get_codex_oauth_service

    try:
        return await asyncio.to_thread(get_codex_oauth_service().cancel, profile_id)
    except CodexProfileError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{profile_id}/oauth/logout")
async def logout_profile_oauth(profile_id: str) -> Dict[str, Any]:
    from app.services.codex_app_server import CodexAppServerError
    from app.services.codex_oauth_service import get_codex_oauth_service

    try:
        return await asyncio.to_thread(get_codex_oauth_service().logout, profile_id)
    except (CodexProfileError, CodexAppServerError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{profile_id}/models")
async def list_profile_models(profile_id: str) -> Dict[str, Any]:
    from app.services.codex_app_server import CodexAppServerError
    from app.services.codex_oauth_service import get_codex_oauth_service

    try:
        return await asyncio.to_thread(get_codex_oauth_service().list_models, profile_id)
    except (CodexProfileError, CodexAppServerError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
