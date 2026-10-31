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
  assert approved["data"]["testTiming"] == "afterMerge"


def test_approveAsksWhenWarningsAndCanCancel(gitProject, writePlan):
  writePlan("0001-ok.md", cleanRows + ["| | | D2 額外做法 | | T3 額外任務 | | 驗證三 |"], planId="P0001", projectDir=gitProject)
  board.cmdSubmit(gitProject, "P0001")
  asked = []
  assert board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: asked.append(prompt) or "n") == 1
  assert asked and "planApproved" not in eventTypes(gitProject)


def test_approveAfterConfirmingWarningsWritesFullSnapshot(gitProject, writePlan, capsys):
  writePlan("0001-ok.md", cleanRows + ["| | | D2 額外做法 | | T3 額外任務 | | 驗證三 |"], planId="P0001", projectDir=gitProject)
  board.cmdSubmit(gitProject, "P0001")
  capsys.readouterr()
  asked = []
  # 確認輸入前後有空白、大寫也算同意；testTiming=None 時事件不帶這個欄位
  assert board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: asked.append(prompt) or " Y ",
                          testTiming=None) == 0
  assert asked == ["確定核准？輸入 y 繼續："]
  output = capsys.readouterr().out
  assert output.startswith("核准前請確認：\n  橘｜")
  assert output.endswith("P0001 已核准，Alfred 會開始執行。\n")
  approved = board_core.readEvents(gitProject)[-1]
  assert approved["type"] == "planApproved" and approved["plan"] == "P0001"
  assert approved["data"] == {
    "baseCommit": board_git.headCommit(gitProject),
    "branch": board_git.planBranch("P0001"),
    "tasks": [
      {"id": "T1", "text": "任務一", "dependsOn": [], "requirementIds": ["R1"]},
      {"id": "T2", "text": "任務二", "dependsOn": ["T1"], "requirementIds": ["R1"]},
      {"id": "T3", "text": "額外任務", "dependsOn": [], "requirementIds": []},
    ],
  }


def test_approveWarnsAboutTrackedChanges(gitProject, writePlan, capsys):
  writePlan("0001-ok.md", cleanRows, planId="P0001", projectDir=gitProject)
  board.cmdSubmit(gitProject, "P0001")
  (gitProject / "README.md").write_text("changed\n", encoding="utf-8")
  board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: "n")
  assert "還沒 commit 的改動" in capsys.readouterr().out


def test_submitCommitsOnlyThePlanFile(gitProject, writePlan, capsys):
  (gitProject / "staged.txt").write_text("staged\n", encoding="utf-8")
  board_git.runGit(gitProject, "add", "staged.txt")
  (gitProject / "README.md").write_text("changed\n", encoding="utf-8")
  writePlan("0001-ok.md", cleanRows, planId="P0001", title="批次上傳", projectDir=gitProject)
  before = board_git.headCommit(gitProject)
  assert board.cmdSubmit(gitProject, "P0001") == 0
  assert "已 commit plans/0001-ok.md" in capsys.readouterr().out
  assert board_git.runGit(gitProject, "rev-parse", "HEAD~1") == before
  assert board_git.runGit(gitProject, "show", "--name-only", "--format=", "HEAD") == "plans/0001-ok.md"
  assert board_git.runGit(gitProject, "log", "-1", "--format=%s") == "P0001 送出 plan：批次上傳"
  # 其他已暫存和沒暫存的改動都還在，沒有被 commit
  assert board_git.runGit(gitProject, "diff", "--cached", "--name-only") == "staged.txt"
  assert board_git.runGit(gitProject, "diff", "--name-only") == "README.md"
  assert (gitProject / "README.md").read_text(encoding="utf-8") == "changed\n"
  assert eventTypes(gitProject) == ["planSubmitted"]
  committed = board_git.headCommit(gitProject)
  assert board.cmdSubmit(gitProject, "P0001") == 0
  assert board_git.headCommit(gitProject) == committed
  assert "已 commit" not in capsys.readouterr().out


def test_submitStopsWhenCommitFails(gitProject, writePlan, capsys):
  hookPath = gitProject / ".git" / "hooks" / "pre-commit"
  hookPath.parent.mkdir(parents=True, exist_ok=True)
  hookPath.write_bytes(b"#!/bin/sh\necho blocked by hook >&2\nexit 1\n")
  hookPath.chmod(0o755)
  writePlan("0001-ok.md", cleanRows, planId="P0001", title="批次上傳", projectDir=gitProject)
  before = board_git.headCommit(gitProject)
  assert board.cmdSubmit(gitProject, "P0001") == 1
  output = capsys.readouterr().out
  assert "commit 失敗，沒有送出" in output and "blocked by hook" in output
  assert "  git --literal-pathspecs add -- plans/0001-ok.md" in output
  assert "  git --literal-pathspecs commit -F .board/commit-message-P0001.txt -- plans/0001-ok.md" in output
  assert board_git.headCommit(gitProject) == before
  assert "planSubmitted" not in eventTypes(gitProject)
  # 使用者照印出來的指令自己 commit 之後，再送出就會通過
  hookPath.unlink()
  board_git.runGit(gitProject, "--literal-pathspecs", "add", "--", "plans/0001-ok.md")
  board_git.runGit(gitProject, "--literal-pathspecs", "commit", "-F", ".board/commit-message-P0001.txt", "--",
                   "plans/0001-ok.md")
  assert board_git.runGit(gitProject, "log", "-1", "--format=%s") == "P0001 送出 plan：批次上傳"
  committed = board_git.headCommit(gitProject)
  assert board.cmdSubmit(gitProject, "P0001") == 0
  assert board_git.headCommit(gitProject) == committed
  assert eventTypes(gitProject) == ["planSubmitted"]


def test_shellQuoteForPowerShellAndSh():
  assert board.shellQuote("plans/0001-ok.md", windows=True) == "plans/0001-ok.md"
  assert board.shellQuote("plans/0001 a's.md", windows=True) == "'plans/0001 a''s.md'"
  assert board.shellQuote("a’b $x", windows=True) == "'a’’b $x'"
  assert board.shellQuote("plans/0001-ok.md", windows=False) == "plans/0001-ok.md"
  assert board.shellQuote("plans/0001 a's.md", windows=False) == "'plans/0001 a'\"'\"'s.md'"


def test_printedCommitCommandsRunInShell(gitProject, capsys):
  plansDir = gitProject / "plans"
  plansDir.mkdir()
  # 檔名的 [ab] 當成萬用字元會匹配到這個已 commit、又被改過的檔案
  decoyPath = plansDir / "0001-a b's $x a.md"
  decoyPath.write_text("舊內容\n", encoding="utf-8")
  board_git.runGit(gitProject, "add", "--", "plans")
  board_git.runGit(gitProject, "commit", "-q", "-m", "decoy")
  decoyPath.write_text("改過\n", encoding="utf-8")
  hookPath = gitProject / ".git" / "hooks" / "pre-commit"
  hookPath.parent.mkdir(parents=True, exist_ok=True)
  hookPath.write_bytes(b"#!/bin/sh\nexit 1\n")
  hookPath.chmod(0o755)
  title = "新增 \"CSV import\" 和 'x' $HOME `y` & z"
  planPath = plansDir / "0001-a b's $x [ab].md"
  planPath.write_text(
    f"---\nid: P0001\ncreated: 2026-10-25\n---\n\n# {title}\n\n"
    "| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |\n|---|---|---|---|---|---|---|\n"
    + "\n".join(cleanRows) + "\n", encoding="utf-8")
  assert board.cmdSubmit(gitProject, "P0001") == 1
  commands = [line.strip() for line in capsys.readouterr().out.splitlines()[-2:]]
  assert commands[0].startswith("git --literal-pathspecs add ")
  assert commands[1].startswith("git --literal-pathspecs commit -F ")
  hookPath.unlink()
  for command in commands:
    # 用印出來時對應的 shell 執行，確認使用者照打就能 commit
    shell = (["powershell", "-NoProfile", "-NonInteractive", "-Command", command] if os.name == "nt"
             else ["sh", "-c", command])
    completed = subprocess.run(shell, cwd=gitProject, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
  assert board_git.runGit(gitProject, "log", "-1", "--format=%s") == f"P0001 送出 plan：{title}"
  assert board_git.runGit(gitProject, "-c", "core.quotePath=false", "show", "--name-only", "--format=",
                          "HEAD") == "plans/0001-a b's $x [ab].md"
  assert board_git.runGit(gitProject, "-c", "core.quotePath=false", "diff", "--name-only") == "plans/0001-a b's $x a.md"
  assert board.cmdSubmit(gitProject, "P0001") == 0
  assert eventTypes(gitProject) == ["planSubmitted"]


def test_planFileNameWithWildcardsIsMatchedLiterally(gitProject, writePlan, capsys):
  decoyPath = writePlan("0001-a.md", cleanRows, planId="P0002", projectDir=gitProject)
  board_git.runGit(gitProject, "add", "--", "plans/0001-a.md")
  board_git.runGit(gitProject, "commit", "-q", "-m", "decoy")
  writePlan("0001-[ab].md", cleanRows, planId="P0001", projectDir=gitProject)
  # 已 commit 而且乾淨的 0001-a.md 不能讓沒被追蹤的 0001-[ab].md 被當成已經 commit
  assert board_git.fileMatchesHead(gitProject, "plans/0001-[ab].md") is False
  assert board.cmdSubmit(gitProject, "P0001") == 0
  assert board_git.runGit(gitProject, "show", "--name-only", "--format=", "HEAD") == "plans/0001-[ab].md"
  # 送出後改了 plan，又剛好改到匹配得到的 0001-a.md：核准要擋下，重新送出也只 commit 這份 plan
  writePlan("0001-[ab].md", cleanRows, planId="P0001", title="改過的標題", projectDir=gitProject)
  decoyPath.write_text("改過\n", encoding="utf-8")
  capsys.readouterr()
  assert board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: "y") == 1
  assert "重新執行 submit" in capsys.readouterr().out
  assert board.cmdSubmit(gitProject, "P0001") == 0
  assert board_git.runGit(gitProject, "show", "--name-only", "--format=", "HEAD") == "plans/0001-[ab].md"
  assert board_git.runGit(gitProject, "diff", "--name-only") == "plans/0001-a.md"
  assert board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: "y") == 0


def test_submitShowsCommitFailureReasonPrintedToStdout(gitProject, writePlan, monkeypatch, capsys):
  realRun = subprocess.run

  def fakeRun(command, *args, **kwargs):
    # 模擬 git commit 把失敗原因只印在 stdout、stderr 是空的
    if command[:3] == ["git", board_git.literalPathspecs, "commit"]:
      return subprocess.CompletedProcess(command, 1, stdout="reason on stdout\n", stderr="")
    return realRun(command, *args, **kwargs)

  monkeypatch.setattr(board_git.subprocess, "run", fakeRun)
  writePlan("0001-ok.md", cleanRows, planId="P0001", projectDir=gitProject)
  assert board.cmdSubmit(gitProject, "P0001") == 1
  output = capsys.readouterr().out
  assert "reason on stdout" in output and "git --literal-pathspecs add -- plans/0001-ok.md" in output
  assert "planSubmitted" not in eventTypes(gitProject)


def test_approveRefusesPlanChangedAfterSubmit(gitProject, writePlan, capsys):
  writePlan("0001-ok.md", cleanRows, planId="P0001", projectDir=gitProject)
  assert board.cmdSubmit(gitProject, "P0001") == 0
  writePlan("0001-ok.md", cleanRows, planId="P0001", title="改過的標題", projectDir=gitProject)
  capsys.readouterr()
  asked = []
  assert board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: asked.append(prompt) or "y") == 1
  output = capsys.readouterr().out
  assert "送出後又被改過" in output and "重新執行 submit" in output
  assert asked == []
  assert "planApproved" not in eventTypes(gitProject)
  assert board.cmdSubmit(gitProject, "P0001") == 0
  assert board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: "y") == 0
  assert board_core.readEvents(gitProject)[-1]["data"]["baseCommit"] == board_git.headCommit(gitProject)


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


def test_statusShowsPauseRejectReasonAgentAndNeeds(gitProject, writePlan, capsys):
  writePlan("0001-ok.md", cleanRows, planId="P0001", title="批次上傳", projectDir=gitProject)
  writePlan("0002-ok.md", cleanRows, planId="P0002", title="系統監控", projectDir=gitProject)
  board.cmdSubmit(gitProject, "P0001")
  board.cmdReject(gitProject, "P0001", "缺驗證")
  board.cmdSubmit(gitProject, "P0002")
  board.cmdApprove(gitProject, "P0002", confirm=lambda prompt: "y")
  board_core.appendEvent(gitProject, "taskStarted", plan="P0002", task="T1", agent="writer-1", roundNumber=1)
  board_core.appendEvent(gitProject, "needsYou", plan="P0002", task="T2", data={"kind": "noChanges", "reason": "沒有改動"})
  board.cmdStop(gitProject)
  capsys.readouterr()
  assert board.cmdStatus(gitProject) == 0
  assert capsys.readouterr().out.splitlines() == [
    f"已暫停：{board.stopReason}",
    "P0001 批次上傳｜退回｜退回原因：缺驗證",
    "P0002 系統監控｜進行中｜完成 0/2",
    "  T1 寫程式中（第 1/3 輪，writer-1）",
    "  T2 等你處理（第 1/3 輪）｜沒有改動",
  ]
  assert board.cmdStatus(gitProject, "P0001") == 0
  assert capsys.readouterr().out.splitlines() == [f"已暫停：{board.stopReason}", "P0001 批次上傳｜退回｜退回原因：缺驗證"]


def writeEvents(projectDir, events):
  boardPath = board_core.boardDir(projectDir)
  boardPath.mkdir(parents=True, exist_ok=True)
  lines = [json.dumps(event, ensure_ascii=False) + "\n" for event in events]
  (boardPath / board_core.eventsFileName).write_text("".join(lines), encoding="utf-8")


def test_statsOfPlanMatchesReferenceValues(gitProject, writePlan, capsys):
  from test_board_stats import p0001Events
  writePlan("0001-ok.md", cleanRows, planId="P0001", title="系統監控", projectDir=gitProject)
  writeEvents(gitProject, p0001Events())
  assert board.cmdStats(gitProject, "P0001") == 0
  lines = capsys.readouterr().out.splitlines()
  assert lines[0] == "P0001 系統監控｜完成"
  assert [line.strip() for line in lines[1:16]] == [
    "任務：完成 5／放棄 0／全部 5",
    "平均輪數：1.2",
    "寫完次數：6",
    "審查：6 次（通過 5、不通過 1）",
    "第一次審查就通過：4/5",
    "測試：1 次，失敗 0，退回 0，沿用 0",
    "寫完後被退回：1/6",
    "你的決定：1（retry 1）",
    "還在等你：0",
    "等你的時間：9 分 52 秒",
    "agent 失敗、合併衝突：0、0",
    "plan 退回：0",
    "耗時：1 小時 13 分 29 秒",
    "模型：writer：claude opus xhigh；reviewer：codex gpt-6.1-sol xhigh",
    "每個任務的輪數：",
  ]
  assert [line.strip() for line in lines[16:]] == ["T1 1 輪｜完成", "T2 1 輪｜完成", "T3 2 輪｜完成",
                                                   "T4 1 輪｜完成", "T5 1 輪｜完成"]


def test_statsListsApprovedPlansAndTotal(gitProject, writePlan, capsys):
  from test_board_stats import p0001Events
  writePlan("0001-ok.md", cleanRows, planId="P0001", title="系統監控", projectDir=gitProject)
  writePlan("0002-ok.md", cleanRows, planId="P0002", projectDir=gitProject)
  writeEvents(gitProject, p0001Events())
  board.cmdSubmit(gitProject, "P0002")
  capsys.readouterr()
  assert board.cmdStats(gitProject) == 0
  summary = "任務 5／0／5｜平均輪數 1.2｜第一次審查就通過 4/5｜寫完後被退回 1/6｜你的決定 1｜耗時 1 小時 13 分 29 秒"
  assert capsys.readouterr().out.splitlines() == [f"P0001 系統監控｜完成｜{summary}", f"總計（1 份）｜{summary}"]
  # 待核准的 plan 不列，也算不出成效
  assert board.cmdStats(gitProject, "P0002") == 1
  assert "還沒核准過" in capsys.readouterr().out


def test_statsBeforeAnyApprovalAndTaskNotStarted(gitProject, writePlan, capsys):
  writePlan("0001-ok.md", cleanRows, planId="P0001", projectDir=gitProject)
  assert board.cmdStats(gitProject) == 0
  assert capsys.readouterr().out.strip() == "還沒有核准過的 plan"
  board.cmdSubmit(gitProject, "P0001")
  board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: "y")
  capsys.readouterr()
  assert board.cmdStats(gitProject, "P0001") == 0
  output = capsys.readouterr().out
  assert "平均輪數：-" in output
  assert "秒（進行中）" in output
  assert "T2 還沒開始｜排隊中" in output


def test_statsIsNotHumanOnly(gitProject, writePlan):
  from test_board_stats import p0001Events
  writePlan("0001-ok.md", cleanRows, planId="P0001", projectDir=gitProject)
  writeEvents(gitProject, p0001Events())
  completed = subprocess.run([sys.executable, str(toolDir / "board.py"), "--project", str(gitProject), "stats", "P0001"],
                             capture_output=True, text=True, encoding="utf-8", stdin=subprocess.PIPE)
  assert completed.returncode == 0, completed.stderr
  assert "平均輪數：1.2" in completed.stdout
  assert "stats" not in board.humanOnlyCommands


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


class FakeStream:
  def __init__(self, tty):
    self.tty = tty

  def isatty(self):
    return self.tty


@pytest.mark.parametrize("stdinTty, stdoutTty, expected", [(True, True, True), (True, False, False), (False, True, False)])
def test_isHumanOutsideWindowsNeedsBothStreamsToBeTerminals(monkeypatch, stdinTty, stdoutTty, expected):
  monkeypatch.setattr(board.os, "name", "posix")
  monkeypatch.setattr(board.sys, "stdin", FakeStream(stdinTty))
  monkeypatch.setattr(board.sys, "stdout", FakeStream(stdoutTty))
  assert board.isHuman() is expected


def test_isHumanOnWindowsAsksConsoleInsteadOfIsatty(monkeypatch):
  # isatty 都說是終端機，但只要有一端不是主控台就不算使用者
  monkeypatch.setattr(board.os, "name", "nt")
  stdin, stdout = FakeStream(True), FakeStream(True)
  monkeypatch.setattr(board.sys, "stdin", stdin)
  monkeypatch.setattr(board.sys, "stdout", stdout)
  monkeypatch.setattr(board, "hasWindowsConsole", lambda stream: stream is stdin)
  assert board.isHuman() is False
  monkeypatch.setattr(board, "hasWindowsConsole", lambda stream: True)
  assert board.isHuman() is True


@pytest.mark.skipif(os.name != "nt", reason="hasWindowsConsole 只在 Windows 上使用")
def test_hasWindowsConsoleFalseForStreamWithoutHandle():
  assert board.hasWindowsConsole(FakeStream(True)) is False


@pytest.mark.skipif(os.name != "nt", reason="hasWindowsConsole 只在 Windows 上使用")
def test_hasWindowsConsoleFalseForRegularFile(tmp_path):
  with open(tmp_path / "out.txt", "w", encoding="utf-8") as stream:
    assert board.hasWindowsConsole(stream) is False


def test_missingPackagesListsOnlyUninstalledOnes(monkeypatch):
  monkeypatch.setattr(board, "tddPackages", ["pytest", "noSuchPackageForBoardTest"])
  assert board.missingPackages() == ["noSuchPackageForBoardTest"]


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


def test_initPrintsEveryStepInOrder(gitProject, capsys):
  writeRaw(gitProject / "AGENTS.md", "# 我的規則\n")
  assert board.cmdInit(gitProject, testCommand="python -m pytest -q") == 0
  assert capsys.readouterr().out.splitlines() == [
    "AGENTS.md：已在最後加上規則",
    ".gitignore：補上 .board/、plans/_review/",
    ".claude/skills/agent-board-plan/SKILL.md：已建立（在 Claude Code 打 /agent-board-plan 加上需求，就由 Claude 寫 plan）",
    "測試指令：python -m pytest -q",
    "若 Alfred 已在執行，測試設定變更後請重啟 Alfred 才會生效",
    "提醒：測試產物（例如 __pycache__/、.pytest_cache/）請自己加進 .gitignore，否則會被 commit 進任務分支",
    "接下來請 commit：",
    "  git add AGENTS.md .gitignore .claude/skills/agent-board-plan/SKILL.md",
    '  git commit -m "啟用 agent-board"',
  ]
  assert board.cmdInit(gitProject, testCommand="python -m pytest -q", skipTests=True) == 1
  assert capsys.readouterr().out.splitlines() == ["沒有修改任何檔案：", "  --tdd、--test-command、--skip-tests 只能選一個"]


def test_initRefusesMissingProjectFolder(tmp_path, capsys):
  missing = tmp_path / "missing"
  assert board.cmdInit(missing, skipTests=True) == 1
  assert capsys.readouterr().out == f"找不到專案資料夾：{missing}\n"
  assert not missing.exists()


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
