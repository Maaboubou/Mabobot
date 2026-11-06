#!/usr/bin/env python3
"""Linux/WSL ttyd lifecycle. No model requests, shell input or output are recorded."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_SESSIONS = 4
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
SESSION_RE = re.compile(r"^[a-f0-9]{32}$")


class TerminalError(RuntimeError):
    code = "terminal_error"


class TerminalNotFound(TerminalError):
    code = "not_found"


class TerminalConflict(TerminalError):
    code = "conflict"


class TerminalManager:
    def __init__(self, project_root: Path = PROJECT_ROOT):
        self.project_root = project_root.resolve()
        self.state_dir = self.project_root / "data" / "terminal"
        self.socket_name = "mabobot-web-" + hashlib.sha256(str(self.project_root).encode()).hexdigest()[:16]

    def _tool(self, name):
        bundled = self.project_root / "data" / "terminal-tools" / name
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)
        return shutil.which(name)

    def status(self):
        dependencies = [{"name": name, "available": bool(self._tool(name))} for name in ("ttyd", "tmux")]
        ttyd = dependencies[0]
        if ttyd["available"]:
            try:
                version = subprocess.run([self._tool("ttyd"), "--version"], capture_output=True,
                                         text=True, timeout=5, check=False)
                match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version.stdout + version.stderr)
                ttyd["version"] = match.group(0) if match else "unknown"
                ttyd["available"] = bool(match and version.returncode == 0 and tuple(map(int, match.groups())) >= (1, 7, 0))
            except (OSError, subprocess.SubprocessError):
                ttyd["available"] = False
        available = sys.platform.startswith("linux") and all(item["available"] for item in dependencies)
        if available:
            message = "终端已就绪"
        elif ttyd.get("version") and not ttyd["available"]:
            message = (f"检测到 ttyd {ttyd['version']}，需要 1.7.0 或更新版本；"
                       "可将官方 Linux 发行版二进制放到项目 data/terminal-tools/ttyd 并赋予执行权限")
        else:
            message = ("请在 Codex 所在的 Linux / WSL 环境安装 ttyd（至少 1.7.0）和 tmux；"
                       "可运行 sudo apt install ttyd tmux。若系统仓库版本较旧，"
                       "请将官方 Linux 发行版二进制放到项目 data/terminal-tools/ttyd 并赋予执行权限")
        return {"available": available, "dependencies": dependencies,
                "runtime": "linux", "project_dir": str(self.project_root),
                "message": message}

    @contextmanager
    def _locked(self):
        import fcntl
        for directory in (self.state_dir.parent, self.state_dir):
            if directory.is_symlink():
                raise TerminalError("终端状态目录不能为符号链接")
            directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        lock_path = self.state_dir / ".lock"
        if lock_path.is_symlink():
            raise TerminalError("终端状态锁无效")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _path(self, identifier):
        if not isinstance(identifier, str) or not SESSION_RE.fullmatch(identifier):
            raise TerminalNotFound("终端会话不存在")
        return self.state_dir / (identifier + ".json")

    def _read(self, identifier):
        path = self._path(identifier)
        try:
            if path.is_symlink() or path.stat().st_size > 16000:
                raise ValueError()
            record = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(record, dict) or record.get("id") != identifier
                    or record.get("project_dir") != str(self.project_root)
                    or not isinstance(record.get("profile_id"), str) or not PROFILE_RE.fullmatch(record["profile_id"])
                    or record.get("tmux_session") != "mabobot-" + identifier
                    or record.get("status") not in {"running", "exited"}
                    or type(record.get("created_at")) not in {int, float}
                    or type(record.get("full_access")) is not bool
                    or record.get("url") != f"/api/terminal/sessions/{identifier}/view/"
                    or type(record.get("pid")) is not int or record["pid"] <= 1
                    or not isinstance(record.get("process_birth"), str)
                    or type(record.get("port")) is not int or not 1024 <= record["port"] <= 65535):
                raise ValueError()
            if record["status"] == "running" and (
                    record.get("username") != "mabobot" or not isinstance(record.get("password"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", record["password"])):
                raise ValueError()
            return record
        except (OSError, ValueError, TypeError):
            raise TerminalNotFound("终端会话不存在或状态已损坏") from None

    def _write(self, record):
        path = self._path(record["id"])
        temp = self.state_dir / (record["id"] + ".tmp")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(record, output, ensure_ascii=False)
        os.replace(temp, path)
        os.chmod(path, 0o600)

    @staticmethod
    def _birth(pid):
        try:
            # The command in parentheses may itself contain spaces or ')'.
            rest = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
            return rest[19] if rest[0] != "Z" else ""
        except (OSError, ValueError, IndexError):
            return ""

    @classmethod
    def _owned_process(cls, record):
        birth = cls._birth(record.get("pid", 0))
        return bool(birth and birth == record.get("process_birth"))

    def _tmux(self, *arguments, check=False):
        executable = self._tool("tmux")
        if not executable:
            raise TerminalError("未安装 tmux")
        try:
            result = subprocess.run([executable, "-u", "-f", "/dev/null", "-L", self.socket_name, *arguments],
                                    stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=8, check=False)
        except (OSError, subprocess.SubprocessError):
            raise TerminalError("无法访问终端会话管理器") from None
        if check and result.returncode:
            raise TerminalError("无法启动终端会话，请检查 Profile 启动脚本和项目目录")
        return result

    def _session_alive(self, record):
        return self._tmux("has-session", "-t", "=" + record["tmux_session"]).returncode == 0

    def _stop_process(self, record):
        if not self._owned_process(record):
            return
        pid = int(record["pid"])
        try:
            # ttyd is created as its own process group. Never signal a reused pid.
            if os.getpgid(pid) != pid:
                return
            os.killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and self._owned_process(record):
                time.sleep(0.025)
            if self._owned_process(record):
                os.killpg(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    def _refresh(self, record):
        if record.get("status") == "running" and (
                not self._owned_process(record) or not self._session_alive(record)):
            self._stop_process(record)
            self._tmux("kill-session", "-t", "=" + record["tmux_session"])
            record.update(status="exited", ended_at=time.time())
            record.pop("password", None)
            self._write(record)
        return record

    @staticmethod
    def _public(record):
        return {key: record[key] for key in (
            "id", "profile_id", "status", "created_at", "full_access", "url")}

    def _records(self):
        records = []
        for path in self.state_dir.glob("*.json"):
            try:
                records.append(self._refresh(self._read(path.stem)))
            except TerminalNotFound:
                continue
        records.sort(key=lambda item: item["created_at"], reverse=True)
        # Retain only a bounded, content-free session list.
        for record in records[40:]:
            if record["status"] != "running":
                self._path(record["id"]).unlink(missing_ok=True)
        return records[:40]

    def list_sessions(self):
        with self._locked():
            return {"sessions": [self._public(record) for record in self._records()]}

    def create(self, profile_id, wrapper_path, full_access=True):
        if not isinstance(profile_id, str) or not PROFILE_RE.fullmatch(profile_id):
            raise TerminalError("Profile 名称无效")
        if type(full_access) is not bool:
            raise TerminalError("终端权限设置无效")
        wrapper = Path(wrapper_path)
        if (not wrapper.is_absolute() or wrapper.name != "codex-profile-" + profile_id
                or not wrapper.is_file() or not os.access(wrapper, os.X_OK)):
            raise TerminalError("Profile 启动脚本不可用，请在 Codex 配置中重新检查 Profile")
        if not self.status()["available"]:
            raise TerminalError(self.status()["message"])
        with self._locked():
            records = self._records()
            if sum(record["status"] == "running" for record in records) >= MAX_SESSIONS:
                raise TerminalConflict(f"最多同时运行 {MAX_SESSIONS} 个终端，请先结束旧会话")
            identifier = uuid.uuid4().hex
            name = "mabobot-" + identifier
            arguments = [str(wrapper)]
            if full_access:
                arguments.append("--dangerously-bypass-approvals-and-sandbox")
            self._tmux("new-session", "-d", "-s", name, "-c", str(self.project_root),
                       shlex.join(arguments), check=True)
            # Scrollback stays in tmux memory; no command/output log is created.
            self._tmux("set-option", "-w", "-t", "=" + name, "history-limit", "20000")
            prefix = f"/api/terminal/sessions/{identifier}/view"
            username, password = "mabobot", secrets.token_urlsafe(32)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            record = {"id": identifier, "profile_id": profile_id, "status": "running",
                      "created_at": time.time(), "full_access": full_access,
                      "project_dir": str(self.project_root), "url": prefix + "/",
                      "tmux_session": name, "port": port, "username": username, "password": password}
            process = None
            try:
                process = subprocess.Popen([
                    self._tool("ttyd"), "-i", "127.0.0.1", "-p", str(port), "-W", "-O",
                    "-c", username + ":" + password, "-b", prefix,
                    self._tool("tmux"), "-u", "-f", "/dev/null", "-L", self.socket_name,
                    "attach-session", "-t", "=" + name,
                ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    cwd=self.project_root, start_new_session=True, close_fds=True)
                record.update(pid=process.pid, process_birth=self._birth(process.pid))
                if not record["process_birth"]:
                    raise TerminalError("终端进程启动失败")
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise TerminalError("ttyd 启动失败，请检查版本和运行环境")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    raise TerminalError("等待终端服务启动超时")
                if not self._session_alive(record):
                    raise TerminalError("Codex 已退出，请检查 Profile 配置")
                self._write(record)
                return self._public(record)
            except BaseException:
                self._stop_process(record)
                self._tmux("kill-session", "-t", "=" + name)
                if process is not None:
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                raise

    def stop(self, identifier):
        with self._locked():
            record = self._read(identifier)
            self._stop_process(record)
            self._tmux("kill-session", "-t", "=" + record["tmux_session"])
            record.update(status="exited", ended_at=time.time())
            record.pop("password", None)
            self._write(record)
            return self._public(record)

    def stop_all(self):
        # Each stop revalidates pid birth and exact tmux target under its lock.
        for record in self.list_sessions()["sessions"]:
            if record["status"] == "running":
                self.stop(record["id"])
        return {"status": "stopped"}

    def connection(self, identifier):
        with self._locked():
            record = self._refresh(self._read(identifier))
            if record["status"] != "running":
                raise TerminalConflict("终端会话已经结束")
            return {"base_url": f"http://127.0.0.1:{int(record['port'])}",
                    "base_path": record["url"].rstrip("/"), "username": record["username"],
                    "password": record["password"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=("status", "list", "create", "stop", "connection", "stop-all"), required=True)
    action = parser.parse_args().action
    try:
        if not sys.platform.startswith("linux"):
            raise TerminalError("真实终端需要 Linux / WSL 运行环境")
        raw = sys.stdin.read(20000)
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            raise TerminalError("终端请求格式无效")
        manager = TerminalManager()
        if action == "status":
            data = manager.status()
        elif action == "list":
            data = manager.list_sessions()
        elif action == "create":
            data = manager.create(payload.get("profile_id"), payload.get("wrapper_path", ""), payload.get("full_access", True))
        elif action == "stop":
            data = manager.stop(payload.get("id"))
        elif action == "connection":
            data = manager.connection(payload.get("id"))
        else:
            data = manager.stop_all()
        print(json.dumps({"status": "success", "data": data}, ensure_ascii=False))
        return 0
    except TerminalError as exc:
        print(json.dumps({"status": "error", "code": exc.code, "error": str(exc)}, ensure_ascii=False))
    except Exception:
        # No raw provider/process errors: they may contain environment values.
        print(json.dumps({"status": "error", "code": "terminal_error", "error": "终端操作失败，请检查运行环境"}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
