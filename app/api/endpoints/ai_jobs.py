"""Project both execution stores into the existing runtime console tables."""
from __future__ import annotations

import json
import time
from datetime import datetime

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel, Field
from app.services.execution_contracts import ExecutionError

from app.services.execution_service import get_execution_service
from app.services.execution_store import TERMINAL

router = APIRouter()


def timestamp(value):
    if isinstance(value, (int, float)):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def label_chats(items):
    ids = {int(item["chat_id"].split(":", 1)[1]) for item in items
           if str(item.get("chat_id", "")).startswith("wechat:") and item["chat_id"].split(":", 1)[1].isdigit()}
    if not ids:
        return
    from app.models.base import SessionLocal
    from app.models.user_permission import WeChatUser
    with SessionLocal() as db:
        chats = {f"wechat:{chat.id}": (chat.chat_name, "group" if chat.is_group else "private")
                 for chat in db.query(WeChatUser).filter(WeChatUser.id.in_(ids))}
    for item in items:
        if item.get("chat_id") in chats:
            item["chat_name"], item["chat_type"] = chats[item["chat_id"]]


def project_run(row, attempt=None):
    context = json.loads(row["context_json"])
    candidates = json.loads(row["route_json"])["candidates"]
    candidate = candidates[attempt["candidate_index"] if attempt else 0]
    result = json.loads(attempt["result_json"] or "{}") if attempt else {}
    # Successful probes and other runs may legitimately report no usage.
    # Keep that value unknown instead of treating JSON null as a mapping.
    usage = json.loads(result.get("usage_json") or "null") or {}
    if candidate["engine"] == "opencode":
        from app.services.opencode_usage import normalize_usage
        usage = normalize_usage(usage) or {}
    return {"request_id": "engine:" + row["run_id"], "execution_run_id": row["run_id"],
        "engine": result.get("engine") or candidate["engine"], "backend": candidate["engine"],
        "config_profile": result.get("profile_id") or candidate["profile_id"],
        "model": result.get("actual_model") or candidate["model"], "status": row["status"],
        "chat_id": context["source_chat_id"], "chat_name": context["source_chat_id"] or context["component"],
        "task_type": context["component"] + "." + context["task_type"],
        "started_at": row["created"], "ended_at": row["updated"] if row["status"] in TERMINAL else None,
        "elapsed_seconds": max(0, (row["updated"] if row["status"] in TERMINAL else time.time()) - row["created"]),
        "total_tokens": usage.get("total_tokens"), "delivery_status": row["delivery_status"],
        "current_item_summary": "已请求取消" if row["cancel_requested"] else "",
        "error": result.get("error_code", "")}


@router.get("")
async def list_jobs(recent_limit: int = Query(default=50, ge=0, le=200)):
    from app.api.codex_jobs import list_codex_jobs
    try:
        response = await list_codex_jobs(recent_limit=recent_limit)
        data = response["data"]
    except Exception as exc:
        data = {"active": [], "recent": [], "sessions": [], "stats": {}, "session_stats": {},
                "runtime": {"running": False, "last_error": str(exc)}}
    service = get_execution_service()
    with service.store.connect() as db:
        rows = db.execute("""SELECT * FROM engine_runs WHERE status IN ('queued','preparing','running')
            OR run_id IN (SELECT run_id FROM engine_runs ORDER BY created DESC LIMIT ?)
            ORDER BY created DESC""", (recent_limit,)).fetchall()
        jobs = []
        for row in rows:
            attempt = db.execute("SELECT * FROM engine_attempts WHERE run_id=? ORDER BY candidate_index DESC LIMIT 1",
                                 (row["run_id"],)).fetchone()
            jobs.append(project_run(dict(row), dict(attempt) if attempt else None))
        opencode_sessions = []
        for session in db.execute("SELECT * FROM engine_sessions WHERE engine='opencode' AND purpose='foreground'"):
            if not session["chat_id"]:
                continue
            state = json.loads(session["state_json"])
            last = db.execute("SELECT * FROM engine_runs WHERE run_id=?", (state.get("last_completed_run", ""),)).fetchone()
            candidate = json.loads(last["route_json"])["candidates"][0] if last else {}
            opencode_sessions.append({"engine": "opencode", "chat_id": session["chat_id"],
                "runtime_profile": session["profile_id"], "model": candidate.get("model", ""),
                "thread_id": state.get("native_session_id", ""), "updated_at": state.get("reset_at") or (last["updated"] if last else 0),
                "reset_pending": bool(state.get("reset_at")),
                "recent_run_id": state.get("last_completed_run", ""),
                "usage": state.get("usage"), "context_tokens": state.get("context_tokens"),
                "model_context_window": state.get("model_context_window"),
                "context_source": state.get("context_source"),
                "lifetime_usage": state.get("lifetime_usage", {}),
                "recorded_turn_count": state.get("recorded_turn_count", 0),
                "unknown_usage_turns": state.get("unknown_usage_turns", 1)})
    label_chats(jobs + opencode_sessions)
    # The engine row owns cancellation/delivery. Native rows contain richer live
    # progress and are folded into it rather than counted as a second task.
    native_by_run = {job["execution_run_id"]: job for job in [*data["recent"], *data["active"]]
                     if job.get("execution_run_id")}
    projected_ids = {job["execution_run_id"] for job in jobs}
    for job in jobs:
        native = native_by_run.get(job["execution_run_id"])
        if native:
            for key in ("chat_name", "chat_type", "current_item_type", "progress_event", "total_tokens", "pool_worker"):
                if key in native:
                    job[key] = native[key]
            job["native_request_id"] = native["request_id"]
    for key, active in (("active", True), ("recent", False)):
        legacy = [{**job, "engine": "codex"} for job in data[key]
                  if job.get("execution_run_id") not in projected_ids]
        combined = legacy + [job for job in jobs if (job["status"] not in TERMINAL) == active]
        combined.sort(key=lambda job: timestamp(job.get("started_at")), reverse=True)
        data[key] = combined if active else combined[:recent_limit]
    data["sessions"] = data.get("sessions", []) + opencode_sessions
    data["sessions"].sort(key=lambda session: timestamp(session.get("updated_at")), reverse=True)
    data["session_stats"] = {**data.get("session_stats", {}), "session_count": len(data["sessions"]),
        "unknown_usage_session_count": data.get("session_stats", {}).get("unknown_usage_session_count", 0)
            + sum(bool(s["unknown_usage_turns"]) for s in opencode_sessions),
        "opencode_turn_count": sum(s["recorded_turn_count"] for s in opencode_sessions),
        "opencode_total_tokens": sum(s["lifetime_usage"].get("total_tokens", 0) for s in opencode_sessions)}
    data["stats"] = {**data.get("stats", {}), "active_count": len(data["active"])}
    _, config = service.config.read()
    data["runtime"]["ai_center"] = True
    data["runtime"]["default_engine"] = config["default_engine"] if config["enabled"] else "codex"
    data["runtime"]["scheduler"] = service.scheduler.status()
    data["runtime"]["opencode_profiles"] = sum(p["engine"] == "opencode" and not p.get("retiring")
                                               for p in config["profiles"].values())
    return {"status": "success", "data": data}


class OpenCodeSessionAction(BaseModel):
    chat_id: str = Field(min_length=1, max_length=200)
    profile_id: str = Field(min_length=1, max_length=200)


def manage_session(body, *, delete):
    try:
        result = get_execution_service().store.manage_opencode_session(body.chat_id, body.profile_id, delete=delete)
    except KeyError:
        raise HTTPException(status_code=404, detail='OpenCode 会话不存在或已删除')
    except ExecutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {'status': 'success', 'data': result}


@router.post('/sessions/opencode/reset')
def reset_opencode_session(body: OpenCodeSessionAction):
    return manage_session(body, delete=False)


@router.post('/sessions/opencode/delete')
def delete_opencode_session(body: OpenCodeSessionAction):
    return manage_session(body, delete=True)
