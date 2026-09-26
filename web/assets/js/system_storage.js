/** Storage inventory and explicit preview → recycle → restore/delete workflow. */
const SystemStorage = {
    data: null, preview: null, days: 7, busy: false, requestId: 0, timer: null, operation: null,
    esc(value) { return UI.escapeHtml(String(value ?? '')); },
    bytes(value) { return SystemOperations.formatBytes(value); },
    time(value) { return SystemOperations.formatTime(value); },

    async load() {
        const id = ++this.requestId;
        const container = document.getElementById('systemStorageConsole');
        if (!container) return;
        if (!this.data) container.innerHTML = '<div class="loading-wrapper">正在读取存储信息…</div>';
        try {
            const [data, jobs] = await Promise.all([
                API.operations.getStorage(), API.operations.getAll(30, 'system:storage')
            ]);
            if (id !== this.requestId || !document.body.contains(container)) return;
            this.data = data;
            const active = (jobs.operations || []).find(row => ['queued', 'running', 'cancelling'].includes(row.status));
            this.busy = Boolean(active);
            this.operation = active || null;
            this.render();
            if (active) this.watch(active.operation_id);
        } catch (error) {
            if (id !== this.requestId) return;
            container.innerHTML = `<div class="system-empty-row text-warning" role="alert">存储信息读取失败：${this.esc(error.message)} <button class="btn btn-light border btn-sm" onclick="SystemStorage.load()">重试</button></div>`;
        }
    },

    render() {
        const el = document.getElementById('systemStorageConsole');
        if (!el || !this.data) return;
        const data = this.data, disk = data.disk || {}, rows = (data.categories || []).filter(row => Number(row.files) > 0), trash = data.trash || [];
        const total = Number(data.total_classified_bytes || 0);
        const disabled = this.busy ? 'disabled' : '';
        const open = new Set([...el.querySelectorAll('details[open][data-storage-category]')].map(node => node.dataset.storageCategory));
        el.innerHTML = `
            <div class="system-platform-heading"><div><h3>存储空间</h3><p>了解空间去向，预览后清理可再生成的缓存</p></div>
                <button id="storageScanButton" class="btn btn-primary btn-sm" ${disabled} onclick="SystemStorage.scan()"><i class="bi bi-arrow-repeat me-1"></i>${data.scanned_at ? '重新扫描' : '扫描空间'}</button></div>
            <div class="storage-job ${this.busy ? '' : 'd-none'}" role="status"><span class="spinner-border spinner-border-sm"></span><span>${this.esc(this.operation?.message || '正在执行存储任务…')}</span></div>
            <div class="storage-overview">
                <div><span>项目文件大小</span><strong>${data.scanned_at ? this.bytes(total) : '待扫描'}</strong><small>${data.scanned_at ? `${Number(data.total_classified_files || 0).toLocaleString()} 个文件` : '点击扫描获取完整分类'}</small></div>
                <div><span>所在磁盘可用空间</span><strong>${this.bytes(disk.free)}</strong><small>磁盘容量 ${this.bytes(disk.total)}</small></div>
                <div><span>回收区待处理文件</span><strong>${this.bytes(trash.reduce((sum, row) => sum + row.bytes, 0))}</strong><small>永久删除后才释放空间</small></div>
            </div>
            <div class="storage-caption">${data.scanned_at ? `统计于 ${this.time(data.scanned_at)} · 文件大小不等于磁盘实际分配空间` : '尚无统计，清理或恢复后需重新扫描。'}${data.incomplete ? ` · 有 ${Number(data.unreadable_count)} 项读取失败，统计不完整` : ''}</div>
            <section class="system-platform-block"><div class="system-platform-block-head"><div><h4>空间分布</h4><p>展开分类查看目录和最大的 20 个文件</p></div><a class="btn btn-light border btn-sm" href="/system/backups" onclick="event.preventDefault(); UI.switchSystemSettingsGroup('backups')">管理备份</a></div>
                ${rows.length ? rows.map(row => this.category(row, total, open.has(row.category))).join('') : `<div class="system-empty-row">${data.scanned_at ? '未发现文件。' : '后台扫描会覆盖整个项目目录，不会跟随符号链接。'}</div>`}
            </section>
            <section class="system-platform-block"><div class="system-platform-block-head"><div><h4>清理缓存</h4><p>仅处理插件托管缓存和临时文件；保留聊天附件、生成内容、数据库和运行环境。</p></div></div>
                <div class="storage-cleanup-controls"><label for="storageRetention">保留最近</label><select id="storageRetention" class="form-select form-select-sm" ${disabled} onchange="SystemStorage.changeDays(this.value)">${[7, 30, 90, 180].map(days => `<option value="${days}" ${days === this.days ? 'selected' : ''}>${days} 天</option>`).join('')}</select><button id="storagePreviewButton" class="btn btn-light border btn-sm" ${disabled} onclick="SystemStorage.makePreview()">预览清理清单</button></div>
                ${this.renderPreview(disabled)}
            </section>
            <section class="system-platform-block"><div class="system-platform-block-head"><div><h4>回收区</h4><p>恢复不会覆盖同名新文件；永久删除不可撤销。</p></div></div>
                ${trash.length ? trash.map(row => `<div class="storage-trash-row"><div><strong>${this.time(row.created_at)}</strong><small>${Number(row.files)} 个文件 · ${this.bytes(row.bytes)}</small></div><div class="storage-action-buttons"><button class="btn btn-light border btn-sm" ${disabled} onclick="SystemStorage.trashAction('${this.esc(row.id)}', 'restore')">恢复</button><button class="btn btn-outline-danger btn-sm" ${disabled} onclick="SystemStorage.trashAction('${this.esc(row.id)}', 'purge')">永久删除</button></div></div>`).join('') : '<div class="system-empty-row">回收区为空。</div>'}
            </section>`;
    },

    category(row, total, isOpen) {
        const percent = total ? Math.min(100, row.bytes / total * 100) : 0;
        return `<details class="storage-category" data-storage-category="${this.esc(row.category)}" ${isOpen ? 'open' : ''}><summary><span><strong>${this.esc(row.label || row.category)}</strong><small>${Number(row.files).toLocaleString()} 个文件${row.category === 'cache' ? ' · 可预览清理' : ' · 仅统计'}</small></span><span class="storage-category-meter"><span style="width:${percent}%"></span></span><b>${this.bytes(row.bytes)}</b><i class="bi bi-chevron-down"></i></summary>
            <div class="storage-category-detail"><h5>主要目录 / 文件</h5>${this.fileRows(row.paths || [])}<h5>最大文件（最多 20 项）</h5>${this.fileRows(row.largest || [])}</div></details>`;
    },

    fileRows(rows) {
        return rows.length ? `<ul class="storage-file-list">${rows.map(row => `<li><code>${this.esc(row.path)}</code><span>${this.bytes(row.bytes)}</span></li>`).join('')}</ul>` : '<div class="system-empty-row">没有文件。</div>';
    },

    renderPreview(disabled) {
        const preview = this.preview;
        if (!preview) return '<div class="storage-caption">先预览具体文件，再确认移入回收区。预览有效期为 30 分钟。</div>';
        return `<div class="storage-preview" role="status"><strong>${Number(preview.files)} 个超过 ${Number(preview.retention_days)} 天未修改的文件 · ${this.bytes(preview.bytes)}</strong>
            <p>移入回收区后仍占用磁盘；永久删除后才释放空间。执行时会跳过已改变的文件。</p>
            ${preview.unreadable_count ? `<p class="text-warning">有 ${Number(preview.unreadable_count)} 项无法读取，未纳入清理。</p>` : ''}
            ${preview.files ? `<details><summary>查看清单（展示最大的 ${Math.min(100, preview.files)} 项）</summary>${this.fileRows(preview.sample || [])}</details><button id="storageCleanupButton" class="btn btn-warning btn-sm mt-3" ${disabled} onclick="SystemStorage.cleanup()">将这 ${Number(preview.files)} 个文件移入回收区</button>` : '<p>没有符合条件的缓存。</p>'}</div>`;
    },

    changeDays(value) { this.days = Number(value); this.preview = null; this.render(); },
    scan() { return this.start(() => API.operations.scanStorage()); },
    makePreview() { this.preview = null; return this.start(() => API.operations.getCleanupPreview(this.days)); },

    async cleanup() {
        if (this.busy || !this.preview?.files) return;
        const preview = this.preview;
        if (!await UI.confirm(`将预览中的 ${preview.files} 个文件（${this.bytes(preview.bytes)}）移入回收区？此操作不会立即释放磁盘空间。`, { title: '清理缓存', confirmText: '移入回收区' })) return;
        if (this.preview !== preview) return;
        await this.start(() => API.operations.cleanupStorage(preview.preview_id, '清理托管缓存'));
    },

    async trashAction(id, action) {
        if (this.busy) return;
        const batch = (this.data.trash || []).find(row => row.id === id);
        if (!batch) return;
        const purge = action === 'purge';
        if (!await UI.confirm(purge ? `永久删除这批 ${batch.files} 个文件（${this.bytes(batch.bytes)}）？删除后无法恢复。` : '将文件恢复到原位置？已有同名文件会保留，冲突项继续留在回收区。', { title: purge ? '永久删除' : '恢复缓存', confirmText: purge ? '永久删除' : '恢复' })) return;
        await this.start(() => API.operations.storageTrashAction(id, action, purge ? '永久删除' : '恢复缓存'));
    },

    async start(request) {
        if (this.busy) return;
        this.busy = true;
        this.operation = { message: '正在提交任务…' };
        this.render();
        try {
            const response = await request();
            this.operation = response.operation;
            this.render();
            this.watch(response.operation.operation_id);
        } catch (error) {
            this.busy = false;
            this.operation = null;
            this.render();
            UI.showError(error.message);
        }
    },

    watch(id) {
        clearTimeout(this.timer);
        const check = async () => {
            try {
                const { operation } = await API.operations.get(id);
                this.operation = operation;
                if (['queued', 'running', 'cancelling'].includes(operation.status)) {
                    this.busy = true;
                    this.render();
                    this.timer = setTimeout(check, 1500);
                    return;
                }
                this.busy = false;
                this.operation = null;
                const result = operation.result || {};
                if (operation.status === 'completed') {
                    if (operation.kind === 'storage_preview') this.preview = result;
                    else if (operation.kind !== 'storage_scan') this.preview = null;
                    const message = operation.kind === 'storage_cleanup'
                        ? `已将 ${result.moved_to_trash || 0} 个文件移入回收区，跳过 ${result.skipped || 0} 个已改变的文件`
                        : operation.kind === 'storage_purge' ? `已永久删除 ${result.processed || 0} 个文件`
                        : operation.kind === 'storage_restore' ? `已恢复 ${result.processed || 0} 个文件`
                        : operation.kind === 'storage_scan' ? '空间统计已更新' : '清理预览已生成';
                    if (result.error_count) UI.showError(`${message}；${result.error_count} 项失败，未处理的文件已保留。${result.errors?.[0]?.error || ''}`);
                    else UI.showSuccess(message);
                } else {
                    this.preview = null;
                    UI.showError(`存储任务未完成：${operation.error || operation.message || operation.status}`);
                }
                await this.load();
            } catch (error) {
                this.busy = false;
                this.operation = null;
                this.render();
                UI.showError(`读取任务状态失败：${error.message}，请重新进入存储页面获取任务状态。`);
            }
        };
        this.timer = setTimeout(check, 400);
    }
};
window.SystemStorage = SystemStorage;
