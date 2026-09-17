"""Pydantic schemas for managed chats and plugin grants."""

from typing import List, Literal, Optional

from pydantic import BaseModel


class UserPermissionBase(BaseModel):
    plugin_name: str
    require_mention: bool = False


class UserPermissionCreate(UserPermissionBase):
    pass


class UserPermission(UserPermissionBase):
    id: int
    user_id: int

    class Config:
        from_attributes = True


class WeChatUserBase(BaseModel):
    chat_name: str
    is_group: bool = False
    listening_enabled: bool = True
    attachment_content_review_enabled: bool = True
    policy_version: int = 1
    codex_access_mode: Literal["isolated", "owner_full"] = "isolated"
    sender_blacklist: Optional[str] = None
    bot_group_nickname: Optional[str] = None
    bot_group_nickname_auto_enabled: bool = True
    bot_group_nickname_detected: Optional[str] = None
    bot_group_nickname_checked_at: Optional[str] = None


class WeChatUserCreate(WeChatUserBase):
    pass


class WeChatUserUpdate(BaseModel):
    is_group: Optional[bool] = None
    sender_blacklist: Optional[str] = None
    bot_group_nickname: Optional[str] = None
    bot_group_nickname_auto_enabled: Optional[bool] = None


class WeChatUser(WeChatUserBase):
    id: int
    assistant_enabled: bool = False
    permissions: List[UserPermission] = []

    class Config:
        from_attributes = True
