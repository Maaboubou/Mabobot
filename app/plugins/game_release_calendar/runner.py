"""Bounded, auditable Codex CLI jobs. No implicit model fallback."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import shutil
import subprocess
import time
from typing import Callable


class CodexRunner:
    model = "gpt-5.6-sol"

    def __init__(self, timeout: int = 900, cancelled: Callable[[], bool] | None = None):
        self.timeout = timeout
        self.cancelled = cancelled or (lambda: False)

    def run(self, name: str, prompt: str, schema: dict, directory: Path,
            effort: str = "medium", browse: bool = True) -> dict:
        import jsonschema
        from app.services.file_tools_runtime import (
            build_codex_runtime_command, get_codex_bin_selection, _as_wsl_path,
        )
        directory = directory.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        if (directory / "job.json").exists():
            archive = directory / "attempts" / str(time.time_ns())
            archive.mkdir(parents=True)
            for filename in ("prompt.txt", "schema.json", "result.json", "events.jsonl", "stderr.log", "job.json"):
                if (directory / filename).exists():
                    shutil.copy2(directory / filename, archive / filename)
        prompt_path, schema_path = directory / "prompt.txt", directory / "schema.json"
        output_path = directory / "result.json"
        output_path.unlink(missing_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        use_wsl = os.name == "nt" and os.getenv("CODEX_PROXY_USE_WSL", "true").lower() not in {"0", "false", "off"}
        selection = get_codex_bin_selection(use_wsl=use_wsl)
        executable = selection.get("configured") or "codex"
        path = lambda p: _as_wsl_path(p) if use_wsl else str(p)
        args = [executable, "exec", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                "-C", path(directory), "-s", "read-only", "-m", self.model,
                "-c", f'model_reasoning_effort="{effort}"',
                "-c", 'web_search="live"' if browse else 'web_search="disabled"',
                "--json", "--output-schema", path(schema_path), "-o", path(output_path), "-"]
        command = build_codex_runtime_command(args, use_wsl=use_wsl)
        started = time.time()
        metadata = {"name": name, "model": self.model, "effort": effort, "browse": browse,
                    "started_at": started, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    "status": "running"}
        def save():
            (directory / "job.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        save()
        manager = None
        request_id = "release-calendar-" + hashlib.sha256(str(directory).encode()).hexdigest()[:20]
        try:
            from app.services.codex_job_manager import codex_job_manager
            manager = codex_job_manager
            manager.register(request_id, {"plugin": "game_release_calendar", "task": name,
                                         "model": self.model, "reasoning_effort": effort})
        except Exception as exc:
            manager = None
            metadata["registry_warning"] = str(exc)
            logging.getLogger(__name__).warning("Codex calendar job registry unavailable: %s", type(exc).__name__)
        try:
            with prompt_path.open("r", encoding="utf-8") as inp, \
                 (directory / "events.jsonl").open("w", encoding="utf-8") as out, \
                 (directory / "stderr.log").open("w", encoding="utf-8") as err:
                proc = subprocess.Popen(command, stdin=inp, stdout=out, stderr=err,
                                        start_new_session=os.name != "nt",
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                try:
                    while proc.poll() is None:
                        if self.cancelled():
                            raise InterruptedError("任务已取消")
                        if time.time() - started > self.timeout:
                            raise TimeoutError(f"子 Codex {name} 超过 {self.timeout} 秒")
                        time.sleep(0.25)
                    if proc.returncode != 0:
                        raise RuntimeError(f"子 Codex {name} 退出码 {proc.returncode}；见 {directory / 'stderr.log'}")
                finally:
                    if proc.poll() is None:
                        if os.name == "nt":
                            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
                        else:
                            os.killpg(proc.pid, signal.SIGTERM)
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            if os.name != "nt":
                                os.killpg(proc.pid, signal.SIGKILL)
                            else:
                                proc.kill()
                            proc.wait()
            result = json.loads(output_path.read_text(encoding="utf-8"))
            jsonschema.validate(result, schema)
            events = []
            for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
            searches = [e for e in events if e.get("item", {}).get("type") in {"web_search", "web_search_call"}]
            metadata["web_search_events"] = len(searches)
            for event in events:
                if event.get("type") == "turn.completed":
                    metadata["usage"] = event.get("usage", {})
            metadata["status"] = "completed"
            return result
        except Exception as exc:
            metadata.update(status="cancelled" if isinstance(exc, InterruptedError) else "failed", error=str(exc))
            raise
        finally:
            metadata["elapsed_seconds"] = round(time.time() - started, 2)
            save()
            if manager:
                try:
                    manager.finish(request_id, status=metadata["status"], elapsed_seconds=metadata["elapsed_seconds"])
                except Exception as exc:
                    logging.getLogger(__name__).warning("Codex calendar registry completion failed: %s", type(exc).__name__)
