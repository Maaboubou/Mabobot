#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
权限管理API端点
- 管理微信用户（群组）
- 管理用户对应的插件权限
"""

from typing import Any, Dict, List, Literal, Optional
import json
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models import base as models_base
from app.models import user_permission as models_permission
from app.schemas import permission as schemas_permission
from app.core.wechat_manager import WeChatManager
from app.dependencies import get_wechat_manager_instance
from app.services.codex_access_service import (
    ISOLATED_ACCESS,
    OWNER_FULL_ACCESS,
    normalize_codex_access_mode,
)


router = APIRouter()


def _normalize_sender_blacklist(raw_value: str | None) -> str | None:
    """Normalize textarea/plain/JSON blacklist input to a JSON string array."""
    if raw_value is None:
        return None

    if isinstance(raw_value, str):
        value = raw_value.strip()
        if not value:
            return None
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = value.replace(",", "\n").splitlines()
    else:
        parsed = raw_value

    if not isinstance(parsed, list):
        return None

    names = []
    seen = set()
    for item in parsed:
        name = str(item or "").strip()
        if name and name not in seen:
            names.append(name)
            seen.add(name)

    return json.dumps(names, ensure_ascii=False) if names else None


def _field_was_set(model, field_name: str) -> bool:
    fields_set = getattr(model, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(model, "__fields_set__", set())
    return field_name in fields_set


def _get_user_or_404(db: Session, user_id: int):
    db_user = (
        db.query(models_permission.WeChatUser)
        .filter(models_permission.WeChatUser.id == user_id)
        .first()
    )
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return db_user


def _invalidate_codex_thread(chat_name: str) -> None:
    """Ensure an existing thread is never reused across a policy transition."""
    try:
        from app.services.agent_runtime import get_agent_runtime
        from app.services.codex_profile_service import get_codex_runtime_registry

        get_agent_runtime().invalidate_chat(chat_name, reason="access_policy_changed")
        get_codex_runtime_registry().invalidate_chat(
            chat_name,
            reason="access_policy_changed",
        )
    except Exception:
        pass


@router.post("/users", response_model=schemas_permission.WeChatUser)
def create_wechat_user(
    user: schemas_permission.WeChatUserCreate,
    db: Session = Depends(models_base.get_db),
    wechat_manager: WeChatManager = Depends(get_wechat_manager_instance)
):
    """
    添加一个新的微信用户或群组到权限管理列表，并确保其被监听。
    如果用户已存在，则直接返回用户信息。
    """
    db_user = db.query(models_permission.WeChatUser).filter(models_permission.WeChatUser.chat_name == user.chat_name).first()
    if db_user:
        # “添加”已有聊天等同于显式恢复监听，并允许后续断线自动恢复。
        if not db_user.listening_enabled:
            db_user.listening_enabled = True
            db_user.policy_version = int(db_user.policy_version or 1) + 1
        db.commit()
        db.refresh(db_user)
        if wechat_manager and wechat_manager.is_connected():
            wechat_manager.add_listen_chat(db_user.chat_name)
        return db_user

    # 用户不存在，创建新用户
    requested_mode = normalize_codex_access_mode(user.codex_access_mode)
    if user.is_group and requested_mode == OWNER_FULL_ACCESS:
        raise HTTPException(status_code=400, detail="群聊不能配置为管理员最大权限")
    user_values = user.dict()
    user_values["codex_access_mode"] = requested_mode
    new_user = models_permission.WeChatUser(**user_values)
    new_user.listening_enabled = True
    new_user.sender_blacklist = _normalize_sender_blacklist(user.sender_blacklist)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # 开始监听
    if wechat_manager and wechat_manager.is_connected():
        wechat_manager.add_listen_chat(new_user.chat_name)
    else:
        # 即使微信未连接，也允许添加用户，只是日志警告
        print("Warning: WeChat manager not connected. User added to DB but not listening.")

    return new_user

@router.get("/users", response_model=List[schemas_permission.WeChatUser])
def list_wechat_users(skip: int = 0, limit: int = 100, db: Session = Depends(models_base.get_db)):
    """列出所有在权限管理列表中的微信用户"""
    users = db.query(models_permission.WeChatUser).offset(skip).limit(limit).all()
    return users

@router.get("/users/{user_id}", response_model=schemas_permission.WeChatUser)
def get_wechat_user(user_id: int, db: Session = Depends(models_base.get_db)):
    """获取单个微信用户的详细信息和权限"""
    db_user = db.query(models_permission.WeChatUser).filter(models_permission.WeChatUser.id == user_id).first()
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return db_user


@router.put("/users/{user_id}", response_model=schemas_permission.WeChatUser)
def update_wechat_user(
    user_id: int,
    user_update: schemas_permission.WeChatUserUpdate,
    db: Session = Depends(models_base.get_db)
):
    """更新微信用户的聊天类型与群聊规则。"""
    db_user = db.query(models_permission.WeChatUser).filter(models_permission.WeChatUser.id == user_id).first()
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    access_changed = False
    if user_update.is_group is not None:
        db_user.is_group = user_update.is_group
        if user_update.is_group and db_user.codex_access_mode == OWNER_FULL_ACCESS:
            db_user.codex_access_mode = ISOLATED_ACCESS
            access_changed = True
    if _field_was_set(user_update, "sender_blacklist"):
        db_user.sender_blacklist = _normalize_sender_blacklist(user_update.sender_blacklist)
    if _field_was_set(user_update, "bot_group_nickname"):
        nickname = str(user_update.bot_group_nickname or "").strip()
        db_user.bot_group_nickname = nickname or None
    if user_update.bot_group_nickname_auto_enabled is not None:
        db_user.bot_group_nickname_auto_enabled = user_update.bot_group_nickname_auto_enabled

    db_user.policy_version = int(db_user.policy_version or 1) + 1
    db.commit()
    db.refresh(db_user)
    if access_changed:
        _invalidate_codex_thread(db_user.chat_name)
    return db_user

@router.delete("/users/{user_id}", response_model=schemas_permission.WeChatUser)
def delete_wechat_user(
    user_id: int,
    db: Session = Depends(models_base.get_db),
    wechat_manager: WeChatManager = Depends(get_wechat_manager_instance)
):
    """从权限管理列表中删除一个用户及其所有权限，并停止监听"""
    db_user = db.query(models_permission.WeChatUser).filter(models_permission.WeChatUser.id == user_id).first()
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    chat_name_to_remove = db_user.chat_name

    # 从数据库删除
    db.delete(db_user)
    db.commit()

    # 停止监听
    if wechat_manager and wechat_manager.is_connected():
        wechat_manager.remove_listen_chat(chat_name_to_remove)
    else:
        print(f"Warning: WeChat manager not connected. User {chat_name_to_remove} removed from DB but listener might still be active if bot was restarted.")

    return db_user
