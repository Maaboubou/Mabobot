"""Assistant role management API endpoints."""

import logging
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models.base import get_db
from app.models.chatbot_role import ChatBotRole, UserChatBotRole
from app.models.user_permission import WeChatUser


router = APIRouter()
logger = logging.getLogger(__name__)


def _reload_assistant_roles_safely() -> bool:
    """Refresh the live role cache when the Assistant is running."""
    try:
        from app.assistant.runtime import get_assistant_handler

        handler = get_assistant_handler()
        if handler is None:
            logger.warning("Assistant 未运行，角色将在下次启动时加载")
            return False
        handler.reload_roles()
        return True
    except Exception as exc:
        logger.warning("重载 Assistant 角色配置失败: %s", exc)
        return False


class RoleCreateRequest(BaseModel):
    name: str
    display_name: str
    prompt: str
    description: str = ""
    output_split_enabled: bool = False
    output_max_chars: int = 120
    output_max_count: int = 3
    output_strip_trailing_period: bool = True
    output_interval_seconds: float = 1.0


class RoleUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    prompt: Optional[str] = None
    description: Optional[str] = None
    output_split_enabled: Optional[bool] = None
    output_max_chars: Optional[int] = None
    output_max_count: Optional[int] = None
    output_strip_trailing_period: Optional[bool] = None
    output_interval_seconds: Optional[float] = None


class UserRoleAssignRequest(BaseModel):
    role_id: int


def _normalize_output_settings(data: Dict[str, Any]) -> Dict[str, Any]:
    max_chars = max(10, min(2000, int(data.get("output_max_chars") or 120)))
    max_count = max(1, min(10, int(data.get("output_max_count") or 3)))
    interval = max(0.0, min(10.0, float(data.get("output_interval_seconds") or 0.0)))
    return {
        "output_split_enabled": bool(data.get("output_split_enabled", False)),
        "output_max_chars": max_chars,
        "output_max_count": max_count,
        "output_strip_trailing_period": bool(data.get("output_strip_trailing_period", True)),
        "output_interval_seconds": interval,
    }


def _role_to_dict(role: ChatBotRole, db: Session) -> Dict[str, Any]:
    return {
        "id": role.id,
        "name": role.name,
        "display_name": role.display_name,
        "description": role.description,
        "prompt": role.prompt,
        "output_split_enabled": bool(role.output_split_enabled),
        "output_max_chars": role.output_max_chars,
        "output_max_count": role.output_max_count,
        "output_strip_trailing_period": bool(role.output_strip_trailing_period),
        "output_interval_seconds": role.output_interval_seconds,
        "user_count": db.query(UserChatBotRole).filter(UserChatBotRole.role_id == role.id).count(),
        "created_at": role.created_at.isoformat() if role.created_at else None,
        "updated_at": role.updated_at.isoformat() if role.updated_at else None,
    }


@router.get("/")
async def list_roles(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """获取所有角色列表"""
    try:
        roles = db.query(ChatBotRole).all()

        role_list = []
        for role in roles:
            role_list.append(_role_to_dict(role, db))

        return {
            "roles": role_list,
            "total": len(role_list)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 注意：具体路径必须在参数化路径之前定义，避免被 /{role_id} 捕获


@router.get("/{role_id}")
async def get_role(role_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """获取单个角色详情"""
    try:
        role = db.query(ChatBotRole).filter(ChatBotRole.id == role_id).first()
        if not role:
            raise HTTPException(status_code=404, detail="角色不存在")

        return {
            "role": _role_to_dict(role, db)
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/")
async def create_role(
    role_request: RoleCreateRequest,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """创建新角色"""
    try:
        # 检查角色名是否已存在
        existing_role = db.query(ChatBotRole).filter(ChatBotRole.name == role_request.name).first()
        if existing_role:
            raise HTTPException(status_code=400, detail="角色名称已存在")

        # 创建新角色
        new_role = ChatBotRole(
            name=role_request.name,
            display_name=role_request.display_name,
            prompt=role_request.prompt,
            description=role_request.description,
            **_normalize_output_settings(role_request.dict())
        )

        db.add(new_role)
        db.commit()
        db.refresh(new_role)

        _reload_assistant_roles_safely()

        return {
            "message": f"角色 '{role_request.display_name}' 创建成功",
            "role_id": new_role.id
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{role_id}")
async def update_role(
    role_id: int,
    role_request: RoleUpdateRequest,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """更新角色信息"""
    try:
        role = db.query(ChatBotRole).filter(ChatBotRole.id == role_id).first()
        if not role:
            raise HTTPException(status_code=404, detail="角色不存在")

        role_name = role.name  # 保存角色名用于日志

        # 更新字段
        if role_request.display_name is not None:
            role.display_name = role_request.display_name
        if role_request.prompt is not None:
            role.prompt = role_request.prompt
        if role_request.description is not None:
            role.description = role_request.description
        update_data = role_request.dict(exclude_unset=True)
        for key, value in _normalize_output_settings(update_data).items():
            if key in update_data:
                setattr(role, key, value)

        db.commit()

        _reload_assistant_roles_safely()

        return {
            "message": f"角色 '{role.display_name}' 更新成功"
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{role_id}")
async def delete_role(role_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """删除角色"""
    try:
        role = db.query(ChatBotRole).filter(ChatBotRole.id == role_id).first()
        if not role:
            raise HTTPException(status_code=404, detail="角色不存在")

        role_name = role.name  # 保存角色名用于日志
        display_name = role.display_name

        # 检查是否有用户正在使用
        user_count = db.query(UserChatBotRole).filter(UserChatBotRole.role_id == role_id).count()
        if user_count > 0:
            raise HTTPException(status_code=400, detail="有用户正在使用此角色，无法删除")

        db.delete(role)
        db.commit()

        _reload_assistant_roles_safely()

        return {
            "message": f"角色 '{display_name}' 删除成功"
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/users/{user_id}/role")
async def get_user_role(user_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """获取用户的角色配置"""
    try:
        user = db.query(WeChatUser).filter(WeChatUser.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

        # 查询用户角色关联
        user_role = db.query(UserChatBotRole).filter(UserChatBotRole.user_id == user_id).first()
        if user_role:
            role = db.query(ChatBotRole).filter(ChatBotRole.id == user_role.role_id).first()
            return {
                "user_id": user_id,
                "chat_name": user.chat_name,
                "role": {
                    "id": role.id,
                    "name": role.name,
                    "display_name": role.display_name,
                    "description": role.description
                }
            }
        else:
            return {
                "user_id": user_id,
                "chat_name": user.chat_name,
                "role": None
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/users/{user_id}/role")
async def assign_user_role(
    user_id: int,
    assign_request: UserRoleAssignRequest,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """为用户分配角色"""
    try:
        user = db.query(WeChatUser).filter(WeChatUser.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

        role = db.query(ChatBotRole).filter(ChatBotRole.id == assign_request.role_id).first()
        if not role:
            raise HTTPException(status_code=404, detail="角色不存在")

        # 检查是否已有角色配置
        existing_user_role = db.query(UserChatBotRole).filter(UserChatBotRole.user_id == user_id).first()
        if existing_user_role:
            # 更新现有配置
            existing_user_role.role_id = assign_request.role_id
        else:
            # 创建新配置
            user_role = UserChatBotRole(user_id=user_id, role_id=assign_request.role_id)
            db.add(user_role)

        db.commit()

        _reload_assistant_roles_safely()

        return {
            "message": f"用户 '{user.chat_name}' 的角色已设置为 '{role.display_name}'"
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/users/{user_id}/role")
async def remove_user_role(user_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """移除用户的角色配置"""
    try:
        user = db.query(WeChatUser).filter(WeChatUser.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

        user_role = db.query(UserChatBotRole).filter(UserChatBotRole.user_id == user_id).first()
        if user_role:
            db.delete(user_role)
            db.commit()

            _reload_assistant_roles_safely()

            return {
                "message": f"用户 '{user.chat_name}' 的角色配置已移除"
            }
        else:
            return {
                "message": f"用户 '{user.chat_name}' 没有角色配置"
            }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reload")
async def reload_assistant_roles():
    """重新加载 Assistant 的角色配置。"""
    try:
        if not _reload_assistant_roles_safely():
            raise HTTPException(
                status_code=503,
                detail="Assistant 未运行"
            )
        return {
            "success": True,
            "message": "Assistant 角色配置重新加载成功"
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        logger.error(f"❌ 重新加载角色配置失败: {str(e)}")
        logger.error(f"堆栈追踪:\n{traceback.format_exc()}")
        raise HTTPException(
            status_code=500,
            detail=f"重新加载角色配置失败: {str(e)}"
        )
