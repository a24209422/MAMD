"""Build portable, managed files without touching project configuration."""
import json
from pathlib import Path

import board

skillNames = ("agent-board-init", "agent-board-plan", "tdd-crap")
hosts = {".agents": "codex", ".claude": "claude"}
markerName = ".agent-board-skill"
manualPolicy = b"policy:\n  allow_implicit_invocation: false\n"


def jsonBytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def readObject(path):
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} 必須是 JSON 物件")
    return value


def resourceFiles(root):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and not set(path.relative_to(root).parts) & {"tests", "__pycache__", ".quality"}}


def runtimeFiles(toolRoot):
    names = [*toolRoot.glob("board*.py"), toolRoot / "conductor.py", toolRoot / "rtm_graph.py",
             toolRoot / "server.py", toolRoot / "install_skill.py", toolRoot / "LICENSE"]
    result = {Path(path.name): path.read_bytes() for path in names}
    result[Path("docs/AGENTS-snippet.md")] = (toolRoot / "docs/AGENTS-snippet.md").read_bytes()
    for name in ("board_setup", "web", "modules", "skills/agent-board-init", "skills/tdd-crap"):
        result.update({Path(name) / path: content for path, content in resourceFiles(toolRoot / name).items()})
    return result


def skillFiles(toolRoot, boardCommand):
    result = {}
    for host, cli in hosts.items():
        for name in skillNames:
            base = Path(host) / "skills" / name
            files = resourceFiles(toolRoot / "skills" / name) if name != "agent-board-plan" else {}
            if name == "agent-board-plan":
                files[Path("SKILL.md")] = board.planSkillText(cli, boardCommand).encode("utf-8")
            if cli == "codex" and name == "agent-board-init":
                files[Path("SKILL.md")] = files[Path("SKILL.md")].replace(b"disable-model-invocation: true\n", b"")
            if name != "tdd-crap":
                files[Path("agents/openai.yaml")] = manualPolicy
            files[Path(markerName)] = b"Managed by agent-board-init.\n"
            result.update({base / path: content for path, content in files.items()})
    return result


def managedFiles(project, toolRoot, settings):
    runtime = "." if project == toolRoot else ".agent-board/runtime"
    command = f'"{runtime}/board.py"'
    result = skillFiles(toolRoot, command)
    if project != toolRoot:
        result.update({Path(runtime) / path: content for path, content in runtimeFiles(toolRoot).items()})
    settings = {**settings, "managedBy": "agent-board", "version": 1, "runtime": runtime}
    result[Path(".agent-board/setup.json")] = jsonBytes(settings)
    agents = board.readKeepingNewlines(project / "AGENTS.md")
    result[Path("AGENTS.md")] = board.updatedAgents(agents, command).encode("utf-8")
    ignore = board.readKeepingNewlines(project / ".gitignore")
    ignore, _ = board.updatedGitignore(ignore)
    eol = board.lineEnding(ignore)
    for entry in (".venv/", "__pycache__/", ".pytest_cache/", ".quality/"):
        if entry.strip("/") not in {line.strip().strip("/") for line in ignore.splitlines()}:
            ignore = board.withTrailingNewline(ignore, eol) + entry + eol
    result[Path(".gitignore")] = ignore.encode("utf-8")
    return result


def skillConflicts(project):
    problems = []
    for host in hosts:
        for name in skillNames:
            directory = project / host / "skills" / name
            if not directory.exists() or (directory / markerName).is_file():
                continue
            content = board.readKeepingNewlines(directory / "SKILL.md")
            managed = board.planSkillMarker in content if name == "agent-board-plan" else "<!-- agent-board:init-skill -->" in content
            if not managed:
                problems.append(f"{directory} 是自訂 skill，不覆蓋；請保留或改名後再初始化")
    return problems


def applyFiles(project, files):
    for relative, content in files.items():
        path = project / relative
        if not path.resolve().is_relative_to(project):
            raise ValueError(f"設定位置超出專案：{path}")
        if path.is_file() and path.read_bytes() == content:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
