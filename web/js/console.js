// 控制台：agent 身分的詳細資料。Astra 對話窗和系統資源這一期還沒接資料，版面上直接寫明
const consoleTabs = [
  { key: 'detail', label: '任務詳情' },
  { key: 'chat', label: '對話內容' },
  { key: 'files', label: '檔案變更' },
  { key: 'tools', label: '工具輸出' },
  { key: 'context', label: 'Context 分析' },
];
const compactNote = '<p class="empty-note">「自動摘要壓縮記錄」「立即執行摘要壓縮」「建立新 Session」還沒有接資料。</p>';
const consoleUi = { tab: 'detail', log: null, diff: null, reviewName: null, reviewText: '', bodyHtml: '', openTools: new Set() };

// 沒選過或選的身分不在了（例如 maxParallel 改小），預設看第一個在忙的，都沒在忙就看第一個
function currentAgent(state) {
  if (!state.agents.some(agent => agent.id === app.selectedAgent)) {
    const first = state.agents.find(agent => busyStatuses.includes(agent.status)) || state.agents[0];
    app.selectedAgent = first ? first.id : null;
  }
  return state.agents.find(agent => agent.id === app.selectedAgent) || null;
}

function renderConsole(state) {
  if (app.page !== 'console') return;
  const agent = currentAgent(state);
  if (!agent) {
    $('#taskHead').innerHTML = '';
    $('#workerPicker').innerHTML = '';
    $('#taskTabs').innerHTML = '';
    showConsoleBody('<p class="muted">沒有 agent 身分。</p>');
    return;
  }
  const cardState = agentCardState(agent);
  const info = groupInfo[agent.group];
  $('#taskPanel').dataset.state = cardState;
  $('#taskHead').innerHTML = `
    <span class="agent-icon">${icon('robot')}</span>
    <h3 class="agent-name">${escapeHtml(agent.id)}<span>（${info.name}・${info.role}）</span></h3>
    <span class="state" data-state="${cardState}">${escapeHtml(agent.status)}</span>`;
  $('#workerPicker').innerHTML = state.agents.map(item =>
    `<button class="mini" type="button" data-agent="${escapeHtml(item.id)}" data-state="${agentCardState(item)}" aria-pressed="${item.id === agent.id}">${escapeHtml(item.id)}</button>`
  ).join('');
  $('#taskTabs').innerHTML = consoleTabs.map(tab =>
    `<button class="tab" type="button" role="tab" data-tab="${tab.key}" aria-selected="${tab.key === consoleUi.tab}">${tab.label}</button>`
  ).join('');
  renderConsoleBody();
}

async function loadConsoleData() {
  const agentId = app.selectedAgent;
  if (app.page !== 'console' || !agentId) return;
  let log;
  try {
    log = await apiGet(`/api/agents/${encodeURIComponent(agentId)}/log`);
  } catch (error) {
    if (agentId === app.selectedAgent) showConsoleBody(`<p class="op-msg">讀不到 ${escapeHtml(agentId)} 的 log：${escapeHtml(error.message)}</p>`);
    return;
  }
  let diff = null;
  if (consoleUi.tab === 'files' && log.plan) {
    try {
      diff = await apiGet(`/api/tasks/${encodeURIComponent(log.plan)}/${encodeURIComponent(log.task)}/diff`);
    } catch (error) {
      diff = { message: `讀不到檔案變更：${error.message}` };
    }
  }
  // 讀的時候可能已經換了身分
  if (agentId !== app.selectedAgent) return;
  if (!consoleUi.log || consoleUi.log.agent !== log.agent || consoleUi.log.file !== log.file) consoleUi.openTools.clear();
  consoleUi.log = log;
  consoleUi.diff = diff;
  renderConsoleBody();
}

// 內容一樣就不重畫，展開的工具和捲動位置才會留著
function showConsoleBody(html) {
  if (html === consoleUi.bodyHtml) return;
  consoleUi.bodyHtml = html;
  rerender($('#taskBody'), html);
}

function renderConsoleBody() {
  const state = app.state;
  const agent = state && currentAgent(state);
  if (!agent) return;
  if (consoleUi.tab === 'context') {
    showConsoleBody(consoleContext(agent));
    return;
  }
  const log = consoleUi.log;
  if (!log || log.agent !== agent.id) {
    showConsoleBody('<p class="muted">讀取中…</p>');
    return;
  }
  const views = {
    detail: () => consoleDetail(state, log),
    chat: () => consoleChat(log),
    files: () => consoleFiles(log, consoleUi.diff),
    tools: () => consoleTools(state, log),
  };
  showConsoleBody(views[consoleUi.tab]());
}

function findTaskOf(state, log) {
  return state.tasks.find(item => item.plan === log.plan && item.task === log.task);
}

function logHeader(log) {
  const notes = [log.file];
  if (log.running) notes.push('執行中');
  if (log.truncated) notes.push('log 太大，只顯示最後一段');
  return `<p class="op-hint">${escapeHtml(`${log.plan} ${log.task} 第 ${log.round} 輪・${notes.join('・')}`)}</p>`;
}

function consoleDetail(state, log) {
  if (!log.plan) return '<p class="empty-note">還沒有做過任務。</p>';
  const task = findTaskOf(state, log);
  if (!task) return `<p class="muted">找不到 ${escapeHtml(log.plan)} ${escapeHtml(log.task)}，plan 檔可能已經移除。</p>`;
  const rows = [
    ['任務', `${task.plan} ${task.task} ${task.title}`],
    ['狀態', task.status],
    ['輪數', `第 ${task.round}/${task.maxRounds} 輪`],
    ['模式', task.mode === 'resolve' ? '解合併衝突' : '寫程式'],
    ['前置', task.dependsOn.length ? task.dependsOn.join('、') : '無'],
    ['worktree', task.worktree],
    task.needs ? ['等你處理', task.needs.reason] : null,
    ['上次測試', task.lastTest ? (task.lastTest.passed ? '通過' : '失敗') : '還沒跑'],
  ].filter(Boolean);
  const testTail = task.lastTest && task.lastTest.tail
    ? `<pre class="code-block" data-keep-scroll="test">${escapeHtml(task.lastTest.tail)}</pre>` : '';
  const reviews = task.reviews.length
    ? `<ul class="tool-list">${task.reviews.map(name =>
      `<li><button class="link" type="button" data-review="${escapeHtml(name)}">${escapeHtml(name)}</button></li>`).join('')}</ul>`
    : '<p class="muted">還沒有審查意見。</p>';
  const reviewBody = consoleUi.reviewName && task.reviews.includes(consoleUi.reviewName)
    ? `<pre class="code-block" data-keep-scroll="review">${escapeHtml(consoleUi.reviewText)}</pre>` : '';
  return `<dl class="detail">${rows.map(([label, value]) => `<div><dt>${label}</dt><dd>${escapeHtml(value)}</dd></div>`).join('')}</dl>
    ${testTail}
    <h3 class="ops-sub">審查意見</h3>${reviews}${reviewBody}`;
}

function consoleChat(log) {
  if (!log.plan) return '<p class="empty-note">還沒有做過任務。</p>';
  if (!log.file) return '<p class="muted">找不到這一輪的 log。</p>';
  const messages = log.messages.length
    ? `<ol class="msg-list">${log.messages.map(message => `<li>${escapeHtml(message.text)}</li>`).join('')}</ol>`
    : '<p class="muted">這段 log 裡沒有 agent 說的話。</p>';
  return logHeader(log) + messages;
}

function consoleFiles(log, diff) {
  if (!log.plan) return '<p class="empty-note">還沒有做過任務。</p>';
  if (!diff) return '<p class="muted">讀取中…</p>';
  if (diff.message) return `<p class="muted">${escapeHtml(diff.message)}</p>`;
  const base = diff.base === 'merge' ? '已合併：合併進 plan 分支的改動' : '還沒合併：任務分支相對 plan 分支的改動';
  const pending = diff.pending
    ? `<h3 class="ops-sub">還沒 commit 的改動</h3><pre class="code-block" data-keep-scroll="pending">${escapeHtml(diff.pending)}</pre>` : '';
  return `<p class="op-hint">${escapeHtml(`${log.plan} ${log.task}・${base}${diff.truncated ? '（diff 太大，只顯示前面）' : ''}`)}</p>
    <pre class="code-block" data-keep-scroll="stat">${escapeHtml(diff.stat || '（沒有改動）')}</pre>
    ${pending}
    ${diff.diff ? `<pre class="code-block" data-keep-scroll="diff">${escapeHtml(diff.diff)}</pre>` : ''}`;
}

function consoleTools(state, log) {
  if (!log.plan) return '<p class="empty-note">還沒有做過任務。</p>';
  let tools = '<p class="muted">找不到這一輪的 log。</p>';
  if (log.file) {
    tools = log.tools.length
      ? `<ul class="tool-list">${log.tools.map((tool, index) => `
          <li><details data-tool="${index}"${consoleUi.openTools.has(index) ? ' open' : ''}>
            <summary>${escapeHtml(tool.name)} ${escapeHtml(tool.target)}${tool.truncated ? '（已截斷）' : ''}</summary>
            <pre class="code-block">${escapeHtml(tool.input)}</pre>
            ${tool.result ? `<pre class="code-block">${escapeHtml(tool.result)}</pre>` : ''}
          </details></li>`).join('')}</ul>`
      : '<p class="muted">這段 log 裡沒有工具呼叫。</p>';
  }
  const task = findTaskOf(state, log);
  const test = task && task.lastTest
    ? `<h3 class="ops-sub">上次測試（${task.lastTest.passed ? '通過' : '失敗'}）</h3><pre class="code-block" data-keep-scroll="test">${escapeHtml(task.lastTest.tail || '')}</pre>`
    : '';
  return (log.file ? logHeader(log) : '') + tools + test;
}

function consoleContext(agent) {
  if (!busyStatuses.includes(agent.status)) {
    return `<p class="empty-note">${escapeHtml(agent.id)} 現在${escapeHtml(agent.status)}，沒有進行中的對話。</p>${compactNote}`;
  }
  const pct = contextPct(agent.context);
  if (pct == null) return `<p class="muted">計算中：agent 還沒回覆第一則訊息。</p>${compactNote}`;
  const level = contextLevel(pct);
  const width = Math.min(100, pct);
  const text = {
    high: `Context 使用率 ${pct}%，已經超過 70%，偏高。`,
    mid: `Context 使用率 ${pct}%，還在 70% 以下。`,
    ok: `Context 使用率 ${pct}%，空間充足。`,
  }[level];
  return `
    <div class="ctx-head" data-level="${level}">
      <span>Context 使用率</span>
      <strong>${pct}%</strong>
      <small>(${tokensK(agent.context.tokens)} / ${tokensK(agent.context.window)} tokens)</small>
    </div>
    <span class="meter meter-lg" data-level="${level}"><span class="meter-track"><i style="width:${width}%"></i></span></span>
    <div class="scale" aria-hidden="true">
      <span class="scale-bar"><i style="left:${width}%"></i></span>
      <span class="scale-ticks"><span style="left:0%">0%</span><span style="left:40%">40%</span><span style="left:70%">70%</span><span style="left:100%">100%</span></span>
    </div>
    <p class="alert" data-level="${level}"><b aria-hidden="true">!</b><span>${text}</span></p>
    ${compactNote}`;
}

$('#workerPicker').addEventListener('click', event => {
  const button = event.target.closest('[data-agent]');
  if (!button) return;
  app.selectedAgent = button.dataset.agent;
  refresh(renderConsole);
  loadConsoleData();
});

$('#taskTabs').addEventListener('click', event => {
  const tab = event.target.closest('[data-tab]');
  if (!tab) return;
  consoleUi.tab = tab.dataset.tab;
  refresh(renderConsole);
  loadConsoleData();
});

$('#taskBody').addEventListener('click', async event => {
  const button = event.target.closest('[data-review]');
  if (!button) return;
  const name = button.dataset.review;
  if (consoleUi.reviewName === name) {
    consoleUi.reviewName = null;
  } else {
    try {
      consoleUi.reviewText = (await apiGet(`/api/reviews/${encodeURIComponent(name)}`)).text;
    } catch (error) {
      consoleUi.reviewText = `讀不到審查意見：${error.message}`;
    }
    consoleUi.reviewName = name;
  }
  renderConsoleBody();
});

// details 的 toggle 事件不會冒泡，要在捕捉階段接
$('#taskBody').addEventListener('toggle', event => {
  const details = event.target;
  if (!details.dataset || details.dataset.tool === undefined) return;
  const index = Number(details.dataset.tool);
  if (details.open) consoleUi.openTools.add(index);
  else consoleUi.openTools.delete(index);
}, true);

onStateChange(renderConsole);
onPoll(() => loadConsoleData());
onPageChange(page => {
  if (page !== 'console') return;
  refresh(renderConsole);
  loadConsoleData();
});
