"""用 Edge（或 Chrome）無頭模式載入儀表板，檢查畫面真的畫出 API 的資料。找不到瀏覽器就跳過。"""
import json
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

import board
import server
from board_core import appendEvent

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


def findBrowser():
  for name in ("msedge", "chrome", "chromium"):
    found = shutil.which(name)
    if found:
      return found
  return next((path for path in browserCandidates if Path(path).exists()), None)


browser = findBrowser()
nodePath = shutil.which("node")
needsBrowser = pytest.mark.skipif(browser is None, reason="找不到 Edge 或 Chrome，跳過網頁畫面測試")


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
  thread = threading.Thread(target=httpServer.serve_forever, daemon=True)
  thread.start()
  yield f"http://127.0.0.1:{httpServer.server_address[1]}/"
  httpServer.shutdown()
  httpServer.server_close()


def renderDom(tmp_path, url):
  # virtual-time-budget 讓頁面的計時器先跑幾輪輪詢，再把畫好的 DOM 印出來
  completed = subprocess.run(
    [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
     f"--user-data-dir={tmp_path / 'browser'}", "--virtual-time-budget=6000", "--dump-dom", url],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
  return completed.stdout


@pytest.mark.skipif(nodePath is None, reason="找不到 node，跳過 JavaScript 語法檢查")
def test_scriptsHaveValidSyntax():
  scripts = sorted((toolDir / "web" / "js").glob("*.js"))
  assert scripts
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


@needsBrowser
def test_overviewShowsConductorGroupsAndAgents(dashboard, tmp_path):
  dom = renderDom(tmp_path, dashboard + "#token=" + token)
  assert "conductor" in dom and "沒有啟動" in dom
  assert "進行中 1 份 plan" in dom and "P0001 批次上傳" in dom
  assert 'data-agent="writer-sub1"' in dom and 'data-agent="reviewer-sub1"' in dom
  assert 'data-agent="reviewer-sub2"' not in dom
  assert "Claude<span>（寫程式）</span>" in dom and "Codex<span>（審查）</span>" in dom
  assert "Write upload.py" in dom and "75%" in dom and "sonnet" in dom
  assert "唯讀" not in dom


@needsBrowser
def test_overviewShowsChosenCliPerRole(dashboard, gitProject, tmp_path):
  (gitProject / ".board" / "config.json").write_text(json.dumps({
    "maxParallel": 2, "writer": {"cli": "codex", "model": "gpt-6.1-sol"}, "reviewer": {"cli": "claude", "model": "opus"},
  }), encoding="utf-8")
  dom = renderDom(tmp_path, dashboard + "#token=" + token)
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
  assert "「Astra 對話窗」還沒有接資料" in dom and "「系統資源監控」還沒有接資料" in dom


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
  assert 'data-draft="role-model:writer" value="sonnet"' in dom
  assert '<option value="opus"></option>' in dom
  # 沒帶 token 只能看
  assert 'data-save-role="writer" disabled' in dom
