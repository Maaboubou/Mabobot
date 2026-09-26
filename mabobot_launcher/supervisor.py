"""Process supervision and health monitoring for Mabobot services."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
from mabobot_logging import create_json_handler, log_path

from .constants import LOG_DIR, PROJECT_ROOT, SERVICES, ServiceSpec

_PRESERVED_BROWSER_PROCESSES = {"chrome", "chrome.exe", "msedge", "msedge.exe"}
_STATUS_LABELS = {
    "stopped": "已停止",
    "starting": "启动中",
    "running": "正常",
    "degraded": "需关注",
    "stopping": "停止中",
    "error": "异常",
}
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ERROR_OUTPUT_RE = re.compile(
    r"(?:\[(?:ERROR|CRITICAL|FATAL)\])|(?:^(?:ERROR|CRITICAL|FATAL)(?:\s|:))",
    re.IGNORECASE,
)
_WARNING_OUTPUT_RE = re.compile(
    r"(?:\[(?:WARNING|WARN)\])|(?:^(?:WARNING|WARN)(?:\s|:))",
    re.IGNORECASE,
)


def infer_output_level(message: str) -> str:
    """Infer a child-process log level from explicit logging markers."""
    plain = _ANSI_ESCAPE_RE.sub("", str(message or "")).strip()
    if plain.startswith("Traceback (most recent call last):"):
        return "error"
    if _ERROR_OUTPUT_RE.search(plain):
        return "error"
    if _WARNING_OUTPUT_RE.search(plain):
        return "warning"
    return "info"


class LogBuffer:
    def __init__(self, max_items: int = 1200, log_file: Path | None = None):
        self._items: deque[dict[str, Any]] = deque(maxlen=max_items)
        self._sequence = 0
        self._lock = threading.RLock()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        target = log_file or log_path("launcher")
        self._logger = logging.getLogger(f"mabobot.launcher.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._logger.addHandler(create_json_handler("launcher", target))

    def add(self, source: str, message: str, level: str = "info", *,
            persist: bool = True) -> dict[str, Any]:
        normalized_level = (
            level if level in {"info", "success", "warning", "error"} else "info"
        )
        clean_message = str(message or "").rstrip("\r\n")
        with self._lock:
            self._sequence += 1
            item = {
                "seq": self._sequence,
                "timestamp": time.time(),
                "source": source,
                "level": normalized_level,
                "message": clean_message,
            }
            self._items.append(item)
        logger_level = (
            logging.ERROR
            if normalized_level == "error"
            else (logging.WARNING if normalized_level == "warning" else logging.INFO)
        )
        if persist:
            self._logger.log(logger_level, "%s", clean_message,
                             extra={"event": "launcher.event", "source": source})
        return item

    def snapshot(self, since: int = 0) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            items = list(self._items)
            if since > 0:
                items = [item for item in items if int(item["seq"]) > since]
            return items, self._sequence

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


@dataclass
class ServiceRuntime:
    spec: ServiceSpec
    process: subprocess.Popen[str] | None = None
    status: str = "stopped"
    detail: str = "等待启动"
    started_at: float | None = None
    exit_code: int | None = None
    expected_stop: bool = False
    generation: int = 0
    health_failures: int = 0
    crash_times: deque[float] = field(default_factory=lambda: deque(maxlen=8))
    extra: dict[str, Any] = field(default_factory=dict)


def collect_stoppable_processes(root_pid: int) -> tuple[list[psutil.Process], int]:
    """Collect a managed process tree while preserving reusable browsers."""
    try:
        root = psutil.Process(root_pid)
        descendants = root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return [], 0

    targets: list[psutil.Process] = []
    preserved_count = 0
    for process in reversed(descendants):
        try:
            process_name = process.name().casefold()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            process_name = ""
        if process_name in _PRESERVED_BROWSER_PROCESSES:
            preserved_count += 1
            continue
        targets.append(process)
    targets.append(root)
    return targets, preserved_count


class ServiceSupervisor:
    def __init__(
        self, project_root: Path = PROJECT_ROOT, specs: Iterable[ServiceSpec] = SERVICES
    ):
        self.project_root = Path(project_root)
        self.logs = LogBuffer()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._shutting_down = False
        self._restore_blocked = False
        self._runtimes = {spec.key: ServiceRuntime(spec=spec) for spec in specs}
        self._monitor_thread: threading.Thread | None = None

    def start_monitoring(self) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._health_loop,
            name="mabobot-health-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def _port_for(self, spec: ServiceSpec) -> int:
        try:
            return int(str(os.getenv(spec.port_env) or spec.default_port).strip())
        except ValueError:
            return spec.default_port

    def _health_url(self, spec: ServiceSpec) -> str:
        return f"http://127.0.0.1:{self._port_for(spec)}{spec.health_path}"

    def _probe(self, spec: ServiceSpec) -> tuple[bool, dict[str, Any]]:
        try:
            request = urllib.request.Request(
                self._health_url(spec),
                headers={"User-Agent": "Mabobot-Launcher/3"},
            )
            with urllib.request.urlopen(request, timeout=0.8) as response:
                if not 200 <= int(response.status) < 300:
                    return False, {}
                content_type = str(response.headers.get("Content-Type") or "")
                if "json" not in content_type.casefold():
                    return True, {}
                try:
                    payload = json.loads(
                        response.read().decode("utf-8", errors="replace")
                    )
                except (ValueError, UnicodeDecodeError):
                    payload = {}
                return True, payload if isinstance(payload, dict) else {}
        except (OSError, urllib.error.URLError, TimeoutError):
            return False, {}

    def _creation_flags(self) -> int:
        if os.name != "nt":
            return 0
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) | int(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )

    def _apply_restore_before_services(self) -> None:
        from dotenv import dotenv_values
        values = dotenv_values(self.project_root / ".env")
        backup_dir = Path(os.getenv("SYSTEM_BACKUP_DIR") or values.get("SYSTEM_BACKUP_DIR") or "data/system_backups")
        if not backup_dir.is_absolute():
            backup_dir = self.project_root / backup_dir
        pending = backup_dir / "pending_restore.json"
        if not pending.exists():
            if self._restore_blocked:
                raise RuntimeError("上次恢复失败，已阻止启动；请检查 restore-failed 记录并重新创建恢复计划")
            return
        if any(item.process and item.process.poll() is None for item in self._runtimes.values()):
            raise RuntimeError("存在待恢复备份，请先停止全部服务，再重新启动")
        # A fresh process loads the current restore code before either service
        # can open a database. All later children import the restored code.
        self._restore_blocked = True
        result = subprocess.run(
            [sys.executable, "-c", "from dotenv import load_dotenv; load_dotenv('.env'); "
             "from app.services.backup_service import apply_pending_restore; apply_pending_restore()"],
            cwd=str(self.project_root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", creationflags=self._creation_flags(),
        )
        if result.returncode or pending.exists():
            raise RuntimeError("备份恢复失败，已阻止服务启动；请查看备份目录中的 restore-failed 记录")
        self._restore_blocked = False
        self.logs.add("系统", "备份已恢复，开始启动服务", "success")

    def start_service(self, key: str, *, automatic: bool = False) -> dict[str, Any]:
        with self._lock:
            runtime = self._require_runtime(key)
            if self._shutting_down:
                return self._snapshot_runtime(runtime)
            if runtime.process and runtime.process.poll() is None:
                return self._snapshot_runtime(runtime)

            try:
                self._apply_restore_before_services()
            except Exception as exc:
                runtime.status = "error"
                runtime.detail = str(exc)
                self.logs.add("系统", runtime.detail, "error")
                return self._snapshot_runtime(runtime)

            script = self.project_root / runtime.spec.script
            if not script.is_file():
                runtime.status = "error"
                runtime.detail = f"缺少 {runtime.spec.script}"
                self.logs.add("系统", runtime.detail, "error")
                return self._snapshot_runtime(runtime)

            if not automatic:
                runtime.crash_times.clear()
            runtime.generation += 1
            generation = runtime.generation
            runtime.expected_stop = False
            runtime.exit_code = None
            runtime.health_failures = 0
            runtime.extra = {}
            runtime.status = "starting"
            runtime.detail = "正在等待健康检查"

            environment = os.environ.copy()
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["PYTHONUNBUFFERED"] = "1"
            try:
                process = subprocess.Popen(
                    [sys.executable, "-u", str(script)],
                    cwd=str(self.project_root),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=self._creation_flags(),
                )
            except OSError as exc:
                runtime.status = "error"
                runtime.detail = f"启动失败：{exc}"
                self.logs.add(runtime.spec.label, runtime.detail, "error")
                return self._snapshot_runtime(runtime)

            runtime.process = process
            runtime.started_at = time.time()
            self.logs.add("系统", f"正在启动 {runtime.spec.label} · PID {process.pid}")
            threading.Thread(
                target=self._read_output,
                args=(runtime.spec.key, process),
                name=f"mabobot-{runtime.spec.key}-output",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._watch_process,
                args=(runtime.spec.key, process, generation),
                name=f"mabobot-{runtime.spec.key}-watch",
                daemon=True,
            ).start()
            return self._snapshot_runtime(runtime)

    def _read_output(self, key: str, process: subprocess.Popen[str]) -> None:
        stream = process.stdout
        if stream is None:
            return
        startup_lines: deque[str] = deque(maxlen=40)
        recent_lines: deque[str] = deque(maxlen=20)
        logging_ready = False
        try:
            for line in stream:
                message = line.rstrip("\r\n")
                if message:
                    recent_lines.append(message)
                    runtime = self._runtimes[key]
                    if "logging.ready service=" in message:
                        logging_ready = True
                        startup_lines.clear()
                    elif not logging_ready:
                        startup_lines.append(message)
                    self.logs.add(
                        runtime.spec.label,
                        message,
                        infer_output_level(message),
                        persist=False,
                    )
        except (OSError, ValueError) as exc:
            if process.poll() is None:
                self.logs.add(
                    self._runtimes[key].spec.label, f"日志读取中断：{exc}", "warning"
                )
        finally:
            runtime = self._runtimes[key]
            if not logging_ready and startup_lines:
                self.logs.add(runtime.spec.label,
                              "子进程在日志初始化前退出，保留最近启动输出", "error")
                for message in startup_lines:
                    self.logs.add(runtime.spec.label, message,
                                  infer_output_level(message))
            elif process.poll() not in (None, 0) and recent_lines:
                self.logs.add(runtime.spec.label,
                              "子进程异常退出，保留最近输出", "error")
                for message in recent_lines:
                    self.logs.add(runtime.spec.label, message,
                                  infer_output_level(message))
            try:
                stream.close()
            except OSError:
                pass

    def _watch_process(
        self, key: str, process: subprocess.Popen[str], generation: int
    ) -> None:
        exit_code = process.wait()
        schedule_restart = False
        restart_delay = 0
        with self._lock:
            runtime = self._runtimes[key]
            if runtime.process is not process:
                return
            runtime.process = None
            runtime.exit_code = exit_code
            runtime.extra = {}
            if runtime.expected_stop or self._shutting_down:
                runtime.status = "stopped"
                runtime.detail = "已停止"
                self.logs.add("系统", f"{runtime.spec.label} 已停止", "success")
                return

            runtime.status = "error"
            runtime.detail = f"进程意外退出（代码 {exit_code}）"
            now = time.monotonic()
            runtime.crash_times.append(now)
            recent_crashes = [
                stamp for stamp in runtime.crash_times if now - stamp <= 300
            ]
            self.logs.add(runtime.spec.label, runtime.detail, "error")
            if len(recent_crashes) <= 5:
                restart_delay = min(30, 2 ** max(1, len(recent_crashes)))
                runtime.detail += f"，{restart_delay} 秒后自动重试"
                schedule_restart = True
            else:
                runtime.detail += "，频繁失败后已暂停自动重试"
                self.logs.add(
                    "系统",
                    f"{runtime.spec.label} 5 分钟内多次退出，已暂停自动重试",
                    "warning",
                )

        if schedule_restart:
            timer = threading.Timer(
                restart_delay,
                self._restart_after_crash,
                args=(key, generation),
            )
            timer.daemon = True
            timer.start()

    def _restart_after_crash(self, key: str, generation: int) -> None:
        with self._lock:
            runtime = self._runtimes[key]
            if (
                self._shutting_down
                or runtime.generation != generation
                or runtime.process is not None
            ):
                return
        self.logs.add(
            "系统", f"正在自动恢复 {self._runtimes[key].spec.label}", "warning"
        )
        self.start_service(key, automatic=True)

    def stop_service(self, key: str) -> dict[str, Any]:
        with self._lock:
            runtime = self._require_runtime(key)
            process = runtime.process
            runtime.generation += 1
            runtime.expected_stop = True
            if not process or process.poll() is not None:
                runtime.process = None
                runtime.status = "stopped"
                runtime.detail = "已停止"
                return self._snapshot_runtime(runtime)
            runtime.status = "stopping"
            runtime.detail = "正在停止进程树"

        targets, preserved_count = collect_stoppable_processes(process.pid)
        if preserved_count:
            self.logs.add(
                "系统",
                f"停止 {runtime.spec.label} 时保留 {preserved_count} 个浏览器进程",
            )
        if not targets:
            try:
                process.terminate()
            except OSError:
                pass
        for target in targets:
            try:
                target.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _gone, alive = psutil.wait_procs(targets, timeout=2.5)
        for target in alive:
            try:
                target.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass

        with self._lock:
            if runtime.process is process:
                runtime.process = None
                runtime.status = "stopped"
                runtime.detail = "已停止"
                runtime.extra = {}
            return self._snapshot_runtime(runtime)

    def restart_service(self, key: str) -> dict[str, Any]:
        self.stop_service(key)
        return self.start_service(key)

    def start_all(self) -> list[dict[str, Any]]:
        results = []
        for key in tuple(self._runtimes):
            result = self.start_service(key)
            results.append(result)
            if self._restore_blocked:
                break
        return results

    def stop_all(self) -> list[dict[str, Any]]:
        return [self.stop_service(key) for key in tuple(self._runtimes)]

    def restart_all(self) -> list[dict[str, Any]]:
        self.stop_all()
        return self.start_all()

    def _health_loop(self) -> None:
        while not self._stop_event.wait(1.0):
            with self._lock:
                active = [
                    (key, runtime.spec, runtime.process, runtime.status)
                    for key, runtime in self._runtimes.items()
                    if runtime.process and runtime.process.poll() is None
                ]
            for key, spec, process, previous_status in active:
                healthy, payload = self._probe(spec)
                with self._lock:
                    runtime = self._runtimes[key]
                    if runtime.process is not process:
                        continue
                    if healthy:
                        runtime.health_failures = 0
                        runtime.extra = self._health_extra(key, payload)
                        if runtime.status != "running":
                            runtime.status = "running"
                            runtime.detail = "健康检查通过"
                            self.logs.add(
                                "系统", f"{runtime.spec.label} 已就绪", "success"
                            )
                    else:
                        runtime.health_failures += 1
                        elapsed = time.time() - (runtime.started_at or time.time())
                        if (
                            elapsed >= runtime.spec.startup_grace_seconds
                            and runtime.health_failures >= 4
                        ):
                            runtime.status = "degraded"
                            runtime.detail = "进程仍在运行，但健康检查未通过"
                            if previous_status != "degraded":
                                self.logs.add(
                                    runtime.spec.label, runtime.detail, "warning"
                                )

    @staticmethod
    def _health_extra(key: str, payload: dict[str, Any]) -> dict[str, Any]:
        if key != "bot":
            return {}
        return {
            "wechat_connected": bool(payload.get("wechat_connected")),
            "wechat_online": bool(payload.get("wechat_online")),
        }

    def ingest_external_log(
        self, source: str, message: str, level: str = "info"
    ) -> None:
        self.logs.add(source, message, level)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._snapshot_runtime(runtime) for runtime in self._runtimes.values()
            ]

    def _snapshot_runtime(self, runtime: ServiceRuntime) -> dict[str, Any]:
        process = runtime.process
        is_alive = bool(process and process.poll() is None)
        uptime = (
            max(0, int(time.time() - runtime.started_at))
            if is_alive and runtime.started_at
            else 0
        )
        return {
            "key": runtime.spec.key,
            "label": runtime.spec.label,
            "script": runtime.spec.script,
            "status": runtime.status,
            "status_label": _STATUS_LABELS.get(runtime.status, runtime.status),
            "detail": runtime.detail,
            "pid": process.pid if is_alive and process else None,
            "uptime_seconds": uptime,
            "address": f"127.0.0.1:{self._port_for(runtime.spec)}",
            "url": self._health_url(runtime.spec),
            "extra": dict(runtime.extra),
        }

    def _require_runtime(self, key: str) -> ServiceRuntime:
        try:
            return self._runtimes[str(key).strip().casefold()]
        except KeyError as exc:
            raise ValueError(f"Unknown Mabobot service: {key}") from exc

    def shutdown(self) -> None:
        with self._lock:
            self._shutting_down = True
        self._stop_event.set()
        self.stop_all()
