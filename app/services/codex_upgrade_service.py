"""Managed Codex CLI update checks and rolling activation."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.agent_runtime import CodexAgentRuntime, get_agent_runtime
from app.services.file_tools_runtime import get_file_tools_runtime
from app.utils.subprocess_utils import hidden_process_kwargs


logger = logging.getLogger(__name__)

_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)(?!\d)")
_STABLE_VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_VERSION_CHECK_FORMAT = 1


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _version_number(value: Any) -> str:
    match = _VERSION_PATTERN.search(str(value or ""))
    return match.group(1) if match else ""


def _version_key(value: Any) -> tuple:
    version = _version_number(value)
    base, _, suffix = version.partition("-")
    parts = tuple(int(part) for part in base.split(".")) if base else (0, 0, 0)
    return (*parts, 0 if suffix else 1, suffix)


def _positive_timeout(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(120, value)


class CodexUpgradeError(RuntimeError):
    pass


def _npm_stable_version(stdout: str) -> str:
    # Login shells may prepend MOTD/nvm output. npm's scalar --json result
    # occupies the final nonempty line; never search arbitrary text for digits.
    lines = stdout.strip().splitlines()
    try:
        value = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        value = None
    if not isinstance(value, str) or not _STABLE_VERSION_PATTERN.fullmatch(value):
        raise CodexUpgradeError("无法识别最新 Codex 版本：npm 未返回有效的稳定版 JSON 字符串")
    return value


class CodexUpgradeService:
    """Install the latest global Codex and let the runtime switch pools safely."""

    def __init__(
        self,
        *,
        runtime: Optional[CodexAgentRuntime] = None,
        database_path: Optional[Path] = None,
    ) -> None:
        self.runtime = runtime or get_agent_runtime()
        configured = database_path or os.getenv("CODEX_RUNTIME_STATE_DB") or "data/codex_runtime.db"
        self.database_path = Path(configured)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._worker: Optional[threading.Thread] = None
        self._operation: Optional[Dict[str, Any]] = None
        self._available_version = ""
        self._checked_at = ""
        self._install_cache: Optional[Dict[str, Any]] = None
        self._install_cache_at = 0.0
        self._initialize_store()
        self._load_state()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize_store(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS codex_upgrade_operations (
                    operation_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    operation_json TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS codex_upgrade_state (
                    state_key TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def _load_state(self) -> None:
        with closing(self._connect()) as connection, connection:
            state_row = connection.execute(
                "SELECT state_json FROM codex_upgrade_state WHERE state_key = 'current'"
            ).fetchone()
            operation_row = connection.execute(
                """
                SELECT operation_json FROM codex_upgrade_operations
                ORDER BY updated_at DESC LIMIT 1
                """
            ).fetchone()
        if state_row:
            try:
                state = json.loads(state_row["state_json"])
                # Older checks could persist a version extracted from a shell
                # banner. Require a fresh check before trusting those caches.
                if (
                    state.get("version_check_format") == _VERSION_CHECK_FORMAT
                    and isinstance(state.get("available_version"), str)
                    and _STABLE_VERSION_PATTERN.fullmatch(state["available_version"])
                ):
                    self._available_version = state["available_version"]
                    self._checked_at = str(state.get("checked_at") or "")
            except (TypeError, json.JSONDecodeError):
                pass
        if operation_row:
            try:
                operation = json.loads(operation_row["operation_json"])
            except (TypeError, json.JSONDecodeError):
                operation = None
            if isinstance(operation, dict):
                if operation.get("status") in {"queued", "running"}:
                    operation["status"] = "failed"
                    operation["stage"] = "interrupted"
                    operation["message"] = "Web 服务重启，更新任务已中止"
                    operation["finished_at"] = _now_iso()
                    self._persist_operation(operation)
                self._operation = operation

    def _persist_state(self) -> None:
        payload = {
            "version_check_format": _VERSION_CHECK_FORMAT,
            "available_version": self._available_version,
            "checked_at": self._checked_at,
        }
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO codex_upgrade_state (state_key, state_json, updated_at)
                VALUES ('current', ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), time.time()),
            )

    def _persist_operation(self, operation: Dict[str, Any]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO codex_upgrade_operations
                    (operation_id, kind, status, operation_json, started_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    status = excluded.status,
                    operation_json = excluded.operation_json,
                    updated_at = excluded.updated_at
                """,
                (
                    operation["operation_id"],
                    operation["kind"],
                    operation["status"],
                    json.dumps(operation, ensure_ascii=False, separators=(",", ":"), default=str),
                    float(operation.get("started_at_epoch") or time.time()),
                    time.time(),
                ),
            )

    def _command(self, args: List[str]) -> List[str]:
        if not self.runtime.probe.use_wsl:
            return args
        return [
            "wsl.exe",
            "bash",
            "-lic",
            " ".join(shlex.quote(str(arg)) for arg in args),
        ]

    def _run(self, args: List[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
        result = subprocess.run(
            self._command(args),
            cwd=self.runtime.workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            **hidden_process_kwargs(),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise CodexUpgradeError(detail[-1500:])
        return result

    def _run_fixed_shell(self, script: str, *, timeout: int = 600) -> subprocess.CompletedProcess:
        command = ["bash", "-lc", script]
        if self.runtime.probe.use_wsl:
            command = ["wsl.exe", "bash", "-lic", script]
        result = subprocess.run(
            command,
            cwd=self.runtime.workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            **hidden_process_kwargs(),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise CodexUpgradeError(detail[-1500:])
        return result

    def detect_installation(self, *, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            if (
                not force
                and self._install_cache is not None
                and time.monotonic() - self._install_cache_at < 30
            ):
                return dict(self._install_cache)
        identity = self.runtime.status().get("compatibility") or {}
        version = str(identity.get("version") or self.runtime.status().get("active_version") or "")
        executable = str(identity.get("executable") or self.runtime.probe.codex_bin)
        realpath = str(identity.get("realpath") or executable)
        try:
            if self.runtime.probe.use_wsl:
                snapshot = get_file_tools_runtime(
                    use_wsl=True,
                    codex_bin=executable,
                    force=force,
                )
                resolved = snapshot.get("codex") if isinstance(snapshot.get("codex"), dict) else {}
                executable = str(resolved.get("path") or executable)
                realpath = str(resolved.get("realpath") or realpath)
            elif executable:
                realpath = str(Path(executable).resolve())
        except Exception:
            logger.debug("Unable to resolve Codex installation path", exc_info=True)

        # A freshly started management process may know the selected WSL path
        # before an App Server pool has reported its active identity. Ask that
        # exact executable for its version so update controls do not disappear.
        if not _version_number(version) and executable:
            try:
                version_result = self._run([executable, "--version"], timeout=30)
                version_lines = str(version_result.stdout or "").strip().splitlines()
                if version_lines:
                    version = version_lines[-1].strip()
            except Exception:
                logger.debug("Unable to read Codex CLI version directly", exc_info=True)

        normalized = realpath.replace("\\", "/").lower()
        if "/node_modules/@openai/codex/" in normalized:
            method = "npm"
            label = "npm 全局安装"
            rollback_supported = True
        elif "/homebrew/" in normalized or "/cellar/" in normalized:
            method = "homebrew"
            label = "Homebrew"
            rollback_supported = False
        else:
            method = "standalone"
            label = "官方安装器"
            rollback_supported = False
        info = {
            "method": method,
            "method_label": label,
            "executable": executable,
            "realpath": realpath,
            "version": version,
            "version_number": _version_number(version),
            "rollback_supported": rollback_supported,
        }
        with self._lock:
            self._install_cache = dict(info)
            self._install_cache_at = time.monotonic()
        return info

    def invalidate_installation_cache(self) -> None:
        with self._lock:
            self._install_cache = None
            self._install_cache_at = 0.0

    def check_latest(self) -> Dict[str, Any]:
        installation = self.detect_installation(force=True)
        result = self._run(["npm", "view", "@openai/codex", "version", "--json"], timeout=45)
        available = _npm_stable_version(result.stdout or "")
        with self._lock:
            self._available_version = available
            self._checked_at = _now_iso()
            self._persist_state()
        current = installation.get("version_number") or ""
        return {
            "installed_version": current,
            "available_version": available,
            "update_available": bool(current and _version_key(available) > _version_key(current)),
            "checked_at": self._checked_at,
            "installation": installation,
        }

    def _set_stage(self, stage: str, progress: int, message: str) -> None:
        with self._lock:
            if not self._operation:
                return
            self._operation.update(
                {
                    "status": "running",
                    "stage": stage,
                    "progress": max(0, min(int(progress), 100)),
                    "message": message,
                    "updated_at": _now_iso(),
                }
            )
            log = {"at": self._operation["updated_at"], "stage": stage, "message": message}
            self._operation.setdefault("logs", []).append(log)
            self._operation["logs"] = self._operation["logs"][-30:]
            self._persist_operation(self._operation)

    def _install(self, installation: Dict[str, Any], target: str) -> None:
        method = installation.get("method")
        if target != "latest" and not _VERSION_PATTERN.fullmatch(str(target)):
            raise CodexUpgradeError("无效的 Codex 版本")
        if method == "npm":
            package = "@openai/codex@latest" if target == "latest" else f"@openai/codex@{target}"
            self._run(
                ["npm", "install", "-g", package, "--no-audit", "--no-fund"],
                timeout=_positive_timeout("CODEX_UPGRADE_TIMEOUT_SECONDS", 600),
            )
            return
        if method == "homebrew":
            if target != "latest":
                raise CodexUpgradeError("Homebrew 安装暂不支持自动回退")
            self._run(["brew", "upgrade", "codex"], timeout=600)
            return
        if target != "latest":
            raise CodexUpgradeError("官方安装器暂不支持自动回退")
        self._run_fixed_shell(
            "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
            timeout=_positive_timeout("CODEX_UPGRADE_TIMEOUT_SECONDS", 600),
        )

    def _rollback_install(self, installation: Dict[str, Any], previous_version: str) -> None:
        if not previous_version or not installation.get("rollback_supported"):
            raise CodexUpgradeError("当前安装方式没有可用的自动回退版本")
        self._set_stage("rollback", 75, f"恢复 Codex {previous_version}")
        self._install(installation, previous_version)
        self._install_cache = None
        self.runtime.refresh(force=True)

    def _run_operation(self, *, rollback_only: bool = False) -> None:
        operation = self._operation or {}
        self.runtime.set_maintenance(True)
        installation: Dict[str, Any] = {}
        previous_version = ""
        install_attempted = False
        try:
            self._set_stage("inspect", 5, "读取当前安装信息")
            installation = self.detect_installation(force=True)
            previous_version = str(
                operation.get("target_version")
                if rollback_only
                else installation.get("version_number") or ""
            )
            operation["install_method"] = installation.get("method")
            operation["previous_version"] = installation.get("version_number") or ""

            if rollback_only:
                if not previous_version:
                    raise CodexUpgradeError("没有可回退的 Codex 版本")
                self._rollback_install(installation, previous_version)
                target_version = previous_version
            else:
                self._set_stage("check", 15, "检查最新稳定版本")
                latest = self.check_latest()
                target_version = str(latest["available_version"])
                operation["target_version"] = target_version
                if not latest.get("update_available"):
                    self._complete_operation(
                        "succeeded",
                        "当前版本已不低于最新稳定版",
                        previous_version,
                    )
                    return
                self._set_stage("install", 35, f"安装 Codex {target_version}")
                install_attempted = True
                # npm can install the exact version returned by the check. This
                # avoids a moving "latest" tag between validation and install.
                install_target = target_version if installation.get("method") == "npm" else "latest"
                self._install(installation, install_target)
                self._install_cache = None

            self._set_stage("protocol", 60, "验证版本与 App Server 协议")
            identity = self.runtime.probe.probe()
            if not identity.compatible:
                raise CodexUpgradeError("; ".join(identity.errors) or "协议验证失败")
            if target_version and _version_number(identity.version) != target_version:
                raise CodexUpgradeError(
                    f"安装后版本不一致：期望 {target_version}，实际 {identity.version}"
                )

            self._set_stage("activate", 78, "启动候选进程池")
            self.runtime.refresh(force=True)
            runtime_status = self.runtime.status()
            if not runtime_status.get("running"):
                raise CodexUpgradeError(str(runtime_status.get("last_error") or "候选进程池未就绪"))
            if _version_number(runtime_status.get("active_version")) != target_version:
                raise CodexUpgradeError("候选版本未接管运行时")

            self._set_stage("verify", 92, "确认双池与稳定协议")
            pools = runtime_status.get("pools") or {}
            if not all(
                pools.get(name, {}).get("workers")
                and all(worker.get("running") for worker in pools[name]["workers"])
                for name in ("interactive", "batch")
            ):
                raise CodexUpgradeError("交互池或批处理池未就绪")
            if not rollback_only:
                self._available_version = target_version
                self._checked_at = _now_iso()
                self._persist_state()
            final_status = "rolled_back" if rollback_only else "succeeded"
            final_message = (
                f"已恢复 Codex {target_version}"
                if rollback_only
                else f"Codex {target_version} 已接管运行时"
            )
            self._complete_operation(final_status, final_message, target_version)
        except Exception as exc:
            error = str(exc)
            restored_version = ""
            if (
                not rollback_only
                and install_attempted
                and installation.get("rollback_supported")
                and previous_version
            ):
                try:
                    self._rollback_install(installation, previous_version)
                    restored_version = previous_version
                    error += f"；已恢复 {previous_version}"
                except Exception as rollback_exc:
                    error += f"；自动恢复失败：{rollback_exc}"
            self._complete_operation("failed", error, restored_version)
            logger.exception("Codex update operation failed")
        finally:
            self.runtime.set_maintenance(False)

    def _complete_operation(self, status: str, message: str, installed_version: str) -> None:
        with self._lock:
            if not self._operation:
                return
            self._operation.update(
                {
                    "status": status,
                    "stage": "complete" if status in {"succeeded", "rolled_back"} else "failed",
                    "progress": 100,
                    "message": message,
                    "installed_version": installed_version,
                    "finished_at": _now_iso(),
                    "updated_at": _now_iso(),
                }
            )
            self._persist_operation(self._operation)

    def _start(self, *, kind: str, target_version: str = "") -> Dict[str, Any]:
        with self._lock:
            if self._worker and self._worker.is_alive():
                raise CodexUpgradeError("已有 Codex 更新任务正在运行")
            now = _now_iso()
            self._operation = {
                "operation_id": uuid.uuid4().hex,
                "kind": kind,
                "status": "queued",
                "stage": "queued",
                "progress": 0,
                "message": "等待执行",
                "started_at": now,
                "started_at_epoch": time.time(),
                "updated_at": now,
                "target_version": target_version,
                "logs": [],
            }
            self._persist_operation(self._operation)
            self._worker = threading.Thread(
                target=self._run_operation,
                kwargs={"rollback_only": kind == "rollback"},
                name=f"codex-{kind}",
                daemon=True,
            )
            self._worker.start()
            return dict(self._operation)

    def start_update(self) -> Dict[str, Any]:
        return self._start(kind="update")

    def start_rollback(self) -> Dict[str, Any]:
        installation = self.detect_installation()
        current = str(installation.get("version_number") or "")
        previous = self._find_rollback_version(current)
        if not previous:
            raise CodexUpgradeError("没有可回退的 Codex 版本")
        return self._start(kind="rollback", target_version=previous)

    def _find_rollback_version(self, current_version: str) -> str:
        with self._lock:
            previous = ""
            if (
                self._operation
                and self._operation.get("status") == "succeeded"
                and _version_number(self._operation.get("installed_version")) == current_version
            ):
                previous = str(self._operation.get("previous_version") or "")
            if not previous:
                with closing(self._connect()) as connection, connection:
                    rows = connection.execute(
                        """
                        SELECT operation_json FROM codex_upgrade_operations
                        WHERE status = 'succeeded' ORDER BY updated_at DESC LIMIT 5
                        """
                    ).fetchall()
                for row in rows:
                    try:
                        payload = json.loads(row["operation_json"])
                    except (TypeError, json.JSONDecodeError):
                        continue
                    installed = _version_number(payload.get("installed_version"))
                    candidate = str(payload.get("previous_version") or "")
                    if installed == current_version and candidate and candidate != current_version:
                        previous = candidate
                        break
        return previous

    def status(self) -> Dict[str, Any]:
        installation = self.detect_installation()
        current = str(installation.get("version_number") or "")
        available = self._available_version
        with self._lock:
            operation = dict(self._operation) if self._operation else None
            running = bool(self._worker and self._worker.is_alive())
        rollback_version = self._find_rollback_version(current)
        return {
            "installation": installation,
            "installed_version": current,
            "available_version": available,
            "update_available": bool(
                current and available and _version_key(available) > _version_key(current)
            ),
            "checked_at": self._checked_at or None,
            "operation_running": running,
            "operation": operation,
            "rollback_version": rollback_version or None,
            "rollback_available": bool(
                rollback_version
                and installation.get("rollback_supported")
                and rollback_version != current
            ),
        }


_service_lock = threading.Lock()
_service: Optional[CodexUpgradeService] = None


def get_codex_upgrade_service() -> CodexUpgradeService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = CodexUpgradeService()
    return _service
