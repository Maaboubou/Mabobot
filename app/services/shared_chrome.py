"""Shared Chrome configuration and coordination for browser-based plugins."""

from __future__ import annotations

import threading
from dataclasses import dataclass


from app.utils.plugin_config import get_config


SHARED_CHROME_CONFIG_PLUGIN = "summary_plus"
DEFAULT_CHROME_DEBUG_PORT = 19223
DEFAULT_CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DEFAULT_CHROME_USER_DATA_DIR = (
    "data/plugins/summary_plus/machine_bound/chrome_profile"
)
DEFAULT_CHROME_PROFILE_DIR = "Default"

_OPERATION_LOCK = threading.RLock()


@dataclass(frozen=True)
class SharedChromeSettings:
    debug_port: int
    executable: str
    user_data_dir: str
    profile_dir: str


def get_shared_chrome_operation_lock() -> threading.RLock:
    """Return the process-wide lock protecting shared browser tab operations."""
    return _OPERATION_LOCK


def load_shared_chrome_settings() -> SharedChromeSettings:
    """Load the single browser configuration owned by ``summary_plus``."""
    raw_port = get_config(
        "chrome_debug_port",
        DEFAULT_CHROME_DEBUG_PORT,
        plugin_name=SHARED_CHROME_CONFIG_PLUGIN,
    )
    try:
        debug_port = int(raw_port)
    except (TypeError, ValueError):
        debug_port = DEFAULT_CHROME_DEBUG_PORT
    if not 1 <= debug_port <= 65535:
        debug_port = DEFAULT_CHROME_DEBUG_PORT

    return SharedChromeSettings(
        debug_port=debug_port,
        executable=str(
            get_config(
                "chrome_path",
                DEFAULT_CHROME_PATH,
                plugin_name=SHARED_CHROME_CONFIG_PLUGIN,
            )
            or DEFAULT_CHROME_PATH
        ),
        user_data_dir=str(
            get_config(
                "chrome_user_data_dir",
                DEFAULT_CHROME_USER_DATA_DIR,
                plugin_name=SHARED_CHROME_CONFIG_PLUGIN,
            )
            or DEFAULT_CHROME_USER_DATA_DIR
        ),
        profile_dir=str(
            get_config(
                "chrome_profile_dir",
                DEFAULT_CHROME_PROFILE_DIR,
                plugin_name=SHARED_CHROME_CONFIG_PLUGIN,
            )
            or DEFAULT_CHROME_PROFILE_DIR
        ),
    )
