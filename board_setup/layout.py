"""Infer Python source/test locations, with explicit overrides for ambiguity."""
import configparser
from pathlib import Path
import shlex
import tomllib


def metadata(project):
    path = project / "pyproject.toml"
    return tomllib.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def testLocations(project, explicit=None):
    if explicit:
        return [explicit]
    configured = metadata(project).get("tool", {}).get("pytest", {}).get("ini_options", {}).get("testpaths", [])
    ini = project / "pytest.ini"
    if ini.exists():
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(ini, encoding="utf-8-sig")
        configured = shlex.split(parser.get("pytest", "testpaths", fallback="")) or configured
    if configured:
        return shlex.split(configured) if isinstance(configured, str) else configured
    directories = [name for name in ("tests", "test", "spec") if (project / name).is_dir()]
    return directories or (["."] if list(project.glob("test_*.py")) else [])


def sourceLocations(project):
    if (project / "src").is_dir():
        return ["src"]
    packages = [path for path in project.iterdir() if path.is_dir() and (path / "__init__.py").is_file()]
    paths = [*project.glob("*.py"), *(file for package in packages for file in package.rglob("*.py"))]
    return sorted(path.relative_to(project).as_posix() for path in paths
                  if not path.name.startswith(("test_", "conftest"))
                  and not set(path.relative_to(project).parts) & {"tests", "test", "__pycache__"})


def checkedLocations(project, values, kind):
    if not values:
        raise ValueError(f"找不到{kind}；請用 --source／--tests 指定實際位置")
    result = []
    for value in values:
        path = (project / value).resolve()
        if not path.exists() or not path.is_relative_to(project):
            raise ValueError(f"{kind}不存在或超出專案：{value}")
        if kind == "原始碼" and (path == project or (path.is_file() and path.suffix != ".py")):
            raise ValueError("來源請指定 Python 子目錄或 .py 檔案，不可使用專案根目錄")
        result.append(path.relative_to(project).as_posix())
    return result


def chooseLayout(project, options, saved):
    sources = options.sources or saved.get("sources") or sourceLocations(project)
    tests = [options.tests] if options.tests else saved.get("tests") or testLocations(project)
    return {"sources": checkedLocations(project, sources, "原始碼"),
            "tests": checkedLocations(project, tests, "測試目錄")}
