"""Portable backup of managed Profile configuration, credentials and Skills.

Only Mabobot-managed homes are included. Session databases, downloaded plugin
caches and the user's global Codex authentication are deliberately excluded.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath

from app.services.wsl_probe_guard import run_guarded_wsl_command

PROFILE_ROOT = PurePosixPath('.codex/mabobot-profiles')
WRAPPER_ROOT = PurePosixPath('.local/bin')
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$')
FILES = {'config.toml', 'api_key', 'auth.json', 'model-catalog.json', 'AGENTS.md',
         '.mabobot-local-auth.sha256', '.mabobot-skill-manager.json'}


def allowed(relative: str) -> bool:
    path = PurePosixPath(relative)
    if path.parent == WRAPPER_ROOT:
        return path.name.startswith('codex-profile-') and bool(NAME.fullmatch(path.name[len('codex-profile-'):]))
    if path.parts[:2] != PROFILE_ROOT.parts:
        return False
    parts = path.parts[2:]
    if len(parts) == 1:
        return parts[0].endswith('.json') and bool(NAME.fullmatch(parts[0][:-5]))
    return (len(parts) >= 2 and bool(NAME.fullmatch(parts[0]))
            and ((len(parts) == 2 and parts[1] in FILES) or (len(parts) >= 3 and parts[1] == 'skills')))


def discover_home() -> tuple[Path, str]:
    configured = os.getenv('SYSTEM_BACKUP_CODEX_USER_HOME')
    if configured:
        home = Path(configured).expanduser().resolve()
        return home, os.getenv('SYSTEM_BACKUP_CODEX_RUNTIME_HOME') or home.as_posix()
    use_wsl = os.name == 'nt' and os.getenv('CODEX_PROXY_USE_WSL', 'true').lower() not in {'0', 'false', 'off', 'no'}
    if not use_wsl:
        home = Path.home().resolve()
        return home, home.as_posix()
    def run(args):
        result = run_guarded_wsl_command(args, capture_output=True, text=True,
                                        encoding='utf-8', errors='replace', timeout=20,
                                        check=False, runner=subprocess.run)
        if result.returncode or not result.stdout.strip():
            raise ValueError('无法定位 WSL Profile 目录；请确认 WSL 可用后重试')
        return result.stdout.strip()
    runtime_home = run(['wsl.exe', 'bash', '-lc', 'printf "%s" "$HOME"'])
    local_home = run(['wsl.exe', 'wslpath', '-w', runtime_home])
    return Path(local_home).resolve(), runtime_home


def runtime_binary() -> str:
    use_wsl = os.name == 'nt' and os.getenv('CODEX_PROXY_USE_WSL', 'true').lower() not in {'0', 'false', 'off', 'no'}
    if use_wsl:
        result = run_guarded_wsl_command(
            ['wsl.exe', 'bash', '-lic', 'command -v codex'], capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=20,
            check=False, runner=subprocess.run)
        binary = result.stdout.strip() if result.returncode == 0 else ''
    else:
        binary = shutil.which('codex') or ''
    if not binary.startswith('/'):
        raise ValueError('目标运行环境未找到 Codex，请先安装后再恢复 Profile')
    return binary


def sources(home: Path):
    root = home / PROFILE_ROOT
    if root.is_symlink():
        raise ValueError('Profile 根目录不能是符号链接')
    for manifest in sorted(root.glob('*.json')):
        if manifest.is_symlink():
            raise ValueError('Profile 清单不能是符号链接')
        name = manifest.stem
        if not NAME.fullmatch(name):
            raise ValueError('Profile 名称无效')
        data = json.loads(manifest.read_text(encoding='utf-8'))
        if data.get('name') != name:
            raise ValueError('Profile 清单名称不匹配')
        profile_root = root / name
        wrapper = home / WRAPPER_ROOT / f'codex-profile-{name}'
        if profile_root.is_symlink() or wrapper.is_symlink():
            raise ValueError(f'Profile {name} 包含链接，无法保证完整迁移')
        if not (profile_root / 'config.toml').is_file() or not wrapper.is_file():
            raise ValueError(f'Profile {name} 缺少配置或启动脚本，无法完整备份')
        yield manifest
        yield wrapper
        for item in sorted(profile_root.rglob('*')):
            relative = item.relative_to(home).as_posix()
            if item.is_symlink() and (allowed(relative) or item.name == 'skills'):
                raise ValueError(f'Profile {name} 包含链接，无法保证完整迁移')
            if item.is_file() and allowed(relative):
                yield item


def validate_members(relatives) -> set[str]:
    archived = set(relatives)
    names = set()
    for relative in archived:
        if not allowed(relative):
            continue
        path = PurePosixPath(relative)
        if path.parent == WRAPPER_ROOT:
            names.add(path.name[len('codex-profile-'):])
        elif path.parent == PROFILE_ROOT:
            names.add(path.stem)
        else:
            names.add(path.parts[2])
    for name in names:
        required = {(PROFILE_ROOT / f'{name}.json').as_posix(),
                    (PROFILE_ROOT / name / 'config.toml').as_posix(),
                    (WRAPPER_ROOT / f'codex-profile-{name}').as_posix()}
        if not required.issubset(archived):
            raise ValueError(f'Profile {name} 备份不完整')
    return names


def prepare_restore(stage: Path, relatives: list[str], runtime_home: str) -> None:
    """Rebase metadata/config and regenerate wrappers without executing payloads."""
    from scripts.file_tools.manage_codex_profiles import _paths, _render_wrapper
    names = validate_members(relatives)
    for name in sorted(names):
        manifest = (PROFILE_ROOT / f'{name}.json').as_posix()
        config = (PROFILE_ROOT / name / 'config.toml').as_posix()
        wrapper = (WRAPPER_ROOT / f'codex-profile-{name}').as_posix()
        metadata = json.loads((stage / manifest).read_text(encoding='utf-8'))
        if metadata.get('name') != name or metadata.get('auth_type') not in {'api_key', 'chatgpt'}:
            raise ValueError(f'Profile {name} 元数据无效')
        old_home = str(metadata.get('codex_home') or '')
        if not old_home or PurePosixPath(old_home.replace('\\', '/')).parts[-3:] != ('.codex', 'mabobot-profiles', name):
            raise ValueError(f'Profile {name} 原路径无效')
        runtime_path = PureWindowsPath(runtime_home) if PureWindowsPath(runtime_home).drive else PurePosixPath(runtime_home)
        paths = _paths(runtime_path, name)
        config_text = (stage / config).read_text(encoding='utf-8')
        config_text = config_text.replace(old_home, str(paths['codex_home']))
        import tomllib
        tomllib.loads(config_text)
        if old_home != str(paths['codex_home']):
            metadata['codex_bin'] = runtime_binary()
            if metadata.get('auth_type') == 'chatgpt':
                # The restored isolated token must not be overwritten by an
                # unrelated global account on the destination machine.
                metadata['auth_source'] = 'device_code'
        metadata.update(codex_home=str(paths['codex_home']), config_path=str(paths['config']),
                        wrapper_path=str(paths['wrapper']))
        (stage / manifest).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        (stage / config).write_text(config_text, encoding='utf-8')
        (stage / wrapper).write_text(_render_wrapper(metadata, paths), encoding='utf-8')
    for relative in relatives:
        if allowed(relative):
            path = stage / relative
            path.chmod(0o700 if relative.startswith('.local/bin/') or '/skills/' in relative else 0o600)


def apply_runtime_permissions(home: Path, runtime_home: str, relatives: list[str]) -> None:
    """Windows chmod on UNC files cannot set WSL's Unix executable bits."""
    files = [relative for relative in relatives if allowed(relative)]
    use_wsl = os.name == 'nt' and os.getenv('CODEX_PROXY_USE_WSL', 'true').lower() not in {'0', 'false', 'off', 'no'}
    if not files or not use_wsl or PureWindowsPath(runtime_home).drive:
        return
    script = (
        "import json,os,sys; from pathlib import Path; data=json.load(sys.stdin); "
        "home=Path(data['home']); "
        "[(home / name).chmod(0o700 if name.startswith('.local/bin/') or '/skills/' in name else 0o600) "
        "for name in data['files']]"
    )
    result = run_guarded_wsl_command(
        ['wsl.exe', 'python3', '-c', script],
        input=json.dumps({'home': runtime_home, 'files': files}),
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=30, check=False, runner=subprocess.run)
    if result.returncode:
        raise ValueError('无法设置 WSL Profile 的凭据和启动脚本权限，恢复已中止')
