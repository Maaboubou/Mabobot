"""Managed storage and lookup for received and sent WeChat file messages.

The mabowx message object is only reliable while its UI control is alive, so
``wx_bot`` downloads files immediately and records the durable result here.
Quoted messages can then be resolved by filename without touching WeChat UI.
Successfully sent local files are archived here too, so their quotes use the
same managed input paths and remain usable after the originals are removed.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DOWNLOAD_ROOT = PROJECT_ROOT / "tmp" / "wechat_files"
DEFAULT_INDEX_PATH = PROJECT_ROOT / "data" / "wechat_file_index.sqlite3"
PROJECT_PATH_PREFIX = "project:///"

_INVALID_PATH_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_FILE_SIZE_LINE = re.compile(
    r"^\d+(?:\.\d+)?\s*(?:bytes?|[kmgt]i?b?|[kmgt])$",
    re.IGNORECASE,
)
_FILE_UI_MARKERS = {
    "文件",
    "[文件]",
    "未下载",
    "下载",
    "暂停下载",
    "微信电脑版",
    "wechat for windows",
}


def _configured_path(env_name: str, default: Path) -> Path:
    raw = str(os.getenv(env_name) or "").strip()
    if not raw:
        return default
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate


def safe_path_component(value: str, *, fallback: str, max_length: int = 80) -> str:
    """Return a Windows-safe component while retaining readable chat/file text."""
    cleaned = unicodedata.normalize("NFKC", str(value or "")).strip()
    cleaned = _INVALID_PATH_CHARS.sub("_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_length].rstrip(" .") or fallback


def safe_file_name(value: str, *, fallback: str = "wechat_file") -> str:
    raw = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    return safe_path_component(raw, fallback=fallback, max_length=180)


def normalize_file_name(value: str) -> str:
    name = safe_file_name(value, fallback="")
    return unicodedata.normalize("NFKC", name).casefold().strip()


def extract_file_name_from_message(content: Any) -> str:
    """Extract the filename from mabowx's multiline FileMessage content."""
    lines = [str(line or "").strip() for line in str(content or "").splitlines()]
    candidates = []
    for line in lines:
        if not line:
            continue
        lowered = line.casefold()
        if lowered in _FILE_UI_MARKERS or _FILE_SIZE_LINE.fullmatch(line):
            continue
        candidates.append(line)

    if not candidates:
        return ""
    # mabowx emits: 文件 / filename / size / 未下载 / 微信电脑版.
    # Prefer a value with an extension, while still supporting extensionless files.
    for candidate in candidates:
        if Path(candidate).suffix:
            return safe_file_name(candidate)
    return safe_file_name(candidates[0])


def quoted_file_name(quote_content: Any) -> str:
    """Normalize a quote preview into the filename that WeChat displayed."""
    lines = [str(line or "").strip() for line in str(quote_content or "").splitlines()]
    values = [line for line in lines if line and line.casefold() not in _FILE_UI_MARKERS]
    if len(values) != 1:
        return ""
    return safe_file_name(values[0], fallback="")


def looks_like_file_quote(quote_content: Any) -> bool:
    name = quoted_file_name(quote_content)
    if not name or len(name) > 180:
        return False
    return bool(Path(name).suffix)


def _safe_project_relative(value: str) -> PurePosixPath:
    text = str(value or "").strip()
    relative = PurePosixPath(text)
    if (
        not text
        or "\\" in text
        or relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
    ):
        raise ValueError(f"invalid project-relative file path: {value}")
    return relative


def local_path_from_external(
    value: str | os.PathLike[str],
    *,
    project_root: Path | None = None,
) -> Path:
    """Translate a portable, Windows or WSL path for the current project."""
    text = str(value or "").strip()
    root = Path(project_root or PROJECT_ROOT).resolve()
    if text.startswith(PROJECT_PATH_PREFIX):
        relative = _safe_project_relative(text[len(PROJECT_PATH_PREFIX) :])
        return (root / Path(*relative.parts)).resolve()
    if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", text):
        win_path = PureWindowsPath(text)
        drive = win_path.drive.rstrip(":").lower()
        return Path("/mnt") / drive / Path(*win_path.parts[1:])
    if os.name == "nt" and text.startswith("/mnt/"):
        parts = Path(text).parts
        if len(parts) >= 4 and len(parts[2]) == 1:
            return Path(f"{parts[2].upper()}:\\", *parts[3:])
    candidate = Path(text)
    if not candidate.is_absolute():
        return (root / candidate).resolve()
    return candidate


def portable_project_path(
    value: str | os.PathLike[str],
    *,
    project_root: Path | None = None,
) -> str:
    """Encode a project-owned path without binding it to one installation root."""
    text = str(value or "").strip()
    if not text:
        return ""
    root = Path(project_root or PROJECT_ROOT).resolve()
    path = local_path_from_external(text, project_root=root).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError:
        return str(path)
    return PROJECT_PATH_PREFIX + relative.as_posix()


def normalize_index_saved_paths(
    index_path: Path,
    *,
    project_root: Path | None = None,
    require_portable: bool = False,
) -> int:
    """Rewrite project-owned file rows to portable paths in one SQLite index."""
    path = Path(index_path)
    if not path.is_file():
        return 0
    updates = []
    nonportable = 0
    connection = sqlite3.connect(str(path), timeout=30)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbound_files'"
        ).fetchone()
        if table is None:
            return 0
        rows = connection.execute(
            "SELECT file_id, saved_path FROM inbound_files WHERE saved_path IS NOT NULL"
        ).fetchall()
        for file_id, saved_path in rows:
            encoded = portable_project_path(
                str(saved_path or ""),
                project_root=project_root,
            )
            if encoded and not encoded.startswith(PROJECT_PATH_PREFIX):
                nonportable += 1
            if encoded and encoded != saved_path:
                updates.append((encoded, str(file_id)))
        if updates:
            connection.executemany(
                "UPDATE inbound_files SET saved_path = ? WHERE file_id = ?",
                updates,
            )
        connection.commit()
    finally:
        connection.close()
    if require_portable and nonportable:
        raise ValueError(
            f"wechat file index contains {nonportable} path(s) outside the project"
        )
    return len(updates)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class FileResolution:
    status: str
    file_name: str = ""
    record: Optional[Dict[str, Any]] = None
    candidate_count: int = 0


class WeChatFileStore:
    """SQLite-backed index plus per-message download directories."""

    def __init__(
        self,
        *,
        download_root: Optional[Path] = None,
        index_path: Optional[Path] = None,
    ) -> None:
        self.download_root = Path(
            download_root
            or _configured_path("WECHAT_FILE_DOWNLOAD_ROOT", DEFAULT_DOWNLOAD_ROOT)
        )
        self.index_path = Path(
            index_path
            or _configured_path("WECHAT_FILE_INDEX_PATH", DEFAULT_INDEX_PATH)
        )
        self.download_root.mkdir(parents=True, exist_ok=True)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        normalize_index_saved_paths(self.index_path)
        self.cleanup_expired()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.index_path), timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inbound_files (
                    file_id TEXT PRIMARY KEY,
                    source_message_id TEXT,
                    chat_name TEXT NOT NULL,
                    sender TEXT,
                    original_filename TEXT NOT NULL,
                    normalized_filename TEXT NOT NULL,
                    saved_path TEXT,
                    received_at REAL NOT NULL,
                    file_size INTEGER,
                    sha256 TEXT,
                    status TEXT NOT NULL,
                    download_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_inbound_files_quote_lookup
                ON inbound_files(chat_name, normalized_filename, received_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_inbound_files_source_message
                ON inbound_files(source_message_id)
                """
            )

    def prepare_download_dir(
        self,
        *,
        chat_name: str,
        file_id: str,
        received_at: float,
    ) -> Path:
        chat_hash = hashlib.sha256(str(chat_name).encode("utf-8")).hexdigest()[:10]
        chat_part = safe_path_component(chat_name, fallback="chat", max_length=60)
        month = datetime.fromtimestamp(float(received_at)).strftime("%Y-%m")
        message_part = safe_path_component(file_id, fallback="file", max_length=80)
        target = self.download_root / f"{chat_part}--{chat_hash}" / month / message_part
        target.mkdir(parents=True, exist_ok=True)
        return target

    def record(
        self,
        *,
        file_id: str,
        source_message_id: str,
        chat_name: str,
        sender: str,
        original_filename: str,
        received_at: float,
        status: str,
        saved_path: Optional[str] = None,
        file_size: Optional[int] = None,
        sha256: Optional[str] = None,
        download_error: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = datetime.now().timestamp()
        name = safe_file_name(original_filename, fallback=f"{file_id}.bin")
        stored_path = portable_project_path(saved_path) if saved_path else None
        values = {
            "file_id": str(file_id),
            "source_message_id": str(source_message_id or ""),
            "chat_name": str(chat_name),
            "sender": str(sender or ""),
            "original_filename": name,
            "normalized_filename": normalize_file_name(name),
            "saved_path": stored_path,
            "received_at": float(received_at),
            "file_size": int(file_size) if file_size is not None else None,
            "sha256": str(sha256 or "") or None,
            "status": str(status),
            "download_error": str(download_error or "")[:1000] or None,
            "created_at": now,
            "updated_at": now,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO inbound_files (
                    file_id, source_message_id, chat_name, sender,
                    original_filename, normalized_filename, saved_path,
                    received_at, file_size, sha256, status, download_error,
                    created_at, updated_at
                ) VALUES (
                    :file_id, :source_message_id, :chat_name, :sender,
                    :original_filename, :normalized_filename, :saved_path,
                    :received_at, :file_size, :sha256, :status, :download_error,
                    :created_at, :updated_at
                )
                ON CONFLICT(file_id) DO UPDATE SET
                    source_message_id=excluded.source_message_id,
                    chat_name=excluded.chat_name,
                    sender=excluded.sender,
                    original_filename=excluded.original_filename,
                    normalized_filename=excluded.normalized_filename,
                    saved_path=excluded.saved_path,
                    received_at=excluded.received_at,
                    file_size=excluded.file_size,
                    sha256=excluded.sha256,
                    status=excluded.status,
                    download_error=excluded.download_error,
                    updated_at=excluded.updated_at
                """,
                values,
            )
        public_values = dict(values)
        if stored_path:
            public_values["saved_path"] = str(local_path_from_external(stored_path))
        return public_values

    def record_sent_file(
        self,
        *,
        chat_name: str,
        file_path: str | os.PathLike[str],
        sent_at: float,
    ) -> Dict[str, Any]:
        """Archive a local file only after WeChat confirms a successful send.

        Keep a separate snapshot in the managed input directory: generated
        outputs may be edited or cleaned up, and Codex accepts only managed
        attachments. No WeChat download or preview operation is needed.
        """
        source = local_path_from_external(file_path)
        if not source.is_file():
            raise FileNotFoundError(f"Sent file no longer exists: {source}")
        file_id = f"sent_file_{uuid.uuid4().hex}"
        directory = self.prepare_download_dir(
            chat_name=chat_name,
            file_id=file_id,
            received_at=sent_at,
        )
        snapshot = directory / safe_file_name(source.name)
        try:
            shutil.copy2(source, snapshot)
            return self.record(
                file_id=file_id,
                source_message_id=file_id,
                chat_name=chat_name,
                sender="self",
                original_filename=source.name,
                received_at=sent_at,
                status="ready",
                saved_path=str(snapshot),
                file_size=snapshot.stat().st_size,
                sha256=sha256_file(snapshot),
            )
        except Exception:
            snapshot.unlink(missing_ok=True)
            raise

    def resolve_quote(
        self,
        *,
        chat_name: str,
        quote_content: Any,
        before_timestamp: Optional[float] = None,
    ) -> FileResolution:
        name = quoted_file_name(quote_content)
        if not name:
            return FileResolution("not_file")
        normalized = normalize_file_name(name)
        upper_bound = float(before_timestamp or datetime.now().timestamp()) + 1.0
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM inbound_files
                WHERE chat_name = ?
                  AND normalized_filename = ?
                  AND received_at <= ?
                ORDER BY received_at DESC, updated_at DESC
                LIMIT 20
                """,
                (str(chat_name), normalized, upper_bound),
            ).fetchall()

        if not rows:
            return FileResolution(
                "not_found" if looks_like_file_quote(name) else "not_file",
                file_name=name,
            )

        latest = dict(rows[0])
        saved_path = latest.get("saved_path")
        if latest.get("status") == "ready" and saved_path:
            path = local_path_from_external(saved_path)
            if path.exists() and path.is_file():
                latest["saved_path"] = str(path.resolve())
                return FileResolution(
                    "ready",
                    file_name=name,
                    record=latest,
                    candidate_count=len(rows),
                )

        status = "missing" if latest.get("status") == "ready" else str(latest.get("status") or "failed")
        return FileResolution(
            status,
            file_name=name,
            record=latest,
            candidate_count=len(rows),
        )

    def cleanup_expired(self, *, retention_days: Optional[int] = None) -> int:
        """Remove expired indexed files, never deleting outside the managed root."""
        if retention_days is None:
            try:
                retention_days = int(os.getenv("WECHAT_FILE_RETENTION_DAYS") or 30)
            except (TypeError, ValueError):
                retention_days = 30
        if retention_days <= 0:
            return 0

        cutoff = time.time() - int(retention_days) * 24 * 60 * 60
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT file_id, saved_path FROM inbound_files WHERE received_at < ?",
                (cutoff,),
            ).fetchall()

        managed_root = self.download_root.resolve()
        expired_ids = []
        for row in rows:
            expired_ids.append(str(row["file_id"]))
            saved_path = row["saved_path"]
            if not saved_path:
                continue
            path = local_path_from_external(saved_path)
            try:
                resolved = path.resolve()
                resolved.relative_to(managed_root)
            except (OSError, ValueError):
                continue
            try:
                if resolved.is_file() and not resolved.is_symlink():
                    resolved.unlink()
            except OSError:
                continue

            parent = resolved.parent
            while parent != managed_root:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

        if expired_ids:
            placeholders = ",".join("?" for _ in expired_ids)
            with self._connect() as connection:
                connection.execute(
                    f"DELETE FROM inbound_files WHERE file_id IN ({placeholders})",
                    expired_ids,
                )
        return len(expired_ids)


_DEFAULT_STORE: Optional[WeChatFileStore] = None


def get_wechat_file_store() -> WeChatFileStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = WeChatFileStore()
    return _DEFAULT_STORE
