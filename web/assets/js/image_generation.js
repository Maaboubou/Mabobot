/* Image channels reuse the console's forms, list surfaces and versioned saves. */
const ImageGeneration = {
    data: null,
    profiles: [],
    busy: false,
    loading: null,
    testing: new Set(),
    esc(value) { return UI.escapeHtml(String(value ?? '')); },

    async catalog() {
        if (!this.loading) {
            this.loading = API.get('/api/image-generation').then(data => {
                this.data = data;
                return data;
            }).finally(() => { this.loading = null; });
        }
        return this.loading;
    },

    async load() {
        const root = document.getElementById('imageGenerationRoot');
        try {
            await this.catalog();
            this.render();
        } catch (error) {
            root.innerHTML = `<div class="alert alert-danger" role="alert">${this.esc(error.message)} <button class="btn btn-sm btn-outline-secondary" onclick="ImageGeneration.load()">重试</button></div>`;
        }
    },

    render() {
        const root = document.getElementById('imageGenerationRoot');
        if (!root || !this.data) return;
        const channels = this.data.channels || [];
        root.innerHTML = `<div class="workspace-toolbar image-generation-toolbar">
            <h5><i class="bi bi-images me-2" aria-hidden="true"></i>图片生成</h5>
            <div class="image-generation-actions"><label class="image-generation-mode">默认模式
                <select class="form-select form-select-sm" id="imageGenerationMode" onchange="ImageGeneration.setMode(this.value)">
                    <option value="native" ${this.data.mode === 'native' ? 'selected' : ''}>Codex 内置</option>
                    <option value="api" ${this.data.mode === 'api' ? 'selected' : ''}>API</option>
                </select></label><button class="btn btn-primary btn-sm" onclick="ImageGeneration.edit()"><i class="bi bi-plus-lg me-1" aria-hidden="true"></i>新增配置</button>
            </div></div>
            <div class="image-request-override">
                <label class="form-check form-switch"><input type="checkbox" class="form-check-input" id="imageGenerationRequestOverride"
                    ${this.data.allow_request_override ? 'checked' : ''} onchange="ImageGeneration.setRequestOverride(this.checked)">
                    <span>允许聊天请求临时切换生图模式</span></label>
                <p>用户明确指定时，仅本次任务改用 API 或 Codex 内置；未指定时使用默认模式。API 使用下方已启用的渠道。</p>
            </div>
            <div class="image-channel-list" aria-label="按尝试顺序排列的生图渠道">${channels.length ? channels.map((c, i) => `
                <article class="image-channel-row ${c.enabled ? '' : 'image-channel-off'}" draggable="true" data-image-channel="${this.esc(c.id)}">
                    <span class="image-channel-grip" title="拖动排序"><i class="bi bi-grip-vertical" aria-hidden="true"></i></span>
                    <input type="checkbox" class="form-check-input" ${c.enabled ? 'checked' : ''} aria-label="启用 ${this.esc(c.name)}" onchange="ImageGeneration.toggle('${this.esc(c.id)}', this.checked)">
                    <div class="image-channel-identity"><strong>${this.esc(c.name)}</strong><span>${this.esc(c.model)}</span></div>
                    <span class="image-channel-kind">${c.provider === 'openai' ? 'OpenAI' : '兼容接口'} · ${c.protocol === 'responses' ? 'Responses' : 'Images'}</span>
                    <div class="image-channel-operations">
                        <button class="btn btn-sm btn-outline-secondary" title="上移" aria-label="上移 ${this.esc(c.name)}" ${i === 0 ? 'disabled' : ''} onclick="ImageGeneration.move('${this.esc(c.id)}', -1)"><i class="bi bi-arrow-up"></i></button>
                        <button class="btn btn-sm btn-outline-secondary" title="下移" aria-label="下移 ${this.esc(c.name)}" ${i === channels.length - 1 ? 'disabled' : ''} onclick="ImageGeneration.move('${this.esc(c.id)}', 1)"><i class="bi bi-arrow-down"></i></button>
                        <button class="btn btn-sm btn-outline-secondary" onclick="ImageGeneration.edit('${this.esc(c.id)}')">编辑</button>
                        <button class="btn btn-sm btn-outline-secondary" title="调用此渠道生成一张测试图片" ${this.testing.has(c.id) ? 'disabled' : ''} onclick="ImageGeneration.test('${this.esc(c.id)}', this)">${this.testing.has(c.id) ? '生成中…' : '测试生图'}</button>
                        <button class="btn btn-sm btn-outline-secondary" title="删除" aria-label="删除 ${this.esc(c.name)}" onclick="ImageGeneration.remove('${this.esc(c.id)}')"><i class="bi bi-trash"></i></button>
                    </div>
                </article>`).join('') : '<div class="image-channel-empty">暂无 API 配置</div>'}</div>
            <div class="image-generation-footnote">${this.data.mode === 'api' ? '按顺序尝试已勾选配置，全部失败后尝试内置生图' : '内置生图失败后结束任务'}</div>
            <div id="imageGenerationTestResult" aria-live="polite"></div>
            <details class="image-call-history"><summary>最近调用</summary><div id="imageCallHistory"></div>
                <button class="btn btn-sm btn-outline-secondary mt-2" onclick="ImageGeneration.loadHistory()">刷新</button></details>`;
        this.bindSort(root);
        this.loadHistory();
    },

    async loadHistory() {
        const root = document.getElementById('imageCallHistory');
        if (!root) return;
        try {
            const {runs = []} = await API.get('/api/image-generation/history');
            if (!root.isConnected) return;
            const status = {running: '进行中', completed: '成功', failed: '失败', partial: '部分完成', cancelled: '已取消'};
            root.innerHTML = runs.slice(0, 30).map(run => `<details class="image-call-row"><summary>
                <span>${this.esc(new Date(run.at).toLocaleString())}</span><span>${this.esc(run.chat || '生图任务')}</span><span>${this.esc(status[run.status] || run.status)}</span></summary>
                ${run.mode ? `<div class="image-call-attempt">实际方式：API${run.selected_by === 'explicit_request' ? ` · 用户本次指定（默认：${run.default_mode === 'native' ? 'Codex 内置' : 'API'}）` : ' · 按配置执行'}</div>` : ''}
                ${(run.attempts || []).map(a => `<div class="image-call-attempt"><strong>${this.esc(a.channel)}</strong><span>${this.esc(a.model)}</span>
                    ${a.protocol ? `<span>${this.esc(a.protocol === 'responses' ? 'Responses' : 'Images')} · ${a.operation === 'edit' ? '编辑' : '生成'}</span>` : ''}
                    <span>${this.esc(status[a.status] || a.status)}${a.elapsed !== undefined ? ` · ${this.esc(a.elapsed)} 秒` : ''}</span>
                    ${a.parameters ? `<span>请求尺寸 ${this.esc(a.parameters.size || 'auto')} · 质量 ${this.esc(a.parameters.quality || 'auto')} · ${this.esc(a.parameters.n)} 张</span>` : ''}
                    ${a.parameters?.complexity ? `<span>复杂度 ${this.esc(a.parameters.complexity)}</span>` : ''}
                    ${a.parameters?.output_format ? `<span>格式 ${this.esc(a.parameters.output_format)}${a.parameters.output_format === 'gif' ? ` · ${this.esc(a.parameters.frame_count ?? 9)} 帧 · 每帧 ${this.esc(a.parameters.frame_duration_ms ?? 200)} ms` : ''}</span>` : ''}
                    ${a.output_sizes?.length ? `<span>实际尺寸 ${this.esc(a.output_sizes.join('、'))}</span>` : ''}
                    ${(a.outputs || []).filter(o => o.format === 'gif').map(o => `<span>实际动画 ${this.esc(o.frames)} 帧 · ${this.esc(o.duration_ms / 1000)} 秒</span>`).join('')}
                    ${a.error ? `<span class="text-danger">${this.esc(a.error)}${a.error_type ? `（${this.esc(a.error_type)}）` : ''}</span>` : ''}</div>`).join('')}</details>`).join('') || '<p class="text-secondary small mt-2">暂无调用</p>';
        } catch (error) {
            if (root.isConnected) root.textContent = error.message;
        }
    },

    async persist(next) {
        if (this.busy) throw Error('配置正在保存');
        this.busy = true;
        const root = document.getElementById('imageGenerationRoot');
        if (root) root.inert = true;
        try {
            this.data = await API.put('/api/image-generation', {
                version: this.data.version, mode: next.mode, channels: next.channels,
                allow_request_override: next.allow_request_override === true
            });
            this.render();
        } catch (error) {
            this.render();
            throw error;
        } finally {
            this.busy = false;
            if (root) root.inert = false;
        }
    },

    async change(edit) {
        if (this.busy) return;
        const next = structuredClone(this.data);
        edit(next);
        try { await this.persist(next); } catch (error) { UI.showError(error.message); }
    },
    setMode(mode) { return this.change(data => { data.mode = mode; }); },
    setRequestOverride(enabled) { return this.change(data => { data.allow_request_override = enabled; }); },
    toggle(id, enabled) { return this.change(data => { data.channels.find(c => c.id === id).enabled = enabled; }); },
    move(id, direction) {
        return this.change(data => {
            const from = data.channels.findIndex(c => c.id === id), to = from + direction;
            if (from < 0 || to < 0 || to >= data.channels.length) return;
            data.channels.splice(to, 0, ...data.channels.splice(from, 1));
        });
    },
    bindSort(root) {
        root.querySelectorAll('[data-image-channel]').forEach(row => {
            row.addEventListener('dragstart', event => {
                event.dataTransfer.setData('text/plain', row.dataset.imageChannel);
                event.dataTransfer.effectAllowed = 'move';
            });
            row.addEventListener('dragover', event => event.preventDefault());
            row.addEventListener('drop', event => {
                event.preventDefault();
                const id = event.dataTransfer.getData('text/plain');
                const from = this.data.channels.findIndex(c => c.id === id);
                const to = this.data.channels.findIndex(c => c.id === row.dataset.imageChannel);
                if (from >= 0 && to >= 0 && from !== to) this.move(id, to - from);
            });
        });
    },
    async remove(id) {
        const channel = this.data.channels.find(c => c.id === id);
        if (await UI.confirm(`删除「${channel.name}」的生图配置？引用它的队列将跳过此项。`, {title: '删除配置', confirmText: '删除'})) {
            await this.change(data => { data.channels = data.channels.filter(c => c.id !== id); });
        }
    },

    field(label, control, wide = false, attrs = '') {
        return `<label class="image-channel-field ${wide ? 'is-wide' : ''}" ${attrs}><span>${label}</span>${control}</label>`;
    },
    input(name, value = '', attrs = '') {
        return `<input class="form-control" name="${name}" value="${this.esc(value)}" ${attrs}>`;
    },
    select(name, values, value, attrs = '') {
        return `<select class="form-select" name="${name}" ${attrs}>${values.map(item => {
            const [v, label] = Array.isArray(item) ? item : [item, item];
            return `<option value="${this.esc(v)}" ${String(v) === String(value) ? 'selected' : ''}>${this.esc(label)}</option>`;
        }).join('')}</select>`;
    },

    modelOptions(models, value, others = []) {
        const ids = [...new Set(models)];
        const extra = [...new Set(others)].filter(id => !ids.includes(id));
        const option = id => `<option value="${this.esc(id)}" ${id === value ? 'selected' : ''}>${this.esc(id)}</option>`;
        const current = value && value !== '__manual__' && !ids.includes(value) && !extra.includes(value)
            ? `<option value="${this.esc(value)}" selected>当前：${this.esc(value)}</option>` : '';
        return `${!value ? '<option value="">选择模型</option>' : ''}${current}${ids.map(option).join('')}
            ${extra.length ? `<optgroup label="其他模型（能力未确认）">${extra.map(option).join('')}</optgroup>` : ''}
            <option value="__manual__" ${value === '__manual__' ? 'selected' : ''}>手动输入…</option>`;
    },

    modelField(name, label, value, models, refresh = false) {
        return `<div class="image-channel-field is-wide" ${name === 'response_model' ? 'data-responses-field' : ''}>
            <div class="image-model-heading"><label for="image-picker-${name}">${label}</label>${refresh
                ? '<button type="button" class="btn btn-sm btn-outline-secondary" data-refresh-image-models onclick="ImageGeneration.refreshModels(true)" title="从当前连接获取模型" aria-label="刷新模型列表"><i class="bi bi-arrow-clockwise" aria-hidden="true"></i></button>' : ''}</div>
            <select id="image-picker-${name}" name="${name}" class="form-select" ${name === 'model' ? 'required' : ''}>${this.modelOptions(models, value)}</select>
            <input class="form-control" name="${name}_custom" aria-label="自定义${label}" maxlength="128" placeholder="模型 ID" value="${this.esc(value)}" hidden disabled>
            ${refresh ? '<span class="image-model-source" data-image-model-source role="status">内置列表</span>' : ''}
        </div>`;
    },

    modelValue(form, name) {
        return form.elements[name].value === '__manual__' ? form.elements[`${name}_custom`].value.trim() : form.elements[name].value;
    },

    async refreshModels(force = false) {
        const form = document.getElementById('imageChannelForm');
        if (!form || !this.editing) return;
        this.discoveryAbort?.abort();
        const requestId = this.modelRequestId = (this.modelRequestId || 0) + 1;
        const el = form.elements, status = form.querySelector('[data-image-model-source]');
        const button = form.querySelector('[data-refresh-image-models]');
        status.title = '';
        button.disabled = false;
        const existing = this.data.channels.find(c => c.id === this.editing.id);
        const payload = {provider: el.provider.value, base_url: el.base_url.value.trim(),
            profile_id: el.profile_id.value, refresh: force, channel_id: existing?.id || ''};
        if (el.api_key.value && !payload.profile_id) payload.api_key = el.api_key.value;
        if (payload.provider === 'openai') payload.headers = {};
        else if (el.headers.value.trim()) {
            try { payload.headers = JSON.parse(el.headers.value); }
            catch (_) { status.textContent = '请检查请求头 JSON'; return; }
        }
        if (!payload.profile_id && payload.provider === 'openai' && !payload.api_key && !existing?.key_configured) {
            status.textContent = force ? '填写密钥后可获取模型' : '内置列表';
            return;
        }
        if (!payload.profile_id && !payload.base_url) { status.textContent = '请先填写 API 地址'; return; }
        const update = (name, models, others = [], live = false) => {
            const picker = el[name], wasManual = picker.value === '__manual__';
            const current = wasManual ? '__manual__' : picker.value;
            const choices = [...models, ...others];
            const keep = existing || picker.dataset.touched || wasManual || !live || !choices.length;
            const selected = choices.includes(current) || keep ? current : choices[0];
            picker.innerHTML = this.modelOptions(models, selected || (live ? choices[0] || '' : ''), others);
        };
        // A different key/endpoint must not continue showing the previous server's catalog.
        update('model', this.data.models || []);
        update('response_model', this.data.response_models || []);
        status.textContent = '正在获取…';
        button.disabled = true;
        const controller = new AbortController();
        this.discoveryAbort = controller;
        try {
            const result = await API.request('/api/image-generation/models/discover', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload), signal: controller.signal
            });
            if (!form.isConnected || requestId !== this.modelRequestId) return;
            update('model', result.models || [], result.other_models || [], true);
            update('response_model', result.response_models || [], [], true);
            const count = (result.models || []).length;
            status.textContent = payload.provider === 'openai'
                ? count ? `已获取 ${count} 个生图模型` : '接口未返回生图模型'
                : `已获取 ${count + (result.other_models || []).length} 个模型`;
            status.title = '列表来自当前连接；实际调用权限以渠道返回为准';
            this.syncEditor(form);
        } catch (error) {
            if (error.name === 'AbortError' || !form.isConnected || requestId !== this.modelRequestId) return;
            status.textContent = '获取失败 · 使用内置列表';
            status.title = error.message;
        } finally {
            if (requestId === this.modelRequestId) button.disabled = false;
        }
    },

    async edit(id) {
        try {
            if (!this.data) await this.catalog();
            const listing = await API.codexProfiles.list();
            this.profiles = (listing.profiles || []).filter(p => p.auth_type === 'api_key');
        } catch (error) { UI.showError(error.message); return; }
        const existing = id && this.data.channels.find(c => c.id === id);
        const c = structuredClone(existing || {id: [...crypto.getRandomValues(new Uint8Array(16))].map(b => b.toString(16).padStart(2, '0')).join(''), name: '', enabled: false,
            provider: 'openai', protocol: 'images', profile_id: '', base_url: 'https://api.openai.com/v1',
            model: this.data.models[0], response_model: '', timeout: 300, edit_transport: 'json', parameters: {}});
        this.editing = c;
        this.editingVersion = this.data.version;
        const p = c.parameters || {};
        const profileOptions = this.profiles.map(p => [p.name, `复用 Codex 配置：${p.name}`]);
        if (c.profile_id && !this.profiles.some(p => p.name === c.profile_id)) profileOptions.push([c.profile_id, `复用 Codex 配置：${c.profile_id} · 不可用`]);
        const root = document.getElementById('imageChannelModal');
        root.querySelector('.modal-title').textContent = existing ? '编辑生图配置' : '新增生图配置';
        root.querySelector('.modal-body').innerHTML = `<form id="imageChannelForm">
            <div class="image-channel-fields">
                ${this.field('名称', this.input('name', c.name, 'required maxlength="80" autocomplete="off"'))}
                ${this.field('类型', this.select('provider', [['openai', 'OpenAI 官方'], ['compatible', '兼容接口']], c.provider))}
                ${this.field('接口', this.select('protocol', [['images', 'Images'], ['responses', 'Responses']], c.protocol))}
                ${this.field('API 连接来源', this.select('profile_id', [['', '手动填写地址和密钥'], ...profileOptions], c.profile_id))}
                ${this.field('API 地址', this.input('base_url', c.base_url, 'type="url"'), true, 'data-manual-connection')}
                ${this.field('API Key', this.input('api_key', '', `type="password" autocomplete="new-password" placeholder="${c.key_configured ? '已配置；留空保持' : '输入密钥'}"`), true, 'data-manual-connection')}
                ${this.modelField('model', '生图模型', c.model, this.data.models, true)}
                ${this.modelField('response_model', '主模型', c.response_model, this.data.response_models || [])}
                ${this.field('尺寸', this.input('size', p.size || 'auto', 'list="imageSizeOptions"'))}
                <datalist id="imageSizeOptions"><option value="auto"></option><option value="512x512"></option><option value="1024x1024"></option><option value="1536x1024"></option><option value="1024x1536"></option><option value="1536x864"></option></datalist>
                ${this.field('质量', this.select('quality', ['auto', 'low', 'medium', 'high', 'xhigh', 'max'], p.quality || 'auto'))}
                ${this.field('构图复杂度', this.select('complexity', [['', '接口默认'], ['low', 'low'], ['medium', 'medium'], ['high', 'high']], p.complexity || ''), false, 'data-complexity-field')}
                ${this.field('格式', this.select('output_format', ['png', 'jpeg', 'webp', ['gif', 'GIF 动图'], ['svg', 'SVG 矢量图']], p.output_format || 'png'))}
                <div class="image-channel-field is-wide text-secondary small" data-generation-format-field>GIF / SVG 仅支持文生图，不能带参考图；不支持流式输出。</div>
                ${this.field('动画帧数', this.input('frame_count', p.frame_count ?? 9, 'type="number" min="2" max="64" required'), false, 'data-gif-field')}
                ${this.field('每帧时长（毫秒）', this.input('frame_duration_ms', p.frame_duration_ms ?? 200, 'type="number" min="20" max="2000" required title="接口按 10 毫秒取整"'), false, 'data-gif-field')}
                ${this.field('背景', this.select('background', [['auto', '自动'], ['opaque', '不透明'], ['transparent', '透明']], p.background || 'auto'))}
                ${this.field('默认数量', this.input('n', p.n || 1, 'type="number" min="1" max="10" required'))}
                ${this.field('超时（秒）', this.input('timeout', c.timeout, 'type="number" min="10" max="3600" required'))}
            </div>
            <details class="image-channel-advanced"><summary>高级设置</summary><div class="image-channel-fields">
                ${this.field('压缩质量', this.input('output_compression', p.output_compression ?? '', 'type="number" min="0" max="100" placeholder="默认"'), false, 'data-compression-field')}
                ${this.field('输入保真度', this.select('input_fidelity', [['', '默认'], ['low', '低'], ['high', '高']], p.input_fidelity || ''), false, 'data-fidelity-field')}
                ${this.field('内容过滤', this.select('moderation', ['auto', 'low'], p.moderation || 'auto'))}
                ${this.field('编辑传输', this.select('edit_transport', [['json', 'JSON'], ['multipart', 'Multipart']], c.edit_transport), false, 'data-images-field')}
                ${this.field('流式接收', this.select('stream', [['false', '关闭'], ['true', '开启']], String(p.stream || false)), false, 'data-stream-toggle-field')}
                ${this.field('预览帧数', this.input('partial_images', p.partial_images || 0, 'type="number" min="0" max="3"'), false, 'data-stream-field')}
                ${this.field('请求头', `<textarea class="form-control font-monospace" name="headers" rows="3" placeholder="${c.headers_configured ? '已配置；留空保持，{} 清空' : '{}'}" spellcheck="false"></textarea>`, true, 'data-compatible-field')}
                ${this.field('额外参数', `<textarea class="form-control font-monospace" name="extra_body" rows="4" placeholder="${c.extra_body_configured ? '已配置；留空保持，{} 清空' : '{}'}" spellcheck="false"></textarea>`, true, 'data-compatible-field')}
            </div></details><p class="text-danger small mt-3 mb-0" data-image-error role="alert" hidden></p>
        </form>`;
        const form = root.querySelector('form');
        form.addEventListener('change', event => {
            const el = form.elements;
            if (event.target.name === 'profile_id') {
                const profile = this.profiles.find(p => p.name === el.profile_id.value);
                if (profile && profile.base_url?.replace(/\/$/, '') !== 'https://api.openai.com/v1') {
                    el.provider.value = 'compatible';
                    el.edit_transport.value = 'multipart';
                }
            }
            if (event.target.name === 'provider') el.edit_transport.value = el.provider.value === 'openai' ? 'json' : 'multipart';
            if (['model', 'response_model'].includes(event.target.name)) event.target.dataset.touched = 'true';
            this.syncEditor(form);
            if (['provider', 'profile_id', 'base_url', 'api_key', 'headers'].includes(event.target.name)) this.refreshModels();
        });
        form.addEventListener('input', event => { if (event.target.name === 'model_custom') this.syncEditor(form); });
        form.addEventListener('submit', event => { event.preventDefault(); this.saveEditor(); });
        this.syncEditor(form);
        bootstrap.Modal.getOrCreateInstance(root).show();
        if (!root.dataset.discoveryBound) {
            root.dataset.discoveryBound = 'true';
            root.addEventListener('hidden.bs.modal', () => {
                this.modelRequestId = (this.modelRequestId || 0) + 1;
                this.discoveryAbort?.abort();
            });
        }
        this.refreshModels();
    },

    syncEditor(form) {
        const el = form.elements, official = el.provider.value === 'openai', responses = el.protocol.value === 'responses';
        const model = this.modelValue(form, 'model');
        const image26 = model.startsWith('gpt-image-2.6-');
        const specialFormat = ['gif', 'svg'].includes(el.output_format.value);
        const show = (selector, visible) => form.querySelectorAll(selector).forEach(row => {
            row.hidden = !visible;
            row.querySelectorAll('input,select,textarea').forEach(input => { input.disabled = !visible; });
        });
        show('[data-manual-connection]', !el.profile_id.value);
        show('[data-responses-field]', responses);
        show('[data-images-field]', !responses);
        show('[data-compatible-field]', !official);
        show('[data-stream-toggle-field]', !specialFormat);
        show('[data-stream-field]', !specialFormat && el.stream.value === 'true');
        show('[data-compression-field]', !specialFormat && (!official || ['jpeg', 'webp'].includes(el.output_format.value)));
        show('[data-complexity-field]', !responses && (!official || image26));
        show('[data-generation-format-field]', specialFormat);
        show('[data-gif-field]', el.output_format.value === 'gif');
        el.size.closest('label').querySelector('span').textContent = el.output_format.value === 'gif' ? '每帧尺寸' : '尺寸';
        el.n.max = el.output_format.value === 'svg' ? '1' : '10';
        [...el.output_format.options].forEach(option => {
            option.disabled = ['gif', 'svg'].includes(option.value) && (responses || official && !image26);
        });
        el.output_format.setCustomValidity(specialFormat && (responses || official && !image26)
            ? 'GIF / SVG 需要支持的 GPT Image 2.6 Images 配置' : '');
        show('[data-fidelity-field]', !official || ['gpt-image-1.5', 'gpt-image-1-mini'].includes(model));
        el.base_url.readOnly = official;
        if (official) el.base_url.value = 'https://api.openai.com/v1';
        for (const name of ['model', 'response_model']) {
            const manual = el[name].value === '__manual__', active = name === 'model' || responses;
            el[`${name}_custom`].hidden = !manual;
            el[`${name}_custom`].disabled = !manual || !active;
            el[`${name}_custom`].required = manual && active;
            el[name].required = active;
        }
        [...el.quality.options].forEach(option => {
            option.disabled = official && ['xhigh', 'max'].includes(option.value) && !model.startsWith('gpt-image-2.5-') && !image26;
        });
    },

    async saveEditor() {
        if (this.busy) return;
        const root = document.getElementById('imageChannelModal'), form = root.querySelector('form');
        if (!form.reportValidity()) return;
        const el = form.elements, c = structuredClone(this.editing), errorBox = form.querySelector('[data-image-error]');
        try {
            if (this.editingVersion !== this.data.version) throw Error('配置已更新，请重新打开编辑器');
            for (const name of ['name', 'provider', 'protocol', 'profile_id', 'base_url', 'edit_transport']) c[name] = el[name].value.trim();
            c.model = this.modelValue(form, 'model');
            c.response_model = this.modelValue(form, 'response_model');
            c.timeout = Number(el.timeout.value);
            if (el.api_key.value && !el.profile_id.value) c.api_key = el.api_key.value;
            c.parameters = {};
            for (const name of ['size', 'quality', 'output_format', 'background', 'moderation', 'input_fidelity', 'complexity']) {
                if (!el[name].disabled && el[name].value) c.parameters[name] = el[name].value;
            }
            for (const name of ['n', 'output_compression', 'partial_images', 'frame_count', 'frame_duration_ms']) {
                if (!el[name].disabled && el[name].value !== '') c.parameters[name] = Number(el[name].value);
            }
            c.parameters.stream = !el.stream.disabled && el.stream.value === 'true';
            for (const name of ['headers', 'extra_body']) {
                if (c.provider === 'openai') c[name] = {};
                else if (el[name].value.trim()) {
                    const value = JSON.parse(el[name].value);
                    if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('请求头和额外参数必须为 JSON 对象');
                    c[name] = value;
                }
            }
            const next = structuredClone(this.data), index = next.channels.findIndex(item => item.id === c.id);
            if (index < 0) next.channels.push(c); else next.channels[index] = c;
            form.inert = true;
            await this.persist(next);
            bootstrap.Modal.getInstance(root).hide();
            this.editing = null;
            form.reset();
            UI.showSuccess('生图配置已保存');
        } catch (error) {
            errorBox.textContent = error instanceof SyntaxError ? '请检查高级设置中的 JSON 格式' : error.message;
            errorBox.hidden = false;
        } finally { form.inert = false; }
    },

    async test(id, button) {
        if (this.testing.has(id)) return;
        this.testing.add(id);
        let root = document.getElementById('imageGenerationTestResult');
        button.disabled = true;
        button.textContent = '生成中…';
        root.innerHTML = '';
        try {
            const result = await API.post(`/api/image-generation/${encodeURIComponent(id)}/test`, {});
            root = document.getElementById('imageGenerationTestResult');
            root.innerHTML = `<details class="image-test-result" open><summary>测试完成</summary>
                <img src="${this.esc(result.preview)}" alt="生图渠道测试结果"><span>${this.esc(result.attempts[0]?.elapsed)} 秒</span></details>`;
        } catch (error) {
            root = document.getElementById('imageGenerationTestResult');
            root.innerHTML = `<div class="alert alert-danger mt-3" role="alert">${this.esc(error.message)}</div>`;
        } finally {
            this.testing.delete(id);
            const current = document.querySelector(`[data-image-channel="${CSS.escape(id)}"] button[title="调用此渠道生成一张测试图片"]`);
            if (current) { current.disabled = false; current.textContent = '测试生图'; }
            this.loadHistory();
        }
    },

    policyEditor(policy = {}, channels = null, attrs = '', parent = {}, allowOverrideControl = true) {
        const value = {mode: policy.mode || 'inherit', channel_ids: policy.channel_ids ?? null,
            allow_request_override: policy.allow_request_override ?? null};
        const catalog = channels || this.data?.channels || [];
        const ids = value.channel_ids ?? parent.channel_ids ?? catalog.filter(c => c.enabled).map(c => c.id);
        const order = [...ids, ...catalog.map(c => c.id).filter(id => !ids.includes(id))];
        const mode = this.select('image_policy_mode', [['inherit', '跟随默认'], ['native', 'Codex 内置'], ['api', 'API']], value.mode, 'data-image-policy-mode');
        const inheritedOverride = parent.allow_request_override ?? this.data?.allow_request_override ?? false;
        const allowOverride = allowOverrideControl && (value.allow_request_override ?? inheritedOverride);
        const override = allowOverrideControl ? this.field('本次模式切换', this.select('image_policy_override',
            [['inherit', '跟随默认'], ['true', '允许用户指定'], ['false', '不允许']],
            value.allow_request_override === null ? 'inherit' : String(value.allow_request_override), 'data-image-policy-override')) : '';
        return `<div class="image-policy-editor" data-image-override-control="${allowOverrideControl}" data-image-inherited-override="${inheritedOverride}" data-image-parent="${this.esc(JSON.stringify(parent))}" data-image-catalog="${this.esc(JSON.stringify(catalog.map(c => ({id: c.id, name: c.name, enabled: c.enabled}))))}">
            <textarea hidden data-image-policy-value ${attrs}>${this.esc(JSON.stringify(value))}</textarea>${mode}
            ${override}
            <details class="image-policy-queue" ${value.mode === 'native' && !allowOverride ? 'hidden' : ''}><summary>API 渠道 <span data-image-policy-source>${value.channel_ids === null ? '默认' : '自定义'}</span></summary>
                <div class="image-policy-items">${order.map(id => {
                    const channel = catalog.find(c => c.id === id) || {id, name: '已删除的配置', enabled: false};
                    return `<div class="image-policy-item" data-policy-channel="${this.esc(id)}"><label><input type="checkbox" data-image-policy-check ${ids.includes(id) ? 'checked' : ''} ${!channel.enabled ? 'disabled' : ''}> <span>${this.esc(channel.name)}${channel.enabled ? '' : ' · 停用'}</span></label>
                        <button type="button" class="btn btn-sm btn-outline-secondary" data-policy-move="-1" title="上移" aria-label="上移 ${this.esc(channel.name)}"><i class="bi bi-arrow-up"></i></button>
                        <button type="button" class="btn btn-sm btn-outline-secondary" data-policy-move="1" title="下移" aria-label="下移 ${this.esc(channel.name)}"><i class="bi bi-arrow-down"></i></button></div>`;
                }).join('') || '<span class="text-secondary small">暂无 API 配置</span>'}</div>
                <button type="button" class="btn btn-sm btn-link" data-policy-reset>恢复默认队列</button>
            </details></div>`;
    },

    updatePolicy(editor, queueChanged = false, reset = false) {
        const input = editor.querySelector('[data-image-policy-value]');
        const previous = JSON.parse(input.value);
        const override = editor.querySelector('[data-image-policy-override]')?.value || 'inherit';
        const value = {mode: editor.querySelector('[data-image-policy-mode]').value, channel_ids: reset ? null : queueChanged
            ? [...editor.querySelectorAll('[data-policy-channel]')].filter(row => row.querySelector('input').checked).map(row => row.dataset.policyChannel)
            : previous.channel_ids, allow_request_override: override === 'inherit' ? null : override === 'true'};
        input.value = JSON.stringify(value);
        const allowOverride = editor.dataset.imageOverrideControl === 'true'
            && (value.allow_request_override ?? editor.dataset.imageInheritedOverride === 'true');
        editor.querySelector('.image-policy-queue').hidden = value.mode === 'native' && !allowOverride;
        editor.querySelector('[data-image-policy-source]').textContent = value.channel_ids === null ? '默认' : '自定义';
        input.dispatchEvent(new Event('input', {bubbles: true}));
    }
};
window.ImageGeneration = ImageGeneration;
document.addEventListener('change', event => {
    const editor = event.target.closest('.image-policy-editor');
    if (editor && event.target.matches('[data-image-policy-mode], [data-image-policy-check], [data-image-policy-override]')) {
        ImageGeneration.updatePolicy(editor, event.target.matches('[data-image-policy-check]'));
    }
});
document.addEventListener('click', event => {
    const button = event.target.closest('[data-policy-move], [data-policy-reset]');
    if (!button) return;
    const editor = button.closest('.image-policy-editor');
    if (button.hasAttribute('data-policy-reset')) {
        ImageGeneration.updatePolicy(editor, false, true);
        const input = editor.querySelector('[data-image-policy-value]');
        const attrs = ['name', 'data-config-key', 'data-config-type', 'data-config-control', 'data-sensitive']
            .filter(a => input.hasAttribute(a)).map(a => `${a}="${ImageGeneration.esc(input.getAttribute(a))}"`).join(' ');
        editor.outerHTML = ImageGeneration.policyEditor(JSON.parse(input.value), JSON.parse(editor.dataset.imageCatalog), attrs,
            JSON.parse(editor.dataset.imageParent), editor.dataset.imageOverrideControl === 'true');
    } else {
        const row = button.closest('[data-policy-channel]'), direction = Number(button.dataset.policyMove);
        const sibling = direction < 0 ? row.previousElementSibling : row.nextElementSibling;
        if (sibling) {
            if (direction < 0) sibling.before(row); else sibling.after(row);
            ImageGeneration.updatePolicy(editor, true);
        }
    }
});
