from __future__ import annotations

import argparse
import ast
import gc
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile

REQUIRED_MODULES = ("pytest", "coverage", "radon")
NO_DATA_HINT = (
    "No source lines were executed. If tests run the source in subprocesses "
    '(CLI tests, pytest-xdist), set patch = ["subprocess"] under '
    "[tool.coverage.run]."
)


def arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Run pytest and check per-function CRAP."
    )
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--max-crap", type=float, default=30)
    parser.add_argument("--max-complexity", type=int, default=10)
    parser.add_argument("--min-coverage", type=float, default=80)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    limits = (args.max_crap, args.min_coverage)
    if not all(math.isfinite(value) for value in limits):
        parser.error("Thresholds must be finite.")
    if args.max_crap < 1 or args.max_complexity < 1:
        parser.error("Complexity and CRAP limits must be >= 1.")
    if not 0 <= args.min_coverage <= 100:
        parser.error("--min-coverage must be between 0 and 100.")
    return args


def require_modules():
    missing = [
        name for name in REQUIRED_MODULES
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        raise ValueError(
            f"Missing packages: {', '.join(missing)}. Install "
            f"requirements.txt into the project environment: {sys.executable}"
        )


def snapshot(root, sources):
    files = {}
    for source in sources:
        if not source.is_dir() or not source.is_relative_to(root):
            raise ValueError(f"Invalid source directory: {source}")
        if source == root:
            raise ValueError("Select source subdirectories, not project root.")
        for entry in source.rglob("*.py"):
            path = entry.resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"Source escapes project: {entry}")
            files[path] = path.read_bytes()
    if not files:
        raise ValueError("No Python source files found.")
    return files


def definitions(node, prefix=()):
    for child in ast.iter_child_nodes(node):
        scope = prefix
        if isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            scope = (*prefix, child.name)
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield ".".join(scope), child
        yield from definitions(child, scope)


def first_line(node):
    # 有裝飾器時，函式 code object 的起始行是第一個裝飾器
    return min([node.lineno, *(item.lineno for item in node.decorator_list)])


def body_lines(node):
    lines = set(range(node.body[0].lineno, node.end_lineno + 1))
    # 巢狀函式另外計分，不算進外層函式
    for child in ast.walk(node):
        if child is not node and isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            lines -= set(range(child.body[0].lineno, child.end_lineno + 1))
    return lines


def is_stub(node):
    body = node.body[1:] if ast.get_docstring(node) is not None else node.body
    return all(
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and statement.value.value is Ellipsis
        for statement in body
    )


def skip_reason(node, lines, measured):
    if is_stub(node):
        return "stub"
    if not lines & measured["statements"]:
        span = range(first_line(node), node.end_lineno + 1)
        if measured["excluded"].intersection(span):
            return "excluded"
        return "no statements"
    return None


def was_entered(node, measured):
    # The module also enters at -1: use actual frame entries for line 1.
    if first_line(node) == 1:
        return node.name in measured["first_line_calls"]
    entry = -first_line(node)
    return any(start == entry for start, _ in measured["arcs"])


def function_score(node, statements, executed):
    from radon.complexity import cc_visit_ast
    from radon.visitors import Function

    matches = [
        block for block in cc_visit_ast(node)
        if isinstance(block, Function) and block.lineno == node.lineno
    ]
    if len(matches) != 1:
        raise ValueError(f"Ambiguous complexity at line {node.lineno}.")

    total = len(statements)
    covered = len(executed)
    complexity = matches[0].complexity
    coverage = covered / total
    return {
        "complexity": complexity,
        "covered_statements": covered,
        "statements": total,
        "coverage": coverage * 100,
        "crap": complexity**2 * (1 - coverage)**3 + complexity,
    }


def score_function(node, measured):
    lines = body_lines(node)
    reason = skip_reason(node, lines, measured)
    if reason:
        return {"reason": reason}
    executed = lines & measured["executed"]
    if not was_entered(node, measured):
        executed = set()
    return function_score(node, lines & measured["statements"], executed)


def file_coverage(cov, path, entries):
    filename, statements, excluded, missing, _ = cov.analysis2(str(path))
    return {
        "statements": set(statements),
        "excluded": set(excluded),
        "executed": set(statements) - set(missing),
        "arcs": cov.get_data().arcs(filename) or [],
        "first_line_calls": {
            name for entry_path, name in entries
            if entry_path == os.path.normcase(str(path))
        },
    }


def analyze(root, sources, measured):
    if not any(item["executed"] for item in measured.values()):
        raise ValueError(NO_DATA_HINT)

    rows = []
    skipped = []
    for path, content in sorted(sources.items()):
        for name, node in definitions(ast.parse(content, filename=str(path))):
            result = {
                "file": path.relative_to(root).as_posix(),
                "function": name,
                "line": node.lineno,
                **score_function(node, measured[path]),
            }
            if "reason" in result:
                skipped.append(result)
            else:
                rows.append(result)

    if not rows:
        raise ValueError("No measurable functions found.")
    return rows, skipped


def measure(sources, pytest_args):
    import coverage
    import pytest

    cov = coverage.Coverage(branch=True, data_suffix=True, source=sources)
    plugins = cov.get_option("run:plugins")
    cov.set_option("run:plugins", [*plugins, "_quality_entries"])
    cov.start()
    try:
        return pytest.main(pytest_args)
    finally:
        cov.stop()
        cov.save()


def run_tests(root, sources, pytest_args, temporary):
    scripts = Path(__file__).resolve().parent
    pythonpath = os.pathsep.join(filter(None, [str(scripts), os.environ.get("PYTHONPATH")]))
    env = dict(
        os.environ, COVERAGE_FILE=str(Path(temporary) / ".coverage"),
        QUALITY_ENTRIES_DIR=str(temporary), PYTHONPATH=pythonpath,
    )
    # Preserve project options and propagate the entry plugin via subprocess patch.
    command = [
        sys.executable, str(scripts / "quality.py"), "--measure",
        json.dumps(list(map(str, sources))), *pytest_args,
    ]
    return subprocess.run(command, cwd=root, env=env).returncode


def read_entries(temporary):
    files = sorted(Path(temporary).glob("entries-*.jsonl"))
    if not files:
        raise ValueError("No function-entry data was written.")
    return {
        tuple(json.loads(line))
        for path in files
        for line in path.read_text(encoding="utf-8").splitlines()
    }


def read_coverage(temporary, paths):
    import coverage

    # 不合併到記憶體：coverage 7.10 合併多個檔案到記憶體會失敗。
    # keep=True 是因為舊版 coverage 在 Windows 合併後刪檔會失敗，
    # 這些檔案會隨暫存目錄一起刪除
    cov = coverage.Coverage(data_file=str(Path(temporary) / ".coverage"))
    cov.combine([temporary], strict=True, keep=True)
    entries = read_entries(temporary)
    return {path: file_coverage(cov, path, entries) for path in paths}


def load_coverage(temporary, paths):
    measured = read_coverage(temporary, paths)
    # 實測 coverage 7.16.0 以前，Coverage 物件被回收前會鎖住資料檔，
    # 它又在循環參照裡，不手動回收的話 Windows 上暫存目錄刪不掉
    gc.collect()
    return measured


def apply_limits(rows, args):
    for row in rows:
        failures = []
        if row["crap"] > args.max_crap:
            failures.append("crap")
        if row["complexity"] > args.max_complexity:
            failures.append("complexity")
        if row["coverage"] < args.min_coverage:
            failures.append("coverage")
        row["failures"] = failures
    return any(row["failures"] for row in rows)


def save_report(path, report):
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def check(root, args, directory, report):
    require_modules()
    sources = [(root / value).resolve() for value in args.source]
    before = snapshot(root, sources)
    pytest_args = args.pytest_args
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]

    # 出錯時舊版 coverage 可能還鎖著資料檔；清不掉的目錄留在 .quality，
    # 不影響版控，也不會蓋掉真正的錯誤訊息
    with tempfile.TemporaryDirectory(
        prefix="run-", dir=directory, ignore_cleanup_errors=True
    ) as temporary:
        test_code = run_tests(
            root, sources, pytest_args or ["tests"], temporary
        )
        report["pytest_exit_code"] = test_code
        if test_code != 0:
            report.update(status="failed", reason="pytest failed")
            return 1

        if snapshot(root, sources) != before:
            raise ValueError("Source files changed during tests.")

        measured = load_coverage(temporary, before)
        report["coverage_tool"] = importlib.metadata.version("coverage")
        report["source_sha256"] = {
            path.relative_to(root).as_posix(): hashlib.sha256(content).hexdigest()
            for path, content in sorted(before.items())
        }
        rows, skipped = analyze(root, before, measured)
        failed = apply_limits(rows, args)
        report["functions"] = sorted(rows, key=lambda row: -row["crap"])
        report["skipped"] = skipped
        report["status"] = "failed" if failed else "passed"
        return 1 if failed else 0


def main(argv=None):
    args = arguments(argv)
    root = Path.cwd().resolve()
    directory = (root / ".quality").resolve()
    if not directory.is_relative_to(root):
        print("ERROR: .quality must stay inside the project.", file=sys.stderr)
        return 2

    report = {
        "status": "running",
        "coverage_metric": (
            "per-function statement coverage; "
            "functions never entered count as 0%"
        ),
        "limits": {
            "max_crap": args.max_crap,
            "max_complexity": args.max_complexity,
            "min_coverage_percent": args.min_coverage,
        },
    }
    try:
        directory.mkdir(exist_ok=True)
        # 讓整個 .quality 目錄不進版控，不必修改專案的 .gitignore
        (directory / ".gitignore").write_text("*\n", encoding="utf-8")
        output = directory / "report.json"
        save_report(output, report)
        try:
            code = check(root, args, directory, report)
        except Exception as error:
            report.update(status="error", reason=str(error))
            code = 2
        save_report(output, report)
    except OSError as error:
        print(f"ERROR: Cannot write report: {error}", file=sys.stderr)
        return 2

    for row in report.get("functions", []):
        state = "FAIL" if row["failures"] else "PASS"
        print(
            f"{state} {row['file']}:{row['line']} {row['function']} "
            f"C={row['complexity']} coverage={row['coverage']:.1f}% "
            f"CRAP={row['crap']:.2f}"
        )
    for row in report.get("skipped", []):
        print(
            f"SKIP {row['file']}:{row['line']} {row['function']} "
            f"({row['reason']})"
        )
    print(f"{report['status'].upper()}: {output}")
    if "reason" in report:
        print(report["reason"], file=sys.stderr)
    return code


if __name__ == "__main__":
    if sys.argv[1:2] == ["--measure"]:
        raise SystemExit(measure(json.loads(sys.argv[2]), sys.argv[3:]))
    raise SystemExit(main())
