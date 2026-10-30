"""指揮程式：讀事件決定下一步，啟動寫程式、審查的 agent（Claude 或 Codex），合併通過的任務。

用法：python conductor.py <專案資料夾>
"""
import json
import os
import re
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
                        defaultConfig, deriveState, isPlanFinished, loadConfig, loadPlans, readEvents,
                        readUserConfig, readyTasks, roleChoiceText, roleNames, roleSpec, taskKey, tasksWithStatus)
from board_prompts import buildResolvePrompt, buildReviewPrompt, buildWritePrompt, reviewerRules, writerRules
from board_scheduling import availableLane
from board_review import (renderReviewMarkdown, reviewFileName, reviewResultSchema, validateReview,
                          validateWriteResult, writeResultSchema)

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
  "testBaseline": "跑基準測試",
  "taskStarted": "開始寫程式",
  "taskWritten": "寫完並 commit",
  "taskBlocked": "回報卡住",
  "testRun": "跑測試",
  "mergeTestRun": "合併後測試",
  "mergeTestSkipped": "略過合併後測試",
  "reviewStarted": "開始審查",
  "reviewPassed": "審查通過",
  "reviewFailed": "審查不通過",
  "taskMerged": "已合併進 plan 分支",
  "taskConflicted": "合併衝突，交給寫程式的 agent 解",
  "agentFailed": "agent 失敗",
  "taskInterrupted": "中斷，回到排隊",
  "needsYou": "等你處理",
  "userDecision": "你的決定",
  "paused": "全部暫停",
  "resumed": "繼續執行",
}


def failureNames(output):
  # pytest short test summary 的「FAILED 名稱 - 原因」；要看完整輸出，失敗多時最後 50 行裝不下
  names = []
  for line in output.splitlines():
    if line.startswith(("FAILED ", "ERROR ")):
      name = line.split(" ", 1)[1].split(" - ", 1)[0].strip()
      if name and name not in names:
        names.append(name)
  return names


# 檔案路徑可能有空白；函式名稱沒有空白，後面一定接著「 C=複雜度」
qualityLinePattern = re.compile(r"^(PASS|FAIL) (.+):\d+ (\S+) C=")


def qualityResults(output):
  """讀 tdd-crap 品質檢查每個函式一行的 PASS／FAIL，回傳 (checked, names)。"""
  # pytest 失敗時品質檢查不算覆蓋率、不印這些行，checked 才分得出「沒有超標」和「沒有檢查」
  checked = False
  names = []
  for line in output.splitlines():
    match = qualityLinePattern.match(line)
    if not match:
      continue
    checked = True
    # 名稱不含行號：函式上面多一行，行號就變了，含行號就跟基準比不了
    name = f"品質門檻 {match.group(2)}::{match.group(3)}"
    if match.group(1) == "FAIL" and name not in names:
      names.append(name)
  return checked, names


def baselinePytestPassed(baseline):
  # 品質檢查只在 pytest 全過時才印 PASS／FAIL 行；有這些行又沒有 pytest 失敗名稱，就是 pytest 全過、只有品質超標
  return bool(baseline.get("passed") or (baseline.get("qualityChecked") and not baseline.get("failures")))


def comparePytestPart(baseline, pytestPassed, failures):
  if pytestPassed:
    return "passed", []
  if baselinePytestPassed(baseline):
    return "newFailure", list(failures)
  baseFailures = baseline.get("failures") or []
  if not baseFailures:
    return "unknown", []
  newFailures = [name for name in failures if name not in baseFailures]
  # 一個名稱都分不出來時，不能確定失敗的就是基準那幾個
  if newFailures or not failures:
    return "newFailure", newFailures
  return "preexisting", []


def compareWithBaseline(baseline, passed, failures, qualityChecked=False, qualityFailures=()):
  """依 plan「測試判定」回傳 (verdict, newFailures)；pytest 與品質門檻分開比再合併。"""
  if passed:
    return "passed", []
  if baseline is None:
    # 正常不會發生（基準記下來前不派工）；沒有東西可比時不擋，交給審查
    return "unknown", []
  verdict, newFailures = comparePytestPart(baseline, qualityChecked and bool(qualityFailures) and not failures, failures)
  if not (qualityChecked and baseline.get("qualityChecked")):
    # 有一方沒檢查品質：品質部分不判定為新增，也不能讓整體變成通過
    return ("unknown", []) if verdict == "passed" else (verdict, newFailures)
  baseQualityFailures = baseline.get("qualityFailures") or []
  newQualityFailures = [name for name in qualityFailures if name not in baseQualityFailures]
  if newQualityFailures:
    return "newFailure", newFailures + newQualityFailures
  # pytest 部分通過時一定有品質超標（判定 pytest 通過的條件），而且都在基準裡
  return ("preexisting", []) if verdict == "passed" else (verdict, newFailures)


def mergeTestOutcome(predecessor, record, covers, mergeFailures, splitCommit=None, staleSplit=False):
  """合併後測試的判定（plan「共同契約」的「判定」）；回傳要寫進 mergeTestRun 的欄位。"""
  verdict, newFailures = compareWithBaseline(predecessor, record["passed"], record["failures"],
                                             record["qualityChecked"], record["qualityFailures"])
  hadSplit = bool(splitCommit) or staleSplit
  if verdict == "unknown" or (verdict == "newFailure" and not newFailures):
    # 分不出是哪些失敗，不能自動退回任何任務；這筆也不當之後的比較對象
    outcomes = {taskId: {"result": "unknown", "names": []} for taskId in covers}
    return {"verdict": verdict, "newFailures": newFailures, "outcomes": outcomes, "advance": False,
            "split": "clear" if hadSplit else None}
  if verdict == "newFailure" and len(covers) > 1:
    # 涵蓋好幾次合併：先不退回，照合併順序逐一測，找出從哪次合併開始失敗
    outcomes = {taskId: {"result": "split", "names": []} for taskId in covers}
    return {"verdict": verdict, "newFailures": newFailures, "outcomes": outcomes, "advance": False, "split": "start"}
  names = set(record["failures"]) | set(record["qualityFailures"])
  outcomes = {}
  for taskId in covers:
    still = [name for name in mergeFailures.get(taskId, []) if name in names]
    if still:
      extra = [name for name in newFailures if name not in still] if verdict == "newFailure" else []
      outcomes[taskId] = {"result": "stillFailing", "names": still + extra}
    elif verdict == "newFailure":
      outcomes[taskId] = {"result": "newFailure", "names": list(newFailures)}
    else:
      outcomes[taskId] = {"result": "verified", "names": []}
  reachedSplit = bool(splitCommit) and record["commit"] == splitCommit
  return {"verdict": verdict, "newFailures": newFailures, "outcomes": outcomes, "advance": True,
          "split": "clear" if reachedSplit or staleSplit else None}


class BackgroundTestJob:
  """在背景執行緒跑任務的測試；結果留給主執行緒的 collectFinishedJobs 寫事件，事件只由主執行緒寫。"""

  def __init__(self, runTestCommand, cwd):
    self.runTestCommand = runTestCommand
    self.cwd = cwd
    self.lock = threading.Lock()
    self.process = None
    self.killed = False
    self.result = None
    self.error = None
    self.thread = threading.Thread(target=self.run, daemon=True)

  def start(self):
    self.thread.start()
    return self

  def run(self):
    try:
      self.result = self.runTestCommand(self.cwd, self.started)
    except Exception as error:
      self.error = error

  def started(self, process):
    with self.lock:
      self.process = process
      killNow = self.killed
    # 測試程式還沒啟動就被要求結束時，啟動後立刻結束，不留下沒人等結果的測試
    if killNow:
      board_agents.killProcessTree(process.pid)

  def poll(self):
    return not self.thread.is_alive()

  def wait(self, timeoutSeconds):
    self.thread.join(timeoutSeconds)

  def kill(self):
    with self.lock:
      self.killed = True
      process = self.process
    if process is not None and process.poll() is None:
      board_agents.killProcessTree(process.pid)


def reusedFromText(source):
  who = " ".join(part for part in (source.get("plan"), source.get("task")) if part)
  testKind = {"baseline": "基準測試", "mergeTestRun": "合併後測試"}.get(source.get("kind"), "測試")
  return f"沿用 {who} 的{testKind}結果"


def testDetails(event, data):
  details = ["通過" if data.get("passed") else "失敗"]
  if data.get("reusedFrom"):
    details.append(reusedFromText(data["reusedFrom"]))
  if event["type"] == "mergeTestRun":
    details.append("涵蓋 " + "、".join(data.get("covers") or []))
    if data.get("reused"):
      details.append("沿用同一個 commit 的結果")
  return details


def eventDetails(event, data):
  details = [f"第 {event['round']} 輪"] if event.get("round") else []
  if event["type"] == "reviewFailed":
    details.append(f"必改 {data.get('mustFix', 0)} 條")
  if event["type"] in ("testRun", "testBaseline", "mergeTestRun"):
    details += testDetails(event, data)
  if data.get("mode") == "resolve":
    details.append("解合併衝突")
  return details + [str(data[key]) for key in ("reason", "action", "note") if data.get(key)]


def formatEvent(event):
  timeText = (event.get("time") or "")[11:19]
  who = " ".join(part for part in (event.get("plan"), event.get("task"), event.get("agent")) if part)
  details = eventDetails(event, event.get("data") or {})
  detailText = f"（{'，'.join(details)}）" if details else ""
  return f"{timeText} {who} {eventLabels.get(event['type'], event['type'])}{detailText}".replace("  ", " ")


def baselineKey(planIdValue):
  # 任務的 key 是「plan/T編號」，用 baseline 當後綴不會和任務撞名
  return f"{planIdValue}/baseline"


def mergeTestKey(planIdValue):
  return f"{planIdValue}/mergetest"


# 任務測試與合併後測試都要有被測的 commit、完整失敗名單和指令，才能拿來當基準
reusableTestRunFields = ("commit", "failures", "command")


def baselineReuseCandidates(events, command):
  """可以沿用成基準的 (種類, 事件)，新的在前；不論來源 plan 的狀態。"""
  candidates = []
  for event in reversed(events):
    data = event.get("data") or {}
    if event["type"] == "testBaseline":
      kind = "baseline"
    elif event["type"] in ("testRun", "mergeTestRun") and all(field in data for field in reusableTestRunFields):
      kind = event["type"]
    else:
      continue
    # 指令不同，結果就不能比
    if data.get("commit") and data.get("command") == command:
      candidates.append((kind, event))
  return candidates


# 合併後測試的結果欄位；逐一測沿用同一個 commit 的結果時照抄這些
mergeTestRecordFields = ("commit", "command", "passed", "failures", "qualityChecked", "qualityFailures", "tail")
# 背景跑完整測試、跑完要移除暫時 worktree 的 job
detachedTestKinds = ("baseline", "mergeTest")


class Conductor:
  def __init__(self, projectDir, config=None, out=print):
    self.projectDir = Path(projectDir).resolve()
    # 測試直接給設定；從設定檔讀的才每一輪重讀，你在儀表板換的模型才會生效
    self.watchConfig = config is None
    self.config = config or loadConfig(self.projectDir)
    self.out = out
    self.jobs = {}
    self.recovered = False
    self.notified = set()
    self.onlyPlansCache = {}
    # 啟動前的歷史事件不重印，終端機只顯示這次執行期間發生的事
    self.printedCount = len(readEvents(self.projectDir))
    self.roles = {role: self.roleLaunch(roleSpec(self.config, role)) for role in roleNames}

  def roleLaunch(self, spec):
    return {"spec": spec, "command": board_agents.resolveCommand(spec["command"], spec["cli"])}

  def refreshRoles(self):
    if not self.watchConfig:
      return
    try:
      userConfig = readUserConfig(self.projectDir)
    except (OSError, ValueError) as error:
      problems = [f"讀不懂：{error}"]
    else:
      problems = configProblems(userConfig) if isinstance(userConfig, dict) else ["最外層要是物件"]
    if problems:
      # 執行中把設定改壞時不停下來：繼續用上一份能用的設定，等你改好
      self.notifyOnce("警告｜.board/config.json 有問題，繼續用原本的寫程式、審查設定：" + "；".join(problems))
      return
    config = dict(defaultConfig)
    config.update(userConfig)
    if any(config[key] != self.config.get(key, defaultConfig[key]) for key in ("testCommand", "skipTests")):
      self.notifyOnce("測試設定已變更：testCommand／skipTests 需要重啟 Alfred 才會生效，"
                      "目前仍使用啟動時的測試指令與權限")
    for role in roleNames:
      spec = roleSpec(config, role)
      if spec != self.roles[role]["spec"]:
        # 換成新的物件、不改舊的：正在跑的 job 手上還拿著舊設定
        self.roles[role] = self.roleLaunch(spec)
        self.out(f"設定變更：{role} 改用 {roleChoiceText(spec['cli'], spec['model'], spec['reasoningEffort'])}，從下一個新啟動的 agent 開始用")

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
    # 主要 agent 優先；主要忙碌時，修正輪才優先沿用先前的子身分。
    return availableLane(lanes, busy, preferred)

  def tick(self):
    self.refreshRoles()
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
    if not state["paused"]:
      # 基準先拿測試名額：基準沒記下來前整份 plan 不派工
      self.runTestBaselines(state)
      plans, state = self.currentState()
      self.startTests(state)
    self.ensurePlanWorktrees(state)
    self.mergeReadyTasks(state)
    plans, state = self.currentState()
    self.completeFinishedPlans(state)
    if not state["paused"]:
      self.startMergeTests(state)
      plans, state = self.currentState()
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
      isTest = info["kind"] in ("test", *detachedTestKinds)
      finished = info["job"].poll() if isTest else info["job"].process.poll() is not None
      if recordInterruption and finished and info["kind"] not in detachedTestKinds:
        # 已經自己結束的 agent 可能寫好了成果，留給 collectFinishedJobs 照常處理，暫停時才不會丟掉
        # 基準與合併後測試不留：Ctrl+C 退出後沒有下一輪收集，worktree 會留著；丟掉結果只是之後重跑
        continue
      del self.jobs[key]
      info["job"].kill()
      info["job"].poll()
      if info["kind"] in detachedTestKinds:
        # Windows 上測試程式還沒完全結束時，它的工作目錄刪不掉；等背景執行緒收完再移除
        info["job"].wait(outputWaitAfterKillSeconds)
        self.removeDetachedWorktree(info)
      # 測試不記中斷：狀態留在測試中，繼續或重開 Alfred 後 startTests 會重跑；基準沒記下來，之後會從頭重跑
      elif recordInterruption and not isTest:
        self.event("taskInterrupted", info, stage=info["kind"])

  def stopJobsOfInactiveTasks(self, state):
    # 任務或整份 plan 被放棄後，正在跑的 agent 沒有意義了，直接結束、不留中斷紀錄
    keys = [key for key, info in self.jobs.items() if self.jobInactive(state, key, info)]
    self.stopJobs(keys, recordInterruption=False)

  def jobInactive(self, state, key, info):
    if info["kind"] in detachedTestKinds:
      return state["plans"].get(info["plan"], {}).get("status") != PlanStatus.active
    return state["tasks"].get(key, {}).get("status") == TaskStatus.abandoned

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
    appendEvent(self.projectDir, "paused",
                data={"reason": f"額度用完（{info['agent']}，{info['cli']}），用 board.py resume 繼續"})
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
      if info["kind"] == "baseline":
        self.finishTestBaseline(info)
        continue
      if info["kind"] == "mergeTest":
        self.finishMergeTest(info)
        continue
      try:
        if info["kind"] == "write":
          self.finishWrite(info)
        elif info["kind"] == "test":
          self.finishTests(state["plans"][info["plan"]], info)
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

  def needsTestBaseline(self, planState):
    command = self.config.get("testCommand", "")
    if not command or self.config.get("skipTests", False):
      return False
    baseline = planState["testBaseline"]
    # 基準要和任務測試用同一個指令才能比；testCommand 改掉後重開 Alfred 就要重跑
    return baseline is None or baseline.get("command") != command

  def runTestBaselines(self, state):
    candidates = None
    for planState in state["plans"].values():
      key = baselineKey(planState["id"])
      if planState["status"] != PlanStatus.active or key in self.jobs or not self.needsTestBaseline(planState):
        continue
      if candidates is None:
        # 有 plan 要基準時才讀整份事件紀錄
        candidates = baselineReuseCandidates(readEvents(self.projectDir), self.config["testCommand"])
      source = next(((kind, event) for kind, event in candidates
                     if self.onlyPlansChanged(event["data"]["commit"], planState["baseCommit"])), None)
      if source is not None:
        # 沿用不跑測試，不佔測試名額
        self.reuseTestBaseline(planState, *source)
      elif not self.waitsForRunningBaseline(planState) and self.freeTestSlots() > 0:
        self.startTestBaseline(planState, key)

  def freeTestSlots(self):
    # 基準、任務測試和合併後測試都是完整測試，合計不超過 maxParallelTests；名額滿了留到之後的 tick
    running = sum(1 for info in self.jobs.values() if info["kind"] in ("test", *detachedTestKinds))
    return self.config.get("maxParallelTests", defaultConfig["maxParallelTests"]) - running

  def onlyPlansChanged(self, fromCommit, toCommit):
    # tests/ 不讀 repo 的 plans/，只差 plan 檔時測試結果不會不同；commit 內容不會變，結果可以一直留著
    key = (fromCommit, toCommit)
    if key not in self.onlyPlansCache:
      try:
        paths = board_git.changedPaths(self.projectDir, fromCommit, toCommit)
      except Exception:
        # 例如來源的 commit 已經不在了：不能確定程式碼相同，就不沿用，照原本流程自己跑
        return False
      self.onlyPlansCache[key] = all(path.startswith("plans/") for path in paths)
    return self.onlyPlansCache[key]

  def waitsForRunningBaseline(self, planState):
    # 另一份 plan 正在跑程式碼相同的基準：這一輪先不啟動，等它記下來再沿用，不同時跑兩份完整測試
    return any(info["kind"] == "baseline" and info["plan"] != planState["id"]
               and self.onlyPlansChanged(info["baseCommit"], planState["baseCommit"])
               for info in self.jobs.values())

  def reuseTestBaseline(self, planState, kind, event):
    data = event["data"]
    # 不建立基準 worktree、不跑測試；commit 記這份 plan 自己的 baseCommit，來源另外記在 reusedFrom
    appendEvent(self.projectDir, "testBaseline", plan=planState["id"],
                data={"command": self.config["testCommand"], "commit": planState["baseCommit"],
                      "passed": bool(data.get("passed")), "failures": list(data.get("failures") or []),
                      "qualityChecked": bool(data.get("qualityChecked")),
                      "qualityFailures": list(data.get("qualityFailures") or []), "tail": data.get("tail", ""),
                      "reusedFrom": {"kind": kind, "plan": event.get("plan"), "task": event.get("task"),
                                     "commit": data["commit"]}})

  def startTestBaseline(self, planState, key):
    planIdValue, baseCommit = planState["id"], planState["baseCommit"]
    # 不在 plan worktree 跑：測試產物會留成未追蹤檔案，之後合併任務可能被擋下
    try:
      worktree = board_git.addBaselineWorktree(self.projectDir, planIdValue, baseCommit)
    except Exception as error:
      # 基準沒記下來前這份 plan 不派工；下一輪會自動再試
      self.notifyOnce(f"{planIdValue} 的基準測試 worktree 建立失敗：{error}")
      return
    # 基準可能跑好幾分鐘，在背景跑，期間才讀得到放棄、暫停的事件
    self.jobs[key] = {"kind": "baseline", "plan": planIdValue, "task": None, "agent": None, "baseCommit": baseCommit,
                      "worktree": worktree, "job": BackgroundTestJob(self.runTestCommand, worktree).start()}

  def finishTestBaseline(self, info):
    job = info["job"]
    try:
      if job.error is not None:
        raise job.error
      passed, tail, failures, qualityChecked, qualityFailures = job.result
      appendEvent(self.projectDir, "testBaseline", plan=info["plan"],
                  data={"command": self.config["testCommand"], "commit": info["baseCommit"], "passed": passed,
                        "failures": failures, "qualityChecked": qualityChecked, "qualityFailures": qualityFailures,
                        "tail": tail})
    except Exception as error:
      # plan 沒有「等你處理」狀態可以記；基準沒記下來，下一輪會自動重跑
      self.notifyOnce(f"{info['plan']} 的基準測試執行失敗：{error}")
    finally:
      self.removeBaselineWorktree(info["plan"])

  def removeBaselineWorktree(self, planIdValue):
    try:
      board_git.removeBaselineWorktree(self.projectDir, planIdValue)
    except Exception as error:
      # 結果已經記下來，不能因為移除失敗而每一輪重跑整套測試；下次建立前會先移除，cleanup 也會清掉
      self.notifyOnce(f"{planIdValue} 的基準測試 worktree 移除失敗：{error}")

  def removeMergeTestWorktree(self, planIdValue):
    try:
      board_git.removeMergeTestWorktree(self.projectDir, planIdValue)
    except Exception as error:
      # 下次建立前會先移除，cleanup 也會清掉
      self.notifyOnce(f"{planIdValue} 的合併後測試 worktree 移除失敗：{error}")

  def removeDetachedWorktree(self, info):
    if info["kind"] == "baseline":
      self.removeBaselineWorktree(info["plan"])
    else:
      self.removeMergeTestWorktree(info["plan"])

  def runsMergeTests(self, planState):
    return (planState["testTiming"] == "afterMerge" and bool(self.config.get("testCommand"))
            and not self.config.get("skipTests", False))

  def startMergeTests(self, state):
    for planState in state["plans"].values():
      key = mergeTestKey(planState["id"])
      if planState["status"] != PlanStatus.active or key in self.jobs or planState["testTiming"] != "afterMerge":
        continue
      if not self.runsMergeTests(planState):
        self.skipMergeTests(planState, state)
        continue
      if self.needsTestBaseline(planState):
        # 第一次合併後測試要跟基準比；testCommand 改掉後也要等新的基準
        continue
      tasks = [taskState for taskState in state["tasks"].values() if taskState["plan"] == planState["id"]]
      if any(taskState["status"] == TaskStatus.needsYou and (taskState["needs"] or {}).get("kind") == "mergeTest"
             for taskState in tasks):
        # 分不出結果的那次等你決定；先測後面的合併，範圍會包含它的改動，可能算錯是誰造成的
        continue
      pending = [taskState for taskState in tasks if taskState["status"] == TaskStatus.mergedUntested]
      if pending:
        try:
          self.startMergeTest(planState, pending, key)
        except Exception as error:
          # plan 沒有「等你處理」狀態可以記；下一輪會自動再試
          self.notifyOnce(f"{planState['id']} 的合併後測試無法開始：{error}")

  def skipMergeTests(self, planState, state):
    # 合併後才把測試關掉時，已合併待測試的任務沒有測試可等；記下來讓重播也得到同樣結果
    covers = [taskState["task"] for taskState in state["tasks"].values()
              if taskState["plan"] == planState["id"] and taskState["status"] == TaskStatus.mergedUntested]
    if covers:
      appendEvent(self.projectDir, "mergeTestSkipped", plan=planState["id"],
                  data={"covers": covers, "reason": "testCommand 沒有設定或 skipTests 為 true"})

  def mergeTestPredecessor(self, planState):
    lastTested = planState["mergeTest"]["lastTested"]
    # testCommand 改掉後，舊指令的結果不能拿來比，改跟重跑的基準比
    if lastTested and lastTested.get("command") == self.config["testCommand"]:
      return lastTested
    return planState["testBaseline"]

  def startMergeTest(self, planState, pending, key):
    planIdValue = planState["id"]
    commits = board_git.firstParentCommits(self.projectDir, planIdValue)
    position = {commit: index for index, commit in enumerate(commits)}
    pending = sorted(pending, key=lambda taskState: position.get(taskState["mergedCommit"], len(commits)))
    split = planState["mergeTest"]["split"]
    first = position.get(pending[0]["mergedCommit"], len(commits))
    splitUsable = bool(split) and split.get("command") == self.config["testCommand"] \
      and split.get("commit") in position and first <= position[split["commit"]]
    info = {"kind": "mergeTest", "plan": planIdValue, "task": None, "agent": None,
            "predecessor": self.mergeTestPredecessor(planState),
            "splitCommit": split["commit"] if splitUsable else None, "staleSplit": bool(split) and not splitUsable}
    if splitUsable:
      # 逐一測：照合併順序一次只測一個任務的合併 commit
      info["commit"], info["covers"] = pending[0]["mergedCommit"], [pending[0]["task"]]
    else:
      info["commit"], info["covers"] = commits[-1], [taskState["task"] for taskState in pending]
    info["mergeFailures"] = {taskState["task"]: list(taskState["mergeFailures"]) for taskState in pending}
    if splitUsable and info["commit"] == split["commit"]:
      # 這個 commit 就是一開始一起測的那次，結果已經有了，不必重跑
      self.writeMergeTestRun(info, {field: split.get(field) for field in mergeTestRecordFields}, reused=True)
      return
    if self.freeTestSlots() <= 0:
      # 名額滿了先不跑，任務維持已合併待測試，留到之後的 tick
      return
    worktree = board_git.addMergeTestWorktree(self.projectDir, planIdValue, info["commit"])
    info["job"] = BackgroundTestJob(self.runTestCommand, worktree).start()
    self.jobs[key] = info

  def finishMergeTest(self, info):
    job = info["job"]
    try:
      if job.error is not None:
        raise job.error
      passed, tail, failures, qualityChecked, qualityFailures = job.result
      record = {"commit": info["commit"], "command": self.config["testCommand"], "passed": passed,
                "failures": failures, "qualityChecked": qualityChecked, "qualityFailures": qualityFailures,
                "tail": tail}
      self.writeMergeTestRun(info, record, reused=False)
    except BoardError:
      raise
    except Exception as error:
      # 結果沒記下來，任務還是已合併待測試，下一輪會自動重跑
      self.notifyOnce(f"{info['plan']} 的合併後測試執行失敗：{error}")
    finally:
      self.removeMergeTestWorktree(info["plan"])

  def writeMergeTestRun(self, info, record, reused):
    # 結果在這裡算好寫進事件：之後重播事件時，結果才不會因為程式或設定改了而不同
    outcome = mergeTestOutcome(info["predecessor"], record, info["covers"], info["mergeFailures"],
                               info["splitCommit"], info["staleSplit"])
    appendEvent(self.projectDir, "mergeTestRun", plan=info["plan"],
                data={**record, "comparedTo": (info["predecessor"] or {}).get("commit"), "covers": info["covers"],
                      "reused": reused, **outcome})

  def startWrites(self, plans, state):
    for taskState in readyTasks(state):
      if taskKey(taskState["plan"], taskState["task"]) in self.jobs:
        continue
      if self.needsTestBaseline(state["plans"][taskState["plan"]]):
        # 基準還沒記下來（例如基準 worktree 建立失敗），之後任務的測試失敗就沒有東西可以比
        continue
      lane = self.freeLane("writer", "write", taskState["agent"])
      if lane is None:
        return
      self.startWrite(plans[taskState["plan"]], taskState, lane)

  def claudeAllowedTools(self, spec):
    allowedTools = list(spec["allowedTools"])
    testCommand = self.config.get("testCommand", "")
    if testCommand:
      # 只開放專案的測試指令，Claude 才能自我驗證，又不會拿到其他 shell 權限；Windows 上 shell 工具是 PowerShell
      powershell = self.config.get("writerTestCommand", testCommand)
      allowedTools += [f"Bash({testCommand}*)", f"PowerShell({powershell}*)"]
    if self.config.get("tddCrap"):
      # tdd-crap 的「先寫測試、確認失敗」要能只跑幾個測試，每次都跑完整品質檢查太慢
      python = self.config.get("pythonCommand", "python")
      powershell = self.config.get("writerPythonCommand", python)
      allowedTools += [f"Bash({python} -m pytest*)", f"PowerShell({powershell} -m pytest*)"]
    return allowedTools

  def startWrite(self, plan, taskState, lane):
    launch = self.roles["writer"]
    cli, model = launch["spec"]["cli"], launch["spec"]["model"]
    info = {"kind": "write", "plan": taskState["plan"], "task": taskState["task"], "round": taskState["round"],
            "agent": lane, "text": taskState["text"], "mode": "resolve" if taskState["resolving"] else "write",
            "cli": cli, "model": model, "launch": launch, "afterMerge": taskState["testAfterMerge"]}
    # 換了 CLI，上一輪的對話接不回來；換了模型，不確定接續時能不能換，所以也開新對話
    resume, info["sessionId"] = self.writerSession(taskState, cli, model)
    cleanRemerge = False
    try:
      worktree = board_git.ensureTaskWorktree(self.projectDir, info["plan"], info["task"])
      if taskState["needsClean"]:
        board_git.cleanWorktree(worktree)
      if taskState["mergeFix"]:
        # 合併後測試退回：從最新的 plan 分支修，才重現得了跟其他任務合在一起的失敗
        board_git.fastForwardTaskToPlan(self.projectDir, info["plan"], info["task"])
      if info["mode"] == "resolve":
        info["conflicts"] = board_git.mergePlanIntoTask(self.projectDir, info["plan"], info["task"],
                                                        f"{info['plan']} {info['task']} 合併最新的 plan 分支")
        cleanRemerge = not info["conflicts"]
      if not cleanRemerge:
        prompt = self.writePrompt(plan, taskState, info)
        argv, stdinText, lineParser = self.writeLaunch(info, worktree, prompt, resume)
        info["job"] = board_agents.AgentJob(argv, worktree, stdinText, self.logPath(info, "write"), lineParser,
                                            self.timeoutSeconds(), env=board_agents.compactionEnv(cli, launch["spec"]["contextWindow"])).start()
    except Exception as error:
      # 例如核准後 plan 檔被改掉這個任務；接住所有例外，否則同一個任務每一輪都會擋住其他任務
      self.event("agentFailed", info, stage="write", reason=f"無法啟動：{type(error).__name__}: {error}")
      return
    info["worktree"] = worktree
    if cleanRemerge:
      # 最新的 plan 分支合併進來沒有衝突（例如你已經在任務的 worktree 處理過），不必叫寫程式的 agent，直接測試、審查
      info["agent"] = None
      self.event("taskWritten", info, commit=board_git.headCommit(worktree), summary="合併最新的 plan 分支，沒有衝突",
                 mode="resolve", conflicts=[], testing=self.testsBeforeReview(info))
      return
    # 先登記再寫事件：事件寫入失敗時 agent 不會變成沒人管的孤兒
    key = taskKey(info["plan"], info["task"])
    self.jobs[key] = info
    try:
      self.event("taskStarted", info, worktree=str(worktree), sessionId=info["sessionId"], resume=resume,
                 mode=info["mode"], cli=cli, model=model, reasoningEffort=launch["spec"]["reasoningEffort"])
    except Exception:
      self.jobs.pop(key, None)
      info["job"].kill()
      raise

  def testsBeforeReview(self, info):
    # 合併後才測試的 plan 寫完直接送審；原流程有測試指令時先進測試中
    return bool(self.config["testCommand"]) and not info.get("afterMerge")

  def verifyAfterMerge(self, taskState):
    # 你選「完成」的任務照原流程直接完成，不再跑合併後測試
    return (taskState["testAfterMerge"] and bool(self.config.get("testCommand"))
            and not self.config.get("skipTests", False) and not taskState["userDone"])

  def writerSession(self, taskState, cli, model):
    resume = bool(taskState["resumable"] and taskState["sessionId"] and taskState["sessionCli"] == cli
                  and taskState["sessionModel"] == model)
    # Claude 可以先指定對話編號；Codex 的要等它跑起來，從輸出取得。
    sessionId = taskState["sessionId"] if resume else (str(uuid.uuid4()) if cli == "claude" else None)
    return resume, sessionId

  def writerCommands(self, cli):
    testCommand, pythonCommand = self.config["testCommand"], self.config.get("pythonCommand", "python")
    if cli == "claude":
      # Claude 用 Bash 工具跑測試；writer* 是給 PowerShell 的 & "..." 寫法，Claude Code 的允許規則比對不到
      return testCommand, pythonCommand
    return self.config.get("writerTestCommand", testCommand), self.config.get("writerPythonCommand", pythonCommand)

  def writePrompt(self, plan, taskState, info):
    testCommand, pythonCommand = self.writerCommands(info["cli"])
    if info["mode"] == "resolve":
      return buildResolvePrompt(plan, info["task"], info["round"], info["conflicts"], taskState["userNote"],
                                testCommand, self.config.get("skipTests", False), afterMerge=info["afterMerge"])
    return buildWritePrompt(plan, info["task"], info["round"], self.readReview(taskState["lastReviewNotes"]),
                            taskState["userNote"], testCommand if self.config.get("tddCrap") else "", info["cli"],
                            testCommand, self.config.get("skipTests", False), self.config.get("tddSkill"), pythonCommand,
                            lastTest=taskState["lastTest"], afterMerge=info["afterMerge"])

  def writeLaunch(self, info, worktree, prompt, resume):
    launch = info["launch"]
    if info["cli"] == "claude":
      argv = board_agents.buildClaudeArgv(launch["command"], info["sessionId"], resume, writerRules("claude"),
                                          self.claudeAllowedTools(launch["spec"]), info["model"], launch["spec"]["reasoningEffort"],
                                          contextWindow=launch["spec"]["contextWindow"])
      return argv, prompt, board_agents.parseClaudeLine
    schemaPath = self.codexFiles(info, "write-schema.json", writeResultSchema,
                                 f"{info['plan']}-{info['task']}-r{info['round']}-write.json")
    argv = board_agents.buildCodexWriteArgv(launch["command"], worktree, schemaPath, info["outPath"],
                                            info["sessionId"] if resume else None, info["model"], launch["spec"]["reasoningEffort"],
                                            contextWindow=launch["spec"]["contextWindow"])
    # Codex 沒有附加系統提示的參數，規則放在 prompt 最前面
    return argv, writerRules("codex") + "\n\n" + prompt, board_agents.parseCodexLine

  def codexFiles(self, info, schemaName, schema, outName):
    runDir = boardDir(self.projectDir) / "runs"
    runDir.mkdir(parents=True, exist_ok=True)
    schemaPath = runDir / schemaName
    schemaPath.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    info["outPath"] = runDir / outName
    # 舊的輸出檔留著的話，這次 Codex 沒寫出結果也會被誤讀成成功
    info["outPath"].unlink(missing_ok=True)
    return schemaPath

  def finalResult(self, info):
    if info["cli"] == "claude":
      return board_agents.claudeFinalResult(info["job"].activity)
    return board_agents.codexFinalResult(info["outPath"])

  def finishWrite(self, info):
    job = info["job"]
    if job.killed and not job.timedOut:
      self.event("taskInterrupted", info, stage="write")
      return
    result = self.writeResult(info, job)
    if result is None:
      return
    # 下一輪要接續這次的對話；Codex 的對話編號到這時才知道
    session = {"sessionId": info["sessionId"] or job.activity.get("threadId"), "cli": info["cli"],
               "model": info["model"]}
    if result["status"] == "blocked":
      self.event("taskBlocked", info, reason=result["blockedReason"], **session)
      return
    commit = self.commitWrite(info)
    if commit is None:
      return
    # 有測試指令時先進測試中，由 startTests 在背景跑，不擋其他任務
    self.event("taskWritten", info, commit=commit, summary=result["summary"], mode=info["mode"],
               conflicts=info.get("conflicts", []), testing=self.testsBeforeReview(info), **session)

  def writeResult(self, info, job):
    """寫程式 agent 的有效回覆；拿不到時已經記下事件，回傳 None。"""
    result, error = self.finalResult(info)
    if result is None:
      # 額度用完不是任務的問題，不能消耗重試次數；整個暫停，等你恢復額度後 resume
      if self.quotaSuspect(job, error):
        self.pauseForQuota(info, "write")
        return None
      reason = "超時" if job.timedOut else (error or f"結束代碼 {job.exitCode}")
      self.event("agentFailed", info, stage="write", reason=f"{reason}｜{self.failureTail(job)}")
      return None
    errors = validateWriteResult(result)
    if errors:
      self.event("agentFailed", info, stage="write", reason="回覆格式不符：" + "；".join(errors))
      return None
    return result

  def commitWrite(self, info):
    """commit 這一輪的改動；不能 commit 時已經記下事件，回傳 None。"""
    try:
      if info["mode"] == "resolve":
        remaining = board_git.filesWithConflictMarkers(info["worktree"], info["conflicts"])
        if remaining:
          # 標記還在就 commit 會把壞掉的檔案送去審查；當成這次失敗，重試時會清乾淨重新合併
          self.event("agentFailed", info, stage="write", reason="還有沒解掉的衝突標記：" + "、".join(remaining))
          return None
        subject = "解合併衝突（" + "、".join(info["conflicts"]) + "）"
      elif not board_git.hasChanges(info["worktree"]):
        self.event("needsYou", info, kind="noChanges", reason="寫程式的 agent 回報完成，但沒有任何改動")
        return None
      else:
        subject = info["text"]
      return board_git.commitAll(info["worktree"], f"{info['plan']} {info['task']} 第{info['round']}輪：{subject}")
    except board_git.GitError as error:
      # 例如專案的 pre-commit hook 失敗；不記事件的話任務會永遠停在「寫程式中」
      self.event("needsYou", info, kind="commitFailed", reason=f"commit 失敗：{error}")
      return None

  def startTests(self, state):
    if not self.config["testCommand"]:
      return
    # 測試不佔寫程式、審查的名額；同時跑的完整測試數由 maxParallelTests 限制，等待中的任務狀態維持測試中、不寫事件
    waiting = [taskState for taskState in tasksWithStatus(state, TaskStatus.testing)
               if taskKey(taskState["plan"], taskState["task"]) not in self.jobs
               and not self.needsTestBaseline(state["plans"][taskState["plan"]])]
    slots = self.freeTestSlots()
    if not waiting or slots <= 0:
      return
    for taskState in self.inTestingOrder(waiting)[:slots]:
      self.startTest(taskState)

  def inTestingOrder(self, tasks):
    # 先進測試中的先測：看最後一次 taskWritten 在事件紀錄裡的位置，時間只到秒，同一秒寫完的也分得出先後
    written = {}
    for index, event in enumerate(readEvents(self.projectDir)):
      if event["type"] == "taskWritten":
        written[taskKey(event.get("plan"), event.get("task"))] = index
    return sorted(tasks, key=lambda taskState: written.get(taskKey(taskState["plan"], taskState["task"]), -1))

  def startTest(self, taskState):
    info = {"kind": "test", "plan": taskState["plan"], "task": taskState["task"], "round": taskState["round"],
            "agent": taskState["agent"]}
    worktree = board_git.taskWorktreePath(self.projectDir, info["plan"], info["task"])
    # 記下被測的 commit 和指令：之後程式碼相同時才能沿用這次的結果
    info["commit"] = board_git.headCommit(worktree)
    info["command"] = self.config["testCommand"]
    info["job"] = BackgroundTestJob(self.runTestCommand, worktree).start()
    self.jobs[taskKey(info["plan"], info["task"])] = info

  def finishTests(self, planState, info):
    job = info["job"]
    if job.error is not None:
      raise job.error
    passed, tail, failures, qualityChecked, qualityFailures = job.result
    # verdict 在這裡算好寫進事件：之後重播事件時，結果才不會因為基準或輸出不同而改變
    verdict, newFailures = compareWithBaseline(planState["testBaseline"], passed, failures, qualityChecked, qualityFailures)
    self.event("testRun", info, passed=passed, tail=tail, verdict=verdict, newFailures=newFailures,
               qualityChecked=qualityChecked, qualityFailures=qualityFailures, commit=info["commit"],
               failures=failures, command=info["command"])

  def runTestCommand(self, cwd, onStarted=None):
    command = self.config["testCommand"]
    # 不用 subprocess.run：Windows 上逾時只會結束 cmd.exe，孫行程還握著管線，communicate 會一直等
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               encoding="utf-8", errors="replace", **options)
    if onStarted:
      # 背景測試要拿到程式才能在暫停、放棄時結束整個程式樹
      onStarted(process)
    try:
      output, _ = process.communicate(timeout=self.timeoutSeconds())
      passed = process.returncode == 0
      failures = [] if passed else failureNames(output)
      qualityChecked, qualityFailures = qualityResults(output)
    except subprocess.TimeoutExpired:
      # 超時的輸出只有一部分，裡面的失敗名稱不完整，當成分不出名稱
      failures = []
      qualityChecked, qualityFailures = False, []
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
    return passed, "\n".join(output.splitlines()[-50:]), failures, qualityChecked, qualityFailures

  def startReviews(self, plans, state):
    for taskState in tasksWithStatus(state, TaskStatus.toReview):
      if taskKey(taskState["plan"], taskState["task"]) in self.jobs:
        continue
      if self.needsTestBaseline(state["plans"][taskState["plan"]]):
        # 審查也是派工：基準還沒用目前的 testCommand 記下來前不送審
        continue
      lane = self.freeLane("reviewer", "review", taskState["reviewAgent"])
      if lane is None:
        return
      self.startReview(plans[taskState["plan"]], taskState, lane)

  def startReview(self, plan, taskState, lane, previousErrors=None, attempt=1, launch=None):
    # 格式不符的重審沿用第一次的設定：儀表板照 reviewStarted 記的 CLI 解析 log，中途換了設定也一致
    launch = launch or self.roles["reviewer"]
    info = {"kind": "review", "plan": taskState["plan"], "task": taskState["task"], "round": taskState["round"],
            "agent": lane, "attempt": attempt, "cli": launch["spec"]["cli"], "model": launch["spec"]["model"],
            "launch": launch}
    try:
      worktree = board_git.taskWorktreePath(self.projectDir, info["plan"], info["task"])
      diff = board_git.taskDiff(self.projectDir, info["plan"], info["task"])
      resolvedConflicts = taskState["conflictFiles"] if taskState["lastWriteMode"] == "resolve" else None
      afterMerge = taskState["testAfterMerge"] and bool(self.config.get("testCommand"))         and not self.config.get("skipTests", False)
      # 合併後才測試的 plan 審查時還沒有完整測試結果；lastTest 是上一次合併後測試，不是這次改動的結果
      testRun = None if taskState["testAfterMerge"] else taskState["lastTest"]
      prompt = buildReviewPrompt(plan, info["task"], info["round"], diff, testRun, previousErrors,
                                 resolvedConflicts, bool(self.config.get("tddCrap")),
                                 self.preexistingFailures(taskState), afterMerge=afterMerge,
                                 mergeFailures=taskState["mergeFailures"])
      argv, stdinText, lineParser = self.reviewLaunch(info, worktree, prompt)
      info["job"] = board_agents.AgentJob(argv, worktree, stdinText, self.logPath(info, f"review{attempt}"),
                                          lineParser, self.timeoutSeconds(),
                                          env=board_agents.compactionEnv(info["cli"], launch["spec"]["contextWindow"])).start()
    except Exception as error:
      self.event("agentFailed", info, stage="review", reason=f"無法啟動：{type(error).__name__}: {error}")
      return
    # 先登記再寫事件：事件寫入失敗時 agent 不會變成沒人管的孤兒
    key = taskKey(info["plan"], info["task"])
    self.jobs[key] = info
    try:
      if attempt == 1:
        self.event("reviewStarted", info, cli=info["cli"], model=info["model"], reasoningEffort=launch["spec"]["reasoningEffort"])
    except Exception:
      self.jobs.pop(key, None)
      info["job"].kill()
      raise

  def preexistingFailures(self, taskState):
    if (taskState["lastTest"] or {}).get("verdict") != "preexisting":
      return []
    baseline = self.currentState()[1]["plans"][taskState["plan"]]["testBaseline"] or {}
    return (baseline.get("failures") or []) + (baseline.get("qualityFailures") or [])

  def reviewLaunch(self, info, worktree, prompt):
    command = info["launch"]["command"]
    if info["cli"] == "claude":
      argv = board_agents.buildClaudeReviewArgv(command, reviewerRules, info["model"], info["launch"]["spec"]["reasoningEffort"],
                                                contextWindow=info["launch"]["spec"]["contextWindow"])
      return argv, prompt, board_agents.parseClaudeLine
    schemaPath = self.codexFiles(info, "review-schema.json", reviewResultSchema,
                                 f"{info['plan']}-{info['task']}-r{info['round']}-review{info['attempt']}.json")
    argv = board_agents.buildCodexArgv(command, worktree, schemaPath, info["outPath"], info["model"],
                                     info["launch"]["spec"]["reasoningEffort"], contextWindow=info["launch"]["spec"]["contextWindow"])
    return argv, reviewerRules + "\n\n" + prompt, board_agents.parseCodexLine

  def finishReview(self, plan, taskState, info):
    job = info["job"]
    if job.killed and not job.timedOut:
      self.event("taskInterrupted", info, stage="review")
      return
    result, error = self.finalResult(info)
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
        self.startReview(plan, taskState, info["agent"], previousErrors=errors, attempt=2, launch=info["launch"])
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
    if self.settleUserDoneWorktree(taskState, info):
      return
    if board_git.isMerged(self.projectDir, planIdValue, taskId):
      # 你已經自己合併過（例如處理完衝突），只要記下來就好
      self.recordMergedByUser(taskState, info)
      return
    merged, commit, conflicts = board_git.mergeTask(self.projectDir, planIdValue, taskId,
                                                    f"合併 {planIdValue} {taskId}：{taskState['text']}")
    if merged:
      self.event("taskMerged", info, commit=commit, verify=self.verifyAfterMerge(taskState))
    else:
      self.recordMergeConflict(taskState, info, conflicts)

  def settleUserDoneWorktree(self, taskState, info):
    """你按完成的任務先把 worktree 收乾淨再合併；回傳 True 代表這個 tick 不再往下合併。"""
    planIdValue, taskId = taskState["plan"], taskState["task"]
    worktree = board_git.taskWorktreePath(self.projectDir, planIdValue, taskId)
    if not (taskState["userDone"] and worktree.exists()):
      return False
    try:
      if board_git.mergeInProgress(worktree):
        # 解衝突途中停下時，任務 worktree 留著還沒結束的合併；直接 commit 會把衝突標記帶進 plan 分支
        if board_git.isMerged(self.projectDir, planIdValue, taskId):
          # 你在 plan worktree 解完了，任務 worktree 裡解到一半的合併已經用不到
          self.recordMergedByUser(taskState, info)
          return True
        files = sorted(set(board_git.unmergedFiles(worktree)) | set(taskState["conflictFiles"]))
        remaining = board_git.filesWithConflictMarkers(worktree, files)
        if remaining:
          self.event("needsYou", info, kind="mergeConflict", conflicts=remaining,
                     reason=f"任務的 worktree 還有沒解掉的衝突標記：{'、'.join(remaining)}")
          return True
      if board_git.hasChanges(worktree):
        board_git.commitAll(worktree, f"{planIdValue} {taskId} 使用者手動修改")
    except board_git.GitError as error:
      self.event("needsYou", info, kind="commitFailed", reason=f"commit 失敗：{error}")
      return True
    return False

  def recordMergedByUser(self, taskState, info):
    commit = board_git.runGit(self.projectDir, "rev-parse", board_git.planBranch(taskState["plan"]))
    self.event("taskMerged", info, commit=commit, verify=self.verifyAfterMerge(taskState))

  def recordMergeConflict(self, taskState, info, conflicts):
    if taskState["conflictCount"] < maxAutoResolves:
      self.event("taskConflicted", info, conflicts=conflicts, reason="、".join(conflicts))
      return
    reason = "合併衝突：" + "、".join(conflicts)
    if taskState["conflictCount"]:
      reason += f"（已自動解過 {taskState['conflictCount']} 次）"
    self.event("needsYou", info, kind="mergeConflict", reason=reason, conflicts=conflicts)

  def completeFinishedPlans(self, state):
    for planState in state["plans"].values():
      if planState["status"] == PlanStatus.active and isPlanFinished(state, planState["id"]):
        appendEvent(self.projectDir, "planCompleted", plan=planState["id"])

  def runForever(self):
    self.out(f"Alfred 啟動：{self.projectDir}（Ctrl+C 結束）")
    try:
      while True:
        self.safeTick()
        time.sleep(self.config["pollSeconds"])
    except KeyboardInterrupt:
      self.out("Alfred 結束中，正在停止所有 agent…")
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
    print(f"{projectDir} 不是 git repo，Alfred 需要用 git 建立 worktree")
    return 1
  try:
    problems = configProblems(readUserConfig(projectDir), requireTestChoice=True)
  except (OSError, ValueError) as error:
    problems = [f"讀不懂：{error}"]
  if problems:
    print(".board/config.json 要先修正：")
    for problem in problems:
      print("  " + problem)
    return 1
  # 兩個 conductor 同時跑時，後開的會把前一個正在跑的任務當成中斷並清掉 worktree
  if not acquireConductorLock(projectDir):
    print(f"這個專案已經有 Alfred 在跑（PID {readLockPid(projectDir)}），不能同時跑兩個")
    return 1
  try:
    Conductor(projectDir).runForever()
  finally:
    releaseConductorLock(projectDir)
  return 0


if __name__ == "__main__":
  sys.exit(main())
