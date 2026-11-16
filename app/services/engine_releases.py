"""Offline, content-addressed OpenCode releases; never overwrite a live binary."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from app.services.execution_contracts import ExecutionError
from app.services.opencode_runtime import ENGINE_VERSION, ADAPTER_VERSION, ADAPTER_DIR


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def verify_release(options):
    binary = Path(options["binary"])
    adapter = Path(options["adapter_dir"])
    if not binary.is_file() or file_hash(binary) != options["binary_sha256"]:
        raise ExecutionError("release_changed", "Pinned OpenCode binary is missing or changed")
    for name, checksum in options["adapter_files"].items():
        if name not in {"plugin.mjs", "package.json"} or file_hash(adapter / name) != checksum:
            raise ExecutionError("release_changed", "Pinned OpenCode adapter is missing or changed")
    if set(options["adapter_files"]) != {"plugin.mjs", "package.json"}:
        raise ExecutionError("release_changed", "Incomplete OpenCode adapter release")
    return binary, adapter


def stage_release(source, expected_sha256, root):
    """The operator supplies a trusted artifact and its independently checked hash.

    No automatic upstream download: only the build already supported by the
    application is accepted. Capability probes remain Profile/model-specific.
    """
    source = Path(source).resolve(strict=True)
    if len(expected_sha256) != 64 or file_hash(source) != expected_sha256:
        raise ValueError("OpenCode binary SHA256 does not match")
    releases = Path(root) / "data" / "engine-releases"
    if any(p.is_symlink() for p in (releases, *releases.parents)):
        raise ValueError("Release directory cannot contain symbolic links")
    releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    adapter_files = {name: file_hash(ADAPTER_DIR / name) for name in ("plugin.mjs", "package.json")}
    release_id = hashlib.sha256((expected_sha256 + json.dumps(adapter_files, sort_keys=True)).encode()).hexdigest()
    target = releases / release_id
    options = {"binary": str((target / "opencode").absolute()), "binary_sha256": expected_sha256,
               "adapter_dir": str((target / "adapter").absolute()), "adapter_files": adapter_files,
               "engine_version": ENGINE_VERSION, "adapter_version": ADAPTER_VERSION}
    if target.exists():
        verify_release(options)
        return options
    with tempfile.TemporaryDirectory(dir=releases, prefix=".stage-") as temporary:
        stage = Path(temporary)
        shutil.copyfile(source, stage / "opencode")
        (stage / "opencode").chmod(0o500)
        if file_hash(stage / "opencode") != expected_sha256:
            raise ValueError("Binary changed while staging")
        result = subprocess.run([str(stage / "opencode"), "--version"], capture_output=True, text=True, timeout=20, check=False)
        if result.returncode or result.stdout.strip() != "opencode v" + ENGINE_VERSION:
            raise ValueError("Unsupported OpenCode version; adapter/protocol regression is required before upgrading")
        (stage / "adapter").mkdir(mode=0o700)
        for name in adapter_files:
            shutil.copyfile(ADAPTER_DIR / name, stage / "adapter" / name)
            (stage / "adapter" / name).chmod(0o400)
        (stage / "release.json").write_text(json.dumps(options, indent=2), encoding="utf-8")
        # Rename a complete directory, never an executable beneath a live lease.
        os.rename(stage, target)
    return options


def activate_release(service, options, *, expected_revision):
    verify_release(options)
    revision, config = service.config.read()
    if revision != expected_revision:
        raise ExecutionError("configuration_conflict", "Configuration changed; inspect before activation")
    changed = []
    for key, profile in config["profiles"].items():
        if profile["engine"] == "opencode" and not profile.get("retiring") and not profile.get("revoked"):
            profile["runtime_options"] = options
            profile["generation"] = uuid.uuid4().hex
            changed.append(key)
    revision = service.config.save(config, expected_revision=revision)
    # Active leases and queued snapshots retain the old generation and artifact.
    # Changing generation invalidates old capability receipts for NEW tasks.
    return {"revision": revision, "profiles": changed, "verification_required": True}
