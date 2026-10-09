"""FastMCP server — exposes the knowledge graph as MCP tools."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from ..core.models import SYMBOL_KINDS
from ..query.engine import QueryEngine
from .session import SessionManager
from .tools import (
    context_to_dict,
    data_flow_to_dict,
    dependents_to_dict,
    deps_to_dict,
    file_map_to_dict,
    impact_to_dict,
    search_matches_to_dict,
    summary_to_dict,
)

# ── Module-level state ────────────────────────────────────────────────────────

_project_path: str = "."
_auto_index: bool = False
_engine: QueryEngine | None = None
_session_manager: SessionManager | None = None
# Background startup indexing: "off" | "indexing" | "ready" | "error: ..."
_index_status: str = "off"


def configure(project_path: str, auto_index: bool = False) -> None:
    """Call before mcp.run() to point the server at a project directory.

    auto_index: build (first run) or incrementally refresh the index in the
    background once the server is up, so a project never has to be indexed by
    hand and stays in sync between sessions.
    """
    global _project_path, _auto_index
    _project_path = project_path
    _auto_index = auto_index


async def _background_index(graph, storage, cfg) -> None:
    global _index_status
    from ..indexer.project_indexer import ProjectIndexer

    _index_status = "indexing"
    try:
        # parse_workers=0: one parser process per core (the serve entry point is guarded)
        indexer = ProjectIndexer(graph, storage, cfg.model_copy(update={"parse_workers": 0}))
        await indexer.index(_project_path)
        _index_status = "ready"
    except Exception as exc:  # never take the server down over indexing
        _index_status = f"error: {exc}"


def init_engine(engine: QueryEngine) -> None:
    """Directly inject an engine (used in tests or custom embeddings)."""
    global _engine
    _engine = engine


def init_session_manager(manager: SessionManager) -> None:
    """Directly inject a SessionManager (used in tests)."""
    global _session_manager
    _session_manager = manager


def _abs(path: str) -> str:
    """Resolve a tool's file argument against the served project root.

    Agents pass project-relative paths; the server's own working directory can
    be a subfolder, so never resolve against the cwd.
    """
    if not path:
        return path
    p = Path(path)
    return str((p if p.is_absolute() else Path(_project_path) / p).resolve())


async def _resolve_file(file: str) -> tuple[str | None, dict[str, Any] | None]:
    """Map a tool's file argument to the indexed path, or explain why it can't.

    Tries the path under the project root first, then as a relative suffix
    ("utils.py", "api/utils.py") matched against every indexed file.
    """
    storage = _get()._storage
    matches = await storage.find_files_by_path(_abs(file))
    if not matches:
        matches = await storage.find_files_by_path(file)
    if len(matches) == 1:
        return matches[0].path, None
    root = Path(_project_path).resolve()
    if not matches:
        return None, {"error": f"File '{file}' not indexed", "project_path": str(root)}

    def rel(p: str) -> str:
        try:
            return Path(p).relative_to(root).as_posix()
        except ValueError:
            return p

    candidates = sorted(rel(m.path) for m in matches)
    return None, {
        "error": f"Path '{file}' is ambiguous: {len(candidates)} indexed files match",
        "candidates": candidates[:20],
        "hint": f"Pass a longer path, e.g. '{candidates[0]}'",
    }


async def _symbol_refs(symbols) -> list[dict[str, Any]]:
    storage = _get()._storage
    paths: dict[str, str] = {}
    out = []
    for s in symbols:
        if s.file_id not in paths:
            f = await storage.get_file_by_id(s.file_id)
            paths[s.file_id] = f.path if f else ""
        out.append(
            {
                "name": s.name,
                "file": paths[s.file_id],
                "file_id": s.file_id,
                "line_start": s.line_start,
            }
        )
    return out


def _get() -> QueryEngine:
    if _engine is None:
        raise RuntimeError(
            "CodePrism server is not initialized. "
            "Run `codeprism index <path>` then `codeprism serve <path>` first."
        )
    return _engine


def _get_session() -> SessionManager:
    if _session_manager is None:
        raise RuntimeError(
            "SessionManager is not initialized. Run `codeprism serve <path>` to start the server."
        )
    return _session_manager


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[None]:
    global _engine, _session_manager, _index_status
    import asyncio
    import contextlib

    from ..core.graph import GraphEngine
    from ..core.paths import get_db_path
    from ..core.storage import StorageManager
    from ..indexer.incremental_updater import IncrementalUpdater

    db_path = get_db_path(_project_path)
    storage = StorageManager(db_path)
    await storage.initialize()
    graph = GraphEngine()
    await graph.load_from_storage(storage)
    _engine = QueryEngine(graph, storage)
    updater = IncrementalUpdater(graph, storage)
    _session_manager = SessionManager(storage, updater, project_root=_project_path)

    import sys

    from ..core.config import CodePrismConfig
    from ..core.paths import get_project_config_path

    try:
        cfg = CodePrismConfig.load(get_project_config_path(_project_path))
    except Exception as e:
        print(f"CodePrism configuration error: {e}", file=sys.stderr)
        cfg = CodePrismConfig()

    # Wire semantic search if embeddings index exists and is configured
    try:
        from ..core.paths import get_chroma_path

        if cfg.enable_embeddings:
            from ..embeddings.embedder import Embedder
            from ..embeddings.store import EmbeddingStore

            _embedder = Embedder(model_name=cfg.embeddings.model, device=cfg.embeddings.device)
            _embed_store = EmbeddingStore(str(get_chroma_path(_project_path)))
            if _embed_store.count() > 0:
                _engine.set_embeddings(_embedder, _embed_store)
    except Exception:
        pass  # embeddings are optional — never block server startup

    index_task = None
    if _auto_index and cfg.auto_index:
        _index_status = "indexing"  # set before the task runs so stats never say "off"
        index_task = asyncio.create_task(_background_index(graph, storage, cfg))
    else:
        _index_status = "off"

    try:
        yield
    finally:
        if index_task is not None:
            index_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await index_task
        await storage.close()
        _engine = None
        _session_manager = None


# ── FastMCP application ───────────────────────────────────────────────────────

mcp = FastMCP(
    "CodePrism",
    instructions=(
        "CodePrism maintains a persistent knowledge graph of a codebase. "
        "Use get_context to understand a symbol, get_impact to assess change risk, "
        "get_module_summary for a file overview, and search_symbol to locate code. "
        "Use scan_file/scan_diff/check_secret_exposure before writing code. "
        "Use record_read/record_write/undo_write to track and revert session changes."
    ),
    lifespan=_lifespan,
)


# ── Indexing & management ─────────────────────────────────────────────────────


@mcp.tool()
async def index_project(
    path: str,
    languages: list[str] | None = None,
    embeddings: bool = False,
) -> dict[str, Any]:
    """Build or rebuild the knowledge graph for a project directory.

    embeddings: also build the vector index for semantic search (requires codeprism[embeddings]).
    """
    global _engine
    from ..core.config import CodePrismConfig
    from ..core.graph import GraphEngine
    from ..core.paths import get_db_path
    from ..core.storage import StorageManager
    from ..indexer.project_indexer import ProjectIndexer

    # parse_workers=0: one parser process per core (the server entry point is guarded)
    cfg = (
        CodePrismConfig(languages=languages, enable_embeddings=embeddings, parse_workers=0)
        if languages
        else CodePrismConfig(enable_embeddings=embeddings, parse_workers=0)
    )
    db_path = get_db_path(path)
    storage = StorageManager(db_path)
    await storage.initialize()
    graph = GraphEngine()
    indexer = ProjectIndexer(graph, storage, cfg)
    result = await indexer.index(path)

    old_storage = _engine._storage if _engine else None
    _engine = QueryEngine(graph, storage)
    if old_storage is not None and old_storage is not storage:
        await old_storage.close()

    return {
        "file_count": result.file_count,
        "symbol_count": result.symbol_count,
        "edge_count": result.edge_count,
        "duration_seconds": round(result.duration_seconds, 3),
        "errors": result.errors,
        "success": result.success,
    }


@mcp.tool()
async def update_file(path: str) -> dict[str, Any]:
    """Incrementally update the graph for a single changed file."""
    from ..indexer.incremental_updater import IncrementalUpdater

    engine = _get()
    updater = IncrementalUpdater(engine._graph, engine._storage)
    result = await updater.update_file(_abs(path))
    return {
        "nodes_added": result.nodes_added,
        "nodes_removed": result.nodes_removed,
        "edges_updated": result.edges_updated,
        "skipped": result.skipped,
    }


@mcp.tool()
async def get_graph_stats(path: str | None = None) -> dict[str, Any]:
    """Return aggregate statistics about the indexed knowledge graph.

    path: optional project path filter — scopes stats to files under that directory.
    """
    stats = await _get().get_stats(path_prefix=_abs(path) if path else None)
    if path:
        stats["filter_path"] = path
    stats["project_path"] = str(Path(_project_path).resolve())
    # While "indexing", results are partial — the first index of a project
    # runs in the background after the server starts.
    stats["index_status"] = _index_status
    return stats


# ── Context retrieval ─────────────────────────────────────────────────────────


@mcp.tool()
async def get_context(file: str, symbol: str, depth: int = 2) -> dict[str, Any]:
    """Get structured context for a symbol: callers, callees, types, variables.

    depth=1: symbol + direct callers/callees
    depth=2: + their neighbours (recommended)
    depth=3: full transitive neighbourhood
    """
    path, err = await _resolve_file(file)
    if err:
        return err
    result = await _get().get_context(path, symbol, depth)
    if result is None:
        return {"error": f"Symbol '{symbol}' not found in {file}"}
    return context_to_dict(result)


@mcp.tool()
async def get_module_summary(file: str) -> dict[str, Any]:
    """Return a high-level narrative summary of a source file."""
    path, err = await _resolve_file(file)
    if err:
        return err
    result = await _get().get_module_summary(path)
    if result is None:
        return {"error": f"File '{file}' not indexed"}
    return summary_to_dict(result)


@mcp.tool()
async def get_file_map(project_path: str = "", limit: int = 200, offset: int = 0) -> dict[str, Any]:
    """Return a directory's file roles in path order, relative to the served project.

    limit: 1-1000 files (default 200). offset: non-negative number of files to skip.
    Totals describe the entire directory; truncated means this page omits files.
    """
    if limit is None:
        return {"error": "limit must be between 1 and 1000"}
    path = _abs(project_path or ".")
    try:
        result = await _get().get_file_map(path, limit=limit, offset=offset)
    except ValueError as exc:
        return {"error": str(exc), "project_path": path}
    return file_map_to_dict(result)


# ── Impact analysis ───────────────────────────────────────────────────────────


@mcp.tool()
async def get_impact(file: str, symbol: str) -> dict[str, Any]:
    """Transitive impact analysis: what breaks if this symbol changes?"""
    path, err = await _resolve_file(file)
    if err:
        return err
    result = await _get().get_impact(path, symbol)
    if result is None:
        return {"error": f"Symbol '{symbol}' not found in {file}"}
    return impact_to_dict(result)


@mcp.tool()
async def get_callers(file: str, function: str) -> dict[str, Any]:
    """All functions that call this function, with call-site metadata.

    file: path relative to the project root, or absolute.
    """
    path, err = await _resolve_file(file)
    if err:
        return err
    if await _get().find_symbol(path, function) is None:
        return {"error": f"Symbol '{function}' not found in '{file}'"}
    callers = await _get().get_callers(path, function)
    return {
        "function": function,
        "file": file,
        "callers": await _symbol_refs(callers),
        "count": len(callers),
    }


@mcp.tool()
async def get_callees(file: str, function: str) -> dict[str, Any]:
    """All functions called by this function.

    file: path relative to the project root, or absolute.
    """
    path, err = await _resolve_file(file)
    if err:
        return err
    if await _get().find_symbol(path, function) is None:
        return {"error": f"Symbol '{function}' not found in '{file}'"}
    callees = await _get().get_callees(path, function)
    return {
        "function": function,
        "file": file,
        "callees": await _symbol_refs(callees),
        "count": len(callees),
    }


@mcp.tool()
async def get_data_flow(file: str, symbol: str) -> dict[str, Any]:
    """Trace where data from this symbol flows (sources, sinks, paths)."""
    path, err = await _resolve_file(file)
    if err:
        return err
    result = await _get().get_data_flow(path, symbol)
    if result is None:
        return {"error": f"Symbol '{symbol}' not found in {file}"}
    return data_flow_to_dict(result)


# ── Symbol search ─────────────────────────────────────────────────────────────


@mcp.tool(
    description="Find symbols by name (substring match).\n\n"
    "project_path: optional directory prefix to restrict results to a sub-project.\n"
    "kind: case-insensitive symbol kind; one of " + ", ".join(SYMBOL_KINDS)
)
async def search_symbol(
    query: str,
    project_path: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """Find symbols by name (substring match).

    project_path: optional directory prefix to restrict results to a sub-project.
    kind: case-insensitive symbol kind from SYMBOL_KINDS.
    """
    try:
        matches = await _get().search_symbols(query, kind)
    except ValueError as error:
        return {"error": str(error)}
    if project_path:
        prefix = _abs(project_path)
        matches = [m for m in matches if m.file_path.startswith(prefix)]
    return search_matches_to_dict(matches)


@mcp.tool()
async def get_dependencies(file: str) -> dict[str, Any]:
    """All modules/packages this file depends on (internal vs external)."""
    path, err = await _resolve_file(file)
    if err:
        return err
    result = await _get().get_dependencies(path)
    if result is None:
        return {"error": f"File '{file}' not indexed"}
    return deps_to_dict(result)


@mcp.tool()
async def get_dependents(file: str) -> dict[str, Any]:
    """All files that transitively depend on this file."""
    path, err = await _resolve_file(file)
    if err:
        return err
    result = await _get().get_dependents(path)
    if result is None:
        return {"error": f"File '{file}' not indexed"}
    return dependents_to_dict(result)


# ── Security ──────────────────────────────────────────────────────────────────


@mcp.tool()
async def scan_file(file: str, content: str | None = None) -> dict[str, Any]:
    """Run all security detectors on a file.

    content: if provided, scans this proposed content (pre-write check);
             otherwise reads the file from disk.
    Returns: status (PASS/WARN/BLOCK), issues[] with line, severity, fix.
    """
    from ..security.scanner import SecurityScanner

    scanner = SecurityScanner()
    if content is not None:
        report = scanner.scan_content(content, file)
    else:
        try:
            from pathlib import Path

            resolved = Path(_abs(file))
            project_resolved = Path(_project_path).resolve()
            if not str(resolved).startswith(str(project_resolved)):
                return {
                    "error": (
                        f"File '{file}' is outside the indexed project directory. "
                        "Pass content= to scan arbitrary text."
                    )
                }
            file_content = resolved.read_text(encoding="utf-8")
            report = scanner.scan_content(file_content, file)
        except FileNotFoundError:
            return {"error": f"File '{file}' not found"}
    return report.to_dict()


@mcp.tool()
async def scan_diff(original: str, proposed: str, file: str = "") -> dict[str, Any]:
    """Security-diff: only report issues introduced by the change, not pre-existing ones.

    This is the primary tool for the Security Gate — call before any file write.
    Returns: status (PASS/WARN/BLOCK), new_issues[] with line, severity, fix.
    """
    from ..security.scanner import SecurityScanner

    scanner = SecurityScanner()
    report = scanner.scan_diff(original, proposed, file)
    return report.to_dict()


@mcp.tool()
async def check_secret_exposure(content: str) -> dict[str, Any]:
    """Scan content specifically for hardcoded secrets, tokens, and API keys.

    Uses entropy analysis + pattern matching on the secrets detector only.
    Returns: status, secrets_found[] with line and description.
    """
    from ..security.scanner import SecurityScanner

    scanner = SecurityScanner()
    report = scanner.scan_secrets_only(content)
    return {
        "status": report.status,
        "secrets_found": [i.to_dict() for i in report.issues],
        "count": len(report.issues),
    }


@mcp.tool()
async def check_dependencies_cve(requirements: str) -> dict[str, Any]:
    """Check a requirements.txt blob for packages with known CVEs via the OSV API.

    requirements: raw text of requirements.txt (one package per line).
    Returns: vulnerable[] list with package, severity, cve_ids, summary.
    A CRITICAL or HIGH severity finding should be treated as a BLOCK.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from ..security.cve import check_requirements

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:
        results = await loop.run_in_executor(pool, check_requirements, requirements)

    vulnerable = [
        {
            "package": r.package,
            "version": r.version,
            "severity": r.severity,
            "cve_ids": r.cve_ids,
            "summary": r.summary,
        }
        for r in results
    ]
    status = "PASS"
    for r in results:
        if r.severity == "CRITICAL":
            status = "BLOCK"
            break
        if r.severity == "HIGH" and status != "BLOCK":
            status = "WARN"
    return {"status": status, "vulnerable": vulnerable, "count": len(vulnerable)}


# ── Session overlay ───────────────────────────────────────────────────────────


@mcp.tool()
async def record_read(session_id: str, file: str, symbol: str) -> dict[str, Any]:
    """Tell CodePrism the agent has read this symbol in this session.

    Allows get_session_context to return what has already been fetched,
    preventing redundant re-reads across long agent chains.
    """
    await _get_session().record_read(session_id, _abs(file), symbol)
    return {"recorded": True, "session_id": session_id, "file": file, "symbol": symbol}


@mcp.tool()
async def record_write(
    session_id: str,
    file: str,
    content_before: str,
    content_after: str,
) -> dict[str, Any]:
    """Log a file write: run security scan, flush to disk, sync the graph.

    Returns status (PASS/WARN/BLOCK) + graph_update. A BLOCK means the write
    introduced a critical security issue — surface this to the user.
    """
    return await _get_session().record_write(session_id, _abs(file), content_before, content_after)


@mcp.tool()
async def get_session_context(session_id: str) -> dict[str, Any]:
    """What has the agent read and written in this session?

    Returns a compact summary for inclusion in the agent's context window
    instead of re-fetching individual symbols.
    """
    ctx = await _get_session().get_context(session_id)
    return {
        "session_id": ctx.session_id,
        "total_events": ctx.total_events,
        "read_count": ctx.read_count,
        "write_count": ctx.write_count,
        "undo_count": ctx.undo_count,
        "files_read": ctx.files_read,
        "files_written": ctx.files_written,
        "summary": ctx.summary,
    }


@mcp.tool()
async def undo_write(session_id: str, steps: int = 1) -> dict[str, Any]:
    """Restore the last N written files from the session journal.

    Reverses agent-authored writes in reverse chronological order.
    The graph is re-synced for each restored file.
    """
    result = await _get_session().undo_write(session_id, steps)
    return {
        "files_restored": result.files_restored,
        "steps_undone": result.steps_undone,
    }
