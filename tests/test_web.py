"""用 Edge（或 Chrome）無頭模式載入儀表板，檢查畫面真的畫出 API 的資料。找不到瀏覽器就跳過。"""
import itertools
import json
import re
import shutil
import subprocess
import threading
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
  thread = threading.Thread(target=httpServer.serve_forever, daemon=True)
  thread.start()
  yield f"http://127.0.0.1:{httpServer.server_address[1]}/"
  httpServer.shutdown()
  httpServer.server_close()


def renderDom(tmp_path, url, *extraArgs):
  # virtual-time-budget 讓頁面的計時器先跑幾輪輪詢，再把畫好的 DOM 印出來
  completed = subprocess.run(
    [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
     f"--user-data-dir={tmp_path / 'browser'}", "--virtual-time-budget=6000", *extraArgs, "--dump-dom", url],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
  return completed.stdout


def addProbe(tmp_path, monkeypatch, script):
  # 只在測試伺服器的 main.js 後面接一段操作與量測；結果寫進 body 的 data-probe，dump-dom 才看得到
  # 包在區塊裡，量測用的變數不會和儀表板的全域名稱撞到
  main = tmp_path / "main.js"
  main.write_text((toolDir / "web/js/main.js").read_text(encoding="utf-8") + "\n{\n" + script + "\n}\n", encoding="utf-8")
  monkeypatch.setitem(server.staticFiles, "/js/main.js", (str(main), "text/javascript; charset=utf-8"))


def readProbe(dom):
  match = re.search(r'data-probe="([^"]*)"', dom)
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
  dom = renderDom(tmp_path, dashboard, "--window-size=1440,1200")
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
  probe = readProbe(renderDom(tmp_path, dashboard))
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
  probe = readProbe(renderDom(tmp_path, dashboard))
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
  dom = renderDom(tmp_path, dashboard + "#token=" + token,
                  f"--window-size={width},1200", f"--screenshot={screenshot}")
  probe = readProbe(dom)
  assert probe["total"] == 6
  assert probe["overflow"] <= 0 and probe["right"] <= probe["viewport"]
  print(f"Overview screenshot: {screenshot}")


@needsBrowser
def test_overviewShowsConductorGroupsAndAgents(dashboard, tmp_path):
  dom = renderDom(tmp_path, dashboard + "#token=" + token)
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
  dom = renderDom(tmp_path, dashboard + "#token=" + token)
  lead = re.search(r'id="leadCard".*?</article>', dom, re.S)
  assert lead and "測試中：P0001 T1" in lead.group(0)
  appendEvent(gitProject, "testRun", plan="P0001", task="T1", roundNumber=1,
              data={"passed": True, "tail": "ok", "verdict": "passed", "newFailures": []})
  assert "測試中：" not in renderDom(tmp_path, dashboard + "#token=" + token)


@needsBrowser
def test_overviewShowsRunningCliUntilMainTaskEnds(dashboard, gitProject, tmp_path):
  (gitProject / ".board" / "config.json").write_text(json.dumps({
    "maxParallel": 2, "writer": {"cli": "codex", "model": "gpt-6.1-sol"}, "reviewer": {"cli": "claude", "model": "opus"},
  }), encoding="utf-8")
  dom = renderDom(tmp_path, dashboard + "#token=" + token)
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
  dom = renderDom(tmp_path, dashboard)
  assert "Codex<span>（寫程式）</span>" in dom and "Claude<span>（審查）</span>" in dom
  assert "gpt-6.1-sol" in dom and "opus" in dom


@needsBrowser
def test_withoutTokenPageIsReadOnly(dashboard, tmp_path):
  assert "唯讀：請用 server.py 印出的網址重新開啟" in renderDom(tmp_path, dashboard)


@needsBrowser
def test_tasksPageDrawsTreeFromServer(dashboard, tmp_path):
  dom = renderDom(tmp_path, dashboard + "#tasks")
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
  probe = readProbe(renderDom(tmp_path, dashboard + "#tasks"))
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
  dom = renderDom(tmp_path, dashboard + "#token=" + token)
  assert "開始寫程式" in dom
  # 異常：等你處理（紅）、conductor 沒在跑、Context 偏高 → 3 筆，有紅色所以徽章是紅的
  assert '<b class="badge" data-tone="red">3</b>' in dom
  # 人工介入：待核准的 P0002 + 等你處理的 P0001 T2
  assert '<b class="badge">2</b>' in dom


@needsBrowser
def test_consoleShowsBusyAgentDetail(dashboard, tmp_path):
  dom = renderDom(tmp_path, dashboard + "#console")
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
  dom = renderDom(tmp_path, dashboard + "#system-monitor")
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
  dom = renderDom(tmp_path, dashboard + "#topology")
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
  dom = renderDom(tmp_path, dashboard + "#console")
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
  dom = renderDom(tmp_path, dashboard + "#system-monitor")
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
  dom = renderDom(tmp_path, dashboard + "#system-monitor", "--window-size=560,1000")
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
  dom = renderDom(tmp_path, dashboard)
  assert "你好，我是儀表板助手" in dom
  assert 'data-ask="等你處理的有哪些？"' in dom and 'data-ask="writer 在做什麼？"' in dom


@needsBrowser
def test_modelsPageShowsRoleSettings(dashboard, tmp_path):
  dom = renderDom(tmp_path, dashboard + "#models")
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
  dom = renderDom(tmp_path, dashboard + "#models")
  assert "CLI New Version" in dom and "full-model-v2" in dom
  assert 'data-role-effort="writer"' in dom and 'value="high"' in dom
  assert "重新整理模型" in dom
