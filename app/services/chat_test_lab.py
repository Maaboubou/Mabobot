"""Isolated conversational tool tests. Production delivery is never connected."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import atexit
import base64
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import threading
import time
import uuid

from app.history.store import ArchiveStore
from app.history.tools import HistorySession, dynamic_tool_specs as history_specs
from app.services.codex_app_server import CodexAppServerManager, CodexThreadStateStore
from app.services.codex_proxy.client import _as_runtime_path
from app.services.bot_base_instructions import active_base

CAPABILITIES = [
    {'name':'历史、图片与成员','mode':'现场只读','detail':'按需读取当前聊天档案。'},
    {'name':'Shell / Python / 文件','mode':'真实工具 · 隔离工作目录','detail':'根目录拒绝访问，工作目录可写；命令行网络关闭。'},
    {'name':'图片生成与编辑','mode':'聊天配置','detail':'按聊天选择的渠道生成，结果仅交付到本会话。'},
    {'name':'网页搜索','mode':'真实检索 · 遵循聊天联网权限','detail':'使用原生搜索及本聊天获准使用的受控浏览工具。'},
    {'name':'计划任务管理','mode':'真实工具 · 独立任务库','detail':'独立空任务库，可创建、修改、查询；真实角色边界核对。测试任务不会被生产调度器执行。'},
    {'name':'微信 @ / 文件交付','mode':'交付拦截 · 只显示在测试台','detail':'使用真实 @ 参数校验；最终文字和文件不会发送微信。'},
    {'name':'邮件与业务写入','mode':'预演','detail':'记录操作目标和内容，仅在本页查看。'},
]

LAB_INSTRUCTIONS = '''【聊天测试台运行环境】
按角色自然回复当前测试消息。此处使用独立测试线程和工作目录，历史直接读取当前聊天。
生成图片和联网检索按本聊天配置真实调用。邮件和外部业务写入必须使用 preview.stage 生成操作预演并明确告知用户。实际提供的工具以工具列表为准。历史和成员资料只读，使用 search、range、read、people 和 read_attachment 查询现场档案；schedules 管理只作用于测试任务库，后台不会自动执行测试任务。
本轮文字与文件交付只显示在 Web 测试台。不要声称已发送微信、邮件或写入生产业务系统。微信 @ 工具只准备测试收件人。
使用 shell / Python 完成文件或数据处理；命令网络关闭。联网查询使用可用的原生网页搜索。
需要交付的文件请保存到工作目录的 outputs 子目录，测试台会提供下载。不要尝试访问原始生产路径或外部服务的写入接口。
'''


def session_access(session):
    return session.workspace_access != 'none'


def safe_file(root: Path, relative: str, limit=64 * 1024 * 1024) -> Path:
    raw = root / relative
    if any(p.is_symlink() or (p.exists() and getattr(p.lstat(), 'st_file_attributes', 0) & 0x400) for p in (raw, *raw.parents)):
        raise ValueError('不允许通过链接访问文件')
    path = Path(os.path.abspath(raw))
    if not path.is_relative_to(Path(os.path.abspath(root))) or not path.is_file() or path.stat().st_size > limit:
        raise ValueError('文件不在测试目录内、不存在或超过大小上限')
    return path


def open_output(root: Path, relative: str):
    """Open before responding; native commands cannot race a link into host reads."""
    target = safe_file(root, relative)
    if os.open in os.supports_dir_fd:
        descriptor = os.open(target.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in target.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            stream = os.fdopen(os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor), 'rb')
        finally:
            os.close(descriptor)
    else:
        stream = target.open('rb')
        if os.name == 'nt':
            import ctypes
            import msvcrt
            from ctypes import wintypes
            getter = ctypes.windll.kernel32.GetFinalPathNameByHandleW
            getter.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            getter.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            length = getter(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
            actual = buffer.value.removeprefix('\\\\?\\')
            if not length or length >= len(buffer) or Path(actual) != target:
                stream.close()
                raise ValueError('文件路径在读取时发生变化')
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024:
        stream.close()
        raise ValueError('不是可下载的普通文件，或文件超过64MB')
    return stream


LIVE_HISTORY_TOOLS = {'search', 'range', 'read', 'people', 'read_attachment'}


class LiveHistoryReader(ArchiveStore):
    """Reuse the live projection and queries without schema initialization or writes."""
    write_search_receipts = False

    def __init__(self, source):
        self.db_path = source.db_path
        self.fts = source.fts

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.db_path.as_uri() + '?mode=ro', uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def record_lookup(self, chat, request_id, tool, details):
        # LabManager records tool events in the test conversation.
        pass


class LabManager(CodexAppServerManager):
    """Use native Codex transport/tools with a closed, per-session host registry."""
    publish_permission_support = False

    def __init__(self, session):
        self.session = session
        super().__init__(codex_bin=session.profile['wrapper_path'], workdir=str(session.workspace),
            state_store=CodexThreadStateStore(session.root/'threads.sqlite3'),
            instance_name='test-lab-'+session.id, managed_profile_id=session.profile['name'],
            experimental_api=True, model_supports_web_search=bool(session.profile.get('supports_web_search')), service_tier=session.spec.get('service_tier',''))
        from app.services.codex_schedule_tool import dynamic_tool_specs as schedule_specs
        from app.services.codex_mention_tool import dynamic_tool_specs as mention_specs
        history, = history_specs()
        history['tools'] = [tool for tool in history['tools'] if tool['name'] in LIVE_HISTORY_TOOLS]
        history['description'] = 'Read messages, images and members directly from the current chat.'
        from app.services.codex_image_tool import dynamic_tool_specs as image_specs, tools_for_mode
        from app.services.chat_test_tools import external_preview_spec
        from app.services.codex_lens_tool import dynamic_tool_specs as lens_specs
        specs = [history, tools_for_mode(image_specs()[0],session.image_mode), external_preview_spec()] + mention_specs()
        if session.schedules_enabled:
            specs += schedule_specs()
        if session.browser_allowed:
            if self.browser_tool.enabled:
                specs += self.browser_tool.dynamic_tool_specs()
            from app.services.lens_management import LensSettings
            specs += lens_specs(LensSettings(**session.prepared.get('lens',{})))
        self.dynamic_tool_specs = specs
        self.allowed_calls = {(ns['name'],t['name']) for ns in specs for t in ns['tools']}
        self.permission_read_roots = (_as_runtime_path(session.inputs, self.use_wsl),)
        self.dynamic_tool_signature = 'test-lab-live-history-v2'
        self._lab_mcp_disabled = {}

    def _command(self):
        from app.services.codex_direct import DIRECT_CONFIG, _toml
        args = super()._command()
        config = {**DIRECT_CONFIG, 'features.shell_tool':session_access(self.session), 'features.unified_exec':session_access(self.session),
                  'features.apply_patch_freeform':self.session.workspace_access=='read_write', 'features.view_image':True,
                  'features.code_mode':True, 'features.code_mode_host':True,
                  'include_permissions_instructions':True,
                  'web_search':'live' if self.session.online else 'disabled'}
        extra = []
        for key, value in config.items():
            extra.extend(['-c',key+'='+_toml(value)])
        if self.use_wsl:
            index = args.index('--payload') + 1
            payload = json.loads(base64.urlsafe_b64decode(args[index]))
            payload['args'].extend(extra)
            args[index] = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        else:
            args.extend(extra)
        return args

    def _thread_config(self, *args, **kwargs):
        config = super()._thread_config(*args, **kwargs)
        config.update({'features.apps':False,
                       'features.plugins':False, 'mcp_servers':{}, **self._lab_mcp_disabled})
        return config

    def _permission_tool_specs(self, *args, **kwargs):
        return self.dynamic_tool_specs

    def _bind_dynamic_tool_turn(self, thread_id, turn_id):
        super()._bind_dynamic_tool_turn(thread_id, turn_id)
        if thread_id == self.session.thread_id:
            self.session.active_turn = turn_id
            self.session.turn_bound.set()

    def _handle_notification(self, method, params):
        super()._handle_notification(method, params)
        item = params.get('item') or {}
        if method == 'item/completed' and item.get('type') == 'commandExecution':
            self.session.event('command_result', {'tool':'shell','output':str(item.get('aggregatedOutput') or '')[:20000],
                'exit_code':item.get('exitCode'),'elapsed_ms':item.get('durationMs')})

    def _record_turn_event(self, tracker, event_type, details=None, **updates):
        self.session.event(event_type, details or {})
        return True

    def _execute_dynamic_tool_request(self, message):
        params = message.get('params') or {}
        name = str(params.get('tool') or '')
        namespace = str(params.get('namespace') or '')
        if '.' in name and not namespace:
            namespace, name = name.split('.',1)
        name = name.removeprefix(namespace+'.')
        call_id = str(params.get('callId') or '')
        started = time.monotonic()
        success = False
        try:
            if (self.session.cancelled.is_set() or self.session.status != 'running'
                    or params.get('threadId') != self.session.thread_id):
                raise ValueError('工具调用不属于当前测试回合')
            if not self.session.turn_bound.wait(timeout=10) or params.get('turnId') != self.session.active_turn:
                raise ValueError('工具调用不属于当前测试回合')
            with self.session.tool_lock:
                if self.session.cancelled.is_set() or self.session.status != 'running':
                    raise ValueError('测试回合已停止')
                if not call_id or call_id in self.session.calls:
                    raise ValueError('重复或无效的工具调用')
                self.session.calls.add(call_id)
                self.session.event('tool_started', {'tool':namespace+'.'+name,'arguments':params.get('arguments')})
                if (namespace,name) not in self.allowed_calls:
                    raise ValueError('本会话未启用该工具')
                with self.session.model_source(call_id):
                    result = self.session.execute_tool(namespace, name, params.get('arguments'))
            from app.history.image_reader import AttachmentImageResult
            from app.services.codex_image_tool import ImageToolResult
            content = result.content_items() if isinstance(result, (AttachmentImageResult,ImageToolResult)) else [
                {'type':'inputText','text':json.dumps(result,ensure_ascii=False)}]
            trace = result.metadata if isinstance(result, AttachmentImageResult) else result
            success = True
        except Exception as exc:
            trace = {'error':str(exc)}
            content = [{'type':'inputText','text':json.dumps(trace,ensure_ascii=False)}]
        self.session.event('tool_completed' if success else 'tool_failed',
            {'tool':namespace+'.'+name,'result':trace,'elapsed_ms':round((time.monotonic()-started)*1000)})
        self._send({'id':message['id'],'result':{'success':success,'contentItems':content}})


class LabSession:
    def __init__(self, root, prepared, source_store, variant, online, recent_reader=None, listener_bus=None):
        self.id = root.name
        self.root, self.workspace = root, root/'workspace'
        self.workspace.mkdir(parents=True)
        (self.workspace/'outputs').mkdir()
        for protected in ('.agents', '.codex', '.git'):
            (self.workspace/protected).mkdir()
        # Host writes land outside the model-writable tree, preventing link attacks.
        self.inputs = root/'inputs'
        self.inputs.mkdir()
        self.prepared = copy.deepcopy(prepared)
        self.chat_id = prepared.get('chat_id')
        self.profile = copy.deepcopy(prepared['profile'])
        self.spec = copy.deepcopy(prepared['specs'][1 if variant=='draft' else 0])
        self.persona = prepared.get('personas',['',''])[1 if variant=='draft' else 0]
        self.source_chat = prepared['context']['chat_name']
        self.chat = 'test-'+self.id
        self.context = copy.deepcopy(prepared['context'])
        self.variant, self.online = variant, online
        self.workspace_access = prepared.get('workspace_access','read_write')
        self.source_store = source_store
        self.recent_reader = recent_reader or source_store.recent
        self.last_context_fingerprint = None
        self.pending_context = []
        from app.services.image_generation_config import runtime_image_mode
        self.listener_bus = listener_bus
        self.ingress = None
        self.actions = []
        self.turn_tools = None
        self.asset_engine = None
        self.image_policy = prepared.get('image_policy',{'mode':'native','channels':[]})
        self.image_mode = runtime_image_mode(self.image_policy)
        self.browser_allowed = bool(prepared.get('research_requested') and self.workspace_access=='read_write')
        self.schedules_enabled = prepared.get('schedules_enabled',True)
        self.status = 'preparing'
        self.error = ''
        self.thread_id = ''
        self.active_turn = ''
        self.turn_bound = threading.Event()
        self.manager = None
        self.review_bridge = None
        self.cancelled = threading.Event()
        self.lock, self.tool_lock = threading.RLock(), threading.Lock()
        self.events, self.messages, self.calls = [], [], set()
        self.event_sequence = 0
        self.turn_number = 0
        self.updated = time.time()
        self.history = HistorySession(self.source_chat, self.id, store=LiveHistoryReader(source_store))
        self.schedules = None
        self.mentions = None
        self.schedule_context = None
        self.closed = False

    def event(self, kind, details):
        # Bound observations; never include image bytes or internal reasoning.
        if kind.startswith('reasoning') or (details or {}).get('item_type')=='reasoning':
            return
        text = json.dumps(details,ensure_ascii=False,default=str)
        if len(text)>24000:
            details={'summary':text[:24000], 'truncated':True}
        with self.lock:
            self.event_sequence += 1
            self.events.append({'id':self.event_sequence,'turn':self.turn_number,'kind':kind,'time':time.time(),'details':details})
            self.events = self.events[-2000:]
            self.updated = time.time()

    def initialize(self):
        try:
            from app.services.scheduled_tasks import ScheduledTaskService, ScheduleActor
            def directory(chat_id=None,chat_name=None):
                if chat_id not in (None,1) or chat_name not in (None,self.chat):
                    raise ValueError('测试任务不能访问其他聊天')
                return {'id':1,'name':self.chat,'enabled':True,'profile_id':self.profile['name'],'blocked_names':[]}
            self.schedules=ScheduledTaskService(self.root/'schedules.sqlite3',directory=directory,reviewer=self.review_schedule)
            admin=ScheduleActor(1,'测试管理员',True)
            self.schedules.set_enabled(True,admin,expected_version=self.schedules.settings()['version'])
            self.schedules.set_chat_enabled(1,True,admin,expected_version=self.schedules.chat_settings(1)['version'])
            if self.cancelled.is_set():raise ValueError('测试初始化已停止')
            from app.services.chat_test_ingress import WebChatIngress
            self.ingress=WebChatIngress(self,self.listener_bus)
            self.status='idle'
            self.event('ready',{'history_mode':'live','capabilities':CAPABILITIES})
        except Exception as exc:
            self.status='cancelled' if self.cancelled.is_set() else 'failed'
            self.error=str(exc)
            self.event('error',{'message':self.error})

    def live_context(self):
        from app.assistant.context_manager import ChatContextManager
        from app.history.context import render_recent
        rows = self.recent_reader(self.source_chat, 50)
        history = render_recent(rows, ChatContextManager(), budget=6000)
        fingerprint = hashlib.sha256(history.encode()).hexdigest()
        with self.lock:
            self.context = {**self.context, 'history': history, 'message_count': len(rows),
                            'captured_at': datetime.now().isoformat(), 'fingerprint': fingerprint}
        return history, fingerprint

    def execute_tool(self,namespace,name,arguments):
        args=json.loads(arguments) if isinstance(arguments,str) else arguments
        if not isinstance(args,dict):raise ValueError('工具参数必须为对象')
        if namespace=='history':
            if name not in LIVE_HISTORY_TOOLS:
                raise ValueError('现场档案仅开放查询，不支持导出或修改成员')
            return self.history.execute(name,args)
        if namespace=='wechat':
            from app.services.codex_mention_tool import execute
            return execute(name,args,self.mentions,self.chat)
        if namespace=='schedules' and self.schedules_enabled:
            from app.services.codex_schedule_tool import execute
            browser=SimpleNamespace(chat_id=self.chat,workdir=self.workspace,request_dir=self.inputs,
                                    workspace_access=self.workspace_access,use_wsl=self.manager.use_wsl)
            result=execute(name,args,self.schedule_context,browser,service=self.schedules)
            if args.get('action') not in ('list','get'):
                with self.lock:self.actions.append({'turn':self.turn_number,'operation':'schedule','target':self.source_chat,'content':result,'executed':False})
            return result
        if self.turn_tools:
            return self.turn_tools.execute(namespace,name,args)
        raise ValueError('此工具尚无隔离适配，测试台未开放')

    def review_schedule(self,definition):
        from app.services.schedule_role_policy import REVIEW_INSTRUCTIONS, REVIEW_SCHEMA, parse_review
        from app.services.bot_reply_comparison import run_variant
        from app.services.opencode_process_bridge import ProcessBridge
        from app.services.codex_direct_runtime import worker_command
        spec={**self.spec,'instructions':REVIEW_INSTRUCTIONS,'developer_instructions':'',
              'input':json.dumps({'role_prompt':self.persona,'task':definition},ensure_ascii=False),'schema':REVIEW_SCHEMA}
        spec.pop('input_messages',None)
        self.event('role_review_started',{'title':definition.get('title')})
        with self.lock:
            if self.cancelled.is_set():raise ValueError('测试已停止')
            bridge=self.review_bridge=ProcessBridge(worker_command())
        try:result=run_variant(bridge,self.profile,spec)
        finally:
            bridge.close()
            with self.lock:self.review_bridge=None
        if result['status']!='completed':raise ValueError('测试计划任务的角色边界核对失败')
        return parse_review(result['text'],self.persona)

    def start_manager(self):
        manager=self.manager=LabManager(self)
        manager.start()
        if not manager.permission_support.get('permission_profiles'):
            raise ValueError('当前 Codex 未通过隔离权限验证，测试未启动')
        probe = self.inputs/('.read-probe-'+uuid.uuid4().hex)
        probe.write_text('LAB_INPUT_READABLE', encoding='utf-8')
        try:
            check = manager._request('command/exec', {'command':['cat',_as_runtime_path(probe,manager.use_wsl)],
                'permissionProfile':'mabobot-isolated-ro-offline','runtimeWorkspaceRoots':[_as_runtime_path(self.workspace,manager.use_wsl)],
                'timeoutMs':5000}, timeout=10)
            if check.get('exitCode') != 0 or check.get('stdout','').strip() != 'LAB_INPUT_READABLE':
                raise ValueError('Codex 无法读取测试附件目录。请将 CHAT_TEST_LAB_ROOT 设置到应用数据盘，避免使用 /tmp。')
        finally:
            probe.unlink(missing_ok=True)
        self.online = self.online and bool(manager.permission_support.get('web_search'))
        config=manager._request('config/read',{'includeLayers':False}).get('config') or {}
        manager._lab_mcp_disabled={f'mcp_servers.{json.dumps(name)}.enabled':False for name in (config.get('mcp_servers') or {})}
        # Explicit request base is frozen at test creation; no global configuration writes.
        from app.services.codex_delivery import provider_instructions
        developer=self.spec.get('assistant_instructions',self.spec['developer_instructions'])
        developer+='\n\n'+provider_instructions(reply_is_json=True,web_search_enabled=self.online,image_mode=self.image_mode)
        token=active_base.set({'mode':'custom','content':self.spec['instructions'],'fingerprint':self.id})
        try:
            self.thread_id=manager._start_thread(model=self.profile['model'],reasoning_effort=self.spec.get('effort') or 'medium',
                web_search_mode='live' if self.online else 'disabled',reasoning_summary='none',timeout=60,
                ephemeral=True,workdir=self.workspace,permission_profile=('mabobot-isolated-rw-offline' if self.workspace_access=='read_write' else 'mabobot-isolated-ro-offline'),
                approval_policy='never',runtime_workspace_roots=[self.workspace],
                developer_instructions=developer+'\n\n'+LAB_INSTRUCTIONS+'\n测试工作目录：'+_as_runtime_path(self.workspace,manager.use_wsl)+'\n交付目录：'+_as_runtime_path(self.workspace/'outputs',manager.use_wsl), image_mode=self.image_mode)
        finally:active_base.reset(token)

    def model_source(self, stage):
        from app.services.execution_authority import AdmissionSource, admission_source
        sender = next((m['sender'] for m in reversed(self.messages) if m['role']=='user' and m.get('sender')), '测试用户')
        source = AdmissionSource(self.chat_id, sender, f'web-test-{self.id}-{self.turn_number}-{stage}', 'assistant')
        return admission_source(source if self.chat_id else None)

    def run_turn(self,text,sender,uploads,kind='message',quote='',quote_sender=''):
        locked=False
        try:
            with self.model_source('trigger'):
                event,text,reason,locked=self.ingress.decide(text or ' '.join(item['name'] for item in uploads),sender,kind,quote,quote_sender)
            if self.cancelled.is_set():raise ValueError('测试已停止')
            if reason:
                with self.lock:
                    self.pending_context.append({'role':'user','name':'history_context','content':f'[{sender}]: {text}'})
                    self.messages.append({'turn':self.turn_number,'role':'notice','content':'未接话 · '+reason,'time':time.time()})
                    self.event('reply_skipped',{'reason':reason})
                return
            previous_files = {f['path']: f['modified_ns'] for f in self.files()}
            if self.manager is None:self.start_manager()
            if self.cancelled.is_set():raise ValueError('测试已停止')
            from app.services.codex_mention_tool import MentionPlan
            from app.services.schedule_context import ScheduleContext
            from app.services.scheduled_tasks import ScheduleActor
            self.mentions=MentionPlan(self.chat)
            self.schedule_context=ScheduleContext(self.chat,ScheduleActor(1,sender),uuid.uuid4().hex,text)
            turn_input=[]
            framework_messages=[]
            history, fingerprint = self.live_context()
            if fingerprint != self.last_context_fingerprint:
                framework_messages.append({'role':'user','name':'history_context','content':history})
            framework_messages.extend(self.pending_context)
            paths=[]
            for item in uploads:
                name=Path(item['name'].replace('\\','/')).name
                data=base64.b64decode(item['data'],validate=True)
                if len(data)>6*1024*1024:raise ValueError('单个上传文件不能超过6MB')
                path=self.inputs/(uuid.uuid4().hex+'-'+name[:120])
                path.parent.mkdir(exist_ok=True);path.write_bytes(data);paths.append(path)
                if path.suffix.lower() in {'.png','.jpg','.jpeg','.webp','.gif'}:
                    turn_input.append({'type':'localImage','path':_as_runtime_path(path,self.manager.use_wsl)})
            from app.services.chat_test_tools import WebTurnTools
            from app.services.codex_lens_tool import prompt_for
            self.turn_tools=WebTurnTools(self,paths,text)
            current=f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] [{sender}]: {text}'
            if paths:current+='\n本轮测试附件：\n'+'\n'.join(_as_runtime_path(p,self.manager.use_wsl) for p in paths)
            if self.browser_allowed and self.turn_tools.bindings:
                current+='\n'+prompt_for(self.turn_tools.bindings,self.turn_tools.lens.settings)
            framework_messages.append({'role':'user','content':current})
            from app.services.codex_proxy.client import render_chat_prompt
            prompt = render_chat_prompt(framework_messages,native_instructions=True,session_delivery=True,
                artifact_output_dir=_as_runtime_path(self.workspace/'outputs',self.manager.use_wsl),
                native_web_search_enabled=self.online,input_image_count=sum(i['type']=='localImage' for i in turn_input))
            turn_input.append({'type':'text','text':prompt})
            options={'model':self.profile['model']}
            if self.spec.get('effort'):options['effort']=self.spec['effort']
            if self.spec.get('schema'):options['outputSchema']=self.spec['schema']
            self.active_turn = ''
            self.turn_bound.clear()
            _,tracker=self.manager._start_tracked_turn(request_id='lab-'+uuid.uuid4().hex,thread_id=self.thread_id,
                turn_input=turn_input,turn_options=options,timeout=300,assistant_task=SimpleNamespace(
                    cancel_requested=self.cancelled,deadline=time.monotonic()+300,bind=lambda *a:None,
                    store=SimpleNamespace(update=lambda *a,**k:None),id=self.id,background=False))
            self.pending_context.clear()
            self.last_context_fingerprint = fingerprint
            if self.cancelled.is_set():raise ValueError('测试已停止')
            from app.services.codex_delivery import parse_delivery, collect_selected_files
            from app.services.wechat_content_review import get_wechat_content_review
            raw, declared = parse_delivery('\n'.join(tracker.final_messages or tracker.unclassified_messages), reply_is_json=True)
            try:
                legacy = json.loads(raw)
                if isinstance(legacy,str):raw=legacy
            except ValueError:
                pass
            settings=self.spec.get('output_settings',{'enabled':True})
            parts=self.ingress._format_response_parts(raw,settings)
            output_dir=self.workspace/'outputs'
            if declared is None:
                declared=[f['path'].removeprefix('outputs/') for f in self.files() if previous_files.get(f['path'])!=f['modified_ns']]
            selected=collect_selected_files(declared,generated_paths=getattr(tracker,'generated_image_paths',[]),
                                            output_dir=output_dir,use_wsl=self.manager.use_wsl)
            for index,item in enumerate(selected):
                with self.model_source(f'file-review-{index}'):
                    get_wechat_content_review().review_file(item['path'],chat_name=self.source_chat)
            self.turn_tools.finalize_images(selected)
            changed_files=[{'name':f['name'],'path':Path(f['path']).relative_to(self.workspace.resolve()).as_posix(),
                            'bytes':f['size'],'mime_type':f['mime_type']} for f in selected]
            if not parts and not changed_files:raise ValueError('模型没有返回可交付的回复或文件')
            with self.lock:
                for index,part in enumerate(parts or ['']):
                    self.messages.append({'turn':self.turn_number,'role':'assistant','content':part,'time':time.time(),
                        'mentions':list(self.mentions.users) if index==0 else [],
                        'files':changed_files if index==len(parts or [''])-1 else [],'usage':tracker.usage})
                self.event('reply_completed',{'parts':parts,'mentions':list(self.mentions.users),'files':changed_files})
            self.ingress.remember_reply(event,parts,self.ingress._get_user_role(self.source_chat))
        except Exception as exc:
            self.status='cancelled' if self.cancelled.is_set() else 'failed'
            self.error=str(exc);self.event('error',{'message':self.error})
        finally:
            if locked:self.ingress._clear_processing(self.source_chat)
            if self.turn_tools:
                self.turn_tools.close()
                self.turn_tools=None
            with self.lock:
                if self.status=='running':self.status='cancelled' if self.cancelled.is_set() else 'idle'
                self.updated=time.time()

    def files(self):
        items=[]
        directory = self.workspace/'outputs'
        if directory.is_symlink() or (directory.exists() and getattr(directory.lstat(), 'st_file_attributes', 0) & 0x400):return items
        for path in directory.rglob('*'):
            if len(items)>=100:break
            relative=path.relative_to(self.workspace).as_posix()
            try:resolved=safe_file(self.workspace,relative)
            except (ValueError, OSError):continue
            stat = resolved.stat()
            items.append({'name':resolved.name,'path':relative,'bytes':stat.st_size,'modified_ns':stat.st_mtime_ns})
        return items

    def public(self,after=0):
        with self.lock:
            return {'id':self.id,'chat_id':self.chat_id,'chat_name':self.source_chat,'variant':self.variant,'model':self.profile['model'],
                'status':self.status,'error':self.error,'history_mode':'live',
                'actions':copy.deepcopy(self.actions),'is_group':self.prepared.get('is_group',False),'image_mode':self.image_mode,'messages':copy.deepcopy(self.messages),'events':[copy.deepcopy(e) for e in self.events if e['id']>after],
                'cursor':self.event_sequence,'files':self.files(),'capabilities':CAPABILITIES,
                'context':self.context,'online':self.online,'workspace_access':self.workspace_access,'updated':self.updated}

    def cancel(self):
        self.cancelled.set()
        if self.review_bridge:
            threading.Thread(target=self.review_bridge.close,daemon=True,name='test-lab-cancel-review').start()
        if self.status == 'idle':self.status = 'cancelled'
        if self.manager and self.thread_id:
            with self.manager._turn_lock:
                turns=[(key,t) for key,t in self.manager._turns.items() if not t.completed.is_set()]
            for turn_id,_ in turns:self.manager._interrupt_turn(self.thread_id,turn_id)
        self.event('cancel_requested',{})

    def close(self):
        self.cancel()
        if self.closed:return
        if self.manager:self.manager.stop()
        with self.tool_lock:
            pass
        if self.ingress:self.ingress.close()
        if self.asset_engine:self.asset_engine.dispose()
        self.closed=True
        shutil.rmtree(self.root,ignore_errors=True)


class ChatTestLab:
    def __init__(self):
        self.lock=threading.RLock()
        self.sessions={}
        self.pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix='chat-test-lab')

    def create(self,prepared,source_store,variant='saved',online=False,recent_reader=None,listener_bus=None):
        with self.lock:
            if len(self.sessions)>=4:raise ValueError('最多保留4个测试会话，请先删除不需要的会话')
            root=Path(os.getenv('CHAT_TEST_LAB_ROOT','data/chat_test_lab')).resolve()/uuid.uuid4().hex
            session=LabSession(root,prepared,source_store,variant,online,recent_reader,listener_bus)
            self.sessions[session.id]=session
            self.pool.submit(session.initialize)
            return session.public()

    def get(self,identity):
        with self.lock:
            session=self.sessions.get(identity)
        if session is None:raise KeyError('测试会话不存在或服务已重启')
        return session

    def send(self,identity,text,sender,uploads,kind="message",quote="",quote_sender=""):
        session=self.get(identity)
        with session.lock:
            if session.status!='idle':raise ValueError('此会话尚未就绪或上一轮未完成；失败后请重置会话')
            if session.turn_number>=50:raise ValueError('测试会话已达50轮，请重置')
            session.status='running';session.error='';session.calls.clear()
            session.turn_number += 1
            session.messages.append({'turn':session.turn_number,'role':'user','content':text,'sender':sender,'time':time.time(),
                                     'kind':kind,'quote':quote,'quote_sender':quote_sender,'attachments':[i['name'] for i in uploads]})
            self.pool.submit(session.run_turn,text,sender,uploads,kind,quote,quote_sender)
        return session.public()

    def delete(self,identity):
        with self.lock:
            session=self.get(identity)
            with session.lock:
                if session.status in {'running','preparing'}:
                    session.cancel()
                    raise ValueError('已请求停止，待当前操作结束后再删除')
                session.status='deleting'
                self.sessions.pop(identity,None)
        session.close()

    def reset(self, identity):
        with self.lock:
            previous = self.get(identity)
            prepared, source, variant, online = previous.prepared, previous.source_store, previous.variant, previous.online
            self.delete(identity)
            return self.create(prepared, source, variant, online, previous.recent_reader, previous.listener_bus)

    def stop(self):
        for session in list(self.sessions.values()):session.cancel()
        self.pool.shutdown(wait=True,cancel_futures=True)
        for session in list(self.sessions.values()):session.close()


lab=ChatTestLab()
atexit.register(lab.stop)
