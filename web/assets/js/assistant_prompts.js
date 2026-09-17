/* Prompt editing and inspection share one workbench; previews never save drafts. */
const PromptWorkbench = {
    serial: 0,
    escape(value) { return UI.escapeHtml(String(value ?? '')); },
    field(name, label, value, help = '', attrs = '') {
        return `<label class="prompt-field"><span>${label}</span><input class="form-control" name="${name}" value="${this.escape(value)}" ${attrs}>${help ? `<small>${help}</small>` : ''}</label>`;
    },
    render(kind, item = {}, create = false) {
        const decision = kind === 'judge';
        const title = decision ? '判断规则' : '角色设定';
        const fields = decision
            ? this.field('trigger_msg_threshold','至少积累消息数',item.trigger_msg_threshold ?? 5,'0 表示不限制消息数','type="number" min="0" max="1000"') +
              this.field('trigger_interval_minutes','距上次回复至少（分钟）',item.trigger_interval_minutes ?? 1,'没有上次回复时不受此项限制','type="number" min="0" max="1440"') +
              this.field('cooldown_msg_threshold','不接话后至少新增消息数',item.cooldown_msg_threshold ?? 5,'0 表示不限制消息数','type="number" min="0" max="1000"') +
              this.field('cooldown_minutes','不接话后至少等待（分钟）',item.cooldown_minutes ?? 1,'0 表示不限制等待时间','type="number" min="0" max="1440"')
            : `<label class="prompt-toggle"><input type="checkbox" name="output_split_enabled" ${item.output_split_enabled ? 'checked' : ''}>分条发送</label><p class="prompt-note">关闭时发送一条完整消息。</p><div class="prompt-fields" data-split-options>` +
              this.field('output_max_chars','每条建议字数',item.output_max_chars ?? 120,'建议目标，不截断句子','type="number" min="10" max="2000"') +
              this.field('output_max_count','最多发送条数',item.output_max_count ?? 3,'内容简短时仍只发一条','type="number" min="1" max="10"') +
              this.field('output_interval_seconds','条间发送间隔（秒）',item.output_interval_seconds ?? 1,'仅控制发送节奏','type="number" min="0" max="10" step="0.1"') +
              `</div><label class="prompt-toggle"><input type="checkbox" name="output_strip_trailing_period" ${item.output_strip_trailing_period !== false ? 'checked' : ''}>去掉末尾句号</label>`;
        return `<form id="${decision ? 'judgeForm' : 'roleForm'}" class="prompt-workbench" data-kind="${kind}">
            <input type="hidden" name="id" value="${this.escape(item.id || '')}"><input type="hidden" name="revision" value="${this.escape(item.revision || '')}">
            <nav class="prompt-tabs" aria-label="编辑分区" role="tablist">
                ${[['identity',title],['behavior',decision?'触发时机':'回复方式'],['preview',decision?'预览与试判':'提示词预览']].map(([key,label],i)=>`<button type="button" role="tab" id="prompt-tab-${key}" aria-controls="prompt-panel-${key}" aria-selected="${i===0}" data-prompt-tab="${key}" class="${i===0?'active':''}">${label}</button>`).join('')}
            </nav>
            <section id="prompt-panel-identity" role="tabpanel" aria-labelledby="prompt-tab-identity" data-prompt-panel="identity">
                ${this.field('display_name','显示名称',item.display_name || '', '', 'required maxlength="160"')}
                ${this.field('description','简要描述',item.description || '')}
                <label class="prompt-field"><span>${title}</span><textarea name="prompt" class="form-control prompt-editor" required maxlength="30000" placeholder="${decision?'哪些情况值得接话？哪些情况保持安静？':'描述身份、说话方式和偏好。'}">${this.escape(item.prompt||'')}</textarea></label>
                <details class="prompt-block"><summary>高级信息<span>内部标识</span></summary><div class="prompt-block-body">${this.field('name','内部 ID',item.name||'',create?'留空自动生成，日常无需填写':'用于稳定绑定，创建后不可修改',create?'':'readonly')}</div></details>
            </section>
            <section id="prompt-panel-behavior" role="tabpanel" aria-labelledby="prompt-tab-behavior" data-prompt-panel="behavior" hidden>
                ${decision?'<p class="prompt-note">收到新消息时，消息数和最短间隔必须同时满足才检查。冷却期同样要求两项同时满足；到点不会自动发言。</p><div class="prompt-fields">':''}${fields}${decision?'</div><p class="prompt-note" data-timing-summary></p><p class="prompt-note">这些条件仅适用于主动接话，不影响引用回复和连续追问。</p>':''}
            </section>
            <section id="prompt-panel-preview" role="tabpanel" aria-labelledby="prompt-tab-preview" data-prompt-panel="preview" hidden>
                <p class="prompt-note">预览当前编辑内容，无需保存，也不会调用模型。</p>
                <details class="prompt-block prompt-preview-scenario"><summary>预览场景<span data-preview-summary>模拟聊天</span></summary><div class="prompt-block-body">
                    <label class="prompt-field"><span>聊天记录来源</span><select class="form-select" data-history-source><option value="simulated">模拟聊天</option><option value="recent">真实聊天</option></select></label>
                    <label class="prompt-field"><span data-preview-chat-label>参考聊天（可选）</span><select class="form-select" data-preview-chat><option value="">未指定聊天</option>${(App._assistantChats||[]).map(c=>`<option value="${Number(c.id)}">${this.escape(c.chat_name)}</option>`).join('')}</select></label>
                    <p class="prompt-note" data-recent-history-note hidden>使用所选聊天的近期消息，需要助手正在运行。</p>
                    ${decision?`<label class="prompt-field"><span>参考角色</span><select class="form-select" data-preview-role><option value="">不指定</option>${(App._roles||[]).map(r=>`<option value="${Number(r.id)}">${this.escape(r.display_name)}</option>`).join('')}</select></label>`:''}
                    <label class="prompt-field" data-simulated-history><span>模拟近期聊天</span><textarea class="form-control prompt-sample" data-preview-history placeholder="群友：最近有什么值得玩的游戏？"></textarea></label>
                    <label class="prompt-field"><span>测试消息</span><textarea class="form-control prompt-sample" data-preview-content>你好，聊聊最近的游戏？</textarea></label>
                    ${decision?'<div class="prompt-fields">'+this.field('trial_messages','模拟积累消息数',5,'','type="number" min="0" max="100000"')+this.field('trial_minutes','模拟距上次回复（分钟）',10,'','type="number" min="0" max="100000"')+'</div><label class="prompt-toggle"><input type="checkbox" data-trial-ignore>忽略时机，只测试语义判断</label><label class="prompt-toggle"><input type="checkbox" data-trial-cooldown>模拟处于冷却期</label><div class="prompt-fields">'+this.field('trial_cooldown_messages','冷却后新增消息',5,'','type="number" min="0" max="100000"')+this.field('trial_cooldown_minutes','冷却已过（分钟）',10,'','type="number" min="0" max="100000"')+'</div>':''}
                </div></details>
                <div class="prompt-toolbar"><button type="button" class="btn btn-sm btn-outline-primary" data-preview-run>刷新预览</button>${decision?'<button type="button" class="btn btn-sm btn-outline-primary" data-trial-run>试判（调用模型）</button>':''}<button type="button" class="btn btn-sm btn-outline-secondary" data-snapshots>真实请求快照</button><button type="button" class="btn btn-sm btn-outline-secondary" data-collapse>全部折叠</button></div>
                <p class="prompt-status" role="status" aria-live="polite" data-preview-status>尚未生成预览</p><div data-trial-result></div><div class="prompt-preview" data-preview-blocks></div>
            </section>
        </form>`;
    },
    mount() {
        const form = document.querySelector('.prompt-workbench');
        if (!form) return;
        const state = {form, generation: ++this.serial, request: 0, dirty: false};
        this.active = state;
        form.addEventListener('submit',event=>event.preventDefault());
        form.querySelectorAll('[data-prompt-tab]').forEach(button=>button.onclick=()=>this.tab(state,button.dataset.promptTab));
        form.querySelector('.prompt-tabs').addEventListener('keydown',event=>{
            if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
            const tabs=[...form.querySelectorAll('[data-prompt-tab]')], index=tabs.indexOf(document.activeElement);
            if(index<0)return;
            event.preventDefault();
            const target=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
            tabs[target].focus();tabs[target].click();
        });
        form.querySelector('[data-preview-chat]').addEventListener('change',()=>{
            const selected=(App._assistantChats||[]).find(c=>Number(c.id)===Number(form.querySelector('[data-preview-chat]').value));
            const role=form.querySelector('[data-preview-role]');
            if(role&&selected?.role?.id)role.value=String(selected.role.id);
            this.updatePreviewSource(form);
        });
        form.querySelector('[data-history-source]').addEventListener('change',()=>this.updatePreviewSource(form));
        form.querySelector('[data-preview-run]').onclick=()=>this.preview(state);
        form.querySelector('[data-trial-run]')?.addEventListener('click',()=>this.preview(state,true));
        form.querySelector('[data-snapshots]').onclick=()=>this.snapshots(state);
        form.querySelector('[data-collapse]').onclick=()=>form.querySelectorAll('[data-preview-blocks] details').forEach(d=>d.open=false);
        form.addEventListener('input',()=>{state.dirty=true;this.status(state,'草稿已更改，刷新预览后查看最新内容');this.updateBehavior(form);});
        this.updateBehavior(form);
        this.updatePreviewSource(form);
        const modal = document.getElementById('configModal');
        modal.addEventListener('hide.bs.modal',()=>{if(this.active===state)this.active=null;}, {once:true});
    },
    updatePreviewSource(form) {
        const recent=form.querySelector('[data-history-source]').value==='recent';
        const chat=form.querySelector('[data-preview-chat]');
        form.querySelector('[data-simulated-history]').hidden=recent;
        form.querySelector('[data-preview-history]').disabled=recent;
        form.querySelector('[data-recent-history-note]').hidden=!recent;
        form.querySelector('[data-preview-chat-label]').textContent=recent?'聊天':'参考聊天（可选）';
        chat.options[0].textContent=recent?'请选择聊天':'未指定聊天';
        form.querySelector('[data-preview-summary]').textContent=recent
            ? (chat.value?`近期消息 · ${chat.selectedOptions[0].textContent}`:'真实聊天 · 待选择')
            : '模拟聊天';
    },
    updateBehavior(form) {
        if (form.dataset.kind==='role') {
            const enabled=form.elements.output_split_enabled.checked;
            form.querySelectorAll('[data-split-options] input').forEach(input=>input.disabled=!enabled);
            form.querySelector('[data-split-options]').classList.toggle('prompt-inactive',!enabled);
        } else {
            form.querySelector('[data-timing-summary]').textContent=`收到新消息时，上次回复后至少积累 ${form.elements.trigger_msg_threshold.value||0} 条消息，且已过去 ${form.elements.trigger_interval_minutes.value||0} 分钟，才考虑主动接话。`;
        }
    },
    tab(state,key,load=true) {
        state.form.querySelectorAll('[data-prompt-tab]').forEach(b=>{b.classList.toggle('active',b.dataset.promptTab===key);b.setAttribute('aria-selected',String(b.dataset.promptTab===key));});
        state.form.querySelectorAll('[data-prompt-panel]').forEach(p=>p.hidden=p.dataset.promptPanel!==key);
        if(key==='preview'&&!state.previewed&&load)this.preview(state);
    },
    status(state,text) { state.form.querySelector('[data-preview-status]').textContent=text; },
    draft(form) {
        const f=form.elements;
        const data={display_name:f.display_name.value,description:f.description.value,prompt:f.prompt.value};
        if (f.revision?.value)data.revision=f.revision.value;
        if(form.dataset.kind==='role') Object.assign(data,{output_split_enabled:f.output_split_enabled.checked,output_max_chars:Number(f.output_max_chars.value),output_max_count:Number(f.output_max_count.value),output_strip_trailing_period:f.output_strip_trailing_period.checked,output_interval_seconds:Number(f.output_interval_seconds.value)});
        else ['trigger_msg_threshold','trigger_interval_minutes','cooldown_msg_threshold','cooldown_minutes'].forEach(k=>data[k]=Number(f[k].value));
        return data;
    },
    request(state) {
        const f=state.form, decision=f.dataset.kind==='judge';
        const recent=f.querySelector('[data-history-source]').value==='recent';
        return {scene:decision?'decision':'reply',draft:this.draft(f),role_id:decision?Number(f.querySelector('[data-preview-role]').value)||null:Number(f.elements.id.value)||null,judge_id:decision?Number(f.elements.id.value)||null:null,chat_id:Number(f.querySelector('[data-preview-chat]').value)||null,use_chat_history:recent,history:recent?'':f.querySelector('[data-preview-history]').value,content:f.querySelector('[data-preview-content]').value,...(decision?{message_count:Number(f.elements.trial_messages.value),elapsed_minutes:Number(f.elements.trial_minutes.value),ignore_timing:f.querySelector('[data-trial-ignore]').checked,in_cooldown:f.querySelector('[data-trial-cooldown]').checked,cooldown_message_count:Number(f.elements.trial_cooldown_messages.value),cooldown_elapsed_minutes:Number(f.elements.trial_cooldown_minutes.value)}:{})};
    },
    valid(state, token) { return this.active===state && state.form.isConnected && state.request===token; },
    revealInvalid(form) {
        const field=form.querySelector(':invalid');
        const panel=field?.closest('[data-prompt-panel]');
        if(panel&&this.active?.form===form)this.tab(this.active,panel.dataset.promptPanel,false);
        field?.closest('details')?.setAttribute('open','');
        form.reportValidity();
    },
    async preview(state, trial=false) {
        const f=state.form;
        if(f.querySelector('[data-history-source]').value==='recent'&&!f.querySelector('[data-preview-chat]').value){
            this.status(state,'请选择要使用近期消息的聊天');
            f.querySelector('.prompt-preview-scenario').open=true;
            f.querySelector('[data-preview-chat]').focus();
            return;
        }
        if(!f.checkValidity()){this.revealInvalid(f);this.status(state,'请先填写有效的名称、规则和参数');return;}
        const token=++state.request;
        const controls=f.querySelectorAll('[data-preview-run],[data-trial-run],[data-snapshots]');
        controls.forEach(b=>b.disabled=true); this.status(state,trial?'正在试判，不会发送微信…':'正在组装当前草稿…');
        try {
            const body=this.request(state), signature=JSON.stringify(body);
            const result=await API.post(`/api/assistant/prompts/${trial?'trial':'preview'}`,body);
            if(!this.valid(state,token))return;
            this.renderBlocks(f.querySelector('[data-preview-blocks]'),result.blocks,state);
            state.previewed=true;
            state.dirty=JSON.stringify(this.request(state))!==signature;
            this.status(state,state.dirty?'请求期间草稿发生变化，请刷新预览':`草稿预览 · 系统规则 v${result.version} · ${result.notes.join(' ')}`);
            const output=f.querySelector('[data-trial-result]');output.replaceChildren();
            if(result.result){const r=result.result;output.innerHTML=`<div class="prompt-trial-result" role="status"><strong>${r.state==='skipped'?'未到判断时机':r.should_reply?'建议接话':'建议不接话'}</strong><p>${this.escape(r.reason)}</p>${r.atmosphere?`<small>当前气氛：${this.escape(r.atmosphere)}</small>`:''}</div>`;}
        } catch(e) { if(this.valid(state,token))this.status(state,`${trial?'试判':'预览'}失败：${e.message}。草稿已保留，可重试。`); }
        finally {if(this.valid(state,token))controls.forEach(b=>b.disabled=false);}
    },
    renderBlocks(container,blocks,state) {
        container.innerHTML=blocks.map((b,i)=>`<details class="prompt-block"><summary><span>${this.escape(b.title)}</span><span class="prompt-block-meta">${this.escape(b.source||'系统内置')} · ${this.escape(b.scope||'本次请求')} · ${b.content.length.toLocaleString()} 字</span></summary><div class="prompt-block-body"><div class="prompt-toolbar"><span class="prompt-note">${this.escape(({system:'系统指令',developer:'系统指令',user:'输入资料',schema:'固定结构',reference:'运行时参考'})[b.role]||'规则')}</span><button type="button" class="btn btn-sm btn-outline-secondary" data-copy="${i}">复制</button>${b.editable?`<button type="button" class="btn btn-sm btn-outline-primary" data-edit="${i}">${b.editable==='system'?'编辑系统规则':b.editable==='permissions'?'编辑来源（新页）':'编辑来源'}</button>`:''}</div><pre class="prompt-scroll" tabindex="0" aria-label="${this.escape(b.title)}完整内容">${this.escape(b.content)}</pre><div data-block-editor></div></div></details>`).join('');
        container.querySelectorAll('[data-copy]').forEach(button=>button.onclick=()=>this.copy(blocks[Number(button.dataset.copy)].content,button));
        container.querySelectorAll('[data-edit]').forEach(button=>button.onclick=async()=>{
            const b=blocks[Number(button.dataset.edit)];
            if(b.editable==='system'){await this.editRule(button.closest('details').querySelector('[data-block-editor]'),b.id,state);return;}
            if(b.editable==='permissions'){const id=Number(state?.form.querySelector('[data-preview-chat]').value);if(id)window.open(`/chats?chat_id=${id}`,'_blank','noopener');return;}
            this.tab(state,b.editable==='output'?'behavior':'identity');
            state.form.elements[b.editable==='output'?'output_split_enabled':'prompt']?.focus();
        });
    },
    async copy(text,button) {
        try {
            try {
                if(!navigator.clipboard?.writeText)throw Error('clipboard unavailable');
                await navigator.clipboard.writeText(text);
            } catch {
                // LAN HTTP pages lack Clipboard API. Keep focus inside the active modal.
                const buffer=document.createElement('textarea');
                buffer.className='prompt-copy-buffer';buffer.value=text;buffer.setAttribute('aria-label','复制内容');
                const focused=document.activeElement;
                (button.closest('.modal')||document.body).append(buffer);
                try {buffer.select();if(!document.execCommand('copy'))throw Error('copy failed');}
                finally {buffer.remove();focused?.focus({preventScroll:true});}
            }
            button.textContent='已复制';
        } catch {button.textContent='复制失败，请选中文本复制';}
        setTimeout(()=>{if(button.isConnected)button.textContent='复制';},2000);
    },
    async editRule(container,key,state) {
        container.textContent='正在读取系统规则…';
        try {
            const data=await API.get('/api/assistant/prompts/rules');
            if(!container.isConnected)return;
            const rule=data.blocks.find(b=>b.id===key);
            container.innerHTML=`<div class="prompt-rule-editor"><p class="prompt-note">影响范围：${this.escape(rule.scope)}的所有请求。这里保存的是系统规则，不会保存角色草稿。</p><textarea class="form-control prompt-editor" maxlength="30000">${this.escape(rule.content)}</textarea><details class="prompt-block"><summary>与系统默认值比较</summary><div class="prompt-block-body"><pre class="prompt-scroll" tabindex="0">${this.escape(rule.text)}</pre></div></details><div class="prompt-toolbar"><button type="button" class="btn btn-sm btn-primary" data-rule-save>保存规则</button><button type="button" class="btn btn-sm btn-outline-secondary" data-rule-reset>填入默认值</button><button type="button" class="btn btn-sm btn-outline-secondary" data-rule-preview>预览此草稿</button><button type="button" class="btn btn-sm btn-outline-secondary" data-rule-cancel>取消</button></div><p role="status" data-rule-status></p><details class="prompt-block"><summary>恢复历史版本<span>先填入，保存后生效</span></summary><div class="prompt-block-body"><select class="form-select" data-rule-history><option value="">选择历史版本</option>${data.history.slice().reverse().map((h,i)=>`<option value="${i}">v${h.version} · ${this.escape(h.at)}</option>`).join('')}</select></div></details><div data-rule-preview-result></div></div>`;
            const area=container.querySelector('textarea');area.focus();
            container.querySelector('[data-rule-reset]').onclick=()=>{area.value=rule.text;container.querySelector('[data-rule-status]').textContent='已填入默认值，保存后生效';};
            container.querySelector('[data-rule-history]').onchange=event=>{
                if(event.target.value==='')return;
                const past=data.history.slice().reverse()[Number(event.target.value)];
                area.value=past.overrides[key] ?? rule.text;
                container.querySelector('[data-rule-status]').textContent=`已填入 v${past.version}，保存后生效`;
            };
            container.querySelector('[data-rule-preview]').onclick=async(event)=>{
                const button=event.currentTarget;button.disabled=true;
                const target=container.querySelector('[data-rule-preview-result]');
                try {
                    const body=state?.form?this.request(state):{scene:key==='decision'?'decision':'reply',draft:{prompt:'通用助手'}};
                    body.rule_draft={[key]:area.value};
                    const result=await API.post('/api/assistant/prompts/preview',body);
                    if(!target.isConnected)return;
                    if(['quoted','followup'].includes(key))this.renderBlocks(target,[{id:key,title:rule.title,content:area.value,source:'未保存系统规则草稿'}],null);
                    else this.renderBlocks(target,result.blocks.map(b=>({...b,editable:null})),null);
                    container.querySelector('[data-rule-status]').textContent='未保存规则预览，不调用模型；展开下方模块查看';
                }catch(e){container.querySelector('[data-rule-status]').textContent=`预览失败：${e.message}`;}
                finally{button.disabled=false;}
            };
            container.querySelector('[data-rule-cancel]').onclick=()=>container.replaceChildren();
            container.querySelector('[data-rule-save]').onclick=async(event)=>{
                const button=event.currentTarget;button.disabled=true;
                try {const result=await API.put('/api/assistant/prompts/rules',{version:data.version,changes:{[key]:area.value}});data.version=result.version;container.querySelector('[data-rule-status]').textContent=result.message;if(state?.form)this.status(state,'系统规则已保存；刷新预览以查看新版本');}
                catch(e){container.querySelector('[data-rule-status]').textContent=`保存失败：${e.message}；编辑内容已保留`;}
                finally{button.disabled=false;}
            };
        }catch(e){container.textContent=`读取失败：${e.message}`;}
    },
    async systemRules() {
        const modal=document.getElementById('configModal');
        document.getElementById('configModalTitle').textContent='系统规则';
        document.getElementById('configModalSaveBtn').classList.add('d-none');
        const body=document.getElementById('configModalBody');
        body.innerHTML='<div class="prompt-workbench"><p class="prompt-note">系统规则影响所有相关请求。展开模块查看内容并编辑；固定协议和聊天权限在请求预览中查看。</p><div data-system-rules>正在读取…</div></div>';
        bootstrap.Modal.getOrCreateInstance(modal).show();
        const target=body.querySelector('[data-system-rules]');
        try {
            const data=await API.get('/api/assistant/prompts/rules');
            if(!target.isConnected)return;
            this.renderBlocks(target,data.blocks.map(b=>({...b,editable:'system',source:b.overridden?'自定义系统规则':'系统默认',role:'system'})),null);
            const versions=document.createElement('details');versions.className='prompt-block';
            versions.innerHTML=`<summary>修改记录<span>当前 v${data.version} · 保留最近 20 次</span></summary><div class="prompt-block-body"><div class="prompt-scroll" tabindex="0">${data.history.slice().reverse().map(h=>`<details class="prompt-block"><summary>v${h.version} · ${this.escape(h.at)}</summary><div class="prompt-block-body"><pre class="prompt-scroll" tabindex="0">${this.escape(JSON.stringify(h.overrides,null,2))}</pre></div></details>`).join('')||'尚无修改记录'}</div></div>`;
            target.append(versions);
        }catch(e){target.textContent=`读取失败：${e.message}，请关闭后重试。`;}
    },
    async snapshots(state) {
        const f=state.form, chatId=Number(f.querySelector('[data-preview-chat]').value),target=f.querySelector('[data-preview-blocks]');
        if(!chatId){this.status(state,'请在“预览场景”中选择要查看的聊天');f.querySelector('.prompt-preview-scenario').open=true;f.querySelector('[data-preview-chat]').focus();return;}
        const token=++state.request;
        this.status(state,'正在读取所选聊天快照…');
        try {
            const data=await API.get(`/api/assistant/prompts/chats/${chatId}/snapshots`);
            if(!this.valid(state,token))return;
            f.querySelector('[data-trial-result]').replaceChildren();
            this.status(state,'真实请求快照 · 每聊天最近 20 次，最长 7 天 · 与当前未保存草稿独立');
            target.innerHTML=data.items.map((item,i)=>`<details class="prompt-block"><summary>${this.escape(new Date(item.created*1000).toLocaleString())}<span>${this.escape(({reply:'主回复',judge:'主动接话',followup_judge:'引用/连续追问'})[item.scene]||item.scene)}</span></summary><div class="prompt-block-body" data-snapshot="${i}">展开后读取</div></details>`).join('')||'<p class="prompt-note">暂无快照。启用此版本后实际发生的请求才会记录。</p>';
            target.querySelectorAll('details').forEach(details=>details.addEventListener('toggle',async()=>{
                const content=details.querySelector('[data-snapshot]');
                if(!details.open||content.dataset.loaded)return;
                content.dataset.loaded='loading';content.textContent='正在读取…';
                try {const payload=await API.get(`/api/assistant/prompts/chats/${chatId}/snapshots/${data.items[Number(content.dataset.snapshot)].id}`);if(!content.isConnected)return;
                    this.renderBlocks(content,Object.entries(payload).map(([key,value])=>({id:key,title:({developer_instructions:'线程指令',turn_input:'本轮输入',output_schema:'输出结构',tools:'工具定义',messages:'判断请求',notice:'范围说明',request_id:'请求标识',thread_id:'会话标识',resumed:'复用会话',rotation_reason:'会话更新原因',model:'模型',role:'角色'})[key]||key,content:typeof value==='string'?value:JSON.stringify(value,null,2),source:'真实请求'})),null);
                    content.dataset.loaded='yes';
                }catch(e){content.textContent=`读取失败：${e.message}；折叠后展开可重试`;delete content.dataset.loaded;}
            }));
        }catch(e){if(this.valid(state,token))this.status(state,`快照读取失败：${e.message}`);}
    }
};
window.PromptWorkbench=PromptWorkbench;
