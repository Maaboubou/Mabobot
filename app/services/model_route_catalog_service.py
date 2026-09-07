"""Display-safe catalog of components that own auxiliary model routes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from app.services.capability_service import (
    CATEGORY_META,
    CORE_ASSISTANT_CONFIG_PATH,
    CORE_ASSISTANT_ID,
    normalize_llm_task_descriptors,
)


_SAFE_ICON_PATTERN = re.compile(r"^bi-[a-z0-9-]+$")


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _readable_owner_name(owner_id: str) -> str:
    words = [part for part in re.split(r"[_-]+", owner_id) if part]
    return " ".join(word[:1].upper() + word[1:] for word in words) or owner_id


class ModelRouteCatalogService:
    """Build the route-owner catalog without treating core services as plugins."""

    def __init__(self, plugin_manager: Optional[Any]):
        self.plugin_manager = plugin_manager

    @staticmethod
    def _owner_item(
        owner_id: str,
        owner_kind: str,
        config: Mapping[str, Any],
        component: Optional[Any] = None,
    ) -> Dict[str, Any]:
        raw_ui = config.get("ui") if isinstance(config.get("ui"), Mapping) else {}
        category = str(config.get("category") or "utility")
        category_meta = CATEGORY_META.get(
            category,
            {"label": "其他", "icon": "bi-grid", "order": 500},
        )
        raw_icon = str(raw_ui.get("icon") or category_meta["icon"])
        icon = raw_icon if _SAFE_ICON_PATTERN.fullmatch(raw_icon) else "bi-diagram-3"
        component_name = str(getattr(component, "name", "") or "")
        component_description = str(getattr(component, "description", "") or "")
        tasks = normalize_llm_task_descriptors(config)
        return {
            "id": owner_id,
            "owner_kind": owner_kind,
            "display_name": str(
                config.get("display_name")
                or component_name
                or _readable_owner_name(owner_id)
            )[:120],
            "description": str(
                component_description or config.get("description") or ""
            )[:500],
            "icon": icon,
            "category": category,
            "category_label": str(category_meta["label"]),
            "category_order": int(category_meta["order"]),
            "featured": owner_kind == "core",
            "tasks": {key: task for key, task in tasks.items() if task.get("routable") is not False},
            "usage_tasks": tasks,
        }

    @staticmethod
    def _unknown_owner(owner_id: str) -> Dict[str, Any]:
        return {
            "id": owner_id,
            "owner_kind": "unknown",
            "display_name": _readable_owner_name(owner_id)[:120],
            "description": "尚未找到此路由主体的声明信息。",
            "icon": "bi-diagram-3",
            "category": "unknown",
            "category_label": "历史路由",
            "category_order": 900,
            "featured": False,
            "tasks": {},
        }

    def list_owners(self, mapped_owner_ids: Iterable[str] = ()) -> List[Dict[str, Any]]:
        mapped = {
            str(owner_id or "").strip()
            for owner_id in mapped_owner_ids
            if str(owner_id or "").strip()
        }
        owners: Dict[str, Dict[str, Any]] = {}

        assistant_config = _read_json(CORE_ASSISTANT_CONFIG_PATH)
        if assistant_config:
            assistant = self._owner_item(
                CORE_ASSISTANT_ID,
                "core",
                assistant_config,
            )
            if assistant["tasks"] or CORE_ASSISTANT_ID in mapped:
                owners[CORE_ASSISTANT_ID] = assistant

        plugins = getattr(self.plugin_manager, "plugins", {}) or {}
        for plugin_id, plugin in plugins.items():
            owner_id = str(plugin_id or "").strip()
            if not owner_id or getattr(plugin, "kind", "plugin") != "plugin":
                continue
            config = plugin.config if isinstance(getattr(plugin, "config", None), dict) else {}
            owner = self._owner_item(owner_id, "plugin", config, plugin)
            if owner["tasks"] or owner_id in mapped:
                owners[owner_id] = owner

        for owner_id in mapped:
            owners.setdefault(owner_id, self._unknown_owner(owner_id))

        return sorted(
            owners.values(),
            key=lambda owner: (
                0 if owner["owner_kind"] == "core" else 1,
                owner["category_order"],
                owner["display_name"].casefold(),
                owner["id"],
            ),
        )
