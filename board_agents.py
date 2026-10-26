"""啟動 agent 子程式、讀取輸出串流、解析 Claude／Codex 的結果。"""
import collections
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from board_review import claudeResultSchema

# 不用 "limit reached"：Claude 的 "Context limit reached" 是對話太長，不是額度用完
quotaPatterns = ["usage limit", "rate limit", "rate_limit", "quota", "hit your limit", "out of extra usage", "額度"]
deniedShellPatterns = [
  "git commit *", "git push *", "git reset *", "git checkout *", "git switch *",
  "git rebase *", "git merge *", "rm *", "del *", "Remove-Item *",
]
# Windows 上 Claude 的 shell 工具是 PowerShell、其他平台是 Bash；同一條規則兩種都要列才擋得住
deniedClaudeTools = [f"{tool}({pattern})" for pattern in deniedShellPatterns for tool in ("Bash", "PowerShell")]


def killProcessTree(pid):
  if os.name == "nt":
    # Windows 沒有程式群組訊號；taskkill /T 才會連子程式（例如 codex.cmd 底下的 node）一起結束
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    return
  try:
    os.killpg(os.getpgid(pid), signal.SIGKILL)
  except ProcessLookupError:
    pass


class AgentJob:
  def __init__(self, argv, cwd, stdinText, logPath, lineParser, timeoutSeconds):
    self.argv = argv
    self.cwd = cwd
    self.stdinText = stdinText
    self.logPath = Path(logPath)
    self.lineParser = lineParser
    self.timeoutSeconds = timeoutSeconds
    self.activity = {"lastActivity": None, "action": ""}
    self.tail = collections.deque(maxlen=200)
    self.exitCode = None
    self.timedOut = False
    self.killed = False
    self.process = None
    self.reader = None
    self.startedAt = None

  def start(self):
    self.logPath.parent.mkdir(parents=True, exist_ok=True)
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    # 子程式若是 Python（測試用的假 agent），Windows 管線預設 cp950，中文 stdin／輸出會亂碼；
    # 真正的 claude.exe／codex 不受影響
    childEnv = {**os.environ, "PYTHONUTF8": "1"}
    self.process = subprocess.Popen(self.argv, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                    env=childEnv, **options)
    self.startedAt = time.time()
    self.activity["lastActivity"] = self.startedAt
    # 先開始讀輸出再寫 stdin，避免輸出塞滿管線時兩邊互等
    self.reader = threading.Thread(target=self.readOutput, daemon=True)
    self.reader.start()
    try:
      # prompt 走 stdin，避免 Windows 指令列長度上限（審查時 prompt 含整份 diff）
      self.process.stdin.write(self.stdinText)
      self.process.stdin.close()
    except OSError:
      pass
    return self

  def readOutput(self):
    with open(self.logPath, "a", encoding="utf-8") as log:
      for rawLine in self.process.stdout:
        log.write(rawLine)
        log.flush()
        line = rawLine.rstrip("\n")
        self.tail.append(line)
        self.activity["lastActivity"] = time.time()
        try:
          self.lineParser(line, self.activity)
        except (ValueError, AttributeError, TypeError) as error:
          self.activity["parseError"] = str(error)

  def poll(self):
    if self.process.poll() is None:
      if time.time() - self.startedAt > self.timeoutSeconds:
        self.timedOut = True
        self.kill()
      return False
    # 程式結束後，讀取執行緒可能還在把最後幾行寫進紀錄
    self.reader.join(timeout=5)
    self.exitCode = self.process.returncode
    return True

  def kill(self):
    if self.process.poll() is not None:
      return
    self.killed = True
    killProcessTree(self.process.pid)
    try:
      self.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
      self.process.kill()

  def tailText(self, lines=20):
    return "\n".join(list(self.tail)[-lines:])


def resolveCommand(configured, name):
  if configured:
    return list(configured)
  # Windows 上 codex 是 npm 產生的 codex.cmd，要用 which 找到完整路徑才啟動得了
  return [shutil.which(name) or name]


def buildClaudeArgv(command, sessionId, resume, systemRules, allowedTools=()):
  argv = list(command) + [
    "-p", "--output-format", "stream-json", "--verbose",
    "--json-schema", json.dumps(claudeResultSchema, ensure_ascii=False),
    "--permission-mode", "acceptEdits", "--permission-prompts", "none",
    "--append-system-prompt", systemRules,
    "--disallowedTools", *deniedClaudeTools,
  ]
  # acceptEdits 加 prompts none 下，沒列入允許的 shell 指令一律被拒，所以要明確放行
  if allowedTools:
    argv += ["--allowedTools", *allowedTools]
  # session 參數維持在最後兩項，呼叫端與測試都依賴這個位置
  return argv + (["--resume", sessionId] if resume else ["--session-id", sessionId])


def describeToolUse(name, toolInput):
  target = toolInput.get("file_path") or toolInput.get("command") or toolInput.get("pattern") or ""
  return f"{name} {str(target)[:80]}".strip()


def parseClaudeLine(line, activity):
  if not line.startswith("{"):
    return
  event = json.loads(line)
  if event.get("session_id"):
    activity["sessionId"] = event["session_id"]
  if event.get("type") == "assistant":
    for block in event.get("message", {}).get("content", []):
      if block.get("type") == "tool_use":
        activity["action"] = describeToolUse(block.get("name", ""), block.get("input", {}))
  elif event.get("type") == "result":
    activity["result"] = event


def claudeFinalResult(activity):
  result = activity.get("result")
  if not result:
    return None, "沒有收到 result 事件"
  if result.get("is_error"):
    return None, str(result.get("result") or result.get("subtype") or "Claude 回報錯誤")
  structured = result.get("structured_output")
  if isinstance(structured, dict):
    return structured, ""
  # 格式化結果沒放在 structured_output 時，退一步解析 result 文字
  try:
    parsed = json.loads(result.get("result") or "")
  except json.JSONDecodeError:
    return None, "result 不是 JSON：" + str(result.get("result"))[:200]
  return (parsed, "") if isinstance(parsed, dict) else (None, "result 不是 JSON 物件")


def buildCodexArgv(command, worktree, schemaPath, outPath):
  return list(command) + ["exec", "-C", str(worktree), "-s", "read-only", "--json",
                          "--output-schema", str(schemaPath), "-o", str(outPath), "-"]


def parseCodexLine(line, activity):
  if not line.startswith("{"):
    return
  event = json.loads(line)
  eventType = event.get("type", "")
  if eventType == "thread.started":
    activity["threadId"] = event.get("thread_id")
  elif eventType.startswith("item."):
    item = event.get("item", {})
    activity["action"] = f"{item.get('type', '')} {str(item.get('command', ''))[:80]}".strip()
  elif eventType in ("turn.failed", "error"):
    activity["error"] = str(event.get("error") or event.get("message") or "")


def codexFinalResult(outPath):
  outPath = Path(outPath)
  if not outPath.exists():
    return None, "沒有產生 -o 輸出檔"
  text = outPath.read_text(encoding="utf-8").strip()
  try:
    parsed = json.loads(text)
  except json.JSONDecodeError:
    return None, "輸出不是 JSON：" + text[:200]
  return (parsed, "") if isinstance(parsed, dict) else (None, "輸出不是 JSON 物件")


def looksLikeQuota(text):
  lowered = text.lower()
  return any(pattern in lowered for pattern in quotaPatterns)
