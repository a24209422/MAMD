"""從 agent 的 log 取出儀表板要顯示的資料。Claude 是 stream-json、Codex 是 exec --json，都是一行一個 JSON。"""
import json
import re
from datetime import datetime

tailBytes = 256 * 1024
detailBytes = 2 * 1024 * 1024
maxFieldChars = 2000

# Codex 還沒有任何動作、或在回覆最後結果時，卡片依階段顯示
codexIdleActions = {"write": "寫程式中", "review": "審查中"}
codexReplyActions = {"write": "回覆寫程式結果", "review": "回覆審查結果"}
maxActionChars = 120


def readTail(path, limit):
  size = path.stat().st_size
  with path.open("rb") as handle:
    if size > limit:
      handle.seek(size - limit)
    data = handle.read()
  text = data.decode("utf-8", errors="replace")
  cut = size > limit
  if cut:
    # 從檔案中間開始讀，第一行多半不完整，直接丟掉
    text = text.split("\n", 1)[1] if "\n" in text else ""
  return text, cut


def parseLines(text):
  records = []
  for line in text.splitlines():
    line = line.strip()
    if not line.startswith("{"):
      continue
    try:
      record = json.loads(line)
    except json.JSONDecodeError:
      # 最後一行可能還在寫，下次讀就完整了
      continue
    if isinstance(record, dict):
      records.append(record)
  return records


def fileName(path):
  # log 裡的路徑是 Windows 寫法，兩種斜線都要能切
  return re.split(r"[\\/]", path)[-1]


def toolTarget(toolInput):
  if not isinstance(toolInput, dict):
    return ""
  if toolInput.get("file_path"):
    return fileName(str(toolInput["file_path"]))
  for key in ("command", "pattern"):
    if toolInput.get(key):
      return str(toolInput[key])
  return ""


def fileChangeNames(item):
  # Codex 改檔案的事件列出每個檔案的完整路徑，卡片上只放檔名
  changes = item.get("changes") if isinstance(item.get("changes"), list) else []
  return "、".join(fileName(str(change.get("path", ""))) for change in changes if isinstance(change, dict))


def shortCommand(command):
  # Codex 在 Windows 用 PowerShell 寫檔時，檔案內容整段在指令裡；卡片只放第一行的開頭
  lines = str(command).splitlines()
  first = lines[0] if lines else ""
  cut = len(lines) > 1 or len(first) > maxActionChars
  return first[:maxActionChars] + ("…" if cut else "")


def claudeContextTokens(usage):
  # 一則訊息送出時整段對話的大小 = 這次新讀的 + 從快取讀的 + 寫進快取的
  return (usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
          + usage.get("cache_creation_input_tokens", 0))


def contentBlocks(record):
  content = (record.get("message") or {}).get("content")
  return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def summarizeRecords(records, cli, kind):
  action, tokens = None, None
  for record in records:
    recordType = record.get("type")
    if cli == "claude" and recordType == "assistant":
      for block in contentBlocks(record):
        # StructuredOutput 是交回結果用的內部工具，不算 agent 在做的事
        if block.get("type") == "tool_use" and block.get("name") != "StructuredOutput":
          action = f"{block.get('name', '')} {toolTarget(block.get('input'))}".strip()
      usage = (record.get("message") or {}).get("usage")
      if isinstance(usage, dict):
        tokens = claudeContextTokens(usage)
    elif cli == "codex" and recordType == "item.completed":
      item = record.get("item") or {}
      if item.get("type") == "command_execution":
        action = f"執行 {shortCommand(item.get('command', ''))}".strip()
      elif item.get("type") == "file_change":
        action = f"修改 {fileChangeNames(item)}".strip()
      elif item.get("type") == "agent_message":
        action = codexReplyActions[kind]
    elif cli == "codex" and recordType == "turn.completed":
      usage = record.get("usage")
      if isinstance(usage, dict) and usage.get("input_tokens") is not None:
        tokens = usage["input_tokens"]
  if cli == "codex" and action is None:
    action = codexIdleActions[kind]
  return action, tokens


def logSummary(path, cli, kind):
  # 卡片每 2 秒更新一次，只看 log 最後一段就夠
  text, _ = readTail(path, tailBytes)
  action, tokens = summarizeRecords(parseLines(text), cli, kind)
  modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
  return {"action": action, "lastActivity": modified, "tokens": tokens}


def clip(value):
  text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
  if len(text) > maxFieldChars:
    return text[:maxFieldChars], True
  return text, False


def toolResultText(content):
  if isinstance(content, str):
    return content
  if isinstance(content, list):
    return "\n".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
  return ""


def claudeDetail(records):
  messages, tools, toolsById = [], [], {}
  for record in records:
    recordType = record.get("type")
    if recordType == "assistant":
      for block in contentBlocks(record):
        if block.get("type") == "text" and str(block.get("text", "")).strip():
          messages.append({"text": block["text"]})
        elif block.get("type") == "tool_use" and block.get("name") != "StructuredOutput":
          inputText, inputCut = clip(block.get("input"))
          tool = {"name": block.get("name", ""), "target": toolTarget(block.get("input")), "input": inputText,
                  "result": "", "truncated": inputCut}
          tools.append(tool)
          toolsById[block.get("id")] = tool
    elif recordType == "user":
      for block in contentBlocks(record):
        tool = toolsById.get(block.get("tool_use_id")) if block.get("type") == "tool_result" else None
        if tool is not None:
          tool["result"], resultCut = clip(toolResultText(block.get("content")))
          tool["truncated"] = tool["truncated"] or resultCut
    elif recordType == "result":
      structured = record.get("structured_output")
      structured = structured if isinstance(structured, dict) else {}
      if structured.get("summary"):
        messages.append({"text": structured["summary"]})
      elif structured:
        # 審查的結果沒有 summary，整份列出來，和 Codex 審查時看到的回覆一樣
        messages.append({"text": json.dumps(structured, ensure_ascii=False)})
  return messages, tools


def codexDetail(records):
  messages, tools = [], []
  for record in records:
    if record.get("type") != "item.completed":
      continue
    item = record.get("item") or {}
    if item.get("type") == "agent_message" and item.get("text"):
      messages.append({"text": item["text"]})
    elif item.get("type") == "command_execution":
      command = str(item.get("command", ""))
      resultText, resultCut = clip(str(item.get("aggregated_output", "")))
      tools.append({"name": "執行", "target": command, "input": command, "result": resultText,
                    "truncated": resultCut})
    elif item.get("type") == "file_change":
      inputText, inputCut = clip(item.get("changes") or [])
      tools.append({"name": "修改", "target": fileChangeNames(item), "input": inputText, "result": "",
                    "truncated": inputCut})
  return messages, tools


def logDetail(path, cli, kind):
  text, cut = readTail(path, detailBytes)
  records = parseLines(text)
  messages, tools = claudeDetail(records) if cli == "claude" else codexDetail(records)
  _, tokens = summarizeRecords(records, cli, kind)
  return {"messages": messages, "tools": tools, "tokens": tokens, "truncated": cut}


def findLog(logsDir, planIdValue, taskId, roundNumber, kind):
  if not logsDir.exists():
    return None
  # 審查結果格式不符時會重跑（review1、review2…）；檔名最後兩段是啟動時間，取最新的
  stage = "write-" if kind == "write" else "review*-"
  paths = sorted(logsDir.glob(f"{planIdValue}-{taskId}-r{roundNumber}-{stage}*.log"),
                 key=lambda path: path.stem.split("-")[-2:])
  return paths[-1] if paths else None
