"""First-class core Assistant console endpoints."""

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.plugin_manager import PluginManager
from app.core.wechat_manager import WeChatManager
from app.dependencies import get_plugin_manager_instance, get_wechat_manager_instance
from app.models.base import get_db
from app.services.assistant_console_service import AssistantConsoleError, AssistantConsoleService


router = APIRouter()


class AssistantChatUpdate(BaseModel):
    enabled: Optional[bool] = None
    proactive_enabled: Optional[bool] = None
    followup_enabled: Optional[bool] = None
    followup_window_seconds: Optional[int] = Field(default=None, ge=10, le=600)
    followup_merge_seconds: Optional[int] = Field(default=None, ge=1, le=30)
    followup_max_turns: Optional[int] = Field(default=None, ge=1, le=10)
    ignored_senders: Optional[List[str]] = None
    role_id: Optional[int] = Field(default=None, ge=1)
    judge_id: Optional[int] = Field(default=None, ge=1)
    bot_group_nickname: Optional[str] = Field(default=None, max_length=128)
    bot_group_nickname_auto_enabled: Optional[bool] = None


def get_assistant_service(
    plugin_manager: PluginManager = Depends(get_plugin_manager_instance),
    wechat_manager: WeChatManager = Depends(get_wechat_manager_instance),
    db: Session = Depends(get_db),
) -> AssistantConsoleService:
    return AssistantConsoleService(plugin_manager, wechat_manager, db)


@router.get("/overview")
def get_assistant_overview(
    service: AssistantConsoleService = Depends(get_assistant_service),
) -> Dict[str, Any]:
    return service.overview()


@router.patch("/chats/{user_id}")
def update_assistant_chat(
    user_id: int,
    request: AssistantChatUpdate,
    service: AssistantConsoleService = Depends(get_assistant_service),
) -> Dict[str, Any]:
    fields_set = getattr(request, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(request, "__fields_set__", set())
    changes = {field: getattr(request, field) for field in fields_set}
    try:
        service.update_chat(user_id, changes)
    except AssistantConsoleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"聊天配置未保存：{exc}") from exc
    return {"message": "聊天的 AI 助手配置已保存"}
