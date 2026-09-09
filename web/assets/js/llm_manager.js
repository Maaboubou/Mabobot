/**
 * LLM Manager - Frontend Logic
 * Handles model connections, task routes, prompts, and statistics
 */

const LLMManager = {
    currentModels: {},
    currentMappings: {},
    currentRouteOwners: {},
    currentStats: {},
    catalogProviders: [],
    catalogModels: [],
    catalogDiscovery: {},
    catalogVersion: '',
    catalogRequestId: 0,
    litellmUpdateStatus: {},
    litellmUpdatePollTimer: null,
    litellmUpdateNotified: '',
    litellmUpdateChecking: false,
    credentialStatusRequestId: 0,
    sharedCredentialStatus: { name: '', configured: false, source: 'unknown' },
    modelSortable: null,
    modelIdAutofill: true,
    providerPresets: {
        openai: {
            label: 'OpenAI', catalogProvider: 'openai', providerValue: 'openai',
            envVar: 'OPENAI_API_KEY', requiresCredential: true,
            modelPlaceholder: '例如 gpt-5.5', apiBase: '', apiBaseRequired: false,
        },
        anthropic: {
            label: 'Anthropic', catalogProvider: 'anthropic', providerValue: 'anthropic',
            envVar: 'ANTHROPIC_API_KEY', requiresCredential: true,
            modelPlaceholder: '例如 anthropic/claude-sonnet-4-6', apiBase: '', apiBaseRequired: false,
        },
        gemini: {
            label: 'Google Gemini', catalogProvider: 'gemini', providerValue: 'gemini',
            envVar: 'GEMINI_API_KEY', requiresCredential: true,
            modelPlaceholder: '例如 gemini/gemini-3.5-flash', apiBase: '', apiBaseRequired: false,
        },
        deepseek: {
            label: 'DeepSeek', catalogProvider: 'deepseek', providerValue: 'deepseek',
            envVar: 'DEEPSEEK_API_KEY', requiresCredential: true,
            modelPlaceholder: '例如 deepseek/deepseek-chat', apiBase: '', apiBaseRequired: false,
        },
        openrouter: {
            label: 'OpenRouter', catalogProvider: 'openrouter', providerValue: 'openrouter',
            envVar: 'OPENROUTER_API_KEY', requiresCredential: true,
            modelPlaceholder: '例如 openrouter/anthropic/claude-sonnet-4',
            apiBase: 'https://openrouter.ai/api/v1', apiBaseRequired: false,
        },
        local_codex: {
            label: '本地 Codex', catalogProvider: 'local_codex', providerValue: 'local_codex',
            envVar: '', requiresCredential: false,
            modelPlaceholder: '例如 gpt-5.6-sol', apiBase: '', apiBaseRequired: false,
        },
        compatible: {
            label: '自定义 OpenAI 兼容接口', catalogProvider: '', providerValue: 'custom_openai',
            envVar: 'CUSTOM_LLM_API_KEY', requiresCredential: false,
            modelPlaceholder: '输入接口暴露的模型名称', apiBase: '', apiBaseRequired: true,
        },
        other: {
            label: '其他 LiteLLM 供应商', catalogProvider: '', providerValue: '',
            envVar: 'LLM_API_KEY', requiresCredential: true,
            modelPlaceholder: '先选择 LiteLLM 供应商', apiBase: '', apiBaseRequired: false,
        },
    },
    activeStatsType: 'today',
    usageView: 'task',
    usageSubject: null,
    usageSearch: '',
    usagePage: 1,
    usageExpanded: null,
    usageSort: { field: 'tokens', direction: 'desc' },


    escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    },

    normalizePastedText(value) {
        let text = String(value ?? '');
        if (typeof text.normalize === 'function') text = text.normalize('NFKC');
        return text.replace(/[\u200B-\u200D\u2060\uFEFF]/g, '');
    },

    sanitizeModelId(value) {
        return this.normalizePastedText(value)
            .trim()
            .replace(/\s+/g, '-')
            .replace(/[\\/?#\x00-\x1f\x7f]+/g, '-')
            .replace(/-+/g, '-')
            .replace(/^[.-]+|[.-]+$/g, '')
            .slice(0, 80);
    },

    sanitizeCredentialName(value) {
        let name = this.normalizePastedText(value)
            .trim()
            .replace(/[^A-Za-z0-9_]+/g, '_')
            .replace(/_+/g, '_')
            .replace(/^_+|_+$/g, '')
            .slice(0, 120);
        if (/^[0-9]/.test(name)) name = `_${name}`;
        return name;
    },

    buildModelCredentialName(modelId) {
        const normalized = this.sanitizeModelId(modelId) || 'model';
        let hash = 2166136261;
        for (let index = 0; index < normalized.length; index += 1) {
            hash = Math.imul(hash ^ normalized.charCodeAt(index), 16777619) >>> 0;
        }
        const stem = normalized
            .toUpperCase()
            .replace(/[^A-Z0-9]+/g, '_')
            .replace(/^_+|_+$/g, '')
            .slice(0, 50) || 'MODEL';
        return `MABOBOT_MODEL_${stem}_${hash.toString(16).padStart(8, '0').toUpperCase()}_API_KEY`;
    },

    mergeDeclaredTaskMappings(rawMappings, routeOwners) {
        const merged = {};
        Object.entries(rawMappings || {}).forEach(([ownerId, mappings]) => {
            if (!mappings || typeof mappings !== 'object' || Array.isArray(mappings)) return;
            merged[ownerId] = {};
            Object.entries(mappings).forEach(([callType, mapping]) => {
                merged[ownerId][callType] = mapping && typeof mapping === 'object'
                    ? {...mapping}
                    : { primary: '', fallback: [], override_params: {} };
            });
        });
        Object.entries(routeOwners || {}).forEach(([ownerId, owner]) => {
            const tasks = owner?.tasks;
            if (!tasks || typeof tasks !== 'object' || Array.isArray(tasks) || !Object.keys(tasks).length) return;
            const ownerMappings = merged[ownerId] ||= {};
            Object.keys(tasks).forEach(callType => {
                if (!ownerMappings[callType]) {
                    ownerMappings[callType] = { primary: '', fallback: [], override_params: {} };
                }
            });
        });
        return merged;
    },

    summarizeBase64ForDisplay(value) {
        const text = String(value ?? '');
        const dataUrlMatch = text.match(/^(data:[^;,]+(?:;[^,]*)?;base64,)([\s\S]+)$/i);
        const prefix = dataUrlMatch ? dataUrlMatch[1] : '';
        const payload = dataUrlMatch ? dataUrlMatch[2] : text;
        const head = payload.slice(0, 80);
        const tail = payload.length > 112 ? payload.slice(-32) : '';
        const omitted = Math.max(payload.length - head.length - tail.length, 0);
        const suffix = tail ? `...${tail}` : '';
        return `${prefix}${head}${suffix} [base64 omitted: ${omitted} chars, total: ${payload.length} chars]`;
    },

    isLikelyBase64String(value, key = '') {
        if (typeof value !== 'string') return false;
        if (/^data:[^;,]+(?:;[^,]*)?;base64,/i.test(value)) return true;
        if (value.length < 512) return false;

        const normalized = value.replace(/\s+/g, '');
        if (normalized.length < 512 || normalized.length % 4 === 1) return false;

        const keyHint = /base64|image|audio|video|file|bytes|payload|data/i.test(String(key));
        if (!keyHint) return false;

        return /^[A-Za-z0-9+/]+={0,2}$/.test(normalized);
    },

    sanitizeCallHistoryPayload(value, key = '') {
        if (this.isLikelyBase64String(value, key)) {
            return this.summarizeBase64ForDisplay(value);
        }

        if (Array.isArray(value)) {
            return value.map(item => this.sanitizeCallHistoryPayload(item, key));
        }

        if (value && typeof value === 'object') {
            const sanitized = {};
            Object.entries(value).forEach(([childKey, childValue]) => {
                sanitized[childKey] = this.sanitizeCallHistoryPayload(childValue, childKey);
            });
            return sanitized;
        }

        return value;
    },

    renderTokenUsage(entry) {
        const formatK = (num) => {
            if (num === null || num === undefined) return '-';
            const val = Number(num);
            if (val >= 1000000) return (val / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
            if (val >= 1000) return (val / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
            return val.toLocaleString();
        };

        const usage = entry.token_usage || {};
        const prompt = usage.prompt_tokens || entry.prompt_tokens || 0;
        const completion = usage.completion_tokens || entry.completion_tokens || 0;
        const total = usage.total_tokens || entry.tokens || 0;
        const cached = usage.cached_tokens || 0;
        const cacheMiss = usage.cache_miss_tokens || 0;
        const cacheRate = usage.cache_hit_rate;
        const reasoning = usage.reasoning_tokens || 0;
        const estimated = usage.estimated || entry.estimated;

        let html = '';
        const badgeClass = "badge bg-light text-secondary border fw-normal";
        const valClass = "text-dark font-monospace ms-1";

        if (estimated) {
            html += `<span class="badge bg-warning-subtle text-warning-emphasis border border-warning-subtle fw-normal">估算</span> `;
        }

        if (prompt || completion) {
            html += `<span class="${badgeClass}">输入<span class="${valClass}">${formatK(prompt)}</span></span>`;
            html += `<span class="${badgeClass}">输出<span class="${valClass}">${formatK(completion)}</span></span>`;
            if (total) html += `<span class="${badgeClass}">总计<span class="${valClass}">${formatK(total)}</span></span>`;
            if (reasoning) html += `<span class="${badgeClass}">推理<span class="${valClass}">${formatK(reasoning)}</span></span>`;
            if (cached) {
                let cacheStr = `<span class="${badgeClass}">缓存<span class="text-success font-monospace ms-1">${formatK(cached)}</span>`;
                if (cacheRate !== undefined) {
                    cacheStr += `<span class="ms-1 opacity-75">(${(Number(cacheRate) * 100).toFixed(1)}%)</span>`;
                }
                cacheStr += `</span>`;
                html += cacheStr;
            }
            if (cacheMiss) html += `<span class="${badgeClass}">未命中<span class="${valClass}">${formatK(cacheMiss)}</span></span>`;
            return html;
        }

        if (total) return `${html}<span class="${badgeClass}">总计<span class="${valClass}">${formatK(total)}</span></span>`;
        return html;
    },

    getModelDomId(modelId) {
        return btoa(unescape(encodeURIComponent(String(modelId))))
            .replace(/[+/=]/g, '_');
    },

    formatTemperature(value, fallback = 'N/A') {
        if (value === undefined || value === null || value === '') return fallback;
        const num = Number(value);
        if (!Number.isFinite(num)) return String(value);
        return Number.isInteger(num) ? num.toFixed(1) : String(value);
    },

    getTaskRouteMeta(ownerId, callType) {
        const owner = this.currentRouteOwners[ownerId] || {};
        const declared = owner.tasks?.[callType];
        if (declared && typeof declared === 'object') {
            const declaredOrder = Number(declared.order);
            return {
                label: declared.label || callType,
                description: declared.description || '尚未补充这项模型任务的用途说明。',
                category: declared.category || '模型任务',
                order: Number.isFinite(declaredOrder) ? declaredOrder : 500,
                advanced: Boolean(declared.advanced),
                declared: true,
            };
        }
        const readable = String(callType || '')
            .replace(/[_-]+/g, ' ')
            .trim()
            .replace(/\b\w/g, value => value.toUpperCase());
        return {
            label: readable ? `自定义任务 · ${readable}` : '未命名任务',
            description: '尚未声明这项模型任务的用途。',
            category: '自定义',
            order: 900,
            advanced: false,
            declared: false,
        };
    },

    getRouteOwnerMeta(ownerId) {
        const owner = this.currentRouteOwners[ownerId] || {};
        const readable = String(ownerId || '')
            .replace(/[_-]+/g, ' ')
            .trim()
            .replace(/\b\w/g, value => value.toUpperCase());
        const icon = /^bi-[a-z0-9-]+$/.test(String(owner.icon || ''))
            ? owner.icon
            : 'bi-diagram-3';
        const ownerKind = ['core', 'plugin', 'unknown'].includes(owner.owner_kind)
            ? owner.owner_kind
            : 'unknown';
        return {
            displayName: owner.display_name || readable || '未命名路由主体',
            description: owner.description || '该路由主体尚未声明说明。',
            icon,
            ownerKind,
            kindLabel: {core: '核心组件', plugin: '插件', unknown: '历史路由'}[ownerKind],
        };
    },

    getRouteModelInfo(modelId) {
        const id = String(modelId || '').trim();
        const config = this.currentModels[id] || {};
        const configuredModel = String(config.model || '').trim();
        return {
            id,
            detail: configuredModel && configuredModel !== id ? configuredModel : '',
        };
    },

    formatRouteModelOption(modelId) {
        const info = this.getRouteModelInfo(modelId);
        return info.detail ? `${info.id} · ${info.detail}` : info.id;
    },

    renderRouteModelCell(modelId, emptyLabel = '未配置') {
        const info = this.getRouteModelInfo(modelId);
        if (!info.id) return `<span class="llm-route-model-empty">${this.escapeHtml(emptyLabel)}</span>`;
        return `<div class="llm-route-model-cell"><strong title="${this.escapeHtml(info.id)}">${this.escapeHtml(info.id)}</strong>${info.detail ? `<span title="${this.escapeHtml(info.detail)}">${this.escapeHtml(info.detail)}</span>` : ''}</div>`;
    },

    /**
     * Initialize LLM Manager when tab is loaded
     */
    async init() {
        this.setupSubTabHandlers();
        this.setupModelEditor();
        this.setupMappingEditor();
        this.loadModelCatalogProviders();

        // Add event listener to clean up modal when closed
        const modalEl = document.getElementById('addModelModal');
        if (modalEl && !modalEl.dataset.cleanupBound) {
            modalEl.dataset.cleanupBound = 'true';
            modalEl.addEventListener('hidden.bs.modal', () => {
                // Reset form to prevent data leakage between edits
                document.getElementById('addModelForm').reset();
                document.getElementById('modelExtraBody').value = '';
                document.getElementById('modelEditMode').value = 'false';
                this.closeModelCatalog();
                this.clearLiteLLMUpdatePoll();
            });
        }

        await this.activateSubTab(this.getSubTabFromPath(), { history: false });
    },

    async initUsage() {
        this.setupSubTabHandlers();
        const path = UI.normalizePath(window.location.pathname);
        const target = ['/usage/calls', '/ai/calls'].includes(path) ? 'llm-history' : 'llm-stats';
        await this.activateSubTab(target, { history: false });
    },

    setupSubTabHandlers() {
        if (this.subTabHandlersReady) return;
        this.subTabHandlersReady = true;

        document.querySelectorAll('#llm > ul.nav [data-bs-target^="#llm-"], #usage > ul.nav [data-bs-target^="#llm-"]').forEach(button => {
            button.addEventListener('click', (event) => {
                event.preventDefault();
                event.stopPropagation();
                const target = button.getAttribute('data-bs-target') || '';
                this.activateSubTab(target.replace(/^#/, ''));
            });
        });
    },

    getSubTabFromPath() {
        const path = UI.normalizePath(window.location.pathname);
        return {
            '/ai/mappings': 'llm-mappings',
            '/ai/usage': 'llm-stats',
            '/ai/calls': 'llm-history',
            '/ai/network': 'llm-proxy'
        }[path] || 'llm-models';
    },

    async activateSubTab(targetId, options = {}) {
        const panes = ['llm-history', 'llm-models', 'llm-mappings', 'llm-stats', 'llm-proxy'];
        if (!panes.includes(targetId)) targetId = 'llm-models';
        panes.forEach(id => {
            const pane = document.getElementById(id);
            const button = document.querySelector(`#llm > ul.nav [data-bs-target="#${id}"], #usage > ul.nav [data-bs-target="#${id}"]`);

            const active = id === targetId;
            if (pane) {
                pane.classList.toggle('show', active);
                pane.classList.toggle('active', active);
                pane.classList.toggle('d-none', !active);
            }
            if (button) {
                button.classList.toggle('active', active);
                button.setAttribute('aria-selected', active ? 'true' : 'false');
            }
        });

        if (options.history !== false) {
            const paths = {
                'llm-models': '/ai/models',
                'llm-mappings': '/ai/mappings',
                'llm-stats': '/usage',
                'llm-history': '/usage/calls',
                'llm-proxy': '/ai/network'
            };
            const path = paths[targetId];
            if (UI.normalizePath(window.location.pathname) !== path) {
                window.history.pushState({ tab: ['llm-stats', 'llm-history'].includes(targetId) ? 'usage' : 'llm', section: targetId }, '', path);
            }
        }

        if (options.load === false) return;
        await this.loadSubTab(targetId);
    },

    async loadSubTab(targetId) {
        if (targetId === 'llm-models') return this.loadModels();
        if (targetId === 'llm-mappings') {
            if (!Object.keys(this.currentModels || {}).length) {
                await this.loadModels();
            }
            return this.loadMappings();
        }
        if (targetId === 'llm-stats') return this.loadStats();
        if (targetId === 'llm-proxy') return this.loadProxy();
        return this.loadCallHistory();
    },

    setupModelEditor() {
        const form = document.getElementById('addModelForm');
        if (!form || form.dataset.editorBound) return;
        form.dataset.editorBound = 'true';

        const modelName = document.getElementById('modelName');
        const modelId = document.getElementById('modelId');
        const template = document.getElementById('modelTemplateSelect');
        modelName?.addEventListener('focus', () => this.openModelCatalog());
        modelName?.addEventListener('input', () => {
            this.renderModelCatalogMenu(modelName.value);
            if (this.modelIdAutofill) this.suggestModelId(modelName.value);
            this.updateModelFormReview();
        });
        modelName?.addEventListener('keydown', event => {
            if (event.key === 'Escape') this.closeModelCatalog();
        });
        modelId?.addEventListener('input', event => {
            if (event.isTrusted) this.modelIdAutofill = false;
            this.updateModelFormReview();
        });
        template?.addEventListener('change', event => this.cloneModelConfig(event.target.value));
        form.addEventListener('input', event => {
            if (event.target !== modelName && event.target !== modelId) this.updateModelFormReview();
        });
        form.addEventListener('change', () => this.updateModelFormReview());
        document.addEventListener('click', event => {
            if (!event.target.closest('.model-catalog-picker')) this.closeModelCatalog();
        });
    },

    setupMappingEditor() {
        const primary = document.getElementById('mappingPrimary');
        const fallback = document.getElementById('mappingFallback');
        if (!primary || primary.dataset.samplingBound) return;
        primary.dataset.samplingBound = 'true';
        primary.addEventListener('change', () => this.updateMappingSamplingControls());
        fallback?.addEventListener('change', () => this.updateMappingSamplingControls());
    },

    async loadModelCatalogProviders() {
        try {
            const response = await fetch('/api/llm/models/catalog');
            const result = await response.json();
            if (!response.ok || result.status !== 'success') {
                throw new Error(result.detail || result.message || '目录加载失败');
            }
            this.catalogProviders = result.data?.providers || [];
            this.catalogVersion = result.data?.version || '';
            const select = document.getElementById('modelOtherProvider');
            if (select) {
                const selected = select.value;
                select.innerHTML = '<option value="">选择供应商…</option>' + this.catalogProviders
                    .map(provider => `<option value="${this.escapeHtml(provider.id)}">${this.escapeHtml(provider.label)} · ${Number(provider.model_count || 0).toLocaleString()} 个模型</option>`)
                    .join('');
                if (selected && this.catalogProviders.some(provider => provider.id === selected)) {
                    select.value = selected;
                }
            }
            this.updateModelFormReview();
        } catch (error) {
            console.warn('LiteLLM catalog providers unavailable:', error);
            this.catalogProviders = [];
        }
    },

    clearLiteLLMUpdatePoll() {
        if (this.litellmUpdatePollTimer) {
            clearTimeout(this.litellmUpdatePollTimer);
            this.litellmUpdatePollTimer = null;
        }
    },

    scheduleLiteLLMUpdatePoll() {
        this.clearLiteLLMUpdatePoll();
        const updateSurfaceVisible = ['addModelModal', 'codexProfileModal']
            .some(id => document.getElementById(id)?.classList.contains('show'));
        if (!updateSurfaceVisible) return;
        this.litellmUpdatePollTimer = setTimeout(() => {
            this.loadLiteLLMUpdateStatus({ quiet: true, poll: true });
        }, 1200);
    },

    renderLiteLLMUpdateStatus(status = {}) {
        this.litellmUpdateStatus = status || {};
        const operation = status.operation || {};
        const running = Boolean(status.operation_running || ['queued', 'running'].includes(operation.status));
        const failed = operation.status === 'failed';
        const restartRequired = Boolean(status.restart_required);
        const updateAvailable = Boolean(status.update_available);
        const runtimeVersion = status.runtime_version || this.catalogVersion || '未知';
        const installedVersion = status.installed_version || runtimeVersion;
        const availableVersion = status.available_version || '';
        const environment = status.environment_label || '当前 Python 环境';
        document.querySelectorAll('[data-litellm-update-surface]').forEach(surface => {
            const badge = surface.querySelector('[data-litellm-update-badge]');
            const summary = surface.querySelector('[data-litellm-update-summary]');
            const checkButton = surface.querySelector('[data-litellm-check]');
            const updateButton = surface.querySelector('[data-litellm-update]');
            const restartButton = surface.querySelector('[data-litellm-restart]');
            const progress = surface.querySelector('[data-litellm-progress]');
            const progressBar = surface.querySelector('[data-litellm-progress-bar]');
            const message = surface.querySelector('[data-litellm-message]');
            if (!badge || !summary || !checkButton || !updateButton || !restartButton || !progress) return;

            badge.className = 'litellm-update-badge';
            if (running) {
                badge.textContent = '更新中';
                badge.classList.add('running');
            } else if (failed) {
                badge.textContent = '更新失败';
                badge.classList.add('failed');
            } else if (restartRequired) {
                badge.textContent = '等待重启';
                badge.classList.add('available');
            } else if (updateAvailable) {
                badge.textContent = `可更新 ${availableVersion}`;
                badge.classList.add('available');
            } else if (availableVersion) {
                badge.textContent = '已是最新';
                badge.classList.add('ready');
            } else {
                badge.textContent = '尚未检查';
            }

            if (restartRequired && runtimeVersion !== installedVersion) {
                summary.textContent = `运行中 ${runtimeVersion} · 已安装 ${installedVersion}；重启全部服务后切换新版。`;
            } else {
                summary.textContent = `当前 ${runtimeVersion} · ${environment}${availableVersion ? ` · PyPI ${availableVersion}` : ''}`;
            }

            checkButton.disabled = running || this.litellmUpdateChecking;
            checkButton.innerHTML = this.litellmUpdateChecking
                ? '<span class="spinner-border spinner-border-sm me-1"></span>检查中'
                : '<i class="bi bi-arrow-clockwise me-1"></i>检查更新';
            updateButton.classList.toggle('d-none', !updateAvailable || running || restartRequired);
            updateButton.disabled = running;
            if (updateAvailable && availableVersion) {
                updateButton.innerHTML = `<i class="bi bi-download me-1"></i>更新到 ${this.escapeHtml(availableVersion)}`;
            }
            restartButton.classList.toggle('d-none', !restartRequired || running);
            progress.classList.toggle('d-none', !running);
            if (running) {
                const percent = Math.max(0, Math.min(Number(operation.progress || 0), 100));
                if (progressBar) progressBar.style.width = `${percent}%`;
                if (message) message.textContent = operation.message || '正在更新 LiteLLM…';
            }
        });
    },

    async loadLiteLLMUpdateStatus(options = {}) {
        try {
            const response = await API.litellm.getStatus();
            const status = response.data || {};
            this.renderLiteLLMUpdateStatus(status);
            if (status.operation_running) {
                this.scheduleLiteLLMUpdatePoll();
            } else {
                this.clearLiteLLMUpdatePoll();
                const operation = status.operation || {};
                const notificationKey = operation.operation_id && operation.status
                    ? `${operation.operation_id}:${operation.status}`
                    : '';
                if (options.poll && notificationKey && notificationKey !== this.litellmUpdateNotified) {
                    this.litellmUpdateNotified = notificationKey;
                    if (operation.status === 'succeeded') {
                        UI.showSuccess(status.restart_required
                            ? 'LiteLLM 更新完成；请重启全部服务后再使用新版模型目录。'
                            : (operation.message || '当前已是最新版本'));
                    } else if (operation.status === 'failed') {
                        UI.showError(`LiteLLM 更新失败：${operation.message || '请查看服务日志'}`);
                    }
                }
            }
            return status;
        } catch (error) {
            this.clearLiteLLMUpdatePoll();
            if (!options.quiet) UI.showError(`读取 LiteLLM 状态失败：${error.message}`);
            return null;
        }
    },

    async checkLiteLLMUpdate() {
        if (this.litellmUpdateChecking) return;
        this.litellmUpdateChecking = true;
        this.renderLiteLLMUpdateStatus(this.litellmUpdateStatus);
        try {
            const response = await API.litellm.checkUpdate();
            const checked = response.data || {};
            const status = await this.loadLiteLLMUpdateStatus({ quiet: true }) || {
                ...this.litellmUpdateStatus,
                ...checked,
            };
            this.renderLiteLLMUpdateStatus(status);
            if (checked.update_available) {
                UI.showInfo(`发现 LiteLLM ${checked.available_version}，确认后可在当前环境中更新。`);
            } else {
                UI.showSuccess('当前 LiteLLM 已是 PyPI 最新版本。');
            }
        } catch (error) {
            UI.showError(`检查 LiteLLM 更新失败：${error.message}`);
        } finally {
            this.litellmUpdateChecking = false;
            this.renderLiteLLMUpdateStatus(this.litellmUpdateStatus);
        }
    },

    async startLiteLLMUpdate() {
        const status = this.litellmUpdateStatus || {};
        const target = status.available_version || '最新版';
        const current = status.installed_version || status.runtime_version || '当前版本';
        const environment = status.environment_label || '当前 Python 环境';
        const confirmed = await UI.confirm(
            `确定将 LiteLLM 从 ${current} 更新到 ${target} 吗？\n\n更新只会作用于${environment}中的固定包 litellm。当前进程会继续使用旧版，完成后必须重启全部服务才会切换新版。`,
            {
                title: '更新本机 LiteLLM',
                confirmText: '开始更新',
                variant: 'primary',
            }
        );
        if (!confirmed) return;
        try {
            await API.litellm.startUpdate();
            UI.showInfo('LiteLLM 更新任务已开始，可在此处查看进度。');
            await this.loadLiteLLMUpdateStatus({ quiet: true });
        } catch (error) {
            UI.showError(`启动 LiteLLM 更新失败：${error.message}`);
        }
    },

    getModelManagementMeta(config = {}) {
        if (config._management && typeof config._management === 'object') return config._management;
        const provider = this.inferProviderKey(config);
        const hasCredential = Boolean(config.api_key);
        return {
            provider,
            credential: {
                mode: provider === 'local_codex' ? 'none' : (hasCredential ? 'direct' : 'missing'),
                configured: hasCredential,
                environment_variable: '',
            },
            share_ready: provider === 'local_codex',
            mapping_count: 0,
            mapped_by: [],
        };
    },

    inferProviderKey(config = {}) {
        const explicit = String(config.custom_llm_provider || config.provider || '').toLowerCase();
        const model = String(config.model || '').toLowerCase();
        const apiBase = String(config.api_base || '').toLowerCase();
        if (['local_codex', 'local_codex_cli'].includes(explicit) || apiBase.includes('/api/codex/')) return 'local_codex';
        if (explicit === 'custom_openai') return 'compatible';
        if (explicit) return explicit;
        if (model.startsWith('openrouter/') || apiBase.includes('openrouter.ai')) return 'openrouter';
        for (const provider of ['anthropic', 'gemini', 'deepseek', 'azure', 'bedrock', 'vertex_ai', 'mistral', 'groq', 'xai']) {
            if (model.startsWith(`${provider}/`)) return provider;
        }
        return apiBase ? 'compatible' : 'openai';
    },

    isGemini3ModelConfig(config = {}) {
        const disabled = this.getModelManagementMeta(config).disabled_parameters || [];
        if (disabled.includes('temperature')) return true;
        const provider = this.inferProviderKey(config);
        const model = String(config.model || '').toLowerCase();
        return ['gemini', 'vertex_ai'].includes(provider) && model.includes('gemini-3');
    },

    isGemini3FormSelection(provider, modelName) {
        const providerId = String(
            provider?.key === 'other'
                ? provider.catalogProvider
                : (provider?.providerValue || provider?.key || '')
        ).toLowerCase();
        return ['gemini', 'vertex_ai'].includes(providerId)
            && String(modelName || '').toLowerCase().includes('gemini-3');
    },

    stripGeminiSamplingParameters(value) {
        if (!value || typeof value !== 'object') return value;
        const cleaned = JSON.parse(JSON.stringify(value));
        const stripKnownContainers = object => {
            if (!object || typeof object !== 'object' || Array.isArray(object)) return;
            for (const key of ['temperature', 'top_p', 'top_k', 'topP', 'topK']) delete object[key];
            for (const key of ['extra_body', 'generation_config', 'generationConfig']) {
                stripKnownContainers(object[key]);
                if (object[key] && typeof object[key] === 'object' && !Object.keys(object[key]).length) {
                    delete object[key];
                }
            }
        };
        stripKnownContainers(cleaned);
        return cleaned;
    },

    updateModelSamplingControls(provider = null, modelName = null) {
        provider ||= this.getActiveProviderPreset();
        modelName ??= this.normalizePastedText(document.getElementById('modelName')?.value || '').trim();
        const isCodex = provider.key === 'local_codex';
        const disabled = isCodex || this.isGemini3FormSelection(provider, modelName);
        const group = document.getElementById('modelTemperatureGroup');
        const input = document.getElementById('modelTemp');
        const notice = document.getElementById('modelSamplingNotice');
        group?.classList.toggle('d-none', disabled);
        notice?.classList.toggle('d-none', !disabled);
        const noticeText = notice?.querySelector('.alert');
        if (noticeText) noticeText.textContent = isCodex
            ? '本地 Codex 按本模型配置独立调用，复用本地登录，不继承聊天 Profile 或历史。当前不传递温度、最大输出 Token 和 SDK 重试次数；请使用推理强度和任务指令控制输出。任务可覆盖模型超时。'
            : 'Gemini 3+ 使用模型默认采样设置；temperature、top_p 和 top_k 不会保存或发送。需要约束输出时请写入系统指令。';
        if (input) {
            input.disabled = disabled;
            if (disabled) input.value = '';
        }
        return disabled;
    },

    updateMappingSamplingControls() {
        const primaryId = document.getElementById('mappingPrimary')?.value || '';
        const fallbackId = document.getElementById('mappingFallback')?.value || '';
        const primaryIsGemini3 = this.isGemini3ModelConfig(this.currentModels[primaryId] || {});
        const fallbackIsGemini3 = this.isGemini3ModelConfig(this.currentModels[fallbackId] || {});
        const hasCodex = [primaryId, fallbackId].some(id =>
            this.getPresetKeyForConfig(this.currentModels[id] || {}) === 'local_codex');
        const group = document.getElementById('mappingOverrideTemperatureGroup');
        const input = document.getElementById('mappingOverrideTemp');
        const notice = document.getElementById('mappingSamplingNotice');
        group?.classList.toggle('d-none', primaryIsGemini3);
        if (input) {
            input.disabled = primaryIsGemini3;
            if (primaryIsGemini3) input.value = '';
        }
        if (notice) {
            notice.classList.toggle('d-none', !(primaryIsGemini3 || fallbackIsGemini3 || hasCodex));
            notice.textContent = primaryIsGemini3
                ? 'Gemini 3+ 使用模型默认采样设置；路由中的 temperature、top_p 和 top_k 不会保存或发送。'
                : 'Gemini 3+ 备用模型使用默认采样设置；此处参数只会应用到支持它们的模型。';
            if (hasCodex) {
                const codexNotice = '本地 Codex 不传递温度和最大输出 Token；这些覆盖仅对支持它们的其他模型生效。';
                notice.textContent = primaryIsGemini3 || fallbackIsGemini3
                    ? `${notice.textContent} ${codexNotice}` : codexNotice;
            }
        }
        return primaryIsGemini3;
    },

    getProviderLabel(providerKey) {
        if (this.providerPresets[providerKey]) return this.providerPresets[providerKey].label;
        return this.catalogProviders.find(provider => provider.id === providerKey)?.label
            || String(providerKey || '未知供应商').replace(/_/g, ' ');
    },

    getActiveProviderPreset() {
        const presetKey = document.getElementById('modelProviderPreset')?.value || 'openai';
        if (presetKey !== 'other') return { key: presetKey, ...this.providerPresets[presetKey] };
        const providerKey = document.getElementById('modelOtherProvider')?.value || '';
        const providerInfo = this.catalogProviders.find(provider => provider.id === providerKey);
        return {
            key: 'other',
            label: providerInfo?.label || '其他 LiteLLM 供应商',
            catalogProvider: providerKey,
            providerValue: providerKey,
            envVar: providerInfo?.environment_variable || (providerKey ? `${providerKey.toUpperCase().replace(/[^A-Z0-9]+/g, '_')}_API_KEY` : 'LLM_API_KEY'),
            requiresCredential: true,
            modelPlaceholder: providerKey ? `输入或选择 ${providerInfo?.label || providerKey} 模型` : '先选择 LiteLLM 供应商',
            apiBase: '',
            apiBaseRequired: false,
        };
    },

    async applyProviderPreset(presetKey, options = {}) {
        const preset = this.providerPresets[presetKey];
        if (!preset) return;
        const preserveValues = options.preserveValues === true;
        document.getElementById('modelProviderPreset').value = presetKey;
        document.querySelectorAll('#modelBillingOptions input, #modelBillingOptions select').forEach(input => { input.disabled = presetKey === 'local_codex'; });
        document.querySelectorAll('.model-provider-option').forEach(button => {
            const active = button.dataset.modelProvider === presetKey;
            button.classList.toggle('active', active);
            button.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
        document.getElementById('modelOtherProviderGroup')?.classList.toggle('d-none', presetKey !== 'other');
        document.getElementById('modelProviderOverrideGroup')?.classList.toggle('d-none', !['compatible', 'other'].includes(presetKey));
        const isCodex = presetKey === 'local_codex';
        document.getElementById('modelCodexReasoningGroup')?.classList.toggle('d-none', !isCodex);
        for (const id of ['modelMaxTokens', 'modelMaxRetries']) {
            const input = document.getElementById(id);
            if (input) input.disabled = isCodex;
            input?.closest('.col-sm-6')?.classList.toggle('d-none', isCodex);
        }
        document.getElementById('modelCodexIsolatedGroup')?.classList.toggle('d-none', !isCodex);
        document.getElementById('modelCredentialSection')?.classList.toggle('d-none', isCodex);
        document.getElementById('modelApiBaseRequired')?.classList.toggle('d-none', !preset.apiBaseRequired);
        const apiBaseHelp = document.getElementById('modelApiBaseHelp');
        if (apiBaseHelp) {
            apiBaseHelp.textContent = preset.apiBaseRequired
                ? '填写兼容 OpenAI Chat Completions 的完整基础地址，通常以 /v1 结尾。'
                : '官方供应商通常无需填写；代理、私有部署或网关场景可覆盖。';
        }
        const webSearchLabel = document.getElementById('modelWebSearchLabel');
        if (webSearchLabel) webSearchLabel.textContent = isCodex ? '允许 Codex 使用 Web 搜索' : '启用供应商 Web 搜索';
        const modelName = document.getElementById('modelName');
        if (modelName) modelName.placeholder = preset.modelPlaceholder;

        if (!preserveValues) {
            if (modelName) modelName.value = '';
            document.getElementById('modelApiBase').value = preset.apiBase || '';
            document.getElementById('modelProvider').value = preset.providerValue || '';
            document.getElementById('modelApiKeyEnv').value = preset.envVar || '';
            document.getElementById('modelApiKeyEnvValue').value = '';
            const credentialMode = document.getElementById('modelCredentialMode');
            credentialMode.value = isCodex ? 'none' : 'environment';
            credentialMode.dataset.canPreserve = 'false';
            credentialMode.dataset.originalMode = '';
            document.getElementById('modelMaxTokens').value = '';
            document.getElementById('modelContextWindow').value = '';
            document.getElementById('modelTimeout').value = isCodex ? '600' : '';
            document.getElementById('modelMaxRetries').value = isCodex ? '0' : '';
            document.getElementById('modelVision').checked = false;
            document.getElementById('modelWebSearch').checked = false;
            document.getElementById('modelExtraBody').value = '';
        }
        this.updateCredentialFields();
        if (presetKey === 'other') {
            if (!this.catalogProviders.length) await this.loadModelCatalogProviders();
            const selectedProvider = document.getElementById('modelOtherProvider')?.value;
            if (selectedProvider) await this.selectOtherProvider(selectedProvider, {
                preserveValues,
                skipCatalog: options.skipCatalog === true,
            });
            else this.setCatalogState([], '请先从上方选择一个 LiteLLM 供应商。');
        } else if (options.skipCatalog) {
            this.setCatalogState([], '正在读取当前连接的模型目录…');
        } else if (preset.catalogProvider) {
            await this.loadModelCatalog(preset.catalogProvider);
        } else {
            this.setCatalogState([], '自定义接口的模型由服务端决定，请直接输入模型名称。');
        }
        this.updateModelFormReview();
    },

    async selectOtherProvider(providerKey, options = {}) {
        if (!providerKey) {
            this.setCatalogState([], '请先选择一个 LiteLLM 供应商。');
            this.updateModelFormReview();
            return;
        }
        const preset = this.getActiveProviderPreset();
        document.getElementById('modelProvider').value = providerKey;
        document.getElementById('modelName').placeholder = preset.modelPlaceholder;
        if (!options.preserveValues) {
            document.getElementById('modelApiKeyEnv').value = preset.envVar;
            document.getElementById('modelApiKeyEnvValue').value = '';
            document.getElementById('modelName').value = '';
        }
        if (!options.skipCatalog) await this.loadModelCatalog(providerKey);
        this.updateCredentialFields();
        this.updateModelFormReview();
    },

    getModelCatalogRequest(providerKey, options = {}) {
        const isEdit = document.getElementById('modelEditMode')?.value === 'true';
        const savedModelId = options.modelId
            || (isEdit ? document.getElementById('modelId')?.dataset.oldId || '' : '');
        return {
            provider: providerKey,
            q: '',
            limit: 300,
            model_id: savedModelId,
            api_base: this.normalizePastedText(document.getElementById('modelApiBase')?.value || '').trim(),
            credential_name: this.sanitizeCredentialName(document.getElementById('modelApiKeyEnv')?.value || ''),
            api_key: options.includePendingCredential
                ? document.getElementById('modelApiKeyEnvValue')?.value.trim() || ''
                : '',
            refresh: options.forceRefresh === true,
        };
    },

    async loadModelCatalog(providerKey, options = {}) {
        if (!providerKey) {
            this.setCatalogState([], '填写 API 地址后可同步服务端模型。', {
                status: 'not_configured', attempted: false,
            });
            return;
        }
        const requestId = ++this.catalogRequestId;
        const hint = document.getElementById('modelCatalogHint');
        const refreshButton = document.getElementById('modelCatalogRefreshButton');
        if (refreshButton) refreshButton.disabled = true;
        if (hint) hint.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>正在合并服务端与 LiteLLM 模型目录…';
        try {
            const payload = this.getModelCatalogRequest(providerKey, {
                ...options,
                includePendingCredential: options.forceRefresh === true,
            });
            // Connection addresses and credential references stay in the body
            // so they never appear in browser history or access-log URLs.
            const response = await fetch('/api/llm/models/catalog/discover', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const result = await response.json();
            if (requestId !== this.catalogRequestId) return;
            if (!response.ok || result.status !== 'success') {
                throw new Error(result.detail || result.message || '模型目录加载失败');
            }
            this.catalogVersion = result.data?.version || this.catalogVersion;
            this.setCatalogState(result.data?.models || [], '', result.data?.discovery || {});
        } catch (error) {
            if (requestId !== this.catalogRequestId) return;
            console.warn(`Catalog unavailable for ${providerKey}:`, error);
            this.setCatalogState([], '目录暂时不可用，仍可手动输入模型名称。', {
                status: 'error', attempted: true,
            });
        } finally {
            if (requestId === this.catalogRequestId && refreshButton) refreshButton.disabled = false;
        }
    },

    async refreshModelCatalog() {
        const preset = this.getActiveProviderPreset();
        const providerKey = preset.catalogProvider
            || (preset.key === 'compatible' ? 'compatible' : preset.providerValue || '');
        await this.loadModelCatalog(providerKey, { forceRefresh: true });
    },

    setCatalogState(models, message = '', discovery = null) {
        this.catalogModels = Array.isArray(models) ? models : [];
        if (discovery && typeof discovery === 'object') this.catalogDiscovery = discovery;
        const hint = document.getElementById('modelCatalogHint');
        if (hint) {
            const version = this.catalogVersion ? ` · LiteLLM ${this.catalogVersion}` : '';
            const available = this.catalogModels.filter(item => item.availability === 'available').length;
            const status = this.catalogDiscovery?.status || '';
            const fetchedDate = this.catalogDiscovery?.fetched_at
                ? new Date(this.catalogDiscovery.fetched_at)
                : null;
            const fetchedAt = fetchedDate && !Number.isNaN(fetchedDate.getTime())
                ? ` · 同步于 ${fetchedDate.toLocaleString('zh-CN', { hour12: false })}`
                : '';
            let statusText = '';
            if (status === 'live') statusText = `服务端可用 ${available} 个模型${fetchedAt}`;
            else if (status === 'cache') statusText = `服务端缓存 ${available} 个模型${fetchedAt}`;
            else if (status === 'stale') statusText = `实时同步失败，使用上次缓存的 ${available} 个模型${fetchedAt}`;
            else if (['not_configured', 'unsupported', 'error'].includes(status)) {
                statusText = this.catalogDiscovery?.message || '当前使用 LiteLLM 本地目录';
            } else statusText = `可选择 ${this.catalogModels.length} 个聊天模型`;
            hint.textContent = message || `${statusText}${version}；目录外模型也可直接输入。`;
        }
        this.renderModelCatalogMenu(document.getElementById('modelName')?.value || '');
    },

    openModelCatalog() {
        const menu = document.getElementById('modelCatalogMenu');
        const input = document.getElementById('modelName');
        if (!menu || !input) return;
        this.renderModelCatalogMenu(input.value);
        menu.classList.remove('d-none');
        input.setAttribute('aria-expanded', 'true');
    },

    closeModelCatalog() {
        document.getElementById('modelCatalogMenu')?.classList.add('d-none');
        document.getElementById('modelName')?.setAttribute('aria-expanded', 'false');
    },

    toggleModelCatalog() {
        const menu = document.getElementById('modelCatalogMenu');
        if (!menu) return;
        if (menu.classList.contains('d-none')) {
            this.openModelCatalog();
            document.getElementById('modelName')?.focus();
        } else {
            this.closeModelCatalog();
        }
    },

    renderModelCatalogMenu(query = '') {
        const menu = document.getElementById('modelCatalogMenu');
        if (!menu) return;
        const needle = String(query || '').trim().toLowerCase();
        const matches = this.catalogModels
            .filter(item => !needle || String(item.id).toLowerCase().includes(needle))
            .slice(0, 40);
        if (!matches.length) {
            menu.innerHTML = `
                <div class="model-catalog-empty">
                    <i class="bi bi-pencil-square"></i>
                    <strong>${this.catalogModels.length ? '目录中没有匹配项' : '当前没有可用目录'}</strong>
                    <span>可以继续手动输入，保存时会使用所选供应商适配器。</span>
                </div>`;
            return;
        }
        menu.innerHTML = matches.map(item => {
            const capabilities = [];
            if (item.availability === 'available') capabilities.push('服务端');
            else if (item.availability === 'catalog_only') capabilities.push('目录');
            if (item.supports_vision) capabilities.push('图片');
            if (item.supports_reasoning) capabilities.push('推理');
            if (item.supports_web_search) capabilities.push('搜索');
            const context = item.max_input_tokens ? this.formatTokenCount(item.max_input_tokens) : '';
            const availability = item.availability === 'available'
                ? '服务端可用'
                : item.availability === 'catalog_only' ? 'LiteLLM 目录' : (item.recommended ? '常用名称' : '版本/预览模型');
            return `
                <button type="button" class="model-catalog-item" role="option"
                    data-model-catalog-id="${this.escapeHtml(item.id)}">
                    <span class="model-catalog-item-main">
                        <strong>${this.escapeHtml(item.id)}</strong>
                        <small>${availability}${context ? ` · 上下文 ${context}` : ''}</small>
                    </span>
                    <span class="model-catalog-capabilities">${capabilities.map(value => `<em>${value}</em>`).join('')}</span>
                </button>`;
        }).join('');
        menu.querySelectorAll('[data-model-catalog-id]').forEach(button => {
            button.addEventListener('click', () => this.selectCatalogModel(button.dataset.modelCatalogId));
        });
    },

    selectCatalogModel(modelId) {
        const item = this.catalogModels.find(model => model.id === modelId);
        const input = document.getElementById('modelName');
        if (!input) return;
        input.value = modelId;
        if (this.modelIdAutofill) this.suggestModelId(modelId);
        if (item) {
            const contextInput = document.getElementById('modelContextWindow');
            const maxTokensInput = document.getElementById('modelMaxTokens');
            if (contextInput && !contextInput.value && item.max_input_tokens) contextInput.value = item.max_input_tokens;
            if (maxTokensInput && !maxTokensInput.value && item.max_output_tokens) maxTokensInput.value = item.max_output_tokens;
            if (item.supports_vision) document.getElementById('modelVision').checked = true;
        }
        this.closeModelCatalog();
        this.updateModelFormReview();
    },

    suggestModelId(modelName) {
        const input = document.getElementById('modelId');
        if (!input) return;
        const parts = String(modelName || '').split('/').filter(Boolean);
        const source = parts.at(-1) || this.getActiveProviderPreset().catalogProvider || 'model';
        let suggestion = source
            .toLowerCase()
            .replace(/[^a-z0-9._-]+/g, '-')
            .replace(/^-+|-+$/g, '')
            .slice(0, 60) || 'model';
        const currentId = document.getElementById('modelEditMode')?.value === 'true'
            ? input.dataset.oldId
            : '';
        if (this.currentModels[suggestion] && suggestion !== currentId) {
            let suffix = 2;
            while (this.currentModels[`${suggestion}-${suffix}`]) suffix += 1;
            suggestion = `${suggestion}-${suffix}`;
        }
        input.value = suggestion;
    },

    formatTokenCount(value) {
        const number = Number(value);
        if (!Number.isFinite(number)) return '-';
        if (number >= 1000000) return `${(number / 1000000).toFixed(number % 1000000 ? 1 : 0)}M`;
        if (number >= 1000) return `${(number / 1000).toFixed(number % 1000 ? 1 : 0)}K`;
        return number.toLocaleString();
    },

    renderSharedCredentialStatus() {
        const target = document.getElementById('modelSharedCredentialStatus');
        if (!target) return;
        const status = this.sharedCredentialStatus || {};
        const content = {
            loading: ['bi-arrow-repeat', '正在检查…'],
            pending: ['bi-shield-plus', '保存时写入本机'],
            preserved: ['bi-shield-check', '使用已保存的 API Key'],
            database: ['bi-shield-check', 'API Key 已保存在本机'],
            environment: ['bi-shield-check', 'API Key 已由环境变量提供'],
            missing: ['bi-exclamation-circle', '请填写 API Key'],
            invalid: ['bi-exclamation-circle', '凭据配置无效'],
            optional: ['bi-info-circle', '免认证接口可留空'],
        }[status.source] || ['bi-info-circle', '尚未配置 API Key'];
        target.className = `model-credential-status ${status.configured ? 'ready' : status.source === 'missing' || status.source === 'invalid' ? 'missing' : ''}`;
        target.innerHTML = `<i class="bi ${content[0]}"></i><span>${content[1]}</span>`;
    },

    async loadSharedCredentialStatus(rawName, options = {}) {
        const name = String(rawName || '').trim();
        if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) {
            this.sharedCredentialStatus = { name, configured: false, source: name ? 'invalid' : 'unknown' };
            this.renderSharedCredentialStatus();
            this.updateModelFormReview();
            return this.sharedCredentialStatus;
        }
        const requestId = ++this.credentialStatusRequestId;
        this.sharedCredentialStatus = { name, configured: false, source: 'loading' };
        this.renderSharedCredentialStatus();
        try {
            const response = await fetch(`/api/llm/credentials/${encodeURIComponent(name)}`);
            const result = await response.json().catch(() => ({}));
            if (requestId !== this.credentialStatusRequestId) return this.sharedCredentialStatus;
            if (!response.ok || result.status !== 'success') throw new Error(result.detail || '无法检查凭据');
            this.sharedCredentialStatus = result.data || { name, configured: false, source: 'missing' };
            if (options.optional && !this.sharedCredentialStatus.configured) {
                this.sharedCredentialStatus.source = 'optional';
            }
        } catch (error) {
            if (requestId !== this.credentialStatusRequestId) return this.sharedCredentialStatus;
            this.sharedCredentialStatus = { name, configured: false, source: 'unknown' };
        }
        this.renderSharedCredentialStatus();
        this.updateModelFormReview();
        return this.sharedCredentialStatus;
    },

    async ensureSharedCredential(name, value) {
        if (value) {
            const response = await fetch(`/api/llm/credentials/${encodeURIComponent(name)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ value }),
            });
            const result = await response.json().catch(() => ({}));
            if (!response.ok || result.status !== 'success') {
                throw new Error(result.detail || '本机凭据保存失败');
            }
            this.sharedCredentialStatus = result.data;
            this.renderSharedCredentialStatus();
            return;
        }
        const status = await this.loadSharedCredentialStatus(name);
        if (!status?.configured) {
            throw new Error(`凭据 ${name} 尚未配置，请填写密钥值后再保存。`);
        }
    },

    onUnifiedCredentialInput() {
        const value = document.getElementById('modelApiKeyEnvValue')?.value.trim() || '';
        const modeInput = document.getElementById('modelCredentialMode');
        if (!modeInput) return;
        if (value) {
            modeInput.value = 'environment';
            this.sharedCredentialStatus = {
                name: document.getElementById('modelApiKeyEnv')?.value || '',
                configured: true,
                source: 'pending',
            };
            this.renderSharedCredentialStatus();
            this.updateCredentialFields({ checkStatus: false });
            return;
        }
        modeInput.value = modeInput.dataset.canPreserve === 'true'
            ? 'preserve'
            : (this.getActiveProviderPreset().requiresCredential ? 'environment' : 'none');
        this.updateCredentialFields();
    },

    getEffectiveCredentialMode(provider, isEdit, envVar, envValue) {
        if (provider.key === 'local_codex') return 'none';
        const requestedMode = document.getElementById('modelCredentialMode')?.value || 'environment';
        if (envValue) return 'environment';
        if (requestedMode === 'preserve' && isEdit) return 'preserve';
        if (provider.requiresCredential) return 'environment';
        const credentialReady = this.sharedCredentialStatus?.name === envVar
            && this.sharedCredentialStatus?.configured;
        return credentialReady ? 'environment' : 'none';
    },

    updateCredentialFields(options = {}) {
        const mode = document.getElementById('modelCredentialMode')?.value || 'environment';
        const provider = this.getActiveProviderPreset();
        const envVar = document.getElementById('modelApiKeyEnv')?.value || '';
        const keyValue = document.getElementById('modelApiKeyEnvValue')?.value.trim() || '';
        if (keyValue) {
            this.credentialStatusRequestId += 1;
            this.sharedCredentialStatus = { name: envVar, configured: true, source: 'pending' };
            this.renderSharedCredentialStatus();
        } else if (mode === 'preserve') {
            this.credentialStatusRequestId += 1;
            this.sharedCredentialStatus = { name: envVar, configured: true, source: 'preserved' };
            this.renderSharedCredentialStatus();
        } else if (mode === 'none' && options.checkStatus !== true) {
            this.credentialStatusRequestId += 1;
            this.sharedCredentialStatus = { name: envVar, configured: false, source: 'optional' };
            this.renderSharedCredentialStatus();
        } else if (options.checkStatus !== false) {
            this.loadSharedCredentialStatus(envVar, { optional: !provider.requiresCredential });
        }
        this.updateModelFormReview();
    },

    updateModelFormReview() {
        const container = document.getElementById('modelFormReviewContent');
        if (!container) return;
        const provider = this.getActiveProviderPreset();
        const modelId = this.sanitizeModelId(document.getElementById('modelId')?.value || '');
        const modelName = this.normalizePastedText(document.getElementById('modelName')?.value || '').trim();
        const apiBase = this.normalizePastedText(document.getElementById('modelApiBase')?.value || '').trim();
        const envVar = this.sanitizeCredentialName(document.getElementById('modelApiKeyEnv')?.value || '');
        const envValue = document.getElementById('modelApiKeyEnvValue')?.value.trim() || '';
        const isEdit = document.getElementById('modelEditMode')?.value === 'true';
        this.updateModelSamplingControls(provider, modelName);
        const mode = this.getEffectiveCredentialMode(provider, isEdit, envVar, envValue);
        const checks = [
            { ok: provider.key !== 'other' || Boolean(provider.catalogProvider), label: provider.label || '选择供应商' },
            { ok: Boolean(modelName), label: modelName || '选择或输入服务端模型' },
            { ok: Boolean(modelId) && !/[\\/?#]/.test(modelId), label: modelId ? `配置名：${modelId}` : '填写配置名称' },
            { ok: !provider.apiBaseRequired || /^https?:\/\//i.test(apiBase), label: provider.apiBaseRequired ? (apiBase ? 'API 地址已填写' : '填写 API 地址') : '连接地址可用默认值' },
            {
                ok: provider.key === 'local_codex'
                    || mode === 'none'
                    || (mode === 'preserve' && isEdit)
                    || (mode === 'environment' && /^[A-Za-z_][A-Za-z0-9_]*$/.test(envVar)
                        && (Boolean(envValue) || (this.sharedCredentialStatus?.name === envVar && this.sharedCredentialStatus?.configured))),
                label: mode === 'environment' ? (envValue ? '新的 API Key 将保存到本机'
                    : this.sharedCredentialStatus?.name === envVar && this.sharedCredentialStatus?.configured
                        ? '本机 API Key 已就绪' : '填写 API Key')
                    : mode === 'preserve' ? '已有 API Key 将保持不变' : '此接口不需要 API Key',
            },
        ];
        container.innerHTML = `
            <div class="model-review-provider">
                <span>接入方式</span><strong>${this.escapeHtml(provider.label || '待选择')}</strong>
            </div>
            <div class="model-review-checks">
                ${checks.map(check => `
                    <div class="${check.ok ? (check.warning ? 'warning' : 'ready') : 'pending'}">
                        <i class="bi ${check.ok ? (check.warning ? 'bi-exclamation-circle' : 'bi-check-circle') : 'bi-circle'}"></i>
                        <span>${this.escapeHtml(check.label)}</span>
                    </div>`).join('')}
            </div>
            <div class="model-review-result ${checks.every(check => check.ok) ? 'ready' : ''}">
                ${checks.every(check => check.ok) ? '可以保存配置' : '完成左侧必填项后即可保存'}
            </div>`;
    },

    filterModels() {
        this.renderModels();
    },

    /**
     * Load all models
     */
    async loadModels() {
        try {
            const response = await fetch('/api/llm/models');
            const result = await response.json();

            if (result.status === 'success') {
                this.currentModels = result.data || {};
                this.renderModels();
            } else {
                throw new Error(result.message || '未知错误');
            }
        } catch (error) {
            console.error('Failed to load models:', error);
            const container = document.getElementById('llmModelsList');
            if (container) {
                container.innerHTML = `
                    <div class="col-12">
                        <div class="alert alert-danger">
                            <i class="bi bi-exclamation-triangle me-2"></i>
                            加载模型失败：${this.escapeHtml(error.message)}
                        </div>
                    </div>
                `;
            }
        }
    },

    /**
     * Render models list
     */
    renderModels() {
        const container = document.getElementById('llmModelsList');
        if (!container) return;
        if (this.modelSortable) {
            this.modelSortable.destroy();
            this.modelSortable = null;
        }
        try {
            const allEntries = Object.entries(this.currentModels || {}).filter(([, config]) => config);
            const search = document.getElementById('llmModelSearch')?.value.trim().toLowerCase() || '';
            const providerFilter = document.getElementById('llmProviderFilter');
            const selectedProvider = providerFilter?.value || '';
            const providers = [...new Set(allEntries.map(([, config]) => this.getModelManagementMeta(config).provider))]
                .filter(Boolean)
                .sort((a, b) => this.getProviderLabel(a).localeCompare(this.getProviderLabel(b), 'zh-CN'));
            if (providerFilter) {
                providerFilter.innerHTML = '<option value="">全部供应商</option>' + providers
                    .map(provider => `<option value="${this.escapeHtml(provider)}">${this.escapeHtml(this.getProviderLabel(provider))}</option>`)
                    .join('');
                providerFilter.value = providers.includes(selectedProvider) ? selectedProvider : '';
            }
            const activeProvider = providerFilter?.value || '';
            const entries = allEntries.filter(([modelId, config]) => {
                const meta = this.getModelManagementMeta(config);
                const matchesProvider = !activeProvider || meta.provider === activeProvider;
                const haystack = `${modelId} ${config.model || ''} ${this.getProviderLabel(meta.provider)} ${config.api_base || ''}`.toLowerCase();
                return matchesProvider && (!search || haystack.includes(search));
            });

            const envCount = allEntries.filter(([, config]) => this.getModelManagementMeta(config).credential?.mode === 'environment').length;
            const directCount = allEntries.filter(([, config]) => this.getModelManagementMeta(config).credential?.mode === 'direct').length;
            const missingCount = allEntries.filter(([, config]) => {
                const credential = this.getModelManagementMeta(config).credential || {};
                return credential.mode === 'missing' || (credential.mode === 'environment' && !credential.configured);
            }).length;
            const summary = document.getElementById('llmModelSummary');
            if (summary) {
                summary.innerHTML = `
                    <span><strong>${allEntries.length}</strong> 个模型连接</span>
                    <span class="ready"><i class="bi bi-shield-check"></i>${envCount} 个 API Key 已统一管理</span>
                    ${directCount ? `<span class="warning"><i class="bi bi-exclamation-circle"></i>${directCount} 个使用旧版凭据</span>` : ''}
                    ${missingCount ? `<span class="muted"><i class="bi bi-key"></i>${missingCount} 个未配置密钥</span>` : ''}
                    ${entries.length !== allEntries.length ? `<span class="filter-result">当前显示 ${entries.length} 个</span>` : ''}`;
            }

            if (!allEntries.length) {
                container.innerHTML = `
                    <div class="llm-model-empty">
                        <i class="bi bi-boxes"></i>
                        <strong>还没有模型连接</strong>
                        <span>从常用供应商开始，通常一分钟内即可完成。</span>
                        <button class="btn btn-primary btn-sm" onclick="LLMManager.showAddModelModal()">添加第一个模型</button>
                    </div>`;
                return;
            }
            if (!entries.length) {
                container.innerHTML = `
                    <div class="llm-model-empty compact">
                        <i class="bi bi-search"></i>
                        <strong>没有匹配的模型</strong>
                        <span>调整搜索词或供应商筛选。</span>
                    </div>`;
                return;
            }

            let html = '';
            for (const [modelId, config] of entries) {
                const meta = this.getModelManagementMeta(config);
                const credential = meta.credential || {};
                const safeModelId = this.escapeHtml(modelId);
                const safeConfiguredModel = this.escapeHtml(config.model || '未知');
                const domModelId = this.getModelDomId(modelId);
                const providerLabel = this.escapeHtml(this.getProviderLabel(meta.provider));
                const credentialState = credential.mode === 'environment' && credential.configured
                    ? { className: 'ready', icon: 'bi-shield-check', label: 'API Key 已就绪' }
                    : credential.mode === 'environment'
                        ? { className: 'missing', icon: 'bi-key', label: 'API Key 未配置' }
                    : credential.mode === 'direct'
                        ? { className: 'warning', icon: 'bi-exclamation-circle', label: 'API Key 已配置（旧版）' }
                        : credential.mode === 'none'
                            ? { className: 'local', icon: 'bi-pc-display', label: '本地调用' }
                            : { className: 'missing', icon: 'bi-key', label: '未配置密钥' };
                const tokenPills = [];
                if (config.context_window_tokens || config.max_input_tokens) {
                    tokenPills.push(`<span title="上下文窗口"><i class="bi bi-arrows-expand"></i>${this.formatTokenCount(config.context_window_tokens || config.max_input_tokens)}</span>`);
                }
                if (config.max_tokens && meta.provider !== 'local_codex') tokenPills.push(`<span title="最大输出"><i class="bi bi-box-arrow-up"></i>${this.formatTokenCount(config.max_tokens)}</span>`);
                if (config.supports_vision) tokenPills.push('<span title="支持图片"><i class="bi bi-image"></i>图片</span>');
                if (config.enable_web_search || config.codex_web_search) tokenPills.push('<span title="启用 Web 搜索"><i class="bi bi-globe"></i>搜索</span>');
                html += `
                    <div class="col-md-6 col-lg-4" data-id="${safeModelId}" data-provider="${this.escapeHtml(meta.provider)}">
                        <div class="card h-100 llm-model-card">
                            <div class="card-body">
                                <div class="llm-model-card-topline">
                                    <span class="llm-provider-badge"><i class="bi bi-cpu"></i>${providerLabel}</span>
                                    <i class="bi bi-grip-vertical drag-handle" title="拖动排序"></i>
                                </div>
                                <div class="llm-model-identity">
                                    <h6>${safeModelId}</h6>
                                    <code title="${safeConfiguredModel}">${safeConfiguredModel}</code>
                                </div>
                                <div class="llm-model-status ${credentialState.className}">
                                    <i class="bi ${credentialState.icon}"></i>
                                    <span>${this.escapeHtml(credentialState.label)}</span>
                                    ${meta.mapping_count ? `<em>${Number(meta.mapping_count)} 个任务在用</em>` : '<em>尚未分配任务</em>'}
                                </div>
                                ${config.api_base ? `<div class="llm-model-endpoint" title="${this.escapeHtml(config.api_base)}"><i class="bi bi-link-45deg"></i>${this.escapeHtml(config.api_base)}</div>` : ''}
                                <div class="llm-model-capabilities">
                                    ${meta.provider === 'local_codex'
                                        ? `<span title="独立调用的推理强度"><i class="bi bi-stars"></i>${this.escapeHtml(config.codex_reasoning_effort || config.extra_body?.reasoning_effort || 'medium')}</span>`
                                        : this.isGemini3ModelConfig(config)
                                        ? '<span title="Gemini 3+ 使用模型默认采样设置"><i class="bi bi-stars"></i>默认采样</span>'
                                        : `<span title="温度"><i class="bi bi-thermometer-half"></i>${this.formatTemperature(config.temperature, '默认')}</span>`}
                                    ${tokenPills.join('')}
                                </div>
                                <div id="modelTestResult_${domModelId}" class="llm-model-test-result" style="display:none;"></div>
                                <div class="llm-model-actions">
                                    <button class="btn btn-sm btn-quiet-accent llm-model-test" id="testModelBtn_${domModelId}" data-model-id="${safeModelId}">
                                        <i class="bi bi-lightning-charge me-1"></i>测试
                                    </button>
                                    <button class="btn btn-sm btn-outline-secondary llm-model-edit" data-model-id="${safeModelId}">
                                        <i class="bi bi-pencil me-1"></i>编辑
                                    </button>
                                    <button class="btn btn-sm btn-link text-danger llm-model-delete" data-model-id="${safeModelId}" title="删除">
                                        <i class="bi bi-trash"></i>
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                `;
            }
            container.innerHTML = html;
            container.querySelectorAll('.llm-model-test').forEach(button => {
                button.addEventListener('click', () => this.testModelConnectivity(button.dataset.modelId));
            });
            container.querySelectorAll('.llm-model-edit').forEach(button => {
                button.addEventListener('click', event => {
                    event.preventDefault();
                    this.editModel(button.dataset.modelId);
                });
            });
            container.querySelectorAll('.llm-model-delete').forEach(button => {
                button.addEventListener('click', event => {
                    event.preventDefault();
                    this.deleteModel(button.dataset.modelId);
                });
            });

            if (window.Sortable && !search && !activeProvider) {
                this.modelSortable = new Sortable(container, {
                    animation: 150,
                    handle: '.drag-handle',
                    ghostClass: 'bg-light',
                    onEnd: async () => {
                        const items = container.querySelectorAll('[data-id]');
                        const order = Array.from(items).map(el => el.getAttribute('data-id'));
                        try {
                            const res = await fetch('/api/llm/models/reorder', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ order })
                            });
                            const result = await res.json().catch(() => ({}));
                            if (!res.ok || result.status !== 'success') {
                                throw new Error(result.detail || result.message || '调整顺序失败');
                            }
                            UI.showSuccess('模型顺序已保存');
                        } catch (e) {
                            console.error('Failed to save new order', e);
                            UI.showError(`保存模型顺序失败：${e.message}`);
                        }
                    }
                });
            }
        } catch (e) {
            console.error('Error rendering models:', e);
                container.innerHTML = `<div class="alert alert-danger">渲染模型失败：${this.escapeHtml(e.message)}</div>`;
        }
    },

    /**
     * Load task model routes and their owning components
     */
    async loadMappings() {
        try {
            const [response, ownerResponse] = await Promise.all([
                fetch('/api/llm/mappings'),
                fetch('/api/llm/route-owners')
                    .then(routeOwnerResponse => routeOwnerResponse.ok
                        ? routeOwnerResponse.json()
                        : { status: 'error', data: [] })
                    .catch(error => {
                        console.warn('Route owner metadata is unavailable:', error);
                        return { status: 'error', data: [] };
                    }),
            ]);
            if (!response.ok) {
                throw new Error(`加载映射失败（HTTP ${response.status}）`);
            }
            const result = await response.json();

            if (result.status === 'success') {
                this.currentRouteOwners = Object.fromEntries(
                    (ownerResponse.status === 'success' ? ownerResponse.data : [])
                        .filter(owner => owner?.id)
                        .map(owner => [owner.id, owner])
                );
                this.currentMappings = this.mergeDeclaredTaskMappings(
                    result.data || {},
                    this.currentRouteOwners,
                );
                this.renderMappings();
            } else {
                throw new Error(result.message || '加载映射时发生未知错误');
            }
        } catch (error) {
            console.error('Failed to load mappings:', error);
            const container = document.getElementById('llmMappingsList');
            if (container) {
                container.innerHTML = `<div class="alert alert-danger">加载映射失败：${this.escapeHtml(error.message)}</div>`;
            }
        }
    },

    /**
     * Render task model routes grouped by their owning component
     */
    renderMappings() {
        const container = document.getElementById('llmMappingsList');
        if (!container) return;

        if (!this.currentMappings || Object.keys(this.currentMappings).length === 0) {
            container.innerHTML = `
                <div class="text-center py-5 text-muted">
                    <i class="bi bi-diagram-3 fs-1 mb-3 d-block"></i>
                    <p>尚未声明可配置的任务模型路由。</p>
                </div>
            `;
            return;
        }

        const ownerEntries = Object.entries(this.currentMappings).sort(([leftName], [rightName]) => {
            const left = this.currentRouteOwners[leftName] || {};
            const right = this.currentRouteOwners[rightName] || {};
            if (Boolean(left.featured) !== Boolean(right.featured)) return left.featured ? -1 : 1;
            const categoryDelta = Number(left.category_order || 500) - Number(right.category_order || 500);
            if (categoryDelta !== 0) return categoryDelta;
            return this.getRouteOwnerMeta(leftName).displayName.localeCompare(
                this.getRouteOwnerMeta(rightName).displayName,
                'zh-CN'
            );
        });

        let html = '<div class="llm-route-groups">';
        for (const [ownerId, mappings] of ownerEntries) {
            const safeOwnerId = this.escapeHtml(ownerId);
            const owner = this.getRouteOwnerMeta(ownerId);
            const taskEntries = Object.entries(mappings || {}).sort(([leftType], [rightType]) => {
                const left = this.getTaskRouteMeta(ownerId, leftType);
                const right = this.getTaskRouteMeta(ownerId, rightType);
                return left.order - right.order || left.label.localeCompare(right.label, 'zh-CN');
            });
            const advancedTaskCount = taskEntries.filter(([callType]) => (
                this.getTaskRouteMeta(ownerId, callType).advanced
            )).length;
            const commonTaskCount = taskEntries.length - advancedTaskCount;
            html += `
                <section class="llm-route-group">
                    <header class="llm-route-group-header">
                        <div class="llm-route-owner">
                            <span class="llm-route-owner-icon"><i class="bi ${this.escapeHtml(owner.icon)}"></i></span>
                            <div>
                                <div class="llm-route-owner-title">
                                    <h6>${this.escapeHtml(owner.displayName)}</h6>
                                    <span class="llm-route-owner-kind is-${this.escapeHtml(owner.ownerKind)}">${this.escapeHtml(owner.kindLabel)}</span>
                                    <span class="llm-route-task-count">${commonTaskCount} 项常用${advancedTaskCount ? ` · ${advancedTaskCount} 项高级` : ''}</span>
                                </div>
                                <p>${this.escapeHtml(owner.description)}</p>
                                <small>路由主体标识：<code>${safeOwnerId}</code></small>
                            </div>
                        </div>
                        ${advancedTaskCount ? `
                        <button class="btn btn-sm btn-outline-secondary llm-route-advanced-toggle" data-owner-id="${safeOwnerId}" aria-expanded="false">
                            <i class="bi bi-sliders me-1"></i>显示高级路由
                        </button>` : ''}
                        <button class="btn btn-sm btn-outline-secondary llm-prompts-button" data-owner-id="${safeOwnerId}">
                            <i class="bi bi-chat-quote me-1"></i>管理提示词
                        </button>
                    </header>
                    <div class="llm-route-table-wrap">
                        <table class="llm-route-table">
                            <thead>
                                <tr>
                                    <th scope="col">任务</th>
                                    <th scope="col">主模型</th>
                                    <th scope="col">失败后备用</th>
                                    <th scope="col">参数</th>
                                    <th scope="col" class="llm-route-action-column">操作</th>
                                </tr>
                            </thead>
                            <tbody>
            `;

            for (const [callType, mapping] of taskEntries) {
                const task = this.getTaskRouteMeta(ownerId, callType);
                const fallbackModels = Array.isArray(mapping.fallback) ? mapping.fallback : [];
                const fallback = fallbackModels[0] || '';
                const overrideParams = mapping.override_params && typeof mapping.override_params === 'object'
                    ? mapping.override_params
                    : {};
                const overrideKeys = Object.keys(overrideParams);
                const safeCallType = this.escapeHtml(callType);
                const routeKey = `${ownerId}.${callType}`;

                html += `
                    <tr class="llm-route-row${task.declared ? '' : ' is-undeclared'}${task.advanced ? ' is-advanced d-none' : ''}" data-owner-id="${safeOwnerId}">
                        <td class="llm-route-task-cell">
                            <div class="llm-route-task-title">
                                <strong>${this.escapeHtml(task.label)}</strong>
                                <span class="llm-route-category">${this.escapeHtml(task.category)}</span>
                                ${task.declared ? '' : '<span class="llm-route-declaration-warning">待补充任务声明</span>'}
                            </div>
                            <p>${this.escapeHtml(task.description)}</p>
                            <code class="llm-route-key" title="${this.escapeHtml(routeKey)}">${safeCallType}</code>
                        </td>
                        <td>${this.renderRouteModelCell(mapping.primary)}</td>
                        <td>
                            ${this.renderRouteModelCell(fallback, '未设置')}
                            ${fallbackModels.length > 1 ? `<span class="llm-route-more">另有 ${fallbackModels.length - 1} 个</span>` : ''}
                        </td>
                        <td>
                            ${overrideKeys.length > 0
                                ? `<span class="llm-route-overrides" title="${this.escapeHtml(JSON.stringify(overrideParams))}"><i class="bi bi-sliders me-1"></i>${overrideKeys.length} 项覆盖</span>`
                                : '<span class="llm-route-overrides is-empty">模型默认</span>'}
                        </td>
                        <td class="llm-route-row-action">
                            <button class="btn btn-sm btn-outline-primary llm-mapping-edit" data-owner-id="${safeOwnerId}" data-call-type="${safeCallType}">
                                <i class="bi bi-pencil-square me-1"></i>配置
                            </button>
                        </td>
                    </tr>
                `;
            }

            html += `
                            </tbody>
                        </table>
                    </div>
                </section>
            `;
        }
        html += '</div>';

        container.innerHTML = html;
        container.querySelectorAll('.llm-prompts-button').forEach(button => {
            button.addEventListener('click', () => this.showPromptsModal(button.dataset.ownerId));
        });
        container.querySelectorAll('.llm-mapping-edit').forEach(button => {
            button.addEventListener('click', () => this.editMapping(button.dataset.ownerId, button.dataset.callType));
        });
        container.querySelectorAll('.llm-route-advanced-toggle').forEach(button => {
            button.addEventListener('click', () => {
                const ownerId = button.dataset.ownerId;
                const rows = container.querySelectorAll(`.llm-route-row.is-advanced[data-owner-id="${CSS.escape(ownerId)}"]`);
                const expanded = button.getAttribute('aria-expanded') !== 'true';
                rows.forEach(row => row.classList.toggle('d-none', !expanded));
                button.setAttribute('aria-expanded', String(expanded));
                button.innerHTML = expanded
                    ? '<i class="bi bi-chevron-up me-1"></i>收起高级路由'
                    : '<i class="bi bi-sliders me-1"></i>显示高级路由';
            });
        });
    },

    /**
     * Show prompt settings for one route owner
     */
    async showPromptsModal(ownerId) {
        const modal = new bootstrap.Modal(document.getElementById('routePromptsModal'));
        const container = document.getElementById('routePromptsModalBody');
        const title = document.getElementById('routePromptsModalTitle');

        title.textContent = `提示词 · ${this.getRouteOwnerMeta(ownerId).displayName}`;
        container.innerHTML = `
            <div class="text-center py-5">
                <div class="spinner-border text-primary" role="status"></div>
                <p class="mt-2 text-muted">正在加载提示词…</p>
            </div>
        `;

        modal.show();

        try {
            const settings = await API.capabilities.getSettings(ownerId);
            const prompts = Object.fromEntries(
                (settings.groups || [])
                    .flatMap(group => group.fields || [])
                    .filter(field => field.key.toLowerCase().includes('prompt') && typeof field.value === 'string')
                    .map(field => [field.key, field.value])
            );

            if (Object.keys(prompts).length === 0) {
                container.innerHTML = `
                    <div class="text-center py-5 text-muted">
                        <i class="bi bi-chat-quote fs-1 mb-3 d-block"></i>
                        <p>未找到此路由主体的提示词。</p>
                    </div>
                `;
                return;
            }

            // Render Prompt Editors
            let html = '';
            for (const [key, value] of Object.entries(prompts)) {
                html += `
                    <div class="mb-4">
                        <label class="form-label fw-bold">${UI.escapeHtml(key)}</label>
                        <textarea class="form-control font-monospace mb-2" rows="6" id="prompt_${UI.escapeHtml(key)}">${UI.escapeHtml(value)}</textarea>
                        <div class="d-flex justify-content-end gap-2">
                             <button class="btn btn-sm btn-outline-secondary" type="button" data-prompt-reset="${UI.escapeHtml(key)}">重置</button>
                             <button class="btn btn-sm btn-primary llm-prompt-save" type="button" data-owner-id="${UI.escapeHtml(ownerId)}" data-prompt-key="${UI.escapeHtml(key)}">保存</button>
                        </div>
                    </div>
                `;
            }
            container.innerHTML = html;
            container.querySelectorAll('[data-prompt-reset]').forEach(button => {
                const key = button.dataset.promptReset;
                button.addEventListener('click', () => {
                    const input = document.getElementById(`prompt_${key}`);
                    if (input) input.value = prompts[key] || '';
                });
            });
            container.querySelectorAll('.llm-prompt-save').forEach(button => {
                button.addEventListener('click', () => this.savePrompt(button.dataset.ownerId, button.dataset.promptKey));
            });

        } catch (error) {
            console.error('Failed to load prompts:', error);
            container.innerHTML = `
                <div class="alert alert-danger">
                    加载提示词失败：${this.escapeHtml(error.message)}
                </div>
            `;
        }
    },

    /**
     * Save specific prompt
     */
    async savePrompt(ownerId, key) {
        try {
            const textarea = document.getElementById(`prompt_${key}`);
            const newValue = textarea.value;

            await API.capabilities.updateSettings(ownerId, { [key]: newValue });
            UI.showSuccess('提示词已保存并应用');

        } catch (error) {
            console.error('Save prompt error:', error);
            UI.showError(`保存失败：${error.message}`);
        }
    },

    /**
     * Load statistics
     */
    async loadRouteOwners() {
        if (!this.routeOwnersPromise) {
            this.routeOwnersPromise = fetch('/api/llm/route-owners')
                .then(async response => {
                    if (!response.ok) throw new Error('任务名称暂时无法读取');
                    const result = await response.json();
                    if (result.status !== 'success') throw new Error('任务名称暂时无法读取');
                    this.currentRouteOwners = Object.fromEntries((result.data || []).map(owner => [owner.id, owner]));
                    this.routeOwnersUnavailable = false;
                }).catch(error => {
                    this.routeOwnersUnavailable = true;
                    console.warn(error);
                }).finally(() => { this.routeOwnersPromise = null; });
        }
        return this.routeOwnersPromise;
    },

    getUsageTaskMeta(key) {
        const dot = key.indexOf('.');
        const ownerId = dot < 0 ? key : key.slice(0, dot);
        const callType = dot < 0 ? '' : key.slice(dot + 1);
        const canonical = ownerId === 'builtin_chatbot' ? 'assistant' : ownerId;
        const declared = this.currentRouteOwners[canonical]?.usage_tasks?.[callType];
        const meta = declared ? { ...declared, declared: true } : this.getTaskRouteMeta(canonical, callType);
        return { ...meta, label: meta.declared ? meta.label : '自定义任务',
            owner: this.currentRouteOwners[canonical]?.display_name || ownerId };
    },

    async loadStats() {
        this.usageAbortController?.abort();
        const controller = new AbortController();
        this.usageAbortController = controller;
        const requestId = (this.usageRequestId || 0) + 1;
        this.usageRequestId = requestId;
        this.usageLoading = true;
        this.usageError = '';
        this.renderStats();
        const params = new URLSearchParams({ period: this.activeStatsType, view: this.usageView });
        if (this.usageSubject) params.set('subject', this.usageSubject);
        try {
            const [response] = await Promise.all([
                fetch(`/api/llm/usage?${params}`, { signal: controller.signal }),
                this.loadRouteOwners(),
            ]);
            if (!response.ok) throw new Error(`用量加载失败（HTTP ${response.status}）`);
            const result = await response.json();
            if (result.status !== 'success' || !result.data) throw new Error('用量数据不可用');
            if (requestId !== this.usageRequestId) return;
            this.currentStats = result.data;
        } catch (error) {
            if (controller.signal.aborted || requestId !== this.usageRequestId) return;
            this.usageError = error.message || '用量加载失败';
        } finally {
            if (requestId === this.usageRequestId) {
                this.usageLoading = false;
                this.renderStats();
            }
        }
    },

    setUsagePeriod(period) {
        if (!['today', '7d', '30d', 'session', 'total'].includes(period)) return;
        this.activeStatsType = period;
        this.usagePage = 1;
        this.usageExpanded = null;
        this.loadStats();
    },

    setUsageView(view) {
        if (!['task', 'chat'].includes(view)) return;
        this.usageView = view;
        this.usageSubject = null;
        this.usageSearch = '';
        this.usagePage = 1;
        this.usageExpanded = null;
        this.loadStats();
    },

    openUsageSubject(key) {
        this.usageSubject = key;
        this.usageView = 'task';
        this.usageSearch = '';
        this.usagePage = 1;
        this.usageExpanded = null;
        this.loadStats();
    },

    usageNumber(value) {
        return Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });
    },

    usageDate(value) {
        return value ? this.escapeHtml(String(value).replace('T', ' ').slice(0, 19)) : '—';
    },

    usageCosts(metrics) {
        const costs = Object.entries(metrics.costs || {}).map(([currency, amount]) => {
            const label = currency === 'UNSPECIFIED' ? '未声明币种' : currency;
            const formatted = Number(amount).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
            return `<span>${this.escapeHtml(label)} ${formatted}</span>`;
        });
        return costs.length ? costs.join('') : '—';
    },

    usageTokens(metrics) {
        return metrics.token_calls ? this.usageNumber(metrics.tokens) : '—';
    },

    renderStats() {
        const container = document.getElementById('llmStatsList');
        if (!container) return;
        const periods = { today: '今日', '7d': '近 7 天', '30d': '近 30 天', session: '本次运行', total: '累计' };
        const subject = this.currentStats.subject;
        container.innerHTML = `<section class="llm-usage" aria-label="模型用量">
            <div class="usage-toolbar">
                <div class="usage-views" aria-label="统计视角">
                    <button type="button" data-usage-view="task" aria-pressed="${this.usageView === 'task'}">调用类型</button>
                    <button type="button" data-usage-view="chat" aria-pressed="${this.usageView === 'chat'}">聊天对象</button>
                </div>
                <label class="usage-period">统计周期<select class="form-select form-select-sm" id="usagePeriod">
                    ${Object.entries(periods).map(([value, label]) => `<option value="${value}" ${value === this.activeStatsType ? 'selected' : ''}>${label}</option>`).join('')}
                </select></label>
            </div>
            ${this.usageSubject ? `<div class="usage-breadcrumb"><button type="button" id="usageBack">全部聊天对象</button><span aria-hidden="true">/</span><span>${this.escapeHtml(subject?.key === this.usageSubject ? subject.name : '所选对象')}</span></div>` : ''}
            <div id="usageResults" aria-live="polite" aria-busy="${Boolean(this.usageLoading)}"></div>
        </section>`;
        container.querySelectorAll('[data-usage-view]').forEach(button => button.addEventListener('click', () => this.setUsageView(button.dataset.usageView)));
        container.querySelector('#usagePeriod').addEventListener('change', event => this.setUsagePeriod(event.target.value));
        container.querySelector('#usageBack')?.addEventListener('click', () => this.setUsageView('chat'));
        this.renderUsageResults();
    },

    renderUsageResults() {
        const target = document.getElementById('usageResults');
        if (!target) return;
        if (this.usageLoading) {
            target.innerHTML = '<div class="usage-empty" role="status">正在读取用量…</div>';
            return;
        }
        if (this.usageError) {
            target.innerHTML = `<div class="usage-empty" role="alert">${this.escapeHtml(this.usageError)}<button type="button" id="usageRetry">重新加载</button></div>`;
            target.querySelector('#usageRetry').addEventListener('click', () => this.loadStats());
            return;
        }
        const metrics = this.currentStats.totals || {};
        const unknownTokens = Math.max(0, (metrics.calls || 0) - (metrics.token_calls || 0));
        const unpriced = Math.max(0, (metrics.calls || 0) - (metrics.cost_calls || 0));
        target.innerHTML = `<div class="usage-overview" aria-label="所选范围概览">
            <div><small>成功次数</small><strong>${this.usageNumber(metrics.successes)}</strong><span>失败尝试不计入</span></div>
            <div><small>已记录 Token</small><strong>${this.usageTokens(metrics)}</strong><span>${unknownTokens ? `${this.usageNumber(unknownTokens)} 次尝试未上报用量` : metrics.estimated_calls ? '含估算 Token' : '输入与输出合计'}</span></div>
            <div><small>预估费用</small><strong class="usage-money">${this.usageCosts(metrics)}</strong><span>已计价 ${this.usageNumber(metrics.cost_calls)} / ${this.usageNumber(metrics.calls)} 次尝试${unpriced ? ` · ${this.usageNumber(unpriced)} 次未知` : ''}</span></div>
            <div><small>失败次数</small><strong>${this.usageNumber(metrics.failures)}</strong><span>未成功的请求</span></div>
        </div>
        <div class="usage-list-toolbar"><label class="usage-search-label"><span class="visually-hidden">搜索${this.usageView === 'chat' ? '聊天对象' : '调用类型'}</span><input type="search" id="usageSearch" class="form-control form-control-sm" placeholder="搜索${this.usageView === 'chat' ? '聊天对象' : '中文名称或英文标识'}" value="${this.escapeHtml(this.usageSearch)}"></label><span id="usageRowCount" class="usage-secondary"></span></div>
        <div id="usageTable"></div>
        <div class="usage-footnote">${this.renderUsageFootnote()}</div>`;
        target.querySelector('#usageSearch').addEventListener('input', event => {
            this.usageSearch = event.target.value;
            this.usagePage = 1;
            this.usageExpanded = null;
            this.renderUsageTable();
        });
        this.renderUsageTable();
    },

    renderUsageFootnote() {
        const meta = this.currentStats.metadata || {};
        const notes = [`统计自 ${this.usageDate(meta.chat_tracking_started_at)} 起记录。成功次数不含失败尝试；Token 和费用包含已上报的消耗。Codex 按官方标准 API 单价折算，包含在预估费用中；无逐次用量时按整轮统计。`];
        if (this.activeStatsType === 'session' && meta.run_started_at) notes.push(`运行开始：${this.usageDate(meta.run_started_at)}。`);
        if (meta.daily_available_from && ['7d', '30d'].includes(this.activeStatsType) && meta.daily_available_from > meta.range_start) notes.push(`现有每日记录从 ${this.escapeHtml(meta.daily_available_from)} 开始，之前的数据不可拆分。`);
        notes.push(`日期按服务端时区 ${this.escapeHtml(meta.timezone || '')} 统计。未上报显示 —。`);
        if (this.routeOwnersUnavailable) notes.push('任务名称暂不可用，刷新后重试。');
        return notes.join(' ');
    },

    getUsageRows() {
        const query = this.usageSearch.trim().toLocaleLowerCase();
        const rows = (this.currentStats.rows || []).filter(row => {
            const meta = this.usageView === 'task' ? this.getUsageTaskMeta(row.key) : {};
            return !query || `${row.name} ${row.key} ${meta.label || ''} ${meta.owner || ''}`.toLocaleLowerCase().includes(query);
        });
        const { field, direction } = this.usageSort;
        const currency = Object.keys(this.currentStats.totals?.costs || {})[0];
        const value = row => {
            if (field === 'cost') return row.metrics.costs?.[currency] ?? null;
            if (field === 'tokens' && !row.metrics.token_calls) return null;
            return Number(row.metrics[field] || 0);
        };
        const compare = (a, b) => {
            const left = value(a), right = value(b);
            if (left === null && right !== null) return 1;
            if (right === null && left !== null) return -1;
            return ((left ?? 0) - (right ?? 0)) * (direction === 'desc' ? -1 : 1) || a.key.localeCompare(b.key);
        };
        return rows.sort(compare);
    },

    renderUsageTable() {
        const target = document.getElementById('usageTable');
        if (!target) return;
        const canSortCost = Object.keys(this.currentStats.totals?.costs || {}).length === 1;
        if (!canSortCost && this.usageSort.field === 'cost') this.usageSort = { field: 'tokens', direction: 'desc' };
        const rows = this.getUsageRows();
        const pages = Math.max(1, Math.ceil(rows.length / 20));
        this.usagePage = Math.min(this.usagePage, pages);
        document.getElementById('usageRowCount').textContent = `${rows.length} 项${this.usageSearch ? '匹配 · 概览为所选范围合计' : ''}`;
        if (!rows.length) {
            target.innerHTML = `<div class="usage-empty">${this.usageSearch ? '没有匹配的记录' : '此范围暂无调用记录'}</div>`;
            return;
        }
        const headers = [['successes', '成功次数'], ['tokens', 'Token'], ['cost', '预估费用'], ['failures', '失败']];
        const pageRows = rows.slice((this.usagePage - 1) * 20, this.usagePage * 20);
        const kindLabels = { group: '群聊', user: '私聊', unknown: '类型未确认', system: '系统' };
        target.innerHTML = `<div class="usage-table-scroll"><table class="usage-table"><thead><tr><th>${this.usageView === 'chat' ? '聊天对象' : '调用类型'}</th>
            ${headers.map(([field, label]) => {
                const active = this.usageSort.field === field;
                const sort = active ? (this.usageSort.direction === 'desc' ? 'descending' : 'ascending') : 'none';
                return `<th aria-sort="${sort}">${field === 'cost' && !canSortCost ? `<span title="不同币种不直接比较">${label}</span>` : `<button type="button" data-usage-sort="${field}">${label}<span class="usage-sort-icon ${active ? 'is-active' : ''}" aria-hidden="true">${active && this.usageSort.direction === 'asc' ? '↑' : '↓'}</span></button>`}</th>`;
            }).join('')}<th><span class="visually-hidden">操作</span></th></tr></thead><tbody>
            ${pageRows.map((row, index) => {
                const meta = this.usageView === 'task' ? this.getUsageTaskMeta(row.key) : null;
                const expanded = this.usageExpanded === row.key;
                const label = meta?.label || row.name;
                return `<tr class=""><td><button type="button" class="usage-name" data-usage-open="${index}" ${this.usageView === 'task' ? `aria-expanded="${expanded}" ${row.children ? '' : `aria-controls="usageDetail${index}"`}` : ''}><span>${this.escapeHtml(label)}</span><small>${this.escapeHtml(meta ? row.key : kindLabels[row.kind] || '聊天对象')}</small></button></td>
                    <td>${this.usageNumber(row.metrics.successes)}</td><td>${this.usageTokens(row.metrics)}</td><td class="usage-money">${this.usageCosts(row.metrics)}</td><td>${this.usageNumber(row.metrics.failures)}</td>
                    <td><button type="button" class="usage-row-action" data-usage-open="${index}" aria-label="${this.escapeHtml((expanded ? '收起' : '查看') + label)}" ${meta ? `aria-expanded="${expanded}" ${row.children ? '' : `aria-controls="usageDetail${index}"`}` : ''}>${meta ? expanded ? '收起' : row.children ? '展开' : '详情' : '查看'}</button></td></tr>
                    ${meta && !row.children ? `<tr id="usageDetail${index}" class="usage-detail-row" ${expanded ? '' : 'hidden'}><td colspan="6">${expanded ? this.renderUsageDetail(row) : ''}</td></tr>` : ''}`;
            }).join('')}</tbody></table></div>
            ${pages > 1 ? `<div class="usage-pagination"><button type="button" id="usagePrev" ${this.usagePage === 1 ? 'disabled' : ''}>上一页</button><span>${this.usagePage} / ${pages}</span><button type="button" id="usageNext" ${this.usagePage === pages ? 'disabled' : ''}>下一页</button></div>` : ''}`;
        target.querySelectorAll('[data-usage-sort]').forEach(button => button.addEventListener('click', () => {
            const field = button.dataset.usageSort;
            this.usageSort = { field, direction: this.usageSort.field === field && this.usageSort.direction === 'desc' ? 'asc' : 'desc' };
            this.usagePage = 1;
            this.renderUsageTable();
            target.querySelector(`[data-usage-sort="${field}"]`)?.focus();
        }));
        target.querySelectorAll('[data-usage-open]').forEach(button => button.addEventListener('click', () => {
            const row = pageRows[Number(button.dataset.usageOpen)];
            if (this.usageView === 'chat') this.openUsageSubject(row.key);
            else {
                this.usageExpanded = this.usageExpanded === row.key ? null : row.key;
                this.renderUsageTable();
                target.querySelector(`[data-usage-open="${button.dataset.usageOpen}"]`)?.focus();
            }
        }));
        target.querySelector('#usagePrev')?.addEventListener('click', () => { this.usagePage--; this.renderUsageTable(); });
        target.querySelector('#usageNext')?.addEventListener('click', () => { this.usagePage++; this.renderUsageTable(); });
        if (this.usageExpanded && target.querySelector('#usageRequestDetails')) this.loadUsageRequests(this.usageExpanded);
    },

    renderUsageDetail(row) {
        const m = row.metrics;
        const average = m.duration_calls ? `${(m.duration_total / m.duration_calls).toFixed(2)} 秒` : '—';
        const cache = m.cache_input_tokens > 0 ? `${(m.cached_tokens / m.cache_input_tokens * 100).toFixed(1)}%` : '—';
        const models = Object.entries(m.models || {}).sort((a, b) => b[1] - a[1]).map(([name, count]) => `${this.escapeHtml(name)} · ${this.usageNumber(count)} 次`).join('<br>') || '—';
        const facts = [
            [row.key === 'assistant.chat' ? '回复结果' : '请求结果', `成功 ${this.usageNumber(m.successes)} ${row.key === 'assistant.chat' ? '轮' : '次'} · 失败 ${this.usageNumber(m.failures)} 次<small>共 ${this.usageNumber(m.calls)} 次尝试，包含重试与模型回退</small>`],
            ['输入 / 输出 Token', `${m.input_calls ? this.usageNumber(m.input_tokens) : '—'} / ${m.output_calls ? this.usageNumber(m.output_tokens) : '—'}`],
            ['平均耗时', `${average}<small>所选周期内 ${this.usageNumber(m.duration_calls)} 次已记录调用</small>`],
            ['缓存命中率', `${cache}<small>按输入 Token 加权 · ${this.usageNumber(m.cache_calls)} 次有缓存明细</small>`],
            ['缓存写入 / 推理 Token', `${this.usageNumber(m.cache_write_tokens)} / ${this.usageNumber(m.reasoning_tokens)}<small>缓存命中率统计读取量；写入与推理未上报时也会显示 0</small>`],
            ['最近调用', this.usageDate(m.last_call)], ['使用模型', models],
            ['统计完整性', `${this.usageNumber(m.token_calls)} 次有 Token · ${this.usageNumber(m.cost_calls)} 次有计价${m.estimated_calls ? `<br>${this.usageNumber(m.estimated_calls)} 次 Token 为估算` : ''}`],
        ];
        return `<div class="usage-detail-grid">${facts.map(([label, value]) => `<div><small>${label}</small><span>${value}</span></div>`).join('')}</div><div id="usageRequestDetails" class="usage-request-details" aria-live="polite">正在读取计价明细…</div>`;
    },

    async loadUsageRequests(task, offset = 0) {
        const target = document.getElementById('usageRequestDetails');
        const params = new URLSearchParams({period: this.activeStatsType, task, limit: '10', offset: String(offset)});
        if (this.usageSubject) params.set('subject', this.usageSubject);
        try {
            const response = await fetch(`/api/llm/usage/requests?${params}`);
            if (!response.ok) throw new Error('计价明细读取失败');
            const result = await response.json();
            if (!target?.isConnected || this.usageExpanded !== task) return;
            const labels = {api_equivalent: 'API 等值估算', estimated: '估算', reported: '供应商上报', free: '免费', included: '订阅包含', rejected: '额度拒绝 · 不计费', unknown: '未知'};
            const sources = {official_snapshot: '官方价格快照', connection_config: '连接配置', litellm_catalog: 'LiteLLM 价格表', provider_response: '供应商返回', none: '无可用价格', quota_rejection: '额度耗尽，请求被拒绝'};
            const reasons = {quota_rejected: '额度耗尽且未返回用量，费用为零', unsupported_usage_dimension: '该用量包含尚未支持的独立计价项目', missing_usage_or_currency: '用量或币种不完整', missing_price: '缺少适用单价', tier_requires_configuration: '该阶梯价格尚未配置', context_tier_unavailable: '只有整轮用量，无法确定各请求是否进入长上下文计价'};
            const buckets = {uncached_input_tokens: '普通输入', completion_tokens: '输出', cached_tokens: '缓存读取', cache_write_tokens: '缓存写入'};
            target.innerHTML = result.data.rows.map(row => {
                const p = row.pricing || {}, snapshot = p.snapshot || {};
                const amount = p.amount === null || p.amount === undefined ? '—' : `${p.currency || ''} ${Number(p.amount).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
                const rates = Object.entries(snapshot.rates || {}).map(([key, rate]) => `${buckets[key] || key} ${Number(rate) * 1000000}`).join(' / ');
                const sourceUrl = /^https:\/\//.test(snapshot.source_url || '') ? snapshot.source_url : '';
                return `<details><summary>${this.escapeHtml(this.usageDate(row.recorded_at))} · ${this.escapeHtml(row.model)} · ${this.escapeHtml(amount)}</summary><div class="usage-secondary">
                    ${this.escapeHtml(labels[p.status] || '未知')}<br>
                    ${this.escapeHtml(sources[snapshot.source] || snapshot.source || '无可用价格')} · ${this.escapeHtml(snapshot.version || '—')}${sourceUrl ? ` · <a href="${this.escapeHtml(sourceUrl)}" target="_blank" rel="noopener noreferrer">价格来源</a>` : ''}<br>
                    ${rates ? `${this.escapeHtml(rates)} ${this.escapeHtml(p.currency || '')} / 百万 Token<br>` : ''}
                    ${p.reason ? `${this.escapeHtml(reasons[p.reason] || p.reason)}<br>` : ''}${row.usage?.estimated ? 'Token 为本地估算<br>' : ''}${p.notes?.includes('cache_not_reported') ? '未上报缓存明细，输入按普通单价估算<br>' : ''}
                    ${this.escapeHtml(row.scope === 'codex_turn' ? 'Codex 整轮用量' : '逐次请求')} · ${row.success ? '成功' : '失败'} · 输入 ${this.usageNumber(row.usage?.prompt_tokens)} / 输出 ${this.usageNumber(row.usage?.completion_tokens)} / 缓存 ${this.usageNumber(row.usage?.cached_tokens)}<br>
                    请求 ${this.escapeHtml(row.id)}<br>业务调用 ${this.escapeHtml(row.logical_id)}</div></details>`;
            }).join('') || '<span class="usage-secondary">暂无请求明细</span>';
            target.insertAdjacentHTML('afterbegin', `<div class="usage-secondary">${result.data.request_count_exact
                ? `已记录模型调用：${this.usageNumber(result.data.request_count)} 次`
                : `已记录逐次调用：${this.usageNumber(result.data.request_count)} 次；另有 ${this.usageNumber(result.data.summary_count)} 条整轮汇总，实际调用总次数未知`}</div>`);
            target.insertAdjacentHTML('beforeend', `<div class="usage-pagination"><button type="button" data-requests-prev ${offset === 0 ? 'disabled' : ''}>上一页</button><span>${Math.floor(offset / 10) + 1} / ${Math.max(1, Math.ceil(result.data.total / 10))}</span><button type="button" data-requests-next ${offset + 10 >= result.data.total ? 'disabled' : ''}>下一页</button></div>`);
            target.querySelector('[data-requests-prev]').onclick = () => this.loadUsageRequests(task, offset - 10);
            target.querySelector('[data-requests-next]').onclick = () => this.loadUsageRequests(task, offset + 10);
        } catch (error) {
            if (target?.isConnected) target.textContent = error.message;
        }
    },

    /**
     * Refresh methods
     */
    async refreshModels() {
        await this.loadModels();
    },

    async refreshMappings() {
        await this.loadMappings();
    },

    async refreshStats() {
        await this.loadStats();
    },

    /**
     * Show add model modal
     */
    async showAddModelModal() {
        const form = document.getElementById('addModelForm');
        form.reset();
        document.getElementById('modelEditMode').value = 'false';
        document.getElementById('modelId').dataset.oldId = '';
        document.getElementById('addModelModalTitle').textContent = '添加模型';
        document.getElementById('addModelModalSubtitle').textContent = '选择供应商和模型，填写 API Key 即可。';
        document.getElementById('modelTemplateGroup').classList.remove('d-none');
        document.getElementById('modelAdvancedOptions').open = false;
        document.getElementById('modelFormAlert').classList.add('d-none');
        const credentialMode = document.getElementById('modelCredentialMode');
        credentialMode.dataset.canPreserve = 'false';
        credentialMode.dataset.originalMode = '';
        this.populateModelTemplates();
        this.modelIdAutofill = true;
        const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('addModelModal'));
        modal.show();
        this.loadLiteLLMUpdateStatus();
        await this.applyProviderPreset('openai');
        this.updateModelFormReview();
    },

    populateModelTemplates() {
        const select = document.getElementById('modelTemplateSelect');
        if (!select) return;
        select.innerHTML = '<option value="">选择已有配置…</option>' + Object.keys(this.currentModels || {})
            .map(modelId => `<option value="${this.escapeHtml(modelId)}">${this.escapeHtml(modelId)} · ${this.escapeHtml(this.currentModels[modelId]?.model || '')}</option>`)
            .join('');
    },

    getPresetKeyForConfig(config) {
        const provider = this.getModelManagementMeta(config).provider;
        if (['openai', 'anthropic', 'gemini', 'deepseek', 'openrouter', 'local_codex', 'compatible'].includes(provider)) {
            return provider;
        }
        return 'other';
    },

    async cloneModelConfig(modelId) {
        if (!modelId || !this.currentModels[modelId]) return;
        const config = this.currentModels[modelId];
        await this.applyConfigToModelForm(modelId, config, { clone: true });
        let copyId = `${modelId}-copy`;
        let suffix = 2;
        while (this.currentModels[copyId]) copyId = `${modelId}-copy-${suffix++}`;
        document.getElementById('modelId').value = copyId;
        document.getElementById('modelId').dataset.oldId = '';
        this.modelIdAutofill = false;
        this.updateModelFormReview();
    },

    /**
     * Show edit model modal
     */
    async editModel(modelId) {
        const config = this.currentModels[modelId];
        if (!config) return;
        document.getElementById('modelEditMode').value = 'true';
        document.getElementById('modelTemplateGroup').classList.add('d-none');
        document.getElementById('modelFormAlert').classList.add('d-none');
        document.getElementById('addModelModalTitle').textContent = `编辑“${modelId}”`;
        document.getElementById('addModelModalSubtitle').textContent = 'API Key 留空会保持不变。';
        const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('addModelModal'));
        modal.show();
        this.loadLiteLLMUpdateStatus();
        await this.applyConfigToModelForm(modelId, config, { edit: true });
    },

    async applyConfigToModelForm(modelId, config, options = {}) {
        const meta = this.getModelManagementMeta(config);
        const presetKey = this.getPresetKeyForConfig(config);
        if (presetKey === 'other' && !this.catalogProviders.length) await this.loadModelCatalogProviders();
        if (presetKey === 'other') {
            document.getElementById('modelOtherProvider').value = meta.provider;
        }
        await this.applyProviderPreset(presetKey, { preserveValues: true, skipCatalog: true });
        if (presetKey === 'other') {
            document.getElementById('modelOtherProvider').value = meta.provider;
            await this.selectOtherProvider(meta.provider, { preserveValues: true, skipCatalog: true });
        }

        document.getElementById('modelId').value = modelId;
        document.getElementById('modelId').dataset.oldId = options.edit ? modelId : '';
        document.getElementById('modelName').value = config.model || '';
        document.getElementById('modelProvider').value = config.custom_llm_provider || config.provider || this.getActiveProviderPreset().providerValue || '';
        document.getElementById('modelApiBase').value = config.api_base || '';
        document.getElementById('modelTemp').value = config.temperature ?? 0.7;
        document.getElementById('modelMaxTokens').value = config.max_tokens ?? '';
        document.getElementById('modelContextWindow').value = config.context_window_tokens ?? config.max_input_tokens ?? '';
        document.getElementById('modelTimeout').value = config.timeout ?? '';
        document.getElementById('modelMaxRetries').value = config.max_retries ?? '';
        document.getElementById('modelBillingMode').value = config.billing_mode || 'auto';
        const currencySelect = document.getElementById('modelCostCurrency');
        const currency = config.cost_currency || 'USD';
        if (![...currencySelect.options].some(option => option.value === currency)) currencySelect.add(new Option(currency, currency));
        currencySelect.value = currency;
        for (const [id, key] of [['modelInputPrice', 'input_cost_per_token'], ['modelOutputPrice', 'output_cost_per_token'], ['modelCacheReadPrice', 'cache_read_input_token_cost'], ['modelCacheWritePrice', 'cache_creation_input_token_cost']]) {
            document.getElementById(id).value = config[key] == null ? '' : config[key] * 1000000;
        }
        document.getElementById('modelVision').checked = Boolean(config.supports_vision || config.vision || config.image_input);
        document.getElementById('modelWebSearch').checked = Boolean(config.enable_web_search || config.codex_web_search);
        document.getElementById('modelCodexReasoning').value = config.codex_reasoning_effort || 'medium';
        document.getElementById('modelCodexIsolated').checked = config.codex_isolated_workdir !== false;
        document.getElementById('modelExtraBody').value = config.extra_body ? JSON.stringify(config.extra_body, null, 2) : '';
        document.getElementById('modelApiKeyEnv').value = meta.credential?.environment_variable || this.getActiveProviderPreset().envVar || '';
        document.getElementById('modelApiKeyEnvValue').value = '';

        const credentialMode = document.getElementById('modelCredentialMode');
        const canPreserve = Boolean(options.edit && meta.credential?.configured);
        credentialMode.dataset.canPreserve = canPreserve ? 'true' : 'false';
        credentialMode.dataset.originalMode = meta.credential?.mode || '';
        if (canPreserve) {
            credentialMode.value = 'preserve';
        } else if (meta.credential?.mode === 'environment') {
            credentialMode.value = 'environment';
        } else if (meta.credential?.mode === 'none' || presetKey === 'local_codex') {
            credentialMode.value = 'none';
        } else {
            credentialMode.value = this.getActiveProviderPreset().requiresCredential ? 'environment' : 'none';
        }
        this.modelIdAutofill = !options.edit && !options.clone;
        this.updateCredentialFields();
        const discoveryProvider = presetKey === 'other'
            ? meta.provider
            : (this.providerPresets[presetKey]?.catalogProvider || (presetKey === 'compatible' ? 'compatible' : ''));
        if ((options.edit || options.clone) && discoveryProvider) {
            await this.loadModelCatalog(discoveryProvider, { modelId });
        }
        this.updateModelFormReview();
    },

    parseOptionalNumber(elementId, label, minimum, maximum = null) {
        const raw = document.getElementById(elementId)?.value.trim() || '';
        if (!raw) return { value: null, empty: true };
        const value = Number(raw);
        if (!Number.isFinite(value) || value < minimum || (maximum !== null && value > maximum)) {
            const range = maximum === null ? `不能小于 ${minimum}` : `必须在 ${minimum}–${maximum} 之间`;
            throw new Error(`${label}${range}`);
        }
        return { value, empty: false };
    },

    validateModelForm(options = {}) {
        const showErrors = options.showErrors !== false;
        const provider = this.getActiveProviderPreset();
        const modelIdInput = document.getElementById('modelId');
        const modelNameInput = document.getElementById('modelName');
        const apiBaseInput = document.getElementById('modelApiBase');
        const envVarInput = document.getElementById('modelApiKeyEnv');
        const isEdit = document.getElementById('modelEditMode').value === 'true';
        const storedEnvVar = this.sanitizeCredentialName(envVarInput.value);
        const envValue = document.getElementById('modelApiKeyEnvValue').value.trim();
        const modelId = this.sanitizeModelId(modelIdInput.value);
        const envVar = envValue ? this.buildModelCredentialName(modelId) : storedEnvVar;
        const values = {
            provider,
            isEdit,
            modelId,
            modelName: this.normalizePastedText(modelNameInput.value).trim(),
            apiBase: this.normalizePastedText(apiBaseInput.value).trim(),
            credentialMode: this.getEffectiveCredentialMode(provider, isEdit, envVar, envValue),
            envVar,
            envValue,
            providerValue: document.getElementById('modelProvider').value.trim() || provider.providerValue || '',
        };
        modelIdInput.value = values.modelId;
        modelNameInput.value = values.modelName;
        apiBaseInput.value = values.apiBase;
        envVarInput.value = values.envVar;
        try {
            if (provider.key === 'other' && !provider.catalogProvider) throw new Error('请选择一个 LiteLLM 供应商。');
            if (!values.modelId) throw new Error('请填写配置名称。');
            if (values.modelId.length > 80 || /[\\/?#\x00-\x1f\x7f]/.test(values.modelId)) {
                throw new Error('配置名称不能包含 /、\\、?、# 或控制字符，且不能超过 80 个字符。');
            }
            if (!values.modelName) throw new Error('请选择或输入服务端模型。');
            if (values.apiBase && !/^(https?:\/\/|env::[A-Za-z_][A-Za-z0-9_]*$)/i.test(values.apiBase)) {
                throw new Error('API 地址必须以 http://、https:// 开头，或使用 env::VAR_NAME。');
            }
            if (provider.apiBaseRequired && !values.apiBase) throw new Error('自定义 OpenAI 兼容接口必须填写 API 地址。');
            if (values.credentialMode === 'environment' && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(values.envVar)) {
                throw new Error('当前供应商缺少有效的 API Key 保存位置，请重新选择供应商。');
            }
            if (values.credentialMode === 'preserve' && !values.isEdit) throw new Error('新增模型时不能保留旧凭据。');
            if (values.credentialMode === 'none' && provider.requiresCredential) {
                throw new Error(`${provider.label} 需要 API Key，请填写后再保存。`);
            }
            const isCodex = provider.key === 'local_codex';
            values.samplingDisabled = isCodex || this.isGemini3FormSelection(provider, values.modelName);
            values.temperature = values.samplingDisabled
                ? { value: null, empty: true }
                : this.parseOptionalNumber('modelTemp', '温度', 0, 2);
            values.maxTokens = isCodex ? { value: null, empty: true } : this.parseOptionalNumber('modelMaxTokens', '最大输出 Token', 1);
            values.contextWindow = this.parseOptionalNumber('modelContextWindow', '上下文窗口', 1);
            values.timeout = this.parseOptionalNumber('modelTimeout', '超时', 1, 3600);
            values.maxRetries = isCodex ? { value: null, empty: true } : this.parseOptionalNumber('modelMaxRetries', 'SDK 重试次数', 0, 20);
            values.prices = [['modelInputPrice', 'input_cost_per_token'], ['modelOutputPrice', 'output_cost_per_token'], ['modelCacheReadPrice', 'cache_read_input_token_cost'], ['modelCacheWritePrice', 'cache_creation_input_token_cost']].map(([id, key]) => [key, isCodex ? {empty: true} : this.parseOptionalNumber(id, '单价', 0)]);
            if (values.prices.some(([, price]) => !price.empty) && (values.prices[0][1].empty || values.prices[1][1].empty)) throw new Error('自定义计价请同时填写输入和输出单价。');
            const extraBody = document.getElementById('modelExtraBody').value.trim();
            values.extraBody = extraBody ? JSON.parse(extraBody) : {};
            if (!values.extraBody || Array.isArray(values.extraBody) || typeof values.extraBody !== 'object') {
                throw new Error('附加请求体必须是 JSON 对象。');
            }
            if (values.samplingDisabled) {
                values.extraBody = this.stripGeminiSamplingParameters(values.extraBody);
            }
            this.showModelFormError('');
            return values;
        } catch (error) {
            if (error instanceof SyntaxError) error = new Error(`附加请求体不是有效 JSON：${error.message}`);
            if (showErrors) this.showModelFormError(error.message);
            return null;
        }
    },

    showModelFormError(message) {
        const alert = document.getElementById('modelFormAlert');
        if (!alert) return;
        alert.textContent = message || '';
        alert.classList.toggle('d-none', !message);
        if (message) alert.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    },

    setModelSaveBusy(busy) {
        for (const id of ['saveModelButton', 'saveAndTestModelButton']) {
            const button = document.getElementById(id);
            if (!button) continue;
            button.disabled = busy;
            if (!button.dataset.label) button.dataset.label = button.innerHTML;
            button.innerHTML = busy ? '<span class="spinner-border spinner-border-sm me-1"></span>正在保存' : button.dataset.label;
        }
    },

    /**
     * Save Model (Add or Edit)
     */
    async saveModel(options = {}) {
        const values = this.validateModelForm();
        if (!values) return;
        const { isEdit } = values;
        const modelIdInput = document.getElementById('modelId');
        const newModelId = values.modelId;
        const oldModelId = modelIdInput.dataset.oldId || newModelId;
        const targetModelId = isEdit ? oldModelId : newModelId;
        const payload = {
            model: values.modelName,
            api_base: values.apiBase,
            custom_llm_provider: values.providerValue,
            supports_vision: document.getElementById('modelVision').checked,
            extra_body: values.extraBody,
        };
        const numericFields = [
            ['temperature', values.temperature],
            ['max_tokens', values.maxTokens],
            ['context_window_tokens', values.contextWindow],
            ['timeout', values.timeout],
            ['max_retries', values.maxRetries],
        ];
        payload.clear_fields = [];
        payload.billing_mode = values.provider.key === 'local_codex' ? 'auto' : document.getElementById('modelBillingMode').value;
        payload.cost_currency = document.getElementById('modelCostCurrency').value;
        for (const [key, parsed] of values.prices) {
            if (!parsed.empty) payload[key] = parsed.value / 1000000;
            else if (isEdit && this.currentModels[targetModelId]?.[key] !== undefined) payload.clear_fields.push(key);
        }
        for (const [key, parsed] of numericFields) {
            if (!parsed.empty) payload[key] = parsed.value;
            else if (isEdit && this.currentModels[targetModelId]?.[key] !== undefined) payload.clear_fields.push(key);
        }
        if (values.contextWindow.empty && isEdit && this.currentModels[targetModelId]?.max_input_tokens !== undefined) {
            payload.clear_fields.push('max_input_tokens');
        }
        if (!Object.keys(values.extraBody).length && isEdit) payload.clear_fields.push('extra_body');
        if (values.credentialMode === 'environment') {
            payload.api_key = `env::${values.envVar}`;
            payload.credential_mode = 'environment';
        }
        if (values.credentialMode === 'none') {
            payload.api_key = '';
            payload.credential_mode = 'none';
        }
        const webSearch = document.getElementById('modelWebSearch').checked;
        if (values.provider.key === 'local_codex') {
            payload.enable_web_search = false;
            payload.codex_web_search = webSearch;
            payload.codex_reasoning_effort = document.getElementById('modelCodexReasoning').value;
            if (isEdit && this.currentModels[targetModelId]?.codex_profile_id) payload.clear_fields.push('codex_profile_id');
            payload.codex_isolated_workdir = document.getElementById('modelCodexIsolated').checked;
        } else {
            payload.enable_web_search = webSearch;
        }

        try {
            this.setModelSaveBusy(true);
            if (values.credentialMode === 'environment') {
                await this.ensureSharedCredential(values.envVar, values.envValue);
            }
            let url = `/api/llm/models/${encodeURIComponent(targetModelId)}`;
            if (isEdit && oldModelId !== newModelId) {
                url += `?new_id=${encodeURIComponent(newModelId)}`;
            }
            const method = isEdit ? 'PUT' : 'POST';

            const response = await fetch(url, {
                method: method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            const result = await response.json().catch(() => ({}));

            if (response.ok && result.status === 'success') {
                const modalEl = document.getElementById('addModelModal');
                const modal = bootstrap.Modal.getInstance(modalEl);
                if (modal) modal.hide();

                UI.showSuccess(`模型“${newModelId}”已保存`);
                document.getElementById('modelApiKeyEnvValue').value = '';
                await this.loadModels();
                await this.loadMappings();
                if (options.testAfterSave) {
                    window.setTimeout(() => this.testModelConnectivity(newModelId), 150);
                }
            } else {
                const detail = Array.isArray(result.detail)
                    ? result.detail.map(item => item.msg || String(item)).join('；')
                    : result.detail;
                throw new Error(detail || result.message || `保存模型失败（HTTP ${response.status}）`);
            }
        } catch (error) {
            console.error('Save model error:', error);
            this.showModelFormError(`保存失败：${error.message}`);
        } finally {
            this.setModelSaveBusy(false);
        }
    },

    /**
     * Delete model
     */
    async deleteModel(modelId) {
        const mappingCount = this.getModelManagementMeta(this.currentModels[modelId] || {}).mapping_count || 0;
        const usageWarning = mappingCount ? `\n该模型仍被 ${mappingCount} 个任务引用，删除后这些任务将无法正常调用。` : '';
        if (!await UI.confirm(`确定要删除模型“${modelId}”吗？${usageWarning}`, {
            title: '删除模型',
            confirmText: '删除',
            variant: 'danger'
        })) {
            return;
        }

        try {
            const response = await fetch(`/api/llm/models/${encodeURIComponent(modelId)}`, {
                method: 'DELETE'
            });
            const result = await response.json();

            if (result.status === 'success') {
                UI.showSuccess(`模型“${modelId}”已删除`);
                await this.loadModels();
            } else {
                throw new Error(result.message || '删除失败');
            }
        } catch (error) {
            console.error('Failed to delete model:', error);
            UI.showError(`删除模型失败：${error.message}`);
        }
    },

    renderModelTestResult(modelId, data) {
        const domModelId = this.getModelDomId(modelId);
        const resultDiv = document.getElementById(`modelTestResult_${domModelId}`);
        if (!resultDiv) return;

        const statusClass = data.ok ? 'alert-success' : 'alert-danger';
        const statusIcon = data.ok ? 'bi-check-circle' : 'bi-exclamation-triangle';
        const latencyText = data.latency_ms !== undefined && data.latency_ms !== null
            ? `${data.latency_ms} 毫秒`
            : 'N/A';
        const configuredModel = this.escapeHtml(data.configured_model || '');
        const actualModel = data.actual_model
            ? `<div><strong>实际模型：</strong><code>${this.escapeHtml(data.actual_model)}</code></div>`
            : '';
        const message = this.escapeHtml(data.message || '');

        resultDiv.style.display = 'block';
        resultDiv.innerHTML = `
            <div class="alert ${statusClass} py-2 px-3 mb-0 small">
                <div class="d-flex justify-content-between align-items-center mb-1">
                    <span><i class="bi ${statusIcon} me-1"></i>${data.ok ? '连通成功' : '连通失败'}</span>
                    <span class="badge ${data.ok ? 'bg-success' : 'bg-danger'}">${latencyText}</span>
                </div>
                <div><strong>配置模型：</strong><code>${configuredModel}</code></div>
                ${actualModel}
                <div class="mt-1 text-break">${message}</div>
            </div>
        `;
    },

    async testModelConnectivity(modelId) {
        const domModelId = this.getModelDomId(modelId);
        const btn = document.getElementById(`testModelBtn_${domModelId}`);
        const resultDiv = document.getElementById(`modelTestResult_${domModelId}`);

        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
        }
        if (resultDiv) {
            resultDiv.style.display = 'block';
            resultDiv.innerHTML = '<div class="text-muted small"><span class="spinner-border spinner-border-sm me-1"></span>正在测试…</div>';
        }

        try {
            const response = await fetch(`/api/llm/models/${encodeURIComponent(modelId)}/test`, {
                method: 'POST'
            });
            const result = await response.json();

            if (!response.ok) {
                throw new Error(result.detail || result.message || '服务器错误');
            }

            if (result.status !== 'success' || !result.data) {
                throw new Error(result.message || '测试失败');
            }

            this.renderModelTestResult(modelId, result.data);
        } catch (error) {
            console.error(`Failed to test model ${modelId}:`, error);
            this.renderModelTestResult(modelId, {
                ok: false,
                latency_ms: null,
                configured_model: this.currentModels[modelId]?.model || modelId,
                actual_model: null,
                message: error.message || '未知错误'
            });
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '<i class="bi bi-lightning-charge me-1"></i>测试';
            }
        }
    },

    /**
     * Edit mapping - opens modal with current mapping data
     */
    async editMapping(ownerId, callType) {
        try {
            const mapping = this.currentMappings[ownerId]?.[callType];
            if (!mapping) {
                UI.showError('未找到映射');
                return;
            }
            const owner = this.getRouteOwnerMeta(ownerId);
            const task = this.getTaskRouteMeta(ownerId, callType);

            document.getElementById('mappingOwnerId').value = ownerId;
            document.getElementById('mappingCallType').value = callType;
            document.getElementById('mappingFullName').value = `${owner.displayName} · ${task.label}`;
            document.getElementById('mappingRouteKey').textContent = `${ownerId}.${callType}`;
            document.getElementById('editMappingModalTitle').textContent = `配置模型路由 · ${task.label}`;

            const availableModelIds = Object.keys(this.currentModels);
            [mapping.primary, ...(mapping.fallback || [])].forEach(modelId => {
                if (modelId && !availableModelIds.includes(modelId)) availableModelIds.push(modelId);
            });
            const primarySelect = document.getElementById('mappingPrimary');
            primarySelect.innerHTML = '<option value="">选择模型…</option>';
            for (const modelId of availableModelIds) {
                const option = document.createElement('option');
                option.value = modelId;
                option.textContent = this.currentModels[modelId]
                    ? this.formatRouteModelOption(modelId)
                    : `${modelId} · 当前模型连接中不存在`;
                if (modelId === mapping.primary) {
                    option.selected = true;
                }
                primarySelect.appendChild(option);
            }

            const fallbackSelect = document.getElementById('mappingFallback');
            fallbackSelect.innerHTML = '<option value="">无（不使用备用模型）</option>';
            for (const modelId of availableModelIds) {
                const option = document.createElement('option');
                option.value = modelId;
                option.textContent = this.currentModels[modelId]
                    ? this.formatRouteModelOption(modelId)
                    : `${modelId} · 当前模型连接中不存在`;
                if (mapping.fallback && mapping.fallback.length > 0 && modelId === mapping.fallback[0]) {
                    option.selected = true;
                }
                fallbackSelect.appendChild(option);
            }

            // Populate override parameters
            const overrideParams = {...(mapping.override_params || {})};
            document.getElementById('mappingOverrideTemp').value = overrideParams.temperature !== undefined ? overrideParams.temperature : '';
            document.getElementById('mappingOverrideMaxTokens').value = overrideParams.max_tokens !== undefined ? overrideParams.max_tokens : '';
            document.getElementById('mappingOverrideTimeout').value = overrideParams.timeout !== undefined ? overrideParams.timeout : '';

            delete overrideParams.temperature;
            delete overrideParams.max_tokens;
            delete overrideParams.timeout;
            document.getElementById('mappingOverrideExtra').value = Object.keys(overrideParams).length > 0 ? JSON.stringify(overrideParams, null, 2) : '';
            this.updateMappingSamplingControls();

            bootstrap.Modal.getOrCreateInstance(document.getElementById('editMappingModal')).show();
        } catch (error) {
            console.error('Failed to open edit mapping modal:', error);
            UI.showError(`打开编辑弹窗失败：${error.message}`);
        }
    },

    /**
     * Save mapping edit
     */
    async saveMappingEdit() {
        try {
            const ownerId = document.getElementById('mappingOwnerId').value;
            const callType = document.getElementById('mappingCallType').value;
            const primary = document.getElementById('mappingPrimary').value;
            const fallbackModel = document.getElementById('mappingFallback').value.trim();
            const overrideTemp = document.getElementById('mappingOverrideTemp').value;
            const overrideMaxTokens = document.getElementById('mappingOverrideMaxTokens').value;
            const overrideTimeout = document.getElementById('mappingOverrideTimeout').value;
            const overrideExtraStr = document.getElementById('mappingOverrideExtra').value.trim();

            if (!primary) {
                UI.showError('请选择主模型');
                return;
            }

            // Build mapping object
            const mapping = {
                primary: primary,
                fallback: fallbackModel ? [fallbackModel] : [],
                override_params: {}
            };

            // Add override params if specified by the user
            if (overrideTemp !== '') {
                mapping.override_params.temperature = parseFloat(overrideTemp);
            }
            if (overrideMaxTokens !== '') {
                mapping.override_params.max_tokens = parseInt(overrideMaxTokens);
            }
            if (overrideTimeout !== '') {
                mapping.override_params.timeout = parseInt(overrideTimeout);
            }

            // Parse and merge extra JSON parameters
            if (overrideExtraStr) {
                try {
                    const extraData = JSON.parse(overrideExtraStr);
                    Object.assign(mapping.override_params, extraData);
                } catch (e) {
                    UI.showError('附加参数中的 JSON 无效：' + e.message);
                    return;
                }
            }
            if (this.isGemini3ModelConfig(this.currentModels[primary] || {})) {
                mapping.override_params = this.stripGeminiSamplingParameters(mapping.override_params);
            }

            // Save via API
            const response = await fetch(`/api/llm/mappings/${ownerId}/${callType}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(mapping)
            });

            const result = await response.json();

            if (result.status === 'success') {
                // Close modal
                const modalEl = document.getElementById('editMappingModal');
                const modal = bootstrap.Modal.getInstance(modalEl);
                if (modal) modal.hide();

                const task = this.getTaskRouteMeta(ownerId, callType);
                UI.showSuccess(`“${task.label}”的模型路由已更新`);
                await this.loadMappings();
            } else {
                throw new Error(result.message || '保存映射失败');
            }
        } catch (error) {
            console.error('Failed to save mapping:', error);
            UI.showError(`保存映射失败：${error.message}`);
        }
    },

    /**
     * Load proxy settings from server
     */
    async loadProxy() {
        try {
            const response = await fetch('/api/llm/proxy');
            const result = await response.json();
            if (result.status === 'success') {
                const { proxy_url, display_url, enabled, sensitive } = result.data;
                const input = document.getElementById('proxyUrlInput');
                const badge = document.getElementById('proxyStatusBadge');
                if (input) {
                    input.value = proxy_url || '';
                    input.placeholder = sensitive ? '代理已配置（含凭据）；留空可直接测试，保存空值会禁用' : 'http://host:port';
                }
                if (badge) {
                    badge.textContent = enabled ? `已启用：${display_url || '已配置'}` : '已禁用';
                    badge.className = `badge rounded-pill fs-6 ${enabled ? 'bg-success' : 'bg-secondary'}`;
                }
            }
        } catch (error) {
            console.error('Failed to load proxy settings:', error);
        }
    },

    /**
     * Save proxy settings to server
     */
    async saveProxy() {
        const input = document.getElementById('proxyUrlInput');
        const badge = document.getElementById('proxyStatusBadge');
        const proxy_url = (input ? input.value : '').trim();

        // Basic URL validation
        if (proxy_url && !proxy_url.match(/^https?:\/\/.+:\d+\/?/)) {
            UI.showError('格式错误，请输入合法的代理地址，例如：http://100.x.x.x:6688');
            return;
        }

        try {
            const response = await fetch('/api/llm/proxy', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ proxy_url })
            });
            const result = await response.json();

            if (result.status === 'success') {
                const { enabled, display_url, proxy_url: publicUrl, sensitive } = result.data;
                if (input) {
                    input.value = publicUrl || '';
                    input.placeholder = sensitive ? '代理已配置（含凭据）；留空可直接测试，保存空值会禁用' : 'http://host:port';
                }
                if (badge) {
                    badge.textContent = enabled ? `已启用：${display_url || '已配置'}` : '已禁用';
                    badge.className = `badge rounded-pill fs-6 ${enabled ? 'bg-success' : 'bg-secondary'}`;
                }
                UI.showSuccess(enabled
                    ? `代理已保存：${display_url || '已配置'}`
                    : '代理已禁用（直连模式）');
            } else {
                throw new Error(result.message || '保存失败');
            }
        } catch (error) {
            console.error('Failed to save proxy settings:', error);
            UI.showError(`保存失败：${error.message}`);
        }
    },

    /**
     * Test proxy and direct connectivity
     */
    async testProxy() {
        const proxyInput = document.getElementById('proxyUrlInput');
        const testUrlInput = document.getElementById('proxyTestUrlInput');
        const resultDiv = document.getElementById('proxyTestResult');
        const btn = document.getElementById('proxyTestBtn');

        const proxy_url = proxyInput ? proxyInput.value.trim() : '';
        const test_url = testUrlInput ? testUrlInput.value.trim() : '';

        // Show loading state
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> 正在测试…'; }
        if (resultDiv) { resultDiv.style.display = 'block'; resultDiv.innerHTML = '<div class="text-muted small"><span class="spinner-border spinner-border-sm me-1"></span>正在测试连通性，请稍候…</div>'; }

        try {
            const resp = await fetch('/api/llm/proxy/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ proxy_url, test_url })
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || '服务器错误');

            const { results, test_url: tested_url, proxy_url: used_proxy } = data;

            const renderRow = (label, icon, r) => {
                const color = r.ok ? 'success' : 'danger';
                const badgeText = r.ok ? `${r.latency_ms} 毫秒` : '失败';
                const badgeColor = r.ok ? 'bg-success' : 'bg-danger';
                const detail = r.ok && r.status_code
                    ? `HTTP ${r.status_code}（${r.latency_ms} 毫秒）`
                    : r.message;
                return `
                    <tr>
                        <td><i class="bi bi-${icon} me-1 text-secondary"></i>${this.escapeHtml(label)}</td>
                        <td><span class="badge ${badgeColor}">${badgeText}</span></td>
                        <td class="text-muted small">${this.escapeHtml(detail)}</td>
                    </tr>`;
            };

            let rows = renderRow('直连', 'arrow-right-circle', results.direct);
            if (results.proxy) {
                rows += renderRow(`代理 (${used_proxy || '已配置'})`, 'hdd-network', results.proxy);
            } else if (!used_proxy) {
                rows += `<tr><td colspan="3" class="text-muted small"><i class="bi bi-info-circle me-1"></i>未配置代理，仅测试直连</td></tr>`;
            }

            resultDiv.innerHTML = `
                <div class="card border-0 bg-light">
                    <div class="card-body p-3">
                        <div class="small text-muted mb-2">
                            <i class="bi bi-link-45deg me-1"></i>
                            测试目标：<code>${this.escapeHtml(tested_url)}</code>
                        </div>
                        <table class="table table-sm mb-0">
                            <thead><tr>
                                <th>通道</th><th>延迟</th><th>详情</th>
                            </tr></thead>
                            <tbody>${rows}</tbody>
                        </table>
                    </div>
                </div>`;
        } catch (err) {
            console.error('Proxy test failed:', err);
            if (resultDiv) resultDiv.innerHTML = `<div class="alert alert-danger py-2 small"><i class="bi bi-exclamation-triangle me-1"></i>${this.escapeHtml(err.message)}</div>`;
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-wifi me-1"></i> 测试连接'; }
        }
    },

    // ========== Call History ==========

    currentHistorySource: null,
    currentHistoryEntries: [],
    currentHistoryPaging: null,
    callHistoryAbortController: null,

    async loadCallHistory() {
        const sourceList = document.getElementById('callHistorySourceList');
        if (!sourceList) return;

        sourceList.innerHTML = '<div class="text-center text-muted py-4 small"><div class="spinner-border spinner-border-sm me-2"></div>加载中…</div>';

        try {
            const [resp] = await Promise.all([fetch('/api/llm/call-history/summary'), this.loadRouteOwners()]);
            const data = await resp.json();
            if (data.status !== 'success' || !data.data) {
                sourceList.innerHTML = '<div class="text-center text-muted py-4 small">暂无调用记录</div>';
                return;
            }

            const summaries = data.data;
            if (!summaries.length) {
                sourceList.innerHTML = '<div class="text-center text-muted py-4 small">暂无调用记录</div>';
                return;
            }

            // Group by plugin_name
            const grouped = {};
            summaries.forEach(s => {
                if (!grouped[s.plugin_name]) grouped[s.plugin_name] = [];
                grouped[s.plugin_name].push(s);
            });

            let html = '';
            for (const [plugin, items] of Object.entries(grouped)) {
                html += `<div class="list-group-item bg-light border-0 py-1 px-3">
                    <small class="text-muted fw-semibold" style="font-size:0.7rem;">${this.escapeHtml(this.currentRouteOwners[plugin]?.display_name || plugin)}</small>
                </div>`;
                items.forEach(item => {
                    const taskMeta = this.getUsageTaskMeta(item.key);
                    const isActive = (this.currentHistorySource === item.key) ? 'active' : '';
                    const statusIcon = item.last_success
                        ? '<i class="bi bi-check-circle-fill text-success"></i>'
                        : '<i class="bi bi-x-circle-fill text-danger"></i>';
                    const timeDisplay = item.last_call ? this.escapeHtml(String(item.last_call).substring(11, 19)) : '';
                    html += `<a href="#" class="list-group-item list-group-item-action border-0 py-2 px-3 ${isActive}"
                        data-history-key="${this.escapeHtml(item.key)}">
                        <div class="d-flex justify-content-between align-items-center">
                            <div>
                                <span class="me-1">${statusIcon}</span>
                                <span class="small fw-medium">${this.escapeHtml(taskMeta.label)}</span>
                                <span class="badge bg-secondary-subtle text-secondary ms-1" style="font-size:0.6rem;">${Number(item.count || 0)}条</span>
                            </div>
                            <small class="text-muted" style="font-size:0.65rem;">${timeDisplay}</small>
                        </div>
                        <small class="call-source-key">${this.escapeHtml(item.key)}</small>
                    </a>`;
                });
            }
            sourceList.innerHTML = html;
            sourceList.querySelectorAll('[data-history-key]').forEach(link => {
                link.addEventListener('click', event => {
                    event.preventDefault();
                    this.showCallHistoryDetail(link.dataset.historyKey);
                });
            });

            // If current source still exists, refresh detail
            if (this.currentHistorySource) {
                this.showCallHistoryDetail(this.currentHistorySource);
            }
        } catch (e) {
            console.error('Failed to load call history:', e);
            sourceList.innerHTML = '<div class="text-center text-danger py-4 small">加载失败</div>';
        }
    },

    async showCallHistoryDetail(key) {
        this.currentHistorySource = key;
        const detail = document.getElementById('callHistoryDetail');
        if (!detail) return;

        // Highlight active in list
        document.querySelectorAll('#callHistorySourceList .list-group-item-action').forEach(el => {
            el.classList.remove('active');
        });
        const activeLink = Array.from(document.querySelectorAll('#callHistorySourceList [data-history-key]'))
            .find(link => link.dataset.historyKey === key);
        if (activeLink) activeLink.classList.add('active');

        detail.innerHTML = '<div class="text-center py-4"><div class="spinner-border spinner-border-sm text-primary"></div></div>';

        try {
            const parts = key.split('.');
            const plugin = parts[0];
            const callType = parts.slice(1).join('.');
            if (this.callHistoryAbortController) this.callHistoryAbortController.abort();
            const controller = new AbortController();
            this.callHistoryAbortController = controller;
            const resp = await fetch(`/api/llm/call-history?plugin_name=${encodeURIComponent(plugin)}&call_type=${encodeURIComponent(callType)}&limit=10&offset=0`, {
                signal: controller.signal
            });
            if (this.callHistoryAbortController !== controller) return;
            this.callHistoryAbortController = null;
            const data = await resp.json();

            if (data.status !== 'success' || !data.data || !Array.isArray(data.data.entries)) {
                detail.innerHTML = '<div class="text-center text-muted py-4">暂无记录</div>';
                return;
            }

            const entries = data.data.entries;
            this.currentHistoryEntries = entries;
            this.currentHistoryPaging = {
                plugin,
                callType,
                key,
                total: data.data.total || entries.length,
                limit: data.data.limit || 10,
                offset: data.data.offset || 0,
            };

            let html = '';
            entries.forEach((entry, idx) => {
                const time = entry.timestamp ? this.escapeHtml(String(entry.timestamp).replace('T', ' ').substring(0, 19)) : '';
                const statusBadge = entry.success
                    ? '<span class="badge bg-success-subtle text-success">成功</span>'
                    : '<span class="badge bg-danger-subtle text-danger">失败</span>';
                const modelBadge = `<span class="badge bg-secondary-subtle text-secondary ms-1">${this.escapeHtml(entry.actual_model || entry.model_id || '未知')}</span>`;

                const hasReasoning = !!entry.has_reasoning;
                const messageCount = entry.message_count || 0;
                const responseSize = entry.response_size || 0;
                const attachmentCount = Number(entry.attachment_count || 0);
                const imageCount = Number(entry.image_count || 0);
                const fileCount = Number(entry.file_count || 0);
                const responseSummary = [
                    `${responseSize} 个字符`,
                    ...(imageCount ? [`${imageCount} 张图片`] : []),
                    ...(fileCount ? [`${fileCount} 个文件`] : []),
                ].join(' · ');
                const reasoningSize = entry.reasoning_size || 0;
                const tokenUsageHtml = this.renderTokenUsage(entry);
                const chatContext = entry.chat_name
                    ? `<span class="badge bg-light text-dark border fw-normal">
                        <i class="bi bi-chat-dots me-1"></i>${this.escapeHtml(entry.chat_name)}
                        ${entry.role_name ? ` · ${this.escapeHtml(entry.role_name)}` : ''}
                       </span>`
                    : '';

                html += `<div class="card border-0 shadow-sm mb-3">
                    <div class="card-body p-3">
                        <div class="d-flex flex-wrap justify-content-between align-items-start gap-2 mb-2">
                            <div class="d-flex align-items-center gap-2 flex-wrap">
                                ${statusBadge}
                                ${modelBadge}
                                <span class="text-muted small"><i class="bi bi-calendar2-event me-1"></i>${time}</span>
                            </div>
                            <div class="d-flex flex-wrap justify-content-end gap-2">
                                <button class="btn btn-sm btn-outline-secondary py-0 px-2" style="font-size:0.75rem;"
                                    onclick="LLMManager.toggleCallHistoryBody(${idx}, 'req')">
                                    <i class="bi bi-box-arrow-in-down me-1"></i>请求
                                </button>
                                ${hasReasoning ? `<button class="btn btn-sm btn-outline-secondary py-0 px-2" style="font-size:0.75rem;"
                                    onclick="LLMManager.toggleCallHistoryBody(${idx}, 'reasoning')">
                                    <i class="bi bi-lightbulb me-1"></i>思维
                                </button>` : ''}
                                <button class="btn btn-sm btn-outline-secondary py-0 px-2" style="font-size:0.75rem;"
                                    onclick="LLMManager.toggleCallHistoryBody(${idx}, 'resp')">
                                    <i class="bi bi-box-arrow-up me-1"></i>响应${attachmentCount ? ` · ${attachmentCount} 附件` : ''}
                                </button>

                            </div>
                        </div>
                        <div class="d-flex flex-wrap align-items-center gap-2 mt-2 pt-2 border-top">
                            <span class="badge bg-light text-secondary border fw-normal"><i class="bi bi-stopwatch me-1"></i>${Number(entry.response_time || 0).toFixed(1)} 秒</span>
                            ${tokenUsageHtml}
                            ${chatContext}
                        </div>
                    </div>
                    <div class="history-req-body d-none" data-history-index="${idx}" data-history-kind="req">
                        <div class="px-3 pt-2">
                            <small class="text-muted fw-semibold">📥 请求（${messageCount} 条消息）</small>
                        </div>
                        <pre class="m-2 p-2 bg-light rounded small" style="max-height:250px; font-size:0.75rem; overflow:auto;"></pre>
                    </div>
                    ${hasReasoning ? `<div class="history-reasoning-body d-none" data-history-index="${idx}" data-history-kind="reasoning">
                        <div class="px-3 pt-2">
                            <small class="text-muted fw-semibold"><i class="bi bi-lightbulb me-1"></i>思维链（${reasoningSize} 个字符）</small>
                        </div>
                        <div class="m-2 p-2 bg-warning-subtle rounded small" style="max-height:250px; font-size:0.8rem; overflow:auto; white-space:pre-wrap;"></div>
                    </div>` : ''}
                    <div class="history-resp-body d-none" data-history-index="${idx}" data-history-kind="resp">
                        <div class="px-3 pt-2">
                            <small class="text-muted fw-semibold">📤 响应（${responseSummary}）</small>
                        </div>
                        <div class="history-response-text m-2 p-2 bg-light rounded small" style="max-height:250px; font-size:0.8rem; overflow:auto; white-space:pre-wrap;"></div>
                        <div class="history-response-attachments d-none px-2 pb-2" aria-label="响应附件">
                        </div>
                    </div>

                    ${!entry.success ? `<div class="history-error-body" data-history-index="${idx}" data-history-kind="error">
                        <div class="px-3 pt-2"><small class="text-muted fw-semibold text-danger">❌ 错误</small></div>
                        <div class="m-2 p-2 bg-danger-subtle rounded small text-danger" style="font-size:0.8rem; white-space:pre-wrap;"></div>
                    </div>` : ''}
                </div>`;
            });

            if (!html) html = '<div class="text-center text-muted py-4">暂无记录</div>';
            html += `<div class="text-center text-muted small pb-2">显示最近 ${entries.length} / ${Number(this.currentHistoryPaging.total || 0)} 条记录</div>`;
            detail.innerHTML = html;
            entries.forEach((entry, idx) => {
                if (!entry.success) this.renderCallHistoryPayload(idx, 'error');
            });
        } catch (e) {
            if (e.name === 'AbortError') return;
            console.error('Failed to load call history detail:', e);
            detail.innerHTML = '<div class="text-center text-danger py-4">加载失败</div>';
        }
    },

    toggleCallHistoryBody(index, kind) {
        const selectorMap = {
            req: '.history-req-body',
            resp: '.history-resp-body',
            reasoning: '.history-reasoning-body',
            error: '.history-error-body'
        };
        const selector = selectorMap[kind];
        const body = document.querySelector(`${selector}[data-history-index="${index}"]`);
        if (!body) return;

        body.classList.toggle('d-none');
        this.renderCallHistoryPayload(index, kind);
    },

    renderCallHistoryPayload(index, kind) {
        const selectorMap = {
            req: '.history-req-body',
            resp: '.history-resp-body',
            reasoning: '.history-reasoning-body',
            error: '.history-error-body'
        };
        const selector = selectorMap[kind];
        const body = document.querySelector(`${selector}[data-history-index="${index}"]`);
        if (!body) return;
        if (body.dataset.rendered === 'true') return;

        const entry = this.currentHistoryEntries[index] || {};
        let text = '';
        const target = body.querySelector('pre') || body.querySelector('.rounded');
        if (!target) return;

        if (kind === 'error') {
            target.textContent = entry.error_preview || '';
            body.dataset.rendered = 'true';
            return;
        }

        target.textContent = '加载中…';
        this.fetchCallHistoryEntry(entry)
            .then(fullEntry => {
                if (kind === 'req') text = JSON.stringify(this.sanitizeCallHistoryPayload(fullEntry.messages || []), null, 2);
                if (kind === 'reasoning') text = fullEntry.reasoning_text || '';
                if (kind === 'resp') {
                    this.renderCallHistoryResponse(body, fullEntry, entry);
                } else {
                    target.textContent = text;
                }
                body.dataset.rendered = 'true';
            })
            .catch(e => {
                console.error('Failed to load call history entry:', e);
                target.textContent = '加载失败';
            });
    },

    buildCallHistoryAttachmentUrl(entry, attachmentIndex, download = false) {
        const params = new URLSearchParams({
            plugin_name: entry.plugin_name || this.currentHistoryPaging?.plugin || '',
            call_type: entry.call_type || this.currentHistoryPaging?.callType || '',
            index: String(entry.index ?? ''),
            attachment_index: String(attachmentIndex),
        });
        if (download) params.set('download', 'true');
        return `/api/llm/call-history/attachment?${params.toString()}`;
    },

    formatCallHistoryAttachmentSize(value) {
        const bytes = Number(value || 0);
        if (!Number.isFinite(bytes) || bytes <= 0) return '大小未知';
        if (bytes < 1024) return `${Math.round(bytes)} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
        return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
    },

    callHistoryFileIcon(attachment) {
        const mime = String(attachment?.mime_type || '').toLowerCase();
        const name = String(attachment?.name || '').toLowerCase();
        if (mime.includes('pdf') || name.endsWith('.pdf')) return 'bi-file-earmark-pdf';
        if (mime.includes('spreadsheet') || /\.(xlsx?|csv|tsv)$/.test(name)) return 'bi-file-earmark-spreadsheet';
        if (mime.includes('presentation') || /\.(pptx?|odp)$/.test(name)) return 'bi-file-earmark-slides';
        if (mime.includes('word') || /\.(docx?|odt|rtf)$/.test(name)) return 'bi-file-earmark-word';
        if (mime.startsWith('audio/')) return 'bi-file-earmark-music';
        if (mime.startsWith('video/')) return 'bi-file-earmark-play';
        if (/\.(zip|7z|rar|tar|gz|tgz)$/.test(name)) return 'bi-file-earmark-zip';
        if (mime.startsWith('text/') || /\.(txt|md|jsonl?|ya?ml|log)$/.test(name)) return 'bi-file-earmark-text';
        return 'bi-file-earmark';
    },

    renderCallHistoryResponse(body, fullEntry, summaryEntry = {}) {
        const textTarget = body.querySelector('.history-response-text');
        const attachmentTarget = body.querySelector('.history-response-attachments');
        if (!textTarget || !attachmentTarget) return;

        const responseText = String(fullEntry.response_text || '');
        const attachments = Array.isArray(fullEntry.response_attachments)
            ? fullEntry.response_attachments.filter(item => item && typeof item === 'object')
            : [];

        textTarget.classList.remove('d-none', 'text-muted');
        if (responseText) {
            textTarget.textContent = responseText;
        } else if (!attachments.length) {
            textTarget.textContent = '（空响应）';
            textTarget.classList.add('text-muted');
        } else {
            textTarget.textContent = '';
            textTarget.classList.add('d-none');
        }

        if (!attachments.length) {
            attachmentTarget.innerHTML = '';
            attachmentTarget.classList.add('d-none');
            return;
        }

        const entry = { ...summaryEntry, ...fullEntry };
        attachmentTarget.innerHTML = attachments.map((attachment, arrayIndex) => {
            const attachmentIndex = Number.isInteger(Number(attachment.index))
                ? Number(attachment.index)
                : arrayIndex;
            const name = String(attachment.name || `附件 ${arrayIndex + 1}`);
            const mime = String(attachment.mime_type || 'application/octet-stream');
            const size = this.formatCallHistoryAttachmentSize(attachment.size);
            const isImage = String(attachment.type || '').toLowerCase() === 'image'
                || mime.toLowerCase().startsWith('image/');
            const previewUrl = this.escapeHtml(this.buildCallHistoryAttachmentUrl(entry, attachmentIndex));
            const downloadUrl = this.escapeHtml(this.buildCallHistoryAttachmentUrl(entry, attachmentIndex, true));
            const safeName = this.escapeHtml(name);
            const safeMeta = this.escapeHtml(`${size} · ${mime}`);

            if (isImage) {
                return `<article class="history-response-attachment history-response-image">
                    <a class="history-response-image-preview" href="${previewUrl}" target="_blank" rel="noopener">
                        <img src="${previewUrl}" alt="${safeName}" loading="lazy" data-history-attachment-image>
                        <span class="history-response-image-fallback d-none"><i class="bi bi-image"></i>图片暂不可预览</span>
                    </a>
                    <div class="history-response-attachment-info">
                        <span title="${safeName}">${safeName}</span>
                        <a href="${downloadUrl}" title="下载 ${safeName}" aria-label="下载 ${safeName}"><i class="bi bi-download"></i></a>
                    </div>
                    <small>${safeMeta}</small>
                </article>`;
            }

            const icon = this.callHistoryFileIcon(attachment);
            return `<a class="history-response-attachment history-response-file" href="${downloadUrl}">
                <i class="bi ${icon} history-response-file-icon"></i>
                <span class="history-response-file-copy">
                    <strong title="${safeName}">${safeName}</strong>
                    <small>${safeMeta}</small>
                </span>
                <i class="bi bi-download history-response-file-download"></i>
            </a>`;
        }).join('');
        attachmentTarget.classList.remove('d-none');
        attachmentTarget.querySelectorAll('[data-history-attachment-image]').forEach(image => {
            image.addEventListener('error', () => {
                image.classList.add('d-none');
                const frame = image.closest('.history-response-image-preview');
                frame?.classList.add('is-unavailable');
                frame?.querySelector('.history-response-image-fallback')?.classList.remove('d-none');
            }, { once: true });
        });
    },

    formatJobTime(value) {
        if (!value) return '-';
        const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);
        return date.toLocaleString('zh-CN');
    },

    formatJobDuration(job) {
        const seconds = Number(job.elapsed_seconds ?? job.duration_seconds ?? 0);
        if (!Number.isFinite(seconds) || seconds <= 0) return '-';
        if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
        const mins = Math.floor(seconds / 60);
        const rest = Math.round(seconds % 60);
        return `${mins} 分 ${rest} 秒`;
    },

    reasoningEffortLabel(value) {
        const normalized = String(value || '');
        return {
            none: '无',
            minimal: '最低',
            low: '低',
            medium: '中',
            high: '高',
            xhigh: '超高',
            max: '最大',
            inherit: '继承默认值'
        }[normalized] || normalized || '-';
    },

    jobStatusBadge(status) {
        const normalized = String(status || 'unknown');
        const cls = {
            starting: 'bg-info',
            running: 'bg-primary',
            completed: 'bg-success',
            failed: 'bg-danger',
            timeout: 'bg-warning text-dark',
            cancelling: 'bg-warning text-dark',
            cancelled: 'bg-secondary',
            terminating: 'bg-warning text-dark',
            idle: 'bg-secondary',
            pending: 'bg-info',
            ready: 'bg-success',
        }[normalized] || 'bg-secondary';
        const label = {
            starting: '正在启动',
            running: '运行中',
            completed: '已完成',
            failed: '失败',
            timeout: '已超时',
            cancelling: '正在取消',
            cancelled: '已取消',
            terminating: '正在终止',
            idle: '空闲',
            pending: '等待中',
            ready: '就绪',
            unknown: '未知',
        }[normalized] || normalized;
        return `<span class="badge ${cls}">${this.escapeHtml(label)}</span>`;
    },

    async loadCodexJobs(options = {}) {
        const container = document.getElementById('codexJobsList');
        if (!container) return;
        const quiet = Boolean(options.quiet);
        if (quiet && this.codexJobsLoading) return;
        this.codexJobsLoading = true;
        try {
            if (!quiet) {
                container.innerHTML = `<div class="text-center text-muted py-4"><div class="spinner-border spinner-border-sm me-2"></div>正在加载 Codex 运行状态…</div>`;
            }
            const result = await API.codexJobs.list();
            const data = result.data || {};
            this.renderCodexJobs(
                data.active || [],
                data.recent || [],
                data.stats || {},
                data.sessions || [],
                data.session_stats || {},
                data.runtime || {},
                data.upgrade || {},
                data.file_tools || {},
            );
            this.scheduleCodexJobsPoll(
                (data.active || []).length,
                Boolean(data.upgrade?.operation_running),
            );
        } catch (e) {
            console.error('Failed to load Codex jobs:', e);
            if (!quiet) {
                container.innerHTML = `<div class="alert alert-danger">加载 Codex 运行状态失败：${this.escapeHtml(e.message)}</div>`;
            }
        } finally {
            this.codexJobsLoading = false;
        }
    },

    scheduleCodexJobsPoll(activeCount = 0, operationRunning = false) {
        clearTimeout(this.codexJobsPollTimer);
        const pane = document.getElementById('codex');
        if (!pane || pane.classList.contains('d-none')) return;
        const delay = operationRunning ? 1000 : activeCount > 0 ? 2000 : 10000;
        this.codexJobsPollTimer = setTimeout(
            () => CodexCenter.load({ quiet: true }),
            delay,
        );
    },

    renderCodexJobs(active, recent, stats, sessions = [], sessionStats = {}, runtime = {}, upgrade = {}, fileTools = {}) {
        const container = document.getElementById('codexJobsList');
        if (!container) return;
        this.codexExpandedSessions ||= new Set();

        const formatK = value => {
            const number = Number(value || 0);
            if (number >= 1000000) return `${(number / 1000000).toFixed(1).replace(/\.0$/, '')}M`;
            if (number >= 1000) return `${(number / 1000).toFixed(1).replace(/\.0$/, '')}k`;
            return number.toLocaleString();
        };
        const statusMeta = status => {
            const key = String(status || 'idle');
            const labels = {
                queued: '等待中', starting: '准备中', running: '运行中', completed: '已完成', failed: '失败',
                timeout: '已超时', cancelling: '正在中断', cancelled: '已取消', idle: '空闲',
                pending_replay: '待回放', rotation_pending: '待新线程',
            };
            const classes = {
                queued: 'busy', starting: 'busy', running: 'busy', completed: 'ready', failed: 'failed',
                timeout: 'warning', cancelling: 'warning', cancelled: '', idle: '', pending_replay: 'warning', rotation_pending: 'warning',
            };
            return { label: labels[key] || key, className: classes[key] || '' };
        };
        const activityLabel = job => {
            const item = String(job?.current_item_type || '');
            const event = String(job?.progress_event || '');
            const labels = {
                reasoning: '分析中', agentMessage: '生成回复', commandExecution: '执行命令',
                webSearch: '搜索中', mcpToolCall: '调用工具', fileChange: '处理文件',
                imageGeneration: '生成图片', dynamicToolCall: '调用工具',
            };
            const base = labels[item] || {
                queued: '等待执行', turn_started: '开始处理', item_started: '处理中',
                token_usage: '整理结果', process_started: '进程运行中', process_completed: '读取结果',
                response_ready: '结果就绪', error: '执行失败',
            }[event] || statusMeta(job?.status).label;
            const summary = String(job?.current_item_summary || '').trim();
            return summary ? `${base} · ${summary}` : base;
        };
        const usageMeta = accuracy => {
            const normalized = String(accuracy || 'unknown').toLowerCase();
            if (normalized === 'reported') return { label: '', className: '', available: true };
            if (normalized === 'estimated') return { label: '含估算', className: 'warning', available: true };
            if (normalized === 'partial') return { label: '部分统计', className: 'warning', available: true };
            return { label: '', className: '', available: false };
        };
        const usageText = (totals, meta) => meta.available
            ? `${formatK(totals?.total_tokens || 0)} Token${meta.label ? ` · ${meta.label}` : ''}`
            : 'Token 未上报';
        const rotationLabel = reason => ({
            no_saved_thread: '首次创建', runtime_profile_changed: 'Profile 已变更', access_policy_changed: '权限策略已变更',
            model_changed: '模型已变更', reasoning_effort_changed: '推理强度已变更', max_turns_reached: '达到轮数上限',
            soft_token_limit: '达到上下文软上限', compaction_limit: '达到压缩次数上限', idle_timeout: '空闲超时',
            message_prefix_changed: '消息前缀已变化', resume_failed: '原线程无法恢复', manual_reset: '手动重置',
            context_policy_changed: '升级为增量线程', role_instructions_changed: '角色指令已变更', incomplete_turn: '上轮未完成',
            chat_policy_changed: '聊天策略已变更', discarded_followup: '未发送的跟进已丢弃',
        }[String(reason || '')] || String(reason || '-'));
        const fallbackLabel = reason => ({
            runtime_unavailable: 'App Server 未就绪', circuit_open: 'App Server 熔断保护',
            explicit_stateless: '显式无状态请求',
        }[String(reason || '')] || String(reason || '-').replace('app_server_unavailable:', 'App Server 异常：'));

        const pools = runtime.pools || {};
        const poolValues = Object.values(pools);
        const runtimeWorkers = poolValues.flatMap(pool => Array.isArray(pool.workers) ? pool.workers : []);
        const lifecyclePolicy = runtimeWorkers[0] || {};
        const workerCount = poolValues.reduce((sum, pool) => sum + Number(pool.size || 0), 0);
        const queueCount = poolValues.reduce((sum, pool) => sum + Number(pool.waiting || 0), 0);
        const activeCount = Number(stats.active_count ?? active.length ?? 0);
        const sessionCount = Number(sessionStats.session_count ?? sessions.length ?? 0);
        const operation = upgrade.operation || null;
        const operationRunning = Boolean(upgrade.operation_running);
        const serverRunning = Boolean(runtime.running);
        const runtimeError = String(runtime.last_error || '');
        const maintenance = Boolean(runtime.maintenance || operationRunning);
        const dotClass = maintenance ? 'busy' : serverRunning ? 'ready' : 'failed';
        const installedVersion = String(runtime.active_version || upgrade.installed_version || '-');
        const schemaShort = String(runtime.schema_hash || '').slice(0, 10) || '-';
        const fileToolNames = Array.isArray(fileTools.command_names) ? fileTools.command_names : [];
        const fileToolRoots = Array.isArray(fileTools.tool_roots) ? fileTools.tool_roots : [];
        const fileToolsReady = fileTools.status === 'ready';
        const codexRuntimePath = String(fileTools.codex?.path || fileTools.codex?.configured || '-');
        const codexCandidates = Array.isArray(fileTools.codex_candidates) ? fileTools.codex_candidates : [];
        const codexCandidatePaths = [...new Set(
            [codexRuntimePath, ...codexCandidates.map(item => String(item?.path || ''))]
                .filter(path => path && path !== '-'),
        )];
        const codexPathValue = this.codexRuntimePathDraft ?? codexRuntimePath;
        const codexCandidateOptions = codexCandidatePaths
            .map(path => `<option value="${this.escapeHtml(path)}"></option>`)
            .join('');

        const updateButton = upgrade.update_available && !operationRunning
            ? `<button class="btn btn-primary codex-update-start"><i class="bi bi-arrow-up-circle me-1"></i>更新至 ${this.escapeHtml(upgrade.available_version || '最新版本')}</button>`
            : '';
        const rollbackButton = upgrade.rollback_available && !operationRunning
            ? `<button class="btn btn-outline-secondary codex-update-rollback" title="恢复 ${this.escapeHtml(upgrade.rollback_version || '')}"><i class="bi bi-arrow-counterclockwise me-1"></i>回退至 ${this.escapeHtml(upgrade.rollback_version || '上一版本')}</button>`
            : '';

        const runtimeActivityHtml = activeCount || queueCount
            ? `${activeCount ? `<span><strong>${activeCount}</strong> 运行中</span>` : ''}${queueCount ? `<span><strong>${queueCount}</strong> 排队</span>` : ''}`
            : '<span>当前空闲</span>';
        const environmentSummary = upgrade.update_available
            ? `${this.escapeHtml(installedVersion)} · 可更新 ${this.escapeHtml(upgrade.available_version || '')}`
            : `${this.escapeHtml(installedVersion)} · ${fileToolsReady ? '工具已连接' : '工具需检查'}`;

        const operationFinishedAt = operation?.finished_at ? Date.parse(operation.finished_at) : NaN;
        const showOperation = Boolean(operation && (
            operationRunning
            || operation.status === 'failed'
            || !Number.isFinite(operationFinishedAt)
            || Date.now() - operationFinishedAt < 10 * 60 * 1000
        ));
        const operationHtml = showOperation ? (() => {
            const opStatus = statusMeta(operation.status === 'succeeded' || operation.status === 'rolled_back' ? 'completed' : operation.status);
            const logs = (operation.logs || []).map(log => `
                <div>${this.escapeHtml(log.at || '')} · ${this.escapeHtml(log.message || '')}</div>`).join('');
            return `
                <div class="codex-operation">
                    <span class="codex-inline-badge ${opStatus.className}">${this.escapeHtml(opStatus.label)}</span>
                    <div>
                        <div class="codex-operation-message" title="${this.escapeHtml(operation.message || '')}">${this.escapeHtml(operation.message || '')}</div>
                        <div class="progress mt-1"><div class="progress-bar" style="width:${Math.max(0, Math.min(Number(operation.progress || 0), 100))}%"></div></div>
                    </div>
                    ${logs ? `<details><summary>过程</summary><div class="codex-operation-log">${logs}</div></details>` : '<span></span>'}
                </div>`;
        })() : '';

        const activeByChat = new Map(
            active.filter(job => job.chat_id).map(job => [String(job.chat_id), job]),
        );

        const renderSessions = () => {
            if (!sessions.length) return '<div class="codex-empty">暂无持久会话</div>';
            const rows = sessions.map(session => {
                const chatId = String(session.chat_id || '');
                const activeJob = activeByChat.get(chatId);
                const continuityStatus = String(session.continuity_status || 'synchronized');
                const status = statusMeta(activeJob?.status || (['pending_replay', 'rotation_pending'].includes(continuityStatus) ? continuityStatus : session.status));
                const input = Number(session.last_input_tokens || 0);
                const contextInput = Number(session.context_input_tokens ?? input);
                const cached = Number(session.last_cached_input_tokens || 0);
                const completion = Number(session.last_completion_tokens || 0);
                const total = Number(session.last_total_tokens || 0);
                const usage = usageMeta(session.usage_accuracy);
                const hasUsage = usage.available;
                const contextWindow = Number(session.model_context_window || 0);
                const contextPercent = session.context_usage_percent === null || session.context_usage_percent === undefined
                    ? null : Number(session.context_usage_percent);
                const safePercent = Math.max(0, Math.min(contextPercent || 0, 100));
                const cacheRate = session.usage_cache_available && input > 0 ? cached / input * 100 : null;
                const roleName = !session.role_name || session.role_name === 'default' ? '默认' : session.role_name;
                const expanded = this.codexExpandedSessions.has(chatId);
                const sessionTotal = session.session_total || {};
                const lifetimeTotal = session.lifetime_total || {};
                const sessionUsage = usageMeta(session.session_usage_accuracy);
                const lifetimeUsage = usageMeta(session.lifetime_usage_accuracy);
                const contextClass = safePercent >= 90 ? 'danger' : safePercent >= 75 ? 'warning' : '';
                const contextHtml = contextWindow && contextPercent !== null && Number.isFinite(contextPercent) ? `
                    <div class="codex-context" title="按 Codex CLI 口径估算：最近请求总 Token，扣除 12K 基础开销">
                        <div class="codex-context-label"><span>${formatK(contextInput)}</span><span>剩余 ${Number(session.context_remaining_percent).toFixed(0)}%</span></div>
                        <div class="codex-context-track ${contextClass}"><i style="width:${safePercent}%"></i></div>
                    </div>` : contextWindow
                    ? `<span class="codex-cell-sub">未知 / ${formatK(contextWindow)}</span>`
                    : '<span class="codex-cell-sub">窗口未知</span>';
                const detailHtml = expanded ? `
                    <tr class="codex-detail-row"><td colspan="7">
                        <div class="codex-detail-grid">
                            <div><small>Thread ID</small><span title="${this.escapeHtml(session.thread_id || '')}">${this.escapeHtml(session.thread_id || '-')}</span></div>
                            <div><small>最近 Turn</small><span title="${this.escapeHtml(session.turn_id || '')}">${this.escapeHtml(session.turn_id || '-')}</span></div>
                            <div><small>最近一轮</small><span>输入 ${formatK(input)} · 输出 ${formatK(completion)} · 缓存 ${formatK(cached)}</span></div>
                            <div><small>当前线程累计</small><span>${usageText(sessionTotal, sessionUsage)}</span></div>
                            <div><small>逻辑会话累计</small><span>${usageText(lifetimeTotal, lifetimeUsage)}</span></div>
                            <div><small>线程生命周期</small><span>第 ${Number(session.thread_generation || 0).toLocaleString()} 代 · 当前 ${Number(session.turn_count || 0).toLocaleString()} 轮 · 总计 ${Number(session.lifetime_turn_count || 0).toLocaleString()} 轮</span></div>
                            <div><small>上下文压缩</small><span>当前 ${Number(session.compaction_count || 0)} 次 · 累计 ${Number(session.lifetime_compaction_count || 0)} 次</span></div>
                            <div><small>推理 / 搜索</small><span>${this.escapeHtml(this.reasoningEffortLabel(session.reasoning_effort))} · ${session.web_search_mode ? this.escapeHtml(session.web_search_mode) : '关闭'}</span></div>
                            <div><small>运行后端</small><span>${this.escapeHtml(session.backend || '-')} · ${this.escapeHtml(session.runtime_profile || '未标记 Profile')} · ${this.escapeHtml(session.access_mode || '-')}</span></div>
                            <div><small>统计来源</small><span>${this.escapeHtml(session.usage_source || '未提供')}${usage.label ? ` · ${this.escapeHtml(usage.label)}` : ''}</span></div>
                            <div><small>配置窗口 / 自动压缩</small><span>${session.configured_context_window ? `${formatK(Number(session.configured_context_window))} / ${formatK(Number(session.auto_compact_token_limit))}（90%）` : '下次请求更新'}</span></div>
                            <div><small>${activeJob ? '运行时有效窗口' : '上次有效窗口'}</small><span>${contextWindow ? formatK(contextWindow) : '未知'}</span></div>
                            <div><small>上下文窗口来源</small><span>${session.model_context_window_source === 'provider_usage' ? '提供方上报' : session.model_context_window_source === 'profile_metadata' ? 'Profile 元数据' : '未知'}</span></div>
                            <div><small>最近轮换</small><span>${this.escapeHtml(rotationLabel(session.last_rotation_reason))}</span></div>
                            <div><small>Exec 回退</small><span>${Number(session.fallback_count || 0)} 次 · ${this.escapeHtml(fallbackLabel(session.last_fallback_reason))}${continuityStatus === 'pending_replay' ? ' · 下一轮自动回放' : ''}</span></div>
                            <div><small>Schema</small><span>${this.escapeHtml(session.schema_hash || runtime.schema_hash || '-')}</span></div>
                            <div><small>最近活动</small><span>${this.formatJobTime(session.updated_at)}</span></div>
                        </div>
                    </td></tr>` : '';
                return `
                    <tr>
                        <td>
                            <div class="codex-cell-main" title="${this.escapeHtml(chatId)}">${this.escapeHtml(chatId || '-')}</div>
                            <div class="codex-cell-sub">${this.escapeHtml(roleName)}</div>
                        </td>
                        <td>
                            <span class="codex-inline-badge ${status.className}">${this.escapeHtml(status.label)}</span>
                            <div class="codex-cell-sub">${activeJob ? this.escapeHtml(activityLabel(activeJob)) : continuityStatus === 'pending_replay' ? '等待下次触发自动回放' : continuityStatus === 'rotation_pending' ? '下次触发创建新线程' : '等待消息'}</div>
                        </td>
                        <td>
                            <div class="codex-cell-main">${this.escapeHtml(session.model || '-')}</div>
                            <div class="codex-cell-sub">${this.escapeHtml(session.runtime_profile || '未标记 Profile')} · ${Number(session.lifetime_turn_count || session.turn_count || 0).toLocaleString()} 轮</div>
                        </td>
                        <td>${contextHtml}</td>
                        <td>
                            <div class="codex-cell-main codex-mono">${hasUsage ? `${formatK(total)} Token` : 'Token 未上报'}${usage.label ? ` <span class="codex-inline-badge ${usage.className}">${this.escapeHtml(usage.label)}</span>` : ''}</div>
                            <div class="codex-cell-sub">缓存 ${cacheRate === null ? '-' : `${cacheRate.toFixed(1)}%`}</div>
                        </td>
                        <td><span class="codex-mono">${this.formatJobTime(session.updated_at)}</span></td>
                        <td>
                            <div class="codex-row-actions">
                                <button class="btn codex-session-toggle" data-chat-id="${this.escapeHtml(chatId)}" title="${expanded ? '收起详情' : '查看详情'}"><i class="bi bi-${expanded ? 'chevron-up' : 'chevron-down'}"></i></button>
                                ${activeJob ? `<button class="btn text-danger codex-session-interrupt" data-chat-id="${this.escapeHtml(chatId)}" title="中断当前任务"><i class="bi bi-stop-circle"></i></button>` : ''}
                                <button class="btn codex-session-reset" data-chat-id="${this.escapeHtml(chatId)}" title="开启新上下文"><i class="bi bi-arrow-repeat"></i></button>
                                <button class="btn text-danger codex-session-delete" data-chat-id="${this.escapeHtml(chatId)}" title="${activeJob ? '请先中断当前任务' : '删除会话'}" ${activeJob ? 'disabled' : ''}><i class="bi bi-trash3"></i></button>
                            </div>
                        </td>
                    </tr>${detailHtml}`;
            }).join('');
            return `
                <div class="table-responsive">
                    <table class="codex-compact-table" style="min-width:860px">
                        <thead><tr><th>会话</th><th>状态</th><th>模型</th><th>上下文</th><th>最近 Token</th><th>活动时间</th><th></th></tr></thead>
                        <tbody>${rows}</tbody>
                    </table>
                </div>`;
        };

        const renderJobs = (jobs, running = false) => {
            if (!jobs.length) return `<div class="codex-empty">${running ? '当前没有运行中的任务' : '暂无任务历史'}</div>`;
            const rows = jobs.map(job => {
                const requestId = String(job.request_id || '');
                const status = statusMeta(job.status);
                const identity = job.chat_name || job.chat_id || '未知来源';
                const sourceType = job.chat_type === 'group' ? '群聊' : (job.chat_type === 'private' ? '私聊' : '');
                const error = job.error ? `<div class="codex-cell-sub text-danger" title="${this.escapeHtml(job.error)}">${this.escapeHtml(job.error)}</div>` : '';
                return `
                    <tr>
                        <td><div class="codex-cell-main">${this.escapeHtml(identity)}</div><div class="codex-cell-sub">${this.escapeHtml(sourceType || '来源未记录')} · <span class="codex-mono" title="${this.escapeHtml(requestId)}">${this.escapeHtml(requestId.slice(0, 12))}</span></div></td>
                        <td><span class="codex-inline-badge ${status.className}">${this.escapeHtml(status.label)}</span><div class="codex-cell-sub">${this.escapeHtml(activityLabel(job))}</div></td>
                        <td><div class="codex-cell-main">${this.escapeHtml(job.model || '-')}</div><div class="codex-cell-sub">${this.escapeHtml(job.pool_worker || job.backend || '')}${job.fallback_reason ? ` · ${this.escapeHtml(fallbackLabel(job.fallback_reason))}` : ''}</div></td>
                        <td><span class="codex-mono">${formatK(job.total_tokens || 0)}</span></td>
                        <td><span class="codex-mono">${this.formatJobDuration(job)}</span>${error}</td>
                        <td><span class="codex-mono">${this.formatJobTime(job.started_at)}</span></td>
                        <td><div class="codex-row-actions"><button class="btn codex-job-details" data-request-id="${this.escapeHtml(requestId)}" title="查看过程"><i class="bi bi-list-ul"></i></button>${running ? `<button class="btn text-danger codex-job-cancel" data-request-id="${this.escapeHtml(requestId)}" title="取消任务"><i class="bi bi-stop-circle"></i></button>` : ''}</div></td>
                    </tr>`;
            }).join('');
            return `
                <div class="table-responsive"><table class="codex-compact-table" style="min-width:760px">
                    <thead><tr><th>来源</th><th>进度</th><th>模型 / Worker</th><th>Token</th><th>耗时</th><th>开始时间</th><th></th></tr></thead>
                    <tbody>${rows}</tbody>
                </table></div>`;
        };

        container.innerHTML = `
            <div class="codex-center">
                <div class="codex-runtime-strip">
                    <div class="codex-runtime-identity">
                        <span class="codex-runtime-dot ${dotClass}"></span>
                        <div><strong title="${this.escapeHtml(runtimeError)}">${maintenance ? '运行环境维护中' : serverRunning ? 'Codex 正常运行' : 'Codex 未就绪'}</strong>${runtimeError ? `<small>${this.escapeHtml(runtimeError)}</small>` : ''}</div>
                    </div>
                    <div class="codex-runtime-facts">
                        ${runtimeActivityHtml}
                    </div>
                </div>
                ${operationHtml}
                <details class="codex-tool-details" ${this.codexToolDetailsOpen ? 'open' : ''}>
                    <summary><span><i class="bi bi-sliders2"></i>环境与版本</span><small>${environmentSummary}</small></summary>
                    <div class="codex-tool-detail-body">
                        <div class="codex-tool-detail-grid">
                            <div class="codex-runtime-selector">
                                <small>WSL Codex 路径</small>
                                <div>
                                    <input class="form-control codex-runtime-path" list="codexRuntimeCandidates" value="${this.escapeHtml(codexPathValue)}" placeholder="/home/user/.local/bin/codex" spellcheck="false">
                                    <datalist id="codexRuntimeCandidates">${codexCandidateOptions}</datalist>
                                    <button class="btn btn-primary codex-runtime-select" ${operationRunning ? 'disabled' : ''}>切换</button>
                                </div>
                                <span>仅接受 WSL 原生绝对路径；可选择使用不同模型配置的 Codex 包装器。</span>
                            </div>
                            <div><small>安装方式</small><span>${this.escapeHtml(upgrade.installation?.method_label || '-')}</span></div>
                            <div><small>Worker</small><span>${workerCount} 个进程</span></div>
                            <div><small>聊天线程</small><span>独立复用 · 配置窗口 90% 原生压缩 · 空闲 ${Number(lifecyclePolicy.idle_rotate_seconds || 0) ? `${Math.round(Number(lifecyclePolicy.idle_rotate_seconds) / 86400)} 天` : '关闭'}</span></div>
                            <div><small>Schema</small><span class="codex-mono">${this.escapeHtml(schemaShort)}</span></div>
                            <div><small>可用命令</small><span>${this.escapeHtml(fileToolNames.join(' · ') || '未探测到')}</span></div>
                            <div><small>工具目录</small><span class="codex-mono">${this.escapeHtml(fileToolRoots.join(' · ') || '使用系统工具')}</span></div>
                            ${runtimeError ? `<div><small>运行时诊断</small><span class="text-danger" title="${this.escapeHtml(runtimeError)}">${this.escapeHtml(runtimeError)}</span></div>` : ''}
                            ${fileTools.error ? `<div><small>工具诊断</small><span class="text-danger">${this.escapeHtml(fileTools.error)}</span></div>` : ''}
                        </div>
                        <div class="codex-maintenance-actions">
                            <button class="btn btn-outline-secondary codex-file-tools-refresh"><i class="bi bi-tools me-1"></i>重新探测工具</button>
                            <button class="btn btn-outline-secondary codex-update-check" ${operationRunning ? 'disabled' : ''}><i class="bi bi-arrow-clockwise me-1"></i>检查更新</button>
                            ${updateButton}${rollbackButton}
                        </div>
                    </div>
                </details>
                ${active.length ? `<section class="codex-section"><div class="codex-section-head"><h6>正在执行</h6><small>${active.length} 个任务</small></div>${renderJobs(active, true)}</section>` : ''}
                <section class="codex-section">
                    <div class="codex-section-head"><h6>会话</h6><small>${sessionCount} 个持久上下文 · 累计 ${Number(sessionStats.lifetime_turn_count || sessionStats.total_turn_count || 0).toLocaleString()} 轮 · 已记录 ${formatK(sessionStats.logical_lifetime_total_tokens || 0)} Token${Number(sessionStats.unknown_usage_session_count || 0) ? ` · ${Number(sessionStats.unknown_usage_session_count)} 个会话统计不完整` : Number(sessionStats.estimated_usage_session_count || 0) ? ` · ${Number(sessionStats.estimated_usage_session_count)} 个会话含估算` : ''}</small></div>
                    ${renderSessions()}
                </section>
                <details class="codex-section codex-history" ${this.codexHistoryOpen ? 'open' : ''}>
                    <summary>任务历史 <span>${recent.length} 条 · 点击展开</span></summary>
                    ${renderJobs(recent, false)}
                </details>
            </div>`;

        container.querySelectorAll('.codex-session-toggle').forEach(button => {
            button.addEventListener('click', () => {
                const chatId = button.dataset.chatId;
                if (this.codexExpandedSessions.has(chatId)) this.codexExpandedSessions.delete(chatId);
                else this.codexExpandedSessions.add(chatId);
                this.renderCodexJobs(active, recent, stats, sessions, sessionStats, runtime, upgrade, fileTools);
            });
        });
        container.querySelector('.codex-history')?.addEventListener('toggle', event => {
            this.codexHistoryOpen = event.currentTarget.open;
        });
        container.querySelector('.codex-tool-details')?.addEventListener('toggle', event => {
            this.codexToolDetailsOpen = event.currentTarget.open;
        });
        container.querySelector('.codex-runtime-path')?.addEventListener('input', event => {
            this.codexRuntimePathDraft = event.currentTarget.value;
        });
        container.querySelectorAll('.codex-job-cancel').forEach(button => {
            button.addEventListener('click', () => this.cancelCodexJob(button.dataset.requestId));
        });
        container.querySelectorAll('.codex-job-details').forEach(button => {
            button.addEventListener('click', () => this.showCodexJobDetails(button.dataset.requestId));
        });
        container.querySelectorAll('.codex-session-reset').forEach(button => {
            button.addEventListener('click', () => this.resetCodexSession(button.dataset.chatId));
        });
        container.querySelectorAll('.codex-session-delete').forEach(button => {
            button.addEventListener('click', () => this.deleteCodexSession(button.dataset.chatId));
        });
        container.querySelectorAll('.codex-session-interrupt').forEach(button => {
            button.addEventListener('click', () => this.interruptCodexSession(button.dataset.chatId));
        });
        container.querySelector('.codex-update-check')?.addEventListener('click', () => this.checkCodexUpdate());
        container.querySelector('.codex-file-tools-refresh')?.addEventListener('click', () => this.refreshCodexFileTools());
        container.querySelector('.codex-runtime-select')?.addEventListener('click', () => this.selectCodexRuntime());
        container.querySelector('.codex-update-start')?.addEventListener('click', () => this.startCodexUpdate(upgrade));
        container.querySelector('.codex-update-rollback')?.addEventListener('click', () => this.rollbackCodex(upgrade));
    },

    async refreshCodexFileTools() {
        try {
            const result = await API.codexJobs.refreshFileTools();
            const count = Array.isArray(result.data?.command_names) ? result.data.command_names.length : 0;
            UI.showSuccess(`文件处理环境已刷新，共发现 ${count} 项工具`);
        } catch (e) {
            UI.showError(e.message);
        } finally {
            await this.loadCodexJobs({ quiet: true });
        }
    },

    async selectCodexRuntime() {
        const input = document.querySelector('#codexJobsList .codex-runtime-path');
        const path = String(input?.value || '').trim();
        if (!path) {
            UI.showError('请输入 WSL 内的 Codex 绝对路径');
            return;
        }
        const button = document.querySelector('#codexJobsList .codex-runtime-select');
        if (button) button.disabled = true;
        try {
            await API.codexJobs.selectRuntime(path);
            this.codexRuntimePathDraft = null;
            UI.showSuccess('Codex 运行路径已切换');
        } catch (e) {
            UI.showError('切换失败：' + e.message);
        } finally {
            if (button) button.disabled = false;
            await this.loadCodexJobs({ quiet: true });
        }
    },

    async cancelCodexJob(requestId) {
        if (!requestId) return;
        if (!await UI.confirm(`确定要取消 Codex 任务 ${requestId.slice(0, 8)}… 吗？`, {
            title: '取消 Codex 任务',
            confirmText: '取消任务',
            variant: 'danger'
        })) return;
        try {
            await API.codexJobs.cancel(requestId);
            UI.showSuccess('已发送取消请求');
            await this.loadCodexJobs();
        } catch (e) {
            UI.showError('取消失败：' + e.message);
            await this.loadCodexJobs();
        }
    },

    async showCodexJobDetails(requestId) {
        if (!requestId) return;
        try {
            const fallbackLabel = reason => ({
                runtime_unavailable: 'App Server 未就绪', circuit_open: 'App Server 熔断保护',
                explicit_stateless: '显式无状态请求',
            }[String(reason || '')] || String(reason || '-').replace('app_server_unavailable:', 'App Server 异常：'));
            const response = await API.codexJobs.events(requestId);
            const job = response.data?.job || {};
            const events = response.data?.events || [];
            const labels = {
                queued: '进入队列', turn_started: '开始处理', item_started: '开始步骤',
                item_completed: '完成步骤', token_usage: '更新 Token', process_started: '进程启动',
                process_completed: '进程结束', response_ready: '结果就绪', error: '发生错误',
                cancel_requested: '请求中断', turn_completed: 'Turn 完成', job_finished: '任务结束',
                context_compaction: '上下文已压缩',
                thread_identified: '识别任务线程', image_generation_completed: '首张图片生成完成',
                single_image_limit_reached: '停止后续图片生成', image_artifact_validated: '图片文件校验通过',
            };
            const itemLabels = {
                userMessage: '提交输入', reasoning: '分析', agentMessage: '生成回复', commandExecution: '执行命令',
                webSearch: '网页搜索', mcpToolCall: '调用工具', fileChange: '处理文件',
                imageGeneration: '生成图片', dynamicToolCall: '调用工具', imageView: '查看图片',
                contextCompaction: '压缩上下文', plan: '更新计划',
            };
            const statusLabels = {
                inProgress: '进行中', in_progress: '进行中', completed: '完成', failed: '失败',
                declined: '已拒绝', cancelled: '已取消', running: '运行中',
            };
            const formatEventDuration = milliseconds => {
                const value = Number(milliseconds);
                if (!Number.isFinite(value) || value < 0) return '';
                if (value < 1000) return `${Math.round(value)} ms`;
                if (value < 60000) return `${(value / 1000).toFixed(value < 10000 ? 1 : 0)} 秒`;
                return `${Math.floor(value / 60000)} 分 ${Math.round(value % 60000 / 1000)} 秒`;
            };
            const timeline = [];
            const startedItems = new Map();
            events.forEach(source => {
                const event = { ...source };
                const itemId = String(event.item_id || '');
                if (event.type === 'item_started' && itemId) {
                    event._started_at = Number(event.created_at || 0);
                    timeline.push(event);
                    startedItems.set(itemId, event);
                } else if (event.type === 'item_completed' && itemId && startedItems.has(itemId)) {
                    const started = startedItems.get(itemId);
                    const startedAt = Number(started._started_at || started.created_at || 0);
                    Object.assign(started, event, {
                        _started_at: startedAt,
                        _elapsed_ms: event.duration_ms ?? Math.max(0, (Number(event.created_at || 0) - startedAt) * 1000),
                    });
                    startedItems.delete(itemId);
                } else {
                    timeline.push(event);
                }
            });

            const eventRows = timeline.length ? timeline.map(event => {
                const item = itemLabels[event.item_type] || event.item_type || '';
                const title = event.item_type
                    ? `${event.type === 'item_started' ? '开始' : event.type === 'item_completed' ? '完成' : ''}${item ? ` ${item}` : ''}`.trim()
                    : labels[event.type] || event.type || '-';
                let detail = String(event.summary || event.message || '').trim();
                const meta = [];
                const elapsed = formatEventDuration(event.duration_ms ?? event._elapsed_ms);
                if (elapsed) meta.push(`耗时 ${elapsed}`);
                if (event.item_type === 'commandExecution') {
                    if (!detail) detail = String(event.command || '').trim();
                    if (event.cwd) meta.push(`目录 ${event.cwd}`);
                    if (event.exit_code !== undefined) meta.push(`退出码 ${event.exit_code}`);
                } else if (event.item_type === 'fileChange') {
                    meta.push(`${Number(event.change_count || 0)} 个文件`);
                    meta.push(`+${Number(event.additions || 0)} / -${Number(event.deletions || 0)}`);
                } else if (event.item_type === 'mcpToolCall' || event.item_type === 'dynamicToolCall') {
                    if (!detail) detail = [event.server || event.namespace, event.tool].filter(Boolean).join(' / ');
                    if (event.read_only !== undefined) meta.push(event.read_only ? '只读调用' : '可能修改数据');
                    if (event.result_items !== undefined) meta.push(`${event.result_items} 项结果`);
                    if (event.success !== undefined) meta.push(event.success ? '调用成功' : '调用失败');
                    if (event.error) meta.push(`错误：${event.error}`);
                } else if (event.item_type === 'webSearch') {
                    if (!detail) detail = String(event.query || '').trim();
                    if (event.action) meta.push(`动作 ${event.action}`);
                    if (event.result_count !== undefined) meta.push(`${event.result_count} 条结果`);
                } else if (event.item_type === 'agentMessage') {
                    detail = event.phase === 'final_answer' ? '正在整理最终答复' : '正在生成阶段性消息';
                    if (event.text_chars) meta.push(`${Number(event.text_chars).toLocaleString()} 字符`);
                } else if (event.item_type === 'reasoning') {
                    detail = '模型正在分析（内部推理内容不展示）';
                } else if (event.item_type === 'userMessage') {
                    detail = `已提交 ${Number(event.content_items || 0)} 项输入`;
                } else if (event.type === 'token_usage') {
                    detail = `输入 ${Number(event.prompt_tokens || 0).toLocaleString()} · 输出 ${Number(event.completion_tokens || 0).toLocaleString()} · 合计 ${Number(event.total_tokens || 0).toLocaleString()} Token`;
                } else if (event.type === 'queued') {
                    detail = [event.profile, event.backend].filter(Boolean).join(' · ');
                } else if (event.type === 'process_started' && event.pid) {
                    detail = `进程 PID ${event.pid}`;
                } else if (event.type === 'process_completed') {
                    detail = `进程退出码 ${event.returncode ?? '-'}`;
                } else if (event.type === 'response_ready') {
                    detail = `回复 ${Number(event.text_chars || 0).toLocaleString()} 字符 · ${Number(event.attachment_count || 0)} 个附件`;
                } else if (event.type === 'job_finished' || event.type === 'turn_completed') {
                    detail = statusLabels[event.status] || event.status || detail;
                }
                const status = statusLabels[event.status] || event.status || '';
                if (status && !meta.includes(status) && !detail.includes(status)) meta.push(status);
                return `
                    <div class="codex-event">
                        <div class="codex-event-head"><strong>${this.escapeHtml(title)}</strong><time>${this.formatJobTime(event.created_at)}</time></div>
                        ${detail ? `<div class="codex-event-summary${event.item_type === 'commandExecution' ? ' codex-event-command' : ''}">${this.escapeHtml(detail)}</div>` : ''}
                        ${meta.length ? `<div class="codex-event-meta">${meta.map(value => `<span>${this.escapeHtml(value)}</span>`).join('')}</div>` : ''}
                    </div>`;
            }).join('') : '<div class="text-muted small">暂无过程记录</div>';

            let modal = document.getElementById('codexJobEventModal');
            if (!modal) {
                modal = document.createElement('div');
                modal.id = 'codexJobEventModal';
                modal.className = 'modal fade';
                modal.tabIndex = -1;
                modal.innerHTML = `
                    <div class="modal-dialog modal-lg modal-dialog-scrollable">
                        <div class="modal-content">
                            <div class="modal-header py-2"><h6 class="modal-title">任务过程</h6><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
                            <div class="modal-body" id="codexJobEventBody"></div>
                        </div>
                    </div>`;
                document.body.appendChild(modal);
            }
            modal.querySelector('#codexJobEventBody').innerHTML = `
                <div class="d-flex flex-wrap gap-3 pb-3 mb-3 border-bottom small text-muted">
                    <span>状态 <strong class="text-body">${this.escapeHtml(job.status || '-')}</strong></span>
                    <span>来源 <strong class="text-body">${this.escapeHtml(job.chat_name || job.chat_id || '未知来源')}</strong></span>
                    ${job.chat_type ? `<span>类型 <strong class="text-body">${this.escapeHtml(job.chat_type === 'group' ? '群聊' : (job.chat_type === 'private' ? '私聊' : job.chat_type))}</strong></span>` : ''}
                    <span>模型 <strong class="text-body">${this.escapeHtml(job.model || '-')}</strong></span>
                    <span>Worker <strong class="text-body">${this.escapeHtml(job.pool_worker || job.backend || '-')}</strong></span>
                    ${job.config_profile ? `<span>Profile <strong class="text-body">${this.escapeHtml(job.config_profile)}</strong></span>` : ''}
                    ${job.fallback_reason ? `<span>Exec 原因 <strong class="text-body">${this.escapeHtml(fallbackLabel(job.fallback_reason))}</strong></span>` : ''}
                    ${job.continuity_status === 'pending_replay' ? '<span>连续性 <strong class="text-warning">待下轮自动回放</strong></span>' : ''}
                    <span>耗时 <strong class="text-body">${this.formatJobDuration(job)}</strong></span>
                    <span>Token <strong class="text-body">${Number(job.total_tokens || 0).toLocaleString()}</strong></span>
                </div>
                <div class="codex-job-input-summary">
                    <span>消息 <strong>${Number(job.message_count || 0).toLocaleString()}</strong></span>
                    <span>提示字符 <strong>${Number(job.prompt_chars || 0).toLocaleString()}</strong></span>
                    <span>输入文件 <strong>${Number(job.input_file_count || 0)}</strong></span>
                    <span>输入图片 <strong>${Number(job.image_count || 0)}</strong></span>
                    <span>输出附件 <strong>${Number(job.attachment_count || 0)}</strong></span>
                    <span class="codex-mono" title="${this.escapeHtml(requestId)}">ID ${this.escapeHtml(requestId.slice(0, 12))}</span>
                </div>
                ${job.error ? `<div class="alert alert-danger py-2 small">${this.escapeHtml(job.error)}</div>` : ''}
                <div class="codex-event-list">${eventRows}</div>`;
            bootstrap.Modal.getOrCreateInstance(modal).show();
        } catch (error) {
            UI.showError(`读取任务过程失败：${error.message}`);
        }
    },

    async checkCodexUpdate() {
        try {
            const response = await API.codexJobs.checkUpdate();
            const data = response.data || {};
            if (data.update_available) {
                UI.showSuccess(`可更新到 Codex ${data.available_version}`);
            } else {
                UI.showSuccess('当前已是最新版本');
            }
            await this.loadCodexJobs({ quiet: true });
        } catch (error) {
            UI.showError(`检查版本失败：${error.message}`);
        }
    },

    async startCodexUpdate(upgrade = {}) {
        const target = upgrade.available_version ? ` ${upgrade.available_version}` : '最新版';
        if (!await UI.confirm(`确定将 Codex 更新到${target}吗？现有任务会继续运行，验证通过后自动切换。`, {
            title: '更新 Codex',
            confirmText: '开始更新',
            variant: 'primary',
        })) return;
        try {
            await API.codexJobs.startUpdate();
            UI.showSuccess('Codex 更新任务已开始');
            await this.loadCodexJobs({ quiet: true });
        } catch (error) {
            UI.showError(`启动更新失败：${error.message}`);
        }
    },

    async rollbackCodex(upgrade = {}) {
        const version = upgrade.rollback_version || '上一版本';
        if (!await UI.confirm(`确定恢复 Codex ${version} 吗？运行时会在验证通过后自动切换。`, {
            title: '恢复 Codex',
            confirmText: '开始恢复',
            variant: 'warning',
        })) return;
        try {
            await API.codexJobs.rollback();
            UI.showSuccess('Codex 恢复任务已开始');
            await this.loadCodexJobs({ quiet: true });
        } catch (error) {
            UI.showError(`启动恢复失败：${error.message}`);
        }
    },

    async resetCodexSession(chatId) {
        if (!chatId) return;
        if (!await UI.confirm(`确定让“${chatId}”在下一条消息时开启新上下文吗？`, {
            title: '开启新上下文',
            confirmText: '确认',
            variant: 'warning',
        })) return;
        try {
            await API.codexJobs.resetSession(chatId);
            this.codexExpandedSessions?.delete(chatId);
            UI.showSuccess('下一条消息将使用新上下文');
            await this.loadCodexJobs({ quiet: true });
        } catch (error) {
            UI.showError(`操作失败：${error.message}`);
        }
    },

    async deleteCodexSession(chatId) {
        if (!chatId) return;
        if (!await UI.confirm(`确定永久删除“${chatId}”的 Codex 会话吗？\n\n持久上下文及 Codex 线程会被删除；微信聊天记录和任务历史不会受影响。`, {
            title: '删除 Codex 会话',
            confirmText: '删除会话',
            variant: 'danger',
        })) return;
        try {
            await API.codexJobs.deleteSession(chatId);
            this.codexExpandedSessions?.delete(chatId);
            UI.showSuccess('Codex 会话已删除');
            await this.loadCodexJobs({ quiet: true });
        } catch (error) {
            UI.showError(`删除失败：${error.message}`);
        }
    },

    async interruptCodexSession(chatId) {
        if (!chatId) return;
        if (!await UI.confirm(`确定中断“${chatId}”当前正在运行的任务吗？`, {
            title: '中断任务',
            confirmText: '中断',
            variant: 'danger',
        })) return;
        try {
            await API.codexJobs.interruptSession(chatId);
            UI.showSuccess('已发送中断请求');
            await this.loadCodexJobs({ quiet: true });
        } catch (error) {
            UI.showError(`中断失败：${error.message}`);
        }
    },

    async fetchCallHistoryEntry(entry) {
        if (entry._fullEntry) return entry._fullEntry;
        const paging = this.currentHistoryPaging || {};
        const plugin = paging.plugin || entry.plugin_name || '';
        const callType = paging.callType || entry.call_type || '';
        const index = entry.index;
        const resp = await fetch(`/api/llm/call-history/entry?plugin_name=${encodeURIComponent(plugin)}&call_type=${encodeURIComponent(callType)}&index=${encodeURIComponent(index)}`);
        const data = await resp.json();
        if (data.status !== 'success' || !data.data) {
            throw new Error(data.detail || data.message || '调用记录不存在');
        }
        entry._fullEntry = data.data;
        return entry._fullEntry;
    },
};

// Export for use in main.js
window.LLMManager = LLMManager;
