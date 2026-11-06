"""Small, redacted image-call receipts stored alongside the console configuration."""
import json
import logging
import threading
import uuid
from datetime import datetime, timezone

KEY = "IMAGE_GENERATION_HISTORY"
_lock = threading.RLock()


def read_history(db):
    from app.models.setting import Setting
    row = db.query(Setting).filter_by(key=KEY).first()
    try:
        value = json.loads(row.value) if row else []
        return value if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


class ImageReceipt:
    def __init__(self, actor="", *, default_mode="api", selected_by="default"):
        self.value = {"id": uuid.uuid4().hex, "at": datetime.now(timezone.utc).isoformat(),
                      "chat": actor, "status": "running", "attempts": [],
                      "mode": "api", "default_mode": default_mode, "selected_by": selected_by}

    def progress(self, attempt):
        # Only fields produced by the host are persisted. No upstream response,
        # custom parameters, request headers, prompts or image data are stored.
        values = {key: attempt[key] for key in (
            "channel_id", "channel", "model", "protocol", "operation", "parameters", "output_sizes", "outputs",
            "status", "images", "elapsed", "error", "error_type", "outcome_unknown",
            "provider_error_code", "provider_error_type", "provider_error_param", "provider_request_id"
        ) if key in attempt}
        previous = next((i for i, a in enumerate(self.value["attempts"])
                         if a["channel_id"] == values["channel_id"]), None)
        if previous is None:
            self.value["attempts"].append(values)
        else:
            self.value["attempts"][previous] = values
        self._write()

    def finish(self, status):
        self.value["status"] = status
        self._write()

    def _write(self):
        from app.models.base import SessionLocal
        from app.models.setting import Setting
        try:
            with _lock, SessionLocal() as db:
                row = db.query(Setting).filter_by(key=KEY).first()
                history = [v for v in read_history(db) if v.get("id") != self.value["id"]]
                history = [self.value, *history][:100]
                if row is None:
                    row = Setting(key=KEY, category="image_generation")
                    db.add(row)
                row.value = json.dumps(history, ensure_ascii=False)
                db.commit()
        except Exception:
            logging.getLogger(__name__).warning("Could not save image API receipt")
