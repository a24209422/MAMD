"""用 Edge（或 Chrome）無頭模式開真的儀表板 server，在掛好的「系統監控」頁看真實主機資料。找不到瀏覽器就跳過。

儀表板的導覽、控制台摘要和窄畫面在 tests/test_web.py 用固定替身資料測；這裡讀真的 CPU、RAM、磁碟，
確認資料經過 server 一路畫到頁面、每 3 秒持續更新，讀不到資料時不顯示假的 0、恢復後接著更新。
"""
import contextlib
import itertools
import json
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path

import pytest

import server
from modules.system_monitor import api, metrics

repoDir = Path(__file__).resolve().parents[3]
browserCandidates = [
  r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
  r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]
gib = 1024 ** 3
# 接在儀表板 main.js 後面：在兩次輪詢之間記下監控頁的畫面和模組快取，最後寫進 body 的 data-probe，dump-dom 才看得到
probeScript = """
{
  const page = document.querySelector('#systemMonitorPage');
  const text = node => node ? node.textContent.replace(/\\s+/g, ' ').trim() : '';
  const records = [];
  const record = () => {
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


def findBrowser():
  for name in ("msedge", "chrome", "chromium"):
    found = shutil.which(name)
    if found:
      return found
  return next((path for path in browserCandidates if Path(path).exists()), None)


browser = findBrowser()
pytestmark = pytest.mark.skipif(browser is None, reason="找不到 Edge 或 Chrome，跳過系統監控的瀏覽器測試")


def realCollector(projectDir):
  # CPU、RAM、磁碟都讀真的；只有時間是替身：瀏覽器用虛擬時間快轉，3 秒一次的輪詢實際只隔幾毫秒，
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


def measure(tmp_path, monkeypatch, url, times):
  # 只換掉測試 server 的 main.js；監控頁從第 0 秒起每 3 秒讀一次，times 排在兩次讀取之間
  main = tmp_path / "main.js"
  main.write_text((repoDir / "web/js/main.js").read_text(encoding="utf-8") + probeScript.replace("TIMES", json.dumps(times)),
                  encoding="utf-8")
  monkeypatch.setitem(server.staticFiles, "/js/main.js", (str(main), "text/javascript; charset=utf-8"))
  completed = subprocess.run(
    [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
     f"--user-data-dir={tmp_path / 'browser'}", f"--virtual-time-budget={times[-1] + 1000}", "--dump-dom",
     url + "#system-monitor"],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
  match = re.search(r'data-probe="([^"]*)"', completed.stdout)
  assert match, "頁面裡的量測沒有跑完"
  return json.loads(unescape(match.group(1)))


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
def test_mountedPageShowsRealHostSamplesAndKeepsUpdating(projectDir, tmp_path, monkeypatch):
  with serving(projectDir) as url:
    records = measure(tmp_path, monkeypatch, url, [1500, 4500, 7500])
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


def test_outageShowsNoFakeZeroAndRecovers(projectDir, tmp_path, monkeypatch):
  # 第 1 次讀取就失敗、第 2 次恢復、第 3、4 次又失敗、第 5 次起恢復
  with serving(projectDir, failing={1, 3, 4}) as url:
    first, loaded, offline, recovered, later = measure(tmp_path, monkeypatch, url, [1500, 4500, 10500, 13500, 16500])
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
