import copy
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from board_core import BoardError, PlanStatus, TaskStatus, deriveState, newTaskState, taskKey
from board_review import renderReviewMarkdown, verifyRef

progressDirName = ".agent-board"
progressFileName = "progress.json"
formatName = "agent-board-progress"
formatVersion = 1
maxReasonLength = 500
maxNoteLength = 2000
pathPlaceholder = "<路徑>"
maskPlaceholder = "<已遮蔽>"
# conductor 記 agent 失敗時用這個符號接上 agent 輸出尾段，尾段是終端內容，不能交接
agentOutputSeparator = "｜"

# 測試紀錄只留結論：指令與輸出是本機的，新電腦的 conductor 看到沒有指令會自己重跑基準
testSummaryFields = ("commit", "passed", "failures", "qualityChecked", "qualityFailures", "comparedTo", "covers")
lastTestFields = ("source", "verdict", "newFailures", "commit", "passed", "failures", "qualityChecked",
                  "qualityFailures")
failureListFields = {"failures", "qualityFailures", "newFailures"}
needsFields = ("kind", "reason", "stage", "conflicts")
planCopyFields = ("status", "rejectReason", "baseCommit", "testTiming")
# 對話編號、agent 名稱、CLI 與 worktree 清理狀態只在原本那台電腦有意義，不放進交接檔
taskCopyFields = ("text", "dependsOn", "requirementIds", "status", "round", "maxRounds", "userNote", "userDone",
                  "failStreak", "lastCommit", "mergedCommit", "resolving", "conflictFiles", "conflictCount",
                  "lastWriteMode", "mergeFix", "mergeFailures", "reviews", "lastReviewNotes")
contentFields = ("format", "version", "projectId", "plans", "branches")

quotedPathPattern = re.compile(r"""(['"])(?:[A-Za-z]:[\\/]|\\\\|/)[^'"\n]*\1""")
# 前面接著網址的 :/ 或相對路徑的字元時不算絕對路徑，plan/P0001、https://… 才不會被誤遮；
# 全形標點視為路徑結尾，中文句子裡「路徑，後文」的後文才不會一起被吃掉
barePathPattern = re.compile(r"""(?<![\w.~-])(?:[A-Za-z]:[\\/]|\\\\)[^\s'"，。；、）」]*"""
                             r"""|(?<![\w.:~/-])/[^\s'"，。；、）」]+""")
conflictMarkerPattern = re.compile(r"^(?:<{7}|={7}|>{7})(?: |$)", re.MULTILINE)
# 遮蔽值遇到全形標點與截斷用的「…」就停，重複清理同一段文字結果才會一樣
secretValue = r"""[^\s'"，。；、）」…]+"""
# 鍵名後可接收尾引號，JSON 與 Python dict 寫法的 "token": "…" 才遮得到
secretKey = (r"""(?i)((?<![a-z])[a-z_-]*(?:token|secret|password|passwd|api[_-]?key)[a-z_-]*["']?"""
             r"""\s*[=:：]\s*)""")
# 依序套用：先去掉終端控制碼，再遮蔽憑證與 PID，最後才遮路徑，避免路徑規則先吃掉 token 的前後文
maskRules = (
  (re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]"), ""),
  (re.compile(r"[\x00-\x08\x0b-\x1f\x7f]"), ""),
  (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1" + maskPlaceholder + "@"),
  (re.compile(r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
              r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|glpat-[A-Za-z0-9_-]{20,})"),
   maskPlaceholder),
  (re.compile(r"(?i)(?<![a-z])(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + maskPlaceholder),
  # 有引號的值整段遮到收尾引號，值裡有空白也不會漏；沒收尾的引號交給下一條照一般值遮
  (re.compile(secretKey + r"""(["'])[^\n]*?\2"""), r"\1\2" + maskPlaceholder + r"\2"),
  (re.compile(secretKey + r"""(["']?)""" + secretValue), r"\1\2" + maskPlaceholder),
  (re.compile(r"(?i)(?<![a-z])(pid)(?![a-z])[\s:=#]*\d+"), r"\1 " + maskPlaceholder),
  (quotedPathPattern, lambda match: match.group(1) + pathPlaceholder + match.group(1)),
  (barePathPattern, pathPlaceholder),
)
unclosedPlaceholderPattern = re.compile(r"<[^<>\s]*$")


class SyncError(BoardError):
  pass


def progressPath(projectDir):
  return Path(projectDir) / progressDirName / progressFileName


def newProjectId():
  return uuid.uuid4().hex


def portableText(text, limit=maxNoteLength):
  for pattern, replacement in maskRules:
    text = pattern.sub(replacement, text)
  if len(text) > limit:
    # 截在遮蔽標記中間會讓下次清理又補回標記，結果對不上，所以連同半個標記一起丟掉
    text = unclosedPlaceholderPattern.sub("", text[:limit - 1]) + "…"
  return text


def portableList(items):
  return None if items is None else [portableText(item) for item in items]


def briefReason(text):
  return portableText(text.split(agentOutputSeparator, 1)[0], maxReasonLength)


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


def testSummary(data, fields):
  picked = pickFields(data, fields)
  # 失敗清單可能帶著本機的測試路徑，和使用者文字一樣要清理
  for field in failureListFields.intersection(picked or {}):
    picked[field] = portableList(picked[field])
  return picked


def portableNeeds(needs):
  picked = pickFields(needs, needsFields)
  if picked and "reason" in picked:
    picked["reason"] = briefReason(picked["reason"])
  return picked


reviewHeaderPattern = re.compile(r"# (\S+) (\S+) 第 ([1-9]\d*) 輪審查：(通過|不通過)")
mustFixItemPattern = re.compile(r"\[([^\]\n]*)\] (.*)", re.DOTALL)
mustFixStartPattern = re.compile(r"- \[([^\]\n]*)\] ")


def reviewItems(lines, startsItem):
  if lines == ["- （無）"]:
    return []
  items = []
  for line in lines:
    # 項目內容本身可能有「- 」開頭的清單行，只有符合項目開頭的行才切新項目，其餘接回上一項
    if startsItem(line):
      items.append(line[2:])
    elif items:
      items[-1] += "\n" + line
    else:
      return None
  return items


def mustFixStarts(refs):
  # 必改項目一定以審查時允許的 [需求編號] 開頭，內容裡的「- [x] …」不會被當成新項目
  def startsItem(line):
    match = mustFixStartPattern.match(line)
    return match is not None and match.group(1).strip().upper() in refs
  return startsItem


def isSuggestStart(line):
  return line.startswith("- ")


def reviewBody(lines, refs):
  # 必改正文也可能有「## 」標題，甚至一行「## 建議」，所以不靠標題切區段：
  # 從最後一個空行後的「## 建議」往前試，寧可把可疑的行留在必改，也不讓必改少一截
  splits = [index for index in range(4, len(lines)) if lines[index] == "## 建議" and lines[index - 1] == ""]
  for index in reversed(splits):
    mustFix = reviewItems(lines[3:index - 1], mustFixStarts(refs))
    suggest = reviewItems(lines[index + 1:-1], isSuggestStart)
    if mustFix is not None and suggest is not None:
      return mustFix, suggest
  return None


def parseReviewMarkdown(text, refs):
  # 解析 renderReviewMarkdown 的格式；不是這個格式就當讀不到，不猜內容
  text = text.replace("\r\n", "\n")
  lines = text.split("\n")
  header = reviewHeaderPattern.fullmatch(lines[0])
  body = reviewBody(lines, refs) if header and lines[1:3] == ["", "## 必改"] else None
  if body is None:
    return None
  itemMatches = [mustFixItemPattern.fullmatch(item) for item in body[0]]
  conclusion = {"round": int(header.group(3)), "verdict": "pass" if header.group(4) == "通過" else "fail",
                "mustFix": [{"ref": match.group(1), "text": match.group(2)} for match in itemMatches],
                "suggest": body[1]}
  # 重新輸出要和原檔一字不差，才能確定解析沒有漏掉或改動任何內容
  rendered = renderReviewMarkdown(header.group(1), header.group(2), conclusion["round"], conclusion)
  return conclusion if rendered == text else None


def portableConclusion(conclusion):
  return {**conclusion, "suggest": portableList(conclusion["suggest"]),
          "mustFix": [{"ref": portableText(item["ref"]), "text": portableText(item["text"])}
                      for item in conclusion["mustFix"]]}


def reviewReader(boardDirectory):
  def read(notes):
    try:
      return (Path(boardDirectory) / notes).read_bytes().decode("utf-8", errors="replace")
    except OSError:
      return None
  return read


def reviewLookup(readReview, previous):
  # 新電腦沒有原機的審查檔，讀不到時沿用上一版交接檔的結論，重新匯出才不會把必改弄丟
  known = {notes: conclusion for plan in (previous or {}).get("plans", {}).values()
           for task in plan["tasks"] for notes, conclusion in task["reviewConclusions"].items()}

  def lookup(notes, refs):
    text = readReview(notes) if readReview else None
    conclusion = parseReviewMarkdown(text, refs) if text is not None else None
    return portableConclusion(conclusion) if conclusion else known.get(notes)
  return lookup


def reviewConclusions(taskState, lookup):
  # 只帶最後一次審查：writer 重寫時讀的是 lastReviewNotes，看板顯示的是最後一輪
  notesNames = {taskState["lastReviewNotes"], *taskState["reviews"][-1:]} - {None}
  refs = {ref.upper() for ref in taskState["requirementIds"]} | {verifyRef}
  conclusions = {notes: lookup(notes, refs) for notes in sorted(notesNames)}
  return {notes: conclusion for notes, conclusion in conclusions.items() if conclusion is not None}


def taskEntry(taskState, lookup):
  entry = {"id": taskState["task"], **{field: taskState[field] for field in taskCopyFields}}
  entry["text"] = portableText(entry["text"])
  entry["userNote"] = portableText(entry["userNote"])
  entry["mergeFailures"] = portableList(entry["mergeFailures"])
  entry["lastTest"] = testSummary(taskState["lastTest"], lastTestFields)
  entry["needs"] = portableNeeds(taskState["needs"])
  entry["reviewConclusions"] = reviewConclusions(taskState, lookup)
  return entry


def planFileInfo(planIdValue, plans, previous):
  plan = plans.get(planIdValue)
  if plan is not None and plan.get("path"):
    return {"file": "plans/" + Path(plan["path"]).name, "sha256": planFileHash(plan["path"])}
  previousEntry = (previous or {}).get("plans", {}).get(planIdValue)
  if previousEntry:
    return {"file": previousEntry["file"], "sha256": previousEntry["sha256"]}
  raise SyncError(f"找不到 {planIdValue} 的 plan 檔，無法記錄它的進度")


def planEntry(state, planState, fileInfo, lookup):
  mergeTest = planState["mergeTest"]
  return {**{field: planState[field] for field in planCopyFields}, **fileInfo,
          "rejectReason": portableText(planState["rejectReason"]),
          "testBaseline": testSummary(planState["testBaseline"], testSummaryFields),
          "mergeTest": {"lastTested": testSummary(mergeTest["lastTested"], testSummaryFields),
                        "split": testSummary(mergeTest["split"], testSummaryFields)},
          "tasks": [taskEntry(taskState, lookup) for taskState in state["tasks"].values()
                    if taskState["plan"] == planState["id"]]}


def chainAfter(body, previous):
  body["previousRevision"] = previous["revision"] if previous else None
  body["ancestors"] = [previous["revision"], *previous["ancestors"]] if previous else []
  body["revision"] = revisionOf(body)
  return body


def portablePlans(state, plans, previous, lookup):
  return {planIdValue: planEntry(state, planState, planFileInfo(planIdValue, plans, previous), lookup)
          for planIdValue, planState in state["plans"].items() if planState["status"] != PlanStatus.draft}


def sameContent(previous, body):
  return previous is not None and all(previous[field] == body[field] for field in contentFields)


def buildSnapshot(state, plans, projectId, previous=None, branches=None, readReview=None):
  if previous is not None and previous["projectId"] != projectId:
    raise SyncError("交接檔的專案識別和這個專案不同，不能接在它後面")
  if branches is None:
    branches = previous["branches"] if previous else {}
  body = {"format": formatName, "version": formatVersion, "projectId": projectId, "branches": dict(branches),
          "plans": portablePlans(state, plans, previous, reviewLookup(readReview, previous))}
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


def portableCheck(clean):
  # 匯出時已清理過，所以清理後還會變的內容一定是外部塞進來的本機資料
  def check(value, path):
    isText(value, path)
    if clean(value) != value:
      fail(path, "含有本機資料（絕對路徑、token、PID 或終端輸出）或太長")
  return check


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
noteText = portableCheck(portableText)
reasonText = portableCheck(briefReason)
textList = optional(listOf(noteText))
conclusionSpec = objectOf({
  "round": isPositive, "verdict": oneOf(("pass", "fail")),
  "mustFix": listOf(objectOf({"ref": noteText, "text": noteText})), "suggest": listOf(noteText),
})

testSummarySpec = objectOf({}, {
  "commit": optional(shaText), "passed": optional(isBool), "failures": textList,
  "qualityChecked": optional(isBool), "qualityFailures": textList, "comparedTo": optional(shaText),
  "covers": optional(listOf(idText)),
})
lastTestSpec = objectOf({}, {
  "source": optional(noteText), "verdict": optional(noteText), "newFailures": textList, "commit": optional(shaText),
  "passed": optional(isBool), "failures": textList, "qualityChecked": optional(isBool), "qualityFailures": textList,
})
needsSpec = objectOf({"kind": noteText, "reason": reasonText},
                     {"stage": optional(noteText), "conflicts": listOf(relativePath)})
taskStatuses = tuple(value for name, value in vars(TaskStatus).items() if not name.startswith("_"))
planStatuses = tuple(value for name, value in vars(PlanStatus).items()
                     if not name.startswith("_") and value != PlanStatus.draft)
taskSpec = objectOf({
  "id": idText, "text": noteText, "dependsOn": listOf(idText), "requirementIds": listOf(noteText),
  "status": oneOf(taskStatuses), "round": isPositive, "maxRounds": isPositive, "userNote": noteText,
  "userDone": isBool, "failStreak": isCount, "lastCommit": optional(shaText), "mergedCommit": optional(shaText),
  "resolving": isBool, "conflictFiles": listOf(relativePath), "conflictCount": isCount, "lastWriteMode": noteText,
  "mergeFix": isBool, "mergeFailures": listOf(noteText), "reviews": listOf(optional(reviewNote)),
  "lastReviewNotes": optional(reviewNote), "lastTest": optional(lastTestSpec), "needs": optional(needsSpec),
  "reviewConclusions": mapOf(reviewNote, conclusionSpec),
})
planSpec = objectOf({
  "status": oneOf(planStatuses), "rejectReason": noteText, "baseCommit": optional(shaText),
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


# 這些狀態在還原後會被 conductor 直接拿來用，缺了對應欄位就無法往下走
requiredByStatus = {
  TaskStatus.needsYou: ("needs", "是等你處理，卻沒有記錄原因"),
  TaskStatus.mergedUntested: ("mergedCommit", "是已合併待測試，卻沒有記錄 mergedCommit"),
}


def checkTask(task, path, taskIds):
  missing = [dependencyId for dependencyId in task["dependsOn"] if dependencyId not in taskIds]
  if missing:
    fail(path, "的前置任務不存在：" + "、".join(missing))
  field, message = requiredByStatus.get(task["status"], (None, None))
  if field and task[field] is None:
    fail(path, message)
  unknown = [notes for notes in task["reviewConclusions"]
             if notes not in task["reviews"] and notes != task["lastReviewNotes"]]
  if unknown:
    fail(path, "的審查結論對不到審查紀錄：" + "、".join(unknown))


def cycleFrom(node, graph, trail, finished):
  if node in trail:
    return trail[trail.index(node):] + [node]
  if node in finished:
    return None
  for dependencyId in graph[node]:
    cycle = cycleFrom(dependencyId, graph, [*trail, node], finished)
    if cycle:
      return cycle
  finished.add(node)
  return None


def findCycle(graph):
  finished = set()
  for node in graph:
    cycle = cycleFrom(node, graph, [], finished)
    if cycle:
      return cycle
  return None


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
  # 循環相依的任務永遠等不到前置完成，還原後會整批卡住
  cycle = findCycle({task["id"]: task["dependsOn"] for task in tasks})
  if cycle:
    fail(path, "的前置任務形成循環：" + " → ".join(cycle))


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


def writeReviewNote(path, planIdValue, taskId, conclusion):
  # 本機已有的審查檔是原稿，比交接檔裡遮蔽過的結論完整，不覆蓋
  if path.exists():
    return
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(renderReviewMarkdown(planIdValue, taskId, conclusion["round"], conclusion), encoding="utf-8")


def restoreReviewNotes(snapshot, boardDirectory):
  # writer 重寫時從 .board/reviews 讀必改，新電腦沒有這些檔就從交接檔的結論補回來
  for planIdValue, plan in snapshot["plans"].items():
    for task in plan["tasks"]:
      for notes, conclusion in task["reviewConclusions"].items():
        writeReviewNote(Path(boardDirectory) / notes, planIdValue, task["id"], conclusion)


def stateFromSnapshot(snapshot, plans):
  # 草稿不在交接檔裡，照 deriveState 的預設值建；plan 檔還沒拉下來的用編號當標題
  missingPlans = {planIdValue: {"title": planIdValue, "meta": {}}
                  for planIdValue in snapshot["plans"] if planIdValue not in plans}
  state = deriveState([], {**plans, **missingPlans}, {"maxRounds": 1})
  for planIdValue, entry in snapshot["plans"].items():
    restorePlan(state, state["plans"][planIdValue], entry)
  return state
