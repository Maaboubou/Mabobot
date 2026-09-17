"""
ChatBot Judge 管理 API 端点
"""

import uuid

from typing import Dict, Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.base import get_db
from app.models.chatbot_judge import ChatBotJudge, UserChatBotJudge
from app.models.user_permission import WeChatUser


from app.services.assistant_prompt_service import prompt_revision, commit_prompt_update
from app.assistant.prompt_composer import normalize_legacy_prompt

router = APIRouter()


class JudgeCreateRequest(BaseModel):
    name: str = ""
    display_name: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=30000)
    prompt_mode: Literal["simple", "template"] = "simple"
    description: str = ""
    trigger_msg_threshold: int = Field(default=5, ge=0, le=1000)
    trigger_interval_minutes: int = Field(default=1, ge=0, le=1440)
    cooldown_msg_threshold: int = Field(default=5, ge=0, le=1000)
    cooldown_minutes: int = Field(default=1, ge=0, le=1440)


class JudgeUpdateRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    revision: Optional[str] = None
    prompt: Optional[str] = Field(default=None, min_length=1, max_length=30000)
    prompt_mode: Optional[Literal["simple", "template"]] = None
    description: Optional[str] = None
    trigger_msg_threshold: Optional[int] = Field(default=None, ge=0, le=1000)
    trigger_interval_minutes: Optional[int] = Field(default=None, ge=0, le=1440)
    cooldown_msg_threshold: Optional[int] = Field(default=None, ge=0, le=1000)
    cooldown_minutes: Optional[int] = Field(default=None, ge=0, le=1440)


class UserJudgeAssignRequest(BaseModel):
    judge_id: int = Field(..., ge=1)


def _normalize_timing_settings(data: Dict[str, Any]) -> Dict[str, int]:
    return {
        "trigger_msg_threshold": max(0, min(1000, int(data.get("trigger_msg_threshold") or 0))),
        "trigger_interval_minutes": max(0, min(1440, int(data.get("trigger_interval_minutes") or 0))),
        "cooldown_msg_threshold": max(0, min(1000, int(data.get("cooldown_msg_threshold") or 0))),
        "cooldown_minutes": max(0, min(1440, int(data.get("cooldown_minutes") or 0))),
    }


def _judge_to_dict(judge: ChatBotJudge, db: Session) -> Dict[str, Any]:
    return {
        "id": judge.id,
        "revision": prompt_revision(judge),
        "name": judge.name,
        "display_name": judge.display_name,
        "description": judge.description,
        "prompt": judge.prompt,
        "prompt_mode": judge.prompt_mode or "simple",
        "trigger_msg_threshold": judge.trigger_msg_threshold,
        "trigger_interval_minutes": judge.trigger_interval_minutes,
        "cooldown_msg_threshold": judge.cooldown_msg_threshold,
        "cooldown_minutes": judge.cooldown_minutes,
        "user_count": db.query(UserChatBotJudge).filter(UserChatBotJudge.judge_id == judge.id).count(),
        "created_at": judge.created_at.isoformat() if judge.created_at else None,
        "updated_at": judge.updated_at.isoformat() if judge.updated_at else None,
    }


def _reload_assistant_judges_safely() -> bool:
    """重载运行中的 Judge 缓存。"""
    try:
        from app.assistant.runtime import get_assistant_handler
        import logging

        logger = logging.getLogger(__name__)
        handler = get_assistant_handler()
        if handler:
            handler.reload_judges()
            logger.info("✅ Assistant 接话判断配置已重载")
            return True
        else:
            logger.warning("⚠️ Assistant 未运行，下次启动加载接话判断")
            return False
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning(f"⚠️ 重载接话判断配置失败: {e}")
        return False


@router.get("/")
async def list_judges(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """获取所有 Judge 列表"""
    try:
        judges = db.query(ChatBotJudge).all()
        judge_list = []
        for judge in judges:
            judge_list.append(_judge_to_dict(judge, db))
        return {"judges": judge_list, "total": len(judge_list)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reload")
async def reload_assistant_judges() -> Dict[str, Any]:
    """重载 Assistant 接话判断配置。"""
    try:
        from app.assistant.runtime import get_assistant_handler

        handler = get_assistant_handler()
        if not handler:
            raise HTTPException(status_code=503, detail="Assistant 未运行")

        handler.reload_judges()
        return {"success": True, "message": "Assistant 接话判断配置重新加载成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重新加载 接话判断配置失败: {e}")


@router.get("/{judge_id}")
async def get_judge(judge_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """获取单个 Judge"""
    try:
        judge = db.query(ChatBotJudge).filter(ChatBotJudge.id == judge_id).first()
        if not judge:
            raise HTTPException(status_code=404, detail="接话判断不存在")

        return {
            "judge": _judge_to_dict(judge, db)
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/")
async def create_judge(judge_request: JudgeCreateRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """创建 Judge"""
    if not judge_request.name.strip():
        judge_request.name = "judge_" + uuid.uuid4().hex[:12]
    try:
        existing = db.query(ChatBotJudge).filter(ChatBotJudge.name == judge_request.name).first()
        if existing:
            raise HTTPException(status_code=400, detail="接话判断名称已存在")

        new_judge = ChatBotJudge(
            name=judge_request.name,
            display_name=judge_request.display_name,
            prompt=normalize_legacy_prompt(judge_request.prompt, decision=True),
            prompt_mode="simple",
            description=judge_request.description,
            **_normalize_timing_settings(judge_request.dict())
        )
        db.add(new_judge)
        db.commit()
        db.refresh(new_judge)

        cache_refreshed = _reload_assistant_judges_safely()
        return {"applied": True, "cache_refreshed": cache_refreshed, "message": f"接话判断 '{new_judge.display_name}' 创建成功", "judge_id": new_judge.id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{judge_id}")
async def update_judge(
    judge_id: int, judge_request: JudgeUpdateRequest, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """更新 Judge"""
    try:
        judge = db.query(ChatBotJudge).filter(ChatBotJudge.id == judge_id).first()
        if not judge:
            raise HTTPException(status_code=404, detail="接话判断不存在")

        if judge_request.revision is not None and judge_request.revision != prompt_revision(judge):
            raise HTTPException(status_code=409, detail="配置已被其他操作更新，请重新打开后再保存")

        if judge_request.display_name is not None:
            judge.display_name = judge_request.display_name
        if judge_request.description is not None:
            judge.description = judge_request.description
        if judge_request.prompt is not None:
            judge.prompt = normalize_legacy_prompt(judge_request.prompt, decision=True)
        if judge_request.prompt_mode is not None:
            judge.prompt_mode = "simple"
        update_data = judge_request.dict(exclude_unset=True)
        for key, value in _normalize_timing_settings(update_data).items():
            if key in update_data:
                setattr(judge, key, value)

        commit_prompt_update(db, judge)
        cache_refreshed = _reload_assistant_judges_safely()
        return {"applied": True, "cache_refreshed": cache_refreshed, "message": f"接话判断 '{judge.display_name}' 更新成功"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{judge_id}")
async def delete_judge(judge_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """删除 Judge"""
    try:
        judge = db.query(ChatBotJudge).filter(ChatBotJudge.id == judge_id).first()
        if not judge:
            raise HTTPException(status_code=404, detail="接话判断不存在")
        bind_count = db.query(UserChatBotJudge).filter(UserChatBotJudge.judge_id == judge_id).count()
        if bind_count > 0:
            raise HTTPException(status_code=400, detail="有用户正在使用此接话判断，无法删除")

        display_name = judge.display_name
        db.delete(judge)
        db.commit()
        _reload_assistant_judges_safely()
        return {"message": f"接话判断 '{display_name}' 删除成功"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/users/{user_id}/judge")
async def get_user_judge(user_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """获取用户 Judge 绑定"""
    try:
        user = db.query(WeChatUser).filter(WeChatUser.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

        user_judge = db.query(UserChatBotJudge).filter(UserChatBotJudge.user_id == user_id).first()
        if not user_judge:
            return {"user_id": user_id, "chat_name": user.chat_name, "judge": None}

        judge = db.query(ChatBotJudge).filter(ChatBotJudge.id == user_judge.judge_id).first()
        if not judge:
            return {"user_id": user_id, "chat_name": user.chat_name, "judge": None}

        return {
            "user_id": user_id,
            "chat_name": user.chat_name,
            "judge": {
                "id": judge.id,
        "revision": prompt_revision(judge),
                "name": judge.name,
                "display_name": judge.display_name,
                "description": judge.description,
                "prompt_mode": judge.prompt_mode or "simple",
                "trigger_msg_threshold": judge.trigger_msg_threshold,
                "trigger_interval_minutes": judge.trigger_interval_minutes,
                "cooldown_msg_threshold": judge.cooldown_msg_threshold,
                "cooldown_minutes": judge.cooldown_minutes,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/users/{user_id}/judge")
async def assign_user_judge(
    user_id: int, assign_request: UserJudgeAssignRequest, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """为用户分配 Judge"""
    try:
        user = db.query(WeChatUser).filter(WeChatUser.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

        judge = db.query(ChatBotJudge).filter(ChatBotJudge.id == assign_request.judge_id).first()
        if not judge:
            raise HTTPException(status_code=404, detail="接话判断不存在")

        existing = db.query(UserChatBotJudge).filter(UserChatBotJudge.user_id == user_id).first()
        if existing:
            existing.judge_id = assign_request.judge_id
        else:
            db.add(UserChatBotJudge(user_id=user_id, judge_id=assign_request.judge_id))

        db.commit()
        _reload_assistant_judges_safely()
        return {"message": f"用户 '{user.chat_name}' 的 接话判断已设置为 '{judge.display_name}'"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/users/{user_id}/judge")
async def remove_user_judge(user_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """移除用户 Judge 绑定"""
    try:
        user = db.query(WeChatUser).filter(WeChatUser.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

        existing = db.query(UserChatBotJudge).filter(UserChatBotJudge.user_id == user_id).first()
        if existing:
            db.delete(existing)
            db.commit()
            _reload_assistant_judges_safely()
            return {"message": f"用户 '{user.chat_name}' 的 Judge 绑定已移除"}

        return {"message": f"用户 '{user.chat_name}' 没有 Judge 绑定"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
