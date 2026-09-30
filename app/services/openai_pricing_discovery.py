"""Discover unknown Codex model prices without blocking a user request.

Codex researches official documentation; this module validates and stores only
structured USD token rates. Failed lookups retain an unknown price.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from urllib.parse import urlsplit


logger = logging.getLogger(__name__)
DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "openai_pricing_discovery.sqlite3"
MODEL_ID = re.compile(r"^gpt-[a-zA-Z0-9][a-zA-Z0-9.-]{0,78}$")
RATE_FIELDS = ("input", "output", "cached_input", "cache_write")
RETRY_SECONDS = 6 * 60 * 60
IN_FLIGHT_SECONDS = 10 * 60
_LOOKUP_SLOT = threading.BoundedSemaphore(1)

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["model", "source_url", "currency", "input", "output", "cached_input",
                 "cache_write", "long_context_threshold", "long_input", "long_output",
                 "long_cached_input", "long_cache_write"],
    "properties": {
        key: {"type": "integer"} if key == "long_context_threshold" else {"type": "string"}
        for key in ("model", "source_url", "currency", "input", "output", "cached_input",
                    "cache_write", "long_context_threshold", "long_input", "long_output",
                    "long_cached_input", "long_cache_write")
    },
}


def _rate(value: object, *, required: bool = True) -> str | None:
    if value in ("", None) and not required:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d{1,8})?", value):
        raise ValueError("Invalid token rate")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid token rate") from exc
    if amount < 0 or amount > 100_000 or (required and amount == 0):
        raise ValueError("Token rate is outside the allowed range")
    return str(amount)


def validate_quote(model: str, raw: object) -> dict:
    """Reject mismatched models, non-official sources, and incomplete prices."""
    if not MODEL_ID.fullmatch(model) or not isinstance(raw, dict) or raw.get("model") != model:
        raise ValueError("Model ID mismatch")
    if raw.get("currency") != "USD":
        raise ValueError("Only USD prices are supported")
    source_url = str(raw.get("source_url") or "")
    parsed = urlsplit(source_url)
    if (parsed.scheme != "https" or parsed.hostname not in {"developers.openai.com", "platform.openai.com"}
            or not (parsed.path.startswith("/api/docs/models/") or parsed.path in {"/api/docs/pricing", "/pricing"})):
        raise ValueError("Price source must be official OpenAI documentation")
    if parsed.path.startswith("/api/docs/models/") and parsed.path.removeprefix("/api/docs/models/").strip("/") != model:
        raise ValueError("Model documentation does not match the requested model")
    standard = {key: _rate(raw.get(key), required=key != "cache_write") for key in RATE_FIELDS}
    threshold = raw.get("long_context_threshold")
    if type(threshold) is not int or threshold < 0 or threshold > 2_000_000:
        raise ValueError("Invalid context threshold")
    long = {}
    if threshold:
        long = {key: _rate(raw.get("long_" + key), required=key != "cache_write") for key in RATE_FIELDS}
        if any(Decimal(long[key]) < Decimal(standard[key]) for key in RATE_FIELDS
               if long[key] is not None and standard[key] is not None):
            raise ValueError("Long-context rates are lower than standard rates")
    elif any(raw.get("long_" + key) not in ("", None) for key in RATE_FIELDS):
        raise ValueError("Unexpected long-context rates")
    return {"model": model, "currency": "USD", "source_url": source_url,
            "standard": standard, "long_context_threshold": threshold, "long_context": long,
            "checked_at": datetime.now(timezone.utc).isoformat()}


@contextmanager
def _connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=0.2)
    try:
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS discovered_openai_price (
                model TEXT PRIMARY KEY, quote_json TEXT, retry_after REAL NOT NULL)""")
            yield db
    finally:
        db.close()


def get_quote(model: str, *, path: Path = DEFAULT_DB) -> dict | None:
    if not MODEL_ID.fullmatch(str(model or "")) or not path.exists():
        return None
    try:
        with _connect(path) as db:
            row = db.execute("SELECT quote_json FROM discovered_openai_price WHERE model=?", (model,)).fetchone()
        if row and row[0]:
            return _decode_stored_quote(model, row[0])
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError):
        logger.warning("Could not read discovered OpenAI pricing for %s", model, exc_info=True)
    return None


def _decode_stored_quote(model: str, encoded: str) -> dict:
    stored = json.loads(encoded)
    validated = validate_quote(model, {**stored["standard"],
        **{"long_" + key: stored["long_context"].get(key, "") for key in RATE_FIELDS},
        "model": model, "currency": stored["currency"], "source_url": stored["source_url"],
        "long_context_threshold": stored["long_context_threshold"]})
    validated["checked_at"] = stored["checked_at"]
    return validated


def _reserve(model: str, path: Path, now: float) -> bool:
    with _connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT quote_json, retry_after FROM discovered_openai_price WHERE model=?", (model,)).fetchone()
        if row:
            if row[0]:
                try:
                    _decode_stored_quote(model, row[0])
                    return False
                except (KeyError, TypeError, ValueError):
                    pass
            elif row[1] > now:
                return False
        db.execute("INSERT OR REPLACE INTO discovered_openai_price VALUES (?, NULL, ?)",
                   (model, now + IN_FLIGHT_SECONDS))
    return True


def _save(model: str, path: Path, quote: dict | None) -> None:
    with _connect(path) as db:
        db.execute("INSERT OR REPLACE INTO discovered_openai_price VALUES (?, ?, ?)",
                   (model, json.dumps(quote, ensure_ascii=False) if quote else None,
                    0 if quote else time.time() + RETRY_SECONDS))


def _query_with_codex(model: str) -> dict:
    from app.services.codex_profile_service import get_codex_profile_service
    from app.services.codex_proxy.client import CodexCliClient

    listing = get_codex_profile_service().list_profiles()
    profiles = listing.get("profiles") or []
    default_id = listing.get("default_profile_id")
    profile = next((item for item in profiles if item.get("name") == default_id
                    and item.get("available")), None)
    if profile is None:
        profile = next((item for item in profiles if item.get("available") and item.get("auth_type") == "chatgpt"), None)
    if profile is None:
        profile = next((item for item in profiles if item.get("available")), None)
    if profile is None:
        raise RuntimeError("No available Codex Profile for pricing discovery")
    with tempfile.TemporaryDirectory(prefix="mabobot_price_") as workdir:
        client = CodexCliClient(codex_bin=profile["wrapper_path"], workdir=workdir,
                                codex_home=profile.get("codex_home"), timeout_seconds=180)
        response = asyncio.run(client.chat({
            "model": profile["model"], "reasoning_effort": "medium", "timeout": 180,
            "codex_runtime_profile": profile["name"], "codex_config_policy": "inherit",
            "codex_sandbox": "read-only", "codex_approval_policy": "never",
            "codex_web_search": "live", "codex_text_only": True,
            "codex_workdir": workdir, "codex_artifact_root": str(Path(workdir) / "artifacts"),
            "output_schema": OUTPUT_SCHEMA,
            "messages": [{"role": "user", "content": (
                f"Find the current USD API token prices for the exact OpenAI model `{model}`. "
                "Use only official developers.openai.com or platform.openai.com model/pricing pages. "
                "Return prices in USD per 1 million tokens. Include standard input, output, "
                "cached input, cache writes; use an empty string for cache writes only if the "
                "official page says the dimension does not apply. Set long_context_threshold "
                "to the input-token count above which the whole request uses long rates, or 0 "
                "when the official page explicitly gives flat rates. If tiered, include all four "
                "long rates; otherwise use empty strings for long rates. If any required price, "
                "threshold, or exact model identity cannot be verified, do not guess: return "
                "empty strings so validation fails. Return only the schema fields and a direct "
                "official source URL. Do not edit files or launch other agents."
            )}],
        }))
    content = response["choices"][0]["message"]["content"]
    return json.loads(content)


def schedule_unknown_model(model: str, *, path: Path = DEFAULT_DB, lookup=None) -> bool:
    """Launch at most one lookup per process; retry failures after six hours."""
    model = str(model or "").split("/")[-1]
    if not MODEL_ID.fullmatch(model) or not _LOOKUP_SLOT.acquire(blocking=False):
        return False
    try:
        if not _reserve(model, path, time.time()):
            _LOOKUP_SLOT.release()
            return False
    except (OSError, sqlite3.Error):
        _LOOKUP_SLOT.release()
        logger.warning("Could not schedule OpenAI pricing discovery for %s", model, exc_info=True)
        return False

    def run():
        try:
            raw = (lookup or _query_with_codex)(model)
            quote = validate_quote(model, raw)
            _save(model, path, quote)
            logger.info("Discovered official OpenAI pricing for %s from %s", model, quote["source_url"])
        except Exception:
            logger.warning("OpenAI pricing discovery failed for %s", model, exc_info=True)
            try:
                _save(model, path, None)
            except (OSError, sqlite3.Error):
                logger.exception("Could not save OpenAI pricing discovery retry time")
        finally:
            _LOOKUP_SLOT.release()

    threading.Thread(target=run, name=f"openai-price-{model}", daemon=True).start()
    return True
