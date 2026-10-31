import json
import os
import threading
import time

import pytest

import board_core
from helpers import approvedEvent, makeEvent, makePlans, testConfig


def test_appendAndReadEvents(tmp_path):
  board_core.appendEvent(tmp_path, "planSubmitted", plan="P0001", data={"red": 0})
  board_core.appendEvent(tmp_path, "taskStarted", plan="P0001", task="T1", agent="claude", roundNumber=1)
  events = board_core.readEvents(tmp_path)
  assert [event["type"] for event in events] == ["planSubmitted", "taskStarted"]
  assert events[1]["round"] == 1
  assert events[1]["agent"] == "claude"
  assert events[0]["data"] == {"red": 0}
  assert events[0]["time"]


def test_userDecisionsReplayFromFileLikeInMemory(tmp_path):
  tasks = [("T1", []), ("T2", []), ("T3", [])]
  events = [approvedEvent("P0001", tasks),
            makeEvent("needsYou", plan="P0001", task="T1", kind="rounds", reason="審查 1 次不通過"),
            makeEvent("userDecision", plan="P0001", task="T1", action="retry", note="改小一點"),
            makeEvent("needsYou", plan="P0001", task="T2", kind="mergeConflict", reason="衝突", conflicts=["a.txt"]),
            makeEvent("userDecision", plan="P0001", task="T2", action="retry"),
            makeEvent("needsYou", plan="P0001", task="T3", kind="blocked", reason="缺資料"),
            makeEvent("userDecision", plan="P0001", task="T3", action="done")]
  for event in events:
    board_core.appendEvent(tmp_path, event["type"], plan=event["plan"], task=event["task"], data=event["data"])
  replayed = board_core.deriveState(board_core.readEvents(tmp_path), makePlans("P0001"), testConfig)
  assert replayed["tasks"] == board_core.deriveState(events, makePlans("P0001"), testConfig)["tasks"]
  first, second, third = (replayed["tasks"][f"P0001/{taskId}"] for taskId, _ in tasks)
  assert (first["status"], first["round"], first["maxRounds"], first["userNote"]) == (
    board_core.TaskStatus.queued, 2, 4, "改小一點")
  assert (second["resolving"], second["conflictFiles"], second["round"]) == (True, ["a.txt"], 2)
  assert (third["status"], third["userDone"], third["needs"]) == (board_core.TaskStatus.merging, True, None)


def test_concurrentAppendsKeepWholeLines(tmp_path):
  def writeMany(writerIndex):
    for eventIndex in range(20):
      board_core.appendEvent(tmp_path, "testEvent", data={"writer": writerIndex, "index": eventIndex, "padding": "字" * 200})

  threads = [threading.Thread(target=writeMany, args=(writerIndex,)) for writerIndex in range(10)]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join()
  events = board_core.readEvents(tmp_path)
  assert len(events) == 200
  assert not (tmp_path / ".board" / "events.lock").exists()


def test_staleLockIsBroken(tmp_path):
  lockPath = tmp_path / ".board" / "events.lock"
  lockPath.parent.mkdir(parents=True)
  lockPath.write_text("", encoding="utf-8")
  oldTime = time.time() - 120
  os.utime(lockPath, (oldTime, oldTime))
  board_core.appendEvent(tmp_path, "testEvent")
  assert len(board_core.readEvents(tmp_path)) == 1


def test_malformedLineIsSkipped(tmp_path):
  board_core.appendEvent(tmp_path, "first")
  with open(tmp_path / ".board" / "events.jsonl", "a", encoding="utf-8") as handle:
    handle.write('{"type": "broken"\n')
  board_core.appendEvent(tmp_path, "second")
  assert [event["type"] for event in board_core.readEvents(tmp_path)] == ["first", "second"]


def test_loadConfigDefaultsAndOverride(tmp_path):
  assert board_core.loadConfig(tmp_path)["maxRounds"] == 3
  (tmp_path / ".board").mkdir()
  (tmp_path / ".board" / "config.json").write_text('{"maxRounds": 5, "testCommand": "pytest -q"}', encoding="utf-8")
  config = board_core.loadConfig(tmp_path)
  assert config["maxRounds"] == 5
  assert config["testCommand"] == "pytest -q"
  assert config["maxParallel"] == 3


def test_loadPlansKeyedById(tmp_path, writePlan):
  writePlan("0002-second.md", ["| R1 需求 | 理解 | D1 做法 | - | T1 任務 | | 驗證 |"], planId="P0002")
  writePlan("0001-first.md", ["| R1 需求 | 理解 | D1 做法 | - | T1 任務 | | 驗證 |"], planId="P0001")
  plans = board_core.loadPlans(tmp_path)
  assert list(plans) == ["P0001", "P0002"]
  assert plans["P0001"]["graph"].tasks["T1"]["text"] == "任務"


def test_lockGivesUpWhenFileCannotBeCreated(tmp_path, monkeypatch):
  def alwaysDenied(path, flags, *args, **kwargs):
    raise PermissionError("denied")

  monkeypatch.setattr(board_core.os, "open", alwaysDenied)
  startTime = time.time()
  with pytest.raises(board_core.BoardError):
    with board_core.fileLock(tmp_path / "x.lock", timeoutSeconds=0.3):
      pass
  assert time.time() - startTime < 2


def test_defaultConfigHasRolesInsteadOfLegacyKeys(tmp_path):
  config = board_core.loadConfig(tmp_path)
  assert (config["writer"], config["reviewer"]) == ({"cli": "claude"}, {"cli": "codex"})
  assert not set(board_core.legacyConfigKeys) & set(config)


def test_roleSpecDefaultsAndOverrides(tmp_path):
  config = board_core.loadConfig(tmp_path)
  assert board_core.roleSpec(config, "writer") == {"cli": "claude", "model": None, "command": None,
                                                    "contextWindow": 200000, "allowedTools": [], "reasoningEffort": None}
  assert board_core.roleSpec(config, "reviewer")["cli"] == "codex"
  assert board_core.roleSpec(config, "reviewer")["contextWindow"] == 475000
  (tmp_path / ".board").mkdir()
  (tmp_path / ".board" / "config.json").write_text(json.dumps({
    "writer": {"cli": "codex", "model": "gpt-6.1-sol"},
    "reviewer": {"cli": "claude", "model": "opus", "contextWindow": 1000000},
  }), encoding="utf-8")
  config = board_core.loadConfig(tmp_path)
  assert board_core.roleSpec(config, "writer") == {"cli": "codex", "model": "gpt-6.1-sol", "command": None,
                                                    "contextWindow": 475000, "allowedTools": [], "reasoningEffort": None}
  assert board_core.roleSpec(config, "reviewer")["contextWindow"] == 1000000
  assert board_core.readUserConfig(tmp_path)["writer"]["cli"] == "codex"
  assert board_core.readUserConfig(tmp_path / "nowhere") == {}


def test_configProblemsFlagsLegacyKeysAndBadRoles():
  assert board_core.configProblems({}) == []
  assert board_core.configProblems({"writer": {"cli": "codex", "model": "gpt-6.1-sol"}, "reviewer": {}}) == []
  problems = board_core.configProblems({"claudeCommand": ["claude"], "writer": {"cli": "gemini", "model": 3},
                                        "reviewer": "codex"})
  assert problems == [
    "claudeCommand 已經不用了，請改寫成 writer.command（見 README 的「換模型」）",
    "writer.cli 只能是 claude 或 codex，現在是 gemini",
    'writer.model 要是文字，例如 "opus"',
    'reviewer 要寫成物件，例如 {"cli": "claude", "model": "opus"}',
  ]
  assert board_core.configProblems({"writer": {"command": "claude"}}) == [
    'writer.command 要是文字陣列，例如 ["C:/tools/claude.exe"]']


def test_badAllowedToolsAreReportedWithoutCrashing():
  assert board_core.configProblems({"writer": {"allowedTools": ["Bash(npm i*)"]}}) == []
  message = 'writer.allowedTools 要是文字陣列，例如 ["Bash(npm install*)"]'
  for value in (True, "Bash(npm i*)", ["Bash(npm i*)", 3]):
    assert board_core.configProblems({"writer": {"allowedTools": value}}) == [message]
  # 儀表板和 conductor 讀設定時不能當掉，也不能把字串拆成一個個字元
  for value in (True, "Bash(npm i*)"):
    assert board_core.roleSpec({"writer": {"allowedTools": value}}, "writer")["allowedTools"] == []


def writeUserConfigFile(projectDir, config):
  (projectDir / ".board").mkdir(exist_ok=True)
  (projectDir / ".board" / "config.json").write_text(json.dumps(config), encoding="utf-8")


def test_saveRoleChoiceOnlyChangesThatRole(tmp_path):
  writeUserConfigFile(tmp_path, {
    "maxParallel": 2,
    "writer": {"cli": "claude", "model": "opus", "allowedTools": ["Bash(npm i*)"]},
    "reviewer": {"cli": "codex", "command": ["C:/tools/codex.cmd"]},
  })
  board_core.saveRoleChoice(tmp_path, "writer", "codex", "gpt-6.1-sol")
  assert board_core.readUserConfig(tmp_path) == {
    "maxParallel": 2,
    "writer": {"cli": "codex", "model": "gpt-6.1-sol", "allowedTools": ["Bash(npm i*)"]},
    "reviewer": {"cli": "codex", "command": ["C:/tools/codex.cmd"]},
  }
  # 有自訂 command 時，不換 CLI、只換模型可以
  board_core.saveRoleChoice(tmp_path, "reviewer", "codex", "gpt-6.1-sol")
  # 模型空白代表用 CLI 的預設模型，model 欄位拿掉
  board_core.saveRoleChoice(tmp_path, "writer", "codex", "")
  config = board_core.readUserConfig(tmp_path)
  assert "model" not in config["writer"] and config["reviewer"]["model"] == "gpt-6.1-sol"
  assert [path.name for path in (tmp_path / ".board").iterdir()] == ["config.json"]


def test_saveRoleChoiceCreatesConfigFile(tmp_path):
  board_core.saveRoleChoice(tmp_path, "reviewer", "claude", "claude-opus-5-5[1m]")
  assert board_core.readUserConfig(tmp_path) == {"reviewer": {"cli": "claude", "model": "claude-opus-5-5[1m]"}}


def test_reasoningEffortIsSavedValidatedAndCleared(tmp_path):
  board_core.saveRoleChoice(tmp_path, "writer", "codex", "test", "ultra")
  config = board_core.readUserConfig(tmp_path)
  assert config["writer"]["reasoningEffort"] == "ultra"
  assert board_core.roleSpec(config, "writer")["reasoningEffort"] == "ultra"
  for value in (True, ["high"], "high&calc", "-p"):
    assert board_core.configProblems({"writer": {"reasoningEffort": value}})
    with pytest.raises(board_core.BoardError):
      board_core.saveRoleChoice(tmp_path, "writer", "codex", "test", value)
  board_core.saveRoleChoice(tmp_path, "writer", "claude", "opus", "")
  assert "reasoningEffort" not in board_core.readUserConfig(tmp_path)["writer"]


def test_saveRoleChoiceRejectsBadChoices(tmp_path):
  writeUserConfigFile(tmp_path, {"reviewer": {"cli": "codex", "command": ["C:/tools/codex.cmd"]}})
  configFile = tmp_path / ".board" / "config.json"
  before = configFile.read_text(encoding="utf-8")
  cases = [
    ("boss", "claude", "", "角色只能是 writer 或 reviewer，現在是 boss"),
    ("writer", "gemini", "", "CLI 只能是 claude 或 codex，現在是 gemini"),
    ("writer", "codex", "gpt&calc", board_core.modelNameRule),
    ("writer", "codex", "-p", board_core.modelNameRule),
    ("writer", "codex", "x" * 101, board_core.modelNameRule),
    ("reviewer", "claude", "", "reviewer 有自訂 command，換 CLI 前要先到 .board/config.json 改 command"),
  ]
  for role, cli, model, message in cases:
    with pytest.raises(board_core.BoardError) as raised:
      board_core.saveRoleChoice(tmp_path, role, cli, model)
    assert str(raised.value) == message
  assert configFile.read_text(encoding="utf-8") == before


def test_saveRoleChoiceRefusesUnreadableConfig(tmp_path):
  (tmp_path / ".board").mkdir()
  configFile = tmp_path / ".board" / "config.json"
  configFile.write_text("{壞掉", encoding="utf-8")
  with pytest.raises(board_core.BoardError, match="讀不懂"):
    board_core.saveRoleChoice(tmp_path, "writer", "codex", "")
  configFile.write_text("[1]", encoding="utf-8")
  with pytest.raises(board_core.BoardError, match="最外層要是物件"):
    board_core.saveRoleChoice(tmp_path, "writer", "codex", "")
  assert configFile.read_text(encoding="utf-8") == "[1]"


def test_saveRoleChoiceRetriesWhileFileIsBusy(tmp_path, monkeypatch):
  realReplace = board_core.os.replace
  calls = []

  def busyTwice(source, target):
    calls.append(target)
    if len(calls) <= 2:
      raise PermissionError("busy")
    realReplace(source, target)

  monkeypatch.setattr(board_core.os, "replace", busyTwice)
  monkeypatch.setattr(board_core.time, "sleep", lambda seconds: None)
  board_core.saveRoleChoice(tmp_path, "writer", "codex", "")
  assert len(calls) == 3 and board_core.readUserConfig(tmp_path) == {"writer": {"cli": "codex"}}

  def alwaysBusy(source, target):
    raise PermissionError("busy")

  monkeypatch.setattr(board_core.os, "replace", alwaysBusy)
  with pytest.raises(board_core.BoardError, match="正被其他程式使用"):
    board_core.saveRoleChoice(tmp_path, "writer", "claude", "")
  assert board_core.readUserConfig(tmp_path) == {"writer": {"cli": "codex"}}
  assert [path.name for path in (tmp_path / ".board").iterdir()] == ["config.json"]
