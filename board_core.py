"""事件紀錄與狀態推導：所有程式都透過這裡讀寫 .board/。"""
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from rtm_graph import analyzePlan, asList, planId

boardDirName = ".board"
eventsFileName = "events.jsonl"
lockFileName = "events.lock"
roleNames = ("writer", "reviewer")
cliNames = ("claude", "codex")
defaultContextWindows = {"claude": 200000, "codex": 475000}
cliTitles = {"claude": "Claude", "codex": "Codex"}
# 模型名稱會變成命令列參數：Windows 上的 codex 是 codex.cmd，參數會經過 cmd.exe，& | 之類的字元可能被當成另一個指令；
# 開頭限定英數字，免得被當成 CLI 的其他參數（例如 -p）
modelNamePattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,99}")
modelNameRule = "模型名稱要以英數字開頭，只能有英數字和 . _ - : / [ ]，最多 100 字"
reasoningEffortPattern = re.compile(r"[a-z][a-z0-9_-]{0,31}")
configReplaceAttempts = 20
# 舊版設定的 key 和新寫法；conductor 看到舊的就拒絕啟動，免得設定被默默忽略
legacyConfigKeys = {
  "claudeCommand": "writer.command",
  "codexCommand": "reviewer.command",
  "claudeAllowedTools": "writer.allowedTools",
  "claudeContextWindow": "writer.contextWindow",
  "codexContextWindow": "reviewer.contextWindow",
}
defaultConfig = {
  "maxParallel": 3,
  "maxRounds": 3,
  "timeoutMinutes": 30,
  "testCommand": "",
  "skipTests": False,
  "pollSeconds": 2,
  "writer": {"cli": "claude"},
  "reviewer": {"cli": "codex"},
  "tddCrap": False,
}


class BoardError(Exception):
  pass


def boardDir(projectDir):
  return Path(projectDir) / boardDirName


def configPath(projectDir):
  return boardDir(projectDir) / "config.json"


def readUserConfig(projectDir):
  path = configPath(projectDir)
  if not path.exists():
    return {}
  return json.loads(path.read_text(encoding="utf-8-sig"))


def loadConfig(projectDir):
  config = dict(defaultConfig)
  config.update(readUserConfig(projectDir))
  return config


def roleSpec(config, role):
  raw = config.get(role)
  # 寫錯格式時 configProblems 會擋下 conductor；這裡只求不當掉，儀表板才看得到警告
  raw = raw if isinstance(raw, dict) else {}
  cli = raw.get("cli") or defaultConfig[role]["cli"]
  allowedTools = raw.get("allowedTools")
  return {"cli": cli, "model": raw.get("model") or None, "reasoningEffort": raw.get("reasoningEffort") or None,
          "command": raw.get("command") or None,
          "contextWindow": raw.get("contextWindow") or (defaultContextWindows.get(cli) if isinstance(cli, str) else None),
          # 字串不能用 list() 拆成一個個字元，其他型別也只求不當掉；configProblems 會回報
          "allowedTools": list(allowedTools) if isinstance(allowedTools, list) else []}


def configProblems(userConfig, requireTestChoice=False):
  if not isinstance(userConfig, dict):
    return ["最外層要是物件"]
  problems = [f"{key} 已經不用了，請改寫成 {replacement}（見 README 的「換模型」）"
              for key, replacement in legacyConfigKeys.items() if key in userConfig]
  for role in roleNames:
    if role in userConfig:
      problems.extend(roleConfigProblems(role, userConfig[role]))
  return problems + testConfigProblems(userConfig, requireTestChoice)


def testConfigProblems(config, requireChoice=False):
  command = config.get("testCommand", "")
  skip = config.get("skipTests", False)
  if not isinstance(command, str):
    return ["testCommand 要是指令字串"]
  if not isinstance(skip, bool):
    return ["skipTests 要是 true 或 false"]
  if skip and command:
    return ["testCommand 和 skipTests: true 不能同時設定"]
  if skip and config.get("tddCrap"):
    return ["TDD 不能略過測試：tddCrap 和 skipTests: true 不能同時設定"]
  if requireChoice and not skip and not command.strip():
    return ["尚未設定測試方式：執行 board.py init --test-command \"你的測試指令\"，"
            "或用 init --skip-tests 明確略過測試；Python 專案也可用 init --tdd"]
  return []


def roleConfigProblems(role, raw):
  if not isinstance(raw, dict):
    return [f'{role} 要寫成物件，例如 {{"cli": "claude", "model": "opus"}}']
  problems = []
  cli = raw.get("cli", defaultConfig[role]["cli"])
  if cli not in cliNames:
    problems.append(f"{role}.cli 只能是 claude 或 codex，現在是 {cli}")
  if raw.get("model") is not None and not isinstance(raw["model"], str):
    problems.append(f'{role}.model 要是文字，例如 "opus"')
  if not validReasoningEffort(raw.get("reasoningEffort")):
    problems.append(f'{role}.reasoningEffort 要是 CLI 支援的推理強度文字，例如 "high"')
  return problems + roleToolProblems(role, raw)


def roleToolProblems(role, raw):
  problems = []
  examples = {"command": '["C:/tools/claude.exe"]', "allowedTools": '["Bash(npm install*)"]'}
  for key, example in examples.items():
    value = raw.get(key)
    if value is None:
      continue
    valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
    if not valid or (key == "command" and not value):
      problems.append(f'{role}.{key} 要是文字陣列，例如 {example}')
  return problems


def roleChoiceText(cli, model, reasoningEffort=None):
  effortText = f"・推理強度 {reasoningEffort}" if reasoningEffort else ""
  return f"{cliTitles.get(cli, cli)}（{model or '預設模型'}）{effortText}"


def writeUserConfig(projectDir, userConfig):
  path = configPath(projectDir)
  path.parent.mkdir(parents=True, exist_ok=True)
  tempPath = path.with_name(path.name + ".tmp")
  tempPath.write_text(json.dumps(userConfig, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
  # conductor 每一輪都會讀設定檔；先寫暫存檔再整個換掉，它才不會讀到寫一半的內容
  for _ in range(configReplaceAttempts):
    try:
      os.replace(tempPath, path)
      return
    except PermissionError:
      # Windows 上別的程式剛好開著設定檔時換不掉，等一下再試
      time.sleep(0.05)
  tempPath.unlink(missing_ok=True)
  raise BoardError("設定檔正被其他程式使用，請稍後再試")


def validReasoningEffort(value):
  return value is None or (isinstance(value, str) and (not value or bool(reasoningEffortPattern.fullmatch(value))))


def validateRoleChoice(role, cli, model, reasoningEffort):
  if role not in roleNames:
    raise BoardError(f"角色只能是 writer 或 reviewer，現在是 {role}")
  if cli not in cliNames:
    raise BoardError(f"CLI 只能是 claude 或 codex，現在是 {cli}")
  if model and not modelNamePattern.fullmatch(model):
    raise BoardError(modelNameRule)
  if not validReasoningEffort(reasoningEffort):
    raise BoardError("推理強度要是 CLI 支援的名稱，例如 high")


def saveRoleChoice(projectDir, role, cli, model, reasoningEffort=None):
  """更新角色的 CLI、模型與推理強度，其他設定原樣保留。"""
  validateRoleChoice(role, cli, model, reasoningEffort)
  try:
    userConfig = readUserConfig(projectDir)
  except (OSError, ValueError) as error:
    raise BoardError(f".board/config.json 讀不懂，請先手動修正：{error}")
  if not isinstance(userConfig, dict):
    raise BoardError(".board/config.json 最外層要是物件，請先手動修正")
  current = userConfig.get(role)
  roleConfig = dict(current) if isinstance(current, dict) else {}
  # 自訂的 command 指向某一個 CLI 的執行檔；換了 CLI 還用它，啟動時會帶錯參數
  if roleConfig.get("command") and cli != roleConfig.get("cli", defaultConfig[role]["cli"]):
    raise BoardError(f"{role} 有自訂 command，換 CLI 前要先到 .board/config.json 改 command")
  roleConfig["cli"] = cli
  for key, value in (("model", model), ("reasoningEffort", reasoningEffort)):
    if value:
      roleConfig[key] = value
    else:
      roleConfig.pop(key, None)
  userConfig[role] = roleConfig
  writeUserConfig(projectDir, userConfig)


def nowText():
  return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def fileLock(lockPath, timeoutSeconds=10, staleSeconds=60):
  # 用「建立檔案成功才算拿到鎖」，Windows 和其他系統行為一致，不必依賴平台專屬的鎖
  deadline = time.time() + timeoutSeconds
  while True:
    try:
      os.close(os.open(lockPath, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
      break
    # Windows 上若別的執行緒正在刪除鎖檔，O_EXCL 會丟 PermissionError 而不是 FileExistsError，要當成「鎖被占用」重試
    except (FileExistsError, PermissionError):
      try:
        # 拿著鎖的程式當掉時鎖檔會留著，放太久就視為失效
        if time.time() - os.path.getmtime(lockPath) > staleSeconds:
          os.remove(lockPath)
          continue
      except (FileNotFoundError, PermissionError):
        # 鎖檔剛被別人移除，或連鎖檔都建不出來（例如唯讀），都必須走逾時檢查，否則會無限忙迴圈
        pass
      if time.time() > deadline:
        raise BoardError(f"等不到事件紀錄的鎖：{lockPath}")
      time.sleep(0.02)
  try:
    yield
  finally:
    try:
      os.remove(lockPath)
    except FileNotFoundError:
      pass


def appendEvent(projectDir, eventType, plan=None, task=None, agent=None, roundNumber=None, data=None):
  directory = boardDir(projectDir)
  directory.mkdir(parents=True, exist_ok=True)
  event = {"time": nowText(), "plan": plan, "task": task, "type": eventType,
           "agent": agent, "round": roundNumber, "data": data or {}}
  line = json.dumps(event, ensure_ascii=False) + "\n"
  with fileLock(directory / lockFileName):
    with open(directory / eventsFileName, "a", encoding="utf-8", newline="\n") as handle:
      handle.write(line)
      handle.flush()
      os.fsync(handle.fileno())
  return event


def readEvents(projectDir):
  path = boardDir(projectDir) / eventsFileName
  if not path.exists():
    return []
  events = []
  for line in path.read_text(encoding="utf-8").splitlines():
    # 只有寫到一半當機才會出現殘缺的行，略過它比讓整個流程停下來好
    try:
      events.append(json.loads(line))
    except json.JSONDecodeError:
      continue
  return events


def loadPlans(projectDir, errors=None):
  plansDir = Path(projectDir) / "plans"
  plans = {}
  if plansDir.exists():
    for planPath in sorted(plansDir.glob("*.md")):
      # 一份存錯編碼或讀不到的 plan 不能讓所有指令和 conductor 一起停擺，略過它並回報
      try:
        plan = analyzePlan(planPath)
      except Exception as error:
        if errors is not None:
          errors.append(f"{planPath.name} 無法讀取（{type(error).__name__}），已略過：{error}")
        continue
      plans[planId(plan)] = plan
  return plans


class PlanStatus:
  draft = "草稿"
  pending = "待核准"
  rejected = "退回"
  active = "進行中"
  completed = "完成"
  abandoned = "放棄"
  superseded = "被取代"


class TaskStatus:
  queued = "排隊中"
  writing = "寫程式中"
  testing = "測試中"
  toReview = "待審查"
  reviewing = "審查中"
  merging = "合併中"
  done = "完成"
  needsYou = "等你處理"
  abandoned = "放棄"


def taskKey(planIdValue, taskId):
  return f"{planIdValue}/{taskId}"


def newTaskState(planIdValue, task, config):
  return {
    "plan": planIdValue, "task": task["id"], "text": task["text"],
    "dependsOn": list(task["dependsOn"]), "requirementIds": list(task["requirementIds"]),
    "status": TaskStatus.queued, "round": 1, "maxRounds": config["maxRounds"],
    "agent": None, "reviewAgent": None, "sessionId": None, "sessionCli": None, "sessionModel": None,
    "resumable": False, "reviewCli": None, "reviewModel": None,
    "needsClean": False,
    "failStreak": 0, "needs": None, "userNote": "", "userDone": False,
    "reviews": [], "lastReviewNotes": None, "lastCommit": None, "lastTest": None,
    "resolving": False, "conflictFiles": [], "conflictCount": 0, "lastWriteMode": "write",
  }


def planTasks(state, planIdValue):
  return [taskState for taskState in state["tasks"].values() if taskState["plan"] == planIdValue]


def onPlanSubmitted(state, planState, event, config):
  planState["status"] = PlanStatus.pending
  planState["rejectReason"] = ""


def onPlanRejected(state, planState, event, config):
  planState["status"] = PlanStatus.rejected
  planState["rejectReason"] = event["data"].get("reason", "")


def onPlanApproved(state, planState, event, config):
  planState["status"] = PlanStatus.active
  planState["baseCommit"] = event["data"].get("baseCommit")
  for task in event["data"].get("tasks", []):
    state["tasks"][taskKey(planState["id"], task["id"])] = newTaskState(planState["id"], task, config)


def onTestBaseline(state, planState, event, config):
  # 只留最新一次：testCommand 改掉後重跑的基準才是之後比對的依據
  planState["testBaseline"] = event["data"]


def onPlanAbandoned(state, planState, event, config):
  planState["status"] = PlanStatus.abandoned


def onPlanCompleted(state, planState, event, config):
  planState["status"] = PlanStatus.completed


planEventHandlers = {
  "planSubmitted": onPlanSubmitted,
  "planRejected": onPlanRejected,
  "planApproved": onPlanApproved,
  "testBaseline": onTestBaseline,
  "planAbandoned": onPlanAbandoned,
  "planCompleted": onPlanCompleted,
}
def returnToStage(taskState, stage):
  if stage == "review":
    taskState["status"] = TaskStatus.toReview
    return
  taskState["status"] = TaskStatus.queued
  # 寫到一半中斷時，worktree 可能留著半成品、對話也可能壞掉，所以清乾淨並開新對話
  taskState["needsClean"] = True
  taskState["resumable"] = False


def onTaskStarted(taskState, event):
  taskState["status"] = TaskStatus.writing
  taskState["round"] = event["round"]
  taskState["agent"] = event["agent"]
  taskState["sessionId"] = event["data"].get("sessionId")
  # 這個版本之前只有 Claude 寫程式，舊事件沒有 cli 就是 Claude 的對話
  taskState["sessionCli"] = event["data"].get("cli", "claude")
  taskState["sessionModel"] = event["data"].get("model")
  taskState["needsClean"] = False
  taskState["needs"] = None
  taskState["userDone"] = False


def rememberSession(taskState, data):
  # Codex 的對話編號要等它跑起來才知道，所以寫完、卡住時再記一次
  if data.get("sessionId"):
    taskState["sessionId"] = data["sessionId"]
    taskState["sessionCli"] = data.get("cli", taskState["sessionCli"])
    taskState["sessionModel"] = data.get("model", taskState["sessionModel"])


def onTaskWritten(taskState, event):
  # 測試改在背景跑：帶 testing 的寫完先進測試中，等 testRun 再決定；舊事件沒有這個欄位，照舊直接待審查
  taskState["status"] = TaskStatus.testing if event["data"].get("testing") else TaskStatus.toReview
  taskState["lastCommit"] = event["data"].get("commit")
  taskState["failStreak"] = 0
  taskState["resumable"] = True
  rememberSession(taskState, event["data"])
  # 你的補充說明只要交給 Claude 一次，寫成功之後就不再附上
  taskState["userNote"] = ""
  # 審查要知道這一輪是不是在解衝突，才會特別檢查有沒有弄壞其他任務已經合併的內容
  taskState["lastWriteMode"] = event["data"].get("mode", "write")
  taskState["resolving"] = False
  if taskState["lastWriteMode"] == "resolve":
    # 重新合併時的衝突可能和當初在 plan 端的不同，審查要看實際解的檔案
    taskState["conflictFiles"] = list(event["data"].get("conflicts", []))
  # 重新合併沒有衝突時不會經過 taskStarted；寫成功本身就代表 worktree 已經乾淨
  taskState["needsClean"] = False


def onTaskBlocked(taskState, event):
  taskState["status"] = TaskStatus.needsYou
  taskState["needs"] = {"kind": "blocked", "reason": "寫程式的 agent 回報卡住：" + event["data"].get("reason", "")}
  taskState["resumable"] = True
  rememberSession(taskState, event["data"])


def onTestRun(taskState, event):
  taskState["lastTest"] = event["data"]
  # verdict 由 conductor 跑完測試時對照當時的基準算好；沒有 verdict 的舊事件照舊只記錄
  if event["data"].get("verdict") != "newFailure":
    if taskState["status"] == TaskStatus.testing:
      taskState["status"] = TaskStatus.toReview
    return
  # 有新增的失敗就不送審，和審查不通過一樣退回寫程式的 agent、算一輪
  if taskState["round"] < taskState["maxRounds"]:
    taskState["status"] = TaskStatus.queued
    taskState["round"] += 1
    return
  names = event["data"].get("newFailures") or []
  reason = f"第 {taskState['round']} 輪測試有新增的失敗，輪數用完"
  taskState["status"] = TaskStatus.needsYou
  taskState["needs"] = {"kind": "tests", "reason": reason + ("：" + "、".join(names) if names else "")}


def onReviewStarted(taskState, event):
  taskState["status"] = TaskStatus.reviewing
  taskState["reviewAgent"] = event["agent"]
  # 儀表板照審查啟動時的 CLI 解析它的 log；中途換了設定也不會用錯格式
  taskState["reviewCli"] = event["data"].get("cli")
  taskState["reviewModel"] = event["data"].get("model")


def onReviewPassed(taskState, event):
  taskState["status"] = TaskStatus.merging
  taskState["failStreak"] = 0
  taskState["reviews"].append(event["data"].get("notes"))


def onReviewFailed(taskState, event):
  taskState["failStreak"] = 0
  taskState["reviews"].append(event["data"].get("notes"))
  taskState["lastReviewNotes"] = event["data"].get("notes")
  if taskState["round"] < taskState["maxRounds"]:
    taskState["status"] = TaskStatus.queued
    taskState["round"] += 1
  else:
    taskState["status"] = TaskStatus.needsYou
    taskState["needs"] = {"kind": "rounds", "reason": f"審查 {taskState['round']} 次不通過"}


def onTaskMerged(taskState, event):
  taskState["status"] = TaskStatus.done
  taskState["needs"] = None


def onAgentFailed(taskState, event):
  stage = event["data"].get("stage", "write")
  if taskState["failStreak"] == 0:
    # 第一次失敗多半是暫時性的（網路、偶發當掉），自動重試一次，不算輪數
    taskState["failStreak"] = 1
    returnToStage(taskState, stage)
    return
  taskState["failStreak"] = 0
  taskState["status"] = TaskStatus.needsYou
  taskState["needs"] = {"kind": "agentFailed", "stage": stage,
                        "reason": "agent 連續失敗：" + event["data"].get("reason", "")}


def onTaskInterrupted(taskState, event):
  # 中斷不是任務本身的問題，回到原本的階段重來，不算失敗也不算輪數
  returnToStage(taskState, event["data"].get("stage", "write"))


def onNeedsYou(taskState, event):
  taskState["status"] = TaskStatus.needsYou
  taskState["needs"] = {"kind": event["data"].get("kind", "other"), "reason": event["data"].get("reason", "")}
  if event["data"].get("conflicts"):
    # 你選「再跑一輪」時要知道該請 Claude 解哪些檔案
    taskState["needs"]["conflicts"] = list(event["data"]["conflicts"])


def startResolving(taskState, conflicts):
  taskState["status"] = TaskStatus.queued
  taskState["resolving"] = True
  taskState["conflictFiles"] = list(conflicts)
  # 解衝突是新的一輪（審查意見檔才不會蓋掉上一輪的），但上限一起加，不吃掉審查退回的額度
  taskState["round"] += 1
  taskState["maxRounds"] += 1
  taskState["userDone"] = False


def onTaskConflicted(taskState, event):
  startResolving(taskState, event["data"].get("conflicts", []))
  taskState["conflictCount"] += 1


# 「再跑一輪」要回到哪個階段，取決於當初為什麼停下來
retryTargets = {
  "blocked": TaskStatus.queued,
  "noChanges": TaskStatus.queued,
  "rounds": TaskStatus.queued,
  "tests": TaskStatus.queued,
  "invalidReview": TaskStatus.toReview,
  "mergeFailed": TaskStatus.merging,
}


def onUserDecision(taskState, event):
  if taskState["status"] != TaskStatus.needsYou:
    return
  action = event["data"].get("action")
  needs = taskState["needs"] or {}
  if action == "abandon":
    taskState["status"] = TaskStatus.abandoned
  elif action == "done":
    taskState["status"] = TaskStatus.merging
    taskState["userDone"] = True
  elif action == "retry":
    if needs.get("kind") in ("rounds", "tests"):
      taskState["maxRounds"] += 1
      taskState["round"] += 1
    if needs.get("kind") == "agentFailed":
      returnToStage(taskState, needs.get("stage", "write"))
    elif needs.get("kind") == "mergeConflict":
      # 自動解衝突的次數用完後，你選「再跑一輪」代表再交給 Claude 解一次
      startResolving(taskState, needs.get("conflicts", []))
    else:
      taskState["status"] = retryTargets.get(needs.get("kind"), TaskStatus.queued)
    taskState["userNote"] = event["data"].get("note", "")
  else:
    return
  taskState["needs"] = None
  taskState["failStreak"] = 0


taskEventHandlers = {
  "taskStarted": onTaskStarted,
  "taskWritten": onTaskWritten,
  "taskBlocked": onTaskBlocked,
  "testRun": onTestRun,
  "reviewStarted": onReviewStarted,
  "reviewPassed": onReviewPassed,
  "reviewFailed": onReviewFailed,
  "taskMerged": onTaskMerged,
  "agentFailed": onAgentFailed,
  "taskInterrupted": onTaskInterrupted,
  "needsYou": onNeedsYou,
  "userDecision": onUserDecision,
  "taskConflicted": onTaskConflicted,
}


def settleAbandonedTasks(state):
  for taskState in state["tasks"].values():
    if state["plans"][taskState["plan"]]["status"] == PlanStatus.abandoned and taskState["status"] != TaskStatus.done:
      taskState["status"] = TaskStatus.abandoned
  # 前置被放棄，等它的任務也做不了；連鎖可能好幾層，所以重複到沒有變化為止
  changed = True
  while changed:
    changed = False
    for taskState in state["tasks"].values():
      if taskState["status"] in (TaskStatus.done, TaskStatus.abandoned):
        continue
      for dependencyId in taskState["dependsOn"]:
        dependency = state["tasks"].get(taskKey(taskState["plan"], dependencyId))
        if dependency and dependency["status"] == TaskStatus.abandoned:
          taskState["status"] = TaskStatus.abandoned
          changed = True
          break


def settlePlanStatuses(state, plans):
  for planState in state["plans"].values():
    tasks = planTasks(state, planState["id"])
    if planState["status"] == PlanStatus.active and tasks and all(task["status"] == TaskStatus.abandoned for task in tasks):
      planState["status"] = PlanStatus.abandoned
  for plan in plans.values():
    for oldId in asList(plan["meta"].get("supersedes")):
      oldPlan = state["plans"].get(oldId)
      # 進行中的 plan 還有 agent 在跑，不能因為被新 plan 取代就改掉它的狀態
      if oldPlan and oldPlan["status"] != PlanStatus.active:
        oldPlan["status"] = PlanStatus.superseded


def deriveState(events, plans, config):
  state = {"paused": False, "pauseReason": "", "plans": {}, "tasks": {}}
  for planIdValue, plan in plans.items():
    state["plans"][planIdValue] = {"id": planIdValue, "title": plan["title"], "status": PlanStatus.draft,
                                   "baseCommit": None, "testBaseline": None, "rejectReason": "",
                                   "meta": plan["meta"]}
  for event in events:
    eventType = event.get("type")
    if eventType == "paused":
      state["paused"] = True
      state["pauseReason"] = event["data"].get("reason", "")
    elif eventType == "resumed":
      state["paused"] = False
      state["pauseReason"] = ""
    elif eventType in planEventHandlers:
      planState = state["plans"].get(event.get("plan"))
      if planState:
        planEventHandlers[eventType](state, planState, event, config)
    elif eventType in taskEventHandlers:
      taskState = state["tasks"].get(taskKey(event.get("plan"), event.get("task")))
      if taskState:
        taskEventHandlers[eventType](taskState, event)
  settleAbandonedTasks(state)
  settlePlanStatuses(state, plans)
  return state


def readyTasks(state):
  if state["paused"]:
    return []
  ready = []
  for taskState in tasksWithStatus(state, TaskStatus.queued):
    dependencies = [state["tasks"].get(taskKey(taskState["plan"], dependencyId)) for dependencyId in taskState["dependsOn"]]
    if all(dependency and dependency["status"] == TaskStatus.done for dependency in dependencies):
      ready.append(taskState)
  return ready


def tasksWithStatus(state, status):
  return [taskState for taskState in state["tasks"].values()
          if taskState["status"] == status and state["plans"][taskState["plan"]]["status"] == PlanStatus.active]


def isPlanFinished(state, planIdValue):
  tasks = planTasks(state, planIdValue)
  finishedStatuses = (TaskStatus.done, TaskStatus.abandoned)
  return (bool(tasks) and all(task["status"] in finishedStatuses for task in tasks)
          and any(task["status"] == TaskStatus.done for task in tasks))


def agentLanes(prefix, config):
  return [prefix] + [f"{prefix}-sub{index}" for index in range(1, config["maxParallel"])]
