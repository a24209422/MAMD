import sys
import time
from pathlib import Path

import pytest

import board_models
from board_core import BoardError

fakeCli = Path(__file__).parent / "fakes" / "fake_models.py"


def command(scenario):
  return [sys.executable, str(fakeCli), scenario]


def test_codexCatalogUsesCliOrderPaginationAndEfforts(tmp_path):
  models = board_models.discoverModels(tmp_path, "codex", command("codex"))
  assert [model["id"] for model in models] == ["gpt-test-v2", "gpt-test-small"]
  assert models[0] == {
    "id": "gpt-test-v2", "displayName": "Test V2", "description": "From CLI", "resolvedModel": "gpt-test-v2",
    "efforts": [{"value": "low", "description": "Fast"}, {"value": "ultra", "description": "Deep"}],
    "defaultEffort": "low", "isDefault": True,
  }


def test_claudeCatalogIncludesVersionsAndOnlySupportedEfforts(tmp_path):
  models = board_models.discoverModels(tmp_path, "claude", command("claude"))
  assert [model["id"] for model in models] == ["default", "opus", "claude-test-old"]
  assert models[1]["resolvedModel"] == "claude-test-new"
  assert models[1]["efforts"] == [{"value": "high", "description": ""}, {"value": "max", "description": ""}]
  assert models[2]["efforts"] == []
  assert models[0]["isDefault"] is True


@pytest.mark.parametrize("scenario,cli", [("error", "codex"), ("error", "claude"), ("empty", "codex"),
                                          ("empty", "claude"), ("exit", "codex"), ("bad", "claude"),
                                          ("loop", "codex"), ("unsafe", "claude")])
def test_catalogReportsCliFailureInsteadOfInventingChoices(tmp_path, scenario, cli):
  with pytest.raises(BoardError):
    board_models.discoverModels(tmp_path, cli, command(scenario), timeout=3)


def test_timeoutIsBoundedAndMissingCliIsReported(tmp_path):
  started = time.monotonic()
  with pytest.raises(BoardError, match="逾時"):
    board_models.discoverModels(tmp_path, "codex", command("timeout"), timeout=0.2)
  assert time.monotonic() - started < 5
  with pytest.raises(BoardError):
    board_models.discoverModels(tmp_path, "codex", [str(tmp_path / "missing.exe")])


def test_catalogCachesByCommandRefreshesAndDoesNotCacheErrors(tmp_path, monkeypatch):
  calls = []
  def discover(projectDir, cli, configured, **kwargs):
    calls.append((cli, configured))
    if configured == ["broken"]:
      raise BoardError("未登入")
    return [{"id": str(len(calls))}]
  monkeypatch.setattr(board_models, "discoverModels", discover)
  catalog = board_models.ModelCatalog(tmp_path, ttl=300)
  first = catalog.get("codex", ["one"])
  assert catalog.get("codex", ["one"]) == first and len(calls) == 1
  assert catalog.get("codex", ["one"], refresh=True) != first
  assert catalog.get("codex", ["two"])["ok"] is True
  assert catalog.get("codex", ["broken"])["message"] == "未登入"
  assert catalog.get("codex", ["broken"])["ok"] is False
  assert len(calls) == 5
  expired = board_models.ModelCatalog(tmp_path, ttl=0)
  expired.get("claude")
  expired.get("claude")
  assert len(calls) == 7


def test_choiceValidationRejectsUnknownModelsAndUnsupportedEffort():
  models = [{"id": "test", "efforts": [{"value": "high"}]}]
  board_models.validateChoice(models, "test", "high")
  board_models.validateChoice(models, "test", "")
  board_models.validateChoice(models, "", "")
  for model, effort in [("typo", ""), ("test", "ultra"), ("", "high")]:
    with pytest.raises(BoardError):
      board_models.validateChoice(models, model, effort)
