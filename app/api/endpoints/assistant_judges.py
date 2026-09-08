"""
ChatBot Judge 管理 API 端点
"""

from typing import Dict, Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.base import get_db
from app.models.chatbot_judge import ChatBotJudge, UserChatBotJudge
from app.models.user_permission import WeChatUser


router = APIRouter()


class JudgeCreateRequest(BaseModel):
    name: str
    display_name: str
    prompt: str
    prompt_mode: Literal["simple", "template"] = "simple"
    description: str = ""
    trigger_msg_threshold: int = 5
    trigger_interval_minutes: int = 1
    cooldown_msg_threshold: int = 5
    cooldown_minutes: int = 1


class JudgeUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    prompt: Optional[str] = None
    prompt_mode: Optional[Literal["simple", "template"]] = None
    description: Optional[str] = None
    trigger_msg_threshold: Optional[int] = None
    trigger_interval_minutes: Optional[int] = None
    cooldown_msg_threshold: Optional[int] = None
    cooldown_minutes: Optional[int] = None


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


def _reload_assistant_judges_safely() -> None:
    """重载运行中的 Judge 缓存。"""
    try:
        from app.assistant.runtime import get_assistant_handler
        import logging

        logger = logging.getLogger(__name__)
        handler = get_assistant_handler()
        if handler:
            handler.reload_judges()
            logger.info("✅ Assistant Judge 配置已重载")
        else:
            logger.warning("⚠️ Assistant 未运行，跳过 Judge 重载")
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning(f"⚠️ 重载 Judge 配置失败: {e}")


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
    """重载 Assistant Judge 配置。"""
    try:
        from app.assistant.runtime import get_assistant_handler

        handler = get_assistant_handler()
        if not handler:
            raise HTTPException(status_code=503, detail="Assistant 未运行")

        handler.reload_judges()
        return {"success": True, "message": "Assistant Judge 配置重新加载成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重新加载 Judge 配置失败: {e}")


@router.get("/{judge_id}")
async def get_judge(judge_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """获取单个 Judge"""
    try:
        judge = db.query(ChatBotJudge).filter(ChatBotJudge.id == judge_id).first()
        if not judge:
            raise HTTPException(status_code=404, detail="Judge 不存在")

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
    try:
        existing = db.query(ChatBotJudge).filter(ChatBotJudge.name == judge_request.name).first()
        if existing:
            raise HTTPException(status_code=400, detail="Judge 名称已存在")

        new_judge = ChatBotJudge(
            name=judge_request.name,
            display_name=judge_request.display_name,
            prompt=judge_request.prompt,
            prompt_mode=judge_request.prompt_mode,
            description=judge_request.description,
            **_normalize_timing_settings(judge_request.dict())
        )
        db.add(new_judge)
        db.commit()
        db.refresh(new_judge)

        _reload_assistant_judges_safely()
        return {"message": f"Judge '{new_judge.display_name}' 创建成功", "judge_id": new_judge.id}
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
            raise HTTPException(status_code=404, detail="Judge 不存在")

        if judge_request.display_name is not None:
            judge.display_name = judge_request.display_name
        if judge_request.description is not None:
            judge.description = judge_request.description
        if judge_request.prompt is not None:
            judge.prompt = judge_request.prompt
        if judge_request.prompt_mode is not None:
            judge.prompt_mode = judge_request.prompt_mode
        update_data = judge_request.dict(exclude_unset=True)
        for key, value in _normalize_timing_settings(update_data).items():
            if key in update_data:
                setattr(judge, key, value)

        db.commit()
        _reload_assistant_judges_safely()
        return {"message": f"Judge '{judge.display_name}' 更新成功"}
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
            raise HTTPException(status_code=404, detail="Judge 不存在")
        bind_count = db.query(UserChatBotJudge).filter(UserChatBotJudge.judge_id == judge_id).count()
        if bind_count > 0:
            raise HTTPException(status_code=400, detail="有用户正在使用此 Judge，无法删除")

        display_name = judge.display_name
        db.delete(judge)
        db.commit()
        _reload_assistant_judges_safely()
        return {"message": f"Judge '{display_name}' 删除成功"}
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
            raise HTTPException(status_code=404, detail="Judge 不存在")

        existing = db.query(UserChatBotJudge).filter(UserChatBotJudge.user_id == user_id).first()
        if existing:
            existing.judge_id = assign_request.judge_id
        else:
            db.add(UserChatBotJudge(user_id=user_id, judge_id=assign_request.judge_id))

        db.commit()
        _reload_assistant_judges_safely()
        return {"message": f"用户 '{user.chat_name}' 的 Judge 已设置为 '{judge.display_name}'"}
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
