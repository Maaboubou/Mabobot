"""
Dashboard API endpoints
提供 Dashboard 页面所需的统计数据
"""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import asyncio
import hashlib
import json
import os
import re
import logging
from pathlib import Path

from app.models.base import get_db
from app.models.user_permission import WeChatUser
from app.services.config_service import get_setting
from app.services.chat_log_index import get_chat_log_index
from app.utils.dashboard_events import (
    get_latest_dashboard_event,
    get_recent_dashboard_events,
    get_recent_events,
)

router = APIRouter()
logger = logging.getLogger(__name__)
_codex_refresh_lock = asyncio.Lock()
_codex_live_status_snapshot_file = Path("data/codex_usage_snapshot.json")
_codex_live_status_snapshot_version = 2


def _get_chat_logs_dir() -> Path:
    """获取聊天记录目录"""
    return Path(get_setting("CHAT_LOG_DIR", "data/chat_logs"))


def _count_today_messages() -> int:
    """统计今日消息总数（走增量索引，避免每次全量扫描日志）"""
    try:
        return get_chat_log_index().today_totals()[0]
    except Exception as exc:
        logger.warning("统计今日消息失败: %s", exc)
        return 0


def _count_today_ai_replies() -> int:
    """统计今日 AI 回复数（走增量索引）"""
    try:
        return get_chat_log_index().today_totals()[1]
    except Exception as exc:
        logger.warning("统计今日 AI 回复失败: %s", exc)
        return 0


def _count_active_users_today() -> int:
    """统计今日活跃聊天数（走增量索引）"""
    try:
        return get_chat_log_index().today_totals()[2]
    except Exception as exc:
        logger.warning("统计今日活跃聊天失败: %s", exc)
        return 0


def _get_recent_activities(limit: int = 20) -> List[Dict[str, Any]]:
    """获取最近的活动记录"""
    try:
        logs_dir = _get_chat_logs_dir()
        if not logs_dir.exists():
            return []

        activities = []
        bot_name = get_setting("WECHAT_BOT_NAME", "刘局")

        # 收集所有日志文件的最新条目
        for log_file in logs_dir.glob("*.jsonl"):
            try:
                chat_name = log_file.stem
                with open(log_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    # 从后往前读取最近的几条
                    for line in reversed(lines[-50:]):  # 每个文件最多取50条
                        try:
                            entry = json.loads(line.strip())
                            time_str = entry.get('time', '')
                            sender = entry.get('sender', '')
                            content = entry.get('content', '')

                            if not time_str:
                                continue

                            # 判断是用户消息还是机器人回复
                            is_bot = sender == bot_name

                            # 生成预览文本（简化处理，都当文本）
                            preview = content[:50] + '...' if len(content) > 50 else content

                            activities.append({
                                'time': time_str,
                                'chat_name': chat_name,
                                'sender': sender,
                                'is_bot': is_bot,
                                'preview': preview
                            })
                        except (json.JSONDecodeError, ValueError, KeyError):
                            continue
            except Exception:
                continue

        # 按时间排序，取最新的 limit 条
        activities.sort(key=lambda x: x['time'], reverse=True)
        return activities[:limit]
    except Exception:
        return []


def _get_top_users_today(limit: int = 5) -> List[Dict[str, Any]]:
    """获取今日最活跃聊天（走增量索引 + 数据库补 is_group）"""
    try:
        snapshot = get_chat_log_index().snapshot(hours=1, days=1)
        ranked = [
            item for item in snapshot.get("top_chats", [])
            if int(item.get("received") or 0) > 0
        ][: max(1, min(int(limit), 20))]
        if not ranked:
            return []

        from app.models.base import SessionLocal
        db = SessionLocal()
        try:
            result = []
            for item in ranked:
                chat_name = item.get("chat_name") or ""
                user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat_name).first()
                result.append({
                    "chat_name": chat_name,
                    "is_group": user.is_group if user else False,
                    "message_count": int(item.get("received") or 0),
                    "reply_count": int(item.get("replies") or 0),
                })
            return result
        finally:
            db.close()
    except Exception as exc:
        logger.warning("统计今日活跃聊天失败: %s", exc)
        return []


def _timestamp_from_epoch(value: Any) -> Optional[str]:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value).isoformat(timespec="seconds")
    except Exception:
        return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _normalize_rate_limit_window(limit: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(limit, dict):
        return None

    used = _number(limit.get("used_percent", limit.get("usedPercent")))
    if used is None:
        return None
    used = max(0.0, min(100.0, used))
    window = _number(limit.get("window_minutes", limit.get("windowDurationMins")))
    resets_at = _number(limit.get("resets_at", limit.get("resetsAt")))
    resets_at_value = int(resets_at) if resets_at is not None else None
    return {
        "used_percent": used,
        "remaining_percent": 100.0 - used,
        "window_minutes": int(window) if window is not None else None,
        "resets_at": resets_at_value,
        "resets_at_iso": _timestamp_from_epoch(resets_at_value),
    }


def _normalize_rate_limits(rate_limits: Dict[str, Any]) -> Dict[str, Any]:
    normalized = {
        "limit_id": rate_limits.get("limit_id", rate_limits.get("limitId")),
        "limit_name": rate_limits.get("limit_name", rate_limits.get("limitName")),
        "plan_type": rate_limits.get("plan_type", rate_limits.get("planType")),
        "rate_limit_reached_type": rate_limits.get(
            "rate_limit_reached_type", rate_limits.get("rateLimitReachedType")
        ),
        "credits": rate_limits.get("credits"),
        "individual_limit": rate_limits.get(
            "individual_limit", rate_limits.get("individualLimit")
        ),
    }
    for key in ("primary", "secondary"):
        normalized[key] = _normalize_rate_limit_window(rate_limits.get(key))
    return normalized


def _format_codex_quota(rate_limits: Dict[str, Any]) -> str:
    parts = []
    for key, label in (("primary", "Primary"), ("secondary", "Secondary")):
        limit = rate_limits.get(key) or {}
        remaining = limit.get("remaining_percent")
        window = limit.get("window_minutes")
        if isinstance(remaining, (int, float)):
            window_text = ""
            if isinstance(window, (int, float)):
                w_val = int(window)
                if w_val % 1440 == 0:
                    window_text = f" / {w_val // 1440}天"
                elif w_val % 60 == 0:
                    window_text = f" / {w_val // 60}小时"
                else:
                    window_text = f" / {w_val}分钟"
            parts.append(f"{label}: 剩余 {remaining:g}%{window_text}")
    return " | ".join(parts) if parts else "Codex rate limit data found"


def _usage_info_from_runtime(response: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = response.get("rateLimits")
    if not isinstance(snapshot, dict):
        return {
            "quota_available": False,
            "quota": None,
            "quota_message": "Codex runtime did not return account rate limits",
            "rate_limits": None,
            "rate_limits_by_limit_id": None,
            "rate_limit_updated_at": None,
            "rollout_file": None,
            "source": "app_server",
        }

    normalized = _normalize_rate_limits(snapshot)
    quota_available = any(
        isinstance((normalized.get(key) or {}).get("remaining_percent"), (int, float))
        for key in ("primary", "secondary")
    )
    buckets = response.get("rateLimitsByLimitId")
    normalized_buckets = None
    if isinstance(buckets, dict):
        normalized_buckets = {
            str(limit_id): _normalize_rate_limits(value)
            for limit_id, value in buckets.items()
            if isinstance(value, dict)
        }

    return {
        "quota_available": quota_available,
        "quota": _format_codex_quota(normalized) if quota_available else None,
        "quota_message": (
            "Read live account limits from Codex runtime"
            if quota_available
            else "Codex account has no percentage-based rate limit"
        ),
        "rate_limits": normalized,
        "rate_limits_by_limit_id": normalized_buckets,
        "rate_limit_reset_credits": response.get("rateLimitResetCredits"),
        "rate_limit_updated_at": datetime.now().isoformat(timespec="seconds"),
        "rollout_file": None,
        "source": "app_server",
    }


def _read_codex_live_status_snapshot(
    snapshot_file: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Read the last successful Codex runtime result used by the dashboard."""
    path = snapshot_file or _codex_live_status_snapshot_file
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning(f"读取 Codex 实时额度快照失败: {exc}")
        return None

    if not isinstance(snapshot, dict):
        return None
    if snapshot.get("schema_version") != _codex_live_status_snapshot_version:
        return None

    payload = snapshot.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("usage_source") != "app_server" or not payload.get("quota_available"):
        return None
    if not isinstance(payload.get("rate_limits"), dict):
        return None
    return dict(payload)


def _write_codex_live_status_snapshot(
    payload: Dict[str, Any],
    snapshot_file: Optional[Path] = None,
) -> None:
    """Atomically persist a successful live result without credentials or tokens."""
    if payload.get("usage_source") != "app_server" or not payload.get("quota_available"):
        return

    path = snapshot_file or _codex_live_status_snapshot_file
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    snapshot = {
        "schema_version": _codex_live_status_snapshot_version,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "payload": payload,
    }
    try:
        temp_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _codex_profile_context_key(profile: Dict[str, Any]) -> str:
    """Scope usage to a configuration/account identity without storing secrets."""
    identity = {key: profile.get(key) for key in (
        "name", "model", "provider_name", "base_url", "auth_type", "auth_source",
        "account_email", "plan_type", "created_at", "auth_sync_status", "available",
    )}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _codex_status_base(refresh: bool) -> Dict[str, Any]:
    return {
        "status": "warning", "logged_in": None, "profile_available": False,
        "profile_id": "", "model": "", "model_provider": "", "auth_mode": "",
        "context_key": "unconfigured", "configuration_scope": "default_assistant",
        "quota_scope": None, "quota_supported": False, "quota_available": False,
        "quota": None, "quota_message": "尚未配置默认助手模型，请先在 Codex 页面完成配置。",
        "version": "", "plan_type": None, "rate_limits": None,
        "rate_limits_by_limit_id": None, "rate_limit_updated_at": None,
        "usage_source": None, "served_from_snapshot": False,
        "refreshed": refresh, "refresh_succeeded": False if refresh else None,
        "updated_at": datetime.now().isoformat(timespec="milliseconds"), "errors": [],
    }


async def _get_codex_status_payload(refresh: bool = False) -> Dict[str, Any]:
    from app.services.codex_profile_service import get_codex_profile_service, get_codex_runtime_registry

    data = _codex_status_base(refresh)
    listing = await asyncio.to_thread(get_codex_profile_service().list_profiles)
    profile_id = listing.get("default_profile_id")
    profile = next((item for item in listing.get("profiles", []) if item.get("name") == profile_id), None)
    if not profile:
        return data
    data.update({
        "profile_id": profile_id, "model": profile.get("model") or "",
        "model_provider": profile.get("provider_name") or "",
        "auth_mode": profile.get("auth_type") or "",
        "profile_available": bool(profile.get("available")),
        "context_key": _codex_profile_context_key(profile),
    })
    if listing.get("stale"):
        data["quota_message"] = "配置读取暂不可用，当前显示最近的配置，未查询额度。"
        return data
    if not profile.get("available"):
        data["quota_message"] = "当前配置尚未完成认证，请在 Codex 页面检查。"
        return data
    data["status"] = "ok"
    if profile.get("auth_type") != "chatgpt":
        data["quota_message"] = "API Key / 第三方模型暂未接入额度查询，请到对应服务商查看余额和限额。"
        return data

    data.update({"quota_supported": True, "quota_scope": "chatgpt_account",
                 "quota_message": "点击刷新读取此配置对应的 ChatGPT 账户额度；额度由账户共享，并非单模型余额。"})

    def cached_usage():
        snapshot = _read_codex_live_status_snapshot()
        if not snapshot or snapshot.get("context_key") != data["context_key"]:
            return None
        # Only quota fields come from cache. Model/provider always reflect the
        # current configuration, never global CLI sessions or environment defaults.
        fields = ("quota_available", "quota", "rate_limits", "rate_limits_by_limit_id",
                  "rate_limit_reset_credits", "rate_limit_updated_at", "plan_type", "version", "usage_source")
        return {key: snapshot.get(key) for key in fields}

    if not refresh:
        cached = cached_usage()
        if cached:
            data.update(cached, served_from_snapshot=True, quota_message="正在显示此配置最近一次获取的账户额度。")
        return data

    async with _codex_refresh_lock:
        try:
            def read_profile_usage():
                runtime, resolved_profile = get_codex_runtime_registry().resolve(profile_id)
                if not resolved_profile or resolved_profile.get("auth_type") != "chatgpt":
                    raise ValueError("配置认证方式已变更，请重新读取配置")
                response, worker = runtime.read_rate_limits(int(os.getenv("CODEX_USAGE_REFRESH_TIMEOUT", "30")))
                return response, worker, resolved_profile

            response, worker, resolved_profile = await asyncio.to_thread(read_profile_usage)
            # Resolving may synchronize account credentials. Do not attach the
            # resulting account's usage to a stale pre-sync identity.
            if resolved_profile:
                profile = resolved_profile
                data.update({"context_key": _codex_profile_context_key(profile),
                             "model": profile.get("model") or "",
                             "model_provider": profile.get("provider_name") or ""})
            usage = _usage_info_from_runtime(response)
            data.update({key: value for key, value in usage.items() if key != "source"})
            data.update({"usage_source": usage["source"], "version": worker.get("codex_version") or "",
                         "plan_type": (usage.get("rate_limits") or {}).get("plan_type"),
                         "refresh_succeeded": bool(usage.get("quota_available")),
                         "quota_message": "额度由此配置对应的 ChatGPT 账户共享，并非单模型余额。" if usage.get("quota_available") else "此账户暂未返回可显示的额度。"})
            if data["refresh_succeeded"]:
                try:
                    _write_codex_live_status_snapshot(data)
                except Exception as exc:
                    logger.warning("保存 Codex 额度快照失败: %s", exc)
            return data
        except Exception as exc:
            logger.warning("读取默认助手配置的 Codex 额度失败: %s", exc)
            cached = cached_usage()
            if cached:
                data.update(cached, served_from_snapshot=True)
            data.update({"status": "warning", "refresh_succeeded": False,
                         "quota_message": "额度刷新失败，显示此配置最近一次的账户额度。" if cached else "额度暂时无法获取，不代表模型不可用或余额为零。"})
            return data


@router.get("/stats")
def get_dashboard_stats():
    """获取 Dashboard 核心统计数据"""
    try:
        from app.services.llm_manager import get_llm_manager
        import psutil

        # LLM Stats Aggregation
        llm_manager = get_llm_manager()
        llm_stats = llm_manager.get_stats()
        session_stats = llm_stats.get('session', {})

        token_usage = 0
        total_calls = 0
        error_count = 0
        llm_response_times = []

        e2e_latency_stats = session_stats.get('assistant.reply_latency')

        for key, data in session_stats.items():
            # Skip the virtual latency metric for call counts and tokens
            if key == 'assistant.reply_latency':
                continue

            token_usage += data.get('total_tokens', 0)
            total_calls += data.get('count', 0)
            error_count += data.get('error_count', 0)
            llm_response_times.extend(data.get('response_times', []))

        avg_latency = 0.0

        # Prioritize E2E Latency if available
        if e2e_latency_stats and e2e_latency_stats.get('response_times'):
            times = e2e_latency_stats.get('response_times', [])
            if times:
                avg_latency = sum(times) / len(times)
        # Fallback to LLM latency if E2E not available
        elif llm_response_times:
            avg_latency = sum(llm_response_times) / len(llm_response_times)

        # Runtime Duration
        create_time = psutil.Process().create_time()
        uptime_seconds = datetime.now().timestamp() - create_time
        # Format as HH:MM:SS
        hours, remainder = divmod(int(uptime_seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        runtime_duration = f"{hours}h {minutes}m {seconds}s"

    except Exception as e:
        logger.error(f"Error calculating stats: {e}")
        token_usage = 0
        total_calls = 0
        error_count = 0
        avg_latency = 0.0
        runtime_duration = "0h 0m 0s"

    return {
        "today_messages": _count_today_messages(),
        "today_ai_replies": _count_today_ai_replies(),
        "active_users": _count_active_users_today(),
        "token_usage": token_usage,
        "runtime_stats": {
            "total_calls": total_calls,
            "avg_latency": round(avg_latency, 2),
            "error_count": error_count,
            "duration": runtime_duration
        }
    }


@router.get("/codex-status")
async def get_codex_status():
    """读取默认助手配置及其对应的账户额度快照。"""
    try:
        return await _get_codex_status_payload(refresh=False)
    except Exception as exc:
        logger.warning(f"获取 Codex 状态失败: {exc}")
        return {
            "status": "error",
            "logged_in": False,
            "login_status": "Codex status unavailable",
            "version": "",
            "model": "",
            "model_provider": "",
            "context_key": "unavailable",
            "quota_supported": False,
            "quota_available": False,
            "quota": None,
            "quota_message": f"获取失败: {exc}",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "errors": [str(exc)],
        }


@router.post("/codex-status/refresh")
async def refresh_codex_status():
    """通过默认助手 Profile 查询账户额度；API Key 配置返回不支持查询。"""
    try:
        return await _get_codex_status_payload(refresh=True)
    except Exception as exc:
        logger.warning(f"刷新 Codex 状态失败: {exc}")
        return {
            "status": "error",
            "logged_in": False,
            "login_status": "Codex status unavailable",
            "version": "",
            "model": "",
            "model_provider": "",
            "context_key": "unavailable",
            "quota_supported": False,
            "quota_available": False,
            "quota": None,
            "quota_message": f"刷新失败: {exc}",
            "rate_limits": None,
            "rate_limit_updated_at": None,
            "rollout_file": None,
            "refreshed": True,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "errors": [str(exc)],
        }


@router.get("/recent-activities")
def get_recent_activities(limit: int = 20):
    """获取最近的活动记录"""
    return {
        "activities": _get_recent_activities(limit)
    }


@router.get("/top-users")
def get_top_users(limit: int = 5):
    """获取今日最活跃用户排行"""
    return {
        "users": _get_top_users_today(limit)
    }


@router.get("/top-plugins")
def get_top_plugins(limit: int = 5):
    """获取今日最热门插件排行"""
    # 这个需要从插件调用日志中统计
    # 暂时返回空数据，后续可以通过日志分析实现
    return {
        "plugins": []
    }


@router.get("/latest-judge")
async def get_latest_judge():
    """获取最新的 judge 输出"""
    try:
        # 优先读取结构化事件（不依赖日志文案）
        judge_events = get_recent_dashboard_events("judge_decision", limit=10)
        if judge_events:
            history = []
            for event in judge_events:
                payload = event.get("payload", {}) or {}
                history.append({
                    "judge_output": {
                        "should_reply": payload.get("should_reply"),
                        "reason": payload.get("reason"),
                        "judge_name": payload.get("judge_name"),
                    },
                    "timestamp": event.get("timestamp"),
                    "should_reply": payload.get("should_reply"),
                    "reason": payload.get("reason"),
                    "judge_name": payload.get("judge_name"),
                    "role_name": payload.get("role_name"),
                    "atmosphere": payload.get("atmosphere"),
                })

            payload = judge_events[0].get("payload", {}) or {}
            should_reply = payload.get("should_reply")
            reason = payload.get("reason")
            judge_name = payload.get("judge_name")
            return {
                "judge_output": {
                    "should_reply": should_reply,
                    "reason": reason,
                    "judge_name": judge_name,
                },
                "timestamp": judge_events[0].get("timestamp"),
                "should_reply": should_reply,
                "reason": reason,
                "judge_name": judge_name,
                "history": history,
            }

        # 兼容旧版本：回退到日志解析
        # 读取应用日志文件
        log_file = Path("logs/app.log")
        if not log_file.exists():
            return {
                "judge_output": None,
                "timestamp": None,
                "should_reply": None,
                "reason": "日志文件不存在",
                "judge_name": None,
                "history": [],
            }

        # 读取最后 2000 行日志（避免读取整个文件）
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            recent_lines = lines[-2000:] if len(lines) > 2000 else lines

        # 查找最新的 judge 输出
        # 日志格式: 2026-01-29 10:32:50,163 [INFO] app.assistant.handler: ⚖️ Judge decided to STAY SILENT: reason
        # 或: ⚖️ Judge decided to REPLY: reason
        judge_pattern = re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Judge decided to (STAY SILENT|REPLY): (.+)')

        latest_judge = None
        latest_timestamp = None
        should_reply = None
        reason = None
        judge_name = None
        history = []

        # 从后往前查找（最新的在最后）
        for line in reversed(recent_lines):
            if 'Judge decided to' in line:
                match = judge_pattern.search(line)
                if match:
                    latest_timestamp = match.group(1)
                    decision = match.group(2)  # "STAY SILENT" or "REPLY"
                    reason = match.group(3).strip()
                    should_reply = (decision == "REPLY")

                    item = {
                        "should_reply": should_reply,
                        "reason": reason,
                        "judge_name": None,
                    }
                    if latest_judge is None:
                        latest_judge = item
                    history.append({
                        "judge_output": item,
                        "timestamp": latest_timestamp,
                        "should_reply": should_reply,
                        "reason": reason,
                        "judge_name": None,
                    })
                    if len(history) >= 10:
                        break

        if latest_judge:
            latest_item = history[0] if history else {}
            return {
                "judge_output": latest_judge,
                "timestamp": latest_item.get("timestamp", latest_timestamp),
                "should_reply": latest_item.get("should_reply"),
                "reason": latest_item.get("reason"),
                "judge_name": judge_name,
                "history": history,
            }
        else:
            return {
                "judge_output": None,
                "timestamp": None,
                "should_reply": None,
                "reason": "暂无 Judge 输出",
                "judge_name": None,
                "history": [],
            }

    except Exception as e:
        logger.error(f"获取最新 judge 输出失败: {e}")
        return {
            "judge_output": None,
            "timestamp": None,
            "should_reply": None,
            "reason": f"获取失败: {str(e)}",
            "judge_name": None,
            "history": [],
        }


@router.get("/latest-search")
async def get_latest_search():
    """获取最新的 web search 输出"""
    try:
        # 优先读取结构化事件（不依赖日志文案）
        search_event = get_latest_dashboard_event("web_search")
        if search_event:
            payload = search_event.get("payload", {}) or {}
            query = payload.get("query")
            content = payload.get("content")
            result_length = payload.get("result_length")
            model_name = payload.get("model_name")
            return {
                "search_output": {
                    "query": query,
                    "content": content,
                    "result_length": result_length,
                    "model_name": model_name
                },
                "timestamp": search_event.get("timestamp"),
                "query": query,
                "content": content,
                "result_length": result_length,
                "model_name": model_name
            }

        # 兼容旧版本：回退到日志解析
        # 读取应用日志文件
        log_file = Path("logs/app.log")
        if not log_file.exists():
            return {
                "search_output": None,
                "timestamp": None,
                "query": None,
                "content": None,
                "result_length": None,
                "reason": "日志文件不存在"
            }

        # 读取最后 2000 行日志（避免读取整个文件）
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            recent_lines = lines[-2000:] if len(lines) > 2000 else lines

        # 查找最新的 search 输出
        # 新格式: 🔍 Web Search Success | Query: "query" | Length: 1234 | Content: actual content...
        # 旧格式: 🔍 Web Search Success | Query: "query" | Results: 1234 chars
        search_pattern_new = re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*🔍 Web Search Success \| Query: "(.+?)" \| Length: (\d+) \| Content: (.+)')
        search_pattern_old = re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*🔍 Web Search Success \| Query: "(.+?)" \| Results: (\d+) chars')

        latest_search = None
        latest_timestamp = None
        query = None
        content = None
        result_length = None
        model_name = None

        # 从后往前查找（最新的在最后）
        for i in range(len(recent_lines) - 1, -1, -1):
            line = recent_lines[i]
            if '🔍 Web Search Success' in line:
                # 先尝试新格式（带 Content）
                if '| Content: ' in line:
                    # 提取基本信息（支持含 Model 字段的新格式）
                    match = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Query: "(.+?)" \| (?:Model: ([^|]+) \| )?Length: (\d+)', line)
                    if match:
                        latest_timestamp = match.group(1)
                        query = match.group(2).strip()
                        model_name = match.group(3).strip() if match.group(3) else None
                        result_length = int(match.group(4))

                        # 提取内容（从 "Content: " 开始）
                        content_start = line.find('| Content: ') + len('| Content: ')
                        content_parts = [line[content_start:].rstrip('\n')]

                        # 继续读取后续行，直到遇到新的日志条目或达到限制
                        j = i + 1
                        max_lines = 200  # 最多读取200行（支持更长的搜索结果）
                        while j < len(recent_lines) and j < i + max_lines:
                            next_line = recent_lines[j]
                            # 如果遇到新的日志条目（以时间戳开头），停止
                            if re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', next_line):
                                break
                            content_parts.append(next_line.rstrip('\n'))
                            j += 1

                        # 合并内容（不限制长度，在前端用 modal 显示）
                        content = '\n'.join(content_parts).strip()

                        latest_search = {
                            "query": query,
                            "content": content,
                            "result_length": result_length,
                            "model_name": model_name
                        }
                        break

                # 如果新格式不匹配，尝试旧格式（无 Content）
                match = search_pattern_old.search(line)
                if match:
                    latest_timestamp = match.group(1)
                    query = match.group(2).strip()
                    result_length = int(match.group(3))
                    content = "（旧版本日志，无内容预览）"

                    latest_search = {
                        "query": query,
                        "content": content,
                        "result_length": result_length
                    }
                    break

        if latest_search:
            return {
                "search_output": latest_search,
                "timestamp": latest_timestamp,
                "query": query,
                "content": content,
                "result_length": result_length,
                "model_name": model_name
            }
        else:
            return {
                "search_output": None,
                "timestamp": None,
                "query": None,
                "content": None,
                "result_length": None,
                "reason": "暂无搜索记录"
            }

    except Exception as e:
        logger.error(f"获取最新搜索输出失败: {e}")
        return {
            "search_output": None,
            "timestamp": None,
            "query": None,
            "content": None,
            "result_length": None,
            "reason": f"获取失败: {str(e)}"
        }


# ==================== 值班台聚合数据 ====================

_ATTENTION_LIMIT = 8
_EVENT_LIMIT = 60
_INCIDENT_WINDOW_HOURS = 24


@router.get("/timeseries")
def get_dashboard_timeseries(
    hours: int = Query(24, ge=1, le=168),
    days: int = Query(7, ge=1, le=30),
):
    """按小时/天聚合的聊天活动趋势，供概览页的 KPI 与图表使用。"""
    try:
        return get_chat_log_index().snapshot(hours=hours, days=days)
    except Exception as exc:
        logger.warning("读取聊天趋势失败: %s", exc)
        return {
            "error": str(exc),
            "hours": hours,
            "days": days,
            "hourly": [],
            "daily": [],
            "today": {"received": 0, "replies": 0, "chats": 0, "reply_rate": 0},
            "yesterday": {"received": 0, "replies": 0, "chats": 0},
            "top_chats": [],
            "last_activity_at": None,
        }


@router.get("/pulse")
def get_dashboard_pulse() -> Dict[str, Any]:
    """五秒级"还在进消息吗"脉搏：最近 1/5/15/60 分钟的收发计数。

    只读内存索引（允许比趋势窗口更短的扫描间隔），供概览页常驻显示，
    请求体不超过几百字节。
    """
    try:
        return get_chat_log_index().pulse()
    except Exception as exc:
        logger.warning("读取聊天脉搏失败: %s", exc)
        return {
            "error": str(exc),
            "generated_at": None,
            "last_activity_at": None,
            "windows": {},
        }


def _attention_item(
    key: str,
    severity: str,
    title: str,
    detail: str = "",
    *,
    at: Optional[Any] = None,
    action: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "key": key,
        "severity": severity,
        "title": title,
        "detail": detail,
        "at": _timestamp_from_epoch(at) if isinstance(at, (int, float)) else at,
        "action": action or {},
    }


def _attention_from_wechat(request: Request, items: List[Dict[str, Any]]) -> None:
    try:
        wechat_manager = getattr(request.app.state, "wechat_manager", None)
        if wechat_manager is not None and not wechat_manager.is_connected_cached():
            items.append(_attention_item(
                "wechat.offline", "critical", "微信未连接",
                "机器人当前收不到消息，请检查微信客户端登录状态。",
                action={"label": "查看运行状态", "tab": "settings", "section": "operations",
                        "path": "/system/operations"},
            ))
    except Exception as exc:
        logger.debug("读取微信连接状态失败: %s", exc)

    try:
        from app.services.wechat_monitor_service import get_monitor_service

        monitor = get_monitor_service().get_status()
        if monitor and not monitor.get("monitoring", True):
            items.append(_attention_item(
                "wechat.monitor_stopped", "warning", "微信监控未运行",
                "断线自动恢复已失效，重启后才会恢复巡检。",
                action={"label": "查看运行状态", "tab": "settings", "section": "operations",
                        "path": "/system/operations"},
            ))
        missing = monitor.get("last_missing_listeners") or []
        if missing:
            names = "、".join(str(name) for name in list(missing)[:3])
            items.append(_attention_item(
                "wechat.listeners_missing", "warning", f"监听器缺失 {len(missing)} 个",
                f"{names}{' 等' if len(missing) > 3 else ''} 需要恢复。",
                action={"label": "查看运行状态", "tab": "settings", "section": "operations",
                        "path": "/system/operations"},
            ))
        if monitor.get("relogin_in_progress"):
            items.append(_attention_item(
                "wechat.relogin", "info", "正在自动重新登录微信",
                "恢复完成前消息可能延迟处理。",
            ))
    except Exception as exc:
        logger.debug("读取微信监控状态失败: %s", exc)


def _attention_from_health(request: Request, items: List[Dict[str, Any]]) -> None:
    try:
        state = getattr(request.app, "state", None)
        # 启动流程完成时才会写入 event_bus 与 plugin_manager；两者同时写入，
        # 因此只有“启动完成但组件仍为空”才是真故障，启动前的空状态不该误报。
        if (
            state is not None
            and hasattr(state, "event_bus")
            and getattr(state, "plugin_manager", None) is None
        ):
            items.append(_attention_item(
                "health.plugin_manager", "critical", "插件管理器未就绪",
                "插件能力当前不可用。",
                action={"label": "查看运行状态", "tab": "settings", "section": "operations",
                        "path": "/system/operations"},
            ))
    except Exception as exc:
        logger.debug("读取插件管理器状态失败: %s", exc)

    try:
        from app.services.plugin_runtime import get_plugin_runtime_registry

        runtime_plugins = get_plugin_runtime_registry().snapshot(include_storage=False)
        unhealthy = [
            item for item in runtime_plugins
            if (item.get("health") or {}).get("status") in {"unhealthy", "failed"}
        ]
        for item in unhealthy[:2]:
            health = item.get("health") or {}
            items.append(_attention_item(
                f"plugin.{item.get('plugin_id')}", "warning",
                f"插件 {item.get('plugin_id')} 健康检查未通过",
                str(health.get("message") or "健康探针报告异常。"),
                at=health.get("checked_at"),
                action={"label": "查看插件", "tab": "plugins", "path": "/plugins"},
            ))
        degraded = [
            item for item in runtime_plugins
            if (item.get("health") or {}).get("status") == "degraded"
        ]
        if degraded:
            names = "、".join(str(item.get("plugin_id")) for item in degraded[:3])
            items.append(_attention_item(
                "plugin.degraded", "info", f"{len(degraded)} 个插件降级运行",
                f"{names}{' 等' if len(degraded) > 3 else ''} 只是部分能力受限。",
                action={"label": "查看插件", "tab": "plugins", "path": "/plugins"},
            ))
    except Exception as exc:
        logger.debug("读取插件运行时状态失败: %s", exc)

    try:
        from app.services.llm_manager import get_llm_manager

        health = get_llm_manager().get_model_health()
        open_circuits = [item for item in health if item.get("status") == "open"]
        for item in open_circuits[:2]:
            items.append(_attention_item(
                f"model.{item.get('model')}", "warning", f"模型熔断：{item.get('model')}",
                str(item.get("last_error") or "连续调用失败，已暂停路由。"),
                at=item.get("last_failure_at"),
                action={"label": "查看调用记录", "tab": "usage", "section": "llm-history",
                        "path": "/usage/calls"},
            ))
    except Exception as exc:
        logger.debug("读取模型健康失败: %s", exc)

    try:
        from app.services.backup_service import get_backup_service

        pending = get_backup_service().overview().get("pending_restore")
        if pending:
            items.append(_attention_item(
                "backup.pending_restore", "warning", "有待恢复的备份",
                "恢复尚未执行，确认后才会替换当前数据。",
                action={"label": "查看备份", "tab": "settings", "section": "backups",
                        "path": "/system/backups"},
            ))
    except Exception as exc:
        logger.debug("读取备份状态失败: %s", exc)


async def _attention_from_codex(items: List[Dict[str, Any]]) -> None:
    try:
        status = await _get_codex_status_payload(refresh=False)
    except Exception as exc:
        logger.debug("读取 Codex 额度失败: %s", exc)
        return
    if not status.get("profile_id"):
        return
    if not status.get("profile_available"):
        items.append(_attention_item(
            "codex.profile", "warning", "默认助手配置不可用",
            str(status.get("quota_message") or "Codex Profile 未就绪。"),
            action={"label": "查看 Codex", "tab": "codex", "path": "/codex"},
        ))
        return
    if not status.get("quota_available"):
        return
    for label, limit in (("主要限额", (status.get("rate_limits") or {}).get("primary")),
                         ("次要限额", (status.get("rate_limits") or {}).get("secondary"))):
        if not isinstance(limit, dict):
            continue
        remaining = limit.get("remaining_percent")
        if not isinstance(remaining, (int, float)):
            used = limit.get("used_percent")
            remaining = 100 - used if isinstance(used, (int, float)) else None
        if remaining is None:
            continue
        if remaining <= 10:
            severity = "critical"
        elif remaining <= 30:
            severity = "warning"
        else:
            continue
        resets_at = limit.get("resets_at")
        detail = f"剩余 {round(float(remaining))}%"
        if isinstance(resets_at, (int, float)):
            detail += f"，{datetime.fromtimestamp(resets_at).strftime('%m-%d %H:%M')} 重置"
        items.append(_attention_item(
            f"codex.quota.{label}", severity, f"Codex {label}即将耗尽", detail,
            action={"label": "查看 Codex", "tab": "codex", "path": "/codex"},
        ))


def _attention_from_storage(items: List[Dict[str, Any]]) -> None:
    try:
        import psutil

        usage = psutil.disk_usage("/")
        percent = (usage.used / usage.total * 100) if usage.total else 0
        if percent >= 80:
            items.append(_attention_item(
                "storage.disk", "critical" if percent >= 90 else "warning",
                f"磁盘占用 {percent:.0f}%",
                f"剩余 {usage.free / (1024 ** 3):.1f} GB，建议先做存储清理。",
                action={"label": "查看存储", "tab": "settings", "section": "operations",
                        "path": "/system/operations"},
            ))
    except Exception as exc:
        logger.debug("读取磁盘占用失败: %s", exc)


def _attention_from_incidents(items: List[Dict[str, Any]]) -> None:
    try:
        from app.services.incident_service import get_incident_service

        # 只有最近仍在复发的运行错误才需要人处理：同一指纹只要继续写日志，
        # last_seen 就会前移并留在待处理里；停止 24 小时后自动退出队列。
        incidents = get_incident_service().list(limit=6, within_hours=_INCIDENT_WINDOW_HOURS)
        for incident in incidents[:2]:
            level = str(incident.get("level") or "").upper()
            if level not in {"ERROR", "CRITICAL"}:
                continue
            items.append(_attention_item(
                f"incident.{incident.get('fingerprint')}",
                "critical" if level == "CRITICAL" else "warning",
                f"{incident.get('component') or '运行日志'} 出现错误",
                f"{str(incident.get('message') or '')[:120]}（累计 {incident.get('count') or 1} 次）",
                at=incident.get("last_seen"),
                action={"label": "查看运行与日志", "tab": "settings", "section": "operations",
                        "path": "/system/operations"},
            ))
    except Exception as exc:
        logger.debug("读取运行事件失败: %s", exc)


def _collect_attention_blocking(request: Request) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    _attention_from_wechat(request, items)
    _attention_from_health(request, items)
    _attention_from_storage(items)
    _attention_from_incidents(items)
    return items


@router.get("/attention")
async def get_dashboard_attention(request: Request) -> Dict[str, Any]:
    """汇总需要人工处理的异常；没有事项时返回空列表，概览页会整块隐藏。"""
    items = await asyncio.to_thread(_collect_attention_blocking, request)
    await _attention_from_codex(items)

    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    # 先按时间倒序，再按严重度稳定排序：同级内最新的排在前面。
    items.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
    items.sort(key=lambda item: severity_rank.get(item.get("severity"), 3))
    summary = {
        "critical": sum(item["severity"] == "critical" for item in items),
        "warning": sum(item["severity"] == "warning" for item in items),
        "info": sum(item["severity"] == "info" for item in items),
    }
    summary["total"] = len(items)
    return {
        "items": items[:_ATTENTION_LIMIT],
        "summary": summary,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _event_time(value: Any) -> Optional[str]:
    """把 epoch、ISO 或 "YYYY-MM-DD HH:MM:SS" 统一成可排序的本地时间串。"""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    if "T" in text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return text
    return text[:19]


def _timeline_from_dashboard_events(items: List[Dict[str, Any]]) -> None:
    for event in get_recent_events(limit=30, event_types=["judge_decision"]):
        payload = event.get("payload") or {}
        should_reply = payload.get("should_reply")
        title = "判决：跳过一次回复" if should_reply is False else "判决：参与对话"
        detail_parts = [str(payload.get("reason") or "").strip()]
        role = payload.get("role_name") or payload.get("judge_name")
        if role:
            detail_parts.append(f"角色 {role}")
        items.append({
            "kind": "judge",
            "level": "info",
            "title": title,
            "detail": " · ".join(part for part in detail_parts if part)[:200],
            "chat_name": payload.get("chat_name") or None,
            "at": _event_time(event.get("timestamp")),
            "action": None,
        })


def _timeline_from_audit(items: List[Dict[str, Any]]) -> None:
    try:
        from app.services.runtime_operations import get_runtime_operation_service

        for record in get_runtime_operation_service().list_audit(limit=15):
            status = str(record.get("status") or "success").lower()
            level = "error" if status in {"failed", "error"} else ("warning" if status in {"cancelled", "skipped"} else "success")
            target = str(record.get("target") or "").strip()
            items.append({
                "kind": "plugin" if str(record.get("category") or "") == "plugin" else "system",
                "level": level,
                "title": f"{record.get('action') or '变更'}{' · ' + target if target else ''}",
                "detail": str(record.get("summary") or "")[:200],
                "chat_name": None,
                "at": _event_time(record.get("created_at")),
                "action": {"label": "查看审计", "tab": "settings", "section": "operations",
                           "path": "/system/operations"},
            })
    except Exception as exc:
        logger.debug("读取审计记录失败: %s", exc)


def _timeline_from_operations(items: List[Dict[str, Any]]) -> None:
    try:
        from app.services.runtime_operations import get_runtime_operation_service

        for record in get_runtime_operation_service().list(limit=12):
            status = str(record.get("status") or "").lower()
            if status == "running":
                continue
            level = {
                "completed": "success", "succeeded": "success",
                "failed": "error", "timeout": "error",
                "cancelled": "warning", "interrupted": "warning",
            }.get(status, "info")
            items.append({
                "kind": "task",
                "level": level,
                "title": str(record.get("title") or "后台任务"),
                "detail": str(record.get("message") or "").strip()[:200] or f"状态：{status or '未知'}",
                "chat_name": None,
                "at": _event_time(record.get("ended_at") or record.get("updated_at") or record.get("created_at")),
                "action": {"label": "查看任务", "tab": "settings", "section": "operations",
                           "path": "/system/operations"},
            })
    except Exception as exc:
        logger.debug("读取后台任务失败: %s", exc)


def _timeline_from_codex(items: List[Dict[str, Any]]) -> None:
    try:
        from app.services.codex_job_manager import codex_job_manager

        for job in codex_job_manager.list_recent(limit=10):
            status = str(job.get("status") or "").lower()
            level = {"completed": "success", "failed": "error", "timeout": "error",
                     "cancelled": "warning"}.get(status, "info")
            chat_name = str(job.get("chat_name") or "").strip() or None
            detail_parts = [str(job.get("model") or "").strip(), f"状态：{status or '未知'}"]
            items.append({
                "kind": "codex",
                "level": level,
                "title": "Codex 任务",
                "detail": " · ".join(part for part in detail_parts if part)[:200],
                "chat_name": chat_name,
                "at": _event_time(job.get("ended_at") or job.get("updated_at") or job.get("started_at")),
                "action": {"label": "查看 Codex", "tab": "codex", "path": "/codex"},
            })
    except Exception as exc:
        logger.debug("读取 Codex 任务失败: %s", exc)


def _timeline_from_llm_failures(items: List[Dict[str, Any]]) -> None:
    try:
        from app.services.llm_manager import get_llm_manager

        page = get_llm_manager().usage_service.requests(period="today", limit=50)
        failures = [
            row for row in page.get("rows", [])
            if not row.get("success") and row.get("scope") == "request"
        ]
        for row in failures[:3]:
            pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
            reason = str(pricing.get("reason") or "").strip()
            detail = f"计价状态 {reason}" if reason else "上游请求未成功，失败已计入用量统计"
            items.append({
                "kind": "llm",
                "level": "error",
                "title": f"模型调用失败 · {row.get('model') or '未知模型'}",
                "detail": detail[:200],
                "chat_name": None,
                "at": _event_time(row.get("recorded_at")),
                "action": {"label": "查看调用记录", "tab": "usage", "section": "llm-history",
                           "path": "/usage/calls"},
            })
    except Exception as exc:
        logger.debug("读取模型调用失败记录失败: %s", exc)


@router.get("/events")
def get_dashboard_events(limit: int = Query(40, ge=5, le=100)) -> Dict[str, Any]:
    """系统级事件时间线：判决、插件与系统变更、后台任务、Codex 任务、调用失败。"""
    items: List[Dict[str, Any]] = []
    _timeline_from_dashboard_events(items)
    _timeline_from_audit(items)
    _timeline_from_operations(items)
    _timeline_from_codex(items)
    _timeline_from_llm_failures(items)

    def sort_key(item: Dict[str, Any]) -> str:
        return str(item.get("at") or "")

    items = [item for item in items if item.get("at")]
    items.sort(key=sort_key, reverse=True)
    kinds = sorted({item["kind"] for item in items})
    return {
        "items": items[: max(5, min(int(limit), _EVENT_LIMIT))],
        "kinds": kinds,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
