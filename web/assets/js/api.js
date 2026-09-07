/**
 * API Module
 * Handles all server communication
 */

const API = {
    // Base Utils
    async request(url, options = {}) {
        try {
            const response = await fetch(url, options);
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                const detail = error.detail || error.message;
                const message = typeof detail === 'object'
                    ? (detail.message || JSON.stringify(detail))
                    : detail;
                throw new Error(message || `HTTP ${response.status}`);
            }
            return await response.json();
        } catch (error) {
            if (error.name !== 'AbortError') {
                console.error(`API Error (${url}):`, error);
            }
            throw error;
        }
    },

    async get(url, options = {}) {
        return this.request(url, options);
    },

    async post(url, data) {
        return this.request(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
    },

    async put(url, data) {
        return this.request(url, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
    },

    async delete(url) {
        return this.request(url, { method: 'DELETE' });
    },

    // System
    system: {
        getInfo: () => API.get('/api/system/info'),
        getStatus: () => API.get('/api/system/status'),
        getHealthDetails: () => API.get('/api/system/health/details'),
        getLogs: (type, lines, search, plugin, options = {}) => {
            const params = new URLSearchParams({
                lines: lines || 100,
                type: type || 'app'
            });
            if (search) params.append('search', search);
            if (plugin) params.append('plugin_name', plugin);
            return API.get(`/api/system/logs/${type}?${params.toString()}`, options);
        },
        restart: (serviceName) => API.post(`/api/system/restart/${serviceName}`),
        getRestartCapabilities: () => API.get('/api/system/restart-capabilities'),
        getProcesses: () => API.get('/api/system/processes'),
        controlBot: (action) => API.post(`/api/system/control/bot/${encodeURIComponent(action)}`),
        checkHealth: () => API.get('/health')
    },

    systemTools: {
        getOverview: (refresh = false) => API.get(`/api/system/tools${refresh ? '?refresh=true' : ''}`),
        checkAll: () => API.post('/api/system/tools/check', {}),
        check: toolId => API.post(`/api/system/tools/${encodeURIComponent(toolId)}/check`, {}),
        update: toolId => API.post(`/api/system/tools/${encodeURIComponent(toolId)}/update`, {}),
        rollback: toolId => API.post(`/api/system/tools/${encodeURIComponent(toolId)}/rollback`, {}),
        repair: toolId => API.post(`/api/system/tools/${encodeURIComponent(toolId)}/repair`, {})
    },

    litellm: {
        getStatus: () => API.get('/api/llm/litellm/status'),
        checkUpdate: () => API.post('/api/llm/litellm/check'),
        startUpdate: () => API.post('/api/llm/litellm/update')
    },

    backups: {
        getOverview: () => API.get('/api/backups/'),
        create: options => API.post('/api/backups/', options),
        validate: name => API.post(`/api/backups/${encodeURIComponent(name)}/validate`, {}),
        prepareRestore: (name, confirmation) => API.post(
            `/api/backups/${encodeURIComponent(name)}/prepare-restore`,
            { confirmation }
        ),
        delete: (name, confirmation) => API.request(
            `/api/backups/${encodeURIComponent(name)}`,
            {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ confirmation })
            }
        ),
        downloadUrl: name => `/api/backups/${encodeURIComponent(name)}/download`,
        importFile: async file => {
            const response = await fetch(
                `/api/backups/import?filename=${encodeURIComponent(file.name)}`,
                { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file }
            );
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || error.message || `HTTP ${response.status}`);
            }
            return response.json();
        }
    },

    operations: {
        getAll: (limit = 100, owner = '') => API.get(
            `/api/operations/?limit=${encodeURIComponent(limit)}${owner ? `&owner=${encodeURIComponent(owner)}` : ''}`
        ),
        get: id => API.get(`/api/operations/${encodeURIComponent(id)}`),
        cancel: id => API.post(`/api/operations/${encodeURIComponent(id)}/cancel`, {}),
        getRuntime: () => API.get('/api/operations/runtime'),
        getIncidents: (limit = 40) => API.get(`/api/operations/incidents?limit=${encodeURIComponent(limit)}`),
        getAudit: (limit = 50) => API.get(`/api/operations/audit?limit=${encodeURIComponent(limit)}`),
        getStorage: () => API.get('/api/operations/storage'),
        scanStorage: () => API.post('/api/operations/storage/scan', {}),
        getCleanupPreview: (days = 7) => API.get(`/api/operations/storage/cleanup-preview?retention_days=${encodeURIComponent(days)}`),
        cleanupStorage: (days, confirmation) => API.post('/api/operations/storage/cleanup', { retention_days: days, confirmation })
    },

    // Plugins
    plugins: {
        getAll: () => API.get('/api/plugins/'),
        getStats: () => API.get('/api/plugins/stats'),
        toggle: (name, enabled) => API.post(`/api/plugins/${name}/toggle`, { enabled }),
        reload: (name) => API.post(`/api/plugins/${name}/reload`),
        // Configuration reads and writes use API.capabilities so values are
        // normalized, validated and secrets are never echoed back.
    },

    // Product-facing capabilities. Unlike the legacy plugin endpoints these
    // responses contain normalized metadata and redacted settings only.
    capabilities: {
        getAll: () => API.get('/api/capabilities/'),
        getDetail: (id) => API.get(`/api/capabilities/${encodeURIComponent(id)}`),
        getSettings: (id) => API.get(`/api/capabilities/settings/${encodeURIComponent(id)}`),
        updateSettings: (id, values) => API.put(
            `/api/capabilities/settings/${encodeURIComponent(id)}`,
            { values }
        )
    },

    // Runtime message routing and per-event execution order.
    automation: {
        getOverview: ({ chatId = null, mentioned = true } = {}) => {
            const params = new URLSearchParams({ mentioned: mentioned ? 'true' : 'false' });
            if (chatId !== null && chatId !== undefined && chatId !== '') {
                params.set('chat_id', String(chatId));
            }
            return API.get(`/api/automation/overview?${params.toString()}`);
        },
        updateOrder: (eventType, listenerKeys, expectedSignature = null) => API.put(
            `/api/automation/events/${encodeURIComponent(eventType)}/order`,
            { listener_keys: listenerKeys, expected_signature: expectedSignature }
        )
    },

    // First-class AI assistant console. This endpoint aggregates roles,
    // judges, chat overrides and the effective global state in one request.
    assistant: {
        getOverview: () => API.get('/api/assistant/overview'),
        updateChat: (userId, changes) => API.request(`/api/assistant/chats/${userId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(changes)
        })
    },

    chatPolicies: {
        get: userId => API.get(`/api/chats/${encodeURIComponent(userId)}/policy`),
        update: (userId, policy) => API.request(`/api/chats/${encodeURIComponent(userId)}/policy`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(policy)
        })
    },

    codexProfiles: {
        list: () => API.get('/api/codex/profiles'),
        create: payload => API.post('/api/codex/profiles', payload),
        update: (profileId, payload) => API.request(`/api/codex/profiles/${encodeURIComponent(profileId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        }),
        delete: profileId => API.delete(`/api/codex/profiles/${encodeURIComponent(profileId)}`),
        setDefault: profileId => API.put('/api/codex/profiles/default/selection', { profile_id: profileId }),
        syncLocalAuth: profileId => API.post(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/auth/sync`,
            {}
        ),
        startOAuth: (profileId, force = false) => API.post(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/oauth/start`,
            { force }
        ),
        getOAuth: profileId => API.get(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/oauth`
        ),
        cancelOAuth: profileId => API.post(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/oauth/cancel`,
            {}
        ),
        finalizeSetup: (profileId, payload) => API.post(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/setup/finalize`,
            payload
        ),
        cancelSetup: profileId => API.post(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/setup/cancel`,
            {}
        ),
        logoutOAuth: profileId => API.post(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/oauth/logout`,
            {}
        ),
        getModels: profileId => API.get(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/models`
        )
    },

    codexSkills: {
        list: profileId => API.get(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/skills`
        ),
        get: (profileId, scope, skillName) => API.get(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/skills/${encodeURIComponent(scope)}/${encodeURIComponent(skillName)}`
        ),
        create: (profileId, payload) => API.post(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/skills`,
            payload
        ),
        installGithub: (profileId, payload) => API.post(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/skills/install/github`,
            payload
        ),
        update: (profileId, skillName, content) => API.put(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/skills/profile/${encodeURIComponent(skillName)}`,
            { content }
        ),
        setEnabled: (profileId, skillName, enabled) => API.put(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/skills/profile/${encodeURIComponent(skillName)}/enabled`,
            { enabled }
        ),
        archive: (profileId, skillName) => API.delete(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/skills/profile/${encodeURIComponent(skillName)}`
        ),
        listTrash: profileId => API.get(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/skills/trash`
        ),
        restore: (profileId, trashId) => API.post(
            `/api/codex/profiles/${encodeURIComponent(profileId)}/skills/trash/${encodeURIComponent(trashId)}/restore`,
            {}
        )
    },


    // WeChat
    wechat: {
        getStatus: () => API.get('/api/wechat/status'),
        getMyInfo: () => API.get('/api/wechat/my-info'),
        getListeners: () => API.get('/api/wechat/listened-chats'),
        addListener: (chatName) => API.post('/api/wechat/add-listen-chat', { chat_name: chatName }),
        removeListener: (chatName) => API.post(`/api/wechat/remove-listen-chat/${chatName}`),
    },

    // Users & Permissions
    users: {
        getAll: () => API.get('/api/permissions/users'),
        delete: (id) => API.request(`/api/permissions/users/${id}`, { method: 'DELETE' }),


        addUser: (chatName, isGroup, senderBlacklist = null) => API.post('/api/permissions/users', {
            chat_name: chatName,
            is_group: isGroup,
            sender_blacklist: senderBlacklist
        }),
        updateUser: (userId, data) => API.put(`/api/permissions/users/${userId}`, data)
    },

    // Roles
    roles: {
        getAll: () => API.get('/api/assistant/roles/'),
        getDetail: (id) => API.get(`/api/assistant/roles/${id}`),
        create: (data) => API.post('/api/assistant/roles/', data),
        update: (id, data) => API.request(`/api/assistant/roles/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        }),
        delete: (id) => API.request(`/api/assistant/roles/${id}`, { method: 'DELETE' })
    },

    // Judges
    judges: {
        getAll: () => API.get('/api/assistant/judges/'),
        getDetail: (id) => API.get(`/api/assistant/judges/${id}`),
        create: (data) => API.post('/api/assistant/judges/', data),
        update: (id, data) => API.request(`/api/assistant/judges/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        }),
        delete: (id) => API.request(`/api/assistant/judges/${id}`, { method: 'DELETE' })
    },

    codexJobs: {
        list: () => API.get('/api/codex/jobs'),
        refreshRuntime: () => API.post('/api/codex/jobs/runtime/refresh', {}),
        selectRuntime: (path) => API.post('/api/codex/jobs/runtime/select', { path }),
        getFileTools: () => API.get('/api/codex/jobs/file-tools'),
        refreshFileTools: () => API.post('/api/codex/jobs/file-tools/refresh', {}),
        cancel: (requestId) => API.post(`/api/codex/jobs/${encodeURIComponent(requestId)}/cancel`, {}),
        events: (requestId) => API.get(`/api/codex/jobs/events/${encodeURIComponent(requestId)}`),
        checkUpdate: () => API.post('/api/codex/jobs/upgrade/check', {}),
        startUpdate: () => API.post('/api/codex/jobs/upgrade/start', {}),
        rollback: () => API.post('/api/codex/jobs/upgrade/rollback', {}),
        resetSession: (chatId) => API.post('/api/codex/jobs/sessions/reset', { chat_id: chatId }),
        deleteSession: (chatId) => API.post('/api/codex/jobs/sessions/delete', { chat_id: chatId }),
        interruptSession: (chatId) => API.post('/api/codex/jobs/sessions/interrupt', { chat_id: chatId })
    },

    // Settings
    settings: {
        getConsole: () => API.get('/api/settings/console'),
        updateConsole: (values) => API.put('/api/settings/console', { values }),
        create: (data) => API.post('/api/settings/', data),
        delete: (key) => API.delete(`/api/settings/${key}`),
        reloadFromEnv: () => API.post('/api/settings/reload-env')
    }
};

// Export for module usage (if we switched to modules) or global
window.API = API;
