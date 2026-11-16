"""Offline recovery barriers and portable, credential-free engine configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from app.services.engine_deployment import recovery_hold_path

STATE_DEFAULTS = {"CODEX_RUNTIME_STATE_DB": "data/codex_runtime.db",
                  "SCHEDULED_TASKS_DB": "data/scheduled_tasks.sqlite3",
                  "WORK_COORDINATION_DB": "data/shared_work.sqlite3",
                  "DATABASE_URL": "sqlite:///data/database.db"}


def host_state_paths(root):
    """Inventory existing auxiliary ledgers so a configured path is not omitted."""
    from dotenv import dotenv_values
    root = Path(root).resolve()
    values = dotenv_values(root / ".env")
    result = {}
    for key, default in STATE_DEFAULTS.items():
        raw = os.getenv(key) or values.get(key) or default
        if key == "DATABASE_URL":
            from sqlalchemy.engine import make_url
            url = make_url(raw)
            if url.get_backend_name() != "sqlite":
                raise ValueError("应用迁移备份只支持本地 SQLite 主数据库；外部数据库需要独立一致性备份")
            raw = url.database or ":memory:"
            if raw == ":memory:":
                continue
        path = Path(raw)
        path = path if path.is_absolute() else root / path
        if not path.exists():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root / "data"):
            raise ValueError(f"{key} 必须位于项目 data/ 内才能完整迁移")
        result[key] = path.resolve().relative_to(root).as_posix()
    return result


def execution_path(root):
    path = Path(os.getenv("ENGINE_EXECUTION_DB") or os.getenv("CODEX_RUNTIME_STATE_DB") or "data/codex_runtime.db")
    return path if path.is_absolute() else Path(root) / path


def engine_databases(root, *, include_external=False, extra_paths=()):
    """Include renamed ledgers, not just the current environment's database."""
    paths = {execution_path(root)}
    paths.update(Path(root) / relative for relative in extra_paths if relative)
    data = Path(root) / "data"
    if data.exists():
        paths.update(p for p in data.glob("*") if p.suffix in {".db", ".sqlite", ".sqlite3"})
    for path in paths:
        if not include_external and not path.resolve().is_relative_to(Path(root).resolve()):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        try:
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "engine_runs" in tables:
                    yield path, db.execute("SELECT COUNT(*) FROM engine_runs").fetchone()[0]
            finally:
                db.close()
        except sqlite3.DatabaseError:
            # A corrupt database must be repaired explicitly, never overwritten
            # under the assumption that it contains no delivery facts.
            if path == execution_path(root):
                raise ValueError("任务账本损坏，不能确认交付事实；请先核对账本") from None


def protect_existing_facts(root):
    if any(count for _, count in engine_databases(root, include_external=True)):
        raise ValueError("目标机器已有任务及交付账本，禁止用备份覆盖；请恢复到空目录，或使用 code-only 软件回退")


def write_hold(root, *, archive, reason="restore"):
    path = recovery_hold_path(root)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("恢复核对标记路径不能包含符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "archive": archive, "reason": reason, "created": time.time(),
               "required": ["accounts", "permissions", "operations", "deliveries", "scheduled_tasks"],
               "opencode_auth": "relogin_required"}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)


def quarantine_restored_state(root, metadata=None):
    """Do not resume accepted work or trust native sessions/capability receipts.

    Delivery and operation facts remain verbatim. A restored queued request is
    cancelled, and a dispatched request remains unknown for reconciliation.
    """
    for path, _ in engine_databases(root, extra_paths=[(metadata or {}).get("execution_db")]):
        db = sqlite3.connect(path)
        try:
            with db:
                db.execute("UPDATE engine_runs SET status=CASE WHEN status IN ('queued','preparing') THEN 'cancelled' ELSE 'interrupted_unknown' END, cancel_requested=1 WHERE status NOT IN ('completed','skipped','failed','cancelled','interrupted_unknown')")
                db.execute("UPDATE engine_config_pins SET released=1")
                db.execute("UPDATE engine_attempts SET status='interrupted_unknown' WHERE status='running'")
                db.execute("UPDATE engine_operations SET status='unknown' WHERE status='running'")
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ("engine_capabilities", "engine_session_heads", "engine_sessions"):
                    if table in tables:
                        db.execute(f"DELETE FROM {table}")
                if "engine_config_versions" in tables:
                    row = db.execute("SELECT config_json FROM engine_config_versions ORDER BY revision DESC LIMIT 1").fetchone()
                    if row:
                        config = portable_config(json.loads(row[0]))
                        config["enabled"] = False
                        for profile in config["profiles"].values():
                            profile["generation"] = uuid.uuid4().hex
                            profile["auth_binding_generation"] = uuid.uuid4().hex
                            profile["auth_ref"] = profile["engine"] + ":restored:" + profile["auth_binding_generation"]
                        db.execute("INSERT INTO engine_config_versions(config_json,created) VALUES(?,?)", (json.dumps(config), time.time()))
        finally:
            db.close()


def rebase_restored_environment(stage, manifest):
    """Never reuse an absolute source-machine account directory after migration."""
    path = Path(stage) / ".env"
    if not path.exists() or not manifest.get("engine_deployment"):
        return
    from dotenv import set_key, dotenv_values
    metadata = manifest["engine_deployment"]
    values = dotenv_values(path)
    if metadata.get("execution_db") or "OPENCODE_PROFILE_ROOT" in values:
        set_key(path, "OPENCODE_PROFILE_ROOT", "data/opencode")
        set_key(path, "OPENCODE_BIN", "opencode")
    if metadata.get("execution_db"):
        set_key(path, "ENGINE_EXECUTION_DB", metadata["execution_db"])
    for key, relative in metadata.get("host_state_paths", {}).items():
        if key in STATE_DEFAULTS:
            value = "sqlite:///" + relative if key == "DATABASE_URL" else relative
            if key in values or value != STATE_DEFAULTS[key]:
                set_key(path, key, value)


def portable_config(config):
    """Allowlist fields: no native binary paths, tokens or unrecognized extras."""
    from app.services.engine_routing import EngineConfigService
    def select(value, names):
        return {k: value[k] for k in names if k in value}
    result = select(config, ("enabled", "default_engine", "defaults"))
    result["profiles"] = {key: select(p, ("engine", "generation", "account_key", "auth_ref", "model", "reasoning_effort",
        "auth_binding_generation", "retiring", "revoked", "native_profile_id", "channel", "connection")) for key, p in config["profiles"].items()}
    for key, profile in result["profiles"].items():
        if profile["engine"] == "codex":
            profile["runtime_options"] = select(config["profiles"][key].get("runtime_options", {}),
                ("fast_mode", "context_window", "supports_web_search", "supports_vision"))
    result["chats"] = {key: select(p, ("engine", "profiles", "image_profile_id")) for key, p in config["chats"].items()}
    result["routes"] = {key: select(p, ("mode", "engine", "profiles", "max_attempts")) for key, p in config["routes"].items()}
    EngineConfigService.validate(result)
    return result


def require_quiescent_rollback(root):
    for path, _ in engine_databases(root):
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            pending = db.execute("SELECT COUNT(*) FROM engine_runs WHERE status NOT IN ('completed','skipped','failed','cancelled') OR delivery_status IN ('pending','sending','partial','uncertain','failed')").fetchone()[0]
            pending += db.execute("SELECT COUNT(*) FROM engine_operations WHERE status IN ('running','unknown')").fetchone()[0]
            if pending:
                raise ValueError("存在在途任务或未决操作/交付，必须核对并收尾后才能回退旧软件")
        finally:
            db.close()
    # The old scheduler does not understand new engine metadata. Pausing its
    # dispatch before a downgrade prevents recovery of queued native work.
    paths = set((Path(root) / "data").glob("*"))
    paths.update(Path(root) / relative for relative in host_state_paths(root).values())
    for path in paths:
        if path.suffix not in {".db", ".sqlite", ".sqlite3"} or not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError("宿主账本不能为符号链接")
        try:
            db = sqlite3.connect(path)
            with db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "scheduled_runs" in tables:
                    if db.execute("SELECT 1 FROM scheduled_runs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                        raise ValueError("存在未收尾日程，必须停止并核对后才能回退旧软件")
                if "scheduled_deliveries" in tables:
                    if db.execute("SELECT 1 FROM scheduled_deliveries WHERE status IN ('pending','sending','unknown') LIMIT 1").fetchone():
                        raise ValueError("存在未决日程发送，必须核对后才能回退旧软件")
        finally:
            db.close()


def pause_restored_schedules(root, metadata=None):
    paths = set((Path(root) / "data").glob("*"))
    paths.update(Path(root) / relative for relative in (metadata or {}).get("host_state_paths", {}).values())
    for path in paths:
        if path.suffix not in {".db", ".sqlite", ".sqlite3"} or not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError("宿主账本不能为符号链接")
        db = sqlite3.connect(path)
        try:
            with db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "scheduled_settings" in tables:
                    db.execute("UPDATE scheduled_settings SET enabled=0")
                    db.execute("UPDATE scheduled_tasks SET status='paused' WHERE status='active'")
                    db.execute("UPDATE scheduled_runs SET status='interrupted',cancel_requested=1 WHERE status IN ('queued','running')")
                    db.execute("UPDATE scheduled_deliveries SET status='unknown',error='恢复后待核对；禁止自动补发' WHERE status IN ('pending','sending')")
        finally:
            db.close()


def release_hold(root, *, evidence):
    if len(evidence.strip()) < 8:
        raise ValueError("请记录账号、权限、未决操作及发送核对依据（至少 8 个字符）")
    path = recovery_hold_path(root)
    if not path.exists():
        raise ValueError("没有待核对的恢复")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(reviewed_at=time.time(), evidence=evidence)
    receipt = path.with_name("engine-recovery-reviewed-" + uuid.uuid4().hex + ".json")
    receipt.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    receipt.chmod(0o600)
    path.unlink()
    return {"released": True, "receipt": receipt.name, "restart_required": True}
