// 總覽的三層：Alfred、各角色的主要 agent、按任務需要啟動的子 agent。
const roleInfo = {
  writer: { role: '寫程式', icon: 'code', accent: 'blue', busyText: '寫程式中',
    duties: ['寫程式', '跑測試', '照審查意見修改', '解合併衝突'] },
  reviewer: { role: '審查', icon: 'shield', accent: 'violet', busyText: '審查中',
    duties: ['審查 diff', '列出必改與建議', '確認解衝突沒弄壞其他任務'] },
};
// 每個角色用哪個 CLI 由 .board/config.json 決定，卡片標題跟著設定顯示
const cliNames = { claude: 'Claude', codex: 'Codex' };

function cliLabel(cli) {
  return cliNames[cli] || cli || '';
}

function activePlans(state) {
  return state.plans.filter(plan => plan.status === '進行中');
}

function leadStatus(state) {
  if (state.paused) return ['paused', '已暫停'];
  return state.conductor.running ? ['run', '運行中'] : ['idle', '沒有啟動'];
}

function renderLead(state) {
  const plans = activePlans(state);
  const total = plans.reduce((sum, plan) => sum + plan.tasks.total, 0);
  const done = plans.reduce((sum, plan) => sum + plan.tasks.done, 0);
  const [stateKey, stateLabel] = leadStatus(state);
  const projectName = state.project.split(/[\\/]/).filter(Boolean).pop();
  const pauseNote = state.paused && state.pauseReason ? `<p class="op-hint">${escapeHtml(state.pauseReason)}</p>` : '';
  $('#leadCard').dataset.paused = String(stateKey !== 'run');
  $('#leadCard').innerHTML = `
    <div class="agent-head">
      <h3 class="agent-name"><span class="agent-icon">${icon('crown')}</span>Alfred<span>（指揮程式）</span></h3>
      <span class="tag">${escapeHtml(projectName)}</span>
      <span class="state" data-state="${stateKey}">${stateLabel}</span>
    </div>
    <div class="lead-body">
      <div class="task-block"><small>目前任務</small><p>${plans.length ? `進行中 ${plans.length} 份 plan` : '沒有進行中的 plan'}</p>${pauseNote}</div>
      <div class="meters">${meter('任務進度', percent(done, total), 'progress', `${done}/${total}`)}</div>
    </div>`;
  $('#focusList').innerHTML = plans.length
    ? plans.map(plan => `<li>${escapeHtml(plan.id)} ${escapeHtml(plan.title)}　${plan.tasks.done}/${plan.tasks.total}</li>`).join('')
    : '<li>沒有進行中的 plan</li>';
}

function roleAgents(state, role) {
  return state.agents.filter(agent => agent.role === role);
}

function renderGroups(state) {
  $('#pair').innerHTML = Object.keys(roleInfo).map(role => {
    const main = roleAgents(state, role).find(agent => agent.id === role);
    return main ? agentCard(state, main, true) : '<p class="muted">主要 agent 資料尚未載入</p>';
  }).join('');
}

function agentCard(state, agent, primary = false) {
  const cardState = agentCardState(agent);
  const busy = busyStatuses.includes(agent.status);
  const task = busy ? state.tasks.find(item => item.plan === agent.plan && item.task === agent.task) : null;
  const taskText = busy ? `${agent.plan} ${agent.task} ${task ? task.title : ''}` : '—';
  const rounds = task
    ? meter('輪數', percent(task.round, task.maxRounds), 'progress', `${task.round}/${task.maxRounds}`)
    : meter('輪數', null, 'progress', '—');
  const pct = contextPct(agent.context);
  let context = meter('Context', null, 'ok', '—');
  if (busy) context = pct == null ? meter('Context', null, 'ok', '計算中') : meter('Context', pct, contextLevel(pct));
  const info = roleInfo[agent.role];
  const childIndex = agent.id.match(/-sub(\d+)$/)?.[1];
  const name = primary
    ? `${escapeHtml(cliLabel(agent.cli))}<span>（${info.role}）</span>`
    : escapeHtml(childIndex ? `子 Agent ${childIndex}` : agent.id);
  const status = agent.status === '閒置' ? (primary ? '待命' : '未啟動') : agent.status;
  const badge = primary ? `<span class="tag">${escapeHtml(agent.model || '預設模型')}</span>` : '';
  return `
    <button class="agent ${primary ? 'agent-mid' : 'agent-sm'}" type="button" data-state="${cardState}" data-accent="${info.accent}" data-agent="${escapeHtml(agent.id)}" data-agent-kind="${primary ? 'main' : 'sub'}" title="在控制台查看${primary ? '主要' : '子'} agent ${escapeHtml(agent.id)}">
      <span class="agent-head"><span class="agent-icon">${icon(primary ? info.icon : 'robot')}</span><span class="agent-name">${name}</span>${badge}</span>
      <span class="sm-state"><span class="state" data-state="${cardState}">${escapeHtml(status)}</span></span>
      <span class="kv"><span>任務</span><b>${escapeHtml(taskText)}</b></span>
      ${rounds}
      ${context}
      <span class="kv"><span>最近動作</span><b>${escapeHtml(busy && agent.action ? agent.action : '—')}</b></span>
    </button>`;
}

function renderAgents(state) {
  $('#squads').innerHTML = Object.keys(roleInfo).map(role => {
    const agents = roleAgents(state, role).filter(agent => agent.id !== role);
    return `
      <div class="squad" data-empty="${agents.length === 0}" style="--n: ${Math.max(1, agents.length)}" aria-label="${roleInfo[role].role}的子 agent">
        ${agents.length ? agents.map(agent => agentCard(state, agent)).join('') : '<p class="muted">無子 agent 名額</p>'}
      </div>`;
  }).join('');
}

function renderOverview(state) {
  renderLead(state);
  renderGroups(state);
  renderAgents(state);
}

// 主要與子 agent 都能開啟自己的控制台；按鈕同時支援鍵盤操作。
function openAgentConsole(event) {
  const card = event.target.closest('[data-agent]');
  if (!card) return;
  app.selectedAgent = card.dataset.agent;
  goToPage('console');
}
for (const selector of ['#pair', '#squads']) $(selector).addEventListener('click', openAgentConsole);

onStateChange(renderOverview);
