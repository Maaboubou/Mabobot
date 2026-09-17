"""Strict administrator-owned permissions; derived fields cannot be submitted."""

from typing import Literal

from pydantic import BaseModel, Field, StrictBool, model_validator


class PermissionValues(BaseModel):
    model_config = {"extra": "forbid"}
    workspace_access: Literal["read_only", "read_write"] = "read_write"
    public_network: StrictBool = True
    online_research: StrictBool = True
    boundary_action: Literal["deny", "auto_review"] = "auto_review"


class PermissionSet(BaseModel):
    model_config = {"extra": "forbid"}
    workspace_access: Literal["read_only", "read_write"] | None = None
    public_network: StrictBool | None = None
    online_research: StrictBool | None = None
    boundary_action: Literal["deny", "auto_review"] | None = None

    @model_validator(mode="after")
    def reject_null(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("权限值不能为空，请使用 reset_fields 恢复默认")
        return self


class ChatPermissionSet(PermissionSet):
    access_scope: Literal["isolated", "owner_full"] | None = None


class PermissionDefaultsPatch(BaseModel):
    model_config = {"extra": "forbid"}
    schema_version: Literal[1] = 1
    expected_version: int = Field(ge=1, strict=True)
    set: PermissionSet = Field(default_factory=PermissionSet)


class ChatPermissionPatch(BaseModel):
    model_config = {"extra": "forbid"}
    schema_version: Literal[1] = 1
    settings_mode: Literal["inherit", "custom"] | None = None
    set: ChatPermissionSet = Field(default_factory=ChatPermissionSet)
    reset_fields: list[Literal["workspace_access", "public_network", "online_research", "boundary_action", "access_scope"]] = Field(default_factory=list)
    reset_all: StrictBool = False
    # Compatibility input for old clients; translated once into the new policy.
    mode: Literal["isolated", "owner_full"] | None = None

    @model_validator(mode="after")
    def consistent_operation(self):
        fields = self.set.model_fields_set
        if fields.intersection(self.reset_fields):
            raise ValueError("同一权限不能同时设置和恢复默认")
        if (self.reset_all or self.settings_mode == "inherit") and (fields or self.mode is not None):
            raise ValueError("跟随默认不能同时设置聊天覆盖")
        if self.mode is not None and "access_scope" in fields:
            raise ValueError("访问范围不能重复设置")
        return self
