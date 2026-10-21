"""Live recording uses the existing per-chat logger grant and plugin master switch."""
import logging
import sys

LOGGER_PLUGIN = "builtin_chat_logger"
logger = logging.getLogger(__name__)


def is_logger_grant(name: str) -> bool:
    return str(name or "").rsplit("/", 1)[-1] == LOGGER_PLUGIN


def is_chat_logging_enabled(chat_name: str) -> bool:
    # A loaded instance exists only while the global plugin is enabled.
    plugin = sys.modules.get("app.plugins.builtin_chat_logger.main")
    if getattr(plugin, "chat_logger_plugin", None) is None or not chat_name:
        return False
    from app.models.base import SessionLocal
    from app.models.user_permission import UserPermission, WeChatUser
    try:
        with SessionLocal() as db:
            names = db.query(UserPermission.plugin_name).join(
                WeChatUser, UserPermission.user_id == WeChatUser.id
            ).filter(WeChatUser.chat_name == chat_name).all()
            return any(is_logger_grant(name) for (name,) in names)
    except Exception:
        logger.warning("无法读取聊天记录授权，跳过本次记录", exc_info=True)
        return False
