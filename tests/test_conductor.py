import subprocess
import sys
import time

import board_agents
import board_core
import board_git
from board_core import PlanStatus, TaskStatus
import conductor as conductorModule
from conductor import formatEvent
from helpers import makeEvent, readCalls, runUntil

oneTask = ["| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | | 驗證一 |"]
chainedTasks = oneTask + ["| R1 | | D1 | | T2 任務二 | T1 | 驗證二 |"]
twoTasks = oneTask + ["| R1 | | D1 | | T2 任務二 | | 驗證二 |"]
threeTasks = oneTask + ["| R1 | | D1 | | T2 任務二 | | 驗證二 |", "| R1 | | D1 | | T3 任務三 | | 驗證三 |"]


def planDone(state):
  return state["plans"]["P0001"]["status"] == PlanStatus.completed


def taskIs(status, taskId="T1"):
  return lambda state: state["tasks"][f"P0001/{taskId}"]["status"] == status


def eventsOf(conductor, eventType):
  return [event for event in board_core.readEvents(conductor.projectDir) if event["type"] == eventType]


def test_happyPathRespectsDependencyAndMerges(startBoard):
  conductor, scenarioDir = startBoard(chainedTasks)
  runUntil(conductor, planDone)
  events = board_core.readEvents(conductor.projectDir)
  types = [(event["type"], event["task"]) for event in events]
  assert types.index(("taskStarted", "T2")) > types.index(("taskMerged", "T1"))
  assert board_git.runGit(conductor.projectDir, "show", "plan/P0001:T2.txt") == "T2 第 1 次"
  assert (conductor.projectDir / ".board" / "reviews" / "P0001-T1-r1.md").exists()
  assert len(eventsOf(conductor, "planCompleted")) == 1
  assert "--allowedTools" not in readCalls(scenarioDir, "write", taskId="T1")[0]["argv"]


def test_parallelTasksUseSeparateLanes(startBoard):
  conductor, _ = startBoard(threeTasks, {"write": {"T1": ["slow"], "T2": ["slow"], "T3": ["slow"]}})
  runUntil(conductor, planDone)
  events = board_core.readEvents(conductor.projectDir)
  starts = [index for index, event in enumerate(events) if event["type"] == "taskStarted"]
  firstWritten = next(index for index, event in enumerate(events) if event["type"] == "taskWritten")
  assert {events[index]["agent"] for index in starts} == {"claude", "claude-sub1", "claude-sub2"}
  assert max(starts) < firstWritten


def test_failThenPassResumesSessionWithReview(startBoard):
  conductor, scenarioDir = startBoard(oneTask, {"review": {"T1": ["fail", "pass"]}})
  runUntil(conductor, planDone)
  calls = readCalls(scenarioDir, "write")
  assert len(calls) == 2
  firstSession = calls[0]["argv"][calls[0]["argv"].index("--session-id") + 1]
  assert calls[1]["argv"][calls[1]["argv"].index("--resume") + 1] == firstSession
  assert "還沒達成需求" in calls[1]["prompt"]
  assert "（第 2 輪）" in calls[1]["prompt"]
  reviewsDir = conductor.projectDir / ".board" / "reviews"
  assert "第 1 輪審查：不通過" in (reviewsDir / "P0001-T1-r1.md").read_text(encoding="utf-8")
  assert "第 2 輪審查：通過" in (reviewsDir / "P0001-T1-r2.md").read_text(encoding="utf-8")


def test_threeFailuresNeedYou(startBoard):
  conductor, scenarioDir = startBoard(oneTask, {"review": {"T1": ["fail"]}})
  state = runUntil(conductor, taskIs(TaskStatus.needsYou))
  assert state["tasks"]["P0001/T1"]["needs"]["reason"] == "審查 3 次不通過"
  assert len(readCalls(scenarioDir, "write")) == 3


def test_blockedNeedsYou(startBoard):
  conductor, _ = startBoard(oneTask, {"write": {"T1": ["blocked"]}})
  state = runUntil(conductor, taskIs(TaskStatus.needsYou))
  assert state["tasks"]["P0001/T1"]["needs"]["reason"] == "Claude 回報卡住：缺少 API 位址"


def test_noChangesNeedsYou(startBoard):
  conductor, _ = startBoard(oneTask, {"write": {"T1": ["nochange"]}})
  state = runUntil(conductor, taskIs(TaskStatus.needsYou))
  assert state["tasks"]["P0001/T1"]["needs"]["kind"] == "noChanges"


def test_crashTwiceNeedsYou(startBoard):
  conductor, scenarioDir = startBoard(oneTask, {"write": {"T1": ["crash"]}})
  state = runUntil(conductor, taskIs(TaskStatus.needsYou))
  assert state["tasks"]["P0001/T1"]["needs"]["kind"] == "agentFailed"
  assert len(readCalls(scenarioDir, "write")) == 2


def test_crashOnceThenSucceeds(startBoard):
  conductor, scenarioDir = startBoard(oneTask, {"write": {"T1": ["crash", "done"]}})
  runUntil(conductor, planDone)
  calls = readCalls(scenarioDir, "write")
  assert "--session-id" in calls[1]["argv"]
  assert len(eventsOf(conductor, "agentFailed")) == 1


def test_invalidReviewTwiceNeedsYou(startBoard):
  conductor, scenarioDir = startBoard(oneTask, {"review": {"T1": ["invalid"]}})
  state = runUntil(conductor, taskIs(TaskStatus.needsYou))
  assert state["tasks"]["P0001/T1"]["needs"]["kind"] == "invalidReview"
  assert len(readCalls(scenarioDir, "review")) == 2


def test_invalidReviewOnceThenPasses(startBoard):
  conductor, scenarioDir = startBoard(oneTask, {"review": {"T1": ["invalid", "pass"]}})
  runUntil(conductor, planDone)
  calls = readCalls(scenarioDir, "review")
  assert "不通過時至少要有一條必改" in calls[1]["prompt"]
  assert len(eventsOf(conductor, "reviewStarted")) == 1


def test_testCommandResultGoesToReview(startBoard):
  conductor, scenarioDir = startBoard(oneTask, testCommand="git --version")
  runUntil(conductor, planDone)
  testRun = eventsOf(conductor, "testRun")[0]
  assert testRun["data"]["passed"] is True
  assert "自動測試結果：通過" in readCalls(scenarioDir, "review")[0]["prompt"]
  writeArgv = readCalls(scenarioDir, "write")[0]["argv"]
  assert "--allowedTools" in writeArgv
  assert "Bash(git --version*)" in writeArgv
  assert "PowerShell(git --version*)" in writeArgv
  assert "Bash(python -m pytest*)" not in writeArgv
  assert "tdd-crap" not in readCalls(scenarioDir, "write")[0]["prompt"]


def test_tddCrapAddsStepsToPromptsAndAllowsPytest(startBoard):
  conductor, scenarioDir = startBoard(oneTask, testCommand="git --version", tddCrap=True)
  runUntil(conductor, planDone)
  write = readCalls(scenarioDir, "write")[0]
  assert "tdd-crap" in write["prompt"] and "git --version" in write["prompt"]
  assert "Bash(python -m pytest*)" in write["argv"] and "PowerShell(python -m pytest*)" in write["argv"]
  assert ".quality/report.json" in readCalls(scenarioDir, "review")[0]["prompt"]


def test_mergeSkipsWhileUserResolvesConflictInPlanWorktree(startBoard, monkeypatch):
  monkeypatch.setattr(conductorModule, "maxAutoResolves", 0)
  conductor, _ = startBoard(twoTasks, {"write": {"T1": ["conflict"], "T2": ["conflict"]}})
  state = runUntil(conductor, lambda current: any(
    task["status"] == TaskStatus.needsYou for task in current["tasks"].values()))
  conflicted = next(task["task"] for task in state["tasks"].values() if task["status"] == TaskStatus.needsYou)
  planPath = board_git.planWorktreePath(conductor.projectDir, "P0001")
  subprocess.run(["git", "merge", "--no-ff", "-m", "手動合併", f"task/P0001-{conflicted}"], cwd=planPath,
                 capture_output=True)
  assert board_git.mergeInProgress(planPath)
  conflictText = (planPath / "shared.txt").read_text(encoding="utf-8")
  needsBefore = len(eventsOf(conductor, "needsYou"))
  board_core.appendEvent(conductor.projectDir, "userDecision", plan="P0001", task=conflicted,
                         data={"action": "done"})
  for _ in range(3):
    conductor.tick()
  state = conductor.currentState()[1]
  assert board_git.mergeInProgress(planPath)
  assert (planPath / "shared.txt").read_text(encoding="utf-8") == conflictText
  assert state["tasks"][f"P0001/{conflicted}"]["status"] == TaskStatus.merging
  assert len(eventsOf(conductor, "needsYou")) == needsBefore
  (planPath / "shared.txt").write_text("人工解完\n", encoding="utf-8")
  subprocess.run(["git", "add", "shared.txt"], cwd=planPath, check=True)
  subprocess.run(["git", "commit", "-q", "--no-edit"], cwd=planPath, check=True)
  runUntil(conductor, planDone)


def test_testCommandTimeoutKillsProcessTree(startBoard, tmp_path):
  scriptPath = tmp_path / "slowTest.py"
  scriptPath.write_text(
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
    "time.sleep(30)\n", encoding="utf-8")
  conductor, _ = startBoard(oneTask, testCommand=f'"{sys.executable}" "{scriptPath}"', timeoutMinutes=0.05)
  startedAt = time.time()
  runUntil(conductor, planDone)
  testRun = eventsOf(conductor, "testRun")[0]
  assert testRun["data"]["passed"] is False
  assert "測試超時" in testRun["data"]["tail"]
  assert time.time() - startedAt < 25


def test_testOutputGivenUpWhenPipeStaysOpenAfterKill(startBoard, tmp_path, monkeypatch):
  import conductor as conductorModule

  pidPath = tmp_path / "escapedPid.txt"
  escaperPath = tmp_path / "escaper.py"
  # 中間這層立刻結束，睡覺的孫程式就脫離程式樹：結束程式樹時殺不到它，它卻還握著測試輸出的管線
  escaperPath.write_text(
    "import os, subprocess, sys\n"
    "options = {} if os.name == 'nt' else {'start_new_session': True}\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],\n"
    "                         stdout=sys.stdout, stderr=sys.stderr, **options)\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n", encoding="utf-8")
  scriptPath = tmp_path / "slowTest.py"
  scriptPath.write_text(
    "import subprocess, sys, time\n"
    "subprocess.run([sys.executable, sys.argv[1], sys.argv[2]], stdout=sys.stdout, stderr=sys.stderr)\n"
    "print('測試輸出', flush=True)\n"
    "time.sleep(30)\n", encoding="utf-8")
  monkeypatch.setattr(conductorModule, "outputWaitAfterKillSeconds", 1)
  conductor, _ = startBoard(oneTask, testCommand=f'"{sys.executable}" "{scriptPath}" "{escaperPath}" "{pidPath}"',
                            timeoutMinutes=0.05)
  startedAt = time.time()
  try:
    runUntil(conductor, planDone)
    testRun = eventsOf(conductor, "testRun")[0]
    assert testRun["data"]["passed"] is False
    assert testRun["data"]["tail"] == "測試超時（輸出無法取得）"
    assert time.time() - startedAt < 15
  finally:
    if pidPath.exists():
      board_agents.killProcessTree(int(pidPath.read_text()))


def test_mergeBlockedByUntrackedFileNeedsYouThenRetryMerges(startBoard, gitProject):
  conductor, _ = startBoard(oneTask)
  planPath = board_git.ensurePlanWorktree(gitProject, "P0001", board_git.headCommit(gitProject))
  (planPath / "T1.txt").write_text("沒有追蹤的檔案\n", encoding="utf-8")
  state = runUntil(conductor, taskIs(TaskStatus.needsYou))
  needs = state["tasks"]["P0001/T1"]["needs"]
  assert needs["kind"] == "mergeFailed"
  assert needs["reason"].startswith("合併失敗：")
  assert "T1.txt" in needs["reason"]
  (planPath / "T1.txt").unlink()
  board_core.appendEvent(gitProject, "userDecision", plan="P0001", task="T1", data={"action": "retry"})
  runUntil(conductor, planDone)


def test_agentFailedReasonKeepsOnlyLast500CharsOfOutput(startBoard):
  conductor, _ = startBoard(oneTask, {"write": {"T1": ["crashlong", "done"]}})
  runUntil(conductor, planDone)
  reason = eventsOf(conductor, "agentFailed")[0]["data"]["reason"]
  prefix, tail = reason.split("｜", 1)
  assert prefix == "沒有收到 result 事件"
  assert tail == "x" * 500


def test_commitFailureNeedsYou(startBoard, gitProject):
  conductor, _ = startBoard(oneTask)
  hookPath = gitProject / ".git" / "hooks" / "pre-commit"
  hookPath.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
  state = runUntil(conductor, taskIs(TaskStatus.needsYou))
  needs = state["tasks"]["P0001/T1"]["needs"]
  assert needs["kind"] == "commitFailed"
  assert "commit 失敗" in needs["reason"]


def test_taskRemovedFromPlanAfterApprovalDoesNotBlockOthers(startBoard, writePlan, gitProject):
  conductor, _ = startBoard(twoTasks)
  # 核准後 plan 檔被改掉 T1 那一列：快照裡還有 T1，但 prompt 找不到它
  writePlan("0001-demo.md", ["| R1 需求一 | 理解一 | D1 做法一 | - | T2 任務二 | | 驗證二 |"], planId="P0001",
            projectDir=gitProject)
  state = runUntil(conductor, lambda current: taskIs(TaskStatus.needsYou)(current)
                   and taskIs(TaskStatus.done, "T2")(current))
  needs = state["tasks"]["P0001/T1"]["needs"]
  assert needs["kind"] == "agentFailed"
  assert "無法啟動：KeyError: 'T1'" in needs["reason"]
  assert len(eventsOf(conductor, "agentFailed")) == 2


def test_formatEvent():
  event = makeEvent("reviewFailed", plan="P0001", task="T2", roundNumber=1, agent="codex-sub1", mustFix=2)
  event["time"] = "2026-10-25T21:40:12+08:00"
  assert formatEvent(event) == "21:40:12 P0001 T2 codex-sub1 審查不通過（第 1 輪，必改 2 條）"
  paused = makeEvent("paused", reason="你執行了 stop")
  paused["time"] = "2026-10-25T21:41:00+08:00"
  assert formatEvent(paused) == "21:41:00 全部暫停（你執行了 stop）"


def conflictedTask(conductor):
  return next(event["task"] for event in eventsOf(conductor, "taskConflicted"))


def test_conflictIsResolvedByClaudeThenReviewedAgain(startBoard):
  conductor, scenarioDir = startBoard(twoTasks, {"write": {"T1": ["conflict", "resolve"], "T2": ["conflict", "resolve"]}})
  runUntil(conductor, planDone)
  assert len(eventsOf(conductor, "taskConflicted")) == 1
  taskId = conflictedTask(conductor)
  otherId = "T2" if taskId == "T1" else "T1"
  calls = readCalls(scenarioDir, "write", taskId=taskId)
  assert len(calls) == 2
  firstSession = calls[0]["argv"][calls[0]["argv"].index("--session-id") + 1]
  assert calls[1]["argv"][calls[1]["argv"].index("--resume") + 1] == firstSession
  assert "解合併衝突" in calls[1]["prompt"] and "- shared.txt" in calls[1]["prompt"]
  assert "（第 2 輪）" in calls[1]["prompt"]
  reviews = readCalls(scenarioDir, "review", taskId=taskId)
  assert "這一輪是解合併衝突" in reviews[-1]["prompt"]
  assert "這一輪是解合併衝突" not in reviews[0]["prompt"]
  starts = [event for event in eventsOf(conductor, "taskStarted") if event["task"] == taskId]
  assert starts[-1]["data"]["mode"] == "resolve"
  # 兩個任務的內容都要留在 plan 分支，不能被解衝突蓋掉
  merged = board_git.runGit(conductor.projectDir, "show", "plan/P0001:shared.txt")
  assert f"{taskId} 第 1 次" in merged and f"{otherId} 第 1 次" in merged
  assert (conductor.projectDir / ".board" / "reviews" / f"P0001-{taskId}-r2.md").exists()


def test_unresolvedMarkersCountAsAgentFailure(startBoard):
  conductor, _ = startBoard(twoTasks, {"write": {"T1": ["conflict", "unresolved"], "T2": ["conflict", "unresolved"]}})
  state = runUntil(conductor, lambda current: any(
    task["status"] == TaskStatus.needsYou for task in current["tasks"].values()))
  stuck = next(task for task in state["tasks"].values() if task["status"] == TaskStatus.needsYou)
  assert stuck["needs"]["kind"] == "agentFailed"
  assert "衝突標記" in stuck["needs"]["reason"] and "shared.txt" in stuck["needs"]["reason"]
  assert len(eventsOf(conductor, "agentFailed")) == 2


def test_conflictBeyondAutoLimitNeedsYouThenRetryResolves(startBoard, monkeypatch):
  monkeypatch.setattr(conductorModule, "maxAutoResolves", 0)
  conductor, scenarioDir = startBoard(twoTasks, {"write": {"T1": ["conflict", "resolve"], "T2": ["conflict", "resolve"]}})
  state = runUntil(conductor, lambda current: any(
    task["status"] == TaskStatus.needsYou for task in current["tasks"].values()))
  stuck = next(task for task in state["tasks"].values() if task["status"] == TaskStatus.needsYou)
  assert stuck["needs"]["kind"] == "mergeConflict"
  assert stuck["needs"]["conflicts"] == ["shared.txt"]
  assert eventsOf(conductor, "taskConflicted") == []
  board_core.appendEvent(conductor.projectDir, "userDecision", plan="P0001", task=stuck["task"],
                         data={"action": "retry", "note": "兩邊都留"})
  runUntil(conductor, planDone)
  calls = readCalls(scenarioDir, "write", taskId=stuck["task"])
  assert "解合併衝突" in calls[-1]["prompt"] and "使用者補充：兩邊都留" in calls[-1]["prompt"]


def test_cleanRemergeSkipsClaudeButStillReviews(startBoard, monkeypatch):
  monkeypatch.setattr(conductorModule, "maxAutoResolves", 0)
  conductor, scenarioDir = startBoard(twoTasks, {"write": {"T1": ["conflict"], "T2": ["conflict"]}})
  state = runUntil(conductor, lambda current: any(
    task["status"] == TaskStatus.needsYou for task in current["tasks"].values()))
  taskId = next(task["task"] for task in state["tasks"].values() if task["status"] == TaskStatus.needsYou)
  # 你在任務的 worktree 把檔案改成和 plan 分支一樣，再合併最新的 plan 分支就不會衝突
  taskPath = board_git.taskWorktreePath(conductor.projectDir, "P0001", taskId)
  planContent = board_git.runGit(conductor.projectDir, "show", "plan/P0001:shared.txt")
  (taskPath / "shared.txt").write_text(planContent + "\n", encoding="utf-8")
  board_git.commitAll(taskPath, "改成和 plan 分支一樣")
  board_core.appendEvent(conductor.projectDir, "userDecision", plan="P0001", task=taskId, data={"action": "retry"})
  runUntil(conductor, planDone)
  assert len(readCalls(scenarioDir, "write", taskId=taskId)) == 1
  written = [event for event in eventsOf(conductor, "taskWritten") if event["task"] == taskId]
  assert written[-1]["data"]["mode"] == "resolve"
  assert len(readCalls(scenarioDir, "review", taskId=taskId)) == 2


def test_chineseFileNameMarkersAreStillDetected(startBoard):
  conductor, _ = startBoard(twoTasks, {"write": {"T1": ["conflictcjk", "unresolved"], "T2": ["conflictcjk", "unresolved"]}})
  state = runUntil(conductor, lambda current: planDone(current) or any(
    task["status"] == TaskStatus.needsYou for task in current["tasks"].values()))
  assert not planDone(state)
  assert eventsOf(conductor, "taskConflicted")[0]["data"]["conflicts"] == ["說明.txt"]
  assert all("說明.txt" in event["data"]["reason"] for event in eventsOf(conductor, "agentFailed"))


def test_autoResolveLimitThenNeedsYou(startBoard):
  conductor, _ = startBoard(twoTasks)
  # 不跑 conductor，直接做出兩條會衝突的任務分支，才能控制解過幾次衝突
  projectDir = conductor.projectDir
  board_git.ensurePlanWorktree(projectDir, "P0001", board_git.headCommit(projectDir))
  for taskId in ("T1", "T2"):
    taskPath = board_git.ensureTaskWorktree(projectDir, "P0001", taskId)
    (taskPath / "shared.txt").write_text(f"{taskId}\n", encoding="utf-8")
    board_git.commitAll(taskPath, taskId)
  board_git.mergeTask(projectDir, "P0001", "T1", "合併 T1")
  taskState = conductor.currentState()[1]["tasks"]["P0001/T2"]
  info = {"plan": "P0001", "task": "T2", "round": 1}
  conductor.mergeOneTask(dict(taskState, conflictCount=conductorModule.maxAutoResolves - 1), info)
  assert eventsOf(conductor, "taskConflicted")[-1]["task"] == "T2"
  conductor.mergeOneTask(dict(taskState, conflictCount=conductorModule.maxAutoResolves), info)
  needs = eventsOf(conductor, "needsYou")[-1]["data"]
  assert needs["kind"] == "mergeConflict" and needs["conflicts"] == ["shared.txt"]
  assert f"已自動解過 {conductorModule.maxAutoResolves} 次" in needs["reason"]


def stuckTask(conductor):
  state = runUntil(conductor, lambda current: any(
    task["status"] == TaskStatus.needsYou for task in current["tasks"].values()))
  return next(task for task in state["tasks"].values() if task["status"] == TaskStatus.needsYou)


def test_blockedWhileResolvingThenRetryReusesMerge(startBoard):
  conductor, scenarioDir = startBoard(twoTasks, {"write": {"T1": ["conflict", "blocked", "resolve"],
                                                           "T2": ["conflict", "blocked", "resolve"]}})
  stuck = stuckTask(conductor)
  assert stuck["needs"]["kind"] == "blocked"
  taskPath = board_git.taskWorktreePath(conductor.projectDir, "P0001", stuck["task"])
  assert board_git.mergeInProgress(taskPath)
  board_core.appendEvent(conductor.projectDir, "userDecision", plan="P0001", task=stuck["task"],
                         data={"action": "retry", "note": "繼續解"})
  runUntil(conductor, planDone)
  calls = readCalls(scenarioDir, "write", taskId=stuck["task"])
  assert "--resume" in calls[-1]["argv"]
  assert "- shared.txt" in calls[-1]["prompt"] and "使用者補充：繼續解" in calls[-1]["prompt"]
  assert "<<<<<<<" not in board_git.runGit(conductor.projectDir, "show", "plan/P0001:shared.txt")


def test_stopWhileResolvingThenResumeFinishes(startBoard):
  conductor, _ = startBoard(twoTasks, {"write": {"T1": ["conflict", "hang", "resolve"],
                                                 "T2": ["conflict", "hang", "resolve"]}})
  runUntil(conductor, lambda current: any(task["status"] == TaskStatus.writing and task["resolving"]
                                          for task in current["tasks"].values()))
  board_core.appendEvent(conductor.projectDir, "paused", data={"reason": "測試暫停"})
  runUntil(conductor, lambda current: not conductor.jobs)
  board_core.appendEvent(conductor.projectDir, "resumed")
  runUntil(conductor, planDone)
  merged = board_git.runGit(conductor.projectDir, "show", "plan/P0001:shared.txt")
  assert "<<<<<<<" not in merged and "T1" in merged and "T2" in merged


def test_doneWhileTaskWorktreeStillHasMarkersNeedsYou(startBoard):
  conductor, _ = startBoard(twoTasks, {"write": {"T1": ["conflict", "blocked"], "T2": ["conflict", "blocked"]}})
  stuck = stuckTask(conductor)
  board_core.appendEvent(conductor.projectDir, "userDecision", plan="P0001", task=stuck["task"], data={"action": "done"})
  stuck = stuckTask(conductor)
  assert stuck["needs"]["kind"] == "mergeConflict"
  assert stuck["needs"]["conflicts"] == ["shared.txt"]
  assert "<<<<<<<" not in board_git.runGit(conductor.projectDir, "show", "plan/P0001:shared.txt")


def test_doneAfterResolvingInTaskWorktreeMerges(startBoard):
  conductor, _ = startBoard(twoTasks, {"write": {"T1": ["conflict", "blocked"], "T2": ["conflict", "blocked"]}})
  stuck = stuckTask(conductor)
  taskPath = board_git.taskWorktreePath(conductor.projectDir, "P0001", stuck["task"])
  (taskPath / "shared.txt").write_text("人工在任務 worktree 解完\n", encoding="utf-8")
  board_core.appendEvent(conductor.projectDir, "userDecision", plan="P0001", task=stuck["task"], data={"action": "done"})
  runUntil(conductor, planDone)
  assert board_git.runGit(conductor.projectDir, "show", "plan/P0001:shared.txt") == "人工在任務 worktree 解完"


def test_doneAfterResolvingInPlanWorktreeIgnoresHalfMerge(startBoard):
  conductor, _ = startBoard(twoTasks, {"write": {"T1": ["conflict", "blocked"], "T2": ["conflict", "blocked"]}})
  stuck = stuckTask(conductor)
  planPath = board_git.planWorktreePath(conductor.projectDir, "P0001")
  subprocess.run(["git", "merge", "--no-ff", "-m", "手動合併", f"task/P0001-{stuck['task']}"], cwd=planPath,
                 capture_output=True)
  (planPath / "shared.txt").write_text("人工在 plan worktree 解完\n", encoding="utf-8")
  subprocess.run(["git", "add", "shared.txt"], cwd=planPath, check=True)
  subprocess.run(["git", "commit", "-q", "--no-edit"], cwd=planPath, check=True)
  board_core.appendEvent(conductor.projectDir, "userDecision", plan="P0001", task=stuck["task"], data={"action": "done"})
  runUntil(conductor, planDone)
  assert board_git.runGit(conductor.projectDir, "show", "plan/P0001:shared.txt") == "人工在 plan worktree 解完"
  # 任務 worktree 裡解到一半的合併不能被 commit 進任務分支
  assert "<<<<<<<" not in board_git.runGit(conductor.projectDir, "show", f"task/P0001-{stuck['task']}:shared.txt")


def test_stagedMarkersThenRetryStillAsksClaude(startBoard):
  conductor, scenarioDir = startBoard(twoTasks, {"write": {"T1": ["conflict", "blocked", "resolve"],
                                                           "T2": ["conflict", "blocked", "resolve"]}})
  stuck = stuckTask(conductor)
  taskPath = board_git.taskWorktreePath(conductor.projectDir, "P0001", stuck["task"])
  board_git.runGit(taskPath, "add", "-A")
  board_core.appendEvent(conductor.projectDir, "userDecision", plan="P0001", task=stuck["task"], data={"action": "retry"})
  runUntil(conductor, planDone)
  assert len(readCalls(scenarioDir, "write", taskId=stuck["task"])) == 3
  assert "<<<<<<<" not in board_git.runGit(conductor.projectDir, "show", "plan/P0001:shared.txt")
