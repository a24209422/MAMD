"""Function entry must be independent of layout and module execution."""

import pytest

from test_quality import PASSING_TEST, project, run_check


@pytest.mark.parametrize("first_line", [True, False], ids=["first", "later"])
@pytest.mark.parametrize("one_line", [True, False], ids=["single", "multi"])
@pytest.mark.parametrize("called", [True, False], ids=["called", "uncalled"])
def test_entry_layout_matrix(project, first_line, one_line, called):
    source = "def answer(): return 42\n" if one_line else "def answer():\n    return 42\n"
    if not first_line:
        source = "\n" + source
    test = PASSING_TEST if called else PASSING_TEST.replace(
        "assert answer() == 42", "assert callable(answer)"
    )
    code, report = run_check(project, source, test)
    row, = report["functions"]
    assert code == (0 if called else 1), report
    assert row["line"] == (1 if first_line else 2)
    assert row["coverage"] == (100 if called else 0)
    assert row["covered_statements"] == (row["statements"] if called else 0)
    assert row["failures"] == ([] if called else ["coverage"])


@pytest.mark.parametrize("called", [True, False], ids=["called", "uncalled"])
def test_first_line_lambda_decorator_is_not_function_entry(project, called):
    source = "@(lambda function: function)\ndef answer(): return 42\n"
    test = PASSING_TEST if called else PASSING_TEST.replace(
        "assert answer() == 42", "assert callable(answer)"
    )
    code, report = run_check(project, source, test)
    assert code == (0 if called else 1), report
    assert report["functions"][0]["coverage"] == (100 if called else 0)


@pytest.mark.parametrize("called", [True, False], ids=["called", "uncalled"])
def test_first_line_entry_in_subprocess(project, called):
    (project / "pyproject.toml").write_text(
        '[tool.coverage.run]\npatch = ["subprocess"]\n', encoding="utf-8"
    )
    child = "from app import answer; " + (
        "assert answer() == 42" if called else "assert callable(answer)"
    )
    test = (
        "import os, subprocess, sys\nfrom pathlib import Path\n"
        "def test_child():\n"
        "    env = dict(os.environ, PYTHONPATH=str(Path('src').resolve()) + os.pathsep + os.environ.get('PYTHONPATH', ''))\n"
        f"    subprocess.run([sys.executable, '-c', {child!r}], env=env, check=True)\n"
    )
    code, report = run_check(project, "def answer(): return 42\n", test)
    assert code == (0 if called else 1), report
    assert report["functions"][0]["coverage"] == (100 if called else 0)


def test_first_line_entry_in_thread(project):
    test = PASSING_TEST.replace(
        "assert answer() == 42",
        "from concurrent.futures import ThreadPoolExecutor\n"
        "    with ThreadPoolExecutor(max_workers=1) as pool:\n"
        "        assert pool.submit(answer).result() == 42",
    )
    code, report = run_check(project, "def answer(): return 42\n", test)
    assert code == 0, report
    assert report["functions"][0]["coverage"] == 100


def test_unmeasured_child_call_cannot_reuse_module_import_coverage(project):
    test = PASSING_TEST.replace(
        "assert answer() == 42",
        "import subprocess\n"
        "    subprocess.run([sys.executable, '-c', \"import sys; sys.path.insert(0, 'src'); from app import answer; assert answer() == 42\"], check=True)",
    )
    code, report = run_check(project, "def answer(): return 42\n", test)
    assert code == 1, report
    assert report["functions"][0]["coverage"] == 0
