import hashlib
import os
import threading
from pathlib import Path

import pytest

import board_projects
import server
from board_core import BoardError


@pytest.fixture
def folders(tmp_path):
  first, second = tmp_path / "專案 A", tmp_path / "專案 B"
  first.mkdir()
  second.mkdir()
  return first, second


@pytest.fixture
def registry(folders):
  return board_projects.ProjectRegistry(folders[0], lambda path: server.Dashboard(path, "test-token"))


def test_openTwoProjectsGivesSeparateDashboards(registry, folders):
  first, second = registry.get(registry.defaultId), registry.open(str(folders[1]))
  assert first.id != second.id
  assert first.path == folders[0].resolve() and second.path == folders[1].resolve()
  assert (first.name, second.name) == ("專案 A", "專案 B")
  assert first.dashboard.projectDir == folders[0].resolve()
  assert second.dashboard.projectDir == folders[1].resolve()
  for attribute in ("modelCatalog", "systemMonitor", "terminal"):
    assert getattr(first.dashboard, attribute) is not getattr(second.dashboard, attribute)
  assert [project["id"] for project in registry.list()] == [first.id, second.id]
  assert registry.list()[1] == {"id": second.id, "name": "專案 B", "path": str(folders[1].resolve())}
  assert registry.get("unknown") is None


def test_commandsInTwoProjectsDoNotMixOutput(registry, folders):
  first, second = registry.get(registry.defaultId).dashboard, registry.open(str(folders[1])).dashboard
  firstStarted, secondStarted, firstPrinted = threading.Event(), threading.Event(), threading.Event()
  results = {}

  def firstCommand(projectDir):
    print(f"{projectDir.name} 開始")
    firstStarted.set()
    # 沒有共用鎖時，B 的指令已經把 stdout 換成 B 的緩衝區，接下來這行會印進 B
    secondStarted.wait(0.5)
    print(f"{projectDir.name} 結束")
    firstPrinted.set()
    return 0

  def secondCommand(projectDir):
    secondStarted.set()
    firstPrinted.wait(0.5)
    print(f"{projectDir.name} 執行")
    return 0

  def runSecond():
    firstStarted.wait(5)
    results["second"] = second.runCommand(secondCommand)

  thread = threading.Thread(target=runSecond)
  thread.start()
  results["first"] = first.runCommand(firstCommand)
  thread.join(5)
  assert results["first"] == {"ok": True, "message": "專案 A 開始\n專案 A 結束"}
  assert results["second"] == {"ok": True, "message": "專案 B 執行"}
  assert first.writeLock is second.writeLock is server.writeLock


def test_reopenSameOrEquivalentPathReusesProject(folders):
  created = []
  registry = board_projects.ProjectRegistry(folders[0], lambda path: created.append(path) or object())
  original = registry.get(registry.defaultId)
  variants = [str(folders[0]), str(folders[0]) + os.sep, str(folders[0] / ".." / folders[0].name),
              str(folders[0]).replace("\\", "/")]
  if os.name == "nt":
    # Windows 的路徑不分大小寫，換大小寫仍是同一個資料夾
    variants += [str(folders[0]).upper(), str(folders[0]).lower()]
  for variant in variants:
    assert registry.open(variant) is original
  assert len(created) == 1 and len(registry.list()) == 1
  assert original.id == hashlib.sha256(os.path.normcase(str(folders[0].resolve())).encode("utf-8")).hexdigest()


def deniedScandir(path):
  raise PermissionError(13, "Access is denied", str(path))


@pytest.mark.parametrize("pathOf, message", [
  (lambda tmp: str(tmp / "missing"), "找不到專案資料夾"),
  (lambda tmp: str(tmp / "note.txt"), "不是資料夾"),
  (lambda tmp: "專案 A", "完整路徑"),
  (lambda tmp: "", "完整路徑"),
  (lambda tmp: "   ", "完整路徑"),
  (lambda tmp: None, "完整路徑"),
  (lambda tmp: str(tmp / "專案 A") + "\0", "完整路徑"),
], ids=["missing", "file", "relative", "empty", "blank", "notText", "nullChar"])
def test_invalidPathIsRejectedWithoutRegistering(folders, tmp_path, pathOf, message):
  (tmp_path / "note.txt").write_text("not a folder", encoding="utf-8")
  created = []
  registry = board_projects.ProjectRegistry(folders[0], lambda path: created.append(path) or object())
  with pytest.raises(BoardError, match=message):
    registry.open(pathOf(tmp_path))
  assert len(created) == 1 and len(registry.list()) == 1


def test_unreadableFolderIsRejectedWithoutRegistering(folders, monkeypatch):
  created = []
  registry = board_projects.ProjectRegistry(folders[0], lambda path: created.append(path) or object())
  monkeypatch.setattr(board_projects.os, "scandir", deniedScandir)
  with pytest.raises(BoardError, match="沒有權限讀取"):
    registry.open(str(folders[1]))
  assert len(created) == 1 and len(registry.list()) == 1


def test_failedDashboardIsNotRegistered(folders):
  def makeDashboard(path):
    if path.name == "專案 B":
      raise OSError("服務啟動失敗")
    return object()

  registry = board_projects.ProjectRegistry(folders[0], makeDashboard)
  with pytest.raises(OSError):
    registry.open(str(folders[1]))
  assert len(registry.list()) == 1
  assert registry.get(board_projects.projectId(folders[1].resolve())) is None


def test_driveRootUsesPathAsName(folders):
  registry = board_projects.ProjectRegistry(folders[0], lambda path: object())
  root = Path(folders[0].resolve().anchor)
  assert registry.open(str(root)).name == str(root)


def test_invalidDefaultPathRaises(tmp_path):
  with pytest.raises(BoardError, match="找不到專案資料夾"):
    board_projects.ProjectRegistry(tmp_path / "missing", lambda path: object())
