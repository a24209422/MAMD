"""agent 輸出的格式，以及審查結果的規則檢查。"""

verifyRef = "驗證"

writeResultSchema = {
  "type": "object",
  "additionalProperties": False,
  "required": ["status", "summary", "blockedReason"],
  "properties": {
    "status": {"type": "string", "enum": ["done", "blocked"]},
    "summary": {"type": "string"},
    "blockedReason": {"type": "string"},
  },
}

reviewResultSchema = {
  "type": "object",
  "additionalProperties": False,
  "required": ["verdict", "mustFix", "suggest"],
  "properties": {
    "verdict": {"type": "string", "enum": ["pass", "fail"]},
    "mustFix": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["ref", "text"],
        "properties": {"ref": {"type": "string"}, "text": {"type": "string"}},
      },
    },
    "suggest": {"type": "array", "items": {"type": "string"}},
  },
}


def validateWriteResult(result):
  if not isinstance(result, dict):
    return ["回覆不是 JSON 物件"]
  if result.get("status") not in ("done", "blocked"):
    return ["status 只能是 done 或 blocked"]
  if result["status"] == "blocked" and not str(result.get("blockedReason", "")).strip():
    return ["blocked 時一定要寫 blockedReason"]
  return []


def validateReview(result, allowedRefs):
  if not isinstance(result, dict):
    return ["回覆不是 JSON 物件"]
  errors = []
  verdict = result.get("verdict")
  mustFix = result.get("mustFix")
  if verdict not in ("pass", "fail"):
    errors.append("verdict 只能是 pass 或 fail")
  if not isinstance(mustFix, list):
    return errors + ["mustFix 必須是陣列"]
  if verdict == "fail" and not mustFix:
    errors.append("不通過時至少要有一條必改")
  if verdict == "pass" and mustFix:
    errors.append("通過時不能有必改")
  # 必改一定要對到這個任務的需求或驗證，審查才不會變成照審查者自己的喜好退回
  allowed = {ref.upper() for ref in allowedRefs} | {verifyRef}
  for item in mustFix:
    item = item if isinstance(item, dict) else {}
    ref = str(item.get("ref", "")).strip().upper()
    if ref not in allowed:
      errors.append(f"必改的 ref「{ref}」不是這個任務的需求編號或「驗證」（可用：{'、'.join(sorted(allowed))}）")
    if not str(item.get("text", "")).strip():
      errors.append("必改的 text 不能是空的")
  if not isinstance(result.get("suggest", []), list):
    errors.append("suggest 必須是陣列")
  return errors


def reviewFileName(planIdValue, taskId, roundNumber):
  return f"{planIdValue}-{taskId}-r{roundNumber}.md"


def renderReviewMarkdown(planIdValue, taskId, roundNumber, result):
  verdictText = "通過" if result["verdict"] == "pass" else "不通過"
  lines = [f"# {planIdValue} {taskId} 第 {roundNumber} 輪審查：{verdictText}", "", "## 必改"]
  lines += [f"- [{item['ref']}] {item['text']}" for item in result["mustFix"]] or ["- （無）"]
  lines += ["", "## 建議"]
  lines += [f"- {text}" for text in result.get("suggest", [])] or ["- （無）"]
  return "\n".join(lines) + "\n"
