"""專案切換的網頁畫面：頂部顯示目前專案、「開啟資料夾」對話框、最近紀錄與整頁切換。找不到 Playwright 或瀏覽器就跳過。"""
import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import board
import board_projects
import server
from browser import browserPages, serverRequests, sharedBrowser  # noqa: F401  測試模組裡要看得到 fixture
from test_server import cleanRows, post, token
from test_server_projects import initialized
from test_web import fixedCollector, gib, recordRequests

waitMs = 15000
recentKey = "boardRecentProjects"


@pytest.fixture
def projects(tmp_path, writePlan):
  # 兩個專案各有一份標題不同的 plan，畫面上分得出讀的是哪一個
  first, second = initialized(tmp_path / "專案 A"), initialized(tmp_path / "專案 B")
  for projectDir in (first, second):
    writePlan("0001-ok.md", cleanRows, planId="P0001", title=f"{projectDir.name} 的 plan", projectDir=projectDir)
    assert board.cmdSubmit(projectDir, "P0001") == 0
  httpServer = server.makeServer(first, 0, token)
  httpServer.RequestHandlerClass.dashboard.systemMonitor.collector = fixedCollector(first)
  port = httpServer.server_address[1]
  url = f"http://127.0.0.1:{port}/"
  requests = recordRequests(httpServer)
  serverRequests[url] = requests
  thread = threading.Thread(target=httpServer.serve_forever, daemon=True)
  thread.start()
  yield SimpleNamespace(first=first, second=second, port=port, url=url, requests=requests, server=httpServer)
  httpServer.shutdown()
  httpServer.server_close()
  serverRequests.pop(url)


def idOf(projectDir):
  return board_projects.projectId(projectDir.resolve())


def shows(projectDir):
  # 頂部列顯示這個專案的完整路徑，而且狀態已經讀回來
  return (f"document.querySelector('#projectPath')?.textContent === {json.dumps(str(projectDir.resolve()))}"
          " && typeof app !== 'undefined' && app.state !== null")


def openPage(context, url, ready):
  page = context.new_page()
  page.goto(url, wait_until="commit")
  page.wait_for_function(ready, timeout=waitMs)
  return page


def unscopedApi(requests):
  # 開啟專案本來就不在專案範圍裡
  return [request["path"] for request in requests if request["path"].startswith("/api/")
          and not request["path"].startswith("/api/projects/")]


def monitorShows(text):
  return f"document.querySelector('#systemMonitorPage')?.textContent.includes({json.dumps(text)})"


def fillFolder(page, path):
  page.click("#projectOpen")
  page.fill("#projectPathInput", path)


def switchTo(page, path, projectDir):
  # 開啟成功是整頁重新載入，要等新頁面載入後再判斷畫面
  fillFolder(page, path)
  with page.expect_navigation(timeout=waitMs):
    page.click("#projectOpenSubmit")
  page.wait_for_function(shows(projectDir), timeout=waitMs)


def test_projectUrlScopesRequestsAndShowsNameAndPath(projects, browserPages):
  post(projects.port, "/api/projects/open", {"path": str(projects.second)})
  page = openPage(browserPages.newContext(), f"{projects.url}?project={idOf(projects.second)}#token={token}",
                  shows(projects.second))
  assert page.text_content("#projectName") == "專案 B"
  paths = [request["path"] for request in projects.requests]
  assert f"/api/projects/{idOf(projects.second)}/state" in paths
  assert unscopedApi(projects.requests) == []


def test_openFolderSwitchesWholePageAndRemembersProject(projects, browserPages):
  context = browserPages.newContext()
  page = openPage(context, f"{projects.url}#token={token}", shows(projects.first))
  assert page.text_content("#projectName") == "專案 A"
  before = len(projects.requests)
  switchTo(page, str(projects.second), projects.second)
  assert re.search(rf"/\?project={idOf(projects.second)}#overview$", page.url)
  assert page.text_content("#projectName") == "專案 B"
  # 最近紀錄存的是 server 回傳的正規化路徑
  assert page.evaluate(f"JSON.parse(localStorage.getItem('{recentKey}'))") == [
    {"id": idOf(projects.second), "name": "專案 B", "path": str(projects.second.resolve())}]
  # 任務、模型、控制台讀的都是 B
  page.evaluate("location.hash = 'tasks'")
  page.wait_for_function("document.documentElement.outerHTML.includes('專案 B 的 plan')", timeout=waitMs)
  assert "專案 A 的 plan" not in page.content()
  for key in ("models", "console"):
    page.evaluate(f"location.hash = '{key}'")
    page.wait_for_function(f"app.page === '{key}'", timeout=waitMs)
  page.wait_for_timeout(500)
  assert unscopedApi(projects.requests[before:]) == []
  # 重新整理仍是 B，token 也還在
  page.reload(wait_until="commit")
  page.wait_for_function(shows(projects.second), timeout=waitMs)
  assert page.is_visible("#projectOpen")
  # 另一個分頁仍可看 A
  other = openPage(context, f"{projects.url}#token={token}", shows(projects.first))
  assert other.text_content("#projectName") == "專案 A"


def test_systemMonitorAndTerminalPanelFollowPageProject(projects, browserPages):
  # B 的磁碟替身用了 90%，A 是 25%：畫面上分得出讀的是哪一個專案的採集器
  second = projects.server.RequestHandlerClass.registry.open(str(projects.second))
  diskPaths = []
  collector = fixedCollector(projects.second)
  collector.readDisk = lambda path: diskPaths.append(Path(path)) or (500 * gib, 450 * gib, 50 * gib)
  second.dashboard.systemMonitor.collector = collector
  page = openPage(browserPages.newContext(), f"{projects.url}#token={token}", shows(projects.first))
  assert page.text_content("#terminalProject") == "專案 A"
  assert page.get_attribute("#terminalProject", "title") == str(projects.first.resolve())
  page.evaluate("location.hash = 'system-monitor'")
  page.wait_for_function(monitorShows("25.0%"), timeout=waitMs)
  switchTo(page, str(projects.second), projects.second)
  # A 頁在預設專案、網址本來就不帶專案範圍，從 B 頁載入後才開始檢查
  before = len(projects.requests)
  assert page.text_content("#terminalProject") == "專案 B"
  page.evaluate("location.hash = 'system-monitor'")
  page.wait_for_function(monitorShows("90.0%"), timeout=waitMs)
  # 新頁從空的快取開始，A 的數值不會混進來
  assert "25.0%" not in page.text_content("#systemMonitorPage")
  assert diskPaths and set(diskPaths) == {projects.second.resolve()}
  assert f"/api/projects/{idOf(projects.second)}/system-monitor" in [request["path"] for request in projects.requests]
  assert unscopedApi(projects.requests[before:]) == []


def test_failedOpenKeepsCurrentProject(projects, browserPages, tmp_path):
  page = openPage(browserPages.newContext(), f"{projects.url}#token={token}", shows(projects.first))
  startUrl = page.url
  fillFolder(page, str(tmp_path / "不存在"))
  page.click("#projectOpenSubmit")
  page.wait_for_function("document.querySelector('#projectOpenMessage').textContent !== ''", timeout=waitMs)
  assert page.url == startUrl and page.is_visible("#projectDialog")
  assert page.text_content("#projectName") == "專案 A"
  assert page.evaluate(f"localStorage.getItem('{recentKey}')") is None


def test_readOnlyPageShowsProjectButCannotOpen(projects, browserPages):
  page = openPage(browserPages.newContext(), projects.url, shows(projects.first))
  assert page.text_content("#projectName") == "專案 A"
  assert page.is_hidden("#projectOpen")


def test_sameNamedFoldersAreToldApartByFullPath(projects, browserPages, tmp_path):
  (tmp_path / "左").mkdir()
  (tmp_path / "右").mkdir()
  left, right = initialized(tmp_path / "左" / "同名"), initialized(tmp_path / "右" / "同名")
  context = browserPages.newContext()
  page = openPage(context, f"{projects.url}#token={token}", shows(projects.first))
  switchTo(page, str(left), left)
  switchTo(page, str(right), right)
  page.click("#projectOpen")
  recent = page.locator("#projectRecent button")
  assert recent.count() == 2
  assert [recent.nth(index).inner_text().split("\n") for index in range(2)] == [
    ["同名", str(right.resolve())], ["同名", str(left.resolve())]]


def test_unavailableRecentStillOpensTypedPath(projects, browserPages):
  context = browserPages.newContext()
  # 瀏覽器不給用 localStorage（例如隱私設定），讀寫都會丟例外
  context.add_init_script(
    "Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('denied', 'SecurityError'); } });")
  page = openPage(context, f"{projects.url}#token={token}", shows(projects.first))
  page.click("#projectOpen")
  assert "讀不到最近紀錄" in page.text_content("#projectRecent")
  page.click("#projectCancel")
  switchTo(page, str(projects.second), projects.second)


def test_unregisteredProjectOffersReturnAndReopen(projects, browserPages):
  # server 重新啟動後，舊網址的 id 還沒登錄；最近紀錄裡有它的路徑
  context = browserPages.newContext()
  page = openPage(context, f"{projects.url}#token={token}", shows(projects.first))
  record = {"id": idOf(projects.second), "name": "專案 B", "path": str(projects.second.resolve())}
  page.evaluate(f"localStorage.setItem('{recentKey}', {json.dumps(json.dumps([record]))})")
  page.goto(f"{projects.url}?project={idOf(projects.second)}#overview", wait_until="commit")
  page.wait_for_function("document.querySelector('[data-project-reopen]') !== null", timeout=waitMs)
  assert page.get_attribute("text=返回預設專案", "href") == "/#overview"
  page.click("[data-project-reopen]")
  assert page.input_value("#projectPathInput") == str(projects.second.resolve())
  # 網址和現在相同，要真的重新載入才會讀到剛登錄的專案
  with page.expect_navigation(timeout=waitMs):
    page.click("#projectRecent button")
  page.wait_for_function(shows(projects.second), timeout=waitMs)
  assert page.is_hidden("[data-project-reopen]")


def test_readOnlyUnregisteredProjectCannotReopen(projects, browserPages):
  page = openPage(browserPages.newContext(), f"{projects.url}?project={'0' * 64}#overview",
                  "document.querySelector('[data-project-reopen]') !== null")
  assert page.is_disabled("[data-project-reopen]")
  assert page.is_hidden("#projectOpen")
