"""Launcher preferences and per-user Windows login registration."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .constants import BOOTSTRAP_SCRIPT, DATA_DIR, SETTINGS_FILE


@dataclass(frozen=True)
class LauncherPreferences:
    auto_confirm_wechat: bool = True
    close_behavior: str = "ask"

    @classmethod
    def from_mapping(cls, value: Any) -> LauncherPreferences:
        if not isinstance(value, dict):
            return cls()
        behavior = value.get("close_behavior", "ask")
        return cls(auto_confirm_wechat=bool(value.get("auto_confirm_wechat", True)),
                   close_behavior=behavior if behavior in ("ask", "tray", "exit") else "ask")


class PreferenceStore:
    def __init__(self, path: Path = SETTINGS_FILE):
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> LauncherPreferences:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return LauncherPreferences()
            return LauncherPreferences.from_mapping(payload)

    def save(self, preferences: LauncherPreferences) -> LauncherPreferences:
        payload = asdict(preferences)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.tmp")
            try:
                temporary.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, self.path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return preferences

    def set_auto_confirm_wechat(self, enabled: bool) -> LauncherPreferences:
        with self._lock:
            return self.save(replace(self.load(), auto_confirm_wechat=bool(enabled)))

    def set_close_behavior(self, behavior: str) -> LauncherPreferences:
        if behavior not in ("ask", "tray", "exit"):
            raise ValueError("请选择有效的窗口关闭方式")
        with self._lock:
            return self.save(replace(self.load(), close_behavior=behavior))


class WindowsLoginStartup:
    """Manage one HKCU Run entry; no administrator permission is required."""

    VALUE_NAME = "Mabobot"
    REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
    LEGACY_SHORTCUT_NAMES = {"start-wechat-autologin.lnk", "start-gui.lnk"}
    LEGACY_MARKERS = (
        "start-wechat-autologin.bat",
        "start-gui.bat",
        "wxautox4",
    )

    def __init__(self, bootstrap_script: Path = BOOTSTRAP_SCRIPT):
        self.bootstrap_script = Path(bootstrap_script).resolve()

    @property
    def command(self) -> str:
        return (
            "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass "
            f'-File "{self.bootstrap_script}" -Startup'
        )

    @staticmethod
    def _normalize_command(command: str) -> str:
        return " ".join(str(command or "").strip().casefold().split())

    @staticmethod
    def _startup_folder() -> Path:
        app_data = os.getenv("APPDATA")
        if app_data:
            return (
                Path(app_data)
                / "Microsoft"
                / "Windows"
                / "Start Menu"
                / "Programs"
                / "Startup"
            )
        return (
            Path.home()
            / "AppData"
            / "Roaming"
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )

    @classmethod
    def is_known_legacy_shortcut(cls, path: Path) -> bool:
        if path.name.casefold() not in cls.LEGACY_SHORTCUT_NAMES:
            return False
        try:
            raw = path.read_bytes()
        except OSError:
            return False
        decoded = "\n".join(
            (
                raw.decode("utf-16-le", errors="ignore"),
                raw.decode("utf-8", errors="ignore"),
                raw.decode("latin-1", errors="ignore"),
            )
        ).casefold()
        return any(marker in decoded for marker in cls.LEGACY_MARKERS)

    def legacy_shortcuts(self) -> list[Path]:
        folder = self._startup_folder()
        if not folder.is_dir():
            return []
        return [
            path
            for path in folder.iterdir()
            if path.is_file() and self.is_known_legacy_shortcut(path)
        ]

    @staticmethod
    def _write_registry(enabled: bool, command: str) -> None:
        import winreg

        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, WindowsLoginStartup.REGISTRY_PATH
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key,
                    WindowsLoginStartup.VALUE_NAME,
                    0,
                    winreg.REG_SZ,
                    command,
                )
            else:
                try:
                    winreg.DeleteValue(key, WindowsLoginStartup.VALUE_NAME)
                except FileNotFoundError:
                    pass

    def _archive_legacy_shortcuts(self) -> list[str]:
        archived: list[str] = []
        destination_root = DATA_DIR / "launcher_migrations"
        for shortcut in self.legacy_shortcuts():
            destination_root.mkdir(parents=True, exist_ok=True)
            destination = destination_root / shortcut.name
            if destination.exists():
                destination = destination.with_name(
                    f"{destination.stem}-{int(time.time())}{destination.suffix}"
                )
            shutil.move(str(shortcut), str(destination))
            archived.append(str(destination))
        return archived

    def migrate_legacy(self) -> dict[str, Any]:
        if os.name != "nt":
            return {"migrated": False, "archived": []}
        if not self.legacy_shortcuts():
            return {"migrated": False, "archived": []}
        self._write_registry(True, self.command)
        archived = self._archive_legacy_shortcuts()
        return {"migrated": True, "archived": archived, "startup": self.status()}

    def status(self) -> dict[str, Any]:
        if os.name != "nt":
            return {
                "supported": False,
                "enabled": False,
                "registered": False,
                "matches_current": False,
                "detail": "仅 Windows 支持登录启动",
            }

        import winreg

        registered_command = ""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.REGISTRY_PATH) as key:
                registered_command = str(
                    winreg.QueryValueEx(key, self.VALUE_NAME)[0] or ""
                )
        except FileNotFoundError:
            pass
        except OSError as exc:
            return {
                "supported": True,
                "enabled": False,
                "registered": False,
                "matches_current": False,
                "detail": f"读取 Windows 启动项失败：{exc}",
            }

        legacy = self.legacy_shortcuts()
        registered = bool(registered_command.strip())
        matches = self._normalize_command(
            registered_command
        ) == self._normalize_command(self.command)
        if legacy:
            detail = "检测到旧版登录启动项，将自动迁移"
        elif registered and not matches:
            detail = "检测到旧路径或旧版本启动项，重新开启即可修复"
        elif matches:
            detail = "当前用户登录后自动启动"
        else:
            detail = "未登记 Windows 登录启动"
        return {
            "supported": True,
            "enabled": matches or bool(legacy),
            "registered": registered,
            "matches_current": matches,
            "legacy_entries": [path.name for path in legacy],
            "detail": detail,
        }

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        if os.name != "nt":
            raise RuntimeError("登录启动仅支持 Windows")

        self._write_registry(bool(enabled), self.command)
        self._archive_legacy_shortcuts()
        return self.status()
