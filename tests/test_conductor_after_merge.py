import subprocess
import sys
import time

import board_core
import board_git
from board_core import PlanStatus, TaskStatus
import conductor as conductorModule
from conductor import Conductor, formatEvent, mergeTestOutcome
from helpers import fakeConfig, readCalls, runUntil

oneTask = ["| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | | 驗證一 |"]
chainedTasks = oneTask + ["| R1 | | D1 | | T2 任務二 | T1 | 驗證二 |"]
threeTasks = oneTask + ["| R1 | | D1 | | T2 任務二 | | 驗證二 |", "| R1 | | D1 | | T3 任務三 | | 驗證三 |"]


def record(commit="m1", passed=False, failures=(), qualityChecked=False, qualityFailures=()):
  return {"commit": commit, "command": "pytest", "passed": passed, "failures": list(failures),
          "qualityChecked": qualityChecked, "qualityFailures": list(qualityFailures), "tail": ""}


passedBefore = {"commit": "base", "passed": True, "failures": [], "qualityChecked": False, "qualityFailures": []}
failedBefore = {"commit": "base", "passed": False, "failures": ["t::old"], "qualityChecked": False, "qualityFailures": []}


def test_outcomeUnknownWhenNoNamesOrNothingToCompare():
  for predecessor, result in ((failedBefore.copy() | {"failures": []}, record(failures=["t::a"])),
                              (passedBefore, record())):
    outcome = mergeTestOutcome(predecessor, result, ["T1", "T2"], {})
    assert outcome["outcomes"] == {"T1": {"result": "unknown", "names": []}, "T2": {"result": "unknown", "names": []}}
    assert (outcome["advance"], outcome["split"]) == (False, None)
  assert mergeTestOutcome(passedBefore, record(), ["T1"], {}, splitCommit="m3")["split"] == "clear"
  assert mergeTestOutcome(passedBefore, record(), ["T1"], {}, staleSplit=True)["split"] == "clear"


def test_outcomeSplitsWhenNewFailureCoversSeveralMerges():
  outcome = mergeTestOutcome(passedBefore, record(failures=["t::a"]), ["T1", "T2"], {})
  assert outcome["verdict"] == "newFailure" and outcome["newFailures"] == ["t::a"]
  assert outcome["outcomes"] == {"T1": {"result": "split", "names": []}, "T2": {"result": "split", "names": []}}
  assert (outcome["advance"], outcome["split"]) == (False, "start")


def test_outcomeNewFailureForSingleMergeAdvances():
  outcome = mergeTestOutcome(failedBefore, record(failures=["t::old", "t::a"]), ["T2"], {})
  assert outcome["outcomes"] == {"T2": {"result": "newFailure", "names": ["t::a"]}}
  assert (outcome["advance"], outcome["split"]) == (True, None)


def test_outcomePassedAndPreexistingVerify():
  for predecessor, result, verdict in ((failedBefore, record(passed=True), "passed"),
                                       (failedBefore, record(failures=["t::old"]), "preexisting")):
    outcome = mergeTestOutcome(predecessor, result, ["T1", "T2"], {})
    assert outcome["verdict"] == verdict
    assert outcome["outcomes"] == {"T1": {"result": "verified", "names": []}, "T2": {"result": "verified", "names": []}}
    assert (outcome["advance"], outcome["split"]) == (True, None)


def test_outcomeStillFailingWhenEarlierFailureRemains():
  before = {**failedBefore, "failures": ["t::a"]}
  outcome = mergeTestOutcome(before, record(failures=["t::a"]), ["T1", "T2"], {"T1": ["t::a"], "T2": []})
  assert outcome["outcomes"] == {"T1": {"result": "stillFailing", "names": ["t::a"]},
                                 "T2": {"result": "verified", "names": []}}
  combined = mergeTestOutcome(before, record(failures=["t::a", "t::b"]), ["T1"], {"T1": ["t::a"]})
  assert combined["outcomes"] == {"T1": {"result": "stillFailing", "names": ["t::a", "t::b"]}}
  fixed = mergeTestOutcome(before, record(passed=True), ["T1"], {"T1": ["t::a"]})
  assert fixed["outcomes"] == {"T1": {"result": "verified", "names": []}}


def test_outcomeQualityNamesFollowBaselineRules():
  before = {**passedBefore, "qualityChecked": True, "qualityFailures": ["品質門檻 a.py::old"]}
  outcome = mergeTestOutcome(before, record(qualityChecked=True, qualityFailures=["品質門檻 a.py::old", "品質門檻 a.py::new"]),
                             ["T1"], {})
  assert outcome["outcomes"] == {"T1": {"result": "newFailure", "names": ["品質門檻 a.py::new"]}}


def test_outcomeClearsSplitWhenReachingItsCommit():
  assert mergeTestOutcome(passedBefore, record(commit="m3", passed=True), ["T3"], {}, splitCommit="m3")["split"] == "clear"
  assert mergeTestOutcome(passedBefore, record(commit="m1", passed=True), ["T1"], {}, splitCommit="m3")["split"] is None


def test_outcomeContractForRemainingBranches():
  # 沒有比較對象：全部 unknown，不前進；有拆分時要清掉
  unknown = mergeTestOutcome(None, record(failures=["t::a"]), ["T1"], {}, splitCommit="m3")
  assert unknown == {"verdict": "unknown", "newFailures": [], "outcomes": {"T1": {"result": "unknown", "names": []}},
                     "advance": False, "split": "clear"}
  # 品質未檢查的基準遇到只有品質超標：unknown
  qualityOnly = mergeTestOutcome(passedBefore, record(qualityChecked=True, qualityFailures=["品質門檻 a.py::x"]), ["T1"], {})
  assert (qualityOnly["verdict"], qualityOnly["outcomes"], qualityOnly["advance"]) == \
    ("unknown", {"T1": {"result": "unknown", "names": []}}, False)
  # 已經在拆分中又涵蓋多次合併：重新開始拆分
  split = mergeTestOutcome(passedBefore, record(failures=["t::a"]), ["T1", "T2"], {}, splitCommit="m3", staleSplit=True)
  assert split == {"verdict": "newFailure", "newFailures": ["t::a"],
                   "outcomes": {"T1": {"result": "split", "names": []}, "T2": {"result": "split", "names": []}},
                   "advance": False, "split": "start"}
  # 前進時 staleSplit 也要清掉
  stale = mergeTestOutcome(passedBefore, record(passed=True), ["T1"], {}, staleSplit=True)
  assert stale == {"verdict": "passed", "newFailures": [], "outcomes": {"T1": {"result": "verified", "names": []}},
                   "advance": True, "split": "clear"}
  # 之前退回的品質名稱仍超標：stillFailing
  before = {**passedBefore, "qualityChecked": True, "qualityFailures": ["品質門檻 a.py::old"]}
  quality = mergeTestOutcome(before, record(qualityChecked=True, qualityFailures=["品質門檻 a.py::old"]),
                             ["T1"], {"T1": ["品質門檻 a.py::old"]})
  assert quality["verdict"] == "preexisting"
  assert quality["outcomes"] == {"T1": {"result": "stillFailing", "names": ["品質門檻 a.py::old"]}}


def mergePlanState(planIdValue, status=PlanStatus.active, testTiming="afterMerge", waitingBaseline=False, split=None):
  return {"id": planIdValue, "status": status, "testTiming": testTiming, "waitingBaseline": waitingBaseline,
          "testBaseline": {"commit": "base", "command": "pytest"}, "mergeTest": {"lastTested": None, "split": split}}


def mergeTask(planIdValue, taskId, status=TaskStatus.mergedUntested, needs=None):
  return {"plan": planIdValue, "task": taskId, "status": status, "needs": needs,
          "mergedCommit": f"m-{taskId}", "mergeFailures": [f"t::{taskId}"]}


def schedulingConductor(tmp_path, monkeypatch, **config):
  # 只看排程的取捨，判斷基準、略過與真正啟動都換成記錄用的替身
  lines, started, skipped = [], [], []
  conductor = Conductor(tmp_path, fakeConfig(**config), out=lines.append)
  monkeypatch.setattr(conductor, "needsTestBaseline", lambda planState: planState["waitingBaseline"])
  monkeypatch.setattr(conductor, "skipMergeTests", lambda planState, state: skipped.append(planState["id"]))
  def startMergeTest(planState, pending, key):
    started.append((planState["id"], [taskState["task"] for taskState in pending], key))

  monkeypatch.setattr(conductor, "startMergeTest", startMergeTest)
  return conductor, lines, started, skipped


def test_startMergeTestsStartsOnlyPlansReadyToTest(tmp_path, monkeypatch):
  conductor, _, started, skipped = schedulingConductor(tmp_path, monkeypatch, testCommand="pytest")
  conductor.jobs = {conductorModule.mergeTestKey("P0002"): {"kind": "mergeTest"}}
  plans = {"P0001": mergePlanState("P0001"), "P0002": mergePlanState("P0002"),
           "P0003": mergePlanState("P0003", status=PlanStatus.completed),
           "P0004": mergePlanState("P0004", testTiming="beforeMerge"),
           "P0005": mergePlanState("P0005", waitingBaseline=True),
           "P0006": mergePlanState("P0006"), "P0007": mergePlanState("P0007"), "P0008": mergePlanState("P0008")}
  tasks = [mergeTask("P0001", "T1"), mergeTask("P0001", "T2", status=TaskStatus.done), mergeTask("P0001", "T3"),
           mergeTask("P0002", "T1"), mergeTask("P0003", "T1"), mergeTask("P0004", "T1"), mergeTask("P0005", "T1"),
           # 分不出結果的那次等你決定時，後面的合併不測；其他原因的等你處理不擋
           mergeTask("P0006", "T1", status=TaskStatus.needsYou, needs={"kind": "mergeTest"}), mergeTask("P0006", "T2"),
           mergeTask("P0007", "T1", status=TaskStatus.needsYou, needs={"kind": "merge"}),
           mergeTask("P0007", "T2", status=TaskStatus.needsYou), mergeTask("P0007", "T3"),
           mergeTask("P0008", "T1", status=TaskStatus.done)]
  state = {"plans": plans, "tasks": {f"{taskState['plan']}/{taskState['task']}": taskState for taskState in tasks}}
  conductor.startMergeTests(state)
  assert started == [("P0001", ["T1", "T3"], "P0001/mergetest"), ("P0007", ["T3"], "P0007/mergetest")]
  assert skipped == []


def test_startMergeTestsSkipsWhenTestsOffAndReportsStartError(tmp_path, monkeypatch):
  conductor, lines, started, skipped = schedulingConductor(tmp_path, monkeypatch)
  state = {"plans": {"P0001": mergePlanState("P0001")}, "tasks": {"P0001/T1": mergeTask("P0001", "T1")}}
  conductor.startMergeTests(state)
  assert (started, skipped) == ([], ["P0001"])
  conductor, lines, started, skipped = schedulingConductor(tmp_path, monkeypatch, testCommand="pytest")

  def failingStart(planState, pending, key):
    raise RuntimeError("壞掉")

  monkeypatch.setattr(conductor, "startMergeTest", failingStart)
  conductor.startMergeTests(state)
  conductor.startMergeTests(state)
  assert lines == ["P0001 的合併後測試無法開始：壞掉"]


def targetConductor(tmp_path, monkeypatch, freeSlots=1):
  # 合併順序是 m-T1、m-T2、m-T3；記下要測哪個 commit、涵蓋誰，不真的建立 worktree 或跑測試
  conductor = Conductor(tmp_path, fakeConfig(testCommand="pytest"), out=lambda line: None)
  written, worktrees = [], []
  monkeypatch.setattr(board_git, "firstParentCommits", lambda projectDir, planIdValue: ["m-T1", "m-T2", "m-T3"])
  monkeypatch.setattr(board_git, "addMergeTestWorktree",
                      lambda projectDir, planIdValue, commit: worktrees.append(commit) or "worktree")
  monkeypatch.setattr(conductorModule, "BackgroundTestJob", lambda runTestCommand, worktree: type(
    "FakeJob", (), {"start": lambda self: "job"})())
  monkeypatch.setattr(conductor, "freeTestSlots", lambda: freeSlots)
  monkeypatch.setattr(conductor, "writeMergeTestRun",
                      lambda info, record, reused: written.append((dict(info), record, reused)))
  return conductor, written, worktrees


def startedTarget(conductor):
  info = conductor.jobs["P0001/mergetest"]
  return info["commit"], info["covers"], info["splitCommit"], info["staleSplit"], info["mergeFailures"]


def test_startMergeTestBatchesPendingInMergeOrder(tmp_path, monkeypatch):
  conductor, written, worktrees = targetConductor(tmp_path, monkeypatch)
  pending = [mergeTask("P0001", "T3"), mergeTask("P0001", "T2")]
  conductor.startMergeTest(mergePlanState("P0001"), pending, "P0001/mergetest")
  assert startedTarget(conductor) == ("m-T3", ["T2", "T3"], None, False, {"T2": ["t::T2"], "T3": ["t::T3"]})
  assert conductor.jobs["P0001/mergetest"]["predecessor"] == {"commit": "base", "command": "pytest"}
  assert conductor.jobs["P0001/mergetest"]["job"] == "job"
  assert (written, worktrees) == ([], ["m-T3"])


def test_startMergeTestSplitTestsOneMergeAtATimeAndReusesSplitCommit(tmp_path, monkeypatch):
  split = {"commit": "m-T3", "command": "pytest", "passed": False, "failures": ["t::a"]}
  conductor, written, worktrees = targetConductor(tmp_path, monkeypatch)
  pending = [mergeTask("P0001", "T2"), mergeTask("P0001", "T1"), mergeTask("P0001", "T3")]
  conductor.startMergeTest(mergePlanState("P0001", split=split), pending, "P0001/mergetest")
  assert startedTarget(conductor) == ("m-T1", ["T1"], "m-T3", False,
                                      {"T1": ["t::T1"], "T2": ["t::T2"], "T3": ["t::T3"]})
  assert worktrees == ["m-T1"]
  # 輪到拆分的 commit：沿用一開始一起測的結果，不建立 worktree、不佔名額
  conductor, written, worktrees = targetConductor(tmp_path, monkeypatch, freeSlots=0)
  conductor.startMergeTest(mergePlanState("P0001", split=split), [mergeTask("P0001", "T3")], "P0001/mergetest")
  assert conductor.jobs == {} and worktrees == []
  [(info, record, reused)] = written
  assert (info["commit"], info["covers"], info["splitCommit"], info["staleSplit"], reused) == \
    ("m-T3", ["T3"], "m-T3", False, True)
  assert record == {field: split.get(field) for field in conductorModule.mergeTestRecordFields}


def test_startMergeTestStaleSplitTestsAllPending(tmp_path, monkeypatch):
  # 指令改了、拆分的 commit 不在 plan 分支上、或待測的合併在拆分範圍之後：拆分作廢，一起測到最新
  stale = [{"commit": "m-T3", "command": "old"}, {"commit": "gone", "command": "pytest"},
           {"commit": "m-T1", "command": "pytest"}]
  for split in stale:
    conductor, written, worktrees = targetConductor(tmp_path, monkeypatch)
    pending = [mergeTask("P0001", "T2"), mergeTask("P0001", "T3")]
    conductor.startMergeTest(mergePlanState("P0001", split=split), pending, "P0001/mergetest")
    assert startedTarget(conductor)[:4] == ("m-T3", ["T2", "T3"], None, True)
    assert (written, worktrees) == ([], ["m-T3"])


def test_startMergeTestWaitsWhenTestSlotsAreFull(tmp_path, monkeypatch):
  conductor, written, worktrees = targetConductor(tmp_path, monkeypatch, freeSlots=0)
  conductor.startMergeTest(mergePlanState("P0001"), [mergeTask("P0001", "T1")], "P0001/mergetest")
  assert (conductor.jobs, written, worktrees) == ({}, [], [])


def eventsOf(conductor, eventType):
  return [event for event in board_core.readEvents(conductor.projectDir) if event["type"] == eventType]


def planDone(state):
  return state["plans"]["P0001"]["status"] == PlanStatus.completed


def allMerged(taskIds):
  return lambda state: all(state["tasks"][f"P0001/{taskId}"]["status"] == TaskStatus.mergedUntested for taskId in taskIds)


def scriptCommand(tmp_path, body, name="mergeTests.py"):
  # 假的完整測試在被測的 worktree 執行，照 T*.txt 的內容決定要不要印出失敗
  scriptPath = tmp_path / name
  scriptPath.write_text("import os, pathlib, sys, time\n" + body, encoding="utf-8")
  return f'"{sys.executable}" "{scriptPath}"'


def sleepyCommand(tmp_path, sleeps):
  logPath = tmp_path / "testLog.txt"
  body = (f"log, sleeps = pathlib.Path({str(logPath)!r}), {sleeps!r}\n"
          "name = pathlib.Path.cwd().name\n"
          "with open(log, 'a', encoding='utf-8') as file: file.write(f'start {name} {os.getpid()}\\n')\n"
          "time.sleep(sleeps.get(name, 0))\n"
          "with open(log, 'a', encoding='utf-8') as file: file.write(f'end {name} {os.getpid()}\\n')\n")
  return scriptCommand(tmp_path, body, "sleepyMergeTests.py"), logPath


def mergeTestLog(logPath, name):
  lines = logPath.read_text(encoding="utf-8").splitlines() if logPath.exists() else []
  return [line.split() for line in lines if line.split()[1] == name]


def noMergeTestWorktree(projectDir):
  return not board_git.mergeTestWorktreePath(projectDir, "P0001").exists() and \
    "-mergetest" not in board_git.runGit(projectDir, "worktree", "list", "--porcelain")


def test_independentTasksMergeThenOneTestCoversThemAndPlanCompletes(startBoard):
  conductor, scenarioDir = startBoard(threeTasks, testTiming="afterMerge", testCommand="git --version")
  realStart = conductor.startMergeTests
  conductor.startMergeTests = lambda state: None
  runUntil(conductor, allMerged(["T1", "T2", "T3"]))
  assert not planDone(conductor.currentState()[1])
  conductor.startMergeTests = realStart
  runUntil(conductor, planDone)
  runs = eventsOf(conductor, "mergeTestRun")
  assert len(runs) == 1
  assert sorted(runs[0]["data"]["covers"]) == ["T1", "T2", "T3"]
  assert runs[0]["data"]["commit"] == board_git.runGit(conductor.projectDir, "rev-parse", "plan/P0001")
  assert runs[0]["data"]["comparedTo"] == eventsOf(conductor, "testBaseline")[0]["data"]["commit"]
  assert eventsOf(conductor, "testRun") == []
  assert all(event["data"]["testing"] is False for event in eventsOf(conductor, "taskWritten"))
  assert all(event["data"]["verify"] is True for event in eventsOf(conductor, "taskMerged"))
  assert noMergeTestWorktree(conductor.projectDir)
  assert "涵蓋 " in formatEvent(runs[0]) and "合併後測試" in formatEvent(runs[0])
  reviewPrompt = readCalls(scenarioDir, "review")[0]["prompt"]
  assert "自動測試結果：" not in reviewPrompt and "合併進 plan 分支後才跑" in reviewPrompt
  assert "合併進 plan 分支後，流程才會跑一次完整測試" in readCalls(scenarioDir, "write")[0]["prompt"]


def test_mergesDuringATestWaitForTheNextOne(startBoard, tmp_path):
  command, logPath = sleepyCommand(tmp_path, {"P0001-mergetest": 4})
  conductor, _ = startBoard(threeTasks, {"write": {"T2": ["slow"], "T3": ["slow"]}}, testTiming="afterMerge",
                            testCommand=command)
  runUntil(conductor, planDone)
  covers = [sorted(event["data"]["covers"]) for event in eventsOf(conductor, "mergeTestRun")]
  assert sum(len(item) for item in covers) == 3
  assert len(covers) < 3
  starts = [entry for entry in mergeTestLog(logPath, "P0001-mergetest") if entry[0] == "start"]
  assert len(starts) == len(covers)


def test_dependentStartsWhileItsPrerequisiteWaitsForMergeTest(startBoard, tmp_path):
  command, _ = sleepyCommand(tmp_path, {"P0001-mergetest": 3})
  conductor, _ = startBoard(chainedTasks, testTiming="afterMerge", testCommand=command)
  runUntil(conductor, planDone)
  events = [(event["type"], event["task"]) for event in board_core.readEvents(conductor.projectDir)]
  assert events.index(("taskStarted", "T2")) < events.index(("mergeTestRun", None))


def approveAfterMergePlan(gitProject, writePlan, planIdValue):
  import board as boardCli

  # baseCommit 有程式碼改動，基準不能沿用，要自己跑
  (gitProject / "calc.py").write_text("def add(a, b):\n  return a + b\n", encoding="utf-8")
  subprocess.run(["git", "add", "calc.py"], cwd=gitProject, check=True, capture_output=True)
  subprocess.run(["git", "commit", "-q", "-m", "改程式"], cwd=gitProject, check=True, capture_output=True)
  writePlan(f"{planIdValue[1:]}-next.md", oneTask, planId=planIdValue, projectDir=gitProject)
  assert boardCli.cmdSubmit(gitProject, planIdValue) == 0
  assert boardCli.cmdApprove(gitProject, planIdValue, confirm=lambda prompt: "y", testTiming="afterMerge") == 0


def logOrder(logPath):
  return [tuple(line.split()[:2]) for line in logPath.read_text(encoding="utf-8").splitlines()]


def test_mergeTestSharesTestSlotWithBaseline(startBoard, writePlan, gitProject, tmp_path):
  command, logPath = sleepyCommand(tmp_path, {"P0001-mergetest": 4, "P0002-baseline": 1})
  conductor, _ = startBoard(oneTask, testTiming="afterMerge", testCommand=command)
  runUntil(conductor, lambda state: "P0001/mergetest" in conductor.jobs)
  approveAfterMergePlan(gitProject, writePlan, "P0002")
  # 預設 maxParallelTests 是 1：合併後測試在跑，P0002 的基準等它結束才開始
  conductor.tick()
  assert "P0002/baseline" not in conductor.jobs and "P0001/mergetest" in conductor.jobs
  fullTestKinds = ("test", "baseline", "mergeTest")
  mostRunning = []

  def bothDone(state):
    mostRunning.append(sum(1 for info in conductor.jobs.values() if info["kind"] in fullTestKinds))
    return all(state["plans"][planIdValue]["status"] == PlanStatus.completed for planIdValue in ("P0001", "P0002"))

  runUntil(conductor, bothDone)
  assert max(mostRunning) == 1
  order = logOrder(logPath)
  assert order.index(("end", "P0001-mergetest")) < order.index(("start", "P0002-baseline"))
  assert order.index(("end", "P0002-baseline")) < order.index(("start", "P0002-mergetest"))


failT2FirstVersion = ("t2 = pathlib.Path('T2.txt')\n"
                      "if t2.exists() and '第 1 次' in t2.read_text(encoding='utf-8'):\n"
                      "  print('FAILED tests/test_t2.py::test_t2 - AssertionError')\n"
                      "  sys.exit(1)\n")


def test_failureAcrossSeveralMergesIsTracedToOneTaskAndFixedOnLatestPlan(startBoard, tmp_path):
  conductor, scenarioDir = startBoard(threeTasks, testTiming="afterMerge",
                                      testCommand=scriptCommand(tmp_path, failT2FirstVersion))
  realStart = conductor.startMergeTests
  conductor.startMergeTests = lambda state: None
  runUntil(conductor, allMerged(["T1", "T2", "T3"]))
  conductor.startMergeTests = realStart
  runUntil(conductor, planDone)
  merged = [event for event in eventsOf(conductor, "taskMerged") if event["round"] == 1]
  mergeOrder = [event["task"] for event in merged]
  runs = [event["data"] for event in eventsOf(conductor, "mergeTestRun")]
  first = runs[0]
  assert sorted(first["covers"]) == ["T1", "T2", "T3"] and first["split"] == "start"
  assert {item["result"] for item in first["outcomes"].values()} == {"split"}
  individual = runs[1:4]
  assert [run["covers"] for run in individual] == [[taskId] for taskId in mergeOrder]
  assert [run["commit"] for run in individual] == [event["data"]["commit"] for event in merged]
  assert [run["reused"] for run in individual] == [False, False, True]
  assert individual[-1]["commit"] == first["commit"] and individual[-1]["split"] == "clear"
  results = {run["covers"][0]: run["outcomes"][run["covers"][0]]["result"] for run in individual}
  assert results == {"T1": "verified", "T2": "newFailure", "T3": "verified"}
  assert runs[4]["covers"] == ["T2"] and runs[4]["outcomes"]["T2"]["result"] == "verified"
  assert len(readCalls(scenarioDir, "write", taskId="T1")) == 1
  assert len(readCalls(scenarioDir, "write", taskId="T3")) == 1
  fixCalls = readCalls(scenarioDir, "write", taskId="T2")
  assert len(fixCalls) == 2
  session = fixCalls[0]["argv"][fixCalls[0]["argv"].index("--session-id") + 1]
  assert fixCalls[1]["argv"][fixCalls[1]["argv"].index("--resume") + 1] == session
  assert "（第 2 輪）" in fixCalls[1]["prompt"]
  assert "合併進 plan 分支後，完整測試" in fixCalls[1]["prompt"]
  assert "- tests/test_t2.py::test_t2" in fixCalls[1]["prompt"]
  starts = [event for event in eventsOf(conductor, "taskStarted") if event["task"] == "T2"]
  assert starts[1]["data"]["resume"] is True and starts[1]["data"]["sessionId"] == starts[0]["data"]["sessionId"]
  for fileName in ("T1.txt", "T3.txt"):
    assert board_git.runGit(conductor.projectDir, "cat-file", "-e", f"task/P0001-T2:{fileName}") == ""
  fixReview = readCalls(scenarioDir, "review", taskId="T2")[1]["prompt"]
  assert "- tests/test_t2.py::test_t2" in fixReview and "自動測試結果：" not in fixReview
  assert "a/T2.txt" in fixReview and "T1.txt" not in fixReview and "T3.txt" not in fixReview
  assert board_git.runGit(conductor.projectDir, "show", "plan/P0001:T2.txt") == "T2 第 2 次"
  assert noMergeTestWorktree(conductor.projectDir)


def test_unknownResultWaitsForYouThenRetryRetestsAndDoneCompletes(startBoard, tmp_path):
  body = "if pathlib.Path('T1.txt').exists():\n  print('boom')\n  sys.exit(1)\n"
  conductor, _ = startBoard(oneTask, testTiming="afterMerge", testCommand=scriptCommand(tmp_path, body))
  isStuck = lambda state: state["tasks"]["P0001/T1"]["status"] == TaskStatus.needsYou
  state = runUntil(conductor, isStuck)
  assert state["tasks"]["P0001/T1"]["needs"]["kind"] == "mergeTest"
  assert eventsOf(conductor, "mergeTestRun")[0]["data"]["outcomes"] == {"T1": {"result": "unknown", "names": []}}
  board_core.appendEvent(conductor.projectDir, "userDecision", plan="P0001", task="T1", data={"action": "retry"})
  state = runUntil(conductor, lambda current: len(eventsOf(conductor, "mergeTestRun")) == 2 and isStuck(current))
  assert state["tasks"]["P0001/T1"]["round"] == 1
  board_core.appendEvent(conductor.projectDir, "userDecision", plan="P0001", task="T1", data={"action": "done"})
  runUntil(conductor, planDone)
  assert len(eventsOf(conductor, "mergeTestRun")) == 2


def test_pauseWhileMergeTestRunsRerunsItWithOneResult(startBoard, tmp_path):
  command, logPath = sleepyCommand(tmp_path, {"P0001-mergetest": 3})
  conductor, _ = startBoard(oneTask, testTiming="afterMerge", testCommand=command)
  runUntil(conductor, lambda state: bool(mergeTestLog(logPath, "P0001-mergetest")))
  board_core.appendEvent(conductor.projectDir, "paused", data={"reason": "測試暫停"})
  state = runUntil(conductor, lambda current: not conductor.jobs)
  assert state["tasks"]["P0001/T1"]["status"] == TaskStatus.mergedUntested
  assert noMergeTestWorktree(conductor.projectDir)
  board_core.appendEvent(conductor.projectDir, "resumed")
  runUntil(conductor, planDone)
  assert len(eventsOf(conductor, "mergeTestRun")) == 1
  assert [entry[0] for entry in mergeTestLog(logPath, "P0001-mergetest")] == ["start", "start", "end"]


def test_abandonWhileMergeTestRunsKillsItAndRemovesWorktree(startBoard, tmp_path):
  command, logPath = sleepyCommand(tmp_path, {"P0001-mergetest": 5})
  conductor, _ = startBoard(oneTask, testTiming="afterMerge", testCommand=command)
  runUntil(conductor, lambda state: bool(mergeTestLog(logPath, "P0001-mergetest")))
  testPid = int(mergeTestLog(logPath, "P0001-mergetest")[0][2])
  board_core.appendEvent(conductor.projectDir, "planAbandoned", plan="P0001")
  runUntil(conductor, lambda current: not conductor.jobs)
  deadline = time.time() + 10
  while conductorModule.processAlive(testPid) and time.time() < deadline:
    time.sleep(0.1)
  assert not conductorModule.processAlive(testPid)
  assert noMergeTestWorktree(conductor.projectDir)
  for _ in range(3):
    conductor.tick()
  assert eventsOf(conductor, "mergeTestRun") == []


def test_restartWhileMergeTestRunsRerunsIt(startBoard, tmp_path):
  command, logPath = sleepyCommand(tmp_path, {"P0001-mergetest": 3})
  conductor, _ = startBoard(oneTask, testTiming="afterMerge", testCommand=command)
  runUntil(conductor, lambda state: bool(mergeTestLog(logPath, "P0001-mergetest")))
  # 和 Ctrl+C 結束 Alfred 時一樣
  conductor.stopJobs(list(conductor.jobs), recordInterruption=True)
  assert noMergeTestWorktree(conductor.projectDir)
  restarted = Conductor(conductor.projectDir, config=fakeConfig(testCommand=command), out=lambda line: None)
  runUntil(restarted, planDone)
  assert len(eventsOf(restarted, "mergeTestRun")) == 1


def test_skipTestsCompletesOnMergeWithoutMergeTest(startBoard):
  conductor, _ = startBoard(oneTask, testTiming="afterMerge")
  runUntil(conductor, planDone)
  assert eventsOf(conductor, "mergeTestRun") == []
  assert eventsOf(conductor, "taskMerged")[0]["data"]["verify"] is False


def test_turningTestsOffAfterMergeCompletesWaitingTasksOnRestart(startBoard):
  conductor, _ = startBoard(oneTask, testTiming="afterMerge", testCommand="git --version")
  conductor.startMergeTests = lambda state: None
  runUntil(conductor, allMerged(["T1"]))
  restarted = Conductor(conductor.projectDir, config=fakeConfig(), out=lambda line: None)
  runUntil(restarted, planDone)
  assert eventsOf(restarted, "mergeTestRun") == []
  skipped = eventsOf(restarted, "mergeTestSkipped")
  assert len(skipped) == 1 and skipped[0]["data"]["covers"] == ["T1"]
  assert "略過合併後測試" in formatEvent(skipped[0])
