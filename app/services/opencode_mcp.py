"""Stable authenticated MCP catalog with immutable, host-registered Session leases."""
from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import secrets
import threading

from app.services.execution_contracts import ExecutionError, canonical, execution_context


@dataclass(frozen=True)
class SessionBinding:
    runtime_generation: str
    profile_id: str
    session_id: str
    handle: object
    tools: frozenset
    executor: object


class HostMCP:
    def __init__(self, *, generation, profile_id, catalog=(), authorize=lambda context: False):
        self.generation, self.profile_id = generation, profile_id
        self.token = secrets.token_urlsafe(32)
        self.catalog = tuple(catalog)
        self.authorize = authorize
        self._bindings, self._retired = {}, set()
        self._lock = threading.RLock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_error(405)

            def do_POST(self):
                self.connection.settimeout(15)
                if self.path != "/mcp" or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + owner.token):
                    self.send_error(401)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2 * 1024 * 1024:
                        raise ValueError("request size")
                    message = json.loads(self.rfile.read(length))
                    if not isinstance(message, dict):
                        raise ValueError("request type")
                    if "id" not in message:
                        self.send_response(202)
                        self.end_headers()
                        return
                    result = owner.dispatch(message.get("method"), message.get("params", {}))
                    response = {"jsonrpc": "2.0", "id": message["id"], "result": result}
                except Exception:
                    response = {"jsonrpc": "2.0", "id": locals().get("message", {}).get("id"),
                                "error": {"code": -32602, "message": "Invalid or unauthorized MCP request"}}
                data = canonical(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="opencode-mcp")
        self._thread.start()

    @property
    def config(self):
        return {"type": "http", "name": "mabobot", "url": f"http://127.0.0.1:{self.server.server_port}/mcp",
                "headers": [{"name": "Authorization", "value": "Bearer " + self.token}]}

    def bind(self, session_id, handle, tools, executor):
        names = {tool["name"] for tool in self.catalog}
        if not set(tools) <= names:
            raise ExecutionError("tool_unavailable", "Requested host tool is not in the stable catalog")
        with self._lock:
            if session_id in self._bindings or session_id in self._retired:
                raise ExecutionError("session_bound", "A native Session cannot be rebound to another task")
            self._bindings[session_id] = SessionBinding(self.generation, self.profile_id, session_id, handle, frozenset(tools), executor)

    def retire(self, session_id):
        with self._lock:
            self._retired.add(session_id)
            self._bindings.pop(session_id, None)

    def dispatch(self, method, params):
        if method == "initialize":
            return {"protocolVersion": params.get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mabobot-host", "version": "1.0.0"}}
        if method == "tools/list":
            return {"tools": list(self.catalog)}
        if method == "ping":
            return {}
        if method != "tools/call":
            raise ValueError("Unsupported MCP method")
        session = params.get("_meta", {}).get("ai.opencode/sessionID")
        with self._lock:
            binding = self._bindings.get(session)
        if binding is None:
            return self._error("Unknown or retired native Session")
        context = binding.handle.context  # capture before entering slow business code
        name = params.get("name")
        try:
            if name not in binding.tools or not self.authorize(context):
                raise ExecutionError("permission_denied", "Tool is not currently authorized")
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be an object")
            spec = next(t for t in self.catalog if t["name"] == name)
            from jsonschema import validate
            validate(arguments, spec["inputSchema"])
            with binding.handle.tool(), execution_context(context):
                result = binding.executor(name, arguments)
                with self._lock:
                    active = self._bindings.get(session) is binding
                if not active or binding.handle.cancel_requested.is_set() or not self.authorize(context):
                    raise ExecutionError("cancelled", "Tool completed after its authorization or Session ended")
            from app.history.image_reader import AttachmentImageResult
            if isinstance(result, AttachmentImageResult):
                header, data = result.image_url.split(',', 1)
                mime = header.removeprefix('data:').removesuffix(';base64')
                return {"content": [{"type": "text", "text": canonical(result.metadata)},
                                    {"type": "image", "mimeType": mime, "data": data}], "isError": False}
            return {"content": [{"type": "text", "text": canonical(result)}], "isError": False}
        except Exception as exc:
            # Host errors are deliberately not a place to dump native request/credential details.
            return self._error(str(exc) if isinstance(exc, ExecutionError) else "Tool input or execution failed")

    @staticmethod
    def _error(message):
        return {"content": [{"type": "text", "text": message}], "isError": True}

    def close(self):
        with self._lock:
            self._retired.update(self._bindings)
            self._bindings.clear()
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=2)
