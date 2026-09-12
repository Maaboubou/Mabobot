"""Product-oriented capability endpoints for the management console."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.plugin_manager import PluginManager
from app.dependencies import get_plugin_manager_instance
from app.models.base import get_db
from app.services.capability_service import CapabilityConfigError, CapabilityService
from app.services.plugin_chat_config_service import PluginChatConfigError, PluginConfigTemplateService, PluginTemplateConflict


router = APIRouter()


class CapabilitySettingsPatch(BaseModel):
    values: Dict[str, Any] = Field(default_factory=dict)


class TranslationPreviewRequest(BaseModel):
    values: Dict[str, Any]
    text: str = Field(default="", max_length=12000)
    run_model: bool = False
    user_id: Optional[int] = Field(default=None, ge=1)


class ConfigTemplateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    values: Dict[str, Any]
    template_id: Optional[int] = Field(default=None, ge=1)
    expected_version: Optional[int] = Field(default=None, ge=1)


class DeleteConfigTemplateRequest(BaseModel):
    template_id: int = Field(ge=1)
    expected_version: int = Field(ge=1)


def get_capability_service(
    plugin_manager: PluginManager = Depends(get_plugin_manager_instance),
    db: Session = Depends(get_db),
) -> CapabilityService:
    return CapabilityService(plugin_manager, db)


@router.get("/")
def list_capabilities(
    service: CapabilityService = Depends(get_capability_service),
) -> Dict[str, Any]:
    capabilities = service.list_capabilities()
    return {"capabilities": capabilities, "total": len(capabilities)}


@router.get("/settings/{capability_id:path}")
def get_capability_settings(
    capability_id: str,
    user_id: Optional[int] = None,
    service: CapabilityService = Depends(get_capability_service),
) -> Dict[str, Any]:
    try:
        result = service.get_settings(capability_id, user_id=user_id)
    except (CapabilityConfigError, PluginChatConfigError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Capability not found")
    return result


@router.post("/translation-preview")
def preview_translation(
    request: TranslationPreviewRequest,
    service: CapabilityService = Depends(get_capability_service),
) -> Dict[str, Any]:
    from app.plugins.builtin_translation.settings import compile_prompt, validate_settings
    from app.models.user_permission import WeChatUser

    try:
        values = validate_settings(request.values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    user = None
    if request.user_id is not None:
        user = service.db.query(WeChatUser).filter_by(id=request.user_id).first()
        if user is None:
            raise HTTPException(status_code=404, detail="聊天不存在")
    result = {"prompt": compile_prompt(values)}
    if request.run_model:
        if not request.text.strip():
            raise HTTPException(status_code=422, detail="请输入试译原文")
        from app.plugins.builtin_translation.main import TranslationService
        from app.services.llm_usage_context import usage_context

        try:
            with usage_context({"user_id": user.id if user else None,
                                "chat_name": user.chat_name if user else None,
                                "usage_scope": "translation_preview"}):
                result["result"] = TranslationService().translate(request.text, values)
        except Exception as exc:
            raise HTTPException(status_code=502, detail="试译未成功，请检查模型设置和调用记录") from exc
    return result


@router.put("/settings/{capability_id:path}")
def update_capability_settings(
    capability_id: str,
    request: CapabilitySettingsPatch,
    service: CapabilityService = Depends(get_capability_service),
) -> Dict[str, Any]:
    try:
        result = service.update_settings(capability_id, request.values)
    except CapabilityConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"设置未应用：{exc}") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Capability not found")
    return {"message": "设置已保存并应用", "settings": result}


@router.get("/config-templates/{capability_id:path}")
def list_config_templates(capability_id: str, service: CapabilityService = Depends(get_capability_service)):
    try:
        return {"templates": PluginConfigTemplateService(service.db, service.plugin_manager).list(capability_id)}
    except PluginChatConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/config-templates/{capability_id:path}")
def save_config_template(capability_id: str, request: ConfigTemplateRequest,
                         service: CapabilityService = Depends(get_capability_service)):
    try:
        return PluginConfigTemplateService(service.db, service.plugin_manager).save(
            capability_id, request.name, request.values, request.template_id, request.expected_version)
    except PluginTemplateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PluginChatConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/config-templates/{capability_id:path}")
def delete_config_template(capability_id: str, request: DeleteConfigTemplateRequest,
                           service: CapabilityService = Depends(get_capability_service)):
    try:
        PluginConfigTemplateService(service.db, service.plugin_manager).delete(
            capability_id, request.template_id, request.expected_version)
        return {"deleted": True}
    except PluginTemplateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{capability_id:path}")
def get_capability(
    capability_id: str,
    service: CapabilityService = Depends(get_capability_service),
) -> Dict[str, Any]:
    capability = service.get_capability(capability_id)
    if capability is None:
        raise HTTPException(status_code=404, detail="Capability not found")
    return {"capability": capability}
