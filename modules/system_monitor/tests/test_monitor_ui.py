"""用 Node 跑系統監控前端的互動測試和語法檢查。找不到 Node 就跳過。"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from modules.system_monitor import api

testsDir = Path(__file__).resolve().parent
nodePath = shutil.which("node")
needsNode = pytest.mark.skipif(nodePath is None, reason="找不到 Node.js，跳過系統監控前端測試")


def runNode(*args):
  return subprocess.run([nodePath, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


@needsNode
def test_monitorScriptHasValidSyntax():
  completed = runNode("--check", str(api.webDir / "monitor.js"))
  assert completed.returncode == 0, completed.stderr


@needsNode
def test_monitorUiInteractions():
  completed = runNode("--test", str(testsDir / "monitor-ui.test.cjs"))
  assert completed.returncode == 0, completed.stdout + completed.stderr


def test_standalonePageLoadsWhitelistedAssets():
  # 單獨開 monitor.html 時用相對路徑載入同一組資產，都要在 server 的白名單裡
  html = (api.webDir / "monitor.html").read_text(encoding="utf-8")
  names = re.findall(r'(?:href|src)="([^"]+)"', html)
  assert names == ["monitor.css", "monitor.js"]
  for name in ["monitor.html", *names]:
    assert api.assetPrefix + name in api.assets
    assert (api.webDir / name).is_file()
