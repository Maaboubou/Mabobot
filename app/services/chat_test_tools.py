"""Bind production tool implementations to a Web turn and its temporary assets."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.setting import Setting
from app.services.codex_browser_tool import BrowserToolContext
from app.services.codex_image_tool import ImageToolSession
from app.services.codex_lens_tool import LensSession, image_bindings
from app.services.codex_proxy.client import _as_runtime_path
from app.services.image_generation_assets import asset_storage
from app.services.lens_management import LensSettings


def external_preview_spec():
    return {'type':'namespace','name':'preview','description':'Web 测试中的外发意图。仅记录拟进行的操作。',
            'tools':[{'type':'function','name':'stage','description':'仅在需要发送邮件或更改外部业务记录时，登记目标和拟提交内容。结果只表示预演，不能声称已发送或提交。',
                      'inputSchema':{'type':'object','additionalProperties':False,'required':['operation','target','content'],
                                     'properties':{'operation':{'type':'string','enum':['email','business_write']},
                                                   'target':{'type':'string','minLength':1,'maxLength':500},
                                                   'content':{'type':'string','minLength':1,'maxLength':12000}}}}]}


class WebTurnTools:
    def __init__(self, session, paths, text):
        self.session = session
        wsl = session.manager.use_wsl
        workspace = session.workspace
        output = workspace/'outputs'
        runtime = lambda p: _as_runtime_path(p, wsl)
        self.context = BrowserToolContext(
            request_id=f'lab-{session.id}-{session.turn_number}', chat_id=session.source_chat,
            access_mode='isolated', workdir=workspace, request_dir=workspace, output_dir=output,
            scratch_root=workspace, runtime_workdir=runtime(workspace), runtime_request_dir=runtime(workspace),
            runtime_output_dir=runtime(output), use_wsl=wsl, workspace_access=session.workspace_access,
            online_research_allowed=session.browser_allowed, reviewed_download_allowed=session.browser_allowed and session.workspace_access=='read_write',
            permission_chat_id=session.chat_id, cancelled=session.cancelled.is_set)
        self.images = ImageToolSession(self.context,session.image_policy,inputs=paths,request_text=text,
            history_session=session.history, progress=lambda info:session.event('image_api_attempt',info))
        image_paths = [p for p in paths if p.suffix.lower() in {'.png','.jpg','.jpeg','.webp','.gif'}]
        self.bindings = image_bindings([],image_paths)
        self.lens = LensSession(self.context,self.bindings,settings=LensSettings(**session.prepared.get('lens',{})),
                               progress=lambda stage:session.event('lens_progress',{'stage':stage}))

    def execute(self, namespace, name, args):
        s = self.session
        if namespace=='images':
            with self.storage():
                return self.images.execute(name,args)
        if namespace=='wx_browser':
            return s.manager.browser_tool.execute(namespace=namespace,tool=name,arguments=args,context=self.context)
        if namespace=='wx_lens':
            return self.lens.execute(name,args)
        if namespace=='preview' and name=='stage':
            if set(args)!={'operation','target','content'} or args['operation'] not in ('email','business_write'):
                raise ValueError('无效的预演操作')
            if not all(isinstance(args[k],str) and 0<len(args[k].strip())<=size for k,size in [('target',500),('content',12000)]):
                raise ValueError('请提供操作目标和内容')
            action={'turn':s.turn_number,**args,'status':'preview','executed':False}
            with s.lock:s.actions.append(action)
            return action
        raise ValueError('此工具未接入当前测试会话')

    def storage(self):
        s=self.session
        if s.asset_engine is None:
            engine=create_engine('sqlite:///'+(s.root/'image-state.sqlite3').as_posix())
            Setting.__table__.create(engine,checkfirst=True)
            s.asset_engine=engine
        return asset_storage(s.root/'image-assets',sessionmaker(bind=s.asset_engine))

    def finalize_images(self, selected):
        if not any(item.get('type')=='image' or item['name'].lower().endswith('.svg') for item in selected):return
        from app.services.codex_delivery import finalize_attachment_images
        from app.services.image_generation_assets import mark_delivery
        with self.storage():
            finalize_attachment_images(self.context.chat_id,selected,request_id=self.context.request_id)
            mark_delivery(self.context.chat_id,[f['asset_id'] for f in selected if f.get('asset_id')],'sent')

    def close(self):
        self.images.close()
        self.lens.close()
