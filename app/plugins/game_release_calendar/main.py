"""Managed command and monthly publisher for the release calendar."""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.event_bus import EventType
from app.utils.plugin_config import get_config

logger = logging.getLogger(__name__)
PLUGIN_NAME = "game_release_calendar"
plugin = None


def cfg(key, default):
    return get_config(key, default, plugin_name=PLUGIN_NAME)


def parse_command(message, command="/月度发售"):
    match = re.fullmatch(re.escape(command) + r"\s+(\d{4}-(?:0[1-9]|1[0-2]))\s+预览", message.strip())
    if not match:
        return None
    datetime.strptime(match.group(1), "%Y-%m")
    return match.group(1)


class ReleaseCalendarPlugin:
    def __init__(self, context, event_bus):
        self.context = context
        self.event_bus = event_bus
        self.lock = threading.Lock()
        self.active = False
        self.state_path = context.storage.persistent_path("publication_state.json")

    def _read_state(self):
        if not self.state_path.exists():
            return {}
        # Fail closed on corrupt state: never resend a potentially published month.
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("月历发布记录格式错误")
        return state

    def _save_state(self, state):
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    def _push_enabled_chats(self):
        """Like Weekly, push only to chats granted the plugin's #push feature."""
        from app.models.user_permission import UserPermission, WeChatUser

        db = self.event_bus.db_session_factory()
        try:
            grants = db.query(WeChatUser.chat_name, UserPermission.plugin_name).join(
                UserPermission, UserPermission.user_id == WeChatUser.id,
            ).all()
            return sorted({
                chat_name for chat_name, plugin_name in grants
                if chat_name and str(plugin_name or "").rsplit("/", 1)[-1] == f"{PLUGIN_NAME}#push"
            })
        finally:
            db.close()

    def submit(self, month, wx=None, chat_name=None, scheduled=False, resume_run_id=None):
        if resume_run_id is not None:
            if scheduled or not re.fullmatch(r"[0-9a-f]{32}", str(resume_run_id)):
                raise ValueError("恢复任务须指定有效的运行编号，且不能作为定时发布")
            state_file = self.context.storage.persistent_path(f"runs/{month}/{resume_run_id}/run.json")
            prior = json.loads(state_file.read_text(encoding="utf-8"))
            if prior.get("month") != month or prior.get("status") not in {"failed", "cancelled"}:
                raise ValueError("只允许恢复同月份的失败或取消任务")
        with self.lock:
            if self.active:
                raise RuntimeError("已有发售日历任务运行中")
            self.active = True

        def run(operation):
            try:
                from .pipeline import generate_calendar

                operation.check_cancelled()
                run_dir = self.context.storage.persistent_path(f"runs/{month}/{resume_run_id or uuid.uuid4().hex}/run.json").parent
                result = generate_calendar(
                    output_dir=run_dir, month=month,
                    title=str(cfg("title", "刘局推荐")),
                    subtitle=str(cfg("subtitle", "{month}月重磅游戏发售信息")).format(month=int(month[5:])),
                    progress=operation.progress,
                    cancelled=lambda: operation.cancelled or self.context.workers.stop_event.is_set(),
                    resume=resume_run_id is not None,
                )
                operation.check_cancelled()
                if self.context.workers.stop_event.is_set():
                    raise RuntimeError("插件已停止")
                if scheduled:
                    if not result.get("publishable", False):
                        raise RuntimeError("资料覆盖或核实未通过，已保留预览，停止自动发布")
                    self._publish(month, result, wx, cancelled=lambda: operation.cancelled)
                elif wx and chat_name:
                    paths = [str(path) for path in result.get("image_paths", [])]
                    if paths and not wx.send_files(chat_name, paths):
                        raise RuntimeError("日历已生成，但预览图片发送失败")
                operation.progress(100, "发售日历已生成")
                return result
            except Exception:
                logger.exception("%s generation failed month=%s chat=%s", PLUGIN_NAME, month, chat_name)
                raise
            finally:
                with self.lock:
                    self.active = False

        try:
            return self.context.tasks.submit(
                "monthly_publish" if scheduled else "calendar_preview",
                f"刘局推荐 · {month} 发售日历",
                run, details={"month": month, "scheduled": scheduled, "chat_name": chat_name,
                              "resume_run_id": resume_run_id},
            )
        except Exception:
            with self.lock:
                self.active = False
            raise

    def _publish(self, month, result, wx, cancelled=lambda: False):
        if not wx:
            raise RuntimeError("消息发送服务尚未就绪")
        paths = [str(path) for path in result.get("image_paths", [])]
        if not paths:
            raise RuntimeError("未生成可发布图片")
        state = self._read_state()
        # Generation may take several minutes: refresh grants before sending.
        for chat_name in self._push_enabled_chats():
            key = f"{month}:{chat_name}"
            if key in state:
                continue
            if cancelled() or self.context.workers.stop_event.is_set():
                raise RuntimeError("插件已停止")
            # Save intent first. An interrupted or uncertain send requires review.
            state[key] = {"status": "sending", "run_dir": str(result.get("run_dir", ""))}
            self._save_state(state)
            if not wx.send_files(chat_name, paths):
                raise RuntimeError(f"日历发送未确认：{chat_name}；请检查后人工处理")
            state[key]["status"] = "sent"
            self._save_state(state)

    def handle_text(self, event):
        message = str(event.data.get("message") or "").strip()
        command = str(cfg("command", "/月度发售"))
        if not message.startswith(command):
            return False
        wx = event.context.get("wx")
        chat_name = event.data.get("chat_name")
        if not wx or not chat_name:
            return False
        try:
            month = parse_command(message, command)
            if not month:
                return bool(wx.send_message(chat_name, f"用法：{command} 2026-10 预览"))
            task = self.submit(month, wx, chat_name)
        except Exception as exc:
            logger.warning("%s command rejected: %s", PLUGIN_NAME, exc)
            wx.send_message(chat_name, f"发售日历任务未启动：{exc}")
            return False
        wx.send_message(chat_name, f"已开始制作 {month} 发售日历；任务编号：{task.get('operation_id', '')}")
        return True

    def scheduler(self):
        while not self.context.workers.stop_event.is_set():
            try:
                now = datetime.now(ZoneInfo("Asia/Shanghai"))
                publish_time = str(cfg("publish_time", "09:00"))
                if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", publish_time):
                    raise ValueError("每月发布时间须为 HH:MM")
                if now.day == 1 and now.strftime("%H:%M") >= publish_time:
                    month = now.strftime("%Y-%m")
                    state = self._read_state()
                    claim = f"attempt:{month}"
                    if claim not in state and not self.active and self._push_enabled_chats():
                        wx = self.event_bus.context.get("wx")
                        if wx:
                            # Record admission once per month. Failures stay visible in tasks.
                            state[claim] = {"status": "claimed"}
                            self._save_state(state)
                            self.submit(month, wx=wx, scheduled=True)
            except Exception:
                logger.exception("%s monthly scheduler failed", PLUGIN_NAME)
            self.context.workers.stop_event.wait(30)


def handle_text(event):
    return plugin.handle_text(event) if plugin else False


def register(event_bus, subscribe, context):
    global plugin
    plugin = ReleaseCalendarPlugin(context, event_bus)
    subscribe(EventType.TEXT_MESSAGE_RECEIVED, handle_text)
    context.health.register(lambda: {"status": "healthy", "message": "发售日历插件已就绪"})
    context.workers.start("monthly-scheduler", plugin.scheduler)


def unregister():
    global plugin
    plugin = None
