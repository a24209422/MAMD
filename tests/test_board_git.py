import pytest

import board_git


def startPlan(projectDir, planIdValue="P0001"):
  return board_git.ensurePlanWorktree(projectDir, planIdValue, board_git.headCommit(projectDir))


def writeAndCommit(path, fileName, content, message="改動"):
  (path / fileName).write_text(content, encoding="utf-8")
  return board_git.commitAll(path, message)


def test_worktreesLiveBesideProject(gitProject):
  assert board_git.worktreesRoot(gitProject) == gitProject.parent / "proj-worktrees"


def test_changedPathsListsOnlyPlanFilesBetweenCommits(gitProject):
  baseCommit = board_git.headCommit(gitProject)
  (gitProject / "plans").mkdir()
  (gitProject / "plans" / "0001-a.md").write_text("plan a\n", encoding="utf-8")
  (gitProject / "plans" / "0002-說明.md").write_text("plan b\n", encoding="utf-8")
  nextCommit = board_git.commitAll(gitProject, "送出 plan")
  assert sorted(board_git.changedPaths(gitProject, baseCommit, nextCommit)) == [
    "plans/0001-a.md", "plans/0002-說明.md"]


def test_changedPathsOfSameCommitIsEmpty(gitProject):
  commit = board_git.headCommit(gitProject)
  assert board_git.changedPaths(gitProject, commit, commit) == []


def test_changedPathsIncludesChangedCode(gitProject):
  baseCommit = board_git.headCommit(gitProject)
  (gitProject / "plans").mkdir()
  (gitProject / "plans" / "0001-a.md").write_text("plan a\n", encoding="utf-8")
  nextCommit = writeAndCommit(gitProject, "calc.py", "x = 1\n")
  assert sorted(board_git.changedPaths(gitProject, baseCommit, nextCommit)) == ["calc.py", "plans/0001-a.md"]


def test_changedPathsRaisesWhenCommitMissing(gitProject):
  commit = board_git.headCommit(gitProject)
  with pytest.raises(board_git.GitError):
    board_git.changedPaths(gitProject, "0" * 40, commit)
  with pytest.raises(board_git.GitError):
    board_git.changedPaths(gitProject, commit, "不存在的分支")


def test_planAndTaskWorktreesAndCommit(gitProject):
  planPath = startPlan(gitProject)
  assert (planPath / "README.md").exists()
  taskPath = board_git.ensureTaskWorktree(gitProject, "P0001", "T1")
  assert taskPath.name == "P0001-T1"
  (taskPath / "a.txt").write_text("hi\n", encoding="utf-8")
  assert board_git.hasChanges(taskPath) is True
  commit = board_git.commitAll(taskPath, "P0001 T1 第1輪")
  assert len(commit) == 40
  assert board_git.hasChanges(taskPath) is False
  assert "a.txt" in board_git.taskDiff(gitProject, "P0001", "T1")


def test_ensureIsIdempotent(gitProject):
  first = startPlan(gitProject)
  assert startPlan(gitProject) == first
  task = board_git.ensureTaskWorktree(gitProject, "P0001", "T1")
  assert board_git.ensureTaskWorktree(gitProject, "P0001", "T1") == task


def test_mergeTaskSuccess(gitProject):
  planPath = startPlan(gitProject)
  writeAndCommit(board_git.ensureTaskWorktree(gitProject, "P0001", "T1"), "a.txt", "hi\n")
  merged, commit, conflicts = board_git.mergeTask(gitProject, "P0001", "T1", "合併 P0001 T1")
  assert merged is True and commit and conflicts == []
  assert board_git.isMerged(gitProject, "P0001", "T1") is True
  assert (planPath / "a.txt").exists()


def test_mergeConflictAborts(gitProject):
  planPath = startPlan(gitProject)
  writeAndCommit(board_git.ensureTaskWorktree(gitProject, "P0001", "T1"), "shared.txt", "from T1\n")
  writeAndCommit(board_git.ensureTaskWorktree(gitProject, "P0001", "T2"), "shared.txt", "from T2\n")
  assert board_git.mergeTask(gitProject, "P0001", "T1", "合併 T1")[0] is True
  merged, commit, conflicts = board_git.mergeTask(gitProject, "P0001", "T2", "合併 T2")
  assert merged is False and commit is None
  assert conflicts == ["shared.txt"]
  assert board_git.hasChanges(planPath) is False


def test_mergeFailureWithoutConflictRaisesWithGitReason(gitProject):
  planPath = startPlan(gitProject)
  writeAndCommit(board_git.ensureTaskWorktree(gitProject, "P0001", "T1"), "a.txt", "from T1\n")
  # plan worktree 裡有同名的未追蹤檔案，git 會拒絕合併，但不會產生衝突檔
  (planPath / "a.txt").write_text("沒有追蹤的檔案\n", encoding="utf-8")
  with pytest.raises(board_git.GitError, match="a.txt"):
    board_git.mergeTask(gitProject, "P0001", "T1", "合併 T1")
  assert board_git.mergeInProgress(planPath) is False
  assert board_git.isMerged(gitProject, "P0001", "T1") is False
  assert (planPath / "a.txt").read_text(encoding="utf-8") == "沒有追蹤的檔案\n"


def test_cleanWorktreeDiscardsUncommitted(gitProject):
  startPlan(gitProject)
  taskPath = board_git.ensureTaskWorktree(gitProject, "P0001", "T1")
  (taskPath / "README.md").write_text("changed\n", encoding="utf-8")
  (taskPath / "new.txt").write_text("new\n", encoding="utf-8")
  board_git.cleanWorktree(taskPath)
  assert board_git.hasChanges(taskPath) is False
  assert not (taskPath / "new.txt").exists()


def test_taskBranchStartsFromLatestPlan(gitProject):
  startPlan(gitProject)
  writeAndCommit(board_git.ensureTaskWorktree(gitProject, "P0001", "T1"), "a.txt", "hi\n")
  board_git.mergeTask(gitProject, "P0001", "T1", "合併 T1")
  assert (board_git.ensureTaskWorktree(gitProject, "P0001", "T2") / "a.txt").exists()


def test_removePlanWorktreesKeepsPlanBranch(gitProject):
  startPlan(gitProject)
  board_git.ensureTaskWorktree(gitProject, "P0001", "T1")
  removed = board_git.removePlanWorktrees(gitProject, "P0001")
  assert "P0001" in removed and "P0001-T1" in removed and "task/P0001-T1" in removed
  assert not board_git.worktreesRoot(gitProject).joinpath("P0001").exists()
  assert board_git.runGit(gitProject, "branch", "--list", "plan/P0001") != ""


def registeredWorktrees(projectDir):
  return board_git.runGit(projectDir, "worktree", "list", "--porcelain")


def test_baselineWorktreeChecksOutBaseCommitDetachedThenIsRemoved(gitProject):
  baseCommit = board_git.headCommit(gitProject)
  writeAndCommit(startPlan(gitProject), "later.txt", "plan 分支之後的改動\n")
  path = board_git.addBaselineWorktree(gitProject, "P0001", baseCommit)
  assert path == board_git.worktreesRoot(gitProject) / "P0001-baseline"
  assert board_git.headCommit(path) == baseCommit
  assert not (path / "later.txt").exists()
  assert board_git.runGit(path, "branch", "--show-current") == ""
  board_git.removeBaselineWorktree(gitProject, "P0001")
  assert not path.exists()
  assert "P0001-baseline" not in registeredWorktrees(gitProject)
  assert board_git.runGit(gitProject, "branch", "--list", "*baseline*") == ""


def test_addBaselineWorktreeReplacesLeftovers(gitProject):
  baseCommit = board_git.headCommit(gitProject)
  path = board_git.addBaselineWorktree(gitProject, "P0001", baseCommit)
  (path / "產物.txt").write_text("上次測試留下的\n", encoding="utf-8")
  assert board_git.addBaselineWorktree(gitProject, "P0001", baseCommit) == path
  assert not (path / "產物.txt").exists()
  # 資料夾還在、卻已經不是登記中的 worktree 時，git 不肯移除，也要能重建
  board_git.runGit(gitProject, "worktree", "remove", "--force", str(path))
  path.mkdir()
  (path / "stray.txt").write_text("殘留\n", encoding="utf-8")
  board_git.addBaselineWorktree(gitProject, "P0001", baseCommit)
  assert not (path / "stray.txt").exists()
  assert board_git.headCommit(path) == baseCommit
  board_git.removeBaselineWorktree(gitProject, "P0001")
  assert not path.exists()


def test_removeBaselineWorktreeWithoutOneDoesNothing(gitProject):
  board_git.removeBaselineWorktree(gitProject, "P0001")
  assert not board_git.baselineWorktreePath(gitProject, "P0001").exists()


def test_removePlanWorktreesAlsoRemovesLeftoverBaseline(gitProject):
  startPlan(gitProject)
  board_git.addBaselineWorktree(gitProject, "P0001", board_git.headCommit(gitProject))
  removed = board_git.removePlanWorktrees(gitProject, "P0001")
  assert "P0001-baseline" in removed
  assert not board_git.baselineWorktreePath(gitProject, "P0001").exists()
  assert "P0001-baseline" not in registeredWorktrees(gitProject)


def test_cleanWorktreeRefusesMainProject(gitProject):
  (gitProject / "README.md").write_text("changed\n", encoding="utf-8")
  with pytest.raises(board_git.GitError):
    board_git.cleanWorktree(gitProject)
  assert (gitProject / "README.md").read_text(encoding="utf-8") == "changed\n"


def test_mergeInProgressDetectsUnfinishedMerge(gitProject):
  import subprocess
  planPath = startPlan(gitProject)
  writeAndCommit(board_git.ensureTaskWorktree(gitProject, "P0001", "T1"), "shared.txt", "from T1\n")
  writeAndCommit(board_git.ensureTaskWorktree(gitProject, "P0001", "T2"), "shared.txt", "from T2\n")
  board_git.mergeTask(gitProject, "P0001", "T1", "合併 T1")
  assert board_git.mergeInProgress(planPath) is False
  subprocess.run(["git", "merge", "task/P0001-T2"], cwd=planPath, capture_output=True)
  assert board_git.mergeInProgress(planPath) is True


def conflictingTasks(projectDir):
  startPlan(projectDir)
  writeAndCommit(board_git.ensureTaskWorktree(projectDir, "P0001", "T1"), "shared.txt", "from T1\n")
  taskPath = board_git.ensureTaskWorktree(projectDir, "P0001", "T2")
  writeAndCommit(taskPath, "shared.txt", "from T2\n")
  assert board_git.mergeTask(projectDir, "P0001", "T1", "合併 T1")[0] is True
  return taskPath


def test_mergePlanIntoTaskLeavesConflictForResolving(gitProject):
  taskPath = conflictingTasks(gitProject)
  conflicts = board_git.mergePlanIntoTask(gitProject, "P0001", "T2", "合併最新的 plan 分支")
  assert conflicts == ["shared.txt"]
  assert board_git.mergeInProgress(taskPath) is True
  assert board_git.filesWithConflictMarkers(taskPath, conflicts) == ["shared.txt"]
  (taskPath / "shared.txt").write_text("from T1\nfrom T2\n", encoding="utf-8")
  assert board_git.filesWithConflictMarkers(taskPath, conflicts) == []
  board_git.commitAll(taskPath, "解衝突")
  assert board_git.mergeInProgress(taskPath) is False
  # 任務分支已經包含最新的 plan 分支，再合併回去不會衝突，diff 也只剩這個任務自己的改動
  assert "from T2" in board_git.taskDiff(gitProject, "P0001", "T2")
  assert board_git.mergeTask(gitProject, "P0001", "T2", "合併 T2")[0] is True
  planPath = board_git.planWorktreePath(gitProject, "P0001")
  assert (planPath / "shared.txt").read_text(encoding="utf-8") == "from T1\nfrom T2\n"


def test_mergePlanIntoTaskWithoutConflictCommits(gitProject):
  startPlan(gitProject)
  writeAndCommit(board_git.ensureTaskWorktree(gitProject, "P0001", "T1"), "a.txt", "from T1\n")
  taskPath = board_git.ensureTaskWorktree(gitProject, "P0001", "T2")
  writeAndCommit(taskPath, "b.txt", "from T2\n")
  board_git.mergeTask(gitProject, "P0001", "T1", "合併 T1")
  assert board_git.mergePlanIntoTask(gitProject, "P0001", "T2", "合併最新的 plan 分支") == []
  assert board_git.mergeInProgress(taskPath) is False
  assert board_git.hasChanges(taskPath) is False
  assert (taskPath / "a.txt").exists()


def test_filesWithConflictMarkersIgnoresPlainSeparatorsAndMissingFiles(tmp_path):
  (tmp_path / "README.md").write_text("標題\n=======\n內容\n", encoding="utf-8")
  (tmp_path / "half.txt").write_text("a\n>>>>>>> task/P0001-T1\n", encoding="utf-8")
  assert board_git.filesWithConflictMarkers(tmp_path, ["README.md", "half.txt", "gone.txt"]) == ["half.txt"]


def test_mergePlanIntoTaskConcludesMergeWhenConflictsAlreadyStaged(gitProject):
  taskPath = conflictingTasks(gitProject)
  board_git.mergePlanIntoTask(gitProject, "P0001", "T2", "合併最新的 plan 分支")
  (taskPath / "shared.txt").write_text("from T1\nfrom T2\n", encoding="utf-8")
  board_git.runGit(taskPath, "add", "shared.txt")
  assert board_git.mergePlanIntoTask(gitProject, "P0001", "T2", "合併最新的 plan 分支") == []
  assert board_git.mergeInProgress(taskPath) is False
  assert board_git.hasChanges(taskPath) is False


def test_conflictFileNamesStayReadableWithChinese(gitProject):
  # git 預設會把非 ASCII 檔名輸出成跳脫字元，拿去找檔案會找不到，標記檢查就會失效
  startPlan(gitProject)
  writeAndCommit(board_git.ensureTaskWorktree(gitProject, "P0001", "T1"), "說明.txt", "from T1\n")
  taskPath = board_git.ensureTaskWorktree(gitProject, "P0001", "T2")
  writeAndCommit(taskPath, "說明.txt", "from T2\n")
  board_git.mergeTask(gitProject, "P0001", "T1", "合併 T1")
  assert board_git.mergeTask(gitProject, "P0001", "T2", "合併 T2")[2] == ["說明.txt"]
  conflicts = board_git.mergePlanIntoTask(gitProject, "P0001", "T2", "合併最新的 plan 分支")
  assert conflicts == ["說明.txt"]
  assert board_git.filesWithConflictMarkers(taskPath, conflicts) == ["說明.txt"]


def test_mergePlanIntoTaskKeepsStagedMarkersAsConflicts(gitProject):
  # git add 過不代表標記已經清掉；還有標記就要交回去解，不能直接 commit
  taskPath = conflictingTasks(gitProject)
  board_git.mergePlanIntoTask(gitProject, "P0001", "T2", "合併最新的 plan 分支")
  board_git.runGit(taskPath, "add", "-A")
  assert board_git.mergePlanIntoTask(gitProject, "P0001", "T2", "合併最新的 plan 分支") == ["shared.txt"]
  assert board_git.mergeInProgress(taskPath) is True


def test_mergeTestWorktreeIsDetachedAtCommitAndReplacedAndRemovedWithPlan(gitProject):
  planPath = startPlan(gitProject)
  firstMerge = writeAndCommit(planPath, "a.txt", "第一次合併\n")
  writeAndCommit(planPath, "b.txt", "之後的合併\n")
  path = board_git.addMergeTestWorktree(gitProject, "P0001", firstMerge)
  assert path == board_git.worktreesRoot(gitProject) / "P0001-mergetest"
  assert board_git.headCommit(path) == firstMerge
  assert (path / "a.txt").exists() and not (path / "b.txt").exists()
  assert board_git.runGit(path, "branch", "--show-current") == ""
  (path / "產物.txt").write_text("上次測試留下的\n", encoding="utf-8")
  assert board_git.addMergeTestWorktree(gitProject, "P0001", firstMerge) == path
  assert not (path / "產物.txt").exists()
  removed = board_git.removePlanWorktrees(gitProject, "P0001")
  assert "P0001-mergetest" in removed
  assert not path.exists()
  assert "P0001-mergetest" not in registeredWorktrees(gitProject)


def test_removeMergeTestWorktree(gitProject):
  path = board_git.addMergeTestWorktree(gitProject, "P0001", board_git.headCommit(gitProject))
  board_git.removeMergeTestWorktree(gitProject, "P0001")
  assert not path.exists()
  board_git.removeMergeTestWorktree(gitProject, "P0001")


def test_firstParentCommitsListsEachMergeInOrderWithoutTaskCommits(gitProject):
  startPlan(gitProject)
  baseCommit = board_git.headCommit(gitProject)
  firstTask = writeAndCommit(board_git.ensureTaskWorktree(gitProject, "P0001", "T1"), "a.txt", "T1\n")
  secondTask = writeAndCommit(board_git.ensureTaskWorktree(gitProject, "P0001", "T2"), "b.txt", "T2\n")
  firstMerge = board_git.mergeTask(gitProject, "P0001", "T1", "合併 T1")[1]
  secondMerge = board_git.mergeTask(gitProject, "P0001", "T2", "合併 T2")[1]
  commits = board_git.firstParentCommits(gitProject, "P0001")
  assert commits[-3:] == [baseCommit, firstMerge, secondMerge]
  assert firstTask not in commits and secondTask not in commits


def test_fastForwardTaskToPlanAfterMergeAndRefusesDivergedBranch(gitProject):
  startPlan(gitProject)
  writeAndCommit(board_git.ensureTaskWorktree(gitProject, "P0001", "T1"), "a.txt", "T1\n")
  writeAndCommit(board_git.ensureTaskWorktree(gitProject, "P0001", "T2"), "b.txt", "T2\n")
  board_git.mergeTask(gitProject, "P0001", "T1", "合併 T1")
  planHead = board_git.mergeTask(gitProject, "P0001", "T2", "合併 T2")[1]
  taskPath = board_git.taskWorktreePath(gitProject, "P0001", "T1")
  board_git.fastForwardTaskToPlan(gitProject, "P0001", "T1")
  assert board_git.headCommit(taskPath) == planHead
  assert (taskPath / "b.txt").exists()
  assert board_git.taskDiff(gitProject, "P0001", "T1") == ""
  writeAndCommit(taskPath, "a.txt", "修正\n")
  writeAndCommit(board_git.planWorktreePath(gitProject, "P0001"), "c.txt", "plan 又往前\n")
  with pytest.raises(board_git.GitError):
    board_git.fastForwardTaskToPlan(gitProject, "P0001", "T1")
