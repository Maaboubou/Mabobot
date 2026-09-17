"""Runtime evidence, separate from saved administrator intent.

Only the App Server publishes capabilities after testing its actual process.
Unknown support is fail-closed. Evidence is discarded at process restart.
"""

import threading
import uuid
import json
import os
from datetime import datetime, timezone

RUNTIME_INSTANCE = uuid.uuid4().hex
_lock = threading.RLock()
_workers: dict[str, dict] = {}

# No model request or application credentials are involved in this probe. A
# private HTTP denial proves the listener is active; a direct socket must fail
# at the sandbox boundary, not merely time out or find a closed remote port.
PUBLIC_PROXY_PROBE = '''import errno,json,os,socket,urllib.request,urllib.error,urllib.parse
p=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
u=urllib.parse.urlsplit(p)
assert u.scheme=="http" and u.hostname in ("127.0.0.1","localhost","::1")
opener=urllib.request.build_opener(urllib.request.ProxyHandler({"http":p,"https":p}))
denied=False
try:
    opener.open("http://169.254.169.254/",timeout=3)
except urllib.error.HTTPError as e:
    denied=e.code==403 and json.loads(e.read(2048)).get("reason")=="not_allowed_local"
assert denied
s=socket.socket();s.settimeout(2)
try:
    result=s.connect_ex(("1.1.1.1",443))
except OSError as e:
    result=e.errno
finally:
    s.close()
assert result in (errno.EPERM,errno.EACCES,errno.ENETUNREACH)
print("MABOBOT_PUBLIC_PROXY_VERIFIED")
'''


def probe_worker(manager) -> dict:
    support = dict(permission_profiles=False, runtime_workspace_roots=False, network_proxy=False,
                   auto_review=False, bounded_auto_review=False, web_search=False, browser=manager.browser_tool.enabled)
    try:
        config = manager._request("config/read", {"includeLayers": False}, timeout=10, ensure_started=False).get("config", {})
        support["auto_review"] = config.get("approvals_reviewer") == "auto_review"
        support["web_search"] = "web_search" in config and manager.model_supports_web_search is not False
        # Exercise the profile parser and sandbox without starting a model turn.
        result = manager._request("command/exec", {"command": ["/bin/sh", "-c", "printf MABOBOT_PROFILE_VERIFIED"],
            "permissionProfile": "mabobot-isolated-ro-offline", "timeoutMs": 5000}, timeout=10, ensure_started=False)
        support["permission_profiles"] = result.get("exitCode") == 0 and result.get("stdout") == "MABOBOT_PROFILE_VERIFIED"
        support["runtime_workspace_roots"] = support["permission_profiles"]
        if support["permission_profiles"]:
            result = manager._request("command/exec", {"command": ["python3", "-c", PUBLIC_PROXY_PROBE],
                "permissionProfile": "mabobot-isolated-ro-public", "timeoutMs": 8000}, timeout=12, ensure_started=False)
            support["network_proxy"] = result.get("exitCode") == 0 and result.get("stdout", "").strip() == "MABOBOT_PUBLIC_PROXY_VERIFIED"
    except Exception:
        # A failed capability probe cannot broaden a chat or break offline work.
        pass
    publish_support(manager.permission_worker_id, manager.managed_profile_id, support)
    return support


def publish_support(worker: str, profile_id: str, support: dict) -> None:
    with _lock:
        _workers[worker] = {**support, "profile_id": profile_id}
    # The Web console and WeChat listener can be separate processes.
    try:
        from app.models.base import SessionLocal
        from app.models.codex_permission import CodexPermissionRuntimeState
        with SessionLocal() as db:
            row = db.get(CodexPermissionRuntimeState, worker)
            if row is None:
                row = CodexPermissionRuntimeState(worker_id=worker)
                db.add(row)
            row.profile_id, row.process_id = profile_id, os.getpid()
            row.support_json = json.dumps(support)
            row.checked_at = datetime.now(timezone.utc).isoformat()
            db.commit()
    except Exception:
        # Pre-migration/standalone probes can still execute, but cannot claim
        # application success in another process without a persisted receipt.
        pass


def remove_support(worker: str) -> None:
    with _lock:
        _workers.pop(worker, None)
    try:
        from app.models.base import SessionLocal
        from app.models.codex_permission import CodexPermissionRuntimeState
        with SessionLocal() as db:
            db.query(CodexPermissionRuntimeState).filter_by(worker_id=worker).delete()
            db.commit()
    except Exception:
        pass


def active_workers(db=None):
    from app.models.base import SessionLocal
    from app.models.codex_permission import CodexPermissionRuntimeState
    owns_session = db is None
    db = db or SessionLocal()
    result = {}
    try:
        for row in db.query(CodexPermissionRuntimeState):
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row.checked_at)).total_seconds()
            if age < 0 or age > 180:
                continue
            try:
                if os.name != "nt":
                    os.kill(row.process_id, 0)
                support = json.loads(row.support_json)
            except (OSError, ValueError):
                continue
            result[row.worker_id] = {**support, "profile_id": row.profile_id}
        return result
    except Exception:
        return {}
    finally:
        if owns_session:
            db.close()


def runtime_support(profile_id: str | None = None, *, db=None) -> dict:
    known = active_workers(db)
    with _lock:
        known.update(_workers)
    workers = [w for w in known.values() if profile_id is None or w["profile_id"] == profile_id]
    keys = ("permission_profiles", "runtime_workspace_roots", "network_proxy", "auto_review", "bounded_auto_review", "web_search", "browser")
    result = {key: bool(workers) and all(w.get(key, False) for w in workers) for key in keys}
    result["checked"] = bool(workers)
    result["reason"] = (
        "运行时尚未验证；将在下次 Codex 请求启动时检查" if not workers
        else "自动审阅尚未通过运行时验证，越界操作保持直接拒绝"
        if not result["auto_review"] else "自动审阅依据宿主规则和聊天上下文判断；模型审阅不等同于绝对隔离"
    )
    return result
