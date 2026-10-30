"""Single leased scheduler and a bounded worker, independent of chat heads."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import logging
import hashlib
import json
import shutil
import threading
import uuid

from app.services.schedule_context import ScheduleContext, schedule_context
from app.services.schedule_rules import describe, display_time
from app.services.scheduled_tasks import ScheduleActor, ScheduleDenied, get_scheduled_tasks

logger = logging.getLogger(__name__)


def scoped_file(raw, root):
    from app.services.codex_browser_tool import _is_link_like
    # Persist relative resource names portably across Windows and WSL hosts.
    path, root = Path(str(raw).replace("\\", "/")), Path(root)
    if not path.is_absolute():
        path = root / path
    if any(_is_link_like(p) for p in (path, *path.parents)):
        raise ScheduleDenied("任务文件不能包含符号链接")
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ScheduleDenied("任务文件已失效或超出聊天空间") from exc
    if not path.is_file():
        raise ScheduleDenied("任务资源不是文件")
    return path.resolve()


class ScheduledTaskRuntime:
    def __init__(self, service=None, *, gateway=None):
        self.service = service or get_scheduled_tasks()
        self.gateway = gateway
        self.owner = uuid.uuid4().hex
        self.stop_event = threading.Event()
        self.thread = None
        self.pool = None
        self.future = None
        self.event_bus = None
        self.handler = None
        self.last_error = ""
        self.is_leader = False

    def start(self, event_bus, handler):
        if self.thread and self.thread.is_alive():
            return
        self.event_bus, self.handler = event_bus, handler
        self.stop_event.clear()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scheduled-task")
        self.thread = threading.Thread(target=self._loop, name="schedule-timer", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def status(self):
        return {"running": bool(self.thread and self.thread.is_alive()), "leader": self.is_leader,
                "busy": bool(self.future and not self.future.done()), "error": self.last_error}

    def current_handler(self):
        # Core Assistant settings can reload its handler without restarting the app.
        return self.handler() if callable(self.handler) else self.handler

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
                self.last_error = ""
            except Exception as exc:
                self.last_error = str(exc)[:1000]
                logger.exception("Scheduled task tick failed")
            self.stop_event.wait(5)

    def tick(self):
        self.is_leader = self.service.lease(self.owner)
        if not self.is_leader:
            return
        self.service.admit_due(self.owner)
        if self.pool and (self.future is None or self.future.done()) and not self.stop_event.is_set():
            # Delivery can block on WeChat; keep it off the lease heartbeat.
            resumes = self.service.pending_resumes(self.owner) if self.service.settings()["enabled"] else []
            if resumes:
                self.future = self.pool.submit(self.service.resume_due, resumes[0], self.owner)
                return
            pending = self.service.pending_failure_runs()
            if pending:
                self.future = self.pool.submit(self.deliver_run, pending[0])
                return
            run = self.service.claim(self.owner)
            if run:
                self.future = self.pool.submit(self.execute, run)

    def _check(self, run):
        if self.stop_event.is_set() or self.service.cancelled(run["id"], self.owner):
            raise InterruptedError("任务停止或调度租约已失效")
        definition = run["definition"]
        chat = self.service.authorize(definition["chat_id"], definition["creator"])
        self.service.authorize(definition["delivery_chat_id"])
        return chat

    def execute(self, run):
        definition = run["definition"]
        result_saved = False
        inference_returned = False
        try:
            if run['trigger'] == 'scheduled' and not run.get('attempts') and self.service.clock() - run['scheduled_at'] > definition['misfire_grace_seconds']:
                self.service.finish(run['id'], 'missed', error='排队时间超过补跑时效')
                return
            chat = self._check(run)
            if self.current_handler() is None:
                raise ScheduleDenied("AI 助手运行环境未启动")
            from app.services.codex_access_service import codex_access_service
            from app.services.assistant_reply_gateway import CodexReplyRequest, get_assistant_reply_gateway
            from app.services.codex_profile_service import CodexProfileService
            access = codex_access_service.for_id(definition["chat_id"], ensure=True)
            from app.services.wechat_file_store import sha256_file
            monitor = definition.get('monitor')
            saved_state = self.service.monitor_state(run) if monitor else None
            fingerprint = ''
            if monitor and monitor.get('source_file'):
                source = scoped_file(monitor['source_file'], access.workdir)
                if source.stat().st_size > 100 * 1024 * 1024:
                    raise ValueError('监控源文件不能超过 100 MB')
                fingerprint = hashlib.sha256(json.dumps([sha256_file(source), monitor, definition['prompt']], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if saved_state['revision'] and saved_state['fingerprint'] == fingerprint:
                    self.service.save_result(run, '', [], {}, observation={
                        'revision': saved_state['revision'], 'fingerprint': fingerprint,
                        'outcome': {'status': 'no_change', 'text': '', 'state': saved_state['state'], 'events': []}})
                    return
            files = []
            for raw in definition.get("resources", []):
                source = scoped_file(raw, access.workdir)
                files.append({"path": str(source), "name": source.name, "size": source.stat().st_size, "sha256": sha256_file(source)})
            profile_id = definition.get("profile_id") or CodexProfileService.resolve_assistant_profile_id(chat.get("profile_id"))
            def bind(manager, thread_id, turn_id):
                with self.service.db(True) as db:
                    db.execute("UPDATE scheduled_runs SET thread_id=?,turn_id=? WHERE id=?", (thread_id, turn_id, run["id"]))
            context = ScheduleContext(chat["name"], ScheduleActor(definition["chat_id"], definition["creator"]),
                                      scheduled=True, run_id=run["id"], bind=bind, input_files=tuple(dict(item) for item in files),
                                      cancelled=lambda: self.stop_event.is_set() or self.service.cancelled(run["id"], self.owner))
            from app.services.schedule_role_policy import OUTCOME_SCHEMA, OUTCOME_INSTRUCTIONS, parse_outcome
            instructions = (
                "你正在后台执行用户已创建的计划任务。直接完成任务并返回最终结果，不能只回复‘已收到/正在处理’。\n"
                "本次是独立会话，不得创建、修改或删除计划任务。不等待用户补充；缺少必要信息时明确报告阻塞原因。\n"
                "最终结果由宿主统一发送到已配置聊天，不要自行重复发送同一结果。遵守当前聊天的权限。\n"
                "需要读取网页时使用 wx_browser.open：本次调度使用项目共享 Chrome，保留其登录状态；"
                "相同网址已有标签页时会刷新后读取，没有时新建。不要把搜索缓存当成实时监控数据。"
                "工具结果的 browser_backend/reused_tab 标明实际浏览器与是否复用页面；连接失败时明确报告，不假称已刷新。\n"
                f"任务编号：{run['task_id']}；名称：{definition['title']}；版本：v{run['version']}\n"
                f"创建者：{definition['creator']}；时间规则：{describe(definition['schedule'])}\n"
                f"本次计划时间：{display_time(run['scheduled_at'], definition['schedule']['timezone'])}\n"
                f"实际执行时间：{display_time(self.service.clock(), definition['schedule']['timezone'])}\n"
                "任务中的昨天/最近一周等相对时间按实际执行时间及上述时区计算。资源附件为创建时保存的副本。\n"
                "用接收聊天当前角色的语气直接说出结果，像自然接话；提醒就直接提醒，无须固定开场。"
                "text 字段写可直接发送的自然正文，不加‘计划任务结果’等管理抬头，不展示任务编号、版本或完整后台指令。"
                "默认不介绍是谁安排的；只有内容只针对某人或不点名容易误解时才自然提及。"
                "可以用本聊天资料中已明确的常用称呼，不虚构昵称，不把称呼当作权限身份。有人追问来源时再据真实记录说明。"
            )
            destination = self.service.authorize(definition["delivery_chat_id"])
            handler = self.current_handler()
            if monitor:
                from app.services.schedule_monitor import monitor_instructions
                instructions += monitor_instructions(monitor, saved_state)
            messages = handler._build_scheduled_messages(destination["name"], instructions + "\n" + OUTCOME_INSTRUCTIONS, definition["prompt"])
            with schedule_context(context):
                result = (self.gateway or get_assistant_reply_gateway()).reply(CodexReplyRequest(
                    chat_name=chat["name"], codex_profile_id=profile_id,
                    delivery_chat_name=destination["name"],
                    messages=messages,
                    fresh_context=True, persistent_session=True, allow_exec_fallback=False,
                    web_search_mode='disabled',
                    history_enabled=True, history_request_id=run['id'],
                    usage_call_type='scheduled',
                    timeout_seconds=definition["timeout_seconds"], max_turns=1, input_files=files, output_schema=OUTCOME_SCHEMA,
                ))
            inference_returned = True
            self._check(run)
            outcome = parse_outcome(result.text, messages[0]["content"])
            if outcome['status'] == 'no_change' and not monitor:
                raise ValueError('普通定期任务不能静默跳过结果')
            if monitor and outcome['status'] in {'completed', 'no_change'}:
                from app.services.schedule_monitor import validate_observation
                validate_observation(outcome)
                if result.attachments:
                    raise ValueError('监控通知须为带稳定标识的文本及来源链接，不支持附件')
                from app.assistant.reply_completion import validate_plain_terminal_reply
                if any(not validate_plain_terminal_reply(e['text']).valid for e in outcome['events']):
                    raise ValueError('监控事件没有有效的最终结果')
                self.service.save_result(run, '', [], result.usage, observation={
                    'revision': saved_state['revision'], 'fingerprint': fingerprint, 'outcome': outcome},
                    at_users=getattr(result, 'at_users', ()))
                result_saved = True
                self.deliver_run(run['id'])
                return
            text = outcome["text"].strip()
            from app.assistant.reply_completion import validate_plain_terminal_reply
            if text and not validate_plain_terminal_reply(text).valid:
                raise ValueError("模型没有返回有效的最终结果")
            if outcome["status"] != "completed" and result.attachments:
                raise ValueError("阻塞结果包含附件，未交付，也未认定任务完成")
            if monitor and outcome['status'] == 'blocked':
                self.service.fail_or_retry(run, outcome['reason'], category='monitor_blocked',
                                           status='blocked', notice_text=text)
                self.deliver_run(run['id'])
                return
            attachments = self._retain_attachments(run, result.attachments, access.artifact_root)
            if not text and not attachments:
                raise ValueError("任务没有产生结果")
            if outcome["status"] == "role_blocked":
                self.service.save_result(run, text, [], result.usage, role_block={"reason": outcome["reason"], "rule": outcome["role_rule"]}, at_users=getattr(result, "at_users", ()))
                result_saved = True
                self.deliver_run(run["id"])
                return
            self.service.save_result(run, text, attachments, result.usage, at_users=getattr(result, "at_users", ()))
            result_saved = True
            self.deliver_run(run["id"], check=lambda: self._check(run))
            self.service.finish(run["id"], "completed" if outcome["status"] == "completed" else "blocked", error=outcome["reason"] or None)
        except InterruptedError as exc:
            self.service.finish(run["id"], "cancelled", error=str(exc))
        except Exception as exc:
            stopped = self.stop_event.is_set() or self.service.cancelled(run["id"], self.owner)
            if result_saved or stopped:
                self.service.finish(run["id"], "completed" if result_saved else "cancelled", error=str(exc))
            else:
                from app.services.schedule_failures import classify_failure
                category, transient = classify_failure(exc)
                disposition = self.service.fail_or_retry(run, exc, category=category, transient=transient and not inference_returned,
                                                          status="blocked" if isinstance(exc, ScheduleDenied) else "failed")
                if disposition == "notified":
                    self.deliver_run(run["id"])
            logger.exception("Scheduled task failed: task=%s run=%s", run["task_id"], run["id"])

    def _retain_attachments(self, run, attachments, root):
        if not attachments:
            return []
        from app.services.codex_browser_tool import _is_link_like
        dest_root = Path(root) / "scheduled-results" / run["id"]
        if any(_is_link_like(p) for p in (dest_root, *dest_root.parents)):
            raise ScheduleDenied("任务结果目录包含链接")
        dest_root.mkdir(parents=True, exist_ok=True)
        retained = []
        for index, attachment in enumerate(attachments):
            source = scoped_file(attachment.get("path", ""), root)
            dest = dest_root / f"{index}-{source.name}"
            shutil.copyfile(source, dest)
            retained.append({**attachment, "path": str(dest), "name": source.name})
        return retained

    def deliver_run(self, run_id, *, check=None, retry=False):
        run = self.service.run(run_id)
        definition = run["definition"]
        def send(delivery):
            if check:
                check()
            self.service.authorize(definition["chat_id"], definition["creator"])
            dest = self.service.authorize(definition["delivery_chat_id"])
            wx = self.event_bus.context.get("wx") if self.event_bus else None
            if wx is None:
                raise ScheduleDenied("微信未连接，结果已保存")
            handler = self.current_handler()
            if delivery["kind"] in {"text", "failure"}:
                members = (run.get("result") or {}).get("at_users", ())
                mention_kwargs = {"at_users": list(members)} if members else {}
                sent = wx.send_message(dest["name"], delivery["payload"], **mention_kwargs)
                if sent and handler:
                    handler._save_response_part_to_log(dest["name"], delivery["payload"])
                return sent
            from app.services.codex_access_service import codex_access_service
            access = codex_access_service.for_id(definition["chat_id"], ensure=False)
            path = scoped_file(delivery["payload"], access.artifact_root)
            if definition["delivery_chat_id"] != definition["chat_id"]:
                target_access = codex_access_service.for_id(definition["delivery_chat_id"], ensure=True)
                # Only Web can select another managed destination. Copy to its
                # artifact root so its normal attachment checks remain effective.
                target = Path(target_access.artifact_root) / "scheduled-results" / run_id
                from app.services.codex_browser_tool import _is_link_like
                if any(_is_link_like(p) for p in (target, *target.parents)):
                    raise ScheduleDenied("交付目录包含链接")
                target.mkdir(parents=True, exist_ok=True)
                destination = target / path.name
                shutil.copyfile(path, destination)
                path = destination
            if handler is None:
                raise ScheduleDenied("助手交付服务不可用")
            return handler._send_response_attachments(wx, dest["name"], [{"path": str(path), "type": "file"}])
        for delivery in run["deliveries"]:
            if delivery["status"] not in ({"pending", "failed"} if retry else {"pending"}):
                continue
            try:
                if not self.service.deliver(delivery["id"], send, retry=retry):
                    break
            except InterruptedError:
                raise
            except Exception:
                logger.exception("Scheduled delivery failed: %s", delivery["id"])
                break


_runtime = None


def get_scheduled_runtime():
    global _runtime
    if _runtime is None:
        _runtime = ScheduledTaskRuntime()
    return _runtime
