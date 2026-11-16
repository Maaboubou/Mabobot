"""Per-attempt usage, pricing and sanitized business request/response history."""
import json


def attempt_pricing(candidate, result, usage):
    from app.services.llm_pricing import normalize_usage, price_usage
    import litellm
    from importlib.metadata import version
    connection = json.loads(candidate.profile_config_json).get('connection') or {}
    subscription = candidate.engine in {'codex', 'opencode'} and connection.get('auth_type') != 'api_key'
    provider = connection.get('catalog_provider') or ('anthropic' if connection.get('protocol') == 'anthropic' else 'openai')
    config = {'model': result.actual_model or result.target_model, 'provider': provider,
              'api_base': connection.get('base_url', '')}
    if candidate.engine == 'api':
        from app.services.llm_manager import get_llm_manager
        from app.services.execution_contracts import digest
        manager = get_llm_manager()
        card = manager.config.get('models', {}).get(candidate.card_id)
        if not card or digest(card) != candidate.generation:
            return {'amount': None, 'currency': None, 'status': 'unknown',
                    'reason': 'configuration_unavailable', 'snapshot': {'source': 'none'}}
        config = dict(card)
        config['api_base'] = manager._resolve_env(config.get('api_base') or '')
        provider = config.get('custom_llm_provider') or config.get('provider') or 'openai'
    normalized = normalize_usage(usage, provider)
    pricing = price_usage(config, result.actual_model or result.target_model, normalized, codex=subscription,
                          catalog=litellm.model_cost, catalog_version=version('litellm'))
    # ACP gives a turn sum. Multiple short requests must not be billed as one
    # long-context request, matching the existing Codex accounting policy.
    if subscription and normalized.get('prompt_tokens', 0) > 272_000:
        pricing.update(amount=None, status='unknown', reason='context_tier_unavailable')
    return pricing


def record_attempt(context, route, candidate, result, duration):
    from app.services.llm_manager import get_llm_manager
    from app.services.execution_service import get_execution_service
    manager = get_llm_manager()
    usage = json.loads(result.usage_json) or {}
    if result.engine == 'opencode':
        from app.services.opencode_usage import normalize_usage
        usage = normalize_usage(usage) or {}
    success = result.status in {'completed', 'skipped'}
    chat = context.source_chat_id
    if chat.startswith('wechat:'):
        from app.services.execution_authority import chat_name
        from app.services.execution_contracts import ExecutionError
        try:
            chat = chat_name(context)
        except (ExecutionError, ValueError):
            pass  # Deleted chats retain their immutable identity in history.
    subject = {'key': context.source_chat_id or 'system', 'name': chat or '系统任务', 'kind': context.source_kind}
    pricing = attempt_pricing(candidate, result, usage)
    metadata = {'task_id': context.task_id, 'run_id': context.run_id, 'attempt_id': result.attempt_id,
        'engine': result.engine, 'profile_id': result.profile_id, 'mode': route.mode, 'route_revision': route.revision,
        'channel': result.channel, 'usage_source': result.usage_source, 'error_code': result.error_code,
        'history_mode': 'full', 'trace_id': result.attempt_id, 'chat_name': chat, 'pricing': pricing}
    manager.usage_service.record(task=context.component + '.' + context.task_type, subject=subject,
        model=result.actual_model or result.target_model, success=success, usage=usage, duration=duration,
        pricing=pricing, request_id=result.attempt_id, logical_id=context.run_id,
        connection={'id': candidate.card_id, 'provider': result.engine, 'profile_id': result.profile_id, 'channel': result.channel},
        raw={'usage_source': result.usage_source}, scope='engine_attempt')
    row = get_execution_service().store.get(context.run_id)
    envelope = json.loads(row['input_json']) if row.get('input_json') else {}
    messages = []
    if envelope.get('instructions'):
        messages.append({'role': 'system', 'content': envelope['instructions']})
    data = json.loads(envelope.get('input_json', 'null'))
    if isinstance(data, list) and all(isinstance(m, dict) and 'role' in m and 'content' in m for m in data):
        messages.extend(data)
    elif data is not None:
        messages.append({'role': 'user', 'content': data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)})
    manager._record_call_history(context.component, context.task_type, candidate.card_id, messages, result.text, duration,
        result.actual_model or result.target_model, int(usage.get('total_tokens') or 0), success,
        error=result.error or result.error_code, token_usage=usage, metadata=metadata,
        response_attachments=json.loads(result.assets_json))
