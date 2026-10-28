"""Prepare a project Python and generate commands for that interpreter."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def quote(argument):
    text = argument.as_posix() if isinstance(argument, Path) else str(argument)
    return '"' + text + '"' if os.name == "nt" else shlex.quote(text)


def run(arguments, **kwargs):
    return subprocess.run(arguments, check=True, encoding="utf-8", errors="replace",
                          env={**os.environ, "PYTHONUTF8": "1"}, **kwargs)


def projectPython(project, requested=None):
    if requested:
        candidate = (project / requested).resolve()
        found = str(candidate) if candidate.is_file() else shutil.which(requested)
        if not found:
            raise ValueError(f"找不到 Python：{requested}")
        return Path(found).resolve()
    executable = project / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.exists():
        print("建立專案 .venv…", flush=True)
        run([sys.executable, "-m", "venv", str(project / ".venv")])
    return executable.resolve()


def prepare(project, requested, requirements):
    executable = projectPython(project, requested)
    probe = ("import importlib.util,json; print(json.dumps([name for name in "
             "('pytest','coverage','radon') if importlib.util.find_spec(name) is None]))")
    missing = json.loads(run([str(executable), "-c", probe], capture_output=True).stdout)
    if missing:
        print("安裝 TDD 套件：" + ", ".join(missing), flush=True)
        run([str(executable), "-m", "pip", "install", "-r", str(requirements)])
        if json.loads(run([str(executable), "-c", probe], capture_output=True).stdout):
            raise ValueError("TDD 套件安裝未完成，請檢查網路與專案 Python 後重試")
    return executable


def qualityCommand(executable, layout):
    arguments = [executable, ".agents/skills/tdd-crap/scripts/quality.py"]
    for source in layout["sources"]:
        arguments.extend(["--source", source])
    return " ".join(map(quote, [*arguments, "--", *layout["tests"]]))


def writerCommand(command):
    # Alfred 使用原生 shell；Windows writer 的 PowerShell 需以 & 呼叫有引號的執行檔。
    return "& " + command if os.name == "nt" else command
