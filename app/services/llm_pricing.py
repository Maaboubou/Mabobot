"""Usage normalization and reproducible, route-specific token pricing.

Prices are snapshots verified 2026-09-07. No network calls on the request path.
Amounts and rates are decimal strings; unknown is never coerced to zero.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from urllib.parse import urlsplit

VERSION = '2026-09-07'
OPENAI_URL = 'https://developers.openai.com/api/docs/pricing'
DEEPSEEK_URL = 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
# USD / million tokens: uncached input, output, cache read, cache write.
OPENAI = {
    'gpt-5.6-sol': ('4', '20', '.4', '5'),
    'gpt-5.6-terra': ('2', '12', '.2', '2.5'),
    'gpt-5.6-luna': ('.2', '1.2', '.02', '.25'),
    'gpt-6-astra': ('10', '50', '1', '12.5'),
}
RATE_KEYS = ('input_cost_per_token', 'output_cost_per_token',
             'cache_read_input_token_cost', 'cache_creation_input_token_cost')
BUCKETS = ('uncached_input_tokens', 'completion_tokens', 'cached_tokens', 'cache_write_tokens')


def read(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def raw_usage(value):
    """Keep numeric usage metadata only, never request/response content or credentials."""
    if hasattr(value, 'model_dump'):
        value = value.model_dump()
    elif hasattr(value, '__dict__'):
        value = vars(value)
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if isinstance(item, dict):
            result[str(key)] = raw_usage(item)
        elif isinstance(item, (int, float, bool)) and decimal(item) is not None:
            result[str(key)] = item
        elif key in {'source', 'currency', 'cost_currency'} and isinstance(item, str):
            result[key] = item[:160]
    return result


def normalize_usage(raw, provider=''):
    def first(*paths):
        for path in paths:
            v = raw
            for key in path.split('.'):
                v = read(v, key)
            d = decimal(v)
            if d is not None and d == int(d):
                return int(d)
        return None
    prompt = first('prompt_tokens', 'input_tokens', 'inputTokens', 'promptTokenCount')
    output = first('completion_tokens', 'output_tokens', 'outputTokens', 'candidatesTokenCount')
    cached = first('prompt_tokens_details.cached_tokens', 'input_tokens_details.cached_tokens',
                   'cached_tokens', 'cached_input_tokens', 'cachedInputTokens', 'cache_read_input_tokens',
                   'prompt_cache_hit_tokens', 'cachedContentTokenCount')
    written = first('prompt_tokens_details.cache_write_tokens', 'input_tokens_details.cache_write_tokens',
                    'cache_write_tokens', 'cache_creation_input_tokens')
    reasoning = first('completion_tokens_details.reasoning_tokens', 'output_tokens_details.reasoning_tokens',
                      'reasoning_tokens', 'reasoningOutputTokens', 'thoughtsTokenCount')
    # Native Gemini candidates exclude thoughts; OpenAI output already includes them.
    if read(raw, 'candidatesTokenCount') is not None and read(raw, 'completion_tokens') is None and output is not None:
        output += reasoning or 0
    native_anthropic = (read(raw, 'prompt_tokens') is None and read(raw, 'input_tokens') is not None
                        and (provider == 'anthropic' or read(raw, 'cache_read_input_tokens') is not None
                             or read(raw, 'cache_creation_input_tokens') is not None))
    if native_anthropic and prompt is not None:
        prompt += (cached or 0) + (written or 0)
    valid = prompt is not None and output is not None
    if prompt is not None and (cached or 0) + (written or 0) > prompt:
        valid = False
    total = prompt + output if prompt is not None and output is not None else first('total_tokens', 'totalTokens', 'totalTokenCount')
    result = {'valid_for_pricing': valid, 'estimated': bool(read(raw, 'estimated', False)),
              'source': str(read(raw, 'source', 'provider_usage'))[:160]}
    for key, value in {'prompt_tokens': prompt, 'completion_tokens': output, 'total_tokens': total,
                       'cached_tokens': cached, 'cache_write_tokens': written, 'reasoning_tokens': reasoning,
                       'uncached_input_tokens': max(0, prompt - (cached or 0) - (written or 0)) if prompt is not None else None}.items():
        if value is not None:
            result[key] = value
    if any(first(path) for path in ('prompt_tokens_details.audio_tokens', 'input_tokens_details.audio_tokens', 'completion_tokens_details.audio_tokens', 'output_tokens_details.audio_tokens')):
        result['unsupported_dimension'] = 'audio_tokens'
    if read(raw, 'cache_data_available') is False:
        result['cache_data_available'] = False
    return result


def pricing_snapshot(config, model, usage, *, codex=False, at=None, catalog=None, catalog_version=None):
    model = str(model or '').split('/')[-1]
    if model == 'gpt-5.6':
        model = 'gpt-5.6-sol'
    provider = str(config.get('custom_llm_provider') or config.get('provider') or str(config.get('model', '')).split('/')[0])
    base = str(config.get('api_base') or '')
    host = urlsplit(base).hostname or ''
    mode = 'api_equivalent' if codex else config.get('billing_mode', 'auto')
    snapshot = {'model': model, 'billing_mode': mode, 'source': 'none', 'version': VERSION,
                'currency': None, 'rates': {}, 'tier': 'standard', 'source_url': None, 'priced_at': (at or datetime.now().astimezone()).isoformat()}
    if mode in {'free', 'included'}:
        return {**snapshot, 'currency': config.get('cost_currency') or 'USD', 'source': 'connection_config'}
    if mode == 'unknown':
        return snapshot
    # User's Codex policy always uses the model's official standard API price.
    if not codex and any(config.get(k) is not None for k in RATE_KEYS):
        snapshot.update(currency=config.get('cost_currency'), source='connection_config',
                        rates={b: str(decimal(config[k])) for b, k in zip(BUCKETS, RATE_KEYS) if decimal(config.get(k)) is not None})
        snapshot['version'] = hashlib.sha256(json.dumps(snapshot['rates'], sort_keys=True).encode()).hexdigest()[:12]
        return snapshot
    official_openai = codex or host == 'api.openai.com' or (not base and provider in {'openai', model})
    if official_openai and model in OPENAI:
        rates = list(map(Decimal, OPENAI[model]))
        if usage.get('prompt_tokens', 0) > 272_000:
            rates = [rates[0] * 2, rates[1] * Decimal('1.5'), rates[2] * 2, rates[3] * 2]
            snapshot['tier'] = 'long_context'
        if not codex:
            service_tier = config.get('service_tier')
            factor = Decimal('2') if service_tier in {'fast', 'priority'} else Decimal('.5') if service_tier == 'flex' else Decimal('1')
            rates = [rate * factor for rate in rates]
            if service_tier:
                snapshot['service_tier'] = service_tier
        snapshot.update(currency='USD', source='official_snapshot', source_url=OPENAI_URL,
                        rates={b: str(r / 1_000_000) for b, r in zip(BUCKETS, rates)})
        return snapshot
    if codex:
        return snapshot  # No invented mapping from an unknown subscription model.
    official_deepseek = host == 'api.deepseek.com' or (not base and provider == 'deepseek')
    if official_deepseek and model in {'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp', 'deepseek-v4-pro', 'deepseek-chat', 'deepseek-reasoner'}:
        at = at or datetime.now().astimezone()
        china = at.astimezone(timezone(timedelta(hours=8)))
        peak = china.weekday() < 5 and (9 <= china.hour < 12 or 14 <= china.hour < 18)
        rates = list(map(Decimal, ('4.5', '13.5', '.15') if model == 'deepseek-v4-pro' else ('1.5', '4.5', '.05')))
        snapshot.update(currency='CNY', source='official_snapshot', source_url=DEEPSEEK_URL,
                        tier='peak' if peak else 'off_peak',
                        rates={b: str(r * (2 if peak else 1) / 1_000_000) for b, r in zip(BUCKETS, rates)})
        return snapshot
    # A price for the same model on another endpoint is not this connection's price.
    known_host = host in {'generativelanguage.googleapis.com', 'api.anthropic.com', 'api.openai.com', 'openrouter.ai', 'api.moonshot.cn', 'api.moonshot.ai'}
    if (not base or known_host) and catalog:
        name = str(config.get('model') or model)
        entry = catalog.get(name) or catalog.get(f'{provider}/{model}') or catalog.get(model)
        if entry:
            snapshot.update(currency='USD', source='litellm_catalog', version=catalog_version or 'unknown',
                            source_url='https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json',
                            rates={b: str(decimal(entry[k])) for b, k in zip(BUCKETS, RATE_KEYS) if decimal(entry.get(k)) is not None})
            # Unsupported price dimensions must not silently use the short-context price.
            if any('above' in key for key in entry) and usage.get('prompt_tokens', 0) > 128_000:
                snapshot['rates'] = {}
                snapshot['reason'] = 'tier_requires_configuration'
    return snapshot


def price_usage(config, model, usage, *, codex=False, at=None, catalog=None, catalog_version=None):
    snapshot = pricing_snapshot(config, model, usage, codex=codex, at=at, catalog=catalog, catalog_version=catalog_version)
    result = {'amount': None, 'currency': snapshot['currency'], 'status': 'unknown', 'snapshot': snapshot}
    mode = snapshot['billing_mode']
    if mode in {'free', 'included'}:
        return {**result, 'amount': '0', 'status': mode}
    if usage.get('unsupported_dimension'):
        return {**result, 'reason': 'unsupported_usage_dimension'}
    if not usage.get('valid_for_pricing') or not snapshot['currency']:
        return {**result, 'reason': 'missing_usage_or_currency'}
    amount = Decimal(0)
    for bucket in BUCKETS:
        tokens = usage.get(bucket, 0)
        rate = decimal(snapshot['rates'].get(bucket))
        if rate is None and (tokens or bucket in BUCKETS[:2]):
            return {**result, 'reason': snapshot.get('reason', 'missing_price')}
        amount += Decimal(tokens) * (rate or Decimal(0))
    return {**result, 'amount': format(amount, 'f'), 'status': 'api_equivalent' if codex else 'estimated',
            'notes': ['cache_not_reported'] if 'cached_tokens' not in usage or usage.get('cache_data_available') is False else []}
