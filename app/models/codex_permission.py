"""Versioned Codex defaults, sparse chat overrides and runtime receipts."""

from sqlalchemy import Column, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from .base import Base


class CodexPermissionDefault(Base):
    __tablename__ = "codex_permission_defaults"

    scope_type = Column(String, primary_key=True)
    schema_version = Column(Integer, nullable=False, default=1)
    version = Column(Integer, nullable=False, default=1)
    config_json = Column(Text, nullable=False)
    updated_at = Column(String, nullable=False)
    updated_by = Column(String, nullable=True)


class CodexChatPermissionOverride(Base):
    __tablename__ = "codex_chat_permission_overrides"

    user_id = Column(Integer, ForeignKey("wechat_users.id", ondelete="CASCADE"), primary_key=True)
    schema_version = Column(Integer, nullable=False, default=1)
    overrides_json = Column(Text, nullable=False, default="{}")
    updated_at = Column(String, nullable=False)
    user = relationship("WeChatUser", back_populates="codex_permissions")


class CodexPermissionReceipt(Base):
    __tablename__ = "codex_permission_receipts"

    user_id = Column(Integer, ForeignKey("wechat_users.id", ondelete="CASCADE"), primary_key=True)
    policy_signature = Column(String, nullable=False)
    status = Column(String, nullable=False)
    applied_at = Column(String, nullable=True)
    reason_code = Column(String, nullable=True)
    # Receipts never survive process restarts as proof of a running sandbox.
    runtime_instance = Column(String, nullable=False)
    user = relationship("WeChatUser", back_populates="codex_permission_receipt")


class CodexPermissionRuntimeState(Base):
    """Short-lived evidence shared between the Bot and Web processes."""
    __tablename__ = "codex_permission_runtime_states"

    worker_id = Column(String, primary_key=True)
    profile_id = Column(String, nullable=False)
    process_id = Column(Integer, nullable=False)
    support_json = Column(Text, nullable=False)
    checked_at = Column(String, nullable=False)
