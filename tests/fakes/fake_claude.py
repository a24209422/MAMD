"""假的 claude CLI：照情境檔決定行為，輸出和 claude -p --output-format stream-json 相同格式。寫程式、審查都會。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import argValue, demoDelay, emit, failEarly, reviewResults, startCall, writeFiles  # noqa: E402


def emitTool(name, fileName, callNumber):
  # usage 照真實 Claude 的欄位，儀表板用它算 Context；每多呼叫一次，對話就長一點
  emit({"type": "assistant", "message": {
    "content": [{"type": "tool_use", "name": name, "input": {"file_path": fileName}}],
    "usage": {"input_tokens": 2, "cache_read_input_tokens": 30000 + callNumber * 20000,
              "cache_creation_input_tokens": 500, "output_tokens": 20}}})


def main():
  call = startCall()
  sessionId = argValue("--session-id") or argValue("--resume")
  emit({"type": "system", "subtype": "init", "session_id": sessionId})
  failEarly(call["action"], "Error: You've hit your usage limit")
  if call["kind"] == "review":
    emitTool("Read", "README.md", call["callNumber"])
    demoDelay()
    structured = reviewResults[call["action"]]
  else:
    status, blockedReason, changed = writeFiles(call["action"], call["taskId"], call["callNumber"])
    for fileName in changed:
      emitTool("Edit" if call["action"] == "resolve" else "Write", fileName, call["callNumber"])
    demoDelay()
    structured = {"status": status, "summary": f"{call['taskId']} 完成", "blockedReason": blockedReason}
  emit({"type": "result", "subtype": "success", "is_error": False, "session_id": sessionId,
        "result": json.dumps(structured, ensure_ascii=False), "structured_output": structured})


if __name__ == "__main__":
  main()
