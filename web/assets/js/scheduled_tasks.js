/* The console and chat tools manage the same versioned task definitions. */
const ScheduledTasks = {
    controlEditing: false, settings: {enabled:true,version:1}, tasks: [], chats: [], selected: null, editing: false, requestId: null, pendingId: null, detailRequest: 0, listRequest: 0, activeTab: 'content', saving: false, removedTaskIds: new Set(), deletingTaskIds: new Set(),
    labels: {active:'启用', paused:'暂停', deleted:'已删除', completed:'完成', queued:'排队中', running:'执行中', failed:'失败', blocked:'执行受阻', role_blocked:'角色限制', interrupted:'执行中断，待核对', cancelled:'已停止', missed:'错过执行', skipped:'已跳过', bundled:'合并确认', pending:'待发送', sending:'发送中', sent:'已发送', unknown:'发送状态未知'},
    esc(value) { return UI.escapeHtml(String(value ?? '')); },
    requestKey() { return globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`; },
    label(value) { return value === 'no_change' ? '检查成功，无需通知' : this.labels[value] || value || '—'; },
    time(value, zone = 'Asia/Shanghai') {
        if (value == null) return '—';
        try { return new Intl.DateTimeFormat('zh-CN', {timeZone:zone, dateStyle:'short', timeStyle:'medium', hour12:false}).format(new Date(value * 1000)); }
        catch { return new Date(value * 1000).toLocaleString(); }
    },
    pauseText(task) {
        const pause = task.pause;
        if (task.status !== 'paused') return '';
        if (pause?.reason === 'timed') return `暂停至 ${this.time(pause.until, task.schedule.timezone)}`;
        if (pause?.reason === 'resume_blocked') return pause.error || '自动恢复受阻，请手动恢复';
        if (task.role_block) return '角色限制暂停';
        return '已暂停，等待手动恢复';
    },
    message(text, error = false) {
        document.getElementById('scheduleMessage').innerHTML = text ? `<div class="alert ${error ? 'alert-danger' : 'alert-success'}">${this.esc(text)}</div>` : '';
    },
    async load({quiet = false} = {}) {
        const root = document.getElementById('scheduledTasksRoot');
        if (!root) return;
        if (!root.dataset.ready) this.mount(root);
        if (quiet && (this.editing || this.controlEditing)) return;
        const request = ++this.listRequest;
        const [data, options] = await Promise.all([API.get(`/api/scheduled-tasks?history=${document.getElementById('scheduleHistory').checked}`), API.get('/api/scheduled-tasks/options')]);
        if (request !== this.listRequest || (quiet && (this.editing || this.controlEditing))) return;
        this.tasks = data.tasks; this.chats = options.chats;
        this.settings = data.settings;
        this.renderSwitch();
        this.renderChatPermissions();
        const s = data.scheduler;
        document.getElementById('scheduleRuntimeStatus').textContent = !this.settings.enabled ? '计划任务功能已关闭' : !s.running ? '调度服务未启动' : s.error ? `调度异常：${s.error}` : s.leader ? `调度服务运行中${s.busy ? ' · 正在执行任务' : ''}` : '调度服务待命，由其他进程执行';
        const filter = document.getElementById('scheduleChatFilter'), previous = filter.value;
        filter.innerHTML = '<option value="">全部聊天</option>' + this.chats.map(c => `<option value="${c.id}">${this.esc(c.name)}</option>`).join('');
        filter.value = previous;
        this.rememberFilters();
        this.renderList();
        if (this.selected && !this.editing && !this.pendingId) await this.select(this.selected.id, {refresh: true});
    },
    mount(root) {
        root.dataset.ready = 'true';
        root.innerHTML = `<div class="schedule-toolbar"><div class="schedule-service"><div class="form-check form-switch mb-0"><input id="scheduleEnabled" class="form-check-input" type="checkbox" role="switch" aria-describedby="scheduleHelp"><label class="form-check-label" for="scheduleEnabled">启用计划任务</label></div><small id="scheduleRuntimeStatus" role="status"></small></div><div class="schedule-actions"><button class="btn btn-sm btn-outline-secondary" data-schedule-action="refresh" aria-label="刷新" title="刷新"><i class="bi bi-arrow-clockwise" aria-hidden="true"></i></button><button class="btn btn-sm btn-primary" data-schedule-action="new">新建任务</button></div></div>
        <details class="schedule-settings"><summary>聊天权限 <span id="schedulePermissionCount" class="text-muted"></span></summary><p class="schedule-hint">仅授权聊天可创建和执行任务，需同时开启助手与监听。取消授权会停止相关任务，重新开启不补跑。</p><div id="scheduleChatPermissions" class="d-flex flex-wrap gap-3"></div></details>
        <details class="schedule-help"><summary>使用说明</summary><p id="scheduleHelp" class="schedule-hint">可在聊天中创建和改进任务，在此查看结果与版本。关闭总开关后，停止新调度并请求停止执行，保留任务与历史；重新开启不补跑。</p></details>
        <div id="scheduleMessage" role="status" aria-live="polite"></div><div class="schedule-filters">
        <input id="scheduleSearch" class="form-control" placeholder="搜索任务或创建者" aria-label="搜索任务或创建者">
        <select id="scheduleChatFilter" class="form-select" aria-label="筛选聊天"></select>
        <select id="scheduleStateFilter" class="form-select" aria-label="筛选状态"><option value="">全部状态</option><option value="active">启用</option><option value="paused">暂停</option><option value="completed">完成</option><option value="deleted">已删除</option></select>
        <label class="schedule-history-filter"><input id="scheduleHistory" type="checkbox" class="form-check-input me-1">含已结束</label></div>
        <div class="schedule-list"><div class="table-responsive"><table class="table table-hover align-middle mb-0"><thead><tr><th scope="col">任务</th><th scope="col">聊天</th><th scope="col">执行计划</th><th scope="col">下次执行</th><th scope="col">状态</th><th scope="col">最近结果</th><th scope="col" class="schedule-row-actions">操作</th></tr></thead><tbody id="scheduleRows"></tbody></table></div></div><div id="scheduleDetailDock"><div id="scheduleDetail" hidden></div></div>`;
        root.addEventListener('click', event => { const button = event.target.closest('[data-schedule-action]'); if (button) { event.preventDefault(); this.handle(button).catch(e => this.message(e.message, true)); } });
        document.getElementById('scheduleChatPermissions').addEventListener('change', event => { if (event.target.matches('[data-schedule-chat]')) this.toggleChat(event.target); });
        document.getElementById('scheduleEnabled').addEventListener('change', () => this.toggleEnabled());
        ['scheduleSearch','scheduleChatFilter','scheduleStateFilter'].forEach(id => document.getElementById(id).addEventListener('input', event => this.filterChanged(event.target)));
        document.getElementById('scheduleHistory').addEventListener('change', event => this.filterChanged(event.target));
        this.rememberFilters();
        root.addEventListener('keydown', event => {
            if (!event.target.matches('[data-schedule-action="detail_tab"]') || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
            const tabs = [...root.querySelectorAll('[data-schedule-action="detail_tab"]')], index = tabs.indexOf(event.target);
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
            event.preventDefault(); this.switchDetailTab(tabs[next].dataset.tab); tabs[next].focus();
        });
    },
    renderChatPermissions() {
        document.getElementById('schedulePermissionCount').textContent = `${this.chats.filter(c => c.schedule_settings?.enabled).length} / ${this.chats.length}`;
        document.getElementById('scheduleChatPermissions').innerHTML = this.chats.map(c => `<label class="form-check"><input class="form-check-input" type="checkbox" data-schedule-chat="${c.id}" ${c.schedule_settings?.enabled ? 'checked' : ''}> <span>${this.esc(c.name)}${c.enabled ? '' : '（助手或监听已关闭）'}</span></label>`).join('') || '<span class="text-muted">暂无已管理的聊天</span>';
    },
    async toggleChat(input) {
        const chat = this.chats.find(c => String(c.id) === input.dataset.scheduleChat);
        input.disabled = true;
        try {
            chat.schedule_settings = await API.request(`/api/scheduled-tasks/chats/${chat.id}/settings`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:input.checked,expected_version:chat.schedule_settings.version})});
            await this.load();
            this.message(`${chat.name}：计划任务权限已${chat.schedule_settings.enabled ? '开启' : '关闭'}`);
        } catch (e) { this.message(e.message,true); }
        finally { this.renderChatPermissions(); }
    },
    taskEnabled(task) {
        return this.settings.enabled && [task.chat_id,task.delivery_chat_id].every(id => this.chats.find(c => c.id === id)?.schedule_settings?.enabled);
    },
    renderSwitch() {
        document.getElementById('scheduleEnabled').checked = this.settings.enabled;
        document.querySelectorAll('[data-schedule-action="new"]').forEach(b => b.disabled = !this.settings.enabled || !this.chats.some(c => c.enabled && c.schedule_settings?.enabled));
        document.querySelectorAll('[data-schedule-action="run_now"],[data-schedule-action="redeliver"]').forEach(b => b.disabled = !this.selected || !this.taskEnabled(this.selected) || (b.dataset.scheduleAction === 'run_now' && this.selected.goal_progress?.complete));
    },
    async toggleEnabled() {
        const input = document.getElementById('scheduleEnabled'); input.disabled = true;
        try {
            this.settings = await API.request('/api/scheduled-tasks/settings',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:input.checked,expected_version:this.settings.version})});
            await this.load();
            this.message(this.settings.enabled ? '计划任务已开启，将从后续时间开始调度。' : '计划任务已关闭，已取消排队任务并请求停止正在执行的任务。');
        } catch (e) { this.message(e.message,true); }
        finally { this.renderSwitch(); input.disabled = false; }
    },
    rememberFilters() {
        this.filters = Object.fromEntries(['scheduleSearch','scheduleChatFilter','scheduleStateFilter','scheduleHistory'].map(id => {
            const field = document.getElementById(id); return [id, field.type === 'checkbox' ? field.checked : field.value];
        }));
    },
    filterChanged(field) {
        if (this.saving || ((this.editing || this.controlEditing) && !confirm('切换筛选会放弃尚未保存的编辑，是否继续？'))) {
            if (field.type === 'checkbox') field.checked = this.filters[field.id]; else field.value = this.filters[field.id];
            return;
        }
        this.rememberFilters();
        if (field.id === 'scheduleHistory') this.removedTaskIds.clear();
        this.closeDetail();
        if (field.id === 'scheduleHistory') this.load().catch(e => this.message(e.message, true));
    },
    filteredTasks() {
        const q = document.getElementById('scheduleSearch').value.trim().toLowerCase(), chat = document.getElementById('scheduleChatFilter').value, status = document.getElementById('scheduleStateFilter').value;
        return this.tasks.filter(t => !this.removedTaskIds.has(t.id) && (!chat || String(t.chat_id) === chat) && (!status || t.status === status) && (!q || [t.title,t.creator,t.id,t.prompt].join(' ').toLowerCase().includes(q)));
    },
    renderList() {
        const tasks = this.filteredTasks(), openId = this.pendingId || this.selected?.id;
        if (openId && !this.editing && !tasks.some(t => t.id === openId)) this.closeDetail({render: false});
        // Keep the editor node (and its draft/listeners) when rebuilding list rows.
        const detail = document.getElementById('scheduleDetail');
        const focused = detail.contains(document.activeElement) ? document.activeElement : null;
        document.getElementById('scheduleDetailDock').append(detail);
        document.getElementById('scheduleRows').innerHTML = tasks.map(t => {
            const last = t.last_run, delivery = [...new Set((last?.deliveries || []).map(d => this.label(d.status)))].join('、');
            const expanded = t.id === (this.pendingId || this.selected?.id);
            return `<tr class="schedule-task-row ${expanded ? 'schedule-row-selected' : ''}"><td class="schedule-task-name"><button class="btn btn-link p-0 text-start schedule-task-toggle" data-schedule-action="select" data-id="${t.id}" aria-expanded="${expanded}" aria-controls="scheduleDetail"><i class="bi bi-chevron-${expanded ? 'down' : 'right'}" aria-hidden="true"></i><span>${this.esc(t.title)}</span></button><div class="small text-muted">${this.esc(t.creator)}</div></td><td data-label="聊天">${this.esc(t.chat_name)}</td><td data-label="执行计划">${this.esc(t.schedule_text)}</td><td data-label="下次执行">${this.taskEnabled(t) && t.status === 'active' ? this.esc(this.time(t.next_run,t.schedule.timezone)) : '—'}</td><td data-label="状态">${this.label(t.status)}${this.pauseText(t) ? `<div class="small text-muted">${this.esc(this.pauseText(t))}</div>` : ''}${!this.taskEnabled(t) ? '<div class="small text-muted">权限未开启</div>' : ''}</td><td data-label="最近结果">${last ? this.label(last.status) : '尚未执行'}${t.goal_progress ? `<div class="small text-muted">目标 ${t.goal_progress.completed_count}/${t.goal_progress.target_count} ${this.esc(t.goal_progress.unit)}</div>` : ''}<div class="small text-muted">${this.esc(delivery)}</div></td><td class="schedule-row-actions">${t.status !== 'deleted' ? `<button type="button" class="btn btn-sm schedule-row-delete" data-schedule-action="row_delete" data-id="${t.id}" title="删除任务" aria-label="删除任务：${this.esc(t.title)}" ${this.deletingTaskIds.has(t.id) ? 'disabled' : ''}><i class="bi bi-trash" aria-hidden="true"></i></button>` : ''}</td></tr>${expanded ? '<tr class="schedule-detail-row"><td colspan="7" class="schedule-detail-cell"></td></tr>' : ''}`;
        }).join('') || '<tr><td colspan="7" class="text-center text-muted py-4">没有符合条件的任务</td></tr>';
        document.querySelector('#scheduleRows .schedule-detail-cell')?.append(detail);
        if (focused?.isConnected) focused.focus({preventScroll: true});
    },
    closeDetail({render = true, focus = false} = {}) {
        const id = this.pendingId || this.selected?.id;
        ++this.detailRequest;
        this.selected = null; this.pendingId = null; this.editing = false; this.controlEditing = false; this.activeTab = 'content';
        const detail = document.getElementById('scheduleDetail');
        detail.replaceChildren(); detail.hidden = true;
        if (render) this.renderList();
        if (focus) [...document.querySelectorAll('[data-schedule-action="select"]')].find(button => button.dataset.id === id)?.focus();
    },
    async select(id, {refresh = false} = {}) {
        const request = ++this.detailRequest;
        if (!refresh) { this.selected = null; this.activeTab = 'content'; this.editing = false; }
        this.pendingId = id;
        const detail = document.getElementById('scheduleDetail');
        detail.hidden = false;
        if (!refresh) detail.innerHTML = `<div class="schedule-detail-loading"><span role="status">正在读取任务…</span>${this.button('close_detail','收起')}</div>`;
        this.renderList();
        try {
            const task = await API.get(`/api/scheduled-tasks/${encodeURIComponent(id)}`);
            if (request !== this.detailRequest || this.editing) return;
            this.pendingId = null;
            if (!this.filteredTasks().some(t => t.id === id)) return this.closeDetail();
            this.selected = task; this.renderList(); this.renderDetail();
        } catch (error) {
            if (request !== this.detailRequest) return;
            this.closeDetail(); throw error;
        }
    },
    switchDetailTab(tab) {
        if (!['content','runs','versions'].includes(tab)) return;
        this.activeTab = tab;
        document.querySelectorAll('[data-schedule-action="detail_tab"]').forEach(button => {
            const active = button.dataset.tab === tab;
            button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1;
        });
        document.querySelectorAll('[data-schedule-panel]').forEach(panel => { panel.hidden = panel.dataset.schedulePanel !== tab; });
    },
    button(action, label, attrs = '') { return `<button class="btn btn-sm btn-outline-secondary" data-schedule-action="${action}" ${attrs}>${label}</button>`; },
    renderDetail() {
        const t = this.selected; if (!t) return;
        this.controlEditing = false;
        const b = (...args) => this.button(...args);
        const controls = t.status === 'deleted' ? '' : [b('edit','编辑任务'), b('run_now','立即执行'), b(t.status === 'paused' ? 'resume' : 'pause',t.status === 'paused' ? '恢复调度' : '暂停调度'), `<details class="schedule-more"><summary>更多操作</summary><div>${b('pause_until','暂停到指定时间')}${t.pause?.reason === 'timed' ? b('pause','取消自动恢复，保持暂停') : ''}${t.status === 'active' ? b('skip_next','跳过下一次') + b('reschedule_next','仅下一次改期') : ''}${t.next_exception ? b('clear_next_change','取消单次调整') : ''}${b('stop','停止当前执行')}${b('delete','删除任务')}</div></details>`].join(' ');
        const detail = document.getElementById('scheduleDetail');
        const expanded = new Set([...detail.querySelectorAll('details[data-detail-key][open]')].map(node => node.dataset.detailKey));
        const runs = t.runs.map(r => {
            const attempts = (r.attempts || []).map(a => `<div class="small text-muted">第 ${a.attempt} 次失败：${this.esc(a.error)}${a.retry_at ? ` · 计划重试：${this.time(a.retry_at)}` : ' · 不再自动重试'}</div>`).join('');
            const failureNote = r.result?.notification_suppressed ? '<p class="small text-muted">同类失败一小时内已通知，本次未重复发送。</p>' : r.result?.quiet_reason ? `<p class="small text-muted">${this.esc({baseline:'首次检查已建立基准，未通知已有内容。',duplicate:'内容已进入通知台账，本次未重复发送。',no_change:'检查成功，没有符合条件的新内容。'}[r.result.quiet_reason] || r.result.quiet_reason)}</p>` : '';

            const files = (r.result?.attachments || []).map((a,i) => `<a class="me-3" href="/api/scheduled-tasks/${t.id}/runs/${r.id}/attachments/${i}">${this.esc(a.name || `附件 ${i+1}`)}</a>`).join('');
            const deliveries = r.deliveries.map(d => `<span class="me-3">${d.kind === 'file' ? '附件' : d.kind === 'failure' ? '失败说明' : '文本'}：${this.label(d.status)}${d.error ? `（${this.esc(d.error)}）` : ''}</span>`).join('');
            const retry = r.result && r.deliveries.some(d => ['pending','failed'].includes(d.status)) && !['queued','running'].includes(r.status) && t.status !== 'deleted' ? b('redeliver','发送未交付结果',`data-run="${r.id}"`) : '';
            return `<details class="schedule-record" data-detail-key="run-${r.id}"><summary>${this.esc(this.time(r.scheduled_at,t.schedule.timezone))} · ${r.trigger === 'manual' ? '手动执行' : r.trigger === 'baseline' ? '首次基准检查' : '定时执行'} · v${r.version} · ${this.label(r.status)}</summary><div class="small text-muted mt-2">开始：${this.time(r.started)}　结束：${this.time(r.finished)}　用量：${this.esc(r.usage?.total_tokens ?? '—')} tokens</div>${attempts}${failureNote}${r.error ? `<p class="text-danger mt-2">${this.esc(r.error)}</p>` : ''}<div class="small my-2">${deliveries}</div>${retry}${r.result ? `<pre class="schedule-output mt-3">${this.esc(r.result.text)}</pre><div>${files}</div>` : '<p class="text-muted mt-2">暂无结果</p>'}<details class="mt-2" data-detail-key="run-prompt-${r.id}"><summary>本次执行指令</summary><pre class="schedule-output">${this.esc(r.definition.prompt)}</pre></details></details>`;
        }).join('');
        const versions = t.versions.map((v,i) => `<details class="schedule-record" data-detail-key="version-${v.version}"><summary>v${v.version} · ${this.esc(v.actor)} · ${this.time(v.created)}</summary><p class="mt-2">修改依据：${this.esc(v.feedback || '—')}</p><pre class="schedule-output">${this.esc(v.definition.prompt)}</pre><p class="small">时间规则：${this.esc(JSON.stringify(v.definition.schedule))}</p>${v.definition.monitor ? `<p class="small">监控条件：${this.esc(v.definition.monitor.condition)}<br>目标：${this.esc(v.definition.monitor.key)}</p>` : ''}${t.versions[i+1] && t.versions[i+1].definition.prompt !== v.definition.prompt ? `<details data-detail-key="previous-${v.version}"><summary>修改前的指令</summary><pre class="schedule-output">${this.esc(t.versions[i+1].definition.prompt)}</pre></details>` : ''}${v.version !== t.version && t.status !== 'deleted' ? b('restore','恢复此版本的内容与时间',`data-version="${v.version}"`) : ''}</details>`).join('');
        const notices = t.notices.length ? `<details class="schedule-settings" data-detail-key="notices"><summary>聊天确认记录</summary>${t.notices.map(n => `<div class="small mt-2">${this.label(n.status)} ${this.esc(n.error || '')}</div><pre class="schedule-output">${this.esc(n.payload)}</pre>`).join('')}</details>` : '';
        detail.hidden = false;
        detail.innerHTML = `<section class="schedule-detail-panel" aria-labelledby="scheduleDetailTitle">
            <div class="schedule-detail-topbar"><header class="schedule-detail-header"><div><h3 id="scheduleDetailTitle">${this.esc(t.title)} <small class="text-muted">v${t.version}</small></h3><p class="schedule-meta">${this.label(t.status)} · ${this.esc(t.chat_name)} · ${this.esc(t.creator)}</p></div></header>
            <div class="schedule-detail-tools">${controls}${b('close_detail','收起','aria-label="收起任务详情"')}</div></div>
            <div id="scheduleControlDock"></div>
            ${t.goal_progress ? `<section class="schedule-goal-summary" aria-label="目标进度"><strong>${t.goal_progress.complete ? '目标已完成' : '目标进度'}：${t.goal_progress.completed_count} / ${t.goal_progress.target_count} ${this.esc(t.goal_progress.unit)}</strong><progress max="${t.goal_progress.target_count}" value="${t.goal_progress.completed_count}" aria-label="已完成目标数量"></progress><p class="schedule-hint">${this.esc(t.goal_progress.stopped_reason || (t.goal_progress.complete ? '已停止后续调度；结果交付状态请查看执行历史。' : `还需完成 ${t.goal_progress.remaining} ${t.goal_progress.unit}。失败、重复检查和发送重试不计数。`))}</p><details><summary>查看执行计划与完成记录</summary><p class="schedule-output">${this.esc(t.monitor.goal.plan)}</p><p class="schedule-hint">起点：${this.esc(t.monitor.goal.start_at)}<br>完成标准：${this.esc(t.monitor.goal.acceptance)}</p>${t.goal_progress.events.map(e => `<p class="schedule-hint">${this.esc(e.event_key)} · ${this.time(e.occurred_at,t.schedule.timezone)}<br>${this.esc(e.source)}</p>`).join('')}</details></section>` : ''}
            ${this.pauseText(t) ? `<p class="schedule-hint">${this.esc(this.pauseText(t))}</p>` : ''}
            ${t.next_exception ? `<p class="schedule-hint">单次调整：${this.time(t.next_exception.original_at,t.schedule.timezone)} → ${t.next_exception.replacement_at ? this.time(t.next_exception.replacement_at,t.schedule.timezone) : '跳过'}。长期排期不变。</p>` : ''}
            ${t.role_block ? `<div class="alert alert-warning"><strong>上次因角色限制暂停</strong><div>${this.esc(t.role_block.reason)}</div><details><summary>角色原文依据</summary><p>${this.esc(t.role_block.rule)}</p></details><small>调整任务或角色后，手动恢复调度；恢复前会重新核对。</small></div>` : ''}
            <nav class="schedule-detail-tabs" role="tablist" aria-label="任务详情分区">${[['content','任务内容'],['runs',`执行历史 ${t.runs.length}`],['versions',`修改版本 ${t.versions.length}`]].map(([key,label]) => `<button type="button" role="tab" id="schedule-tab-${key}" aria-controls="schedule-panel-${key}" data-schedule-action="detail_tab" data-tab="${key}">${label}</button>`).join('')}</nav>
            <div id="schedule-panel-content" role="tabpanel" aria-labelledby="schedule-tab-content" data-schedule-panel="content">
                <div class="schedule-detail-grid"><section class="schedule-instructions"><h4>执行指令</h4><pre class="schedule-output schedule-prompt">${this.esc(t.prompt)}</pre>${t.monitor ? `<h4>满足条件才通知</h4><p>${this.esc(t.monitor.condition)}</p><p class="small">首次：${t.monitor.initial === 'baseline' ? '建立基准，不发已有内容' : '符合条件立即通知'}</p><details data-detail-key="monitor-state"><summary>检查进度</summary><p class="small">目标：${this.esc(t.monitor.key)} · 状态版本：${t.monitor_state?.revision || 0} · 上次成功检查：${this.time(t.monitor_state?.updated)}</p><pre class="schedule-output">${this.esc(t.monitor_state?.state || '尚未建立基准')}</pre>${t.monitor.source_file ? `<p class="small">文件变更检查：${this.esc(t.monitor.source_file)}</p>` : ''}</details>` : ''}${t.original_request ? `<details class="schedule-original" data-detail-key="original"><summary>原始请求</summary><p class="schedule-output schedule-hint">${this.esc(t.original_request)}</p></details>` : ''}</section>
                <aside class="schedule-definition" aria-label="执行安排"><h4>执行安排</h4><dl><div><dt>执行计划</dt><dd>${this.esc(t.schedule_text)}</dd></div><div><dt>下次执行</dt><dd>${this.taskEnabled(t) && t.status === 'active' ? this.esc(this.time(t.next_run,t.schedule.timezone)) : '—'}</dd></div><div><dt>结果发送到</dt><dd>${this.esc(t.delivery_chat_name)}</dd></div></dl><details class="schedule-settings" data-detail-key="parameters"><summary>运行参数</summary><p class="schedule-hint">任务编号：${t.id}<br>后续触发：${t.upcoming.map(x => this.time(x,t.schedule.timezone)).join(' / ') || '无'}<br>补跑时效：${t.misfire_grace_seconds} 秒<br>单次超时：${t.timeout_seconds} 秒<br>暂停不终止已开始的执行。</p></details>${notices}</aside></div>
            </div>
            <div id="schedule-panel-runs" role="tabpanel" aria-labelledby="schedule-tab-runs" data-schedule-panel="runs"><p class="schedule-hint">最近 50 次执行，展开查看结果与交付状态。</p>${runs || '<p class="schedule-hint">尚未执行</p>'}</div>
            <div id="schedule-panel-versions" role="tabpanel" aria-labelledby="schedule-tab-versions" data-schedule-panel="versions"><p class="schedule-hint">最近 100 个版本，恢复后生成新版本。</p>${versions}</div>
        </section>`;
        detail.querySelectorAll('details[data-detail-key]').forEach(node => { node.open = expanded.has(node.dataset.detailKey); });
        this.switchDetailTab(this.activeTab);
        this.renderSwitch();
    },
    editor(task = null) {
        ++this.detailRequest; this.pendingId = null;
        this.editing = true; this.requestId = this.requestKey();
        document.getElementById('scheduleDetail').hidden = false;
        this.renderList();
        const t = task || {title:'',prompt:'',creator:'Web 管理员',chat_id:this.chats.find(c => c.enabled && c.schedule_settings?.enabled)?.id,delivery_chat_id:'',profile_id:'',timeout_seconds:600,misfire_grace_seconds:3600,resources:[],schedule:{kind:'daily',time:'09:00',timezone:'Asia/Shanghai',weekdays:[1],seconds:3600}};
        const options = (selected) => this.chats.map(c => `<option value="${c.id}" ${c.id === selected ? 'selected' : ''}>${this.esc(c.name)}${c.enabled ? '' : '（助手或监听已关闭）'}</option>`).join('');
        const field = (label,name,value,type='text') => `<label class="col-md-6 form-label">${label}<input class="form-control mt-1" name="${name}" type="${type}" value="${this.esc(value)}"></label>`;
        document.getElementById('scheduleDetail').innerHTML = `<section class="schedule-detail-panel"><h3 class="h5">${task ? '编辑任务' : '新建任务'}</h3><form id="scheduleForm"><div class="row g-3">
        ${field('任务名称','title',t.title)}${field('创建者名字 / 别名','creator',t.creator)}
        <label class="col-md-6 form-label">所属聊天<select name="chat_id" class="form-select mt-1" ${task ? 'disabled' : ''}>${options(t.chat_id)}</select></label><label class="col-md-6 form-label">结果发送到<select name="delivery_chat_id" class="form-select mt-1"><option value="">所属聊天</option>${options(t.delivery_chat_id)}</select></label>
        <label class="col-12 form-label">执行指令<textarea name="prompt" class="form-control mt-1" rows="6" required>${this.esc(t.prompt)}</textarea></label>
        <div class="col-12"><label><input type="checkbox" name="monitor_enabled" ${t.monitor ? 'checked' : ''}> 仅在有更新或满足条件时通知</label><p class="small text-muted mb-0">没有变化时保持安静，检查失败仍记录并反馈。普通定期报告无需勾选。</p></div>
        <div class="col-12 row g-3 m-0 p-0" data-monitor-fields>
        ${field('监控目标标识（如账号主页；换目标时修改）','monitor_key',t.monitor?.key || '')}
        <label class="col-md-6 form-label">首次成功检查<select name="monitor_initial" class="form-select mt-1"><option value="baseline" ${t.monitor?.initial !== 'notify' ? 'selected' : ''}>建立基准，不通知已有内容</option><option value="notify" ${t.monitor?.initial === 'notify' ? 'selected' : ''}>符合条件立即通知</option></select></label>
        <label class="col-12 form-label">通知条件与核验要求<textarea name="monitor_condition" rows="3" class="form-control mt-1" placeholder="例如：实际读取此账号的新帖，按帖子 ID 去重并附原帖链接；无法读取时报告阻塞。">${this.esc(t.monitor?.condition || '')}</textarea></label>
        ${field('可选：先检查工作区数据文件，无变更时不调用模型','monitor_source_file',t.monitor?.source_file || '')}
        <label class="col-12"><input type="checkbox" name="goal_enabled" ${t.monitor?.goal ? 'checked' : ''}> 完成指定数量的事件后结束（如未来五期，按成功事件计数）</label>
        <div class="col-12 row g-3 m-0 p-0" data-goal-fields>
        ${field('目标数量（1–100）','goal_target',t.monitor?.goal?.target_count || 5,'number')}${field('计数单位（如期、场、份）','goal_unit',t.monitor?.goal?.unit || '期')}
        ${field('事件起点（含时区）','goal_start_at',t.monitor?.goal?.start_at || new Date().toISOString())}
        <label class="col-12 form-label">每个事件的完成标准<textarea name="goal_acceptance" rows="3" class="form-control mt-1" placeholder="例如：按官方期号逐期核对所有指定号码，并附可核实的来源。">${this.esc(t.monitor?.goal?.acceptance || '')}</textarea></label>
        <label class="col-12 form-label">执行计划<textarea name="goal_plan" rows="4" class="form-control mt-1" placeholder="数据来源、检查节奏、核对步骤、如何补查漏过事件，未公布或读取失败如何处理。">${this.esc(t.monitor?.goal?.plan || '')}</textarea></label>
        ${t.monitor?.goal ? '<label class="col-12"><input type="checkbox" name="goal_reset"> 开始新一批目标（旧进度保留在历史记录，不计入新一批）</label>' : ''}
        <p class="schedule-hint">目标任务需选择周期检查；完成数量由后台按不同事件累计，不依赖执行次数。修改现有起点或完成标准时需开始新一批。</p></div></div>
        <label class="col-md-6 form-label">触发方式<select name="kind" class="form-select mt-1">${[['once','仅一次'],['daily','每天'],['weekly','每周'],['interval','固定间隔'],['window','每日时段内重复']].map(([k,v]) => `<option value="${k}" ${t.schedule.kind === k ? 'selected' : ''}>${v}</option>`).join('')}</select></label>${field('时区','timezone',t.schedule.timezone)}
        <div class="col-12 row g-3 m-0 p-0" data-kind="once">${field('一次性时间（含时区，如 2026-10-28T09:00:00+08:00）','at',t.schedule.at || '')}</div>
        <div class="col-12 row g-3 m-0 p-0" data-kind="daily weekly">${field('本地触发时间','time',t.schedule.time || '09:00','time')}</div>
        <div class="col-12 row g-3 m-0 p-0" data-kind="daily weekly">${field('多个时刻（可选，用逗号分隔，如 08:00,12:00,18:00；填写后替代上面的单个时间）','times',(t.schedule.times || []).join(','))}</div>
        <div class="col-12 row g-3 m-0 p-0" data-kind="window">${field('时段开始 HH:MM','start_time',t.schedule.start_time || '08:00')}${field('时段结束 HH:MM（可用 24:00；早于开始表示跨午夜）','end_time',t.schedule.end_time || '24:00')}${field('时段内间隔（分钟）','window_minutes',(t.schedule.seconds || 7200) / 60,'number')}<label class="col-12"><input type="checkbox" name="include_end" ${t.schedule.include_end ? 'checked' : ''}> 包含结束时刻（须落在间隔上）</label><div class="col-12 schedule-weekdays">${[1,2,3,4,5,6,7].map(d => `<label class="me-3"><input type="checkbox" name="window_weekdays" value="${d}" ${((['window','weekly'].includes(t.schedule.kind) ? t.schedule.weekdays : null) || [1,2,3,4,5,6,7]).includes(d) ? 'checked' : ''}> 周${'一二三四五六日'[d-1]}</label>`).join('')}</div><p class="schedule-hint">跨午夜按时段开始的星期计算；例如周五 22:00–02:00 延续至周六凌晨。</p></div>
        <div class="col-12" data-kind="weekly"><span class="me-3">每周执行日</span>${[1,2,3,4,5,6,7].map(d => `<label class="me-3"><input type="checkbox" name="weekdays" value="${d}" ${(t.schedule.weekdays || [1]).includes(d) ? 'checked' : ''}> 周${'一二三四五六日'[d-1]}</label>`).join('')}</div>
        <div class="col-12 row g-3 m-0 p-0" data-kind="interval">${field('间隔秒数（最少 60 秒）','seconds',t.schedule.seconds || 3600,'number')}${field('首次执行时间（含时区；留空从现在计时）','start_at',t.schedule.start_at || '')}</div>
        <div class="col-12 row g-3 m-0 p-0" data-kind="daily weekly interval window">${field('开始日期（可选，含当天）','start_date',t.schedule.start_date || '', 'date')}${field('结束日期（可选，含当天；跨夜时段可延续至次日）','end_date',t.schedule.end_date || '', 'date')}</div>
        <div class="col-12"><button type="button" class="btn btn-sm btn-outline-secondary" data-schedule-action="preview">预览未来三次触发</button><span id="schedulePreview" class="small ms-2" role="status"></span></div>
        <details class="col-12 schedule-advanced"><summary>高级设置</summary><div class="row g-3 mt-1">${field('单次超时（秒）','timeout_seconds',t.timeout_seconds,'number')}${field('允许补跑的时效（秒）','misfire_grace_seconds',t.misfire_grace_seconds,'number')}${field('执行 Profile（留空跟随所属聊天）','profile_id',t.profile_id || '')}
        <label class="col-12 form-label">持久资源引用（工作区内文件，每行一个）<textarea name="resources" rows="2" class="form-control mt-1">${this.esc(t.resources.join('\n'))}</textarea></label></div></details>
        ${task ? '<label class="col-12 form-label">修改说明<textarea name="feedback" rows="2" class="form-control mt-1" placeholder="说明改进要求，保留在版本历史中"></textarea></label>' : ''}
        </div><div class="schedule-form-actions d-flex gap-2 mt-3"><button class="btn btn-primary" type="submit">保存任务</button><button class="btn btn-outline-secondary" type="button" data-schedule-action="cancel_edit">取消</button></div></form></section>`;
        const form = document.getElementById('scheduleForm');
        const visibility = () => form.querySelectorAll('[data-kind]').forEach(el => { el.hidden = !el.dataset.kind.split(' ').includes(form.elements.kind.value); });
        form.elements.kind.addEventListener('change',visibility); visibility();
        const monitorVisibility = () => { form.querySelector('[data-monitor-fields]').hidden = !form.elements.monitor_enabled.checked; };
        form.elements.monitor_enabled.addEventListener('change', monitorVisibility); monitorVisibility();
        const goalVisibility = () => {
            const enabled = form.elements.goal_enabled.checked;
            form.querySelector('[data-goal-fields]').hidden = !enabled;
            form.elements.monitor_initial.disabled = enabled;
            form.elements.monitor_source_file.disabled = enabled;
            if (enabled) form.elements.monitor_initial.value = 'notify';
        };
        form.elements.goal_enabled.addEventListener('change', goalVisibility); goalVisibility();
        form.addEventListener('submit',event => { event.preventDefault(); this.save().catch(e => this.message(e.message,true)); });
    },
    formData() {
        const data = new FormData(document.getElementById('scheduleForm')), kind = data.get('kind');
        const schedule = {kind,timezone:data.get('timezone').trim()};
        if (kind === 'once') schedule.at = data.get('at').trim();
        if (['daily','weekly'].includes(kind)) {
            const times = data.get('times').trim();
            if (times) schedule.times = times.split(/[,，、\s]+/).filter(Boolean);
            else schedule.time = data.get('time');
        }
        if (kind === 'window') Object.assign(schedule, {start_time:data.get('start_time').trim(),end_time:data.get('end_time').trim(),seconds:Number(data.get('window_minutes')) * 60,include_end:data.has('include_end'),weekdays:data.getAll('window_weekdays').map(Number)});
        if (kind !== 'once') for (const key of ['start_date','end_date']) if (data.get(key)) schedule[key] = data.get(key);
        if (kind === 'weekly') schedule.weekdays = data.getAll('weekdays').map(Number);
        if (kind === 'interval') { schedule.seconds = Number(data.get('seconds')); if (data.get('start_at').trim()) schedule.start_at = data.get('start_at').trim(); }
        const chatId = this.selected?.chat_id || Number(data.get('chat_id'));
        const monitor = data.has('monitor_enabled') ? {key:data.get('monitor_key').trim(),condition:data.get('monitor_condition').trim(),initial:data.get('monitor_initial') || 'notify',source_file:(data.get('monitor_source_file') || '').trim()} : null;
        if (monitor && data.has('goal_enabled')) {
            monitor.initial = 'notify'; monitor.source_file = '';
            monitor.goal = {key: !data.has('goal_reset') && this.selected?.monitor?.goal?.key || `goal-${this.requestId}`,
                target_count:Number(data.get('goal_target')),unit:data.get('goal_unit').trim(),start_at:data.get('goal_start_at').trim(),
                acceptance:data.get('goal_acceptance').trim(),plan:data.get('goal_plan').trim()};
        }
        return {title:data.get('title').trim(),prompt:data.get('prompt').trim(),creator:data.get('creator').trim(),chat_id:chatId,delivery_chat_id:Number(data.get('delivery_chat_id')) || chatId,profile_id:data.get('profile_id').trim(),schedule,timeout_seconds:Number(data.get('timeout_seconds')),misfire_grace_seconds:Number(data.get('misfire_grace_seconds')),resources:data.get('resources').split('\n').map(s => s.trim()).filter(Boolean),monitor};
    },
    async save() {
        const form = document.getElementById('scheduleForm'), button = form.querySelector('[type="submit"]'); button.disabled = true; this.saving = true;
        try {
            let definition = this.formData(), result;
            if (this.selected) {
                const equal = (a,b) => JSON.stringify(a,Object.keys(a || {}).sort()) === JSON.stringify(b,Object.keys(b || {}).sort());
                definition = Object.fromEntries(Object.entries(definition).filter(([key,value]) => key === 'schedule' ? !equal(value,this.selected[key]) : JSON.stringify(value) !== JSON.stringify(this.selected[key])));
                result = await API.request(`/api/scheduled-tasks/${this.selected.id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({definition,expected_version:this.selected.version,feedback:form.elements.feedback.value || 'Web 编辑'})});
            } else result = await API.post('/api/scheduled-tasks',{definition,request_id:this.requestId});
            this.editing = false; this.selected = result.task; await this.load(); this.message('任务已保存；后续执行使用最新版本。');
        } finally { this.saving = false; button.disabled = false; }
    },
    controlEditor(action) {
        this.controlEditing = true;
        const dock = document.getElementById('scheduleControlDock');
        const pause = action === 'pause_until';
        dock.innerHTML = `<form class="schedule-control-form"><label class="form-label">${pause ? '自动恢复时间' : '下一次改到'}（含时区）<input name="instant" class="form-control mt-1" required placeholder="2026-11-07T09:00:00+08:00"></label><p class="schedule-hint">${pause ? '暂停后保留原排期，到期检查权限后恢复，不补跑暂停期间任务。已开始的执行继续完成。' : '仅调整下一次，之后仍按原排期执行。'}</p><div class="schedule-form-actions d-flex gap-2"><button type="submit" class="btn btn-primary">确认${pause ? '暂停' : '改期'}</button><button type="button" class="btn btn-outline-secondary" data-schedule-action="close_control">取消</button></div></form>`;
        const form = dock.querySelector('form');
        form.elements.instant.focus();
        form.addEventListener('submit', async event => {
            event.preventDefault();
            if (this.saving) return;
            this.saving = true;
            const button = form.querySelector('[type="submit"]'); button.disabled = true;
            try {
                await API.post(`/api/scheduled-tasks/${this.selected.id}/actions/${pause ? 'pause' : 'reschedule_next'}`, {expected_version:this.selected.version, [pause ? 'until' : 'at']:form.elements.instant.value.trim()});
                this.saving = false; this.controlEditing = false;
                await this.load(); this.message(pause ? '已保存暂停和自动恢复时间。' : '已保存单次改期，长期排期不变。');
            } catch (e) { this.message(e.message, true); }
            finally { this.saving = false; button.disabled = false; }
        });
    },
    async deleteFromRow(button) {
        const id = button.dataset.id, task = this.tasks.find(task => task.id === id);
        if (!task || this.deletingTaskIds.has(id)) return;
        const draft = this.editing && this.selected?.id === id ? '未保存的编辑也会丢弃。' : '';
        if (!confirm(`删除“${task.title}”？将取消后续调度并停止当前执行。${draft}历史记录仍会保留。`)) return;
        this.deletingTaskIds.add(id); button.disabled = true;
        try {
            await API.post(`/api/scheduled-tasks/${encodeURIComponent(id)}/actions/delete`, {expected_version: task.version, request_id: this.requestKey()});
            this.removedTaskIds.add(id);
            if ((this.pendingId || this.selected?.id) === id) this.closeDetail(); else this.renderList();
            this.message('任务已删除，后续调度已取消；如有执行中的任务，已请求停止。');
            await this.load();
        } finally { this.deletingTaskIds.delete(id); button.disabled = false; }
    },
    async handle(button) {
        const action = button.dataset.scheduleAction;
        if (this.saving) return;
        if (['pause_until','reschedule_next'].includes(action)) return this.controlEditor(action);
        if (action === 'close_control') { this.controlEditing = false; document.getElementById('scheduleControlDock').replaceChildren(); return; }
        if (action === 'skip_next' && !confirm('跳过下一次执行？之后仍按原排期执行。')) return;
        if (action === 'row_delete') return this.deleteFromRow(button);
        if (['select','new','refresh','close_detail'].includes(action) && (this.editing || this.controlEditing) && !confirm('放弃尚未保存的编辑？')) return;
        if (action === 'detail_tab') return this.switchDetailTab(button.dataset.tab);
        if (action === 'close_detail') return this.closeDetail({focus: true});
        if (action === 'select') {
            if (button.dataset.id === (this.pendingId || this.selected?.id)) return this.closeDetail({focus: true});
            return this.select(button.dataset.id);
        }
        if (action === 'new') { this.closeDetail(); return this.editor(); }
        if (action === 'refresh') { this.editing = false; this.controlEditing = false; return this.load(); }
        if (action === 'edit') return this.editor(this.selected);
        if (action === 'cancel_edit') { this.editing = false; if (this.selected) return this.renderDetail(); this.closeDetail(); return; }
        if (action === 'preview') { const data = await API.post('/api/scheduled-tasks/preview',this.formData().schedule); document.getElementById('schedulePreview').textContent = data.upcoming.map(t => this.time(t,data.schedule.timezone)).join(' / '); return; }
        if (!this.selected) return;
        if (action === 'delete' && !confirm('删除此任务并停止当前执行？历史记录仍会保留。')) return;
        if (action === 'restore' && !confirm('将所选版本的内容与时间规则恢复为一个新版本？')) return;
        button.disabled = true;
        try { await API.post(`/api/scheduled-tasks/${this.selected.id}/actions/${action}`,{expected_version:this.selected.version,restore_version:Number(button.dataset.version) || null,run_id:button.dataset.run || null,request_id:this.requestKey()}); await this.load(); this.message(action === 'run_now' ? '已加入执行队列，完成后会发送结果。' : action === 'stop' ? '已请求停止执行。' : '操作已保存。'); }
        finally { button.disabled = false; }
    }
};
