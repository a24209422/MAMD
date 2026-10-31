import pytest

from board_core import PlanStatus, TaskStatus, deriveState, isPlanFinished, readyTasks
from helpers import approvedEvent, makeEvent, makePlans, testConfig


def afterMergeApproved(tasks):
  event = approvedEvent("P0001", list(tasks))
  event["data"]["testTiming"] = "afterMerge"
  return event


def stateAfter(*events, tasks=(("T1", []),)):
  return deriveState([afterMergeApproved(tasks), *events], makePlans("P0001"), testConfig)


def task(state, taskId="T1"):
  return state["tasks"][f"P0001/{taskId}"]


def mergedCycle(roundNumber, taskId="T1", commit="m1"):
  return [
    makeEvent("taskStarted", plan="P0001", task=taskId, roundNumber=roundNumber, agent="writer",
              worktree="wt", sessionId="session-1", resume=roundNumber > 1, cli="claude", model="opus"),
    makeEvent("taskWritten", plan="P0001", task=taskId, roundNumber=roundNumber, commit="c1", summary="完成",
              testing=False, sessionId="session-1", cli="claude", model="opus"),
    makeEvent("reviewStarted", plan="P0001", task=taskId, roundNumber=roundNumber, agent="reviewer"),
    makeEvent("reviewPassed", plan="P0001", task=taskId, roundNumber=roundNumber,
              notes=f"reviews/P0001-{taskId}-r{roundNumber}.md"),
    makeEvent("taskMerged", plan="P0001", task=taskId, roundNumber=roundNumber, commit=commit, verify=True),
  ]


def mergeTestRun(outcomes, commit="m1", advance=True, split=None, **data):
  record = {"commit": commit, "command": "pytest", "passed": False, "failures": ["t::a"], "qualityChecked": False,
            "qualityFailures": [], "tail": "1 failed", "comparedTo": "abc123", "covers": list(outcomes),
            "reused": False, "verdict": "newFailure", "newFailures": ["t::a"], "outcomes": outcomes,
            "advance": advance, "split": split}
  record.update(data)
  return makeEvent("mergeTestRun", plan="P0001", **record)


def test_oldApprovalHasNoTestTimingAndMergeStillCompletes():
  state = deriveState([approvedEvent("P0001", [("T1", [])]),
                       makeEvent("taskMerged", plan="P0001", task="T1", commit="m1")], makePlans("P0001"), testConfig)
  assert state["plans"]["P0001"]["testTiming"] is None
  assert task(state)["status"] == TaskStatus.done
  assert task(state)["mergedCommit"] is None
  assert task(state)["testAfterMerge"] is False
  assert task(stateAfter())["testAfterMerge"] is True


def test_mergeWithVerifyWaitsForMergeTestButUnblocksDependents():
  state = stateAfter(*mergedCycle(1), tasks=(("T1", []), ("T2", ["T1"])))
  assert state["plans"]["P0001"]["testTiming"] == "afterMerge"
  assert task(state)["status"] == TaskStatus.mergedUntested
  assert task(state)["mergedCommit"] == "m1"
  assert [item["task"] for item in readyTasks(state)] == ["T2"]
  assert not isPlanFinished(state, "P0001")


def test_verifiedCompletesTaskAndThenPlan():
  verified = mergeTestRun({"T1": {"result": "verified", "names": []}}, passed=True, failures=[], verdict="passed",
                          newFailures=[])
  state = stateAfter(*mergedCycle(1), verified)
  assert task(state)["status"] == TaskStatus.done
  assert isPlanFinished(state, "P0001")
  assert state["plans"]["P0001"]["mergeTest"]["lastTested"]["commit"] == "m1"


def test_newFailureReopensTaskKeepingConversation():
  failed = mergeTestRun({"T1": {"result": "newFailure", "names": ["t::a"]}})
  before = task(stateAfter(*mergedCycle(1)))
  state = task(stateAfter(*mergedCycle(1), failed))
  assert (state["status"], state["round"]) == (TaskStatus.queued, 2)
  assert state["lastTest"] == {"source": "mergeTest", "verdict": "newFailure", "newFailures": ["t::a"],
                               "commit": "m1", "tail": "1 failed"}
  assert state["mergeFailures"] == ["t::a"]
  assert state["mergeFix"] is True
  assert state["lastReviewNotes"] is None
  for field in ("resumable", "sessionId", "sessionCli", "sessionModel"):
    assert state[field] == before[field]
  assert state["resumable"] is True and state["sessionId"] == "session-1"


def test_stillFailingAlsoReopensAndWrittenClearsMergeFix():
  failed = mergeTestRun({"T1": {"result": "newFailure", "names": ["t::a"]}})
  still = mergeTestRun({"T1": {"result": "stillFailing", "names": ["t::a"]}}, commit="m2", verdict="preexisting",
                       newFailures=[])
  state = task(stateAfter(*mergedCycle(1), failed, *mergedCycle(2, commit="m2"), still))
  assert (state["status"], state["round"], state["mergeFix"]) == (TaskStatus.queued, 3, True)
  rewritten = task(stateAfter(*mergedCycle(1), failed, *mergedCycle(2, commit="m2")[:2]))
  assert rewritten["mergeFix"] is False


def test_mergeTestFailureAfterLastRoundNeedsYou():
  events = []
  for roundNumber in (1, 2, 3):
    events += [*mergedCycle(roundNumber, commit=f"m{roundNumber}"),
               mergeTestRun({"T1": {"result": "newFailure", "names": ["t::a"]}}, commit=f"m{roundNumber}")]
  state = task(stateAfter(*events))
  assert state["status"] == TaskStatus.needsYou
  assert state["needs"] == {"kind": "tests", "reason": "第 3 輪合併後測試有新增的失敗，輪數用完：t::a"}
  retried = task(stateAfter(*events, makeEvent("userDecision", plan="P0001", task="T1", action="retry")))
  assert (retried["status"], retried["round"], retried["maxRounds"]) == (TaskStatus.queued, 4, 4)


def test_unknownNeedsYouThenRetryRetestsAndDoneCompletes():
  unknown = mergeTestRun({"T1": {"result": "unknown", "names": []}}, commit="abcdef1234", advance=False,
                         verdict="unknown", newFailures=[])
  stuck = task(stateAfter(*mergedCycle(1), unknown))
  assert stuck["status"] == TaskStatus.needsYou
  assert stuck["needs"]["kind"] == "mergeTest"
  assert "commit abcdef1" in stuck["needs"]["reason"]
  retried = task(stateAfter(*mergedCycle(1), unknown, makeEvent("userDecision", plan="P0001", task="T1", action="retry")))
  assert (retried["status"], retried["round"], retried["maxRounds"]) == (TaskStatus.mergedUntested, 1, 3)
  done = stateAfter(*mergedCycle(1), unknown, makeEvent("userDecision", plan="P0001", task="T1", action="done"))
  assert task(done)["status"] == TaskStatus.done
  assert isPlanFinished(done, "P0001")


def test_splitStartKeepsTasksAndClearEndsIt():
  tasks = (("T1", []), ("T2", []))
  merges = [*mergedCycle(1, "T1", "m1"), *mergedCycle(1, "T2", "m2")]
  start = mergeTestRun({"T1": {"result": "split", "names": []}, "T2": {"result": "split", "names": []}},
                       commit="m2", advance=False, split="start")
  state = stateAfter(*merges, start, tasks=tasks)
  assert [task(state, taskId)["status"] for taskId in ("T1", "T2")] == [TaskStatus.mergedUntested] * 2
  assert state["plans"]["P0001"]["mergeTest"] == {"lastTested": None, "split": start["data"]}
  first = mergeTestRun({"T1": {"result": "verified", "names": []}}, commit="m1", passed=True, failures=[],
                       verdict="passed", newFailures=[])
  last = mergeTestRun({"T2": {"result": "newFailure", "names": ["t::a"]}}, commit="m2", split="clear", reused=True)
  state = stateAfter(*merges, start, first, last, tasks=tasks)
  assert state["plans"]["P0001"]["mergeTest"] == {"lastTested": last["data"], "split": None}
  assert (task(state, "T1")["status"], task(state, "T2")["status"]) == (TaskStatus.done, TaskStatus.queued)


def test_outcomeForTaskNotWaitingForMergeTestIsIgnored():
  abandoned = [makeEvent("needsYou", plan="P0001", task="T1", kind="blocked", reason="卡住"),
               makeEvent("userDecision", plan="P0001", task="T1", action="abandon")]
  state = stateAfter(*abandoned, mergeTestRun({"T1": {"result": "verified", "names": []}}))
  assert task(state)["status"] == TaskStatus.abandoned
  assert state["plans"]["P0001"]["status"] == PlanStatus.abandoned


def test_mergeTestSkippedCompletesWaitingTasks():
  skipped = makeEvent("mergeTestSkipped", plan="P0001", covers=["T1"], reason="skipTests")
  state = stateAfter(*mergedCycle(1), skipped)
  assert task(state)["status"] == TaskStatus.done
  assert isPlanFinished(state, "P0001")


def needsYou(kind, **data):
  return makeEvent("needsYou", plan="P0001", task="T1", kind=kind, reason="停下", **data)


def decided(*events, **data):
  return task(stateAfter(*events, makeEvent("userDecision", plan="P0001", task="T1", **data)))


@pytest.mark.parametrize("kind, expected", [
  ("blocked", TaskStatus.queued), ("noChanges", TaskStatus.queued), ("invalidReview", TaskStatus.toReview),
  ("mergeFailed", TaskStatus.merging), ("mergeTest", TaskStatus.mergedUntested), ("other", TaskStatus.queued),
])
def test_retryReturnsToStageByNeedsKindWithoutSpendingRounds(kind, expected):
  state = decided(needsYou(kind), action="retry", note="補充")
  assert (state["status"], state["round"], state["maxRounds"]) == (expected, 1, 3)
  assert (state["needs"], state["failStreak"], state["userNote"], state["userDone"]) == (None, 0, "補充", False)
  assert (state["resolving"], state["needsClean"]) == (False, False)


def test_retryAfterReviewRoundsAddsOneRound():
  state = decided(needsYou("rounds"), action="retry")
  assert (state["status"], state["round"], state["maxRounds"], state["userNote"]) == (TaskStatus.queued, 2, 4, "")


def test_retryAfterMergeTestFailureKeepsMergeFixAndNote():
  events = []
  for roundNumber in (1, 2, 3):
    events += [*mergedCycle(roundNumber, commit=f"m{roundNumber}"),
               mergeTestRun({"T1": {"result": "newFailure", "names": ["t::a"]}}, commit=f"m{roundNumber}")]
  state = decided(*events, action="retry", note="看 t::a")
  assert (state["status"], state["round"], state["maxRounds"]) == (TaskStatus.queued, 4, 4)
  assert (state["mergeFix"], state["mergeFailures"], state["userNote"]) == (True, ["t::a"], "看 t::a")


@pytest.mark.parametrize("stage, expected, needsClean", [
  ("write", TaskStatus.queued, True), ("review", TaskStatus.toReview, False),
])
def test_retryAfterAgentFailedReturnsToFailedStage(stage, expected, needsClean):
  failed = [makeEvent("agentFailed", plan="P0001", task="T1", stage=stage, reason="exit 1")] * 2
  stuck = task(stateAfter(*failed))
  assert stuck["needs"]["kind"] == "agentFailed"
  state = decided(*failed, action="retry")
  assert (state["status"], state["needsClean"], state["round"], state["maxRounds"]) == (expected, needsClean, 1, 3)
  assert (state["needs"], state["failStreak"]) == (None, 0)


def test_retryAfterMergeConflictResolvesAgain():
  state = decided(needsYou("mergeConflict", conflicts=["shared.txt"]), action="retry", note="兩邊都留")
  assert (state["status"], state["resolving"], state["conflictFiles"]) == (TaskStatus.queued, True, ["shared.txt"])
  assert (state["round"], state["maxRounds"], state["userDone"], state["userNote"]) == (2, 4, False, "兩邊都留")
  assert state["conflictCount"] == 0


def test_doneMergesAgainUnlessAlreadyMerged():
  state = decided(needsYou("blocked"), action="done", note="不會記")
  assert (state["status"], state["userDone"], state["needs"], state["userNote"]) == (TaskStatus.merging, True, None, "")
  merged = decided(needsYou("mergeTest"), action="done")
  assert (merged["status"], merged["userDone"], merged["needs"]) == (TaskStatus.done, False, None)


def test_abandonClearsNeeds():
  state = decided(needsYou("tests"), action="abandon")
  assert (state["status"], state["needs"], state["round"], state["maxRounds"]) == (TaskStatus.abandoned, None, 1, 3)


def test_decisionIsIgnoredUnlessWaitingForYouOrActionUnknown():
  notWaiting = decided(action="retry", note="補充")
  assert (notWaiting["status"], notWaiting["userNote"], notWaiting["round"]) == (TaskStatus.queued, "", 1)
  unknown = decided(needsYou("rounds"), action="later", note="補充")
  assert (unknown["status"], unknown["needs"]["kind"], unknown["userNote"]) == (TaskStatus.needsYou, "rounds", "")
  assert (unknown["round"], unknown["maxRounds"]) == (1, 3)
