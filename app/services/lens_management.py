"""Global, versioned Lens configuration. Each request captures an immutable value."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

KEY = "LENS_RUNTIME_V1"
_lock = threading.RLock()
_last_result = None
_last_error = None


class LensSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    enabled: bool = True
    image_timeout_seconds: int = Field(default=100, ge=10, le=180)
    max_images: int = Field(default=4, ge=1, le=4)
    max_images_per_call: int = Field(default=2, ge=1, le=2)
    max_concurrency: int = Field(default=2, ge=1, le=2)
    cache_ttl_seconds: int = Field(default=3600, ge=0, le=86400)

    @model_validator(mode="after")
    def limits(self):
        if self.max_images_per_call > self.max_images:
            raise ValueError("单次图片数不能超过每轮图片数")
        return self


def defaults():
    try:
        timeout = min(180, max(10, int(os.getenv("CODEX_LENS_IMAGE_TIMEOUT", "100"))))
    except ValueError:
        timeout = 100
    return LensSettings(
        enabled=os.getenv("CODEX_LENS_TOOL_ENABLED", "1").lower().strip()
        not in {"0", "false", "off", "no"},
        image_timeout_seconds=timeout,
    )


def read_settings(db=None):
    if db is None:
        from app.models.base import SessionLocal

        with SessionLocal() as session:
            return read_settings(session)
    from app.models.setting import Setting

    row = db.query(Setting).filter(Setting.key == KEY).first()
    data = row.get_value() if row else {}
    data = data if isinstance(data, dict) else {}
    settings = LensSettings(**{**defaults().model_dump(), **data.get("overrides", {})})
    return {
        "version": int(data.get("version", 0)),
        "settings": settings.model_dump(),
        "defaults": defaults().model_dump(),
        "history": data.get("history", []),
    }


def update_settings(db, settings, expected_version):
    from app.models.setting import Setting

    with _lock:
        current = read_settings(db)
        if current["version"] != expected_version:
            raise ValueError("运行参数已被其他操作更新，请重新打开后再保存")
        values = settings.model_dump() if settings is not None else {}
        data = {
            "version": current["version"] + 1,
            "overrides": values,
            "history": (
                current["history"]
                + [
                    {
                        "version": current["version"],
                        "at": datetime.now(timezone.utc).isoformat(),
                        "settings": current["settings"],
                    }
                ]
            )[-20:],
        }
        row = db.query(Setting).filter(Setting.key == KEY).first()
        serialized = json.dumps(data, ensure_ascii=False)
        if row:
            changed = (
                db.query(Setting)
                .filter(Setting.id == row.id, Setting.value == row.value)
                .update({"value": serialized}, synchronize_session=False)
            )
            if not changed:
                db.rollback()
                raise ValueError("运行参数已变化，请重新打开")
        else:
            db.add(Setting(key=KEY, value=serialized, category="assistant"))
        db.commit()
        db.expire_all()
        return read_settings(db)


def policy_blocks(texts):
    from app.assistant.prompt_composer import block
    from app.assistant.prompt_defaults import DEFAULT_RULES

    return [
        block(
            key,
            DEFAULT_RULES[key]["title"],
            texts[key],
            role="system",
            source="全局系统规则",
            editable="system",
        )
        for key in ("image_search", "video_search")
    ]


def record_result(result):
    """Diagnostics only: no image, URL, purpose or search result content."""
    global _last_result, _last_error
    entry = {
        key: result[key]
        for key in ("status", "failed_stage", "error_type", "cached", "total")
        if key in result
    }
    entry["at"] = datetime.now(timezone.utc).isoformat()
    with _lock:
        _last_result = entry
        if entry.get("status") != "ok":
            _last_error = entry


def runtime_status():
    from app.services.lens.worker import browser_executable

    try:
        driver = version("patchright")
    except PackageNotFoundError:
        driver = None
    executable = browser_executable()
    # Inspection does not launch a browser or issue an external request.
    with _lock:
        return {
            "driver": driver,
            "driver_required": "1.63.0",
            "browser_path": executable,
            "browser_exists": Path(executable).is_file() if executable else None,
            "last_result": _last_result,
            "last_error": _last_error,
            "notice": "状态仅检查当前服务环境；最近结果和错误自本次服务启动起记录。默认浏览器是否可用以实际检索为准。",
        }
