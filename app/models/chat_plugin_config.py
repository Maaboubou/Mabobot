"""Chat-owned plugin settings, independent of replaceable permission rows."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from .base import Base


def utcnow():
    return datetime.now(timezone.utc)


class ChatPluginConfig(Base):
    __tablename__ = "chat_plugin_configs"
    __table_args__ = (UniqueConstraint("user_id", "plugin_name", name="uq_chat_plugin_config"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("wechat_users.id"), nullable=False, index=True)
    plugin_name = Column(String(160), nullable=False)
    schema_version = Column(Integer, nullable=False, default=1)
    overrides_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    user = relationship("WeChatUser", back_populates="plugin_configs")
