"""給 Claude 寫程式、給 Codex 審查的 prompt。"""
from rtm_graph import planId

maxDiffChars = 150000
rowFields = ["requirement", "understanding", "decision", "rejected", "task", "dependsOn", "verify"]

claudeRules = """你是自動流程裡負責寫程式的 agent。
- 只在目前的資料夾（這個任務的 git worktree）裡改檔案。
- 不要執行 git commit、push、reset、checkout 等 git 指令，commit 由流程負責。
- 不要修改 plans/ 和 .board/ 底下的檔案。
- 只做這個任務要求的事，不要順手重構或加功能。
- 完成後，最後的回覆照指定的 JSON 格式：status 填 done，summary 用一句話說明改了什麼。
- 執行指令一次只跑一個，不要用 ;、&&、| 串接；只有被允許的指令（通常是專案的測試指令）能執行，其他會被拒絕。
- 缺少資訊或遇到自己無法解決的問題時，status 填 blocked，blockedReason 寫清楚需要人提供什麼。"""

codexRules = """你是自動流程裡負責審查的 agent，只能讀不能改。
- 依照需求、AI 理解、做法、驗證，判斷這個任務的 diff 有沒有達成。
- 「必改」只放違反需求或驗證、或會讓功能壞掉的問題；每一條的 ref 填它違反的需求編號（例如 R1）或「驗證」。
- 風格、命名、可以更好但不影響需求的意見，一律放「建議」，不會退回。
- 有任何必改，verdict 填 fail；沒有必改，verdict 填 pass。
- 最後的回覆照指定的 JSON 格式。"""


def tddWriteLines(qualityCommand):
  return [
    "這個專案照 tdd-crap 開發：",
    "- 先用 Skill 工具載入 tdd-crap，照它的流程寫：一次一個行為，先寫測試並確認失敗，再寫實作、整理。",
    f"- 品質檢查指令要照原樣執行，換別的寫法會被拒絕：{qualityCommand}",
    "- 只想快速跑幾個測試時，用 python -m pytest 開頭的指令。",
    "- pytest、coverage、radon 已經裝好，不用檢查或安裝。",
    # tdd-crap 會要求先讀 AGENTS.md，那段寫著「不要自己寫程式」，不說清楚 Claude 可能直接回報卡住
    "- AGENTS.md 裡 agent-board 那一段是給 Codex 寫 plan 用的，和你無關。",
  ]


tddReviewLines = [
  "這個專案照 tdd-crap 開發，自動測試就是品質檢查（每個函式的覆蓋率與循環複雜度）。",
  # 報告依 CRAP 由高到低排，conductor 只附最後 50 行，超標的函式反而容易被截掉
  "- 品質檢查沒通過時，讀 worktree 裡的 .quality/report.json 找出超標的函式；上面附的輸出只有最後 50 行。",
  "- 確認測試真的在驗證行為：用放寬斷言、刪除測試、加 # pragma: no cover 排除程式碼等方式通過門檻的，"
  "列為必改，ref 填「驗證」。",
]


def formatRows(rows):
  lines = ["| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |", "|---|---|---|---|---|---|---|"]
  for row in rows:
    lines.append("| " + " | ".join(row[field].replace("|", "\\|") for field in rowFields) + " |")
  return "\n".join(lines)


def taskRows(plan, taskId):
  rowNumbers = set(plan["graph"].tasks[taskId]["rowNumbers"])
  return [row for row in plan["graph"].rows if row["rowNumber"] in rowNumbers]


def requirementLines(plan, taskId):
  lines = []
  for reqId in plan["graph"].tasks[taskId]["requirementIds"]:
    node = plan["graph"].nodes.get(reqId, {})
    lines.append(f"- {reqId}：{node.get('text', '')}（AI 理解：{node.get('note') or '未寫'}）")
  return lines or ["- （沒有對應的需求）"]


def taskHeadline(plan, taskId, roundNumber):
  return f"plan {planId(plan)}「{plan['title']}」的任務 {taskId}：{plan['graph'].tasks[taskId]['text']}（第 {roundNumber} 輪）"


def buildWritePrompt(plan, taskId, roundNumber, reviewMarkdown="", userNote="", qualityCommand=""):
  parts = [
    taskHeadline(plan, taskId, roundNumber), "",
    "這個任務要滿足的需求：", *requirementLines(plan, taskId), "",
    "plan 裡和這個任務有關的列：", formatRows(taskRows(plan, taskId)),
  ]
  if reviewMarkdown:
    parts += ["", "上一輪審查沒有通過，請修正「必改」的每一條（「建議」可以參考，不強制）：", reviewMarkdown]
  if userNote:
    parts += ["", "使用者補充：" + userNote]
  if qualityCommand:
    parts += ["", *tddWriteLines(qualityCommand)]
  parts += ["", "改完後要能照表格的「驗證」欄驗證。完成後依格式回覆。"]
  return "\n".join(parts)


def buildResolvePrompt(plan, taskId, roundNumber, conflicts, userNote=""):
  parts = [
    taskHeadline(plan, taskId, roundNumber), "",
    "這一輪要解合併衝突：其他任務已經先合併進 plan 分支，和這個任務改到同樣的檔案。",
    "流程已經把最新的 plan 分支合併進這個資料夾，下列檔案留有衝突標記（<<<<<<<、=======、>>>>>>>）：",
    *[f"- {name}" for name in conflicts], "",
    "請解掉每一個衝突：同時保留這個任務的改動和其他任務已經合併的改動，不要刪掉別人的功能。",
    "兩邊真的互相矛盾、無法同時保留時，status 填 blocked，blockedReason 說明矛盾在哪裡。",
    "解完後確認檔案裡沒有任何衝突標記，再跑測試。", "",
    "這個任務要滿足的需求：", *requirementLines(plan, taskId), "",
    "plan 裡和這個任務有關的列：", formatRows(taskRows(plan, taskId)),
  ]
  if userNote:
    parts += ["", "使用者補充：" + userNote]
  parts += ["", "完成後依格式回覆。"]
  return "\n".join(parts)


def buildReviewPrompt(plan, taskId, roundNumber, diff, testRun=None, previousErrors=None, resolvedConflicts=None,
                      tddCrap=False):
  parts = [
    "審查 " + taskHeadline(plan, taskId, roundNumber), "",
    "這個任務要滿足的需求（必改的 ref 只能用這些編號或「驗證」）：", *requirementLines(plan, taskId), "",
    "plan 裡和這個任務有關的列：", formatRows(taskRows(plan, taskId)),
  ]
  if resolvedConflicts:
    # 解衝突時最容易出的錯是把別人的改動蓋掉，審查者要知道這一輪的重點
    parts += ["", f"這一輪是解合併衝突：其他任務先合併進 plan 分支，和這個任務都改了 {'、'.join(resolvedConflicts)}。",
              "這個任務已經把最新的 plan 分支合併進來並解掉衝突。請確認這個任務原本的改動還在，"
              "也沒有刪掉或改壞其他任務已經合併的內容。下面的 diff 是這個任務相對於最新 plan 分支的改動。"]
  if testRun:
    parts += ["", f"自動測試結果：{'通過' if testRun['passed'] else '失敗'}", "```", testRun.get("tail", ""), "```"]
  if tddCrap:
    parts += ["", *tddReviewLines]
  if len(diff) > maxDiffChars:
    # 太長的 diff 會超出模型能讀的範圍，截斷並明講，讓審查者知道還有沒看到的部分
    diff = diff[:maxDiffChars] + f"\n（diff 太長，只附前 {maxDiffChars} 個字元）"
  parts += ["", "這個任務的 diff：", "```diff", diff or "（沒有 diff）", "```"]
  if previousErrors:
    parts += ["", "你上一次的審查結果不符合規則，請修正後重新回覆：", *[f"- {error}" for error in previousErrors]]
  return "\n".join(parts)
