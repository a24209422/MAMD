import subprocess
import sys
from pathlib import Path

import pytest

toolDir = Path(__file__).resolve().parent.parent
# 測試直接 import 工具目錄裡的模組，不另外打包
if str(toolDir) not in sys.path:
  sys.path.insert(0, str(toolDir))

tableHeader = (
  "| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |\n"
  "|---|---|---|---|---|---|---|\n"
)


def planText(planId, title, rows, extraMeta=""):
  return (f"---\nid: {planId}\ncreated: 2026-10-25\n{extraMeta}---\n\n# {title}\n\n## 追溯矩陣\n\n"
          + tableHeader + "\n".join(rows) + "\n")


@pytest.fixture
def writePlan(tmp_path):
  def write(fileName, rows, planId=None, title="測試 plan", extraMeta="", projectDir=None):
    plansDir = (projectDir or tmp_path) / "plans"
    plansDir.mkdir(parents=True, exist_ok=True)
    path = plansDir / fileName
    path.write_text(planText(planId or "P" + fileName[:4], title, rows, extraMeta), encoding="utf-8")
    return path
  return write


def git(cwd, *args):
  subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def gitProject(tmp_path):
  projectDir = tmp_path / "proj"
  projectDir.mkdir()
  git(projectDir, "init", "-q", "-b", "main")
  git(projectDir, "config", "user.name", "tester")
  git(projectDir, "config", "user.email", "tester@example.com")
  (projectDir / "README.md").write_text("demo\n", encoding="utf-8")
  git(projectDir, "add", ".")
  git(projectDir, "commit", "-q", "-m", "init")
  return projectDir


import json


@pytest.fixture
def startBoard(gitProject, writePlan, tmp_path, monkeypatch):
  import board as boardCli
  from conductor import Conductor
  from helpers import fakeConfig

  conductors = []

  def start(rows, scenario=None, **configOverrides):
    scenarioDir = tmp_path / "scenario"
    scenarioDir.mkdir(exist_ok=True)
    (scenarioDir / "scenario.json").write_text(json.dumps(scenario or {}), encoding="utf-8")
    monkeypatch.setenv("FAKE_SCENARIO", str(scenarioDir / "scenario.json"))
    writePlan("0001-demo.md", rows, planId="P0001", projectDir=gitProject)
    assert boardCli.cmdSubmit(gitProject, "P0001") == 0
    assert boardCli.cmdApprove(gitProject, "P0001", confirm=lambda prompt: "y") == 0
    conductor = Conductor(gitProject, config=fakeConfig(**configOverrides), out=lambda line: None)
    conductors.append(conductor)
    return conductor, scenarioDir

  yield start
  # 卡住情境會留下睡 120 秒的假 agent，Windows 上它們會鎖住暫存資料夾，測試結束要先殺掉
  for conductor in conductors:
    for info in list(conductor.jobs.values()):
      info["job"].kill()
