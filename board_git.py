"""plan／任務分支與 worktree 的 git 操作。"""
import re
import shutil
import subprocess
from pathlib import Path


class GitError(Exception):
  pass


def runGit(cwd, *args, check=True):
  completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
  if check and completed.returncode != 0:
    # git hook 可能把擋下的原因印在 stdout，只看 stderr 會漏掉
    output = "\n".join(text for text in (completed.stderr.strip(), completed.stdout.strip()) if text)
    raise GitError(f"git {' '.join(args)} 失敗：{output}")
  return completed.stdout.strip()


def worktreesRoot(projectDir):
  projectDir = Path(projectDir).resolve()
  # 放在專案資料夾旁邊，避免被專案自己的搜尋和測試掃到
  return projectDir.parent / f"{projectDir.name}-worktrees"


def planBranch(planIdValue):
  return f"plan/{planIdValue}"


def taskBranch(planIdValue, taskId):
  return f"task/{planIdValue}-{taskId}"


def planWorktreePath(projectDir, planIdValue):
  return worktreesRoot(projectDir) / planIdValue


def taskWorktreePath(projectDir, planIdValue, taskId):
  return worktreesRoot(projectDir) / f"{planIdValue}-{taskId}"


def baselineWorktreePath(projectDir, planIdValue):
  return worktreesRoot(projectDir) / f"{planIdValue}-baseline"


def headCommit(projectDir):
  return runGit(projectDir, "rev-parse", "HEAD")


def hasChanges(path):
  return runGit(path, "status", "--porcelain") != ""


def hasTrackedChanges(path):
  # .board/ 和剛寫好的 plan 都是未追蹤檔案，不會影響 plan 分支的內容，所以不算
  return runGit(path, "status", "--porcelain", "--untracked-files=no") != ""


def mergeInProgress(path):
  # 使用者在 plan worktree 手動解衝突時會留下 MERGE_HEAD，這時 conductor 不能再動這個 worktree
  return runGit(path, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False) != ""


def branchExists(projectDir, branch):
  return runGit(projectDir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False) != ""


def addWorktree(projectDir, path, branch, startPoint):
  path.parent.mkdir(parents=True, exist_ok=True)
  if branchExists(projectDir, branch):
    runGit(projectDir, "worktree", "add", str(path), branch)
  else:
    runGit(projectDir, "worktree", "add", "-b", branch, str(path), startPoint)


def ensurePlanWorktree(projectDir, planIdValue, baseCommit):
  path = planWorktreePath(projectDir, planIdValue)
  if not path.exists():
    addWorktree(projectDir, path, planBranch(planIdValue), baseCommit)
  return path


def ensureTaskWorktree(projectDir, planIdValue, taskId):
  path = taskWorktreePath(projectDir, planIdValue, taskId)
  if not path.exists():
    # 任務開始時才從 plan 分支開出來，前置任務的改動已經合併在裡面
    addWorktree(projectDir, path, taskBranch(planIdValue, taskId), planBranch(planIdValue))
  return path


def addBaselineWorktree(projectDir, planIdValue, baseCommit):
  # 上次沒移除乾淨的基準 worktree 可能留著舊的測試產物，先整個移除再重建
  removeBaselineWorktree(projectDir, planIdValue)
  path = baselineWorktreePath(projectDir, planIdValue)
  path.parent.mkdir(parents=True, exist_ok=True)
  # detached：基準只是暫時跑測試，不需要分支，也不會留下要清的分支
  runGit(projectDir, "worktree", "add", "--detach", str(path), baseCommit)
  return path


def removeBaselineWorktree(projectDir, planIdValue):
  path = baselineWorktreePath(projectDir, planIdValue)
  if path.exists():
    runGit(projectDir, "worktree", "remove", "--force", str(path), check=False)
  if path.exists():
    # 資料夾還在但已經不是登記中的 worktree（例如被 prune 過），git 不肯移除，只能直接刪
    shutil.rmtree(path)
  runGit(projectDir, "worktree", "prune")


def cleanWorktree(path):
  # 這個函式會丟掉未提交的改動，只能用在 worktree 上，絕不能碰使用者的主資料夾
  resolved = Path(path).resolve()
  isWorktreeFolder = resolved.parent.name.endswith("-worktrees")
  # linked worktree 的 .git 是檔案，主工作目錄的 .git 是資料夾
  isLinkedWorktree = (resolved / ".git").is_file()
  if not (isWorktreeFolder and isLinkedWorktree):
    raise GitError(f"拒絕清理非 worktree 的路徑：{path}")
  runGit(path, "reset", "--hard", "HEAD")
  runGit(path, "clean", "-fd")


def gitSucceeds(cwd, *args):
  return subprocess.run(["git", *args], cwd=cwd, capture_output=True).returncode == 0


# 路徑預設是 pathspec，`--` 也不會停用萬用字元：plans/0001-[ab].md 會匹配到 plans/0001-a.md
literalPathspecs = "--literal-pathspecs"


def fileMatchesHead(projectDir, relativePath):
  # 沒被追蹤的檔案 git diff 看不到差異，要先確認 git 認得它
  return (gitSucceeds(projectDir, literalPathspecs, "ls-files", "--error-unmatch", "--", relativePath)
          and gitSucceeds(projectDir, literalPathspecs, "diff", "--quiet", "--no-ext-diff", "--no-textconv",
                          "HEAD", "--", relativePath))


def commitFile(projectDir, relativePath, message):
  # commit 後面接路徑只 commit 這個檔案，主資料夾其他已暫存的改動照樣留在暫存區
  runGit(projectDir, literalPathspecs, "add", "--", relativePath)
  runGit(projectDir, literalPathspecs, "commit", "-q", "-m", message, "--", relativePath)
  return runGit(projectDir, "rev-parse", "HEAD")


def commitAll(path, message):
  runGit(path, "add", "-A")
  runGit(path, "commit", "-q", "-m", message)
  return runGit(path, "rev-parse", "HEAD")


def taskDiff(projectDir, planIdValue, taskId):
  return runGit(projectDir, "diff", f"{planBranch(planIdValue)}...{taskBranch(planIdValue, taskId)}")


def isMerged(projectDir, planIdValue, taskId):
  completed = subprocess.run(["git", "merge-base", "--is-ancestor", taskBranch(planIdValue, taskId),
                              planBranch(planIdValue)], cwd=projectDir, capture_output=True)
  return completed.returncode == 0


def mergeTask(projectDir, planIdValue, taskId, message):
  planPath = planWorktreePath(projectDir, planIdValue)
  completed = subprocess.run(["git", "merge", "--no-ff", "-m", message, taskBranch(planIdValue, taskId)],
                             cwd=planPath, capture_output=True, text=True, encoding="utf-8", errors="replace")
  if completed.returncode == 0:
    return True, runGit(planPath, "rev-parse", "HEAD"), []
  conflicts = unmergedFiles(planPath)
  # 衝突時把 plan worktree 還原，之後的任務才能繼續合併
  runGit(planPath, "merge", "--abort", check=False)
  if not conflicts:
    # 沒有衝突檔卻失敗（例如未追蹤檔案會被覆蓋），不是你能在 worktree 解的衝突，要把 git 的原因帶出去
    raise GitError(f"git merge 失敗：{(completed.stderr or completed.stdout).strip()}")
  return False, None, conflicts


def unmergedFiles(path):
  # 預設 core.quotePath 會把中文檔名輸出成 "\350..." 跳脫字元，拿去找檔案會找不到
  return runGit(path, "-c", "core.quotePath=false", "diff", "--name-only", "--diff-filter=U",
                check=False).splitlines()


def mergePlanIntoTask(projectDir, planIdValue, taskId, message):
  path = taskWorktreePath(projectDir, planIdValue, taskId)
  # 上次解到一半（例如 Claude 回報卡住後你選 retry）時合併還沒結束，沿用留著的衝突，不能再合併一次
  if mergeInProgress(path):
    conflicts = unmergedFiles(path)
    if not conflicts:
      # git add 過不代表標記已經清掉（例如你先 add 再選 retry），還有標記就交回去解，不能直接 commit
      staged = runGit(path, "-c", "core.quotePath=false", "diff", "--cached", "--name-only", check=False).splitlines()
      conflicts = filesWithConflictMarkers(path, staged)
    if not conflicts:
      # 衝突都已經解決並標記好，直接完成這次合併，不然會一直停在合併中
      commitAll(path, message)
    return conflicts
  completed = subprocess.run(["git", "merge", "--no-ff", "-m", message, planBranch(planIdValue)],
                             cwd=path, capture_output=True, text=True, encoding="utf-8", errors="replace")
  if completed.returncode == 0:
    return []
  conflicts = unmergedFiles(path)
  if not conflicts:
    runGit(path, "merge", "--abort", check=False)
    raise GitError(f"git merge 失敗：{(completed.stderr or completed.stdout).strip()}")
  # 衝突標記留在檔案裡，交給 Claude 解；解完由 commitAll 完成這次合併
  return conflicts


# 只認開頭和結尾的標記：單獨一行的 ======= 也是 Markdown 標題底線，不能當成衝突
conflictMarkerPattern = re.compile(r"^(<{7}|>{7})( |$)", re.MULTILINE)


def filesWithConflictMarkers(path, files):
  remaining = []
  for name in files:
    filePath = Path(path) / name
    if filePath.is_file() and conflictMarkerPattern.search(filePath.read_text(encoding="utf-8", errors="replace")):
      remaining.append(name)
  return remaining


def removePlanWorktrees(projectDir, planIdValue):
  removed = []
  root = worktreesRoot(projectDir)
  if root.exists():
    for path in sorted(root.iterdir()):
      if path == baselineWorktreePath(projectDir, planIdValue):
        # Alfred 跑基準測試到一半被關掉時會留下來
        removeBaselineWorktree(projectDir, planIdValue)
        removed.append(path.name)
      elif path.name == planIdValue or path.name.startswith(f"{planIdValue}-"):
        runGit(projectDir, "worktree", "remove", "--force", str(path))
        removed.append(path.name)
  branches = runGit(projectDir, "branch", "--list", f"task/{planIdValue}-*", "--format=%(refname:short)")
  for branch in branches.splitlines():
    runGit(projectDir, "branch", "-D", branch)
    removed.append(branch)
  runGit(projectDir, "worktree", "prune")
  return removed
