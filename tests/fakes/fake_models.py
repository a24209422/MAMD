"""只接受初始化與模型查詢；收到對話就失敗，避免測試意外使用推理服務。"""
import json
import sys
import time

scenario = sys.argv[1]


def emit(message):
  print(json.dumps(message), flush=True)


for line in sys.stdin:
  message = json.loads(line)
  method = message.get("method")
  if method == "initialized":
    continue
  if scenario == "timeout":
    time.sleep(60)
    continue
  if scenario == "exit":
    sys.exit(1)
  if message.get("type") == "control_request":
    assert message["request"]["subtype"] == "initialize"
    response = {"request_id": message["request_id"], "subtype": "success", "response": {"models": [
      {"value": "default", "displayName": "Default", "resolvedModel": "claude-test-new"},
      {"value": "opus", "displayName": "Opus New", "resolvedModel": "claude-test-new", "supportsEffort": True,
       "supportedEffortLevels": ["high", "max"]},
      {"value": "claude-test-old", "displayName": "Old", "supportsEffort": False,
       "supportedEffortLevels": ["high"]},
    ]}}
    if scenario == "error":
      response = {"request_id": message["request_id"], "subtype": "error", "error": "未登入"}
    if scenario == "empty":
      response["response"]["models"] = []
    if scenario == "bad":
      response["response"]["models"] = "invalid"
    if scenario == "unsafe":
      response["response"]["models"] = [{"value": "bad&name", "displayName": "Unsafe"}]
    emit({"type": "control_response", "response": response})
  elif method == "initialize":
    print("startup warning (not JSON)", flush=True)
    emit({"id": message["id"], "result": {}})
  elif method == "model/list":
    if scenario == "error":
      emit({"id": message["id"], "error": {"message": "未登入"}})
      continue
    data = [{"id": "internal-id", "model": "gpt-test-v2", "displayName": "Test V2", "description": "From CLI",
             "isDefault": True, "defaultReasoningEffort": "low", "supportedReasoningEfforts": [
               {"reasoningEffort": "low", "description": "Fast"}, {"reasoningEffort": "ultra", "description": "Deep"}]}]
    cursor = "page2"
    if message["params"].get("cursor"):
      data = [{"model": "gpt-test-small", "displayName": "Small"}, {"model": "hidden", "hidden": True}]
      cursor = "page2" if scenario == "loop" else None
    if scenario == "empty":
      data, cursor = [], None
    emit({"id": message["id"], "result": {"data": data, "nextCursor": cursor}})
  else:
    raise AssertionError("Unexpected request: " + str(method))
