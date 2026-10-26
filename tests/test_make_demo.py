import json
import subprocess
import sys
from pathlib import Path

import board_core

fakesDir = Path(__file__).resolve().parent / "fakes"


def test_makeDemoCreatesSubmittedProject(tmp_path):
  projectDir = tmp_path / "demo"
  completed = subprocess.run([sys.executable, str(fakesDir / "make_demo.py"), str(projectDir)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
  assert completed.returncode == 0, completed.stdout + completed.stderr
  assert [event["type"] for event in board_core.readEvents(projectDir)] == ["planSubmitted"]
  config = json.loads((projectDir / ".board" / "config.json").read_text(encoding="utf-8"))
  assert config["claudeCommand"][-1].endswith("fake_claude.py")
  assert config["codexCommand"][-1].endswith("fake_codex.py")
  assert "FAKE_DELAY" in completed.stdout and "server.py" in completed.stdout
  again = subprocess.run([sys.executable, str(fakesDir / "make_demo.py"), str(projectDir)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
  assert again.returncode == 1 and "已經存在" in again.stdout
