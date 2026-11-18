"""Administrator-only Web testing, never connected to a production sender."""
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from urllib.parse import quote
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.models.base import get_db
from app.api.endpoints.assistant_prompts import PreviewRequest
from app.services.chat_test_lab import lab, open_output

router=APIRouter()


class CreateTest(BaseModel):
    request: PreviewRequest
    variant: Literal['saved','draft']='saved'


class TestUpload(BaseModel):
    name: str = Field(min_length=1,max_length=160)
    data: str = Field(max_length=9*1024*1024)


class TestMessage(BaseModel):
    kind: Literal['message','mention','quote'] = 'message'
    quote: str = Field(default='',max_length=12000)
    quote_sender: str = Field(default='',max_length=160)
    content: str = Field(default='',max_length=20000)
    sender: str = Field(default='测试用户',min_length=1,max_length=160)
    files: list[TestUpload] = Field(default_factory=list,max_length=4)


def session(identity):
    try:return lab.get(identity)
    except KeyError as exc:raise HTTPException(404,str(exc)) from exc


@router.get('/sessions')
def sessions():
    with lab.lock:
        return {'items':[{'id':s.id,'chat_id':s.chat_id,'chat_name':s.source_chat,'status':s.status,'variant':s.variant,'model':s.profile['model'],'updated':s.updated} for s in lab.sessions.values()]}


@router.post('/sessions')
def create(request: CreateTest, db: Annotated[Session,Depends(get_db)]):
    from app.services.bot_reply_comparison import prepare_comparison
    from app.assistant.runtime import get_assistant_handler
    from app.services.codex_permission_service import CodexPermissionService
    if not request.request.chat_id or request.request.scene!='reply':
        raise HTTPException(422,'请选择聊天；连续工具测试用于主回复场景')
    preview=request.request.model_copy(update={'content':'测试会话初始化（尚未发送）'})
    from app.api.endpoints.assistant_prompts import chat
    from app.services.engine_permissions import selected_engine
    user=chat(db,preview.chat_id)
    if selected_engine(user)!='codex':
        raise HTTPException(422,'此聊天使用 OpenCode，目前工具测试会话仅接入 Codex')
    prepared=prepare_comparison(preview,db)
    handler=get_assistant_handler()
    if not handler:raise HTTPException(409,'助手尚未运行')
    described=CodexPermissionService(db).describe(user)
    from app.services.image_generation_config import resolve_policy
    from app.services.lens_management import read_settings as read_lens_settings
    from app.services.scheduled_tasks import get_scheduled_tasks
    tasks=get_scheduled_tasks()
    prepared.update(is_group=user.is_group, image_policy=resolve_policy(chat_id=user.id,db=db),
                    lens=(preview.lens_draft.model_dump() if request.variant=='draft' and preview.lens_draft else read_lens_settings(db)['settings']),
                    schedules_enabled=bool(tasks.settings()['enabled'] and tasks.chat_settings(user.id)['enabled']),
                    research_requested=bool(described['values']['online_research']))
    permissions=described['effective']
    prepared['chat_id']=preview.chat_id
    prepared['workspace_access']=permissions['workspace_access']
    online=prepared['research_requested'] and bool(prepared['profile'].get('supports_web_search'))
    try:return lab.create(prepared,handler.chat_log_manager.archive,request.variant,online,handler.chat_log_manager.get_context_messages,handler.event_bus)
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@router.get('/sessions/{identity}')
def get_session(identity: str, after: int=0):
    return session(identity).public(max(0,after))


@router.post('/sessions/{identity}/messages')
def send(identity: str, request: TestMessage):
    import base64
    if request.kind=='quote' and not request.quote.strip():raise HTTPException(422,'请填写引用内容')
    if not request.content.strip() and not request.files:raise HTTPException(422,'请输入消息或选择附件')
    try:
        files=[f.model_dump() for f in request.files]
        for item in files:
            if len(base64.b64decode(item['data'],validate=True))>6*1024*1024:
                raise ValueError('单个文件最多6MB')
        session(identity)
        return lab.send(identity,request.content,request.sender,files,request.kind,request.quote,request.quote_sender.strip())
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc


@router.post('/sessions/{identity}/stop')
def stop(identity: str):
    selected=session(identity)
    selected.cancel()
    return {'message':'已请求停止；结束后可重置测试会话'}


@router.post('/sessions/{identity}/reset')
def reset(identity: str):
    session(identity)
    try:return lab.reset(identity)
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@router.delete('/sessions/{identity}')
def delete(identity: str):
    session(identity)
    try:lab.delete(identity)
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    return {'message':'测试会话已删除'}


@router.get('/sessions/{identity}/files')
def file(identity: str,path: str,preview: bool=False):
    selected=session(identity)
    if not path.startswith('outputs/'):raise HTTPException(403,'只能下载测试输出文件')
    try:stream=open_output(selected.workspace/'outputs',path.removeprefix('outputs/'))
    except (ValueError, OSError) as exc:raise HTTPException(404,'文件不存在或不可安全下载') from exc
    mime='application/octet-stream'
    if preview:
        from PIL import Image
        try:
            with Image.open(stream) as picture:
                mime={'PNG':'image/png','JPEG':'image/jpeg','GIF':'image/gif','WEBP':'image/webp'}[picture.format]
            stream.seek(0)
        except (KeyError,ValueError,OSError,Image.DecompressionBombError):
            stream.close()
            raise HTTPException(415,'无法预览该文件')
    def chunks():
        with stream:
            remaining = 64 * 1024 * 1024
            while remaining > 0:
                data = stream.read(min(65536, remaining))
                if not data:break
                remaining -= len(data)
                yield data
    return StreamingResponse(chunks(),media_type=mime,background=BackgroundTask(stream.close),
        headers={'Content-Disposition':("inline" if preview else "attachment")+"; filename*=utf-8''"+quote(path.rsplit('/',1)[-1]),
                 'X-Content-Type-Options':'nosniff','Content-Security-Policy':'sandbox'})
