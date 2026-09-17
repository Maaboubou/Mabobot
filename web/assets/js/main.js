/**
 * Main Application Module
 */

const App = {
    // State
    currentTab: UI.getInitialTab(),
    refreshInterval: null,
    logRefreshInterval: null,
    logAbortController: null,
    logControlsReady: false,
    isLoading: false,
    logFollowEnabled: false,
    logProgrammaticScroll: false,
    currentLogContent: '',
    currentLogSearchQuery: '',
    currentLogSearchMatches: [],
    currentLogSearchIndex: -1,
    currentLogStatusBase: '就绪',
    webRestartSupported: false,
    webRestartUnavailableReason: '正在检查管理控制台状态…',
    restartCapabilities: null,
    botControlSupported: false,
    botControlUnavailableReason: '正在检查管理控制台状态…',
    botServiceRunning: null,
    automationView: 'library',
    automationCapabilities: [],
    automationRouting: null,
    automationSelectedEvent: null,
    automationSelectedChatId: null,
    automationRouteMode: 'sort',
    automationDraftEvent: null,
    automationDraftKeys: null,
    automationOrderDirty: false,
    automationOrderSaving: false,
    managedChatSelectionRequest: 0,
    managedChatFilter: 'all',
    _managedChatReferenceData: null,
    _managedChatReferencePromise: null,
    _managedChatProfilesData: null,

    openSelectedChatArchive() {
        const userId = Number(this._selectedChatPolicy?.user_id || 0);
        if (userId) this.openChatArchive(userId);
    },

    async init() {
        console.log('App Initializing...');
        UI.init(); // Setup UI listeners

        // Global Error Handler
        window.addEventListener('unhandledrejection', event => {
            console.error('Unhandled promise rejection:', event.reason);
        });

        // Health Check
        try {
            await API.system.checkHealth();
            await this.configureRestartControls();
            await this.configureBotServiceControl();

            // Set up Polling
            this.startAutoRefresh();
            document.addEventListener('visibilitychange', () => {
                if (document.hidden) {
                    this.stopAutoRefresh();
                    Dashboard.stopPulse();
                    if (this.logAbortController) this.logAbortController.abort();
                } else {
                    this.startAutoRefresh();
                    this.refreshCurrentTab();
                    if (this.currentTab === 'dashboard') Dashboard.startPulse();
                }
            });

            // Load only the requested route. The previous startup path loaded
            // Dashboard and every LLM sub-view even when neither was visible.
            UI.switchTab(this.currentTab, { history: false });
        } catch (e) {
            UI.showError('系统初始化失败：' + e.message, 'alert');
        }
    },

    async refreshCurrentTab() {
        if (document.hidden) return;
        if (this.isLoading) return;
        await this.refreshBotServiceState({ quiet: true });
        // Don't auto-refresh settings or forms to avoid overwriting user input
        if (['settings', 'users', 'roles', 'llm', 'usage'].includes(this.currentTab)) return;

        await this.loadTab(this.currentTab, true);
    },

    startAutoRefresh() {
        if (this.refreshInterval || document.hidden) return;
        this.refreshInterval = setInterval(() => this.refreshCurrentTab(), 30000);
    },

    stopAutoRefresh() {
        if (!this.refreshInterval) return;
        clearInterval(this.refreshInterval);
        this.refreshInterval = null;
    },

    async loadTab(tabName, isBackground = false) {
        this.currentTab = tabName;
        this.isLoading = true;
        // 概览页的脉搏只在概览页跑：切到别的标签立刻停，回到概览时由 load 重新拉起。
        if (tabName !== 'dashboard') Dashboard.stopPulse();

        try {
            switch (tabName) {
                case 'dashboard':
                    await Dashboard.load({ quiet: isBackground });
                    break;
                case 'plugins':
                    await this.loadPlugins(!isBackground);
                    break;
                case 'codex':
                    await CodexCenter.load({ quiet: isBackground });
                    break;
                case 'wechat':
                    await this.loadWeChat();
                    break;
                case 'users':
                    await this.loadUsers();
                    break;
                case 'roles':
                    await this.loadRoles();
                    break;
                case 'usage':
                    await LLMManager.initUsage();
                    break;
                case 'llm':
                    await LLMManager.init();
                    break;
                case 'logs':
                    if (!isBackground) this.setupLogControls();
                    await this.loadLogs(null, !isBackground);
                    break;
                case 'settings':
                    this.loadSettings();
                    break;
            }
        } catch (e) {
            console.error(`Failed to load tab ${tabName}:`, e);
            if (!isBackground) {
                UI.showError(`加载页面 ${tabName} 失败：${e.message}`);
            }
        } finally {
            this.isLoading = false;
        }
    },

    async restartSystem() {
        return this.restartManagedService('all', {
            confirmMessage: '确定要重启全部服务吗？Web 和微信 Bot 都会短暂中断。',
            confirmTitle: '重启全部服务',
            overlayTitle: '系统重启中',
            overlayMessage: '正在重启全部服务…'
        });
    },

    async configureRestartControls() {
        const webButton = document.getElementById('restartWebButton');
        if (!webButton) return;

        try {
            const capabilities = await API.system.getRestartCapabilities();
            this.restartCapabilities = capabilities;
            const webRestartSupported = capabilities?.signal_protocol >= 2
                && capabilities?.services?.includes('web');
            this.webRestartSupported = webRestartSupported;
            this.webRestartUnavailableReason = capabilities?.reason
                || '当前管理面板进程不支持单独重启 Web 服务。请完整关闭并重新打开 Mabobot 管理面板。';
            webButton.disabled = false;
            webButton.classList.toggle('restart-supported', webRestartSupported);
            webButton.classList.toggle('text-secondary', !webRestartSupported);
            webButton.setAttribute('aria-disabled', webRestartSupported ? 'false' : 'true');
            webButton.title = webRestartSupported
                ? '只重启 Web，微信 Bot 保持运行'
                : this.webRestartUnavailableReason;
        } catch (e) {
            this.webRestartSupported = false;
            this.webRestartUnavailableReason = '无法确认管理面板能力。请完整关闭并重新打开 Mabobot 管理面板。';
            webButton.disabled = false;
            webButton.classList.remove('restart-supported');
            webButton.classList.add('text-secondary');
            webButton.setAttribute('aria-disabled', 'true');
            webButton.title = this.webRestartUnavailableReason;
        }
    },

    async configureBotServiceControl() {
        const toggle = document.getElementById('botServiceToggle');
        if (!toggle) return;

        try {
            const capabilities = this.restartCapabilities || await API.system.getRestartCapabilities();
            this.restartCapabilities = capabilities;
            this.botControlSupported = capabilities?.signal_protocol >= 3
                && capabilities?.controls?.includes('start-bot')
                && capabilities?.controls?.includes('stop-bot');
            this.botControlUnavailableReason = this.botControlSupported
                ? ''
                : '当前管理面板需要完整重启一次，才能使用 Bot 服务启停开关。';
            await this.refreshBotServiceState();
        } catch (error) {
            this.botControlSupported = false;
            this.botControlUnavailableReason = '无法确认 Bot 服务控制能力。请完整重启管理面板后重试。';
            this.renderBotServiceControl({ running: false, supported: false });
        }
    },

    renderBotServiceControl({ running, supported = this.botControlSupported, busy = false }) {
        const control = document.getElementById('botServiceControl');
        const toggle = document.getElementById('botServiceToggle');
        const state = document.getElementById('botServiceState');
        if (!control || !toggle || !state) return;

        this.botServiceRunning = Boolean(running);
        toggle.setAttribute('aria-checked', String(this.botServiceRunning));
        toggle.disabled = !supported || busy;
        control.classList.toggle('is-running', this.botServiceRunning && !busy);
        control.classList.toggle('is-stopped', !this.botServiceRunning && !busy);
        control.classList.toggle('is-unavailable', !supported);
        control.classList.toggle('is-busy', busy);
        state.textContent = busy ? (this.botServiceRunning ? '启动中' : '停止中') : (this.botServiceRunning ? '运行中' : '已停止');
        control.title = supported
            ? `Bot 服务${this.botServiceRunning ? '正在运行，关闭开关可停止服务' : '已停止，打开开关可启动服务'}`
            : this.botControlUnavailableReason;
    },

    async refreshBotServiceState(options = {}) {
        const toggle = document.getElementById('botServiceToggle');
        if (!toggle) return false;
        try {
            const result = await API.system.getProcesses();
            const running = Boolean(result?.processes?.wx_bot?.running);
            this.renderBotServiceControl({ running });
            return running;
        } catch (error) {
            if (!options.quiet) UI.showError('读取 Bot 服务状态失败：' + error.message);
            return this.botServiceRunning;
        }
    },

    async waitForBotServiceState(expectedRunning, timeoutMs = 12000) {
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            const result = await API.system.getProcesses();
            if (Boolean(result?.processes?.wx_bot?.running) === expectedRunning) return true;
            await new Promise(resolve => setTimeout(resolve, 500));
        }
        return false;
    },

    async toggleBotService(enabled) {
        const previousState = this.botServiceRunning;
        if (!this.botControlSupported) {
            this.renderBotServiceControl({ running: previousState, supported: false });
            UI.showError(this.botControlUnavailableReason, 'alert');
            return;
        }

        this.renderBotServiceControl({ running: enabled, busy: true });
        try {
            await API.system.controlBot(enabled ? 'start' : 'stop');
            const reachedExpectedState = await this.waitForBotServiceState(enabled);
            if (!reachedExpectedState) throw new Error(`等待 Bot 服务${enabled ? '启动' : '停止'}超时`);
            this.renderBotServiceControl({ running: enabled });
            UI.showSuccess(`Bot 服务已${enabled ? '启动' : '停止'}`);
        } catch (error) {
            const actualState = await this.refreshBotServiceState({ quiet: true });
            this.renderBotServiceControl({ running: actualState });
            UI.showError(`Bot 服务${enabled ? '启动' : '停止'}失败：${error.message}`);
        }
    },

    async restartWeb() {
        if (!this.webRestartSupported) {
            UI.showError(this.webRestartUnavailableReason, 'alert');
            return;
        }
        return this.restartManagedService('web', {
            confirmMessage: '确定只重启 Web 服务吗？微信 Bot 将保持运行。',
            confirmTitle: '只重启 Web',
            overlayTitle: 'Web 服务重启中',
            overlayMessage: '正在重启 Web 服务…'
        });
    },

    async restartManagedService(service, options) {
        if (!await UI.confirm(options.confirmMessage, {
            title: options.confirmTitle,
            confirmText: '重启',
            variant: 'warning'
        })) return;

        // 立即显示重连 Overlay，不等待 API 响应
        // 原因：重启请求会让服务器立刻下线，fetch 必然以 NetworkError 结束，
        // 这是正常现象而非错误。
        UI.showRestartOverlay(options.overlayTitle, options.overlayMessage);

        // 发出重启请求（fire-and-forget），NetworkError 视为成功
        try {
            await API.system.restart(service);
        } catch (e) {
            // NetworkError / TypeError 表示服务器已经开始重启，属于预期行为
            if (!(e instanceof TypeError) && !e.message.includes('NetworkError') && !e.message.includes('Failed to fetch')) {
                // 真正的意外错误才取消 overlay 并提示
                UI.hideRestartOverlay();
                UI.showError('无法触发重启：' + e.message);
                return;
            }
        }

        // 延迟 3 秒后开始轮询（给服务器时间真正重启）
        setTimeout(() => {
            this.pollForRecovery();
        }, 3000);
    },

    async pollForRecovery() {
        const check = async () => {
            try {
                await API.system.checkHealth();
                // 恢复成功，刷新页面
                window.location.reload();
            } catch (e) {
                // 还未恢复，继续轮询
                setTimeout(check, 2000);
            }
        };
        check();
    },

    // --- Tab Actions ---

    renderJudgeOutput(data) {
        const container = document.getElementById('judgeOutput');

        if (!data || !data.judge_output) {
            container.innerHTML = `
                <div class="text-center text-muted small">
                    <i class="bi bi-info-circle me-1"></i>
                    ${this.escapeHtml(data?.reason || '暂无数据')}
                </div>
            `;
            return;
        }

        const history = Array.isArray(data.history) && data.history.length > 0
            ? data.history.slice(0, 10)
            : [data];
        const latest = history[0] || data;
        const shouldReply = latest.should_reply;
        const reason = latest.reason || '无原因';
        const timestamp = latest.timestamp || '';
        const judgeName = latest.judge_name || latest.judge_output?.judge_name || '';

        // 格式化时间（只显示时分秒）
        const timeDisplay = this.formatDashboardTime(timestamp);

        const html = `
            <div class="mb-2">
                <div class="d-flex justify-content-between align-items-center mb-2">
                    <span class="badge ${shouldReply ? 'bg-success' : 'bg-secondary'}">
                        ${latest.state === 'failed' ? '判断失败' : shouldReply ? '✅ 需要回复' : '⏸️ 无需回复'}
                    </span>
                    <div class="d-flex align-items-center gap-2">
                        ${timeDisplay ? `<small class="text-muted">${this.escapeHtml(timeDisplay)}</small>` : ''}
                        ${history.length > 1 ? `
                            <button class="btn btn-sm btn-outline-secondary py-0 px-2" onclick="App.showJudgeHistoryModal()">
                                <i class="bi bi-clock-history me-1"></i>历史
                            </button>
                        ` : ''}
                    </div>
                </div>
                ${judgeName ? `<div class="small text-muted mb-2"><strong>接话判断：</strong>${this.escapeHtml(judgeName)}</div>` : ''}
                <div class="small text-muted">
                    <strong>原因：</strong><br>
                    ${this.escapeHtml(reason)}
                </div>
            </div>
        `;

        container.innerHTML = html;
        container.dataset.history = JSON.stringify(history);
    },

    showJudgeHistoryModal() {
        const container = document.getElementById('judgeOutput');
        const historyData = container?.dataset.history;
        const history = historyData ? JSON.parse(historyData) : [];
        const body = document.getElementById('modalJudgeHistoryContent');

        if (!body) return;

        if (!history.length) {
            body.innerHTML = `
                <div class="text-center text-muted py-3">
                    <i class="bi bi-info-circle me-1"></i>
                    暂无 接话判断 历史记录
                </div>
            `;
        } else {
            body.innerHTML = history.map((item, idx) => {
                const itemShouldReply = item.should_reply;
                const itemReason = item.reason || '无原因';
                const itemTime = this.formatDashboardTime(item.timestamp || '');
                const itemJudgeName = item.judge_name || item.judge_output?.judge_name || '';
                const itemRoleName = item.role_name || '';
                const itemAtmosphere = item.atmosphere || '';
                return `
                    <div class="py-3 ${idx > 0 ? 'border-top' : ''}">
                        <div class="d-flex justify-content-between align-items-center gap-2 mb-2">
                            <span class="badge ${itemShouldReply ? 'bg-success' : 'bg-secondary'}">
                                ${item.state === 'failed' ? '判断失败' : itemShouldReply ? '需要回复' : '无需回复'}
                            </span>
                            ${itemTime ? `<small class="text-muted">${this.escapeHtml(itemTime)}</small>` : ''}
                        </div>
                        <div class="small text-muted mb-2">
                            ${itemJudgeName ? `<strong>接话判断：</strong>${this.escapeHtml(itemJudgeName)}` : ''}
                            ${itemRoleName ? `${itemJudgeName ? ' &nbsp;|&nbsp; ' : ''}<strong>角色：</strong>${this.escapeHtml(itemRoleName)}` : ''}
                        </div>
                        ${itemAtmosphere ? `<div class="small text-muted mb-2"><strong>氛围：</strong>${this.escapeHtml(itemAtmosphere)}</div>` : ''}
                        <div class="small text-break" style="white-space: pre-wrap;">${this.escapeHtml(itemReason)}</div>
                    </div>
                `;
            }).join('');
        }

        const modal = new bootstrap.Modal(document.getElementById('judgeHistoryModal'));
        modal.show();
    },


    escapeHtml(text) {
        return UI.escapeHtml(text);
    },

    formatDashboardTime(timestamp) {
        if (!timestamp) return '';
        if (typeof timestamp === 'string' && timestamp.includes(' ')) {
            return timestamp.split(' ')[1] || '';
        }
        const date = new Date(timestamp);
        return Number.isNaN(date.getTime()) ? String(timestamp) : UI.formatTimeOfDay(date);
    },

    async loadPlugins(showSpinner = true) {
        if (!showSpinner && this.automationOrderDirty) return;
        if (showSpinner) {
            UI.showLoading('pluginsList');
        }
        const [capabilitiesData, routingData] = await Promise.all([
            API.capabilities.getAll(),
            API.automation.getOverview({ chatId: this.automationSelectedChatId })
        ]);
        this.automationCapabilities = capabilitiesData.capabilities || [];
        this.automationRouting = routingData;
        if (!(routingData.event_types || []).some(item => item.id === this.automationSelectedEvent)) {
            this.automationSelectedEvent = routingData.event_types?.[0]?.id || null;
        }
        this.renderAutomationWorkbench();
    },

    renderAutomationWorkbench() {
        UI.renderAutomationWorkbench(
            this.automationCapabilities,
            this.automationRouting,
            {
                view: this.automationView,
                selectedEvent: this.automationSelectedEvent,
                selectedChatId: this.automationSelectedChatId,
                routeMode: this.automationRouteMode,
                draftEvent: this.automationDraftEvent,
                draftKeys: this.automationDraftKeys,
                dirty: this.automationOrderDirty,
                saving: this.automationOrderSaving
            }
        );
    },

    async confirmDiscardAutomationDraft() {
        if (!this.automationOrderDirty) return true;
        return UI.confirm('当前执行顺序尚未应用。放弃这次调整吗？', {
            title: '放弃顺序调整',
            confirmText: '放弃调整',
            variant: 'warning'
        });
    },

    clearAutomationDraft() {
        this.automationDraftEvent = null;
        this.automationDraftKeys = null;
        this.automationOrderDirty = false;
        this.automationOrderSaving = false;
    },

    async setAutomationView(view) {
        if (!['routes', 'library'].includes(view) || view === this.automationView) return;
        if (!await this.confirmDiscardAutomationDraft()) {
            this.renderAutomationWorkbench();
            return;
        }
        this.clearAutomationDraft();
        this.automationView = view;
        this.renderAutomationWorkbench();
    },

    async selectAutomationEvent(eventType) {
        if (!eventType || eventType === this.automationSelectedEvent) return;
        if (!await this.confirmDiscardAutomationDraft()) {
            this.renderAutomationWorkbench();
            return;
        }
        this.clearAutomationDraft();
        this.automationSelectedEvent = eventType;
        this.renderAutomationWorkbench();
    },

    setAutomationRouteMode(mode) {
        if (!['sort', 'detail'].includes(mode) || mode === this.automationRouteMode) return;
        this.automationRouteMode = mode;
        this.renderAutomationWorkbench();
    },

    async selectAutomationChat(chatId) {
        const normalized = chatId === '' || chatId === null || chatId === undefined
            ? null
            : Number(chatId);
        if (normalized === this.automationSelectedChatId) return;
        if (!await this.confirmDiscardAutomationDraft()) {
            this.renderAutomationWorkbench();
            return;
        }
        this.clearAutomationDraft();
        this.automationSelectedChatId = Number.isFinite(normalized) ? normalized : null;
        await this.loadPlugins(true);
    },

    currentAutomationKeys() {
        const liveItems = this.automationRouting?.routes?.[this.automationSelectedEvent] || [];
        const liveKeys = liveItems.map(item => item.listener_key);
        if (this.automationDraftEvent === this.automationSelectedEvent && Array.isArray(this.automationDraftKeys)) {
            return [
                ...this.automationDraftKeys.filter(key => liveKeys.includes(key)),
                ...liveKeys.filter(key => !this.automationDraftKeys.includes(key))
            ];
        }
        return liveKeys;
    },

    setAutomationDraft(keys) {
        const liveKeys = (this.automationRouting?.routes?.[this.automationSelectedEvent] || [])
            .map(item => item.listener_key);
        const normalized = keys.filter((key, index) => liveKeys.includes(key) && keys.indexOf(key) === index);
        liveKeys.forEach(key => {
            if (!normalized.includes(key)) normalized.push(key);
        });
        this.automationDraftEvent = this.automationSelectedEvent;
        this.automationDraftKeys = normalized;
        this.automationOrderDirty = normalized.some((key, index) => key !== liveKeys[index]);
        if (!this.automationOrderDirty) this.clearAutomationDraft();
        this.renderAutomationWorkbench();
    },

    captureAutomationOrder() {
        const routeList = document.getElementById('automationRouteList');
        if (!routeList) return;
        const keys = [...routeList.querySelectorAll('.automation-route-step')]
            .map(step => step.dataset.listenerKey)
            .filter(Boolean);
        this.setAutomationDraft(keys);
    },

    undoAutomationOrder() {
        this.clearAutomationDraft();
        this.renderAutomationWorkbench();
    },

    async saveAutomationOrder() {
        if (!this.automationOrderDirty || !this.automationSelectedEvent || this.automationOrderSaving) return;
        this.automationOrderSaving = true;
        this.renderAutomationWorkbench();
        try {
            await API.automation.updateOrder(
                this.automationSelectedEvent,
                this.currentAutomationKeys(),
                this.automationRouting?.signature || null
            );
            this.clearAutomationDraft();
            UI.showSuccess('执行顺序已保存并立即生效');
            await this.loadPlugins(false);
        } catch (error) {
            this.automationOrderSaving = false;
            this.renderAutomationWorkbench();
            UI.showError('执行顺序未应用：' + error.message);
        }
    },

    async togglePlugin(name, checked, options = {}) {
        const displayName = options.displayName || name;
        try {
            await API.plugins.toggle(name, checked);
            UI.showSuccess(`${displayName} ${checked ? '已启用' : '已禁用'}`);
            if (this._managedChatReferenceData) {
                const capability = (this._managedChatReferenceData.capabilities || [])
                    .find(item => item.id === name);
                if (capability) {
                    capability.enabled = checked;
                    capability.loaded = checked;
                    capability.status = checked ? 'running' : 'disabled';
                }
            }
        } catch (e) {
            UI.showError(e.message);
            return false;
        }
        if (options.refreshWorkbench !== false) {
            try {
                await this.loadPlugins(false);
            } catch (refreshError) {
                console.warn('Plugin workbench refresh failed:', refreshError);
                UI.showInfo('插件状态已更新，列表将在下次打开时刷新');
            }
        }
        return true;
    },

    async reloadPlugin(name) {
        if (!await UI.confirm(`确定要重新加载插件 ${name} 吗？`, {
            title: '重新加载插件',
            confirmText: '重新加载'
        })) return;
        try {
            await API.plugins.reload(name);
            UI.showSuccess('插件已重新加载');
            await this.loadPlugins();
        } catch (e) {
            UI.showError(e.message);
        }
    },

    async showPluginDetails(name) {
        try {
            const data = await API.capabilities.getDetail(name);
            document.getElementById('configModalTitle').textContent = `能力详情：${data.capability?.display_name || name}`;
            document.getElementById('configModalBody').innerHTML = `
                <pre id="pluginDetailsJson" class="m-0 p-3 bg-light rounded small" style="max-height:60vh;overflow:auto;"></pre>
            `;
            document.getElementById('pluginDetailsJson').textContent = JSON.stringify(data, null, 2);
            const saveBtn = document.getElementById('configModalSaveBtn');
            if (saveBtn) {
                saveBtn.onclick = null;
                saveBtn.classList.add('d-none');
            }
            new bootstrap.Modal(document.getElementById('configModal')).show();
        } catch (e) {
            UI.showError('加载能力详情失败：' + e.message);
        }
    },

    async showPluginSettings(name, options = {}) {
        return this.showCapabilitySettings(name, options);
    },

    collectConfigValues(form) {
        const values = {};
        form.querySelectorAll('[data-config-key]').forEach(input => {
            if (input.matches(':disabled')) return;
            const {configKey: key, configType: type} = input.dataset;
            if (input.dataset.sensitive === 'true' && input.value === '') return;
            if (input.dataset.configControl === 'json') {
                try { values[key] = JSON.parse(input.value); }
                catch (_) { throw Error(`${input.closest('.cap-settings-field')?.querySelector('label')?.textContent || key}：请填写有效的 JSON`); }
            } else if (input.dataset.configControl === 'languages') {
                values[key] = JSON.parse(input.value).map(value => UI.normalizeTranslationLanguage(value));
                if (![2, 3].includes(values[key].length) || values[key].some(value => !value.trim())) throw Error('请选择 2 或 3 种互译语言');
                if (new Set(values[key].map(value => value.trim().toLowerCase())).size !== values[key].length) throw Error('互译语言不能重复');
            } else if (type === 'boolean') values[key] = input.checked;
            else if (type === 'integer') values[key] = Number.parseInt(input.value, 10);
            else if (type === 'number') values[key] = Number.parseFloat(input.value);
            else if (type === 'array') values[key] = input.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
            else values[key] = input.value;
        });
        return values;
    },

    async showChatPluginSettings(name, parentForm) {
        const requestId = this._capabilitySettingsRequest = (this._capabilitySettingsRequest || 0) + 1;
        try {
            const userId = Number(parentForm.dataset.userId);
            const settings = await API.capabilities.getSettings(name, userId);
            if (!parentForm.isConnected || requestId !== this._capabilitySettingsRequest) return;
            const modal = document.getElementById('configModal');
            const chatName = this._selectedChatPolicy?.chat?.chat_name || `聊天 ${userId}`;
            const displayName = (this._managedChatReferenceData?.capabilities || []).find(item=>item.id===name)?.display_name || settings.display_name || name;
            document.getElementById('configModalTitle').textContent = `${displayName} · ${chatName}`;
            document.getElementById('configModalBody').innerHTML = UI.renderChatPluginSettingsForm(settings, {}, {chatName});
            const form = modal.querySelector('#chatPluginSettingsForm');
            const translation = name === 'builtin_translation';
            const config = settings.chat_config;
            const fields = settings.groups.flatMap(group=>group.fields);
            const saveBtn = document.getElementById('configModalSaveBtn');
            const resetBtn = form.querySelector('[data-chat-config-reset]');
            const getValues = () => translation ? UI.readTranslationValues(form) : this.collectConfigValues(form);
            const getPatch = () => translation ? UI.collectTranslationChatPatch(form, config)
                : UI.pluginChatPatch(getValues(), config, Boolean(form._resetDefaults));
            const refresh = () => {
                let patch = null, valid = true, validationMessage = '';
                try { patch = getPatch(); } catch (error) { valid = false; validationMessage = error.message; }
                const validation = form.querySelector('[data-chat-config-error]');
                if (validation) { validation.textContent = validationMessage; validation.hidden = !validationMessage; }
                saveBtn.disabled = Boolean(form._saving) || !valid || !patch;
                const overrides = form._resetDefaults ? {} : {...config.overrides};
                Object.assign(overrides, patch?.set || {});
                const custom = Object.keys(overrides).length > 0;
                form.querySelector('[data-chat-config-status]').textContent = custom ? '已自定义' : '默认配置';
                resetBtn.disabled = Boolean(form._saving) || (!custom && !patch);
            };
            const applyValues = values => {
                if (translation) UI.applyTranslationValues(form, values);
                else fields.forEach(field => {
                    if (!Object.hasOwn(values, field.key)) return;
                    const input = [...form.querySelectorAll('[data-config-key]')].find(item=>item.dataset.configKey===field.key);
                    if (input) input.closest('.cap-settings-field').outerHTML = UI.renderCapabilitySettingsField({...field, value: values[field.key]});
                });
                refresh();
            };
            if (translation) {
                UI.bindTranslationSettings(form, {scope: 'chat', onChange: refresh});
                this.bindConfigTemplates(modal, name, getValues, applyValues);
                this.bindTranslationPreview(modal, getValues, userId);
            }
            form.addEventListener('submit', event=>event.preventDefault());
            form.addEventListener('input', refresh);
            form.addEventListener('change', refresh);
            resetBtn.onclick = () => {
                form._resetDefaults = true;
                applyValues(config.defaults);
            };
            saveBtn.classList.remove('d-none');
            saveBtn.textContent = '保存';
            saveBtn.onclick = async () => {
                if (form._saving || !parentForm.isConnected || !form.isConnected) return;
                let patch;
                try { patch = getPatch(); } catch (error) { UI.showError(error.message); return; }
                if (!patch) return;
                const invalid = form.querySelector(':invalid');
                if (invalid) {
                    for (let node = invalid.parentElement; node && node !== form; node = node.parentElement) {
                        if (node.tagName === 'DETAILS') node.open = true;
                    }
                }
                if (!form.reportValidity()) return;
                form._saving = true;
                form.inert = true;
                refresh();
                saveBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>正在保存';
                try {
                    const updated = await API.chatPolicies.update(userId, {
                        expected_version: Number(parentForm.dataset.version), plugin_configs: {[name]: patch}
                    });
                    if (parentForm.isConnected) {
                        parentForm.dataset.version = String(updated.version);
                        this._selectedChatPolicy = updated;
                        const drafts = JSON.parse(parentForm.elements.plugin_config_draft.value || '{}');
                        delete drafts[name];
                        parentForm.elements.plugin_config_draft.value = JSON.stringify(drafts);
                        UI.setPluginScopeBadge(parentForm, name, Object.keys(updated.plugin_configs?.[name]?.overrides || {}).length > 0);
                    }
                    bootstrap.Modal.getInstance(modal)?.hide();
                    UI.showSuccess('聊天配置已保存');
                } catch (error) { UI.showError(`保存失败：${error.message}`); }
                finally {
                    form._saving = false;
                    form.inert = false;
                    if (form.isConnected) { saveBtn.textContent = '保存'; refresh(); }
                }
            };
            refresh();
            new bootstrap.Modal(modal).show();
        } catch (error) { UI.showError(`加载聊天配置失败：${error.message}`); }
    },

    bindConfigTemplates(modal, pluginName, getValues, applyValues) {
        const box = modal.querySelector('[data-template-manage]')?.closest('.translation-prompt-editor');
        const select = modal.querySelector('[data-config-template-select]');
        if (!box || !select) return;
        const nameInput = box.querySelector('[data-template-name]');
        const status = box.querySelector('[data-template-status]');
        const manager = box.querySelector('[data-template-manage]');
        const controls = [...box.querySelectorAll('[data-config-template-select], [data-template-load], [data-template-name], [data-template-create], [data-template-update], [data-template-rename], [data-template-delete]')];
        let templates = [];
        const selected = () => templates.find(item => String(item.id) === select.value);
        const refresh = async id => {
            templates = (await API.capabilities.getConfigTemplates(pluginName)).templates;
            if (!box.isConnected) return;
            select.innerHTML = '<option value="">选择模板</option>' + templates.map(item => `<option value="${Number(item.id)}">${UI.escapeHtml(item.name)}</option>`).join('');
            select.value = String(id || '');
        };
        const act = async fn => {
            controls.forEach(control => control.disabled = true);
            status.textContent = '';
            try { await fn(); }
            catch (error) { if (box.isConnected) status.textContent = error.message; }
            finally { controls.forEach(control => control.disabled = false); }
        };
        select.addEventListener('change', () => { nameInput.value = selected()?.name || ''; });
        box.querySelector('[data-template-load]').onclick = () => {
            const template = selected();
            if (!template) { status.textContent = '请先选择模板'; return; }
            applyValues(structuredClone(template.values));
            if (manager) manager.open = false;
            status.textContent = `已导入「${template.name}」，可继续修改。`;
        };
        for (const [attribute, update] of [['data-template-create', false], ['data-template-update', true]]) {
            box.querySelector(`[${attribute}]`).onclick = () => act(async () => {
                const template = selected();
                if (update && !template) throw Error('请先选择要覆盖的模板');
                const name = nameInput.value.trim();
                if (!name) throw Error('请输入模板名称');
                if (update && !await UI.confirm(`用当前语言与提示词覆盖模板「${template.name}」？`, {
                    title: '覆盖模板', confirmText: '覆盖'
                })) return;
                const result = await API.capabilities.saveConfigTemplate(pluginName, {name, values: getValues(),
                    ...(update ? {template_id: template.id, expected_version: template.version} : {})});
                await refresh(result.id);
                if (manager) manager.open = false;
                status.textContent = update ? '模板已覆盖。' : '模板已保存，可在其他聊天导入。';
            });
        }
        box.querySelector('[data-template-rename]').onclick = () => act(async () => {
            const template = selected();
            if (!template) throw Error('请先选择要重命名的模板');
            const name = nameInput.value.trim();
            if (!name) throw Error('请输入新的模板名称');
            if (!await UI.confirm(`将模板「${template.name}」重命名为「${name}」？`, {
                title: '重命名模板', confirmText: '重命名'
            })) return;
            await API.capabilities.saveConfigTemplate(pluginName, {name, values: structuredClone(template.values),
                template_id: template.id, expected_version: template.version});
            await refresh(template.id);
            if (manager) manager.open = false;
            status.textContent = `模板已重命名为「${name}」。`;
        });
        box.querySelector('[data-template-delete]').onclick = () => act(async () => {
            const template = selected();
            if (!template) throw Error('请先选择要删除的模板');
            if (!await UI.confirm(`删除模板「${template.name}」？已导入的聊天不受影响。`, {
                title: '删除模板', confirmText: '删除', variant: 'danger'
            })) return;
            await API.capabilities.deleteConfigTemplate(pluginName, {template_id: template.id, expected_version: template.version});
            await refresh(); nameInput.value = '';
            if (manager) manager.open = false;
            status.textContent = '模板已删除，已配置的聊天保持原有配置。';
        });
        act(() => refresh());
    },

    bindTranslationPreview(modal, getValues, userId) {
        const container = modal.querySelector('[data-translation-preview]');
        if (!container) return;
        const run = async runModel => {
            const buttons = [...container.querySelectorAll('button')];
            buttons.forEach(button => button.disabled = true);
            const output = container.querySelector('[data-translation-result]');
            try {
                const response = await API.capabilities.previewTranslation({values: getValues(), user_id: userId || null,
                    text: container.querySelector('[data-translation-sample]').value, run_model: runModel});
                if (!container.isConnected) return;
                const reasons = {emoji_only: '纯表情，已跳过', no_translatable_text: '没有需要翻译的文字', unsupported_source: '源语言不属于所选语言', ambiguous: '无法确定主要语言'};
                output.textContent = runModel ? response.result.text || reasons[response.result.status] || response.result.status : response.prompt;
                output.hidden = false;
            } catch (error) {
                if (!container.isConnected) return;
                output.textContent = error.message; output.hidden = false;
            } finally { buttons.forEach(button => button.disabled = false); }
        };
        container.querySelector('[data-preview-prompt]').onclick = () => run(false);
        container.querySelector('[data-preview-translation]').onclick = () => run(true);
    },

    async showCapabilitySettings(name, options = {}) {
        const requestId = this._capabilitySettingsRequest = (this._capabilitySettingsRequest || 0) + 1;
        try {
            const [detailResponse, settings] = await Promise.all([
                API.capabilities.getDetail(name),
                API.capabilities.getSettings(name)
            ]);
            if (requestId !== this._capabilitySettingsRequest) return;
            const capability = detailResponse.capability || {};
            this.currentCapabilitySettings = settings;
            this.currentCapabilityId = name;

            const displayName = capability.display_name || name;
            document.getElementById('configModalTitle').textContent = `${displayName} · 默认配置`;
            document.getElementById('configModalBody').innerHTML = UI.renderCapabilitySettingsForm(settings, capability);

            const saveBtn = document.getElementById('configModalSaveBtn');
            saveBtn.classList.remove('d-none');
            saveBtn.textContent = '保存默认配置';
            saveBtn.onclick = () => this.saveCapabilitySettings(name);

            const modalElement = document.getElementById('configModal');
            new bootstrap.Modal(modalElement).show();
            UI.bindCapabilitySettingsControls(modalElement);
            if (name === 'builtin_translation') {
                saveBtn.disabled = !UI.translationState(modalElement).valid;
                modalElement.addEventListener('input', () => {
                    saveBtn.disabled = !UI.translationState(modalElement).valid;
                });
                this.bindConfigTemplates(modalElement, name,
                    () => UI.readTranslationValues(modalElement),
                    values => UI.applyTranslationValues(modalElement, values));
                this.bindTranslationPreview(modalElement, () => UI.readTranslationValues(modalElement));
            }
            if ((capability.features || []).includes('push')) {
                const shell = modalElement.querySelector('.cap-settings-shell');
                shell.pushReady = this.loadCapabilityPushRecipients(name, shell);
            }
            if (options.focusGroup) {
                UI.focusCapabilitySettingsGroup(modalElement, options.focusGroup);
            }
        } catch (e) {
            UI.showError('加载设置失败：' + e.message);
        }
    },

    async loadCapabilityPushRecipients(name, shell) {
        const container = shell.querySelector('[data-push-recipients]');
        const summary = shell.querySelector('[data-push-summary]');
        const search = shell.querySelector('[data-push-search]');
        const refreshSummary = () => {
            const selected = [...container.querySelectorAll('input:checked')];
            summary.textContent = selected.length ? `已选 ${selected.length} 个对象：${selected.map(i => i.dataset.chatName).join('、')}` : '未选择接收对象';
        };
        try {
            const users = await API.users.getAll();
            if (!shell.isConnected) return;
            container.replaceChildren();
            for (const user of users) {
                const label = document.createElement('label');
                label.className = 'd-flex align-items-center gap-2 py-1';
                const input = document.createElement('input');
                input.type = 'checkbox';
                input.className = 'form-check-input';
                input.dataset.userId = String(user.id);
                input.dataset.chatName = user.chat_name;
                input.checked = (user.permissions || []).some(p => p.plugin_name === `${name}#push`);
                input.dataset.original = String(input.checked);
                input.onchange = refreshSummary;
                const text = document.createElement('span');
                text.textContent = `${user.chat_name}（${user.is_group ? '群聊' : '私聊'}）`;
                label.append(input, text);
                container.append(label);
            }
            if (!users.length) container.textContent = '暂无聊天，请先在“聊天”页面添加接收对象。';
            search.oninput = () => {
                for (const label of container.querySelectorAll('label')) {
                    label.classList.toggle('d-none', !label.textContent.toLowerCase().includes(search.value.trim().toLowerCase()));
                }
            };
            refreshSummary();
            shell.savePushRecipients = async () => {
                for (const input of container.querySelectorAll('input')) {
                    if (String(input.checked) === input.dataset.original) continue;
                    const checked = input.checked;
                    const policy = await API.chatPolicies.get(input.dataset.userId);
                    const grants = (policy.plugin_grants || []).filter(p => p.plugin_name !== `${name}#push`)
                        .map(p => ({plugin_name: p.plugin_name, require_mention: Boolean(p.require_mention)}));
                    if (checked) grants.push({plugin_name: `${name}#push`, require_mention: false});
                    const updated = await API.chatPolicies.update(input.dataset.userId, {expected_version: policy.version, plugin_grants: grants});
                    this.syncSavedPushRecipient(input.dataset.userId, name, checked, policy.version, updated);
                    input.dataset.original = String(checked);
                }
            };
        } catch (error) {
            shell.pushLoadError = error;
            summary.textContent = '接收对象加载失败，请重新打开设置';
            container.textContent = error.message;
        }
    },

    syncSavedPushRecipient(userId, name, checked, previousVersion, updated) {
        const form = document.getElementById('chatPolicyForm');
        if (!form || form.dataset.userId !== String(userId) || Number(form.dataset.version) !== previousVersion || !updated?.version) return;
        const controls = [...form.querySelectorAll('input[name], select[name], textarea[name], .chat-policy-plugin-toggle, .chat-policy-plugin-mention, .chat-policy-plugin-push')];
        const card = [...form.querySelectorAll('[data-plugin-card]')].find(item => item.dataset.pluginCard === name);
        const input = card?.querySelector('.chat-policy-plugin-push');
        const baseline = JSON.parse(form._initialSnapshot || '[]');
        const index = controls.indexOf(input);
        if (input && baseline[index]) {
            // Preserve an unsaved choice in the chat form while advancing its baseline.
            if (input.checked === baseline[index].checked) input.checked = checked;
            baseline[index].checked = checked;
            form._initialSnapshot = JSON.stringify(baseline);
            input.dispatchEvent(new Event('change', {bubbles:true}));
        }
        form.dataset.version = String(updated.version);
        this._selectedChatPolicy = updated;
        UI.syncChatPolicyDirty(form);
    },

    async saveCapabilitySettings(name) {
        const form = document.getElementById('capabilitySettingsForm');
        const shell = form?.closest('.cap-settings-shell');
        if (!form || shell.dataset.capabilityId !== name) return;
        if (!form.checkValidity()) {
            form.reportValidity();
            return;
        }

        let values;
        try {
            values = this.collectConfigValues(form);
        } catch (error) {
            UI.showError(error.message);
            return;
        }

        const saveBtn = document.getElementById('configModalSaveBtn');
        const originalHtml = saveBtn.innerHTML;
        saveBtn.disabled = true;
        saveBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>正在应用';
        try {
            await shell.pushReady;
            if (shell.pushLoadError) throw shell.pushLoadError;
            if (!shell.isConnected) return;
            shell.querySelectorAll('[data-push-recipients] input').forEach(input => { input.disabled = true; });
            await API.capabilities.updateSettings(name, values);
            await shell.savePushRecipients?.();
            if (!shell.isConnected) return;
            const modal = bootstrap.Modal.getInstance(document.getElementById('configModal'));
            if (modal) modal.hide();
            UI.showSuccess('设置已保存并应用');
            await this.loadPlugins(false);
        } catch (e) {
            UI.showError('部分设置可能已保存，请重试：' + e.message);
        } finally {
            shell.querySelectorAll('[data-push-recipients] input').forEach(input => { input.disabled = false; });
            saveBtn.disabled = false;
            saveBtn.innerHTML = originalHtml;
        }
    },

    // Users & Listeners
    async loadUsers() {
        try {
            const [usersData, listenersData, profilesData, assistantOverview] = await Promise.all([
                API.users.getAll(),
                API.wechat.getListeners(),
                API.codexProfiles.list().catch(() => ({ profiles: [], default_profile_id: '' })),
                API.assistant.getOverview()
            ]);
            this.updateManagedChatProfiles(profilesData);
            this.updateManagedChatAssistantOverview(assistantOverview);

            const dbUsers = usersData || [];
            // Handle different structure of listeners response
            const activeChatsMap = listenersData.listened_chats || listenersData || {};
            const activeChatNames = new Set(Object.keys(activeChatsMap));

            // Merge Data
            // Map: chat_name -> { id: int|null, chat_name: str, has_permission_config: bool, is_listening: bool }
            const mergedMap = new Map();

            // 1. Add DB Users
            dbUsers.forEach(u => {
                mergedMap.set(u.chat_name, {
                    ...u,
                    has_permission_config: true,
                    is_listening: activeChatNames.has(u.chat_name)
                });
            });

            // 2. Add Active Listeners not in DB
            activeChatNames.forEach(name => {
                if (!mergedMap.has(name)) {
                    mergedMap.set(name, {
                        id: null, // No DB ID yet
                        chat_name: name,
                        has_permission_config: false,
                        is_listening: true,
                        is_group: false // Unknown, assume false or handle in UI
                    });
                }
            });

            const mergedList = Array.from(mergedMap.values()).sort((a, b) => {
                // Sort by: Listening -> Configured -> Name
                if (a.is_listening !== b.is_listening) return b.is_listening - a.is_listening;
                return a.chat_name.localeCompare(b.chat_name);
            });

            this._managedChats = mergedList;
            UI.updateMetric('managedChatsCount', mergedList.length);
            UI.bindManagedChatPicker();
            const search = document.getElementById('chatListSearch');
            if (search && !search.dataset.bound) {
                search.dataset.bound = 'true';
                search.addEventListener('input', UI.debounce(() => this.filterManagedChats(), 120));
            }
            document.querySelectorAll('[data-chat-filter]').forEach(button => {
                if (button.dataset.bound) return;
                button.dataset.bound = 'true';
                button.addEventListener('click', () => {
                    this.managedChatFilter = button.dataset.chatFilter || 'all';
                    document.querySelectorAll('[data-chat-filter]').forEach(item => {
                        item.classList.toggle('active', item === button);
                    });
                    this.filterManagedChats();
                });
            });
            this.filterManagedChats();
            if (!this.currentThreadName) {
                const requestedChatId = Number(new URLSearchParams(window.location.search).get('chat_id'));
                const firstManagedChat = mergedList.find(chat => chat.id === requestedChatId) || mergedList.find(chat => chat.id);
                if (firstManagedChat) {
                    await this.selectUser(firstManagedChat.chat_name, firstManagedChat.id);
                }
            } else {
                UI.setActiveManagedChat(this.currentThreadName);
            }
        } catch (e) {
            UI.showError('加载用户失败：' + e.message);
        }
    },

    filterManagedChats() {
        const query = String(document.getElementById('chatListSearch')?.value || '').trim().toLowerCase();
        const state = this.managedChatFilter || 'all';
        const chats = (this._managedChats || []).filter(chat => {
            const matchesQuery = !query || chat.chat_name.toLowerCase().includes(query);
            const matchesState = state === 'all'
                || (state === 'active' && chat.is_listening)
                || (state === 'paused' && !chat.is_listening);
            return matchesQuery && matchesState;
        });
        UI.renderUsersList(chats);
    },

    showAddUserModal() {
        // Reset form
        const form = document.getElementById('addUserForm');
        if (form) form.reset();

        // Show Modal
        const el = document.getElementById('addUserModal');
        if (el && window.bootstrap) {
            const modal = new bootstrap.Modal(el);
            modal.show();
        }
    },

    async submitAddUser() {
        const form = document.getElementById('addUserForm');
        if (!form) return;

        const chatName = form.chat_name.value.trim();
        const isGroup = form.is_group.value === 'true';

        if (!chatName) {
            UI.showError('聊天名称不能为空');
            return;
        }

        try {
            const user = await API.users.addUser(chatName, isGroup, null);
            let assistantWarning = '';
            if (form.assistant_enabled?.checked) {
                try {
                    await API.chatPolicies.update(user.id, {
                        expected_version: Number(user.policy_version || 1),
                        assistant: { enabled: true }
                    });
                } catch (error) {
                    assistantWarning = error.message;
                }
            }

            // Close modal
            const el = document.getElementById('addUserModal');
            const modal = bootstrap.Modal.getInstance(el);
            if (modal) modal.hide();

            await this.loadUsers();
            await this.selectUser(user.chat_name, user.id);
            if (assistantWarning) {
                UI.showError(`聊天已添加并开始监听，但 AI 助手未能启用：${assistantWarning}`);
            } else {
                UI.showSuccess(form.assistant_enabled?.checked ? '聊天已就绪，AI 助手可以开始回复' : '聊天已添加，插件可独立运行');
            }
        } catch (e) {
            UI.showError('添加用户失败：' + e.message);
        }
    },

    async getManagedChatReferenceData(force = false) {
        if (force) this._managedChatReferenceData = null;
        if (this._managedChatReferenceData) return this._managedChatReferenceData;
        if (!this._managedChatReferencePromise) {
            const overviewRevision = this._assistantOverviewRevision || 0;
            this._managedChatReferencePromise = Promise.all([
                API.capabilities.getAll(),
                API.assistant.getOverview(),
                this._managedChatProfilesData
                    ? Promise.resolve(this._managedChatProfilesData)
                    : API.codexProfiles.list().catch(() => ({ profiles: [], default_profile_id: '' }))
            ]).then(([capabilitiesData, assistantOverview, profiles]) => {
                this._managedChatProfilesData = profiles;
                this._managedChatReferenceData = {
                    capabilities: capabilitiesData.capabilities || [],
                    assistantOverview: overviewRevision === (this._assistantOverviewRevision || 0)
                        ? assistantOverview : this._assistantOverview,
                    profiles
                };
                return this._managedChatReferenceData;
            }).finally(() => {
                this._managedChatReferencePromise = null;
            });
        }
        return this._managedChatReferencePromise;
    },

    updateManagedChatAssistantOverview(overview) {
        this._assistantOverview = overview;
        this._assistantOverviewRevision = (this._assistantOverviewRevision || 0) + 1;
        if (this._managedChatReferenceData) {
            this._managedChatReferenceData.assistantOverview = overview;
        }
        UI.updateChatPolicyAssistantOptions(overview);
    },

    updateManagedChatProfiles(profilesData) {
        const normalized = profilesData && typeof profilesData === 'object'
            ? profilesData
            : { profiles: [], default_profile_id: '' };
        this._managedChatProfilesData = normalized;
        if (this._managedChatReferenceData) {
            this._managedChatReferenceData.profiles = normalized;
        }
        UI.updateChatPolicyProfileOptions(normalized);
    },

    // Permission Management Integration
    async selectUser(chatName, userId) {
        const selectedChatName = String(chatName || '');
        if (!selectedChatName) return;
        if (this.currentThreadName
            && this.currentThreadName !== selectedChatName
            && UI.isChatPolicyDirty()) {
            const discard = await UI.confirm('当前聊天有尚未保存的更改。切换后这些更改会丢失。', {
                title: '放弃未保存的更改？',
                confirmText: '放弃并切换',
                variant: 'warning'
            });
            if (!discard) {
                UI.setActiveManagedChat(this.currentThreadName);
                return false;
            }
        }
        const requestId = ++this.managedChatSelectionRequest;
        this.currentThreadName = selectedChatName;
        UI.closeManagedChatPicker();
        UI.setActiveManagedChat(selectedChatName);
        UI.renderManagedChatPending(selectedChatName);

        if (!userId) {
            this._selectedChatPolicy = null;
            UI.renderUnmanagedChatPolicy(selectedChatName);
            return true;
        }

        try {
            const [referenceData, policy] = await Promise.all([
                this.getManagedChatReferenceData(),
                API.chatPolicies.get(userId)
            ]);

            // A slower response for a previously selected chat must never
            // replace the panel for the user's latest selection.
            if (requestId !== this.managedChatSelectionRequest
                || this.currentThreadName !== selectedChatName) return;

            this._selectedChatPolicy = policy;
            UI.renderChatPolicy(
                policy,
                referenceData.capabilities,
                referenceData.assistantOverview,
                referenceData.profiles
            );
            return true;
        } catch (e) {
            if (requestId !== this.managedChatSelectionRequest) return;
            UI.renderManagedChatError(selectedChatName);
            UI.showError('加载用户详情失败：' + e.message);
            return false;
        }
    },

    async adoptActiveChat(chatName, isGroup) {
        try {
            const user = await API.users.addUser(chatName, Boolean(isGroup));
            UI.showSuccess('聊天已加入策略管理');
            await this.loadUsers();
            await this.selectUser(user.chat_name, user.id);
        } catch (error) {
            UI.showError(`加入失败：${error.message}`);
        }
    },

    linesFromPolicyField(form, name) {
        return [...new Set(String(form.elements[name]?.value || '').split(/\r?\n|,/).map(item => item.trim()).filter(Boolean))];
    },

    async saveChatPolicy() {
        const form = document.getElementById('chatPolicyForm');
        if (!form) return;
        if (!form.checkValidity()) {
            form.reportValidity();
            return;
        }
        const userId = Number(form.dataset.userId || 0);
        const isGroup = form.elements.chat_type.value === 'group';
        const originalIsGroup = form.dataset.originalGroup === 'true';
        const codexMode = isGroup ? 'isolated' : form.elements.codex_mode.value;
        if (codexMode === 'owner_full' && this._selectedChatPolicy?.codex?.mode !== 'owner_full') {
            const approved = await UI.confirm('最大权限允许此私聊中的 Codex 访问本机文件。只应授予你本人可控的私聊。', { title: '确认 Codex 最大权限', confirmText: '确认授予', variant: 'warning' });
            if (!approved) return;
        }
        const pluginGrants = [];
        form.querySelectorAll('.chat-policy-plugin-toggle:checked').forEach(toggle => {
            const card = toggle.closest('.chat-policy-plugin');
            const mentionToggle = card.querySelector('.chat-policy-plugin-mention');
            pluginGrants.push({
                plugin_name: toggle.value,
                require_mention: isGroup ? (mentionToggle ? mentionToggle.checked : true) : false
            });
        });
        form.querySelectorAll('.chat-policy-plugin-push:checked').forEach(input => {
            const toggle = input.closest('.chat-policy-plugin').querySelector('.chat-policy-plugin-toggle');
            pluginGrants.push({ plugin_name: `${toggle.value}#push`, require_mention: false });
        });
        const roleValue = form.elements.role_id.value;
        const proactiveEnabled = Boolean(isGroup && form.elements.proactive_enabled?.checked);
        const judgeValue = proactiveEnabled ? form.elements.judge_id?.value : '';
        if (proactiveEnabled && !judgeValue) {
            UI.showError('启用主动参与前需要选择一个 接话判断');
            form.elements.judge_id?.focus();
            return;
        }
        const payload = {
            expected_version: Number(form.dataset.version),
            chat: {
                is_group: isGroup,
                listening_enabled: form.elements.listening_enabled.checked,
                attachment_content_review_enabled: form.elements.attachment_content_review_enabled.checked,
                sender_blacklist: this.linesFromPolicyField(form, 'sender_blacklist'),
                ...(isGroup && originalIsGroup ? {
                    bot_group_nickname: form.elements.bot_group_nickname?.value.trim() || '',
                    bot_group_nickname_auto_enabled: Boolean(form.elements.bot_group_nickname_auto_enabled?.checked)
                } : {})
            },
            assistant: {
                enabled: form.elements.assistant_enabled.checked,
                codex_profile_id: form.elements.codex_profile_id.value || null,
                role_id: roleValue ? Number(roleValue) : null,
                followup_enabled: form.elements.followup_enabled.checked,
                followup_window_seconds: Number(form.elements.followup_window_seconds.value),
                followup_merge_seconds: Number(form.elements.followup_merge_seconds.value),
                followup_max_turns: Number(form.elements.followup_max_turns.value),


                ignored_senders: this.linesFromPolicyField(form, 'assistant_ignored_senders'),
                ...(isGroup ? {
                    proactive_enabled: proactiveEnabled,
                    judge_id: judgeValue ? Number(judgeValue) : null
                } : { proactive_enabled: false, judge_id: null })
            },
            codex: CodexPermissions.chatPatch(form),
            plugin_grants: pluginGrants,
            plugin_configs: JSON.parse(form.elements.plugin_config_draft?.value || '{}')
        };
        const controls = [...form.querySelectorAll('input, select, textarea, button')]
            .map(control => [control, control.disabled]);
        controls.forEach(([control]) => { control.disabled = true; });
        UI.setChatPolicySaving(true);
        try {
            const updated = await API.chatPolicies.update(userId, payload);
            this._selectedChatPolicy = updated;
            await this.loadUsers();
            const referenceData = await this.getManagedChatReferenceData();
            UI.renderChatPolicy(
                updated,
                referenceData.capabilities,
                referenceData.assistantOverview,
                referenceData.profiles
            );
            UI.showSuccess('聊天策略已保存');
            (updated.side_effect_warnings || []).forEach(message => UI.showInfo(message));
        } catch (error) {
            UI.showError(error.status === 409 ? '配置已被其他页面更新。已保留输入，请核对后重新保存。' : `保存失败：${error.message}`);
            if (error.status === 409) {
                const latest = await API.chatPolicies.get(userId).catch(() => null);
                if (latest) {
                    form.dataset.version = latest.version;
                    this._selectedChatPolicy = latest;
                    if (form._permissions) form._permissions = latest.codex;
                }
            }
            // Retain the draft; an explicit second save is required after conflict review.
        } finally {
            controls.forEach(([control, disabled]) => {
                if (control.isConnected) control.disabled = disabled;
            });
            if (form.isConnected) UI.syncChatPolicyDirty(form);
        }
    },


    deleteSelectedManagedChat() {
        const userId = Number(this._selectedChatPolicy?.user_id || 0);
        const chatName = this._selectedChatPolicy?.chat?.chat_name || this.currentThreadName;
        if (userId && chatName) return this.deleteUser(userId, chatName);
    },

    async deleteUser(userId, chatName) {
        if (!await UI.confirm('确定删除此用户吗？删除后将停止监听并移除所有权限。', {
            title: '删除用户',
            confirmText: '删除',
            variant: 'danger'
        })) return;
        try {
            // First try to stop listening (if currently active)
            try {
                await API.wechat.removeListener(chatName);
            } catch (e) {
                console.log('Note: Could not remove listener (may not be active):', e.message);
            }

            // Delete from database
            await API.users.delete(userId);

            if (this.currentThreadName === chatName) {
                this.currentThreadName = '';
                this._selectedChatPolicy = null;
                UI.resetManagedChatContext();
            }
            await this.loadUsers();
            UI.showSuccess('聊天已删除');
        } catch (e) {
            UI.showError('删除用户失败：' + e.message);
        }
    },

    async loadRoles() {
        try {
            // One aggregated request replaces the previous N+1 sequence (users,
            // each user's permissions, role binding and 接话判断 binding).
            const overview = await API.assistant.getOverview();
            const roles = overview.roles || [];
            const judges = overview.judges || [];
            const chats = overview.chats || [];

            this.updateManagedChatAssistantOverview(overview);
            this._roles = roles;
            this._judges = judges;
            this._assistantChats = chats;

            UI.updateMetric('statsTotalRoles', roles.length);
            UI.updateMetric('statsTotalJudges', judges.length);
            this.filterAndRenderRoles(document.getElementById('rolesSearchInput')?.value || '');
            this.setAssistantSection(this.getAssistantSectionFromPath(), { history: false });

            const searchInput = document.getElementById('rolesSearchInput');
            if (searchInput && !searchInput.dataset.bound) {
                searchInput.dataset.bound = "true";
                searchInput.addEventListener('input', UI.debounce((e) => {
                    this.filterAndRenderRoles(e.target.value);
                }, 150));
            }


        } catch (e) {
            console.error('Error loading roles/judges:', e);
            UI.showError('加载角色管理失败：' + e.message);
        }
    },

    getAssistantSectionFromPath() {
        return UI.normalizePath(window.location.pathname) === '/assistant/judges' ? 'judges' : 'roles';
    },

    setAssistantSection(section, options = {}) {
        if (section === 'chats') { UI.switchTab('users'); return; }
        const selected = section === 'judges' ? 'judges' : 'roles';
        this._assistantSection = selected;
        const ids = { roles: 'assistantRolesSection', judges: 'assistantJudgesSection' };
        document.querySelectorAll('[data-assistant-section]').forEach(button => {
            const active = button.dataset.assistantSection === selected;
            button.classList.toggle('active', active);
            button.setAttribute('aria-selected', String(active));
            button.id = `assistant-${button.dataset.assistantSection}-tab`;
            button.setAttribute('aria-controls', ids[button.dataset.assistantSection]);
            button.tabIndex = active ? 0 : -1;
            button.onkeydown = event => {
                if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
                event.preventDefault();
                const next = event.key === 'Home' ? 'roles' : event.key === 'End' ? 'judges' : selected === 'roles' ? 'judges' : 'roles';
                this.setAssistantSection(next);
                document.querySelector(`[data-assistant-section="${next}"]`).focus();
            };
        });
        Object.entries(ids).forEach(([key, id]) => {
            const panel = document.getElementById(id);
            panel?.classList.toggle('d-none', key !== selected);
            panel?.setAttribute('role', 'tabpanel');
            panel?.setAttribute('aria-labelledby', `assistant-${key}-tab`);
        });
        const search = document.getElementById('rolesSearchInput');
        const name = selected === 'roles' ? '角色' : '接话判断';
        if (search) { search.placeholder = `搜索${name}名称、用途或提示词…`; search.setAttribute('aria-label', `搜索${name}`); }
        const create = document.querySelector('#assistantCreateButton span');
        if (create) create.textContent = `新建${name}`;
        if (options.history !== false) {
            const path = selected === 'judges' ? '/assistant/judges' : '/assistant/roles';
            if (UI.normalizePath(window.location.pathname) !== path) window.history.pushState({ tab: 'roles', section: selected }, '', path);
        }
    },

    createAssistantEntry() {
        if (this._assistantSection === 'judges') this.showCreateJudgeModal();
        else this.showCreateRoleModal();
    },

    async showAssistantGlobalSettings() {
        await this.showCapabilitySettings('assistant');
    },

    openAssistantRoleManager(section = 'roles') {
        const selected = section === 'judges' ? 'judges' : 'roles';
        window.history.pushState({ tab: 'roles', section: selected }, '', `/assistant/${selected}`);
        UI.switchTab('roles', { history: false });
        this.setAssistantSection(selected, { history: false });
    },

    async showAssistantChatEditor(userId) {
        const chat = (this._assistantChats || []).find(item => item.id === userId);
        if (!chat) return;
        UI.switchTab('users');
        await this.selectUser(chat.chat_name, userId);
    },

    getAvatarColor(name) {
        const colors = [
            'linear-gradient(135deg, var(--primary), var(--primary-active))',
            'linear-gradient(135deg, var(--accent-teal), var(--primary))',
            'linear-gradient(135deg, var(--success), var(--accent-teal))',
            'linear-gradient(135deg, var(--accent-amber), var(--warning))',
            'linear-gradient(135deg, var(--body-strong), var(--ink))',
            'linear-gradient(135deg, var(--muted), var(--body-strong))',
            'linear-gradient(135deg, var(--primary), var(--accent-amber))',
            'linear-gradient(135deg, var(--accent-teal), var(--success))'
        ];
        let hash = 0;
        for (let i = 0; i < name.length; i++) {
            hash = name.charCodeAt(i) + ((hash << 5) - hash);
        }
        const index = Math.abs(hash) % colors.length;
        return colors[index];
    },

    getInitials(name) {
        if (!name) return '??';
        const cleanName = name.replace(/[^\w\s\u4e00-\u9fa5]/g, '').trim();
        if (cleanName.length === 0) return name.substring(0, Math.min(2, name.length));

        const isChinese = /^[\u4e00-\u9fa5]+$/.test(cleanName);
        if (isChinese) {
            return cleanName.substring(0, Math.min(2, cleanName.length));
        }

        const words = cleanName.split(/\s+/);
        if (words.length >= 2) {
            return (words[0][0] + words[1][0]).toUpperCase();
        }
        return cleanName.substring(0, Math.min(2, cleanName.length)).toUpperCase();
    },

    filterAndRenderRoles(query) {
        const normalizeSearchValue = value => String(value ?? '').toLowerCase();
        const q = normalizeSearchValue(query).trim();

        // Filter roles
        const filteredRoles = (this._roles || []).filter(r =>
            normalizeSearchValue(r.display_name).includes(q) ||
            normalizeSearchValue(r.name).includes(q) ||
            normalizeSearchValue(r.description).includes(q) ||
            normalizeSearchValue(r.prompt).includes(q)
        );

        // Filter judges
        const filteredJudges = (this._judges || []).filter(j =>
            normalizeSearchValue(j.display_name).includes(q) ||
            normalizeSearchValue(j.name).includes(q) ||
            normalizeSearchValue(j.description).includes(q) ||
            normalizeSearchValue(j.prompt).includes(q)
        );

        this.renderPremiumRoles(filteredRoles, query);
        this.renderPremiumJudges(filteredJudges, query);
    },

    getAssistantEntrySummary(item) {
        const description = String(item.description || '').trim();
        if (description) return description;
        // This is a plain-text excerpt, never an inferred description or rendered Markdown.
        return String(item.prompt || '')
            .replace(/^\s{0,3}#{1,6}\s+/gm, '')
            .replace(/^\s*[-*+]\s+/gm, '')
            .replace(/\*\*|__|`/g, '')
            .replace(/\s+/g, ' ')
            .trim()
            .slice(0, 160) || '尚未填写用途说明';
    },

    renderAssistantEntryActions(kind, item) {
        const isRole = kind === 'role';
        const label = isRole ? '角色' : '接话判断';
        const method = isRole ? 'Role' : 'Judge';
        const id = Number(item.id);
        const name = UI.escapeHtml(item.display_name || item.name || `未命名${label}`);
        const userCount = Number(item.user_count || 0);
        const canDelete = userCount === 0;
        const reason = canDelete ? '' : `已关联 ${userCount} 个聊天，解除关联后可删除。`;
        return `
            <div class="roles-entry-actions">
                <button type="button" class="btn btn-sm btn-surface" onclick="App.show${method}Editor(${id})"
                        aria-label="编辑${label}：${name}">编辑</button>
                <span class="roles-entry-delete-wrap" title="${reason || `删除${label}`}"
                      ${reason ? `tabindex="0" role="group" aria-label="无法删除${label}：${name}。${reason}"` : ''}>
                    <button type="button" class="btn btn-sm roles-entry-delete" onclick="App.delete${method}(${id})"
                            aria-label="删除${label}：${name}" title="${reason || `删除${label}`}" ${canDelete ? '' : 'disabled'}>
                        <i class="bi bi-trash" aria-hidden="true"></i>
                    </button>
                </span>
            </div>`;
    },

    renderAssistantCollectionEmpty(kind, query) {
        if (String(query || '').trim()) {
            return `<div class="assistant-empty-inline roles-collection-empty" role="status">
                <span>没有匹配“${UI.escapeHtml(String(query).trim())}”的${kind === 'role' ? '角色' : '接话判断'}</span>
                <button type="button" class="btn btn-sm btn-surface" onclick="App.clearAssistantEntrySearch()">清除筛选</button>
            </div>`;
        }
        return `<div class="assistant-empty-inline roles-collection-empty">${kind === 'role'
            ? '还没有角色，点击“新建角色”添加第一个。'
            : '还没有接话判断，点击“新建接话判断”配置主动回复条件。'}</div>`;
    },

    clearAssistantEntrySearch() {
        const search = document.getElementById('rolesSearchInput');
        if (search) search.value = '';
        this.filterAndRenderRoles('');
        search?.focus();
    },

    renderAssistantEntryChats(kind, identity) {
        const chats = (this._assistantChats || []).filter(chat => Number(chat[kind]?.id) === Number(identity));
        if (!chats.length) return '';
        return `<div class="roles-entry-chats" aria-label="关联聊天">${chats.map(chat => `<button type="button" class="chat-policy-inline-action" onclick="App.showAssistantChatEditor(${Number(chat.id)})">${UI.escapeHtml(chat.chat_name)}</button>`).join('')}</div>`;
    },

    renderPremiumRoles(rolesList, query = '') {
        const container = document.getElementById('rolesGrid');
        if (!container) return;

        container.innerHTML = rolesList.map(r => {
            const roleId = Number(r.id);
            const displayName = UI.escapeHtml(r.display_name || r.name || '未命名角色');
            const summary = UI.escapeHtml(this.getAssistantEntrySummary(r));
            const userCount = Number(r.user_count || 0);
            return `
                <article class="roles-entry" aria-labelledby="role-${roleId}-title">
                    <div class="roles-entry-head">
                        <div class="roles-entry-identity">
                            <h6 id="role-${roleId}-title" title="${displayName}">${displayName}</h6>
                            <span class="roles-entry-usage">${userCount > 0 ? `已关联 ${userCount} 个聊天` : '暂未关联聊天'}</span>
                        </div>
                        ${this.renderAssistantEntryActions('role', r)}
                    </div>
                    <p class="roles-entry-summary" title="${summary}">${summary}</p>
                    ${this.renderAssistantEntryChats('role', roleId)}
                    <ul class="roles-entry-properties" aria-label="回复方式">
                        <li>${r.output_split_enabled
                            ? `最多 ${Number(r.output_max_count || 0)} 条 · 建议 ${Number(r.output_max_chars || 0)} 字/条`
                            : '单条消息'}</li>
                        ${r.output_strip_trailing_period ? '<li>移除句号</li>' : ''}
                    </ul>
                </article>`;
        }).join('') || this.renderAssistantCollectionEmpty('role', query);
    },

    renderPremiumJudges(judgesList, query = '') {
        const container = document.getElementById('judgesGrid');
        if (!container) return;

        container.innerHTML = judgesList.map(j => {
            const judgeId = Number(j.id);
            const displayName = UI.escapeHtml(j.display_name || j.name || '未命名接话判断');
            const summary = UI.escapeHtml(this.getAssistantEntrySummary(j));
            const userCount = Number(j.user_count || 0);
            return `
                <article class="roles-entry" aria-labelledby="judge-${judgeId}-title">
                    <div class="roles-entry-head">
                        <div class="roles-entry-identity">
                            <h6 id="judge-${judgeId}-title" title="${displayName}">${displayName}</h6>
                            <span class="roles-entry-usage">${userCount > 0 ? `已关联 ${userCount} 个聊天` : '暂未关联聊天'}</span>
                        </div>
                        ${this.renderAssistantEntryActions('judge', j)}
                    </div>
                    <p class="roles-entry-summary" title="${summary}">${summary}</p>
                    ${this.renderAssistantEntryChats('judge', judgeId)}
                    <dl class="roles-entry-rules">
                        <dt>触发</dt><dd>${Number(j.trigger_msg_threshold || 0)} 条消息，且间隔 ${Number(j.trigger_interval_minutes || 0)} 分钟</dd>
                        <dt>冷却</dt><dd>${Number(j.cooldown_msg_threshold || 0)} 条消息，且经过 ${Number(j.cooldown_minutes || 0)} 分钟</dd>
                    </dl>
                </article>`;
        }).join('') || this.renderAssistantCollectionEmpty('judge', query);
    },

    renderTabbedRoleEditorForm(role = {}, isCreate = false) {
        return PromptWorkbench.render('role', role, isCreate);
    },

    renderTabbedJudgeEditorForm(judge = {}, isCreate = false) {
        return PromptWorkbench.render('judge', judge, isCreate);
    },

    showCreateRoleModal() {
        const html = this.renderTabbedRoleEditorForm({}, true);
        document.getElementById('configModalBody').innerHTML = html;
            PromptWorkbench.mount();
        document.getElementById('configModalTitle').textContent = '创建新角色';

        const saveBtn = document.getElementById('configModalSaveBtn');
        saveBtn.classList.remove('d-none');
        saveBtn.onclick = async () => {
            await this.saveRole(null);
        };

        new bootstrap.Modal(document.getElementById('configModal')).show();
    },

    async showRoleEditor(roleId) {
        try {
            const result = await API.roles.getDetail(roleId);
            const role = result.role;

            const html = this.renderTabbedRoleEditorForm(role, false);
            document.getElementById('configModalBody').innerHTML = html;
            PromptWorkbench.mount();
            document.getElementById('configModalTitle').textContent = `编辑角色：${role.display_name}`;

            const saveBtn = document.getElementById('configModalSaveBtn');
            saveBtn.classList.remove('d-none');
            saveBtn.onclick = async () => {
                await this.saveRole(roleId);
            };

            new bootstrap.Modal(document.getElementById('configModal')).show();

        } catch (e) {
            UI.showError(e.message);
        }
    },

    async saveRole(roleId) {
        const saveButton = document.getElementById('configModalSaveBtn');
        if (saveButton?.disabled) return;
        if (saveButton) saveButton.disabled = true;
        try {
            const form = document.getElementById('roleForm');
            if (!form.checkValidity()) {
                PromptWorkbench.revealInvalid(form);
                return;
            }

            const data = {
                revision: form.elements.revision?.value || null,
                display_name: form.display_name.value,
                description: form.description.value,
                prompt: form.prompt.value,
                output_split_enabled: form.output_split_enabled.checked,
                output_max_chars: parseInt(form.output_max_chars.value || '120', 10),
                output_max_count: parseInt(form.output_max_count.value || '3', 10),
                output_strip_trailing_period: form.output_strip_trailing_period.checked,
                output_interval_seconds: parseFloat(form.output_interval_seconds.value || '1')
            };

            // If creating, we also need the name
            let saved;
            if (!roleId) {
                data.name = form.name.value;
                saved = await API.roles.create(data);
            } else {
                saved = await API.roles.update(roleId, data);
            }

            UI.showSuccess(saved?.applied === false ? '已保存；助手启动或重新加载后生效' : '已保存；下一次请求采用新配置');
            const modal = bootstrap.Modal.getInstance(document.getElementById('configModal'));
            if (modal) modal.hide();

            // Refresh both the role manager and the retained chat policy form.
            await this.loadRoles();
        } catch (e) {
            UI.showError('操作失败：' + e.message);
        } finally {
            if (saveButton) saveButton.disabled = false;
        }
    },

    async deleteRole(roleId) {
        if (!await UI.confirm('确定删除此角色吗？该操作无法撤销。', {
            title: '删除角色',
            confirmText: '删除',
            variant: 'danger'
        })) return;
        try {
            await API.roles.delete(roleId);
            this.loadRoles();
        } catch (e) {
            UI.showError('删除角色失败：' + e.message);
        }
    },

    showCreateJudgeModal() {
        const html = this.renderTabbedJudgeEditorForm({}, true);
        document.getElementById('configModalBody').innerHTML = html;
            PromptWorkbench.mount();
        document.getElementById('configModalTitle').textContent = '创建接话判断';

        const saveBtn = document.getElementById('configModalSaveBtn');
        saveBtn.classList.remove('d-none');
        saveBtn.onclick = async () => {
            await this.saveJudge(null);
        };

        new bootstrap.Modal(document.getElementById('configModal')).show();

    },

    async showJudgeEditor(judgeId) {
        try {
            const result = await API.judges.getDetail(judgeId);
            const judge = result.judge;

            const html = this.renderTabbedJudgeEditorForm(judge, false);
            document.getElementById('configModalBody').innerHTML = html;
            PromptWorkbench.mount();
            document.getElementById('configModalTitle').textContent = `编辑接话判断：${judge.display_name}`;

            const saveBtn = document.getElementById('configModalSaveBtn');
            saveBtn.classList.remove('d-none');
            saveBtn.onclick = async () => {
                await this.saveJudge(judgeId);
            };

            new bootstrap.Modal(document.getElementById('configModal')).show();

        } catch (e) {
            UI.showError(e.message);
        }
    },

    async saveJudge(judgeId) {
        const saveButton = document.getElementById('configModalSaveBtn');
        if (saveButton?.disabled) return;
        if (saveButton) saveButton.disabled = true;
        try {
            const form = document.getElementById('judgeForm');
            if (!form.checkValidity()) {
                PromptWorkbench.revealInvalid(form);
                return;
            }

            const data = {
                revision: form.elements.revision?.value || null,
                display_name: form.display_name.value,
                description: form.description.value,
                prompt_mode: "simple",
                prompt: form.prompt.value,
                trigger_msg_threshold: parseInt(form.trigger_msg_threshold.value || '0', 10),
                trigger_interval_minutes: parseInt(form.trigger_interval_minutes.value || '0', 10),
                cooldown_msg_threshold: parseInt(form.cooldown_msg_threshold.value || '0', 10),
                cooldown_minutes: parseInt(form.cooldown_minutes.value || '0', 10)
            };

            let saved;
            if (!judgeId) {
                data.name = form.name.value;
                saved = await API.judges.create(data);
            } else {
                saved = await API.judges.update(judgeId, data);
            }

            UI.showSuccess(saved?.applied === false ? '已保存；助手启动或重新加载后生效' : '已保存；下一次请求采用新配置');
            const modal = bootstrap.Modal.getInstance(document.getElementById('configModal'));
            if (modal) modal.hide();
            this.loadRoles();
        } catch (e) {
            UI.showError('接话判断操作失败：' + e.message);
        } finally {
            if (saveButton) saveButton.disabled = false;
        }
    },

    async deleteJudge(judgeId) {
        if (!await UI.confirm('确定删除此接话判断吗？该操作无法撤销。', {
            title: '删除接话判断',
            confirmText: '删除',
            variant: 'danger'
        })) return;
        try {
            await API.judges.delete(judgeId);
            this.loadRoles();
        } catch (e) {
            UI.showError('删除接话判断 失败：' + e.message);
        }
    },

    // Logs
    currentLogType: 'app', // Track current log type

    setupLogControls() {
        if (this.logControlsReady) return;
        const debouncedLoad = UI.debounce(() => this.loadLogs(), 350);
        const debouncedSearch = UI.debounce(() => this.applyLogSearch(), 160);
        const pluginFilter = document.getElementById('logPluginFilter');
        const searchInput = document.getElementById('logSearchInput');
        const linesSelect = document.getElementById('logLinesSelect');
        const logContent = document.getElementById('logContent');

        if (pluginFilter) pluginFilter.addEventListener('change', debouncedLoad);
        if (linesSelect) linesSelect.addEventListener('change', debouncedLoad);
        if (searchInput) {
            searchInput.addEventListener('input', debouncedSearch);
            searchInput.addEventListener('keydown', (event) => {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    this.navigateLogSearch(event.shiftKey ? -1 : 1);
                } else if (event.key === 'Escape' && searchInput.value) {
                    searchInput.value = '';
                    this.applyLogSearch({ focus: false });
                }
            });
        }
        if (logContent) {
            let scrollFrame = null;
            logContent.addEventListener('scroll', () => {
                if (scrollFrame !== null) return;
                scrollFrame = requestAnimationFrame(() => {
                    scrollFrame = null;
                    this.handleLogViewportScroll();
                });
            }, { passive: true });
        }
        this.logControlsReady = true;
    },

    async loadLogs(logType, isInitialLoad = false) {
        // If no logType specified, use current or default to 'app'
        if (!logType) {
            logType = this.currentLogType || 'app';
        }

        const logTypeChanged = logType !== this.currentLogType;
        // Store the current log type
        this.currentLogType = logType;

        // Get filter values
        const pluginFilter = document.getElementById('logPluginFilter');
        const searchInput = document.getElementById('logSearchInput');
        const linesSelect = document.getElementById('logLinesSelect');

        let plugin = null;
        let lines = 500;
        if (linesSelect) lines = parseInt(linesSelect.value) || 500;

        if (pluginFilter) {
            // Disable plugin filter for non-app logs
            if (logType !== 'app') {
                pluginFilter.disabled = true;
                pluginFilter.value = "";
            } else {
                pluginFilter.disabled = false;
                plugin = pluginFilter.value;

                // Populate if empty (and we are on app log)
                if (pluginFilter.options.length <= 1) {
                    try {
                        const pluginsData = await API.plugins.getAll();
                        if (pluginsData.plugins) {
                            // Clear existing (keep first)
                            while (pluginFilter.options.length > 1) {
                                pluginFilter.remove(1);
                            }
                            // Add new
                            const pluginsList = Array.isArray(pluginsData.plugins) ? pluginsData.plugins : Object.values(pluginsData.plugins);
                            pluginsList.sort((a, b) => a.name.localeCompare(b.name));

                            pluginsList.forEach(p => {
                                const option = document.createElement('option');
                                option.value = p.name;
                                option.textContent = p.name;
                                pluginFilter.appendChild(option);
                            });
                            // Restore value if it was set
                            if (plugin) pluginFilter.value = plugin;
                        }
                    } catch (e) {
                        console.error("Failed to populate plugin filter", e);
                    }
                }
            }
        }

        // Update status bar
        const statusInfo = document.getElementById('logStatusInfo');
        if (statusInfo) statusInfo.textContent = '加载中…';

        try {
            if (this.logAbortController) this.logAbortController.abort();
            const controller = new AbortController();
            this.logAbortController = controller;
            // Keyword finding is browser-side so the surrounding log lines remain visible.
            const data = await API.system.getLogs(logType, lines, null, plugin, { signal: controller.signal });
            if (this.logAbortController !== controller) return;
            this.logAbortController = null;
            if (data.content !== undefined) {
                this.currentLogContent = data.content || '';
                const activeSearch = String(searchInput?.value || '').trim();
                const searchChanged = activeSearch !== this.currentLogSearchQuery;
                await UI.renderLogs(this.currentLogContent, activeSearch);

                // Update active button state
                document.querySelectorAll('.logs-type-btn').forEach(btn => {
                    btn.classList.remove('active');
                });
                const activeBtn = document.querySelector(`.logs-type-btn[data-log-type="${logType}"]`);
                if (activeBtn) {
                    activeBtn.classList.add('active');
                }

                // Update status bar
                const lineCount = data.content ? data.content.split('\n').filter(l => l.trim()).length : 0;
                const totalInfo = data.total_lines ? ` / 共 ${data.total_lines} 行` : '';
                this.currentLogStatusBase = `${logType} · ${lineCount} 行${totalInfo}`;
                if (plugin) this.currentLogStatusBase += ` · 插件：${plugin}`;
                this.syncLogSearchMatches(activeSearch, {
                    resetIndex: searchChanged || logTypeChanged,
                    focus: Boolean(activeSearch) && (searchChanged || logTypeChanged || isInitialLoad),
                });

                // Show latest logs by default; keep following on subsequent refreshes when enabled.
                if (!activeSearch && (this.logFollowEnabled || isInitialLoad || logTypeChanged)) {
                    if (isInitialLoad || logTypeChanged) this.setLogFollowEnabled(true);
                    this.scrollLogToBottom(isInitialLoad);
                } else {
                    this.updateLogJumpLatest();
                }
            }
        } catch (e) {
            if (e.name === 'AbortError') return;
            await UI.renderLogs('加载日志失败：' + e.message, null);
            if (statusInfo) statusInfo.textContent = '加载失败';
        }
    },

    toggleLogFollow() {
        this.setLogFollowEnabled(!this.logFollowEnabled);
        if (this.logFollowEnabled) this.scrollLogToBottom();
    },

    setLogFollowEnabled(enabled) {
        this.logFollowEnabled = Boolean(enabled);
        const btn = document.getElementById('logFollowBtn');
        if (btn) {
            btn.classList.toggle('active', this.logFollowEnabled);
            btn.setAttribute('aria-pressed', String(this.logFollowEnabled));
            btn.title = this.logFollowEnabled ? '正在跟踪最新日志' : '自动跟踪最新日志';
            const icon = btn.querySelector('i');
            icon?.classList.toggle('bi-arrow-down-circle-fill', this.logFollowEnabled);
            icon?.classList.toggle('bi-arrow-down-circle', !this.logFollowEnabled);
            const label = btn.querySelector('.log-follow-label');
            if (label) label.textContent = this.logFollowEnabled ? '跟随中' : '跟随';
        }
    },

    isLogViewportAtBottom() {
        const container = document.getElementById('logContent');
        if (!container) return true;
        return container.scrollHeight - container.scrollTop - container.clientHeight <= 24;
    },

    updateLogJumpLatest() {
        const button = document.getElementById('logJumpLatest');
        if (!button) return;
        button.classList.toggle('d-none', this.isLogViewportAtBottom());
    },

    handleLogViewportScroll() {
        if (this.logProgrammaticScroll) return;
        const atBottom = this.isLogViewportAtBottom();
        if (!atBottom && this.logFollowEnabled) this.setLogFollowEnabled(false);
        this.updateLogJumpLatest();
    },

    resumeLogFollow() {
        this.setLogFollowEnabled(true);
        this.scrollLogToBottom();
    },

    async applyLogSearch({ focus = true } = {}) {
        const input = document.getElementById('logSearchInput');
        const query = String(input?.value || '').trim();
        const queryChanged = query !== this.currentLogSearchQuery;
        if (query) this.setLogFollowEnabled(false);
        await UI.renderLogs(this.currentLogContent, query);
        this.syncLogSearchMatches(query, {
            resetIndex: queryChanged,
            focus: focus && Boolean(query),
        });
    },

    syncLogSearchMatches(query, { resetIndex = false, focus = false } = {}) {
        const container = document.getElementById('logContent');
        this.currentLogSearchQuery = String(query || '').trim();
        this.currentLogSearchMatches = container
            ? Array.from(container.querySelectorAll('mark.log-search-match'))
            : [];

        if (!this.currentLogSearchQuery || this.currentLogSearchMatches.length === 0) {
            this.currentLogSearchIndex = -1;
        } else if (resetIndex || this.currentLogSearchIndex < 0) {
            this.currentLogSearchIndex = 0;
        } else {
            this.currentLogSearchIndex = Math.min(
                this.currentLogSearchIndex,
                this.currentLogSearchMatches.length - 1
            );
        }

        this.updateLogSearchControls();
        if (focus && this.currentLogSearchIndex >= 0) this.focusCurrentLogSearchMatch();
    },

    updateLogSearchControls() {
        const total = this.currentLogSearchMatches.length;
        const current = this.currentLogSearchIndex >= 0 ? this.currentLogSearchIndex + 1 : 0;
        this.currentLogSearchMatches.forEach((match, index) => {
            const active = index === this.currentLogSearchIndex;
            match.classList.toggle('is-current', active);
            if (active) match.setAttribute('aria-current', 'true');
            else match.removeAttribute('aria-current');
        });

        const position = document.getElementById('logSearchPosition');
        if (position) position.textContent = `${current}/${total}`;
        for (const id of ['logSearchPrevious', 'logSearchNext']) {
            const button = document.getElementById(id);
            if (button) button.disabled = total === 0;
        }

        const statusInfo = document.getElementById('logStatusInfo');
        if (statusInfo) {
            statusInfo.textContent = this.currentLogSearchQuery
                ? `${this.currentLogStatusBase} · 查找：“${this.currentLogSearchQuery}” · ${total} 处`
                : this.currentLogStatusBase;
        }
    },

    async navigateLogSearch(direction = 1) {
        const input = document.getElementById('logSearchInput');
        const query = String(input?.value || '').trim();
        if (!query) {
            input?.focus();
            return;
        }
        if (query !== this.currentLogSearchQuery) {
            await this.applyLogSearch();
            return;
        }
        const total = this.currentLogSearchMatches.length;
        if (!total) return;
        this.currentLogSearchIndex = (
            this.currentLogSearchIndex + (direction < 0 ? -1 : 1) + total
        ) % total;
        this.updateLogSearchControls();
        this.focusCurrentLogSearchMatch();
    },

    focusCurrentLogSearchMatch() {
        const container = document.getElementById('logContent');
        const match = this.currentLogSearchMatches[this.currentLogSearchIndex];
        const line = match?.closest('.log-line');
        if (!container || !line) return;
        const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        const lineRect = line.getBoundingClientRect();
        const containerRect = container.getBoundingClientRect();
        const targetTop = container.scrollTop
            + lineRect.top - containerRect.top
            - Math.max(0, (container.clientHeight - lineRect.height) / 2);
        container.scrollTo({ top: Math.max(0, targetTop), behavior: reducedMotion ? 'auto' : 'smooth' });
    },

    scrollLogToBottom(resetViewport = false) {
        const el = document.getElementById('logContent');
        if (!el) return;

        if (resetViewport) {
            const mainContent = document.querySelector('.main-content');
            if (mainContent) mainContent.scrollTop = 0;
        }
        this.logProgrammaticScroll = true;
        requestAnimationFrame(() => {
            el.scrollTop = el.scrollHeight;
            requestAnimationFrame(() => {
                el.scrollTop = el.scrollHeight;
                this.logProgrammaticScroll = false;
                this.updateLogJumpLatest();
            });
        });
    },

    // Settings
    async loadSettings() {
        try {
            const settings = await API.settings.getConsole();
            UI.renderSystemSettings(settings);
            const activeGroup = document.getElementById('settings')?.dataset.activeSystemGroup;
            if (activeGroup === 'notifications') await window.EmailNotifications?.load();
            if (activeGroup === 'operations') await window.SystemOperations?.loadRuntime();
            if (activeGroup === 'tools') await window.SystemTools?.load();
            if (activeGroup === 'backups') await window.SystemOperations?.loadBackups();
        } catch (e) {
            UI.showError('加载设置失败：' + e.message);
        }
    },

    async saveSettings() {
        try {
            const inputs = document.querySelectorAll('.system-setting-input');
            const values = {};

            for (const input of inputs) {
                if (input.disabled || input.readOnly) continue;
                const key = input.name;
                const value = input.value;
                const original = input.dataset.original;
                const sensitive = input.dataset.sensitive === 'true';
                if (sensitive && value === '') continue;
                if (value !== original) values[key] = value;
            }

            if (Object.keys(values).length === 0) {
                UI.showInfo('未检测到更改');
                return;
            }

            await API.settings.updateConsole(values);
            UI.showSuccess('系统设置已原子保存');
            await this.loadSettings();
        } catch (e) {
            UI.showError('保存设置失败：' + e.message);
        }
    },

    async reloadSettingsFromEnv() {
        if (!await UI.confirm('确定从 .env 文件重新加载设置吗？', {
            title: '重新加载设置',
            confirmText: '重新加载'
        })) return;
        try {
            await API.settings.reloadFromEnv();
            UI.showSuccess('设置已重新加载');
            this.loadSettings();
        } catch (e) {
            UI.showError(e.message);
        }
    }
};

window.App = App;
document.addEventListener('DOMContentLoaded', () => App.init());
