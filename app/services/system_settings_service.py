"""Product-facing system console with explicit setting ownership.

The database contains settings created by the core application, plugins,
internal jobs and model connections.  A product UI must not infer their
meaning from the key name: doing so previously mixed model credentials with
email, GitHub and plugin settings and exposed controls that the runtime never
read.  This module is the presentation/ownership registry for the System page.
"""

from __future__ import annotations

import os
import re
from collections import OrderedDict
from typing import Any, Dict, List, Mapping

from sqlalchemy.orm import Session

from app.models.setting import Setting
from app.models.user_permission import WeChatUser
from app.services.settings_service import SettingsService


class SystemSettingsError(ValueError):
    pass


GROUPS = OrderedDict(
    [
        (
            "identity",
            {
                "title": "微信身份",
                "description": "自动识别优先，全局名称只作为备用",
                "icon": "bi-person-badge",
            },
        ),
        (
            "integrations",
            {
                "title": "外部服务",
                "description": "飞书、通知与第三方服务连接",
                "icon": "bi-link-45deg",
            },
        ),
        (
            "runtime",
            {
                "title": "运行环境",
                "description": "查看启动参数及其真正的配置来源",
                "icon": "bi-activity",
            },
        ),
        (
            "developer",
            {
                "title": "扩展设置",
                "description": "仅管理尚未归属的自定义键与导入工具",
                "icon": "bi-sliders",
            },
        ),
    ]
)


# Only settings with an explicit product meaning belong in the first three
# groups. Unknown rows remain available in the advanced settings view.
FIELD_SPECS: Dict[str, Dict[str, Any]] = {
    "WECHAT_BOT_NAME": {
        "group": "identity",
        "section": "备用身份",
        "title": "全局备用 @ 名称",
        "description": "仅在群聊昵称尚未自动识别、且该聊天没有手动备用名称时使用；也参与部分消息的机器人/用户归类。",
        "source": "数据库 · 全局备用",
        "editable": True,
        "always": True,
        "requires_restart": True,
    },
    "FEISHU_APP_ID": {
        "group": "integrations", "section": "飞书", "title": "飞书 App ID",
        "description": "飞书自建应用的 App ID。", "source": "本机数据库", "editable": True,
    },
    "FEISHU_APP_SECRET": {
        "group": "integrations", "section": "飞书", "title": "飞书 App Secret",
        "description": "飞书应用密钥，只保存在本机数据库。", "source": "本机数据库", "editable": True,
    },
    "FEISHU_APP_TYPE": {
        "group": "integrations", "section": "飞书", "title": "飞书应用类型",
        "description": "通常使用企业自建应用；不确定时保持 custom。", "source": "本机数据库", "editable": True,
        "control": "select",
        "options": [
            {"value": "custom", "label": "企业自建（custom）"},
            {"value": "store", "label": "应用商店（store）"},
        ],
    },
    "FEISHU_BITABLE_APP_TOKEN": {
        "group": "integrations", "section": "飞书", "title": "默认多维表格 Token",
        "description": "供使用默认飞书多维表格的能力读取。", "source": "本机数据库", "editable": True,
    },
    "GITHUB_TOKEN": {
        "group": "integrations", "section": "通知与分享", "title": "GitHub Token",
        "description": "用于需要 GitHub Gist 等能力的功能。", "source": "本机数据库优先", "editable": True,
    },
    "CODEX_PROXY_KEY": {
        "group": "integrations", "section": "本地服务", "title": "Codex 代理访问密钥",
        "description": "保护本地 Codex 代理接口；修改后新的请求立即使用。", "source": "本机数据库优先", "editable": True,
    },
    "CODEX_BINARY_POLICY": {
        "group": "runtime", "section": "Codex", "title": "Codex 二进制策略",
        "description": "运行时使用全局 Codex 命令并自动识别当前版本。",
        "source": ".env / 启动环境", "editable": False, "environment_only": True,
    },
    "CODEX_CONFIG_POLICY": {
        "group": "runtime", "section": "Codex", "title": "Codex 配置策略",
        "description": "控制任务是否继承全局 Codex 配置、Rules 与 Skills。",
        "source": ".env / 启动环境", "editable": False, "environment_only": True,
    },
    "CODEX_INTERACTIVE_POOL_SIZE": {
        "group": "runtime", "section": "Codex", "title": "交互进程数",
        "description": "承载聊天与持久会话的 Codex 进程数量。",
        "source": ".env / 启动环境", "editable": False, "environment_only": True,
    },
    "CODEX_BATCH_POOL_SIZE": {
        "group": "runtime", "section": "Codex", "title": "批处理进程数",
        "description": "承载周报、记忆和代理请求的 Codex 进程数量。",
        "source": ".env / 启动环境", "editable": False, "environment_only": True,
    },
    "CODEX_APP_SERVER_ROTATE_TOKENS": {
        "group": "runtime", "section": "Codex 会话", "title": "线程 Token 软上限",
        "description": "当前线程输入达到该值后，在下一轮用完整锚点创建新线程。",
        "source": ".env / 启动环境", "editable": False, "environment_only": True,
    },
    "CODEX_APP_SERVER_MAX_COMPACTIONS": {
        "group": "runtime", "section": "Codex 会话", "title": "线程压缩次数上限",
        "description": "默认 2 次；达到后在下一轮轮换线程，0 表示关闭此条件。",
        "source": ".env / 启动环境", "editable": False, "environment_only": True,
    },
    "CODEX_APP_SERVER_IDLE_ROTATE_SECONDS": {
        "group": "runtime", "section": "Codex 会话", "title": "空闲轮换时间",
        "description": "默认 2592000 秒（30 天）；超过后下一次消息创建新线程，0 表示关闭。",
        "source": ".env / 启动环境", "editable": False, "environment_only": True,
    },
    "CODEX_APP_SERVER_CONTEXT_SAFETY_TOKENS": {
        "group": "runtime", "section": "Codex 会话", "title": "上下文安全余量",
        "description": "根据 Profile 或提供方上报的窗口计算线程软上限时预留的 Token。",
        "source": ".env / 启动环境", "editable": False, "environment_only": True,
    },
    # These integrations currently read the process environment directly.
    # Present their state, but do not offer a database input that cannot work.
    "TIKHUB_API_TOKEN": {
        "group": "integrations", "section": "插件运行环境", "title": "TikHub Token",
        "description": "摘要增强插件直接从启动环境读取。请在 .env 中设置后重启。",
        "source": ".env / 启动环境", "editable": False, "environment_only": True,
    },
    "CF_ACCOUNT_ID": {
        "group": "integrations", "section": "插件运行环境", "title": "Cloudflare 账号 ID",
        "description": "图片编辑的 R2 上传功能直接从 .env 读取。", "source": ".env / 启动环境",
        "editable": False, "environment_only": True,
    },
    "CF_BUCKET": {
        "group": "integrations", "section": "插件运行环境", "title": "R2 Bucket",
        "description": "图片编辑的 R2 存储桶，直接从 .env 读取。", "source": ".env / 启动环境",
        "editable": False, "environment_only": True,
    },
    "CF_PUBLIC_URL": {
        "group": "integrations", "section": "插件运行环境", "title": "R2 公开地址",
        "description": "R2 文件的公开访问基础地址，直接从 .env 读取。", "source": ".env / 启动环境",
        "editable": False, "environment_only": True,
    },
    "CF_ACCESS_KEY": {
        "group": "integrations", "section": "插件运行环境", "title": "R2 Access Key",
        "description": "Cloudflare R2 访问密钥，直接从 .env 读取。", "source": ".env / 启动环境",
        "editable": False, "environment_only": True,
    },
    "CF_SECRET_KEY": {
        "group": "integrations", "section": "插件运行环境", "title": "R2 Secret Key",
        "description": "Cloudflare R2 秘密密钥，直接从 .env 读取。", "source": ".env / 启动环境",
        "editable": False, "environment_only": True,
    },
    # Startup values are deliberately diagnostics. Changing their database row
    # cannot change the process that already selected its port/database/path.
    "WEB_PORT": {
        "group": "runtime", "section": "启动参数", "title": "Web 端口",
        "description": "端口在进程启动前确定；请修改 .env 或启动命令后重启。",
        "source": ".env / 启动命令", "editable": False, "runtime_key": True,
    },
    "WX_BOT_PORT": {
        "group": "runtime", "section": "启动参数", "title": "微信桥接端口",
        "description": "微信桥接进程与 Web 端的桥接客户端共用此端口；修改 .env 后重启。",
        "source": ".env / 启动命令", "editable": False, "runtime_key": True,
    },
    "DATABASE_URL": {
        "group": "runtime", "section": "启动参数", "title": "数据库连接",
        "description": "数据库连接在模块加载时建立，不能从当前数据库反向切换自身。",
        "source": ".env / 启动环境", "editable": False, "runtime_key": True,
    },
    "LOG_LEVEL": {
        "group": "runtime", "section": "启动参数", "title": "日志级别",
        "description": "日志级别在应用启动时确定；请通过启动配置调整并重启。",
        "source": "应用启动配置", "editable": False, "runtime_key": True,
    },
    "PLUGINS_DIR": {
        "group": "runtime", "section": "固定路径", "title": "插件目录",
        "description": "插件从项目内 app/plugins 加载，目录由项目结构定义。",
        "source": "项目结构", "editable": False, "runtime_key": True,
    },
}


MODEL_SETTING_PREFIXES = (
    "OPENAI", "ANTHROPIC", "GEMINI", "DEEPSEEK", "OPENROUTER", "PERPLEXITY",
    "LINKAI", "GROK", "KIMI", "AZURE", "BEDROCK", "VERTEX", "MISTRAL",
    "GROQ", "XAI", "COHERE", "TOGETHER", "DEEPINFRA", "FIREWORKS",
)
MODEL_MANAGED_KEYS = {"LLM_PROXY_URL"}
NON_UI_INTERNAL_KEYS = {"LLM_CALL_TIMEOUT", "QQEMAIL_ADDR", "QQEMAIL_CODE",
                        "EMAIL_NOTIFICATION_CONFIG", "EMAIL_NOTIFICATION_PASSWORD", "EMAIL_NOTIFICATION_HISTORY"}


SENSITIVE_PATTERN = re.compile(
    r"(API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|ACCESS_KEY|QQEMAIL_CODE|CODEX_PROXY_KEY|DATABASE_URL|PROXY_URL|DSN|WEBHOOK)",
    re.IGNORECASE,
)


def is_sensitive_setting(key: str) -> bool:
    return bool(SENSITIVE_PATTERN.search(key))


def is_model_credential_setting(key: str) -> bool:
    upper = key.upper()
    return upper.startswith(MODEL_SETTING_PREFIXES) and (
        "API_KEY" in upper or upper.endswith(("_TOKEN", "_SECRET", "_API_BASE"))
    )


def _title_for_key(key: str) -> str:
    words = key.replace("_", " ").title()
    return words.replace("Api", "API").replace("Url", "URL").replace("Id", "ID")


def _clear_setting_caches() -> None:
    SettingsService._get_from_db_cached.cache_clear()
    try:
        from app.services import config_service

        config_service._settings_cache.clear()
        config_service._last_cache_time = 0
    except Exception:
        # The console save has already committed; cache cleanup must not turn a
        # successful write into a misleading API failure during early startup.
        pass


class SystemSettingsConsoleService:
    def __init__(self, db: Session):
        self.db = db

    def _identity_summary(self) -> Dict[str, int]:
        try:
            groups = self.db.query(WeChatUser).filter(WeChatUser.is_group.is_(True)).all()
        except Exception:
            return {"group_count": 0, "auto_enabled_count": 0, "detected_count": 0, "manual_count": 0}
        return {
            "group_count": len(groups),
            "auto_enabled_count": sum(bool(item.bot_group_nickname_auto_enabled) for item in groups),
            "detected_count": sum(bool(str(item.bot_group_nickname_detected or "").strip()) for item in groups),
            "manual_count": sum(bool(str(item.bot_group_nickname or "").strip()) for item in groups),
        }

    def _field(self, key: str, setting: Setting | None, spec: Dict[str, Any]) -> Dict[str, Any]:
        sensitive = is_sensitive_setting(key)
        configured = bool(setting and setting.value)
        value = setting.value if setting else ""
        readonly_text = ""
        if spec.get("environment_only"):
            configured = bool(str(os.getenv(key, "") or "").strip())
            readonly_text = "已由启动环境配置" if configured else "当前运行环境未配置 · 请修改 .env 后重启"
        elif spec.get("runtime_key"):
            if key == "WEB_PORT":
                effective = str(os.getenv("WEB_PORT", "8888") or "8888")
                configured = True
                readonly_text = f"当前进程：{effective}"
            elif key == "WX_BOT_PORT":
                effective = str(os.getenv("WX_BOT_PORT", "5555") or "5555")
                configured = True
                readonly_text = f"当前进程：{effective}"
            elif key == "DATABASE_URL":
                configured = bool(str(os.getenv("DATABASE_URL", "") or "").strip())
                readonly_text = "已由启动环境指定" if configured else "当前使用默认本机数据库"
            elif key == "PLUGINS_DIR":
                configured = True
                readonly_text = "当前目录：app/plugins"
            else:
                configured = True
                readonly_text = "由应用启动配置管理"
        elif not spec.get("editable", False):
            readonly_text = "内部状态，不可手动修改"

        control = spec.get("control") or ("password" if sensitive else "text")
        return {
            "key": key,
            "title": spec.get("title") or _title_for_key(key),
            "description": spec.get("description") or (setting.description if setting else ""),
            "group": spec["group"],
            "section": spec.get("section", "其他"),
            "source": spec.get("source", "本机数据库"),
            "control": control,
            "options": spec.get("options", []),
            "sensitive": sensitive,
            "configured": configured,
            "value": None if sensitive else value,
            "editable": bool(spec.get("editable", False)),
            "requires_restart": bool(spec.get("requires_restart", False)),
            "readonly_text": readonly_text,
            "advanced": bool(spec.get("advanced", False)),
        }

    def get_console(self) -> Dict[str, Any]:
        settings = {item.key: item for item in self.db.query(Setting).order_by(Setting.key).all()}
        grouped: Dict[str, List[Dict[str, Any]]] = {key: [] for key in GROUPS}

        for key, spec in FIELD_SPECS.items():
            setting = settings.get(key)
            if setting is not None or spec.get("always") or spec.get("environment_only") or spec.get("runtime_key"):
                grouped[spec["group"]].append(self._field(key, setting, spec))

        hidden_model_keys = []
        known_keys = set(FIELD_SPECS)
        for key, setting in settings.items():
            if key in known_keys:
                continue
            if setting.category == "model_credentials" or key in MODEL_MANAGED_KEYS or is_model_credential_setting(key):
                hidden_model_keys.append(key)
                continue
            if "MIGRATION" in key.upper() or key in NON_UI_INTERNAL_KEYS:
                continue
            spec = {
                "group": "developer",
                "section": "扩展设置",
                "title": _title_for_key(key),
                "description": setting.description or "供脚本或插件读取的扩展设置；仅在对应文档明确要求时修改。",
                "source": "扩展数据库键",
                "editable": True,
                "advanced": True,
            }
            grouped["developer"].append(self._field(key, setting, spec))

        groups = [
            {"id": group_id, **meta, "fields": grouped[group_id]}
            for group_id, meta in GROUPS.items()
        ]
        return {
            "groups": groups,
            "setting_count": len(settings),
            "identity": self._identity_summary(),
            "handoffs": [
                {
                    "id": "models",
                    "title": "模型供应商与凭据",
                    "description": "在“模型与调用 → 模型连接”中管理模型、API 地址、共享密钥和代理。",
                    "route": "/ai/models",
                    "action": "前往模型连接",
                    "configured_count": sum(bool(settings[key].value) for key in hidden_model_keys if key in settings),
                }
            ],
        }

    def update(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        if len(values) > 100:
            raise SystemSettingsError("一次更新的设置过多")
        existing = {
            setting.key: setting
            for setting in self.db.query(Setting).filter(Setting.key.in_(values)).all()
        }
        before_values = {
            key: (existing[key].value if key in existing else None)
            for key in values
        }

        for key, raw_value in values.items():
            spec = FIELD_SPECS.get(key)
            current = existing.get(key)
            if spec is not None and not spec.get("editable", False):
                raise SystemSettingsError(f"{key} 由 {spec.get('source', '其他页面')} 管理，不能在系统页修改")
            if (current is not None and current.category == "model_credentials") or key in MODEL_MANAGED_KEYS or is_model_credential_setting(key):
                raise SystemSettingsError(f"{key} 已由模型连接页管理")
            if "MIGRATION" in key.upper() or key in NON_UI_INTERNAL_KEYS:
                raise SystemSettingsError(f"{key} 是只读内部状态")
            if spec is None and key not in existing:
                raise SystemSettingsError(f"未知设置项：{key}")
            if not isinstance(raw_value, (str, int, float)) or isinstance(raw_value, bool):
                raise SystemSettingsError(f"{key} 的值无效")
            value = str(raw_value).strip()
            if is_sensitive_setting(key) and value == "********":
                raise SystemSettingsError(f"{key} 不能保存脱敏占位符")
            if key == "WECHAT_BOT_NAME" and not value:
                raise SystemSettingsError("全局备用 @ 名称不能为空")

            setting = existing.get(key)
            if setting is None:
                setting = Setting(key=key, category="system")
                self.db.add(setting)
                existing[key] = setting
            setting.value = value

        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        _clear_setting_caches()
        try:
            from app.services.runtime_operations import get_runtime_operation_service

            get_runtime_operation_service().record_audit(
                category="settings",
                action="update_system_settings",
                target="system_console",
                summary=f"更新 {len(values)} 个系统设置",
                before=before_values,
                after={key: existing[key].value for key in values if key in existing},
                details={"keys": sorted(values)},
            )
        except Exception:
            # Audit persistence must not turn an already committed settings
            # update into a misleading API failure.
            pass
        return self.get_console()
