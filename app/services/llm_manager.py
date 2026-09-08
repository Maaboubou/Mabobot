"""
统一大模型管理服务 - 基于 LiteLLM
支持 OpenAI、Gemini、DeepSeek 等 100+ 模型
"""

import asyncio
import copy
import hashlib
import litellm
import json
import logging
import os
import ssl
import time
import threading
import uuid
from contextvars import ContextVar
from importlib.metadata import version as package_version
from app.services.llm_pricing import normalize_usage, price_usage, raw_usage, read, decimal

_usage_call = ContextVar("llm_billing_call", default=None)
from typing import List, Dict, Optional, Any
from pathlib import Path
from datetime import datetime

try:
    import httpx as _httpx
except ImportError:
    _httpx = None

try:
    import requests as _requests
except ImportError:
    _requests = None

from app.services.config_service import get_setting

logger = logging.getLogger(__name__)

# 全局锁：防止并发调用同时修改 HTTP_PROXY / HTTPS_PROXY 环境变量
_proxy_env_lock = threading.Lock()


def configure_litellm_sync_transport() -> None:
    """Use HTTPX for synchronous LiteLLM calls made from worker threads.

    LiteLLM defaults to an aiohttp transport even for ``completion()``. The
    synchronous wrapper creates short-lived asyncio loops, so cached aiohttp
    sessions can outlive their owning loop and are later reported as unclosed.
    HTTPX is the stable transport for this long-lived, threaded service.
    """
    litellm.disable_aiohttp_transport = True


# Apply this before any manager or model-test endpoint can issue a request.
configure_litellm_sync_transport()

# Google-only 工具名称集合（只有 gemini/ 直连才支持）
_GOOGLE_ONLY_TOOLS: frozenset = frozenset({"google_search", "google_search_retrieval"})

# Some OpenAI-compatible gateways return provider errors as normal assistant
# content. Treat these short, known error payloads as failed calls so fallback
# models can be tried.
_RETRIABLE_CONTENT_FAILURE_MARKERS: tuple[str, ...] = (
    "this model is overloaded right now",
    "model is overloaded",
    "please try again shortly or pick a different model",
)

# Gemini 3.x reasoning is tuned for the provider defaults. Google recommends
# removing these sampling controls from every Gemini 3.x request; newer models
# ignore them today and may reject them in future API versions.
GEMINI_3_SAMPLING_PARAMETERS: frozenset[str] = frozenset(
    {"temperature", "top_p", "top_k"}
)
_GEMINI_3_SAMPLING_ALIASES: frozenset[str] = frozenset(
    {"temperature", "top_p", "top_k", "topP", "topK"}
)


def is_gemini_3_model_config(model_cfg: Dict[str, Any]) -> bool:
    """Return whether a config calls Gemini 3.x through Google's adapters."""
    model = str(model_cfg.get("model") or "").strip().lower()
    provider = str(
        model_cfg.get("custom_llm_provider") or model_cfg.get("provider") or ""
    ).strip().lower()
    api_base = str(model_cfg.get("api_base") or "").strip().lower()

    if not provider:
        if model.startswith("gemini/"):
            provider = "gemini"
        elif model.startswith("vertex_ai/"):
            provider = "vertex_ai"
        elif "generativelanguage.googleapis.com" in api_base:
            provider = "gemini"
        elif "aiplatform.googleapis.com" in api_base:
            provider = "vertex_ai"

    return provider in {"gemini", "vertex_ai"} and "gemini-3" in model


def _strip_gemini_3_sampling_parameters_inplace(payload: Dict[str, Any]) -> set[str]:
    """Remove Gemini 3.x sampling controls from a config/request dictionary."""
    removed: set[str] = set()
    for key in _GEMINI_3_SAMPLING_ALIASES:
        if key in payload:
            payload.pop(key, None)
            removed.add(key)

    extra_body = payload.get("extra_body")
    if isinstance(extra_body, dict):
        for key in _GEMINI_3_SAMPLING_ALIASES:
            if key in extra_body:
                extra_body.pop(key, None)
                removed.add(f"extra_body.{key}")
        for container_key in ("generation_config", "generationConfig"):
            generation_config = extra_body.get(container_key)
            if not isinstance(generation_config, dict):
                continue
            for key in _GEMINI_3_SAMPLING_ALIASES:
                if key in generation_config:
                    generation_config.pop(key, None)
                    removed.add(f"extra_body.{container_key}.{key}")
            if not generation_config:
                extra_body.pop(container_key, None)
        if not extra_body:
            payload.pop("extra_body", None)
    return removed


def sanitize_gemini_3_model_config(
    model_cfg: Dict[str, Any],
) -> tuple[Dict[str, Any], set[str]]:
    """Copy a model config and remove deprecated Gemini 3.x sampling values."""
    sanitized = copy.deepcopy(model_cfg)
    if not is_gemini_3_model_config(sanitized):
        return sanitized, set()
    return sanitized, _strip_gemini_3_sampling_parameters_inplace(sanitized)


def _is_google_only_tool(tool: dict) -> bool:
    """判断一个 tool dict 是否是 Google 专属工具。"""
    return isinstance(tool, dict) and bool(set(tool.keys()) & _GOOGLE_ONLY_TOOLS)


class LLMManager:
    """统一大模型管理服务"""

    def __init__(
        self,
        config_dir: str = "data",
        telemetry_dir: Optional[str | Path] = None,
    ):
        self.config_dir = Path(config_dir)
        self.models_path = self.config_dir / "llm_models.json"
        self.mappings_path = self.config_dir / "llm_mappings.json"
        self.legacy_config_path = self.config_dir / "llm_config.json"

        telemetry_root = (
            Path(telemetry_dir)
            if telemetry_dir is not None
            else Path("data")
        )
        self.call_history_path = telemetry_root / "llm_call_history.jsonl"
        self.config = {"models": {}, "plugin_mappings": {}}
        self.session_stats = {}
        self.total_stats = {}
        self.daily_stats = {}
        self.call_history = {}  # key: "plugin.call_type" -> list[dict], 每key最多10条
        self._call_history_lock = threading.RLock()
        self._stats_lock = threading.RLock()
        self._config_lock = threading.RLock()
        self._model_health_lock = threading.RLock()
        self._model_health: Dict[str, Dict[str, Any]] = {}
        self._last_models_mtime = None
        self._last_mappings_mtime = None
        self.last_used_model: Optional[str] = None  # 最近一次成功调用实际使用的模型名
        self._proxy_cache: Optional[str] = None      # 缓存的代理 URL，避免每次 call 都读 DB
        self._proxy_cache_dirty: bool = True          # True = 需要重新读 DB

        self.load_config()
        from app.services.llm_usage_service import LLMUsageService
        self.usage_service = LLMUsageService(
            telemetry_root / "llm_usage_v2.sqlite3",
        )
        self.load_call_history()

        litellm.set_verbose = False
        # fallback 到不支持某些参数的模型时，自动丢弃不支持的参数
        litellm.drop_params = True

        litellm.success_callback = [self._log_success]
        litellm.failure_callback = [self._log_failure]

        logger.info("✅ LLM Manager 初始化成功")
        self.apply_proxy_env_vars()

    # ─────────────────────── 模型健康与熔断 ───────────────────────

    @staticmethod
    def _circuit_failure_threshold() -> int:
        try:
            return max(2, min(int(os.getenv("LLM_CIRCUIT_FAILURE_THRESHOLD", "3")), 20))
        except (TypeError, ValueError):
            return 3

    @staticmethod
    def _circuit_cooldown_seconds() -> int:
        try:
            return max(15, min(int(os.getenv("LLM_CIRCUIT_COOLDOWN_SECONDS", "120")), 3600))
        except (TypeError, ValueError):
            return 120

    def _record_model_success(self, model: str, duration: Optional[float] = None) -> None:
        key = str(model or "unknown")
        now = time.time()
        with self._model_health_lock:
            item = self._model_health.setdefault(key, {})
            item.update(
                {
                    "model": key,
                    "status": "healthy",
                    "consecutive_failures": 0,
                    "opened_until": 0,
                    "last_success_at": now,
                    "last_latency_seconds": round(float(duration), 3) if duration is not None else None,
                }
            )

    def _record_model_failure(self, model: str, error: Any = None) -> None:
        key = str(model or "unknown")
        now = time.time()
        threshold = self._circuit_failure_threshold()
        with self._model_health_lock:
            item = self._model_health.setdefault(key, {"model": key, "consecutive_failures": 0})
            failures = int(item.get("consecutive_failures") or 0) + 1
            item.update(
                {
                    "model": key,
                    "consecutive_failures": failures,
                    "last_failure_at": now,
                    "last_error": str(error or "model call failed")[:500],
                }
            )
            if failures >= threshold:
                item["status"] = "open"
                item["opened_until"] = now + self._circuit_cooldown_seconds()
            else:
                item["status"] = "degraded"

    def _model_circuit_open(self, model: str) -> bool:
        key = str(model or "unknown")
        now = time.time()
        with self._model_health_lock:
            item = self._model_health.get(key)
            if not item:
                return False
            opened_until = float(item.get("opened_until") or 0)
            if opened_until > now:
                return True
            if item.get("status") == "open":
                item["status"] = "half_open"
            return False

    def get_model_health(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._model_health_lock:
            rows = [dict(item) for item in self._model_health.values()]
        for item in rows:
            opened_until = float(item.get("opened_until") or 0)
            if item.get("status") == "open" and opened_until <= now:
                item["status"] = "half_open"
            item["retry_after_seconds"] = max(0, round(opened_until - now, 1))
        rows.sort(key=lambda item: (item.get("status") != "open", -(item.get("last_failure_at") or 0)))
        return rows

    # ─────────────────────────── 代理管理 ───────────────────────────

    def apply_proxy_env_vars(self):
        """
        将代理配置写入 os.environ，并同步更新 LiteLLM 的代理兼容开关。

        主服务的同步调用固定使用 HTTPX；保留 aiohttp_trust_env 是为了兼容
        进程内可能显式启用 aiohttp 的第三方扩展。

        性能优化：内部缓存代理 URL，若未发生变化则跳过 DB 读取和 transport 更新。
        调用时机：LLMManager 启动时 + Web UI 保存代理设置后（通过 mark_proxy_dirty()）
        """
        with _proxy_env_lock:
            # 读取当前配置
            new_proxy_url = self._get_proxy_url()

            # 若缓存匹配且不是脏标记，直接返回（避免每次 call 都读 DB + 更新 transport）
            if not self._proxy_cache_dirty and new_proxy_url == self._proxy_cache:
                return

            self._proxy_cache = new_proxy_url
            self._proxy_cache_dirty = False

            proxy_vars = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                          "http_proxy", "https_proxy", "all_proxy"]
            no_proxy_val = "localhost,127.0.0.1,::1,0.0.0.0"

            if new_proxy_url:
                for var in proxy_vars:
                    os.environ[var] = new_proxy_url
                os.environ["NO_PROXY"] = no_proxy_val
                os.environ["no_proxy"] = no_proxy_val
                os.environ["AIOHTTP_TRUST_ENV"] = "True"
                litellm.aiohttp_trust_env = True
                logger.info("🌐 代理已配置（地址已隐藏，NO_PROXY=%s）", no_proxy_val)
            else:
                for var in proxy_vars + ["NO_PROXY", "no_proxy", "AIOHTTP_TRUST_ENV"]:
                    os.environ.pop(var, None)
                litellm.aiohttp_trust_env = False
                logger.info("🌐 代理已清除（直连模式，aiohttp_trust_env=False）")

            # 主服务固定走 HTTPX，不需要再通过丢弃 aiohttp shared session
            # 引用来刷新代理，也避免重新引入无法 await close 的清理路径。

    def mark_proxy_dirty(self):
        """标记代理配置已变更，下次 call 时重新应用。供 Web UI 保存代理后调用。"""
        self._proxy_cache_dirty = True

    # ───────────────────────── 配置热更新 ─────────────────────────

    def _check_and_reload_if_modified(self):
        """检查配置文件是否被修改，如果是则自动重新加载"""
        try:
            modified = False
            # 检查模型配置
            if self.models_path.exists():
                mtime = self.models_path.stat().st_mtime
                if self._last_models_mtime is None or mtime > self._last_models_mtime:
                    modified = True

            # 检查映射配置
            if self.mappings_path.exists():
                mtime = self.mappings_path.stat().st_mtime
                if self._last_mappings_mtime is None or mtime > self._last_mappings_mtime:
                    modified = True

            if modified:
                logger.info("🔄 检测到配置文件变化，自动重新加载")
                self.load_config()
                logger.info("✅ 配置已自动热更新")
        except Exception as e:
            logger.error(f"❌ 检查配置文件修改时出错: {e}")

    def load_config(self):
        """Load both config files under the same lock used by atomic saves."""
        with self._config_lock:
            self._load_config_unlocked()

    def _load_config_unlocked(self):
        """加载配置文件（支持分离后的文件与旧版迁移）"""
        migration_needed = False

        # 1. 尝试加载新版模型配置
        if self.models_path.exists():
            with open(self.models_path, 'r', encoding='utf-8') as f:
                self.config["models"] = json.load(f)
            self._last_models_mtime = self.models_path.stat().st_mtime

        # 2. 尝试加载新版映射配置
        if self.mappings_path.exists():
            with open(self.mappings_path, 'r', encoding='utf-8') as f:
                self.config["plugin_mappings"] = json.load(f)
            self._last_mappings_mtime = self.mappings_path.stat().st_mtime

        # 3. 迁移逻辑：如果新文件不存在但旧文件存在
        if (not self.models_path.exists() or not self.mappings_path.exists()) and self.legacy_config_path.exists():
            logger.info(f"🚚 发现旧版配置文件 {self.legacy_config_path}，正在执行自动迁移...")
            try:
                with open(self.legacy_config_path, 'r', encoding='utf-8') as f:
                    legacy_data = json.load(f)

                if not self.models_path.exists() and "models" in legacy_data:
                    self.config["models"] = legacy_data["models"]
                if not self.mappings_path.exists() and "plugin_mappings" in legacy_data:
                    self.config["plugin_mappings"] = legacy_data["plugin_mappings"]

                migration_needed = True
            except Exception as e:
                logger.error(f"❌ 迁移旧版配置失败: {e}")

        # 4. 如果没有任何配置文件，加载默认配置
        if not self.config.get("models") and not self.config.get("plugin_mappings"):
            logger.info("📄 未找到任何配置，加载默认配置")
            default_config = self._get_default_config()
            self.config["models"] = default_config.get("models", {})
            self.config["plugin_mappings"] = default_config.get("plugin_mappings", {})
            migration_needed = True

        # 自动清理 Gemini 3.x 已弃用采样参数。旧配置在首次加载新版
        # 代码时会被修正并写回，避免仅靠调用时过滤而长期保留脏数据。
        from app.services.retired_model_routes import prune_retired_model_routes

        modified = prune_retired_model_routes(self.config.get("plugin_mappings", {}))
        for model_id, model_cfg in list(self.config.get("models", {}).items()):
            if not isinstance(model_cfg, dict):
                continue
            if self._is_local_codex_model(model_cfg) and "codex_profile_id" in model_cfg:
                model_cfg.pop("codex_profile_id", None)
                modified = True
                logger.info(
                    "🧹 已移除模型 %s 的旧 Codex Profile 绑定，单次调用使用模型自身配置",
                    model_id,
                )
            sanitized, removed = sanitize_gemini_3_model_config(model_cfg)
            if removed:
                self.config["models"][model_id] = sanitized
                modified = True
                logger.info(
                    "🧹 已清理 Gemini 3+ 模型 %s 的弃用采样参数: %s",
                    model_id,
                    ", ".join(sorted(removed)),
                )
        for plugin_name, plugin_mappings in self.config.get("plugin_mappings", {}).items():
            if not isinstance(plugin_mappings, dict):
                continue
            for call_type, mapping in plugin_mappings.items():
                if not isinstance(mapping, dict):
                    continue
                primary_id = str(mapping.get("primary") or "").strip()
                primary_cfg = self.config.get("models", {}).get(primary_id, {})
                overrides = mapping.get("override_params")
                if not is_gemini_3_model_config(primary_cfg) or not isinstance(overrides, dict):
                    continue
                removed = _strip_gemini_3_sampling_parameters_inplace(overrides)
                if removed:
                    modified = True
                    logger.info(
                        "🧹 已清理 Gemini 3+ 路由 %s.%s 的弃用采样参数: %s",
                        plugin_name,
                        call_type,
                        ", ".join(sorted(removed)),
                    )

        # Migrate the historical plugin-owned routes to the first-class
        # Assistant namespace before pruning retired reply/tool routes.
        all_mappings = self.config.setdefault("plugin_mappings", {})
        assistant_mappings = all_mappings.setdefault("assistant", {})
        legacy_assistant_mappings = all_mappings.pop("builtin_chatbot", None)
        if isinstance(legacy_assistant_mappings, dict):
            for call_type, mapping in legacy_assistant_mappings.items():
                assistant_mappings.setdefault(call_type, mapping)
            modified = True
            logger.info("🧹 已将旧 builtin_chatbot 辅助模型路由迁移到 assistant")

        if assistant_mappings.pop("web_search", None) is not None:
            modified = True
            logger.info("🧹 已删除旧模型路由 assistant.web_search")
        legacy_image_mapping = assistant_mappings.pop("ocr", None)
        if legacy_image_mapping is not None:
            modified = True
            logger.info("🧹 已迁移旧模型路由 assistant.ocr")
        legacy_vision_mapping = assistant_mappings.pop("vision", None)
        if legacy_vision_mapping is not None:
            modified = True
            logger.info("🧹 已迁移未使用的旧模型路由 assistant.vision")
        legacy_chat_mapping = assistant_mappings.pop("chat", None)
        if legacy_chat_mapping is not None:
            modified = True
            logger.info(
                "🧹 已删除最终回复的通用模型路由 assistant.chat；聊天回复现仅由 Codex 驱动"
            )

        logger_mappings = all_mappings.get("builtin_chat_logger")
        if not isinstance(logger_mappings, dict):
            logger_mappings = {}
        if "image_understanding" not in logger_mappings:
            replacement = legacy_image_mapping
            if not isinstance(replacement, dict):
                replacement = legacy_vision_mapping or legacy_chat_mapping
            if isinstance(replacement, dict) and replacement.get("primary"):
                logger_mappings["image_understanding"] = copy.deepcopy(replacement)
                all_mappings["builtin_chat_logger"] = logger_mappings
                modified = True
                logger.info(
                    "🧹 图片模型路由归属已调整为 builtin_chat_logger.image_understanding"
                )

        if (
            "followup_judge" not in assistant_mappings
            and "deepseek" in self.config.get("models", {})
            and "deepseek-followup" not in self.config.get("models", {})
        ):
            followup_model = copy.deepcopy(self.config["models"]["deepseek"])
            followup_model["temperature"] = 0.1
            followup_model["max_tokens"] = 200
            followup_model["timeout"] = 15
            followup_model["extra_body"] = {
                "thinking": {"type": "disabled"},
            }
            followup_model["enable_web_search"] = False
            self.config["models"]["deepseek-followup"] = followup_model
            modified = True

        if "followup_judge" not in assistant_mappings:
            regular_judge = assistant_mappings.get("judge") or {}
            regular_primary = str(regular_judge.get("primary") or "").strip()
            preferred_primary = (
                "deepseek-followup"
                if "deepseek-followup" in self.config.get("models", {})
                else regular_primary or "gemini-flash"
            )
            fallback = []
            if regular_primary and regular_primary != preferred_primary:
                fallback.append(regular_primary)
            assistant_mappings["followup_judge"] = {
                "primary": preferred_primary,
                "fallback": fallback,
                "override_params": {
                    "temperature": 0.1,
                    "max_tokens": 200,
                    "timeout": 15,
                    "response_format": {"type": "json_object"},
                },
            }
            modified = True

        if "summary_plus" in self.config.get("plugin_mappings", {}):
            summary_mappings = self.config["plugin_mappings"]["summary_plus"]
            if "bilibili_mindmap" not in summary_mappings:
                summary_mappings["bilibili_mindmap"] = {
                    "primary": "gpt-5.2",
                    "fallback": ["OPENROUTER-DS"],
                    "override_params": {},
                }
                modified = True

        if migration_needed or modified:
            self.save_config()

        logger.info(
            "📄 配置加载完成: models=%s, mappings=%s",
            len(self.config.get("models", {})),
            len(self.config.get("plugin_mappings", {})),
        )

    def save_config(self):
        """保存配置文件（分两个文件保存）"""
        with self._config_lock:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            models = copy.deepcopy(self.config.get("models", {}))
            mappings = copy.deepcopy(self.config.get("plugin_mappings", {}))

            def atomic_write(path: Path, payload: Dict[str, Any]) -> None:
                temp_path = path.with_name(
                    f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
                )
                try:
                    with open(temp_path, "w", encoding="utf-8") as f:
                        json.dump(payload, f, indent=2, ensure_ascii=False)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temp_path, path)
                finally:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except Exception:
                        pass

            atomic_write(self.models_path, models)
            atomic_write(self.mappings_path, mappings)
            self._last_models_mtime = self.models_path.stat().st_mtime
            self._last_mappings_mtime = self.mappings_path.stat().st_mtime

        logger.info(f"💾 配置已保存到 {self.models_path} 和 {self.mappings_path}")

    def get_model_name(self, plugin_name: str, call_type: str) -> str:
        """获取指定插件和调用类型对应的主模型名称"""
        try:
            mapping = self._get_mapping(plugin_name, call_type)
            if not mapping:
                return "Unknown"
            primary_id = mapping.get("primary")
            if not primary_id:
                return "Unknown"
            model_config = self.config.get("models", {}).get(primary_id)
            if not model_config:
                return "Unknown"
            return model_config.get("model", "Unknown")
        except Exception:
            return "Unknown"

    def is_local_codex_call(
        self,
        plugin_name: str,
        call_type: str,
    ) -> bool:
        """Return whether the configured primary model uses local Codex."""
        self._check_and_reload_if_modified()
        mapping = self._get_mapping(plugin_name, call_type)
        if not mapping:
            return False
        model_id = str(mapping.get("primary") or "").strip()
        model_config = self.config.get("models", {}).get(model_id)
        return bool(model_config and self._is_local_codex_model(model_config))

    def get_call_capabilities(self, plugin_name: str, call_type: str) -> Dict[str, Any]:
        """Return normalized capabilities for the configured primary route.

        Callers use this boundary instead of scattering provider/model-name
        checks throughout plugins.  ``native_web_search`` intentionally means
        the local Codex runtime; ordinary providers use Mabobot's local tools.
        """
        self._check_and_reload_if_modified()
        mapping = self._get_mapping(plugin_name, call_type) or {}
        model_id = str(mapping.get("primary") or "").strip()
        model_config = self.config.get("models", {}).get(model_id) or {}
        model_name = str(model_config.get("model") or model_id)
        local_codex = bool(model_config and self._is_local_codex_model(model_config))

        vision = bool(
            model_config.get("supports_vision")
            or model_config.get("vision")
            or model_config.get("image_input")
        )
        function_calling = bool(model_config.get("supports_function_calling"))
        if model_name and not local_codex:
            try:
                vision = vision or bool(litellm.supports_vision(model_name))
            except Exception:
                pass
            try:
                function_calling = function_calling or bool(
                    litellm.supports_function_calling(model_name)
                )
            except Exception:
                pass
        return {
            "model_id": model_id,
            "model": model_name,
            "provider": str(
                model_config.get("custom_llm_provider")
                or model_config.get("provider")
                or ""
            ),
            "local_codex": local_codex,
            "native_web_search": local_codex,
            "vision": bool(vision or local_codex),
            "tool_calling": bool(function_calling or local_codex),
        }

    def count_rendered_prompt_tokens(
        self,
        plugin_name: str,
        call_type: str,
        messages: List[Dict],
        *,
        input_image_count: int = 0,
    ) -> Optional[int]:
        """Count the primary local-Codex prompt with its real text renderer.

        Other providers use different chat serialization and tokenizers, so
        callers receive ``None`` and can retain their provider-specific or
        conservative fallback behavior.
        """
        self._check_and_reload_if_modified()
        mapping = self._get_mapping(plugin_name, call_type)
        if not mapping:
            return None

        primary_model_id = mapping.get("primary")
        model_config = self.config.get("models", {}).get(primary_model_id)
        if not model_config or not self._is_local_codex_model(model_config):
            return None

        primary_params = self._build_single_model_params(
            model_cfg=model_config,
            messages=messages,
            caller_tools=None,
            extra_kwargs=dict(mapping.get("override_params") or {}),
        )
        extra_body = primary_params.get("extra_body") or {}
        web_search_value = primary_params.get(
            "codex_web_search",
            primary_params.get(
                "web_search",
                extra_body.get("codex_web_search", extra_body.get("web_search")),
            ),
        )
        if isinstance(web_search_value, str):
            native_web_search_enabled = web_search_value.strip().lower() in {"1", "true", "yes", "on"}
        else:
            native_web_search_enabled = bool(web_search_value)

        from app.services.codex_proxy.client import count_codex_prompt_tokens

        token_count = count_codex_prompt_tokens(
            messages,
            native_web_search_enabled=native_web_search_enabled,
            input_image_count=input_image_count,
            text_only=True,
        )
        return token_count or None

    # ─────────────────────── 核心参数构建 ────────────────────────

    def _build_single_model_params(
        self,
        model_cfg: dict,
        messages: List[Dict],
        caller_tools: Optional[List] = None,
        extra_kwargs: Optional[dict] = None,
    ) -> dict:
        """
        为**单个模型**干净地构建完整的 litellm.completion() 参数字典。

        设计原则：
        - 不修改任何输入参数（纯函数）
        - 不依赖外部可变状态（除 self.config）
        - 所有工具过滤逻辑集中在此处，只需维护一份

        Args:
            model_cfg:          来自 config["models"][id] 的模型配置
            messages:           消息列表（只读引用，函数内部会 copy 处理）
            caller_tools:       调用方传入的原始 tools 列表（如 [{"google_search": {}}]）
            extra_kwargs:       其余透传给 litellm 的参数（不含 tools / model / messages）

        Returns:
            整洁的参数字典，可直接传给 litellm.completion(**params)
        """
        model_str = model_cfg["model"]
        params: dict = {
            "model": model_str,
            "messages": messages,
        }
        params["_mabobot_supports_vision"] = bool(
            model_cfg.get("supports_vision")
            or model_cfg.get("vision")
            or model_cfg.get("image_input")
        )

        # ── temperature ──
        if "temperature" in model_cfg:
            params["temperature"] = model_cfg["temperature"]

        # ── max_tokens ──
        if "max_tokens" in model_cfg:
            params["max_tokens"] = model_cfg["max_tokens"]

        # ── timeout ──
        if "timeout" in model_cfg:
            params["timeout"] = model_cfg["timeout"]

        # ── 认证 ──
        # 显式提供 api_key/api_base：若配置不存在则设为 None。
        # 关键用途：当此模型作为 fallback 出现时，显式的 None 会阻止其继承主模型的参数（LiteLLM 默认会继承顶层参数）。
        api_key = self._resolve_env(model_cfg.get("api_key"))
        api_base = self._resolve_env(model_cfg.get("api_base"))
        custom_llm_provider = self._resolve_env(
            model_cfg.get("custom_llm_provider") or model_cfg.get("provider")
        )
        params["api_key"] = api_key if api_key else None
        params["api_base"] = api_base if api_base else None
        params["custom_llm_provider"] = custom_llm_provider if custom_llm_provider else None

        # 本地 Codex CLI 代理一次失败通常已经等到 CODEX_PROXY_TIMEOUT。
        # OpenAI/LiteLLM 默认会对 5xx 自动重试，导致单个群聊 worker 被 600s * 多次重试阻塞。
        # 对本地 Codex 代理禁用 SDK 层重试，让失败尽快交给业务 fallback/队列继续处理。
        api_base_l = str(api_base or "").lower()
        if "max_retries" in model_cfg:
            params["max_retries"] = int(model_cfg.get("max_retries") or 0)
        elif "/api/codex/" in api_base_l or api_base_l.endswith("/api/codex/v1"):
            params["max_retries"] = 0

        # ── 响应格式与额外参数 ──
        # 优先级：extra_kwargs > model_cfg
        # 关键修复：确保 extra_body 永远是字典，且 response_format 只有在非 None 时才设置

        # 1. response_format
        if extra_kwargs and "response_format" in extra_kwargs:
            val = extra_kwargs["response_format"]
            if val: params["response_format"] = copy.deepcopy(val)
        else:
            val = model_cfg.get("response_format")
            if val: params["response_format"] = copy.deepcopy(val)

        # 2. extra_body (核心修复: 强制为 dict)
        if extra_kwargs and "extra_body" in extra_kwargs:
            params["extra_body"] = copy.deepcopy(extra_kwargs["extra_body"]) or {}
        else:
            params["extra_body"] = copy.deepcopy(model_cfg.get("extra_body")) or {}

        if params["extra_body"] is None:
            params["extra_body"] = {}

        # ── 自定义价格 ──
        # LiteLLM 对新模型的价格表可能滞后；允许在模型配置里显式写入
        # 官方每 token 价格，供 response_cost 自动计算使用。
        _PRICING_KEYS = (
            "input_cost_per_token",
            "output_cost_per_token",
            "cache_read_input_token_cost",
            "cache_creation_input_token_cost",
        )
        for key in _PRICING_KEYS:
            if extra_kwargs and key in extra_kwargs:
                params[key] = extra_kwargs[key]
            elif key in model_cfg:
                params[key] = model_cfg[key]

        # ── 工具处理（集中在此处，消除三处重复逻辑）──
        self._apply_tools(params, model_str, model_cfg, caller_tools)

        # ── 映射/调用透传参数 ──
        # call() 已剔除调用方的 temperature/max_tokens/timeout，因此这里的
        # 同名核心参数来自 mapping.override_params，必须覆盖模型级默认值。
        if extra_kwargs:
            for k, v in extra_kwargs.items():
                if k not in ("tools", "model", "messages", "api_key", "api_base"):
                    params[k] = v

        # ── 针对 DeepSeek Reasoner 官方 API 的兼容性脱敏 ──
        # R1 官方目前对 response_format: json_object 的支持不稳定，偶发返回空响应。
        # 且 LiteLLM 在处理 R1 返回 reasoning_content + json_object 时存在解析风险。
        if params.get("model", "").endswith("deepseek-reasoner") and "response_format" in params:
            api_base = params.get("api_base", "")
            if not api_base or "deepseek.com" in api_base:
                logger.warning(f"🛡️ 已从 deepseek-reasoner (官方) 移除 response_format 参数以避免空响应错误。建议在 Prompt 中明确 JSON 要求。")
                params.pop("response_format", None)

        # 最后一层防御：mapping.override_params、插件透传或历史配置都不能
        # 再把采样参数带回 Gemini 3.x 请求。
        if is_gemini_3_model_config(model_cfg):
            _strip_gemini_3_sampling_parameters_inplace(params)

        return params

    def _apply_tools(
        self,
        params: dict,
        model_str: str,
        model_cfg: dict,
        caller_tools: Optional[List],
    ) -> None:
        """
        将正确的 tools 配置写入 params（就地修改 params）。

        规则：
        1. Gemini 直连（gemini/ 前缀）：保留 caller_tools 中的所有工具，
           包括 google_search。
        2. Moonshot/Kimi：剔除 google_search 工具，若 enable_web_search=True
           注入 $web_search builtin_function。
        3. OpenRouter / 其他：剔除 google_search 工具；若 enable_web_search=True
           通过 extra_body.plugins 注入 web 插件。
        4. 若最终没有任何工具，**不设置 tools 键**（避免 tools=None 或 tools=[] 报错）。
        """
        is_gemini = model_str.startswith("gemini/")
        is_moonshot = model_str.startswith("moonshot/")
        enable_web_search = model_cfg.get("enable_web_search") is True

        # ── 处理 caller_tools ──
        if caller_tools:
            if is_gemini:
                # Gemini 直连：所有工具都支持（包括 google_search）
                valid_tools = list(caller_tools)
            else:
                # 非 Gemini：剔除 Google 专属工具
                valid_tools = [t for t in caller_tools if not _is_google_only_tool(t)]

            if valid_tools:
                params["tools"] = valid_tools
            # 没有有效工具时，不设置 tools 键（关键修复：不写 tools=None）

        # ── 注入平台 web search 配置 ──
        if enable_web_search:
            if is_moonshot:
                tools = params.get("tools") or []
                has_web = any(
                    isinstance(t, dict) and t.get("function", {}).get("name") == "$web_search"
                    for t in tools
                )
                if not has_web:
                    tools.append({"type": "builtin_function", "function": {"name": "$web_search"}})
                params["tools"] = tools
            else:
                # OpenRouter / 其他：通过 extra_body.plugins 注入
                if params.get("extra_body") is None:
                    params["extra_body"] = {}
                plugins = params["extra_body"].get("plugins", [])
                if not any(isinstance(p, dict) and p.get("id") == "web" for p in plugins):
                    plugins.append({"id": "web"})
                    params["extra_body"]["plugins"] = plugins

    @staticmethod
    def _remove_model_native_web_search(params: dict) -> None:
        """Remove provider-native search from a non-Codex local-tool call."""
        tools = []
        for tool in params.get("tools") or []:
            function = tool.get("function") if isinstance(tool, dict) else None
            name = str(function.get("name") or "") if isinstance(function, dict) else ""
            if name == "$web_search" or (isinstance(tool, dict) and "google_search" in tool):
                continue
            tools.append(tool)
        if tools:
            params["tools"] = tools
        else:
            params.pop("tools", None)

        extra_body = params.get("extra_body")
        if isinstance(extra_body, dict):
            plugins = [
                plugin
                for plugin in extra_body.get("plugins") or []
                if not (isinstance(plugin, dict) and plugin.get("id") == "web")
            ]
            if plugins:
                extra_body["plugins"] = plugins
            else:
                extra_body.pop("plugins", None)
            for key in ("web_search", "codex_web_search", "web_search_options"):
                extra_body.pop(key, None)
        params.pop("web_search", None)
        params.pop("codex_web_search", None)
        params.pop("web_search_options", None)
        params.pop("enable_web_search", None)

    @staticmethod
    def _strip_internal_provider_params(params: dict) -> None:
        """Keep Mabobot routing metadata out of external provider payloads."""

        def clean(payload: dict) -> None:
            for key in list(payload):
                if str(key).startswith("_mabobot_"):
                    payload.pop(key, None)

            extra_body = payload.get("extra_body")
            if isinstance(extra_body, dict):
                for key in list(extra_body):
                    if str(key).startswith(("_mabobot_", "mabobot_")):
                        extra_body.pop(key, None)
                if not extra_body:
                    payload.pop("extra_body", None)

        clean(params)
        for fallback in params.get("fallbacks") or []:
            if isinstance(fallback, dict):
                clean(fallback)

    # ─────────────────────────── 主调用接口 ──────────────────────────

    def call(self, plugin_name: str, call_type: str, messages: List[Dict], **kwargs) -> str:
        if plugin_name in {"assistant", "builtin_chatbot"} and str(call_type).startswith("memory_"):
            raise RuntimeError("Generated chat memory is retired; no model call was made")
        started = time.monotonic()
        context = {"id": uuid.uuid4().hex, "plugin": plugin_name, "task": call_type, "attempts": 0,
                   "metadata": {"chat_name": kwargs.get("_mabobot_chat_name") or kwargs.get("_mabobot_chat_id"),
                                "user_id": kwargs.get("_mabobot_user_id"), "chat_type": kwargs.get("_mabobot_chat_type"),
                                "usage_scope": kwargs.get("_mabobot_usage_scope")}}
        token = _usage_call.set(context)
        try:
            return self._call_impl(plugin_name, call_type, messages, **kwargs)
        except Exception as exc:
            if not context["attempts"]:
                mapping = getattr(self, "config", {}).get("plugin_mappings", {}).get(plugin_name, {}).get(call_type, {})
                self._record_usage(plugin_name, call_type, str(mapping.get("primary") or "unknown"),
                                   None, time.monotonic() - started, success=False, metadata=context["metadata"], error=str(exc))
            raise
        finally:
            _usage_call.reset(token)

    def _call_impl(
        self,
        plugin_name: str,
        call_type: str,
        messages: List[Dict],
        **kwargs
    ) -> str:
        """
        统一调用接口

        Args:
            plugin_name: 组件名称（例如 ``assistant``）
            call_type:   调用类型 (如 "chat", "search", "vision", "judge")
            messages:    OpenAI 格式的消息数组
            **kwargs:    额外参数 (temperature, max_tokens, tools …)

        Returns:
            模型返回的文本内容
        """
        if plugin_name in {"assistant", "builtin_chatbot"} and call_type == "chat":
            raise ValueError(
                "AI 助手最终回复只能通过 CodexReplyGateway 调用，不能使用通用模型路由"
            )
        self._check_and_reload_if_modified()

        # ── 1. 获取配置 ──
        mapping = self._get_mapping(plugin_name, call_type)
        if not mapping:
            raise ValueError(f"❌ 未找到配置: {plugin_name}.{call_type}")

        primary_model_id = str(mapping.get("primary") or "").strip()
        if not primary_model_id:
            raise ValueError(f"❌ 未配置主模型: {plugin_name}.{call_type}")

        model_config = self.config["models"].get(primary_model_id)
        if not model_config:
            raise ValueError(f"❌ 模型不存在: {primary_model_id}")

        # ── 2. 从 kwargs 中提取特殊参数（不修改原始 kwargs）──
        caller_tools        = kwargs.get("tools")       # 调用方 tools（如 google_search）
        attachment_capture  = kwargs.get("_mabobot_attachment_capture")
        raw_input_files     = kwargs.get("_mabobot_input_files")
        input_files         = [
            copy.deepcopy(item)
            for item in (raw_input_files or [])
            if isinstance(item, dict) and item.get("path")
        ] if isinstance(raw_input_files, list) else []
        allow_image_input   = bool(kwargs.get("_mabobot_allow_image_input"))
        require_image_input = bool(kwargs.get("_mabobot_require_image_input"))
        disable_model_web_search = bool(
            kwargs.get("_mabobot_disable_model_web_search")
        )
        codex_chat_id       = str(kwargs.get("_mabobot_chat_id") or "").strip()
        codex_role_name     = str(kwargs.get("_mabobot_role_name") or "").strip()
        history_chat_name   = str(
            kwargs.get("_mabobot_chat_name") or codex_chat_id
        ).strip()
        history_mode        = str(
            kwargs.get("_mabobot_history_mode") or "full"
        ).strip().lower()
        usage_capture       = kwargs.get("_mabobot_usage_capture")
        codex_output_schema = (
            copy.deepcopy(kwargs.get("_mabobot_codex_output_schema"))
            if isinstance(kwargs.get("_mabobot_codex_output_schema"), dict)
            else None
        )
        history_metadata = {
            "chat_name": history_chat_name,
            "user_id": kwargs.get("_mabobot_user_id"),
            "chat_type": kwargs.get("_mabobot_chat_type"),
            "usage_scope": kwargs.get("_mabobot_usage_scope"),
            "role_name": codex_role_name,
            "history_mode": history_mode,
            "input_file_count": len(input_files),
            "_usage_capture": usage_capture,
        }
        codex_retry         = bool(kwargs.get("_mabobot_codex_retry"))
        codex_reasoning_effort = str(
            kwargs.get("_mabobot_codex_reasoning_effort") or "inherit"
        ).strip().lower()
        codex_reasoning_summary = str(
            kwargs.get("_mabobot_codex_reasoning_summary") or "inherit"
        ).strip().lower()
        codex_web_search_mode = str(
            kwargs.get("_mabobot_codex_web_search_mode") or "inherit"
        ).strip().lower()
        try:
            codex_timeout_seconds = max(0, int(kwargs.get("_mabobot_codex_timeout_seconds") or 0))
        except (TypeError, ValueError):
            codex_timeout_seconds = 0
        try:
            codex_max_turns = max(0, int(kwargs.get("_mabobot_codex_max_turns") or 0))
        except (TypeError, ValueError):
            codex_max_turns = 0
        codex_exec_fallback = bool(kwargs.get("_mabobot_codex_exec_fallback", True))
        mapping_overrides = mapping.get("override_params", {})

        # 映射覆盖参数来自用户配置；插件调用参数不能覆盖模型核心配置。
        extra_kwargs = dict(mapping_overrides)

        _MODEL_CONFIG_ONLY_KEYS = {"temperature", "max_tokens", "timeout"}
        _HANDLED_KEYS = frozenset({
            "tools",
            "_mabobot_attachment_capture",
            "_mabobot_input_files",
            "_mabobot_allow_image_input",
            "_mabobot_require_image_input",
            "_mabobot_disable_model_web_search",
            "_mabobot_chat_id",
            "_mabobot_chat_name",
            "_mabobot_user_id",
            "_mabobot_chat_type",
            "_mabobot_usage_scope",
            "_mabobot_role_name",
            "_mabobot_history_mode",
            "_mabobot_usage_capture",
            "_mabobot_codex_output_schema",
            "_mabobot_codex_retry",
            "_mabobot_codex_reasoning_effort",
            "_mabobot_codex_reasoning_summary",
            "_mabobot_codex_web_search_mode",
            "_mabobot_codex_timeout_seconds",
            "_mabobot_codex_max_turns",
            "_mabobot_codex_exec_fallback",
        } | _MODEL_CONFIG_ONLY_KEYS)
        for k, v in kwargs.items():
            if k not in _HANDLED_KEYS:
                extra_kwargs[k] = v

        # ── 3. 构建主模型参数 ──
        primary_params = self._build_single_model_params(
            model_cfg=model_config,
            messages=messages,
            caller_tools=caller_tools,
            extra_kwargs=extra_kwargs,
        )
        if disable_model_web_search:
            self._remove_model_native_web_search(primary_params)

        # ── 5. 构建 fallback 列表 ──
        fallback_models = mapping.get("fallback", [])
        fallback_list = []
        fallback_entries = []
        if fallback_models:
            for fb_id in fallback_models:
                if fb_id == primary_model_id:
                    continue
                fb_cfg = self.config["models"].get(fb_id)
                if not fb_cfg:
                    continue
                # fallback 也使用同样的干净构建逻辑，核心参数使用 fallback 自身模型配置。
                fb_params = self._build_single_model_params(
                    model_cfg=fb_cfg,
                    messages=messages,
                    caller_tools=caller_tools,
                    extra_kwargs=extra_kwargs,
                )
                if disable_model_web_search:
                    self._remove_model_native_web_search(fb_params)
                if self._model_circuit_open(str(fb_params.get("model") or fb_id)):
                    logger.warning("⚡ 跳过熔断中的备用模型: %s", fb_params.get("model") or fb_id)
                    continue
                fallback_list.append(fb_params)
                fallback_entries.append(
                    {
                        "model_id": fb_id,
                        "model_config": fb_cfg,
                        "params": fb_params,
                    }
                )

            if fallback_list:
                has_local_codex_fallback = any(
                    self._is_local_codex_model(entry["model_config"])
                    for entry in fallback_entries
                )
                use_litellm_native_fallbacks = (
                    not getattr(self, "usage_service", None)
                    and not self._is_local_codex_model(model_config)
                    and not has_local_codex_fallback
                )
                if (
                    use_litellm_native_fallbacks
                    and self._model_circuit_open(str(primary_params.get("model") or primary_model_id))
                ):
                    skipped_model = primary_params.get("model") or primary_model_id
                    primary_params = copy.deepcopy(fallback_list.pop(0))
                    logger.warning(
                        "⚡ 主模型 %s 处于熔断期，直接切换到 %s",
                        skipped_model,
                        primary_params.get("model"),
                    )
                if fallback_list and use_litellm_native_fallbacks:
                    primary_params["fallbacks"] = fallback_list
            else:
                has_local_codex_fallback = False
        else:
            has_local_codex_fallback = False

        # ── 6. 代理检查（缓存机制：无变化时为 no-op）──
        self.apply_proxy_env_vars()

        # ── 7. 执行调用 ──
        timeout_label = primary_params.get("timeout", "model/default")
        logger.info(
            f"🤖 LLM 调用: {plugin_name}.{call_type} -> "
            f"{primary_model_id} ({model_config['model']}) [timeout={timeout_label}]"
        )

        def _do_call(params: dict, billing_config=None) -> tuple:
            """
            单次 litellm.completion() 调用。
            深拷贝 params 防止 LiteLLM 的 fallback 机制污染原始参数字典。
            """
            t0 = time.time()
            safe_params = copy.deepcopy(params)
            safe_params["drop_params"] = True
            configured_supports_vision = bool(safe_params.pop("_mabobot_supports_vision", False))
            fallback_vision_flags = {}
            if "fallbacks" in safe_params and isinstance(safe_params["fallbacks"], list):
                for fb in safe_params["fallbacks"]:
                    fallback_vision_flags[id(fb)] = bool(fb.pop("_mabobot_supports_vision", False))
            self._strip_internal_provider_params(safe_params)

            # 视觉降级：主模型不支持视觉时剔除图片（fallback 由 litellm 处理）。
            # 明确要求图片输入的能力必须保留图片，并由模型调用结果决定成败。
            target_model = safe_params.get("model", "")
            try:
                supports_vision = configured_supports_vision or litellm.supports_vision(target_model) or "grok" in target_model.lower()
            except Exception:
                supports_vision = configured_supports_vision or "grok" in target_model.lower()

            # 特殊处理：不仅检查主模型，如果 call_type 需要剔除图片，对所有 fallback 也要剔除
            if not require_image_input and call_type != "vision":
                # 处理主模型
                if "messages" in safe_params and safe_params["messages"]:
                    if "perplexity/" in target_model or not supports_vision:
                        safe_params["messages"] = self._strip_images_from_messages(
                            safe_params["messages"]
                        )

                # 处理 fallback 模型
                if "fallbacks" in safe_params and isinstance(safe_params["fallbacks"], list):
                    for fb in safe_params["fallbacks"]:
                        fb_model = fb.get("model", "")
                        fb_configured_supports_vision = fallback_vision_flags.get(id(fb), False)
                        try:
                            fb_supports_vision = fb_configured_supports_vision or litellm.supports_vision(fb_model) or "grok" in fb_model.lower()
                        except Exception:
                            fb_supports_vision = fb_configured_supports_vision or "grok" in fb_model.lower()

                        if "perplexity/" in fb_model or not fb_supports_vision:
                            if "messages" in fb and fb["messages"]:
                                fb["messages"] = self._strip_images_from_messages(fb["messages"])

                        # 特殊处理：Grok 视觉格式转换 (自建反向 Grok 兼容性)
                        if "grok" in fb_model.lower():
                             if "messages" in fb and fb["messages"]:
                                fb["messages"] = self._transform_messages_for_grok(fb["messages"])

            # ── 针对 Grok 模型族的主消息格式转换 ──
            if "grok" in target_model.lower():
                if "messages" in safe_params and safe_params["messages"]:
                    safe_params["messages"] = self._transform_messages_for_grok(
                        safe_params["messages"]
                    )

            resp = self._usage_completion(_billing_config=billing_config, **safe_params)
            return resp, time.time() - t0

        def _try_single_params(
            params: dict,
            candidate_model_config: Optional[dict] = None,
        ) -> tuple:
            active_model_config = candidate_model_config or model_config
            max_empty_retries = 3
            last_resp, last_time, last_result = None, 0, ""

            for attempt in range(max_empty_retries):
                try:
                    response, response_time = _do_call(params, active_model_config)
                except Exception as direct_exc:
                    proxy_url = self._get_proxy_url()
                    if proxy_url and self._is_network_error(direct_exc):
                        logger.warning(
                            f"⚠️ 直连失败 ({type(direct_exc).__name__}: {str(direct_exc)[:80]})，"
                            "代理已通过环境变量配置（地址已隐藏），重试中..."
                        )
                        response, response_time = _do_call(params, active_model_config)
                        logger.info(f"✅ 代理重试成功 ({response_time:.2f}s)")
                    else:
                        raise

                # ── 8. Kimi 二阶段 web search（移出 _do_call，避免代理重试重跑 stage-1）──
                response = self._handle_kimi_stage2(response, params, params.get("timeout"))

                result = response.choices[0].message.content
                if result:
                    result = result.strip()
                else:
                    result = ""

                if not result:
                    result = self._recover_empty_response_with_stream(
                        response=response,
                        model_config=active_model_config,
                        primary_params=params,
                        call_timeout=params.get("timeout"),
                    )

                if self._is_retriable_content_failure(result):
                    actual_model = self._resolve_actual_model(response, active_model_config)
                    logger.warning(
                        f"⚠️ LLM 返回可重试错误文本 [{actual_model}]，准备触发 fallback: "
                        f"{result[:160]}"
                    )
                    return response, response_time, ""

                if result:
                    return response, response_time, result

                # 记录最后一次结果供兜底返回
                last_resp, last_time, last_result = response, response_time, result

                if attempt < max_empty_retries - 1:
                    actual_model = self._resolve_actual_model(response, active_model_config)
                    logger.warning(
                        f"⚠️ LLM 返回空响应 [{actual_model}]，"
                        f"正在进行第 {attempt + 2}/{max_empty_retries} 次重试..."
                    )
                    time.sleep(1)  # 增加 1 秒延迟，避免过于频繁地请求不稳定服务器

            return last_resp, last_time, last_result

        def _complete_fallback_success(
            entry: Dict[str, Any],
            response,
            response_time: float,
            result: str,
        ) -> str:
            candidate_config = entry["model_config"]
            if self._is_local_codex_model(candidate_config):
                self.last_used_model = self._resolve_local_codex_model(response, candidate_config)
            else:
                self.last_used_model = self._resolve_actual_model(
                    response,
                    candidate_config,
                )

            response_attachments = self._extract_attachments_from_response(response)
            if isinstance(attachment_capture, list):
                attachment_capture.clear()
                attachment_capture.extend(response_attachments)
            token_usage = self._extract_token_usage(response)
            self._record_stats(
                plugin_name,
                call_type,
                entry["model_id"],
                response,
                response_time,
                token_usage=token_usage, metadata=history_metadata,
            )
            self._record_call_history(
                plugin_name,
                call_type,
                primary_model_id,
                messages,
                result,
                response_time,
                self.last_used_model,
                token_usage.get("total_tokens", 0),
                success=True,
                reasoning_text=self._extract_reasoning_from_response(response),
                token_usage=token_usage,
                metadata=history_metadata,
                response_attachments=response_attachments,
            )
            logger.info(
                f"✅ Fallback 成功: {len(result)} 字符 ({response_time:.2f}s) "
                f"[实际模型: {self.last_used_model}]"
            )
            return result

        def _attempt_managed_fallbacks(
            entries: List[Dict[str, Any]],
        ) -> Optional[str]:
            """Run fallbacks in configured order across LiteLLM/Codex runtimes."""
            for entry in entries:
                candidate_config = entry["model_config"]
                candidate_params = copy.deepcopy(entry["params"])
                candidate_params.pop("fallbacks", None)
                if input_files and not self._is_local_codex_model(candidate_config):
                    logger.warning(
                        "⚠️ 文件输入请求跳过不支持本地文件的非 Codex fallback: %s",
                        candidate_params.get("model") or entry["model_id"],
                    )
                    continue
                logger.info(
                    "🔄 应用层 Fallback 重试: %s",
                    candidate_params.get("model") or entry["model_id"],
                )
                try:
                    if self._is_local_codex_model(candidate_config):
                        response, response_time, result = self._usage_local_codex(
                            model_config=candidate_config,
                            messages=messages,
                            params=candidate_params,
                            allow_image_input=allow_image_input,
                            chat_id=codex_chat_id,
                            role_name=codex_role_name,
                            retry=codex_retry,
                            codex_reasoning_effort=codex_reasoning_effort,
                            codex_reasoning_summary=codex_reasoning_summary,
                            codex_web_search_mode=(
                                "disabled"
                                if disable_model_web_search
                                else codex_web_search_mode
                            ),
                            codex_timeout_seconds=codex_timeout_seconds,
                            codex_max_turns=codex_max_turns,
                            codex_exec_fallback=codex_exec_fallback,
                            codex_output_schema=codex_output_schema,
                            input_files=input_files,
                        )
                    else:
                        response, response_time, result = _try_single_params(
                            candidate_params,
                            candidate_config,
                        )
                    if result:
                        return _complete_fallback_success(
                            entry,
                            response,
                            response_time,
                            result,
                        )
                    logger.warning(
                        "⚠️ Fallback 模型 %s 返回空内容",
                        candidate_params.get("model") or entry["model_id"],
                    )
                except Exception as fallback_exc:
                    logger.warning(
                        "⚠️ Fallback 模型 %s 调用失败: %s",
                        candidate_params.get("model") or entry["model_id"],
                        fallback_exc,
                    )
            return None

        if (getattr(self, "usage_service", None) and fallback_entries
                and self._model_circuit_open(str(primary_params.get("model") or primary_model_id))):
            fallback_result = _attempt_managed_fallbacks(fallback_entries)
            if fallback_result is not None:
                return fallback_result
            raise RuntimeError("Primary model circuit is open and all fallback models failed")

        if self._is_local_codex_model(model_config):
            try:
                response, response_time, result = self._usage_local_codex(
                    model_config=model_config,
                    messages=messages,
                    params=primary_params,
                    allow_image_input=allow_image_input,
                    chat_id=codex_chat_id,
                    role_name=codex_role_name,
                    retry=codex_retry,
                    codex_reasoning_effort=codex_reasoning_effort,
                    codex_reasoning_summary=codex_reasoning_summary,
                    codex_web_search_mode=codex_web_search_mode,
                    codex_timeout_seconds=codex_timeout_seconds,
                    codex_max_turns=codex_max_turns,
                    codex_exec_fallback=codex_exec_fallback,
                    codex_output_schema=codex_output_schema,
                    input_files=input_files,
                )
                if not result:
                    raise ValueError("Local Codex CLI returned empty response")

                self.last_used_model = self._resolve_local_codex_model(response, model_config)
                response_attachments = self._extract_attachments_from_response(response)
                if isinstance(attachment_capture, list):
                    attachment_capture.clear()
                    attachment_capture.extend(response_attachments)
                token_usage = self._extract_token_usage(response)
                self._record_stats(
                    plugin_name,
                    call_type,
                    primary_model_id,
                    response,
                    response_time,
                    token_usage=token_usage, metadata=history_metadata,
                )
                self._record_call_history(
                    plugin_name,
                    call_type,
                    primary_model_id,
                    messages,
                    result,
                    response_time,
                    self.last_used_model,
                    token_usage.get("total_tokens", 0),
                    success=True,
                    reasoning_text=self._extract_reasoning_from_response(response),
                    token_usage=token_usage,
                    metadata=history_metadata,
                    response_attachments=response_attachments,
                )
                logger.info(
                    f"✅ Local Codex CLI 成功: {len(result)} 字符 ({response_time:.2f}s) "
                    f"[实际模型: {self.last_used_model}]"
                )
                return result
            except Exception as local_exc:
                self._record_error(plugin_name, call_type, primary_model_id)
                self._record_call_history(
                    plugin_name,
                    call_type,
                    primary_model_id,
                    messages,
                    "",
                    0,
                    f"local_codex/{model_config.get('model', primary_model_id)}",
                    0,
                    success=False,
                    error=str(local_exc),
                    metadata=history_metadata,
                )
                logger.error(f"❌ Local Codex CLI 调用失败: {plugin_name}.{call_type} -> {local_exc}")
                if not fallback_entries:
                    raise
                logger.info("🔄 Local Codex CLI 失败，尝试 fallback 模型")
                fallback_result = _attempt_managed_fallbacks(fallback_entries)
                if fallback_result is not None:
                    return fallback_result
                raise local_exc

        try:
            # 1. 尝试主模型（内部已包含 LiteLLM 针对 API 异常的 fallback）
            try:
                response, response_time, result = _try_single_params(primary_params)
            except Exception:
                # LiteLLM 不认识项目内部的 local_codex_cli provider。
                # 只要链上含本地 Codex，就由应用层按配置顺序调度全部 fallback。
                if has_local_codex_fallback or getattr(self, "usage_service", None):
                    fallback_result = _attempt_managed_fallbacks(fallback_entries)
                    if fallback_result is not None:
                        return fallback_result
                raise

            # 2. 如果结果依然为空，说明 API 成功但没有内容或返回了可重试错误文本，手动执行一次 fallback
            if not result:
                actual_model = self._resolve_actual_model(response, model_config)
                logger.warning(f"⚠️ LLM 成功返回但内容不可用 [{actual_model}]，准备执行手动 fallback...")

                fallback_result = _attempt_managed_fallbacks(fallback_entries)
                if fallback_result is not None:
                    return fallback_result

                self.last_used_model = self._resolve_actual_model(response, model_config)
                error_msg = (
                    f"LLM 返回空响应/失败内容且所有 Fallback 均失效 "
                    f"[实际模型: {self.last_used_model}]"
                )
                raise ValueError(error_msg)

            # 记录实际使用的模型名（fallback 时 resp.model 是真正生效的模型）
            self.last_used_model = self._resolve_actual_model(response, model_config)
            response_attachments = self._extract_attachments_from_response(response)
            if isinstance(attachment_capture, list):
                attachment_capture.clear()
                attachment_capture.extend(response_attachments)

            # ── 9. 统计 & 返回 ──
            token_usage = self._extract_token_usage(response)
            self._record_stats(
                plugin_name, call_type, primary_model_id,
                response, response_time, token_usage=token_usage, metadata=history_metadata,
            )

            # 记录调用历史（请求+响应，最多若干条/来源）
            self._record_call_history(
                plugin_name, call_type, primary_model_id,
                messages, result, response_time,
                self.last_used_model, token_usage.get("total_tokens", 0), success=True,
                reasoning_text=self._extract_reasoning_from_response(response),
                token_usage=token_usage,
                metadata=history_metadata,
                response_attachments=response_attachments,
            )

            logger.info(
                f"✅ LLM 成功: {len(result)} 字符 ({response_time:.2f}s) "
                f"[实际模型: {self.last_used_model}]"
            )
            return result

        except Exception as e:
            self._record_error(plugin_name, call_type, primary_model_id)
            self._record_call_history(
                plugin_name, call_type, primary_model_id,
                messages, "", 0,
                model_config.get("model", "unknown"), 0,
                success=False, error=str(e),
                metadata=history_metadata,
            )
            logger.error(f"❌ LLM 调用失败: {plugin_name}.{call_type} -> {e}")
            raise

    # ──────────────────────── Kimi 二阶段处理 ────────────────────────

    def _recover_empty_response_with_stream(
        self,
        response,
        model_config: dict,
        primary_params: dict,
        call_timeout: Optional[int],
    ) -> str:
        """
        某些模型/网关在非流式 chat.completions 下会返回空 content，
        但在 stream=True 时能正常逐块返回文本。

        当前已确认 https://quiz.playoffer.cn/codex 的 gpt-5.4 存在该行为。
        DeepSeek 官方 deepseek-v4-pro 也观察到非流式成功但 content 为空、
        同参数 stream=True 可恢复 delta.content 的情况。
        这里做一次定向流式补救，避免把“服务端非流式序列化问题”误判成模型无回复。
        """
        actual_model = self._resolve_actual_model(response, model_config)
        provider = (
            getattr(getattr(response, "_hidden_params", {}), "get", lambda *_: None)("custom_llm_provider")
            or model_config.get("custom_llm_provider")
            or model_config.get("provider")
            or ""
        )
        api_base = self._resolve_env(model_config.get("api_base")) or primary_params.get("api_base") or ""
        provider_l = str(provider or "").lower()
        actual_model_l = str(actual_model or "").lower()
        param_model_l = str(primary_params.get("model") or "").lower()
        api_base_l = str(api_base or "").lower()
        is_official_deepseek = (
            (
                param_model_l.startswith("deepseek/")
                or actual_model_l.startswith("deepseek/")
                or provider_l == "deepseek"
            )
            and "openrouter" not in param_model_l
            and "openrouter" not in api_base_l
            and provider_l != "custom_openai"
        )
        # 判定是否值得尝试流式补救：
        # 1. 明确已知的“非流式返回空”的网关 (quiz.playoffer / 100.121.xx)
        # 2. 所有的 custom_openai 兼容提供商
        # 3. 任何包含 grok 字样的模型 (针对用户反馈)
        # 4. DeepSeek 官方 API（已实测同参数 stream=True 可恢复文本）
        should_try_stream = (
            provider_l in ["custom_openai"] or
            "quiz.playoffer.cn/codex" in api_base or
            "100.121.36.39" in api_base or
            "grok" in actual_model_l or
            is_official_deepseek
        )

        if not should_try_stream:
            return ""

        logger.warning(
            f"⚠️ 检测到非流式空响应，尝试用 stream=True 重取文本 [{actual_model}]"
        )

        try:
            stream_params = copy.deepcopy(primary_params)
            if call_timeout is not None:
                stream_params["timeout"] = call_timeout
            stream_params["drop_params"] = True
            stream_params["stream"] = True
            stream_params.pop("fallbacks", None)
            # primary_params 仍包含仅供 Mabobot 内部路由使用的元数据；正常
            # 非流式调用会在 _do_call 中清理，流式补救也必须遵循同一边界。
            self._strip_internal_provider_params(stream_params)

            chunks = self._usage_completion(_billing_config=model_config, **stream_params)
            parts = []
            for chunk in chunks:
                if not getattr(chunk, "choices", None):
                    continue
                delta = getattr(chunk.choices[0], "delta", None)
                text = getattr(delta, "content", None) if delta else None
                if text:
                    parts.append(text)

            recovered = "".join(parts).strip()
            if recovered:
                try:
                    setattr(response, "_stream_recovered_empty_content", True)
                    setattr(response, "_stream_recovered_chars", len(recovered))
                except Exception:
                    pass
                logger.info(
                    f"✅ 已通过流式补救恢复文本: {len(recovered)} 字符 [实际模型: {actual_model}]"
                )
            return recovered
        except Exception as stream_exc:
            logger.warning(
                f"⚠️ 流式补救失败 [{actual_model}]: {type(stream_exc).__name__}: {stream_exc}"
            )
            return ""

    # ──────────────────────── Kimi 二阶段处理 ────────────────────────

    def _handle_kimi_stage2(self, resp, primary_params: dict, call_timeout: Optional[int]):
        """
        处理 Kimi (Moonshot) 原生 web search 的二阶段调用。
        当第一阶段的 finish_reason 为 "tool_calls" 且包含 $web_search 时，
        发起第二阶段请求以获取真实的联网结果。

        设计：与 _do_call 完全解耦，代理重试只重跑 stage-1，不影响此处。
        """
        resp_model_str = (getattr(resp, "model", "") or "").lower()
        primary_model_str = primary_params.get("model", "")
        is_kimi_resp = (
            primary_model_str.startswith("moonshot/")
            or "moonshot" in resp_model_str
            or "kimi" in resp_model_str
        )

        if not is_kimi_resp:
            return resp
        if not (resp.choices and resp.choices[0].finish_reason == "tool_calls"):
            return resp

        msg = resp.choices[0].message
        if not any(
            t.function.name == "$web_search"
            for t in getattr(msg, "tool_calls", [])
        ):
            return resp

        logger.info("🔍 Kimi 请求原生 Web Search 插件，正在获取第二阶段联网结果...")

        # 找到 Kimi 的完整配置（支持 Kimi 是 fallback 模型的场景）
        kimi_model, kimi_api_key, kimi_api_base, kimi_temperature = \
            self._resolve_kimi_config(primary_model_str, primary_params)

        # 构建第二阶段参数（不含 tools / fallbacks）
        stage2_messages = list(primary_params["messages"])
        stage2_messages.append(msg)
        for tool_call in msg.tool_calls:
            if tool_call.function.name == "$web_search":
                stage2_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.function.name,
                    "content": tool_call.function.arguments,
                })

        kimi_params: dict = {
            "model": kimi_model,
            "messages": stage2_messages,
        }
        if call_timeout is not None:
            kimi_params["timeout"] = call_timeout
        if kimi_api_key:
            kimi_params["api_key"] = kimi_api_key
        if kimi_api_base:
            kimi_params["api_base"] = kimi_api_base
        if kimi_temperature is not None:
            kimi_params["temperature"] = kimi_temperature

        logger.info(
            f"🔑 [Kimi-Stage2] model={kimi_model!r} | "
            f"api_key={'set(' + kimi_api_key[:8] + '...)' if kimi_api_key else 'NONE'} | "
            f"api_base={kimi_api_base!r}"
        )

        try:
            resp = self._usage_completion(**kimi_params)
        except Exception as exc1:
            logger.warning(f"⚠️ Kimi 第二阶段失败 ({type(exc1).__name__}: {str(exc1)[:80]})，重试一次...")
            try:
                resp = self._usage_completion(**kimi_params)
            except Exception as exc2:
                logger.error(f"❌ Kimi 第二阶段彻底失败，降级为空结果: {exc2}")
                # 构造最小化的合法 ModelResponse 避免上层崩溃
                from litellm.utils import ModelResponse, Choices, Message as LLMMessage
                empty_resp = ModelResponse()
                empty_resp.choices = [
                    Choices(
                        finish_reason="stop",
                        index=0,
                        message=LLMMessage(role="assistant", content=""),
                    )
                ]
                resp = empty_resp

        return resp

    def _resolve_kimi_config(self, primary_model_str: str, primary_params: dict):
        """
        解析 Kimi (Moonshot) 的完整连接配置。
        支持 Kimi 是主模型或 fallback 模型两种场景。
        返回 (model, api_key, api_base, temperature)
        """
        if primary_model_str.startswith("moonshot/"):
            # Kimi 是主模型
            return (
                primary_model_str,
                primary_params.get("api_key"),
                primary_params.get("api_base"),
                primary_params.get("temperature"),
            )

        # Kimi 是 fallback：从 config["models"] 实时解析
        for _mid, _mcfg in self.config.get("models", {}).items():
            if "moonshot" in _mcfg.get("model", ""):
                return (
                    _mcfg["model"],
                    self._resolve_env(_mcfg["api_key"]) if "api_key" in _mcfg else None,
                    self._resolve_env(_mcfg["api_base"]) if "api_base" in _mcfg else None,
                    _mcfg.get("temperature"),
                )

        # 最终兜底（理论上不应到达这里）
        resp_model = primary_params.get("model", "kimi-k2-turbo-preview")
        kimi_model = resp_model if "moonshot" in resp_model else f"moonshot/{resp_model}"
        return kimi_model, None, None, None

    # ─────────────────────── 工具方法 ──────────────────────────

    @staticmethod
    def _strip_images_from_messages(messages: List[Dict]) -> List[Dict]:
        """从消息列表中剔除图片内容（用于不支持视觉的模型）"""
        cleaned = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                text_only = [
                    item for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                if text_only:
                    new_msg = copy.deepcopy(msg)
                    new_msg["content"] = text_only
                    cleaned.append(new_msg)
            else:
                cleaned.append(msg)
        return cleaned

    @staticmethod
    def _transform_messages_for_grok(messages: List[Dict]) -> List[Dict]:
        """
        针对自建 Grok 模型转换消息格式。
        将 {"type": "image_url", "image_url": {"url": "data:..."}}
        转换为 {"type": "file", "file": {"file_data": "data:..."}}
        """
        if not messages:
            return messages

        new_messages = copy.deepcopy(messages)
        transformed_count = 0
        for msg in new_messages:
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "image_url":
                        img_url = item.get("image_url", {}).get("url")
                        if img_url:
                            item["type"] = "file"
                            item["file"] = {"file_data": img_url}
                            item.pop("image_url", None)
                            transformed_count += 1

        if transformed_count > 0:
            logger.info(f"✨ Transformed {transformed_count} vision items for Grok format")
        return new_messages

    @staticmethod
    def _resolve_local_codex_model(response, model_config: dict) -> str:
        """Use the runtime result, including for fallback and connectivity tests."""
        backend = str(response.get("backend") or "runtime") if isinstance(response, dict) else "runtime"
        model = response.get("model") if isinstance(response, dict) else None
        return f"local_codex_{backend}/{model or model_config.get('model') or 'unknown'}"

    @staticmethod
    def _resolve_actual_model(response, model_config: dict) -> str:
        """从 LiteLLM response 中还原实际使用的完整模型名（含 provider 前缀）"""
        actual = getattr(response, "model", None) or model_config["model"]
        provider = model_config.get("custom_llm_provider") or model_config.get("provider") or ""
        try:
            provider = response._hidden_params.get("custom_llm_provider", "") or provider
        except Exception:
            pass
        if provider and "/" not in actual:
            actual = f"{provider}/{actual}"
        return actual

    def _get_mapping(self, plugin_name: str, call_type: str) -> Optional[Dict]:
        """获取插件调用映射"""
        plugin_mappings = self.config.get("plugin_mappings", {}).get(plugin_name, {})
        return plugin_mappings.get(call_type)

    def _get_proxy_url(self) -> Optional[str]:
        """从数据库读取代理配置，返回代理 URL 或 None（禁用）"""
        proxy_url = get_setting("LLM_PROXY_URL", "")
        if proxy_url and isinstance(proxy_url, str) and proxy_url.strip():
            return proxy_url.strip()
        return None

    def _is_network_error(self, exc: Exception) -> bool:
        """判断异常是否为网络连接级别错误（值得用代理重试）"""
        err_str = str(exc).lower()
        network_keywords = [
            "connection", "timeout", "ssl", "certificate", "network",
            "refused", "unreachable", "handshake", "reset by peer",
            "eof occurred", "timed out", "name or service not known",
            "temporary failure in name resolution", "no route to host",
            "server disconnected",
        ]
        if any(kw in err_str for kw in network_keywords):
            return True

        retriable_types = [ConnectionError, TimeoutError, ssl.SSLError]
        if _httpx:
            retriable_types += [
                _httpx.ConnectError, _httpx.TimeoutException,
                _httpx.RemoteProtocolError, _httpx.ReadError,
            ]
        if _requests:
            retriable_types += [
                _requests.exceptions.ConnectionError,
                _requests.exceptions.Timeout,
                _requests.exceptions.SSLError,
            ]
        return isinstance(exc, tuple(retriable_types))

    @staticmethod
    def _is_retriable_content_failure(content: str) -> bool:
        """
        判断模型是否把上游临时故障作为普通文本返回。

        这类返回不是 API exception，LiteLLM 不会自动 fallback；这里把短错误
        文本视为失败，交给现有手动 fallback 逻辑处理。
        """
        if not content or not isinstance(content, str):
            return False

        normalized = " ".join(content.strip().lower().split())
        if len(normalized) > 500:
            return False

        return any(marker in normalized for marker in _RETRIABLE_CONTENT_FAILURE_MARKERS)

    def _resolve_env(self, value: str) -> str:
        """
        解析环境变量 (格式: env::VAR_NAME)
        优先从数据库读取（使用 get_setting），如果没有则从环境变量读取
        """
        if isinstance(value, str) and value.startswith("env::"):
            var_name = value[5:]
            db_value = get_setting(var_name)
            if db_value:
                return db_value
            env_value = os.getenv(var_name, "")
            if not env_value:
                logger.warning(f"⚠️ 配置项未设置: {var_name} (数据库和环境变量都没有)")
            return env_value
        return value

    def _run_coroutine_sync(self, coro):
        """Run a coroutine from sync code; use a helper thread if an event loop is already active."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        result_box: Dict[str, Any] = {}
        error_box: Dict[str, BaseException] = {}

        def runner():
            try:
                result_box["result"] = asyncio.run(coro)
            except BaseException as exc:
                error_box["error"] = exc

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join()
        if "error" in error_box:
            raise error_box["error"]
        return result_box.get("result")

    @staticmethod
    def _is_local_codex_model(model_config: dict) -> bool:
        provider = str(model_config.get("provider") or model_config.get("custom_llm_provider") or "").lower()
        api_base = str(model_config.get("api_base") or "").lower()
        return provider in {"local_codex", "local_codex_cli"} or "/api/codex/" in api_base or api_base.endswith("/api/codex/v1")

    def _call_local_codex(
        self,
        model_config: dict,
        messages: List[Dict],
        params: dict,
        allow_image_input: bool,
        chat_id: str = "",
        role_name: str = "",
        retry: bool = False,
        codex_reasoning_effort: str = "inherit",
        codex_reasoning_summary: str = "inherit",
        codex_web_search_mode: str = "inherit",
        codex_timeout_seconds: int = 0,
        codex_max_turns: int = 0,
        codex_exec_fallback: bool = True,
        codex_output_schema: Optional[dict] = None,
        input_files: Optional[List[Dict[str, Any]]] = None,
    ) -> tuple:
        """Use local Codex as a stateless LLM provider, independent of chat Profiles."""
        from app.services.agent_runtime import get_agent_runtime

        extra_body = copy.deepcopy(params.get("extra_body") or {})
        timeout = int(params.get("timeout") or model_config.get("timeout") or 600)
        payload = {
            "model": params.get("model") or model_config.get("model") or "gpt-5.6-sol",
            "timeout": timeout,
            "messages": messages,
            "extra_body": extra_body,
            "codex_text_only": True,
            "codex_isolated_workdir": True,
            "codex_sandbox": "read-only",
        }
        try:
            configured_context_window = int(
                model_config.get("context_window")
                or model_config.get("context_window_tokens")
                or 0
            )
        except (TypeError, ValueError):
            configured_context_window = 0
        if configured_context_window >= 4096:
            payload["codex_model_context_window"] = configured_context_window
        configured_reasoning_effort = str(
            model_config.get("codex_reasoning_effort") or ""
        ).strip().lower()
        if configured_reasoning_effort:
            payload["reasoning_effort"] = configured_reasoning_effort
        if "codex_web_search" in model_config:
            payload["codex_web_search"] = model_config.get("codex_web_search")
        configured_sandbox = str(
            model_config.get("codex_sandbox") or ""
        ).strip().lower()
        if configured_sandbox:
            payload["codex_sandbox"] = configured_sandbox
        if "codex_isolated_workdir" in model_config:
            payload["codex_isolated_workdir"] = model_config.get(
                "codex_isolated_workdir"
            )
        response_format = params.get("response_format") or model_config.get("response_format") or {}
        if not codex_output_schema and isinstance(response_format, dict):
            schema_config = response_format.get("json_schema")
            if response_format.get("type") == "json_schema" and isinstance(schema_config, dict):
                codex_output_schema = schema_config.get("schema")
        if codex_output_schema:
            payload["output_schema"] = copy.deepcopy(codex_output_schema)
        if input_files:
            payload["mabobot_input_files"] = copy.deepcopy(input_files)
        # Preserve direct top-level flags if present.
        for key in ("reasoning_effort", "web_search", "codex_web_search"):
            if key in params:
                payload[key] = params[key]
        if codex_reasoning_effort != "inherit":
            payload["reasoning_effort"] = codex_reasoning_effort
        if codex_reasoning_summary != "inherit":
            payload["codex_reasoning_summary"] = codex_reasoning_summary
        if codex_web_search_mode != "inherit":
            payload["codex_web_search"] = codex_web_search_mode
        if codex_timeout_seconds > 0:
            timeout = codex_timeout_seconds
            payload["timeout"] = timeout
        if allow_image_input:
            payload["mabobot_allow_image_input"] = True
            payload.setdefault("extra_body", {})["mabobot_allow_image_input"] = True

        payload.setdefault("reasoning_effort", extra_body.get("reasoning_effort") or "medium")
        payload.setdefault(
            "codex_web_search",
            payload.get("web_search", extra_body.get("codex_web_search", extra_body.get("web_search", False))),
        )
        if chat_id:
            payload["codex_source_chat_name"] = chat_id
        t0 = time.time()
        runtime = get_agent_runtime()
        response = runtime.run(
            payload,
            profile_name="batch",
            allow_exec_fallback=codex_exec_fallback,
        )
        response_time = time.time() - t0
        choices = response.get("choices") or [] if isinstance(response, dict) else []
        message = (choices[0].get("message") if choices else {}) or {}
        result = str(message.get("content") or "").strip()
        return response, response_time, result

    # ─────────────────────── 统计 ────────────────────────────

    def _extract_reasoning_from_response(self, response) -> str:
        """尽力提取供应商实际返回的 reasoning/thinking 文本。"""
        if not response:
            return ""

        def read(obj, key):
            if obj is None:
                return None
            if isinstance(obj, dict):
                return obj.get(key)
            return getattr(obj, key, None)

        def stringify(value) -> str:
            if value is None:
                return ""
            if isinstance(value, str):
                return value.strip()
            if isinstance(value, list):
                parts = [stringify(item) for item in value]
                return "\n".join(part for part in parts if part).strip()
            if isinstance(value, dict):
                for key in ("text", "content", "reasoning", "summary"):
                    text = stringify(value.get(key))
                    if text:
                        return text
                try:
                    return json.dumps(value, ensure_ascii=False, indent=2)
                except Exception:
                    return str(value)
            return str(value).strip()

        choices = read(response, "choices") or []
        if not choices:
            return ""

        first_choice = choices[0]
        message = read(first_choice, "message") or read(first_choice, "delta")
        carriers = [message, first_choice]
        for carrier in (message, first_choice):
            carriers.extend([
                read(carrier, "additional_kwargs"),
                read(carrier, "model_extra"),
                read(carrier, "provider_specific_fields"),
            ])

        for carrier in carriers:
            for field in ("reasoning_content", "reasoning", "thinking", "reasoning_details"):
                text = stringify(read(carrier, field))
                if text:
                    return text

        return ""

    def _extract_attachments_from_response(self, response) -> List[Dict[str, Any]]:
        """从兼容 OpenAI 的扩展响应中提取可选附件。"""
        if not response:
            return []

        def read(obj, key):
            if obj is None:
                return None
            if isinstance(obj, dict):
                return obj.get(key)
            getter = getattr(obj, "get", None)
            if callable(getter):
                try:
                    value = getter(key)
                    if value is not None:
                        return value
                except Exception:
                    pass
            return getattr(obj, key, None)

        carriers = [response]
        choices = read(response, "choices") or []
        if choices:
            first_choice = choices[0]
            message = read(first_choice, "message") or read(first_choice, "delta")
            carriers.extend([first_choice, message])
            for carrier in (message, first_choice):
                carriers.extend([
                    read(carrier, "additional_kwargs"),
                    read(carrier, "model_extra"),
                    read(carrier, "provider_specific_fields"),
                ])

        attachments: List[Dict[str, Any]] = []
        seen = set()
        for carrier in carriers:
            raw_items = read(carrier, "attachments")
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                path = item.get("path")
                if not path or path in seen:
                    continue
                seen.add(path)
                attachments.append(dict(item))
        return attachments

    def _extract_token_usage(self, response) -> dict:
        """从模型返回的 usage 中提取可选 token 明细；缺字段时安全退化。"""
        usage = None
        if response:
            if isinstance(response, dict):
                usage = response.get("usage")
            else:
                usage = getattr(response, "usage", None)
        if not usage:
            return {}

        total_tokens = self._usage_value(usage, "total_tokens")
        prompt_tokens = self._usage_value(usage, "prompt_tokens")
        completion_tokens = self._usage_value(usage, "completion_tokens")
        if not prompt_tokens:
            prompt_tokens = self._usage_value(usage, "input_tokens")
        if not completion_tokens:
            completion_tokens = self._usage_value(usage, "output_tokens")

        prompt_details = self._usage_value(usage, "prompt_tokens_details", {}) or {}
        completion_details = self._usage_value(usage, "completion_tokens_details", {}) or {}

        cached_tokens = (
            self._usage_value(prompt_details, "cached_tokens")
            or self._usage_value(usage, "cached_tokens")
            or self._usage_value(usage, "cache_read_input_tokens")
            or self._usage_value(usage, "prompt_cache_hit_tokens")
        )
        cache_miss_tokens = (
            self._usage_value(usage, "prompt_cache_miss_tokens")
            or self._usage_value(usage, "cache_miss_input_tokens")
        )
        reasoning_tokens = (
            self._usage_value(completion_details, "reasoning_tokens")
            or self._usage_value(usage, "reasoning_tokens")
        )

        result = {}
        for key, value in {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cached_tokens": cached_tokens,
            "cache_miss_tokens": cache_miss_tokens,
            "reasoning_tokens": reasoning_tokens,
        }.items():
            if value:
                result[key] = value
        if cached_tokens or cache_miss_tokens:
            denom = cached_tokens + cache_miss_tokens
            if denom:
                result["cache_hit_rate"] = round(cached_tokens / denom, 6)
        if isinstance(usage, dict):
            metadata = {
                key: usage[key]
                for key in ("estimated", "encoding", "source")
                if key in usage
            }
        else:
            metadata = {
                key: getattr(usage, key)
                for key in ("estimated", "encoding", "source")
                if getattr(usage, key, None) is not None
            }
        for key, value in metadata.items():
            if key == "estimated":
                result[key] = bool(value)
            elif value:
                result[key] = value
        return result

    def load_call_history(self):
        """从本地 JSONL 恢复 LLM 调用历史（每个来源保留最近若干条）。"""
        max_history_per_key = self._call_history_limit()
        if not self.call_history_path.exists():
            return

        try:
            loaded = {}
            with open(self.call_history_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    key = (
                        entry.get("key")
                        or f"{entry.get('plugin_name', '')}.{entry.get('call_type', '')}"
                    )
                    if key == ".":
                        continue

                    entry.pop("key", None)
                    entry = self._sanitize_history_entry(entry)
                    loaded.setdefault(key, []).append(entry)
                    if len(loaded[key]) > max_history_per_key:
                        loaded[key] = loaded[key][-max_history_per_key:]

            with self._call_history_lock:
                self.call_history = loaded
            logger.info(f"✅ 已加载 LLM 调用历史: {sum(len(v) for v in loaded.values())} 条")
            # Older builds persisted embedded image/base64 payloads and could
            # grow a few hundred records to hundreds of MB. Rewrite once after
            # sanitizing so future saves remain bounded.
            if self.call_history_path.stat().st_size > 20 * 1024 * 1024:
                with self._call_history_lock:
                    self._save_call_history()
        except Exception as e:
            logger.warning(f"⚠️ 加载 LLM 调用历史失败: {e}")

    def _save_call_history(self):
        """将当前内存中的调用历史落盘为 JSONL。"""
        try:
            self.call_history_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.call_history_path.with_suffix(".jsonl.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                for key, entries in self.call_history.items():
                    for entry in entries:
                        record = {"key": key, **entry}
                        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            os.replace(tmp_path, self.call_history_path)
        except Exception as e:
            logger.warning(f"⚠️ 保存 LLM 调用历史失败: {e}")

    def record_codex_reply(
        self,
        *,
        messages: List[Dict[str, Any]],
        response: Optional[Dict[str, Any]],
        response_text: str,
        response_time: float,
        model_id: str,
        backend: str,
        success: bool,
        error: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record first-class Codex replies without routing them through LiteLLM."""
        metadata = {**(metadata or {}), "billing_codex": True}
        safe_response = response if isinstance(response, dict) else {}
        token_usage = self._extract_token_usage(safe_response)
        response_attachments = self._extract_attachments_from_response(safe_response)
        actual_model = f"local_codex_{str(backend or 'runtime')}/{str(model_id or 'unknown')}"
        if success:
            self._record_stats(
                "assistant",
                "chat",
                str(model_id or "unknown"),
                safe_response,
                max(0.0, float(response_time or 0.0)),
                token_usage=token_usage, metadata=metadata,
            )
        else:
            self._record_error("assistant", "chat", str(model_id or "unknown"))
            self._record_usage("assistant", "chat", str(model_id or "unknown"),
                               safe_response, response_time, success=False, metadata=metadata, error=error)
        self._record_call_history(
            "assistant",
            "chat",
            str(model_id or "unknown"),
            messages,
            response_text,
            max(0.0, float(response_time or 0.0)),
            actual_model,
            int(token_usage.get("total_tokens", 0) or 0),
            success=bool(success),
            error=str(error or ""),
            reasoning_text=self._extract_reasoning_from_response(safe_response),
            token_usage=token_usage,
            metadata=metadata,
            response_attachments=response_attachments,
        )

    def _record_call_history(self, plugin_name: str, call_type: str, model_id: str,
                             messages: list, response_text: str, response_time: float,
                             actual_model: str, tokens: int, success: bool, error: str = "",
                             reasoning_text: str = "", token_usage: dict = None,
                             metadata: Optional[dict] = None,
                             response_attachments: Optional[List[Dict[str, Any]]] = None):
        """记录每次 LLM 调用的请求/响应到内存和本地 JSONL。"""
        max_history_per_key = self._call_history_limit()
        key = f"{plugin_name}.{call_type}"
        raw_metadata = metadata if isinstance(metadata, dict) else {}
        usage_capture = raw_metadata.get("_usage_capture")
        safe_metadata = copy.deepcopy(
            {
                key: value
                for key, value in raw_metadata.items()
                if not str(key).startswith("_")
            }
        )
        history_mode = str(
            safe_metadata.get("history_mode") or "full"
        ).strip().lower()
        if history_mode not in {"full", "summary", "none"}:
            history_mode = "full"

        entry = self._sanitize_history_entry({
            "timestamp": datetime.now().isoformat(),
            "plugin_name": plugin_name,
            "call_type": call_type,
            "model_id": model_id,
            "actual_model": actual_model,
            "messages": messages,
            "response_text": response_text if response_text else "",
            "response_attachments": response_attachments or [],
            "reasoning_text": reasoning_text if reasoning_text else "",
            "response_time": round(response_time, 3),
            "tokens": tokens,
            "token_usage": token_usage or {},
            "success": success,
            "error": error if error else "",
            "chat_name": str(safe_metadata.get("chat_name") or ""),
            "role_name": str(safe_metadata.get("role_name") or ""),
            "trace_id": str(safe_metadata.get("trace_id") or ""),
        })
        if isinstance(usage_capture, list):
            usage_capture.append(
                {
                    "timestamp": entry["timestamp"],
                    "plugin_name": plugin_name,
                    "call_type": call_type,
                    "model_id": model_id,
                    "actual_model": actual_model,
                    "response_time": entry["response_time"],
                    "tokens": tokens,
                    "token_usage": copy.deepcopy(entry["token_usage"]),
                    "success": success,
                    "error": entry["error"],
                    "chat_name": entry["chat_name"],
                    "trace_id": entry["trace_id"],
                }
            )
        if history_mode == "none":
            return
        if history_mode == "summary":
            entry["messages"] = []
            entry["response_text"] = ""
            entry["response_attachments"] = []
            entry["reasoning_text"] = ""
        with self._call_history_lock:
            if key not in self.call_history:
                self.call_history[key] = []
            self.call_history[key].append(entry)
            if len(self.call_history[key]) > max_history_per_key:
                self.call_history[key] = self.call_history[key][-max_history_per_key:]
            self._save_call_history()

    @staticmethod
    def _call_history_limit() -> int:
        try:
            return max(10, int(get_setting("LLM_CALL_HISTORY_PER_KEY", "") or 50))
        except Exception:
            return 50

    @staticmethod
    def _history_text_limit() -> int:
        try:
            return max(2000, min(int(os.getenv("LLM_HISTORY_MAX_TEXT_CHARS", "50000")), 500000))
        except (TypeError, ValueError):
            return 50000

    @classmethod
    def _sanitize_history_value(cls, value: Any, *, depth: int = 0) -> Any:
        if depth > 12:
            return "[nested payload omitted]"
        if isinstance(value, str):
            limit = cls._history_text_limit()
            lowered = value[:64].lower()
            looks_binary = lowered.startswith("data:image/") or lowered.startswith("data:audio/") or lowered.startswith("data:video/")
            if looks_binary:
                digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]
                return f"[embedded binary omitted: chars={len(value)} sha256={digest}]"
            if len(value) > limit:
                digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]
                return value[:limit] + f"\n[…truncated chars={len(value)} sha256={digest}]"
            return value
        if isinstance(value, dict):
            return {
                str(key): cls._sanitize_history_value(item, depth=depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            items = list(value)
            if len(items) > 300:
                items = items[:20] + [{"_omitted_items": len(items) - 300}] + items[-280:]
            return [cls._sanitize_history_value(item, depth=depth + 1) for item in items]
        return value

    @classmethod
    def _sanitize_history_entry(cls, entry: Dict[str, Any]) -> Dict[str, Any]:
        sanitized = dict(entry)
        for key in (
            "messages",
            "response_text",
            "response_attachments",
            "reasoning_text",
            "error",
        ):
            if key in sanitized:
                sanitized[key] = cls._sanitize_history_value(sanitized[key])
        return sanitized

    def get_call_history(self, plugin_name: str = None, call_type: str = None) -> dict:
        """获取调用历史记录，可按插件/类型筛选"""
        with self._call_history_lock:
            if plugin_name and call_type:
                key = f"{plugin_name}.{call_type}"
                return {key: list(self.call_history.get(key, []))}
            result = {}
            for key, entries in self.call_history.items():
                parts = key.split(".", 1)
                p_name, c_type = parts[0], parts[1] if len(parts) > 1 else ""
                if plugin_name and p_name != plugin_name:
                    continue
                if call_type and c_type != call_type:
                    continue
                result[key] = list(entries)
            return result

    @staticmethod
    def _history_entry_summary(entry: dict, index: int) -> dict:
        messages = entry.get("messages") or []
        response_text = entry.get("response_text") or ""
        response_attachments = [
            item
            for item in (entry.get("response_attachments") or [])
            if isinstance(item, dict)
        ]
        image_count = sum(
            1
            for item in response_attachments
            if str(item.get("type") or "").lower() == "image"
            or str(item.get("mime_type") or "").lower().startswith("image/")
        )
        reasoning_text = entry.get("reasoning_text") or ""
        return {
            "index": index,
            "timestamp": entry.get("timestamp"),
            "plugin_name": entry.get("plugin_name"),
            "call_type": entry.get("call_type"),
            "model_id": entry.get("model_id"),
            "actual_model": entry.get("actual_model"),
            "response_time": entry.get("response_time", 0),
            "tokens": entry.get("tokens", 0),
            "token_usage": entry.get("token_usage") or {},
            "success": entry.get("success", False),
            "message_count": len(messages),
            "response_size": len(response_text),
            "attachment_count": len(response_attachments),
            "image_count": image_count,
            "file_count": max(0, len(response_attachments) - image_count),
            "has_attachments": bool(response_attachments),
            "reasoning_size": len(reasoning_text),
            "has_reasoning": bool(reasoning_text),
            "has_error": bool(entry.get("error")),
            "chat_name": entry.get("chat_name") or "",
            "role_name": entry.get("role_name") or "",
            "trace_id": entry.get("trace_id") or "",
            "response_preview": response_text[:240],
            "error_preview": (entry.get("error") or "")[:240],
        }

    def get_call_history_summaries(
        self,
        plugin_name: str,
        call_type: str,
        limit: int = 10,
        offset: int = 0,
    ) -> dict:
        """获取轻量调用历史详情列表，避免一次性传输完整 messages/response。"""
        key = f"{plugin_name}.{call_type}"
        with self._call_history_lock:
            entries = list(self.call_history.get(key, []))

        total = len(entries)
        offset = max(0, offset)
        limit = max(1, min(int(limit or 10), 50))
        indexed = list(enumerate(entries))
        indexed.reverse()
        page = indexed[offset:offset + limit]

        return {
            "key": key,
            "total": total,
            "limit": limit,
            "offset": offset,
            "entries": [self._history_entry_summary(entry, index) for index, entry in page],
        }

    def get_call_history_entry(
        self,
        plugin_name: str,
        call_type: str,
        index: int,
    ) -> Optional[dict]:
        """按原始 index 获取单条完整调用历史。"""
        key = f"{plugin_name}.{call_type}"
        with self._call_history_lock:
            entries = self.call_history.get(key, [])
            if index < 0 or index >= len(entries):
                return None
            return dict(entries[index])

    def get_call_history_summary(self) -> list:
        """获取调用历史概览：列出所有已记录的 plugin.call_type 及其最新调用时间"""
        summaries = []
        with self._call_history_lock:
            for key, entries in self.call_history.items():
                if not entries:
                    continue
                parts = key.split(".", 1)
                latest = entries[-1]
                summaries.append({
                    "key": key,
                    "plugin_name": parts[0],
                    "call_type": parts[1] if len(parts) > 1 else "",
                    "count": len(entries),
                    "last_call": latest["timestamp"],
                    "last_success": latest["success"],
                    "last_model": latest.get("actual_model", latest.get("model_id", "")),
                })
        summaries.sort(key=lambda x: x["last_call"], reverse=True)
        return summaries

    @staticmethod
    def _empty_stats_entry() -> dict:
        return {
            "count": 0, "total_tokens": 0,
            "model_usage": {}, "last_call": None,
            "response_times": [], "error_count": 0,
            "cache_hit_tokens": 0, "cache_miss_tokens": 0,
            "cache_observed_calls": 0, "cache_hit_rate": 0.0,
        }

    @staticmethod
    def _today_key() -> str:
        return datetime.now().date().isoformat()

    def _get_today_stats(self) -> dict:
        today = self._today_key()
        stats = self.daily_stats.setdefault(today, {})
        # 控制文件增长，保留最近 31 天。
        if len(self.daily_stats) > 31:
            for day in sorted(self.daily_stats.keys())[:-31]:
                self.daily_stats.pop(day, None)
        return stats

    def _record_stats_usage(self, plugin_name: str, call_type: str, model_id: str, usage):
        """记录调用统计 - 专门处理 Usage 对象"""
        key = f"{plugin_name}.{call_type}"
        timestamp = datetime.now().isoformat()
        today_stats = self._get_today_stats()
        for stats_dict in [self.session_stats, self.total_stats, today_stats]:
            if key not in stats_dict:
                stats_dict[key] = self._empty_stats_entry()
            tokens = (getattr(usage, "total_tokens", 0) or 0) if usage else 0
            stats_dict[key]["count"] += 1
            stats_dict[key]["total_tokens"] += tokens
            stats_dict[key]["last_call"] = timestamp
            stats_dict[key]["model_usage"][model_id] = \
                stats_dict[key]["model_usage"].get(model_id, 0) + 1
        self.save_stats()

    @staticmethod
    def _usage_value(obj, key: str, default: int = 0):
        if not obj:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default) or default
        return getattr(obj, key, default) or default

    def _usage_connection(self, params):
        configs = getattr(self, "config", {}).get("models", {})
        model = str(params.get("model") or "unknown")
        matches = [(key, cfg) for key, cfg in configs.items()
                   if str(cfg.get("model")) == model
                   and str(self._resolve_env(cfg.get("api_base")) or "") == str(params.get("api_base") or "")]
        key, config = matches[0] if len(matches) == 1 else (model, {})
        # Effective parameters, never credentials, are used for the price snapshot.
        config = {**config, **{k: params[k] for k in ("model", "api_base", "custom_llm_provider", "service_tier") if k in params}}
        return key, copy.deepcopy(config)

    def _usage_completion(self, _billing_config=None, **params):
        context = _usage_call.get()
        if not context or not getattr(self, "usage_service", None):
            return litellm.completion(**params)
        model_id, config = self._usage_connection(params)
        if _billing_config is not None:
            config = copy.deepcopy(_billing_config)
            config['api_base'] = params.get('api_base')
            config['service_tier'] = params.get('service_tier')
            model_id = next((key for key, value in self.config.get('models', {}).items() if value == _billing_config), model_id)
        # Make SDK retries observable; network/429/5xx retries retain the configured limit.
        retries = int(params.get("max_retries", params.get("num_retries", 2)) or 0)
        params = {**params, "max_retries": 0, "num_retries": 0}
        params.pop("fallbacks", None)
        if params.get("stream"):
            params["stream_options"] = {**(params.get("stream_options") or {}), "include_usage": True}
        for retry in range(retries + 1):
            started, at = time.monotonic(), datetime.now().astimezone()
            context["attempts"] += 1
            try:
                response = litellm.completion(**params)
            except Exception as exc:
                self._record_usage(context["plugin"], context["task"], model_id, getattr(exc, "response", None),
                                   time.monotonic() - started, success=False, metadata=context["metadata"], config=config, at=at, error=str(exc))
                code = getattr(exc, "status_code", None)
                if retry < retries and (self._is_network_error(exc) or code in {408, 409, 429} or isinstance(code, int) and code >= 500):
                    time.sleep(min(2 ** retry, 8))
                    continue
                raise
            if params.get("stream"):
                def tracked_stream():
                    last = None
                    ok = False
                    try:
                        for chunk in response:
                            if read(chunk, "usage") is not None:
                                last = chunk
                            yield chunk
                        ok = True
                    finally:
                        self._record_usage(context["plugin"], context["task"], model_id, last,
                                           time.monotonic() - started, success=ok, metadata=context["metadata"], config=config, at=at)
                return tracked_stream()
            self._record_usage(context["plugin"], context["task"], model_id, response,
                               time.monotonic() - started, success=True, metadata=context["metadata"], config=config, at=at)
            return response

    def _usage_local_codex(self, **kwargs):
        context = _usage_call.get()
        started, at = time.monotonic(), datetime.now().astimezone()
        config = copy.deepcopy(kwargs["model_config"])
        config["model"] = kwargs["params"].get("model") or config.get("model")
        if context:
            context["attempts"] += 1
        response, ok, failure = None, False, ""
        try:
            response, duration, result = self._call_local_codex(**kwargs)
            ok = bool(result)
            return response, duration, result
        except Exception as exc:
            response = getattr(exc, "billing_response", None)
            failure = str(exc)
            raise
        finally:
            if context:
                self._record_usage(context["plugin"], context["task"], str(config.get("model")), response,
                                   time.monotonic() - started, success=ok,
                                   metadata={**context["metadata"], "billing_codex": True}, config=config, at=at, error=failure)

    def _record_usage(self, plugin_name, call_type, model_id, response, duration, *, success, metadata=None, config=None, at=None, error=""):
        """Best-effort accounting must never turn a successful reply into a failure."""
        service = getattr(self, "usage_service", None)
        if service is None or call_type == "reply_latency":
            return
        try:
            from app.services.llm_usage_context import resolve_usage_subject
            at = at or datetime.now().astimezone()
            config = copy.deepcopy(config if config is not None else self.config.get("models", {}).get(model_id, {}))
            if config.get("api_base"):
                config["api_base"] = self._resolve_env(config["api_base"])
            actual = str(read(response, "model") or config.get("model") or model_id)
            if read(response, "service_tier"):
                config["service_tier"] = read(response, "service_tier")
            provider = str(config.get("custom_llm_provider") or config.get("provider") or "")
            codex = bool((metadata or {}).get("billing_codex")) or self._is_local_codex_model(config)
            segments = read(response, "billing_usage_segments")
            if codex and segments and any(segments):
                shared = {**(metadata or {}), "billing_logical_id": uuid.uuid4().hex, "billing_scope": "codex_turn"}
                for index, segment in enumerate(segments):
                    self._record_usage(plugin_name, call_type, model_id,
                                       {"model": actual, "usage": segment, "id": f"{read(response, 'id', '')}:{index}"},
                                       duration if index == len(segments) - 1 else None,
                                       success=success, metadata={**shared, "billing_scope": segment.get("billing_scope", "codex_turn")}, config=config, at=at)
                return
            raw = read(response, "usage")
            usage = normalize_usage(raw, provider)
            pricing = price_usage(config, actual, usage, codex=codex, at=at,
                                  catalog=litellm.model_cost, catalog_version=package_version("litellm"))
            if codex and (metadata or {}).get("billing_scope") != "request" and usage.get("prompt_tokens", 0) > 272_000:
                # A turn total may contain many short requests; it cannot determine a per-request tier.
                pricing.update(amount=None, status="unknown", reason="context_tier_unavailable")
            # Explicit provider monetary metadata is distinct from LiteLLM's calculated response_cost.
            explicit_cost = decimal(read(raw, "cost"))
            explicit_currency = read(raw, "cost_currency") or read(raw, "currency")
            if not codex and explicit_cost is not None and explicit_currency:
                pricing = {"amount": str(explicit_cost), "currency": str(explicit_currency).upper(),
                           "status": "reported", "snapshot": {"source": "provider_response", "model": actual}}
            if not success and codex and not raw and any(marker in str(error).lower() for marker in (
                "you've hit your usage limit", "insufficient_quota", "quota_exceeded",
            )):
                pricing = {"amount": "0", "currency": "USD", "status": "rejected",
                           "reason": "quota_rejected", "snapshot": {"source": "quota_rejection", "model": actual}}
            context = _usage_call.get()
            from urllib.parse import urlsplit
            base = urlsplit(str(config.get("api_base") or ""))
            connection = {"id": model_id, "provider": provider, "host": base.hostname or "", "codex": codex}
            service.record(task=f"{plugin_name}.{call_type}", subject=resolve_usage_subject(metadata), model=actual,
                           success=success, usage=usage, duration=duration, pricing=pricing, raw=raw_usage(raw),
                           connection=connection, logical_id=context["id"] if context else (metadata or {}).get("billing_logical_id"),
                           provider_request_id=str(read(response, "id") or "")[:200],
                           scope=(metadata or {}).get("billing_scope") or ("codex_turn" if codex else "request"))
        except Exception:
            logger.exception("Failed to record LLM request usage")

    def _record_stats(
        self,
        plugin_name: str,
        call_type: str,
        model_id: str,
        response,
        response_time: float = 0.0,
        token_usage: dict = None,
        metadata: dict = None,
    ):
        """记录调用统计"""
        with self._stats_lock:
            self._record_stats_unlocked(
                plugin_name,
                call_type,
                model_id,
                response,
                response_time,
                token_usage,
            )

        if not _usage_call.get():
            self._record_usage(plugin_name, call_type, model_id, response, response_time,
                               success=True, metadata=metadata)

    def _record_stats_unlocked(
        self,
        plugin_name: str,
        call_type: str,
        model_id: str,
        response,
        response_time: float = 0.0,
        token_usage: dict = None,
    ):
        key = f"{plugin_name}.{call_type}"
        today_stats = self._get_today_stats()
        for stats_dict in [self.session_stats, self.total_stats, today_stats]:
            if key not in stats_dict:
                stats_dict[key] = self._empty_stats_entry()

        tokens = 0
        if isinstance(response, dict) and response.get("usage"):
            tokens = int(self._usage_value(response.get("usage"), "total_tokens") or 0)
        elif not isinstance(response, dict) and hasattr(response, "usage") and response.usage:
            tokens = getattr(response.usage, "total_tokens", 0) or 0
        usage = token_usage or self._extract_token_usage(response)
        cached_tokens = int(usage.get("cached_tokens", 0) or 0)
        cache_miss_tokens = int(usage.get("cache_miss_tokens", 0) or 0)

        timestamp = datetime.now().isoformat()
        for stats_dict in [self.session_stats, self.total_stats, today_stats]:
            stats_dict[key]["count"] += 1
            stats_dict[key]["total_tokens"] += tokens
            stats_dict[key]["last_call"] = timestamp
            if cached_tokens or cache_miss_tokens:
                stats_dict[key]["cache_hit_tokens"] += cached_tokens
                stats_dict[key]["cache_miss_tokens"] += cache_miss_tokens
                stats_dict[key]["cache_observed_calls"] += 1
                denom = stats_dict[key]["cache_hit_tokens"] + stats_dict[key]["cache_miss_tokens"]
                stats_dict[key]["cache_hit_rate"] = round(
                    stats_dict[key]["cache_hit_tokens"] / denom, 6
                ) if denom else 0.0
            rts = stats_dict[key]["response_times"]
            rts.append(response_time)
            if len(rts) > 10:
                stats_dict[key]["response_times"] = rts[-10:]
            stats_dict[key]["model_usage"][model_id] = \
                stats_dict[key]["model_usage"].get(model_id, 0) + 1

        self.save_stats()

    def _record_error(self, plugin_name: str, call_type: str, model_id: str):
        """记录错误统计"""
        with self._stats_lock:
            self._record_error_unlocked(plugin_name, call_type, model_id)

    def _record_error_unlocked(
        self,
        plugin_name: str,
        call_type: str,
        model_id: str,
    ):
        key = f"{plugin_name}.{call_type}"
        today_stats = self._get_today_stats()
        for stats_dict in [self.session_stats, self.total_stats, today_stats]:
            if key not in stats_dict:
                stats_dict[key] = self._empty_stats_entry()
            stats_dict[key]["error_count"] = stats_dict[key].get("error_count", 0) + 1
        self.save_stats()

    def _log_success(self, kwargs, response_obj, start_time, end_time):
        """LiteLLM 成功回调"""
        duration = end_time - start_time
        model = kwargs.get("model", "unknown")
        self._record_model_success(model, duration)
        logger.debug(f"✅ LLM Success: {model} - {duration:.2f}s")

    def _log_failure(self, kwargs, response_obj, start_time, end_time):
        """LiteLLM 失败回调"""
        model = kwargs.get("model", "unknown")
        error = kwargs.get("exception") or response_obj
        self._record_model_failure(model, error)
        logger.error("❌ LLM Failure: %s error_type=%s status_code=%s",
                     model, type(error).__name__ if error is not None else "unknown",
                     getattr(error, "status_code", None))

    # ─────────────────────── 持久化统计 ───────────────────────

    def save_stats(self):
        """Health/latency counters are ephemeral; durable usage lives in the request ledger."""
        return

    def get_stats(self) -> Dict:
        """获取统计数据"""
        return {
            "today": self.daily_stats.get(self._today_key(), {}),
            "session": self.session_stats,
            "total": self.total_stats,
            "model_health": self.get_model_health(),
        }

    # ─────────────────────── 配置 CRUD ────────────────────────

    def get_config(self) -> Dict:
        """获取当前配置"""
        return self.config

    def update_model(self, model_id: str, config: Dict):
        """更新模型配置（完全替换）"""
        with self._config_lock:
            if "models" not in self.config:
                self.config["models"] = {}
            sanitized, removed = sanitize_gemini_3_model_config(config)
            if self._is_local_codex_model(sanitized):
                sanitized.pop("codex_profile_id", None)
            self.config["models"][model_id] = sanitized
            self.save_config()
        if removed:
            logger.info(
                "🧹 保存时已移除 Gemini 3+ 模型 %s 的弃用采样参数: %s",
                model_id,
                ", ".join(sorted(removed)),
            )
        logger.info(f"✅ 模型配置已更新: {model_id}")

    def rename_model(self, old_id: str, new_id: str):
        """Atomically rename a model ID and every mapping that references it."""
        with self._config_lock:
            self._rename_model_unlocked(old_id, new_id)

    def _rename_model_unlocked(self, old_id: str, new_id: str):
        """重命名模型 ID，并同步更新任务模型路由。"""
        if "models" not in self.config or old_id not in self.config["models"]:
            return

        if old_id == new_id:
            return

        # Rename in models (preserve order if possible)
        new_models = {}
        for k, v in self.config["models"].items():
            if k == old_id:
                new_models[new_id] = v
            else:
                new_models[k] = v
        self.config["models"] = new_models

        # Update plugin mappings
        if "plugin_mappings" in self.config:
            for plugin_name, mappings in self.config["plugin_mappings"].items():
                for call_type, mapping in mappings.items():
                    changed = False
                    if mapping.get("primary") == old_id:
                        mapping["primary"] = new_id
                        changed = True
                    if "fallback" in mapping and isinstance(mapping["fallback"], list):
                        for i, fb in enumerate(mapping["fallback"]):
                            if fb == old_id:
                                mapping["fallback"][i] = new_id
                                changed = True
                    if changed:
                        logger.info(f"🔄 自动更新映射: {plugin_name}.{call_type} 引用了 {old_id} -> {new_id}")

        self.save_config()
        logger.info(f"✅ 模型ID已重命名: {old_id} -> {new_id}")

    def reorder_models(self, order: List[str]):
        """Atomically reorder models without racing config saves."""
        with self._config_lock:
            self._reorder_models_unlocked(order)

    def _reorder_models_unlocked(self, order: List[str]):
        """根据给定的 ID 列表重新排序模型"""
        if "models" not in self.config:
            return

        current_models = self.config["models"]
        new_models = {}

        for model_id in order:
            if model_id in current_models:
                new_models[model_id] = current_models[model_id]

        for model_id, cfg in current_models.items():
            if model_id not in new_models:
                new_models[model_id] = cfg

        self.config["models"] = new_models
        self.save_config()
        logger.info(f"✅ 模型顺序已更新")

    def delete_model(self, model_id: str):
        """删除模型配置"""
        with self._config_lock:
            if model_id in self.config.get("models", {}):
                del self.config["models"][model_id]
                self.save_config()
                logger.info(f"🗑️ 模型配置已删除: {model_id}")

    def unbind_codex_profile(self, profile_id: str) -> int:
        """Reset local-Codex model bindings that reference a deleted Profile."""
        normalized = str(profile_id or "").strip()
        if not normalized:
            return 0
        changed = 0
        with self._config_lock:
            for model_config in self.config.get("models", {}).values():
                if not isinstance(model_config, dict):
                    continue
                if str(model_config.get("codex_profile_id") or "").strip() != normalized:
                    continue
                model_config.pop("codex_profile_id", None)
                changed += 1
            if changed:
                self.save_config()
        if changed:
            logger.info(
                "🧹 已将 %s 个模型配置从已删除的 Codex Profile %s 恢复为默认 Profile",
                changed,
                normalized,
            )
        return changed

    def update_mapping(self, plugin_name: str, call_type: str, mapping: Dict):
        """更新一个路由主体的任务模型路由。"""
        from app.services.retired_model_routes import is_retired_model_route

        if is_retired_model_route(plugin_name, call_type):
            raise ValueError("该模型路由已弃用并移除")
        if plugin_name in {"assistant", "builtin_chatbot"} and call_type == "chat":
            raise ValueError("AI 助手最终回复不支持通用模型路由")
        with self._config_lock:
            if "plugin_mappings" not in self.config:
                self.config["plugin_mappings"] = {}
            if plugin_name not in self.config["plugin_mappings"]:
                self.config["plugin_mappings"][plugin_name] = {}
            primary_id = str(mapping.get("primary") or "").strip()
            primary_cfg = self.config.get("models", {}).get(primary_id, {})
            if is_gemini_3_model_config(primary_cfg):
                mapping = copy.deepcopy(mapping)
                overrides = mapping.get("override_params")
                if isinstance(overrides, dict):
                    _strip_gemini_3_sampling_parameters_inplace(overrides)
            self.config["plugin_mappings"][plugin_name][call_type] = mapping
            self.save_config()
        logger.info(f"✅ 任务模型路由已更新: {plugin_name}.{call_type}")

    def _get_default_config(self) -> Dict:
        """默认配置"""
        return {
            "models": {
                "gpt4": {
                    "model": "gpt-4.1",
                    "api_key": "env::OPENAI_API_KEY",
                    "temperature": 0.7,
                    "max_tokens": 2000,
                },
                "gemini-flash": {
                    "model": "gemini/gemini-3-flash-preview",
                    "api_key": "env::GEMINI_API_KEY",
                    "temperature": 0.6,
                },
                "deepseek": {
                    "model": "deepseek/deepseek-chat",
                    "api_base": "env::DEEPSEEK_API_BASE",
                    "api_key": "env::DEEPSEEK_API_KEY",
                    "temperature": 0.1,
                },
                "deepseek-followup": {
                    "model": "deepseek/deepseek-chat",
                    "api_base": "env::DEEPSEEK_API_BASE",
                    "api_key": "env::DEEPSEEK_API_KEY",
                    "temperature": 0.1,
                    "max_tokens": 200,
                    "timeout": 15,
                    "extra_body": {"thinking": {"type": "disabled"}},
                    "enable_web_search": False,
                },
            },
            "plugin_mappings": {
                "assistant": {
                    "judge": {
                        "primary": "deepseek",
                    },
                    "followup_judge": {
                        "primary": "deepseek-followup",
                        "fallback": ["gemini-flash"],
                        "override_params": {
                            "temperature": 0.1,
                            "max_tokens": 200,
                            "timeout": 15,
                            "response_format": {"type": "json_object"},
                        },
                    },
                },
                "builtin_chat_logger": {
                    "image_understanding": {
                        "primary": "gpt4",
                        "fallback": ["gemini-flash"],
                        "override_params": {},
                    }
                },
            },
        }


# ─────────────── 全局单例 ────────────────

_llm_manager_instance: Optional[LLMManager] = None


def get_llm_manager() -> LLMManager:
    """获取 LLM Manager 单例"""
    global _llm_manager_instance
    if _llm_manager_instance is None:
        _llm_manager_instance = LLMManager()
    return _llm_manager_instance


def reload_llm_config():
    """重新加载配置（热更新）"""
    global _llm_manager_instance
    if _llm_manager_instance:
        _llm_manager_instance.load_config()
        logger.info("🔄 LLM 配置已重新加载")
    else:
        logger.warning("⚠️ LLM Manager 未初始化")
