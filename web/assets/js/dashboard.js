/**
 * 概览值班台
 *
 * 页面按值守问题分带：还在跑吗（状态带）→ 有事要处理吗（待处理）→
 * 今天跑得怎么样（KPI 与 24 小时活动）→ 花在哪（额度与用量）→
 * 最近发生了什么（系统级事件时间线）。数据源各自独立降级，
 * 单个接口失败只影响对应分带。
 */
const Dashboard = {
    // 整页 30 秒刷新（main.js 的自动刷新）之外，这一条独立心跳每 5 秒跑一次：
    // 后端只让它读内存索引里追加的字节，用来表达"系统还在进消息"。
    PULSE_INTERVAL_MS: 5000,
    PULSE_TICK_MS: 1000,

    state: {
        loading: false,
        attentionItems: [],
        attentionExpanded: false,
        timelineItems: [],
        timelineVisible: [],
        timelineFilter: 'all',
        timelineKinds: [],
        lastCodexStatus: null,
        pulse: null,
        pulseTimer: null,
        pulseTickTimer: null,
        pulseInFlight: false,
        pulseFailed: false,
        lastActivityAt: null,
        sparklines: {},
        hourlyData: [],
        hourlyGeneratedAt: null,
        hourlyIndex: null,
        hourlyPinned: false,
        hourlyBound: false,
    },

    /* ===== 加载 ===== */

    async load(options = {}) {
        if (this.state.loading) {
            // 已经在刷新时也要保证心跳在跑：切回概览依赖它，不能等下一轮。
            this.startPulse();
            return;
        }
        this.state.loading = true;
        this.setBusy(options.quiet !== true);
        this.startPulse();
        try {
            const requests = [
                API.dashboard.getTimeseries(),
                API.dashboard.getAttention(),
                API.dashboard.getEvents(),
                API.dashboard.getCodexStatus(),
                API.dashboard.getUsage('today'),
                API.dashboard.getUsageSeries(7),
                API.system.getStatus(),
                API.system.getHealthDetails(),
                API.wechat.getStatus(),
                API.wechat.getListeners(),
            ];
            const labels = ['趋势', '待处理', '事件', 'Codex', '模型用量', '用量趋势', '资源', '运行状态', '微信', '监听'];
            const settled = await Promise.allSettled(requests);
            const value = (index, fallback) => settled[index].status === 'fulfilled' ? settled[index].value : fallback;
            const failed = settled
                .map((item, index) => item.status === 'rejected' ? labels[index] : null)
                .filter(Boolean);

            const timeseries = value(0, null);
            const attention = value(1, null);
            const events = value(2, null);
            const codexStatus = value(3, {
                status: 'error',
                quota_message: settled[3].reason?.message || 'Codex 状态暂不可用',
            });
            const usageToday = value(4, null);
            const usageSeries = value(5, null);
            const systemStatus = value(6, null);
            const health = value(7, null);
            const wechatStatus = value(8, null);
            const listened = value(9, null);

            this.renderStatus({ systemStatus, health, wechatStatus, listened, timeseries });
            this.renderAttention(attention, failed.includes('待处理'));
            this.renderKpis({ timeseries, usageToday, usageSeries });
            this.renderHourly(timeseries);
            this.renderUsageSummary(usageToday);
            this.renderTopChats(timeseries);
            this.renderCodexStatus(codexStatus);
            this.renderTimeline(events, failed.includes('事件'));

            this.reportFreshness(failed);
        } catch (error) {
            console.error('Failed to render dashboard:', error);
            this.reportFreshness(['渲染']);
        } finally {
            this.state.loading = false;
            this.setBusy(false);
        }
    },

    refresh() {
        return this.load();
    },

    setBusy(busy) {
        const button = document.getElementById('dashboardRefreshBtn');
        if (!button) return;
        button.disabled = !!busy;
        const icon = button.querySelector('i');
        if (icon) icon.classList.toggle('spinning', !!busy);
    },

    reportFreshness(failed) {
        const updated = document.getElementById('dashboardUpdatedAt');
        if (!updated) return;
        const stamp = UI.formatTimeOfDay(new Date());
        updated.textContent = failed.length
            ? `更新 ${stamp} · ${failed.length} 项暂不可用`
            : `更新 ${stamp}`;
        updated.title = failed.length ? `本次未取到：${failed.join('、')}` : '全部数据已刷新';
    },

    markStale(ids) {
        ids.forEach(id => {
            const element = document.getElementById(id);
            if (!element) return;
            element.dataset.stale = 'true';
            element.title = '本区域本次刷新失败，内容可能不是最新状态';
        });
    },

    /* ===== 状态带 ===== */

    renderStatus({ systemStatus, health, wechatStatus, listened, timeseries }) {
        const verdict = document.getElementById('dashboardVerdict');
        const verdictText = document.getElementById('dashboardVerdictText');
        const verdictMeta = document.getElementById('dashboardVerdictMeta');
        const facts = document.getElementById('dashboardStatusFacts');
        if (!verdict || !verdictText || !facts) return;

        const wechatOnline = wechatStatus?.status === 'connected' || wechatStatus?.running === true;
        const status = health?.status || (health ? (health.ready ? 'ready' : 'not_ready') : null);
        const verdictTone = status === 'ready' ? 'is-ok' : status === 'degraded' ? 'is-warning' : status ? 'is-danger' : 'is-checking';
        const verdictCopy = {
            'is-ok': ['运行正常', 'bi-check-circle'],
            'is-warning': ['部分能力降级', 'bi-exclamation-triangle'],
            'is-danger': ['核心组件异常', 'bi-x-octagon'],
            'is-checking': ['状态读取中…', 'bi-circle'],
        }[verdictTone];
        verdict.dataset.tone = verdictTone.replace('is-', '');
        verdictText.textContent = verdictCopy[0];
        verdict.querySelector('i')?.setAttribute('class', `bi ${verdictCopy[1]}`);

        const activeOperations = Number(health?.operations?.active_count || 0);
        const metaParts = [];
        if (systemStatus?.uptime) metaParts.push(`面板已运行 ${systemStatus.uptime}`);
        if (!wechatOnline) metaParts.push('微信未连接');
        if (activeOperations) metaParts.push(`${activeOperations} 个后台任务执行中`);
        verdictMeta.textContent = metaParts.join(' · ') || '微信、监听与后台任务均正常';

        const listenedCount = listened?.listened_chats ? Object.keys(listened.listened_chats).length : null;
        const diskPercent = Number(systemStatus?.disk_percent);
        const cpuPercent = Number(systemStatus?.cpu_percent);
        const memoryPercent = Number(systemStatus?.memory_percent);
        const lastActivity = timeseries?.last_activity_at;
        const rows = [
            {
                label: '微信',
                value: wechatOnline ? '在线' : '离线',
                tone: wechatOnline ? 'is-ok' : 'is-danger',
                title: wechatOnline ? '微信连接正常' : '机器人当前收不到消息',
            },
            {
                label: '监听',
                value: listenedCount === null ? '—' : `${UI.formatNumber(listenedCount)} 个聊天`,
                tone: listenedCount ? 'is-ok' : '',
                title: '正在监听的聊天数量',
            },
            {
                id: 'dashboardLastMessageFact',
                valueId: 'dashboardLastMessageValue',
                label: '最后消息',
                value: lastActivity ? UI.formatRelativeTime(lastActivity) : '今天还没有消息',
                title: lastActivity || '今天还没有收到消息',
            },
            {
                label: '后台任务',
                value: activeOperations ? `${UI.formatNumber(activeOperations)} 项` : '空闲',
                tone: activeOperations ? 'is-busy' : '',
                title: activeOperations ? `${activeOperations} 个平台托管任务正在执行` : '当前没有平台托管任务',
            },
        ];
        if (Number.isFinite(diskPercent)) {
            rows.push({
                label: '磁盘',
                value: `${diskPercent.toFixed(0)}%`,
                tone: diskPercent >= 90 ? 'is-danger' : diskPercent >= 80 ? 'is-warning' : '',
                title: `磁盘已用 ${diskPercent.toFixed(1)}%`,
            });
        }
        const temperature = this.temperatureFact(systemStatus?.temperature);
        if (temperature) rows.push(temperature);
        if (Number.isFinite(memoryPercent) && memoryPercent >= 80) {
            rows.push({
                label: '内存', value: `${memoryPercent.toFixed(0)}%`,
                tone: memoryPercent >= 90 ? 'is-danger' : 'is-warning',
                title: '内存占用偏高',
            });
        }
        if (Number.isFinite(cpuPercent) && cpuPercent >= 80) {
            rows.push({
                label: 'CPU', value: `${cpuPercent.toFixed(0)}%`,
                tone: cpuPercent >= 90 ? 'is-danger' : 'is-warning',
                title: 'CPU 占用偏高',
            });
        }
        if (systemStatus?.system_uptime) {
            rows.push({
                label: '开机',
                value: systemStatus.system_uptime,
                title: `这台机器已连续开机 ${systemStatus.system_uptime}`,
            });
        }

        facts.innerHTML = rows.map(row => `
            <li class="${row.tone || ''}"${row.id ? ` id="${row.id}"` : ''} title="${UI.escapeHtml(row.title || '')}">
                <span>${UI.escapeHtml(row.label)}</span>
                <strong${row.valueId ? ` id="${row.valueId}"` : ''}>${UI.escapeHtml(row.value)}</strong>
            </li>
        `).join('');
        facts.removeAttribute('data-stale');
        if (lastActivity && lastActivity > (this.state.lastActivityAt || '')) {
            this.state.lastActivityAt = lastActivity;
        }
    },

    temperatureFact(temperature) {
        const sensors = Array.isArray(temperature?.sensors)
            ? temperature.sensors.filter(sensor => Number.isFinite(Number(sensor?.celsius)))
            : [];
        if (!sensors.length) return null;
        const hottest = Math.max(...sensors.map(sensor => Number(sensor.celsius)));
        const tone = hottest >= 90 ? 'is-danger' : hottest >= 75 ? 'is-warning' : '';
        const readout = sensor => `${sensor.label || '温度'} ${Number(sensor.celsius).toFixed(1)}°C`;
        return {
            label: '温度',
            value: sensors.slice(0, 2).map(readout).join(' · '),
            tone,
            title: sensors.map(sensor => [
                readout(sensor), sensor.sensor_name, sensor.source,
            ].filter(Boolean).join(' · ')).join(' ｜ '),
        };
    },

    /* ===== 实时脉搏 ===== */

    // 概览页可见时每 5 秒拉一次脉搏，另用 1 秒一次的本地 tick 更新相对时间：
    // 相对时间只改文本，不额外发请求，新消息到达时数字跳回"刚刚"。
    startPulse() {
        if (document.hidden) return;
        this.renderPulse();
        this.refreshPulse();
        if (!this.state.pulseTimer) {
            this.state.pulseTimer = setInterval(() => this.refreshPulse(), this.PULSE_INTERVAL_MS);
        }
        if (!this.state.pulseTickTimer) {
            this.state.pulseTickTimer = setInterval(() => this.renderPulse(), this.PULSE_TICK_MS);
        }
    },

    stopPulse() {
        if (this.state.pulseTimer) {
            clearInterval(this.state.pulseTimer);
            this.state.pulseTimer = null;
        }
        if (this.state.pulseTickTimer) {
            clearInterval(this.state.pulseTickTimer);
            this.state.pulseTickTimer = null;
        }
    },

    async refreshPulse() {
        if (document.hidden || this.state.pulseInFlight) return;
        const shell = document.getElementById('dashboard');
        if (shell && shell.classList.contains('d-none')) return;
        this.state.pulseInFlight = true;
        try {
            const payload = await API.dashboard.getPulse();
            // 接口异常时后端会返回 200 + {error}（窗口为空），这种载荷不能当成
            // "最近 5 分钟没有消息"，否则会把读取失败伪装成"静默"。
            if (!payload?.windows?.['5m']) {
                throw new Error(payload?.error || '脉搏数据结构不完整');
            }
            this.state.pulseFailed = false;
            this.applyPulse(payload);
        } catch (error) {
            // 拿不到数据时保留上一次的读数并标记陈旧，绝不假装"静默"。
            this.state.pulseFailed = true;
            this.renderPulse();
        } finally {
            this.state.pulseInFlight = false;
        }
    },

    applyPulse(payload) {
        const previous = this.state.pulse;
        const lastActivity = payload?.last_activity_at || null;
        if (lastActivity && lastActivity > (this.state.lastActivityAt || '')) {
            this.state.lastActivityAt = lastActivity;
        }
        this.state.pulse = payload || null;
        const advanced = !!lastActivity && lastActivity !== (previous?.last_activity_at || null);
        const received = Number(payload?.windows?.['5m']?.received || 0);
        const previousReceived = Number(previous?.windows?.['5m']?.received || 0);
        this.renderPulse({ flash: advanced || received > previousReceived });
    },

    pulseWindow(minutes) {
        return this.state.pulse?.windows?.[`${minutes}m`] || null;
    },

    pulseState() {
        if (this.state.pulseFailed) return 'stale';
        if (!this.state.pulse) return 'idle';
        if (Number(this.pulseWindow(1)?.received || 0) > 0) return 'live';
        if (Number(this.pulseWindow(5)?.received || 0) > 0) return 'recent';
        return 'idle';
    },

    renderPulse({ flash = false } = {}) {
        const five = this.pulseWindow(5) || {};
        const received = Number(five.received || 0);
        const replies = Number(five.replies || 0);
        const text = !this.state.pulse
            ? (this.state.pulseFailed ? '脉搏暂不可用' : '正在读取聊天脉搏…')
            : received
                ? `最近 5 分钟 ${UI.formatNumber(received)} 条 · 回复 ${UI.formatNumber(replies)} 条`
                : this.state.lastActivityAt
                    ? `静默中 · 最后消息 ${UI.formatRelativeTime(this.state.lastActivityAt)}`
                    : '静默中 · 今天还没有消息';

        const element = document.getElementById('dashboardPulse');
        const lastMessage = document.getElementById('dashboardLastMessageValue');
        const fact = document.getElementById('dashboardLastMessageFact');
        if (fact) {
            if (this.state.pulseFailed) {
                fact.dataset.stale = 'true';
                fact.title = '脉搏刷新失败，显示的可能是旧数据';
            } else {
                fact.removeAttribute('data-stale');
                fact.title = this.state.lastActivityAt || '今天还没有收到消息';
            }
        }
        if (lastMessage) {
            const age = this.state.lastActivityAt
                ? this.formatLiveAge(this.state.lastActivityAt)
                : '今天还没有消息';
            if (lastMessage.textContent !== age) lastMessage.textContent = age;
        }
        if (element) {
            element.dataset.state = this.pulseState();
            if (this.state.pulseFailed) {
                element.dataset.stale = 'true';
            } else {
                element.removeAttribute('data-stale');
            }
        }
        const textNode = document.getElementById('dashboardPulseText');
        if (textNode && textNode.textContent !== text) textNode.textContent = text;
        if (flash) {
            // 新消息到达：脉搏与「最后消息」各闪一下，动效由 CSS 降级开关兜底。
            this.flashPulseNode(element);
            this.flashPulseNode(fact);
        }
    },

    flashPulseNode(element) {
        if (!element) return;
        element.classList.remove('is-flash');
        void element.offsetWidth;
        element.classList.add('is-flash');
        setTimeout(() => element.classList.remove('is-flash'), 900);
    },

    // 10 秒内说"刚刚"，60 秒内报秒，之后交回 UI.formatRelativeTime 的粗粒度。
    formatLiveAge(value) {
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return UI.formatRelativeTime(value);
        const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
        if (seconds < 10) return '刚刚';
        if (seconds < 60) return `${seconds} 秒前`;
        return UI.formatRelativeTime(value);
    },

    /* ===== 待处理 ===== */

    renderAttention(payload, stale) {
        const band = document.getElementById('dashboardAttentionBand');
        const list = document.getElementById('dashboardAttentionList');
        if (!band || !list) return;

        if (stale) {
            this.state.attentionItems = [];
            this.state.attentionSummary = {};
            band.hidden = false;
            this.markStale(['dashboardAttentionBand']);
            list.hidden = false;
            list.innerHTML = '<li class="dashboard-attention-empty">本次未能读取待处理事项，稍后会自动重试。</li>';
            const count = document.getElementById('dashboardAttentionCount');
            const summary = document.getElementById('dashboardAttentionSummary');
            const toggle = document.getElementById('dashboardAttentionToggle');
            if (count) count.textContent = '—';
            if (summary) summary.textContent = '';
            if (toggle) toggle.hidden = true;
            return;
        }

        this.state.attentionItems = payload?.items || [];
        this.state.attentionSummary = payload?.summary || {};
        this.applyAttention();
    },

    /* 默认折叠：只留标题、数量与严重度，展开/收起由用户决定，
       自动刷新不会把用户展开的状态收回去。 */
    applyAttention() {
        const band = document.getElementById('dashboardAttentionBand');
        const list = document.getElementById('dashboardAttentionList');
        const count = document.getElementById('dashboardAttentionCount');
        const summaryEl = document.getElementById('dashboardAttentionSummary');
        const toggle = document.getElementById('dashboardAttentionToggle');
        const items = this.state.attentionItems;
        const summary = this.state.attentionSummary;

        if (!band || !list) return;
        if (!items.length) {
            band.hidden = true;
            list.innerHTML = '';
            list.hidden = true;
            return;
        }
        band.hidden = false;
        if (count) {
            count.textContent = UI.formatNumber(summary.total || items.length);
            count.classList.toggle('is-critical', Number(summary.critical || 0) > 0);
            count.classList.toggle('is-warning', !Number(summary.critical || 0) && Number(summary.warning || 0) > 0);
        }
        if (summaryEl) {
            summaryEl.textContent = [
                [summary.critical, '严重'], [summary.warning, '警告'], [summary.info, '提示'],
            ]
                .filter(([value]) => Number(value) > 0)
                .map(([value, label]) => `${label} ${UI.formatNumber(value)}`)
                .join(' · ');
        }

        const expanded = this.state.attentionExpanded;
        if (toggle) {
            toggle.hidden = false;
            toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
            toggle.innerHTML = expanded
                ? '收起<i class="bi bi-chevron-up" aria-hidden="true"></i>'
                : '展开<i class="bi bi-chevron-down" aria-hidden="true"></i>';
        }
        list.hidden = !expanded;
        if (!expanded) {
            this.state.attentionVisible = [];
            list.innerHTML = '';
            return;
        }

        this.state.attentionVisible = items;
        list.innerHTML = items.map((item, index) => this.attentionItemHtml(item, index)).join('');
        list.removeAttribute('data-stale');
    },

    attentionItemHtml(item, index) {
        const severity = ['critical', 'warning', 'info'].includes(item.severity) ? item.severity : 'info';
        const icons = { critical: 'bi-exclamation-octagon', warning: 'bi-exclamation-triangle', info: 'bi-info-circle' };
        const action = item.action && item.action.tab
            ? `<button type="button" class="dashboard-text-action" onclick="Dashboard.openAttention(${index})">${UI.escapeHtml(item.action.label || '查看')}</button>`
            : '';
        const at = item.at ? UI.formatRelativeTime(item.at) : '';
        return `
            <li class="dashboard-attention-item is-${severity}">
                <i class="bi ${icons[severity]}" aria-hidden="true"></i>
                <div class="dashboard-attention-copy">
                    <strong>${UI.escapeHtml(item.title || '')}</strong>
                    ${item.detail ? `<p>${UI.escapeHtml(item.detail)}</p>` : ''}
                </div>
                ${at ? `<span class="dashboard-attention-time">${UI.escapeHtml(at)}</span>` : ''}
                ${action}
            </li>
        `;
    },

    openAttention(index) {
        const item = (this.state.attentionVisible || [])[index];
        if (item?.action) this.navigate(item.action);
    },

    toggleAttention() {
        this.state.attentionExpanded = !this.state.attentionExpanded;
        this.applyAttention();
    },

    navigate(action) {
        if (!action || !action.tab) return;
        const path = action.path || UI.routes?.[action.tab] || '/';
        if (UI.normalizePath(window.location.pathname) !== path) {
            window.history.pushState({ tab: action.tab, section: action.section }, '', path);
        }
        UI.switchTab(action.tab, { history: false });
    },

    openCallHistory() {
        this.navigate({ tab: 'usage', section: 'llm-history', path: '/usage/calls' });
    },

    /* ===== 今日 KPI ===== */

    renderKpis({ timeseries, usageToday, usageSeries }) {
        const today = timeseries?.today;
        const yesterday = timeseries?.yesterday;
        const daily = timeseries?.daily || [];
        // 真实接口 /api/llm/usage 返回 {data:{period,view,totals,rows,metadata}}；
        // totals 才是区间汇总，别再读不存在的 metrics。
        const metrics = usageToday?.data?.totals || {};
        const series = usageSeries?.data || [];

        const setText = (id, text) => {
            const element = document.getElementById(id);
            if (element) {
                element.textContent = text;
                element.removeAttribute('data-stale');
            }
        };
        if (!timeseries) this.markStale(['statTodayMessages', 'statAiReplies', 'sparkMessages']);
        if (!usageToday) this.markStale(['dashboardCost']);

        setText('statTodayMessages', today ? UI.formatNumber(today.received) : '—');
        setText('statAiReplies', today ? UI.formatNumber(today.replies) : '—');
        setText('statActiveUsersPill', today ? UI.formatNumber(today.chats) : '—');

        setText('statTodayMessagesMeta', today
            ? (yesterday ? `昨日全天 ${UI.formatNumber(yesterday.received)}` : '昨日数据暂不可用')
            : '暂不可用');
        setText('statAiRepliesMeta', today
            ? (yesterday ? `昨日全天 ${UI.formatNumber(yesterday.replies)}` : '')
            : '暂不可用');

        const costText = this.formatCosts(metrics.costs);
        const averageCost = this.averageDailyCost(series);
        setText('dashboardCost', costText);
        setText('dashboardCostMeta', !usageToday ? '暂不可用'
            : averageCost ? `7 日均 ${averageCost}` : '今日暂无用量');

        const currentDate = String(timeseries?.generated_at || '').slice(0, 10)
            || new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' });
        this.renderSparkline('sparkMessages', daily.map(day => ({
            date: day.date, value: day.received, text: `收到消息 ${UI.formatNumber(day.received)} 条`,
        })), currentDate);
        this.renderSparkline('sparkReplies', daily.map(day => ({
            date: day.date, value: day.replies, text: `Bot发送 ${UI.formatNumber(day.replies)} 条`,
        })), currentDate);
        this.renderSparkline('sparkCost', series.map(day => ({
            date: day.date, value: this.costTotal(day.costs),
            text: `成本 ${Object.entries(day.costs || {}).map(([code, amount]) =>
                `${code} ${UI.formatNumber(amount, { maximumFractionDigits: 8 })}`).join(' + ') || '0'}`,
        })), currentDate);
    },

    renderSparkline(elementId, data, currentDate) {
        const svg = document.getElementById(elementId);
        if (!svg) return;
        const container = svg.closest('.dashboard-spark-chart');
        if (!container) return;
        this.hideSparkTip(elementId);
        const chart = this.state.sparklines[elementId] ||= {};
        Object.assign(chart, { data: data || [], currentDate, index: null, pinned: false });
        this.bindSparkline(elementId, container);
        const series = chart.data.map(day => Math.max(0, Number(day.value) || 0));
        container.tabIndex = series.length ? 0 : -1;
        if (!series.length) {
            svg.innerHTML = '';
            svg.classList.add('is-empty');
            return;
        }
        svg.classList.remove('is-empty');
        const width = 120;
        const height = 28;
        const pad = 3;
        const max = Math.max(...series);
        const step = series.length > 1 ? (width - pad * 2) / (series.length - 1) : 0;
        const points = series.map((value, index) => {
            const x = pad + index * step;
            const y = height - pad - (max ? (value / max) * (height - pad * 2) : 0);
            return [Math.round(x * 10) / 10, Math.round(y * 10) / 10];
        });
        chart.points = points;
        const line = points.map(point => point.join(',')).join(' ');
        const last = points[points.length - 1];
        svg.innerHTML = `
            <polyline class="dashboard-spark-line" points="${line}"></polyline>
            <circle class="dashboard-spark-dot" cx="${last[0]}" cy="${last[1]}" r="2"></circle>
            <circle class="dashboard-spark-active" cx="${last[0]}" cy="${last[1]}" r="2.5" hidden></circle>
        `;
    },

    bindSparkline(elementId, container) {
        if (container.dataset.readoutBound) return;
        container.dataset.readoutBound = 'true';
        const indexAt = event => {
            const total = this.state.sparklines[elementId]?.data.length || 0;
            const rect = container.getBoundingClientRect();
            if (!total || !rect.width) return null;
            // Match the SVG's 3px padding and snap across the whole chart area.
            const ratio = ((event.clientX - rect.left) / rect.width * 120 - 3) / 114;
            return Math.max(0, Math.min(total - 1, Math.round(ratio * (total - 1))));
        };
        container.addEventListener('mousemove', event => {
            if (this.state.sparklines[elementId]?.pinned) return;
            const index = indexAt(event);
            if (index !== null) this.showSparkTip(elementId, index);
        });
        container.addEventListener('mouseleave', () => {
            if (!this.state.sparklines[elementId]?.pinned) this.hideSparkTip(elementId);
        });
        container.addEventListener('click', event => {
            const index = indexAt(event);
            if (index === null) return;
            const chart = this.state.sparklines[elementId];
            if (chart.pinned && chart.index === index) this.hideSparkTip(elementId);
            else this.showSparkTip(elementId, index, true);
        });
        container.addEventListener('focus', () => {
            const total = this.state.sparklines[elementId]?.data.length || 0;
            if (total) this.showSparkTip(elementId, total - 1);
        });
        container.addEventListener('blur', () => this.hideSparkTip(elementId));
        container.addEventListener('keydown', event => {
            const chart = this.state.sparklines[elementId];
            const total = chart?.data.length || 0;
            if (!total) return;
            if (event.key === 'Escape') {
                this.hideSparkTip(elementId);
                return;
            }
            let index = chart.index ?? total - 1;
            if (event.key === 'ArrowLeft') index--;
            else if (event.key === 'ArrowRight') index++;
            else if (event.key === 'Home') index = 0;
            else if (event.key === 'End') index = total - 1;
            else return;
            event.preventDefault();
            this.showSparkTip(elementId, Math.max(0, Math.min(total - 1, index)), true);
        });
        document.addEventListener('click', event => {
            if (!container.contains(event.target)) this.hideSparkTip(elementId);
        });
    },

    showSparkTip(elementId, index, pinned = false) {
        const chart = this.state.sparklines[elementId];
        const day = chart?.data[index];
        const svg = document.getElementById(elementId);
        const container = svg?.closest('.dashboard-spark-chart');
        const tip = document.getElementById(`${elementId}Tip`);
        const point = chart?.points?.[index];
        if (!day || !container || !tip || !point) return;
        Object.keys(this.state.sparklines).filter(id => id !== elementId)
            .forEach(id => this.hideSparkTip(id));
        tip.innerHTML = `<strong>${UI.escapeHtml(day.date || '日期未知')}</strong>
            <span>${UI.escapeHtml(day.text)}</span>${day.date === chart.currentDate ? '<span>截至当前</span>' : ''}`;
        tip.hidden = false;
        const half = tip.offsetWidth / 2;
        const center = point[0] / 120 * container.clientWidth;
        tip.style.left = `${Math.max(half, Math.min(center, container.clientWidth - half))}px`;
        const dot = svg.querySelector('.dashboard-spark-active');
        dot.setAttribute('cx', point[0]);
        dot.setAttribute('cy', point[1]);
        dot.removeAttribute('hidden');
        Object.assign(chart, { index, pinned });
    },

    hideSparkTip(elementId) {
        const tip = document.getElementById(`${elementId}Tip`);
        if (tip) tip.hidden = true;
        document.getElementById(elementId)?.querySelector('.dashboard-spark-active')?.setAttribute('hidden', '');
        const chart = this.state.sparklines[elementId];
        if (chart) Object.assign(chart, { index: null, pinned: false });
    },

    formatCosts(costs) {
        const symbols = { USD: '$', CNY: '¥', RMB: '¥', EUR: '€', GBP: '£', JPY: '¥' };
        const entries = Object.entries(costs || {}).filter(([, value]) => Number(value) > 0);
        if (!entries.length) return '—';
        return entries.slice(0, 2).map(([code, value]) => {
            const amount = Number(value);
            const text = amount >= 100 ? amount.toFixed(0) : amount.toFixed(amount >= 1 ? 2 : 3);
            const symbol = symbols[String(code).toUpperCase()];
            return symbol ? `${symbol}${text}` : `${code} ${text}`;
        }).join(' + ');
    },

    costTotal(costs) {
        return Object.values(costs || {}).reduce((sum, value) => sum + (Number(value) || 0), 0);
    },

    averageDailyCost(series) {
        const totals = {};
        let days = 0;
        (series || []).forEach(day => {
            const present = Object.entries(day.costs || {}).filter(([, value]) => Number(value) > 0);
            if (!present.length) return;
            days += 1;
            present.forEach(([code, value]) => {
                totals[code] = (totals[code] || 0) + Number(value);
            });
        });
        if (!days) return '';
        const [code, total] = Object.entries(totals).sort((a, b) => b[1] - a[1])[0];
        return this.formatCosts({ [code]: total / days });
    },

    formatTokens(value) {
        const tokens = Number(value) || 0;
        if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
        if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}K`;
        return UI.formatNumber(tokens);
    },

    /* ===== 24 小时活动 ===== */

    renderHourly(timeseries) {
        const container = document.getElementById('dashboardHourlyChart');
        if (!container) return;
        const hourly = timeseries?.hourly || [];
        const selectedHour = (this.state.hourlyData || [])[this.state.hourlyIndex]?.at;
        const pinned = this.state.hourlyPinned;
        this.state.hourlyData = hourly;
        this.state.hourlyGeneratedAt = timeseries?.generated_at || null;
        this.state.hourlyIndex = null;
        this.state.hourlyPinned = false;
        if (!hourly.length) {
            container.dataset.stale = 'true';
            container.innerHTML = '<div class="dashboard-empty">最近 24 小时的聊天记录暂不可用。</div>';
            const summary = document.getElementById('dashboardHourlySummary');
            if (summary) summary.textContent = '';
            return;
        }
        const max = Math.max(1, ...hourly.flatMap(hour => [Number(hour.received) || 0, Number(hour.replies) || 0]));
        container.innerHTML = `
            <div class="dashboard-hourly-bars" id="dashboardHourlyBars">${hourly.map((hour, index) => {
                const received = Math.max(0, Number(hour.received) || 0);
                const replies = Math.max(0, Number(hour.replies) || 0);
                // Independent counts share one scale; split replies can exceed received messages.
                const receivedHeight = received / max * 100;
                const repliesHeight = replies / max * 100;
                const label = index % 3 === 0 ? `${hour.at.slice(11)}:00` : '';
                return `<div class="dashboard-hour" data-hour-index="${index}">
                    <div class="dashboard-hour-track${replies > received ? ' has-more-replies' : ''}">
                        <span class="dashboard-hour-bar is-received" style="height:${receivedHeight}%"></span>
                        <span class="dashboard-hour-bar is-replies" style="height:${repliesHeight}%"></span>
                    </div>
                    <small>${label}</small>
                </div>`;
            }).join('')}</div>
            <div class="dashboard-hour-tip" id="dashboardHourlyTip" role="status" hidden></div>
        `;
        this.bindHourly();

        const summary = document.getElementById('dashboardHourlySummary');
        if (summary) {
            const total = hourly.reduce((sum, hour) => sum + (Number(hour.received) || 0), 0);
            const replies = hourly.reduce((sum, hour) => sum + (Number(hour.replies) || 0), 0);
            const busiest = hourly.reduce(
                (best, hour) => (Number(hour.received) || 0) > (Number(best.received) || 0) ? hour : best,
                hourly[0],
            );
            summary.textContent = total || replies
                ? `收到 ${UI.formatNumber(total)} · Bot发送 ${UI.formatNumber(replies)}${total ? ` · 消息峰值 ${busiest.at.slice(11)}:00` : ''}`
                : '最近 24 小时没有消息';
        }
        container.removeAttribute('data-stale');
        const selectedIndex = hourly.findIndex(hour => hour.at === selectedHour);
        if (selectedIndex >= 0) this.showHourTip(selectedIndex, { pinned });
    },

    /* 柱图读数：桌面悬浮、手机点按（按横向位置吸附到最近一小时）、
       键盘左右键切换。原生 title 在手机上没有提示且延迟 1 秒，所以改成
       图表内的浮层，数据也顺带进了读屏（role="status"）。 */
    bindHourly() {
        const container = document.getElementById('dashboardHourlyChart');
        if (!container || this.state.hourlyBound) return;
        this.state.hourlyBound = true;

        const indexAt = event => {
            const bars = document.getElementById('dashboardHourlyBars');
            const total = (this.state.hourlyData || []).length;
            if (!bars || !total) return null;
            const rect = bars.getBoundingClientRect();
            if (!rect.width) return null;
            const ratio = (event.clientX - rect.left) / rect.width;
            return Math.max(0, Math.min(total - 1, Math.floor(ratio * total)));
        };

        container.addEventListener('mousemove', event => {
            if (event.target.closest('#dashboardHourlyTip')) return;
            if (this.state.hourlyPinned) return;
            const index = indexAt(event);
            if (index !== null && index !== this.state.hourlyIndex) this.showHourTip(index);
        });
        container.addEventListener('mouseleave', () => {
            if (!this.state.hourlyPinned) this.hideHourTip();
        });
        // 手指点按落点误差远大于一根柱子（手机上一根约 10px），
        // 所以用横向位置判定最近的一小时，而不是要求点中柱子本身。
        container.addEventListener('click', event => {
            if (event.target.closest('#dashboardHourlyTip')) return;
            const index = indexAt(event);
            if (index === null) return;
            if (this.state.hourlyPinned && index === this.state.hourlyIndex) this.hideHourTip();
            else this.showHourTip(index, { pinned: true });
        });
        container.addEventListener('focus', () => {
            const total = (this.state.hourlyData || []).length;
            if (total) this.showHourTip(this.state.hourlyIndex ?? total - 1);
        });
        container.addEventListener('blur', () => {
            if (!this.state.hourlyPinned) this.hideHourTip();
        });
        container.addEventListener('keydown', event => {
            const total = (this.state.hourlyData || []).length;
            if (!total) return;
            const step = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
            let index = this.state.hourlyIndex;
            if (step) index = (index === null ? total - 1 : index) + step;
            else if (event.key === 'Home') index = 0;
            else if (event.key === 'End') index = total - 1;
            else return;
            event.preventDefault();
            this.showHourTip(Math.max(0, Math.min(total - 1, index)), { pinned: true });
        });
        document.addEventListener('click', event => {
            if (this.state.hourlyPinned && !container.contains(event.target)) this.hideHourTip();
        });
    },

    showHourTip(index, { pinned = false } = {}) {
        const hour = (this.state.hourlyData || [])[index];
        const container = document.getElementById('dashboardHourlyChart');
        const tip = document.getElementById('dashboardHourlyTip');
        const cell = container?.querySelector(`.dashboard-hour[data-hour-index="${index}"]`);
        if (!hour || !tip || !cell || !container) return;
        const received = Math.max(0, Number(hour.received) || 0);
        const chatReplies = hour.chat_replies != null && Number.isFinite(Number(hour.chat_replies))
            ? Math.max(0, Number(hour.chat_replies)) : null;
        // Hour buckets are server-local China time, independent of the browser's timezone.
        const start = new Date(`${hour.at}:00:00+08:00`);
        const end = new Date(start.getTime() + 60 * 60 * 1000);
        const partial = String(this.state.hourlyGeneratedAt || '').slice(0, 13) === hour.at;
        const plugins = (Array.isArray(hour.plugin_triggers) ? hour.plugin_triggers : [])
            .filter(plugin => Number(plugin.count) > 0)
            .sort((a, b) => Number(b.count) - Number(a.count));
        const pluginReadout = plugins.map(plugin =>
            `<dt>${UI.escapeHtml(plugin.name || plugin.plugin_id)}：</dt><dd>${UI.formatNumber(plugin.count)}<small>次</small></dd>`).join('');
        tip.innerHTML = `
            <strong>${UI.escapeHtml(`${UI.formatShortDateTime(start)} – ${UI.formatShortDateTime(end)}`)}</strong>
            <dl class="dashboard-hour-details">
                <dt>消息数：</dt><dd>${UI.formatNumber(received)}<small>条</small></dd>
                <dt>聊天回复：</dt><dd>${chatReplies === null ? '—' : UI.formatNumber(chatReplies)}<small>条</small></dd>
                ${pluginReadout}
            </dl>
            <small class="dashboard-hour-note">${hour.plugin_triggers_available === false ? '插件统计暂不可用' : '插件按处理轮次统计'}${partial ? ' · 截至当前' : ''}</small>`;
        tip.hidden = false;
        const half = tip.offsetWidth / 2 + 4;
        const center = cell.offsetLeft + cell.offsetWidth / 2;
        tip.style.left = `${Math.min(Math.max(center, half), container.clientWidth - half)}px`;
        container.querySelectorAll('.dashboard-hour.is-active')
            .forEach(node => node.classList.remove('is-active'));
        cell.classList.add('is-active');
        this.state.hourlyIndex = index;
        this.state.hourlyPinned = !!pinned;
    },

    hideHourTip() {
        const tip = document.getElementById('dashboardHourlyTip');
        if (tip) {
            tip.hidden = true;
            tip.removeAttribute('style');
        }
        document.querySelectorAll('#dashboardHourlyChart .dashboard-hour.is-active')
            .forEach(node => node.classList.remove('is-active'));
        this.state.hourlyIndex = null;
        this.state.hourlyPinned = false;
    },

    /* ===== 用量与聊天 ===== */

    renderUsageSummary(usageToday) {
        const container = document.getElementById('dashboardUsageSummary');
        if (!container) return;
        const metrics = usageToday?.data?.totals;
        if (!metrics) {
            container.dataset.stale = 'true';
            container.innerHTML = '<div class="dashboard-empty">用量统计暂不可用。</div>';
            return;
        }
        const calls = Number(metrics.calls || 0);
        const failures = Number(metrics.failures || 0);
        const durationCalls = Number(metrics.duration_calls || 0);
        const averageDuration = durationCalls ? Number(metrics.duration_total || 0) / durationCalls : 0;
        const models = Object.entries(metrics.models || {})
            .sort((a, b) => Number(b[1]) - Number(a[1]))
            .slice(0, 5);
        const modelTotal = models.reduce((sum, [, count]) => sum + (Number(count) || 0), 0);

        container.innerHTML = `
            <div class="dashboard-usage-grid">
                <div><span>Token</span><strong>${this.formatTokens(metrics.tokens)}</strong></div>
                <div><span>调用</span><strong>${UI.formatNumber(calls)}</strong></div>
                <div><span>失败</span><strong class="${failures ? 'is-error' : ''}">${UI.formatNumber(failures)}</strong></div>
                <div><span>平均耗时</span><strong>${averageDuration ? `${averageDuration.toFixed(1)}s` : '—'}</strong></div>
            </div>
            ${models.length ? `<div class="dashboard-model-heading">调用最多的模型</div><div class="dashboard-model-matrix">${models.map(([model, count]) => {
                const share = modelTotal ? (Number(count) || 0) / modelTotal : 0;
                return `
                <div class="dashboard-model-row" title="${UI.escapeHtml(model)} · ${UI.formatNumber(count)} 次调用">
                    <span class="dashboard-model-name">${UI.escapeHtml(model)}</span>
                    <span class="dashboard-model-dots" aria-hidden="true">${Array.from({ length: 8 }, (_, index) =>
                        `<i${index < Math.round(share * 8) ? ' class="is-on"' : ''}></i>`).join('')}</span>
                    <em>${UI.formatNumber(count)}</em>
                    <b>${Math.round(share * 100)}%</b>
                </div>`;
            }).join('')}</div>` : ''}
        `;
        container.removeAttribute('data-stale');
    },

    renderTopChats(timeseries) {
        const container = document.getElementById('dashboardTopChats');
        if (!container) return;
        const chats = timeseries?.top_chats || [];
        if (!timeseries) {
            container.dataset.stale = 'true';
            container.innerHTML = '<div class="dashboard-empty">聊天活动暂不可用。</div>';
            return;
        }
        if (!chats.length) {
            container.innerHTML = '<div class="dashboard-empty">今天还没有聊天活动。</div>';
            return;
        }
        container.innerHTML = `
            <table class="dashboard-chat-table">
                <caption class="visually-hidden">今日活跃聊天的收到消息与 Bot发送数量</caption>
                <colgroup><col><col class="dashboard-chat-number"><col class="dashboard-chat-number"></colgroup>
                <thead><tr><th scope="col">聊天</th><th scope="col">收到消息</th><th scope="col">Bot发送</th></tr></thead>
                <tbody>${chats.map(chat => `<tr>
                    <th scope="row" title="${UI.escapeHtml(chat.chat_name || '')}">${UI.escapeHtml(chat.chat_name || '未知聊天')}</th>
                    <td>${UI.formatNumber(Number(chat.received) || 0)}</td>
                    <td>${UI.formatNumber(Number(chat.replies) || 0)}</td>
                </tr>`).join('')}</tbody>
            </table>
        `;
        container.removeAttribute('data-stale');
    },

    /* ===== 事件时间线 ===== */

    renderTimeline(payload, stale) {
        const container = document.getElementById('dashboardTimeline');
        const filters = document.getElementById('dashboardTimelineFilters');
        if (!container) return;

        if (stale || !payload) {
            this.state.timelineItems = [];
            this.state.timelineKinds = [];
            container.dataset.stale = 'true';
            container.innerHTML = '<div class="dashboard-empty">事件流暂不可用，稍后会自动重试。</div>';
            if (filters) filters.innerHTML = '';
            return;
        }

        this.state.timelineItems = payload.items || [];
        this.state.timelineKinds = payload.kinds || [];
        this.renderTimelineFilters();
        this.renderTimelineItems();
        container.removeAttribute('data-stale');
    },

    renderTimelineFilters() {
        const filters = document.getElementById('dashboardTimelineFilters');
        if (!filters) return;
        const labels = {
            judge: '判决', task: '任务', plugin: '插件',
            system: '系统', codex: 'Codex', llm: '模型调用',
        };
        const chips = [
            { id: 'all', label: '全部' },
            { id: 'issues', label: '异常' },
            ...this.state.timelineKinds.map(kind => ({ id: kind, label: labels[kind] || kind })),
        ];
        if (chips.length <= 2 && !this.state.timelineKinds.length) {
            filters.innerHTML = '';
            return;
        }
        filters.innerHTML = chips.map(chip => `
            <button type="button" class="dashboard-filter${this.state.timelineFilter === chip.id ? ' is-active' : ''}"
                onclick="Dashboard.setTimelineFilter('${chip.id}')">${UI.escapeHtml(chip.label)}</button>
        `).join('');
    },

    setTimelineFilter(filter) {
        this.state.timelineFilter = filter;
        this.renderTimelineFilters();
        this.renderTimelineItems();
    },

    renderTimelineItems() {
        const container = document.getElementById('dashboardTimeline');
        if (!container) return;
        const filter = this.state.timelineFilter;
        const items = this.state.timelineItems.filter(item => {
            if (filter === 'all') return true;
            if (filter === 'issues') return ['error', 'warning'].includes(item.level);
            return item.kind === filter;
        });
        this.state.timelineVisible = items;

        if (!items.length) {
            container.innerHTML = `<div class="dashboard-empty">${filter === 'issues'
                ? '近期没有需要关注的异常事件。'
                : '这个分类下暂时没有事件。'}</div>`;
            return;
        }

        const icons = {
            error: 'bi-x-octagon', warning: 'bi-exclamation-triangle',
            success: 'bi-check-circle', info: 'bi-info-circle',
        };
        container.innerHTML = `<ul class="dashboard-timeline-list">${items.map((item, index) => `
            <li class="dashboard-timeline-item is-${item.level || 'info'}">
                <span class="dashboard-timeline-time">${UI.escapeHtml((item.at || '').slice(11, 16))}</span>
                <i class="bi ${icons[item.level] || icons.info}" aria-hidden="true"></i>
                <div class="dashboard-timeline-copy">
                    <strong>${UI.escapeHtml(item.title || '')}${item.chat_name
                        ? `<span class="dashboard-timeline-chat">${UI.escapeHtml(item.chat_name)}</span>` : ''}</strong>
                    ${item.detail ? `<p>${UI.escapeHtml(item.detail)}</p>` : ''}
                </div>
                ${item.action && item.action.tab
                    ? `<button type="button" class="dashboard-text-action" onclick="Dashboard.openEvent(${index})">${UI.escapeHtml(item.action.label || '查看')}</button>`
                    : ''}
            </li>`).join('')}</ul>`;
    },

    openEvent(index) {
        const item = this.state.timelineVisible[index];
        if (item?.action) this.navigate(item.action);
    },

    /* ===== Codex 额度（随概览一并呈现） ===== */

    localizeCodexQuotaMessage(message) {
        const text = String(message || '');
        const exactLabels = {
            'Codex runtime did not return account rate limits': 'Codex 运行时未返回账户限额',
            'Latest rollout file does not contain rate_limits yet': '最新 rollout 文件尚未包含 rate_limits',
            'Read from latest Codex rollout rate_limits': '已从最新 Codex rollout 文件读取 rate_limits',
            'Live refresh failed; showing last successful live usage': '实时刷新失败，正在显示最近一次成功获取的实时用量',
            'Live refresh failed; showing cached rollout data': '实时刷新失败，正在显示缓存的 rollout 数据',
            'Failed to fetch': '网络请求失败',
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

        // Discard out-of-order responses, including an old account refresh that
        // completes after the default configuration has changed.
        const incomingTime = Date.parse(data?.updated_at || '') || 0;
        const savedTime = Date.parse(this.state.lastCodexStatus?.updated_at || '') || 0;
        const newerUsageForSameContext = data?.context_key
            && data.context_key === this.state.lastCodexStatus?.context_key
            && data?.quota_supported && data?.quota_available
            && (Date.parse(data.rate_limit_updated_at || '') || 0)
                > (Date.parse(this.state.lastCodexStatus?.rate_limit_updated_at || '') || 0);
        if (incomingTime && savedTime > incomingTime) {
            if (!newerUsageForSameContext) return;
            data = { ...data, updated_at: this.state.lastCodexStatus.updated_at };
        }
        this.state.lastCodexStatus = data;

        const configured = !!data?.profile_available;
        const quotaSupported = !!data?.quota_supported;
        const quotaAvailable = quotaSupported && !!data?.quota_available;
        const snapshot = !!data?.served_from_snapshot;
        const statusClass = !configured || data?.status === 'error' ? 'is-danger'
            : data?.status === 'warning' ? 'is-warning'
            : snapshot || !quotaAvailable ? 'is-muted' : 'is-ok';
        const statusText = data?.status === 'error' ? '状态暂不可用'
            : !configured ? (data?.profile_id ? '配置待检查' : '未配置')
            : !quotaSupported ? (data?.status === 'warning' ? '配置待检查' : '已配置 · 额度未接入')
            : data?.status === 'warning' ? '额度暂不可用'
            : quotaAvailable ? (snapshot ? '账户额度快照' : '账户额度') : '额度待刷新';
        const updatedAt = data?.rate_limit_updated_at ? UI.formatShortDateTime(data.rate_limit_updated_at) : '';
        container.title = updatedAt ? `额度读取于 ${updatedAt}` : '尚无额度读取时间';
        container.tabIndex = 0;
        container.setAttribute('aria-label', container.title);
        const quotaMessage = this.localizeCodexQuotaMessage(data?.quota_message || '配置状态暂不可用。');
        const bothLimits = data?.rate_limits?.primary && data?.rate_limits?.secondary;
        const primaryLimit = quotaAvailable ? this.renderCodexLimit(data?.rate_limits?.primary, bothLimits ? '主要限额' : '', snapshot) : '';
        const secondaryLimit = quotaAvailable ? this.renderCodexLimit(data?.rate_limits?.secondary, bothLimits ? '次要限额' : '', snapshot) : '';

        const limits = primaryLimit || secondaryLimit;
        const abnormal = data?.status === 'error' || data?.status === 'warning';
        container.innerHTML = `
            ${limits ? `<div class="dashboard-codex-limits">${primaryLimit}${secondaryLimit}</div>` : ''}
            ${!limits || abnormal ? `<span class="dashboard-inline-state ${statusClass}" title="${UI.escapeHtml(quotaMessage)}">${UI.escapeHtml(statusText)}</span>` : ''}
        `;
        container.removeAttribute('data-stale');
    },

    renderCodexLimit(limit, label, snapshot = false) {
        if (!limit || typeof limit.used_percent !== 'number') return '';

        const used = Math.max(0, Math.min(100, limit.used_percent));
        const rawRemaining = typeof limit.remaining_percent === 'number' ? limit.remaining_percent : 100 - used;
        const remaining = Math.max(0, Math.min(100, rawRemaining));
        const tone = remaining <= 10 ? 'is-danger' : remaining <= 30 ? 'is-warning' : 'is-ok';
        const resetTime = limit.resets_at ? new Date(limit.resets_at * 1000) : null;
        // 已经过期的重置时间不再展示：旧读数会同时误导"额度"和"重置"两件事。
        const resetText = resetTime && !Number.isNaN(resetTime.getTime()) && resetTime.getTime() > Date.now()
            ? `${UI.formatShortDateTime(resetTime)} 重置` : '';

        return `
            <article class="dashboard-codex-limit ${tone}">
                ${label ? `<small>${UI.escapeHtml(label)}</small>` : '<small></small>'}
                <strong>${remaining.toFixed(remaining % 1 === 0 ? 0 : 1)}%</strong>
                <small class="dashboard-codex-remaining">${snapshot ? '上次读取剩余' : '剩余'}</small>
                <em>${resetText ? UI.escapeHtml(resetText) : ''}</em>
            </article>
        `;
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
                    <div class="dashboard-empty">
                        <span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>正在刷新 Codex 用量…
                    </div>
                `;
            }
            const data = await API.dashboard.refreshCodexStatus();
            this.renderCodexStatus(data);
        } catch (error) {
            console.error('Failed to refresh Codex usage:', error);
            this.renderCodexStatus({
                status: 'error',
                logged_in: false,
                quota_available: false,
                quota_message: error.message || '刷新 Codex 用量失败',
                updated_at: new Date().toISOString(),
            });
        } finally {
            if (button) {
                button.disabled = false;
                button.innerHTML = '<i class="bi bi-arrow-clockwise" aria-hidden="true"></i>';
            }
        }
    },
};
