"""Versioned selector profiles shared by runtime and compatibility checks."""
from __future__ import annotations

import copy
import importlib.resources
import re
import os
import tempfile
from pathlib import Path
from functools import lru_cache
from typing import Any

import yaml

FIELDS = frozenset({'control_type', 'name', 'class_name', 'automation_id'})


def version_key(version: str) -> tuple[int, ...]:
    if not re.fullmatch(r'\d+(?:\.\d+){1,3}', version):
        raise ValueError(f'无效微信版本: {version!r}')
    parts = tuple(map(int, version.split('.')))
    return parts + (0,) * (4 - len(parts))


def choose_version(version: str, available: list[str]) -> str:
    target = version_key(version)
    if version in available:
        return version
    older = [v for v in available if version_key(v) <= target]
    if not older:
        raise KeyError(f'没有适用于微信 {version} 的同版或较旧配置: {available}')
    return max(older, key=version_key)


def available_versions(locale: str = 'cn') -> list[str]:
    if not re.fullmatch(r'[a-z_]+', locale):
        raise ValueError('无效语言')
    pattern = re.compile(r'wechat_(\d+(?:\.\d+){1,3})_' + re.escape(locale) + r'\.yaml')
    return sorted([m[1] for f in importlib.resources.files('mabowx.selectors').iterdir()
                   if (m := pattern.fullmatch(f.name))], key=version_key)


@lru_cache(maxsize=None)
def _load(version: str, locale: str) -> dict[str, Any]:
    selected = choose_version(version, available_versions(locale))
    filename = f'wechat_{selected}_{locale}.yaml'
    data = yaml.safe_load(importlib.resources.files('mabowx.selectors').joinpath(filename).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or str(data.get('version')) != selected or data.get('locale') != locale:
        raise ValueError(f'配置元数据与文件名不符: {filename}')
    data['_selection'] = {'requested_version': version, 'profile_version': selected,
                          'locale': locale, 'exact': selected == version,
                          'validation': data.get('validation', 'pending')}
    return data


def load_profile(version: str = '4.1.12', locale: str = 'cn') -> dict[str, Any]:
    return copy.deepcopy(_load(version, locale))


def create_version_profile(version: str, directory: Path | None = None) -> bool:
    """Create an independent pending profile atomically, never overwrite edits."""
    version_key(version)
    folder = directory or Path(__file__).resolve().parents[1] / 'selectors'
    destination = folder / f'wechat_{version}_cn.yaml'
    if destination.exists():
        return False
    profile = load_profile(version)
    source = profile.pop('_selection')['profile_version']
    profile.update(version=version, copied_from=source, validation='pending')
    folder.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.profile-', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            yaml.safe_dump(profile, f, allow_unicode=True, sort_keys=False)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return False
        _load.cache_clear()
        return True
    finally:
        os.unlink(temporary)


def get_selector(path: str, version: str = '4.1.12', locale: str = 'cn') -> dict[str, Any]:
    node: Any = _load(version, locale)
    for part in path.split('.'):
        node = node[part]
    if not isinstance(node, dict):
        raise TypeError(f'选择器路径 {path!r} 不是配置对象')
    return copy.deepcopy(node)


def runtime_selector(window, path: str) -> dict[str, Any]:
    """Cache the detected profile on each window; do not mutate global version state."""
    profile = getattr(window, '_selector_profile', None)
    if profile is None:
        from .win32 import get_process_path, get_version_by_path
        pid = getattr(window, 'pid', None)
        exe = get_process_path(pid) if pid else None
        version = get_version_by_path(exe) if exe else None
        # Lightweight test doubles have no process. A live unreadable version
        # must fail explicitly rather than silently claim the default version.
        if pid and not version:
            raise RuntimeError(f'无法读取微信版本: pid={pid}')
        profile = load_profile(version or '4.1.12')
        window._selector_profile = profile
        if pid:
            from mabowx.logger import wxlog
            wxlog.info(f"微信选择器配置: {profile['_selection']}")
    node = profile
    for part in path.split('.'):
        node = node[part]
    if not isinstance(node, dict) or not node or set(node) - FIELDS:
        raise ValueError(f'运行时选择器字段不受支持: {path}: {node}')
    return dict(node)
