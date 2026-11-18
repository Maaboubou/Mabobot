// Continuous conversations always use the configuration captured at creation.
const ChatTestLab = {
    esc(v) { return UI.escapeHtml(String(v ?? '')); },
    open(options={}) { return AssistantStudio.open({...options,view:'lab'}); },
    async mount(body, studio) {
        const s = this.state = {body,studio,sessions:[],session:null,events:[],cursor:0,seq:0};
        body.innerHTML = `<div class="tl-grid">
          <aside class="tl-sessions"><div class="tl-rail-title"><h3>测试会话</h3><button class="btn btn-sm btn-link" data-close-list>收起</button></div>
            <label class="tl-variant">新会话配置<select class="form-select form-select-sm" data-variant><option value="saved">已保存配置</option><option value="draft">当前编辑草稿</option></select></label>
            <button class="btn btn-primary btn-sm w-100" data-create>新建会话</button>
            <nav data-session-list aria-label="测试会话"></nav><small>现场聊天背景 · 模型调用计费<br>最多 4 个会话，服务重启后清除</small></aside>
          <main class="tl-conversation"><header class="tl-conversation-heading">
            <button class="btn btn-sm btn-outline-secondary" data-list-toggle aria-expanded="false">会话</button>
            <div class="tl-heading-text"><h3 data-heading>测试对话</h3><small data-state></small></div>
            <details class="tl-menu"><summary>管理</summary><div><button data-reset>重置会话</button><button data-delete>删除会话</button></div></details></header>
            <div class="tl-transcript" data-transcript aria-live="polite"></div>
            <div class="tl-composer"><div data-quote-row hidden><input class="form-control form-control-sm" data-quote-sender aria-label="引用作者" placeholder="作者（默认机器人）" maxlength="160"><input class="form-control form-control-sm" data-quote aria-label="引用内容" placeholder="引用内容" maxlength="12000"></div><textarea id="tlMessage" class="form-control" rows="2" maxlength="20000" aria-label="测试消息" placeholder="输入测试消息"></textarea>
            <div class="tl-compose-footer"><label class="btn btn-sm btn-outline-secondary tl-upload" title="最多 4 个附件，每个 6 MB"><i class="bi bi-paperclip"></i><span class="visually-hidden">附件</span><input type="file" multiple data-files></label>
            <select class="form-select form-select-sm tl-event-kind" aria-label="模拟消息类型" data-kind><option value="message">普通消息</option><option value="mention">@机器人</option><option value="quote">引用消息</option></select><input class="form-control form-control-sm tl-sender-field" data-sender value="测试用户" maxlength="160" aria-label="模拟发言人">
            <button class="btn btn-outline-danger btn-sm" data-stop hidden>停止</button><button class="btn btn-primary btn-sm" data-send>发送</button></div>
            <small data-file-note></small><p class="tl-status" data-status role="status"></p></div></main></div>`;
        s.el = name => body.querySelector('[data-'+name+']');
        const list = open => {
            body.classList.toggle('tl-show-list',open);
            s.el('list-toggle').setAttribute('aria-expanded',String(open));
            if(open)s.el('variant').focus();
        };
        s.el('list-toggle').onclick = () => list(!body.classList.contains('tl-show-list'));
        s.el('close-list').onclick = () => {list(false);s.el('list-toggle').focus();};
        s.el('session-list').onclick = e => {
            const button=e.target.closest('[data-session]');
            if(button && !this.mutating()) {list(false);this.select(button.dataset.session);}
        };
        s.el('create').onclick = () => this.create();
        s.el('kind').onchange=()=>{s.el('quote-row').hidden=s.el('kind').value!=='quote';if(!s.el('quote-row').hidden)s.el('quote').focus();};
        s.el('send').onclick = () => this.send();
        s.el('stop').onclick = () => this.stop();
        for(const action of ['reset','delete']) s.el(action).onclick = () => this.remove(action);
        s.el('files').onchange = () => s.el('file-note').textContent = [...s.el('files').files].map(f=>f.name).join('、');
        body.querySelector('#tlMessage').onkeydown = e => {if((e.ctrlKey || e.metaKey) && e.key==='Enter'){e.preventDefault();this.send();}};
        this.syncVariant();this.render();
        try {
            await this.refreshList(s);
            if(this.state!==s)return;
            s.ready=true;
            this.changeChat();
        } catch(error) {
            if(this.state!==s)return;
            this.status('会话列表读取失败：'+error.message);
            const retry=document.createElement('button');
            retry.className='btn btn-sm btn-outline-secondary';retry.textContent='重试';
            retry.onclick=()=>this.mount(body,studio);
            s.el('status').append(retry);
        }
    },
    status(text) { if(this.state) this.state.el('status').textContent=text; },
    mutating() { const s=this.state;return !!(s?.sending || s?.creating || s?.removing); },
    syncVariant() {
        const s=this.state, draft=AssistantStudio.draftRequest();
        const select=s.el('variant');
        select.options[1].disabled=!draft;
        const snapshot=JSON.stringify(draft);
        if(s.draftKey!==snapshot)select.value=draft?'draft':'saved';
        s.draftKey=snapshot;
        const label=()=>s.el('create').textContent=select.value==='draft'?'用当前草稿新建':'新建会话';
        select.onchange=label;label();
    },
    async refreshList(s) {
        const r=await API.get('/api/assistant/test-lab/sessions');
        if(this.state!==s)return;
        s.sessions=r.items.sort((a,b)=>(b.updated||0)-(a.updated||0));
        this.list();
    },
    list() {
        const s=this.state;
        s.el('session-list').innerHTML=s.sessions.filter(x=>Number(x.chat_id)===s.studio.chatId).map(x=>`<button data-session="${this.esc(x.id)}" aria-current="${x.id===s.session?.id}"><strong>${this.esc(x.chat_name)}</strong><span>${this.version(x)} · ${this.label(x.status)}${x.updated?' · '+this.esc(new Date(x.updated*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})):''}</span></button>`).join('') || '<p class="tl-muted">暂无会话</p>';
    },
    version(r) { return r.variant==='draft'?'草稿配置':'已保存配置'; },
    label(value) { return {loading:'读取中',preparing:'准备中',idle:'就绪',running:'执行中',failed:'失败',cancelled:'已停止'}[value]||''; },
    changeChat() {
        const s=this.state;
        this.syncVariant(); this.list();
        s.body.classList.remove('tl-show-list');
        s.el('list-toggle').setAttribute('aria-expanded','false');
        this.select(s.sessions.find(x=>Number(x.chat_id)===s.studio.chatId)?.id);
    },
    controls() {
        const s=this.state, busy=this.mutating(), status=s.session?.status;
        s.el('send').disabled=busy || status!=='idle';
        s.body.querySelector('#tlMessage').disabled=busy;
        s.el('files').disabled=busy;
        s.el('sender').disabled=busy;
        s.el('kind').disabled=busy;
        s.el('quote').disabled=busy;
        s.el('kind').querySelector('[value=mention]').disabled=!s.session?.is_group;
        s.el('create').disabled=busy || !s.ready || !s.studio.chatId;
        s.el('stop').hidden=!['running','preparing'].includes(status);
        s.el('variant').disabled=busy;
        s.el('delete').disabled=s.el('reset').disabled=busy || !s.session || ['running','preparing'].includes(status);
        s.studio.select.disabled=busy || BotWorkbench.state?.busy || !s.studio.overview.chats?.length;
    },
    clearInput() {
        const s=this.state;
        s.body.querySelector('#tlMessage').value='';
        s.el('files').value='';
        s.el('quote').value='';s.el('quote-sender').value='';
        s.el('file-note').textContent='';
        this.status('');
    },
    async select(id) {
        const s=this.state;
        clearTimeout(this.timer);s.seq++;s.events=[];s.cursor=0;
        s.session=id?{...s.sessions.find(x=>x.id===id),id,status:'loading',messages:[]}:null;
        s.el('kind').value='message';s.el('quote-row').hidden=true;
        this.clearInput();this.render();this.list();
        if(id)await this.poll(s,s.seq);
    },
    async poll(s,seq) {
        const id=s.session?.id;
        if(!id || seq!==s.seq || this.state!==s)return;
        clearTimeout(this.timer);
        try {
            const r=await API.get(`/api/assistant/test-lab/sessions/${id}?after=${s.cursor}`);
            if(this.state!==s || seq!==s.seq)return;
            s.session=r;s.events.push(...(r.events||[]));s.events=s.events.slice(-2000);s.cursor=r.cursor;
            this.render();
            if(['preparing','running'].includes(r.status))this.timer=setTimeout(()=>this.poll(s,seq),1000);
            else {await this.refreshList(s);}
        } catch(error) {
            if(this.state!==s || seq!==s.seq)return;
            if(error.status===404) {
                await this.select(null);
                this.status('此测试会话已结束');
                await this.refreshList(s).catch(()=>{});
                return;
            }
            this.status('读取失败：'+error.message);
            this.timer=setTimeout(()=>this.poll(s,seq),3000);
        }
    },
    async create() {
        const s=this.state;
        if(this.mutating() || !s.studio.chatId)return;
        s.creating=true;this.controls();this.status('正在建立会话…');
        const variant=s.el('variant').value;
        const request=variant==='draft'?AssistantStudio.draftRequest():{chat_id:s.studio.chatId};
        try {
            const r=await API.post('/api/assistant/test-lab/sessions',{variant,request});
            if(this.state!==s)return;
            s.sessions.unshift(r);
            s.body.classList.remove('tl-show-list');s.el('list-toggle').setAttribute('aria-expanded','false');
            await this.select(r.id);
        } catch(error){if(this.state===s)this.status('新建失败：'+error.message);}
        finally {if(this.state===s){s.creating=false;this.controls();}}
    },
    files(files,id,preview=false) {
        return (files||[]).map(f=>{
            const url=`/api/assistant/test-lab/sessions/${encodeURIComponent(id)}/files?path=${encodeURIComponent(f.path)}`;
            const image=preview && ['image/png','image/jpeg','image/webp','image/gif'].includes(f.mime_type);
            return `<a class="tl-file" download href="${url}">${image?`<img class="tl-image" loading="lazy" alt="${this.esc(f.name)}" src="${url}&preview=true">`:''}<i class="bi bi-file-earmark"></i> ${this.esc(f.name)} <small>${(f.bytes/1024).toFixed(1)} KB</small></a>`;
        }).join('');
    },
    render() {
        const s=this.state,r=s.session,panel=s.el('transcript');
        const open=new Set([...panel.querySelectorAll('details[open]')].map(d=>d.dataset.detail));
        const atBottom=panel.scrollHeight-panel.scrollTop-panel.clientHeight<64;
        const details=(key,title,html)=>`<details class="tl-detail" data-detail="${this.esc(key)}" ${open.has(String(key))?'open':''}><summary>${this.esc(title)}</summary>${html}</details>`;
        const events=(turn)=>s.events.filter(e=>e.turn===turn && !['ready','turn_started','turn_completed','reply_completed'].includes(e.kind)).map(e=>details('e'+e.id,(e.details.tool||e.kind)+(e.details.elapsed_ms!=null?' · '+e.details.elapsed_ms+' ms':''),`<pre>${this.esc(JSON.stringify(e.details,null,2))}</pre>`)).join('');
        s.el('heading').textContent=r?.chat_name || '测试对话';
        s.el('state').textContent=r ? this.version(r)+' · '+this.label(r.status) : '';
        s.body.querySelector('.tl-menu').hidden=!r;
        let html='';
        if(r){
            for(const [index,m] of (r.messages||[]).entries()) {
                if(m.role==='notice'){html+=`<p class="tl-muted">${this.esc(m.content)}</p>`;continue;}
                const lastPart=r.messages[index+1]?.turn!==m.turn;
                const actions=lastPart && m.role==='assistant'?(r.actions||[]).filter(a=>a.turn===m.turn):[];
                const user=m.role==='user',steps=user||!lastPart?'':events(m.turn);
                html+=`<article class="tl-message ${user?'user':'assistant'}"><div>${user?this.esc(m.sender||'测试用户'):'助手'}</div>${m.kind==='quote'?`<blockquote>${this.esc(m.quote_sender||'机器人')} · ${this.esc(m.quote)}</blockquote>`:m.kind==='mention'?'<small>@机器人</small>':''}<p>${this.esc(m.content)}</p>
                  ${m.attachments?.length?`<small>${m.attachments.map(f=>this.esc(f)).join('、')}</small>`:''}
                  ${m.mentions?.length?`<small>@ ${m.mentions.map(v=>this.esc(v)).join('、')}</small>`:''}
                  ${steps?details('turn'+m.turn,'执行过程',steps):''}${this.files(m.files,r.id,true)}${actions.map(a=>details('action'+r.actions.indexOf(a),({email:'邮件预演',business_write:'业务写入预演',schedule:'测试任务'})[a.operation]||'操作预演',`<p>${this.esc(a.target)}</p><pre>${this.esc(typeof a.content==='string'?a.content:JSON.stringify(a.content,null,2))}</pre>`)).join('')}</article>`;
            }
            if(!html) {
                const emptyText={loading:'正在读取会话…',preparing:'正在初始化会话…',idle:'会话已就绪',failed:'会话准备失败',cancelled:'会话已停止',running:'执行中…'};
                html=`<div class="tl-empty">${emptyText[r.status]||'等待会话状态…'}</div>`;
            }
            if(['running','failed','cancelled'].includes(r.status)) {
                const last=r.messages?.at(-1);
                if(last?.role==='user')html+=events(last.turn);
            }
            if(r.error)html+=`<p role="alert" class="tl-error">${this.esc(r.error)}</p>`;
            const scope=(r.capabilities||[]).map(c=>`<p><strong>${this.esc(c.name)}</strong> · ${this.esc(c.mode)}<br>${this.esc(c.detail)}</p>`).join('');
            if(r.context && r.status!=='preparing')html+=details('info','会话详情',`<p>${this.esc(r.model)} · 文件权限 ${this.esc(({read_write:'可写',read_only:'只读',none:'关闭'})[r.workspace_access]||'按聊天配置')} · 生图 ${this.esc(r.image_mode?.startsWith('api')?'API':'原生')} · 搜索${r.online?'开启':'关闭'}</p>`+details('scope','隔离范围',scope)+details('context','本次聊天背景',`<pre>${this.esc(r.context.history)}</pre>`)+(r.files?.length?details('files','会话文件',this.files(r.files,r.id)):''));
        } else html='<div class="tl-empty"><i class="bi bi-chat-square-text"></i><button class="btn btn-sm btn-outline-secondary" data-first>新建测试会话</button></div>';
        panel.innerHTML=html;
        panel.querySelector('[data-first]')?.addEventListener('click',()=>this.create());
        if(atBottom)panel.scrollTop=panel.scrollHeight;
        this.controls();
    },
    async send() {
        const s=this.state;
        if(this.mutating() || s.session?.status!=='idle')return;
        const text=s.body.querySelector('#tlMessage').value, files=[...s.el('files').files];
        if(!text.trim()&&!files.length){this.status('请输入消息或添加附件');return;}
        if(files.length>4 || files.some(f=>f.size>6*1024*1024)){this.status('最多 4 个附件，每个 6 MB');return;}
        const kind=s.el('kind').value,quote=kind==='quote'?s.el('quote').value:'';
        if(kind==='quote'&&!quote.trim()){this.status('请填写引用内容');return;}
        const id=s.session.id;
        s.sending=true;clearTimeout(this.timer);s.seq++;this.controls();
        try {
            const uploads=await Promise.all(files.map(f=>new Promise((resolve,reject)=>{
                const reader=new FileReader();
                reader.onload=()=>resolve({name:f.name,data:String(reader.result).split(',')[1]});
                reader.onerror=reject;reader.readAsDataURL(f);
            })));
            const r=await API.post(`/api/assistant/test-lab/sessions/${id}/messages`,{content:text,kind,quote,quote_sender:kind==='quote'?s.el('quote-sender').value:'',sender:s.el('sender').value||'测试用户',files:uploads});
            if(this.state!==s)return;
            s.session=r;this.clearInput();this.render();await this.poll(s,s.seq);
        }catch(error){if(this.state===s)this.status('发送失败：'+error.message);}
        finally{if(this.state===s){s.sending=false;this.controls();}}
    },
    async stop() {
        const s=this.state,id=s.session?.id;
        try {await API.post(`/api/assistant/test-lab/sessions/${id}/stop`,{});if(this.state===s&&s.session?.id===id)this.status('已请求停止');}
        catch(error){if(this.state===s&&s.session?.id===id)this.status(error.message);}
    },
    async remove(action) {
        const s=this.state,id=s.session?.id;
        if(!id || this.mutating() || !confirm(action==='reset'?'清空记录并重置此会话？':'删除此会话及文件？'))return;
        s.removing=true;this.controls();s.body.querySelector('.tl-menu').open=false;
        try {
            const url='/api/assistant/test-lab/sessions/'+id;
            const next=action==='reset'?await API.post(url+'/reset',{}):await API.delete(url);
            if(this.state!==s)return;
            await this.refreshList(s);
            if(this.state===s)await this.select(action==='reset'?next.id:s.sessions.find(x=>Number(x.chat_id)===s.studio.chatId)?.id);
        }catch(error){if(this.state===s)this.status(error.message);}
        finally{if(this.state===s){s.removing=false;this.controls();}}
    }
};
