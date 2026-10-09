"""Serialization helpers that convert query result objects to MCP-friendly dicts."""

from __future__ import annotations

from typing import Any

from ..core.models import SymbolRecord
from ..query.context import ContextResult
from ..query.engine import (
    DataFlowResult,
    DependencyResult,
    DependentResult,
    FileMap,
    SearchMatch,
)
from ..query.impact import ImpactResult
from ..query.summary import ModuleSummary

# ── Atom serializers ──────────────────────────────────────────────────────────


def _sym(s: SymbolRecord) -> dict[str, Any]:
    return {
        "id": s.id,
        "name": s.name,
        "kind": s.kind.value,
        "file_id": s.file_id,
        "line_start": s.line_start,
        "line_end": s.line_end,
        "signature": s.signature,
        "docstring": s.docstring,
        "is_async": s.is_async,
        "is_public": s.is_public,
        "complexity_score": s.complexity_score,
    }


# ── Result serializers ────────────────────────────────────────────────────────


def context_to_dict(r: ContextResult) -> dict[str, Any]:
    return {
        "symbol": _sym(r.symbol),
        "file": {"id": r.file.id, "path": r.file.path, "language": r.file.language},
        "direct_callers": [_sym(s) for s in r.direct_callers],
        "direct_callees": [_sym(s) for s in r.direct_callees],
        "related_types": [_sym(s) for s in r.related_types],
        "relevant_variables": [_sym(s) for s in r.relevant_variables],
        "estimated_token_count": r.estimated_token_count,
    }


def impact_to_dict(r: ImpactResult) -> dict[str, Any]:
    return {
        "symbol": _sym(r.symbol),
        "direct_dependents": [_sym(s) for s in r.direct_dependents],
        "transitive_dependents": [_sym(s) for s in r.transitive_dependents],
        "severity": r.severity,
        "affected_test_files": r.affected_test_files,
        "public_api_affected": r.public_api_affected,
        "estimated_change_surface": r.estimated_change_surface,
    }


def summary_to_dict(r: ModuleSummary) -> dict[str, Any]:
    return {
        "file": {"id": r.file.id, "path": r.file.path, "language": r.file.language},
        "purpose": r.purpose,
        "public_api": [_sym(s) for s in r.public_api],
        "dependencies": r.dependencies,
        "complexity_score": r.complexity_score,
        "test_coverage_file": r.test_coverage_file,
        "key_classes": [_sym(s) for s in r.key_classes],
    }


def file_map_to_dict(r: FileMap) -> dict[str, Any]:
    return {
        "project_path": r.project_path,
        "total_files": r.total_files,
        "total_symbols": r.total_symbols,
        "limit": r.limit,
        "offset": r.offset,
        "truncated": r.truncated,
        "files": [
            {
                "path": e.path,
                "language": e.language,
                "line_count": e.line_count,
                "symbol_count": e.symbol_count,
                "class_count": e.class_count,
                "function_count": e.function_count,
                "role_summary": e.role_summary,
            }
            for e in r.entries
        ],
    }


def search_matches_to_dict(matches: list[SearchMatch]) -> dict[str, Any]:
    return {
        "count": len(matches),
        "matches": [
            {
                "name": m.symbol.name,
                "kind": m.symbol.kind.value,
                "file_path": m.file_path,
                "line_start": m.symbol.line_start,
                "signature": m.symbol.signature,
                "docstring_excerpt": m.docstring_excerpt,
                "score": m.score,
            }
            for m in matches
        ],
    }


def deps_to_dict(r: DependencyResult) -> dict[str, Any]:
    return {
        "file_path": r.file_path,
        "internal_deps": r.internal_deps,
        "external_deps": r.external_deps,
        "circular_deps": r.circular_deps,
    }


def dependents_to_dict(r: DependentResult) -> dict[str, Any]:
    return {"file_path": r.file_path, "dependents": r.dependents}


def data_flow_to_dict(r: DataFlowResult) -> dict[str, Any]:
    return {
        "symbol": _sym(r.symbol),
        "sources": [_sym(s) for s in r.sources],
        "sinks": [_sym(s) for s in r.sinks],
        "intermediate_nodes": [_sym(s) for s in r.intermediate_nodes],
        "flow_paths": r.flow_paths,
    }
