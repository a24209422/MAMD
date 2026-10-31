import json
import subprocess
import sys
import threading

import pytest

import board
import board_core
import board_projects
from conftest import git
from modules.terminal.service import TerminalService
from test_server import call, cleanRows, eventTypes, post, serving
from test_terminal import FakePty


def initialized(projectDir):
  projectDir.mkdir(exist_ok=True)
  git(projectDir, "init", "-q", "-b", "main")
  git(projectDir, "config", "user.name", "tester")
  git(projectDir, "config", "user.email", "tester@example.com")
  (projectDir / "README.md").write_text("demo\n", encoding="utf-8")
  git(projectDir, "add", ".")
  git(projectDir, "commit", "-q", "-m", "init")
  board_core.writeUserConfig(projectDir, {"skipTests": True})
  return projectDir


@pytest.fixture
def twoProjects(tmp_path):
  first, second = initialized(tmp_path / "專案 A"), initialized(tmp_path / "專案 B")
  with serving(first) as httpServer:
    yield first, second, httpServer.server_address[1]


def scoped(projectDir, path):
  return f"/api/projects/{board_projects.projectId(projectDir.resolve())}{path}"


def listedIds(port):
  return [project["id"] for project in post(port, "/api/projects/list")[1]["projects"]]


def test_openAndListNeedToken(twoProjects):
  first, second, port = twoProjects
  for path in ("/api/projects/open", "/api/projects/list"):
    assert call(port, "POST", path, {"path": str(second)})[0] == 403
    assert post(port, path, {"path": str(second)}, withToken="wrong")[0] == 403
  assert listedIds(port) == [board_projects.projectId(first.resolve())]
  status, opened = post(port, "/api/projects/open", {"path": str(second)})
  assert status == 200 and opened["ok"] is True
  assert opened["project"] == {"id": board_projects.projectId(second.resolve()), "name": "專案 B",
                               "path": str(second.resolve()), "ready": True, "problems": []}
  _, listed = post(port, "/api/projects/list")
  assert listed["defaultId"] == board_projects.projectId(first.resolve())
  assert [project["name"] for project in listed["projects"]] == ["專案 A", "專案 B"]
  assert listed["projects"][1] == opened["project"]


def test_openRejectsInvalidPath(twoProjects, tmp_path):
  _, _, port = twoProjects
  for path in ("專案 B", str(tmp_path / "missing"), 3):
    status, payload = post(port, "/api/projects/open", {"path": path})
    assert status == 400 and payload["ok"] is False and payload["message"]
  assert len(listedIds(port)) == 1


def test_scopedRoutesOnlyAffectChosenProject(twoProjects, writePlan):
  first, second, port = twoProjects
  for projectDir in (first, second):
    writePlan("0001-ok.md", cleanRows, planId="P0001", title=f"{projectDir.name} 的 plan", projectDir=projectDir)
    assert board.cmdSubmit(projectDir, "P0001") == 0
  post(port, "/api/projects/open", {"path": str(second)})
  assert call(port, "GET", scoped(first, "/plans/P0001"))[1]["title"] == "專案 A 的 plan"
  assert call(port, "GET", scoped(second, "/plans/P0001"))[1]["title"] == "專案 B 的 plan"
  assert call(port, "GET", scoped(second, "/state"))[1]["project"] == str(second.resolve())
  _, rejected = post(port, scoped(second, "/plans/P0001/reject"), {"reason": "只退 B"})
  assert rejected["ok"] is True
  assert eventTypes(second)[-1] == "planRejected" and "planRejected" not in eventTypes(first)
  # 舊 API 固定是啟動時的預設專案
  assert call(port, "GET", "/api/plans/P0001")[1]["title"] == "專案 A 的 plan"
  assert call(port, "GET", "/api/state")[1]["project"] == str(first.resolve())
  assert post(port, "/api/stop")[1]["ok"] is True
  assert eventTypes(first)[-1] == "paused" and "paused" not in eventTypes(second)


def test_unknownProjectIdIs404(twoProjects):
  first, _, port = twoProjects
  unknown = "/api/projects/" + "0" * 64
  assert call(port, "GET", unknown + "/state")[0] == 404
  assert post(port, unknown + "/stop")[0] == 404
  # 未登錄的 id 一樣先驗 token
  assert post(port, unknown + "/stop", withToken="wrong")[0] == 403
  assert call(port, "GET", "/api/projects/open/state")[0] == 404
  # 專案範圍裡不能再開啟或列出專案，也不能再套一層專案
  assert post(port, scoped(first, "/projects/list"))[0] == 404
  assert call(port, "GET", scoped(first, scoped(first, "/state")[4:]))[0] == 404
  assert call(port, "GET", scoped(first, "/../state"))[0] == 404
  assert "paused" not in eventTypes(first)


def test_pendingRequestStaysOnItsProject(twoProjects, monkeypatch):
  first, second, port = twoProjects
  started, release, results = threading.Event(), threading.Event(), {}
  realStop = board.cmdStop

  def slowStop(projectDir):
    started.set()
    release.wait(10)
    return realStop(projectDir)

  monkeypatch.setattr(board, "cmdStop", slowStop)
  thread = threading.Thread(target=lambda: results.update(stop=post(port, scoped(first, "/stop"))))
  thread.start()
  assert started.wait(10)
  assert post(port, "/api/projects/open", {"path": str(second)})[0] == 200
  release.set()
  thread.join(10)
  assert results["stop"][1]["ok"] is True
  assert eventTypes(first) == ["paused"] and eventTypes(second) == []


def test_eachProjectShowsItsOwnAlfredAndSwitchingLeavesItAlone(tmp_path):
  first, second = initialized(tmp_path / "專案 A"), initialized(tmp_path / "專案 B")
  # A 的 Alfred 用一個還活著的程序代替；B 沒有啟動
  alfred = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
  try:
    lockPath = first / ".board" / "conductor.lock"
    lockPath.write_text(str(alfred.pid), encoding="utf-8")
    eventsBefore = eventTypes(first)
    with serving(first) as httpServer:
      port = httpServer.server_address[1]
      post(port, "/api/projects/open", {"path": str(second)})
      assert call(port, "GET", scoped(first, "/state"))[1]["conductor"] == {"running": True, "pid": alfred.pid}
      assert call(port, "GET", scoped(second, "/state"))[1]["conductor"] == {"running": False, "pid": None}
    # 開啟、切換和關閉 server 都不碰 Alfred 的程序與任務紀錄
    assert alfred.poll() is None
    assert lockPath.read_text(encoding="utf-8") == str(alfred.pid)
    assert eventTypes(first) == eventsBefore
  finally:
    alfred.kill()
    alfred.wait()


class FakeTerminal:
  def __init__(self):
    self.calls, self.closed = [], False

  def request(self, action, body, httpPort):
    self.calls.append((action, body))
    return 200, {"ok": True, "sessions": []}

  def close(self):
    self.closed = True


def test_uninitializedFolderAllowsTerminalButRejectsWrites(tmp_path):
  default, plain = initialized(tmp_path / "專案 A"), tmp_path / "空資料夾"
  plain.mkdir()
  with serving(default) as httpServer:
    port = httpServer.server_address[1]
    _, opened = post(port, "/api/projects/open", {"path": str(plain)})
    project = opened["project"]
    assert project["ready"] is False and len(project["problems"]) == 2
    terminal = FakeTerminal()
    httpServer.RequestHandlerClass.registry.get(project["id"]).dashboard.terminal = terminal
    assert post(port, scoped(plain, "/terminals/list"))[1] == {"ok": True, "sessions": []}
    assert terminal.calls == [("list", {})]
    assert call(port, "GET", scoped(plain, "/state"))[0] == 200
    for path, body in (("/stop", {}), ("/resume", {}), ("/plans/P0001/approve", {}),
                       ("/roles/writer", {"cli": "codex"})):
      status, refused = post(port, scoped(plain, path), body)
      assert status == 409 and refused["ok"] is False and refused["problems"] == project["problems"]
      assert "不是 git repo" in refused["message"]
    # 不替使用者 init、寫設定或啟動 Alfred
    assert list(plain.iterdir()) == []
    # 使用者自己在這個資料夾初始化後，不用重新開啟就能操作
    initialized(plain)
    assert post(port, scoped(plain, "/stop"))[1]["ok"] is True


class ProjectPty(FakePty):
  # 記下 shell 從哪個資料夾啟動
  def __init__(self, project):
    super().__init__(project)
    self.project = project


def test_terminalsStayInTheirOwnProject(tmp_path):
  pytest.importorskip("websockets")
  from websockets.exceptions import ConnectionClosed
  from websockets.sync.client import connect
  first, second = initialized(tmp_path / "專案 A"), initialized(tmp_path / "專案 B")
  with serving(first) as httpServer:
    port = httpServer.server_address[1]
    post(port, "/api/projects/open", {"path": str(second)})
    registry = httpServer.RequestHandlerClass.registry
    for project in registry.all():
      project.dashboard.terminal = TerminalService(project.path, ProjectPty)
    terminalOf = {projectDir: registry.get(board_projects.projectId(projectDir.resolve())).dashboard.terminal
                  for projectDir in (first, second)}
    firstId = post(port, scoped(first, "/terminals/create"))[1]["session"]["id"]
    assert post(port, scoped(second, "/terminals/list"))[1]["sessions"] == []
    # B 的範圍裡拿 A 的 id 連不上也關不掉
    for action in ("connect", "close"):
      status, refused = post(port, scoped(second, f"/terminals/{action}"), {"id": firstId})
      assert status == 400 and refused["ok"] is False
    # A 的票證拿到 B 的連線位址也不能用
    ticket = post(port, scoped(first, "/terminals/connect"), {"id": firstId})[1]
    secondUrl = f"ws://127.0.0.1:{terminalOf[second].port}/"
    assert secondUrl != ticket["url"]
    with connect(secondUrl, origin=f"http://127.0.0.1:{port}", proxy=None) as socket:
      socket.send(json.dumps({"ticket": ticket["ticket"]}))
      with pytest.raises(ConnectionClosed):
        socket.recv(timeout=3)
    # 8 個分頁的上限各專案分開算
    for _ in range(8):
      assert post(port, scoped(second, "/terminals/create"))[1]["ok"] is True
    status, full = post(port, scoped(second, "/terminals/create"))
    assert status == 400 and "上限" in full["message"]
    assert post(port, scoped(first, "/terminals/create"))[1]["ok"] is True
    firstSessions = post(port, scoped(first, "/terminals/list"))[1]["sessions"]
    assert len(firstSessions) == 2 and firstSessions[0] == {"id": firstId, "title": "PowerShell 1", "running": True}
    # 新 shell 從所屬專案的資料夾啟動
    ptys = {projectDir: [item.pty for item in terminalOf[projectDir].manager.items.values()] for projectDir in (first, second)}
    assert {pty.project for pty in ptys[first]} == {first.resolve()}
    assert {pty.project for pty in ptys[second]} == {second.resolve()}
  # server 關閉時兩個專案的 shell 都收掉
  assert all(pty.closed for projectPtys in ptys.values() for pty in projectPtys)


def test_closingServerClosesEveryProjectTerminal(tmp_path):
  default, other = initialized(tmp_path / "專案 A"), initialized(tmp_path / "專案 B")
  terminals = []
  with serving(default) as httpServer:
    port = httpServer.server_address[1]
    post(port, "/api/projects/open", {"path": str(other)})
    for project in httpServer.RequestHandlerClass.registry.all():
      project.dashboard.terminal = FakeTerminal()
      terminals.append(project.dashboard.terminal)
  assert len(terminals) == 2 and all(terminal.closed for terminal in terminals)
