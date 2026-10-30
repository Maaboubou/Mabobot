/* One catalog, original persistence APIs: no duplicate prompt storage. */
const RoutePrompts = {
    serial: 0,
    esc(value) { return UI.escapeHtml(String(value ?? '')); },
    async entries(ownerId) {
        const settingsRequest = API.capabilities.getSettings(ownerId);
        const [settings, roles, judges, rules] = await Promise.all([
            settingsRequest,
            ownerId === 'assistant' ? API.roles.getAll() : null,
            ownerId === 'assistant' ? API.judges.getAll() : null,
            ownerId === 'assistant' ? API.get('/api/assistant/prompts/rules') : null
        ]);
        const items = (settings.groups || []).flatMap(group => group.fields || [])
            .filter(field => (field.is_prompt ?? field.key.toLowerCase().includes('prompt')) &&
                field.editable !== false && !field.deprecated && !field.sensitive &&
                (typeof field.value === 'string' || field.value == null))
            .map(field => ({
                id: `setting:${field.key}`, kind: 'setting', key: field.key,
                title: field.title || '提示词', group: '默认提示词',
                description: field.description, scope: '全局默认；聊天自定义配置不受影响',
                content: field.value ?? field.default ?? ''
            }));
        for (const [kind, data, group] of [['role', roles?.roles, '角色设定'], ['judge', judges?.judges, '接话判断']]) {
            for (const row of data || []) items.push({
                id: `${kind}:${row.id}`, kind, key: row.id, title: row.display_name || group,
                group, description: row.description, scope: `所有使用此${group}的聊天`,
                content: row.prompt || '', revision: row.revision
            });
        }
        for (const rule of rules?.blocks || []) items.push({
            id: `rule:${rule.id}`, kind: 'rule', key: rule.id, title: rule.title,
            group: '系统规则', scope: rule.scope, content: rule.content
        });
        return {items: items.map(item => ({...item, draft: item.content})), ruleVersion: rules?.version};
    },
    async open(ownerId, ownerName) {
        // Finish Bootstrap's close transition before reusing its backdrop/body state.
        if (this.closing) await this.closing;
        const modal = document.getElementById('routePromptsModal');
        const body = document.getElementById('routePromptsModalBody');
        const state = {serial: ++this.serial, ownerId, items: [], busy: false, selected: null};
        this.active = state;
        document.getElementById('routePromptsModalTitle').textContent = `提示词 · ${ownerName}`;
        body.innerHTML = '<p class="route-prompt-empty" role="status">正在加载提示词…</p>';
        if (!modal.dataset.promptsBound) {
            modal.dataset.promptsBound = 'true';
            modal.addEventListener('hide.bs.modal', event => {
                const current = this.active;
                if (current?.busy || (current?.items.some(item => item.draft !== item.content) && !confirm('有未保存的提示词修改，确定放弃并关闭？'))) {
                    event.preventDefault(); return;
                }
                this.active = null;
                this.closing = new Promise(resolve => modal.addEventListener('hidden.bs.modal', () => {
                    this.closing = null;
                    resolve();
                }, {once: true}));
            });
        }
        bootstrap.Modal.getOrCreateInstance(modal).show();
        try {
            const data = await this.entries(ownerId);
            if (this.active !== state) return;
            Object.assign(state, data);
            state.selected = state.items[0]?.id;
            this.mount(state, body);
        } catch (error) {
            if (this.active === state) body.innerHTML = `<p class="route-prompt-empty text-danger" role="alert">加载失败：${this.esc(error.message)}。请关闭后重试。</p>`;
        }
    },
    mount(state, body) {
        if (!state.items.length) {
            body.innerHTML = '<p class="route-prompt-empty">暂无可编辑提示词。此任务可能使用固定指令。</p>';
            return;
        }
        body.innerHTML = `<div class="route-prompt-workspace">
            <aside class="route-prompt-directory"><label class="visually-hidden" for="routePromptSearch">搜索提示词</label><input id="routePromptSearch" type="search" class="form-control" placeholder="搜索提示词" autocomplete="off"><nav aria-label="提示词目录" data-prompt-directory></nav></aside>
            <section class="route-prompt-editor" aria-label="提示词编辑" data-prompt-editor></section>
        </div>`;
        state.body = body;
        body.querySelector('#routePromptSearch').addEventListener('input', () => this.directory(state));
        this.directory(state);
        this.editor(state);
    },
    directory(state) {
        const q = state.body.querySelector('#routePromptSearch').value.trim().toLowerCase();
        const filtered = state.items.filter(item => [item.title, item.group, item.description, item.key].join(' ').toLowerCase().includes(q));
        const groups = [...new Set(filtered.map(item => item.group))];
        const directory = state.body.querySelector('[data-prompt-directory]');
        directory.innerHTML = groups.map(group => `<div class="route-prompt-group"><h3>${this.esc(group)} <span>${filtered.filter(item => item.group === group).length}</span></h3>${filtered.filter(item => item.group === group).map(item => `<button type="button" data-prompt-id="${this.esc(item.id)}" aria-current="${item.id === state.selected ? 'true' : 'false'}">${this.esc(item.title)}<span data-prompt-dirty ${item.draft === item.content ? 'hidden' : ''} aria-label="未保存"> ·</span></button>`).join('')}</div>`).join('') || '<p class="route-prompt-empty">没有匹配的提示词</p>';
        directory.querySelectorAll('[data-prompt-id]').forEach(button => button.onclick = () => {
            if (state.busy) return;
            state.selected = button.dataset.promptId;
            directory.querySelectorAll('[data-prompt-id]').forEach(node => node.setAttribute('aria-current', String(node === button)));
            this.editor(state);
        });
    },
    editor(state) {
        const item = state.items.find(row => row.id === state.selected);
        const editor = state.body.querySelector('[data-prompt-editor]');
        editor.innerHTML = `<header><h3>${this.esc(item.title)}</h3><p>${this.esc(item.group)} · ${this.esc(item.scope)}</p></header>
            ${item.description ? `<p class="route-prompt-description">${this.esc(item.description)}</p>` : ''}
            <label class="visually-hidden" for="routePromptContent">${this.esc(item.title)}</label>
            <textarea id="routePromptContent" class="form-control" spellcheck="false" ${item.kind !== 'setting' ? 'maxlength="30000"' : ''}>${this.esc(item.draft)}</textarea>
            <div class="route-prompt-actions"><span data-prompt-count></span><button type="button" class="btn btn-sm btn-outline-secondary" data-prompt-undo>撤销修改</button><button type="button" class="btn btn-sm btn-primary" data-prompt-save>保存提示词</button></div>
            <p class="route-prompt-status" role="status" aria-live="polite" data-prompt-status></p>
            <details class="route-prompt-reference"><summary>配置详情</summary><p>内部标识：<code>${this.esc(item.key)}</code></p><p>保存当前提示词，其他项目的草稿不会一并保存。</p></details>`;
        const area = editor.querySelector('textarea');
        const sync = () => {
            const dirty = item.draft !== item.content;
            editor.querySelector('[data-prompt-count]').textContent = `${item.draft.length.toLocaleString()} 字符${dirty ? ' · 未保存' : ''}`;
            editor.querySelector('[data-prompt-save]').disabled = state.busy || !dirty;
            editor.querySelector('[data-prompt-undo]').disabled = state.busy || !dirty;
            state.body.querySelectorAll('[data-prompt-id]').forEach(button => {
                if (button.dataset.promptId === item.id) button.querySelector('[data-prompt-dirty]').hidden = !dirty;
            });
        };
        area.addEventListener('input', () => { item.draft = area.value; sync(); });
        editor.querySelector('[data-prompt-undo]').onclick = () => { area.value = item.draft = item.content; editor.querySelector('[data-prompt-status]').textContent = ''; sync(); };
        editor.querySelector('[data-prompt-save]').onclick = async () => {
            if (state.busy || item.draft === item.content) return;
            state.busy = true; area.disabled = true; sync();
            const status = editor.querySelector('[data-prompt-status]');
            status.textContent = '正在保存…';
            try {
                const result = await this.save(state, item);
                item.content = item.draft = result.content;
                area.value = item.content;
                status.textContent = result.message || '已保存';
            } catch (error) { status.textContent = `保存失败：${error.message}。草稿已保留。`; }
            finally { state.busy = false; area.disabled = false; sync(); }
        };
        sync();
    },
    async save(state, item) {
        if (item.kind === 'rule') {
            const result = await API.put('/api/assistant/prompts/rules', {version: state.ruleVersion, changes: {[item.key]: item.draft}});
            state.ruleVersion = result.version;
            return {content: result.texts?.[item.key] ?? item.draft, message: result.message};
        }
        if (item.kind === 'role' || item.kind === 'judge') {
            const api = item.kind === 'role' ? API.roles : API.judges;
            const result = await api.update(item.key, {prompt: item.draft, revision: item.revision});
            item.revision = result.revision;
            return {content: result.prompt ?? item.draft, message: result.cache_refreshed === false ? '已保存；助手下次启动时加载' : '已保存，下次请求生效'};
        }
        await API.capabilities.updateSettings(state.ownerId, {[item.key]: item.draft});
        return {content: item.draft, message: '已保存全局默认，聊天自定义配置保持不变'};
    }
};
