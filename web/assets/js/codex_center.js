/** First-class Codex setup and operations surface. */
const CodexCenter = {
    loading: false,
    profileEditorBound: false,
    profiles: [],
    localAuth: { available: false, storage: 'unavailable', reason: '本机登录不可导入' },
    editingProfile: null,
    catalogProviders: [],
    profileCatalogModels: [],
    profileCatalogRequestId: 0,
    oauthProfile: '',
    oauthMakeDefault: false,
    oauthSetupDraft: false,
    oauthAuthSource: '',
    oauthPollTimer: null,
    oauthModels: [],
    oauthEditingProfile: false,
    profileProviderBases: {
        openai: 'https://api.openai.com/v1',
        deepseek: 'https://api.deepseek.com',
        openrouter: 'https://openrouter.ai/api/v1',
        xai: 'https://api.x.ai/v1',
        groq: 'https://api.groq.com/openai/v1',
        mistral: 'https://api.mistral.ai/v1',
        together_ai: 'https://api.together.xyz/v1',
        fireworks_ai: 'https://api.fireworks.ai/inference/v1',
        perplexity: 'https://api.perplexity.ai',
        cerebras: 'https://api.cerebras.ai/v1'
    },

    escape(value) {
        return UI.escapeHtml(value);
    },

    async load({ quiet = false } = {}) {
        if (this.loading) return;
        this.loading = true;
        try {
            const [jobsResponse, profiles] = await Promise.all([
                API.codexJobs.list(),
                quiet ? Promise.resolve(null) : API.codexProfiles.list()
            ]);
            const jobs = jobsResponse.data || {};
            if (profiles) {
                this.renderProfiles(profiles);
                window.App?.updateManagedChatProfiles(profiles);
            }
            LLMManager.renderCodexJobs(
                jobs.active || [], jobs.recent || [], jobs.stats || {},
                jobs.sessions || [], jobs.session_stats || {}, jobs.runtime || {},
                jobs.upgrade || {}, jobs.file_tools || {}
            );
            LLMManager.scheduleCodexJobsPoll(
                (jobs.active || []).length,
                Boolean(jobs.upgrade?.operation_running)
            );
        } catch (error) {
            if (!quiet) UI.showError(`加载 Codex 运行中心失败：${error.message}`);
            if (!quiet) {
                const jobs = document.getElementById('codexJobsList');
                const profiles = document.getElementById('codexProfilesGrid');
                const message = `<div class="alert alert-danger mb-0">${this.escape(error.message)}</div>`;
                if (jobs) jobs.innerHTML = message;
                if (profiles) profiles.innerHTML = message;
            }
        } finally {
            this.loading = false;
        }
    },

    renderProfiles(data) {
        const profiles = data.profiles || [];
        this.profiles = profiles;
        this.localAuth = data.local_auth || this.localAuth;
        this.updateLocalAuthChoice();
        const container = document.getElementById('codexProfilesGrid');
        if (!container) return;
        const setupCard = data.default_profile_id ? '' : `
            <article class="codex-profile-card codex-profile-setup-card">
                <header class="codex-profile-card-head">
                    <strong>尚未配置默认 Profile</strong>
                    <span class="codex-profile-status warning">待配置</span>
                </header>
                <p>Assistant 需要一个已完成凭据配置的 Profile。</p>
                <footer class="codex-profile-card-actions">
                    <button class="btn btn-sm btn-primary" type="button" data-open-profile>新建 Profile</button>
                </footer>
            </article>`;
        const cards = profiles.map(profile => {
            const selected = profile.name === data.default_profile_id;
            const ready = Boolean(profile.available);
            const official = profile.auth_type === 'chatgpt';
            const contextWindow = Number(profile.context_window || 0);
            const contextLabel = contextWindow >= 1_000_000
                ? `${(contextWindow / 1_000_000).toFixed(contextWindow % 1_000_000 ? 1 : 0)}M`
                : (contextWindow >= 1000 ? `${Math.round(contextWindow / 1000)}K` : '自动');
            const sourceLabel = profile.auth_source === 'local_cache'
                ? '本机登录副本'
                : (official ? 'ChatGPT 登录' : (profile.provider_name || 'Responses API'));
            const localCache = profile.auth_source === 'local_cache';
            const syncStatus = String(profile.auth_sync_status || '');
            const syncWarning = localCache && syncStatus && syncStatus !== 'synced';
            const syncStatusLabel = syncStatus === 'outdated'
                ? '登录待同步'
                : (syncStatus === 'missing'
                    ? '登录未同步'
                    : (syncStatus === 'unavailable' || syncStatus === 'invalid' ? '登录需检查' : ''));
            return `
                <article class="codex-profile-card ${selected ? 'selected' : ''} ${ready ? '' : 'not-ready'}" ${selected ? 'aria-current="true"' : ''}>
                    <header class="codex-profile-card-head">
                        <div class="codex-profile-card-identity">
                            <strong title="${this.escape(profile.name)}">${this.escape(profile.name)}</strong>
                            <span class="codex-profile-kind">${official ? '官方 Codex' : 'API'}</span>
                        </div>
                        <div class="codex-profile-card-statuses">
                            ${selected ? '<span class="codex-profile-status selected">默认</span>' : ''}
                            <span class="codex-profile-status ${ready ? 'ready' : 'warning'}">${ready ? '可用' : '需登录'}</span>
                            ${syncWarning ? `<span class="codex-profile-status warning" title="${this.escape(profile.auth_sync_reason || syncStatusLabel)}">${syncStatusLabel}</span>` : ''}
                        </div>
                    </header>
                    <div class="codex-profile-card-model" title="${this.escape(profile.model)}">${this.escape(profile.model)}</div>
                    <div class="codex-profile-card-meta" aria-label="Profile 配置摘要">
                        ${ready ? `<span><b>推理</b>${this.escape(profile.reasoning_effort || 'inherit')}</span><span><b>上下文</b>${contextLabel}</span>` : '<span class="warning">完成凭据配置后可使用</span>'}
                        ${profile.supports_vision ? '<span>图片</span>' : ''}
                        ${profile.supports_web_search ? '<span>搜索</span>' : ''}
                    </div>
                    <footer class="codex-profile-card-actions">
                        <span class="codex-profile-card-source" title="${this.escape(sourceLabel)}">${this.escape(sourceLabel)}</span>
                        <div>
                        <button class="btn btn-sm btn-outline-secondary" type="button" data-profile-skills="${this.escape(profile.name)}" title="管理此 Profile 的 Skills"><i class="bi bi-tools me-1"></i>Skills</button>
                        <button class="btn btn-sm btn-outline-secondary codex-profile-icon-action" type="button" data-edit-profile="${this.escape(profile.name)}" aria-label="编辑 ${this.escape(profile.name)}" title="编辑"><i class="bi bi-pencil"></i></button>
                        ${localCache ? `<button class="btn btn-sm ${syncWarning ? 'btn-outline-warning' : 'btn-outline-secondary'} codex-profile-icon-action" type="button" data-sync-local-auth="${this.escape(profile.name)}" aria-label="同步 ${this.escape(profile.name)} 的本机登录" title="同步本机登录"><i class="bi bi-arrow-repeat"></i></button>` : ''}
                        ${official && !ready
                            ? (localCache ? '' : `<button class="btn btn-sm btn-outline-primary" type="button" data-profile-login="${this.escape(profile.name)}">完成登录</button>`)
                            : (selected ? '' : `<button class="btn btn-sm btn-outline-primary" type="button" data-default-profile="${this.escape(profile.name)}" ${ready ? '' : 'disabled'}>设为默认</button>`)}
                        <button class="btn btn-sm btn-outline-danger codex-profile-icon-action" type="button" data-delete-profile="${this.escape(profile.name)}" aria-label="删除 ${this.escape(profile.name)}" title="删除"><i class="bi bi-trash3"></i></button>
                        </div>
                    </footer>
                </article>`;
        }).join('');
        container.innerHTML = setupCard + cards + `
            <button class="codex-profile-add" type="button" onclick="CodexCenter.openProfileModal()"><i class="bi bi-plus-lg"></i><span>新建 Profile</span><small>连接官方 Codex 或兼容 Responses API</small></button>`;
        container.querySelectorAll('[data-default-profile]').forEach(button => {
            button.addEventListener('click', () => this.setDefault(button.dataset.defaultProfile || ''));
        });
        container.querySelector('[data-open-profile]')?.addEventListener('click', () => this.openProfileModal());
        container.querySelectorAll('[data-profile-login]').forEach(button => {
            button.addEventListener('click', () => this.startOAuth(button.dataset.profileLogin || '', false));
        });
        container.querySelectorAll('[data-sync-local-auth]').forEach(button => {
            button.addEventListener('click', () => this.syncLocalAuth(button.dataset.syncLocalAuth || '', button));
        });
        container.querySelectorAll('[data-edit-profile]').forEach(button => {
            button.addEventListener('click', () => this.openProfileModal(button.dataset.editProfile || ''));
        });
        container.querySelectorAll('[data-profile-skills]').forEach(button => {
            button.addEventListener('click', () => CodexSkills.open(button.dataset.profileSkills || ''));
        });
        container.querySelectorAll('[data-delete-profile]').forEach(button => {
            button.addEventListener('click', () => this.deleteProfile(button.dataset.deleteProfile || '', button));
        });
    },

    setupProfileEditor() {
        if (this.profileEditorBound) return;
        const form = document.getElementById('codexProfileForm');
        if (!form) return;
        this.profileEditorBound = true;
        form.querySelectorAll('input[name="profile_source"]').forEach(input => {
            input.addEventListener('change', () => this.setProfileSource(input.value));
        });
        document.getElementById('codexProfileCatalogProvider')?.addEventListener('change', event => {
            this.selectProfileProvider(event.target.value, { forceProvider: true });
        });
        const modelInput = document.getElementById('codexProfileModel');
        const capabilityInputs = [
            document.getElementById('codexProfileSupportsVision'),
            document.getElementById('codexProfileSupportsWebSearch')
        ].filter(Boolean);
        modelInput?.addEventListener('focus', () => {
            if (this.getProfileSource() === 'catalog') this.openProfileCatalog();
        });
        modelInput?.addEventListener('input', () => {
            capabilityInputs.forEach(input => {
                if (input.dataset.autoModel && input.dataset.autoModel !== modelInput.value) {
                    input.checked = false;
                    delete input.dataset.autoModel;
                }
            });
            if (this.getProfileSource() === 'catalog') this.renderProfileCatalog(modelInput.value);
        });
        modelInput?.addEventListener('keydown', event => {
            if (event.key === 'Escape') this.closeProfileCatalog();
        });
        form.addEventListener('submit', event => {
            event.preventDefault();
            this.saveProfile();
        });
        capabilityInputs.forEach(input => input.addEventListener('change', event => {
            if (event.isTrusted) delete event.target.dataset.autoModel;
        }));
        document.addEventListener('click', event => {
            if (!event.target.closest('.codex-profile-model-picker')) this.closeProfileCatalog();
        });
        document.getElementById('codexProfileModal')?.addEventListener('hidden.bs.modal', () => {
            const secret = document.getElementById('codexProfileApiKey');
            if (secret) {
                secret.value = '';
                secret.type = 'password';
            }
            this.editingProfile = null;
            this.closeProfileCatalog();
            if (!document.getElementById('addModelModal')?.classList.contains('show')
                && typeof LLMManager !== 'undefined') {
                LLMManager.clearLiteLLMUpdatePoll();
            }
        });
        window.addEventListener('beforeunload', () => {
            if (!this.oauthSetupDraft || !this.oauthProfile) return;
            navigator.sendBeacon(
                `/api/codex/profiles/${encodeURIComponent(this.oauthProfile)}/setup/cancel`
            );
        });
    },

    getProfileSource() {
        return document.querySelector('#codexProfileForm input[name="profile_source"]:checked')?.value || 'oauth';
    },

    updateLocalAuthChoice() {
        const input = document.querySelector('#codexProfileForm input[name="auth_source"][value="local_cache"]');
        const hint = document.getElementById('codexLocalAuthHint');
        const choice = document.getElementById('codexLocalAuthChoice');
        const available = Boolean(this.localAuth?.available);
        if (hint) hint.textContent = this.localAuth?.reason || (available ? '可导入本机 Codex 登录' : '本机登录不可导入');
        if (choice) choice.classList.toggle('opacity-50', !available);
        if (!input) return;
        input.disabled = Boolean(this.editingProfile) || !available;
        if (!this.editingProfile && !available && input.checked) {
            const device = document.querySelector('#codexProfileForm input[name="auth_source"][value="device_code"]');
            if (device) device.checked = true;
        }
    },

    async setProfileSource(source, { load = true } = {}) {
        const form = document.getElementById('codexProfileForm');
        if (!form) return;
        const oauthMode = source === 'oauth';
        const catalogMode = source === 'catalog';
        const officialWizard = oauthMode && !this.editingProfile;
        form.classList.toggle('codex-profile-mode-manual', !catalogMode && !oauthMode);
        form.classList.toggle('codex-profile-mode-oauth', oauthMode);
        form.querySelectorAll('.codex-profile-api-field').forEach(element => {
            element.classList.toggle('d-none', oauthMode);
        });
        form.querySelectorAll('.codex-profile-oauth-field').forEach(element => {
            element.classList.toggle('d-none', !oauthMode);
        });
        form.querySelectorAll('.codex-profile-catalog-field').forEach(element => {
            element.classList.toggle('d-none', !catalogMode);
        });
        form.querySelectorAll('.codex-profile-final-config-field').forEach(element => {
            element.classList.toggle('d-none', officialWizard);
        });
        const modelInput = document.getElementById('codexProfileModel');
        const baseUrl = document.getElementById('codexProfileBaseUrl');
        const apiKey = document.getElementById('codexProfileApiKey');
        const nameInput = document.getElementById('codexProfileName');
        const nameField = form.querySelector('.codex-profile-name-field');
        const saveButton = document.getElementById('saveCodexProfileButton');
        if (modelInput) modelInput.required = !oauthMode;
        if (baseUrl) baseUrl.required = !oauthMode;
        if (apiKey) apiKey.required = !oauthMode && !this.editingProfile;
        if (nameInput) nameInput.placeholder = oauthMode ? '例如 chatgpt-main' : '例如 deepseek-main';
        nameField?.classList.toggle('col-md-5', !officialWizard);
        nameField?.classList.toggle('col-12', officialWizard);
        if (saveButton && !this.editingProfile) {
            saveButton.textContent = officialWizard ? '下一步' : '创建 Profile';
        }
        if (oauthMode) {
            this.updateLocalAuthChoice();
            this.closeProfileCatalog();
            return;
        }
        if (modelInput) {
            modelInput.placeholder = catalogMode
                ? '输入或从 LiteLLM 目录选择'
                : '例如 deepseek-chat';
        }
        if (!catalogMode) {
            this.closeProfileCatalog();
            return;
        }
        if (!load) return;
        if (typeof LLMManager !== 'undefined') {
            void LLMManager.loadLiteLLMUpdateStatus({ quiet: true });
        }
        if (!this.catalogProviders.length) await this.loadProfileCatalogProviders();
        const providerSelect = document.getElementById('codexProfileCatalogProvider');
        const providerKey = providerSelect?.value
            || (this.catalogProviders.some(provider => provider.id === 'openai') ? 'openai' : this.catalogProviders[0]?.id || '');
        if (providerSelect && providerKey) providerSelect.value = providerKey;
        if (providerKey) await this.selectProfileProvider(providerKey, { resetModel: false });
    },

    async loadProfileCatalogProviders() {
        const select = document.getElementById('codexProfileCatalogProvider');
        if (select) {
            select.disabled = true;
            select.innerHTML = '<option value="">正在读取目录…</option>';
        }
        try {
            const response = await fetch('/api/llm/models/catalog');
            const result = await response.json();
            if (!response.ok || result.status !== 'success') {
                throw new Error(result.detail || result.message || '目录加载失败');
            }
            this.catalogProviders = Array.isArray(result.data?.providers) ? result.data.providers : [];
            if (!this.catalogProviders.length) throw new Error('LiteLLM 目录没有可用供应商');
        } catch (error) {
            console.warn('Codex Profile LiteLLM providers unavailable:', error);
            this.catalogProviders = [
                { id: 'openai', label: 'OpenAI', model_count: 0 },
                { id: 'deepseek', label: 'DeepSeek', model_count: 0 },
                { id: 'openrouter', label: 'OpenRouter', model_count: 0 }
            ];
        } finally {
            if (select) {
                const selected = select.value;
                select.innerHTML = this.catalogProviders.map(provider => `
                    <option value="${this.escape(provider.id)}">${this.escape(provider.label)}${provider.model_count ? ` · ${UI.formatNumber(provider.model_count)} 个模型` : ''}</option>`).join('');
                const next = this.catalogProviders.some(provider => provider.id === selected)
                    ? selected
                    : (this.catalogProviders.some(provider => provider.id === 'openai') ? 'openai' : this.catalogProviders[0]?.id || '');
                select.value = next;
                select.disabled = false;
            }
        }
    },

    getProfileProviderLabel(providerKey) {
        return this.catalogProviders.find(provider => provider.id === providerKey)?.label
            || String(providerKey || '').replaceAll('_', ' ').replace(/\b\w/g, character => character.toUpperCase());
    },

    async selectProfileProvider(providerKey, { resetModel = true, forceProvider = false } = {}) {
        if (!providerKey) return;
        const providerName = document.getElementById('codexProfileProviderName');
        const baseUrl = document.getElementById('codexProfileBaseUrl');
        const modelInput = document.getElementById('codexProfileModel');
        const label = this.getProfileProviderLabel(providerKey);
        const suggestedBase = this.profileProviderBases[providerKey] || '';
        if (providerName) {
            const previousAuto = providerName.dataset.autoValue || '';
            if (forceProvider || !providerName.value || providerName.value === previousAuto) providerName.value = label;
            providerName.dataset.autoValue = label;
        }
        if (baseUrl) {
            const previousAuto = baseUrl.dataset.autoValue || '';
            if (forceProvider || !baseUrl.value || baseUrl.value === previousAuto) baseUrl.value = suggestedBase;
            baseUrl.dataset.autoValue = suggestedBase;
        }
        if (resetModel && modelInput) modelInput.value = '';
        if (resetModel) {
            [
                document.getElementById('codexProfileSupportsVision'),
                document.getElementById('codexProfileSupportsWebSearch')
            ].filter(Boolean).forEach(input => {
                input.checked = false;
                delete input.dataset.autoModel;
            });
        }
        await this.loadProfileCatalog(providerKey);
    },

    async loadProfileCatalog(providerKey) {
        const requestId = ++this.profileCatalogRequestId;
        try {
            const response = await fetch(`/api/llm/models/catalog?provider=${encodeURIComponent(providerKey)}&limit=300`);
            const result = await response.json();
            if (requestId !== this.profileCatalogRequestId) return;
            if (!response.ok || result.status !== 'success') {
                throw new Error(result.detail || result.message || '模型目录加载失败');
            }
            this.profileCatalogModels = Array.isArray(result.data?.models) ? result.data.models : [];
        } catch (error) {
            if (requestId !== this.profileCatalogRequestId) return;
            console.warn(`Codex Profile catalog unavailable for ${providerKey}:`, error);
            this.profileCatalogModels = [];
        }
        this.renderProfileCatalog(document.getElementById('codexProfileModel')?.value || '');
    },

    getNativeProfileModelId(item) {
        const modelId = String(item?.provider_model_id || item?.id || '');
        const providerKey = document.getElementById('codexProfileCatalogProvider')?.value || '';
        const prefix = `${providerKey}/`;
        return providerKey && modelId.startsWith(prefix) ? modelId.slice(prefix.length) : modelId;
    },

    formatTokenCount(value) {
        const count = Number(value || 0);
        if (!count) return '';
        if (count >= 1000000) return `${(count / 1000000).toFixed(count % 1000000 ? 1 : 0)}M`;
        if (count >= 1000) return `${Math.round(count / 1000)}K`;
        return UI.formatNumber(count);
    },

    renderProfileCatalog(query = '') {
        const menu = document.getElementById('codexProfileCatalogMenu');
        if (!menu) return;
        const needle = String(query || '').trim().toLowerCase();
        const matches = this.profileCatalogModels.filter(item => {
            const nativeId = this.getNativeProfileModelId(item);
            return !needle || nativeId.toLowerCase().includes(needle) || String(item.id || '').toLowerCase().includes(needle);
        }).slice(0, 40);
        if (!matches.length) {
            menu.innerHTML = `
                <div class="model-catalog-empty">
                    <i class="bi bi-pencil-square"></i>
                    <strong>${this.profileCatalogModels.length ? '目录中没有匹配项' : '当前没有可用目录'}</strong>
                    <span>可以继续手动输入 Responses API 接受的模型 ID。</span>
                </div>`;
            return;
        }
        menu.innerHTML = matches.map(item => {
            const modelId = this.getNativeProfileModelId(item);
            const capabilities = [];
            if (item.supports_reasoning) capabilities.push('推理');
            if (item.supports_vision) capabilities.push('图片');
            if (item.supports_web_search) capabilities.push('搜索');
            const context = item.max_input_tokens ? `上下文 ${this.formatTokenCount(item.max_input_tokens)}` : '';
            return `
                <button type="button" class="model-catalog-item" role="option" data-codex-profile-model="${this.escape(modelId)}">
                    <span class="model-catalog-item-main"><strong>${this.escape(modelId)}</strong><small>${item.recommended ? '常用名称' : '版本/预览模型'}${context ? ` · ${context}` : ''}</small></span>
                    <span class="model-catalog-capabilities">${capabilities.map(value => `<em>${value}</em>`).join('')}</span>
                </button>`;
        }).join('');
        menu.querySelectorAll('[data-codex-profile-model]').forEach(button => {
            button.addEventListener('click', () => this.selectProfileCatalogModel(button.dataset.codexProfileModel));
        });
    },

    selectProfileCatalogModel(modelId) {
        const item = this.profileCatalogModels.find(candidate => this.getNativeProfileModelId(candidate) === modelId);
        const modelInput = document.getElementById('codexProfileModel');
        const contextInput = document.querySelector('#codexProfileForm [name="context_window"]');
        const nameInput = document.getElementById('codexProfileName');
        const visionInput = document.getElementById('codexProfileSupportsVision');
        const webSearchInput = document.getElementById('codexProfileSupportsWebSearch');
        if (modelInput) modelInput.value = modelId;
        if (contextInput && item?.max_input_tokens) contextInput.value = item.max_input_tokens;
        [
            [visionInput, item?.supports_vision],
            [webSearchInput, item?.supports_web_search]
        ].forEach(([input, supported]) => {
            if (!input) return;
            input.checked = Boolean(supported);
            input.dataset.autoModel = modelId;
        });
        if (nameInput && !nameInput.value) {
            nameInput.value = String(modelId).toLowerCase()
                .replace(/[^a-z0-9_-]+/g, '-')
                .replace(/^-+|-+$/g, '')
                .slice(0, 48);
        }
        this.closeProfileCatalog();
    },

    openProfileCatalog() {
        if (this.getProfileSource() !== 'catalog') return;
        const menu = document.getElementById('codexProfileCatalogMenu');
        const input = document.getElementById('codexProfileModel');
        if (!menu || !input) return;
        this.renderProfileCatalog(input.value);
        menu.classList.remove('d-none');
        input.setAttribute('aria-expanded', 'true');
    },

    closeProfileCatalog() {
        document.getElementById('codexProfileCatalogMenu')?.classList.add('d-none');
        document.getElementById('codexProfileModel')?.setAttribute('aria-expanded', 'false');
    },

    toggleProfileCatalog() {
        const menu = document.getElementById('codexProfileCatalogMenu');
        if (!menu) return;
        if (menu.classList.contains('d-none')) {
            this.openProfileCatalog();
            document.getElementById('codexProfileModel')?.focus();
        } else {
            this.closeProfileCatalog();
        }
    },

    async openProfileModal(profileName = '') {
        const form = document.getElementById('codexProfileForm');
        if (!form) return;
        this.setupProfileEditor();
        const profile = profileName
            ? this.profiles.find(item => item.name === profileName)
            : null;
        if (profileName && !profile) {
            UI.showError('Codex Profile 不存在，请刷新后重试');
            return;
        }
        if (profile?.auth_type === 'chatgpt') {
            await this.startOAuth(
                profile.name,
                Boolean(profile.is_default),
                false,
                null,
                {
                    authSource: profile.auth_source || 'device_code',
                    editingConfiguration: true
                }
            );
            return;
        }
        this.editingProfile = profile;
        form.reset();
        this.profileCatalogModels = [];
        form.querySelectorAll('input[name="profile_source"]').forEach(input => {
            input.disabled = false;
        });
        form.querySelectorAll('input[name="auth_source"]').forEach(input => {
            input.disabled = false;
        });
        const providerName = document.getElementById('codexProfileProviderName');
        const baseUrl = document.getElementById('codexProfileBaseUrl');
        const nameInput = document.getElementById('codexProfileName');
        const modelInput = document.getElementById('codexProfileModel');
        const apiKey = document.getElementById('codexProfileApiKey');
        const apiKeyHint = document.getElementById('codexProfileApiKeyHint');
        const visionInput = document.getElementById('codexProfileSupportsVision');
        const webSearchInput = document.getElementById('codexProfileSupportsWebSearch');
        const modalTitle = document.getElementById('codexProfileModalTitle');
        const modalSubtitle = document.getElementById('codexProfileModalSubtitle');
        const saveButton = document.getElementById('saveCodexProfileButton');
        if (providerName) delete providerName.dataset.autoValue;
        if (baseUrl) delete baseUrl.dataset.autoValue;
        if (visionInput) delete visionInput.dataset.autoModel;
        if (webSearchInput) delete webSearchInput.dataset.autoModel;
        if (nameInput) nameInput.readOnly = Boolean(profile);
        if (apiKey) apiKey.placeholder = '';

        let source = 'oauth';
        if (profile) {
            source = profile.auth_type === 'chatgpt' ? 'oauth' : 'manual';
            if (nameInput) nameInput.value = profile.name || '';
            if (modelInput) modelInput.value = profile.model || '';
            if (providerName) providerName.value = profile.provider_name || '';
            if (baseUrl) baseUrl.value = profile.base_url || '';
            if (form.elements.reasoning_effort) form.elements.reasoning_effort.value = profile.reasoning_effort || 'high';
            if (form.elements.model_verbosity) form.elements.model_verbosity.value = profile.model_verbosity || 'inherit';
            if (form.elements.context_window) form.elements.context_window.value = Number(profile.context_window || 128000);
            if (form.elements.make_default) {
                form.elements.make_default.checked = Boolean(profile.is_default);
                form.elements.make_default.disabled = Boolean(profile.is_default);
            }
            if (visionInput) visionInput.checked = Boolean(profile.supports_vision);
            if (webSearchInput) webSearchInput.checked = Boolean(profile.supports_web_search);
            form.querySelectorAll('input[name="profile_source"]').forEach(input => {
                input.checked = input.value === source;
                input.disabled = profile.auth_type === 'chatgpt' || input.value === 'oauth';
            });
            form.querySelectorAll('input[name="auth_source"]').forEach(input => {
                input.checked = input.value === (profile.auth_source || 'device_code');
                input.disabled = true;
            });
            if (apiKey) apiKey.placeholder = '留空则继续使用当前 API Key';
            if (apiKeyHint) apiKeyHint.textContent = '留空不会修改现有密钥；输入新 Key 后保存即可完成轮换。密钥仍只存入权限为 0600 的独立文件。';
            if (modalTitle) modalTitle.textContent = `编辑 Codex Profile · ${profile.name}`;
            if (modalSubtitle) modalSubtitle.textContent = profile.auth_type === 'chatgpt'
                ? '调整模型参数；如需更换账号，请重新完成 ChatGPT 授权。'
                : '可修改接口、模型参数或轮换 API Key。';
            if (saveButton) saveButton.textContent = '保存更改';
        } else {
            const oauthSource = form.querySelector('input[name="profile_source"][value="oauth"]');
            if (oauthSource) oauthSource.checked = true;
            if (form.elements.make_default) {
                form.elements.make_default.checked = true;
                form.elements.make_default.disabled = false;
            }
            if (apiKeyHint) apiKeyHint.textContent = '密钥通过标准输入交给 WSL，并存入权限为 0600 的独立文件；不会写入 Profile 清单或返回网页。';
            if (modalTitle) modalTitle.textContent = '新建 Codex Profile';
            if (modalSubtitle) modalSubtitle.textContent = '使用 ChatGPT 官方登录或连接 Responses 兼容接口。';
            if (saveButton) saveButton.textContent = '创建 Profile';
        }
        this.updateLocalAuthChoice();
        await this.setProfileSource(source, { load: false });
        bootstrap.Modal.getOrCreateInstance(document.getElementById('codexProfileModal')).show();
    },

    async saveProfile() {
        if (this.editingProfile) {
            await this.updateProfile(this.editingProfile);
            return;
        }
        await this.createProfile();
    },

    async updateProfile(profile) {
        const form = document.getElementById('codexProfileForm');
        if (!profile || !form?.reportValidity()) return;
        const values = new FormData(form);
        const payload = {
            reasoning_effort: String(values.get('reasoning_effort') || 'high'),
            model_verbosity: String(values.get('model_verbosity') || 'inherit'),
            context_window: Number(values.get('context_window') || 128000),
            supports_vision: values.get('supports_vision') === 'on',
            supports_web_search: values.get('supports_web_search') === 'on'
        };
        const secret = String(values.get('api_key') || '');
        if (profile.auth_type === 'api_key') {
            payload.model = String(values.get('model') || '').trim();
            payload.provider_name = String(values.get('provider_name') || '').trim();
            payload.base_url = String(values.get('base_url') || '').trim();
            if (secret) payload.api_key = secret;
        }
        const button = document.getElementById('saveCodexProfileButton');
        if (button) button.disabled = true;
        try {
            await API.codexProfiles.update(profile.name, payload);
            if (form.elements.make_default?.checked && !profile.is_default) {
                await API.codexProfiles.setDefault(profile.name);
            }
            const modalElement = document.getElementById('codexProfileModal');
            const modal = bootstrap.Modal.getInstance(modalElement);
            const hidden = modalElement && modalElement.classList.contains('show')
                ? new Promise(resolve => modalElement.addEventListener('hidden.bs.modal', resolve, { once: true }))
                : Promise.resolve();
            modal?.hide();
            await hidden;
            await this.load();
            UI.showSuccess(secret ? 'Codex Profile 已更新，新的 API Key 已保存' : 'Codex Profile 已更新');
        } catch (error) {
            UI.showError(`更新 Profile 失败：${error.message}`);
        } finally {
            if (button) button.disabled = false;
            const apiKey = form.elements.api_key;
            if (apiKey) apiKey.value = '';
        }
    },

    async createProfile() {
        const form = document.getElementById('codexProfileForm');
        if (!form?.reportValidity()) return;
        const values = new FormData(form);
        const source = this.getProfileSource();
        const oauthMode = source === 'oauth';
        const payload = {
            name: String(values.get('name') || '').trim(),
            auth_type: oauthMode ? 'chatgpt' : 'api_key',
            auth_source: oauthMode ? String(values.get('auth_source') || 'device_code') : 'device_code',
            model: oauthMode ? '' : String(values.get('model') || '').trim(),
            provider_name: oauthMode ? 'ChatGPT 官方登录' : String(values.get('provider_name') || '').trim(),
            base_url: oauthMode ? '' : String(values.get('base_url') || '').trim(),
            api_key: oauthMode ? '' : String(values.get('api_key') || ''),
            reasoning_effort: String(values.get('reasoning_effort') || 'high'),
            model_verbosity: String(values.get('model_verbosity') || 'inherit'),
            context_window: Number(values.get('context_window') || 128000),
            supports_vision: oauthMode || values.get('supports_vision') === 'on',
            supports_web_search: oauthMode || values.get('supports_web_search') === 'on',
            make_default: oauthMode ? false : values.get('make_default') === 'on',
            setup_pending: oauthMode
        };
        const button = document.getElementById('saveCodexProfileButton');
        if (button) button.disabled = true;
        try {
            const result = await API.codexProfiles.create(payload);
            const profileModalElement = document.getElementById('codexProfileModal');
            const profileModal = bootstrap.Modal.getInstance(profileModalElement);
            const hidden = profileModalElement && profileModalElement.classList.contains('show')
                ? new Promise(resolve => profileModalElement.addEventListener('hidden.bs.modal', resolve, { once: true }))
                : Promise.resolve();
            profileModal?.hide();
            await hidden;
            if (oauthMode && result.oauth_status) {
                await this.startOAuth(
                    result.profile?.name || payload.name,
                    true,
                    false,
                    result.oauth_status,
                    { setupDraft: true, authSource: payload.auth_source }
                );
            } else if (oauthMode && result.requires_login) {
                await this.startOAuth(
                    result.profile?.name || payload.name,
                    true,
                    false,
                    null,
                    { setupDraft: true, authSource: payload.auth_source }
                );
            } else {
                await this.load();
                UI.showSuccess('Codex Profile 已创建');
            }
        } catch (error) {
            UI.showError(`创建 Profile 失败：${error.message}`);
        } finally {
            if (button) button.disabled = false;
            const secret = form.elements.api_key;
            if (secret) secret.value = '';
        }
    },

    async setDefault(profileId) {
        if (!profileId) return;
        try {
            await API.codexProfiles.setDefault(profileId);
            UI.showSuccess(`默认 Profile 已切换为 ${profileId}`);
            await this.load();
        } catch (error) {
            UI.showError(`切换失败：${error.message}`);
        }
    },

    async syncLocalAuth(profileName, button = null) {
        if (!profileName) return;
        if (button) button.disabled = true;
        try {
            await API.codexProfiles.syncLocalAuth(profileName);
            await this.load();
            UI.showSuccess(`已同步并验证 ${profileName} 的本机 Codex 登录`);
        } catch (error) {
            UI.showError(`同步本机登录失败：${error.message}`);
            if (button) button.disabled = false;
        }
    },

    async deleteProfile(profileName, button = null) {
        const profile = this.profiles.find(item => item.name === profileName);
        if (!profile) {
            UI.showError('Codex Profile 不存在，请刷新后重试');
            return;
        }
        const defaultNotice = profile.is_default
            ? '\n它当前是默认 Profile；删除后会自动选择另一个可用 Profile，如无可用项则需要重新配置。'
            : '';
        const confirmed = await UI.confirm(
            `确定永久删除 Codex Profile “${profileName}”吗？${defaultNotice}\n引用它的聊天会恢复为继承默认 Profile。此操作无法撤销。`,
            {
                title: '删除 Codex Profile',
                confirmText: '永久删除',
                variant: 'danger'
            }
        );
        if (!confirmed) return;
        if (button) button.disabled = true;
        try {
            const result = await API.codexProfiles.delete(profileName);
            await this.load();
            const resetCount = Number(result.chat_bindings_cleared || 0)
                + Number(result.model_bindings_cleared || 0);
            UI.showSuccess(
                resetCount
                    ? `Codex Profile 已删除，并重置 ${resetCount} 处引用`
                    : 'Codex Profile 已删除'
            );
        } catch (error) {
            UI.showError(`删除 Profile 失败：${error.message}`);
            if (button) button.disabled = false;
        }
    },

    resetOAuthView() {
        clearTimeout(this.oauthPollTimer);
        this.oauthPollTimer = null;
        this.oauthModels = [];
        document.getElementById('codexOAuthAlert')?.classList.add('d-none');
        document.getElementById('codexOAuthPending')?.classList.remove('d-none');
        document.getElementById('codexOAuthConnected')?.classList.add('d-none');
        document.getElementById('codexOAuthFinish')?.classList.add('d-none');
        const cancel = document.getElementById('codexOAuthCancel');
        if (cancel) cancel.textContent = '取消授权';
        const finish = document.getElementById('codexOAuthFinish');
        if (finish) finish.textContent = this.oauthSetupDraft
            ? '完成创建'
            : (this.oauthEditingProfile ? '保存更改' : '保存并使用');
        const editingProfile = this.oauthEditingProfile
            ? this.profiles.find(item => item.name === this.oauthProfile)
            : null;
        const makeDefault = document.getElementById('codexOAuthMakeDefault');
        if (makeDefault) {
            makeDefault.checked = this.oauthMakeDefault;
            makeDefault.disabled = Boolean(editingProfile?.is_default);
        }
        const verbosity = document.getElementById('codexOAuthVerbosity');
        if (verbosity) verbosity.value = editingProfile?.model_verbosity || 'inherit';
    },

    async startOAuth(
        profileName,
        makeDefault = false,
        force = false,
        initialStatus = null,
        { setupDraft = false, authSource = '', editingConfiguration = false } = {}
    ) {
        if (!profileName) return;
        this.oauthProfile = profileName;
        this.oauthMakeDefault = Boolean(makeDefault);
        this.oauthSetupDraft = Boolean(setupDraft);
        this.oauthEditingProfile = Boolean(editingConfiguration);
        this.resetOAuthView();
        const profile = this.profiles.find(item => item.name === profileName);
        this.oauthAuthSource = authSource || profile?.auth_source || 'device_code';
        const importedLogin = this.oauthAuthSource === 'local_cache';
        const title = document.getElementById('codexOAuthModalTitle');
        if (title) title.textContent = this.oauthSetupDraft
            ? '配置官方 Codex Profile'
            : (this.oauthEditingProfile ? `编辑官方 Profile · ${profileName}` : '绑定 ChatGPT');
        const subtitle = document.getElementById('codexOAuthSubtitle');
        if (subtitle) subtitle.textContent = this.oauthEditingProfile
            ? `${profileName} · 从当前 ChatGPT 账号的可用目录选择模型`
            : (importedLogin
                ? `${profileName} · 导入并验证本机 Codex 登录副本`
                : `${profileName} · 扫码完成 Codex 官方授权`);
        bootstrap.Modal.getOrCreateInstance(document.getElementById('codexOAuthModal')).show();
        try {
            const status = initialStatus || await API.codexProfiles.startOAuth(profileName, force);
            this.renderOAuthStatus(status);
        } catch (error) {
            this.showOAuthError(error.message);
        }
    },

    renderOAuthStatus(status) {
        const state = String(status?.status || 'idle');
        if (state === 'pending') {
            const loginId = String(status.login_id || '');
            const link = document.getElementById('codexOAuthLink');
            if (link) link.href = status.verification_url || '#';
            const code = document.getElementById('codexOAuthCode');
            if (code) code.textContent = status.user_code || '';
            const qr = document.getElementById('codexOAuthQr');
            if (qr && loginId) {
                qr.src = `/api/codex/profiles/${encodeURIComponent(this.oauthProfile)}/oauth/qr?login_id=${encodeURIComponent(loginId)}`;
            }
            const seconds = Math.max(0, Number(status.expires_in || 0));
            const countdown = document.getElementById('codexOAuthCountdown');
            if (countdown) countdown.textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')} 后过期`;
            this.oauthPollTimer = setTimeout(() => this.pollOAuth(), 1200);
            return;
        }
        clearTimeout(this.oauthPollTimer);
        this.oauthPollTimer = null;
        if (state === 'connected') {
            document.getElementById('codexOAuthPending')?.classList.add('d-none');
            document.getElementById('codexOAuthConnected')?.classList.remove('d-none');
            const account = status.account || {};
            const accountLabel = [account.email, account.plan_type].filter(Boolean).join(' · ') || 'ChatGPT 账号';
            const accountElement = document.getElementById('codexOAuthAccount');
            if (accountElement) accountElement.textContent = accountLabel;
            const subtitle = document.getElementById('codexOAuthSubtitle');
            if (subtitle && this.oauthSetupDraft) {
                subtitle.textContent = `${this.oauthProfile} · 登录完成，请选择账号可用配置`;
            }
            this.oauthModels = Array.isArray(status.models) ? status.models : [];
            const editingProfile = this.oauthEditingProfile
                ? this.profiles.find(item => item.name === this.oauthProfile)
                : null;
            const configuredModelId = String(editingProfile?.model || '');
            const hasConfiguredModel = this.oauthModels.some(model => model.id === configuredModelId);
            const select = document.getElementById('codexOAuthModel');
            if (select) {
                select.innerHTML = this.oauthModels.map(model => `<option value="${this.escape(model.id)}" ${model.is_default ? 'selected' : ''}>${this.escape(model.display_name || model.id)} · ${this.escape(model.id)}</option>`).join('');
                if (hasConfiguredModel) select.value = configuredModelId;
                select.disabled = !this.oauthModels.length;
            }
            this.syncOAuthModelConfig({ preserveProfile: hasConfiguredModel });
            const finish = document.getElementById('codexOAuthFinish');
            finish?.classList.remove('d-none');
            if (finish) finish.disabled = !this.oauthModels.length;
            const cancel = document.getElementById('codexOAuthCancel');
            if (cancel) cancel.textContent = this.oauthSetupDraft
                ? '放弃创建'
                : (this.oauthEditingProfile ? '取消' : '稍后使用');
            return;
        }
        this.showOAuthError(status?.error || (state === 'expired' ? '授权已过期，请重新发起登录。' : 'ChatGPT 授权未完成。'));
    },

    async pollOAuth() {
        if (!this.oauthProfile) return;
        try {
            this.renderOAuthStatus(await API.codexProfiles.getOAuth(this.oauthProfile));
        } catch (error) {
            this.showOAuthError(error.message);
        }
    },

    showOAuthError(message) {
        clearTimeout(this.oauthPollTimer);
        this.oauthPollTimer = null;
        const alert = document.getElementById('codexOAuthAlert');
        if (alert) {
            alert.textContent = message || 'ChatGPT 授权失败';
            alert.classList.remove('d-none');
        }
        const cancel = document.getElementById('codexOAuthCancel');
        if (cancel) cancel.textContent = '关闭';
    },

    async copyOAuthCode() {
        const code = document.getElementById('codexOAuthCode')?.textContent || '';
        if (!code) return;
        try {
            await navigator.clipboard.writeText(code);
            UI.showSuccess('验证码已复制');
        } catch (error) {
            UI.showError('复制失败，请手动输入验证码');
        }
    },

    syncOAuthModelConfig({ preserveProfile = false } = {}) {
        const modelId = document.getElementById('codexOAuthModel')?.value || '';
        const model = this.oauthModels.find(item => item.id === modelId) || {};
        const efforts = Array.isArray(model.supported_reasoning_efforts)
            ? model.supported_reasoning_efforts.filter(Boolean)
            : [];
        const defaultEffort = efforts.includes(model.default_reasoning_effort)
            ? model.default_reasoning_effort
            : (efforts[0] || model.default_reasoning_effort || 'high');
        const editingProfile = preserveProfile && this.oauthEditingProfile
            ? this.profiles.find(item => item.name === this.oauthProfile)
            : null;
        const configuredEffort = String(editingProfile?.reasoning_effort || '');
        const selectedEffort = configuredEffort && (!efforts.length || efforts.includes(configuredEffort))
            ? configuredEffort
            : defaultEffort;
        const reasoning = document.getElementById('codexOAuthReasoning');
        if (reasoning) {
            const values = efforts.length ? efforts : [selectedEffort];
            reasoning.innerHTML = values.map(value => `<option value="${this.escape(value)}">${this.escape(value)}</option>`).join('');
            reasoning.value = selectedEffort;
            reasoning.disabled = !modelId;
        }
        const contextWindow = Number(model.context_window || 0);
        const context = document.getElementById('codexOAuthContextWindow');
        if (context) context.value = contextWindow >= 4096
            ? `${UI.formatNumber(contextWindow)} tokens`
            : '由 Codex 账号配置决定';
        const capabilities = [];
        if ((model.input_modalities || []).includes('image')) capabilities.push('图片输入');
        if (model.supports_web_search) capabilities.push('原生 Web 搜索');
        const capabilityElement = document.getElementById('codexOAuthCapabilities');
        if (capabilityElement) capabilityElement.textContent = capabilities.length
            ? `账号目录声明的能力：${capabilities.join(' · ')}`
            : '账号目录未声明额外能力。';
        const description = document.getElementById('codexOAuthModelDescription');
        if (description) description.textContent = model.description
            || '模型来自当前账号的 Codex 目录。';
    },

    async cancelOAuth() {
        clearTimeout(this.oauthPollTimer);
        this.oauthPollTimer = null;
        const profileName = this.oauthProfile;
        const setupDraft = this.oauthSetupDraft;
        const pending = !document.getElementById('codexOAuthPending')?.classList.contains('d-none');
        if (profileName) {
            try {
                if (setupDraft) {
                    await API.codexProfiles.cancelSetup(profileName);
                } else if (pending) {
                    await API.codexProfiles.cancelOAuth(profileName);
                }
            } catch (error) {
                if (setupDraft) {
                    this.showOAuthError(`清理未完成的 Profile 失败：${error.message}`);
                    return;
                }
                // The app-server may already have completed or expired the login.
            }
        }
        this.oauthSetupDraft = false;
        this.oauthEditingProfile = false;
        this.oauthProfile = '';
        bootstrap.Modal.getInstance(document.getElementById('codexOAuthModal'))?.hide();
        await this.load();
    },

    async closeOAuth() {
        await this.cancelOAuth();
    },

    async finishOAuth() {
        const profileName = this.oauthProfile;
        const editingConfiguration = this.oauthEditingProfile;
        const modelId = document.getElementById('codexOAuthModel')?.value || '';
        const button = document.getElementById('codexOAuthFinish');
        if (!profileName || !modelId) {
            this.showOAuthError('当前账号没有返回可用的 Codex 模型。');
            return;
        }
        const model = this.oauthModels.find(item => item.id === modelId) || {};
        const payload = {
            model: modelId,
            reasoning_effort: document.getElementById('codexOAuthReasoning')?.value || model.default_reasoning_effort || 'high',
            model_verbosity: document.getElementById('codexOAuthVerbosity')?.value || 'inherit',
            make_default: Boolean(document.getElementById('codexOAuthMakeDefault')?.checked)
        };
        if (Number(model.context_window || 0) >= 4096) payload.context_window = Number(model.context_window);
        payload.supports_vision = (model.input_modalities || []).includes('image');
        payload.supports_web_search = Boolean(model.supports_web_search);
        if (button) button.disabled = true;
        try {
            let result = null;
            if (this.oauthSetupDraft) {
                result = await API.codexProfiles.finalizeSetup(profileName, {
                    model: payload.model,
                    reasoning_effort: payload.reasoning_effort,
                    model_verbosity: payload.model_verbosity,
                    make_default: payload.make_default
                });
            } else {
                await API.codexProfiles.update(profileName, payload);
                if (payload.make_default) await API.codexProfiles.setDefault(profileName);
            }
            const madeDefault = payload.make_default
                || result?.default_profile_id === profileName;
            this.oauthSetupDraft = false;
            this.oauthEditingProfile = false;
            this.oauthProfile = '';
            bootstrap.Modal.getInstance(document.getElementById('codexOAuthModal'))?.hide();
            UI.showSuccess(editingConfiguration
                ? '官方 Codex Profile 已更新'
                : (madeDefault ? 'ChatGPT 已连接并设为默认 Profile' : 'ChatGPT Profile 已创建'));
            await this.load();
        } catch (error) {
            this.showOAuthError(error.message);
        } finally {
            if (button) button.disabled = false;
        }
    }
};
