/** Unified environment-tool updates and runtime maintenance. */
const SystemTools = {
    requestId: 0,
    pollTimer: null,
    busyActions: new Set(),
    expandedDetails: new Set(),
    checkErrors: {},
    checkErrorVersions: {},
    overview: null,

    esc(value) {
        return UI.escapeHtml(String(value ?? ''));
    },

    unwrap(response) {
        return response?.data || response || {};
    },

    operationActive(operation) {
        return Boolean(operation && ['queued', 'running', 'cancelling'].includes(operation.status));
    },

    operationLabel(operation) {
        return ({
            queued: '等待执行', running: '执行中', cancelling: '取消中',
            succeeded: '已完成', completed: '已完成', rolled_back: '已回退',
            failed: '失败', cancelled: '已取消', interrupted: '已中断'
        })[operation?.status] || operation?.status || '';
    },

    toolBusy(tool) {
        return Boolean(tool?.operation_running || this.operationActive(tool?.operation)
            || tool?.check_state === 'checking' || this.busyActions.has('check-all')
            || [...this.busyActions].some(key => key.endsWith(`:${tool?.id}`)));
    },

    checkError(tool) {
        return tool.check_error || this.checkErrors[tool.id] || '';
    },

    isEnvironment(tool) {
        return tool.category === 'maintenance' || ['ffmpeg', 'playwright'].includes(tool.id);
    },

    formatBeijing(value) {
        if (!value) return '';
        const date = new Date(value);
        if (!Number.isFinite(date.getTime())) return '';
        return new Intl.DateTimeFormat('zh-CN', {
            timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
        }).format(date);
    },

    versionText(value) {
        return value ? this.esc(value) : '<span class="text-muted">未检测</span>';
    },

    versionStatus(tool) {
        if (tool.operation_running || this.operationActive(tool.operation)) return ['primary', '正在处理'];
        if (tool.check_state === 'checking' || this.busyActions.has(`check:${tool.id}`)
            || (this.busyActions.has('check-all') && tool.actions?.check)) return ['muted', '正在检查'];
        if (tool.restart_required) return ['warning', '等待重启'];
        if (tool.health && tool.health !== 'ready') return ['danger', tool.health_label || '环境需检查'];
        if (this.isEnvironment(tool)) return ['success', '环境正常'];
        if (tool.update_available) return ['primary', '有更新'];
        if (this.checkError(tool) || tool.check_state === 'failed') return ['warning', '检查失败'];
        if (tool.checked_at && tool.available_version) return ['success', '已是最新'];
        return ['muted', '尚未检查'];
    },

    actionButton(tool, action, label, primary = false) {
        return `<button type="button" class="btn btn-sm ${primary ? 'btn-primary' : 'btn-light border'}" data-tool-action="${action}" data-focus-key="${this.esc(`${tool.id}:${action}`)}" ${this.toolBusy(tool) ? 'disabled' : ''}>${this.esc(label)}</button>`;
    },

    renderActions(tool) {
        const actions = tool.actions || {};
        const active = tool.operation_running || this.operationActive(tool.operation);
        const buttons = [];
        if (active) {
            buttons.push('<button type="button" class="btn btn-sm btn-light border" disabled>正在处理…</button>');
        } else if (actions.restart) {
            buttons.push(this.actionButton(tool, 'restart', '重启后生效', true));
        } else if (actions.repair && tool.health !== 'ready') {
            buttons.push(this.actionButton(tool, 'repair', '安装 / 修复', true));
        } else if (tool.update_available && actions.update) {
            buttons.push(this.actionButton(tool, 'update', `更新至 ${tool.available_version || '最新版'}`, true));
            if (this.checkError(tool) && actions.check) buttons.push(this.actionButton(tool, 'check', '重试检查'));
        } else if (actions.check && (this.checkError(tool) || tool.check_state === 'failed')) {
            buttons.push(this.actionButton(tool, 'check', '重试检查'));
        } else if (tool.id === 'codex' && actions.update && !tool.available_version) {
            buttons.push(this.actionButton(tool, 'update', '检查并更新', true));
        } else if (actions.check) {
            const checking = tool.check_state === 'checking' || this.busyActions.has(`check:${tool.id}`)
                || this.busyActions.has('check-all');
            buttons.push(this.actionButton(tool, 'check', checking ? '正在检查…' : tool.checked_at ? '重新检查' : '检查版本'));
        }
        buttons.push(`<button type="button" class="btn btn-sm system-tool-detail-toggle" data-detail-toggle="${this.esc(tool.id)}" data-focus-key="${this.esc(`${tool.id}:details`)}" aria-controls="system-tool-details-${this.esc(tool.id)}" aria-expanded="${this.expandedDetails.has(tool.id)}">详情<i class="bi bi-chevron-down" aria-hidden="true"></i></button>`);
        return buttons.join('');
    },

    renderOperation(tool, { history = false } = {}) {
        const operation = tool.operation;
        if (!operation) return '';
        const active = this.operationActive(operation);
        const failed = ['failed', 'interrupted'].includes(operation.status);
        if (history === (active || failed)) return '';
        const progress = Math.max(0, Math.min(Number(operation.progress || 0), 100));
        return `
            <div class="system-tool-operation ${active ? 'active' : ''} ${failed ? 'failed' : ''}" ${active || failed ? 'aria-live="polite"' : ''}>
                <div class="system-tool-operation-copy"><strong>${this.esc(this.operationLabel(operation))}</strong><span>${this.esc(operation.error || operation.message || '')}</span></div>
                ${active ? `<div class="system-tool-operation-progress"><div class="system-progress" role="progressbar" aria-label="${this.esc(tool.title)} 操作进度" aria-valuenow="${progress}" aria-valuemin="0" aria-valuemax="100"><span style="width:${progress}%"></span></div><small>${progress}%</small></div>` : ''}
            </div>`;
    },

    renderDetails(tool) {
        const details = tool.details || {};
        const components = Array.isArray(details.components) ? details.components : Object.values(details.components || {});
        const rows = [['来源', tool.source_label || '未识别'], ['管理方式', tool.managed ? 'Mabobot 管理' : '外部安装仅检测']];
        if (tool.description) rows.push(['用途', tool.description]);
        if (tool.path) rows.push(['调用路径', tool.path]);
        if (details.realpath && details.realpath !== tool.path) rows.push(['实际路径', details.realpath]);
        if (details.install_method) rows.push(['安装方式', details.install_method]);
        if (details.package_version) rows.push(['管理包', `static-ffmpeg ${details.package_version}`]);
        if (details.browser_version) rows.push(['浏览器版本', details.browser_version]);
        if (tool.last_attempt_at) rows.push(['最近尝试', `${this.formatBeijing(tool.last_attempt_at)} · 北京时间`]);
        if (tool.next_check_at) rows.push(['下次检查', `${this.formatBeijing(tool.next_check_at)} · 北京时间`]);
        components.forEach(component => {
            if (component?.name) rows.push([component.name, [component.version, component.source_label, component.path].filter(Boolean).join(' · ')]);
        });
        const actions = tool.actions || {};
        const buttons = [];
        if (actions.check && tool.update_available && !this.checkError(tool)) buttons.push(this.actionButton(tool, 'check', '重新检查'));
        if (actions.rollback) buttons.push(this.actionButton(tool, 'rollback', `回退${details.rollback_version ? `至 ${details.rollback_version}` : '至上一版本'}`));
        if (actions.repair && tool.health === 'ready') buttons.push(this.actionButton(tool, 'repair', '重新安装'));
        return `
            <div class="system-tool-details" id="system-tool-details-${this.esc(tool.id)}" data-tool-details="${this.esc(tool.id)}" ${this.expandedDetails.has(tool.id) ? '' : 'hidden'}>
                <dl>${rows.map(([label, value]) => `<div><dt>${this.esc(label)}</dt><dd>${this.esc(value)}</dd></div>`).join('')}</dl>
                ${tool.message && tool.health === 'ready' ? `<p class="system-tool-detail-message">${this.esc(tool.message)}</p>` : ''}
                ${buttons.length ? `<div class="system-tool-detail-actions">${buttons.join('')}</div>` : ''}
                ${this.renderOperation(tool, { history: true })}
            </div>`;
    },

    renderTool(tool) {
        const environment = this.isEnvironment(tool);
        const [statusClass, status] = this.versionStatus(tool);
        const checked = this.formatBeijing(tool.checked_at);
        const error = this.checkError(tool);
        return `
            <article class="system-tool-card ${tool.id === 'codex' && window.location.hash === '#codex' ? 'system-tool-target' : ''}" data-tool-id="${this.esc(tool.id)}" ${tool.id === 'codex' ? 'id="codex-tool-row" tabindex="-1" aria-label="Codex 版本与更新"' : ''}>
                <div class="system-tool-card-main">
                    <div class="system-tool-identity"><span class="system-tool-icon"><i class="bi ${this.esc(tool.icon || 'bi-tools')}" aria-hidden="true"></i></span><div class="system-tool-title"><h4>${this.esc(tool.title)}</h4>${tool.id === 'codex' ? '<small>每天 08:00 自动检查</small>' : ''}</div></div>
                    <div class="system-tool-version"><div><span>${this.versionText(tool.installed_version)}</span>${environment ? '' : `<span class="system-tool-version-arrow" aria-label="最新稳定版">→</span><strong class="${tool.update_available ? 'text-primary' : ''}">${tool.available_version ? this.esc(tool.available_version) : '待检查'}</strong>`}</div>${tool.runtime_version && tool.runtime_version !== tool.installed_version ? `<small>运行中 ${this.esc(tool.runtime_version)}</small>` : environment ? '<small>配套运行环境</small>' : ''}</div>
                    <div class="system-tool-status"><span class="system-state-pill ${statusClass}">${this.esc(status)}</span><small>${environment ? '仅检测环境可用性' : checked ? `${this.esc(checked)} · 北京时间` : tool.id === 'codex' ? '北京时间 · 自动检查' : '手动检查版本'}</small></div>
                    <div class="system-tool-actions">${this.renderActions(tool)}</div>
                </div>
                ${error ? `<div class="system-tool-error" role="status"><i class="bi bi-exclamation-triangle" aria-hidden="true"></i><span>检查失败：${this.esc(error)}${tool.update_available ? '；保留上次发现的更新。' : ''}</span></div>` : ''}
                ${tool.health && tool.health !== 'ready' && tool.message ? `<div class="system-tool-error"><i class="bi bi-exclamation-circle" aria-hidden="true"></i><span>${this.esc(tool.message)}</span></div>` : ''}
                ${this.renderOperation(tool)}
                ${this.renderDetails(tool)}
            </article>`;
    },

    render(overview) {
        const container = document.getElementById('systemToolsConsole');
        if (!container) return;
        this.overview = overview;
        const tools = [...(overview.tools || [])].sort((a, b) => Number(b.id === 'codex') - Number(a.id === 'codex'));
        tools.forEach(tool => {
            if (this.checkErrors[tool.id] && tool.checked_at && tool.checked_at !== this.checkErrorVersions[tool.id]
                && tool.check_state !== 'failed') delete this.checkErrors[tool.id];
        });
        const active = tools.some(tool => tool.operation_running || this.operationActive(tool.operation) || tool.check_state === 'checking');
        const signature = JSON.stringify([tools, this.checkErrors, [...this.busyActions], [...this.expandedDetails]]);
        if (container.dataset.ready !== 'true' || this.renderSignature !== signature) {
            const focusKey = container.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
            const rowFocused = document.activeElement?.id === 'codex-tool-row';
            const allBusy = this.busyActions.size > 0 || active;
            container.innerHTML = `
                <div class="system-platform-heading system-tools-heading"><div><h3>工具与更新</h3><p>Codex 每天北京时间 08:00 检查 · 其他组件手动检查</p></div><div class="system-tools-command-actions">
                    <button type="button" class="btn btn-sm btn-light border" data-console-action="refresh" data-focus-key="refresh"><i class="bi bi-arrow-clockwise me-1" aria-hidden="true"></i>刷新状态</button>
                    <button type="button" class="btn btn-sm btn-light border" id="systemToolsCheckAll" data-console-action="check-all" data-focus-key="check-all" ${allBusy ? 'disabled' : ''}>${this.busyActions.has('check-all') ? '正在检查…' : '检查全部版本'}</button>
                </div></div>
                <div class="system-tool-list"><div class="system-tool-list-head" aria-hidden="true"><span>组件</span><span>当前 → 最新稳定版</span><span>状态 / 检查时间</span><span>操作</span></div>${tools.length ? tools.map(tool => this.renderTool(tool)).join('') : '<div class="system-empty-row">尚未读取到组件，请刷新状态。</div>'}</div>`;
            this.renderSignature = signature;
            this.bindActions(container);
            if (focusKey) container.querySelector(`[data-focus-key="${CSS.escape(focusKey)}"]`)?.focus({ preventScroll: true });
            else if (rowFocused) container.querySelector('#codex-tool-row')?.focus({ preventScroll: true });
        }
        container.dataset.ready = 'true';
        container.removeAttribute('aria-busy');
        const codex = tools.find(tool => tool.id === 'codex');
        if (codex) UI.renderCodexUpdateNotice?.(codex);
        this.focusCodex();
        this.schedulePoll(active);
    },

    bindActions(container) {
        container.querySelectorAll('[data-tool-action]').forEach(button => button.addEventListener('click', () => {
            const id = button.closest('[data-tool-id]').dataset.toolId;
            const action = button.dataset.toolAction;
            if (action === 'restart') this.restartAll();
            else if (['check', 'update', 'rollback', 'repair'].includes(action)) this[action](id);
        }));
        container.querySelectorAll('[data-detail-toggle]').forEach(button => button.addEventListener('click', () => {
            const id = button.dataset.detailToggle;
            const open = !this.expandedDetails.has(id);
            if (open) this.expandedDetails.add(id);
            else this.expandedDetails.delete(id);
            button.setAttribute('aria-expanded', String(open));
            container.querySelector(`[data-tool-details="${CSS.escape(id)}"]`).hidden = !open;
        }));
        container.querySelector('[data-console-action="refresh"]')?.addEventListener('click', () => this.load({ force: true }));
        container.querySelector('[data-console-action="check-all"]')?.addEventListener('click', () => this.checkAll());
    },

    focusCodex({ force = false } = {}) {
        const container = document.getElementById('systemToolsConsole');
        const settings = document.getElementById('settings');
        if (!container || settings?.classList.contains('d-none') || settings?.dataset.activeSystemGroup !== 'tools') return;
        if (window.location.hash !== '#codex') {
            delete container.dataset.codexFocused;
            return;
        }
        if (!force && container.dataset.codexFocused === 'true') return;
        const card = container.querySelector('[data-tool-id="codex"]');
        if (!card) return;
        container.dataset.codexFocused = 'true';
        card.classList.add('system-tool-target');
        card.focus({ preventScroll: true });
        card.scrollIntoView({ block: 'nearest', behavior: 'auto' });
    },

    schedulePoll(active = false) {
        clearTimeout(this.pollTimer);
        const settings = document.getElementById('settings');
        if (!settings || settings.classList.contains('d-none') || settings.dataset.activeSystemGroup !== 'tools') return;
        this.pollTimer = setTimeout(() => {
            if (settings.classList.contains('d-none') || settings.dataset.activeSystemGroup !== 'tools') return;
            if (document.hidden) this.schedulePoll(active);
            else this.load({ quiet: true });
        }, active ? 1800 : 30000);
    },

    async load({ quiet = false, force = false } = {}) {
        const container = document.getElementById('systemToolsConsole');
        if (!container) return;
        const requestId = ++this.requestId;
        if (!quiet && container.dataset.ready !== 'true') container.innerHTML = '<div class="loading-wrapper">正在读取工具状态…</div>';
        else if (!quiet) container.setAttribute('aria-busy', 'true');
        try {
            const response = await API.systemTools.getOverview(force);
            if (requestId !== this.requestId || !document.body.contains(container)) return;
            this.render(this.unwrap(response));
        } catch (error) {
            if (requestId !== this.requestId) return;
            container.removeAttribute('aria-busy');
            if (container.dataset.ready === 'true') {
                if (!quiet) UI.showError(`工具状态刷新失败：${error.message}`);
            } else {
                container.innerHTML = `<div class="system-empty-row text-danger" role="alert">工具状态加载失败：${this.esc(error.message)} <button type="button" class="btn btn-sm btn-light border" data-console-action="refresh">重试</button></div>`;
                this.bindActions(container);
            }
            this.schedulePoll();
        }
    },

    async withBusy(key, callback) {
        const tool = this.overview?.tools?.find(item => key.endsWith(`:${item.id}`));
        if (this.busyActions.has(key) || this.busyActions.has('check-all') || (tool && this.toolBusy(tool))) return;
        if (key === 'check-all' && (this.busyActions.size || this.overview?.tools?.some(item => this.toolBusy(item)))) return;
        this.busyActions.add(key);
        if (this.overview) this.render(this.overview);
        try {
            await callback();
        } finally {
            this.busyActions.delete(key);
            if (this.overview) this.render(this.overview);
        }
    },

    async checkAll() {
        await this.withBusy('check-all', async () => {
            try {
                const overview = this.unwrap(await API.systemTools.checkAll());
                this.checkErrors = overview.check_errors || {};
                this.checkErrorVersions = Object.fromEntries((overview.tools || [])
                    .filter(tool => this.checkErrors[tool.id]).map(tool => [tool.id, tool.checked_at]));
                this.render(overview);
                if (Object.keys(this.checkErrors).length) UI.showInfo('部分工具检查失败，已在对应组件中标明');
                else UI.showSuccess('版本检查完成');
            } catch (error) {
                UI.showError(`检查失败：${error.message}`);
                await this.load({ quiet: true });
            }
        });
    },

    async check(toolId) {
        await this.withBusy(`check:${toolId}`, async () => {
            delete this.checkErrors[toolId];
            try {
                await API.systemTools.check(toolId);
                UI.showSuccess('版本检查完成');
            } catch (error) {
                this.checkErrors[toolId] = error.message;
                this.checkErrorVersions[toolId] = this.overview?.tools?.find(tool => tool.id === toolId)?.checked_at;
                UI.showError(`检查失败：${error.message}`);
            }
            await this.load({ quiet: true });
        });
    },

    async update(toolId) {
        await this.withBusy(`update:${toolId}`, async () => {
            const tool = this.overview?.tools?.find(item => item.id === toolId);
            const title = tool?.title || toolId;
            const description = toolId === 'codex'
                ? '将检查最新稳定版本并安装，安装后自动校验并切换运行环境。长时间运行的任务可能受影响，建议任务结束后更新。'
                : '系统会检查稳定版本、安装并执行校验。';
            if (!await UI.confirm(description, { title: `更新 ${title}`, confirmText: '开始更新', variant: 'primary' })) return;
            try {
                await API.systemTools.update(toolId);
                UI.showSuccess(`${title} 更新任务已开始`);
            } catch (error) {
                UI.showError(`无法开始更新：${error.message}`);
            }
            await this.load({ quiet: true });
        });
    },

    async rollback(toolId) {
        await this.withBusy(`rollback:${toolId}`, async () => {
            if (!await UI.confirm('确定回退到上一个已记录版本吗？运行时会重新进行兼容性验证。', {
                title: '回退工具版本', confirmText: '开始回退', variant: 'warning'
            })) return;
            try {
                await API.systemTools.rollback(toolId);
                UI.showSuccess('回退任务已开始');
            } catch (error) {
                UI.showError(`无法开始回退：${error.message}`);
            }
            await this.load({ quiet: true });
        });
    },

    async repair(toolId) {
        await this.withBusy(`repair:${toolId}`, async () => {
            const title = this.overview?.tools?.find(item => item.id === toolId)?.title || toolId;
            const detail = toolId === 'playwright'
                ? '将安装与当前 Playwright Python 包配套的 Chromium，并执行一次无头启动验证。'
                : '将恢复 requirements 中锁定的 static-ffmpeg，并验证 FFmpeg 与 FFprobe。外部路径不会被覆盖。';
            if (!await UI.confirm(detail, { title: `安装 / 修复 ${title}`, confirmText: '开始修复', variant: 'primary' })) return;
            try {
                await API.systemTools.repair(toolId);
                UI.showSuccess(`${title} 修复任务已开始`);
            } catch (error) {
                UI.showError(`无法开始修复：${error.message}`);
            }
            await this.load({ quiet: true });
        });
    },

    restartAll() {
        return App.restartSystem();
    }
};

window.SystemTools = SystemTools;
