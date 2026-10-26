"""假的 codex exec：照情境檔決定行為，事件和 codex exec --json 相同格式，結果寫到 -o 指定的檔案。寫程式、審查都會。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import argValue, demoDelay, emit, failEarly, reviewResults, startCall, writeFiles  # noqa: E402


def main():
  call = startCall()
  # exec resume 時，對話編號在最後的「-」前一個參數
  threadId = sys.argv[-2] if "resume" in sys.argv else f"thread-{call['taskId']}-{call['callNumber']}"
  emit({"type": "thread.started", "thread_id": threadId})
  failEarly(call["action"], "ERROR: rate limit reached")
  if call["kind"] == "review":
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "command_execution", "command": "git diff",
                                             "aggregated_output": "", "exit_code": 0}})
    demoDelay()
    result = reviewResults[call["action"]]
  else:
    status, blockedReason, changed = writeFiles(call["action"], call["taskId"], call["callNumber"])
    if changed:
      emit({"type": "item.completed", "item": {"id": "item_0", "type": "file_change", "status": "completed",
                                               "changes": [{"path": str(Path(name).resolve()), "kind": "update"}
                                                           for name in changed]}})
    demoDelay()
    result = {"status": status, "summary": f"{call['taskId']} 完成", "blockedReason": blockedReason}
  text = json.dumps(result, ensure_ascii=False)
  Path(argValue("-o")).write_text(text, encoding="utf-8")
  emit({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": text}})
  emit({"type": "turn.completed", "usage": {"input_tokens": 18000 + call["callNumber"] * 500, "cached_input_tokens": 0,
                                            "output_tokens": 90}})


if __name__ == "__main__":
  main()
