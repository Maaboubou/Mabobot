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
            healthy: '正常', degraded: '降级', unhealthy: '异常', stopped: '已停止'
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
        const [healthResult, operationsResult, runtimeResult, incidentsResult, auditResult, storageResult] = await Promise.allSettled([
            API.get('/api/system/health/details'),
            API.operations.getAll(80),
            API.operations.getRuntime(),
            API.operations.getIncidents(40),
            API.operations.getAudit(40),
            API.operations.getStorage()
        ]);
        const health = healthResult.status === 'fulfilled' ? healthResult.value : null;
        const operations = operationsResult.status === 'fulfilled' ? operationsResult.value : { operations: [], stats: {} };
        const runtime = runtimeResult.status === 'fulfilled' ? runtimeResult.value : { plugins: [], summary: {} };
        const incidents = incidentsResult.status === 'fulfilled' ? incidentsResult.value : { incidents: [], summary: {} };
        const audit = auditResult.status === 'fulfilled' ? auditResult.value : { records: [] };
        const storage = storageResult.status === 'fulfilled' ? storageResult.value : { categories: [], scan_required: true };
        const checks = health?.checks || {};
        const recent = operations.operations || [];
        const plugins = runtime.plugins || [];
        const active = recent.filter(item => ['queued', 'running', 'cancelling'].includes(item.status));
        const storageScan = active.find(item => item.owner === 'system:storage' && item.kind === 'storage_scan');
        const incidentItems = incidents.incidents || [];
        const auditItems = audit.records || [];
        const unhealthyPlugins = Number(runtime.summary?.unhealthy || 0);

        if (requestId !== this.runtimeRequestId || !document.body.contains(container)) return;

        container.innerHTML = `
            <div class="system-platform-heading">
                <div><h3>运行状态</h3><p>集中查看服务健康、插件、后台任务和存储</p></div>
                <button class="btn btn-sm btn-light border" onclick="SystemOperations.loadRuntime()"><i class="bi bi-arrow-clockwise me-1"></i>刷新</button>
            </div>
            <div class="system-runtime-strip">
                ${Object.entries({ Web: true, 数据库: checks.database, 事件总线: checks.event_bus, 插件: checks.plugin_manager, 微信: checks.wechat }).map(([label, ok]) => `
                    <div><span>${this.esc(label)}</span><strong class="text-${ok ? 'success' : 'warning'}">${ok ? '正常' : '未就绪'}</strong></div>`).join('')}
                <div><span>后台任务</span><strong>${active.length}</strong></div>
                <div><span>模型熔断</span><strong class="text-${Number(health?.models?.open_circuits || 0) ? 'warning' : 'success'}">${Number(health?.models?.open_circuits || 0)}</strong></div>
                <div><span>已管理插件</span><strong>${Number(runtime.summary?.total || 0)}</strong></div>
            </div>
            <details class="system-platform-block system-fold" ${unhealthyPlugins ? 'open' : ''}>
                <summary class="system-platform-block-head"><div><h4>插件状态</h4><p>任务、数据、健康检查和资源清理由系统统一管理</p></div><div class="system-fold-meta"><span class="${unhealthyPlugins ? 'text-danger' : ''}">${unhealthyPlugins ? `${unhealthyPlugins} 个异常` : `${Number(runtime.summary?.total || 0)} 个插件`}</span><i class="bi bi-chevron-down"></i></div></summary>
                <div class="table-responsive"><table class="table system-compact-table system-plugins-table align-middle mb-0">
                    <thead><tr><th>插件</th><th>健康</th><th>后台任务</th><th>数据占用</th></tr></thead>
                    <tbody>${plugins.length ? plugins.map(item => {
                        const storageBytes = (item.storage?.entries || []).reduce((sum, entry) => sum + Number(entry.bytes || 0), 0);
                        const healthState = item.health?.status || 'healthy';
                        return `<tr><td data-label="插件"><strong>${this.esc(item.plugin_id)}</strong><small>已接入统一管理</small></td>
                            <td data-label="健康"><span class="system-state-pill ${this.statusClass(healthState)}">${this.esc(this.statusLabel(healthState))}</span><small>${this.esc(item.health?.message || '')}</small></td>
                            <td data-label="后台任务">${Number(item.active_tasks || 0)}</td><td data-label="数据占用">${this.formatBytes(storageBytes)}</td></tr>`;
                    }).join('') : '<tr><td colspan="4" class="text-muted py-4 text-center">暂无插件状态数据</td></tr>'}</tbody>
                </table></div>
            </details>
            <details class="system-platform-block system-fold" ${active.length ? 'open' : ''}>
                <summary class="system-platform-block-head"><div><h4>后台任务</h4><p>查看正在执行、等待、完成或失败的操作</p></div><div class="system-fold-meta"><span class="${active.length ? 'text-primary' : ''}">${active.length ? `${active.length} 项执行中` : `${recent.length} 条记录`}</span><i class="bi bi-chevron-down"></i></div></summary>
                ${this.renderOperationsTable(recent)}
            </details>
            <details class="system-platform-block system-fold" id="systemStorageBlock" ${storageScan ? 'open' : ''}>
                <summary class="system-platform-block-head"><div><h4>存储空间</h4><p id="systemStorageMeta">${storage.scanned_at ? `统计于 ${this.formatTime(storage.scanned_at)}` : '尚未执行分类统计'}</p></div><div class="storage-scan-actions"><span class="system-fold-count" id="systemStorageTotal">${this.formatBytes(storage.total_classified_bytes || 0)}</span><div id="storageScanState" class="storage-scan-state d-none" aria-live="polite"><div><i class="bi bi-arrow-repeat"></i><span>准备扫描…</span></div><div class="system-progress"><span></span></div><small>0%</small></div><button class="btn btn-sm btn-light border" id="storageScanButton" onclick="event.preventDefault(); event.stopPropagation(); SystemOperations.scanStorage()"><i class="bi bi-hdd me-1"></i>扫描</button><i class="bi bi-chevron-down system-fold-chevron"></i></div></summary>
                <div id="systemStorageContent">${this.renderStorage(storage)}</div>
            </details>
            <details class="system-platform-block system-fold">
                <summary class="system-platform-block-head"><div><h4>最近问题</h4><p>相同错误会合并显示，详细信息仍保留在运行日志</p></div><div class="system-fold-meta"><span class="${incidentItems.length ? 'text-warning' : ''}">${incidentItems.length ? `${incidentItems.length} 组` : '暂无问题'}</span><i class="bi bi-chevron-down"></i></div></summary>
                <div class="system-fold-toolbar"><span>按错误指纹聚合的最近记录</span><a class="btn btn-sm btn-light border" href="/operations/logs" onclick="event.preventDefault(); UI.switchTab('logs')">查看日志</a></div>
                ${this.renderIncidents(incidentItems)}
            </details>
            <details class="system-platform-block system-fold">
                <summary class="system-platform-block-head"><div><h4>变更记录</h4><p>敏感配置仅记录已配置状态，不保存密钥值</p></div><div class="system-fold-meta"><span>${auditItems.length} 条</span><i class="bi bi-chevron-down"></i></div></summary>
                ${this.renderAudit(auditItems)}
            </details>`;
        container.dataset.ready = 'true';
        container.removeAttribute('aria-busy');
        if (storageScan) {
            this.updateStorageScanState(storageScan);
            this.pollOperation(
                storageScan.operation_id,
                result => this.finishStorageScan(result),
                operation => this.updateStorageScanState(operation)
            );
        }
    },

    renderIncidents(items) {
        if (!items.length) return '<div class="system-empty-row">最近日志中没有需要关注的警告或错误。</div>';
        return `<div class="table-responsive"><table class="table system-compact-table system-incidents-table align-middle mb-0">
            <thead><tr><th>事件</th><th>组件</th><th>级别</th><th>次数</th><th>最后发生</th></tr></thead>
            <tbody>${items.map(item => `<tr><td data-label="事件"><strong>${this.esc(item.message)}</strong><small>${this.esc(item.fingerprint)}</small></td>
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

    renderStorage(storage) {
        const labels = {
            models: '本地模型', generated: '生成内容',
            chat_archive: '聊天档案', diagnostics: '日志与诊断', managed_plugins: '插件数据',
            temporary: '临时文件', backups: '备份'
        };
        const categories = storage.categories || [];
        const cleanup = storage.cleanup || {};
        if (!categories.length) return '<div class="system-empty-row">点击“扫描”在后台统计分类占用，不会阻塞页面。</div>';
        return `<div class="storage-category-strip">${categories.map(item => `<div><span>${this.esc(labels[item.category] || item.category)}</span><strong>${this.formatBytes(item.bytes)}</strong><small>${Number(item.files || 0)} 文件</small></div>`).join('')}</div>
            <div class="storage-cleanup-row"><div><strong>插件缓存清理</strong><span>${cleanup.files ? `${cleanup.files} 个超过 ${cleanup.retention_days} 天的文件，可释放 ${this.formatBytes(cleanup.bytes)}` : '没有达到清理条件的插件缓存；未纳入管理的目录不会自动删除'}</span></div>
                ${cleanup.files ? '<input id="storageCleanupConfirmation" class="form-control form-control-sm" placeholder="清理托管缓存"><button class="btn btn-sm btn-light border" onclick="SystemOperations.cleanupStorage()">移入回收区</button>' : ''}</div>`;
    },

    updateStorageScanState(operation) {
        const button = document.getElementById('storageScanButton');
        const state = document.getElementById('storageScanState');
        if (!state) return;
        const storageBlock = document.getElementById('systemStorageBlock');
        if (storageBlock) storageBlock.open = true;
        const progress = Math.max(0, Math.min(Number(operation?.progress || 0), 100));
        state.classList.remove('d-none');
        state.querySelector('span')?.replaceChildren(document.createTextNode(operation?.message || '正在扫描存储…'));
        const progressBar = state.querySelector('.system-progress > span');
        if (progressBar) progressBar.style.width = `${progress}%`;
        const percentage = state.querySelector('small');
        if (percentage) percentage.textContent = `${progress}%`;
        if (button) {
            button.disabled = true;
            button.innerHTML = '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>扫描中';
        }
    },

    resetStorageScanState() {
        const button = document.getElementById('storageScanButton');
        const state = document.getElementById('storageScanState');
        state?.classList.add('d-none');
        if (button) {
            button.disabled = false;
            button.innerHTML = '<i class="bi bi-hdd me-1"></i>扫描';
        }
    },

    async refreshStorage() {
        const content = document.getElementById('systemStorageContent');
        if (!content) return;
        const storage = await API.operations.getStorage();
        content.innerHTML = this.renderStorage(storage);
        const meta = document.getElementById('systemStorageMeta');
        if (meta) meta.textContent = storage.scanned_at ? `统计于 ${this.formatTime(storage.scanned_at)}` : '尚未执行分类统计';
        const total = document.getElementById('systemStorageTotal');
        if (total) total.textContent = this.formatBytes(storage.total_classified_bytes || 0);
    },

    async finishStorageScan(result) {
        try {
            if (result.status === 'completed') {
                await this.refreshStorage();
                UI.showSuccess('存储分类统计已更新');
            } else if (result.status !== 'cancelled') {
                UI.showError(`存储扫描失败：${result.error || result.message}`);
            }
        } catch (error) {
            UI.showError(`读取存储统计失败：${error.message}`);
        } finally {
            this.resetStorageScanState();
        }
    },

    async scanStorage() {
        const button = document.getElementById('storageScanButton');
        if (button?.disabled) return;
        try {
            this.updateStorageScanState({ progress: 0, message: '正在启动扫描…' });
            const response = await API.operations.scanStorage();
            UI.showInfo('存储扫描已在后台开始');
            this.updateStorageScanState(response.operation);
            this.pollOperation(
                response.operation.operation_id,
                result => this.finishStorageScan(result),
                operation => this.updateStorageScanState(operation)
            );
        } catch (error) {
            this.resetStorageScanState();
            UI.showError(error.message);
        }
    },

    async cleanupStorage() {
        const confirmation = document.getElementById('storageCleanupConfirmation')?.value || '';
        if (confirmation !== '清理托管缓存') {
            UI.showError('请输入“清理托管缓存”');
            return;
        }
        if (!await UI.confirm('文件会移入 data/system_trash，可恢复；不会清理旧插件的未托管目录。', { title: '清理托管缓存', confirmText: '移入回收区' })) return;
        try {
            const result = await API.operations.cleanupStorage(7, confirmation);
            UI.showSuccess(`已将 ${result.moved_to_trash} 个文件移入回收区`);
            await this.scanStorage();
        } catch (error) {
            UI.showError(error.message);
        }
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

    async loadBackups() {
        const container = document.getElementById('systemBackupsConsole');
        if (!container) return;
        const requestId = ++this.backupRequestId;
        if (container.dataset.ready !== 'true') {
            container.innerHTML = '<div class="loading-wrapper">正在读取备份…</div>';
        } else {
            container.setAttribute('aria-busy', 'true');
        }
        try {
            const [overview, operationData] = await Promise.all([
                API.backups.getOverview(),
                API.operations.getAll(30, 'system:backup')
            ]);
            const backups = overview.backups || [];
            const active = (operationData.operations || []).find(item => ['queued', 'running', 'cancelling'].includes(item.status));
            const pendingRestoreName = overview.pending_restore?.archive_name || null;
            if (requestId !== this.backupRequestId || !document.body.contains(container)) return;
            container.innerHTML = `
                <header class="backup-command-bar">
                    <div class="backup-command-title">
                        <h3>备份与迁移</h3>
                        <span><strong>${backups.length}</strong> 个${backups[0] ? ` · 最近 ${this.formatTime(backups[0].created_at)}` : ' · 尚无备份'}</span>
                    </div>
                    <div class="backup-command-actions">
                        <div class="backup-command-config">
                            <span class="backup-security-chip" title="${this.esc(overview.security?.warning || '')}"><i class="bi bi-shield-exclamation"></i>未加密</span>
                            <label class="backup-profile-control"><span class="visually-hidden">备份类型</span><select id="backupProfile" class="form-select form-select-sm" onchange="SystemOperations.updateBackupProfileCopy()"><option value="state">状态备份</option><option value="migration">完整迁移</option></select></label>
                            <span class="backup-profile-copy" id="backupProfileCopy">配置、数据库、聊天空间与附件</span>
                            <details class="backup-options-menu">
                                <summary class="btn btn-light border btn-sm"><i class="bi bi-sliders me-1"></i>范围<i class="bi bi-chevron-down backup-options-chevron"></i></summary>
                                <div class="backup-options-popover">
                                    <strong>额外包含</strong>
                                    <label class="form-check"><input id="backupGenerated" class="form-check-input" type="checkbox" checked><span>生成内容</span></label>
                                    <label class="form-check"><input id="backupDiagnostics" class="form-check-input" type="checkbox"><span>调用诊断</span></label>
                                    <label class="form-check"><input id="backupModels" class="form-check-input" type="checkbox"><span>本地模型</span></label>
                                    <label class="form-check"><input id="backupMachineBound" class="form-check-input" type="checkbox"><span>机器绑定数据</span></label>
                                </div>
                            </details>
                        </div>
                        <div class="backup-command-buttons">
                            <button class="btn btn-primary btn-sm" data-backup-action ${active ? 'disabled' : ''} onclick="SystemOperations.createBackup()"><i class="bi bi-archive me-1"></i>创建</button>
                            <button class="btn btn-light border btn-sm" data-backup-action ${active ? 'disabled' : ''} onclick="document.getElementById('backupImportFile').click()"><i class="bi bi-upload me-1"></i>导入</button>
                            <button class="btn btn-light border btn-sm backup-refresh-button" onclick="SystemOperations.loadBackups()" title="刷新" aria-label="刷新备份列表"><i class="bi bi-arrow-clockwise"></i></button>
                        </div>
                        <input id="backupImportFile" class="d-none" type="file" accept=".zip,.mabobot-backup.zip" onchange="SystemOperations.importBackup(this)">
                    </div>
                </header>
                <div id="backupActiveOperation" class="backup-operation-slot">${this.renderBackupOperation(active)}</div>
                <div id="backupRestorePanel">${this.renderRestorePanel()}</div>
                <section class="backup-history-list">
                    <div class="backup-list-heading"><h4>备份记录</h4><span>下载、校验、恢复或删除</span></div>
                    ${this.renderBackupsTable(backups, pendingRestoreName, Boolean(active))}
                </section>`;
            container.dataset.ready = 'true';
            container.removeAttribute('aria-busy');
            if (active) {
                this.pollOperation(
                    active.operation_id,
                    () => this.loadBackups(),
                    operation => this.updateBackupOperation(operation)
                );
            }
        } catch (error) {
            container.removeAttribute('aria-busy');
            if (container.dataset.ready === 'true') {
                UI.showError(`读取备份失败：${error.message}`);
            } else {
                container.innerHTML = `<div class="system-settings-empty text-danger">读取备份失败：${this.esc(error.message)}</div>`;
            }
        }
    },

    renderBackupOperation(operation) {
        if (!operation || !['queued', 'running', 'cancelling'].includes(operation.status)) return '';
        const progress = Math.max(0, Math.min(Number(operation.progress || 0), 100));
        return `<div class="backup-active-operation"><div><strong>${this.esc(operation.title)}</strong><span>${this.esc(operation.message || '等待执行')}</span></div><div><div class="system-progress"><span style="width:${progress}%"></span></div><small>${progress}%</small></div></div>`;
    },

    updateBackupOperation(operation) {
        const slot = document.getElementById('backupActiveOperation');
        if (slot) slot.innerHTML = this.renderBackupOperation(operation);
        const active = Boolean(operation && ['queued', 'running', 'cancelling'].includes(operation.status));
        document.querySelectorAll('[data-backup-action]').forEach(button => {
            button.disabled = active || button.dataset.backupLocked === 'true';
        });
    },

    updateBackupProfileCopy() {
        const profile = document.getElementById('backupProfile')?.value || 'state';
        const copy = document.getElementById('backupProfileCopy');
        if (copy) copy.textContent = profile === 'migration'
            ? '包含 mabowx 当前代码和完整状态，适合整机迁移'
            : '配置、数据库、聊天空间与附件';
    },

    renderBackupsTable(backups, pendingRestoreName = null, actionsDisabled = false) {
        if (!backups.length) return '<div class="system-empty-row">尚未创建备份。</div>';
        return `<div class="table-responsive"><table class="table system-compact-table backup-history-table system-backup-table align-middle mb-0">
            <thead><tr><th>备份</th><th>类型</th><th>数据</th><th>创建时间</th><th></th></tr></thead>
            <tbody>${backups.map(item => {
                const pending = item.name === pendingRestoreName;
                const disabled = actionsDisabled ? 'disabled' : '';
                return `<tr>
                <td data-label="备份"><strong>${this.esc(item.name)}</strong><small>${item.imported ? '已导入' : '本机创建'} · v${this.esc(item.app_version || '-')} · ${item.valid ? '结构正常' : '结构异常'}${pending ? ' · 待恢复' : ''}</small>${item.error ? `<small class="text-danger">${this.esc(item.error)}</small>` : ''}</td>
                <td data-label="类型"><span class="system-state-pill ${item.profile === 'migration' ? 'primary' : 'muted'}">${item.profile === 'migration' ? '完整迁移' : '状态备份'}</span></td>
                <td data-label="数据">${Number(item.file_count || 0)} 个文件 · ${this.formatBytes(item.bytes)}${item.contains_plaintext_env ? '<small class="text-warning">包含明文 .env</small>' : ''}</td>
                <td data-label="创建时间">${this.formatTime(item.created_at)}</td>
                <td data-label="操作" class="system-table-action-cell"><div class="backup-row-actions">
                    <a class="btn btn-sm btn-light border" href="${API.backups.downloadUrl(item.name)}" title="下载"><i class="bi bi-download"></i></a>
                    <button class="btn btn-sm btn-light border" data-backup-action ${disabled} onclick="SystemOperations.validateBackup('${this.esc(item.name)}')" title="完整校验"><i class="bi bi-check2-circle"></i></button>
                    <button class="btn btn-sm btn-light border" data-backup-action ${item.valid ? '' : 'data-backup-locked="true" disabled'} ${disabled} onclick="SystemOperations.selectRestore('${this.esc(item.name)}')" title="恢复"><i class="bi bi-arrow-counterclockwise"></i></button>
                    <button class="btn btn-sm btn-light border text-danger" data-backup-action ${pending ? 'data-backup-locked="true" disabled' : ''} ${disabled} onclick="SystemOperations.deleteBackup('${this.esc(item.name)}')" title="${pending ? '待恢复计划正在使用此备份' : '永久删除'}"><i class="bi bi-trash3"></i></button>
                </div></td></tr>`;
            }).join('')}</tbody></table></div>`;
    },

    renderRestorePanel() {
        if (!this.restoreSelection) return '';
        return `<section class="system-platform-block backup-restore-block">
            <div class="system-platform-block-head"><div><h4>准备恢复</h4><p>${this.esc(this.restoreSelection)}</p></div><button class="btn-close" onclick="SystemOperations.selectRestore(null)" aria-label="关闭"></button></div>
            <div class="backup-restore-confirm"><div><strong>恢复会在下一次启动前执行</strong><span>系统先创建恢复前快照；请输入“恢复备份”继续。</span></div>
                <input id="backupRestoreConfirmation" class="form-control form-control-sm" autocomplete="off" placeholder="恢复备份">
                <button class="btn btn-danger btn-sm" onclick="SystemOperations.prepareRestore()">准备恢复</button></div>
        </section>`;
    },

    selectRestore(name) {
        this.restoreSelection = name || null;
        const panel = document.getElementById('backupRestorePanel');
        if (panel) panel.innerHTML = this.renderRestorePanel();
    },

    async createBackup() {
        const profile = document.getElementById('backupProfile')?.value || 'state';
        if (!await UI.confirm(
            profile === 'migration' ? '完整迁移包会直接包含当前 .env，且本版尚未加密。确定创建吗？' : '确定创建当前系统状态备份吗？',
            { title: profile === 'migration' ? '创建完整迁移包' : '创建状态备份', confirmText: '开始创建' }
        )) return;
        try {
            const response = await API.backups.create({
                profile,
                include_generated: Boolean(document.getElementById('backupGenerated')?.checked),
                include_diagnostics: Boolean(document.getElementById('backupDiagnostics')?.checked),
                include_models: Boolean(document.getElementById('backupModels')?.checked),
                include_machine_bound: Boolean(document.getElementById('backupMachineBound')?.checked)
            });
            UI.showInfo('备份任务已开始');
            this.updateBackupOperation(response.operation);
            this.pollOperation(response.operation.operation_id, result => {
                if (result.status === 'completed') UI.showSuccess('备份已创建');
                if (result.status === 'failed') UI.showError(`备份失败：${result.error || result.message}`);
                this.loadBackups();
            }, operation => this.updateBackupOperation(operation));
        } catch (error) {
            UI.showError(error.message);
        }
    },

    async importBackup(input) {
        const file = input.files?.[0];
        input.value = '';
        if (!file) return;
        if (!await UI.confirm('导入的迁移包可能包含明文密钥。仅导入你信任的文件。', { title: '导入迁移包', confirmText: '导入' })) return;
        try {
            const result = await API.backups.importFile(file);
            UI.showSuccess(`已导入 ${result.name}`);
            this.loadBackups();
        } catch (error) {
            UI.showError(`导入失败：${error.message}`);
        }
    },

    async deleteBackup(name) {
        if (!await UI.confirm(
            `确定永久删除备份“${name}”吗？\n删除后无法恢复，并会立即释放该归档占用的空间。`,
            { title: '删除备份', confirmText: '永久删除', variant: 'danger' }
        )) return;
        try {
            const result = await API.backups.delete(name, '删除备份');
            if (this.restoreSelection === name) this.restoreSelection = null;
            UI.showSuccess(`已删除 ${result.name}，释放 ${this.formatBytes(result.bytes)}`);
            await this.loadBackups();
        } catch (error) {
            UI.showError(`删除失败：${error.message}`);
        }
    },

    async validateBackup(name) {
        try {
            const response = await API.backups.validate(name);
            UI.showInfo('正在执行完整校验');
            this.updateBackupOperation(response.operation);
            this.pollOperation(response.operation.operation_id, result => {
                if (result.status === 'completed' && result.result?.valid) UI.showSuccess('备份完整性校验通过');
                else if (result.status !== 'cancelled') UI.showError(`备份校验失败：${result.error || result.result?.errors?.[0] || result.message}`);
                this.loadBackups();
            }, operation => this.updateBackupOperation(operation));
        } catch (error) {
            UI.showError(error.message);
        }
    },

    async prepareRestore() {
        const confirmation = document.getElementById('backupRestoreConfirmation')?.value || '';
        if (confirmation !== '恢复备份') {
            UI.showError('请输入“恢复备份”');
            return;
        }
        if (!await UI.confirm('目标数据将在下一次启动前被替换，并会先创建恢复前快照。', { title: '确认准备恢复', confirmText: '准备恢复', variant: 'danger' })) return;
        try {
            const response = await API.backups.prepareRestore(this.restoreSelection, confirmation);
            this.updateBackupOperation(response.operation);
            this.pollOperation(response.operation.operation_id, async result => {
                if (result.status === 'completed') {
                    UI.showSuccess('恢复计划已创建，请重启全部服务以应用');
                    await App.restartSystem();
                } else if (result.status === 'failed') {
                    UI.showError(`准备恢复失败：${result.error || result.message}`);
                    this.loadBackups();
                }
            }, operation => this.updateBackupOperation(operation));
        } catch (error) {
            UI.showError(error.message);
        }
    },

    pollOperation(operationId, onFinished, onProgress = null) {
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
                UI.showError(`读取任务状态失败：${error.message}`);
            }
        };
        setTimeout(check, 700);
    }
};

window.SystemOperations = SystemOperations;
