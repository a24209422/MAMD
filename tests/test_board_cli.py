import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import board
import board_core
import board_git

toolDir = Path(__file__).resolve().parent.parent
cleanRows = ["| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | | 驗證一 |",
             "| R1 | | D1 | | T2 任務二 | T1 | 驗證二 |"]


def eventTypes(projectDir):
  return [event["type"] for event in board_core.readEvents(projectDir)]


def test_submitRejectsRedAndTaskWithoutId(gitProject, writePlan):
  writePlan("0001-red.md", ["| R1 需求 | 理解 | D1 做法 | - | T1 任務 | T9 | 驗證 |"], planId="P0001", projectDir=gitProject)
  writePlan("0002-noid.md", ["| R1 需求 | 理解 | D1 做法 | - | 沒有編號的任務 | | 驗證 |"], planId="P0002", projectDir=gitProject)
  assert board.cmdSubmit(gitProject, "P0001") == 1
  assert board.cmdSubmit(gitProject, "P0002") == 1
  assert board.cmdSubmit(gitProject, "P0099") == 1
  assert eventTypes(gitProject) == []


def test_submitAndApproveRejectPlanWithoutTitle(gitProject, capsys):
  plansDir = gitProject / "plans"
  plansDir.mkdir()
  (plansDir / "0001-untitled.md").write_text(
    "---\nid: P0001\ncreated: 2026-10-25\n---\n\n"
    "| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |\n|---|---|---|---|---|---|---|\n"
    + "\n".join(cleanRows) + "\n", encoding="utf-8")
  assert board.cmdSubmit(gitProject, "P0001") == 1
  assert "# 標題" in capsys.readouterr().out
  assert board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: "y") == 1
  assert eventTypes(gitProject) == []


def test_submitWritesEventWithCounts(gitProject, writePlan, capsys):
  writePlan("0001-ok.md", cleanRows + ["| | | D2 額外做法 | | T3 額外任務 | | 驗證三 |"], planId="P0001", projectDir=gitProject)
  assert board.cmdSubmit(gitProject, "P0001") == 0
  event = board_core.readEvents(gitProject)[0]
  assert event["type"] == "planSubmitted"
  assert event["data"]["orange"] == 1
  assert "橘｜" in capsys.readouterr().out


def test_approveWritesTaskSnapshot(gitProject, writePlan):
  writePlan("0001-ok.md", cleanRows, planId="P0001", projectDir=gitProject)
  assert board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: "y") == 1
  board.cmdSubmit(gitProject, "P0001")
  assert board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: "y") == 0
  approved = board_core.readEvents(gitProject)[-1]
  assert approved["type"] == "planApproved"
  assert approved["data"]["baseCommit"] == board_git.headCommit(gitProject)
  assert [task["id"] for task in approved["data"]["tasks"]] == ["T1", "T2"]
  assert approved["data"]["tasks"][1]["dependsOn"] == ["T1"]


def test_approveAsksWhenWarningsAndCanCancel(gitProject, writePlan):
  writePlan("0001-ok.md", cleanRows + ["| | | D2 額外做法 | | T3 額外任務 | | 驗證三 |"], planId="P0001", projectDir=gitProject)
  board.cmdSubmit(gitProject, "P0001")
  asked = []
  assert board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: asked.append(prompt) or "n") == 1
  assert asked and "planApproved" not in eventTypes(gitProject)


def test_approveWarnsAboutTrackedChanges(gitProject, writePlan, capsys):
  writePlan("0001-ok.md", cleanRows, planId="P0001", projectDir=gitProject)
  board.cmdSubmit(gitProject, "P0001")
  (gitProject / "README.md").write_text("changed\n", encoding="utf-8")
  board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: "n")
  assert "還沒 commit 的改動" in capsys.readouterr().out


def test_rejectAndAbandon(gitProject, writePlan):
  writePlan("0001-ok.md", cleanRows, planId="P0001", projectDir=gitProject)
  assert board.cmdReject(gitProject, "P0001", "缺驗證") == 1
  board.cmdSubmit(gitProject, "P0001")
  assert board.cmdReject(gitProject, "P0001", "缺驗證") == 0
  assert board.cmdAbandon(gitProject, "P0001") == 0
  assert board.cmdAbandon(gitProject, "P0001") == 1
  assert eventTypes(gitProject) == ["planSubmitted", "planRejected", "planAbandoned"]


def test_decideOnlyWhenNeedsYou(gitProject, writePlan):
  writePlan("0001-ok.md", cleanRows, planId="P0001", projectDir=gitProject)
  board.cmdSubmit(gitProject, "P0001")
  board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: "y")
  assert board.cmdDecide(gitProject, "P0001", "T1", "retry") == 1
  board_core.appendEvent(gitProject, "needsYou", plan="P0001", task="T1", data={"kind": "noChanges", "reason": "沒有改動"})
  assert board.cmdDecide(gitProject, "P0001", "T1", "retry", "請改 a.py") == 0
  assert board_core.readEvents(gitProject)[-1]["data"] == {"action": "retry", "note": "請改 a.py"}


def test_stopAndResume(gitProject):
  assert board.cmdResume(gitProject) == 1
  assert board.cmdStop(gitProject) == 0
  assert board.cmdStop(gitProject) == 1
  assert board.cmdResume(gitProject) == 0
  assert eventTypes(gitProject) == ["paused", "resumed"]


def test_cleanupRemovesWorktreesOfFinishedPlan(gitProject, writePlan):
  writePlan("0001-ok.md", cleanRows, planId="P0001", projectDir=gitProject)
  board.cmdSubmit(gitProject, "P0001")
  board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: "y")
  board_git.ensurePlanWorktree(gitProject, "P0001", board_git.headCommit(gitProject))
  board_git.ensureTaskWorktree(gitProject, "P0001", "T1")
  assert board.cmdCleanup(gitProject, "P0001", confirm=lambda prompt: "y") == 1
  board.cmdAbandon(gitProject, "P0001")
  assert board.cmdCleanup(gitProject, "P0001", confirm=lambda prompt: "y") == 0
  assert not board_git.taskWorktreePath(gitProject, "P0001", "T1").exists()


def test_statusAndIndex(gitProject, writePlan, capsys):
  writePlan("0001-ok.md", cleanRows, planId="P0001", title="批次上傳", projectDir=gitProject)
  board.cmdSubmit(gitProject, "P0001")
  board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: "y")
  capsys.readouterr()
  assert board.cmdStatus(gitProject) == 0
  output = capsys.readouterr().out
  assert "P0001 批次上傳｜進行中｜完成 0/2" in output
  assert "T2 排隊中（第 1/3 輪）" in output
  assert board.cmdIndex(gitProject) == 0
  assert (gitProject / "plans" / "_review" / "index.html").exists()


def test_humanOnlyCommandsRefusedWhenPiped(gitProject):
  completed = subprocess.run([sys.executable, str(toolDir / "board.py"), "--project", str(gitProject), "stop"],
                             capture_output=True, text=True, encoding="utf-8", stdin=subprocess.PIPE)
  assert completed.returncode == 2
  assert "只能由使用者在終端機執行" in completed.stdout
  assert eventTypes(gitProject) == []


@pytest.mark.skipif(os.name != "nt", reason="NUL 裝置被 isatty 當成終端機是 Windows 才有的行為")
def test_humanOnlyCommandsRefusedWhenRedirectedToNul(gitProject):
  boardPath = toolDir / "board.py"

  def runWithNul(command):
    # cmd /c 會拿掉最外層的一對引號，裡面的路徑各自加引號才不怕空白
    commandLine = f'cmd /c ""{sys.executable}" "{boardPath}" --project "{gitProject}" {command} <NUL >NUL"'
    return subprocess.run(commandLine, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=60)

  # 先確認同樣的引號寫法確實執行到 board.py：status 不限終端機，應該成功
  status = runWithNul("status")
  assert status.returncode == 0, status.stderr
  stopped = runWithNul("stop")
  assert "paused" not in eventTypes(gitProject)
  assert stopped.returncode == 2, stopped.stderr


def test_hasTrackedChangesIgnoresUntracked(gitProject):
  (gitProject / "new.txt").write_text("x", encoding="utf-8")
  assert board_git.hasTrackedChanges(gitProject) is False
  (gitProject / "README.md").write_text("changed\n", encoding="utf-8")
  assert board_git.hasTrackedChanges(gitProject) is True


def test_statusAndSubmitWarnAboutUnreadablePlan(gitProject, writePlan, capsys):
  writePlan("0001-ok.md", cleanRows, planId="P0001", projectDir=gitProject)
  (gitProject / "plans" / "0002-utf16.md").write_text("---\nid: P0002\n---\n", encoding="utf-16")
  assert board.cmdStatus(gitProject) == 0
  output = capsys.readouterr().out
  assert output.startswith("警告｜0002-utf16.md 無法讀取（UnicodeDecodeError），已略過：")
  assert "P0001 測試 plan｜草稿" in output
  assert board.cmdSubmit(gitProject, "P0001") == 0
  assert capsys.readouterr().out.startswith("警告｜0002-utf16.md 無法讀取")


def test_approveRefusesTaskWithoutIdAddedAfterSubmit(gitProject, writePlan, capsys):
  writePlan("0001-ok.md", cleanRows, planId="P0001", projectDir=gitProject)
  assert board.cmdSubmit(gitProject, "P0001") == 0
  writePlan("0001-ok.md", cleanRows + ["| R1 | | D1 | | 沒有編號的任務 | | 驗證三 |"], planId="P0001",
            projectDir=gitProject)
  asked = []
  assert board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: asked.append(prompt) or "y") == 1
  assert "planApproved" not in eventTypes(gitProject)
  assert asked == []
  assert "紅｜任務「沒有編號的任務」沒有編號，無法排程" in capsys.readouterr().out


def test_approveWarningsListsProblemsAndUncommittedChanges(gitProject, writePlan):
  writePlan("0001-ok.md", cleanRows + ["| | | D2 額外做法 | | T3 額外任務 | | 驗證三 |"], planId="P0001",
            projectDir=gitProject)
  plan = board_core.loadPlans(gitProject)["P0001"]
  warnings = board.approveWarnings(gitProject, plan)
  assert warnings and all(warning.startswith(("橘｜", "黃｜")) for warning in warnings)
  (gitProject / "README.md").write_text("changed\n", encoding="utf-8")
  assert board.approveWarnings(gitProject, plan)[-1] == "主資料夾有還沒 commit 的改動，這些改動不會進 plan 分支"


def test_stopWritesReasonConstant(gitProject):
  assert board.cmdStop(gitProject) == 0
  assert board_core.readEvents(gitProject)[-1]["data"] == {"reason": board.stopReason}


snippetHeading = "## agent-board 流程（寫 plan 的 agent 遵守）"


def readRaw(path):
  return path.read_bytes().decode("utf-8")


def writeRaw(path, text):
  # write_text 在 Windows 會把 \n 換成 CRLF，測試換行格式要寫原始位元組
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_bytes(text.encode("utf-8"))


@pytest.fixture
def fakeTddCrap(tmp_path, monkeypatch):
  skillDir = tmp_path / "skills" / "tdd-crap"
  writeRaw(skillDir / "scripts" / "quality.py", "")
  writeRaw(skillDir / "requirements.txt", "pytest\ncoverage\nradon\n")
  monkeypatch.setattr(board, "tddCrapDir", skillDir)
  monkeypatch.setattr(board, "missingPackages", lambda: [])
  return skillDir


def runInit(projectDir, *args):
  return subprocess.run([sys.executable, str(toolDir / "board.py"), "--project", str(projectDir), "init", *args],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


def test_initCreatesAgentsAndGitignore(gitProject, capsys):
  assert board.cmdInit(gitProject, skipTests=True) == 0
  agents = readRaw(gitProject / "AGENTS.md")
  assert agents.startswith(board.agentsStartMarker + "\n")
  assert agents.endswith(board.agentsEndMarker + "\n")
  snippet = (toolDir / "docs" / "AGENTS-snippet.md").read_text(encoding="utf-8").strip()
  assert snippet.replace("{{boardPy}}", str(board.boardPyPath)) in agents
  assert readRaw(gitProject / ".gitignore") == ".board/\nplans/_review/\n"
  assert "git add AGENTS.md .gitignore .claude/skills/agent-board-plan/SKILL.md" in capsys.readouterr().out
  assert json.loads(readRaw(gitProject / ".board" / "config.json")) == {"testCommand": "", "skipTests": True}


def test_initWritesWhereBoardPyActuallyIs(gitProject, monkeypatch):
  # 別人 clone 到哪裡都不一定，Codex 要用 init 寫進去的路徑執行 submit
  assert board.cmdInit(gitProject, skipTests=True) == 0
  agents = readRaw(gitProject / "AGENTS.md")
  assert "{{boardPy}}" not in agents
  assert f"python {Path(board.__file__).resolve()} submit P0007" in agents
  monkeypatch.setattr(board, "boardPyPath", Path("C:/my tools/agent-board/board.py"))
  assert board.cmdInit(gitProject, skipTests=True) == 0
  assert f'python "{Path("C:/my tools/agent-board/board.py")}" status' in readRaw(gitProject / "AGENTS.md")


def test_initKeepsExistingContentAndIsIdempotent(gitProject, capsys):
  writeRaw(gitProject / "AGENTS.md", "# 我的規則\n\n- 用中文")
  writeRaw(gitProject / ".gitignore", "node_modules/\n/.board\n")
  assert board.cmdInit(gitProject, skipTests=True) == 0
  agents = readRaw(gitProject / "AGENTS.md")
  assert agents.startswith("# 我的規則\n\n- 用中文\n\n" + board.agentsStartMarker)
  assert agents.count(board.agentsStartMarker) == 1
  assert readRaw(gitProject / ".gitignore") == "node_modules/\n/.board\nplans/_review/\n"
  capsys.readouterr()
  assert board.cmdInit(gitProject, skipTests=True) == 0
  assert readRaw(gitProject / "AGENTS.md") == agents
  out = capsys.readouterr().out
  assert "AGENTS.md：已是最新" in out and "不用 commit" in out


def test_initReplacesOldBlockAndKeepsCrlf(gitProject):
  writeRaw(gitProject / "AGENTS.md",
           f"# 前面\r\n\r\n{board.agentsStartMarker}\r\n舊的規則\r\n{board.agentsEndMarker}\r\n\r\n# 後面\r\n")
  writeRaw(gitProject / ".gitignore", "dist/\r\n")
  assert board.cmdInit(gitProject, skipTests=True) == 0
  agents = readRaw(gitProject / "AGENTS.md")
  assert "舊的規則" not in agents and snippetHeading in agents
  assert agents.startswith("# 前面\r\n\r\n" + board.agentsStartMarker + "\r\n")
  assert agents.endswith(board.agentsEndMarker + "\r\n\r\n# 後面\r\n")
  assert "\n" not in agents.replace("\r\n", "")
  assert readRaw(gitProject / ".gitignore") == "dist/\r\n.board/\r\nplans/_review/\r\n"


def test_initRefusesManuallyPastedRules(gitProject, capsys):
  original = f"# 專案\n\n{snippetHeading}\n\n舊的規則\n"
  writeRaw(gitProject / "AGENTS.md", original)
  assert board.cmdInit(gitProject, skipTests=True) == 1
  assert "第 3 行" in capsys.readouterr().out
  assert readRaw(gitProject / "AGENTS.md") == original
  assert not (gitProject / ".gitignore").exists()
  assert not (gitProject / ".claude").exists()


def test_initNeedsGitRepoWithCommit(tmp_path, capsys):
  plain = tmp_path / "plain"
  plain.mkdir()
  assert board.cmdInit(plain) == 1
  assert "git init" in capsys.readouterr().out
  subprocess.run(["git", "init", "-q"], cwd=plain, check=True)
  assert board.cmdInit(plain) == 1
  assert "還沒有任何 commit" in capsys.readouterr().out
  assert not (plain / "AGENTS.md").exists()


def test_initTddWritesConfig(gitProject, fakeTddCrap, monkeypatch, capsys):
  (gitProject / "src").mkdir()
  (gitProject / "tests").mkdir()
  monkeypatch.setattr(board, "missingPackages", lambda: ["radon"])
  assert board.cmdInit(gitProject, tdd=True) == 0
  command = f"python {(fakeTddCrap / 'scripts' / 'quality.py').as_posix()} --source src -- tests"
  config = json.loads((gitProject / ".board" / "config.json").read_text(encoding="utf-8"))
  assert config == {"tddCrap": True, "testCommand": command}
  assert board_core.loadConfig(gitProject)["testCommand"] == command
  out = capsys.readouterr().out
  assert "radon" in out and "pip install -r" in out


def test_initTddKeepsOtherSettingsAndUsesGivenFolders(gitProject, fakeTddCrap):
  for folder in ("lib", "app", "spec"):
    (gitProject / folder).mkdir()
  writeRaw(gitProject / ".board" / "config.json", '{"maxParallel": 1}')
  assert board.cmdInit(gitProject, tdd=True, sources=["lib", "app"], testsDir="spec") == 0
  config = json.loads((gitProject / ".board" / "config.json").read_text(encoding="utf-8"))
  assert config["maxParallel"] == 1 and config["tddCrap"] is True
  assert config["testCommand"].endswith(" --source lib --source app -- spec")
  # 已經是同一個指令時重跑不算衝突
  assert board.cmdInit(gitProject, tdd=True, sources=["lib", "app"], testsDir="spec") == 0


def test_initTddChecksEverythingBeforeWriting(gitProject, fakeTddCrap, monkeypatch, capsys):
  (gitProject / "src").mkdir()
  assert board.cmdInit(gitProject, tdd=True) == 1
  assert "找不到資料夾 tests" in capsys.readouterr().out
  (gitProject / "tests").mkdir()
  writeRaw(gitProject / ".board" / "config.json", '{"testCommand": "pytest -q"}')
  assert board.cmdInit(gitProject, tdd=True) == 1
  assert "pytest -q" in capsys.readouterr().out
  assert json.loads(readRaw(gitProject / ".board" / "config.json")) == {"testCommand": "pytest -q"}
  (gitProject / ".board" / "config.json").unlink()
  monkeypatch.setattr(board, "tddCrapDir", gitProject / "nowhere")
  assert board.cmdInit(gitProject, tdd=True) == 1
  out = capsys.readouterr().out
  assert "找不到 tdd-crap" in out and "install_skill.py" in out
  assert not (gitProject / "AGENTS.md").exists() and not (gitProject / ".gitignore").exists()
  assert not (gitProject / ".claude").exists()


def test_initCommandLine(gitProject):
  refused = runInit(gitProject, "--source", "lib")
  assert refused.returncode == 2 and "--tdd" in refused.stdout
  completed = runInit(gitProject, "--skip-tests")
  assert completed.returncode == 0 and "AGENTS.md" in completed.stdout
  assert (gitProject / "AGENTS.md").exists()


def test_initWritesPlanSkillForClaude(gitProject, capsys):
  assert board.cmdInit(gitProject, skipTests=True) == 0
  skill = readRaw(gitProject / ".claude" / "skills" / "agent-board-plan" / "SKILL.md")
  assert skill.startswith("---\nname: agent-board-plan\n")
  assert "disable-model-invocation: true" in skill and board.planSkillMarker in skill
  assert snippetHeading in skill
  assert f"python {Path(board.__file__).resolve()} submit P0007" in skill
  capsys.readouterr()
  assert board.cmdInit(gitProject, skipTests=True) == 0
  out = capsys.readouterr().out
  assert ".claude/skills/agent-board-plan/SKILL.md：已是最新" in out and "不用 commit" in out


def test_initRefusesSkillItDidNotWrite(gitProject, capsys):
  mine = "---\nname: agent-board-plan\n---\n我自己的\n"
  writeRaw(gitProject / ".claude" / "skills" / "agent-board-plan" / "SKILL.md", mine)
  assert board.cmdInit(gitProject, skipTests=True) == 1
  assert "不是 init 產生的" in capsys.readouterr().out
  assert readRaw(gitProject / ".claude" / "skills" / "agent-board-plan" / "SKILL.md") == mine
  assert not (gitProject / "AGENTS.md").exists()
