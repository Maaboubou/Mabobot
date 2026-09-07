"""Versioned project backup, validation and offline restore service.

Backup format v2 is the first mabowx-only generation. It intentionally rejects
all older archives and includes ``.env`` in plaintext because complete machine
migration is required before encrypted archives are introduced. Every manifest
and API response marks that fact prominently.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from app.services.wechat_file_store import normalize_index_saved_paths
from app.services.wsl_probe_guard import run_guarded_wsl_command
from app.version import APP_VERSION, BACKUP_FORMAT_VERSION, PLUGIN_RUNTIME_API_VERSION


logger = logging.getLogger(__name__)


class BackupError(RuntimeError):
    pass


class IncompatibleBackupError(BackupError):
    """Raised for archives from the retired pre-mabowx backup generation."""


@dataclass(frozen=True)
class BackupOptions:
    profile: str = "state"
    include_models: bool = False
    include_diagnostics: bool = False
    include_machine_bound: bool = False
    include_generated: bool = True

    def normalized(self) -> "BackupOptions":
        if self.profile not in {"state", "migration"}:
            raise BackupError("备份类型只能是 state 或 migration")
        return self


class BackupService:
    FORMAT_NAME = "mabobot-mabowx-backup"
    AUTOMATION_BACKEND = "mabowx"
    MANIFEST_NAME = "backup-manifest.json"
    PENDING_NAME = "pending_restore.json"
    ARCHIVE_SUFFIX = ".mabobot-backup.zip"

    # State archives contain mutable operator state only. Runtime manifests and
    # dependency files belong to migration archives so a state restore cannot
    # silently downgrade code after an application update.
    STATE_ROOT_FILES = {".env"}
    STATE_DATA_DIRECTORIES = {
        "chat_logs",
        "chat_archive",
        "chat_summaries",
        "chatbot_anchor_contexts",
        "codex_chat_scopes",
        "memory_backups",
        "plugins",
    }
    GENERATED_DATA_DIRECTORIES = {
        "daily_reports",
        "weekly_reports",
    }
    DIAGNOSTIC_NAMES = {
        "llm_call_history.jsonl",
    }
    MACHINE_BOUND_DATA_DIRECTORIES = {
        "chrome_profile",
        "asr_cache",
    }
    MIGRATION_CODE_ROOTS = {
        "app",
        "mabobot_launcher",
        "mabowx",
        "web",
        "scripts",
        "config",
    }
    MIGRATION_ROOT_FILES = {
        "start.py",
        "wx_bot.py",
        "wechat_auto_login.py",
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-file-tools.txt",
        ".env",
        ".env.example",
        "cookies.txt",
        "START.bat",
    }
    MIGRATION_GENERATED_ROOTS = {"mabowx文件下载"}
    PORTABLE_STORAGE_DEFAULTS = {
        "wechat_files": ("WECHAT_FILE_DOWNLOAD_ROOT", "tmp/wechat_files", "directory"),
        "wechat_file_index": ("WECHAT_FILE_INDEX_PATH", "data/wechat_file_index.sqlite3", "file"),
        "codex_chat_scopes": ("CODEX_CHAT_SCOPE_ROOT", "data/codex_chat_scopes", "directory"),
    }
    REQUIRED_MIGRATION_FILES = {
        "START.bat",
        "mabobot_launcher/__main__.py",
        "wx_bot.py",
        "mabowx/__init__.py",
        "mabowx/api/wechat.py",
    }
    REQUIRED_MIGRATION_PREFIXES = {"mabowx/selectors/": ".yaml"}
    PPT_MASTER_ARCHIVE_ROOT = PurePosixPath(".codex/skills/ppt-master")
    SKIP_PARTS = {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".git",
        ".venv",
        "node_modules",
    }
    SKIP_SUFFIXES = {".pyc", ".pyo", ".tmp", ".temp", ".log"}

    def __init__(
        self,
        project_root: Optional[Path] = None,
        backup_root: Optional[Path] = None,
        ppt_master_dir: Optional[Path] = None,
    ):
        self.project_root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        configured = backup_root or Path(os.getenv("SYSTEM_BACKUP_DIR") or "data/system_backups")
        if not configured.is_absolute():
            configured = self.project_root / configured
        self.backup_root = configured.resolve()
        self.incoming_root = self.backup_root / "incoming"
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.incoming_root.mkdir(parents=True, exist_ok=True)
        self.portable_storage: Dict[str, Dict[str, Any]] = {}
        self.portable_storage_errors: List[str] = []
        for storage_id, (env_name, default, kind) in self.PORTABLE_STORAGE_DEFAULTS.items():
            try:
                path, relative = self._resolve_portable_storage_path(
                    env_name,
                    default,
                    kind=kind,
                )
                self.portable_storage[storage_id] = {
                    "path": path,
                    "relative": relative,
                    "kind": kind,
                }
            except BackupError as exc:
                self.portable_storage_errors.append(str(exc))
        configured_skill = str(
            ppt_master_dir or os.getenv("SYSTEM_BACKUP_PPT_MASTER_DIR") or ""
        ).strip()
        self.ppt_master_dir: Optional[Path] = None
        self.ppt_master_config_error: Optional[str] = None
        self._ppt_master_requested = bool(configured_skill)
        if configured_skill:
            try:
                self.ppt_master_dir = self._resolve_ppt_master_dir(configured_skill)
            except BackupError as exc:
                self.ppt_master_config_error = str(exc)
        self._lock = threading.RLock()

    def _resolve_portable_storage_path(
        self,
        env_name: str,
        default: str,
        *,
        kind: str,
    ) -> Tuple[Path, str]:
        raw = str(os.getenv(env_name) or default).strip()
        candidate = Path(raw).expanduser()
        if (
            not raw
            or candidate.is_absolute()
            or raw.startswith(("~", "\\\\", "//"))
            or re.match(r"^[A-Za-z]:[\\/]", raw)
            or ".." in PurePosixPath(raw.replace("\\", "/")).parts
        ):
            raise BackupError(
                f"{env_name} 必须使用项目内相对路径，mabowx v2 备份不接受机器绝对路径"
            )
        resolved = (self.project_root / candidate).resolve()
        try:
            relative = resolved.relative_to(self.project_root).as_posix()
        except ValueError as exc:
            raise BackupError(f"{env_name} 必须位于项目目录内") from exc
        try:
            resolved.relative_to(self.backup_root)
        except ValueError:
            pass
        else:
            raise BackupError(f"{env_name} 不能指向系统备份目录")

        relative_path = PurePosixPath(relative)
        if kind == "file":
            if relative_path.parts[0] != "data" or len(relative_path.parts) < 2:
                raise BackupError(f"{env_name} 必须指向 data/ 下的文件")
        elif relative_path.parts[0] not in {"data", "tmp"}:
            raise BackupError(f"{env_name} 必须指向 data/ 或 tmp/ 下的目录")
        return resolved, relative

    def _require_portable_storage(self) -> None:
        if self.portable_storage_errors:
            raise BackupError("；".join(self.portable_storage_errors))

    @staticmethod
    def _resolve_ppt_master_dir(configured: str) -> Path:
        """Resolve the configured global PPT Master directory.

        ``wsl`` is a portable marker for the Codex installation used by this
        project: on Windows it resolves the default WSL user's Codex home; when
        the service itself runs in WSL it resolves the current Linux home.
        """
        if configured.strip().lower() != "wsl":
            return Path(configured).expanduser().resolve()

        if os.name != "nt":
            codex_home = Path(os.getenv("CODEX_HOME") or (Path.home() / ".codex"))
            return (codex_home / "skills" / "ppt-master").expanduser().resolve()

        try:
            locate = run_guarded_wsl_command(
                [
                    "wsl.exe",
                    "bash",
                    "-lic",
                    'printf "%s" "${CODEX_HOME:-$HOME/.codex}/skills/ppt-master"',
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
                runner=subprocess.run,
            )
            linux_path = (locate.stdout or "").strip()
            if locate.returncode != 0 or not linux_path:
                detail = (locate.stderr or locate.stdout or "无法定位 WSL Codex 目录").strip()
                raise BackupError(detail[-1000:])
            convert = run_guarded_wsl_command(
                ["wsl.exe", "wslpath", "-w", linux_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
                runner=subprocess.run,
            )
            windows_path = (convert.stdout or "").strip()
            if convert.returncode != 0 or not windows_path:
                detail = (convert.stderr or convert.stdout or "无法转换 WSL Codex 目录").strip()
                raise BackupError(detail[-1000:])
            return Path(windows_path).resolve()
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackupError(f"无法访问 WSL 中的 PPT Master 技能: {exc}") from exc

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _safe_archive_name(value: str) -> str:
        name = Path(str(value or "")).name
        if (
            name != value
            or not name.endswith(BackupService.ARCHIVE_SUFFIX)
            or re.fullmatch(r"[A-Za-z0-9._-]+", name) is None
        ):
            raise BackupError("备份文件名无效")
        return name

    def archive_path(self, name: str) -> Path:
        safe_name = self._safe_archive_name(name)
        for root in (self.backup_root, self.incoming_root):
            candidate = root / safe_name
            if candidate.is_file():
                return candidate
        raise BackupError("备份文件不存在")

    def _relative(self, path: Path) -> str:
        resolved = path.resolve()
        if self.ppt_master_dir is not None:
            try:
                skill_relative = resolved.relative_to(self.ppt_master_dir.resolve())
                return (self.PPT_MASTER_ARCHIVE_ROOT / skill_relative.as_posix()).as_posix()
            except ValueError:
                pass
        try:
            return resolved.relative_to(self.project_root).as_posix()
        except ValueError:
            pass
        raise BackupError(f"备份源不在允许的项目或 Codex 技能目录内: {path}")

    def _is_backup_internal(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.backup_root)
            return True
        except ValueError:
            return False

    def _skip_common(self, path: Path) -> bool:
        if self._is_backup_internal(path):
            return True
        relative = Path(self._relative(path))
        if any(part in self.SKIP_PARTS for part in relative.parts):
            return True
        return path.suffix.lower() in self.SKIP_SUFFIXES or path.name.endswith(("-wal", "-shm"))

    def _iter_tree(self, root: Path) -> Iterator[Path]:
        if not root.exists():
            return
        if root.is_file():
            if not self._skip_common(root):
                yield root
            return
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink() and not self._skip_common(path):
                yield path

    def _iter_managed_plugin_data(
        self,
        root: Path,
        *,
        include_machine_bound: bool,
        include_generated: bool,
    ) -> Iterator[Path]:
        """Back up only the durable namespace provided by PluginContext.

        Cache and temporary directories are intentionally lifecycle-managed
        runtime data.  Keeping this rule in the backup collector means every
        Runtime API v2 plugin inherits the same migration behaviour without
        needing plugin-specific backup code.
        """
        if not root.exists():
            return
        for path in root.iterdir():
            if path.is_file() and not path.is_symlink() and not self._skip_common(path):
                yield path
                continue
            if not path.is_dir():
                continue
            persistent = path / "persistent"
            if persistent.exists():
                yield from self._iter_tree(persistent)
            generated = path / "generated"
            if include_generated and generated.exists():
                yield from self._iter_tree(generated)
            machine_bound = path / "machine_bound"
            if include_machine_bound and machine_bound.exists():
                yield from self._iter_tree(machine_bound)

    def _iter_state_data(self, options: BackupOptions) -> Iterator[Path]:
        data_root = self.project_root / "data"
        if not data_root.exists():
            return
        for path in data_root.iterdir():
            if self._is_backup_internal(path):
                continue
            if path.name == "system_trash":
                continue
            if path.name == "models" and not options.include_models:
                continue
            if path.name in self.MACHINE_BOUND_DATA_DIRECTORIES and not options.include_machine_bound:
                continue
            if path.name in self.GENERATED_DATA_DIRECTORIES and not options.include_generated:
                continue
            if path.name.startswith("corrupt_telemetry") and not options.include_diagnostics:
                continue
            if path.is_dir():
                if options.profile == "state" and path.name not in (
                    self.STATE_DATA_DIRECTORIES
                    | self.GENERATED_DATA_DIRECTORIES
                    | ({"models"} if options.include_models else set())
                    | (self.MACHINE_BOUND_DATA_DIRECTORIES if options.include_machine_bound else set())
                    | ({path.name} if options.include_diagnostics and path.name.startswith("corrupt_telemetry") else set())
                ):
                    continue
                if path.name == "plugins":
                    yield from self._iter_managed_plugin_data(
                        path,
                        include_machine_bound=options.include_machine_bound,
                        include_generated=options.include_generated,
                    )
                    continue
                yield from self._iter_tree(path)
                continue
            if path.name in self.DIAGNOSTIC_NAMES and not options.include_diagnostics:
                continue
            if not self._skip_common(path):
                yield path

    def _iter_plugin_state_files(self) -> Iterator[Path]:
        plugins = self.project_root / "app" / "plugins"
        if not plugins.exists():
            return
        for name in ("routing_order.json",):
            path = plugins / name
            if path.is_file():
                yield path
        for path in plugins.glob("*/config.json"):
            if path.is_file():
                yield path

    def _iter_portable_storage(self) -> Iterator[Path]:
        for storage_id in ("wechat_files", "codex_chat_scopes"):
            entry = self.portable_storage[storage_id]
            yield from self._iter_tree(Path(entry["path"]))
        index = Path(self.portable_storage["wechat_file_index"]["path"])
        if index.is_file():
            yield index

    def collect_sources(self, options: BackupOptions) -> List[Path]:
        options = options.normalized()
        self._require_portable_storage()
        sources: Dict[str, Path] = {}

        def add(paths: Iterable[Path]) -> None:
            for path in paths:
                if path.is_file() and not self._skip_common(path):
                    sources[self._relative(path)] = path

        add(self.project_root / name for name in self.STATE_ROOT_FILES if (self.project_root / name).is_file())
        add(self._iter_state_data(options))
        add(self._iter_plugin_state_files())
        add(self._iter_portable_storage())

        if options.profile == "migration":
            add(self.project_root / name for name in self.MIGRATION_ROOT_FILES if (self.project_root / name).is_file())
            for name in self.MIGRATION_CODE_ROOTS:
                root = self.project_root / name
                if root.exists():
                    add(self._iter_tree(root))
            if options.include_generated:
                for name in self.MIGRATION_GENERATED_ROOTS:
                    root = self.project_root / name
                    if root.exists():
                        add(self._iter_tree(root))
            if self._ppt_master_requested:
                if self.ppt_master_config_error:
                    raise BackupError(
                        f"PPT Master 备份目录配置无效: {self.ppt_master_config_error}"
                    )
                skill_root = self.ppt_master_dir
                if skill_root is None or not (skill_root / "SKILL.md").is_file():
                    raise BackupError(
                        f"PPT Master 技能目录不存在或缺少 SKILL.md: {skill_root}"
                    )
                add(self._iter_tree(skill_root))

        return [sources[key] for key in sorted(sources)]

    @staticmethod
    def _is_sqlite(path: Path) -> bool:
        if path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"} or not path.is_file():
            return False
        try:
            with path.open("rb") as handle:
                return handle.read(16) == b"SQLite format 3\x00"
        except OSError:
            return False

    @staticmethod
    def _sqlite_snapshot(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
        source_connection = sqlite3.connect(source_uri, uri=True, timeout=30)
        target_connection = sqlite3.connect(str(destination), timeout=30)
        try:
            source_connection.execute("PRAGMA busy_timeout=30000")
            source_connection.backup(target_connection, pages=1024, sleep=0.01)
        finally:
            target_connection.close()
            source_connection.close()

    def _prepared_source(self, source: Path, staging: Path) -> Tuple[Path, str]:
        relative = self._relative(source)
        snapshot = staging / relative
        if self._is_sqlite(source):
            self._sqlite_snapshot(source, snapshot)
            index_entry = self.portable_storage.get("wechat_file_index") or {}
            index_path = index_entry.get("path")
            if index_path is not None and source.resolve() == Path(index_path).resolve():
                try:
                    normalize_index_saved_paths(
                        snapshot,
                        project_root=self.project_root,
                        require_portable=True,
                    )
                except (sqlite3.DatabaseError, ValueError) as exc:
                    raise BackupError(
                        "微信文件索引包含项目外路径，无法创建可迁移的 mabowx v2 备份"
                    ) from exc
                return snapshot, "sqlite_online_backup_portable_paths"
            return snapshot, "sqlite_online_backup"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, snapshot)
        return snapshot, "file_snapshot"

    def _classification(self, relative: str) -> Dict[str, Any]:
        relative_path = PurePosixPath(relative)
        wechat_files_root = str(
            (self.portable_storage.get("wechat_files") or {}).get("relative") or "tmp/wechat_files"
        ).rstrip("/")
        codex_scopes_root = str(
            (self.portable_storage.get("codex_chat_scopes") or {}).get("relative")
            or "data/codex_chat_scopes"
        ).rstrip("/")
        sensitive = (
            relative_path.name == ".env"
            or "cookie" in relative.lower()
            or "secret" in relative.lower()
            or relative == wechat_files_root
            or relative.startswith(wechat_files_root + "/")
            or relative == codex_scopes_root
            or relative.startswith(codex_scopes_root + "/")
        )
        parts = PurePosixPath(relative).parts
        machine_bound = relative.startswith(("data/chrome_profile/", "data/asr_cache/")) or (
            len(parts) >= 4
            and parts[:2] == ("data", "plugins")
            and parts[3] == "machine_bound"
        )
        generated = relative.startswith(
            (
                "data/weekly_reports/",
                "data/daily_reports/",
                "mabowx文件下载/",
            )
        ) or (
            len(parts) >= 4
            and parts[:2] == ("data", "plugins")
            and parts[3] == "generated"
        )
        owner = "core"
        if len(parts) >= 3 and parts[:2] == ("data", "plugins"):
            owner = f"plugin-storage:{parts[2]}"
        elif len(parts) >= 3 and parts[:2] == ("app", "plugins"):
            owner = f"plugin:{parts[2]}"
        elif parts[:3] == (".codex", "skills", "ppt-master"):
            owner = "codex-skill:ppt-master"
        elif parts[:1] == ("mabowx",):
            owner = "wechat-automation:mabowx"
        elif relative == wechat_files_root or relative.startswith(wechat_files_root + "/"):
            owner = "wechat-file-store"
        elif relative == codex_scopes_root or relative.startswith(codex_scopes_root + "/"):
            owner = "codex-chat-scope"
        return {
            "sensitive": sensitive,
            "portable": not machine_bound,
            "machine_bound": machine_bound,
            "generated": generated,
            "owner": owner,
        }

    def create_backup(self, options: BackupOptions, operation: Any = None) -> Dict[str, Any]:
        options = options.normalized()
        with self._lock:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            name = f"mabobot-mabowx-{options.profile}-{timestamp}{self.ARCHIVE_SUFFIX}"
            destination = self.backup_root / name
            collision = 1
            while destination.exists():
                name = (
                    f"mabobot-mabowx-{options.profile}-{timestamp}-{collision}"
                    f"{self.ARCHIVE_SUFFIX}"
                )
                destination = self.backup_root / name
                collision += 1
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            sources = self.collect_sources(options)
            if not sources:
                raise BackupError("没有找到可备份的数据")
            if operation:
                operation.progress(2, "已完成备份清单", files=len(sources))

            records: List[Dict[str, Any]] = []
            total_bytes = 0
            with tempfile.TemporaryDirectory(prefix="backup-stage-", dir=self.backup_root) as staging_name:
                staging = Path(staging_name)
                try:
                    archive_snapshot_names = {}
                    raw_archive = self.project_root / "data" / "chat_archive"
                    if (raw_archive / "index.sqlite3").exists():
                        from app.history.store import ArchiveStore
                        snapshot_root = staging / "chat-archive-snapshot"
                        ArchiveStore(raw_archive).snapshot_to(snapshot_root)
                        sources = [source for source in sources if not source.is_relative_to(raw_archive)]
                        for source in sorted(snapshot_root.rglob("*")):
                            if source.is_file():
                                sources.append(source)
                                archive_snapshot_names[source] = "data/chat_archive/" + source.relative_to(snapshot_root).as_posix()
                    with zipfile.ZipFile(
                        temporary,
                        "w",
                        compression=zipfile.ZIP_DEFLATED,
                        compresslevel=3,
                        allowZip64=True,
                    ) as archive:
                        for index, source in enumerate(sources, 1):
                            if operation:
                                operation.check_cancelled()
                            relative = archive_snapshot_names.get(source) or self._relative(source)
                            prepared, consistency = self._prepared_source(source, staging)
                            if source in archive_snapshot_names:
                                consistency = "archive_committed_prefix"
                            size = prepared.stat().st_size
                            classification = self._classification(relative)
                            record = {
                                "path": relative,
                                "bytes": size,
                                "sha256": self._sha256(prepared),
                                "consistency": consistency,
                                **classification,
                            }
                            archive.write(prepared, relative)
                            records.append(record)
                            total_bytes += size
                            if operation and (index == len(sources) or index % 20 == 0):
                                operation.progress(
                                    5 + int(index / len(sources) * 85),
                                    f"正在打包 {index}/{len(sources)}",
                                    current=relative,
                                    bytes=total_bytes,
                                )

                        manifest = {
                            "format": self.FORMAT_NAME,
                            "format_version": BACKUP_FORMAT_VERSION,
                            "generation": "mabowx-v2",
                            "automation": {
                                "backend": self.AUTOMATION_BACKEND,
                                "bundled": True,
                            },
                            "created_at": self._utc_now(),
                            "app_version": APP_VERSION,
                            "plugin_runtime_api_version": PLUGIN_RUNTIME_API_VERSION,
                            "profile": options.profile,
                            "storage_layout": {
                                storage_id: str(entry["relative"])
                                for storage_id, entry in sorted(self.portable_storage.items())
                            },
                            "options": {
                                "include_models": options.include_models,
                                "include_diagnostics": options.include_diagnostics,
                                "include_machine_bound": options.include_machine_bound,
                                "include_generated": options.include_generated,
                            },
                            "security": {
                                "encrypted": False,
                                "contains_plaintext_env": any(
                                    PurePosixPath(item["path"]).name == ".env"
                                    for item in records
                                ),
                                "warning": (
                                    "此备份格式未加密；.env、Cookie、聊天附件、"
                                    "Codex 工作区和插件密钥均以明文保存。"
                                ),
                            },
                            "consistency": {
                                "mode": "per_file_snapshot",
                                "sqlite_online_backup": True,
                                "wechat_file_paths_portable": True,
                            },
                            "file_count": len(records),
                            "total_bytes": total_bytes,
                            "files": records,
                        }
                        archive.writestr(
                            self.MANIFEST_NAME,
                            json.dumps(manifest, ensure_ascii=False, indent=2),
                        )
                    os.replace(temporary, destination)
                finally:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass

            if operation:
                operation.progress(98, "正在校验迁移包")
            validation = self.validate_archive(destination, verify_files=False)
            result = {
                "name": name,
                "path": str(destination),
                "profile": options.profile,
                "bytes": destination.stat().st_size,
                "file_count": len(records),
                "created_at": manifest["created_at"],
                "security": manifest["security"],
                "valid": validation["valid"],
            }
            try:
                from app.services.runtime_operations import get_runtime_operation_service

                get_runtime_operation_service().record_audit(
                    category="backup",
                    action="create_backup",
                    target=name,
                    summary="创建状态备份" if options.profile == "state" else "创建完整迁移包",
                    after={"profile": options.profile, "bytes": result["bytes"], "file_count": len(records)},
                    details={"contains_plaintext_env": manifest["security"]["contains_plaintext_env"]},
                )
            except Exception:
                pass
            return result

    @staticmethod
    def _safe_member(member: str) -> str:
        member = str(member or "")
        path = PurePosixPath(member)
        if (
            "\\" in member
            or "\x00" in member
            or path.is_absolute()
            or not path.parts
            or any(
                part in {"", ".", ".."} or ":" in part
                for part in path.parts
            )
        ):
            raise BackupError(f"迁移包包含不安全路径: {member}")
        return path.as_posix()

    @staticmethod
    def _path_is_within(relative: str, root: str) -> bool:
        normalized_root = str(root or "").rstrip("/")
        return relative == normalized_root or relative.startswith(normalized_root + "/")

    def _validate_storage_layout(self, payload: Any) -> Dict[str, str]:
        if not isinstance(payload, dict):
            raise BackupError("mabowx v2 备份缺少存储布局")
        required = set(self.PORTABLE_STORAGE_DEFAULTS)
        if set(payload) != required:
            raise BackupError("mabowx v2 备份的存储布局不完整")
        layout = {key: self._safe_member(str(payload[key])) for key in sorted(required)}
        wechat_parts = PurePosixPath(layout["wechat_files"]).parts
        index_parts = PurePosixPath(layout["wechat_file_index"]).parts
        scope_parts = PurePosixPath(layout["codex_chat_scopes"]).parts
        if wechat_parts[0] not in {"data", "tmp"}:
            raise BackupError("微信文件目录必须位于 data/ 或 tmp/ 下")
        if len(index_parts) < 2 or index_parts[0] != "data":
            raise BackupError("微信文件索引必须位于 data/ 下")
        if len(scope_parts) < 2 or scope_parts[0] != "data":
            raise BackupError("Codex 聊天目录必须位于 data/ 下")
        protected = {"data/system_backups", "data/system_trash"}
        if any(
            any(self._path_is_within(value, root) for root in protected)
            for value in layout.values()
        ):
            raise BackupError("mabowx v2 存储布局指向受保护目录")
        return layout

    def _is_plugin_state_member(self, relative: str) -> bool:
        path = PurePosixPath(relative)
        if relative == "app/plugins/routing_order.json":
            return True
        return (
            len(path.parts) == 4
            and path.parts[:2] == ("app", "plugins")
            and path.parts[-1] == "config.json"
        )

    def _is_allowed_manifest_member(
        self,
        relative: str,
        *,
        profile: str,
        options: Dict[str, Any],
        storage_layout: Dict[str, str],
    ) -> bool:
        if relative == self.MANIFEST_NAME:
            return False
        if relative in self.STATE_ROOT_FILES or self._is_plugin_state_member(relative):
            return True
        if any(self._path_is_within(relative, root) for root in storage_layout.values()):
            return True

        path = PurePosixPath(relative)
        parts = path.parts
        if parts and parts[0] == "data":
            if any(
                self._path_is_within(relative, root)
                for root in {"data/system_backups", "data/system_trash"}
            ):
                return False
            if profile == "migration":
                if self._path_is_within(relative, "data/models"):
                    return bool(options.get("include_models"))
                if any(
                    self._path_is_within(relative, f"data/{name}")
                    for name in self.MACHINE_BOUND_DATA_DIRECTORIES
                ):
                    return bool(options.get("include_machine_bound"))
                if any(
                    self._path_is_within(relative, f"data/{name}")
                    for name in self.GENERATED_DATA_DIRECTORIES
                ):
                    return bool(options.get("include_generated", True))
                return True

            if len(parts) == 2:
                if parts[-1] in self.DIAGNOSTIC_NAMES:
                    return bool(options.get("include_diagnostics"))
                return True
            allowed_directories = set(self.STATE_DATA_DIRECTORIES)
            if options.get("include_generated", True):
                allowed_directories.update(self.GENERATED_DATA_DIRECTORIES)
            if options.get("include_models"):
                allowed_directories.add("models")
            if options.get("include_machine_bound"):
                allowed_directories.update(self.MACHINE_BOUND_DATA_DIRECTORIES)
            if options.get("include_diagnostics") and parts[1].startswith("corrupt_telemetry"):
                return True
            return len(parts) >= 2 and parts[1] in allowed_directories

        if profile != "migration":
            return False
        if relative in self.MIGRATION_ROOT_FILES:
            return True
        if any(self._path_is_within(relative, root) for root in self.MIGRATION_CODE_ROOTS):
            return True
        if options.get("include_generated", True) and any(
            self._path_is_within(relative, root)
            for root in self.MIGRATION_GENERATED_ROOTS
        ):
            return True
        return self._path_is_within(relative, self.PPT_MASTER_ARCHIVE_ROOT.as_posix())

    def _validate_manifest_contract(self, payload: Dict[str, Any]) -> None:
        profile = str(payload.get("profile") or "")
        if profile not in {"state", "migration"}:
            raise BackupError("mabowx v2 备份类型无效")
        options = payload.get("options")
        if not isinstance(options, dict):
            raise BackupError("mabowx v2 备份缺少选项清单")
        option_names = {
            "include_models",
            "include_diagnostics",
            "include_machine_bound",
            "include_generated",
        }
        if not option_names.issubset(options) or any(
            not isinstance(options[name], bool) for name in option_names
        ):
            raise BackupError("mabowx v2 备份选项清单无效")
        storage_layout = self._validate_storage_layout(payload.get("storage_layout"))
        files = payload.get("files")
        if not isinstance(files, list) or not files:
            raise BackupError("mabowx v2 备份文件清单为空")

        seen: Set[str] = set()
        for record in files:
            if not isinstance(record, dict):
                raise BackupError("mabowx v2 备份文件记录无效")
            relative = self._safe_member(str(record.get("path") or ""))
            if relative in seen:
                raise BackupError(f"mabowx v2 备份包含重复路径: {relative}")
            seen.add(relative)
            if not self._is_allowed_manifest_member(
                relative,
                profile=profile,
                options=options,
                storage_layout=storage_layout,
            ):
                raise BackupError(f"mabowx v2 备份包含越界文件: {relative}")
            try:
                byte_count = int(record.get("bytes"))
            except (TypeError, ValueError) as exc:
                raise BackupError(f"mabowx v2 文件大小无效: {relative}") from exc
            if byte_count < 0 or re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256") or "")) is None:
                raise BackupError(f"mabowx v2 文件校验记录无效: {relative}")

        try:
            declared_file_count = int(payload.get("file_count"))
        except (TypeError, ValueError) as exc:
            raise BackupError("mabowx v2 备份文件数量无效") from exc
        if declared_file_count != len(files):
            raise BackupError("mabowx v2 备份文件数量与清单不一致")
        try:
            declared_total_bytes = int(payload.get("total_bytes"))
        except (TypeError, ValueError) as exc:
            raise BackupError("mabowx v2 备份总大小无效") from exc
        if declared_total_bytes < 0:
            raise BackupError("mabowx v2 备份总大小无效")
        if profile == "migration":
            missing = sorted(self.REQUIRED_MIGRATION_FILES - seen)
            if missing:
                raise BackupError("mabowx 完整迁移包缺少必要代码: " + "、".join(missing))
            for prefix, suffix in self.REQUIRED_MIGRATION_PREFIXES.items():
                if not any(path.startswith(prefix) and path.endswith(suffix) for path in seen):
                    raise BackupError(f"mabowx 完整迁移包缺少必要资源: {prefix}*{suffix}")

    def read_manifest(self, archive_path: Path) -> Dict[str, Any]:
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                payload = json.loads(archive.read(self.MANIFEST_NAME).decode("utf-8"))
        except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError(f"无法读取备份清单: {exc}") from exc
        if payload.get("format") != self.FORMAT_NAME:
            raise IncompatibleBackupError("旧备份格式已停用；当前只接受 mabowx v2 备份")
        try:
            format_version = int(payload.get("format_version") or 0)
        except (TypeError, ValueError) as exc:
            raise IncompatibleBackupError("旧备份格式已停用；当前只接受 mabowx v2 备份") from exc
        if format_version != BACKUP_FORMAT_VERSION:
            raise IncompatibleBackupError("旧备份格式已停用；当前只接受 mabowx v2 备份")
        automation = payload.get("automation")
        if (
            payload.get("generation") != "mabowx-v2"
            or not isinstance(automation, dict)
            or automation.get("backend") != self.AUTOMATION_BACKEND
            or automation.get("bundled") is not True
        ):
            raise IncompatibleBackupError("备份不属于当前 mabowx 自动化代际")
        self._validate_manifest_contract(payload)
        return payload

    def validate_archive(self, archive_path: Path, *, verify_files: bool = True, operation: Any = None) -> Dict[str, Any]:
        manifest = self.read_manifest(archive_path)
        expected = {str(item.get("path")): item for item in manifest.get("files", [])}
        errors: List[str] = []
        checked = 0
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                member_rows = [
                    (self._safe_member(info.filename), info)
                    for info in archive.infolist()
                ]
                member_names = [name for name, _info in member_rows]
                if len(member_names) != len(set(member_names)):
                    errors.append("迁移包包含重复 ZIP 条目")
                members = {name: info for name, info in member_rows}
                payload_names = set(members) - {self.MANIFEST_NAME}
                extra_names = sorted(payload_names - set(expected))
                if extra_names:
                    errors.append("迁移包包含未登记文件: " + "、".join(extra_names[:5]))
                for relative, record in expected.items():
                    self._safe_member(relative)
                    if relative not in members:
                        errors.append(f"缺少文件: {relative}")
                        continue
                    recorded_bytes = record.get("bytes")
                    if recorded_bytes is None or int(recorded_bytes) != members[relative].file_size:
                        errors.append(f"文件大小不匹配: {relative}")
                        continue
                    if verify_files:
                        digest = hashlib.sha256()
                        with archive.open(relative) as handle:
                            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                                digest.update(chunk)
                        if digest.hexdigest() != record.get("sha256"):
                            errors.append(f"校验和不匹配: {relative}")
                    checked += 1
                    if operation and checked % 20 == 0:
                        operation.progress(
                            min(95, int(checked / max(len(expected), 1) * 95)),
                            f"正在校验 {checked}/{len(expected)}",
                        )
                expected_total = sum(int(record.get("bytes") or 0) for record in expected.values())
                if int(manifest.get("total_bytes") or -1) != expected_total:
                    errors.append("迁移包总大小与清单不一致")
        except (OSError, zipfile.BadZipFile, BackupError) as exc:
            errors.append(str(exc))
        return {
            "valid": not errors,
            "errors": errors,
            "checked_files": checked,
            "manifest": {key: value for key, value in manifest.items() if key != "files"},
            "security_warning": (manifest.get("security") or {}).get("warning"),
        }

    def list_backups(self) -> List[Dict[str, Any]]:
        rows = []
        for source, imported in ((self.backup_root, False), (self.incoming_root, True)):
            for path in source.glob(f"*{self.ARCHIVE_SUFFIX}"):
                try:
                    manifest = self.read_manifest(path)
                    validation = self.validate_archive(path, verify_files=False)
                    rows.append(
                        {
                            "name": path.name,
                            "bytes": path.stat().st_size,
                            "created_at": manifest.get("created_at"),
                            "profile": manifest.get("profile"),
                            "app_version": manifest.get("app_version"),
                            "generation": manifest.get("generation"),
                            "automation_backend": (manifest.get("automation") or {}).get("backend"),
                            "file_count": manifest.get("file_count"),
                            "encrypted": bool((manifest.get("security") or {}).get("encrypted")),
                            "contains_plaintext_env": bool((manifest.get("security") or {}).get("contains_plaintext_env")),
                            "imported": imported,
                            "valid": validation["valid"],
                            "error": "；".join(validation["errors"][:3]) if validation["errors"] else None,
                        }
                    )
                except IncompatibleBackupError:
                    # The mabowx generation is a hard cut. Retired archives stay
                    # on disk but are intentionally absent from the current UI.
                    continue
                except BackupError as exc:
                    rows.append(
                        {
                            "name": path.name,
                            "bytes": path.stat().st_size,
                            "imported": imported,
                            "valid": False,
                            "error": str(exc),
                        }
                    )
        rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return rows

    def import_path(self, filename: str) -> Path:
        safe = self._safe_archive_name(filename)
        return self.incoming_root / safe

    def pending_path(self) -> Path:
        return self.backup_root / self.PENDING_NAME

    def delete_archive(self, archive_name: str, *, confirmation: str) -> Dict[str, Any]:
        """Permanently remove one managed backup archive.

        A backup referenced by the pending offline-restore plan is protected so
        the next application start cannot be left with a broken restore target.
        """
        if confirmation.strip() != "删除备份":
            raise BackupError("请输入“删除备份”确认操作")

        with self._lock:
            archive = self.archive_path(archive_name)
            pending_path = self.pending_path()
            if pending_path.exists():
                try:
                    pending = json.loads(pending_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise BackupError("待恢复计划无法读取，已阻止删除备份") from exc
                pending_name = str(pending.get("archive_name") or "")
                if not pending_name:
                    pending_name = Path(str(pending.get("archive") or "")).name
                if pending_name == archive.name:
                    raise BackupError("该备份正在用于待执行的恢复计划，不能删除")

            imported = archive.parent == self.incoming_root
            size = archive.stat().st_size
            profile = None
            try:
                profile = self.read_manifest(archive).get("profile")
            except BackupError:
                # Invalid archives must remain deletable from the management UI.
                pass
            archive.unlink()

        result = {
            "deleted": True,
            "name": archive.name,
            "bytes": size,
            "profile": profile,
            "imported": imported,
        }
        try:
            from app.services.runtime_operations import get_runtime_operation_service

            get_runtime_operation_service().record_audit(
                category="backup",
                action="delete_backup",
                target=archive.name,
                summary="永久删除备份",
                before={
                    "profile": profile,
                    "bytes": size,
                    "imported": imported,
                },
                after={"exists": False},
            )
        except Exception:
            pass
        return result

    def prepare_restore(self, archive_name: str, *, confirmation: str) -> Dict[str, Any]:
        if confirmation.strip() != "恢复备份":
            raise BackupError("请输入“恢复备份”确认操作")
        archive = self.archive_path(archive_name)
        validation = self.validate_archive(archive, verify_files=True)
        if not validation["valid"]:
            raise BackupError("备份校验失败：" + "；".join(validation["errors"][:5]))
        self._preflight_restore_targets(self.read_manifest(archive))
        pending = {
            "schema_version": 2,
            "backup_format_version": BACKUP_FORMAT_VERSION,
            "automation_backend": self.AUTOMATION_BACKEND,
            "archive": str(archive),
            "archive_name": archive.name,
            "prepared_at": self._utc_now(),
            "security_warning": validation.get("security_warning"),
        }
        temporary = self.pending_path().with_suffix(".tmp")
        temporary.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.pending_path())
        result = {
            "prepared": True,
            "archive_name": archive.name,
            "restart_required": True,
            "message": "恢复计划已创建；下一次启动将在加载应用前恢复。",
        }
        try:
            from app.services.runtime_operations import get_runtime_operation_service

            get_runtime_operation_service().record_audit(
                category="backup",
                action="prepare_restore",
                target=archive.name,
                summary="已创建离线恢复计划",
                details={"restart_required": True},
            )
        except Exception:
            pass
        return result

    def _extract_verified(self, archive_path: Path, destination: Path) -> Tuple[Dict[str, Any], List[str]]:
        validation = self.validate_archive(archive_path, verify_files=True)
        if not validation["valid"]:
            raise BackupError("备份校验失败：" + "；".join(validation["errors"][:5]))
        manifest = self.read_manifest(archive_path)
        restored: List[str] = []
        with zipfile.ZipFile(archive_path, "r") as archive:
            for record in manifest.get("files", []):
                relative = self._safe_member(str(record["path"]))
                target = destination / Path(*PurePosixPath(relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(relative) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                restored.append(relative)
        return manifest, restored

    def _restore_target(self, relative: str) -> Path:
        member = PurePosixPath(self._safe_member(relative))
        prefix = self.PPT_MASTER_ARCHIVE_ROOT.parts
        if member.parts[: len(prefix)] == prefix:
            if self.ppt_master_dir is None:
                detail = self.ppt_master_config_error or "未配置 SYSTEM_BACKUP_PPT_MASTER_DIR"
                raise BackupError(f"无法恢复 PPT Master 技能: {detail}")
            suffix = member.parts[len(prefix) :]
            if not suffix:
                raise BackupError("PPT Master 恢复条目缺少文件路径")
            skill_root = self.ppt_master_dir.resolve()
            target = (skill_root / Path(*suffix)).resolve()
            try:
                target.relative_to(skill_root)
            except ValueError as exc:
                raise BackupError(f"PPT Master 恢复目标越界: {relative}") from exc
            return target

        target = (self.project_root / Path(*member.parts)).resolve()
        try:
            target.relative_to(self.project_root)
        except ValueError as exc:
            raise BackupError(f"恢复目标越过项目目录: {relative}") from exc
        return target

    def _preflight_restore_targets(self, manifest: Dict[str, Any]) -> None:
        for record in manifest.get("files", []):
            self._restore_target(str(record.get("path") or ""))

    def _validate_staged_payload(
        self,
        manifest: Dict[str, Any],
        stage: Path,
    ) -> None:
        records = {
            str(record["path"]): record
            for record in manifest.get("files", [])
        }
        for relative, record in records.items():
            path = stage / Path(*PurePosixPath(relative).parts)
            consistency = str(record.get("consistency") or "")
            if consistency.startswith("sqlite_online_backup"):
                connection = None
                try:
                    connection = sqlite3.connect(str(path), timeout=30)
                    quick_check = connection.execute("PRAGMA quick_check").fetchone()
                except sqlite3.DatabaseError as exc:
                    raise BackupError(f"SQLite 恢复快照不可读: {relative}: {exc}") from exc
                finally:
                    if connection is not None:
                        connection.close()
                if not quick_check or str(quick_check[0]).lower() != "ok":
                    raise BackupError(f"SQLite 恢复快照校验失败: {relative}")

        if manifest.get("profile") != "migration":
            return
        for relative in sorted(self.REQUIRED_MIGRATION_FILES):
            if not relative.endswith(".py"):
                continue
            path = stage / Path(*PurePosixPath(relative).parts)
            try:
                compile(path.read_bytes(), relative, "exec")
            except (OSError, SyntaxError, ValueError) as exc:
                raise BackupError(f"mabowx 必要代码无法加载: {relative}: {exc}") from exc

    def _migration_stale_files(self, archived: Set[str]) -> List[Path]:
        stale: Dict[str, Path] = {}
        for root_name in sorted(self.MIGRATION_CODE_ROOTS):
            root = self.project_root / root_name
            if not root.exists():
                continue
            for path in self._iter_tree(root):
                relative = self._relative(path)
                if relative not in archived:
                    stale[relative] = path
        return [stale[key] for key in sorted(stale)]

    def restore_archive(self, archive_path: Path, *, create_safety_backup: bool = True) -> Dict[str, Any]:
        """Restore an archive while the application is stopped.

        Files are fully extracted and verified before any project path changes.
        A file-level rollback directory protects the current installation if an
        overwrite fails midway.
        """
        archive_path = archive_path.resolve()
        with self._lock, tempfile.TemporaryDirectory(prefix="restore-stage-", dir=self.backup_root) as stage_name:
            stage = Path(stage_name) / "payload"
            rollback = Path(stage_name) / "rollback"
            stage.mkdir(parents=True)
            manifest, relatives = self._extract_verified(archive_path, stage)
            self._preflight_restore_targets(manifest)
            self._validate_staged_payload(manifest, stage)

            safety_backup = None
            if create_safety_backup:
                manifest_options = manifest.get("options") or {}
                safety_backup = self.create_backup(
                    BackupOptions(
                        profile=(
                            "migration"
                            if manifest.get("profile") == "migration"
                            else "state"
                        ),
                        include_models=bool(manifest_options.get("include_models")),
                        include_diagnostics=bool(manifest_options.get("include_diagnostics")),
                        include_machine_bound=bool(manifest_options.get("include_machine_bound")),
                        include_generated=bool(manifest_options.get("include_generated", True)),
                    ),
                )

            existing: List[str] = []
            created: List[str] = []
            applied: List[str] = []
            removed: List[str] = []
            try:
                if "data/chat_archive/snapshot.json" in relatives:
                    # A journal snapshot replaces the archive as a unit. A
                    # later index/WAL or extra journal shard must not survive
                    # restoration of an earlier committed prefix.
                    archive_root = self.project_root / "data" / "chat_archive"
                    archived_paths = set(relatives)
                    for stale_path in sorted(archive_root.rglob("*")):
                        if stale_path.is_symlink():
                            raise BackupError("聊天档案目录包含符号链接，无法安全恢复")
                        if not stale_path.is_file():
                            continue
                        relative = self._relative(stale_path)
                        if relative not in archived_paths:
                            previous = rollback / Path(*PurePosixPath(relative).parts)
                            previous.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(stale_path, previous)
                            stale_path.unlink()
                            removed.append(relative)
                if manifest.get("profile") == "migration":
                    for stale_path in self._migration_stale_files(set(relatives)):
                        relative = self._relative(stale_path)
                        previous = rollback / Path(*PurePosixPath(relative).parts)
                        previous.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(stale_path, previous)
                        stale_path.unlink()
                        removed.append(relative)
                for relative in relatives:
                    source = stage / Path(*PurePosixPath(relative).parts)
                    target = self._restore_target(relative)
                    if target.exists():
                        previous = rollback / Path(*PurePosixPath(relative).parts)
                        previous.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, previous)
                        existing.append(relative)
                    else:
                        created.append(relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(f".{target.name}.restore.tmp")
                    shutil.copy2(source, temporary)
                    os.replace(temporary, target)
                    applied.append(relative)
            except Exception:
                for relative in reversed(applied):
                    target = self._restore_target(relative)
                    previous = rollback / Path(*PurePosixPath(relative).parts)
                    if relative in existing and previous.exists():
                        shutil.copy2(previous, target)
                    elif relative in created:
                        try:
                            target.unlink(missing_ok=True)
                        except OSError:
                            pass
                for relative in removed:
                    previous = rollback / Path(*PurePosixPath(relative).parts)
                    target = self.project_root / Path(*PurePosixPath(relative).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(previous, target)
                raise

            return {
                "restored": True,
                "archive": archive_path.name,
                "profile": manifest.get("profile"),
                "files": len(applied),
                "removed_stale_files": len(removed),
                "safety_backup": safety_backup,
                "security_warning": (manifest.get("security") or {}).get("warning"),
                "restored_at": self._utc_now(),
            }

    def apply_pending_restore(self) -> Optional[Dict[str, Any]]:
        pending_path = self.pending_path()
        if not pending_path.exists():
            return None
        try:
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            if (
                int(pending.get("schema_version") or 0) != 2
                or int(pending.get("backup_format_version") or 0) != BACKUP_FORMAT_VERSION
                or pending.get("automation_backend") != self.AUTOMATION_BACKEND
            ):
                raise IncompatibleBackupError("旧恢复计划已停用；请用 mabowx v2 备份重新创建")
            archive = Path(str(pending.get("archive") or ""))
            if not archive.is_file():
                raise BackupError("待恢复的备份文件不存在")
            result = self.restore_archive(archive, create_safety_backup=True)
            completed = self.backup_root / f"restore-applied-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
            completed.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            pending_path.unlink()
            return result
        except Exception as exc:
            failed = dict(pending if "pending" in locals() else {})
            failed.update({"failed_at": self._utc_now(), "error": str(exc)})
            failed_path = self.backup_root / f"restore-failed-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
            failed_path.write_text(json.dumps(failed, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                pending_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def overview(self) -> Dict[str, Any]:
        pending = None
        if self.pending_path().exists():
            try:
                pending = json.loads(self.pending_path().read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pending = {"invalid": True}
        backups = self.list_backups()
        return {
            "format_version": BACKUP_FORMAT_VERSION,
            "generation": "mabowx-v2",
            "automation_backend": self.AUTOMATION_BACKEND,
            "backup_directory": str(self.backup_root),
            "security": {
                "encrypted": False,
                "env_included": True,
                "warning": "当前备份格式未加密；.env、聊天附件和 Codex 工作区应按敏感文件保管。",
                "encryption_planned": True,
            },
            "pending_restore": pending,
            "backups": backups,
            "counts": {
                "total": len(backups),
                "valid": sum(bool(item.get("valid")) for item in backups),
                "migration": sum(item.get("profile") == "migration" for item in backups),
            },
            "profiles": [
                {"id": "state", "title": "状态备份", "description": "数据库、配置、聊天、附件与插件状态"},
                {"id": "migration", "title": "完整迁移", "description": "状态、mabowx 当前代码、插件、Codex 技能和生成内容"},
            ],
        }


_service: Optional[BackupService] = None
_service_lock = threading.Lock()


def get_backup_service() -> BackupService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = BackupService()
    return _service


def apply_pending_restore(project_root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    return BackupService(project_root=project_root).apply_pending_restore()
