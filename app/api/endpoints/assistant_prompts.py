"""LAN administrator prompt inspection. No preview endpoint invokes a model."""

import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.assistant.context_manager import ChatContextManager
from app.assistant.prompt_composer import (
    block,
    compose_decision,
    compose_reply,
    composition_version,
    messages_from_blocks,
)
from app.assistant.prompt_defaults import DEFAULT_RULES
from app.models.base import get_db
from app.models.chatbot_judge import ChatBotJudge
from app.models.chatbot_role import ChatBotRole
from app.models.user_permission import WeChatUser
from app.services.assistant_prompt_service import (
    get_snapshot,
    list_snapshots,
    read_rules,
    update_rules,
)

router = APIRouter()


class RuleUpdate(BaseModel):
    version: int = Field(ge=0)
    changes: dict[str, str | None]


class PreviewRequest(BaseModel):
    scene: Literal["reply", "decision"] = "reply"
    role_id: int | None = None
    judge_id: int | None = None
    chat_id: int | None = None
    # Drafts are validated using the same CRUD models below.
    draft: dict[str, Any] = Field(default_factory=dict)
    rule_draft: dict[str, str] = Field(default_factory=dict)
    history: str = Field(default="", max_length=60000)
    content: str = Field(default="你好，聊聊最近的游戏？", max_length=20000)
    sender: str = Field(default="测试用户", max_length=160)
    use_chat_history: bool = False
    elapsed_minutes: float = Field(default=10, ge=0, le=100000)
    message_count: int = Field(default=5, ge=0, le=100000)
    ignore_timing: bool = False
    in_cooldown: bool = False
    cooldown_elapsed_minutes: float = Field(default=10, ge=0, le=100000)
    cooldown_message_count: int = Field(default=5, ge=0, le=100000)


def chat(db, chat_id):
    row = db.get(WeChatUser, chat_id)
    if not row:
        raise HTTPException(404, "聊天不存在")
    return row


def row_data(db, cls, identity):
    if identity is None:
        return {}
    row = db.get(cls, identity)
    if not row:
        raise HTTPException(404, "配置不存在")
    return {
        c.name: getattr(row, c.name)
        for c in cls.__table__.columns
        if c.name not in ("created_at", "updated_at")
    }


@router.get("/rules")
def rules(db: Annotated[Session, Depends(get_db)]):
    state = read_rules(db)
    return {
        **state,
        "blocks": [
            dict(
                id=k, **v, content=state["texts"][k], overridden=k in state["overrides"]
            )
            for k, v in DEFAULT_RULES.items()
        ],
    }


@router.put("/rules")
def save_rules(request: RuleUpdate, db: Annotated[Session, Depends(get_db)]):
    try:
        result = update_rules(db, request.changes, request.version)
        return {
            **result,
            "message": "已保存；进行中的请求保持原版本，下一次请求采用新规则",
        }
    except ValueError as exc:
        raise HTTPException(
            409 if "更新" in str(exc) or "变化" in str(exc) else 422, str(exc)
        ) from exc


def build_preview(request, db):
    from app.api.endpoints.assistant_judges import JudgeUpdateRequest
    from app.api.endpoints.assistant_roles import RoleUpdateRequest

    role = row_data(db, ChatBotRole, request.role_id)
    judge = row_data(db, ChatBotJudge, request.judge_id)
    from pydantic import ValidationError

    try:
        model = RoleUpdateRequest if request.scene == "reply" else JudgeUpdateRequest
        unknown = set(request.draft) - set(model.model_fields)
        if unknown:
            raise ValueError("不支持的草稿字段：" + ", ".join(sorted(unknown)))
        draft = model(**request.draft).model_dump(exclude_unset=True, exclude_none=True)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    (role if request.scene == "reply" else judge).update(draft)
    state = read_rules(db)
    if set(request.rule_draft) - set(DEFAULT_RULES) or any(
        not v.strip() or len(v) > 30000 for v in request.rule_draft.values()
    ):
        raise HTTPException(422, "系统规则草稿无效")
    texts = {**state["texts"], **request.rule_draft}
    history = request.history
    notes = [
        "配置预览：未调用模型，也未保存草稿。",
        "运行时线程、附件实际路径及后续工具结果以真实请求快照为准。",
    ]
    user = chat(db, request.chat_id) if request.chat_id else None
    if request.use_chat_history:
        if history.strip():
            raise HTTPException(422, "真实聊天与模拟聊天只能选择一种")
        if not user:
            raise HTTPException(422, "请先选择聊天")
        from app.assistant.runtime import get_assistant_handler

        handler = get_assistant_handler()
        if not handler:
            raise HTTPException(409, "助手未运行，无法读取实时上下文；可使用模拟资料")
        rows = handler.chat_log_manager.get_context_messages(
            user.chat_name, 50 if request.scene == "reply" else 20
        )
    else:
        rows = (
            [{"sender": "模拟群友", "content": history, "is_bot": False}]
            if history
            else []
        )
    if request.scene == "reply":
        from app.utils.plugin_config import get_plugin_setting

        search_setting = get_plugin_setting("assistant", "search_enabled", True)
        web_enabled = (
            search_setting.strip().lower() in {"1", "true", "yes", "on"}
            if isinstance(search_setting, str)
            else bool(search_setting)
        )
        if user:
            from app.services.codex_permission_service import CodexPermissionService

            web_enabled = (
                web_enabled and CodexPermissionService(db).resolve(user).online_research
            )
        notes.append(
            "此处是文本请求预览；模型、搜索模式与附加工具的最终选择以实际快照为准。"
        )
        from app.history.context import render_recent

        history = render_recent(rows, ChatContextManager(), budget=6000)
        blocks = compose_reply(
            role.get("prompt") or "你是一个有用的助手。",
            role,
            history,
            f"[{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')}] [{request.sender}]: {request.content}",
            rules=texts,
        )
        from app.assistant.prompt_composer import output_contract
        from app.services.codex_delivery import delivery_instructions, delivery_schema
        from app.services.codex_proxy.client import render_chat_prompt

        blocks.append(
            block(
                "adapter",
                "运行时适配规则（路径为示例）",
                render_chat_prompt(
                    [],
                    artifact_output_dir="<本轮交付目录>",
                    native_instructions=True,
                    native_web_search_enabled=web_enabled,
                    available_file_commands=[],
                ),
                role="reference",
                source="运行时适配器",
            )
        )
        blocks.append(
            block(
                "delivery",
                "附件交付协议",
                delivery_instructions(reply_is_json=True),
                role="reference",
                source="运行时适配器",
            )
        )
        blocks.append(
            block(
                "delivery_schema",
                "交付结构",
                json.dumps(
                    delivery_schema(output_contract(role)[1]),
                    ensure_ascii=False,
                    indent=2,
                ),
                role="schema",
            )
        )
        from app.history.tools import dynamic_tool_specs

        blocks.append(
            block(
                "tools",
                "历史工具定义",
                json.dumps(dynamic_tool_specs(), ensure_ascii=False, indent=2),
                role="reference",
            )
        )
        if user:
            from app.services.codex_access_service import chat_scope_path
            from app.services.codex_permission_instructions import (
                permission_instructions,
            )
            from app.services.codex_permission_service import CodexPermissionService

            permissions = CodexPermissionService(db).resolve(user)
            blocks.append(
                block(
                    "permissions",
                    "当前聊天权限",
                    permission_instructions(
                        user_id=user.id,
                        permissions=permissions,
                        scope_root=chat_scope_path(user.chat_name),
                        workdir="<运行时工作目录>",
                    ),
                    role="reference",
                    source="聊天权限设置",
                    editable="permissions",
                )
            )
        else:
            notes.append("未选择聊天：权限、模型与可用工具按运行时配置确定。")
    else:
        history = "\n".join(
            f"[{r.get('sender', '群友')}]: {r.get('content', '')}" for r in rows
        )
        history += f"\n[{request.sender}]: {request.content}"
        blocks = compose_decision(
            judge.get("prompt") or "有明确值得补充的内容时接话，避免重复和打扰。",
            history,
            persona=role.get("prompt", ""),
            rules=texts,
        )
    timing = request.message_count >= int(
        judge.get("trigger_msg_threshold", 5) or 0
    ) and request.elapsed_minutes >= int(judge.get("trigger_interval_minutes", 1) or 0)
    if request.in_cooldown:
        timing = (
            timing
            and request.cooldown_message_count
            >= int(judge.get("cooldown_msg_threshold", 5) or 0)
            and request.cooldown_elapsed_minutes
            >= int(judge.get("cooldown_minutes", 1) or 0)
        )
    return {
        "blocks": blocks,
        "version": state["version"],
        "fingerprint": composition_version(texts),
        "notes": notes,
        "timing_met": timing,
        "draft": True,
    }


@router.post("/preview")
def preview(request: PreviewRequest, db: Annotated[Session, Depends(get_db)]):
    return build_preview(request, db)


@router.post("/trial")
def trial(request: PreviewRequest, db: Annotated[Session, Depends(get_db)]):
    if request.scene != "decision":
        raise HTTPException(422, "试判仅用于接话判断")
    result = build_preview(request, db)
    if not result["timing_met"] and not request.ignore_timing:
        return {
            **result,
            "result": {
                "state": "skipped",
                "should_reply": False,
                "reason": "模拟触发或冷却条件未满足，未调用模型",
            },
        }
    from app.assistant.runtime import get_assistant_handler

    handler = get_assistant_handler()
    try:
        messages = messages_from_blocks(result["blocks"])
        if handler is not None:
            raw = handler._call_auxiliary_model(
                "judge", messages, response_format={"type": "json_object"}
            )
            parsed = handler._extract_first_json_object(raw)
        else:
            # Decision trials need a configured model, not a connected WeChat listener.
            from app.services.llm_manager import get_llm_manager

            raw = get_llm_manager().call(
                plugin_name="assistant",
                call_type="judge",
                messages=messages,
                response_format={"type": "json_object"},
            )
            start = str(raw or "").find("{")
            parsed = (
                json.JSONDecoder().raw_decode(str(raw)[start:])[0]
                if start >= 0
                else None
            )
        if (
            not isinstance(parsed, dict)
            or type(parsed.get("should_reply")) is not bool
            or not isinstance(parsed.get("reason"), str)
        ):
            raise ValueError("判断结果格式无效")
        result["result"] = {
            "state": "completed",
            "should_reply": parsed["should_reply"],
            "reason": parsed["reason"],
            "atmosphere": str(parsed.get("atmosphere") or ""),
        }

        return result
    except Exception as exc:
        raise HTTPException(502, "试判失败，未发送微信或改变线上状态") from exc


@router.get("/chats/{chat_id}/snapshots")
def snapshots(chat_id: int, db: Annotated[Session, Depends(get_db)]):
    return {"items": list_snapshots(chat(db, chat_id).chat_name)}


@router.get("/chats/{chat_id}/snapshots/{snapshot_id}")
def snapshot(chat_id: int, snapshot_id: str, db: Annotated[Session, Depends(get_db)]):
    payload = get_snapshot(chat(db, chat_id).chat_name, snapshot_id)
    if payload is None:
        raise HTTPException(404, "快照不存在或已过期")
    return payload
