"""The deterministic helper used by either agent's initialization skill."""
import argparse
import sys

from .project import Options, initialize


def main(argv=None):
    if not sys.stdout.isatty() and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="一次設定 agent-board-plan 與 tdd-crap，預設啟用 Python TDD")
    parser.add_argument("--project", default=".")
    parser.add_argument("--source", action="append")
    parser.add_argument("--tests")
    parser.add_argument("--python", help="指定專案 Python；未指定時使用或建立專案 .venv")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--tdd", action="store_true", help="明確將既有測試設定改用 TDD")
    mode.add_argument("--test-command")
    mode.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args(argv)
    options = Options(args.source, args.tests, args.python, args.test_command, args.skip_tests, args.tdd)
    return initialize(args.project, options)
