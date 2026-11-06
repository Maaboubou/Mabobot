"""Unified, source-aware management for Mabobot environment tools."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.services.runtime_operations import OperationContext, get_runtime_operation_service
from app.utils.subprocess_utils import hidden_process_kwargs


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_FFMPEG_VERSION = "3.0"
_ACTIVE_OPERATION_STATUSES = {"queued", "running", "cancelling"}


class SystemToolError(RuntimeError):
    """A safe, operator-facing tool management error."""


def _package_version(distribution: str) -> str:
    try:
        return package_version(distribution)
    except PackageNotFoundError:
        return ""


def _command_error(result: subprocess.CompletedProcess) -> str:
    raw = str(result.stderr or result.stdout or "").strip()
    return raw[-1500:] or f"进程退出码 {result.returncode}"


class SystemToolService:
    """Aggregate existing updaters and manage non-package runtime artifacts."""

    def __init__(
        self,
        *,
        python_executable: Optional[str] = None,
        project_root: Optional[Path] = None,
        runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    ) -> None:
        self.python_executable = str(python_executable or sys.executable)
        self.project_root = Path(project_root or PROJECT_ROOT)
        self._runner = runner or subprocess.run
        self._cache_lock = threading.RLock()
        self._environment_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}

    def _run(self, args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
        return self._runner(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
            **hidden_process_kwargs(),
        )

    @staticmethod
    def _base_card(
        tool_id: str,
        title: str,
        description: str,
        *,
        category: str,
        icon: str,
    ) -> Dict[str, Any]:
        return {
            "id": tool_id,
            "title": title,
            "description": description,
            "category": category,
            "icon": icon,
            "health": "unknown",
            "health_label": "未检测",
            "message": "尚未读取工具状态",
            "installed_version": None,
            "runtime_version": None,
            "available_version": None,
            "update_available": False,
            "checked_at": None,
            "restart_required": False,
            "source_label": "未识别",
            "path": None,
            "managed": False,
            "operation": None,
            "operation_running": False,
            "actions": {
                "check": False,
                "update": False,
                "rollback": False,
                "repair": False,
                "restart": False,
            },
            "details": {},
        }

    @staticmethod
    def _failed_card(card: Dict[str, Any], exc: Exception) -> Dict[str, Any]:
        card.update(
            health="unavailable",
            health_label="不可用",
            message=str(exc) or type(exc).__name__,
        )
        return card

    def _codex_card(self) -> Dict[str, Any]:
        card = self._base_card(
            "codex",
            "Codex",
            "本地 Codex CLI 与 App Server 运行时",
            category="upgradable",
            icon="bi-terminal",
        )
        try:
            from app.services.codex_upgrade_service import get_codex_upgrade_service

            status = get_codex_upgrade_service().status()
            installation = status.get("installation") or {}
            installed = str(status.get("installed_version") or "")
            operation = status.get("operation")
            card.update(
                health="ready" if installed else "missing",
                health_label="正常" if installed else "未安装",
                message=(
                    "新版会先通过协议和双进程池验证，再接管运行时"
                    if installed
                    else "没有检测到可用的 Codex CLI"
                ),
                installed_version=installed or None,
                available_version=status.get("available_version"),
                update_available=bool(status.get("update_available")),
                checked_at=status.get("checked_at"),
                check_state=status.get("check_state", "idle"),
                check_error=status.get("check_error"),
                last_attempt_at=status.get("last_attempt_at"),
                last_success_at=status.get("last_success_at"),
                last_error_at=status.get("last_error_at"),
                next_check_at=status.get("next_check_at"),
                check_schedule_timezone=status.get("check_schedule_timezone", "Asia/Shanghai"),
                check_schedule_time=status.get("check_schedule_time", "08:00"),
                source_label=installation.get("method_label") or "未识别安装方式",
                path=installation.get("executable"),
                managed=bool(installed),
                operation=operation,
                operation_running=bool(status.get("operation_running")),
                actions={
                    "check": bool(installed),
                    "update": bool(installed),
                    "rollback": bool(status.get("rollback_available")),
                    "repair": False,
                    "restart": False,
                },
                details={
                    "realpath": installation.get("realpath"),
                    "install_method": installation.get("method"),
                    "rollback_version": status.get("rollback_version"),
                },
            )
            return card
        except Exception as exc:
            return self._failed_card(card, exc)

    def _litellm_card(self) -> Dict[str, Any]:
        card = self._base_card(
            "litellm",
            "LiteLLM",
            "模型供应商适配与统一调用层",
            category="upgradable",
            icon="bi-diagram-3",
        )
        try:
            from app.services.litellm_update_service import get_litellm_update_service

            status = get_litellm_update_service().status()
            installed = str(status.get("installed_version") or "")
            restart_required = bool(status.get("restart_required"))
            card.update(
                health="degraded" if restart_required else ("ready" if installed else "missing"),
                health_label="等待重启" if restart_required else ("正常" if installed else "未安装"),
                message=(
                    "磁盘版本已更新，重启全部服务后由运行进程加载"
                    if restart_required
                    else "失败时会自动恢复更新前版本"
                ),
                installed_version=installed or None,
                runtime_version=status.get("runtime_version"),
                available_version=status.get("available_version"),
                update_available=bool(status.get("update_available")),
                checked_at=status.get("checked_at"),
                restart_required=restart_required,
                source_label=status.get("environment_label") or "当前 Python 环境",
                path=self.python_executable,
                managed=bool(installed),
                operation=status.get("operation"),
                operation_running=bool(status.get("operation_running")),
                actions={
                    "check": bool(installed) and not restart_required,
                    "update": bool(installed) and not restart_required,
                    "rollback": False,
                    "repair": False,
                    "restart": restart_required,
                },
            )
            return card
        except Exception as exc:
            return self._failed_card(card, exc)

    def _ytdlp_card(self) -> Dict[str, Any]:
        card = self._base_card(
            "yt-dlp",
            "yt-dlp",
            "视频站点解析、字幕探测与媒体下载",
            category="upgradable",
            icon="bi-cloud-arrow-down",
        )
        try:
            from app.services.ytdlp_update_service import get_ytdlp_update_service

            status = get_ytdlp_update_service().status()
            installed = str(status.get("installed_version") or "")
            executable = status.get("executable")
            healthy = bool(installed and executable)
            card.update(
                health="ready" if healthy else "missing",
                health_label="正常" if healthy else "模块缺失",
                message=(
                    "通过当前 Python 模块调用，升级后独立校验"
                    if healthy
                    else "当前 Python 环境中的 yt-dlp 模块不可用"
                ),
                installed_version=installed or None,
                available_version=status.get("available_version"),
                update_available=bool(status.get("update_available")),
                checked_at=status.get("checked_at"),
                source_label=status.get("environment_label") or "当前 Python 环境",
                path=executable,
                managed=bool(installed),
                operation=status.get("operation"),
                operation_running=bool(status.get("operation_running")),
                actions={
                    "check": bool(installed),
                    "update": bool(installed),
                    "rollback": False,
                    "repair": False,
                    "restart": False,
                },
                details={"used_by": ["Summary Plus"]},
            )
            return card
        except Exception as exc:
            return self._failed_card(card, exc)

    @staticmethod
    def _configured_media_values() -> Dict[str, str]:
        try:
            from app.utils.plugin_config import get_config

            return {
                key: str(get_config(key, plugin_name="summary_plus", default="") or "").strip()
                for key in ("ffmpeg_path", "ffprobe_path", "ffmpeg_dir")
            }
        except Exception:
            return {"ffmpeg_path": "", "ffprobe_path": "", "ffmpeg_dir": ""}

    def _static_ffmpeg_paths(self) -> Dict[str, str]:
        try:
            from static_ffmpeg import run as static_ffmpeg_run

            directory = Path(static_ffmpeg_run.get_platform_dir())
            suffix = ".exe" if os.name == "nt" else ""
            return {
                "ffmpeg": str(directory / f"ffmpeg{suffix}"),
                "ffprobe": str(directory / f"ffprobe{suffix}"),
            }
        except Exception:
            return {}

    def _media_candidates(self, tool_name: str) -> tuple[list[Dict[str, str]], list[str]]:
        configured = self._configured_media_values()
        suffix = ".exe" if os.name == "nt" else ""
        candidates: list[Dict[str, str]] = []
        issues: list[str] = []

        def add(path: str | Path, source: str, label: str) -> None:
            raw = str(path or "").strip()
            if raw:
                candidates.append({"path": raw, "source": source, "source_label": label})

        configured_path = configured.get(f"{tool_name}_path", "")
        if configured_path:
            add(configured_path, "configured", "Summary Plus 指定路径")
            if not Path(configured_path).is_file():
                issues.append(f"配置的 {tool_name} 路径不存在")

        environment_path = str(os.getenv(f"{tool_name.upper()}_PATH") or "").strip()
        if environment_path:
            add(environment_path, "environment", "环境变量")
            if not Path(environment_path).is_file():
                issues.append(f"{tool_name.upper()}_PATH 指向的文件不存在")

        ffmpeg_dir = configured.get("ffmpeg_dir", "")
        if ffmpeg_dir:
            add(Path(ffmpeg_dir) / f"{tool_name}{suffix}", "configured", "Summary Plus 媒体目录")

        add(
            self.project_root / "tools" / "ffmpeg" / "bin" / f"{tool_name}{suffix}",
            "project",
            "项目工具目录",
        )

        static_path = self._static_ffmpeg_paths().get(tool_name)
        if static_path:
            add(static_path, "static_ffmpeg", "static-ffmpeg 托管")

        if os.name == "nt":
            add(Path("C:/msys64/ucrt64/bin") / f"{tool_name}.exe", "system", "MSYS2")
            add(Path("C:/msys64/mingw64/bin") / f"{tool_name}.exe", "system", "MSYS2")

        path_candidate = shutil.which(tool_name)
        if path_candidate:
            add(path_candidate, "system", "系统 PATH")
        return candidates, issues

    def _probe_media_executable(self, candidate: Dict[str, str]) -> Dict[str, Any]:
        path = candidate["path"]
        result = self._run([path, "-version"], timeout=20)
        first_line = str(result.stdout or result.stderr or "").strip().splitlines()
        summary = first_line[0].strip() if first_line else ""
        match = re.search(r"\bversion\s+([^\s]+)", summary, re.IGNORECASE)
        return {
            **candidate,
            "available": Path(path).is_file(),
            "healthy": result.returncode == 0,
            "version": match.group(1) if match else None,
            "summary": summary[-500:],
            "error": None if result.returncode == 0 else _command_error(result),
        }

    def _detect_media_component(self, tool_name: str) -> Dict[str, Any]:
        candidates, issues = self._media_candidates(tool_name)
        selected = next((candidate for candidate in candidates if Path(candidate["path"]).is_file()), None)
        if not selected:
            return {
                "name": tool_name,
                "available": False,
                "healthy": False,
                "version": None,
                "path": None,
                "source": "missing",
                "source_label": "未找到",
                "issues": issues,
                "error": f"没有找到 {tool_name} 可执行文件",
            }
        try:
            probed = self._probe_media_executable(selected)
        except Exception as exc:
            probed = {
                **selected,
                "available": True,
                "healthy": False,
                "version": None,
                "error": str(exc),
            }
        probed.update(name=tool_name, issues=issues)
        return probed

    def _latest_maintenance_operation(self, tool_id: str) -> Optional[Dict[str, Any]]:
        records = get_runtime_operation_service().list(
            limit=1,
            owner=f"system:tools:{tool_id}",
        )
        return records[0] if records else None

    def _ffmpeg_card_uncached(self) -> Dict[str, Any]:
        card = self._base_card(
            "ffmpeg",
            "FFmpeg / FFprobe",
            "媒体转码、音视频探测与下载后合并",
            category="maintenance",
            icon="bi-film",
        )
        ffmpeg = self._detect_media_component("ffmpeg")
        ffprobe = self._detect_media_component("ffprobe")
        components = [ffmpeg, ffprobe]
        healthy = all(item.get("healthy") for item in components)
        sources = list(dict.fromkeys(str(item.get("source_label") or "未找到") for item in components))
        managed = any(item.get("source") == "static_ffmpeg" for item in components)
        issues = [issue for item in components for issue in item.get("issues") or []]
        errors = [str(item.get("error")) for item in components if item.get("error")]
        operation = self._latest_maintenance_operation("ffmpeg")
        operation_running = bool(operation and operation.get("status") in _ACTIVE_OPERATION_STATUSES)
        card.update(
            health="ready" if healthy else "degraded",
            health_label="正常" if healthy else "需要修复",
            message=(
                "两个命令均可执行；外部安装只检测、不覆盖"
                if healthy and not managed
                else (
                    "两个命令均由当前 Python 环境托管"
                    if healthy
                    else "；".join((issues + errors)[:3]) or "媒体工具不完整"
                )
            ),
            installed_version=ffmpeg.get("version") or ffprobe.get("version"),
            source_label=" / ".join(sources),
            path=ffmpeg.get("path") or ffprobe.get("path"),
            managed=managed,
            operation=operation,
            operation_running=operation_running,
            actions={
                "check": False,
                "update": False,
                "rollback": False,
                "repair": bool(managed or not healthy),
                "restart": False,
            },
            details={
                "components": components,
                "package_version": _package_version("static-ffmpeg") or None,
            },
        )
        return card

    def _playwright_probe(self, *, launch: bool = False) -> Dict[str, Any]:
        script = """
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    path = p.chromium.executable_path
    result = {"path": path, "installed": Path(path).is_file(), "browser_version": None}
    if %s:
        browser = p.chromium.launch(headless=True)
        try:
            result["browser_version"] = browser.version
        finally:
            browser.close()
    print(json.dumps(result, ensure_ascii=False))
""" % ("True" if launch else "False")
        result = self._run(
            [self.python_executable, "-c", script],
            timeout=180 if launch else 30,
        )
        if result.returncode != 0:
            raise SystemToolError(_command_error(result))
        lines = str(result.stdout or "").strip().splitlines()
        try:
            payload = json.loads(lines[-1]) if lines else {}
        except json.JSONDecodeError as exc:
            raise SystemToolError("Playwright 探测没有返回有效结果") from exc
        if not isinstance(payload, dict):
            raise SystemToolError("Playwright 探测结果格式无效")
        return payload

    def _playwright_card_uncached(self) -> Dict[str, Any]:
        card = self._base_card(
            "playwright",
            "Playwright Chromium",
            "思维导图和 HTML 内容的无头浏览器渲染运行时",
            category="maintenance",
            icon="bi-browser-chrome",
        )
        installed_version = _package_version("playwright")
        probe: Dict[str, Any] = {}
        error = ""
        if installed_version:
            try:
                probe = self._playwright_probe()
            except Exception as exc:
                error = str(exc)
        installed = bool(probe.get("installed"))
        operation = self._latest_maintenance_operation("playwright")
        operation_running = bool(operation and operation.get("status") in _ACTIVE_OPERATION_STATUSES)
        card.update(
            health="ready" if installed else ("degraded" if installed_version else "missing"),
            health_label="正常" if installed else ("浏览器缺失" if installed_version else "包缺失"),
            message=(
                "Chromium 版本与当前 Playwright Python 包配套"
                if installed
                else (
                    error or "需要安装与当前 Playwright 版本匹配的 Chromium"
                    if installed_version
                    else "Playwright Python 包未安装，请先修复应用依赖"
                )
            ),
            installed_version=installed_version or None,
            source_label="当前 Python 环境",
            path=probe.get("path"),
            managed=bool(installed_version),
            operation=operation,
            operation_running=operation_running,
            actions={
                "check": False,
                "update": False,
                "rollback": False,
                "repair": bool(installed_version),
                "restart": False,
            },
            details={"browser_installed": installed, "browser_version": probe.get("browser_version")},
        )
        return card

    def _cached_environment_card(self, tool_id: str, *, force: bool = False) -> Dict[str, Any]:
        now = time.monotonic()
        with self._cache_lock:
            cached = self._environment_cache.get(tool_id)
            if cached and not force and now - cached[0] < 30:
                card = deepcopy(cached[1])
                operation = self._latest_maintenance_operation(tool_id)
                card["operation"] = operation
                card["operation_running"] = bool(
                    operation and operation.get("status") in _ACTIVE_OPERATION_STATUSES
                )
                return card
        card = self._ffmpeg_card_uncached() if tool_id == "ffmpeg" else self._playwright_card_uncached()
        with self._cache_lock:
            self._environment_cache[tool_id] = (now, deepcopy(card))
        return card

    def invalidate_environment(self, tool_id: Optional[str] = None) -> None:
        with self._cache_lock:
            if tool_id:
                self._environment_cache.pop(tool_id, None)
            else:
                self._environment_cache.clear()

    def tool_card(self, tool_id: str, *, force: bool = False) -> Dict[str, Any]:
        normalized = str(tool_id or "").strip().lower()
        if normalized == "codex":
            return self._codex_card()
        if normalized == "litellm":
            return self._litellm_card()
        if normalized in {"yt-dlp", "ytdlp"}:
            return self._ytdlp_card()
        if normalized in {"ffmpeg", "playwright"}:
            return self._cached_environment_card(normalized, force=force)
        raise SystemToolError(f"不支持的工具：{tool_id}")

    def overview(self, *, force_environment: bool = False) -> Dict[str, Any]:
        tools = [
            self.tool_card("codex"),
            self.tool_card("litellm"),
            self.tool_card("yt-dlp"),
            self.tool_card("ffmpeg", force=force_environment),
            self.tool_card("playwright", force=force_environment),
        ]
        return {
            "tools": tools,
            "summary": {
                "total": len(tools),
                "ready": sum(item.get("health") == "ready" for item in tools),
                "attention": sum(item.get("health") not in {"ready"} for item in tools),
                "updates": sum(bool(item.get("update_available")) for item in tools),
                "active": sum(bool(item.get("operation_running")) for item in tools),
                "restart_required": sum(bool(item.get("restart_required")) for item in tools),
            },
        }

    def check_tool(self, tool_id: str) -> Dict[str, Any]:
        normalized = str(tool_id or "").strip().lower()
        if normalized == "codex":
            from app.services.codex_upgrade_service import get_codex_upgrade_service

            get_codex_upgrade_service().check_latest()
        elif normalized == "litellm":
            from app.services.litellm_update_service import get_litellm_update_service

            get_litellm_update_service().check_latest()
        elif normalized in {"yt-dlp", "ytdlp"}:
            from app.services.ytdlp_update_service import get_ytdlp_update_service

            get_ytdlp_update_service().check_latest()
            normalized = "yt-dlp"
        elif normalized in {"ffmpeg", "playwright"}:
            return self.tool_card(normalized, force=True)
        else:
            raise SystemToolError(f"不支持的工具：{tool_id}")
        return self.tool_card(normalized)

    def start_update(self, tool_id: str) -> Dict[str, Any]:
        normalized = str(tool_id or "").strip().lower()
        if normalized == "codex":
            from app.services.codex_upgrade_service import get_codex_upgrade_service

            return get_codex_upgrade_service().start_update()
        if normalized == "litellm":
            from app.services.litellm_update_service import get_litellm_update_service

            return get_litellm_update_service().start_update()
        if normalized in {"yt-dlp", "ytdlp"}:
            from app.services.ytdlp_update_service import get_ytdlp_update_service

            return get_ytdlp_update_service().start_update()
        raise SystemToolError(f"{tool_id} 不支持版本升级")

    def start_rollback(self, tool_id: str) -> Dict[str, Any]:
        if str(tool_id or "").strip().lower() != "codex":
            raise SystemToolError(f"{tool_id} 没有可用的手动回退")
        from app.services.codex_upgrade_service import get_codex_upgrade_service

        return get_codex_upgrade_service().start_rollback()

    def _repair_ffmpeg(self, context: OperationContext) -> Dict[str, Any]:
        context.progress(10, "检查 static-ffmpeg 托管包")
        context.check_cancelled()
        reinstall = self._run(
            [
                self.python_executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--force-reinstall",
                "--no-deps",
                f"static-ffmpeg=={STATIC_FFMPEG_VERSION}",
            ],
            timeout=1200,
        )
        if reinstall.returncode != 0:
            raise SystemToolError(f"static-ffmpeg 修复失败：{_command_error(reinstall)}")

        context.progress(55, "准备 FFmpeg 与 FFprobe 执行文件")
        prepare_script = """
import json
from static_ffmpeg import run
ffmpeg, ffprobe = run.get_or_fetch_platform_executables_else_raise()
print(json.dumps({"ffmpeg": ffmpeg, "ffprobe": ffprobe}, ensure_ascii=False))
"""
        prepared = self._run(
            [self.python_executable, "-c", prepare_script],
            timeout=1200,
        )
        if prepared.returncode != 0:
            raise SystemToolError(f"FFmpeg 下载或准备失败：{_command_error(prepared)}")
        context.check_cancelled()

        context.progress(82, "验证 FFmpeg 与 FFprobe")
        lines = str(prepared.stdout or "").strip().splitlines()
        try:
            paths = json.loads(lines[-1]) if lines else {}
        except json.JSONDecodeError as exc:
            raise SystemToolError("FFmpeg 准备过程没有返回有效路径") from exc
        results = {}
        for name in ("ffmpeg", "ffprobe"):
            path = str(paths.get(name) or "")
            if not path or not Path(path).is_file():
                raise SystemToolError(f"修复后仍未找到 {name} 可执行文件")
            probe = self._probe_media_executable(
                {"path": path, "source": "static_ffmpeg", "source_label": "static-ffmpeg 托管"}
            )
            if not probe.get("healthy"):
                raise SystemToolError(f"{name} 校验失败：{probe.get('error') or '未知错误'}")
            results[name] = probe
        self.invalidate_environment("ffmpeg")
        context.progress(100, "FFmpeg 与 FFprobe 已修复")
        return {"components": results}

    def _repair_playwright(self, context: OperationContext) -> Dict[str, Any]:
        if not _package_version("playwright"):
            raise SystemToolError("Playwright Python 包未安装，请先修复应用依赖")
        context.progress(15, "安装与当前 Playwright 匹配的 Chromium")
        install = self._run(
            [self.python_executable, "-m", "playwright", "install", "chromium"],
            timeout=1800,
        )
        if install.returncode != 0:
            raise SystemToolError(f"Chromium 安装失败：{_command_error(install)}")
        context.check_cancelled()
        context.progress(80, "启动无头 Chromium 进行验证")
        probe = self._playwright_probe(launch=True)
        if not probe.get("installed"):
            raise SystemToolError("安装结束后仍未找到 Chromium")
        self.invalidate_environment("playwright")
        context.progress(100, "Playwright Chromium 已安装并通过启动验证")
        return probe

    def _run_repair_with_audit(
        self,
        tool_id: str,
        target: Callable[[OperationContext], Dict[str, Any]],
        context: OperationContext,
    ) -> Dict[str, Any]:
        audit = get_runtime_operation_service()
        try:
            result = target(context)
            audit.record_audit(
                category="tool_maintenance",
                action="repair_tool",
                target=tool_id,
                summary=f"{tool_id} 环境修复完成",
                after=result,
            )
            return result
        except Exception as exc:
            audit.record_audit(
                category="tool_maintenance",
                action="repair_tool",
                target=tool_id,
                status="failed",
                summary=str(exc),
            )
            raise

    def start_repair(self, tool_id: str) -> Dict[str, Any]:
        normalized = str(tool_id or "").strip().lower()
        targets: Dict[str, Callable[[OperationContext], Dict[str, Any]]] = {
            "ffmpeg": self._repair_ffmpeg,
            "playwright": self._repair_playwright,
        }
        target = targets.get(normalized)
        if not target:
            raise SystemToolError(f"{tool_id} 不支持环境修复")
        card = self.tool_card(normalized)
        if not (card.get("actions") or {}).get("repair"):
            raise SystemToolError(
                f"{card.get('title') or tool_id} 当前由外部安装管理，Mabobot 不会覆盖它"
            )
        owner = f"system:tools:{normalized}"
        active = next(
            (
                item
                for item in get_runtime_operation_service().list(limit=10, owner=owner)
                if item.get("status") in _ACTIVE_OPERATION_STATUSES
            ),
            None,
        )
        if active:
            raise SystemToolError(f"{self.tool_card(normalized).get('title')} 修复任务正在运行")
        return get_runtime_operation_service().submit(
            owner=owner,
            kind=f"{normalized}_repair",
            title=f"修复 {card.get('title')}",
            target=lambda context: self._run_repair_with_audit(normalized, target, context),
            details={"tool_id": normalized},
        )


_service_lock = threading.Lock()
_service: Optional[SystemToolService] = None


def get_system_tool_service() -> SystemToolService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = SystemToolService()
    return _service
