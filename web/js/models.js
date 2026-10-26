// 「模型」分頁：模型與推理強度從本機 CLI 讀取，設定從下一個新啟動的 agent 生效。
const modelsUi = { results: {}, catalogs: {}, loading: {}, saving: {}, html: '' };
const effortLabels = { none: '不推理', minimal: '最低', low: '低', medium: '中', high: '高', xhigh: '特高', max: '最高', ultra: 'Ultra' };

function roleDraftKeys(role) {
  return { cli: `role-cli:${role}`, model: `role-model:${role}`, effort: `role-effort:${role}` };
}

function roleChoice(state, role) {
  const keys = roleDraftKeys(role);
  const saved = state.roles[role];
  return { cli: drafts[keys.cli] ?? saved.cli, model: drafts[keys.model] ?? String(saved.model ?? ''),
    reasoningEffort: drafts[keys.effort] ?? String(saved.reasoningEffort ?? '') };
}

function roleCatalog(role, cli) {
  return modelsUi.catalogs[`${role}:${cli}`] || { models: [], ok: false };
}

function modelOption(value, label, selected, disabled = false) {
  return `<option value="${escapeHtml(value)}"${value === selected ? ' selected' : ''}${disabled ? ' disabled' : ''}>${escapeHtml(label)}</option>`;
}

function modelOptions(catalog, choice) {
  const options = [modelOption('', `使用 ${cliLabel(choice.cli)} CLI 預設`, choice.model)];
  catalog.models.forEach(model => options.push(modelOption(model.id, model.displayName, choice.model)));
  if (choice.model && !catalog.models.some(model => model.id === choice.model)) {
    options.push(modelOption(choice.model, `${choice.model}（目前設定，CLI 清單未列出）`, choice.model, true));
  }
  return options.join('');
}

function effortOptions(model, choice) {
  const options = [modelOption('', '沿用 CLI 設定', choice.reasoningEffort)];
  const efforts = model?.efforts || [];
  efforts.forEach(effort => options.push(modelOption(effort.value,
    `${effortLabels[effort.value] || effort.value}（${effort.value}）`, choice.reasoningEffort)));
  if (choice.reasoningEffort && !efforts.some(effort => effort.value === choice.reasoningEffort)) {
    options.push(modelOption(choice.reasoningEffort, `${choice.reasoningEffort}（目前設定，不受此模型支援）`, choice.reasoningEffort, true));
  }
  return options.join('');
}

function catalogHint(catalog, loading, selected, choice) {
  if (!app.token) return '請用 server.py 印出的網址開啟，即可讀取 CLI 模型清單';
  if (loading) return '正在讀取 CLI 模型清單…';
  if (catalog.message) return catalog.message;
  if (!catalog.ok) return '尚未讀取模型清單';
  if (choice.model && !selected) return '目前設定不在 CLI 清單中，請重新選擇模型後儲存';
  if (!selected) return '模型與推理強度沿用 CLI 設定；選擇模型後可設定它支援的推理強度';
  return [selected.description, `模型 ID：${selected.id}`,
    selected.resolvedModel !== selected.id ? `目前對應：${selected.resolvedModel}` : ''].filter(Boolean).join(' · ');
}

function roleSettingCard(state, role) {
  const info = roleInfo[role], saved = state.roles[role], keys = roleDraftKeys(role);
  const choice = roleChoice(state, role), catalog = roleCatalog(role, choice.cli);
  const selected = catalog.models.find(model => model.id === choice.model);
  const loading = modelsUi.loading[`${role}:${choice.cli}`];
  const busy = !app.token || modelsUi.saving[role];
  const invalidModel = choice.model && !selected;
  const invalidEffort = choice.reasoningEffort && !selected?.efforts.some(effort => effort.value === choice.reasoningEffort);
  const cliOptions = Object.entries(cliNames).map(([cli, label]) => modelOption(cli, label, choice.cli)).join('');
  const result = modelsUi.results[role];
  const effortText = saved.reasoningEffort ? `・推理 ${saved.reasoningEffort}` : '';
  const hint = catalogHint(catalog, loading, selected, choice);
  return `
    <article class="agent agent-mid role-setting" data-accent="${info.accent}" aria-busy="${Boolean(loading || modelsUi.saving[role])}">
      <div class="agent-head">
        <h3 class="agent-name"><span class="agent-icon">${icon(info.icon)}</span>${info.role}<span>（${role}）</span></h3>
        <span class="tag" data-accent="${info.accent}">目前：${escapeHtml(cliLabel(saved.cli))}・${escapeHtml(saved.model || '預設模型')}${escapeHtml(effortText)}</span>
      </div>
      <label class="field"><span>CLI</span>
        <select data-draft="${keys.cli}" data-role-cli="${role}"${busy || saved.customCommand ? ' disabled' : ''}>${cliOptions}</select>
      </label>
      ${saved.customCommand ? '<p class="op-hint">這個角色有自訂 command，要換 CLI 請先改 .board/config.json 的 command</p>' : ''}
      <label class="field"><span>模型</span>
        <select data-draft="${keys.model}" data-role-model="${role}"${busy || loading ? ' disabled' : ''}>${modelOptions(catalog, choice)}</select>
      </label>
      <p class="${catalog.message || (catalog.ok && invalidModel) ? 'op-msg' : 'op-hint'} model-detail" role="status">${escapeHtml(hint)}</p>
      <label class="field"><span>推理強度</span>
        <select data-draft="${keys.effort}" data-role-effort="${role}"${busy || loading || !selected || (!selected.efforts.length && !choice.reasoningEffort) ? ' disabled' : ''}>${effortOptions(selected, choice)}</select>
      </label>
      ${selected && !selected.efforts.length ? '<p class="op-hint">CLI 未提供這個模型的推理強度選項</p>' : ''}
      ${invalidEffort ? '<p class="op-msg">目前的推理強度不適用，請重新選擇模型或推理強度</p>' : ''}
      <div class="role-actions">
        <button class="act act-primary" type="button" data-save-role="${role}"${busy || loading || invalidModel || invalidEffort ? ' disabled' : ''}>${modelsUi.saving[role] ? '儲存中…' : '儲存'}</button>
        <button class="act" type="button" data-refresh-models="${role}"${busy || loading ? ' disabled' : ''}>${loading ? '讀取中…' : '重新整理模型'}</button>
      </div>
      ${result ? `<p class="${result.ok ? 'op-hint' : 'op-msg'}" role="status">${escapeHtml(result.message)}</p>` : ''}
    </article>`;
}

function renderModels(state) {
  const html = Object.keys(roleInfo).map(role => roleSettingCard(state, role)).join('');
  // 避免每兩秒的狀態輪詢關掉正在操作的選單。
  if (html === modelsUi.html) return;
  modelsUi.html = html;
  rerender($('#modelsBody'), html);
}

async function loadRoleModels(role, force = false) {
  if (!app.token || !app.state) return;
  const choice = roleChoice(app.state, role), key = `${role}:${choice.cli}`;
  const cached = modelsUi.catalogs[key];
  if (modelsUi.loading[key] || (!force && cached && Date.now() - cached.loadedAt < 300000)) return;
  modelsUi.loading[key] = true;
  refresh(renderModels);
  try {
    const result = await apiPost(`/api/models/${role}`, { cli: choice.cli, refresh: force });
    // 回應依角色與 CLI 儲存；使用者中途換 CLI 時，舊回應不會蓋掉新選單。
    modelsUi.catalogs[key] = { ...result, models: result.models || [], loadedAt: Date.now() };
  } catch (error) {
    modelsUi.catalogs[key] = { ok: false, models: [], message: `讀取模型清單失敗：${error.message}`, loadedAt: Date.now() };
  } finally {
    modelsUi.loading[key] = false;
    refresh(renderModels);
  }
}

async function saveRole(role) {
  if (!app.token || !app.state || modelsUi.saving[role]) return;
  const keys = roleDraftKeys(role), choice = roleChoice(app.state, role);
  modelsUi.saving[role] = true;
  refresh(renderModels);
  try {
    const result = await apiPost(`/api/roles/${role}`, choice);
    modelsUi.results[role] = result;
    if (result.ok) {
      app.state.roles[role] = { ...app.state.roles[role], ...choice, model: choice.model || null,
        reasoningEffort: choice.reasoningEffort || null };
      Object.values(keys).forEach(key => delete drafts[key]);
      pollState();
    }
  } finally {
    modelsUi.saving[role] = false;
    refresh(renderModels);
  }
}

$('#modelsBody').addEventListener('change', event => {
  const target = event.target;
  const role = target.dataset.roleCli || target.dataset.roleModel || target.dataset.roleEffort;
  if (!role) return;
  const keys = roleDraftKeys(role);
  if (target.dataset.roleCli) {
    drafts[keys.cli] = target.value;
    drafts[keys.model] = '';
    drafts[keys.effort] = '';
    delete modelsUi.results[role];
    loadRoleModels(role);
  } else if (target.dataset.roleModel) {
    drafts[keys.model] = target.value;
    drafts[keys.effort] = '';
  } else {
    drafts[keys.effort] = target.value;
  }
  refresh(renderModels);
});

$('#modelsBody').addEventListener('click', event => {
  const button = event.target.closest('[data-save-role], [data-refresh-models]');
  if (!button || button.disabled) return;
  if (button.dataset.saveRole) saveRole(button.dataset.saveRole);
  else loadRoleModels(button.dataset.refreshModels, true);
});

function loadVisibleModelChoices() {
  if (app.page === 'models' && app.state) Object.keys(roleInfo).forEach(role => loadRoleModels(role));
}

onStateChange(state => { renderModels(state); loadVisibleModelChoices(); });
onPageChange(loadVisibleModelChoices);
