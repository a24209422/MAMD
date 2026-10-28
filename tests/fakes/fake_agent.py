"""假 agent 的共用部分：讀 prompt、照情境檔決定行為、做寫程式的檔案改動。假 Claude、假 Codex 都用它。"""
import json
import os
import re
import sys
import time
from pathlib import Path

taskPattern = re.compile(r"plan ([A-Za-z]+\d+)「.*?」的任務 ([A-Za-z]+\d+)：")
# 審查的 prompt 有一行以「審查 plan」開頭；Codex 收到的 prompt 前面還有規則，所以逐行找
reviewPattern = re.compile(r"^審查 plan ", re.MULTILINE)
reviewResults = {
  "pass": {"verdict": "pass", "mustFix": [], "suggest": ["可以再精簡"]},
  "fail": {"verdict": "fail", "mustFix": [{"ref": "R1", "text": "還沒達成需求"}], "suggest": []},
  "invalid": {"verdict": "fail", "mustFix": [], "suggest": []},
}


def nextAction(kind, planIdValue, taskId, defaultAction):
  scenarioPath = Path(os.environ["FAKE_SCENARIO"])
  scenario = json.loads(scenarioPath.read_text(encoding="utf-8"))
  counterPath = scenarioPath.parent / f"count-{kind}-{planIdValue}-{taskId}.txt"
  callNumber = int(counterPath.read_text()) + 1 if counterPath.exists() else 1
  counterPath.write_text(str(callNumber))
  actions = scenario.get(kind, {}).get(taskId, [defaultAction])
  return actions[min(callNumber, len(actions)) - 1], callNumber, scenarioPath.parent


def argValue(flag):
  return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None


def emit(event):
  print(json.dumps(event, ensure_ascii=False), flush=True)


def startCall():
  sys.stdout.reconfigure(encoding="utf-8")
  prompt = sys.stdin.read()
  planIdValue, taskId = taskPattern.search(prompt).groups()
  kind = "review" if reviewPattern.search(prompt) else "write"
  action, callNumber, scenarioDir = nextAction(kind, planIdValue, taskId, "pass" if kind == "review" else "done")
  (scenarioDir / f"call-{kind}-{planIdValue}-{taskId}-{callNumber}.json").write_text(
    json.dumps({"argv": sys.argv[1:], "prompt": prompt,
                "env": {key: os.environ.get(key) for key in ("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                                                           "DISABLE_AUTO_COMPACT", "DISABLE_COMPACT")}},
               ensure_ascii=False), encoding="utf-8")
  return {"kind": kind, "action": action, "callNumber": callNumber, "taskId": taskId}


def failEarly(action, quotaMessage):
  if action == "crash":
    print("fake crash", flush=True)
    sys.exit(1)
  if action == "crashmention":
    # JSON 行裡提到額度字樣（例如讀到的檔案內容），不是真的額度用完
    emit({"type": "assistant", "message": {"content": [
      {"type": "tool_use", "name": "Read", "input": {"file_path": "usage limit and rate limit"}}]}})
    print("fake crash", flush=True)
    sys.exit(1)
  if action == "crashlong":
    # 當掉時印出很長的輸出，例如整段 stack trace
    for index in range(3):
      print(f"line{index} " + "x" * 1000, flush=True)
    sys.exit(1)
  if action == "quota":
    print(quotaMessage, flush=True)
    sys.exit(1)
  if action == "hang":
    time.sleep(120)
  if action == "slow":
    time.sleep(1)


def writeFiles(action, taskId, callNumber):
  """照情境改檔案，回傳 (status, blockedReason, 改到的檔名)。"""
  if action == "blocked":
    return "blocked", "缺少 API 位址", []
  changed = []
  if action == "resolve":
    # 拿掉衝突標記、保留兩邊的內容，模擬 agent 把衝突解掉
    for path in Path(".").glob("*.txt"):
      lines = path.read_text(encoding="utf-8").splitlines()
      kept = [line for line in lines if not line.startswith(("<<<<<<<", "=======", ">>>>>>>"))]
      path.write_text("\n".join(kept) + "\n", encoding="utf-8")
      changed.append(path.name)
  elif action in ("done", "slow", "conflict", "conflictcjk"):
    fileName = {"conflict": "shared.txt", "conflictcjk": "說明.txt"}.get(action, f"{taskId}.txt")
    Path(fileName).write_text(f"{taskId} 第 {callNumber} 次\n", encoding="utf-8")
    changed.append(fileName)
  return "done", "", changed


def demoDelay():
  # 展示儀表板時讓每一步慢一點，才看得到寫程式中、審查中、最近動作和 Context
  time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
