"""Strict Codex generation, checked at the native HTTP boundary.

This module is stdlib-only so the same implementation can run inside WSL.
The relay never reads credential files or retries a request. Codex owns
authentication. The host builds the inference envelope from the admitted task,
so native agent scaffolding cannot become model input or tool declarations.
"""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import secrets
import signal
import socket
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.services.execution_contracts import ExecutionError, canonical

ADAPTER_VERSION = "codex-direct-2"
MAX_BODY = 2 * 1024 * 1024


def direct_spec(request):
    data = json.loads(request.envelope.input_json)
    if not isinstance(data, str) or json.loads(request.envelope.assets_json):
        raise ExecutionError("capability_unavailable", "Codex 直接生成暂只支持独立文本输入", retryable=True)
    params = json.loads(request.candidate.parameters_json)
    if set(params) - {"reasoning_effort", "temperature", "max_tokens", "response_format"} or any(
        params.get(k) is not None for k in ("temperature", "max_tokens")
    ):
        raise ExecutionError("capability_unavailable", "Codex 直接生成不能保证这些采样或长度参数", retryable=True)
    fmt = params.get("response_format") or {}
    schema = json.loads(request.route.output_contract_json).get("schema")
    if fmt and fmt.get("type") != "json_schema":
        raise ExecutionError("capability_unavailable", "Codex 直接生成仅支持 JSON Schema 输出格式", retryable=True)
    if fmt:
        selected = (fmt.get("json_schema") or {}).get("schema")
        if not isinstance(selected, dict) or schema is not None and schema != selected:
            raise ExecutionError("capability_unavailable", "Codex 输出格式与任务契约不一致", retryable=True)
        schema = selected
    if schema is not None and not isinstance(schema, dict):
        raise ExecutionError("capability_unavailable", "Codex 输出 Schema 必须是对象", retryable=True)
    spec = {"model": request.candidate.model, "instructions": request.envelope.instructions,
            "input": data, "effort": params.get("reasoning_effort"), "schema": schema,
            "deadline": request.context.deadline}
    options = json.loads(request.candidate.profile_config_json)
    spec["service_tier"] = "fast" if options.get("fast_mode") else "default"
    if options.get("context_window"):
        spec["context_window"] = options["context_window"]
    if len(canonical(spec).encode()) > MAX_BODY // 2:
        raise ExecutionError("input_limit", "Codex direct input exceeds the verified request limit")
    return spec


def verify_outbound(body, spec):
    """Validate parsed *serialized* native HTTP content; never log its contents."""
    allowed = {"model", "instructions", "input", "tools", "tool_choice", "parallel_tool_calls",
               "reasoning", "store", "stream", "include", "prompt_cache_key", "text", "service_tier",
               "client_metadata"}
    violations = []
    if not isinstance(body, dict):
        body = {}
    if set(body) - allowed:
        violations.append("unexpected_fields")
    if body.get("model") != spec["model"]:
        violations.append("model_mismatch")
    if body.get("tools") not in (None, []) or body.get("tool_choice") not in (None, "auto", "none"):
        violations.append("tools_present")
    # Message IDs are transport details. Everything model-visible is compared
    # exactly, including role, ordering and text (no substring/marker checks).
    messages = []
    for item in body.get("input", []) if isinstance(body.get("input"), list) else [None]:
        if (not isinstance(item, dict) or item.get("type", "message") != "message"
                or set(item) - {"type", "id", "role", "content"}):
            violations.append("non_task_input")
            continue
        content = item.get("content")
        if not isinstance(content, list) or any(not isinstance(p, dict) or set(p) != {"type", "text"}
                or p.get("type") != "input_text" or not isinstance(p.get("text"), str) for p in content):
            violations.append("non_text_input")
            continue
        messages.append((item.get("role"), "".join(p["text"] for p in content)))
    expected = [(m["role"], m["content"]) for m in spec.get("input_messages", [{"role": "user", "content": spec["input"]}])]
    if spec.get("developer_instructions"):
        expected.insert(0, ("developer", spec["developer_instructions"]))
    if body.get("instructions") == spec["instructions"]:
        pass
    elif body.get("instructions") in (None, ""):
        expected.insert(0, ("developer", spec["instructions"]))
    else:
        violations.append("instructions_mismatch")
    if messages != expected:
        violations.append("messages_mismatch")
    reasoning = body.get("reasoning") or {}
    if not isinstance(reasoning, dict) or set(reasoning) - {"effort", "summary", "context"}:
        violations.append("reasoning_mismatch")
    elif spec.get("effort") is not None and reasoning.get("effort") != spec["effort"]:
        violations.append("reasoning_mismatch")
    elif reasoning.get("summary") not in (None, "none", "auto", "concise", "detailed") or reasoning.get("context") not in (None, "all_turns"):
        violations.append("reasoning_mismatch")
    text = body.get("text") or {}
    if not isinstance(text, dict) or set(text) - {"format", "verbosity"}:
        violations.append("format_mismatch")
    else:
        fmt = text.get("format") or {}
        if spec.get("schema") is not None:
            if (not isinstance(fmt, dict) or set(fmt) - {"type", "name", "strict", "schema"}
                    or fmt.get("type") != "json_schema" or fmt.get("schema") != spec["schema"]):
                violations.append("format_mismatch")
        elif fmt and fmt != {"type": "text"}:
            violations.append("format_mismatch")
    if body.get("stream") is not True or body.get("store") is not False:
        violations.append("transport_mismatch")
    if body.get("include") not in (None, [], ["reasoning.encrypted_content"]):
        violations.append("unexpected_include")
    return {"verified": not violations, "violations": sorted(set(violations)),
            "message_count": len(messages), "transport": "http_sse"}


def task_outbound(body, spec):
    """Keep native authentication, but use only the host's immutable task data."""
    if not isinstance(body, dict) or body.get("model") != spec["model"]:
        raise ValueError("model_mismatch")
    reasoning = body.get("reasoning") or {}
    if not isinstance(reasoning, dict) or (
        spec.get("effort") is not None and reasoning.get("effort") != spec["effort"]
    ):
        raise ValueError("reasoning_mismatch")
    payload = {
        "model": spec["model"], "instructions": spec["instructions"],
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": spec["input"]}]}],
        "tools": [], "tool_choice": "none", "parallel_tool_calls": False,
        "store": False, "stream": True,
    }
    if "input_messages" in spec:
        payload["input"] = [{"type": "message", "role": m["role"], "content": [
            {"type": "input_text", "text": m["content"]}]} for m in spec["input_messages"]]
    if spec.get("developer_instructions"):
        payload["input"].insert(0, {"type": "message", "role": "developer", "content": [
            {"type": "input_text", "text": spec["developer_instructions"]}]})
    if spec.get("effort") is not None:
        payload["reasoning"] = {"effort": spec["effort"]}
    if spec.get("schema") is not None:
        payload["text"] = {"format": {"type": "json_schema", "name": "result",
            "strict": True, "schema": spec["schema"]}}
    if spec.get("service_tier") == "fast":
        payload["service_tier"] = "priority"
    return payload


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class OutboundGate:
    """One task, one private endpoint, at most one forwarded generation."""
    def __init__(self, spec, upstream, cancelled, *, auth_headers=(), task_envelope=False):
        parsed = urlsplit(upstream)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ExecutionError("capability_unavailable", "Unsupported native provider endpoint")
        self.spec, self.upstream, self.cancelled = spec, upstream, cancelled
        self.task_envelope = task_envelope
        self.auth_headers = {name.lower() for name in auth_headers} - {"host", "connection", "content-length", "transfer-encoding"}
        self.lock = threading.Lock()
        self.shape = {"verified": False, "violations": [], "request_count": 0, "forwarded_count": 0}
        self.error_code = ""
        self.retry_after = None
        self.usage, self.actual_model = None, ""
        self._connections = set()
        self._closing = False
        self.path = "/" + secrets.token_urlsafe(24) + "/responses"
        gate = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass  # Credentials and prompts must never reach HTTP logs.

            def do_POST(self):
                gate._post(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}" + self.path.removesuffix("/responses")

    @staticmethod
    def _reject(handler, status=400):
        data = b'{"error":{"message":"Codex direct outbound contract rejected"}}'
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _post(self, handler):
        if handler.path != self.path:
            self._reject(handler, 404)
            return
        try:
            length = int(handler.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY or handler.headers.get("Content-Encoding") or handler.headers.get("Transfer-Encoding"):
                raise ValueError("unsupported framing")
            handler.connection.settimeout(max(0.1, min(10, self.spec["deadline"] - time.time())))
            raw = handler.rfile.read(length)
            native = json.loads(raw)
            native_shape = verify_outbound(native, self.spec)
            if self.task_envelope:
                raw = canonical(task_outbound(native, self.spec)).encode("utf-8")
            shape = verify_outbound(json.loads(raw), self.spec)
            shape["envelope_source"] = "host_task" if self.task_envelope else "native"
            shape["native_violations"] = native_shape["violations"]
        except (ValueError, OSError) as exc:
            reason = str(exc) if str(exc) in {"model_mismatch", "reasoning_mismatch"} else "invalid_http_body"
            shape = {"verified": False, "violations": [reason]}
        with self.lock:
            count = self.shape["request_count"] + 1
            forwarded = self.shape["forwarded_count"]
            if count != 1:
                shape = {"verified": False, "violations": ["duplicate_request"]}
            self.shape = {**shape, "request_count": count, "forwarded_count": forwarded}
            if not shape["verified"]:
                self.error_code = "interrupted_unknown" if forwarded else "outbound_contract_mismatch"
            elif self._closing or self.cancelled.is_set() or time.time() >= self.spec["deadline"]:
                self.error_code = "cancelled"
            else:
                self.shape["forwarded_count"] += 1
            rejected = bool(self.error_code)
        if rejected:
            self._reject(handler)
            return
        # Forward only native authentication and transport headers to the
        # original configured endpoint. Never follow a credential-bearing redirect.
        headers = {k: v for k, v in handler.headers.items() if k.lower() in self.auth_headers | {
            "authorization", "chatgpt-account-id", "openai-organization", "openai-project",
            "openai-beta", "user-agent", "originator", "version", "session_id"}}
        headers["Content-Type"] = "application/json"
        try:
            response = build_opener(_NoRedirect()).open(Request(self.upstream, data=raw, headers=headers),
                timeout=max(0.1, self.spec["deadline"] - time.time()))
        except HTTPError as exc:
            response = exc
        except OSError:
            self.error_code = "interrupted_unknown"
            self._reject(handler, 502)
            return
        try:
            with response:
                upstream_socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                with self.lock:
                    if upstream_socket:
                        self._connections.add(upstream_socket)
                    closing = self._closing
                if closing:
                    return
                if response.status != 200:
                    self.shape["upstream_status"] = response.status
                    self.error_code = {401: "authentication_failed", 403: "authentication_failed", 429: "rate_limited"}.get(response.status, "provider_error")
                    try:
                        self.retry_after = min(3600, max(0, float(response.headers.get("Retry-After", "30"))))
                    except ValueError:
                        self.retry_after = 30
                handler.send_response(response.status)
                handler.send_header("Content-Type", response.headers.get("Content-Type", "text/event-stream"))
                handler.end_headers()
                while not self.cancelled.is_set() and time.time() < self.spec["deadline"]:
                    chunk = response.readline(MAX_BODY + 1)
                    if not chunk:
                        break
                    if len(chunk) > MAX_BODY:
                        self.error_code = "output_truncated"
                        break
                    if response.status == 200 and not self._response_line_allowed(chunk):
                        break
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
        except OSError:
            if not self.error_code:
                self.error_code = "interrupted_unknown"
        finally:
            with self.lock:
                self._connections.discard(locals().get("upstream_socket"))

    def _response_line_allowed(self, line):
        if not line.startswith(b"data:"):
            return True
        raw = line[5:].strip()
        if raw == b"[DONE]":
            return True
        try:
            event = json.loads(raw)
            response = event.get("response") or {}
            if isinstance(response.get("usage"), dict):
                self.usage = response["usage"]
            if isinstance(response.get("model"), str):
                self.actual_model = response["model"]
            items = [event["item"]] if "item" in event else response.get("output", [])
            if any(item.get("type") not in {"message", "reasoning"} for item in items):
                self.error_code = "unexpected_tool_output"
                return False
            if response.get("model") and response["model"] != self.spec["model"]:
                self.error_code = "model_mismatch"
                return False
            if response.get("status") == "incomplete":
                self.error_code = "output_truncated"
                return False
        except (ValueError, TypeError, AttributeError):
            self.error_code = "interrupted_unknown"
            return False
        return True

    def close(self):
        with self.lock:
            self._closing = True
            for connection in self._connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


# Startup overrides suppress discovery before any thread exists. Per-task
# baseInstructions and input still go through separate native protocol fields.
DIRECT_CONFIG = {
    "project_doc_max_bytes": 0, "mcp_servers": {}, "web_search": "disabled",
    "include_environment_context": False, "include_permissions_instructions": False,
    "include_collaboration_mode_instructions": False, "include_apps_instructions": False,
    "tools.update_plan.enabled": False, "tools.experimental_request_user_input.enabled": False,
    **{"features." + flag: False for flag in (
        "shell_tool", "unified_exec", "apply_patch_freeform", "view_image", "image_generation",
        "apps", "plugins", "memories", "external_agent_memory_import", "multi_agent", "collab",
        "code_mode", "code_mode_host", "js_repl", "tool_search", "skill_search", "browser_use",
        "computer_use", "goals", "sleep_tool", "responses_websockets", "responses_websockets_v2",
        "enable_request_compression", "remote_compaction_v2", "async_compaction", "hooks",
    )},
    "features.skip_host_skill_discovery": True,
    "check_for_update_on_startup": False,
}


def _toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + "=" + _toml(v) for k, v in value.items()) + "}"
    return json.dumps(value, ensure_ascii=False)


class NativeRPC:
    def __init__(self, launcher, cwd):
        args = [launcher, "app-server", "--listen", "stdio://"]
        for key, value in DIRECT_CONFIG.items():
            args += ["-c", key + "=" + _toml(value)]
        env = {k: v for k, v in os.environ.items() if k not in {
            "CODEX_REMOTE_PAYLOAD", "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_TOOL_CALL_ID"}}
        # wsl.exe --exec does not load login-shell PATH. Managed npm Codex
        # launchers use /usr/bin/env node, so include the provisioned runtimes.
        native_paths = [Path.home() / ".local/bin", Path.home() / ".hermes/node/bin"]
        native_paths.extend(sorted((Path.home() / ".nvm/versions/node").glob("*/bin"), reverse=True))
        env["PATH"] = os.pathsep.join([str(path) for path in native_paths if path.is_dir()]
            + [env.get("PATH", os.defpath)])
        self.process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
            start_new_session=True)
        self.incoming = queue.Queue()
        self.events = []
        self.counter = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                self.incoming.put(json.loads(line))
        except (OSError, ValueError):
            pass
        finally:
            self.incoming.put(None)

    def send(self, value):
        self.process.stdin.write(canonical(value) + "\n")
        self.process.stdin.flush()

    def receive(self, timeout):
        message = self.incoming.get(timeout=timeout)
        if message is None:
            raise ExecutionError("interrupted_unknown", "Codex direct process disconnected")
        if "method" in message and "id" in message:
            self.send({"id": message["id"], "error": {"code": -32601, "message": "Direct generation has no host tools"}})
            raise ExecutionError("outbound_contract_mismatch", "Codex direct requested a host operation")
        return message

    def rpc(self, method, params, timeout=30):
        interrupt = method == "turn/interrupt"
        if not interrupt and (getattr(self, "cancelled", threading.Event()).is_set()
                or time.time() >= getattr(self, "deadline", float("inf"))):
            raise ExecutionError("cancelled", "Codex direct request cancelled")
        self.counter += 1
        identifier = self.counter
        self.send({"id": identifier, "method": method, "params": params})
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if not interrupt and (getattr(self, "cancelled", threading.Event()).is_set()
                    or time.time() >= getattr(self, "deadline", float("inf"))):
                raise ExecutionError("cancelled", "Codex direct request cancelled")
            try:
                message = self.receive(min(0.1, max(0.01, until - time.monotonic())))
            except queue.Empty:
                continue
            if message.get("id") == identifier and "method" not in message:
                if "error" in message:
                    raise ExecutionError("native_error", "Codex direct RPC rejected: " + method)
                return message["result"]
            self.events.append(message)
        raise ExecutionError("interrupted_unknown", "Codex direct RPC timed out")

    def close(self):
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=3)
        self.reader.join(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()


def _provider(rpc, spec):
    config = rpc.rpc("config/read", {"includeLayers": False})["config"]
    name = config.get("model_provider") or "openai"
    provider = {k: v for k, v in (config.get("model_providers", {}).get(name) or {}).items() if v is not None}
    if name == "openai":
        account = rpc.rpc("account/read", {"refreshToken": False}).get("account") or {}
        if account.get("type") == "chatgpt":
            base = (config.get("chatgpt_base_url") or "https://chatgpt.com/backend-api").rstrip("/") + "/codex"
        else:
            base = config.get("openai_base_url") or "https://api.openai.com/v1"
        provider.update(requires_openai_auth=True, wire_api="responses")
    else:
        base = provider.get("base_url")
    if not base or provider.get("wire_api", "responses") != "responses" or any(
        provider.get(k) for k in ("aws", "gateway_oauth", "query_params")
    ):
        raise ExecutionError("capability_unavailable", "Native provider has no verified direct HTTP transport")
    if spec.get("subscription"):
        account = rpc.rpc("account/read", {"refreshToken": False}).get("account") or {}
        if account.get("type") != "chatgpt":
            raise ExecutionError("authentication_failed", "Subscription generation requires the pinned ChatGPT account")
    return provider, base.rstrip("/") + "/responses"


def generate(launcher, spec, cancelled, bind=lambda *_: None):
    """One isolated native process per request; no resume, exec fallback or replay."""
    evidence = {"adapter_version": ADAPTER_VERSION, "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "transport": "http_sse", "single_connection": True}
    result = {"status": "failed", "text": "", "actual_model": "", "usage": None, "evidence": evidence}
    rpc = gate = None
    dispatched = False
    try:
        with tempfile.TemporaryDirectory(prefix="mabobot-codex-direct-") as cwd:
            try:
                evidence["stage"] = "initialize"
                if cancelled.is_set() or time.time() >= spec["deadline"]:
                    raise ExecutionError("cancelled", "Direct generation cancelled before dispatch")
                rpc = NativeRPC(launcher, cwd)
                rpc.cancelled, rpc.deadline = cancelled, spec["deadline"]
                initialized = rpc.rpc("initialize", {"clientInfo": {"name": "mabobot_direct", "version": ADAPTER_VERSION}})
                evidence["engine_version"] = initialized.get("userAgent", "unknown")
                rpc.send({"method": "initialized", "params": {}})
                evidence["stage"] = "provider"
                provider, upstream = _provider(rpc, spec)
                gate = OutboundGate(spec, upstream, cancelled,
                    auth_headers=(*provider.get("http_headers", {}), *provider.get("env_http_headers", {})),
                    task_envelope=True)
                provider.update(name="Mabobot verified direct", base_url=gate.base_url,
                    supports_websockets=False, request_max_retries=0, stream_max_retries=0)
                # Keep native authentication, replacing only its inference URL.
                evidence["stage"] = "thread_start"
                started = rpc.rpc("thread/start", {"model": spec["model"], "modelProvider": "mabobot_direct",
                    "cwd": cwd, "ephemeral": True, "baseInstructions": spec["instructions"], "developerInstructions": spec.get("developer_instructions", ""),
                    "approvalPolicy": "never", "sandbox": "read-only", "serviceTier": spec.get("service_tier", "default"),
                    "config": {**DIRECT_CONFIG, "model_providers.mabobot_direct": provider,
                        **({"model_context_window": spec["context_window"]} if spec.get("context_window") else {})}})
                if started.get("model") != spec["model"] or started.get("modelProvider") != "mabobot_direct":
                    raise ExecutionError("model_mismatch", "Codex did not select the pinned direct model/provider")
                session = started["thread"]["id"]
                bind(session, "")
                if cancelled.is_set() or time.time() >= spec["deadline"]:
                    raise ExecutionError("cancelled", "Direct generation cancelled before turn start")
                params = {"threadId": session, "model": spec["model"], "input": [{"type": "text", "text": spec["input"]}]}
                if spec.get("effort") is not None:
                    params["effort"] = spec["effort"]
                if spec.get("schema") is not None:
                    params["outputSchema"] = spec["schema"]
                dispatched = True
                evidence["stage"] = "turn_start"
                turn = rpc.rpc("turn/start", params)["turn"]["id"]
                evidence["stage"] = "generation"
                bind(session, turn)
                interrupted_at = None
                while True:
                    if (cancelled.is_set() or time.time() >= spec["deadline"]) and interrupted_at is None:
                        interrupted_at = time.monotonic()
                        rpc.rpc("turn/interrupt", {"threadId": session, "turnId": turn}, timeout=3)
                    if interrupted_at is not None and time.monotonic() - interrupted_at > 3:
                        raise ExecutionError("interrupted_unknown", "Native cancellation is unconfirmed")
                    if gate.error_code:
                        raise ExecutionError(gate.error_code, "Codex direct HTTP boundary rejected the request")
                    try:
                        event = rpc.events.pop(0) if rpc.events else rpc.receive(0.1)
                    except queue.Empty:
                        continue
                    p = event.get("params", {})
                    if p.get("threadId") != session:
                        continue
                    if event.get("method") == "thread/tokenUsage/updated":
                        last = (p.get("tokenUsage") or {}).get("last") or {}
                        result["usage"] = {"input_tokens": last.get("inputTokens", 0),
                            "output_tokens": last.get("outputTokens", 0), "cached_input_tokens": last.get("cachedInputTokens", 0),
                            "total_tokens": last.get("totalTokens", 0)} if last else None
                    if event.get("method") == "item/completed":
                        item = p.get("item") or {}
                        if item.get("type") == "agentMessage":
                            result["text"] += item.get("text", "")
                        elif item.get("type") not in {"userMessage", "reasoning"}:
                            raise ExecutionError("outbound_contract_mismatch", "Unexpected native operation in direct generation")
                    if event.get("method") == "turn/completed" and p["turn"]["id"] == turn:
                        if gate.error_code:
                            raise ExecutionError(gate.error_code, "Codex direct response was rejected")
                        status = p["turn"]["status"]
                        if not gate.shape["verified"] or gate.shape["forwarded_count"] != 1:
                            raise ExecutionError("outbound_unverified", "No verified native HTTP request was observed")
                        result["status"] = "cancelled" if interrupted_at or status == "interrupted" else "completed" if status == "completed" else "failed"
                        result["actual_model"] = spec["model"]
                        if result["status"] == "failed":
                            result.update(error_code="native_error", error="Codex direct generation failed")
                        break
            finally:
                if rpc:
                    rpc.close()
                if gate:
                    gate.close()
                    evidence["outbound_shape"] = dict(gate.shape)
                    if result["usage"] is None and gate.usage is not None:
                        result["usage"] = gate.usage
                    if gate.actual_model:
                        result["actual_model"] = gate.actual_model
                    if gate.retry_after is not None:
                        result["retry_after_seconds"] = gate.retry_after
    except Exception as exc:
        code = getattr(exc, "code", "interrupted_unknown" if dispatched else "capability_unavailable")
        if gate and gate.error_code:
            code = gate.error_code
        if code == "cancelled" and gate and gate.shape["forwarded_count"]:
            code = "interrupted_unknown"
        evidence["failure_type"] = type(exc).__name__
        shape = evidence.get("outbound_shape", {})
        detail = ",".join(shape.get("violations", []))
        if shape.get("upstream_status"):
            detail = "HTTP " + str(shape["upstream_status"])
        result.update(status="cancelled" if code == "cancelled" else "interrupted_unknown" if code == "interrupted_unknown" else "failed",
                      error_code=code, error=f"Codex direct generation failed: {code} stage={evidence.get('stage', 'startup')} {detail}".strip(), text="")
    return result
