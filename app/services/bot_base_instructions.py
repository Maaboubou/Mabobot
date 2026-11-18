"""Versioned administrator-owned base instructions and per-request isolation."""
from contextvars import ContextVar
import hashlib
import json
from datetime import datetime, timezone
from app.services.assistant_prompt_service import _lock

KEY = 'BOT_BASE_INSTRUCTIONS_V1'
active_base = ContextVar('bot_base_instructions', default=None)


def read_base(db=None):
    if db is None:
        from app.models.base import SessionLocal
        with SessionLocal() as session:
            return read_base(session)
    from app.models.setting import Setting
    row = db.query(Setting).filter(Setting.key == KEY).first()
    data = row.get_value() if row else {}
    data = data if isinstance(data, dict) else {}
    content = data.get('content') or ''
    mode = 'custom' if content else 'default'
    return {**data, 'version': int(data.get('version', 0)), 'mode': mode,
            'content': content, 'history': data.get('history', []),
            'fingerprint': hashlib.sha256((mode + '\n' + content).encode()).hexdigest(),
            'default_available': False,
            'notice': '默认原文按所选聊天的 Profile 从来源接口读取。自定义会替换基础指令。'}


def update_base(db, content, version):
    from app.models.setting import Setting
    if content is not None and (not content.strip() or len(content) > 100000):
        raise ValueError('自定义基础指令须为 1–100000 字符')
    with _lock:
        current = read_base(db)
        if current['version'] != version:
            raise ValueError('配置已被更新，请刷新后重试；草稿可保留')
        data = {'version': version + 1, 'content': content.strip() if content else '',
                'history': (current['history'] + [{k: current[k] for k in ('version', 'content', 'mode')}])[-20:],
                'updated_at': datetime.now(timezone.utc).isoformat()}
        row = db.query(Setting).filter(Setting.key == KEY).first()
        if row:
            changed = db.query(Setting).filter(Setting.id == row.id, Setting.value == row.value).update(
                {'value': json.dumps(data, ensure_ascii=False)}, synchronize_session=False)
            if not changed:
                db.rollback()
                raise ValueError('配置已被更新，请刷新后重试')
        else:
            db.add(Setting(key=KEY, value=json.dumps(data, ensure_ascii=False), category='assistant'))
        db.commit()
        db.expire_all()
        return read_base(db)


def base_parameters():
    state = active_base.get() or {}
    return {'baseInstructions': state['content']} if state.get('mode') in {'custom', 'persona'} else {}

ADAPTER_VERSION = 'wechat-persona-v1'
ADAPTER = '''【微信聊天适配说明】
当前交互场景为微信聊天。下面的角色设定负责对外身份、称呼、语气、语言风格和回复节奏。
原生指令中的默认编程代理身份、职业口吻、协作表达和进度汇报习惯，仅在当前任务确实需要时适用；普通聊天不主动介绍编程代理身份，不机械输出计划、进度、标题和总结。
保留原生指令中的任务理解、工具调用、事实核验、错误处理与完成任务的能力要求。
角色设定不授予工具权限，不改变宿主输出协议，也不覆盖平台规则。工具权限、交付格式和执行边界继续由宿主配置与应用指令控制。
原生文本保持原样；本说明仅明确聊天场景下身份与表达的适用范围。'''


def read_chat_base(db, chat_id):
    from app.models.setting import Setting
    row = db.query(Setting).filter(Setting.key == f'{KEY}_CHAT_{int(chat_id)}').first()
    data = row.get_value() if row else {}
    return {'version': 0, 'mode': 'inherit', 'content': '', 'history': [], **(data or {}),
            'chat_id': int(chat_id), 'adapter': ADAPTER, 'adapter_version': ADAPTER_VERSION}


def update_chat_base(db, chat_id, mode, content, version):
    from app.models.setting import Setting
    if mode not in {'inherit', 'default', 'persona', 'custom'}:
        raise ValueError('基础指令模式无效')
    if mode == 'custom' and (not content or not content.strip() or len(content) > 100000):
        raise ValueError('自定义基础指令须为 1–100000 字符')
    with _lock:
        current = read_chat_base(db, chat_id)
        if current['version'] != version:
            raise ValueError('配置已被更新，请刷新后重试；草稿可保留')
        data = {'version': version + 1, 'mode': mode, 'content': content.strip() if mode == 'custom' else '',
                'history': (current['history'] + [{k: current[k] for k in ('version', 'mode', 'content')}])[-20:]}
        key = f'{KEY}_CHAT_{int(chat_id)}'
        row = db.query(Setting).filter(Setting.key == key).first()
        if row:
            changed = db.query(Setting).filter(Setting.id == row.id, Setting.value == row.value).update(
                {'value': json.dumps(data, ensure_ascii=False)}, synchronize_session=False)
            if not changed:
                db.rollback()
                raise ValueError('配置已被更新，请刷新后重试')
        else:
            db.add(Setting(key=key, value=json.dumps(data, ensure_ascii=False), category='assistant'))
        db.commit(); db.expire_all()
        return read_chat_base(db, chat_id)


def compose_chat_base(config, global_base, source=None, persona=''):
    """One pure composer shared by runtime and draft previews."""
    mode = config['mode']
    result = {**config, 'configured_mode': mode}
    if mode == 'inherit':
        result.update(mode=global_base['mode'], content=global_base['content'], inherited_version=global_base['version'])
    elif mode == 'default':
        result['content'] = ''
    elif mode == 'persona':
        if not source or not source.get('available') or not source.get('content'):
            raise ValueError('聊天基础指令合成失败：无法读取当前 Profile / 模型的原生指令')
        if not persona.strip():
            raise ValueError('聊天基础指令合成失败：当前聊天没有可用的人设')
        result.update(source=source, persona=persona, adapter=ADAPTER, adapter_version=ADAPTER_VERSION,
                      content=source['content'] + '\n\n' + ADAPTER + '\n\n【当前聊天角色设定】\n' + persona)
    result['fingerprint'] = hashlib.sha256((result['mode'] + '\n' + result['content']).encode()).hexdigest()
    return result


def resolve_chat_base(request, global_base):
    user_id = request.get('codex_chat_user_id')
    if not user_id:
        return global_base
    from app.models.base import SessionLocal
    with SessionLocal() as db:
        config = read_chat_base(db, user_id)
    source = None
    personas = [m for m in request.get('messages', []) if m.get('name') == 'mabobot_persona' and m.get('role') == 'system']
    if config['mode'] == 'persona':
        from app.services.codex_profile_service import get_codex_profile_service
        service = get_codex_profile_service()
        profile = service.resolve_assistant_profile_id(request.get('codex_runtime_profile'))
        try:
            source = service._run('read-base-instructions', {'name': profile, 'model': request.get('model')})
        except Exception as exc:
            raise ValueError('聊天基础指令合成失败：原文读取失败，请在工作台检查来源') from exc
    return compose_chat_base(config, global_base, source, '\n\n'.join(m['content'] for m in personas))


def requires_app_server(user_id):
    """Exec fallback cannot preserve per-thread baseInstructions."""
    from app.models.base import SessionLocal
    with SessionLocal() as db:
        mode = read_chat_base(db, user_id)['mode'] if user_id else 'inherit'
        return mode != 'inherit' or read_base(db)['mode'] == 'custom'
