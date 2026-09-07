"""Administration of raw chat archives, using the existing managed-user scope."""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models import base, user_permission
from app.history.tools import get_archive


router = APIRouter()


class MemberEdit(BaseModel):
    current_name: str = Field(min_length=1, max_length=160)
    aliases: list[str] = Field(default_factory=list, max_length=60)
    version: Optional[int] = Field(None, ge=0)


@router.get('/users/{user_id}/members')
def members(user_id: int, query: str = Query('', max_length=160), offset: int = Query(0, ge=0, le=1000000), db: Session = Depends(base.get_db)):
    return get_archive().members(chat_for(user_id, db), query=query, offset=offset)


@router.put('/users/{user_id}/members/{identity}')
def edit_member(user_id: int, identity: str, edit: MemberEdit, db: Session = Depends(base.get_db)):
    try:
        row = get_archive().save_member(chat_for(user_id,db), edit.current_name, edit.aliases, identity=identity, version=edit.version)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {key:row[key] for key in ('id','current_name','aliases','version')}


@router.post('/users/{user_id}/members')
def create_member(user_id: int, edit: MemberEdit, db: Session = Depends(base.get_db)):
    try:
        row = get_archive().save_member(chat_for(user_id,db),edit.current_name,edit.aliases,version=0)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc
    return {key:row[key] for key in ('id','current_name','aliases','version')}


@router.delete('/users/{user_id}/members/{identity}')
def delete_member(user_id: int, identity: str, version: Optional[int] = Query(None, ge=0), db: Session = Depends(base.get_db)):
    try:
        deleted=get_archive().delete_member(chat_for(user_id,db),identity,version=version)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc
    if not deleted:raise HTTPException(404,'当前聊天中没有这个成员')
    return {'status':'deleted'}


def chat_for(user_id: int, db: Session):
    user = db.query(user_permission.WeChatUser).filter(user_permission.WeChatUser.id == user_id).first()
    if user is None:
        raise HTTPException(404, "Chat not found")
    return user.chat_name


@router.get("/users/{user_id}")
def browse(user_id: int, query: str = Query("", max_length=200), sender: str = Query("", max_length=160),
           start: str = "", end: str = "", offset: int = Query(0, ge=0, le=1000000),
           db: Session = Depends(base.get_db)):
    chat = chat_for(user_id, db)
    store = get_archive()
    try:
        result = store.search(chat, query=query, sender=sender, start=start, end=end, offset=offset, limit=50)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    messages = []
    for identity in result.pop("ids"):
        rows = store.read(chat, identity)
        if rows:
            row = rows[0]
            messages.append({"id": identity, "sender": row["sender"], "time": row.get("time") or row["occurred_at"],
                             "content": row["content"][:600], "truncated": len(row["content"]) > 600,
                             "kind": row.get("message_type") or "text", "is_bot": bool(row.get("is_bot")),
                             "evidence_only": bool(row.get("evidence_only")), "corrected": bool(row.get("correction"))})
    return {**result, "chat": chat, "messages": messages, "summary": store.coverage(chat),
            "coverage_notice": "日期范围表示已保存记录的起止时间，不能证明期间完整。补充日志证据可能缺少元数据或与原始记录重叠。"}


@router.get("/users/{user_id}/coverage")
def coverage(user_id: int, offset: int = Query(0, ge=0, le=1000000), db: Session = Depends(base.get_db)):
    chat = chat_for(user_id, db)
    with get_archive().connection() as connection:
        days = [dict(row) for row in connection.execute("SELECT date(occurred,'+8 hours') AS day,kind,count(*) AS messages FROM messages WHERE chat=? GROUP BY day,kind ORDER BY day DESC,kind LIMIT 101 OFFSET ?", (chat,offset))]
    return {"chat": chat, "days": days[:100], 'next_offset':offset+100 if len(days)>100 else None, "completeness": "unknown_without_authoritative_export"}


@router.get("/users/{user_id}/lookups")
def lookups(user_id: int, db: Session = Depends(base.get_db)):
    chat = chat_for(user_id, db)
    with get_archive().connection() as connection:
        rows = [dict(row) for row in connection.execute("SELECT request_id,tool,details,created_at FROM audit WHERE chat=? ORDER BY seq DESC LIMIT 50", (chat,))]
    for row in rows:
        row["details"] = json.loads(row["details"])
    return {"items": rows}


@router.get("/users/{user_id}/export")
def export(user_id: int, db: Session = Depends(base.get_db)):
    chat = chat_for(user_id, db)
    rows = get_archive().export_rows(chat)
    return StreamingResponse((json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                             media_type="application/x-ndjson",
                             headers={"Content-Disposition": f'attachment; filename="chat-{user_id}.jsonl"'})


@router.get("/users/{user_id}/messages/{message_id}")
def detail(user_id: int, message_id: str, offset: int = Query(0, ge=0), db: Session = Depends(base.get_db)):
    rows = get_archive().read(chat_for(user_id, db), message_id, before=2, after=2)
    if not rows:
        raise HTTPException(404, "Message not found")
    for row in rows:
        start = offset if row["id"] == message_id else 0
        text = row["content"]
        row["content"] = text[start:start + 16000]
        row["next_offset"] = start + 16000 if start + 16000 < len(text) else None
    return {"messages": rows}


class Revision(BaseModel):
    content: Optional[str] = Field(None, max_length=200000)
    deleted: Optional[bool] = None
    reason: str = Field(min_length=2, max_length=1000)


@router.put("/users/{user_id}/messages/{message_id}")
def revise(user_id: int, message_id: str, revision: Revision, db: Session = Depends(base.get_db)):
    chat = chat_for(user_id, db)
    changes = {key: value for key, value in revision.model_dump().items() if key in {"content", "deleted"} and value is not None}
    changes["correction"] = {"reason": revision.reason, "source": "administrator"}
    if not get_archive().revise(chat, message_id, **changes):
        raise HTTPException(404, "Message not found")
    return {"status": "success"}
