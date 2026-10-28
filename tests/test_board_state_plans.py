from board_core import PlanStatus, TaskStatus, deriveState, loadPlans, planTasks
from helpers import approvedEvent, makeEvent, makePlans, testConfig


def planStatus(events, plans=None, planIdValue="P0001"):
  return deriveState(events, plans or makePlans("P0001"), testConfig)["plans"][planIdValue]["status"]


def test_planWithoutEventsIsDraft():
  assert planStatus([]) == PlanStatus.draft


def test_submitRejectResubmit():
  submitted = makeEvent("planSubmitted", plan="P0001")
  rejected = makeEvent("planRejected", plan="P0001", reason="缺驗證")
  assert planStatus([submitted]) == PlanStatus.pending
  state = deriveState([submitted, rejected], makePlans("P0001"), testConfig)
  assert state["plans"]["P0001"]["status"] == PlanStatus.rejected
  assert state["plans"]["P0001"]["rejectReason"] == "缺驗證"
  assert planStatus([submitted, rejected, submitted]) == PlanStatus.pending


def test_approveCreatesQueuedTasksFromSnapshot():
  events = [makeEvent("planSubmitted", plan="P0001"), approvedEvent("P0001", [("T1", []), ("T2", ["T1"])])]
  state = deriveState(events, makePlans("P0001"), testConfig)
  assert state["plans"]["P0001"]["status"] == PlanStatus.active
  assert state["plans"]["P0001"]["baseCommit"] == "abc123"
  tasks = planTasks(state, "P0001")
  assert [task["task"] for task in tasks] == ["T1", "T2"]
  assert tasks[1]["dependsOn"] == ["T1"]
  assert all(task["status"] == TaskStatus.queued and task["round"] == 1 and task["maxRounds"] == 3 for task in tasks)


def test_testBaselineIsRecordedOnPlanAndLatestWins():
  approved = approvedEvent("P0001", [("T1", [])])
  assert deriveState([approved], makePlans("P0001"), testConfig)["plans"]["P0001"]["testBaseline"] is None
  first = makeEvent("testBaseline", plan="P0001", command="pytest -q", commit="abc123", passed=False, tail="1 failed")
  rerun = makeEvent("testBaseline", plan="P0001", command="python -m pytest", commit="abc123", passed=True, tail="ok")
  state = deriveState([approved, first], makePlans("P0001"), testConfig)
  assert state["plans"]["P0001"]["testBaseline"] == {"command": "pytest -q", "commit": "abc123", "passed": False,
                                                     "tail": "1 failed"}
  assert state["plans"]["P0001"]["status"] == PlanStatus.active
  state = deriveState([approved, first, rerun], makePlans("P0001"), testConfig)
  assert state["plans"]["P0001"]["testBaseline"]["command"] == "python -m pytest"


def test_abandonPlanAbandonsUnfinishedTasks():
  events = [approvedEvent("P0001", [("T1", [])]), makeEvent("planAbandoned", plan="P0001")]
  state = deriveState(events, makePlans("P0001"), testConfig)
  assert state["plans"]["P0001"]["status"] == PlanStatus.abandoned
  assert state["tasks"]["P0001/T1"]["status"] == TaskStatus.abandoned


def test_completedEvent():
  events = [approvedEvent("P0001", [("T1", [])]), makeEvent("planCompleted", plan="P0001")]
  assert planStatus(events) == PlanStatus.completed


def test_supersededUnlessActive():
  plans = makePlans("P0001", "P0002", supersedes={"P0002": "P0001"})
  assert planStatus([makeEvent("planSubmitted", plan="P0001")], plans) == PlanStatus.superseded
  assert planStatus([approvedEvent("P0001", [("T1", [])])], plans) == PlanStatus.active


def test_pausedAndResumed():
  paused = makeEvent("paused", reason="你按了停止")
  state = deriveState([paused], makePlans("P0001"), testConfig)
  assert state["paused"] is True
  assert state["pauseReason"] == "你按了停止"
  assert deriveState([paused, makeEvent("resumed")], makePlans("P0001"), testConfig)["paused"] is False


def test_eventsForUnknownPlanAreIgnored():
  state = deriveState([makeEvent("planSubmitted", plan="P0099")], makePlans("P0001"), testConfig)
  assert list(state["plans"]) == ["P0001"]


def test_loadPlansSkipsUnreadableFile(tmp_path, writePlan):
  writePlan("0001-ok.md", ["| R1 需求 | 理解 | D1 做法 | - | T1 任務 | | 驗證 |"], planId="P0001")
  # Windows PowerShell 5.1 的 Out-File 預設就是 UTF-16，plan 很容易被存成這種編碼
  (tmp_path / "plans" / "0002-utf16.md").write_text("---\nid: P0002\n---\n\n# 存錯編碼\n", encoding="utf-16")
  errors = []
  assert list(loadPlans(tmp_path, errors)) == ["P0001"]
  assert len(errors) == 1
  assert errors[0].startswith("0002-utf16.md 無法讀取（UnicodeDecodeError），已略過：")
  assert list(loadPlans(tmp_path)) == ["P0001"]
