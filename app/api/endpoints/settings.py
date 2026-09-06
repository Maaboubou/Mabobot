"""
系统设置管理API端点
"""

from typing import Any, Dict, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models.base import get_db
from app.services.settings_service import SettingsService
from app.schemas import setting as schemas_setting
from app.services.system_settings_service import (
    SystemSettingsConsoleService,
    SystemSettingsError,
    is_sensitive_setting,
)

router = APIRouter()


class SystemSettingsUpdate(BaseModel):
    values: Dict[str, Any]


def _public_setting(setting) -> schemas_setting.SettingPublic:
    sensitive = is_sensitive_setting(setting.key)
    return schemas_setting.SettingPublic(
        key=setting.key,
        value="********" if sensitive else setting.value,
        description=setting.description,
        category=setting.category,
        is_sensitive=sensitive,
    )

def get_settings_service(db: Session = Depends(get_db)) -> SettingsService:
    """依赖注入，获取SettingsService实例"""
    return SettingsService(db)

@router.get("/", response_model=List[schemas_setting.SettingPublic])
def get_all_settings(
    settings_service: SettingsService = Depends(get_settings_service)
):
    """获取所有设置项（对敏感信息脱敏）"""
    settings = settings_service.get_all()
    public_settings = []
    for s in settings:
        # 简单的脱敏逻辑：如果key包含API_KEY或SECRET，则隐藏值
        # 扩展脱敏逻辑：包含 TOKEN, PASSWORD 等也隐藏
        public_settings.append(_public_setting(s))
    return public_settings

@router.put("/console")
def update_settings_console(
    request: SystemSettingsUpdate,
    db: Session = Depends(get_db),
):
    try:
        result = SystemSettingsConsoleService(db).update(request.values)
    except SystemSettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"message": "系统设置已保存", "console": result}


@router.get("/console")
def get_settings_console(db: Session = Depends(get_db)):
    return SystemSettingsConsoleService(db).get_console()


@router.post("/", response_model=schemas_setting.SettingPublic)
def create_or_update_setting(
    setting_in: schemas_setting.SettingCreate,
    settings_service: SettingsService = Depends(get_settings_service)
):
    """创建或更新一个设置项"""
    setting = settings_service.set(
        key=setting_in.key,
        value=setting_in.value,
        description=setting_in.description,
        category=setting_in.category
    )
    return _public_setting(setting)

@router.get("/{key}", response_model=schemas_setting.SettingPublic)
def get_setting_by_key(
    key: str,
    settings_service: SettingsService = Depends(get_settings_service)
):
    """获取单个设置项（对敏感信息脱敏）"""
    value = settings_service.get(key)
    if value is None:
        raise HTTPException(status_code=404, detail=f"Setting '{key}' not found")

    is_sensitive = is_sensitive_setting(key)

    return schemas_setting.SettingPublic(
        key=key,
        value="********" if is_sensitive else value,
        is_sensitive=is_sensitive
    )

@router.put("/{key}", response_model=schemas_setting.SettingPublic)
def update_setting_value(
    key: str,
    setting_in: schemas_setting.SettingUpdate,
    settings_service: SettingsService = Depends(get_settings_service)
):
    """仅更新一个设置项的值"""
    # 检查是否为敏感信息，如果是脱敏的星号则不更新
    is_sensitive = is_sensitive_setting(key)

    if is_sensitive and setting_in.value == "********":
        # 如果是敏感信息且值为脱敏星号，则跳过更新
        raise HTTPException(
            status_code=400,
            detail=f"不能保存脱敏的星号值，请输入真实的{key}"
        )

    setting = settings_service.set(key, setting_in.value)
    if not setting:
         raise HTTPException(status_code=404, detail=f"Setting '{key}' not found, cannot update.")
    return _public_setting(setting)

@router.post("/reload-env")
async def reload_environment_config():
    """从.env文件重新加载配置到数据库"""
    try:
        from app.services.config_service import reload_from_env
        from app.services.openai_service import rebuild_client

        updated_count = reload_from_env()
        rebuild_client()  # 重建OpenAI客户端

        return {
            "success": True,
            "message": f"成功重新加载了 {updated_count} 个配置项",
            "updated_count": updated_count
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"重新加载配置失败: {str(e)}"
        )

@router.delete("/{key}")
def delete_setting(
    key: str,
    settings_service: SettingsService = Depends(get_settings_service)
):
    """删除指定的设置项"""
    success = settings_service.delete(key)
    if not success:
        raise HTTPException(status_code=404, detail=f"Setting '{key}' not found or already deleted.")
    return {"success": True, "message": f"Setting '{key}' deleted successfully."}
