"""Reusable configuration snapshots; applying one never creates a live binding."""

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint

from .base import Base
from .chat_plugin_config import utcnow


class PluginConfigTemplate(Base):
    __tablename__ = "plugin_config_templates"
    __table_args__ = (UniqueConstraint("plugin_name", "name", name="uq_plugin_config_template_name"),)

    id = Column(Integer, primary_key=True)
    plugin_name = Column(String(160), nullable=False, index=True)
    name = Column(String(80), nullable=False)
    schema_version = Column(Integer, nullable=False, default=1)
    version = Column(Integer, nullable=False, default=1)
    config_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
