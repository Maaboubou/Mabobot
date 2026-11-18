// The two views share the chat selection and live for the lifetime of one dialog.
const AssistantStudio = {
    async open(options = {}) {
        if (this.state) return this.show(options.view || 'edit');
        const modal = document.createElement('div');
        modal.id = 'assistantStudio';
        modal.className = 'modal fade';
        modal.tabIndex = -1;
        modal.setAttribute('aria-labelledby', 'studioTitle');
        modal.innerHTML = `<div class="modal-dialog modal-fullscreen"><div class="modal-content bw-shell studio-shell">
            <header class="studio-header"><h2 id="studioTitle">行为与测试</h2>
            <nav aria-label="工作区视图"><button type="button" data-view="edit">行为编辑</button><button type="button" data-view="lab">测试会话</button></nav>
            <select class="form-select form-select-sm" aria-label="当前聊天" data-studio-chat disabled></select>
            <button class="btn-close" data-bs-dismiss="modal" aria-label="关闭工作区"></button></header>
            <section id="botWorkbench" class="modal-body bw-body" aria-label="行为编辑" hidden></section>
            <section id="chatTestLab" class="modal-body tl-body tl-shell" aria-label="测试会话" hidden></section>
            <div data-loading class="bw-loading" role="status">正在读取聊天…</div></div></div>`;
        document.body.append(modal);
        const s = this.state = {modal, options, opener:document.activeElement, chatId:0, view:options.view || 'edit'};
        const select = s.select = modal.querySelector('[data-studio-chat]');
        const instance = bootstrap.Modal.getOrCreateInstance(modal);
        const warn = event => { if (BotWorkbench.dirty()) event.preventDefault(); };
        window.addEventListener('beforeunload', warn);
        modal.addEventListener('hide.bs.modal', event => {
            if (BotWorkbench.state?.busy || ChatTestLab.mutating() ||
                (BotWorkbench.dirty() && !confirm('放弃未保存的修改并关闭？'))) event.preventDefault();
        });
        modal.addEventListener('hidden.bs.modal', () => {
            window.removeEventListener('beforeunload', warn);
            clearTimeout(ChatTestLab.timer);
            ChatTestLab.state = null;
            BotWorkbench.state = null;
            this.state = null;
            instance.dispose();
            modal.remove();
            if (s.opener?.isConnected) s.opener.focus();
        }, {once:true});
        modal.querySelectorAll('[data-view]').forEach(b => b.onclick = () => this.show(b.dataset.view));
        instance.show();
        const load = async () => {
            try {
                const overview = await API.assistant.getOverview();
                if (this.state !== s) return;
                const chats = overview.chats || [];
                const bound = options.kind && chats.find(c => c[options.kind]?.id === options.id);
                s.chatId = Number(options.chatId || bound?.id || chats[0]?.id || 0);
                s.overview = overview;
                select.innerHTML = chats.map(c => `<option value="${Number(c.id)}">${UI.escapeHtml(c.chat_name)}</option>`).join('') || '<option value="0">未配置聊天</option>';
                select.value = String(s.chatId);
                select.disabled = !chats.length;
                select.onchange = () => this.setChat(Number(select.value));
                modal.querySelector('[data-loading]').hidden = true;
                await this.show(s.view);
            } catch (error) {
                if (this.state !== s) return;
                const panel = modal.querySelector('[data-loading]');
                panel.innerHTML = `${UI.escapeHtml(error.message)} <button class="btn btn-sm btn-outline-secondary">重试</button>`;
                panel.querySelector('button').onclick = load;
            }
        };
        await load();
    },
    setChat(id) {
        const s = this.state;
        if (id === s.chatId) return;
        s.chatId = id;
        s.select.value = String(id);
        if (BotWorkbench.state?.body) BotWorkbench.changeChat(id);
        if (ChatTestLab.state?.ready) ChatTestLab.changeChat();
    },
    async show(view) {
        const s = this.state;
        s.view = view;
        if (!s.overview) return;
        const edit = s.modal.querySelector('#botWorkbench'), lab = s.modal.querySelector('#chatTestLab');
        edit.hidden = view !== 'edit';
        lab.hidden = view !== 'lab';
        s.modal.querySelectorAll('[data-view]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.view === view)));
        if (view === 'edit' && !BotWorkbench.state) await BotWorkbench.mount(edit, s);
        if (view === 'lab') {
            if (!ChatTestLab.state) await ChatTestLab.mount(lab, s);
            else if (ChatTestLab.state.ready) ChatTestLab.syncVariant();
        }
    },
    draftRequest() {
        const s = this.state;
        if (BotWorkbench.state?.body) {
            const req = BotWorkbench.request(true, 'reply');
            if (BotWorkbench.hasReplyDraft()) return req;
        }
        return Number(s.options.request?.chat_id) === s.chatId ? s.options.request : null;
    }
};

// Shared by the source editor and the role/judge forms. Results stay attached to
// the exact chat, input and revisions that produced them.
const BehaviorCheck = {
    mount(host, context) {
        host.innerHTML = `<section class="studio-check"><div class="studio-check-heading"><h3 data-check-title></h3><select class="form-select form-select-sm" aria-label="验证方式" data-check-mode hidden><option value="preview">请求结构</option><option value="compare">回复对比</option></select></div>
            <div class="studio-check-context" data-check-context></div>
            <div data-check-input><label><span class="visually-hidden">测试消息</span><textarea class="form-control" rows="2" maxlength="20000" placeholder="输入测试消息" data-check-message></textarea></label>
            <div class="studio-check-options" data-judge-options hidden><label>配置<select class="form-select form-select-sm" data-version><option value="draft">当前草稿</option><option value="saved">已保存</option></select></label>
            <details><summary>模拟触发条件</summary><div class="studio-timing">${[['message_count','新增消息',5],['elapsed_minutes','距上次回复（分）',10],['cooldown_message_count','冷却后新增消息',5],['cooldown_elapsed_minutes','冷却时间（分）',10]].map(([key,label,value])=>`<label>${label}<input type="number" class="form-control form-control-sm" data-time="${key}" min="0" max="100000" value="${value}"></label>`).join('')}</div>
            <label><input type="checkbox" data-cooldown> 处于冷却期</label><label><input type="checkbox" data-ignore> 忽略条件，仅测语义</label></details></div></div>
            <button type="button" class="btn btn-primary btn-sm" data-check-run></button>
            <small data-check-note></small><p data-check-state role="status"></p><div data-check-result></div></section>`;
        const find = sel => host.querySelector(sel);
        const results = new Map(), pending = new Set(), messages = new Map(), modes = new Map();
        let chatId;
        const current = () => {
            const c = context();
            const message=find('[data-check-message]');
            if(chatId!==c.request.chat_id){
                if(chatId!==undefined)messages.set(chatId,message.value);
                chatId=c.request.chat_id;
                message.value=messages.get(chatId)||'';
            }
            c.canSwitch=c.mode==='preview';
            if(c.canSwitch)c.mode=modes.get(c.item)||'preview';
            if(c.mode==='compare' && !c.request.chat_id)c.block='请先选择聊天';
            const body = {...c.request, content:find('[data-check-message]').value};
            if (c.mode === 'decision') {
                if (find('[data-version]').value === 'saved') {
                    for (const field of ['rule_draft','draft','chat_base_mode','chat_base_content','global_base_mode','global_base_content']) delete body[field];
                }
                body.ignore_timing = find('[data-ignore]').checked;
                body.in_cooldown = find('[data-cooldown]').checked;
                host.querySelectorAll('[data-time]').forEach(e => body[e.dataset.time] = Number(e.value));
            }
            const key = JSON.stringify([body.chat_id,c.item,c.mode]);
            return {c,body,key,signature:JSON.stringify([body,c.revision])};
        };
        function refresh() {
            const {c,key,signature} = current(), preview = c.mode === 'preview';
            find('[data-check-mode]').hidden=!c.canSwitch;
            if(c.canSwitch)find('[data-check-mode]').value=c.mode;
            find('[data-check-title]').textContent = c.canSwitch?'验证修改': {preview:'请求结构',compare:'回复对比',decision:'接话试判'}[c.mode];
            find('[data-check-context]').textContent = c.label;
            find('[data-check-input]').hidden = preview;
            find('[data-judge-options]').hidden = c.mode !== 'decision';
            find('[data-check-run]').textContent = pending.has(key) ? '运行中…' : {preview:'预览请求结构',compare:'对比回复',decision:'开始试判'}[c.mode];
            find('[data-check-run]').disabled = pending.has(key) || !!c.block;
            find('[data-check-note]').textContent = c.block || {preview:'本地组装 · 无模型调用',compare:'相同背景 · 2 次模型调用',decision:'模拟消息数与间隔'}[c.mode];
            const record = results.get(key);
            find('[data-check-state]').textContent = record && record.signature !== signature ? '配置或输入已变更，可重新运行' : '';
            const output = find('[data-check-result]');
            if (output._record !== record) { output.innerHTML = record?.html || ''; output._record = record; }
        }
        find('[data-check-run]').onclick = async () => {
            const {c,body,key,signature} = current();
            if (c.block || pending.has(key)) return;
            if (c.mode === 'preview') return c.preview();
            if (!body.content.trim()) { find('[data-check-message]').focus(); find('[data-check-state]').textContent='请输入测试消息'; return; }
            if (c.mode === 'decision' && [...host.querySelectorAll('[data-time]')].some(e=>!e.reportValidity())) return;
            pending.add(key); refresh();
            const esc = v => UI.escapeHtml(String(v ?? ''));
            try {
                const r = await API.post('/api/assistant/prompts/' + (c.mode === 'decision' ? 'trial' : 'compare-replies'), body);
                let html;
                if (c.mode === 'decision') {
                    const answer = r.result.state === 'skipped' ? '已跳过模型判断' : r.result.should_reply ? '建议接话' : '建议不接话';
                    html = `<p>模拟触发条件 · ${r.timing_met?'满足':'未满足'}${body.ignore_timing?'（已忽略）':''}</p><strong>${answer}</strong><p>${esc(r.result.reason)}</p>`;
                } else {
                    html = ['saved','draft'].map(k=>`<article><small>${k==='saved'?'已保存配置':'本次草稿'} · ${esc(r[k].elapsed_seconds)} 秒</small><p>${esc(r[k].text || r[k].error)}</p></article>`).join('') + (r.same_configuration?'<small>两版配置相同</small>':'') + `<details><summary>本次测试背景</summary><pre>${esc(r.context?.history)}</pre></details>`;
                }
                const variant=c.mode==='decision'?(body.draft?'草稿':'已保存'):'已保存 × 草稿';
                const caption=`<small class="studio-check-caption">${esc(c.label)} · ${variant}</small>`;
                results.set(key,{signature,html:caption+html});
            } catch(error) { results.set(key,{signature,html:`<p role="alert">${esc(error.message)}</p>`}); }
            finally { pending.delete(key); if(host.isConnected) refresh(); }
        };
        find('[data-check-mode]').oninput=e=>modes.set(context().item,e.target.value);
        host.addEventListener('input', refresh);
        refresh();
        return refresh;
    }
};
