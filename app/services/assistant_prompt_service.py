"""Versioned Assistant rules, conservative migration, and bounded request diagnostics."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.assistant.prompt_composer import composition_version, normalize_legacy_prompt
from app.assistant.prompt_defaults import DEFAULT_RULES

logger = logging.getLogger(__name__)
RULE_KEY = "ASSISTANT_PROMPT_RULES_V1"
MIGRATION_KEY = "ASSISTANT_PROMPT_MIGRATION_V1"
_lock = threading.RLock()


def read_rules(db=None):
    if db is None:
        from app.models.base import SessionLocal

        with SessionLocal() as session:
            return read_rules(session)
    from app.models.setting import Setting

    row = db.query(Setting).filter(Setting.key == RULE_KEY).first()
    data = row.get_value() if row else {}
    data = data if isinstance(data, dict) else {}
    texts = {
        k: data.get("overrides", {}).get(k, v["text"]) for k, v in DEFAULT_RULES.items()
    }
    return {
        "version": int(data.get("version", 0)),
        "fingerprint": composition_version(texts),
        "texts": texts,
        "history": data.get("history", []),
        "overrides": data.get("overrides", {}),
    }


def update_rules(db, changes, expected_version):
    from app.models.setting import Setting

    if set(changes) - set(DEFAULT_RULES):
        raise ValueError("包含未知规则")
    for value in changes.values():
        if value is not None and (
            not isinstance(value, str) or not value.strip() or len(value) > 30000
        ):
            raise ValueError("规则内容须为 1–30000 字；恢复默认请使用恢复按钮")
    with _lock:
        current = read_rules(db)
        if current["version"] != expected_version:
            raise ValueError("规则已被其他操作更新，请重新打开后再保存")
        overrides = dict(current["overrides"])
        for key, value in changes.items():
            if value is None or value == DEFAULT_RULES[key]["text"]:
                overrides.pop(key, None)
            else:
                overrides[key] = value.strip()
        history = current["history"] + [
            {
                "version": current["version"],
                "at": datetime.now(timezone.utc).isoformat(),
                "overrides": current["overrides"],
            }
        ]
        data = {
            "version": current["version"] + 1,
            "overrides": overrides,
            "history": history[-20:],
        }
        row = db.query(Setting).filter(Setting.key == RULE_KEY).first()
        serialized = json.dumps(data, ensure_ascii=False)
        if row:
            previous = row.value
            changed = (
                db.query(Setting)
                .filter(Setting.id == row.id, Setting.value == previous)
                .update({"value": serialized}, synchronize_session=False)
            )
            if not changed:
                db.rollback()
                raise ValueError("规则已变化，请重新打开")
        else:
            db.add(Setting(key=RULE_KEY, value=serialized, category="assistant"))
        db.commit()
        db.expire_all()
        return read_rules(db)


def migrate_prompts(db):
    """Transactional original-value backup, stable bindings and idempotent normalization."""
    from app.models.chatbot_judge import ChatBotJudge, UserChatBotJudge
    from app.models.chatbot_role import ChatBotRole, UserChatBotRole
    from app.models.setting import Setting

    if db.query(Setting).filter(Setting.key == MIGRATION_KEY).first():
        return False
    backup = {"roles": [], "judges": [], "at": datetime.now(timezone.utc).isoformat()}
    for cls, group in (
        (UserChatBotRole, "role_bindings"),
        (UserChatBotJudge, "judge_bindings"),
    ):
        backup[group] = [
            {
                "id": row.id,
                "user_id": row.user_id,
                "role_id" if group == "role_bindings" else "judge_id": row.role_id
                if group == "role_bindings"
                else row.judge_id,
            }
            for row in db.query(cls).all()
        ]
    backup["system_rules"] = read_rules(db)
    for cls, group in ((ChatBotRole, "roles"), (ChatBotJudge, "judges")):
        for row in db.query(cls).all():
            backup[group].append(
                {
                    c.name: str(getattr(row, c.name))
                    if isinstance(getattr(row, c.name), datetime)
                    else getattr(row, c.name)
                    for c in cls.__table__.columns
                }
            )
            row.prompt = normalize_legacy_prompt(row.prompt, decision=group == "judges")
            if group == "judges":
                row.prompt_mode = "simple"
                if row.display_name in ("默认 Judge", "刘局-和联胜 Judge"):
                    row.display_name = row.display_name.replace(" Judge", "接话判断")
            if row.name == "Liuju-HLS":
                # Only remove the exact known starter protocol; custom prose is preserved.
                row.prompt = row.prompt.replace(
                    "\n\n## 输出\n输出协议由系统统一处理。你要发到微信群里的话，只放进 json 的 messages 数组里。\nmessages 里的每一项，都必须是刘局会直接发出去的微信短句。\n不要在 messages 之外加标题、解释或其他文字。",
                    "",
                )
                row.prompt = row.prompt.replace(
                    "你**直接不回**，或者发一个「……」让话题自然沉。",
                    "主动接话时交给接话判断决定沉默；已被直接询问时用简短、不展开的表达回应。",
                )
                row.prompt = row.prompt.replace(
                    "**你允许自己发短句、废话、甚至不接话。**",
                    "**你允许自己发短句、废话；是否主动接话由接话判断决定。**",
                )
                source_start = "查资料是为了把话说准，查完仍按群友的口吻接话。"
                from app.chatbot_presets import BUILTIN_CHATBOT_ROLES

                known_source = next(
                    (
                        line
                        for line in BUILTIN_CHATBOT_ROLES[0]["prompt"].splitlines()
                        if line.startswith(source_start)
                    ),
                    "",
                )
                lines = row.prompt.splitlines()
                row.prompt = "\n".join(
                    source_start if line == known_source else line for line in lines
                )
    item = Setting(
        key=MIGRATION_KEY,
        category="assistant",
        description="迁移前完整角色/判断记录；保留 ID 与绑定，可按原记录恢复",
    )
    item.set_value(backup)
    db.add(item)
    db.commit()
    return True


@contextmanager
def _snapshot_db():
    path = Path(
        os.environ.get(
            "MABOBOT_PROMPT_SNAPSHOT_DB", "data/assistant_prompt_snapshots.sqlite3"
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=5)
    c.execute(
        "CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, chat TEXT NOT NULL, created REAL NOT NULL, scene TEXT NOT NULL, payload TEXT NOT NULL)"
    )
    c.execute("CREATE INDEX IF NOT EXISTS snapshots_chat ON snapshots(chat,created)")
    try:
        with c:
            yield c
    finally:
        c.close()


def _redact(value):
    if isinstance(value, dict):
        return {
            k: (
                "[已隐藏]"
                if any(
                    s in k.lower()
                    for s in (
                        "api_key",
                        "authorization",
                        "password",
                        "secret",
                        "access_token",
                    )
                )
                else _redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        import re

        value = re.sub(
            r"data:[^;\s]+;base64,[A-Za-z0-9+/=]+", "[附件二进制省略]", value
        )
        value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]+", r"\1[已隐藏]", value)
        return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[已隐藏]", value)
    return value


def record_snapshot(chat, scene, payload):
    if not chat:
        return None
    try:
        with _lock, _snapshot_db() as c:
            now = time.time()
            sid = uuid.uuid4().hex
            c.execute("DELETE FROM snapshots WHERE created < ?", (now - 7 * 86400,))
            # Bound diagnostics, never silently pretend a truncated record is complete.
            safe = _redact(payload)
            encoded = json.dumps(safe, ensure_ascii=False)
            if len(encoded) > 2_000_000:
                encoded = json.dumps(
                    {"notice": "请求过大，未保存正文", "truncated": True},
                    ensure_ascii=False,
                )
            c.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?)",
                (sid, chat, now, scene, encoded),
            )
            c.execute(
                "DELETE FROM snapshots WHERE chat=? AND id NOT IN (SELECT id FROM snapshots WHERE chat=? ORDER BY created DESC LIMIT 20)",
                (chat, chat),
            )
            return sid
    except Exception:
        logger.exception("提示词快照未保存")
        return None


def list_snapshots(chat):
    with _lock, _snapshot_db() as c:
        c.execute("DELETE FROM snapshots WHERE created < ?", (time.time() - 7 * 86400,))
        return [
            {"id": r[0], "created": r[1], "scene": r[2]}
            for r in c.execute(
                "SELECT id,created,scene FROM snapshots WHERE chat=? ORDER BY created DESC LIMIT 20",
                (chat,),
            )
        ]


def get_snapshot(chat, sid):
    with _snapshot_db() as c:
        row = c.execute(
            "SELECT payload FROM snapshots WHERE chat=? AND id=? AND created>=?",
            (chat, sid, time.time() - 7 * 86400),
        ).fetchone()
        return json.loads(row[0]) if row else None


def prompt_revision(row):
    values = {c.name: str(getattr(row, c.name)) for c in row.__table__.columns}
    return composition_version(values)


def finish_snapshot(chat, sid, outcome):
    if not sid or not chat:
        return
    try:
        with _lock, _snapshot_db() as c:
            row = c.execute(
                "SELECT payload FROM snapshots WHERE chat=? AND id=?", (chat, sid)
            ).fetchone()
            if row:
                payload = json.loads(row[0])
                payload["outcome"] = _redact(outcome)
                c.execute(
                    "UPDATE snapshots SET payload=? WHERE chat=? AND id=?",
                    (json.dumps(payload, ensure_ascii=False), chat, sid),
                )
    except Exception:
        logger.exception("提示词快照结果未更新")


def commit_prompt_update(db, row):
    """Atomically compare stored configuration, including concurrent Web processes."""
    from fastapi import HTTPException
    from sqlalchemy import inspect

    cls = type(row)
    state = inspect(row)
    original, changed = {}, {}
    for column in cls.__table__.columns:
        # SQLite CURRENT_TIMESTAMP stores seconds, whereas ORM datetime binds
        # include microseconds. Comparing these audit fields as text rejects an
        # unchanged row. Every configuration field is compared below instead;
        # updated_at still advances through the column's normal onupdate value.
        if column.name in {"created_at", "updated_at"}:
            continue
        attr = state.attrs[column.name]
        original[column.name] = (
            attr.history.deleted[0]
            if attr.history.deleted
            else getattr(row, column.name)
        )
        if attr.history.has_changes():
            changed[column.name] = getattr(row, column.name)
    if not changed:
        return
    db.expunge(row)  # prevent an unconditional ORM flush of the modified instance
    count = (
        db.query(cls)
        .filter(*(getattr(cls, k) == v for k, v in original.items()))
        .update(changed, synchronize_session=False)
    )
    if count != 1:
        db.rollback()
        raise HTTPException(
            409, "配置已被其他操作更新，草稿未覆盖线上版本；请重新打开后再保存"
        )
    db.commit()
