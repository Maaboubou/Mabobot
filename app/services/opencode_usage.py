"""Translate pinned OpenCode/ACP counters into the console's inclusive totals."""
from __future__ import annotations

import math


def token_count(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def normalize_usage(value):
    if not isinstance(value, dict):
        return None
    # ACP and native OpenCode report disjoint input/cache and output/reasoning
    # buckets. The host's prompt/completion totals include those subcategories.
    if 'inputTokens' in value:
        raw_input, raw_output = value.get('inputTokens'), value.get('outputTokens')
        read, write = value.get('cachedReadTokens', 0), value.get('cachedWriteTokens', 0)
        reasoning = value.get('thoughtTokens', 0)
        total = value.get('totalTokens')
    elif 'input' in value:
        raw_input, raw_output = value.get('input'), value.get('output')
        cache = value.get('cache') or {}
        read, write = cache.get('read', 0), cache.get('write', 0)
        reasoning, total = value.get('reasoning', 0), None
    else:
        # Already normalized; also accept standard provider usage from direct calls.
        result = {key: number for key, item in value.items()
                  if key in {'prompt_tokens', 'completion_tokens', 'input_tokens', 'output_tokens',
                             'total_tokens', 'cached_tokens', 'cache_write_tokens', 'cache_miss_tokens',
                             'reasoning_tokens', 'cache_hit_rate'}
                  and (number := token_count(item)) is not None}
        for target, source in [('prompt_tokens', 'input_tokens'), ('completion_tokens', 'output_tokens')]:
            if target not in result and source in result:
                result[target] = result[source]
        if 'total_tokens' not in result and all(key in result for key in ('prompt_tokens', 'completion_tokens')):
            result['total_tokens'] = result['prompt_tokens'] + result['completion_tokens']
        return result or None
    if any(token_count(item) is None for item in (raw_input, raw_output, read, write, reasoning)):
        return None
    prompt, completion = raw_input + read + write, raw_output + reasoning
    return {'prompt_tokens': prompt, 'input_tokens': prompt,
            'completion_tokens': completion, 'output_tokens': completion,
            'total_tokens': token_count(total) if token_count(total) is not None else prompt + completion,
            'cached_tokens': read, 'cache_write_tokens': write, 'cache_miss_tokens': raw_input + write,
            'reasoning_tokens': reasoning,
            **({'cache_hit_rate': read / prompt} if prompt else {})}


def context_usage(update):
    used, size = token_count(update.get('used')), token_count(update.get('size'))
    if used is None or size is None or size == 0:
        return {}
    return {'context_tokens': used, 'model_context_window': size, 'context_source': 'opencode_acp'}
