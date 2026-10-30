"""Non-blocking launcher status from the actual Codex runtime probe."""

from __future__ import annotations

import threading
import time


def probe_codex_environment():
    # Keep runtime/database imports and WSL startup off the UI snapshot path.
    from app.services.file_tools_runtime import get_file_tools_runtime

    return get_file_tools_runtime()


class CodexEnvironmentMonitor:
    def __init__(self, *, probe=probe_codex_environment):
        self._probe = probe
        self._lock = threading.Lock()
        self._running = False
        self._checked_at = None
        self._status = {"ready": False, "detail": "检测中…"}

    def snapshot(self):
        with self._lock:
            if not self._running and (
                self._checked_at is None or time.monotonic() - self._checked_at >= 60
            ):
                self._running = True
                threading.Thread(target=self._worker, name="codex-environment", daemon=True).start()
            return {"key": "codex", "label": "Codex", "optional": True, **self._status}

    def _worker(self):
        try:
            runtime = self._probe()
            host = {"wsl": "WSL", "windows": "Windows", "linux": "Linux"}.get(
                runtime.get("runtime"), "运行环境"
            )
            ready = bool(runtime.get("codex", {}).get("available"))
            if runtime.get("stale"):
                status = {"ready": False, "detail": f"{host} · 暂无法确认"}
            elif ready:
                status = {"ready": True, "detail": f"{host} · 已就绪"}
            elif runtime.get("error") and runtime["error"] != "Codex CLI was not detected":
                status = {"ready": False, "detail": f"{host} · 检测失败"}
            else:
                status = {"ready": False, "detail": f"{host} · 未检测到 CLI"}
        except Exception:
            # An optional runtime must not break the launcher or leave it probing forever.
            status = {"ready": False, "detail": "Codex · 检测失败"}
        with self._lock:
            self._status = status
            self._checked_at = time.monotonic()
            self._running = False
