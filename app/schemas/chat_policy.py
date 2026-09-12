"""Request contracts for atomic chat policy updates."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ChatSettingsPatch(BaseModel):
    is_group: Optional[bool] = None
    listening_enabled: Optional[bool] = None
    sender_blacklist: Optional[List[str]] = Field(default=None, max_length=200)
    bot_group_nickname: Optional[str] = Field(default=None, max_length=128)
    bot_group_nickname_auto_enabled: Optional[bool] = None


class AssistantSettingsPatch(BaseModel):
    enabled: Optional[bool] = None
    proactive_enabled: Optional[bool] = None
    followup_enabled: Optional[bool] = None
    followup_window_seconds: Optional[int] = Field(default=None, ge=10, le=600)
    followup_merge_seconds: Optional[int] = Field(default=None, ge=1, le=30)
    followup_max_turns: Optional[int] = Field(default=None, ge=1, le=10)
    ignored_senders: Optional[List[str]] = Field(default=None, max_length=200)
    role_id: Optional[int] = Field(default=None, ge=1)
    judge_id: Optional[int] = Field(default=None, ge=1)
    codex_profile_id: Optional[str] = Field(default=None, max_length=48)


class CodexSettingsPatch(BaseModel):
    mode: Optional[Literal["isolated", "owner_full"]] = None


class PluginGrantPatch(BaseModel):
    plugin_name: str = Field(min_length=1, max_length=160)
    require_mention: bool = False


class PluginConfigPatch(BaseModel):
    model_config = {"extra": "forbid"}
    set: Dict[str, Any] = Field(default_factory=dict, max_length=100)
    reset_fields: List[str] = Field(default_factory=list, max_length=100)
    reset_all: bool = False


class ChatPolicyPatch(BaseModel):
    expected_version: int = Field(ge=1)
    chat: Optional[ChatSettingsPatch] = None
    assistant: Optional[AssistantSettingsPatch] = None
    codex: Optional[CodexSettingsPatch] = None
    # Omitted means preserve. An explicit empty list removes every plugin grant.
    plugin_grants: Optional[List[PluginGrantPatch]] = Field(default=None, max_length=200)
    plugin_configs: Optional[Dict[str, PluginConfigPatch]] = Field(default=None, max_length=200)
