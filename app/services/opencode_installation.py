"""Optional installation receipt and explicit checks; no startup WSL probe storm."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
_lock = threading.RLock()
_cached, _checked = None, 0.0
_receipt_stamp = None


def receipt_path():
    return ROOT / "data/opencode-installation.json"


def status(*, force=False):
    global _cached, _checked, _receipt_stamp
    from app.services.opencode_wsl import use_wsl, call_worker
    missing = {"installed": False, "status": "missing", "runtime": "wsl" if use_wsl() else "linux",
               "install_available": True, "message": "OpenCode 为可选组件，点击安装后自动准备运行环境"}
    with _lock:
        if not force:
            stamp = receipt_path().stat().st_mtime_ns if receipt_path().is_file() else None
            if _cached is not None and stamp == _receipt_stamp and time.monotonic() - _checked < 30:
                return dict(_cached)
            if not receipt_path().is_file():
                return missing
            # A receipt is only an installation observation. Actual execution
            # always verifies the binary in Linux before opening an account.
            try:
                return {**json.loads(receipt_path().read_text(encoding="utf-8")), "install_available": True}
            except (ValueError, OSError):
                return missing
        try:
            value = call_worker("status", {"check": True}, timeout=30)
            value["runtime"] = "wsl" if use_wsl() else "linux"
            value.update(install_available=True, message="OpenCode 已安装，可在引擎中心独立登录")
            if value["installed"]:
                path = receipt_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary, path)
            elif receipt_path().exists():
                receipt_path().unlink()
        except Exception:
            value = {**missing, "status": "unavailable", "message": "运行环境尚未就绪；点击安装将准备 WSL 和固定版本 OpenCode"}
        _cached, _checked = value, time.monotonic()
        _receipt_stamp = receipt_path().stat().st_mtime_ns if receipt_path().is_file() else None
        return dict(value)


def command():
    if os.name == "nt":
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(ROOT / "scripts/launcher/prepare_opencode.ps1")]
    return ["bash", str(ROOT / "scripts/launcher/prepare_linux.sh"), "opencode"]


def install(context):
    context.progress(5, "正在准备可选 OpenCode；请留意安装窗口中的系统提示")
    process = subprocess.Popen(command(), cwd=ROOT, stdin=None,
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0)
    code = process.wait()
    if code == 3010:
        raise RuntimeError("WSL 已安装，需要重启 Windows；重启后点击安装 OpenCode 继续")
    if code:
        raise RuntimeError("OpenCode 安装未完成；请检查安装窗口提示后重试")
    context.progress(90, "正在验证 WSL 运行桥接与 OpenCode 固定版本")
    value = status(force=True)
    if not value.get("installed") or value.get("status") != "ready":
        raise RuntimeError("安装命令已结束，但运行验证未通过；请点击重新安装或检查版本")
    context.progress(100, "OpenCode 已安装并接入，可在引擎中心添加账号")
    return value
