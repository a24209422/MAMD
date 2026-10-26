// 儀表板共用：API、輪詢、token、換頁和小工具。各面板在自己的檔案用 onStateChange、onPoll、onPageChange 註冊
const pollMs = 2000;
const busyStatuses = ['寫程式中', '解衝突中', '審查中'];
const pages = [
  { key: 'overview', label: '總覽', icon: 'grid' },
  { key: 'tasks', label: '任務', icon: 'clipboard' },
  { key: 'models', label: '模型', icon: 'cube' },
  { key: 'console', label: '控制台', icon: 'monitor' },
  { key: 'topology', label: '拓撲', icon: 'nodes' },
  { key: 'settings', label: '設定', icon: 'gear' },
];
const builtPages = ['overview', 'tasks', 'console'];
const iconPaths = {
  expand: '<path d="M5 6l6 6-6 6M12 6l6 6-6 6"/>',
  grid: '<rect x="4" y="4" width="7" height="7"/><rect x="13" y="4" width="7" height="7"/><rect x="4" y="13" width="7" height="7"/><rect x="13" y="13" width="7" height="7"/>',
  clipboard: '<rect x="5" y="5" width="14" height="16"/><path d="M9 3h6v4H9zM8.5 11.5h7M8.5 15.5h5"/>',
  cube: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5M12 12v9"/>',
  monitor: '<rect x="3" y="4" width="18" height="12"/><path d="M8 20h8M12 16v4M7 8h6M7 11.5h9"/>',
  nodes: '<circle cx="12" cy="12" r="2.5"/><circle cx="5" cy="5" r="2"/><circle cx="19" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/><path d="M6.5 6.5l3.7 3.7M17.5 6.5l-3.7 3.7M6.5 17.5l3.7-3.7M17.5 17.5l-3.7-3.7"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/>',
  crown: '<path d="M3 8l4.5 4L12 5l4.5 7L21 8l-2 11H5z"/>',
  code: '<path d="M8 7l-5 5 5 5M16 7l5 5-5 5M14 4l-4 16"/>',
  shield: '<path d="M12 3l8 3v6c0 4.5-3.4 8.2-8 9-4.6-.8-8-4.5-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-4.5"/>',
  robot: '<rect x="5" y="8" width="14" height="11"/><path d="M12 4.5V8M9 12.5v1.5M15 12.5v1.5M9.5 16.5h5M3 12v4M21 12v4"/><circle cx="12" cy="3.5" r="1"/>',
  send: '<path d="M4 12L20 4l-6 16-3-7z"/><path d="M11 13l9-9"/>',
  chat: '<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9.5h8M8 12.5h5"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
};
const app = { state: null, stateText: '', token: '', online: true, page: 'overview', selectedAgent: null, selectedPlan: null };
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

async function apiGetText(path) {
  const response = await fetch(path, { cache: 'no-store' });
  const text = await response.text();
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      message = JSON.parse(text).message || message;
    } catch (error) {
      // 不是 JSON 就用狀態碼當訊息
    }
    throw new Error(message);
  }
  return text;
}

async function apiGet(path) {
  return JSON.parse(await apiGetText(path));
}

async function apiPost(path, body = {}) {
  try {
    const response = await fetch(path, {
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
  $('#notice').innerHTML = notes.map(note => `<p>${escapeHtml(note)}</p>`).join('');
  $('#notice').hidden = !notes.length;
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
    if (text !== app.stateText) {
      app.stateText = text;
      app.state = JSON.parse(text);
      renderAll();
    } else {
      renderChrome();
    }
    pollRenderers.forEach(render => safeRun(render, app.state));
  } catch (error) {
    app.online = false;
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

document.addEventListener('input', event => {
  if (event.target.dataset && event.target.dataset.draft) drafts[event.target.dataset.draft] = event.target.value;
});
