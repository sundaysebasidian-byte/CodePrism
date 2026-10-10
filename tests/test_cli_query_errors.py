"""CLI query errors remain distinct from valid empty results on a real index."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

import codeprism.mcp.server as srv
from codeprism.cli import app
from codeprism.core.graph import GraphEngine
from codeprism.core.paths import get_db_path
from codeprism.core.storage import StorageManager
from codeprism.query.engine import QueryEngine

runner = CliRunner()
FILE_COMMANDS = ("callers", "context", "impact", "summary")


@pytest.fixture
def query_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Index two same-named files without touching the user's data directory."""
    monkeypatch.setattr("codeprism.core.paths.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    project = tmp_path / "project"
    for directory in ("api", "core"):
        source = project / directory / "utils.py"
        source.parent.mkdir(parents=True)
        source.write_text("def helper():\n    return 1\n", encoding="utf-8")
    with (project / "api" / "utils.py").open("a", encoding="utf-8") as stream:
        stream.write("\ndef used():\n    return 2\n\ndef invoke():\n    return used()\n")
    result = runner.invoke(app, ["index", str(project), "--languages", "python", "--workers", "1"])
    assert result.exit_code == 0, result.output
    return project


def _target(command: str, file: str, symbol: str = "helper") -> str:
    """Summary takes a file; the other query commands take file::symbol."""
    return file if command == "summary" else f"{file}::{symbol}"


@pytest.mark.parametrize("command", FILE_COMMANDS)
def test_cli_ambiguous_file_reports_candidates_on_stderr(query_project: Path, command: str) -> None:
    """Every affected command explains an ambiguous file and exits nonzero."""
    result = runner.invoke(
        app, [command, _target(command, "utils.py"), "--project", str(query_project)]
    )
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "ambiguous" in result.stderr
    assert "2 indexed files match" in result.stderr
    assert result.stderr.index("api/utils.py") < result.stderr.index("core/utils.py")
    assert "Pass a longer path" in result.stderr
    assert "No callers found" not in result.output


@pytest.mark.parametrize("command", FILE_COMMANDS)
def test_cli_missing_file_reports_not_found_on_stderr(query_project: Path, command: str) -> None:
    """An unindexed file is an error rather than a successful empty answer."""
    result = runner.invoke(
        app, [command, _target(command, "nowhere.py"), "--project", str(query_project)]
    )
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "Not found:" in result.stderr and "nowhere.py" in result.stderr
    assert "No callers found" not in result.output


@pytest.mark.parametrize("command", ["callers", "context", "impact"])
def test_cli_missing_symbol_reports_not_found_on_stderr(query_project: Path, command: str) -> None:
    """A known file with an unknown symbol remains distinct from zero callers."""
    result = runner.invoke(
        app,
        [command, _target(command, "api/utils.py", "nope"), "--project", str(query_project)],
    )
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "Not found:" in result.stderr and "nope" in result.stderr
    assert "No callers found" not in result.output


def test_cli_existing_symbol_with_zero_callers_succeeds(query_project: Path) -> None:
    """The genuine empty answer from the issue reproduction still exits zero."""
    result = runner.invoke(
        app, ["callers", "api/utils.py::helper", "--project", str(query_project)]
    )
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert "No callers found for helper" in result.stdout


def test_cli_existing_symbol_with_callers_keeps_results(query_project: Path) -> None:
    """Validation preserves the actual graph caller rather than fabricating an answer."""
    result = runner.invoke(app, ["callers", "api/utils.py::used", "--project", str(query_project)])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert "Callers of used" in result.stdout and "invoke" in result.stdout


@pytest.mark.parametrize("command", FILE_COMMANDS)
def test_cli_longer_path_resolves_against_project_not_cwd(
    query_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """All four commands accept an unambiguous path from an unrelated working directory."""
    cwd = tmp_path / "other"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    result = runner.invoke(
        app, [command, _target(command, "api/utils.py"), "--project", "../project"]
    )
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    expected = {
        "callers": "No callers found for helper",
        "context": "helper",
        "impact": "Impact: helper",
        "summary": "utils.py",
    }
    assert expected[command] in result.stdout


@pytest.fixture
async def query_engine(query_project: Path) -> AsyncIterator[QueryEngine]:
    """Load the persisted project graph with real SQLite storage for resolver tests."""
    storage = StorageManager(get_db_path(query_project))
    await storage.initialize()
    try:
        graph = GraphEngine()
        await graph.load_from_storage(storage)
        yield QueryEngine(graph, storage)
    finally:
        await storage.close()


@pytest.mark.parametrize("file", ["utils.py", "nowhere.py", "api/utils.py", ""])
async def test_shared_resolver_preserves_mcp_response_contract(
    query_project: Path, query_engine: QueryEngine, monkeypatch: pytest.MonkeyPatch, file: str
) -> None:
    """The existing MCP wrapper keeps its exact response keys, messages and candidates."""
    monkeypatch.setattr(srv, "_engine", query_engine)
    monkeypatch.setattr(srv, "_project_path", str(query_project))
    expected: tuple[str | None, dict[str, object] | None]
    if file == "utils.py":
        expected = (
            None,
            {
                "error": "Path 'utils.py' is ambiguous: 2 indexed files match",
                "candidates": ["api/utils.py", "core/utils.py"],
                "hint": "Pass a longer path, e.g. 'api/utils.py'",
            },
        )
    elif file == "api/utils.py":
        expected = (str((query_project / file).resolve()), None)
    else:
        expected = (
            None,
            {"error": f"File '{file}' not indexed", "project_path": str(query_project.resolve())},
        )
    assert await query_engine.resolve_file(file, str(query_project)) == expected
    assert await srv._resolve_file(file) == expected
