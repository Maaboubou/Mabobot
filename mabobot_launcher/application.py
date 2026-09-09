"""Desktop host and JavaScript bridge for the Mabobot launcher."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import psutil

from app.version import APP_VERSION

from .constants import (
    APP_ICON_FILE,
    APP_ICON_IMAGE,
    CONTROL_SIGNAL_FILE,
    INSTALL_SCRIPT,
    PROJECT_ROOT,
    SHOW_SIGNAL_FILE,
    UI_DIR,
)
from .settings import PreferenceStore, WindowsLoginStartup
from .state import clear_launcher_state, consume_control_signal, write_launcher_state
from .supervisor import ServiceSupervisor
from .compatibility import CompatibilityMonitor


_UI_LOAD_TIMEOUT_SECONDS = 8.0


class LauncherApi:
    """Small, explicit API exposed to the local launcher page."""

    def __init__(self, application: DesktopLauncher):
        # pywebview recursively exposes public object attributes. Keep this
        # back-reference private so only the methods on LauncherApi become JS APIs.
        self._application = application

    def get_snapshot(self, since: int = 0) -> dict[str, Any]:
        return self._application.snapshot(int(since or 0))

    def start_service(self, key: str) -> dict[str, Any]:
        return self._application.action_result(
            f"启动 {key}", self._application.supervisor.start_service, key
        )

    def stop_service(self, key: str) -> dict[str, Any]:
        return self._application.action_result(
            f"停止 {key}", self._application.supervisor.stop_service, key
        )

    def restart_service(self, key: str) -> dict[str, Any]:
        return self._application.action_result(
            f"重启 {key}", self._application.supervisor.restart_service, key
        )

    def start_all(self) -> dict[str, Any]:
        return self._application.action_result(
            "启动全部服务", self._application.supervisor.start_all
        )

    def stop_all(self) -> dict[str, Any]:
        return self._application.action_result(
            "停止全部服务", self._application.supervisor.stop_all
        )

    def restart_all(self) -> dict[str, Any]:
        return self._application.action_result(
            "重启全部服务", self._application.supervisor.restart_all
        )

    def clear_logs(self) -> dict[str, Any]:
        self._application.supervisor.logs.clear()
        self._application.supervisor.logs.add("系统", "当前日志视图已清空")
        return {"ok": True}

    def set_launch_at_login(self, enabled: bool) -> dict[str, Any]:
        try:
            status = self._application.startup.set_enabled(bool(enabled))
            self._application.supervisor.logs.add(
                "系统",
                "已开启随 Windows 登录启动" if enabled else "已关闭随 Windows 登录启动",
                "success",
            )
            return {"ok": True, "startup": status}
        except Exception as exc:
            self._application.supervisor.logs.add(
                "系统", f"更新登录启动失败：{exc}", "error"
            )
            return {"ok": False, "error": str(exc)}

    def set_auto_confirm_wechat(self, enabled: bool) -> dict[str, Any]:
        try:
            preferences = self._application.preferences.set_auto_confirm_wechat(
                bool(enabled)
            )
            self._application.supervisor.logs.add(
                "系统",
                "已开启启动前自动确认微信登录"
                if enabled
                else "已关闭启动前自动确认微信登录",
                "success",
            )
            return {"ok": True, "auto_confirm_wechat": preferences.auto_confirm_wechat}
        except Exception as exc:
            self._application.supervisor.logs.add(
                "系统", f"保存启动设置失败：{exc}", "error"
            )
            return {"ok": False, "error": str(exc)}

    def open_web_console(self) -> dict[str, Any]:
        return self._application.open_url(self._application.web_console_url)

    def get_email_preferences(self) -> dict[str, Any]:
        from app.services.email_settings_service import open_settings_db, public_config
        with open_settings_db() as db:
            return public_config(db)

    def save_email_preferences(self, values: dict) -> dict[str, Any]:
        from pydantic import ValidationError
        from app.services.email_settings_service import open_settings_db, EmailPreferencesUpdate, save_config
        try:
            request = EmailPreferencesUpdate(**values)
        except ValidationError:
            raise ValueError("邮箱配置格式不正确，请检查邮箱服务和端口") from None
        with open_settings_db() as db:
            return save_config(db, request)

    def test_email_preferences(self) -> dict[str, Any]:
        from app.services.email_settings_service import open_settings_db, test_saved_config
        with open_settings_db() as db:
            return test_saved_config(db)

    def open_project_folder(self) -> dict[str, Any]:
        return self._application.open_path(PROJECT_ROOT)

    def run_compatibility_check(self) -> dict[str, Any]:
        return self._application.compatibility.start()

    def set_compatibility_baseline(self) -> dict[str, Any]:
        return self._application.compatibility.set_baseline()

    def open_compatibility_report(self, diff: bool = False) -> dict[str, Any]:
        try:
            path = self._application.compatibility.document(bool(diff))
            if os.name == 'nt':
                os.startfile(str(path))
            else:
                subprocess.Popen(['xdg-open', str(path)])
            return {'ok': True}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    def open_logs_folder(self) -> dict[str, Any]:
        return self._application.open_path(PROJECT_ROOT / "logs")

    def repair_environment(self) -> dict[str, Any]:
        return self._application.repair_environment()

    def minimize_window(self) -> dict[str, Any]:
        self._application.minimize_window()
        return {"ok": True}

    def toggle_maximize(self) -> dict[str, Any]:
        return {"ok": True, "maximized": self._application.toggle_maximize()}

    def begin_window_resize(self, edge: str) -> dict[str, Any]:
        return {"ok": self._application.begin_window_resize(str(edge))}

    def hide_window(self) -> dict[str, Any]:
        self._application.hide_window()
        return {"ok": True}

    def exit_application(self) -> dict[str, Any]:
        self._application.request_exit()
        return {"ok": True}

    def close_window(self, choice: str | None = None, remember: bool = False) -> dict[str, Any]:
        return self._application.request_close(choice, remember)

    def set_close_behavior(self, behavior: str) -> dict[str, Any]:
        try:
            self._application.preferences.set_close_behavior(behavior)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


class DesktopLauncher:
    def __init__(self, *, startup_mode: bool = False):
        self.startup_mode = startup_mode
        self.supervisor = ServiceSupervisor()
        self.compatibility = CompatibilityMonitor()
        self.preferences = PreferenceStore()
        self.startup = WindowsLoginStartup()
        self.api = LauncherApi(self)
        self.window: Any = None
        self.tray_icon: Any = None
        self._tray_available = False
        self._allow_close = False
        self._maximized = False
        self._workflow_started = False
        self._workflow_lock = threading.Lock()
        self._exiting = False
        self._signal_stop = threading.Event()
        self._repair_lock = threading.Lock()
        self._repairing = False
        self._environment_cache: tuple[float, list[dict[str, Any]]] | None = None
        self.started_at = time.time()

    @property
    def web_console_url(self) -> str:
        try:
            port = int(str(os.getenv("WEB_PORT") or "8888").strip())
        except ValueError:
            port = 8888
        return f"http://127.0.0.1:{port}/"

    def run(self) -> int:
        import webview

        if os.name == "nt":
            try:
                import ctypes

                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "Maaboubou.Mabobot.Desktop"
                )
            except Exception:
                pass
        self.supervisor.start_monitoring()
        try:
            migration = self.startup.migrate_legacy()
            if migration.get("migrated"):
                self.supervisor.logs.add(
                    "系统",
                    "旧版微信自动登录启动项已迁移到统一桌面启动器",
                    "success",
                )
        except Exception as exc:
            self.supervisor.logs.add(
                "系统", f"迁移旧版登录启动项失败：{exc}", "warning"
            )
        self.supervisor.logs.add(
            "系统", f"Mabobot {APP_VERSION} 桌面启动器已就绪", "success"
        )
        write_launcher_state()
        self._start_signal_monitor()

        self.window = webview.create_window(
            f"Mabobot {APP_VERSION}",
            str(UI_DIR / "index.html"),
            js_api=self.api,
            width=1180,
            height=780,
            min_size=(980, 700),
            resizable=True,
            frameless=True,
            easy_drag=False,
            shadow=True,
            background_color="#faf9f5",
            hidden=self.startup_mode,
        )
        self.window.events.loaded += self._on_loaded
        self.window.events.closing += self._on_closing
        self._start_tray()
        try:
            webview.start(
                func=self._startup_fallback,
                gui="edgechromium",
                debug=False,
                icon=str(APP_ICON_FILE) if APP_ICON_FILE.is_file() else None,
            )
            return 0
        finally:
            self._signal_stop.set()
            if not self._exiting:
                self.supervisor.shutdown()
            self._stop_tray()
            clear_launcher_state()

    def _on_loaded(self) -> None:
        self._begin_startup_workflow()

    def _startup_fallback(self) -> None:
        window = self.window
        loaded = bool(
            window
            and window.events.loaded.wait(timeout=_UI_LOAD_TIMEOUT_SECONDS)
        )
        if not loaded:
            self.supervisor.logs.add(
                "系统",
                "界面加载回调超时，后台启动流程已继续",
                "warning",
            )
        self._begin_startup_workflow()

    def _begin_startup_workflow(self) -> None:
        with self._workflow_lock:
            if self._workflow_started:
                return
            self._workflow_started = True
        threading.Thread(
            target=self._startup_workflow,
            name="mabobot-startup-workflow",
            daemon=True,
        ).start()

    def _startup_workflow(self) -> None:
        try:
            preferences = self.preferences.load()
            if self.startup_mode and preferences.auto_confirm_wechat:
                self.supervisor.logs.add("开机流程", "正在等待桌面与微信登录状态")
                if not self._run_wechat_login_assistant():
                    self.supervisor.logs.add(
                        "开机流程",
                        "微信自动确认未完成，服务未启动，请在登录微信后手动启动",
                        "warning",
                    )
                    self.show_window()
                    return
            self.supervisor.start_all()
        except Exception as exc:
            self.supervisor.logs.add(
                "系统", f"自动启动服务失败：{exc}", "error"
            )
            self.show_window()

    def _run_wechat_login_assistant(self) -> bool:
        script = PROJECT_ROOT / "wechat_auto_login.py"
        if not script.is_file():
            self.supervisor.logs.add("开机流程", "缺少 wechat_auto_login.py", "error")
            return False
        flags = (
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
        )
        try:
            process = subprocess.Popen(
                [sys.executable, "-u", str(script), "--no-start"],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
            )
        except OSError as exc:
            self.supervisor.logs.add(
                "开机流程", f"微信登录辅助启动失败：{exc}", "error"
            )
            return False
        if process.stdout:
            for line in process.stdout:
                message = line.rstrip("\r\n")
                if message:
                    self.supervisor.ingest_external_log("微信登录", message)
        return process.wait() == 0

    def _start_signal_monitor(self) -> None:
        threading.Thread(
            target=self._signal_loop,
            name="mabobot-launcher-signals",
            daemon=True,
        ).start()

    def _signal_loop(self) -> None:
        while not self._signal_stop.wait(0.25):
            if SHOW_SIGNAL_FILE.exists():
                try:
                    SHOW_SIGNAL_FILE.unlink(missing_ok=True)
                    self.show_window()
                except OSError as exc:
                    self.supervisor.logs.add("系统", f"恢复窗口失败：{exc}", "warning")
            if not CONTROL_SIGNAL_FILE.exists():
                continue
            try:
                action = consume_control_signal(CONTROL_SIGNAL_FILE)
                if action == "web":
                    self.supervisor.logs.add("Web 控制台", "收到 Web 服务重启请求")
                    self.supervisor.restart_service("web")
                elif action == "start-bot":
                    self.supervisor.logs.add("Web 控制台", "收到微信 Bot 启动请求")
                    self.supervisor.start_service("bot")
                elif action == "stop-bot":
                    self.supervisor.logs.add("Web 控制台", "收到微信 Bot 停止请求")
                    self.supervisor.stop_service("bot")
                else:
                    self.supervisor.logs.add("Web 控制台", "收到全部服务重启请求")
                    self.supervisor.restart_all()
            except Exception as exc:
                self.supervisor.logs.add(
                    "Web 控制台", f"处理控制请求失败：{exc}", "error"
                )

    def action_result(self, label: str, action: Any, *args: Any) -> dict[str, Any]:
        try:
            result = action(*args)
            return {"ok": True, "message": f"{label}请求已执行", "result": result}
        except Exception as exc:
            self.supervisor.logs.add("系统", f"{label}失败：{exc}", "error")
            return {"ok": False, "error": str(exc)}

    def snapshot(self, since: int = 0) -> dict[str, Any]:
        services = self.supervisor.snapshot()
        logs, last_sequence = self.supervisor.logs.snapshot(since)
        running = sum(item["status"] == "running" for item in services)
        problem = any(item["status"] in {"degraded", "error"} for item in services)
        starting = any(item["status"] in {"starting", "stopping"} for item in services)
        if problem:
            overall = {"status": "warning", "label": "系统需要关注"}
        elif running == len(services) and services:
            overall = {"status": "running", "label": "系统运行正常"}
        elif starting:
            overall = {"status": "starting", "label": "服务状态切换中"}
        else:
            overall = {"status": "stopped", "label": "服务尚未全部启动"}

        startup_status = self.startup.status()
        preferences = self.preferences.load()
        return {
            "version": APP_VERSION,
            "launcher_started_at": self.started_at,
            "uptime_seconds": max(0, int(time.time() - self.started_at)),
            "overall": overall,
            "services": services,
            "logs": logs,
            "last_sequence": last_sequence,
            "settings": {
                "launch_at_login": bool(startup_status.get("enabled")),
                "startup": startup_status,
                "auto_confirm_wechat": preferences.auto_confirm_wechat,
                "close_behavior": preferences.close_behavior,
                "tray_available": self._tray_available,
            },
            "environment": self._environment_status(),
            "compatibility": self.compatibility.snapshot(),
            "repairing": self._repairing,
            "startup_mode": self.startup_mode,
        }

    def _environment_status(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._environment_cache and now - self._environment_cache[0] < 5:
            return self._environment_cache[1]

        version_ready = sys.version_info[:2] in {(3, 11), (3, 12)}
        requirements_ready = self._requirements_ready()
        wechat_running = self._process_running({"wechat.exe", "weixin.exe"})
        codex_ready = bool(shutil.which("codex") or shutil.which("codex.exe"))
        checks = [
            {
                "key": "python",
                "label": f"Python {sys.version_info.major}.{sys.version_info.minor}",
                "ready": version_ready,
                "detail": "已就绪" if version_ready else "需要 3.11 或 3.12",
            },
            {
                "key": "dependencies",
                "label": "项目依赖",
                "ready": requirements_ready,
                "detail": "已就绪" if requirements_ready else "需要修复",
            },
            {
                "key": "wechat",
                "label": "微信客户端",
                "ready": wechat_running,
                "detail": "运行中" if wechat_running else "未检测到",
            },
            {
                "key": "codex",
                "label": "Codex",
                "ready": codex_ready,
                "optional": True,
                "detail": "已就绪" if codex_ready else "可选 · 未检测到",
            },
        ]
        self._environment_cache = (now, checks)
        return checks

    @staticmethod
    def _process_running(names: set[str]) -> bool:
        for process in psutil.process_iter(["name"]):
            try:
                if str(process.info.get("name") or "").casefold() in names:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    @staticmethod
    def _requirements_ready() -> bool:
        requirements = PROJECT_ROOT / "requirements.txt"
        marker = PROJECT_ROOT / ".venv" / ".requirements.sha256"
        try:
            current = hashlib.sha256(requirements.read_bytes()).hexdigest().upper()
            saved = marker.read_text(encoding="ascii").strip().upper()
            return current == saved
        except OSError:
            return False

    def repair_environment(self) -> dict[str, Any]:
        if not self._repair_lock.acquire(blocking=False):
            return {"ok": False, "error": "环境修复已经在进行中"}
        self._repairing = True
        threading.Thread(
            target=self._repair_environment_worker,
            name="mabobot-environment-repair",
            daemon=True,
        ).start()
        return {"ok": True, "message": "环境修复已开始"}

    def _repair_environment_worker(self) -> None:
        try:
            self.supervisor.logs.add("环境", "正在停止服务并修复运行环境", "warning")
            self.supervisor.stop_all()
            if os.name != "nt" or not INSTALL_SCRIPT.is_file():
                raise RuntimeError("找不到 Windows 环境安装脚本")
            process = subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(INSTALL_SCRIPT),
                    "-Force",
                    "-InstallMissingRuntimes",
                ],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
            if process.stdout:
                for line in process.stdout:
                    message = line.rstrip("\r\n")
                    if message:
                        self.supervisor.ingest_external_log("环境", message)
            exit_code = process.wait()
            if exit_code != 0:
                raise RuntimeError(f"安装脚本退出代码 {exit_code}")
            self._environment_cache = None
            self.supervisor.logs.add(
                "环境", "运行环境修复完成，正在恢复服务", "success"
            )
            self.supervisor.start_all()
        except Exception as exc:
            self.supervisor.logs.add("环境", f"运行环境修复失败：{exc}", "error")
            self.show_window()
        finally:
            self._repairing = False
            self._repair_lock.release()

    @staticmethod
    def open_url(url: str) -> dict[str, Any]:
        try:
            webbrowser.open(url)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def open_path(path: Path) -> dict[str, Any]:
        try:
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(path)])
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def minimize_window(self) -> None:
        if self.window:
            self.window.minimize()

    def toggle_maximize(self) -> bool:
        if not self.window:
            return False
        if self._maximized:
            self.window.restore()
        else:
            self.window.maximize()
        self._maximized = not self._maximized
        return self._maximized

    def begin_window_resize(self, edge: str) -> bool:
        """Start a native Windows resize gesture for the frameless window."""
        if os.name != "nt" or not self.window or self._maximized:
            return False

        hit_tests = {
            "n": 12,
            "ne": 14,
            "e": 11,
            "se": 17,
            "s": 15,
            "sw": 16,
            "w": 10,
            "nw": 13,
        }
        hit_test = hit_tests.get(edge.lower())
        native = getattr(self.window, "native", None)
        if hit_test is None or native is None:
            return False

        try:
            import ctypes

            handle = int(native.Handle.ToInt64())
            user32 = ctypes.windll.user32
            user32.ReleaseCapture()
            user32.SendMessageW(
                ctypes.c_void_p(handle),
                ctypes.c_uint(0x00A1),
                ctypes.c_size_t(hit_test),
                ctypes.c_ssize_t(0),
            )
            return True
        except Exception as exc:
            self.supervisor.logs.add(
                "系统", f"调整启动器窗口大小失败：{exc}", "warning"
            )
            return False

    def show_window(self) -> None:
        if self.window:
            self.window.show()
            self.window.restore()
            self._maximized = False

    def hide_window(self) -> None:
        if not self.window:
            return
        if self._tray_available:
            self.window.hide()
            try:
                self.tray_icon.notify("Mabobot 仍在后台运行", "Mabobot")
            except Exception:
                pass
        else:
            self.window.minimize()

    def _on_closing(self) -> bool | None:
        if self._allow_close:
            return None
        def close_requested():
            result = self.request_close()
            if result.get("needs_choice") and self.window:
                try:
                    self.window.evaluate_js("window.showLauncherCloseDialog()")
                except Exception as exc:
                    self.supervisor.logs.add("系统", f"无法显示关闭选项，请在设置中选择关闭方式：{exc}", "warning")

        threading.Thread(target=close_requested, name="mabobot-close-window", daemon=True).start()
        return False

    def request_close(self, choice: str | None = None, remember: bool = False) -> dict[str, Any]:
        if self._exiting:
            return {"ok": True}
        behavior = self.preferences.load().close_behavior if choice is None else choice
        if behavior == "ask" and choice is None:
            return {"ok": True, "needs_choice": True}
        if behavior not in ("tray", "exit"):
            return {"ok": False, "error": "请选择有效的窗口关闭方式"}
        if remember:
            try:
                self.preferences.set_close_behavior(behavior)
            except Exception as exc:
                return {"ok": False, "error": f"保存关闭设置失败：{exc}"}
        if behavior == "exit":
            self.request_exit()
        else:
            self.hide_window()
        return {"ok": True}

    def request_exit(self) -> None:
        if self._exiting:
            return
        self._exiting = True
        threading.Thread(
            target=self._exit_worker, name="mabobot-exit", daemon=True
        ).start()

    def _exit_worker(self) -> None:
        self.supervisor.logs.add("系统", "正在停止服务并退出 Mabobot")
        self.supervisor.shutdown()
        self._allow_close = True
        self._signal_stop.set()
        self._stop_tray()
        clear_launcher_state()
        if self.window:
            self.window.destroy()

    def _start_tray(self) -> None:
        if os.name != "nt":
            return
        try:
            import pystray
            from PIL import Image, ImageDraw

            if APP_ICON_IMAGE.is_file():
                with Image.open(APP_ICON_IMAGE) as source:
                    image = source.convert("RGBA").resize(
                        (64, 64), Image.Resampling.LANCZOS
                    )
            else:
                image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                draw = ImageDraw.Draw(image)
                draw.rounded_rectangle((5, 5, 59, 59), radius=15, fill="#cc785c")
                draw.line(
                    (18, 44, 18, 22, 32, 38, 46, 22, 46, 44),
                    fill="#fffaf6",
                    width=5,
                )
            menu = pystray.Menu(
                pystray.MenuItem(
                    "打开 Mabobot", lambda *_: self.show_window(), default=True
                ),
                pystray.MenuItem(
                    "打开 Web 控制台", lambda *_: self.open_url(self.web_console_url)
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出并停止服务", lambda *_: self.request_exit()),
            )
            self.tray_icon = pystray.Icon("Mabobot", image, "Mabobot", menu)
            threading.Thread(
                target=self.tray_icon.run, name="mabobot-tray", daemon=True
            ).start()
            self._tray_available = True
        except Exception as exc:
            self._tray_available = False
            self.supervisor.logs.add(
                "系统", f"系统托盘不可用，将改为最小化窗口：{exc}", "warning"
            )

    def _stop_tray(self) -> None:
        icon = self.tray_icon
        self.tray_icon = None
        self._tray_available = False
        if icon:
            try:
                icon.stop()
            except Exception:
                pass
