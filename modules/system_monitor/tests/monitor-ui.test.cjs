// 系統監控前端的互動測試：用假的 fetch、時鐘、DOM 和 document，不連真的 server
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../web/monitor.js'), 'utf8');
const gib = 1024 ** 3;
// 台北時間 2026-10-27 10:00:00
const baseMs = Date.UTC(2026, 9, 27, 2, 0, 0);

const settle = () => new Promise(resolve => setImmediate(resolve));

// 假時鐘：advance 時依到期時間執行計時器，每執行一個就等 promise 跑完
function fakeClock() {
  let now = 1_000_000;
  let nextId = 1;
  const timers = new Map();
  return {
    now: () => now,
    setTimeout: (fn, ms) => {
      const id = nextId++;
      timers.set(id, { at: now + ms, fn });
      return id;
    },
    clearTimeout: id => { timers.delete(id); },
    pending: () => timers.size,
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        const due = [...timers.entries()].filter(([, timer]) => timer.at <= end)
          .sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
        if (!due) break;
        timers.delete(due[0]);
        now = due[1].at;
        due[1].fn();
        await settle();
      }
      now = end;
      await settle();
    },
  };
}

// 假 fetch：每個請求都先掛著，由測試決定何時、怎麼回應
function fakeServer() {
  const requests = [];
  function fetch(url, init) {
    return new Promise((resolve, reject) => {
      const request = { url, init };
      request.reply = (payload, status = 200) => resolve({
        ok: status >= 200 && status < 300, status,
        text: async () => (typeof payload === 'string' ? payload : JSON.stringify(payload)),
      });
      request.fail = () => reject(new TypeError('Failed to fetch'));
      init.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
      requests.push(request);
    });
  }
  return { fetch, requests, last: () => requests[requests.length - 1] };
}

// 假節點：寫入 innerHTML 時找出 data-smon-slot，querySelector 拿得到同一個區塊
function element() {
  let html = '';
  const node = {
    writes: 0,
    slots: {},
    get innerHTML() { return html; },
    set innerHTML(value) {
      html = value;
      node.writes += 1;
      node.slots = {};
      for (const match of value.matchAll(/data-smon-slot="([\w-]+)"/g)) node.slots[match[1]] = element();
    },
    querySelector(selector) {
      const match = selector.match(/data-smon-slot="([\w-]+)"/);
      return (match && node.slots[match[1]]) || null;
    },
    // 骨架和各區塊內容串在一起，方便比對文字
    text() { return html + Object.values(node.slots).map(slot => slot.text()).join(''); },
  };
  return node;
}

function fakeDocument() {
  const listeners = [];
  const doc = {
    hidden: false,
    addEventListener(type, fn) {
      if (type === 'visibilitychange') listeners.push(fn);
    },
    setHidden(value) {
      doc.hidden = value;
      listeners.forEach(fn => fn());
    },
  };
  return doc;
}

function setup() {
  const clock = fakeClock();
  const server = fakeServer();
  const document = fakeDocument();
  const context = vm.createContext({ AbortController, console });
  vm.runInContext(source, context);
  const monitor = context.SystemMonitor.create({
    fetch: server.fetch, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, document,
  });
  return { clock, server, document, monitor };
}

function iso(seconds) {
  return new Date(baseMs + seconds * 1000 + 8 * 3600 * 1000).toISOString().replace('Z', '+08:00');
}

// cpu 給數字是有效值；給物件就照物件內容當成 cpu 項目（取樣中、不支援、失敗）
function sample(id, seconds, { cpu = 20, memory = 50 } = {}) {
  const at = iso(seconds);
  const cpuItem = typeof cpu === 'number'
    ? { status: 'ok', updatedAt: at, message: null, percent: cpu }
    : { updatedAt: at, percent: null, ...cpu };
  return {
    sampleId: id, sampledAt: at, intervalSeconds: 3,
    cpu: cpuItem,
    memory: { status: 'ok', updatedAt: at, message: null, totalBytes: 16 * gib, usedBytes: 16 * gib * memory / 100,
      availableBytes: 16 * gib * (100 - memory) / 100, percent: memory },
    disk: { status: 'ok', updatedAt: at, message: null, totalBytes: 500 * gib, usedBytes: 125 * gib, freeBytes: 375 * gib,
      percent: 25, volume: 'D:' },
  };
}

async function reply(server, payload, status) {
  server.last().reply(payload, status);
  await settle();
}

function card(container, key) {
  const match = container.slots.cards.innerHTML.match(new RegExp(`<article class="smon-card" data-key="${key}"[\\s\\S]*?</article>`));
  assert.ok(match, `找不到 ${key} 卡片`);
  return match[0];
}

function linePath(container, key) {
  const match = container.slots[`${key}Chart`].innerHTML.match(/class="smon-line" d="([^"]*)"/);
  return match ? match[1] : '';
}

test('first load shows loading, then three cards with host label and sample time', async () => {
  const { server, monitor } = setup();
  const box = element();
  const page = monitor.mountPage(box);
  // 掛上但還沒顯示，不讀資料
  assert.equal(server.requests.length, 0);
  page.setVisible(true);
  assert.equal(server.requests.length, 1);
  assert.equal(server.requests[0].url, '/api/system-monitor');
  assert.equal(server.requests[0].init.cache, 'no-store');
  assert.match(box.text(), /載入中/);
  await reply(server, sample(1, 0, { cpu: { status: 'warming-up', message: '取樣中' } }));
  const html = box.text();
  assert.doesNotMatch(html, /載入中/);
  assert.match(html, /server 主機/);
  assert.match(html, /最後資料 <b>10:00:00<\/b>/);
  assert.match(card(box, 'cpu'), /取樣中/);
  assert.match(card(box, 'memory'), /50\.0%/);
  assert.match(card(box, 'memory'), /8\.0 \/ 16\.0 GiB/);
  assert.match(card(box, 'memory'), /可用<\/dt><dd>8\.0 GiB/);
  assert.match(card(box, 'disk'), /磁碟 D:/);
  assert.match(card(box, 'disk'), /125\.0 \/ 500\.0 GiB/);
  assert.match(card(box, 'disk'), /剩餘<\/dt><dd>375\.0 GiB/);
  assert.doesNotMatch(html, /過期/);
});

test('polls every 3 seconds and a slow response never overlaps the next request', async () => {
  const { clock, server, monitor } = setup();
  monitor.mountPage(element()).setVisible(true);
  await reply(server, sample(1, 0));
  await clock.advance(2999);
  assert.equal(server.requests.length, 1);
  await clock.advance(1);
  assert.equal(server.requests.length, 2);
  // 第二次很慢：之後兩次排程都只重畫，不再發請求
  await clock.advance(6000);
  assert.equal(server.requests.length, 2);
  await reply(server, sample(2, 3));
  await clock.advance(3000);
  assert.equal(server.requests.length, 3);
});

test('leaving the page or hiding the tab stops polling; coming back reads at once without extra timers', async () => {
  const { clock, server, document, monitor } = setup();
  const page = monitor.mountPage(element());
  const summary = monitor.mountSummary(element());
  page.setVisible(true);
  await reply(server, sample(1, 0));
  page.setVisible(false);
  assert.equal(clock.pending(), 0);
  await clock.advance(30000);
  assert.equal(server.requests.length, 1);
  // 換到控制台的摘要：立刻讀一次
  summary.setVisible(true);
  assert.equal(server.requests.length, 2);
  await reply(server, sample(2, 30));
  document.setHidden(true);
  assert.equal(clock.pending(), 0);
  await clock.advance(30000);
  assert.equal(server.requests.length, 2);
  document.setHidden(false);
  assert.equal(server.requests.length, 3);
  await reply(server, sample(3, 60));
  // 反覆進出：只多一次立即讀取，請求沒回來前不再發，排程也只有一個
  for (let round = 0; round < 5; round += 1) {
    summary.setVisible(false);
    summary.setVisible(true);
    page.setVisible(true);
    page.setVisible(false);
  }
  assert.equal(server.requests.length, 4);
  await reply(server, sample(4, 63));
  assert.equal(clock.pending(), 1);
  await clock.advance(3000);
  assert.equal(server.requests.length, 5);
  await reply(server, sample(5, 66));
  await clock.advance(2999);
  assert.equal(server.requests.length, 5);
});

test('a short page switch or hidden tab still leaves a gap; a switch that misses no update does not', async () => {
  const { clock, server, document, monitor } = setup();
  const box = element();
  const page = monitor.mountPage(box);
  const summary = monitor.mountSummary(element());
  page.setVisible(true);
  await reply(server, sample(1, 0));
  await clock.advance(3000);
  await reply(server, sample(2, 3));
  // 離頁 6 秒（不到 10 秒）：錯過了讀取，回來的第一筆另起一段
  page.setVisible(false);
  await clock.advance(6000);
  page.setVisible(true);
  await reply(server, sample(3, 9));
  assert.equal(monitor.snapshot().history.at(-1).broken, true);
  assert.equal(linePath(box, 'cpu'), 'M510.0,128.0L540.0,128.0M600.0,128.0h0');
  // 頁籤隱藏 6 秒也一樣
  await clock.advance(3000);
  await reply(server, sample(4, 12));
  document.setHidden(true);
  await clock.advance(6000);
  document.setHidden(false);
  await reply(server, sample(5, 18));
  const history = monitor.snapshot().history;
  assert.equal(history.at(-2).broken, false);
  assert.equal(history.at(-1).broken, true);
  // 換到控制台摘要時還沒到下一次讀取，沒有錯過資料，照常連線
  await clock.advance(1000);
  page.setVisible(false);
  summary.setVisible(true);
  await reply(server, sample(6, 19));
  assert.equal(monitor.snapshot().history.at(-1).broken, false);
});

test('summary reuses the shared cache and keeps the same keyboard link while updating', async () => {
  const { clock, server, monitor } = setup();
  const pageBox = element();
  const summaryBox = element();
  const page = monitor.mountPage(pageBox);
  page.setVisible(true);
  await reply(server, sample(1, 0, { cpu: 12.5 }));
  const summary = monitor.mountSummary(summaryBox, { href: '#system-monitor' });
  page.setVisible(false);
  summary.setVisible(true);
  // 新的請求還沒回來，摘要已經用快取畫出三項
  assert.match(summaryBox.innerHTML, /<a class="smon-summary" href="#system-monitor">/);
  const rows = summaryBox.slots.rows.innerHTML;
  assert.match(rows, /CPU[\s\S]*12\.5%/);
  assert.match(rows, /RAM[\s\S]*50\.0%[\s\S]*可用 8\.0 GiB/);
  assert.match(rows, /磁碟 D:[\s\S]*25\.0%[\s\S]*剩餘 375\.0 GiB/);
  assert.match(summaryBox.slots.foot.innerHTML, /server 主機・最後資料 10:00:00[\s\S]*查看完整頁/);
  await reply(server, sample(2, 3, { cpu: 40 }));
  await clock.advance(3000);
  await reply(server, sample(3, 6, { cpu: 41 }));
  assert.match(summaryBox.slots.rows.innerHTML, /41\.0%/);
  // 連結所在的骨架只寫過一次，更新只換裡面的內容
  assert.equal(summaryBox.writes, 1);
});

test('trend keeps only the last 60 seconds by sampledAt and skips repeated sampleIds', async () => {
  const { clock, server, monitor } = setup();
  const box = element();
  monitor.mountPage(box).setVisible(true);
  await reply(server, sample(1, 0));
  for (let id = 2; id <= 30; id += 1) {
    await clock.advance(3000);
    await reply(server, sample(id, (id - 1) * 3));
  }
  // server 快取期間回傳同一個 sampleId，不重複加入
  await clock.advance(3000);
  await reply(server, sample(30, 87));
  let history = monitor.snapshot().history;
  assert.equal(history.length, 21);
  assert.equal(history[0].sampleId, 10);
  assert.equal(history[0].t, baseMs + 27000);
  assert.equal(history[history.length - 1].sampleId, 30);
  assert.match(box.slots.cpuChart.innerHTML, /21 筆/);
  // 以實際時間裁切：下一筆晚了 45 秒，只留下 60 秒內的，而且不和前面連線
  await clock.advance(3000);
  await reply(server, sample(31, 132));
  history = monitor.snapshot().history;
  // vm 裡建立的陣列原型不同，先轉成這邊的陣列再比
  assert.deepEqual(Array.from(history, point => point.sampleId), [25, 26, 27, 28, 29, 30, 31]);
  assert.equal(history[history.length - 1].broken, true);
  assert.equal((linePath(box, 'cpu').match(/M/g) || []).length, 2);
});

test('a real 0% is drawn as a value; warming-up, unsupported and errors are never shown as 0%', async () => {
  const { clock, server, monitor } = setup();
  const box = element();
  monitor.mountPage(box).setVisible(true);
  await reply(server, sample(1, 0, { cpu: 0 }));
  assert.match(card(box, 'cpu'), /<p class="smon-value">0\.0%<\/p>/);
  assert.match(card(box, 'cpu'), /data-state="ok"/);
  await clock.advance(3000);
  await reply(server, sample(2, 3, { cpu: { status: 'warming-up', message: '取樣中' } }));
  assert.match(card(box, 'cpu'), /<p class="smon-value">—<\/p>/);
  assert.match(card(box, 'cpu'), /data-state="warming-up">取樣中/);
  assert.doesNotMatch(card(box, 'cpu'), /0\.0%/);
  await clock.advance(3000);
  await reply(server, sample(3, 6, { cpu: 30 }));
  assert.deepEqual(Array.from(monitor.snapshot().history, point => point.cpu), [0, null, 30]);
  // 0% 在底線上；null 留缺口，前後兩點各自是一個點，不連成線
  assert.equal(linePath(box, 'cpu'), 'M540.0,160.0h0M600.0,112.0h0');
  await clock.advance(3000);
  await reply(server, sample(4, 9, { cpu: { status: 'unsupported', message: '<b>超過 64 個邏輯處理器</b>' } }));
  assert.match(card(box, 'cpu'), /data-state="unsupported">不支援/);
  assert.match(card(box, 'cpu'), /&lt;b&gt;超過 64 個邏輯處理器/);
  assert.doesNotMatch(card(box, 'cpu'), /<b>超過/);
  await clock.advance(3000);
  await reply(server, sample(5, 12, { cpu: { status: 'error', message: 'CPU 資料暫時無法取得：拒絕存取' } }));
  assert.match(card(box, 'cpu'), /data-state="error">讀取失敗/);
  assert.match(card(box, 'cpu'), /拒絕存取/);
  assert.equal(monitor.snapshot().history.at(-1).cpu, null);
  // 其他項目照常更新
  assert.match(card(box, 'memory'), /50\.0%/);
});

test('network failure keeps old values marked stale; recovery shows new data and leaves a gap', async () => {
  const { clock, server, monitor } = setup();
  const box = element();
  monitor.mountPage(box).setVisible(true);
  await reply(server, sample(1, 0, { cpu: 10 }));
  await clock.advance(3000);
  await reply(server, sample(2, 3, { cpu: 20 }));
  await clock.advance(3000);
  server.last().fail();
  await settle();
  let html = box.text();
  assert.match(html, /資料暫時無法取得（連不到 server）/);
  assert.match(card(box, 'cpu'), /20\.0%/);
  assert.match(card(box, 'cpu'), /data-stale="true"/);
  assert.match(card(box, 'cpu'), /過期/);
  await clock.advance(3000);
  await reply(server, { ok: false, message: 'server 錯誤：採集器壞了' }, 500);
  assert.match(box.text(), /server 回應 HTTP 500：server 錯誤：採集器壞了/);
  await clock.advance(3000);
  await reply(server, sample(3, 12, { cpu: 30 }));
  html = box.text();
  assert.doesNotMatch(html, /資料暫時無法取得/);
  assert.doesNotMatch(html, /過期/);
  assert.match(card(box, 'cpu'), /30\.0%/);
  // 恢復後的點另起一段，不和斷線前連在一起
  assert.equal(linePath(box, 'cpu'), 'M480.0,144.0L510.0,128.0M600.0,112.0h0');
});

test('first load failure says data is unavailable instead of showing zeros, then recovers', async () => {
  const { clock, server, monitor } = setup();
  const box = element();
  const summaryBox = element();
  monitor.mountPage(box).setVisible(true);
  monitor.mountSummary(summaryBox).setVisible(true);
  server.last().fail();
  await settle();
  assert.match(box.text(), /資料暫時無法取得（連不到 server）；恢復後會自動更新/);
  assert.match(summaryBox.text(), /資料暫時無法取得/);
  assert.doesNotMatch(box.text() + summaryBox.text(), /\d\.\d%/);
  await clock.advance(3000);
  await reply(server, sample(1, 3));
  assert.doesNotMatch(box.text() + summaryBox.text(), /資料暫時無法取得/);
  assert.match(summaryBox.slots.rows.innerHTML, /20\.0%/);
});

test('data older than 10 seconds is stale while a request hangs, and the hung request is aborted', async () => {
  const { clock, server, monitor } = setup();
  const box = element();
  monitor.mountPage(box).setVisible(true);
  await reply(server, sample(1, 0));
  await clock.advance(3000);
  assert.equal(server.requests.length, 2);
  await clock.advance(6000);
  assert.doesNotMatch(box.text(), /過期/);
  // 距最後一次成功 12 秒：請求還掛著，排程照樣把數值標成過期
  await clock.advance(3000);
  assert.match(box.text(), /已超過 10 秒沒有新資料/);
  assert.match(card(box, 'cpu'), /data-stale="true"/);
  assert.equal(server.requests.length, 2);
  // 請求等了 10 秒被中斷，下一次排程重新讀
  await clock.advance(1000);
  assert.match(box.text(), /server 超過 10 秒沒有回應/);
  await clock.advance(2000);
  assert.equal(server.requests.length, 3);
  await reply(server, sample(2, 15));
  assert.doesNotMatch(box.text(), /過期/);
});

test('late responses do not write into an unmounted view, and remounting replaces the old view', async () => {
  const { clock, server, monitor } = setup();
  const box = element();
  const page = monitor.mountPage(box);
  page.setVisible(true);
  page.unmount();
  assert.equal(box.innerHTML, '');
  const writes = box.writes;
  await reply(server, sample(1, 0));
  assert.equal(box.writes, writes);
  assert.equal(box.text(), '');
  assert.equal(clock.pending(), 0);
  // 同一個節點掛第二次，舊的控制代碼不再有作用
  const first = monitor.mountPage(box);
  const second = monitor.mountPage(box);
  first.setVisible(true);
  assert.equal(server.requests.length, 1);
  second.setVisible(true);
  assert.equal(server.requests.length, 2);
  first.unmount();
  assert.notEqual(box.innerHTML, '');
});
