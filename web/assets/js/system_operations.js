/** System operations, plugin runtime and backup/migration console. */
const SystemOperations = {
    restoreSelection: null,
    pollingOperations: new Set(),
    runtimeRequestId: 0,
    backupRequestId: 0,

    esc(value) {
        return UI.escapeHtml(String(value ?? ''));
    },

    formatBytes(value) {
        const bytes = Number(value || 0);
        if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
        return `${(bytes / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
    },

    formatTime(value) {
        if (!value) return '-';
        const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
        return Number.isNaN(date.getTime()) ? '-' : UI.formatDateTime(date);
    },

    statusLabel(status) {
        return ({
            queued: '等待', running: '运行中', cancelling: '取消中', completed: '完成',
            failed: '失败', cancelled: '已取消', interrupted: '已中断',
            unknown: '未检测', healthy: '正常', degraded: '降级', unhealthy: '异常', stopped: '已停止'
        })[status] || status || '-';
    },

    statusClass(status) {
        if (['completed', 'healthy', 'ready', 'live'].includes(status)) return 'success';
        if (['failed', 'unhealthy', 'not_ready'].includes(status)) return 'danger';
        if (['running', 'queued'].includes(status)) return 'primary';
        return 'warning';
    },

    async loadRuntime() {
        const container = document.getElementById('systemOperationsConsole');
        if (!container) return;
        const requestId = ++this.runtimeRequestId;
        if (container.dataset.ready !== 'true') {
            container.innerHTML = '<div class="loading-wrapper">正在读取运行状态…</div>';
        } else {
            container.setAttribute('aria-busy', 'true');
        }
        const [healthResult, operationsResult, runtimeResult, incidentsResult, auditResult] = await Promise.allSettled([
            API.get('/api/system/health/details'),
            API.operations.getAll(80),
            API.operations.getRuntime(),
            API.operations.getIncidents(40),
            API.operations.getAudit(40)
        ]);
        const health = healthResult.status === 'fulfilled' ? healthResult.value : null;
        const operations = operationsResult.status === 'fulfilled' ? operationsResult.value : { operations: [], stats: {} };
        const runtime = runtimeResult.status === 'fulfilled' ? runtimeResult.value : { plugins: [], summary: {} };
        const incidents = incidentsResult.status === 'fulfilled' ? incidentsResult.value : { incidents: [], summary: {} };
        const audit = auditResult.status === 'fulfilled' ? auditResult.value : { records: [] };
        const checks = health?.checks || {};
        const failedSources = [
            ['服务健康', healthResult], ['后台任务', operationsResult], ['插件状态', runtimeResult],
            ['最近问题', incidentsResult], ['变更记录', auditResult]
        ].filter(([, result]) => result.status === 'rejected').map(([label]) => label);
        const loadError = '<div class="system-empty-row text-warning">读取失败，请点击上方刷新重试。</div>';
        const recent = operations.operations || [];
        const plugins = runtime.plugins || [];
        const active = recent.filter(item => ['queued', 'running', 'cancelling'].includes(item.status));
        const incidentItems = incidents.incidents || [];
        const auditItems = audit.records || [];
        const unhealthyPlugins = plugins.filter(item => ['unhealthy', 'failed', 'degraded'].includes(item.health?.status)).length;

        if (requestId !== this.runtimeRequestId || !document.body.contains(container)) return;

        container.innerHTML = `
            <div class="system-platform-heading">
                <div><h3>运行状态</h3><p>集中查看服务健康、插件和后台任务</p></div>
                <button class="btn btn-sm btn-light border" onclick="SystemOperations.loadRuntime()"><i class="bi bi-arrow-clockwise me-1"></i>刷新</button>
            </div>
            ${failedSources.length ? `<div class="system-empty-row text-warning" role="alert">${this.esc(failedSources.join('、'))}读取失败，以下未获取的数据不代表正常。</div>` : ''}
            <div class="system-runtime-strip">
                ${Object.entries({ 数据库: checks.database, 事件总线: checks.event_bus, 插件: checks.plugin_manager, 微信: checks.wechat }).map(([label, ok]) => `
                    <div><span>${this.esc(label)}</span><strong class="text-${ok === true ? 'success' : 'warning'}">${ok === true ? '正常' : ok === false ? '未就绪' : '未获取'}</strong></div>`).join('')}
                <div><span>后台任务</span><strong>${operationsResult.status === 'fulfilled' ? active.length : '未获取'}</strong></div>
            </div>
            <div class="system-fold-toolbar"><span>按需展开诊断信息</span><a class="btn btn-sm btn-light border" href="/plugins">管理插件</a><a class="btn btn-sm btn-light border" href="/operations/logs">查看日志</a></div>
            ${Number(health?.models?.open_circuits || 0) ? `<div class="system-empty-row text-warning">${Number(health.models.open_circuits)} 个模型因连续失败暂停调用。<a href="/ai/models">检查模型连接</a></div>` : ''}
            <details class="system-platform-block system-fold" ${unhealthyPlugins ? 'open' : ''}>
                <summary class="system-platform-block-head"><div><h4>插件状态</h4><p>任务、数据、健康检查和资源清理由系统统一管理</p></div><div class="system-fold-meta"><span class="${unhealthyPlugins ? 'text-danger' : ''}">${runtimeResult.status === 'rejected' ? '读取失败' : unhealthyPlugins ? `${unhealthyPlugins} 个异常` : `${Number(runtime.summary?.total || 0)} 个插件`}</span><i class="bi bi-chevron-down"></i></div></summary>
                <div class="table-responsive"><table class="table system-compact-table system-plugins-table align-middle mb-0">
                    <thead><tr><th>插件</th><th>健康</th><th>后台任务</th><th>数据占用</th></tr></thead>
                    <tbody>${runtimeResult.status === 'rejected' ? '<tr><td colspan="4" class="text-warning">插件状态读取失败，请刷新重试。</td></tr>' : plugins.length ? plugins.map(item => {
                        const storageBytes = (item.storage?.entries || []).reduce((sum, entry) => sum + Number(entry.bytes || 0), 0);
                        const healthState = item.health?.status || 'unknown';
                        return `<tr><td data-label="插件"><strong>${this.esc(item.plugin_id)}</strong><small>已接入统一管理</small></td>
                            <td data-label="健康"><span class="system-state-pill ${this.statusClass(healthState)}">${this.esc(this.statusLabel(healthState))}</span><small>${this.esc(item.health?.message || '')}</small></td>
                            <td data-label="后台任务">${Number(item.active_tasks || 0)}</td><td data-label="数据占用">${this.formatBytes(storageBytes)}</td></tr>`;
                    }).join('') : '<tr><td colspan="4" class="text-muted py-4 text-center">暂无插件状态数据</td></tr>'}</tbody>
                </table></div>
            </details>
            <details class="system-platform-block system-fold" ${active.length ? 'open' : ''}>
                <summary class="system-platform-block-head"><div><h4>后台任务</h4><p>查看正在执行、等待、完成或失败的操作</p></div><div class="system-fold-meta"><span class="${active.length ? 'text-primary' : ''}">${operationsResult.status === 'rejected' ? '读取失败' : active.length ? `${active.length} 项执行中` : `${recent.length} 条记录`}</span><i class="bi bi-chevron-down"></i></div></summary>
                ${operationsResult.status === 'rejected' ? loadError : this.renderOperationsTable([...active, ...recent.filter(item => !active.includes(item))])}
            </details>
            <details class="system-platform-block system-fold">
                <summary class="system-platform-block-head"><div><h4>最近问题</h4><p>相同错误会合并显示，详细信息仍保留在运行日志</p></div><div class="system-fold-meta"><span class="${incidentItems.length ? 'text-warning' : ''}">${incidentsResult.status === 'rejected' ? '读取失败' : incidentItems.length ? `${incidentItems.length} 组` : '暂无问题'}</span><i class="bi bi-chevron-down"></i></div></summary>
                <div class="system-fold-toolbar"><span>按错误指纹聚合的最近记录</span><a class="btn btn-sm btn-light border" href="/operations/logs" onclick="event.preventDefault(); UI.switchTab('logs')">查看日志</a></div>
                ${incidentsResult.status === 'rejected' ? loadError : this.renderIncidents(incidentItems)}
            </details>
            <details class="system-platform-block system-fold">
                <summary class="system-platform-block-head"><div><h4>变更记录</h4><p>敏感配置仅记录已配置状态，不保存密钥值</p></div><div class="system-fold-meta"><span>${auditResult.status === 'rejected' ? '读取失败' : `${auditItems.length} 条`}</span><i class="bi bi-chevron-down"></i></div></summary>
                ${auditResult.status === 'rejected' ? loadError : this.renderAudit(auditItems)}
            </details>`;
        container.dataset.ready = 'true';
        container.removeAttribute('aria-busy');
    },

    renderIncidents(items) {
        if (!items.length) return '<div class="system-empty-row">最近日志中没有需要关注的警告或错误。</div>';
        return `<div class="table-responsive"><table class="table system-compact-table system-incidents-table align-middle mb-0">
            <thead><tr><th>事件</th><th>组件</th><th>级别</th><th>次数</th><th>最后发生</th></tr></thead>
            <tbody>${items.map(item => `<tr><td data-label="事件"><strong>${this.esc(item.message)}</strong></td>
                <td data-label="组件"><code>${this.esc(item.component)}</code></td>
                <td data-label="级别"><span class="system-state-pill ${item.level === 'WARNING' ? 'warning' : 'danger'}">${this.esc(item.level)}</span></td>
                <td data-label="次数">${Number(item.count || 0)}</td><td data-label="最后发生">${this.esc(item.last_seen || '-')}</td></tr>`).join('')}</tbody>
        </table></div>`;
    },

    renderAudit(items) {
        if (!items.length) return '<div class="system-empty-row">暂无变更记录。</div>';
        return `<div class="table-responsive"><table class="table system-compact-table system-audit-table align-middle mb-0">
            <thead><tr><th>变更</th><th>对象</th><th>分类</th><th>结果</th><th>时间</th></tr></thead>
            <tbody>${items.map(item => `<tr><td data-label="变更"><strong>${this.esc(item.summary)}</strong><small>${this.esc(item.action)}</small></td>
                <td data-label="对象"><code>${this.esc(item.target)}</code></td><td data-label="分类">${this.esc(item.category)}</td>
                <td data-label="结果"><span class="system-state-pill ${item.status === 'success' ? 'success' : 'danger'}">${item.status === 'success' ? '成功' : '失败'}</span></td>
                <td data-label="时间">${this.formatTime(item.created_at)}</td></tr>`).join('')}</tbody></table></div>`;
    },

    renderOperationsTable(items) {
        if (!items.length) return '<div class="system-empty-row">暂无后台任务。</div>';
        return `<div class="table-responsive"><table class="table system-compact-table system-operations-table align-middle mb-0">
            <thead><tr><th>任务</th><th>所有者</th><th>状态</th><th>进度</th><th>更新时间</th><th></th></tr></thead>
            <tbody>${items.map(item => {
                const active = ['queued', 'running', 'cancelling'].includes(item.status);
                return `<tr><td data-label="任务"><strong>${this.esc(item.title)}</strong><small>${this.esc(item.message || item.kind)}</small></td>
                    <td data-label="所有者"><code>${this.esc(item.owner)}</code></td>
                    <td data-label="状态"><span class="system-state-pill ${this.statusClass(item.status)}">${this.esc(this.statusLabel(item.status))}</span></td>
                    <td data-label="进度"><div class="system-progress"><span style="width:${Math.max(0, Math.min(Number(item.progress || 0), 100))}%"></span></div><small>${Number(item.progress || 0)}%</small></td>
                    <td data-label="更新时间">${this.formatTime(item.updated_at)}</td>
                    <td data-label="操作" class="system-table-action-cell">${active ? `<button class="btn btn-sm btn-light border" onclick="SystemOperations.cancelOperation('${this.esc(item.operation_id)}')">取消</button>` : ''}</td></tr>`;
            }).join('')}</tbody></table></div>`;
    },

    async cancelOperation(operationId) {
        try {
            await API.operations.cancel(operationId);
            UI.showInfo('已提交取消请求');
            this.loadRuntime();
        } catch (error) {
            UI.showError(error.message);
        }
    },

    backupDraft: { profile: 'state', include_generated: true, include_diagnostics: false, include_models: false, include_machine_bound: false, include_codex_profiles: true },
    backupOverview: null,
    backupOperations: [],
    backupBusy: false,
    backupEditor: false,
    backupNotice: null,
    backupOptions: [
        ['backupCodexProfiles', 'include_codex_profiles', '独立 Codex Profile 与 Skill', '包含独立登录凭据；不包含全局账号、会话数据库和插件缓存。'],
        ['backupGenerated', 'include_generated', '生成内容', '包含报告、图片等生成文件，可能明显增加备份大小。'],
        ['backupDiagnostics', 'include_diagnostics', '调用诊断', '包含调用诊断记录，可能含请求和响应内容。'],
        ['backupModels', 'include_models', '本地模型', '包含本地模型文件，通常占用较多空间。'],
        ['backupMachineBound', 'include_machine_bound', '机器绑定数据', '包含浏览器配置等设备相关数据；换机后可能需要重新登录。']
    ],

    backupIsActive(operation) { return operation && ['queued', 'running', 'cancelling'].includes(operation.status); },

    async loadBackups() {
        const container = document.getElementById('systemBackupsConsole');
        if (!container) return;
        const requestId = ++this.backupRequestId;
        this.ensureBackupShell(container);
        container.setAttribute('aria-busy', 'true');
        try {
            const [overview, operations] = await Promise.all([API.backups.getOverview(), API.operations.getAll(80, 'system:backup')]);
            if (requestId !== this.backupRequestId || !document.body.contains(container)) return;
            if (this.backupLoadError) {
                this.backupLoadError = false;
                if (this.backupNotice?.message.startsWith('读取备份失败')) {
                    this.backupNotice = null;
                    document.getElementById('backupNotice').innerHTML = '';
                }
            }
            this.backupOverview = overview;
            this.backupOperations = operations.operations || [];
            this.renderBackupData();
            this.backupOperations.filter(item => this.backupIsActive(item)).forEach(item => this.watchBackupOperation(item));
        } catch (error) {
            if (requestId !== this.backupRequestId || !document.body.contains(container)) return;
            this.backupLoadError = true;
            this.showBackupNotice('danger', `读取备份失败：${error.message}。请点击刷新重试；当前显示的信息可能已过期。`);
        } finally {
            if (requestId === this.backupRequestId) container.removeAttribute('aria-busy');
        }
    },

    ensureBackupShell(container) {
        if (container.dataset.backupShell === 'true') return;
        if (!this.backupDraftLoaded) {
            this.backupDraftLoaded = true;
            try {
                const saved = JSON.parse(sessionStorage.getItem('mabobot.backupDraft') || '{}');
                if (['state', 'migration'].includes(saved.profile)) this.backupDraft.profile = saved.profile;
                this.backupOptions.forEach(([, key]) => {
                    if (typeof saved[key] === 'boolean') this.backupDraft[key] = saved[key];
                });
            } catch (_) { /* Unavailable storage must not prevent creating a backup. */ }
        }
        container.dataset.backupShell = 'true';
        container.innerHTML = `
            <header class="backup-toolbar"><div><h3>备份与恢复</h3><p>保存当前数据，或从已有备份恢复。</p></div>
                <div class="backup-actions"><button class="btn btn-primary" id="backupCreateEntry" data-backup-action data-action="open-create">创建备份</button>
                <button class="btn btn-light border" data-backup-action data-action="import">导入备份</button>
                <button class="btn btn-light border" data-action="refresh">刷新</button></div></header>
            <div id="backupNotice" aria-live="polite" tabindex="-1"></div>
            <div id="backupActiveOperation" aria-live="polite"></div>
            <div id="backupPendingRestore"></div>
            <div id="backupLastRestore"></div><div id="backupRecentTask"></div>
            <div id="backupCreatePanel"></div>
            <div id="backupRestorePanel"></div>
            <section class="backup-history-list" aria-label="备份记录"><div class="backup-list-heading"><h4>备份记录</h4><span id="backupCount">正在读取…</span></div><div id="backupRecords"></div></section>
            <input id="backupImportFile" class="d-none" type="file" accept=".zip,.mabobot-backup.zip">
            <p class="backup-footnote">备份未加密，包含配置密钥和聊天数据。请妥善保管下载的文件；导入只添加记录，不会立即覆盖当前数据。</p>`;
        container.onclick = event => {
            const button = event.target.closest('[data-action]');
            if (!button || button.disabled) return;
            const name = button.dataset.name;
            const actions = {
                'open-create': () => this.openBackupEditor(), 'close-create': () => this.closeBackupEditor(),
                create: () => this.createBackup(), refresh: () => this.loadBackups(),
                import: () => document.getElementById('backupImportFile').click(),
                restore: () => this.selectRestore(name), 'close-restore': () => this.selectRestore(null),
                validate: () => this.validateBackup(name), delete: () => this.deleteBackup(name),
                prepare: () => this.prepareRestore(), 'cancel-restore': () => this.cancelPendingRestore(),
                restart: () => this.restartForRestore()
            };
            actions[button.dataset.action]?.();
        };
        container.onchange = event => {
            if (event.target.id === 'backupImportFile') return this.importBackup(event.target);
            if (event.target.id === 'backupProfile') this.backupDraft.profile = event.target.value;
            const option = this.backupOptions.find(([id]) => id === event.target.id);
            if (option) this.backupDraft[option[1]] = event.target.checked;
            try { sessionStorage.setItem('mabobot.backupDraft', JSON.stringify(this.backupDraft)); } catch (_) { /* Keep the in-memory draft. */ }
            this.updateBackupProfileCopy();
        };
        if (this.backupEditor) this.renderBackupEditor();
        if (this.restoreSelection) document.getElementById('backupRestorePanel').innerHTML = this.renderRestorePanel();
        if (this.backupNotice) this.showBackupNotice(this.backupNotice.tone, this.backupNotice.message);
    },

    showBackupNotice(tone, message) {
        this.backupNotice = { tone, message };
        const slot = document.getElementById('backupNotice');
        if (slot) slot.innerHTML = `<div class="backup-message text-${tone}" role="status">${this.esc(message)}</div>`;
    },

    renderBackupData() {
        const overview = this.backupOverview;
        if (!overview || !document.getElementById('backupRecords')) return;
        const backups = overview.backups || [];
        const pending = overview.pending_restore;
        document.getElementById('backupCount').textContent = `${UI.formatNumber(backups.length)} 个备份 · 恢复前会自动执行完整校验`;
        document.getElementById('backupRecords').innerHTML = this.renderBackupsTable(backups, pending?.archive_name);
        document.getElementById('backupPendingRestore').innerHTML = pending ? `
            <section class="backup-message backup-pending" aria-label="待执行的恢复计划"><h4>${pending.invalid ? '恢复计划无法读取' : '恢复计划已准备，尚未应用'}</h4>
            <p>${this.esc(pending.archive_name || '计划文件异常，请取消后重新选择备份。')}</p>
            <p>下次启动全部服务时将应用此计划。只关闭页面或取消重启不会撤销恢复。</p>
            <div class="backup-actions">${pending.invalid ? '' : '<button class="btn btn-danger" data-backup-action data-action="restart">重启全部服务并恢复</button>'}
            <button class="btn btn-light border" data-backup-action data-action="cancel-restore">取消恢复计划</button></div></section>` : '';
        const last = overview.last_restore;
        document.getElementById('backupLastRestore').innerHTML = last ? `<details class="backup-result"><summary>上次恢复${last.status === 'completed' ? '已完成' : '失败'} · ${this.formatTime(last.time)}</summary><p>${this.esc(last.archive_name || '')}</p><p>${this.esc(last.error || '数据已应用。请检查服务状态和关键配置。')}</p>${last.safety_backup ? `<p>恢复前快照：${this.esc(last.safety_backup)}</p>` : ''}</details>` : '';
        const recent = this.backupOperations.find(item => !this.backupIsActive(item));
        document.getElementById('backupRecentTask').innerHTML = recent ? `<details class="backup-result"><summary>最近任务：${this.esc(recent.title)} · ${this.esc(recent.kind === 'backup_validate' && recent.status === 'completed' && !recent.result?.valid ? '校验失败' : this.statusLabel(recent.status))}</summary><p>${this.formatTime(recent.updated_at || recent.finished_at || recent.created_at)}</p><p>${this.esc(recent.error || recent.result?.errors?.[0] || recent.message || '任务已结束')}</p></details>` : '';
        this.updateBackupOperation();
    },

    updateBackupOperation(operation) {
        if (operation?.operation_id) {
            this.backupOperations = [operation, ...this.backupOperations.filter(item => item.operation_id !== operation.operation_id)];
        }
        const active = this.backupOperations.filter(item => this.backupIsActive(item));
        const slot = document.getElementById('backupActiveOperation');
        if (slot) slot.innerHTML = active.map(item => this.renderBackupOperation(item)).join('');
        const busy = this.backupBusy || active.length > 0;
        document.querySelectorAll('#systemBackupsConsole [data-backup-action]').forEach(button => {
            button.disabled = busy || button.dataset.backupLocked === 'true' || (button.dataset.action === 'prepare' && !!this.backupOverview?.pending_restore);
        });
        const entry = document.getElementById('backupCreateEntry');
        if (entry) entry.classList.toggle('btn-primary', !this.backupEditor && !this.restoreSelection && !this.backupOverview?.pending_restore);
        if (entry) entry.classList.toggle('btn-light', this.backupEditor || !!this.restoreSelection || !!this.backupOverview?.pending_restore);
    },

    renderBackupOperation(operation) {
        const progress = Math.max(0, Math.min(Number(operation.progress || 0), 100));
        return `<div class="backup-active-operation"><div><strong>${this.esc(operation.title)}</strong><span>${this.esc(operation.message || '等待执行')}</span></div><div><progress max="100" value="${progress}" aria-label="任务进度"></progress><span>${progress}%</span></div></div>`;
    },

    openBackupEditor() {
        this.selectRestore(null, false);
        this.backupEditor = true;
        this.renderBackupEditor();
        this.updateBackupOperation();
        this.focusBackupPanel('backupCreatePanel', '#backupProfile');
    },

    closeBackupEditor() {
        this.backupEditor = false;
        document.getElementById('backupCreatePanel').innerHTML = '';
        this.updateBackupOperation();
        document.getElementById('backupCreateEntry')?.focus();
    },

    renderBackupEditor() {
        const draft = this.backupDraft;
        document.getElementById('backupCreatePanel').innerHTML = `<section class="backup-editor" aria-labelledby="backupEditorTitle">
            <h4 id="backupEditorTitle">创建备份</h4>
            <label for="backupProfile">备份用途</label><select id="backupProfile" class="form-select"><option value="state" ${draft.profile === 'state' ? 'selected' : ''}>状态备份 · 日常保存与恢复数据</option><option value="migration" ${draft.profile === 'migration' ? 'selected' : ''}>完整迁移 · 搬到另一台机器</option></select>
            <p id="backupProfileCopy"></p>
            <p>始终包含：配置（含 .env 密钥）、数据库、聊天档案与附件、插件持久数据。</p>
            <fieldset class="backup-options-menu"><legend>选择附加内容</legend>${this.backupOptions.map(([id, key, title, help]) => `<label class="backup-option" for="${id}"><input class="form-check-input" id="${id}" type="checkbox" ${draft[key] ? 'checked' : ''}><span><strong>${title}</strong><small>${help}</small></span></label>`).join('')}</fieldset>
            <p class="backup-security-note">备份未加密。密钥、聊天数据和所选登录凭据会写入备份文件。</p>
            <div class="backup-actions"><button class="btn btn-primary" data-backup-action data-action="create">开始创建</button><button class="btn btn-light border" data-action="close-create">收起</button></div></section>`;
        this.updateBackupProfileCopy();
    },

    updateBackupProfileCopy() {
        const copy = document.getElementById('backupProfileCopy');
        if (!copy) return;
        const draft = this.backupDraft;
        copy.textContent = (draft.profile === 'migration' ? '额外包含当前项目代码；目标机器仍需安装 Python、微信及所需外部工具。' : '用于恢复本机数据，不包含项目代码。')
            + ` ${draft.include_codex_profiles ? '包含' : '不包含'}独立 Codex Profile 与 Skill。`;
    },

    renderBackupsTable(backups, pendingRestoreName = null) {
        if (!backups.length) return '<div class="backup-empty">尚无备份。点击“创建备份”保存当前数据，或“导入备份”添加已有文件。</div>';
        return `<div class="table-responsive"><table class="table system-compact-table backup-history-table system-backup-table align-middle mb-0"><thead><tr><th>备份与校验状态</th><th>类型</th><th>大小</th><th>创建时间</th><th>操作</th></tr></thead><tbody>${backups.map(item => {
            const pending = item.name === pendingRestoreName;
            const validation = this.backupOperations.find(op => op.kind === 'backup_validate' && op.details?.archive_name === item.name && !this.backupIsActive(op));
            const validationLabel = validation ? (validation.status === 'completed' && validation.result?.valid ? '最近完整校验通过' : `最近完整校验${this.statusLabel(validation.status === 'completed' ? 'failed' : validation.status)}`) : '尚无完整校验记录';
            const action = (type, label, locked = false, reason = '') => `<button class="btn btn-sm btn-light border" data-backup-action data-action="${type}" data-name="${this.esc(item.name)}" ${locked ? 'data-backup-locked="true" disabled' : ''} title="${this.esc(reason || label)}">${label}</button>`;
            return `<tr><td data-label="备份"><strong>${this.esc(item.name)}</strong><small>${item.imported ? '已导入' : '本机创建'} · v${this.esc(item.app_version || '未知')} · ${item.compatible === false ? '旧版格式 · 不兼容' : item.valid ? '结构检查通过（不等于完整校验）' : '结构异常'}${pending ? ' · 待恢复' : ''}</small><small>${this.esc(validationLabel)}</small>${item.error ? `<small class="text-danger">${this.esc(item.error)}</small>` : ''}${validation?.result?.errors?.length ? `<small class="text-danger">${this.esc(validation.result.errors[0])}</small>` : ''}</td>
                <td data-label="类型">${item.compatible === false ? '旧版备份' : item.profile === 'migration' ? '完整迁移' : '状态备份'}</td><td data-label="大小">${this.formatBytes(item.bytes)}<small>${UI.formatNumber(item.file_count || 0)} 个文件</small></td><td data-label="创建时间">${this.formatTime(item.created_at)}</td>
                <td data-label="操作" class="system-table-action-cell"><div class="backup-row-actions">${action('restore', '恢复', !item.valid || !!this.backupOverview?.pending_restore, !item.valid ? '此备份不可恢复，请查看结构检查说明' : pendingRestoreName ? '请先处理当前恢复计划' : '')}<a class="btn btn-sm btn-light border" href="${API.backups.downloadUrl(item.name)}" title="下载">下载</a>${action('validate', '完整校验', item.compatible === false)}${action('delete', '删除', pending, pending ? '请先取消此备份的恢复计划' : '永久删除')}</div></td></tr>`;
        }).join('')}</tbody></table></div>`;
    },

    renderRestorePanel() {
        if (!this.restoreSelection) return '';
        const item = this.backupOverview?.backups?.find(row => row.name === this.restoreSelection);
        return `<section class="backup-editor backup-restore-block" aria-labelledby="backupRestoreTitle"><h4 id="backupRestoreTitle" tabindex="-1">恢复备份</h4><p class="backup-filename">${this.esc(this.restoreSelection)}</p>
            <ol class="backup-steps"><li>确认影响并准备：自动校验完整性，创建恢复计划。</li><li>重启全部服务：Web 和微信 Bot 将短暂中断，先创建安全快照，再替换数据。</li><li>服务恢复后：查看恢复结果并检查配置。</li></ol>
            <p class="backup-security-note">${item?.profile === 'migration' ? '此迁移包将替换备份覆盖的项目代码、配置和数据。' : '备份覆盖的配置和数据将被替换。'}备份之后的相关改动可能丢失。恢复所选登录凭据可能改变当前账号。</p>
            <p>请仅恢复可信来源的备份。准备完成后可选择重启应用，或取消计划。</p>
            <label for="backupRestoreConfirmation">输入“恢复备份”确认上述影响</label><input id="backupRestoreConfirmation" class="form-control" autocomplete="off" aria-describedby="backupRestoreError"><p id="backupRestoreError" class="text-danger" role="alert"></p>
            <div class="backup-actions"><button class="btn btn-danger" data-backup-action data-action="prepare">校验并准备恢复</button><button class="btn btn-light border" data-action="close-restore">取消</button></div></section>`;
    },

    focusBackupPanel(id, selector) {
        const panel = document.getElementById(id);
        if (!panel) return;
        const target = panel.querySelector(selector);
        if (target && !target.hasAttribute('tabindex') && target.getAttribute('role') === 'status') target.tabIndex = -1;
        target?.focus({ preventScroll: true });
        UI.scrollIntoView(panel, { behavior: 'smooth', block: 'start' });
    },

    selectRestore(name, returnFocus = true) {
        const previous = this.restoreSelection;
        this.restoreSelection = name || null;
        if (name) {
            this.backupEditor = false;
            document.getElementById('backupCreatePanel').innerHTML = '';
        }
        const panel = document.getElementById('backupRestorePanel');
        if (panel) panel.innerHTML = this.renderRestorePanel();
        this.updateBackupOperation();
        if (name) this.focusBackupPanel('backupRestorePanel', '#backupRestoreTitle');
        else if (previous && returnFocus) {
            const button = [...document.querySelectorAll('[data-action="restore"]')].find(element => element.dataset.name === previous);
            (button || document.getElementById('backupCreateEntry'))?.focus();
        }
    },

    async runBackupRequest(action) {
        if (this.backupBusy || this.backupOperations.some(item => this.backupIsActive(item))) return;
        this.backupBusy = true;
        this.updateBackupOperation();
        try { await action(); }
        catch (error) {
            this.showBackupNotice('danger', `操作失败：${error.message}`);
            this.focusBackupPanel('backupNotice', '[role="status"]');
        }
        finally { this.backupBusy = false; this.updateBackupOperation(); }
    },

    watchBackupOperation(operation) {
        this.pollOperation(operation.operation_id, async result => {
            const valid = result.kind !== 'backup_validate' || result.result?.valid;
            const ok = result.status === 'completed' && valid;
            this.showBackupNotice(ok ? 'success' : 'danger', `${result.title || '任务'}：${ok ? '已完成' : this.statusLabel(result.status === 'completed' ? 'failed' : result.status)}${result.error || result.result?.errors?.[0] ? `。${result.error || result.result.errors[0]}` : ''}`);
            if (ok && result.kind === 'backup_prepare_restore') this.selectRestore(null, false);
            await this.loadBackups();
            if (ok && result.kind === 'backup_prepare_restore') this.focusBackupPanel('backupPendingRestore', 'button');
        }, result => this.updateBackupOperation(result), error => {
            this.showBackupNotice('danger', `任务状态暂时无法读取：${error.message}。任务可能仍在执行，请刷新重新连接。`);
        });
    },

    async createBackup() {
        await this.runBackupRequest(async () => {
            const response = await API.backups.create({ ...this.backupDraft });
            this.updateBackupOperation(response.operation);
            this.showBackupNotice('secondary', '备份正在创建。完成后可在记录中下载。');
            this.watchBackupOperation(response.operation);
        });
    },

    async importBackup(input) {
        const file = input.files?.[0];
        input.value = '';
        if (!file) return;
        if (!/\.mabobot-backup\.zip$/i.test(file.name)) {
            this.showBackupNotice('danger', '请选择 .mabobot-backup.zip 备份文件。旧版备份请用对应版本导出迁移。');
            return;
        }
        await this.runBackupRequest(async () => {
            this.showBackupNotice('secondary', `正在上传并检查 ${file.name}，请保持页面打开。`);
            const result = await API.backups.importFile(file, percent => {
                this.showBackupNotice('secondary', percent >= 100 ? '上传完成，正在检查备份结构…' : `正在上传 ${file.name}：${percent}%`);
            });
            this.showBackupNotice('success', `已导入 ${result.name}。当前数据未改变；如需恢复，请点击该记录的“恢复”。`);
            await this.loadBackups();
        });
    },

    async deleteBackup(name) {
        await this.runBackupRequest(async () => {
            if (!await UI.confirm(`永久删除备份“${name}”后无法找回。当前运行数据不受影响。`, { title: '删除备份', confirmText: '永久删除', variant: 'danger' })) return;
            const result = await API.backups.delete(name, '删除备份');
            if (this.restoreSelection === name) this.selectRestore(null);
            this.showBackupNotice('success', `已删除备份，释放 ${this.formatBytes(result.bytes)}。`);
            await this.loadBackups();
        });
    },

    async validateBackup(name) {
        await this.runBackupRequest(async () => {
            const response = await API.backups.validate(name);
            this.updateBackupOperation(response.operation);
            this.watchBackupOperation(response.operation);
        });
    },

    async prepareRestore() {
        const confirmation = document.getElementById('backupRestoreConfirmation')?.value.trim() || '';
        if (confirmation !== '恢复备份') {
            document.getElementById('backupRestoreError').textContent = '请输入“恢复备份”后继续。';
            document.getElementById('backupRestoreConfirmation').focus();
            return;
        }
        const name = this.restoreSelection;
        if (!name) return;
        await this.runBackupRequest(async () => {
            const response = await API.backups.prepareRestore(name, confirmation);
            this.updateBackupOperation(response.operation);
            this.watchBackupOperation(response.operation);
        });
    },

    async cancelPendingRestore() {
        await this.runBackupRequest(async () => {
            const name = this.backupOverview?.pending_restore?.archive_name || '';
            if (!await UI.confirm('取消后，下次启动将不再应用此恢复计划。备份文件和当前数据都会保留。', { title: '取消恢复计划', confirmText: '取消恢复计划' })) return;
            await API.backups.cancelRestore(name);
            this.showBackupNotice('success', '恢复计划已取消。备份文件和当前数据未改变。');
            await this.loadBackups();
        });
    },

    async restartForRestore() {
        await this.runBackupRequest(async () => {
            await App.restartManagedService('all', {
                confirmMessage: '重启全部服务后将应用待执行的恢复计划，备份覆盖的数据会被替换。Web 和微信 Bot 将短暂中断。',
                confirmTitle: '重启并恢复备份', overlayTitle: '正在重启并恢复', overlayMessage: '正在等待全部服务恢复…'
            });
        });
    },

    pollOperation(operationId, onFinished, onProgress = null, onError = null) {
        if (this.pollingOperations.has(operationId)) return;
        this.pollingOperations.add(operationId);
        const check = async () => {
            if (!this.pollingOperations.has(operationId)) return;
            try {
                const response = await API.operations.get(operationId);
                const operation = response.operation;
                if (onProgress) await onProgress(operation);
                if (['completed', 'failed', 'cancelled', 'interrupted'].includes(operation.status)) {
                    this.pollingOperations.delete(operationId);
                    if (onFinished) await onFinished(operation);
                    return;
                }
                setTimeout(check, 1200);
            } catch (error) {
                this.pollingOperations.delete(operationId);
                if (onError) onError(error);
                else UI.showError(`读取任务状态失败：${error.message}`);
            }
        };
        setTimeout(check, 700);
    }
};

window.SystemOperations = SystemOperations;
