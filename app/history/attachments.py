"""Original attachment path metadata and bounded download coordination."""
from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import threading
import time

_lock = threading.Lock()
_pending = {}


def begin_attachment(chat, row_id):
    done = threading.Event()
    with _lock:
        _pending[(chat, row_id)] = done
    return done


def finish_attachment(chat, row_id):
    with _lock:
        done = _pending.pop((chat, row_id), None)
    if done:
        done.set()


def wait_for_attachments(chat, timeout=3.0):
    """Wait once for current downloads, never for image understanding."""
    deadline = time.monotonic() + timeout
    with _lock:
        pending = [done for (name, _), done in _pending.items() if name == chat]
    for done in pending:
        done.wait(max(0, deadline - time.monotonic()))


def attachment_metadata(source_path, *, name=""):
    """Record an existing file without creating a copy, link or directory."""
    from app.services.wechat_file_store import local_path_from_external

    source = local_path_from_external(source_path).absolute()
    if not source.is_file():
        raise FileNotFoundError("附件文件不可用")
    return {"status": "ready", "path": str(source),
            "name": name or source.name, "size": source.stat().st_size}


def attachment_text(message):
    attachment = (message.get("metadata") or {}).get("attachment") or {}
    status = attachment.get("status")
    image_id = str(message.get("id") or "") if message.get("message_type") == "image" else ""
    read_hint = (f'图片附件 ID（attachment_id）：{image_id}；需要看图时调用 history.read_attachment。\n'
                 if image_id and attachment else "")
    if status == "pending":
        return read_hint + "附件路径正在获取，尚未就绪。"
    if status == "failed":
        return read_hint + "附件路径获取失败，暂无可读取路径。"
    path = str(attachment.get("path") or "")
    if status != "ready" or not path:
        return ""
    # Include both host and WSL forms when the bot runs on Windows. The runtime
    # profile decides which environment Codex uses, independently of logging.
    paths = [path]
    if re.match(r"^[A-Za-z]:[\\/]", path):
        windows = PureWindowsPath(path)
        paths.append(str(PurePosixPath('/mnt', windows.drive[0].lower(), *windows.parts[1:])))
    hint = read_hint or ("若当前任务需要图片内容，可通过 history.range 查找该图片的 attachment_id，再调用 history.read_attachment。\n" if message.get("message_type") == "image" else "")
    return hint + "附件路径：" + "；WSL 路径：".join(paths)
