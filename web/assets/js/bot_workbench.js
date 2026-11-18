/* Administrator workbench: source edits, draft composition and observed requests. */
const BotWorkbench = {
    esc(value) { return UI.escapeHtml(String(value ?? '')); },
    open(options={}) { return AssistantStudio.open(options); },
    async mount(body, studio) {
        const state = this.state = {items:[], seq:0, selected:'base'};
        body.innerHTML = '<div class="bw-loading" role="status"><span class="spinner-border spinner-border-sm"></span> 正在读取规则与聊天配置…</div>';
        try {
            const overview = studio.overview;
            const [rules, base] = await Promise.all([ API.get('/api/assistant/prompts/rules'), API.get('/api/assistant/prompts/base')]);
            if (this.state !== state) return;
            Object.assign(state, {overview, rules, base, chatId: studio.chatId, scene: 'reply'});
            state.items = [
                {id:'base',title:'全局基础指令',group:'运行基础',kind:'base',content:base.content,mode:base.mode,scope:'使用此后端的聊天'},
                ...(overview.roles||[]).map(row=>({id:`role:${row.id}`,key:row.id,title:row.display_name,group:'角色人设',kind:'role',content:row.prompt,revision:row.revision,
                    scope:`${(overview.chats||[]).filter(c=>c.role?.id===row.id).length} 个聊天`,outputSaved:this.output(row)})),
                ...rules.blocks.map(row=>({id:`rule:${row.id}`,key:row.id,title:row.title,group:'应用规则',kind:'rule',content:row.content,defaultText:row.text,scope:row.scope})),
                ...(overview.judges||[]).map(row=>({id:`judge:${row.id}`,key:row.id,title:row.display_name,group:'接话判断',kind:'judge',content:row.prompt,revision:row.revision,
                    scope:`${(overview.chats||[]).filter(c=>c.judge?.id===row.id).length} 个聊天`,outputSaved:{
                        trigger_msg_threshold:row.trigger_msg_threshold??5,trigger_interval_minutes:row.trigger_interval_minutes??1,
                        cooldown_msg_threshold:row.cooldown_msg_threshold??5,cooldown_minutes:row.cooldown_minutes??1}}))
            ].map(i=>({...i,content:i.content||'',draft:i.content||'',draftMode:i.mode,outputDraft:i.outputSaved?{...i.outputSaved}:undefined}));
            const target = studio.options.kind && state.items.find(i => i.id === `${studio.options.kind}:${studio.options.id}`);
            if(target) { state.selected=target.id; state.scene=target.kind==='judge'?'decision':'reply'; }
            body.innerHTML = `<div class="bw-grid"><aside class="bw-nav"><a class="bw-quick-link" href="#bwQuick">快速验证 ↓</a><details data-sources><summary>指令来源</summary>
                <input id="bwSearch" aria-label="搜索指令来源" type="search" class="form-control form-control-sm" placeholder="搜索来源"><nav data-directory aria-label="指令来源"></nav></details></aside>
                <main class="bw-editor" data-editor></main>
                <aside class="bw-inspector"><div id="bwQuick" data-quick></div>
                <details class="bw-advanced"><summary>高级检查</summary>
                <label>请求场景<select data-scene class="form-select form-select-sm"><option value="reply">主回复</option><option value="decision">接话判断</option></select></label>
                <button class="btn btn-sm btn-outline-secondary" data-preview>对比请求结构</button>
                <button class="btn btn-sm btn-outline-secondary" data-snapshot>最近真实请求</button>
                <a class="bw-permissions" data-permissions target="_blank" rel="noopener" href="/chats">聊天权限 ↗</a><div data-evidence></div>
                <details class="bw-advanced" data-lens-section><summary>图片与视频检索参数</summary><div data-lens-settings></div></details></details>
                <section class="bw-results" data-results hidden></section></aside></div>`;
            state.body = body;
            const narrow = window.matchMedia('(max-width: 1040px)');
            const adapt = () => {body.querySelector('[data-sources]').open = !narrow.matches;};
            body.querySelector('.bw-quick-link').onclick=e=>{e.preventDefault();body.querySelector('[data-quick]').scrollIntoView({block:'start'});body.querySelector('[data-quick] button:not(:disabled)')?.focus({preventScroll:true});};
            adapt(); narrow.addEventListener('change', adapt);
            studio.modal.addEventListener('hidden.bs.modal',()=>narrow.removeEventListener('change',adapt),{once:true});
            body.querySelector('[data-scene]').value = state.scene;
            body.querySelector('[data-scene]').onchange = e => {state.scene=e.target.value;state.seq++;body.querySelector('[data-results]').hidden=true;};
            body.querySelector('[data-permissions]').href='/chats?chat_id='+Number(state.chatId);
            body.querySelector('#bwSearch').oninput = () => this.directory();
            body.querySelector('[data-preview]').onclick = () => this.preview();
            body.querySelector('[data-snapshot]').onclick = () => this.snapshots();
            body.querySelector('[data-lens-section]').addEventListener('toggle',e=>{if(e.target.open&&!e.target.dataset.loaded){e.target.dataset.loaded='true';PromptWorkbench.lensPanel(body.querySelector('[data-lens-settings]'),state.overview.chats||[]);}});
            this.directory(); this.editor();
            state.quick = BehaviorCheck.mount(body.querySelector('[data-quick]'),()=>this.checkContext());
            this.loadBaseSource(); await this.loadChatBase(!target);
        } catch (e) { if (this.state === state) {body.innerHTML = `<div class="bw-loading" role="alert">加载失败：${this.esc(e.message)} <button class="btn btn-primary" data-retry>重新加载</button></div>`;body.querySelector('[data-retry]').onclick = () => this.mount(body,studio);} }
    },
    async loadChatBase(select=false, refresh=false) {
        const s=this.state, chatId=s.chatId, selection=s.seq;if(!chatId)return;
        const id=`chatbase:${chatId}`, existing=s.items.find(i=>i.id===id);
        if(existing&&!refresh){if(select)s.selected=id;this.directory();this.editor();return;}
        try{
            const config=await API.get(`/api/assistant/prompts/chats/${chatId}/base`);
            if(this.state!==s)return;
            if(existing&&this.changed(existing))return;
            const item={id,chatId,kind:'chatbase',title:'本聊天基础指令',group:'00 · 当前聊天',config,content:config.content||'',draft:config.content||'',mode:config.mode,draftMode:config.mode};
            if(existing)Object.assign(existing,item);else s.items.push(item);
            if(String(s.chatId)===String(chatId)&&!s.busy){if(select && s.seq===selection && s.selected==='base')s.selected=id;this.directory();this.editor();}
        }catch(e){if(this.state===s&&String(s.chatId)===String(chatId))this.status(`聊天配置读取失败：${e.message}`);}
    },
    chatBaseEditor(s,i,editor) {
        const modes=[['inherit','继承全局','保持现有全局设置'],['default','原生默认','仅此聊天取消全局覆盖'],['persona','融入当前人设','原生指令 + 适配说明 + 人设'],['custom','完全自定义','仅此聊天使用完整替换文本']];
        const c=i.config, role=this.chat()?.role;
        editor.innerHTML=`<div class="bw-editor-heading"><h2>本聊天基础指令</h2><div class="bw-scope">保存范围 · ${this.esc(this.chat()?.chat_name)}</div></div><div class="bw-mode bw-chat-modes">${modes.map(([m,t,d])=>`<button data-mode="${m}" aria-pressed="${i.draftMode===m}">${t}<small>${d}</small></button>`).join('')}</div>
        ${i.draftMode==='custom'?`<label class="bw-text-label" for="bwText">本聊天完整基础指令</label><textarea id="bwText" class="bw-text" maxlength="100000">${this.esc(i.draft)}</textarea>`:i.draftMode==='persona'?`<details class="bw-block"><summary>原生指令</summary><pre>${this.esc(s.baseSource?.content||s.baseSource?.notice||'正在读取原文…')}</pre></details><details class="bw-block"><summary>聊天适配 · ${this.esc(c.adapter_version)}</summary><pre>${this.esc(c.adapter)}</pre></details><details class="bw-block"><summary>绑定人设 · ${this.esc(role?.display_name||'未绑定')}</summary><pre>${this.esc(s.items.find(x=>x.id===`role:${role?.id}`)?.draft||role?.prompt||'未绑定人设')}</pre>${role?`<button class="btn btn-sm btn-outline-secondary bw-source" data-persona-edit>编辑此角色 ↗</button>`:''}</details>`:`<p class="bw-muted">${i.draftMode==='inherit'?'继承全局基础指令设置；人设仍由应用指令提供。':'使用当前 Profile / 模型原生默认；人设仍由应用指令提供。'}</p>`}
        ${c.source_diff?`<details class="bw-block" open><summary>上游原文已变化 · 请检查身份与表达适配</summary><pre>${this.esc(c.source_diff)}</pre></details>`:''}
        ${c.composition?`<details class="bw-block"><summary>已保存配置的最终合成结果<small>${this.esc(c.composition.fingerprint?.slice(0,16))} · ${c.matches_last_request===true?'与最近请求一致':c.matches_last_request===false?'等待后续请求采用':'尚无请求可核验'}</small></summary><pre>${this.esc(c.composition.content||'跟随运行时原生默认')}</pre></details>`:`<p class="bw-status">${this.esc(c.notice||'尚未生成合成结果')}</p>`}
        <div class="bw-status" data-status role="status"></div><div class="bw-editor-footer"><span data-count></span><div><button class="btn btn-outline-secondary" data-undo>撤销修改</button><button class="btn btn-primary" data-save>仅保存此聊天</button></div></div><details class="bw-history"><summary>恢复本聊天历史配置</summary><select class="form-select" data-chat-history><option value="">选择版本…</option>${(c.history||[]).slice().reverse().map((h,n)=>`<option value="${n}">v${h.version} · ${this.esc(h.mode)}</option>`).join('')}</select></details>`;
        editor.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>{i.draftMode=b.dataset.mode;this.editor();this.directory();});
        editor.querySelector('#bwText')?.addEventListener('input',e=>{i.draft=e.target.value;this.sync();this.directory();});
        editor.querySelector('[data-persona-edit]')?.addEventListener('click',()=>{this.choose(`role:${role.id}`);});
        editor.querySelector('[data-save]').onclick=()=>this.save();
        editor.querySelector('[data-undo]').onclick=()=>{i.draft=i.content;i.draftMode=i.mode;this.editor();this.directory();};
        editor.querySelector('[data-chat-history]').onchange=e=>{if(e.target.value==='')return;const h=c.history.slice().reverse()[Number(e.target.value)];i.draft=h.content;i.draftMode=h.mode;this.editor();this.directory();};
        this.sync();
    },
    async loadBaseSource() {
        const s=this.state, token=(s.sourceSeq||0)+1;s.sourceSeq=token;s.baseSource=null;
        if((s.selected==='base'||s.selected===`chatbase:${s.chatId}`)&&!s.busy)this.editor();
        try {
            const source=await API.get('/api/assistant/prompts/base/source'+(s.chatId?'?chat_id='+Number(s.chatId):''));
            if(this.state!==s||token!==s.sourceSeq)return;
            s.baseSource=source;
        }catch(e){if(this.state!==s||token!==s.sourceSeq)return;s.baseSource={available:false,notice:e.message};}
        if((s.selected==='base'||s.selected===`chatbase:${s.chatId}`)&&!s.busy)this.editor();
    },
    defaultSourceHTML() {
        const r=this.state.baseSource;
        if(!r)return '<div class="bw-default" role="status"><span class="spinner-border spinner-border-sm"></span><p>正在读取当前聊天 Profile 的基础指令原文…</p></div>';
        if(!r.available)return `<div class="bw-default"><h3>原文读取未完成</h3><p>${this.esc(r.notice)}</p><button class="btn btn-outline-secondary" data-base-refresh>重新读取</button></div>`;
        return `<div class="bw-source-meta"><strong>${this.esc(r.profile_id)} · ${this.esc(r.model)}</strong><small>来源：${this.esc(r.source)}<br>内容指纹：${this.esc(r.fingerprint?.slice(0,16))} · 更新：${this.esc(r.modified_at)}</small><p>${this.esc(r.notice)}</p><div><button class="btn btn-sm btn-outline-secondary" data-base-refresh>刷新原文</button> <button class="btn btn-sm btn-primary" data-base-copy>以此原文开始自定义</button></div></div><label class="bw-text-label" for="bwBaseOriginal">基础指令原文<span>只读 · 修改请创建自定义草稿</span></label><textarea id="bwBaseOriginal" class="bw-text" readonly>${this.esc(r.content)}</textarea>${r.persistent_instructions?`<details class="bw-history"><summary>同模型的持久指令原文（独立来源）</summary><pre class="bw-original-extra">${this.esc(r.persistent_instructions)}</pre></details>`:''}`;
    },
    changed(i) { return i.draft !== i.content || i.draftMode !== i.mode || JSON.stringify(i.outputDraft) !== JSON.stringify(i.outputSaved); },
    dirty() { return this.state?.items.some(i => this.changed(i)); },
    item() { return this.state.items.find(i => i.id === this.state.selected); },
    chat() { return this.state.overview.chats?.find(c => String(c.id) === String(this.state.chatId)); },
    changeChat(chatId) {
        const s=this.state;
        s.chatId=chatId; s.seq++; s.selected='base'; s.scene='reply'; s.notice='';
        s.body.querySelector('[data-scene]').value='reply';
        s.body.querySelector('[data-results]').hidden=true;
        s.body.querySelector('[data-permissions]').href='/chats?chat_id='+Number(chatId);
        this.evidence(''); this.editor(); this.directory(); this.loadBaseSource(); this.loadChatBase(true);
    },
    choose(id) {
        const s=this.state;
        if(s.busy)return;
        if(!s.items.some(i=>i.id===id))return;
        s.selected=id; s.seq++; s.notice='';
        s.scene=this.item().kind==='judge'?'decision':'reply';
        s.body.querySelector('[data-scene]').value=s.scene;
        s.body.querySelector('[data-results]').hidden=true;
        this.directory(); this.editor();
        if(window.matchMedia('(max-width:760px)').matches) s.body.querySelector('[data-sources]').open=false;
    },
    status(text) { if(this.state){this.state.notice=text;const el=this.state.body?.querySelector('[data-status]');if(el)el.textContent=text;} },
    evidence(text) { this.state?.body?.querySelector('[data-evidence]')?.replaceChildren(document.createTextNode(text)); },
    directory() {
        const s = this.state, q = s.body.querySelector('#bwSearch').value.toLowerCase();
        const items = s.items.filter(i => (!i.chatId || String(i.chatId)===String(s.chatId)) && `${i.title} ${i.draft}`.toLowerCase().includes(q));
        const chat=this.chat(), related=new Set([`chatbase:${s.chatId}`,`role:${chat?.role?.id}`,`judge:${chat?.judge?.id}`]);
        const row=i=>`<button type="button" data-item="${this.esc(i.id)}" aria-current="${i.id===s.selected}"><span>${this.esc(i.title)}</span>${this.changed(i)?'<small>草稿</small>':''}</button>`;
        const directory=s.body.querySelector('[data-directory]');
        const expanded=directory.querySelector('details')?.open ?? false;
        directory.innerHTML=items.filter(i=>related.has(i.id)).map(row).join('')+
            `<details ${expanded||q?'open':''}><summary>全部来源</summary>${items.filter(i=>!related.has(i.id)).map(row).join('') || '<small>无其他来源</small>'}</details>`;
        directory.querySelectorAll('[data-item]').forEach(b=>b.onclick=()=>this.choose(b.dataset.item));
    },
    editor() {
        const s = this.state, i = this.item(), esc = x => this.esc(x);
        const editor = s.body.querySelector('[data-editor]');
        if(i.kind==='chatbase'){this.chatBaseEditor(s,i,editor);return;}
        editor.innerHTML = `<div class="bw-editor-heading"><span class="bw-kicker">${esc(i.group)}</span><h2>${esc(i.title)}</h2><div class="bw-scope"><i class="bi bi-broadcast"></i> 保存范围 · ${esc(i.scope)}</div></div>
        ${i.kind === 'base' ? `<div class="bw-mode" role="group" aria-label="基础指令模式"><button data-mode="default" aria-pressed="${i.draftMode === 'default'}">跟随 Codex 默认<small>由运行环境与模型决定</small></button><button data-mode="custom" aria-pressed="${i.draftMode === 'custom'}">自定义基础指令<small>使用你维护的完整内容</small></button></div>` : ''}
        <div data-edit-area>${i.kind === 'base' && i.draftMode === 'default' ? `${this.defaultSourceHTML() }` : `<label class="bw-text-label" for="bwText">${i.kind === 'base' ? '完整基础指令' : '指令内容'}<span>Markdown</span></label><textarea id="bwText" class="bw-text" spellcheck="false" maxlength="${i.kind === 'base' ? 100000 : 30000}">${esc(i.draft)}</textarea>`}</div>
        ${i.kind === 'role' ? this.outputEditor(i) : i.kind==='judge'?this.judgeEditor(i):''}<div class="bw-status" data-status role="status"></div><div class="bw-editor-footer"><span data-count></span><div><button class="btn btn-outline-secondary" data-undo>撤销修改</button><button class="btn btn-primary" data-save>保存修改</button></div></div>
        <details class="bw-history"><summary>版本与恢复</summary>${i.kind === 'rule' ? '<button class="btn btn-sm btn-outline-secondary" data-default>将默认规则填入草稿</button>' : ''}${i.kind === 'base' ? `<label>恢复历史版本到草稿<select class="form-select" data-history><option value="">选择版本…</option>${s.base.history.slice().reverse().map((h,n) => `<option value="${n}">v${h.version} · ${h.mode === 'custom' ? '自定义' : '跟随默认'}</option>`).join('')}</select></label>` : '<p>真实请求保留当次采用的内容，可在右侧查看。此处只保存修改。</p>'}</details>`;
        editor.querySelector('[data-base-refresh]')?.addEventListener('click',()=>this.loadBaseSource());
        editor.querySelector('[data-base-copy]')?.addEventListener('click',()=>{i.draft=s.baseSource.content;i.draftMode='custom';this.editor();this.directory();});
        editor.querySelector('#bwText')?.addEventListener('input', e => {i.draft = e.target.value;this.sync();this.directory();});
        editor.querySelectorAll('[data-mode]').forEach(b => b.onclick = () => {i.draftMode = b.dataset.mode;this.editor();this.directory();});
        editor.querySelector('[data-undo]').onclick = () => {i.draft = i.content;i.draftMode = i.mode;if(i.outputSaved)i.outputDraft={...i.outputSaved};this.editor();this.directory();};
        editor.querySelector('[data-save]').onclick = () => this.save();
        editor.querySelector('[data-default]')?.addEventListener('click', () => {i.draft = i.defaultText;this.editor();this.directory();});
        editor.querySelector('[data-history]')?.addEventListener('change', e => {if(e.target.value==='')return;const h=s.base.history.slice().reverse()[Number(e.target.value)];i.draft=h.content;i.draftMode=h.mode;this.editor();this.directory();});
        editor.querySelectorAll('[data-output]').forEach(e => e.oninput = () => {i.outputDraft[e.dataset.output] = e.type==='checkbox'?e.checked:Number(e.value);this.sync();this.directory();});
        this.sync();
    },
    output(row) { return {output_split_enabled:!!row.output_split_enabled,output_max_count:row.output_max_count??3,output_max_chars:row.output_max_chars??120,output_interval_seconds:row.output_interval_seconds??1,output_strip_trailing_period:row.output_strip_trailing_period!==false}; },
    outputEditor(i) { const o=i.outputDraft;return `<details class="bw-history"><summary>回复方式</summary><div class="bw-output"><label><input type="checkbox" data-output="output_split_enabled" ${o.output_split_enabled?'checked':''}> 分条发送</label><label><input type="checkbox" data-output="output_strip_trailing_period" ${o.output_strip_trailing_period?'checked':''}> 去掉末尾句号</label>${[['output_max_count','最多条数',1,10,1],['output_max_chars','每条建议字数',10,2000,1],['output_interval_seconds','发送间隔（秒）',0,10,0.1]].map(([k,t,min,max,step])=>`<label>${t}<input class="form-control" type="number" data-output="${k}" value="${o[k]}" min="${min}" max="${max}" step="${step}"></label>`).join('')}</div></details>`; },
    judgeEditor(i) {
        const fields=[['trigger_msg_threshold','新增消息（条）',1000],['trigger_interval_minutes','回复间隔（分）',1440],
            ['cooldown_msg_threshold','冷却后新增消息（条）',1000],['cooldown_minutes','冷却时长（分）',1440]];
        return `<details class="bw-history"><summary>触发与冷却</summary><div class="bw-output">${fields.map(([key,label,max])=>
            `<label>${label}<input class="form-control" data-output="${key}" type="number" min="0" max="${max}" required value="${i.outputDraft[key]}"></label>`).join('')}</div></details>`;
    },
    sync() {
        const s = this.state, i = this.item(), dirty = this.changed(i);
        s.quick?.();
        const previewNote=s.body.querySelector('[data-preview-note]');
        if(previewNote)previewNote.hidden=s.previewKey===JSON.stringify([this.request(false),this.request(true)]);
        s.body.querySelector('[data-status]').textContent=s.notice||'';
        s.body.querySelector('[data-count]').textContent = [i.draft.length ? i.draft.length.toLocaleString()+' 字符' : '',dirty?'未保存草稿':'已保存配置'].filter(Boolean).join(' · ');
        s.body.querySelector('[data-save]').disabled = s.busy || !dirty || (!((i.kind === 'base' && i.draftMode === 'default') || (i.kind==='chatbase' && i.draftMode!=='custom')) && !i.draft.trim());
        s.body.querySelector('[data-undo]').disabled = s.busy || !dirty;
    },
    async save() {
        const s = this.state, i = this.item(); if(s.busy)return;
        if([...s.body.querySelectorAll('[data-output]')].some(e=>!e.reportValidity()))return;
        s.busy = true; AssistantStudio.state.select.disabled=true; this.sync();this.status('正在保存修改…');
        s.body.querySelectorAll('.bw-editor textarea, .bw-editor select, .bw-editor input, [data-mode], [data-default]').forEach(e=>e.disabled=true);
        try {
            let result;
            if(i.kind === 'chatbase'){result=await API.put(`/api/assistant/prompts/chats/${i.chatId}/base`,{version:i.config.version,mode:i.draftMode,content:i.draft});i.config={...i.config,...result};i.mode=i.draftMode=result.mode;i.content=i.draft=result.content;i.needsRefresh=true;}
            else if(i.kind === 'base') {result=await API.put('/api/assistant/prompts/base',{version:s.base.version,content:i.draftMode==='default'?null:i.draft});s.base=result;i.content=i.draft=result.content;i.mode=i.draftMode=result.mode;}
            else if(i.kind === 'rule') {result=await API.put('/api/assistant/prompts/rules',{version:s.rules.version,changes:{[i.key]:i.draft}});s.rules.version=result.version;i.content=i.draft=result.texts[i.key];}
            else {result=await (i.kind==='role'?API.roles:API.judges).update(i.key,{prompt:i.draft,revision:i.revision,...(i.outputDraft||{})});i.revision=result.revision;i.content=i.draft=result.prompt ?? i.draft;if(i.outputDraft)i.outputSaved={...i.outputDraft};}
            this.status(result.cache_refreshed === false ? '已保存；助手下次启动时加载。' : result.message || '已保存');
            this.evidence('配置已保存，尚未核验后续请求。点击「查看最近真实请求」检查实际采用的内容。');
        } catch(e) {this.status(`保存失败：${e.message}。草稿仍保留；版本冲突时请复制草稿后重新打开。`);}
        finally {s.busy=false;if(this.state===s){AssistantStudio.state.select.disabled=!s.overview.chats?.length;this.editor();this.directory();if(i.needsRefresh||i.kind==='role'||i.kind==='base'){i.needsRefresh=false;this.loadChatBase(false,true);}}}
    },
    request(draft, scene=this.state.scene) {
        const s=this.state, chat=this.chat();
        const role=s.items.find(i=>i.id===`role:${chat?.role?.id}`);
        const judge=s.items.find(i=>i.id===`judge:${chat?.judge?.id}`);
        const cb=s.items.find(i=>i.id===`chatbase:${s.chatId}`);
        const target=scene==='reply'?role:judge;
        return {...(cb?{chat_base_mode:cb[draft?'draftMode':'mode'],chat_base_content:cb[draft?'draft':'content']}:{}),scene, chat_id:s.chatId?Number(s.chatId):null, role_id:role?.key, judge_id:judge?.key,
            draft:target?{prompt:target[draft?'draft':'content'],...target[draft?'outputDraft':'outputSaved']}:{},
            rule_draft:Object.fromEntries(s.items.filter(i=>i.kind==='rule').map(i=>[i.key,i[draft?'draft':'content']])),
            history:'',use_chat_history:!!s.chatId,...(draft?{global_base_mode:s.items.find(i=>i.id==='base').draftMode,global_base_content:s.items.find(i=>i.id==='base').draft}:{}),content:'你好，聊聊最近的游戏？'};
    },
    blocks(result, draft) {
        const i=this.state.items[0];
        const mode=draft?i.draftMode:i.mode;
        return [{id:'base',title:'Codex 基础指令',role:'baseInstructions',content:mode==='custom'?i[draft?'draft':'content']:(this.state.baseSource?.available?this.state.baseSource.content:'默认原文未读取：'+(this.state.baseSource?.notice||'正在读取'))},...result.blocks];
    },
    blockHTML(b) {
        const role={system:'system · Codex 主回复转为 developer',developer:'应用指令 → developerInstructions',user:'用户输入 / 资料',schema:'结构约束',reference:'运行时参考'}[b.role]||b.role;
        const chat=this.chat(), roleItem=this.state.items.find(i=>i.id===`role:${chat?.role?.id}`);
        const source=b.id==='base'?(this.state.items.some(i=>i.id===`chatbase:${this.state.chatId}`)?`chatbase:${this.state.chatId}`:'base'):b.editable==='system'?`rule:${b.id}`:['role','output'].includes(b.editable)?roleItem?.id:b.editable==='judge'?`judge:${chat?.judge?.id}`:null;
        return `<details class="bw-block"><summary>${this.esc(b.title)}<small>${this.esc(role)}${b.source?' · '+this.esc(b.source):''}</small></summary>${source?`<button class="btn btn-sm btn-outline-secondary bw-source" data-source-id="${this.esc(source)}">编辑来源 ↗</button>`:''}<pre tabindex="0">${this.esc(b.content)}</pre></details>`;
    },
    hasReplyDraft() {
        const s=this.state;
        return s.items.some(i=>this.changed(i) && (i.kind==='base'||i.kind==='rule'||
            i.id===`role:${this.chat()?.role?.id}` || i.id===`chatbase:${s.chatId}`));
    },
    checkContext() {
        const i=this.item(), chat=this.chat(), s=this.state;
        const mode=['base','chatbase'].includes(i.kind)?'preview':i.kind==='judge'?'decision':'compare';
        const mismatch=['role','judge'].includes(i.kind) && chat?.[i.kind]?.id!==i.key;
        return {mode, item:i.id, request:this.request(true,mode==='decision'?'decision':'reply'),
            revision:[s.base.version,s.rules.version,s.items.find(x=>x.id===`chatbase:${s.chatId}`)?.config.version,
                s.items.find(x=>x.id===`role:${chat?.role?.id}`)?.revision,s.items.find(x=>x.id===`judge:${chat?.judge?.id}`)?.revision],
            label:[chat?.chat_name, mode==='decision'?chat?.judge?.display_name:chat?.role?.display_name].filter(Boolean).join(' · '),
            block:mode!=='preview' && (!chat?'请先选择聊天':mismatch?'请选择使用此'+(i.kind==='judge'?'判断器':'角色')+'的聊天':''),
            preview:()=>this.preview()};
    },
    async preview() {
        const s=this.state, seq=++s.seq; this.status('正在组装已保存配置与草稿…');
        try {
            const baseBlocks=s.scene==='reply'?[this.blocks({blocks:[]},false)[0],this.blocks({blocks:[]},true)[0]]:[];
            const requests=[this.request(false),this.request(true)];
            const results=await Promise.all(requests.map(body=>API.post('/api/assistant/prompts/preview',body)));
            if(this.state!==s||seq!==s.seq)return;
            const panel=s.body.querySelector('[data-results]');panel.hidden=false;
            s.previewKey=JSON.stringify(requests);
            const versions=results.map((r,n)=>[...(r.blocks.some(b=>b.id==='base')?[]:[baseBlocks[n]]),...r.blocks].filter(Boolean));
            const ids=[...new Set(versions.flat().map(b=>b.id))];
            const differences=ids.filter(id=>versions[0].find(b=>b.id===id)?.content!==versions[1].find(b=>b.id===id)?.content);
            panel.innerHTML=`<header><h3>请求结构对比</h3><small data-preview-note hidden>配置已变更，可重新预览</small><p>${differences.length?'变更 '+differences.length+' 项':'结构一致'}</p></header><div class="bw-compare">
                ${versions.map((blocks,n)=>`<details ${n?'open':''}><summary>${n?'当前草稿':'已保存配置'}</summary>${blocks.map(b=>this.blockHTML({...b,title:b.title+(differences.includes(b.id)?' · 已修改':'')})).join('')}</details>`).join('')}</div>`;
            panel.querySelectorAll('[data-source-id]').forEach(b=>b.onclick=()=>{this.choose(b.dataset.sourceId);s.body.querySelector('[data-editor]').scrollIntoView({behavior:'smooth',block:'start'});});
            this.sync();this.status('');panel.scrollIntoView({behavior:'smooth',block:'start'});
        } catch(e) {if(this.state===s&&seq===s.seq)this.status(`预览失败：${e.message}`);}
    },
    async snapshots() {
        const s=this.state, seq=++s.seq;if(!s.chatId){this.status('请先配置并选择一个聊天。');return;}
        this.status('正在读取真实请求…');
        try {
            const list=await API.get(`/api/assistant/prompts/chats/${s.chatId}/snapshots`);
            if(this.state!==s||seq!==s.seq)return;
            const rows=list.items.filter(i=>i.scene===s.scene);
            if(!rows.length){this.evidence('此聊天在当前场景下还没有请求快照，尚无法确认实际采用版本。');this.status('暂无可用快照。');return;}
            const panel=s.body.querySelector('[data-results]');panel.hidden=false;
            panel.innerHTML=`<header><h3>真实请求记录</h3><label>请求时间<select class="form-select" data-snapshot-select>${rows.map(r=>`<option value="${this.esc(r.id)}">${new Date(r.created*1000).toLocaleString()}</option>`).join('')}</select></label></header><div data-snapshot-content></div>`;
            const load=async id=>{
                const token=++s.seq;
                try {
                    const p=await API.get(`/api/assistant/prompts/chats/${s.chatId}/snapshots/${encodeURIComponent(id)}`);
                    if(this.state!==s||s.seq!==token)return;
                    const base=p.base_instructions;
                    this.evidence(base ? `请求提交版本：基础指令 v${base.version} · ${base.fingerprint===(s.items.find(i=>i.id===`chatbase:${s.chatId}`)?.config.composition?.fingerprint||s.base.fingerprint)?'与已保存配置一致':'与当前配置不同或尚未核验'}。此记录证明发送内容，不代表模型逐条遵循。` : '此快照未记录基础指令，无法确认基础层是否生效。');
                    const blocks=[{title:'基础指令',role:'baseInstructions',content:['custom','persona'].includes(base?.mode)?base.content:base?'跟随运行环境默认；默认原文不可观测':'此快照未记录'}, {title:'实际应用指令',role:'developer',content:p.developer_instructions || '此场景未记录该字段'}, {title:'完整请求快照',role:'记录',content:JSON.stringify(p,null,2)}];
                    panel.querySelector('[data-snapshot-content]').innerHTML=`<p class="bw-muted">模型：${this.esc(p.model||'未记录')} · Profile：${this.esc(p.runtime_profile||'未记录')} · ${this.esc(p.notice||'')}</p>${blocks.map(b=>this.blockHTML(b)).join('')}`;
                    this.status('已读取真实请求。快照仅包含宿主可观测内容。');
                }catch(e){if(this.state===s&&s.seq===token)this.status(`快照读取失败：${e.message}`);}
            };
            panel.querySelector('[data-snapshot-select]').onchange=e=>load(e.target.value);
            await load(rows[0].id);panel.scrollIntoView({behavior:'smooth',block:'start'});
        }catch(e){if(this.state===s)this.status(`读取失败：${e.message}`);}
    }
};
