/** Lightweight Profile Skill manager. */
const CodexSkills = {
    profileId: '',
    skills: [],
    trash: [],
    selected: null,
    view: 'installed',
    bound: false,

    escape(value) {
        return UI.escapeHtml(value);
    },

    bind() {
        if (this.bound) return;
        const form = document.getElementById('codexSkillEditor');
        if (!form) return;
        this.bound = true;
        form.addEventListener('submit', event => {
            event.preventDefault();
            this.save();
        });
        document.getElementById('codexSkillsSearch')?.addEventListener('input', () => {
            if (this.view === 'trash') this.renderTrash();
            else this.renderList();
        });
        document.getElementById('codexSkillsModal')?.addEventListener('hidden.bs.modal', () => {
            this.profileId = '';
            this.skills = [];
            this.trash = [];
            this.selected = null;
            this.clearAlert();
        });
    },

    async open(profileId) {
        if (!profileId) return;
        this.bind();
        this.profileId = profileId;
        this.skills = [];
        this.trash = [];
        this.selected = null;
        this.view = 'installed';
        const profileName = document.getElementById('codexSkillsProfileName');
        if (profileName) profileName.textContent = profileId;
        const title = document.getElementById('codexSkillsModalTitle');
        if (title) title.textContent = `${profileId} · Skills`;
        const search = document.getElementById('codexSkillsSearch');
        if (search) search.value = '';
        this.setTabs();
        this.showEmpty('正在读取 Skills…', '请稍候。');
        bootstrap.Modal.getOrCreateInstance(document.getElementById('codexSkillsModal')).show();
        await this.load();
    },

    setTabs() {
        document.getElementById('codexSkillsInstalledTab')?.classList.toggle('active', this.view === 'installed');
        document.getElementById('codexSkillsTrashTab')?.classList.toggle('active', this.view === 'trash');
        document.getElementById('codexSkillNewButton')?.classList.toggle('d-none', this.view === 'trash');
        document.getElementById('codexSkillGithubButton')?.classList.toggle('d-none', this.view === 'trash');
    },

    setAlert(message) {
        const alert = document.getElementById('codexSkillsAlert');
        if (!alert) return;
        alert.textContent = message;
        alert.classList.remove('d-none');
    },

    clearAlert() {
        const alert = document.getElementById('codexSkillsAlert');
        if (!alert) return;
        alert.textContent = '';
        alert.classList.add('d-none');
    },

    async load() {
        const list = document.getElementById('codexSkillsList');
        if (list) list.innerHTML = '<div class="loading-wrapper small">正在读取 Skills…</div>';
        this.clearAlert();
        try {
            const result = await API.codexSkills.list(this.profileId);
            this.skills = result.skills || [];
            const counts = result.counts || {};
            const summary = document.getElementById('codexSkillsSummary');
            if (summary) {
                summary.textContent = `Profile ${Number(counts.profile || 0)} · 系统 ${Number(counts.system || 0)} · 已停用 ${Number(counts.disabled || 0)}`;
            }
            const trashCount = document.getElementById('codexSkillsTrashCount');
            if (trashCount) trashCount.textContent = String(Number(counts.trash || 0));
            this.renderList();
        } catch (error) {
            this.setAlert(`加载 Skill 失败：${error.message}`);
            if (list) list.innerHTML = '<div class="codex-skill-list-empty">无法读取 Skill 列表</div>';
        }
    },

    filtered(items) {
        const query = (document.getElementById('codexSkillsSearch')?.value || '').trim().toLowerCase();
        if (!query) return items;
        return items.filter(item => `${item.name} ${item.description || ''}`.toLowerCase().includes(query));
    },

    renderList() {
        if (this.view !== 'installed') return;
        const container = document.getElementById('codexSkillsList');
        if (!container) return;
        const items = this.filtered(this.skills);
        if (!items.length) {
            container.innerHTML = '<div class="codex-skill-list-empty">没有匹配的 Skill</div>';
            return;
        }
        const groups = [
            ['profile', 'Profile Skills'],
            ['system', '系统 Skills']
        ];
        container.innerHTML = groups.map(([scope, label]) => {
            const scoped = items.filter(item => item.scope === scope);
            if (!scoped.length) return '';
            return `<div class="codex-skill-list-group"><div class="codex-skill-list-label">${label}<span>${scoped.length}</span></div>${scoped.map(item => {
                const active = this.selected?.name === item.name && this.selected?.scope === item.scope;
                const state = item.enabled ? '已启用' : '已停用';
                return `<button type="button" class="codex-skill-list-item ${active ? 'active' : ''} ${item.enabled ? '' : 'disabled-skill'}" data-skill-scope="${this.escape(item.scope)}" data-skill-name="${this.escape(item.name)}">
                    <span class="codex-skill-list-main"><strong>${this.escape(item.name)}</strong><small>${this.escape(item.description || '未提供 description')}</small></span>
                    <span class="codex-skill-list-state ${item.enabled ? 'enabled' : 'disabled'}">${state}</span>
                </button>`;
            }).join('')}</div>`;
        }).join('');
        container.querySelectorAll('[data-skill-name]').forEach(button => {
            button.addEventListener('click', () => this.select(
                button.dataset.skillScope || '',
                button.dataset.skillName || ''
            ));
        });
    },

    async select(scope, name) {
        if (!scope || !name) return;
        this.clearAlert();
        try {
            const skill = await API.codexSkills.get(this.profileId, scope, name);
            this.selected = skill;
            this.renderList();
            this.showSkill(skill);
        } catch (error) {
            this.setAlert(`读取 Skill 失败：${error.message}`);
        }
    },

    showEmpty(title = '选择一个 Skill 查看详情', detail = 'Profile Skill 可以编辑、停用和移入回收站。') {
        document.getElementById('codexSkillEditor')?.classList.add('d-none');
        const empty = document.getElementById('codexSkillEmpty');
        if (!empty) return;
        empty.classList.remove('d-none');
        const strong = empty.querySelector('strong');
        const span = empty.querySelector('span');
        if (strong) strong.textContent = title;
        if (span) span.textContent = detail;
    },

    showEditor() {
        document.getElementById('codexSkillEmpty')?.classList.add('d-none');
        document.getElementById('codexSkillEditor')?.classList.remove('d-none');
    },

    showSkill(skill) {
        this.showEditor();
        document.getElementById('codexSkillNameField')?.classList.add('d-none');
        document.getElementById('codexSkillDescriptionField')?.classList.add('d-none');
        document.getElementById('codexSkillGithubFields')?.classList.add('d-none');
        document.getElementById('codexSkillContentField')?.classList.remove('d-none');
        const nameInput = document.getElementById('codexSkillName');
        const descriptionInput = document.getElementById('codexSkillDescription');
        ['codexSkillGithubRepository', 'codexSkillGithubPath', 'codexSkillGithubRef'].forEach(id => {
            const input = document.getElementById(id);
            if (input) input.disabled = true;
        });
        if (nameInput) nameInput.disabled = true;
        if (descriptionInput) descriptionInput.disabled = true;
        const title = document.getElementById('codexSkillEditorTitle');
        const meta = document.getElementById('codexSkillEditorMeta');
        const badge = document.getElementById('codexSkillScopeBadge');
        const content = document.getElementById('codexSkillContent');
        const label = document.getElementById('codexSkillContentLabel');
        const hint = document.getElementById('codexSkillContentHint');
        const save = document.getElementById('codexSkillSaveButton');
        const toggle = document.getElementById('codexSkillToggleButton');
        const archive = document.getElementById('codexSkillArchiveButton');
        if (title) title.textContent = skill.name;
        if (meta) {
            const date = skill.modified_at ? UI.formatDateTime(skill.modified_at) : '';
            const source = skill.origin?.provider === 'github'
                ? ` · GitHub ${skill.origin.repository || ''}@${skill.origin.ref || ''}`
                : '';
            meta.textContent = `${skill.size || 0} bytes${date ? ` · ${date}` : ''}${skill.has_supporting_files ? ' · 含支持文件' : ''}${source}`;
        }
        if (badge) {
            badge.textContent = skill.scope === 'system' ? '系统 · 只读' : 'Profile';
            badge.dataset.scope = skill.scope;
        }
        if (content) {
            content.disabled = false;
            content.value = skill.content || '';
            content.readOnly = !skill.editable;
        }
        if (label) label.textContent = 'SKILL.md';
        if (hint) {
            const dependencies = Array.isArray(skill.origin?.dependency_files)
                ? skill.origin.dependency_files
                : [];
            hint.textContent = skill.validation_error
                ? `校验提示：${skill.validation_error}`
                : dependencies.length
                    ? `GitHub 包含依赖声明：${dependencies.join('、')}。系统没有自动安装这些依赖。`
                    : (skill.editable ? '保存前会校验 frontmatter、名称和文件大小。' : '系统 Skill 由 Codex 管理，此处仅供查看。');
            hint.classList.toggle('text-danger', Boolean(skill.validation_error));
        }
        save?.classList.toggle('d-none', !skill.editable);
        if (save) save.textContent = '保存';
        toggle?.classList.toggle('d-none', !skill.editable);
        archive?.classList.toggle('d-none', !skill.editable);
        if (toggle && skill.editable) {
            toggle.innerHTML = skill.enabled
                ? '<i class="bi bi-pause-circle me-1"></i>停用'
                : '<i class="bi bi-play-circle me-1"></i>启用';
        }
    },

    startCreate() {
        this.view = 'installed';
        this.setTabs();
        this.selected = { mode: 'create' };
        this.renderList();
        this.showEditor();
        document.getElementById('codexSkillNameField')?.classList.remove('d-none');
        document.getElementById('codexSkillDescriptionField')?.classList.remove('d-none');
        document.getElementById('codexSkillGithubFields')?.classList.add('d-none');
        document.getElementById('codexSkillContentField')?.classList.remove('d-none');
        const name = document.getElementById('codexSkillName');
        const description = document.getElementById('codexSkillDescription');
        const content = document.getElementById('codexSkillContent');
        if (name) {
            name.disabled = false;
            name.value = '';
        }
        if (description) {
            description.disabled = false;
            description.value = '';
        }
        if (content) {
            content.disabled = false;
            content.value = '# 工作流程\n\n1. 在这里写清楚执行步骤。';
            content.readOnly = false;
        }
        ['codexSkillGithubRepository', 'codexSkillGithubPath', 'codexSkillGithubRef'].forEach(id => {
            const input = document.getElementById(id);
            if (input) input.disabled = true;
        });
        const title = document.getElementById('codexSkillEditorTitle');
        const meta = document.getElementById('codexSkillEditorMeta');
        const badge = document.getElementById('codexSkillScopeBadge');
        const label = document.getElementById('codexSkillContentLabel');
        const hint = document.getElementById('codexSkillContentHint');
        if (title) title.textContent = '新建 Skill';
        if (meta) meta.textContent = '保存到当前 Profile，不影响其他 Profile。';
        if (badge) {
            badge.textContent = 'Profile';
            badge.dataset.scope = 'profile';
        }
        if (label) label.textContent = '指令正文';
        if (hint) {
            hint.textContent = '创建时会自动生成合法的 SKILL.md frontmatter。';
            hint.classList.remove('text-danger');
        }
        const save = document.getElementById('codexSkillSaveButton');
        save?.classList.remove('d-none');
        if (save) save.textContent = '创建';
        document.getElementById('codexSkillToggleButton')?.classList.add('d-none');
        document.getElementById('codexSkillArchiveButton')?.classList.add('d-none');
        name?.focus();
    },

    startGithubInstall() {
        this.view = 'installed';
        this.setTabs();
        this.selected = { mode: 'github' };
        this.renderList();
        this.showEditor();
        document.getElementById('codexSkillNameField')?.classList.add('d-none');
        document.getElementById('codexSkillDescriptionField')?.classList.add('d-none');
        document.getElementById('codexSkillContentField')?.classList.add('d-none');
        document.getElementById('codexSkillGithubFields')?.classList.remove('d-none');
        const name = document.getElementById('codexSkillName');
        const description = document.getElementById('codexSkillDescription');
        const content = document.getElementById('codexSkillContent');
        if (name) name.disabled = true;
        if (description) description.disabled = true;
        if (content) content.disabled = true;
        const repository = document.getElementById('codexSkillGithubRepository');
        const path = document.getElementById('codexSkillGithubPath');
        const ref = document.getElementById('codexSkillGithubRef');
        if (repository) {
            repository.disabled = false;
            repository.value = '';
        }
        if (path) {
            path.disabled = false;
            path.value = '';
        }
        if (ref) {
            ref.disabled = false;
            ref.value = 'main';
        }
        const title = document.getElementById('codexSkillEditorTitle');
        const meta = document.getElementById('codexSkillEditorMeta');
        const badge = document.getElementById('codexSkillScopeBadge');
        if (title) title.textContent = '从 GitHub 安装';
        if (meta) meta.textContent = '仅支持公开仓库；下载、校验完成后才会启用。';
        if (badge) {
            badge.textContent = 'GitHub → Profile';
            badge.dataset.scope = 'profile';
        }
        const save = document.getElementById('codexSkillSaveButton');
        save?.classList.remove('d-none');
        if (save) save.textContent = '安装';
        document.getElementById('codexSkillToggleButton')?.classList.add('d-none');
        document.getElementById('codexSkillArchiveButton')?.classList.add('d-none');
        repository?.focus();
    },

    async save() {
        if (!this.profileId || !this.selected) return;
        const requestProfileId = this.profileId;
        const button = document.getElementById('codexSkillSaveButton');
        if (button) button.disabled = true;
        this.clearAlert();
        try {
            if (this.selected.mode === 'create') {
                const name = (document.getElementById('codexSkillName')?.value || '').trim();
                const description = (document.getElementById('codexSkillDescription')?.value || '').trim();
                const instructions = document.getElementById('codexSkillContent')?.value || '';
                const created = await API.codexSkills.create(this.profileId, { name, description, instructions });
                await this.load();
                await this.select('profile', created.name || name);
                UI.showSuccess(`Skill “${created.name || name}”已创建`);
            } else if (this.selected.mode === 'github') {
                const repository_url = (document.getElementById('codexSkillGithubRepository')?.value || '').trim();
                const skill_path = (document.getElementById('codexSkillGithubPath')?.value || '').trim();
                const ref = (document.getElementById('codexSkillGithubRef')?.value || 'main').trim();
                if (!repository_url || !skill_path || !ref) {
                    throw new Error('请填写 GitHub 仓库、Skill 路径和分支/提交');
                }
                if (button) button.textContent = '安装中…';
                const installed = await API.codexSkills.installGithub(
                    requestProfileId,
                    { repository_url, skill_path, ref }
                );
                if (this.profileId === requestProfileId) {
                    await this.load();
                    await this.select('profile', installed.name);
                }
                const dependencies = installed.install?.dependency_files || [];
                UI.showSuccess(
                    dependencies.length
                        ? `Skill “${installed.name}”已安装；检测到依赖声明，尚未自动安装依赖`
                        : `Skill “${installed.name}”已从 GitHub 安装`
                );
            } else if (this.selected.editable) {
                const content = document.getElementById('codexSkillContent')?.value || '';
                const name = this.selected.name;
                await API.codexSkills.update(this.profileId, name, content);
                await this.load();
                await this.select('profile', name);
                UI.showSuccess(`Skill “${name}”已保存`);
            }
        } catch (error) {
            const action = this.selected?.mode === 'github' ? '安装' : '保存';
            this.setAlert(`${action} Skill 失败：${error.message}`);
        } finally {
            if (button) {
                button.disabled = false;
                button.textContent = this.selected?.mode === 'create'
                    ? '创建'
                    : this.selected?.mode === 'github' ? '安装' : '保存';
            }
        }
    },

    cancelEdit() {
        this.selected = null;
        this.renderList();
        this.showEmpty();
    },

    async toggleSelected() {
        const skill = this.selected;
        if (!skill?.editable) return;
        const enabled = !skill.enabled;
        try {
            await API.codexSkills.setEnabled(this.profileId, skill.name, enabled);
            await this.load();
            await this.select('profile', skill.name);
            UI.showSuccess(`Skill “${skill.name}”已${enabled ? '启用' : '停用'}`);
        } catch (error) {
            this.setAlert(`更新 Skill 状态失败：${error.message}`);
        }
    },

    async archiveSelected() {
        const skill = this.selected;
        if (!skill?.editable) return;
        const confirmed = await UI.confirm(
            `将 Skill “${skill.name}”移入回收站吗？之后可以恢复。`,
            { title: '移入 Skill 回收站', confirmText: '移入回收站', variant: 'danger' }
        );
        if (!confirmed) return;
        try {
            await API.codexSkills.archive(this.profileId, skill.name);
            this.selected = null;
            this.showEmpty('Skill 已移入回收站', '可切换到回收站恢复。');
            await this.load();
            UI.showSuccess(`Skill “${skill.name}”已移入回收站`);
        } catch (error) {
            this.setAlert(`归档 Skill 失败：${error.message}`);
        }
    },

    async showInstalled() {
        this.view = 'installed';
        this.selected = null;
        this.setTabs();
        this.showEmpty();
        await this.load();
    },

    async showTrash() {
        this.view = 'trash';
        this.selected = null;
        this.setTabs();
        this.showEmpty('Skill 回收站', '选择左侧记录即可恢复到当前 Profile。');
        const list = document.getElementById('codexSkillsList');
        if (list) list.innerHTML = '<div class="loading-wrapper small">正在读取回收站…</div>';
        try {
            const result = await API.codexSkills.listTrash(this.profileId);
            this.trash = result.items || [];
            const count = document.getElementById('codexSkillsTrashCount');
            if (count) count.textContent = String(Number(result.count || 0));
            this.renderTrash();
        } catch (error) {
            this.setAlert(`读取回收站失败：${error.message}`);
        }
    },

    renderTrash() {
        if (this.view !== 'trash') return;
        const container = document.getElementById('codexSkillsList');
        if (!container) return;
        const items = this.filtered(this.trash);
        if (!items.length) {
            container.innerHTML = '<div class="codex-skill-list-empty">回收站为空</div>';
            return;
        }
        container.innerHTML = items.map(item => {
            const date = item.deleted_at ? UI.formatDateTime(item.deleted_at) : '';
            return `<div class="codex-skill-trash-item"><span><strong>${this.escape(item.name)}</strong><small>${this.escape(date || '删除时间未知')}</small></span><button type="button" class="btn btn-outline-primary btn-sm" data-restore-skill="${this.escape(item.trash_id)}"><i class="bi bi-arrow-counterclockwise me-1"></i>恢复</button></div>`;
        }).join('');
        container.querySelectorAll('[data-restore-skill]').forEach(button => {
            button.addEventListener('click', () => this.restore(button.dataset.restoreSkill || '', button));
        });
    },

    async restore(trashId, button = null) {
        if (!trashId) return;
        if (button) button.disabled = true;
        try {
            const restored = await API.codexSkills.restore(this.profileId, trashId);
            this.view = 'installed';
            this.setTabs();
            await this.load();
            await this.select('profile', restored.name);
            UI.showSuccess(`Skill “${restored.name}”已恢复`);
        } catch (error) {
            this.setAlert(`恢复 Skill 失败：${error.message}`);
            if (button) button.disabled = false;
        }
    }
};
