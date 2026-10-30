/* Setup lives beside the launcher: navigation never starts an installation. */
window.SetupWizardUI = (() => {
    'use strict';
    const steps = ['welcome', 'codex', 'components', 'finish'];
    const titles = [
        ['WELCOME TO MABOBOT', '让 Mabobot 准备就绪', '用几步完成配置，让常用功能真正可用。'],
        ['CONNECT YOUR AI', '连接你习惯的 AI', '先准备运行环境，再选择账号或 API。已有配置会保留。'],
        ['MAKE IT YOURS', '按需添加更多能力', '不必一次装齐。以后可以从设置中重新打开向导。'],
        ['READY WHEN YOU ARE', '看看准备得怎么样', '已配置的项目和待办事项都在这里，按需继续即可。'],
    ];
    const $ = id => document.getElementById(id);
    const all = selector => [...document.querySelectorAll(selector)];
    let api, refresh, notify, snapshot, opened = false, initialized = false, busy = false, step = 'welcome', previousFocus;
    let refreshing = false;
    const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));

    async function call(method, ...args) {
        const result = await api(method, ...args);
        if (result?.ok === false) throw new Error(result.error || '操作未完成');
        return result;
    }
    function error(message = '') { $('setupError').textContent = message; $('setupError').hidden = !message; }
    async function perform(action) {
        if (busy) return;
        busy = true; error();
        try { await action(); await refresh(); }
        catch (e) { error(e.message || String(e)); if (!opened) notify?.(e.message || String(e), 'error'); }
        finally { busy = false; render(snapshot); }
    }
    async function detect() {
        if (refreshing || busy) return;
        refreshing = true; error();
        try { await call('refresh_setup'); await refresh(); }
        catch (e) { error(e.message || String(e)); }
        finally { refreshing = false; }
    }
    function setOpen(value) {
        opened = value;
        $('setupWizard').hidden = !value;
        document.querySelector('.workspace').inert = value;
        if (value) {
            previousFocus = document.activeElement;
            $('setupTitle').focus();
        } else previousFocus?.focus();
    }
    function showStep(value, focus = true) {
        step = steps.includes(value) ? value : 'welcome';
        const index = steps.indexOf(step);
        const [eyebrow, title, subtitle] = titles[index];
        $('setupEyebrow').textContent = eyebrow;
        $('setupTitle').textContent = title;
        $('setupSubtitle').textContent = subtitle;
        all('[data-setup-panel]').forEach(panel => { panel.hidden = panel.dataset.setupPanel !== step; });
        all('[data-setup-step]').forEach(button => {
            const active = button.dataset.setupStep === step;
            button.classList.toggle('is-active', active);
            if (active) button.setAttribute('aria-current', 'step'); else button.removeAttribute('aria-current');
        });
        $('setupBack').hidden = index === 0;
        $('setupNext').innerHTML = index === 3 ? '保存并进入 Mabobot <span aria-hidden="true">↗</span>' : index === 0 ? '开始配置 <span aria-hidden="true">→</span>' : '下一步 <span aria-hidden="true">→</span>';
        if (focus) { $('setupScroll').scrollTop = 0; $('setupTitle').focus(); }
    }
    function move(value) {
        return perform(async () => { await call('save_setup_progress', 'active', value); showStep(value); });
    }
    function dismiss(status = 'skipped') {
        return perform(async () => { await call('save_setup_progress', status, step); setOpen(false); });
    }
    function open() {
        return perform(async () => {
            const saved = snapshot?.setup;
            const value = saved?.status === 'completed' ? 'welcome' : saved?.step || 'welcome';
            await call('save_setup_progress', 'active', value);
            showStep(value, false); setOpen(true);
            await call('refresh_setup');
        });
    }
    function render(data) {
        if (!data) return;
        snapshot = data;
        const state = data.setup;
        if (!state) return;
        $('setupEntryHint').textContent = state.status === 'completed' ? '已走完配置流程。可重新打开，检查环境或安装更多组件。' : state.status === 'skipped' ? '已保存进度，可随时回来继续配置。' : '检查环境、准备 Codex，并按需安装功能组件。';
        if (!initialized) {
            initialized = true;
            showStep(state.step, false);
            if (state.status === 'active') { setOpen(true); detect(); }
        }
        if (!opened) return;
        const checks = state.checks || {}, operation = state.operation || {};
        const running = operation.status === 'running';
        const waiting = state.checking || running || busy;
        const wsl = checks.wsl || {};
        let action = 'codex', label = '准备 Codex', help = '将打开 Linux 安装窗口。需要时输入 Linux 密码，完成后回到这里。';
        if (wsl.state === 'missing') {
            action = 'wsl'; label = '安装 WSL 与 Ubuntu';
            help = 'Windows 可能请求管理员权限。安装后若要求重启，请保存工作、重启，再双击 START.bat 继续。';
        } else if (['initialize', 'error'].includes(wsl.state)) {
            action = 'wsl_terminal'; label = '打开 Linux 完成初始化';
            help = '在打开的窗口创建 Linux 用户名和密码；完成后输入 exit 返回。若仍以 root 运行，请按安装帮助设置默认用户。';
        }
        const runtimeButton = $('setupRuntimeAction');
        document.querySelector('.setup-card-actions [data-setup-link]').dataset.setupLink = checks.use_wsl === false ? 'codex_help' : 'wsl_help';
        runtimeButton.dataset.setupInstall = action;
        runtimeButton.textContent = checks.codex?.ready ? '运行环境已找到' : label;
        runtimeButton.disabled = waiting || !wsl.state || Boolean(checks.codex?.ready) || checks.use_wsl === false;
        $('setupRuntimeDetail').textContent = state.checking ? '正在检查运行环境，请稍候…' : [wsl.detail, checks.codex?.detail].filter(Boolean).join(' · ') || '点击重新检测，检查运行环境。';
        $('setupRuntimeHelp').textContent = checks.codex?.ready ? '已有 Codex 可继续使用；接下来配置模型账号或接口。' : checks.use_wsl === false ? '当前使用本机 Codex。请按官方安装帮助准备 CLI，已有配置不会被向导改写。' : help;
        $('setupCodexBadge').textContent = checks.codex?.ready ? '已找到' : state.checking ? '检测中' : '待准备';
        $('setupCodexBadge').classList.toggle('is-ready', Boolean(checks.codex?.ready));
        $('setupProfileDot').className = 'presence-dot ' + (checks.profile?.ready ? 'running' : 'starting');
        $('setupProfileDetail').textContent = checks.profile?.detail || '配置完成后，返回这里重新检测。';
        all('[data-setup-install]').forEach(button => {
            if (button === runtimeButton) return;
            const key = button.dataset.setupInstall, ready = checks[key]?.ready;
            button.disabled = waiting || Boolean(ready) || (key === 'file_tools' && (checks.use_wsl === false || wsl.state !== 'ready'));
            button.textContent = ready ? '已就绪' : running && operation.action === key ? '安装中…' : operation.action === key && ['error', 'interrupted'].includes(operation.status) ? '重试安装' : '安装';
        });
        all('[data-setup-badge]').forEach(badge => {
            const ready = checks[badge.dataset.setupBadge]?.ready;
            badge.textContent = ready ? '已就绪' : state.checking ? '检测中' : '可选';
            badge.classList.toggle('is-ready', Boolean(ready));
        });
        all('[data-setup-detail]').forEach(detail => {
            const key = detail.dataset.setupDetail;
            detail.textContent = checks[key]?.detail || '';
            if (key === 'file_tools' && !checks[key]?.ready) detail.textContent += ' · 需要 Ubuntu / Debian，安装时可能要求 Linux 密码';
        });
        all('[data-setup-refresh]').forEach(button => { button.disabled = waiting; button.textContent = state.checking ? '检测中…' : '重新检测'; });
        all('[data-requires-cli]').forEach(button => { button.disabled = !checks.codex?.ready; });
        const items = [
            ['基础环境', (data.environment || []).filter(c => ['python', 'dependencies'].includes(c.key)).every(c => c.ready), 'Python 与项目依赖'],
            ['Codex', checks.codex?.ready, checks.codex?.detail],
            ['默认模型', checks.profile?.ready, checks.profile?.detail],
            ['浏览器', checks.browser?.ready, checks.browser?.detail],
            ['媒体组件', checks.media?.ready, checks.media?.detail],
            ['文件处理', checks.file_tools?.ready, checks.file_tools?.detail],
        ];
        $('setupSummary').innerHTML = items.map(([name, ready, detail]) => `<div><span class="setup-summary-symbol ${ready ? 'is-ready' : ''}">${ready ? '✓' : '·'}</span><strong>${esc(name)}</strong><span>${esc(detail || '尚未检测')}</span><small>${ready ? '已准备' : '可稍后配置'}</small></div>`).join('');
        $('setupOperation').hidden = !operation.status;
        $('setupOperation').classList.toggle('is-running', running);
        $('setupOperation').classList.toggle('is-error', ['error', 'interrupted'].includes(operation.status));
        $('setupOperationTitle').textContent = running ? '正在准备，请稍候' : operation.status === 'error' ? '这一步需要处理' : operation.status === 'interrupted' ? '上次安装未确认完成' : operation.status === 'succeeded' ? '组件已就绪' : '安装步骤已结束';
        $('setupOperationMessage').textContent = (operation.message || '') + (running && operation.elapsed_seconds >= 60 ? ` 已等待 ${Math.floor(operation.elapsed_seconds / 60)} 分钟。` : '');
        $('setupLogTail').hidden = !operation.log_tail;
        $('setupLogTail').textContent = operation.log_tail || '';
        if (checks.error && !busy) error(checks.error);
    }
    function init(callApi, refreshNow, showToast) {
        api = callApi; refresh = refreshNow; notify = showToast;
        document.addEventListener('click', event => {
            const target = event.target.closest('button');
            if (!target) return;
            if (target.hasAttribute('data-setup-open')) return open();
            if (!opened) return;
            if (target.dataset.setupStep) return move(target.dataset.setupStep);
            if (target.hasAttribute('data-setup-refresh')) return detect();
            if (target.dataset.setupInstall) return perform(() => call('install_setup_component', target.dataset.setupInstall));
            if (target.dataset.setupLink) return perform(() => call('open_setup_page', target.dataset.setupLink));
        });
        $('setupSkip').addEventListener('click', () => dismiss());
        $('setupBack').addEventListener('click', () => move(steps[Math.max(0, steps.indexOf(step) - 1)]));
        $('setupNext').addEventListener('click', () => step === 'finish' ? dismiss('completed') : move(steps[steps.indexOf(step) + 1]));
        $('setupLogs').addEventListener('click', () => perform(() => call('open_logs_folder')));
        window.addEventListener('focus', () => { if (opened && !snapshot?.setup?.operation?.status?.includes('running')) detect(); });
        document.addEventListener('keydown', event => {
            if (!opened || !$('confirmModal').hidden) return;
            if (event.key === 'Escape') { event.preventDefault(); dismiss(); }
            if (event.key === 'Tab') {
                const controls = [...$('setupWizard').querySelectorAll('button, summary, a[href]')].filter(el => !el.disabled && el.getClientRects().length);
                const first = controls[0], last = controls[controls.length - 1];
                if (event.shiftKey && (document.activeElement === first || document.activeElement === $('setupTitle'))) { event.preventDefault(); last?.focus(); }
                else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
            }
        });
    }
    return { init, render, open };
})();
