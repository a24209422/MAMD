// 總覽的三層：conductor、Claude／Codex 兩組、每個 agent 身分
const groupInfo = {
  claude: { name: 'Claude', role: '寫程式', icon: 'code', accent: 'blue', busyText: '寫程式中',
    duties: ['寫程式', '跑測試', '照審查意見修改', '解合併衝突'] },
  codex: { name: 'Codex', role: '審查', icon: 'shield', accent: 'violet', busyText: '審查中',
    duties: ['審查 diff', '列出必改與建議', '確認解衝突沒弄壞其他任務'] },
};

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
      <h3 class="agent-name"><span class="agent-icon">${icon('crown')}</span>conductor<span>（指揮程式）</span></h3>
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

function groupAgents(state, group) {
  return state.agents.filter(agent => agent.group === group);
}

function renderGroups(state) {
  $('#pair').innerHTML = Object.entries(groupInfo).map(([group, info]) => {
    const agents = groupAgents(state, group);
    const busy = agents.filter(agent => busyStatuses.includes(agent.status));
    const model = agents.length ? agents[0].model : null;
    const stateKey = state.paused ? 'paused' : busy.length ? 'run' : 'idle';
    const stateLabel = { paused: '已暫停', run: '運行中', idle: '閒置' }[stateKey];
    const doing = state.paused ? '已暫停' : busy.length ? `${busy.length} 個${info.busyText}` : '閒置';
    return `
      <article class="agent agent-mid" data-state="${stateKey}" data-accent="${info.accent}">
        <div class="agent-head">
          <h3 class="agent-name"><span class="agent-icon">${icon(info.icon)}</span>${info.name}<span>（${info.role}）</span></h3>
          ${model ? `<span class="tag" data-accent="${info.accent}">${escapeHtml(model)}</span>` : ''}
          <span class="state" data-state="${stateKey}">${stateLabel}</span>
        </div>
        <div class="mid-body">
          <div class="mid-main">
            <div class="task-block"><small>目前任務</small><p>${doing}</p></div>
            ${meter('忙碌', percent(busy.length, agents.length), 'progress', `${busy.length}/${agents.length}`)}
          </div>
          <ul class="duties">${info.duties.map(duty => `<li>${duty}</li>`).join('')}</ul>
        </div>
      </article>`;
  }).join('');
}

function agentCard(state, agent) {
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
  return `
    <button class="agent agent-sm" type="button" data-state="${cardState}" data-agent="${escapeHtml(agent.id)}" title="在控制台查看 ${escapeHtml(agent.id)}">
      <span class="agent-head"><span class="agent-icon">${icon('robot')}</span><span class="agent-name">${escapeHtml(agent.id)}</span></span>
      <span class="sm-state"><span class="state" data-state="${cardState}">${escapeHtml(agent.status)}</span></span>
      <span class="kv"><span>任務</span><b>${escapeHtml(taskText)}</b></span>
      ${rounds}
      ${context}
      <span class="kv"><span>最近動作</span><b>${escapeHtml(busy && agent.action ? agent.action : '—')}</b></span>
    </button>`;
}

function renderAgents(state) {
  $('#squads').innerHTML = Object.keys(groupInfo).map(group => {
    const agents = groupAgents(state, group);
    return `
      <div class="squad" style="--n: ${Math.max(1, agents.length)}" aria-label="${groupInfo[group].name} 的 agent 身分">
        ${agents.map(agent => agentCard(state, agent)).join('')}
      </div>`;
  }).join('');
}

function renderOverview(state) {
  renderLead(state);
  renderGroups(state);
  renderAgents(state);
}

// 點小卡到控制台看這個身分；控制台發現換了身分會自己重讀資料
$('#squads').addEventListener('click', event => {
  const card = event.target.closest('[data-agent]');
  if (!card) return;
  app.selectedAgent = card.dataset.agent;
  goToPage('console');
});

onStateChange(renderOverview);
