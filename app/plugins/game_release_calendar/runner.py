"""Auditable calendar jobs using the shared plugin/task model route."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import time
from typing import Callable

PLUGIN_NAME = "game_release_calendar"
CALL_TYPE = "monthly_report"


class CalendarRunner:
    def __init__(self, timeout: int = 900, cancelled: Callable[[], bool] | None = None,
                 manager=None):
        if manager is None:
            from app.services.llm_manager import get_llm_manager
            manager = get_llm_manager()
        self.manager = manager
        self.timeout = timeout
        self.cancelled = cancelled or (lambda: False)
        capabilities = manager.get_call_capabilities(PLUGIN_NAME, CALL_TYPE)
        self.model = capabilities.get("model") or ""
        if not capabilities.get("model_id") or not self.model:
            raise ValueError("请在“插件与任务模型路由”中配置游戏发售月报的主模型")

    def run(self, name: str, prompt: str, schema: dict, directory: Path,
            effort: str = "medium", browse: bool = True) -> dict:
        import jsonschema

        if self.cancelled():
            raise InterruptedError("任务已取消")
        directory = directory.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        if (directory / "job.json").exists():
            archive = directory / "attempts" / str(time.time_ns())
            archive.mkdir(parents=True)
            for filename in ("prompt.txt", "schema.json", "result.json", "job.json"):
                if (directory / filename).exists():
                    shutil.copy2(directory / filename, archive / filename)
        output_path = directory / "result.json"
        output_path.unlink(missing_ok=True)
        (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
        (directory / "schema.json").write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        started = time.time()
        usage = []
        metadata = {"name": name, "model": self.model, "call_type": CALL_TYPE,
                    "effort": effort, "browse": browse, "started_at": started,
                    "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "status": "running"}

        def save():
            (directory / "job.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        save()
        try:
            content = self.manager.call(
                PLUGIN_NAME, CALL_TYPE,
                messages=[{"role": "system", "content": "只返回符合下列 JSON Schema 的 JSON 对象，不要使用 Markdown 代码块。\n"
                           + json.dumps(schema, ensure_ascii=False)},
                          {"role": "user", "content": prompt}],
                tools=[{"google_search": {}}] if browse else None,
                _mabobot_codex_output_schema=schema,
                _mabobot_codex_reasoning_effort=effort,
                _mabobot_codex_web_search_mode="live" if browse else "disabled",
                _mabobot_codex_timeout_seconds=self.timeout,
                _mabobot_disable_model_web_search=not browse,
                _mabobot_usage_capture=usage,
            )
            if self.cancelled():
                raise InterruptedError("任务已取消")
            output_path.write_text(content, encoding="utf-8")
            result = json.loads(content)
            jsonschema.validate(result, schema)
            metadata["status"] = "completed"
            return result
        except Exception as exc:
            metadata.update(status="cancelled" if isinstance(exc, InterruptedError) else "failed", error=str(exc))
            raise
        finally:
            metadata["usage"] = usage
            metadata["elapsed_seconds"] = round(time.time() - started, 2)
            save()
