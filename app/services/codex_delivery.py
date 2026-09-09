"""Validated final-file selection, independent of the user's wording.

The model selects deliverables in its existing final answer. Native image paths
are authorized only by successful events from this request, never by model text.
"""

from __future__ import annotations

import json
import shutil
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.services.codex_proxy.client import (
    _as_runtime_path,
    _collect_artifact_attachments,
    _materialize_codex_generated_image,
)


DELIVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "_mabobot_reply": {"type": "string"},
        "_mabobot_files": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["_mabobot_reply", "_mabobot_files"],
    "additionalProperties": False,
}

DELIVERY_INSTRUCTIONS = """
Final delivery protocol (overrides earlier attachment/copy instructions):
- Return one JSON object with _mabobot_reply (string) and _mabobot_files (array of strings).
- Put the complete normal reply in _mabobot_reply. If the persona requires JSON, serialize that JSON as this string. Do not expose the envelope or file paths in the normal reply.
- List only FINAL deliverables in _mabobot_files, in delivery order. Use [] for text-only replies or when generation failed. Exclude reference images, intermediate assets and discarded versions, including images embedded in a final document.
- For native imagegen outputs, list the exact successful tool result savedPath. The host copies and validates these files; do NOT use shell commands to copy, locate or verify native imagegen files outside your workspace.
- For other files (including documents, spreadsheets, presentations, audio and video), create the real final file in the designated outputs directory and list its path relative to that directory.
- A listed file must already exist. Do not claim success for a failed generation. Do not claim WeChat delivery; the host sends files after validating this reply.
- No extra tool call or model turn is needed to submit this list.
"""


def delivery_schema(reply_schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keep the caller's typed reply contract inside the delivery envelope."""
    schema = deepcopy(DELIVERY_SCHEMA)
    if reply_schema is not None:
        nested = deepcopy(reply_schema)

        def rebase_refs(value: Any) -> None:
            # Fragment references originally pointed at the reply document's
            # root. Its root now lives under this property in the wire schema.
            if isinstance(value, dict):
                ref = value.get('$ref')
                if isinstance(ref, str) and (ref == '#' or ref.startswith('#/')):
                    value['$ref'] = '#/properties/_mabobot_reply' + ref[1:]
                for child in value.values():
                    rebase_refs(child)
            elif isinstance(value, list):
                for child in value:
                    rebase_refs(child)

        rebase_refs(nested)
        schema['properties']['_mabobot_reply'] = nested
    return schema


def delivery_instructions(*, reply_is_json: bool = False) -> str:
    if not reply_is_json:
        return DELIVERY_INSTRUCTIONS
    return DELIVERY_INSTRUCTIONS.replace(
        '_mabobot_reply (string)', '_mabobot_reply (the value required by the original reply schema)',
    ).replace(
        'Put the complete normal reply in _mabobot_reply. If the persona requires JSON, serialize that JSON as this string.',
        'Put the original structured reply directly in _mabobot_reply, preserving its status, messages and all schema constraints. Do not encode it as a JSON string.',
    )


class DeliveryError(ValueError):
    pass


def parse_delivery(text: str, *, reply_is_json: bool = False) -> tuple[str, list[str] | None]:
    """None preserves legacy providers; an explicit empty list means send none."""
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        if "_mabobot_reply" in text or "_mabobot_files" in text:
            raise DeliveryError("Malformed final attachment envelope")
        return text, None
    if not isinstance(value, dict) or not ({"_mabobot_reply", "_mabobot_files"} & value.keys()):
        return text, None
    reply, files = value.get("_mabobot_reply"), value.get("_mabobot_files")
    if ('_mabobot_reply' not in value or (not reply_is_json and not isinstance(reply, str))
            or not isinstance(files, list)
            or any(not isinstance(path, str) or not path.strip() for path in files)):
        raise DeliveryError("Invalid final attachment envelope")
    if reply_is_json:
        reply = json.dumps(reply, ensure_ascii=False, separators=(',', ':'))
    return reply, list(dict.fromkeys(files))


def collect_selected_files(
    paths: list[str], *, generated_paths: list[str], output_dir: Path, use_wsl: bool,
) -> list[dict[str, Any]]:
    """Resolve only current output files or exact successful native event paths."""
    if not paths:
        return []
    existing = _collect_artifact_attachments(output_dir)
    by_path = {}
    for attachment in existing:
        path = Path(attachment["path"])
        by_path[str(path)] = attachment
        by_path[_as_runtime_path(path, use_wsl)] = attachment
        by_path[path.relative_to(output_dir.resolve()).as_posix()] = attachment
    # Private staging avoids overwriting a document or a model-created image.
    staging = output_dir / ("delivery-" + uuid.uuid4().hex)
    resolved = []
    try:
        for index, path in enumerate(paths, 1):
            attachment = by_path.get(path)
            if attachment is None and path in generated_paths:
                staging.mkdir(exist_ok=True)
                recovered = _materialize_codex_generated_image(
                    path, output_dir=staging, index=index, use_wsl=use_wsl,
                )
                if recovered is not None:
                    resolved.append(str(recovered.resolve()))
                    continue
            if attachment is None:
                raise DeliveryError("Selected attachment is missing, invalid, or outside this request")
            resolved.append(attachment)
        # Hash each imported file once, even for multi-image deliveries.
        imported = {a['path']: a for a in _collect_artifact_attachments(staging)} if staging.exists() else {}
        selected = []
        seen_hashes = set()
        for value in resolved:
            attachment = imported.get(value) if isinstance(value, str) else value
            if attachment is None:
                raise DeliveryError("Imported attachment could not be validated")
            if attachment["sha256"] not in seen_hashes:
                selected.append(attachment)
                seen_hashes.add(attachment["sha256"])
        return selected
    except Exception:
        if staging.is_dir():
            shutil.rmtree(staging)
        raise
