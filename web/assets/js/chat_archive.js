/* Chat archive and nickname directory: compact, paginated, on-demand views. */
(function () {
    if (!window.App) return;
    const esc = value => App.escapeHtml(String(value ?? ''));
    const $ = id => document.getElementById(id);
    const request = async (url, options) => {
        const response = await fetch(url, options);
        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(typeof error.detail === 'string' ? error.detail : `请求失败 (${response.status})`);
        }
        return response.json();
    };
    const state = {userId: 0, view: 'messages', offset: 0, next: null, generation: 0, members: [], opening: 0};
    const endpoint = () => `/api/history/users/${state.userId}`;
    const time = value => /T.*(?:Z|\+\d\d:\d\d)$/.test(value || '') ? UI.formatDateTime(value, {timeZone: 'Asia/Shanghai'}) : value;
    function ensureModal() {
        if ($('chatArchiveModal')) return;
        document.body.insertAdjacentHTML('beforeend', `
          <div class="modal fade archive-modal" id="chatArchiveModal" tabindex="-1" aria-labelledby="chatArchiveTitle">
            <div class="modal-dialog modal-dialog-scrollable"><div class="modal-content">
              <div class="modal-header"><div class="archive-heading"><h5 class="modal-title" id="chatArchiveTitle">聊天档案</h5><span>记录与成员</span></div>
                <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button></div>
              <div class="modal-body"><div class="archive-console">
                <div class="archive-toolbar"><select id="archiveChat" class="form-select" aria-label="选择聊天"></select>
                  <nav class="archive-tabs" aria-label="档案视图"><button type="button" id="archiveMessages" class="active" aria-pressed="true">聊天记录</button><button type="button" id="archiveMembers" aria-pressed="false">成员别名</button></nav>
                  <div class="archive-toolbar-actions"><a id="archiveExport" class="btn btn-sm btn-outline-secondary">导出记录</a><details id="archiveExtras"><summary class="btn btn-sm btn-outline-secondary">更多</summary><div><button id="archiveLookups" type="button">查阅轨迹</button><button id="archiveCoverage" type="button">日期覆盖</button></div></details></div>
                </div>
                <form id="archiveFilters" class="archive-filters"><input id="archiveQuery" class="form-control" maxlength="160" aria-label="关键词" placeholder="搜索聊天内容">
                  <div id="archiveMessageFilters"><input id="archiveSender" class="form-control" maxlength="160" placeholder="昵称或别名" aria-label="发送者"><input id="archiveStart" type="date" class="form-control" aria-label="开始日期"><span>—</span><input id="archiveEnd" type="date" class="form-control" aria-label="结束日期（含）"></div>
                  <button class="btn btn-primary" type="submit">查找</button><button id="archiveAddMember" class="btn btn-outline-secondary" type="button" hidden>添加成员</button>
                </form>
                <div class="archive-status"><span id="archiveSummary" aria-live="polite"></span><span id="archiveViewLabel"></span></div>
                <div id="archiveResults" aria-live="polite"></div>
                <footer class="archive-pagination"><span id="archivePageLabel"></span><div><button id="archivePrevious" class="btn btn-sm btn-outline-secondary" type="button" hidden>上一页</button><button id="archiveMore" class="btn btn-sm btn-outline-secondary" type="button" hidden>下一页</button></div></footer>
              </div></div></div></div></div>`);
        $('archiveFilters').addEventListener('submit', event => {event.preventDefault(); load(0);});
        $('archiveChat').addEventListener('change', event => {state.userId = Number(event.target.value); load(0);});
        $('archiveMessages').addEventListener('click', () => switchView('messages'));
        $('archiveMembers').addEventListener('click', () => switchView('members'));
        $('archiveLookups').addEventListener('click', () => switchView('lookups'));
        $('archiveCoverage').addEventListener('click', () => switchView('coverage'));
        $('archiveMore').addEventListener('click', () => load(state.next));
        $('archivePrevious').addEventListener('click', () => load(Math.max(0, state.offset - (state.view === 'coverage' ? 100 : 50))));
        $('archiveAddMember').addEventListener('click', () => editMember(null));
        $('archiveResults').addEventListener('click', event => {
            const button = event.target.closest('button');
            if (!button) return;
            if (button.dataset.message) detail(button.dataset.message, Number(button.dataset.offset || 0));
            if (button.dataset.member) editMember(state.members.find(row => row.id === button.dataset.member));
            if (button.dataset.deleteMember) deleteMember(state.members.find(row => row.id === button.dataset.deleteMember), button);
            if (button.hasAttribute('data-cancel-member')) load(state.offset);
        });
        $('archiveResults').addEventListener('submit', saveMember);
        $('chatArchiveModal').addEventListener('hidden.bs.modal', () => {++state.generation;});
    }
    function switchView(view) {
        state.view = view;
        $('archiveQuery').value = '';
        $('archiveExtras').open = false;
        load(0);
    }
    function controls() {
        const records = state.view === 'messages', members = state.view === 'members';
        $('archiveMessages').classList.toggle('active', records);
        $('archiveMembers').classList.toggle('active', members);
        $('archiveMessages').setAttribute('aria-pressed', String(records));
        $('archiveMembers').setAttribute('aria-pressed', String(members));
        $('archiveFilters').hidden = !(records || members);
        $('archiveMessageFilters').hidden = !records;
        $('archiveAddMember').hidden = !members;
        $('archiveQuery').placeholder = members ? '搜索昵称或别名' : '搜索聊天内容';
        $('archiveExport').href = endpoint() + '/export';
        $('archiveViewLabel').textContent = {messages: '每页 50 条', members: '昵称 A–Z · 中文按拼音', lookups: '最近 50 次', coverage: '北京时间'}[state.view];
    }
    const table = (headers, rows, cls = '') => `<div class="table-responsive"><table class="codex-compact-table ${cls}"><thead><tr>${headers.map(text => `<th>${text}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;
    async function load(offset = 0) {
        controls();
        const generation = ++state.generation;
        state.offset = offset || 0; state.next = null;
        $('archiveMore').hidden = true; $('archivePrevious').hidden = true;
        $('archivePageLabel').textContent = ''; $('archiveSummary').textContent = '';
        $('archiveResults').innerHTML = '<div class="codex-empty">正在读取…</div>';
        if (!state.userId) { $('archiveResults').innerHTML = '<div class="codex-empty">暂无可用聊天</div>'; return; }
        const parameters = new URLSearchParams({offset: state.offset});
        let path = endpoint();
        if (state.view === 'messages' || state.view === 'members') parameters.set('query', $('archiveQuery').value);
        if (state.view === 'messages') {
            for (const [name,id] of [['sender','archiveSender'],['start','archiveStart']]) if ($(id).value) parameters.set(name,$(id).value);
            if ($('archiveEnd').value) {
                const day = new Date($('archiveEnd').value + 'T00:00:00Z'); day.setUTCDate(day.getUTCDate()+1);
                parameters.set('end',day.toISOString().slice(0,10));
            }
        } else path += '/' + state.view;
        try {
            const data = await request(`${path}?${parameters}`);
            if (generation !== state.generation) return;
            let count = 0, html = '';
            if (state.view === 'messages') {
                count = data.messages.length;
                const summary = data.summary?.[0];
                $('archiveSummary').textContent = summary ? `已保存 ${UI.formatNumber(summary.messages)} 条` : '暂无记录';
                $('archiveSummary').title = data.coverage_notice || '';
                html = table(['时间','发言人','内容',''], data.messages.map(row => `<tr class="archive-message-row"><td class="archive-time">${esc(time(row.time))}</td><td class="archive-sender"><strong>${esc(row.sender)}</strong>${row.is_bot ? '<small>机器人</small>' : ''}</td><td><div class="archive-excerpt">${esc(row.content)}${row.truncated ? '…' : ''}</div>${row.corrected ? '<span class="archive-note">含人工更正</span>' : ''}</td><td><button class="btn archive-text-button" data-message="${esc(row.id)}" aria-label="查看原文与上下文">展开</button></td></tr><tr class="archive-detail-row" id="archive-detail-${esc(row.id)}" hidden><td colspan="4"></td></tr>`).join(''), 'archive-message-table');
                if (data.status === 'ambiguous_sender') html = '<div class="codex-empty">此别名对应多个成员，请在成员别名页查看后使用具体昵称。</div>';
            } else if (state.view === 'members') {
                state.members = data.items; count = data.items.length;
                $('archiveSummary').textContent = `${UI.formatNumber(data.total)} 位成员`;
                html = table(['当前昵称','别名',''], data.items.map(row => `<tr><td class="archive-member-name">${esc(row.current_name)}</td><td class="archive-aliases">${row.aliases.map(name => `<span>${esc(name)}</span>`).join('') || '<span class="archive-muted">—</span>'}</td><td><div class="archive-member-actions"><button class="btn archive-text-button" data-member="${esc(row.id)}">编辑</button><button class="btn archive-text-button text-danger" data-delete-member="${esc(row.id)}" aria-label="删除成员 ${esc(row.current_name)}">删除</button></div></td></tr>`).join(''), 'archive-members-table');
            } else if (state.view === 'lookups') {
                count = data.items.length; $('archiveSummary').textContent = '按需查阅记录';
                html = table(['时间','操作','查询','返回量'], data.items.map(row => `<tr><td class="archive-time">${esc(time(row.created_at))}</td><td>${esc(row.tool)}</td><td class="archive-query-cell">${esc(Object.entries(row.details.filters || {}).map(([key,value]) => `${key}: ${value}`).join(' · '))}<small>${row.details.cache_hit ? '复用查询定位' : ''}</small></td><td>${UI.formatNumber(row.details.returned_bytes || 0)} B</td></tr>`).join(''), 'archive-lookups-table');
            } else {
                count = data.days.length; $('archiveSummary').textContent = '已保存记录的分布；空白日期不代表没有聊天';
                html = table(['日期','类型','记录数'],data.days.map(row => `<tr><td>${esc(row.day)}</td><td>${esc(row.kind)}</td><td>${UI.formatNumber(row.messages)}</td></tr>`).join(''));
            }
            $('archiveResults').innerHTML = count ? html : '<div class="codex-empty">没有匹配记录</div>';
            if (data.status === 'ambiguous_sender') $('archiveResults').innerHTML = html;
            state.next = data.next_offset ?? null;
            $('archiveMore').hidden = state.next === null;
            $('archivePrevious').hidden = state.offset === 0;
            $('archivePageLabel').textContent = count ? `${state.offset+1}–${state.offset+count}` : '';
        } catch (error) { if (generation === state.generation) $('archiveResults').innerHTML = `<div class="codex-empty">${esc(error.message)}</div>`; }
    }
    async function detail(identity, offset = 0) {
        const target = $('archive-detail-' + identity);
        if (!target) return;
        if (!target.hidden && !offset) { target.hidden = true; return; }
        const generation = state.generation, path = endpoint();
        target.hidden = false; target.firstElementChild.innerHTML = '<div class="codex-empty">正在读取原文…</div>';
        try {
            const data = await request(`${path}/messages/${encodeURIComponent(identity)}?offset=${offset}`);
            if (generation !== state.generation || !target.isConnected) return;
            target.firstElementChild.innerHTML = `<div class="archive-detail">${data.messages.map(row => `<article class="${row.id === identity ? 'selected' : ''}"><header><strong>${esc(row.sender)}</strong><time>${esc(time(row.time || row.occurred_at))}</time></header><div>${esc(row.content)}</div>${row.evidence_only ? '<small>补充日志记录</small>' : ''}${row.correction ? `<small>人工更正：${esc(typeof row.correction === 'string' ? row.correction : row.correction.reason || '')}</small>` : ''}${row.id === identity && row.next_offset ? `<button class="btn archive-text-button" data-message="${esc(identity)}" data-offset="${Number(row.next_offset)}">继续读取</button>` : ''}</article>`).join('')}</div>`;
        } catch (error) { if (generation === state.generation) target.firstElementChild.textContent = error.message; }
    }
    function editMember(row) {
        ++state.generation;
        $('archiveMore').hidden = true; $('archivePrevious').hidden = true;
        $('archiveResults').innerHTML = `<form id="archiveMemberEditor" class="archive-member-editor" data-identity="${esc(row?.id || '')}" data-version="${Number(row?.version || 0)}"><label>当前昵称<input class="form-control" name="current_name" required maxlength="160" value="${esc(row?.current_name || '')}"></label><label>别名<textarea class="form-control" name="aliases" rows="5" placeholder="每行一个别名">${esc((row?.aliases || []).join('\n'))}</textarea></label><div class="archive-editor-actions"><span id="archiveEditorError" role="alert"></span><button type="button" class="btn btn-outline-secondary" data-cancel-member>取消</button><button type="submit" class="btn btn-primary">保存</button></div></form>`;
        $('archiveMemberEditor').elements.current_name.focus();
    }
    async function deleteMember(row, button) {
        if (!row || button.disabled) return;
        if (button.dataset.ready !== 'yes') {
            button.dataset.ready = 'yes'; button.textContent = '确认删除';
            setTimeout(() => { if (button.isConnected) { delete button.dataset.ready; button.textContent = '删除'; } }, 4000);
            return;
        }
        button.disabled = true;
        const generation = state.generation;
        try {
            await request(`${endpoint()}/members/${encodeURIComponent(row.id)}?version=${row.version}`, {method: 'DELETE'});
            if (generation === state.generation) await load(state.members.length === 1 ? Math.max(0, state.offset - 50) : state.offset);
        } catch (error) { if (generation === state.generation) { button.disabled = false; UI.showError(error.message); } }
    }
    async function saveMember(event) {
        if (event.target.id !== 'archiveMemberEditor') return;
        event.preventDefault();
        const form = event.target, button = form.querySelector('[type=submit]'), generation = state.generation;
        if (button.disabled) return;
        button.disabled = true; $('archiveEditorError').textContent = '';
        const payload = {current_name: form.elements.current_name.value.trim(), aliases: [...new Set(form.elements.aliases.value.split('\n').map(value => value.trim()).filter(Boolean))], version: Number(form.dataset.version)};
        const path = endpoint() + '/members' + (form.dataset.identity ? '/' + encodeURIComponent(form.dataset.identity) : '');
        try {
            await request(path, {method: form.dataset.identity ? 'PUT' : 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload)});
            if (generation === state.generation) await load(state.offset);
        } catch (error) { if (generation === state.generation) { $('archiveEditorError').textContent = error.message; button.disabled = false; } }
    }
    Object.assign(App, {
        async openChatArchive(userId) {
            ensureModal(); const opening = ++state.opening;
            try {
                const users = await API.users.getAll();
                if (opening !== state.opening) return;
                $('archiveChat').innerHTML = users.map(user => `<option value="${Number(user.id)}">${esc(user.chat_name)}</option>`).join('');
                state.userId = Number(userId || users[0]?.id || 0);
                $('archiveChat').value = String(state.userId);
                state.view = 'messages'; $('archiveQuery').value = ''; $('archiveSender').value = ''; $('archiveStart').value = ''; $('archiveEnd').value = '';
                bootstrap.Modal.getOrCreateInstance($('chatArchiveModal')).show();
                await load(0);
            } catch (error) { UI.showError(error.message); }
        },
        async openChatArchiveHub() { await this.openChatArchive(0); }
    });
})();
