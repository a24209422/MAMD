"""Context 門檻、原生 CLI 啟動參數及雙角色接線。"""
import json
import os
import sys

import pytest

import board_agents
import board_core
from board_core import TaskStatus
from helpers import fakeRole, readCalls, runUntil
from test_board_agents import collectLines, waitJob
from test_conductor import eventsOf, oneTask, planDone, taskIs


@pytest.mark.parametrize("window,expected", [(200000, 120000), (475000, 285000), (1000000, 600000), (100001, 60000)])
def test_compactTokenLimit(window, expected):
  assert board_core.compactTokenLimit(window) == expected


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "200000"])
def test_badContextWindowIsRejected(value):
  problems = board_core.configProblems({"writer": {"contextWindow": value}})
  assert any("writer.contextWindow" in problem for problem in problems)
  with pytest.raises(ValueError, match="contextWindow"):
    board_core.compactTokenLimit(value)


def test_nullContextWindowKeepsDefault():
  assert board_core.configProblems({"writer": {"contextWindow": None}}) == []
  assert board_core.roleSpec({"writer": {"contextWindow": None}}, "writer")["contextWindow"] == 200000


@pytest.mark.parametrize("thread", [None, "thread-to-continue"])
def test_codexWriteUsesTotalContextThreshold(tmp_path, thread):
  argv = board_agents.buildCodexWriteArgv(["codex"], tmp_path, "schema", "out", threadId=thread,
                                          reasoningEffort="high", contextWindow=475000)
  overrides = [argv[index + 1] for index, value in enumerate(argv) if value == "-c"]
  assert "model_auto_compact_token_limit=285000" in overrides
  assert 'model_auto_compact_token_limit_scope="total"' in overrides
  assert 'model_reasoning_effort="high"' in overrides
  if thread:
    assert argv[-2:] == [thread, "-"]
    assert "sandbox_mode=workspace-write" in overrides
  else:
    assert argv[argv.index("-s") + 1] == "workspace-write"


def test_codexReviewKeepsReadOnlyWithOwnThreshold(tmp_path):
  argv = board_agents.buildCodexArgv(["codex"], tmp_path, "schema", "out", contextWindow=1000000)
  assert "model_auto_compact_token_limit=600000" in argv
  assert argv[argv.index("-s") + 1] == "read-only"


@pytest.mark.parametrize("resume", [False, True])
def test_claudeCompactionOverridesAreSessionLocal(resume):
  writer = board_agents.buildClaudeArgv(["claude"], "session", resume, "rules", contextWindow=200000)
  reviewer = board_agents.buildClaudeReviewArgv(["claude"], "rules", contextWindow=1000000)
  for argv, window in ((writer, 200000), (reviewer, 1000000)):
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["autoCompactEnabled"] is True
    assert argv[argv.index("--autocompact") + 1] == str(window)
    assert settings["env"] == board_agents.compactionEnv("claude", window)
    assert settings["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "60"
  assert writer[-2:] == ["--resume" if resume else "--session-id", "session"]
  denied = reviewer[reviewer.index("--disallowedTools") + 1:]
  assert {"Edit", "Write", "Bash", "PowerShell"} <= set(denied)


@pytest.mark.parametrize("window", [99999, 1000001])
def test_claudeUnsupportedWindowFailsClearly(window):
  with pytest.raises(ValueError, match="Claude contextWindow"):
    board_agents.compactionEnv("claude", window)


def test_childEnvironmentIsIsolated(tmp_path, monkeypatch):
  monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "99")
  script = "import os; print(os.getenv('CLAUDE_AUTOCOMPACT_PCT_OVERRIDE'))"
  job = board_agents.AgentJob([sys.executable, "-c", script], tmp_path, "", tmp_path / "child.log",
                              collectLines, 20, env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "60"}).start()
  waitJob(job)
  assert job.activity["lines"] == ["60"]
  assert os.environ["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "99"


def test_codexDoesNotReceiveClaudeOverrides():
  assert board_agents.compactionEnv("codex", 475000) == {}


@pytest.mark.parametrize("writerCli,reviewCli", [("claude", "codex"), ("codex", "claude"), ("claude", "claude"), ("codex", "codex")])
def test_rolesAndResumedWriterReceiveCompaction(startBoard, writerCli, reviewCli):
  board, scenario = startBoard(oneTask, {"review": {"T1": ["fail", "pass"]}},
                               writer=fakeRole(writerCli, contextWindow=200000),
                               reviewer=fakeRole(reviewCli, contextWindow=1000000))
  runUntil(board, planDone)
  for kind, cli, window in (("write", writerCli, 200000), ("review", reviewCli, 1000000)):
    calls = readCalls(scenario, kind)
    assert len(calls) == 2
    for call in calls:
      if cli == "codex":
        assert f"model_auto_compact_token_limit={window * 60 // 100}" in call["argv"]
      else:
        assert call["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "60"
        assert call["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == str(window)
        assert call["env"]["DISABLE_AUTO_COMPACT"] == "0"
        assert call["env"]["DISABLE_COMPACT"] == "0"
    if kind == "write":
      flag = "--resume" if cli == "claude" else "resume"
      assert flag in calls[1]["argv"]


def test_unsupportedReviewWindowReportsFailureWithoutStartingCli(startBoard):
  board, scenario = startBoard(oneTask, reviewer=fakeRole("claude", contextWindow=50000))
  state = runUntil(board, taskIs(TaskStatus.needsYou))
  assert "Claude contextWindow" in state["tasks"]["P0001/T1"]["needs"]["reason"]
  assert not list(scenario.glob("call-review-*.json"))
  assert all(event["data"]["stage"] == "review" for event in eventsOf(board, "agentFailed"))
  assert state["tasks"]["P0001/T1"]["round"] == 1


def test_reviewEventFailureKillsItsConfiguredProcess(startBoard, monkeypatch):
  board, _ = startBoard(oneTask)
  monkeypatch.setattr(board, "startReviews", lambda *args: None)
  state = runUntil(board, taskIs(TaskStatus.toReview))
  plans, _ = board.currentState()
  created = []
  originalJob = board_agents.AgentJob

  def trackJob(*args, **kwargs):
    job = originalJob(*args, **kwargs)
    created.append(job)
    return job

  def failedEvent(*args, **kwargs):
    raise OSError("event write failed")

  monkeypatch.setattr(board_agents, "AgentJob", trackJob)
  monkeypatch.setattr(board, "event", failedEvent)
  with pytest.raises(OSError, match="event write failed"):
    board.startReview(plans["P0001"], state["tasks"]["P0001/T1"], "reviewer")
  assert not board.jobs
  assert len(created) == 1 and created[0].killed
  assert created[0].process.poll() is not None
