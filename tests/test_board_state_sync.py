import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import board
import board_api
import board_git
from board_review import renderReviewMarkdown
from board_setup.project import Options, initialize
from board_sync import (backupDirName, checkpointPath, isChoosable, newProjectId, progressPath, readCheckpoint,
                        readSnapshot, revisionOf, writeSnapshot)
from browser import browserPages, sharedBrowser  # noqa: F401  測試模組裡要看得到 fixture
from conftest import git
from helpers import makeEvent
from test_board_export import holdAlfredLock, legacyProject  # noqa: F401
from test_board_handoff import computers, planLines, publish, statusOutput  # noqa: F401
from test_board_setup import toolRoot
from test_board_sync_load import started, writeEvents
from test_server import post, serving
from test_server_projects import initialized, scoped
from test_web import dashboard, domHas, token as webToken  # noqa: F401

toolDir = Path(__file__).resolve().parent.parent


def restoreBranches(clone):
  # 第二台電腦照交接檔記錄的 commit 建出本機分支，選本機時才有完整來源可以交接
  for name, sha in readSnapshot(clone)["branches"].items():
    git(clone, "branch", "-f", name, sha)


def divergeWith(computers, capsys, localEvents, sourceEvents, prepare=lambda source, clone: None):
  source, clone, remote = computers
  assert initialize(clone, Options(skipTests=True), toolRoot) == 0
  restoreBranches(clone)
  writeEvents(clone, localEvents)
  writeEvents(source, sourceEvents)
  prepare(source, clone)
  incoming = publish(source, capsys, remote)
  git(clone, "pull", "-q", "--ff-only")
  return source, clone, remote, incoming


@pytest.fixture
def diverged(computers, capsys):
  # 兩台從同一版各自往下走：電腦 2 跑到第 3 輪，電腦 1 跑到第 2 輪並交接
  return divergeWith(computers, capsys, [started("T1", roundNumber=3)], [started("T1", roundNumber=2)])


def reviewedRound2():
  return [started("T1", roundNumber=2),
          makeEvent("reviewStarted", plan="P0001", task="T1", roundNumber=2, agent="codex"),
          makeEvent("reviewFailed", plan="P0001", task="T1", roundNumber=2, agent="codex",
                    notes="reviews/P0001-T1-r2.md", mustFix=1, suggest=0)]


def reviewNote(projectDir, mustFix):
  text = renderReviewMarkdown("P0001", "T1", 2, {"verdict": "fail", "mustFix": [{"ref": "R1", "text": mustFix}],
                                                 "suggest": []})
  path = projectDir / ".board" / "reviews" / "P0001-T1-r2.md"
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(text, encoding="utf-8")
  return text


def fileBytes(clone):
  return progressPath(clone).read_bytes(), checkpointPath(clone).read_bytes()


def backups(clone):
  directory = clone / ".board" / backupDirName
  return sorted(directory.iterdir()) if directory.exists() else []


def roundOfT1(projectDir, capsys):
  # P0001 T1 是兩台各自推進的任務；本機進度會多帶 agent 名稱，只比輪次
  lines = planLines(statusOutput(projectDir, capsys))
  line = lines[[index for index, text in enumerate(lines) if text.startswith("P0001 ")][0] + 1]
  assert line.startswith("  T1 寫程式中（第 ")
  return line.split("（第 ")[1][:3]


def stateSync(projectDir, side, capsys):
  capsys.readouterr()
  code = board.cmdStateSync(projectDir, side)
  return code, capsys.readouterr().out


def test_divergedProgressIsShownAndKeptUntilUserChooses(diverged, capsys):
  _, clone, _, incoming = diverged
  before = fileBytes(clone)

  output = statusOutput(clone, capsys)
  view = board_api.stateView(clone)

  assert "請決定要接續哪一邊" in output
  assert "state-sync --take incoming" in output and "--take local" in output
  assert f"收到 {incoming['revision'][:12]}" in output
  assert output.splitlines()[0].startswith("交接版本｜本機基底 ")
  assert roundOfT1(clone, capsys) == "3/3"
  assert view["sync"]["status"] == "diverged" and view["sync"]["choosable"] is True
  assert fileBytes(clone) == before
  assert backups(clone) == []


def test_takeLocalBacksUpAndHandsOffNewRevision(diverged, capsys):
  source, clone, remote, incoming = diverged
  replaced = fileBytes(clone)

  code, output = stateSync(clone, "local", capsys)

  assert code == 0, output
  assert "已保留本機進度，產生新交接版本" in output and "Git 分支、worktree 與未提交的檔案都沒有刪除" in output
  assert "請 commit 這些檔案：" in output and "請 push 這些分支：" in output
  [backup] = backups(clone)
  assert sorted(path.name for path in backup.iterdir()) == ["events.jsonl", "progress.json", "sync-checkpoint.json"]
  assert ((backup / "progress.json").read_bytes(), (backup / "sync-checkpoint.json").read_bytes()) == replaced
  chosen = readSnapshot(clone)
  assert chosen["previousRevision"] == incoming["revision"]
  assert readCheckpoint(clone)["base"]["revision"] == chosen["revision"]
  assert roundOfT1(clone, capsys) == "3/3"
  assert board_api.stateView(clone)["sync"]["choosable"] is False

  # 照常匯出交接，原本那台 pull 後就接續這一版
  published = publish(clone, capsys, remote)
  git(source, "pull", "-q", "--ff-only", str(remote), "main")
  assert published["revision"] == chosen["revision"]
  assert roundOfT1(source, capsys) == "3/3"
  assert board_api.stateView(source)["sync"]["baseRevision"] == chosen["revision"]


def test_takeIncomingBacksUpAndContinuesFromHandoff(diverged, capsys):
  _, clone, _, incoming = diverged
  events = (clone / ".board" / "events.jsonl").read_bytes()

  code, output = stateSync(clone, "incoming", capsys)

  assert code == 0, output
  assert "已改接續交接檔的進度" in output and "請 commit" not in output
  [backup] = backups(clone)
  assert (backup / "events.jsonl").read_bytes() == events
  assert readCheckpoint(clone)["base"]["revision"] == incoming["revision"]
  # 事件紀錄本身不刪，只是邊界移到最後，舊的本機進度不再算進來
  assert (clone / ".board" / "events.jsonl").read_bytes() == events
  assert roundOfT1(clone, capsys) == "2/3"
  capsys.readouterr()
  assert board.cmdExportState(clone) == 0
  assert readSnapshot(clone)["revision"] == incoming["revision"]


def test_takeIncomingReplacesConflictingReviewNote(computers, capsys):
  # 兩台在同一任務同一輪各自審查，結論不同；選交接檔後 writer 要照交接檔的必改重寫
  notes = {}

  def prepare(source, clone):
    notes["local"] = reviewNote(clone, "本機審查：請補 A 的測試")
    notes["incoming"] = reviewNote(source, "交接檔審查：請補 B 的測試")

  _, clone, _, incoming = divergeWith(computers, capsys, reviewedRound2(), reviewedRound2(), prepare)
  assert board_api.stateView(clone)["sync"]["choosable"] is True

  code, output = stateSync(clone, "incoming", capsys)

  assert code == 0, output
  [backup] = backups(clone)
  assert (backup / "reviews" / "P0001-T1-r2.md").read_text(encoding="utf-8") == notes["local"]
  assert (clone / ".board" / "reviews" / "P0001-T1-r2.md").read_text(encoding="utf-8") == notes["incoming"]
  # 沒有衝突的審查檔照舊留在原處，不搬進備份
  assert sorted(path.name for path in (backup / "reviews").iterdir()) == ["P0001-T1-r2.md"]
  capsys.readouterr()
  assert board.cmdExportState(clone) == 0
  assert readSnapshot(clone)["revision"] == incoming["revision"]


def lockCheckpoint(monkeypatch):
  # 模擬防毒或編輯器一直開著 checkpoint：換檔一律被拒，其他檔案照常寫入
  realReplace = os.replace

  def refuse(source, target):
    if Path(target).name == checkpointPath(Path(".")).name:
      raise PermissionError("被占用")
    realReplace(source, target)
  monkeypatch.setattr(os, "replace", refuse)


def test_failedCheckpointRestoresReviewNotes(computers, capsys, monkeypatch):
  notes = {}

  def prepare(source, clone):
    notes["local"] = reviewNote(clone, "本機審查：請補 A 的測試")
    reviewNote(source, "交接檔審查：請補 B 的測試")

  _, clone, _, _ = divergeWith(computers, capsys, reviewedRound2(), reviewedRound2(), prepare)
  before = fileBytes(clone)
  lockCheckpoint(monkeypatch)

  code, output = stateSync(clone, "incoming", capsys)

  assert code == 1 and "本機進度與交接檔都沒有更動" in output and "被其他程式占用" in output
  # 本機狀態沒換，writer 讀到的必改也要還是本機那一份
  assert (clone / ".board" / "reviews" / "P0001-T1-r2.md").read_text(encoding="utf-8") == notes["local"]
  assert fileBytes(clone) == before and backups(clone) == []


def test_failedCheckpointRestoresHandoffFile(diverged, capsys, monkeypatch):
  _, clone, _, _ = diverged
  before = fileBytes(clone)
  lockCheckpoint(monkeypatch)

  code, output = stateSync(clone, "local", capsys)

  assert code == 1 and "本機進度與交接檔都沒有更動" in output
  assert fileBytes(clone) == before and backups(clone) == []
  monkeypatch.undo()
  assert roundOfT1(clone, capsys) == "3/3"
  assert board_api.stateView(clone)["sync"]["choosable"] is True


def test_takeLocalWithSameContentStillHandsOffNewRevision(computers, capsys):
  # 兩台各自走出一樣的進度：內容相同但歷史不同，選本機仍要記下兩邊的版本鏈
  _, clone, _, incoming = divergeWith(computers, capsys, [started("T1", roundNumber=2)],
                                      [started("T1", roundNumber=2)])
  localBase = readCheckpoint(clone)["base"]
  assert board_api.stateView(clone)["sync"]["choosable"] is True

  code, output = stateSync(clone, "local", capsys)

  assert code == 0, output
  chosen = readSnapshot(clone)
  assert chosen["revision"] != incoming["revision"]
  assert chosen["previousRevision"] == incoming["revision"]
  assert localBase["revision"] in chosen["ancestors"]
  assert readCheckpoint(clone)["base"]["revision"] == chosen["revision"]


def test_choosingKeepsGitBranchesAndWorktreeFiles(diverged, capsys):
  _, clone, _, _ = diverged
  branches = board_git.runGit(clone, "branch", "--list")
  (clone / "未提交.txt").write_text("還沒 commit\n", encoding="utf-8")

  assert stateSync(clone, "incoming", capsys)[0] == 0

  assert board_git.runGit(clone, "branch", "--list") == branches
  assert (clone / "未提交.txt").read_text(encoding="utf-8") == "還沒 commit\n"


@pytest.mark.parametrize("side", ["incoming", "local"])
def test_alfredRunningRefusesChoice(diverged, capsys, side):
  _, clone, _, _ = diverged
  holdAlfredLock(clone)
  before = fileBytes(clone)

  code, output = stateSync(clone, side, capsys)

  assert code == 1
  assert "本機進度與交接檔都沒有更動" in output and "請先停止 Alfred 再選擇" in output
  assert fileBytes(clone) == before and backups(clone) == []


def test_takeLocalRefusesHalfDoneWorktree(diverged, capsys):
  _, clone, _, _ = diverged
  (clone / "README.md").write_text("合併到一半\n", encoding="utf-8")
  git(clone, "add", "README.md")
  (clone / ".git" / "MERGE_HEAD").write_text(board_git.runGit(clone, "rev-parse", "HEAD") + "\n", encoding="utf-8")
  before = fileBytes(clone)

  code, output = stateSync(clone, "local", capsys)

  assert code == 1 and "主專案有未解的合併衝突" in output
  assert fileBytes(clone) == before and backups(clone) == []


def test_withoutDivergenceThereIsNothingToChoose(computers, capsys):
  _, clone, _ = computers
  assert initialize(clone, Options(skipTests=True), toolRoot) == 0
  before = fileBytes(clone)

  code, output = stateSync(clone, "local", capsys)

  assert code == 1 and "沒有分歧，不需要選擇" in output
  assert fileBytes(clone) == before and backups(clone) == []


def corruptCheckpoint(clone):
  checkpointPath(clone).write_text("{壞掉", encoding="utf-8")


def unknownSchema(clone):
  snapshot = json.loads(progressPath(clone).read_text(encoding="utf-8"))
  snapshot["version"] = 99
  progressPath(clone).write_text(json.dumps(snapshot), encoding="utf-8")


def otherProject(clone):
  # 內容和版本鏈都合格，只是屬於另一個專案
  snapshot = readSnapshot(clone)
  snapshot["projectId"] = newProjectId()
  snapshot["revision"] = revisionOf(snapshot)
  writeSnapshot(clone, snapshot)


@pytest.mark.parametrize("damage", [corruptCheckpoint, unknownSchema, otherProject])
@pytest.mark.parametrize("side", ["incoming", "local"])
def test_brokenOrForeignHandoffCannotBeForced(diverged, capsys, damage, side):
  _, clone, _, _ = diverged
  damage(clone)
  before = fileBytes(clone)
  assert board_api.stateView(clone)["sync"]["choosable"] is False

  code, output = stateSync(clone, side, capsys)

  assert code == 1 and "本機進度與交接檔都沒有更動" in output
  assert fileBytes(clone) == before and backups(clone) == []


def test_choosableOnlyForReadableSameProjectProgress():
  base = {"base": {"projectId": "a"}}
  assert isChoosable("unrelated", None, {"projectId": "a"}) is True
  assert isChoosable("older", base, {"projectId": "a"}) is True
  assert isChoosable("unrelated", base, {"projectId": "b"}) is False
  assert isChoosable("error", base, {"projectId": "a"}) is False


def test_choosingInsideLinkedWorktreeIsRefused(diverged, capsys):
  _, clone, _, _ = diverged
  linked = clone.parent / "連結的 worktree"
  git(clone, "worktree", "add", "-q", "-b", "暫用", str(linked))
  before = fileBytes(clone)

  code, output = stateSync(linked, "local", capsys)

  assert code == 1 and "只能在主專案執行" in output
  assert fileBytes(clone) == before and backups(clone) == []


def test_agentPipeCannotChoose(diverged):
  _, clone, _, _ = diverged
  before = fileBytes(clone)
  completed = subprocess.run([sys.executable, str(toolDir / "board.py"), "--project", str(clone), "state-sync",
                              "--take", "local"], capture_output=True, text=True, encoding="utf-8",
                             stdin=subprocess.PIPE)
  assert completed.returncode == 2
  assert "「state-sync」只能由使用者在終端機執行" in completed.stdout
  assert fileBytes(clone) == before and backups(clone) == []


def test_userTerminalRunsChoiceThroughMain(diverged, capsys, monkeypatch):
  _, clone, _, incoming = diverged
  monkeypatch.setattr(board, "isHuman", lambda: True)
  assert board.main(["--project", str(clone), "state-sync", "--take", "incoming"]) == 0
  assert readCheckpoint(clone)["base"]["revision"] == incoming["revision"]


def test_takeOptionIsRequired(monkeypatch, tmp_path):
  monkeypatch.setattr(board, "isHuman", lambda: True)
  with pytest.raises(SystemExit):
    board.main(["--project", str(tmp_path), "state-sync"])


def test_revisionLineShowsBothSides():
  assert board.revisionLine({"baseRevision": None, "incomingRevision": None}) is None
  assert board.revisionLine({"baseRevision": "a" * 64, "incomingRevision": None}) == \
    f"交接版本｜本機基底 {'a' * 12}｜收到 無"
  assert board.revisionLine({"baseRevision": None, "incomingRevision": "b" * 64}) == \
    f"交接版本｜本機基底 無｜收到 {'b' * 12}"


def test_printSyncLinesShowsHintOnlyWhenChoosable(capsys):
  sync = {"baseRevision": None, "incomingRevision": None, "message": "", "choosable": False, "blockedPlans": {}}
  board.printSyncLines(sync)
  assert capsys.readouterr().out == ""
  board.printSyncLines({**sync, "baseRevision": "c" * 64, "message": "分歧", "choosable": True})
  assert capsys.readouterr().out.splitlines() == [f"交接版本｜本機基底 {'c' * 12}｜收到 無", "交接檔｜分歧",
                                                  board.syncChoiceHint]


def test_dashboardChoiceNeedsTokenAndChoosesOnlyThatProject(diverged, tmp_path, capsys):
  _, clone, _, incoming = diverged
  other = initialized(tmp_path / "另一個專案")
  before = fileBytes(clone)
  with serving(other) as httpServer:
    port = httpServer.server_address[1]
    assert post(port, "/api/projects/open", {"path": str(clone)})[0] == 200
    assert post(port, scoped(clone, "/sync/incoming"), withToken="wrong")[0] == 403
    assert fileBytes(clone) == before
    # 預設專案沒有分歧，選擇不會動到它
    status, refused = post(port, "/api/sync/local")
    assert status == 200 and refused["ok"] is False and "不需要選擇" in refused["message"]
    status, chosen = post(port, scoped(clone, "/sync/incoming"))
  assert status == 200 and chosen["ok"] is True, chosen
  assert "已改接續交接檔的進度" in chosen["message"]
  assert readCheckpoint(clone)["base"]["revision"] == incoming["revision"]
  assert backups(other) == []


def test_uninitializedProjectCannotChoose(tmp_path):
  default, plain = initialized(tmp_path / "專案 A"), tmp_path / "空資料夾"
  plain.mkdir()
  with serving(default) as httpServer:
    port = httpServer.server_address[1]
    post(port, "/api/projects/open", {"path": str(plain)})
    status, refused = post(port, scoped(plain, "/sync/local"))
  assert status == 409 and refused["ok"] is False
  assert list(plain.iterdir()) == []


def test_uninitializedDefaultProjectCannotChoose(tmp_path):
  plain = tmp_path / "空資料夾"
  plain.mkdir()
  with serving(plain) as httpServer:
    status, refused = post(httpServer.server_address[1], "/api/sync/local")
  assert status == 409 and refused["ok"] is False and "不是 git repo" in refused["message"]
  assert not (plain / ".board").exists()


def choosableSync(monkeypatch):
  # 看板只負責顯示，分歧判斷另外驗；這裡直接讓讀到的進度帶著分歧與未匯出的標記
  realLoad = board_api.loadProgress

  def loadDiverged(*args, **kwargs):
    loaded = realLoad(*args, **kwargs)
    loaded["sync"].update(baseRevision="d" * 64, incomingRevision="e" * 64, choosable=True)
    return loaded
  monkeypatch.setattr(board_api, "loadProgress", loadDiverged)
  monkeypatch.setattr(board_api, "unexportedProgress", lambda *args: True)


def test_dashboardShowsRevisionsAndChoiceButtons(dashboard, browserPages, monkeypatch):
  choosableSync(monkeypatch)
  dom = browserPages.renderUntil(dashboard + "#token=" + webToken, domHas('data-sync-take="local"'))
  assert f"交接版本｜本機基底 {'d' * 12}｜收到 {'e' * 12}" in dom
  assert "本機有進度還沒匯出" in dom
  assert '<button class="mini-btn" type="button" data-sync-take="incoming">接收交接檔</button>' in dom
  assert '<button class="mini-btn" type="button" data-sync-take="local">保留本機</button>' in dom


def test_readOnlyDashboardDisablesChoiceButtons(dashboard, browserPages, monkeypatch):
  choosableSync(monkeypatch)
  dom = browserPages.renderUntil(dashboard, domHas('data-sync-take="local"'))
  assert 'data-sync-take="incoming" disabled=""' in dom
  assert 'data-sync-take="local" disabled=""' in dom
