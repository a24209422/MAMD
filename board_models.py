"""從本機 CLI 讀取模型選單與推理強度；不發送對話。"""
import json
import os
import queue
import subprocess
import threading
import time

from board_agents import killProcessTree, resolveCommand
from board_core import BoardError, modelNamePattern, reasoningEffortPattern


class CliChannel:
  def __init__(self, argv, cwd, timeout):
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    self.process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                    env={**os.environ, "PYTHONUTF8": "1"}, **options)
    self.deadline = time.monotonic() + timeout
    self.lines = queue.Queue()
    self.reader = threading.Thread(target=self.readLines, daemon=True)
    self.reader.start()

  def readLines(self):
    for line in self.process.stdout:
      self.lines.put(line)
    self.lines.put(None)

  def send(self, message):
    self.process.stdin.write(json.dumps(message) + "\n")
    self.process.stdin.flush()

  def receive(self):
    while True:
      try:
        line = self.lines.get(timeout=max(0, self.deadline - time.monotonic()))
      except queue.Empty:
        raise BoardError("讀取模型清單逾時，請確認 CLI 已登入後重新整理") from None
      if line is None:
        raise BoardError("CLI 提早結束，請確認 CLI 已安裝、已登入且支援模型查詢")
      try:
        message = json.loads(line)
      except ValueError:
        continue  # CLI 的啟動警告不是協定訊息
      if isinstance(message, dict):
        return message

  def request(self, message, requestId, claude=False):
    self.send(message)
    while True:
      response = self.receive()
      if claude:
        response = response.get("response", {}) if response.get("type") == "control_response" else {}
        responseId = response.get("request_id")
      else:
        responseId = response.get("id")
      if responseId != requestId:
        continue
      if response.get("error"):
        raise BoardError("CLI 無法讀取模型清單：" + str(response["error"]))
      return response.get("response" if claude else "result", {})

  def close(self):
    if self.process.poll() is None:
      killProcessTree(self.process.pid)
    self.process.wait(timeout=5)
    self.reader.join(timeout=1)
    self.process.stdout.close()
    self.process.stdin.close()


def codexModels(channel):
  channel.request({"id": 1, "method": "initialize", "params": {
    "clientInfo": {"name": "agent-board", "version": "1.0"}}}, 1)
  channel.send({"method": "initialized"})
  rows, cursors, cursor = [], set(), None
  for page in range(20):
    params = {"includeHidden": False, "limit": 100, "cursor": cursor}
    result = channel.request({"id": page + 2, "method": "model/list", "params": params}, page + 2)
    rows.extend(modelRows(result, "data"))
    cursor = result.get("nextCursor")
    if not cursor:
      return rows
    if cursor in cursors:
      break
    cursors.add(cursor)
  raise BoardError("CLI 模型清單分頁異常，請更新 CLI 後重新整理")


def modelRows(result, key):
  rows = result.get(key) if isinstance(result, dict) else None
  if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
    raise BoardError("CLI 回傳的模型清單格式不符，請更新 CLI 後重新整理")
  return rows


def claudeModels(channel):
  result = channel.request({"type": "control_request", "request_id": "models-init",
                            "request": {"subtype": "initialize"}}, "models-init", claude=True)
  return modelRows(result, "models")


def modelEfforts(row, cli):
  if cli == "claude":
    levels = row.get("supportedEffortLevels", []) if row.get("supportsEffort") else []
    return [{"value": level, "description": ""} for level in levels if isinstance(level, str) and reasoningEffortPattern.fullmatch(level)]
  return [{"value": item["reasoningEffort"], "description": item.get("description", "")}
          for item in row.get("supportedReasoningEfforts", [])
          if isinstance(item.get("reasoningEffort"), str) and reasoningEffortPattern.fullmatch(item["reasoningEffort"])]


def normalizeModel(row, cli):
  model = row.get("value") if cli == "claude" else row.get("model")
  if not isinstance(model, str) or not modelNamePattern.fullmatch(model):
    raise BoardError("CLI 回傳了無法使用的模型名稱，請更新 CLI 後重新整理")
  return {"id": model, "displayName": row.get("displayName") or model, "description": row.get("description", ""),
          "resolvedModel": row.get("resolvedModel") or model, "efforts": modelEfforts(row, cli),
          "defaultEffort": row.get("defaultReasoningEffort"),
          "isDefault": model == "default" if cli == "claude" else bool(row.get("isDefault"))}


def discoverModels(projectDir, cli, command=None, timeout=20):
  if cli not in ("claude", "codex"):
    raise BoardError("CLI 只能是 claude 或 codex")
  args = ["app-server"] if cli == "codex" else [
    "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
    "--settings", '{"disableAllHooks":true}', "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
  ]
  try:
    channel = CliChannel(resolveCommand(command, cli) + args, projectDir, timeout)
    try:
      rows = claudeModels(channel) if cli == "claude" else codexModels(channel)
      models = [normalizeModel(row, cli) for row in rows if not row.get("hidden")]
    finally:
      channel.close()
  except OSError as error:
    raise BoardError(f"無法啟動 {cli} 讀取模型清單：{error}") from error
  if not models:
    raise BoardError("CLI 沒有回傳可選模型，請確認已登入或更新 CLI 後重新整理")
  return models


class ModelCatalog:
  def __init__(self, projectDir, ttl=300):
    self.projectDir, self.ttl = projectDir, ttl
    self.cache, self.keyLocks = {}, {}
    self.lock = threading.Lock()

  def get(self, cli, command=None, refresh=False):
    key = (cli, tuple(command or []))
    with self.lock:
      keyLock = self.keyLocks.setdefault(key, threading.Lock())
    with keyLock:
      cached = self.cache.get(key)
      if not refresh and cached and time.monotonic() - cached[0] < self.ttl:
        return cached[1]
      try:
        models = discoverModels(self.projectDir, cli, command)
      except BoardError as error:
        self.cache.pop(key, None)
        return {"ok": False, "cli": cli, "models": [], "message": str(error)}
      result = {"ok": True, "cli": cli, "models": models}
      self.cache[key] = (time.monotonic(), result)
      return result


def validateChoice(models, model, effort):
  if not model and not effort:
    return
  selected = next((item for item in models if item["id"] == model), None)
  if selected is None:
    raise BoardError("請從 CLI 模型清單選擇模型；清單可能已更新，請重新整理")
  if effort and effort not in [item["value"] for item in selected["efforts"]]:
    raise BoardError("這個模型不支援所選的推理強度，請重新選擇")
