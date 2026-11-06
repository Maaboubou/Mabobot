"""Local and Tailscale Serve gateway to private ttyd processes."""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
import httpx
from starlette.concurrency import run_in_threadpool

from app.services.terminal_service import (
    TerminalConflict, TerminalError, TerminalNotFound, get_terminal_service,
)
from app.services.terminal_access import discover_serve_origins, origin_key

router = APIRouter()
_LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}
_ID_PATTERN = r"^[a-f0-9]{32}$"


def require_terminal_access(connection, *, mutation=False, websocket=False):
    """Accept direct localhost or this node's verified Tailscale Serve routes."""
    try:
        peer = connection.client.host
        if not ipaddress.ip_address(peer).is_loopback:
            raise ValueError()
        if "tailscale-funnel-request" in connection.headers:
            raise ValueError()
        host = connection.headers.get("host", "")
        authority = urlsplit("http://" + host)
        # Validate Host independently before consulting local discovery.
        origin_key("http://" + host)
        forwarded = {name.lower() for name in connection.headers
                     if name.lower() == "forwarded" or name.lower().startswith("x-forwarded-")
                     or name.lower() == "x-real-ip"}
        if authority.hostname in _LOCAL_NAMES and not forwarded:
            scheme = connection.url.scheme.replace("ws", "http", 1) if websocket else connection.url.scheme
            expected_origin = origin_key(scheme + "://" + host)
        else:
            if forwarded - {"x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"}:
                raise ValueError()
            if connection.headers.get("x-forwarded-host", "").lower() != host.lower():
                raise ValueError()
            # Serve replaces this with one client address; chains are not trusted.
            ipaddress.ip_address(connection.headers.get("x-forwarded-for", ""))
            scheme = connection.headers.get("x-forwarded-proto", "http")
            expected_origin = origin_key(scheme + "://" + host)
            backend_port = connection.scope["server"][1]
            allowed = {origin_key(value) for value in discover_serve_origins(backend_port)}
            if expected_origin not in allowed:
                raise ValueError()
        if connection.headers.get("sec-fetch-site", "").lower() == "cross-site":
            raise ValueError()
        origin = connection.headers.get("origin", "")
        if origin:
            if origin_key(origin) != expected_origin:
                raise ValueError()
        elif mutation or websocket:
            raise ValueError()
    except (AttributeError, ValueError, TypeError, KeyError, IndexError):
        raise HTTPException(403, "请通过本机地址或当前节点已配置的 Tailscale Serve 地址访问终端") from None


def _service_call(action, *args):
    try:
        return action(*args)
    except TerminalNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except TerminalConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except TerminalError as exc:
        raise HTTPException(422, str(exc)) from exc


class CreateSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, max_length=48, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    full_access: bool = Field(default=True, strict=True)


@router.get("")
def overview(request: Request):
    require_terminal_access(request)
    service = get_terminal_service()
    status = _service_call(service.status)
    sessions = {"sessions": []} if status.get("runtime") == "windows" else _service_call(service.list_sessions)
    return {**status, **sessions}


@router.get("/sessions")
def list_sessions(request: Request):
    require_terminal_access(request)
    return _service_call(get_terminal_service().list_sessions)


@router.post("/sessions")
def create_session(request: Request, payload: CreateSession):
    require_terminal_access(request, mutation=True)
    return _service_call(get_terminal_service().create, payload.profile_id, payload.full_access)


@router.delete("/sessions/{session_id}")
def stop_session(request: Request, session_id: str):
    require_terminal_access(request, mutation=True)
    return _service_call(get_terminal_service().stop, session_id)


def _private_connection(session_id):
    if not isinstance(session_id, str) or not re.fullmatch(_ID_PATTERN, session_id):
        raise HTTPException(404, "终端会话不存在")
    connection = _service_call(get_terminal_service().get_connection, session_id)
    base = connection["base_url"]
    try:
        parsed = urlsplit(base)
        valid = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
                 and not parsed.username and not parsed.password and parsed.path in {"", "/"}
                 and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(502, "终端连接地址无效")
    path = f"/api/terminal/sessions/{session_id}/view"
    if connection.get("base_path", path) != path:
        raise HTTPException(502, "终端连接路径无效")
    credential = base64.b64encode(
        (connection["username"] + ":" + connection["password"]).encode()).decode()
    return base.rstrip("/"), path, credential


_PRIVATE_RESPONSE_HEADERS = {
    "Cache-Control": "no-store", "X-Frame-Options": "SAMEORIGIN",
    "Content-Security-Policy": "frame-ancestors 'self'", "X-Content-Type-Options": "nosniff",
}


@router.get("/sessions/{session_id}/view", include_in_schema=False)
def terminal_redirect(request: Request, session_id: str):
    require_terminal_access(request)
    if request.url.query:
        raise HTTPException(404, "终端资源不存在")
    _private_connection(session_id)
    return RedirectResponse(f"/api/terminal/sessions/{session_id}/view/", status_code=307,
                            headers=_PRIVATE_RESPONSE_HEADERS)


@router.get("/sessions/{session_id}/view/{path:path}", include_in_schema=False)
async def terminal_http(request: Request, session_id: str, path: str):
    await run_in_threadpool(require_terminal_access, request)
    if path not in {"", "token", "favicon.ico"} or request.url.query:
        raise HTTPException(404, "终端资源不存在")
    base, prefix, credential = await run_in_threadpool(_private_connection, session_id)
    if path == "token":
        # ttyd otherwise exposes its Basic credential to JS. Supply the real
        # authentication only in the gateway's initial upstream WS message.
        return JSONResponse({"token": ""}, headers=_PRIVATE_RESPONSE_HEADERS)
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=15, follow_redirects=False) as client:
            async with client.stream("GET", base + prefix + "/" + path,
                                     headers={"Authorization": "Basic " + credential}) as upstream:
                if upstream.status_code != 200:
                    raise HTTPException(502, "终端页面暂不可用，请重新连接")
                chunks, size = [], 0
                async for chunk in upstream.aiter_bytes():
                    size += len(chunk)
                    if size > 10 * 1024 * 1024:
                        raise HTTPException(502, "终端页面超过大小限制")
                    chunks.append(chunk)
                return Response(b"".join(chunks), media_type=upstream.headers.get("content-type", "text/html"),
                                headers=_PRIVATE_RESPONSE_HEADERS)
    except httpx.HTTPError:
        raise HTTPException(502, "无法连接终端，请刷新会话列表") from None


@router.websocket("/sessions/{session_id}/view/ws")
async def terminal_websocket(websocket: WebSocket, session_id: str):
    try:
        await run_in_threadpool(require_terminal_access, websocket, websocket=True)
        if websocket.url.query or "tty" not in websocket.scope.get("subprotocols", []):
            raise HTTPException(403, "终端连接格式无效")
        base, prefix, credential = await run_in_threadpool(_private_connection, session_id)
    except HTTPException:
        await websocket.close(code=1008)
        return
    from websockets.asyncio.client import connect
    try:
        async with connect(base.replace("http://", "ws://", 1) + prefix + "/ws",
                           origin=base, subprotocols=["tty"],
                           additional_headers={"Authorization": "Basic " + credential},
                           proxy=None, max_size=1024 * 1024, open_timeout=10,
                           close_timeout=2) as upstream:
            await websocket.accept(subprotocol="tty")

            async def browser_to_terminal():
                # First frame is ttyd's size/auth handshake, not terminal input.
                initial = await asyncio.wait_for(websocket.receive(), timeout=15)
                value = initial.get("text")
                if value is None:
                    value = (initial.get("bytes") or b"").decode("utf-8")
                if len(value) > 4096:
                    raise ValueError("oversized handshake")
                payload = json.loads(value)
                if not isinstance(payload, dict):
                    raise ValueError("invalid handshake")
                payload = {"AuthToken": credential,
                           "columns": max(2, min(500, int(payload.get("columns", 80)))),
                           "rows": max(2, min(300, int(payload.get("rows", 24))))}
                await upstream.send(json.dumps(payload))
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    data = message.get("bytes")
                    if data is None:
                        data = message.get("text", "")
                    if len(data) > 1024 * 1024:
                        raise ValueError("oversized terminal frame")
                    await upstream.send(data)

            async def terminal_to_browser():
                async for data in upstream:
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)
                    else:
                        await websocket.send_text(data)

            tasks = [asyncio.create_task(browser_to_terminal()), asyncio.create_task(terminal_to_browser())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (OSError, ValueError, UnicodeError, TimeoutError, WebSocketDisconnect):
        pass
    except Exception:
        # Terminal frames and provider diagnostics may contain secrets; do not log them.
        pass
    finally:
        try:
            await websocket.close(code=1000)
        except (RuntimeError, WebSocketDisconnect):
            pass
