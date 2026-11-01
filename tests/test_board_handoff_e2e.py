"""兩個不同路徑的 clone 透過本機 bare remote 雙向交接；指令和 Alfred 都用初始化帶進專案的 runtime 執行。"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import board_git
from board_core import PlanStatus, TaskStatus, configPath, readEvents
from board_setup.project import Options, initialize
from board_sync import checkpointPath, progressPath, readCheckpoint, readSnapshot
from conftest import git
from helpers import fakeRole, fakesDir
from test_board_export import legacyProject, pushListed, section  # noqa: F401
from test_board_handoff import planLines, statusOutput
from test_board_setup import toolRoot

initFiles = [".agents", ".claude", ".agent-board", "AGENTS.md", ".gitignore"]


def runScript(projectDir, script, *args, home, env=None):
  # 拿掉 PYTHONPATH，確保 import 到的是專案裡的 runtime，不是開發中的原始碼
  env = {**{key: value for key, value in os.environ.items() if key != "PYTHONPATH"}, **(env or {})}
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


def useFakeAgents(clone, tmp_path):
  # 設定檔只在本機，不進交接檔；換成假 agent 才能在測試裡真的派工
  config = json.loads(configPath(clone).read_text(encoding="utf-8-sig"))
  config.update({"writer": fakeRole("claude"), "reviewer": fakeRole("codex"), "pollSeconds": 0.05})
  configPath(clone).write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
  scenario = tmp_path / "scenario" / "scenario.json"
  scenario.parent.mkdir()
  scenario.write_text("{}", encoding="utf-8")
  return {"FAKE_SCENARIO": str(scenario)}


def isAncestor(projectDir, older, newer):
  return subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=projectDir).returncode == 0


def test_cloneResumesTaskWithRuntimeAndSourceOnlyPulls(computers, tmp_path):
  source, clone, remote, home = computers
  initClone(clone, home)
  recorded = readSnapshot(clone)["branches"]
  env = useFakeAgents(clone, tmp_path)

  # 電腦 2 用 clone 帶來的 runtime 跑 Alfred：照交接檔從遠端建分支、重建 worktree，再把退回的 T1 做完
  driven = runScript(clone, fakesDir / "run_alfred.py", str(clone), "P0001", "T1", TaskStatus.done, home=home, env=env)
  conductorFile = json.loads(driven.splitlines()[-1])["conductor"]
  assert Path(conductorFile).parent == clone / ".agent-board" / "runtime"

  started = [event for event in readEvents(clone) if event["type"] == "taskStarted" and event["task"] == "T1"]
  assert started[-1]["round"] == 2
  assert started[-1]["data"]["worktree"] == str(board_git.taskWorktreePath(clone, "P0001", "T1"))
  written = [event for event in readEvents(clone) if event["type"] == "taskWritten" and event["task"] == "T1"]
  # 新成果接在交接記錄的 SHA 後面，代表 worktree 是從記錄的分支重建，不是從頭開始
  newCommit = written[-1]["data"]["commit"]
  assert newCommit != recorded["task/P0001-T1"] and isAncestor(clone, recorded["task/P0001-T1"], newCommit)
  # 合併回的 plan 分支同時帶著交接前合併的 T3 和這次的新成果
  assert isAncestor(clone, recorded["plan/P0001"], "plan/P0001") and isAncestor(clone, newCommit, "plan/P0001")
  assert board_git.runGit(clone, "show", "plan/P0001:t3.txt") == "T3 的成果"

  output = runtimeBoard(clone, "export-state", home=home)
  commitListed(clone, output)
  pushAgain(clone, output, remote)
  updated = readSnapshot(clone)

  git(source, "pull", "-q", "--ff-only", str(remote), "main")

  sourceStatus = runtimeBoard(source, "status", home=home)
  assert f"  T1 {TaskStatus.done}（第 2/3 輪）" in planLines(sourceStatus)
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
