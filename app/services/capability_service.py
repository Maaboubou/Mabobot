"""Capability catalog and normalized settings descriptors.

The plugin manager is intentionally a runtime-oriented API.  The Web console,
however, needs a product-oriented view that does not leak config file layout,
internal identifiers, or secrets.  This module is the compatibility boundary
between those two concerns.

Existing plugins continue to use their current ``config.json`` files.  New and
migrated plugins can enrich fields with UI metadata, while this service supplies
safe defaults for older manifests.
"""

from __future__ import annotations

import json
import math
import os
import re
import copy
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy.orm import Session

from app.models.chatbot_role import ChatBotRole
from app.models.user_permission import UserPermission


PLUGIN_METADATA_KEYS = {
    "name",
    "display_name",
    "version",
    "description",
    "author",
    "category",
    "requires",
    "config_schema",
    "features",
    "runtime",
    "ui",
}

CATEGORY_META = {
    "ai": {"label": "AI 助手", "icon": "bi-stars", "order": 10},
    "content": {"label": "内容处理", "icon": "bi-file-richtext", "order": 20},
    "language": {"label": "语言工具", "icon": "bi-translate", "order": 30},
    "image": {"label": "图像工具", "icon": "bi-image", "order": 40},
    "finance": {"label": "数据查询", "icon": "bi-graph-up-arrow", "order": 50},
    "logistics": {"label": "业务自动化", "icon": "bi-box-seam", "order": 60},
    "tool": {"label": "实用工具", "icon": "bi-tools", "order": 70},
    "utility": {"label": "实用工具", "icon": "bi-lightning-charge", "order": 70},
}

CORE_ASSISTANT_ID = "assistant"
CORE_ASSISTANT_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "assistant" / "config.json"
)


def normalize_llm_task_descriptors(config: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Expose only the documented, display-safe LLM task metadata."""

    raw_ui = config.get("ui") if isinstance(config.get("ui"), Mapping) else {}
    raw_tasks = raw_ui.get("llm_tasks") if isinstance(raw_ui.get("llm_tasks"), Mapping) else {}
    tasks: Dict[str, Dict[str, Any]] = {}
    for raw_task_id, raw_descriptor in raw_tasks.items():
        task_id = str(raw_task_id or "").strip()
        if not task_id or len(task_id) > 120 or not isinstance(raw_descriptor, Mapping):
            continue

        label = str(raw_descriptor.get("label") or task_id).strip()[:80]
        description = str(raw_descriptor.get("description") or "").strip()[:300]
        category = str(raw_descriptor.get("category") or "模型任务").strip()[:40]
        if not label:
            label = task_id
        if not category:
            category = "模型任务"
        try:
            order = int(raw_descriptor.get("order", 500))
        except (TypeError, ValueError, OverflowError):
            order = 500
        descriptor = {
            "label": label,
            "description": description,
            "category": category,
            "order": max(-10000, min(order, 10000)),
        }
        if raw_descriptor.get("routable") is False:
            descriptor["routable"] = False
        if raw_descriptor.get("advanced") is True:
            descriptor["advanced"] = True
        tasks[task_id] = descriptor
    return tasks

GROUP_META = OrderedDict(
    [
        ("basic", {"title": "基础设置", "description": "最常用的功能与默认行为", "order": 10}),
        ("trigger", {"title": "触发与范围", "description": "触发关键词、聊天范围与执行条件", "order": 20}),
        ("reply", {"title": "回复行为", "description": "回复方式、角色和输出规则", "order": 30}),
        ("model", {"title": "模型与提示词", "description": "模型选择和任务提示词", "order": 40}),
        ("tools", {"title": "工具与理解", "description": "网页搜索、图片理解与其他辅助能力", "order": 45}),
        ("context", {"title": "上下文", "description": "上下文窗口与消息预算", "order": 50}),
        ("schedule", {"title": "计划任务", "description": "定时执行与推送计划", "order": 70}),
        ("media", {"title": "媒体处理", "description": "图片、视频、字幕与输出文件", "order": 80}),
        ("browser", {"title": "浏览器", "description": "浏览器自动化与页面提取", "order": 90}),
        ("network", {"title": "网络与外部服务", "description": "网络访问和第三方服务连接", "order": 100}),
        ("advanced", {"title": "高级设置", "description": "性能、容错与精细调优", "order": 900}),
        ("developer", {"title": "开发者设置", "description": "路径、调试与内部实现参数", "order": 1000}),
    ]
)


ASSISTANT_GROUP_FIELDS = {
    "reply": {"default_role", "allow_mention_trigger"},
    "context": {
        "context_limit",
        "max_context_tokens",
        "context_window_auto_detect",
        "context_safety_margin_tokens",
        "reserved_output_tokens",
        "context_message_fetch_limit",
        "context_window_strategy",
        "anchor_message_count",
        "anchor_rollover_prompt_tokens",
        "recent_context_ratio",
        "ephemeral_context_ratio",
        "ephemeral_context_max_tokens",
    },
    "model": {
        "codex_persistent_session_enabled",
        "codex_reasoning_effort",
        "codex_reasoning_summary",
        "codex_web_search_mode",
        "codex_turn_timeout_seconds",
        "codex_max_turns_per_thread",
        "codex_exec_fallback_enabled",
    },
    "tools": {
        "search_enabled",
        "search_region",
        "search_safesearch",
        "search_max_results",
        "search_fetch_max_pages",
        "search_timeout_seconds",
    },
}

ASSISTANT_BASIC_FIELDS = {
    "default_role",
    "allow_mention_trigger",
    "codex_persistent_session_enabled",
    "codex_reasoning_effort",
    "codex_web_search_mode",
    "context_window_auto_detect",
    "context_window_strategy",
    "search_enabled",
}

SUMMARY_GROUP_FIELDS = {
    "summary": {
        "special_translation_groups",
        "special_translation_target_language",
        "domain_blacklist",
        "sender_blacklist",
        "prompt_summary",
        "mindmap_layout",
    },
    "local_asr": {
        "local_asr_enabled",
        "local_asr_max_duration_minutes",
        "local_asr_timeout_seconds",
        "local_asr_runtime_path",
        "local_asr_model_path",
        "local_asr_vad_path",
    },
    "douyin": {
        "douyin_max_download_duration",
    },
    "bilibili": {
        "prompt_bilibili_mindmap",
        "danmaku_font_size",
        "danmaku_line_spacing",
        "danmaku_display_region_ratio",
        "danmaku_limit_window_seconds",
        "danmaku_max_per_window",
        "bilibili_danmaku_webmask_enabled",
        "bilibili_video_crf",
        "bilibili_max_download_duration",
        "bilibili_burn_danmu",
        "bili_cookie_alert_cooldown_sec",
    },
    "xiaohongshu": {
        "xhs_max_download_duration",
        "xhs_max_images",
    },
    "youtube": {
        "prompt_youtube_mindmap",
        "yt_transcript_proxy",
        "yt_transcript_local_port",
    },
    "media": {
        "ffmpeg_dir",
        "ffmpeg_path",
        "ffprobe_path",
    },
    "browser": {
        "chrome_debug_port",
        "chrome_path",
        "chrome_user_data_dir",
        "chrome_profile_dir",
        "page_load_timeout",
        "webdriver_command_timeout_sec",
    },
}


FIELD_TITLE_OVERRIDES = {
    "enabled_chats": "适用聊天",
    "default_role": "默认角色",
    "trigger_keywords": "触发关键词",
    "trigger_keyword": "触发关键词",
    "trigger_word": "触发词",
    "model_name": "模型",
    "search_enabled": "启用网页搜索",
    "search_region": "DDGS 搜索区域",
    "search_safesearch": "DDGS 安全搜索",
    "search_max_results": "搜索结果上限",
    "search_fetch_max_pages": "深入读取网页数",
    "search_timeout_seconds": "搜索超时",
    "image_enrichment_enabled": "启用图片内容补充",
    "image_understanding_prompt": "图片理解提示词",
    "allow_mention_trigger": "允许群聊 @ 触发",
    "request_timeout": "请求超时",
    "page_load_timeout": "页面加载超时",
    "chrome_path": "Chrome 路径",
    "chrome_debug_port": "Chrome 调试端口",
    "chrome_user_data_dir": "Chrome 用户数据目录",
    "chrome_profile_dir": "Chrome 配置目录",
    "ffmpeg_path": "FFmpeg 路径",
    "ffprobe_path": "FFprobe 路径",
}

LEGACY_SETTINGS_FIELDS = {
    "enabled_chats",
    "proactive_interval_minutes",
    "proactive_msg_threshold",
    "proactive_judge_model",
    "proactive_judge_prompt",
}

SENSITIVE_KEY_PATTERN = re.compile(
    r"(^|_)(api_?key|secret|password|passwd|credential|token|private_?key)(_|$)",
    re.IGNORECASE,
)


class CapabilityConfigError(ValueError):
    """Raised when a capability settings patch violates its manifest."""


def _normalize_config_value(key: str, field: Mapping[str, Any], value: Any) -> Any:
    normalized_type, _ = _normalize_type(field)

    if normalized_type == "boolean":
        if not isinstance(value, bool):
            raise CapabilityConfigError(f"{key} 必须是布尔值")
        normalized = value
    elif normalized_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise CapabilityConfigError(f"{key} 必须是整数")
        normalized = value
    elif normalized_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CapabilityConfigError(f"{key} 必须是数字")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise CapabilityConfigError(f"{key} 必须是有限数字")
    elif normalized_type == "array":
        if not isinstance(value, list):
            raise CapabilityConfigError(f"{key} 必须是列表")
        normalized = value
    elif normalized_type == "object":
        if not isinstance(value, dict):
            raise CapabilityConfigError(f"{key} 必须是对象")
        normalized = value
    else:
        if not isinstance(value, str):
            raise CapabilityConfigError(f"{key} 必须是文本")
        normalized = value

    minimum = field.get("minimum")
    maximum = field.get("maximum")
    if normalized_type in {"integer", "number"}:
        if minimum is not None and normalized < minimum:
            raise CapabilityConfigError(f"{key} 不能小于 {minimum}")
        if maximum is not None and normalized > maximum:
            raise CapabilityConfigError(f"{key} 不能大于 {maximum}")

    options = field.get("enum") or field.get("options") or []
    if options and normalized not in options:
        raise CapabilityConfigError(f"{key} 不是允许的选项")
    return normalized


def validate_settings_patch(config: Mapping[str, Any], values: Mapping[str, Any]) -> Dict[str, Any]:
    schema = config.get("config_schema") or {}
    unknown = sorted(set(values) - set(schema))
    if unknown:
        raise CapabilityConfigError(f"未知设置项：{', '.join(unknown)}")
    return {
        key: _normalize_config_value(key, schema[key], value)
        for key, value in values.items()
    }


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _config_values(config: Mapping[str, Any]) -> Dict[str, Any]:
    nested = config.get("config")
    if isinstance(nested, dict):
        return dict(nested)
    schema = config.get("config_schema") or {}
    return {key: config[key] for key in schema if key in config}


def _is_sensitive(key: str, field: Mapping[str, Any]) -> bool:
    explicit = field.get("sensitive")
    if explicit is not None:
        return bool(explicit)
    if key.lower().endswith("_env"):
        return False
    return bool(SENSITIVE_KEY_PATTERN.search(key))


def _derive_title(key: str, field: Mapping[str, Any]) -> str:
    if field.get("title"):
        return str(field["title"])
    if key in FIELD_TITLE_OVERRIDES:
        return FIELD_TITLE_OVERRIDES[key]

    description = str(field.get("description") or "").strip()
    if description:
        candidate = re.split(r"[。；;，,（(]", description, maxsplit=1)[0].strip()
        candidate = re.sub(r"^(是否|用于|设置|配置)", "", candidate).strip()
        if 2 <= len(candidate) <= 32:
            return candidate

    words = [part for part in key.replace("-", "_").split("_") if part]
    return " ".join(word.upper() if len(word) <= 4 else word.title() for word in words)


def _normalize_type(field: Mapping[str, Any]) -> tuple[str, str]:
    raw_type = str(field.get("type") or "string").lower()
    if raw_type in {"text", "textarea"}:
        return "string", "textarea"
    if raw_type == "select":
        return "string", "select"
    if raw_type == "object":
        return "object", "json"
    if raw_type == "array":
        return "array", "list"
    if raw_type == "boolean":
        return "boolean", "switch"
    if raw_type in {"integer", "number"}:
        return raw_type, "number"
    if field.get("enum") or field.get("options"):
        return "string", "select"
    return "string", "text"


def _infer_group(plugin_id: str, key: str, field: Mapping[str, Any]) -> str:
    declared = field.get("group") or field.get("x-group")
    if declared:
        return str(declared)

    if plugin_id == CORE_ASSISTANT_ID:
        for group_id, keys in ASSISTANT_GROUP_FIELDS.items():
            if key in keys:
                return group_id
    elif plugin_id == "summary_plus":
        for group_id, keys in SUMMARY_GROUP_FIELDS.items():
            if key in keys:
                return group_id

    lowered = key.lower()
    if lowered == "enabled_chats" or any(token in lowered for token in ("trigger", "keyword", "sender_blacklist", "domain_blacklist")):
        return "trigger"
    if any(token in lowered for token in ("push_time", "push_weekday", "schedule", "interval")):
        return "schedule"
    if any(token in lowered for token in ("prompt", "model", "temperature", "max_completion_tokens")):
        return "model"
    if any(token in lowered for token in ("chrome", "browser", "webdriver")):
        return "browser"
    if any(token in lowered for token in ("ffmpeg", "ffprobe", "image", "video", "danmaku", "screenshot", "pdf")):
        return "media"
    if any(token in lowered for token in ("api_base", "proxy", "url", "endpoint", "zone", "country")):
        return "network"
    if any(token in lowered for token in ("path", "dir", "debug", "concurrency", "lock", "stale")):
        return "developer"
    if any(token in lowered for token in ("timeout", "retry", "threshold", "limit", "max_", "min_", "cache")):
        return "advanced"
    return "basic"


def _infer_level(
    plugin_id: str,
    key: str,
    group_id: str,
    field: Mapping[str, Any],
) -> str:
    declared = field.get("level") or field.get("x-level")
    if declared in {"basic", "advanced", "developer"}:
        return str(declared)
    if plugin_id == CORE_ASSISTANT_ID:
        return "basic" if key in ASSISTANT_BASIC_FIELDS else "advanced"
    if group_id == "developer":
        return "developer"
    if group_id in {"advanced", "browser", "network"}:
        return "advanced"
    return "basic"


def normalize_settings_descriptor(plugin_id: str, config: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return grouped, typed and redacted settings for one plugin."""

    schema = config.get("config_schema") or {}
    values = _config_values(config)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    raw_ui = config.get("ui") if isinstance(config.get("ui"), Mapping) else {}
    custom_group_meta = (
        raw_ui.get("settings_groups")
        if isinstance(raw_ui.get("settings_groups"), Mapping)
        else {}
    )

    for key, raw_field in schema.items():
        field = raw_field if isinstance(raw_field, dict) else {}
        if field.get("level") == "hidden":
            continue
        normalized_type, control = _normalize_type(field)
        group_id = _infer_group(plugin_id, key, field)
        level = _infer_level(plugin_id, key, group_id, field)
        sensitive = _is_sensitive(key, field)
        is_object = normalized_type == "object"
        value = values.get(key, field.get("default"))
        if (
            normalized_type == "string"
            and control == "text"
            and "prompt" in key.lower()
        ):
            control = "textarea"
        options = field.get("enum") or field.get("options") or []
        option_labels = field.get("enum_labels") or {}
        deprecated = key in LEGACY_SETTINGS_FIELDS

        descriptor = {
            "key": key,
            "title": _derive_title(key, field),
            "description": str(field.get("description") or ""),
            "type": normalized_type,
            "control": control,
            "group": group_id,
            "level": level,
            "scope": str(field.get("scope") or "global"),
            "sensitive": sensitive,
            "configured": value not in (None, "", [], {}),
            "editable": not is_object,
            "deprecated": deprecated,
            "deprecation_message": "该字段仅用于历史数据迁移，不再参与运行。" if deprecated else "",
            "value": None if sensitive or is_object else value,
            "default": None if sensitive else field.get("default"),
            "minimum": field.get("minimum"),
            "maximum": field.get("maximum"),
            "step": field.get("step"),
            "options": [
                {"value": option, "label": option_labels.get(str(option), str(option))}
                for option in options
            ],
            "placeholder": field.get("placeholder"),
            "unit": field.get("unit"),
            "requires_restart": bool(field.get("requires_restart", False)),
        }
        grouped.setdefault(group_id, []).append(descriptor)

    result = []
    for group_id, fields in grouped.items():
        default_meta = GROUP_META.get(
            group_id,
            {"title": group_id, "description": "", "order": 500},
        )
        declared_meta = custom_group_meta.get(group_id)
        if not isinstance(declared_meta, Mapping):
            declared_meta = {}
        try:
            group_order = int(declared_meta.get("order", default_meta["order"]))
        except (TypeError, ValueError):
            group_order = int(default_meta["order"])
        meta = {
            "title": str(declared_meta.get("title") or default_meta["title"]),
            "description": str(
                declared_meta.get("description") or default_meta["description"]
            ),
            "order": group_order,
        }
        result.append(
            {
                "id": group_id,
                "title": meta["title"],
                "description": meta["description"],
                "order": meta["order"],
                "fields": fields,
            }
        )
    return sorted(result, key=lambda group: (group["order"], group["title"]))


class CapabilityService:
    """Read-only product view over the current plugin runtime and settings."""

    def __init__(self, plugin_manager: Any, db: Optional[Session] = None):
        self.plugin_manager = plugin_manager
        self.db = db

    def _component(self, component_id: str) -> Optional[Any]:
        plugin = self.plugin_manager.get_plugin_info(component_id)
        if plugin is not None or component_id != CORE_ASSISTANT_ID:
            return plugin
        try:
            config = json.loads(CORE_ASSISTANT_CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return None
        return SimpleNamespace(
            name=CORE_ASSISTANT_ID,
            version=str(config.get("version") or ""),
            description=str(config.get("description") or ""),
            author=str(config.get("author") or "System"),
            path=str(CORE_ASSISTANT_CONFIG_PATH.parent),
            enabled=True,
            loaded=False,
            listener_ids=[],
            config=config,
            kind="core",
        )

    def _assignment_counts(self) -> Dict[str, Dict[str, int]]:
        if self.db is None:
            return {}
        rows = self.db.query(UserPermission.plugin_name).all()
        counts: Dict[str, Dict[str, int]] = {}
        for (permission_name,) in rows:
            is_push = permission_name.endswith("#push")
            plugin_id = permission_name[:-5] if is_push else permission_name
            item = counts.setdefault(plugin_id, {"chats": 0, "push_chats": 0})
            item["push_chats" if is_push else "chats"] += 1
        return counts

    def _capability_item(self, plugin_id: str, plugin: Any, counts: Mapping[str, Dict[str, int]]) -> Dict[str, Any]:
        config = plugin.config or {}
        raw_ui = config.get("ui") if isinstance(config.get("ui"), Mapping) else {}
        category = str(config.get("category") or "utility")
        category_meta = CATEGORY_META.get(
            category,
            {"label": "其他能力", "icon": "bi-grid", "order": 500},
        )
        assignments = counts.get(plugin_id, {"chats": 0, "push_chats": 0})
        enabled = bool(plugin.enabled)
        loaded = bool(plugin.loaded)
        listener_count = len(plugin.listener_ids or [])
        if getattr(plugin, "kind", "plugin") == "core":
            try:
                from app.assistant.runtime import get_assistant_runtime

                core_status = get_assistant_runtime().status()
                enabled = True
                loaded = bool(core_status.get("ready"))
                listener_count = int(core_status.get("listener_count") or 0)
            except Exception:
                loaded = False
        if enabled and loaded:
            status = "running"
        elif not enabled:
            status = "disabled"
        else:
            status = "error"

        schema = config.get("config_schema") or {}
        return {
            "id": plugin_id,
            "display_name": str(config.get("display_name") or plugin.name or plugin_id),
            "internal_name": plugin.name or plugin_id,
            "description": plugin.description or str(config.get("description") or ""),
            "version": plugin.version,
            "author": plugin.author,
            "category": category,
            "category_label": category_meta["label"],
            "category_order": category_meta["order"],
            "icon": str(raw_ui.get("icon") or category_meta["icon"]),
            "management_url": (
                raw_ui.get("management_url")
                if isinstance(raw_ui.get("management_url"), str)
                and raw_ui["management_url"].startswith("/api/")
                and not any(c in raw_ui["management_url"] for c in "\\\r\n\t\"<>")
                else None
            ),
            "llm_tasks": normalize_llm_task_descriptors(config),
            "featured": False,
            "system": plugin_id.startswith("builtin_"),
            "enabled": enabled,
            "loaded": loaded,
            "status": status,
            "listener_count": listener_count,
            "settings_count": len(schema),
            "configurable": bool(schema),
            "features": list(config.get("features") or []),
            "assigned_chat_count": assignments["chats"],
            "push_chat_count": assignments["push_chats"],
        }

    def list_capabilities(self) -> List[Dict[str, Any]]:
        counts = self._assignment_counts()
        items = [
            self._capability_item(plugin_id, plugin, counts)
            for plugin_id, plugin in self.plugin_manager.plugins.items()
            if getattr(plugin, "kind", "plugin") == "plugin"
        ]
        return sorted(
            items,
            key=lambda item: (
                0 if item["featured"] else 1,
                item["category_order"],
                item["display_name"].casefold(),
            ),
        )

    def get_capability(self, plugin_id: str) -> Optional[Dict[str, Any]]:
        plugin = self._component(plugin_id)
        if plugin is None:
            return None
        return self._capability_item(plugin_id, plugin, self._assignment_counts())

    def get_settings(self, plugin_id: str) -> Optional[Dict[str, Any]]:
        plugin = self._component(plugin_id)
        if plugin is None:
            return None
        groups = normalize_settings_descriptor(plugin_id, plugin.config or {})
        # Relational choices belong to the console contract rather than static
        # plugin manifests. Expose them as safe select options so users do not
        # have to type internal role identifiers by hand.
        if self.db is not None and plugin_id == CORE_ASSISTANT_ID:
            role_options = [
                {"value": role.name, "label": role.display_name}
                for role in self.db.query(ChatBotRole).order_by(ChatBotRole.id).all()
            ]
            for group in groups:
                for field in group["fields"]:
                    if field["key"] == "default_role":
                        field["control"] = "select"
                        field["options"] = role_options
        return {
            "capability_id": plugin_id,
            "scope": "global",
            "notice": str(
                (
                    (plugin.config or {}).get("ui")
                    if isinstance((plugin.config or {}).get("ui"), Mapping)
                    else {}
                ).get("settings_notice")
                or "这里设置该能力对所有聊天的默认行为。"
            ),
            "groups": groups,
            "field_count": sum(
                1
                for group in groups
                for field in group["fields"]
                if not field["deprecated"]
            ),
        }

    def update_settings(self, plugin_id: str, values: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Validate and atomically apply a partial global settings patch."""

        plugin = self._component(plugin_id)
        if plugin is None:
            return None

        config_path = Path(plugin.path) / "config.json"
        original_bytes = config_path.read_bytes()
        try:
            config = json.loads(original_bytes.decode("utf-8-sig"))
        except Exception as exc:
            raise CapabilityConfigError("插件配置文件不是有效 JSON") from exc

        validated = validate_settings_patch(config, values)
        source_values = config.get("config") if isinstance(config.get("config"), dict) else config
        previous_values = {key: source_values.get(key) for key in validated}
        if isinstance(config.get("config"), dict):
            config["config"].update(validated)
        else:
            config.update(validated)

        is_core_component = getattr(plugin, "kind", "plugin") == "core"
        should_reload = bool(plugin.loaded and plugin.enabled)
        _atomic_write_json(config_path, config)
        try:
            if is_core_component:
                plugin.config = config
                from app.assistant.runtime import get_assistant_runtime

                runtime = get_assistant_runtime()
                if runtime.handler is not None and not runtime.restart():
                    raise RuntimeError("核心助手重新加载失败")
            elif should_reload:
                if not self.plugin_manager.reload_plugin(plugin_id):
                    raise RuntimeError("插件重新加载失败")
            else:
                plugin.config = config
        except Exception as exc:
            rollback_payload = json.loads(original_bytes.decode("utf-8-sig"))
            _atomic_write_json(config_path, rollback_payload)
            # reload_plugin may remove the registry entry before failing.
            restored = self._component(plugin_id)
            if is_core_component:
                if restored is not None:
                    restored.config = rollback_payload
                try:
                    from app.assistant.runtime import get_assistant_runtime

                    runtime = get_assistant_runtime()
                    if runtime.handler is not None:
                        runtime.restart()
                except Exception:
                    pass
            elif should_reload:
                if restored is None:
                    self.plugin_manager.load_plugin(plugin_id)
                else:
                    self.plugin_manager.reload_plugin(plugin_id)
            elif restored is not None:
                restored.config = rollback_payload
            try:
                from app.services.runtime_operations import get_runtime_operation_service

                get_runtime_operation_service().record_audit(
                    category="plugin_settings",
                    action="update_plugin_settings",
                    target=plugin_id,
                    status="failed",
                    summary="插件设置更新失败，已恢复原配置",
                    before=previous_values,
                    after=validated,
                    details={"keys": sorted(validated), "error": str(exc)},
                )
            except Exception:
                pass
            raise

        try:
            from app.services.runtime_operations import get_runtime_operation_service

            get_runtime_operation_service().record_audit(
                category="plugin_settings",
                action="update_plugin_settings",
                target=plugin_id,
                summary=f"更新 {len(validated)} 个插件设置",
                before=previous_values,
                after=validated,
                details={"keys": sorted(validated), "reloaded": should_reload},
            )
        except Exception:
            pass

        return self.get_settings(plugin_id)


def public_plugin_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    """Return manifest metadata without raw business configuration values."""
    result = {
        key: copy.deepcopy(value)
        for key, value in config.items()
        if key in PLUGIN_METADATA_KEYS and key not in {"runtime"}
    }
    schema = result.get("config_schema")
    if isinstance(schema, dict):
        for key, raw_field in schema.items():
            if isinstance(raw_field, dict) and _is_sensitive(key, raw_field):
                raw_field.pop("default", None)
                raw_field["sensitive"] = True
    return result
