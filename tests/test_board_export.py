import os
import shutil
import subprocess

import pytest

import board
import board_git
from board_core import PlanStatus, deriveState, loadConfig, loadPlans, readEvents
from board_sync import checkpointPath, loadProgress, progressPath, readSnapshot
from conftest import git
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
expectedFiles = [".agent-board/progress.json", "plans/0001-first.md", "plans/0002-second.md", "plans/0004-done.md"]


def revParse(projectDir, branch):
  return board_git.runGit(projectDir, "rev-parse", f"refs/heads/{branch}")


def commitIn(path, fileName, text):
  (path / fileName).write_text(text, encoding="utf-8")
  git(path, "add", fileName)
  git(path, "commit", "-q", "-m", f"改 {fileName}")


@pytest.fixture
def legacyProject(gitProject, writePlan):
  # 升級前的舊專案：只有 .board 事件紀錄，沒有交接檔；有完成、進行中、退回與草稿的 plan
  writePlan("0001-first.md", rows, planId="P0001", projectDir=gitProject)
  writePlan("0002-second.md", rows[:1], planId="P0002", projectDir=gitProject)
  writePlan("0003-draft.md", rows[:1], planId="P0003", projectDir=gitProject)
  writePlan("0004-done.md", rows[:1], planId="P0004", projectDir=gitProject)
  writeEvents(gitProject, projectEvents(gitProject) + doneEvents)
  git(gitProject, "branch", "plan/P0001")
  git(gitProject, "worktree", "add", "-q", str(board_git.planWorktreePath(gitProject, "P0001")), "plan/P0001")
  taskPath = board_git.taskWorktreePath(gitProject, "P0001", "T1")
  git(gitProject, "worktree", "add", "-q", "-b", "task/P0001-T1", str(taskPath), "plan/P0001")
  commitIn(taskPath, "t1.txt", "T1 寫到一半先 commit\n")
  git(gitProject, "branch", "task/P0001-T3", "plan/P0001")
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


def test_exportListsExactCommitPathsAndBranches(legacyProject, capsys):
  branches = {branch: revParse(legacyProject, branch) for branch in ("plan/P0001", "task/P0001-T1", "task/P0001-T3")}

  assert exportState(legacyProject) == 0

  output = capsys.readouterr().out
  assert section(output, "請 commit 這些檔案：") == expectedFiles
  assert section(output, "請 push 這些分支：") == [f"{branch} {sha}" for branch, sha in branches.items()]
  assert readSnapshot(legacyProject)["branches"] == branches
  assert "P0003" not in readSnapshot(legacyProject)["plans"]


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


@pytest.mark.parametrize("breakSource, keyword", [
  (holdAlfredLock, "Alfred"),
  (leaveHalfWork, "P0001-T1"),
  (appendBrokenEvent, "殘缺"),
  (leaveMergeConflict, "衝突"),
  (conflictProgressFile, "衝突"),
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


def test_statusWithoutPlansHasNoHint(gitProject, capsys):
  assert board.cmdStatus(gitProject) == 0

  assert "export-state" not in capsys.readouterr().out
