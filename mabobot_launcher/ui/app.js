(() => {
    'use strict';

    const ui = {
        currentPage: 'overview',
        snapshot: null,
        logs: [],
        lastSequence: 0,
        logFilter: 'all',
        polling: false,
        connected: false,
        modalAction: null,
    };

    const $ = (selector, root = document) => root.querySelector(selector);
    const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

    function escapeHtml(value) {
        return String(value ?? '')
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;')
            .replaceAll("'", '&#039;');
    }

    function icon(name) {
        return `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
    }

    function serviceIcon(key) {
        return key === 'web' ? 'globe' : 'message';
    }

    function serviceDescription(key) {
        return key === 'web'
            ? 'FastAPI 应用、AI 助手、插件与本地管理控制台。'
            : '通过 mabowx 连接微信，负责消息监听、读取与发送。';
    }

    function formatDuration(seconds) {
        const value = Math.max(0, Number(seconds) || 0);
        if (value < 60) return '少于 1 分钟';
        const minutes = Math.floor(value / 60);
        if (minutes < 60) return `${minutes} 分钟`;
        const hours = Math.floor(minutes / 60);
        const remainder = minutes % 60;
        if (hours < 24) return remainder ? `${hours} 小时 ${remainder} 分钟` : `${hours} 小时`;
        const days = Math.floor(hours / 24);
        return `${days} 天 ${hours % 24} 小时`;
    }

    function formatTime(timestamp) {
        if (!timestamp) return '--:--:--';
        return new Date(Number(timestamp) * 1000).toLocaleTimeString('zh-CN', {
            hour12: false,
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
        });
    }

    function serviceByKey(key) {
        return ui.snapshot?.services?.find(item => item.key === key) || null;
    }

    function statusClass(status) {
        return ['running', 'starting', 'stopping', 'degraded', 'error', 'stopped'].includes(status)
            ? status
            : 'stopped';
    }

    function render(snapshot) {
        ui.snapshot = snapshot;
        $('#titleVersion').textContent = snapshot.version || '3.0.2';
        renderOverall();
        renderGlobalControls();
        renderOverviewServices();
        renderDetailedServices();
        renderRecentLogs();
        renderLogStream();
        renderEnvironment();
        renderSettings();
    }

    function renderOverall() {
        const snapshot = ui.snapshot;
        const overall = snapshot.overall || { status: 'stopped', label: '状态未知' };
        const overallDot = $('#overallDot');
        overallDot.className = `presence-dot ${statusClass(overall.status)}`;
        $('#overallLabel').textContent = overall.label;
        $('#sidebarDot').className = `presence-dot ${statusClass(overall.status)}`;
        $('#sidebarStatus').textContent = overall.label;

        const bot = serviceByKey('bot');
        const web = serviceByKey('web');
        const wechatOnline = Boolean(bot?.extra?.wechat_online);
        const wechatConnected = Boolean(bot?.extra?.wechat_connected);
        const wechatLabel = wechatOnline ? '在线' : (wechatConnected ? '已连接' : statusText(bot));
        setStripStatus($('#wechatStatus'), wechatLabel, wechatOnline || bot?.status === 'running', bot?.status);
        setStripStatus($('#webStatus'), statusText(web), web?.status === 'running', web?.status);
        $('#uptimeValue').textContent = formatDuration(snapshot.uptime_seconds);
    }

    function statusText(service) {
        return service?.status_label || '未启动';
    }

    function setStripStatus(element, label, ready, serviceStatus) {
        element.textContent = label;
        element.className = ready ? '' : (['error', 'degraded'].includes(serviceStatus) ? 'is-warning' : 'is-muted');
    }

    function renderGlobalControls() {
        const services = ui.snapshot?.services || [];
        const transition = services.find(service => ['starting', 'stopping'].includes(service.status));
        const fullyActive = services.length > 0 && services.every(service => ['running', 'degraded'].includes(service.status));
        const anyActive = services.some(service => ['running', 'starting', 'stopping', 'degraded'].includes(service.status));
        const stopping = transition?.status === 'stopping';
        const stopMode = fullyActive || stopping;
        const action = fullyActive ? 'stop-all' : 'start-all';
        const label = transition ? (stopping ? '停止中' : '启动中') : (fullyActive ? '停止' : '启动');

        $$('[data-service-toggle]').forEach(button => {
            button.dataset.action = action;
            button.disabled = Boolean(transition);
            button.classList.toggle('button-primary', !stopMode);
            button.classList.toggle('button-secondary', stopMode);
            button.classList.toggle('is-stop', stopMode);
            button.querySelector('use').setAttribute('href', stopMode ? '#i-stop' : '#i-play');
            button.querySelector('span').textContent = label;
            button.setAttribute('aria-label', `${label} Mabobot 服务`);
        });

        $$('[data-service-restart]').forEach(button => {
            button.disabled = Boolean(transition) || !anyActive;
        });
    }

    function renderOverviewServices() {
        const container = $('#overviewServices');
        container.innerHTML = (ui.snapshot.services || []).map(service => {
            return `
                <article class="service-card">
                    <div class="service-card-top">
                        <span class="service-icon">${icon(serviceIcon(service.key))}</span>
                        <div class="service-copy">
                            <h3>${escapeHtml(service.label)}</h3>
                            <span class="service-state ${statusClass(service.status)}"><span class="presence-dot ${statusClass(service.status)}"></span>${escapeHtml(service.status_label)}</span>
                        </div>
                    </div>
                    <div class="service-address"><span>${escapeHtml(service.address)}</span><span>${service.pid ? `PID ${service.pid}` : '等待启动'}</span></div>
                </article>`;
        }).join('');
    }

    function renderDetailedServices() {
        const container = $('#detailedServices');
        container.innerHTML = (ui.snapshot.services || []).map(service => {
            return `
                <article class="detailed-service">
                    <div class="detailed-main">
                        <span class="service-icon">${icon(serviceIcon(service.key))}</span>
                        <div class="detailed-copy">
                            <h2>${escapeHtml(service.label)} <span class="service-state ${statusClass(service.status)}"><span class="presence-dot ${statusClass(service.status)}"></span>${escapeHtml(service.status_label)}</span></h2>
                            <p>${serviceDescription(service.key)}</p>
                            <div class="service-facts">
                                <span class="service-fact">${icon('terminal')}${escapeHtml(service.script)}</span>
                                <span class="service-fact">${icon('globe')}${escapeHtml(service.address)}</span>
                                <span class="service-fact">${icon('clock')}${service.pid ? formatDuration(service.uptime_seconds) : '未运行'}</span>
                                <span class="service-fact">${icon('info')}${escapeHtml(service.detail)}</span>
                            </div>
                        </div>
                    </div>
                </article>`;
        }).join('');
    }

    function renderRecentLogs() {
        const target = $('#recentLogs');
        const items = ui.logs.slice(-5).reverse();
        if (!items.length) {
            target.innerHTML = '<div class="empty-state">等待服务日志</div>';
            return;
        }
        target.innerHTML = items.map(item => {
            const level = item.level === 'error' ? 'error' : (item.level === 'warning' ? 'warning' : 'success');
            const symbol = level === 'success' ? 'check' : 'info';
            return `<div class="recent-log-item"><span class="log-level-icon ${level}">${icon(symbol)}</span><span class="recent-message">${escapeHtml(item.message)}</span><time class="recent-time">${formatTime(item.timestamp)}</time></div>`;
        }).join('');
    }

    function filteredLogs() {
        if (ui.logFilter === 'all') return ui.logs;
        if (ui.logFilter === 'error') return ui.logs.filter(item => item.level === 'error');
        return ui.logs.filter(item => item.source === ui.logFilter);
    }

    function renderLogStream(forceBottom = false) {
        const stream = $('#logStream');
        const shouldFollow = $('#followLogs').checked;
        const wasNearBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 55;
        const items = filteredLogs();
        if (!items.length) {
            stream.innerHTML = '<div class="empty-state">当前筛选下没有日志</div>';
        } else {
            stream.innerHTML = items.map(item => `
                <div class="log-line ${escapeHtml(item.level)}">
                    <time class="log-time">${formatTime(item.timestamp)}</time>
                    <span class="log-source">${escapeHtml(item.source)}</span>
                    <span class="log-message">${escapeHtml(item.message)}</span>
                </div>`).join('');
        }
        $('#logCount').textContent = `${items.length} 条记录`;
        const errorCount = ui.logs.filter(item => item.level === 'error').length;
        const badge = $('#logBadge');
        badge.textContent = String(errorCount);
        badge.hidden = errorCount === 0;
        if (forceBottom || (shouldFollow && (wasNearBottom || ui.currentPage !== 'logs'))) {
            window.requestAnimationFrame(() => { stream.scrollTop = stream.scrollHeight; });
        }
    }

    function renderEnvironment() {
        const checks = ui.snapshot.environment || [];
        const readyCount = checks.filter(item => item.ready).length;
        $('#environmentSummary').textContent = `${readyCount}/${checks.length} 就绪`;
        $('#environmentList').innerHTML = checks.map(item => `
            <div class="environment-item">
                <span class="environment-icon">${icon(item.key === 'wechat' ? 'message' : (item.key === 'dependencies' ? 'layers' : 'terminal'))}</span>
                <span class="environment-name">${escapeHtml(item.label)}</span>
                <span class="environment-result ${item.ready ? '' : 'is-missing'}">${icon(item.ready ? 'check' : 'info')}${escapeHtml(item.detail)}</span>
            </div>`).join('');
        $('#settingsEnvironment').innerHTML = checks.map(item => `
            <div class="environment-tile"><strong>${escapeHtml(item.label)}</strong><span class="${item.ready ? '' : 'is-missing'}"><i class="presence-dot ${item.ready ? 'running' : 'starting'}"></i>${escapeHtml(item.detail)}</span></div>`).join('');
    }

    function renderSettings() {
        const settings = ui.snapshot.settings || {};
        const startupEnabled = Boolean(settings.launch_at_login);
        const autoLoginEnabled = Boolean(settings.auto_confirm_wechat);
        ['#startupToggle', '#startupToggleSettings'].forEach(selector => {
            const input = $(selector);
            input.checked = startupEnabled;
            input.disabled = !settings.startup?.supported;
        });
        ['#autoLoginToggle', '#autoLoginToggleSettings'].forEach(selector => {
            const input = $(selector);
            input.checked = autoLoginEnabled;
            input.disabled = !startupEnabled;
        });
        $('#autoLoginRow').classList.toggle('is-disabled', !startupEnabled);
        $('#autoLoginSettingsRow').classList.toggle('is-disabled', !startupEnabled);
        $('#autoLoginHint').textContent = startupEnabled ? '微信在线后再启动服务' : '依赖登录启动';
        $('#startupDetail').textContent = settings.startup?.detail || '未登记 Windows 登录启动';
        $('#closeBehavior').value = settings.close_behavior || 'ask';
        const trayAvailable = settings.tray_available !== false;
        $('#closeBehavior option[value="tray"]').textContent = trayAvailable ? '最小化到系统托盘' : '最小化窗口';
        $('#closeTrayLabel').textContent = trayAvailable ? '最小化到系统托盘' : '最小化窗口';
        $('#closeTrayLabel').nextElementSibling.textContent = trayAvailable ? '机器人继续运行，可从托盘重新打开。' : '系统托盘不可用，窗口将保留在任务栏，机器人继续运行。';
        $('#closeBehaviorHint').textContent = { ask: '关闭时选择继续运行或完全退出', tray: trayAvailable ? '机器人继续运行，可从托盘重新打开' : '系统托盘不可用，将保留在任务栏', exit: '停止微信 Bot 和 Web 服务，并退出启动器' }[settings.close_behavior || 'ask'];

        const repairText = ui.snapshot.repairing ? '正在修复环境' : '修复环境';
        $$('[data-action="repair"]').forEach(button => {
            button.disabled = Boolean(ui.snapshot.repairing);
            const label = button.querySelector('[data-repair-label]');
            if (label) label.textContent = repairText;
        });
    }

    function mergeLogs(items) {
        if (!Array.isArray(items) || !items.length) return;
        const known = new Set(ui.logs.map(item => item.seq));
        for (const item of items) {
            if (!known.has(item.seq)) ui.logs.push(item);
        }
        ui.logs.sort((a, b) => Number(a.seq) - Number(b.seq));
        if (ui.logs.length > 1200) ui.logs = ui.logs.slice(-1200);
    }

    async function apiCall(method, ...args) {
        const api = window.pywebview?.api;
        if (!api || typeof api[method] !== 'function') throw new Error('桌面桥接尚未就绪');
        return api[method](...args);
    }

    async function poll() {
        if (ui.polling) return;
        ui.polling = true;
        try {
            const snapshot = await apiCall('get_snapshot', ui.lastSequence);
            mergeLogs(snapshot.logs);
            ui.lastSequence = Number(snapshot.last_sequence) || ui.lastSequence;
            ui.connected = true;
            render(snapshot);
        } catch (error) {
            if (ui.connected) toast(`与启动器连接中断：${error.message || error}`, 'error');
            ui.connected = false;
            $('#sidebarStatus').textContent = '启动器连接中断';
            $('#sidebarDot').className = 'presence-dot error';
        } finally {
            ui.polling = false;
            window.setTimeout(poll, ui.connected ? 900 : 1600);
        }
    }

    async function execute(method, args = [], successMessage = '') {
        try {
            const result = await apiCall(method, ...args);
            if (result && result.ok === false) throw new Error(result.error || '操作未完成');
            if (successMessage) toast(successMessage);
            await refreshNow();
            return true;
        } catch (error) {
            toast(error.message || String(error), 'error');
            return false;
        }
    }

    async function refreshNow() {
        if (ui.polling) return;
        ui.polling = true;
        try {
            const snapshot = await apiCall('get_snapshot', ui.lastSequence);
            mergeLogs(snapshot.logs);
            ui.lastSequence = Number(snapshot.last_sequence) || ui.lastSequence;
            ui.connected = true;
            render(snapshot);
        } finally {
            ui.polling = false;
        }
    }

    function showPage(page) {
        if (!['overview', 'services', 'logs', 'settings'].includes(page)) return;
        ui.currentPage = page;
        $$('.nav-item').forEach(item => item.classList.toggle('is-active', item.dataset.page === page));
        $$('[data-page-panel]').forEach(panel => panel.classList.toggle('is-visible', panel.dataset.pagePanel === page));
        if (page === 'logs') renderLogStream(true);
        if (page === 'settings') window.EmailNotifications?.mount($('#launcherEmailConsole'), {
            get: () => apiCall('get_email_preferences'),
            save: values => apiCall('save_email_preferences', values),
            test: () => apiCall('test_email_preferences')
        });
    }

    function toast(message, level = 'success') {
        const item = document.createElement('div');
        item.className = `toast ${level}`;
        item.textContent = message;
        $('#toastStack').append(item);
        window.setTimeout(() => item.remove(), 3500);
    }

    function confirmAction(title, message, confirmLabel, action) {
        ui.modalPreviousFocus = document.activeElement;
        $('#closeOptions').hidden = true;
        $('#modalTitle').textContent = title;
        $('#modalMessage').textContent = message;
        $('#modalConfirm').textContent = confirmLabel;
        ui.modalAction = action;
        $('#confirmModal').hidden = false;
        $('#modalCancel').focus();
    }

    function closeModal() {
        $('#confirmModal').hidden = true;
        ui.modalAction = null;
        ui.modalPreviousFocus?.focus();
    }

    window.showLauncherCloseDialog = function () {
        if (!$('#confirmModal').hidden && !$('#closeOptions').hidden) return;
        confirmAction('关闭 Mabobot', '请选择关闭窗口后的运行方式。', '确定', async () => {
            const choice = $('input[name="closeChoice"]:checked').value;
            await execute('close_window', [choice, $('#rememberClose').checked]);
        });
        $('#closeOptions').hidden = false;
    };

    async function requestWindowClose() {
        try {
            const result = await apiCall('close_window');
            if (result?.ok === false) throw new Error(result.error);
            if (result?.needs_choice) window.showLauncherCloseDialog();
        } catch (error) { toast(error.message || String(error), 'error'); }
    }

    function bindEvents() {
        document.addEventListener('click', async event => {
            const nav = event.target.closest('[data-page]');
            if (nav) return showPage(nav.dataset.page);
            const pageLink = event.target.closest('[data-go-page]');
            if (pageLink) return showPage(pageLink.dataset.goPage);

            const actionButton = event.target.closest('[data-action]');
            if (!actionButton) return;
            const action = actionButton.dataset.action;
            if (action === 'open-web') return execute('open_web_console');
            if (action === 'open-folder') return execute('open_project_folder');
            if (action === 'start-all') return execute('start_all', [], 'Mabobot 启动请求已提交');
            if (action === 'restart-all') return execute('restart_all', [], 'Mabobot 已重启');
            if (action === 'stop-all') {
                return confirmAction('停止 Mabobot', '微信消息监听与 Web 控制台都会停止，桌面启动器仍会保持打开。', '停止', () => execute('stop_all', [], 'Mabobot 已停止'));
            }
            if (action === 'repair') {
                return confirmAction('修复运行环境', '将先停止服务，检查基础组件并重新安装项目依赖；缺失的 Python 或 WebView2 会从官方源自动补齐，完成后恢复服务。该过程可能需要数分钟。', '开始修复', () => execute('repair_environment', [], '环境修复已在后台开始'));
            }
            if (action === 'exit') {
                return confirmAction('退出 Mabobot', '这会停止全部受管服务并退出系统托盘。确定继续吗？', '停止并退出', () => execute('exit_application'));
            }
        });

        $('#minimizeButton').addEventListener('click', () => execute('minimize_window'));
        const toggleWindowMaximize = async () => {
            try {
                const result = await apiCall('toggle_maximize');
                document.body.classList.toggle('is-maximized', Boolean(result?.maximized));
            } catch (error) {
                toast(error.message || String(error), 'error');
            }
        };
        $('#maximizeButton').addEventListener('click', toggleWindowMaximize);
        $('#closeButton').addEventListener('click', requestWindowClose);
        $('.window-actions').addEventListener('mousedown', event => event.stopPropagation());
        $('#titlebar').addEventListener('dblclick', event => {
            if (!event.target.closest('.window-actions')) toggleWindowMaximize();
        });
        $$('[data-resize-edge]').forEach(handle => {
            handle.addEventListener('mousedown', event => {
                if (event.button !== 0) return;
                event.preventDefault();
                event.stopPropagation();
                apiCall('begin_window_resize', handle.dataset.resizeEdge).catch(error => {
                    toast(error.message || String(error), 'error');
                });
            });
        });
        $('#openFolderSide').addEventListener('click', () => execute('open_project_folder'));
        $('#openLogsFolder').addEventListener('click', () => execute('open_logs_folder'));
        $('#clearLogs').addEventListener('click', async () => {
            if (await execute('clear_logs')) {
                ui.logs = [];
                ui.lastSequence = 0;
                await refreshNow();
            }
        });

        ['#startupToggle', '#startupToggleSettings'].forEach(selector => {
            $(selector).addEventListener('change', async event => {
                const enabled = event.target.checked;
                $$('input[id^="startupToggle"]').forEach(input => { input.disabled = true; });
                await execute('set_launch_at_login', [enabled], enabled ? '已开启登录启动' : '已关闭登录启动');
            });
        });
        ['#autoLoginToggle', '#autoLoginToggleSettings'].forEach(selector => {
            $(selector).addEventListener('change', event => execute(
                'set_auto_confirm_wechat',
                [event.target.checked],
                event.target.checked ? '已开启微信登录自动确认' : '已关闭微信登录自动确认',
            ));
        });

        $('#closeBehavior').addEventListener('change', event => execute('set_close_behavior', [event.target.value], '已保存关闭方式'));

        $('#logFilters').addEventListener('click', event => {
            const button = event.target.closest('[data-filter]');
            if (!button) return;
            ui.logFilter = button.dataset.filter;
            $$('#logFilters button').forEach(item => item.classList.toggle('is-active', item === button));
            renderLogStream(true);
        });
        $('#followLogs').addEventListener('change', event => renderLogStream(event.target.checked));
        $('#modalCancel').addEventListener('click', closeModal);
        $('#modalConfirm').addEventListener('click', async () => {
            const action = ui.modalAction;
            closeModal();
            if (action) await action();
        });
        $('#confirmModal').addEventListener('click', event => {
            if (event.target === $('#confirmModal')) closeModal();
        });
        document.addEventListener('keydown', event => {
            if (event.key === 'Tab' && !$('#confirmModal').hidden) {
                const controls = [...$('#confirmModal').querySelectorAll('button, input')].filter(item => item.getClientRects().length && !item.disabled);
                const first = controls[0], last = controls[controls.length - 1];
                if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
                else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
            }
            if (event.key === 'Escape' && !$('#confirmModal').hidden) closeModal();
            if (event.ctrlKey && event.key.toLowerCase() === 'l') {
                event.preventDefault();
                showPage('logs');
            }
        });
    }

    function demoSnapshot() {
        const now = Date.now() / 1000;
        return {
            version: '3.0.2',
            uptime_seconds: 2580,
            overall: { status: 'running', label: '系统运行正常' },
            services: [
                { key: 'web', label: 'Web 服务', script: 'start.py', status: 'running', status_label: '正常', detail: '健康检查通过', pid: 14208, uptime_seconds: 2580, address: '127.0.0.1:8888', extra: {} },
                { key: 'bot', label: '微信 Bot', script: 'wx_bot.py', status: 'running', status_label: '正常', detail: '健康检查通过', pid: 14116, uptime_seconds: 2584, address: '127.0.0.1:5555', extra: { wechat_connected: true, wechat_online: true } },
            ],
            logs: [], last_sequence: 5,
            settings: { launch_at_login: true, auto_confirm_wechat: true, close_behavior: 'tray', startup: { supported: true, detail: '当前用户登录后自动启动' } },
            environment: [
                { key: 'python', label: 'Python 3.12', ready: true, detail: '已就绪' },
                { key: 'dependencies', label: '项目依赖', ready: true, detail: '已就绪' },
                { key: 'wechat', label: '微信客户端', ready: true, detail: '运行中' },
                { key: 'codex', label: 'Codex', ready: true, detail: '已就绪', optional: true },
            ],
            repairing: false,
        };
    }

    function start() {
        bindEvents();
        const parameters = new URLSearchParams(window.location.search);
        if (parameters.has('preview')) {
            const demo = demoSnapshot();
            ui.logs = [
                { seq: 1, timestamp: Date.now() / 1000 - 17, source: '系统', level: 'success', message: 'Mabobot 已准备就绪' },
                { seq: 2, timestamp: Date.now() / 1000 - 14, source: 'Web 服务', level: 'success', message: 'Web 服务已启动' },
                { seq: 3, timestamp: Date.now() / 1000 - 10, source: '微信 Bot', level: 'success', message: '微信 Bot 连接成功' },
                { seq: 4, timestamp: Date.now() / 1000 - 6, source: '环境', level: 'info', message: '运行环境检查完成' },
                { seq: 5, timestamp: Date.now() / 1000 - 2, source: '系统', level: 'success', message: '所有服务运行正常' },
            ];
            render(demo);
            showPage(parameters.get('page') || 'overview');
            return;
        }
        if (window.pywebview?.api) poll();
        else window.addEventListener('pywebviewready', poll, { once: true });
    }

    start();
})();
