#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
微信用户和权限的数据模型
"""

from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Text
from sqlalchemy.orm import relationship
from .base import Base
from .chat_plugin_config import ChatPluginConfig


def _assistant_chat_policy_model():
    """Resolve the policy model even in the standalone WeChat listener process."""
    from .assistant_policy import AssistantChatPolicy

    return AssistantChatPolicy

class WeChatUser(Base):
    __tablename__ = "wechat_users"

    id = Column(Integer, primary_key=True, index=True)
    chat_name = Column(String, unique=True, index=True, nullable=False)
    # Legacy storage only. The management remark feature was retired; keeping
    # the column avoids a destructive migration for existing installations.
    remark = Column(String, nullable=True)
    is_group = Column(Boolean, default=False)
    listening_enabled = Column(Boolean, default=True, nullable=False)
    # Monotonic revision for the aggregate chat policy document. Every write
    # through the unified policy API increments this value so concurrent admin
    # pages cannot silently overwrite one another.
    policy_version = Column(Integer, default=1, nullable=False)
    # Codex access is administrator-owned. New and unknown chats fail closed.
    # owner_full is valid only for an explicitly selected private chat.
    codex_access_mode = Column(String, default="isolated", nullable=False)
    sender_blacklist = Column(Text, nullable=True)  # 当前群/私聊内全局 sender 黑名单（JSON array）
    # 群内 @ 名称与微信账号的全局显示名并不总是相同。手动值始终作为
    # 备用别名；自动值只从微信群详情的“我在本群的昵称”字段读取。
    bot_group_nickname = Column(String, nullable=True)
    bot_group_nickname_auto_enabled = Column(Boolean, default=True)
    bot_group_nickname_detected = Column(String, nullable=True)
    # 自动校准发生变化时保留旧值，避免群成员继续使用旧 @ 名称时失联。
    bot_group_nickname_aliases = Column(Text, nullable=True)  # JSON array
    bot_group_nickname_checked_at = Column(String, nullable=True)

    permissions = relationship("UserPermission", back_populates="user", cascade="all, delete-orphan")
    plugin_configs = relationship(ChatPluginConfig, back_populates="user", cascade="all, delete-orphan")
    assistant_policy = relationship(
        _assistant_chat_policy_model,
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    # chatbot_role = relationship("UserChatBotRole", back_populates="user", uselist=False, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<WeChatUser(chat_name='{self.chat_name}')>"

    @property
    def assistant_enabled(self) -> bool:
        """Expose Assistant state without recreating the retired plugin grant."""
        return bool(self.assistant_policy and self.assistant_policy.enabled)

class UserPermission(Base):
    __tablename__ = "user_permissions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("wechat_users.id"), nullable=False)
    plugin_name = Column(String, nullable=False)
    require_mention = Column(Boolean, default=False)  # 是否需要@机器人才能触发
    # The fields below are retained temporarily for migration from historical
    # builtin_chatbot rows. New plugin grants use only plugin_name and
    # require_mention; assistant settings live in AssistantChatPolicy.
    proactive_enabled = Column(Boolean, default=False)  # legacy assistant field
    followup_enabled = Column(Boolean, default=False)  # Bot 回复后的无 @ 连续对话
    followup_window_seconds = Column(Integer, default=60)  # 回复后续聊有效窗口
    followup_merge_seconds = Column(Integer, default=3)  # 连续消息合并等待
    followup_max_turns = Column(Integer, default=3)  # 最多连续无 @ 回复轮数

    ignored_senders = Column(Text, nullable=True)  # builtin_chatbot 每群 sender 黑名单（JSON array）

    user = relationship("WeChatUser", back_populates="permissions")

    def __repr__(self):
        return f"<UserPermission(user_id={self.user_id}, plugin='{self.plugin_name}', require_mention={self.require_mention})>"
