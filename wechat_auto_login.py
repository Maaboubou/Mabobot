#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
自动处理微信登录确认，并按需截取扫码登录二维码。

本模块同时供开机启动助手和运行中的微信掉线监控复用。

流程：
1. 等待 Windows / 微信自动启动稳定。
2. 自动确认账号安全掉线提示并点击“进入微信”。
3. 通过 wx_bot 健康接口或微信主窗口确认已登录。
4. 需要扫码时截取二维码；在线后按需继续启动 Mabobot。
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

from app.utils.subprocess_utils import hidden_process_kwargs


ROOT_DIR = Path(__file__).resolve().parent
LOG_DIR = ROOT_DIR / "logs"
LOG_FILE = LOG_DIR / "wechat_auto_login.log"

WECHAT_TITLES = {"微信", "WeChat"}
VK_RETURN = 0x0D
SW_RESTORE = 9

LOGIN_ACKNOWLEDGE_NAMES = ("我知道了",)
LOGIN_ENTER_NAMES = ("进入微信",)
LOGIN_QR_NAMES = ("二维码",)
LOGIN_QR_CLASS_NAME = "mmui::XImageView"


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def score(self) -> int:
        score = 0
        if "login" in self.class_name.lower():
            score += 100
        if self.title in WECHAT_TITLES:
            score += 20
        if 260 <= self.width <= 620 and 320 <= self.height <= 760:
            score += 50
        return score


@dataclass
class DisplayInfo:
    primary_width: int
    primary_height: int
    virtual_width: int
    virtual_height: int
    session_name: str

    @property
    def ready(self) -> bool:
        return self.primary_width >= 1280 and self.primary_height >= 720


@dataclass(frozen=True)
class ReloginResult:
    """一次掉线重登录推进结果。"""

    status: str
    reason: str
    qr_image_path: Path | None = None


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def ensure_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("wechat_auto_login.py 必须在 Windows Python 中运行")


def load_project_env() -> None:
    load_dotenv(ROOT_DIR / ".env")
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))


def get_display_info() -> DisplayInfo:
    ensure_windows()
    user32 = ctypes.windll.user32
    return DisplayInfo(
        primary_width=int(user32.GetSystemMetrics(0)),
        primary_height=int(user32.GetSystemMetrics(1)),
        virtual_width=int(user32.GetSystemMetrics(78)),
        virtual_height=int(user32.GetSystemMetrics(79)),
        session_name=os.environ.get("SESSIONNAME", ""),
    )


def wait_for_display_ready(timeout: int, poll_interval: float) -> bool:
    deadline = time.monotonic() + max(0, timeout)
    last_info: DisplayInfo | None = None

    while True:
        info = get_display_info()
        if (
            last_info is None
            or info.primary_width != last_info.primary_width
            or info.primary_height != last_info.primary_height
            or info.virtual_width != last_info.virtual_width
            or info.virtual_height != last_info.virtual_height
        ):
            logging.info(
                "当前显示环境: primary=%sx%s virtual=%sx%s session=%s",
                info.primary_width,
                info.primary_height,
                info.virtual_width,
                info.virtual_height,
                info.session_name,
            )
            last_info = info

        if info.ready:
            return True

        if time.monotonic() >= deadline:
            logging.warning(
                "显示环境未达标，当前 primary=%sx%s，要求至少 1280x720",
                info.primary_width,
                info.primary_height,
            )
            return False

        time.sleep(max(1.0, poll_interval))


def iter_windows() -> Iterable[WindowInfo]:
    ensure_windows()
    user32 = ctypes.windll.user32

    enum_windows = user32.EnumWindows
    enum_windows.argtypes = [ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p), ctypes.c_void_p]
    enum_windows.restype = ctypes.c_bool

    is_window_visible = user32.IsWindowVisible
    get_window_text_length = user32.GetWindowTextLengthW
    get_window_text = user32.GetWindowTextW
    get_class_name = user32.GetClassNameW
    get_window_rect = user32.GetWindowRect

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    windows: list[WindowInfo] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not is_window_visible(hwnd):
            return True

        title_len = get_window_text_length(hwnd)
        if title_len <= 0:
            return True

        title_buf = ctypes.create_unicode_buffer(title_len + 1)
        get_window_text(hwnd, title_buf, title_len + 1)
        title = title_buf.value.strip()
        if title not in WECHAT_TITLES:
            return True

        class_buf = ctypes.create_unicode_buffer(256)
        get_class_name(hwnd, class_buf, 256)

        rect = RECT()
        if not get_window_rect(hwnd, ctypes.byref(rect)):
            return True

        info = WindowInfo(
            hwnd=int(hwnd),
            title=title,
            class_name=class_buf.value.strip(),
            left=rect.left,
            top=rect.top,
            right=rect.right,
            bottom=rect.bottom,
        )
        windows.append(info)
        return True

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(callback)
    enum_windows(enum_proc, 0)
    return windows


def find_login_window() -> WindowInfo | None:
    candidates = [
        window
        for window in iter_windows()
        if "login" in window.class_name.lower()
        or (260 <= window.width <= 620 and 320 <= window.height <= 760)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[0]


def find_main_window() -> WindowInfo | None:
    candidates = [
        window
        for window in iter_windows()
        if "mainwindow" in window.class_name.lower()
        or (window.width >= 420 and window.height >= 360)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[0]


def focus_window(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.2)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)


def press_enter() -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_RETURN, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_RETURN, 0, 2, 0)


def press_enter_on_login_window(window: WindowInfo) -> None:
    logging.info(
        "发现微信登录窗口: hwnd=%s title=%s class=%s size=%sx%s",
        window.hwnd,
        window.title,
        window.class_name,
        window.width,
        window.height,
    )
    focus_window(window.hwnd)
    press_enter()
    logging.info("已向微信登录窗口发送 Enter")


def _login_descendants(window: WindowInfo) -> list[Any]:
    """读取登录窗 UIA 控件；Qt 顶层 Win32 类名本身不包含业务语义。"""
    from mabowx.core import uia

    root = uia.control_from_handle(window.hwnd)
    return list(uia.iter_descendants(root, max_nodes=300))


def _find_login_control(
    controls: Iterable[Any],
    *,
    names: Iterable[str] = (),
    class_name: str | None = None,
) -> Any | None:
    expected_names = set(names)
    for control in controls:
        try:
            if expected_names and control.Name not in expected_names:
                continue
            if class_name and control.ClassName != class_name:
                continue
            return control
        except Exception:
            continue
    return None


def _bounding_rect(control: Any) -> tuple[int, int, int, int]:
    rect = control.BoundingRectangle
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def capture_login_qr(
    window: WindowInfo,
    qr_control: Any,
    *,
    output_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """抓取并放大二维码，返回待发送的临时 PNG 路径。

    微信 4.x 的 Qt 渲染会忽略 PrintWindow 的 viewport 偏移，因此不能
    直接请求二维码子区域。登录窗很小，先抓完整窗口再裁剪既可靠也便宜。
    """
    from PIL import Image

    from mabowx.core import win32

    qr_left, qr_top, qr_right, qr_bottom = _bounding_rect(qr_control)
    full_image = win32.capture_window_rect(
        window.hwnd,
        (window.left, window.top, window.right, window.bottom),
    )
    crop_box = (
        max(0, qr_left - window.left),
        max(0, qr_top - window.top),
        min(window.width, qr_right - window.left),
        min(window.height, qr_bottom - window.top),
    )
    if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
        raise RuntimeError(f"二维码控件边界超出登录窗口: qr={crop_box}")

    qr_image = full_image.crop(crop_box).convert("RGB")
    if qr_image.width < 80 or qr_image.height < 80:
        raise RuntimeError(f"二维码截图尺寸异常: {qr_image.size}")

    # 整数倍最近邻放大不会模糊二维码边缘，邮件客户端预览也更容易扫码。
    scale = max(1, min(4, 544 // max(qr_image.size)))
    if scale > 1:
        qr_image = qr_image.resize(
            (qr_image.width * scale, qr_image.height * scale),
            Image.Resampling.NEAREST,
        )

    temp_file = tempfile.NamedTemporaryFile(
        prefix="wechat-login-qr-",
        suffix=".png",
        dir=output_dir,
        delete=False,
    )
    output_path = Path(temp_file.name)
    temp_file.close()
    try:
        qr_image.save(output_path, format="PNG")
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    return output_path


def prepare_relogin_qr(
    *,
    timeout: float = 20,
    poll_interval: float = 0.5,
    output_dir: str | os.PathLike[str] | None = None,
) -> ReloginResult:
    """推进掉线后的确认/重新登录流程，必要时返回二维码截图。"""
    deadline = time.monotonic() + max(0, timeout)
    last_reason = "尚未发现微信登录窗口"
    last_fallback_enter_at = 0.0

    while time.monotonic() <= deadline:
        if mabobot_already_running() or find_main_window():
            return ReloginResult("online", "微信已恢复在线")

        window = find_login_window()
        if not window:
            time.sleep(max(0.1, poll_interval))
            continue

        try:
            controls = _login_descendants(window)
            qr_control = _find_login_control(
                controls,
                names=LOGIN_QR_NAMES,
                class_name=LOGIN_QR_CLASS_NAME,
            )
            if qr_control is not None:
                focus_window(window.hwnd)
                path = capture_login_qr(
                    window,
                    qr_control,
                    output_dir=output_dir,
                )
                logging.info("已截取微信登录二维码: %s", path)
                return ReloginResult(
                    "qr_required",
                    "微信要求扫码登录",
                    qr_image_path=path,
                )

            acknowledge = _find_login_control(
                controls,
                names=LOGIN_ACKNOWLEDGE_NAMES,
            )
            if acknowledge is not None:
                last_reason = "已确认微信账号安全掉线提示"
                logging.info("发现微信安全掉线提示，点击“我知道了”")
                acknowledge.Click()
                time.sleep(max(0.2, poll_interval))
                continue

            enter_wechat = _find_login_control(
                controls,
                names=LOGIN_ENTER_NAMES,
            )
            if enter_wechat is not None:
                last_reason = "已点击“进入微信”，等待扫码页"
                logging.info("发现微信重新登录页，点击“进入微信”")
                enter_wechat.Click()
                time.sleep(max(0.5, poll_interval))
                continue

            last_reason = "已找到登录窗口，但未识别当前页面控件"
        except Exception as exc:
            last_reason = f"读取或操作微信登录控件失败: {exc}"
            logging.warning("%s", last_reason)

        # UIA 短暂不可用时保留原有 Enter 兜底，但限制频率。
        if time.monotonic() >= last_fallback_enter_at:
            press_enter_on_login_window(window)
            last_fallback_enter_at = time.monotonic() + 5
        time.sleep(max(0.1, poll_interval))

    return ReloginResult("unavailable", last_reason)


def log_display_info() -> None:
    info = get_display_info()
    logging.info(
        "当前显示环境: primary=%sx%s virtual=%sx%s session=%s",
        info.primary_width,
        info.primary_height,
        info.virtual_width,
        info.virtual_height,
        info.session_name,
    )


def mabobot_already_running() -> bool:
    try:
        import requests

        bridge_port = (os.getenv("WX_BOT_PORT") or "5555").strip()
        response = requests.get(f"http://127.0.0.1:{bridge_port}/health", timeout=2)
        if response.ok:
            data = response.json()
            return bool(data.get("wechat_connected") and data.get("wechat_online"))
    except Exception:
        return False
    return False


def python_executable_for_project() -> str:
    candidates = [
        ROOT_DIR / ".venv" / "Scripts" / "python.exe",
        ROOT_DIR / "venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def start_mabobot(start_target: str) -> None:
    if mabobot_already_running():
        logging.info("Mabobot 微信桥接已经在线，跳过重复启动")
        return

    target = (ROOT_DIR / start_target).resolve()
    if not target.exists():
        raise FileNotFoundError(f"Mabobot 启动目标不存在: {target}")

    if target.suffix.lower() == ".bat":
        command = ["cmd.exe", "/c", str(target)]
    elif target.suffix.lower() == ".py":
        command = [python_executable_for_project(), str(target)]
    else:
        command = [str(target)]

    subprocess.Popen(
        command,
        cwd=str(ROOT_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **hidden_process_kwargs(new_process_group=True),
    )
    logging.info("已启动 Mabobot: %s", target)


def send_qr_required_email(reason: str) -> bool:
    from app.services.email_service import get_email_service

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = "🚨 微信开机自动登录失败，需要扫码确认"
    body = (
        "微信开机自动登录未成功，Mabobot 未启动。\n\n"
        f"时间：{now}\n"
        f"主机：{os.environ.get('COMPUTERNAME') or os.environ.get('HOSTNAME') or 'unknown'}\n"
        f"原因：{reason}\n\n"
        "常见情况：微信显示二维码登录界面，按 Enter 无法进入微信。\n"
        "请远程到这台电脑扫码登录微信，登录完成后再启动 Mabobot。"
    )
    return get_email_service().send_email(body, subject, event="startup_failure")


def wait_and_login(args: argparse.Namespace) -> bool:
    deadline = time.monotonic() + args.login_timeout
    next_enter_at = 0.0
    enter_sent = False

    while time.monotonic() < deadline:
        if mabobot_already_running():
            return True

        main_window = find_main_window()
        if main_window:
            logging.info(
                "发现微信主窗口，认为登录已完成: hwnd=%s title=%s class=%s size=%sx%s",
                main_window.hwnd,
                main_window.title,
                main_window.class_name,
                main_window.width,
                main_window.height,
            )
            return True

        window = find_login_window()
        if window and time.monotonic() >= next_enter_at:
            press_enter_on_login_window(window)
            enter_sent = True
            next_enter_at = time.monotonic() + args.enter_interval

        time.sleep(args.poll_interval)

    if enter_sent:
        logging.warning("已尝试 Enter，但微信仍未在线，判断为二维码/手动登录场景")
    else:
        logging.warning("超时内没有找到可处理的微信登录确认窗口")
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="自动确认微信登录并启动 Mabobot")
    parser.add_argument("--initial-delay", type=int, default=int(os.getenv("WECHAT_AUTOLOGIN_INITIAL_DELAY", "60")))
    parser.add_argument("--login-timeout", type=int, default=int(os.getenv("WECHAT_AUTOLOGIN_TIMEOUT", "180")))
    parser.add_argument("--poll-interval", type=float, default=float(os.getenv("WECHAT_AUTOLOGIN_POLL_INTERVAL", "5")))
    parser.add_argument("--enter-interval", type=float, default=float(os.getenv("WECHAT_AUTOLOGIN_ENTER_INTERVAL", "20")))
    parser.add_argument("--display-timeout", type=int, default=int(os.getenv("WECHAT_AUTOLOGIN_DISPLAY_TIMEOUT", "180")))
    parser.add_argument("--start-delay", type=int, default=int(os.getenv("WECHAT_AUTOLOGIN_START_DELAY", "15")))
    parser.add_argument("--start-target", default=os.getenv("MABOBOT_START_TARGET", "START.bat"))
    parser.add_argument("--no-start", action="store_true", help="只确认微信登录，不再创建启动器进程")
    parser.add_argument("--no-email", action="store_true", help="失败时不发邮件，仅写日志")
    return parser.parse_args()


def main() -> int:
    setup_logging()
    load_project_env()

    try:
        ensure_windows()
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 2

    args = parse_args()
    logging.info("微信自动登录启动，初始等待 %ss", args.initial_delay)
    log_display_info()
    time.sleep(max(0, args.initial_delay))

    display_ready = wait_for_display_ready(args.display_timeout, args.poll_interval)

    if wait_and_login(args):
        if not display_ready:
            if not args.no_email:
                send_qr_required_email(
                    reason=(
                        "微信已可进入，但显示环境低于 1280x720。"
                        "为避免 mabowx 在 640x480/headless 桌面下错误调整窗口，本次未启动。"
                    )
                )
            return 1
        if args.start_delay > 0:
            logging.info("微信在线后等待 %ss 再启动 Mabobot", args.start_delay)
            time.sleep(args.start_delay)
        if args.no_start:
            logging.info("微信登录确认完成，交回桌面启动器继续启动服务")
        else:
            start_mabobot(args.start_target)
        return 0

    if not args.no_email:
        send_qr_required_email(
            reason=(
                f"{args.login_timeout}s 内未确认微信已登录。"
                "如果屏幕上是二维码登录界面，这是预期告警。"
            )
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
