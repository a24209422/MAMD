// 「模型」分頁：寫程式、審查各自選 CLI 和模型，存進 .board/config.json；conductor 從下一個新啟動的 agent 開始用
// Codex 的模型清單會變，不確定有哪些，只列 Claude 的別名；兩邊都可以自己輸入
const modelSuggestions = { claude: ['opus', 'sonnet', 'haiku'], codex: [] };
const modelsUi = { results: {}, html: '' };

function roleDraftKeys(role) {
  return { cli: `role-cli:${role}`, model: `role-model:${role}` };
}

// 還沒存的選擇放在 drafts，每 2 秒重畫時才不會被設定檔的值蓋掉
function roleChoice(state, role) {
  const keys = roleDraftKeys(role);
  const saved = state.roles[role];
  // 設定檔手寫成數字之類時也當文字處理，按儲存才不會在 trim 時出錯
  return { cli: drafts[keys.cli] ?? saved.cli, model: drafts[keys.model] ?? String(saved.model ?? '') };
}

function roleSettingCard(state, role) {
  const info = roleInfo[role];
  const saved = state.roles[role];
  const keys = roleDraftKeys(role);
  const choice = roleChoice(state, role);
  const result = modelsUi.results[role];
  const cliDisabled = !app.token || saved.customCommand ? ' disabled' : '';
  const cliOptions = Object.entries(cliNames).map(([cli, label]) =>
    `<option value="${cli}"${cli === choice.cli ? ' selected' : ''}>${label}</option>`).join('');
  const suggestions = (modelSuggestions[choice.cli] || []).map(name => `<option value="${name}"></option>`).join('');
  return `
    <article class="agent agent-mid role-setting" data-accent="${info.accent}">
      <div class="agent-head">
        <h3 class="agent-name"><span class="agent-icon">${icon(info.icon)}</span>${info.role}<span>（${role}）</span></h3>
        <span class="tag" data-accent="${info.accent}">目前：${escapeHtml(cliLabel(saved.cli))}・${escapeHtml(saved.model || '預設模型')}</span>
      </div>
      <label class="field"><span>CLI</span>
        <select data-draft="${keys.cli}" data-role-cli="${role}"${cliDisabled}>${cliOptions}</select>
      </label>
      ${saved.customCommand ? '<p class="op-hint">這個角色有自訂 command，要換 CLI 請先改 .board/config.json 的 command</p>' : ''}
      <label class="field"><span>模型</span>
        <input type="text" list="models-${role}" data-draft="${keys.model}" value="${escapeHtml(choice.model)}" placeholder="空白＝${escapeHtml(cliLabel(choice.cli))} 的預設模型" autocomplete="off" spellcheck="false"${writeDisabled()}>
      </label>
      <datalist id="models-${role}">${suggestions}</datalist>
      <div class="role-actions">
        <button class="act act-primary" type="button" data-save-role="${role}"${writeDisabled()}>儲存</button>
      </div>
      ${result ? `<p class="${result.ok ? 'op-hint' : 'op-msg'}">${escapeHtml(result.message)}</p>` : ''}
    </article>`;
}

function renderModels(state) {
  const html = Object.keys(roleInfo).map(role => roleSettingCard(state, role)).join('');
  // 只有內容變了才重畫：agent 跑的時候狀態每 2 秒都在變，一直重畫會把打開的下拉選單關掉
  if (html === modelsUi.html) return;
  modelsUi.html = html;
  rerender($('#modelsBody'), html);
}

async function saveRole(role) {
  if (!app.token || !app.state) return;
  const keys = roleDraftKeys(role);
  const choice = roleChoice(app.state, role);
  const model = choice.model.trim();
  const result = await apiPost(`/api/roles/${role}`, { cli: choice.cli, model });
  modelsUi.results[role] = result;
  if (result.ok) {
    // 先改畫面上的設定再清掉暫存，下一次輪詢之前才不會閃回舊值
    app.state.roles[role] = { ...app.state.roles[role], cli: choice.cli, model: model || null };
    delete drafts[keys.cli];
    delete drafts[keys.model];
    pollState();
  }
  // 儲存按鈕是直接停用的，內容沒變也要重畫一次才會恢復
  modelsUi.html = '';
  refresh(renderModels);
}

$('#modelsBody').addEventListener('change', event => {
  if (!event.target.matches('[data-role-cli]')) return;
  // 模型名稱跟著 CLI：Claude 的 sonnet 交給 Codex 要到執行時才失敗，所以換 CLI 就清空、回到新 CLI 的預設模型
  drafts[roleDraftKeys(event.target.dataset.roleCli).model] = '';
  // 建議的模型清單和提示字也要跟著換
  refresh(renderModels);
});

$('#modelsBody').addEventListener('click', event => {
  const button = event.target.closest('[data-save-role]');
  if (!button || button.disabled) return;
  // 等 server 回應前先停用按鈕，避免連按送出兩次
  button.disabled = true;
  saveRole(button.dataset.saveRole);
});

onStateChange(renderModels);
