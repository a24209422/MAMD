import threading

import pytest

import board
import board_core
import board_projects
from conftest import git
from test_server import call, cleanRows, eventTypes, post, serving


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
