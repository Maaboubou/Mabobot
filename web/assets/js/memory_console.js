/** Task-oriented memory console. Loaded after main.js to replace the legacy workbench. */
(function attachMemoryConsole() {
    if (!window.App || !window.API) return;

    const loadedSection = state => state?.loadedSections || new Set();
    const responseData = value => value?.data && value?.status ? value.data : (value || {});
    const splitValues = value => String(value || '')
        .split(/[\n,，、;；]+/)
        .map(item => item.trim())
        .filter(Boolean);

    Object.assign(App, {
        async openMemoryLibraryHub() {
            const shortcut = document.getElementById('memoryLibraryShortcut');
            if (shortcut?.getAttribute('aria-busy') === 'true') return;
            shortcut?.setAttribute('aria-busy', 'true');
            shortcut?.classList.add('disabled');
            if (window.innerWidth < 768 && typeof UI.toggleSidebar === 'function') {
                UI.toggleSidebar(false);
            }
            try {
                const users = await this.loadMemoryLibraryUsers();
                if (!users.length) {
                    UI.showError('暂无可查看记忆库的聊天，请先添加监听对象。');
                    return;
                }
                let savedUserId = 0;
                try {
                    savedUserId = Number(localStorage.getItem('mabobot.memoryLibraryUserId') || 0);
                } catch (_error) {
                    // Storage is optional.
                }
                const selected = users.find(item => item.chat_name === this.currentThreadName)
                    || users.find(item => item.id === savedUserId)
                    || users.find(item => item.assistantEnabled)
                    || users[0];
                await this.openChatMemoryLibrary(selected.id, { users });
            } catch (error) {
                UI.showError('打开记忆库失败：' + error.message);
            } finally {
                shortcut?.removeAttribute('aria-busy');
                shortcut?.classList.remove('disabled');
            }
        },

        async loadMemoryLibraryUsers() {
            const response = await API.users.getAll();
            const users = (Array.isArray(response) ? response : [])
                .filter(item => Number(item?.id) > 0 && item?.chat_name)
                .map(item => ({
                    id: Number(item.id),
                    chat_name: String(item.chat_name),
                    is_group: !!item.is_group,
                    assistantEnabled: Boolean(item.assistant_enabled)
                }))
                .sort((left, right) => {
                    if (left.assistantEnabled !== right.assistantEnabled) {
                        return Number(right.assistantEnabled) - Number(left.assistantEnabled);
                    }
                    return left.chat_name.localeCompare(right.chat_name, 'zh-CN');
                });
            this._memoryLibraryUsers = users;
            return users;
        },

        resetMemoryLibraryFilters() {
            ['memoryLibraryQuery', 'memoryLibraryDateFrom', 'memoryLibraryDateTo',
                'memoryLibraryPeopleQuery'].forEach(id => {
                const input = document.getElementById(id);
                if (input) input.value = '';
            });
            const status = document.getElementById('memoryLibraryStatus');
            if (status) status.value = 'all';
        },

        renderMemoryLibraryUserSwitcher(users, selectedUserId) {
            const selected = users.find(item => item.id === Number(selectedUserId));
            const chatName = document.getElementById('memoryLibraryChatName');
            const options = document.getElementById('memoryLibraryUserOptions');
            const query = document.getElementById('memoryLibraryUserQuery');
            const empty = document.getElementById('memoryLibraryUserEmpty');
            const toggle = document.getElementById('memoryLibraryUserSwitcher');
            if (chatName) {
                chatName.textContent = selected ? selected.chat_name : '选择聊天';
                chatName.title = selected?.chat_name || '';
            }
            if (query) query.value = '';
            empty?.classList.toggle('d-none', users.length > 0);
            if (!options) return;
            options.innerHTML = users.map(item => {
                const label = item.chat_name;
                const secondary = item.is_group ? '群聊' : '私聊';
                const active = item.id === Number(selectedUserId);
                return `<button type="button" class="dropdown-item memory-user-option ${active ? 'active' : ''}" onclick="App.switchMemoryLibraryUser(${item.id})">
                    <span class="memory-user-option-avatar ${item.is_group ? 'is-group' : ''}"><i class="bi ${item.is_group ? 'bi-people-fill' : 'bi-person-fill'}"></i></span>
                    <span class="memory-user-option-copy"><strong>${this.escapeHtml(label)}</strong><small>${this.escapeHtml(secondary)}</small></span>
                    ${item.assistantEnabled ? '<span class="memory-user-option-badge">AI 助手</span>' : ''}
                    <i class="bi bi-check-lg memory-user-option-check"></i>
                </button>`;
            }).join('');
            if (toggle && toggle.dataset.searchReady !== 'true') {
                toggle.dataset.searchReady = 'true';
                toggle.parentElement?.addEventListener('shown.bs.dropdown', () => {
                    const input = document.getElementById('memoryLibraryUserQuery');
                    if (input) {
                        input.value = '';
                        this.filterMemoryLibraryUsers();
                        input.focus();
                    }
                });
            }
        },

        filterMemoryLibraryUsers() {
            const query = (document.getElementById('memoryLibraryUserQuery')?.value || '')
                .trim().toLowerCase();
            const options = Array.from(document.querySelectorAll(
                '#memoryLibraryUserOptions .memory-user-option'
            ));
            let visible = 0;
            options.forEach(option => {
                const matches = !query || (option.textContent || '').toLowerCase().includes(query);
                option.classList.toggle('d-none', !matches);
                if (matches) visible += 1;
            });
            document.getElementById('memoryLibraryUserEmpty')
                ?.classList.toggle('d-none', visible > 0);
        },

        createMemoryLibraryState(userId, users = []) {
            return {
                userId: Number(userId),
                users,
                eventPage: 1,
                peoplePage: 1,
                reviewPage: 1,
                changesPage: 1,
                limit: 20,
                overview: null,
                loadedSections: new Set()
            };
        },

        prepareMemoryLibraryLoad() {
            const loading = `
                <div class="memory-empty-state">
                    <span class="spinner-border spinner-border-sm"></span>
                    <span>正在读取记忆库…</span>
                </div>`;
            const ids = [
                'memoryLibraryOverviewContent',
                'memoryLibraryEventList',
                'memoryLibraryPeopleList',
                'memoryLibraryReviewsContent',
                'memoryLibraryChangesContent',
                'memoryLibraryMaintenanceContent'
            ];
            ids.forEach(id => {
                const element = document.getElementById(id);
                if (element) element.innerHTML = loading;
            });
            ['memoryLibraryEventStatus', 'memoryLibraryPagination',
                'memoryLibraryPeoplePagination', 'memoryLibraryReviewsPagination',
                'memoryLibraryChangesPagination']
                .forEach(id => {
                    const element = document.getElementById(id);
                    if (element) element.innerHTML = '';
                });
            ['memoryLibraryEventCount', 'memoryLibraryPeopleCount', 'memoryLibraryReviewCount']
                .forEach(id => {
                    const element = document.getElementById(id);
                    if (element) element.textContent = '…';
                });
            const summary = document.getElementById('memoryLibrarySummary');
            if (summary) summary.innerHTML = loading;
            this.closeMemoryDrawer();
        },

        async openChatMemoryLibrary(userId, options = {}) {
            if (!userId) return;
            const modalElement = document.getElementById('memoryLibraryModal');
            if (!modalElement) return;
            let users = Array.isArray(options.users) ? options.users : [];
            if (!users.length) {
                users = await this.loadMemoryLibraryUsers();
            }
            this._memoryLibraryState = this.createMemoryLibraryState(userId, users);
            this.resetMemoryLibraryFilters();
            this.prepareMemoryLibraryLoad();
            this.renderMemoryLibraryUserSwitcher(users, userId);
            try {
                localStorage.setItem('mabobot.memoryLibraryUserId', String(Number(userId)));
            } catch (_error) {
                // Storage is optional.
            }

            const show = () => {
                const overviewButton = modalElement.querySelector(
                    '[data-bs-target="#memoryLibraryOverviewPane"]'
                );
                this.switchMemoryTab(overviewButton, '#memoryLibraryOverviewPane');
                bootstrap.Modal.getOrCreateInstance(modalElement).show();
            };
            const configElement = document.getElementById('configModal');
            const configModal = configElement ? bootstrap.Modal.getInstance(configElement) : null;
            if (configElement?.classList.contains('show') && configModal) {
                this._memoryLibraryReturnToConfig = true;
                configElement.addEventListener('hidden.bs.modal', show, { once: true });
                configModal.hide();
            } else {
                this._memoryLibraryReturnToConfig = false;
                show();
            }
            modalElement.addEventListener('hidden.bs.modal', () => {
                if (this._memoryLibraryReturnToConfig && configElement) {
                    this._memoryLibraryReturnToConfig = false;
                    bootstrap.Modal.getOrCreateInstance(configElement).show();
                }
            }, { once: true });
        },

        async switchMemoryLibraryUser(userId) {
            const current = this._memoryLibraryState;
            const targetId = Number(userId);
            if (!targetId || current?.userId === targetId) return;
            const activeTarget = document.querySelector(
                '#memoryLibraryModal .memory-nav-btn.active'
            )?.dataset.bsTarget || '#memoryLibraryOverviewPane';
            const users = current?.users || this._memoryLibraryUsers || [];
            this._memoryLibraryState = this.createMemoryLibraryState(targetId, users);
            this.resetMemoryLibraryFilters();
            this.prepareMemoryLibraryLoad();
            this.renderMemoryLibraryUserSwitcher(users, targetId);
            try {
                localStorage.setItem('mabobot.memoryLibraryUserId', String(targetId));
            } catch (_error) {
                // Storage is optional.
            }
            const toggle = document.getElementById('memoryLibraryUserSwitcher');
            if (toggle && window.bootstrap?.Dropdown) {
                bootstrap.Dropdown.getOrCreateInstance(toggle).hide();
            }
            const button = document.querySelector(
                `#memoryLibraryModal .memory-nav-btn[data-bs-target="${activeTarget}"]`
            );
            this.switchMemoryTab(button, activeTarget, { force: true });
        },

        switchMemoryTab(button, targetId, options = {}) {
            const modal = document.getElementById('memoryLibraryModal');
            if (!modal) return;
            this.closeMemoryDrawer();
            modal.querySelectorAll('.memory-nav-btn').forEach(item => {
                item.classList.toggle('active', item === button);
            });
            modal.querySelectorAll('.mem-pane').forEach(pane => {
                pane.style.display = pane.matches(targetId) ? 'flex' : 'none';
            });
            const state = this._memoryLibraryState;
            if (!state) return;
            const loaders = {
                '#memoryLibraryOverviewPane': () => this.loadMemoryLibraryOverview(),
                '#memoryLibraryEventsPane': () => this.loadMemoryLibraryEvents(state.eventPage || 1),
                '#memoryLibraryPeoplePane': () => this.loadMemoryLibraryPeople(state.peoplePage || 1),
                '#memoryLibraryReviewsPane': () => this.loadMemoryLibraryReviews(state.reviewPage || 1),
                '#memoryLibraryChangesPane': () => this.loadMemoryLibraryChanges(state.changesPage || 1),
                '#memoryLibraryMaintenancePane': () => this.loadMemoryLibraryMaintenance()
            };
            if (options.force || !loadedSection(state).has(targetId)) {
                loaders[targetId]?.();
            }
        },

        async refreshMemoryLibrary() {
            const state = this._memoryLibraryState;
            if (!state?.userId) return;
            state.loadedSections.clear();
            const active = document.querySelector(
                '#memoryLibraryModal .memory-nav-btn.active'
            );
            this.switchMemoryTab(
                active,
                active?.dataset.bsTarget || '#memoryLibraryOverviewPane',
                { force: true }
            );
        },

        formatMemoryBytes(value) {
            const bytes = Math.max(0, Number(value) || 0);
            if (bytes < 1024) return `${bytes} B`;
            if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
            return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
        },

        memoryModeLabel(mode, enabled) {
            if (mode === 'off') return '此聊天已关闭';
            if (mode === 'custom') return enabled ? '此聊天自定义（开启）' : '此聊天自定义（关闭）';
            return `继承全局（${enabled ? '开启' : '关闭'}）`;
        },

        async loadMemoryLibraryOverview() {
            const state = this._memoryLibraryState;
            if (!state?.userId) return;
            const container = document.getElementById('memoryLibraryOverviewContent');
            try {
                const data = responseData(await API.memory.getOverview(state.userId));
                if (this._memoryLibraryState !== state) return;
                state.overview = data;
                state.loadedSections.add('#memoryLibraryOverviewPane');
                const config = data.configuration || {};
                const effective = config.effective || {};
                const health = data.health || {};
                const storage = data.storage || {};
                const stage = data.stage_memory || {};
                const user = data.user || {};
                const selected = (state.users || []).find(item => item.id === state.userId);
                const chatLabel = selected?.chat_name || user.chat_name || '当前聊天';
                const chatName = document.getElementById('memoryLibraryChatName');
                if (chatName) {
                    chatName.textContent = chatLabel;
                    chatName.title = user.chat_name || '';
                }
                document.getElementById('memoryLibraryEventCount').textContent =
                    Number(data.event_count || 0).toLocaleString();
                document.getElementById('memoryLibraryPeopleCount').textContent =
                    Number(data.person_memory?.profile_count || data.people_count || 0).toLocaleString();
                const reviewCount = Number(data.quarantined_event_count || 0)
                    + Number(data.person_memory?.observations?.quarantined || 0);
                document.getElementById('memoryLibraryReviewCount').textContent =
                    reviewCount.toLocaleString();
                const summary = document.getElementById('memoryLibrarySummary');
                if (summary) {
                    summary.innerHTML = `
                        <div class="memory-status-strip">
                            <span class="memory-status-dot ${effective.memory_enabled ? 'is-on' : 'is-off'}"></span>
                            <strong>${this.escapeHtml(this.memoryModeLabel(config.mode, !!effective.memory_enabled))}</strong>
                            <span>${Number(data.active_event_count || 0).toLocaleString()} 条有效记忆</span>
                            <span>${Number(data.person_memory?.profile_count || 0).toLocaleString()} 个人物</span>
                            <span>${this.formatMemoryBytes(storage.logical_bytes || 0)} 逻辑数据</span>
                            <button class="btn btn-sm btn-outline-secondary ms-auto" type="button"
                                onclick="App.refreshMemoryLibrary()">
                                <i class="bi bi-arrow-clockwise me-1"></i>刷新
                            </button>
                        </div>`;
                }
                if (!container) return;
                const embedding = health.embedding || {};
                const integrity = health.integrity || {};
                container.innerHTML = `
                    <div class="memory-overview-grid">
                        <section class="memory-overview-card memory-overview-primary">
                            <div class="memory-card-kicker">当前状态</div>
                            <h3>${effective.memory_enabled ? '长期记忆正在工作' : '长期记忆未启用'}</h3>
                            <p>${config.mode === 'inherit' ? '此聊天使用全局设置。' : '此聊天使用独立设置。'}
                                后台还有 ${Number(health.pending_messages || 0).toLocaleString()} 条消息等待处理。</p>
                            <div class="memory-inline-actions">
                                <button class="btn btn-primary" type="button"
                                    onclick="App.openMemorySettingsForCurrentChat()">调整记忆设置</button>
                                <button class="btn btn-outline-secondary" type="button"
                                    onclick="App.switchMemoryTab(document.querySelector('[data-bs-target=&quot;#memoryLibraryMaintenancePane&quot;]'), '#memoryLibraryMaintenancePane')">查看健康状态</button>
                            </div>
                        </section>
                        <section class="memory-overview-card">
                            <div class="memory-card-kicker">质量保护</div>
                            <div class="memory-health-list">
                                <span><i class="bi ${embedding.ready ? 'bi-check-circle-fill text-success' : 'bi-exclamation-circle-fill text-warning'}"></i>
                                    向量检索：${embedding.ready ? '就绪' : '关键词回退'}</span>
                                <span><i class="bi ${integrity.ok ? 'bi-check-circle-fill text-success' : 'bi-x-circle-fill text-danger'}"></i>
                                    数据一致性：${integrity.ok ? '正常' : '需要检查'}</span>
                                <span><i class="bi bi-shield-check"></i>
                                    证据复核：${effective.memory_verification_enabled ? '开启' : '关闭'}</span>
                            </div>
                        </section>
                        <section class="memory-overview-card memory-stage-card">
                            <div class="d-flex justify-content-between align-items-start gap-3">
                                <div>
                                    <div class="memory-card-kicker">当前状态摘要</div>
                            <h4>${stage.mode === 'manual' ? '人工固定' : '自动维护 · 无需保存'}</h4>
                                </div>
                                <span class="badge text-bg-light border">${this.escapeHtml(stage.updated_at || '尚未生成')}</span>
                            </div>
                            <form class="memory-stage-form" data-current-mode="${stage.mode === 'manual' ? 'manual' : 'auto'}"
                                onsubmit="event.preventDefault(); App.saveMemoryStage(this);">
                                <div class="memory-stage-mode">
                                    <label><input type="radio" name="mode" value="auto" ${stage.mode !== 'manual' ? 'checked' : ''}
                                        onchange="App.syncMemoryStageForm(this.form)"> 自动维护</label>
                                    <label><input type="radio" name="mode" value="manual" ${stage.mode === 'manual' ? 'checked' : ''}
                                        onchange="App.syncMemoryStageForm(this.form)"> 人工固定</label>
                                </div>
                                <textarea class="form-control" name="summary" rows="7"
                                    placeholder="还没有阶段摘要">${this.escapeHtml(stage.summary || '')}</textarea>
                                <div class="memory-stage-save-row">
                                    <input class="form-control" name="reason" maxlength="1000"
                                        value="${this.escapeHtml(stage.manual_note || '')}"
                                        placeholder="修改原因（切换或人工编辑时必填）">
                                    <button class="btn btn-primary" type="submit" data-stage-submit>保存人工摘要</button>
                                </div>
                                <div class="form-text" data-stage-help></div>
                            </form>
                        </section>
                    </div>`;
                this.syncMemoryStageForm(container.querySelector('.memory-stage-form'));
            } catch (error) {
                if (container) container.innerHTML = `<div class="alert alert-danger">读取概览失败：${this.escapeHtml(error.message)}</div>`;
            }
        },

        syncMemoryStageForm(form) {
            if (!form) return;
            const currentMode = form.dataset.currentMode === 'manual' ? 'manual' : 'auto';
            const mode = form.elements.mode.value;
            const isManual = mode === 'manual';
            const isModeChange = mode !== currentMode;
            const summary = form.elements.summary;
            const reason = form.elements.reason;
            const submit = form.querySelector('[data-stage-submit]');
            const help = form.querySelector('[data-stage-help]');

            summary.readOnly = !isManual;
            reason.disabled = !isManual && !isModeChange;
            submit.disabled = !isManual && !isModeChange;
            if (isManual) {
                submit.textContent = currentMode === 'manual' ? '保存人工摘要' : '固定为人工摘要';
                help.textContent = '人工固定后，后台不会覆盖这段内容；修改摘要时需填写原因。';
            } else if (isModeChange) {
                submit.textContent = '恢复自动维护';
                help.textContent = '恢复后会保留当前摘要作为过渡，并在下一次后台任务中根据有效记忆重建。';
            } else {
                submit.textContent = '无需保存';
                help.textContent = '这是后台根据有效事件自动维护的只读摘要，日常使用不需点击保存。';
            }
        },

        async saveMemoryStage(form) {
            const state = this._memoryLibraryState;
            if (!state?.userId || !form) return;
            const mode = form.elements.mode.value;
            const reason = form.elements.reason.value.trim();
            if (reason.length < 2) {
                UI.showError('请填写修改原因。');
                return;
            }
            try {
                await API.memory.updateStage(state.userId, {
                    mode,
                    summary: form.elements.summary.value,
                    reason
                });
                UI.showSuccess(mode === 'manual' ? '阶段摘要已固定' : '已恢复自动维护');
                state.loadedSections.delete('#memoryLibraryChangesPane');
                await this.loadMemoryLibraryOverview();
            } catch (error) {
                UI.showError('保存阶段摘要失败：' + error.message);
            }
        },

        openMemorySettingsForCurrentChat() {
            const userId = this._memoryLibraryState?.userId;
            if (!userId) return;
            bootstrap.Modal.getOrCreateInstance(document.getElementById('memoryLibraryModal')).hide();
            setTimeout(() => this.showAssistantChatEditor(userId), 180);
        },

        searchMemoryLibraryEvents() {
            this.loadMemoryLibraryEvents(1, true);
        },

        async loadMemoryLibraryEvents(page = 1, force = false) {
            const state = this._memoryLibraryState;
            if (!state?.userId) return;
            const list = document.getElementById('memoryLibraryEventList');
            const statusElement = document.getElementById('memoryLibraryEventStatus');
            const pagination = document.getElementById('memoryLibraryPagination');
            const safePage = Math.max(1, Number(page) || 1);
            if (force) state.loadedSections.delete('#memoryLibraryEventsPane');
            list.innerHTML = '<div class="memory-empty-state"><span class="spinner-border spinner-border-sm"></span>正在读取记忆…</div>';
            try {
                const data = responseData(await API.memory.getEvents(state.userId, {
                    q: document.getElementById('memoryLibraryQuery')?.value.trim() || '',
                    date_from: document.getElementById('memoryLibraryDateFrom')?.value || '',
                    date_to: document.getElementById('memoryLibraryDateTo')?.value || '',
                    status: document.getElementById('memoryLibraryStatus')?.value || 'all',
                    offset: (safePage - 1) * state.limit,
                    limit: state.limit
                }));
                if (this._memoryLibraryState !== state) return;
                const items = data.items || [];
                const total = Number(data.total || 0);
                const pages = Math.max(1, Math.ceil(total / state.limit));
                state.eventPage = Math.min(safePage, pages);
                state.loadedSections.add('#memoryLibraryEventsPane');
                statusElement.textContent = `${total.toLocaleString()} 条符合条件的记忆`;
                list.innerHTML = items.length
                    ? `<div class="memory-list">${items.map(item => this.renderMemoryEventSummary(item)).join('')}</div>`
                    : '<div class="memory-empty-state"><i class="bi bi-inbox"></i><span>没有符合条件的记忆</span></div>';
                pagination.innerHTML = this.renderMemoryPagination(
                    state.eventPage,
                    pages,
                    'App.loadMemoryLibraryEvents'
                );
            } catch (error) {
                list.innerHTML = `<div class="alert alert-danger">读取记忆失败：${this.escapeHtml(error.message)}</div>`;
            }
        },

        renderMemoryPagination(page, pages, handler) {
            return `
                <span class="small text-muted">第 ${page} / ${pages} 页</span>
                <div class="btn-group">
                    <button class="btn btn-sm btn-outline-secondary" ${page <= 1 ? 'disabled' : ''}
                        onclick="${handler}(${page - 1})">上一页</button>
                    <button class="btn btn-sm btn-outline-secondary" ${page >= pages ? 'disabled' : ''}
                        onclick="${handler}(${page + 1})">下一页</button>
                </div>`;
        },

        renderMemoryEventSummary(item) {
            const state = item.is_invalidated ? '已作废'
                : item.superseded_by_event_id ? '已替代'
                    : item.verification_status === 'quarantined' ? '自动隔离' : '有效';
            const tone = state === '有效' ? 'success' : state === '自动隔离' ? 'warning' : 'secondary';
            return `
                <article class="memory-row-card">
                    <button class="memory-row-main" type="button" onclick="App.openMemoryEventDetail(${Number(item.id)})">
                        <span class="memory-row-icon"><i class="bi bi-clock-history"></i></span>
                        <span class="memory-row-copy">
                            <span class="memory-row-title">${this.escapeHtml(item.title || '未命名记忆')}</span>
                            <span class="memory-row-summary">${this.escapeHtml(item.summary || '')}</span>
                            <span class="memory-row-meta">#${Number(item.id)} · ${this.escapeHtml(item.end_time || item.created_at || '')}</span>
                        </span>
                        <span class="badge text-bg-${tone}">${state}</span>
                    </button>
                </article>`;
        },

        async openMemoryEventDetail(eventId) {
            const state = this._memoryLibraryState;
            if (!state?.userId) return;
            this.openMemoryDrawer('<i class="bi bi-clock-history me-2"></i>记忆详情', '<div class="memory-empty-state"><span class="spinner-border spinner-border-sm"></span>正在读取来源…</div>');
            try {
                const data = responseData(await API.memory.getEvent(state.userId, eventId));
                const event = data.event || {};
                const messages = data.messages || [];
                const content = document.getElementById('memDrawerContent');
                content.innerHTML = `
                    <div class="memory-detail-stack">
                        <section>
                            <div class="memory-detail-eyebrow">记忆 #${Number(event.id || 0)}</div>
                            <h4>${this.escapeHtml(event.title || '未命名记忆')}</h4>
                            <p>${this.escapeHtml(event.summary || '')}</p>
                        </section>
                        <details class="memory-detail-section">
                            <summary>查看来源消息（${messages.length}）</summary>
                            <div class="memory-evidence-list">
                                ${messages.map(message => `
                                    <div><strong>${this.escapeHtml(message.sender || '未知')}</strong>
                                    <small>${this.escapeHtml(message.time || '')}</small>
                                    <p>${this.escapeHtml(message.content || '')}</p></div>`).join('') || '<span class="text-muted">来源已经轮转或不可用</span>'}
                            </div>
                        </details>
                        ${!event.is_invalidated && !event.superseded_by_event_id ? `
                            <details class="memory-detail-section">
                                <summary>纠正这条记忆</summary>
                                <form class="memory-correction-form" data-event-id="${Number(event.id)}"
                                    onsubmit="event.preventDefault(); App.submitMemoryEventCorrection(this);">
                                    <label>哪里不准确<textarea class="form-control" name="false_claims" rows="2" required></textarea></label>
                                    <label>正确信息<textarea class="form-control" name="corrected_claim" rows="2"></textarea></label>
                                    <label>涉及人物<input class="form-control" name="affected_people" value="${this.escapeHtml((event.participants || []).join('、'))}"></label>
                                    <label>核对原因<input class="form-control" name="reason" required maxlength="1000"></label>
                                    <div class="d-flex justify-content-between gap-2">
                                        <button class="btn btn-outline-danger" type="button"
                                            onclick="App.deleteMemoryEvent(${Number(event.id)}, this)">删除记忆</button>
                                        <button class="btn btn-primary" type="submit">提交纠正</button>
                                    </div>
                                </form>
                            </details>` : ''}
                    </div>`;
            } catch (error) {
                document.getElementById('memDrawerContent').innerHTML = `<div class="alert alert-danger">${this.escapeHtml(error.message)}</div>`;
            }
        },

        async submitMemoryEventCorrection(form) {
            const state = this._memoryLibraryState;
            const eventId = Number(form?.dataset.eventId || 0);
            if (!state?.userId || !eventId) return;
            const falseClaims = splitValues(form.elements.false_claims.value);
            if (!falseClaims.length) {
                UI.showError('请填写具体不准确的说法。');
                return;
            }
            const payload = {
                action: 'invalidate',
                reason: form.elements.reason.value.trim(),
                false_claims: falseClaims,
                corrected_claim: form.elements.corrected_claim.value.trim(),
                affected_people: splitValues(form.elements.affected_people.value),
                existing_replacement_event_id: 0,
                corrected_event: null
            };
            if (!payload.reason) return UI.showError('请填写核对原因。');
            if (!await UI.confirm('提交后会同步隔离相关人物证据，并保留可撤销记录。', {
                title: '提交记忆纠正', confirmText: '确认纠正', variant: 'danger'
            })) return;
            try {
                const result = responseData(await API.memory.correctEvent(state.userId, eventId, payload));
                const affected = Number(result.correction?.after?.person?.observation_count || 0);
                UI.showSuccess(`记忆已纠正${affected ? `，同时隔离 ${affected} 条人物证据` : ''}`);
                this.closeMemoryDrawer();
                this.invalidateMemorySections(['overview', 'events', 'reviews', 'changes']);
                await this.loadMemoryLibraryEvents(state.eventPage || 1, true);
            } catch (error) {
                UI.showError('纠正失败：' + error.message);
            }
        },

        async deleteMemoryEvent(eventId) {
            const state = this._memoryLibraryState;
            if (!state?.userId) return;
            if (!await UI.confirm('删除后会退出检索，并同步隔离相关人物证据。操作可从变更记录撤销。', {
                title: '删除记忆', confirmText: '删除', variant: 'danger'
            })) return;
            try {
                await API.memory.deleteEvent(state.userId, eventId, '管理员从记忆库删除');
                UI.showSuccess('记忆已删除');
                this.closeMemoryDrawer();
                this.invalidateMemorySections(['overview', 'events', 'reviews', 'changes']);
                await this.loadMemoryLibraryEvents(state.eventPage || 1, true);
            } catch (error) {
                UI.showError('删除失败：' + error.message);
            }
        },

        searchMemoryLibraryPeople() {
            this.loadMemoryLibraryPeople(1, true);
        },

        async loadMemoryLibraryPeople(page = 1, force = false) {
            const state = this._memoryLibraryState;
            if (!state?.userId) return;
            const container = document.getElementById('memoryLibraryPeopleList');
            const pagination = document.getElementById('memoryLibraryPeoplePagination');
            const safePage = Math.max(1, Number(page) || 1);
            if (force) state.loadedSections.delete('#memoryLibraryPeoplePane');
            container.innerHTML = '<div class="memory-empty-state"><span class="spinner-border spinner-border-sm"></span>正在读取人物…</div>';
            try {
                const data = responseData(await API.memory.getPeople(state.userId, {
                    q: document.getElementById('memoryLibraryPeopleQuery')?.value.trim() || '',
                    offset: (safePage - 1) * state.limit,
                    limit: state.limit
                }));
                if (this._memoryLibraryState !== state) return;
                const total = Number(data.total || 0);
                const pages = Math.max(1, Math.ceil(total / state.limit));
                state.peoplePage = Math.min(safePage, pages);
                state.loadedSections.add('#memoryLibraryPeoplePane');
                container.innerHTML = (data.items || []).length
                    ? `<div class="memory-people-grid">${data.items.map(person => `
                        <button type="button" class="memory-person-summary" onclick="App.openMemoryPersonDetail(${Number(person.person_id)})">
                            <span class="memory-person-avatar">${this.escapeHtml(this.getInitials(person.person_name || '?'))}</span>
                            <span class="memory-row-copy">
                                <span class="memory-row-title">${this.escapeHtml(person.person_name || '未知人物')}</span>
                                <span class="memory-row-summary">${this.escapeHtml(person.summary || '资料正在整理')}</span>
                                <span class="memory-row-meta">${Number(person.observation_count || 0)} 条证据 · ${Number(person.fact_count || 0)} 条事实</span>
                            </span>
                            <i class="bi bi-chevron-right"></i>
                        </button>`).join('')}</div>`
                    : '<div class="memory-empty-state"><i class="bi bi-person-x"></i><span>没有匹配的人物资料</span></div>';
                pagination.innerHTML = this.renderMemoryPagination(
                    state.peoplePage,
                    pages,
                    'App.loadMemoryLibraryPeople'
                );
            } catch (error) {
                container.innerHTML = `<div class="alert alert-danger">读取人物失败：${this.escapeHtml(error.message)}</div>`;
            }
        },

        async openMemoryPersonDetail(personId) {
            const state = this._memoryLibraryState;
            if (!state?.userId) return;
            this.openMemoryDrawer('<i class="bi bi-person-circle me-2"></i>人物资料', '<div class="memory-empty-state"><span class="spinner-border spinner-border-sm"></span>正在读取人物证据…</div>');
            try {
                const data = responseData(await API.memory.getPerson(state.userId, personId));
                const person = data.profile || {};
                const facts = person.facts || [];
                const patterns = person.patterns || [];
                const observations = person.recent_observations || [];
                document.getElementById('memDrawerContent').innerHTML = `
                    <div class="memory-detail-stack">
                        <section><div class="memory-detail-eyebrow">人物 #${Number(person.person_id || 0)}</div>
                            <h4>${this.escapeHtml(person.person_name || '未知人物')}</h4>
                            <p class="text-muted">${this.escapeHtml((person.aliases || []).map(item => item.alias_name).filter(Boolean).join(' · '))}</p>
                        </section>
                        <section class="memory-detail-section">
                            <h6>当前资料</h6>
                            <div class="memory-profile-text">${this.escapeHtml(person.rendered_text || '资料正在整理')}</div>
                        </section>
                        <details class="memory-detail-section" open><summary>事实（${facts.length}）</summary>
                            <div class="memory-fact-list">${facts.map(fact => `
                                <div><span>${this.escapeHtml(fact.value || '')}</span>
                                    <small>${this.escapeHtml(fact.field_name || '')} · ${this.escapeHtml(fact.status || '')}</small>
                                    ${fact.manual_lock ? `<input class="form-control form-control-sm" name="fact_delete_reason" maxlength="1000" placeholder="删除原因"><button class="btn btn-sm btn-link text-danger" onclick="App.deleteMemoryPersonFact(${Number(person.person_id)}, ${Number(fact.id)}, this)">删除</button>` : ''}
                                </div>`).join('') || '<span class="text-muted">暂无结构化事实</span>'}</div>
                        </details>
                        ${patterns.length ? `<details class="memory-detail-section"><summary>稳定特点（${patterns.length}）</summary>
                            ${patterns.map(item => `<p>${this.escapeHtml(item.description || item.label || '')}</p>`).join('')}</details>` : ''}
                        <details class="memory-detail-section"><summary>来源证据（${observations.length}）</summary>
                            <div class="memory-observation-list">${observations.map(item => `
                                <div class="memory-observation-item">
                                    <p>${this.escapeHtml(item.statement || '')}</p>
                                    <small>${this.escapeHtml(item.observed_at || '')} · ${this.escapeHtml(item.quality_status || '')}</small>
                                    ${item.quality_status === 'active' ? `<div class="d-flex flex-wrap gap-2 mt-2"><input class="form-control form-control-sm flex-grow-1" name="review_reason" maxlength="1000" placeholder="隔离原因"><button class="btn btn-sm btn-outline-warning" onclick="App.reviewMemoryObservation(${Number(person.person_id)}, ${Number(item.id)}, 'quarantined', this)">隔离</button></div>` : ''}
                                </div>`).join('')}</div>
                        </details>
                        <details class="memory-detail-section"><summary>添加人工事实</summary>
                            <form onsubmit="event.preventDefault(); App.addMemoryPersonFact(${Number(person.person_id)}, this);">
                                <select class="form-select mb-2" name="field"><option value="other">一般事实</option><option value="preference">偏好</option><option value="occupation">职业</option><option value="location">地点</option><option value="plan">计划</option><option value="current_status">当前状态</option></select>
                                <textarea class="form-control mb-2" name="value" required maxlength="600"></textarea>
                                <input class="form-control mb-2" name="reason" required placeholder="添加原因">
                                <button class="btn btn-primary" type="submit">保存事实</button>
                            </form>
                        </details>
                        <details class="memory-detail-section"><summary>身份工具</summary>
                            <form class="mb-3" onsubmit="event.preventDefault(); App.addMemoryPersonAlias(${Number(person.person_id)}, this);">
                                <label class="form-label">添加确认别名</label>
                                <input class="form-control mb-2" name="alias_name" required maxlength="80" placeholder="昵称或曾用名">
                                <input class="form-control mb-2" name="reason" required minlength="2" maxlength="1000" placeholder="添加原因">
                                <button class="btn btn-outline-primary" type="submit">添加别名</button>
                            </form>
                            <form class="border-top pt-3" onsubmit="event.preventDefault(); App.mergeMemoryPerson(${Number(person.person_id)}, this);">
                                <label class="form-label">合并重复人物</label>
                                <input class="form-control mb-2" name="target_person_name" required maxlength="80" placeholder="要保留的准确姓名或确认别名">
                                <input class="form-control mb-2" name="reason" required minlength="2" maxlength="1000" placeholder="合并原因">
                                <div class="form-text mb-2">当前人物会合并到目标人物。操作可从“变更记录”撤销。</div>
                                <button class="btn btn-outline-danger" type="submit">合并到目标人物</button>
                            </form>
                        </details>
                    </div>`;
            } catch (error) {
                document.getElementById('memDrawerContent').innerHTML = `<div class="alert alert-danger">${this.escapeHtml(error.message)}</div>`;
            }
        },

        async reviewMemoryObservation(personId, observationId, status, trigger = null) {
            const state = this._memoryLibraryState;
            const row = trigger?.closest('.memory-observation-item, .memory-review-row');
            const reason = row?.querySelector('[name="review_reason"]')?.value?.trim() || '';
            if (!state?.userId) return;
            if (reason.length < 2) {
                UI.showError('请先填写至少 2 个字的纠正原因');
                row?.querySelector('[name="review_reason"]')?.focus();
                return;
            }
            try {
                await API.memory.reviewObservation(state.userId, personId, observationId, {
                    quality_status: status,
                    reason
                });
                UI.showSuccess('证据状态已更新');
                this.invalidateMemorySections(['overview', 'people', 'reviews', 'changes']);
                if (row?.classList.contains('memory-review-row')) {
                    await this.loadMemoryLibraryReviews(state.reviewPage || 1);
                } else {
                    await this.openMemoryPersonDetail(personId);
                }
            } catch (error) {
                UI.showError('更新证据失败：' + error.message);
            }
        },

        async addMemoryPersonFact(personId, form) {
            const state = this._memoryLibraryState;
            if (!state?.userId) return;
            try {
                await API.memory.addPersonFact(state.userId, personId, {
                    field: form.elements.field.value,
                    value: form.elements.value.value.trim(),
                    status: 'current',
                    sensitivity: 'low',
                    reason: form.elements.reason.value.trim()
                });
                UI.showSuccess('人物事实已保存');
                this.invalidateMemorySections(['overview', 'people', 'changes']);
                await this.openMemoryPersonDetail(personId);
            } catch (error) {
                UI.showError('保存人物事实失败：' + error.message);
            }
        },

        async deleteMemoryPersonFact(personId, factId, trigger = null) {
            const state = this._memoryLibraryState;
            const row = trigger?.closest('.memory-fact-list > div');
            const reason = row?.querySelector('[name="fact_delete_reason"]')?.value?.trim() || '';
            if (!state?.userId) return;
            if (reason.length < 2) {
                UI.showError('请先填写至少 2 个字的删除原因');
                row?.querySelector('[name="fact_delete_reason"]')?.focus();
                return;
            }
            try {
                await API.memory.deletePersonFact(state.userId, personId, factId, reason);
                UI.showSuccess('人物事实已删除');
                this.invalidateMemorySections(['overview', 'people', 'changes']);
                await this.openMemoryPersonDetail(personId);
            } catch (error) {
                UI.showError('删除人物事实失败：' + error.message);
            }
        },

        async addMemoryPersonAlias(personId, form) {
            const state = this._memoryLibraryState;
            if (!state?.userId || !form?.reportValidity()) return;
            try {
                await API.memory.addPersonAlias(state.userId, personId, {
                    alias_name: form.elements.alias_name.value.trim(),
                    reason: form.elements.reason.value.trim()
                });
                UI.showSuccess('人物别名已添加');
                this.invalidateMemorySections(['overview', 'people', 'changes']);
                await this.openMemoryPersonDetail(personId);
            } catch (error) {
                UI.showError('添加别名失败：' + error.message);
            }
        },

        async mergeMemoryPerson(personId, form) {
            const state = this._memoryLibraryState;
            if (!state?.userId || !form?.reportValidity()) return;
            if (!await UI.confirm('当前人物的证据、事实和别名会迁移到目标人物。确认继续吗？', {
                title: '合并重复人物', confirmText: '确认合并', variant: 'danger'
            })) return;
            try {
                await API.memory.mergePerson(state.userId, personId, {
                    target_person_name: form.elements.target_person_name.value.trim(),
                    reason: form.elements.reason.value.trim()
                });
                UI.showSuccess('重复人物已合并');
                this.closeMemoryDrawer();
                this.invalidateMemorySections(['overview', 'people', 'reviews', 'changes']);
                await this.loadMemoryLibraryPeople(1, true);
            } catch (error) {
                UI.showError('合并人物失败：' + error.message);
            }
        },

        async loadMemoryLibraryReviews(page = 1) {
            const state = this._memoryLibraryState;
            const container = document.getElementById('memoryLibraryReviewsContent');
            const pagination = document.getElementById('memoryLibraryReviewsPagination');
            if (!state?.userId || !container) return;
            const safePage = Math.max(1, Number(page) || 1);
            try {
                const data = responseData(await API.memory.getReviews(state.userId, {
                    offset: (safePage - 1) * state.limit,
                    limit: state.limit
                }));
                if (this._memoryLibraryState !== state) return;
                state.loadedSections.add('#memoryLibraryReviewsPane');
                const events = data.events?.items || [];
                const observations = data.observations?.items || [];
                const total = Math.max(
                    Number(data.events?.total || 0),
                    Number(data.observations?.total || 0)
                );
                const pages = Math.max(1, Math.ceil(total / state.limit));
                state.reviewPage = Math.min(safePage, pages);
                document.getElementById('memoryLibraryReviewCount').textContent =
                    (Number(data.events?.total || 0) + Number(data.observations?.total || 0)).toLocaleString();
                const reviewNotice = `<div class="memory-review-notice">
                    <i class="bi bi-shield-lock"></i>
                    <div><strong>这里是隔离区，不是必须清空的任务列表。</strong>
                    <span>未处理的内容会一直保持隔离，不会参与 AI 回答或人物画像。只有发现自动判断错误时，才需要使用下方入口进行纠正；日常无需处理。</span></div>
                </div>`;
                container.innerHTML = reviewNotice + ((!events.length && !observations.length)
                    ? '<div class="memory-empty-state"><i class="bi bi-shield-check"></i><span>目前没有自动隔离内容</span></div>'
                    : `<div class="memory-review-columns">
                        <section><h5>自动隔离记忆 <span>${Number(data.events?.total || 0)}</span></h5>
                            ${events.map(item => `<div class="memory-review-row"><button class="btn btn-link p-0 text-start" onclick="App.openMemoryEventDetail(${Number(item.id)})"><strong>${this.escapeHtml(item.title || '')}</strong></button><span>${this.escapeHtml(item.verification_note || '系统已自动排除')}</span><input class="form-control form-control-sm" name="event_review_reason" maxlength="1000" placeholder="纠正原因（必填）"><div class="d-flex gap-2"><button class="btn btn-sm btn-success" onclick="App.reviewMemoryEvent(${Number(item.id)}, 'approve', this)">纠正为有效</button><button class="btn btn-sm btn-outline-danger" onclick="App.reviewMemoryEvent(${Number(item.id)}, 'reject', this)">确认排除</button></div></div>`).join('') || '<p class="text-muted">暂无</p>'}
                        </section>
                        <section><h5>自动隔离人物证据 <span>${Number(data.observations?.total || 0)}</span></h5>
                            ${observations.map(item => `<div class="memory-review-row"><strong>${this.escapeHtml(item.person_name || '未知人物')}</strong><span>${this.escapeHtml(item.statement || '')}</span><input class="form-control form-control-sm" name="review_reason" maxlength="1000" placeholder="纠正原因（必填）"><div class="d-flex gap-2"><button class="btn btn-sm btn-success" onclick="App.reviewMemoryObservation(${Number(item.person_id)}, ${Number(item.id)}, 'active', this)">纠正为有效</button><button class="btn btn-sm btn-outline-danger" onclick="App.reviewMemoryObservation(${Number(item.person_id)}, ${Number(item.id)}, 'rejected', this)">确认排除</button></div></div>`).join('') || '<p class="text-muted">暂无</p>'}
                        </section>
                    </div>`);
                if (pagination) {
                    pagination.innerHTML = this.renderMemoryPagination(
                        state.reviewPage,
                        pages,
                        'App.loadMemoryLibraryReviews'
                    );
                }
            } catch (error) {
                container.innerHTML = `<div class="alert alert-danger">读取自动隔离内容失败：${this.escapeHtml(error.message)}</div>`;
            }
        },

        async reviewMemoryEvent(eventId, decision, trigger) {
            const state = this._memoryLibraryState;
            const row = trigger?.closest('.memory-review-row');
            const reason = row?.querySelector('[name="event_review_reason"]')?.value?.trim() || '';
            if (!state?.userId) return;
            if (reason.length < 2) {
                UI.showError('请先填写至少 2 个字的纠正原因');
                row?.querySelector('[name="event_review_reason"]')?.focus();
                return;
            }
            try {
                await API.memory.reviewEvent(state.userId, eventId, { decision, reason });
                UI.showSuccess(decision === 'approve' ? '事件记忆已纠正为有效' : '已确认排除该事件记忆');
                this.invalidateMemorySections(['overview', 'events', 'reviews', 'changes']);
                await this.loadMemoryLibraryReviews(state.reviewPage || 1);
            } catch (error) {
                UI.showError('复核事件失败：' + error.message);
            }
        },

        async loadMemoryLibraryChanges(page = 1) {
            const state = this._memoryLibraryState;
            const container = document.getElementById('memoryLibraryChangesContent');
            const pagination = document.getElementById('memoryLibraryChangesPagination');
            if (!state?.userId || !container) return;
            const safePage = Math.max(1, Number(page) || 1);
            try {
                const data = responseData(await API.memory.getChanges(state.userId, {
                    offset: (safePage - 1) * state.limit,
                    limit: state.limit
                }));
                const total = Number(data.total || 0);
                const pages = Math.max(1, Math.ceil(total / state.limit));
                state.changesPage = Math.min(safePage, pages);
                state.loadedSections.add('#memoryLibraryChangesPane');
                const labels = {
                    event: '事件记忆', stage: '阶段摘要', person_identity: '人物身份',
                    person_memory: '人物资料', maintenance: '维护操作'
                };
                container.innerHTML = (data.items || []).length
                    ? `<div class="memory-change-list">${data.items.map(item => {
                        const reversible = item.status === 'active'
                            && ['event', 'stage', 'person_identity'].includes(item.category);
                        return `<article class="memory-change-row"><span class="memory-change-type">${this.escapeHtml(labels[item.category] || item.category)}</span><div><strong>${this.escapeHtml(item.action || '')}</strong><p>${this.escapeHtml(item.reason || '')}</p><small>${this.escapeHtml(item.created_at || '')} · ${this.escapeHtml(item.status || '')}</small></div>${reversible ? `<button class="btn btn-sm btn-outline-danger" onclick="App.revertMemoryChange('${item.category}', ${Number(item.id)})">撤销</button>` : ''}</article>`;
                    }).join('')}</div>`
                    : '<div class="memory-empty-state"><i class="bi bi-journal"></i><span>暂无人工变更记录</span></div>';
                pagination.innerHTML = this.renderMemoryPagination(
                    state.changesPage,
                    pages,
                    'App.loadMemoryLibraryChanges'
                );
            } catch (error) {
                container.innerHTML = `<div class="alert alert-danger">读取变更记录失败：${this.escapeHtml(error.message)}</div>`;
            }
        },

        async revertMemoryChange(category, changeId) {
            const state = this._memoryLibraryState;
            if (!state?.userId) return;
            if (!await UI.confirm('撤销会恢复这次变更前的状态；若有更晚的相关修改，系统会拒绝操作。', {
                title: '撤销记忆变更', confirmText: '撤销', variant: 'danger'
            })) return;
            try {
                await API.memory.revertChange(state.userId, changeId, category);
                UI.showSuccess('变更已撤销');
                this.invalidateMemorySections(['overview', 'events', 'people', 'reviews', 'changes']);
                await this.loadMemoryLibraryChanges(state.changesPage || 1);
            } catch (error) {
                UI.showError('撤销失败：' + error.message);
            }
        },

        async loadMemoryLibraryMaintenance() {
            const state = this._memoryLibraryState;
            const container = document.getElementById('memoryLibraryMaintenanceContent');
            if (!state?.userId || !container) return;
            try {
                const data = responseData(await API.memory.getMaintenance(state.userId, 90));
                state.loadedSections.add('#memoryLibraryMaintenancePane');
                const categories = data.storage?.categories || {};
                const cleanup = data.candidate_cleanup || {};
                const integrity = data.integrity || {};
                const preview = data.clear_preview || {};
                const categoryLabels = {
                    events: '事件记忆', event_messages: '事件来源', people: '人物',
                    source_messages: '人物来源消息', message_links: '人物消息关联',
                    candidates: '人物候选', observations: '人物证据',
                    person_snapshots: '人物快照', corrections: '事件纠错快照',
                    identity_audits: '人物身份审计', projection_audits: '人物资料审计'
                };
                container.innerHTML = `
                    <div class="memory-maintenance-grid">
                        <section class="memory-maintenance-card">
                            <div class="memory-card-kicker">数据健康</div>
                            <h4>${integrity.ok ? '结构正常' : '发现一致性问题'}</h4>
                            <p>${integrity.ok ? '已检查事件来源、人物引用和活动快照。' : '请先备份数据库，再查看服务日志。'}</p>
                            <div class="memory-schema-tags">${Object.entries(integrity.schema_versions || {}).map(([key, value]) => `<span>${this.escapeHtml(key)} v${Number(value)}</span>`).join('')}</div>
                        </section>
                        <section class="memory-maintenance-card">
                            <div class="memory-card-kicker">可安全清理</div>
                            <h4>${Number(cleanup.rejected_candidate_count || 0).toLocaleString()} 条过期候选</h4>
                            <p>这些候选已经被证据核验拒绝，不属于正式记忆，预计释放 ${this.formatMemoryBytes(cleanup.estimated_bytes || 0)}。</p>
                            <button class="btn btn-outline-primary" type="button" onclick="App.cleanupMemoryCandidates()" ${cleanup.rejected_candidate_count ? '' : 'disabled'}>清理 90 天前候选</button>
                        </section>
                        <section class="memory-maintenance-card memory-storage-card">
                            <div class="memory-card-kicker">逻辑数据占用</div>
                            <div class="memory-storage-list">${Object.entries(categories).map(([key, item]) => `<div><span>${this.escapeHtml(categoryLabels[key] || key)}</span><strong>${Number(item.count || 0).toLocaleString()}</strong><small>${this.formatMemoryBytes(item.bytes || 0)}</small></div>`).join('')}</div>
                        </section>
                        <section class="memory-maintenance-card memory-maintenance-safe">
                            <div class="memory-card-kicker">操作保护</div>
                            <h4>先确认聊天，再执行维护</h4>
                            <p>请输入完整聊天名称 <code>${this.escapeHtml(preview.confirmation || '')}</code>。备份使用 SQLite 在线备份机制，包含整个记忆数据库，运行中的读写也能保持一致。</p>
                            <input class="form-control mb-3" id="memoryMaintenanceConfirmation" autocomplete="off" placeholder="输入聊天名称确认">
                            <button class="btn btn-outline-primary" type="button" onclick="App.createMemoryBackup(this)"><i class="bi bi-shield-check me-1"></i>创建清理前备份</button>
                            <div class="form-text mt-2">备份保存在 <code>${this.escapeHtml(data.backup?.directory || 'data/memory_backups')}</code>，不会自动上传。</div>
                        </section>
                        <section class="memory-maintenance-card memory-danger-card">
                            <div class="memory-card-kicker">危险区域</div>
                            <h4>清理当前聊天的记忆</h4>
                            <p>清理不会删除聊天原始日志；审计记录会保留，但已删除的记忆内容只能从数据库备份恢复。</p>
                            <div class="memory-danger-actions">
                                <button class="btn btn-outline-danger" onclick="App.clearMemoryScope('stage')">清理状态摘要</button>
                                <button class="btn btn-outline-danger" onclick="App.clearMemoryScope('events')">清理 ${Number(preview.events?.events || 0)} 条记忆</button>
                                <button class="btn btn-outline-danger" onclick="App.clearMemoryScope('people')">清理人物资料</button>
                                <button class="btn btn-danger" onclick="App.clearMemoryScope('all')">清理全部</button>
                            </div>
                        </section>
                    </div>`;
            } catch (error) {
                container.innerHTML = `<div class="alert alert-danger">读取维护状态失败：${this.escapeHtml(error.message)}</div>`;
            }
        },

        async createMemoryBackup(button) {
            const state = this._memoryLibraryState;
            const confirmation = document.getElementById('memoryMaintenanceConfirmation')?.value || '';
            if (!state?.userId) return;
            const original = button?.innerHTML;
            if (button) {
                button.disabled = true;
                button.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>正在备份';
            }
            try {
                const result = responseData(await API.memory.backup(state.userId, confirmation));
                UI.showSuccess(`备份已创建：${result.filename}（${this.formatMemoryBytes(result.bytes)}）`);
                this.invalidateMemorySections(['changes']);
            } catch (error) {
                UI.showError('创建备份失败：' + error.message);
            } finally {
                if (button) {
                    button.disabled = false;
                    button.innerHTML = original;
                }
            }
        },

        async cleanupMemoryCandidates() {
            const state = this._memoryLibraryState;
            const confirmation = document.getElementById('memoryMaintenanceConfirmation')?.value || '';
            if (!state?.userId) return;
            try {
                const result = responseData(await API.memory.cleanupCandidates(state.userId, {
                    retention_days: 90,
                    confirmation
                }));
                UI.showSuccess(`已清理 ${Number(result.deleted || 0).toLocaleString()} 条过期候选`);
                this.invalidateMemorySections(['overview', 'maintenance', 'changes']);
                await this.loadMemoryLibraryMaintenance();
            } catch (error) {
                UI.showError('清理失败：' + error.message);
            }
        },

        async clearMemoryScope(scope) {
            const state = this._memoryLibraryState;
            const confirmation = document.getElementById('memoryMaintenanceConfirmation')?.value || '';
            if (!state?.userId) return;
            if (!await UI.confirm('这是不可从记忆库撤销的数据清理。变更记录会保留，但相关记忆内容将被删除。', {
                title: '确认清理记忆', confirmText: '继续清理', variant: 'danger'
            })) return;
            try {
                await API.memory.clear(state.userId, { scope, confirmation });
                UI.showSuccess('记忆清理完成');
                this.closeMemoryDrawer();
                this._memoryLibraryState.loadedSections.clear();
                await this.loadMemoryLibraryMaintenance();
            } catch (error) {
                UI.showError('清理失败：' + error.message);
            }
        },

        invalidateMemorySections(names) {
            const state = this._memoryLibraryState;
            if (!state) return;
            const mapping = {
                overview: '#memoryLibraryOverviewPane', events: '#memoryLibraryEventsPane',
                people: '#memoryLibraryPeoplePane', reviews: '#memoryLibraryReviewsPane',
                changes: '#memoryLibraryChangesPane', maintenance: '#memoryLibraryMaintenancePane'
            };
            names.forEach(name => state.loadedSections.delete(mapping[name] || name));
        },

        openMemoryDrawer(titleHtml, contentHtml) {
            const drawer = document.getElementById('memoryLibraryInspectDrawer');
            const title = document.getElementById('memDrawerTitle');
            const content = document.getElementById('memDrawerContent');
            if (title) title.innerHTML = titleHtml;
            if (content) content.innerHTML = contentHtml;
            drawer?.classList.add('open');
        },

        closeMemoryDrawer() {
            document.getElementById('memoryLibraryInspectDrawer')?.classList.remove('open');
        }
    });
})();
