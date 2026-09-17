/** Shared backend-described form for defaults and per-chat differences. */
const CodexPermissions = {
    schema: null,
    defaults: null,
    scope: 'group',
    foldOpen: false,
    drafts: {},
    requestId: 0,
    statuses: { effective: '已生效', next_turn: '下一轮生效', refreshing: '正在刷新', unsupported: '运行时不支持', failed: '应用失败' },
    reasons: {
        permission_profile_unavailable: '文件隔离尚未通过运行时验证，不能显示已生效。',
        network_proxy_unavailable: '公共互联网暂不可用：网络代理尚未通过验证。',
        auto_review_unavailable: '自动审阅暂不可用：审阅器尚未通过运行时验证，当前直接拒绝越界操作。',
        online_research_partial: '在线研究仅部分可用，请查看下方各通道状态。',
        online_research_unavailable: '在线研究工具尚未通过运行时验证。'
    },

    summary(effective = {}) {
        const e = UI.escapeHtml;
        return `<p class="mb-2">${effective.workspace_access === 'read_only' ? '工作区只读' : '工作区可读写'} · ${effective.public_network ? '可访问公共互联网' : '禁止公共互联网'} · ${effective.boundary_action === 'auto_review' ? '自动审阅越界操作' : '直接拒绝越界操作'}</p>
            <p class="text-muted mb-2">原生搜索：${effective.web_search_mode === 'live' ? '允许' : '禁止'} · 浏览器：${effective.browser_allowed ? '允许' : '禁止'} · 审核下载：${effective.reviewed_download_allowed ? '允许' : '禁止'}</p>
            ${effective.boundary_action === 'auto_review' ? '<p class="text-muted mb-2">越界请求由自动审阅判断，允许后继续执行，拒绝后停止该操作。模型审阅可能出错。</p>' : ''}
            ${(effective.issues || []).map(code => `<p class="text-muted mb-1">${e(this.reasons[code] || '权限应用需要检查运行时')}</p>`).join('')}`;
    },

    researchDetail(effective = {}) {
        return `原生搜索${effective.web_search_mode === 'live' ? '可用' : '不可用'}、浏览器${effective.browser_allowed ? '可用' : '不可用'}、审核下载${effective.reviewed_download_allowed ? '可用' : '不可用'}`;
    },

    /** Issue copy has to stand on its own: the defaults card no longer lists derived channels. */
    issueText(code, effective = {}) {
        if (code === 'online_research_partial') return `在线研究仅部分可用：${this.researchDetail(effective)}。`;
        if (code === 'online_research_unavailable') return `在线研究工具尚未通过运行时验证：${this.researchDetail(effective)}。`;
        return this.reasons[code] || '权限应用需要检查运行时';
    },

    /** Compact, single-card form for the Codex center defaults editor. */
    defaultsForm(data, fields) {
        const e = UI.escapeHtml;
        const values = data.values || {};
        const effective = data.effective || {};
        const issues = (effective.issues || []).map(code => this.issueText(code, effective));
        const boundaries = data.constraints?.boundaries || this.schema?.hard_boundaries || [];
        const scopeLabel = data.scope_type === 'group' ? '群聊' : '私聊';
        const runtime = this.statuses[data.apply_status] || '正在刷新';
        return `<form class="codex-permission-form">
            <div class="codex-permission-toolbar">
                <div class="chat-policy-permission-options codex-permission-segment" role="radiogroup" aria-label="权限作用范围">${[['group', '群聊默认'], ['private', '私聊默认']].map(([scope, label]) => `<label><input type="radio" name="default_scope" value="${scope}" ${data.scope_type === scope ? 'checked' : ''}> ${label}</label>`).join('')}</div>
                <div class="codex-permission-runtime" role="status" aria-live="polite">${data.apply_status && data.apply_status !== 'next_turn' ? `<strong>有效权限 · ${e(runtime)}</strong>` : ''}</div>
            </div>
            ${issues.length ? `<div class="codex-permission-issues" role="note"><i class="bi bi-exclamation-triangle" aria-hidden="true"></i><div>${issues.map(text => `<p>${e(text)}</p>`).join('')}</div></div>` : ''}
            <div class="chat-policy-field-grid two chat-policy-permission-grid codex-permission-grid">${fields.map(field => `<fieldset class="chat-policy-permission-field codex-permission-field" data-permission-field="${e(field.key)}">
                <legend>${e(field.label)}<small title="${e(field.help)}">${e(field.help)}</small></legend>
                <div class="chat-policy-permission-options">${field.options.map((option, index) => `<label for="codex-default-${e(field.key)}-${index}"><input id="codex-default-${e(field.key)}-${index}" type="radio" name="permission_${e(field.key)}" value="${e(JSON.stringify(option.value))}" ${values[field.key] === option.value ? 'checked' : ''}> ${e(option.label)}</label>`).join('')}</div>
                ${field.key === 'boundary_action' ? `<p class="codex-permission-caveat" ${values.boundary_action === 'auto_review' ? '' : 'hidden'}>自动审阅由模型判断，可能出错；未获批准的操作不会执行。</p>` : ''}
            </fieldset>`).join('')}</div>
            <div class="codex-permission-actions">
                <p class="codex-permission-impact">保存后影响跟随默认的 ${UI.formatNumber(data.affected_chats || 0)} 个${scopeLabel}；下一轮生效，进行中的轮次保持原权限。</p>
                <button type="submit" class="btn btn-primary" disabled>保存并生效</button>
            </div>
            <details class="system-fold codex-permission-boundaries"><summary>查看系统固定边界</summary><div class="mt-2">
                <p>${e(data.constraints?.scope_display || '每个聊天使用独立空间')}</p>
                ${boundaries.map(text => `<p class="mb-1">${e(text)}</p>`).join('')}
                <p>${e(data.runtime_support?.reason || '运行时已完成能力检查')}</p>
                <p class="text-muted">插件网络和账号继续由“功能插件”授权管理。</p>
            </div></details>
        </form>`;
    },

    render(data, fields, { chat = false, prefix = 'codex-default' } = {}) {
        const e = UI.escapeHtml;
        const custom = data.settings_mode === 'custom';
        const values = data.values || data.defaults || {};
        const runtime = data.runtime || { status: data.apply_status };
        const boundaries = data.constraints?.boundaries || this.schema?.hard_boundaries || [];
        return `<section class="chat-policy-block" data-permission-editor="${prefix}">
            <div class="chat-policy-block-head"><h4>${chat ? 'Codex 权限' : '权限默认'}</h4>
            <span class="chat-policy-plugin-scope ${custom ? 'is-custom' : ''}">${chat ? (custom ? '本聊天已自定义' : '跟随默认') : (this.scope === 'group' ? '群聊默认' : '私聊默认')}</span></div>
            ${chat ? `<fieldset class="chat-policy-permission-field"><legend>作用范围</legend><div class="chat-policy-permission-options">
                ${[['inherit', '跟随默认'], ['custom', '本聊天自定义']].map(([value, label]) => `<label><input type="radio" name="permission_mode" value="${value}" ${custom === (value === 'custom') ? 'checked' : ''}> ${label}</label>`).join('')}</div>
                <p class="text-muted">${custom ? '已覆盖字段使用本聊天设置，其余字段继续跟随默认' : `当前聊天使用${data.effective?.scope === 'group' ? '群聊' : '私聊'}默认权限`}</p></fieldset>` : ''}
            <div role="status" aria-live="polite" class="mb-3"><strong>有效权限 · ${e(this.statuses[runtime.status] || '正在刷新')}</strong>
                ${runtime.applied_at ? `<span> · ${e(UI.formatDateTime(runtime.applied_at))}</span>` : ''}
                ${this.summary(data.effective)}
                ${runtime.status === 'unsupported' ? '<a href="/codex" onclick="event.preventDefault(); UI.switchTab(\'codex\')">查看 Codex 运行中心</a>' : ''}
            </div>
            <div class="chat-policy-field-grid two chat-policy-permission-grid">${fields.map(field => `<fieldset class="chat-policy-permission-field" data-permission-field="${e(field.key)}" ${chat && !custom ? 'disabled' : ''}>
                <legend>${e(field.label)}${chat ? `<small> · ${data.sources?.[field.key] === 'chat' ? '本聊天' : '默认'}</small>` : ''}</legend>
                <div class="chat-policy-permission-options">${field.options.map((option, index) => `<label for="${prefix}-${e(field.key)}-${index}"><input id="${prefix}-${e(field.key)}-${index}" type="radio" name="permission_${e(field.key)}" value="${e(JSON.stringify(option.value))}" ${values[field.key] === option.value ? 'checked' : ''}> ${e(option.label)}</label>`).join('')}</div>
                <small class="text-muted">${e(field.help)}</small>
            </fieldset>`).join('')}</div>
            ${chat && data.constraints?.owner_full_allowed ? `<details class="system-fold mt-3" ${values.access_scope === 'owner_full' ? 'open' : ''}>
                <summary>管理员私聊访问范围${values.access_scope === 'owner_full' ? ' · 本机最大权限' : ''}</summary>
                <div class="chat-policy-field-grid compact mt-2"><label><span>Codex 访问范围</span><select class="form-select" name="codex_mode" data-private-value="${e(values.access_scope || 'isolated')}" ${!data.constraints?.owner_full_allowed ? 'disabled' : ''}>
                <option value="isolated">隔离空间</option>${data.constraints?.owner_full_allowed ? `<option value="owner_full" ${values.access_scope === 'owner_full' ? 'selected' : ''}>管理员 · 本机最大权限</option>` : ''}</select></label></div>
                <p class="text-muted">仅可为本人可控的管理员私聊显式启用；文件和命令范围将扩大。</p>
            </details>` : chat ? '<input type="hidden" name="codex_mode" value="isolated">' : ''}
            <details class="system-fold mt-3"><summary>查看系统固定边界</summary><div class="mt-2">
                <p>${e(data.constraints?.scope_display || '每个聊天使用独立空间')}</p>
                ${boundaries.map(text => `<p class="mb-1">${e(text)}</p>`).join('')}
                <p>${e(data.runtime_support?.reason || '运行时已完成能力检查')}</p>
                <p class="text-muted">插件网络和账号继续由“功能插件”授权管理。</p>
            </div></details>
            ${chat ? '<button type="button" class="btn btn-outline-secondary mt-3" data-permission-reset>恢复默认</button>' : ''}
        </section>`;
    },

    values(root, fields) {
        return Object.fromEntries(fields.map(field => [field.key, JSON.parse(root.querySelector(`[name="permission_${field.key}"]:checked`)?.value ?? 'null')]));
    },

    bindChat(root, data) {
        if (!root || !data.fields) return;
        root._permissions = data;
        root.querySelectorAll('[name="permission_mode"]').forEach(input => input.addEventListener('change', () => {
            const custom = root.querySelector('[name="permission_mode"]:checked')?.value === 'custom';
            root.querySelectorAll('[data-permission-field]').forEach(field => {
                field.disabled = !custom;
                const value = custom ? (data.values || data.defaults)[field.dataset.permissionField] : data.defaults[field.dataset.permissionField];
                field.querySelectorAll('input').forEach(radio => { radio.checked = JSON.parse(radio.value) === value; });
            });
            if (!custom && root.elements.codex_mode) root.elements.codex_mode.value = 'isolated';
        }));
        root.querySelector('[name="codex_mode"]')?.addEventListener('change', () => {
            if (root.elements.codex_mode.value === 'owner_full') {
                root.querySelector('[name="permission_mode"][value="custom"]').checked = true;
                root.querySelectorAll('[data-permission-field]').forEach(field => { field.disabled = false; });
            }
        });
        root.querySelector('[data-permission-reset]')?.addEventListener('click', async () => {
            if (!await UI.confirm('将清除本聊天全部权限差异，并恢复跟随当前默认权限。', { title: '恢复默认权限', confirmText: '恢复默认' })) return;
            const inherit = root.querySelector('[name="permission_mode"][value="inherit"]');
            inherit.checked = true;
            inherit.dispatchEvent(new Event('change', { bubbles: true }));
            await App.saveChatPolicy();
        });
    },

    chatPatch(form) {
        const data = form._permissions;
        if (!data) return { mode: form.elements.codex_mode?.value || 'isolated' };
        if (form.querySelector('[name="permission_mode"]:checked')?.value !== 'custom') return { settings_mode: 'inherit', reset_all: true };
        const values = this.values(form, data.fields);
        values.access_scope = form.elements.chat_type.value === 'group' ? 'isolated' : form.elements.codex_mode.value;
        return { settings_mode: 'custom', set: values };
    },

    async loadDefaults(scope = this.scope) {
        const root = document.getElementById('codexPermissionDefaults');
        if (!root) return;
        const body = root.querySelector('.codex-permission-fold-body') || root;
        if (root.dataset.permissionFoldBound !== '1') {
            root.dataset.permissionFoldBound = '1';
            root.open = this.foldOpen;
            root.addEventListener('toggle', () => { this.foldOpen = root.open; });
        }
        const previous = body.querySelector('form');
        if (previous && this.defaults) this.drafts[previous.dataset.permissionScope] = { version: Number(previous.dataset.version), values: this.values(previous, this.schema.fields) };
        this.scope = scope;
        const requestId = ++this.requestId;
        root.setAttribute('aria-busy', 'true');
        try {
            const [schema, data] = await Promise.all([this.schema || API.get('/api/codex/permissions/schema'), API.get(`/api/codex/permissions/defaults/${scope}`)]);
            if (requestId !== this.requestId) return;
            this.schema = schema;
            this.defaults = data;
            this.renderDefaults(root, data);
        } catch (error) {
            if (!body.querySelector('form')) {
                root.open = true;
                body.innerHTML = '<p class="text-muted p-3">权限读取失败，请刷新运行中心重试。</p>';
            }
            UI.showError(`读取权限失败：${error.message}`);
        } finally {
            if (requestId === this.requestId) root.removeAttribute('aria-busy');
        }
    },

    renderDefaults(root, data) {
        const body = root.querySelector('.codex-permission-fold-body') || root;
        const draft = this.drafts[this.scope];
        body.innerHTML = this.defaultsForm(data, this.schema.fields);
        const form = body.querySelector('form');
        form.dataset.permissionScope = data.scope_type;
        form.dataset.version = draft?.version ?? data.version;
        if (draft) form.querySelectorAll('[data-permission-field] input').forEach(radio => {
            radio.checked = JSON.parse(radio.value) === draft.values[radio.closest('fieldset').dataset.permissionField];
        });
        const button = form.querySelector('[type="submit"]');
        const sync = () => {
            const values = this.values(form, this.schema.fields);
            const dirty = JSON.stringify(values) !== JSON.stringify(data.values);
            button.disabled = !dirty;
            const caveat = form.querySelector('.codex-permission-caveat');
            if (caveat) caveat.hidden = values.boundary_action !== 'auto_review';
        };
        form.addEventListener('change', sync);
        form.querySelectorAll('[name="default_scope"]').forEach(radio => radio.addEventListener('change', () => this.loadDefaults(radio.value)));
        form.addEventListener('submit', event => { event.preventDefault(); this.saveDefaults(form, data); });
        sync();
    },

    async saveDefaults(form, data) {
        const button = form.querySelector('[type="submit"]');
        const scope = data.scope_type;
        const values = this.values(form, this.schema.fields);
        const controls = [...form.querySelectorAll('input, button')];
        controls.forEach(control => { control.disabled = true; });
        button.textContent = '正在保存权限…';
        try {
            const updated = await API.request(`/api/codex/permissions/defaults/${scope}`, {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ expected_version: Number(form.dataset.version), set: values })
            });
            delete this.drafts[scope];
            this.defaults = updated;
            this.renderDefaults(document.getElementById('codexPermissionDefaults'), updated);
            UI.showSuccess('权限已保存，应用状态以运行时确认为准');
        } catch (error) {
            if (error.status === 409) {
                const latest = await API.get(`/api/codex/permissions/defaults/${scope}`).catch(() => null);
                if (latest) { data.version = latest.version; form.dataset.version = latest.version; this.defaults = latest; }
                UI.showError('配置已被其他页面更新。已保留输入，请核对后重新保存。');
            } else UI.showError(`保存权限失败：${error.message}`);
        } finally {
            if (form.isConnected) {
                controls.forEach(control => { control.disabled = false; });
                button.textContent = '保存并生效';
            }
        }
    }
};
