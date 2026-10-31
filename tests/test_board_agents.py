import json
import os
import subprocess
import sys
import time

import board_agents
from board_review import reviewResultSchema


def waitJob(job, timeout=20):
  deadline = time.time() + timeout
  while not job.poll():
    assert time.time() < deadline, "等太久"
    time.sleep(0.05)


def isAlive(pid):
  if os.name == "nt":
    # tasklist 輸出是系統地區編碼（cp950），用 locale 解碼並容忍無法解的位元組
    return str(pid) in subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True,
                                      errors="replace").stdout
  try:
    os.kill(pid, 0)
    return True
  except OSError:
    return False


def collectLines(line, activity):
  activity.setdefault("lines", []).append(line)


def test_jobPassesStdinAndCapturesOutput(tmp_path):
  script = "import sys; print('got', sys.stdin.read().strip()); print('第二行')"
  job = board_agents.AgentJob([sys.executable, "-c", script], tmp_path, "你好", tmp_path / "log.txt", collectLines, 30).start()
  waitJob(job)
  assert job.exitCode == 0
  assert job.activity["lines"] == ["got 你好", "第二行"]
  assert "第二行" in (tmp_path / "log.txt").read_text(encoding="utf-8")
  assert job.tailText(1) == "第二行"


def test_jobTimeoutKillsProcess(tmp_path):
  job = board_agents.AgentJob([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, "",
                              tmp_path / "log.txt", collectLines, 0.5).start()
  waitJob(job)
  assert job.timedOut is True
  assert job.killed is True


def test_killStopsChildProcesses(tmp_path):
  script = ("import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n")
  job = board_agents.AgentJob([sys.executable, "-c", script], tmp_path, "", tmp_path / "log.txt", collectLines, 60).start()
  deadline = time.time() + 10
  while not job.activity.get("lines"):
    assert time.time() < deadline
    time.sleep(0.05)
  childPid = int(job.activity["lines"][0])
  job.kill()
  waitJob(job)
  time.sleep(0.5)
  assert job.killed is True
  assert isAlive(childPid) is False


lingerAfterResult = ("import json, subprocess, sys, time\n"
                     "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                     "print(json.dumps({'type': 'system', 'subtype': 'init', 'child': child.pid}), flush=True)\n"
                     "print(json.dumps({'type': 'result', 'is_error': False, 'structured_output': {'status': 'done'}}),"
                     " flush=True)\n"
                     "time.sleep(60)\n")


def childPidOf(logPath):
  return json.loads(logPath.read_text(encoding="utf-8").splitlines()[0])["child"]


def test_jobStopsProcessTreeAfterResultGracePeriod(tmp_path, monkeypatch):
  monkeypatch.setattr(board_agents, "resultGraceSeconds", 0.5)
  job = board_agents.AgentJob([sys.executable, "-c", lingerAfterResult], tmp_path, "", tmp_path / "log.txt",
                              board_agents.parseClaudeLine, 60).start()
  started = time.time()
  waitJob(job)
  # 交出結果後只等寬限時間，不會等到 60 秒的逾時或子程式自己結束
  assert time.time() - started < 15
  assert job.stoppedAfterResult is True
  assert job.killed is False and job.timedOut is False
  assert board_agents.claudeFinalResult(job.activity) == ({"status": "done"}, "")
  time.sleep(0.5)
  assert isAlive(childPidOf(tmp_path / "log.txt")) is False


def test_jobWaitsForGracePeriodBeforeStopping(tmp_path, monkeypatch):
  monkeypatch.setattr(board_agents, "resultGraceSeconds", 30)
  job = board_agents.AgentJob([sys.executable, "-c", lingerAfterResult], tmp_path, "", tmp_path / "log.txt",
                              board_agents.parseClaudeLine, 60).start()
  deadline = time.time() + 10
  while not job.activity.get("result"):
    assert time.time() < deadline
    time.sleep(0.05)
  # 寬限時間內讓 CLI 自己收尾（例如寫完 session），還不結束
  assert job.poll() is False and job.stoppedAfterResult is False
  job.kill()
  waitJob(job)


def test_stopAfterResultFallsBackToKillWhenTreeDoesNotExit(tmp_path, monkeypatch):
  class StubbornProcess:
    pid = 12345
    killedDirectly = False

    def wait(self, timeout):
      raise subprocess.TimeoutExpired("agent", timeout)

    def kill(self):
      self.killedDirectly = True

  treeKills = []
  monkeypatch.setattr(board_agents, "killProcessTree", treeKills.append)
  job = board_agents.AgentJob(["agent"], tmp_path, "", tmp_path / "log.txt", board_agents.parseClaudeLine, 30)
  job.process = StubbornProcess()
  job.stopAfterResult()
  # 程式樹結束不了時至少結束 agent 本身，conductor 才不會一直等它
  assert treeKills == [12345] and job.process.killedDirectly is True
  assert job.stoppedAfterResult is True and job.killed is False


class FakeProcess:
  pid = 12345

  def __init__(self, returncode=None, exitsOnWait=True):
    self.returncode = returncode
    self.exitsOnWait = exitsOnWait
    self.killedDirectly = False

  def poll(self):
    return self.returncode

  def wait(self, timeout):
    if not self.exitsOnWait:
      raise subprocess.TimeoutExpired("agent", timeout)
    self.returncode = -9

  def kill(self):
    self.killedDirectly = True


def fakeJob(tmp_path, monkeypatch, process):
  treeKills = []
  monkeypatch.setattr(board_agents, "killProcessTree", treeKills.append)
  job = board_agents.AgentJob(["agent"], tmp_path, "", tmp_path / "log.txt", board_agents.parseClaudeLine, 30)
  job.process = process
  return job, treeKills


def test_killIgnoresProcessThatAlreadyExited(tmp_path, monkeypatch):
  job, treeKills = fakeJob(tmp_path, monkeypatch, FakeProcess(returncode=0))
  job.kill()
  # 已經結束的程式不算被中斷，conductor 才會照常收下這一輪的結果
  assert job.killed is False and treeKills == []


def test_killStopsTreeAndWaitsForAgent(tmp_path, monkeypatch):
  job, treeKills = fakeJob(tmp_path, monkeypatch, FakeProcess())
  job.kill()
  assert job.killed is True and treeKills == [12345]
  assert job.process.killedDirectly is False


def test_killFallsBackToKillWhenTreeDoesNotExit(tmp_path, monkeypatch):
  job, treeKills = fakeJob(tmp_path, monkeypatch, FakeProcess(exitsOnWait=False))
  job.kill()
  assert job.killed is True and treeKills == [12345]
  assert job.process.killedDirectly is True


def test_killProcessTreeUsesTaskkillOnWindows(monkeypatch):
  calls = []
  monkeypatch.setattr(board_agents.os, "name", "nt")
  monkeypatch.setattr(board_agents.subprocess, "run", lambda argv, **options: calls.append(argv))
  board_agents.killProcessTree(321)
  assert calls == [["taskkill", "/PID", "321", "/T", "/F"]]


def test_killProcessTreeKillsProcessGroupOnPosix(monkeypatch):
  killed = []
  monkeypatch.setattr(board_agents.os, "name", "posix")
  monkeypatch.setattr(board_agents.os, "getpgid", lambda pid: pid + 1, raising=False)
  monkeypatch.setattr(board_agents.os, "killpg", lambda group, sig: killed.append((group, sig)), raising=False)
  monkeypatch.setattr(board_agents.signal, "SIGKILL", 9, raising=False)
  board_agents.killProcessTree(321)
  assert killed == [(322, 9)]


def test_killProcessTreeIgnoresProcessThatIsGoneOnPosix(monkeypatch):
  def missingGroup(pid):
    raise ProcessLookupError(pid)

  monkeypatch.setattr(board_agents.os, "name", "posix")
  monkeypatch.setattr(board_agents.os, "getpgid", missingGroup, raising=False)
  # Windows 沒有 killpg、SIGKILL，取屬性在算參數之前，不補會先丟 AttributeError
  monkeypatch.setattr(board_agents.os, "killpg", lambda group, sig: None, raising=False)
  monkeypatch.setattr(board_agents.signal, "SIGKILL", 9, raising=False)
  # 程式已經結束時不算錯誤，呼叫端不必另外處理
  assert board_agents.killProcessTree(321) is None


def test_jobThatExitsAfterResultIsNotMarkedStopped(tmp_path, monkeypatch):
  monkeypatch.setattr(board_agents, "resultGraceSeconds", 0.5)
  script = "import json; print(json.dumps({'type': 'result', 'is_error': False, 'structured_output': {}}))"
  job = board_agents.AgentJob([sys.executable, "-c", script], tmp_path, "", tmp_path / "log.txt",
                              board_agents.parseClaudeLine, 30).start()
  waitJob(job)
  assert job.exitCode == 0 and job.stoppedAfterResult is False and job.killed is False


def test_resolveCommand():
  assert board_agents.resolveCommand(["python", "fake.py"], "claude") == ["python", "fake.py"]
  assert len(board_agents.resolveCommand(None, "git")) == 1


def test_buildClaudeArgv():
  newSession = board_agents.buildClaudeArgv(["claude"], "sid-1", False, "規則")
  assert newSession[:3] == ["claude", "-p", "--output-format"]
  assert newSession[-2:] == ["--session-id", "sid-1"]
  assert "--json-schema" in newSession and "--verbose" in newSession
  assert "Bash(git push *)" in newSession
  assert "PowerShell(git push *)" in newSession
  assert "--allowedTools" not in newSession
  assert newSession[newSession.index("--permission-prompts") + 1] == "none"
  assert board_agents.buildClaudeArgv(["claude"], "sid-1", True, "規則")[-2:] == ["--resume", "sid-1"]
  withAllowed = board_agents.buildClaudeArgv(["claude"], "sid-1", False, "規則", ["Bash(pytest*)", "PowerShell(pytest*)"])
  allowedIndex = withAllowed.index("--allowedTools")
  assert withAllowed[allowedIndex + 1:allowedIndex + 3] == ["Bash(pytest*)", "PowerShell(pytest*)"]
  assert withAllowed[-2:] == ["--session-id", "sid-1"]


def test_parseClaudeLines():
  activity = {}
  lines = [
    json.dumps({"type": "system", "subtype": "init", "session_id": "sid-1"}),
    json.dumps({"type": "assistant", "message": {"content": [
      {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.txt"}}]}}),
    "這行不是 JSON",
    json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": "sid-1",
                "result": "", "structured_output": {"status": "done", "summary": "好了", "blockedReason": ""}}),
  ]
  for line in lines:
    board_agents.parseClaudeLine(line, activity)
  assert activity["sessionId"] == "sid-1"
  assert activity["action"] == "Edit a.txt"
  assert board_agents.claudeFinalResult(activity) == ({"status": "done", "summary": "好了", "blockedReason": ""}, "")


def test_claudeFinalResultFallbacks():
  textResult = {"result": {"type": "result", "is_error": False, "result": '{"status": "blocked", "summary": "", "blockedReason": "缺資料"}'}}
  assert board_agents.claudeFinalResult(textResult)[0]["status"] == "blocked"
  assert board_agents.claudeFinalResult({"result": {"is_error": True, "result": "API Error"}}) == (None, "API Error")
  assert board_agents.claudeFinalResult({}) == (None, "沒有收到 result 事件")


def test_buildCodexArgv(tmp_path):
  argv = board_agents.buildCodexArgv(["codex"], tmp_path / "wt", tmp_path / "schema.json", tmp_path / "out.json")
  assert argv[:4] == ["codex", "exec", "-C", str(tmp_path / "wt")]
  assert argv[argv.index("-s") + 1] == "read-only"
  assert argv[argv.index("-o") + 1] == str(tmp_path / "out.json")
  assert "--json" in argv and argv[-1] == "-"


def test_parseCodexLinesAndResult(tmp_path):
  activity = {}
  board_agents.parseCodexLine(json.dumps({"type": "thread.started", "thread_id": "th-1"}), activity)
  board_agents.parseCodexLine(json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "git diff"}}), activity)
  board_agents.parseCodexLine(json.dumps({"type": "turn.failed", "error": {"message": "boom"}}), activity)
  assert activity["threadId"] == "th-1"
  assert activity["action"] == "command_execution git diff"
  assert "boom" in activity["error"]
  outPath = tmp_path / "out.json"
  assert board_agents.codexFinalResult(outPath) == (None, "沒有產生 -o 輸出檔")
  outPath.write_text('{"verdict": "pass", "mustFix": [], "suggest": []}', encoding="utf-8")
  assert board_agents.codexFinalResult(outPath)[0]["verdict"] == "pass"


def test_codexFinalResultRejectsNonJsonAndNonObject(tmp_path):
  outPath = tmp_path / "out.json"
  outPath.write_text("  不是 JSON  \n", encoding="utf-8")
  assert board_agents.codexFinalResult(outPath) == (None, "輸出不是 JSON：不是 JSON")
  outPath.write_text('["pass"]', encoding="utf-8")
  assert board_agents.codexFinalResult(str(outPath)) == (None, "輸出不是 JSON 物件")


def test_looksLikeQuota():
  assert board_agents.looksLikeQuota("Error: You've hit your usage limit") is True
  assert board_agents.looksLikeQuota("Rate limit reached for requests") is True
  assert board_agents.looksLikeQuota("SyntaxError: invalid syntax") is False
  assert board_agents.looksLikeQuota("Context limit reached") is False
  assert board_agents.looksLikeQuota("You've hit your limit · resets 5pm") is True
  assert board_agents.looksLikeQuota("You're out of extra usage") is True


def test_buildClaudeArgvWithModel():
  argv = board_agents.buildClaudeArgv(["claude"], "sid-1", False, "規則", model="opus")
  assert argv[:4] == ["claude", "--model", "opus", "-p"]
  assert argv[-2:] == ["--session-id", "sid-1"]


def test_reasoningEffortReachesBothClisAndCodexResume(tmp_path):
  claude = board_agents.buildClaudeArgv(["claude"], "sid", True, "rules", model="opus", reasoningEffort="max")
  review = board_agents.buildClaudeReviewArgv(["claude"], "rules", reasoningEffort="high")
  assert claude[claude.index("--effort") + 1] == "max" and claude[-2:] == ["--resume", "sid"]
  assert review[review.index("--effort") + 1] == "high"
  for thread in (None, "thread-1"):
    argv = board_agents.buildCodexWriteArgv(["codex"], tmp_path, "s", "o", threadId=thread, reasoningEffort="ultra")
    overrides = [argv[index + 1] for index, arg in enumerate(argv) if arg == "-c"]
    assert 'model_reasoning_effort="ultra"' in overrides and argv[-1] == "-"
  argv = board_agents.buildCodexArgv(["codex"], tmp_path, "s", "o", reasoningEffort="low")
  assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="low"'


def test_buildClaudeReviewArgvIsReadOnly():
  argv = board_agents.buildClaudeReviewArgv(["claude"], "審查規則", model="sonnet")
  assert argv[:4] == ["claude", "--model", "sonnet", "-p"]
  assert json.loads(argv[argv.index("--json-schema") + 1]) == reviewResultSchema
  assert argv[argv.index("--permission-mode") + 1] == "manual"
  assert argv[argv.index("--permission-prompts") + 1] == "none"
  assert argv[argv.index("--append-system-prompt") + 1] == "審查規則"
  denied = argv[argv.index("--disallowedTools") + 1:]
  assert {"Edit", "Write", "NotebookEdit", "Bash", "PowerShell"} <= set(denied)
  assert "--session-id" not in argv and "--resume" not in argv
  assert board_agents.buildClaudeReviewArgv(["claude"], "規則")[:2] == ["claude", "-p"]


def test_buildCodexArgvWithModel(tmp_path):
  argv = board_agents.buildCodexArgv(["codex"], tmp_path / "wt", tmp_path / "s.json", tmp_path / "o.json",
                                     model="gpt-6.1-sol")
  assert argv[argv.index("-m") + 1] == "gpt-6.1-sol" and argv[-1] == "-"
  assert "-m" not in board_agents.buildCodexArgv(["codex"], tmp_path / "wt", tmp_path / "s.json", tmp_path / "o.json")


def test_buildCodexWriteArgvNewAndResume(tmp_path):
  fresh = board_agents.buildCodexWriteArgv(["codex"], tmp_path / "wt", tmp_path / "s.json", tmp_path / "o.json")
  assert fresh[:4] == ["codex", "exec", "-C", str(tmp_path / "wt")]
  assert fresh[fresh.index("-s") + 1] == "workspace-write"
  assert fresh[fresh.index("--output-schema") + 1] == str(tmp_path / "s.json")
  assert fresh[fresh.index("-o") + 1] == str(tmp_path / "o.json")
  assert "--json" in fresh and fresh[-1] == "-" and "-m" not in fresh
  resumed = board_agents.buildCodexWriteArgv(["codex"], tmp_path / "wt", tmp_path / "s.json", tmp_path / "o.json",
                                             threadId="th-1", model="gpt-6.1-sol")
  assert resumed[:3] == ["codex", "exec", "resume"]
  assert resumed[resumed.index("-c") + 1] == "sandbox_mode=workspace-write"
  assert resumed[resumed.index("-m") + 1] == "gpt-6.1-sol"
  assert resumed[-2:] == ["th-1", "-"]
  assert "-C" not in resumed and "-s" not in resumed
