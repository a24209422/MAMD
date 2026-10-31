// 系統監控模組的前端：完整頁（CPU、RAM、磁碟三張卡＋CPU／RAM 最近一分鐘走勢）和控制台摘要。
// 兩種畫面共用同一份快取和同一個輪詢排程，與儀表板 /api/state 的 2 秒輪詢分開。
//
// 主系統的接法（掛載節點和換頁時機由主系統決定，模組不碰導覽與 board 狀態）：
//   const monitor = SystemMonitor.shared({ apiUrl: '/api/projects/<id>/system-monitor' });  // 不給 apiUrl 時用模組預設網址
//   const page = monitor.mountPage(element);
//   const summary = monitor.mountSummary(element, { href: '#system-monitor' });
//   page.setVisible(true);   // 進入這一頁；離開時 setVisible(false)
//   page.unmount();          // 不再使用：清空節點，之後才回來的資料也不會寫進去
// 有任何一個畫面看得到、且瀏覽器頁籤沒有隱藏時，立刻讀一次，之後每 3 秒讀一次；都看不到就停。
//
// 整個檔案包在函式裡，只對外放 SystemMonitor，不會和 web/js 的全域名稱撞到
(function (root) {
  const apiPath = '/api/system-monitor';
  const intervalMs = 3000;
  const windowMs = 60000;
  // 距最後一次成功超過 10 秒就標成過期；相鄰兩筆相隔超過 10 秒，走勢也斷開不連線
  const staleMs = 10000;
  const requestTimeoutMs = 10000;
  // 依 sampledAt 裁切後一分鐘約 21 筆；另設筆數上限，主機時間異常時也不會一直變大
  const maxPoints = 120;
  const gib = 1024 ** 3;
  const plotWidth = 600;
  const plotHeight = 160;
  const itemKeys = ['cpu', 'memory', 'disk'];
  const statusLabels = { 'warming-up': '取樣中', unsupported: '不支援', error: '讀取失敗' };
  const chartTitles = { cpu: 'CPU 使用率', memory: 'RAM 使用率' };
  const pageSkeleton = `
    <div class="smon">
      <header class="smon-head">
        <h2 class="smon-title">server 主機資源</h2>
        <p class="smon-meta" data-smon-slot="meta"></p>
      </header>
      <div class="smon-status" role="status" data-smon-slot="status"></div>
      <div class="smon-cards" data-smon-slot="cards"></div>
      <div class="smon-charts">
        <section class="smon-chart" data-key="cpu" aria-label="CPU 使用率走勢" data-smon-slot="cpuChart"></section>
        <section class="smon-chart" data-key="memory" aria-label="RAM 使用率走勢" data-smon-slot="memoryChart"></section>
      </div>
    </div>`;

  // 自己產生的錯誤訊息直接顯示；其他（fetch 本身失敗）一律當成連不到 server
  class LoadError extends Error {}

  function escapeHtml(text) {
    return String(text ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  }

  function isNumber(value) {
    return typeof value === 'number' && Number.isFinite(value);
  }

  function percentText(value) {
    return `${value.toFixed(1)}%`;
  }

  function gibText(bytes) {
    return isNumber(bytes) ? `${(bytes / gib).toFixed(1)} GiB` : '—';
  }

  function usageText(used, total) {
    return isNumber(used) && isNumber(total) ? `${(used / gib).toFixed(1)} / ${(total / gib).toFixed(1)} GiB` : '—';
  }

  // sampledAt 是 server 主機的當地時間，例如 2026-10-27T10:00:03.123+08:00，畫面只顯示時:分:秒
  function timeText(isoText) {
    return typeof isoText === 'string' ? isoText.slice(11, 19) : '';
  }

  function level(pct) {
    if (pct >= 90) return 'high';
    if (pct >= 70) return 'mid';
    return 'ok';
  }

  // 只有 status 是 ok 且帶數字才算有效值；取樣中、不支援、失敗的數值都是 null，不能當成 0%
  function readItem(item) {
    if (!item || typeof item !== 'object') return { state: 'error', label: '讀取失敗', message: '資料格式不正確' };
    if (item.status === 'ok') {
      return isNumber(item.percent) ? { state: 'ok', percent: item.percent }
        : { state: 'error', label: '讀取失敗', message: '資料格式不正確' };
    }
    const known = Object.prototype.hasOwnProperty.call(statusLabels, item.status);
    return { state: known ? item.status : 'error', label: known ? statusLabels[item.status] : '讀取失敗', message: item.message || '' };
  }

  function validPercent(item) {
    const read = readItem(item);
    return read.state === 'ok' ? read.percent : null;
  }

  async function fetchPayload(fetchImpl, url, signal) {
    const response = await fetchImpl(url, { cache: 'no-store', signal });
    const text = await response.text();
    if (!response.ok) {
      let message = '';
      try {
        message = JSON.parse(text).message || '';
      } catch (error) {
        // 不是 JSON 就只報狀態碼
      }
      throw new LoadError(`server 回應 HTTP ${response.status}${message ? `：${message}` : ''}`);
    }
    let payload = null;
    try {
      payload = JSON.parse(text);
    } catch (error) {
      // 和缺欄位一樣當成格式不正確
    }
    if (!payload || typeof payload !== 'object' || !isNumber(payload.sampleId)
        || !itemKeys.every(key => payload[key] && typeof payload[key] === 'object')) {
      throw new LoadError('資料格式不正確');
    }
    return payload;
  }

  function meterHtml(pct) {
    const width = pct === null ? 0 : Math.min(100, Math.max(0, pct));
    return `<span class="smon-meter" aria-hidden="true"><i style="width:${width}%"></i></span>`;
  }

  function staleTag(stale) {
    return stale ? '<span class="smon-tag" data-state="stale">過期</span>' : '';
  }

  function createMonitor(options = {}) {
    const fetchImpl = options.fetch || ((...args) => root.fetch(...args));
    // 主系統依本頁專案給網址，每個專案讀自己的採集器；模組單獨使用時沿用預設網址
    const url = options.apiUrl || apiPath;
    const now = options.now || (() => Date.now());
    const setTimer = options.setTimeout || ((fn, ms) => root.setTimeout(fn, ms));
    const clearTimer = options.clearTimeout || (id => root.clearTimeout(id));
    const doc = options.document || root.document || null;
    // latest：最後一次成功的回應；error：最近一次讀取失敗的原因，成功後清掉
    // segment：走勢的段落編號，讀取失敗或停止輪詢錯過讀取時加一，不同段的點不連線
    const cache = { latest: null, receivedAt: 0, error: null, history: [], segment: 0 };
    const views = new Set();
    const viewOf = new WeakMap();
    let timer = null;
    let nextTickAt = null;
    let loading = false;

    if (doc && typeof doc.addEventListener === 'function') doc.addEventListener('visibilitychange', sync);

    function active() {
      return !(doc && doc.hidden) && [...views].some(view => view.visible);
    }

    // 已經在輪詢就不重設計時器：換頁進出、兩個畫面輪流顯示都只有一個排程
    function sync() {
      if (active()) {
        if (timer !== null) return;
        // 停止期間錯過了原本排定的讀取（切頁、頁籤隱藏），回來的資料另起一段，不和停止前連線
        if (nextTickAt !== null && now() >= nextTickAt) cache.segment += 1;
        tick();
      } else if (timer !== null) {
        clearTimer(timer);
        timer = null;
      }
    }

    function tick() {
      timer = setTimer(tick, intervalMs);
      nextTickAt = now() + intervalMs;
      // 上一次還沒回來就不再發，請求不會重疊；只重畫，讓太久沒更新的數值標成過期
      if (loading) renderAll();
      else load();
    }

    async function load() {
      loading = true;
      // 依發出請求時的段落記錄；停止前就發出、停止後才回來的資料仍接在停止前那一段
      const segment = cache.segment;
      const controller = new AbortController();
      // server 卡住時中斷請求，不然連線一直佔著，之後也不會再讀
      const timeout = setTimer(() => controller.abort(), requestTimeoutMs);
      let payload = null;
      let failure = '';
      try {
        payload = await fetchPayload(fetchImpl, url, controller.signal);
      } catch (error) {
        if (controller.signal.aborted) failure = `server 超過 ${requestTimeoutMs / 1000} 秒沒有回應`;
        else failure = error instanceof LoadError ? error.message : '連不到 server';
      }
      clearTimer(timeout);
      loading = false;
      if (payload) accept(payload, segment);
      else fail(failure);
      // 只重畫還掛著的畫面；已卸載的節點不會再被寫入
      renderAll();
    }

    function accept(payload, segment) {
      cache.latest = payload;
      cache.receivedAt = now();
      cache.error = null;
      addPoint(payload, segment);
    }

    function fail(message) {
      cache.error = message;
      // 之後的新資料不和中斷前的連線
      cache.segment += 1;
    }

    // 只加入新的 sampleId；時間用 server 的 sampledAt，不用瀏覽器收到的時間
    function addPoint(payload, segment) {
      const history = cache.history;
      const t = Date.parse(payload.sampledAt);
      const last = history[history.length - 1];
      if (!Number.isFinite(t) || (last && last.sampleId === payload.sampleId)) return;
      // server 重啟或主機時間往回調，前後接不起來，從這一筆重新累積
      if (last && t <= last.t) history.length = 0;
      const previous = history[history.length - 1];
      history.push({
        t,
        sampleId: payload.sampleId,
        sampledAt: payload.sampledAt,
        cpu: validPercent(payload.cpu),
        memory: validPercent(payload.memory),
        segment,
        // 斷線過、停止輪詢錯過讀取，或隔太久才有下一筆，都另起一段
        broken: !previous || previous.segment !== segment || t - previous.t > staleMs,
      });
      while (history.length && (history[0].t < t - windowMs || history.length > maxPoints)) history.shift();
    }

    function isStale() {
      return cache.latest !== null && (cache.error !== null || now() - cache.receivedAt > staleMs);
    }

    function renderAll() {
      views.forEach(view => {
        if (view.visible) view.render();
      });
    }

    function itemState(key) {
      if (cache.latest) return readItem(cache.latest[key]);
      return cache.error ? { state: 'error', label: '無法取得', message: '' } : { state: 'loading', label: '載入中', message: '' };
    }

    function itemTitle(key) {
      if (key === 'cpu') return 'CPU';
      if (key === 'memory') return 'RAM';
      const volume = cache.latest && cache.latest.disk.volume;
      return volume ? `磁碟 ${volume}` : '磁碟';
    }

    function details(key) {
      const item = cache.latest[key];
      if (key === 'memory') return [['已用', usageText(item.usedBytes, item.totalBytes)], ['可用', gibText(item.availableBytes)]];
      if (key === 'disk') return [['已用', usageText(item.usedBytes, item.totalBytes)], ['剩餘', gibText(item.freeBytes)]];
      return [['範圍', '所有邏輯處理器合計']];
    }

    function metaHtml(stale) {
      const time = cache.latest ? timeText(cache.latest.sampledAt) : '';
      return `<span>監控對象：server 主機</span>
        <span>最後資料 <b>${escapeHtml(time || '—')}</b></span>${staleTag(stale)}
        <span>每 ${intervalMs / 1000} 秒更新</span>`;
    }

    function statusHtml(stale) {
      if (cache.error) {
        const kept = cache.latest ? '，以下保留最後一次取得的數值並標為過期' : '';
        return `<p class="smon-banner" data-state="error">資料暫時無法取得（${escapeHtml(cache.error)}）${kept}；恢復後會自動更新。</p>`;
      }
      if (!cache.latest) return '<p class="smon-banner" data-state="loading">載入中…</p>';
      if (stale) return `<p class="smon-banner" data-state="stale">已超過 ${staleMs / 1000} 秒沒有新資料，以下數值已過期。</p>`;
      return '';
    }

    function cardHtml(key, stale) {
      const item = itemState(key);
      const ok = item.state === 'ok';
      const tag = ok ? '' : `<span class="smon-tag" data-state="${item.state}">${escapeHtml(item.label)}</span>`;
      const rows = ok ? details(key) : [];
      const note = !ok && item.message && item.message !== item.label ? `<p class="smon-note">${escapeHtml(item.message)}</p>` : '';
      return `<article class="smon-card" data-key="${key}" data-state="${item.state}" data-level="${ok ? level(item.percent) : 'none'}" data-stale="${stale}">
        <header class="smon-card-head"><h3>${escapeHtml(itemTitle(key))}</h3>${tag}${staleTag(stale)}</header>
        <p class="smon-value">${ok ? percentText(item.percent) : '—'}</p>
        ${meterHtml(ok ? item.percent : null)}
        ${rows.length ? `<dl class="smon-detail">${rows.map(([label, value]) => `<div><dt>${label}</dt><dd>${escapeHtml(value)}</dd></div>`).join('')}</dl>` : ''}
        ${note}
      </article>`;
    }

    function chartHtml(key, stale) {
      const points = cache.history;
      const last = points[points.length - 1];
      const values = points.map(point => point[key]).filter(isNumber);
      const item = itemState(key);
      const current = item.state === 'ok' ? percentText(item.percent) : item.label;
      const stats = values.length
        ? `目前 ${current}・最高 ${percentText(Math.max(...values))}・最低 ${percentText(Math.min(...values))}・${values.length} 筆`
        : `目前 ${current}・尚無走勢資料，從現在起每 ${intervalMs / 1000} 秒累積一筆`;
      // 右邊界是最新一筆的 sampledAt，往左 60 秒
      const end = last ? last.t : 0;
      const x = t => ((t - end + windowMs) / windowMs * plotWidth).toFixed(1);
      const y = value => (plotHeight - Math.min(100, Math.max(0, value)) / 100 * plotHeight).toFixed(1);
      const segments = [];
      let segment = null;
      points.forEach(point => {
        const value = point[key];
        // null 不畫；斷線或隔太久的點另起一段，不和前面連線
        if (!isNumber(value)) {
          segment = null;
          return;
        }
        if (!segment || point.broken) {
          segment = [];
          segments.push(segment);
        }
        segment.push(`${x(point.t)},${y(value)}`);
      });
      // 只有一筆的段落畫成一個點（長度 0 的線段配圓頭）
      const line = segments.map(part => `M${part.join('L')}${part.length === 1 ? 'h0' : ''}`).join('');
      const area = segments.filter(part => part.length > 1)
        .map(part => `M${part[0].split(',')[0]},${plotHeight}L${part.join('L')}L${part[part.length - 1].split(',')[0]},${plotHeight}Z`).join('');
      const dot = last && isNumber(last[key]) ? `<path class="smon-dot" d="M${x(last.t)},${y(last[key])}h0"/>` : '';
      const grid = [0, 25, 50, 75, 100].map(pct => `<line class="smon-grid" x1="0" x2="${plotWidth}" y1="${y(pct)}" y2="${y(pct)}"/>`).join('');
      const label = `${chartTitles[key]}最近 ${windowMs / 1000} 秒走勢：${stats}${stale ? '（過期）' : ''}`;
      return `<h3 class="smon-chart-title">${chartTitles[key]}<small>最近 ${windowMs / 1000} 秒</small></h3>
        <p class="smon-stats">${escapeHtml(stats)}${staleTag(stale)}</p>
        <div class="smon-plot">
          <span class="smon-axis-y" aria-hidden="true"><span style="top:0%">100%</span><span style="top:50%">50%</span><span style="top:100%">0%</span></span>
          <svg viewBox="0 0 ${plotWidth} ${plotHeight}" preserveAspectRatio="none" role="img" aria-label="${escapeHtml(label)}" data-stale="${stale}">
            ${grid}${area ? `<path class="smon-area" d="${area}"/>` : ''}${line ? `<path class="smon-line" d="${line}"/>` : ''}${dot}
          </svg>
        </div>
        <p class="smon-axis-x" aria-hidden="true"><span>${windowMs / 1000} 秒前</span><span>${windowMs / 2000} 秒前</span><span>最新 ${escapeHtml(last ? timeText(last.sampledAt) : '—')}</span></p>`;
    }

    function drawPage(write) {
      const stale = isStale();
      write('meta', metaHtml(stale));
      write('status', statusHtml(stale));
      write('cards', itemKeys.map(key => cardHtml(key, stale)).join(''));
      write('cpuChart', chartHtml('cpu', stale));
      write('memoryChart', chartHtml('memory', stale));
    }

    function summaryRow(key, stale) {
      const item = itemState(key);
      const ok = item.state === 'ok';
      const data = cache.latest && cache.latest[key];
      let extra = '';
      if (ok && key === 'memory') extra = `可用 ${gibText(data.availableBytes)}`;
      if (ok && key === 'disk') extra = `剩餘 ${gibText(data.freeBytes)}`;
      return `<span class="smon-sum-row" data-key="${key}" data-state="${item.state}" data-level="${ok ? level(item.percent) : 'none'}" data-stale="${stale}">
        <span class="smon-sum-name">${escapeHtml(itemTitle(key))}</span>
        ${meterHtml(ok ? item.percent : null)}
        <b class="smon-sum-value">${escapeHtml(ok ? percentText(item.percent) : item.label)}</b>
        ${extra ? `<small>${escapeHtml(extra)}</small>` : ''}
      </span>`;
    }

    function drawSummary(write) {
      const stale = isStale();
      write('rows', itemKeys.map(key => summaryRow(key, stale)).join(''));
      let state = '';
      if (cache.error) state = '<span class="smon-sum-state" data-state="error">資料暫時無法取得</span>';
      else if (!cache.latest) state = '<span class="smon-sum-state" data-state="loading">載入中…</span>';
      const time = cache.latest ? timeText(cache.latest.sampledAt) : '—';
      write('foot', `<span>server 主機・最後資料 ${escapeHtml(time)}</span>${staleTag(stale)}${state}
        <span class="smon-sum-more">查看完整頁 →</span>`);
    }

    // 骨架只寫一次，之後只換各區塊的內容；摘要的連結元素一直是同一個，鍵盤焦點不會被重畫弄掉
    function mount(container, skeleton, draw) {
      const previous = viewOf.get(container);
      if (previous) previous.unmount();
      container.innerHTML = skeleton;
      const written = new Map();
      const view = {
        visible: false,
        render() {
          draw((name, html) => {
            if (written.get(name) === html) return;
            const slot = container.querySelector(`[data-smon-slot="${name}"]`);
            if (!slot) return;
            slot.innerHTML = html;
            written.set(name, html);
          });
        },
      };
      const handle = {
        setVisible(visible) {
          if (!views.has(view)) return;
          view.visible = Boolean(visible);
          if (view.visible) view.render();
          sync();
        },
        unmount() {
          if (!views.delete(view)) return;
          viewOf.delete(container);
          container.innerHTML = '';
          sync();
        },
      };
      views.add(view);
      viewOf.set(container, handle);
      return handle;
    }

    return {
      mountPage(container) {
        return mount(container, pageSkeleton, drawPage);
      },
      // 三項精簡摘要；整塊是連到完整頁的連結，href 由主系統決定
      mountSummary(container, { href = '#system-monitor' } = {}) {
        const skeleton = `<a class="smon-summary" href="${escapeHtml(href)}">
          <span class="smon-sum-rows" data-smon-slot="rows"></span>
          <span class="smon-sum-foot" data-smon-slot="foot"></span>
        </a>`;
        return mount(container, skeleton, drawSummary);
      },
      // 目前快取內容的複本，給測試與除錯看
      snapshot() {
        return { latest: cache.latest, error: cache.error, stale: isStale(), history: cache.history.map(point => ({ ...point })) };
      },
    };
  }

  let shared = null;
  root.SystemMonitor = {
    create: createMonitor,
    // 主系統用這一個：完整頁和控制台摘要拿到同一個實例，才會共用快取與排程
    // 第一次呼叫的 options 決定網址；切換專案是整頁重新載入，所以一頁只會有一個網址
    shared(options) {
      if (!shared) shared = createMonitor(options);
      return shared;
    },
  };
})(globalThis);
