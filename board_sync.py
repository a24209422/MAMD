import copy
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from board_core import BoardError, PlanStatus, TaskStatus, deriveState, newTaskState, taskKey

progressDirName = ".agent-board"
progressFileName = "progress.json"
formatName = "agent-board-progress"
formatVersion = 1
maxReasonLength = 500
pathPlaceholder = "<路徑>"

# 測試紀錄只留結論：指令與輸出是本機的，新電腦的 conductor 看到沒有指令會自己重跑基準
testSummaryFields = ("commit", "passed", "failures", "qualityChecked", "qualityFailures", "comparedTo", "covers")
lastTestFields = ("source", "verdict", "newFailures", "commit", "passed", "failures", "qualityChecked",
                  "qualityFailures")
needsFields = ("kind", "reason", "stage", "conflicts")
planCopyFields = ("status", "rejectReason", "baseCommit", "testTiming")
# 對話編號、agent 名稱、CLI 與 worktree 清理狀態只在原本那台電腦有意義，不放進交接檔
taskCopyFields = ("text", "dependsOn", "requirementIds", "status", "round", "maxRounds", "userNote", "userDone",
                  "failStreak", "lastCommit", "mergedCommit", "resolving", "conflictFiles", "conflictCount",
                  "lastWriteMode", "mergeFix", "mergeFailures", "reviews", "lastReviewNotes")
contentFields = ("format", "version", "projectId", "plans", "branches")

quotedPathPattern = re.compile(r"""(['"])(?:[A-Za-z]:[\\/]|\\\\|/)[^'"\n]*\1""")
# 前面接著網址的 :/ 或相對路徑的字元時不算絕對路徑，plan/P0001、https://… 才不會被誤遮
barePathPattern = re.compile(r"""(?<![\w.~-])(?:[A-Za-z]:[\\/]|\\\\)[^\s'"]*|(?<![\w.:~/-])/[^\s'"/]+/[^\s'"]*""")
conflictMarkerPattern = re.compile(r"^(?:<{7}|={7}|>{7})(?: |$)", re.MULTILINE)


class SyncError(BoardError):
  pass


def progressPath(projectDir):
  return Path(projectDir) / progressDirName / progressFileName


def newProjectId():
  return uuid.uuid4().hex


def briefReason(text):
  text = quotedPathPattern.sub(lambda match: match.group(1) + pathPlaceholder + match.group(1), text)
  text = barePathPattern.sub(pathPlaceholder, text)
  if len(text) > maxReasonLength:
    text = text[:maxReasonLength - 1] + "…"
  return text


def canonicalJson(data):
  return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def revisionOf(snapshot):
  # 只看內容與前序版本，不看包含它的 Git commit，同一份進度在哪台電腦算都一樣
  body = {key: value for key, value in snapshot.items() if key != "revision"}
  return hashlib.sha256(canonicalJson(body).encode("utf-8")).hexdigest()


def planFileHash(path):
  # Git 在 Windows 可能把換行轉成 CRLF，同一份 plan 在兩台電腦的雜湊要一樣
  text = Path(path).read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
  return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pickFields(data, fields):
  if data is None:
    return None
  return {field: data[field] for field in fields if field in data}


def portableNeeds(needs):
  picked = pickFields(needs, needsFields)
  if picked and "reason" in picked:
    picked["reason"] = briefReason(picked["reason"])
  return picked


def taskEntry(taskState):
  entry = {"id": taskState["task"], **{field: taskState[field] for field in taskCopyFields}}
  entry["lastTest"] = pickFields(taskState["lastTest"], lastTestFields)
  entry["needs"] = portableNeeds(taskState["needs"])
  return entry


def planFileInfo(planIdValue, plans, previous):
  plan = plans.get(planIdValue)
  if plan is not None and plan.get("path"):
    return {"file": "plans/" + Path(plan["path"]).name, "sha256": planFileHash(plan["path"])}
  previousEntry = (previous or {}).get("plans", {}).get(planIdValue)
  if previousEntry:
    return {"file": previousEntry["file"], "sha256": previousEntry["sha256"]}
  raise SyncError(f"找不到 {planIdValue} 的 plan 檔，無法記錄它的進度")


def planEntry(state, planState, fileInfo):
  mergeTest = planState["mergeTest"]
  return {**{field: planState[field] for field in planCopyFields}, **fileInfo,
          "testBaseline": pickFields(planState["testBaseline"], testSummaryFields),
          "mergeTest": {"lastTested": pickFields(mergeTest["lastTested"], testSummaryFields),
                        "split": pickFields(mergeTest["split"], testSummaryFields)},
          "tasks": [taskEntry(taskState) for taskState in state["tasks"].values()
                    if taskState["plan"] == planState["id"]]}


def chainAfter(body, previous):
  body["previousRevision"] = previous["revision"] if previous else None
  body["ancestors"] = [previous["revision"], *previous["ancestors"]] if previous else []
  body["revision"] = revisionOf(body)
  return body


def portablePlans(state, plans, previous):
  return {planIdValue: planEntry(state, planState, planFileInfo(planIdValue, plans, previous))
          for planIdValue, planState in state["plans"].items() if planState["status"] != PlanStatus.draft}


def sameContent(previous, body):
  return previous is not None and all(previous[field] == body[field] for field in contentFields)


def buildSnapshot(state, plans, projectId, previous=None, branches=None):
  if previous is not None and previous["projectId"] != projectId:
    raise SyncError("交接檔的專案識別和這個專案不同，不能接在它後面")
  if branches is None:
    branches = previous["branches"] if previous else {}
  body = {"format": formatName, "version": formatVersion, "projectId": projectId, "branches": dict(branches),
          "plans": portablePlans(state, plans, previous)}
  # 經過一次 JSON 轉換：不和 state 共用物件，也確定內容寫得進檔案
  body = json.loads(canonicalJson(body))
  # 內容沒變就沿用上一版，重複匯出不會一直長出新的 revision
  if sameContent(previous, body):
    return previous
  snapshot = chainAfter(body, previous)
  validateSnapshot(snapshot)
  return snapshot


def fail(path, message):
  raise SyncError(f"交接檔的 {path or '最上層'} {message}")


def joinPath(path, key):
  return f"{path}.{key}" if path else str(key)


def isText(value, path):
  if not isinstance(value, str):
    fail(path, "必須是文字")


def isBool(value, path):
  if not isinstance(value, bool):
    fail(path, "必須是 true 或 false")


def isCount(value, path):
  if type(value) is not int or value < 0:
    fail(path, "必須是非負整數")


def isPositive(value, path):
  if type(value) is not int or value < 1:
    fail(path, "必須是正整數")


def isPortableReason(value, path):
  isText(value, path)
  if briefReason(value) != value:
    fail(path, "含有本機路徑或太長")


def matches(pattern, description):
  compiled = re.compile(pattern)

  def check(value, path):
    if not isinstance(value, str) or not compiled.fullmatch(value):
      fail(path, f"必須是{description}：{value!r}")
  return check


def oneOf(values):
  def check(value, path):
    if not any(value == allowed and type(value) is type(allowed) for allowed in values):
      fail(path, f"不是合法的值：{value!r}")
  return check


def optional(check):
  def checkOptional(value, path):
    if value is not None:
      check(value, path)
  return checkOptional


def listOf(check):
  def checkList(value, path):
    if not isinstance(value, list):
      fail(path, "必須是清單")
    for index, item in enumerate(value):
      check(item, f"{path}[{index}]")
  return checkList


def mapOf(keyCheck, valueCheck):
  def checkMap(value, path):
    if not isinstance(value, dict):
      fail(path, "必須是物件")
    for key, item in value.items():
      itemPath = joinPath(path, key)
      keyCheck(key, itemPath)
      valueCheck(item, itemPath)
  return checkMap


def objectOf(required, optionalFields=None):
  allowed = {**(optionalFields or {}), **required}

  def checkObject(value, path):
    if not isinstance(value, dict):
      fail(path, "必須是物件")
    for key in required:
      if key not in value:
        fail(path, f"缺少欄位：{key}")
    for key, item in value.items():
      if key not in allowed:
        fail(path, f"有欄位不在白名單：{key}")
      allowed[key](item, joinPath(path, key))
  return checkObject


shaText = matches(r"[0-9a-f]{7,64}", " commit SHA")
hashText = matches(r"[0-9a-f]{64}", " SHA-256 雜湊")
idText = matches(r"[A-Za-z0-9][A-Za-z0-9_.-]*", "不含路徑的編號")
# 只接受專案內的相對路徑：不能是絕對路徑，也不能用 .. 跳出專案
relativePath = matches(r"(?![A-Za-z]:|[\\/~])(?!(?:.*[\\/])?\.\.(?:[\\/]|$)).+", "專案內的相對路徑")
reviewNote = matches(r"reviews/[^/\\:]+\.md", " reviews/ 底下的審查意見檔")
textList = optional(listOf(isText))

testSummarySpec = objectOf({}, {
  "commit": optional(shaText), "passed": optional(isBool), "failures": textList,
  "qualityChecked": optional(isBool), "qualityFailures": textList, "comparedTo": optional(shaText),
  "covers": optional(listOf(idText)),
})
lastTestSpec = objectOf({}, {
  "source": optional(isText), "verdict": optional(isText), "newFailures": textList, "commit": optional(shaText),
  "passed": optional(isBool), "failures": textList, "qualityChecked": optional(isBool), "qualityFailures": textList,
})
needsSpec = objectOf({"kind": isText, "reason": isPortableReason},
                     {"stage": optional(isText), "conflicts": listOf(relativePath)})
taskStatuses = tuple(value for name, value in vars(TaskStatus).items() if not name.startswith("_"))
planStatuses = tuple(value for name, value in vars(PlanStatus).items()
                     if not name.startswith("_") and value != PlanStatus.draft)
taskSpec = objectOf({
  "id": idText, "text": isText, "dependsOn": listOf(idText), "requirementIds": listOf(isText),
  "status": oneOf(taskStatuses), "round": isPositive, "maxRounds": isPositive, "userNote": isText,
  "userDone": isBool, "failStreak": isCount, "lastCommit": optional(shaText), "mergedCommit": optional(shaText),
  "resolving": isBool, "conflictFiles": listOf(relativePath), "conflictCount": isCount, "lastWriteMode": isText,
  "mergeFix": isBool, "mergeFailures": listOf(isText), "reviews": listOf(optional(reviewNote)),
  "lastReviewNotes": optional(reviewNote), "lastTest": optional(lastTestSpec), "needs": optional(needsSpec),
})
planSpec = objectOf({
  "status": oneOf(planStatuses), "rejectReason": isText, "baseCommit": optional(shaText),
  "testTiming": oneOf((None, "afterMerge")), "file": matches(r"plans/[^/\\:]+\.md", " plans/ 底下的 .md 檔"),
  "sha256": hashText, "testBaseline": optional(testSummarySpec),
  "mergeTest": objectOf({"lastTested": optional(testSummarySpec), "split": optional(testSummarySpec)}),
  "tasks": listOf(taskSpec),
})
snapshotSpec = objectOf({
  "format": isText, "version": isCount, "projectId": matches(r"[0-9a-f]{32}", " 32 位十六進位的專案識別"),
  "revision": hashText, "previousRevision": optional(hashText), "ancestors": listOf(hashText),
  "plans": mapOf(idText, planSpec), "branches": mapOf(matches(r"(?:plan|task)/[A-Za-z0-9][A-Za-z0-9_.-]*",
                                                              " plan/ 或 task/ 分支"), shaText),
})


def checkFormat(data):
  if not isinstance(data, dict):
    fail("", "必須是物件")
  if data.get("format") != formatName:
    raise SyncError("這不是 agent-board 的交接檔（format 欄位不符）")
  if data.get("version") != formatVersion:
    raise SyncError(f"交接檔是版本 {data.get('version')!r}，這個工具只支援版本 {formatVersion}，請先更新工具")


def checkLineage(snapshot):
  ancestors = snapshot["ancestors"]
  expectedHead = [snapshot["previousRevision"]] if snapshot["previousRevision"] else []
  if ancestors[:1] != expectedHead or len(set(ancestors)) != len(ancestors) or snapshot["revision"] in ancestors:
    raise SyncError("交接檔的前序版本鏈不一致：previousRevision 和 ancestors 對不上或有重複")


def checkTask(task, path, taskIds):
  missing = [dependencyId for dependencyId in task["dependsOn"] if dependencyId not in taskIds]
  if missing:
    fail(path, "的前置任務不存在：" + "、".join(missing))
  if task["status"] == TaskStatus.needsYou and task["needs"] is None:
    fail(path, "是等你處理，卻沒有記錄原因")


def checkPlanTasks(planIdValue, plan):
  path = f"plans.{planIdValue}"
  tasks = plan["tasks"]
  if tasks and plan["status"] in (PlanStatus.pending, PlanStatus.rejected):
    fail(path, "還沒核准，不應該有任務")
  taskIds = [task["id"] for task in tasks]
  if len(set(taskIds)) != len(taskIds):
    fail(path, "的任務編號重複")
  for index, task in enumerate(tasks):
    checkTask(task, f"{path}.tasks[{index}]", taskIds)


def validateSnapshot(snapshot):
  checkFormat(snapshot)
  snapshotSpec(snapshot, "")
  checkLineage(snapshot)
  for planIdValue, plan in snapshot["plans"].items():
    checkPlanTasks(planIdValue, plan)
  if revisionOf(snapshot) != snapshot["revision"]:
    raise SyncError("交接檔的 revision 和內容對不上，檔案可能被改過或損毀")


def decodeContent(content):
  if isinstance(content, str):
    return content
  try:
    return content.decode("utf-8-sig")
  except UnicodeDecodeError:
    raise SyncError("交接檔內容損毀：不是 UTF-8 文字") from None


def parseSnapshot(content):
  text = decodeContent(content)
  # 衝突標記要先擋：它會讓 JSON 解析失敗，但使用者該做的是解衝突，不是當成檔案壞掉
  if conflictMarkerPattern.search(text):
    raise SyncError("交接檔有 Git 合併衝突標記（<<<<<<< / ======= / >>>>>>>），請先解決衝突")
  if not text.strip():
    raise SyncError("交接檔是空的，內容損毀")
  try:
    data = json.loads(text)
  except json.JSONDecodeError as error:
    raise SyncError(f"交接檔內容損毀，不是合法的 JSON：{error}") from None
  validateSnapshot(data)
  return data


def serializeSnapshot(snapshot):
  return json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def readSnapshot(projectDir):
  path = progressPath(projectDir)
  try:
    content = path.read_bytes()
  except FileNotFoundError:
    return None
  try:
    return parseSnapshot(content)
  except SyncError as error:
    raise SyncError(f"{progressDirName}/{progressFileName}：{error}") from None


def replaceWithRetry(source, target, attempts=20):
  for _ in range(attempts):
    try:
      os.replace(source, target)
      return
    except PermissionError:
      # Windows 上防毒或編輯器短暫開著檔案時 replace 會被拒絕，稍等再試
      time.sleep(0.05)
  raise SyncError(f"{progressDirName}/{progressFileName} 被其他程式占用，無法更新，請關掉開著它的程式再試")


def writeSnapshot(projectDir, snapshot):
  # 先驗證再寫：不合格的內容不能蓋掉原本可用的交接檔
  validateSnapshot(snapshot)
  path = progressPath(projectDir)
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_name(progressFileName + ".tmp")
  try:
    with open(temporary, "wb") as handle:
      handle.write(serializeSnapshot(snapshot).encode("utf-8"))
      handle.flush()
      os.fsync(handle.fileno())
    replaceWithRetry(temporary, path)
  finally:
    if temporary.exists():
      temporary.unlink()


def restoreTask(planState, entry):
  taskState = newTaskState(planState["id"], entry, {"maxRounds": entry["maxRounds"]})
  for field in (*taskCopyFields, "lastTest", "needs"):
    taskState[field] = copy.deepcopy(entry[field])
  taskState["testAfterMerge"] = planState["testTiming"] == "afterMerge"
  return taskState


def restorePlan(state, planState, entry):
  for field in (*planCopyFields, "testBaseline", "mergeTest"):
    planState[field] = copy.deepcopy(entry[field])
  for task in entry["tasks"]:
    state["tasks"][taskKey(planState["id"], task["id"])] = restoreTask(planState, task)


def stateFromSnapshot(snapshot, plans):
  # 草稿不在交接檔裡，照 deriveState 的預設值建；plan 檔還沒拉下來的用編號當標題
  missingPlans = {planIdValue: {"title": planIdValue, "meta": {}}
                  for planIdValue in snapshot["plans"] if planIdValue not in plans}
  state = deriveState([], {**plans, **missingPlans}, {"maxRounds": 1})
  for planIdValue, entry in snapshot["plans"].items():
    restorePlan(state, state["plans"][planIdValue], entry)
  return state
