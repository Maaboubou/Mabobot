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
    lastLiveCodexStatus: null,
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
                    if (this.logAbortController) this.logAbortController.abort();
                } else {
                    this.startAutoRefresh();
                    this.refreshCurrentTab();
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
        if (['settings', 'users', 'roles', 'llm'].includes(this.currentTab)) return;

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

        try {
            switch (tabName) {
                case 'dashboard':
                    await this.refreshDashboard();
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

    async refreshDashboard() {
        // Each panel owns its failure state. One slow integration must not
        // prevent otherwise healthy dashboard sections from refreshing.
        const requests = [
                API.request('/api/dashboard/stats'),
                API.request('/api/dashboard/recent-activities?limit=14'),
                API.request('/api/dashboard/top-users?limit=5'),
                API.system.getStatus(),
                API.wechat.getStatus(),
                API.wechat.getMyInfo(),
                API.plugins.getStats(),
                API.request('/api/dashboard/codex-status'),
                API.system.getHealthDetails()
        ];
        const labels = ['统计', '动态', '聊天', '资源', '微信', '微信资料', '插件', 'Codex', '运行状态'];
        const settled = await Promise.allSettled(requests);
        const value = (index, fallback) => settled[index].status === 'fulfilled' ? settled[index].value : fallback;
        const failed = settled
            .map((item, index) => item.status === 'rejected' ? labels[index] : null)
            .filter(Boolean);

        const dashStats = value(0, null);
        const recentActivities = value(1, null);
        const topUsers = value(2, null);
        const systemStatus = value(3, null);
        const wxStatus = value(4, null);
        const wxInfo = value(5, {});
        const llmStats = value(6, null);
        const codexStatus = value(7, {
            status: 'error',
            quota_message: settled[7].reason?.message || 'Codex 状态暂不可用'
        });
        const runtimeHealth = value(8, null);

        try {
            if (dashStats) this.renderDashboardStats(dashStats);
            else this.markDashboardPanelStale(['statTodayMessages', 'statAiReplies', 'statTokenUsage']);
            if (recentActivities) this.renderRecentActivities(recentActivities.activities);
            else this.markDashboardPanelStale(['recentActivities']);
            if (topUsers) this.renderTopUsers(topUsers.users);
            else this.markDashboardPanelStale(['topUsers']);
            if (systemStatus) this.renderSystemResources(systemStatus);
            else this.markDashboardPanelStale(['systemResources']);
            if (wxStatus) this.renderWeChatStatus(wxStatus, wxInfo);
            else this.markDashboardPanelStale(['dashboardWechatStatus']);
            if (llmStats) this.renderLLMStats(llmStats.stats || llmStats);
            else this.markDashboardPanelStale(['dashboardPluginHealth', 'dashboardModelCalls', 'dashboardErrors']);
            this.renderCodexStatus(codexStatus);
            if (systemStatus && llmStats) this.renderDashboardSummary(systemStatus, llmStats.stats || llmStats);
            if (runtimeHealth) this.renderRuntimeHealth(runtimeHealth);
            else this.markDashboardPanelStale(['dashboardSystemHealth', 'dashboardActiveOperations']);

            const now = new Date();
            const updated = document.getElementById('lastUpdateTime');
            if (updated) {
                updated.textContent = failed.length
                    ? `更新 ${now.toLocaleTimeString('zh-CN')} · ${failed.length} 项暂不可用`
                    : `更新 ${now.toLocaleTimeString('zh-CN')}`;
                updated.title = failed.length ? `暂不可用：${failed.join('、')}` : '全部数据已刷新';
            }
        } catch (e) {
            console.error('Failed to render dashboard:', e);
        }
    },

    markDashboardPanelStale(elementIds) {
        elementIds.forEach(id => {
            const element = document.getElementById(id);
            if (!element) return;
            element.dataset.stale = 'true';
            element.title = '本区域本次刷新失败，当前内容可能不是最新状态';
            if (!element.textContent.trim() || element.textContent.trim() === '-') {
                element.textContent = '暂不可用';
            }
        });
    },

    openDashboardErrors() {
        const path = '/ai/calls';
        if (UI.normalizePath(window.location.pathname) !== path) {
            window.history.pushState({ tab: 'llm', section: 'llm-history' }, '', path);
        }
        UI.switchTab('llm', { history: false });
    },

    async refreshCodexUsage() {
        const button = document.getElementById('refreshCodexUsageBtn');
        const container = document.getElementById('codexStatusOutput');
        try {
            if (button) {
                button.disabled = true;
                button.innerHTML = '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span>';
            }
            if (container) {
                container.innerHTML = `
                    <div class="text-center text-muted py-3">
                        <div class="spinner-border spinner-border-sm me-2" aria-hidden="true"></div>
                        正在刷新 Codex 用量…
                    </div>
                `;
            }

            const data = await API.request('/api/dashboard/codex-status/refresh', { method: 'POST' });
            this.renderCodexStatus(data);
        } catch (e) {
            console.error('Failed to refresh Codex usage:', e);
            this.renderCodexStatus({
                status: 'error',
                logged_in: false,
                quota_available: false,
                quota_message: e.message || '刷新 Codex 用量失败',
                updated_at: new Date().toISOString(),
            });
        } finally {
            if (button) {
                button.disabled = false;
                button.innerHTML = '<i class="bi bi-arrow-clockwise"></i>';
            }
        }
    },

    renderDashboardStats(stats) {
        document.getElementById('statTodayMessages').textContent = Number(stats?.today_messages || 0).toLocaleString();
        document.getElementById('statAiReplies').textContent = Number(stats?.today_ai_replies || 0).toLocaleString();
        document.getElementById('statActiveUsers').textContent = Number(stats?.active_users || 0).toLocaleString();

        // Format token usage
        const tokens = Number(stats?.token_usage || 0);
        let tokenDisplay;
        if (tokens >= 1000000) {
            tokenDisplay = (tokens / 1000000).toFixed(1) + 'M';
        } else if (tokens >= 1000) {
            tokenDisplay = (tokens / 1000).toFixed(1) + 'K';
        } else {
            tokenDisplay = tokens.toString();
        }
        document.getElementById('statTokenUsage').textContent = tokenDisplay;
    },

    renderRecentActivities(activities) {
        const container = document.getElementById('recentActivities');
        if (!activities || activities.length === 0) {
            container.innerHTML = '<div class="p-4 text-center text-muted">暂无活动记录</div>';
            return;
        }

        const html = activities.map(activity => {
            const icon = activity.is_bot ?
                '<i class="bi bi-robot text-success"></i>' :
                '<i class="bi bi-person text-primary"></i>';
            const time = String(activity.time || '').split(' ')[1] || '';

            return `
                <div class="dashboard-activity-row">
                    <div class="dashboard-activity-icon ${activity.is_bot ? 'bot' : 'person'}">${icon}</div>
                    <div class="flex-grow-1 min-w-0">
                        <div class="dashboard-activity-title">
                            <strong class="text-truncate">${this.escapeHtml(activity.chat_name || '未知聊天')}</strong>
                            <small>${this.escapeHtml(time)}</small>
                        </div>
                        <div class="dashboard-activity-preview text-truncate">${this.escapeHtml(activity.sender || '未知')}: ${this.escapeHtml(activity.preview || '')}</div>
                    </div>
                </div>
            `;
        }).join('');

        container.innerHTML = html;
    },

    renderTopUsers(users) {
        const container = document.getElementById('topUsers');
        if (!users || users.length === 0) {
            container.innerHTML = '<div class="text-center text-muted small">暂无数据</div>';
            return;
        }

        const html = users.map((user, index) => `
            <div class="dashboard-chat-rank">
                <span class="dashboard-rank-index">${index + 1}</span>
                <div class="min-w-0">
                    <strong class="text-truncate">${this.escapeHtml(user.chat_name || '未知聊天')}</strong>
                </div>
                <span class="dashboard-rank-count">${Number(user.message_count || 0).toLocaleString()}</span>
            </div>
        `).join('');

        container.innerHTML = html;
    },

    renderSystemResources(status) {
        const container = document.getElementById('systemResources');
        if (!container) return;

        const cpu = Number(status?.cpu_percent || 0);
        const memory = Number(status?.memory_percent || 0);
        const disk = Number(status?.disk_percent || 0);
        const systemUptime = status?.system_uptime || '未知';
        const appUptime = status?.uptime || '未知';
        const temperature = status?.temperature;
        const temperatureSensors = Array.isArray(temperature?.sensors)
            ? temperature.sensors.filter(sensor => Number.isFinite(Number(sensor?.celsius)))
            : [];

        const resourceRow = (label, value) => {
            const tone = value >= 85 ? 'danger' : value >= 65 ? 'warning' : 'success';
            const safeValue = Math.max(0, Math.min(100, value));
            return `
                <div class="dashboard-resource-item ${tone}">
                    <div><span>${label}</span><strong>${value.toFixed(1)}%</strong></div>
                    <span class="dashboard-resource-track"><i style="width: ${safeValue}%"></i></span>
                </div>
            `;
        };

        const temperatureHtml = temperatureSensors.length > 0
            ? `<div class="dashboard-temperature-row">
                    <span>温度</span>
                    <div>${temperatureSensors.slice(0, 2).map(sensor => {
                        const value = Number(sensor.celsius);
                        const tone = value >= 90 ? 'danger' : value >= 75 ? 'warning' : 'success';
                        const details = [sensor.sensor_name, sensor.source].filter(Boolean).join(' · ');
                        return `<span class="dashboard-temperature ${tone}" title="${this.escapeHtml(details)}">${this.escapeHtml(sensor.label || '温度')} ${value.toFixed(1)}°C</span>`;
                    }).join('')}</div>
                </div>`
            : '';

        this.systemResourceSnapshot = { cpu, memory, disk, systemUptime, appUptime };
        container.innerHTML = `
            <div class="dashboard-resource-grid">
                ${resourceRow('CPU', cpu)}
                ${resourceRow('内存', memory)}
                ${resourceRow('磁盘', disk)}
            </div>
            ${temperatureHtml}
        `;
    },

    renderWeChatStatus(wxStatus, wxInfo) {
        const container = document.getElementById('dashboardWechatStatus');
        if (!container) return;
        const isOnline = wxStatus?.status === 'connected' || wxStatus?.running === true;
        const botName = wxInfo?.display_name || wxStatus?.stats?.bot_name || '未知';
        container.innerHTML = `
            <span class="dashboard-inline-state ${isOnline ? 'online' : 'offline'}" title="${this.escapeHtml(botName)}">
                <i class="bi bi-wechat"></i>${isOnline ? '在线' : '离线'}
            </span>
        `;
    },

    renderLLMStats(stats) {
        const totalCalls = Number(stats?.total_calls || 0);
        const errorCount = Number(stats?.error_count || 0);
        const avgResponseTime = Number(stats?.avg_response_time || 0);

        // Format response time
        let responseTimeDisplay;
        if (avgResponseTime >= 1) {
            responseTimeDisplay = avgResponseTime.toFixed(1) + ' 秒';
        } else if (avgResponseTime > 0) {
            responseTimeDisplay = (avgResponseTime * 1000).toFixed(0) + ' 毫秒';
        } else {
            responseTimeDisplay = '-';
        }

        const callsElement = document.getElementById('dashboardModelCalls');
        const errorsElement = document.getElementById('dashboardErrors');
        const callsMetaElement = document.getElementById('dashboardCallsMeta');
        if (callsElement) callsElement.textContent = totalCalls.toLocaleString();
        if (errorsElement) {
            errorsElement.textContent = errorCount.toLocaleString();
            const errorLink = errorsElement.closest('.dashboard-kpi');
            errorLink?.classList.toggle('has-errors', errorCount > 0);
            if (errorLink) {
                errorLink.title = errorCount > 0
                    ? `查看 ${errorCount.toLocaleString()} 次调用错误`
                    : '查看调用诊断';
            }
        }
        if (callsMetaElement) callsMetaElement.textContent = `平均 ${responseTimeDisplay}`;
    },

    renderDashboardSummary(systemStatus, stats) {
        const enabledPlugins = Number(stats?.enabled_plugins || 0);
        const loadedPlugins = Number(stats?.loaded_plugins || 0);
        const pluginHealth = document.getElementById('dashboardPluginHealth');
        const appUptime = document.getElementById('dashboardUptime');
        const systemUptime = document.getElementById('dashboardSystemUptime');
        if (pluginHealth) pluginHealth.textContent = enabledPlugins ? `${loadedPlugins}/${enabledPlugins} 运行` : `${loadedPlugins} 运行`;
        if (appUptime) appUptime.textContent = this.systemResourceSnapshot?.appUptime || systemStatus?.uptime || '未知';
        if (systemUptime) systemUptime.textContent = this.systemResourceSnapshot?.systemUptime || systemStatus?.system_uptime || '未知';
    },

    renderRuntimeHealth(health) {
        const healthElement = document.getElementById('dashboardSystemHealth');
        const operationsElement = document.getElementById('dashboardActiveOperations');
        const checks = health?.checks || {};
        const names = { database: '数据库', event_bus: '事件总线', plugin_manager: '插件', wechat: '微信' };
        const failedChecks = Object.entries(checks)
            .filter(([, ready]) => !ready)
            .map(([name]) => names[name] || name);
        const status = health?.status || (health?.ready ? 'ready' : 'not_ready');
        const statusText = status === 'ready' ? '正常' : status === 'degraded' ? '可用' : '异常';
        const tone = status === 'ready' ? 'online' : status === 'degraded' ? 'warning' : 'offline';
        if (healthElement) {
            healthElement.innerHTML = `<span class="dashboard-inline-state ${tone}"><i class="bi bi-circle-fill"></i>${statusText}</span>`;
            healthElement.title = failedChecks.length ? `未就绪：${failedChecks.join('、')}` : '核心组件运行正常';
            healthElement.removeAttribute('data-stale');
        }
        if (operationsElement) {
            const activeCount = Number(health?.operations?.active_count || 0);
            operationsElement.textContent = activeCount ? `${activeCount} 项` : '空闲';
            operationsElement.classList.toggle('text-primary', activeCount > 0);
            operationsElement.title = activeCount ? `${activeCount} 个平台托管任务正在执行` : '当前没有平台托管任务';
            operationsElement.removeAttribute('data-stale');
        }
    },

    localizeCodexQuotaMessage(message) {
        const text = String(message || '');
        const exactLabels = {
            'Codex runtime did not return account rate limits': 'Codex 运行时未返回账户限额',
            'Latest rollout file does not contain rate_limits yet': '最新 rollout 文件尚未包含 rate_limits',
            'Read from latest Codex rollout rate_limits': '已从最新 Codex rollout 文件读取 rate_limits',
            'Live refresh failed; showing last successful live usage': '实时刷新失败，正在显示最近一次成功获取的实时用量',
            'Live refresh failed; showing cached rollout data': '实时刷新失败，正在显示缓存的 rollout 数据',
            'Failed to fetch': '网络请求失败'
        };
        if (exactLabels[text]) return exactLabels[text];
        if (text.startsWith('No rollout files found under ')) {
            return `未在以下目录找到 rollout 文件：${text.slice('No rollout files found under '.length)}`;
        }
        if (text.startsWith('Failed to read rollout file: ')) {
            return `读取 rollout 文件失败：${text.slice('Failed to read rollout file: '.length)}`;
        }
        return text;
    },

    renderCodexStatus(data) {
        const container = document.getElementById('codexStatusOutput');
        if (!container) return;

        // A dashboard poll can finish after a manual refresh. Keep the newest
        // successful app-server result instead of letting an old rollout win.
        const hasLiveUsage = data?.usage_source === 'app_server' && !!data?.quota_available;
        if (hasLiveUsage) {
            const incomingTime = Date.parse(data.updated_at || '') || 0;
            const savedTime = Date.parse(this.lastLiveCodexStatus?.updated_at || '') || 0;
            if (!this.lastLiveCodexStatus || incomingTime >= savedTime) {
                this.lastLiveCodexStatus = data;
            } else {
                data = this.lastLiveCodexStatus;
            }
        } else if (this.lastLiveCodexStatus) {
            data = this.lastLiveCodexStatus;
        }

        const loggedIn = !!data?.logged_in;
        const statusClass = loggedIn ? 'online' : 'offline';
        const statusText = data?.usage_source === 'app_server'
            ? (data?.served_from_snapshot ? '最近实时用量' : '实时用量')
            : (loggedIn ? '缓存用量' : '尚未刷新');
        const model = data?.model || 'gpt-5.5';
        const version = data?.version || '-';
        const authMode = data?.auth_mode || (loggedIn ? 'chatgpt' : '-');
        const planType = data?.plan_type ? ` / ${data.plan_type}` : '';
        const updatedAt = data?.updated_at ? this.formatDashboardTime(data.updated_at) : '';
        const quotaMessage = data?.quota_available
            ? (data?.quota || 'Codex 已返回用量信息')
            : this.localizeCodexQuotaMessage(data?.quota_message || '点击刷新以读取当前 Codex 账户限额。');
        const primaryLimit = this.renderCodexLimit(data?.rate_limits?.primary, '主要限额');
        const secondaryLimit = this.renderCodexLimit(data?.rate_limits?.secondary, '次要限额');

        container.innerHTML = `
            <div class="dashboard-codex-meta">
                <span class="dashboard-inline-state ${statusClass}"><i class="bi bi-circle-fill"></i>${statusText}</span>
                ${updatedAt ? `<small>${this.escapeHtml(updatedAt)}</small>` : ''}
            </div>
            <div class="dashboard-codex-identity" title="Codex ${this.escapeHtml(version)}">
                <strong>${this.escapeHtml(model)}</strong>
                <span>${this.escapeHtml(authMode + planType)}</span>
            </div>
            ${primaryLimit || secondaryLimit ? `
                <div class="dashboard-codex-limits">
                    ${primaryLimit}
                    ${secondaryLimit}
                </div>
            ` : ''}
            ${!data?.quota_available ? `
                <div class="dashboard-codex-empty">
                    <i class="bi bi-info-circle"></i><span>${this.escapeHtml(quotaMessage)}</span>
                </div>
            ` : ''}
        `;
    },

    renderCodexLimit(limit, label) {
        if (!limit || typeof limit.used_percent !== 'number') return '';

        const used = Math.max(0, Math.min(100, limit.used_percent));
        const rawRemaining = typeof limit.remaining_percent === 'number'
            ? limit.remaining_percent
            : 100 - used;
        const remaining = Math.max(0, Math.min(100, rawRemaining));
        const tone = remaining <= 10 ? 'danger' : remaining <= 30 ? 'warning' : 'success';
        const resetTime = limit.resets_at ? new Date(limit.resets_at * 1000) : null;
        const resetText = resetTime && !Number.isNaN(resetTime.getTime())
            ? resetTime.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
            : '-';
        const windowText = limit.window_minutes ? this.formatMinutes(limit.window_minutes) : '-';

        return `
            <article class="dashboard-codex-limit ${tone}">
                <div>
                    <span>${this.escapeHtml(label)}</span>
                    <strong>${remaining.toFixed(remaining % 1 === 0 ? 0 : 1)}%</strong>
                </div>
                <span class="dashboard-resource-track"><i style="width: ${remaining}%"></i></span>
                <small>${this.escapeHtml(windowText)} 窗口 · ${this.escapeHtml(resetText)} 重置 · 已用 ${used.toFixed(used % 1 === 0 ? 0 : 1)}%</small>
            </article>
        `;
    },

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
                        ${shouldReply ? '✅ 需要回复' : '⏸️ 无需回复'}
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
                ${judgeName ? `<div class="small text-muted mb-2"><strong>Judge：</strong>${this.escapeHtml(judgeName)}</div>` : ''}
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
                    暂无 Judge 历史记录
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
                                ${itemShouldReply ? '需要回复' : '无需回复'}
                            </span>
                            ${itemTime ? `<small class="text-muted">${this.escapeHtml(itemTime)}</small>` : ''}
                        </div>
                        <div class="small text-muted mb-2">
                            ${itemJudgeName ? `<strong>Judge：</strong>${this.escapeHtml(itemJudgeName)}` : ''}
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

    renderMemoryTraceSummary(trace) {
        const enabled = trace.enabled !== false;
        const events = Array.isArray(trace.events) ? trace.events.length : 0;
        const people = Array.isArray(trace.people) ? trace.people.length : 0;
        const hasStage = Boolean(trace.stage && trace.stage.included);
        const tokens = Number(trace.tokens || 0);
        const budget = Number(trace.token_budget || 0);
        const latency = Number(trace.retrieval_ms || 0);

        return `
            <div class="border rounded-3 bg-body-tertiary p-2 mb-3">
                <div class="d-flex justify-content-between align-items-center gap-2 mb-2">
                    <span class="small fw-semibold">
                        <i class="bi bi-database-check text-primary me-1"></i>
                        本轮已注入记忆
                    </span>
                    <span class="badge ${enabled ? 'text-bg-success' : 'text-bg-secondary'}">
                        ${enabled ? `${tokens.toLocaleString()} / ${budget.toLocaleString()} tokens` : '已关闭'}
                    </span>
                </div>
                <div class="d-flex flex-wrap gap-1">
                    <span class="badge text-bg-light border">阶段 ${hasStage ? '1' : '0'}</span>
                    <span class="badge text-bg-light border">事件 ${events}</span>
                    <span class="badge text-bg-light border">人物 ${people}</span>
                    <span class="badge text-bg-light border">检索 ${latency.toLocaleString()} 毫秒</span>
                    <span class="badge text-bg-light border">
                        向量 ${trace.vector_ready ? '就绪' : '未使用'}
                    </span>
                </div>
            </div>
        `;
    },

    renderMemoryTrace(trace, namespace = '') {
        if (!trace) {
            return '<div class="text-muted text-center py-4">此调用没有记忆审计记录。</div>';
        }
        if (trace.enabled === false) {
            return `
                <div class="alert alert-secondary mb-0">
                    <i class="bi bi-database-x me-1"></i>本轮记忆功能已关闭，没有向提示词注入记忆。
                </div>
            `;
        }

        const events = Array.isArray(trace.events) ? trace.events : [];
        const people = Array.isArray(trace.people) ? trace.people : [];
        const droppedEvents = Array.isArray(trace.dropped_events) ? trace.dropped_events : [];
        const droppedPeople = Array.isArray(trace.dropped_people) ? trace.dropped_people : [];
        const stage = trace.stage || {};
        const safeTraceId = `${trace.trace_id || 'memory'}-${namespace || 'record'}`
            .replace(/[^a-zA-Z0-9_-]/g, '');

        let html = `
            <div class="alert alert-info py-2 small">
                <i class="bi bi-info-circle me-1"></i>
                这里展示的是<strong>实际注入本轮提示词</strong>的记忆；它不等同于模型内部一定采用了这些内容。
            </div>
            ${this.renderMemoryTraceSummary(trace)}
        `;

        html += `
            <section class="mb-4">
                <h6 class="d-flex align-items-center gap-2">
                    <i class="bi bi-layers text-primary"></i>阶段记忆
                    <span class="badge ${stage.included ? 'text-bg-success' : 'text-bg-secondary'}">
                        ${stage.included ? '已注入' : '未注入'}
                    </span>
                </h6>
        `;
        if (stage.included) {
            html += `
                <div class="border rounded-3 p-3 bg-body-tertiary">
                    <div class="d-flex flex-wrap gap-2 mb-2 small text-muted">
                        <span>来源事件 #${Number(stage.source_event_id || 0)}</span>
                        ${stage.updated_at ? `<span>更新于 ${this.escapeHtml(stage.updated_at)}</span>` : ''}
                        ${stage.truncated ? '<span class="badge text-bg-warning">按预算截断</span>' : ''}
                    </div>
                    <div style="white-space: pre-wrap;">${this.escapeHtml(stage.text || '')}</div>
                </div>
            `;
        } else {
            html += '<div class="text-muted small">本轮没有阶段记忆注入提示词。</div>';
        }
        html += '</section>';

        html += `
            <section class="mb-4">
                <h6><i class="bi bi-people text-primary me-2"></i>人物记忆 <span class="badge text-bg-light border">${people.length}</span></h6>
        `;
        if (people.length) {
            html += '<div class="vstack gap-2">';
            people.forEach(person => {
                const reasons = Array.isArray(person.selection_reasons) ? person.selection_reasons : [];
                html += `
                    <div class="border rounded-3 p-3">
                        <div class="d-flex justify-content-between gap-2 mb-1">
                            <strong>${this.escapeHtml(person.name || '未知人物')}</strong>
                            <small class="text-muted">来源事件 #${Number(person.source_event_id || 0)}</small>
                        </div>
                        <div class="d-flex flex-wrap gap-1 mb-2">
                            ${reasons.map(reason => `<span class="badge text-bg-light border">${this.escapeHtml(reason)}</span>`).join('')}
                        </div>
                        <div class="small" style="white-space: pre-wrap;">${this.escapeHtml(person.profile_text || '')}</div>
                    </div>
                `;
            });
            html += '</div>';
        } else {
            html += '<div class="text-muted small">本轮没有人物资料注入提示词。</div>';
        }
        html += '</section>';

        html += `
            <section class="mb-4">
                <h6><i class="bi bi-journal-text text-primary me-2"></i>事件记忆 <span class="badge text-bg-light border">${events.length}</span></h6>
        `;
        if (events.length) {
            html += '<div class="vstack gap-2">';
            events.forEach((event, index) => {
                const score = Math.max(0, Math.min(1, Number(event.retrieval_score || 0)));
                const scorePercent = Math.round(score * 100);
                const reasons = Array.isArray(event.match_reasons) ? event.match_reasons : [];
                const participants = Array.isArray(event.participants) ? event.participants : [];
                const keywords = Array.isArray(event.keywords) ? event.keywords : [];
                const breakdown = event.score_breakdown || {};
                const sourceTargetId = `memory-source-${safeTraceId}-${Number(event.id || index)}`;
                const timeRange = [event.start_time, event.end_time].filter(Boolean).join(' ～ ');

                html += `
                    <details class="border rounded-3 overflow-hidden" ${index === 0 ? 'open' : ''}>
                        <summary class="p-3 bg-body-tertiary" style="cursor: pointer;">
                            <div class="d-inline-flex flex-wrap align-items-center gap-2">
                                <strong>#${Number(event.id || 0)} ${this.escapeHtml(event.title || '未命名事件')}</strong>
                                <span class="badge text-bg-primary">相关度 ${scorePercent}%</span>
                                ${event.certainty ? `<span class="badge text-bg-light border">${this.escapeHtml(event.certainty)}</span>` : ''}
                            </div>
                        </summary>
                        <div class="p-3">
                            <div class="progress mb-2" role="progressbar" aria-label="记忆相关度"
                                 aria-valuenow="${scorePercent}" aria-valuemin="0" aria-valuemax="100"
                                 style="height: 5px;">
                                <div class="progress-bar" style="width: ${scorePercent}%"></div>
                            </div>
                            <div class="d-flex flex-wrap gap-1 mb-2">
                                ${reasons.map(reason => `<span class="badge text-bg-info">${this.escapeHtml(reason)}</span>`).join('')}
                            </div>
                            ${timeRange ? `<div class="small text-muted mb-2"><i class="bi bi-clock me-1"></i>${this.escapeHtml(timeRange)}</div>` : ''}
                            <div class="mb-2" style="white-space: pre-wrap;">${this.escapeHtml(event.summary || '')}</div>
                            ${participants.length ? `<div class="small mb-1"><strong>参与者：</strong>${participants.map(value => this.escapeHtml(value)).join('、')}</div>` : ''}
                            ${keywords.length ? `<div class="small mb-1"><strong>关键词：</strong>${keywords.map(value => this.escapeHtml(value)).join('、')}</div>` : ''}
                            ${(event.decisions || []).length ? `<div class="small mb-1"><strong>结论：</strong>${event.decisions.map(value => this.escapeHtml(value)).join('；')}</div>` : ''}
                            ${(event.open_items || []).length ? `<div class="small mb-1"><strong>未完成：</strong>${event.open_items.map(value => this.escapeHtml(value)).join('；')}</div>` : ''}
                            <details class="mt-3">
                                <summary class="small text-muted" style="cursor: pointer;">查看检索得分明细</summary>
                                <div class="d-flex flex-wrap gap-1 mt-2">
                                    ${Object.entries(breakdown).map(([key, value]) => `
                                        <span class="badge text-bg-light border">
                                            ${this.escapeHtml(this.memoryScoreLabel(key))} ${Number(value || 0).toFixed(3)}
                                        </span>
                                    `).join('')}
                                </div>
                            </details>
                            <div class="d-flex flex-wrap gap-2 mt-3">
                                <button class="btn btn-sm btn-outline-primary"
                                        onclick="App.loadMemoryEventSource(${Number(event.id || 0)}, '${sourceTargetId}', this)">
                                    <i class="bi bi-chat-left-text me-1"></i>查看原始消息
                                </button>
                                <details>
                                    <summary class="btn btn-sm btn-outline-secondary">实际注入文本</summary>
                                    <pre class="mt-2 mb-0 p-2 bg-body-tertiary border rounded small"
                                         style="white-space: pre-wrap; max-height: 260px; overflow: auto;">${this.escapeHtml(event.prompt_text || '')}</pre>
                                </details>
                            </div>
                            <div id="${sourceTargetId}" class="mt-3"
                                 data-chat-name="${this.escapeHtml(trace.chat_name || '')}"></div>
                        </div>
                    </details>
                `;
            });
            html += '</div>';
        } else {
            html += '<div class="text-muted small">本轮没有历史事件注入提示词。</div>';
        }
        html += '</section>';

        if (droppedEvents.length || droppedPeople.length) {
            html += `
                <details class="border rounded-3 p-3">
                    <summary class="small fw-semibold" style="cursor: pointer;">
                        因 Token 预算未注入的候选（事件 ${droppedEvents.length}，人物 ${droppedPeople.length}）
                    </summary>
                    <div class="mt-2 small text-muted">
                        ${droppedEvents.map(event => `
                            <div>#${Number(event.id || 0)} ${this.escapeHtml(event.title || '未命名事件')}</div>
                        `).join('')}
                        ${droppedPeople.map(person => `
                            <div>${this.escapeHtml(person.name || '未知人物')}</div>
                        `).join('')}
                    </div>
                </details>
            `;
        }
        return html;
    },

    memoryScoreLabel(key) {
        return {
            semantic: '语义',
            lexical: '文字',
            keyword: '关键词',
            participant: '人物',
            recency: '时效',
            importance: '重要度'
        }[key] || key;
    },

    async loadMemoryEventSource(eventId, targetId, button) {
        const target = document.getElementById(targetId);
        if (!target || target.dataset.loaded === 'true') return;
        const chatName = target.dataset.chatName || '';
        if (!chatName) {
            target.innerHTML = '<div class="alert alert-warning py-2 mb-0">缺少群聊名称，无法读取来源消息。</div>';
            return;
        }

        const originalHtml = button ? button.innerHTML : '';
        if (button) {
            button.disabled = true;
            button.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>读取中';
        }
        target.innerHTML = '<div class="text-muted small">正在读取事件对应的原始群聊消息……</div>';
        try {
            const params = new URLSearchParams({
                chat_name: chatName,
                event_id: String(eventId)
            });
            const response = await API.request(`/api/assistant/roles/memory-event-source?${params.toString()}`);
            const messages = response?.data?.messages || [];
            if (!messages.length) {
                target.innerHTML = `
                    <div class="alert alert-secondary py-2 mb-0 small">
                        没有找到对应的原始消息；聊天日志可能已轮转或清理。
                    </div>
                `;
            } else {
                target.innerHTML = `
                    <div class="border rounded-3 p-2 bg-body-tertiary">
                        <div class="small fw-semibold mb-2">原始消息（${messages.length}）</div>
                        <div class="vstack gap-2" style="max-height: 360px; overflow-y: auto;">
                            ${messages.map(message => `
                                <div class="bg-body border rounded p-2 small">
                                    <div class="d-flex justify-content-between gap-2 text-muted mb-1">
                                        <span>#${Number(message._log_cursor || 0)} · ${this.escapeHtml(message.sender || '未知')}</span>
                                        <span>${this.escapeHtml(message.time || '')}</span>
                                    </div>
                                    <div style="white-space: pre-wrap;">${this.escapeHtml(message.content || '')}</div>
                                </div>
                            `).join('')}
                        </div>
                    </div>
                `;
            }
            target.dataset.loaded = 'true';
        } catch (error) {
            target.innerHTML = `
                <div class="alert alert-danger py-2 mb-0 small">
                    读取原始消息失败：${this.escapeHtml(error.message || String(error))}
                </div>
            `;
        } finally {
            if (button) {
                button.disabled = false;
                button.innerHTML = originalHtml;
            }
        }
    },

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    },

    formatDashboardTime(timestamp) {
        if (!timestamp) return '';
        if (typeof timestamp === 'string' && timestamp.includes(' ')) {
            return timestamp.split(' ')[1] || '';
        }
        const date = new Date(timestamp);
        return Number.isNaN(date.getTime()) ? String(timestamp) : date.toLocaleTimeString('zh-CN');
    },

    formatMinutes(minutes) {
        if (!minutes) return '-';
        if (minutes % 1440 === 0) {
            return `${minutes / 1440}天`;
        } else if (minutes % 60 === 0) {
            return `${minutes / 60}小时`;
        } else {
            return `${minutes}分钟`;
        }
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

    async showCapabilitySettings(name, options = {}) {
        try {
            const [detailResponse, settings] = await Promise.all([
                API.capabilities.getDetail(name),
                API.capabilities.getSettings(name)
            ]);
            const capability = detailResponse.capability || {};
            this.currentCapabilitySettings = settings;
            this.currentCapabilityId = name;

            document.getElementById('configModalTitle').textContent = `配置 · ${capability.display_name || name}`;
            document.getElementById('configModalBody').innerHTML = UI.renderCapabilitySettingsForm(settings, capability);

            const saveBtn = document.getElementById('configModalSaveBtn');
            saveBtn.classList.remove('d-none');
            saveBtn.onclick = () => this.saveCapabilitySettings(name);

            const modalElement = document.getElementById('configModal');
            new bootstrap.Modal(modalElement).show();
            UI.bindCapabilitySettingsControls(modalElement);
            if (options.focusGroup) {
                UI.focusCapabilitySettingsGroup(modalElement, options.focusGroup);
            }
        } catch (e) {
            UI.showError('加载设置失败：' + e.message);
        }
    },

    async saveCapabilitySettings(name) {
        const form = document.getElementById('capabilitySettingsForm');
        if (!form) return;
        if (!form.checkValidity()) {
            form.reportValidity();
            return;
        }

        const values = {};
        form.querySelectorAll('[data-config-key]').forEach(input => {
            if (input.disabled) return;
            const key = input.dataset.configKey;
            const type = input.dataset.configType;
            const sensitive = input.dataset.sensitive === 'true';
            if (sensitive && input.value === '') return;

            if (type === 'boolean') {
                values[key] = input.checked;
            } else if (type === 'integer') {
                values[key] = Number.parseInt(input.value, 10);
            } else if (type === 'number') {
                values[key] = Number.parseFloat(input.value);
            } else if (type === 'array') {
                values[key] = input.value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
            } else {
                values[key] = input.value;
            }
        });

        const saveBtn = document.getElementById('configModalSaveBtn');
        const originalHtml = saveBtn.innerHTML;
        saveBtn.disabled = true;
        saveBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>正在应用';
        try {
            await API.capabilities.updateSettings(name, values);
            const modal = bootstrap.Modal.getInstance(document.getElementById('configModal'));
            if (modal) modal.hide();
            UI.showSuccess('设置已保存并应用');
            await this.loadPlugins(false);
        } catch (e) {
            UI.showError('保存失败：' + e.message);
        } finally {
            saveBtn.disabled = false;
            saveBtn.innerHTML = originalHtml;
        }
    },

    // Users & Listeners
    async loadUsers() {
        try {
            const [usersData, listenersData, profilesData] = await Promise.all([
                API.users.getAll(),
                API.wechat.getListeners(),
                API.codexProfiles.list().catch(() => ({ profiles: [], default_profile_id: '' }))
            ]);
            this.updateManagedChatProfiles(profilesData);

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
                const firstManagedChat = mergedList.find(chat => chat.id);
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
                    assistantOverview,
                    profiles
                };
                return this._managedChatReferenceData;
            }).finally(() => {
                this._managedChatReferencePromise = null;
            });
        }
        return this._managedChatReferencePromise;
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
            if (card.querySelector('.chat-policy-plugin-push')?.checked) {
                pluginGrants.push({ plugin_name: `${toggle.value}#push`, require_mention: false });
            }
        });
        const roleValue = form.elements.role_id.value;
        const proactiveEnabled = Boolean(isGroup && form.elements.proactive_enabled?.checked);
        const judgeValue = proactiveEnabled ? form.elements.judge_id?.value : '';
        if (proactiveEnabled && !judgeValue) {
            UI.showError('启用主动参与前需要选择一个 Judge');
            form.elements.judge_id?.focus();
            return;
        }
        const memoryMode = form.elements.memory_mode.value;
        const payload = {
            expected_version: Number(form.dataset.version),
            chat: {
                is_group: isGroup,
                listening_enabled: form.elements.listening_enabled.checked,
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
                memory_mode: memoryMode,
                memory_overrides: memoryMode === 'custom' ? {
                    memory_enabled: true,
                    memory_verification_enabled: form.elements.memory_verification_enabled.checked,
                    memory_person_enabled: form.elements.memory_person_enabled.checked,
                    memory_retention_days: Number(form.elements.memory_retention_days.value),
                    memory_retrieval_top_k: Number(form.elements.memory_retrieval_top_k.value)
                } : {},
                ignored_senders: this.linesFromPolicyField(form, 'assistant_ignored_senders'),
                ...(isGroup ? {
                    proactive_enabled: proactiveEnabled,
                    judge_id: judgeValue ? Number(judgeValue) : null
                } : { proactive_enabled: false, judge_id: null })
            },
            codex: { mode: codexMode },
            plugin_grants: pluginGrants
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
            UI.showError(`保存失败：${error.message}`);
            if (/版本|刷新|current_version/.test(error.message)) {
                await this.selectUser(this.currentThreadName, userId);
            }
        } finally {
            controls.forEach(([control, disabled]) => {
                if (control.isConnected) control.disabled = disabled;
            });
            if (form.isConnected) UI.syncChatPolicyDirty(form);
        }
    },

    openSelectedChatMemory() {
        const userId = Number(this._selectedChatPolicy?.user_id || 0);
        if (userId) this.openChatMemoryLibrary(userId);
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
            // each user's permissions, role binding and Judge binding).
            const overview = await API.assistant.getOverview();
            const roles = overview.roles || [];
            const judges = overview.judges || [];
            const chats = overview.chats || [];

            this._assistantOverview = overview;
            this._roles = roles;
            this._judges = judges;
            this._assistantChats = chats;

            UI.updateMetric('statsTotalRoles', roles.length);
            UI.updateMetric('statsTotalJudges', judges.length);
            this.renderAssistantOverview(overview);
            this.renderAssistantChats(chats);
            this.renderPremiumRoles(roles);
            this.renderPremiumJudges(judges);
            this.setAssistantSection(this.getAssistantSectionFromPath(), { history: false });

            const searchInput = document.getElementById('rolesSearchInput');
            if (searchInput && !searchInput.dataset.bound) {
                searchInput.dataset.bound = "true";
                searchInput.addEventListener('input', UI.debounce((e) => {
                    this.filterAndRenderRoles(e.target.value);
                }, 150));
            }

            const chatsSearch = document.getElementById('assistantChatsSearch');
            const chatsFilter = document.getElementById('assistantChatsFilter');
            if (chatsSearch && !chatsSearch.dataset.bound) {
                chatsSearch.dataset.bound = 'true';
                chatsSearch.addEventListener('input', UI.debounce(() => {
                    this.filterAssistantChats();
                }, 150));
            }
            if (chatsFilter && !chatsFilter.dataset.bound) {
                chatsFilter.dataset.bound = 'true';
                chatsFilter.addEventListener('change', () => this.filterAssistantChats());
            }

        } catch (e) {
            console.error('Error loading roles/judges:', e);
            UI.showError('加载 AI 助手控制中心失败：' + e.message);
        }
    },

    getAssistantSectionFromPath() {
        const path = UI.normalizePath(window.location.pathname);
        if (path === '/assistant/chats') return 'chats';
        if (path === '/assistant/roles') return 'roles';
        return 'overview';
    },

    setAssistantSection(section, options = {}) {
        const sections = ['overview', 'chats', 'roles'];
        const selected = sections.includes(section) ? section : 'overview';
        const ids = {
            overview: 'assistantOverviewSection',
            chats: 'assistantChatsSection',
            roles: 'assistantRolesSection'
        };
        document.querySelectorAll('[data-assistant-section]').forEach(button => {
            const active = button.dataset.assistantSection === selected;
            button.classList.toggle('active', active);
            button.setAttribute('aria-selected', String(active));
        });
        Object.entries(ids).forEach(([key, id]) => {
            document.getElementById(id)?.classList.toggle('d-none', key !== selected);
        });
        if (options.history !== false) {
            const paths = { overview: '/assistant', chats: '/assistant/chats', roles: '/assistant/roles' };
            const path = paths[selected];
            if (UI.normalizePath(window.location.pathname) !== path) {
                window.history.pushState({ tab: 'roles', section: selected }, '', path);
            }
        }
    },

    renderAssistantOverview(overview) {
        const summary = overview.summary || {};
        const capability = overview.capability || {};
        const metricContainer = document.getElementById('assistantOverviewStats');
        if (metricContainer) {
            const metrics = [
                ['bi-activity', '运行状态', capability.status === 'running' ? '正在运行' : '未运行', capability.status === 'running' ? 'success' : 'muted'],
                ['bi-chat-square-text', '已启用聊天', `${summary.enabled_chat_count || 0} / ${summary.chat_count || 0}`, 'primary'],
                ['bi-broadcast', '主动回复', `${summary.proactive_chat_count || 0} 个聊天`, 'warning'],
                ['bi-person-badge', '角色 / Judge', `${summary.role_count || 0} / ${summary.judge_count || 0}`, 'violet']
            ];
            metricContainer.innerHTML = metrics.map(([icon, label, value, tone]) => `
                <div class="assistant-metric-card ${tone}">
                    <i class="bi ${icon}"></i>
                    <div><small>${label}</small><strong>${value}</strong></div>
                </div>
            `).join('');
        }

        const flags = overview.global || {};
        const globalSummary = document.getElementById('assistantGlobalSummary');
        if (globalSummary) {
            const defaultRole = (overview.roles || []).find(role => role.name === flags.default_role);
            const rows = [
                ['默认角色', defaultRole?.display_name || flags.default_role || '未设置'],
                ['主动回复', '按群聊独立启用'],
                ['@触发', flags.allow_mention_trigger ? '允许' : '关闭'],
                ['长期记忆', flags.memory_enabled ? '开启' : '关闭'],
                ['网页搜索 / 图片内容补充', `${flags.search_enabled ? '搜索开启' : '搜索关闭'} · ${flags.image_enrichment_enabled ? '图片补充开启' : '图片补充关闭'}`]
            ];
            globalSummary.innerHTML = rows.map(([label, value]) => `
                <div><span>${label}</span><strong>${UI.escapeHtml(String(value))}</strong></div>
            `).join('');
        }

        const modelSummary = document.getElementById('assistantModelSummary');
        if (modelSummary) {
            const mappings = Object.entries(overview.models?.mappings || {});
            if (!mappings.length) {
                modelSummary.innerHTML = '<div class="assistant-empty-inline">尚未配置 Judge 或记忆辅助模型。</div>';
            } else {
                const labels = {
                    chat: '对话回复', judge: '主动判断',
                    memory_generate: '记忆生成', memory_review: '记忆审核',
                    memory_synthesize: '记忆归纳'
                };
                modelSummary.innerHTML = mappings.slice(0, 6).map(([type, mapping]) => `
                    <div><span>${UI.escapeHtml(labels[type] || type)}</span><strong>${UI.escapeHtml(mapping.primary || '未设置')}</strong></div>
                `).join('');
            }
        }
    },

    filterAssistantChats() {
        const query = (document.getElementById('assistantChatsSearch')?.value || '').trim().toLowerCase();
        const filter = document.getElementById('assistantChatsFilter')?.value || 'all';
        const chats = (this._assistantChats || []).filter(chat => {
            const matchesText = !query || chat.chat_name.toLowerCase().includes(query);
            const matchesState = filter === 'all'
                || (filter === 'enabled' && chat.enabled)
                || (filter === 'disabled' && !chat.enabled)
                || (filter === 'proactive' && chat.enabled && chat.proactive_enabled);
            return matchesText && matchesState;
        });
        this.renderAssistantChats(chats);
    },

    renderAssistantChats(chats) {
        const container = document.getElementById('assistantChatsGrid');
        if (!container) return;
        if (!chats.length) {
            container.innerHTML = `
                <div class="assistant-empty-state">
                    <i class="bi bi-chat-square"></i><strong>没有匹配的聊天</strong>
                    <span>可以在“聊天”页添加监听对象，然后在这里配置 AI 助手。</span>
                </div>`;
            return;
        }
        container.innerHTML = chats.map(chat => {
            const name = UI.escapeHtml(chat.chat_name);
            const rawName = UI.escapeHtml(chat.chat_name);
            const role = UI.escapeHtml(chat.role?.display_name || '默认角色');
            const judge = UI.escapeHtml(chat.judge?.display_name || '未启用');
            const memory = chat.memory || {};
            const memoryLabels = {
                inherit: memory.effective_enabled ? '继承全局 · 开启' : '继承全局 · 关闭',
                off: '此聊天关闭',
                custom: '此聊天自定义'
            };
            const triggerCard = chat.is_group
                ? `<div><span>Judge</span><strong>${judge}</strong><small>${chat.proactive_enabled ? '主动回复开启' : '主动回复关闭'}</small></div>`
                : '<div><span>触发方式</span><strong>收到消息</strong><small>私聊直接回复，无需 Judge</small></div>';
            return `
                <article class="assistant-chat-card ${chat.enabled ? 'enabled' : 'disabled'}">
                    <div class="assistant-chat-card-head">
                        <div class="assignment-avatar" style="background:${this.getAvatarColor(chat.chat_name)}">${UI.escapeHtml(this.getInitials(chat.chat_name))}</div>
                        <div class="assistant-chat-identity">
                            <strong title="${rawName}">${name}</strong>
                            <small>${chat.is_group ? '群聊' : '私聊'}${chat.is_listening ? ' · 正在监听' : ' · 未监听'}</small>
                        </div>
                        <span class="assistant-state-pill ${chat.enabled ? 'on' : 'off'}">${chat.enabled ? 'AI 助手已启用' : '未启用'}</span>
                    </div>
                    <div class="assistant-chat-config">
                        <div><span>角色</span><strong>${role}</strong><small>${chat.role_source === 'chat' ? '聊天覆盖' : '继承全局'}</small></div>
                        ${triggerCard}
                        <div><span>连续对话</span><strong>${chat.followup_enabled ? '开启' : '关闭'}</strong><small>${chat.followup_enabled ? `${chat.followup_window_seconds} 秒 · ${chat.followup_max_turns} 轮` : '需要重新触发'}</small></div>
                        <div><span>长期记忆</span><strong>${memoryLabels[memory.mode] || memoryLabels.inherit}</strong><small>${memory.mode === 'custom' ? '仅覆盖必要参数' : (memory.mode === 'off' ? '不读取也不生成新记忆' : '随全局设置自动调整')}</small></div>
                    </div>
                    <div class="assistant-chat-card-footer">
                        <button class="btn btn-sm btn-light border" onclick="App.showAssistantChatEditor(${Number(chat.id)})"><i class="bi bi-box-arrow-up-right me-1"></i>在聊天页配置</button>
                    </div>
                </article>`;
        }).join('');
    },

    async showAssistantGlobalSettings() {
        await this.showCapabilitySettings('assistant');
    },

    openAssistantRoleManager() {
        window.history.pushState({ tab: 'roles', section: 'roles' }, '', '/assistant/roles');
        UI.switchTab('roles', { history: false });
        this.setAssistantSection('roles', { history: false });
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

        this.renderPremiumRoles(filteredRoles);
        this.renderPremiumJudges(filteredJudges);
    },

    renderPremiumRoles(rolesList) {
        const container = document.getElementById('rolesGrid');
        if (!container) return;

        const roleCards = rolesList.map(r => {
            const rawPrompt = String(r.prompt || '');
            const promptPreview = UI.escapeHtml(rawPrompt.substring(0, 160)) + (rawPrompt.length > 160 ? '...' : '');
            const displayName = UI.escapeHtml(r.display_name || r.name || '未命名角色');
            const description = UI.escapeHtml(r.description || '暂无描述');
            const roleId = Number(r.id);
            const userCount = Number(r.user_count || 0);
            const countBadge = `<span class="badge bg-light text-secondary border rounded-pill"><i class="bi bi-people me-1"></i>${userCount} 个启用</span>`;

            return `
                <div class="premium-card-modern premium-role-card fade-in">
                    <div class="card-body p-3 flex-grow-1">
                        <div class="d-flex justify-content-between align-items-start gap-2 mb-1">
                            <h6 class="card-title fw-bold text-truncate mb-0" style="font-size: 0.95rem;" title="${displayName}">${displayName}</h6>
                            ${countBadge}
                        </div>
                        <small class="text-muted text-truncate d-block mb-3" style="font-size: 0.8rem;">${description}</small>

                        <div class="prompt-snippet" title="系统提示词预览">${promptPreview || '<span class="text-muted">提示词为空。</span>'}</div>

                        <div class="d-flex flex-wrap gap-1 mt-2">
                            ${r.output_split_enabled
                                ? `<span class="badge bg-success-subtle text-success border border-success-subtle rounded-pill" style="font-size: 0.72rem;"><i class="bi bi-scissors me-1"></i>拆分 ${Number(r.output_max_chars || 0)}×${Number(r.output_max_count || 0)}</span>`
                                : `<span class="badge bg-secondary-subtle text-secondary border border-secondary-subtle rounded-pill" style="font-size: 0.72rem;"><i class="bi bi-chat-left-dots me-1"></i>单条消息</span>`}
                            ${r.output_strip_trailing_period ? `<span class="badge bg-info-subtle text-info border border-info-subtle rounded-pill" style="font-size: 0.72rem;"><i class="bi bi-eraser me-1"></i>移除句号</span>` : ''}
                        </div>
                    </div>
                    <div class="premium-card-footer">
                        <button class="btn btn-sm btn-outline-primary flex-fill" onclick="App.showRoleEditor(${roleId})" title="编辑角色规则">
                            <i class="bi bi-pencil me-1"></i> 编辑
                        </button>
                        <button class="btn btn-sm btn-outline-danger"
                                onclick="App.deleteRole(${roleId})"
                                title="${userCount > 0 ? `无法删除：有 ${userCount} 个用户正在使用此角色` : '删除'}"
                                ${userCount > 0 ? 'disabled' : ''}>
                            <i class="bi bi-trash"></i>
                        </button>
                    </div>
                </div>
            `;
        }).join('');

        container.innerHTML = roleCards || '<div class="assistant-empty-inline">还没有角色，请创建第一个角色。</div>';
    },

    renderPremiumJudges(judgesList) {
        const container = document.getElementById('judgesGrid');
        if (!container) return;

        const judgeCards = judgesList.map(j => {
            const rawPrompt = String(j.prompt || '');
            const promptPreview = UI.escapeHtml(rawPrompt.substring(0, 160)) + (rawPrompt.length > 160 ? '...' : '');
            const displayName = UI.escapeHtml(j.display_name || j.name || '未命名 Judge');
            const description = UI.escapeHtml(j.description || '暂无描述');
            const judgeId = Number(j.id);
            const userCount = Number(j.user_count || 0);
            const isBuiltin = j.is_builtin === true
                || String(j.is_builtin ?? '').trim().toLowerCase() === 'true';
            const canDelete = !isBuiltin && userCount === 0;
            const deleteTitle = isBuiltin
                ? '内置 Judge 无法删除'
                : (userCount > 0
                    ? `无法删除：有 ${userCount} 个用户正在使用此 Judge`
                    : '删除');
            const modeBadge = j.prompt_mode === 'template'
                ? '<span class="badge bg-primary-subtle text-primary border border-primary-subtle rounded-pill" style="font-size: 0.72rem;">模板</span>'
                : '<span class="badge bg-success-subtle text-success border border-success-subtle rounded-pill" style="font-size: 0.72rem;">简洁</span>';

            return `
                <div class="premium-card-modern premium-judge-card fade-in">
                    <div class="card-body p-3 flex-grow-1">
                        <div class="d-flex justify-content-between align-items-start gap-2 mb-1">
                            <h6 class="card-title fw-bold text-truncate mb-0" style="font-size: 0.95rem;" title="${displayName}">${displayName}</h6>
                            ${modeBadge}
                        </div>
                        <small class="text-muted text-truncate d-block mb-3" style="font-size: 0.8rem;">${description}</small>

                        <div class="prompt-snippet" title="判断规则预览">${promptPreview || '<span class="text-muted">规则为空。</span>'}</div>

                        <div class="timing-grid mt-2">
                            <div class="timing-item" title="触发阈值">
                                <i class="bi bi-chat-left-dots text-primary"></i>
                                <span>触发：${Number(j.trigger_msg_threshold || 0)} 条消息</span>
                            </div>
                            <div class="timing-item" title="触发间隔（分钟）">
                                <i class="bi bi-clock text-primary"></i>
                                <span>间隔：${Number(j.trigger_interval_minutes || 0)} 分钟</span>
                            </div>
                            <div class="timing-item" title="冷却阈值">
                                <i class="bi bi-hourglass text-warning"></i>
                                <span>冷却：${Number(j.cooldown_msg_threshold || 0)} 条消息</span>
                            </div>
                            <div class="timing-item" title="冷却时间（分钟）">
                                <i class="bi bi-shield text-warning"></i>
                                <span>冷却：${Number(j.cooldown_minutes || 0)} 分钟</span>
                            </div>
                        </div>
                    </div>
                    <div class="premium-card-footer">
                        <button class="btn btn-sm btn-outline-primary flex-fill" onclick="App.showJudgeEditor(${judgeId})" title="编辑 Judge 设置">
                            <i class="bi bi-pencil me-1"></i> 编辑
                        </button>
                        <button class="btn btn-sm btn-outline-danger"
                                onclick="App.deleteJudge(${judgeId})"
                                title="${UI.escapeHtml(deleteTitle)}"
                                ${canDelete ? '' : 'disabled'}>
                            <i class="bi bi-trash"></i>
                        </button>
                    </div>
                </div>
            `;
        }).join('');

        container.innerHTML = judgeCards || '<div class="assistant-empty-inline">还没有 Judge，主动回复将不会生效。</div>';
    },

    renderTabbedRoleEditorForm(role = {}, isCreate = false) {
        const splitEnabled = !!role.output_split_enabled;
        const maxChars = UI.escapeHtml(role.output_max_chars ?? 120);
        const maxCount = UI.escapeHtml(role.output_max_count ?? 3);
        const stripPeriod = role.output_strip_trailing_period !== false;
        const interval = UI.escapeHtml(role.output_interval_seconds ?? 1.0);
        const roleId = UI.escapeHtml(role.id || '');
        const displayName = UI.escapeHtml(role.display_name || '');
        const internalName = UI.escapeHtml(role.name || '');
        const description = UI.escapeHtml(role.description || '');
        const prompt = UI.escapeHtml(role.prompt || '');

        return `
            <form id="roleForm" class="p-1">
                <input type="hidden" name="id" value="${roleId}">

                <!-- Tab Controls -->
                <ul class="nav editor-modal-tabs mb-4" role="tablist">
                    <li class="nav-item" role="presentation">
                        <button class="nav-link active" id="role-prompt-tab" data-bs-toggle="tab" data-bs-target="#role-prompt-panel" type="button" role="tab">
                            <i class="bi bi-chat-square-quote"></i> 系统身份
                        </button>
                    </li>
                    <li class="nav-item" role="presentation">
                        <button class="nav-link" id="role-output-tab" data-bs-toggle="tab" data-bs-target="#role-output-panel" type="button" role="tab">
                            <i class="bi bi-sliders"></i> 输出拆分
                        </button>
                    </li>
                </ul>

                <!-- Tab Contents -->
                <div class="tab-content">
                    <!-- Panel 1: Identity & System Prompt -->
                    <div class="tab-pane fade show active" id="role-prompt-panel" role="tabpanel">
                        <div class="row g-3 mb-3">
                            <div class="col-md-6">
                                <label class="form-label fw-semibold">显示名称 <span class="text-danger">*</span></label>
                                <input type="text" class="form-control" name="display_name" value="${displayName}" placeholder="例如：客户服务" required>
                            </div>
                            <div class="col-md-6">
                                <label class="form-label fw-semibold">内部 ID（唯一）<span class="text-danger">*</span></label>
                                <input type="text" class="form-control ${!isCreate ? 'bg-light' : ''}" name="name" value="${internalName}" placeholder="例如：support_role" ${!isCreate ? 'disabled readonly' : 'required'}>
                                ${isCreate ? '<div class="form-text" style="font-size:0.75rem;">仅支持字母、数字和下划线，创建后无法修改。</div>' : ''}
                            </div>
                        </div>
                        <div class="mb-3">
                            <label class="form-label fw-semibold">简要描述</label>
                            <input type="text" class="form-control" name="description" value="${description}" placeholder="简要说明角色用途">
                        </div>
                        <div class="mb-3">
                            <label class="form-label fw-semibold d-flex justify-content-between align-items-center">
                                <span>系统提示词 <span class="text-danger">*</span></span>
                                <span class="badge bg-secondary-subtle text-secondary rounded-pill fw-normal" style="font-size:0.75rem;">LLM 指令</span>
                            </label>
                            <textarea class="form-control font-monospace border-secondary border-opacity-25" name="prompt" rows="10" placeholder="你是一名专业的聊天助手……" style="font-size: 0.85rem; line-height: 1.5;" required>${prompt}</textarea>
                            <div class="form-text mt-2" style="font-size: 0.78rem; line-height: 1.45;">
                                <strong class="text-primary"><i class="bi bi-info-circle"></i> 动态变量：</strong><br>
                                <code>{chat_text}</code> - 最近消息 &nbsp;|&nbsp;
                                <code>{search_results}</code> - Web 搜索上下文 &nbsp;|&nbsp;
                                <code>{sender}</code> - 当前用户 &nbsp;|&nbsp;
                                <code>{content}</code> - 用户消息
                            </div>
                        </div>
                    </div>

                    <!-- Panel 2: Human-like Splitting Controls -->
                    <div class="tab-pane fade" id="role-output-panel" role="tabpanel">
                        <div class="bg-light p-3 rounded mb-4 border d-flex justify-content-between align-items-center">
                            <div>
                                <h6 class="fw-bold mb-1 text-dark">模拟真人输入</h6>
                                <p class="text-muted mb-0" style="font-size: 0.78rem;">按角色拆分消息，让 Bot 回复显得更自然。</p>
                            </div>
                            <div class="form-check form-switch modern-toggle">
                                <input class="form-check-input" type="checkbox" role="switch" name="output_split_enabled" ${splitEnabled ? 'checked' : ''}>
                            </div>
                        </div>

                        <div class="row g-3">
                            <div class="col-md-4">
                                <label class="form-label fw-semibold">单段字符上限</label>
                                <div class="input-group input-group-sm">
                                    <input type="number" class="form-control" name="output_max_chars" min="10" max="2000" value="${maxChars}">
                                    <span class="input-group-text">字符</span>
                                </div>
                                <div class="form-text" style="font-size:0.75rem;">单段消息的最大长度。</div>
                            </div>
                            <div class="col-md-4">
                                <label class="form-label fw-semibold">最多消息段数</label>
                                <div class="input-group input-group-sm">
                                    <input type="number" class="form-control" name="output_max_count" min="1" max="10" value="${maxCount}">
                                    <span class="input-group-text">条</span>
                                </div>
                                <div class="form-text" style="font-size:0.75rem;">一条回复最多拆分成多少段。</div>
                            </div>
                            <div class="col-md-4">
                                <label class="form-label fw-semibold">发送间隔</label>
                                <div class="input-group input-group-sm">
                                    <input type="number" class="form-control" name="output_interval_seconds" min="0" max="10" step="0.1" value="${interval}">
                                    <span class="input-group-text">秒</span>
                                </div>
                                <div class="form-text" style="font-size:0.75rem;">各消息段之间的发送间隔。</div>
                            </div>
                        </div>

                        <div class="form-check mt-4 border-top pt-3">
                            <input class="form-check-input" type="checkbox" name="output_strip_trailing_period" id="stripTrailingPeriod" ${stripPeriod ? 'checked' : ''}>
                            <label class="form-check-label fw-semibold" for="stripTrailingPeriod" style="font-size:0.88rem;">移除末尾句号</label>
                            <div class="form-text" style="font-size:0.75rem;">自动移除回复末尾的句号（。或 .），使表达更自然。</div>
                        </div>
                    </div>
                </div>
            </form>
        `;
    },

    renderTabbedJudgeEditorForm(judge = {}, isCreate = false) {
        const triggerMsgs = UI.escapeHtml(judge.trigger_msg_threshold ?? 5);
        const triggerMinutes = UI.escapeHtml(judge.trigger_interval_minutes ?? 1);
        const cooldownMsgs = UI.escapeHtml(judge.cooldown_msg_threshold ?? triggerMsgs);
        const cooldownMinutes = UI.escapeHtml(judge.cooldown_minutes ?? triggerMinutes);
        const judgeId = UI.escapeHtml(judge.id || '');
        const displayName = UI.escapeHtml(judge.display_name || '');
        const internalName = UI.escapeHtml(judge.name || '');
        const description = UI.escapeHtml(judge.description || '');
        const prompt = UI.escapeHtml(judge.prompt || '');

        return `
            <form id="judgeForm" class="p-1">
                <input type="hidden" name="id" value="${judgeId}">

                <!-- Tab Controls -->
                <ul class="nav editor-modal-tabs mb-4" role="tablist">
                    <li class="nav-item" role="presentation">
                        <button class="nav-link active" id="judge-rules-tab" data-bs-toggle="tab" data-bs-target="#judge-rules-panel" type="button" role="tab">
                            <i class="bi bi-shield-check"></i> 判断规则
                        </button>
                    </li>
                    <li class="nav-item" role="presentation">
                        <button class="nav-link" id="judge-timing-tab" data-bs-toggle="tab" data-bs-target="#judge-timing-panel" type="button" role="tab">
                            <i class="bi bi-clock"></i> 触发时机
                        </button>
                    </li>
                </ul>

                <!-- Tab Contents -->
                <div class="tab-content">
                    <!-- Panel 1: Decision Prompt -->
                    <div class="tab-pane fade show active" id="judge-rules-panel" role="tabpanel">
                        <div class="row g-3 mb-3">
                            <div class="col-md-6">
                                <label class="form-label fw-semibold">显示名称 <span class="text-danger">*</span></label>
                                <input type="text" class="form-control" name="display_name" value="${displayName}" placeholder="例如：主动销售 Judge" required>
                            </div>
                            <div class="col-md-6">
                                <label class="form-label fw-semibold">内部 ID（唯一）<span class="text-danger">*</span></label>
                                <input type="text" class="form-control ${!isCreate ? 'bg-light' : ''}" name="name" value="${internalName}" placeholder="例如：strict_judge" ${!isCreate ? 'disabled readonly' : 'required'}>
                                ${isCreate ? '<div class="form-text" style="font-size:0.75rem;">仅支持字母、数字和下划线，创建后无法修改。</div>' : ''}
                            </div>
                        </div>
                        <div class="mb-3">
                            <label class="form-label fw-semibold">简要描述</label>
                            <input type="text" class="form-control" name="description" value="${description}" placeholder="简要说明此 Judge 的用途">
                        </div>
                        <div class="row g-3 mb-3">
                            <div class="col-md-6">
                                <label class="form-label fw-semibold">提示词判断模式</label>
                                <select class="form-select" name="prompt_mode" onchange="App.onJudgePromptModeChange(this.value)">
                                    <option value="simple" ${judge.prompt_mode === 'simple' || !judge.prompt_mode ? 'selected' : ''}>简洁（推荐）</option>
                                    <option value="template" ${judge.prompt_mode === 'template' ? 'selected' : ''}>模板（高级）</option>
                                </select>
                            </div>
                            <div class="col-md-6 d-flex align-items-end">
                                <div id="judgePromptModeHint" class="form-text mt-0 bg-light p-2 rounded border" style="font-size: 0.75rem; line-height: 1.4;">
                                    简洁模式会自动注入上下文，系统会强制要求 JSON 输出格式。
                                </div>
                            </div>
                        </div>
                        <div class="mb-3">
                            <label class="form-label fw-semibold d-flex justify-content-between align-items-center">
                                <span>Judge 判断规则 <span class="text-danger">*</span></span>
                                <span class="badge bg-secondary-subtle text-secondary rounded-pill fw-normal" style="font-size:0.75rem;">判断提示词</span>
                            </label>
                            <textarea class="form-control font-monospace border-secondary border-opacity-25" name="prompt" rows="9" placeholder="指定触发 Bot 的用户意图条件……" style="font-size: 0.85rem; line-height: 1.5;" required>${prompt}</textarea>

                            <div id="judgeTemplateTools" class="mt-2 d-none">
                                <button type="button" class="btn btn-sm btn-outline-secondary" onclick="App.insertJudgeTemplateVar('{{chat_text}}')">
                                    <i class="bi bi-braces me-1"></i> 插入 {{chat_text}}
                                </button>
                                <small class="text-muted ms-2">高级用法：使用 <code>{{chat_text}}</code> 注入自定义聊天上下文。</small>
                            </div>
                        </div>
                    </div>

                    <!-- Panel 2: Cadence Timing Controls -->
                    <div class="tab-pane fade" id="judge-timing-panel" role="tabpanel">
                        <div class="alert alert-info py-2 px-3 small border-info border-opacity-25 mb-4 d-flex align-items-start gap-2">
                            <i class="bi bi-info-circle-fill mt-1 text-info"></i>
                            <div>
                                <strong>触发频率规则：</strong>主动判断按以下时间限制执行。消息较多或间隔较短时会触发检查，判断拒绝后则进入冷却期。
                            </div>
                        </div>

                        <h6 class="fw-bold mb-3 text-dark border-bottom pb-2"><i class="bi bi-lightning text-primary me-1"></i>触发时机规则</h6>
                        <div class="row g-3 mb-4">
                            <div class="col-md-6">
                                <label class="form-label fw-semibold">触发消息数</label>
                                <div class="input-group input-group-sm">
                                    <input type="number" class="form-control" name="trigger_msg_threshold" min="0" max="1000" value="${triggerMsgs}">
                                    <span class="input-group-text">条</span>
                                </div>
                                <div class="form-text" style="font-size:0.75rem;">未回复消息达到此数量时主动检查。</div>
                            </div>
                            <div class="col-md-6">
                                <label class="form-label fw-semibold">触发间隔上限</label>
                                <div class="input-group input-group-sm">
                                    <input type="number" class="form-control" name="trigger_interval_minutes" min="0" max="1440" value="${triggerMinutes}">
                                    <span class="input-group-text">分钟</span>
                                </div>
                                <div class="form-text" style="font-size:0.75rem;">距上一条 AI 消息达到此时长时主动检查。</div>
                            </div>
                        </div>

                        <h6 class="fw-bold mb-3 text-dark border-bottom pb-2"><i class="bi bi-hourglass-split text-warning me-1"></i>冷却规则（判断拒绝后）</h6>
                        <div class="row g-3">
                            <div class="col-md-6">
                                <label class="form-label fw-semibold">冷却消息数</label>
                                <div class="input-group input-group-sm">
                                    <input type="number" class="form-control" name="cooldown_msg_threshold" min="0" max="1000" value="${cooldownMsgs}">
                                    <span class="input-group-text">条</span>
                                </div>
                                <div class="form-text" style="font-size:0.75rem;">Judge 拒绝后，在收到这些消息前跳过主动检查。</div>
                            </div>
                            <div class="col-md-6">
                                <label class="form-label fw-semibold">冷却时长</label>
                                <div class="input-group input-group-sm">
                                    <input type="number" class="form-control" name="cooldown_minutes" min="0" max="1440" value="${cooldownMinutes}">
                                    <span class="input-group-text">分钟</span>
                                </div>
                                <div class="form-text" style="font-size:0.75rem;">Judge 拒绝后，在此时长内跳过主动检查。</div>
                            </div>
                        </div>
                    </div>
                </div>
            </form>
        `;
    },

    showCreateRoleModal() {
        const html = this.renderTabbedRoleEditorForm({}, true);
        document.getElementById('configModalBody').innerHTML = html;
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
        try {
            const form = document.getElementById('roleForm');
            if (!form.checkValidity()) {
                form.reportValidity();
                return;
            }

            const data = {
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
            if (!roleId) {
                data.name = form.name.value;
                await API.roles.create(data);
            } else {
                await API.roles.update(roleId, data);
            }

            const modal = bootstrap.Modal.getInstance(document.getElementById('configModal'));
            if (modal) modal.hide();

            // Refresh UI to show changes
            this.loadRoles();
        } catch (e) {
            UI.showError('操作失败：' + e.message);
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
        document.getElementById('configModalTitle').textContent = '创建新 Judge';

        const saveBtn = document.getElementById('configModalSaveBtn');
        saveBtn.classList.remove('d-none');
        saveBtn.onclick = async () => {
            await this.saveJudge(null);
        };

        new bootstrap.Modal(document.getElementById('configModal')).show();
        this.onJudgePromptModeChange('simple');
    },

    async showJudgeEditor(judgeId) {
        try {
            const result = await API.judges.getDetail(judgeId);
            const judge = result.judge;

            const html = this.renderTabbedJudgeEditorForm(judge, false);
            document.getElementById('configModalBody').innerHTML = html;
            document.getElementById('configModalTitle').textContent = `编辑 Judge：${judge.display_name}`;

            const saveBtn = document.getElementById('configModalSaveBtn');
            saveBtn.classList.remove('d-none');
            saveBtn.onclick = async () => {
                await this.saveJudge(judgeId);
            };

            new bootstrap.Modal(document.getElementById('configModal')).show();
            this.onJudgePromptModeChange(judge.prompt_mode || 'simple');
        } catch (e) {
            UI.showError(e.message);
        }
    },

    onJudgePromptModeChange(mode) {
        const hint = document.getElementById('judgePromptModeHint');
        const tools = document.getElementById('judgeTemplateTools');
        const isTemplate = mode === 'template';

        if (hint) {
            hint.textContent = isTemplate
                ? '模板模式：使用 {chat_text} / {{chat_text}} 插入上下文。系统仍会强制要求 JSON 输出格式。'
                : '简洁模式会自动注入上下文。系统会强制要求 JSON 输出格式，无需手动指定。';
        }
        if (tools) {
            tools.classList.toggle('d-none', !isTemplate);
        }
    },

    insertJudgeTemplateVar(token) {
        const form = document.getElementById('judgeForm');
        if (!form || !form.prompt) return;

        const textarea = form.prompt;
        const start = textarea.selectionStart || 0;
        const end = textarea.selectionEnd || 0;
        const before = textarea.value.substring(0, start);
        const after = textarea.value.substring(end);

        textarea.value = `${before}${token}${after}`;
        textarea.focus();
        textarea.selectionStart = textarea.selectionEnd = start + token.length;
    },

    async saveJudge(judgeId) {
        try {
            const form = document.getElementById('judgeForm');
            if (!form.checkValidity()) {
                form.reportValidity();
                return;
            }

            const data = {
                display_name: form.display_name.value,
                description: form.description.value,
                prompt_mode: form.prompt_mode.value,
                prompt: form.prompt.value,
                trigger_msg_threshold: parseInt(form.trigger_msg_threshold.value || '0', 10),
                trigger_interval_minutes: parseInt(form.trigger_interval_minutes.value || '0', 10),
                cooldown_msg_threshold: parseInt(form.cooldown_msg_threshold.value || '0', 10),
                cooldown_minutes: parseInt(form.cooldown_minutes.value || '0', 10)
            };

            if (!judgeId) {
                data.name = form.name.value;
                await API.judges.create(data);
            } else {
                await API.judges.update(judgeId, data);
            }

            const modal = bootstrap.Modal.getInstance(document.getElementById('configModal'));
            if (modal) modal.hide();
            this.loadRoles();
        } catch (e) {
            UI.showError('Judge 操作失败：' + e.message);
        }
    },

    async deleteJudge(judgeId) {
        if (!await UI.confirm('确定删除此 Judge 吗？该操作无法撤销。', {
            title: '删除 Judge',
            confirmText: '删除',
            variant: 'danger'
        })) return;
        try {
            await API.judges.delete(judgeId);
            this.loadRoles();
        } catch (e) {
            UI.showError('删除 Judge 失败：' + e.message);
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
