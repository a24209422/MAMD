"""Set up the skills, runtime and test configuration in a project."""
from dataclasses import dataclass
from pathlib import Path
import subprocess

import board
from board_core import testConfigProblems
from . import assets, environment, layout


@dataclass
class Options:
    sources: list[str] | None = None
    tests: str | None = None
    python: str | None = None
    testCommand: str | None = None
    skipTests: bool = False
    tdd: bool = False


def preflight(project):
    if not project.is_dir():
        raise ValueError(f"找不到專案：{project}")
    subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=project, check=True, capture_output=True)
    problems = assets.skillConflicts(project)
    problem = board.agentsProblem(board.readKeepingNewlines(project / "AGENTS.md"))
    if problem:
        problems.append(problem)
    saved = assets.readObject(project / ".agent-board/setup.json")
    if saved and (saved.get("managedBy") != "agent-board" or saved.get("version") != 1):
        problems.append(".agent-board/setup.json 不是支援的 agent-board 設定，不覆蓋")
    runtime = project / ".agent-board/runtime"
    if runtime.exists() and saved.get("managedBy") != "agent-board":
        problems.append(f"{runtime} 不是本工具管理的目錄，不覆蓋")
    if problems:
        raise ValueError("\n".join(problems))
    return saved, assets.readObject(project / ".board/config.json")


def explicitMode(options):
    if sum((options.tdd, options.skipTests, options.testCommand is not None)) > 1:
        raise ValueError("--tdd、--test-command、--skip-tests 只能選一個")
    if options.tdd:
        return "tdd"
    if options.skipTests:
        return "skip"
    if options.testCommand is not None:
        if not options.testCommand.strip():
            raise ValueError("--test-command 不可空白")
        return "custom"
    return None


def chooseMode(options, saved, current):
    selected = explicitMode(options)
    if selected:
        return selected
    if saved.get("mode") == "custom" and current.get("testCommand") == saved.get("testCommand"):
        return "custom"
    if saved.get("mode") == "skip" and current.get("skipTests"):
        return "skip"
    if current.get("testCommand") and not current.get("tddCrap"):
        raise ValueError("已有一般測試設定；使用 --tdd 明確改用 TDD，或 --test-command 保留原指令")
    if current.get("skipTests"):
        raise ValueError("已有略過測試設定；使用 --tdd 或 --skip-tests 明確選擇")
    return saved.get("mode", "tdd")


def settingsFor(project, options, saved, mode):
    if mode == "tdd":
        return {"mode": mode, **layout.chooseLayout(project, options, saved)}
    if mode == "skip":
        return {"mode": mode, "testCommand": ""}
    command = options.testCommand if options.testCommand is not None else saved.get("testCommand", "")
    if mode == "custom" and not command.strip():
        raise ValueError("請提供 --test-command")
    return {"mode": mode, "testCommand": command}


def configured(project, options, settings, current, toolRoot):
    config = dict(current)
    config.pop("skipTests", None)
    if settings["mode"] == "tdd":
        python = environment.prepare(project, options.python, toolRoot / "skills/tdd-crap/requirements.txt")
        config.update(tddCrap=True, testCommand=environment.qualityCommand(python, settings),
                      pythonCommand=environment.quote(python), tddSkill=".agents/skills/tdd-crap")
        config["writerTestCommand"] = environment.writerCommand(config["testCommand"])
        config["writerPythonCommand"] = environment.writerCommand(config["pythonCommand"])
    else:
        config.update(tddCrap=False, testCommand=settings["testCommand"], skipTests=settings["mode"] == "skip")
        config.pop("pythonCommand", None)
        config.pop("tddSkill", None)
        config.pop("writerTestCommand", None)
        config.pop("writerPythonCommand", None)
    problems = testConfigProblems(config, requireChoice=True)
    if problems:
        raise ValueError("\n".join(problems))
    return config


def initialize(project, options=None, toolRoot=None):
    project = Path(project).resolve()
    options = options or Options()
    toolRoot = Path(toolRoot or Path(__file__).resolve().parents[1]).resolve()
    try:
        saved, current = preflight(project)
        mode = chooseMode(options, saved, current)
        settings = settingsFor(project, options, saved, mode)
        files = assets.managedFiles(project, toolRoot, settings)
        for path in (*files, Path(".board/config.json")):
            if not (project / path).resolve().is_relative_to(project):
                raise ValueError(f"設定位置超出專案：{path}")
        config = configured(project, options, settings, current, toolRoot)
        files[Path(".board/config.json")] = assets.jsonBytes(config)
        assets.applyFiles(project, files)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"初始化未完成：{error}")
        return 1
    print("已設定 agent-board-plan、tdd-crap 與初始化入口（Codex／Claude）。")
    print(f"測試方式：{mode}；設定檔：.board/config.json")
    print("請將 .agents/、.claude/、.agent-board/、AGENTS.md、.gitignore 納入 Git。")
    print("換電腦 clone 後再次呼叫 agent-board-init；若技能未顯示，重新開啟 agent。")
    print("Alfred 若已在執行，請重啟以套用新的測試設定。")
    return 0
