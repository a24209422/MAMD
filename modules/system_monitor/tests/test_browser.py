"""用共用的 Edge（或 Chrome）開真的儀表板 server，在掛好的「系統監控」頁看真實主機資料。找不到瀏覽器就跳過。

儀表板的導覽、控制台摘要和窄畫面在 tests/test_web.py 用固定替身資料測；這裡讀真的 CPU、RAM、磁碟，
確認資料經過 server 一路畫到頁面、每 3 秒持續更新，讀不到資料時不顯示假的 0、恢復後接著更新。
"""
import contextlib
import itertools
import json
import re
import shutil
import sys
import threading
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path

import pytest

import server
from modules.system_monitor import api, metrics

repoDir = Path(__file__).resolve().parents[3]
# 共用瀏覽器的工具在 tests/browser.py；和 tests/ 的測試 import 同一份，整段測試才只啟動一次瀏覽器
if str(repoDir / "tests") not in sys.path:
  sys.path.append(str(repoDir / "tests"))
import browser  # noqa: E402
from browser import browserPages, sharedBrowser  # noqa: E402,F401  測試模組裡要看得到 fixture

gib = 1024 ** 3
pollMs = metrics.intervalSeconds * 1000
# 接在儀表板 main.js 前面：監控頁每讀完一次、畫面也更新完，probeLoads 加一，測試等到它才往下快轉；
# 在兩次輪詢之間記下監控頁的畫面和模組快取，最後寫進 body 的 data-probe
probeScript = """
{
  const text = node => node ? node.textContent.replace(/\\s+/g, ' ').trim() : '';
  window.probeLoads = 0;
  const realFetch = window.fetch;
  window.fetch = (...args) => {
    const pending = realFetch.apply(window, args);
    if (args[0] !== API_PATH) return pending;
    return pending.then(response => {
      const read = response.text.bind(response);
      // 監控模組讀完內容後才接著處理、重畫；MessageChannel 的訊息排在這些都跑完之後，也不受 page.clock 控制
      response.text = () => read().finally(() => {
        const channel = new MessageChannel();
        channel.port1.onmessage = () => { window.probeLoads += 1; };
        channel.port2.postMessage(null);
      });
      return response;
    });
  };
  const records = [];
  const record = () => {
    const page = document.querySelector('#systemMonitorPage');
    const cache = SystemMonitor.shared().snapshot();
    records.push({
      error: cache.error, stale: cache.stale, sampledAt: cache.latest && cache.latest.sampledAt,
      history: cache.history.map(point => ({ sampleId: point.sampleId, broken: point.broken, cpu: point.cpu, memory: point.memory })),
      banner: text(page.querySelector('.smon-status')),
      meta: text(page.querySelector('.smon-meta')),
      cards: Object.fromEntries([...page.querySelectorAll('.smon-card')].map(card => [card.dataset.key, {
        title: text(card.querySelector('h3')), state: card.dataset.state, stale: card.dataset.stale,
        value: text(card.querySelector('.smon-value')), tags: [...card.querySelectorAll('.smon-tag')].map(text),
        details: Object.fromEntries([...card.querySelectorAll('.smon-detail div')].map(row => [text(row.querySelector('dt')), text(row.querySelector('dd'))])),
      }])),
      lines: Object.fromEntries([...page.querySelectorAll('.smon-chart')].map(chart => {
        const line = chart.querySelector('.smon-line');
        return [chart.dataset.key, line ? line.getAttribute('d') : ''];
      })),
    });
  };
  const times = TIMES;
  times.forEach(ms => setTimeout(record, ms));
  setTimeout(() => { document.body.dataset.probe = JSON.stringify(records); }, times[times.length - 1] + 100);
}
"""


def realCollector(projectDir):
  # CPU、RAM、磁碟都讀真的；只有時間是替身：瀏覽器用 page.clock 快轉，3 秒一次的輪詢實際只隔幾毫秒，
  # 這裡讓每次讀取都當成隔了 3 秒，每次都是一筆新的真實樣本，取樣時間也依序往後 3 秒
  ticks, stamps = itertools.count(), itertools.count()
  start = datetime.now().astimezone()
  return metrics.Collector(projectDir, clock=lambda: metrics.intervalSeconds * next(ticks),
                           now=lambda: start + timedelta(seconds=metrics.intervalSeconds * next(stamps)))


@contextlib.contextmanager
def serving(projectDir, failing=()):
  """啟動真的儀表板 server；failing 是第幾次（從 1 起算）讀監控資料時 server 回 503，模擬 server 暫時無法提供資料。"""
  httpServer = server.makeServer(projectDir, 0, "browser-token")
  httpServer.RequestHandlerClass.dashboard.systemMonitor.collector = realCollector(projectDir)
  reads = itertools.count(1)

  class FailingHandler(httpServer.RequestHandlerClass):
    def routeGet(self, path):
      # 不用直接斷線模擬：瀏覽器遇到沒有回應的連線可能自己重送，第幾次讀取就對不上
      if path == api.apiPath and next(reads) in failing:
        self.sendJson(503, {"ok": False, "message": "server 暫停服務"})
        return
      super().routeGet(path)

  httpServer.RequestHandlerClass = FailingHandler
  thread = threading.Thread(target=httpServer.serve_forever, daemon=True)
  thread.start()
  try:
    yield f"http://127.0.0.1:{httpServer.server_address[1]}/"
  finally:
    httpServer.shutdown()
    httpServer.server_close()


def measure(browserPages, tmp_path, monkeypatch, url, times):
  # 只換掉測試 server 的 main.js；監控頁從第 0 秒起每 3 秒讀一次，times 排在兩次讀取之間
  probe = probeScript.replace("TIMES", json.dumps(times)).replace("API_PATH", json.dumps(api.apiPath))
  main = tmp_path / "main.js"
  main.write_text(probe + (repoDir / "web/js/main.js").read_text(encoding="utf-8"), encoding="utf-8")
  monkeypatch.setitem(server.staticFiles, "/js/main.js", (str(main), "text/javascript; charset=utf-8"))
  context = browserPages.newContext()
  try:
    page = context.new_page()
    problems = []
    page.on("console", lambda message: message.type == "error" and problems.append(message.text))
    page.on("pageerror", lambda error: problems.append(f"未處理的例外：{error}"))
    page.on("requestfailed", lambda request: problems.append(f"{request.method} {request.url} {request.failure}"))
    # 時間停在頁面載入那一刻，之後只照測試快轉：頁面載入再慢，輪詢也不會自己先跑
    start = datetime.now()
    page.clock.install(time=start)
    page.clock.pause_at(start + timedelta(seconds=10))
    page.goto(url + "#system-monitor", wait_until="commit", timeout=browser.readySeconds * 1000)
    fastForward(page, times + [times[-1] + 100], problems)
    waitFor(page, "document.body.dataset.probe !== undefined", "量測結果寫進 data-probe", problems)
    dom = page.content()
  finally:
    browserPages.closeContext(context)
  match = re.search(r'data-probe="([^"]*)"', dom)
  assert match, "頁面裡的量測沒有跑完"
  return json.loads(unescape(match.group(1)))


def fastForward(page, targets, problems):
  # 每到一次輪詢就等這次讀完、畫面更新完再往下快轉，請求還在路上時不會先跳到下一次輪詢或記錄
  now, loads = 0, 1
  waitFor(page, "window.probeLoads >= 1", "第 1 次讀取", problems)
  for target in targets:
    while loads * pollMs <= target:
      page.clock.run_for(loads * pollMs - now)
      now, loads = loads * pollMs, loads + 1
      waitFor(page, f"window.probeLoads >= {loads}", f"第 {loads} 次讀取", problems)
    page.clock.run_for(target - now)
    now = target


def waitFor(page, condition, step, problems):
  try:
    page.wait_for_function(condition, timeout=browser.readySeconds * 1000)
  except browser.PlaywrightError as error:
    lines = [f"頁面裡的量測沒有跑完：等不到{step}（{str(error).strip().splitlines()[0]}）"]
    raise AssertionError("\n".join(lines + browser.listing("瀏覽器 console 的錯誤與失敗的請求", problems, "（沒有）"))) from None


def gibValues(text):
  return [float(number) for number in re.findall(r"\d+\.\d", text)]


def historyPoints(record):
  # 走勢裡每一點的 (sampleId, 是否和前一點斷開)
  return [(point["sampleId"], point["broken"]) for point in record["history"]]


@pytest.fixture
def projectDir(tmp_path):
  folder = tmp_path / "proj"
  folder.mkdir()
  return folder


@pytest.mark.skipif(not metrics.isWindows, reason="真實的 CPU、RAM 採集只支援 Windows")
def test_mountedPageShowsRealHostSamplesAndKeepsUpdating(browserPages, projectDir, tmp_path, monkeypatch):
  with serving(projectDir) as url:
    records = measure(browserPages, tmp_path, monkeypatch, url, [1500, 4500, 7500])
  # 每 3 秒一筆新樣本接到走勢後面，最後資料時間跟著換成最新一筆
  assert [historyPoints(record) for record in records] == [[(1, True)], [(1, True), (2, False)], [(1, True), (2, False), (3, False)]]
  for record in records:
    assert record["error"] is None and record["stale"] is False and record["banner"] == ""
    assert "監控對象：server 主機" in record["meta"] and f"最後資料 {record['sampledAt'][11:19]}" in record["meta"]
    assert all(card["stale"] == "false" for card in record["cards"].values())
  last = records[-1]
  cpu, memory, disk = (last["cards"][key] for key in ("cpu", "memory", "disk"))
  # CPU 第一筆只建立基準；之後是 0～100 的有效值，或系統計數還沒前進時仍標「取樣中」（超過 64 個邏輯處理器時標「不支援」），
  # 沒有有效值時不會顯示假的 0
  if cpu["state"] == "ok":
    assert 0 <= float(cpu["value"].rstrip("%")) <= 100 and last["history"][-1]["cpu"] == float(cpu["value"].rstrip("%"))
  else:
    assert cpu["state"] in ("warming-up", "unsupported") and cpu["value"] == "—" and last["history"][-1]["cpu"] is None

  # RAM：已用＋可用＝總量（三個數各自四捨五入到 0.1 GiB），總量就是這台主機的實體記憶體
  ramTotal = metrics.windowsMemory()[0] / gib
  used, total = gibValues(memory["details"]["已用"])
  available, = gibValues(memory["details"]["可用"])
  assert memory["state"] == "ok" and memory["title"] == "RAM"
  assert abs(total - ramTotal) <= 0.051 and abs(used + available - total) <= 0.151
  assert memory["value"] == f"{last['history'][-1]['memory']:.1f}%"
  # 磁碟：傳給 server 的專案資料夾所在磁碟區
  usage = shutil.disk_usage(projectDir)
  used, total = gibValues(disk["details"]["已用"])
  free, = gibValues(disk["details"]["剩餘"])
  assert disk["state"] == "ok" and disk["title"] == f"磁碟 {metrics.volumeLabel(projectDir.resolve())}"
  assert abs(total - usage.total / gib) <= 0.051 and abs(free - usage.free / gib) <= 0.5
  # RAM 三筆都有值，走勢連成一條線
  assert last["lines"]["memory"].startswith("M") and last["lines"]["memory"].count("L") == 2


def test_outageShowsNoFakeZeroAndRecovers(browserPages, projectDir, tmp_path, monkeypatch):
  # 第 1 次讀取就失敗、第 2 次恢復、第 3、4 次又失敗、第 5 次起恢復
  with serving(projectDir, failing={1, 3, 4}) as url:
    first, loaded, offline, recovered, later = measure(browserPages, tmp_path, monkeypatch, url,
                                                       [1500, 4500, 10500, 13500, 16500])
  failure = "server 回應 HTTP 503：server 暫停服務"

  # 還沒拿到任何資料：每一項都是「無法取得」和「—」，不是 0%
  assert first["error"] == failure and first["history"] == []
  assert f"資料暫時無法取得（{failure}）" in first["banner"] and "最後資料 —" in first["meta"]
  for card in first["cards"].values():
    assert (card["state"], card["value"], card["tags"], card["details"]) == ("error", "—", ["無法取得"], {})
  assert first["lines"] == {"cpu": "", "memory": ""}

  # 恢復後不用重新整理就補上資料
  assert loaded["error"] is None and loaded["banner"] == "" and historyPoints(loaded) == [(1, True)]
  assert loaded["cards"]["disk"]["state"] == "ok" and loaded["cards"]["disk"]["value"].endswith("%")

  # 有資料後又讀不到：保留最後一次的數值並標成過期，不清空、不換成 0
  assert offline["error"] == failure and offline["stale"] is True
  assert f"資料暫時無法取得（{failure}），以下保留最後一次取得的數值並標為過期" in offline["banner"]
  assert "過期" in offline["meta"] and offline["history"] == loaded["history"]
  for key, card in offline["cards"].items():
    before = loaded["cards"][key]
    assert (card["state"], card["value"], card["details"]) == (before["state"], before["value"], before["details"])
    assert card["stale"] == "true" and "過期" in card["tags"]

  # 再恢復：自動接著更新，過期標記拿掉；中斷前後的點分成兩段，不連成同一條線
  for record in (recovered, later):
    assert record["error"] is None and record["stale"] is False and record["banner"] == ""
    assert "過期" not in record["meta"]
    assert all(card["stale"] == "false" and "過期" not in card["tags"] for card in record["cards"].values())
  assert historyPoints(recovered) == [(1, True), (2, True)]
  assert historyPoints(later) == [(1, True), (2, True), (3, False)]
  assert recovered["sampledAt"] > loaded["sampledAt"] and later["sampledAt"] > recovered["sampledAt"]
