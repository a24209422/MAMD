"""Measurement evidence, profiler ownership, and runner cleanup contracts."""

import json
from pathlib import Path
from types import SimpleNamespace

import coverage
import pytest

from test_quality import quality
import _quality_entries as entries


def frame(name="answer", line=1, filename="src/app.py"):
    return SimpleNamespace(f_code=SimpleNamespace(
        co_name=name, co_firstlineno=line, co_filename=filename,
    ))


def test_recorder_only_saves_active_first_line_function_calls(tmp_path, monkeypatch):
    recorder = entries.EntryRecorder()
    recorder.directory = tmp_path
    monkeypatch.setattr(entries.Coverage, "current", lambda: None)
    recorder.record(frame(), "call", None)
    monkeypatch.setattr(entries.Coverage, "current", lambda: object())
    recorder.record(frame(), "return", None)
    recorder.record(frame(line=2), "call", None)
    recorder.record(frame(name="<module>"), "call", None)
    assert recorder.seen == set()
    recorder.record(frame(), "call", None)
    recorder.record(frame(), "call", None)
    rows = recorder.output_path().read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    filename, name = json.loads(rows[0])
    assert Path(filename).resolve() == Path("src/app.py").resolve()
    assert name == "answer"


@pytest.mark.parametrize("existing", ["main", "thread"])
def test_existing_profilers_are_not_overwritten(monkeypatch, existing):
    monkeypatch.setattr(entries.sys, "getprofile", lambda: object() if existing == "main" else None)
    monkeypatch.setattr(entries.threading, "getprofile", lambda: object() if existing == "thread" else None)
    recorder = entries.EntryRecorder()
    with pytest.raises(ValueError, match="existing profiler"):
        recorder.configure(None)
    assert recorder.directory is None


def test_configurer_installs_hooks_once_and_creates_empty_evidence(tmp_path, monkeypatch):
    callbacks = []
    monkeypatch.setenv("QUALITY_ENTRIES_DIR", str(tmp_path))
    monkeypatch.setattr(entries.sys, "getprofile", lambda: None)
    monkeypatch.setattr(entries.threading, "getprofile", lambda: None)
    monkeypatch.setattr(entries.sys, "setprofile", callbacks.append)
    monkeypatch.setattr(entries.threading, "setprofile", callbacks.append)
    recorder = entries.EntryRecorder()
    recorder.configure(None)
    recorder.configure(None)
    assert callbacks == [recorder.record, recorder.record]
    assert recorder.output_path().read_text(encoding="utf-8") == ""
    assert quality.read_entries(tmp_path) == set()


def test_coverage_plugin_registers_shared_recorder():
    registered = []
    entries.coverage_init(SimpleNamespace(add_configurer=registered.append), {})
    assert registered == [entries._recorder]


def test_missing_entry_evidence_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="No function-entry data"):
        quality.read_entries(tmp_path)


def test_entry_evidence_combines_processes_and_deduplicates(tmp_path):
    (tmp_path / "entries-1.jsonl").write_text(
        '["a.py", "answer"]\n["a.py", "answer"]\n', encoding="utf-8"
    )
    (tmp_path / "entries-2.jsonl").write_text(
        '["b.py", "other"]\n', encoding="utf-8"
    )
    assert quality.read_entries(tmp_path) == {("a.py", "answer"), ("b.py", "other")}


@pytest.mark.parametrize("outcome", [0, 1, "exception"])
def test_runner_preserves_plugins_and_always_saves(monkeypatch, outcome):
    events = []
    options = {}
    def make_coverage(**kwargs):
        options.update(kwargs)
        return SimpleNamespace(
            get_option=lambda key: ["project_plugin"],
            set_option=lambda key, value: options.update({key: value}),
            start=lambda: events.append("start"),
            stop=lambda: events.append("stop"),
            save=lambda: events.append("save"),
        )
    def run_pytest(args):
        assert args == ["tests", "-q"]
        if outcome == "exception":
            raise RuntimeError("pytest crashed")
        return outcome
    monkeypatch.setattr(coverage, "Coverage", make_coverage)
    monkeypatch.setattr(pytest, "main", run_pytest)
    if outcome == "exception":
        with pytest.raises(RuntimeError, match="pytest crashed"):
            quality.measure(["src"], ["tests", "-q"])
    else:
        assert quality.measure(["src"], ["tests", "-q"]) == outcome
    assert events == ["start", "stop", "save"]
    assert options == {
        "branch": True, "data_suffix": True, "source": ["src"],
        "run:plugins": ["project_plugin", "_quality_entries"],
    }
