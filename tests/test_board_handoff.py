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
from test_board_sync import rows
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


def test_shallowCloneWithoutApprovedBaseBlocksUntilHistoryIsFetched(computers, tmp_path, capsys, monkeypatch):
  source, _, remote = computers
  expected = [line for line in planLines(statusOutput(source, capsys)) if not line.endswith(f"｜{PlanStatus.draft}")]
  shallow = tmp_path / "電腦3" / "淺層"
  shallow.parent.mkdir()
  # 分支尖端都抓得到、SHA 也吻合，但核准基底在 depth 1 之外
  git(tmp_path, "clone", "-q", "--depth", "1", "--no-single-branch", "-b", "main", remote.as_uri(), str(shallow))
  baseCommit = readSnapshot(shallow)["plans"]["P0001"]["baseCommit"]
  messages, launched = [], []
  alfred = Conductor(shallow, config=fakeConfig(), out=messages.append)
  stubLaunches(alfred, monkeypatch, launched)

  plans, state = alfred.currentState()
  alfred.startWrites(plans, state)

  output = statusOutput(shallow, capsys)
  assert planLines(output) == expected
  assert "P0001 不派工" in output and baseCommit[:12] in output and "git fetch --unshallow" in output
  assert any("P0001" in message and "git fetch --unshallow" in message for message in messages)
  assert [entry for entry in launched if entry[1] == "P0001"] == []

  git(shallow, "fetch", "-q", "--unshallow")
  alfred.currentState()

  assert "P0001" not in alfred.sync["blockedPlans"]


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
    "RecordedTest", (), {"start": lambda self: recorded.append(Path(worktree)) or self,
                         "poll": lambda self: False, "kill": lambda self: None,
                         "wait": lambda self, seconds: None})())


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

    def poll(self):
      # 一直跑著：同一個 Alfred 連續 tick 時不會去收結果
      return False
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


def leaveConflictIn(worktree, branch="task/P0001-T1", fileName="t1.txt"):
  # 分支 SHA 不變，但 worktree 停在合併到一半、同一個檔案兩邊都有改的狀態
  git(worktree, "checkout", "-q", "-b", f"side-{fileName}", "HEAD~1")
  (worktree / fileName).write_text("這台電腦另外改的版本\n", encoding="utf-8")
  git(worktree, "add", fileName)
  git(worktree, "commit", "-q", "-m", "另一個版本")
  git(worktree, "checkout", "-q", branch)
  board_git.runGit(worktree, "merge", "-q", f"side-{fileName}", check=False)


def test_conflictedLocalWorktreeBlocksReview(computers, capsys, monkeypatch):
  source, clone, remote = computers
  writeEvents(source, [makeEvent("taskWritten", plan="P0001", task="T1", summary="完成",
                                 commit=localSha(source, "task/P0001-T1"))])
  recorded = publish(source, capsys, remote)["branches"]
  git(clone, "pull", "-q", "--ff-only")
  jobs, messages = [], []
  recordAgentJobs(monkeypatch, jobs)
  alfred = Conductor(clone, config=fakeConfig(), out=messages.append)
  alfred.currentState()
  worktree = board_git.ensureTaskWorktree(clone, "P0001", "T1")
  leaveConflictIn(worktree)
  assert board_git.unmergedFiles(worktree) == ["t1.txt"]

  plans, state = alfred.currentState()
  alfred.ensurePlanWorktrees(state)
  alfred.startReviews(plans, state)
  alfred.startTests(state)

  assert localSha(clone, "task/P0001-T1") == recorded["task/P0001-T1"]
  reason = alfred.sync["blockedPlans"]["P0001"]
  assert "t1.txt" in reason and "git merge --abort" in reason
  assert jobs == []
  assert any("P0001 不派工" in message and "t1.txt" in message for message in messages)
  assert "P0001 不派工" in statusOutput(clone, capsys)

  # 衝突處理掉之後就照常接續審查
  git(worktree, "merge", "--abort")
  plans, state = alfred.currentState()
  alfred.startReviews(plans, state)

  assert "P0001" not in alfred.sync["blockedPlans"]
  assert [job["cwd"] for job in jobs] == [worktree]


def approveOtherPlan(clone, writePlan):
  # 跟交接無關、在這台電腦才核准的 plan：P0001 被擋時它要照常派工
  writePlan("0007-other.md", rows[:1], planId="P0007", projectDir=clone)
  writeEvents(clone, [makeEvent("planSubmitted", plan="P0007"),
                      makeEvent("planApproved", plan="P0007", baseCommit=localSha(clone, "main"), branch="plan/P0007",
                                testTiming="afterMerge",
                                tasks=[{"id": "T1", "text": "T1 內容", "dependsOn": [], "requirementIds": ["R1"]}])])


def p0001Progress(alfred):
  state = alfred.currentState()[1]
  return {key: (task["status"], task["round"]) for key, task in state["tasks"].items() if task["plan"] == "P0001"}


def p0001EventsSince(clone, count):
  return [event["type"] for event in readEvents(clone)[count:] if event.get("plan") == "P0001"]


def test_conflictedPlanWorktreeHoldsEveryDispatchUntilResolved(computers, writePlan, capsys, monkeypatch):
  _, clone, _ = computers
  jobs, tested, messages = [], [], []
  recordAgentJobs(monkeypatch, jobs)
  recordTestJobs(monkeypatch, tested)
  alfred = Conductor(clone, config=fakeConfig(), out=messages.append)
  _, state = alfred.currentState()
  alfred.ensurePlanWorktrees(state)
  approveOtherPlan(clone, writePlan)
  planWorktree = board_git.planWorktreePath(clone, "P0001")
  # 你在 plan worktree 手動合併到一半；plan 分支 SHA 還是交接記錄的那個
  leaveConflictIn(planWorktree, "plan/P0001", "t3.txt")
  assert board_git.unmergedFiles(planWorktree) == ["t3.txt"]
  before, eventCount = p0001Progress(alfred), len(readEvents(clone))

  alfred.tick()

  reason = alfred.sync["blockedPlans"]["P0001"]
  assert str(planWorktree) in reason and "t3.txt" in reason and "git merge --abort" in reason
  assert any("P0001 不派工" in message and "t3.txt" in message for message in messages)
  assert "P0001 不派工" in statusOutput(clone, capsys)
  # 寫程式、合併後測試的略過都不能動到 P0001；進度和輪次原封不動
  assert p0001EventsSince(clone, eventCount) == [] and p0001Progress(alfred) == before
  assert before["P0001/T3"] == (TaskStatus.mergedUntested, 1)
  assert tested == []
  assert [job["cwd"] for job in jobs] == [board_git.taskWorktreePath(clone, "P0007", "T1")]

  # 模擬你放棄這次手動合併；同一個 Alfred 下一輪就接續，不必重新匯入或重啟
  git(planWorktree, "merge", "--abort")
  alfred.tick()

  assert "P0001" not in alfred.sync["blockedPlans"]
  assert board_git.taskWorktreePath(clone, "P0001", "T1") in [job["cwd"] for job in jobs]
  writing = [event for event in readEvents(clone) if event["type"] == "taskStarted" and event["plan"] == "P0001"][-1]
  assert (writing["task"], writing["round"]) == ("T1", before["P0001/T1"][1])
  assert "mergeTestSkipped" in p0001EventsSince(clone, eventCount)


@pytest.mark.parametrize("kind", ["plan", "task"])
def test_mergeHeadWithoutConflictFilesHoldsUntilCommitted(computers, monkeypatch, kind):
  _, clone, _ = computers
  jobs = []
  recordAgentJobs(monkeypatch, jobs)
  alfred = Conductor(clone, config=fakeConfig(), out=lambda message: None)
  _, state = alfred.currentState()
  alfred.ensurePlanWorktrees(state)
  if kind == "plan":
    worktree, branch, fileName = board_git.planWorktreePath(clone, "P0001"), "plan/P0001", "t3.txt"
  else:
    worktree, branch, fileName = board_git.ensureTaskWorktree(clone, "P0001", "T1"), "task/P0001-T1", "t1.txt"
  leaveConflictIn(worktree, branch, fileName)
  # 衝突內容已經解好也 git add 了，只差最後的 commit；分支 SHA 仍是交接記錄的那個
  (worktree / fileName).write_text("解好的版本\n", encoding="utf-8")
  git(worktree, "add", fileName)
  assert board_git.unmergedFiles(worktree) == [] and board_git.mergeInProgress(worktree)
  before = p0001Progress(alfred)

  alfred.tick()

  reason = alfred.sync["blockedPlans"]["P0001"]
  assert f"{kind} worktree {worktree}" in reason and "commit" in reason and "git merge --abort" in reason
  assert jobs == [] and p0001Progress(alfred) == before

  # 模擬你在該目錄 commit 完成合併；分支往前走、仍帶著交接的成果，下一輪就接續
  git(worktree, "commit", "-q", "--no-edit")
  alfred.tick()

  assert "P0001" not in alfred.sync["blockedPlans"]
  assert [job["cwd"] for job in jobs] == [board_git.taskWorktreePath(clone, "P0001", "T1")]
  assert p0001Progress(alfred)["P0001/T1"] == (TaskStatus.writing, before["P0001/T1"][1])


def test_conflictedPlanDoesNotRunTestBaselineUntilResolved(computers, monkeypatch):
  _, clone, _ = computers
  tested = []
  recordTestJobs(monkeypatch, tested)
  # 名額夠兩份 plan 同時跑基準，P0001 沒跑才確定是被擋，不是排隊
  alfred = Conductor(clone, config=fakeConfig(testCommand="python -c pass", maxParallelTests=2), out=lambda message: None)
  _, state = alfred.currentState()
  alfred.ensurePlanWorktrees(state)
  planWorktree = board_git.planWorktreePath(clone, "P0001")
  leaveConflictIn(planWorktree, "plan/P0001", "t3.txt")

  alfred.tick()

  # 交接來的基準不算數、這台電腦還沒有自己的基準；衝突沒解前 P0001 連基準都不跑，正常的 P0005 照跑
  assert alfred.needsTestBaseline(alfred.currentState()[1]["plans"]["P0001"])
  assert "P0001" in alfred.sync["blockedPlans"]
  assert [path.name for path in tested] == ["P0005-baseline"]
  assert not any(info["plan"] == "P0001" for info in alfred.jobs.values())

  git(planWorktree, "merge", "--abort")
  alfred.tick()

  assert [path.name for path in tested] == ["P0005-baseline", "P0001-baseline"]
  assert any(info["plan"] == "P0001" and info["kind"] == "baseline" for info in alfred.jobs.values())


def test_resolvingTaskDoesNotHidePlanWorktreeConflict(computers, monkeypatch):
  _, clone, _ = computers
  jobs = []
  recordAgentJobs(monkeypatch, jobs)
  alfred = Conductor(clone, config=fakeConfig(), out=lambda message: None)
  _, state = alfred.currentState()
  alfred.ensurePlanWorktrees(state)
  # T1 在這台電腦合併衝突、進入解衝突；它的 task worktree 留著衝突是正常的解衝突流程
  writeEvents(clone, [makeEvent("taskConflicted", plan="P0001", task="T1", conflicts=["t1.txt"])])
  taskWorktree = board_git.ensureTaskWorktree(clone, "P0001", "T1")
  leaveConflictIn(taskWorktree)
  planWorktree = board_git.planWorktreePath(clone, "P0001")
  leaveConflictIn(planWorktree, "plan/P0001", "t3.txt")
  before = p0001Progress(alfred)
  assert alfred.currentState()[1]["tasks"]["P0001/T1"]["resolving"]

  alfred.tick()

  reason = alfred.sync["blockedPlans"]["P0001"]
  assert f"plan worktree {planWorktree}" in reason and str(taskWorktree) not in reason
  assert jobs == [] and p0001Progress(alfred) == before

  git(planWorktree, "merge", "--abort")
  alfred.tick()

  # plan worktree 處理好後，T1 照原本的解衝突流程接著解自己 worktree 裡的衝突，輪次不因被擋而改變
  assert "P0001" not in alfred.sync["blockedPlans"]
  assert [job["cwd"] for job in jobs] == [taskWorktree]
  resolving = [event for event in readEvents(clone) if event["type"] == "taskStarted"][-1]
  assert (resolving["task"], resolving["round"], resolving["data"]["mode"]) == ("T1", before["P0001/T1"][1], "resolve")
  assert board_git.unmergedFiles(taskWorktree) == ["t1.txt"]


def test_userDoneTaskWithResolvedMergeStillSettlesAndMerges(computers, monkeypatch):
  _, clone, _ = computers
  recordAgentJobs(monkeypatch, [])
  alfred = Conductor(clone, config=fakeConfig(), out=lambda message: None)
  _, state = alfred.currentState()
  alfred.ensurePlanWorktrees(state)
  taskWorktree = board_git.ensureTaskWorktree(clone, "P0001", "T1")
  leaveConflictIn(taskWorktree)
  # 你在 task worktree 把衝突解好、git add 後按「完成」，原本就由 Alfred 檢查標記後 commit 再合併
  (taskWorktree / "t1.txt").write_text("解好的版本\n", encoding="utf-8")
  git(taskWorktree, "add", "t1.txt")
  writeEvents(clone, [makeEvent("needsYou", plan="P0001", task="T1", kind="mergeConflict", conflicts=["t1.txt"],
                                reason="合併衝突"),
                      makeEvent("userDecision", plan="P0001", task="T1", action="done")])
  assert alfred.currentState()[1]["tasks"]["P0001/T1"]["userDone"]

  alfred.tick()

  assert "P0001" not in alfred.sync["blockedPlans"]
  assert not board_git.mergeInProgress(taskWorktree)
  merged = [event for event in readEvents(clone) if event["type"] == "taskMerged" and event["task"] == "T1"]
  assert merged and board_git.containsCommit(clone, "refs/heads/plan/P0001", localSha(clone, "task/P0001-T1"))


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


def reviewedInSource(source, *, passed):
  commit = localSha(source, "task/P0001-T1")
  events = [started("T1", roundNumber=2), makeEvent("taskWritten", plan="P0001", task="T1", summary="完成", commit=commit),
            makeEvent("reviewStarted", plan="P0001", task="T1", roundNumber=2, agent="codex")]
  if passed:
    events.append(makeEvent("reviewPassed", plan="P0001", task="T1", roundNumber=2, agent="codex",
                            notes="reviews/P0001-T1-r2.md", mustFix=0, suggest=0))
  return events


def test_foreignReviewingIsReviewedAgainHereInSameRound(computers, capsys, monkeypatch):
  source, clone, remote = computers
  writeEvents(source, reviewedInSource(source, passed=False))
  recorded = publish(source, capsys, remote)["branches"]
  git(clone, "pull", "-q", "--ff-only")
  assert "等待接續" in statusOutput(clone, capsys)
  jobs = []
  recordAgentJobs(monkeypatch, jobs)
  alfred = Conductor(clone, config=fakeConfig(), out=lambda message: None)

  alfred.tick()

  # 另一台電腦審到一半的結果拿不回來，在這台電腦的 task worktree 從頭審同一輪，不算修正輪
  worktree = board_git.taskWorktreePath(clone, "P0001", "T1")
  reviews = [event for event in readEvents(clone) if event["type"] == "reviewStarted"]
  assert (reviews[-1]["task"], reviews[-1]["round"], reviews[-1]["agent"]) == ("T1", 2, "reviewer")
  assert [job["cwd"] for job in jobs] == [worktree]
  assert board_git.headCommit(worktree) == recorded["task/P0001-T1"]
  assert str(source) not in " ".join(jobs[0]["argv"]) + jobs[0]["stdin"]
  assert p0001Progress(alfred)["P0001/T1"] == (TaskStatus.reviewing, 2)


def blockFor(clone, alfred, block):
  # 擋住 P0001 的條件在 Alfred 第一次 tick 前就存在；回傳模擬使用者解除阻擋的動作
  if block == "missingBranch":
    git(clone, "update-ref", "-d", "refs/remotes/origin/task/P0001-T1")
    return lambda: git(clone, "fetch", "-q", "origin")
  _, state = alfred.currentState()
  if block == "planConflict":
    alfred.ensurePlanWorktrees(state)
    worktree = board_git.planWorktreePath(clone, "P0001")
    leaveConflictIn(worktree, "plan/P0001", "t3.txt")
  else:
    worktree = board_git.ensureTaskWorktree(clone, "P0001", "T1")
    leaveConflictIn(worktree)
  return lambda: git(worktree, "merge", "--abort")


def waitingTasks(clone):
  return {task["task"]: task["handoffWaiting"] for task in board_api.stateView(clone)["tasks"] if task["plan"] == "P0001"}


# 來源電腦停在哪個執行態、本機恢復時記的中斷階段，以及恢復後重新開始的事件
foreignRuns = {
  "writing": (lambda source: [started("T1", roundNumber=2)], TaskStatus.writing, "write", "taskStarted"),
  "reviewing": (lambda source: reviewedInSource(source, passed=False), TaskStatus.reviewing, "review", "reviewStarted"),
}


@pytest.mark.parametrize("block", ["missingBranch", "planConflict", "taskConflict"])
@pytest.mark.parametrize("stage", ["writing", "reviewing"])
def test_blockedForeignRunKeepsProgressUntilResolvedThenRecoversOnce(computers, writePlan, capsys, monkeypatch,
                                                                     stage, block):
  source, clone, remote = computers
  sourceEvents, status, interruptedStage, restartType = foreignRuns[stage]
  writeEvents(source, sourceEvents(source))
  publish(source, capsys, remote)
  git(clone, "pull", "-q", "--ff-only")
  jobs = []
  recordAgentJobs(monkeypatch, jobs)
  alfred = Conductor(clone, config=fakeConfig(), out=lambda message: None)
  unblock = blockFor(clone, alfred, block)
  alfred.currentState()
  approveOtherPlan(clone, writePlan)
  eventCount = len(readEvents(clone))

  alfred.tick()
  alfred.tick()

  # 被擋期間不記中斷、不改階段與輪次，仍顯示等待接續；正常的 P0007 照常派工
  assert "P0001" in alfred.sync["blockedPlans"]
  assert p0001EventsSince(clone, eventCount) == []
  assert p0001Progress(alfred)["P0001/T1"] == (status, 2)
  assert waitingTasks(clone)["T1"] is True
  assert [job["cwd"] for job in jobs] == [board_git.taskWorktreePath(clone, "P0007", "T1")]

  unblock()
  alfred.tick()
  alfred.tick()

  # 同一個 Alfred 解除後自動補做恢復：只記一次中斷，回到原階段在本機同一輪重新開始，之後的 tick 不再重複
  assert "P0001" not in alfred.sync["blockedPlans"]
  later = [event for event in readEvents(clone)[eventCount:] if event.get("plan") == "P0001" and event.get("task") == "T1"]
  assert [(event["type"], event["round"]) for event in later] == [("taskInterrupted", 2), (restartType, 2)]
  assert later[0]["data"]["stage"] == interruptedStage
  assert [job["cwd"] for job in jobs][1:] == [board_git.taskWorktreePath(clone, "P0001", "T1")]
  assert p0001Progress(alfred)["P0001/T1"] == (status, 2)
  assert waitingTasks(clone)["T1"] is False


def test_blockedFinishedPlanIsCompletedOnlyAfterResolved(computers, monkeypatch):
  _, clone, _ = computers
  # P0005 的任務都完成了，但這台電腦抓不到它的 plan 分支
  git(clone, "update-ref", "-d", "refs/remotes/origin/plan/P0005")
  recordAgentJobs(monkeypatch, [])
  alfred = Conductor(clone, config=fakeConfig(), out=lambda message: None)

  def completions():
    return [event for event in readEvents(clone) if event["type"] == "planCompleted" and event["plan"] == "P0005"]

  alfred.tick()
  alfred.tick()

  assert "P0005" in alfred.sync["blockedPlans"]
  assert completions() == []
  assert alfred.currentState()[1]["plans"]["P0005"]["status"] == PlanStatus.active

  git(clone, "fetch", "-q", "origin")
  alfred.tick()
  alfred.tick()

  assert "P0005" not in alfred.sync["blockedPlans"]
  assert len(completions()) == 1


def test_foreignMergingIsMergedHereInLocalPlanWorktree(computers, capsys, monkeypatch):
  source, clone, remote = computers
  writeEvents(source, reviewedInSource(source, passed=True))
  recorded = publish(source, capsys, remote)["branches"]
  git(clone, "pull", "-q", "--ff-only")
  assert taskStatuses(clone)["P0001/T1"] == TaskStatus.merging
  recordAgentJobs(monkeypatch, [])
  alfred = Conductor(clone, config=fakeConfig(), out=lambda message: None)

  alfred.tick()

  # 審查通過、還沒合併的任務在這台電腦的 plan worktree 合併，輪次沿用交接記錄的
  planWorktree = board_git.planWorktreePath(clone, "P0001")
  merged = [event for event in readEvents(clone) if event["type"] == "taskMerged" and event["task"] == "T1"]
  assert len(merged) == 1 and merged[0]["round"] == 2
  assert board_git.headCommit(planWorktree) == merged[0]["data"]["commit"]
  assert board_git.containsCommit(clone, merged[0]["data"]["commit"], recorded["task/P0001-T1"])
  assert board_git.containsCommit(clone, merged[0]["data"]["commit"], recorded["plan/P0001"])
  assert p0001Progress(alfred)["P0001/T1"][1] == 2


def test_foreignMergedUntestedRunsMergeTestHere(computers, monkeypatch):
  _, clone, _ = computers
  command = "python -c pass"
  tested = []
  recordTestJobs(monkeypatch, tested)
  alfred = Conductor(clone, config=fakeConfig(testCommand=command), out=lambda message: None)
  alfred.currentState()
  # 交接檔的基準不帶指令，先讓這台電腦有自己的基準，才輪得到合併後測試
  writeEvents(clone, [makeEvent("testBaseline", plan=planIdValue, command=command, passed=True, failures=[])
                      for planIdValue in ("P0001", "P0005")])
  before = p0001Progress(alfred)

  alfred.tick()

  # 另一台電腦合併完、還沒驗的任務，在這台電腦的 mergetest worktree 驗交接記錄的合併 commit
  mergedCommit = alfred.currentState()[1]["tasks"]["P0001/T3"]["mergedCommit"]
  worktree = board_git.mergeTestWorktreePath(clone, "P0001")
  assert tested == [worktree]
  assert alfred.jobs["P0001/mergetest"]["commit"] == mergedCommit
  assert alfred.jobs["P0001/mergetest"]["covers"] == ["T3"]
  assert board_git.headCommit(worktree) == mergedCommit
  assert p0001Progress(alfred)["P0001/T3"] == before["P0001/T3"] == (TaskStatus.mergedUntested, 1)


def taskStatuses(clone):
  state = Conductor(clone, config=fakeConfig(), out=lambda message: None).currentState()[1]
  return {key: task["status"] for key, task in state["tasks"].items()}


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
