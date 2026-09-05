"""
summary_plus 摘要插件
"""


import logging
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional, Any, Callable, List, Dict

import requests
from selenium import webdriver

from app.core.event_bus import Event, EventType
from app.services.llm_manager import get_llm_manager
from app.services.shared_chrome import get_shared_chrome_operation_lock
from app.services.email_service import get_email_service
from app.services.plugin_runtime import PluginContext
from app.services.runtime_operations import OperationContext
from app.utils.plugin_config import get_config
from .asr_service import bili_transcribe_local, douyin_transcribe_local
from .browser_service import browser_summarize, open_blank_worker_tab
from .browser_runtime import BrowserRuntimeMixin
from .mindmap_service import (
    MINDMAP_SYSTEM_PROMPT_DEFAULT,
    get_mindmap_skip_reason,
    is_mindmap_skip_response,
    render_mindmap_to_image,
    resolve_mindmap_layout,
    summarize_to_mindmap_json,
)
from .media_pipeline import (
    TIKHUB_ENDPOINT_FETCH_ONE,
    TIKHUB_ENDPOINT_FETCH_ONE_WEB,
    TIKHUB_ENDPOINT_TIKTOK_FETCH_ONE,
    MediaPipelineMixin,
)
from .platform_service import handle_link_message as route_link_message
from .runtime_support import (
    ArtifactManager,
    DispatchDecision,
    ManagedDispatcher,
    StorageMigrator,
)
from .subtitle_service import bili_get_subtitles
from .yt_transcript import get_best_transcript_text
from .ytdlp_cookie_service import ytdlp_browser_cookie_args
from .xhs_service import (
    TIKHUB_ENDPOINT_XHS_IMAGE_NOTE,
    TIKHUB_ENDPOINT_XHS_VIDEO_NOTE,
    XiaohongshuMixin,
)

logger = logging.getLogger(__name__)
# start.py 将 app.plugins 默认压到 WARNING；summary_plus 是长链路插件，
# 需要保留 INFO 级链路日志，便于定位“URL 已获取但没有下一步”的问题。
logger.setLevel(logging.INFO)


class SummaryService(BrowserRuntimeMixin, MediaPipelineMixin, XiaohongshuMixin):
    """摘要服务（复用 builtin_summary 的浏览器摘要能力，并增强抖音解析）"""

    # 时间常量 (秒)
    WINDOW_HANDLE_STABILIZE_DELAY = 0.5  # 窗口句柄稳定等待时间
    RETRY_DELAY = 0.25  # 重试间隔
    PAGE_CONTENT_STABILIZE_DELAY = 1.5  # 页面内容稳定等待时间

    # 内容限制
    MAX_CONTENT_LENGTH = 20000  # 最大内容长度(字符)

    def __init__(self, context: Optional[PluginContext] = None):
        self.llm_manager = get_llm_manager()
        self.logger = logger
        self.context = context

        # Load config
        plugin_name = "summary_plus"
        self.max_pending_tasks = max(
            1,
            int(get_config("max_pending_tasks", plugin_name=plugin_name, default=12)),
        )
        self.media_worker_count = max(
            1,
            int(get_config("media_worker_count", plugin_name=plugin_name, default=2)),
        )
        self.artifact_retention_hours = max(
            1,
            int(get_config("artifact_retention_hours", plugin_name=plugin_name, default=24)),
        )
        self.max_artifact_size_mb = max(
            1,
            int(get_config("max_artifact_size_mb", plugin_name=plugin_name, default=512)),
        )
        self.artifact_quota_mb = max(
            1,
            int(get_config("artifact_quota_mb", plugin_name=plugin_name, default=8192)),
        )
        self.storage_migration_notes: List[str] = []
        self.artifacts: Optional[ArtifactManager] = None
        self.dispatcher: Optional[ManagedDispatcher] = None
        self._bili_cookie_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "cookies.txt",
        )

        configured_chrome_data_dir = str(
            get_config(
                "chrome_user_data_dir",
                plugin_name=plugin_name,
                default="tmp/chrome_data",
            )
            or "tmp/chrome_data"
        )
        if context is not None:
            migrator = StorageMigrator(context.storage, self.logger)
            persistent_cookie = context.storage.persistent_path("bilibili/cookies.txt")
            migrator.copy_file_once(Path(self._bili_cookie_path), persistent_cookie)
            self._bili_cookie_path = str(persistent_cookie)

            legacy_chrome = Path("tmp/chrome_data")
            configured_path = Path(configured_chrome_data_dir)
            if not configured_path.is_absolute() and configured_path.as_posix().rstrip("/") == "tmp/chrome_data":
                managed_chrome = context.storage.machine_bound_root / "chrome_profile"
                configured_chrome_data_dir = str(
                    migrator.adopt_directory_once(legacy_chrome, managed_chrome)
                )
            self.storage_migration_notes = list(migrator.notes)
            self.artifacts = ArtifactManager(
                context.storage,
                max_artifact_size_mb=self.max_artifact_size_mb,
                quota_mb=self.artifact_quota_mb,
                retention_hours=self.artifact_retention_hours,
                legacy_roots=(
                    Path("tmp/videos"),
                    Path("tmp/images"),
                    Path("tmp/mindmaps"),
                    Path("tmp/subtitles"),
                ),
            )
            cleaned = self.artifacts.cleanup_stale()
            if cleaned["files"]:
                self.logger.info(
                    "🧹 已清理 Summary Plus 过期产物: %s 个文件 / %.1f MB",
                    cleaned["files"],
                    cleaned["bytes"] / (1024 * 1024),
                )
            self.dispatcher = ManagedDispatcher(
                context,
                max_pending=self.max_pending_tasks,
                media_workers=self.media_worker_count,
            )

        # Chrome settings
        self.chrome_debug_port = int(get_config("chrome_debug_port", plugin_name=plugin_name))
        self.chrome_path = get_config("chrome_path", plugin_name=plugin_name)
        self.chrome_user_data_dir = configured_chrome_data_dir
        self.chrome_profile_dir = get_config("chrome_profile_dir", plugin_name=plugin_name)
        self.page_load_timeout = int(get_config("page_load_timeout", plugin_name=plugin_name))
        self.webdriver_command_timeout_sec = int(
            get_config("webdriver_command_timeout_sec", plugin_name=plugin_name, default=8)
        )
        # Translation settings
        self.special_translation_groups = set(get_config("special_translation_groups", plugin_name=plugin_name) or [])
        self.special_translation_target_language = str(get_config("special_translation_target_language", plugin_name=plugin_name) or "English")
        self.domain_blacklist = [d.lower() for d in (get_config("domain_blacklist", plugin_name=plugin_name) or [])]
        self.sender_blacklist = set(
            s.lower().strip()
            for s in (get_config("sender_blacklist", plugin_name=plugin_name) or [])
            if isinstance(s, str) and s.strip()
        )

        # Prompts
        self.prompt_summary = get_config("prompt_summary", plugin_name=plugin_name)

        # WSL 探测
        self.is_wsl = self._check_is_wsl()
        if self.is_wsl:
            self.logger.info("🐧 检测到 WSL 环境，将启用 Windows 互操作模式")

        self.prompt_bilibili_mindmap = str(
            get_config(
                "prompt_bilibili_mindmap",
                plugin_name=plugin_name,
                default=MINDMAP_SYSTEM_PROMPT_DEFAULT,
            )
            or MINDMAP_SYSTEM_PROMPT_DEFAULT
        )
        self.prompt_youtube_mindmap = str(
            get_config(
                "prompt_youtube_mindmap",
                plugin_name=plugin_name,
                default=MINDMAP_SYSTEM_PROMPT_DEFAULT,
            )
            or MINDMAP_SYSTEM_PROMPT_DEFAULT
        )
        self.mindmap_layout = str(
            get_config("mindmap_layout", plugin_name=plugin_name, default="vertical") or "vertical"
        ).strip().lower()

        # Danmaku settings
        self.danmaku_font_size = int(get_config("danmaku_font_size", plugin_name=plugin_name, default=50))
        self.danmaku_line_spacing = float(get_config("danmaku_line_spacing", plugin_name=plugin_name, default=1.2))
        self.danmaku_display_region_ratio = float(get_config("danmaku_display_region_ratio", plugin_name=plugin_name, default=0.8))
        self.danmaku_limit_window_seconds = float(get_config("danmaku_limit_window_seconds", plugin_name=plugin_name, default=5))
        self.danmaku_max_per_window = int(get_config("danmaku_max_per_window", plugin_name=plugin_name, default=20))
        self.bilibili_danmaku_webmask_enabled = bool(get_config("bilibili_danmaku_webmask_enabled", plugin_name=plugin_name, default=True))
        self.bilibili_video_crf = int(get_config("bilibili_video_crf", plugin_name=plugin_name, default=20))
        self.bilibili_max_download_duration = int(get_config("bilibili_max_download_duration", plugin_name=plugin_name, default=300))
        self.douyin_max_download_duration = max(
            1,
            int(
                get_config(
                    "douyin_max_download_duration",
                    plugin_name=plugin_name,
                    default=300,
                )
            ),
        )
        self.ffmpeg_bin = self._resolve_media_tool(
            "ffmpeg",
            plugin_name=plugin_name,
            configured_path=str(get_config("ffmpeg_path", plugin_name=plugin_name, default="") or ""),
        )
        self.ffprobe_bin = self._resolve_media_tool(
            "ffprobe",
            plugin_name=plugin_name,
            configured_path=str(get_config("ffprobe_path", plugin_name=plugin_name, default="") or ""),
        )
        self.yt_dlp_command = self._resolve_ytdlp_command()
        self.logger.info(f"🎞️ FFmpeg: {self.ffmpeg_bin}")
        self.logger.info(f"🎞️ FFprobe: {self.ffprobe_bin}")
        self.logger.info("📥 yt-dlp: %s", subprocess.list2cmdline(self.yt_dlp_command))

        # Local ASR settings
        self.local_asr_enabled = bool(
            get_config("local_asr_enabled", plugin_name=plugin_name, default=True)
        )
        self.local_asr_max_duration_minutes = max(
            1,
            int(
                get_config(
                    "local_asr_max_duration_minutes",
                    plugin_name=plugin_name,
                    default=35,
                )
            ),
        )
        self.local_asr_timeout_seconds = max(
            30,
            int(
                get_config(
                    "local_asr_timeout_seconds",
                    plugin_name=plugin_name,
                    default=600,
                )
            ),
        )
        def resolve_local_asr_path(config_key: str, default_path: str) -> str:
            configured = str(
                get_config(config_key, plugin_name=plugin_name, default=default_path)
                or default_path
            ).strip()
            configured = os.path.expandvars(os.path.expanduser(configured))
            return configured if os.path.isabs(configured) else os.path.abspath(configured)

        self.local_asr_runtime_path = resolve_local_asr_path(
            "local_asr_runtime_path",
            os.path.join(
                "data", "models", "sensevoice", "llama-funasr-sensevoice.exe"
            ),
        )
        self.local_asr_model_path = resolve_local_asr_path(
            "local_asr_model_path",
            os.path.join(
                "data", "models", "sensevoice", "sensevoice-small-f32.gguf"
            ),
        )
        self.local_asr_vad_path = resolve_local_asr_path(
            "local_asr_vad_path",
            os.path.join("data", "models", "sensevoice", "fsmn-vad.gguf"),
        )
        if self.local_asr_enabled:
            missing_asr_resources = [
                path
                for path in (
                    self.local_asr_runtime_path,
                    self.local_asr_model_path,
                    self.local_asr_vad_path,
                )
                if not os.path.isfile(path)
            ]
            if missing_asr_resources:
                self.logger.error(
                    "❌ 本地 ASR 已启用，但资源不存在: %s",
                    ", ".join(missing_asr_resources),
                )
            else:
                self.logger.info(
                    "🎙️ 本地 ASR 已启用: SenseVoice F32 / CPU AVX2, 最大时长=%s 分钟",
                    self.local_asr_max_duration_minutes,
                )

        # XHS settings
        self.xhs_max_download_duration = int(
            get_config("xhs_max_download_duration", plugin_name=plugin_name, default=300)
        )
        self.xhs_max_images = int(get_config("xhs_max_images", plugin_name=plugin_name, default=9))

        # YouTube settings
        self.yt_transcript_proxy = get_config("yt_transcript_proxy", plugin_name=plugin_name, default="")
        self.yt_transcript_local_port = int(get_config("yt_transcript_local_port", plugin_name=plugin_name, default=7897))

        # Bilibili settings
        self.bilibili_burn_danmu = bool(get_config("bilibili_burn_danmu", plugin_name=plugin_name, default=True))
        self.bili_cookie_email_alert_enabled = bool(
            get_config("bili_cookie_email_alert_enabled", plugin_name=plugin_name, default=True)
        )
        self.bili_cookie_alert_cooldown_sec = int(
            get_config("bili_cookie_alert_cooldown_sec", plugin_name=plugin_name, default=21600)
        )
        self._bili_cookie_alert_last_ts = 0.0

        # 横向脑图才需要约 1 MB 的 D3/Markmap 资源，按需加载。
        self.js_d3 = ""
        self.js_markmap_lib = ""
        self.js_markmap_view = ""
        if self._resolve_mindmap_layout() == "horizontal":
            self._load_local_assets()

        # WebDriver 单例管理
        self.driver: Optional[webdriver.Chrome] = None
        self.driver_lock = threading.RLock()
        self.driver_operation_lock = get_shared_chrome_operation_lock()
        with self.driver_lock:
            self._init_webdriver()

    # -------------------------
    # 资源管理
    # -------------------------
    def _temp_dir(self, category: str) -> str:
        artifacts = getattr(self, "artifacts", None)
        if artifacts is not None:
            return str(artifacts.category_dir(category))
        legacy = os.path.join(os.getcwd(), "tmp", category)
        os.makedirs(legacy, exist_ok=True)
        return legacy

    def dispatch_operation(
        self,
        kind: str,
        chat_name: str,
        url: str,
        target: Callable[[OperationContext], Any],
        *,
        title: Optional[str] = None,
    ) -> DispatchDecision:
        if self.dispatcher is None:
            raise RuntimeError("Summary Plus 托管调度器未初始化")
        return self.dispatcher.submit(
            kind,
            chat_name,
            url,
            target,
            title=title,
        )

    def health_snapshot(self) -> Dict[str, Any]:
        dispatcher = self.dispatcher.snapshot() if self.dispatcher else {"mode": "compatibility"}
        artifact_inventory = self.artifacts.inventory() if self.artifacts else {}
        driver_ready = self.driver is not None
        status = "healthy" if driver_ready else "degraded"
        legacy_bytes = sum(
            int(item.get("bytes") or 0)
            for item in artifact_inventory.get("legacy", [])
        )
        message = "浏览器与托管队列已就绪" if driver_ready else "浏览器驱动尚未就绪，平台直链任务仍可运行"
        return {
            "status": status,
            "message": message,
            "driver_ready": driver_ready,
            "dispatcher": dispatcher,
            "artifacts": artifact_inventory,
            "legacy_artifact_bytes": legacy_bytes,
            "migration_notes": list(self.storage_migration_notes),
        }

    def close(self) -> None:
        if self.dispatcher is not None:
            self.dispatcher.close()
        operation_lock = getattr(self, "driver_operation_lock", threading.RLock())
        with operation_lock:
            with self.driver_lock:
                self._close_driver()

    def _load_local_assets(self):
        """预加载本地 JS 依赖以供 HTML 模板使用"""
        if self.js_d3 and self.js_markmap_lib and self.js_markmap_view:
            return
        assets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
        self.js_d3 = ""
        self.js_markmap_lib = ""
        self.js_markmap_view = ""

        try:
            d3_path = os.path.join(assets_dir, "d3.min.js")
            lib_path = os.path.join(assets_dir, "markmap-lib.js")
            view_path = os.path.join(assets_dir, "markmap-view.js")

            if os.path.exists(d3_path):
                with open(d3_path, 'r', encoding='utf-8') as f: self.js_d3 = f.read()
            if os.path.exists(lib_path):
                with open(lib_path, 'r', encoding='utf-8') as f: self.js_markmap_lib = f.read()
            if os.path.exists(view_path):
                with open(view_path, 'r', encoding='utf-8') as f: self.js_markmap_view = f.read()

            self.logger.info(f"📦 已加载本地 JS 资源 (D3: {len(self.js_d3)}, Lib: {len(self.js_markmap_lib)}, View: {len(self.js_markmap_view)})")
        except Exception as e:
            self.logger.error(f"❌ 加载本地资源失败: {e}")

    # -------------------------
    # Chrome / WebDriver 管理
    # -------------------------
    # 抖音解析（TikHub）
    # -------------------------
    def _get_bili_cookies_path(self) -> str:
        """获取唯一受支持的 B 站 Cookie 文件路径。"""
        return getattr(
            self,
            "_bili_cookie_path",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt"),
        )

    def _verify_bili_login_by_cookie_map(self, cookie_map: dict) -> bool:
        """通过 cookie 映射调用 B 站 nav 接口验证登录态"""
        try:
            required = ("SESSDATA", "DedeUserID")
            if not all(cookie_map.get(k) for k in required):
                self.logger.warning("⚠️ Cookie 缺少关键登录字段 (SESSDATA / DedeUserID)")
                return False

            cookie_header = "; ".join(f"{k}={v}" for k, v in cookie_map.items() if v)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://www.bilibili.com/",
                "Cookie": cookie_header,
            }
            resp = requests.get("https://api.bilibili.com/x/web-interface/nav", headers=headers, timeout=8)
            payload = resp.json() if resp.ok else {}
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            is_login = bool(data.get("isLogin"))
            if is_login:
                uname = data.get("uname", "")
                if uname:
                    self.logger.info(f"✅ B 站登录态验证通过，账号: {uname}")
                else:
                    self.logger.info("✅ B 站登录态验证通过")
                return True

            self.logger.warning("⚠️ B 站 nav 接口返回未登录状态")
            return False
        except Exception as e:
            self.logger.warning(f"⚠️ 验证 B 站登录态失败: {e}")
            return False

    def _verify_bili_login_by_cookies(self, cookies: List[dict]) -> bool:
        """通过 cookies 调用 B 站 nav 接口验证登录态"""
        cookie_map = {}
        for c in cookies:
            name = str(c.get("name", "")).strip()
            value = str(c.get("value", "")).strip()
            if name and value:
                cookie_map[name] = value
        return self._verify_bili_login_by_cookie_map(cookie_map)

    def _load_netscape_cookie_map(self, file_path: str) -> dict:
        """读取 Netscape cookies.txt 并转换为 cookie 映射"""
        cookie_map = {}
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) < 7:
                        continue
                    name = parts[5].strip()
                    value = parts[6].strip()
                    if name and value:
                        cookie_map[name] = value
        except Exception as e:
            self.logger.warning(f"⚠️ 读取 Cookie 文件失败: {file_path}, {e}")
        return cookie_map

    def _verify_bili_login_by_cookie_file(self, file_path: str) -> bool:
        """验证本地 cookie 文件是否仍为有效登录态"""
        if not os.path.exists(file_path):
            return False
        cookie_map = self._load_netscape_cookie_map(file_path)
        if not cookie_map:
            self.logger.warning(f"⚠️ Cookie 文件为空或格式不兼容: {file_path}")
            return False
        return self._verify_bili_login_by_cookie_map(cookie_map)

    def _send_bili_cookie_failure_email(self, chat_name: str) -> None:
        """发送 B 站 cookie 失效告警邮件（带冷却）"""
        if not self.bili_cookie_email_alert_enabled:
            return

        now = time.time()
        cooldown = max(60, int(self.bili_cookie_alert_cooldown_sec or 21600))
        if now - self._bili_cookie_alert_last_ts < cooldown:
            self.logger.info("ℹ️ B站 Cookie 告警邮件处于冷却期，跳过发送")
            return

        self._bili_cookie_alert_last_ts = now
        cookies_path = self._get_bili_cookies_path()
        body = (
            "summary_plus 检测到 B 站 Cookie 失效。\n\n"
            "判定条件：\n"
            "1) 当前本地 Cookie nav 校验失败\n"
            "2) 自动抓取后 nav 再次校验失败\n\n"
            f"会话: {chat_name or '(unknown)'}\n"
            f"Cookie 文件: {cookies_path}\n"
            f"时间戳: {int(now)}\n\n"
            "建议：在调试 Chrome 中重新登录 B 站，再重试。"
        )
        try:
            ok = get_email_service().send_email(body, "🚨 summary_plus B站Cookie失效告警")
            if ok:
                self.logger.info("✅ 已发送 B站 Cookie 失效告警邮件")
            else:
                self.logger.error("❌ B站 Cookie 失效告警邮件发送失败")
        except Exception as e:
            self.logger.error(f"❌ 发送 B站 Cookie 失效告警邮件异常: {e}")

    def _ensure_bili_cookie_login_ready(self, wx: Any = None, chat_name: str = "") -> bool:
        """
        确保 B 站 cookie 可用：
        1) 先校验现有 cookie 文件(nav)
        2) 失败则自动抓取并再次校验(nav)
        3) 两次都失败则触发告警
        """
        cookies_path = self._get_bili_cookies_path()

        if self._verify_bili_login_by_cookie_file(cookies_path):
            return True

        self.logger.warning(f"⚠️ 本地 Cookie nav 校验失败，尝试自动抓取: {cookies_path}")
        login_ok = self._update_bili_cookies_from_browser()
        if login_ok and self._verify_bili_login_by_cookie_file(cookies_path):
            return True

        self.notify_login_failure(wx, chat_name)
        self._send_bili_cookie_failure_email(chat_name=chat_name)
        return False

    def _update_bili_cookies_from_browser(self):
        """用 Selenium 驱动获取 B 站 Cookie 并保存为 Netscape 格式"""
        operation_lock = getattr(self, "driver_operation_lock", threading.RLock())
        with operation_lock:
            return self._update_bili_cookies_from_browser_locked()

    def _update_bili_cookies_from_browser_locked(self):
        """在浏览器操作锁内刷新 B 站 Cookie。"""
        cookies_path = self._get_bili_cookies_path()
        try:
            driver = self._ensure_driver_available()
            self.logger.info("🔄 正在通过 Selenium 提取 B 站 Cookie...")

            # 记录原始窗口和 URL
            original_handle = driver.current_window_handle
            # 通过 CDP/Selenium 创建工作标签页。直接依赖 window.open 可能被
            # Chrome 拦截，并导致下方从空句柄列表取 [0] 时越界。
            open_blank_worker_tab(driver, self.logger)
            driver.get("https://www.bilibili.com")

            # 等待 B 站加载一点点
            time.sleep(1.5)

            cookies = driver.get_cookies()

            # 关闭临时标签页并切换回原始窗口
            try:
                driver.close()
            except Exception:
                pass
            try:
                driver.switch_to.window(original_handle)
            except Exception:
                pass

            if not cookies:
                self.logger.warning("⚠️ Selenium 未能获取到任何 Cookie")
                return False

            # 关键：先验证登录，再写入文件，避免覆盖已有有效 cookie
            if not self._verify_bili_login_by_cookies(cookies):
                self.logger.warning(
                    f"⚠️ 本次提取的 B 站 Cookie 未通过登录校验，已放弃覆盖: {cookies_path}"
                )
                return False

            self._save_cookies_as_netscape(cookies, cookies_path)
            self.logger.info(f"✅ B 站 Cookie 提取并保存成功: {cookies_path}")
            return True

        except Exception as e:
            self.logger.error(f"❌ 通过 Selenium 更新 B 站 Cookie 发生异常: {e}")
            return False

    def _save_cookies_as_netscape(self, cookies: List[dict], file_path: str):
        """将 Selenium 格式的 cookies 转换为 Netscape (yt-dlp 支持) 格式"""
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write("# http://curl.haxx.se/rfc/cookie_spec.html\n")
            f.write("# This is a generated file!  Do not edit.\n\n")

            for cookie in cookies:
                # Netscape 格式: domain, is_domain_flag, path, is_secure, expires, name, value
                # domain: .bilibili.com
                # is_domain_flag: TRUE/FALSE (如果以 . 开头通常为 TRUE)
                # is_secure: TRUE/FALSE
                # expires: timestamp (0 means session)

                domain = cookie.get('domain', '')
                # Netscape 格式要求：如果是子域名包含模式，domain 必须以 . 开头，且 flag 为 TRUE
                # 如果 Selenium 返回的 domain 不带 .，我们根据情况补上
                is_domain_flag = 'TRUE'
                if not domain.startswith('.'):
                    if domain.count('.') >= 1: # 比如 bilibili.com -> .bilibili.com
                        domain = '.' + domain
                    else:
                        is_domain_flag = 'FALSE'

                path = cookie.get('path', '/')
                is_secure = 'TRUE' if cookie.get('secure', False) else 'FALSE'

                # expiry 可能为 None (session cookie)
                expiry = cookie.get('expiry')
                expires = int(expiry) if expiry is not None else 0

                name = cookie.get('name', '')
                value = cookie.get('value', '')

                line = f"{domain}\t{is_domain_flag}\t{path}\t{is_secure}\t{expires}\t{name}\t{value}\n"
                f.write(line)

    def notify_login_failure(self, wx: Any, chat_name: str):
        """通知用户 B 站登录失效 (已改为仅记录日志)"""
        msg = "⚠️ 【B站 AI 字幕】检测到 B 站登录已失效或无法从浏览器获取 Cookie。 请确保已经在调试用的 Chrome 中登录了 B 站，且该浏览器没有被完全锁定。"
        self.logger.warning(msg)
        # if wx:
        #     try:
        #         wx.send_message(chat_name, msg)
        #     except Exception as e:
        #         self.logger.error(f"❌ 发送登录失败通知失败: {e}")


    # -------------------------
    # Delegated Services (subtitle/local ASR/mindmap/browser)
    # -------------------------
    async def _generate_bilibili_mindmap_async(
        self,
        b_url: str,
        wx: Any,
        chat_name: str,
        article_text: Optional[str] = None,
        output_prefix: str = "bili_map",
    ):
        try:
            # 1. get subtitles ( if not provided )
            if not article_text:
                article_text = self._bili_get_subtitles(b_url)
            if not article_text:
                if not self.local_asr_enabled:
                    self.logger.info("[*] 未找到字幕且本地 ASR 已关闭，跳过脑图生成。")
                    return
                duration = self._check_bilibili_duration(b_url)
                max_seconds = self.local_asr_max_duration_minutes * 60
                if duration is None:
                    self.logger.warning("[!] 无法确认视频时长，为避免超限，跳过本地 ASR。")
                    return
                if duration > max_seconds:
                    self.logger.info(
                        "[*] 未找到字幕且视频时长 %s 秒超过本地 ASR 上限 %s 秒，跳过。",
                        duration,
                        max_seconds,
                    )
                    return
                self.logger.info("[*] 未找到字幕，切换到本地 SenseVoice ASR...")
                article_text = self._bili_transcribe_local(b_url)

            if not article_text:
                self.logger.warning("[!] 未提取到任何有效文字内容，无法生成思维导图。")
                #if wx: wx.send_message(chat_name, "❌ 未提取到任何有效文字内容，无法生成思维导图。")
                return

            self.logger.info(f"[*] 提取到文字内容长度：{len(article_text)}。正在通过 LLM 构建思维导图结构...")
            # if wx: wx.send_message(chat_name, "⏳ 内容提取成功，正在生成思维导图...")

            # 2. summarize to mindmap
            my_json = self._bili_summarize_to_mindmap(article_text)
            if is_mindmap_skip_response(my_json):
                reason = get_mindmap_skip_reason(my_json) or "内容信息密度不足"
                self.logger.info("[*] 跳过脑图生成：%s", reason)
                return
            if not my_json:
                self.logger.warning("[!] LLM 生成导图失败。")
                return

            base_dir = self._temp_dir("mindmaps")
            os.makedirs(base_dir, exist_ok=True)
            uid = uuid.uuid4().hex[:8]
            layout = self._resolve_mindmap_layout()
            png_file = os.path.join(
                base_dir,
                f"{output_prefix}_{int(time.time())}_{uid}_{layout}.png",
            )

            self.logger.info(f"[*] 正在渲染并截取高清脑图（模式: {layout}）...")
            success = await self._render_mindmap_to_image(my_json, png_file)

            if success:
                self.logger.info(f"✨ 流程全部完成！导图图片: {png_file}")
            else:
                self.logger.error("❌ 渲染脑图失败")

            # send pic
            if wx and os.path.exists(png_file):
                if hasattr(wx, "send_files"):
                    wx.send_files(chat_name, [png_file])
                elif hasattr(wx, "SendFiles"):
                    wx.SendFiles(png_file, chat_name)
                else:
                    self.logger.error("❌ 无法发送脑图图片: chat=%s file=%s (wx 实例缺少 send_files/SendFiles)", chat_name, png_file)

        except Exception as e:
            self.logger.error(f"[❌] 脑图生成流程执行出错: {e}", exc_info=True)

    async def _generate_douyin_mindmap_async(
        self,
        douyin_url: str,
        wx: Any,
        chat_name: str,
        article_text: str,
    ):
        """Render Douyin ASR text with the mature Bilibili mindmap pipeline."""
        await self._generate_bilibili_mindmap_async(
            douyin_url,
            wx,
            chat_name,
            article_text=article_text,
            output_prefix="douyin_map",
        )

    async def _generate_youtube_mindmap_async(self, yt_url: str, wx: Any, chat_name: str):
        """异步处理 YouTube 脑图逻辑：获取字幕 -> LLM 总结 -> 生成脑图 -> 发送"""
        try:
            self.logger.info(f"🎬 开始处理 YouTube 脑图: {yt_url}")

            # 1. 获取字幕
            try:
                proxy_dict = None
                if self.yt_transcript_proxy:
                    proxy_dict = {
                        "http": self.yt_transcript_proxy,
                        "https": self.yt_transcript_proxy,
                    }

                article_text = get_best_transcript_text(
                    yt_url,
                    proxy_urls=proxy_dict,
                    local_proxy_port=self.yt_transcript_local_port,
                    debug=True,
                )
                if not article_text:
                    self.logger.warning(f"⚠️ YouTube 视频未获取到有效字幕内容: {yt_url}")
                    return
            except Exception as e:
                self.logger.error(f"❌ 获取 YouTube 字幕失败: {e}")
                return

            # 2. 总结生成脑图 JSON
            mindmap_json = self._yt_summarize_to_mindmap(article_text)
            if is_mindmap_skip_response(mindmap_json):
                reason = get_mindmap_skip_reason(mindmap_json) or "内容信息密度不足"
                self.logger.info("⚠️ YouTube 内容跳过脑图: %s", reason)
                return
            if mindmap_json:
                # 3. 生成预览图
                base_dir = self._temp_dir("mindmaps")
                os.makedirs(base_dir, exist_ok=True)

                uid = uuid.uuid4().hex[:8]
                layout = self._resolve_mindmap_layout()
                png_file = os.path.join(base_dir, f"yt_map_{int(time.time())}_{uid}_{layout}.png")

                success = await self._render_mindmap_to_image(mindmap_json, png_file)

                if success and os.path.exists(png_file):
                    # 4. 发送图片
                    if wx:
                        if hasattr(wx, "send_files"):
                            wx.send_files(chat_name, [png_file])
                        elif hasattr(wx, "SendFiles"):
                            wx.SendFiles(png_file, chat_name)
                    self.logger.info(f"✅ YouTube 脑图发送成功: {png_file}")
                else:
                    self.logger.error("❌ YouTube 脑图渲染失败")
            else:
                self.logger.warning("⚠️ YouTube 脑图总结内容为空")

        except Exception as e:
            self.logger.error(f"❌ 生成 YouTube 脑图流程异常: {e}", exc_info=True)

    def _yt_summarize_to_mindmap(self, text: str):
        """针对 YouTube 内容生成脑图 JSON"""
        try:
            return self._summarize_to_mindmap_json(
                text=text,
                system_prompt=self.prompt_youtube_mindmap or MINDMAP_SYSTEM_PROMPT_DEFAULT,
                call_type="youtube_mindmap",
            )
        except Exception as e:
            self.logger.error(f"❌ YouTube 脑图 LLM 总结失败: {e}")
            return None


    def _bili_transcribe_local(self, url: str) -> str:
        return bili_transcribe_local(
            url=url,
            cookies_path=self._get_bili_cookies_path(),
            yt_dlp_command=self.yt_dlp_command,
            ffmpeg_bin=self.ffmpeg_bin,
            runtime_path=self.local_asr_runtime_path,
            model_path=self.local_asr_model_path,
            vad_path=self.local_asr_vad_path,
            timeout_sec=self.local_asr_timeout_seconds,
            cache_enabled=True,
            cache_dir=(
                str(self.context.storage.cache_path("asr/.keep").parent)
                if self.context is not None
                else None
            ),
            logger=self.logger,
        )

    def _douyin_transcribe_local(self, url: str) -> str:
        with ytdlp_browser_cookie_args(
            platform="douyin",
            debug_port=self.chrome_debug_port,
            user_data_dir=self.chrome_user_data_dir,
            profile_dir=self.chrome_profile_dir,
            logger=self.logger,
        ) as cookie_args:
            return douyin_transcribe_local(
                url=url,
                cookie_args=cookie_args,
                yt_dlp_command=self.yt_dlp_command,
                ffmpeg_bin=self.ffmpeg_bin,
                runtime_path=self.local_asr_runtime_path,
                model_path=self.local_asr_model_path,
                vad_path=self.local_asr_vad_path,
                timeout_sec=self.local_asr_timeout_seconds,
                cache_enabled=True,
                cache_dir=(
                    str(self.context.storage.cache_path("asr/.keep").parent)
                    if self.context is not None
                    else None
                ),
                logger=self.logger,
            )

    def _bili_get_subtitles(self, url: str) -> Optional[str]:
        return bili_get_subtitles(
            url=url,
            cookies_path=self._get_bili_cookies_path(),
            logger=self.logger,
            temp_dir=self._temp_dir("subtitles"),
        )

    def _bili_summarize_to_mindmap(self, text: str):
        return self._summarize_to_mindmap_json(
            text=text,
            system_prompt=self.prompt_bilibili_mindmap or MINDMAP_SYSTEM_PROMPT_DEFAULT,
            call_type="bilibili_mindmap",
        )

    def _summarize_to_mindmap_json(self, text: str, system_prompt: str, call_type: str):
        return summarize_to_mindmap_json(
            llm_manager=self.llm_manager,
            text=text,
            system_prompt=system_prompt,
            call_type=call_type,
            plugin_name="summary_plus",
        )

    def _resolve_mindmap_layout(self) -> str:
        return resolve_mindmap_layout(self.mindmap_layout)

    async def _render_mindmap_to_image(self, mindmap_json: dict, png_path: str) -> bool:
        if self._resolve_mindmap_layout() == "horizontal":
            self._load_local_assets()
        rendered = await render_mindmap_to_image(
            mindmap_json=mindmap_json,
            png_path=png_path,
            layout=self.mindmap_layout,
            js_d3=self.js_d3,
            js_markmap_lib=self.js_markmap_lib,
            js_markmap_view=self.js_markmap_view,
            logger=self.logger,
        )
        artifacts = getattr(self, "artifacts", None)
        if rendered and artifacts is not None:
            artifacts.validate_file(png_path)
        return rendered


    # -------------------------
    # 浏览器摘要（Selenium + OpenAI）
    # -------------------------
    def summarize_url(
        self,
        url: str,
        is_link_message: bool = False,
        chat_name: str = "",
        sender: str = "",
    ) -> Optional[str]:
        actual_url = None if url == "LINK_MESSAGE_CLICKED" else url
        return self._browser_summarize(
            actual_url,
            is_link_message,
            chat_name=chat_name,
            sender=sender,
        )

    def translate_text_for_special_group(self, chinese_text: str) -> Optional[str]:
        try:
            target_lang = self.special_translation_target_language
            prompt = f"你是专业翻译，请将下面这段中文翻译成{target_lang}，保持原文段落和格式，保持语言地道、自然，不要附加任何说明，只输出翻译结果。"

            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": chinese_text},
            ]

            response = self.llm_manager.call(
                plugin_name="summary_plus",
                call_type="translate",
                messages=messages
            )
            return response.strip()
        except Exception as e:
            self.logger.error(f"❌ 特殊群翻译失败: {e}")
            return None

    def _browser_summarize(
        self,
        url: Optional[str],
        is_link_message: bool = False,
        chat_name: str = "",
        sender: str = "",
    ) -> Optional[str]:
        operation_lock = getattr(self, "driver_operation_lock", threading.RLock())
        with operation_lock:
            return browser_summarize(
                self,
                url,
                is_link_message,
                chat_name=chat_name,
                sender=sender,
            )



# 全局实例
summary_service: Optional[SummaryService] = None


def handle_link_message(event: Event):
    """处理链接消息事件（委托到 platform_service）"""
    global summary_service
    svc = summary_service
    if svc is None:
        return False
    return route_link_message(event=event, svc=svc, logger=logger)


def register(event_bus, subscribe, context: PluginContext):
    global summary_service
    logger.info("📰 Registering summary_plus plugin...")
    summary_service = SummaryService(context=context)
    context.health.register(summary_service.health_snapshot)
    context.register_cleanup(unregister)
    if summary_service.storage_migration_notes:
        context.audit.record(
            "storage_migration",
            summary="Summary Plus 已接入插件标准存储目录",
            details={"notes": summary_service.storage_migration_notes},
        )
    # 默认阻断由 config.json 控制；此处不强制覆盖
    subscribe(event_type=EventType.LINK_MESSAGE_RECEIVED, handler=handle_link_message)
    logger.info("✅ summary_plus 插件注册成功")


def unregister():
    """取消注册插件"""
    global summary_service
    logger.info("📰 Unregistering summary_plus plugin...")
    if summary_service is not None:
        summary_service.close()
    summary_service = None
    logger.info("✅ summary_plus 插件卸载完成")
