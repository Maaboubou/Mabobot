"""
数据库基础模型
"""

import logging
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker


logger = logging.getLogger(__name__)

# 数据库配置
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/database.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def drop_legacy_knowledge_base_columns(bind=None):
    """Permanently remove the retired per-chat knowledge-base configuration."""
    active_engine = bind or engine
    inspector = inspect(active_engine)
    if not inspector.has_table("user_permissions"):
        return []

    existing = {column["name"] for column in inspector.get_columns("user_permissions")}
    obsolete = [name for name in ("kb_enabled", "kb_id") if name in existing]
    if not obsolete:
        return []

    quote = active_engine.dialect.identifier_preparer.quote
    table_name = quote("user_permissions")
    with active_engine.begin() as connection:
        for column_name in obsolete:
            connection.execute(
                text(f"ALTER TABLE {table_name} DROP COLUMN {quote(column_name)}")
            )
            logger.info("已删除弃用的知识库字段: user_permissions.%s", column_name)
    return obsolete


def ensure_wechat_user_access_columns(bind=None):
    """Apply the small forward migration required by existing SQLite installs."""
    active_engine = bind or engine
    inspector = inspect(active_engine)
    if not inspector.has_table("wechat_users"):
        return []

    existing = {column["name"] for column in inspector.get_columns("wechat_users")}
    added = []
    quote = active_engine.dialect.identifier_preparer.quote
    if "codex_access_mode" not in existing:
        with active_engine.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {quote('wechat_users')} "
                    f"ADD COLUMN {quote('codex_access_mode')} "
                    "VARCHAR NOT NULL DEFAULT 'isolated'"
                )
            )
        logger.info("已添加 Codex 聊天访问策略字段: wechat_users.codex_access_mode")
        added.append("codex_access_mode")
    if "policy_version" not in existing:
        with active_engine.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {quote('wechat_users')} "
                    f"ADD COLUMN {quote('policy_version')} "
                    "INTEGER NOT NULL DEFAULT 1"
                )
            )
        logger.info("已添加聊天策略版本字段: wechat_users.policy_version")
        added.append("policy_version")
    return added


def ensure_assistant_policy_columns(bind=None):
    """Add request-scoped Codex profile selection to existing policy tables."""
    active_engine = bind or engine
    inspector = inspect(active_engine)
    if not inspector.has_table("assistant_chat_policies"):
        return []
    existing = {
        column["name"] for column in inspector.get_columns("assistant_chat_policies")
    }
    if "codex_profile_id" in existing:
        return []
    quote = active_engine.dialect.identifier_preparer.quote
    with active_engine.begin() as connection:
        connection.execute(
            text(
                f"ALTER TABLE {quote('assistant_chat_policies')} "
                f"ADD COLUMN {quote('codex_profile_id')} VARCHAR"
            )
        )
    logger.info("已添加 Assistant 的逐请求 Codex Profile 字段")
    return ["codex_profile_id"]


def migrate_legacy_current_codex_bindings(bind=None):
    """Map the retired ``__current__`` choice to inherited managed Profile use."""
    active_engine = bind or engine
    inspector = inspect(active_engine)
    if not inspector.has_table("assistant_chat_policies"):
        return 0
    existing = {
        column["name"] for column in inspector.get_columns("assistant_chat_policies")
    }
    if "codex_profile_id" not in existing:
        return 0
    quote = active_engine.dialect.identifier_preparer.quote
    with active_engine.begin() as connection:
        result = connection.execute(
            text(
                f"UPDATE {quote('assistant_chat_policies')} "
                f"SET {quote('codex_profile_id')} = NULL "
                f"WHERE {quote('codex_profile_id')} = :legacy_value"
            ),
            {"legacy_value": "__current__"},
        )
    migrated = int(result.rowcount or 0)
    if migrated:
        logger.info("已迁移 %s 条旧版当前 Codex 绑定为默认 Profile", migrated)
    return migrated


def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def drop_legacy_memory_columns(bind=None):
    """Remove configuration columns for the deleted generated-memory engine."""
    active_engine = bind or engine
    inspector = inspect(active_engine)
    quote = active_engine.dialect.identifier_preparer.quote
    removed = []
    with active_engine.begin() as connection:
        for table in ("user_permissions", "assistant_chat_policies"):
            if inspector.has_table(table) and "memory_profile" in {
                column["name"] for column in inspector.get_columns(table)
            }:
                connection.execute(text(f"ALTER TABLE {quote(table)} DROP COLUMN memory_profile"))
                removed.append(table)
    return removed


def create_tables():
    """创建所有表"""
    # 导入所有模型以确保它们被注册
    from . import setting
    from . import user_permission  # 先导入WeChatUser
    from . import chatbot_role    # 后导入引用WeChatUser的模型
    from . import chatbot_judge   # Judge 配置与用户绑定
    from . import assistant_policy
    from . import chat_plugin_config
    from . import plugin_config_template

    Base.metadata.create_all(bind=engine)
    drop_legacy_memory_columns()
    drop_legacy_knowledge_base_columns(engine)
    ensure_wechat_user_access_columns(engine)
    ensure_assistant_policy_columns(engine)
    migrate_legacy_current_codex_bindings(engine)
    assistant_policy.migrate_legacy_assistant_permissions(engine)
