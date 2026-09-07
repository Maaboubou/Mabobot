#!/usr/bin/env python3
"""Create and inspect isolated Codex homes without exposing their secrets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}
VERBOSITY_VALUES = {"inherit", "low", "medium", "high"}
AUTH_TYPES = {"api_key", "chatgpt"}
CHATGPT_AUTH_SOURCES = {"device_code", "local_cache"}
SETUP_STATUSES = {"pending", "ready"}
SECRET_ENV_NAME = "MABOBOT_CODEX_PROFILE_API_KEY"
MAX_AUTH_CACHE_BYTES = 1024 * 1024
PROFILE_DIRECTORY_NAME = "mabobot-profiles"
LEGACY_PROFILE_DIRECTORY_NAMES = ("wxautox-profiles",)
LEGACY_PROFILE_ARTIFACTS = {
    ".wxautox-local-auth.sha256": ".mabobot-local-auth.sha256",
    ".wxautox-skill-manager.json": ".mabobot-skill-manager.json",
    ".wxautox-skill-manager.lock": ".mabobot-skill-manager.lock",
    ".wxautox-skill-audit.jsonl": ".mabobot-skill-audit.jsonl",
    ".wxautox-skill-trash": ".mabobot-skill-trash",
    ".wxautox-skill-history": ".mabobot-skill-history",
    ".wxautox-skill-staging": ".mabobot-skill-staging",
}


class ProfileError(ValueError):
    pass


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _atomic_write_bytes(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _safe_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            os.chmod(path, 0o700)
    except OSError as exc:
        raise ProfileError(f"无法保护目录 {path}: {exc}") from exc


def _required(payload: dict[str, Any], key: str, label: str, limit: int) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ProfileError(f"{label}不能为空")
    if len(value) > limit or "\x00" in value:
        raise ProfileError(f"{label}格式不正确")
    return value


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _validate_base_url(value: Any) -> str:
    base_url = str(value or "").strip()
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or any(character.isspace() for character in base_url)
    ):
        raise ProfileError("API Base URL 必须是有效的 http(s) 地址，且不能包含账号密码")
    return base_url.rstrip("/")


def _validate_api_key(value: Any) -> str:
    api_key = str(value or "")
    if not api_key or len(api_key) > 4096 or any(char in api_key for char in "\r\n\x00"):
        raise ProfileError("API Key 不能为空，且不能包含换行符")
    return api_key


def _validate(payload: dict[str, Any]) -> dict[str, Any]:
    name = _required(payload, "name", "Profile 名称", 48)
    if not PROFILE_NAME_RE.fullmatch(name):
        raise ProfileError("Profile 名称只能包含字母、数字、连字符和下划线，且必须以字母或数字开头")
    auth_type = str(payload.get("auth_type") or "api_key").strip().lower()
    if auth_type not in AUTH_TYPES:
        raise ProfileError("登录方式必须是 api_key 或 chatgpt")
    setup_pending = _as_bool(payload.get("setup_pending"))
    if setup_pending and auth_type != "chatgpt":
        raise ProfileError("只有 ChatGPT 官方 Profile 支持分步创建")
    model = str(payload.get("model") or "").strip()
    if setup_pending and not model:
        # The isolated Codex home needs a syntactically valid bootstrap config
        # before login. The account-backed choice replaces this value when the
        # wizard is finalized.
        model = "gpt-5.6-sol"
    if not model:
        raise ProfileError("模型 ID 不能为空")
    if len(model) > 128 or not MODEL_ID_RE.fullmatch(model):
        raise ProfileError("模型 ID 格式不正确")
    auth_source = str(payload.get("auth_source") or "device_code").strip().lower()
    if auth_type == "chatgpt" and auth_source not in CHATGPT_AUTH_SOURCES:
        raise ProfileError("ChatGPT 登录来源必须是 device_code 或 local_cache")
    if auth_type != "chatgpt":
        auth_source = "api_key"

    base_url = ""
    api_key = ""
    if auth_type == "api_key":
        base_url = _validate_base_url(payload.get("base_url"))
        api_key = _validate_api_key(payload.get("api_key"))

    default_provider = "ChatGPT 官方登录" if auth_type == "chatgpt" else "OpenAI Responses compatible"
    provider_name = str(payload.get("provider_name") or default_provider).strip()
    if not provider_name or len(provider_name) > 100 or "\x00" in provider_name:
        raise ProfileError("供应商名称格式不正确")
    effort = str(payload.get("reasoning_effort") or "high").strip().lower()
    if effort not in REASONING_EFFORTS:
        raise ProfileError("推理强度格式不正确")
    verbosity = str(payload.get("model_verbosity") or "inherit").strip().lower()
    if verbosity not in VERBOSITY_VALUES:
        raise ProfileError("输出详细度格式不正确")
    try:
        context_window = int(payload.get("context_window") or 128000)
    except (TypeError, ValueError) as exc:
        raise ProfileError("上下文窗口必须是整数") from exc
    if not 4096 <= context_window <= 10_000_000:
        raise ProfileError("上下文窗口必须在 4096 到 10000000 之间")
    supports_vision = _as_bool(payload.get("supports_vision"))
    supports_web_search = _as_bool(payload.get("supports_web_search"))
    codex_path = Path(_required(payload, "codex_bin", "Codex 路径", 1000)).expanduser()
    if not codex_path.is_absolute() or not codex_path.is_file() or not os.access(codex_path, os.X_OK):
        raise ProfileError("Codex 路径必须是当前 Linux/WSL 用户可执行的绝对路径")
    return {
        "name": name,
        "auth_type": auth_type,
        "auth_source": auth_source,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "provider_name": provider_name,
        "reasoning_effort": effort,
        "model_verbosity": verbosity,
        "context_window": context_window,
        "supports_vision": supports_vision,
        "supports_web_search": supports_web_search,
        "codex_bin": str(codex_path),
        "setup_status": "pending" if setup_pending else "ready",
    }


def _paths(
    home: Path,
    name: str,
    *,
    directory_name: str = PROFILE_DIRECTORY_NAME,
) -> dict[str, Path]:
    metadata = home / ".codex" / directory_name
    codex_home = metadata / name
    return {
        "metadata": metadata,
        "codex_home": codex_home,
        "config": codex_home / "config.toml",
        "catalog": codex_home / "model-catalog.json",
        "secret": codex_home / "api_key",
        "auth": codex_home / "auth.json",
        "auth_fingerprint": codex_home / ".mabobot-local-auth.sha256",
        "manifest": metadata / f"{name}.json",
        "wrapper": home / ".local" / "bin" / f"codex-profile-{name}",
    }


def _legacy_profile(profile: Any, *, name: str, paths: dict[str, Path]) -> dict[str, Any]:
    """Validate old public metadata and derive every path from the new home."""
    if not isinstance(profile, dict) or str(profile.get("name") or "") != name:
        raise ProfileError(f"旧版 Codex Profile 清单与名称不匹配：{name}")

    auth_type = str(profile.get("auth_type") or "api_key").strip().lower()
    if auth_type not in AUTH_TYPES:
        raise ProfileError(f"旧版 Codex Profile 登录方式无效：{name}")
    auth_source = str(profile.get("auth_source") or "device_code").strip().lower()
    if auth_type == "chatgpt":
        if auth_source not in CHATGPT_AUTH_SOURCES:
            auth_source = "device_code"
    else:
        auth_source = "api_key"

    model = str(profile.get("model") or "").strip()
    if not MODEL_ID_RE.fullmatch(model):
        raise ProfileError(f"旧版 Codex Profile 模型 ID 无效：{name}")
    provider_default = "ChatGPT 官方登录" if auth_type == "chatgpt" else "OpenAI Responses compatible"
    provider_name = str(profile.get("provider_name") or provider_default).strip()
    if not provider_name or len(provider_name) > 100 or "\x00" in provider_name:
        raise ProfileError(f"旧版 Codex Profile 供应商名称无效：{name}")
    base_url = "" if auth_type == "chatgpt" else _validate_base_url(profile.get("base_url"))

    reasoning_effort = str(profile.get("reasoning_effort") or "high").strip().lower()
    if reasoning_effort not in REASONING_EFFORTS:
        reasoning_effort = "high"
    model_verbosity = str(profile.get("model_verbosity") or "inherit").strip().lower()
    if model_verbosity not in VERBOSITY_VALUES:
        model_verbosity = "inherit"
    try:
        context_window = int(profile.get("context_window") or 128000)
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"旧版 Codex Profile 上下文窗口无效：{name}") from exc
    if not 4096 <= context_window <= 10_000_000:
        raise ProfileError(f"旧版 Codex Profile 上下文窗口无效：{name}")

    codex_bin = str(profile.get("codex_bin") or "").strip()
    if not codex_bin or "\x00" in codex_bin or not Path(codex_bin).expanduser().is_absolute():
        raise ProfileError(f"旧版 Codex Profile 的 Codex 路径无效：{name}")
    setup_status = str(profile.get("setup_status") or "ready").strip().lower()
    if setup_status not in SETUP_STATUSES:
        setup_status = "ready"

    migrated = {
        "name": name,
        "auth_type": auth_type,
        "auth_source": auth_source,
        "model": model,
        "provider_name": provider_name,
        "base_url": base_url,
        "reasoning_effort": reasoning_effort,
        "model_verbosity": model_verbosity,
        "context_window": context_window,
        "supports_vision": _as_bool(profile.get("supports_vision")),
        "supports_web_search": _as_bool(profile.get("supports_web_search")),
        "wire_api": "chatgpt" if auth_type == "chatgpt" else "responses",
        "codex_bin": codex_bin,
        "wrapper_path": str(paths["wrapper"]),
        "config_path": str(paths["config"]),
        "codex_home": str(paths["codex_home"]),
        "created_at": str(profile.get("created_at") or datetime.now(timezone.utc).isoformat()),
        "setup_status": setup_status,
    }
    for key in ("account_email", "plan_type"):
        value = str(profile.get(key) or "").strip()
        if value and len(value) <= 320 and "\x00" not in value:
            migrated[key] = value
    return migrated


def _rename_legacy_profile_artifacts(codex_home: Path) -> None:
    for old_name, new_name in LEGACY_PROFILE_ARTIFACTS.items():
        source = codex_home / old_name
        target = codex_home / new_name
        if not source.exists() and not source.is_symlink():
            continue
        if source.is_symlink():
            raise ProfileError(f"旧版 Codex Profile 状态文件不安全：{source}")
        if target.exists() or target.is_symlink():
            continue
        os.replace(source, target)


def _migrate_legacy_profiles(home: Path) -> dict[str, list[str]]:
    """Move pre-Mabobot managed homes without copying or exposing credentials.

    The legacy manifest is removed last.  If the process is interrupted after
    moving a profile directory, the next run can safely resume from the new
    directory while the old manifest still records the pending migration.
    """
    resolved_home = home.resolve()
    current_metadata = resolved_home / ".codex" / PROFILE_DIRECTORY_NAME
    result: dict[str, list[str]] = {"migrated": [], "conflicts": []}

    for legacy_directory_name in LEGACY_PROFILE_DIRECTORY_NAMES:
        legacy_metadata = resolved_home / ".codex" / legacy_directory_name
        if not legacy_metadata.exists():
            continue
        if legacy_metadata.is_symlink() or not legacy_metadata.is_dir():
            raise ProfileError(f"旧版 Codex Profile 目录不安全：{legacy_metadata}")
        try:
            manifests = sorted(legacy_metadata.glob("*.json"))
        except OSError as exc:
            raise ProfileError(f"无法检查旧版 Codex Profile：{exc}") from exc
        if not manifests:
            continue
        _safe_directory(current_metadata)

        for legacy_manifest in manifests:
            if legacy_manifest.is_symlink() or not legacy_manifest.is_file():
                continue
            name = legacy_manifest.stem
            if not PROFILE_NAME_RE.fullmatch(name):
                continue
            legacy_paths = _paths(
                resolved_home,
                name,
                directory_name=legacy_directory_name,
            )
            current_paths = _paths(resolved_home, name)

            if current_paths["manifest"].exists() or current_paths["manifest"].is_symlink():
                # A completed interrupted migration has no legacy profile home;
                # only then is its now-redundant old manifest safe to retire.
                if (
                    not legacy_paths["codex_home"].exists()
                    and current_paths["codex_home"].is_dir()
                    and not current_paths["codex_home"].is_symlink()
                ):
                    try:
                        current_data = json.loads(
                            current_paths["manifest"].read_text(encoding="utf-8")
                        )
                    except (OSError, ValueError, TypeError):
                        current_data = None
                    if isinstance(current_data, dict) and current_data.get("name") == name:
                        legacy_manifest.unlink()
                        continue
                result["conflicts"].append(name)
                continue

            legacy_home = legacy_paths["codex_home"]
            current_home = current_paths["codex_home"]
            if legacy_home.exists() or legacy_home.is_symlink():
                if legacy_home.is_symlink() or not legacy_home.is_dir():
                    raise ProfileError(f"旧版 Codex Profile 目录不安全：{name}")
                if current_home.exists() or current_home.is_symlink():
                    result["conflicts"].append(name)
                    continue
                os.replace(legacy_home, current_home)
            elif current_home.is_symlink() or not current_home.is_dir():
                raise ProfileError(f"旧版 Codex Profile 数据不完整：{name}")

            try:
                legacy_data = json.loads(legacy_manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise ProfileError(f"旧版 Codex Profile 清单已损坏：{name}") from exc
            _rename_legacy_profile_artifacts(current_home)
            profile = _legacy_profile(legacy_data, name=name, paths=current_paths)
            rendered_config = _render_config(profile, current_paths)
            rendered_catalog = (
                json.dumps(_model_catalog(profile), ensure_ascii=False, indent=2) + "\n"
                if profile["auth_type"] == "api_key"
                else None
            )
            rendered_wrapper = _render_wrapper(profile, current_paths)
            rendered_manifest = json.dumps(profile, ensure_ascii=False, indent=2) + "\n"

            _safe_directory(current_paths["wrapper"].parent)
            _atomic_write(current_paths["config"], rendered_config, 0o600)
            if rendered_catalog is not None:
                _atomic_write(current_paths["catalog"], rendered_catalog, 0o600)
            _atomic_write(current_paths["wrapper"], rendered_wrapper, 0o700)
            _atomic_write(current_paths["manifest"], rendered_manifest, 0o600)
            legacy_manifest.unlink()
            result["migrated"].append(name)

    result["migrated"].sort()
    result["conflicts"] = sorted(set(result["conflicts"]))
    return result


def _local_auth_status(home: Path) -> dict[str, Any]:
    source = home.resolve() / ".codex" / "auth.json"
    try:
        info = source.lstat()
    except FileNotFoundError:
        return {
            "available": False,
            "storage": "unavailable",
            "reason": "未发现可复制的本机 auth.json；当前登录可能使用系统钥匙串",
        }
    except OSError as exc:
        return {
            "available": False,
            "storage": "unavailable",
            "reason": f"无法检查本机登录缓存：{exc}",
        }
    if not stat.S_ISREG(info.st_mode) or source.is_symlink():
        return {
            "available": False,
            "storage": "unavailable",
            "reason": "本机登录缓存不是安全的普通文件",
        }
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return {
            "available": False,
            "storage": "unavailable",
            "reason": "本机登录缓存不属于当前运行用户",
        }
    if stat.S_IMODE(info.st_mode) & 0o077:
        return {
            "available": False,
            "storage": "unavailable",
            "reason": "本机登录缓存权限过宽；请先将 auth.json 权限设为 0600",
        }
    if info.st_size <= 0 or info.st_size > MAX_AUTH_CACHE_BYTES:
        return {
            "available": False,
            "storage": "unavailable",
            "reason": "本机登录缓存为空或大小异常",
        }
    return {"available": True, "storage": "file", "reason": "可导入本机 Codex 登录"}


def _read_local_auth(home: Path) -> bytes:
    status = _local_auth_status(home)
    if not status["available"]:
        raise ProfileError(str(status["reason"]))
    source = home.resolve() / ".codex" / "auth.json"
    descriptor = -1
    try:
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ProfileError("本机登录缓存不是普通文件")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ProfileError("本机登录缓存不属于当前运行用户")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ProfileError("本机登录缓存权限过宽；请先将 auth.json 权限设为 0600")
        if info.st_size <= 0 or info.st_size > MAX_AUTH_CACHE_BYTES:
            raise ProfileError("本机登录缓存为空或大小异常")
        chunks: list[bytes] = []
        remaining = MAX_AUTH_CACHE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after_read = os.fstat(descriptor)
        if (
            len(content) != info.st_size
            or after_read.st_size != info.st_size
            or after_read.st_mtime_ns != info.st_mtime_ns
            or after_read.st_ino != info.st_ino
        ):
            raise ProfileError("读取本机登录缓存时文件发生变化，请重试")
        parsed = json.loads(content)
    except ProfileError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise ProfileError("本机登录缓存无法读取或不是有效 JSON") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(parsed, dict) or not parsed:
        raise ProfileError("本机登录缓存内容不完整")
    return content


def _read_profile_auth(path: Path) -> bytes:
    """Read an isolated Profile credential without following replacement links."""
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ProfileError("Profile 登录缓存不是普通文件")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ProfileError("Profile 登录缓存不属于当前运行用户")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ProfileError("Profile 登录缓存权限过宽")
        if info.st_size <= 0 or info.st_size > MAX_AUTH_CACHE_BYTES:
            raise ProfileError("Profile 登录缓存为空或大小异常")
        chunks: list[bytes] = []
        remaining = MAX_AUTH_CACHE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after_read = os.fstat(descriptor)
        if (
            len(content) != info.st_size
            or after_read.st_size != info.st_size
            or after_read.st_mtime_ns != info.st_mtime_ns
            or after_read.st_ino != info.st_ino
        ):
            raise ProfileError("读取 Profile 登录缓存时文件发生变化，请重试")
        return content
    except ProfileError:
        raise
    except OSError as exc:
        raise ProfileError(f"Profile 登录缓存无法读取：{exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _local_auth_sync_status(
    home: Path,
    profile_auth: Path,
    source_fingerprint: Path,
    *,
    local_status: dict[str, Any],
) -> dict[str, Any]:
    if not local_status.get("available"):
        return {
            "auth_sync_status": "unavailable",
            "auth_sync_reason": str(local_status.get("reason") or "本机登录不可用"),
        }
    try:
        local_content = _read_local_auth(home)
    except ProfileError as exc:
        return {"auth_sync_status": "unavailable", "auth_sync_reason": str(exc)}
    if not profile_auth.exists() or profile_auth.is_symlink():
        return {
            "auth_sync_status": "missing",
            "auth_sync_reason": "尚未同步本机 Codex 登录",
        }
    try:
        _read_profile_auth(profile_auth)
    except ProfileError as exc:
        return {"auth_sync_status": "invalid", "auth_sync_reason": str(exc)}
    if source_fingerprint.is_symlink():
        return {
            "auth_sync_status": "invalid",
            "auth_sync_reason": "本机登录同步标记不安全",
        }
    try:
        recorded_digest = source_fingerprint.read_text(encoding="ascii").strip().lower()
    except FileNotFoundError:
        return {
            "auth_sync_status": "outdated",
            "auth_sync_reason": "需要建立本机登录同步基线",
        }
    except OSError as exc:
        return {
            "auth_sync_status": "invalid",
            "auth_sync_reason": f"无法读取本机登录同步标记：{exc}",
        }
    if not re.fullmatch(r"[0-9a-f]{64}", recorded_digest):
        return {
            "auth_sync_status": "invalid",
            "auth_sync_reason": "本机登录同步标记已损坏",
        }
    local_digest = hashlib.sha256(local_content).hexdigest()
    if local_digest == recorded_digest:
        return {"auth_sync_status": "synced", "auth_sync_reason": "已与本机登录同步"}
    return {
        "auth_sync_status": "outdated",
        "auth_sync_reason": "检测到本机 Codex 登录已更新",
    }


def _chatgpt_profile_paths(payload: dict[str, Any], *, home: Path) -> tuple[str, dict[str, Path]]:
    name = _required(payload, "name", "Profile 名称", 48)
    if not PROFILE_NAME_RE.fullmatch(name):
        raise ProfileError("Profile 名称格式不正确")
    paths = _paths(home.resolve(), name)
    if paths["manifest"].is_symlink():
        raise ProfileError("Codex Profile 清单不安全")
    try:
        profile = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ProfileError("Codex Profile 不存在或清单已损坏") from exc
    if not isinstance(profile, dict) or profile.get("name") != name:
        raise ProfileError("Codex Profile 清单与名称不匹配")
    if profile.get("auth_type") != "chatgpt":
        raise ProfileError("只有 ChatGPT 官方 Profile 可以导入登录缓存")
    if paths["codex_home"].is_symlink() or not paths["codex_home"].is_dir():
        raise ProfileError("Codex Profile 目录不安全")
    return name, paths


def import_local_auth(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    name, paths = _chatgpt_profile_paths(payload, home=home)
    content = _read_local_auth(home)
    try:
        changed = _read_profile_auth(paths["auth"]) != content
    except ProfileError:
        changed = True
    _atomic_write_bytes(paths["auth"], content, 0o600)
    _atomic_write(
        paths["auth_fingerprint"],
        hashlib.sha256(content).hexdigest() + "\n",
        0o600,
    )
    return {
        "name": name,
        "imported": True,
        "changed": changed,
        "auth_configured": True,
        "auth_sync_status": "synced",
    }


def clear_profile_auth(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    name, paths = _chatgpt_profile_paths(payload, home=home)
    auth = paths["auth"]
    fingerprint = paths["auth_fingerprint"]
    if auth.is_symlink():
        raise ProfileError("Codex Profile 登录缓存不安全，已拒绝清除")
    if fingerprint.is_symlink():
        raise ProfileError("Codex Profile 登录同步标记不安全，已拒绝清除")
    try:
        if auth.exists():
            auth.unlink()
        if fingerprint.exists():
            fingerprint.unlink()
    except OSError as exc:
        raise ProfileError(f"清除 Codex Profile 登录缓存失败：{exc}") from exc
    return {"name": name, "cleared": True, "auth_configured": False}


def _model_catalog(profile: dict[str, Any]) -> dict[str, Any]:
    model = profile["model"]
    window = int(profile["context_window"])
    verbosity = profile["model_verbosity"]
    supports_vision = bool(profile.get("supports_vision", False))
    supports_web_search = bool(profile.get("supports_web_search", False))
    return {
        "models": [
            {
                "slug": model,
                "display_name": model,
                "description": f"{model} through {profile['provider_name']}",
                "base_instructions": "You are Codex, a coding agent. Inspect relevant files, preserve unrelated changes, and verify completed work.",
                "default_reasoning_level": profile["reasoning_effort"],
                "supported_reasoning_levels": [
                    {"effort": item, "description": f"{item} reasoning"}
                    for item in ("minimal", "low", "medium", "high", "xhigh", "max")
                ],
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 1,
                "additional_speed_tiers": [],
                "service_tiers": [],
                "availability_nux": None,
                "upgrade": None,
                "include_skills_usage_instructions": False,
                "include_plugin_usage_instructions": True,
                "include_apps_usage_instructions": True,
                "default_reasoning_summary": "none",
                "support_verbosity": verbosity != "inherit",
                "default_verbosity": None if verbosity == "inherit" else verbosity,
                "web_search_tool_type": "text",
                "truncation_policy": {"mode": "tokens", "limit": 10000},
                "supports_image_detail_original": supports_vision,
                "context_window": window,
                "max_context_window": window,
                "effective_context_window_percent": 95,
                "experimental_supported_tools": [],
                "input_modalities": ["text", "image"] if supports_vision else ["text"],
                "supports_search_tool": supports_web_search,
                "use_responses_lite": False,
                "node_repl_auto_review_required": False,
                "node_repl_disabled": False,
            }
        ]
    }


def _render_managed_skill_config(paths: dict[str, Path]) -> list[str]:
    """Preserve Skill enablement written by the Profile Skill manager."""
    state_path = paths["codex_home"] / ".mabobot-skill-manager.json"
    disabled: list[str] = []
    if state_path.is_symlink():
        raise ProfileError("Skill 管理状态文件不安全，已拒绝重建 Profile 配置")
    if state_path.exists():
        try:
            if not state_path.is_file() or state_path.stat().st_size > 64 * 1024:
                raise ProfileError("Skill 管理状态文件格式或大小不正确")
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except ProfileError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise ProfileError("Skill 管理状态已损坏，已拒绝重建 Profile 配置") from exc
        raw_disabled = state.get("disabled") if isinstance(state, dict) else None
        if raw_disabled is None:
            raw_disabled = []
        if not isinstance(raw_disabled, list) or any(
            not isinstance(item, str)
                            or not re.fullmatch(
                                r"(?=.{1,64}$)[a-z0-9]+(?:-[a-z0-9]+)*",
                                item,
                            )
            for item in raw_disabled
        ):
            raise ProfileError("Skill 管理状态中的 disabled 列表无效")
        disabled = sorted(set(raw_disabled))
    lines = ["# BEGIN mabowx managed Skill state"]
    for name in disabled:
        lines.extend(
            [
                "[[skills.config]]",
                f"path = {json.dumps(str(paths['codex_home'] / 'skills' / name / 'SKILL.md'), ensure_ascii=False)}",
                "enabled = false",
                "",
            ]
        )
    if not disabled:
        lines.append("# No disabled Profile Skills.")
    lines.append("# END mabowx managed Skill state")
    return lines


def _render_config(profile: dict[str, Any], paths: dict[str, Path]) -> str:
    toml = lambda value: json.dumps(str(value), ensure_ascii=False)
    lines = [
        "# Managed by mabowx in an isolated CODEX_HOME.",
        f"model = {toml(profile['model'])}",
        f"model_context_window = {profile['context_window']}",
        f"model_auto_compact_token_limit = {int(profile['context_window']) * 9 // 10}",
        f"model_reasoning_effort = {toml(profile['reasoning_effort'])}",
        'model_reasoning_summary = "none"',
    ]
    if profile["model_verbosity"] != "inherit":
        lines.append(f"model_verbosity = {toml(profile['model_verbosity'])}")
    if profile["auth_type"] == "chatgpt":
        lines.extend(
            [
                'forced_login_method = "chatgpt"',
                'cli_auth_credentials_store = "file"',
                "",
                "[shell_environment_policy]",
                'inherit = "core"',
                "ignore_default_excludes = false",
                "",
            ]
        )
        lines.extend(_render_managed_skill_config(paths))
        lines.append("")
        return "\n".join(lines)
    provider_id = "mabobot_" + re.sub(r"[^A-Za-z0-9_-]", "_", profile["name"])
    lines[0] += " The API key is stored in a separate 0600 file."
    lines[2:2] = [
        f"model_provider = {toml(provider_id)}",
        f"model_catalog_json = {toml(str(paths['catalog']))}",
    ]
    lines.extend(
        [
            "",
            f"[model_providers.{provider_id}]",
            f"name = {toml(profile['provider_name'])}",
            f"base_url = {toml(profile['base_url'])}",
            f"env_key = {toml(SECRET_ENV_NAME)}",
            'wire_api = "responses"',
            "requires_openai_auth = false",
            "",
            "[shell_environment_policy]",
            'inherit = "core"',
            "ignore_default_excludes = false",
            "",
        ]
    )
    lines.extend(_render_managed_skill_config(paths))
    lines.append("")
    return "\n".join(lines)


def _render_wrapper(profile: dict[str, Any], paths: dict[str, Path]) -> str:
    common = f'''#!/usr/bin/env bash
set -euo pipefail
readonly MABOBOT_PROFILE_CODEX_HOME={shlex.quote(str(paths["codex_home"]))}
readonly MABOBOT_PROFILE_CODEX_BIN={shlex.quote(profile["codex_bin"])}
export CODEX_HOME="${{MABOBOT_PROFILE_CODEX_HOME}}"
export CODEX_SQLITE_HOME="${{MABOBOT_PROFILE_CODEX_HOME}}"
'''
    if profile["auth_type"] == "chatgpt":
        return common + '''unset CODEX_ACCESS_TOKEN CODEX_API_KEY OPENAI_API_KEY MABOBOT_CODEX_PROFILE_API_KEY
exec "${MABOBOT_PROFILE_CODEX_BIN}" "$@"
'''
    return common + f'''readonly MABOBOT_PROFILE_KEY_FILE={shlex.quote(str(paths["secret"]))}
profile_api_key=""
if [[ -s "${{MABOBOT_PROFILE_KEY_FILE}}" ]]; then
    IFS= read -r profile_api_key < "${{MABOBOT_PROFILE_KEY_FILE}}" || true
    profile_api_key="${{profile_api_key%$'\\r'}}"
fi
if [[ -z "${{profile_api_key}}" ]]; then
    printf 'Codex Profile API Key 不存在或为空：%s\\n' "${{MABOBOT_PROFILE_KEY_FILE}}" >&2
    exit 1
fi
export {SECRET_ENV_NAME}="${{profile_api_key}}"
unset profile_api_key CODEX_ACCESS_TOKEN CODEX_API_KEY OPENAI_API_KEY
exec "${{MABOBOT_PROFILE_CODEX_BIN}}" "$@"
'''


def create_profile(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    profile = _validate(payload)
    paths = _paths(home.resolve(), profile["name"])
    if paths["codex_home"].exists() or paths["manifest"].exists() or paths["wrapper"].exists():
        raise ProfileError(f"Profile 已存在或文件名冲突：{profile['name']}")
    for key in ("codex_home", "metadata"):
        _safe_directory(paths[key])
    _safe_directory(paths["wrapper"].parent)
    public = {
        "name": profile["name"],
        "auth_type": profile["auth_type"],
        "auth_source": profile["auth_source"],
        "model": profile["model"],
        "provider_name": profile["provider_name"],
        "base_url": profile["base_url"],
        "reasoning_effort": profile["reasoning_effort"],
        "model_verbosity": profile["model_verbosity"],
        "context_window": profile["context_window"],
        "supports_vision": profile["supports_vision"],
        "supports_web_search": profile["supports_web_search"],
        "wire_api": "chatgpt" if profile["auth_type"] == "chatgpt" else "responses",
        "codex_bin": profile["codex_bin"],
        "wrapper_path": str(paths["wrapper"]),
        "config_path": str(paths["config"]),
        "codex_home": str(paths["codex_home"]),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "setup_status": profile["setup_status"],
    }
    written: list[Path] = []
    try:
        if profile["auth_type"] == "api_key":
            _atomic_write(paths["secret"], profile["api_key"] + "\n", 0o600)
            written.append(paths["secret"])
            _atomic_write(paths["catalog"], json.dumps(_model_catalog(profile), ensure_ascii=False, indent=2) + "\n", 0o600)
            written.append(paths["catalog"])
        _atomic_write(paths["config"], _render_config(profile, paths), 0o600)
        written.append(paths["config"])
        _atomic_write(paths["wrapper"], _render_wrapper(profile, paths), 0o700)
        written.append(paths["wrapper"])
        _atomic_write(paths["manifest"], json.dumps(public, ensure_ascii=False, indent=2) + "\n", 0o600)
        written.append(paths["manifest"])
    except Exception:
        for path in reversed(written):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return {
        **public,
        "key_configured": profile["auth_type"] == "api_key",
        "auth_configured": False,
        "available": profile["auth_type"] == "api_key" and profile["setup_status"] == "ready",
    }


def update_profile(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    name = _required(payload, "name", "Profile 名称", 48)
    if not PROFILE_NAME_RE.fullmatch(name):
        raise ProfileError("Profile 名称格式不正确")
    paths = _paths(home.resolve(), name)
    try:
        profile = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ProfileError("Codex Profile 不存在或清单已损坏") from exc
    auth_type = str(profile.get("auth_type") or "api_key")
    api_profile_fields = {"provider_name", "base_url", "api_key"}
    if auth_type != "api_key" and api_profile_fields.intersection(payload):
        raise ProfileError("ChatGPT Profile 不支持 API Key 或接口地址配置")
    if "model" in payload:
        model = _required(payload, "model", "模型 ID", 128)
        if not MODEL_ID_RE.fullmatch(model):
            raise ProfileError("模型 ID 格式不正确")
        profile["model"] = model
    if "reasoning_effort" in payload:
        effort = str(payload.get("reasoning_effort") or "").strip().lower()
        if effort not in REASONING_EFFORTS:
            raise ProfileError("推理强度格式不正确")
        profile["reasoning_effort"] = effort
    if "model_verbosity" in payload:
        verbosity = str(payload.get("model_verbosity") or "").strip().lower()
        if verbosity not in VERBOSITY_VALUES:
            raise ProfileError("输出详细度格式不正确")
        profile["model_verbosity"] = verbosity
    if "provider_name" in payload:
        provider_name = str(payload.get("provider_name") or "").strip()
        if not provider_name or len(provider_name) > 100 or "\x00" in provider_name:
            raise ProfileError("供应商名称格式不正确")
        profile["provider_name"] = provider_name
    if "base_url" in payload:
        profile["base_url"] = _validate_base_url(payload.get("base_url"))
    next_api_key: str | None = None
    if "api_key" in payload:
        next_api_key = _validate_api_key(payload.get("api_key"))
    if "context_window" in payload:
        try:
            value = int(payload["context_window"])
        except (TypeError, ValueError) as exc:
            raise ProfileError("上下文窗口必须是整数") from exc
        if not 4096 <= value <= 10_000_000:
            raise ProfileError("上下文窗口必须在 4096 到 10000000 之间")
        profile["context_window"] = value
    if "supports_vision" in payload:
        profile["supports_vision"] = _as_bool(payload.get("supports_vision"))
    if "supports_web_search" in payload:
        profile["supports_web_search"] = _as_bool(payload.get("supports_web_search"))
    if "setup_status" in payload:
        setup_status = str(payload.get("setup_status") or "").strip().lower()
        if setup_status not in SETUP_STATUSES:
            raise ProfileError("Profile 创建状态格式不正确")
        if auth_type != "chatgpt" and setup_status != "ready":
            raise ProfileError("API Key Profile 不支持待完成状态")
        profile["setup_status"] = setup_status
    for key in ("account_email", "plan_type"):
        if key in payload:
            value = str(payload.get(key) or "").strip()
            if len(value) > 320 or "\x00" in value:
                raise ProfileError("ChatGPT 账号信息格式不正确")
            if value:
                profile[key] = value
            else:
                profile.pop(key, None)
    # Render every derived file before mutating any on-disk Profile state.  In
    # particular, _render_config validates the Skill manager state and may fail
    # closed.  Keeping that validation ahead of the secret write prevents a
    # rejected Profile update from leaving a partially changed API key behind.
    rendered_config = _render_config(profile, paths)
    rendered_catalog = (
        json.dumps(_model_catalog(profile), ensure_ascii=False, indent=2) + "\n"
        if profile.get("auth_type") == "api_key"
        else None
    )
    rendered_manifest = json.dumps(profile, ensure_ascii=False, indent=2) + "\n"

    if next_api_key is not None:
        _atomic_write(paths["secret"], next_api_key + "\n", 0o600)
    _atomic_write(paths["config"], rendered_config, 0o600)
    if rendered_catalog is not None:
        _atomic_write(paths["catalog"], rendered_catalog, 0o600)
    _atomic_write(paths["manifest"], rendered_manifest, 0o600)
    return profile


def delete_profile(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    """Delete one managed profile and only its validated, profile-scoped files."""
    name = _required(payload, "name", "Profile 名称", 48)
    if not PROFILE_NAME_RE.fullmatch(name):
        raise ProfileError("Profile 名称格式不正确")

    paths = _paths(home.resolve(), name)
    manifest = paths["manifest"]
    if manifest.is_symlink() or not manifest.is_file():
        raise ProfileError("Codex Profile 不存在或清单不安全")
    try:
        profile = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ProfileError("Codex Profile 不存在或清单已损坏") from exc
    if not isinstance(profile, dict) or str(profile.get("name") or "") != name:
        raise ProfileError("Codex Profile 清单与名称不匹配")

    codex_home = paths["codex_home"]
    if codex_home.is_symlink():
        raise ProfileError("Codex Profile 目录不安全，已拒绝删除")
    if codex_home.exists() and not codex_home.is_dir():
        raise ProfileError("Codex Profile 目录格式不正确")

    try:
        if codex_home.exists():
            shutil.rmtree(codex_home)
        wrapper = paths["wrapper"]
        if wrapper.exists() or wrapper.is_symlink():
            wrapper.unlink()
        # Remove the manifest last so an interrupted deletion remains visible
        # and can be retried instead of leaving an undiscoverable launcher.
        manifest.unlink()
    except OSError as exc:
        raise ProfileError(f"删除 Codex Profile 失败：{exc}") from exc

    return {"name": name, "deleted": True}


def list_profiles(*, home: Path) -> dict[str, Any]:
    migration = _migrate_legacy_profiles(home)
    metadata = home.resolve() / ".codex" / PROFILE_DIRECTORY_NAME
    local_auth = _local_auth_status(home)
    profiles: list[dict[str, Any]] = []
    try:
        manifests = sorted(metadata.glob("*.json"))
    except OSError:
        manifests = []
    for manifest in manifests:
        if manifest.is_symlink():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        name = str(data.get("name") or "") if isinstance(data, dict) else ""
        if not PROFILE_NAME_RE.fullmatch(name):
            continue
        expected = _paths(home.resolve(), name)
        if manifest != expected["manifest"]:
            continue
        auth_type = str(data.get("auth_type") or "api_key")
        setup_status = str(data.get("setup_status") or "ready")
        if setup_status not in SETUP_STATUSES:
            setup_status = "ready"
        key_ready = expected["secret"].is_file() and expected["secret"].stat().st_size > 0
        auth_ready = (
            not expected["auth"].is_symlink()
            and expected["auth"].is_file()
            and expected["auth"].stat().st_size > 0
        )
        credentials_ready = auth_ready if auth_type == "chatgpt" else key_ready
        profile = {
                **{key: value for key, value in data.items() if key != "api_key"},
                # Never trust persisted path fields as permission inputs.  They
                # are derived from the validated manifest filename and target
                # home on every listing, so a hand-edited manifest cannot point
                # a managed runtime at an unrelated Skill tree.
                "wrapper_path": str(expected["wrapper"]),
                "config_path": str(expected["config"]),
                "codex_home": str(expected["codex_home"]),
                "auth_type": auth_type,
                "setup_status": setup_status,
                "key_configured": key_ready,
                "auth_configured": auth_ready,
                "available": expected["wrapper"].is_file()
                and os.access(expected["wrapper"], os.X_OK)
                and expected["config"].is_file()
                and credentials_ready
                and setup_status == "ready",
            }
        if auth_type == "chatgpt" and profile.get("auth_source") == "local_cache":
            profile.update(
                _local_auth_sync_status(
                    home,
                    expected["auth"],
                    expected["auth_fingerprint"],
                    local_status=local_auth,
                )
            )
        profiles.append(profile)
    return {
        "codex_home": str(metadata),
        "local_auth": local_auth,
        "profiles": profiles,
        "migration": migration,
    }


def _request() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (ValueError, TypeError) as exc:
        raise ProfileError("请求体必须是 JSON 对象") from exc
    if not isinstance(value, dict):
        raise ProfileError("请求体必须是 JSON 对象")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--action",
        choices=("list", "create", "update", "delete", "import-local-auth", "clear-auth"),
        required=True,
    )
    parser.add_argument("--home", help=argparse.SUPPRESS)
    options = parser.parse_args()
    home = Path(options.home).expanduser() if options.home else Path.home()
    try:
        if options.action == "list":
            result = list_profiles(home=home)
        else:
            # Every mutating action first reconciles data created by wxautox4.
            # This prevents a duplicate create from hiding an existing legacy
            # profile and makes migration independent of which screen opens first.
            _migrate_legacy_profiles(home)
            if options.action == "create":
                result = create_profile(_request(), home=home)
            elif options.action == "update":
                result = update_profile(_request(), home=home)
            elif options.action == "import-local-auth":
                result = import_local_auth(_request(), home=home)
            elif options.action == "clear-auth":
                result = clear_profile_auth(_request(), home=home)
            else:
                result = delete_profile(_request(), home=home)
        print(json.dumps({"status": "success", "data": result}, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (ProfileError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
