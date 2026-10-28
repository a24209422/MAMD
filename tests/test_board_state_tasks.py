from board_core import (PlanStatus, TaskStatus, agentLanes, deriveState, isPlanFinished,
                        readyTasks, tasksWithStatus)
from helpers import approvedEvent, makeEvent, makePlans, testConfig


def stateAfter(*events, tasks=(("T1", []),)):
  return deriveState([approvedEvent("P0001", list(tasks)), *events], makePlans("P0001"), testConfig)


def started(roundNumber, agent="claude", taskId="T1"):
  return makeEvent("taskStarted", plan="P0001", task=taskId, roundNumber=roundNumber, agent=agent,
                   worktree="wt", sessionId=f"session-{roundNumber}", resume=roundNumber > 1)


def written(taskId="T1", **data):
  return makeEvent("taskWritten", plan="P0001", task=taskId, commit="c1", summary="完成", **data)


def reviewStarted(roundNumber, taskId="T1"):
  return makeEvent("reviewStarted", plan="P0001", task=taskId, roundNumber=roundNumber, agent="codex")


def reviewFailed(roundNumber, taskId="T1"):
  return makeEvent("reviewFailed", plan="P0001", task=taskId, roundNumber=roundNumber, agent="codex",
                   notes=f"reviews/P0001-{taskId}-r{roundNumber}.md", mustFix=1, suggest=0)


def failCycle(roundNumber):
  return [started(roundNumber), written(), reviewStarted(roundNumber), reviewFailed(roundNumber)]


def decision(action, note="", taskId="T1"):
  return makeEvent("userDecision", plan="P0001", task=taskId, action=action, note=note)


def task(state, taskId="T1"):
  return state["tasks"][f"P0001/{taskId}"]


def test_happyPath():
  assert task(stateAfter(started(1)))["status"] == TaskStatus.writing
  assert task(stateAfter(started(1), written()))["status"] == TaskStatus.toReview
  assert task(stateAfter(started(1), written(), reviewStarted(1)))["status"] == TaskStatus.reviewing
  passed = makeEvent("reviewPassed", plan="P0001", task="T1", roundNumber=1, notes="reviews/P0001-T1-r1.md")
  assert task(stateAfter(started(1), written(), reviewStarted(1), passed))["status"] == TaskStatus.merging
  merged = makeEvent("taskMerged", plan="P0001", task="T1", commit="m1")
  assert task(stateAfter(started(1), written(), reviewStarted(1), passed, merged))["status"] == TaskStatus.done


def test_reviewFailedRequeuesNextRound():
  state = task(stateAfter(*failCycle(1)))
  assert state["status"] == TaskStatus.queued
  assert state["round"] == 2
  assert state["lastReviewNotes"] == "reviews/P0001-T1-r1.md"
  assert state["resumable"] is True


def test_thirdFailureNeedsYou():
  state = task(stateAfter(*failCycle(1), *failCycle(2), *failCycle(3)))
  assert state["status"] == TaskStatus.needsYou
  assert state["needs"] == {"kind": "rounds", "reason": "審查 3 次不通過"}


def test_retryAfterRoundsRaisesLimit():
  state = task(stateAfter(*failCycle(1), *failCycle(2), *failCycle(3), decision("retry", "錯誤訊息改中文")))
  assert state["status"] == TaskStatus.queued
  assert state["round"] == 4
  assert state["maxRounds"] == 4
  assert state["userNote"] == "錯誤訊息改中文"
  assert state["needs"] is None


def ranTests(roundNumber, **data):
  return makeEvent("testRun", plan="P0001", task="T1", roundNumber=roundNumber, agent="claude", **data)


def newFailureCycle(roundNumber):
  return [started(roundNumber), written(),
          ranTests(roundNumber, passed=False, tail="1 failed", verdict="newFailure", newFailures=["t::new"])]


def test_newFailureRequeuesNextRoundWithoutReview():
  state = task(stateAfter(*newFailureCycle(1)))
  assert state["status"] == TaskStatus.queued
  assert state["round"] == 2
  assert state["resumable"] is True
  assert state["lastTest"]["verdict"] == "newFailure"


def test_newFailureAfterLastRoundNeedsYouThenRetryRaisesLimit():
  stuck = task(stateAfter(*failCycle(1), *newFailureCycle(2), *newFailureCycle(3)))
  assert stuck["status"] == TaskStatus.needsYou
  assert stuck["needs"] == {"kind": "tests", "reason": "第 3 輪測試有新增的失敗，輪數用完：t::new"}
  retried = task(stateAfter(*failCycle(1), *newFailureCycle(2), *newFailureCycle(3), decision("retry", "先修測試")))
  assert (retried["status"], retried["round"], retried["maxRounds"]) == (TaskStatus.queued, 4, 4)
  assert retried["userNote"] == "先修測試" and retried["needs"] is None


def test_newFailureWithoutNamesNeedsYouWithPlainReason():
  cycles = [event for roundNumber in (1, 2, 3) for event in newFailureCycle(roundNumber)]
  cycles[-1]["data"]["newFailures"] = []
  assert task(stateAfter(*cycles))["needs"]["reason"] == "第 3 輪測試有新增的失敗，輪數用完"


def test_testRunWithoutNewFailureOnlyRecords():
  # 沒有 verdict 的舊事件，以及 passed、preexisting、unknown，都照常等審查
  for data in ({"passed": False, "tail": "1 failed"}, {"passed": True, "tail": "ok", "verdict": "passed"},
               {"passed": False, "tail": "x", "verdict": "preexisting", "newFailures": []},
               {"passed": False, "tail": "x", "verdict": "unknown", "newFailures": []}):
    before = task(stateAfter(started(1), written()))
    after = task(stateAfter(started(1), written(), ranTests(1, **data)))
    assert after["lastTest"] == data
    assert {**after, "lastTest": None} == before


def test_writtenWithTestingWaitsForTestRunBeforeReview():
  testing = task(stateAfter(started(1), written(testing=True)))
  assert testing["status"] == TaskStatus.testing
  for verdict in ("passed", "preexisting"):
    after = task(stateAfter(started(1), written(testing=True), ranTests(1, passed=verdict == "passed", tail="x",
                                                                      verdict=verdict, newFailures=[])))
    assert after["status"] == TaskStatus.toReview
  failed = task(stateAfter(started(1), written(testing=True),
                           ranTests(1, passed=False, tail="1 failed", verdict="newFailure", newFailures=["t::new"])))
  assert (failed["status"], failed["round"]) == (TaskStatus.queued, 2)


def test_writtenWithoutTestingFieldStillGoesToReview():
  assert task(stateAfter(started(1), written()))["status"] == TaskStatus.toReview
  assert task(stateAfter(started(1), written(testing=False)))["status"] == TaskStatus.toReview


def test_agentFailedRetriesOnceThenNeedsYou():
  failed = makeEvent("agentFailed", plan="P0001", task="T1", stage="write", reason="exit 1")
  once = task(stateAfter(started(1), failed))
  assert once["status"] == TaskStatus.queued
  assert once["needsClean"] is True
  assert once["resumable"] is False
  twice = task(stateAfter(started(1), failed, started(1), failed))
  assert twice["status"] == TaskStatus.needsYou
  assert twice["needs"]["kind"] == "agentFailed"
  assert twice["needs"]["stage"] == "write"


def test_reviewAgentFailedReturnsToReview():
  failed = makeEvent("agentFailed", plan="P0001", task="T1", stage="review", reason="timeout")
  assert task(stateAfter(started(1), written(), reviewStarted(1), failed))["status"] == TaskStatus.toReview


def test_successResetsFailStreak():
  failed = makeEvent("agentFailed", plan="P0001", task="T1", stage="write", reason="exit 1")
  state = task(stateAfter(started(1), failed, started(1), written(), reviewStarted(1), reviewFailed(1), started(2), failed))
  assert state["status"] == TaskStatus.queued


def test_blockedThenRetryResumes():
  blocked = makeEvent("taskBlocked", plan="P0001", task="T1", reason="不知道 API 位址")
  state = task(stateAfter(started(1), blocked))
  assert state["status"] == TaskStatus.needsYou
  assert state["needs"]["kind"] == "blocked"
  retried = task(stateAfter(started(1), blocked, decision("retry", "API 在 /api/upload")))
  assert retried["status"] == TaskStatus.queued
  assert retried["resumable"] is True
  assert retried["round"] == 1


def test_userDoneGoesToMerging():
  blocked = makeEvent("taskBlocked", plan="P0001", task="T1", reason="卡住")
  state = task(stateAfter(started(1), blocked, decision("done")))
  assert state["status"] == TaskStatus.merging
  assert state["userDone"] is True


def test_interruptedWriteRequeuesSameRound():
  interrupted = makeEvent("taskInterrupted", plan="P0001", task="T1", stage="write")
  state = task(stateAfter(started(1), interrupted))
  assert state["status"] == TaskStatus.queued
  assert state["round"] == 1
  assert state["needsClean"] is True
  assert state["failStreak"] == 0


def test_needsYouKindsDecideRetryTarget():
  invalid = makeEvent("needsYou", plan="P0001", task="T1", kind="invalidReview", reason="格式錯")
  assert task(stateAfter(invalid, decision("retry")))["status"] == TaskStatus.toReview
  noChanges = makeEvent("needsYou", plan="P0001", task="T1", kind="noChanges", reason="沒有改動")
  assert task(stateAfter(noChanges, decision("retry")))["status"] == TaskStatus.queued
  mergeFailed = makeEvent("needsYou", plan="P0001", task="T1", kind="mergeFailed", reason="合併失敗：未追蹤檔案")
  assert task(stateAfter(mergeFailed, decision("retry")))["status"] == TaskStatus.merging
  internalError = makeEvent("needsYou", plan="P0001", task="T1", kind="internalError", reason="處理結果出錯")
  assert task(stateAfter(internalError, decision("retry")))["status"] == TaskStatus.queued


def test_decisionIgnoredUnlessNeedsYou():
  assert task(stateAfter(started(1), decision("abandon")))["status"] == TaskStatus.writing


def test_abandonTaskAbandonsDependentsAndPlan():
  blocked = makeEvent("taskBlocked", plan="P0001", task="T1", reason="卡住")
  state = stateAfter(started(1), blocked, decision("abandon"), tasks=(("T1", []), ("T2", ["T1"])))
  assert task(state, "T1")["status"] == TaskStatus.abandoned
  assert task(state, "T2")["status"] == TaskStatus.abandoned
  assert state["plans"]["P0001"]["status"] == PlanStatus.abandoned


def test_readyTasksRespectsDependenciesAndPause():
  state = stateAfter(tasks=(("T1", []), ("T2", ["T1"]), ("T3", [])))
  assert [taskState["task"] for taskState in readyTasks(state)] == ["T1", "T3"]
  merged = makeEvent("taskMerged", plan="P0001", task="T1", commit="m1")
  state = stateAfter(merged, tasks=(("T1", []), ("T2", ["T1"])))
  assert [taskState["task"] for taskState in readyTasks(state)] == ["T2"]
  paused = deriveState([approvedEvent("P0001", [("T1", [])]), makeEvent("paused", reason="停止")],
                       makePlans("P0001"), testConfig)
  assert readyTasks(paused) == []


def test_tasksWithStatus():
  state = stateAfter(started(1), written())
  assert [taskState["task"] for taskState in tasksWithStatus(state, TaskStatus.toReview)] == ["T1"]


def test_isPlanFinished():
  merged = makeEvent("taskMerged", plan="P0001", task="T1", commit="m1")
  assert isPlanFinished(stateAfter(merged), "P0001") is True
  assert isPlanFinished(stateAfter(), "P0001") is False


def test_agentLanes():
  assert agentLanes("writer", {"maxParallel": 3}) == ["writer", "writer-sub1", "writer-sub2"]
  assert agentLanes("reviewer", {"maxParallel": 1}) == ["reviewer"]


def conflicted(taskId="T1", conflicts=("a.py",)):
  return makeEvent("taskConflicted", plan="P0001", task=taskId, roundNumber=1, conflicts=list(conflicts),
                   reason="合併衝突：" + "、".join(conflicts))


def passCycle(roundNumber):
  return [started(roundNumber), written(), reviewStarted(roundNumber),
          makeEvent("reviewPassed", plan="P0001", task="T1", roundNumber=roundNumber, agent="codex",
                    notes=f"reviews/P0001-T1-r{roundNumber}.md", mustFix=0, suggest=0)]


def test_conflictRequeuesForResolveWithExtraRound():
  state = task(stateAfter(*passCycle(1), conflicted(conflicts=("a.py", "b.py"))))
  assert state["status"] == TaskStatus.queued
  assert state["resolving"] is True
  assert state["conflictFiles"] == ["a.py", "b.py"]
  assert state["conflictCount"] == 1
  # 解衝突是新的一輪，但不吃掉審查退回的額度
  assert state["round"] == 2
  assert state["maxRounds"] == 4


def test_resolveWriteClearsResolvingAndRemembersMode():
  resolvedWrite = makeEvent("taskWritten", plan="P0001", task="T1", commit="c2", summary="解完", mode="resolve",
                            conflicts=["b.py"])
  state = task(stateAfter(*passCycle(1), conflicted(), started(2), resolvedWrite))
  assert state["status"] == TaskStatus.toReview
  assert state["resolving"] is False
  assert state["lastWriteMode"] == "resolve"
  # 審查要看 Claude 實際解的檔案，不是當初在 plan 端合併時的清單
  assert state["conflictFiles"] == ["b.py"]
  assert state["conflictCount"] == 1
  assert task(stateAfter(*passCycle(1)))["lastWriteMode"] == "write"


def test_resolveSurvivesInterruptionAndAgentFailure():
  interrupted = makeEvent("taskInterrupted", plan="P0001", task="T1", stage="write")
  failed = makeEvent("agentFailed", plan="P0001", task="T1", stage="write", reason="還有衝突標記")
  for stopEvent in (interrupted, failed):
    state = task(stateAfter(*passCycle(1), conflicted(), started(2), stopEvent))
    assert state["status"] == TaskStatus.queued
    assert state["resolving"] is True
    assert state["needsClean"] is True


def test_retryAfterMergeConflictAsksClaudeToResolve():
  conflict = makeEvent("needsYou", plan="P0001", task="T1", kind="mergeConflict", reason="合併衝突：a.py",
                       conflicts=["a.py"])
  state = task(stateAfter(*passCycle(1), conflict, decision("retry", "保留兩邊")))
  assert state["status"] == TaskStatus.queued
  assert state["resolving"] is True
  assert state["conflictFiles"] == ["a.py"]
  assert state["round"] == 2
  assert state["maxRounds"] == 4
  assert state["userNote"] == "保留兩邊"


def test_writeWithoutClaudeStillClearsNeedsClean():
  # 重新合併沒有衝突時不會有 taskStarted，寫成功本身就代表 worktree 已經乾淨
  failed = makeEvent("agentFailed", plan="P0001", task="T1", stage="write", reason="還有衝突標記")
  cleanWrite = makeEvent("taskWritten", plan="P0001", task="T1", commit="c3", summary="沒有衝突", mode="resolve",
                         conflicts=[])
  state = task(stateAfter(*passCycle(1), conflicted(), started(2), failed, cleanWrite))
  assert state["needsClean"] is False


def test_codexSessionIsRememberedWhenWritten():
  start = makeEvent("taskStarted", plan="P0001", task="T1", roundNumber=1, agent="writer", sessionId=None, cli="codex")
  writtenEvent = makeEvent("taskWritten", plan="P0001", task="T1", commit="c1", sessionId="thread-1", cli="codex")
  state = task(stateAfter(start, writtenEvent))
  assert (state["sessionId"], state["sessionCli"], state["resumable"]) == ("thread-1", "codex", True)


def test_startEventsWithoutCliAreClaudeSessions():
  state = task(stateAfter(started(1)))
  assert (state["sessionId"], state["sessionCli"]) == ("session-1", "claude")


def test_blockedRemembersCodexSession():
  start = makeEvent("taskStarted", plan="P0001", task="T1", roundNumber=1, agent="writer", sessionId=None, cli="codex")
  blocked = makeEvent("taskBlocked", plan="P0001", task="T1", reason="缺資料", sessionId="thread-9", cli="codex")
  state = task(stateAfter(start, blocked))
  assert (state["sessionId"], state["sessionCli"]) == ("thread-9", "codex")


def test_startedJobsRememberCliAndModel():
  start = makeEvent("taskStarted", plan="P0001", task="T1", roundNumber=1, agent="writer", sessionId=None,
                    cli="codex", model="gpt-6.1-sol")
  writtenEvent = makeEvent("taskWritten", plan="P0001", task="T1", commit="c1", sessionId="thread-1", cli="codex",
                           model="gpt-6.1-sol")
  review = makeEvent("reviewStarted", plan="P0001", task="T1", roundNumber=1, agent="reviewer", cli="claude",
                     model="opus")
  state = task(stateAfter(start, writtenEvent, review))
  assert (state["sessionCli"], state["sessionModel"]) == ("codex", "gpt-6.1-sol")
  assert (state["reviewCli"], state["reviewModel"]) == ("claude", "opus")


def test_oldEventsWithoutModelLeaveItEmpty():
  state = task(stateAfter(started(1), written(), reviewStarted(1)))
  assert (state["sessionModel"], state["reviewCli"], state["reviewModel"]) == (None, None, None)
