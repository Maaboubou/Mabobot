/* A real ttyd terminal. Profile credentials stay in the local launch wrapper. */
const TerminalPage = {
    data: null,
    profiles: [],
    selectedId: '',
    pending: false,
    loading: null,
    esc(value) { return UI.escapeHtml(String(value ?? '')); },

    async load({ quiet = false } = {}) {
        const root = document.getElementById('terminalRoot');
        if (!root) return;
        this.mount(root);
        if (this.loading) return this.loading;
        this.loading = this.fetchState(quiet).finally(() => { this.loading = null; });
        return this.loading;
    },

    mount(root) {
        if (root.dataset.mounted) return;
        root.dataset.mounted = 'true';
        root.innerHTML = `<div class="terminal-toolbar">
            <form id="terminalCreateForm" class="terminal-create-form">
                <label for="terminalProfile" class="visually-hidden">Codex Profile</label>
                <select id="terminalProfile" name="profile_id" class="form-select form-select-sm" required aria-label="Codex Profile"><option value="">正在加载…</option></select>
                <button class="btn btn-primary btn-sm" type="submit" id="terminalCreateButton" aria-label="新建会话" title="新建会话" disabled><i class="bi bi-plus-lg" aria-hidden="true"></i><span class="terminal-button-label">新建会话</span></button>
                <label class="terminal-access-option" title="允许 Codex 执行命令并修改本机文件；取消勾选时沿用 Profile 的权限设置。"><input id="terminalFullAccess" name="full_access" class="form-check-input" type="checkbox" checked aria-describedby="terminalAccessDescription"><span>完整访问</span></label>
            </form>
            <button type="button" class="btn btn-outline-secondary btn-sm terminal-icon-button" id="terminalRefreshButton" aria-label="刷新状态" title="刷新状态"><i class="bi bi-arrow-clockwise" aria-hidden="true"></i></button>
            <details class="terminal-info"><summary aria-label="终端说明" title="终端说明"><i class="bi bi-info-circle" aria-hidden="true"></i></summary><div class="terminal-info-content">
                <p id="terminalAccessDescription">完整访问允许 Codex 执行命令并修改本机文件。取消勾选时沿用 Profile 的权限设置。</p>
                <p>离开页面会保留会话；“结束会话”会停止对应的 Codex 进程。</p>
                <div class="terminal-location" id="terminalLocation"></div>
            </div></details>
        </div>
        <div id="terminalMessage" class="terminal-message" role="status" aria-live="polite" hidden></div>
        <div id="terminalDependencies" class="terminal-dependencies" hidden></div>
        <div class="terminal-session-bar" hidden>
            <div class="terminal-session-tabs" id="terminalSessionTabs" role="tablist" aria-label="终端会话"></div>
            <div class="terminal-session-actions"><button type="button" class="btn btn-outline-secondary btn-sm" id="terminalReconnectButton" aria-label="重新连接" title="重新连接" disabled><i class="bi bi-plug" aria-hidden="true"></i><span class="terminal-button-label">重新连接</span></button><button type="button" class="btn btn-outline-danger btn-sm" id="terminalStopButton" aria-label="结束会话" title="结束会话" disabled><i class="bi bi-stop-circle" aria-hidden="true"></i><span class="terminal-button-label">结束会话</span></button></div>
        </div>
        <div class="terminal-display" id="terminalDisplay" role="tabpanel" aria-label="终端内容"><div class="terminal-empty" id="terminalEmpty"><i class="bi bi-terminal" aria-hidden="true"></i><p>选择 Profile，新建本机 Codex 会话。</p><span>切换页面或刷新浏览器后，可以继续连接正在运行的会话。</span></div></div>`;
        root.querySelector('form').addEventListener('submit', event => { event.preventDefault(); this.create(); });
        root.querySelector('#terminalRefreshButton').addEventListener('click', () => this.load());
        root.querySelector('#terminalStopButton').addEventListener('click', () => this.stop());
        root.querySelector('#terminalReconnectButton').addEventListener('click', () => this.reconnect());
        root.querySelector('#terminalSessionTabs').addEventListener('click', event => {
            const button = event.target.closest('[data-terminal-session]');
            if (button) this.select(button.dataset.terminalSession, true);
        });
        root.querySelector('#terminalSessionTabs').addEventListener('keydown', event => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            const sessions = this.sessions();
            if (!sessions.length) return;
            event.preventDefault();
            const current = sessions.findIndex(session => session.id === this.selectedId);
            const index = event.key === 'Home' ? 0 : event.key === 'End' ? sessions.length - 1
                : (current + (event.key === 'ArrowRight' ? 1 : -1) + sessions.length) % sessions.length;
            this.select(sessions[index].id);
            document.getElementById(`terminal-tab-${sessions[index].id}`)?.focus();
        });
    },

    async fetchState(quiet) {
        const refresh = document.getElementById('terminalRefreshButton');
        refresh.disabled = true;
        try {
            const [data, listing] = await Promise.all([
                API.get('/api/terminal'), API.codexProfiles.list()
            ]);
            this.data = data;
            this.profiles = (listing.profiles || []).filter(profile => profile.available);
            const select = document.getElementById('terminalProfile');
            const selected = this.profiles.some(profile => profile.name === select.value) ? select.value
                : this.profiles.some(profile => profile.name === listing.default_profile_id) ? listing.default_profile_id
                    : this.profiles[0]?.name || '';
            select.innerHTML = this.profiles.length ? this.profiles.map(profile => `<option value="${this.esc(profile.name)}">${this.esc(profile.name)}</option>`).join('') : '<option value="">暂无可用 Profile</option>';
            select.value = selected;
            this.renderState();
            this.message(!this.profiles.length ? '请先在 Codex 页面完成 Profile 登录或密钥配置。'
                : data.available === false ? data.message || '' : '');
        } catch (error) {
            if (!quiet || !this.data) this.message(`无法读取终端状态：${error.message}`, true);
        } finally {
            refresh.disabled = false;
            this.updateControls();
        }
    },

    message(text = '', error = false) {
        const root = document.getElementById('terminalMessage');
        if (!root) return;
        root.textContent = text;
        root.classList.toggle('text-danger', error);
        root.hidden = !text;
    },

    renderState() {
        const data = this.data || {};
        const location = document.getElementById('terminalLocation');
        location.textContent = [data.runtime, data.project_dir].filter(Boolean).join(' · ');
        const dependencies = document.getElementById('terminalDependencies');
        const missing = (data.dependencies || []).filter(item => !item.available);
        dependencies.hidden = data.available !== false;
        dependencies.innerHTML = data.available === false ? `<strong>终端环境尚未就绪</strong><p>${missing.length ? `缺少 ${missing.map(item => this.esc(item.name)).join('、')}。` : ''}请在 Codex 所在的 Linux / WSL 环境准备终端工具，然后刷新状态。</p><code>sudo apt install ttyd tmux</code>` : '';
        this.renderSessions();
    },

    validSession(session) {
        return session && typeof session.id === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(session.id);
    },

    sessions() {
        return (this.data?.sessions || []).filter(session => this.validSession(session));
    },

    renderSessions() {
        const sessions = this.sessions();
        if (!sessions.some(session => session.id === this.selectedId)) {
            this.selectedId = sessions.find(session => session.status === 'running')?.id || sessions[0]?.id || '';
        }
        const tabs = document.getElementById('terminalSessionTabs');
        tabs.closest('.terminal-session-bar').hidden = !sessions.length;
        tabs.innerHTML = sessions.map((session, index) => `<button type="button" role="tab" class="terminal-session-tab ${session.id === this.selectedId ? 'active' : ''}" data-terminal-session="${this.esc(session.id)}" id="terminal-tab-${this.esc(session.id)}" aria-selected="${session.id === this.selectedId}" aria-controls="terminalDisplay" tabindex="${session.id === this.selectedId ? '0' : '-1'}"><span class="terminal-session-dot ${session.status === 'running' ? 'is-running' : ''}" aria-hidden="true"></span><span>${this.esc(session.profile_id || 'Codex')} · ${index + 1}</span><small>${session.status === 'running' ? '运行中' : '已结束'}</small></button>`).join('');
        document.querySelectorAll('#terminalDisplay iframe').forEach(frame => {
            if (!sessions.some(session => session.id === frame.dataset.terminalFrame && session.status === 'running')) frame.remove();
        });
        this.showSelected();
        this.updateControls();
    },

    select(identifier, focus = false) {
        if (!this.sessions().some(session => session.id === identifier)) return;
        this.selectedId = identifier;
        this.renderSessions();
        if (focus) document.querySelector(`#terminalDisplay iframe[data-terminal-frame="${CSS.escape(identifier)}"]`)?.focus();
    },

    showSelected() {
        const session = this.sessions().find(item => item.id === this.selectedId);
        const display = document.getElementById('terminalDisplay');
        if (session) display.setAttribute('aria-labelledby', `terminal-tab-${session.id}`);
        else display.removeAttribute('aria-labelledby');
        const empty = document.getElementById('terminalEmpty');
        document.querySelectorAll('#terminalDisplay iframe').forEach(frame => { frame.hidden = frame.dataset.terminalFrame !== this.selectedId; });
        const path = session?.url;
        const validPath = typeof path === 'string' && path === `/api/terminal/sessions/${session.id}/view/`;
        if (!session || session.status !== 'running' || !validPath) {
            empty.hidden = false;
            empty.querySelector('p').textContent = session ? '此会话已结束，请新建会话。' : '选择 Profile，新建本机 Codex 会话。';
            return;
        }
        empty.hidden = true;
        let frame = display.querySelector(`[data-terminal-frame="${CSS.escape(session.id)}"]`);
        if (!frame) {
            frame = document.createElement('iframe');
            frame.dataset.terminalFrame = session.id;
            frame.src = path;
            frame.title = `Codex 终端：${session.profile_id || session.id}`;
            frame.className = 'terminal-frame';
            frame.setAttribute('allow', 'clipboard-read; clipboard-write; fullscreen');
            frame.referrerPolicy = 'same-origin';
            display.append(frame);
        }
        frame.hidden = false;
    },

    updateControls() {
        const create = document.getElementById('terminalCreateButton');
        if (!create) return;
        const session = this.sessions().find(item => item.id === this.selectedId);
        create.disabled = this.pending || !this.data?.available || !this.profiles.length;
        document.getElementById('terminalProfile').disabled = this.pending;
        document.getElementById('terminalFullAccess').disabled = this.pending;
        document.getElementById('terminalStopButton').disabled = this.pending || !session;
        document.getElementById('terminalReconnectButton').disabled = this.pending || session?.status !== 'running';
    },

    async create() {
        if (this.pending || !this.data?.available) return;
        const form = document.getElementById('terminalCreateForm');
        if (!form.reportValidity()) return;
        this.pending = true;
        this.message('正在启动本机 Codex…');
        this.updateControls();
        try {
            const session = await API.post('/api/terminal/sessions', {
                profile_id: form.elements.profile_id.value,
                full_access: form.elements.full_access.checked
            });
            if (!this.validSession(session)) throw Error('终端服务未返回有效会话');
            this.data.sessions = [...this.sessions().filter(item => item.id !== session.id), session];
            this.selectedId = session.id;
            this.renderSessions();
            this.message();
        } catch (error) {
            this.message(`启动失败：${error.message}`, true);
        } finally {
            this.pending = false;
            this.updateControls();
        }
    },

    reconnect() {
        if (this.pending) return;
        const session = this.sessions().find(item => item.id === this.selectedId);
        if (!session || session.status !== 'running') return;
        document.querySelector(`#terminalDisplay iframe[data-terminal-frame="${CSS.escape(session.id)}"]`)?.remove();
        this.showSelected();
        this.message();
    },

    async stop() {
        if (this.pending || !this.selectedId) return;
        const identifier = this.selectedId;
        this.pending = true;
        this.updateControls();
        this.message('正在结束会话…');
        try {
            await API.delete(`/api/terminal/sessions/${encodeURIComponent(identifier)}`);
            this.data.sessions = this.sessions().filter(item => item.id !== identifier);
            this.renderSessions();
            this.message();
        } catch (error) {
            this.message(`结束失败：${error.message}`, true);
        } finally {
            this.pending = false;
            this.updateControls();
        }
    }
};
