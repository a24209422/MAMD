"""給寫程式、審查的 agent 的規則與 prompt。"""
from pathlib import Path

from rtm_graph import planId

maxDiffChars = 150000
rowFields = ["requirement", "understanding", "decision", "rejected", "task", "dependsOn", "verify"]
# tdd-crap 是使用者自己的 Claude skill，跟著使用者的家目錄走，不在這個 repo 裡
tddCrapDir = Path.home() / ".claude" / "skills" / "tdd-crap"

# Codex 會自己讀專案的 AGENTS.md，tdd-crap 也會叫 Claude 先讀；那段寫著「不要自己寫程式」，不說清楚會直接回報卡住
writerBaseRules = """你是自動流程裡負責寫程式的 agent。
- 只在目前的資料夾（這個任務的 git worktree）裡改檔案。
- 不要執行 git commit、push、reset、checkout 等 git 指令，commit 由流程負責。
- 不要修改 plans/ 和 .board/ 底下的檔案。
- 只做這個任務要求的事，不要順手重構或加功能。
- 專案 AGENTS.md 裡 agent-board 那一段是給寫 plan 的 agent 看的，和你無關。
- 完成後，最後的回覆照指定的 JSON 格式：status 填 done，summary 用一句話說明改了什麼。
- 缺少資訊或遇到自己無法解決的問題時，status 填 blocked，blockedReason 寫清楚需要人提供什麼。"""
# Claude 寫程式時只放行測試指令，串接的指令會整串被拒；Codex 在沙箱裡沒有這層限制
claudeShellRule = "- 執行指令一次只跑一個，不要用 ;、&&、| 串接；只有被允許的指令（通常是專案的測試指令）能執行，其他會被拒絕。"

reviewerRules = """你是自動流程裡負責審查的 agent，只能讀不能改。
- 依照需求、AI 理解、做法、驗證，判斷這個任務的 diff 有沒有達成。
- 「必改」只放違反需求或驗證、或會讓功能壞掉的問題；每一條的 ref 填它違反的需求編號（例如 R1）或「驗證」。
- 風格、命名、可以更好但不影響需求的意見，一律放「建議」，不會退回。
- 有任何必改，verdict 填 fail；沒有必改，verdict 填 pass。
- 專案 AGENTS.md 裡 agent-board 那一段是給寫 plan 的 agent 看的，和你無關。
- 最後的回覆照指定的 JSON 格式。"""


def writerRules(cli):
  return (writerBaseRules + "\n" + claudeShellRule) if cli == "claude" else writerBaseRules


def tddWriteLines(qualityCommand, cli):
  if cli == "claude":
    steps = [
      "- 先用 Skill 工具載入 tdd-crap，照它的流程寫：一次一個行為，先寫測試並確認失敗，再寫實作、整理。",
      f"- 品質檢查指令要照原樣執行，換別的寫法會被拒絕：{qualityCommand}",
      "- 只想快速跑幾個測試時，用 python -m pytest 開頭的指令。",
    ]
  else:
    # Codex 沒有 Skill 工具，直接讀 skill 檔照著做
    steps = [
      f"- 先讀 {tddCrapDir / 'SKILL.md'}，照它的流程寫：一次一個行為，先寫測試並確認失敗，再寫實作、整理。",
      f"- 品質檢查指令：{qualityCommand}",
    ]
  return ["這個專案照 tdd-crap 開發：", *steps, "- pytest、coverage、radon 已經裝好，不用檢查或安裝。"]


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


def testExecutionLines(testCommand, skipTests):
  if testCommand:
    return ["專案設定的測試指令（流程已將這個指令加入 writer 的允許清單）：", testCommand,
            "- 請照原樣執行；針對性測試依該工具語法追加參數，保留上述指令前綴。",
            "- 測試設定已由流程提供，不需要讀取主專案的 .board/config.json。",
            "- 無法執行或測試失敗時據實回報，不要把未執行說成通過。"]
  if skipTests:
    return ["使用者已明確略過自動測試：不要求執行測試指令；交付時說明未執行測試，不要宣稱通過。"]
  return []


def testFailureLines(lastTest):
  names = lastTest.get("newFailures") or []
  return ["上一輪的自動測試出現基準沒有的失敗，沒有送審；請修正到這些測試通過：",
          *([f"- {name}" for name in names] or ["- （輸出裡分不出測試名稱，請看下面的測試輸出）"]),
          "測試輸出最後 50 行：", "```", lastTest.get("tail", ""), "```"]


def baselineFailureLines(testRun, baselineFailures):
  # 基準就有的失敗不是這個任務造成的，不說清楚的話審查者會因為「測試失敗」退回
  if testRun.get("verdict") == "preexisting":
    return ["這些失敗基準就有（plan 開始時在 baseCommit 跑測試就失敗），不是這個任務造成的，不必因為它們退回：",
            *[f"- {name}" for name in baselineFailures]]
  if testRun.get("verdict") == "unknown":
    return ["基準就有失敗（plan 開始時在 baseCommit 跑測試就失敗），但輸出分不出測試名稱，無法比對哪些失敗是這個任務新增的；"
            "不必因為測試失敗本身退回，請依 diff 判斷這個任務有沒有弄壞東西。"]
  return []


def buildWritePrompt(plan, taskId, roundNumber, reviewMarkdown="", userNote="", qualityCommand="", cli="claude",
                     testCommand="", skipTests=False, lastTest=None):
  parts = [
    taskHeadline(plan, taskId, roundNumber), "",
    "這個任務要滿足的需求：", *requirementLines(plan, taskId), "",
    "plan 裡和這個任務有關的列：", formatRows(taskRows(plan, taskId)),
  ]
  if lastTest and lastTest.get("verdict") == "newFailure":
    # 被測試退回的這一輪沒有審查；更早的審查意見針對的是舊的改動，附上反而會讓 writer 改錯方向
    parts += ["", *testFailureLines(lastTest)]
  elif reviewMarkdown:
    parts += ["", "上一輪審查沒有通過，請修正「必改」的每一條（「建議」可以參考，不強制）：", reviewMarkdown]
  if userNote:
    parts += ["", "使用者補充：" + userNote]
  parts += ["", *testExecutionLines(testCommand, skipTests)]
  if qualityCommand:
    parts += ["", *tddWriteLines(qualityCommand, cli)]
  parts += ["", "改完後要能照表格的「驗證」欄驗證。完成後依格式回覆。"]
  return "\n".join(parts)


def buildResolvePrompt(plan, taskId, roundNumber, conflicts, userNote="", testCommand="", skipTests=False):
  parts = [
    taskHeadline(plan, taskId, roundNumber), "",
    "這一輪要解合併衝突：其他任務已經先合併進 plan 分支，和這個任務改到同樣的檔案。",
    "流程已經把最新的 plan 分支合併進這個資料夾，下列檔案留有衝突標記（<<<<<<<、=======、>>>>>>>）：",
    *[f"- {name}" for name in conflicts], "",
    "請解掉每一個衝突：同時保留這個任務的改動和其他任務已經合併的改動，不要刪掉別人的功能。",
    "兩邊真的互相矛盾、無法同時保留時，status 填 blocked，blockedReason 說明矛盾在哪裡。",
    "解完後確認檔案裡沒有任何衝突標記，依下方專案測試設定驗證。", "",
    "這個任務要滿足的需求：", *requirementLines(plan, taskId), "",
    "plan 裡和這個任務有關的列：", formatRows(taskRows(plan, taskId)),
  ]
  if userNote:
    parts += ["", "使用者補充：" + userNote]
  parts += ["", *testExecutionLines(testCommand, skipTests)]
  parts += ["", "完成後依格式回覆。"]
  return "\n".join(parts)


def buildReviewPrompt(plan, taskId, roundNumber, diff, testRun=None, previousErrors=None, resolvedConflicts=None,
                      tddCrap=False, baselineFailures=None):
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
    parts += baselineFailureLines(testRun, baselineFailures or [])
  if tddCrap:
    parts += ["", *tddReviewLines]
  if len(diff) > maxDiffChars:
    # 太長的 diff 會超出模型能讀的範圍，截斷並明講，讓審查者知道還有沒看到的部分
    diff = diff[:maxDiffChars] + f"\n（diff 太長，只附前 {maxDiffChars} 個字元）"
  parts += ["", "這個任務的 diff：", "```diff", diff or "（沒有 diff）", "```"]
  if previousErrors:
    parts += ["", "你上一次的審查結果不符合規則，請修正後重新回覆：", *[f"- {error}" for error in previousErrors]]
  return "\n".join(parts)
