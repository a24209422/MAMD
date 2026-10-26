import os
import threading
import time

import pytest

import board_core


def test_appendAndReadEvents(tmp_path):
  board_core.appendEvent(tmp_path, "planSubmitted", plan="P0001", data={"red": 0})
  board_core.appendEvent(tmp_path, "taskStarted", plan="P0001", task="T1", agent="claude", roundNumber=1)
  events = board_core.readEvents(tmp_path)
  assert [event["type"] for event in events] == ["planSubmitted", "taskStarted"]
  assert events[1]["round"] == 1
  assert events[1]["agent"] == "claude"
  assert events[0]["data"] == {"red": 0}
  assert events[0]["time"]


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


def test_defaultConfigHasContextWindows(tmp_path):
  config = board_core.loadConfig(tmp_path)
  assert config["claudeContextWindow"] == 200000
  assert config["codexContextWindow"] == 475000
