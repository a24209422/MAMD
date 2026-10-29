"""用 Playwright 控制共用的 Edge（或 Chrome）載入儀表板，等畫面畫出 API 的資料再檢查。找不到 Playwright 或瀏覽器就跳過。"""
import itertools
import json
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import warnings
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from types import SimpleNamespace

import pytest

import board
import browser
import server
from board_core import appendEvent
from browser import browserPages, sharedBrowser  # noqa: F401  測試模組裡要看得到 fixture
from modules.system_monitor import api as monitorApi
from modules.system_monitor import metrics

toolDir = Path(__file__).resolve().parent.parent
token = "web-token"
uploadRows = ["| R1 一次上傳多張 | 單次最多 50 張 | D1 新增批次 API | 逐張呼叫：太慢 | T1 後端批次 API | | 選 50 張都上傳成功 |",
              "| R1 | | D1 | | T2 前端提示上限 | T1 | 選 51 張出現提示 |"]
thumbRows = ["| R1 上傳不要變慢 | 不等縮圖就回應 | D1 背景產生縮圖 | 同步產生：太慢 | T1 縮圖背景工作 | | 上傳 50 張回應不變慢 |",
             "| | | D2 改存 WebP | | T2 縮圖改 WebP | | 副檔名是 .webp |"]
# 150000 / 200000 = 75%，超過 70% 的門檻
claudeLog = [{"type": "assistant", "message": {
  "content": [{"type": "tool_use", "id": "tu1", "name": "Write", "input": {"file_path": "D:\\w\\upload.py", "content": "x"}}],
  "usage": {"input_tokens": 2, "cache_read_input_tokens": 149000, "cache_creation_input_tokens": 998, "output_tokens": 10}}}]
gib = 1024 ** 3
# 每個測試 server 的網址 → 它收到的請求；和共用瀏覽器同一份，renderUntil 失敗時列出來
serverRequests = browser.serverRequests
fakeDashboard = "http://127.0.0.1:1/"
# 資料讀回來、畫面畫好了：一般頁看 app.state，系統監控頁看監控卡片有數值（資料回來前卡片先畫成空的）
appDrawn = "typeof app !== 'undefined' && app.state !== null"
monitorDrawn = "document.querySelector('#systemMonitorPage .smon-card[data-state=\"ok\"]') !== null"
# 量版面前網頁字型要換好，不然寬度是備用字型的；先讀 offsetWidth 排版，用到的字型才會開始載入
fontsLoaded = "document.body.offsetWidth > 0 && document.fonts.status === 'loaded'"
timedOutError = "Page.wait_for_function: Timeout 15000ms exceeded."
nodePath = shutil.which("node")


def fixedCollector(projectDir):
  # 替身主機數據，不讀真實主機：每次請求都是新的一筆、時間往後 3 秒；CPU 60%（第一筆取樣中）、RAM 75%、磁碟 25%
  clockTicks = itertools.count(0, metrics.intervalSeconds)
  cpuTicks = itertools.count()
  stamps = itertools.count()
  start = datetime(2026, 10, 27, 10, 0, tzinfo=timezone(timedelta(hours=8)))
  def cpuTimes():
    tick = next(cpuTicks)
    return 4 * tick, 6 * tick, 4 * tick
  return metrics.Collector(projectDir, cpuTimes, lambda: (16 * gib, 4 * gib), lambda path: (500 * gib, 125 * gib, 375 * gib),
                           clock=lambda: next(clockTicks),
                           now=lambda: start + timedelta(seconds=metrics.intervalSeconds * next(stamps)))


@pytest.fixture
def dashboard(gitProject, writePlan):
  boardDir = gitProject / ".board"
  boardDir.mkdir(exist_ok=True)
  (boardDir / "config.json").write_text(
    json.dumps({"maxParallel": 2, "writer": {"cli": "claude", "model": "sonnet"}}), encoding="utf-8")
  writePlan("0001-upload.md", uploadRows, planId="P0001", title="批次上傳",
            extraMeta="area: [前端/上傳, 後端/上傳]\n", projectDir=gitProject)
  writePlan("0002-thumb.md", thumbRows, planId="P0002", title="背景縮圖", extraMeta="area: [後端/縮圖]\n",
            projectDir=gitProject)
  assert board.cmdSubmit(gitProject, "P0001") == 0
  assert board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: "y") == 0
  assert board.cmdSubmit(gitProject, "P0002") == 0
  appendEvent(gitProject, "taskStarted", plan="P0001", task="T1", agent="writer", roundNumber=1, data={"mode": "write"})
  logsDir = boardDir / "logs"
  logsDir.mkdir()
  (logsDir / "P0001-T1-r1-write-20261026-100000.log").write_text(
    "".join(json.dumps(record) + "\n" for record in claudeLog), encoding="utf-8")
  appendEvent(gitProject, "needsYou", plan="P0001", task="T2", roundNumber=1,
              data={"kind": "blocked", "reason": "寫程式的 agent 回報卡住：缺少 API 位址"})
  httpServer = server.makeServer(gitProject, 0, token)
  httpServer.RequestHandlerClass.dashboard.systemMonitor.collector = fixedCollector(gitProject)
  url = f"http://127.0.0.1:{httpServer.server_address[1]}/"
  serverRequests[url] = recordRequests(httpServer)
  thread = threading.Thread(target=httpServer.serve_forever, daemon=True)
  thread.start()
  yield url
  httpServer.shutdown()
  httpServer.server_close()
  serverRequests.pop(url)


def recordRequests(httpServer):
  # 畫面重畫幾次都沒有資料時，要分得出是瀏覽器沒來要、server 回錯，還是 server 丟出例外
  requests = []
  class RecordingHandler(httpServer.RequestHandlerClass):
    raised = None
    def dispatch(self, route):
      self.raised = None
      def recordedRoute(path):
        try:
          route(path)
        except Exception as error:
          self.raised = f"{type(error).__name__}: {error}"
          raise
      super().dispatch(recordedRoute)
    def log_request(self, code="-", size="-"):
      requests.append({"method": self.command, "path": getattr(self, "path", ""), "status": int(code), "error": self.raised})
  httpServer.RequestHandlerClass = RecordingHandler
  return requests


def domHas(text):
  # ready：畫面裡已經有這段 HTML，和斷言比對的是同一份序列化結果
  return f"document.documentElement.outerHTML.includes({json.dumps(text)})"


def probeHas(*keys):
  # ready：量測在計時器到了就寫出，畫面還沒有資料時也會寫；要等第一個斷言要看的值有資料才算畫好，值對不對仍交給斷言
  path = "".join(f"?.[{json.dumps(key)}]" for key in keys)
  return ("() => { const text = document.body?.dataset.probe; if (text === undefined) return false;"
          f" const value = JSON.parse(text){path}; return Array.isArray(value) ? value.length > 0 : Boolean(value); }}")


def fastForward(monkeypatch, ms, drawn=appDrawn, step=None, screenshot=None, then=()):
  # 量測的計時器不用真的等：分頁的時間先停住，計時器不會在資料回來前先跑；等資料畫好（drawn 成立）才快轉 ms 毫秒
  # then：之後每一段（條件, 毫秒），條件成立才再快轉；全部快轉完才恢復照真實時間走
  # step：每次只快轉這麼多，中間讓頁面處理 hashchange 這類事件；screenshot：取 DOM 前先截圖
  stages = [(drawn, ms), *then]
  realLoadPage = browser.loadPage
  def loadPage(context, url, ready, viewport):
    return realLoadPage(ClockContext(context, stages, step, screenshot), url, ready, viewport)
  monkeypatch.setattr(browser, "loadPage", loadPage)


class ClockContext:
  # 只換掉 new_page，其他照原本的 context
  def __init__(self, context, *clockArgs):
    self.context, self.clockArgs = context, clockArgs

  def new_page(self):
    page = self.context.new_page()
    # 載入前就停住時間；之後只有快轉時計時器才會跑
    now = datetime.now()
    page.clock.install(time=now)
    page.clock.pause_at(now + timedelta(seconds=1))
    return ClockPage(page, *self.clockArgs)


class ClockPage:
  # 等 ready 前先一段一段快轉計時器；其他操作照原本的 page
  def __init__(self, page, stages, step, screenshot):
    self.page, self.stages, self.step, self.screenshot = page, stages, step, screenshot

  def __getattr__(self, name):
    return getattr(self.page, name)

  def wait_for_function(self, ready, timeout):
    started = time.monotonic()
    remaining = lambda: max(timeout - (time.monotonic() - started) * 1000, 1000)
    for condition, ms in self.stages:
      self.page.wait_for_function(condition, timeout=remaining())
      step = self.step or ms
      for _ in range(0, ms, step):
        self.page.clock.run_for(step)
    self.page.clock.resume()
    self.page.wait_for_function(ready, timeout=remaining())

  def content(self):
    if self.screenshot:
      self.page.screenshot(path=str(self.screenshot))
    return self.page.content()


def addProbe(tmp_path, monkeypatch, script):
  # 只在測試伺服器的 main.js 後面接一段操作與量測；結果寫進 body 的 data-probe，取 DOM 時才看得到
  # 包在區塊裡，量測用的變數不會和儀表板的全域名稱撞到
  main = tmp_path / "main.js"
  main.write_text((toolDir / "web/js/main.js").read_text(encoding="utf-8") + "\n{\n" + script + "\n}\n", encoding="utf-8")
  monkeypatch.setitem(server.staticFiles, "/js/main.js", (str(main), "text/javascript; charset=utf-8"))


def probeMatch(dom):
  return re.search(r'data-probe="([^"]*)"', dom)


def readProbe(dom):
  match = probeMatch(dom)
  assert match, "頁面裡的量測沒有跑完"
  return json.loads(unescape(match.group(1)))


def sectionTag(dom, page):
  match = re.search(rf'<section[^>]*data-page="{page}"[^>]*>', dom)
  assert match, page
  return match.group(0)


def railLink(dom, page):
  match = re.search(rf'<a class="rail-btn"[^>]*data-page="{page}"[^>]*>', dom)
  return match.group(0) if match else None


@pytest.mark.skipif(nodePath is None, reason="找不到 node，跳過 JavaScript 語法檢查")
def test_scriptsHaveValidSyntax():
  # 儀表板自己的程式，加上各模組掛進儀表板的畫面程式
  scripts = sorted((toolDir / "web" / "js").glob("*.js")) + sorted((toolDir / "modules").glob("*/web/*.js"))
  assert toolDir / "web/js/main.js" in scripts and toolDir / "modules/system_monitor/web/monitor.js" in scripts
  for script in scripts:
    completed = subprocess.run([nodePath, "--check", str(script)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    assert completed.returncode == 0, f"{script.name}：{completed.stderr}"


def test_pageScriptsAreServedAndExist():
  html = (toolDir / "web" / "index.html").read_text(encoding="utf-8")
  names = re.findall(r'<script src="js/([a-z]+)\.js"></script>', html)
  assert names[0] == "core" and names[-1] == "main"
  assert names == [name for name in server.scriptNames if name in names]
  for name in names:
    assert (toolDir / "web" / "js" / f"{name}.js").is_file()


def test_dashboardLoadsMonitorModuleFromWhitelist():
  html = (toolDir / "web" / "index.html").read_text(encoding="utf-8")
  # 主頁只引用模組的樣式和程式，走 server 的模組白名單，不另存一份
  assets = re.findall(r'(?:href|src)="(modules/[^"]+)"', html)
  assert assets == ["modules/system-monitor/monitor.css", "modules/system-monitor/monitor.js"]
  for asset in assets:
    assert "/" + asset in monitorApi.assets
  # 換頁時才掛載，模組程式要比 main.js 先載入
  assert html.index('src="modules/system-monitor/monitor.js"') < html.index('src="js/main.js"')
  assert 'id="systemMonitorPage"' in html and 'id="systemMonitorSummary"' in html
  assert "「系統資源監控」還沒有接資料" not in html


def servedRequest(path, status, error=None):
  return {"method": "GET", "path": path, "status": status, "error": error}


def brokenState(projectDir):
  raise RuntimeError("替身：讀狀態失敗")


def test_dashboardRecordsRequestsAndRaisedErrors(dashboard, monkeypatch):
  # renderUntil 失敗時列出的請求來自這份紀錄，要分得出 server 回錯還是丟出例外
  monkeypatch.setattr(server, "getRoutes", [(server.getRoutes[0][0], brokenState), *server.getRoutes[1:]])
  urllib.request.urlopen(dashboard).close()
  with pytest.raises(urllib.error.HTTPError):
    urllib.request.urlopen(dashboard + "api/state")
  assert serverRequests[dashboard] == [
    servedRequest("/", 200), servedRequest("/api/state", 500, "RuntimeError: 替身：讀狀態失敗")]


def loadResult(dom="", ready=False, error=None, consoleErrors=(), failedRequests=(), seconds=0.04):
  attempt = browser.Attempt()
  attempt.dom, attempt.ready, attempt.error, attempt.seconds = dom, ready, error, seconds
  attempt.consoleErrors, attempt.failedRequests = list(consoleErrors), list(failedRequests)
  return attempt


def timedOutLoad():
  # 負載高時等到逾時：畫面還是空的，只有逾時的錯誤
  return loadResult(error=timedOutError), []


def fakeBrowserPages(monkeypatch, loads):
  # 替身瀏覽器：每次載入依序回傳結果和這次 server 收到的請求，不開真的 Edge，負載高時也不會自己偶發失敗
  requests = []
  monkeypatch.setitem(serverRequests, fakeDashboard, requests)
  remaining = iter(loads)
  def loadPage(context, url, ready, viewport):
    attempt, received = next(remaining)
    requests.extend(received)
    return attempt
  monkeypatch.setattr(browser, "loadPage", loadPage)
  return browser.BrowserPages(SimpleNamespace(new_context=lambda: SimpleNamespace(close=lambda: None)))


def test_renderUntilRedrawsWhenStateIsNotReadyYet(monkeypatch):
  # 替身：前兩次載入頁面時 /api/state 都回 503，畫面只有骨架；第 3 次載入才有資料
  skeleton = (loadResult('<main id="overview"></main>', error=timedOutError),
              [servedRequest("/", 200), servedRequest("/api/state", 503)])
  loaded = (loadResult('<main id="overview"><button>開始寫程式</button></main>', ready=True),
            [servedRequest("/", 200), servedRequest("/api/state", 200)])
  pages = fakeBrowserPages(monkeypatch, [skeleton, skeleton, loaded])
  with pytest.warns(UserWarning, match=r"test_renderUntilRedrawsWhenStateIsNotReadyYet .*重畫 3 次") as caught:
    dom = pages.renderUntil(fakeDashboard + "#token=" + token, domHas("開始寫程式"))
  assert dom == loaded[0].dom
  # 驗證時要在 pytest 結尾的 warnings summary 看到重畫次數，檢查完原樣再發出
  for item in caught:
    warnings.warn_explicit(item.message, item.category, item.filename, item.lineno)


def test_renderUntilRedrawsDashboardAfterStateUnavailable(dashboard, browserPages, monkeypatch):
  # 整合驗證：真的 server 在前兩次載入頁面時 /api/state 都回 503，頁面自己每 2 秒重讀也拿不到，第 3 次載入才有資料
  realRouteGet = server.DashboardHandler.routeGet
  pageLoads = []
  def flakyRouteGet(handler, path):
    if path == "/":
      pageLoads.append(path)
    if path == "/api/state" and len(pageLoads) <= 2:
      handler.sendJson(503, {"ok": False, "message": "替身：暫時無法回應"})
      return
    realRouteGet(handler, path)
  monkeypatch.setattr(server.DashboardHandler, "routeGet", flakyRouteGet)
  # 前兩次一定等不到，等到頁面自己重讀過一輪就好，不用等滿；第 3 次照常等
  realLoadPage, realReadySeconds = browser.loadPage, browser.readySeconds
  def loadPage(context, url, ready, viewport):
    monkeypatch.setattr(browser, "readySeconds", 3 if len(pageLoads) < 2 else realReadySeconds)
    return realLoadPage(context, url, ready, viewport)
  monkeypatch.setattr(browser, "loadPage", loadPage)
  with pytest.warns(UserWarning, match=r"test_renderUntilRedrawsDashboardAfterStateUnavailable .*重畫 3 次") as caught:
    dom = browserPages.renderUntil(dashboard + "#token=" + token, domHas("開始寫程式"))
  assert "開始寫程式" in dom and len(pageLoads) == 3
  assert servedRequest("/api/state", 503) in serverRequests[dashboard]
  for item in caught:
    warnings.warn_explicit(item.message, item.category, item.filename, item.lineno)


def test_renderUntilFailsWithDiagnosticsWhenNeverReady(monkeypatch):
  broken = (loadResult("<main></main>", error=timedOutError, failedRequests=[f"GET {fakeDashboard}api/state 500"]),
            [servedRequest("/", 200), servedRequest("/api/state", 500, "RuntimeError: 替身：讀狀態失敗")])
  pages = fakeBrowserPages(monkeypatch, [broken] * browser.renderAttempts)
  with pytest.raises(AssertionError) as failure:
    pages.renderUntil(fakeDashboard, "false")
  blocks = re.split(r"\n(?=第 \d 次)", str(failure.value))
  assert blocks[0] == "畫面重畫 3 次都沒有準備好："
  assert len(blocks) == 4
  for attempt, block in enumerate(blocks[1:], 1):
    assert re.match(rf"第 {attempt} 次：耗時 \d+\.\d 秒（{re.escape(timedOutError)}）\n  瀏覽器 console 的錯誤：（沒有）\n"
                    rf"  失敗的請求：\n    GET {re.escape(fakeDashboard)}api/state 500\n", block)
    assert "\n  server 收到的請求：\n" in block
    assert "    GET / 200\n" in block and "    GET /api/state 500 RuntimeError: 替身：讀狀態失敗" in block


def test_renderPageStillFailsOnBrowserTimeout(monkeypatch):
  # 只畫一次的 renderPage 沒有重畫，逾時若回傳空白畫面，只檢查文字不存在的斷言會誤判通過
  pages = fakeBrowserPages(monkeypatch, [timedOutLoad()])
  with pytest.raises(AssertionError, match="畫面沒有準備好"):
    pages.renderPage(fakeDashboard, "true")


def test_renderUntilRedrawsAfterBrowserTimeout(monkeypatch):
  pages = fakeBrowserPages(monkeypatch, [timedOutLoad(), (loadResult("<p>ready</p>", ready=True), [])])
  with pytest.warns(UserWarning, match="重畫 2 次"):
    dom = pages.renderUntil(fakeDashboard, domHas("ready"))
  assert dom == "<p>ready</p>"


def test_renderUntilFailureMarksTimedOutAttempts(monkeypatch):
  pages = fakeBrowserPages(monkeypatch, [timedOutLoad(), (loadResult("<p>empty</p>"), []), timedOutLoad()])
  with pytest.raises(AssertionError) as failure:
    pages.renderUntil(fakeDashboard, "false")
  blocks = re.split(r"\n(?=第 \d 次)", str(failure.value))
  assert blocks[0] == "畫面重畫 3 次都沒有準備好："
  assert re.match(rf"第 1 次：耗時 \d+\.\d 秒（{re.escape(timedOutError)}）\n  瀏覽器 console 的錯誤：（沒有）\n", blocks[1])
  assert re.match(r"第 2 次：耗時 \d+\.\d 秒\n", blocks[2])
  assert re.match(rf"第 3 次：耗時 \d+\.\d 秒（{re.escape(timedOutError)}）\n", blocks[3])


def test_renderFailureTextListsEachAttemptsDiagnostics():
  attempt = loadResult(consoleErrors=["未處理的例外：替身", "畫面程式壞掉了"], seconds=2.04)
  assert browser.renderFailureText("畫面重畫 1 次都沒有準備好", [attempt]) == "\n".join([
    "畫面重畫 1 次都沒有準備好：", "第 1 次：耗時 2.0 秒", "  瀏覽器 console 的錯誤：",
    "    未處理的例外：替身", "    畫面程式壞掉了", "  失敗的請求：（沒有）", "  server 收到的請求：（沒有收到）"])


def test_overview_has_main_agents_and_two_children_per_role(dashboard, gitProject, browserPages, tmp_path, monkeypatch):
  configPath = gitProject / ".board/config.json"
  config = json.loads(configPath.read_text(encoding="utf-8"))
  config["maxParallel"] = 3
  configPath.write_text(json.dumps(config), encoding="utf-8")
  addProbe(tmp_path, monkeypatch, """
    setTimeout(() => {
      const ids = selector => [...document.querySelectorAll(selector)].map(node => node.dataset.agent);
      const writer = document.querySelector('#pair [data-agent="writer"]');
      const result = {
        main: ids('#pair [data-agent]'), children: ids('#squads [data-agent]'),
        childNames: [...document.querySelectorAll('#squads .agent-name')].map(node => node.textContent.trim()),
        mainText: writer?.textContent || '',
        idleChildren: [...document.querySelectorAll('#squads .sm-state')].map(node => node.textContent.trim()),
      };
      if (writer) writer.click();
      setTimeout(() => {
        result.selected = app.selectedAgent; result.page = app.page;
        document.body.dataset.probe = JSON.stringify(result);
      }, 500);
    }, 1000);""")
  # 點下去之後換頁是 hashchange 事件，一小段一小段快轉，中間讓頁面處理
  fastForward(monkeypatch, 1500, step=50)
  dom = browserPages.renderUntil(dashboard, probeHas("main"), viewport={"width": 1440, "height": 1200})
  probe = readProbe(dom)
  assert probe["main"] == ["writer", "reviewer"]
  assert probe["children"] == ["writer-sub1", "writer-sub2", "reviewer-sub1", "reviewer-sub2"]
  assert probe["childNames"] == ["子 Agent 1", "子 Agent 2"] * 2
  assert "P0001 T1 後端批次 API" in probe["mainText"] and "75%" in probe["mainText"]
  assert probe["idleChildren"] == ["未啟動"] * 4
  assert probe["selected"] == "writer" and probe["page"] == "console"


def test_main_status_is_its_own_task_not_a_child_summary(dashboard, browserPages, tmp_path, monkeypatch):
  addProbe(tmp_path, monkeypatch, """
    setTimeout(() => {
      const state = structuredClone(app.state);
      Object.assign(state.agents.find(agent => agent.id === 'writer'), { status: '閒置', context: null });
      Object.assign(state.agents.find(agent => agent.id === 'writer-sub1'), {
        status: '寫程式中', plan: 'P0001', task: 'T1', context: { tokens: 150000, window: 200000 },
      });
      renderOverview(state);
      const main = document.querySelector('#pair [data-agent="writer"]');
      const child = document.querySelector('#squads [data-agent="writer-sub1"]');
      const result = { mainState: main?.dataset.state, mainText: main?.textContent,
        childState: child?.dataset.state, childText: child?.textContent };
      state.agents.forEach(agent => { agent.status = '已暫停'; });
      state.paused = true;
      renderOverview(state);
      result.paused = [...document.querySelectorAll('#pair [data-agent], #squads [data-agent]')]
        .every(node => node.dataset.state === 'paused');
      document.body.dataset.probe = JSON.stringify(result);
    }, 1000);""")
  fastForward(monkeypatch, 1000)
  probe = readProbe(browserPages.renderUntil(dashboard, probeHas("mainState")))
  assert probe["mainState"] == "idle" and "待命" in probe["mainText"]
  assert "P0001 T1" not in probe["mainText"]
  assert probe["childState"] == "warn" and "75%" in probe["childText"]
  assert probe["paused"] is True


def test_single_capacity_shows_only_mains_and_no_child_cards(dashboard, gitProject, browserPages, tmp_path, monkeypatch):
  path = gitProject / ".board/config.json"
  config = json.loads(path.read_text(encoding="utf-8"))
  config["maxParallel"] = 1
  path.write_text(json.dumps(config), encoding="utf-8")
  addProbe(tmp_path, monkeypatch, """
    setTimeout(() => {
      document.body.dataset.probe = JSON.stringify({
        main: [...document.querySelectorAll('#pair [data-agent]')].map(node => node.dataset.agent),
        children: document.querySelectorAll('#squads [data-agent]').length,
        text: document.querySelector('#squads').textContent,
      });
    }, 1000);""")
  fastForward(monkeypatch, 1000)
  probe = readProbe(browserPages.renderUntil(dashboard, probeHas("main")))
  assert probe["main"] == ["writer", "reviewer"]
  assert probe["children"] == 0 and "無子 agent 名額" in probe["text"]


@pytest.mark.parametrize("width", [1440, 560])
def test_main_and_child_cards_fit_the_window(dashboard, gitProject, browserPages, tmp_path, monkeypatch, width):
  path = gitProject / ".board/config.json"
  config = json.loads(path.read_text(encoding="utf-8"))
  config["maxParallel"] = 3
  path.write_text(json.dumps(config), encoding="utf-8")
  addProbe(tmp_path, monkeypatch, """
    setTimeout(() => {
      const cards = [...document.querySelectorAll('#pair [data-agent], #squads [data-agent]')];
      document.body.dataset.probe = JSON.stringify({
        total: cards.length,
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        right: Math.max(...cards.map(card => card.getBoundingClientRect().right)),
        viewport: innerWidth,
      });
    }, 1500);""")
  screenshot = tmp_path / f"agent-overview-{width}.png"
  fastForward(monkeypatch, 1500, drawn=f"{appDrawn} && {fontsLoaded}", screenshot=screenshot)
  dom = browserPages.renderUntil(dashboard + "#token=" + token, probeHas("total"), viewport={"width": width, "height": 1200})
  probe = readProbe(dom)
  assert probe["total"] == 6
  assert probe["overflow"] <= 0 and probe["right"] <= probe["viewport"]
  print(f"Overview screenshot: {screenshot}")


def test_overviewShowsConductorGroupsAndAgents(dashboard, browserPages):
  dom = browserPages.renderUntil(dashboard + "#token=" + token, domHas("Alfred<span>（指揮程式）</span>"))
  assert "Alfred<span>（指揮程式）</span>" in dom and "沒有啟動" in dom
  assert "進行中 1 份 plan" in dom and "P0001 批次上傳" in dom
  assert 'data-agent="writer-sub1"' in dom and 'data-agent="reviewer-sub1"' in dom
  assert 'data-agent="reviewer-sub2"' not in dom
  assert "Claude<span>（寫程式）</span>" in dom and "Codex<span>（審查）</span>" in dom
  assert "Write upload.py" in dom and "75%" in dom and "sonnet" in dom
  assert "唯讀" not in dom
  assert "測試中：" not in dom


def test_overviewLeadCardListsTestingTasks(dashboard, gitProject, browserPages):
  # 寫完在背景跑測試時列在 Alfred 卡片；測試跑完轉成待審查就不再列出
  appendEvent(gitProject, "taskWritten", plan="P0001", task="T1", roundNumber=1,
              data={"commit": "c1", "summary": "完成", "testing": True})
  dom = browserPages.renderUntil(dashboard + "#token=" + token, domHas("測試中：P0001 T1"))
  lead = re.search(r'id="leadCard".*?</article>', dom, re.S)
  assert lead and "測試中：P0001 T1" in lead.group(0)
  appendEvent(gitProject, "testRun", plan="P0001", task="T1", roundNumber=1,
              data={"passed": True, "tail": "ok", "verdict": "passed", "newFailures": []})
  # 要看的是「測試中：」不在畫面上，沒有資料的畫面也不在；等 Alfred 卡片畫出來才算有資料
  dom = browserPages.renderUntil(dashboard + "#token=" + token, domHas("Alfred<span>（指揮程式）</span>"))
  assert "測試中：" not in dom


def test_overviewShowsRunningCliUntilMainTaskEnds(dashboard, gitProject, browserPages):
  (gitProject / ".board" / "config.json").write_text(json.dumps({
    "maxParallel": 2, "writer": {"cli": "codex", "model": "gpt-6.1-sol"}, "reviewer": {"cli": "claude", "model": "opus"},
  }), encoding="utf-8")
  dom = browserPages.renderUntil(dashboard + "#token=" + token, domHas("Claude<span>（寫程式）</span>"))
  # writer 還在執行舊的 Claude 任務，大卡要呈現真實執行身分；閒置的 reviewer 使用新設定。
  assert "Claude<span>（寫程式）</span>" in dom and "Claude<span>（審查）</span>" in dom
  assert "Codex<span>（寫程式）</span>" not in dom and "opus" in dom


def test_idleMainShowsTheNewRoleConfiguration(dashboard, gitProject, browserPages):
  (gitProject / ".board/config.json").write_text(json.dumps({
    "maxParallel": 3, "writer": {"cli": "codex", "model": "gpt-6.1-sol"},
    "reviewer": {"cli": "claude", "model": "opus"},
  }), encoding="utf-8")
  appendEvent(gitProject, "taskWritten", plan="P0001", task="T1", agent="writer", roundNumber=1,
              data={"commit": "abc", "summary": "完成任務"})
  dom = browserPages.renderUntil(dashboard, domHas("Codex<span>（寫程式）</span>"))
  assert "Codex<span>（寫程式）</span>" in dom and "Claude<span>（審查）</span>" in dom
  assert "gpt-6.1-sol" in dom and "opus" in dom


def test_withoutTokenPageIsReadOnly(dashboard, browserPages):
  readOnly = "唯讀：請用 server.py 印出的網址重新開啟"
  assert readOnly in browserPages.renderUntil(dashboard, domHas(readOnly))


def test_tasksPageDrawsTreeFromServer(dashboard, browserPages):
  # 樹先畫出來，選到的 plan 細節另外讀；兩邊都畫好才取畫面
  dom = browserPages.renderUntil(dashboard + "#tasks", domHas('data-plan-side="前端"') + " && " + domHas("橘｜"))
  assert 'data-plan-side="前端"' in dom and 'data-plan-side="後端"' in dom
  assert "P0002 背景縮圖" in dom
  assert "橘｜" in dom and "沒有對應需求" in dom
  assert 'data-class="extra"' in dom
  assert "後端・縮圖" in dom
  assert "放棄這份 plan" in dom


def test_tasksPageShowsStatsOfApprovedPlan(dashboard, gitProject, browserPages, tmp_path, monkeypatch, capsys):
  # 一開始選的是待核准的 P0002，沒有成效；換到進行中的 P0001 才出現，數字和 board.py stats P0001 一樣
  addProbe(tmp_path, monkeypatch, """
    const read = () => ({
      plan: app.selectedPlan, hidden: document.querySelector('#planStats').hidden,
      items: [...document.querySelectorAll('#planStatsList dt')].map(dt => [dt.textContent, dt.nextElementSibling.textContent]),
    });
    const result = {};
    setTimeout(() => { result.pending = read(); selectPlan('P0001'); }, 1500);
    setTimeout(() => { result.approved = read(); document.body.dataset.probe = JSON.stringify(result); }, 3500);""")
  # P0002 的細節讀回來才快轉到第一個計時器；換到 P0001 後等它的成效畫出來，再快轉到第二個
  fastForward(monkeypatch, 1500, drawn=f"{appDrawn} && {domHas('橘｜')}",
              then=[("app.selectedPlan === 'P0001' && !document.querySelector('#planStats').hidden", 2000)])
  probe = readProbe(browserPages.renderUntil(dashboard + "#tasks", probeHas("pending", "plan")))
  assert probe["pending"] == {"plan": "P0002", "hidden": True, "items": []}
  assert probe["approved"]["plan"] == "P0001" and probe["approved"]["hidden"] is False
  capsys.readouterr()
  assert board.cmdStats(gitProject, "P0001") == 0
  cliLines = [line.strip() for line in capsys.readouterr().out.splitlines()[1:]]
  pageLines = []
  for label, value in probe["approved"]["items"]:
    if label == "每個任務的輪數":
      pageLines += [f"{label}：", *value.split("\n")]
    else:
      pageLines.append(f"{label}：{value}")
  # 耗時算到現在，兩邊讀的時間不同，只比是不是進行中
  elapsed = [line for line in pageLines if line.startswith("耗時：")]
  assert len(elapsed) == 1 and elapsed[0].endswith("（進行中）")
  assert [line for line in pageLines if not line.startswith("耗時：")] == \
         [line for line in cliLines if not line.startswith("耗時：")]


def test_opsFeedAndBadges(dashboard, browserPages):
  dom = browserPages.renderUntil(dashboard + "#token=" + token, domHas("開始寫程式"))
  assert "開始寫程式" in dom
  # 異常：等你處理（紅）、conductor 沒在跑、Context 偏高 → 3 筆，有紅色所以徽章是紅的
  assert '<b class="badge" data-tone="red">3</b>' in dom
  # 人工介入：待核准的 P0002 + 等你處理的 P0001 T2
  assert '<b class="badge">2</b>' in dom


def test_consoleShowsBusyAgentDetail(dashboard, browserPages):
  # 任務細節另外讀 log、系統資源摘要另外讀監控資料；身分、細節和摘要都畫好才取畫面
  # 摘要在監控資料回來前就先畫出空的列，要等數值出現
  ready = " && ".join(domHas(text) for text in ("（Claude・寫程式）", "第 1/3 輪", "75.0%"))
  dom = browserPages.renderUntil(dashboard + "#console", ready)
  assert "（Claude・寫程式）" in dom
  assert "P0001 T1 後端批次 API" in dom and "第 1/3 輪" in dom
  assert "「Astra 對話窗」還沒有接資料" in dom and "「系統資源監控」還沒有接資料" not in dom
  # 系統資源區塊換成系統監控模組的三項摘要，整塊連到完整頁
  summary = dom[dom.index('id="systemMonitorSummary"'):]
  assert summary.startswith('id="systemMonitorSummary"><a class="smon-summary" href="#system-monitor">')
  for key in ("cpu", "memory", "disk"):
    assert f'class="smon-sum-row" data-key="{key}"' in summary
  assert "75.0%" in summary and "25.0%" in summary and "server 主機" in summary


def test_systemMonitorPageMountsCardsAndCharts(dashboard, gitProject, browserPages):
  # 換頁不用等資料，先看的區塊顯示與否沒有資料也成立；卡片在資料回來前就先畫出空的，等數值出現才算有資料
  dom = browserPages.renderUntil(dashboard + "#system-monitor", domHas('class="smon-card"') + " && " + domHas("75.0%"))
  assert "hidden" not in sectionTag(dom, "system-monitor")
  assert "hidden" in sectionTag(dom, "overview") and "hidden" in sectionTag(dom, "placeholder")
  link = railLink(dom, "system-monitor")
  assert link and 'aria-current="page"' in link and 'title="系統監控"' in link
  assert '<span id="termPage">— 系統監控</span>' in dom
  page = dom[dom.index('id="systemMonitorPage"'):]
  for key in ("cpu", "memory", "disk"):
    assert f'class="smon-card" data-key="{key}"' in page
  for key in ("cpu", "memory"):
    assert re.search(rf'class="smon-chart" data-key="{key}"[^>]*>.*?<svg', page, re.S)
  assert f"磁碟 {metrics.volumeLabel(gitProject.resolve())}" in page
  assert "75.0%" in page and "25.0%" in page and "監控對象：server 主機" in page


def test_topologyLinkRedirectsToSystemMonitor(dashboard, browserPages, tmp_path, monkeypatch):
  addProbe(tmp_path, monkeypatch, "document.body.dataset.probe = JSON.stringify({ hash: location.hash, page: app.page });")
  dom = browserPages.renderUntil(dashboard + "#topology", probeHas("page"))
  assert readProbe(dom) == {"hash": "#system-monitor", "page": "system-monitor"}
  assert "hidden" not in sectionTag(dom, "system-monitor")
  # 左側導覽不再有空白的拓撲頁
  assert railLink(dom, "topology") is None and "拓撲" not in dom


def test_consoleSummaryOpensFullPage(dashboard, browserPages, tmp_path, monkeypatch):
  addProbe(tmp_path, monkeypatch, """
    const summaryLink = document.querySelector('#systemMonitorSummary a');
    const focusable = summaryLink.tabIndex >= 0;
    setTimeout(() => summaryLink.click(), 1000);
    setTimeout(() => {
      document.body.dataset.probe = JSON.stringify({
        focusable, hash: location.hash, page: app.page,
        shown: !document.querySelector('#systemMonitorPage').hidden,
        cards: document.querySelectorAll('#systemMonitorPage .smon-card').length,
      });
    }, 2500);""")
  # 快轉到點擊；換到完整頁、監控資料讀回來後，再快轉到量測
  fastForward(monkeypatch, 1000, then=[(monitorDrawn, 1500)])
  dom = browserPages.renderUntil(dashboard + "#console", probeHas("cards"))
  assert readProbe(dom) == {"focusable": True, "hash": "#system-monitor", "page": "system-monitor", "shown": True, "cards": 3}


def test_switchingPagesKeepsOtherPagesAndStopsMonitorPolling(dashboard, browserPages, tmp_path, monkeypatch):
  # 從系統監控頁依序切到總覽、任務、模型、設定，再到控制台；離開期間不能再讀監控資料，到控制台摘要才再讀
  addProbe(tmp_path, monkeypatch, """
    let monitorCalls = 0;
    const realFetch = window.fetch;
    window.fetch = (url, init) => {
      if (String(url).includes('/api/system-monitor')) monitorCalls += 1;
      return realFetch(url, init);
    };
    const visits = [];
    const record = () => visits.push({
      hash: location.hash,
      current: document.querySelector('.rail-btn[aria-current="page"]').dataset.page,
      shown: [...document.querySelectorAll('.page')].filter(section => !section.hidden).map(section => section.dataset.page),
      label: document.querySelector('#emptyLabel').textContent,
    });
    const result = { visits };
    ['overview', 'tasks', 'models', 'settings'].forEach((key, index) => {
      setTimeout(() => { location.hash = key; }, 300 + index * 500);
      setTimeout(record, 550 + index * 500);
    });
    setTimeout(() => { result.whileAway = monitorCalls; location.hash = 'console'; }, 4000);
    setTimeout(() => {
      result.onConsole = monitorCalls;
      document.body.dataset.probe = JSON.stringify(result);
    }, 4800);""")
  # 要跨過好幾輪輪詢，用 page.clock 一小段一小段快轉，每段之間讓頁面處理換頁；監控資料第一次讀回來才開始
  fastForward(monkeypatch, 4800, drawn=f"{appDrawn} && {monitorDrawn}", step=50)
  dom = browserPages.renderUntil(dashboard + "#system-monitor", probeHas("visits"))
  probe = readProbe(dom)
  # 標籤在當下那一頁記錄；最後停在控制台，DOM 裡的標籤已經換掉
  assert probe["visits"] == [
    {"hash": "#overview", "current": "overview", "shown": ["overview"], "label": "總覽"},
    {"hash": "#tasks", "current": "tasks", "shown": ["tasks"], "label": "任務"},
    {"hash": "#models", "current": "models", "shown": ["models"], "label": "模型"},
    {"hash": "#settings", "current": "settings", "shown": ["placeholder"], "label": "設定"},
  ]
  # 3 秒的輪詢在離開系統監控頁時就停了；進控制台立刻讀一次
  assert probe["whileAway"] == 0 and probe["onConsole"] == 1


def test_narrowWindowStacksMonitorCardsWithoutOverflow(dashboard, browserPages, tmp_path, monkeypatch):
  addProbe(tmp_path, monkeypatch, """
    setTimeout(() => {
      const page = document.querySelector('#systemMonitorPage');
      const right = page.getBoundingClientRect().right;
      const rights = [...page.querySelectorAll('*')].map(node => node.getBoundingClientRect().right);
      const lefts = selector => [...page.querySelectorAll(selector)].map(node => Math.round(node.getBoundingClientRect().left));
      const tops = selector => [...page.querySelectorAll(selector)].map(node => Math.round(node.getBoundingClientRect().top));
      document.body.dataset.probe = JSON.stringify({
        width: innerWidth,
        documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        pageOverflow: page.scrollWidth - page.clientWidth,
        contentOverflow: Math.max(...rights) - right,
        cardLefts: lefts('.smon-card'), cardTops: tops('.smon-card'),
        chartLefts: lefts('.smon-chart'), chartTops: tops('.smon-chart'),
      });
    }, 3000);""")
  # 視窗寬度沒有資料也量得到；等監控卡片畫出來才算有資料
  fastForward(monkeypatch, 3000, drawn=f"{monitorDrawn} && {fontsLoaded}")
  dom = browserPages.renderUntil(dashboard + "#system-monitor", probeHas("cardTops"), viewport={"width": 560, "height": 1000})
  probe = readProbe(dom)
  assert probe["width"] <= 760
  assert probe["documentOverflow"] <= 0 and probe["pageOverflow"] <= 0 and probe["contentOverflow"] <= 1
  # 三張卡和兩張走勢都改成直向排列：同一個左邊界，由上往下
  assert len(set(probe["cardLefts"])) == 1 and probe["cardTops"] == sorted(set(probe["cardTops"]))
  assert len(probe["cardTops"]) == 3
  assert len(set(probe["chartLefts"])) == 1 and probe["chartTops"] == sorted(set(probe["chartTops"]))
  assert len(probe["chartTops"]) == 2


def test_assistantGreetsAndOffersQuickAsks(dashboard, browserPages):
  dom = browserPages.renderUntil(dashboard, domHas("你好，我是儀表板助手"))
  assert "你好，我是儀表板助手" in dom
  assert 'data-ask="等你處理的有哪些？"' in dom and 'data-ask="writer 在做什麼？"' in dom


def test_modelsPageShowsRoleSettings(dashboard, browserPages):
  # 換頁不用等資料；等角色目前的設定畫出來才算有資料
  dom = browserPages.renderUntil(dashboard + "#models", domHas("目前：Claude・sonnet"))
  section = re.search(r'<section class="page models-page"[^>]*>', dom)
  assert section and "hidden" not in section.group(0)
  assert "目前：Claude・sonnet" in dom and "目前：Codex・預設模型" in dom
  assert 'data-role-model="writer" disabled' in dom
  selected = re.search(r'<option[^>]*value="sonnet"[^>]*>', dom)
  assert selected and "selected" in selected.group(0) and "disabled" in selected.group(0)
  # 沒帶 token 只能看
  assert 'data-save-role="writer" disabled' in dom


def test_modelsPageLoadsCliModelsAndEfforts(dashboard, browserPages, tmp_path, monkeypatch):
  import board_models
  def discover(projectDir, cli, command, **kwargs):
    model = "sonnet" if cli == "claude" else "gpt-test-new"
    return [{"id": model, "displayName": "CLI New Version", "resolvedModel": "full-model-v2",
             "description": "CLI model description", "efforts": [{"value": "high", "description": "Deep"}]}]
  monkeypatch.setattr(board_models, "discoverModels", discover)
  # 只在測試伺服器的 main.js 預填 sessionStorage；產品的 token 流程另有既有測試。
  main = tmp_path / "main.js"
  main.write_text(f"sessionStorage.setItem('boardToken', '{token}');\n" +
                  (toolDir / "web/js/main.js").read_text(encoding="utf-8"), encoding="utf-8")
  monkeypatch.setitem(server.staticFiles, "/js/main.js", (str(main), "text/javascript; charset=utf-8"))
  dom = browserPages.renderUntil(dashboard + "#models", domHas("CLI New Version"))
  assert "CLI New Version" in dom and "full-model-v2" in dom
  assert 'data-role-effort="writer"' in dom and 'value="high"' in dom
  assert "重新整理模型" in dom
