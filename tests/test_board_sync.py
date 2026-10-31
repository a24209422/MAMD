import json
import shutil

import pytest

import board_sync
from board_core import PlanStatus, TaskStatus, deriveState, loadPlans
from board_sync import (SyncError, buildSnapshot, newProjectId, parseSnapshot, progressPath, readSnapshot,
                        serializeSnapshot, stateFromSnapshot, writeSnapshot)
from helpers import makeEvent, testConfig

projectId = "0123456789abcdef0123456789abcdef"
baseSha = "1" * 40
rows = ["| R1 需求 | 理解 | D1 做法 | 不採用 | T1 第一個 | | 驗證 |",
        "| R1 | | D1 | | T2 第二個 | T1 | 驗證 |",
        "| R1 | | D1 | | T3 第三個 | | 驗證 |"]


def approved(planIdValue):
  return makeEvent("planApproved", plan=planIdValue, baseCommit=baseSha, branch=f"plan/{planIdValue}",
                   testTiming="afterMerge", tasks=[
                     {"id": "T1", "text": "第一個", "dependsOn": [], "requirementIds": ["R1"]},
                     {"id": "T2", "text": "第二個", "dependsOn": ["T1"], "requirementIds": ["R1"]},
                     {"id": "T3", "text": "第三個", "dependsOn": [], "requirementIds": ["R1"]}])


def projectEvents(projectDir):
  # 事件裡刻意混入本機路徑、對話編號、測試指令和輸出，驗證它們不會進到交接檔
  local = str(projectDir)
  return [
    makeEvent("planSubmitted", plan="P0001"),
    approved("P0001"),
    makeEvent("testBaseline", plan="P0001", command=f"{local}/.venv/python -m pytest", commit=baseSha,
              passed=False, failures=["tests/test_a.py::test_old"], qualityChecked=False, qualityFailures=[],
              tail=f"FAILED {local}/tests/test_a.py"),
    makeEvent("taskStarted", plan="P0001", task="T1", roundNumber=1, agent="claude",
              worktree=f"{local}-worktrees/P0001-T1", sessionId="session-secret-1", cli="claude", model="opus"),
    makeEvent("taskWritten", plan="P0001", task="T1", commit="2" * 40, summary="完成", testing=True,
              sessionId="session-secret-1"),
    makeEvent("testRun", plan="P0001", task="T1", roundNumber=1, passed=True, tail=f"pid 4242 at {local}",
              verdict="pass", newFailures=[], qualityChecked=True, qualityFailures=[], commit="2" * 40,
              failures=[], command=f"{local}/.venv/python -m pytest"),
    makeEvent("reviewStarted", plan="P0001", task="T1", roundNumber=1, agent="codex", cli="codex", model="gpt"),
    makeEvent("reviewFailed", plan="P0001", task="T1", roundNumber=1, notes="reviews/P0001-T1-r1.md",
              mustFix=1, suggest=0),
    makeEvent("needsYou", plan="P0001", task="T1", kind="noChanges", reason="沒有改動"),
    makeEvent("userDecision", plan="P0001", task="T1", action="retry", note="請先修 A"),
    makeEvent("taskStarted", plan="P0001", task="T3", roundNumber=1, agent="claude-sub1", sessionId="session-secret-3"),
    makeEvent("taskWritten", plan="P0001", task="T3", commit="3" * 40, summary="完成"),
    makeEvent("reviewStarted", plan="P0001", task="T3", roundNumber=1, agent="codex"),
    makeEvent("reviewPassed", plan="P0001", task="T3", roundNumber=1, notes="reviews/P0001-T3-r1.md"),
    makeEvent("taskMerged", plan="P0001", task="T3", commit="4" * 40, verify=True),
    makeEvent("needsYou", plan="P0001", task="T2", kind="commitFailed",
              reason=f"commit 失敗：fatal: Unable to create '{local}\\.git\\index.lock'"),
    makeEvent("planSubmitted", plan="P0002"),
    makeEvent("planRejected", plan="P0002", reason="範圍太大"),
  ]


def makeProject(root, writePlan):
  root.mkdir(parents=True)
  writePlan("0001-first.md", rows, planId="P0001", projectDir=root)
  writePlan("0002-second.md", rows[:1], planId="P0002", projectDir=root)
  writePlan("0003-draft.md", rows[:1], planId="P0003", projectDir=root)
  return root


def projectState(projectDir):
  plans = loadPlans(projectDir)
  return plans, deriveState(projectEvents(projectDir), plans, testConfig)


def exported(projectDir, branches=None):
  plans, state = projectState(projectDir)
  return buildSnapshot(state, plans, projectId, branches=branches or {"plan/P0001": "5" * 40})


portableTaskFields = ("plan", "task", "text", "dependsOn", "requirementIds", "status", "round", "maxRounds",
                      "userNote", "userDone", "failStreak", "lastCommit", "mergedCommit", "testAfterMerge",
                      "resolving", "conflictFiles", "conflictCount", "lastWriteMode", "mergeFix", "mergeFailures",
                      "reviews", "lastReviewNotes")
portablePlanFields = ("id", "status", "rejectReason", "baseCommit", "testTiming")


def portableView(state):
  return ({planIdValue: {field: planState[field] for field in portablePlanFields}
           for planIdValue, planState in state["plans"].items() if planState["status"] != PlanStatus.draft},
          {key: {field: taskState[field] for field in portableTaskFields} for key, taskState in state["tasks"].items()})


def test_twoProjectPathsReadSameSnapshotWithSamePortableFields(tmp_path, writePlan):
  first = makeProject(tmp_path / "電腦1" / "專案 a", writePlan)
  second = makeProject(tmp_path / "other disk" / "clone", writePlan)
  _, sourceState = projectState(first)
  writeSnapshot(first, exported(first))
  progressPath(second).parent.mkdir()
  shutil.copyfile(progressPath(first), progressPath(second))

  restored = [stateFromSnapshot(readSnapshot(directory), loadPlans(directory)) for directory in (first, second)]

  assert portableView(restored[0]) == portableView(restored[1]) == portableView(sourceState)
  task = restored[1]["tasks"]["P0001/T1"]
  assert (task["status"], task["round"], task["userNote"]) == (TaskStatus.queued, 2, "請先修 A")
  assert restored[1]["tasks"]["P0001/T3"]["status"] == TaskStatus.mergedUntested
  assert restored[1]["plans"]["P0002"]["rejectReason"] == "範圍太大"
  assert restored[1]["plans"]["P0003"]["status"] == PlanStatus.draft


def test_restoredStateRestartsLocalOnlyFields(tmp_path, writePlan):
  projectDir = makeProject(tmp_path / "proj", writePlan)
  restored = stateFromSnapshot(exported(projectDir), loadPlans(projectDir))
  task = restored["tasks"]["P0001/T1"]
  assert (task["sessionId"], task["agent"], task["resumable"], task["sessionCli"]) == (None, None, False, None)
  assert restored["plans"]["P0001"]["title"] == "測試 plan"
  assert restored["paused"] is False


def test_snapshotFileHasNoMachineLocalData(tmp_path, writePlan):
  projectDir = makeProject(tmp_path / "proj", writePlan)
  writeSnapshot(projectDir, exported(projectDir))
  text = progressPath(projectDir).read_text(encoding="utf-8")
  for forbidden in (str(tmp_path), str(tmp_path).replace("\\", "/"), "session-secret", "pid 4242", "pytest",
                    "worktrees", "index.lock", "claude", "codex", "opus", "FAILED"):
    assert forbidden not in text
  assert "P0003" not in json.loads(text)["plans"]


def test_reasonKeepsMessageWithoutAbsolutePath(tmp_path, writePlan):
  projectDir = makeProject(tmp_path / "proj", writePlan)
  needs = exported(projectDir)["plans"]["P0001"]["tasks"][1]["needs"]
  assert needs["kind"] == "commitFailed"
  assert needs["reason"].startswith("commit 失敗：fatal: Unable to create '")
  assert str(tmp_path) not in needs["reason"]


def test_longReasonIsShortened():
  assert len(board_sync.briefReason("x" * 5000)) <= board_sync.maxReasonLength
  assert board_sync.briefReason("見 /home/me/proj/a.txt 與 C:\\Users\\me\\b.txt") == "見 <路徑> 與 <路徑>"
  assert board_sync.briefReason("合併 plan/P0001 與 reviews/P0001-T1-r1.md") == "合併 plan/P0001 與 reviews/P0001-T1-r1.md"


def test_testResultsKeepConclusionWithoutCommandOrOutput(tmp_path, writePlan):
  projectDir = makeProject(tmp_path / "proj", writePlan)
  snapshot = exported(projectDir)
  plan = snapshot["plans"]["P0001"]
  assert plan["testBaseline"] == {"commit": baseSha, "passed": False, "failures": ["tests/test_a.py::test_old"],
                                  "qualityChecked": False, "qualityFailures": []}
  restored = stateFromSnapshot(snapshot, loadPlans(projectDir))
  # 沒有指令：新電腦的 conductor 會自己重跑基準，不把別台的結果當本機快取
  assert restored["plans"]["P0001"]["testBaseline"].get("command") is None
  assert restored["tasks"]["P0001/T1"]["lastTest"]["verdict"] == "pass"


def test_planEntryKeepsApprovedTasksInOrderAndFileHash(tmp_path, writePlan):
  projectDir = makeProject(tmp_path / "proj", writePlan)
  plan = exported(projectDir)["plans"]["P0001"]
  assert [task["id"] for task in plan["tasks"]] == ["T1", "T2", "T3"]
  assert plan["tasks"][1]["dependsOn"] == ["T1"]
  assert plan["file"] == "plans/0001-first.md"
  assert len(plan["sha256"]) == 64


def test_planHashIgnoresLineEndings(tmp_path, writePlan):
  first = makeProject(tmp_path / "a", writePlan)
  second = makeProject(tmp_path / "b", writePlan)
  lf = (first / "plans" / "0001-first.md").read_bytes().replace(b"\r\n", b"\n")
  (first / "plans" / "0001-first.md").write_bytes(lf)
  (second / "plans" / "0001-first.md").write_bytes(lf.replace(b"\n", b"\r\n"))
  assert exported(first)["plans"]["P0001"]["sha256"] == exported(second)["plans"]["P0001"]["sha256"]


def test_serializationIsStable(tmp_path, writePlan):
  projectDir = makeProject(tmp_path / "proj", writePlan)
  text = serializeSnapshot(exported(projectDir))
  assert text == serializeSnapshot(parseSnapshot(text.encode("utf-8"))) == serializeSnapshot(parseSnapshot(text))
  assert text.endswith("\n") and "\r" not in text


def test_sameProgressKeepsRevision(tmp_path, writePlan):
  projectDir = makeProject(tmp_path / "proj", writePlan)
  plans, state = projectState(projectDir)
  first = buildSnapshot(state, plans, projectId)
  again = buildSnapshot(state, plans, projectId, previous=first)
  assert again == first
  restored = stateFromSnapshot(first, plans)
  assert buildSnapshot(restored, plans, projectId, previous=first) == first


def test_changedProgressExtendsAncestorChain(tmp_path, writePlan):
  projectDir = makeProject(tmp_path / "proj", writePlan)
  plans, state = projectState(projectDir)
  first = buildSnapshot(state, plans, projectId)
  state["tasks"]["P0001/T3"]["status"] = TaskStatus.done
  second = buildSnapshot(state, plans, projectId, previous=first)
  state["tasks"]["P0001/T1"]["userNote"] = "再補充"
  third = buildSnapshot(state, plans, projectId, previous=second)
  assert first["previousRevision"] is None and first["ancestors"] == []
  assert second["previousRevision"] == first["revision"] and second["ancestors"] == [first["revision"]]
  assert third["ancestors"] == [second["revision"], first["revision"]]
  assert len({first["revision"], second["revision"], third["revision"]}) == 3


def test_revisionDoesNotDependOnLocation(tmp_path, writePlan):
  first = makeProject(tmp_path / "a", writePlan)
  second = makeProject(tmp_path / "b" / "c", writePlan)
  assert exported(first)["revision"] == exported(second)["revision"]


def test_previousFromAnotherProjectIsRejected(tmp_path, writePlan):
  projectDir = makeProject(tmp_path / "proj", writePlan)
  plans, state = projectState(projectDir)
  other = buildSnapshot(state, plans, newProjectId())
  with pytest.raises(SyncError, match="專案識別"):
    buildSnapshot(state, plans, projectId, previous=other)


def test_newProjectIdIsRandomHex():
  first, second = newProjectId(), newProjectId()
  assert first != second and len(first) == 32 and int(first, 16) >= 0


def test_readMissingSnapshotReturnsNone(tmp_path):
  assert readSnapshot(tmp_path) is None


def snapshotBytes(tmp_path, writePlan):
  projectDir = makeProject(tmp_path / "proj", writePlan)
  return json.loads(serializeSnapshot(exported(projectDir)))


def encode(data):
  return json.dumps(data, ensure_ascii=False).encode("utf-8")


def resign(data):
  # 竄改內容後重算 revision，才測得到 revision 以外的驗證
  data["revision"] = board_sync.revisionOf(data)
  return encode(data)


@pytest.mark.parametrize("content, message", [
  (b"", "損毀"),
  (b"{not json", "損毀"),
  (b"\xff\xfe\x00", "損毀"),
  (b"[]", "物件"),
  ("<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> theirs\n".encode("utf-8"), "衝突標記"),
])
def test_unreadableContentIsRejected(content, message):
  with pytest.raises(SyncError, match=message):
    parseSnapshot(content)


def test_unsupportedVersionIsRejected(tmp_path, writePlan):
  data = snapshotBytes(tmp_path, writePlan)
  data["version"] = 2
  with pytest.raises(SyncError, match="版本 2"):
    parseSnapshot(encode(data))
  data["format"] = "something-else"
  with pytest.raises(SyncError, match="不是 agent-board"):
    parseSnapshot(encode(data))


def mutate(data, path, value):
  target = data
  for key in path[:-1]:
    target = target[key]
  if value is KeyError:
    del target[path[-1]]
  else:
    target[path[-1]] = value


@pytest.mark.parametrize("path, value, message", [
  (("projectId",), KeyError, "缺少欄位：projectId"),
  (("plans", "P0001", "tasks", 0, "round"), KeyError, "缺少欄位：round"),
  (("plans", "P0001", "tasks", 0, "sessionId"), "abc", "不在白名單：sessionId"),
  (("plans", "P0001", "status"), "神秘", "plans.P0001.status"),
  (("plans", "P0001", "status"), PlanStatus.draft, "plans.P0001.status"),
  (("plans", "P0001", "tasks", 0, "status"), "亂寫", r"plans.P0001.tasks\[0\].status"),
  (("plans", "P0001", "tasks", 0, "round"), True, r"round"),
  (("plans", "P0001", "tasks", 0, "round"), 0, r"round"),
  (("plans", "P0001", "tasks", 1, "needs", "reason"), "見 C:\\Users\\me\\a.txt", "reason"),
  (("plans", "P0001", "tasks", 0, "conflictFiles"), ["/etc/passwd"], "conflictFiles"),
  (("plans", "P0001", "tasks", 0, "reviews"), ["../secret.md"], "reviews"),
  (("plans", "P0001", "tasks", 0, "dependsOn"), ["T9"], "T9"),
  (("plans", "P0001", "tasks", 1, "needs"), None, "等你處理"),
  (("plans", "P0001", "tasks", 2, "id"), "T1", "重複"),
  (("plans", "P0001", "file"), "C:/x/plans/0001.md", "file"),
  (("plans", "P0001", "baseCommit"), "../../etc", "baseCommit"),
  (("plans", "P0001", "tasks", 0, "text"), 5, "text"),
  (("plans", "P0001", "tasks", 0, "userDone"), "yes", "userDone"),
  (("plans", "P0001", "tasks", 0, "failStreak"), -1, "failStreak"),
  (("plans", "P0001", "tasks", 0, "dependsOn"), "T1", "dependsOn"),
  (("plans", "P0001", "mergeTest"), [], "mergeTest"),
  (("plans",), [], "plans"),
  (("plans", "P0002", "tasks"), [], None),
  (("branches", "main"), "5" * 40, "branches"),
  (("branches", "plan/P0001"), "not a sha", "branches"),
  (("projectId",), "../x", "projectId"),
  (("ancestors",), ["a" * 64], "前序"),
])
def test_invalidFieldsAreRejected(tmp_path, writePlan, path, value, message):
  data = snapshotBytes(tmp_path, writePlan)
  mutate(data, path, value)
  if message is None:
    # 沒有核准過的 plan 本來就沒有任務，這不算錯
    assert parseSnapshot(resign(data))["plans"]["P0002"]["tasks"] == []
    return
  with pytest.raises(SyncError, match=message):
    parseSnapshot(resign(data))


def test_pendingPlanWithTasksIsRejected(tmp_path, writePlan):
  data = snapshotBytes(tmp_path, writePlan)
  data["plans"]["P0002"]["tasks"] = data["plans"]["P0001"]["tasks"][2:]
  with pytest.raises(SyncError, match="還沒核准"):
    parseSnapshot(resign(data))


def test_tamperedContentIsRejected(tmp_path, writePlan):
  data = snapshotBytes(tmp_path, writePlan)
  data["plans"]["P0001"]["tasks"][0]["round"] = 1
  with pytest.raises(SyncError, match="revision"):
    parseSnapshot(encode(data))


def test_brokenAncestorChainIsRejected(tmp_path, writePlan):
  data = snapshotBytes(tmp_path, writePlan)
  data["previousRevision"] = "a" * 64
  with pytest.raises(SyncError, match="前序"):
    parseSnapshot(resign(data))
  data["ancestors"] = ["a" * 64, "a" * 64]
  with pytest.raises(SyncError, match="前序"):
    parseSnapshot(resign(data))


def test_writeRejectsInvalidSnapshotAndKeepsOldFile(tmp_path, writePlan):
  projectDir = makeProject(tmp_path / "proj", writePlan)
  snapshot = exported(projectDir)
  writeSnapshot(projectDir, snapshot)
  before = progressPath(projectDir).read_bytes()
  broken = json.loads(serializeSnapshot(snapshot))
  broken["plans"]["P0001"]["status"] = "亂寫"
  with pytest.raises(SyncError):
    writeSnapshot(projectDir, broken)
  assert progressPath(projectDir).read_bytes() == before
  assert sorted(path.name for path in progressPath(projectDir).parent.iterdir()) == ["progress.json"]


def test_writeReplacesFileAtomically(tmp_path, writePlan, monkeypatch):
  projectDir = makeProject(tmp_path / "proj", writePlan)
  snapshot = exported(projectDir)
  replaced = []
  realReplace = board_sync.os.replace

  def recordReplace(source, target):
    replaced.append((source, target))
    return realReplace(source, target)

  monkeypatch.setattr(board_sync.os, "replace", recordReplace)
  writeSnapshot(projectDir, snapshot)
  source, target = replaced[0]
  assert str(target) == str(progressPath(projectDir)) and str(source).startswith(str(progressPath(projectDir).parent))
  assert readSnapshot(projectDir) == snapshot
  assert progressPath(projectDir).relative_to(projectDir).as_posix() == ".agent-board/progress.json"


def test_writeRetriesWhileFileIsBusy(tmp_path, writePlan, monkeypatch):
  projectDir = makeProject(tmp_path / "proj", writePlan)
  snapshot = exported(projectDir)
  realReplace = board_sync.os.replace
  calls = []

  def busyOnce(source, target):
    calls.append(source)
    if len(calls) == 1:
      raise PermissionError("busy")
    return realReplace(source, target)

  monkeypatch.setattr(board_sync.os, "replace", busyOnce)
  monkeypatch.setattr(board_sync.time, "sleep", lambda seconds: None)
  writeSnapshot(projectDir, snapshot)
  assert len(calls) == 2 and readSnapshot(projectDir) == snapshot


def test_writeGivesUpWhenFileStaysBusy(tmp_path, writePlan, monkeypatch):
  projectDir = makeProject(tmp_path / "proj", writePlan)

  def alwaysBusy(source, target):
    raise PermissionError("busy")

  monkeypatch.setattr(board_sync.os, "replace", alwaysBusy)
  monkeypatch.setattr(board_sync.time, "sleep", lambda seconds: None)
  with pytest.raises(SyncError, match="其他程式"):
    writeSnapshot(projectDir, exported(projectDir))
  assert list(progressPath(projectDir).parent.iterdir()) == []


def test_readReportsCorruptFileWithPath(tmp_path):
  progressPath(tmp_path).parent.mkdir()
  progressPath(tmp_path).write_text("{", encoding="utf-8")
  with pytest.raises(SyncError, match=r"\.agent-board/progress\.json"):
    readSnapshot(tmp_path)


def test_stateFromSnapshotKeepsPlanWithoutFile(tmp_path, writePlan):
  projectDir = makeProject(tmp_path / "proj", writePlan)
  snapshot = exported(projectDir)
  plans = loadPlans(projectDir)
  del plans["P0002"]
  restored = stateFromSnapshot(snapshot, plans)
  assert restored["plans"]["P0002"]["status"] == PlanStatus.rejected
  assert restored["plans"]["P0002"]["title"] == "P0002"
  # plan 檔還沒 pull 下來時沿用上一版記下的檔名和雜湊，不能把它的進度丟掉
  assert buildSnapshot(restored, plans, projectId, previous=snapshot, branches=snapshot["branches"]) is snapshot
  with pytest.raises(SyncError, match="P0002"):
    buildSnapshot(restored, plans, projectId)
