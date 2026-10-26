import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import quality  # noqa: E402

PASSING_TEST = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path("src").resolve()))
from app import answer

def test_answer():
    assert answer() == 42
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    # 在同一個行程內呼叫 main()，quality.py 本身的覆蓋率才量得到
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    return tmp_path


def run_check(project, source, test=PASSING_TEST, options=()):
    (project / "src" / "app.py").write_text(source, encoding="utf-8")
    (project / "tests" / "test_app.py").write_text(test, encoding="utf-8")
    code = quality.main(["--source", "src", *options, "--", "tests"])
    report = json.loads(
        (project / ".quality" / "report.json").read_text(encoding="utf-8")
    )
    return code, report


def test_passing_function(project):
    code, report = run_check(project, "def answer():\n    return 42\n")
    assert code == 0, report
    assert report["status"] == "passed"
    assert report["functions"][0]["crap"] == 1


def test_uncalled_function_fails_coverage(project):
    source = (
        "def answer():\n    return 42\n\n"
        "def unused():\n    return 99\n"
    )
    code, report = run_check(project, source)
    assert code == 1
    unused = next(
        row for row in report["functions"] if row["function"] == "unused"
    )
    assert unused["coverage"] == 0
    assert "coverage" in unused["failures"]


def test_failed_tests_cannot_pass_quality(project):
    code, report = run_check(
        project,
        "def answer():\n    return 42\n",
        PASSING_TEST.replace("== 42", "== 0"),
    )
    assert code == 1
    assert report["status"] == "failed"
    assert report["pytest_exit_code"] == 1


def test_source_changes_are_rejected(project):
    test = """
from pathlib import Path

def test_changes_source():
    Path("src/app.py").write_text(
        "def answer():\\n    return 0\\n", encoding="utf-8"
    )
"""
    code, report = run_check(
        project, "def answer():\n    return 42\n", test
    )
    assert code == 2
    assert "changed during tests" in report["reason"]


def test_nested_functions_are_reported_separately(project):
    source = """
def answer():
    def inner():
        return 42
    return inner()
"""
    code, report = run_check(project, source)
    assert code == 0, report
    assert {row["function"] for row in report["functions"]} == {
        "answer", "answer.inner"
    }


def test_same_name_functions_are_scored_by_line(project):
    source = """
class Box:
    def __init__(self):
        self._value = 0

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self._value = value
"""
    test = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path("src").resolve()))
from app import Box

def test_read_value():
    assert Box().value == 0
"""
    code, report = run_check(project, source, test)
    assert code == 1
    coverage = {
        row["line"]: row["coverage"]
        for row in report["functions"] if row["function"] == "Box.value"
    }
    assert coverage == {7: 100, 11: 0}


def test_overload_stubs_are_skipped(project):
    source = """
from typing import overload

@overload
def answer(value: int) -> int: ...
@overload
def answer(value: str) -> str: ...
def answer(value=None):
    return 42
"""
    code, report = run_check(project, source)
    assert code == 0, report
    assert [row["line"] for row in report["functions"]] == [8]
    assert [(row["line"], row["reason"]) for row in report["skipped"]] == [
        (5, "stub"), (7, "stub")
    ]


def test_docstring_only_method_is_skipped(project):
    source = """
from typing import Protocol

class Greeter(Protocol):
    def greet(self) -> str:
        \"\"\"Return a greeting.\"\"\"

def answer():
    return 42
"""
    code, report = run_check(project, source)
    assert code == 0, report
    assert report["skipped"] == [{
        "file": "src/app.py", "function": "Greeter.greet",
        "line": 5, "reason": "stub",
    }]


def test_excluded_function_is_skipped(project, capsys):
    source = (
        "def answer():\n    return 42\n\n"
        "def debug():  # pragma: no cover\n    print('debug')\n"
    )
    code, report = run_check(project, source)
    assert code == 0, report
    assert report["skipped"] == [{
        "file": "src/app.py", "function": "debug",
        "line": 4, "reason": "excluded",
    }]
    assert "SKIP src/app.py:4 debug (excluded)" in capsys.readouterr().out


def test_one_line_functions_count_only_when_called(project):
    source = """
import functools

def answer(): return 42

def unused(): return 0

@functools.cache
def cached(): return 1

@functools.cache
def cached_unused(): return 2
"""
    test = PASSING_TEST + """
from app import cached

def test_cached():
    assert cached() == 1
"""
    code, report = run_check(project, source, test)
    assert code == 1
    coverage = {row["function"]: row["coverage"] for row in report["functions"]}
    assert coverage == {
        "answer": 100, "unused": 0, "cached": 100, "cached_unused": 0
    }


def test_unimported_file_in_plain_directory_is_reported(project):
    (project / "src" / "tools").mkdir()
    (project / "src" / "tools" / "helper.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    code, report = run_check(project, "def answer():\n    return 42\n")
    assert code == 1
    helper = next(
        row for row in report["functions"] if row["function"] == "helper"
    )
    assert helper["file"] == "src/tools/helper.py"
    assert helper["coverage"] == 0


CLI_SOURCE = """
def answer():
    return 42

if __name__ == "__main__":
    print(answer())
"""

CLI_TEST = """
import subprocess
import sys

def test_cli():
    result = subprocess.run(
        [sys.executable, "src/app.py"], capture_output=True, text=True
    )
    assert result.stdout.strip() == "42"
"""


def test_unmeasured_subprocess_is_an_error(project):
    code, report = run_check(project, CLI_SOURCE, CLI_TEST)
    assert code == 2
    assert 'patch = ["subprocess"]' in report["reason"]


def test_subprocess_patch_measures_cli(project):
    (project / "pyproject.toml").write_text(
        '[tool.coverage.run]\npatch = ["subprocess"]\n', encoding="utf-8"
    )
    code, report = run_check(project, CLI_SOURCE, CLI_TEST)
    assert code == 0, report
    assert report["functions"][0]["coverage"] == 100


BRANCH_SOURCE = """
def answer(flag=True):
    if flag:
        return 42
    return 0
"""

BRANCH_TEST = PASSING_TEST + """
def test_flag_off():
    assert answer(False) == 0
"""


@pytest.mark.parametrize("options, failures", [
    (["--max-complexity", "1"], ["complexity"]),
    (["--max-crap", "1.5"], ["crap"]),
])
def test_limits_are_applied(project, options, failures):
    code, report = run_check(project, BRANCH_SOURCE, BRANCH_TEST, options)
    assert code == 1
    assert report["functions"][0]["failures"] == failures


def test_multiple_sources_are_checked(project):
    (project / "lib").mkdir()
    (project / "lib" / "extra.py").write_text(
        "def extra():\n    return 1\n", encoding="utf-8"
    )
    test = PASSING_TEST + """
sys.path.insert(0, str(Path("lib").resolve()))
from extra import extra

def test_extra():
    assert extra() == 1
"""
    code, report = run_check(
        project, "def answer():\n    return 42\n", test,
        ["--source", "lib"],
    )
    assert code == 0, report
    assert {row["file"] for row in report["functions"]} == {
        "src/app.py", "lib/extra.py"
    }


@pytest.mark.parametrize("options", [
    ["--max-crap", "nan"],
    ["--max-crap", "0"],
    ["--min-coverage", "101"],
])
def test_invalid_limits_are_rejected(options):
    with pytest.raises(SystemExit) as exit_info:
        quality.arguments(["--source", "src", *options])
    assert exit_info.value.code == 2


@pytest.mark.parametrize("source, message", [
    ("missing", "Invalid source directory"),
    (".", "not project root"),
])
def test_invalid_sources_are_rejected(project, source, message):
    assert quality.main(["--source", source]) == 2
    report = json.loads(
        (project / ".quality" / "report.json").read_text(encoding="utf-8")
    )
    assert message in report["reason"]


def test_missing_packages_are_reported(project, monkeypatch):
    monkeypatch.setattr(
        quality, "REQUIRED_MODULES", ("pytest", "no_such_package")
    )
    code, report = run_check(project, "def answer():\n    return 42\n")
    assert code == 2
    assert "no_such_package" in report["reason"]
    assert "pytest_exit_code" not in report


def test_report_directory_is_ignored_by_git(project):
    run_check(project, "def answer():\n    return 42\n")
    gitignore = project / ".quality" / ".gitignore"
    assert gitignore.read_text(encoding="utf-8") == "*\n"


def test_unwritable_report_directory(project, capsys):
    (project / ".quality").write_text("", encoding="utf-8")
    assert quality.main(["--source", "src"]) == 2
    assert "Cannot write report" in capsys.readouterr().err
