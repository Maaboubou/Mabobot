"""Chrome/WebDriver lifecycle for Summary Plus."""

from __future__ import annotations

import os
import subprocess
import time
from typing import List, Optional, cast
from urllib.parse import urlparse

import requests
from selenium import webdriver

from app.utils.subprocess_utils import hidden_process_kwargs


class BrowserRuntimeMixin:
    """Own the single automation browser and its recovery lifecycle."""

    def _open_background_page(self, url):
        from .background_page import BackgroundPage
        # The existing Chrome owns the login/profile. Never attach Selenium to
        # a new tab, activate it, or launch a replacement foreground browser.
        return BackgroundPage(
            self.chrome_debug_port, url,
            command_timeout=max(3, int(self.webdriver_command_timeout_sec or 8)),
        )

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
            driver.quit()
            self.logger.info(f"✅ WebDriver 已关闭{f' ({reason})' if reason else ''}")
        except Exception as e:
            self.logger.warning(f"⚠️ WebDriver quit 失败{f' ({reason})' if reason else ''}: {e}")
        finally:
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
                self.logger.error("❌ WebDriver 初始化失败，driver 为 None")
        except Exception as e:
            self.logger.error(f"❌ WebDriver 初始化异常: {e}", exc_info=True)
            self.driver = None

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
            max_attempts = 10
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

    def _is_debug_port_ready(self) -> bool:
        """检查远程调试端口是否可用"""
        try:
            resp = requests.get(f"http://127.0.0.1:{self.chrome_debug_port}/json/version", timeout=1.0)
            if resp.status_code != 200:
                return False
            data = resp.json()
            browser = str(data.get("Browser") or data.get("product") or "")
            websocket_url = str(data.get("webSocketDebuggerUrl") or "")
            if browser.startswith("Chrome/") and websocket_url.startswith("ws://"):
                return True
            self.logger.warning(
                f"⚠️ 端口 {self.chrome_debug_port} 响应了请求，但不是 Chrome DevTools: {browser or 'unknown'}"
            )
            return False
        except Exception:
            return False

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

    def _chrome_process_match_terms(self) -> List[str]:
        terms = [f"--remote-debugging-port={self.chrome_debug_port}"]
        user_data_dir = os.path.abspath(self.chrome_user_data_dir)

        if self.is_wsl:
            try:
                user_data_dir = subprocess.check_output(
                    ["wslpath", "-w", user_data_dir],
                    text=True,
                    **hidden_process_kwargs(),
                ).strip()
            except Exception:
                pass

        if user_data_dir:
            terms.append(f"--user-data-dir={user_data_dir}")
            terms.append(user_data_dir)
        return [term for term in terms if term]

    def _terminate_automation_chrome_processes(self) -> None:
        """按命令行特征清理本插件拉起的 Chrome，避免残留自动化实例堆积。"""
        terms = self._chrome_process_match_terms()
        if not terms:
            return

        ps_terms = ", ".join("'" + term.replace("'", "''") + "'" for term in terms)
        ps_script = (
            f"$terms=@({ps_terms}); "
            "Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | "
            "Where-Object { "
            "$cmd=$_.CommandLine; "
            "$cmd -and ($terms | Where-Object { $cmd -like \"*$_*\" }) "
            "} | ForEach-Object { "
            "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue "
            "}"
        )

        cmd = "powershell.exe" if self.is_wsl else "powershell"
        try:
            result = subprocess.run(
                [cmd, "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=10,
                **hidden_process_kwargs(),
            )
            if result.returncode == 0:
                self.logger.info("✅ 已按自动化 Chrome 命令行特征清理残留进程")
            else:
                self.logger.warning(f"⚠️ 自动化 Chrome 命令行清理失败: {result.stderr.strip()}")
        except Exception as e:
            self.logger.warning(f"⚠️ 自动化 Chrome 命令行清理异常: {e}")

    def _is_windows_chrome_pid(self, pid: str) -> bool:
        cmd = "powershell.exe" if self.is_wsl else "powershell"
        try:
            result = subprocess.run(
                [
                    cmd,
                    "-NoProfile",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {int(pid)}\").Name",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                **hidden_process_kwargs(),
            )
            return result.stdout.strip().lower() == "chrome.exe"
        except Exception as e:
            self.logger.warning(f"⚠️ 检查 PID {pid} 进程类型失败: {e}")
            return False

    def _terminate_chrome_process(self):
        """强制终止监听指定调试端口的 Chrome 进程"""
        try:
            if self.is_wsl:
                # 在 WSL 中，我们需要找到监听 9222 端口的 Windows 进程并杀掉它
                self.logger.info(f"🔍 (WSL) 正在查找监听端口 {self.chrome_debug_port} 的 Windows 进程...")
                try:
                    # 使用 netstat.exe 查找 PID
                    cmd_netstat = f"netstat.exe -ano | grep LISTENING | grep :{self.chrome_debug_port}"
                    result = subprocess.run(
                        cmd_netstat,
                        capture_output=True,
                        text=True,
                        shell=True,
                        **hidden_process_kwargs(),
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        # netstat.exe 输出最后一位是 PID
                        lines = result.stdout.strip().split('\n')
                        pids = set()
                        for line in lines:
                            parts = line.split()
                            if len(parts) >= 5:
                                pids.add(parts[-1])

                        for pid in pids:
                            if not self._is_windows_chrome_pid(pid):
                                self.logger.warning(f"⚠️ PID {pid} 不是 chrome.exe，跳过端口清理")
                                continue
                            self.logger.info(f"🔫 (WSL-Win) 找到 PID: {pid}，准备使用 taskkill.exe 终止...")
                            result = subprocess.run(
                                ["taskkill.exe", "/F", "/PID", pid],
                                capture_output=True,
                                text=True,
                                **hidden_process_kwargs(),
                            )
                            if result.returncode == 0:
                                self.logger.info(f"✅ (WSL-Win) 进程 {pid} 已终止")
                            else:
                                stderr = (result.stderr or result.stdout or "").strip()
                                self.logger.warning(f"⚠️ (WSL-Win) 终止进程 {pid} 失败: {stderr}")
                        time.sleep(1)
                    else:
                        self.logger.info(f"🤷 (WSL) 未通过 netstat.exe 找到监听端口 {self.chrome_debug_port} 的进程")
                        # 兜底：直接按名字杀
                        self.logger.info("🛡️ (WSL) 尝试按映像名称清理 chrome.exe...")
                        subprocess.run(
                            [
                                "taskkill.exe",
                                "/F",
                                "/IM",
                                "chrome.exe",
                                "/FI",
                                f"WINDOWTITLE eq *{self.chrome_debug_port}*",
                            ],
                            capture_output=True,
                            **hidden_process_kwargs(),
                        )
                except Exception as e:
                    self.logger.warning(f"⚠️ WSL 互操作关闭 Chrome 失败: {e}")

            elif os.name == "nt":
                command = f'netstat -aon | findstr LISTENING | findstr ":{self.chrome_debug_port}"'
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    shell=True,
                    **hidden_process_kwargs(),
                )
                if result.returncode == 0 and result.stdout.strip():
                    pids = set()
                    for line in result.stdout.strip().splitlines():
                        parts = line.split()
                        if len(parts) >= 5:
                            pids.add(parts[-1])
                    for pid in pids:
                        if not self._is_windows_chrome_pid(pid):
                            self.logger.warning(f"⚠️ PID {pid} 不是 chrome.exe，跳过端口清理")
                            continue
                        self.logger.info(f"🔫 (Windows) 找到监听端口 {self.chrome_debug_port} 的 PID: {pid}，准备终止...")
                        kill_result = subprocess.run(
                            ["taskkill", "/F", "/PID", pid],
                            capture_output=True,
                            text=True,
                            **hidden_process_kwargs(),
                        )
                        if kill_result.returncode == 0:
                            self.logger.info(f"✅ 进程 {pid} 已终止")
                        else:
                            stderr = (kill_result.stderr or kill_result.stdout or "").strip()
                            self.logger.warning(f"⚠️ 终止进程 {pid} 失败: {stderr}")
                    time.sleep(1)
                else:
                    self.logger.info(f"🤷 (Windows) 未找到监听端口 {self.chrome_debug_port} 的进程")
            else:
                command = f"lsof -ti :{self.chrome_debug_port}"
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    shell=True,
                    **hidden_process_kwargs(),
                )
                if result.stdout.strip():
                    pids = result.stdout.strip().split("\n")
                    for pid in pids:
                        if pid.strip():
                            self.logger.info(f"🔫 (Unix) 找到 PID: {pid}，准备终止...")
                            subprocess.run(
                                f"kill -9 {pid}",
                                check=True,
                                shell=True,
                                capture_output=True,
                                **hidden_process_kwargs(),
                            )
                            self.logger.info(f"✅ 进程 {pid} 已终止")
                    time.sleep(1)
                else:
                    self.logger.info(f"🤷 (Unix) 未找到监听端口 {self.chrome_debug_port} 的进程")
        except Exception as e:
            self.logger.error(f"❌ 终止 Chrome 进程时出错: {e}", exc_info=True)
        finally:
            self._terminate_automation_chrome_processes()

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
                resp = requests.get(
                    f"http://127.0.0.1:{self.chrome_debug_port}/json/version",
                    timeout=1.0,
                )
                resp.raise_for_status()
                data = resp.json()
                browser = str(data.get("Browser") or data.get("product") or "unknown")
                if not browser.startswith("Chrome/"):
                    raise RuntimeError(f"调试端口响应异常: {browser}")
                self.logger.info(f"✅ WebDriver HTTP 健康检查通过: {browser}")
                return True
            except Exception as e:
                self.logger.warning(f"⚠️ WebDriver 健康检查失败，将重建: {e}")
                return False

    def _is_blacklisted(self, url: str) -> bool:
        """检查 URL 是否在域名黑名单中。
        支持精确域名（如 example.com）和通配符后缀（如 *.xyz 或直接填 .xyz）。
        """
        if not url or not self.domain_blacklist:
            return False
        try:
            netloc = urlparse(url).netloc.lower()
            # 去掉端口号
            netloc = netloc.split(":")[0]
            if not netloc:
                return False

            for entry in self.domain_blacklist:
                entry = entry.lstrip("*")  # 兼容 *.xyz 写法，统一为 .xyz
                if entry.startswith("."):
                    # 后缀匹配：命中 foo.xyz / bar.baz.xyz 等，但不命中 xyz 本身
                    if netloc.endswith(entry):
                        return True
                else:
                    # 精确匹配或子域名匹配
                    if netloc == entry or netloc.endswith("." + entry):
                        return True
        except Exception as e:
            self.logger.warning(f"⚠️ 检查黑名单时解析 URL 出错: {e}")
        return False

    def _is_sender_blacklisted(self, sender: str) -> bool:
        """检查消息发送者是否在 sender 黑名单中（大小写不敏感）"""
        if not sender or not self.sender_blacklist:
            return False
        return sender.lower().strip() in self.sender_blacklist

    def _ensure_driver_available(self) -> webdriver.Chrome:
        """确保 WebDriver 可用，如果不可用则尝试重新初始化"""
        if not self._is_driver_healthy():
            with self.driver_lock:
                if self.driver:
                    self._quit_webdriver(self.driver, reason="健康检查失败")
                    self.driver = None
                self._init_webdriver()
            if not self._is_driver_healthy():
                raise RuntimeError("无法获取健康的 WebDriver 实例")
        return cast(webdriver.Chrome, self.driver)

    def _close_driver(self):
        """关闭 WebDriver 实例 (假定锁已被外部获取)"""
        if self.driver:
            self._quit_webdriver(self.driver, reason="服务关闭")
            self.driver = None

    # -------------------------
