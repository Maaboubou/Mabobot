#!/usr/bin/env python3
"""Offline deployment/recovery commands. Run with the bot's Python environment."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Probe only enabled engines; never start a model")
    stage = commands.add_parser("stage-opencode", help="Validate and stage the pinned build without activating it")
    stage.add_argument("--binary", type=Path, required=True)
    stage.add_argument("--sha256", required=True)
    activate = commands.add_parser("activate-opencode", help="Switch new work to a staged release; retain old leases")
    activate.add_argument("--release", required=True, help="Content-addressed release directory name")
    activate.add_argument("--expected-revision", type=int, required=True)
    export = commands.add_parser("export-config", help="Export non-sensitive engine configuration; no accounts or capabilities")
    export.add_argument("--output", type=Path, required=True)
    restore_config = commands.add_parser("import-config", help="Import into an empty deployment with dispatch held")
    restore_config.add_argument("--input", type=Path, required=True)
    restore = commands.add_parser("restore", help="Offline application restore or code-only rollback")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--code-only", action="store_true")
    release = commands.add_parser("review-recovery", help="Record operator reconciliation; restart services after review")
    release.add_argument("--evidence", required=True)
    args = parser.parse_args()
    root = args.project_root.resolve(strict=True)
    os.chdir(root)
    from dotenv import load_dotenv
    load_dotenv(root / ".env")
    from app.services.engine_deployment import deployment_status
    from app.services.engine_recovery import execution_path, portable_config, protect_existing_facts, write_hold, release_hold
    from app.services.backup_service import BackupService
    if args.command == "doctor":
        result = deployment_status(check_versions=True)
    elif args.command == "stage-opencode":
        from app.services.engine_releases import stage_release
        result = stage_release(args.binary, args.sha256, root)
    elif args.command == "restore":
        result = BackupService(root).restore_archive(args.archive, code_only=args.code_only)
    elif args.command == "review-recovery":
        BackupService(root)._require_offline()
        result = release_hold(root, evidence=args.evidence)
    else:
        # Opening the config store never starts adapters or resumes tasks.
        from app.services.execution_store import ExecutionStore
        from app.services.engine_routing import EngineConfigService
        from types import SimpleNamespace
        store = ExecutionStore(execution_path(root))
        config = EngineConfigService(store)
        if args.command == "activate-opencode":
            import re
            if not re.fullmatch(r"[a-f0-9]{64}", args.release):
                raise ValueError("Invalid release ID")
            from app.services.engine_releases import activate_release
            options = json.loads((root / "data/engine-releases" / args.release / "release.json").read_text())
            result = activate_release(SimpleNamespace(config=config), options, expected_revision=args.expected_revision)
        elif args.command == "export-config":
            revision, current = config.read()
            result = {"format": "mabobot-engine-config", "version": 1, "source_revision": revision,
                      "accounts": "not_included_relogin_required", "config": portable_config(current)}
            # Never overwrite an existing export or follow a symlink.
            with args.output.open("x", encoding="utf-8") as stream:
                os.chmod(args.output, 0o600)
                json.dump(result, stream, ensure_ascii=False, indent=2)
            result = {"exported": str(args.output), "accounts": "not_included_relogin_required"}
        else:
            BackupService(root)._require_offline()
            protect_existing_facts(root)
            revision, _ = config.read()
            if revision:
                raise ValueError("配置迁移只允许导入空部署；已有配置请逐项核对")
            value = json.loads(args.input.read_text(encoding="utf-8"))
            if value.get("format") != "mabobot-engine-config" or value.get("version") != 1:
                raise ValueError("Unsupported engine configuration package")
            current = portable_config(value["config"])
            current["enabled"] = False
            import uuid
            for profile in current["profiles"].values():
                profile["generation"] = uuid.uuid4().hex
                profile["auth_binding_generation"] = uuid.uuid4().hex
                profile["auth_ref"] = profile["engine"] + ":imported:" + profile["auth_binding_generation"]
            write_hold(root, archive=args.input.name, reason="configuration_import")
            result = {"revision": config.save(current, expected_revision=0), "dispatch_held": True,
                      "accounts": "not_included_relogin_required"}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
