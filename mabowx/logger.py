"""mabowx 日志系统。

设计目标：

- 只管理 ``mabowx`` 命名空间下的 logger，不修改 root logger 和宿主的
  日志 handler，避免嵌入 Mabobot 时破坏其自身日志；
- 控制台默认 INFO，开启 debug 后显示 DEBUG；
- 文件日志始终记录 DEBUG，方便部署后定位问题；
- 文件日志按大小滚动，并限制备份份数与保留天数；
- 文件日志包含 PID / 线程 / 模块 / 行号；
- 超长消息自动截断，避免 UIA 树 dump 等把日志文件撑爆。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from mabobot_logging import ManagedRotatingFileHandler, configured_level, create_json_handler

try:
    import colorama
except ImportError:  # pragma: no cover - Linux 纯逻辑测试环境可能没有 colorama
    colorama = None  # type: ignore[assignment]

from .param import WxParam

if colorama is not None and hasattr(colorama, "just_fix_windows_console"):
    colorama.just_fix_windows_console()

LOG_COLORS = {
    "DEBUG": colorama.Fore.CYAN if colorama else "",
    "INFO": colorama.Fore.GREEN if colorama else "",
    "WARNING": colorama.Fore.YELLOW if colorama else "",
    "ERROR": colorama.Fore.RED if colorama else "",
    "CRITICAL": colorama.Fore.MAGENTA if colorama else "",
}

CONSOLE_FORMAT = "%(asctime)s [%(name)s] [%(levelname)s] %(message)s"
class ColoredFormatter(logging.Formatter):
    """控制台彩色日志格式；无 colorama 时退化为纯文本。"""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        color = LOG_COLORS.get(record.levelname, "")
        if color and colorama is not None:
            return f"{color}{message}{colorama.Style.RESET_ALL}"
        return message


def truncate_message(message: str, limit: int | None = None) -> str:
    """截断超长日志，避免单条日志过大。"""
    limit = limit if limit is not None else int(WxParam.LOG_MAX_MESSAGE_CHARS)
    if limit <= 0:
        return message
    if len(message) <= limit:
        return message
    marker = f"...[truncated, total={len(message)} chars]"
    return message[: max(0, limit - len(marker))] + marker


class MabowxLogger:
    """mabowx 专用 logger 单例。"""

    name = "mabowx"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.logger = logging.getLogger(self.name)
        self.logger.setLevel(logging.DEBUG)
        # 关键：不让日志冒泡到 root logger，也不清空 root handlers。
        self.logger.propagate = False

        self.console_handler = logging.StreamHandler()
        self.console_handler.setFormatter(
            ColoredFormatter(CONSOLE_FORMAT, "%Y-%m-%d %H:%M:%S")
        )
        self.console_handler.setLevel(logging.INFO)
        self.logger.addHandler(self.console_handler)

        self.file_handler: ManagedRotatingFileHandler | None = None
        self._file_setup_failed = False
        self._debug_enabled = False

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def set_debug(self, debug: bool = False) -> None:
        """开启/关闭控制台 DEBUG 日志；文件日志始终为 DEBUG。"""
        with self._lock:
            self._debug_enabled = bool(debug)
            self.console_handler.setLevel(logging.DEBUG if debug else logging.INFO)

    def set_level(self, level: str | int) -> None:
        """设置控制台日志级别，例如 ``"INFO"`` / ``logging.DEBUG``。"""
        with self._lock:
            self.console_handler.setLevel(level)

    def configure(
        self,
        log_dir: Path | str | None = None,
        *,
        debug: bool | None = None,
        console: bool = True,
    ) -> None:
        """重新配置日志目录和控制台开关。

        ``log_dir`` 为 None 时使用 WxParam.LOG_DIR；``console=False``
        可在纯后台场景关闭控制台输出。
        """
        with self._lock:
            if debug is not None:
                self.set_debug(debug)
            if log_dir is not None:
                WxParam.LOG_DIR = Path(log_dir)
            self.console_handler.setLevel(
                logging.DEBUG if self._debug_enabled else logging.INFO
            )
            if not console:
                self.logger.removeHandler(self.console_handler)
            elif self.console_handler not in self.logger.handlers:
                self.logger.addHandler(self.console_handler)
            self._remove_file_handler()
            self._file_setup_failed = False
            if WxParam.ENABLE_FILE_LOGGER:
                self._setup_file_logger()

    def close_file(self) -> None:
        """关闭当前文件 handler（测试或进程退出前使用）。"""
        with self._lock:
            self._remove_file_handler()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _log_dir(self) -> Path:
        env_dir = os.environ.get("MABOWX_LOG_DIR")
        if env_dir:
            return Path(env_dir)
        return Path(WxParam.LOG_DIR)

    def _setup_file_logger(self) -> None:
        if not WxParam.ENABLE_FILE_LOGGER or self.file_handler is not None:
            return
        if self._file_setup_failed:
            return
        try:
            log_dir = self._log_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            handler = create_json_handler("mabowx", log_dir / "mabowx.jsonl")
            handler.setLevel(configured_level(os.getenv("MABOWX_FILE_LOG_LEVEL", "DEBUG")))
            self.logger.addHandler(handler)
            self.file_handler = handler
        except Exception as exc:  # pragma: no cover - 文件系统异常
            self._file_setup_failed = True
            # 避免再次触发文件 handler 创建造成递归。
            logging.getLogger(self.name).warning("mabowx 文件日志初始化失败: %s", exc)

    def _remove_file_handler(self) -> None:
        if self.file_handler is None:
            return
        try:
            self.logger.removeHandler(self.file_handler)
            self.file_handler.close()
        except Exception:
            pass
        self.file_handler = None

    def _ensure_file_logger(self) -> None:
        if not WxParam.ENABLE_FILE_LOGGER:
            return
        if self.file_handler is None and not self._file_setup_failed:
            self._setup_file_logger()

    def _emit(self, level: str, msg: str, *args, stacklevel: int = 2, **kwargs) -> None:
        if not isinstance(msg, str):
            msg = str(msg)
        self._ensure_file_logger()
        getattr(self.logger, level)(
            truncate_message(msg),
            *args,
            stacklevel=stacklevel,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def debug(self, msg: str, *args, stacklevel: int = 2, **kwargs) -> None:
        self._emit("debug", msg, *args, stacklevel=stacklevel, **kwargs)

    def info(self, msg: str, *args, stacklevel: int = 2, **kwargs) -> None:
        self._emit("info", msg, *args, stacklevel=stacklevel, **kwargs)

    def warning(self, msg: str, *args, stacklevel: int = 2, **kwargs) -> None:
        self._emit("warning", msg, *args, stacklevel=stacklevel, **kwargs)

    def error(self, msg: str, *args, stacklevel: int = 2, **kwargs) -> None:
        self._emit("error", msg, *args, stacklevel=stacklevel, **kwargs)

    def critical(self, msg: str, *args, stacklevel: int = 2, **kwargs) -> None:
        self._emit("critical", msg, *args, stacklevel=stacklevel, **kwargs)

    def exception(
        self,
        msg: str,
        *args,
        stacklevel: int = 2,
        exc_info: bool | tuple | None = True,
        **kwargs,
    ) -> None:
        """记录异常消息并附带 traceback。"""
        if not isinstance(msg, str):
            msg = str(msg)
        self._ensure_file_logger()
        self.logger.exception(
            truncate_message(msg),
            *args,
            stacklevel=stacklevel,
            exc_info=exc_info,
            **kwargs,
        )

    def log_control(self, label: str, control, *, level: str = "debug") -> None:
        """记录 UIA 控件的关键属性，方便定位选择器问题。"""
        if not control:
            return
        try:
            rect = control.BoundingRectangle
            rect_text = f"{int(rect.left)},{int(rect.top)},{int(rect.right)},{int(rect.bottom)}"
        except Exception:
            rect_text = "<unreadable>"
        try:
            name = str(control.Name or "")
        except Exception:
            name = "<unreadable>"
        try:
            class_name = str(control.ClassName or "")
        except Exception:
            class_name = "<unreadable>"
        try:
            automation_id = str(control.AutomationId or "")
        except Exception:
            automation_id = "<unreadable>"
        getattr(self, level)(
            f"{label}: type={getattr(control, 'ControlTypeName', '?')} "
            f"name={name!r} class={class_name!r} aid={automation_id!r} rect={rect_text}"
        )


wxlog = MabowxLogger()
