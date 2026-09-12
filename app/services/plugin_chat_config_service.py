"""Shared chat overrides: scoped fields, fresh defaults, and transactional writes."""

import copy
import hashlib
import json
from pathlib import Path
from sqlalchemy.exc import IntegrityError

from app.models.chat_plugin_config import ChatPluginConfig
from app.models.plugin_config_template import PluginConfigTemplate


class PluginChatConfigError(ValueError):
    pass


class PluginTemplateConflict(PluginChatConfigError):
    pass


def chat_fields(config):
    from app.services.capability_service import _is_sensitive

    return {key: field for key, field in (config.get("config_schema") or {}).items()
            if isinstance(field, dict) and field.get("scope") == "global_and_chat"
            and not _is_sensitive(key, field)}


def default_values(config):
    values = {key: copy.deepcopy(field.get("default")) for key, field in chat_fields(config).items()}
    configured = config.get("config") or {}
    for key in values:
        if key in config:
            values[key] = copy.deepcopy(config[key])
        elif key in configured:
            values[key] = copy.deepcopy(configured[key])
    return values


def validate_effective(plugin_name, config, values):
    from app.services.capability_service import validate_settings_patch

    try:
        normalized = validate_settings_patch(config, values)
        if plugin_name == "builtin_translation":
            from app.plugins.builtin_translation.settings import validate_settings

            normalized = validate_settings(normalized)
        return normalized
    except ValueError as exc:
        raise PluginChatConfigError(str(exc)) from exc


class PluginChatConfigService:
    def __init__(self, db, plugin_manager=None):
        self.db = db
        self.plugin_manager = plugin_manager

    def config_for(self, plugin_name):
        plugin = self.plugin_manager.get_plugin_info(plugin_name) if self.plugin_manager else None
        if plugin is None or getattr(plugin, "kind", "plugin") != "plugin":
            raise PluginChatConfigError(f"插件不存在：{plugin_name}")
        path = getattr(plugin, "path", None)
        if path:
            try:
                return json.loads((Path(path) / "config.json").read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                raise PluginChatConfigError("插件默认配置无法读取") from exc
        return copy.deepcopy(getattr(plugin, "config", {}) or {})

    @staticmethod
    def overrides(row):
        if row is None:
            return {}
        if row.schema_version != 1:
            raise PluginChatConfigError("聊天插件配置版本无法识别")
        try:
            values = json.loads(row.overrides_json)
        except (ValueError, TypeError) as exc:
            raise PluginChatConfigError("聊天插件配置损坏") from exc
        if not isinstance(values, dict):
            raise PluginChatConfigError("聊天插件配置必须是对象")
        return values

    def describe(self, user_id, plugin_name, config=None, row=None):
        if config is None:
            config = self.config_for(plugin_name)
        if row is None:
            row = self.db.query(ChatPluginConfig).filter_by(user_id=user_id, plugin_name=plugin_name).first()
        overrides = self.overrides(row)
        fields = chat_fields(config)
        if set(overrides) - set(fields):
            raise PluginChatConfigError("聊天配置包含不允许覆盖的字段")
        defaults = validate_effective(plugin_name, config, default_values(config))
        effective = validate_effective(plugin_name, config, {**defaults, **overrides})
        fingerprint = hashlib.sha256(json.dumps(defaults, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {"schema_version": 1, "overrides": copy.deepcopy(overrides), "effective": effective,
                "defaults": defaults, "sources": {key: "chat" if key in overrides else "global" for key in fields},
                "defaults_revision": fingerprint}

    def describe_all(self, user_id):
        result = {}
        rows = {row.plugin_name: row for row in self.db.query(ChatPluginConfig).filter_by(user_id=user_id).all()}
        plugins = getattr(self.plugin_manager, "plugins", {}) or {}
        for name in set(rows) | set(plugins):
            try:
                config = self.config_for(name)
                if not chat_fields(config) and name not in rows:
                    continue
                result[name] = self.describe(user_id, name, config, rows.get(name))
            except PluginChatConfigError as exc:
                result[name] = {"unavailable": True, "error": str(exc)}
        return result

    def apply(self, user_id, changes):
        for name, patch in changes.items():
            config = self.config_for(name)
            allowed = chat_fields(config)
            if not allowed:
                raise PluginChatConfigError(f"插件不支持聊天配置：{name}")
            fields = set(patch.set) | set(patch.reset_fields)
            if fields - set(allowed):
                raise PluginChatConfigError("包含未知或仅允许全局设置的字段")
            if set(patch.set) & set(patch.reset_fields) or (patch.reset_all and fields):
                raise PluginChatConfigError("同一配置不能同时设置和重置")
            row = self.db.query(ChatPluginConfig).filter_by(user_id=user_id, plugin_name=name).first()
            # reset_all is also the explicit recovery path for corrupted overrides.
            overrides = {} if patch.reset_all else self.overrides(row)
            for key in patch.reset_fields:
                overrides.pop(key, None)
            overrides.update(patch.set)
            effective = validate_effective(name, config, {**default_values(config), **overrides})
            overrides = {key: effective[key] for key in overrides}
            if not overrides:
                if row is not None:
                    self.db.delete(row)
            else:
                if row is None:
                    row = ChatPluginConfig(user_id=user_id, plugin_name=name, schema_version=1)
                    self.db.add(row)
                row.overrides_json = json.dumps(overrides, ensure_ascii=False)

    def validate_defaults(self, plugin_name, config):
        if not chat_fields(config):
            return
        defaults = default_values(config)
        validate_effective(plugin_name, config, defaults)
        for row in self.db.query(ChatPluginConfig).filter_by(plugin_name=plugin_name).all():
            overrides = self.overrides(row)
            if set(overrides) - set(chat_fields(config)):
                raise PluginChatConfigError("已有聊天包含不可用的覆盖字段")
            validate_effective(plugin_name, config, {**defaults, **overrides})


class PluginConfigTemplateService:
    def __init__(self, db, plugin_manager):
        self.db = db
        self.configs = PluginChatConfigService(db, plugin_manager)

    @staticmethod
    def public(row):
        if row.schema_version != 1:
            raise PluginChatConfigError("模板版本无法识别")
        return {"id": row.id, "name": row.name, "plugin_name": row.plugin_name,
                "version": row.version, "schema_version": row.schema_version,
                "values": json.loads(row.config_json)}

    def list(self, plugin_name):
        config = self.configs.config_for(plugin_name)
        if not chat_fields(config):
            raise PluginChatConfigError("此插件不支持聊天模板")
        return [self.public(row) for row in self.db.query(PluginConfigTemplate)
                .filter_by(plugin_name=plugin_name).order_by(PluginConfigTemplate.name).all()]

    def save(self, plugin_name, name, values, template_id=None, expected_version=None):
        config = self.configs.config_for(plugin_name)
        allowed = chat_fields(config)
        if not allowed or set(values) != set(allowed):
            raise PluginChatConfigError("模板必须包含完整的可覆盖配置，不能包含授权或全局专属字段")
        normalized = validate_effective(plugin_name, config, values)
        name = name.strip()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise PluginChatConfigError("模板名称须为 1–80 个有效字符")
        payload = json.dumps(normalized, ensure_ascii=False)
        try:
            if template_id is None:
                row = PluginConfigTemplate(plugin_name=plugin_name, name=name, config_json=payload)
                self.db.add(row)
                self.db.flush()
                template_id = row.id
            else:
                updated = self.db.query(PluginConfigTemplate).filter_by(
                    id=template_id, plugin_name=plugin_name, version=expected_version,
                ).update({"name": name, "config_json": payload, "version": PluginConfigTemplate.version + 1}, synchronize_session=False)
                if updated != 1:
                    raise PluginTemplateConflict("模板已被更新或删除，请重新加载模板列表")
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise PluginChatConfigError("已有同名模板，请更换名称或更新所选模板") from exc
        except Exception:
            self.db.rollback()
            raise
        self.db.expire_all()
        return self.public(self.db.query(PluginConfigTemplate).filter_by(id=template_id).one())

    def delete(self, plugin_name, template_id, expected_version):
        try:
            deleted = self.db.query(PluginConfigTemplate).filter_by(
                id=template_id, plugin_name=plugin_name, version=expected_version,
            ).delete(synchronize_session=False)
            if deleted != 1:
                raise PluginTemplateConflict("模板已被更新或删除，请重新加载模板列表")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise


class ChatConfigFacade:
    """One fresh session and one independent snapshot per plugin invocation."""

    def __init__(self, plugin_name, plugin_path, session_factory=None):
        self.plugin_name = plugin_name
        self.plugin_path = Path(plugin_path)
        self.session_factory = session_factory

    def resolve(self, *, chat_id):
        from app.models.base import SessionLocal
        from app.models.user_permission import WeChatUser

        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            raise PluginChatConfigError("缺少可信聊天 ID")
        config = json.loads((self.plugin_path / "config.json").read_text(encoding="utf-8-sig"))
        with (self.session_factory or SessionLocal)() as db:
            if db.query(WeChatUser.id).filter_by(id=chat_id).first() is None:
                raise PluginChatConfigError("聊天不存在")
            return PluginChatConfigService(db).describe(chat_id, self.plugin_name, config)["effective"]
