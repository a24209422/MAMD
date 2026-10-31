import json

import pytest

from board_core import PlanStatus, TaskStatus, deriveState, loadPlans, readEvents, readyTasks
from board_sync import (buildSnapshot, checkpointPath, loadProgress, progressPath, readCheckpoint, writeSnapshot)
from helpers import makeEvent, testConfig
from test_board_sync import approved, baseSha, makeProject, portableView, projectEvents, projectId, rows


def writeEvents(projectDir, events):
  directory = projectDir / ".board"
  directory.mkdir(parents=True, exist_ok=True)
  with open(directory / "events.jsonl", "a", encoding="utf-8", newline="\n") as handle:
    for event in events:
      handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def snapshotOf(projectDir, events, previous=None, identity=projectId):
  plans = loadPlans(projectDir)
  return buildSnapshot(deriveState(events, plans, testConfig), plans, identity, previous=previous)


def localView(projectDir, events):
  plans = loadPlans(projectDir)
  return portableView(deriveState(events, plans, testConfig))


def load(projectDir):
  return loadProgress(projectDir, testConfig)


def started(taskId, roundNumber=1, planIdValue="P0001"):
  return makeEvent("taskStarted", plan=planIdValue, task=taskId, roundNumber=roundNumber, agent="claude")


def written(taskId, planIdValue="P0001", **data):
  return makeEvent("taskWritten", plan=planIdValue, task=taskId, commit="2" * 40, **data)


def machines(tmp_path, writePlan):
  return makeProject(tmp_path / "A", writePlan), makeProject(tmp_path / "B", writePlan)


def test_withoutSnapshotMatchesEventReplay(tmp_path, writePlan):
  projectDir = makeProject(tmp_path / "proj", writePlan)
  events = projectEvents(projectDir)
  writeEvents(projectDir, events)

  loaded = load(projectDir)

  assert portableView(loaded["state"]) == localView(projectDir, events)
  assert loaded["state"] == deriveState(readEvents(projectDir), loadPlans(projectDir), testConfig)
  assert loaded["sync"]["status"] == "none"
  assert not checkpointPath(projectDir).exists()


def test_firstRestoreBuildsStateFromSnapshot(tmp_path, writePlan):
  source, target = machines(tmp_path, writePlan)
  events = projectEvents(source)
  snapshot = snapshotOf(source, events)
  writeSnapshot(target, snapshot)

  loaded = load(target)

  assert portableView(loaded["state"]) == localView(source, events)
  assert loaded["sync"]["status"] == "accepted"
  assert loaded["sync"]["baseRevision"] == snapshot["revision"]
  assert readCheckpoint(target)["base"] == snapshot
  assert loaded["state"]["plans"]["P0003"]["status"] == PlanStatus.draft


def test_repeatedLoadsKeepRoundsAndApprovals(tmp_path, writePlan):
  source, target = machines(tmp_path, writePlan)
  writeSnapshot(target, snapshotOf(source, projectEvents(source)))
  first = load(target)
  assert [load(target)["state"] for _ in range(3)] == [first["state"]] * 3

  writeEvents(target, [makeEvent("planSubmitted", plan="P0002"),
                       makeEvent("planApproved", plan="P0002", baseCommit=baseSha, tasks=[
                         {"id": "T1", "text": "新任務", "dependsOn": [], "requirementIds": ["R1"]}]),
                       started("T1", roundNumber=2)])
  loads = [load(target) for _ in range(3)]

  assert all(loaded["sync"]["status"] == "same" for loaded in loads)
  for loaded in loads:
    tasks = loaded["state"]["tasks"]
    assert (tasks["P0001/T1"]["status"], tasks["P0001/T1"]["round"]) == (TaskStatus.writing, 2)
    assert sorted(key for key in tasks if key.startswith("P0002/")) == ["P0002/T1"]
    assert tasks["P0002/T1"]["round"] == 1
    assert loaded["state"]["plans"]["P0002"]["status"] == PlanStatus.active


def test_sourceMachineDoesNotReplayItsOwnEvents(tmp_path, writePlan):
  source = makeProject(tmp_path / "A", writePlan)
  events = projectEvents(source)
  writeEvents(source, events)
  writeSnapshot(source, snapshotOf(source, events))

  loads = [load(source) for _ in range(2)]

  assert [loaded["sync"]["status"] for loaded in loads] == ["accepted", "same"]
  for loaded in loads:
    assert portableView(loaded["state"]) == localView(source, events)
    assert loaded["state"]["tasks"]["P0001/T1"]["round"] == 2


def test_skippedRevisionsAcceptDescendant(tmp_path, writePlan):
  source, target = machines(tmp_path, writePlan)
  events = projectEvents(source)
  first = snapshotOf(source, events)
  writeSnapshot(target, first)
  load(target)
  # 沒有持久進度的本機事件（開始後中斷回到原狀）不算本機新進度
  writeEvents(target, [started("T1", roundNumber=2),
                       makeEvent("taskInterrupted", plan="P0001", task="T1", stage="write")])

  later = [started("T1", roundNumber=2), written("T1"), makeEvent("reviewStarted", plan="P0001", task="T1")]
  chain = [first]
  for count in range(1, len(later) + 1):
    chain.append(snapshotOf(source, events + later[:count], previous=chain[-1]))
  writeSnapshot(target, chain[-1])

  loaded = load(target)

  assert loaded["sync"]["status"] == "accepted"
  assert portableView(loaded["state"]) == localView(source, events + later)
  assert readCheckpoint(target)["eventCount"] == 2
  again = load(target)
  assert again["sync"]["status"] == "same" and again["state"] == loaded["state"]


def test_localProgressIsNotOverwrittenByNewerSnapshot(tmp_path, writePlan):
  source, target = machines(tmp_path, writePlan)
  events = projectEvents(source)
  first = snapshotOf(source, events)
  writeSnapshot(target, first)
  load(target)
  writeEvents(target, [started("T1", roundNumber=2), written("T1")])
  writeSnapshot(target, snapshotOf(source, events + [started("T3", roundNumber=2)], previous=first))

  loads = [load(target) for _ in range(2)]

  for loaded in loads:
    assert loaded["sync"]["status"] == "diverged"
    assert loaded["sync"]["message"]
    tasks = loaded["state"]["tasks"]
    assert (tasks["P0001/T1"]["status"], tasks["P0001/T1"]["round"]) == (TaskStatus.toReview, 2)
    assert tasks["P0001/T3"]["status"] == TaskStatus.mergedUntested
  assert readCheckpoint(target)["base"] == first


def test_olderSnapshotKeepsAcceptedProgress(tmp_path, writePlan):
  source, target = machines(tmp_path, writePlan)
  events = projectEvents(source)
  first = snapshotOf(source, events)
  second = snapshotOf(source, events + [started("T1", roundNumber=2)], previous=first)
  writeSnapshot(target, second)
  load(target)
  writeSnapshot(target, first)

  loaded = load(target)

  assert loaded["sync"]["status"] == "older" and loaded["sync"]["message"]
  assert loaded["state"]["tasks"]["P0001/T1"]["status"] == TaskStatus.writing
  assert readCheckpoint(target)["base"] == second


def test_unrelatedSnapshotKeepsAcceptedProgress(tmp_path, writePlan):
  source, target = machines(tmp_path, writePlan)
  events = projectEvents(source)
  writeSnapshot(target, snapshotOf(source, events))
  accepted = load(target)
  for other in (snapshotOf(source, events[:2]), snapshotOf(source, events, identity="f" * 32)):
    writeSnapshot(target, other)

    loaded = load(target)

    assert loaded["sync"]["status"] == "unrelated" and loaded["sync"]["message"]
    assert loaded["state"] == accepted["state"]


def test_localProgressWithoutCheckpointIsKept(tmp_path, writePlan):
  source, target = machines(tmp_path, writePlan)
  localEvents = [makeEvent("planSubmitted", plan="P0002")]
  writeEvents(target, localEvents)
  writeSnapshot(target, snapshotOf(source, projectEvents(source)))

  loaded = load(target)

  assert loaded["sync"]["status"] == "unrelated" and loaded["sync"]["message"]
  assert loaded["state"] == deriveState(localEvents, loadPlans(target), testConfig)
  assert not checkpointPath(target).exists()


def test_corruptSnapshotKeepsLocalProgress(tmp_path, writePlan):
  source, target = machines(tmp_path, writePlan)
  events = projectEvents(source)
  writeSnapshot(target, snapshotOf(source, events))
  accepted = load(target)
  progressPath(target).write_text("{壞掉", encoding="utf-8")

  loaded = load(target)

  assert loaded["sync"]["status"] == "error"
  assert ".agent-board/progress.json" in loaded["sync"]["message"]
  assert loaded["state"] == accepted["state"]


@pytest.mark.parametrize("corrupt", [lambda data: [], lambda data: {**data, "eventCount": -1}])
def test_corruptCheckpointIsReported(tmp_path, writePlan, corrupt):
  source, target = machines(tmp_path, writePlan)
  writeSnapshot(target, snapshotOf(source, projectEvents(source)))
  load(target)
  checkpointPath(target).write_text(json.dumps(corrupt(readCheckpoint(target))), encoding="utf-8")

  loaded = load(target)

  assert loaded["sync"]["status"] == "error"
  assert "sync-checkpoint.json" in loaded["sync"]["message"]
  assert loaded["state"] == deriveState([], loadPlans(target), testConfig)


def test_pauseStaysLocal(tmp_path, writePlan):
  source, target = machines(tmp_path, writePlan)
  writeEvents(target, [makeEvent("paused", reason="下班")])
  writeSnapshot(target, snapshotOf(source, projectEvents(source)))

  loaded = load(target)

  assert loaded["sync"]["status"] == "accepted"
  assert (loaded["state"]["paused"], loaded["state"]["pauseReason"]) == (True, "下班")
  assert load(target)["state"]["paused"] is True


def approvedWith(planIdValue, taskIds):
  return makeEvent("planApproved", plan=planIdValue, baseCommit=baseSha, testTiming="afterMerge", tasks=[
    {"id": taskId, "text": f"{taskId} 內容", "dependsOn": [], "requirementIds": ["R1"]} for taskId in taskIds])


def richProject(root, writePlan):
  root.mkdir(parents=True)
  for number in range(1, 8):
    writePlan(f"000{number}-plan.md", rows[:1], planId=f"P000{number}", projectDir=root)
  writePlan("0008-plan.md", rows[:1], planId="P0008", projectDir=root, extraMeta="supersedes: P0007\n")
  return root


def richEvents():
  taskIds = [f"T{index}" for index in range(1, 11)]
  merged = [written("T7"), makeEvent("taskMerged", plan="P0001", task="T7", commit="4" * 40, verify=True)]
  return [
    makeEvent("planSubmitted", plan="P0001"), approvedWith("P0001", taskIds),
    started("T2"),
    started("T3"), written("T3", testing=True),
    started("T4"), written("T4"),
    started("T5"), written("T5"), makeEvent("reviewStarted", plan="P0001", task="T5"),
    started("T6"), written("T6"), makeEvent("reviewPassed", plan="P0001", task="T6", notes="reviews/P0001-T6-r1.md"),
    started("T7"), *merged,
    started("T8"), written("T8"), makeEvent("taskMerged", plan="P0001", task="T8", commit="5" * 40, verify=True),
    makeEvent("mergeTestRun", plan="P0001", advance=True, commit="5" * 40, passed=True,
              outcomes={"T8": {"result": "verified"}}),
    makeEvent("needsYou", plan="P0001", task="T9", kind="blocked", reason="缺資料"),
    makeEvent("needsYou", plan="P0001", task="T10", kind="blocked", reason="不做了"),
    makeEvent("userDecision", plan="P0001", task="T10", action="abandon"),
    makeEvent("planSubmitted", plan="P0002"), makeEvent("planRejected", plan="P0002", reason="重寫"),
    makeEvent("planSubmitted", plan="P0004"),
    makeEvent("planSubmitted", plan="P0005"), approvedWith("P0005", ["T1"]),
    makeEvent("planCompleted", plan="P0005"),
    makeEvent("planSubmitted", plan="P0006"), approvedWith("P0006", ["T1"]),
    makeEvent("planAbandoned", plan="P0006"),
    makeEvent("planSubmitted", plan="P0007"), makeEvent("planRejected", plan="P0007", reason="被新版取代"),
  ]


def test_allPlanAndTaskStatusesRebuild(tmp_path, writePlan):
  source = richProject(tmp_path / "A", writePlan)
  target = richProject(tmp_path / "B", writePlan)
  events = richEvents()
  writeSnapshot(target, snapshotOf(source, events))

  state = load(target)["state"]

  assert portableView(state) == localView(source, events)
  planStatuses = {planState["status"] for planState in state["plans"].values()}
  taskStatuses = {taskState["status"] for taskState in state["tasks"].values()}
  assert planStatuses == {value for name, value in vars(PlanStatus).items() if not name.startswith("_")}
  assert taskStatuses == {value for name, value in vars(TaskStatus).items() if not name.startswith("_")}


def test_changedPlanFileBlocksApprovedPlan(tmp_path, writePlan):
  source, target = machines(tmp_path, writePlan)
  writeSnapshot(target, snapshotOf(source, projectEvents(source)))
  writePlan("0001-first.md", rows[:2], planId="P0001", projectDir=target)
  writePlan("0003-draft.md", rows, planId="P0003", projectDir=target)

  loaded = load(target)

  reason = loaded["sync"]["blockedPlans"]["P0001"]
  assert "plans/0001-first.md" in reason
  assert loaded["state"]["plans"]["P0001"]["syncBlocked"] == reason
  assert loaded["state"]["plans"]["P0003"]["status"] == PlanStatus.draft
  assert [task for task in readyTasks(loaded["state"]) if task["plan"] == "P0001"] == []


def test_missingPlanFileBlocksApprovedPlan(tmp_path, writePlan):
  source, target = machines(tmp_path, writePlan)
  writeSnapshot(target, snapshotOf(source, projectEvents(source)))
  (target / "plans" / "0001-first.md").unlink()

  loaded = load(target)

  assert "找不到" in loaded["sync"]["blockedPlans"]["P0001"]
  assert readyTasks(loaded["state"]) == []


def test_matchingPlanFilesKeepTasksReady(tmp_path, writePlan):
  source, target = machines(tmp_path, writePlan)
  writeSnapshot(target, snapshotOf(source, projectEvents(source)))

  loaded = load(target)

  assert loaded["sync"]["blockedPlans"] == {}
  assert [task["task"] for task in readyTasks(loaded["state"])] == ["T1"]
