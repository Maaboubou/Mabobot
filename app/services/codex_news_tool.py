"""Task-scoped host execution for last30days; no browser access in chat shells."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import subprocess
import sys
import threading
import uuid

from app.services.codex_browser_tool import CodexBrowserToolError, _is_link_like
from app.services.file_tools_runtime import _as_wsl_path
from app.utils.subprocess_utils import apply_hidden_process_defaults

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "data/last30days.json"
logger = logging.getLogger(__name__)


def settings():
    try:
        if CONFIG.is_symlink() or CONFIG.stat().st_size > 16384:
            return {}
        value = json.loads(CONFIG.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("enabled") is not True
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", str(value.get("profile_id", "")))
                or not isinstance(value.get("task_ids"), list)
                or any(not isinstance(x, str) or not re.fullmatch(r"T-[0-9a-f]+", x) for x in value["task_ids"])):
            return {}
        return value
    except (OSError, ValueError, TypeError):
        return {}


def authorized_run(schedule, profile_id, *, service=None):
    config = settings()
    if (not config or config["profile_id"] != profile_id or schedule is None
            or not schedule.scheduled or not schedule.run_id
            or schedule.cancelled and schedule.cancelled()):
        return None
    if service is None:
        from app.services.scheduled_tasks import get_scheduled_tasks
        service = get_scheduled_tasks()
    try:
        run = service.run(schedule.run_id)
        definition = run["definition"]
        if (run["task_id"] not in config["task_ids"] or run["status"] != "running"
                or run.get("cancel_requested") or definition["chat_id"] != schedule.actor.chat_id
                or definition["chat_name"] != schedule.chat):
            return None
        service.authorize(definition["chat_id"], definition["creator"])
        if not service.settings()["enabled"] or not service.chat_settings(definition["chat_id"])["enabled"]:
            return None
        return run
    except (ValueError, KeyError, TypeError):
        return None


def available(schedule, profile_id):
    return authorized_run(schedule, profile_id) is not None


def dynamic_tool_specs():
    return [{"type": "namespace", "name": "news", "description": "授权计划任务专用的last30days免费社区检索。宿主使用Firefox的X登录态，凭据不进入聊天。",
             "tools": [{"type": "function", "name": "research",
                        "description": "运行已安装的last30days引擎，返回公开帖子和来源状态。根据群聊关注点提供具体英文检索主题；用于当前早间新闻任务。默认快速检索近2天，收到结果后严格筛选过去24小时。不会发送消息。",
                        "inputSchema": {"type": "object", "additionalProperties": False,
                                        "required": ["topic"], "properties": {
                                            "topic": {"type": "string", "minLength": 1, "maxLength": 400},
                                            "sources": {"type": "array", "minItems": 1, "maxItems": 4,
                                                        "items": {"type": "string", "enum": ["x", "reddit", "hackernews", "github"]}},
                                            "subreddits": {"type": "array", "maxItems": 5, "items": {"type": "string", "pattern": "^[A-Za-z0-9_]{2,40}$"}},
                                            "x_handles": {"type": "array", "maxItems": 5, "items": {"type": "string", "pattern": "^[A-Za-z0-9_]{1,15}$"}},
                                            "days": {"type": "integer", "minimum": 1, "maximum": 7},
                                        }}}]}]


class NewsSession:
    def __init__(self, profile_id, context, schedule, *, service=None, runner=None):
        self.profile_id, self.context, self.schedule = profile_id, context, schedule
        self.service = service
        self.runner = runner or subprocess.run
        self.lock = threading.Lock()
        self.calls = 0

    def execute(self, tool, arguments):
        from scripts.file_tools.run_last30days import validate_request
        if tool != "research":
            raise CodexBrowserToolError("不支持的新闻检索工具")
        try:
            request = validate_request(json.loads(arguments) if isinstance(arguments, str) else arguments)
        except (ValueError, TypeError):
            raise CodexBrowserToolError("新闻检索参数无效") from None
        with self.lock:
            if (not self.context.online_research_allowed
                    or self.context.cancelled and self.context.cancelled()
                    or not authorized_run(self.schedule, self.profile_id, service=self.service)
                    or self.context.permission_chat_id != self.schedule.actor.chat_id):
                raise CodexBrowserToolError("当前任务未授权使用last30days或已停止")
            if self.calls >= 2:
                raise CodexBrowserToolError("本轮两次社区检索已完成，请整理已有证据并用网页核实重点")
            self.calls += 1
            script = ROOT / "scripts/file_tools/run_last30days.py"
            command = (["wsl.exe", "python3", "-X", "utf8", _as_wsl_path(script)]
                       if self.context.use_wsl else [sys.executable, "-X", "utf8", str(script)])
            command += ["--profile", self.profile_id]
            try:
                result = self.runner(command, **apply_hidden_process_defaults({
                    "input": json.dumps(request, ensure_ascii=False), "text": True,
                    "encoding": "utf-8", "errors": "replace", "capture_output": True,
                    "timeout": 230, "check": False,
                }))
                data = json.loads(result.stdout)
                if result.returncode or not isinstance(data, dict) or data.get("engine") != "last30days":
                    raise ValueError("engine failure")
            except (OSError, ValueError, subprocess.SubprocessError):
                logger.warning("last30days failed profile=%s run=%s", self.profile_id, self.schedule.run_id)
                raise CodexBrowserToolError("last30days检索未完成；未发送消息。请检查数据源状态或使用现有网页检索。") from None
            if (self.context.cancelled and self.context.cancelled()
                    or not authorized_run(self.schedule, self.profile_id, service=self.service)):
                raise CodexBrowserToolError("任务已停止，已丢弃检索结果")
            # The runner returns only redacted public evidence. Preserve it in
            # the request directory so findings can be traced to actual posts.
            try:
                directory = self.context.request_dir / ".news"
                if any(_is_link_like(p) for p in (directory, *directory.parents)):
                    raise ValueError("linked audit directory")
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f"research-{self.calls}-{uuid.uuid4().hex}.json"
                with path.open("x", encoding="utf-8") as output:
                    json.dump({"request": request, "report": data}, output, ensure_ascii=False, indent=2)
            except (OSError, ValueError):
                logger.warning("Could not save public news evidence run=%s", self.schedule.run_id)
            counts = {source: len(items) for source, items in data.get("items_by_source", {}).items()
                      if isinstance(items, list)}
            logger.info("last30days completed run=%s call=%s items=%s", self.schedule.run_id, self.calls, counts)
            return data
