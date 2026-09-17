#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
插件配置工具模块
提供统一的插件配置读取功能，让插件优先使用自己的 config.json 配置
"""

import json
import logging
from pathlib import Path
from typing import Any, Optional, Dict

logger = logging.getLogger(__name__)
ASSISTANT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "assistant" / "config.json"


def get_plugin_config(plugin_name: str, plugin_path: Optional[str] = None) -> Dict[str, Any]:
    """
    读取插件的配置文件

    Args:
        plugin_name: 插件名称
        plugin_path: 插件路径，如果不提供则自动推断

    Returns:
        插件配置字典，如果读取失败返回空字典
    """
    try:
        if plugin_path:
            config_file = Path(plugin_path) / "config.json"
        elif plugin_name in {"assistant", "builtin_chatbot"}:
            # ``builtin_chatbot`` is accepted only while historical callers
            # and persisted settings are migrated to the first-class domain.
            config_file = ASSISTANT_CONFIG_PATH
        else:
            # 自动推断插件路径
            # 将插件名称中的斜杠转换为路径分隔符
            plugin_path_parts = plugin_name.split('/')
            config_file = Path(__file__).parent.parent / "plugins" / Path(*plugin_path_parts) / "config.json"

        if not config_file.exists():
            logger.warning(f"Plugin config file not found: {config_file}")
            return {}

        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
            return config
    except Exception as e:
        logger.error(f"Failed to load plugin config for '{plugin_name}': {e}")
        return {}


def get_plugin_setting(plugin_name: str, key: str, default: Any = None, plugin_path: Optional[str] = None) -> Any:
    """
    从插件配置中获取特定设置值

    Args:
        plugin_name: 插件名称
        key: 配置键名
        default: 默认值
        plugin_path: 插件路径，如果不提供则自动推断

    Returns:
        配置值或默认值
    """
    from app.services.plugin_config_context import current_config_scope

    scope = current_config_scope()
    if scope is not None and plugin_name in scope.snapshots:
        from copy import deepcopy

        return deepcopy(scope.snapshots[plugin_name].get(key, default))
    config = get_plugin_config(plugin_name, plugin_path)
    if scope is not None:
        from copy import deepcopy

        return deepcopy(scope.values(plugin_name, config, plugin_path).get(key, default))

    # 1. 优先从根目录获取 (Root level)
    if key in config:
        return config[key]

    # 2. 尝试从 nested 'config' 字典获取 (Frontend often saves here)
    if "config" in config and isinstance(config["config"], dict) and key in config["config"]:
        return config["config"][key]

    # 3. 从 config_schema 中获取默认值
    if "config_schema" in config and key in config["config_schema"]:
        schema_item = config["config_schema"][key]
        return schema_item.get("default", default)

    # 4. 返回提供的默认值
    return default


def get_current_plugin_name() -> Optional[str]:
    """
    从调用栈中自动推断当前插件名称

    Returns:
        插件名称或None
    """
    import inspect

    try:
        # 获取调用栈
        frame = inspect.currentframe()
        while frame:
            frame = frame.f_back
            if frame and frame.f_code.co_filename:
                file_path = Path(frame.f_code.co_filename)

                # 检查是否在插件目录中
                if "plugins" in file_path.parts:
                    plugins_index = file_path.parts.index("plugins")
                    if plugins_index + 1 < len(file_path.parts):
                        # 构建完整的插件路径（支持多级目录）
                        plugin_parts = []
                        for i in range(plugins_index + 1, len(file_path.parts)):
                            part = file_path.parts[i]
                            # 如果遇到 config.json、__pycache__ 或任何 .py 文件，停止
                            # 注意：如果是单文件插件，如 plugins/my_plugin.py，此时 plugin_parts 为空，返回 None
                            if part.endswith(".py") or part in ["config.json", "__pycache__"]:
                                break
                            plugin_parts.append(part)

                        if plugin_parts:
                            # 使用正斜杠连接，确保跨平台兼容
                            return "/".join(plugin_parts)

        return None
    except Exception as e:
        logger.warning(f"Failed to auto-detect plugin name: {e}")
        return None


def get_config(key: str, default: Any = None, plugin_name: Optional[str] = None) -> Any:
    """
    便捷函数：自动推断插件名称并获取配置

    Args:
        key: 配置键名
        default: 默认值
        plugin_name: 插件名称，如果不提供则自动推断

    Returns:
        配置值或默认值
    """
    if not plugin_name:
        plugin_name = get_current_plugin_name()

    if not plugin_name:
        logger.warning(f"Cannot auto-detect plugin name for config key '{key}', using default value")
        return default

    return get_plugin_setting(plugin_name, key, default)
