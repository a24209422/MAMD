"""測試設定必須在派工前明確選擇，並且與 writer 的指令權限一致。"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import board
import board_core
import conductor as conductor_module
from board_prompts import buildResolvePrompt, buildWritePrompt
from helpers import fakeConfig, readCalls, runUntil
from rtm_graph import analyzePlan


def config_path(project):
  return project / ".board" / "config.json"


def write_config(project, value):
  path = config_path(project)
  path.parent.mkdir(exist_ok=True)
  path.write_text(json.dumps(value), encoding="utf-8")


def test_init_requires_test_choice_before_modifying_files(gitProject, capsys):
  assert board.cmdInit(gitProject) == 1
  out = capsys.readouterr().out
  assert "--test-command" in out and "--skip-tests" in out
  assert not (gitProject / "AGENTS.md").exists()
  assert not (gitProject / ".claude").exists()
  assert not config_path(gitProject).exists()


def test_init_sets_custom_command_and_preserves_other_settings(gitProject):
  original = {"maxParallel": 2, "writer": {"cli": "codex"}, "custom": {"keep": True}}
  write_config(gitProject, original)
  assert board.cmdInit(gitProject, testCommand="npm test") == 0
  expected = {**original, "testCommand": "npm test"}
  assert json.loads(config_path(gitProject).read_text(encoding="utf-8")) == expected
  # 沒帶選項重跑也沿用已選的指令，不改使用者設定。
  before = config_path(gitProject).read_bytes()
  assert board.cmdInit(gitProject) == 0
  assert config_path(gitProject).read_bytes() == before


def test_init_skip_is_explicit_and_can_be_changed_to_testing(gitProject):
  assert board.cmdInit(gitProject, skipTests=True) == 0
  assert json.loads(config_path(gitProject).read_text(encoding="utf-8")) == {
    "testCommand": "", "skipTests": True}
  assert board.cmdInit(gitProject) == 0
  assert board.cmdInit(gitProject, testCommand="python -m pytest -q") == 0
  config = json.loads(config_path(gitProject).read_text(encoding="utf-8"))
  assert config == {"testCommand": "python -m pytest -q"}


@pytest.mark.parametrize("command", ["", "   ", 123])
def test_init_rejects_empty_or_non_string_command(gitProject, command):
  assert board.cmdInit(gitProject, testCommand=command) == 1
  assert not (gitProject / "AGENTS.md").exists()


def test_init_rejects_conflicting_choices_and_broken_json(gitProject):
  assert board.cmdInit(gitProject, testCommand="npm test", skipTests=True) == 1
  assert not (gitProject / "AGENTS.md").exists()
  config_path(gitProject).parent.mkdir()
  config_path(gitProject).write_text("{broken", encoding="utf-8")
  assert board.cmdInit(gitProject, testCommand="npm test") == 1
  assert config_path(gitProject).read_text(encoding="utf-8") == "{broken"
  assert not (gitProject / "AGENTS.md").exists()


def test_init_does_not_silently_disable_existing_tdd(gitProject):
  original = {"tddCrap": True, "testCommand": "python quality.py"}
  write_config(gitProject, original)
  assert board.cmdInit(gitProject, skipTests=True) == 1
  assert board.cmdInit(gitProject, testCommand="npm test") == 1
  assert json.loads(config_path(gitProject).read_text(encoding="utf-8")) == original


@pytest.mark.parametrize("config", [
  {"testCommand": None}, {"testCommand": ["pytest"]}, {"skipTests": "true"},
  {"skipTests": True, "testCommand": "npm test"}, {"skipTests": True, "testCommand": "   "},
  {"skipTests": True, "tddCrap": True},
])
def test_invalid_test_settings_are_rejected(config):
  assert board_core.configProblems(config)


def test_max_parallel_tests_defaults_to_one_and_reads_user_value(gitProject):
  assert board_core.loadConfig(gitProject)["maxParallelTests"] == 1
  write_config(gitProject, {"testCommand": "npm test", "maxParallelTests": 2})
  assert board_core.loadConfig(gitProject)["maxParallelTests"] == 2
  assert board_core.configProblems(board_core.readUserConfig(gitProject)) == []


@pytest.mark.parametrize("value", [0, -1, True, "2", 1.5, None])
def test_invalid_max_parallel_tests_is_rejected(value):
  problems = board_core.configProblems({"testCommand": "npm test", "maxParallelTests": value})
  assert any("maxParallelTests 要寫成 1 以上的整數" in problem for problem in problems)


@pytest.mark.parametrize("config", [{}, {"testCommand": "   "}])
def test_main_refuses_missing_test_choice_without_taking_lock(gitProject, config, monkeypatch, capsys):
  write_config(gitProject, config)
  def unexpected_start(*args, **kwargs):
    pytest.fail("不應在缺少測試設定時派工")
  monkeypatch.setattr(conductor_module, "Conductor", unexpected_start)
  assert conductor_module.main([str(gitProject)]) == 1
  assert "尚未設定測試方式" in capsys.readouterr().out
  assert not (config_path(gitProject).parent / "conductor.lock").exists()


@pytest.mark.parametrize("config", [{"testCommand": "npm test"}, {"skipTests": True}])
def test_main_accepts_existing_command_or_explicit_skip(gitProject, config, monkeypatch):
  write_config(gitProject, config)
  started = []
  class FakeConductor:
    def __init__(self, project):
      self.project = project
    def runForever(self):
      started.append(self.project)
  monkeypatch.setattr(conductor_module, "Conductor", FakeConductor)
  assert conductor_module.main([str(gitProject)]) == 0
  assert started == [gitProject.resolve()]


def test_main_reports_broken_config_without_traceback(gitProject, capsys):
  config_path(gitProject).parent.mkdir()
  config_path(gitProject).write_text("{broken", encoding="utf-8")
  assert conductor_module.main([str(gitProject)]) == 1
  assert "讀不懂" in capsys.readouterr().out


def test_command_change_warns_once_and_keeps_launched_permissions(gitProject):
  write_config(gitProject, fakeConfig(testCommand="npm test"))
  lines = []
  controller = conductor_module.Conductor(gitProject, out=lines.append)
  write_config(gitProject, fakeConfig(testCommand="python -m pytest -q"))
  controller.refreshRoles()
  controller.refreshRoles()
  assert len([line for line in lines if "需要重啟 Alfred" in line]) == 1
  assert controller.config["testCommand"] == "npm test"
  allowed = controller.claudeAllowedTools(controller.roles["writer"]["spec"])
  assert "PowerShell(npm test*)" in allowed
  assert "PowerShell(python -m pytest -q*)" not in allowed


def test_normal_writer_receives_the_command_that_alfred_runs(startBoard):
  rows = ["| R1 需求 | 理解 | D1 做法 | 不採用 | T1 任務 | | 驗證 |"]
  controller, scenario = startBoard(rows, testCommand="git --version")
  runUntil(controller, lambda state: state["plans"]["P0001"]["status"] == board_core.PlanStatus.completed)
  write = readCalls(scenario, "write")[0]
  assert "專案設定的測試指令" in write["prompt"]
  assert "git --version" in write["prompt"]
  assert "PowerShell(git --version*)" in write["argv"]
  assert "tdd-crap" not in write["prompt"]


def test_both_writer_paths_explain_explicit_skip(writePlan):
  plan = analyzePlan(writePlan("0001-demo.md", [
    "| R1 需求 | 理解 | D1 做法 | 不採用 | T1 任務 | | 驗證 |"], planId="P0001"))
  assert "已明確略過自動測試" in buildWritePrompt(plan, "T1", 1, skipTests=True)
  assert "已明確略過自動測試" in buildResolvePrompt(plan, "T1", 1, ["app.py"], skipTests=True)
  prompt = buildResolvePrompt(plan, "T1", 1, ["app.py"], testCommand="npm test")
  assert "npm test" in prompt and "不需要讀取主專案的 .board/config.json" in prompt


def test_init_cli_accepts_command_and_rejects_mutually_exclusive_options(gitProject):
  argv = [sys.executable, str(Path(board.__file__)), "--project", str(gitProject), "init"]
  refused = subprocess.run(argv + ["--test-command", "npm test", "--skip-tests"], capture_output=True)
  assert refused.returncode == 2
  assert not (gitProject / "AGENTS.md").exists()
  accepted = subprocess.run(argv + ["--test-command", "npm test"], capture_output=True)
  assert accepted.returncode == 0
  assert json.loads(config_path(gitProject).read_text(encoding="utf-8"))["testCommand"] == "npm test"
