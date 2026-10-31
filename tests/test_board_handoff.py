"""兩台電腦交接：電腦 1 匯出並 push，電腦 2 clone／pull 後 init、status、看板與 Alfred 讀到同一份進度。"""
import json
import sys

import pytest

import board
import board_api
from board_core import PlanStatus, TaskStatus, readEvents
from board_setup.project import Options, initialize
from board_sync import checkpointPath, progressPath, readCheckpoint, readSnapshot
from conductor import Conductor
from conftest import git
from helpers import fakeConfig
from test_board_export import exportState, holdAlfredLock, legacyProject, pushListed, section  # noqa: F401
from test_board_setup import toolRoot
from test_board_sync_load import started, writeEvents


def statusOutput(projectDir, capsys):
  capsys.readouterr()
  assert board.cmdStatus(projectDir) == 0
  return capsys.readouterr().out


def planLines(output):
  # 交接提示與同步說明另外驗，這裡只比 plan 與任務的狀態行
  return [line for line in output.splitlines() if line.startswith(("P0", "  T"))]


def publish(projectDir, capsys, remote, first=False):
  # 照 export-state 列出的檔案 commit，再 push 主分支與列出的分支
  capsys.readouterr()
  assert exportState(projectDir) == 0
  output = capsys.readouterr().out
  git(projectDir, "add", "--", *section(output, "請 commit 這些檔案："))
  git(projectDir, "commit", "-q", "-m", "交接進度")
  if first:
    pushListed(projectDir, output, remote)
  else:
    refs = ["HEAD:refs/heads/main"] + [f"refs/heads/{line.split()[0]}" for line in section(output, "請 push 這些分支：")]
    git(projectDir, "push", "-q", "-f", str(remote), *refs)
  return readSnapshot(projectDir)


@pytest.fixture
def computers(legacyProject, tmp_path, capsys):
  remote = tmp_path / "remote.git"
  publish(legacyProject, capsys, remote, first=True)
  clone = tmp_path / "電腦2" / "另一個路徑"
  clone.parent.mkdir()
  git(tmp_path, "clone", "-q", "-b", "main", str(remote), str(clone))
  git(clone, "config", "user.name", "tester")
  git(clone, "config", "user.email", "tester@example.com")
  return legacyProject, clone, remote


def test_firstCloneInitRestoresSameStatus(computers, capsys):
  source, clone, _ = computers
  # 草稿只在寫 plan 的那台電腦，本來就不進交接檔
  expected = [line for line in planLines(statusOutput(source, capsys)) if not line.endswith(f"｜{PlanStatus.draft}")]

  assert initialize(clone, Options(skipTests=True), toolRoot) == 0

  assert "已接納交接進度" in capsys.readouterr().out
  assert planLines(statusOutput(clone, capsys)) == expected
  assert any(f"｜{PlanStatus.active}" in line for line in expected)


def test_laterPullUpdatesStatusAndBoardWithoutInit(computers, capsys):
  source, clone, remote = computers
  assert initialize(clone, Options(skipTests=True), toolRoot) == 0
  before = board_api.stateView(clone)

  writeEvents(source, [started("T1", roundNumber=2)])
  updated = publish(source, capsys, remote)
  git(clone, "pull", "-q", "--ff-only")

  assert "  T1 寫程式中（第 2/3 輪）" in planLines(statusOutput(clone, capsys))
  # 已開著的頁面輪詢 /api/state 時內容變了就重畫，新開的頁面也讀同一份
  after = board_api.stateView(clone)
  assert after != before
  assert after["sync"]["baseRevision"] == updated["revision"]
  statuses = {(task["plan"], task["task"]): task["status"] for task in after["tasks"]}
  assert statuses["P0001", "T1"] == TaskStatus.writing


def localConfig(projectDir):
  return json.loads((projectDir / ".board" / "config.json").read_text(encoding="utf-8"))


def test_reinitAndPullKeepProgressFileAndLocalSettings(computers, capsys):
  source, clone, remote = computers
  local = {"writer": {"cli": "codex", "model": "本機模型"}, "maxParallel": 1}
  (clone / ".board").mkdir(exist_ok=True)
  (clone / ".board" / "config.json").write_text(json.dumps(local, ensure_ascii=False), encoding="utf-8")
  (clone / "src").mkdir()
  (clone / "src" / "app.py").write_text("answer = 42\n", encoding="utf-8")
  (clone / "tests").mkdir()
  options = Options(python=sys.executable)
  assert initialize(clone, options, toolRoot) == 0
  settled = localConfig(clone)
  progress, checkpoint = progressPath(clone).read_bytes(), checkpointPath(clone).read_bytes()

  assert initialize(clone, options, toolRoot) == 0
  assert progressPath(clone).read_bytes() == progress
  assert checkpointPath(clone).read_bytes() == checkpoint

  writeEvents(source, [started("T1", roundNumber=2)])
  publish(source, capsys, remote)
  git(clone, "pull", "-q", "--ff-only")
  statusOutput(clone, capsys)
  board_api.stateView(clone)

  assert localConfig(clone) == settled
  assert settled["writer"] == local["writer"] and settled["maxParallel"] == 1
  assert settled["pythonCommand"]


@pytest.fixture
def pulledWhileAlfredRuns(computers, capsys):
  # 電腦 2 接過一次後開著 Alfred，這時才 pull 到電腦 1 的新交接檔
  source, clone, remote = computers
  assert initialize(clone, Options(skipTests=True), toolRoot) == 0
  holdAlfredLock(clone)
  writeEvents(source, [started("T1", roundNumber=2)])
  updated = publish(source, capsys, remote)
  git(clone, "pull", "-q", "--ff-only")
  return clone, updated


def test_alfredRunningKeepsOldProgressAndExplains(pulledWhileAlfredRuns, capsys):
  clone, updated = pulledWhileAlfredRuns
  checkpoint = checkpointPath(clone).read_bytes()

  output = statusOutput(clone, capsys)
  view = board_api.stateView(clone)

  assert "  T1 排隊中（第 2/3 輪）" in planLines(output)
  assert "請停止 Alfred" in output
  assert view["sync"]["status"] == "waiting" and view["sync"]["incomingRevision"] == updated["revision"]
  assert any("請停止 Alfred" in warning for warning in view["warnings"])
  assert checkpointPath(clone).read_bytes() == checkpoint


def test_reinitWhileAlfredRunsDoesNotSwitchBase(pulledWhileAlfredRuns, capsys):
  clone, _ = pulledWhileAlfredRuns
  checkpoint = checkpointPath(clone).read_bytes()
  capsys.readouterr()

  assert initialize(clone, Options(skipTests=True), toolRoot) == 0

  assert "請停止 Alfred" in capsys.readouterr().out
  assert checkpointPath(clone).read_bytes() == checkpoint


def test_alfredAcceptsAtStartupButHoldsHandoffArrivingWhileRunning(computers, capsys, monkeypatch):
  source, clone, remote = computers
  messages, launched = [], []
  alfred = Conductor(clone, config=fakeConfig(), out=messages.append)
  monkeypatch.setattr(alfred, "startWrite", lambda plan, taskState, lane: launched.append(taskState["task"]))

  plans, state = alfred.currentState()
  assert readCheckpoint(clone)["base"]["revision"] == readSnapshot(clone)["revision"]
  alfred.startWrites(plans, state)
  assert "T1" in launched

  launched.clear()
  writeEvents(source, [started("T1", roundNumber=2)])
  updated = publish(source, capsys, remote)
  git(clone, "pull", "-q", "--ff-only")
  checkpoint = checkpointPath(clone).read_bytes()
  alfred.currentState()
  plans, state = alfred.currentState()
  alfred.startWrites(plans, state)

  assert state["tasks"]["P0001/T1"]["status"] == TaskStatus.queued
  assert launched == []
  assert checkpointPath(clone).read_bytes() == checkpoint
  assert readSnapshot(clone)["revision"] == updated["revision"]
  assert sum("請停止 Alfred" in message for message in messages) == 1


def test_stoppingAlfredAcceptsWaitingProgress(pulledWhileAlfredRuns, capsys):
  clone, updated = pulledWhileAlfredRuns
  (clone / ".board" / "conductor.lock").unlink()

  assert "  T1 寫程式中（第 2/3 輪）" in planLines(statusOutput(clone, capsys))
  assert board_api.stateView(clone)["sync"]["baseRevision"] == updated["revision"]
