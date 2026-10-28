"""Find the bundled runtime relative to this skill, including after git clone."""
from pathlib import Path
import sys


def main():
    for parent in Path(__file__).resolve().parents:
        for candidate in (parent / ".agent-board/runtime", parent):
            if (candidate / "board_setup/cli.py").is_file():
                sys.path.insert(0, str(candidate))
                from board_setup.cli import main as initialize
                return initialize()
    print("找不到隨專案交付的 agent-board 執行程式；請確認 .agent-board/runtime 已納入 Git。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
