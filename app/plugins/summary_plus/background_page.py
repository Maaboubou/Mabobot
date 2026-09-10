"""CDP access to one owned background tab in the existing interactive Chrome."""

import json
import logging
import threading
import time
from urllib.parse import urlparse

import requests
import websocket


class BackgroundPage:
    def __init__(self, debug_port, url, *, command_timeout=8):
        self._socket = None
        self._target = None
        self._session = None
        self._next_id = 0
        self._lock = threading.RLock()
        self._timeout = float(command_timeout)
        try:
            response = requests.get(f"http://127.0.0.1:{int(debug_port)}/json/version", timeout=2)
            response.raise_for_status()
            endpoint = response.json().get("webSocketDebuggerUrl", "")
            parsed = urlparse(endpoint)
            if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise RuntimeError("Chrome 未提供本机 CDP 接口")
            self._socket = websocket.create_connection(endpoint, timeout=self._timeout, suppress_origin=True)
            self._target = self._call("Target.createTarget", {
                "url": "about:blank", "background": True,
            })["targetId"]
            self._session = self._call("Target.attachToTarget", {
                "targetId": self._target, "flatten": True,
            })["sessionId"]
            self._call("Page.enable", {}, page=True)
            # Allow scroll/lazy-render callbacks in the hidden tab without activating
            # its Chrome window or changing the user's selected tab.
            self._call("Emulation.setFocusEmulationEnabled", {"enabled": True}, page=True)
            self.get(url)
        except Exception:
            self.close()
            raise

    def _call(self, method, params, *, page=False):
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            payload = {"id": request_id, "method": method, "params": params}
            if page:
                payload["sessionId"] = self._session
            deadline = time.monotonic() + self._timeout
            self._socket.send(json.dumps(payload))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"后台 Chrome 命令超时: {method}")
                self._socket.settimeout(remaining)
                response = json.loads(self._socket.recv())
                if response.get("id") != request_id:
                    continue  # Browser/page events do not own this response.
                if "error" in response:
                    raise RuntimeError(f"后台 Chrome 命令失败: {method}: {response['error'].get('message', '')}")
                return response.get("result", {})

    def execute_script(self, script):
        result = self._call("Runtime.evaluate", {
            "expression": "(function(){" + script + "})()",
            "returnByValue": True, "awaitPromise": True,
        }, page=True)
        if result.get("exceptionDetails"):
            raise RuntimeError("后台页面脚本执行失败")
        return result.get("result", {}).get("value")

    @property
    def current_url(self):
        return self.execute_script("return location.href") or ""

    @property
    def title(self):
        return self.execute_script("return document.title") or ""

    def get(self, url):
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("后台摘要需要 HTTP URL")
        result = self._call("Page.navigate", {"url": url}, page=True)
        if result.get("errorText"):
            raise RuntimeError(f"后台页面打开失败: {result['errorText']}")

    def refresh(self):
        self._call("Page.reload", {}, page=True)

    def close(self):
        if self._socket is None:
            return
        try:
            if self._target is not None:
                self._call("Target.closeTarget", {"targetId": self._target})
        except Exception:
            logging.getLogger(__name__).warning("后台摘要标签页关闭失败", exc_info=True)
        finally:
            self._target = None
            socket = self._socket
            self._socket = None
            socket.close()
