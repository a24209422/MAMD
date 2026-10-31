"""server 已開啟的專案清單：每個專案資料夾對應一份自己的 Dashboard，只存在 server 記憶體。"""
import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from board import repoProblems
from board_core import BoardError, configPath, configProblems, readUserConfig

pathHint = "請輸入專案資料夾的完整路徑，例如 D:\\projects\\demo"


@dataclass(frozen=True)
class Project:
  id: str
  name: str
  path: Path
  dashboard: object

  def view(self):
    return {"id": self.id, "name": self.name, "path": str(self.path)}


def projectId(path):
  return hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()


def resolveProjectPath(pathText):
  # 相對路徑會依 server 啟動位置解讀，使用者看不出實際開了哪裡，所以只收完整路徑
  if not isinstance(pathText, str) or "\0" in pathText or not Path(pathText).is_absolute():
    raise BoardError(pathHint)
  # realpath 把 ..、斜線方向、捷徑連結都換成同一個實際位置，等價路徑才會得到同一個 id
  path = Path(os.path.realpath(pathText))
  if not path.exists():
    raise BoardError(f"找不到專案資料夾：{path}")
  if not path.is_dir():
    raise BoardError(f"這個路徑不是資料夾：{path}")
  try:
    with os.scandir(path) as entries:
      next(entries, None)
  except OSError as error:
    raise BoardError(f"沒有權限讀取這個資料夾：{path}（{error.strerror or error}）") from error
  return path


def readiness(path):
  # 每次都重新檢查：使用者可能剛在終端機裡 init 或修好設定，不必重新開啟專案
  problems = repoProblems(path)
  if not configPath(path).is_file():
    return {"ready": False, "problems": problems + [".board/config.json 不存在：在這個專案執行 board.py init 完成初始化"]}
  try:
    problems += [f".board/config.json：{problem}" for problem in configProblems(readUserConfig(path), requireTestChoice=True)]
  except (OSError, ValueError) as error:
    problems.append(f".board/config.json 讀不懂，請先手動修正：{error}")
  return {"ready": not problems, "problems": problems}


class ProjectRegistry:
  def __init__(self, defaultPath, makeDashboard):
    self.makeDashboard = makeDashboard
    self.projects = {}
    self.lock = threading.Lock()
    self.defaultId = self.open(str(defaultPath)).id

  def open(self, pathText):
    path = resolveProjectPath(pathText)
    identifier = projectId(path)
    with self.lock:
      if identifier not in self.projects:
        # Dashboard 建好才放進清單：建立失敗時不會留下沒有服務的專案
        # 磁碟根目錄沒有資料夾名稱，改用路徑當名稱
        self.projects[identifier] = Project(identifier, path.name or str(path), path, self.makeDashboard(path))
      return self.projects[identifier]

  def get(self, identifier):
    return self.projects.get(identifier)

  def all(self):
    with self.lock:
      return list(self.projects.values())

  def list(self):
    return [project.view() for project in self.all()]
