"""Private, multiplexed stdio transport to a Linux worker (no TCP forwarding)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import queue
import subprocess
import threading

from app.services.execution_contracts import ExecutionError, canonical
from app.utils.subprocess_utils import apply_hidden_process_defaults

MAX_FRAME = 8 * 1024 * 1024


class ProcessBridge:
    def __init__(self, command, *, callback=lambda *_: None, env=None):
        self.callback = callback
        self._lock, self._write = threading.RLock(), threading.Lock()
        self._close_lock, self._closed = threading.Lock(), False
        self._pending, self._counter = {}, 0
        self._callbacks = ThreadPoolExecutor(8, thread_name_prefix="opencode-host-tools")
        self.process = subprocess.Popen(command, **apply_hidden_process_defaults(dict(
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1, env=env)))
        self.reader = threading.Thread(target=self._read, name="opencode-wsl-bridge", daemon=True)
        self.reader.start()

    def send(self, value):
        encoded = canonical(value)
        if len(encoded.encode()) > MAX_FRAME:
            raise ExecutionError("input_limit", "OpenCode bridge frame exceeds its limit")
        with self._write:
            if self.process.poll() is not None:
                raise ExecutionError("interrupted_unknown", "OpenCode Linux worker disconnected")
            try:
                self.process.stdin.write(encoded + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise ExecutionError("interrupted_unknown", "OpenCode Linux worker disconnected") from exc

    def request(self, method, params=None, timeout=40):
        with self._lock:
            self._counter += 1
            identifier = self._counter
            target = self._pending[identifier] = queue.Queue()
        try:
            self.send({"id": identifier, "method": method, "params": params or {}})
            result = target.get(timeout=timeout)
            if "error" in result:
                raise ExecutionError(result["error"].get("code", "native_error"), result["error"]["message"])
            return result["result"]
        except queue.Empty as exc:
            raise ExecutionError("interrupted_unknown", "OpenCode Linux worker confirmation timed out") from exc
        finally:
            with self._lock:
                self._pending.pop(identifier, None)

    def _callback(self, message):
        try:
            result = self.callback(message["event"], message.get("params", {}))
            if "callback_id" in message:
                self.send({"callback_id": message["callback_id"], "result": result})
        except Exception:
            if "callback_id" in message:
                try:
                    self.send({"callback_id": message["callback_id"], "error": "Host tool denied or disconnected"})
                except Exception:
                    pass

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_FRAME + 1)
                if not line or len(line.encode()) > MAX_FRAME:
                    break
                message = json.loads(line)
                if "event" in message:
                    # The reader must remain free for cancellation/responses while
                    # a tool waits on a nested model request.
                    if message["event"] == "update":
                        self._callback(message)
                    else:
                        self._callbacks.submit(self._callback, message)
                else:
                    with self._lock:
                        target = self._pending.get(message.get("id"))
                    if target:
                        target.put(message)
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                for target in self._pending.values():
                    target.put({"error": {"code": "interrupted_unknown", "message": "OpenCode Linux worker disconnected"}})

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            try:
                self._close()
            finally:
                self._closed = True

    def _close(self):
        # EOF is the Linux supervisor's shutdown signal. It terminates native
        # processes using its own process-group/PID-fd handles before exiting.
        with self._write:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.reader.join(timeout=2)
        self.process.stdout.close()
        self._callbacks.shutdown(wait=False, cancel_futures=True)
