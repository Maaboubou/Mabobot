"""Process-neutral runtime logging configuration and JSONL formatting."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator


LOG_PROFILES = {
    "app": ("logs/app.jsonl", 20 * 1024 * 1024, 5, 30),
    "wx_bot": ("logs/wx_bot.jsonl", 20 * 1024 * 1024, 5, 30),
    "mabowx": ("mabowx_logs/mabowx.jsonl", 10 * 1024 * 1024, 6, 14),
    "launcher": ("logs/launcher.jsonl", 3 * 1024 * 1024, 2, 14),
    "auto_login": ("logs/wechat_auto_login.jsonl", 2 * 1024 * 1024, 2, 14),
    "dashboard_events": ("logs/dashboard_events.jsonl", 10 * 1024 * 1024, 4, 30),
}
ROOT = Path(__file__).resolve().parent
_context: ContextVar[dict[str, str]] = ContextVar("mabobot_log_context", default={})
_EXTRA_FIELDS = (
    "trace_id", "delivery_id", "message_id", "request_id", "chat",
    "attempt", "duration_ms", "outcome", "error_code", "source",
)


def log_path(service: str) -> Path:
    return ROOT / LOG_PROFILES[service][0]


def write_log_path(service: str) -> Path:
    path = log_path(service)
    if service == "app" and os.getenv("MABOBOT_WEB_MULTI_WORKER") == "1":
        return path.with_name(f"app.{os.getpid()}.jsonl")
    return path


def configured_level(value: str | int | None = None) -> int:
    raw = value if value is not None else os.getenv("LOG_LEVEL", "INFO")
    if isinstance(raw, int):
        return raw
    name = str(raw).upper().strip()
    if name not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError(f"Invalid LOG_LEVEL: {raw!r}")
    return int(getattr(logging, name))


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    next_context = dict(_context.get())
    next_context.update({key: str(value) for key, value in fields.items() if value is not None})
    token = _context.set(next_context)
    try:
        yield
    finally:
        _context.reset(token)


def current_log_context() -> dict[str, str]:
    return dict(_context.get())


class JsonLineFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "event": getattr(record, "event", "unclassified"),
            "message": record.getMessage(),
            "pid": record.process,
            "thread": record.thread,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        context = _context.get()
        for key in _EXTRA_FIELDS:
            value = getattr(record, key, context.get(key))
            if value is not None and value != "":
                data[key] = value
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            data["stack"] = self.formatStack(record.stack_info)
        return json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))


class ManagedRotatingFileHandler(RotatingFileHandler):
    """Bounded file sink that retries Windows rename locks and prunes old backups."""

    def __init__(self, filename: str | Path, *, max_bytes: int, backup_count: int,
                 max_age_days: int, retry_seconds: float = 60.0):
        self.max_age_days = max(1, int(max_age_days))
        self.retry_seconds = max(1.0, float(retry_seconds))
        self._retry_at = 0.0
        self._last_prune = 0.0
        self.rollover_pending = False
        super().__init__(filename, mode="a", maxBytes=max_bytes, backupCount=backup_count,
                         encoding="utf-8", delay=True)
        self._prune_old_backups()

    def _prune_old_backups(self) -> None:
        self._last_prune = time.monotonic()
        cutoff = time.time() - self.max_age_days * 86400
        path = Path(self.baseFilename)
        for candidate in path.parent.glob(path.name + ".*"):
            if not candidate.name[len(path.name) + 1:].isdigit():
                continue
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except OSError:
                # A reader can temporarily hold the rotated file on Windows.
                pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            now = time.monotonic()
            if now >= self._retry_at and self.shouldRollover(record):
                try:
                    self.doRollover()
                    self.rollover_pending = False
                    self._retry_at = 0.0
                except OSError as exc:
                    self.rollover_pending = True
                    self._retry_at = now + self.retry_seconds
                    if self.stream is None:
                        self.stream = self._open()
                    print(f"Log rollover delayed for {self.baseFilename}: {exc}", file=sys.stderr)
            logging.FileHandler.emit(self, record)
            if now - self._last_prune >= 3600:
                self._prune_old_backups()
        except Exception as exc:
            print(f"Log write failed for {self.baseFilename}: {exc}", file=sys.stderr)
            self.handleError(record)


def create_json_handler(service: str, path: str | Path | None = None) -> ManagedRotatingFileHandler:
    _, max_bytes, backups, max_age = LOG_PROFILES[service]
    target = Path(path) if path is not None else write_log_path(service)
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = ManagedRotatingFileHandler(target, max_bytes=max_bytes,
                                         backup_count=backups, max_age_days=max_age)
    handler.setFormatter(JsonLineFormatter(service))
    return handler


def configure_process_logging(service: str, *, level: str | int | None = None,
                              console_stream=None, console_formatter=None) -> logging.Logger:
    root = logging.getLogger()
    resolved_level = configured_level(level)
    if getattr(root, "_mabobot_service", None) == service:
        if level is not None:
            root.setLevel(resolved_level)
        return root
    file_handler = create_json_handler(service)
    console = logging.StreamHandler(console_stream or sys.stdout)
    console.setFormatter(console_formatter or logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.basicConfig(level=resolved_level, handlers=[file_handler, console], force=True)
    root._mabobot_service = service
    root.info("logging.ready service=%s", service, extra={"event": "logging.ready"})
    return root
