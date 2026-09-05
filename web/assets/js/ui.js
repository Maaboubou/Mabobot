/**
 * UI Module
 * Handles DOM manipulation and rendering
 */

const UI = {
    sidebarStorageKey: 'mabobot.sidebarCollapsed',
    themeStorageKey: 'mabobot.colorTheme',
    routes: {
        dashboard: '/',
        codex: '/codex',
        users: '/chats',
        roles: '/assistant',
        plugins: '/plugins',
        llm: '/ai',
        logs: '/operations/logs',
        settings: '/system'
    },
    routeAliases: {
        '/dashboard': 'dashboard',
        '/codex/profiles': 'codex',
        '/codex/runs': 'codex',
        '/users': 'users',
        '/roles': 'roles',
        '/automations': 'plugins',
        '/llm': 'llm',
        '/logs': 'logs',
        '/settings': 'settings',
        '/system/providers': 'settings',
        '/system/integrations': 'settings',
        '/system/runtime': 'settings',
        '/system/developer': 'settings',
        '/system/operations': 'settings',
        '/system/tools': 'settings',
        '/system/backups': 'settings',
        '/assistant/roles': 'roles',
        '/assistant/chats': 'roles',
        '/assistant/memory': 'roles',
        '/ai/models': 'llm',
        '/ai/mappings': 'llm',
        '/ai/usage': 'llm',
        '/ai/sessions': 'codex',
        '/ai/calls': 'llm',
        '/ai/network': 'llm',
        '/operations': 'logs'
    },

    // Icons mapping
    icons: {
        cpu: 'bi-cpu',
        memory: 'bi-memory',
        disk: 'bi-hdd',
        time: 'bi-clock',
        check: 'bi-check-circle-fill',
        error: 'bi-x-circle-fill',
        plugin: 'bi-puzzle',
        user: 'bi-person'
    },

    // Initialize UI components
    init() {
        this.setupTheme();

        // Mobile Sidebar
        const toggleBtn = document.querySelector('.mobile-toggle');
        const overlay = document.querySelector('.mobile-overlay');
        const sidebar = document.getElementById('sidebarNavigation');
        const mobileCloseBtn = document.getElementById('sidebarMobileClose');
        const collapseBtn = document.getElementById('sidebarCollapseToggle');
        if (toggleBtn) {
            toggleBtn.addEventListener('click', () => {
                UI.toggleSidebar(!sidebar?.classList.contains('show'));
            });
        }
        if (overlay) {
            overlay.addEventListener('click', () => UI.toggleSidebar(false));
        }
        mobileCloseBtn?.addEventListener('click', () => UI.toggleSidebar(false));
        sidebar?.addEventListener('keydown', event => UI.handleMobileSidebarKeydown(event));
        const mobileSheetHandle = sidebar?.querySelector('.sidebar-brand');
        mobileSheetHandle?.addEventListener('touchstart', event => {
            const touch = event.changedTouches[0];
            UI.mobileNavTouchStart = touch ? { x: touch.clientX, y: touch.clientY } : null;
        }, { passive: true });
        mobileSheetHandle?.addEventListener('touchend', event => {
            const start = UI.mobileNavTouchStart;
            const touch = event.changedTouches[0];
            UI.mobileNavTouchStart = null;
            if (!start || !touch) return;
            const deltaX = touch.clientX - start.x;
            const deltaY = touch.clientY - start.y;
            if (deltaY > 64 && deltaY > Math.abs(deltaX) * 1.2) UI.toggleSidebar(false);
        }, { passive: true });
        this.syncMobileNavigationMode();
        window.addEventListener('resize', this.debounce(() => this.syncMobileNavigationMode(), 120));
        if (collapseBtn) {
            collapseBtn.addEventListener('click', () => UI.toggleDesktopSidebar());

            let shouldCollapse = false;
            try {
                shouldCollapse = localStorage.getItem(this.sidebarStorageKey) === 'true';
            } catch (e) {
                console.warn('Could not restore sidebar state:', e);
            }
            this.toggleDesktopSidebar(shouldCollapse, false);
        }

        // Tab Navigation
        document.querySelectorAll('.nav-link[data-tab]').forEach(link => {
            link.addEventListener('click', (e) => {
                e.preventDefault();
                UI.switchTab(link.dataset.tab);
            });
        });

        window.addEventListener('popstate', () => {
            UI.switchTab(UI.getInitialTab(), { history: false });
        });

        // Initialize Tooltips if Bootstrap is available
        if (typeof bootstrap !== 'undefined' && bootstrap.Tooltip) {
            const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
            tooltipTriggerList.map(function (tooltipTriggerEl) {
                return new bootstrap.Tooltip(tooltipTriggerEl);
            });
        }
    },

    getSystemTheme() {
        return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    },

    getSavedTheme() {
        try {
            const value = localStorage.getItem(this.themeStorageKey);
            return ['light', 'dark'].includes(value) ? value : null;
        } catch (e) {
            console.warn('Could not restore color theme:', e);
            return null;
        }
    },

    applyTheme(theme, persist = false) {
        const nextTheme = theme === 'dark' ? 'dark' : 'light';
        document.documentElement.dataset.theme = nextTheme;
        document.documentElement.setAttribute('data-bs-theme', nextTheme);
        document.querySelector('meta[name="theme-color"]')
            ?.setAttribute('content', getComputedStyle(document.documentElement).getPropertyValue('--bg-body').trim());

        if (persist) {
            try {
                localStorage.setItem(this.themeStorageKey, nextTheme);
            } catch (e) {
                console.warn('Could not save color theme:', e);
            }
        }

        const button = document.getElementById('themeToggle');
        if (!button) return;
        const isDark = nextTheme === 'dark';
        const actionLabel = isDark ? '切换到亮色模式' : '切换到暗色模式';
        button.setAttribute('aria-label', actionLabel);
        button.setAttribute('aria-pressed', String(isDark));
        button.title = actionLabel;
        const icon = button.querySelector('i');
        if (icon) icon.className = `bi ${isDark ? 'bi-sun' : 'bi-moon-stars'}`;
        const label = button.querySelector('[data-theme-label]');
        if (label) label.textContent = isDark ? '亮色' : '暗色';
    },

    setupTheme() {
        this.applyTheme(this.getSavedTheme() || this.getSystemTheme());
        document.getElementById('themeToggle')?.addEventListener('click', () => {
            const currentTheme = document.documentElement.dataset.theme || 'light';
            this.applyTheme(currentTheme === 'dark' ? 'light' : 'dark', true);
        });

        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', event => {
            if (!this.getSavedTheme()) this.applyTheme(event.matches ? 'dark' : 'light');
        });
    },

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    },

    debounce(fn, delay = 300) {
        let timer = null;
        return (...args) => {
            clearTimeout(timer);
            timer = setTimeout(() => fn(...args), delay);
        };
    },

    toggleSidebar(show, options = {}) {
        const sidebar = document.getElementById('sidebarNavigation');
        const overlay = document.querySelector('.mobile-overlay');
        const toggleBtn = document.querySelector('.mobile-toggle');
        if (!sidebar || !overlay) return;
        const isMobile = window.innerWidth <= 768;
        const wasOpen = sidebar.classList.contains('show');
        const shouldShow = Boolean(show) && isMobile;
        if (shouldShow && !wasOpen) {
            this.mobileNavReturnFocus = document.activeElement instanceof HTMLElement
                ? document.activeElement
                : toggleBtn;
        }
        sidebar.classList.toggle('show', shouldShow);
        overlay.classList.toggle('show', shouldShow);
        document.body.classList.toggle('mobile-nav-open', shouldShow);
        sidebar.inert = isMobile && !shouldShow;
        if (isMobile) sidebar.setAttribute('aria-hidden', shouldShow ? 'false' : 'true');
        else sidebar.removeAttribute('aria-hidden');
        if (toggleBtn) {
            const actionLabel = shouldShow ? '关闭主导航' : '打开主导航';
            toggleBtn.setAttribute('aria-expanded', String(shouldShow));
            toggleBtn.setAttribute('aria-label', actionLabel);
            toggleBtn.title = actionLabel;
            toggleBtn.querySelector('[data-mobile-nav-label]')?.replaceChildren(document.createTextNode(actionLabel));
        }
        if (shouldShow) {
            window.requestAnimationFrame(() => {
                const activeLink = sidebar.querySelector('.nav-link.active');
                (activeLink || document.getElementById('sidebarMobileClose'))?.focus({ preventScroll: true });
            });
        } else if (wasOpen && options.restoreFocus !== false) {
            const returnFocus = this.mobileNavReturnFocus?.isConnected
                ? this.mobileNavReturnFocus
                : toggleBtn;
            window.requestAnimationFrame(() => returnFocus?.focus({ preventScroll: true }));
        }
    },

    syncMobileNavigationMode() {
        const sidebar = document.getElementById('sidebarNavigation');
        const overlay = document.querySelector('.mobile-overlay');
        const toggleBtn = document.querySelector('.mobile-toggle');
        if (!sidebar || !overlay) return;
        const updateTrigger = expanded => {
            if (!toggleBtn) return;
            const actionLabel = expanded ? '关闭主导航' : '打开主导航';
            toggleBtn.setAttribute('aria-expanded', String(expanded));
            toggleBtn.setAttribute('aria-label', actionLabel);
            toggleBtn.title = actionLabel;
            toggleBtn.querySelector('[data-mobile-nav-label]')?.replaceChildren(document.createTextNode(actionLabel));
        };
        const isMobile = window.innerWidth <= 768;
        if (!isMobile) {
            sidebar.classList.remove('show');
            overlay.classList.remove('show');
            document.body.classList.remove('mobile-nav-open');
            sidebar.inert = false;
            sidebar.removeAttribute('aria-hidden');
            updateTrigger(false);
            return;
        }
        const open = sidebar.classList.contains('show');
        sidebar.inert = !open;
        sidebar.setAttribute('aria-hidden', open ? 'false' : 'true');
        overlay.classList.toggle('show', open);
        document.body.classList.toggle('mobile-nav-open', open);
        updateTrigger(open);
    },

    handleMobileSidebarKeydown(event) {
        const sidebar = document.getElementById('sidebarNavigation');
        if (!sidebar?.classList.contains('show') || window.innerWidth > 768) return;
        if (event.key === 'Escape') {
            event.preventDefault();
            this.toggleSidebar(false);
            return;
        }
        if (event.key !== 'Tab') return;
        const focusable = [...sidebar.querySelectorAll('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])')]
            .filter(element => !element.hidden && element.getClientRects().length);
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    },

    toggleDesktopSidebar(collapsed, persist = true) {
        const sidebar = document.querySelector('.sidebar');
        const toggleBtn = document.getElementById('sidebarCollapseToggle');
        if (!sidebar || !toggleBtn) return;

        const shouldCollapse = typeof collapsed === 'boolean'
            ? collapsed
            : !sidebar.classList.contains('is-collapsed');
        sidebar.classList.toggle('is-collapsed', shouldCollapse);

        const actionLabel = shouldCollapse ? '展开侧边栏' : '折叠侧边栏';
        toggleBtn.setAttribute('aria-label', actionLabel);
        toggleBtn.setAttribute('aria-expanded', String(!shouldCollapse));
        toggleBtn.title = actionLabel;

        document.querySelectorAll('.sidebar .nav-link[data-tab]').forEach(link => {
            const label = link.querySelector('span')?.textContent?.trim();
            if (!label) return;
            link.setAttribute('aria-label', label);
            if (shouldCollapse) {
                link.title = label;
            } else {
                link.removeAttribute('title');
            }
        });

        if (persist) {
            try {
                localStorage.setItem(this.sidebarStorageKey, String(shouldCollapse));
            } catch (e) {
                console.warn('Could not save sidebar state:', e);
            }
        }
    },

    normalizePath(pathname) {
        if (!pathname || pathname === '/index.html') return '/';
        const normalized = pathname.replace(/\/+$/, '');
        return normalized || '/';
    },

    getInitialTab() {
        const pathname = this.normalizePath(window.location.pathname);
        const direct = Object.entries(this.routes).find(([, path]) => path === pathname);
        if (direct) return direct[0];
        return this.routeAliases[pathname] || 'dashboard';
    },

    switchTab(tabId, options = {}) {
        if (!this.routes[tabId]) tabId = 'dashboard';
        const logsPageActive = tabId === 'logs';
        document.body.classList.toggle('logs-page-active', logsPageActive);
        if (logsPageActive && window.innerWidth <= 768) {
            window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
            const mainContent = document.querySelector('.main-content');
            if (mainContent) mainContent.scrollTop = 0;
        }
        // Update Sidebar
        document.querySelectorAll('.nav-link').forEach(el => {
            el.classList.remove('active');
            el.removeAttribute('aria-current');
        });
        const activeLink = document.querySelector(`.nav-link[data-tab="${tabId}"]`);
        if (activeLink) {
            activeLink.classList.add('active');
            activeLink.setAttribute('aria-current', 'page');
        }

        // Update Content
        // specific selector to avoid hiding nested tab-content (like in LLM manager)
        document.querySelectorAll('.main-content > .tab-content').forEach(el => el.classList.add('d-none'));
        const targetTab = document.getElementById(tabId);
        if (targetTab) {
            targetTab.classList.remove('d-none');
            // Trigger load data for this tab
            window.App.loadTab(tabId);
        }

        if (options.history !== false) {
            const targetPath = this.routes[tabId];
            if (this.normalizePath(window.location.pathname) !== targetPath) {
                window.history.pushState({ tab: tabId }, '', targetPath);
            }
        }

        // Close sidebar on mobile
        if (window.innerWidth <= 768) UI.toggleSidebar(false);

        // Update Title
        const titleMap = {
            'dashboard': '概览',
            'codex': 'Codex 运行中心',
            'plugins': '功能插件',
            'users': '聊天管理',
            'roles': 'Bot 设定',
            'settings': '系统',
            'wechat': 'WeChat 状态',
            'logs': '运行与日志',
            'llm': '模型配置'
        };
        const currentTitle = titleMap[tabId] || '概览';
        document.getElementById('pageTitle').textContent = currentTitle;
        const mobileNavCurrent = document.querySelector('[data-mobile-nav-current]');
        if (mobileNavCurrent) mobileNavCurrent.textContent = `当前页面 · ${currentTitle}`;
    },

    showLoading(elementId) {
        const el = document.getElementById(elementId);
        if (el) {
            el.innerHTML = `
                <div class="loading-wrapper">
                    <div class="spinner-custom"></div>
                    <p>正在加载数据…</p>
                </div>
            `;
        }
    },

    showLoadingOverlay(message) {
        let overlay = document.querySelector('.mobile-overlay');
        // Create full screen overlay if it's just the sidebar one or doesn't exist
        // or just reuse a new specific one

        // Remove existing if any (to avoid duplicates/conflicts)
        const old = document.getElementById('system-loading-overlay');
        if (old) old.remove();

        const div = document.createElement('div');
        div.id = 'system-loading-overlay';
        div.style.position = 'fixed';
        div.style.top = '0';
        div.style.left = '0';
        div.style.width = '100vw';
        div.style.height = '100vh';
        div.style.backgroundColor = 'rgba(var(--ink-rgb), 0.85)';
        div.style.zIndex = '9999';
        div.style.display = 'flex';
        div.style.flexDirection = 'column';
        div.style.alignItems = 'center';
        div.style.justifyContent = 'center';
        div.style.color = 'var(--text-on-dark)';
        div.style.backdropFilter = 'blur(5px)';

        div.innerHTML = `
            <div class="spinner-border text-light mb-4" style="width: 3rem; height: 3rem;" role="status"></div>
            <h4 class="fw-light">${this.escapeHtml(message || '正在加载…')}</h4>
            <div class="small text-white-50 mt-2">页面会自动重新加载。</div>
        `;

        document.body.appendChild(div);
    },

    showRestartOverlay(title = '系统重启中', statusMessage = '正在重启服务…') {
        const old = document.getElementById('system-restart-overlay');
        if (old) old.remove();

        const div = document.createElement('div');
        div.id = 'system-restart-overlay';
        div.style.cssText = `
            position:fixed; top:0; left:0; width:100vw; height:100vh;
            background:rgba(var(--ink-rgb),0.96); z-index:10000;
            display:flex; flex-direction:column; align-items:center; justify-content:center;
            color:var(--text-on-dark); backdrop-filter:blur(8px); font-family:inherit;
        `;

        div.innerHTML = `
            <style>
                @keyframes rx-spin { to { transform: rotate(360deg); } }
                @keyframes rx-pulse { 0%,100%{opacity:.3} 50%{opacity:1} }
                @keyframes rx-dot { 0%,80%,100%{transform:scale(0);opacity:0} 40%{transform:scale(1);opacity:1} }
                .rx-ring {
                    width:80px; height:80px; border-radius:50%;
                    border:3px solid rgba(var(--primary-rgb),0.2);
                    border-top-color:var(--primary);
                    animation: rx-spin 1s linear infinite;
                    margin-bottom:32px;
                }
                .rx-dots span {
                    display:inline-block; width:8px; height:8px; border-radius:50%;
                    background:var(--primary); margin:0 4px;
                }
                .rx-dots span:nth-child(1){animation:rx-dot 1.4s ease-in-out 0s infinite}
                .rx-dots span:nth-child(2){animation:rx-dot 1.4s ease-in-out .2s infinite}
                .rx-dots span:nth-child(3){animation:rx-dot 1.4s ease-in-out .4s infinite}
                .rx-status { animation: rx-pulse 2s ease-in-out infinite; }
            </style>
            <div class="rx-ring"></div>
            <h4 style="font-weight:300;letter-spacing:.05em;margin-bottom:8px;">${this.escapeHtml(title)}</h4>
            <div class="rx-status" style="color:var(--text-muted-on-dark);font-size:.9rem;margin-bottom:24px;" id="rx-status-text">${this.escapeHtml(statusMessage)}</div>
            <div class="rx-dots"><span></span><span></span><span></span></div>
            <div style="margin-top:28px;color:var(--text-muted-on-dark);font-size:.8rem;">
                已等待 <span id="rx-elapsed">0</span> 秒 &nbsp;·&nbsp; 连接恢复后将自动刷新
            </div>
        `;

        document.body.appendChild(div);

        // Elapsed timer
        let elapsed = 0;
        this._rxTimer = setInterval(() => {
            elapsed++;
            const el = document.getElementById('rx-elapsed');
            if (el) el.textContent = elapsed;
        }, 1000);

        // Status messages cycle
        const statuses = [
            statusMessage,
            '等待服务恢复…',
            '正在重新连接…',
            '即将完成，请稍候…',
        ];
        let si = 0;
        this._rxStatusTimer = setInterval(() => {
            si = (si + 1) % statuses.length;
            const el = document.getElementById('rx-status-text');
            if (el) el.textContent = statuses[si];
        }, 4000);
    },

    hideRestartOverlay() {
        const div = document.getElementById('system-restart-overlay');
        if (div) div.remove();
        if (this._rxTimer) { clearInterval(this._rxTimer); this._rxTimer = null; }
        if (this._rxStatusTimer) { clearInterval(this._rxStatusTimer); this._rxStatusTimer = null; }
    },

    showError(message, type = 'toast') {
        if (type === 'toast') {
            this.showToast(message, 'danger');
        } else {
            const container = document.querySelector('main');
            const alert = document.createElement('div');
            alert.className = 'alert alert-danger alert-dismissible fade show mb-4';
            alert.innerHTML = `
                <strong>错误：</strong>${this.escapeHtml(message)}
                <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
            `;
            container.prepend(alert);
        }
    },

    showSuccess(message) {
        this.showToast(message, 'success');
    },

    showInfo(message) {
        this.showToast(message, 'info');
    },

    showToast(message, variant = 'secondary', delay = 3500) {
        if (typeof bootstrap === 'undefined' || !bootstrap.Toast) {
            console.log(`[${variant}] ${message}`);
            return;
        }

        let container = document.getElementById('toastContainer');
        if (!container) {
            container = document.createElement('div');
            container.id = 'toastContainer';
            container.className = 'toast-container position-fixed top-0 end-0 p-3';
            container.style.zIndex = '10800';
            document.body.appendChild(container);
        }

        const iconMap = {
            success: 'bi-check-circle',
            danger: 'bi-exclamation-triangle',
            warning: 'bi-exclamation-circle',
            info: 'bi-info-circle',
            secondary: 'bi-bell'
        };
        const titleMap = {
            success: '成功',
            danger: '错误',
            warning: '提醒',
            info: '信息',
            secondary: '通知'
        };

        const toastEl = document.createElement('div');
        toastEl.className = 'toast shadow border-0';
        toastEl.setAttribute('role', 'status');
        toastEl.setAttribute('aria-live', 'polite');
        toastEl.setAttribute('aria-atomic', 'true');
        toastEl.innerHTML = `
            <div class="toast-header">
                <i class="bi ${iconMap[variant] || iconMap.secondary} text-${variant} me-2"></i>
                <strong class="me-auto">${titleMap[variant] || titleMap.secondary}</strong>
                <button type="button" class="btn-close" data-bs-dismiss="toast" aria-label="关闭"></button>
            </div>
            <div class="toast-body">${this.escapeHtml(message)}</div>
        `;

        container.appendChild(toastEl);
        const toast = new bootstrap.Toast(toastEl, { delay });
        toastEl.addEventListener('hidden.bs.toast', () => toastEl.remove());
        toast.show();
    },

    confirm(message, options = {}) {
        if (typeof bootstrap === 'undefined' || !bootstrap.Modal) {
            return Promise.resolve(window.confirm(message));
        }

        const old = document.getElementById('uiConfirmModal');
        if (old) old.remove();

        const confirmText = options.confirmText || '确认';
        const cancelText = options.cancelText || '取消';
        const title = options.title || '请确认';
        const requestedVariant = options.variant || 'primary';
        const variant = ['primary', 'danger', 'warning', 'success', 'secondary'].includes(requestedVariant)
            ? requestedVariant
            : 'primary';

        const modalEl = document.createElement('div');
        modalEl.className = 'modal fade';
        modalEl.id = 'uiConfirmModal';
        modalEl.tabIndex = -1;
        modalEl.innerHTML = `
            <div class="modal-dialog modal-dialog-centered">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">${this.escapeHtml(title)}</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button>
                    </div>
                    <div class="modal-body">
                        <div style="white-space:pre-wrap;">${this.escapeHtml(message)}</div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">${this.escapeHtml(cancelText)}</button>
                        <button type="button" class="btn btn-${variant}" id="uiConfirmAccept">${this.escapeHtml(confirmText)}</button>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(modalEl);

        return new Promise(resolve => {
            let accepted = false;
            const modal = new bootstrap.Modal(modalEl);
            modalEl.querySelector('#uiConfirmAccept').addEventListener('click', () => {
                accepted = true;
                modal.hide();
            });
            modalEl.addEventListener('hidden.bs.modal', () => {
                modalEl.remove();
                resolve(accepted);
            }, { once: true });
            modal.show();
        });
    },

    // Components Renderers
    renderDashboard(status, components, stats, wxStatus, wxInfo) {
        // 0. Render Bot Status (WeChat)
        const botStatusEl = document.getElementById('dashboardWechatStatus');
        if (botStatusEl && wxStatus) {
            const isConnected = wxStatus.status === 'connected';
            const info = wxInfo || {};

            // Try to resolve user info keys
            const name = info.display_name || info.name || info.nickname || info.nickName || info.self_nickname || 'WeChat Bot';
            const avatar = info.bm || info.avatar || info.headImgUrl || info.mediumHeadImgUrl || '';
            const id = info.id || info.wxid || info.username || '';
            const safeAvatar = this.escapeHtml(avatar);

            const statusBadge = isConnected ?
                '<span class="badge bg-success-subtle text-success rounded-pill"><i class="bi bi-circle-fill me-1" style="font-size: 6px; vertical-align: middle;"></i>在线</span>' :
                '<span class="badge bg-danger-subtle text-danger rounded-pill"><i class="bi bi-circle-fill me-1" style="font-size: 6px; vertical-align: middle;"></i>离线</span>';

            botStatusEl.innerHTML = `
                <div class="mb-3">
                    ${avatar ?
                    `<img src="${safeAvatar}" class="rounded-circle shadow-sm border border-2 border-white" style="width: 80px; height: 80px; object-fit: cover;" alt="WeChat 头像">` :
                    `<div class="rounded-circle bg-light d-inline-flex align-items-center justify-content-center shadow-sm" style="width: 80px; height: 80px;"><i class="bi bi-robot fs-1 text-secondary"></i></div>`
                }
                </div>
                <h5 class="fw-bold mb-1">${isConnected ? this.escapeHtml(name) : 'WeChat 客户端'}</h5>
                ${isConnected && id ? `<div class="text-muted small font-monospace mb-2">${this.escapeHtml(id)}</div>` : ''}
                <div class="mt-2">${statusBadge}</div>
            `;
        }

        // 1. Render Compact System Status
        const statusList = document.getElementById('systemStatusList');
        if (statusList && status && status.system) {
            const cpu = Math.max(0, Math.min(100, Number(status.system.cpu?.usage_percent || 0)));
            const mem = Math.max(0, Math.min(100, Number(status.system.memory?.percent || 0)));
            const disk = Math.max(0, Math.min(100, Number(status.system.disk?.percent || 0)));
            const uptime = Math.max(0, Number(status.system.uptime || 0) / 3600).toFixed(1);

            statusList.innerHTML = `
                <div class="mb-4">
                    <div class="d-flex justify-content-between mb-1">
                        <span class="small fw-bold text-muted">CPU</span>
                        <span class="small text-primary">${cpu.toFixed(1)}%</span>
                    </div>
                    <div class="progress" style="height: 6px;">
                        <div class="progress-bar bg-primary" style="width: ${cpu}%"></div>
                    </div>
                </div>
                <div class="mb-4">
                    <div class="d-flex justify-content-between mb-1">
                        <span class="small fw-bold text-muted">内存</span>
                        <span class="small text-info">${mem.toFixed(1)}%</span>
                    </div>
                    <div class="progress" style="height: 6px;">
                        <div class="progress-bar bg-info" style="width: ${mem}%"></div>
                    </div>
                </div>
                <div class="mb-4">
                     <div class="d-flex justify-content-between mb-1">
                        <span class="small fw-bold text-muted">磁盘</span>
                        <span class="small text-warning">${disk.toFixed(1)}%</span>
                    </div>
                    <div class="progress" style="height: 6px;">
                        <div class="progress-bar bg-warning" style="width: ${disk}%"></div>
                    </div>
                </div>
                 <div class="d-flex align-items-center pt-2 border-top">
                    <i class="bi bi-clock me-2 text-muted"></i>
                    <div>
                        <div class="small text-muted fw-bold">运行时间</div>
                        <div class="h6 mb-0">${uptime} 小时</div>
                    </div>
                </div>
            `;
        }

        // 2. Render Listeners Summary (User-Centric View)
        const listenersContainer = document.getElementById('listenersSummary');
        const totalCountEl = document.getElementById('totalListenersCount');

        // Normalize stats object
        const s = stats && stats.stats ? stats.stats : (stats || {});

        if (listenersContainer && s.user_listeners_by_type) {
            const groups = s.user_listeners_by_type;
            const totalListeners = s.total_listeners || 0;
            if (totalCountEl) totalCountEl.textContent = `${totalListeners} 个活跃监听`;

            if (Object.keys(groups).length === 0) {
                listenersContainer.innerHTML = `
                    <div class="text-center py-5 text-muted">
                        <i class="bi bi-broadcast opacity-25" style="font-size: 3rem;"></i>
                        <p class="mt-3">未找到活跃的消息监听。</p>
                    </div>
                 `;
            } else {
                const typeLabels = {
                    'text_message_received': { icon: 'bi-chat-text', label: '文本消息', color: 'success' },
                    'image_message_received': { icon: 'bi-image', label: '图片', color: 'warning' },
                    'file_message_received': { icon: 'bi-file-earmark', label: '文件', color: 'primary' },
                    'quote_message_received': { icon: 'bi-chat-quote', label: '引用消息', color: 'info' },
                    'quote_text_message_received': { icon: 'bi-blockquote-left', label: '文本引用', color: 'info' },
                    'quote_image_message_received': { icon: 'bi-card-image', label: '图片引用', color: 'warning' },
                    'quote_video_message_received': { icon: 'bi-camera-video-fill', label: '视频引用', color: 'danger' },
                    'friend_request_received': { icon: 'bi-person-plus', label: '好友请求', color: 'danger' },
                    'system_startup': { icon: 'bi-power', label: '系统启动', color: 'secondary' },
                    'system_shutdown': { icon: 'bi-power', label: '系统关闭', color: 'dark' },
                    'plugin_loaded': { icon: 'bi-plugin', label: '插件事件', color: 'secondary' },
                    'emotion_message_received': { icon: 'bi-emoji-smile', label: '表情消息', color: 'warning' }
                };

                const html = Object.entries(groups).map(([type, usersMap]) => {
                    const meta = typeLabels[type] || { icon: 'bi-lightning', label: type.replace(/_/g, ' '), color: 'secondary' };
                    // usersMap is { 'UserA': ['plugin1', 'plugin2'] }

                    const userItems = Object.entries(usersMap).map(([user, plugins], idx) => {
                        const collapseId = `collapse-listener-${idx}-${Math.abs(String(type).split('').reduce((sum, char) => sum + char.charCodeAt(0), 0))}`;
                        const pluginList = plugins.map(p => `<span class="badge bg-light text-secondary border me-1">${this.escapeHtml(p)}</span>`).join('');

                        return `
                            <div class="me-3 mb-3 d-inline-block text-start">
                                <button class="btn btn-outline-${meta.color} btn-sm rounded-pill px-3 position-relative"
                                        type="button"
                                        data-bs-toggle="collapse"
                                        data-bs-target="#${collapseId}"
                                        aria-expanded="false"
                                        style="border-style: dashed;">
                                    <i class="bi bi-person me-1"></i> ${this.escapeHtml(user)}
                                    <span class="position-absolute top-0 start-100 translate-middle badge rounded-pill bg-secondary" style="font-size: 0.6em;">
                                        ${plugins.length}
                                    </span>
                                </button>
                                <div class="collapse mt-2" id="${collapseId}">
                                    <div class="card card-body p-2 bg-light border-0 shadow-sm" style="min-width: 200px;">
                                        <small class="text-muted d-block mb-1">活跃插件：</small>
                                        <div>${pluginList}</div>
                                    </div>
                                </div>
                            </div>
                        `;
                    }).join('');

                    if (!userItems) return ''; // Skip empty groups if any

                    return `
                        <div class="mb-4 pb-3 border-bottom">
                             <div class="d-flex align-items-center mb-3">
                                <div class="bg-${meta.color}-subtle text-${meta.color} rounded p-2 me-3 d-flex align-items-center justify-content-center">
                                    <i class="bi ${meta.icon} fs-5"></i>
                                </div>
                                <h6 class="mb-0 fw-bold text-dark">${this.escapeHtml(meta.label)}</h6>
                             </div>
                             <div class="d-flex flex-wrap ps-5">
                                ${userItems}
                             </div>
                        </div>
                    `;
                }).join('');

                listenersContainer.innerHTML = html || '<div class="text-muted ps-4">没有活跃的用户监听。</div>';
            }
        }
    },

    updateMetric(id, value) {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    },

    renderPluginsList(plugins) {
        const capabilities = Array.isArray(plugins)
            ? plugins
            : Object.entries(plugins || {}).map(([id, info]) => ({ id, ...info }));
        return this.renderCapabilitiesList(capabilities);
    },

    renderAutomationWorkbench(capabilities, routing, state = {}) {
        const container = document.getElementById('pluginsList');
        if (!container) return;

        const view = state.view === 'routes' ? 'routes' : 'library';
        const summary = routing?.summary || {};
        const eventTypes = routing?.event_types || [];
        const selectedEvent = eventTypes.some(item => item.id === state.selectedEvent)
            ? state.selectedEvent
            : (eventTypes[0]?.id || '');
        const selectedMeta = eventTypes.find(item => item.id === selectedEvent) || {};
        const routeMode = state.routeMode === 'detail' ? 'detail' : 'sort';
        const chats = routing?.chats || [];
        const context = routing?.context || {};
        const selectedChatId = context.chat_id ?? state.selectedChatId ?? null;
        const isChatPreview = selectedChatId !== null && selectedChatId !== undefined;
        const chatOptions = chats.map(chat => `
            <option value="${Number(chat.id)}" ${Number(chat.id) === Number(selectedChatId) ? 'selected' : ''}>
                ${this.escapeHtml(chat.display_name || chat.chat_name)}${chat.is_group ? ' · 群聊' : ' · 私聊'}
            </option>
        `).join('');
        const workbenchHead = `
            <section class="automation-command fade-in">
                <div class="automation-view-switch" role="tablist" aria-label="插件视图">
                    <button type="button" class="automation-view-button ${view === 'library' ? 'active' : ''}" data-automation-view="library" role="tab" aria-selected="${view === 'library'}">
                        <i class="bi bi-grid"></i><span>插件库</span>
                    </button>
                    <button type="button" class="automation-view-button ${view === 'routes' ? 'active' : ''}" data-automation-view="routes" role="tab" aria-selected="${view === 'routes'}">
                        <i class="bi bi-diagram-3"></i><span>执行顺序</span>
                    </button>
                </div>
                ${view === 'routes' ? `
                    <div class="automation-command-title">
                        <span><i class="bi bi-globe2"></i>全局规则 · 各消息类型独立排序</span>
                        <span class="is-ok"><i class="bi bi-check2"></i>${Number(summary.listener_count || 0)} 个节点已纳入中央顺序表</span>
                    </div>
                    <div class="automation-command-context">
                        <div class="automation-route-mode-switch" role="tablist" aria-label="执行顺序显示模式">
                            <button type="button" class="automation-route-mode-button ${routeMode === 'sort' ? 'active' : ''}" data-route-mode="sort" role="tab" aria-selected="${routeMode === 'sort'}">
                                <i class="bi bi-list-ol"></i><span>紧凑排序</span>
                            </button>
                            <button type="button" class="automation-route-mode-button ${routeMode === 'detail' ? 'active' : ''}" data-route-mode="detail" role="tab" aria-selected="${routeMode === 'detail'}">
                                <i class="bi bi-card-text"></i><span>查看详情</span>
                            </button>
                        </div>
                        <label class="automation-chat-control" title="只读查看该聊天会经过哪些全局节点">
                            <i class="bi bi-eye"></i>
                            <select class="form-select form-select-sm" id="automationChatPreview" aria-label="聊天执行顺序只读预览">
                                <option value="">全部聊天 · 全局结构</option>
                                ${chatOptions}
                            </select>
                        </label>
                    </div>
                ` : ''}
            </section>
        `;

        if (view === 'library') {
            container.innerHTML = `${workbenchHead}<div id="automationCapabilityLibrary"></div>`;
            container.querySelectorAll('[data-automation-view]').forEach(button => {
                button.addEventListener('click', () => App.setAutomationView(button.dataset.automationView));
            });
            this.renderCapabilitiesList(capabilities, 'automationCapabilityLibrary');
            return;
        }

        const eventButtons = eventTypes.map(event => `
            <button type="button" class="automation-event-button ${event.id === selectedEvent ? 'active' : ''}" data-event-type="${this.escapeHtml(event.id)}">
                <span class="automation-event-icon"><i class="bi ${this.escapeHtml(event.icon)}"></i></span>
                <span class="automation-event-copy"><strong>${this.escapeHtml(event.label)}</strong><small>${isChatPreview ? `${Number(event.eligible_count)} / ${Number(event.listener_count)} 个会经过` : `${Number(event.listener_count)} 个执行节点`}</small></span>
                ${event.blocker_count ? `<span class="automation-event-count is-blocking" title="${Number(event.blocker_count)} 个可截断节点"><i class="bi bi-sign-stop"></i>${Number(event.blocker_count)}</span>` : `<span class="automation-event-count">${Number(event.listener_count)}</span>`}
            </button>
        `).join('');

        const liveItems = routing?.routes?.[selectedEvent] || [];
        const liveKeys = liveItems.map(item => item.listener_key);
        const draftKeys = state.draftEvent === selectedEvent && Array.isArray(state.draftKeys)
            ? state.draftKeys.filter(key => liveKeys.includes(key))
            : liveKeys;
        const missingKeys = liveKeys.filter(key => !draftKeys.includes(key));
        const orderedItems = [...draftKeys, ...missingKeys]
            .map(key => liveItems.find(item => item.listener_key === key))
            .filter(Boolean);

        const detailRouteSteps = orderedItems.map((item, index) => {
            const eligible = Boolean(item.eligible);
            const canBlock = eligible && Boolean(item.can_block);
            const statusLabel = item.status === 'disabled' ? '已停用' : '需检查';
            const exceptionalStatus = item.status !== 'running';
            const stateClass = !eligible ? 'is-skipped' : '';
            const trigger = item.trigger || {};
            const editable = Array.isArray(trigger.editable) ? trigger.editable : [];
            const triggerValues = editable.map(field => {
                const rawValue = Array.isArray(field.value) ? field.value.join('、') : String(field.value ?? '');
                return `<span class="automation-trigger-value"><strong>${this.escapeHtml(field.title)}</strong>${this.escapeHtml(rawValue || '未设置')}</span>`;
            }).join('');
            const conditions = (trigger.conditions || []).map(condition => `<li>${this.escapeHtml(condition)}</li>`).join('');
            const previewLabel = isChatPreview
                ? (eligible ? '该聊天已启用' : (item.reason || '该聊天不会经过'))
                : (item.scope_summary || '全局');
            const propagationLabel = item.propagation === 'observe'
                ? '<span><i class="bi bi-eye"></i>只观察，不拦截</span>'
                : canBlock
                    ? '<span class="is-blocker"><i class="bi bi-sign-stop"></i>命中并消费后结束</span>'
                    : '<span><i class="bi bi-arrow-down"></i>命中后继续</span>';
            const step = `
                <div class="automation-route-step ${stateClass}" data-listener-key="${this.escapeHtml(item.listener_key)}">
                    <article class="automation-route-card is-detail ${canBlock ? 'can-block' : ''}">
                        <div class="automation-detail-marker" aria-hidden="true"><i class="bi bi-card-text"></i></div>
                        <div class="automation-route-rank">${String(index + 1).padStart(2, '0')}</div>
                        <div class="automation-route-icon"><i class="bi ${this.escapeHtml(item.icon || 'bi-puzzle')}"></i></div>
                        <div class="automation-route-main">
                            <div class="automation-route-title-row">
                                <h3>${this.escapeHtml(item.display_name || item.plugin_name)}</h3>
                                ${exceptionalStatus ? `<span class="automation-route-status is-${this.escapeHtml(item.status || 'unknown')}">${statusLabel}</span>` : ''}
                            </div>
                            <div class="automation-route-id">${this.escapeHtml(item.listener_title || item.plugin_name)} · ${this.escapeHtml(item.handler_name || item.listener_key)}</div>
                            <div class="automation-route-trigger">
                                <div class="automation-trigger-head">
                                    <span><i class="bi bi-lightning-charge"></i>触发条件</span>
                                    ${editable.length ? `<button type="button" class="automation-trigger-button"><i class="bi bi-pencil-square"></i>编辑全局触发</button>` : '<small><i class="bi bi-lock"></i>插件固定</small>'}
                                </div>
                                <p>${this.escapeHtml(trigger.summary || '由插件规则判断')}</p>
                                ${triggerValues ? `<div class="automation-trigger-values">${triggerValues}</div>` : ''}
                                ${conditions ? `<ul>${conditions}</ul>` : ''}
                            </div>
                            <div class="automation-route-tags">
                                <span class="${eligible ? 'is-eligible' : 'is-muted'}"><i class="bi ${eligible ? 'bi-people' : 'bi-slash-circle'}"></i>${this.escapeHtml(previewLabel)}</span>
                                ${propagationLabel}
                            </div>
                        </div>
                        <div class="automation-route-actions">
                            <button type="button" class="automation-config-button" title="打开全部插件设置"><i class="bi bi-sliders"></i></button>
                        </div>
                    </article>
                    ${index < orderedItems.length - 1 ? (canBlock ? `
                        <div class="automation-route-branch">
                            <span class="continues"><i class="bi bi-arrow-down"></i>未命中 / 未消费 → 下一插件</span>
                            <span class="stops"><i class="bi bi-sign-stop"></i>命中并消费 → 结束</span>
                        </div>
                    ` : '<div class="automation-route-connector"><i class="bi bi-arrow-down"></i></div>') : ''}
                </div>
            `;
            return step;
        }).join('');

        const compactRouteSteps = orderedItems.map((item, index) => {
            const eligible = Boolean(item.eligible);
            const stateClass = !eligible ? 'is-skipped' : '';
            const trigger = item.trigger || {};
            const triggerSummary = trigger.summary || '由插件规则判断';
            const previewLabel = isChatPreview
                ? (eligible ? '该聊天已启用' : (item.reason || '该聊天不会经过'))
                : (item.status === 'running' ? '运行中' : '需检查');
            const outcome = item.propagation === 'observe'
                ? '<span class="automation-compact-outcome is-observe"><i class="bi bi-eye"></i>只观察</span>'
                : item.propagation === 'stop_on_consumed'
                    ? '<span class="automation-compact-outcome is-stop"><i class="bi bi-sign-stop"></i>消费后结束</span>'
                    : '<span class="automation-compact-outcome is-continue"><i class="bi bi-arrow-down"></i>继续传递</span>';
            return `
                <div class="automation-route-step automation-compact-step ${stateClass}" data-listener-key="${this.escapeHtml(item.listener_key)}">
                    <article class="automation-compact-row ${item.can_block ? 'can-block' : ''}">
                        <div class="automation-drag-handle" title="按住拖动调整顺序" aria-label="按住拖动调整顺序"><i class="bi bi-grip-vertical"></i></div>
                        <div class="automation-compact-rank" aria-label="当前顺序第 ${index + 1} 位">${String(index + 1).padStart(2, '0')}</div>
                        <div class="automation-compact-icon"><i class="bi ${this.escapeHtml(item.icon || 'bi-puzzle')}"></i></div>
                        <div class="automation-compact-identity">
                            <strong>${this.escapeHtml(item.display_name || item.plugin_name)}</strong>
                            <small>${this.escapeHtml(item.listener_title || item.handler_name || item.plugin_name)}</small>
                        </div>
                        <div class="automation-compact-trigger" title="${this.escapeHtml(triggerSummary)}">
                            <i class="bi bi-lightning-charge"></i><span>${this.escapeHtml(triggerSummary)}</span>
                        </div>
                        <span class="automation-compact-preview ${eligible ? 'is-enabled' : 'is-skipped'}" title="${this.escapeHtml(previewLabel)}">
                            <i class="bi ${eligible ? 'bi-check-circle' : 'bi-slash-circle'}"></i>${this.escapeHtml(previewLabel)}
                        </span>
                        ${outcome}
                    </article>
                </div>
            `;
        }).join('');
        const routeSteps = routeMode === 'sort' ? compactRouteSteps : detailRouteSteps;
        const dirtyBar = state.dirty ? `
            <div class="automation-save-bar" role="status">
                <div><span class="automation-unsaved-dot"></span><strong>顺序尚未应用</strong><small>调整结果确认后才会写入全局顺序</small></div>
                <div class="automation-save-actions">
                    <button type="button" class="btn btn-surface btn-sm" id="automationUndoOrder"><i class="bi bi-arrow-counterclockwise me-1"></i>撤销</button>
                    <button type="button" class="btn btn-primary btn-sm" id="automationSaveOrder" ${state.saving ? 'disabled' : ''}>
                        ${state.saving ? '<span class="spinner-border spinner-border-sm me-1"></span>' : '<i class="bi bi-check2 me-1"></i>'}应用新顺序
                    </button>
                </div>
            </div>
        ` : '';

        container.innerHTML = `
            ${workbenchHead}
            <div class="automation-workbench-grid">
                <aside class="automation-event-rail">
                    <div class="automation-rail-heading"><span>消息通道</span><small>各自独立排序</small></div>
                    <div class="automation-event-list">${eventButtons || '<div class="text-muted small p-3">暂无消息监听器</div>'}</div>
                </aside>
                <main class="automation-route-panel">
                    <div class="automation-route-panel-head">
                        <div>
                            <h2><i class="bi ${this.escapeHtml(selectedMeta.icon || 'bi-diagram-3')}"></i>${this.escapeHtml(selectedMeta.label || '消息')}处理顺序</h2>
                            <span class="automation-route-count">该通道共 ${Number(selectedMeta.listener_count || 0)} 个节点 · 顺序对所有聊天生效</span>
                        </div>
                        <div class="automation-route-panel-actions">
                            ${routeMode === 'sort' ? `
                                <span class="automation-sort-hint"><i class="bi bi-grip-vertical"></i>拖动左侧把手调整顺序</span>
                            ` : `
                                <div class="automation-route-legend">
                                    <span><i class="bi bi-arrow-down is-pass"></i>放行</span>
                                    <span><i class="bi bi-sign-stop is-block"></i>截断</span>
                                    <span><i class="bi bi-slash-circle is-skip"></i>跳过</span>
                                </div>
                            `}
                        </div>
                    </div>
                    <div class="automation-preview-banner ${isChatPreview ? 'is-chat' : ''}">
                        <i class="bi ${isChatPreview ? 'bi-eye' : 'bi-globe2'}"></i>
                        ${isChatPreview
                            ? `<span><strong>只读预览：${this.escapeHtml(context.chat_display_name || context.chat_name || '当前聊天')}</strong> · ${Number(selectedMeta.eligible_count || 0)} / ${Number(selectedMeta.listener_count || 0)} 个节点会经过；灰色行表示该聊天未启用或不适用。调整仍会修改全局顺序。</span>`
                            : '<span><strong>全局结构</strong> · 选择顶部聊天可查看该聊天的插件开关和实际经过顺序。</span>'}
                    </div>
                    <div class="automation-route-list" id="automationRouteList">
                        ${routeSteps || '<div class="empty-state-panel"><i class="bi bi-diagram-3"></i><h3>暂无执行节点</h3><p>该消息类型目前没有插件监听。</p></div>'}
                    </div>
                </main>
            </div>
            ${dirtyBar}
        `;

        container.querySelectorAll('[data-automation-view]').forEach(button => {
            button.addEventListener('click', () => App.setAutomationView(button.dataset.automationView));
        });
        container.querySelectorAll('[data-route-mode]').forEach(button => {
            button.addEventListener('click', () => App.setAutomationRouteMode(button.dataset.routeMode));
        });
        document.getElementById('automationChatPreview')?.addEventListener('change', event => {
            App.selectAutomationChat(event.target.value);
        });
        container.querySelectorAll('.automation-event-button').forEach(button => {
            button.addEventListener('click', () => App.selectAutomationEvent(button.dataset.eventType));
        });
        document.getElementById('automationUndoOrder')?.addEventListener('click', () => App.undoAutomationOrder());
        document.getElementById('automationSaveOrder')?.addEventListener('click', () => App.saveAutomationOrder());

        const routeList = document.getElementById('automationRouteList');
        if (!routeList) return;
        routeList.querySelectorAll('.automation-route-step').forEach(step => {
            const key = step.dataset.listenerKey;
            step.querySelector('.automation-config-button')?.addEventListener('click', () => {
                const item = liveItems.find(candidate => candidate.listener_key === key);
                if (item) App.showPluginSettings(item.plugin_name);
            });
            step.querySelector('.automation-trigger-button')?.addEventListener('click', () => {
                const item = liveItems.find(candidate => candidate.listener_key === key);
                if (item) App.showPluginSettings(item.plugin_name, { focusGroup: 'trigger' });
            });
            if (routeMode !== 'sort') return;
            const handle = step.querySelector('.automation-drag-handle');
            handle?.addEventListener('pointerdown', event => {
                if (event.button !== 0 || event.isPrimary === false) return;
                event.preventDefault();

                const pointerId = event.pointerId;
                const sourceRect = step.getBoundingClientRect();
                const pointerOffsetY = event.clientY - sourceRect.top;
                const originalKeys = [...routeList.querySelectorAll('.automation-route-step')]
                    .map(item => item.dataset.listenerKey);
                const ghost = step.querySelector('.automation-compact-row')?.cloneNode(true);
                if (!ghost) return;

                ghost.classList.add('automation-drag-ghost');
                ghost.style.width = `${sourceRect.width}px`;
                ghost.style.left = `${sourceRect.left}px`;
                ghost.style.top = `${sourceRect.top}px`;
                document.body.appendChild(ghost);

                step.classList.add('is-dragging');
                routeList.classList.add('is-sorting');
                document.body.classList.add('automation-pointer-sorting');
                try {
                    handle.setPointerCapture(pointerId);
                } catch (_) {
                    // Pointer capture is an enhancement; document listeners keep sorting functional.
                }

                let lastPointerY = event.clientY;
                let scrollFrame = null;

                const placeAtPointer = clientY => {
                    const siblings = [...routeList.querySelectorAll('.automation-route-step:not(.is-dragging)')];
                    const before = siblings.find(item => {
                        const rect = item.getBoundingClientRect();
                        return clientY < rect.top + rect.height / 2;
                    });
                    if (before) {
                        routeList.insertBefore(step, before);
                    } else {
                        routeList.appendChild(step);
                    }
                };

                const updateGhost = clientY => {
                    const top = Math.max(
                        8,
                        Math.min(window.innerHeight - sourceRect.height - 8, clientY - pointerOffsetY)
                    );
                    ghost.style.top = `${top}px`;
                };

                const autoScroll = () => {
                    const edge = Math.min(120, window.innerHeight * 0.16);
                    const distanceTop = lastPointerY;
                    const distanceBottom = window.innerHeight - lastPointerY;
                    let speed = 0;
                    if (distanceTop < edge) {
                        speed = -Math.max(5, Math.round((edge - distanceTop) * 0.24));
                    } else if (distanceBottom < edge) {
                        speed = Math.max(5, Math.round((edge - distanceBottom) * 0.24));
                    }
                    if (speed) {
                        window.scrollBy(0, speed);
                        placeAtPointer(lastPointerY);
                    }
                    scrollFrame = window.requestAnimationFrame(autoScroll);
                };

                const onPointerMove = moveEvent => {
                    if (moveEvent.pointerId !== pointerId) return;
                    moveEvent.preventDefault();
                    lastPointerY = moveEvent.clientY;
                    updateGhost(lastPointerY);
                    placeAtPointer(lastPointerY);
                };

                const restoreOriginalOrder = () => {
                    const byKey = new Map(
                        [...routeList.querySelectorAll('.automation-route-step')]
                            .map(item => [item.dataset.listenerKey, item])
                    );
                    originalKeys.forEach(originalKey => {
                        const item = byKey.get(originalKey);
                        if (item) routeList.appendChild(item);
                    });
                };

                const finish = commit => {
                    document.removeEventListener('pointermove', onPointerMove);
                    document.removeEventListener('pointerup', onPointerUp);
                    document.removeEventListener('pointercancel', onPointerCancel);
                    document.removeEventListener('keydown', onKeyDown);
                    if (scrollFrame !== null) window.cancelAnimationFrame(scrollFrame);
                    try {
                        if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId);
                    } catch (_) {
                        // The browser may already have released capture.
                    }
                    if (!commit) restoreOriginalOrder();
                    ghost.remove();
                    step.classList.remove('is-dragging');
                    routeList.classList.remove('is-sorting');
                    document.body.classList.remove('automation-pointer-sorting');
                    App.captureAutomationOrder();
                };

                const onPointerUp = upEvent => {
                    if (upEvent.pointerId === pointerId) finish(true);
                };
                const onPointerCancel = cancelEvent => {
                    if (cancelEvent.pointerId === pointerId) finish(false);
                };
                const onKeyDown = keyEvent => {
                    if (keyEvent.key !== 'Escape') return;
                    keyEvent.preventDefault();
                    finish(false);
                };

                document.addEventListener('pointermove', onPointerMove, { passive: false });
                document.addEventListener('pointerup', onPointerUp);
                document.addEventListener('pointercancel', onPointerCancel);
                document.addEventListener('keydown', onKeyDown);
                scrollFrame = window.requestAnimationFrame(autoScroll);
            });
        });
    },

    renderCapabilitiesList(capabilities, containerId = 'pluginsList') {
        const container = document.getElementById(containerId);
        if (!container) return;

        const automations = (capabilities || []).filter(item => !item.featured);
        if (automations.length === 0) {
            container.innerHTML = '<div class="empty-state-panel"><i class="bi bi-puzzle"></i><h3>暂无插件能力</h3><p>发现的插件会显示在这里。</p></div>';
            return;
        }

        const categoryFilters = [...new Map(automations.map(item => [item.category, item.category_label])).entries()];
        const cards = automations.map(info => {
            const name = info.id;
            const isEnabled = info.enabled;
            const statusLabel = info.status === 'running' ? '运行中' : info.status === 'disabled' ? '已停用' : '需检查';
            const statusClass = info.status === 'running' ? 'is-running' : info.status === 'disabled' ? 'is-disabled' : 'is-error';

            return `
                <article class="capability-card" data-capability-id="${this.escapeHtml(name)}" data-category="${this.escapeHtml(info.category)}" data-search="${this.escapeHtml(`${info.display_name} ${info.internal_name} ${info.description}`.toLowerCase())}">
                    <div class="capability-card-head">
                        <div class="capability-icon"><i class="bi ${this.escapeHtml(info.icon || 'bi-lightning-charge')}"></i></div>
                        <div class="capability-heading">
                            <div class="d-flex align-items-center gap-2 flex-wrap">
                                <h3>${this.escapeHtml(info.display_name || name)}</h3>
                                <span class="capability-status ${statusClass}"><span></span>${statusLabel}</span>
                            </div>
                            <div class="capability-internal-id">${this.escapeHtml(name)} · v${this.escapeHtml(info.version || '1.0')}</div>
                        </div>
                        <div class="form-check form-switch capability-enable">
                            <input class="form-check-input capability-toggle" type="checkbox" role="switch"
                                ${isEnabled ? 'checked' : ''} aria-label="启用 ${this.escapeHtml(info.display_name || name)}">
                        </div>
                    </div>
                    <p class="capability-description">${this.escapeHtml(info.description || '暂无功能说明')}</p>
                    <div class="capability-metrics">
                        <span><strong>${Number(info.assigned_chat_count || 0)}</strong> 个聊天</span>
                        <span><strong>${Number(info.settings_count || 0)}</strong> 项设置</span>
                        <span>${this.escapeHtml(info.category_label || '其他')}</span>
                    </div>
                    <div class="capability-actions">
                        <button class="btn btn-quiet-accent btn-sm capability-configure" ${info.configurable ? '' : 'disabled'}>
                            <i class="bi bi-sliders me-1"></i>配置
                        </button>
                        <button class="btn btn-light border btn-sm capability-assign">
                            <i class="bi bi-chat-square-text me-1"></i>分配聊天
                        </button>
                        <div class="dropdown ms-auto">
                            <button class="btn btn-light border btn-sm" data-bs-toggle="dropdown" aria-label="更多操作"><i class="bi bi-three-dots"></i></button>
                            <ul class="dropdown-menu dropdown-menu-end">
                                <li><button class="dropdown-item capability-reload"><i class="bi bi-arrow-clockwise me-2"></i>重新加载</button></li>
                                <li><button class="dropdown-item capability-details"><i class="bi bi-code-square me-2"></i>开发者详情</button></li>
                            </ul>
                        </div>
                    </div>
                </article>
            `;
        }).join('');

        const filters = categoryFilters.map(([id, label]) => `
            <button type="button" class="capability-filter" data-capability-filter="${this.escapeHtml(id)}">${this.escapeHtml(label)}</button>
        `).join('');

        const newHtml = `
            <div class="capability-toolbar">
                <div class="capability-search"><i class="bi bi-search"></i><input id="capabilitySearch" type="search" placeholder="搜索插件"></div>
                <div class="capability-filters"><button type="button" class="capability-filter active" data-capability-filter="all">全部</button>${filters}</div>
            </div>
            <div class="capability-grid" id="capabilityGrid">${cards}</div>
        `;

        container.innerHTML = newHtml;

        container.querySelectorAll('.capability-card').forEach(card => {
            const capabilityId = card.dataset.capabilityId;
            card.querySelector('.capability-toggle')?.addEventListener('change', event => {
                App.togglePlugin(capabilityId, event.target.checked);
            });
            card.querySelector('.capability-configure')?.addEventListener('click', () => App.showPluginSettings(capabilityId));
            card.querySelector('.capability-assign')?.addEventListener('click', () => UI.switchTab('users'));
            card.querySelector('.capability-reload')?.addEventListener('click', () => App.reloadPlugin(capabilityId));
            card.querySelector('.capability-details')?.addEventListener('click', () => App.showPluginDetails(capabilityId));
        });

        const applyFilters = () => {
            const query = (document.getElementById('capabilitySearch')?.value || '').trim().toLowerCase();
            const active = container.querySelector('.capability-filter.active')?.dataset.capabilityFilter || 'all';
            container.querySelectorAll('.capability-card').forEach(card => {
                const categoryMatch = active === 'all' || card.dataset.category === active;
                const searchMatch = !query || (card.dataset.search || '').includes(query);
                card.classList.toggle('d-none', !categoryMatch || !searchMatch);
            });
        };
        document.getElementById('capabilitySearch')?.addEventListener('input', this.debounce(applyFilters, 120));
        container.querySelectorAll('.capability-filter').forEach(button => {
            button.addEventListener('click', () => {
                container.querySelectorAll('.capability-filter').forEach(item => item.classList.remove('active'));
                button.classList.add('active');
                applyFilters();
            });
        });
    },

    renderUsersList(users) {
        const list = document.getElementById('usersList');
        if (!list) return;

        if (!users || users.length === 0) {
            list.innerHTML = `
                <div class="chat-picker-empty">
                    <i class="bi bi-search"></i>
                    <span>没有符合当前条件的聊天</span>
                </div>
            `;
            return;
        }

        list.innerHTML = users.map(u => {
            const userId = Number(u.id) || null;
            const isActive = u.is_listening;
            const listeningEnabled = u.listening_enabled !== false;
            const isConfigured = u.has_permission_config;
            const isSelected = window.App.currentThreadName === u.chat_name;
            const chatTypeIcon = u.is_group ? 'bi-people' : 'bi-person';
            const chatTypeLabel = u.is_group ? '群聊' : '私聊';
            const safeChatName = this.escapeHtml(u.chat_name);
            const statusClass = isActive ? 'active' : (listeningEnabled ? 'waiting' : 'paused');
            const statusLabel = isActive ? '监听中' : (listeningEnabled ? '等待连接' : '已暂停');

            return `
                <button type="button" class="chat-picker-item ${isSelected ? 'active' : ''}"
                    data-chat-select aria-current="${isSelected ? 'true' : 'false'}"
                    data-user-id="${userId || ''}" data-chatname="${safeChatName}">
                    <span class="chat-picker-item-icon"><i class="bi ${chatTypeIcon}"></i></span>
                    <span class="chat-picker-item-copy">
                        <strong>${safeChatName}</strong>
                        <small>${chatTypeLabel}${isConfigured ? '' : ' · 待管理'}</small>
                    </span>
                    <span class="chat-picker-status ${statusClass}"><i></i>${statusLabel}</span>
                </button>
            `;
        }).join('');

        const selectChat = item => {
            const userId = Number(item.dataset.userId) || null;
            window.App.selectUser(item.dataset.chatname, userId);
        };
        list.querySelectorAll('[data-chat-select]').forEach(item => {
            item.addEventListener('click', () => selectChat(item));
        });
    },

    bindManagedChatPicker() {
        const picker = document.getElementById('chatPicker');
        if (!picker || picker.dataset.bound) return;
        picker.dataset.bound = 'true';
        document.addEventListener('click', event => {
            if (picker.open && !picker.contains(event.target)) picker.open = false;
        });
        picker.addEventListener('keydown', event => {
            if (event.key === 'Escape') {
                picker.open = false;
                picker.querySelector('summary')?.focus();
            }
        });
    },

    closeManagedChatPicker() {
        const picker = document.getElementById('chatPicker');
        if (picker) picker.open = false;
    },

    setActiveManagedChat(chatName) {
        const selectedName = String(chatName || '');
        document.querySelectorAll('#usersList [data-chat-select]').forEach(item => {
            const isSelected = item.dataset.chatname === selectedName;
            item.classList.toggle('active', isSelected);
            item.setAttribute('aria-current', isSelected ? 'true' : 'false');
        });
        const chat = (window.App._managedChats || []).find(item => item.chat_name === selectedName);
        if (chat) this.setManagedChatContext(chat);
    },

    setManagedChatContext(chat) {
        const label = document.getElementById('selectedChatLabel');
        const meta = document.getElementById('selectedChatMeta');
        const avatar = document.getElementById('selectedChatAvatar');
        const userId = Number(chat?.user_id || chat?.id || 0);
        const chatName = String(chat?.chat_name || '');
        const displayName = chatName || '选择一个聊天';
        const listening = Boolean(chat?.listening_active ?? chat?.is_listening);
        const listeningEnabled = chat?.listening_enabled !== false;
        const typeLabel = chat?.is_group ? '群聊' : '私聊';
        const statusLabel = listening ? '监听中' : (listeningEnabled ? '等待连接' : '已暂停');
        if (label) label.textContent = displayName;
        if (meta) meta.textContent = chatName
            ? `${typeLabel} · ${statusLabel}`
            : '尚未选择';
        if (avatar) avatar.innerHTML = `<i class="bi ${chatName ? (chat?.is_group ? 'bi-people' : 'bi-person') : 'bi-chat-square-text'}"></i>`;
        const memoryButton = document.getElementById('chatMemoryButton');
        const deleteButton = document.getElementById('chatDeleteButton');
        if (memoryButton) memoryButton.disabled = !userId;
        if (deleteButton) deleteButton.disabled = !userId;
    },

    resetManagedChatContext() {
        this.setManagedChatContext(null);
        const saveButton = document.getElementById('chatPolicySaveButton');
        if (saveButton) {
            saveButton.disabled = true;
            saveButton.classList.remove('is-dirty', 'is-saving');
            const label = saveButton.querySelector('span');
            if (label) label.textContent = '已保存';
        }
    },

    renderManagedChatPending(chatName) {
        const container = document.getElementById('userPermissionsPanelContainer');
        if (!container) return;
        container.setAttribute('aria-busy', 'true');
        container.innerHTML = `
            <div class="chat-policy-empty">
                <span class="spinner-border spinner-border-sm" aria-hidden="true"></span>
                <h6>${this.escapeHtml(chatName || '聊天')}</h6>
                <p>正在加载聊天配置…</p>
            </div>
        `;
        const saveButton = document.getElementById('chatPolicySaveButton');
        if (saveButton) saveButton.disabled = true;
    },

    renderManagedChatError(chatName) {
        const container = document.getElementById('userPermissionsPanelContainer');
        if (!container) return;
        container.removeAttribute('aria-busy');
        container.innerHTML = `
            <div class="chat-policy-empty error">
                <i class="bi bi-exclamation-circle" aria-hidden="true"></i>
                <h6>${this.escapeHtml(chatName || '聊天')}</h6>
                <p>聊天配置加载失败，请重试。</p>
            </div>
        `;
    },

    getSystemSettingsGroupFromPath() {
        const path = this.normalizePath(window.location.pathname);
        return {
            '/system/providers': 'integrations',
            '/system/integrations': 'integrations',
            '/system/runtime': 'runtime',
            '/system/developer': 'developer',
            '/system/operations': 'operations',
            '/system/tools': 'tools',
            '/system/backups': 'backups'
        }[path] || 'identity';
    },

    renderSystemSettings(consoleData) {
        const container = document.getElementById('settings');
        if (!container) return;
        const groups = consoleData.groups || [];
        const primaryGroups = groups.filter(group => group.id !== 'developer');
        const extensionGroups = groups.filter(group => group.id === 'developer');
        const platformGroups = [
            ...primaryGroups,
            {
                id: 'operations', title: '运行状态', icon: 'bi-heart-pulse',
                description: '统一任务、插件状态和组件健康'
            },
            {
                id: 'tools', title: '工具与更新', icon: 'bi-box-arrow-up',
                description: '升级必要组件并修复媒体与浏览器运行时'
            },
            {
                id: 'backups', title: '备份与迁移', icon: 'bi-shield-check',
                description: '状态备份、完整迁移和安全恢复'
            },
            ...extensionGroups
        ];
        const identity = consoleData.identity || {};
        const requested = this.getSystemSettingsGroupFromPath();
        const activeId = platformGroups.some(group => group.id === requested) ? requested : groups[0]?.id;
        const navigation = platformGroups.map(group => `
            <button type="button" class="system-settings-nav-item ${group.id === activeId ? 'active' : ''}" data-system-group="${this.escapeHtml(group.id)}" data-system-icon="${this.escapeHtml(group.icon)}">
                <i class="bi ${this.escapeHtml(group.icon)}"></i>
                <span><strong>${this.escapeHtml(group.title)}</strong></span>
            </button>
        `).join('');
        const activeGroup = platformGroups.find(group => group.id === activeId) || platformGroups[0] || {};
        const mobileNavigation = `
            <details class="system-settings-mobile-picker" id="systemSettingsMobilePicker">
                <summary class="system-settings-mobile-toggle" aria-label="选择系统分区">
                    <span class="system-settings-mobile-icon" data-system-mobile-icon><i class="bi ${this.escapeHtml(activeGroup.icon || 'bi-sliders')}"></i></span>
                    <span class="system-settings-mobile-current">
                        <strong data-system-mobile-title>${this.escapeHtml(activeGroup.title || '系统')}</strong>
                        <small data-system-mobile-description>${this.escapeHtml(activeGroup.description || '选择需要管理的系统分区')}</small>
                    </span>
                    <i class="bi bi-chevron-down system-settings-mobile-chevron" aria-hidden="true"></i>
                </summary>
                <div class="system-settings-mobile-popover">
                    <div class="system-settings-mobile-popover-head">
                        <strong>系统分区</strong><span>${platformGroups.length}</span>
                    </div>
                    <div class="system-settings-mobile-list" role="listbox" aria-label="系统设置分区">
                        ${platformGroups.map(group => `
                            <button type="button" class="system-settings-mobile-item ${group.id === activeId ? 'active' : ''}"
                                data-system-mobile-group="${this.escapeHtml(group.id)}" data-system-icon="${this.escapeHtml(group.icon)}"
                                data-system-title="${this.escapeHtml(group.title)}" data-system-description="${this.escapeHtml(group.description || '')}"
                                role="option" aria-selected="${group.id === activeId ? 'true' : 'false'}">
                                <span class="system-settings-mobile-item-icon"><i class="bi ${this.escapeHtml(group.icon)}"></i></span>
                                <span class="system-settings-mobile-item-copy"><strong>${this.escapeHtml(group.title)}</strong><small>${this.escapeHtml(group.description || '')}</small></span>
                                <i class="bi bi-check-lg system-settings-mobile-check" aria-hidden="true"></i>
                            </button>`).join('')}
                    </div>
                </div>
            </details>`;
        const renderField = (field, group) => {
                const inputId = `system-setting-${field.key}`;
                const restart = field.requires_restart ? '<span class="system-restart-pill">需重启</span>' : '';
                let control = '';
                if (!field.editable) {
                    control = `<div class="system-setting-readonly"><i class="bi bi-info-circle"></i><span>${this.escapeHtml(field.readonly_text || '当前页面只展示状态')}</span></div>`;
                } else if (field.control === 'select') {
                    control = `
                        <select class="form-select system-setting-input" id="${inputId}" name="${this.escapeHtml(field.key)}" data-original="${this.escapeHtml(field.value || '')}" data-sensitive="false">
                            ${(field.options || []).map(option => `<option value="${this.escapeHtml(option.value)}" ${String(field.value) === String(option.value) ? 'selected' : ''}>${this.escapeHtml(option.label)}</option>`).join('')}
                        </select>`;
                } else {
                    const isSecret = field.sensitive;
                    const value = isSecret ? '' : (field.value ?? '');
                    const placeholder = isSecret
                        ? (field.configured ? '已配置—输入新值以替换' : '未配置')
                        : '';
                    control = `
                        <div class="system-setting-control">
                            <input type="${field.control === 'number' ? 'number' : isSecret ? 'password' : 'text'}" class="form-control system-setting-input"
                                id="${inputId}" name="${this.escapeHtml(field.key)}" value="${this.escapeHtml(String(value))}"
                                placeholder="${this.escapeHtml(placeholder)}" data-original="${this.escapeHtml(String(value))}" data-sensitive="${isSecret}">
                            ${isSecret ? `<button type="button" class="btn btn-light border" data-reveal-setting="${inputId}" aria-label="显示或隐藏 ${this.escapeHtml(field.title)}"><i class="bi bi-eye"></i></button>` : ''}
                        </div>`;
                }
                return `
                    <div class="system-setting-row">
                        <div class="system-setting-copy">
                            <div><label for="${inputId}">${this.escapeHtml(field.title)}</label>${restart}</div>
                            <p>${this.escapeHtml(field.description || '')}</p>
                            ${group.id === 'developer' ? `<div class="system-setting-meta"><code>${this.escapeHtml(field.key)}</code></div>` : ''}
                        </div>
                        <div>${control}</div>
                    </div>`;
        };
        const sections = groups.map(group => {
            const sectionNames = [];
            const fieldsBySection = {};
            (group.fields || []).forEach(field => {
                const name = field.section || '其他';
                if (!fieldsBySection[name]) {
                    sectionNames.push(name);
                    fieldsBySection[name] = [];
                }
                fieldsBySection[name].push(field);
            });
            const fields = sectionNames.map(name => `
                <div class="system-setting-subsection">
                    ${sectionNames.length > 1 ? `<div class="system-setting-subsection-title"><span>${this.escapeHtml(name)}</span></div>` : ''}
                    ${fieldsBySection[name].map(field => renderField(field, group)).join('')}
                </div>`).join('') || '<div class="system-settings-empty">当前没有此类设置。</div>';
            const editable = (group.fields || []).some(field => field.editable);
            const developerActions = group.id === 'developer' ? `
                <div class="system-developer-actions">
                    <div><strong>自定义配置</strong><small>仅在扩展文档明确要求时添加或导入。</small></div>
                    <button class="btn btn-sm btn-light border" onclick="UI.showAddSettingModal()"><i class="bi bi-plus-lg me-1"></i>添加自定义键</button>
                    <button class="btn btn-sm btn-light border" onclick="App.reloadSettingsFromEnv()"><i class="bi bi-arrow-clockwise me-1"></i>导入 .env 到数据库</button>
                </div>` : '';
            const description = group.id === 'identity'
                ? `已识别 ${Number(identity.detected_count || 0)} / ${Number(identity.group_count || 0)} 个群聊昵称；全局名称仅在没有聊天级名称时使用。`
                : group.description;
            const contextAction = group.id === 'identity'
                ? '<a class="btn btn-sm btn-light" href="/assistant/chats">各聊天身份<i class="bi bi-arrow-right ms-1"></i></a>'
                : '';
            return `
                <section class="system-settings-section ${group.id === activeId ? '' : 'd-none'}" data-system-section="${this.escapeHtml(group.id)}">
                    <div class="system-settings-section-head"><div><h3>${this.escapeHtml(group.title)}</h3><p>${this.escapeHtml(description)}</p></div><div class="system-settings-section-actions">${contextAction}${editable ? '<button class="btn btn-primary btn-sm" onclick="App.saveSettings()"><i class="bi bi-check-lg me-1"></i>保存</button>' : ''}</div></div>
                    <div class="system-settings-fields">${fields}</div>
                    ${developerActions}
                </section>`;
        }).join('');
        const platformSections = `
            <section class="system-settings-section ${activeId === 'operations' ? '' : 'd-none'}" data-system-section="operations">
                <div id="systemOperationsConsole" class="system-platform-console"><div class="loading-wrapper">正在读取运行状态…</div></div>
            </section>
            <section class="system-settings-section ${activeId === 'tools' ? '' : 'd-none'}" data-system-section="tools">
                <div id="systemToolsConsole" class="system-platform-console"><div class="loading-wrapper">正在读取工具状态…</div></div>
            </section>
            <section class="system-settings-section ${activeId === 'backups' ? '' : 'd-none'}" data-system-section="backups">
                <div id="systemBackupsConsole" class="system-platform-console"><div class="loading-wrapper">正在读取备份…</div></div>
            </section>`;

        container.innerHTML = `
            <div class="system-settings-shell">
                ${mobileNavigation}
                <aside class="system-settings-nav">${navigation}</aside>
                <main class="system-settings-main">${sections}${platformSections}</main>
            </div>`;
        container.dataset.activeSystemGroup = activeId || '';
        container.querySelectorAll('[data-system-group]').forEach(button => button.addEventListener('click', () => {
            this.switchSystemSettingsGroup(button.dataset.systemGroup);
        }));
        container.querySelectorAll('[data-system-mobile-group]').forEach(button => {
            button.addEventListener('click', () => {
                this.switchSystemSettingsGroup(button.dataset.systemMobileGroup, { mobile: true });
            });
        });
        const mobilePicker = container.querySelector('#systemSettingsMobilePicker');
        mobilePicker?.addEventListener('keydown', event => {
            if (event.key !== 'Escape') return;
            mobilePicker.open = false;
            mobilePicker.querySelector('summary')?.focus();
        });
        if (!this.systemSettingsPickerOutsideBound) {
            this.systemSettingsPickerOutsideBound = true;
            document.addEventListener('click', event => {
                const picker = document.getElementById('systemSettingsMobilePicker');
                if (picker?.open && !picker.contains(event.target)) picker.open = false;
            });
        }
        container.querySelectorAll('[data-reveal-setting]').forEach(button => button.addEventListener('click', () => {
            const input = document.getElementById(button.dataset.revealSetting);
            if (!input) return;
            input.type = input.type === 'password' ? 'text' : 'password';
            button.querySelector('i')?.classList.toggle('bi-eye');
            button.querySelector('i')?.classList.toggle('bi-eye-slash');
        }));
    },

    switchSystemSettingsGroup(groupId, options = {}) {
        const container = document.getElementById('settings');
        if (!container) return;
        container.dataset.activeSystemGroup = groupId;
        container.querySelectorAll('[data-system-group]').forEach(button => {
            button.classList.toggle('active', button.dataset.systemGroup === groupId);
        });
        container.querySelectorAll('[data-system-section]').forEach(section => {
            section.classList.toggle('d-none', section.dataset.systemSection !== groupId);
        });
        const mobilePicker = container.querySelector('#systemSettingsMobilePicker');
        const mobileItem = container.querySelector(`[data-system-mobile-group="${CSS.escape(groupId)}"]`);
        container.querySelectorAll('[data-system-mobile-group]').forEach(button => {
            const active = button.dataset.systemMobileGroup === groupId;
            button.classList.toggle('active', active);
            button.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        if (mobileItem) {
            const mobileIcon = container.querySelector('[data-system-mobile-icon]');
            const mobileTitle = container.querySelector('[data-system-mobile-title]');
            const mobileDescription = container.querySelector('[data-system-mobile-description]');
            if (mobileIcon) {
                const icon = document.createElement('i');
                icon.className = `bi ${mobileItem.dataset.systemIcon || 'bi-sliders'}`;
                mobileIcon.replaceChildren(icon);
            }
            if (mobileTitle) mobileTitle.textContent = mobileItem.dataset.systemTitle || '系统';
            if (mobileDescription) mobileDescription.textContent = mobileItem.dataset.systemDescription || '';
        }
        if (mobilePicker) mobilePicker.open = false;
        if (options.history !== false) {
            const paths = {
                identity: '/system', integrations: '/system/integrations',
                runtime: '/system/runtime', developer: '/system/developer',
                operations: '/system/operations', tools: '/system/tools', backups: '/system/backups'
            };
            const path = paths[groupId] || '/system';
            if (this.normalizePath(window.location.pathname) !== path) {
                window.history.pushState({ tab: 'settings', section: groupId }, '', path);
            }
        }
        if (groupId === 'operations') window.SystemOperations?.loadRuntime();
        if (groupId === 'tools') window.SystemTools?.load();
        if (groupId === 'backups') window.SystemOperations?.loadBackups();
        if (options.mobile && window.innerWidth <= 767.98) {
            const main = container.querySelector('.system-settings-main');
            window.requestAnimationFrame(() => main?.scrollIntoView({
                behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
                block: 'start'
            }));
        }
    },

    togglePassword(id) {
        const input = document.getElementById(id);
        if (input) {
            input.type = input.type === 'password' ? 'text' : 'password';
        }
    },

    renderChatPolicy(policy, capabilities, assistantOverview, profilesData) {
        const container = document.getElementById('userPermissionsPanelContainer');
        if (!container) return;
        container.removeAttribute('aria-busy');
        const chat = policy.chat || {};
        const assistant = policy.assistant || {};
        const codex = policy.codex || {};
        const memory = assistant.memory || { mode: 'inherit', overrides: {} };
        const globalMemory = assistantOverview.global?.memory || {};
        const memoryOverrides = memory.overrides || {};
        const memoryValue = (key, fallback) => memoryOverrides[key] !== undefined
            ? memoryOverrides[key]
            : (globalMemory[key] !== undefined ? globalMemory[key] : fallback);
        const grants = policy.plugin_grants || [];
        const grantByName = Object.fromEntries(grants.map(item => [item.plugin_name, item]));
        const roles = assistantOverview.roles || [];
        const judges = assistantOverview.judges || [];
        const selectedProfile = assistant.codex_profile_id || '';
        const selectedPluginCount = grants.filter(item => !String(item.plugin_name).includes('#')).length;
        const chatLogCapability = (capabilities || []).find(item => item.id === 'builtin_chat_logger');
        const chatLogEnabled = Boolean(chatLogCapability?.enabled);
        const chatLogStatus = !chatLogCapability
            ? '插件不可用'
            : (chatLogEnabled
                ? (chatLogCapability.loaded ? '持续记录新消息' : '已启用，但加载异常')
                : '已关闭，助手仍可正常回复');
        const formatLines = values => (values || []).join('\n');
        const roleOptions = roles.map(item => `<option value="${Number(item.id)}" ${Number(assistant.role_id) === Number(item.id) ? 'selected' : ''}>${this.escapeHtml(item.display_name || item.name)}</option>`).join('');
        const judgeOptions = judges.map(item => `<option value="${Number(item.id)}" ${Number(assistant.judge_id) === Number(item.id) ? 'selected' : ''}>${this.escapeHtml(item.display_name || item.name)}</option>`).join('');
        const profileOptions = this.renderCodexProfileOptions(profilesData, selectedProfile);
        const pluginCards = [...(capabilities || [])]
            .sort((a, b) => Number(Boolean(grantByName[b.id])) - Number(Boolean(grantByName[a.id]))
                || Number(b.featured) - Number(a.featured)
                || String(a.display_name || a.id).localeCompare(String(b.display_name || b.id)))
            .map(capability => {
                const grant = grantByName[capability.id];
                const pushGrant = grantByName[`${capability.id}#push`];
                const checked = Boolean(grant);
                const available = Boolean(capability.enabled && capability.loaded);
                const supportsPush = (capability.features || []).includes('push');
                const configurable = Boolean(capability.configurable);
                const searchValue = `${capability.display_name || ''} ${capability.id} ${capability.description || ''}`.toLowerCase();
                return `
                    <article class="chat-policy-plugin ${checked ? 'selected' : ''} ${available ? '' : 'unavailable'}"
                        data-plugin-card="${this.escapeHtml(capability.id)}" data-plugin-search="${this.escapeHtml(searchValue)}">
                        <div class="chat-policy-plugin-main">
                            <span><i class="bi ${this.escapeHtml(capability.icon || 'bi-puzzle')}"></i></span>
                            <div><strong>${this.escapeHtml(capability.display_name || capability.id)}</strong><small>${this.escapeHtml(capability.description || capability.category_label || '')}</small></div>
                            <button type="button" class="chat-policy-plugin-config" data-plugin-config="${this.escapeHtml(capability.id)}"
                                ${configurable ? '' : 'disabled'} title="${configurable ? '配置此功能插件' : '此功能插件没有可配置项'}" aria-label="配置 ${this.escapeHtml(capability.display_name || capability.id)}">
                                <i class="bi bi-sliders"></i><span>配置</span>
                            </button>
                            <input class="form-check-input chat-policy-plugin-toggle" type="checkbox" value="${this.escapeHtml(capability.id)}" ${checked ? 'checked' : ''} ${available ? '' : 'disabled'} aria-label="启用 ${this.escapeHtml(capability.display_name || capability.id)}">
                        </div>
                        <div class="chat-policy-plugin-options">
                            ${chat.is_group ? `<label><input class="form-check-input chat-policy-plugin-mention" type="checkbox" ${grant?.require_mention ? 'checked' : ''} ${checked && available ? '' : 'disabled'}><span>仅在 @Bot 时触发</span></label>` : ''}
                            ${supportsPush ? `<label><input class="form-check-input chat-policy-plugin-push" type="checkbox" ${pushGrant ? 'checked' : ''} ${checked && available ? '' : 'disabled'}><span>允许后台推送</span></label>` : ''}
                        </div>
                    </article>`;
            }).join('');
        const activePane = ['assistant', 'plugins', 'advanced'].includes(this.chatPolicyActivePane)
            ? this.chatPolicyActivePane
            : 'assistant';

        container.innerHTML = `
            <form id="chatPolicyForm" class="chat-policy-shell" data-user-id="${Number(policy.user_id)}"
                data-version="${Number(policy.version)}" data-original-group="${Boolean(chat.is_group)}">
                <section class="chat-policy-summary" aria-label="当前聊天关键状态">
                    <label class="chat-policy-state-toggle">
                        <span><i class="bi bi-ear"></i><span><strong>接收消息</strong><small>${chat.listening_active ? '监听已生效' : (chat.listening_enabled ? '等待连接同步' : '当前已暂停')}</small></span></span>
                        <input class="form-check-input" type="checkbox" name="listening_enabled" ${chat.listening_enabled ? 'checked' : ''}>
                    </label>
                    <label class="chat-policy-state-toggle">
                        <span><i class="bi bi-stars"></i><span><strong>AI 助手</strong><small data-assistant-status>${assistant.enabled ? '已启用' : '已关闭'}</small></span></span>
                        <input class="form-check-input" type="checkbox" name="assistant_enabled" ${assistant.enabled ? 'checked' : ''}>
                    </label>
                    <label class="chat-policy-state-toggle">
                        <span><i class="bi bi-journal-text"></i><span><strong>启用聊天记录</strong><small data-chat-log-plugin-status>${this.escapeHtml(chatLogStatus)}</small></span></span>
                        <input class="form-check-input" type="checkbox" data-chat-log-plugin-toggle ${chatLogEnabled ? 'checked' : ''} ${chatLogCapability ? '' : 'disabled'} aria-label="启用聊天记录插件">
                    </label>
                </section>

                <nav class="chat-policy-tabs" role="tablist" aria-label="聊天配置分区">
                    <button type="button" class="${activePane === 'assistant' ? 'active' : ''}" data-chat-policy-tab="assistant" aria-selected="${activePane === 'assistant'}"><i class="bi bi-stars"></i>助手</button>
                    <button type="button" class="${activePane === 'plugins' ? 'active' : ''}" data-chat-policy-tab="plugins" aria-selected="${activePane === 'plugins'}"><i class="bi bi-puzzle"></i>功能插件 <span data-plugin-tab-count>${selectedPluginCount}</span></button>
                    <button type="button" class="${activePane === 'advanced' ? 'active' : ''}" data-chat-policy-tab="advanced" aria-selected="${activePane === 'advanced'}"><i class="bi bi-sliders"></i>高级</button>
                </nav>

                <div class="chat-policy-pane ${activePane === 'assistant' ? 'active' : ''}" data-chat-policy-pane="assistant">
                    <section class="chat-policy-block">
                        <div class="chat-policy-block-head"><h4>回复身份</h4></div>
                        <div class="chat-policy-field-grid three">
                            <label><span class="chat-field-label"><span>Codex Profile</span><a href="/codex" onclick="event.preventDefault(); UI.switchTab('codex')">管理</a></span><select class="form-select" name="codex_profile_id">${profileOptions}</select></label>
                            <label><span class="chat-field-label"><span>角色</span><a href="/assistant/roles" onclick="event.preventDefault(); App.openAssistantRoleManager()">管理</a></span><select class="form-select" name="role_id"><option value="">继承全局默认角色</option>${roleOptions}</select></label>
                            <label><span>Codex 访问范围</span><select class="form-select" name="codex_mode" data-private-value="${this.escapeHtml(codex.mode || 'isolated')}" ${chat.is_group ? 'disabled' : ''}><option value="isolated" ${codex.mode === 'owner_full' ? '' : 'selected'}>隔离空间</option>${chat.is_group ? '' : `<option value="owner_full" ${codex.mode === 'owner_full' ? 'selected' : ''}>管理员 · 本机最大权限</option>`}</select><small class="field-help" title="${this.escapeHtml(codex.label || '隔离空间')} · ${this.escapeHtml(codex.workdir || '')}">${this.escapeHtml(codex.label || '隔离空间')} · ${this.escapeHtml(codex.workdir || '')}</small></label>
                        </div>
                    </section>

                    <section class="chat-policy-block">
                        <div class="chat-policy-block-head"><h4>对话行为</h4></div>
                        <div class="chat-setting-list">
                            <div class="chat-setting-group">
                                <label class="chat-setting-row"><span><strong>连续对话</strong><small>短时间内无需再次唤醒</small></span><input class="form-check-input" type="checkbox" name="followup_enabled" ${assistant.followup_enabled ? 'checked' : ''}></label>
                                <div class="chat-policy-dependent" data-followup-settings>
                                    <label><span>有效窗口（秒）</span><input class="form-control" name="followup_window_seconds" type="number" min="10" max="600" value="${Number(assistant.followup_window_seconds || 60)}" required></label>
                                    <label><span>消息合并（秒）</span><input class="form-control" name="followup_merge_seconds" type="number" min="1" max="30" value="${Number(assistant.followup_merge_seconds || 3)}" required></label>
                                    <label><span>最多轮数</span><input class="form-control" name="followup_max_turns" type="number" min="1" max="10" value="${Number(assistant.followup_max_turns || 3)}" required></label>
                                </div>
                            </div>
                            ${chat.is_group ? `<div class="chat-setting-group">
                                <label class="chat-setting-row"><span><strong>主动参与群聊</strong><small>${judges.length ? '由回复判断器决定是否加入对话' : '需要先创建回复判断器'}</small></span><input class="form-check-input" type="checkbox" name="proactive_enabled" ${assistant.proactive_enabled ? 'checked' : ''} ${judges.length ? '' : 'disabled'}></label>
                                <div class="chat-policy-field-grid three chat-policy-group-settings">
                                    <label data-judge-setting><span class="chat-field-label"><span>回复判断器</span><a href="/assistant/roles" onclick="event.preventDefault(); App.openAssistantRoleManager()">管理</a></span><select class="form-select" name="judge_id"><option value="">选择判断器</option>${judgeOptions}</select>${judges.length ? '' : '<small class="chat-field-status">尚未创建判断器</small>'}</label>
                                    <label><span>群内机器人昵称</span><input class="form-control" name="bot_group_nickname" value="${this.escapeHtml(chat.bot_group_nickname || '')}" maxlength="128" placeholder="无需填写 @"><small>${chat.bot_group_nickname_detected ? `最近识别：${this.escapeHtml(chat.bot_group_nickname_detected)}` : '用于识别群内 @ 名称'}</small></label>
                                    <label class="chat-policy-toggle-field"><span>自动校准</span><span class="chat-toggle-control"><small>从微信读取本群昵称</small><input class="form-check-input" type="checkbox" name="bot_group_nickname_auto_enabled" ${chat.bot_group_nickname_auto_enabled ? 'checked' : ''}></span></label>
                                </div>
                            </div>` : ''}
                        </div>
                    </section>

                    <section class="chat-policy-block chat-policy-memory">
                        <div class="chat-policy-block-head"><div class="chat-policy-title-row"><h4>长期记忆</h4><button class="chat-policy-inline-action" type="button" onclick="App.showCapabilitySettings('assistant', {focusGroup: 'memory'})">全局设置</button></div><div class="chat-policy-block-actions"><button class="chat-policy-head-action" type="button" onclick="App.openSelectedChatMemory()"><i class="bi bi-database"></i>记忆库</button></div></div>
                        <div class="chat-memory-mode-grid">
                            <label class="chat-memory-mode"><input type="radio" name="memory_mode" value="inherit" ${memory.mode === 'inherit' ? 'checked' : ''}><span>继承全局</span></label>
                            <label class="chat-memory-mode"><input type="radio" name="memory_mode" value="off" ${memory.mode === 'off' ? 'checked' : ''}><span>关闭</span></label>
                            <label class="chat-memory-mode"><input type="radio" name="memory_mode" value="custom" ${memory.mode === 'custom' ? 'checked' : ''}><span>自定义</span></label>
                        </div>
                        <div class="chat-memory-custom" data-memory-custom>
                            <div class="chat-policy-field-grid two">
                                <label class="chat-policy-toggle-field"><span>证据复核</span><span class="chat-toggle-control"><small>低可信内容自动隔离，不产生人工任务</small><input class="form-check-input" type="checkbox" name="memory_verification_enabled" ${memoryValue('memory_verification_enabled', true) ? 'checked' : ''}></span></label>
                                <label class="chat-policy-toggle-field"><span>人物记忆</span><span class="chat-toggle-control"><small>维护人物事实与关系</small><input class="form-check-input" type="checkbox" name="memory_person_enabled" ${memoryValue('memory_person_enabled', true) ? 'checked' : ''}></span></label>
                            </div>
                            <div class="chat-policy-field-grid two chat-memory-fields">
                                <label><span>检索时间范围（天）</span><input class="form-control" name="memory_retention_days" type="number" min="0" max="3650" value="${Number(memoryValue('memory_retention_days', 365))}" required></label>
                                <label><span>每次最多召回</span><input class="form-control" name="memory_retrieval_top_k" type="number" min="1" max="20" value="${Number(memoryValue('memory_retrieval_top_k', 6))}" required></label>
                            </div>
                        </div>
                    </section>
                </div>

                <div class="chat-policy-pane ${activePane === 'plugins' ? 'active' : ''}" data-chat-policy-pane="plugins">
                    <section class="chat-policy-block">
                        <div class="chat-policy-block-head"><h4>功能插件</h4><div class="chat-policy-block-actions"><a class="chat-policy-head-action" href="/plugins" onclick="event.preventDefault(); UI.switchTab('plugins')"><i class="bi bi-box-arrow-up-right"></i>管理功能插件</a></div></div>
                        <div class="chat-plugin-toolbar">
                            <label><i class="bi bi-search"></i><input type="search" data-plugin-search-input placeholder="搜索插件" autocomplete="off"></label>
                            <div><button type="button" class="active" data-plugin-filter="enabled">已启用</button><button type="button" data-plugin-filter="all">全部</button></div>
                        </div>
                        <div class="chat-policy-plugin-grid">${pluginCards || '<div class="assistant-empty-inline">当前没有可授权插件。</div>'}</div>
                        <div class="chat-plugin-filter-empty d-none" data-plugin-filter-empty>没有符合当前条件的插件</div>
                    </section>
                </div>

                <div class="chat-policy-pane ${activePane === 'advanced' ? 'active' : ''}" data-chat-policy-pane="advanced">
                    <section class="chat-policy-block chat-policy-type-block" aria-label="聊天类型设置">
                        <div class="chat-policy-field-grid compact">
                            <label><span>聊天类型</span><select class="form-select" name="chat_type"><option value="private" ${chat.is_group ? '' : 'selected'}>私聊</option><option value="group" ${chat.is_group ? 'selected' : ''}>群聊</option></select><small class="chat-field-status" data-chat-type-note hidden></small></label>
                        </div>
                    </section>
                    <section class="chat-policy-block">
                        <div class="chat-policy-block-head"><h4>忽略规则</h4></div>
                        <div class="chat-policy-field-grid two">
                            <label><span>全链路忽略</span><textarea class="form-control" name="sender_blacklist" rows="5" placeholder="每行一个发送者名称">${this.escapeHtml(formatLines(chat.sender_blacklist))}</textarea></label>
                            <label><span>仅 AI 助手忽略</span><textarea class="form-control" name="assistant_ignored_senders" rows="5" placeholder="每行一个发送者名称">${this.escapeHtml(formatLines(assistant.ignored_senders))}</textarea></label>
                        </div>
                    </section>
                </div>
            </form>`;

        const form = document.getElementById('chatPolicyForm');
        this.setManagedChatContext({ ...chat, user_id: policy.user_id });
        this.bindChatPolicyForm(form);
    },

    renderCodexProfileOptions(profilesData = {}, selectedProfile = '') {
        const profiles = Array.isArray(profilesData.profiles) ? profilesData.profiles : [];
        const selected = String(selectedProfile || '');
        const defaultLabel = profilesData.default_profile_id
            ? `继承默认 · ${profilesData.default_profile_id}`
            : '继承默认 · 尚未配置默认 Profile';
        const visibleProfiles = profiles.filter(item => item.available || item.name === selected);
        return [
            `<option value="" ${selected ? '' : 'selected'}>${this.escapeHtml(defaultLabel)}</option>`,
            ...visibleProfiles.map(item => `<option value="${this.escapeHtml(item.name)}" ${selected === item.name ? 'selected' : ''}>${this.escapeHtml(item.name)} · ${this.escapeHtml(item.model)}${item.available ? '' : ' · 尚不可用'}</option>`)
        ].join('');
    },

    updateChatPolicyProfileOptions(profilesData) {
        const form = document.getElementById('chatPolicyForm');
        const select = form?.elements?.codex_profile_id;
        if (!select) return;
        const selected = select.value;
        select.innerHTML = this.renderCodexProfileOptions(profilesData, selected);
        if ([...select.options].some(option => option.value === selected)) {
            select.value = selected;
        }
        this.syncChatPolicyDirty(form);
    },

    bindChatPolicyForm(form) {
        if (!form) return;
        const syncPlugin = toggle => {
            const card = toggle.closest('.chat-policy-plugin');
            card.classList.toggle('selected', toggle.checked);
            card.querySelectorAll('.chat-policy-plugin-options input').forEach(input => {
                input.disabled = !toggle.checked || card.classList.contains('unavailable');
                if (!toggle.checked) input.checked = false;
            });
        };
        form.querySelectorAll('.chat-policy-plugin-toggle').forEach(toggle => {
            toggle.addEventListener('change', () => syncPlugin(toggle));
            syncPlugin(toggle);
        });
        form.querySelectorAll('[data-plugin-config]').forEach(button => {
            button.addEventListener('click', event => {
                event.preventDefault();
                event.stopPropagation();
                App.showPluginSettings(button.dataset.pluginConfig);
            });
        });
        const chatLogPluginToggle = form.querySelector('[data-chat-log-plugin-toggle]');
        chatLogPluginToggle?.addEventListener('change', async event => {
            event.stopPropagation();
            const requested = Boolean(chatLogPluginToggle.checked);
            chatLogPluginToggle.disabled = true;
            const applied = await App.togglePlugin('builtin_chat_logger', requested, {
                displayName: '聊天记录',
                refreshWorkbench: false
            });
            if (!applied) chatLogPluginToggle.checked = !requested;
            const enabled = Boolean(chatLogPluginToggle.checked);
            const status = form.querySelector('[data-chat-log-plugin-status]');
            if (status) status.textContent = enabled ? '持续记录新消息' : '已关闭，助手仍可正常回复';
            chatLogPluginToggle.disabled = false;
        });

        const syncDependentControls = () => {
            const followupEnabled = form.elements.followup_enabled.checked;
            form.querySelector('[data-followup-settings]')?.classList.toggle('is-collapsed', !followupEnabled);
            const proactiveEnabled = Boolean(form.elements.proactive_enabled?.checked);
            const judgeSetting = form.querySelector('[data-judge-setting]');
            if (judgeSetting) {
                judgeSetting.classList.toggle('is-disabled', !proactiveEnabled);
                form.elements.judge_id.disabled = !proactiveEnabled;
                form.elements.judge_id.required = proactiveEnabled;
                if (proactiveEnabled && !form.elements.judge_id.value) {
                    form.elements.judge_id.value = form.elements.judge_id.querySelector('option[value]:not([value=""])')?.value || '';
                }
            }
            const memoryMode = form.elements.memory_mode.value;
            form.querySelector('[data-memory-custom]')?.classList.toggle('is-collapsed', memoryMode !== 'custom');
            form.querySelectorAll('.chat-memory-mode').forEach(card => {
                card.classList.toggle('selected', card.querySelector('input').checked);
            });
            const groupSelected = form.elements.chat_type.value === 'group';
            const codexSelect = form.elements.codex_mode;
            if (groupSelected) {
                if (codexSelect.value !== 'isolated') codexSelect.dataset.privateValue = codexSelect.value;
                codexSelect.value = 'isolated';
                codexSelect.disabled = true;
            } else {
                codexSelect.disabled = false;
                const privateValue = codexSelect.dataset.privateValue;
                if ([...codexSelect.options].some(option => option.value === privateValue)) codexSelect.value = privateValue;
            }
            const typeChanged = groupSelected !== (form.dataset.originalGroup === 'true');
            const typeNote = form.querySelector('[data-chat-type-note]');
            if (typeNote) {
                typeNote.hidden = !typeChanged;
                typeNote.textContent = typeChanged ? '保存后将重新加载对应设置' : '';
            }
        };

        const updateSummary = () => {
            const selectedCount = form.querySelectorAll('.chat-policy-plugin-toggle:checked').length;
            form.querySelectorAll('[data-plugin-tab-count]').forEach(item => { item.textContent = selectedCount; });
            const assistantStatus = form.querySelector('[data-assistant-status]');
            if (assistantStatus) assistantStatus.textContent = form.elements.assistant_enabled.checked ? '已启用' : '已关闭';
        };

        form.querySelectorAll('[data-chat-policy-tab]').forEach(button => {
            button.addEventListener('click', () => this.switchChatPolicyPane(button.dataset.chatPolicyTab));
        });
        const pluginSearch = form.querySelector('[data-plugin-search-input]');
        const applyPluginFilter = () => {
            const query = String(pluginSearch?.value || '').trim().toLowerCase();
            const activeFilter = form.querySelector('[data-plugin-filter].active')?.dataset.pluginFilter || 'enabled';
            let visible = 0;
            form.querySelectorAll('[data-plugin-card]').forEach(card => {
                const matchesQuery = !query || String(card.dataset.pluginSearch || '').includes(query);
                const matchesFilter = activeFilter === 'all' || card.classList.contains('selected');
                const show = matchesQuery && matchesFilter;
                card.classList.toggle('d-none', !show);
                if (show) visible += 1;
            });
            form.querySelector('[data-plugin-filter-empty]')?.classList.toggle('d-none', visible > 0);
        };
        pluginSearch?.addEventListener('input', this.debounce(applyPluginFilter, 100));
        form.querySelectorAll('[data-plugin-filter]').forEach(button => button.addEventListener('click', () => {
            form.querySelectorAll('[data-plugin-filter]').forEach(item => item.classList.toggle('active', item === button));
            applyPluginFilter();
        }));

        form.addEventListener('change', () => {
            syncDependentControls();
            updateSummary();
            applyPluginFilter();
            this.syncChatPolicyDirty(form);
        });
        form.addEventListener('input', this.debounce(() => this.syncChatPolicyDirty(form), 60));
        syncDependentControls();
        updateSummary();
        applyPluginFilter();
        form._initialSnapshot = this.serializeChatPolicyForm(form);
        this.syncChatPolicyDirty(form);
    },

    switchChatPolicyPane(pane) {
        const form = document.getElementById('chatPolicyForm');
        if (!form || !['assistant', 'plugins', 'advanced'].includes(pane)) return;
        this.chatPolicyActivePane = pane;
        form.querySelectorAll('[data-chat-policy-tab]').forEach(button => {
            const active = button.dataset.chatPolicyTab === pane;
            button.classList.toggle('active', active);
            button.setAttribute('aria-selected', String(active));
        });
        form.querySelectorAll('[data-chat-policy-pane]').forEach(panel => {
            panel.classList.toggle('active', panel.dataset.chatPolicyPane === pane);
        });
    },

    serializeChatPolicyForm(form) {
        if (!form) return '';
        const values = [...form.querySelectorAll([
            'input[name]',
            'select[name]',
            'textarea[name]',
            '.chat-policy-plugin-toggle',
            '.chat-policy-plugin-mention',
            '.chat-policy-plugin-push'
        ].join(', '))].map(control => ({
            name: control.name,
            type: control.type,
            value: control.value,
            checked: ['checkbox', 'radio'].includes(control.type) ? control.checked : null,
            disabled: control.disabled
        }));
        return JSON.stringify(values);
    },

    isChatPolicyDirty() {
        const form = document.getElementById('chatPolicyForm');
        return Boolean(form?._initialSnapshot && this.serializeChatPolicyForm(form) !== form._initialSnapshot);
    },

    syncChatPolicyDirty(form = document.getElementById('chatPolicyForm')) {
        if (!form) return;
        const dirty = Boolean(form._initialSnapshot && this.serializeChatPolicyForm(form) !== form._initialSnapshot);
        const button = document.getElementById('chatPolicySaveButton');
        if (!button) return;
        button.disabled = !dirty;
        button.classList.toggle('is-dirty', dirty);
        button.classList.remove('is-saving');
        const label = button.querySelector('span');
        if (label) label.textContent = dirty ? '保存更改' : '已保存';
    },

    setChatPolicySaving(saving) {
        const button = document.getElementById('chatPolicySaveButton');
        if (!button) return;
        button.disabled = Boolean(saving);
        button.classList.toggle('is-saving', Boolean(saving));
        const label = button.querySelector('span');
        if (label) label.textContent = saving ? '正在保存' : '保存更改';
    },

    renderUnmanagedChatPolicy(chatName) {
        const container = document.getElementById('userPermissionsPanelContainer');
        if (!container) return;
        container.removeAttribute('aria-busy');
        const saveButton = document.getElementById('chatPolicySaveButton');
        if (saveButton) {
            saveButton.disabled = true;
            saveButton.classList.remove('is-dirty', 'is-saving');
            const label = saveButton.querySelector('span');
            if (label) label.textContent = '已保存';
        }
        container.innerHTML = `
            <div class="chat-policy-unmanaged">
                <span><i class="bi bi-shield-plus"></i></span>
                <h3>${this.escapeHtml(chatName)}</h3>
                <p>微信正在监听这个聊天，但它还没有受控策略。先确认聊天类型，再配置 Assistant、Codex 权限或独立插件。</p>
                <div><button class="btn btn-primary" onclick="App.adoptActiveChat(this.dataset.chat, false)" data-chat="${this.escapeHtml(chatName)}"><i class="bi bi-person me-1"></i>作为私聊管理</button><button class="btn btn-outline-primary" onclick="App.adoptActiveChat(this.dataset.chat, true)" data-chat="${this.escapeHtml(chatName)}"><i class="bi bi-people me-1"></i>作为群聊管理</button></div>
            </div>`;
    },

    renderLogs(content, searchKeyword) {
        const container = document.getElementById('logContent');
        if (!container) return Promise.resolve({ matchCount: 0 });

        if (!content) {
            container.innerHTML = '<div class="logs-placeholder"><i class="bi bi-inbox"></i><span>无日志内容</span></div>';
            return Promise.resolve({ matchCount: 0 });
        }

        const lines = content.split('\n');
        const keyword = String(searchKeyword || '').trim();
        const literalPattern = keyword.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        let matchCount = 0;

        const renderSyntax = segment => {
            let escaped = this.escapeHtml(segment);
            escaped = escaped.replace(
                /\[(ERROR|WARNING|INFO|DEBUG|CRITICAL|FATAL)\]/gi,
                '<span class="log-level log-level-$1">[$1]</span>'
            );
            escaped = escaped.replace(
                /(\[([a-zA-Z_][a-zA-Z0-9_.]*)\])/g,
                '<span class="log-module">$1</span>'
            );
            return escaped.replace(
                /(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:,\d{3})?)/g,
                '<span class="log-time">$1</span>'
            );
        };

        const renderHighlightedLine = line => {
            if (!literalPattern) return renderSyntax(line);
            const regex = new RegExp(literalPattern, 'gi');
            let cursor = 0;
            let html = '';
            let match;
            while ((match = regex.exec(line)) !== null) {
                html += renderSyntax(line.slice(cursor, match.index));
                const matchIndex = matchCount++;
                html += `<mark class="log-search-match" data-log-match-index="${matchIndex}">${renderSyntax(match[0])}</mark>`;
                cursor = regex.lastIndex;
            }
            return html + renderSyntax(line.slice(cursor));
        };

        const renderLine = (line, lineNo) => {
            let lineClass = '';

            // Detect log level for line coloring
            if (/\[ERROR\]|Traceback|Error:|exception/i.test(line)) {
                lineClass = 'log-error';
            } else if (/\[WARNING\]|WARNING:/i.test(line)) {
                lineClass = 'log-warn';
            } else if (/\[(CRITICAL|FATAL)\]/i.test(line)) {
                lineClass = 'log-critical';
            }

            const escaped = renderHighlightedLine(line);
            return `<div class="log-line ${lineClass}" data-line="${lineNo}">` +
                `<span class="log-ln">${lineNo}</span>` +
                `<span class="log-text">${escaped}</span>` +
                `</div>`;
        };

        // Save current scroll position to prevent jumping
        const prevScrollTop = container.scrollTop;
        const prevScrollHeight = container.scrollHeight;
        const isAtBottom = container.scrollHeight - container.scrollTop <= container.clientHeight + 10;

        let html = '';
        for (let index = 0; index < lines.length; index++) {
            html += renderLine(lines[index], index + 1);
        }

        container.innerHTML = html;

        // Restore scroll position if not following
        if (!isAtBottom) {
            // Adjust scroll position based on height difference if lines were added
            const heightDiff = container.scrollHeight - prevScrollHeight;
            if (heightDiff > 0) {
                 container.scrollTop = prevScrollTop + heightDiff;
            } else {
                 container.scrollTop = prevScrollTop;
            }
        }

        return Promise.resolve({ matchCount });
    },

    renderCapabilitySettingsForm(settings, capability = {}) {
        const groups = settings.groups || [];
        const editableFields = groups.flatMap(group => group.fields || []).filter(field => !field.deprecated);
        const hasBasicFields = editableFields.some(field => field.level === 'basic');
        const levelFilter = hasBasicFields ? 'basic' : 'all';
        const globalMemory = editableFields.find(field => field.key === 'memory_enabled');
        const inheritanceSummary = settings.capability_id === 'assistant' && globalMemory ? `
            <div class="cap-settings-inheritance">
                <span class="cap-settings-inheritance-icon"><i class="bi bi-database-check"></i></span>
                <div><strong data-global-memory-summary>长期记忆全局默认：${globalMemory.value ? '开启' : '关闭'}</strong>
                    <small data-global-memory-help>聊天卡片上的“继承全局 · ${globalMemory.value ? '开启' : '关闭'}”就是来自这个设置。</small></div>
                <div class="cap-settings-inheritance-actions">
                    <label class="cap-settings-primary-toggle">
                        <span data-global-memory-toggle-label>${globalMemory.value ? '已开启' : '已关闭'}</span>
                        <input class="form-check-input" type="checkbox" data-global-memory-toggle ${globalMemory.value ? 'checked' : ''} aria-label="长期记忆全局总开关">
                    </label>
                    <button type="button" class="btn btn-sm btn-outline-primary" data-settings-jump="memory">详细设置</button>
                </div>
            </div>` : '';

        const nav = groups.map(group => {
            const visibleCount = (group.fields || []).filter(field => !field.deprecated).length;
            if (!visibleCount) return '';
            return `<button type="button" class="cap-settings-nav-item" data-settings-anchor="cap-settings-${this.escapeHtml(group.id)}">
                <span>${this.escapeHtml(group.title)}</span><small data-settings-visible-count>${visibleCount}</small>
            </button>`;
        }).join('');

        const sections = groups.map(group => {
            const fields = (group.fields || []).filter(field => !field.deprecated);
            if (!fields.length) return '';
            const fieldHtml = fields.map(field => this.renderCapabilitySettingsField(field)).join('');
            return `<section class="cap-settings-section" id="cap-settings-${this.escapeHtml(group.id)}">
                <header><div><h3>${this.escapeHtml(group.title)}</h3><p>${this.escapeHtml(group.description || '')}</p></div></header>
                <div class="cap-settings-fields">${fieldHtml}</div>
            </section>`;
        }).join('');

        return `
            <div class="cap-settings-shell" data-level-filter="${levelFilter}" data-capability-id="${this.escapeHtml(settings.capability_id || '')}">
                <aside class="cap-settings-aside">
                    <div class="cap-settings-capability">
                        <span class="capability-icon"><i class="bi ${this.escapeHtml(capability.icon || 'bi-sliders')}"></i></span>
                        <div><strong>${this.escapeHtml(capability.display_name || settings.capability_id)}</strong><small>${Number(settings.field_count || 0)} 项全局设置</small></div>
                    </div>
                    <nav class="cap-settings-nav">${nav}</nav>
                </aside>
                <main class="cap-settings-main">
                    <div class="cap-settings-toolbar">
                        <div class="cap-settings-search"><i class="bi bi-search"></i><input type="search" id="capabilitySettingsSearch" placeholder="搜索设置"></div>
                        <div class="btn-group btn-group-sm" role="group" aria-label="设置级别">
                            <button type="button" class="btn btn-outline-secondary ${levelFilter === 'basic' ? 'active' : ''}" data-settings-level="basic">常用</button>
                            <button type="button" class="btn btn-outline-secondary ${levelFilter === 'all' ? 'active' : ''}" data-settings-level="all">全部</button>
                        </div>
                    </div>
                    <div class="cap-settings-notice"><i class="bi bi-globe2"></i><span>${this.escapeHtml(settings.notice || '这里设置该能力对所有聊天的默认行为。')}</span></div>
                    ${inheritanceSummary}
                    <form id="capabilitySettingsForm">${sections}</form>
                    <div class="cap-settings-empty d-none" id="capabilitySettingsEmpty">没有匹配的设置</div>
                </main>
            </div>`;
    },

    renderCapabilitySettingsField(field) {
        const id = `cap-cfg-${field.key}`;
        const value = field.value ?? field.default ?? '';
        const common = `data-config-key="${this.escapeHtml(field.key)}" data-config-type="${this.escapeHtml(field.type)}" data-sensitive="${field.sensitive ? 'true' : 'false'}"`;
        const minimum = field.minimum !== null && field.minimum !== undefined ? ` min="${this.escapeHtml(String(field.minimum))}"` : '';
        const maximum = field.maximum !== null && field.maximum !== undefined ? ` max="${this.escapeHtml(String(field.maximum))}"` : '';
        const step = field.step !== null && field.step !== undefined ? ` step="${this.escapeHtml(String(field.step))}"` : (field.type === 'number' ? ' step="any"' : '');
        let control = '';

        if (!field.editable) {
            control = `<div class="cap-settings-readonly"><i class="bi bi-braces"></i><span>结构化配置将在专用编辑器中管理，当前已保护原值。</span></div>`;
        } else if (field.type === 'boolean') {
            control = `<div class="form-check form-switch modern-toggle mb-0"><input class="form-check-input" type="checkbox" id="${id}" ${common} ${value ? 'checked' : ''}></div>`;
        } else if ((field.options || []).length) {
            const options = field.options.map(option => `<option value="${this.escapeHtml(String(option.value))}" ${String(value) === String(option.value) ? 'selected' : ''}>${this.escapeHtml(String(option.label))}</option>`).join('');
            control = `<select class="form-select" id="${id}" ${common}>${options}</select>`;
        } else if (field.type === 'array') {
            const text = Array.isArray(value) ? value.join('\n') : String(value || '');
            control = `<textarea class="form-control" id="${id}" rows="4" ${common}>${this.escapeHtml(text)}</textarea><small>每行一项</small>`;
        } else if (field.type === 'integer' || field.type === 'number') {
            control = `<input class="form-control" type="number" id="${id}" value="${this.escapeHtml(String(value))}" ${common}${minimum}${maximum}${step}>`;
        } else if (field.control === 'textarea') {
            control = `<textarea class="form-control font-monospace cap-settings-prompt-editor" id="${id}" rows="12" ${common}>${this.escapeHtml(String(value))}</textarea>`;
        } else {
            const inputType = field.sensitive ? 'password' : 'text';
            const placeholder = field.sensitive && field.configured ? '已配置；留空保持不变' : (field.placeholder || '');
            control = `<input class="form-control" type="${inputType}" id="${id}" value="${field.sensitive ? '' : this.escapeHtml(String(value))}" placeholder="${this.escapeHtml(placeholder)}" ${common}>`;
        }

        return `<div class="cap-settings-field" data-settings-level-value="${this.escapeHtml(field.level || 'basic')}" data-settings-search-value="${this.escapeHtml(`${field.title} ${field.description} ${field.key}`.toLowerCase())}">
            <div class="cap-settings-label"><label for="${id}">${this.escapeHtml(field.title)}</label><p>${this.escapeHtml(field.description || '')}</p>${field.level === 'developer' ? `<code>${this.escapeHtml(field.key)}</code>` : ''}</div>
            <div class="cap-settings-control">${control}</div>
        </div>`;
    },

    bindCapabilitySettingsControls(modalElement) {
        const shell = modalElement.querySelector('.cap-settings-shell');
        if (!shell) return;
        const search = shell.querySelector('#capabilitySettingsSearch');
        const filterButtons = shell.querySelectorAll('[data-settings-level]');

        const apply = () => {
            const query = (search?.value || '').trim().toLowerCase();
            const level = shell.dataset.levelFilter || 'all';
            let visibleFields = 0;
            shell.querySelectorAll('.cap-settings-field').forEach(field => {
                const levelMatch = level === 'all' || field.dataset.settingsLevelValue === 'basic';
                const searchMatch = !query || (field.dataset.settingsSearchValue || '').includes(query);
                const visible = levelMatch && searchMatch;
                field.classList.toggle('d-none', !visible);
                if (visible) visibleFields += 1;
            });
            shell.querySelectorAll('.cap-settings-section').forEach(section => {
                section.classList.toggle('d-none', !section.querySelector('.cap-settings-field:not(.d-none)'));
            });
            shell.querySelectorAll('[data-settings-anchor]').forEach(button => {
                const section = shell.querySelector(`#${button.dataset.settingsAnchor}`);
                const count = section?.querySelectorAll('.cap-settings-field:not(.d-none)').length || 0;
                button.classList.toggle('d-none', count === 0);
                const badge = button.querySelector('[data-settings-visible-count]');
                if (badge) badge.textContent = String(count);
            });
            shell.querySelector('#capabilitySettingsEmpty')?.classList.toggle('d-none', visibleFields > 0);
        };

        const syncDependencies = () => {
            if (shell.dataset.capabilityId !== 'assistant') return;
            const memoryToggle = shell.querySelector('[data-config-key="memory_enabled"]');
            if (!memoryToggle) return;
            const enabled = memoryToggle.checked;
            const summary = shell.querySelector('[data-global-memory-summary]');
            const help = shell.querySelector('[data-global-memory-help]');
            const primaryToggle = shell.querySelector('[data-global-memory-toggle]');
            const primaryToggleLabel = shell.querySelector('[data-global-memory-toggle-label]');
            if (primaryToggle) primaryToggle.checked = enabled;
            if (primaryToggleLabel) primaryToggleLabel.textContent = enabled ? '已开启' : '已关闭';
            if (summary) summary.textContent = `长期记忆全局默认：${enabled ? '开启' : '关闭'}`;
            if (help) help.textContent = `保存后，所有“继承全局”的聊天都会显示并使用“${enabled ? '开启' : '关闭'}”状态。`;
            shell.querySelectorAll('[data-config-key^="memory_"]').forEach(control => {
                if (control === memoryToggle) return;
                control.disabled = !enabled;
                control.closest('.cap-settings-field')?.classList.toggle('is-dependency-disabled', !enabled);
            });
        };

        search?.addEventListener('input', this.debounce(apply, 100));
        filterButtons.forEach(button => button.addEventListener('click', () => {
            shell.dataset.levelFilter = button.dataset.settingsLevel;
            filterButtons.forEach(item => item.classList.toggle('active', item === button));
            apply();
        }));
        shell.querySelectorAll('[data-settings-anchor]').forEach(button => button.addEventListener('click', () => {
            const section = shell.querySelector(`#${button.dataset.settingsAnchor}`);
            section?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }));
        shell.querySelectorAll('[data-settings-jump]').forEach(button => button.addEventListener('click', () => {
            const targetId = `cap-settings-${button.dataset.settingsJump}`;
            shell.querySelector(`#${targetId}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }));
        shell.querySelector('[data-config-key="memory_enabled"]')?.addEventListener('change', syncDependencies);
        shell.querySelector('[data-global-memory-toggle]')?.addEventListener('change', event => {
            const memoryToggle = shell.querySelector('[data-config-key="memory_enabled"]');
            if (!memoryToggle) return;
            memoryToggle.checked = event.currentTarget.checked;
            memoryToggle.dispatchEvent(new Event('change', { bubbles: true }));
        });
        syncDependencies();
        apply();
    },

    focusCapabilitySettingsGroup(modalElement, groupId) {
        const shell = modalElement?.querySelector('.cap-settings-shell');
        if (!shell || !groupId) return;
        const targetId = `cap-settings-${groupId}`;
        const target = shell.querySelector(`#${targetId}`);
        if (!target) return;
        if (!target.querySelector('.cap-settings-field:not(.d-none)')) {
            shell.querySelector('[data-settings-level="all"]')?.click();
        }
        shell.querySelectorAll('[data-settings-anchor]').forEach(button => {
            button.classList.toggle('active', button.dataset.settingsAnchor === targetId);
        });
        target.classList.add('is-focused');
        window.setTimeout(() => {
            target.scrollIntoView({ behavior: 'smooth', block: 'start' });
            window.setTimeout(() => target.classList.remove('is-focused'), 1400);
        }, 180);
    },

    showAddSettingModal() {
        const form = document.getElementById('addSettingForm');
        if (form) form.reset();

        const modalEl = document.getElementById('addSettingModal');
        if (modalEl) {
            let mInst = bootstrap.Modal.getInstance(modalEl);
            if (!mInst) {
                mInst = new bootstrap.Modal(modalEl);
            }
            mInst.show();
        }
    },

    async submitNewSetting() {
        const key = document.getElementById('newSettingKey').value.trim();
        const value = document.getElementById('newSettingValue').value.trim();
        if (!key || !value) {
            UI.showError('键名和值不能为空');
            return;
        }

        const data = {
            key: key,
            value: value,
            category: document.getElementById('newSettingCategory').value.trim() || 'default',
            description: document.getElementById('newSettingDescription').value.trim()
        };

        const btn = document.querySelector('#addSettingModal .btn-primary');
        const originalText = btn.innerHTML;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>正在保存…';
        btn.disabled = true;

        try {
            await API.settings.create(data);

            // Close modal safely
            const modalEl = document.getElementById('addSettingModal');
            let mInst = bootstrap.Modal.getInstance(modalEl);
            if (mInst) {
                mInst.hide();
            }

            // Refresh settings view
            if (window.App && App.loadSettings) {
                await App.loadSettings();
            }

        } catch (error) {
            UI.showError('添加设置失败：' + error.message);
        } finally {
            if (btn) {
                btn.innerHTML = originalText;
                btn.disabled = false;
            }
        }
    },

    async deleteSetting(key) {
        if (!await UI.confirm(`确定要永久删除设置键 ${key} 吗？\n此操作无法撤销。`, {
            title: '删除设置',
            confirmText: '删除',
            variant: 'danger'
        })) {
            return;
        }

        try {
            const resp = await API.settings.delete(key);
            if (resp && resp.success) {
                // Remove it from the DOM immediately or just reload
                if (window.App && App.loadSettings) {
                    await App.loadSettings();
                }
            }
        } catch (error) {
            UI.showError(`删除设置 ${key} 失败：` + error.message);
        }
    }
};

window.UI = UI;
