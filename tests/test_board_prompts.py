from board_prompts import (buildResolvePrompt, buildReviewPrompt, buildWritePrompt, claudeRules, codexRules,
                           formatRows, maxDiffChars)
from rtm_graph import analyzePlan


def samplePlan(writePlan):
  return analyzePlan(writePlan("0001-demo.md", [
    "| R1 需求一 | 理解一 | D1 做法一 | 逐張呼叫：太慢 | T1 任務一 | | 驗證一 |",
    "| R1 | | D1 | | T2 任務二 | T1 | 驗證二 |",
  ], planId="P0001"))


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
  assert "git commit" in claudeRules and "blocked" in claudeRules
  assert "ref" in codexRules and "建議" in codexRules


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
  assert command in prompt and "python -m pytest" in prompt
  assert "AGENTS.md" in prompt
  # 最後一行固定是交回結果的提醒，tdd 的段落要放在它前面
  assert prompt.splitlines()[-1] == "改完後要能照表格的「驗證」欄驗證。完成後依格式回覆。"
  assert "tdd-crap" not in buildWritePrompt(samplePlan(writePlan), "T1", 1)


def test_reviewPromptAddsTestQualityRuleOnlyWhenEnabled(writePlan):
  prompt = buildReviewPrompt(samplePlan(writePlan), "T1", 1, "+x", testRun={"passed": False, "tail": "FAILED"},
                             tddCrap=True)
  assert ".quality/report.json" in prompt
  assert "# pragma: no cover" in prompt and "「驗證」" in prompt
  assert prompt.index("tdd-crap") < prompt.index("這個任務的 diff：")
  assert "tdd-crap" not in buildReviewPrompt(samplePlan(writePlan), "T1", 1, "+x")
