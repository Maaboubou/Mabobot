/* Shared by the local Web console and the desktop launcher. */
window.EmailNotifications = (() => {
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const endpoint = '/api/settings/notifications/email';
    const webTransport = {
        get: () => API.get(endpoint),
        save: values => API.put(endpoint, values),
        test: () => API.post(`${endpoint}/test`, {})
    };
    async function mount(container, transport = webTransport) {
        if (!container || container.dataset.emailMounted) return;
        container.dataset.emailMounted = 'true';
        container.textContent = '正在读取邮箱设置…';
        let saved;
        const field = (name, title, value, type = 'text', extra = '') => `<label class="email-field"><span>${title}</span><input name="${name}" type="${type}" value="${esc(value)}" ${extra}></label>`;
        function render(data) {
            saved = data.config;
            const c = saved;
            container.innerHTML = `<form class="email-preferences">
                <div class="email-heading"><div><h3>邮箱提醒</h3><p>使用你自己的邮箱发送，配置保存在本机。邮件经所选邮箱服务商传递。</p></div><span class="email-status">${esc(data.status)}</span></div>
                <label class="email-check"><input type="checkbox" name="enabled" ${c.enabled ? 'checked' : ''}>开启邮件提醒</label>
                <div class="email-grid">
                    <label class="email-field"><span>邮箱服务</span><select name="provider" aria-label="邮箱服务">${Object.entries(data.presets).map(([key,p]) => `<option value="${esc(key)}" ${key === c.provider ? 'selected' : ''}>${esc(p.label)}</option>`).join('')}</select></label>
                    ${field('address', '发件邮箱', c.address, 'email', 'autocomplete="off" placeholder="你的邮箱地址"')}
                    ${field('password', 'SMTP 授权码 / 应用专用密码', '', 'password', `autocomplete="new-password" placeholder="${c.password_set ? '已设置，留空保留' : '请输入邮箱授权码'}"`)}
                    ${field('recipient', '收件邮箱', c.recipient, 'email', 'placeholder="留空发送给自己"')}
                </div>
                <p class="email-help" data-help></p>
                <p data-google-help hidden><a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener noreferrer">创建 Google 应用专用密码 ↗</a> · <a href="https://support.google.com/accounts/answer/185833?hl=zh-Hans" target="_blank" rel="noopener noreferrer">查看官方说明 ↗</a></p>
                <label class="email-check"><input name="clear_password" type="checkbox">清除已保存的授权码（需先关闭提醒）</label>
                <div data-smtp class="email-grid">
                    ${field('host', 'SMTP 服务器', c.host, 'text', 'placeholder="smtp.example.com"')}
                    ${field('port', '端口', c.port, 'number', 'min="1" max="65535" required')}
                    <label class="email-field"><span>加密方式</span><select name="security" aria-label="加密方式"><option value="ssl" ${c.security === 'ssl' ? 'selected' : ''}>SSL / TLS</option><option value="starttls" ${c.security === 'starttls' ? 'selected' : ''}>STARTTLS</option></select></label>
                    ${field('username', 'SMTP 登录账号', c.username, 'text', 'placeholder="留空使用发件邮箱"')}
                </div>
                <fieldset class="email-events"><legend>提醒内容</legend>${Object.entries(data.events).map(([key,label]) => `<label class="email-check"><input type="checkbox" name="event_${key}" ${c.events[key] !== false ? 'checked' : ''}>${esc(label)}</label>`).join('')}</fieldset>
                <div class="email-actions"><button type="submit">保存设置</button><button type="button" data-test>发送测试邮件</button><button type="button" data-refresh>刷新状态</button></div>
                <p class="email-feedback" role="status" aria-live="polite">测试会向已保存的收件邮箱发送一封不含业务内容的邮件。修改后请先保存。</p>
                <details class="email-history"><summary>最近发送记录（最多 20 条）</summary>${data.history.length ? `<ul>${data.history.map(item => `<li><time>${esc(new Date(item.time).toLocaleString())}</time> · ${esc(item.event === 'test' ? '测试邮件' : data.events[item.event] || '邮件提醒')}<br>${esc(item.message)}</li>`).join('')}</ul>` : '<p>暂无发送记录。记录不包含邮件正文或登录二维码。</p>'}</details>
            </form>`;
            const form = container.querySelector('form');
            const feedback = (text, error = false) => {
                const node = form.querySelector('[role="status"]');
                node.textContent = text;
                node.classList.toggle('email-error', error);
            };
            const value = key => form.elements.namedItem(key).value;
            let dirty = false;
            let busy = false;
            const selectPreset = () => {
                const provider = value('provider');
                form.querySelector('[data-google-help]').hidden = provider !== 'gmail';
                form.querySelector('[data-smtp]').hidden = provider !== 'custom';
                form.querySelector('[data-help]').textContent = provider === 'qq'
                    ? '在 QQ 邮箱网页版「设置 → 账号」开启 SMTP 服务并获取授权码。这里填写授权码，不是邮箱登录密码。'
                    : provider === 'gmail' ? '先在 Google 账号中开启两步验证，再在「应用专用密码」中创建 Mabobot 密码（16 位），粘贴到上方；不要填写 Google 登录密码。部分组织账号或高级保护账号不提供此功能。'
                    : provider === 'custom' ? '向邮箱服务商查询 SMTP 设置；请使用授权码或应用专用密码。'
                    : '在邮箱网页版「设置 → POP3/SMTP/IMAP」开启 SMTP 服务，按提示获取客户端授权码。';
                if (provider !== 'custom') {
                    ['host', 'port', 'security'].forEach(key => { form.elements.namedItem(key).value = data.presets[provider][key]; });
                }
            };
            selectPreset();
            form.elements.namedItem('provider').addEventListener('change', selectPreset);
            form.addEventListener('input', () => { dirty = true; });
            form.addEventListener('change', () => { dirty = true; });
            async function run(action) {
                if (busy) return;
                busy = true;
                // Freeze fields too: changes typed during an in-flight save must not disappear.
                form.querySelectorAll('button,input,select').forEach(button => { button.disabled = true; });
                try { await action(); }
                catch (error) { feedback(error.message || String(error), true); }
                finally {
                    busy = false;
                    form.querySelectorAll('button,input,select').forEach(button => { button.disabled = false; });
                }
            }
            form.addEventListener('submit', event => {
                event.preventDefault();
                const values = {
                    enabled: form.elements.namedItem('enabled').checked,
                    provider: value('provider'), address: value('address'), recipient: value('recipient'),
                    password: value('password') || null, clear_password: form.elements.namedItem('clear_password').checked,
                    host: value('host'), port: Number(value('port')), security: value('security'), username: value('username'),
                    events: Object.fromEntries(Object.keys(data.events).map(key => [key, form.elements.namedItem(`event_${key}`).checked]))
                };
                run(async () => {
                    render(await transport.save(values));
                    container.querySelector('[role="status"]').textContent = '设置已保存，立即生效。可以发送测试邮件检查配置。';
                });
            });
            form.querySelector('[data-test]').addEventListener('click', () => {
                if (dirty) return feedback('配置有未保存的修改，请先保存后再测试。', true);
                feedback('正在发送测试邮件，请稍候…');
                run(async () => {
                    const result = await transport.test();
                    render(result.settings);
                    const node = container.querySelector('[role="status"]');
                    node.textContent = result.message;
                    node.classList.toggle('email-error', !result.ok);
                });
            });
            form.querySelector('[data-refresh]').addEventListener('click', () => {
                if (dirty) return feedback('请先保存修改，再刷新状态。', true);
                run(async () => render(await transport.get()));
            });
        }
        try { render(await transport.get()); }
        catch (error) {
            container.textContent = `读取邮箱配置失败：${error.message || error} `;
            const retry = document.createElement('button');
            retry.textContent = '重试';
            retry.onclick = () => { delete container.dataset.emailMounted; mount(container, transport); };
            container.append(retry);
        }
    }
    return {mount, load: () => mount(document.getElementById('systemEmailConsole'))};
})();
