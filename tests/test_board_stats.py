from datetime import datetime, timedelta

from board_core import deriveState
from board_stats import boardStats, planStats
from helpers import approvedEvent, makeEvent, makePlans, testConfig

startTime = datetime.fromisoformat("2026-10-26T10:00:00+08:00")
writerModel = {"cli": "claude", "model": "opus", "reasoningEffort": "xhigh"}
reviewerModel = {"cli": "codex", "model": "gpt-6.1-sol", "reasoningEffort": "xhigh"}


class Timeline:
  """依序產生事件，每個事件比前一個晚指定的秒數。"""

  def __init__(self, planIdValue="P0001"):
    self.planId = planIdValue
    self.current = startTime
    self.events = []

  def add(self, eventType, task=None, roundNumber=None, after=60, **data):
    self.current += timedelta(seconds=after)
    event = makeEvent(eventType, plan=self.planId, task=task, roundNumber=roundNumber, **data)
    event["time"] = self.current.isoformat()
    self.events.append(event)
    return event

  def approve(self, tasks, after=60):
    event = approvedEvent(self.planId, tasks)
    self.current += timedelta(seconds=after)
    event["time"] = self.current.isoformat()
    self.events.append(event)

  def write(self, task, roundNumber, mode="write"):
    self.add("taskStarted", task, roundNumber, mode=mode, **writerModel)
    self.add("taskWritten", task, roundNumber, commit="c1", mode=mode)

  def review(self, task, roundNumber, passed=True):
    self.add("reviewStarted", task, roundNumber, **reviewerModel)
    self.add("reviewPassed" if passed else "reviewFailed", task, roundNumber, notes=f"r{roundNumber}.md")

  def merge(self, task):
    self.add("taskMerged", task, commit="m1")


def chain(*taskIds):
  return [(taskId, [taskIds[index - 1]] if index else []) for index, taskId in enumerate(taskIds)]


def p0001Events():
  timeline = Timeline()
  timeline.add("planSubmitted")
  timeline.approve(chain("T1", "T2", "T3", "T4", "T5"))
  approvedAt = timeline.current
  timeline.write("T1", 1)
  timeline.review("T1", 1)
  timeline.merge("T1")
  timeline.write("T2", 1)
  # P0001 時的 testRun 還沒有 verdict
  timeline.add("testRun", "T2", 1, passed=True, tail="5 passed")
  timeline.review("T2", 1)
  timeline.merge("T2")
  timeline.write("T3", 1)
  timeline.review("T3", 1, passed=False)
  timeline.add("taskStarted", "T3", 2, mode="write", **writerModel)
  timeline.add("taskBlocked", "T3", 2, reason="缺資料")
  timeline.add("userDecision", "T3", after=9 * 60 + 52, action="retry", note="")
  timeline.write("T3", 2)
  timeline.review("T3", 2)
  timeline.merge("T3")
  for taskId in ("T4", "T5"):
    timeline.write(taskId, 1)
    timeline.review(taskId, 1)
    timeline.merge(taskId)
  timeline.add("planCompleted", after=0)
  # 調整最後一個事件的時間，讓耗時剛好是 1 小時 13 分 29 秒
  timeline.events[-1]["time"] = (approvedAt + timedelta(hours=1, minutes=13, seconds=29)).isoformat()
  return timeline.events


def statsOf(events, *planIds, now=None):
  plans = makePlans(*(planIds or ("P0001",)))
  return boardStats(events, deriveState(events, plans, testConfig), now=now)


def test_p0001MatchesReferenceValues():
  stats = statsOf(p0001Events())["plans"]["P0001"]
  assert stats["tasks"] == {"done": 5, "abandoned": 0, "total": 5}
  assert stats["averageRounds"] == 1.2
  assert stats["written"] == 6
  assert stats["reviews"] == {"total": 6, "passed": 5, "failed": 1}
  assert stats["firstReviewPassed"] == {"passed": 4, "reviewed": 5}
  assert stats["tests"] == {"runs": 1, "failed": 0, "returned": 0}
  assert stats["returnedAfterWrite"] == {"returned": 1, "written": 6}
  assert stats["decisions"] == {"total": 1, "retry": 1, "done": 0, "abandon": 0}
  assert stats["waiting"] == 0
  assert stats["waitSeconds"] == 9 * 60 + 52
  assert (stats["agentFailed"], stats["conflicts"], stats["planRejected"]) == (0, 0, 0)
  assert stats["duration"] == {"seconds": 3600 + 13 * 60 + 29, "inProgress": False}
  assert stats["models"] == {"writer": [writerModel], "reviewer": [reviewerModel]}


def test_totalEqualsSinglePlan():
  stats = statsOf(p0001Events())
  assert stats["total"] == stats["plans"]["P0001"]


def test_oldTestRunWithoutVerdictCountsFailureButNotReturn():
  timeline = Timeline()
  timeline.approve([("T1", [])])
  timeline.write("T1", 1)
  timeline.add("testRun", "T1", 1, passed=False, tail="1 failed")
  timeline.review("T1", 1)
  timeline.merge("T1")
  timeline.add("planCompleted")
  stats = statsOf(timeline.events)["plans"]["P0001"]
  assert stats["tests"] == {"runs": 1, "failed": 1, "returned": 0}
  assert stats["returnedAfterWrite"] == {"returned": 0, "written": 1}


def test_newFailureCountsAsReturn():
  timeline = Timeline()
  timeline.approve([("T1", [])])
  timeline.write("T1", 1)
  timeline.add("testRun", "T1", 1, passed=False, verdict="newFailure", newFailures=["tests/test_a.py::test_a"])
  timeline.write("T1", 2)
  timeline.add("testRun", "T1", 2, passed=False, verdict="preexisting", newFailures=[])
  timeline.review("T1", 2)
  timeline.merge("T1")
  stats = statsOf(timeline.events)["plans"]["P0001"]
  assert stats["tests"] == {"runs": 2, "failed": 2, "returned": 1}
  assert stats["returnedAfterWrite"] == {"returned": 1, "written": 2}
  assert stats["averageRounds"] == 2
  assert stats["firstReviewPassed"] == {"passed": 1, "reviewed": 1}


def test_conflictResolutionCountsRoundAndConflict():
  timeline = Timeline()
  timeline.approve([("T1", [])])
  timeline.write("T1", 1)
  timeline.review("T1", 1)
  timeline.add("taskConflicted", "T1", 1, conflicts=["a.txt"], reason="a.txt")
  timeline.write("T1", 2, mode="resolve")
  timeline.review("T1", 2)
  timeline.merge("T1")
  stats = statsOf(timeline.events)["plans"]["P0001"]
  assert stats["conflicts"] == 1
  assert stats["averageRounds"] == 2
  assert stats["written"] == 2
  assert stats["firstReviewPassed"] == {"passed": 1, "reviewed": 1}
  assert stats["tasks"]["done"] == 1


def test_interruptionDoesNotAddRoundOrWrite():
  timeline = Timeline()
  timeline.approve([("T1", [])])
  timeline.add("taskStarted", "T1", 1, mode="write", **writerModel)
  timeline.add("taskInterrupted", "T1", 1, stage="write")
  timeline.write("T1", 1)
  timeline.review("T1", 1)
  timeline.merge("T1")
  stats = statsOf(timeline.events)["plans"]["P0001"]
  assert stats["averageRounds"] == 1
  assert stats["written"] == 1
  assert stats["agentFailed"] == 0


def test_abandonedTasksAndPlan():
  timeline = Timeline()
  timeline.approve(chain("T1", "T2"))
  timeline.add("taskStarted", "T1", 1, mode="write", **writerModel)
  timeline.add("agentFailed", "T1", 1, stage="write", reason="exit 1")
  timeline.add("taskStarted", "T1", 1, mode="write", **writerModel)
  timeline.add("agentFailed", "T1", 1, stage="write", reason="exit 1")
  timeline.add("userDecision", "T1", after=300, action="abandon", note="")
  stats = statsOf(timeline.events, now=timeline.current + timedelta(hours=5))["plans"]["P0001"]
  assert stats["tasks"] == {"done": 0, "abandoned": 2, "total": 2}
  assert stats["agentFailed"] == 2
  assert stats["decisions"] == {"total": 1, "retry": 0, "done": 0, "abandon": 1}
  assert stats["waitSeconds"] == 300
  # 沒開始的 T2 沒有輪數，不算進平均
  assert stats["averageRounds"] == 1
  # 放棄的 plan 沒有 planCompleted，依定義算到現在並標進行中
  assert stats["duration"] == {"seconds": 60 * 4 + 300 + 5 * 3600, "inProgress": True}


def test_taskWaitingForYouAndPlanInProgress():
  timeline = Timeline()
  timeline.approve([("T1", [])])
  approvedAt = timeline.current
  timeline.write("T1", 1)
  timeline.add("needsYou", "T1", 1, kind="noChanges", reason="沒有改動")
  now = approvedAt + timedelta(minutes=30)
  stats = statsOf(timeline.events, now=now)["plans"]["P0001"]
  assert stats["waiting"] == 1
  assert stats["waitSeconds"] == 0
  assert stats["duration"] == {"seconds": 30 * 60, "inProgress": True}
  assert stats["firstReviewPassed"] == {"passed": 0, "reviewed": 0}


def test_onlyApprovedPlansAndTotalSumsThem():
  other = Timeline("P0002")
  other.add("planSubmitted")
  other.add("planRejected", reason="太大")
  other.add("planSubmitted")
  other.approve([("T1", [])])
  other.write("T1", 1)
  other.review("T1", 1, passed=False)
  other.write("T1", 2)
  other.review("T1", 2)
  other.merge("T1")
  other.add("planCompleted")
  draft = Timeline("P0003")
  draft.add("planSubmitted")
  events = p0001Events() + other.events + draft.events
  stats = statsOf(events, "P0001", "P0002", "P0003")
  assert list(stats["plans"]) == ["P0001", "P0002"]
  assert stats["plans"]["P0002"]["planRejected"] == 1
  total = stats["total"]
  assert total["tasks"] == {"done": 6, "abandoned": 0, "total": 6}
  assert total["averageRounds"] == round(8 / 6, 2)
  assert total["written"] == 8
  assert total["reviews"] == {"total": 8, "passed": 6, "failed": 2}
  assert total["firstReviewPassed"] == {"passed": 4, "reviewed": 6}
  assert total["planRejected"] == 1
  assert total["duration"]["seconds"] == (stats["plans"]["P0001"]["duration"]["seconds"]
                                          + stats["plans"]["P0002"]["duration"]["seconds"])


def test_oldEventsWithoutCliUseClaudeForWriter():
  timeline = Timeline()
  timeline.approve([("T1", [])])
  timeline.add("taskStarted", "T1", 1, mode="write")
  timeline.add("reviewStarted", "T1", 1)
  events = timeline.events
  stats = planStats(events, deriveState(events, makePlans("P0001"), testConfig), "P0001")
  assert stats["models"] == {"writer": [{"cli": "claude", "model": None, "reasoningEffort": None}],
                             "reviewer": [{"cli": None, "model": None, "reasoningEffort": None}]}
