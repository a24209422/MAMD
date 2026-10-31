// Human-operated shell sessions. Only rendering lives in the browser.
const terminals = new Map();
let selectedTerminal = null;
let terminalBusy = false;
let terminalRevision = 0;
let terminalOpen = window.innerWidth >= 1200;
let terminalWidth = 480;
try { terminalWidth = Number(localStorage.getItem('terminalWidth')) || 480; } catch (_) { /* storage optional */ }

function terminalNotice(text) { $('#terminalStatus').textContent = text; }

function setTerminalOpen(open) {
  terminalOpen = open;
  if (!open) $('#chatbar').classList.remove('maximized');
  $('#chatbar').classList.toggle('is-open', open);
  document.querySelectorAll('[data-chat-toggle]').forEach(button => button.setAttribute('aria-expanded', String(open)));
  requestAnimationFrame(fitTerminal);
}

function setTerminalWidth(width) {
  terminalWidth = Math.max(320, Math.min(width, Math.max(320, window.innerWidth - 360)));
  $('#chatbar').style.setProperty('--terminal-width', `${terminalWidth}px`);
  try { localStorage.setItem('terminalWidth', String(terminalWidth)); } catch (_) { /* storage optional */ }
  fitTerminal();
}

function fitTerminal() {
  const item = terminals.get(selectedTerminal);
  if (!item || !terminalOpen || item.screen.clientWidth < 10) return;
  item.fit.fit();
  sendTerminal(item, { type: 'resize', cols: item.term.cols, rows: item.term.rows });
}

function sendTerminal(item, message) {
  if (item.socket && item.socket.readyState === WebSocket.OPEN) item.socket.send(JSON.stringify(message));
}

function selectTerminal(id) {
  selectedTerminal = id;
  for (const [key, item] of terminals) item.screen.hidden = key !== id;
  renderTerminalTabs();
  requestAnimationFrame(() => { fitTerminal(); terminals.get(id)?.term.focus(); });
}

function renderTerminalTabs() {
  $('#terminalTabs').replaceChildren();
  for (const [id, item] of terminals) {
    const group = document.createElement('div');
    group.className = 'terminal-tab';
    group.setAttribute('aria-selected', String(id === selectedTerminal));
    const tab = document.createElement('button');
    tab.type = 'button'; tab.role = 'tab';
    tab.textContent = item.title + (item.running ? '' : '（已退出）');
    tab.setAttribute('aria-selected', String(id === selectedTerminal));
    tab.onclick = () => selectTerminal(id);
    const close = document.createElement('button');
    close.type = 'button'; close.textContent = '×'; close.title = `關閉 ${item.title}`;
    close.onclick = async () => {
      if (!window.confirm(`關閉 ${item.title}？其中的程式會一起結束。`)) return;
      const result = await apiPost('/api/terminals/close', { id });
      if (!result.ok) { terminalNotice(result.message); return; }
      terminalRevision++;
      disposeTerminal(id);
      selectTerminal(terminals.keys().next().value || null);
    };
    group.append(tab, close); $('#terminalTabs').append(group);
  }
}

function disposeTerminal(id) {
  const item = terminals.get(id);
  item.disposed = true;
  clearTimeout(item.retry);
  item.socket?.close(); item.term.dispose(); item.screen.remove(); terminals.delete(id);
}

function addTerminal(view) {
  const screen = document.createElement('div');
  screen.className = 'terminal-screen'; screen.hidden = true;
  $('#terminalScreens').append(screen);
  const term = new Terminal({ cursorBlink: true, scrollback: 5000, fontSize: 13,
    theme: { background: '#10151b', foreground: '#dae2ea' } });
  const fit = new FitAddon.FitAddon(); term.loadAddon(fit); term.open(screen);
  const item = { ...view, screen, term, fit, socket: null, disposed: false, connecting: false, taken: false };
  terminals.set(view.id, item);
  term.onData(text => {
    // Split large pastes below the transport's 64 KiB message limit.
    const characters = Array.from(text);
    for (let offset = 0; offset < characters.length; offset += 4096) sendTerminal(item, { type: 'input', text: characters.slice(offset, offset + 4096).join('') });
  });
  term.attachCustomKeyEventHandler(event => {
    if (event.type !== 'keydown') return true;
    if (event.ctrlKey && event.key.toLowerCase() === 'c' && term.hasSelection()) {
      navigator.clipboard?.writeText(term.getSelection()).catch(() => {}); return false;
    }
    return true;
  });
  connectTerminal(item);
  return item;
}

async function connectTerminal(item) {
  if (item.disposed || item.connecting || item.taken) return;
  item.connecting = true;
  const result = await apiPost('/api/terminals/connect', { id: item.id });
  item.connecting = false;
  if (item.disposed) return;
  if (!result.ok) {
    terminalNotice(result.message);
    item.retry = setTimeout(() => syncTerminals(), 2000); return;
  }
  const socket = new WebSocket(result.url); item.socket = socket;
  socket.onopen = () => {
    item.term.reset();
    socket.send(JSON.stringify({ ticket: result.ticket }));
    fitTerminal(); terminalNotice('已連線');
  };
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.type !== 'output') return;
    if (message.truncated) { item.term.reset(); terminalNotice('較早輸出已省略；互動式畫面可能需要重新繪製'); }
    item.running = message.running; renderTerminalTabs();
    item.term.write(message.text, () => sendTerminal(item, { type: 'ack' }));
  };
  socket.onclose = event => {
    if (item.disposed) return;
    if (event.code === 4001) { item.taken = true; terminalNotice('已由另一視窗接管；重新整理可接回'); return; }
    terminalNotice('終端連線中斷，正在重新連接…');
    item.retry = setTimeout(() => syncTerminals(), 2000);
  };
}

async function syncTerminals() {
  if (!app.token) { $('#terminalNew').disabled = true; terminalNotice('請用 server.py 印出的網址開啟，以操作終端機'); return; }
  if (terminalBusy) return;
  terminalBusy = true;
  const revision = terminalRevision;
  const result = await apiPost('/api/terminals/list');
  terminalBusy = false;
  if (!result.ok) { terminalNotice(result.message); return; }
  // A list requested before create/close must not overwrite the newer local state.
  if (revision !== terminalRevision) { syncTerminals(); return; }
  for (const id of terminals.keys()) if (!result.sessions.some(view => view.id === id)) disposeTerminal(id);
  for (const view of result.sessions) {
    const item = terminals.get(view.id) || addTerminal(view);
    item.running = view.running;
    if (!item.socket || item.socket.readyState === WebSocket.CLOSED) connectTerminal(item);
  }
  if (!terminals.has(selectedTerminal)) selectTerminal(terminals.keys().next().value || null);
  else renderTerminalTabs();
}

$('#terminalNew').onclick = async () => {
  $('#terminalNew').disabled = true;
  const result = await apiPost('/api/terminals/create');
  $('#terminalNew').disabled = false;
  if (!result.ok) { terminalNotice(result.message); return; }
  terminalRevision++;
  addTerminal(result.session); setTerminalOpen(true); selectTerminal(result.session.id);
};
document.addEventListener('click', event => { if (event.target.closest('[data-chat-toggle]')) setTerminalOpen(!terminalOpen); });
$('#terminalMax').onclick = () => { $('#chatbar').classList.toggle('maximized'); setTerminalOpen(true); };
const terminalHandle = $('#terminalResize');
terminalHandle.onpointerdown = event => { terminalHandle.setPointerCapture(event.pointerId); };
terminalHandle.onpointermove = event => { if (terminalHandle.hasPointerCapture(event.pointerId)) setTerminalWidth(window.innerWidth - event.clientX); };
terminalHandle.onkeydown = event => {
  if (['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); setTerminalWidth(terminalWidth + (event.key === 'ArrowLeft' ? 20 : -20)); }
};
new ResizeObserver(fitTerminal).observe($('#terminalScreens'));
window.addEventListener('resize', () => setTerminalWidth(terminalWidth));
setTerminalWidth(terminalWidth); setTerminalOpen(terminalOpen);
// main.js initializes the shared token after loading this module.
window.addEventListener('DOMContentLoaded', syncTerminals);
