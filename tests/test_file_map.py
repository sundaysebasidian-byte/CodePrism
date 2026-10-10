"""Directory-scoped file maps with stable, explicit pagination."""

from io import StringIO
from pathlib import Path

import pytest
from fastmcp import Client
from rich.console import Console

import codeprism.cli as cli
import codeprism.mcp.server as server
from codeprism.core.models import FileRecord, NodeKind, SymbolRecord
from codeprism.query.engine import QueryEngine


@pytest.fixture
async def mapped_project(storage, graph, tmp_path, monkeypatch):
    root = tmp_path / "project"
    files = []
    for relative in ("src/a.py", "src/nested/b.py", "src_extra/c.py", "z.py"):
        path = root / relative
        file = FileRecord.create(str(path), language="python", line_count=10)
        await storage.upsert_file(file)
        await storage.upsert_symbol(
            SymbolRecord.create(
                file_path=str(path),
                file_id=file.id,
                name="work",
                kind=NodeKind.FUNCTION,
                line_start=1,
                line_end=2,
            )
        )
        files.append(str(path))
    engine = QueryEngine(graph, storage)
    monkeypatch.setattr(server, "_engine", engine)
    monkeypatch.setattr(server, "_project_path", str(root))
    return engine, root, files


async def test_directory_filter_and_totals(mapped_project):
    engine, root, files = mapped_project
    result = await engine.get_file_map(str(root / "src"))
    assert [e.path for e in result.entries] == files[:2]
    assert result.total_files == result.total_symbols == 2
    assert all(e.function_count == e.symbol_count == 1 for e in result.entries)


async def test_mcp_relative_directory_uses_served_root(mapped_project, tmp_path, monkeypatch):
    _, root, files = mapped_project
    monkeypatch.chdir(tmp_path)
    result = await server.get_file_map("src/../src/")
    assert [e["path"] for e in result["files"]] == files[:2]
    assert result["project_path"] == str(root / "src")
    assert result["total_symbols"] == 2


@pytest.mark.parametrize("relative", ["missing", "src_extra2", "src/a.py"])
async def test_unindexed_directory_is_an_error(mapped_project, relative):
    engine, root, _ = mapped_project
    with pytest.raises(ValueError, match="No indexed files"):
        await engine.get_file_map(str(root / relative))
    result = await server.get_file_map(relative)
    assert "No indexed files" in result["error"]


async def test_pages_keep_scoped_totals_and_order(mapped_project):
    engine, root, files = mapped_project
    first = await server.get_file_map(limit=2)
    second = await server.get_file_map(limit=2, offset=2)
    past_end = await server.get_file_map(limit=2, offset=4)
    assert [e["path"] for e in first["files"] + second["files"]] == files
    for page in (first, second, past_end):
        assert page["total_files"] == page["total_symbols"] == 4
        assert page["limit"] == 2
        assert page["truncated"] is True
    assert first["offset"] == 0
    assert second["offset"] == 2
    assert past_end["files"] == []
    whole = await engine.get_file_map(str(root), limit=None)
    assert len(whole.entries) == 4
    assert whole.truncated is False


async def test_default_and_maximum_caps(storage, graph, tmp_path, monkeypatch):
    root = tmp_path / "big"
    for i in reversed(range(1001)):
        await storage.upsert_file(FileRecord.create(str(root / f"{i:04d}.py")))
    engine = QueryEngine(graph, storage)
    monkeypatch.setattr(server, "_engine", engine)
    monkeypatch.setattr(server, "_project_path", str(root))
    result = await server.get_file_map()
    assert len(result["files"]) == result["limit"] == 200
    assert result["total_files"] == 1001
    assert result["truncated"] is True
    assert Path(result["files"][0]["path"]).name == "0000.py"
    assert len((await server.get_file_map(limit=1000))["files"]) == 1000
    assert len((await engine.get_file_map(str(root), limit=None)).entries) == 1001


@pytest.mark.parametrize("command", ["stats", "scan"])
async def test_cli_consumers_keep_complete_index(storage, graph, tmp_path, monkeypatch, command):
    """Whole-index CLI output includes files beyond the page cap and outside the root."""
    root = tmp_path / "project"
    root.mkdir()
    paths = [root / f"{i:04d}.py" for i in range(201)] + [tmp_path / "external.py"]
    for path in paths:
        path.write_text("value = 1\n", encoding="utf-8")
        await storage.upsert_file(FileRecord.create(str(path), language="python", line_count=1))
    engine = QueryEngine(graph, storage)

    async def open_session(project):
        return engine, storage

    output = StringIO()
    monkeypatch.setattr(cli, "_open_session", open_session)
    monkeypatch.setattr(cli, "console", Console(file=output, width=120))
    if command == "stats":
        await cli._stats(str(root), verbose=True)
        table = output.getvalue().rsplit("Files", 1)[1]
        rows = [line for line in table.splitlines() if line.lstrip().startswith("│")]
        assert len(rows) == len(paths)
        assert "0200.py" in table
    else:
        await cli._scan(".", all_=True, diff=None, project=str(root))
        assert "202 files · 0 issue(s) found" in output.getvalue()


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": -1}, {"limit": 1001}, {"offset": -1}])
async def test_invalid_pagination_is_an_error(mapped_project, kwargs):
    engine, root, _ = mapped_project
    with pytest.raises(ValueError):
        await engine.get_file_map(str(root), **kwargs)
    assert "error" in await server.get_file_map(**kwargs)


async def test_empty_index_and_protocol_arguments(storage, graph, tmp_path, monkeypatch):
    import codeprism.core.paths as paths

    monkeypatch.setattr(server, "_engine", QueryEngine(graph, storage))
    monkeypatch.setattr(server, "_project_path", str(tmp_path))
    monkeypatch.setattr(server, "_auto_index", False)
    monkeypatch.setattr(paths, "get_db_path", lambda project: tmp_path / "protocol.db")
    assert "error" in await server.get_file_map()
    async with Client(server.mcp) as client:
        tools = await client.list_tools()
        tool = next(t for t in tools if t.name == "get_file_map")
        schema = getattr(tool, "input_schema", None) or tool.inputSchema
        assert schema["properties"]["limit"]["default"] == 200
        assert "offset" in schema["properties"]
        result = await client.call_tool("get_file_map", {"limit": 1, "offset": 0})
        assert "No indexed files" in result.data["error"]
