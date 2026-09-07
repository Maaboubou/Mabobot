"""
FastAPI主应用程序
"""

import asyncio
import logging
import os
from dotenv import load_dotenv

from .utils.network_env import (
    configure_startup_network_environment,
    preload_litellm_cost_map_direct,
)
from .utils.logging_utils import create_rotating_file_handler


# app.main 也可能被 uvicorn 直接导入，因此在其他应用模块之前加载本地端口配置。
load_dotenv()
configure_startup_network_environment()
preload_litellm_cost_map_direct()

from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from sqlalchemy import text
import uvicorn
from typing import Optional

# An explicit one-shot cleanup runs before any telemetry singleton is loaded.
from pathlib import Path
from .history.cleanup_legacy import run_pending_cleanup
run_pending_cleanup(Path(__file__).resolve().parents[1])

from .core.event_bus import get_event_bus, EventType
from .core.plugin_manager import PluginManager
from .core.wechat_manager import WeChatManager
from .models.base import create_tables, SessionLocal
from .api.endpoints import assistant, assistant_judges, assistant_roles, automation, backups, capabilities, chat_policies, codex_profiles, operations, system, settings, plugins, wechat, permissions, dashboard
from .api.endpoints import codex_skills, email_notifications
from .api import internal as internal_api
from .api import codex_proxy
from .api import codex_jobs
from .api import llm_config
from .api import litellm_updates
from .api import tool_updates
from .models import user_permission as models_permission
from .models import setting as models_setting
from .services.feishu_bitable_service import FeishuBitableService
from .services.wechat_monitor_service import get_monitor_service
from .utils.plugin_config import get_plugin_setting
from .utils.health_state import stable_active_listeners
from .chatbot_presets import BUILTIN_CHATBOT_JUDGES, BUILTIN_CHATBOT_ROLES
from .version import APP_VERSION

# 配置日志
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        create_rotating_file_handler("logs/app.log"),
        logging.StreamHandler()
    ]
)

# 配置第三方库日志级别，减少噪音
logging.getLogger("werkzeug").setLevel(logging.WARNING)  # 只记录警告以上
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # 禁用访问日志
logging.getLogger("mabowx").setLevel(logging.INFO)
logging.getLogger("requests").setLevel(logging.WARNING)  # requests库只记录警告
logging.getLogger("urllib3").setLevel(logging.WARNING)  # urllib3库只记录警告
logging.getLogger("app.plugins").setLevel(logging.WARNING)  # 插件注册日志设为警告以上

logger = logging.getLogger(__name__)

# 全局变量
event_bus = None
plugin_manager: Optional[PluginManager] = None
wechat_manager = None
monitor_service = None
agent_runtime = None
assistant_runtime = None


def ensure_initial_settings(db: SessionLocal):
    """确保初始配置从.env文件加载到数据库"""
    load_dotenv() # 加载.env文件

    required_keys = [
        "OPENAI_API_KEY",
        "LINKAI_API_KEY",
        "PERPLEXITY_API_KEY",
        "GEMINI_API_KEY",
        "LINKAI_API_BASE",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_APP_TYPE",
        "FEISHU_BITABLE_APP_TOKEN",
        "WECHAT_BOT_NAME",
        "GITHUB_TOKEN",
        "QQEMAIL_ADDR",
        "QQEMAIL_CODE",
        "TIKHUB_API_TOKEN",
        "CODEX_PROXY_KEY",
    ]

    for key in required_keys:
        # 检查数据库中是否已存在
        existing_setting = db.query(models_setting.Setting).filter(models_setting.Setting.key == key).first()
        if not existing_setting:
            # 从环境变量获取
            value = os.getenv(key)
            if value:
                logger.debug(f"'{key}' not found in database. Migrating from .env file...")
                new_setting = models_setting.Setting(key=key, value=value)
                db.add(new_setting)
                db.commit()
                logger.debug(f"Successfully migrated '{key}' to database.")
            else:
                logger.warning(f"'{key}' not found in .env file. Some features may not work.")

    db.commit()

    _ensure_chatbot_role_output_columns(db)
    _ensure_chatbot_judge_timing_columns(db)
    _ensure_user_permission_extension_columns(db)
    _ensure_wechat_user_sender_blacklist_column(db)
    _ensure_wechat_user_bot_nickname_columns(db)
    _ensure_wechat_user_listener_preference_column(db)

    # 创建默认的ChatBot角色
    _ensure_default_assistant_roles(db)
    # 创建默认的ChatBot Judge，并迁移绑定
    default_judge = _ensure_default_assistant_judges(db)
    migration_flag_key = "CHATBOT_DEFAULT_JUDGE_BIND_MIGRATION_V1"
    migration_flag = db.query(models_setting.Setting).filter(models_setting.Setting.key == migration_flag_key).first()
    if default_judge and not migration_flag:
        _bind_default_judge_to_existing_chatbot_users(db, default_judge.id)
        db.add(models_setting.Setting(key=migration_flag_key, value=datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        db.commit()


def _ensure_chatbot_role_output_columns(db: SessionLocal):
    """为历史 SQLite 数据库补齐角色输出规范字段。"""
    try:
        if "sqlite" not in str(db.bind.url):
            return

        existing = {
            row[1]
            for row in db.execute(text("PRAGMA table_info(chatbot_roles)")).fetchall()
        }
        columns = {
            "output_split_enabled": "BOOLEAN NOT NULL DEFAULT 0",
            "output_max_chars": "INTEGER NOT NULL DEFAULT 120",
            "output_max_count": "INTEGER NOT NULL DEFAULT 3",
            "output_strip_trailing_period": "BOOLEAN NOT NULL DEFAULT 1",
            "output_interval_seconds": "FLOAT NOT NULL DEFAULT 1.0",
        }
        for name, ddl in columns.items():
            if name not in existing:
                db.execute(text(f"ALTER TABLE chatbot_roles ADD COLUMN {name} {ddl}"))
                logger.info("已为 chatbot_roles 添加字段: %s", name)
        db.commit()
    except Exception as e:
        logger.error(f"补齐ChatBot角色输出规范字段失败: {e}")
        db.rollback()


def _ensure_chatbot_judge_timing_columns(db: SessionLocal):
    """为历史 SQLite 数据库补齐 Judge 触发/冷却参数字段。"""
    try:
        if "sqlite" not in str(db.bind.url):
            return

        trigger_interval = int(get_plugin_setting("assistant", "proactive_interval_minutes", 1) or 1)
        trigger_threshold = int(get_plugin_setting("assistant", "proactive_msg_threshold", 5) or 0)
        cooldown_minutes = trigger_interval
        cooldown_threshold = trigger_threshold

        existing = {
            row[1]
            for row in db.execute(text("PRAGMA table_info(chatbot_judges)")).fetchall()
        }
        columns = {
            "trigger_msg_threshold": f"INTEGER NOT NULL DEFAULT {max(0, trigger_threshold)}",
            "trigger_interval_minutes": f"INTEGER NOT NULL DEFAULT {max(0, trigger_interval)}",
            "cooldown_msg_threshold": f"INTEGER NOT NULL DEFAULT {max(0, cooldown_threshold)}",
            "cooldown_minutes": f"INTEGER NOT NULL DEFAULT {max(0, cooldown_minutes)}",
        }
        for name, ddl in columns.items():
            if name not in existing:
                db.execute(text(f"ALTER TABLE chatbot_judges ADD COLUMN {name} {ddl}"))
                logger.info("已为 chatbot_judges 添加字段: %s", name)
        db.commit()
    except Exception as e:
        logger.error(f"补齐ChatBot Judge触发/冷却字段失败: {e}")
        db.rollback()


def _ensure_user_permission_extension_columns(db: SessionLocal):
    """为历史 SQLite 数据库补齐插件的群/私聊级扩展配置字段。"""
    try:
        if "sqlite" not in str(db.bind.url):
            return

        existing = {
            row[1]
            for row in db.execute(text("PRAGMA table_info(user_permissions)")).fetchall()
        }
        columns = {
            "ignored_senders": "TEXT",
            "followup_enabled": "BOOLEAN NOT NULL DEFAULT 0",
            "followup_window_seconds": "INTEGER NOT NULL DEFAULT 60",
            "followup_merge_seconds": "INTEGER NOT NULL DEFAULT 3",
            "followup_max_turns": "INTEGER NOT NULL DEFAULT 3",
        }
        for name, ddl in columns.items():
            if name not in existing:
                db.execute(text(f"ALTER TABLE user_permissions ADD COLUMN {name} {ddl}"))
                logger.info("已为 user_permissions 添加字段: %s", name)
        db.commit()
    except Exception as e:
        logger.error(f"补齐插件群/私聊级扩展配置字段失败: {e}")
        db.rollback()


def _ensure_wechat_user_sender_blacklist_column(db: SessionLocal):
    """为历史 SQLite 数据库补齐群/用户级全局 sender 黑名单字段。"""
    try:
        if "sqlite" not in str(db.bind.url):
            return

        existing = {
            row[1]
            for row in db.execute(text("PRAGMA table_info(wechat_users)")).fetchall()
        }
        if "sender_blacklist" not in existing:
            db.execute(text("ALTER TABLE wechat_users ADD COLUMN sender_blacklist TEXT"))
            logger.info("已为 wechat_users 添加字段: sender_blacklist")
        db.commit()
    except Exception as e:
        logger.error(f"补齐群/用户级 sender 黑名单字段失败: {e}")
        db.rollback()


def _ensure_wechat_user_bot_nickname_columns(db: SessionLocal):
    """为历史 SQLite 数据库补齐群内机器人昵称字段。"""
    try:
        if "sqlite" not in str(db.bind.url):
            return

        existing = {
            row[1]
            for row in db.execute(text("PRAGMA table_info(wechat_users)")).fetchall()
        }
        columns = {
            "bot_group_nickname": "TEXT",
            "bot_group_nickname_auto_enabled": "BOOLEAN NOT NULL DEFAULT 1",
            "bot_group_nickname_detected": "TEXT",
            "bot_group_nickname_aliases": "TEXT",
            "bot_group_nickname_checked_at": "TEXT",
        }
        for name, ddl in columns.items():
            if name not in existing:
                db.execute(text(f"ALTER TABLE wechat_users ADD COLUMN {name} {ddl}"))
                logger.info("已为 wechat_users 添加字段: %s", name)
        db.commit()
    except Exception as e:
        logger.error(f"补齐群内机器人昵称字段失败: {e}")
        db.rollback()


def _ensure_wechat_user_listener_preference_column(db: SessionLocal):
    """为历史 SQLite 数据库补齐持久化的监听启用状态。"""
    try:
        if "sqlite" not in str(db.bind.url):
            return

        existing = {
            row[1]
            for row in db.execute(text("PRAGMA table_info(wechat_users)")).fetchall()
        }
        if "listening_enabled" not in existing:
            db.execute(
                text(
                    "ALTER TABLE wechat_users "
                    "ADD COLUMN listening_enabled BOOLEAN NOT NULL DEFAULT 1"
                )
            )
            logger.info("已为 wechat_users 添加字段: listening_enabled")
        db.commit()
    except Exception as e:
        logger.error(f"补齐聊天监听启用状态字段失败: {e}")
        db.rollback()


def _ensure_default_assistant_roles(db: SessionLocal):
    """确保系统内置的 ChatBot 角色存在，不覆盖用户已有配置。"""
    try:
        from app.models.chatbot_role import ChatBotRole

        desired_roles = [
            {
                "name": "default",
                "display_name": "默认助手",
                "description": "友好、专业的AI助手",
                "prompt": "你是一个有用的AI助手，能够回答各种问题并提供帮助。请用简洁明了的语言回复。",
                "is_builtin": "true"
            },
            *BUILTIN_CHATBOT_ROLES,
        ]

        created_count = 0
        for role_data in desired_roles:
            existing = db.query(ChatBotRole).filter(ChatBotRole.name == role_data["name"]).first()
            if existing:
                continue
            db.add(ChatBotRole(**role_data))
            created_count += 1

        db.commit()
        if created_count:
            logger.info("成功创建 %s 个内置 ChatBot 角色", created_count)

    except Exception as e:
        logger.error(f"创建内置 ChatBot 角色失败: {e}")
        db.rollback()


def _get_system_default_judge_prompt() -> str:
    """系统内置默认 Judge 模板（template 模式）"""
    return """## Role
你是一个高情商的聊天群组观察员，你的名字是刘局(GG)。

## Context Background
以下是最近约 30 条对话历史，用于你理解当前的聊天主题、语气和氛围。
[对话开始]
{chat_text}
[对话结束]

## Task
请重点分析【对话结束】前的最后几条消息。

## Rules for Intervention (介入准则)
1. **不介入**：如果最后几条消息是：
   - 礼貌性结束语
   - 纯表情包或无意义的复读。
   - 无法判断当前主题。
2. **介入**：如果：
   - 用户表现出明显的困惑或在寻找答案。
   - 话题出现冷场，且你有一个非常幽默的梗可以接（适合闲聊场景）。
   - 有人提到了你的名字或暗示需要 AI 帮助。

## Output (Strict JSON)
{
  "atmosphere": "简述当前氛围（如：技术讨论、轻松闲聊、争论等）",
  "should_reply": true/false,
  "reason": "为什么判断需要或不需要回复"
}"""


def _ensure_default_assistant_judges(db: SessionLocal):
    """确保内置 Judge 存在，并从旧配置迁移默认 prompt（幂等）。"""
    try:
        from app.models.chatbot_judge import ChatBotJudge

        default_judge = db.query(ChatBotJudge).filter(ChatBotJudge.name == "default_judge").first()
        created_count = 0
        if not default_judge:
            legacy_prompt = get_plugin_setting("assistant", "proactive_judge_prompt", None)
            prompt_text = legacy_prompt if isinstance(legacy_prompt, str) and legacy_prompt.strip() else _get_system_default_judge_prompt()

            default_judge = ChatBotJudge(
                name="default_judge",
                display_name="默认 Judge",
                description="默认主动回复判断器（由旧 proactive_judge_prompt 迁移）",
                prompt=prompt_text,
                prompt_mode="template",
                trigger_msg_threshold=int(get_plugin_setting("assistant", "proactive_msg_threshold", 5) or 0),
                trigger_interval_minutes=int(get_plugin_setting("assistant", "proactive_interval_minutes", 1) or 1),
                cooldown_msg_threshold=int(get_plugin_setting("assistant", "proactive_msg_threshold", 5) or 0),
                cooldown_minutes=int(get_plugin_setting("assistant", "proactive_interval_minutes", 1) or 1),
                is_builtin="true",
            )
            db.add(default_judge)
            created_count += 1

        for judge_data in BUILTIN_CHATBOT_JUDGES:
            existing = db.query(ChatBotJudge).filter(ChatBotJudge.name == judge_data["name"]).first()
            if existing:
                continue
            db.add(ChatBotJudge(**judge_data))
            created_count += 1

        db.commit()
        db.refresh(default_judge)
        if created_count:
            logger.info("成功创建 %s 个内置 ChatBot Judge", created_count)
        return default_judge
    except Exception as e:
        logger.error(f"创建内置 ChatBot Judge 失败: {e}")
        db.rollback()
        return None


def _bind_default_judge_to_existing_chatbot_users(db: SessionLocal, default_judge_id: int):
    """为当前已有 chatbot 权限的用户批量绑定默认 Judge（幂等）"""
    try:
        from app.models.chatbot_judge import UserChatBotJudge
        from app.models.assistant_policy import AssistantChatPolicy

        user_ids = (
            db.query(AssistantChatPolicy.user_id)
            .filter(AssistantChatPolicy.enabled.is_(True))
            .distinct()
            .all()
        )
        if not user_ids:
            return

        created_count = 0
        for row in user_ids:
            user_id = row[0]
            exists = db.query(UserChatBotJudge).filter(UserChatBotJudge.user_id == user_id).first()
            if exists:
                continue
            db.add(UserChatBotJudge(user_id=user_id, judge_id=default_judge_id))
            created_count += 1

        if created_count > 0:
            db.commit()
            logger.info(f"成功为 {created_count} 个存量用户绑定默认 Judge")
        else:
            logger.debug("存量用户默认 Judge 绑定无需变更")
    except Exception as e:
        logger.error(f"批量绑定默认 Judge 失败: {e}")
        db.rollback()


def sync_all_listeners(db: SessionLocal, wechat_manager: WeChatManager, plugin_manager: PluginManager):
    """Synchronize explicit listener intent without granting capabilities."""
    if not (wechat_manager and wechat_manager.is_connected_cached()):
        logger.warning("WeChat manager not connected, skipping listener sync.")
        return

    listener_status = wechat_manager.get_listener_status()
    if listener_status.get("status") != "success":
        logger.warning(
            "Listener status unavailable; skipping bulk sync to avoid duplicate UI work: %s",
            listener_status.get("message"),
        )
        return

    active_listeners = stable_active_listeners(listener_status)
    registered_listeners = {
        str(name)
        for key in ("desired", "actual")
        for name in (listener_status.get(key) or [])
        if name
    }

    logger.debug("Syncing all listeners from database...")
    users = db.query(models_permission.WeChatUser).all()
    if not users:
        logger.info("No users found in database to sync.")
        return

    success_count = 0
    for user in users:
        # 手动暂停是持久化意图，启动和重连时都不能被自动恢复覆盖。
        if not bool(user.listening_enabled):
            if user.chat_name in registered_listeners:
                if wechat_manager.remove_listen_chat(user.chat_name):
                    logger.info("Removed stale listener registration for paused chat '%s'.", user.chat_name)
                else:
                    logger.warning("Failed to remove stale listener registration for paused chat '%s'.", user.chat_name)
            else:
                logger.debug("Listener explicitly paused for '%s'; skipping auto recovery.", user.chat_name)
        elif user.chat_name in active_listeners:
            success_count += 1
            logger.debug("Listener already active for '%s'; skipping UI rebind.", user.chat_name)
        elif wechat_manager.add_listen_chat(user.chat_name):
            success_count += 1
            logger.debug(f"Successfully started listening to '{user.chat_name}'.")
        else:
            logger.error(f"Failed to start listening to '{user.chat_name}'.")

        current_permissions = {p.plugin_name for p in user.permissions}
        if not current_permissions:
            logger.debug(
                "Chat '%s' has no explicit capability grants; leaving it unassigned",
                user.chat_name,
            )
        else:
            logger.debug(f"User '{user.chat_name}' already has permissions: {current_permissions}")
            # 现有用户保持其权限配置不变

    logger.info(f"Listener sync completed. {success_count}/{len(users)} listeners active.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global event_bus, plugin_manager, wechat_manager, monitor_service, agent_runtime, assistant_runtime

    logger.info("Starting Mabobot...")

    try:
        # 创建数据目录
        os.makedirs("data", exist_ok=True)
        os.makedirs("data/chat_logs", exist_ok=True)

        # Recover committed raw history before plugins begin recording messages.
        from app.history.tools import get_archive
        get_archive()

        # 1. 初始化数据库
        create_tables()
        logger.debug("Database initialized")

        # 2. 确保初始配置已从.env加载到数据库
        with SessionLocal() as db:
            ensure_initial_settings(db)
        logger.debug("Initial settings checked/migrated.")

        try:
            from .services.agent_runtime import get_agent_runtime

            agent_runtime = get_agent_runtime()
            loop = asyncio.get_running_loop()
            started = await loop.run_in_executor(None, agent_runtime.start)
            if started:
                logger.info("Codex runtime started")
            else:
                logger.warning("Codex runtime is using its fallback backend")
        except Exception as e:
            logger.warning("Codex runtime startup failed: %s", e)

        # 3. 初始化事件总线
        event_bus = get_event_bus(db_session_factory=SessionLocal)
        await event_bus.start()
        logger.info("Event bus started")

        # 4.1 初始化飞书服务并注入到事件总线上下文（需早于插件加载）
        try:
            feishu_service = FeishuBitableService()
            event_bus.context["feishu"] = feishu_service
            logger.info("FeishuBitableService initialized and injected into event bus context")
        except Exception as e:
            logger.warning(f"FeishuBitableService init failed (will be lazy-initialized when used): {e}")

        # 4. 初始化插件管理器（放在注入共享服务之后，保证插件register时可获取）
        plugin_manager = PluginManager(event_bus=event_bus)
        plugin_manager.start_monitoring()
        plugin_manager.load_all_plugins()
        logger.info("Plugin manager initialized")

        # The assistant is a first-class, failure-isolated application
        # component.  It is deliberately started outside PluginManager.
        from app.assistant.runtime import get_assistant_runtime

        assistant_runtime = get_assistant_runtime()
        if not assistant_runtime.start(event_bus):
            logger.warning(
                "Core assistant is degraded; independent plugins remain available: %s",
                assistant_runtime.status().get("error") or "unknown error",
            )

        # 5. 初始化微信管理器
        wechat_manager = WeChatManager(event_bus=event_bus)
        if not wechat_manager.start():
            logger.warning("Could not connect to wx_bot service. Some features will be unavailable.")
        else:
            logger.info("WeChat manager connected to wx_bot service")
            # 注入到全局上下文，便于插件在计划任务中直接发送
            try:
                event_bus.context["wx"] = wechat_manager
            except Exception:
                pass

        # 6. 同步所有已存在的监听器
        with SessionLocal() as db:
            if plugin_manager and wechat_manager:
                sync_all_listeners(db, wechat_manager, plugin_manager)

        # 注册微信重连事件监听，重连时从数据库全量恢复监听
        def on_wechat_reconnected(event):
            logger.info("Received WECHAT_RECONNECTED event, syncing listeners from database...")
            # 因为是在事件处理线程执行，需要新的db session
            with SessionLocal() as db:
                if plugin_manager and wechat_manager:
                    sync_all_listeners(db, wechat_manager, plugin_manager)

        if event_bus:
            event_bus.subscribe(
                event_type=EventType.WECHAT_RECONNECTED,
                handler=on_wechat_reconnected,
                plugin_name="system_main",
                order_index=0
            )

        # 7. 启动微信掉线监控服务。即使启动瞬间微信已经离线，也必须让
        # 监控线程运行，才能推进重新登录并发送二维码；连接恢复由
        # WeChatManager 自身的后台监控同步。
        if wechat_manager:
            try:
                monitor_service = get_monitor_service()
                monitor_service.set_wechat_manager(wechat_manager)
                if monitor_service.start_monitoring():
                    logger.info("✅ 微信掉线监控服务已启动")
                else:
                    logger.warning("⚠️ 微信掉线监控服务启动失败")
            except Exception as e:
                logger.error(f"❌ 微信掉线监控服务初始化失败: {e}")
        else:
            logger.warning("⚠️ 微信管理器未初始化，跳过掉线监控服务启动")

        # 将管理器实例添加到应用状态
        app.state.event_bus = event_bus
        app.state.plugin_manager = plugin_manager
        app.state.wechat_manager = wechat_manager
        app.state.monitor_service = monitor_service
        app.state.codex_runtime = agent_runtime
        app.state.assistant_runtime = assistant_runtime

        logger.info("Application startup completed")

        yield

    except Exception as e:
        logger.error(f"Startup failed: {e}")
        raise

    finally:
        # 清理资源
        logger.info("Shutting down application...")

        if monitor_service:
            monitor_service.stop_monitoring()
            logger.info("WeChat monitor service stopped")

        if assistant_runtime:
            assistant_runtime.stop()
            logger.info("Core assistant stopped")

        if plugin_manager:
            plugin_manager.stop_monitoring()
            unload_results = plugin_manager.unload_all_plugins()
            unloaded_count = sum(unload_results.values())
            logger.info(
                "Plugin manager stopped; unloaded %s/%s plugins",
                unloaded_count,
                len(unload_results),
            )

        if agent_runtime:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, agent_runtime.stop)
            except Exception as e:
                logger.warning("Codex runtime shutdown failed: %s", e)

        try:
            from app.services.codex_profile_service import get_codex_runtime_registry

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, get_codex_runtime_registry().stop_all)
        except Exception as e:
            logger.warning("Codex Profile runtimes shutdown failed: %s", e)

        if wechat_manager:
            wechat_manager.stop()
            logger.info("WeChat manager stopped")

        if event_bus:
            await event_bus.stop()
            logger.info("Event bus stopped")

        logger.info("Application shutdown completed")


# 创建FastAPI应用
app = FastAPI(
    title="Mabobot",
    description="基于事件驱动的微信自动化助手",
    version=APP_VERSION,
    lifespan=lifespan
)

# The bundled console is same-origin and does not need CORS. Deployments with
# a separate frontend can explicitly opt in to a comma-separated allowlist.
cors_origins = [
    origin.strip()
    for origin in os.getenv("WEB_CORS_ORIGINS", "").split(",")
    if origin.strip()
]
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
    )


# 注册路由
app.include_router(system.router, prefix="/api/system", tags=["system"])
app.include_router(backups.router, prefix="/api/backups", tags=["backups"])
app.include_router(operations.router, prefix="/api/operations", tags=["operations"])
app.include_router(email_notifications.router, prefix="/api/settings/notifications/email", tags=["settings"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])
app.include_router(plugins.router, prefix="/api/plugins", tags=["plugins"])
app.include_router(capabilities.router, prefix="/api/capabilities", tags=["capabilities"])
app.include_router(automation.router, prefix="/api/automation", tags=["automation"])
app.include_router(assistant.router, prefix="/api/assistant", tags=["assistant"])
app.include_router(chat_policies.router, prefix="/api/chats", tags=["chat-policies"])
app.include_router(codex_profiles.router, prefix="/api/codex/profiles", tags=["codex-profiles"])
app.include_router(codex_skills.router, prefix="/api/codex/profiles", tags=["codex-skills"])
app.include_router(wechat.router, prefix="/api/wechat", tags=["wechat"])
app.include_router(internal_api.router, prefix="/api/internal", tags=["internal"])
app.include_router(codex_proxy.router)
app.include_router(codex_jobs.router)
app.include_router(permissions.router, prefix="/api/permissions", tags=["permissions"])
from .api.endpoints import history
app.include_router(history.router, prefix="/api/history", tags=["history"])
app.include_router(assistant_roles.router, prefix="/api/assistant/roles", tags=["assistant_roles"])
app.include_router(assistant_judges.router, prefix="/api/assistant/judges", tags=["assistant_judges"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(llm_config.router)  # LLM 配置管理 API（已包含 /api/llm 前缀）
app.include_router(litellm_updates.router)
app.include_router(tool_updates.router)

# 静态文件服务（前端）
# 挂载web目录作为静态文件服务
app.mount("/notification-assets", StaticFiles(directory="mabobot_launcher/ui/notifications"), name="notification-assets")
app.mount("/static", StaticFiles(directory="web"), name="static")

@app.get("/", response_class=HTMLResponse)
async def read_root():
    """根路径 - 提供Web界面"""
    # 直接使用web/index.html
    web_index = "web/index.html"
    if os.path.exists(web_index):
        with open(web_index, 'r', encoding='utf-8') as f:
            return HTMLResponse(content=f.read())

    # 如果没有前端文件，返回简单的状态页面
    return HTMLResponse(content="""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Mabobot 管理面板</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; }
            .status { padding: 20px; border-radius: 5px; margin: 10px 0; }
            .success { background-color: #d4edda; border: 1px solid #c3e6cb; }
            .info { background-color: #d1ecf1; border: 1px solid #bee5eb; }
        </style>
    </head>
    <body>
        <h1>Mabobot 管理面板</h1>
        <div class="status success">
            <strong>✅ 系统正在运行</strong>
        </div>
        <div class="status info">
            <strong>API文档:</strong> <a href="/docs">/docs</a><br>
            <strong>系统状态:</strong> <a href="/api/system/status">/api/system/status</a><br>
            <strong>插件列表:</strong> <a href="/api/plugins">/api/plugins</a>
        </div>
        <p>Web管理界面文件未找到，请检查 web/ 目录。</p>
    </body>
    </html>
    """)


@app.get("/health")
async def health_check():
    """健康检查端点；只读缓存，绝不在探针路径同步请求 wx_bot。"""
    manager = getattr(app.state, 'wechat_manager', None)
    return {
        "status": "live",
        "version": APP_VERSION,
        "components": {
            "event_bus": hasattr(app.state, 'event_bus') and app.state.event_bus is not None,
            "plugin_manager": hasattr(app.state, 'plugin_manager') and app.state.plugin_manager is not None,
            "wechat_manager": bool(manager and manager.is_connected_cached())
        }
    }


@app.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
async def spa_route_fallback(full_path: str):
    """Serve the console shell for client-side routes.

    API and static misses must remain real 404 responses; only operator-facing
    routes are eligible for the SPA fallback.
    """
    if full_path.startswith(("api/", "static/")):
        raise HTTPException(status_code=404, detail="Not found")
    return await read_root()


if __name__ == "__main__":
    # 开发模式运行
    uvicorn.run(
        "app.main:app",
        host=os.getenv("WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("WEB_PORT", "8888")),
        reload=True,
        log_level="info"
    )
