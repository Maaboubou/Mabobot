"""Windows entry point for ``pythonw -m mabobot_launcher``."""

from __future__ import annotations

import argparse
import ctypes
import os
import sys

from dotenv import load_dotenv

from .application import DesktopLauncher
from .constants import PROJECT_ROOT
from .instance import SingleInstance


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mabobot 桌面启动器")
    parser.add_argument("--startup", action="store_true", help="Windows 登录启动模式")
    return parser.parse_args()


def show_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "Mabobot 启动失败", 0x10)
    else:
        print(message, file=sys.stderr)


def main() -> int:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    instance = SingleInstance()
    try:
        if not instance.acquire():
            instance.request_show()
            return 0
        if os.name != "nt":
            show_error("Mabobot 桌面启动器目前仅支持 Windows 10/11。")
            return 2
        return DesktopLauncher(startup_mode=args.startup).run()
    except Exception as exc:
        show_error(f"桌面启动器无法启动：\n\n{exc}\n\n请查看 logs/launcher.jsonl。")
        return 1
    finally:
        instance.release()


if __name__ == "__main__":
    raise SystemExit(main())
