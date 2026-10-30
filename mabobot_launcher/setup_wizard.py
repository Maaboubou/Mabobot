"""Resumable desktop setup. Probes and installers never run on the UI thread."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from .constants import PROJECT_ROOT

STEPS = ("welcome", "codex", "components", "finish")
ACTIONS = {"wsl", "wsl_terminal", "codex", "browser", "media", "file_tools"}
LABELS = {"wsl": "安装 WSL 与 Ubuntu", "wsl_terminal": "初始化 Linux 用户",
          "codex": "准备 Codex", "browser": "安装浏览器", "media": "安装媒体组件",
          "file_tools": "安装文件处理工具"}


def _run(argv, *, timeout=30):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)


def _decode(value: bytes) -> str:
    return value.decode("utf-16" if value.startswith((b"\xff\xfe", b"\xfe\xff")) else
                        "utf-16-le" if b"\x00" in value else "utf-8", errors="replace").strip()


def probe_wsl() -> dict:
    if os.name != "nt":
        return {"state": "native", "detail": "当前为本机 Linux 环境"}
    try:
        listing = _run(["wsl.exe", "--list", "--quiet"], timeout=15)
        names = _decode(listing.stdout)
        if listing.returncode or not names:
            return {"state": "missing", "detail": "尚未安装 WSL 或 Linux 发行版"}
        result = _run(["wsl.exe", "--exec", "sh", "-c",
                       'printf "%s\\n" "$WSL_DISTRO_NAME" "$(id -u)" "$(uname -r)" "$(command -v python3 || echo missing)"'], timeout=20)
        lines = _decode(result.stdout).splitlines()
        if result.returncode or len(lines) < 4:
            return {"state": "initialize", "detail": "请完成 Linux 首次初始化；若刚安装 WSL，请先重启电脑"}
        distro, uid, kernel, python = lines[-4:]
        if "microsoft-standard" not in kernel.lower() and "wsl2" not in kernel.lower():
            return {"state": "error", "detail": "Codex 需要 WSL 2，请按安装帮助将当前发行版转换为 WSL 2"}
        if uid == "0":
            return {"state": "initialize", "detail": f"{distro} 尚在使用 root，请完成普通 Linux 用户初始化"}
        return {"state": "ready", "detail": f"{distro} · WSL 2", "python": python != "missing"}
    except (OSError, subprocess.SubprocessError):
        return {"state": "error", "detail": "WSL 未能响应；刚安装后请重启电脑，否则打开安装帮助排查"}


def probe_setup() -> dict:
    from app.services.file_tools_runtime import _default_use_wsl, get_file_tools_runtime
    from app.utils.browser_executable import find_chrome_executable
    use_wsl = _default_use_wsl()
    wsl = probe_wsl() if use_wsl else {"state": "native", "detail": "使用已配置的本机 Codex"}
    result = {"wsl": wsl, "use_wsl": use_wsl, "codex": {"ready": False, "detail": "等待运行环境"},
              "profile": {"ready": False, "detail": "待配置模型"}, "file_tools": {"ready": False, "detail": "待安装"}}
    # Never start a distribution before its user has finished the interactive first launch.
    if wsl["state"] in {"ready", "native"}:
        runtime = get_file_tools_runtime(force=True)
        codex = runtime.get("codex") or {}
        ready = bool(codex.get("available")) and not runtime.get("stale")
        result["codex"] = {"ready": ready, "detail": "CLI 已找到" if ready else "尚未找到可用的 Codex CLI"}
        if ready:
            try:
                from app.services.codex_profile_service import CodexProfileService, DEFAULT_ASSISTANT_PROFILE_KEY
                from app.models.base import SessionLocal
                from app.models.setting import Setting

                # A diagnostic must not migrate the default or use a different process's cached setting.
                listing = CodexProfileService()._run("list")
                with SessionLocal() as db:
                    setting = db.query(Setting).filter(Setting.key == DEFAULT_ASSISTANT_PROFILE_KEY).first()
                    default_id = str(setting.get_value() or "") if setting else ""
                profile = next((p for p in listing.get("profiles", [])
                                if p.get("name") == default_id and p.get("available")), None)
                configured = bool(profile) and not listing.get("_mabobot_stale_reason")
                result["profile"] = {"ready": configured,
                                     "detail": "默认模型与凭据已配置（尚未发起模型请求）" if configured else "请创建 Profile 并设为默认"}
            except Exception:
                result["profile"] = {"ready": False, "detail": "暂无法读取模型配置，请打开 Codex 页面检查"}
        commands = set(runtime.get("command_names") or [])
        try:
            python_command = ["wsl.exe", "python3"] if use_wsl else [sys.executable]
            check = _run([*python_command, "-c", "import docx, openpyxl, pptx, fitz"], timeout=15)
            files_ready = check.returncode == 0 and {"pandoc", "pdftotext", "tesseract"} <= commands
        except (OSError, subprocess.SubprocessError):
            files_ready = False
        result["file_tools"] = {"ready": files_ready, "detail": "常用文档与 OCR 工具已找到" if files_ready else "文档／OCR 工具未完整安装"}
    try:
        from app.services.shared_chrome import load_shared_chrome_settings
        executable = load_shared_chrome_settings().executable
    except Exception:
        executable = find_chrome_executable()
    browser_ready = Path(executable).is_file()
    # Verify Playwright independently: shared Chrome alone does not provide PDF/mindmap rendering.
    try:
        chromium = _run([sys.executable, "-c", "from pathlib import Path; from playwright.sync_api import sync_playwright; "
                         "p=sync_playwright().start(); path=p.chromium.executable_path; p.stop(); "
                         "raise SystemExit(0 if Path(path).is_file() else 1)"], timeout=20)
        browser_ready = browser_ready and chromium.returncode == 0
    except (OSError, subprocess.SubprocessError):
        browser_ready = False
    result["browser"] = {"ready": browser_ready, "detail": "浏览器已就绪" if browser_ready else
                         "浏览器未完整准备；若已手动指定浏览器，请检查链接摘要中的浏览器路径"}
    try:
        # Do not fetch binaries while merely checking status.
        from static_ffmpeg.run import get_platform_dir
        root = Path(get_platform_dir())
        suffix = ".exe" if os.name == "nt" else ""
        media_ready = all((root / (name + suffix)).is_file() for name in ("ffmpeg", "ffprobe"))
        if media_ready:
            media_ready = all(_run([str(root / (name + suffix)), "-version"], timeout=10).returncode == 0
                              for name in ("ffmpeg", "ffprobe"))
    except (ImportError, OSError, subprocess.SubprocessError):
        media_ready = False
    result["media"] = {"ready": media_ready, "detail": "FFmpeg / FFprobe 已找到" if media_ready else "媒体组件待安装"}
    return result


def installation_command(action: str) -> tuple[list[str], bool]:
    """Only fixed, allowlisted commands; never interpolate browser-supplied command text."""
    if action not in ACTIONS:
        raise ValueError("未知安装项目")
    if action == "browser":
        return [sys.executable, "-m", "playwright", "install", "chromium"], False
    if action == "media":
        return [sys.executable, "-c", "import subprocess; from static_ffmpeg import run; "
                "paths=run.get_or_fetch_platform_executables_else_raise(); "
                "[subprocess.run([p, '-version'], check=True) for p in paths]"], False
    if os.name != "nt":
        raise ValueError("此安装入口面向 Windows；请使用现有 Linux 环境管理工具")
    if action == "wsl":
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(PROJECT_ROOT / "scripts/launcher/prepare_wsl.ps1")], True
    if action == "wsl_terminal":
        return ["wsl.exe"], True
    from app.services.file_tools_runtime import _default_use_wsl, _as_wsl_path
    if not _default_use_wsl():
        raise ValueError("当前配置为本机 Codex；请按安装帮助准备本机 CLI，或恢复 WSL 配置后重试")
    if probe_wsl()["state"] != "ready":
        raise ValueError("请先完成 WSL 安装与 Linux 用户初始化，再安装组件")
    script = _as_wsl_path(PROJECT_ROOT / "scripts/launcher/prepare_linux.sh")
    return ["wsl.exe", "--exec", "bash", script, "codex" if action == "codex" else "file-tools"], True


class SetupWizard:
    def __init__(self, root: Path = PROJECT_ROOT, *, probe=probe_setup):
        self.root = Path(root)
        self.path = self.root / "data/launcher_setup.json"
        self.log_path = self.root / "logs/launcher_setup.log"
        self._lock = threading.RLock()
        self._probe = probe
        self._checks: dict = {}
        self._checking = False
        self._checked_at = 0.0
        self._thread = None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        self._state = {"status": data.get("status") if data.get("status") in {"active", "skipped", "completed"} else "active",
                       "step": data.get("step") if data.get("step") in STEPS else "welcome"}
        self._operation = data.get("operation") if isinstance(data.get("operation"), dict) else {}
        self._recover_operation()

    def _recover_operation(self):
        if self._operation.get("status") != "running":
            return
        if self._process_alive():
            self._operation["message"] = "上次启动的安装仍在运行，请在安装窗口完成后重新检测"
        else:
            self._operation.update(status="interrupted", message="上次安装未确认完成。已保留安装内容，请重新检测后重试。")

    def _process_alive(self):
        try:
            import psutil
            process = psutil.Process(int(self._operation.get("pid", 0)))
            return abs(process.create_time() - float(self._operation.get("created_at", 0))) < 1 and process.is_running()
        except (ValueError, TypeError, psutil.Error):
            return False

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({**self._state, "operation": self._operation}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    def update(self, status: str = "active", step: str | None = None) -> dict:
        if status not in {"active", "skipped", "completed"} or (step is not None and step not in STEPS):
            raise ValueError("无效的向导进度")
        with self._lock:
            self._state["status"] = status
            if step is not None:
                self._state["step"] = step
            self._save()
        return {"ok": True}

    def snapshot(self):
        with self._lock:
            if self._operation.get("status") == "running" and not self._thread:
                self._recover_operation()
            operation = deepcopy(self._operation)
            if operation.get("status") == "running":
                operation["elapsed_seconds"] = max(0, int(time.time() - operation.get("started_at", time.time())))
            if operation.get("status") and not operation.get("console"):
                try:
                    with self.log_path.open("rb") as log:
                        size = log.seek(0, 2)
                        log.seek(max(int(operation.get("log_offset", size)), size - 3000))
                        tail = log.read().decode("utf-8", errors="replace")
                    operation["log_tail"] = re.sub(r"(https?://)[^/\s@]+@", r"\1***@", tail)
                except (OSError, ValueError):
                    pass
            return {**self._state, "checks": deepcopy(self._checks), "checking": self._checking,
                    "operation": operation}

    def refresh(self):
        with self._lock:
            if self._checking or self._operation.get("status") == "running":
                return {"ok": True}
            self._checking = True
        threading.Thread(target=self._probe_worker, name="setup-probe", daemon=True).start()
        return {"ok": True}

    def _probe_worker(self):
        try:
            checks = self._probe()
        except Exception:
            checks = {"error": "检测未完成，请重试；详细安装记录可在日志目录查看。"}
        with self._lock:
            self._checks = checks
            self._checked_at = time.monotonic()
            self._checking = False
            action = self._operation.get("action")
            if self._operation.get("status") == "finished" and action in {"codex", "browser", "media", "file_tools"}:
                check = checks.get(action)
                if check is not None:
                    self._operation.update(
                        status="succeeded" if check.get("ready") else "error",
                        message="组件已安装并通过检测。" if check.get("ready") else
                        "安装命令已结束，但检测仍未通过。" + str(check.get("detail") or "请检查日志后重试。"),
                    )
                else:
                    self._operation["message"] = "安装步骤已结束，环境检测未完成，请重新检测。"
                try:
                    self._save()
                except OSError:
                    pass

    def install(self, action: str):
        if action not in ACTIONS:
            return {"ok": False, "error": "未知安装项目"}
        with self._lock:
            if self._operation.get("status") == "running" or self._checking:
                return {"ok": False, "error": "正在检测或安装，请等待当前步骤结束"}
            self._operation = {"action": action, "status": "running", "message": LABELS[action] + "…",
                               "started_at": time.time()}
            try:
                self._save()
            except OSError:
                self._operation = {}
                return {"ok": False, "error": "无法保存安装进度，请检查项目目录写入权限和磁盘空间"}
            self._thread = threading.Thread(target=self._install_worker, args=(action,), name="setup-install", daemon=True)
            self._thread.start()
        return {"ok": True}

    def _install_worker(self, action: str):
        try:
            command, console = installation_command(action)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            flags = (getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if console else getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
            with self.log_path.open("ab") as log:
                with self._lock:
                    self._operation.update(console=console, log_offset=log.tell())
                    if console:
                        self._operation["message"] = LABELS[action] + "：请在单独打开的安装窗口中完成提示，再回到这里。"
                log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {LABELS[action]}\n".encode())
                log.flush()
                process = subprocess.Popen(command, cwd=self.root, stdin=None if console else subprocess.DEVNULL,
                                           stdout=None if console else log, stderr=None if console else subprocess.STDOUT,
                                           creationflags=flags)
                with self._lock:
                    import psutil
                    try:
                        created_at = psutil.Process(process.pid).create_time()
                    except psutil.NoSuchProcess:
                        created_at = 0  # A quick installer may have exited before this observation.
                    self._operation.update(pid=process.pid, created_at=created_at)
                    self._save()
                # Interactive sudo/user creation must stay in the visible terminal, not the WebView.
                code = process.wait()
            if code:
                raise RuntimeError(f"安装未完成（退出码 {code}）。请检查网络和安装窗口／日志，再重试。")
            message = "安装步骤已结束，正在检查实际结果。"
            if action == "wsl":
                message = "WSL 安装步骤已结束。如提示重启，请保存工作并重启电脑，再打开 START.bat 继续。"
            elif action == "wsl_terminal":
                message = "Linux 窗口已关闭，正在重新检查。"
            with self._lock:
                self._operation.update(status="finished", message=message)
        except Exception as exc:
            with self._lock:
                self._operation.update(status="error", message=str(exc))
        finally:
            with self._lock:
                self._thread = None
                self._save()
            self.refresh()
