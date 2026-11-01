"""兩個不同路徑的 clone 透過本機 bare remote 雙向交接；指令都用初始化帶進專案的 runtime 執行。"""
import os
import subprocess
import sys

import pytest

from board_core import PlanStatus
from board_setup.project import Options, initialize
from board_sync import checkpointPath, progressPath, readCheckpoint, readSnapshot
from conductor import Conductor
from conftest import git
from helpers import fakeConfig
from test_board_export import legacyProject, pushListed, section  # noqa: F401
from test_board_handoff import planLines, statusOutput
from test_board_setup import toolRoot
from test_board_sync_load import started, writeEvents

initFiles = [".agents", ".claude", ".agent-board", "AGENTS.md", ".gitignore"]


def runScript(projectDir, script, *args, home):
  # 拿掉 PYTHONPATH，確保 import 到的是專案裡的 runtime，不是開發中的原始碼
  env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
  env["USERPROFILE"] = str(home)
  result = subprocess.run([sys.executable, str(script), *args], cwd=projectDir, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120, env=env)
  assert result.returncode == 0, result.stdout + result.stderr
  return result.stdout


def runtimeBoard(projectDir, *args, home):
  return runScript(projectDir, projectDir / ".agent-board" / "runtime" / "board.py", *args, home=home)


def initClone(clone, home):
  return runScript(clone, clone / ".claude/skills/agent-board-init/scripts/init.py", "--skip-tests", home=home)


def commitListed(projectDir, output):
  git(projectDir, "add", "--", *section(output, "請 commit 這些檔案："))
  git(projectDir, "commit", "-q", "-m", "交接進度")


def pushAgain(projectDir, output, remote):
  refs = ["HEAD:refs/heads/main"] + [f"refs/heads/{line.split()[0]}" for line in section(output, "請 push 這些分支：")]
  git(projectDir, "push", "-q", str(remote), *refs)


def sharedLines(output):
  # 草稿只在寫 plan 的那台電腦，本來就不進交接檔
  return [line for line in planLines(output) if not line.endswith(f"｜{PlanStatus.draft}")]


@pytest.fixture
def computers(legacyProject, tmp_path):
  source, home = legacyProject, tmp_path / "家目錄"
  # 已完成的 plan 合併進主分支後刪掉分支，成果只靠主分支交付
  git(source, "merge", "-q", "--no-ff", "-m", "合併 P0006", "plan/P0006")
  git(source, "branch", "-D", "task/P0006-T1")
  git(source, "branch", "-D", "plan/P0006")
  assert initialize(source, Options(skipTests=True), toolRoot) == 0
  git(source, "add", "--", *initFiles)
  git(source, "commit", "-q", "-m", "初始化 agent-board")
  output = runtimeBoard(source, "export-state", home=home)
  commitListed(source, output)
  remote = tmp_path / "remote.git"
  pushListed(source, output, remote)
  clone = tmp_path / "電腦 二" / "專案 B"
  clone.parent.mkdir()
  git(tmp_path, "clone", "-q", "-b", "main", str(remote), str(clone))
  git(clone, "config", "user.name", "tester")
  git(clone, "config", "user.email", "tester@example.com")
  return source, clone, remote, home


def test_cloneInitThenReinitShowsSameStatus(computers):
  source, clone, _, home = computers
  expected = sharedLines(runtimeBoard(source, "status", home=home))

  assert "已接納交接進度" in initClone(clone, home)
  assert planLines(runtimeBoard(clone, "status", home=home)) == expected
  assert any(f"｜{PlanStatus.completed}" in line for line in expected)

  progress, checkpoint = progressPath(clone).read_bytes(), checkpointPath(clone).read_bytes()
  initClone(clone, home)
  assert (progressPath(clone).read_bytes(), checkpointPath(clone).read_bytes()) == (progress, checkpoint)
  assert planLines(runtimeBoard(clone, "status", home=home)) == expected


def test_cloneUpdatesAndSourceOnlyPulls(computers):
  source, clone, remote, home = computers
  initClone(clone, home)
  # 電腦 2 啟動 Alfred 時照交接檔從遠端建分支，再往下跑一輪
  alfred = Conductor(clone, config=fakeConfig(), out=lambda message: None)
  plans, state = alfred.currentState()
  alfred.ensurePlanWorktrees(state)
  writeEvents(clone, [started("T1", roundNumber=2)])
  output = runtimeBoard(clone, "export-state", home=home)
  commitListed(clone, output)
  pushAgain(clone, output, remote)
  updated = readSnapshot(clone)

  git(source, "pull", "-q", "--ff-only", str(remote), "main")

  sourceStatus = runtimeBoard(source, "status", home=home)
  assert "  T1 寫程式中（第 2/3 輪）" in planLines(sourceStatus)
  assert sharedLines(sourceStatus) == planLines(runtimeBoard(clone, "status", home=home))
  assert readCheckpoint(source)["base"]["revision"] == updated["revision"]
  assert "export-state" not in sourceStatus


def test_projectWithoutHandoffKeepsWorkingAfterInit(legacyProject, tmp_path, capsys):
  before = statusOutput(legacyProject, capsys)

  assert initialize(legacyProject, Options(skipTests=True), toolRoot) == 0

  assert "已接納交接進度" not in capsys.readouterr().out
  after = runtimeBoard(legacyProject, "status", home=tmp_path / "家目錄")
  assert planLines(after) == planLines(before)
  assert "交接檔｜" not in after
  assert not progressPath(legacyProject).exists()
