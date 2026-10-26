import os
import subprocess
import sys
from pathlib import Path

import install_skill

toolDir = Path(__file__).resolve().parent.parent


def test_repoBundlesTddCrap():
  skillDir = toolDir / "skills" / "tdd-crap"
  assert (skillDir / "SKILL.md").is_file() and (skillDir / "scripts" / "quality.py").is_file()
  assert install_skill.sourceDir == skillDir


def test_installCopiesSkillAndMarker(tmp_path, capsys):
  target = tmp_path / "skills" / "tdd-crap"
  assert install_skill.install(install_skill.sourceDir, target) == 0
  assert (target / "SKILL.md").read_bytes() == (install_skill.sourceDir / "SKILL.md").read_bytes()
  assert (target / "scripts" / "quality.py").is_file()
  assert (target / install_skill.markerName).is_file()
  assert "已安裝" in capsys.readouterr().out


def test_reinstallReplacesOwnCopy(tmp_path, capsys):
  target = tmp_path / "tdd-crap"
  install_skill.install(install_skill.sourceDir, target)
  (target / "old.txt").write_text("舊檔", encoding="utf-8")
  capsys.readouterr()
  assert install_skill.install(install_skill.sourceDir, target) == 0
  assert not (target / "old.txt").exists()
  assert "已更新" in capsys.readouterr().out


def test_existingSkillWithoutMarkerIsKept(tmp_path, capsys):
  # 沒有標記檔代表是使用者自己放的同名 skill，蓋掉就找不回來
  target = tmp_path / "tdd-crap"
  target.mkdir()
  (target / "SKILL.md").write_text("使用者自己的", encoding="utf-8")
  assert install_skill.install(install_skill.sourceDir, target) == 1
  assert (target / "SKILL.md").read_text(encoding="utf-8") == "使用者自己的"
  assert "不覆蓋" in capsys.readouterr().out


def test_commandLineInstallsIntoHome(tmp_path):
  # Windows 的 Path.home() 看 USERPROFILE，其他平台看 HOME
  env = dict(os.environ, USERPROFILE=str(tmp_path), HOME=str(tmp_path))
  completed = subprocess.run([sys.executable, str(toolDir / "install_skill.py")], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", env=env, timeout=60)
  assert completed.returncode == 0, completed.stdout + completed.stderr
  assert (tmp_path / ".claude" / "skills" / "tdd-crap" / "SKILL.md").is_file()
