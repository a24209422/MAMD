import os
import subprocess
import sys

import board as boardCli
import board_core
import board_git
import conductor as conductorModule
from board_core import PlanStatus, TaskStatus
from conductor import Conductor
from helpers import fakeConfig, fakeRole, readCalls, runUntil

oneTask = ["| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | | 驗證一 |"]
twoIndependent = oneTask + ["| R1 | | D1 | | T2 任務二 | | 驗證二 |"]


def planDone(state):
  return state["plans"]["P0001"]["status"] == PlanStatus.completed


def taskIs(status, taskId="T1"):
  return lambda state: state["tasks"][f"P0001/{taskId}"]["status"] == status


def eventTypes(conductor):
  return [event["type"] for event in board_core.readEvents(conductor.projectDir)]


def test_stopInterruptsAndResumeContinues(startBoard):
  conductor, scenarioDir = startBoard(oneTask, {"write": {"T1": ["hang", "done"]}})
  runUntil(conductor, taskIs(TaskStatus.writing))
  runUntil(conductor, lambda state: (scenarioDir / "call-write-P0001-T1-1.json").exists())
  board_core.appendEvent(conductor.projectDir, "paused", data={"reason": "測試停止"})
  runUntil(conductor, lambda state: not conductor.jobs and taskIs(TaskStatus.queued)(state))
  assert "taskInterrupted" in eventTypes(conductor)
  board_core.appendEvent(conductor.projectDir, "resumed")
  runUntil(conductor, planDone)
  assert len(readCalls(scenarioDir, "write")) == 2


def test_pauseKeepsResultOfAgentThatAlreadyFinished(startBoard):
  conductor, _ = startBoard(oneTask)
  runUntil(conductor, taskIs(TaskStatus.writing))
  # 等 Claude 自己寫完並結束，但 conductor 還沒收它的結果，這時才暫停
  conductor.jobs["P0001/T1"]["job"].process.wait(timeout=30)
  board_core.appendEvent(conductor.projectDir, "paused", data={"reason": "測試停止"})
  state = runUntil(conductor, lambda state: not conductor.jobs)
  assert state["tasks"]["P0001/T1"]["status"] == TaskStatus.toReview
  assert "taskWritten" in eventTypes(conductor)
  assert "taskInterrupted" not in eventTypes(conductor)


def test_restartRecoversInterruptedTask(startBoard):
  conductor, scenarioDir = startBoard(oneTask, {"write": {"T1": ["hang", "done"]}})
  runUntil(conductor, taskIs(TaskStatus.writing))
  runUntil(conductor, lambda state: (scenarioDir / "call-write-P0001-T1-1.json").exists())
  for info in conductor.jobs.values():
    # 模擬 conductor 當掉：agent 跟著結束，但沒有留下任何事件
    info["job"].kill()
  restarted = Conductor(conductor.projectDir, config=fakeConfig(), out=lambda line: None)
  runUntil(restarted, planDone)
  assert eventTypes(restarted).count("taskInterrupted") == 1


def test_quotaPausesEverything(startBoard):
  conductor, _ = startBoard(oneTask, {"write": {"T1": ["quota"]}})
  state = runUntil(conductor, lambda state: state["paused"])
  assert "額度用完" in state["pauseReason"]
  assert "agentFailed" not in eventTypes(conductor)
  assert state["tasks"]["P0001/T1"]["status"] == TaskStatus.queued


def test_codexWriterQuotaPausesEverything(startBoard):
  conductor, _ = startBoard(oneTask, {"write": {"T1": ["quota"]}}, writer=fakeRole("codex"))
  state = runUntil(conductor, lambda state: state["paused"])
  assert "額度用完（writer，codex）" in state["pauseReason"]
  assert state["tasks"]["P0001/T1"]["status"] == TaskStatus.queued


def test_reviewQuotaPausesEverything(startBoard):
  conductor, _ = startBoard(oneTask, {"review": {"T1": ["quota"]}})
  state = runUntil(conductor, lambda state: state["paused"])
  assert state["tasks"]["P0001/T1"]["status"] == TaskStatus.toReview


def test_timeoutCountsAsFailure(startBoard):
  conductor, _ = startBoard(oneTask, {"write": {"T1": ["hang", "done"]}}, timeoutMinutes=0.03)
  runUntil(conductor, planDone)
  failures = [event for event in board_core.readEvents(conductor.projectDir) if event["type"] == "agentFailed"]
  assert len(failures) == 1
  assert failures[0]["data"]["reason"].startswith("超時")


def test_abandonPlanKillsJobs(startBoard):
  conductor, _ = startBoard(oneTask, {"write": {"T1": ["hang"]}})
  runUntil(conductor, taskIs(TaskStatus.writing))
  board_core.appendEvent(conductor.projectDir, "planAbandoned", plan="P0001")
  state = runUntil(conductor, lambda state: not conductor.jobs)
  assert state["tasks"]["P0001/T1"]["status"] == TaskStatus.abandoned
  assert "taskInterrupted" not in eventTypes(conductor)


def test_mergeConflictThenUserMergesAndMarksDone(startBoard, monkeypatch):
  monkeypatch.setattr(conductorModule, "maxAutoResolves", 0)
  conductor, _ = startBoard(twoIndependent, {"write": {"T1": ["conflict"], "T2": ["conflict"]}})
  state = runUntil(conductor, lambda state: any(task["status"] == TaskStatus.needsYou for task in state["tasks"].values()))
  conflicted = next(task for task in state["tasks"].values() if task["status"] == TaskStatus.needsYou)
  assert conflicted["needs"]["kind"] == "mergeConflict"
  assert "shared.txt" in conflicted["needs"]["reason"]
  planPath = board_git.planWorktreePath(conductor.projectDir, "P0001")
  subprocess.run(["git", "merge", "--no-ff", "-X", "theirs", "-m", "手動解衝突",
                  board_git.taskBranch("P0001", conflicted["task"])], cwd=planPath, check=True, capture_output=True)
  assert boardCli.cmdDecide(conductor.projectDir, "P0001", conflicted["task"], "done") == 0
  runUntil(conductor, planDone)


def test_retryAfterRoundsExhaustedPassesNote(startBoard):
  conductor, scenarioDir = startBoard(oneTask, {"review": {"T1": ["fail", "fail", "fail", "pass"]}})
  runUntil(conductor, taskIs(TaskStatus.needsYou))
  assert boardCli.cmdDecide(conductor.projectDir, "P0001", "T1", "retry", "改用中文訊息") == 0
  runUntil(conductor, planDone)
  calls = readCalls(scenarioDir, "write")
  assert len(calls) == 4
  assert "使用者補充：改用中文訊息" in calls[3]["prompt"]
  assert "（第 4 輪）" in calls[3]["prompt"]


def test_startFailureBecomesNeedsYou(startBoard, tmp_path):
  conductor, _ = startBoard(oneTask, writer=fakeRole("claude", command=[str(tmp_path / "missing-claude.exe")]))
  state = runUntil(conductor, taskIs(TaskStatus.needsYou))
  assert state["tasks"]["P0001/T1"]["needs"]["kind"] == "agentFailed"
  assert "無法啟動" in state["tasks"]["P0001/T1"]["needs"]["reason"]


def test_safeTickReportsErrorAndKeepsRunning(startBoard, monkeypatch):
  conductor, _ = startBoard(oneTask)
  lines = []
  reporting = Conductor(conductor.projectDir, config=fakeConfig(), out=lines.append)

  def failing(state):
    raise board_git.GitError("模擬失敗")

  monkeypatch.setattr(reporting, "ensurePlanWorktrees", failing)
  reporting.safeTick()
  assert any("錯誤（GitError）：模擬失敗" in line for line in lines)


def test_eventWriteFailureDoesNotStrandTask(startBoard, monkeypatch):
  import time

  import conductor as conductorModule

  conductor, _ = startBoard(oneTask)
  lines = []
  reporting = Conductor(conductor.projectDir, config=fakeConfig(), out=lines.append)
  realAppend = conductorModule.appendEvent
  failed = []

  def flakyAppend(*args, **kwargs):
    if args[1] == "taskWritten" and not failed:
      failed.append(True)
      raise board_core.BoardError("模擬鎖逾時")
    return realAppend(*args, **kwargs)

  monkeypatch.setattr(conductorModule, "appendEvent", flakyAppend)
  deadline = time.time() + 60
  while time.time() < deadline and not planDone(reporting.currentState()[1]):
    reporting.safeTick()
    time.sleep(0.05)
  assert planDone(reporting.currentState()[1])
  assert any("錯誤（BoardError）：模擬鎖逾時" in line for line in lines)
  assert eventTypes(reporting).count("taskInterrupted") == 1


def test_quotaWordsInsideJsonAreNotQuota(startBoard):
  conductor, _ = startBoard(oneTask, {"write": {"T1": ["crashmention", "done"]}})
  runUntil(conductor, planDone)
  assert "paused" not in eventTypes(conductor)
  assert eventTypes(conductor).count("agentFailed") == 1


def test_safeTickPrintsSameErrorOnlyOnce(startBoard, monkeypatch):
  conductor, _ = startBoard(oneTask)
  lines = []
  reporting = Conductor(conductor.projectDir, config=fakeConfig(), out=lines.append)

  def failing(state):
    raise board_git.GitError("模擬失敗")

  monkeypatch.setattr(reporting, "ensurePlanWorktrees", failing)
  for _ in range(3):
    reporting.safeTick()
  assert sum("錯誤（GitError）：模擬失敗" in line for line in lines) == 1


def test_planWorktreeFailureIsReportedOnceWithoutStoppingTick(startBoard, monkeypatch):
  conductor, _ = startBoard(oneTask)
  lines = []
  reporting = Conductor(conductor.projectDir, config=fakeConfig(), out=lines.append)

  def failingWorktree(projectDir, planIdValue, baseCommit):
    raise board_git.GitError("模擬 worktree 失敗")

  monkeypatch.setattr(board_git, "ensurePlanWorktree", failingWorktree)
  for _ in range(3):
    reporting.tick()
  assert lines.count("P0001 的 plan worktree 建立失敗：模擬 worktree 失敗") == 1
  assert "planCompleted" not in eventTypes(reporting)


def test_unreadablePlanIsReportedOnceAndOthersRun(startBoard):
  conductor, _ = startBoard(oneTask)
  (conductor.projectDir / "plans" / "0002-utf16.md").write_text("---\nid: P0002\n---\n", encoding="utf-16")
  lines = []
  reporting = Conductor(conductor.projectDir, config=fakeConfig(), out=lines.append)
  runUntil(reporting, planDone)
  warnings = [line for line in lines if "0002-utf16.md 無法讀取（UnicodeDecodeError），已略過" in line]
  assert len(warnings) == 1


def test_resultProcessingErrorNeedsYouAndOthersContinue(startBoard, monkeypatch):
  conductor, _ = startBoard(twoIndependent)
  realFinishWrite = conductor.finishWrite

  def failingFinishWrite(info):
    if info["task"] == "T1":
      raise RuntimeError("模擬處理失敗")
    realFinishWrite(info)

  monkeypatch.setattr(conductor, "finishWrite", failingFinishWrite)
  state = runUntil(conductor, lambda current: taskIs(TaskStatus.needsYou)(current)
                   and taskIs(TaskStatus.done, "T2")(current))
  assert state["tasks"]["P0001/T1"]["needs"] == {
    "kind": "internalError", "reason": "處理 agent 結果時出錯：RuntimeError: 模擬處理失敗"}
  assert "agentFailed" not in eventTypes(conductor)


def test_mergeErrorNeedsYouAndOtherTasksStillMerge(startBoard, monkeypatch):
  conductor, _ = startBoard(twoIndependent)
  realMergeTask = board_git.mergeTask

  def failingMergeTask(projectDir, planIdValue, taskId, message):
    if taskId == "T1":
      raise board_git.GitError("模擬合併失敗")
    return realMergeTask(projectDir, planIdValue, taskId, message)

  monkeypatch.setattr(board_git, "mergeTask", failingMergeTask)
  state = runUntil(conductor, lambda current: taskIs(TaskStatus.needsYou)(current)
                   and taskIs(TaskStatus.done, "T2")(current))
  assert state["tasks"]["P0001/T1"]["needs"] == {"kind": "mergeFailed", "reason": "合併失敗：模擬合併失敗"}


def lockPathOf(projectDir):
  return projectDir / ".board" / "conductor.lock"


def test_conductorLockRefusedWhileOwnerAlive(gitProject):
  lockPath = lockPathOf(gitProject)
  lockPath.parent.mkdir(parents=True, exist_ok=True)
  lockPath.write_text(str(os.getpid()), encoding="utf-8")
  assert conductorModule.acquireConductorLock(gitProject) is False
  assert lockPath.read_text(encoding="utf-8") == str(os.getpid())


def test_conductorLockTakesOverStaleLockAndReleases(gitProject):
  lockPath = lockPathOf(gitProject)
  assert conductorModule.acquireConductorLock(gitProject) is True
  assert lockPath.read_text(encoding="utf-8") == str(os.getpid())
  finished = subprocess.Popen([sys.executable, "-c", "pass"])
  finished.wait()
  lockPath.write_text(str(finished.pid), encoding="utf-8")
  assert conductorModule.acquireConductorLock(gitProject) is True
  assert lockPath.read_text(encoding="utf-8") == str(os.getpid())
  lockPath.write_text("壞掉的內容", encoding="utf-8")
  assert conductorModule.acquireConductorLock(gitProject) is True
  assert lockPath.read_text(encoding="utf-8") == str(os.getpid())
  conductorModule.releaseConductorLock(gitProject)
  assert not lockPath.exists()


def test_mainRefusesMissingFolderAndNonGitFolder(tmp_path, monkeypatch, capsys):
  # 不讓 git 往上找到暫存資料夾外層剛好存在的 repo
  monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
  assert conductorModule.main([str(tmp_path / "missing")]) == 1
  assert "找不到專案資料夾" in capsys.readouterr().out
  plainDir = tmp_path / "plain"
  plainDir.mkdir()
  assert conductorModule.main([str(plainDir)]) == 1
  assert "不是 git repo" in capsys.readouterr().out
  assert not lockPathOf(plainDir).exists()


def test_mainRefusesLegacyConfig(gitProject, capsys):
  (gitProject / ".board").mkdir(exist_ok=True)
  (gitProject / ".board" / "config.json").write_text('{"claudeCommand": ["claude"]}', encoding="utf-8")
  assert conductorModule.main([str(gitProject)]) == 1
  out = capsys.readouterr().out
  assert ".board/config.json 要先修正：" in out
  assert "claudeCommand 已經不用了" in out
  assert not lockPathOf(gitProject).exists()


def test_mainRefusesSecondConductor(gitProject, monkeypatch, capsys):
  lockPath = lockPathOf(gitProject)
  lockPath.parent.mkdir(parents=True, exist_ok=True)
  (lockPath.parent / "config.json").write_text('{"skipTests": true}', encoding="utf-8")
  lockPath.write_text(str(os.getpid()), encoding="utf-8")
  started = []
  monkeypatch.setattr(conductorModule, "Conductor", lambda projectDir: started.append(projectDir))
  assert conductorModule.main([str(gitProject)]) == 1
  assert f"這個專案已經有 Alfred 在跑（PID {os.getpid()}），不能同時跑兩個" in capsys.readouterr().out
  assert started == []
  assert lockPath.exists()


def test_mainHoldsLockWhileRunningAndReleasesAfter(gitProject, monkeypatch):
  lockPath = lockPathOf(gitProject)
  lockPath.parent.mkdir(parents=True, exist_ok=True)
  (lockPath.parent / "config.json").write_text('{"skipTests": true}', encoding="utf-8")
  seenLocks = []

  class FakeConductor:
    def __init__(self, projectDir):
      self.projectDir = projectDir

    def runForever(self):
      seenLocks.append(lockPath.read_text(encoding="utf-8"))

  monkeypatch.setattr(conductorModule, "Conductor", FakeConductor)
  assert conductorModule.main([str(gitProject)]) == 0
  assert seenLocks == [str(os.getpid())]
  assert not lockPath.exists()
