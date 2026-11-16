"""Offline, versioned engine configuration and immutable candidate resolution."""
from __future__ import annotations

import copy
from dataclasses import asdict
import json
import re
import time

from app.services.execution_contracts import ExecutionError, RouteCandidate, RouteSnapshot, canonical, digest

DEFAULT_CONFIG = {"default_engine": "codex", "defaults": {"codex": "", "opencode": ""},
                  "chats": {}, "profiles": {}, "routes": {}, "enabled": False}
MODEL_PARAMETERS = ("reasoning_effort", "temperature", "max_tokens", "response_format")


class EngineConfigService:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS engine_config_versions (
                    revision INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_json TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS engine_capabilities (
                    binding_key TEXT NOT NULL, capability TEXT NOT NULL,
                    status TEXT NOT NULL, evidence_json TEXT NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(binding_key, capability)
                );
            """)

    def read(self, revision=None):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM engine_config_versions " +
                ("WHERE revision=?" if revision else "ORDER BY revision DESC LIMIT 1"),
                (revision,) if revision else ()).fetchone()
        if row:
            return row["revision"], json.loads(row["config_json"])
        if revision:
            raise KeyError(revision)
        return 0, copy.deepcopy(DEFAULT_CONFIG)

    def save(self, config, *, expected_revision):
        self.validate(config)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            revision = db.execute("SELECT COALESCE(MAX(revision),0) FROM engine_config_versions").fetchone()[0]
            if revision != expected_revision:
                raise ExecutionError("configuration_conflict", "Configuration changed; refresh before saving")
            result = db.execute("INSERT INTO engine_config_versions(config_json,created) VALUES(?,?)", (canonical(config), time.time()))
            return result.lastrowid

    @staticmethod
    def validate(config):
        if not isinstance(config, dict) or set(config) != set(DEFAULT_CONFIG):
            raise ValueError("Invalid engine configuration fields")
        if config["default_engine"] not in {"codex", "opencode"} or not isinstance(config["enabled"], bool):
            raise ValueError("Invalid default engine")
        for profile_id, profile in config["profiles"].items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", profile_id):
                raise ValueError("Invalid Profile ID")
            if profile.get("engine") not in {"codex", "opencode"}:
                raise ValueError("Invalid Profile engine")
            if profile.get("engine") == "codex" and profile.get("native_profile_id", profile_id) != profile_id:
                raise ValueError("Codex Profile IDs must preserve the existing native ID")
            allowed = {"engine", "generation", "account_key", "auth_ref", "model", "reasoning_effort",
                       "auth_binding_generation", "retiring", "revoked", "native_profile_id", "channel", "runtime_options", "connection"}
            if set(profile) - allowed or not all(profile.get(k) for k in ("generation", "account_key", "auth_ref", "model", "auth_binding_generation")):
                raise ValueError("Incomplete Profile or unsupported configuration field")
            if profile.get("connection"):
                from app.services.opencode_profile_config import validate_connection
                if profile["engine"] != "opencode":
                    raise ValueError("Custom connection belongs to OpenCode")
                validate_connection(profile["connection"])
        for engine, profile_id in config["defaults"].items():
            if engine not in {"codex", "opencode"}:
                raise ValueError("Invalid engine default")
            if profile_id and config["profiles"].get(profile_id, {}).get("engine") != engine:
                raise ValueError("Default Profile does not belong to the selected engine")
        for chat in config["chats"].values():
            if chat.get("engine", "inherit") not in {"inherit", "codex", "opencode"}:
                raise ValueError("Invalid chat engine")
            for engine, profile_id in chat.get("profiles", {}).items():
                if profile_id and config["profiles"].get(profile_id, {}).get("engine") != engine:
                    raise ValueError("Chat Profile does not belong to selected engine")
        for route in config["routes"].values():
            if route.get("mode") not in {"legacy", "direct", "tools"}:
                raise ValueError("Invalid task processing mode")
            if route.get("engine", "follow") not in {"follow", "codex", "opencode"}:
                raise ValueError("Invalid route engine policy")

    def record_capability(self, candidate, capability, status, evidence):
        # Called by a host verification probe, never from a browser-supplied bool.
        if status not in {"listed", "verified", "not_verified", "unsupported"}:
            raise ValueError("Unknown capability status")
        if status == "verified" and not evidence.get("adapter_version"):
            raise ValueError("Verified capability requires adapter evidence")
        key = self.capability_key(candidate)
        with self.store.connect() as db:
            db.execute("INSERT INTO engine_capabilities VALUES(?,?,?,?,?) ON CONFLICT(binding_key,capability) DO UPDATE SET status=excluded.status,evidence_json=excluded.evidence_json,updated=excluded.updated",
                (key, capability, status, canonical(evidence), time.time()))

    @staticmethod
    def capability_key(candidate):
        return digest([candidate.engine, candidate.profile_id, candidate.generation, candidate.auth_ref,
                       candidate.model, candidate.parameters_json, candidate.channel])

    def capability(self, candidate, name):
        with self.store.connect() as db:
            row = db.execute("SELECT status,evidence_json FROM engine_capabilities WHERE binding_key=? AND capability=?", (self.capability_key(candidate), name)).fetchone()
        return {"status": row[0], "evidence": json.loads(row[1])} if row else {"status": "not_verified", "evidence": {}}

    def resolve(self, *, chat_id, component, task_type, models, mapping,
                tools=(), output_contract=None, image_policy=None, role_signature="", revision=None,
                engine_override="follow", profile_override=""):
        revision, config = self.read(revision)
        route = config["routes"].get(component + "." + task_type, {})
        mode = route.get("mode", mapping.get("mode", "legacy"))
        chat = config["chats"].get(str(chat_id), {})
        candidates = []
        fallback = mapping.get("fallbacks") or mapping.get("fallback") or []
        if isinstance(fallback, str):
            fallback = [fallback]
        card_ids = [mapping.get("primary"), *fallback]
        for card_id in dict.fromkeys(card_ids):
            card = models.get(card_id)
            if not card:
                raise ExecutionError("model_missing", f"Model card is missing: {card_id}")
            provider = card.get("provider") or card.get("custom_llm_provider") or ""
            if card.get("kind") == "subscription":
                engine = next((value for value in (engine_override, route.get("engine"), card.get("engine_policy"),
                              chat.get("engine"), config["default_engine"])
                              if value and value not in {"follow", "inherit"}), "codex")
            elif provider in {"local_codex", "local_codex_cli"} or "/api/codex/" in str(card.get("api_base", "")):
                engine = "codex"
            else:
                engine = "api"
            params = {k: copy.deepcopy(card[k]) for k in MODEL_PARAMETERS if k in card}
            if engine == "api":
                # Keep endpoint/key in the versioned model config, never the route snapshot.
                candidate = RouteCandidate(str(card_id), engine, "", digest(card), str(card["model"]),
                    str(card.get("account_key") or "api:" + str(card_id)), "model:" + str(card_id),
                    canonical(params), str(provider))
            else:
                profile_id = (profile_override or route.get("profiles", {}).get(engine) or
                    (card.get("codex_profile_id") if card.get("kind") != "subscription" else "") or
                    chat.get("profiles", {}).get(engine) or config["defaults"].get(engine))
                profile = config["profiles"].get(profile_id)
                if not profile or profile.get("engine") != engine or profile.get("retiring") or profile.get("revoked"):
                    raise ExecutionError("profile_unavailable", f"No available {engine} Profile for this route")
                runtime_options = profile.get("runtime_options", {})
                if engine == "opencode":
                    from app.services.opencode_profile_config import snapshot_options
                    runtime_options = snapshot_options(profile)
                if card.get('kind') == 'subscription' and (
                        card.get('execution_policy') == 'profile_generation'
                        or card.get('custom_llm_provider') == 'chatgpt_subscription'):
                    if profile.get('connection', {}).get('auth_type') == 'api_key':
                        raise ExecutionError('profile_unavailable', 'ChatGPT 订阅模型需要登录型 Profile，当前选中了 API Key Profile')
                    if card.get('execution_policy') == 'profile_generation':
                        runtime_options = {**runtime_options, 'subscription_model': True}
                candidate = RouteCandidate(str(card_id), engine, profile_id, str(profile["generation"]),
                    str(card["model"]), profile["account_key"], profile["auth_ref"], canonical(params),
                    str(profile.get("channel", "")), profile_config_json=canonical(runtime_options))
            candidates.append(candidate)
        return RouteSnapshot(revision=str(revision), config_version=str(revision), mode=mode,
            candidates=tuple(candidates), max_attempts=min(int(route.get("max_attempts", len(candidates))), len(candidates)),
            tools=tuple(tools) if mode == "tools" else (), output_contract_json=canonical(output_contract or {}),
            image_policy_json=canonical(image_policy or {}), role_signature=role_signature)

    def check_candidate(self, candidate, mode):
        from app.services.engine_deployment import require_engine
        require_engine(candidate.engine)
        _, config = self.read()
        if candidate.engine != "api":
            profile = config["profiles"].get(candidate.profile_id)
            if not profile or profile.get("revoked"):
                raise ExecutionError("authorization_revoked", "Profile authorization was revoked")
            # Normal edits/retirement do not replace the snapshot's account.
            if profile["auth_ref"] != candidate.auth_ref:
                raise ExecutionError("account_changed", "Original account binding is no longer available")
        capability = "strict_generation" if mode == "direct" else "tool_calling"
        if mode == 'direct' and json.loads(candidate.profile_config_json).get('subscription_model'):
            # Pinned adapters validate the selected model and outbound contract
            # on the actual request. A probe for another model/effort is not its authority.
            return
        if mode != "legacy" and self.capability(candidate, capability)["status"] != "verified":
            raise ExecutionError("capability_unavailable", f"{candidate.engine} {capability} is not verified for this Profile/model", retryable=True)

    def conversion_preview(self, model_id, models, mappings):
        card = models[model_id]
        references = []
        for component, routes in mappings.items():
            for name, mapping in routes.items():
                fallback = mapping.get("fallbacks") or mapping.get("fallback") or []
                if isinstance(fallback, str):
                    fallback = [fallback]
                if model_id in [mapping.get("primary"), *fallback]:
                    references.append({"component": component, "task": name, "route": copy.deepcopy(mapping)})
        return {"model_id": model_id, "model": card.get("model"), "parameters": {k: card[k] for k in MODEL_PARAMETERS if k in card},
                "references": references, "preserve_id": True, "old_provider": card.get("provider"),
                "changes": ["显式转换后可跟随任务引擎", "直接生成要求对应账号能力验证", "图片未知结果先核对，发送失败不重画"]}
