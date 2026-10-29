import json
import os
import subprocess
import sys
import time

import board_agents
import board_core
import board_git
from board_core import PlanStatus, TaskStatus
import conductor as conductorModule
from conductor import Conductor, compareWithBaseline, failureNames, formatEvent, qualityResults
from helpers import fakeConfig, fakeRole, makeEvent, readCalls, runUntil

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
  assert {events[index]["agent"] for index in starts} == {"writer", "writer-sub1", "writer-sub2"}
  assert max(starts) < firstWritten


def test_singleTaskStartsOnlyMainWriterAndReviewer(startBoard):
  conductor, _ = startBoard(oneTask)
  runUntil(conductor, planDone)
  assert {event["agent"] for event in eventsOf(conductor, "taskStarted")} == {"writer"}
  assert {event["agent"] for event in eventsOf(conductor, "reviewStarted")} == {"reviewer"}


def test_childRetryUsesFreeMainAndKeepsItsConversation(startBoard):
  conductor, scenarioDir = startBoard(twoTasks, {
    "write": {"T2": ["slow", "done"]}, "review": {"T2": ["fail", "pass"]},
  })
  runUntil(conductor, planDone)
  starts = [event for event in eventsOf(conductor, "taskStarted") if event["task"] == "T2"]
  assert [event["agent"] for event in starts] == ["writer-sub1", "writer"]
  calls = readCalls(scenarioDir, "write", taskId="T2")
  session = calls[0]["argv"][calls[0]["argv"].index("--session-id") + 1]
  assert calls[1]["argv"][calls[1]["argv"].index("--resume") + 1] == session


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
  assert state["tasks"]["P0001/T1"]["needs"]["reason"] == "寫程式的 agent 回報卡住：缺少 API 位址"


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


def noBaselineWorktreeLeft(projectDir):
  root = board_git.worktreesRoot(projectDir)
  leftovers = [path.name for path in root.iterdir() if path.name.endswith("-baseline")] if root.exists() else []
  return leftovers == [] and "-baseline" not in board_git.runGit(projectDir, "worktree", "list", "--porcelain")


def test_testBaselineRunsAtBaseCommitBeforeFirstTaskThenIsRemoved(startBoard):
  conductor, _ = startBoard(chainedTasks, testCommand="git rev-parse HEAD")
  runUntil(conductor, planDone)
  events = board_core.readEvents(conductor.projectDir)
  types = [event["type"] for event in events]
  assert types.count("testBaseline") == 1
  assert types.index("testBaseline") < types.index("taskStarted")
  baseCommit = eventsOf(conductor, "planApproved")[0]["data"]["baseCommit"]
  baseline = eventsOf(conductor, "testBaseline")[0]
  assert baseline["plan"] == "P0001" and baseline["task"] is None
  assert baseline["data"] == {"command": "git rev-parse HEAD", "commit": baseCommit, "passed": True, "failures": [],
                              "qualityChecked": False, "qualityFailures": [], "tail": baseCommit}
  assert conductor.currentState()[1]["plans"]["P0001"]["testBaseline"] == baseline["data"]
  assert noBaselineWorktreeLeft(conductor.projectDir)


def test_failingBaselineStillDispatches(startBoard):
  # 基準 commit 裡沒有 T1.txt，所以基準失敗；任務寫出 T1.txt 後測試通過
  conductor, _ = startBoard(oneTask, testCommand="git cat-file -e HEAD:T1.txt")
  runUntil(conductor, planDone)
  assert eventsOf(conductor, "testBaseline")[0]["data"]["passed"] is False
  assert eventsOf(conductor, "testRun")[0]["data"]["passed"] is True
  assert eventsOf(conductor, "testRun")[0]["data"]["verdict"] == "passed"
  assert noBaselineWorktreeLeft(conductor.projectDir)


def pythonTestCommand(tmp_path, body):
  # 假的測試程式在任務的 worktree 執行；基準的 worktree 在 baseCommit，還沒有 T1.txt
  scriptPath = tmp_path / "fakeTests.py"
  scriptPath.write_text("import pathlib, sys\nwritten = pathlib.Path('T1.txt')\n" + body, encoding="utf-8")
  return f'"{sys.executable}" "{scriptPath}"'


failNewTest = "print('FAILED tests/test_new.py::test_new - AssertionError')\nsys.exit(1)\n"


def test_newFailureGoesBackToWriterWithoutReview(startBoard, tmp_path):
  body = "if written.exists() and '第 1 次' in written.read_text(encoding='utf-8'):\n  " + failNewTest.replace("\n", "\n  ")
  conductor, scenarioDir = startBoard(oneTask, testCommand=pythonTestCommand(tmp_path, body))
  runUntil(conductor, planDone)
  events = board_core.readEvents(conductor.projectDir)
  assert eventsOf(conductor, "testBaseline")[0]["data"]["passed"] is True
  firstRun = next(index for index, event in enumerate(events) if event["type"] == "testRun")
  assert events[firstRun]["data"]["verdict"] == "newFailure"
  assert events[firstRun]["data"]["newFailures"] == ["tests/test_new.py::test_new"]
  assert (events[firstRun + 1]["type"], events[firstRun + 1]["round"]) == ("taskStarted", 2)
  assert "reviewStarted" not in [event["type"] for event in events[:firstRun + 1]]
  assert [event["data"]["verdict"] for event in eventsOf(conductor, "testRun")] == ["newFailure", "passed"]
  assert len(eventsOf(conductor, "reviewStarted")) == 1
  calls = readCalls(scenarioDir, "write")
  assert len(calls) == 2
  assert "（第 2 輪）" in calls[1]["prompt"]
  assert "- tests/test_new.py::test_new" in calls[1]["prompt"]
  assert "FAILED tests/test_new.py::test_new - AssertionError" in calls[1]["prompt"]


def test_testReturnAfterFailedReviewDropsEarlierReview(startBoard, tmp_path):
  # 第 1 輪審查不通過，第 2 輪寫出的內容測試有新增的失敗，第 3 輪只附測試的失敗
  body = "if written.exists() and '第 2 次' in written.read_text(encoding='utf-8'):\n  " + failNewTest.replace("\n", "\n  ")
  conductor, scenarioDir = startBoard(oneTask, {"review": {"T1": ["fail", "pass"]}},
                                      testCommand=pythonTestCommand(tmp_path, body))
  runUntil(conductor, planDone)
  assert [event["data"]["verdict"] for event in eventsOf(conductor, "testRun")] == ["passed", "newFailure", "passed"]
  calls = readCalls(scenarioDir, "write")
  assert "還沒達成需求" in calls[1]["prompt"]
  assert "（第 3 輪）" in calls[2]["prompt"]
  assert "- tests/test_new.py::test_new" in calls[2]["prompt"]
  assert "FAILED tests/test_new.py::test_new - AssertionError" in calls[2]["prompt"]
  assert "還沒達成需求" not in calls[2]["prompt"]


def test_newFailureEveryRoundNeedsYouThenRetryRunsAnotherRound(startBoard, tmp_path):
  body = "if written.exists():\n  " + failNewTest.replace("\n", "\n  ")
  conductor, scenarioDir = startBoard(oneTask, testCommand=pythonTestCommand(tmp_path, body))
  state = runUntil(conductor, taskIs(TaskStatus.needsYou))
  needs = state["tasks"]["P0001/T1"]["needs"]
  assert needs["kind"] == "tests"
  assert "測試有新增的失敗" in needs["reason"] and "tests/test_new.py::test_new" in needs["reason"]
  assert len(readCalls(scenarioDir, "write")) == 3
  assert eventsOf(conductor, "reviewStarted") == []
  board_core.appendEvent(conductor.projectDir, "userDecision", plan="P0001", task="T1", data={"action": "retry"})
  state = runUntil(conductor, lambda current: current["tasks"]["P0001/T1"]["round"] == 4
                   and taskIs(TaskStatus.needsYou)(current))
  assert state["tasks"]["P0001/T1"]["maxRounds"] == 4
  assert [event["round"] for event in eventsOf(conductor, "taskStarted")] == [1, 2, 3, 4]
  assert eventsOf(conductor, "reviewStarted") == []


def test_sameFailuresAsBaselineStillGoToReview(startBoard, tmp_path):
  body = "print('FAILED tests/test_old.py::test_old - AssertionError')\nsys.exit(1)\n"
  conductor, scenarioDir = startBoard(oneTask, testCommand=pythonTestCommand(tmp_path, body))
  runUntil(conductor, planDone)
  assert eventsOf(conductor, "testBaseline")[0]["data"]["failures"] == ["tests/test_old.py::test_old"]
  testRun = eventsOf(conductor, "testRun")[0]
  assert (testRun["data"]["verdict"], testRun["data"]["newFailures"]) == ("preexisting", [])
  assert len(eventsOf(conductor, "reviewStarted")) == 1
  assert len(readCalls(scenarioDir, "write")) == 1
  reviewPrompt = readCalls(scenarioDir, "review")[0]["prompt"]
  assert "基準就有" in reviewPrompt and "- tests/test_old.py::test_old" in reviewPrompt


qualityOutput = ("512 passed in 3.2s\n"
                 "PASS conductor.py:55 failureNames C=4 coverage=100.0% CRAP=4.00\n"
                 "FAIL conductor.py:300 Conductor.mergeOneTask C=16 coverage=90.0% CRAP=16.06\n"
                 "SKIP conductor.py:10 main (no coverage)\n"
                 "FAIL board.py:20 missingPackages C=2 coverage=0.0% CRAP=6.00\n"
                 "FAIL: .quality/report.json\n")


def test_qualityFailuresAreRecordedInBaselineAndTestRun(startBoard, tmp_path):
  body = f"print({qualityOutput!r})\nsys.exit(1)\n"
  conductor, _ = startBoard(oneTask, testCommand=pythonTestCommand(tmp_path, body))
  runUntil(conductor, planDone)
  expected = ["品質門檻 conductor.py::Conductor.mergeOneTask", "品質門檻 board.py::missingPackages"]
  for event in (eventsOf(conductor, "testBaseline")[0], eventsOf(conductor, "testRun")[0]):
    assert event["data"]["qualityChecked"] is True
    assert event["data"]["qualityFailures"] == expected


def test_pytestFailureWithoutQualityLinesIsNotChecked(startBoard, tmp_path):
  body = "print('FAILED tests/test_old.py::test_old - AssertionError')\nprint('FAIL: pytest failed')\nsys.exit(1)\n"
  conductor, _ = startBoard(oneTask, testCommand=pythonTestCommand(tmp_path, body))
  runUntil(conductor, planDone)
  for event in (eventsOf(conductor, "testBaseline")[0], eventsOf(conductor, "testRun")[0]):
    assert (event["data"]["qualityChecked"], event["data"]["qualityFailures"]) == (False, [])


baseQualityOutput = ("512 passed in 3.2s\n"
                     "PASS conductor.py:55 failureNames C=4 coverage=100.0% CRAP=4.00\n"
                     "FAIL conductor.py:300 Conductor.mergeOneTask C=16 coverage=90.0% CRAP=16.06\n"
                     "FAIL board.py:20 missingPackages C=2 coverage=0.0% CRAP=6.00\n"
                     "FAIL board.py:40 main C=8 coverage=0.0% CRAP=72.00\n"
                     "FAIL: .quality/report.json\n")
baseQualityNames = ["品質門檻 conductor.py::Conductor.mergeOneTask", "品質門檻 board.py::missingPackages",
                    "品質門檻 board.py::main"]


def test_sameQualityFailuresAsBaselineGoToReviewWithNames(startBoard, tmp_path):
  body = f"print({baseQualityOutput!r})\nsys.exit(1)\n"
  conductor, scenarioDir = startBoard(oneTask, testCommand=pythonTestCommand(tmp_path, body))
  runUntil(conductor, planDone)
  testRun = eventsOf(conductor, "testRun")[0]
  assert (testRun["data"]["verdict"], testRun["data"]["newFailures"]) == ("preexisting", [])
  assert len(eventsOf(conductor, "reviewStarted")) == 1
  reviewPrompt = readCalls(scenarioDir, "review")[0]["prompt"]
  assert "基準就有" in reviewPrompt
  assert all(f"- {name}" in reviewPrompt for name in baseQualityNames)


def test_newQualityFailureGoesBackToWriter(startBoard, tmp_path):
  extraFail = "FAIL board.py:60 newHelper C=3 coverage=0.0% CRAP=12.00\n"
  body = (f"output = {baseQualityOutput!r}\n"
          "if written.exists() and '第 1 次' in written.read_text(encoding='utf-8'):\n"
          f"  output = {extraFail!r} + output\n"
          "print(output)\nsys.exit(1)\n")
  conductor, _ = startBoard(oneTask, testCommand=pythonTestCommand(tmp_path, body))
  runUntil(conductor, planDone)
  firstRun, secondRun = eventsOf(conductor, "testRun")
  assert (firstRun["data"]["verdict"], firstRun["data"]["newFailures"]) == ("newFailure", ["品質門檻 board.py::newHelper"])
  assert secondRun["data"]["verdict"] == "preexisting"
  assert len(eventsOf(conductor, "reviewStarted")) == 1


def test_qualityResultsReadPassFailLinesWithoutLineNumbers():
  assert qualityResults(qualityOutput) == (True, ["品質門檻 conductor.py::Conductor.mergeOneTask",
                                                  "品質門檻 board.py::missingPackages"])
  moved = qualityOutput.replace("conductor.py:300", "conductor.py:301")
  assert qualityResults(moved) == qualityResults(qualityOutput)
  spacedPath = ("PASS conductor.py:55 failureNames C=4 coverage=100.0% CRAP=4.00\n"
                "FAIL src/new module.py:20 process C=2 coverage=0.0% CRAP=6.00\n")
  assert qualityResults(spacedPath) == (True, ["品質門檻 src/new module.py::process"])
  assert qualityResults(spacedPath.split("\n", 1)[1]) == (True, ["品質門檻 src/new module.py::process"])
  allPassed = "PASS conductor.py:55 failureNames C=4 coverage=100.0% CRAP=4.00\nPASS: .quality/report.json\n"
  assert qualityResults(allPassed) == (True, [])
  # pytest 失敗時品質檢查沒有算，沒有 PASS／FAIL 行
  assert qualityResults("FAILED tests/test_a.py::test_one - AssertionError\nFAIL: pytest failed\n") == (False, [])
  # 一般 pytest 輸出，沒有品質檢查
  assert qualityResults("tests/test_a.py ..\n2 passed in 0.1s\n") == (False, [])


def test_unnamedBaselineFailureIsExplainedToReviewer(startBoard, tmp_path):
  conductor, scenarioDir = startBoard(oneTask, testCommand=pythonTestCommand(tmp_path, "print('boom')\nsys.exit(1)\n"))
  runUntil(conductor, planDone)
  assert eventsOf(conductor, "testRun")[0]["data"]["verdict"] == "unknown"
  reviewPrompt = readCalls(scenarioDir, "review")[0]["prompt"]
  assert "基準就有" in reviewPrompt and "無法比對" in reviewPrompt


def test_changedTestCommandRerunsBaselineAfterRestart(startBoard):
  firstConductor, _ = startBoard(oneTask, {"write": {"T1": ["blocked", "done"]}}, testCommand="git --version")
  runUntil(firstConductor, taskIs(TaskStatus.needsYou))
  sameCommand = Conductor(firstConductor.projectDir, config=fakeConfig(testCommand="git --version"), out=lambda line: None)
  sameCommand.tick()
  assert len(eventsOf(sameCommand, "testBaseline")) == 1
  changed = Conductor(firstConductor.projectDir, config=fakeConfig(testCommand="git rev-parse HEAD"),
                      out=lambda line: None)
  board_core.appendEvent(changed.projectDir, "userDecision", plan="P0001", task="T1", data={"action": "retry"})
  runUntil(changed, planDone)
  events = board_core.readEvents(changed.projectDir)
  baselines = [index for index, event in enumerate(events) if event["type"] == "testBaseline"]
  starts = [index for index, event in enumerate(events) if event["type"] == "taskStarted"]
  assert [events[index]["data"]["command"] for index in baselines] == ["git --version", "git rev-parse HEAD"]
  assert baselines[0] < starts[0] and baselines[1] < starts[1]
  assert noBaselineWorktreeLeft(changed.projectDir)


def test_skipTestsRunsNoBaselineAndDispatches(startBoard):
  conductor, _ = startBoard(oneTask)
  assert conductor.config["skipTests"] is True
  runUntil(conductor, planDone)
  assert eventsOf(conductor, "testBaseline") == []
  assert eventsOf(conductor, "testRun") == []
  assert all(event["data"]["testing"] is False for event in eventsOf(conductor, "taskWritten"))
  assert conductor.currentState()[1]["plans"]["P0001"]["testBaseline"] is None
  assert noBaselineWorktreeLeft(conductor.projectDir)


def test_noDispatchUntilBaselineIsRecorded(startBoard, monkeypatch):
  conductor, _ = startBoard(oneTask, testCommand="git --version")
  lines = []
  conductor.out = lines.append

  def failingBaseline(projectDir, planIdValue, baseCommit):
    raise board_git.GitError("模擬基準 worktree 失敗")

  realBaseline = board_git.addBaselineWorktree
  monkeypatch.setattr(board_git, "addBaselineWorktree", failingBaseline)
  for _ in range(3):
    conductor.tick()
  assert lines.count("P0001 的基準測試 worktree 建立失敗：模擬基準 worktree 失敗") == 1
  assert eventsOf(conductor, "taskStarted") == [] and eventsOf(conductor, "testBaseline") == []
  monkeypatch.setattr(board_git, "addBaselineWorktree", realBaseline)
  runUntil(conductor, planDone)
  types = [event["type"] for event in board_core.readEvents(conductor.projectDir)]
  assert types.index("testBaseline") < types.index("taskStarted")


def test_noReviewUntilBaselineForChangedCommandIsRecorded(startBoard, monkeypatch):
  firstConductor, _ = startBoard(oneTask, {"review": {"T1": ["hang", "pass"]}}, testCommand="git --version")
  runUntil(firstConductor, taskIs(TaskStatus.reviewing))
  # 暫停會中斷審查，任務回到待審查
  board_core.appendEvent(firstConductor.projectDir, "paused", data={"reason": "測試暫停"})
  runUntil(firstConductor, lambda current: not firstConductor.jobs and taskIs(TaskStatus.toReview)(current))
  board_core.appendEvent(firstConductor.projectDir, "resumed")
  lines = []
  restarted = Conductor(firstConductor.projectDir, config=fakeConfig(testCommand="git rev-parse HEAD"), out=lines.append)

  def failingBaseline(projectDir, planIdValue, baseCommit):
    raise board_git.GitError("模擬基準 worktree 失敗")

  realBaseline = board_git.addBaselineWorktree
  monkeypatch.setattr(board_git, "addBaselineWorktree", failingBaseline)
  for _ in range(3):
    restarted.tick()
  assert lines.count("P0001 的基準測試 worktree 建立失敗：模擬基準 worktree 失敗") == 1
  assert len(eventsOf(restarted, "reviewStarted")) == 1
  assert not restarted.jobs
  assert restarted.currentState()[1]["tasks"]["P0001/T1"]["status"] == TaskStatus.toReview
  monkeypatch.setattr(board_git, "addBaselineWorktree", realBaseline)
  runUntil(restarted, planDone)
  events = board_core.readEvents(restarted.projectDir)
  baselines = [index for index, event in enumerate(events) if event["type"] == "testBaseline"]
  reviews = [index for index, event in enumerate(events) if event["type"] == "reviewStarted"]
  assert [events[index]["data"]["command"] for index in baselines] == ["git --version", "git rev-parse HEAD"]
  assert baselines[1] < reviews[1]


def test_tddCrapAddsStepsToPromptsAndAllowsPytest(startBoard):
  conductor, scenarioDir = startBoard(oneTask, testCommand="git --version", tddCrap=True)
  runUntil(conductor, planDone)
  write = readCalls(scenarioDir, "write")[0]
  assert "tdd-crap" in write["prompt"] and "git --version" in write["prompt"]
  assert "Bash(python -m pytest*)" in write["argv"] and "PowerShell(python -m pytest*)" in write["argv"]
  assert ".quality/report.json" in readCalls(scenarioDir, "review")[0]["prompt"]


projectTddCommands = {"testCommand": "git --version", "tddCrap": True, "tddSkill": ".agents/skills/tdd-crap",
                      "pythonCommand": '"project-python"', "writerTestCommand": "git --version --build-options",
                      "writerPythonCommand": '& "project-python"'}


def test_claudeWriterGetsBashCommandsWithoutChangingRunner(startBoard):
  conductor, scenarioDir = startBoard(oneTask, **projectTddCommands)
  runUntil(conductor, planDone)
  write = readCalls(scenarioDir, "write")[0]
  # PowerShell 的 & "..." 呼叫比對不到允許規則，Claude 改拿 Bash 寫法（P0008 D4）
  assert "git --version" in write["prompt"] and "git --version --build-options" not in write["prompt"]
  assert '"project-python" -m pytest' in write["prompt"] and '& "project-python"' not in write["prompt"]
  assert any("用 Bash 工具執行" in arg for arg in write["argv"])
  assert 'Bash("project-python" -m pytest*)' in write["argv"]
  assert "SKILL.md" in write["prompt"] and ".agents" in write["prompt"]
  assert eventsOf(conductor, "testRun")[0]["data"]["passed"] is True


def test_codexWriterKeepsPowerShellCommands(startBoard):
  conductor, scenarioDir = startBoard(oneTask, writer=fakeRole("codex"), reviewer=fakeRole("claude"),
                                      **projectTddCommands)
  runUntil(conductor, planDone)
  write = readCalls(scenarioDir, "write")[0]
  assert "git --version --build-options" in write["prompt"]
  assert '& "project-python" -m pytest' in write["prompt"]
  assert "用 Bash 工具執行" not in write["prompt"]


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


def sleepyTestCommand(tmp_path, sleeps):
  # 假的測試在開始、結束時各記一行到 testLog.txt，照任務 worktree 的名稱睡幾秒；基準的 worktree 不睡
  logPath = tmp_path / "testLog.txt"
  scriptPath = tmp_path / "sleepyTests.py"
  scriptPath.write_text(
    "import os, pathlib, sys, time\n"
    f"log, sleeps = pathlib.Path({str(logPath)!r}), {sleeps!r}\n"
    "name = pathlib.Path.cwd().name\n"
    "with open(log, 'a', encoding='utf-8') as file: file.write(f'start {name} {os.getpid()}\\n')\n"
    "time.sleep(sleeps.get(name, 0))\n"
    "with open(log, 'a', encoding='utf-8') as file: file.write(f'end {name} {os.getpid()}\\n')\n", encoding="utf-8")
  return f'"{sys.executable}" "{scriptPath}"', logPath


def taskTestLog(logPath, taskId="T1"):
  lines = logPath.read_text(encoding="utf-8").splitlines() if logPath.exists() else []
  return [line.split() for line in lines if line.split()[1] == f"P0001-{taskId}"]


def taskTestStarted(logPath, count=1):
  return lambda state: len([entry for entry in taskTestLog(logPath) if entry[0] == "start"]) >= count


def test_testsRunInBackgroundInParallelWithoutBlockingOtherTasks(startBoard, tmp_path):
  command, logPath = sleepyTestCommand(tmp_path, {"P0001-T1": 6, "P0001-T2": 1.5})
  conductor, _ = startBoard(twoTasks, {"write": {"T2": ["slow"]}}, testCommand=command)
  runUntil(conductor, taskIs(TaskStatus.testing))
  runUntil(conductor, planDone)
  assert all(event["data"]["testing"] is True for event in eventsOf(conductor, "taskWritten"))
  order = [tuple(line.split()[:2]) for line in logPath.read_text(encoding="utf-8").splitlines()]
  # 兩個任務的測試期間重疊：T2 的測試在 T1 的測試結束（之後才寫 testRun）之前就開始
  assert order.index(("start", "P0001-T2")) < order.index(("end", "P0001-T1"))
  events = [(event["type"], event["task"]) for event in board_core.readEvents(conductor.projectDir)]
  assert events.index(("reviewStarted", "T2")) < events.index(("testRun", "T1"))


def test_pauseWhileTestingRerunsTestsWithoutInterruption(startBoard, tmp_path):
  command, logPath = sleepyTestCommand(tmp_path, {"P0001-T1": 3})
  conductor, _ = startBoard(oneTask, testCommand=command)
  runUntil(conductor, taskTestStarted(logPath))
  board_core.appendEvent(conductor.projectDir, "paused", data={"reason": "測試暫停"})
  state = runUntil(conductor, lambda current: not conductor.jobs)
  assert state["tasks"]["P0001/T1"]["status"] == TaskStatus.testing
  board_core.appendEvent(conductor.projectDir, "resumed")
  runUntil(conductor, planDone)
  assert len(eventsOf(conductor, "testRun")) == 1
  assert eventsOf(conductor, "taskInterrupted") == []
  assert [entry[0] for entry in taskTestLog(logPath)] == ["start", "start", "end"]


def test_abandonWhileTestingKillsTestsWithoutTestRun(startBoard, tmp_path):
  command, logPath = sleepyTestCommand(tmp_path, {"P0001-T1": 5})
  conductor, _ = startBoard(oneTask, testCommand=command)
  runUntil(conductor, taskTestStarted(logPath))
  testPid = int(taskTestLog(logPath)[0][2])
  board_core.appendEvent(conductor.projectDir, "planAbandoned", plan="P0001")
  runUntil(conductor, lambda current: not conductor.jobs)
  deadline = time.time() + 10
  while conductorModule.processAlive(testPid) and time.time() < deadline:
    time.sleep(0.1)
  assert not conductorModule.processAlive(testPid)
  for _ in range(3):
    conductor.tick()
  assert eventsOf(conductor, "testRun") == []
  assert [entry[0] for entry in taskTestLog(logPath)] == ["start"]


def test_restartWhileTestingRerunsTests(startBoard, tmp_path):
  command, logPath = sleepyTestCommand(tmp_path, {"P0001-T1": 3})
  conductor, _ = startBoard(oneTask, testCommand=command)
  runUntil(conductor, taskTestStarted(logPath))
  # 和 Ctrl+C 結束 Alfred 時一樣
  conductor.stopJobs(list(conductor.jobs), recordInterruption=True)
  assert conductor.currentState()[1]["tasks"]["P0001/T1"]["status"] == TaskStatus.testing
  restarted = Conductor(conductor.projectDir, config=fakeConfig(testCommand=command), out=lambda line: None)
  runUntil(restarted, planDone)
  assert len(eventsOf(restarted, "testRun")) == 1
  assert eventsOf(restarted, "taskInterrupted") == []
  assert [entry[0] for entry in taskTestLog(logPath)] == ["start", "start", "end"]


def baselineStarted(logPath):
  return lambda state: bool(taskTestLog(logPath, "baseline"))


def processEnded(pid):
  deadline = time.time() + 10
  while conductorModule.processAlive(pid) and time.time() < deadline:
    time.sleep(0.1)
  return not conductorModule.processAlive(pid)


def test_abandonWhileBaselineRunsKillsItWithoutDispatch(startBoard, tmp_path):
  command, logPath = sleepyTestCommand(tmp_path, {"P0001-baseline": 5})
  conductor, _ = startBoard(oneTask, testCommand=command)
  runUntil(conductor, baselineStarted(logPath))
  testPid = int(taskTestLog(logPath, "baseline")[0][2])
  board_core.appendEvent(conductor.projectDir, "planAbandoned", plan="P0001")
  conductor.tick()
  assert not conductor.jobs
  assert processEnded(testPid)
  assert noBaselineWorktreeLeft(conductor.projectDir)
  for _ in range(3):
    conductor.tick()
  assert eventsOf(conductor, "testBaseline") == [] and eventsOf(conductor, "taskStarted") == []
  assert [entry[0] for entry in taskTestLog(logPath, "baseline")] == ["start"]


def test_pauseWhileBaselineRunsKillsItThenResumeRerunsBaseline(startBoard, tmp_path):
  command, logPath = sleepyTestCommand(tmp_path, {"P0001-baseline": 3})
  conductor, _ = startBoard(oneTask, testCommand=command)
  runUntil(conductor, baselineStarted(logPath))
  testPid = int(taskTestLog(logPath, "baseline")[0][2])
  board_core.appendEvent(conductor.projectDir, "paused", data={"reason": "測試暫停"})
  conductor.tick()
  assert not conductor.jobs
  assert processEnded(testPid)
  assert noBaselineWorktreeLeft(conductor.projectDir)
  assert eventsOf(conductor, "testBaseline") == [] and eventsOf(conductor, "taskInterrupted") == []
  board_core.appendEvent(conductor.projectDir, "resumed")
  runUntil(conductor, planDone)
  types = [event["type"] for event in board_core.readEvents(conductor.projectDir)]
  assert types.count("testBaseline") == 1
  assert types.index("testBaseline") < types.index("taskStarted")
  assert [entry[0] for entry in taskTestLog(logPath, "baseline")] == ["start", "start", "end"]


def test_otherPlanDispatchesWhileBaselineRuns(startBoard, writePlan, gitProject, tmp_path):
  import board as boardCli

  command, logPath = sleepyTestCommand(tmp_path, {"P0002-baseline": 10})
  conductor, _ = startBoard(oneTask, testCommand=command)
  writePlan("0002-other.md", oneTask, planId="P0002", projectDir=gitProject)
  assert boardCli.cmdSubmit(gitProject, "P0002") == 0
  assert boardCli.cmdApprove(gitProject, "P0002", confirm=lambda prompt: "y") == 0
  runUntil(conductor, lambda state: eventsOf(conductor, "taskStarted"))
  assert [event["plan"] for event in eventsOf(conductor, "taskStarted")] == ["P0001"]
  assert [event["plan"] for event in eventsOf(conductor, "testBaseline")] == ["P0001"]
  logLines = [line.split()[:2] for line in logPath.read_text(encoding="utf-8").splitlines()]
  assert ["start", "P0002-baseline"] in logLines and ["end", "P0002-baseline"] not in logLines


def test_stopJobsWhileBaselineRunsKillsItAndRemovesWorktree(startBoard, tmp_path):
  command, logPath = sleepyTestCommand(tmp_path, {"P0001-baseline": 5})
  conductor, _ = startBoard(oneTask, testCommand=command)
  runUntil(conductor, baselineStarted(logPath))
  testPid = int(taskTestLog(logPath, "baseline")[0][2])
  # 和 Ctrl+C 結束 Alfred 時一樣
  conductor.stopJobs(list(conductor.jobs), recordInterruption=True)
  assert not conductor.jobs
  assert processEnded(testPid)
  assert noBaselineWorktreeLeft(conductor.projectDir)
  assert eventsOf(conductor, "testBaseline") == [] and eventsOf(conductor, "taskInterrupted") == []


def test_stopJobsAfterBaselineFinishedBeforeCollectRemovesWorktree(startBoard):
  conductor, _ = startBoard(oneTask, testCommand="git --version")
  conductor.tick()
  job = conductor.jobs["P0001/baseline"]["job"]
  job.wait(10)
  assert job.poll()
  # 測試在兩輪 tick 之間跑完、還沒被收集時按 Ctrl+C，退出後不會再有 tick 清理
  conductor.stopJobs(list(conductor.jobs), recordInterruption=True)
  assert not conductor.jobs
  assert noBaselineWorktreeLeft(conductor.projectDir)
  assert eventsOf(conductor, "testBaseline") == [] and eventsOf(conductor, "taskInterrupted") == []


def test_baselineErrorIsNotifiedOnceAndRetried(startBoard):
  conductor, _ = startBoard(oneTask, testCommand="git --version")
  lines = []
  conductor.out = lines.append
  calls = []

  def failingRun(cwd, onStarted=None):
    calls.append(cwd)
    raise OSError("模擬測試啟動失敗")

  conductor.runTestCommand = failingRun
  runUntil(conductor, lambda state: len(calls) >= 3)
  assert lines.count("P0001 的基準測試執行失敗：模擬測試啟動失敗") == 1
  assert eventsOf(conductor, "testBaseline") == [] and eventsOf(conductor, "taskStarted") == []
  del conductor.runTestCommand
  runUntil(conductor, planDone)
  types = [event["type"] for event in board_core.readEvents(conductor.projectDir)]
  assert types.count("testBaseline") == 1
  assert types.index("testBaseline") < types.index("taskStarted")
  assert noBaselineWorktreeLeft(conductor.projectDir)


def test_baselineWorktreeRemovalFailureKeepsResult(startBoard, tmp_path, monkeypatch):
  command, logPath = sleepyTestCommand(tmp_path, {"P0001-baseline": 1})
  conductor, _ = startBoard(oneTask, testCommand=command)
  lines = []
  conductor.out = lines.append
  runUntil(conductor, baselineStarted(logPath))

  def failingRemove(projectDir, planIdValue):
    raise board_git.GitError("模擬移除失敗")

  monkeypatch.setattr(board_git, "removeBaselineWorktree", failingRemove)
  runUntil(conductor, lambda state: eventsOf(conductor, "taskStarted"))
  assert lines.count("P0001 的基準測試 worktree 移除失敗：模擬移除失敗") == 1
  assert len(eventsOf(conductor, "testBaseline")) == 1


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
    "open(sys.argv[1], 'a').write(f'{child.pid}\\n')\n", encoding="utf-8")
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
    # 基準測試和任務測試各會留下一個脫離的孫程式
    if pidPath.exists():
      for pid in pidPath.read_text().split():
        board_agents.killProcessTree(int(pid))


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
  event = makeEvent("reviewFailed", plan="P0001", task="T2", roundNumber=1, agent="reviewer-sub1", mustFix=2)
  event["time"] = "2026-10-25T21:40:12+08:00"
  assert formatEvent(event) == "21:40:12 P0001 T2 reviewer-sub1 審查不通過（第 1 輪，必改 2 條）"
  paused = makeEvent("paused", reason="你執行了 stop")
  paused["time"] = "2026-10-25T21:41:00+08:00"
  assert formatEvent(paused) == "21:41:00 全部暫停（你執行了 stop）"
  baseline = makeEvent("testBaseline", plan="P0001", command="pytest -q", passed=False, tail="1 failed")
  baseline["time"] = "2026-10-25T21:42:00+08:00"
  assert formatEvent(baseline) == "21:42:00 P0001 跑基準測試（失敗）"


def test_failureNamesReadPytestShortSummary():
  output = ("tests/test_a.py F.E\n"
            "=========================== short test summary info ===========================\n"
            "FAILED tests/test_a.py::test_one - AssertionError: 1 != 2\n"
            "ERROR tests/test_b.py - ImportError: no module\n"
            "FAILED tests/test_a.py::test_two\n"
            "FAILED tests/test_a.py::test_one - AssertionError: again\n"
            "  FAILED indented line is not a summary\n")
  assert failureNames(output) == ["tests/test_a.py::test_one", "tests/test_b.py", "tests/test_a.py::test_two"]
  assert failureNames("1 failed in 0.1s") == []


def test_compareWithBaselineFollowsPlanRules():
  passedBaseline = {"passed": True, "failures": []}
  namedBaseline = {"passed": False, "failures": ["t::old"]}
  unnamedBaseline = {"passed": False, "failures": []}
  assert compareWithBaseline(namedBaseline, True, []) == ("passed", [])
  assert compareWithBaseline(passedBaseline, False, []) == ("newFailure", [])
  assert compareWithBaseline(passedBaseline, False, ["t::new"]) == ("newFailure", ["t::new"])
  assert compareWithBaseline(namedBaseline, False, ["t::old", "t::new"]) == ("newFailure", ["t::new"])
  assert compareWithBaseline(namedBaseline, False, []) == ("newFailure", [])
  assert compareWithBaseline(namedBaseline, False, ["t::old"]) == ("preexisting", [])
  assert compareWithBaseline(unnamedBaseline, False, ["t::new"]) == ("unknown", [])
  # 沒有 failures 欄位的基準事件當成分不出名稱
  assert compareWithBaseline({"passed": False}, False, ["t::new"]) == ("unknown", [])


def test_compareWithBaselineComparesQualityFailures():
  old = ["品質門檻 a.py::one", "品質門檻 a.py::two", "品質門檻 b.py::three"]
  new = "品質門檻 c.py::four"
  qualityBaseline = {"passed": False, "failures": [], "qualityChecked": True, "qualityFailures": old}
  # 基準 pytest 全過、品質 3 個超標，任務一樣：基準就有
  assert compareWithBaseline(qualityBaseline, False, [], True, old) == ("preexisting", [])
  assert compareWithBaseline(qualityBaseline, False, [], True, old[:2]) == ("preexisting", [])
  # 任務多 1 個超標：只列新增的那個
  assert compareWithBaseline(qualityBaseline, False, [], True, old + [new]) == ("newFailure", [new])
  # 任務 pytest 有失敗（品質沒算）：基準 pytest 全過，失敗都是新增的
  assert compareWithBaseline(qualityBaseline, False, ["t::new"], False, []) == ("newFailure", ["t::new"])
  assert compareWithBaseline(qualityBaseline, False, [], False, []) == ("newFailure", [])
  # 基準 pytest 有失敗、品質沒算，任務 pytest 全過、品質超標：比不了品質，不是新增也不是通過
  pytestBaseline = {"passed": False, "failures": ["t::old"], "qualityChecked": False, "qualityFailures": []}
  assert compareWithBaseline(pytestBaseline, False, [], True, old) == ("unknown", [])
  assert compareWithBaseline(pytestBaseline, False, ["t::old"], False, []) == ("preexisting", [])
  assert compareWithBaseline(pytestBaseline, False, ["t::old", "t::new"], False, []) == ("newFailure", ["t::new"])
  # pytest 無法比對、品質也沒有新增時維持 unknown
  unnamedBaseline = {"passed": False, "failures": [], "qualityChecked": False, "qualityFailures": []}
  assert compareWithBaseline(unnamedBaseline, False, ["t::x"], False, []) == ("unknown", [])
  assert compareWithBaseline(unnamedBaseline, False, [], True, old) == ("unknown", [])
  # 基準全部通過且有檢查品質：品質超標都是新增的
  passedBaseline = {"passed": True, "failures": [], "qualityChecked": True, "qualityFailures": []}
  assert compareWithBaseline(passedBaseline, False, [], True, [new]) == ("newFailure", [new])
  # 基準通過但沒有檢查品質：品質比不了，不判定為新增，也不是通過
  assert compareWithBaseline({"passed": True, "failures": [], "qualityChecked": False}, False, [], True, [new]) \
    == ("unknown", [])
  assert compareWithBaseline({"passed": True, "failures": []}, False, [], True, [new]) == ("unknown", [])


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


def test_agentsReceiveTextOutsideTable(startBoard):
  ruleTable = "## 規則表\n\n| 輸入 | 輸出 |\n|---|---|\n| a | 1 |\n| b | 2 |"
  conductor, scenarioDir = startBoard(twoTasks, {"write": {"T1": ["conflict", "resolve"], "T2": ["conflict", "resolve"]}},
                                      planAfter=ruleTable)
  runUntil(conductor, planDone)
  taskId = conflictedTask(conductor)
  writes = readCalls(scenarioDir, "write", taskId=taskId)
  assert "解合併衝突" not in writes[0]["prompt"] and "解合併衝突" in writes[1]["prompt"]
  calls = writes + readCalls(scenarioDir, "review", taskId=taskId)
  assert len(calls) >= 4
  for call in calls:
    assert "plan 裡表格以外的說明（整份 plan 共用）：" in call["prompt"]
    assert ruleTable in call["prompt"]


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
  conductor, scenarioDir = startBoard(twoTasks, {"write": {"T1": ["conflict", "hang", "resolve"],
                                                 "T2": ["conflict", "hang", "resolve"]}})
  runUntil(conductor, lambda current: any(task["status"] == TaskStatus.writing and task["resolving"]
                                          for task in current["tasks"].values()))
  resolvingTask = next(info["task"] for info in conductor.jobs.values() if info.get("mode") == "resolve")
  runUntil(conductor, lambda state: (scenarioDir / f"call-write-P0001-{resolvingTask}-2.json").exists())
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


def test_codexWriterResumesAndClaudeReviews(startBoard):
  conductor, scenarioDir = startBoard(oneTask, {"review": {"T1": ["fail", "pass"]}},
                                      writer=fakeRole("codex", model="gpt-6.1-sol", reasoningEffort="high"),
                                      reviewer=fakeRole("claude", model="opus", reasoningEffort="max"))
  runUntil(conductor, planDone)
  first, second = readCalls(scenarioDir, "write")
  assert first["argv"][first["argv"].index("-s") + 1] == "workspace-write"
  assert first["argv"][first["argv"].index("-m") + 1] == "gpt-6.1-sol"
  assert first["prompt"].startswith("你是自動流程裡負責寫程式的 agent。")
  assert second["argv"][:2] == ["exec", "resume"] and second["argv"][-2] == "thread-T1-1"
  assert 'model_reasoning_effort="high"' in first["argv"] and 'model_reasoning_effort="high"' in second["argv"]
  assert "還沒達成需求" in second["prompt"]
  review = readCalls(scenarioDir, "review")[0]
  assert review["argv"][:2] == ["--model", "opus"]
  assert review["argv"][review["argv"].index("--effort") + 1] == "max"
  assert review["argv"][review["argv"].index("--permission-mode") + 1] == "manual"
  assert review["prompt"].startswith("審查 plan P0001")
  assert board_git.runGit(conductor.projectDir, "show", "plan/P0001:T1.txt") == "T1 第 2 次"
  starts = eventsOf(conductor, "taskStarted")
  assert [event["data"]["cli"] for event in starts] == ["codex", "codex"]
  assert starts[0]["data"]["sessionId"] is None and starts[1]["data"]["sessionId"] == "thread-T1-1"
  assert {event["agent"] for event in eventsOf(conductor, "reviewStarted")} == {"reviewer"}
  assert {event["data"]["cli"] for event in eventsOf(conductor, "reviewStarted")} == {"claude"}


def test_claudeWriterAndCodexReviewerUseConfiguredEffort(startBoard):
  conductor, scenarioDir = startBoard(oneTask, writer=fakeRole("claude", model="opus", reasoningEffort="high"),
                                     reviewer=fakeRole("codex", reasoningEffort="low"))
  runUntil(conductor, planDone)
  write, review = readCalls(scenarioDir, "write")[0], readCalls(scenarioDir, "review")[0]
  assert write["argv"][write["argv"].index("--effort") + 1] == "high"
  assert 'model_reasoning_effort="low"' in review["argv"]
  assert eventsOf(conductor, "taskStarted")[0]["data"]["reasoningEffort"] == "high"
  assert eventsOf(conductor, "reviewStarted")[0]["data"]["reasoningEffort"] == "low"


def test_switchingWriterCliStartsNewSession(startBoard):
  conductor, scenarioDir = startBoard(oneTask, {"write": {"T1": ["blocked", "done"]}})
  runUntil(conductor, taskIs(TaskStatus.needsYou))
  switched = Conductor(conductor.projectDir, config=fakeConfig(writer=fakeRole("codex")), out=lambda line: None)
  board_core.appendEvent(conductor.projectDir, "userDecision", plan="P0001", task="T1", data={"action": "retry"})
  runUntil(switched, planDone)
  second = readCalls(scenarioDir, "write")[1]
  assert second["argv"][:2] == ["exec", "-C"]
  assert eventsOf(switched, "taskStarted")[-1]["data"]["resume"] is False


def test_codexWriterReadsTddSkillAndHasNoAllowedTools(startBoard):
  conductor, scenarioDir = startBoard(oneTask, writer=fakeRole("codex"), testCommand="git --version", tddCrap=True)
  runUntil(conductor, planDone)
  write = readCalls(scenarioDir, "write")[0]
  assert "SKILL.md" in write["prompt"] and "Skill 工具" not in write["prompt"]
  assert "--allowedTools" not in write["argv"]


def test_writerAllowedToolsReachClaude(startBoard):
  conductor, scenarioDir = startBoard(oneTask, writer=fakeRole("claude", allowedTools=["Bash(npm install*)",
                                                                                     "PowerShell(npm install*)"]))
  runUntil(conductor, planDone)
  argv = readCalls(scenarioDir, "write")[0]["argv"]
  allowedIndex = argv.index("--allowedTools")
  assert argv[allowedIndex + 1:allowedIndex + 3] == ["Bash(npm install*)", "PowerShell(npm install*)"]


def writeConfigFile(projectDir, config):
  (projectDir / ".board" / "config.json").write_text(json.dumps(config), encoding="utf-8")


def watchingConductor(projectDir, lines, **configOverrides):
  # 沒有直接給設定、從 .board/config.json 讀的 conductor 才會每一輪重讀
  writeConfigFile(projectDir, fakeConfig(**configOverrides))
  return Conductor(projectDir, out=lines.append)


def test_roleChangeAppliesToNextAgentWithNewSession(startBoard):
  firstConductor, scenarioDir = startBoard(oneTask, {"review": {"T1": ["fail", "pass"]}})
  lines = []
  conductor = watchingConductor(firstConductor.projectDir, lines, writer=fakeRole("claude", model="opus"))
  runUntil(conductor, taskIs(TaskStatus.reviewing))
  writeConfigFile(conductor.projectDir, fakeConfig(writer=fakeRole("claude", model="sonnet")))
  runUntil(conductor, planDone)
  first, second = readCalls(scenarioDir, "write")
  assert first["argv"][:2] == ["--model", "opus"] and second["argv"][:2] == ["--model", "sonnet"]
  # 換了模型就開新對話：不確定接續舊對話時能不能換模型
  assert "--resume" not in second["argv"]
  starts = eventsOf(conductor, "taskStarted")
  assert [(event["data"]["model"], event["data"]["resume"]) for event in starts] == [("opus", False), ("sonnet", False)]
  assert lines.count("設定變更：writer 改用 Claude（sonnet），從下一個新啟動的 agent 開始用") == 1


def test_brokenConfigKeepsPreviousRolesAndWarnsOnce(startBoard):
  firstConductor, scenarioDir = startBoard(oneTask)
  lines = []
  conductor = watchingConductor(firstConductor.projectDir, lines)
  # cli 寫錯，但 command 仍指向假 agent：就算沒擋下也不會啟動真的 CLI
  writeConfigFile(conductor.projectDir, fakeConfig(writer={**fakeRole("claude"), "cli": "gemini"}))
  runUntil(conductor, planDone)
  warning = "警告｜.board/config.json 有問題，繼續用原本的寫程式、審查設定：writer.cli 只能是 claude 或 codex，現在是 gemini"
  assert lines.count(warning) == 1
  assert [event["data"]["cli"] for event in eventsOf(conductor, "taskStarted")] == ["claude"]
  (conductor.projectDir / ".board" / "config.json").write_text("{壞掉", encoding="utf-8")
  conductor.refreshRoles()
  assert conductor.roles["writer"]["spec"]["cli"] == "claude"
  assert lines[-1].startswith("警告｜.board/config.json 有問題，繼續用原本的寫程式、審查設定：讀不懂：")


def test_reviewRetryKeepsSettingsOfFirstAttempt(startBoard):
  firstConductor, scenarioDir = startBoard(oneTask, {"review": {"T1": ["invalid", "pass"]}})
  lines = []
  conductor = watchingConductor(firstConductor.projectDir, lines, reviewer=fakeRole("codex", model="m1"))
  runUntil(conductor, taskIs(TaskStatus.reviewing))
  writeConfigFile(conductor.projectDir, fakeConfig(reviewer=fakeRole("claude", model="opus")))
  runUntil(conductor, planDone)
  first, second = readCalls(scenarioDir, "review")
  # 格式不符的重審和第一次是同一個審查，沿用同一組設定
  assert second["argv"][:2] == ["exec", "-C"] and second["argv"][second["argv"].index("-m") + 1] == "m1"
  reviews = eventsOf(conductor, "reviewStarted")
  assert [(event["data"]["cli"], event["data"]["model"]) for event in reviews] == [("codex", "m1")]


def test_badAllowedToolsWhileRunningKeepsPreviousRoles(startBoard):
  firstConductor, scenarioDir = startBoard(oneTask)
  lines = []
  conductor = watchingConductor(firstConductor.projectDir, lines)
  writeConfigFile(conductor.projectDir, fakeConfig(writer=fakeRole("claude", allowedTools=True)))
  runUntil(conductor, planDone)
  warning = ('警告｜.board/config.json 有問題，繼續用原本的寫程式、審查設定：'
             'writer.allowedTools 要是文字陣列，例如 ["Bash(npm install*)"]')
  assert lines.count(warning) == 1


def lingeringChildrenAlive(scenarioDir):
  return [pid for pid in (int(path.read_text()) for path in scenarioDir.glob("linger-*.pid")) if processAlive(pid)]


def processAlive(pid):
  if sys.platform == "win32":
    return str(pid) in subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True,
                                      errors="replace").stdout
  try:
    os.kill(pid, 0)
    return True
  except OSError:
    return False


def test_writerThatLingersAfterResultIsCollectedAndResumed(startBoard, monkeypatch):
  monkeypatch.setattr(board_agents, "resultGraceSeconds", 0.5)
  # 第一輪交出結果後留著背景程式不退出；審查退回後第二輪要接回同一個對話
  conductor, scenarioDir = startBoard(oneTask, {"write": {"T1": ["linger", "done"]}, "review": {"T1": ["fail", "pass"]}})
  runUntil(conductor, planDone, timeout=40)
  assert len(eventsOf(conductor, "taskWritten")) == 2
  assert eventsOf(conductor, "taskInterrupted") == [] and eventsOf(conductor, "agentFailed") == []
  calls = readCalls(scenarioDir, "write")
  firstSession = calls[0]["argv"][calls[0]["argv"].index("--session-id") + 1]
  assert calls[1]["argv"][calls[1]["argv"].index("--resume") + 1] == firstSession
  time.sleep(0.5)
  assert lingeringChildrenAlive(scenarioDir) == []


def test_reviewerThatLingersAfterResultIsCollected(startBoard, monkeypatch):
  monkeypatch.setattr(board_agents, "resultGraceSeconds", 0.5)
  conductor, scenarioDir = startBoard(oneTask, {"review": {"T1": ["linger"]}}, reviewer=fakeRole("claude"))
  runUntil(conductor, planDone, timeout=40)
  assert len(eventsOf(conductor, "reviewPassed")) == 1
  assert eventsOf(conductor, "taskInterrupted") == [] and eventsOf(conductor, "agentFailed") == []
  time.sleep(0.5)
  assert lingeringChildrenAlive(scenarioDir) == []


def test_errorResultThatLingersIsRecordedAsAgentFailed(startBoard, monkeypatch):
  monkeypatch.setattr(board_agents, "resultGraceSeconds", 0.5)
  conductor, _ = startBoard(oneTask, {"write": {"T1": ["lingererror", "done"]}})
  runUntil(conductor, planDone, timeout=40)
  failed = eventsOf(conductor, "agentFailed")
  assert len(failed) == 1 and "假的錯誤結果" in failed[0]["data"]["reason"]
  assert eventsOf(conductor, "taskInterrupted") == []
