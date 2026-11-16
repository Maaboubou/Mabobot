"""Linux/WSL isolated tools with optional validated public proxy access."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

from app.services.execution_contracts import ExecutionError


class RestrictedExecutor:
    def __init__(self, workspace, *, writable, public_network=False, cancelled=lambda: False,
                 timeout=20, max_workspace_bytes=128 * 1024 * 1024):
        self.workspace = Path(workspace).absolute()
        if any(p.is_symlink() for p in (self.workspace, *self.workspace.parents)):
            raise ExecutionError("unsafe_workspace", "Workspace contains a symbolic link")
        self.writable, self.public_network = writable, public_network
        self.cancelled, self.timeout, self.max_workspace_bytes = cancelled, timeout, max_workspace_bytes

    def _size(self):
        total = 0
        for root, dirs, files in os.walk(self.workspace, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
            for name in files:
                try:
                    total += (Path(root) / name).lstat().st_size
                except FileNotFoundError:
                    pass
                if total > self.max_workspace_bytes:
                    return total
        return total

    def execute(self, argv):
        from contextlib import nullcontext
        network_context = nullcontext()
        # WSL worker creates its own relay; host sockets are never forwarded.
        if self.public_network and os.name == "posix":
            from app.services.workspace_network import WorkspaceNetwork
            network_context = WorkspaceNetwork()
        with network_context as network:
            return self._execute(argv, network)

    def _execute(self, argv, network):
        if os.name == "nt":
            from app.services.opencode_process_bridge import ProcessBridge
            from app.services.opencode_wsl import worker_command
            from app.services.file_tools_runtime import _as_wsl_path
            from concurrent.futures import ThreadPoolExecutor, TimeoutError
            transport = ProcessBridge(worker_command())
            pool = ThreadPoolExecutor(1)
            try:
                future = pool.submit(transport.request, "workspace", {"workspace": _as_wsl_path(self.workspace),
                    "writable": self.writable, "public_network": self.public_network, "argv": argv, "timeout": self.timeout,
                    "max_workspace_bytes": self.max_workspace_bytes}, self.timeout + 15)
                while True:
                    if self.cancelled():
                        raise ExecutionError("cancelled", "Tool was cancelled")
                    try:
                        return future.result(timeout=0.1)
                    except TimeoutError:
                        continue
            finally:
                transport.close()
                pool.shutdown(wait=True)
        if os.name != "posix" or not shutil.which("bwrap"):
            raise ExecutionError("sandbox_unavailable", "The required OS isolation is unavailable")
        if self.cancelled():
            raise ExecutionError("cancelled", "Tool was cancelled")
        if not self.workspace.is_dir() or self._size() > self.max_workspace_bytes:
            raise ExecutionError("workspace_quota", "Workspace is missing or exceeds its quota")
        command = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--bind" if self.writable else "--ro-bind", str(self.workspace), "/workspace",
            "--chdir", "/workspace", "--clearenv", "--setenv", "PATH", "/usr/bin:/bin",
            ]
        if network is not None:
            from app.services.workspace_network import SANDBOX_PROXY_RUNNER
            command += ["--dir", "/run", "--ro-bind", str(network.path), "/run/mabobot-egress.sock"]
            if Path("/etc/ssl/certs").is_dir():
                command += ["--ro-bind", "/etc/ssl/certs", "/etc/ssl/certs"]
            argv = ["/usr/bin/python3", "-I", "-c", SANDBOX_PROXY_RUNNER, *argv]
        command += ["--", *argv]
        # Apply limits in a fresh interpreter, not preexec_fn in a threaded host.
        limiter = "import os,resource,sys;resource.setrlimit(resource.RLIMIT_CPU,(20,20));resource.setrlimit(resource.RLIMIT_FSIZE,(16777216,16777216));resource.setrlimit(resource.RLIMIT_NOFILE,(64,64));resource.setrlimit(resource.RLIMIT_AS,(536870912,536870912));os.execvp(sys.argv[1],sys.argv[1:])"
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.Popen(["/usr/bin/python3", "-I", "-c", limiter, *command],
                stdout=out, stderr=err, stdin=subprocess.DEVNULL, close_fds=True,
                start_new_session=True, env={"PATH": "/usr/bin:/bin"})
            deadline = time.monotonic() + self.timeout
            try:
                while process.poll() is None:
                    if self.cancelled():
                        raise ExecutionError("cancelled", "Tool was cancelled")
                    if time.monotonic() >= deadline:
                        raise ExecutionError("tool_timeout", "Tool exceeded its time limit")
                    if self._size() > self.max_workspace_bytes:
                        raise ExecutionError("workspace_quota", "Workspace quota exceeded")
                    time.sleep(0.05)
                out.seek(0)
                err.seek(0)
                result = {"exit_code": process.returncode, "stdout": out.read(65536).decode("utf-8", "replace"),
                          "stderr": err.read(65536).decode("utf-8", "replace")}
                if process.returncode and ("bwrap:" in result["stderr"] or "Operation not permitted" in result["stderr"]):
                    raise ExecutionError("sandbox_unavailable", "OS isolation could not be established")
                return result
            finally:
                # Descendants must not survive a successful shell either.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
