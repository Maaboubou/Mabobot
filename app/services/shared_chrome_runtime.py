"""Shared Chrome lifecycle for Selenium and CDP browser clients."""

from __future__ import annotations

import logging
import os
import ntpath
import subprocess
import threading
import time
from dataclasses import replace
from typing import Callable, Optional, TypeVar, cast
from urllib.parse import urlparse

import psutil
import requests
from selenium import webdriver

from app.services.shared_chrome import (
    SharedChromeSettings,
    _argument_value,
    get_shared_chrome_operation_lock,
    load_shared_chrome_settings,
)
from app.utils.plugin_config import get_config
from app.utils.subprocess_utils import hidden_process_kwargs

T = TypeVar("T")


def stop_shared_driver_service(driver: webdriver.Chrome) -> None:
    """Disconnect a Selenium client without sending browser-level quit."""
    service = getattr(driver, "service", None)
    if service is not None:
        service.stop()


class SharedChromeRuntimeMixin:
    """Lifecycle shared by persistent and temporary automation clients."""

    def _set_webdriver_command_timeout(self, driver: Optional[webdriver.Chrome] = None) -> None:
        """降低当前 Selenium 会话命令超时，不修改进程级全局状态。"""
        timeout = max(3, int(self.webdriver_command_timeout_sec or 8))
        if not driver:
            return
        try:
            executor = getattr(driver, "command_executor", None)
            client_config = getattr(executor, "_client_config", None)
            if client_config is not None:
                client_config.timeout = timeout
        except Exception as e:
            self.logger.debug(f"设置当前 WebDriver 命令超时失败: {e}")

    def _get_driver_service_pid(self, driver: Optional[webdriver.Chrome]) -> Optional[int]:
        try:
            service = getattr(driver, "service", None)
            process = getattr(service, "process", None)
            pid = getattr(process, "pid", None)
            return int(pid) if pid else None
        except Exception:
            return None

    def _terminate_process_by_pid(self, pid: Optional[int], label: str = "process") -> None:
        if not pid:
            return
        try:
            if self.is_wsl:
                cmd = ["taskkill.exe", "/F", "/PID", str(pid)]
            elif os.name == "nt":
                cmd = ["taskkill", "/F", "/PID", str(pid)]
            else:
                cmd = ["kill", "-9", str(pid)]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=8,
                **hidden_process_kwargs(),
            )
            if result.returncode == 0:
                self.logger.info(f"✅ 已终止 {label}: PID={pid}")
            else:
                stderr = (result.stderr or result.stdout or "").strip()
                self.logger.warning(f"⚠️ 终止 {label} 失败: PID={pid} {stderr}")
        except Exception as e:
            self.logger.warning(f"⚠️ 终止 {label} 异常: PID={pid} {e}")

    def _quit_webdriver(self, driver: Optional[webdriver.Chrome], reason: str = "") -> None:
        if not driver:
            return
        service_pid = self._get_driver_service_pid(driver)
        try:
            self._set_webdriver_command_timeout(driver)
            stop_shared_driver_service(driver)
            self.logger.info(f"✅ WebDriver 已关闭{f' ({reason})' if reason else ''}")
        except Exception as e:
            self.logger.warning(f"⚠️ 停止 ChromeDriver 失败{f' ({reason})' if reason else ''}: {e}")
            self._terminate_process_by_pid(service_pid, "chromedriver")

    def _validate_webdriver_connection(self, driver: webdriver.Chrome) -> None:
        """用浏览器级 CDP 命令验证连接，避免在当前标签页执行 JS 时卡死。"""
        version = driver.execute_cdp_cmd("Browser.getVersion", {})
        browser = version.get("product") or version.get("Browser") or "unknown"
        self.logger.info(f"✅ WebDriver CDP 连接验证成功: {browser}")

    def _init_webdriver(self):
        """初始化 WebDriver 实例 (假定锁已被外部获取)"""
        if self.driver:
            self.logger.info("ℹ️ WebDriver 实例已存在，跳过初始化")
            return
        try:
            self.driver = self._get_webdriver_instance()
            if self.driver:
                self.logger.info("✅ WebDriver 初始化成功")
            else:
                self.driver_browser_identity = None
                self.logger.error("❌ WebDriver 初始化失败，driver 为 None")
        except Exception as e:
            self.logger.error(f"❌ WebDriver 初始化异常: {e}", exc_info=True)
            self.driver = None
            self.driver_browser_identity = None

    def _check_is_wsl(self) -> bool:
        """检查是否在 WSL 环境中运行"""
        try:
            if os.name == "nt":
                return False
            with open("/proc/version", "r") as f:
                return "microsoft" in f.read().lower()
        except Exception:
            return False

    def _get_webdriver_instance(self) -> Optional[webdriver.Chrome]:
        """获取 WebDriver 实例 - 封装创建和连接逻辑"""
        try:
            self.logger.info(f"🔍 检查调试端口 {self.chrome_debug_port} 是否可用")
            if not self._is_debug_port_ready():
                self.logger.info("🚀 调试端口不可用，将启动新的 Chrome 实例")
                self._terminate_chrome_process() # 确保端口彻底释放
                self._start_chrome_debug()
            else:
                self.logger.info("✅ 调试端口已可用，将尝试连接现有 Chrome 实例")

            opts = webdriver.ChromeOptions()
            opts.debugger_address = f"127.0.0.1:{self.chrome_debug_port}"
            self._set_webdriver_command_timeout()

            last_err = None
            max_attempts = max(1, int(getattr(self, "connect_retries", 10)))
            chrome_restarted = False
            for attempt in range(max_attempts):
                try:
                    self.logger.info(f"🔄 尝试连接 WebDriver (第 {attempt + 1}/{max_attempts} 次)")
                    # 关键：设置较短的命令超时，避免在无效会话上卡死
                    driver = webdriver.Chrome(options=opts)
                    self._set_webdriver_command_timeout(driver)
                    try:
                        driver.set_page_load_timeout(max(5, int(self.page_load_timeout or 15)))
                    except Exception:
                        pass

                    # 验证 driver 是否真正可用。不要在当前标签页执行 JS：
                    # Chrome 可能停在新标签页、扩展页或旧的卡死页面。
                    try:
                        self._validate_webdriver_connection(driver)
                        self.logger.info(f"✅ WebDriver 连接成功 (尝试 {attempt + 1}/{max_attempts})")
                    except Exception as e:
                        self.logger.warning(f"⚠️ WebDriver 连接成功但验证失败: {e}")
                        self._quit_webdriver(driver, reason="验证失败")
                        raise e

                    # 注入反爬脚本
                    self._apply_stealth(driver)
                    self.driver_browser_identity = self._debug_browser_identity()
                    # 等待 DevTools session 完全稳定
                    time.sleep(0.75)
                    return driver
                except Exception as e:
                    last_err = e
                    err_msg = str(getattr(e, "msg", str(e))).lower()
                    self.logger.warning(f"⚠️ 第 {attempt + 1} 次连接失败: {err_msg}")

                    # 如果是致命错误或多次失败，强制重启
                    should_restart_chrome = (
                        "target window already closed" in err_msg
                        or "invalid session id" in err_msg
                        or "read timed out" in err_msg
                        or "max retries exceeded" in err_msg
                        or attempt >= 3
                    )
                    if should_restart_chrome and not chrome_restarted:
                        chrome_restarted = True
                        self.logger.warning(f"⚠️ 触发强制重启模式 (错误类型: {'会话失效' if 'invalid' in err_msg else '窗口关闭' if 'window' in err_msg else '重试超限'})")
                        self._terminate_chrome_process()
                        self._start_chrome_debug()
                    elif should_restart_chrome:
                        self.logger.warning("⚠️ 本轮已重启过 Chrome，避免重复拉起新实例，继续重试连接")

                    time.sleep(self.WINDOW_HANDLE_STABILIZE_DELAY)

            error_msg = f"无法连接到 Chrome 调试端口 127.0.0.1:{self.chrome_debug_port}: {last_err}"
            self.logger.error(f"❌ {error_msg}")
            raise RuntimeError(error_msg)
        except Exception as e:
            self.logger.error(f"❌ 创建 WebDriver 实例失败: {e}", exc_info=True)
            return None

    def _debug_browser_identity(self) -> Optional[str]:
        """Return the browser-level DevTools identity for restart detection."""
        try:
            resp = requests.get(f"http://127.0.0.1:{self.chrome_debug_port}/json/version", timeout=1.0)
            if resp.status_code != 200:
                return None
            data = resp.json()
            browser = str(data.get("Browser") or data.get("product") or "")
            websocket_url = str(data.get("webSocketDebuggerUrl") or "")
            parsed = urlparse(websocket_url)
            if (
                browser.startswith("Chrome/")
                and parsed.scheme == "ws"
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            ):
                return websocket_url
            self.logger.warning(
                f"⚠️ 端口 {self.chrome_debug_port} 响应了请求，但不是 Chrome DevTools: {browser or 'unknown'}"
            )
            return None
        except Exception:
            return None

    def _is_debug_port_ready(self) -> bool:
        """检查远程调试端口是否可用"""
        return bool(self._debug_browser_identity())

    def _start_chrome_debug(self):
        """启动 Chrome 调试模式"""
        if self._is_debug_port_ready():
            self.logger.info(f"✅ Chrome 调试端口 {self.chrome_debug_port} 已可用，跳过重复启动")
            return

        user_data_dir = os.path.abspath(self.chrome_user_data_dir)
        try:
            os.makedirs(user_data_dir, exist_ok=True)
        except Exception as e:
            self.logger.warning(f"⚠️ 创建用户数据目录失败: {e}")

        # WSL 路径转换
        if self.is_wsl:
            try:
                # 尝试将 WSL 路径转换为 Windows 路径以供 chrome.exe 使用
                cmd = ["wslpath", "-w", user_data_dir]
                win_user_data_dir = subprocess.check_output(
                    cmd, text=True, **hidden_process_kwargs()
                ).strip()
                self.logger.info(f"📂 WSL 路径转换: {user_data_dir} -> {win_user_data_dir}")
                user_data_dir = win_user_data_dir
            except Exception as e:
                self.logger.warning(f"⚠️ wslpath 转换失败: {e}")

        try:
            # 增强反爬标志
            args = [
                self.chrome_path,
                f"--remote-debugging-port={self.chrome_debug_port}",
                f"--user-data-dir={user_data_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",  # 禁用自动化检测
                "--disable-infobars",  # 禁用 "正在受自动化测试软件控制"
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-browser-side-navigation",
            ]
            if isinstance(self.chrome_profile_dir, str) and self.chrome_profile_dir.strip():
                args.append(f"--profile-directory={self.chrome_profile_dir.strip()}")

            self.logger.info(f"🚀 正在启动 Chrome: {' '.join(args)}")
            # 在 WSL 中使用 subprocess.Popen 启动 Windows 程序是异步的且不会阻塞
            subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                **hidden_process_kwargs(),
            )

            self.logger.info("⏳ 等待 Chrome 调试端口就绪...")
            for i in range(40):
                if self._is_debug_port_ready():
                    self.logger.info(f"✅ Chrome 调试端口在 {i * 0.25:.2f} 秒后就绪")
                    return
                time.sleep(0.25)

            raise RuntimeError("Chrome 调试端口启动超时")
        except Exception as e:
            self.logger.error(f"❌ 启动 Chrome 调试模式失败: {e}", exc_info=True)
            raise

    def _apply_stealth(self, driver: webdriver.Chrome):
        """注入 CDP 脚本以掩盖自动化特征"""
        try:
            # 这里的脚本是核心，用于绕过大多数基础检测（如 navigator.webdriver）
            stealth_js = """
            Object.defineProperty(navigator, 'webdriver', {
              get: () => undefined
            });

            // 掩盖 WebGL 指纹
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
              if (parameter === 37445) return 'Intel Inc.';
              if (parameter === 37446) return 'Intel(R) Iris(TM) Graphics 6100';
              return getParameter.apply(this, arguments);
            };

            // 补充其它常见特征
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """
            # 在每个新页面加载前执行
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": stealth_js
            })
            self.logger.info("🛡️ CDP Stealth 脚本已注入")
        except Exception as e:
            self.logger.warning(f"⚠️ 注入 Stealth 脚本失败: {e}")

    def _terminate_chrome_process(self):
        """Stop only Chrome processes with this managed port AND profile."""
        if self.is_wsl:
            self._terminate_wsl_chrome()
            return

        profile = os.path.normcase(os.path.abspath(self.chrome_user_data_dir))
        owned = []
        for process in psutil.process_iter(["name", "cmdline"]):
            try:
                arguments = list(process.info.get("cmdline") or [])
                name = str(process.info.get("name") or "").casefold()
                executable = ntpath.basename(self.chrome_path).casefold()
                if name not in {executable, "chrome", "chrome.exe", "chromium", "chromium-browser"}:
                    continue
                if _argument_value(arguments, "--type"):
                    continue
                if _argument_value(arguments, "--remote-debugging-port") != str(self.chrome_debug_port):
                    continue
                actual_profile = _argument_value(arguments, "--user-data-dir")
                if not actual_profile or os.path.normcase(os.path.abspath(actual_profile)) != profile:
                    continue
                owned.extend(process.children(recursive=True))
                owned.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        for process in owned:
            try:
                process.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if owned:
            _, alive = psutil.wait_procs(owned, timeout=2)
            for process in alive:
                try:
                    process.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            self.logger.info("已清理共享 Chrome 的受管进程: %s", len(owned))

    def _terminate_wsl_chrome(self):
        """Match both managed flags before closing Windows Chrome from WSL."""
        try:
            profile = subprocess.check_output(
                ["wslpath", "-w", os.path.abspath(self.chrome_user_data_dir)],
                text=True, **hidden_process_kwargs(),
            ).strip()
            if not profile:
                raise RuntimeError("共享 Chrome 用户目录转换为空")
            profile_literal = "'" + profile.replace("'", "''") + "'"
            port_pattern = r'(?:^|\s|")--remote-debugging-port(?:=|\s+)"?' + str(self.chrome_debug_port) + r'(?:\s|"|$)'
            port_literal = "'" + port_pattern.replace("'", "''") + "'"
            script = (
                f"$profile={profile_literal}; $portPattern={port_literal}; "
                "Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | "
                "Where-Object { $cmd=$_.CommandLine; "
                "$cmd -and $cmd -match $portPattern -and "
                "$cmd.IndexOf($profile, [StringComparison]::OrdinalIgnoreCase) -ge 0 "
                "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script],
                capture_output=True, text=True, timeout=10, **hidden_process_kwargs(),
            )
            if result.returncode:
                self.logger.warning("共享 Chrome 受管进程清理失败: %s", result.stderr.strip())
        except Exception as exc:
            self.logger.warning("共享 Chrome 受管进程清理失败: %s", exc)

    def _is_driver_healthy(self) -> bool:
        """检查 WebDriver 健康状态"""
        with self.driver_lock:
            if not self.driver:
                return False
            try:
                # 不在健康检查里执行 Selenium/CDP 命令。
                # 实测 Chrome/ChromeDriver 偶发卡死时，execute_cdp_cmd("Browser.getVersion")
                # 可能无视较短的 RemoteConnection timeout，把用户事件 worker 卡在
                # “已解析链接卡片 URL”之后，导致后续摘要流程没有任何日志。这里改用
                # DevTools HTTP 探活（requests timeout=1s），失败再重建 driver。
                browser_identity = self._debug_browser_identity()
                if not browser_identity:
                    raise RuntimeError("共享 Chrome 调试端口不可用")
                if browser_identity != self.driver_browser_identity:
                    raise RuntimeError("共享 Chrome 已重启，当前 WebDriver 会话已过期")
                self.logger.info("✅ WebDriver HTTP 健康检查通过")
                return True
            except Exception as e:
                self.logger.warning(f"⚠️ WebDriver 健康检查失败，将重建: {e}")
                return False

    def _ensure_driver_available(self) -> webdriver.Chrome:
        """确保 WebDriver 可用，如果不可用则尝试重新初始化"""
        with get_shared_chrome_operation_lock():
            if not self._is_driver_healthy():
                with self.driver_lock:
                    if self.driver:
                        self._quit_webdriver(self.driver, reason="健康检查失败")
                        self.driver = None
                        self.driver_browser_identity = None
                    self._init_webdriver()
                if not self._is_driver_healthy():
                    raise RuntimeError("无法获取健康的 WebDriver 实例")
            return cast(webdriver.Chrome, self.driver)

    def _close_driver(self):
        """关闭 WebDriver 实例 (假定锁已被外部获取)"""
        if self.driver:
            self._quit_webdriver(self.driver, reason="服务关闭")
            self.driver = None
            self.driver_browser_identity = None



class SharedChromeRuntime(SharedChromeRuntimeMixin):
    """Shared lifecycle independent of any plugin service.

    Callers hold the shared Chrome operation lock and own the returned temporary
    driver. Stop its driver service after use to leave the shared browser open.
    """

    WINDOW_HANDLE_STABILIZE_DELAY = 0.5

    def __init__(
        self,
        settings: SharedChromeSettings,
        *,
        logger: logging.Logger,
        page_load_timeout: int,
        webdriver_command_timeout_sec: int = 8,
    ):
        self.logger = logger
        self.chrome_debug_port = settings.debug_port
        self.chrome_path = settings.executable
        self.chrome_user_data_dir = settings.user_data_dir
        self.chrome_profile_dir = settings.profile_dir
        self.page_load_timeout = max(5, int(page_load_timeout))
        self.webdriver_command_timeout_sec = max(3, int(webdriver_command_timeout_sec))
        self.is_wsl = self._check_is_wsl()
        self.driver = None
        self.driver_lock = threading.RLock()
        self.driver_browser_identity = None

    def ensure_browser(self) -> str:
        """Return a local CDP endpoint, starting the managed browser if needed."""
        with get_shared_chrome_operation_lock():
            identity = self._debug_browser_identity()
            if not identity:
                self._terminate_chrome_process()
                self._start_chrome_debug()
                identity = self._debug_browser_identity()
            if not identity:
                raise RuntimeError(f"无法连接共享 Chrome 调试端口 127.0.0.1:{self.chrome_debug_port}")
            return identity

    def connect_driver(self) -> webdriver.Chrome:
        """Create a temporary Selenium session using shared startup/recovery."""
        with get_shared_chrome_operation_lock():
            driver = self._get_webdriver_instance()
            if driver is None:
                raise RuntimeError("共享 Chrome 启动或连接失败，已尝试自动恢复")
            return driver

    def connect_cdp(self, connector: Callable[[str], T]) -> T:
        """Connect a CDP transport; recover once before reporting failure.

        The connector only establishes a transport. Page navigation and business
        errors must happen outside it so they do not restart the shared browser.
        """
        with get_shared_chrome_operation_lock():
            endpoint = self.ensure_browser()
            try:
                return connector(endpoint)
            except Exception:
                self.logger.warning("共享 Chrome CDP 连接失败，尝试重启恢复", exc_info=True)
                self._terminate_chrome_process()
                self._start_chrome_debug()
                return connector(self.ensure_browser())

    def release_driver(self, driver: webdriver.Chrome) -> None:
        """Stop a temporary driver service while keeping shared Chrome open."""
        self._quit_webdriver(driver)


def get_shared_chrome_runtime(
    *,
    settings: Optional[SharedChromeSettings] = None,
    debug_port: Optional[int] = None,
    logger: Optional[logging.Logger] = None,
    page_load_timeout: int = 20,
) -> SharedChromeRuntime:
    """Load the current shared configuration for each new browser operation."""
    settings = settings or load_shared_chrome_settings()
    if debug_port is not None:
        settings = replace(settings, debug_port=int(debug_port))
    return SharedChromeRuntime(
        settings,
        logger=logger or logging.getLogger(__name__),
        page_load_timeout=page_load_timeout,
        webdriver_command_timeout_sec=int(
            get_config("webdriver_command_timeout_sec", 8, plugin_name="summary_plus") or 8
        ),
    )

