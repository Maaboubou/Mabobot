#!/usr/bin/env python3
"""Install the verified patched OpenCode, without touching any existing account."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile

PIN = "a6b15d33daf6ab7d35569f6d711fdb630b9f04e7"
VERSION = "2.3.0+mabobot.p0.1"
BUN_VERSION = "1.4.4"
PROJECT = Path(__file__).resolve().parents[2]


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def run(args, *, cwd=None, env=None, timeout=1800):
    subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=True, timeout=timeout)


def install(prefix=None):
    if platform.system() != "Linux":
        raise RuntimeError("OpenCode must be installed inside WSL/Linux")
    arch = {"x86_64": "x64", "aarch64": "arm64"}.get(platform.machine())
    if not arch:
        raise RuntimeError("Unsupported Linux architecture")
    prefix = Path(prefix or Path.home() / ".local/share/mabobot/opencode").absolute()
    if any(p.is_symlink() for p in (prefix, *prefix.parents)):
        raise RuntimeError("Install directory cannot contain symbolic links")
    prefix.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (prefix / "install.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        marker = prefix / "current.json"
        if marker.exists():
            previous = json.loads(marker.read_text())
            binary = Path(previous["binary"])
            if (binary.is_relative_to(prefix / "releases") and binary.is_file()
                    and previous.get("version") == VERSION and sha(binary) == previous.get("sha256")):
                result = subprocess.check_output([str(binary), "--version"], text=True, timeout=20).strip()
                if result == "opencode v" + VERSION:
                    print("OpenCode is already installed and verified", flush=True)
                    return previous
        for dependency in ("git", "npm", "python3"):
            if not shutil.which(dependency):
                raise RuntimeError("Missing prerequisite: " + dependency)
        tools = prefix / "build-tools"
        print("Preparing pinned build tools", flush=True)
        run(["npm", "install", "--prefix", tools, "--ignore-scripts", "--no-audit", "--no-fund",
             "--save-exact", "@oven/bun-linux-" + arch + "@" + BUN_VERSION])
        bun = tools / "node_modules/@oven" / ("bun-linux-" + arch) / "bin/bun"
        if subprocess.check_output([str(bun), "--version"], text=True).strip() != BUN_VERSION:
            raise RuntimeError("Bun version mismatch")
        with tempfile.TemporaryDirectory(prefix="build-", dir=prefix) as temporary:
            source = Path(temporary) / "source"
            print("Fetching locked OpenCode source", flush=True)
            run(["git", "init", source])
            run(["git", "-C", source, "remote", "add", "origin", "https://github.com/anomalyco/opencode.git"])
            run(["git", "-C", source, "fetch", "--depth", "1", "origin", PIN])
            run(["git", "-C", source, "checkout", "--detach", "FETCH_HEAD"])
            actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
            if actual != PIN:
                raise RuntimeError("OpenCode source pin mismatch")
            patch = PROJECT / "scripts/launcher/patches/retain-transient-generate.patch"
            run(["git", "-C", source, "apply", "--check", patch])
            run(["git", "-C", source, "apply", patch])
            env = {**os.environ, "PATH": str(bun.parent) + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
                   "OPENCODE_VERSION": VERSION, "OPENCODE_CHANNEL": "local"}
            print("Installing locked dependencies and building OpenCode (first install may take several minutes)", flush=True)
            run([bun, "install", "--frozen-lockfile", "--filter", "@opencode/cli", "--ignore-scripts"], cwd=source, env=env)
            run([bun, "run", "script/build.ts", "--single", "--skip-install", "--skip-web-ui"], cwd=source / "packages/cli", env=env)
            built = source / "packages/cli/dist" / ("cli-linux-" + arch) / "bin/opencode"
            if subprocess.check_output([str(built), "--version"], text=True, timeout=20).strip() != "opencode v" + VERSION:
                raise RuntimeError("Built binary version mismatch")
            checksum = sha(built)
            release = prefix / "releases" / checksum
            release.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = release / "opencode"
            if not target.exists():
                shutil.copyfile(built, target)
                target.chmod(0o500)
            if sha(target) != checksum:
                raise RuntimeError("Release checksum mismatch")
            value = {"version": VERSION, "source_commit": PIN, "binary": str(target), "sha256": checksum,
                     "patch_sha256": sha(patch), "bun_version": BUN_VERSION}
            pending = prefix / "current.json.tmp"
            pending.write_text(json.dumps(value, indent=2) + "\n")
            pending.chmod(0o600)
            os.replace(pending, marker)
            print("OpenCode installation verified; ready to configure an independent Profile", flush=True)
            return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path)
    args = parser.parse_args()
    install(args.prefix)
