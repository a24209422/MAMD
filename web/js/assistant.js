// 右側儀表板助手：只用最新的 /api/state 回答，規則式比對，不呼叫任何模型
const botSender = { name: '儀表板助手', icon: 'chat', color: 'var(--green)' };
const botQuickAsks = ['等你處理的有哪些？', '整體進度', 'Context 偏高的有誰？', 'writer 在做什麼？'];
let chatOpen = false;

// 比對前去掉空白、連字號、底線，「Writer-Sub1」「writer sub1」都認得
function normalizeAsk(text) {
  return String(text).toLowerCase().replace(/[\s\-_]/g, '');
}

function listHtml(lines) {
  return `<ul>${lines.map(line => `<li>${escapeHtml(line)}</li>`).join('')}</ul>`;
}

function agentLine(state, agent) {
  if (!busyStatuses.includes(agent.status)) return `${agent.id}：${agent.status}`;
  const task = state.tasks.find(item => item.plan === agent.plan && item.task === agent.task);
  const pct = contextPct(agent.context);
  const rounds = task ? `第 ${task.round}/${task.maxRounds} 輪` : `第 ${agent.round} 輪`;
  return `${agent.id}：${agent.status}，${agent.plan} ${agent.task}${task ? ` ${task.title}` : ''}，${rounds}，`
    + `Context ${pct == null ? '計算中' : `${pct}%`}，最近動作 ${agent.action || '—'}`;
}

function taskLine(task) {
  const owner = task.status === '寫程式中' ? task.agent : task.status === '審查中' ? task.reviewAgent : null;
  const parts = [`${task.plan} ${task.task} ${task.title}：${task.status}`, `第 ${task.round}/${task.maxRounds} 輪`];
  if (owner) parts.push(`在 ${owner} 手上`);
  if (task.needs) parts.push(task.needs.reason);
  return parts.join('，');
}

function botReply(text) {
  const state = app.state;
  if (!state) return '還沒讀到資料，等一下再問。';
  const asked = normalizeAsk(text);
  // 長的先比，P0001T10 才不會被 P0001T1 搶先、writer-sub1 才不會被 writer 搶先
  const longestFirst = (items, keyOf) => [...items].sort((a, b) => keyOf(b).length - keyOf(a).length);
  const task = longestFirst(state.tasks, item => item.plan + item.task)
    .find(item => asked.includes(normalizeAsk(item.plan + item.task)));
  if (task) return escapeHtml(taskLine(task));
  const plan = state.plans.find(item => asked.includes(normalizeAsk(item.id)));
  if (plan) {
    const tasks = state.tasks.filter(item => item.plan === plan.id);
    return listHtml([`${plan.id} ${plan.title}：${plan.status}，完成 ${plan.tasks.done}/${plan.tasks.total}`, ...tasks.map(taskLine)]);
  }
  const agent = longestFirst(state.agents, item => item.id).find(item => asked.includes(normalizeAsk(item.id)));
  if (agent) return escapeHtml(agentLine(state, agent));
  if (/等你處理|要我處理|處理/.test(asked)) {
    if (!state.needsYou.length) return '目前沒有等你處理的任務。';
    return `等你處理的任務：${listHtml(state.needsYou.map(item => `${item.plan} ${item.task} ${item.title}：${item.reason}`))}`;
  }
  if (/context|上下文/.test(asked)) {
    const crowded = state.agents
      .map(item => [item, contextPct(item.context)])
      .filter(([item, pct]) => busyStatuses.includes(item.status) && pct != null && pct >= 40)
      .sort((a, b) => b[1] - a[1]);
    if (!crowded.length) return '目前在忙的 agent，Context 都在 40% 以下。';
    return `Context 在 40% 以上的：<ul>${crowded.map(([item, pct]) =>
      `<li>${escapeHtml(item.id)}：${pct >= 70 ? `<span class="hl">${pct}%</span>` : `${pct}%`}</li>`).join('')}</ul>`;
  }
  if (/進度|狀況|狀態|整體|總覽/.test(asked)) {
    const active = state.plans.filter(item => item.status === '進行中');
    const lines = [state.conductor.running ? 'conductor 運行中' : 'conductor 沒有啟動'];
    if (state.paused) lines.push(`已暫停：${state.pauseReason}`);
    if (!active.length) lines.push('沒有進行中的 plan');
    active.forEach(item => lines.push(`${item.id} ${item.title}：完成 ${item.tasks.done}/${item.tasks.total}`));
    if (state.needsYou.length) lines.push(`等你處理 ${state.needsYou.length} 個任務`);
    return listHtml(lines);
  }
  return '我可以查：某個 agent 在做什麼（例如「writer 在做什麼？」）、某個任務或 plan（例如「P0002 T2」）、等你處理的有哪些、Context 偏高的有誰、整體進度。';
}

function clockNow() {
  return new Date().toTimeString().slice(0, 5);
}

function appendMessage(thread, sender, kind, html, time) {
  const item = document.createElement('li');
  item.className = `msg msg-${kind}`;
  if (kind === 'user') {
    item.innerHTML = `<p>${html}</p><time>${time}</time>`;
  } else {
    item.style.setProperty('--a', sender.color);
    item.innerHTML = `
      <span class="agent-icon">${icon(sender.icon)}</span>
      <div>
        <div class="msg-meta"><b>${sender.name}</b><time>${time}</time></div>
        <div class="bubble">${html}</div>
      </div>`;
  }
  thread.append(item);
  thread.scrollTop = thread.scrollHeight;
}

function askBot(text) {
  appendMessage($('#botThread'), botSender, 'user', escapeHtml(text), clockNow());
  setTimeout(() => appendMessage($('#botThread'), botSender, 'agent', botReply(text), clockNow()), 300);
}

function setChatOpen(open) {
  chatOpen = open;
  $('#chatbar').classList.toggle('is-open', open);
  document.querySelectorAll('[data-chat-toggle]').forEach(button => button.setAttribute('aria-expanded', String(open)));
  if (open) $('#botThread').scrollTop = $('#botThread').scrollHeight;
}

document.addEventListener('click', event => {
  if (event.target.closest('[data-chat-toggle]')) setChatOpen(!chatOpen);
});

$('#botComposer').addEventListener('submit', event => {
  event.preventDefault();
  const text = $('#botInput').value.trim();
  if (!text) return;
  $('#botInput').value = '';
  askBot(text);
});

$('#botQuick').addEventListener('click', event => {
  const button = event.target.closest('[data-ask]');
  if (button) askBot(button.dataset.ask);
});

$('#botQuick').innerHTML = botQuickAsks.map(ask => `<button type="button" data-ask="${escapeHtml(ask)}">${escapeHtml(ask)}</button>`).join('');
appendMessage($('#botThread'), botSender, 'agent',
  '你好，我是儀表板助手。可以問我某個 agent 在做什麼、某個任務或 plan 的狀況、等你處理的有哪些，或整體進度。', clockNow());
// 視窗夠寬才預設展開，不然中間的三層會被擠得太窄
setChatOpen(window.innerWidth >= 1200);
