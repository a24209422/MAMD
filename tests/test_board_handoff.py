"""兩台電腦交接：電腦 1 匯出並 push，電腦 2 clone／pull 後 init、status、看板與 Alfred 讀到同一份進度。"""
import json
import sys
from pathlib import Path

import pytest

import board
import board_agents
import board_api
import board_git
import conductor as conductorModule
from board_core import PlanStatus, TaskStatus, deriveState, loadConfig, loadPlans, readEvents, tasksWithStatus
from board_setup.project import Options, initialize
from board_sync import checkpointPath, progressPath, readCheckpoint, readSnapshot
from conductor import Conductor
from conftest import git
from helpers import fakeConfig, makeEvent
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

  # 已開著的頁面輪詢 /api/state 時內容變了就重畫，新開的頁面也讀同一份；先讀 API，確認輪詢本身就會接納
  after = board_api.stateView(clone)
  assert "  T1 寫程式中（第 2/3 輪）" in planLines(statusOutput(clone, capsys))
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


def stubLaunches(alfred, monkeypatch, launched, recordFirst=False):
  # 不真的啟動 agent；recordFirst 時第一個派出的任務照實記下開始與寫完，讓本機在接納後有真實進度、又留下待審的任務
  def startWrite(plan, taskState, lane):
    if recordFirst and not launched:
      alfred.event("taskStarted", {**taskState, "agent": lane})
      alfred.event("taskWritten", taskState, commit="2" * 40, summary="完成")
    launched.append(("write", taskState["plan"], taskState["task"]))
  monkeypatch.setattr(alfred, "startWrite", startWrite)
  monkeypatch.setattr(alfred, "startReview", lambda plan, taskState, lane: launched.append(
    ("review", taskState["plan"], taskState["task"])))


def taskWrittenInSource(source):
  commit = board_git.runGit(source, "rev-parse", "refs/heads/task/P0001-T1")
  return [started("T1", roundNumber=2), makeEvent("taskWritten", plan="P0001", task="T1", commit=commit, summary="完成")]


def test_waitingHandoffAlsoHoldsPendingReviews(computers, capsys, monkeypatch):
  source, clone, remote = computers
  writeEvents(source, taskWrittenInSource(source))
  publish(source, capsys, remote)
  git(clone, "pull", "-q", "--ff-only")
  launched = []
  alfred = Conductor(clone, config=fakeConfig(), out=lambda message: None)
  stubLaunches(alfred, monkeypatch, launched)
  plans, state = alfred.currentState()
  alfred.startReviews(plans, state)
  assert ("review", "P0001", "T1") in launched

  launched.clear()
  writeEvents(source, [makeEvent("reviewStarted", plan="P0001", task="T1", roundNumber=2, agent="codex")])
  publish(source, capsys, remote)
  git(clone, "pull", "-q", "--ff-only")
  plans, state = alfred.currentState()
  alfred.startReviews(plans, state)
  alfred.startWrites(plans, state)

  assert alfred.sync["status"] == "waiting"
  assert state["tasks"]["P0001/T1"]["status"] == TaskStatus.toReview
  assert launched == []


def test_localProgressThenNewHandoffKeepsHoldingDispatch(computers, capsys, monkeypatch):
  source, clone, remote = computers
  messages, launched = [], []
  alfred = Conductor(clone, config=fakeConfig(maxParallel=1), out=messages.append)
  stubLaunches(alfred, monkeypatch, launched, recordFirst=True)
  plans, state = alfred.currentState()
  alfred.startWrites(plans, state)
  assert launched and [event["type"] for event in readEvents(clone)[-2:]] == ["taskStarted", "taskWritten"]

  launched.clear()
  writeEvents(source, [started("T1", roundNumber=2)])
  publish(source, capsys, remote)
  git(clone, "pull", "-q", "--ff-only")
  for _ in range(2):
    plans, state = alfred.currentState()
    assert tasksWithStatus(state, TaskStatus.toReview)
    alfred.startReviews(plans, state)
    alfred.startWrites(plans, state)

  assert alfred.sync["status"] == "diverged"
  assert launched == []
  assert sum("交接檔｜" in message for message in messages) == 1


def test_stoppingAlfredAcceptsWaitingProgress(pulledWhileAlfredRuns, capsys):
  clone, updated = pulledWhileAlfredRuns
  (clone / ".board" / "conductor.lock").unlink()

  assert "  T1 寫程式中（第 2/3 輪）" in planLines(statusOutput(clone, capsys))
  assert board_api.stateView(clone)["sync"]["baseRevision"] == updated["revision"]


def localSha(projectDir, branch):
  return board_git.runGit(projectDir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)


def test_alfredContinuesFromPushedBranchShasAtNewPath(computers):
  source, clone, _ = computers
  recorded = readSnapshot(clone)["branches"]
  alfred = Conductor(clone, config=fakeConfig(), out=lambda message: None)

  plans, state = alfred.currentState()
  alfred.ensurePlanWorktrees(state)

  assert alfred.sync["blockedPlans"] == {}
  for branch in ("plan/P0001", "task/P0001-T1", "task/P0001-T3", "plan/P0005"):
    assert localSha(clone, branch) == recorded[branch]
  worktree = board_git.planWorktreePath(clone, "P0001")
  assert board_git.headCommit(worktree) == recorded["plan/P0001"]
  assert clone.parent in worktree.parents and source not in worktree.parents


def test_missingPushedBranchShowsProgressButRefusesDispatch(computers, capsys, monkeypatch):
  source, clone, _ = computers
  expected = [line for line in planLines(statusOutput(source, capsys)) if not line.endswith(f"｜{PlanStatus.draft}")]
  git(clone, "update-ref", "-d", "refs/remotes/origin/task/P0001-T1")
  messages, launched = [], []
  alfred = Conductor(clone, config=fakeConfig(), out=messages.append)
  stubLaunches(alfred, monkeypatch, launched)

  plans, state = alfred.currentState()
  alfred.ensurePlanWorktrees(state)
  alfred.startReviews(plans, state)
  alfred.startWrites(plans, state)

  output = statusOutput(clone, capsys)
  assert planLines(output) == expected
  assert "P0001 不派工" in output and "task/P0001-T1" in output
  assert any("P0001 不派工" in warning for warning in board_api.stateView(clone)["warnings"])
  assert [entry for entry in launched if entry[1] == "P0001"] == []
  assert not board_git.planWorktreePath(clone, "P0001").exists()
  assert localSha(clone, "task/P0001-T1") == ""
  refusal = [message for message in messages if "P0001" in message and "task/P0001-T1" in message]
  assert refusal and "push" in refusal[0] and "git fetch" in refusal[0]
  # 其他分支都在的 plan 照常接續
  assert localSha(clone, "plan/P0005") == readSnapshot(clone)["branches"]["plan/P0005"]


def test_divergedLocalBranchIsKeptAndBlocksPlan(computers):
  _, clone, _ = computers
  git(clone, "branch", "task/P0001-T1", "main")
  diverged = localSha(clone, "task/P0001-T1")
  alfred = Conductor(clone, config=fakeConfig(), out=lambda message: None)

  _, state = alfred.currentState()
  alfred.ensurePlanWorktrees(state)

  assert "task/P0001-T1" in alfred.sync["blockedPlans"]["P0001"]
  assert localSha(clone, "task/P0001-T1") == diverged
  assert tasksWithStatus(state, TaskStatus.mergedUntested) == []


def test_finishedWorkKeepsItsStageWithoutDeletedTaskBranches(computers, capsys, monkeypatch):
  source, clone, _ = computers
  sourceState = deriveState(readEvents(source), loadPlans(source), loadConfig(source))
  sourceStatuses = {key: task["status"] for key, task in sourceState["tasks"].items()}
  jobs = []
  recordAgentJobs(monkeypatch, jobs)
  alfred = Conductor(clone, config=fakeConfig(), out=lambda message: None)

  plans, state = alfred.currentState()
  alfred.ensurePlanWorktrees(state)
  alfred.startReviews(plans, state)
  alfred.startWrites(plans, state)

  assert alfred.sync["blockedPlans"] == {}
  assert state["tasks"]["P0001/T3"]["status"] == sourceStatuses["P0001/T3"] == TaskStatus.mergedUntested
  assert state["tasks"]["P0005/T1"]["status"] == TaskStatus.done
  # 完成的 plan 只看交付分支上的成果，task 分支沒 push、這台電腦也沒有都照樣顯示完成
  for planIdValue in ("P0004", "P0006"):
    assert state["plans"][planIdValue]["status"] == PlanStatus.completed
    assert localSha(clone, f"task/{planIdValue}-T1") == ""
  def finishedLines(projectDir):
    return [line for line in planLines(statusOutput(projectDir, capsys)) if line.startswith(("P0004", "P0006"))]
  shown = finishedLines(clone)
  assert shown == finishedLines(source)
  assert len(shown) == 2 and all(f"｜{PlanStatus.completed}｜" in line for line in shown)
  restarted = {(event["plan"], event["task"]) for event in readEvents(clone) if event["type"] == "taskStarted"}
  assert restarted & {("P0001", "T3"), ("P0005", "T1"), ("P0004", "T1"), ("P0006", "T1")} == set()


def test_finishedPlanNotYetDeliveredNeedsItsPushedPlanBranch(computers, capsys):
  source, clone, _ = computers
  recorded = readSnapshot(clone)["branches"]
  expected = [line for line in planLines(statusOutput(source, capsys)) if line.startswith("P0006")]
  # 來源漏 push plan/P0006：P0006 已完成但還沒合併進 main，成果只在這個分支上
  git(clone, "update-ref", "-d", "refs/remotes/origin/plan/P0006")
  messages = []
  alfred = Conductor(clone, config=fakeConfig(), out=messages.append)

  alfred.currentState()

  output = statusOutput(clone, capsys)
  assert [line for line in planLines(output) if line.startswith("P0006")] == expected
  assert "P0006 不派工" in output and "plan/P0006" in output
  refusal = [message for message in messages if "P0006" in message and "plan/P0006" in message]
  assert refusal and "push" in refusal[0] and "git fetch" in refusal[0]
  assert localSha(clone, "plan/P0006") == ""

  # 補 push 後在這台電腦 fetch，就照記錄的 SHA 建回分支、不再阻擋
  git(clone, "fetch", "-q", "origin")
  _, state = alfred.currentState()

  assert "P0006" not in alfred.sync["blockedPlans"]
  assert state["plans"]["P0006"]["status"] == PlanStatus.completed
  assert localSha(clone, "plan/P0006") == recorded["plan/P0006"]


def recordTestJobs(monkeypatch, recorded):
  # 只記下要在哪個工作目錄跑測試，不真的跑
  monkeypatch.setattr(conductorModule, "BackgroundTestJob", lambda runTestCommand, worktree: type(
    "RecordedTest", (), {"start": lambda self: recorded.append(Path(worktree)) or self})())


def test_blockedPlanKeepsMergedUntestedWhenTestsAreOff(computers, monkeypatch):
  _, clone, _ = computers
  git(clone, "branch", "task/P0001-T1", "main")
  recordAgentJobs(monkeypatch, [])
  alfred = Conductor(clone, config=fakeConfig(), out=lambda message: None)

  alfred.tick()

  # 關掉測試時合併後測試會被略過、任務直接完成；交接被擋的 plan 不能因此跳過原本的測試
  assert [event for event in readEvents(clone) if event["type"] == "mergeTestSkipped"] == []
  assert alfred.currentState()[1]["tasks"]["P0001/T3"]["status"] == TaskStatus.mergedUntested


def test_blockedPlanDoesNotRunMergeTests(computers, monkeypatch):
  _, clone, _ = computers
  command = "python -c pass"
  git(clone, "branch", "task/P0001-T1", "main")
  tested = []
  messages = []
  recordTestJobs(monkeypatch, tested)
  alfred = Conductor(clone, config=fakeConfig(testCommand=command), out=messages.append)
  alfred.currentState()
  # 交接檔的基準不帶指令，這台電腦要先有自己的基準，合併後測試才會輪到
  writeEvents(clone, [makeEvent("testBaseline", plan="P0001", command=command, passed=True, failures=[])])

  _, state = alfred.currentState()
  alfred.ensurePlanWorktrees(state)
  assert not alfred.needsTestBaseline(state["plans"]["P0001"])
  alfred.startMergeTests(state)

  assert "P0001" in alfred.sync["blockedPlans"]
  assert tested == [] and not any(info["plan"] == "P0001" for info in alfred.jobs.values())
  # 被擋的 plan 根本不該嘗試合併後測試，連「無法開始」的通知都不該出現
  assert not any("合併後測試" in str(message) for message in messages)


def recordAgentJobs(monkeypatch, recorded):
  # 只記下要啟動的指令與工作目錄，不真的跑 agent
  class RecordedJob:
    def __init__(self, argv, cwd, stdinText, *args, **kwargs):
      recorded.append({"argv": [str(part) for part in argv], "cwd": Path(cwd), "stdin": stdinText or ""})

    def start(self):
      return self
  monkeypatch.setattr(board_agents, "AgentJob", RecordedJob)


def test_foreignWritingWaitsThenRestartsHereWithoutOldSession(computers, capsys, monkeypatch):
  source, clone, remote = computers
  sourceWorktree = board_git.taskWorktreePath(source, "P0001", "T1")
  writeEvents(source, [makeEvent("taskStarted", plan="P0001", task="T1", roundNumber=2, agent="claude",
                                 sessionId="來源對話", resume=False, worktree=str(sourceWorktree))])
  recorded = publish(source, capsys, remote)["branches"]
  git(clone, "pull", "-q", "--ff-only")

  lines = statusOutput(clone, capsys).splitlines()
  assert "等待接續" in lines[lines.index("  T1 寫程式中（第 2/3 輪）") + 1]
  waiting = {task["task"]: task["handoffWaiting"] for task in board_api.stateView(clone)["tasks"] if task["plan"] == "P0001"}
  assert waiting == {"T1": True, "T2": False, "T3": False}

  jobs = []
  recordAgentJobs(monkeypatch, jobs)
  alfred = Conductor(clone, config=fakeConfig(), out=lambda message: None)
  plans, state = alfred.currentState()
  alfred.recoverInterrupted(state)
  plans, state = alfred.currentState()
  alfred.ensurePlanWorktrees(state)
  alfred.startWrites(plans, state)

  startedHere = [event for event in readEvents(clone) if event["type"] == "taskStarted"][-1]
  assert (startedHere["task"], startedHere["round"]) == ("T1", 2)
  assert startedHere["data"]["resume"] is False and startedHere["data"]["sessionId"] != "來源對話"
  worktree = board_git.taskWorktreePath(clone, "P0001", "T1")
  assert [job["cwd"] for job in jobs] == [worktree]
  assert board_git.headCommit(worktree) == recorded["task/P0001-T1"]
  launchText = " ".join(jobs[0]["argv"]) + jobs[0]["stdin"]
  assert str(source) not in launchText and "來源對話" not in launchText
  assert "等待接續" not in statusOutput(clone, capsys)


def test_waitingReviewIsReviewedHereInRebuiltWorktree(computers, capsys, monkeypatch):
  source, clone, remote = computers
  writeEvents(source, [makeEvent("taskWritten", plan="P0001", task="T1", summary="完成",
                                 commit=localSha(source, "task/P0001-T1"))])
  recorded = publish(source, capsys, remote)["branches"]
  git(clone, "pull", "-q", "--ff-only")

  jobs = []
  recordAgentJobs(monkeypatch, jobs)
  alfred = Conductor(clone, config=fakeConfig(), out=lambda message: None)
  plans, state = alfred.currentState()
  waiting = tasksWithStatus(state, TaskStatus.toReview)
  assert [task["task"] for task in waiting] == ["T1"]
  alfred.ensurePlanWorktrees(state)
  alfred.startReviews(plans, state)

  # 審查在這台電腦的 task worktree 裡看交接記錄的成果，不是來源電腦的路徑
  worktree = board_git.taskWorktreePath(clone, "P0001", "T1")
  assert [job["cwd"] for job in jobs] == [worktree]
  assert board_git.headCommit(worktree) == recorded["task/P0001-T1"]
  assert str(source) not in " ".join(jobs[0]["argv"]) + jobs[0]["stdin"]
  reviewStarted = [event for event in readEvents(clone) if event["type"] == "reviewStarted"][-1]
  # 接手審查不消耗修正輪
  assert (reviewStarted["task"], reviewStarted["round"]) == ("T1", waiting[0]["round"])


def test_foreignTestingRerunsHereInRebuiltWorktree(computers, capsys, monkeypatch):
  source, clone, remote = computers
  command = "python -c pass"
  writeEvents(source, [makeEvent("testBaseline", plan="P0001", command=command, passed=True, failures=[]),
                       makeEvent("taskWritten", plan="P0001", task="T1", summary="完成", testing=True,
                                 commit=localSha(source, "task/P0001-T1"))])
  recorded = publish(source, capsys, remote)["branches"]
  git(clone, "pull", "-q", "--ff-only")
  assert "等待接續" in statusOutput(clone, capsys)

  testedIn = []
  monkeypatch.setattr(conductorModule, "BackgroundTestJob", lambda runTestCommand, worktree: type(
    "RecordedTest", (), {"start": lambda self: testedIn.append(Path(worktree)) or self})())
  alfred = Conductor(clone, config=fakeConfig(testCommand=command), out=lambda message: None)
  _, state = alfred.currentState()
  # 另一台電腦的測試基準只當歷史，這台電腦要先用自己的環境重跑基準
  assert alfred.needsTestBaseline(state["plans"]["P0001"])
  writeEvents(clone, [makeEvent("testBaseline", plan="P0001", command=command, passed=True, failures=[])])
  _, state = alfred.currentState()
  alfred.ensurePlanWorktrees(state)
  alfred.startTests(state)

  # 另一台電腦的測試結果不能當這台的快取，要在這台電腦的 worktree 重跑交接記錄的成果
  worktree = board_git.taskWorktreePath(clone, "P0001", "T1")
  assert testedIn == [worktree]
  assert board_git.headCommit(worktree) == recorded["task/P0001-T1"]
