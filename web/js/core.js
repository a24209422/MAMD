// 儀表板共用：API、輪詢、token、換頁和小工具。各面板在自己的檔案用 onStateChange、onPoll、onPageChange 註冊
const pollMs = 2000;
const busyStatuses = ['寫程式中', '解衝突中', '審查中'];
const pages = [
  { key: 'overview', label: '總覽', icon: 'grid' },
  { key: 'tasks', label: '任務', icon: 'clipboard' },
  { key: 'models', label: '模型', icon: 'cube' },
  { key: 'console', label: '控制台', icon: 'monitor' },
  { key: 'system-monitor', label: '系統監控', icon: 'pulse' },
  { key: 'settings', label: '設定', icon: 'gear' },
];
const builtPages = ['overview', 'tasks', 'models', 'console', 'system-monitor'];
// 改過名的頁面：舊網址換成新頁面，例如原本空白的 #topology 換成 #system-monitor
const pageAliases = { topology: 'system-monitor' };
const iconPaths = {
  expand: '<path d="M5 6l6 6-6 6M12 6l6 6-6 6"/>',
  grid: '<rect x="4" y="4" width="7" height="7"/><rect x="13" y="4" width="7" height="7"/><rect x="4" y="13" width="7" height="7"/><rect x="13" y="13" width="7" height="7"/>',
  clipboard: '<rect x="5" y="5" width="14" height="16"/><path d="M9 3h6v4H9zM8.5 11.5h7M8.5 15.5h5"/>',
  cube: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5M12 12v9"/>',
  monitor: '<rect x="3" y="4" width="18" height="12"/><path d="M8 20h8M12 16v4M7 8h6M7 11.5h9"/>',
  pulse: '<rect x="3" y="4" width="18" height="16"/><path d="M6 13h3l2-4 3 7 2-3h2"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/>',
  crown: '<path d="M3 8l4.5 4L12 5l4.5 7L21 8l-2 11H5z"/>',
  code: '<path d="M8 7l-5 5 5 5M16 7l5 5-5 5M14 4l-4 16"/>',
  shield: '<path d="M12 3l8 3v6c0 4.5-3.4 8.2-8 9-4.6-.8-8-4.5-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-4.5"/>',
  robot: '<rect x="5" y="8" width="14" height="11"/><path d="M12 4.5V8M9 12.5v1.5M15 12.5v1.5M9.5 16.5h5M3 12v4M21 12v4"/><circle cx="12" cy="3.5" r="1"/>',
  send: '<path d="M4 12L20 4l-6 16-3-7z"/><path d="M11 13l9-9"/>',
  chat: '<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9.5h8M8 12.5h5"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
};
// 這一頁看哪個專案在載入時就由網址固定，之後不再改；換專案一律整頁重新載入，還在跑的請求不會改送到別的專案
const app = {
  state: null, stateText: '', token: '', online: true, page: 'overview', selectedAgent: null, selectedPlan: null,
  project: new URLSearchParams(location.search).get('project') || '', projectMissing: false,
};
const recentProjectsKey = 'boardRecentProjects';
const recentProjectsLimit = 10;
const stateRenderers = [];
const pollRenderers = [];
const pageRenderers = [];
// 輸入框還沒送出的字，依 data-draft 記住；每 2 秒重畫時放回去
const drafts = {};
let pollTimer = null;
let polling = false;
let pollAgain = false;

const $ = selector => document.querySelector(selector);

function onStateChange(render) {
  stateRenderers.push(render);
}

function onPoll(render) {
  pollRenderers.push(render);
}

function onPageChange(render) {
  pageRenderers.push(render);
}

// 一個面板出錯不能讓其他面板跟著停
function safeRun(render, arg) {
  try {
    const result = render(arg);
    if (result && typeof result.catch === 'function') result.catch(error => console.error(error));
  } catch (error) {
    console.error(error);
  }
}

function refresh(render) {
  if (app.state) safeRun(render, app.state);
}

function icon(name, extraClass = '') {
  return `<svg class="ico ${extraClass}" viewBox="0 0 24 24" aria-hidden="true">${iconPaths[name]}</svg>`;
}

function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

// 事件時間像 2026-10-26T13:56:20+08:00，畫面只顯示時：分
function clock(isoText) {
  return isoText ? isoText.slice(11, 16) : '';
}

function percent(part, total) {
  return total ? Math.round(part / total * 100) : 0;
}

// 門檻對齊 Context 分析的刻度：40% 以下綠、70% 以上紅
function contextLevel(pct) {
  if (pct >= 70) return 'high';
  if (pct >= 40) return 'mid';
  return 'ok';
}

function contextPct(context) {
  if (!context || context.tokens == null || !context.window) return null;
  return Math.round(context.tokens / context.window * 100);
}

function tokensK(tokens) {
  return `${Math.round(tokens / 1000)}K`;
}

function agentCardState(agent) {
  if (agent.status === '已暫停') return 'paused';
  if (!busyStatuses.includes(agent.status)) return 'idle';
  const pct = contextPct(agent.context);
  return pct != null && pct >= 70 ? 'warn' : 'run';
}

// 小卡整張是 button，裡面只能放 span
function meter(label, pct, level, valueText) {
  const width = pct == null ? 0 : Math.min(100, pct);
  const value = valueText ?? (pct == null ? '—' : `${pct}%`);
  return `<span class="meter" data-level="${level}">
    <span class="meter-label">${label}</span>
    <span class="meter-track"><i style="width:${width}%"></i></span>
    <b class="meter-pct">${escapeHtml(value)}</b>
  </span>`;
}

function writeDisabled() {
  return app.token ? '' : ' disabled';
}

function readToken() {
  const match = location.hash.match(/^#token=([^&]+)/);
  if (match) {
    try {
      sessionStorage.setItem('boardToken', match[1]);
    } catch (error) {
      // 存不了就只在這次載入有效，重新整理要再用原網址開
    }
    // token 留在網址列會跟 #console 這類換頁衝突，也容易被截圖帶出去
    history.replaceState(null, '', location.pathname + location.search);
    return match[1];
  }
  try {
    return sessionStorage.getItem('boardToken') || '';
  } catch (error) {
    return '';
  }
}

// 各面板照舊寫 /api/...，在這裡統一加上本頁的專案；開啟、列出專案本來就不屬於任何專案
function apiUrl(path) {
  if (!app.project || !path.startsWith('/api/') || path.startsWith('/api/projects/')) return path;
  return `/api/projects/${encodeURIComponent(app.project)}${path.slice(4)}`;
}

async function apiGetText(path) {
  const response = await fetch(apiUrl(path), { cache: 'no-store' });
  const text = await response.text();
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      message = JSON.parse(text).message || message;
    } catch (error) {
      // 不是 JSON 就用狀態碼當訊息
    }
    const failure = new Error(message);
    // 輪詢要分得出是 server 連不到，還是 server 重新啟動後還沒登錄這個專案（404）
    failure.status = response.status;
    throw failure;
  }
  return text;
}

async function apiGet(path) {
  return JSON.parse(await apiGetText(path));
}

async function apiPost(path, body = {}) {
  try {
    const response = await fetch(apiUrl(path), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Board-Token': app.token },
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    return { ...payload, ok: Boolean(payload.ok), message: payload.message || '' };
  } catch (error) {
    return { ok: false, message: `連不到 server：${error.message}` };
  }
}

function renderChrome() {
  const [pillState, pillText] = !app.online ? ['offline', '離線']
    : app.state && app.state.paused ? ['paused', '暫停'] : ['live', 'LIVE'];
  $('#pill').dataset.state = pillState;
  $('#pill').textContent = pillText;
  const notes = [];
  if (!app.token) notes.push('唯讀：請用 server.py 印出的網址重新開啟');
  if (!app.online) notes.push('連不到 server，畫面停在最後一次讀到的資料，每 2 秒重試');
  if (app.state) app.state.warnings.forEach(warning => notes.push(`警告｜${warning}`));
  const missing = app.projectMissing ? `<p>server 還沒登錄這個專案（可能重新啟動過）。
    <a class="mini-btn" href="/#overview">返回預設專案</a>
    <button class="mini-btn" type="button" data-project-reopen${writeDisabled()}>重新開啟</button></p>` : '';
  $('#notice').innerHTML = missing + notes.map(note => `<p>${escapeHtml(note)}</p>`).join('');
  $('#notice').hidden = !missing && !notes.length;
  renderProjectBar();
}

function projectName(path) {
  return path.split(/[\\/]/).filter(Boolean).pop() || path;
}

function renderProjectBar() {
  const path = app.state ? app.state.project : '';
  const name = path ? projectName(path) : app.projectMissing ? '找不到這個專案' : '讀取中…';
  // 資料夾名稱和路徑來自使用者的磁碟，一律當純文字放
  $('#projectName').textContent = name;
  $('#projectPath').textContent = path;
  $('#projectPath').title = path;
  // 終端機面板也標出所屬專案，新 shell 從這個資料夾啟動
  $('#terminalProject').textContent = path ? name : '';
  $('#terminalProject').title = path;
  $('#projectOpen').hidden = !app.token;
}

// 讀不到（瀏覽器不給用或內容壞掉）回傳 null，和「還沒有紀錄」分開提示；兩種情況都還能直接輸入路徑
function readRecentProjects() {
  try {
    const records = JSON.parse(localStorage.getItem(recentProjectsKey) || '[]');
    if (!Array.isArray(records)) return null;
    return records.filter(record => record && typeof record.id === 'string' && typeof record.path === 'string')
      .slice(0, recentProjectsLimit);
  } catch (error) {
    return null;
  }
}

function rememberProject(project) {
  const records = (readRecentProjects() || []).filter(record => record.id !== project.id);
  records.unshift({ id: project.id, name: project.name, path: project.path });
  try {
    localStorage.setItem(recentProjectsKey, JSON.stringify(records.slice(0, recentProjectsLimit)));
  } catch (error) {
    // 存不了只是少一筆最近紀錄，不影響這次開啟
  }
}

function renderRecentProjects() {
  const records = readRecentProjects();
  const list = $('#projectRecent');
  list.replaceChildren();
  if (!records || !records.length) {
    const empty = document.createElement('li');
    empty.className = 'op-hint';
    empty.textContent = records ? '還沒有最近紀錄' : '讀不到最近紀錄，請直接輸入路徑';
    list.append(empty);
    return;
  }
  for (const record of records) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'project-recent-btn';
    const name = document.createElement('strong');
    name.textContent = record.name || projectName(record.path);
    const path = document.createElement('span');
    path.textContent = record.path;
    button.append(name, path);
    // 紀錄可能已經過時（資料夾搬走、server 重新啟動），每次都交給 server 重新驗證
    button.addEventListener('click', () => openProject(record.path));
    const item = document.createElement('li');
    item.append(button);
    list.append(item);
  }
}

function openProjectDialog(path = '') {
  if (!app.token) return;
  $('#projectPathInput').value = path;
  $('#projectOpenMessage').textContent = '';
  renderRecentProjects();
  if (!$('#projectDialog').open) $('#projectDialog').showModal();
  $('#projectPathInput').focus();
}

// server 重新啟動後舊 id 還沒登錄；id 由路徑算出，從最近紀錄找回路徑重新開啟就會拿回同一個 id
function missingProjectPath() {
  const record = (readRecentProjects() || []).find(item => item.id === app.project);
  return record ? record.path : '';
}

async function openProject(path) {
  const target = path.trim();
  if (!target) {
    $('#projectOpenMessage').textContent = '請輸入專案資料夾的完整路徑';
    return;
  }
  $('#projectOpenSubmit').disabled = true;
  const result = await apiPost('/api/projects/open', { path: target });
  $('#projectOpenSubmit').disabled = false;
  if (!result.ok) {
    $('#projectOpenMessage').textContent = result.message || '開啟失敗';
    return;
  }
  rememberProject(result.project);
  goToProject(result.project.id);
}

// 整頁重新載入，舊頁的 plan、agent、草稿、模型清單、log 和錯誤提示都不會帶到新專案
function goToProject(id) {
  const search = `?project=${encodeURIComponent(id)}`;
  const target = `/${search}#overview`;
  // 網址只差在 # 後面時瀏覽器不會重新載入，重新開啟同一個專案要自己重新整理
  if (location.search === search) {
    history.replaceState(null, '', target);
    location.reload();
  } else {
    location.assign(target);
  }
}

function renderAll() {
  renderChrome();
  stateRenderers.forEach(render => safeRun(render, app.state));
}

async function pollState() {
  // 操作成功後會立刻要求重讀；正在讀的時候就等它讀完再讀一次，不要同時跑兩輪
  if (polling) {
    pollAgain = true;
    return;
  }
  polling = true;
  clearTimeout(pollTimer);
  try {
    const text = await apiGetText('/api/state');
    app.online = true;
    app.projectMissing = false;
    if (text !== app.stateText) {
      app.stateText = text;
      app.state = JSON.parse(text);
      renderAll();
    } else {
      renderChrome();
    }
    pollRenderers.forEach(render => safeRun(render, app.state));
  } catch (error) {
    app.projectMissing = Boolean(app.project) && error.status === 404;
    // 回 404 代表 server 還在，只是不認得這個專案，不算離線
    app.online = app.projectMissing;
    renderChrome();
  } finally {
    polling = false;
    if (pollAgain) {
      pollAgain = false;
      pollState();
    } else {
      pollTimer = setTimeout(pollState, pollMs);
    }
  }
}

// 每 2 秒重畫一次；輸入框的字、焦點和捲動位置要留著，不然打字打到一半會被清掉
function rerender(container, html) {
  const active = document.activeElement;
  const focusKey = active && container.contains(active) && active.dataset ? active.dataset.draft : null;
  const selection = focusKey && typeof active.selectionStart === 'number' ? [active.selectionStart, active.selectionEnd] : null;
  const scrolls = [...container.querySelectorAll('[data-keep-scroll]')].map(element => [element.dataset.keepScroll, element.scrollTop]);
  const ownScroll = container.scrollTop;
  container.innerHTML = html;
  container.querySelectorAll('[data-draft]').forEach(input => {
    if (drafts[input.dataset.draft] !== undefined) input.value = drafts[input.dataset.draft];
  });
  scrolls.forEach(([key, top]) => {
    const element = container.querySelector(`[data-keep-scroll="${key}"]`);
    if (element) element.scrollTop = top;
  });
  container.scrollTop = ownScroll;
  if (focusKey) {
    const input = container.querySelector(`[data-draft="${focusKey}"]`);
    if (input) {
      input.focus();
      if (selection) input.setSelectionRange(selection[0], selection[1]);
    }
  }
}

function showPage(key) {
  if (Object.hasOwn(pageAliases, key)) {
    key = pageAliases[key];
    // 用 replaceState 換網址不會再觸發 hashchange，上一頁也不會回到舊網址
    history.replaceState(null, '', `${location.pathname}${location.search}#${key}`);
  }
  const page = pages.find(item => item.key === key) || pages[0];
  app.page = page.key;
  const sectionKey = builtPages.includes(page.key) ? page.key : 'placeholder';
  document.querySelectorAll('.rail-btn[data-page]').forEach(link => {
    if (link.dataset.page === page.key) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  document.querySelectorAll('.page').forEach(section => {
    section.hidden = section.dataset.page !== sectionKey;
  });
  $('#emptyLabel').textContent = page.label;
  $('#termPage').textContent = `— ${page.label}`;
  pageRenderers.forEach(render => safeRun(render, page.key));
}

function goToPage(key) {
  if (location.hash === `#${key}`) showPage(key);
  else location.hash = key;
}

function renderRail() {
  $('#railLinks').innerHTML = pages.map(page =>
    `<a class="rail-btn" href="#${page.key}" data-page="${page.key}" title="${page.label}">${icon(page.icon)}<span class="rail-lbl">${page.label}</span></a>`
  ).join('');
  document.querySelectorAll('[data-icon]').forEach(slot => {
    slot.outerHTML = icon(slot.dataset.icon, slot.className);
  });
}

// 系統監控頁的內容由 modules/system_monitor 的 monitor.js 畫；這裡只掛上節點，進出這一頁時開始或停止它的 3 秒輪詢
let monitorPage = null;
// 完整頁和控制台摘要共用同一個實例；網址照本頁專案，讀的是該專案的採集器與磁碟
function systemMonitor() {
  return SystemMonitor.shared({ apiUrl: apiUrl('/api/system-monitor') });
}
onPageChange(page => {
  if (!monitorPage) monitorPage = systemMonitor().mountPage($('#systemMonitorPage'));
  monitorPage.setVisible(page === 'system-monitor');
});

document.addEventListener('input', event => {
  if (event.target.dataset && event.target.dataset.draft) drafts[event.target.dataset.draft] = event.target.value;
});
