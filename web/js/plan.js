// 任務頁：plan 追溯樹。節點、連線和紅橘黃問題都由 server 用 rtm_graph.py 算好，這裡只負責畫
const planStatusOrder = ['待核准', '進行中', '退回', '草稿', '完成', '被取代', '放棄'];
const abandonableStatuses = ['草稿', '待核准', '退回', '進行中'];
const severityOrder = [['red', '紅', 'var(--red)'], ['orange', '橘', 'var(--orange)'], ['yellow', '黃', 'var(--amber)']];
const planKindLabels = { plan: 'PLAN', requirement: '需求', decision: '做法', task: '任務', verify: '驗證' };
const gapLabels = { missing: '漏做', extra: '多做', incomplete: '資訊不完整' };
const planUi = { side: 'all', module: 'all', detail: null, detailText: '', treeText: '', confirmAbandon: false, message: '' };

function planSides(plans) {
  return [...new Set(plans.flatMap(plan => plan.area.map(item => item.side)))];
}

function plansInScope(plans) {
  if (planUi.side === 'all') return plans;
  return plans.filter(plan => plan.area.some(item =>
    item.side === planUi.side && (planUi.module === 'all' || item.module === planUi.module)));
}

function renderPlanScope(plans) {
  const sides = planSides(plans);
  if (planUi.side !== 'all' && !sides.includes(planUi.side)) {
    planUi.side = 'all';
    planUi.module = 'all';
  }
  const sideButtons = [['all', '全部'], ...sides.map(side => [side, side])].map(([value, label]) =>
    `<button type="button" data-plan-side="${escapeHtml(value)}" aria-pressed="${value === planUi.side}">${escapeHtml(label)}</button>`
  ).join('');
  let moduleSelect = '';
  if (planUi.side !== 'all') {
    const modules = [...new Set(plans.flatMap(plan =>
      plan.area.filter(item => item.side === planUi.side && item.module).map(item => item.module)))];
    if (planUi.module !== 'all' && !modules.includes(planUi.module)) planUi.module = 'all';
    const options = [['all', '全部'], ...modules.map(name => [name, name])].map(([value, label]) =>
      `<option value="${escapeHtml(value)}" ${value === planUi.module ? 'selected' : ''}>${escapeHtml(label)}</option>`
    ).join('');
    moduleSelect = `<label class="plan-pick"><span>功能模組</span><select data-plan-module>${options}</select></label>`;
  }
  $('#planScope').innerHTML = `<div class="scope-tabs" role="group" aria-label="範圍">${sideButtons}</div>${moduleSelect}`;
}

// 每個狀態一個選單，只顯示有 plan 的狀態；只有正在看的 plan 所屬的選單有選取值
function renderPlanPicks(scoped, plan) {
  const statuses = planStatusOrder.filter(status => scoped.some(item => item.status === status));
  $('#planPicks').innerHTML = statuses.map(status => {
    const group = scoped.filter(item => item.status === status);
    const active = Boolean(plan) && plan.status === status;
    const options = group.map(item =>
      `<option value="${escapeHtml(item.id)}" ${active && item.id === plan.id ? 'selected' : ''}>${escapeHtml(item.id)} ${escapeHtml(item.title)}</option>`
    ).join('');
    return `
      <label class="plan-pick" data-active="${active}">
        <span>${status}<b>${group.length}</b></span>
        <select data-plan-pick><option value="" ${active ? '' : 'selected'}>選擇…</option>${options}</select>
      </label>`;
  }).join('');
}

function clearPlanDetail(message, isError = false) {
  planUi.detail = null;
  planUi.detailText = '';
  planUi.treeText = '';
  $('#planStatus').hidden = true;
  $('#planStats').hidden = true;
  $('#planStatsList').innerHTML = '';
  $('#planMeta').innerHTML = '';
  $('#planActions').innerHTML = '';
  $('#planProblems').innerHTML = `<p class="${isError ? 'op-msg' : 'muted'}">${escapeHtml(message)}</p>`;
  $('#planTree').innerHTML = '';
}

function renderPlanPage(state) {
  if (app.page !== 'tasks') return;
  renderPlanScope(state.plans);
  const scoped = plansInScope(state.plans);
  if (!scoped.some(item => item.id === app.selectedPlan)) {
    const ordered = planStatusOrder.flatMap(status => scoped.filter(item => item.status === status));
    app.selectedPlan = ordered.length ? ordered[0].id : null;
  }
  const plan = scoped.find(item => item.id === app.selectedPlan);
  renderPlanPicks(scoped, plan);
  if (!plan) {
    clearPlanDetail(state.plans.length ? '這個範圍沒有 plan。' : 'plans/ 裡還沒有 plan。');
    return;
  }
  if (!planUi.detail || planUi.detail.id !== plan.id) loadPlanDetail(plan.id);
}

async function loadPlanDetail(planId) {
  let text;
  try {
    text = await apiGetText(`/api/plans/${encodeURIComponent(planId)}`);
  } catch (error) {
    if (planId === app.selectedPlan) clearPlanDetail(`讀不到 ${planId}：${error.message}`, true);
    return;
  }
  // 讀的時候可能已經換了 plan 或換了頁；內容沒變就不重畫，捲動位置才會留著
  if (planId !== app.selectedPlan || app.page !== 'tasks' || text === planUi.detailText) return;
  planUi.detailText = text;
  planUi.detail = JSON.parse(text);
  // 進行中的 plan 耗時每次輪詢都會變，只有成效變了就只重畫成效，追溯樹不動
  const { stats, ...rest } = planUi.detail;
  const treeText = JSON.stringify(rest);
  if (treeText !== planUi.treeText) {
    planUi.treeText = treeText;
    renderPlanDetail();
  }
  renderPlanStats();
}

// 數字的寫法和 board.py stats P0007 一樣，兩邊對得起來
function statsDuration(seconds) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const parts = hours ? [`${hours} 小時`] : [];
  if (hours || minutes) parts.push(`${minutes} 分`);
  return [...parts, `${seconds % 60} 秒`].join(' ');
}

function statsModels(items) {
  const names = items.map(combo => Object.values(combo).filter(Boolean).join(' ')).filter(Boolean);
  return names.join('、') || '-';
}

function statsDecisions(decisions) {
  const counts = ['retry', 'done', 'abandon'].filter(action => decisions[action]).map(action => `${action} ${decisions[action]}`);
  return counts.length ? `${decisions.total}（${counts.join('、')}）` : String(decisions.total);
}

function statsItems(stats) {
  const { tasks, reviews, tests } = stats;
  const rounds = stats.taskRounds.map(item => `${item.task} ${item.rounds ? `${item.rounds} 輪` : '還沒開始'}｜${item.status}`);
  return [
    ['任務', `完成 ${tasks.done}／放棄 ${tasks.abandoned}／全部 ${tasks.total}`],
    ['平均輪數', stats.averageRounds === null ? '-' : String(stats.averageRounds)],
    ['寫完次數', String(stats.written)],
    ['審查', `${reviews.total} 次（通過 ${reviews.passed}、不通過 ${reviews.failed}）`],
    ['第一次審查就通過', `${stats.firstReviewPassed.passed}/${stats.firstReviewPassed.reviewed}`],
    ['測試', `${tests.runs} 次，失敗 ${tests.failed}，退回 ${tests.returned}`],
    ['寫完後被退回', `${stats.returnedAfterWrite.returned}/${stats.returnedAfterWrite.written}`],
    ['你的決定', statsDecisions(stats.decisions)],
    ['還在等你', String(stats.waiting)],
    ['等你的時間', statsDuration(stats.waitSeconds)],
    ['agent 失敗、合併衝突', `${stats.agentFailed}、${stats.conflicts}`],
    ['plan 退回', String(stats.planRejected)],
    ['耗時', statsDuration(stats.duration.seconds) + (stats.duration.inProgress ? '（進行中）' : '')],
    ['模型', `writer：${statsModels(stats.models.writer)}；reviewer：${statsModels(stats.models.reviewer)}`],
    ['每個任務的輪數', rounds.join('\n')],
  ];
}

// 還沒核准過的 plan 沒有成效，整個區塊藏起來
function renderPlanStats() {
  const stats = planUi.detail && planUi.detail.stats;
  $('#planStats').hidden = !stats;
  $('#planStatsList').innerHTML = stats
    ? statsItems(stats).map(([label, value]) => `<dt>${label}</dt><dd>${escapeHtml(value)}</dd>`).join('')
    : '';
}

function renderPlanDetail() {
  const detail = planUi.detail;
  $('#planStatus').hidden = false;
  $('#planStatus').dataset.status = detail.status;
  $('#planStatus').textContent = detail.status;
  const metaItems = [
    detail.branch ? ['分支', detail.branch] : null,
    detail.area.length ? ['範圍', detail.area.map(item => item.module ? `${item.side}・${item.module}` : item.side).join('、')] : null,
    detail.created ? ['建立', detail.created] : null,
    detail.supersedes.length ? ['取代', detail.supersedes.join('、')] : null,
    detail.supersededBy.length ? ['被取代', detail.supersededBy.join('、')] : null,
    ['來源', detail.source],
  ].filter(Boolean);
  $('#planMeta').innerHTML = metaItems.map(([label, value]) => `<div><dt>${label}</dt><dd>${escapeHtml(value)}</dd></div>`).join('');
  renderPlanActions();
  // blockers 已經包含紅色問題，這裡只另外列出紅色問題以外的原因（例如缺標題），同一行才不會出現兩次
  const extraBlockers = detail.blockers.filter(text => !detail.problems.red.includes(text));
  const blocked = detail.blockers.length
    ? `<p class="plan-blocked">不能核准：紅色問題要先修好</p>${extraBlockers.length
      ? `<ul class="plan-problems">${extraBlockers.map(text => `<li style="--k: var(--red)">紅｜${escapeHtml(text)}</li>`).join('')}</ul>` : ''}`
    : '';
  const problemItems = severityOrder.flatMap(([severity, label, color]) =>
    detail.problems[severity].map(text => `<li style="--k: ${color}">${label}｜${escapeHtml(text)}</li>`));
  const problemList = problemItems.length ? `<ul class="plan-problems">${problemItems.join('')}</ul>` : '';
  const okNote = detail.blockers.length || problemItems.length ? '' : '<p class="plan-ok">沒有斷線：每條需求都有做法、任務和驗證。</p>';
  $('#planProblems').innerHTML = blocked + problemList + okNote;
  $('#planTree').innerHTML = detail.graph
    ? `<ul class="tree">${planTreeItem(buildChildren(detail.graph), 'root', 'solid', new Set())}</ul>`
    : '<p class="muted">這份 plan 找不到追溯矩陣表格。</p>';
}

function renderPlanActions() {
  const detail = planUi.detail;
  if (!detail) return;
  const message = planUi.message ? `<span class="op-msg">${escapeHtml(planUi.message)}</span>` : '';
  if (!abandonableStatuses.includes(detail.status)) {
    $('#planActions').innerHTML = message;
    return;
  }
  const controls = planUi.confirmAbandon
    ? `<span class="op-hint">進行中的 agent 會被結束。</span>
       <button class="mini-btn" type="button" data-plan-op="cancel">取消</button>
       <button class="mini-btn mini-danger" type="button" data-plan-op="abandon"${writeDisabled()}>確定放棄</button>`
    : `<button class="mini-btn mini-danger" type="button" data-plan-op="ask"${writeDisabled()}>放棄這份 plan</button>`;
  $('#planActions').innerHTML = controls + message;
}

function buildChildren(graph) {
  const nodes = new Map(graph.nodes.map(node => [node.key, node]));
  const children = new Map();
  graph.edges.forEach(edge => {
    if (!children.has(edge.from)) children.set(edge.from, []);
    children.get(edge.from).push(edge);
  });
  return { nodes, children };
}

function planNodeHtml(node) {
  const nodeClass = node.class;
  if (node.kind === 'gap') {
    return `<div class="pnode" data-class="${nodeClass}"><small>${gapLabels[nodeClass]}</small><b>${escapeHtml(node.text)}</b></div>`;
  }
  if (node.kind === 'plan' || node.kind === 'verify') {
    return `<div class="pnode" data-class="${nodeClass}"><small>${planKindLabels[node.kind]}</small><b>${escapeHtml(node.text)}</b></div>`;
  }
  let note = '';
  if (node.kind === 'requirement') note = `<p>理解：${escapeHtml(node.note || '（未寫）')}</p>`;
  else if (node.kind === 'decision' && node.note) note = `<p>不採用：${escapeHtml(node.note)}</p>`;
  const id = node.id ? `<span class="pid">${escapeHtml(node.id)}</span>` : '';
  return `<div class="pnode" data-class="${nodeClass}"><small>${planKindLabels[node.kind]}</small><b>${id}${escapeHtml(node.text)}</b>${note}</div>`;
}

function planTreeItem(graph, key, edgeStyle, seen) {
  seen.add(key);
  const childEdges = (graph.children.get(key) || []).filter(edge => !seen.has(edge.to));
  const childItems = childEdges.map(edge => planTreeItem(graph, edge.to, edge.style, seen)).join('');
  return `<li data-edge="${edgeStyle}">${planNodeHtml(graph.nodes.get(key))}${childItems ? `<ul>${childItems}</ul>` : ''}</li>`;
}

function selectPlan(planId) {
  app.selectedPlan = planId;
  planUi.confirmAbandon = false;
  planUi.message = '';
  refresh(renderPlanPage);
}

// 從別的面板跳過來看某份 plan：範圍先放寬到全部，那份 plan 才一定選得到
function showPlan(planId) {
  planUi.side = 'all';
  planUi.module = 'all';
  selectPlan(planId);
  goToPage('tasks');
}

$('#planScope').addEventListener('click', event => {
  const button = event.target.closest('[data-plan-side]');
  if (!button || button.dataset.planSide === planUi.side) return;
  planUi.side = button.dataset.planSide;
  planUi.module = 'all';
  refresh(renderPlanPage);
});

$('#planScope').addEventListener('change', event => {
  if (!event.target.matches('[data-plan-module]')) return;
  planUi.module = event.target.value;
  refresh(renderPlanPage);
});

$('#planPicks').addEventListener('change', event => {
  if (!event.target.matches('[data-plan-pick]') || !event.target.value) return;
  selectPlan(event.target.value);
});

$('#planActions').addEventListener('click', async event => {
  const button = event.target.closest('[data-plan-op]');
  if (!button || !planUi.detail) return;
  const op = button.dataset.planOp;
  if (op === 'ask') planUi.confirmAbandon = true;
  if (op === 'cancel') planUi.confirmAbandon = false;
  if (op === 'abandon') {
    if (!app.token) return;
    button.disabled = true;
    const result = await apiPost(`/api/plans/${encodeURIComponent(planUi.detail.id)}/abandon`);
    planUi.confirmAbandon = false;
    planUi.message = result.ok ? '' : result.message;
    if (result.ok) pollState();
  }
  renderPlanActions();
});

onStateChange(renderPlanPage);
// plan 檔被 Codex 改了不會反映在 /api/state，在任務頁時每次輪詢都重讀這份 plan
onPoll(() => {
  if (app.page === 'tasks' && app.selectedPlan) loadPlanDetail(app.selectedPlan);
});
onPageChange(page => {
  if (page === 'tasks') refresh(renderPlanPage);
});
