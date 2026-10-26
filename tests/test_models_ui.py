import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="找不到 Node.js")
def test_modelPickerInteractions():
  script = Path(__file__).with_name("models-ui.test.cjs")
  completed = subprocess.run([shutil.which("node"), "--test", str(script)],
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
  assert completed.returncode == 0, completed.stdout + completed.stderr
