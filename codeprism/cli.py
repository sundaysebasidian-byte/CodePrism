"""CodePrism CLI — typer-based entry point."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .core.languages import SUPPORTED_LANGUAGES, normalize_language
from .core.models import SYMBOL_KINDS

if TYPE_CHECKING:
    from .query.engine import QueryEngine

# Allowlist for git ref characters — prevents argument injection via diff_range
_SAFE_GIT_REF_RE = re.compile(r"^(?!-)[\w./~^@{}:+\-]{1,200}$")

app = typer.Typer(
    name="codeprism",
    help="CodePrism — knowledge graph for AI coding agents.",
    add_completion=False,
)
console = Console()


def _version_callback(value: bool) -> None:
    """Print the installed version and exit when --version is passed."""
    if value:
        from . import __version__

        typer.echo(f"codeprism {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed version and exit.",
    ),
) -> None:
    """CodePrism — knowledge graph for AI coding agents."""


# ── Shared helpers ────────────────────────────────────────────────────────────


async def _open_session(project_path: str):
    """Open storage + graph for an already-indexed project."""
    from .core.graph import GraphEngine
    from .core.paths import get_db_path
    from .core.storage import StorageManager
    from .query.engine import QueryEngine

    db_path = get_db_path(project_path)
    storage = StorageManager(db_path)
    await storage.initialize()
    graph = GraphEngine()
    await graph.load_from_storage(storage)
    return QueryEngine(graph, storage), storage


async def _resolve_query_file(engine: QueryEngine, file: str, project: str) -> str:
    """Report failed file resolution on stderr before running a CLI query."""
    path, error = await engine.resolve_file(file, project)
    if error is not None:
        if "candidates" in error:
            typer.echo(error["error"], err=True)
            for candidate in error["candidates"]:
                typer.echo(f"  {candidate}", err=True)
            typer.echo(error["hint"], err=True)
        else:
            typer.echo(f"Not found: {error['error']}", err=True)
        raise typer.Exit(1)
    assert path is not None
    return path


def _parse_target(target: str) -> tuple[str, str]:
    """Parse 'file.py::symbol' → (file_path, symbol_name)."""
    if "::" not in target:
        console.print("[red]Error:[/red] Use format  file.py::symbol_name")
        raise typer.Exit(1)
    file_path, symbol = target.rsplit("::", 1)
    return file_path, symbol


# ── index ─────────────────────────────────────────────────────────────────────


@app.command()
def index(
    path: str = typer.Argument(..., help="Project directory to index"),
    languages: str | None = typer.Option(
        None,
        "--languages",
        "-l",
        help="Comma-separated language list (default: project config, or all supported languages)",
    ),
    embeddings: bool | None = typer.Option(
        None,
        "--embeddings/--no-embeddings",
        "-e",
        help="Override semantic vector indexing (default: project config; requires codeprism[embeddings])",
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Re-parse all files even if unchanged (skip incremental check)"
    ),
    workers: int = typer.Option(
        0, "--workers", "-j", help="Parser processes (0 = one per CPU core, 1 = in-process)"
    ),
) -> None:
    """Build the knowledge graph for a project directory.

    Re-runs are incremental by default: only changed or new files are parsed.
    Use --force to re-parse everything from scratch.
    Project .codeprism.toml settings apply unless explicitly overridden by flags.
    """
    if languages:
        requested_languages = [language.strip() for language in languages.split(",")]
        unknown_languages = list(
            dict.fromkeys(
                language
                for language in requested_languages
                if normalize_language(language) not in SUPPORTED_LANGUAGES
            )
        )
        if unknown_languages:
            unknown = ", ".join(f"'{language}'" for language in unknown_languages)
            supported = ", ".join(SUPPORTED_LANGUAGES)
            raise typer.BadParameter(
                f"Unknown language {unknown}. Supported: {supported}",
                param_hint="--languages",
            )

    asyncio.run(_index(path, languages, embeddings, force, workers))


async def _index(
    path: str,
    languages: str | None,
    embeddings: bool | None = None,
    force: bool = False,
    workers: int = 0,
) -> None:
    from .core.config import CodePrismConfig
    from .core.graph import GraphEngine
    from .core.paths import get_db_path, get_project_config_path
    from .core.storage import StorageManager
    from .indexer.project_indexer import ProjectIndexer

    langs = [lang.strip() for lang in languages.split(",")] if languages else None
    config = CodePrismConfig.load(get_project_config_path(path))
    if langs is not None:
        config.languages = langs
    if embeddings is not None:
        config.enable_embeddings = embeddings
    config.parse_workers = workers

    db_path = get_db_path(path)
    storage = StorageManager(db_path)
    await storage.initialize()
    graph = GraphEngine()

    mode = "[dim](full re-index)[/dim]" if force else "[dim](incremental)[/dim]"
    console.print(f"Indexing [bold]{path}[/bold] {mode}")
    if config.enable_embeddings:
        console.print("[dim]Embeddings enabled — will build vector index after parsing...[/dim]")
    indexer = ProjectIndexer(graph, storage, config)
    result = await indexer.index(path, force=force)
    await storage.close()

    if result.format_upgraded:
        console.print(
            "[yellow]Index format changed since this project was indexed — "
            "all files were re-parsed.[/yellow]"
        )
    if result.success:
        skipped_note = (
            f" · [dim]{result.files_skipped} unchanged[/dim]" if result.files_skipped else ""
        )
        console.print(
            f"[green]Done.[/green] "
            f"{result.file_count} files · {result.symbol_count} symbols · "
            f"{result.edge_count} edges · {result.duration_seconds:.2f}s"
            f"{skipped_note}"
        )
        if config.enable_embeddings:
            console.print("[green]Semantic index built.[/green] search_symbol now uses embeddings.")
    else:
        console.print(f"[yellow]Completed with {len(result.errors)} error(s).[/yellow]")
        for err in result.errors:
            console.print(f"  [red]•[/red] {err}")


# ── context ───────────────────────────────────────────────────────────────────


@app.command()
def context(
    target: str = typer.Argument(..., help="file.py::symbol_name"),
    depth: int = typer.Option(2, "--depth", "-d", help="Traversal depth (1-3)"),
    project: str = typer.Option(".", "--project", "-p", help="Project path"),
) -> None:
    """Get structured context for a symbol."""
    asyncio.run(_context(target, depth, project))


async def _context(target: str, depth: int, project: str) -> None:
    file_path, sym_name = _parse_target(target)
    engine, storage = await _open_session(project)
    try:
        resolved = await _resolve_query_file(engine, file_path, project)
        result = await engine.get_context(resolved, sym_name, depth)
    finally:
        await storage.close()

    if result is None:
        typer.echo(f"Not found: {sym_name} in {file_path}", err=True)
        raise typer.Exit(1)

    s = result.symbol
    console.print(
        Panel(
            f"[bold]{s.name}[/bold]  [{s.kind.value}]\n"
            f"[dim]{s.signature or ''}[/dim]\n\n" + (s.docstring or ""),
            title=f"{file_path}  line {s.line_start}–{s.line_end}",
        )
    )

    if result.direct_callers:
        console.print("\n[bold]Callers:[/bold]")
        for c in result.direct_callers:
            console.print(f"  • {c.name} ({c.kind.value})")

    if result.direct_callees:
        console.print("\n[bold]Callees:[/bold]")
        for c in result.direct_callees:
            console.print(f"  • {c.name} ({c.kind.value})")

    console.print(f"\n[dim]Estimated tokens: {result.estimated_token_count}[/dim]")


# ── impact ────────────────────────────────────────────────────────────────────


@app.command()
def impact(
    target: str = typer.Argument(..., help="file.py::symbol_name"),
    project: str = typer.Option(".", "--project", "-p", help="Project path"),
) -> None:
    """Transitive impact analysis — what breaks if this symbol changes?"""
    asyncio.run(_impact(target, project))


async def _impact(target: str, project: str) -> None:
    file_path, sym_name = _parse_target(target)
    engine, storage = await _open_session(project)
    try:
        resolved = await _resolve_query_file(engine, file_path, project)
        result = await engine.get_impact(resolved, sym_name)
    finally:
        await storage.close()

    if result is None:
        typer.echo(f"Not found: {sym_name} in {file_path}", err=True)
        raise typer.Exit(1)

    severity_colour = {"LOW": "green", "MEDIUM": "yellow", "HIGH": "red", "CRITICAL": "bright_red"}
    colour = severity_colour.get(result.severity, "white")

    console.print(
        Panel(
            f"Severity: [{colour}][bold]{result.severity}[/bold][/{colour}]\n"
            f"Direct dependents: {len(result.direct_dependents)}\n"
            f"Transitive dependents: {result.estimated_change_surface}\n"
            f"Public API affected: {'yes' if result.public_api_affected else 'no'}\n"
            f"Affected test files: {len(result.affected_test_files)}",
            title=f"Impact: {sym_name}",
        )
    )

    if result.direct_dependents:
        console.print("\n[bold]Direct dependents:[/bold]")
        for s in result.direct_dependents[:10]:
            console.print(f"  • {s.name} ({s.kind.value})")

    if result.affected_test_files:
        console.print("\n[bold]Affected test files:[/bold]")
        for fp in result.affected_test_files:
            console.print(f"  • {fp}")


# ── summary ───────────────────────────────────────────────────────────────────


@app.command()
def summary(
    file: str = typer.Argument(..., help="Source file path"),
    project: str = typer.Option(".", "--project", "-p", help="Project path"),
) -> None:
    """High-level summary of a source file."""
    asyncio.run(_summary(file, project))


async def _summary(file: str, project: str) -> None:
    engine, storage = await _open_session(project)
    try:
        resolved = await _resolve_query_file(engine, file, project)
        result = await engine.get_module_summary(resolved)
    finally:
        await storage.close()

    if result is None:
        typer.echo(f"Not found: {file}", err=True)
        raise typer.Exit(1)

    console.print(Panel(result.purpose, title=Path(file).name))
    console.print(f"Complexity score: {result.complexity_score:.1f}")

    if result.public_api:
        console.print("\n[bold]Public API:[/bold]")
        for s in result.public_api:
            console.print(f"  • {s.name} ({s.kind.value})")

    if result.key_classes:
        console.print("\n[bold]Key classes:[/bold]")
        for c in result.key_classes:
            console.print(f"  • {c.name}")

    if result.dependencies:
        console.print(f"\n[bold]Dependencies:[/bold] {', '.join(result.dependencies[:8])}")

    if result.test_coverage_file:
        console.print(f"\n[bold]Test file:[/bold] {result.test_coverage_file}")


# ── callers ───────────────────────────────────────────────────────────────────


@app.command()
def callers(
    target: str = typer.Argument(..., help="file.py::function_name"),
    project: str = typer.Option(".", "--project", "-p", help="Project path"),
) -> None:
    """List all functions that call the given function."""
    asyncio.run(_callers(target, project))


async def _callers(target: str, project: str) -> None:
    file_path, sym_name = _parse_target(target)
    engine, storage = await _open_session(project)
    try:
        resolved = await _resolve_query_file(engine, file_path, project)
        if await engine.find_symbol(resolved, sym_name) is None:
            typer.echo(f"Not found: {sym_name} in {file_path}", err=True)
            raise typer.Exit(1)
        syms = await engine.get_callers(resolved, sym_name)
    finally:
        await storage.close()

    if not syms:
        console.print(f"No callers found for [bold]{sym_name}[/bold]")
        return

    console.print(f"[bold]Callers of {sym_name}[/bold] ({len(syms)})")
    for s in syms:
        console.print(f"  • {s.name}  line {s.line_start}")


# ── search ────────────────────────────────────────────────────────────────────


@app.command()
def search(
    query: str = typer.Argument(..., help="Symbol name or substring"),
    kind: str | None = typer.Option(
        None, "--kind", "-k", help="Symbol kind (case-insensitive): " + ", ".join(SYMBOL_KINDS)
    ),
    project: str = typer.Option(".", "--project", "-p", help="Project path"),
) -> None:
    """Find symbols matching a query string."""
    asyncio.run(_search(query, kind, project))


async def _search(query: str, kind: str | None, project: str) -> None:
    engine, storage = await _open_session(project)
    try:
        matches = await engine.search_symbols(query, kind)
    except ValueError as error:
        console.print(f"Error: {error}", markup=False)
        raise typer.Exit(2) from error
    finally:
        await storage.close()

    if not matches:
        console.print(f"No matches for [bold]{query}[/bold]")
        return

    table = Table(title=f"Results for '{query}' ({len(matches)} found)")
    table.add_column("Name", style="bold")
    table.add_column("Kind")
    table.add_column("File")
    table.add_column("Line")

    for m in matches[:30]:
        table.add_row(
            m.symbol.name,
            m.symbol.kind.value,
            m.file_path,
            str(m.symbol.line_start or ""),
        )
    console.print(table)


# ── stats ─────────────────────────────────────────────────────────────────────


@app.command()
def stats(
    project: str = typer.Option(".", "--project", "-p", help="Project path"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output stats as JSON"),
) -> None:
    """Show knowledge graph statistics."""
    asyncio.run(_stats(project, verbose, json_output))


async def _stats(project: str, verbose: bool, json_output: bool = False) -> None:
    import json as _json

    engine, storage = await _open_session(project)
    try:
        data = await engine.get_stats()
        file_map = await engine.get_file_map(project) if verbose else None
    finally:
        await storage.close()

    if json_output:
        print(
            _json.dumps(
                {
                    "file_count": data["file_count"],
                    "function_count": data["function_count"],
                    "class_count": data["class_count"],
                    "variable_count": data["variable_count"],
                    "import_count": data["import_count"],
                    "edge_count": data["edge_count"],
                    "languages": data["languages"] or [],
                    "last_indexed_at": data.get("last_indexed_at"),
                    "coverage_percent": data.get("coverage_percent", 0.0),
                }
            )
        )
        return

    console.print(
        Panel(
            f"Files:     {data['file_count']}\n"
            f"Functions: {data['function_count']}\n"
            f"Classes:   {data['class_count']}\n"
            f"Variables: {data['variable_count']}\n"
            f"Imports:   {data['import_count']}\n"
            f"Edges:     {data['edge_count']}\n"
            f"Languages: {', '.join(data['languages'] or ['-'])}",
            title="CodePrism Graph Stats",
        )
    )

    if verbose and file_map:
        table = Table(title="Files")
        table.add_column("Path", max_width=60, overflow="ellipsis")
        table.add_column("Lang")
        table.add_column("Lines", justify="right")
        table.add_column("Symbols", justify="right")
        project_root = Path(project).resolve()
        for e in file_map.entries:
            try:
                display_path = str(Path(e.path).relative_to(project_root))
            except ValueError:
                display_path = e.path
            # Let the column shrink on narrow terminals, but keep each path on one line.
            table.add_row(
                Text(display_path, no_wrap=True),
                e.language,
                str(e.line_count),
                str(e.symbol_count),
            )
        console.print(table)


# ── serve ─────────────────────────────────────────────────────────────────────


@app.command()
def serve(
    path: str | None = typer.Argument(
        None,
        help="Project directory. Default: the project (nearest .git / .codeprism.toml) "
        "containing the current directory — so one user-level config serves every project.",
    ),
    transport: str = typer.Option("stdio", "--transport", "-t", help="stdio | sse"),
    port: int = typer.Option(8765, "--port", help="Port for SSE transport"),
    no_auto_index: bool = typer.Option(
        False, "--no-auto-index", help="Don't build/refresh the index in the background"
    ),
) -> None:
    """Start the MCP server (default: stdio transport for Claude Code etc.)."""
    from .core.paths import find_project_root
    from .mcp.server import configure, mcp

    if path is None:
        root = find_project_root(Path.cwd())
        # Outside any project (e.g. launched from ~): serve the cwd, never auto-index it
        project, auto = (str(root), True) if root else (str(Path.cwd()), False)
    else:
        project, auto = path, True
    configure(project, auto_index=auto and not no_auto_index)
    if transport == "sse":
        console.print(
            "[yellow]Warning:[/yellow] SSE transport has no built-in authentication. "
            "Add a reverse-proxy auth layer (nginx/Caddy) before exposing to a network."
        )
        mcp.run(transport="sse", port=port)
    else:
        mcp.run()


# ── watch ─────────────────────────────────────────────────────────────────────


@app.command()
def watch(
    path: str = typer.Argument(..., help="Project directory to watch"),
) -> None:
    """Watch a project directory and incrementally update the graph on file changes."""
    asyncio.run(_watch(path))


async def _watch(path: str) -> None:
    from .core.graph import GraphEngine
    from .core.paths import get_db_path
    from .core.storage import StorageManager
    from .indexer.incremental_updater import IncrementalUpdater
    from .indexer.watcher import ProjectWatcher

    db_path = get_db_path(path)
    storage = StorageManager(db_path)
    await storage.initialize()
    graph = GraphEngine()
    await graph.load_from_storage(storage)
    updater = IncrementalUpdater(graph, storage)

    def on_update(fp: str, result) -> None:
        console.print(
            f"Updated [bold]{Path(fp).name}[/bold]: "
            f"+{result.nodes_added} −{result.nodes_removed} symbols"
        )

    watcher = ProjectWatcher(updater, on_update=on_update)
    console.print(f"Watching [bold]{path}[/bold]  (Ctrl+C to stop)")
    try:
        await watcher.run(path)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await storage.close()
        console.print("\nStopped.")


# ── setup ─────────────────────────────────────────────────────────────────────


@app.command()
def setup(
    agent: str = typer.Argument(
        "claude",
        help="Target agent: claude | codex | cursor | windsurf | continue | zed",
    ),
    project: str = typer.Option(".", "--project", "-p", help="Project path to serve"),
    global_: bool = typer.Option(
        False,
        "--global",
        "-g",
        help="Write to the agent's user-level config instead of the project",
    ),
) -> None:
    """Configure an AI coding agent to use CodePrism as an MCP server.

    Examples:
        codeprism setup claude --project /path/to/repo
        codeprism setup codex --project /path/to/repo
        codeprism setup cursor --project /path/to/repo --global
        codeprism setup windsurf --project /path/to/repo
        codeprism setup continue --project /path/to/repo
        codeprism setup zed --project /path/to/repo
    """
    _setup(agent, project, global_)


def _setup(agent: str, project: str, global_: bool) -> None:

    abs_project = str(Path(project).resolve())
    server_entry = {
        "command": "codeprism",
        "args": ["serve", abs_project],
    }

    # Project-scoped files go in the --project directory, not the current one
    project_dir = Path(abs_project)
    agent = agent.lower()
    if global_ and agent in ("claude", "codex"):
        # A user-level entry must work in every project: these agents launch MCP
        # servers in the directory the session starts in, and a path-less
        # `codeprism serve` serves the project containing that directory.
        server_entry = {"command": "codeprism", "args": ["serve"]}
    if agent == "claude":
        _write_claude_config(server_entry, global_, project_dir)
    elif agent == "codex":
        _write_codex_config(server_entry, global_, project_dir)
    elif agent == "cursor":
        _write_cursor_config(server_entry, global_, project_dir)
    elif agent == "windsurf":
        _write_windsurf_config(server_entry, global_, project_dir)
    elif agent in ("continue", "continue.dev"):
        _write_continue_config(server_entry, global_, project_dir)
    elif agent == "zed":
        _write_zed_config(server_entry, global_, project_dir)
    else:
        console.print(
            f"[red]Unknown agent:[/red] {agent!r}. "
            "Supported: claude, codex, cursor, windsurf, continue, zed"
        )
        raise typer.Exit(1)


_CODEPRISM_MARKER = "<!-- codeprism-instructions -->"

_AGENTS_MD_BLOCK = """\
<!-- codeprism-instructions -->
## CodePrism — Knowledge Graph (added by `codeprism setup`)

This project is indexed with [CodePrism](https://github.com/knight22-21/CodePrism).
A live knowledge graph of every file, symbol, and relationship is available to any
coding agent through the `codeprism` MCP server.

### Use CodePrism FIRST — before reading any source file

| Instead of … | Use … |
|---|---|
| Reading a file to understand a function | `get_context(file, symbol, depth=2)` |
| Grepping for who calls a function | `get_callers(file, function)` |
| Reading a file to understand its role | `get_module_summary(file)` |
| Guessing the blast radius of a change | `get_impact(file, symbol)` |
| Searching for a symbol by name | `search_symbol(query)` |
| Reading a file before writing it | `scan_diff(original, proposed, file)` |
| Wondering what you already read | `get_session_context(session_id)` |

### Tool quick-reference

```
get_context(file, symbol, depth=2)      → signature, callers, callees, types (< 400 tokens)
get_module_summary(file)                → purpose, public API, complexity (< 150 tokens)
get_impact(file, symbol)                → severity, dependents, affected tests
get_callers(file, function)             → every call site with line numbers
get_callees(file, function)             → every function this one calls
search_symbol(query, kind=None)         → find symbols by name substring
get_file_map(project_path)              → full file tree with role summaries
get_dependencies(file)                  → imports: internal vs external
scan_diff(original, proposed, file)     → security check before any write
record_read(session_id, file, symbol)   → log what you've already read
record_write(session_id, file, before, after) → log + security gate + graph sync
get_session_context(session_id)         → compact digest of session activity
undo_write(session_id, steps=1)         → roll back agent-authored writes
```

### Rules

1. **Always query the graph before reading files.** `get_context` returns callers,
   callees, and signature in under 400 tokens. Reading the whole file costs 10–100×
   more context for the same information.
2. **Only fall back to `Read`/`Grep` when graph data is provably insufficient** —
   e.g. you need the exact implementation body, not just the structure.
3. **Always call `scan_diff` before writing a file.** A `BLOCK` status means the
   proposed content contains a critical security issue — do not write it.
4. **Use `record_read` / `record_write` to track session state** so you never
   re-fetch context you already have.
<!-- /codeprism-instructions -->"""

# CLAUDE.md stays thin: Claude Code imports the shared AGENTS.md guide. It reads
# AGENTS.md on its own only when no CLAUDE.md exists, so the import keeps both
# working together (https://code.claude.com/docs/en/memory#agents-md).
_CLAUDE_MD_BLOCK = """\
<!-- codeprism-instructions -->
## CodePrism

CodePrism usage instructions for every coding agent live in AGENTS.md:

@AGENTS.md
<!-- /codeprism-instructions -->"""

_CLAUDE_MD_BLOCK_NO_IMPORT = """\
<!-- codeprism-instructions -->
## CodePrism

CodePrism usage instructions are in AGENTS.md (already imported above).
<!-- /codeprism-instructions -->"""


def _read_json_config(path: Path) -> dict:
    """Load a JSON config, refusing to clobber a file we can't parse."""
    import json

    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError:
        console.print(
            f"[red]Error:[/red] {path} is not valid JSON — fix or remove it, then re-run setup."
        )
        raise typer.Exit(1) from None
    if not isinstance(data, dict):
        console.print(f"[red]Error:[/red] {path} does not contain a JSON object.")
        raise typer.Exit(1)
    return data


def _write_json_config(path: Path, data: dict) -> None:
    """Write JSON atomically (temp file + replace) so a crash never truncates it."""
    import json
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".codeprism-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _drop_stale_claude_settings_entry(settings_file: Path) -> None:
    """Remove the `mcpServers.codeprism` key older setups wrote to settings.json.

    Claude Code never read MCP servers from settings.json, so the entry was dead
    weight that made it look configured when it wasn't.
    """
    if not settings_file.exists():
        return
    data = _read_json_config(settings_file)
    servers = data.get("mcpServers")
    if isinstance(servers, dict) and "codeprism" in servers:
        del servers["codeprism"]
        if not servers:
            del data["mcpServers"]
        _write_json_config(settings_file, data)


def _write_claude_config(
    server_entry: dict, global_: bool, project_dir: Path | None = None
) -> None:
    """Register CodePrism where Claude Code actually reads MCP servers.

    Project scope: ``<project>/.mcp.json`` (shared, commit it), pre-approved for
    you via ``enabledMcpjsonServers`` in ``.claude/settings.local.json``.
    User scope (``--global``): top-level ``mcpServers`` in ``~/.claude.json``.
    """
    project_dir = project_dir or Path.cwd()
    entry = {"type": "stdio", **server_entry}

    if global_:
        config_file = Path.home() / ".claude.json"
        data = _read_json_config(config_file)
        data.setdefault("mcpServers", {})["codeprism"] = entry
        _write_json_config(config_file, data)
        _drop_stale_claude_settings_entry(Path.home() / ".claude" / "settings.json")
        scope = "user scope, all projects"
    else:
        config_file = project_dir / ".mcp.json"
        data = _read_json_config(config_file)
        data.setdefault("mcpServers", {})["codeprism"] = entry
        _write_json_config(config_file, data)

        # Project .mcp.json servers need a one-time approval; pre-approve it for
        # the person running setup without committing that choice for teammates.
        local_settings = project_dir / ".claude" / "settings.local.json"
        local = _read_json_config(local_settings)
        approved = local.setdefault("enabledMcpjsonServers", [])
        if "codeprism" not in approved:
            approved.append("codeprism")
            _write_json_config(local_settings, local)
        _drop_stale_claude_settings_entry(project_dir / ".claude" / "settings.json")
        scope = "project scope"

    # Instructions always live in the project directory
    agents_md = _write_agents_md(project_dir)
    claude_md = _write_claude_md(project_dir)

    console.print(
        f"[green]Done.[/green] CodePrism MCP server added to [bold]{config_file}[/bold] ({scope})."
    )
    _print_instructions_written(agents_md, claude_md)
    console.print(
        "[dim]Restart Claude Code, then run /mcp to confirm codeprism is connected.[/dim]"
    )


def _toml_str(value: str) -> str:
    import json

    return json.dumps(value)  # a JSON string is a valid TOML basic string


def _upsert_codex_server(text: str, server_entry: dict) -> str:
    """Replace or add ``[mcp_servers.codeprism]`` in a Codex config.toml.

    Line-based so comments and formatting elsewhere survive. The result is
    re-parsed and must equal the original with only our table changed.
    """
    import re
    import tomllib

    original = tomllib.loads(text) if text.strip() else {}
    header = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(#.*)?$")
    kept, skipping = [], False
    for line in text.splitlines():
        m = header.match(line)
        if m:
            name = re.sub(r"\s*\.\s*", ".", m.group(1)).replace('"', "").replace("'", "")
            skipping = name == "mcp_servers.codeprism" or name.startswith("mcp_servers.codeprism.")
        if not skipping:
            kept.append(line)
    args = ", ".join(_toml_str(a) for a in server_entry["args"])
    table = (
        "[mcp_servers.codeprism]\n"
        f"command = {_toml_str(server_entry['command'])}\n"
        f"args = [{args}]\n"
    )
    body = "\n".join(kept).rstrip()
    updated = (body + "\n\n" if body else "") + table

    parsed = tomllib.loads(updated)
    expected = dict(original)
    expected["mcp_servers"] = {
        **original.get("mcp_servers", {}),
        "codeprism": {"command": server_entry["command"], "args": list(server_entry["args"])},
    }
    if parsed != expected:
        raise ValueError("could not update config.toml without changing other settings")
    return updated


def _write_codex_config(server_entry: dict, global_: bool, project_dir: Path | None = None) -> None:
    """Codex: [mcp_servers.codeprism] in config.toml + AGENTS.md instructions."""
    import os
    import tomllib

    project_dir = project_dir or Path.cwd()
    config_file = (
        (Path.home() / ".codex" / "config.toml")
        if global_
        else (project_dir / ".codex" / "config.toml")
    )
    text = config_file.read_text(encoding="utf-8") if config_file.exists() else ""
    try:
        updated = _upsert_codex_server(text, server_entry)
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        args = " ".join(server_entry["args"])
        console.print(f"[red]Error:[/red] could not update {config_file}: {exc}")
        console.print(
            f"Add it manually: [bold]codex mcp add codeprism -- {server_entry['command']} {args}[/bold]"
        )
        raise typer.Exit(1) from None
    config_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_file.with_name(config_file.name + ".codeprism-tmp")
    tmp.write_text(updated, encoding="utf-8")
    os.replace(tmp, config_file)

    agents_md = _write_agents_md(project_dir)
    scope = "user config, all projects" if global_ else "project config"
    console.print(
        f"[green]Done.[/green] CodePrism MCP server added to [bold]{config_file}[/bold] ({scope})."
    )
    _print_instructions_written(agents_md)
    if not global_:
        console.print(
            "[dim]Codex only loads .codex/config.toml in trusted projects — accept the trust "
            "prompt, or use --global to write ~/.codex/config.toml.[/dim]"
        )
    console.print(
        "[dim]Start a new Codex session, then run /mcp to confirm codeprism is listed.[/dim]"
    )


def _write_cursor_config(
    server_entry: dict, global_: bool, project_dir: Path | None = None
) -> None:
    import json

    project_dir = project_dir or Path.cwd()
    if global_:
        config_dir = Path.home() / ".cursor"
    else:
        config_dir = project_dir / ".cursor"

    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "mcp.json"

    existing: dict = {}
    if config_file.exists():
        try:
            existing = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    servers = existing.setdefault("mcpServers", {})
    servers["codeprism"] = server_entry
    config_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    # Cursor reads AGENTS.md natively; .cursorrules is the legacy format
    agents_md = _write_agents_md(project_dir)

    scope = "global" if global_ else "project"
    console.print(
        f"[green]Done.[/green] CodePrism MCP server added to [bold]{config_file}[/bold] ({scope})."
    )
    _print_instructions_written(agents_md)
    console.print("[dim]Restart Cursor to pick up the change.[/dim]")


def _write_windsurf_config(
    server_entry: dict, global_: bool, project_dir: Path | None = None
) -> None:
    import json

    project_dir = project_dir or Path.cwd()
    if global_:
        config_dir = Path.home() / ".codeium" / "windsurf"
    else:
        config_dir = project_dir / ".windsurf"

    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "mcp_config.json"

    existing: dict = {}
    if config_file.exists():
        try:
            existing = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    servers = existing.setdefault("mcpServers", {})
    servers["codeprism"] = server_entry
    config_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    _print_instructions_written(_write_agents_md(project_dir))

    scope = "global (~/.codeium/windsurf/)" if global_ else "project (.windsurf/)"
    console.print(
        f"[green]Done.[/green] CodePrism MCP server added to [bold]{config_file}[/bold] ({scope})."
    )
    console.print("[dim]Restart Windsurf / Cascade to pick up the change.[/dim]")


def _write_continue_rule(project_dir: Path) -> Path:
    """Continue.dev reads .continue/rules/*.md, not AGENTS.md (yet)."""
    rule = project_dir / ".continue" / "rules" / "codeprism.md"
    rule.parent.mkdir(parents=True, exist_ok=True)
    body = _AGENTS_MD_BLOCK.split("\n", 1)[1].rsplit("\n", 1)[0]  # drop HTML markers
    rule.write_text(
        "---\nname: CodePrism knowledge graph\nalwaysApply: true\n---\n\n" + body + "\n",
        encoding="utf-8",
    )
    return rule


def _write_continue_config(
    server_entry: dict, global_: bool, project_dir: Path | None = None
) -> None:
    import json

    project_dir = project_dir or Path.cwd()
    # Continue.dev only has a global config; warn if --global not passed
    config_file = Path.home() / ".continue" / "config.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if config_file.exists():
        try:
            existing = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    servers = existing.setdefault("mcpServers", {})
    servers["codeprism"] = server_entry
    config_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    _print_instructions_written(_write_continue_rule(project_dir))

    console.print(f"[green]Done.[/green] CodePrism MCP server added to [bold]{config_file}[/bold].")
    console.print("[dim]Reload the Continue extension to pick up the change.[/dim]")


def _write_zed_config(server_entry: dict, global_: bool, project_dir: Path | None = None) -> None:
    import json

    project_dir = project_dir or Path.cwd()
    if global_:
        config_file = Path.home() / ".config" / "zed" / "settings.json"
    else:
        config_file = project_dir / ".zed" / "settings.json"

    config_file.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if config_file.exists():
        try:
            existing = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Zed uses context_servers, not mcpServers
    ctx_servers = existing.setdefault("context_servers", {})
    ctx_servers["codeprism"] = {
        "command": {
            "path": server_entry["command"],
            "args": server_entry["args"],
        }
    }
    config_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    _print_instructions_written(_write_agents_md(project_dir))

    scope = "global (~/.config/zed/)" if global_ else "project (.zed/)"
    console.print(
        f"[green]Done.[/green] CodePrism context server added to [bold]{config_file}[/bold] ({scope})."
    )
    console.print("[dim]Restart Zed to pick up the change.[/dim]")


def _write_agents_md(project_dir: Path) -> Path:
    """Write the shared CodePrism guide to AGENTS.md (read by most agents)."""
    agents_md = project_dir / "AGENTS.md"
    _upsert_agent_instructions(agents_md, _AGENTS_MD_BLOCK, _CODEPRISM_MARKER)
    return agents_md


def _write_claude_md(project_dir: Path) -> Path:
    """Thin CLAUDE.md that imports AGENTS.md (skip the import if one already exists)."""
    import re

    claude_md = project_dir / "CLAUDE.md"
    existing = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
    closing = _CODEPRISM_MARKER.replace("<!-- ", "<!-- /", 1)
    outside_block = re.sub(
        re.escape(_CODEPRISM_MARKER) + r".*?" + re.escape(closing), "", existing, flags=re.DOTALL
    )
    already_imports = re.search(r"(?m)^\s*@AGENTS\.md\s*$", outside_block) is not None
    block = _CLAUDE_MD_BLOCK_NO_IMPORT if already_imports else _CLAUDE_MD_BLOCK
    _upsert_agent_instructions(claude_md, block, _CODEPRISM_MARKER)
    return claude_md


def _print_instructions_written(*files: Path) -> None:
    for f in files:
        console.print(
            f"[green]Done.[/green] Usage instructions written to [bold]{f.resolve()}[/bold]."
        )


def _upsert_agent_instructions(file: Path, block: str, marker: str) -> None:
    """Insert or replace the CodePrism block inside an existing instructions file."""
    if not file.exists():
        file.write_text(block + "\n", encoding="utf-8")
        return

    import re

    existing = file.read_text(encoding="utf-8")
    if marker in existing:
        # Derive closing tag: "<!-- foo -->" → "<!-- /foo -->" (space before slash)
        closing = marker.replace("<!-- ", "<!-- /", 1)
        pattern = re.compile(
            re.escape(marker) + r".*?" + re.escape(closing),
            re.DOTALL,
        )
        if pattern.search(existing):
            # Block with open+close markers found — replace it in place.
            updated = pattern.sub(block, existing, count=1)
        else:
            # Opening marker present but closing tag missing — append updated block.
            updated = existing.rstrip() + "\n\n" + block + "\n"
        file.write_text(updated, encoding="utf-8")
    else:
        # No marker at all — append to the end.
        file.write_text(existing.rstrip() + "\n\n" + block + "\n", encoding="utf-8")


# ── visualize ─────────────────────────────────────────────────────────────────


@app.command()
def visualize(
    path: str = typer.Argument(..., help="Indexed project directory"),
    out: str = typer.Option("graph.html", "--out", "-o", help="Output HTML file"),
) -> None:
    """Generate a self-contained interactive graph visualization (opens in any browser)."""
    asyncio.run(_visualize(path, out))


async def _visualize(path: str, out: str) -> None:
    import json as _json

    from .core.graph import GraphEngine
    from .core.paths import get_db_path
    from .core.storage import StorageManager

    db_path = get_db_path(path)
    storage = StorageManager(db_path)
    await storage.initialize()
    graph = GraphEngine()
    await graph.load_from_storage(storage)
    await storage.close()

    raw = graph.to_json()
    nodes = raw["nodes"]
    edges = raw["edges"]

    _KIND = {
        "NodeKind.FILE": "file",
        "NodeKind.FUNCTION": "function",
        "NodeKind.CLASS": "class",
        "NodeKind.VARIABLE": "variable",
        "NodeKind.IMPORT": "import",
    }
    _EKIND = {
        "EdgeKind.CALLS": "calls",
        "EdgeKind.IMPORTS": "imports",
        "EdgeKind.INHERITS": "inherits",
        "EdgeKind.CONTAINS": "contains",
    }

    node_list = []
    for n in nodes:
        nd = dict(n)
        kind = _KIND.get(nd.get("kind", ""), nd.get("kind", ""))
        label = Path(nd["name"]).name if kind == "file" else nd.get("name", "")
        node_list.append(
            {
                "id": nd["id"],
                "name": nd.get("name", ""),
                "label": label,
                "kind": kind,
                "file": nd.get("file", ""),
                "line": nd.get("line", 0),
            }
        )

    link_list = []
    for e in edges:
        ed = dict(e)
        kind = _EKIND.get(ed.get("kind", ""), ed.get("kind", ""))
        link_list.append(
            {
                "source": ed.get("source", ""),
                "target": ed.get("target", ""),
                "kind": kind,
            }
        )

    graph_data = {"nodes": node_list, "links": link_list}
    data_json = _json.dumps(graph_data, separators=(",", ":")).replace("</", "<\\/")

    html = _VIZ_HTML_TEMPLATE.replace("__DATA__", data_json).replace("__TITLE__", Path(path).name)
    out_path = Path(out)
    if out_path.suffix.lower() != ".html":
        console.print("[red]--out must have a .html extension[/red]")
        raise typer.Exit(1)
    out_path.write_text(html, encoding="utf-8")

    console.print(f"[green]Visualization saved:[/green] [bold]{out_path.resolve()}[/bold]")
    console.print(
        f"[dim]{len(node_list)} nodes, {len(link_list)} edges. Open in any browser.[/dim]"
    )
    if len(node_list) > 2000:
        console.print(
            "[yellow]Large graph (>2000 nodes) — 'Files' or 'Symbols' view recommended.[/yellow]"
        )


_VIZ_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>CodePrism — __TITLE__</title>
<style>
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#020818;--surface:rgba(6,12,32,0.94);--border:rgba(255,255,255,0.07);
  --text:#e2e8f0;--dim:#475569;--dim2:#334155;
  --cyan:#22d3ee;--violet:#a78bfa;--green:#4ade80;--orange:#fb923c;
  --red:#f87171;--blue:#60a5fa;--purple:#c084fc;
}
html,body{height:100%;overflow:hidden}
body{
  background:var(--bg);
  font-family:'Segoe UI',system-ui,-apple-system,BlinkMacSystemFont,sans-serif;
  color:var(--text);font-size:13px;line-height:1.5;
}
body::before{
  content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(ellipse 70% 50% at 15% 65%,rgba(34,211,238,.055) 0%,transparent 100%),
    radial-gradient(ellipse 60% 45% at 85% 20%,rgba(167,139,250,.055) 0%,transparent 100%),
    radial-gradient(ellipse 40% 35% at 60% 85%,rgba(74,222,128,.025) 0%,transparent 100%);
}
#tb{
  position:fixed;top:0;left:0;right:0;z-index:50;height:50px;
  display:flex;align-items:center;gap:10px;padding:0 18px;
  background:rgba(2,8,24,0.88);backdrop-filter:blur(24px) saturate(160%);
  border-bottom:1px solid var(--border);
  box-shadow:0 1px 0 rgba(34,211,238,.07),0 4px 32px rgba(0,0,0,.5);
}
.tdiv{width:1px;height:22px;background:var(--border);flex-shrink:0;margin:0 2px}
#logo{display:flex;align-items:center;gap:9px;flex-shrink:0;margin-right:2px}
#logo-mark{
  width:30px;height:30px;border-radius:8px;flex-shrink:0;
  background:linear-gradient(135deg,var(--cyan) 0%,var(--violet) 100%);
  display:flex;align-items:center;justify-content:center;
  font-weight:900;font-size:13px;color:#fff;letter-spacing:-.5px;
  box-shadow:0 0 14px rgba(34,211,238,.35),0 2px 8px rgba(0,0,0,.4);
}
#logo-name{
  font-weight:700;font-size:14px;letter-spacing:.02em;
  background:linear-gradient(90deg,var(--cyan),var(--violet));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}
#proj-name{font-size:11px;color:var(--dim);font-weight:400;flex-shrink:0}
#views{
  display:flex;gap:2px;padding:3px;
  background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:999px;
}
.seg{
  border:none;border-radius:999px;padding:4px 13px;cursor:pointer;
  font-size:11px;font-weight:500;letter-spacing:.02em;color:var(--dim);
  background:transparent;transition:all .2s;
  display:flex;align-items:center;gap:5px;font-family:inherit;
}
.seg:hover{color:var(--cyan);background:rgba(34,211,238,.07)}
.seg.active{
  color:#fff;font-weight:600;
  background:linear-gradient(135deg,rgba(34,211,238,.18),rgba(167,139,250,.18));
  box-shadow:0 0 0 1px rgba(34,211,238,.45),0 2px 12px rgba(34,211,238,.12);
}
.badge{
  font-size:9px;font-weight:700;letter-spacing:.01em;
  padding:1px 6px;border-radius:999px;min-width:22px;text-align:center;
  background:rgba(255,255,255,.06);color:var(--dim);font-variant-numeric:tabular-nums;
}
.seg.active .badge{background:rgba(34,211,238,.18);color:var(--cyan)}
#sw{position:relative;flex-shrink:0}
#si{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--dim);font-size:12px;pointer-events:none}
#search{
  background:rgba(255,255,255,.04);border:1px solid var(--border);
  color:var(--text);padding:5px 10px 5px 30px;
  border-radius:999px;font-size:11px;width:175px;outline:none;font-family:inherit;
  transition:all .22s;
}
#search::placeholder{color:var(--dim2)}
#search:focus{
  border-color:rgba(34,211,238,.45);background:rgba(34,211,238,.04);
  width:210px;box-shadow:0 0 0 3px rgba(34,211,238,.08);
}
#stats{
  font-size:10px;color:var(--dim);white-space:nowrap;
  padding:3px 10px;border-radius:999px;border:1px solid var(--border);
  background:rgba(255,255,255,.02);font-variant-numeric:tabular-nums;
}
#spin-btn{
  margin-left:auto;display:flex;align-items:center;gap:7px;
  cursor:pointer;padding:5px 13px;border-radius:999px;
  border:1px solid var(--border);background:rgba(255,255,255,.03);
  transition:all .22s;user-select:none;flex-shrink:0;
}
#spin-btn:hover{border-color:rgba(34,211,238,.35);background:rgba(34,211,238,.07)}
#spin-btn.on{border-color:rgba(34,211,238,.55);background:rgba(34,211,238,.1)}
#spin-pip{
  width:7px;height:7px;border-radius:50%;
  background:var(--dim2);transition:all .3s;flex-shrink:0;
}
#spin-btn.on #spin-pip{
  background:var(--cyan);
  box-shadow:0 0 7px var(--cyan),0 0 14px rgba(34,211,238,.4);
  animation:pip-pulse 1.8s ease-in-out infinite;
}
@keyframes pip-pulse{0%,100%{opacity:1}50%{opacity:.45}}
#spin-lbl{font-size:11px;color:var(--dim);font-weight:500;transition:color .2s}
#spin-btn.on #spin-lbl{color:var(--cyan)}
#panel{
  position:fixed;right:0;top:0;bottom:0;width:265px;z-index:45;
  background:rgba(3,8,24,0.97);backdrop-filter:blur(28px) saturate(150%);
  border-left:1px solid var(--border);
  display:flex;flex-direction:column;
  transform:translateX(100%);transition:transform .28s cubic-bezier(.4,0,.2,1);
  box-shadow:-12px 0 40px rgba(0,0,0,.5);
}
#panel.open{transform:translateX(0)}
#ph{
  padding:56px 16px 16px;border-bottom:1px solid var(--border);
  position:relative;flex-shrink:0;
}
#pclose{
  position:absolute;top:12px;right:12px;cursor:pointer;
  width:26px;height:26px;display:flex;align-items:center;justify-content:center;
  border-radius:7px;color:var(--dim);font-size:16px;
  border:1px solid transparent;transition:all .15s;
}
#pclose:hover{color:var(--cyan);background:rgba(34,211,238,.1);border-color:rgba(34,211,238,.25)}
#p-chip{
  display:inline-flex;align-items:center;gap:5px;
  padding:3px 10px;border-radius:999px;font-size:9px;font-weight:700;
  letter-spacing:.1em;text-transform:uppercase;margin-bottom:10px;
}
#p-chip-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
#p-name{font-size:15px;font-weight:700;color:var(--text);line-height:1.3;word-break:break-word}
#pb{padding:14px 16px;overflow-y:auto;flex:1}
.pr{
  margin-bottom:9px;padding:9px 11px;border-radius:9px;
  background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.05);
  transition:background .15s;
}
.pr:hover{background:rgba(255,255,255,.05)}
.pk{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--dim);margin-bottom:3px}
.pv{font-size:11px;color:#cbd5e1;line-height:1.4;word-break:break-all}
.pv.mono{font-family:'Cascadia Code','Fira Code',Consolas,monospace;font-size:10.5px}
#legend{
  position:fixed;bottom:16px;left:16px;z-index:45;
  background:rgba(3,8,24,0.88);backdrop-filter:blur(20px);
  border:1px solid var(--border);border-radius:12px;padding:13px 15px;
  box-shadow:0 8px 32px rgba(0,0,0,.4);
}
.lg-hd{font-size:8px;font-weight:700;text-transform:uppercase;letter-spacing:.14em;color:var(--dim2);margin-bottom:8px}
.lg-r{display:flex;align-items:center;gap:8px;font-size:10px;color:var(--dim);margin-bottom:6px}
.lg-r:last-child{margin-bottom:0}
.lg-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.lg-line{width:20px;height:2px;border-radius:1px;flex-shrink:0}
.lg-sep{height:1px;background:var(--border);margin:10px 0 9px}
#graph{width:100vw;height:100vh;display:block}
</style>
</head>
<body>
<div id="tb">
  <div id="logo">
    <div id="logo-mark">CP</div>
    <span id="logo-name">CodePrism</span>
  </div>
  <div class="tdiv"></div>
  <span id="proj-name">__TITLE__</span>
  <div class="tdiv"></div>
  <div id="views">
    <button class="seg active" id="btn-files" onclick="setView('files')">Files<span class="badge" id="b-files">—</span></button>
    <button class="seg" id="btn-symbols" onclick="setView('symbols')">Symbols<span class="badge" id="b-symbols">—</span></button>
    <button class="seg" id="btn-all" onclick="setView('all')">All<span class="badge" id="b-all">—</span></button>
  </div>
  <div id="sw"><span id="si">&#x2315;</span><input id="search" placeholder="Search nodes..." oninput="doSearch(this.value)"/></div>
  <span id="stats">loading...</span>
  <div id="spin-btn" class="on" onclick="toggleSpin()">
    <div id="spin-pip"></div><span id="spin-lbl">Spinning</span>
  </div>
</div>
<div id="panel">
  <div id="ph">
    <span id="pclose" onclick="closePanel()">&times;</span>
    <div id="p-chip"><div id="p-chip-dot"></div><span id="p-chip-txt"></span></div>
    <div id="p-name"></div>
  </div>
  <div id="pb"></div>
</div>
<div id="legend">
  <div class="lg-hd">Nodes</div>
  <div class="lg-r"><div class="lg-dot" style="background:#22d3ee;box-shadow:0 0 6px rgba(34,211,238,.6)"></div>File</div>
  <div class="lg-r"><div class="lg-dot" style="background:#a78bfa;box-shadow:0 0 6px rgba(167,139,250,.6)"></div>Class</div>
  <div class="lg-r"><div class="lg-dot" style="background:#4ade80;box-shadow:0 0 6px rgba(74,222,128,.6)"></div>Function</div>
  <div class="lg-r"><div class="lg-dot" style="background:#fb923c;box-shadow:0 0 6px rgba(251,146,60,.6)"></div>Variable</div>
  <div class="lg-sep"></div>
  <div class="lg-hd">Edges</div>
  <div class="lg-r"><div class="lg-line" style="background:#f87171"></div>calls</div>
  <div class="lg-r"><div class="lg-line" style="background:#60a5fa"></div>imports</div>
  <div class="lg-r"><div class="lg-line" style="background:#c084fc"></div>inherits</div>
</div>
<div id="graph"></div>
<script src="https://cdn.jsdelivr.net/npm/3d-force-graph@1/dist/3d-force-graph.min.js"></script>
<script>
var RAW=__DATA__;

var NC={file:'#22d3ee',class:'#a78bfa',function:'#4ade80',variable:'#fb923c',import:'#1e293b'};
var NS={file:8,class:5,function:2.5,variable:2,import:1.2};
var LC={calls:'#f87171',imports:'#60a5fa',inherits:'#c084fc',contains:'#1e293b'};
var PC={calls:'#ff6080',imports:'#22d3ee',inherits:'#c084fc'};

var nodeById={};
RAW.nodes.forEach(function(n){nodeById[n.id]=Object.assign({},n);});

var fCt=RAW.nodes.filter(function(n){return n.kind==='file';}).length;
var sCt=RAW.nodes.filter(function(n){return n.kind==='class'||n.kind==='function';}).length;
var aCt=RAW.nodes.length;
function fmt(n){return n>999?(n/1000).toFixed(1)+'k':String(n);}
document.getElementById('b-files').textContent=fmt(fCt);
document.getElementById('b-symbols').textContent=fmt(sCt);
document.getElementById('b-all').textContent=fmt(aCt);

var hiSet=new Set();
function nc(n){if(hiSet.size&&!hiSet.has(n.id))return '#0c1122';return NC[n.kind]||'#334155';}
function nv(n){var b=NS[n.kind]||2;return hiSet.size&&hiSet.has(n.id)?b*3:b;}

function viewData(view){
  var nOk,eOk;
  if(view==='files'){
    nOk=function(n){return n.kind==='file';};
    eOk=function(l){return l.kind==='imports';};
  }else if(view==='symbols'){
    nOk=function(n){return n.kind==='class'||n.kind==='function';};
    eOk=function(l){return l.kind==='calls'||l.kind==='inherits';};
  }else{nOk=function(){return true;};eOk=function(){return true;};}
  var ids=new Set();
  var nodes=RAW.nodes.filter(function(n){if(nOk(n)){ids.add(n.id);return true;}return false;})
    .map(function(n){return nodeById[n.id];});
  var links=RAW.links.filter(function(l){return eOk(l)&&ids.has(l.source)&&ids.has(l.target);})
    .map(function(l){return{source:l.source,target:l.target,kind:l.kind};});
  return{nodes:nodes,links:links};
}

var Graph,curView='files',spinning=true,spinRAF=null,spinAngle=0;

function spinFrame(){
  if(!spinning){spinRAF=null;return;}
  spinAngle+=0.003;
  var p=Graph.camera().position;
  var r=Math.sqrt(p.x*p.x+p.z*p.z);
  if(r<10)r=400;
  Graph.cameraPosition({x:r*Math.sin(spinAngle),z:r*Math.cos(spinAngle)});
  spinRAF=requestAnimationFrame(spinFrame);
}

function setSpin(on){
  spinning=on;
  var btn=document.getElementById('spin-btn');
  var lbl=document.getElementById('spin-lbl');
  if(on){
    btn.classList.add('on');lbl.textContent='Spinning';
    var p=Graph.camera().position;
    spinAngle=Math.atan2(p.x,p.z);
    if(!spinRAF)spinFrame();
  }else{
    btn.classList.remove('on');lbl.textContent='Paused';
  }
}
function toggleSpin(){setSpin(!spinning);}

function initGraph(){
  Graph=ForceGraph3D()(document.getElementById('graph'))
    .backgroundColor('#020818')
    .nodeId('id')
    .nodeLabel(function(n){
      var c=NC[n.kind]||'#94a3b8';
      return '<div style="background:rgba(3,8,28,0.96);border:1px solid rgba(255,255,255,0.1);border-left:3px solid '+c+';border-radius:0 8px 8px 0;padding:8px 12px;min-width:140px;box-shadow:0 8px 32px rgba(0,0,0,.6)">'
        +'<div style="font-size:12px;font-weight:700;color:'+c+';margin-bottom:3px">'+esc(n.label||n.name||'')+'</div>'
        +'<div style="font-size:10px;color:#475569;text-transform:uppercase;letter-spacing:.06em">'+esc(n.kind)+'</div>'
        +'</div>';
    })
    .nodeColor(nc).nodeVal(nv).nodeOpacity(0.92)
    .linkColor(function(l){return LC[l.kind]||'#1e293b';})
    .linkWidth(1.5).linkOpacity(0.75)
    .linkDirectionalArrowLength(5).linkDirectionalArrowRelPos(1)
    .linkDirectionalArrowColor(function(l){return LC[l.kind]||'#1e293b';})
    .linkDirectionalParticles(function(l){
      return l.kind==='calls'?5:l.kind==='imports'?3:l.kind==='inherits'?3:0;
    })
    .linkDirectionalParticleSpeed(0.006)
    .linkDirectionalParticleWidth(2.5)
    .linkDirectionalParticleColor(function(l){return PC[l.kind]||'#fff';})
    .onNodeClick(openPanel)
    .onBackgroundClick(closePanel);

  Graph.d3Force('charge').strength(-120);
  Graph.d3Force('link').distance(40);

  setView('files');
  // Start spin after two frames so camera is positioned
  requestAnimationFrame(function(){requestAnimationFrame(function(){setSpin(true);});});
}

function setView(v){
  curView=v;hiSet.clear();
  document.getElementById('search').value='';
  ['files','symbols','all'].forEach(function(n){
    document.getElementById('btn-'+n).classList.toggle('active',n===v);
  });
  var d=viewData(v);
  Graph.graphData(d);
  document.getElementById('stats').textContent=d.nodes.length+' nodes  ·  '+d.links.length+' edges';
}

function doSearch(q){
  hiSet.clear();
  if(q.trim()){
    var ql=q.toLowerCase();
    Graph.graphData().nodes.forEach(function(n){
      if((n.label||n.name||'').toLowerCase().indexOf(ql)>=0)hiSet.add(n.id);
    });
  }
  Graph.nodeColor(nc).nodeVal(nv);
}

function openPanel(node){
  var c=NC[node.kind]||'#64748b';
  var dot=document.getElementById('p-chip-dot');
  dot.style.background=c;dot.style.boxShadow='0 0 6px '+c;
  document.getElementById('p-chip-txt').textContent=node.kind.toUpperCase();
  var chip=document.getElementById('p-chip');
  chip.style.cssText='display:inline-flex;align-items:center;gap:5px;padding:3px 10px;'
    +'border-radius:999px;font-size:9px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;'
    +'margin-bottom:10px;background:'+c+'1a;color:'+c+';border:1px solid '+c+'44';
  document.getElementById('p-name').textContent=node.name||node.label||'';
  var rows='';
  if(node.file){
    var short=(node.file||'').split('/').pop().split('\\\\').pop()||node.file;
    rows+='<div class="pr"><div class="pk">File</div><div class="pv mono">'+esc(short)+'</div></div>';
  }
  if(node.line)rows+='<div class="pr"><div class="pk">Line</div><div class="pv mono">'+node.line+'</div></div>';
  if(node.name&&node.name!==node.label)
    rows+='<div class="pr"><div class="pk">Full name</div><div class="pv mono">'+esc(node.name)+'</div></div>';
  document.getElementById('pb').innerHTML=rows||
    '<div style="color:var(--dim2);font-size:11px;text-align:center;padding:20px 0">No additional info</div>';
  document.getElementById('panel').classList.add('open');
}
function closePanel(){document.getElementById('panel').classList.remove('open');}

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

initGraph();
</script>
</body>
</html>"""


# ── scan ──────────────────────────────────────────────────────────────────────


@app.command()
def scan(
    target: str = typer.Argument(..., help="File path to scan"),
    all_: bool = typer.Option(False, "--all", "-a", help="Scan all indexed files"),
    diff: str | None = typer.Option(
        None,
        "--diff",
        help="Git diff range to scan, e.g. HEAD~1..HEAD or main..feature",
    ),
    project: str = typer.Option(".", "--project", "-p", help="Project path (for --all)"),
) -> None:
    """Run security detectors on a file (or all indexed files with --all).

    Examples:
        codeprism scan payments/processor.py
        codeprism scan --all --project /path/to/repo
        codeprism scan . --diff HEAD~1..HEAD
    """
    asyncio.run(_scan(target, all_, diff, project))


async def _scan(target: str, all_: bool, diff: str | None, project: str) -> None:
    from .security.scanner import SecurityScanner

    scanner = SecurityScanner()

    if diff:
        await _scan_git_diff(diff, scanner)
        return

    if all_:
        engine, storage = await _open_session(project)
        try:
            fm = await engine.get_file_map(project)
        finally:
            await storage.close()

        total_issues = 0
        for entry in fm.entries:
            try:
                content = Path(entry.path).read_text(encoding="utf-8")
            except Exception:
                continue
            report = scanner.scan_content(content, entry.path)
            if report.issues:
                total_issues += len(report.issues)
                _print_scan_report(report, entry.path)

        console.print(
            f"\n[bold]Scan complete.[/bold] "
            f"{len(fm.entries)} files · {total_issues} issue(s) found."
        )
        return

    # Single file scan
    try:
        content = Path(target).read_text(encoding="utf-8")
    except FileNotFoundError:
        console.print(f"[red]File not found:[/red] {target}")
        raise typer.Exit(1) from None

    report = scanner.scan_content(content, target)
    _print_scan_report(report, target)

    if report.is_blocked:
        raise typer.Exit(2)


async def _scan_git_diff(diff_range: str, scanner) -> None:
    """Scan only the files changed in a git diff range (e.g. HEAD~1..HEAD)."""
    import subprocess

    if not _SAFE_GIT_REF_RE.match(diff_range):
        console.print(
            f"[red]Invalid diff range:[/red] {diff_range!r} — "
            "only alphanumeric and standard git ref characters are allowed."
        )
        raise typer.Exit(1)

    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", diff_range],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        console.print(f"[red]git diff failed:[/red] {exc.stderr.strip()}")
        raise typer.Exit(1) from exc

    changed_files = [f.strip() for f in proc.stdout.splitlines() if f.strip()]
    if not changed_files:
        console.print(f"[dim]No changed files in {diff_range}[/dim]")
        return

    console.print(
        f"Scanning [bold]{len(changed_files)}[/bold] changed file(s) in [bold]{diff_range}[/bold]"
    )

    total_issues = 0
    blocked = False
    for rel_path in changed_files:
        fp = Path(rel_path)
        if not fp.exists():
            continue
        try:
            after_content = fp.read_text(encoding="utf-8")
        except Exception:
            continue

        # Retrieve the content before the diff so we only report new issues.
        before_ref = diff_range.split("..")[0] if ".." in diff_range else diff_range + "~1"
        try:
            before_proc = subprocess.run(
                ["git", "show", f"{before_ref}:{rel_path}"],
                capture_output=True,
                text=True,
            )
            before_content = before_proc.stdout if before_proc.returncode == 0 else ""
        except Exception:
            before_content = ""

        report = scanner.scan_diff(before_content, after_content, str(fp))
        if report.issues:
            total_issues += len(report.issues)
            _print_scan_report(report, str(fp))
            if report.is_blocked:
                blocked = True

    console.print(
        f"\n[bold]Diff scan complete.[/bold] "
        f"{len(changed_files)} files · {total_issues} new issue(s)."
    )
    if blocked:
        raise typer.Exit(2)


def _print_scan_report(report, file_path: str) -> None:
    from rich.table import Table

    severity_colour = {"BLOCK": "bright_red", "WARN": "yellow", "INFO": "blue"}
    status_colour = {"BLOCK": "bright_red", "WARN": "yellow", "PASS": "green"}
    colour = status_colour.get(report.status, "white")

    console.print(
        f"\n[bold]{Path(file_path).name}[/bold]  "
        f"[{colour}]{report.status}[/{colour}]  "
        f"({len(report.issues)} issue(s))"
    )

    if not report.issues:
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Line", justify="right", width=6)
    table.add_column("Sev", width=7)
    table.add_column("Category", width=14)
    table.add_column("Description")

    for issue in report.issues:
        sev_col = severity_colour.get(issue.severity, "white")
        table.add_row(
            str(issue.line_number or ""),
            f"[{sev_col}]{issue.severity}[/{sev_col}]",
            issue.category,
            issue.description,
        )
    console.print(table)
