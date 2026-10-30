"""Find a Chrome-compatible browser, including the managed Playwright download."""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path


DEFAULT_CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def managed_browser_candidates() -> list[Path]:
    configured = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "")
    if configured == "0":
        import importlib.util
        spec = importlib.util.find_spec("playwright")
        roots = [Path(spec.origin).parent / "driver/package/.local-browsers"] if spec and spec.origin else []
    elif configured:
        roots = [Path(configured).expanduser()]
    elif sys.platform == "win32":
        roots = [Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "ms-playwright"]
    elif sys.platform == "darwin":
        roots = [Path.home() / "Library/Caches/ms-playwright"]
    else:
        roots = [Path(os.getenv("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "ms-playwright"]
    candidates = []
    for root in roots:
        versions = sorted(root.glob("chromium-*"), key=lambda p: int(re.search(r"(\d+)$", p.name)[1])
                          if re.search(r"(\d+)$", p.name) else 0, reverse=True)
        for version in versions:
            candidates.extend(version / relative for relative in (
                "chrome-win64/chrome.exe", "chrome-win/chrome.exe",
                "chrome-linux64/chrome", "chrome-linux/chrome",
                "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
            ))
    return candidates


def find_chrome_executable(configured: str = "") -> str:
    value = str(configured or "").strip()
    # Keep explicit custom choices visible (and diagnosable) instead of silently replacing them.
    if value and value != DEFAULT_CHROME_PATH:
        return value
    candidates = [Path(value)] if value else []
    if sys.platform == "win32":
        for root in (os.getenv("PROGRAMFILES"), os.getenv("PROGRAMFILES(X86)"), os.getenv("LOCALAPPDATA")):
            if root:
                candidates.append(Path(root) / "Google/Chrome/Application/chrome.exe")
    else:
        for command in ("google-chrome", "chromium", "chromium-browser"):
            resolved = shutil.which(command)
            if resolved:
                candidates.append(Path(resolved))
    candidates.extend(managed_browser_candidates())
    return next((str(path) for path in candidates if path.is_file()), value or DEFAULT_CHROME_PATH)
