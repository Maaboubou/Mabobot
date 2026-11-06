"""Chat-owned image versions and immutable snapshots, independent of providers.

Callers authorize source files before registering them. Only this module's
chat-scoped snapshots can be resolved by asset ID; a path is never an asset ID.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.RLock()
_session_factory = None
_MAX_IMAGE_BYTES = 50 * 1024 * 1024
_STATUSES = {"unknown", "pending", "sending", "sent", "failed", "blocked", "uncertain", "cancelled", "interrupted"}
_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".svg"}


def _session():
    from app.models.base import SessionLocal
    return (_session_factory or SessionLocal)()


def _chat_hash(chat):
    return hashlib.sha256(chat.encode()).hexdigest()


def _key(chat):
    return "IMAGE_GENERATION_ASSET_SECRET_" + _chat_hash(chat)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _checked_path(raw, *, missing=False):
    """Check before resolving so parent links, including 3.11 junctions, fail."""
    path = Path(raw).absolute()
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise ValueError("图片文件已不存在") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("图片路径包含链接，无法读取")
    return path.resolve()


def _root(chat, *, create=False):
    root = _checked_path(os.getenv("IMAGE_GENERATION_ASSET_ROOT", "data/image_generation/assets"), missing=True)
    folder = root / _chat_hash(chat)
    _checked_path(folder, missing=True)
    if create:
        folder.mkdir(parents=True, exist_ok=True)
        _checked_path(folder)
    return folder


def _digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _snapshot(chat, raw, *, expected_hash=None):
    source = _checked_path(raw)
    source_info = source.stat()
    if not stat.S_ISREG(source_info.st_mode):
        raise ValueError("图片不是普通文件")
    if source_info.st_size > _MAX_IMAGE_BYTES:
        raise ValueError("图片超过 50 MiB 资产上限")
    folder = _root(chat, create=True)
    temporary = None
    try:
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as original, tempfile.NamedTemporaryFile(dir=folder, delete=False) as target:
            temporary = Path(target.name)
            for chunk in iter(lambda: original.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
                if size > _MAX_IMAGE_BYTES:
                    raise ValueError("图片超过 50 MiB 资产上限")
                target.write(chunk)
        fingerprint = digest.hexdigest()
        if expected_hash is not None and fingerprint != expected_hash:
            raise ValueError("之前登记的图片已变化，不能迁移为可信资产")
        suffix = source.suffix.lower() if source.suffix.lower() in _SUFFIXES else ""
        destination = folder / (fingerprint + suffix)
        _checked_path(destination, missing=True)
        if destination.exists():
            if not destination.is_file() or _digest(destination) != fingerprint:
                raise ValueError("图片资产快照已变化")
        else:
            os.replace(temporary, destination)
        return str(destination), str(source), fingerprint, size
    except OSError as exc:
        raise ValueError("无法保存图片资产快照") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _media_info(path):
    from PIL import Image
    if Path(path).suffix.lower() == ".svg":
        from app.services.image_generation_media import inspect_svg
        width, height = inspect_svg(Path(path).read_bytes())
        return {"width": width, "height": height, "format": "svg", "has_alpha": None}
    try:
        with Image.open(path) as picture:
            if picture.width * picture.height > 40_000_000:
                raise ValueError("图片像素数超过资产上限")
            # Edits use a GIF's first frame. Record actual transparency for that
            # reference frame; an all-opaque RGBA image is not transparent.
            picture.load()
            alpha = (picture.getchannel("A") if "A" in picture.getbands() else
                     picture.convert("RGBA").getchannel("A") if "transparency" in picture.info else None)
            result = {"width": picture.width, "height": picture.height,
                      "format": str(picture.format or "").lower(),
                      "has_alpha": alpha is not None and alpha.getextrema()[0] < 255}
            if getattr(picture, "is_animated", False):
                result["frame_count"] = picture.n_frames
            return result
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError("图片资产不是有效图片") from exc


def _source_ids(parents=(), explicit=(), singular=None):
    """Provenance only: historical attachment access still belongs to history."""
    identities = [*explicit, *([singular] if singular else [])]
    if not identities:
        for parent in parents:
            identities.extend(parent.get("source_attachment_ids") or
                              ([parent["source_attachment_id"]] if parent.get("source_attachment_id") else []))
    if any(not isinstance(identity, str) or not 1 <= len(identity) <= 160 for identity in identities):
        raise ValueError("原图来源 ID 无效")
    return list(dict.fromkeys(identities))


def _record(chat, snapshot, *, request_id="", source="api", parent_asset_ids=(), parameters=None,
            role="intermediate", source_attachment_id=None, source_attachment_ids=(),
            version=1, batch_id="", batch_index=1):
    path, origin_path, digest, size = snapshot
    sources = _source_ids(explicit=source_attachment_ids, singular=source_attachment_id)
    return {"asset_id": "img_" + uuid.uuid4().hex, "chat_hash": _chat_hash(chat),
            "path": path, "origin_path": origin_path, "origin_paths": [origin_path], "sha256": digest, "size": size,
            "created_at": _now(), "updated_at": _now(), "request_id": str(request_id), "source": str(source),
            "parent_asset_ids": list(dict.fromkeys(parent_asset_ids)), "version": version,
            "parameters": copy.deepcopy(parameters or {}), "role": role,
            "source_attachment_id": sources[0] if sources else None, "source_attachment_ids": sources,
            "delivery_status": "pending" if role == "final" else "unknown",
            "delivered_at": None,
            "batch_id": batch_id, "batch_index": batch_index, **_media_info(path)}


def _read(db, chat):
    from app.models.setting import Setting
    row = db.query(Setting).filter_by(key=_key(chat)).first()
    try:
        values = json.loads(row.value) if row else []
    except (ValueError, TypeError) as exc:
        raise ValueError("图片资产索引损坏") from exc
    if not isinstance(values, list):
        raise ValueError("图片资产索引格式无效")
    changed = False
    for index, value in enumerate(values):
        if not isinstance(value, dict) or value.get("asset_id"):
            continue
        # Legacy records are trusted only while their original hash agrees.
        # Unreadable legacy records stay stored but are not exposed as assets.
        if not value.get("path") or not value.get("sha256"):
            continue
        try:
            snapshot = _snapshot(chat, value["path"], expected_hash=value["sha256"])
            migrated = _record(chat, snapshot, source="legacy_api", batch_id="legacy")
        except (ValueError, OSError):
            continue
        migrated["created_at"] = value.get("created_at") or migrated["created_at"]
        migrated["legacy"] = True
        values[index] = migrated
        changed = True
    return row, values, changed


def _save(db, chat, row, values):
    from app.models.setting import Setting
    if row is None:
        row = Setting(key=_key(chat), category="image_generation")
        db.add(row)
    row.value = json.dumps(values, ensure_ascii=False)
    db.commit()


def _owned(values, chat):
    return [v for v in values if isinstance(v, dict) and v.get("asset_id") and v.get("chat_hash") == _chat_hash(chat)]


def _validate_snapshot(chat, value):
    try:
        digest = value["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("图片资产摘要无效")
        suffix = Path(value["path"]).suffix.lower()
        # Old installation paths are provenance only. Restored assets resolve
        # exclusively inside the current installation's chat-owned root.
        path = _checked_path(_root(chat) / (digest + (suffix if suffix in _SUFFIXES else "")))
        if not path.is_file():
            raise ValueError("图片资产不属于当前聊天的存储目录")
        if path.stat().st_size > _MAX_IMAGE_BYTES or _digest(path) != digest:
            raise ValueError("图片资产快照已变化，无法作为原图使用")
        value["path"] = str(path)
    except (KeyError, TypeError, OSError) as exc:
        raise ValueError("图片资产快照无法读取") from exc
    return value


def register_images(chat, paths, *, request_id="", source="api", parent_asset_ids=(), parameters=None,
                    role="intermediate", source_attachment_id=None, source_attachment_ids=()):
    """Persist authorized images once per request, source path and content hash."""
    if not chat or not paths:
        return []
    parents = list(dict.fromkeys(parent_asset_ids))
    with _lock, _session() as db:
        row, values, _ = _read(db, chat)
        owned = _owned(values, chat)
        by_id = {v["asset_id"]: v for v in owned}
        if any(identity not in by_id for identity in parents):
            raise ValueError("父图片资产不属于当前聊天")
        results = []
        batch_id = "batch_" + uuid.uuid4().hex
        for index, raw in enumerate(paths, 1):
            snapshot = _snapshot(chat, raw)
            _, origin, digest, _ = snapshot
            existing = next((v for v in owned if v["request_id"] == str(request_id)
                             and origin in v.get("origin_paths", [v["origin_path"]]) and v["sha256"] == digest), None)
            if existing:
                _validate_snapshot(chat, existing)
                results.append(existing)
                continue
            related = parents or [v["asset_id"] for v in owned if v["origin_path"] == origin][:1]
            version = 1 + max((by_id[p]["version"] for p in related), default=0)
            value = _record(chat, snapshot, request_id=request_id, source=source, parent_asset_ids=related,
                            parameters=parameters, role=role,
                            source_attachment_ids=_source_ids([by_id[p] for p in related], source_attachment_ids,
                                                              source_attachment_id),
                            version=version, batch_id=batch_id, batch_index=index)
            values.insert(0, value)
            owned.insert(0, value)
            by_id[value["asset_id"]] = value
            results.append(value)
        _save(db, chat, row, values)
        return copy.deepcopy(results)


def resolve_asset(chat, asset_id):
    """Resolve only a current-chat ID and verify the retained immutable bytes."""
    if not chat or not isinstance(asset_id, str) or not asset_id or len(asset_id) > 160:
        raise ValueError("图片资产 ID 无效")
    with _lock, _session() as db:
        row, values, changed = _read(db, chat)
        if changed:
            _save(db, chat, row, values)
        value = next((v for v in _owned(values, chat) if v["asset_id"] == asset_id), None)
        if value is None:
            raise ValueError("当前聊天没有这张图片资产")
        return copy.deepcopy(_validate_snapshot(chat, value))


def list_assets(chat, *, limit=32, offset=0, include_intermediate=False, request_id=None):
    """Page known assets; interrupted results remain available for recovery."""
    if not chat:
        return []
    if isinstance(limit, bool) or isinstance(offset, bool) or int(limit) < 1 or int(offset) < 0:
        raise ValueError("图片列表分页参数无效")
    with _lock, _session() as db:
        row, values, changed = _read(db, chat)
        if changed:
            _save(db, chat, row, values)
        result = [v for v in _owned(values, chat)
                  if (include_intermediate or v["role"] == "final" or v.get("legacy") or v["delivery_status"] == "interrupted")
                  and (request_id is None or v["request_id"] == str(request_id))]
        # An export or metadata update is not a send. Old images become recent
        # again only once their new delivery is confirmed.
        result.sort(key=lambda value: value.get("delivered_at") or value["created_at"], reverse=True)
        return copy.deepcopy(result[int(offset):int(offset) + int(limit)])


def mark_delivery(chat, asset_ids, status):
    if status not in _STATUSES:
        raise ValueError("图片交付状态无效")
    identities = set(asset_ids)
    if not identities:
        return
    with _lock, _session() as db:
        row, values, _ = _read(db, chat)
        selected = [v for v in _owned(values, chat) if v["asset_id"] in identities]
        if len(selected) != len(identities):
            raise ValueError("图片资产不属于当前聊天")
        now = _now()
        for value in selected:
            value.update(delivery_status=status, updated_at=now)
            if status == "sent":
                value["delivered_at"] = now
        _save(db, chat, row, values)


def bind_asset_path(chat, asset_id, path):
    """Associate a caller-authorized staging copy without creating a version."""
    with _lock, _session() as db:
        row, values, _ = _read(db, chat)
        value = next((v for v in _owned(values, chat) if v["asset_id"] == asset_id), None)
        if value is None:
            raise ValueError("当前聊天没有这张图片资产")
        _validate_snapshot(chat, value)
        staged = _checked_path(path)
        if (not staged.is_file() or staged.stat().st_size > _MAX_IMAGE_BYTES
                or _digest(staged) != value["sha256"]):
            raise ValueError("导出副本与原图片资产不一致")
        value["origin_paths"] = list(dict.fromkeys([*value.get("origin_paths", [value["origin_path"]]), str(staged)]))
        value["updated_at"] = _now()
        _save(db, chat, row, values)
        return copy.deepcopy(value)


def finalize_images(chat, paths, *, request_id="", source="bot"):
    """Snapshot selected final outputs, retaining generation identity and lineage."""
    if not chat or not paths:
        return []
    with _lock, _session() as db:
        row, values, _ = _read(db, chat)
        owned = _owned(values, chat)
        finalized_ids = {v["asset_id"] for v in owned if v["role"] == "final" and v.get("batch_id")}
        # Fix this set before adding finals, so sibling outputs are not parents.
        request_assets = [v for v in owned if request_id and v["request_id"] == str(request_id)]
        results = []
        for raw in paths:
            snapshot = _snapshot(chat, raw)
            _, origin, digest, _ = snapshot
            candidates = [v for v in owned if v["sha256"] == digest]
            value = next((v for v in candidates
                          if origin in {*v.get("origin_paths", [v["origin_path"]]), v["path"]}), None)
            if value is None and request_id:
                value = next((v for v in candidates if v["request_id"] == str(request_id)), None)
            if value is None:
                parents = [v for v in owned if origin in v.get("origin_paths", [v["origin_path"]])][:1] or request_assets
                value = _record(chat, snapshot, request_id=request_id, source=source,
                                parent_asset_ids=[v["asset_id"] for v in parents],
                                source_attachment_ids=_source_ids(parents),
                                parameters=parents[0].get("parameters") if len(parents) == 1 else None,
                                version=1 + max((v["version"] for v in parents), default=0), role="final")
                value["parameters"].update(size=f'{value["width"]}x{value["height"]}', output_format=value["format"])
                values.insert(0, value)
                owned.insert(0, value)
            else:
                _validate_snapshot(chat, value)
                if value["role"] != "final":
                    value["delivery_status"] = "pending"
                value.update(role="final", updated_at=_now(), origin_path=origin,
                             origin_paths=list(dict.fromkeys([*value.get("origin_paths", [value["origin_path"]]), origin])))
            results.append(value)
        batch_id = "batch_" + hashlib.sha256("|".join(v["asset_id"] for v in results).encode()).hexdigest()[:32]
        delivery_batch_id = "delivery_" + hashlib.sha256(
            (str(request_id) + "|" + "|".join(v["asset_id"] for v in results)).encode()).hexdigest()[:32]
        for index, value in enumerate(results, 1):
            if value["asset_id"] not in finalized_ids:
                value.update(batch_id=batch_id, batch_index=index)
            value.update(last_delivery_batch_id=delivery_batch_id, last_delivery_batch_index=index,
                         last_delivery_request_id=str(request_id))
        _save(db, chat, row, values)
        return copy.deepcopy(results)


def remember_images(chat, paths):
    """Compatibility entry point; new integrations should retain returned IDs."""
    return register_images(chat, paths)


def recent_images(chat):
    """Legacy path interface; new tools must use list_assets and resolve_asset.

    Keep existing origin-path and hash checks for older sessions. When that file
    is removed, the retained snapshot still provides an authorized reference.
    """
    results = []
    for value in list_assets(chat, include_intermediate=True):
        if Path(value["path"]).suffix.lower() == ".svg":
            continue
        try:
            _validate_snapshot(chat, value)
            origin = _checked_path(value["origin_path"])
            if origin.is_file():
                value["path"] = str(origin)
        except (ValueError, OSError):
            try:
                _validate_snapshot(chat, value)
            except (ValueError, OSError):
                continue
        results.append(value)
    return results
