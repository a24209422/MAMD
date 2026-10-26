from board_review import (claudeResultSchema, codexReviewSchema, renderReviewMarkdown, reviewFileName,
                          validateClaudeResult, validateReview)


def failResult(*mustFix, suggest=()):
  return {"verdict": "fail", "mustFix": [{"ref": ref, "text": text} for ref, text in mustFix], "suggest": list(suggest)}


def test_schemasRequireEveryField():
  assert claudeResultSchema["required"] == ["status", "summary", "blockedReason"]
  assert claudeResultSchema["additionalProperties"] is False
  assert codexReviewSchema["required"] == ["verdict", "mustFix", "suggest"]
  assert codexReviewSchema["properties"]["mustFix"]["items"]["additionalProperties"] is False


def test_validateClaudeResult():
  assert validateClaudeResult({"status": "done", "summary": "好了", "blockedReason": ""}) == []
  assert validateClaudeResult({"status": "blocked", "summary": "", "blockedReason": ""}) == ["blocked 時一定要寫 blockedReason"]
  assert validateClaudeResult({"status": "maybe"}) == ["status 只能是 done 或 blocked"]
  assert validateClaudeResult("done") == ["回覆不是 JSON 物件"]


def test_validFailAndPass():
  assert validateReview(failResult(("R1", "沒有提示")), ["R1"]) == []
  assert validateReview({"verdict": "pass", "mustFix": [], "suggest": ["可以更短"]}, ["R1"]) == []


def test_failNeedsMustFixAndPassMustNotHaveIt():
  assert validateReview({"verdict": "fail", "mustFix": [], "suggest": []}, ["R1"]) == ["不通過時至少要有一條必改"]
  passWithFix = {"verdict": "pass", "mustFix": [{"ref": "R1", "text": "x"}], "suggest": []}
  assert validateReview(passWithFix, ["R1"]) == ["通過時不能有必改"]


def test_refMustBeTaskRequirementOrVerify():
  errors = validateReview(failResult(("R9", "別的需求")), ["R1"])
  assert len(errors) == 1 and "R9" in errors[0]
  assert validateReview(failResult(("驗證", "畫面上看不到提示")), ["R1"]) == []
  assert validateReview(failResult(("r1", "小寫也可以")), ["R1"]) == []


def test_emptyMustFixTextIsRejected():
  assert validateReview(failResult(("R1", "  ")), ["R1"]) == ["必改的 text 不能是空的"]


def test_nonObjectReview():
  assert validateReview(["pass"], ["R1"]) == ["回覆不是 JSON 物件"]


def test_renderReviewMarkdown():
  markdown = renderReviewMarkdown("P0001", "T2", 1, failResult(("R1", "沒有提示"), suggest=["文字改短"]))
  assert markdown == (
    "# P0001 T2 第 1 輪審查：不通過\n\n## 必改\n- [R1] 沒有提示\n\n## 建議\n- 文字改短\n"
  )
  passed = renderReviewMarkdown("P0001", "T2", 2, {"verdict": "pass", "mustFix": [], "suggest": []})
  assert passed == "# P0001 T2 第 2 輪審查：通過\n\n## 必改\n- （無）\n\n## 建議\n- （無）\n"


def test_reviewFileName():
  assert reviewFileName("P0001", "T2", 3) == "P0001-T2-r3.md"
