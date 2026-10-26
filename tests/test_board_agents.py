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
