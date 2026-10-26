"""假的 codex exec：照情境檔決定審查結果，寫到 -o 指定的檔案。"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_claude import argValue, emit, nextAction, taskPattern  # noqa: E402

results = {
  "pass": {"verdict": "pass", "mustFix": [], "suggest": ["可以再精簡"]},
  "fail": {"verdict": "fail", "mustFix": [{"ref": "R1", "text": "還沒達成需求"}], "suggest": []},
  "invalid": {"verdict": "fail", "mustFix": [], "suggest": []},
}


def main():
  sys.stdout.reconfigure(encoding="utf-8")
  prompt = sys.stdin.read()
  planIdValue, taskId = taskPattern.search(prompt).groups()
  action, callNumber, scenarioDir = nextAction("review", planIdValue, taskId, "pass")
  (scenarioDir / f"call-review-{planIdValue}-{taskId}-{callNumber}.json").write_text(
    json.dumps({"argv": sys.argv[1:], "prompt": prompt}, ensure_ascii=False), encoding="utf-8")
  emit({"type": "thread.started", "thread_id": f"thread-{taskId}-{callNumber}"})
  if action == "crash":
    print("fake crash", flush=True)
    sys.exit(1)
  if action == "quota":
    print("ERROR: rate limit reached", flush=True)
    sys.exit(1)
  if action == "hang":
    time.sleep(120)
  emit({"type": "item.completed", "item": {"id": "item_0", "type": "command_execution", "command": "git diff",
                                           "aggregated_output": "", "exit_code": 0}})
  # 展示儀表板時讓審查慢一點，才看得到審查中和最近動作
  time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
  result = json.dumps(results[action], ensure_ascii=False)
  Path(argValue("-o")).write_text(result, encoding="utf-8")
  emit({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": result}})
  emit({"type": "turn.completed", "usage": {"input_tokens": 18000 + callNumber * 500, "cached_input_tokens": 0,
                                            "output_tokens": 90}})


if __name__ == "__main__":
  main()
