/** Unified environment-tool updates and runtime maintenance. */
const SystemTools = {
    requestId: 0,
    pollTimer: null,
    busyActions: new Set(),

    esc(value) {
        return UI.escapeHtml(String(value ?? ''));
    },

    unwrap(response) {
        return response?.data || response || {};
    },

    statusClass(tool) {
        if (tool.operation_running) return 'primary';
        if (tool.health === 'ready') return 'success';
        if (['missing', 'unavailable'].includes(tool.health)) return 'danger';
        return 'warning';
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

    versionText(value) {
        return value ? this.esc(value) : '<span class="text-muted">未检测</span>';
    },

    renderOperation(tool) {
        const operation = tool.operation;
        if (!operation) return '';
        const progress = Math.max(0, Math.min(Number(operation.progress || 0), 100));
        const active = this.operationActive(operation);
        const failed = operation.status === 'failed';
        return `
            <div class="system-tool-operation ${active ? 'active' : ''} ${failed ? 'failed' : ''}" aria-live="polite">
                <div class="system-tool-operation-copy">
                    <strong>${this.esc(this.operationLabel(operation))}</strong>
                    <span>${this.esc(operation.message || operation.error || '')}</span>
                </div>
                <div class="system-tool-operation-progress">
                    <div class="system-progress"><span style="width:${progress}%"></span></div>
                    <small>${progress}%</small>
                </div>
            </div>`;
    },

    renderDetails(tool) {
        const details = tool.details || {};
        const components = Array.isArray(details.components)
            ? details.components
            : Object.values(details.components || {});
        const rows = [];
        if (tool.path) rows.push(['调用路径', tool.path]);
        if (details.realpath && details.realpath !== tool.path) rows.push(['实际路径', details.realpath]);
        if (details.install_method) rows.push(['安装方式', details.install_method]);
        if (details.package_version) rows.push(['管理包', `static-ffmpeg ${details.package_version}`]);
        if (details.browser_version) rows.push(['浏览器版本', details.browser_version]);
        components.forEach(component => {
            if (!component?.name) return;
            rows.push([
                component.name,
                [component.version, component.source_label, component.path].filter(Boolean).join(' · ')
            ]);
        });
        if (!rows.length) return '';
        return `
            <details class="system-tool-details">
                <summary>查看来源与调用路径<i class="bi bi-chevron-down"></i></summary>
                <div>${rows.map(([label, value]) => `
                    <div><span>${this.esc(label)}</span><code title="${this.esc(value)}">${this.esc(value)}</code></div>`).join('')}</div>
            </details>`;
    },

    renderActions(tool) {
        const actions = tool.actions || {};
        const busy = tool.operation_running || this.busyActions.has(tool.id);
        const buttons = [];
        if (actions.check) {
            buttons.push(`<button class="btn btn-sm btn-light border" ${busy ? 'disabled' : ''} onclick="SystemTools.check('${this.esc(tool.id)}')"><i class="bi bi-search me-1"></i>检查版本</button>`);
        }
        if (actions.update) {
            const label = tool.update_available && tool.available_version
                ? `升级到 ${this.esc(tool.available_version)}`
                : '升级到最新版';
            buttons.push(`<button class="btn btn-sm btn-primary" ${busy ? 'disabled' : ''} onclick="SystemTools.update('${this.esc(tool.id)}')"><i class="bi bi-arrow-up-circle me-1"></i>${label}</button>`);
        }
        if (actions.rollback) {
            const version = tool.details?.rollback_version;
            buttons.push(`<button class="btn btn-sm btn-light border" ${busy ? 'disabled' : ''} onclick="SystemTools.rollback('${this.esc(tool.id)}')"><i class="bi bi-arrow-counterclockwise me-1"></i>回退${version ? `到 ${this.esc(version)}` : ''}</button>`);
        }
        if (actions.repair) {
            buttons.push(`<button class="btn btn-sm ${tool.health === 'ready' ? 'btn-light border' : 'btn-primary'}" ${busy ? 'disabled' : ''} onclick="SystemTools.repair('${this.esc(tool.id)}')"><i class="bi bi-wrench-adjustable me-1"></i>${tool.health === 'ready' ? '重新安装' : '安装 / 修复'}</button>`);
        }
        if (actions.restart) {
            buttons.push(`<button class="btn btn-sm btn-warning" ${busy ? 'disabled' : ''} onclick="SystemTools.restartAll()"><i class="bi bi-arrow-repeat me-1"></i>重启后生效</button>`);
        }
        return buttons.join('');
    },

    renderTool(tool) {
        const available = tool.available_version;
        const current = tool.installed_version;
        return `
            <article class="system-tool-card" data-tool-id="${this.esc(tool.id)}">
                <div class="system-tool-card-main">
                    <div class="system-tool-identity">
                        <span class="system-tool-icon"><i class="bi ${this.esc(tool.icon || 'bi-tools')}"></i></span>
                        <div>
                            <div class="system-tool-title"><h4>${this.esc(tool.title)}</h4><span class="system-state-pill ${this.statusClass(tool)}">${this.esc(tool.operation_running ? '处理中' : tool.health_label)}</span>${tool.restart_required ? '<span class="system-state-pill warning">需重启</span>' : ''}</div>
                            <p>${this.esc(tool.description)}</p>
                        </div>
                    </div>
                    <div class="system-tool-facts">
                        <div><span>当前版本</span><strong>${this.versionText(current)}</strong>${tool.runtime_version && tool.runtime_version !== current ? `<small>运行中 ${this.esc(tool.runtime_version)}</small>` : ''}</div>
                        <div><span>最新稳定版</span><strong class="${tool.update_available ? 'text-primary' : ''}">${this.versionText(available)}</strong><small>${tool.checked_at ? `检查于 ${this.esc(UI.formatDateTime(tool.checked_at))}` : '尚未检查'}</small></div>
                        <div><span>来源</span><strong>${this.esc(tool.source_label || '未识别')}</strong><small>${tool.managed ? '可由 Mabobot 管理' : '外部安装仅检测'}</small></div>
                    </div>
                </div>
                <div class="system-tool-card-footer">
                    <div class="system-tool-message"><i class="bi bi-info-circle"></i><span>${this.esc(tool.message || '')}</span></div>
                    <div class="system-tool-actions">${this.renderActions(tool)}</div>
                </div>
                ${this.renderOperation(tool)}
                ${this.renderDetails(tool)}
            </article>`;
    },

    render(overview) {
        const container = document.getElementById('systemToolsConsole');
        if (!container) return;
        const tools = overview.tools || [];
        const summary = overview.summary || {};
        const upgradable = tools.filter(tool => tool.category === 'upgradable');
        const maintenance = tools.filter(tool => tool.category === 'maintenance');
        const errors = overview.check_errors || {};
        container.innerHTML = `
            <div class="system-platform-heading system-tools-heading">
                <div><h3>工具与更新</h3><p>统一检查必要组件；更新仍按工具逐项确认</p></div>
                <div class="system-tools-command-actions">
                    <button class="btn btn-sm btn-light border" onclick="SystemTools.load({ force: true })"><i class="bi bi-arrow-clockwise me-1"></i>刷新状态</button>
                    <button class="btn btn-sm btn-primary" id="systemToolsCheckAll" onclick="SystemTools.checkAll()"><i class="bi bi-search me-1"></i>检查全部版本</button>
                </div>
            </div>
            <div class="system-runtime-strip system-tools-summary">
                <div><span>纳入管理</span><strong>${Number(summary.total || tools.length)}</strong></div>
                <div><span>状态正常</span><strong class="text-success">${Number(summary.ready || 0)}</strong></div>
                <div><span>需要关注</span><strong class="${Number(summary.attention || 0) ? 'text-warning' : ''}">${Number(summary.attention || 0)}</strong></div>
                <div><span>可用更新</span><strong class="${Number(summary.updates || 0) ? 'text-primary' : ''}">${Number(summary.updates || 0)}</strong></div>
                <div><span>正在处理</span><strong>${Number(summary.active || 0)}</strong></div>
            </div>
            ${Object.keys(errors).length ? `<div class="system-tools-check-errors"><i class="bi bi-exclamation-triangle"></i><span>${Object.entries(errors).map(([id, error]) => `${this.esc(id)}：${this.esc(error)}`).join('；')}</span></div>` : ''}
            <section class="system-tool-group">
                <div class="system-tool-group-head"><div><h4>可升级组件</h4><p>检查稳定版，逐项安装并验证；不会执行“全部升级”</p></div><span>${upgradable.length} 项</span></div>
                <div class="system-tool-list">${upgradable.map(tool => this.renderTool(tool)).join('')}</div>
            </section>
            <section class="system-tool-group">
                <div class="system-tool-group-head"><div><h4>环境维护</h4><p>识别实际来源，只修复 Mabobot 托管的运行时</p></div><span>${maintenance.length} 项</span></div>
                <div class="system-tool-list">${maintenance.map(tool => this.renderTool(tool)).join('')}</div>
            </section>`;
        container.dataset.ready = 'true';
        container.removeAttribute('aria-busy');
        this.schedulePoll(tools.some(tool => tool.operation_running || this.operationActive(tool.operation)));
    },

    schedulePoll(active) {
        if (this.pollTimer) {
            clearTimeout(this.pollTimer);
            this.pollTimer = null;
        }
        if (!active || document.getElementById('settings')?.dataset.activeSystemGroup !== 'tools') return;
        this.pollTimer = setTimeout(() => this.load({ quiet: true }), 1800);
    },

    async load({ quiet = false, force = false } = {}) {
        const container = document.getElementById('systemToolsConsole');
        if (!container) return;
        const requestId = ++this.requestId;
        if (!quiet && container.dataset.ready !== 'true') {
            container.innerHTML = '<div class="loading-wrapper">正在读取工具状态…</div>';
        } else {
            container.setAttribute('aria-busy', 'true');
        }
        try {
            const response = await API.systemTools.getOverview(force);
            if (requestId !== this.requestId || !document.body.contains(container)) return;
            this.render(this.unwrap(response));
        } catch (error) {
            if (requestId !== this.requestId) return;
            container.removeAttribute('aria-busy');
            if (container.dataset.ready === 'true') {
                UI.showError(`工具状态刷新失败：${error.message}`);
            } else {
                container.innerHTML = `<div class="system-empty-row text-danger">工具状态加载失败：${this.esc(error.message)}</div>`;
            }
        }
    },

    async withBusy(key, callback) {
        if (this.busyActions.has(key)) return;
        this.busyActions.add(key);
        try {
            await callback();
        } finally {
            this.busyActions.delete(key);
        }
    },

    async checkAll() {
        await this.withBusy('check-all', async () => {
            const button = document.getElementById('systemToolsCheckAll');
            if (button) {
                button.disabled = true;
                button.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>正在检查';
            }
            try {
                const response = await API.systemTools.checkAll();
                this.render(this.unwrap(response));
                const errors = this.unwrap(response).check_errors || {};
                if (Object.keys(errors).length) UI.showInfo('部分工具检查失败，已在页面中标明');
                else UI.showSuccess('版本检查完成');
            } catch (error) {
                UI.showError(`检查失败：${error.message}`);
                await this.load({ quiet: true });
            }
        });
    },

    async check(toolId) {
        await this.withBusy(`check:${toolId}`, async () => {
            try {
                await API.systemTools.check(toolId);
                UI.showSuccess('版本检查完成');
            } catch (error) {
                UI.showError(`检查失败：${error.message}`);
            }
            await this.load({ quiet: true });
        });
    },

    async update(toolId) {
        const card = document.querySelector(`[data-tool-id="${CSS.escape(toolId)}"]`);
        const title = card?.querySelector('.system-tool-title h4')?.textContent || toolId;
        if (!await UI.confirm(`确定升级 ${title} 吗？\n系统会安装检查到的稳定版本，并在安装后执行校验。`, {
            title: `升级 ${title}`,
            confirmText: '开始升级',
            variant: 'primary'
        })) return;
        await this.withBusy(`update:${toolId}`, async () => {
            try {
                await API.systemTools.update(toolId);
                UI.showSuccess(`${title} 升级任务已开始`);
                await this.load({ quiet: true });
            } catch (error) {
                UI.showError(`无法开始升级：${error.message}`);
            }
        });
    },

    async rollback(toolId) {
        if (!await UI.confirm('确定回退到上一个已记录版本吗？运行时会重新进行兼容性验证。', {
            title: '回退工具版本', confirmText: '开始回退', variant: 'warning'
        })) return;
        try {
            await API.systemTools.rollback(toolId);
            UI.showSuccess('回退任务已开始');
            await this.load({ quiet: true });
        } catch (error) {
            UI.showError(`无法开始回退：${error.message}`);
        }
    },

    async repair(toolId) {
        const card = document.querySelector(`[data-tool-id="${CSS.escape(toolId)}"]`);
        const title = card?.querySelector('.system-tool-title h4')?.textContent || toolId;
        const detail = toolId === 'playwright'
            ? '将安装与当前 Playwright Python 包配套的 Chromium，并执行一次无头启动验证。'
            : '将恢复 requirements 中锁定的 static-ffmpeg，并验证 FFmpeg 与 FFprobe。外部路径不会被覆盖。';
        if (!await UI.confirm(detail, {
            title: `安装 / 修复 ${title}`, confirmText: '开始修复', variant: 'primary'
        })) return;
        try {
            await API.systemTools.repair(toolId);
            UI.showSuccess(`${title} 修复任务已开始`);
            await this.load({ quiet: true });
        } catch (error) {
            UI.showError(`无法开始修复：${error.message}`);
        }
    },

    restartAll() {
        return App.restartSystem();
    }
};

window.SystemTools = SystemTools;
