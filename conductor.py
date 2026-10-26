"""指揮程式：讀事件決定下一步，啟動 Claude 寫程式、Codex 審查，合併通過的任務。

用法：python conductor.py <專案資料夾>
"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import board_agents
import board_git
from board_core import (BoardError, PlanStatus, TaskStatus, agentLanes, appendEvent, boardDir, configProblems,
                        deriveState, isPlanFinished, loadConfig, loadPlans, readEvents, readUserConfig, readyTasks,
                        taskKey, tasksWithStatus)
from board_prompts import buildResolvePrompt, buildReviewPrompt, buildWritePrompt, claudeRules, codexRules
from board_review import (codexReviewSchema, renderReviewMarkdown, reviewFileName, validateClaudeResult,
                          validateReview)

outputWaitAfterKillSeconds = 10
maxReasonTailChars = 500
# 每解完一次又衝突，代表其他任務一直搶先合併；超過次數就停下來給你看，不無限循環
maxAutoResolves = 2

eventLabels = {
  "planSubmitted": "送出 plan，等你核准",
  "planApproved": "plan 已核准",
  "planRejected": "plan 被退回",
  "planAbandoned": "plan 已放棄",
  "planCompleted": "plan 完成",
  "taskStarted": "開始寫程式",
  "taskWritten": "寫完並 commit",
  "taskBlocked": "回報卡住",
  "testRun": "跑測試",
  "reviewStarted": "開始審查",
  "reviewPassed": "審查通過",
  "reviewFailed": "審查不通過",
  "taskMerged": "已合併進 plan 分支",
  "taskConflicted": "合併衝突，交給 Claude 解",
  "agentFailed": "agent 失敗",
  "taskInterrupted": "中斷，回到排隊",
  "needsYou": "等你處理",
  "userDecision": "你的決定",
  "paused": "全部暫停",
  "resumed": "繼續執行",
}


def formatEvent(event):
  timeText = (event.get("time") or "")[11:19]
  who = " ".join(part for part in (event.get("plan"), event.get("task"), event.get("agent")) if part)
  data = event.get("data") or {}
  details = []
  if event.get("round"):
    details.append(f"第 {event['round']} 輪")
  if event["type"] == "reviewFailed":
    details.append(f"必改 {data.get('mustFix', 0)} 條")
  if event["type"] == "testRun":
    details.append("通過" if data.get("passed") else "失敗")
  if data.get("mode") == "resolve":
    details.append("解合併衝突")
  for key in ("reason", "action", "note"):
    if data.get(key):
      details.append(str(data[key]))
  detailText = f"（{'，'.join(details)}）" if details else ""
  return f"{timeText} {who} {eventLabels.get(event['type'], event['type'])}{detailText}".replace("  ", " ")


class Conductor:
  def __init__(self, projectDir, config=None, out=print):
    self.projectDir = Path(projectDir).resolve()
    self.config = config or loadConfig(self.projectDir)
    self.out = out
    self.jobs = {}
    self.recovered = False
    self.notified = set()
    # 啟動前的歷史事件不重印，終端機只顯示這次執行期間發生的事
    self.printedCount = len(readEvents(self.projectDir))
    self.claudeCommand = board_agents.resolveCommand(self.config["claudeCommand"], "claude")
    self.codexCommand = board_agents.resolveCommand(self.config["codexCommand"], "codex")

  def currentState(self):
    loadErrors = []
    plans = loadPlans(self.projectDir, loadErrors)
    for message in loadErrors:
      self.notifyOnce("警告｜" + message)
    return plans, deriveState(readEvents(self.projectDir), plans, self.config)

  def notifyOnce(self, message):
    # 每隔幾秒就跑一輪，問題沒解決前每輪都會再遇到同一個錯誤；只印第一次，終端機才不會被洗版
    if message in self.notified:
      return
    self.notified.add(message)
    self.out(message)

  def timeoutSeconds(self):
    return self.config["timeoutMinutes"] * 60

  def event(self, eventType, info, **data):
    appendEvent(self.projectDir, eventType, plan=info["plan"], task=info["task"], agent=info.get("agent"),
                roundNumber=info.get("round"), data=data)

  def logPath(self, info, kind):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return boardDir(self.projectDir) / "logs" / f"{info['plan']}-{info['task']}-r{info['round']}-{kind}-{stamp}.log"

  def readReview(self, notes):
    path = boardDir(self.projectDir) / notes if notes else None
    return path.read_text(encoding="utf-8") if path and path.exists() else ""

  def freeLane(self, prefix, kind, preferred=None):
    busy = {info["agent"] for info in self.jobs.values() if info["kind"] == kind}
    lanes = agentLanes(prefix, self.config)
    # 修正輪優先交給上一輪的同一個身分，儀表板上看起來才連貫
    if preferred in lanes and preferred not in busy:
      return preferred
    return next((lane for lane in lanes if lane not in busy), None)

  def tick(self):
    plans, state = self.currentState()
    if not self.recovered:
      self.recoverInterrupted(state)
      self.recovered = True
      plans, state = self.currentState()
    if state["paused"]:
      self.stopJobs(list(self.jobs), recordInterruption=True)
    else:
      self.stopJobsOfInactiveTasks(state)
    self.collectFinishedJobs(plans, state)
    plans, state = self.currentState()
    self.ensurePlanWorktrees(state)
    self.mergeReadyTasks(state)
    plans, state = self.currentState()
    self.completeFinishedPlans(state)
    if not state["paused"]:
      self.startReviews(plans, state)
      self.startWrites(plans, state)
    self.printNewEvents()

  def safeTick(self):
    try:
      self.tick()
    except Exception as error:
      # 單一輪出錯（例如 git 失敗、事件紀錄鎖逾時）不能讓 conductor 結束，否則還在跑的 agent 會變成沒人管的孤兒
      self.notifyOnce(f"錯誤（{type(error).__name__}）：{error}")
      # 下一輪重跑復原，把沒有 job 卻停在寫程式中／審查中的任務拉回排隊；recoverInterrupted 會跳過 self.jobs 裡的任務，重跑是安全的
      self.recovered = False

  def recoverInterrupted(self, state):
    # 剛啟動時手上沒有任何 agent；事件裡還在寫或審的任務，代表上次 conductor 中途結束
    for key, taskState in state["tasks"].items():
      stage = {TaskStatus.writing: "write", TaskStatus.reviewing: "review"}.get(taskState["status"])
      if stage and key not in self.jobs and state["plans"][taskState["plan"]]["status"] == PlanStatus.active:
        appendEvent(self.projectDir, "taskInterrupted", plan=taskState["plan"], task=taskState["task"],
                    roundNumber=taskState["round"], data={"stage": stage})

  def stopJobs(self, keys, recordInterruption):
    for key in keys:
      info = self.jobs.get(key)
      if info is None:
        continue
      if recordInterruption and info["job"].process.poll() is not None:
        # 已經自己結束的 agent 可能寫好了成果，留給 collectFinishedJobs 照常處理，暫停時才不會丟掉
        continue
      del self.jobs[key]
      info["job"].kill()
      info["job"].poll()
      if recordInterruption:
        self.event("taskInterrupted", info, stage=info["kind"])

  def stopJobsOfInactiveTasks(self, state):
    # 任務或整份 plan 被放棄後，正在跑的 agent 沒有意義了，直接結束、不留中斷紀錄
    keys = [key for key in self.jobs if state["tasks"].get(key, {}).get("status") == TaskStatus.abandoned]
    self.stopJobs(keys, recordInterruption=False)

  def quotaSuspect(self, job, error):
    if job.timedOut:
      return False
    # JSON 行裡有 agent 讀寫的檔案內容，裡面出現 rate limit 等字不代表額度用完，只看純文字行
    plainLines = [line for line in list(job.tail)[-20:] if not line.lstrip().startswith("{")]
    text = "\n".join([str(error or ""), str(job.activity.get("error", ""))] + plainLines)
    return board_agents.looksLikeQuota(text)

  def failureTail(self, job):
    # 輸出尾段可能是整段 stack trace；事件只留最後一段，完整內容在 .board/logs/
    return job.tailText(5)[-maxReasonTailChars:]

  def pauseForQuota(self, info, stage):
    appendEvent(self.projectDir, "paused", data={"reason": f"額度用完（{info['agent']}），用 board.py resume 繼續"})
    self.event("taskInterrupted", info, stage=stage)

  def printNewEvents(self):
    events = readEvents(self.projectDir)
    for event in events[self.printedCount:]:
      self.out(formatEvent(event))
    self.printedCount = len(events)

  def collectFinishedJobs(self, plans, state):
    for key, info in list(self.jobs.items()):
      if not info["job"].poll():
        continue
      del self.jobs[key]
      try:
        if info["kind"] == "write":
          self.finishWrite(info)
        else:
          self.finishReview(plans[info["plan"]], state["tasks"][key], info)
      except BoardError:
        # 事件紀錄寫不進去時，再寫 needsYou 多半也會失敗；交給 safeTick 下一輪的復原（taskInterrupted）處理
        raise
      except Exception as error:
        # 不記成 agentFailed：agent 的成果可能是好的，agentFailed 重試時會清掉 worktree
        self.event("needsYou", info, kind="internalError",
                   reason=f"處理 agent 結果時出錯：{type(error).__name__}: {error}")

  def ensurePlanWorktrees(self, state):
    for planState in state["plans"].values():
      if planState["status"] == PlanStatus.active:
        try:
          board_git.ensurePlanWorktree(self.projectDir, planState["id"], planState["baseCommit"])
        except Exception as error:
          # plan 沒有「等你處理」狀態可以記，只能印出來；下一輪會自動再試
          self.notifyOnce(f"{planState['id']} 的 plan worktree 建立失敗：{error}")

  def startWrites(self, plans, state):
    for taskState in readyTasks(state):
      if taskKey(taskState["plan"], taskState["task"]) in self.jobs:
        continue
      lane = self.freeLane("claude", "write", taskState["agent"])
      if lane is None:
        return
      self.startWrite(plans[taskState["plan"]], taskState, lane)

  def claudeAllowedTools(self):
    allowedTools = list(self.config.get("claudeAllowedTools", []))
    testCommand = self.config.get("testCommand", "")
    if testCommand:
      # 只開放專案的測試指令，Claude 才能自我驗證，又不會拿到其他 shell 權限；Windows 上 shell 工具是 PowerShell
      allowedTools += [f"Bash({testCommand}*)", f"PowerShell({testCommand}*)"]
    if self.config.get("tddCrap"):
      # tdd-crap 的「先寫測試、確認失敗」要能只跑幾個測試，每次都跑完整品質檢查太慢
      allowedTools += ["Bash(python -m pytest*)", "PowerShell(python -m pytest*)"]
    return allowedTools

  def startWrite(self, plan, taskState, lane):
    info = {"kind": "write", "plan": taskState["plan"], "task": taskState["task"], "round": taskState["round"],
            "agent": lane, "text": taskState["text"], "mode": "resolve" if taskState["resolving"] else "write"}
    resume = bool(taskState["resumable"] and taskState["sessionId"])
    sessionId = taskState["sessionId"] if resume else str(uuid.uuid4())
    cleanRemerge = False
    try:
      worktree = board_git.ensureTaskWorktree(self.projectDir, info["plan"], info["task"])
      if taskState["needsClean"]:
        board_git.cleanWorktree(worktree)
      if info["mode"] == "resolve":
        info["conflicts"] = board_git.mergePlanIntoTask(self.projectDir, info["plan"], info["task"],
                                                        f"{info['plan']} {info['task']} 合併最新的 plan 分支")
        cleanRemerge = not info["conflicts"]
      if not cleanRemerge:
        if info["mode"] == "resolve":
          prompt = buildResolvePrompt(plan, info["task"], info["round"], info["conflicts"], taskState["userNote"])
        else:
          prompt = buildWritePrompt(plan, info["task"], info["round"], self.readReview(taskState["lastReviewNotes"]),
                                    taskState["userNote"],
                                    self.config["testCommand"] if self.config.get("tddCrap") else "")
        argv = board_agents.buildClaudeArgv(self.claudeCommand, sessionId, resume, claudeRules,
                                            self.claudeAllowedTools())
        info["job"] = board_agents.AgentJob(argv, worktree, prompt, self.logPath(info, "write"),
                                            board_agents.parseClaudeLine, self.timeoutSeconds()).start()
    except Exception as error:
      # 例如核准後 plan 檔被改掉這個任務；接住所有例外，否則同一個任務每一輪都會擋住其他任務
      self.event("agentFailed", info, stage="write", reason=f"無法啟動：{type(error).__name__}: {error}")
      return
    info["worktree"] = worktree
    if cleanRemerge:
      # 最新的 plan 分支合併進來沒有衝突（例如你已經在任務的 worktree 處理過），不必叫 Claude，直接測試、審查
      info["agent"] = None
      self.event("taskWritten", info, commit=board_git.headCommit(worktree), summary="合併最新的 plan 分支，沒有衝突",
                 mode="resolve", conflicts=[])
      self.runTests(info)
      return
    # 先登記再寫事件：事件寫入失敗時 agent 不會變成沒人管的孤兒
    key = taskKey(info["plan"], info["task"])
    self.jobs[key] = info
    try:
      self.event("taskStarted", info, worktree=str(worktree), sessionId=sessionId, resume=resume, mode=info["mode"])
    except Exception:
      self.jobs.pop(key, None)
      info["job"].kill()
      raise

  def finishWrite(self, info):
    job = info["job"]
    if job.killed and not job.timedOut:
      self.event("taskInterrupted", info, stage="write")
      return
    result, error = board_agents.claudeFinalResult(job.activity)
    if result is None:
      # 額度用完不是任務的問題，不能消耗重試次數；整個暫停，等你恢復額度後 resume
      if self.quotaSuspect(job, error):
        self.pauseForQuota(info, "write")
        return
      reason = "超時" if job.timedOut else (error or f"結束代碼 {job.exitCode}")
      self.event("agentFailed", info, stage="write", reason=f"{reason}｜{self.failureTail(job)}")
      return
    errors = validateClaudeResult(result)
    if errors:
      self.event("agentFailed", info, stage="write", reason="回覆格式不符：" + "；".join(errors))
      return
    if result["status"] == "blocked":
      self.event("taskBlocked", info, reason=result["blockedReason"])
      return
    try:
      if info["mode"] == "resolve":
        remaining = board_git.filesWithConflictMarkers(info["worktree"], info["conflicts"])
        if remaining:
          # 標記還在就 commit 會把壞掉的檔案送去審查；當成這次失敗，重試時會清乾淨重新合併
          self.event("agentFailed", info, stage="write", reason="還有沒解掉的衝突標記：" + "、".join(remaining))
          return
        subject = "解合併衝突（" + "、".join(info["conflicts"]) + "）"
      elif not board_git.hasChanges(info["worktree"]):
        self.event("needsYou", info, kind="noChanges", reason="Claude 回報完成，但沒有任何改動")
        return
      else:
        subject = info["text"]
      commit = board_git.commitAll(info["worktree"], f"{info['plan']} {info['task']} 第{info['round']}輪：{subject}")
    except board_git.GitError as error:
      # 例如專案的 pre-commit hook 失敗；不記事件的話任務會永遠停在「寫程式中」
      self.event("needsYou", info, kind="commitFailed", reason=f"commit 失敗：{error}")
      return
    self.event("taskWritten", info, commit=commit, summary=result["summary"], mode=info["mode"],
               conflicts=info.get("conflicts", []))
    self.runTests(info)

  def runTests(self, info):
    command = self.config["testCommand"]
    if not command:
      return
    # 測試在這裡同步執行：跑測試的這段時間 conductor 不排新工作，所以測試指令要夠快
    # 不用 subprocess.run：Windows 上逾時只會結束 cmd.exe，孫行程還握著管線，communicate 會一直等
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(command, shell=True, cwd=info["worktree"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               encoding="utf-8", errors="replace", **options)
    try:
      output, _ = process.communicate(timeout=self.timeoutSeconds())
      passed = process.returncode == 0
    except subprocess.TimeoutExpired:
      board_agents.killProcessTree(process.pid)
      try:
        output, _ = process.communicate(timeout=outputWaitAfterKillSeconds)
        output = "測試超時\n" + (output or "")
      except subprocess.TimeoutExpired:
        # 脫離程式樹的孫程式殺不到又握著輸出管線，再等下去整個 conductor 會卡住，只能放棄輸出；
        # 管線交給背景執行緒等它結束後關閉，不留下沒關的檔案
        threading.Thread(target=process.communicate, daemon=True).start()
        output = "測試超時（輸出無法取得）"
      passed = False
    self.event("testRun", info, passed=passed, tail="\n".join(output.splitlines()[-50:]))

  def startReviews(self, plans, state):
    for taskState in tasksWithStatus(state, TaskStatus.toReview):
      if taskKey(taskState["plan"], taskState["task"]) in self.jobs:
        continue
      lane = self.freeLane("codex", "review", taskState["reviewAgent"])
      if lane is None:
        return
      self.startReview(plans[taskState["plan"]], taskState, lane)

  def startReview(self, plan, taskState, lane, previousErrors=None, attempt=1):
    info = {"kind": "review", "plan": taskState["plan"], "task": taskState["task"], "round": taskState["round"],
            "agent": lane, "attempt": attempt}
    runDir = boardDir(self.projectDir) / "runs"
    runDir.mkdir(parents=True, exist_ok=True)
    schemaPath = runDir / "codex-review-schema.json"
    schemaPath.write_text(json.dumps(codexReviewSchema, ensure_ascii=False), encoding="utf-8")
    info["outPath"] = runDir / f"{info['plan']}-{info['task']}-r{info['round']}-review{attempt}.json"
    # 舊的輸出檔留著的話，這次 Codex 沒寫出結果也會被誤讀成成功
    info["outPath"].unlink(missing_ok=True)
    try:
      worktree = board_git.taskWorktreePath(self.projectDir, info["plan"], info["task"])
      diff = board_git.taskDiff(self.projectDir, info["plan"], info["task"])
      resolvedConflicts = taskState["conflictFiles"] if taskState["lastWriteMode"] == "resolve" else None
      prompt = codexRules + "\n\n" + buildReviewPrompt(plan, info["task"], info["round"], diff,
                                                       taskState["lastTest"], previousErrors, resolvedConflicts,
                                                       bool(self.config.get("tddCrap")))
      argv = board_agents.buildCodexArgv(self.codexCommand, worktree, schemaPath, info["outPath"])
      info["job"] = board_agents.AgentJob(argv, worktree, prompt, self.logPath(info, f"review{attempt}"),
                                          board_agents.parseCodexLine, self.timeoutSeconds()).start()
    except Exception as error:
      self.event("agentFailed", info, stage="review", reason=f"無法啟動：{type(error).__name__}: {error}")
      return
    # 先登記再寫事件：事件寫入失敗時 agent 不會變成沒人管的孤兒
    key = taskKey(info["plan"], info["task"])
    self.jobs[key] = info
    try:
      if attempt == 1:
        self.event("reviewStarted", info)
    except Exception:
      self.jobs.pop(key, None)
      info["job"].kill()
      raise

  def finishReview(self, plan, taskState, info):
    job = info["job"]
    if job.killed and not job.timedOut:
      self.event("taskInterrupted", info, stage="review")
      return
    result, error = board_agents.codexFinalResult(info["outPath"])
    if result is None:
      if self.quotaSuspect(job, error):
        self.pauseForQuota(info, "review")
        return
      reason = "超時" if job.timedOut else (error or f"結束代碼 {job.exitCode}")
      self.event("agentFailed", info, stage="review", reason=f"{reason}｜{self.failureTail(job)}")
      return
    errors = validateReview(result, taskState["requirementIds"])
    if errors:
      if info["attempt"] == 1:
        # 格式不符先附上錯誤請它重審一次，第二次還不行才交給你
        self.startReview(plan, taskState, info["agent"], previousErrors=errors, attempt=2)
        return
      self.event("needsYou", info, kind="invalidReview", reason="審查結果不符規則：" + "；".join(errors))
      return
    notesName = reviewFileName(info["plan"], info["task"], info["round"])
    reviewsDir = boardDir(self.projectDir) / "reviews"
    reviewsDir.mkdir(parents=True, exist_ok=True)
    (reviewsDir / notesName).write_text(renderReviewMarkdown(info["plan"], info["task"], info["round"], result),
                                        encoding="utf-8")
    eventType = "reviewPassed" if result["verdict"] == "pass" else "reviewFailed"
    self.event(eventType, info, notes=f"reviews/{notesName}", mustFix=len(result["mustFix"]),
               suggest=len(result.get("suggest", [])))

  def mergeReadyTasks(self, state):
    for taskState in tasksWithStatus(state, TaskStatus.merging):
      info = {"plan": taskState["plan"], "task": taskState["task"], "round": taskState["round"]}
      try:
        self.mergeOneTask(taskState, info)
      except BoardError:
        # 事件紀錄寫不進去時交給 safeTick；下一輪 isMerged 會補記已經完成的合併
        raise
      except Exception as error:
        # 一個任務合併出錯（例如 git 拒絕覆蓋未追蹤檔案）不能擋住其他任務的合併
        self.event("needsYou", info, kind="mergeFailed", reason=f"合併失敗：{error}")

  def mergeOneTask(self, taskState, info):
    planIdValue, taskId = taskState["plan"], taskState["task"]
    planPath = board_git.planWorktreePath(self.projectDir, planIdValue)
    # 你正在 plan worktree 解衝突時不能動它：merge --abort 會毀掉你解到一半的內容，這個 tick 先跳過
    if board_git.mergeInProgress(planPath) or board_git.hasTrackedChanges(planPath):
      return
    worktree = board_git.taskWorktreePath(self.projectDir, planIdValue, taskId)
    try:
      if taskState["userDone"] and worktree.exists() and board_git.mergeInProgress(worktree):
        # 解衝突途中停下時，任務 worktree 留著還沒結束的合併；直接 commit 會把衝突標記帶進 plan 分支
        if board_git.isMerged(self.projectDir, planIdValue, taskId):
          # 你在 plan worktree 解完了，任務 worktree 裡解到一半的合併已經用不到
          self.event("taskMerged", info, commit=board_git.runGit(self.projectDir, "rev-parse", board_git.planBranch(planIdValue)))
          return
        files = sorted(set(board_git.unmergedFiles(worktree)) | set(taskState["conflictFiles"]))
        remaining = board_git.filesWithConflictMarkers(worktree, files)
        if remaining:
          self.event("needsYou", info, kind="mergeConflict", conflicts=remaining,
                     reason=f"任務的 worktree 還有沒解掉的衝突標記：{'、'.join(remaining)}")
          return
      if taskState["userDone"] and worktree.exists() and board_git.hasChanges(worktree):
        board_git.commitAll(worktree, f"{planIdValue} {taskId} 使用者手動修改")
    except board_git.GitError as error:
      self.event("needsYou", info, kind="commitFailed", reason=f"commit 失敗：{error}")
      return
    if board_git.isMerged(self.projectDir, planIdValue, taskId):
      # 你已經自己合併過（例如處理完衝突），只要記下來就好
      self.event("taskMerged", info, commit=board_git.runGit(self.projectDir, "rev-parse", board_git.planBranch(planIdValue)))
      return
    merged, commit, conflicts = board_git.mergeTask(self.projectDir, planIdValue, taskId,
                                                    f"合併 {planIdValue} {taskId}：{taskState['text']}")
    if merged:
      self.event("taskMerged", info, commit=commit)
    elif taskState["conflictCount"] < maxAutoResolves:
      self.event("taskConflicted", info, conflicts=conflicts, reason="、".join(conflicts))
    else:
      reason = "合併衝突：" + "、".join(conflicts)
      if taskState["conflictCount"]:
        reason += f"（已自動解過 {taskState['conflictCount']} 次）"
      self.event("needsYou", info, kind="mergeConflict", reason=reason, conflicts=conflicts)

  def completeFinishedPlans(self, state):
    for planState in state["plans"].values():
      if planState["status"] == PlanStatus.active and isPlanFinished(state, planState["id"]):
        appendEvent(self.projectDir, "planCompleted", plan=planState["id"])

  def runForever(self):
    self.out(f"conductor 啟動：{self.projectDir}（Ctrl+C 結束）")
    try:
      while True:
        self.safeTick()
        time.sleep(self.config["pollSeconds"])
    except KeyboardInterrupt:
      self.out("conductor 結束中，正在停止所有 agent…")
      self.stopJobs(list(self.jobs), recordInterruption=True)
      self.printNewEvents()


def conductorLockPath(projectDir):
  return boardDir(projectDir) / "conductor.lock"


def readLockPid(projectDir):
  try:
    pid = int(conductorLockPath(projectDir).read_text(encoding="utf-8").strip())
  except (OSError, ValueError):
    return None
  return pid if pid > 0 else None


def processAlive(pid):
  if os.name == "nt":
    # Windows 的 os.kill(pid, 0) 等於送出 Ctrl+C 事件，不能拿來檢查；改問 tasklist
    completed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True,
                               errors="replace")
    return str(pid) in completed.stdout.split()
  try:
    os.kill(pid, 0)
  except ProcessLookupError:
    return False
  except PermissionError:
    return True
  return True


def acquireConductorLock(projectDir):
  lockPath = conductorLockPath(projectDir)
  lockPath.parent.mkdir(parents=True, exist_ok=True)
  # 第二次還建不起來，代表另一個 conductor 剛好同時搶到鎖
  for _ in range(2):
    try:
      handle = os.open(lockPath, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
      ownerPid = readLockPid(projectDir)
      if ownerPid is not None and processAlive(ownerPid):
        return False
      # 上一個 conductor 沒有正常結束（例如視窗被直接關掉），鎖檔留著但程式已經不在
      lockPath.unlink(missing_ok=True)
      continue
    with os.fdopen(handle, "w", encoding="utf-8") as lockFile:
      lockFile.write(str(os.getpid()))
    return True
  return False


def releaseConductorLock(projectDir):
  conductorLockPath(projectDir).unlink(missing_ok=True)


def main(argv=None):
  argv = sys.argv[1:] if argv is None else argv
  if not argv:
    print("用法：python conductor.py <專案資料夾>")
    return 1
  if not sys.stdout.isatty():
    # 輸出被導到檔案或其他程式時，Windows 預設編碼不是 UTF-8，中文會變亂碼
    sys.stdout.reconfigure(encoding="utf-8")
  projectDir = Path(argv[0]).resolve()
  if not projectDir.is_dir():
    print(f"找不到專案資料夾：{projectDir}")
    return 1
  # 不是 git repo 時 worktree、commit、合併全都做不了，與其每一輪都報錯，不如一開始就停
  if board_git.runGit(projectDir, "rev-parse", "--is-inside-work-tree", check=False) != "true":
    print(f"{projectDir} 不是 git repo，conductor 需要用 git 建立 worktree")
    return 1
  problems = configProblems(readUserConfig(projectDir))
  if problems:
    print(".board/config.json 要先修正：")
    for problem in problems:
      print("  " + problem)
    return 1
  # 兩個 conductor 同時跑時，後開的會把前一個正在跑的任務當成中斷並清掉 worktree
  if not acquireConductorLock(projectDir):
    print(f"這個專案已經有 conductor 在跑（PID {readLockPid(projectDir)}），不能同時跑兩個")
    return 1
  try:
    Conductor(projectDir).runForever()
  finally:
    releaseConductorLock(projectDir)
  return 0


if __name__ == "__main__":
  sys.exit(main())
