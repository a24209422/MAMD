"""假的 claude CLI：照情境檔決定行為，輸出和 claude -p --output-format stream-json 相同格式。"""
import json
import os
import re
import sys
import time
from pathlib import Path

taskPattern = re.compile(r"plan ([A-Za-z]+\d+)「.*?」的任務 ([A-Za-z]+\d+)：")


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


def emitTool(name, fileName, callNumber):
  # usage 照真實 Claude 的欄位，儀表板用它算 Context；每多呼叫一次，對話就長一點
  emit({"type": "assistant", "message": {
    "content": [{"type": "tool_use", "name": name, "input": {"file_path": fileName}}],
    "usage": {"input_tokens": 2, "cache_read_input_tokens": 30000 + callNumber * 20000,
              "cache_creation_input_tokens": 500, "output_tokens": 20}}})


def main():
  sys.stdout.reconfigure(encoding="utf-8")
  prompt = sys.stdin.read()
  planIdValue, taskId = taskPattern.search(prompt).groups()
  action, callNumber, scenarioDir = nextAction("write", planIdValue, taskId, "done")
  (scenarioDir / f"call-write-{planIdValue}-{taskId}-{callNumber}.json").write_text(
    json.dumps({"argv": sys.argv[1:], "prompt": prompt}, ensure_ascii=False), encoding="utf-8")
  sessionId = argValue("--session-id") or argValue("--resume")
  emit({"type": "system", "subtype": "init", "session_id": sessionId})
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
    print("Error: You've hit your usage limit", flush=True)
    sys.exit(1)
  if action == "hang":
    time.sleep(120)
  if action == "slow":
    time.sleep(1)
  status, blockedReason = "done", ""
  if action == "blocked":
    status, blockedReason = "blocked", "缺少 API 位址"
  elif action == "resolve":
    # 拿掉衝突標記、保留兩邊的內容，模擬 Claude 把衝突解掉
    for path in Path(".").glob("*.txt"):
      lines = path.read_text(encoding="utf-8").splitlines()
      kept = [line for line in lines if not line.startswith(("<<<<<<<", "=======", ">>>>>>>"))]
      path.write_text("\n".join(kept) + "\n", encoding="utf-8")
      emitTool("Edit", path.name, callNumber)
  elif action in ("done", "slow", "conflict", "conflictcjk"):
    fileName = {"conflict": "shared.txt", "conflictcjk": "說明.txt"}.get(action, f"{taskId}.txt")
    Path(fileName).write_text(f"{taskId} 第 {callNumber} 次\n", encoding="utf-8")
    emitTool("Write", fileName, callNumber)
  # 展示儀表板時讓每一步慢一點，才看得到寫程式中、最近動作和 Context
  time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
  structured = {"status": status, "summary": f"{taskId} 完成", "blockedReason": blockedReason}
  emit({"type": "result", "subtype": "success", "is_error": False, "session_id": sessionId,
        "result": json.dumps(structured, ensure_ascii=False), "structured_output": structured})


if __name__ == "__main__":
  main()
