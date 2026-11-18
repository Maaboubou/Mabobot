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
                ${[['identity',title],['behavior',decision?'触发时机':'回复方式']].map(([key,label],i)=>`<button type="button" role="tab" id="prompt-tab-${key}" aria-controls="prompt-panel-${key}" aria-selected="${i===0}" data-prompt-tab="${key}" class="${i===0?'active':''}">${label}</button>`).join('')}
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
            <details class="prompt-test-link"><summary>${decision?'试判接话':'验证回复'}</summary><label>背景聊天<select data-check-chat class="form-select form-select-sm"></select></label><div data-form-check></div></details>
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
        const check=form.querySelector('.prompt-test-link');
        check.addEventListener('toggle',()=>{
            if(!check.open || state.refreshCheck)return;
            const chats=App._assistantChats||[], select=check.querySelector('[data-check-chat]');
            const id=Number(form.elements.id.value),kind=form.dataset.kind;
            select.innerHTML=chats.map(c=>`<option value="${Number(c.id)}">${this.escape(c.chat_name)}</option>`).join('') || '<option>无可用聊天</option>';
            const bound=chats.find(c=>c[kind]?.id===id);
            if(bound)select.value=String(bound.id);
            state.refreshCheck=BehaviorCheck.mount(check.querySelector('[data-form-check]'),()=>{
                const chat=chats.find(c=>c.id===Number(select.value));
                return {mode:kind==='role'?'compare':'decision',item:kind+':'+id,
                    label:chat?'已保存基准 · '+(chat[kind]?.display_name||'默认设置'):'',
                    block:!chat?'请先配置聊天':id && chat[kind]?.id!==id?'请选择绑定此配置的聊天':'',
                    revision:form.elements.revision.value,
                    request:{chat_id:chat?.id,role_id:chat?.role?.id,judge_id:chat?.judge?.id,
                        scene:kind==='role'?'reply':'decision',use_chat_history:true,history:'',draft:this.draft(form)}};
            });
            select.onchange=state.refreshCheck;
        });
        form.addEventListener('input',e=>{if(check.contains(e.target))return;state.dirty=true;this.updateBehavior(form);state.refreshCheck?.();});
        this.updateBehavior(form);
        const modal = document.getElementById('configModal');
        modal.addEventListener('hide.bs.modal',()=>{if(this.active===state)this.active=null;}, {once:true});
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

    },

    draft(form) {
        const f=form.elements;
        const data={display_name:f.display_name.value,description:f.description.value,prompt:f.prompt.value};
        if (f.revision?.value)data.revision=f.revision.value;
        if(form.dataset.kind==='role') Object.assign(data,{output_split_enabled:f.output_split_enabled.checked,output_max_chars:Number(f.output_max_chars.value),output_max_count:Number(f.output_max_count.value),output_strip_trailing_period:f.output_strip_trailing_period.checked,output_interval_seconds:Number(f.output_interval_seconds.value)});
        else ['trigger_msg_threshold','trigger_interval_minutes','cooldown_msg_threshold','cooldown_minutes'].forEach(k=>data[k]=Number(f[k].value));
        return data;
    },
    revealInvalid(form) {
        const field=form.querySelector(':invalid');
        const panel=field?.closest('[data-prompt-panel]');
        if(panel&&this.active?.form===form)this.tab(this.active,panel.dataset.promptPanel,false);
        field?.closest('details')?.setAttribute('open','');
        form.reportValidity();
    },
    renderBlocks(container,blocks,state) {
        container.innerHTML=blocks.map((b,i)=>`<details class="prompt-block"><summary><span>${this.escape(b.title)}</span><span class="prompt-block-meta">${this.escape(b.source||'系统内置')} · ${this.escape(b.scope||'本次请求')} · ${b.content.length.toLocaleString()} 字</span></summary><div class="prompt-block-body"><div class="prompt-toolbar"><span class="prompt-note">${this.escape(({system:'系统指令',developer:'系统指令',user:'输入资料',schema:'固定结构',reference:'运行时参考'})[b.role]||'规则')}</span><button type="button" class="btn btn-sm btn-outline-secondary" data-copy="${i}">复制</button></div><pre class="prompt-scroll" tabindex="0" aria-label="${this.escape(b.title)}完整内容">${this.escape(b.content)}</pre><div data-block-editor></div></div></details>`).join('');
        container.querySelectorAll('[data-copy]').forEach(button=>button.onclick=()=>this.copy(blocks[Number(button.dataset.copy)].content,button));

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
    async lensPanel(target,chats=App._assistantChats||[]) {
        target.textContent='正在读取检索配置…';
        try {
            const data=await API.get('/api/assistant/prompts/lens');
            if(!target.isConnected)return;
            const fields=[['image_timeout_seconds','单张超时（秒）',10,180],['max_images','每轮图片上限',1,4],['max_images_per_call','单次图片上限',1,2],['max_concurrency','全局并发上限',1,2],['cache_ttl_seconds','缓存有效期（秒，0 为禁用）',0,86400]];
            target.innerHTML=`<p class="prompt-note">所有 App Server Profiles 继承此全局配置。保存后新请求生效，进行中的请求保留原配置。图片仅限本轮输入；上传、权限隔离、进程清理和禁止人脸身份识别由程序固定校验。</p><form data-lens-form><label class="form-check"><input class="form-check-input" type="checkbox" name="enabled">启用 Lens 图片检索</label>${fields.map(([key,label,min,max])=>`<label class="prompt-field"><span>${label}</span><input class="form-control" type="number" name="${key}" min="${min}" max="${max}" step="1" required></label>`).join('')}<div class="prompt-toolbar"><button class="btn btn-sm btn-primary" type="submit">保存运行参数</button><button class="btn btn-sm btn-outline-secondary" type="button" data-lens-default>填入默认值</button><button class="btn btn-sm btn-outline-secondary" type="button" data-lens-preview>预览参数草稿</button></div><label class="prompt-field"><span>恢复历史参数（填入后保存生效）</span><select class="form-select" data-lens-history></select></label></form><p role="status" data-lens-message></p><details class="prompt-block"><summary>环境状态与最近错误</summary><div class="prompt-block-body"><button class="btn btn-sm btn-outline-secondary" data-lens-refresh>刷新状态</button><pre class="prompt-scroll" data-lens-status></pre></div></details><label class="prompt-field"><span>检查聊天生效范围与真实快照</span><select class="form-select" data-lens-chat><option value="">请选择聊天</option>${chats.map(c=>`<option value="${Number(c.id)}">${this.escape(c.chat_name)}</option>`).join('')}</select></label><p class="prompt-note" data-lens-scope></p><button class="btn btn-sm btn-outline-secondary" data-lens-snapshots>查看真实请求快照</button><div data-lens-output></div>`;
            const form=target.querySelector('form'), message=target.querySelector('[data-lens-message]'), output=target.querySelector('[data-lens-output]');
            const fill=values=>{form.elements.enabled.checked=values.enabled;fields.forEach(([key])=>form.elements[key].value=values[key]);};
            const draft=()=>({enabled:form.elements.enabled.checked,...Object.fromEntries(fields.map(([key])=>[key,Number(form.elements[key].value)]))});
            const history=()=>{target.querySelector('[data-lens-history]').innerHTML='<option value="">选择历史版本</option>'+data.history.slice().reverse().map((h,i)=>`<option value="${i}">v${h.version} · ${this.escape(h.at)}</option>`).join('');};
            const status=value=>{target.querySelector('[data-lens-status]').textContent=`Patchright：${value.driver||'未安装'}（要求 ${value.driver_required}）\n浏览器：${value.browser_path||'默认驱动浏览器，未启动验证'}${value.browser_exists===false?'（路径不存在）':''}\n最近结果：${JSON.stringify(value.last_result)||'暂无'}\n最近错误：${JSON.stringify(value.last_error)||'暂无'}\n${value.notice}`;};
            fill(data.settings);history();status(data.status);
            target.querySelector('[data-lens-default]').onclick=()=>{fill(data.defaults);message.textContent='已填入当前环境默认值，保存后生效';};
            target.querySelector('[data-lens-history]').onchange=e=>{if(e.target.value!==''){fill(data.history.slice().reverse()[Number(e.target.value)].settings);message.textContent='已填入历史参数，保存后生效';}};
            form.onsubmit=async e=>{e.preventDefault();const button=form.querySelector('[type=submit]');button.disabled=true;try{const result=await API.put('/api/assistant/prompts/lens',{version:data.version,settings:draft()});Object.assign(data,result);history();message.textContent=result.message;}catch(err){message.textContent=`保存失败：${err.message}；草稿已保留`;}finally{button.disabled=false;}};
            const refresh=async()=>{try{const id=target.querySelector('[data-lens-chat]').value;const result=await API.get('/api/assistant/prompts/lens'+(id?`?chat_id=${id}`:''));if(!target.isConnected||id!==target.querySelector('[data-lens-chat]').value)return;status(result.status);const c=result.scope.chat;target.querySelector('[data-lens-scope]').textContent=result.scope.profiles+(c?`。${c.name}：在线研究${c.online_research?'允许':'禁止'}；工作区${c.workspace_access==='read_only'?'只读':'可写'}。Lens ${result.settings.enabled&&c.online_research&&c.workspace_access!=='read_only'?'可用':'不可用'}。`:'。请选择聊天查看权限；未保存参数不影响实际请求。');}catch(err){message.textContent=`状态读取失败：${err.message}`;}};
            target.querySelector('[data-lens-refresh]').onclick=refresh;target.querySelector('[data-lens-chat]').onchange=()=>{output.replaceChildren();refresh();};refresh();
            target.querySelector('[data-lens-preview]').onclick=async()=>{if(!form.reportValidity())return;try{const result=await API.post('/api/assistant/prompts/preview',{scene:'reply',lens_draft:draft(),chat_id:Number(target.querySelector('[data-lens-chat]').value)||null});if(!target.isConnected)return;this.renderBlocks(output,result.blocks.filter(b=>['image_search','video_search','lens_policy','lens_tools','permissions'].includes(b.id)).map(b=>({...b,editable:null})),null);message.textContent='未保存参数预览；使用当前已保存规则，不调用模型或上传图片';}catch(err){message.textContent=`预览失败：${err.message}`;}};
            target.querySelector('[data-lens-snapshots]').onclick=async()=>{const id=Number(target.querySelector('[data-lens-chat]').value);if(!id){message.textContent='请先选择聊天';return;}try{const result=await API.get(`/api/assistant/prompts/chats/${id}/snapshots`);if(!target.isConnected||id!==Number(target.querySelector('[data-lens-chat]').value))return;output.innerHTML=result.items.map((item,i)=>`<details class="prompt-block"><summary>${this.escape(new Date(item.created*1000).toLocaleString())} · ${this.escape(item.scene)}</summary><div class="prompt-block-body" data-lens-snapshot="${i}">展开后读取</div></details>`).join('')||'<p>暂无真实请求快照</p>';output.querySelectorAll('details').forEach(details=>details.addEventListener('toggle',async()=>{const content=details.querySelector('[data-lens-snapshot]');if(!details.open||content.dataset.loaded)return;content.dataset.loaded='loading';try{const payload=await API.get(`/api/assistant/prompts/chats/${id}/snapshots/${result.items[Number(content.dataset.lensSnapshot)].id}`);if(!content.isConnected)return;this.renderBlocks(content,Object.entries(payload).map(([key,value])=>({id:key,title:({lens_policy:'本轮检索配置',developer_instructions:'实际系统指令',tools:'实际工具定义',turn_input:'实际输入'})[key]||key,content:typeof value==='string'?value:JSON.stringify(value,null,2),source:'真实请求'})),null);content.dataset.loaded='yes';}catch(err){delete content.dataset.loaded;content.textContent=`读取失败：${err.message}；折叠后展开重试`;}}));message.textContent='真实请求快照：每聊天最近20次，最长7天；与未保存草稿独立';}catch(err){message.textContent=`读取失败：${err.message}`;}};
        } catch(e) {target.textContent=`读取失败：${e.message}`;}
    },
    async systemRules() { return BotWorkbench.open(); },

};
window.PromptWorkbench=PromptWorkbench;
