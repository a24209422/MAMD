"""用 Edge（或 Chrome）無頭模式載入儀表板，檢查畫面真的畫出 API 的資料。找不到瀏覽器就跳過。"""
import itertools
import json
import os
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

import pytest

import board
import server
from board_core import appendEvent
from modules.system_monitor import api as monitorApi
from modules.system_monitor import metrics

toolDir = Path(__file__).resolve().parent.parent
browserCandidates = [
  r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
  r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]
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
renderAttempts = 3
stderrTailLines = 5
# 每個測試 server 的網址 → 它收到的請求，renderUntil 失敗時列出來
serverRequests = {}
fakeDashboard = "http://127.0.0.1:1/"


def findBrowser():
  for name in ("msedge", "chrome", "chromium"):
    found = shutil.which(name)
    if found:
      return found
  return next((path for path in browserCandidates if Path(path).exists()), None)


browser = findBrowser()
nodePath = shutil.which("node")
needsBrowser = pytest.mark.skipif(browser is None, reason="找不到 Edge 或 Chrome，跳過網頁畫面測試")


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


class RenderedDom(str):
  # 照舊當成 DOM 文字用；另外帶著瀏覽器的 stderr 與耗時，重畫都失敗時拿來診斷
  def __new__(cls, dom, stderr, seconds, timedOut=False):
    rendered = super().__new__(cls, dom)
    rendered.stderr, rendered.seconds, rendered.timedOut = stderr, seconds, timedOut
    return rendered


def renderDom(tmp_path, url, *extraArgs, profile="browser"):
  # virtual-time-budget 讓頁面的計時器先跑幾輪輪詢，再把畫好的 DOM 印出來
  started = time.monotonic()
  completed = subprocess.run(
    [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
     f"--user-data-dir={tmp_path / profile}", "--virtual-time-budget=6000", *extraArgs, "--dump-dom", url],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
  return RenderedDom(completed.stdout, completed.stderr, time.monotonic() - started)


def renderAttempt(tmp_path, url, extraArgs, profile):
  # 負載高時瀏覽器偶爾卡住，只有 renderUntil 當成這次沒畫好再重畫；直接呼叫 renderDom 仍要因逾時失敗，免得空白畫面讓斷言誤判通過
  started = time.monotonic()
  try:
    return renderDom(tmp_path, url, *extraArgs, profile=profile)
  except subprocess.TimeoutExpired as timeout:
    return RenderedDom(outputText(timeout.stdout), outputText(timeout.stderr), time.monotonic() - started, timedOut=True)


def outputText(output):
  # 逾時時已收到的輸出在部分平台是 bytes 或 None
  if isinstance(output, bytes):
    return output.decode("utf-8", errors="replace")
  return output or ""


def renderUntil(tmp_path, url, ready, *extraArgs):
  # 負載高時偶爾拿到還沒有資料的畫面，單獨重跑就過；換新的 user-data-dir 重畫，真的壞掉時每次都不成立仍會失敗
  requests = next((recorded for base, recorded in serverRequests.items() if url.startswith(base)), [])
  tries = []
  for attempt in range(1, renderAttempts + 1):
    firstRequest = len(requests)
    dom = renderAttempt(tmp_path, url, extraArgs, f"browser-{attempt}")
    if ready(dom):
      if attempt > 1:
        warnings.warn(f"{currentTestName()} 的畫面重畫 {attempt} 次才有資料")
      return dom
    tries.append((dom, requests[firstRequest:]))
  raise AssertionError(renderFailureText(tries))


def currentTestName():
  return os.environ.get("PYTEST_CURRENT_TEST", "").rsplit(" ", 1)[0]


def renderFailureText(tries):
  timedOut = [str(attempt) for attempt, (dom, requests) in enumerate(tries, 1) if dom.timedOut]
  timedOutText = f"（第 {'、'.join(timedOut)} 次逾時）" if timedOut else ""
  lines = [f"畫面重畫 {len(tries)} 次都沒有準備好{timedOutText}："]
  for attempt, (dom, requests) in enumerate(tries, 1):
    stderrTail = dom.stderr.strip().splitlines()[-stderrTailLines:]
    lines.append(f"第 {attempt} 次：耗時 {dom.seconds:.1f} 秒" + ("（逾時）" if dom.timedOut else ""))
    lines.append("  瀏覽器 stderr 最後幾行：" + ("" if stderrTail else "（沒有輸出）"))
    lines += [f"    {line}" for line in stderrTail]
    lines.append("  server 收到的請求：" + ("" if requests else "（沒有收到）"))
    lines += [f"    {requestText(request)}" for request in requests]
  return "\n".join(lines)


def requestText(request):
  text = f"{request['method']} {request['path']} {request['status']}"
  return f"{text} {request['error']}" if request["error"] else text


def addProbe(tmp_path, monkeypatch, script):
  # 只在測試伺服器的 main.js 後面接一段操作與量測；結果寫進 body 的 data-probe，dump-dom 才看得到
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


def probeReady(pick):
  # 量測在計時器到了就寫出，畫面還沒有資料時也會寫；要等第一個斷言要看的值有資料才算畫好，值對不對仍交給斷言
  return lambda dom: bool(probeMatch(dom)) and bool(pick(readProbe(dom)))


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


def fakeDashboardRuns(monkeypatch, renders):
  # 替身瀏覽器：每次重畫固定回傳畫面和這次 server 收到的請求，不開真的 Edge，負載高時也不會自己偶發失敗
  requests = []
  monkeypatch.setitem(serverRequests, fakeDashboard, requests)
  remaining = iter(renders)
  def run(command, **kwargs):
    dom, received = next(remaining)
    requests.extend(received)
    return subprocess.CompletedProcess(command, 0, stdout=dom, stderr="edge started\n")
  monkeypatch.setattr(subprocess, "run", run)


def test_renderUntilRedrawsWhenStateIsNotReadyYet(tmp_path, monkeypatch):
  # 替身：前兩次載入頁面時 /api/state 都回 503，畫面只有骨架；第 3 次載入才有資料
  skeleton = ('<main id="overview"></main>', [servedRequest("/", 200), servedRequest("/api/state", 503)])
  loaded = ('<main id="overview"><button>開始寫程式</button></main>',
            [servedRequest("/", 200), servedRequest("/api/state", 200)])
  fakeDashboardRuns(monkeypatch, [skeleton, skeleton, loaded])
  with pytest.warns(UserWarning, match=r"test_renderUntilRedrawsWhenStateIsNotReadyYet .*重畫 3 次") as caught:
    dom = renderUntil(tmp_path, fakeDashboard + "#token=" + token, lambda dom: "開始寫程式" in dom)
  assert dom == loaded[0]
  # 驗證時要在 pytest 結尾的 warnings summary 看到重畫次數，檢查完原樣再發出
  for item in caught:
    warnings.warn_explicit(item.message, item.category, item.filename, item.lineno)


@needsBrowser
def test_renderUntilRedrawsDashboardAfterStateUnavailable(dashboard, tmp_path, monkeypatch):
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
  with pytest.warns(UserWarning, match=r"test_renderUntilRedrawsDashboardAfterStateUnavailable .*重畫 3 次") as caught:
    dom = renderUntil(tmp_path, dashboard + "#token=" + token, lambda dom: "開始寫程式" in dom)
  assert "開始寫程式" in dom and len(pageLoads) == 3
  assert servedRequest("/api/state", 503) in serverRequests[dashboard]
  for item in caught:
    warnings.warn_explicit(item.message, item.category, item.filename, item.lineno)


def test_renderUntilFailsWithDiagnosticsWhenNeverReady(tmp_path, monkeypatch):
  broken = ("<main></main>", [servedRequest("/", 200), servedRequest("/api/state", 500, "RuntimeError: 替身：讀狀態失敗")])
  fakeDashboardRuns(monkeypatch, [broken] * renderAttempts)
  with pytest.raises(AssertionError) as failure:
    renderUntil(tmp_path, fakeDashboard, lambda dom: False)
  blocks = re.split(r"\n(?=第 \d 次)", str(failure.value))
  assert blocks[0] == "畫面重畫 3 次都沒有準備好："
  assert len(blocks) == 4
  for attempt, block in enumerate(blocks[1:], 1):
    assert re.match(rf"第 {attempt} 次：耗時 \d+\.\d 秒\n  瀏覽器 stderr 最後幾行：\n    edge started\n", block)
    assert "\n  server 收到的請求：\n" in block
    assert "    GET / 200\n" in block and "    GET /api/state 500 RuntimeError: 替身：讀狀態失敗" in block


def fakeBrowserRuns(monkeypatch, outcomes):
  # 替身瀏覽器：依序逾時或印出 DOM，不必真的開瀏覽器也能重現負載高時的逾時
  remaining = iter(outcomes)
  def run(command, **kwargs):
    outcome = next(remaining)
    if outcome == "timeout":
      raise subprocess.TimeoutExpired(command, kwargs["timeout"], output="", stderr="edge hung\n")
    return subprocess.CompletedProcess(command, 0, stdout=outcome, stderr="")
  monkeypatch.setattr(subprocess, "run", run)


def test_renderDomStillFailsOnBrowserTimeout(tmp_path, monkeypatch):
  # 直接呼叫 renderDom 的測試沒有重畫，逾時若變成空白畫面，只檢查文字不存在的斷言會誤判通過
  fakeBrowserRuns(monkeypatch, ["timeout"])
  with pytest.raises(subprocess.TimeoutExpired):
    renderDom(tmp_path, "http://127.0.0.1:1/")


def test_renderUntilRedrawsAfterBrowserTimeout(tmp_path, monkeypatch):
  fakeBrowserRuns(monkeypatch, ["timeout", "<p>ready</p>"])
  with pytest.warns(UserWarning, match="重畫 2 次"):
    dom = renderUntil(tmp_path, "http://127.0.0.1:1/", lambda dom: "ready" in dom)
  assert dom == "<p>ready</p>" and not dom.timedOut


def test_renderUntilFailureMarksTimedOutAttempts(tmp_path, monkeypatch):
  fakeBrowserRuns(monkeypatch, ["timeout", "<p>empty</p>", "timeout"])
  with pytest.raises(AssertionError) as failure:
    renderUntil(tmp_path, "http://127.0.0.1:1/", lambda dom: False)
  blocks = re.split(r"\n(?=第 \d 次)", str(failure.value))
  assert blocks[0] == "畫面重畫 3 次都沒有準備好（第 1、3 次逾時）："
  assert re.match(r"第 1 次：耗時 \d+\.\d 秒（逾時）\n  瀏覽器 stderr 最後幾行：\n    edge hung\n", blocks[1])
  assert re.match(r"第 2 次：耗時 \d+\.\d 秒\n", blocks[2])
  assert re.match(r"第 3 次：耗時 \d+\.\d 秒（逾時）\n", blocks[3])


def test_renderFailureTextKeepsLastStderrLines():
  dom = RenderedDom("<html></html>", "".join(f"line{number}\n" for number in range(1, 8)), 2.04)
  assert renderFailureText([(dom, [])]) == "\n".join([
    "畫面重畫 1 次都沒有準備好：", "第 1 次：耗時 2.0 秒", "  瀏覽器 stderr 最後幾行：",
    *[f"    line{number}" for number in range(3, 8)], "  server 收到的請求：（沒有收到）"])


@needsBrowser
def test_overview_has_main_agents_and_two_children_per_role(dashboard, gitProject, tmp_path, monkeypatch):
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
  dom = renderUntil(tmp_path, dashboard, probeReady(lambda probe: probe["main"]), "--window-size=1440,1200")
  probe = readProbe(dom)
  assert probe["main"] == ["writer", "reviewer"]
  assert probe["children"] == ["writer-sub1", "writer-sub2", "reviewer-sub1", "reviewer-sub2"]
  assert probe["childNames"] == ["子 Agent 1", "子 Agent 2"] * 2
  assert "P0001 T1 後端批次 API" in probe["mainText"] and "75%" in probe["mainText"]
  assert probe["idleChildren"] == ["未啟動"] * 4
  assert probe["selected"] == "writer" and probe["page"] == "console"


@needsBrowser
def test_main_status_is_its_own_task_not_a_child_summary(dashboard, tmp_path, monkeypatch):
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
  probe = readProbe(renderUntil(tmp_path, dashboard, probeReady(lambda probe: probe.get("mainState"))))
  assert probe["mainState"] == "idle" and "待命" in probe["mainText"]
  assert "P0001 T1" not in probe["mainText"]
  assert probe["childState"] == "warn" and "75%" in probe["childText"]
  assert probe["paused"] is True


@needsBrowser
def test_single_capacity_shows_only_mains_and_no_child_cards(dashboard, gitProject, tmp_path, monkeypatch):
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
  probe = readProbe(renderUntil(tmp_path, dashboard, probeReady(lambda probe: probe["main"])))
  assert probe["main"] == ["writer", "reviewer"]
  assert probe["children"] == 0 and "無子 agent 名額" in probe["text"]


@needsBrowser
@pytest.mark.parametrize("width", [1440, 560])
def test_main_and_child_cards_fit_the_window(dashboard, gitProject, tmp_path, monkeypatch, width):
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
  dom = renderUntil(tmp_path, dashboard + "#token=" + token, probeReady(lambda probe: probe["total"]),
                    f"--window-size={width},1200", f"--screenshot={screenshot}")
  probe = readProbe(dom)
  assert probe["total"] == 6
  assert probe["overflow"] <= 0 and probe["right"] <= probe["viewport"]
  print(f"Overview screenshot: {screenshot}")


@needsBrowser
def test_overviewShowsConductorGroupsAndAgents(dashboard, tmp_path):
  dom = renderUntil(tmp_path, dashboard + "#token=" + token, lambda dom: "Alfred<span>（指揮程式）</span>" in dom)
  assert "Alfred<span>（指揮程式）</span>" in dom and "沒有啟動" in dom
  assert "進行中 1 份 plan" in dom and "P0001 批次上傳" in dom
  assert 'data-agent="writer-sub1"' in dom and 'data-agent="reviewer-sub1"' in dom
  assert 'data-agent="reviewer-sub2"' not in dom
  assert "Claude<span>（寫程式）</span>" in dom and "Codex<span>（審查）</span>" in dom
  assert "Write upload.py" in dom and "75%" in dom and "sonnet" in dom
  assert "唯讀" not in dom
  assert "測試中：" not in dom


@needsBrowser
def test_overviewLeadCardListsTestingTasks(dashboard, gitProject, tmp_path):
  # 寫完在背景跑測試時列在 Alfred 卡片；測試跑完轉成待審查就不再列出
  appendEvent(gitProject, "taskWritten", plan="P0001", task="T1", roundNumber=1,
              data={"commit": "c1", "summary": "完成", "testing": True})
  dom = renderUntil(tmp_path, dashboard + "#token=" + token, lambda dom: "測試中：P0001 T1" in dom)
  lead = re.search(r'id="leadCard".*?</article>', dom, re.S)
  assert lead and "測試中：P0001 T1" in lead.group(0)
  appendEvent(gitProject, "testRun", plan="P0001", task="T1", roundNumber=1,
              data={"passed": True, "tail": "ok", "verdict": "passed", "newFailures": []})
  # 要看的是「測試中：」不在畫面上，沒有資料的畫面也不在；等 Alfred 卡片畫出來才算有資料
  dom = renderUntil(tmp_path, dashboard + "#token=" + token, lambda dom: "Alfred<span>（指揮程式）</span>" in dom)
  assert "測試中：" not in dom


@needsBrowser
def test_overviewShowsRunningCliUntilMainTaskEnds(dashboard, gitProject, tmp_path):
  (gitProject / ".board" / "config.json").write_text(json.dumps({
    "maxParallel": 2, "writer": {"cli": "codex", "model": "gpt-6.1-sol"}, "reviewer": {"cli": "claude", "model": "opus"},
  }), encoding="utf-8")
  dom = renderUntil(tmp_path, dashboard + "#token=" + token, lambda dom: "Claude<span>（寫程式）</span>" in dom)
  # writer 還在執行舊的 Claude 任務，大卡要呈現真實執行身分；閒置的 reviewer 使用新設定。
  assert "Claude<span>（寫程式）</span>" in dom and "Claude<span>（審查）</span>" in dom
  assert "Codex<span>（寫程式）</span>" not in dom and "opus" in dom


@needsBrowser
def test_idleMainShowsTheNewRoleConfiguration(dashboard, gitProject, tmp_path):
  (gitProject / ".board/config.json").write_text(json.dumps({
    "maxParallel": 3, "writer": {"cli": "codex", "model": "gpt-6.1-sol"},
    "reviewer": {"cli": "claude", "model": "opus"},
  }), encoding="utf-8")
  appendEvent(gitProject, "taskWritten", plan="P0001", task="T1", agent="writer", roundNumber=1,
              data={"commit": "abc", "summary": "完成任務"})
  dom = renderUntil(tmp_path, dashboard, lambda dom: "Codex<span>（寫程式）</span>" in dom)
  assert "Codex<span>（寫程式）</span>" in dom and "Claude<span>（審查）</span>" in dom
  assert "gpt-6.1-sol" in dom and "opus" in dom


@needsBrowser
def test_withoutTokenPageIsReadOnly(dashboard, tmp_path):
  readOnly = "唯讀：請用 server.py 印出的網址重新開啟"
  assert readOnly in renderUntil(tmp_path, dashboard, lambda dom: readOnly in dom)


@needsBrowser
def test_tasksPageDrawsTreeFromServer(dashboard, tmp_path):
  dom = renderUntil(tmp_path, dashboard + "#tasks", lambda dom: 'data-plan-side="前端"' in dom)
  assert 'data-plan-side="前端"' in dom and 'data-plan-side="後端"' in dom
  assert "P0002 背景縮圖" in dom
  assert "橘｜" in dom and "沒有對應需求" in dom
  assert 'data-class="extra"' in dom
  assert "後端・縮圖" in dom
  assert "放棄這份 plan" in dom


@needsBrowser
def test_tasksPageShowsStatsOfApprovedPlan(dashboard, gitProject, tmp_path, monkeypatch, capsys):
  # 一開始選的是待核准的 P0002，沒有成效；換到進行中的 P0001 才出現，數字和 board.py stats P0001 一樣
  addProbe(tmp_path, monkeypatch, """
    const read = () => ({
      plan: app.selectedPlan, hidden: document.querySelector('#planStats').hidden,
      items: [...document.querySelectorAll('#planStatsList dt')].map(dt => [dt.textContent, dt.nextElementSibling.textContent]),
    });
    const result = {};
    setTimeout(() => { result.pending = read(); selectPlan('P0001'); }, 1500);
    setTimeout(() => { result.approved = read(); document.body.dataset.probe = JSON.stringify(result); }, 3500);""")
  probe = readProbe(renderUntil(tmp_path, dashboard + "#tasks", probeReady(lambda probe: probe["pending"]["plan"])))
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


@needsBrowser
def test_opsFeedAndBadges(dashboard, tmp_path):
  dom = renderUntil(tmp_path, dashboard + "#token=" + token, lambda dom: "開始寫程式" in dom)
  assert "開始寫程式" in dom
  # 異常：等你處理（紅）、conductor 沒在跑、Context 偏高 → 3 筆，有紅色所以徽章是紅的
  assert '<b class="badge" data-tone="red">3</b>' in dom
  # 人工介入：待核准的 P0002 + 等你處理的 P0001 T2
  assert '<b class="badge">2</b>' in dom


@needsBrowser
def test_consoleShowsBusyAgentDetail(dashboard, tmp_path):
  dom = renderUntil(tmp_path, dashboard + "#console", lambda dom: "（Claude・寫程式）" in dom)
  assert "（Claude・寫程式）" in dom
  assert "P0001 T1 後端批次 API" in dom and "第 1/3 輪" in dom
  assert "「Astra 對話窗」還沒有接資料" in dom and "「系統資源監控」還沒有接資料" not in dom
  # 系統資源區塊換成系統監控模組的三項摘要，整塊連到完整頁
  summary = dom[dom.index('id="systemMonitorSummary"'):]
  assert summary.startswith('id="systemMonitorSummary"><a class="smon-summary" href="#system-monitor">')
  for key in ("cpu", "memory", "disk"):
    assert f'class="smon-sum-row" data-key="{key}"' in summary
  assert "75.0%" in summary and "25.0%" in summary and "server 主機" in summary


@needsBrowser
def test_systemMonitorPageMountsCardsAndCharts(dashboard, gitProject, tmp_path):
  # 換頁不用等資料，先看的區塊顯示與否沒有資料也成立；等監控卡片畫出來才算有資料
  dom = renderUntil(tmp_path, dashboard + "#system-monitor", lambda dom: 'class="smon-card"' in dom)
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


@needsBrowser
def test_topologyLinkRedirectsToSystemMonitor(dashboard, tmp_path, monkeypatch):
  addProbe(tmp_path, monkeypatch, "document.body.dataset.probe = JSON.stringify({ hash: location.hash, page: app.page });")
  dom = renderUntil(tmp_path, dashboard + "#topology", probeReady(lambda probe: probe.get("page")))
  assert readProbe(dom) == {"hash": "#system-monitor", "page": "system-monitor"}
  assert "hidden" not in sectionTag(dom, "system-monitor")
  # 左側導覽不再有空白的拓撲頁
  assert railLink(dom, "topology") is None and "拓撲" not in dom


@needsBrowser
def test_consoleSummaryOpensFullPage(dashboard, tmp_path, monkeypatch):
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
  dom = renderUntil(tmp_path, dashboard + "#console", probeReady(lambda probe: probe["cards"]))
  assert readProbe(dom) == {"focusable": True, "hash": "#system-monitor", "page": "system-monitor", "shown": True, "cards": 3}


@needsBrowser
def test_switchingPagesKeepsOtherPagesAndStopsMonitorPolling(dashboard, tmp_path, monkeypatch):
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
  dom = renderUntil(tmp_path, dashboard + "#system-monitor", probeReady(lambda probe: probe["visits"]))
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


@needsBrowser
def test_narrowWindowStacksMonitorCardsWithoutOverflow(dashboard, tmp_path, monkeypatch):
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
  dom = renderUntil(tmp_path, dashboard + "#system-monitor", probeReady(lambda probe: probe["cardTops"]),
                    "--window-size=560,1000")
  probe = readProbe(dom)
  assert probe["width"] <= 760
  assert probe["documentOverflow"] <= 0 and probe["pageOverflow"] <= 0 and probe["contentOverflow"] <= 1
  # 三張卡和兩張走勢都改成直向排列：同一個左邊界，由上往下
  assert len(set(probe["cardLefts"])) == 1 and probe["cardTops"] == sorted(set(probe["cardTops"]))
  assert len(probe["cardTops"]) == 3
  assert len(set(probe["chartLefts"])) == 1 and probe["chartTops"] == sorted(set(probe["chartTops"]))
  assert len(probe["chartTops"]) == 2


@needsBrowser
def test_assistantGreetsAndOffersQuickAsks(dashboard, tmp_path):
  dom = renderUntil(tmp_path, dashboard, lambda dom: "你好，我是儀表板助手" in dom)
  assert "你好，我是儀表板助手" in dom
  assert 'data-ask="等你處理的有哪些？"' in dom and 'data-ask="writer 在做什麼？"' in dom


@needsBrowser
def test_modelsPageShowsRoleSettings(dashboard, tmp_path):
  # 換頁不用等資料；等角色目前的設定畫出來才算有資料
  dom = renderUntil(tmp_path, dashboard + "#models", lambda dom: "目前：Claude・sonnet" in dom)
  section = re.search(r'<section class="page models-page"[^>]*>', dom)
  assert section and "hidden" not in section.group(0)
  assert "目前：Claude・sonnet" in dom and "目前：Codex・預設模型" in dom
  assert 'data-role-model="writer" disabled' in dom
  selected = re.search(r'<option[^>]*value="sonnet"[^>]*>', dom)
  assert selected and "selected" in selected.group(0) and "disabled" in selected.group(0)
  # 沒帶 token 只能看
  assert 'data-save-role="writer" disabled' in dom


@needsBrowser
def test_modelsPageLoadsCliModelsAndEfforts(dashboard, tmp_path, monkeypatch):
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
  dom = renderUntil(tmp_path, dashboard + "#models", lambda dom: "CLI New Version" in dom)
  assert "CLI New Version" in dom and "full-model-v2" in dom
  assert 'data-role-effort="writer"' in dom and 'value="high"' in dom
  assert "重新整理模型" in dom
