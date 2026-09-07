"""Chat-scoped policy for the first-class Codex assistant."""

from __future__ import annotations

import logging

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Session, relationship
from sqlalchemy.sql import func

from .base import Base


logger = logging.getLogger(__name__)


def _wechat_user_model():
    """Resolve WeChatUser without depending on model import order."""
    from .user_permission import WeChatUser

    return WeChatUser


class AssistantChatPolicy(Base):
    __tablename__ = "assistant_chat_policies"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_assistant_chat_policy_user_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("wechat_users.id"), nullable=False, index=True)
    enabled = Column(Boolean, default=False, nullable=False)
    proactive_enabled = Column(Boolean, default=False, nullable=False)
    followup_enabled = Column(Boolean, default=False, nullable=False)
    followup_window_seconds = Column(Integer, default=60, nullable=False)
    followup_merge_seconds = Column(Integer, default=3, nullable=False)
    followup_max_turns = Column(Integer, default=3, nullable=False)
    ignored_senders = Column(Text, nullable=True)
    # Empty means inherit the administrator-selected default; a concrete id
    # selects an isolated managed Profile for each request.
    codex_profile_id = Column(String, nullable=True)
    version = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    user = relationship(_wechat_user_model, back_populates="assistant_policy")


def migrate_legacy_assistant_permissions(bind) -> int:
    """Move legacy ``builtin_chatbot`` grants into AssistantChatPolicy.

    The migration is idempotent and deletes only the retired assistant grant;
    every unrelated plugin grant remains untouched.
    """

    from app.models.user_permission import UserPermission

    migrated = 0
    with Session(bind=bind) as db:
        legacy_rows = (
            db.query(UserPermission)
            .filter(UserPermission.plugin_name == "builtin_chatbot")
            .order_by(UserPermission.id)
            .all()
        )
        for legacy in legacy_rows:
            policy = (
                db.query(AssistantChatPolicy)
                .filter(AssistantChatPolicy.user_id == legacy.user_id)
                .first()
            )
            if policy is None:
                policy = AssistantChatPolicy(
                    user_id=legacy.user_id,
                    enabled=True,
                    proactive_enabled=bool(legacy.proactive_enabled),
                    followup_enabled=bool(legacy.followup_enabled),
                    followup_window_seconds=int(legacy.followup_window_seconds or 60),
                    followup_merge_seconds=int(legacy.followup_merge_seconds or 3),
                    followup_max_turns=int(legacy.followup_max_turns or 3),
                    ignored_senders=legacy.ignored_senders,
                )
                db.add(policy)
                migrated += 1
            db.delete(legacy)
        if legacy_rows:
            db.commit()
            logger.info(
                "Migrated %s legacy assistant grants and removed %s plugin permission rows",
                migrated,
                len(legacy_rows),
            )
    return migrated
