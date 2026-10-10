"""Verbose stats rendering using real temporary project indexes."""

import asyncio
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from codeprism.cli import app
from codeprism.core.models import FileRecord
from codeprism.core.paths import get_db_path
from codeprism.core.storage import StorageManager

runner = CliRunner()


def _index_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative_paths: tuple[Path, ...]
) -> Path:
    """Create a deep project and index its files in isolated SQLite storage."""
    monkeypatch.setattr("codeprism.core.paths.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    project = tmp_path / ("deep_project_" * 6)
    for relative_path in relative_paths:
        source = project / relative_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("def process():\n    return 1\n", encoding="utf-8")
    result = runner.invoke(app, ["index", str(project), "--languages", "python", "--workers", "1"])
    assert result.exit_code == 0, result.output
    return project


def _file_rows(output: str) -> list[str]:
    """Read data rows from the final Files table, excluding its header."""
    table = output.rsplit("Files", 1)[1]
    return [
        line
        for line in table.splitlines()
        if line.lstrip().startswith("│") and line.split("│")[1].strip() != "Path"
    ]


@pytest.mark.parametrize("width", [80, 120])
@pytest.mark.parametrize("relative_project", [False, True])
def test_stats_verbose_shows_distinct_project_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, width: int, relative_project: bool
) -> None:
    """Deep absolute prefixes disappear while same-named files stay identifiable."""
    relative_paths = (
        Path("src/domain/payments/processor.py"),
        Path("src/domain/refunds/processor.py"),
    )
    project = _index_project(tmp_path, monkeypatch, relative_paths)
    monkeypatch.setattr("codeprism.cli.console", Console(width=width))
    monkeypatch.chdir(tmp_path)
    project_arg = project.name if relative_project else str(project)

    result = runner.invoke(app, ["stats", "--verbose", "--project", project_arg])

    assert result.exit_code == 0, result.output
    rows = _file_rows(result.output)
    assert len(rows) == len(relative_paths), result.output
    for relative_path in relative_paths:
        assert sum(str(relative_path) in row for row in rows) == 1, result.output
    assert project.name not in result.output
    assert all(len(line) <= width for line in result.output.splitlines())


@pytest.mark.parametrize("width", [80, 120])
def test_stats_verbose_truncates_long_relative_paths_to_one_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    """Very long relative paths truncate without wrapping the numeric columns."""
    relative_path = Path("src") / ("nested directory " * 6) / "processor.py"
    project = _index_project(tmp_path, monkeypatch, (relative_path,))
    monkeypatch.setattr("codeprism.cli.console", Console(width=width))

    result = runner.invoke(app, ["stats", "--verbose", "--project", str(project)])

    assert result.exit_code == 0, result.output
    rows = _file_rows(result.output)
    assert len(rows) == 1, result.output
    columns = [column.strip() for column in rows[0].split("│")[1:-1]]
    assert columns[0].startswith(str(Path("src") / "nested directory"))
    assert columns[0].endswith("…"), result.output
    assert columns[1:] == ["python", "3", "1"]
    assert all(len(line) <= width for line in result.output.splitlines())


def test_stats_verbose_keeps_indexed_paths_outside_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An out-of-root indexed record stays visible instead of breaking stats."""
    project = _index_project(tmp_path, monkeypatch, (Path("app.py"),))
    external = tmp_path / "external.py"
    external.write_text("value = 1\n", encoding="utf-8")

    async def add_external_file() -> None:
        storage = StorageManager(get_db_path(project))
        await storage.initialize()
        try:
            await storage.upsert_file(
                FileRecord.create(path=str(external), language="python", line_count=1)
            )
        finally:
            await storage.close()

    asyncio.run(add_external_file())
    monkeypatch.setattr("codeprism.cli.console", Console(width=120))

    result = runner.invoke(app, ["stats", "--verbose", "--project", str(project)])

    assert result.exit_code == 0, result.output
    rows = _file_rows(result.output)
    assert len(rows) == 2, result.output
    external_rows = [
        row
        for row in rows
        if [column.strip() for column in row.split("│")[2:-1]] == ["python", "1", "0"]
    ]
    assert len(external_rows) == 1, result.output
    assert external_rows[0].split("│")[1].strip().startswith(external.anchor)
