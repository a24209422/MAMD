from board_prompts import (buildResolvePrompt, buildReviewPrompt, buildWritePrompt, formatRows, maxDiffChars,
                           maxExtraTextChars, reviewerRules, tddCrapDir, writerRules)
from rtm_graph import analyzePlan

ruleTable = "## 規則表\n\n| 輸入 | 輸出 |\n|---|---|\n| a | 1 |\n| b | 2 |"
extraHeading = "plan 裡表格以外的說明（整份 plan 共用）："


def samplePlan(writePlan, after=""):
  return analyzePlan(writePlan("0001-demo.md", [
    "| R1 需求一 | 理解一 | D1 做法一 | 逐張呼叫：太慢 | T1 任務一 | | 驗證一 |",
    "| R1 | | D1 | | T2 任務二 | T1 | 驗證二 |",
  ], planId="P0001", after=after))


def tableOnlyPlan(tmp_path):
  path = tmp_path / "plans" / "0001-demo.md"
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text("---\nid: P0001\n---\n\n# 測試 plan\n\n"
                  "| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |\n|---|---|---|---|---|---|---|\n"
                  "| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | | 驗證一 |\n", encoding="utf-8")
  return analyzePlan(path)


def allPrompts(plan, taskId="T1"):
  return [buildWritePrompt(plan, taskId, 1), buildResolvePrompt(plan, taskId, 2, ["calc.py"]),
          buildReviewPrompt(plan, taskId, 1, "+x")]


def test_promptsIncludeTextOutsideTableAfterOwnRows(writePlan):
  for prompt in allPrompts(samplePlan(writePlan, after=ruleTable)):
    assert extraHeading in prompt
    assert ruleTable in prompt
    # 接在任務的列後面，其他任務的列不會跟著附上
    assert prompt.index("plan 裡和這個任務有關的列：") < prompt.index(extraHeading) < prompt.index(ruleTable)
    assert "驗證二" not in prompt


def test_promptsWithoutTextOutsideTableHaveNoExtraSection(tmp_path):
  plan = tableOnlyPlan(tmp_path)
  assert plan["extraText"] == ""
  for prompt in allPrompts(plan):
    assert extraHeading not in prompt
  lines = buildWritePrompt(plan, "T1", 1).splitlines()
  # 標題、表頭、分隔線、一列，之後直接接原本的下一段，跟加這個功能之前一樣
  rowsEnd = lines.index("plan 裡和這個任務有關的列：") + 3
  assert lines[rowsEnd].startswith("| R1 需求一")
  assert lines[rowsEnd + 1:] == ["", "", "改完後要能照表格的「驗證」欄驗證。完成後依格式回覆。"]


def test_longTextOutsideTableIsTruncated(writePlan):
  for prompt in allPrompts(samplePlan(writePlan, after="y" * (maxExtraTextChars + 10))):
    assert f"說明太長，只附前 {maxExtraTextChars} 個字元" in prompt
    assert "y" * (maxExtraTextChars - 100) in prompt
    assert "y" * (maxExtraTextChars + 1) not in prompt


def test_writePromptIncludesTaskRequirementsAndOwnRows(writePlan):
  prompt = buildWritePrompt(samplePlan(writePlan), "T2", 1)
  assert prompt.splitlines()[0] == "plan P0001「測試 plan」的任務 T2：任務二（第 1 輪）"
  assert "- R1：需求一（AI 理解：理解一）" in prompt
  assert "驗證二" in prompt
  assert "驗證一" not in prompt


def test_writePromptWithReviewAndNote(writePlan):
  prompt = buildWritePrompt(samplePlan(writePlan), "T1", 2, reviewMarkdown="## 必改\n- [R1] 沒有提示", userNote="提示用中文")
  assert prompt.splitlines()[0].endswith("（第 2 輪）")
  assert "- [R1] 沒有提示" in prompt
  assert "使用者補充：提示用中文" in prompt


def test_writePromptAfterTestReturnShowsNewFailuresInsteadOfReview(writePlan):
  lastTest = {"passed": False, "tail": "FAILED t.py::test_new - boom\n1 failed", "verdict": "newFailure",
              "newFailures": ["t.py::test_new"]}
  prompt = buildWritePrompt(samplePlan(writePlan), "T1", 3, reviewMarkdown="## 必改\n- [R1] 舊意見", lastTest=lastTest)
  assert "基準沒有的失敗" in prompt and "- t.py::test_new" in prompt
  assert "FAILED t.py::test_new - boom\n1 failed" in prompt
  assert "舊意見" not in prompt and "上一輪審查沒有通過" not in prompt


def test_writePromptAfterTestReturnWithoutNamesPointsToOutput(writePlan):
  lastTest = {"passed": False, "tail": "測試超時", "verdict": "newFailure", "newFailures": []}
  prompt = buildWritePrompt(samplePlan(writePlan), "T1", 2, lastTest=lastTest)
  assert "分不出測試名稱" in prompt and "測試超時" in prompt


def test_writePromptKeepsReviewWhenLastTestWasNotReturned(writePlan):
  for lastTest in ({"passed": True, "tail": "ok", "verdict": "passed", "newFailures": []},
                   {"passed": False, "tail": "1 failed"}):
    prompt = buildWritePrompt(samplePlan(writePlan), "T1", 2, reviewMarkdown="## 必改\n- [R1] 意見", lastTest=lastTest)
    assert "- [R1] 意見" in prompt and "基準沒有的失敗" not in prompt


def test_reviewPromptExplainsPreexistingAndUnknownFailures(writePlan):
  preexisting = {"passed": False, "tail": "1 failed", "verdict": "preexisting", "newFailures": []}
  prompt = buildReviewPrompt(samplePlan(writePlan), "T1", 1, "+x", testRun=preexisting,
                             baselineFailures=["t.py::test_old"])
  assert "基準就有" in prompt and "- t.py::test_old" in prompt and "不必因為" in prompt
  prompt = buildReviewPrompt(samplePlan(writePlan), "T1", 1, "+x", testRun=preexisting,
                             baselineFailures=["品質門檻 conductor.py::Conductor.mergeOneTask"])
  assert "基準就有" in prompt and "- 品質門檻 conductor.py::Conductor.mergeOneTask" in prompt
  unknown = {"passed": False, "tail": "1 failed", "verdict": "unknown", "newFailures": []}
  prompt = buildReviewPrompt(samplePlan(writePlan), "T1", 1, "+x", testRun=unknown)
  assert "基準就有" in prompt and "無法比對" in prompt and "不必因為" in prompt
  for testRun in ({"passed": True, "tail": "ok", "verdict": "passed"}, {"passed": False, "tail": "1 failed"}):
    assert "基準就有" not in buildReviewPrompt(samplePlan(writePlan), "T1", 1, "+x", testRun=testRun)


def test_reviewPromptIncludesDiffTestAndErrors(writePlan):
  prompt = buildReviewPrompt(samplePlan(writePlan), "T1", 1, "+print('hi')",
                             testRun={"passed": False, "tail": "1 failed"}, previousErrors=["不通過時至少要有一條必改"])
  assert prompt.splitlines()[0] == "審查 plan P0001「測試 plan」的任務 T1：任務一（第 1 輪）"
  assert "+print('hi')" in prompt
  assert "自動測試結果：失敗" in prompt and "1 failed" in prompt
  assert "- 不通過時至少要有一條必改" in prompt


def test_longDiffIsTruncated(writePlan):
  prompt = buildReviewPrompt(samplePlan(writePlan), "T1", 1, "x" * (maxDiffChars + 10))
  assert "diff 太長，只附前" in prompt
  assert "x" * (maxDiffChars + 1) not in prompt


def test_rulesMentionKeyConstraints():
  assert "git commit" in writerRules("claude") and "blocked" in writerRules("claude")
  assert "git commit" in writerRules("codex") and "blocked" in writerRules("codex")
  assert "ref" in reviewerRules and "建議" in reviewerRules
  # Codex 會自己讀 AGENTS.md，那段寫著「不要自己寫程式」
  assert "AGENTS.md" in writerRules("codex") and "AGENTS.md" in reviewerRules


def test_onlyClaudeWriterIsToldCommandsAreRestricted():
  assert "串接" in writerRules("claude")
  assert "串接" not in writerRules("codex")


def test_bothWritersReportInTraditionalChinese():
  for cli in ("claude", "codex"):
    rules = writerRules(cli)
    assert "summary、blockedReason 一律用正體中文" in rules
    assert "測試名稱與錯誤訊息保留原文" in rules


def test_onlyClaudeWriterIsToldToRunTestsWithBash():
  assert "測試指令用 Bash 工具執行，不要用 PowerShell 工具" in writerRules("claude")
  assert "Bash 工具" not in writerRules("codex")


def test_formatRowsEscapesPipes():
  row = {"requirement": "R1 a|b", "understanding": "", "decision": "", "rejected": "", "task": "T1",
         "dependsOn": "", "verify": ""}
  assert "R1 a\\|b" in formatRows([row])


def test_resolvePromptListsConflictsAndKeepsBothSides(writePlan):
  prompt = buildResolvePrompt(samplePlan(writePlan), "T2", 2, ["calc.py", "test_calc.py"], userNote="保留兩邊")
  # 第一行和寫程式的 prompt 相同格式，假 agent 和記錄都靠它認出是哪個任務
  assert prompt.splitlines()[0] == "plan P0001「測試 plan」的任務 T2：任務二（第 2 輪）"
  assert "- calc.py" in prompt and "- test_calc.py" in prompt
  assert "<<<<<<<" in prompt
  assert "保留" in prompt and "blocked" in prompt
  assert "驗證二" in prompt
  assert "使用者補充：保留兩邊" in prompt


def test_reviewPromptExplainsResolvedConflicts(writePlan):
  prompt = buildReviewPrompt(samplePlan(writePlan), "T2", 2, "+x", resolvedConflicts=["calc.py"])
  assert "解合併衝突" in prompt and "calc.py" in prompt
  assert "解合併衝突" not in buildReviewPrompt(samplePlan(writePlan), "T2", 2, "+x")


def test_writePromptAddsTddCrapStepsOnlyWhenEnabled(writePlan):
  command = "python C:/skills/tdd-crap/scripts/quality.py --source src -- tests"
  prompt = buildWritePrompt(samplePlan(writePlan), "T1", 1, qualityCommand=command)
  assert "Skill 工具載入 tdd-crap" in prompt
  # 品質檢查交給流程跑（P0008 D1），tdd 段落不再列出指令要 writer 照跑
  assert command not in prompt and "python -m pytest" in prompt
  # 最後一行固定是交回結果的提醒，tdd 的段落要放在它前面
  assert prompt.splitlines()[-1] == "改完後要能照表格的「驗證」欄驗證。完成後依格式回覆。"
  assert "tdd-crap" not in buildWritePrompt(samplePlan(writePlan), "T1", 1)


reviewHeaderLines = [
  "審查 plan P0001「測試 plan」的任務 T1：任務一（第 1 輪）", "",
  "這個任務要滿足的需求（必改的 ref 只能用這些編號或「驗證」）：", "- R1：需求一（AI 理解：理解一）", "",
  "plan 裡和這個任務有關的列：",
  "| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |", "|---|---|---|---|---|---|---|",
  "| R1 需求一 | 理解一 | D1 做法一 |  | T1 任務一 |  | 驗證一 |",
]
tddReviewTextLines = [
  "這個專案照 tdd-crap 開發，自動測試就是品質檢查（每個函式的覆蓋率與循環複雜度）。",
  "- 品質檢查沒通過時，讀 worktree 裡的 .quality/report.json 找出超標的函式；上面附的輸出只有最後 50 行。",
  "- 確認測試真的在驗證行為：用放寬斷言、刪除測試、加 # pragma: no cover 排除程式碼等方式通過門檻的，列為必改，ref 填「驗證」。",
]


def test_reviewPromptFullOutputForOldFlowStaysUnchanged(tmp_path):
  # 鎖住舊流程每個可選區段的文字與順序，重構時提示詞不能有任何差異
  prompt = buildReviewPrompt(tableOnlyPlan(tmp_path), "T1", 1, "x" * (maxDiffChars + 10),
                             testRun={"passed": False, "tail": "1 failed", "verdict": "preexisting"},
                             previousErrors=["錯誤一", "錯誤二"], resolvedConflicts=["calc.py", "test_calc.py"],
                             tddCrap=True, baselineFailures=["t.py::test_old"])
  assert prompt.splitlines() == [
    *reviewHeaderLines, "",
    "這一輪是解合併衝突：其他任務先合併進 plan 分支，和這個任務都改了 calc.py、test_calc.py。",
    "這個任務已經把最新的 plan 分支合併進來並解掉衝突。請確認這個任務原本的改動還在，"
    "也沒有刪掉或改壞其他任務已經合併的內容。下面的 diff 是這個任務相對於最新 plan 分支的改動。", "",
    "自動測試結果：失敗", "```", "1 failed", "```",
    "這些失敗基準就有（plan 開始時在 baseCommit 跑測試就失敗），不是這個任務造成的，不必因為它們退回：",
    "- t.py::test_old", "",
    *tddReviewTextLines, "",
    "這個任務的 diff：", "```diff", "x" * maxDiffChars, f"（diff 太長，只附前 {maxDiffChars} 個字元）", "```", "",
    "你上一次的審查結果不符合規則，請修正後重新回覆：", "- 錯誤一", "- 錯誤二",
  ]


def test_reviewPromptFullOutputForAfterMergeStaysUnchanged(tmp_path):
  prompt = buildReviewPrompt(tableOnlyPlan(tmp_path), "T1", 1, "+x", tddCrap=True, afterMerge=True,
                             mergeFailures=["t.py::test_new"])
  assert prompt.splitlines() == [
    *reviewHeaderLines, "",
    "這個 plan 的完整測試在審查通過、合併進 plan 分支後才跑，這裡沒有自動測試結果；"
    "不要因為沒有測試結果而退回，也不要自己跑完整測試。",
    "驗證欄裡的完整測試（例如執行 python -m pytest -q 通過）由合併後的測試把關，不是這次審查要確認的項目；"
    "驗證欄的其他項目照常確認。",
    "這一輪是修正合併後測試出現的新增失敗，請確認 diff 針對這些失敗：", "- t.py::test_new", "",
    *tddReviewTextLines, "",
    "這個任務的 diff：", "```diff", "+x", "```",
  ]


def test_reviewPromptFullOutputWithoutOptionsStaysUnchanged(tmp_path):
  prompt = buildReviewPrompt(tableOnlyPlan(tmp_path), "T1", 1, "",
                             testRun={"passed": True, "tail": "ok", "verdict": "passed"})
  assert prompt.splitlines() == [
    *reviewHeaderLines, "",
    "自動測試結果：通過", "```", "ok", "```", "",
    "這個任務的 diff：", "```diff", "（沒有 diff）", "```",
  ]


def test_reviewPromptAddsTestQualityRuleOnlyWhenEnabled(writePlan):
  prompt = buildReviewPrompt(samplePlan(writePlan), "T1", 1, "+x", testRun={"passed": False, "tail": "FAILED"},
                             tddCrap=True)
  assert ".quality/report.json" in prompt
  assert "# pragma: no cover" in prompt and "「驗證」" in prompt
  assert prompt.index("tdd-crap") < prompt.index("這個任務的 diff：")
  assert "tdd-crap" not in buildReviewPrompt(samplePlan(writePlan), "T1", 1, "+x")


def test_writerRunsOnlyRelatedTestsAndLeavesFullRunToFlow(writePlan):
  for prompt in (buildWritePrompt(samplePlan(writePlan), "T1", 1, testCommand="npm test"),
                 buildResolvePrompt(samplePlan(writePlan), "T1", 2, ["calc.py"], testCommand="npm test")):
    assert "npm test" in prompt
    assert "只跑跟這次改動相關的測試" in prompt
    assert "不要自己跑完整測試" in prompt and "背景執行" in prompt
    assert "交付後流程會在這個 worktree 跑一次完整測試" in prompt
    assert "請照原樣執行" not in prompt


def test_tddWriterLeavesQualityCheckToFlow(writePlan):
  command = "python C:/skills/tdd-crap/scripts/quality.py --source src -- tests"
  for cli in ("claude", "codex"):
    prompt = buildWritePrompt(samplePlan(writePlan), "T1", 1, qualityCommand=command, cli=cli, testCommand=command,
                              pythonCommand='"project-python"')
    assert '"project-python" -m pytest' in prompt
    assert "第 6 步的品質檢查由流程在交付後執行" in prompt
    assert ".quality/report.json" in prompt
    # 品質檢查指令只出現在專案測試指令那一段，不再要求 writer 照原樣執行
    assert prompt.count(command) == 1
    assert "品質檢查指令要照原樣執行" not in prompt


def test_writePromptAfterQualityReturnPointsToReport(writePlan):
  lastTest = {"passed": False, "tail": "FAILED: .quality/report.json", "verdict": "newFailure", "newFailures": []}
  tdd = buildWritePrompt(samplePlan(writePlan), "T1", 2, qualityCommand="quality", lastTest=lastTest)
  # 提示要接在退回原因那段，writer 一讀到失敗就知道去哪裡找超標的函式
  failureSection = tdd[tdd.index("基準沒有的失敗"):tdd.index("這個專案照 tdd-crap 開發")]
  assert "覆蓋率或複雜度" in failureSection and ".quality/report.json" in failureSection
  plain = buildWritePrompt(samplePlan(writePlan), "T1", 2, lastTest=lastTest)
  assert "覆蓋率或複雜度" not in plain


def test_codexWriterReadsTddSkillFile(writePlan):
  command = "python C:/skills/tdd-crap/scripts/quality.py --source src -- tests"
  prompt = buildWritePrompt(samplePlan(writePlan), "T1", 1, qualityCommand=command, cli="codex")
  assert str(tddCrapDir / "SKILL.md") in prompt and command not in prompt
  assert "Skill 工具" not in prompt
  assert prompt.splitlines()[-1] == "改完後要能照表格的「驗證」欄驗證。完成後依格式回覆。"


def test_afterMergeWriterIsToldFullTestsRunAfterMerge(writePlan):
  plan = samplePlan(writePlan)
  for prompt in (buildWritePrompt(plan, "T1", 1, testCommand="npm test", afterMerge=True),
                 buildResolvePrompt(plan, "T1", 2, ["calc.py"], testCommand="npm test", afterMerge=True)):
    assert "合併進 plan 分支後，流程才會跑一次完整測試" in prompt
    assert "交付後流程會在這個 worktree 跑一次完整測試" not in prompt
    assert "只跑跟這次改動相關的測試" in prompt and "不要自己跑完整測試" in prompt


mergeFailure = {"source": "mergeTest", "verdict": "newFailure", "newFailures": ["t.py::test_new"],
                "commit": "abcdef1234567", "tail": "FAILED t.py::test_new - boom"}


def test_mergeFixPromptShowsFailuresOnLatestPlanAndLaterReview(writePlan):
  plan = samplePlan(writePlan)
  prompt = buildWritePrompt(plan, "T1", 2, lastTest=mergeFailure, testCommand="npm test", afterMerge=True)
  assert "合併進 plan 分支後，完整測試（commit abcdef1）出現新增的失敗" in prompt
  assert "已經更新到最新的 plan 分支" in prompt
  assert "- t.py::test_new" in prompt and "FAILED t.py::test_new - boom" in prompt
  assert "基準沒有的失敗" not in prompt and "上一輪審查沒有通過" not in prompt
  reviewed = buildWritePrompt(plan, "T1", 3, reviewMarkdown="## 必改\n- [R1] 還沒修好", lastTest=mergeFailure,
                              testCommand="npm test", afterMerge=True)
  assert "- t.py::test_new" in reviewed and "- [R1] 還沒修好" in reviewed


def test_afterMergeReviewHasNoTestResultAndListsFailuresToFix(writePlan):
  plan = samplePlan(writePlan)
  prompt = buildReviewPrompt(plan, "T1", 1, "+x", afterMerge=True)
  assert "自動測試結果：" not in prompt
  assert "合併進 plan 分支後才跑" in prompt and "不要自己跑完整測試" in prompt
  assert "驗證欄裡的完整測試" in prompt and "不是這次審查要確認的項目" in prompt
  assert "修正合併後測試" not in prompt
  fix = buildReviewPrompt(plan, "T1", 2, "+x", afterMerge=True, mergeFailures=["t.py::test_new"])
  assert "這一輪是修正合併後測試出現的新增失敗" in fix and "- t.py::test_new" in fix
  assert "合併進 plan 分支後才跑" not in buildReviewPrompt(plan, "T1", 1, "+x")
