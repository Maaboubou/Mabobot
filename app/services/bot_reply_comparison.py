"""Real text generations with one frozen live-history snapshot and no delivery/tools."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import threading
import time
import uuid
from fastapi import HTTPException

comparison_lock = threading.Lock()


def prepare_comparison(request, db):
    from app.api.endpoints.assistant_prompts import build_preview, chat, base_source, row_data
    from app.models.chatbot_role import ChatBotRole, UserChatBotRole
    from app.assistant.runtime import get_assistant_handler
    from app.assistant.prompt_composer import normalize_legacy_prompt
    from app.services.assistant_prompt_service import read_rules
    from app.services.bot_base_instructions import read_base, read_chat_base, compose_chat_base
    from app.services.codex_profile_service import get_codex_profile_service
    from app.utils.plugin_config import get_plugin_setting

    user = chat(db, request.chat_id)
    handler = get_assistant_handler()
    if handler is None:
        raise HTTPException(409, '助手未运行，无法读取正式回复使用的聊天背景。请先启动助手。')
    binding = db.query(UserChatBotRole).filter(UserChatBotRole.user_id == user.id).first()
    role = db.get(ChatBotRole, binding.role_id) if binding else db.query(ChatBotRole).filter(
        ChatBotRole.name == str(get_plugin_setting('assistant', 'default_role', 'default'))).first()
    if not role:
        raise HTTPException(422, '此聊天未配置可用角色')
    if request.role_id is not None and request.role_id != role.id:
        raise HTTPException(409, '此聊天绑定角色已变化，请重新打开工作台')
    # The same reader/window/renderer as Handler._build_messages_array; freeze once.
    rows = handler.chat_log_manager.get_context_messages(user.chat_name, 50)
    rows = json.loads(json.dumps(list(rows), ensure_ascii=False, default=str))
    captured_at = datetime.now(timezone.utc).isoformat()
    source = base_source(db, user.id)
    if not source.get('available'):
        raise HTTPException(422, source.get('notice') or '无法读取基础指令原文')
    try:
        profile = get_codex_profile_service().get_profile(source['profile_id'])
    except Exception as exc:
        raise HTTPException(503, '无法读取测试 Profile，请检查 Codex 运行环境') from exc
    if not profile or not profile.get('available') or profile.get('model') != source['model']:
        raise HTTPException(409, 'Profile 不可用或模型已变化，请刷新后重试')
    role_data = row_data(db, ChatBotRole, role.id)
    output_keys = ('output_split_enabled','output_max_chars','output_max_count','output_strip_trailing_period','output_interval_seconds')
    saved_role = {'prompt': role_data['prompt'], **{k: role_data[k] for k in output_keys}}
    rules = read_rules(db)['texts']
    global_config, chat_config = read_base(db), read_chat_base(db, user.id)
    specs = []
    for is_draft in (False, True):
        role_draft = {**saved_role, **(request.draft if is_draft else {})}
        variant = request.model_copy(update={'role_id':role.id, 'history':'', 'use_chat_history':True,
            'draft':role_draft, 'rule_draft':{**rules, **(request.rule_draft if is_draft else {})},
            'lens_draft':request.lens_draft if is_draft else None})
        preview = build_preview(variant, db, frozen_rows=rows, include_chat_base=False)
        global_variant = dict(global_config)
        if is_draft and request.global_base_mode is not None:
            global_variant.update(mode=request.global_base_mode,
                                  content=request.global_base_content if request.global_base_mode=='custom' else '')
            if request.global_base_mode=='custom' and not request.global_base_content.strip():
                raise HTTPException(422, '全局自定义基础指令不能为空')
        config = dict(chat_config)
        if is_draft and request.chat_base_mode is not None:
            config.update(mode=request.chat_base_mode, content=request.chat_base_content)
        if config['mode']=='custom' and not config['content'].strip():
            raise HTTPException(422, '聊天自定义基础指令不能为空')
        try:
            composed = compose_chat_base(config, global_variant, source, normalize_legacy_prompt(role_draft['prompt']))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        blocks = [b for b in preview['blocks'] if not (config['mode']=='persona' and b['id']=='role')]
        developer = '\n\n'.join(b['content'] for b in blocks if b['role'] in {'system','developer'})
        inputs = [{'role':b['role'], 'content':b['content']} for b in blocks if b['role']=='user']
        if specs:
            # Same input, including time and history, on both sides.
            inputs = specs[0]['input_messages']
        schema = json.loads(next(b['content'] for b in blocks if b['id']=='delivery_schema'))
        spec = {'output_settings': {'enabled':bool(role_draft.get('output_split_enabled')),
                    'max_count':role_draft.get('output_max_count',3),'strip_trailing_period':role_draft.get('output_strip_trailing_period',True)},
                'model':profile['model'], 'instructions':composed['content'] or source['content'],
                'developer_instructions':developer, 'assistant_instructions':'\n\n'.join(b['content'] for b in blocks if b['role'] in {'system','developer'} and b['id']!='delivery'), 'input_messages':inputs,
                'input':request.content, 'schema':schema,
                'effort':profile.get('reasoning_effort'),
                'service_tier':'fast' if profile.get('fast_mode') else 'default',
                'subscription':profile.get('auth_type')=='chatgpt'}
        if spec['effort'] in (None,'inherit'): spec.pop('effort')
        specs.append(spec)
    history = next((m['content'] for m in specs[0]['input_messages'] if '以下是历史资料' in m['content']), '')
    return {'profile':profile, 'specs':specs, 'decision_rules':[rules,{**rules,**request.rule_draft}], 'personas':[normalize_legacy_prompt(saved_role['prompt']),normalize_legacy_prompt({**saved_role,**request.draft}['prompt'])], 'context':{'chat_name':user.chat_name,
        'captured_at':captured_at, 'message_count':len(rows), 'history':history,
        'fingerprint':hashlib.sha256(json.dumps(specs[0]['input_messages'],ensure_ascii=False).encode()).hexdigest()},
        'notice':'两次真实模型调用，共用正式回复读取器生成的近期聊天背景。独立测试会话，不发送微信、不写入聊天历史；工具与正式旧线程的隐含上下文不参与。'}


def run_variant(bridge, profile, spec):
    from app.services.codex_direct import MAX_BODY
    started = time.monotonic()
    if len(json.dumps(spec, ensure_ascii=False).encode()) > MAX_BODY // 2:
        return {'status':'failed','error':'测试输入过长，请缩短草稿','text':''}
    try:
        result = bridge.request('generate', {'attempt_id':uuid.uuid4().hex,
            'launcher':profile['wrapper_path'], 'spec':{**{k:v for k,v in spec.items() if k not in {'output_settings','assistant_instructions'}},'deadline':time.time()+180}}, timeout=225)
        text = result.get('text','')
        try:
            parsed = json.loads(text)
            reply = parsed.get('_mabobot_reply', parsed)
            if isinstance(reply,str):reply=json.loads(reply)
            messages = reply.get('messages')
            if isinstance(messages,list) and all(isinstance(m,str) for m in messages):
                text = '\n\n'.join(messages)
        except (ValueError,AttributeError):
            pass
        return {'status':result.get('status','failed'), 'text':text,
                'error':result.get('error') or result.get('error_code') or '',
                'usage':result.get('usage'), 'actual_model':result.get('actual_model'),
                'elapsed_seconds':round(time.monotonic()-started,1),
                'fingerprint':hashlib.sha256(json.dumps(spec,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
    except Exception:
        return {'status':'failed','text':'','error':'测试调用失败或超时；未自动重试，请检查 Profile 状态',
                'elapsed_seconds':round(time.monotonic()-started,1)}


def run_comparison(prepared):
    from app.services.opencode_process_bridge import ProcessBridge
    from app.services.codex_direct_runtime import worker_command
    try:
        bridge = ProcessBridge(worker_command())
    except Exception as exc:
        raise HTTPException(503, '无法启动独立测试进程，请检查 Codex 运行环境') from exc
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run_variant, bridge, prepared['profile'], spec) for spec in prepared['specs']]
            results = [future.result() for future in futures]
        return {'saved':results[0], 'draft':results[1], 'context':prepared['context'],
                'model':prepared['profile']['model'], 'profile_id':prepared['profile']['name'],
                'notice':prepared['notice'], 'same_configuration':prepared['specs'][0]==prepared['specs'][1]}
    finally:
        bridge.close()
