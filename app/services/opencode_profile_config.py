"""Public connection metadata and private, immutable API credentials for OpenCode."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from app.services.execution_contracts import ExecutionError, canonical

PACKAGES = {
    'responses': '@opencode/ai/providers/openai-compatible-responses',
    'chat_completions': '@opencode/ai/providers/openai-compatible',
    'anthropic': '@opencode/ai/providers/anthropic-compatible',
}
# Catalog choices are presentation metadata; the selected protocol determines the adapter.
PROVIDERS = {
    'openai': ('OpenAI', 'https://api.openai.com/v1', 'responses'),
    'anthropic': ('Anthropic', 'https://api.anthropic.com/v1', 'anthropic'),
    'deepseek': ('DeepSeek', 'https://api.deepseek.com/v1', 'chat_completions'),
    'openrouter': ('OpenRouter', 'https://openrouter.ai/api/v1', 'chat_completions'),
    'xai': ('xAI', 'https://api.x.ai/v1', 'chat_completions'),
    'groq': ('Groq', 'https://api.groq.com/openai/v1', 'chat_completions'),
    'mistral': ('Mistral', 'https://api.mistral.ai/v1', 'chat_completions'),
    'together_ai': ('Together AI', 'https://api.together.xyz/v1', 'chat_completions'),
    'fireworks_ai': ('Fireworks AI', 'https://api.fireworks.ai/inference/v1', 'chat_completions'),
    'cerebras': ('Cerebras', 'https://api.cerebras.ai/v1', 'chat_completions'),
}


def validate_connection(connection):
    if not connection:
        return
    if set(connection) - {'auth_type', 'provider_id', 'provider_name', 'protocol', 'base_url', 'credential_id',
                           'context_window', 'supports_vision', 'catalog_provider', 'source'}:
        raise ValueError('Unsupported OpenCode connection field')
    if connection.get('auth_type') not in {'chatgpt', 'api_key'}:
        raise ValueError('Invalid OpenCode authentication type')
    window = connection.get('context_window')
    if window is not None and (type(window) is not int or not 4096 <= window <= 10_000_000):
        raise ValueError('上下文窗口必须在 4096 到 10000000 Token 之间')
    if connection['auth_type'] == 'api_key':
        if connection.get('protocol') not in PACKAGES or connection.get('provider_id') != 'mabobot':
            raise ValueError('Unsupported OpenCode provider protocol')
        parsed = urlsplit(connection.get('base_url', ''))
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('接口地址必须是无账号密码、查询参数和片段的 HTTP(S) 地址')
        if any(c in connection['base_url'] for c in '{}\r\n\x00'):
            raise ValueError('Invalid endpoint characters')
        if not re.fullmatch(r'[a-f0-9]{32}', connection.get('credential_id', '')):
            raise ValueError('Missing API credential reference')


def credential_file(root, credential_id):
    if not re.fullmatch(r'[a-f0-9]{32}', credential_id):
        raise ValueError('Invalid credential reference')
    directory = Path(root) / 'credentials'
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ValueError('Credential directory cannot contain symlinks')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise ValueError('Credential directory must be private')
    return directory / (credential_id + '.json')


def write_credential(root, credential_id, secret):
    if not isinstance(secret, str) or not secret or len(secret) > 4096 or any(c in secret for c in '\r\n\x00{}\"\\'):
        raise ValueError('API Key 不能为空，也不能包含换行或配置占位符')
    path = credential_file(root, credential_id)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(canonical({'api_key': secret}))
        stream.flush()
        os.fsync(stream.fileno())


def read_credential(root, credential_id):
    path = credential_file(root, credential_id)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding='utf-8') as stream:
            if os.fstat(stream.fileno()).st_mode & 0o077:
                raise ValueError('Credential must be private')
            return json.load(stream)['api_key']
    except FileNotFoundError as exc:
        raise ExecutionError('authentication_required', '此 Profile 的 API Key 不可用，请重新填写') from exc


def snapshot_options(profile):
    from app.services.opencode_wsl import pinned_options
    return {**pinned_options(profile.get('runtime_options', {})),
            **({'connection': profile['connection']} if profile.get('connection') else {})}


def model_spec(candidate):
    connection = json.loads(candidate.profile_config_json).get('connection', {})
    params = json.loads(candidate.parameters_json)
    return {'providerID': connection.get('provider_id', 'openai'), 'id': candidate.model,
            'variant': params.get('reasoning_effort') or ('default' if connection.get('auth_type') == 'api_key' else 'high')}


def runtime_connection(root, connection, models):
    """Build native provider config; the actual key is only passed in the process environment."""
    if not connection or connection.get('auth_type') != 'api_key':
        return None, {}
    validate_connection(connection)
    secret = read_credential(root, connection['credential_id'])
    catalog = {}
    for model in models:
        item = catalog.setdefault(model['id'], {
            'capabilities': {'tools': True, 'input': ['text', 'image'] if connection.get('supports_vision') else ['text'], 'output': ['text']},
            'limit': {'context': connection.get('context_window', 128000), 'output': 4096}, 'variants': []})
        effort = model.get('variant', 'default')
        if effort != 'default' and not any(v['id'] == effort for v in item['variants']):
            body = {'reasoning': {'effort': effort}} if connection['protocol'] == 'responses' else {'reasoning_effort': effort}
            item['variants'].append({'id': effort, 'body': body})
    providers = {'mabobot': {'name': connection.get('provider_name') or '自定义接口',
        'package': PACKAGES[connection['protocol']],
        'settings': {'baseURL': connection['base_url'], 'apiKey': '{env:MABOBOT_PROFILE_API_KEY}'}, 'models': catalog}}
    return providers, {'MABOBOT_PROFILE_API_KEY': secret}


def apply_context_window(config, connection, models):
    """Overlay only the selected models' limits; keep native auth/catalog intact."""
    validate_connection(connection)
    window = (connection or {}).get('context_window')
    if window is None:
        return
    for model in models:
        provider = config.setdefault('providers', {}).setdefault(model['providerID'], {})
        metadata = provider.setdefault('models', {}).setdefault(model['id'], {})
        metadata.setdefault('limit', {})['context'] = window


def oauth_inventory(root, binary, profile_id, model):
    from app.services.opencode_mcp import HostMCP
    from app.services.opencode_runtime import OpenCodeRuntime
    import uuid
    generation = 'catalog-' + uuid.uuid4().hex
    mcp = HostMCP(generation=generation, profile_id=profile_id, catalog=[])
    runtime = OpenCodeRuntime(binary=binary, profile_root=root, profile_id=profile_id,
        generation=generation, models=[{'providerID': 'openai', 'id': model, 'variant': 'default'}], mcp=mcp)
    try:
        runtime.start()
        return runtime.bridge('inventory')
    finally:
        runtime.stop()
        mcp.close()


def terminal_profile(root, binary, profile_id, env, *, connection=None, model='', effort='default'):
    """A terminal wrapper resolves its private credential when launched, never embeds it."""
    control = Path(root) / 'terminal-control'
    if control.is_symlink():
        raise ValueError('Unsafe terminal directory')
    control.mkdir(mode=0o700, exist_ok=True)
    wrapper = control / ('opencode-profile-' + profile_id)
    config_path = control / 'opencode.json'
    if wrapper.is_symlink() or config_path.is_symlink():
        raise ValueError('Unsafe terminal file')
    provider_id = (connection or {}).get('provider_id', 'openai')
    models = [{'providerID': provider_id, 'id': model, 'variant': effort}]
    providers, _ = runtime_connection(root, connection, models)
    config = {'update': 'disable', 'plugins': [],
        'providers': providers or {'openai': {'settings': {'transport': 'http'}}},
        'policies': [{'action': 'skill.use', 'resource': '*', 'effect': 'deny'}]}
    if model:
        config['model'] = provider_id + '/' + model + '#' + effort
    apply_context_window(config, connection, models)
    config_path.write_text(canonical(config), encoding='utf-8')
    env = {**env, 'OPENCODE_CONFIG_DIR': str(control), 'XDG_CONFIG_HOME': str(control), 'TERM': 'xterm-256color'}
    lines = ['#!/usr/bin/env python3', 'import os, json', 'env = ' + repr(env)]
    if (connection or {}).get('auth_type') == 'api_key':
        path = credential_file(root, connection['credential_id'])
        lines += ['fd = os.open(' + repr(str(path)) + ', os.O_RDONLY | os.O_NOFOLLOW)',
            'if os.fstat(fd).st_mode & 0o077: raise RuntimeError("Credential must be private")',
            'with os.fdopen(fd) as stream: env["MABOBOT_PROFILE_API_KEY"] = json.load(stream)["api_key"]']
    lines += ['os.chdir(' + repr(str(control)) + ')', 'os.execve(' + repr(str(binary)) + ', [' + repr(str(binary)) + '], env)']
    wrapper.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    wrapper.chmod(0o700)
    return str(wrapper)
