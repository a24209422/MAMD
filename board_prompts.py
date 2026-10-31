"""給寫程式、審查的 agent 的規則與 prompt。"""
from pathlib import Path

from rtm_graph import planId

maxDiffChars = 150000
maxExtraTextChars = 30000
rowFields = ["requirement", "understanding", "decision", "rejected", "task", "dependsOn", "verify"]
# 舊專案的相容位置；新初始化專案由設定傳入專案內的 skill 路徑。
tddCrapDir = Path.home() / ".claude" / "skills" / "tdd-crap"

# Codex 會自己讀專案的 AGENTS.md，tdd-crap 也會叫 Claude 先讀；那段寫著「不要自己寫程式」，不說清楚會直接回報卡住
writerBaseRules = """你是自動流程裡負責寫程式的 agent。
- 只在目前的資料夾（這個任務的 git worktree）裡改檔案。
- 不要執行 git commit、push、reset、checkout 等 git 指令，commit 由流程負責。
- 不要修改 plans/ 和 .board/ 底下的檔案。
- 只做這個任務要求的事，不要順手重構或加功能。
- AGENTS.md 的 plan 撰寫與送出規則只適用於規劃角色；你直接依已核准任務實作。共通的使用者專屬操作與 .board/ 限制仍有效，不要呼叫 agent-board-init 或 agent-board-plan。
- 完成後，最後的回覆照指定的 JSON 格式：status 填 done，summary 用一句話說明改了什麼。
- 缺少資訊或遇到自己無法解決的問題時，status 填 blocked，blockedReason 寫清楚需要人提供什麼。
- 最後回覆的 summary、blockedReason 一律用正體中文（台灣用語）寫，程式碼、指令、檔名、測試名稱與錯誤訊息保留原文。"""
# Claude 寫程式時只放行測試指令，串接的指令會整串被拒；Codex 在沙箱裡沒有這層限制
claudeShellRule = "\n".join([
  "- 執行指令一次只跑一個，不要用 ;、&&、| 串接；只有被允許的指令（通常是專案的測試指令）能執行，其他會被拒絕。",
  # 實測 PowerShell 的 & "路徑" 呼叫怎麼寫允許規則都比對不到，同一個指令用 Bash 才放行
  '- 測試指令用 Bash 工具執行，不要用 PowerShell 工具；PowerShell 的 `& "..."` 呼叫比對不到允許清單，會被自動拒絕。',
])

reviewerRules = """你是自動流程裡負責審查的 agent，只能讀不能改。
- 依照需求、AI 理解、做法、驗證，判斷這個任務的 diff 有沒有達成。
- 「必改」只放違反需求或驗證、或會讓功能壞掉的問題；每一條的 ref 填它違反的需求編號（例如 R1）或「驗證」。
- 風格、命名、可以更好但不影響需求的意見，一律放「建議」，不會退回。
- 有任何必改，verdict 填 fail；沒有必改，verdict 填 pass。
- AGENTS.md 的 plan 撰寫與送出規則只適用於規劃角色；你只審查目前任務。共通的使用者專屬操作與 .board/ 限制仍有效，不要呼叫 agent-board-init 或 agent-board-plan。
- 最後的回覆照指定的 JSON 格式。"""


def writerRules(cli):
  return (writerBaseRules + "\n" + claudeShellRule) if cli == "claude" else writerBaseRules


qualityReportHint = "讀這個 worktree 的 .quality/report.json 找出超標的函式（測試輸出只附最後 50 行）"


def tddWriteLines(cli, skillPath=None, pythonCommand="python"):
  if cli == "claude" and not skillPath:
    first = "- 先用 Skill 工具載入 tdd-crap，照它的流程寫：一次一個行為，先寫測試並確認失敗，再寫實作、整理。"
  else:
    # Codex 沒有 Skill 工具；專案內的 skill 也直接指檔案，兩種 CLI 讀到的是同一份
    first = f"- 先讀 {Path(skillPath or tddCrapDir) / 'SKILL.md'}，照它的流程寫：一次一個行為，先寫測試並確認失敗，再寫實作、整理。"
  return ["這個專案照 tdd-crap 開發：", first,
          f"- Red、Green 和回歸都用 {pythonCommand} -m pytest 只跑相關的測試檔或測試。",
          # 完整品質檢查約 11 分鐘，超過 Claude Code Bash 工具 10 分鐘的上限會被轉到背景，writer 交卷後還拖住程式
          "- tdd-crap 開發循環第 6 步的品質檢查由流程在交付後執行，不必自己跑。",
          f"- 品質檢查沒過被退回時，{qualityReportHint}。",
          "- pytest、coverage、radon 已經裝好，不用檢查或安裝。"]


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


def extraTextLines(plan):
  # 表格外的規則表、契約、對照值不附的話，writer 在 worktree 裡不一定讀得到 plan 檔；沒有就不加這一段
  extraText = plan.get("extraText", "")
  if not extraText:
    return []
  if len(extraText) > maxExtraTextChars:
    extraText = extraText[:maxExtraTextChars] + f"\n（說明太長，只附前 {maxExtraTextChars} 個字元）"
  return ["", "plan 裡表格以外的說明（整份 plan 共用）：", extraText]


def requirementLines(plan, taskId):
  lines = []
  for reqId in plan["graph"].tasks[taskId]["requirementIds"]:
    node = plan["graph"].nodes.get(reqId, {})
    lines.append(f"- {reqId}：{node.get('text', '')}（AI 理解：{node.get('note') or '未寫'}）")
  return lines or ["- （沒有對應的需求）"]


def taskHeadline(plan, taskId, roundNumber):
  return f"plan {planId(plan)}「{plan['title']}」的任務 {taskId}：{plan['graph'].tasks[taskId]['text']}（第 {roundNumber} 輪）"


def testExecutionLines(testCommand, skipTests, afterMerge=False):
  if testCommand:
    # 合併後才測試的 plan 寫完直接送審，完整測試在合併進 plan 分支後才跑
    fullTestLine = ("- 不要自己跑完整測試，也不要用背景執行跑測試；審查通過、合併進 plan 分支後，流程才會跑一次完整測試，"
                    "出現新增的失敗會退回這個任務修正。" if afterMerge else
                    "- 不要自己跑完整測試，也不要用背景執行跑測試；交付後流程會在這個 worktree 跑一次完整測試，結果決定能不能送審。")
    return ["專案設定的測試指令（流程已將這個指令加入 writer 的允許清單）：", testCommand,
            "- 開發時只跑跟這次改動相關的測試：保留上述指令前綴，依該工具語法追加參數。",
            fullTestLine,
            "- 測試設定已由流程提供，不需要讀取主專案的 .board/config.json。",
            "- 無法執行或測試失敗時據實回報，不要把未執行說成通過。"]
  if skipTests:
    return ["使用者已明確略過自動測試：不要求執行測試指令；交付時說明未執行測試，不要宣稱通過。"]
  return []


def testFailureLines(lastTest, tddCrap=False):
  names = lastTest.get("newFailures") or []
  if lastTest.get("source") == "mergeTest":
    # 合併後測試退回時改動已經在 plan 分支裡，流程把資料夾快轉到最新的 plan 分支，writer 要知道看到的不只自己的改動
    header = (f"這個任務合併進 plan 分支後，完整測試（commit {(lastTest.get('commit') or '')[:7]}）出現新增的失敗，"
              "或上一次退回的失敗還在。這個資料夾已經更新到最新的 plan 分支，包含其他任務已經合併的改動；"
              "請修正到這些測試通過：")
  else:
    header = "上一輪的自動測試出現基準沒有的失敗，沒有送審；請修正到這些測試通過："
  lines = [header,
           *([f"- {name}" for name in names] or ["- （輸出裡分不出測試名稱，請看下面的測試輸出）"])]
  if tddCrap:
    # 覆蓋率或複雜度沒過時 pytest 全部通過，輸出裡沒有失敗的測試名稱，writer 只看輸出會找不到原因
    lines.append(f"- 品質檢查的覆蓋率或複雜度沒過時，輸出裡沒有測試名稱，請{qualityReportHint}。")
  return [*lines, "測試輸出最後 50 行：", "```", lastTest.get("tail", ""), "```"]


def baselineFailureLines(testRun, baselineFailures):
  # 基準就有的失敗不是這個任務造成的，不說清楚的話審查者會因為「測試失敗」退回
  if testRun.get("verdict") == "preexisting":
    return ["這些失敗基準就有（plan 開始時在 baseCommit 跑測試就失敗），不是這個任務造成的，不必因為它們退回：",
            *[f"- {name}" for name in baselineFailures]]
  if testRun.get("verdict") == "unknown":
    return ["基準就有失敗（plan 開始時在 baseCommit 跑測試就失敗），但輸出分不出測試名稱，無法比對哪些失敗是這個任務新增的；"
            "不必因為測試失敗本身退回，請依 diff 判斷這個任務有沒有弄壞東西。"]
  return []


def afterMergeReviewLines(mergeFailures):
  # 新流程審查時還沒跑完整測試；不說清楚的話，審查者會因為驗證欄寫了完整測試而一直退回
  lines = ["這個 plan 的完整測試在審查通過、合併進 plan 分支後才跑，這裡沒有自動測試結果；"
           "不要因為沒有測試結果而退回，也不要自己跑完整測試。",
           "驗證欄裡的完整測試（例如執行 python -m pytest -q 通過）由合併後的測試把關，不是這次審查要確認的項目；"
           "驗證欄的其他項目照常確認。"]
  if mergeFailures:
    lines += ["這一輪是修正合併後測試出現的新增失敗，請確認 diff 針對這些失敗：", *[f"- {name}" for name in mergeFailures]]
  return lines


def buildWritePrompt(plan, taskId, roundNumber, reviewMarkdown="", userNote="", qualityCommand="", cli="claude",
                     testCommand="", skipTests=False, tddSkill=None, pythonCommand="python", lastTest=None,
                     afterMerge=False):
  parts = [
    taskHeadline(plan, taskId, roundNumber), "",
    "這個任務要滿足的需求：", *requirementLines(plan, taskId), "",
    "plan 裡和這個任務有關的列：", formatRows(taskRows(plan, taskId)), *extraTextLines(plan),
  ]
  if lastTest and lastTest.get("verdict") == "newFailure":
    # 被測試退回的這一輪沒有審查；更早的審查意見針對的是舊的改動，附上反而會讓 writer 改錯方向
    parts += ["", *testFailureLines(lastTest, bool(qualityCommand))]
  mergeFixReviewed = lastTest and lastTest.get("source") == "mergeTest"
  if reviewMarkdown and (mergeFixReviewed or not (lastTest and lastTest.get("verdict") == "newFailure")):
    # 合併後測試退回後，修正輪又被審查退回：失敗還要修，審查意見也要改，兩個都附
    parts += ["", "上一輪審查沒有通過，請修正「必改」的每一條（「建議」可以參考，不強制）：", reviewMarkdown]
  if userNote:
    parts += ["", "使用者補充：" + userNote]
  parts += ["", *testExecutionLines(testCommand, skipTests, afterMerge)]
  if qualityCommand:
    parts += ["", *tddWriteLines(cli, tddSkill, pythonCommand)]
  parts += ["", "改完後要能照表格的「驗證」欄驗證。完成後依格式回覆。"]
  return "\n".join(parts)


def buildResolvePrompt(plan, taskId, roundNumber, conflicts, userNote="", testCommand="", skipTests=False,
                       afterMerge=False):
  parts = [
    taskHeadline(plan, taskId, roundNumber), "",
    "這一輪要解合併衝突：其他任務已經先合併進 plan 分支，和這個任務改到同樣的檔案。",
    "流程已經把最新的 plan 分支合併進這個資料夾，下列檔案留有衝突標記（<<<<<<<、=======、>>>>>>>）：",
    *[f"- {name}" for name in conflicts], "",
    "請解掉每一個衝突：同時保留這個任務的改動和其他任務已經合併的改動，不要刪掉別人的功能。",
    "兩邊真的互相矛盾、無法同時保留時，status 填 blocked，blockedReason 說明矛盾在哪裡。",
    "解完後確認檔案裡沒有任何衝突標記，依下方專案測試設定驗證。", "",
    "這個任務要滿足的需求：", *requirementLines(plan, taskId), "",
    "plan 裡和這個任務有關的列：", formatRows(taskRows(plan, taskId)), *extraTextLines(plan),
  ]
  if userNote:
    parts += ["", "使用者補充：" + userNote]
  parts += ["", *testExecutionLines(testCommand, skipTests, afterMerge)]
  parts += ["", "完成後依格式回覆。"]
  return "\n".join(parts)


def resolvedConflictReviewLines(resolvedConflicts):
  if not resolvedConflicts:
    return []
  # 解衝突時最容易出的錯是把別人的改動蓋掉，審查者要知道這一輪的重點
  return ["", f"這一輪是解合併衝突：其他任務先合併進 plan 分支，和這個任務都改了 {'、'.join(resolvedConflicts)}。",
          "這個任務已經把最新的 plan 分支合併進來並解掉衝突。請確認這個任務原本的改動還在，"
          "也沒有刪掉或改壞其他任務已經合併的內容。下面的 diff 是這個任務相對於最新 plan 分支的改動。"]


def testRunReviewLines(testRun, baselineFailures):
  if not testRun:
    return []
  return ["", f"自動測試結果：{'通過' if testRun['passed'] else '失敗'}", "```", testRun.get("tail", ""), "```",
          *baselineFailureLines(testRun, baselineFailures or [])]


def optionalReviewLines(resolvedConflicts, testRun, baselineFailures, afterMerge, mergeFailures, tddCrap):
  # 各區段的順序是提示詞契約的一部分，審查者依這個順序讀
  lines = [*resolvedConflictReviewLines(resolvedConflicts), *testRunReviewLines(testRun, baselineFailures)]
  if afterMerge:
    lines += ["", *afterMergeReviewLines(mergeFailures or [])]
  if tddCrap:
    lines += ["", *tddReviewLines]
  return lines


def reviewDiffLines(diff):
  if len(diff) > maxDiffChars:
    # 太長的 diff 會超出模型能讀的範圍，截斷並明講，讓審查者知道還有沒看到的部分
    diff = diff[:maxDiffChars] + f"\n（diff 太長，只附前 {maxDiffChars} 個字元）"
  return ["", "這個任務的 diff：", "```diff", diff or "（沒有 diff）", "```"]


def previousErrorLines(previousErrors):
  if not previousErrors:
    return []
  return ["", "你上一次的審查結果不符合規則，請修正後重新回覆：", *[f"- {error}" for error in previousErrors]]


def buildReviewPrompt(plan, taskId, roundNumber, diff, testRun=None, previousErrors=None, resolvedConflicts=None,
                      tddCrap=False, baselineFailures=None, afterMerge=False, mergeFailures=None):
  parts = [
    "審查 " + taskHeadline(plan, taskId, roundNumber), "",
    "這個任務要滿足的需求（必改的 ref 只能用這些編號或「驗證」）：", *requirementLines(plan, taskId), "",
    "plan 裡和這個任務有關的列：", formatRows(taskRows(plan, taskId)), *extraTextLines(plan),
    *optionalReviewLines(resolvedConflicts, testRun, baselineFailures, afterMerge, mergeFailures, tddCrap),
    *reviewDiffLines(diff), *previousErrorLines(previousErrors),
  ]
  return "\n".join(parts)
