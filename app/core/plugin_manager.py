"""
插件管理器 - 负责插件的加载、卸载、热重载
"""

import json
import hashlib
import importlib
import importlib.util
import inspect
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import time
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from .event_bus import EventBus, EventType, Event, get_event_bus
from .plugin_manifest import (
    PluginManifestError,
    listener_manifest_map,
    load_plugin_manifest,
    validate_registered_listeners,
)
from .routing_order import RoutingOrderStore
from app.services.plugin_runtime import PluginContext, get_plugin_runtime_registry


@dataclass
class PluginInfo:
    """插件信息"""
    name: str
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    path: str = ""
    enabled: bool = True
    loaded: bool = False
    module: Optional[Any] = None
    config: Dict[str, Any] = None
    listener_ids: List[str] = None
    manifest: Dict[str, Any] = None
    runtime_context: Optional[PluginContext] = None
    last_error: str = ""
    last_loaded_at: Optional[float] = None
    last_failed_at: Optional[float] = None
    last_unloaded_at: Optional[float] = None
    source_fingerprint: str = ""
    kind: str = "plugin"

    def __post_init__(self):
        if self.config is None:
            self.config = {}
        if self.listener_ids is None:
            self.listener_ids = []
        if self.manifest is None:
            self.manifest = {}


def purge_plugin_package_modules(package_name: str) -> List[str]:
    """清除插件包与所有子模块，返回被清除的模块名。"""

    package_module = sys.modules.get(package_name)
    cached_module_names = [
        module_name
        for module_name in list(sys.modules)
        if module_name == package_name or module_name.startswith(package_name + ".")
    ]
    for module_name in sorted(cached_module_names, reverse=True):
        sys.modules.pop(module_name, None)

    parent_name, _, child_name = package_name.rpartition(".")
    parent_module = sys.modules.get(parent_name)
    if (
        package_module is not None
        and parent_module is not None
        and getattr(parent_module, child_name, None) is package_module
    ):
        delattr(parent_module, child_name)

    return cached_module_names


class PluginFileHandler(FileSystemEventHandler):
    """插件文件变化监听器"""

    def __init__(self, plugin_manager: 'PluginManager'):
        self.plugin_manager = plugin_manager
        self.logger = logging.getLogger(__name__)
        self._debounce_seconds = 0.75
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()

    def _queue_source_event(self, file_path: Path, event_type: str) -> None:
        if file_path.suffix.lower() != ".py":
            return

        plugin_name = self.plugin_manager.plugin_for_source_path(file_path)
        if plugin_name is None:
            return

        with self.plugin_manager._lock:
            plugin_info = self.plugin_manager.plugins.get(plugin_name)
            if plugin_info is None or not plugin_info.enabled or not plugin_info.loaded:
                return

        with self._pending_lock:
            previous = self._pending.get(plugin_name)
            generation = int((previous or {}).get("generation") or 0) + 1
            events = list((previous or {}).get("events") or [])
            event_label = f"{event_type}:{file_path}"
            if event_label not in events:
                events.append(event_label)
            if previous is not None:
                previous["timer"].cancel()
            timer = threading.Timer(
                self._debounce_seconds,
                self._check_plugin_content,
                args=(plugin_name, generation),
            )
            timer.daemon = True
            self._pending[plugin_name] = {
                "generation": generation,
                "events": events[-10:],
                "timer": timer,
            }
            timer.start()

    def _check_plugin_content(self, plugin_name: str, generation: int) -> None:
        with self._pending_lock:
            pending = self._pending.get(plugin_name)
            if pending is None or pending["generation"] != generation:
                return
            events = list(pending["events"])
            self._pending.pop(plugin_name, None)

        new_fingerprint = self.plugin_manager.plugin_source_fingerprint(plugin_name)
        with self.plugin_manager._lock:
            plugin_info = self.plugin_manager.plugins.get(plugin_name)
            if plugin_info is None or not plugin_info.enabled or not plugin_info.loaded:
                return
            old_fingerprint = plugin_info.source_fingerprint

        event_summary = ", ".join(events)
        if new_fingerprint is None:
            self.logger.warning(
                "Unable to fingerprint plugin '%s' after source event(s): %s",
                plugin_name,
                event_summary,
            )
            return
        if new_fingerprint == old_fingerprint:
            self.logger.info(
                "Ignored unchanged plugin source event: plugin=%s events=%s fingerprint=%s",
                plugin_name,
                event_summary,
                new_fingerprint[:12],
            )
            return

        self.logger.info(
            "Detected plugin source content change: plugin=%s events=%s old=%s new=%s",
            plugin_name,
            event_summary,
            old_fingerprint[:12] or "none",
            new_fingerprint[:12],
        )
        self.logger.debug(
            "Plugin source fingerprint details: plugin=%s old=%s new=%s",
            plugin_name,
            old_fingerprint or "none",
            new_fingerprint,
        )
        self.plugin_manager.request_hot_reload(plugin_name)

    def on_modified(self, event):
        if event.is_directory:
            return
        self._queue_source_event(Path(event.src_path), "modified")

    def on_created(self, event):
        if event.is_directory:
            # 新增插件目录
            plugin_path = Path(event.src_path)
            config_file = plugin_path / "config.json"
            if config_file.exists():
                self.logger.info(f"Detected new plugin directory: {plugin_path.name}")
                threading.Thread(
                    target=self.plugin_manager._discover_and_load_new_plugin,
                    args=(str(plugin_path),),
                    daemon=True
                ).start()
            return
        self._queue_source_event(Path(event.src_path), "created")

    def on_deleted(self, event):
        if event.is_directory:
            # 插件目录被删除
            plugin_path = Path(event.src_path)
            plugin_name = plugin_path.name
            if plugin_name in self.plugin_manager.plugins:
                self.logger.info(f"Plugin directory deleted: {plugin_name}")
                threading.Thread(
                    target=self.plugin_manager.unload_plugin,
                    args=(plugin_name,),
                    daemon=True
                ).start()
            return
        self._queue_source_event(Path(event.src_path), "deleted")

    def on_moved(self, event):
        if event.is_directory:
            return
        self._queue_source_event(Path(event.src_path), "moved_from")
        self._queue_source_event(Path(event.dest_path), "moved_to")


class PluginManager:
    """插件管理器核心类"""

    def __init__(self, plugins_dir: str = "app/plugins", event_bus: Optional[EventBus] = None):
        self.plugins_dir = Path(plugins_dir)
        self.event_bus = event_bus or get_event_bus()
        self.logger = logging.getLogger(__name__)

        self.plugins: Dict[str, PluginInfo] = {}
        self._lock = threading.RLock()
        self._hot_reload_lock = threading.Lock()
        self._pending_hot_reloads: set[str] = set()
        self._hot_reload_poll_interval = 1.0
        self._hot_reload_max_wait = 30 * 60.0
        self.routing_order = RoutingOrderStore(self.plugins_dir / "routing_order.json")
        self.runtime_registry = get_plugin_runtime_registry()

        # 文件监控
        self._observer = Observer()
        self._file_handler = PluginFileHandler(self)

        # 确保插件目录存在
        self.plugins_dir.mkdir(parents=True, exist_ok=True)

    def start_monitoring(self):
        """启动文件监控"""
        try:
            self._observer.schedule(self._file_handler, str(self.plugins_dir), recursive=True)
            self._observer.start()
            self.logger.debug(f"Started monitoring plugin directory: {self.plugins_dir}")
        except Exception as e:
            self.logger.error(f"Failed to start file monitoring: {e}")

    def stop_monitoring(self):
        """停止文件监控"""
        try:
            self._observer.stop()
            self._observer.join()
            self.logger.info("Stopped plugin file monitoring")
        except Exception as e:
            self.logger.error(f"Error stopping file monitoring: {e}")

    @staticmethod
    def _source_fingerprint(plugin_path: Path) -> Optional[str]:
        """Hash Python source paths and contents for stable hot-reload decisions."""
        try:
            source_files = sorted(
                (path for path in plugin_path.rglob("*.py") if path.is_file()),
                key=lambda path: path.relative_to(plugin_path).as_posix(),
            )
            digest = hashlib.sha256()
            for source_path in source_files:
                relative = source_path.relative_to(plugin_path).as_posix().encode("utf-8")
                digest.update(relative)
                digest.update(b"\0")
                digest.update(source_path.read_bytes())
                digest.update(b"\0")
            return digest.hexdigest()
        except OSError:
            return None

    def plugin_source_fingerprint(self, plugin_name: str) -> Optional[str]:
        with self._lock:
            plugin_info = self.plugins.get(plugin_name)
            if plugin_info is None:
                return None
            plugin_path = Path(plugin_info.path)
        return self._source_fingerprint(plugin_path)

    def plugin_for_source_path(self, source_path: Path) -> Optional[str]:
        candidate = source_path.resolve()
        with self._lock:
            plugin_paths = [(name, info.path) for name, info in self.plugins.items()]
        # resolve() performs filesystem I/O and releases the GIL. Do not keep
        # a live registry iterator across it while startup discovers plugins.
        plugins = [(name, Path(path).resolve()) for name, path in plugin_paths]
        for plugin_name, plugin_path in sorted(plugins, key=lambda item: len(item[1].parts), reverse=True):
            if candidate == plugin_path or plugin_path in candidate.parents:
                return plugin_name
        return None

    def discover_plugins(self):
        """发现所有插件并预加载信息（支持递归子目录）"""
        self.logger.debug("Discovering plugins...")
        if not self.plugins_dir.exists():
            self.logger.warning(f"Plugins directory not found: {self.plugins_dir}")
            return

        # 递归查找包含 config.json 与 main.py 的目录
        for config_path in self.plugins_dir.rglob("config.json"):
            plugin_dir = config_path.parent
            if plugin_dir.name.startswith('_'):
                continue
            main_file = plugin_dir / "main.py"
            if not main_file.exists():
                continue

            # 以相对路径作为键，允许多级目录，如 "feishu/feishu_fetch_demo"
            rel_path = plugin_dir.relative_to(self.plugins_dir)
            plugin_key = str(rel_path).replace('\\', '/')

            if plugin_key in self.plugins:
                continue

            # 独立加载配置文件（直接从路径读取）
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            except Exception as e:
                self.logger.error(f"Failed to load config for plugin '{plugin_key}': {e}")
                continue

            if str(config.get("component_kind") or "plugin").strip().lower() == "core":
                self.logger.debug(
                    "Skipping application-owned core component during plugin discovery: %s",
                    plugin_key,
                )
                continue

            plugin_info = PluginInfo(
                name=config.get('name', plugin_dir.name),
                version=config.get('version', '1.0.0'),
                description=config.get('description', ''),
                author=config.get('author', ''),
                path=str(plugin_dir),
                enabled=bool((config.get('runtime') or {}).get('enabled', True)),
                config=config,
                kind="plugin",
            )
            with self._lock:
                self.plugins[plugin_key] = plugin_info
            self.logger.debug(f"Found plugin: {plugin_key}")

        self.logger.debug(f"Discovery complete. Found {len(self.plugins)} total plugins.")

    def load_plugin_config(self, plugin_name: str) -> Optional[Dict[str, Any]]:
        """加载插件配置"""
        # First try to get plugin info to get the actual path
        plugin_info = self.plugins.get(plugin_name)

        if plugin_info and plugin_info.path:
            # Use the actual plugin path
            config_file = Path(plugin_info.path) / "config.json"
        else:
            # Fallback to using plugin_name directly
            config_file = self.plugins_dir / plugin_name / "config.json"

        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                return config
        except Exception as e:
            self.logger.error(f"Failed to load config for plugin '{plugin_name}': {e}")
            return None

    def load_plugin(self, plugin_name: str) -> bool:
        """加载插件"""
        with self._lock:
            existing_info = self.plugins.get(plugin_name)
            if existing_info is not None and existing_info.kind != "plugin":
                self.logger.warning(
                    "Core component '%s' is managed by the application lifecycle, not PluginManager",
                    plugin_name,
                )
                return False
            if plugin_name in self.plugins and self.plugins[plugin_name].loaded:
                self.logger.warning(f"Plugin '{plugin_name}' is already loaded")
                return True

            # 支持多级目录：优先使用已发现的插件信息中的绝对路径
            if plugin_name in self.plugins:
                plugin_path = Path(self.plugins[plugin_name].path)
            else:
                plugin_path = self.plugins_dir / plugin_name
            if not plugin_path.exists():
                self.logger.error(f"Plugin directory not found: {plugin_path}")
                return False

            # 加载配置
            config = self.load_plugin_config(plugin_name)
            if not config:
                return False

            try:
                # 动态导入插件模块
                manifest = load_plugin_manifest(plugin_path, config)
                manifest_by_listener = listener_manifest_map(manifest)
                # 计算模块名：plugins.<相对路径用点分隔>.main
                rel_path = plugin_path.relative_to(self.plugins_dir)
                rel_module_path = str(rel_path).replace('\\', '.').replace('/', '.')
                # 使用完整的包路径 app.plugins... 以匹配API中的导入
                module_name = "app.plugins." + rel_module_path + ".main"
                spec = importlib.util.spec_from_file_location(
                    module_name,
                    plugin_path / "main.py"
                )
                if spec is None or spec.loader is None:
                    self.logger.error(f"Failed to create module spec for plugin '{plugin_name}'")
                    return False

                module = importlib.util.module_from_spec(spec)
                import sys
                sys.modules[module_name] = module
                spec.loader.exec_module(module)

                # 检查插件是否有注册函数
                if not hasattr(module, 'register'):
                    self.logger.error(f"Plugin '{plugin_name}' missing 'register' function")
                    return False


                # 获取或创建插件信息
                plugin_info = self.plugins.get(plugin_name)
                if not plugin_info:
                    # 插件信息不存在,创建新的(这可能发生在重新加载时)
                    plugin_info = PluginInfo(
                        name=plugin_name,
                        version=config.get("version", "1.0.0"),
                        description=config.get("description", ""),
                        author=config.get("author", ""),
                        path=str(plugin_path),
                        enabled=bool((config.get("runtime") or {}).get("enabled", True)),
                        config=config
                    )
                    self.plugins[plugin_name] = plugin_info
                else:
                    # 插件信息已存在,更新配置和元数据
                    plugin_info.version = config.get("version", "1.0.0")
                    plugin_info.description = config.get("description", "")
                    plugin_info.author = config.get("author", "")
                    plugin_info.config = config  # 更新配置为最新加载的数据

                plugin_info.module = module
                plugin_info.loaded = True
                plugin_info.manifest = manifest
                plugin_info.runtime_context = self.runtime_registry.register(
                    plugin_name, manifest, plugin_path
                )


                # 注册插件
                listener_ids = []
                listener_key_counts: Dict[str, int] = {}
                registered_identities = []

                def subscribe_wrapper(
                    event_type,
                    handler,
                    listener_key: Optional[str] = None,
                ):
                    event_value = event_type.value if isinstance(event_type, EventType) else str(event_type)
                    handler_name = getattr(handler, "__name__", handler.__class__.__name__)
                    base_key = listener_key or f"{event_value}:{handler_name}"
                    duplicate_index = listener_key_counts.get(base_key, 0)
                    listener_key_counts[base_key] = duplicate_index + 1
                    local_key = base_key if duplicate_index == 0 else f"{base_key}#{duplicate_index + 1}"
                    stable_key = (
                        local_key
                        if local_key.startswith(f"{plugin_name}:")
                        else f"{plugin_name}:{local_key}"
                    )

                    identity = (event_value, handler_name)
                    listener_spec = manifest_by_listener.get(identity)
                    if listener_spec is None:
                        raise PluginManifestError(
                            f"监听器 {event_value}:{handler_name} 未在 manifest.json 声明"
                        )
                    registered_identities.append(identity)
                    propagation = listener_spec["propagation"]
                    order_index = self.routing_order.index_for(event_value, stable_key)

                    listener_id = self.event_bus.subscribe(
                        event_type=event_type,
                        handler=handler,
                        plugin_name=plugin_name,
                        order_index=order_index,
                        propagation=propagation,
                        listener_key=stable_key,
                        handler_name=handler_name,
                        order_source="routing_order",
                        trigger_spec=listener_spec,
                    )
                    listener_ids.append(listener_id)
                    return listener_id

                # Every plugin is a Runtime API v2 plugin and must receive its
                # owned PluginContext. There is deliberately no legacy branch.
                register_signature = inspect.signature(module.register)
                positional = [
                    parameter
                    for parameter in register_signature.parameters.values()
                    if parameter.kind in {
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    }
                ]
                accepts_context = (
                    len(positional) >= 3
                    or "context" in register_signature.parameters
                    or any(
                        parameter.kind == inspect.Parameter.VAR_POSITIONAL
                        for parameter in register_signature.parameters.values()
                    )
                )
                if not accepts_context:
                    raise PluginManifestError(
                        "plugin_api_version=2 的 register 必须接收第三个 PluginContext 参数"
                    )
                module.register(
                    self.event_bus,
                    subscribe_wrapper,
                    plugin_info.runtime_context,
                )
                validate_registered_listeners(manifest, registered_identities)
                plugin_info.listener_ids = listener_ids
                plugin_info.last_error = ""
                plugin_info.last_loaded_at = time.time()
                plugin_info.source_fingerprint = self._source_fingerprint(plugin_path) or ""
                if not plugin_info.enabled:
                    for listener_id in listener_ids:
                        self.event_bus.disable_listener(listener_id)

                # self.plugins[plugin_name] = plugin_info # 不再需要，因为我们是更新

                # 发布插件加载事件
                self.event_bus.publish(Event(
                    type=EventType.PLUGIN_LOADED,
                    source="plugin_manager",
                    data={
                        "plugin_name": plugin_name,
                        "plugin_info": {
                            "version": plugin_info.version,
                            "description": plugin_info.description,
                            "author": plugin_info.author
                        }
                    }
                ))

                self.logger.debug(f"Successfully loaded plugin '{plugin_name}' v{plugin_info.version}")
                return True

            except Exception as e:
                for listener_id in locals().get("listener_ids", []):
                    self.event_bus.unsubscribe(listener_id)
                plugin_info = self.plugins.get(plugin_name)
                if plugin_info is not None:
                    plugin_info.listener_ids = []
                    plugin_info.loaded = False
                    plugin_info.module = None
                    plugin_info.runtime_context = None
                    plugin_info.last_error = str(e)
                    plugin_info.last_failed_at = time.time()
                self.runtime_registry.unregister(plugin_name)
                self.logger.error(f"Failed to load plugin '{plugin_name}': {e}")
                return False

    def unload_plugin(self, plugin_name: str) -> bool:
        """卸载插件"""
        with self._lock:
            if plugin_name not in self.plugins:
                self.logger.warning(f"Plugin '{plugin_name}' not found")
                return False

            plugin_info = self.plugins[plugin_name]
            if plugin_info.kind != "plugin":
                return False

            try:
                # 取消所有事件订阅
                for listener_id in plugin_info.listener_ids:
                    self.event_bus.unsubscribe(listener_id)

                # Runtime API v2 requires plugins to register cleanup through
                # PluginContext. Closing the context is the single teardown
                # path, so resources are not released twice.
                cleanup_errors = self.runtime_registry.unregister(plugin_name)
                if cleanup_errors:
                    self.logger.warning(
                        "Plugin '%s' runtime cleanup completed with errors: %s",
                        plugin_name,
                        "; ".join(cleanup_errors),
                    )
                plugin_info.runtime_context = None
                plugin_info.last_unloaded_at = time.time()

                # 从sys.modules中移除整个插件包。只移除 main.py 会让
                # getmagnet.py 等子模块在热重载后继续使用旧代码。
                plugin_path = Path(plugin_info.path)
                rel_path = plugin_path.relative_to(self.plugins_dir)
                rel_module_path = str(rel_path).replace('\\', '.').replace('/', '.')
                package_name = "app.plugins." + rel_module_path
                purge_plugin_package_modules(package_name)

                # 发布插件卸载事件
                self.event_bus.publish(Event(
                    type=EventType.PLUGIN_UNLOADED,
                    source="plugin_manager",
                    data={"plugin_name": plugin_name}
                ))

                del self.plugins[plugin_name]

                self.logger.debug(f"Successfully unloaded plugin '{plugin_name}'")
                return True

            except Exception as e:
                self.logger.error(f"Failed to unload plugin '{plugin_name}': {e}")
                return False

    def unload_all_plugins(self) -> Dict[str, bool]:
        """按加载顺序的逆序卸载全部插件，确保驱动等插件资源被释放。"""
        results: Dict[str, bool] = {}

        for plugin_name in reversed(
            [name for name, item in self.plugins.items() if item.kind == "plugin"]
        ):
            results[plugin_name] = self.unload_plugin(plugin_name)

        unloaded_count = sum(results.values())
        self.logger.info("Unloaded %s/%s plugins", unloaded_count, len(results))
        return results

    def reload_plugin(self, plugin_name: str) -> bool:
        """重新加载插件"""
        with self._lock:
            if plugin_name not in self.plugins:
                return self.load_plugin(plugin_name)
            if self.plugins[plugin_name].kind != "plugin":
                return False

            self.logger.debug(f"Reloading plugin '{plugin_name}'")

            # 先卸载
            if not self.unload_plugin(plugin_name):
                return False

            # 再加载
            return self.load_plugin(plugin_name)

    def _plugin_active_task_count(self, plugin_name: str) -> int:
        with self._lock:
            plugin_info = self.plugins.get(plugin_name)
            context = plugin_info.runtime_context if plugin_info is not None else None
        return context.tasks.active_count() if context is not None else 0

    def request_hot_reload(self, plugin_name: str) -> bool:
        """合并源码触发的重载，并等待插件活动任务结束。"""
        with self._lock:
            plugin_info = self.plugins.get(plugin_name)
            if plugin_info is None or not plugin_info.enabled or not plugin_info.loaded:
                return False

        with self._hot_reload_lock:
            if plugin_name in self._pending_hot_reloads:
                self.logger.debug("Coalesced pending hot reload for plugin '%s'", plugin_name)
                return True
            self._pending_hot_reloads.add(plugin_name)

        thread = threading.Thread(
            target=self._hot_reload_when_idle,
            args=(plugin_name,),
            name=f"plugin-reload-{plugin_name.replace('/', '-')}",
            daemon=True,
        )
        thread.start()
        return True

    def _hot_reload_when_idle(self, plugin_name: str) -> None:
        deadline = time.monotonic() + self._hot_reload_max_wait
        waiting_logged = False
        try:
            while True:
                with self._lock:
                    plugin_info = self.plugins.get(plugin_name)
                    if plugin_info is None or not plugin_info.enabled or not plugin_info.loaded:
                        return

                active_tasks = self._plugin_active_task_count(plugin_name)
                if active_tasks == 0:
                    break
                if not waiting_logged:
                    self.logger.info(
                        "Deferring hot reload for plugin '%s': %s active task(s)",
                        plugin_name,
                        active_tasks,
                    )
                    waiting_logged = True
                if time.monotonic() >= deadline:
                    self.logger.warning(
                        "Skipped hot reload for plugin '%s' after waiting %.0f seconds; "
                        "%s task(s) still active",
                        plugin_name,
                        self._hot_reload_max_wait,
                        active_tasks,
                    )
                    return
                time.sleep(self._hot_reload_poll_interval)

            new_fingerprint = self.plugin_source_fingerprint(plugin_name)
            with self._lock:
                plugin_info = self.plugins.get(plugin_name)
                if plugin_info is None or not plugin_info.enabled or not plugin_info.loaded:
                    return
                loaded_fingerprint = plugin_info.source_fingerprint
            if new_fingerprint is None:
                self.logger.warning("Skipped hot reload for plugin '%s': source fingerprint failed", plugin_name)
                return
            if new_fingerprint == loaded_fingerprint:
                self.logger.info(
                    "Cancelled pending hot reload for plugin '%s': source content returned to loaded state",
                    plugin_name,
                )
                return

            self.logger.info(
                "Reloading plugin '%s' after active tasks completed: old=%s new=%s",
                plugin_name,
                loaded_fingerprint[:12] or "none",
                new_fingerprint[:12],
            )
            self.reload_plugin(plugin_name)
        finally:
            with self._hot_reload_lock:
                self._pending_hot_reloads.discard(plugin_name)

    def _discover_and_load_new_plugin(self, plugin_path: str):
        """发现并加载新插件"""
        time.sleep(1.0)  # 等待目录创建完成
        plugin_name = Path(plugin_path).name
        self.logger.debug(f"Loading new plugin: {plugin_name}")
        self.load_plugin(plugin_name)

    def load_all_plugins(self) -> Dict[str, bool]:
        """加载所有插件"""
        self.discover_plugins() # 先发现所有插件
        results = {}

        for plugin_name in list(self.plugins.keys()):
            if self.plugins[plugin_name].kind != "plugin":
                continue
            if self.plugins[plugin_name].enabled:
                results[plugin_name] = self.load_plugin(plugin_name)
            else:
                # Disabled plugins remain discoverable/configurable without
                # importing their module or starting background work.
                results[plugin_name] = True

        loaded_count = sum(results.values())
        plugin_count = sum(1 for item in self.plugins.values() if item.kind == "plugin")
        self.logger.info(f"Loaded {loaded_count}/{plugin_count} plugins")

        return results

    def enable_plugin(self, plugin_name: str) -> bool:
        """启用插件"""
        with self._lock:
            if plugin_name not in self.plugins:
                return False

            plugin_info = self.plugins[plugin_name]
            if plugin_info.kind != "plugin":
                return False
            if not self._save_plugin_runtime_enabled(plugin_name, True):
                return False
            plugin_info.enabled = True

            if not plugin_info.loaded:
                if not self.load_plugin(plugin_name):
                    self._save_plugin_runtime_enabled(plugin_name, False)
                    current = self.plugins.get(plugin_name)
                    if current:
                        current.enabled = False
                    return False
                plugin_info = self.plugins[plugin_name]

            # 启用所有监听器
            for listener_id in plugin_info.listener_ids:
                self.event_bus.enable_listener(listener_id)

            self.logger.debug(f"Enabled plugin '{plugin_name}'")
            return True

    def disable_plugin(self, plugin_name: str) -> bool:
        """禁用插件"""
        with self._lock:
            if plugin_name not in self.plugins:
                return False

            plugin_info = self.plugins[plugin_name]
            if plugin_info.kind != "plugin":
                return False
            if not self._save_plugin_runtime_enabled(plugin_name, False):
                return False
            plugin_info.enabled = False

            # 禁用所有监听器
            for listener_id in plugin_info.listener_ids:
                self.event_bus.disable_listener(listener_id)

            self.logger.debug(f"Disabled plugin '{plugin_name}'")
            return True

    def _save_plugin_runtime_enabled(self, plugin_name: str, enabled: bool) -> bool:
        """Persist lifecycle state separately from plugin business settings."""
        plugin_info = self.plugins.get(plugin_name)
        if not plugin_info:
            return False
        config_file = Path(plugin_info.path) / "config.json"
        temporary = config_file.with_name(f".{config_file.name}.tmp")
        try:
            with open(config_file, "r", encoding="utf-8") as handle:
                config = json.load(handle)
            runtime = config.get("runtime")
            if not isinstance(runtime, dict):
                runtime = {}
            runtime["enabled"] = bool(enabled)
            config["runtime"] = runtime
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(config, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, config_file)
            plugin_info.config = config
            return True
        except Exception as exc:
            self.logger.error("Failed to persist runtime state for '%s': %s", plugin_name, exc)
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass
            return False

    def get_plugin_info(self, plugin_name: str) -> Optional[PluginInfo]:
        """获取插件信息"""
        return self.plugins.get(plugin_name)

    def list_plugins(self) -> Dict[str, Dict[str, Any]]:
        """列出所有插件"""
        result = {}
        for name, plugin in self.plugins.items():
            if plugin.kind != "plugin":
                continue
            # 获取运行中的监听器信息
            listeners = self.get_plugin_listeners(name)

            # 推断特性
            features = plugin.config.get("features", [])
            if isinstance(features, list):
                features = list(features) # copy
            else:
                features = []

            # 根据 README 规范推断 push 能力
            if "push" not in features:
                if "DAILY_PUSH_TIME" in plugin.config or "ENABLE_DAILY_PUSH" in plugin.config:
                    features.append("push")

            health = (
                plugin.runtime_context.health_snapshot()
                if plugin.runtime_context is not None
                else {
                    "status": "stopped" if not plugin.enabled else "unhealthy",
                    "message": plugin.last_error or ("插件已停用" if not plugin.enabled else "插件未加载"),
                }
            )

            result[name] = {
                "name": plugin.name,
                "display_name": plugin.config.get("display_name", plugin.name),
                "category": plugin.config.get("category", "utility"),
                "version": plugin.version,
                "description": plugin.description,
                "author": plugin.author,
                "enabled": plugin.enabled,
                "loaded": plugin.loaded,
                "listener_count": len(plugin.listener_ids),
                "features": features,
                "manifest_version": plugin.manifest.get("schema_version"),
                "plugin_api_version": plugin.manifest.get("plugin_api_version"),
                "last_error": plugin.last_error or None,
                "last_loaded_at": plugin.last_loaded_at,
                "last_failed_at": plugin.last_failed_at,
                "health": health,
                "jobs": list(plugin.manifest.get("jobs", [])),
            }
        return result

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        plugin_values = [item for item in self.plugins.values() if item.kind == "plugin"]
        stats = {
            "total_plugins": len(plugin_values),
            "loaded_plugins": sum(1 for p in plugin_values if p.loaded),
            "enabled_plugins": sum(1 for p in plugin_values if p.enabled),
            "total_listeners": sum(len(p.listener_ids) for p in plugin_values)
        }

        # 聚合监听器信息: EventType -> [PluginName, ...]
        listeners_by_type: Dict[str, List[str]] = {}

        for plugin in plugin_values:
            if not plugin.enabled or not plugin.loaded:
                continue

            for listener_id in plugin.listener_ids:
                info = self.event_bus.get_listener_info(listener_id)
                if info and info.get('enabled', True):
                    e_type = info['event_type'].value
                    if e_type not in listeners_by_type:
                        listeners_by_type[e_type] = []

                    if plugin.name not in listeners_by_type[e_type]:
                        listeners_by_type[e_type].append(plugin.name)

        stats['listeners_by_type'] = listeners_by_type

        # 添加 LLM 统计信息
        try:
            from app.services.llm_manager import get_llm_manager
            llm_manager = get_llm_manager()
            llm_stats = llm_manager.get_stats()

            # 汇总所有插件的 LLM 调用统计
            total_calls = 0
            total_tokens = 0
            total_errors = 0
            all_response_times = []

            # 使用 session stats（当前运行期间的统计）
            for key, data in llm_stats.get("session", {}).items():
                total_calls += data.get("count", 0)
                total_tokens += data.get("total_tokens", 0)
                total_errors += data.get("error_count", 0)
                all_response_times.extend(data.get("response_times", []))

            # 计算平均响应时间
            avg_response_time = 0.0
            if all_response_times:
                avg_response_time = sum(all_response_times) / len(all_response_times)

            stats['total_calls'] = total_calls
            stats['total_tokens'] = total_tokens
            stats['error_count'] = total_errors
            stats['avg_response_time'] = avg_response_time

        except Exception as e:
            logger.error(f"获取 LLM 统计失败: {e}")
            stats['total_calls'] = 0
            stats['total_tokens'] = 0
            stats['error_count'] = 0
            stats['avg_response_time'] = 0.0

        return stats

    def get_all_plugin_names(self) -> List[str]:
        """获取所有已发现插件的名称列表"""
        return [name for name, item in self.plugins.items() if item.kind == "plugin"]

    def get_plugin_listeners(self, plugin_name: str) -> List[Dict[str, Any]]:
        """获取插件的所有监听器信息"""
        plugin_info = self.plugins.get(plugin_name)
        if not plugin_info:
            return []

        listeners = []
        for listener_id in plugin_info.listener_ids:
            listener_info = self.event_bus.get_listener_info(listener_id)
            if listener_info:
                listeners.append(listener_info)

        return listeners
