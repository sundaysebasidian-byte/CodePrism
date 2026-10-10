"""QueryEngine — single entry point for all graph queries."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.graph import GraphEngine
from ..core.models import SYMBOL_KINDS, FileRecord, NodeKind, SymbolRecord
from ..core.storage import DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT, StorageManager
from . import context as _ctx_mod
from . import impact as _imp_mod
from . import summary as _sum_mod
from .context import ContextResult
from .impact import ImpactResult
from .summary import ModuleSummary

# ── Additional result types ───────────────────────────────────────────────────


@dataclass
class SearchMatch:
    symbol: SymbolRecord
    file_path: str
    score: float = 1.0
    docstring_excerpt: str | None = None


@dataclass
class SearchPage:
    """Search matches with a complete substring total, or None for semantic search."""

    matches: list[SearchMatch]
    total: int | None = None


@dataclass
class FileMapEntry:
    path: str
    language: str
    line_count: int
    symbol_count: int
    class_count: int
    function_count: int
    role_summary: str


@dataclass
class FileMap:
    project_path: str
    entries: list[FileMapEntry] = field(default_factory=list)
    total_files: int = 0
    total_symbols: int = 0


@dataclass
class DependencyResult:
    file_path: str
    internal_deps: list[str] = field(default_factory=list)
    external_deps: list[str] = field(default_factory=list)
    circular_deps: list[str] = field(default_factory=list)


@dataclass
class DependentResult:
    file_path: str
    dependents: list[str] = field(default_factory=list)


@dataclass
class DataFlowResult:
    symbol: SymbolRecord
    sources: list[SymbolRecord] = field(default_factory=list)
    sinks: list[SymbolRecord] = field(default_factory=list)
    intermediate_nodes: list[SymbolRecord] = field(default_factory=list)
    flow_paths: list[list[str]] = field(default_factory=list)


# ── Engine ────────────────────────────────────────────────────────────────────


class QueryEngine:
    """Facade over GraphEngine + StorageManager for high-level queries."""

    def __init__(self, graph: GraphEngine, storage: StorageManager) -> None:
        self._graph = graph
        self._storage = storage
        self._embedder: Any | None = None
        self._embed_store: Any | None = None

    def set_embeddings(self, embedder: Any, store: Any) -> None:
        """Inject embeddings components to enable semantic search."""
        self._embedder = embedder
        self._embed_store = store

    # ── Delegation to sub-modules ─────────────────────────────────────────────

    async def get_context(
        self, file_path: str, symbol_name: str, depth: int = 2
    ) -> ContextResult | None:
        return await _ctx_mod.get_context(self._graph, self._storage, file_path, symbol_name, depth)

    async def get_impact(self, file_path: str, symbol_name: str) -> ImpactResult | None:
        return await _imp_mod.get_impact(self._graph, self._storage, file_path, symbol_name)

    async def get_module_summary(self, file_path: str) -> ModuleSummary | None:
        return await _sum_mod.get_module_summary(self._graph, self._storage, file_path)

    # ── Symbol lookup ─────────────────────────────────────────────────────────

    async def find_symbol(self, file_path: str, name: str) -> SymbolRecord | None:
        file = await self._storage.get_file_by_path(file_path)
        if not file:
            return None
        syms = await self._storage.get_symbols_for_file(file.id)
        return _ctx_mod._pick(syms, name)

    async def get_file(self, file_path: str) -> FileRecord | None:
        return await self._storage.get_file_by_path(file_path)

    async def resolve_file(
        self, file: str, project_path: str
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Resolve against the project root, then by suffix, or explain a failed lookup."""
        root = Path(project_path).resolve()
        path = Path(file)
        absolute = str((path if path.is_absolute() else root / path).resolve()) if file else file
        matches = await self._storage.find_files_by_path(absolute)
        if not matches:
            matches = await self._storage.find_files_by_path(file)
        if len(matches) == 1:
            return matches[0].path, None
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

    # ── Callers / callees ─────────────────────────────────────────────────────

    async def get_callers(self, file_path: str, symbol_name: str) -> list[SymbolRecord]:
        sym = await self.find_symbol(file_path, symbol_name)
        if not sym:
            return []
        return self._graph.get_callers(sym.id)

    async def get_callees(self, file_path: str, symbol_name: str) -> list[SymbolRecord]:
        sym = await self.find_symbol(file_path, symbol_name)
        if not sym:
            return []
        return self._graph.get_callees(sym.id)

    # ── Search ────────────────────────────────────────────────────────────────

    async def search_symbols(self, query: str, kind: str | None = None) -> list[SearchMatch]:
        """Return the default search page while preserving the existing list API."""
        return (await self.search_symbols_page(query, kind)).matches

    async def search_symbols_page(
        self,
        query: str,
        kind: str | None = None,
        *,
        limit: int = DEFAULT_SEARCH_LIMIT,
        file_prefix: str | None = None,
    ) -> SearchPage:
        """Search with filtered substring pagination; semantic results keep their cap.

        file_prefix is an already-resolved literal path prefix. limit and total
        apply only to substring mode; the semantic backend still requests 20
        neighbors and supplies no complete total.
        """
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_SEARCH_LIMIT
        ):
            raise ValueError(f"limit must be an integer between 1 and {MAX_SEARCH_LIMIT}")
        if kind is not None:
            normalized = kind.lower()
            if normalized not in SYMBOL_KINDS:
                raise ValueError(f"Unknown kind {kind!r}. Valid kinds: {', '.join(SYMBOL_KINDS)}")
            kind = normalized
        if self._embedder is not None and self._embed_store is not None:
            matches = await self._semantic_search(query, kind)
            if file_prefix is not None:
                matches = [match for match in matches if match.file_path.startswith(file_prefix)]
            return SearchPage(matches)
        raw, total = await self._storage.search_symbols_page(
            query, kind, limit=limit, file_prefix=file_prefix
        )
        all_files = await self._storage.get_all_files()
        id_to_path = {f.id: f.path for f in all_files}
        matches = [
            SearchMatch(
                symbol=sym,
                file_path=id_to_path.get(sym.file_id, ""),
                score=1.0,
                docstring_excerpt=sym.docstring[:120] if sym.docstring else None,
            )
            for sym in raw
        ]
        return SearchPage(matches, total)

    async def _semantic_search(self, query: str, kind: str | None = None) -> list[SearchMatch]:
        import asyncio as _asyncio

        vector = await _asyncio.to_thread(self._embedder.encode_one, query)
        results = self._embed_store.search(vector, top_k=20)
        if kind:
            kind_lower = kind.lower()
            results = [r for r in results if r.metadata.get("kind", "").lower() == kind_lower]
        matches: list[SearchMatch] = []
        for r in results:
            sym = await self._storage.get_symbol_by_id(r.symbol_id)
            if sym:
                matches.append(
                    SearchMatch(
                        symbol=sym,
                        file_path=r.file_path,
                        score=max(0.0, 1.0 - r.distance),
                        docstring_excerpt=sym.docstring[:120] if sym.docstring else None,
                    )
                )
        return matches

    # ── File map ──────────────────────────────────────────────────────────────

    async def get_file_map(self, project_path: str = "") -> FileMap:
        all_files = await self._storage.get_all_files()
        all_syms = await self._storage.get_all_symbols()

        # Build per-file symbol counts
        file_syms: dict[str, list[SymbolRecord]] = {f.id: [] for f in all_files}
        for sym in all_syms:
            if sym.file_id in file_syms:
                file_syms[sym.file_id].append(sym)

        entries: list[FileMapEntry] = []
        for f in sorted(all_files, key=lambda x: x.path):
            syms = file_syms[f.id]
            n_class = sum(1 for s in syms if s.kind == NodeKind.CLASS)
            n_func = sum(1 for s in syms if s.kind == NodeKind.FUNCTION)
            stem = Path(f.path).name
            role = f"{stem}: "
            parts = []
            if n_class:
                parts.append(f"{n_class} class{'es' if n_class > 1 else ''}")
            if n_func:
                parts.append(f"{n_func} function{'s' if n_func > 1 else ''}")
            role += ", ".join(parts) if parts else "source file"
            entries.append(
                FileMapEntry(
                    path=f.path,
                    language=f.language or "",
                    line_count=f.line_count or 0,
                    symbol_count=len(syms),
                    class_count=n_class,
                    function_count=n_func,
                    role_summary=role,
                )
            )

        return FileMap(
            project_path=project_path,
            entries=entries,
            total_files=len(all_files),
            total_symbols=len(all_syms),
        )

    # ── Dependencies ──────────────────────────────────────────────────────────

    async def get_dependencies(self, file_path: str) -> DependencyResult | None:
        file = await self._storage.get_file_by_path(file_path)
        if not file:
            return None

        syms = await self._storage.get_symbols_for_file(file.id)
        import_syms = [s for s in syms if s.kind == NodeKind.IMPORT]

        # Anything with a matching non-import symbol in the graph = internal.
        # Uses a name-only projection query instead of loading all SymbolRecords.
        known_names = await self._storage.get_non_import_symbol_names(file.id)

        # Group by source module (stored in signature since parser v0.1.7).
        # Falls back to symbol name for DBs indexed before the signature fix.
        from collections import defaultdict

        module_symbols: dict[str, list[str]] = defaultdict(list)
        for imp in import_syms:
            source_module = imp.signature or imp.name
            module_symbols[source_module].append(imp.name)

        internal: list[str] = []
        external: list[str] = []
        for source_module, names in module_symbols.items():
            if any(name in known_names for name in names):
                internal.append(source_module)
            else:
                external.append(source_module)

        all_files_map = {f.id: f.path for f in await self._storage.get_all_files()}
        raw_cycles = self._graph.get_import_cycles_for_file(file.id)
        circular: list[str] = []
        for cycle in raw_cycles:
            nodes = [all_files_map.get(fid, fid) for fid in cycle]
            circular.append(" → ".join(nodes + [nodes[0]]))

        return DependencyResult(
            file_path=file_path,
            internal_deps=internal,
            external_deps=external,
            circular_deps=circular,
        )

    async def get_dependents(self, file_path: str) -> DependentResult | None:
        file = await self._storage.get_file_by_path(file_path)
        if not file:
            return None

        syms = await self._storage.get_symbols_for_file(file.id)
        all_files = await self._storage.get_all_files()
        id_to_path = {f.id: f.path for f in all_files}

        dependent_paths: set[str] = set()
        for sym in syms:
            trans = self._graph.get_transitive_dependents(sym.id)
            for tid in trans:
                rec = self._graph.get_symbol(tid)
                if rec:
                    fp = id_to_path.get(rec.file_id, "")
                    if fp and fp != file_path:
                        dependent_paths.add(fp)

        return DependentResult(file_path=file_path, dependents=sorted(dependent_paths))

    # ── Data flow ─────────────────────────────────────────────────────────────

    async def get_data_flow(self, file_path: str, symbol_name: str) -> DataFlowResult | None:
        sym = await self.find_symbol(file_path, symbol_name)
        if not sym:
            return None

        # sources = things that flow INTO this symbol (callers + symbols that define it)
        sources = self._graph.get_callers(sym.id)

        # sinks = things this symbol produces / calls
        sinks = self._graph.get_callees(sym.id)

        # Approximate flow paths (direct 1-hop paths only for now)
        paths: list[list[str]] = []
        for src in sources[:5]:
            paths.append([src.name, sym.name])
        for sink in sinks[:5]:
            paths.append([sym.name, sink.name])

        return DataFlowResult(
            symbol=sym,
            sources=sources[:20],
            sinks=sinks[:20],
            intermediate_nodes=[],
            flow_paths=paths,
        )

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def get_stats(self, path_prefix: str | None = None) -> dict:
        return await self._storage.get_stats(path_prefix=path_prefix)
