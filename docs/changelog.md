# Changelog

All notable changes to CodePrism are documented here.
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Fixed
- CLI `index` and the `index_project` MCP tool now load `.codeprism.toml`, matching the server's
  startup index. Explicit language and embedding options override the file, while omitted options
  preserve it; `index --no-embeddings` can disable configured embeddings. Automatic CLI/MCP parser
  workers and the library's in-process default remain unchanged. (#48)
- CLI `callers`, `context`, `impact` and `summary` distinguish missing files and ambiguous paths,
  report candidates on stderr, and exit with status 1 for lookup errors. `callers` no longer
  reports an unknown symbol as a successful zero-callers result. MCP responses stay unchanged.
  (#44)
- `index --languages` now rejects unknown names with a list of supported languages. Names remain
  case-insensitive, and supported aliases such as `py` and `C++` still work. (#55)
- `stats --verbose` shows project-relative paths and truncates very long paths instead of
  wrapping the file table across multiple lines. (#17)
- `codeprism --version` now exists, and `codeprism.__version__` is read from the installed
  package metadata instead of a hard-coded `0.1.0`. (#54)
- `scan --diff` now rejects ranges beginning with `-`, so Git options such as `--stat` cannot be
  mistaken for a diff range. (#60)
- Reject unknown symbol kinds in CLI and MCP searches instead of returning no matches.
  Both help descriptions now list all searchable kinds; matching remains case-insensitive.
  (#56)

---

## [v0.1.11] — 2026-09-30

> **Upgrading:** existing indexes are rebuilt once automatically (index format 2).

### Fixed
- **Call links follow imports (Python).** Cross-file calls were linked by name alone, so
  `asyncio.run()` could link to your own `run`, `str()` to a project `str`, and a method call to
  a *field* of the same name. Calls now follow the caller's imports (including package
  re-exports and function-local imports); builtins and third-party calls link to nothing; calls
  on objects link only when there is exactly one candidate in a file the caller imports. On a
  new cross-file benchmark, precision went from 0.68-0.96 to **1.00** on requests, flask, httpx
  and CodePrism, with recall equal or better. Other languages still resolve by name. (#33)
- **Gitignored files are no longer indexed** (vendored checkouts, build output). Uses git's own
  file list, with a plain-walk fallback outside git. Opt out with `respect_gitignore = false`.
  (#31)
- **Ambiguous paths are reported, not "not indexed".** A short path matching several files
  (`utils.py`) now returns the candidate paths and a hint. (#32)

### Added
- `benchmarks/run_call_precision.py`: cross-file call accuracy against a Python `ast` oracle.

---

## [v0.1.10] — 2026-09-30

### Fixed
- **Tools now accept project-relative file paths.** Files are stored with absolute paths, so
  `get_callers("src/app.py", ...)`, `get_module_summary`, `get_context`, `search_symbol`'s
  `project_path` and the other file tools silently returned empty results for the relative
  paths agents normally use. Relative paths, `./`, either separator and (on Windows) any case
  now resolve against the served project root; an ambiguous name (two `utils.py`) is never
  guessed. (#30)
- **`get_callers` / `get_callees` report errors** for an unindexed file or unknown symbol instead
  of an empty list, and include each caller's file path, not just its internal id. (#30)
- **No duplicate file records**: `codeprism index .` stored relative paths while the MCP server
  stored absolute ones. Paths are now always absolute; an old relative index heals itself on the
  next index run. (#30)

---

## [v0.1.9] — 2026-09-30

Graph sync and scale fixes, a working Claude Code setup, Codex support, and
AGENTS.md as the shared agent guide.

> **Upgrading:** existing indexes are rebuilt automatically on the next `codeprism index`
> or server start (new index format version), which also clears stale rows left by the
> re-index bugs fixed below. Re-run `codeprism setup <agent>` to move to the new config
> locations and get `AGENTS.md`.

### Added
- **`codeprism setup codex`**: registers `[mcp_servers.codeprism]` in `.codex/config.toml`
  (or `~/.codex/config.toml` with `--global`). Comments and other settings are preserved.
- **`AGENTS.md` is the shared guide.** Every setup writes the CodePrism usage guide to
  `AGENTS.md` (read by Codex, Cursor, Windsurf, Zed, Copilot, ...). `CLAUDE.md` becomes a thin
  `@AGENTS.md` import. Continue.dev gets `.continue/rules/codeprism.md`. (#27)
- **One user-level config for every project.** `codeprism serve` without a path serves the
  project containing the current directory (nearest `.git` / `.codeprism.toml`) and builds or
  refreshes its index in the background at startup. `setup claude|codex --global` use it.
  `get_graph_stats` reports `index_status`. (#29)
- **Parallel parsing.** `codeprism index` and the MCP server parse in a process pool
  (`--workers/-j`, `parse_workers` config), and SQLite is tuned for bulk writes.
  Full index of a 2M LOC corpus: 89.6s → 42.2s. (#24)
- **Index format version.** Indexes from an older format are fully re-parsed once instead of
  being trusted by checksum. (#25)

### Fixed
- **Claude Code setup never actually registered the server.** MCP entries were written to
  `.claude/settings.json`, which Claude Code doesn't read. Now `.mcp.json` (project, pre-approved
  in `settings.local.json`) or `~/.claude.json` (`--global`); stale entries are migrated, and
  setup files go to `--project` instead of the current directory. (#26)
- **Java, C, C++, Ruby and PHP files were never indexed**, and the watcher ignored them (and
  `.rs`). All 10 languages are now indexed and watched by default. (#20)
- **Re-indexing left stale data**: renamed symbols lingered and edges were duplicated when line
  numbers shifted; `--force` didn't remove deleted files. (#21)
- **`update_file` dropped callers from other files** from the in-memory graph. (#22)
- **Single-file updates did whole-repo work**: 2.6s → ~55ms at 2M LOC. (#28)
- **`search_symbol` kind filter was case-sensitive**; `kind="Function"` returned nothing.
  Thanks @MilindLate. (#19, fixes #18)

---

## [v0.1.8] — 2026-09-28

### Added
- **PHP parser (L4)** — `codeprism/parser/php_parser.py`: namespaces, `use` imports (short alias),
  classes, interfaces, traits, methods (visibility + static), properties, constants, top-level
  functions. 27 tests in `tests/test_parser_php.py`.
- **Ruby parser (L3)** — `codeprism/parser/ruby_parser.py`: modules, classes with inheritance,
  instance methods, singleton/class methods (`def self.x`), `require`/`require_relative` imports,
  constants, visibility tracking (`public`/`protected`/`private`). 24 tests in
  `tests/test_parser_ruby.py`.
- **C / C++ parser (L2)** — `codeprism/parser/c_parser.py`: two parsers (`CParser` + `CppParser`),
  covering functions, structs, typedefs, `#include` imports, C++ classes with inheritance,
  namespaces, templates. 37 tests in `tests/test_parser_c.py`.
- **Java parser (L1)** — `codeprism/parser/java_parser.py`: classes, interfaces, enums, methods,
  annotations, generics, import statements. 35 tests in `tests/test_parser_java.py`.

---

## [v0.1.7] — 2026-09-18

### Added
- **Benchmark corpus expansion**: added pallets/flask 3.0.3 and encode/httpx 0.27.2
  as benchmark corpora (10 tasks each — symbol_lookup, call_trace, impact_analysis,
  dependency_map). Token reduction: flask 91.3%, httpx 93.0%. Overall average across
  3 production codebases: **91%**.

### Fixed
- **`get_dependencies` output format** (`python_parser` + `engine`):
  was returning imported symbol names (`HTTPAdapter`, `_basic_auth_str`) instead of
  source modules (`.adapters`, `.auth`). Root cause: `source_module` was stored in
  `extra{}` which is excluded from SQLite persistence. Fix: store `source_module` in
  the `signature` field (persisted). `get_dependencies` now groups import symbols by
  source module and returns deduplicated module paths. Falls back to symbol names for
  DBs indexed before this change (backward compatible). Re-index required for updated output.
- **Benchmark ground truths** (`requests_004`, `requests_006`, `requests_008`):
  updated to match actual tool output — module paths for dependency_map tasks,
  and both callers (`Session.request` and `SessionRedirectMixin.resolve_redirects`)
  for the `get_callers("send")` task.

---

## [v0.1.6] — 2026-09-17

### Added
- **Level 2 latency benchmark** (`benchmarks/run_latency_benchmark.py`)
  - Measures p50/p95/p99 query latency with configurable warmup and reps
  - Holds the CodePrism engine open across all tasks — times pure query latency, not indexing
  - Results on psf/requests: get_context p50=1.2ms, get_impact p50=2.0ms, get_dependencies p50=3.8ms

### Fixed
- **`get_dependencies` latency** (`StorageManager.get_non_import_symbol_names`):
  was calling `SELECT * FROM symbols` (full table scan) to build the internal/external
  classification set. Fixed with `SELECT DISTINCT name WHERE kind != 'import'`, reducing
  latency from 10.7ms → 3.8ms p50 (64% faster). All 341 tests pass.

---

## [v0.1.5] — 2026-09-17

### Added
- **Level 1 benchmark harness** — token reduction + LLM-as-judge accuracy eval
  - `benchmarks/run_token_benchmark.py` — CLI runner with tiktoken backend
  - `benchmarks/accuracy.py` — Ollama cloud and OpenAI-compatible judge backends
  - `benchmarks/tasks/fixture_tasks.json` — 10 tasks against the fixture project
  - `benchmarks/tasks/requests_tasks.json` — 10 tasks against psf/requests v2.32.3
  - `benchmarks/setup_repos.py` — clones real-world repos for extended benchmark runs
  - `benchmarks/list_models.py` — lists available Ollama judge models with ratings
  - `.env.example` — template for API keys
- **CI: token benchmark workflow** — `.github/workflows/benchmark.yml` runs on every push/PR,
  token-only mode, no API key required, results uploaded as artifact
- **`[bench]` optional dependency group** — `tiktoken`, `openai`, `python-dotenv`
- **`docstring` field in `get_context` output** — symbol docstrings now included in graph
  query results, improving accuracy on symbol_lookup tasks

### Fixed
- **Cross-file call edge bug** (`BaseParser.resolve_intrafile_refs`): CALLS refs that matched
  import stubs were being resolved to the stub instead of the real cross-file target. Fix: skip
  CALLS-to-import-stub resolution intra-file, let `_resolve_cross_file` handle them.
  Impact: `run_payment → compute_checksum` and all similar cross-file calls now wire correctly.
  All 341 tests pass.

### Benchmark results
- Fixture corpus (tiny files): 27% avg token reduction, accuracy baseline=0.89 / CP=0.60
- Requests corpus (real-world, 6k–12k token files): **88.5% avg token reduction**,
  accuracy baseline=0.81 / CP=0.66

---

## [v0.1.4] — 2026-09-06

### Added
- Property-based tests (17 Hypothesis tests) for scanner, graph, and search invariants
- Comprehensive `INTEGRATIONS.md` covering 11 editors and agent frameworks
  (Claude Code, Cursor, Windsurf, Continue.dev, Zed, VS Code Copilot, Cody, Docker Compose,
  CI pipelines, pre-commit hook, OpenAI Agents SDK)
- Compact Integrations section in README linking to `INTEGRATIONS.md`

---

## [v0.1.3] — 2026-09-06

### Added
- Semantic search via embeddings (ChromaDB vector index)
- `--embeddings` flag to `codeprism index` CLI
- `enable_embeddings` config option in `.codeprism.toml`
- `set_embeddings` on QueryEngine; `search_symbols` falls back to substring if no embeddings

### Fixed
- pyproject.toml encoding corruption from UTF-16 write

---

## [v0.1.2] — 2026-09-06

### Added
- `project_path` param to `search_symbol` MCP tool
- `get_graph_stats` path filter

### Fixed
- Author email in pyproject.toml

---

## [v0.1.1] — 2026-09-05

### Added
- MCP server implementation (FastMCP, stdio + SSE transports)
- MCP tools: `get_context`, `get_impact`, `get_module_summary`, `get_callers`, `get_callees`,
  `search_symbol`, `get_file_map`, `get_dependencies`, `scan_diff`, `record_read`,
  `record_write`, `get_session_context`, `undo_write`
- CLI interface: `index`, `serve`, `scan`, `context`, `impact`, `callers`,
  `search`, `summary`, `stats`, `watch`, `setup`
- Query engine: `get_context`, `get_callers`, `get_impact`, `get_dependencies`,
  `get_module_summary`, `search_symbols`
- Security gate: six detector categories (secrets, injection, weak crypto, env exposure,
  unsafe deps, code safety) with BLOCK / WARN / INFO severity
- Incremental indexer + file watcher (watchdog-based, checksum change detection)
- Python, JavaScript/TypeScript, Go parsers (tree-sitter)
- SQLite storage + NetworkX in-memory graph
- `codeprism setup claude` and `codeprism setup cursor` auto-configuration commands

---

## [Initial] — 2026-09-04

- Project scaffolding: pyproject.toml, test fixtures, .gitignore
