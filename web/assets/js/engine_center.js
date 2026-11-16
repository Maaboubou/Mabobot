/** Engine-specific actions inside the shared Profile and task components. */
const EngineCenter = {
    loginTimer: null,
    loginRequest: 0,
    escape(value) { return UI.escapeHtml(String(value ?? '')); },

    setProfileEngine(engine, { refresh = true } = {}) {
        document.getElementById('aiProfileEngine').value = engine;
        const form = document.getElementById('codexProfileForm');
        document.getElementById('codexProfileFields').disabled = false;
        document.getElementById('codexProfileFields').classList.remove('d-none');
        CodexCenter.catalogProviders = [];
        CodexCenter.profileCatalogModels = [];
        ++CodexCenter.profileCatalogRequestId;
        const effort = form.elements.reasoning_effort;
        let automatic = effort.querySelector('[data-opencode-default]');
        if (engine === 'opencode' && !automatic) {
            automatic = new Option('跟随模型默认', 'default'); automatic.dataset.opencodeDefault = 'true'; effort.prepend(automatic);
        } else if (engine !== 'opencode') automatic?.remove();
        if (refresh && !CodexCenter.editingProfile) effort.value = engine === 'opencode' ? 'default' : 'high';
        if (refresh) void CodexCenter.setProfileSource(CodexCenter.getProfileSource());
    },

    updateProfileFields() {
        const form = document.getElementById('codexProfileForm');
        const opencode = CodexCenter.getProfileEngine() === 'opencode';
        const oauth = CodexCenter.getProfileSource() === 'oauth';
        const editing = Boolean(CodexCenter.editingProfile);
        document.getElementById('codexLocalAuthChoice')?.classList.toggle('d-none', opencode);
        if (opencode) form.querySelector('[name="auth_source"][value="device_code"]').checked = true;
        document.getElementById('profileAuthSourceHint').textContent = opencode
            ? '扫码完成 OpenCode 独立授权，随后选择此账号可用的模型。' : '导入只会复制文件型登录缓存到此 Profile，不会移动或退出本机 Codex 账号。';
        document.getElementById('profileOAuthSourceHint').textContent = opencode ? '扫码连接 ChatGPT 账号' : '导入本机登录或扫码连接';
        document.getElementById('profileManualSourceHint').textContent = opencode ? '配置接口地址、协议和 API Key' : '直接配置 Responses 兼容接口';
        document.getElementById('profileBaseUrlLabel').textContent = opencode ? 'API Base URL' : 'Responses API Base URL';
        document.getElementById('profileProtocolHint').textContent = opencode
            ? '目录会回填接口与协议，也可以按供应商文档调整。' : '目录只负责快速选型；接口本身必须兼容 OpenAI Responses API。';
        const contextHint = document.getElementById('profileContextWindowHint');
        if (contextHint) contextHint.textContent = opencode
            ? '用于此 Profile 的上下文窗口，不会改变供应商的硬限制；每轮按聊天历史建立上下文。'
            : '官方登录和 API 均生效；自动压缩阈值为此值的 90%。运行时可用窗口可能扣除预留空间。';
        const protocol = document.getElementById('opencodeProfileProtocol');
        protocol.disabled = !opencode || oauth;
        protocol.closest('.opencode-protocol-field').classList.toggle('d-none', !opencode || oauth);
        for (const name of ['model_verbosity', 'fast_mode']) {
            const input = form.elements[name]; input.disabled = opencode;
            input.closest('.codex-profile-final-config-field').classList.toggle('d-none', opencode || (oauth && !editing));
        }
        form.elements.supports_vision.disabled = opencode;
        form.elements.supports_vision.closest('label').classList.toggle('d-none', opencode);
        form.elements.supports_web_search.disabled = opencode;
        form.elements.supports_web_search.closest('label').classList.toggle('d-none', opencode);
        form.elements.reasoning_effort.disabled = opencode && !oauth && protocol.value === 'anthropic';
        if (opencode) {
            document.getElementById('codexProfileApiKeyHint').textContent = editing
                ? '留空保留当前 API Key；更换密钥后会重新验证连接。' : 'API Key 保存在此 OpenCode Profile 的私有目录，不会返回页面。';
            document.getElementById('saveCodexProfileButton').textContent = editing ? '保存并验证' : oauth ? '下一步' : '创建并验证';
        }
    },

    syncProtocol() {
        const form = document.getElementById('codexProfileForm');
        if (CodexCenter.getProfileEngine() !== 'opencode') return;
        form.elements.reasoning_effort.value = 'default';
        this.updateProfileFields();
    },

    async saveProfile() {
        const form = document.getElementById('codexProfileForm');
        if (!form.reportValidity()) return;
        const source = CodexCenter.getProfileSource();
        const oauth = source === 'oauth';
        const payload = { name: form.elements.name.value.trim(), auth_type: oauth ? 'chatgpt' : 'api_key', source,
            model: oauth ? '' : form.elements.model.value.trim(),
            reasoning_effort: oauth ? 'high' : form.elements.reasoning_effort.value || 'default',
            provider_name: oauth ? '' : form.elements.provider_name.value.trim(),
            catalog_provider: source === 'catalog' ? document.getElementById('codexProfileCatalogProvider').value : '',
            protocol: document.getElementById('opencodeProfileProtocol').value,
            base_url: oauth ? '' : form.elements.base_url.value.trim(), api_key: oauth ? '' : form.elements.api_key.value,
            context_window: oauth ? null : Number(form.elements.context_window.value || 128000), supports_vision: !oauth && !form.elements.supports_vision.disabled && form.elements.supports_vision.checked };
        const makeDefault = form.elements.make_default.checked;
        const editing = Boolean(CodexCenter.editingProfile);
        const button = document.getElementById('saveCodexProfileButton');
        button.disabled = true;
        let saved = false;
        try {
            if (editing) await API.request(`/api/ai/profiles/opencode/${encodeURIComponent(payload.name)}`, {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
            });
            else await API.post('/api/ai/profiles/opencode', payload);
            saved = true;
            form.elements.api_key.value = '';
            payload.api_key = '';
            button.disabled = false;
            const element = document.getElementById('codexProfileModal');
            const hidden = new Promise(resolve => element.addEventListener('hidden.bs.modal', resolve, { once: true }));
            bootstrap.Modal.getInstance(element).hide();
            await hidden;
            await CodexCenter.load();
            if (oauth) await this.login(payload.name, makeDefault);
            else await this.verify(payload.name, makeDefault);
        } catch (error) { UI.showError(`保存 Profile 失败：${error.message}`); }
        finally {
            if (!saved) { button.disabled = false; form.elements.api_key.value = ''; }
        }
    },

    connectionModal(profileId, makeDefault) {
        clearTimeout(this.loginTimer);
        const request = ++this.loginRequest;
        let element = document.getElementById('aiProfileConnectionModal');
        if (!element) {
            element = document.createElement('div');
            element.id = 'aiProfileConnectionModal';
            element.className = 'modal fade';
            element.tabIndex = -1;
            element.setAttribute('aria-labelledby', 'aiProfileConnectionTitle');
            element.innerHTML = `<div class="modal-dialog modal-dialog-centered"><div class="modal-content">
                <div class="modal-header"><div><h5 class="modal-title" id="aiProfileConnectionTitle">连接 OpenCode</h5><p class="modal-subtitle mb-0" data-connection-profile></p></div><button class="btn-close" type="button" data-bs-dismiss="modal" aria-label="关闭"></button></div>
                <div class="modal-body"><div class="ai-connection-status" data-connection-status role="status" aria-live="polite"></div><div data-connection-code-box class="mt-3 d-none"><div class="small text-muted mb-1">授权码（点击复制）</div><button type="button" class="codex-oauth-code fs-4" data-connection-code title="点击复制授权码"></button></div><div data-connection-link class="mt-3"></div><div data-connection-models class="mt-3"></div></div>
                <div class="modal-footer"><button class="btn btn-outline-secondary" type="button" data-connection-login>重新登录</button><button class="btn btn-outline-primary" type="button" data-connection-verify>重试验证</button><button class="btn btn-secondary" type="button" data-bs-dismiss="modal">关闭</button></div>
            </div></div>`;
            document.body.appendChild(element);
            element.querySelector('[data-connection-code]').onclick = async event => {
                const code = event.currentTarget.textContent;
                if (!code) return;
                try { await navigator.clipboard.writeText(code); UI.showSuccess('授权码已复制'); }
                catch (_) { UI.showError('复制失败，请手动输入上方授权码'); }
            };
            element.addEventListener('hidden.bs.modal', () => {
                ++this.loginRequest; clearTimeout(this.loginTimer);
                element.querySelector('[data-connection-code]').textContent = '';
                element.querySelector('[data-connection-code-box]').classList.add('d-none');
            });
        }
        element.querySelectorAll('[data-connection-login], [data-connection-verify]').forEach(button => { button.disabled = false; });
        element.querySelector('[data-connection-verify]').classList.remove('d-none');
        element.querySelector('[data-connection-profile]').textContent = profileId;
        element.querySelector('[data-connection-code]').textContent = '';
        element.querySelector('[data-connection-code-box]').classList.add('d-none');
        element.querySelector('[data-connection-link]').replaceChildren();
        element.querySelector('[data-connection-models]').replaceChildren();
        const apiProfile = CodexCenter.profiles.find(profile => profile.name === profileId)?.auth_type === 'api_key';
        element.querySelector('[data-connection-login]').classList.toggle('d-none', apiProfile);
        element.querySelector('[data-connection-login]').onclick = () => this.login(profileId, makeDefault);
        element.querySelector('[data-connection-verify]').onclick = () => this.verify(profileId, makeDefault);
        bootstrap.Modal.getOrCreateInstance(element).show();
        return { element, request, status: element.querySelector('[data-connection-status]') };
    },

    async login(profileId, makeDefault = false) {
        const view = this.connectionModal(profileId, makeDefault);
        view.status.textContent = '正在启动 OpenCode 授权…';
        view.element.querySelector('[data-connection-verify]').classList.add('d-none');
        const update = async status => {
            if (view.request !== this.loginRequest) return;
            if (status.status === 'connected') { await this.configureModels(profileId, makeDefault); return; }
            if (status.status !== 'waiting') {
                view.element.querySelector('[data-connection-code]').textContent = '';
                view.element.querySelector('[data-connection-code-box]').classList.add('d-none');
                view.status.textContent = '登录未完成，请重新登录。';
                return;
            }
            const code = String(status.user_code || '');
            view.element.querySelector('[data-connection-code]').textContent = code;
            view.element.querySelector('[data-connection-code-box]').classList.toggle('d-none', !code);
            view.status.textContent = code
                ? (status.url ? '请扫码或打开授权页面，输入下方授权码。' : '授权码已就绪，正在获取授权地址…')
                : (status.url ? '授权地址已就绪，正在等待授权码…' : '正在获取授权信息…');
            const box = view.element.querySelector('[data-connection-link]');
            box.replaceChildren();
            if (status.url) {
                const url = new URL(status.url);
                if (url.protocol === 'https:' && ['auth.openai.com', 'chatgpt.com'].includes(url.hostname)) {
                    const link = document.createElement('a');
                    link.href = url.href; link.target = '_blank'; link.rel = 'noopener';
                    const frame = document.createElement('div'); frame.className = 'codex-oauth-qr-frame mx-auto mb-3';
                    const qr = document.createElement('img'); qr.alt = 'OpenCode 授权二维码';
                    qr.src = `/api/ai/profiles/opencode/${encodeURIComponent(profileId)}/login/qr?v=${encodeURIComponent(status.started || '')}`;
                    qr.onerror = () => frame.remove(); frame.appendChild(qr); box.appendChild(frame);
                    link.className = 'btn btn-primary'; link.textContent = '打开授权页面'; box.appendChild(link);
                }
            }
            this.loginTimer = setTimeout(async () => {
                try { await update(await API.get(`/api/engines/profiles/${encodeURIComponent(profileId)}/login`)); }
                catch (error) { if (view.request === this.loginRequest) view.status.textContent = error.message; }
            }, 2000);
        };
        try { await update(await API.post(`/api/engines/profiles/${encodeURIComponent(profileId)}/login`, {})); }
        catch (error) { if (view.request === this.loginRequest) view.status.textContent = error.message; }
    },

    async configureModels(profileId, makeDefault = false) {
        const view = this.connectionModal(profileId, makeDefault);
        view.status.textContent = '正在读取此账号可用的模型…';
        view.element.querySelector('[data-connection-verify]').classList.add('d-none');
        try {
            const inventory = await API.get(`/api/ai/profiles/opencode/${encodeURIComponent(profileId)}/models`);
            if (view.request !== this.loginRequest) return;
            if (!inventory.authenticated || !inventory.models?.length) throw new Error('尚未读取到已登录账号的模型，请重新登录后重试。');
            const esc = value => this.escape(value);
            const box = view.element.querySelector('[data-connection-models]');
            box.innerHTML = `<label class="form-label" for="aiConnectedModel">模型</label><select class="form-select mb-3" id="aiConnectedModel">${inventory.models.map(model => `<option value="${esc(model.id)}">${esc(model.name || model.id)}</option>`).join('')}</select>
                <label class="form-label" for="aiConnectedEffort">推理强度</label><select class="form-select mb-3" id="aiConnectedEffort"></select>
                <label class="form-label" for="aiConnectedContextWindow">上下文窗口（Token）</label><input class="form-control" id="aiConnectedContextWindow" type="number" min="4096" max="10000000" placeholder="跟随模型默认"><div class="form-text mb-3">留空跟随模型默认。设置会传入 OpenCode，不会改变供应商的硬限制；每轮按聊天历史建立上下文。</div>
                <label class="form-check form-switch mb-3"><input class="form-check-input" type="checkbox" data-connected-default ${makeDefault ? 'checked' : ''}><span class="form-check-label">设为默认 Profile</span></label>
                <button class="btn btn-primary" type="button" data-connected-save>保存并验证</button>`;
            const select = box.querySelector('#aiConnectedModel');
            const effort = box.querySelector('#aiConnectedEffort');
            const current = CodexCenter.profiles.find(profile => profile.name === profileId);
            const contextWindow = box.querySelector('#aiConnectedContextWindow');
            contextWindow.value = current?.context_window || '';
            if (inventory.models.some(model => model.id === current?.model)) select.value = current.model;
            const update = () => {
                const model = inventory.models.find(model => model.id === select.value);
                const variants = model?.variants?.length ? model.variants : ['default'];
                effort.replaceChildren(...variants.map(value => new Option(value === 'default' ? '跟随模型默认' : value, value)));
                if (variants.includes(current?.reasoning_effort)) effort.value = current.reasoning_effort;
                else if (variants.includes('high')) effort.value = 'high';
            };
            select.addEventListener('change', update); update();
            view.status.textContent = '账号已连接，选择模型后完成配置。';
            box.querySelector('[data-connected-save]').onclick = async event => {
                if (!contextWindow.reportValidity()) return;
                event.target.disabled = true;
                try {
                    await API.request(`/api/ai/profiles/opencode/${encodeURIComponent(profileId)}`, {
                        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({name: profileId, auth_type: 'chatgpt', source: 'oauth', model: select.value, reasoning_effort: effort.value, context_window: contextWindow.value ? Number(contextWindow.value) : null})
                    });
                    await this.verify(profileId, box.querySelector('[data-connected-default]').checked);
                } catch (error) { view.status.textContent = error.message; event.target.disabled = false; }
            };
        } catch (error) { if (view.request === this.loginRequest) view.status.textContent = error.message; }
    },

    async verify(profileId, makeDefault = false) {
        const view = this.connectionModal(profileId, makeDefault);
        view.status.textContent = '正在验证连接，将执行一次简短的模型调用…';
        const buttons = view.element.querySelectorAll('[data-connection-login], [data-connection-verify]');
        buttons.forEach(button => { button.disabled = true; });
        try {
            const listing = await API.aiProfiles.list();
            const profile = listing.profiles.find(item => item.name === profileId && item.engine === 'opencode');
            if (!profile) throw new Error('Profile 不存在或已停用');
            const result = await API.post(`/api/engines/profiles/${encodeURIComponent(profileId)}/test`, {
                model: profile.model, reasoning_effort: profile.reasoning_effort, mode: 'tools'
            });
            if (view.request !== this.loginRequest) return;
            if (result.status !== 'verified') throw new Error(result.message || '连接未通过验证，请检查登录状态和模型配置后重试。');
            if (makeDefault) await API.aiProfiles.setDefault(profileId);
            view.status.textContent = makeDefault ? '连接成功，已设为默认 Profile。' : '连接成功，可以在 Profile 卡片或聊天设置中选择使用。';
            await CodexCenter.load();
        } catch (error) { if (view.request === this.loginRequest) view.status.textContent = error.message; }
        finally { if (view.request === this.loginRequest) buttons.forEach(button => { button.disabled = false; }); }
    },

    async retire(profileId) {
        if (!await UI.confirm(`停用 Profile “${profileId}”？已接受的任务会保留原配置；绑定此 Profile 的聊天需要重新选择。`, {
            title: '停用 OpenCode Profile', confirmText: '停用', variant: 'danger'
        })) return;
        try {
            await API.delete(`/api/ai/profiles/opencode/${encodeURIComponent(profileId)}`);
            await CodexCenter.load();
            UI.showSuccess('Profile 已停用');
        } catch (error) { UI.showError(error.message); }
    },

    async showRunDetails(runId, nativeRequestId = '') {
        try {
            const detail = await API.get(`/api/engines/runs/${encodeURIComponent(runId)}`);
            const esc = value => this.escape(value);
            let modal = document.getElementById('aiRunDetailModal');
            if (!modal) {
                modal = document.createElement('div'); modal.id = 'aiRunDetailModal'; modal.className = 'modal fade'; modal.tabIndex = -1;
                modal.setAttribute('aria-labelledby', 'aiRunDetailTitle');
                modal.innerHTML = `<div class="modal-dialog modal-lg modal-dialog-scrollable"><div class="modal-content"><div class="modal-header"><h5 class="modal-title" id="aiRunDetailTitle">任务过程</h5><button class="btn-close" type="button" data-bs-dismiss="modal" aria-label="关闭"></button></div><div class="modal-body"></div></div></div>`;
                document.body.appendChild(modal);
            }
            const statusLabels = { queued: '排队中', preparing: '准备输入', running: '运行中', completed: '已完成', skipped: '正常跳过', failed: '失败', cancelled: '已取消', interrupted_unknown: '结果待核对' };
            const deliveryLabels = { pending: '待交付', delivered: '已交付', sent: '已发送', failed: '交付失败', blocked: '已阻止交付', not_required: '无需交付' };
            modal.querySelector('.modal-body').innerHTML = `<div class="codex-job-input-summary"><span>状态 <strong>${esc(statusLabels[detail.status] || detail.status)}</strong></span><span>交付 <strong>${esc(deliveryLabels[detail.delivery_status] || detail.delivery_status)}</strong></span><span>ID ${esc(runId.slice(0,12))}</span></div>
                <div class="codex-event-list">${detail.attempts.map(attempt => {
                    const candidate = detail.route.candidates[attempt.candidate_index] || {};
                    return `<div class="codex-event"><div class="codex-event-head"><strong>${esc(({codex:'Codex',opencode:'OpenCode',api:'API'})[candidate.engine] || candidate.engine)} · ${esc(candidate.profile_id || 'API 模型')}</strong><span>${esc(statusLabels[attempt.status] || attempt.status)}</span></div><div class="codex-event-summary">${esc(candidate.model)}${attempt.result?.error_code ? ` · ${esc(attempt.result.error_code)}` : ''}</div></div>`;
                }).join('') || '<div class="codex-empty">任务尚未开始执行</div>'}</div>
                <div class="d-flex flex-wrap gap-2 mt-3" data-run-operations>${detail.operations.map(operation => `<button type="button" class="btn btn-sm btn-outline-secondary" data-operation="${esc(operation.operation_id)}">核对 ${esc(operation.kind)} · ${esc(operation.status)}</button>`).join('')}</div><div data-operation-detail class="mt-3 small" role="status"></div>`;
            if (nativeRequestId) {
                const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-sm btn-outline-primary mt-3';
                button.textContent = '查看 Codex 执行过程';
                button.onclick = async () => {
                    const hidden = new Promise(resolve => modal.addEventListener('hidden.bs.modal', resolve, { once: true }));
                    bootstrap.Modal.getInstance(modal).hide();
                    await hidden;
                    await LLMManager.showCodexJobDetails(nativeRequestId);
                };
                modal.querySelector('.modal-body').appendChild(button);
            }
            modal.querySelectorAll('[data-operation]').forEach(button => button.addEventListener('click', async () => {
                const box = modal.querySelector('[data-operation-detail]');
                try {
                    const operation = await API.get(`/api/engines/operations/${encodeURIComponent(button.dataset.operation)}`);
                    box.textContent = `操作状态：${operation.status}`;
                    if (operation.kind === 'image_job' && operation.status === 'unknown') {
                        for (const outcome of ['completed', 'failed']) {
                            const resolve = document.createElement('button'); resolve.type = 'button'; resolve.className = 'btn btn-sm btn-outline-primary ms-2';
                            resolve.textContent = outcome === 'completed' ? '确认图片已完整登记' : '确认生成失败且无图片';
                            resolve.onclick = async () => {
                                const evidence = window.prompt('填写核对依据（至少 8 个字符）；此操作不会重新生成或发送。');
                                if (!evidence) return;
                                try { await API.post(`/api/engines/operations/${encodeURIComponent(operation.operation_id)}/reconcile`, { outcome, evidence }); await this.showRunDetails(runId, nativeRequestId); }
                                catch (error) { box.textContent = error.message; }
                            };
                            box.appendChild(resolve);
                        }
                    }
                } catch (error) { box.textContent = error.message; }
            }));
            bootstrap.Modal.getOrCreateInstance(modal).show();
        } catch (error) { UI.showError(`读取任务详情失败：${error.message}`); }
    }
};
