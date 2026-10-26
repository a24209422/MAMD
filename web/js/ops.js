// 總覽左邊的切換格：即時活動、異常警告、人工介入（核准、退回、decide、停止與繼續）
const opsTabs = [
  { key: 'feed', label: '即時活動', title: '即時活動紀錄' },
  { key: 'alerts', label: '異常警告', title: '異常警告' },
  { key: 'manual', label: '人工介入', title: '人工介入' },
];
// open 記住每一筆展開了哪個確認框，results 記住失敗訊息；每 2 秒重畫時照這裡畫回去
const opsUi = { tab: 'feed', feedFilter: 'all', open: {}, results: {}, warnings: {}, alertIds: '' };
const stuckMs = 5 * 60 * 1000;
const retryToQueue = '回到排隊，寫程式的 agent 接手（會附上你的說明）';
const retryHints = {
  rounds: '輪數上限 +1，寫程式的 agent 帶著最新審查意見和你的說明再改一輪',
  blocked: retryToQueue,
  noChanges: retryToQueue,
  commitFailed: retryToQueue,
  internalError: retryToQueue,
  agentFailed: '回到失敗的那個階段重跑',
  invalidReview: '重新審查（說明不會交給審查的 agent）',
  mergeConflict: '再交給寫程式的 agent 解一次（會附上你的說明）',
  mergeFailed: '重試合併',
};
const readAlerts = loadReadAlerts();

function loadReadAlerts() {
  try {
    return new Set(JSON.parse(localStorage.getItem('boardReadAlerts') || '[]'));
  } catch (error) {
    return new Set();
  }
}

function saveReadAlerts() {
  try {
    localStorage.setItem('boardReadAlerts', JSON.stringify([...readAlerts]));
  } catch (error) {
    // 存不了就只記到關掉頁面為止
  }
}

// 條件消失的警告不再記已讀，下次再發生要重新提醒
function pruneReadAlerts(alerts) {
  const ids = new Set(alerts.map(alert => alert.id));
  const stale = [...readAlerts].filter(id => !ids.has(id));
  stale.forEach(id => readAlerts.delete(id));
  if (stale.length) saveReadAlerts();
}

function whoColor(who) {
  if (who.startsWith('writer')) return 'var(--blue)';
  if (who.startsWith('reviewer')) return 'var(--violet)';
  return who === '你' ? 'var(--amber)' : 'var(--green)';
}

function feedView(state) {
  const names = ['all', ...new Set(state.recentEvents.map(item => item.who))];
  if (!names.includes(opsUi.feedFilter)) opsUi.feedFilter = 'all';
  const items = opsUi.feedFilter === 'all' ? state.recentEvents : state.recentEvents.filter(item => item.who === opsUi.feedFilter);
  const options = names.map(name =>
    `<option value="${escapeHtml(name)}" ${name === opsUi.feedFilter ? 'selected' : ''}>${name === 'all' ? '全部' : escapeHtml(name)}</option>`
  ).join('');
  const list = items.length
    ? items.map(item => `
      <li>
        <time>${clock(item.time)}</time>
        <b class="who" style="--who: ${whoColor(item.who)}">${escapeHtml(item.who)}</b>
        <span class="what">${escapeHtml(item.text)}</span>
      </li>`).join('')
    : '<li><span class="what">還沒有事件</span></li>';
  return `<label class="feed-filter"><span>篩選</span><select data-feed-filter>${options}</select></label><ol class="feed">${list}</ol>`;
}

function currentAlerts(state) {
  const alerts = state.needsYou.map(item => ({
    id: `needs-${item.plan}-${item.task}-${item.since}`, kind: '等你處理', level: 'high',
    agent: `${item.plan} ${item.task}`, at: item.since, text: item.reason,
  }));
  if (state.paused) {
    alerts.push({ id: 'paused', kind: '已暫停', level: 'warn', agent: '全部', at: null, text: state.pauseReason || '已暫停' });
  }
  const hasWork = state.plans.some(plan => plan.status === '待核准' || plan.status === '進行中');
  if (!state.conductor.running && hasWork) {
    alerts.push({ id: 'conductor-off', kind: 'conductor 沒在跑', level: 'warn', agent: 'conductor', at: null,
      text: '有待核准或進行中的 plan，但 conductor 沒有啟動，核准後也不會開始。' });
  }
  state.recentEvents.filter(event => event.type === 'agentFailed').forEach(event => alerts.push({
    id: `failed-${event.time}-${event.plan}-${event.task}`, kind: 'agent 失敗', level: 'warn',
    agent: event.agent || `${event.plan} ${event.task}`, at: event.time, text: `${event.text}（第一次失敗會自動重試）`,
  }));
  state.agents.filter(agent => busyStatuses.includes(agent.status)).forEach(agent => {
    if (agent.lastActivity && Date.now() - Date.parse(agent.lastActivity) > stuckMs) {
      alerts.push({ id: `stuck-${agent.id}-${agent.plan}-${agent.task}`, kind: '卡住', level: 'warn', agent: agent.id,
        at: agent.lastActivity, text: `${agent.plan} ${agent.task} 的 log 超過 5 分鐘沒有新內容（最近動作：${agent.action || '—'}）。` });
    }
    const pct = contextPct(agent.context);
    if (pct != null && pct >= 70) {
      alerts.push({ id: `ctx-${agent.id}-${agent.plan}-${agent.task}`, kind: 'Context 偏高', level: 'warn', agent: agent.id,
        at: null, text: `Context 使用率 ${pct}%（${tokensK(agent.context.tokens)} / ${tokensK(agent.context.window)} tokens）。` });
    }
  });
  return alerts;
}

function alertsView(alerts) {
  if (!alerts.length) return '<p class="muted">目前沒有異常。</p>';
  // 未讀的排前面，已讀的沉到下面
  const sorted = [...alerts.filter(alert => !readAlerts.has(alert.id)), ...alerts.filter(alert => readAlerts.has(alert.id))];
  return sorted.map(alert => {
    const read = readAlerts.has(alert.id);
    return `
      <article class="alert-item" data-level="${alert.level}" data-read="${read}">
        <div class="ai-head"><b>${escapeHtml(alert.kind)}・${escapeHtml(alert.agent)}</b><time>${clock(alert.at)}</time></div>
        <p>${escapeHtml(alert.text)}</p>
        ${read ? '<span class="ai-done">已讀</span>' : `<button class="link" type="button" data-op="read" data-id="${escapeHtml(alert.id)}">標為已讀</button>`}
      </article>`;
  }).join('');
}

function resultLine(key) {
  const message = opsUi.results[key];
  return message ? `<p class="op-msg">${escapeHtml(message)}</p>` : '';
}

function planItem(plan) {
  const key = `plan:${plan.id}`;
  const planId = escapeHtml(plan.id);
  const counts = `紅 ${plan.problems.red}・橘 ${plan.problems.orange}・黃 ${plan.problems.yellow}`;
  const mode = opsUi.open[key];
  let actions;
  if (mode === 'approve') {
    const warnings = opsUi.warnings[plan.id] || [];
    actions = `<div class="op-form">
        <p class="op-hint">核准前請確認：</p>
        <ul class="op-warn">${warnings.map(warning => `<li>${escapeHtml(warning)}</li>`).join('')}</ul>
        <div class="ap-actions">
          <button class="mini-btn" type="button" data-op="cancel" data-key="${key}">取消</button>
          <button class="mini-btn mini-ok" type="button" data-op="approve-confirm" data-plan="${planId}"${writeDisabled()}>確定核准</button>
        </div>
      </div>`;
  } else if (mode === 'reject') {
    actions = `<div class="op-form">
        <input type="text" data-draft="reject:${planId}" placeholder="退回原因（必填）" aria-label="退回原因">
        <div class="ap-actions">
          <button class="mini-btn" type="button" data-op="cancel" data-key="${key}">取消</button>
          <button class="mini-btn mini-danger" type="button" data-op="reject" data-plan="${planId}"${writeDisabled()}>送出退回</button>
        </div>
      </div>`;
  } else {
    actions = `<div class="ap-actions">
        <button class="mini-btn" type="button" data-op="trace" data-plan="${planId}">看追溯樹</button>
        <button class="mini-btn mini-ok" type="button" data-op="approve" data-plan="${planId}"${writeDisabled()}>核准</button>
        <button class="mini-btn" type="button" data-op="open" data-key="${key}" data-mode="reject"${writeDisabled()}>退回</button>
      </div>`;
  }
  return `
    <li>
      <div class="ai-head"><b>${planId} 待核准</b><time></time></div>
      <p>${escapeHtml(plan.title)}｜${counts}</p>
      ${actions}${resultLine(key)}
    </li>`;
}

function needsItem(item) {
  const key = `task:${item.plan}/${item.task}`;
  const ids = `data-plan="${escapeHtml(item.plan)}" data-task="${escapeHtml(item.task)}"`;
  const mode = opsUi.open[key];
  const conflicts = item.conflicts.length ? `<p>衝突檔案：${item.conflicts.map(escapeHtml).join('、')}</p>` : '';
  let actions;
  if (mode === 'done' || mode === 'abandon') {
    let text = '確定放棄這個任務？';
    if (mode === 'done') {
      text = item.kind === 'mergeConflict'
        ? `到 ${item.planWorktree} 執行 git merge task/${item.plan}-${item.task} 解完並 commit，或在 ${item.taskWorktree} 清掉衝突標記後再按。`
        : `確定已經在 ${item.taskWorktree} 改好？conductor 會直接合併。`;
    }
    actions = `<div class="op-form">
        <p class="op-hint">${escapeHtml(text)}</p>
        <div class="ap-actions">
          <button class="mini-btn" type="button" data-op="cancel" data-key="${key}">取消</button>
          <button class="mini-btn mini-danger" type="button" data-op="decide" data-action="${mode}" ${ids}${writeDisabled()}>${mode === 'done' ? '確定完成' : '確定放棄'}</button>
        </div>
      </div>`;
  } else {
    actions = `<div class="ap-actions">
        <button class="mini-btn mini-ok" type="button" data-op="decide" data-action="retry" ${ids}${writeDisabled()}>重試</button>
        <button class="mini-btn" type="button" data-op="open" data-key="${key}" data-mode="done"${writeDisabled()}>完成</button>
        <button class="mini-btn mini-danger" type="button" data-op="open" data-key="${key}" data-mode="abandon"${writeDisabled()}>放棄</button>
      </div>
      <p class="op-hint">重試：${escapeHtml(retryHints[item.kind] || retryToQueue)}</p>`;
  }
  return `
    <li>
      <div class="ai-head"><b>${escapeHtml(item.plan)} ${escapeHtml(item.task)} 等你處理</b><time>${clock(item.since)}</time></div>
      <p>${escapeHtml(item.title)}</p>
      <p>${escapeHtml(item.reason)}</p>
      ${conflicts}
      <div class="op-form"><input type="text" data-draft="note:${escapeHtml(item.plan)}/${escapeHtml(item.task)}" placeholder="給寫程式的 agent 的說明（選填）" aria-label="給寫程式的 agent 的說明"></div>
      ${actions}${resultLine(key)}
    </li>`;
}

function stopBlock(state) {
  if (state.paused) {
    return `<button class="act act-primary" type="button" data-op="resume"${writeDisabled()}>▶ 全部繼續</button>`;
  }
  if (opsUi.open.stop) {
    return `<div class="op-form">
        <p class="op-hint">會結束所有 agent，進行中的任務回到排隊。</p>
        <div class="ap-actions">
          <button class="mini-btn" type="button" data-op="cancel" data-key="stop">取消</button>
          <button class="mini-btn mini-danger" type="button" data-op="stop"${writeDisabled()}>確定停止</button>
        </div>
      </div>`;
  }
  return `<button class="act act-danger" type="button" data-op="open" data-key="stop"${writeDisabled()}>⏹ 停止全部 Agent</button>`;
}

function manualView(state) {
  const pendingPlans = state.plans.filter(plan => plan.status === '待核准');
  const items = [...pendingPlans.map(planItem), ...state.needsYou.map(needsItem)];
  const agentList = `<ul class="ctl-list">${state.agents.map(agent => `
      <li><span class="ctl-name">${escapeHtml(agent.id)}</span><span class="state" data-state="${agentCardState(agent)}">${escapeHtml(agent.status)}</span></li>`).join('')}</ul>
    <p class="op-hint">個別暫停還沒有這個功能。</p>`;
  return `${stopBlock(state)}${resultLine('stop')}
    <h3 class="ops-sub">個別 Agent</h3>${agentList}
    <h3 class="ops-sub">待處理<span>${items.length}</span></h3>
    ${items.length ? `<ul class="approvals">${items.join('')}</ul>` : '<p class="muted">沒有待核准的 plan 或等你處理的任務。</p>'}`;
}

function renderOps(state) {
  const alerts = currentAlerts(state);
  pruneReadAlerts(alerts);
  opsUi.alertIds = alerts.map(alert => alert.id).join('|');
  const unread = alerts.filter(alert => !readAlerts.has(alert.id));
  const waiting = state.plans.filter(plan => plan.status === '待核准').length + state.needsYou.length;
  const badges = {
    feed: '',
    alerts: unread.length ? `<b class="badge" data-tone="${unread.some(alert => alert.level === 'high') ? 'red' : 'amber'}">${unread.length}</b>` : '',
    manual: waiting ? `<b class="badge">${waiting}</b>` : '',
  };
  $('#opsTabs').innerHTML = opsTabs.map(tab =>
    `<button class="tab" type="button" role="tab" data-ops="${tab.key}" aria-selected="${tab.key === opsUi.tab}">${tab.label}${badges[tab.key]}</button>`
  ).join('');
  $('#opsLabel').textContent = opsTabs.find(tab => tab.key === opsUi.tab).title;
  const html = opsUi.tab === 'feed' ? feedView(state) : opsUi.tab === 'alerts' ? alertsView(alerts) : manualView(state);
  rerender($('#opsBody'), html);
}

async function runWrite(op, data) {
  if (!app.token) return;
  let key;
  let result;
  if (op === 'stop' || op === 'resume') {
    key = 'stop';
    result = await apiPost(`/api/${op}`);
  } else if (op === 'approve' || op === 'approve-confirm') {
    key = `plan:${data.plan}`;
    const body = op === 'approve' ? { confirm: false } : { confirm: true, warnings: opsUi.warnings[data.plan] || [] };
    result = await apiPost(`/api/plans/${encodeURIComponent(data.plan)}/approve`, body);
    if (result.needsConfirm) {
      // 有警告要你確認；警告和上次看到的不同時也會回到這裡，畫面換成新的清單
      opsUi.warnings[data.plan] = result.warnings;
      opsUi.open[key] = 'approve';
      delete opsUi.results[key];
      refresh(renderOps);
      return;
    }
  } else if (op === 'reject') {
    key = `plan:${data.plan}`;
    const reason = (drafts[`reject:${data.plan}`] || '').trim();
    if (!reason) {
      opsUi.results[key] = '退回要寫原因';
      refresh(renderOps);
      return;
    }
    result = await apiPost(`/api/plans/${encodeURIComponent(data.plan)}/reject`, { reason });
    if (result.ok) delete drafts[`reject:${data.plan}`];
  } else if (op === 'decide') {
    key = `task:${data.plan}/${data.task}`;
    const noteKey = `note:${data.plan}/${data.task}`;
    result = await apiPost(`/api/tasks/${encodeURIComponent(data.plan)}/${encodeURIComponent(data.task)}/decide`,
      { action: data.action, note: (drafts[noteKey] || '').trim() });
    if (result.ok) delete drafts[noteKey];
  } else {
    return;
  }
  if (result.ok) {
    delete opsUi.open[key];
    delete opsUi.results[key];
    pollState();
  } else {
    opsUi.results[key] = result.message;
  }
  refresh(renderOps);
}

$('#opsTabs').addEventListener('click', event => {
  const tab = event.target.closest('[data-ops]');
  if (!tab) return;
  opsUi.tab = tab.dataset.ops;
  refresh(renderOps);
});

$('#opsBody').addEventListener('change', event => {
  if (!event.target.matches('[data-feed-filter]')) return;
  opsUi.feedFilter = event.target.value;
  refresh(renderOps);
});

$('#opsBody').addEventListener('click', event => {
  const button = event.target.closest('[data-op]');
  if (!button || button.disabled) return;
  const { op, key, mode, id, plan } = button.dataset;
  if (op === 'read') {
    readAlerts.add(id);
    saveReadAlerts();
  } else if (op === 'open') {
    opsUi.open[key] = mode || 'confirm';
    delete opsUi.results[key];
  } else if (op === 'cancel') {
    delete opsUi.open[key];
  } else if (op === 'trace') {
    showPlan(plan);
    return;
  } else {
    // 等 server 回應前先停用按鈕，避免連按送出兩次
    button.disabled = true;
    runWrite(op, button.dataset);
    return;
  }
  refresh(renderOps);
});

onStateChange(renderOps);
// 「卡住」是看時間經過，狀態沒變也可能出現；每次輪詢檢查警告清單有沒有變
onPoll(state => {
  if (currentAlerts(state).map(alert => alert.id).join('|') !== opsUi.alertIds) renderOps(state);
});
