"""Deployment policy shared by startup, management and execution.

No model process or network request is needed to inspect this policy. Engine
selection is an operator environment setting; browser input cannot supply a
binary, a command, a profile directory or an upstream address.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

from app.services.execution_contracts import ExecutionError

ENGINES = frozenset({"codex", "opencode"})


def enabled_engines():
    raw = os.getenv("MABOBOT_ENABLED_ENGINES", "codex,opencode")
    values = frozenset(part.strip().lower() for part in raw.split(",") if part.strip())
    if not values <= ENGINES:
        raise ExecutionError("deployment_invalid", "MABOBOT_ENABLED_ENGINES accepts codex,opencode only")
    return values


def require_engine(engine):
    if engine in ENGINES and engine not in enabled_engines():
        raise ExecutionError("engine_disabled", f"{engine} is not enabled in this deployment", retryable=True)


def recovery_hold_path(project_root=None):
    return Path(project_root or Path.cwd()) / "data" / "engine-recovery-hold.json"


def recovery_pending(project_root=None):
    return recovery_hold_path(project_root).exists()


def require_dispatch():
    if recovery_pending():
        raise ExecutionError("recovery_review_required", "恢复后尚未核对账号、权限、未决操作和交付；自动派发已暂停")


def inspect_engine(engine, *, check_version=False):
    require_known = engine in ENGINES
    if not require_known:
        raise ValueError("Unknown engine")
    enabled = engine in enabled_engines()
    result = {"enabled": enabled, "installed": False, "status": "disabled", "version": None}
    if not enabled:
        return result
    if engine == "opencode":
        from app.services.opencode_wsl import use_wsl
        if use_wsl():
            from app.services.opencode_installation import status
            return {**result, **status(force=check_version), "enabled": True, "update_policy": "pinned_manual"}
    configured = (os.getenv("OPENCODE_BIN", "opencode") if engine == "opencode"
                  else os.getenv("CODEX_PROXY_BIN") or os.getenv("CODEX_BIN", "codex"))
    if engine == "codex" and os.name == "nt" and os.getenv("CODEX_PROXY_USE_WSL", "true").lower() not in {"0", "false", "off", "no"}:
        # Reuse the existing bounded WSL probe; never try a Linux binary on Windows.
        from app.services.file_tools_runtime import get_file_tools_runtime
        native = get_file_tools_runtime()
        result.update(installed=bool(native.get("codex", {}).get("available")), runtime="wsl")
        result["status"] = "installed" if result["installed"] else "missing"
        result["version"] = native.get("codex", {}).get("version")
        return result
    binary = shutil.which(configured)
    if engine == "opencode" and not binary and configured == "opencode":
        from app.services.opencode_installation import status
        native = status(force=check_version)
        if native.get("installed") and native.get("runtime") == "linux":
            return {**result, **native, "enabled": True, "update_policy": "pinned_manual"}
    result.update(installed=bool(binary), status="installed" if binary else "missing")
    if engine == "opencode":
        from app.services.opencode_runtime import ENGINE_VERSION, ADAPTER_VERSION
        result.update(required_version=ENGINE_VERSION, adapter_version=ADAPTER_VERSION,
                      update_policy="pinned_manual", skills="disabled_by_managed_policy")
        if os.name != "posix" or not Path("/proc/self/status").exists():
            result["status"] = "platform_unsupported"
            return result
    if binary and check_version:
        try:
            # Version output is bounded in time and is never treated as a model
            # or account capability verification.
            output = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20, check=False)
            version = output.stdout.strip()[:200]
            result.update(version=version, status="ready" if output.returncode == 0 else "probe_failed")
            if engine == "opencode" and version != "opencode v" + result["required_version"]:
                result["status"] = "version_mismatch"
        except (OSError, subprocess.SubprocessError):
            result["status"] = "probe_failed"
    return result


def deployment_status(*, check_versions=False):
    return {"engines": {engine: inspect_engine(engine, check_version=check_versions) for engine in sorted(ENGINES)},
            "recovery_pending": recovery_pending(), "console_requires_model": False,
            "opencode_account_restore": "configuration_only_relogin_required"}
