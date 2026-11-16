"""Engine-independent ChatGPT subscription model cards."""
import copy


def is_subscription(card):
    return card.get('kind') == 'subscription'


def normalize_card(card):
    result = copy.deepcopy(card)
    result.update(kind='subscription', execution_policy='profile_generation', engine_policy=card.get('engine_policy') or 'follow',
                  custom_llm_provider='chatgpt_subscription',
                  reasoning_effort=card.get('reasoning_effort') or card.get('codex_reasoning_effort') or 'medium')
    for key in ('provider', 'api_key', 'api_base', 'codex_profile_id', 'codex_reasoning_effort',
                'codex_web_search', 'codex_isolated_workdir', 'codex_sandbox', 'enable_web_search'):
        result.pop(key, None)
    return result


def migrate(models, mappings):
    """Preserve model parameters and every fallback reference while renaming old IDs."""
    models, mappings = copy.deepcopy(models), copy.deepcopy(mappings)
    renamed = {}
    for identity, card in list(models.items()):
        provider = card.get('custom_llm_provider') or card.get('provider')
        if provider not in {'local_codex', 'local_codex_cli'} and not is_subscription(card):
            continue
        if card.get('codex_profile_id'):
            raise ValueError('先确认旧模型卡绑定的账号类型：' + identity)
        target = 'chatgpt-' + identity[6:] if identity.startswith('codex-') else identity
        if target != identity and target in models:
            raise ValueError('模型 ID 已存在：' + target)
        models.pop(identity)
        models[target] = normalize_card(card)
        renamed[identity] = target
    for tasks in mappings.values():
        for mapping in tasks.values():
            if not isinstance(mapping, dict):
                continue
            for key in ('primary', 'fallback', 'fallbacks'):
                value = mapping.get(key)
                if isinstance(value, str):
                    mapping[key] = renamed.get(value, value)
                elif isinstance(value, list):
                    mapping[key] = [renamed.get(v, v) for v in value]
    return models, mappings, renamed


def test_card(manager, model_id):
    import uuid
    from app.services.execution_service import get_execution_service
    from app.services.execution_contracts import TaskContext, InputEnvelope, canonical
    service = get_execution_service()
    route = service.config.resolve(chat_id='', component='engine_probe', task_type='subscription',
        models=manager.config['models'], mapping={'primary': model_id, 'mode': 'direct'})
    context = TaskContext.create(timeout=120, request_id=uuid.uuid4().hex, source_kind='system', source_chat_id='',
        actor_id='administrator', component='engine_probe', task_type='subscription', delivery_chat_id='',
        delivery_owner='', authorization_ref='engine-probe:subscription:' + model_id, permission_revision='1')
    service.store.admit(context, route)
    return service.execute(context.run_id, InputEnvelope('Reply exactly OK.', canonical('Connection test')))


def call_card(manager, card, messages, params, *, context, reasoning_effort='inherit',
              timeout=0, output_schema=None, input_files=None):
    """Execute one subscription node; the caller retains its API fallback chain."""
    import json
    import time
    import uuid
    from dataclasses import replace
    from app.services.execution_authority import make_context
    from app.services.execution_service import get_execution_service
    from app.services.execution_contracts import ExecutionError, InputEnvelope, canonical
    if not context:
        raise ExecutionError('source_missing', '订阅模型调用需要业务任务来源')
    started = time.monotonic()
    effective = normalize_card(card)
    for key in ('model', 'reasoning_effort', 'response_format', 'temperature', 'max_tokens'):
        if params.get(key) is not None:
            effective[key] = copy.deepcopy(params[key])
    if reasoning_effort != 'inherit':
        effective['reasoning_effort'] = reasoning_effort
    model_id = next((key for key, value in manager.config['models'].items() if value == card), 'subscription')
    task = make_context(context['plugin'], context['task'],
        timeout=int(timeout or params.get('timeout') or card.get('timeout') or 600), delivery=False)
    task = replace(task, request_id=task.request_id + ':' + uuid.uuid4().hex)
    service = get_execution_service()
    response_format = effective.get('response_format') or {}
    schema = output_schema or (response_format.get('json_schema') or {}).get('schema')
    contract = {'schema': schema} if schema else ({'type': 'json'} if response_format.get('type') == 'json_object' else None)
    route = service.config.resolve(chat_id=task.source_chat_id, component=task.component,
        task_type=task.task_type, models={model_id: effective}, mapping={'primary': model_id, 'mode': 'direct'},
        output_contract=contract)
    # This seam is a pure-generation node even when the component has an agent route.
    route = replace(route, mode='direct', tools=(), max_attempts=1)
    instructions = '\n\n'.join(str(m.get('content') or '') for m in messages if m.get('role') in {'system', 'developer'})
    data = [m for m in messages if m.get('role') not in {'system', 'developer'}]
    if any(isinstance(m.get('content'), list) and any(p.get('type') != 'text' for p in m['content']) for m in data):
        raise ExecutionError('capability_unavailable', '订阅模型直接生成暂不支持多模态输入，请使用支持该输入的任务路由')
    data = data[0]['content'] if len(data) == 1 and data[0].get('role') == 'user' and isinstance(data[0].get('content'), str) else canonical(data)
    if contract:
        instructions += '\nReturn valid JSON only.'
        if schema:
            instructions += '\nRequired JSON schema: ' + canonical(schema)
    envelope = InputEnvelope(instructions or 'Follow the user request and return the requested result.',
        canonical(data), assets_json=canonical(input_files or []))
    run_id, _ = service.admit(task, route)
    result = service.execute(run_id, envelope, emit_telemetry=False)
    response = {'model': result.actual_model or result.target_model, 'backend': result.engine,
        'engine': result.engine, 'profile_id': result.profile_id, 'run_id': result.run_id,
        'usage': json.loads(result.usage_json), 'choices': [{'message': {'content': result.text}}]}
    if result.status != 'completed':
        error = ExecutionError(result.error_code or result.status, result.error or '订阅模型调用失败')
        error.billing_response = response
        raise error
    return response, time.monotonic() - started, result.text.strip()
