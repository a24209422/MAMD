import json
import os
import shutil
import subprocess

import pytest

import board
import board_git
from board_core import PlanStatus, TaskStatus, deriveState, loadConfig, loadPlans, readEvents
from board_sync import checkpointPath, loadProgress, progressPath, readSnapshot
from conftest import git, planText
from helpers import makeEvent, testConfig
from test_board_sync import baseSha, portableView, projectEvents, rows
from test_board_sync_load import started, writeEvents

doneEvents = [
  makeEvent("planSubmitted", plan="P0004"),
  makeEvent("planApproved", plan="P0004", baseCommit=baseSha, tasks=[
    {"id": "T1", "text": "已完成的任務", "dependsOn": [], "requirementIds": ["R1"]}]),
  makeEvent("userDecision", plan="P0004", task="T1", action="done", note=""),
  makeEvent("planCompleted", plan="P0004"),
]
expectedFiles = [".agent-board/progress.json", "plans/0001-first.md", "plans/0002-second.md", "plans/0004-done.md",
                 "plans/0005-old-merge.md", "plans/0006-unmerged.md"]
expectedBranches = ("plan/P0001", "plan/P0005", "plan/P0006", "task/P0001-T1", "task/P0001-T3")


def revParse(projectDir, branch):
  return board_git.runGit(projectDir, "rev-parse", f"refs/heads/{branch}")


def commitIn(path, fileName, text):
  (path / fileName).write_text(text, encoding="utf-8")
  git(path, "add", fileName)
  git(path, "commit", "-q", "-m", f"改 {fileName}")


def mergeTaskInMain(projectDir, planIdValue, fileName):
  # 用真的 commit 與 --no-ff 合併做出已合併的任務；回傳任務成果和合併 commit
  planBranch, taskBranch = f"plan/{planIdValue}", f"task/{planIdValue}-T1"
  git(projectDir, "branch", planBranch, "main")
  git(projectDir, "checkout", "-q", "-b", taskBranch, planBranch)
  commitIn(projectDir, fileName, f"{planIdValue} 的成果\n")
  git(projectDir, "checkout", "-q", planBranch)
  git(projectDir, "merge", "-q", "--no-ff", "-m", f"合併 {taskBranch}", taskBranch)
  git(projectDir, "checkout", "-q", "main")
  return revParse(projectDir, taskBranch), revParse(projectDir, planBranch)


def oldMergeEvents(planIdValue, taskCommit, mergeCommit):
  # 舊流程的 taskMerged 沒帶 verify：任務直接完成，不記 mergedCommit
  return [
    makeEvent("planSubmitted", plan=planIdValue),
    makeEvent("planApproved", plan=planIdValue, baseCommit=baseSha, tasks=[
      {"id": "T1", "text": "舊流程合併的任務", "dependsOn": [], "requirementIds": ["R1"]}]),
    makeEvent("taskStarted", plan=planIdValue, task="T1", roundNumber=1, agent="claude"),
    makeEvent("taskWritten", plan=planIdValue, task="T1", commit=taskCommit, summary="完成"),
    makeEvent("reviewStarted", plan=planIdValue, task="T1", roundNumber=1, agent="codex"),
    makeEvent("reviewPassed", plan=planIdValue, task="T1", roundNumber=1, notes=f"reviews/{planIdValue}-T1-r1.md"),
    makeEvent("taskMerged", plan=planIdValue, task="T1", commit=mergeCommit),
  ]


def withRealCommits(events, commits):
  # 重複字元的假 SHA 不在任何分支上，驗不出分支是否真的帶著記錄的成果
  text = json.dumps(events, ensure_ascii=False)
  for fake, real in commits.items():
    text = text.replace(fake, real)
  return json.loads(text)


@pytest.fixture
def legacyProject(gitProject, writePlan):
  # 升級前的舊專案：只有 .board 事件紀錄，沒有交接檔；有完成、進行中、退回與草稿的 plan
  writePlan("0001-first.md", rows, planId="P0001", projectDir=gitProject)
  writePlan("0002-second.md", rows[:1], planId="P0002", projectDir=gitProject)
  writePlan("0003-draft.md", rows[:1], planId="P0003", projectDir=gitProject)
  writePlan("0004-done.md", rows[:1], planId="P0004", projectDir=gitProject)
  # P0005 進行中、任務已用舊流程合併且 task 分支已刪；P0006 已完成但還沒合併進主分支
  writePlan("0005-old-merge.md", rows[:1], planId="P0005", projectDir=gitProject)
  writePlan("0006-unmerged.md", rows[:1], planId="P0006", projectDir=gitProject)
  oldMerged = mergeTaskInMain(gitProject, "P0005", "t5.txt")
  git(gitProject, "branch", "-D", "task/P0005-T1")
  unmerged = mergeTaskInMain(gitProject, "P0006", "t6.txt")
  git(gitProject, "branch", "plan/P0001")
  planPath = board_git.planWorktreePath(gitProject, "P0001")
  git(gitProject, "worktree", "add", "-q", str(planPath), "plan/P0001")
  taskPath = board_git.taskWorktreePath(gitProject, "P0001", "T1")
  git(gitProject, "worktree", "add", "-q", "-b", "task/P0001-T1", str(taskPath), "plan/P0001")
  commitIn(taskPath, "t1.txt", "T1 寫到一半先 commit\n")
  git(planPath, "checkout", "-q", "-b", "task/P0001-T3")
  commitIn(planPath, "t3.txt", "T3 的成果\n")
  git(planPath, "checkout", "-q", "plan/P0001")
  git(planPath, "merge", "-q", "--no-ff", "-m", "合併 T3", "task/P0001-T3")
  commits = {"2" * 40: revParse(gitProject, "task/P0001-T1"), "3" * 40: revParse(gitProject, "task/P0001-T3"),
             "4" * 40: revParse(gitProject, "plan/P0001")}
  writeEvents(gitProject, withRealCommits(projectEvents(gitProject) + doneEvents, commits)
              + oldMergeEvents("P0005", *oldMerged) + oldMergeEvents("P0006", *unmerged)
              + [makeEvent("planCompleted", plan="P0006")])
  git(gitProject, "branch", "task/P0004-T1")
  return gitProject


def exportState(projectDir):
  return board.main(["--project", str(projectDir), "export-state"])


def section(output, header):
  lines = output.splitlines()
  start = lines.index(header) + 1
  listed = []
  for line in lines[start:]:
    if not line.startswith("  "):
      break
    listed.append(line.strip())
  return listed


def sourceView(projectDir):
  plans = loadPlans(projectDir)
  return portableView(deriveState(readEvents(projectDir), plans, loadConfig(projectDir)))


def test_legacyProjectExportRestoresInBlankEnvironment(legacyProject, tmp_path, capsys):
  assert exportState(legacyProject) == 0
  listed = section(capsys.readouterr().out, "請 commit 這些檔案：")
  blank = tmp_path / "blank"
  for relativePath in listed:
    (blank / relativePath).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(legacyProject / relativePath, blank / relativePath)

  loaded = loadProgress(blank, testConfig)

  assert loaded["sync"]["status"] == "accepted"
  assert portableView(loaded["state"]) == sourceView(legacyProject)
  assert loaded["state"]["plans"]["P0004"]["status"] == PlanStatus.completed
  assert loaded["state"]["plans"]["P0001"]["status"] == PlanStatus.active
  # 新環境還沒建出本機分支，不能誤報成有未匯出的進度
  capsys.readouterr()
  assert board.cmdStatus(blank) == 0
  assert "export-state" not in capsys.readouterr().out


def test_exportListsExactCommitPathsAndBranches(legacyProject, capsys):
  branches = {branch: revParse(legacyProject, branch) for branch in expectedBranches}

  assert exportState(legacyProject) == 0

  output = capsys.readouterr().out
  assert section(output, "請 commit 這些檔案：") == expectedFiles
  assert section(output, "請 push 這些分支：") == [f"{branch} {sha}" for branch, sha in branches.items()]
  assert readSnapshot(legacyProject)["branches"] == branches
  assert "P0003" not in readSnapshot(legacyProject)["plans"]


def test_fixtureCoversOldAndNewMergeStates(legacyProject):
  state = deriveState(readEvents(legacyProject), loadPlans(legacyProject), testConfig)
  tasks, plans = state["tasks"], state["plans"]

  assert tasks["P0001/T3"]["mergedCommit"] == revParse(legacyProject, "plan/P0001")
  assert (tasks["P0005/T1"]["status"], tasks["P0005/T1"]["mergedCommit"]) == (TaskStatus.done, None)
  assert not board_git.branchExists(legacyProject, "task/P0005-T1")
  assert (plans["P0005"]["status"], plans["P0006"]["status"]) == (PlanStatus.active, PlanStatus.completed)


def recordedResults(snapshot):
  # 交接檔記下的成果：沒放棄的任務都要拿得到；plan 被放棄前已完成的任務也還保有成果
  return {commit for plan in snapshot["plans"].values()
          for task in plan["tasks"] if task["status"] != TaskStatus.abandoned
          for commit in (task["lastCommit"], task["mergedCommit"]) if commit}


def pushListed(projectDir, output, remote):
  # 照輸出實際 push：交接檔所在的主分支加上列出的分支，送到空的 bare repo
  git(remote.parent, "init", "-q", "--bare", remote.name)
  refs = ["HEAD:refs/heads/main"] + [f"refs/heads/{line.split()[0]}" for line in section(output, "請 push 這些分支：")]
  git(projectDir, "push", "-q", str(remote), *refs)


def missingResults(remote, commits):
  return sorted(commit for commit in commits if subprocess.run(
    ["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=remote, capture_output=True).returncode != 0)


def test_pushingListedBranchesDeliversEveryRecordedResult(legacyProject, tmp_path, capsys):
  assert exportState(legacyProject) == 0
  remote = tmp_path / "remote.git"

  pushListed(legacyProject, capsys.readouterr().out, remote)

  results = recordedResults(readSnapshot(legacyProject))
  assert len(results) == 5
  assert missingResults(remote, results) == []


def test_completedPlanMergedIntoMainNeedsNoBranches(legacyProject, tmp_path, capsys):
  git(legacyProject, "merge", "-q", "--no-ff", "-m", "合併 P0006", "plan/P0006")
  # 合併進主分支並清理後，task 與 plan 分支都不在了，成果靠主分支交付
  git(legacyProject, "branch", "-D", "task/P0006-T1")
  git(legacyProject, "branch", "-D", "plan/P0006")

  assert exportState(legacyProject) == 0

  output = capsys.readouterr().out
  assert "plan/P0006" not in output
  remote = tmp_path / "remote.git"
  pushListed(legacyProject, output, remote)
  assert missingResults(remote, recordedResults(readSnapshot(legacyProject))) == []


def supersedeUnmergedPlan(projectDir):
  # 新 plan 取代已完成但沒合併的 P0006，P0006 的完成任務仍留在交接檔裡
  (projectDir / "plans" / "0007-replace.md").write_text(
    planText("P0007", "取代 P0006", rows[:1], "supersedes: P0006\n"), encoding="utf-8")


def test_supersededPlanKeepsUnmergedResultsDeliverable(legacyProject, tmp_path, capsys):
  supersedeUnmergedPlan(legacyProject)
  git(legacyProject, "branch", "-D", "task/P0006-T1")

  assert exportState(legacyProject) == 0

  output = capsys.readouterr().out
  snapshot = readSnapshot(legacyProject)
  assert snapshot["plans"]["P0006"]["status"] == PlanStatus.superseded
  assert f"plan/P0006 {revParse(legacyProject, 'plan/P0006')}" in section(output, "請 push 這些分支：")
  remote = tmp_path / "remote.git"
  pushListed(legacyProject, output, remote)
  results = recordedResults(snapshot)
  assert {task["lastCommit"] for task in snapshot["plans"]["P0006"]["tasks"]} <= results
  assert missingResults(remote, results) == []


def abandonOldMergedPlan(projectDir):
  # P0005 的 T1 已完成並合併進 plan 分支，之後才放棄整份 plan；完成的成果還留在交接檔裡
  writeEvents(projectDir, [makeEvent("planAbandoned", plan="P0005")])


def test_abandonedPlanKeepsCompletedResultsDeliverable(legacyProject, tmp_path, capsys):
  abandonOldMergedPlan(legacyProject)

  assert exportState(legacyProject) == 0

  output = capsys.readouterr().out
  snapshot = readSnapshot(legacyProject)
  assert snapshot["plans"]["P0005"]["status"] == PlanStatus.abandoned
  assert snapshot["plans"]["P0005"]["tasks"][0]["status"] == TaskStatus.done
  assert f"plan/P0005 {revParse(legacyProject, 'plan/P0005')}" in section(output, "請 push 這些分支：")
  remote = tmp_path / "remote.git"
  pushListed(legacyProject, output, remote)
  results = recordedResults(snapshot)
  assert {task["lastCommit"] for task in snapshot["plans"]["P0005"]["tasks"]} <= results
  assert missingResults(remote, results) == []


def deletePlanFile(path):
  path.unlink()


def corruptPlanFile(path):
  path.write_bytes(b"\xff\xfe\x00 not utf-8")


@pytest.mark.parametrize("damage", [deletePlanFile, corruptPlanFile])
def test_firstExportRejectsEventsOfUnloadablePlan(legacyProject, capsys, damage):
  # 舊專案的 plan 檔遺失或讀不到時，重播會整份略過它的事件；照樣匯出就把這段歷史永久丟掉
  path = legacyProject / "plans" / "0005-old-merge.md"
  original = path.read_bytes()
  damage(path)

  assert exportState(legacyProject) == 1

  assert "P0005" in capsys.readouterr().out
  assert not progressPath(legacyProject).exists()
  assert not checkpointPath(legacyProject).exists()
  path.write_bytes(original)
  assert exportState(legacyProject) == 0
  assert readSnapshot(legacyProject)["plans"]["P0005"]["tasks"][0]["status"] == TaskStatus.done


def test_statusHintsCompletedPlanMergedAfterExport(legacyProject, capsys):
  assert exportState(legacyProject) == 0
  git(legacyProject, "merge", "-q", "--no-ff", "-m", "合併 P0006", "plan/P0006")
  capsys.readouterr()

  # 沒有新事件，但再匯出時不必再 push plan/P0006，交接內容已經不同
  assert board.cmdStatus(legacyProject) == 0
  assert "export-state" in capsys.readouterr().out

  assert exportState(legacyProject) == 0
  assert "plan/P0006" not in readSnapshot(legacyProject)["branches"]
  capsys.readouterr()
  assert board.cmdStatus(legacyProject) == 0
  assert "export-state" not in capsys.readouterr().out


def test_exportDoesNotCommitOrChangeTaskStatus(legacyProject):
  before = (board_git.headCommit(legacyProject), sourceView(legacyProject), len(readEvents(legacyProject)))

  assert exportState(legacyProject) == 0

  assert (board_git.headCommit(legacyProject), sourceView(legacyProject), len(readEvents(legacyProject))) == before
  assert "progress.json" in board_git.runGit(legacyProject, "status", "--porcelain", "--untracked-files=all")


def test_reexportingSameProgressKeepsRevision(legacyProject, capsys):
  assert exportState(legacyProject) == 0
  first = progressPath(legacyProject).read_bytes()
  capsys.readouterr()

  assert exportState(legacyProject) == 0

  assert progressPath(legacyProject).read_bytes() == first
  assert "沒有新進度" in capsys.readouterr().out


def test_newProgressAfterExportChainsNextRevision(legacyProject):
  assert exportState(legacyProject) == 0
  first = readSnapshot(legacyProject)
  writeEvents(legacyProject, [started("T1", roundNumber=2)])

  assert loadProgress(legacyProject, testConfig)["sync"]["status"] == "same"
  assert exportState(legacyProject) == 0

  second = readSnapshot(legacyProject)
  assert second["previousRevision"] == first["revision"]
  assert second["projectId"] == first["projectId"]


def holdAlfredLock(projectDir):
  (projectDir / ".board" / "conductor.lock").write_text(str(os.getpid()), encoding="utf-8")


def leaveHalfWork(projectDir):
  (board_git.taskWorktreePath(projectDir, "P0001", "T1") / "half.txt").write_text("寫到一半\n", encoding="utf-8")


def appendBrokenEvent(projectDir):
  with open(projectDir / ".board" / "events.jsonl", "a", encoding="utf-8", newline="\n") as handle:
    handle.write('{"time": "2026-10-25T10:00:00+08:00", "plan": "P0001", "ty\n')


def leaveMergeConflict(projectDir):
  planPath = board_git.planWorktreePath(projectDir, "P0001")
  commitIn(planPath, "t1.txt", "plan 這邊的內容\n")
  subprocess.run(["git", "merge", "task/P0001-T1"], cwd=planPath, capture_output=True)
  assert board_git.mergeInProgress(planPath)


def conflictProgressFile(projectDir):
  path = progressPath(projectDir)
  path.write_text("<<<<<<< HEAD\n" + path.read_text(encoding="utf-8") + "=======\n{}\n>>>>>>> other\n",
                  encoding="utf-8")


def leaveMainProjectConflict(projectDir):
  # 交接檔本身沒有衝突標記，但主專案還停在合併衝突中
  git(projectDir, "checkout", "-q", "-b", "side")
  commitIn(projectDir, "README.md", "side 的內容\n")
  git(projectDir, "checkout", "-q", "main")
  commitIn(projectDir, "README.md", "main 的內容\n")
  subprocess.run(["git", "merge", "side"], cwd=projectDir, capture_output=True)
  assert board_git.mergeInProgress(projectDir)


def changeApprovedPlan(projectDir):
  path = projectDir / "plans" / "0001-first.md"
  path.write_text(path.read_text(encoding="utf-8") + "\n核准後偷改的內容\n", encoding="utf-8")


def dropBranchWithWork(projectDir):
  # T3 已有成果（lastCommit），分支不見了就沒有東西能把成果帶到另一台電腦
  git(projectDir, "branch", "-D", "task/P0001-T3")


def rewindTaskBranch(projectDir):
  # 分支還在但退回到成果之前，照清單 push 過去也拿不到任務成果
  git(projectDir, "branch", "-f", "task/P0001-T3", "task/P0001-T3~1")


def rewindPlanBranch(projectDir):
  git(board_git.planWorktreePath(projectDir, "P0001"), "reset", "-q", "--hard", "HEAD~1")


def rewindOldMergedPlanBranch(projectDir):
  # 舊流程完成的任務沒有 mergedCommit，只能靠 lastCommit 發現 plan 分支丟了成果
  git(projectDir, "branch", "-f", "plan/P0005", "plan/P0005~1")


def dropUnmergedCompletedPlan(projectDir):
  # 已完成但沒合併進主分支，plan 分支是成果唯一的來源
  git(projectDir, "branch", "-D", "task/P0006-T1")
  git(projectDir, "branch", "-D", "plan/P0006")


def rewindUnmergedCompletedPlan(projectDir):
  git(projectDir, "branch", "-D", "task/P0006-T1")
  git(projectDir, "branch", "-f", "plan/P0006", "plan/P0006~1")


def dropSupersededUnmergedPlan(projectDir):
  supersedeUnmergedPlan(projectDir)
  dropUnmergedCompletedPlan(projectDir)


def dropAbandonedPlanWithResults(projectDir):
  abandonOldMergedPlan(projectDir)
  git(projectDir, "branch", "-D", "plan/P0005")


def addPlanWorktree(projectDir, planIdValue):
  path = board_git.planWorktreePath(projectDir, planIdValue)
  git(projectDir, "worktree", "add", "-q", str(path), f"plan/{planIdValue}")
  return path


def leaveHalfWorkInUnmergedPlan(projectDir):
  # 完成但沒合併的 plan 分支要交付，它的 worktree 有半成品時 push 出去的分支不是完整成果
  (addPlanWorktree(projectDir, "P0006") / "half.txt").write_text("寫到一半\n", encoding="utf-8")


def leaveConflictInSupersededPlan(projectDir):
  supersedeUnmergedPlan(projectDir)
  path = addPlanWorktree(projectDir, "P0006")
  git(path, "checkout", "-q", "-b", "side-P0006")
  commitIn(path, "t6.txt", "side 的內容\n")
  git(path, "checkout", "-q", "plan/P0006")
  commitIn(path, "t6.txt", "plan 的內容\n")
  subprocess.run(["git", "merge", "side-P0006"], cwd=path, capture_output=True)
  assert board_git.mergeInProgress(path)


def leaveHalfWorkInAbandonedPlan(projectDir):
  abandonOldMergedPlan(projectDir)
  (addPlanWorktree(projectDir, "P0005") / "half.txt").write_text("寫到一半\n", encoding="utf-8")


def submitUnloadablePlan(projectDir):
  (projectDir / "plans" / "0008-broken.md").write_bytes(b"\xff\xfe\x00 not utf-8")
  writeEvents(projectDir, [makeEvent("planSubmitted", plan="P0008")])


@pytest.mark.parametrize("breakSource, keyword", [
  (holdAlfredLock, "Alfred"),
  (leaveHalfWork, "P0001-T1"),
  (appendBrokenEvent, "殘缺"),
  (leaveMergeConflict, "衝突"),
  (conflictProgressFile, "衝突"),
  (leaveMainProjectConflict, "主專案"),
  (changeApprovedPlan, "plans/0001-first.md"),
  (dropBranchWithWork, "task/P0001-T3"),
  (rewindTaskBranch, "task/P0001-T3"),
  (rewindPlanBranch, "plan/P0001"),
  (rewindOldMergedPlanBranch, "plan/P0005"),
  (dropUnmergedCompletedPlan, "plan/P0006"),
  (rewindUnmergedCompletedPlan, "plan/P0006"),
  (dropSupersededUnmergedPlan, "plan/P0006"),
  (dropAbandonedPlanWithResults, "plan/P0005"),
  (leaveHalfWorkInUnmergedPlan, "半成品"),
  (leaveConflictInSupersededPlan, "衝突"),
  (leaveHalfWorkInAbandonedPlan, "半成品"),
  (submitUnloadablePlan, "P0008"),
])
def test_failedChecksKeepExistingHandoffFile(legacyProject, capsys, breakSource, keyword):
  assert exportState(legacyProject) == 0
  writeEvents(legacyProject, [started("T1", roundNumber=2)])
  breakSource(legacyProject)
  before = (progressPath(legacyProject).read_bytes(), checkpointPath(legacyProject).read_bytes())
  capsys.readouterr()

  assert exportState(legacyProject) == 1

  assert (progressPath(legacyProject).read_bytes(), checkpointPath(legacyProject).read_bytes()) == before
  output = capsys.readouterr().out
  assert keyword in output
  assert "請 commit" not in output


def test_firstExportFailureWritesNothing(legacyProject):
  holdAlfredLock(legacyProject)

  assert exportState(legacyProject) == 1

  assert not progressPath(legacyProject).exists()
  assert not checkpointPath(legacyProject).exists()


def test_exportFromTaskWorktreeIsRejected(legacyProject, capsys):
  assert exportState(legacyProject) == 0
  before = (progressPath(legacyProject).read_bytes(), checkpointPath(legacyProject).read_bytes())
  taskPath = board_git.taskWorktreePath(legacyProject, "P0001", "T1")
  capsys.readouterr()

  assert exportState(taskPath) == 1

  assert "主專案" in capsys.readouterr().out
  assert not progressPath(taskPath).exists()
  assert not (taskPath / ".board").exists()
  assert (progressPath(legacyProject).read_bytes(), checkpointPath(legacyProject).read_bytes()) == before


def test_statusHintsUnexportedProgress(legacyProject, capsys):
  assert board.cmdStatus(legacyProject) == 0
  assert "export-state" in capsys.readouterr().out

  assert exportState(legacyProject) == 0
  capsys.readouterr()
  assert board.cmdStatus(legacyProject) == 0
  assert "export-state" not in capsys.readouterr().out

  writeEvents(legacyProject, [started("T1", roundNumber=2)])
  assert board.cmdStatus(legacyProject) == 0
  assert "export-state" in capsys.readouterr().out


def test_statusHintsBranchCommitWithoutNewEvent(legacyProject, capsys):
  assert exportState(legacyProject) == 0
  first = readSnapshot(legacyProject)
  # 使用者自己在任務分支補 commit，沒有新事件，但交接檔記的分支 SHA 已經過時
  commitIn(board_git.taskWorktreePath(legacyProject, "P0001", "T1"), "t1.txt", "補上的修改\n")
  capsys.readouterr()

  assert board.cmdStatus(legacyProject) == 0
  assert "export-state" in capsys.readouterr().out

  assert exportState(legacyProject) == 0
  assert readSnapshot(legacyProject)["previousRevision"] == first["revision"]
  capsys.readouterr()
  assert board.cmdStatus(legacyProject) == 0
  assert "export-state" not in capsys.readouterr().out


def test_statusWithoutPlansHasNoHint(gitProject, capsys):
  assert board.cmdStatus(gitProject) == 0

  assert "export-state" not in capsys.readouterr().out
