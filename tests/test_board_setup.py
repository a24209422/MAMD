"""Exercise initialization without using the developer's global skill installs."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from board_setup.project import Options, initialize
from conftest import git

toolRoot = Path(__file__).resolve().parents[1]


@pytest.fixture
def pythonProject(gitProject):
    (gitProject / "src").mkdir()
    (gitProject / "tests").mkdir()
    (gitProject / "src" / "calc.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    (gitProject / "tests" / "test_calc.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path('src').resolve()))\nfrom calc import answer\n"
        "def test_answer():\n    assert answer() == 42\n", encoding="utf-8")
    return gitProject


def config(project):
    return json.loads((project / ".board" / "config.json").read_text(encoding="utf-8"))


def test_initialize_installs_both_skills_for_both_agents(pythonProject):
    assert initialize(pythonProject, Options(python=sys.executable), toolRoot) == 0
    for host in (".agents", ".claude"):
        for name in ("agent-board-init", "agent-board-plan", "tdd-crap"):
            assert (pythonProject / host / "skills" / name / "SKILL.md").is_file()
        assert (pythonProject / host / "skills/tdd-crap/scripts/_quality_entries.py").is_file()
    assert config(pythonProject)["tddCrap"] is True
    assert "D:\\MAMD-stable" not in (pythonProject / "AGENTS.md").read_text(encoding="utf-8")
    assert (pythonProject / ".agent-board/runtime/board.py").is_file()
    assert (pythonProject / ".agent-board/runtime/web/index.html").is_file()
    assert not (pythonProject / ".agent-board/runtime/.git").exists()


def test_reinitialize_preserves_rules_roles_and_is_idempotent(pythonProject):
    (pythonProject / "AGENTS.md").write_text("# Project rules\nKeep my rules.\n", encoding="utf-8")
    (pythonProject / ".board").mkdir()
    roles = {"writer": {"cli": "codex", "model": "model-x"}, "maxParallel": 1}
    (pythonProject / ".board/config.json").write_text(json.dumps(roles), encoding="utf-8")
    options = Options(python=sys.executable)
    assert initialize(pythonProject, options, toolRoot) == 0
    before = (pythonProject / "AGENTS.md").read_bytes()
    beforeConfig = config(pythonProject)
    assert initialize(pythonProject, options, toolRoot) == 0
    assert (pythonProject / "AGENTS.md").read_bytes() == before
    assert b"Keep my rules." in before
    assert config(pythonProject) == beforeConfig
    assert config(pythonProject)["writer"] == roles["writer"]
    assert config(pythonProject)["maxParallel"] == 1


def test_custom_skill_conflict_does_not_partially_initialize(pythonProject):
    custom = pythonProject / ".agents/skills/tdd-crap/SKILL.md"
    custom.parent.mkdir(parents=True)
    custom.write_text("My own skill", encoding="utf-8")
    assert initialize(pythonProject, Options(python=sys.executable), toolRoot) == 1
    assert custom.read_text(encoding="utf-8") == "My own skill"
    assert not (pythonProject / "AGENTS.md").exists()
    assert not (pythonProject / ".claude").exists()
    assert not (pythonProject / ".agent-board").exists()


@pytest.mark.parametrize("host", [".agents", ".claude"])
def test_cloned_project_rebuilds_config_and_runs_quality(pythonProject, tmp_path, host):
    assert initialize(pythonProject, Options(python=sys.executable), toolRoot) == 0
    git(pythonProject, "add", ".")
    git(pythonProject, "commit", "-qm", "Initialize skills")
    clone = tmp_path / ("clone with spaces " + host[1:])
    git(tmp_path, "clone", "-q", str(pythonProject), str(clone))
    assert not (clone / ".board").exists()
    entry = clone / host / "skills/agent-board-init/scripts/init.py"
    result = subprocess.run([sys.executable, str(entry), "--python", sys.executable], cwd=clone,
                            capture_output=True, text=True, encoding="utf-8", timeout=60,
                            env={**os.environ, "USERPROFILE": str(tmp_path / "new-user")})
    assert result.returncode == 0, result.stdout + result.stderr
    command = config(clone)["testCommand"]
    assert str(pythonProject) not in command
    tested = subprocess.run(command, shell=True, cwd=clone, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
    assert tested.returncode == 0, tested.stdout + tested.stderr
    report = json.loads((clone / ".quality/report.json").read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert report["functions"][0]["function"] == "answer"


def test_flat_project_and_root_tests_can_run_quality(gitProject):
    (gitProject / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    (gitProject / "test_app.py").write_text(
        "from app import answer\ndef test_answer():\n    assert answer() == 42\n", encoding="utf-8")
    assert initialize(gitProject, Options(python=sys.executable), toolRoot) == 0
    tested = subprocess.run(config(gitProject)["testCommand"], shell=True, cwd=gitProject,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert tested.returncode == 0, tested.stdout + tested.stderr
    report = json.loads((gitProject / ".quality/report.json").read_text(encoding="utf-8"))
    assert list(report["source_sha256"]) == ["app.py"]


def test_explicit_test_modes_survive_clone_and_repeat(gitProject):
    options = Options(testCommand="npm test")
    assert initialize(gitProject, options, toolRoot) == 0
    assert initialize(gitProject, toolRoot=toolRoot) == 0
    assert config(gitProject)["testCommand"] == "npm test"
    assert not config(gitProject)["tddCrap"]
    assert initialize(gitProject, Options(skipTests=True), toolRoot) == 0
    assert initialize(gitProject, toolRoot=toolRoot) == 0
    assert config(gitProject)["skipTests"] is True


def test_existing_testing_choice_needs_explicit_migration(pythonProject):
    (pythonProject / ".board").mkdir()
    original = {"testCommand": "python -m pytest", "reviewer": {"cli": "claude"}}
    path = pythonProject / ".board/config.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    assert initialize(pythonProject, Options(python=sys.executable), toolRoot) == 1
    assert config(pythonProject) == original
    assert not (pythonProject / ".agents").exists()
    assert initialize(pythonProject, Options(python=sys.executable, tdd=True), toolRoot) == 0
    assert config(pythonProject)["reviewer"] == original["reviewer"]


def test_writer_uses_project_skill_and_python(writePlan):
    from board_prompts import buildWritePrompt
    from rtm_graph import analyzePlan
    plan = analyzePlan(writePlan("0001-demo.md", [
        "| R1 做加法 | 加兩數 | D1 新增函式 | - | T1 寫加法 | | 驗證結果 |"], planId="P0001"))
    prompt = buildWritePrompt(plan, "T1", 1, qualityCommand="quality-command", cli="codex",
                              tddSkill=".agents/skills/tdd-crap", pythonCommand='"project-python"')
    assert ".agents" in prompt and "SKILL.md" in prompt
    assert str(Path.home() / ".claude/skills/tdd-crap/SKILL.md") not in prompt
    claude = buildWritePrompt(plan, "T1", 1, qualityCommand="quality-command", cli="claude",
                              tddSkill=".agents/skills/tdd-crap", pythonCommand='"project-python"')
    assert '"project-python" -m pytest' in claude
    assert str(Path(".agents/skills/tdd-crap/SKILL.md")) in claude


def test_claude_writer_can_run_selected_project_python():
    from conductor import Conductor
    conductor = object.__new__(Conductor)
    conductor.config = {"tddCrap": True, "testCommand": "quality-command", "pythonCommand": '"project-python"'}
    tools = conductor.claudeAllowedTools({"allowedTools": []})
    assert 'PowerShell("project-python" -m pytest*)' in tools


def test_manual_init_and_plan_policies_are_installed(pythonProject):
    assert initialize(pythonProject, Options(python=sys.executable), toolRoot) == 0
    for name in ("agent-board-init", "agent-board-plan"):
        policy = pythonProject / ".agents/skills" / name / "agents/openai.yaml"
        assert "allow_implicit_invocation: false" in policy.read_text(encoding="utf-8")
        claude = pythonProject / ".claude/skills" / name / "SKILL.md"
        assert "disable-model-invocation: true" in claude.read_text(encoding="utf-8")
        codex = pythonProject / ".agents/skills" / name / "SKILL.md"
        assert "disable-model-invocation" not in codex.read_text(encoding="utf-8")


@pytest.mark.parametrize("relative,content", [
    (".board/config.json", "[]"), (".board/config.json", "{broken"),
    (".agent-board/setup.json", '{"managedBy":"other"}'),
    ("AGENTS.md", "<!-- agent-board:start -->\nMissing end"),
    (".agent-board/runtime/board.py", "not managed"),
])
def test_invalid_existing_files_are_preserved(pythonProject, relative, content):
    path = pythonProject / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    assert initialize(pythonProject, Options(python=sys.executable), toolRoot) == 1
    assert path.read_text(encoding="utf-8") == content
    assert not (pythonProject / ".claude").exists()


@pytest.mark.parametrize("options", [
    Options(sources=["../"], python=sys.executable),
    Options(sources=["."], python=sys.executable),
    Options(sources=["README.md"], python=sys.executable),
    Options(tests="missing", python=sys.executable),
    Options(python="not-a-python-binary"),
    Options(testCommand=" "), Options(tdd=True, skipTests=True),
])
def test_invalid_options_do_not_install_skills(pythonProject, options):
    assert initialize(pythonProject, options, toolRoot) == 1
    assert not (pythonProject / ".agents").exists()


def test_layout_reads_pytest_metadata_and_excludes_package_tests(gitProject):
    from board_setup import layout
    package = gitProject / "app"
    (package / "tests").mkdir(parents=True)
    for path in (package / "__init__.py", package / "logic.py", package / "tests/test_logic.py"):
        path.write_text("# example\n", encoding="utf-8")
    (gitProject / "checks").mkdir()
    (gitProject / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["checks"]\n', encoding="utf-8")
    assert layout.testLocations(gitProject) == ["checks"]
    assert layout.sourceLocations(gitProject) == ["app/__init__.py", "app/logic.py"]
    (gitProject / "pytest.ini").write_text('[pytest]\ntestpaths = checks app/tests\n', encoding="utf-8")
    assert layout.testLocations(gitProject) == ["checks", "app/tests"]
    assert initialize(gitProject, Options(python=sys.executable), toolRoot) == 0
    assert '"app/tests"' in config(gitProject)["testCommand"] or "app/tests" in config(gitProject)["testCommand"]


def test_missing_layout_and_non_git_project_are_reported(gitProject, tmp_path):
    assert initialize(gitProject, Options(python=sys.executable), toolRoot) == 1
    assert initialize(tmp_path, Options(python=sys.executable), toolRoot) == 1
    assert initialize(tmp_path / "missing", toolRoot=toolRoot) == 1


def test_environment_creates_venv_and_installs_only_when_missing(tmp_path, monkeypatch):
    from board_setup import environment
    commands = []
    replies = iter(['["radon"]', "", "[]"])

    def fake_run(arguments, **kwargs):
        commands.append(arguments)
        if "venv" in arguments:
            executable = tmp_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            executable.parent.mkdir(parents=True)
            executable.touch()
            return subprocess.CompletedProcess(arguments, 0, "")
        return subprocess.CompletedProcess(arguments, 0, next(replies))

    monkeypatch.setattr(environment, "run", fake_run)
    python = environment.prepare(tmp_path, None, toolRoot / "skills/tdd-crap/requirements.txt")
    assert python.is_file()
    assert commands[0][1:3] == ["-m", "venv"]
    assert commands[2][1:5] == ["-m", "pip", "install", "-r"]
    assert environment.projectPython(tmp_path) == python


def test_dependency_install_failure_does_not_claim_success(pythonProject, monkeypatch, capsys):
    from board_setup import environment

    def unavailable(*args):
        raise subprocess.CalledProcessError(1, "pip")

    monkeypatch.setattr(environment, "prepare", unavailable)
    assert initialize(pythonProject, toolRoot=toolRoot) == 1
    assert "初始化未完成" in capsys.readouterr().out
    assert not (pythonProject / ".agent-board").exists()


def test_install_still_missing_is_an_error(tmp_path, monkeypatch):
    from board_setup import environment
    monkeypatch.setattr(environment, "projectPython", lambda *args: Path(sys.executable))
    monkeypatch.setattr(environment, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 0, '["radon"]'))
    with pytest.raises(ValueError, match="安裝未完成"):
        environment.prepare(tmp_path, None, tmp_path / "requirements.txt")


def test_shell_quoting_and_python_lookup(tmp_path, monkeypatch):
    from board_setup import environment
    assert environment.projectPython(tmp_path, sys.executable) == Path(sys.executable).resolve()
    monkeypatch.setattr(environment.shutil, "which", lambda name: sys.executable)
    assert environment.projectPython(tmp_path, "python") == Path(sys.executable).resolve()
    expected = '"path with spaces"' if os.name == "nt" else "'path with spaces'"
    assert environment.quote("path with spaces") == expected


def test_saved_non_tdd_setup_rebuilds_local_config(gitProject):
    assert initialize(gitProject, Options(skipTests=True), toolRoot) == 0
    path = gitProject / ".board/config.json"
    path.unlink()
    assert initialize(gitProject, toolRoot=toolRoot) == 0
    assert config(gitProject)["skipTests"]


def test_only_runtime_assets_are_bundled():
    from board_setup.assets import runtimeFiles
    files = runtimeFiles(toolRoot)
    assert Path("board_setup/project.py") in files
    assert Path("skills/tdd-crap/scripts/quality.py") in files
    assert Path("requirements-terminal.txt") in files
    assert Path("modules/terminal/service.py") in files
    assert Path("web/vendor/xterm.js") in files
    assert Path("web/js/terminal.js") in files
    assert not any("tests" in path.parts or "__pycache__" in path.parts for path in files)


def test_cli_initializes_a_project(pythonProject):
    from board_setup.cli import main
    assert main(["--project", str(pythonProject), "--python", sys.executable]) == 0
    assert config(pythonProject)["tddCrap"]
    with pytest.raises(SystemExit) as error:
        main(["--tdd", "--skip-tests"])
    assert error.value.code == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell command contract")
def test_powershell_can_run_generated_quality_command(pythonProject):
    assert initialize(pythonProject, Options(python=sys.executable), toolRoot) == 0
    settings = config(pythonProject)
    command = settings.get("writerTestCommand", settings["testCommand"])
    tested = subprocess.run([shutil.which("powershell"), "-NoProfile", "-NonInteractive", "-Command", command],
                            cwd=pythonProject, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
    assert tested.returncode == 0, tested.stdout + tested.stderr

