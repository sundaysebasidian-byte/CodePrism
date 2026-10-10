# CodePrism

**Stop feeding your AI agent the whole codebase. Give it a graph.**

CodePrism builds a persistent knowledge graph of your project — every function, class, import, and data-flow relationship — and exposes it to any AI coding agent via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io). Instead of your agent reading 40 files to understand one function, it queries the graph and gets exactly what it needs in under 200 tokens.

[![PyPI](https://img.shields.io/pypi/v/codeprism-ai)](https://pypi.org/project/codeprism-ai/)
[![Python](https://img.shields.io/pypi/pyversions/codeprism-ai)](https://pypi.org/project/codeprism-ai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://github.com/knight22-21/CodePrism/actions/workflows/ci.yml/badge.svg)](https://github.com/knight22-21/CodePrism/actions)

---

## Why CodePrism

| Without CodePrism | With CodePrism |
|---|---|
| Agent reads 30–50 files per task | Agent queries the graph — 1–3 targeted calls |
| 8,000–40,000 tokens per context window | 200–800 tokens for equivalent context |
| Agent re-reads the same files repeatedly | Session overlay tracks what's already been read |
| Security issues discovered after the write | Security gate runs before every write |
| Entire codebase re-sent on every file change | Incremental graph update in milliseconds |

**Token reduction target: 60–80% on large codebases.**

> Averaged **91% across 3 real-world repos** (psf/requests, pallets/flask, encode/httpx).
> See [docs/benchmark-results.md](docs/benchmark-results.md) for full results.

---

## What CodePrism Does

- **Indexes** your codebase using tree-sitter AST parsing (Python, JavaScript, TypeScript, Go, Rust, Java, C, C++, Ruby, PHP)
- **Maintains** a knowledge graph per project — built automatically in the background the first time
  you open a project, refreshed incrementally on every start, and honouring your `.gitignore`
- **Answers** precise structural questions: callers, callees, impact, dependencies, data flow
- **Guards** every write with a security scanner — secrets, injection, weak crypto, and more
- **Tracks** agent sessions — what was read, what was written, undo support
- **Serves** all of the above via MCP to Claude Code, Cursor, and any MCP-compatible agent

---

## Installation

```bash
pip install codeprism-ai
```

Python 3.12+ required.

**Optional: semantic search** (heavier install, enables embedding-based symbol search)
```bash
pip install "codeprism-ai[embeddings]"
```

---

## Quickstart

### 1. Connect your AI agent

Run `setup` once per agent. For Claude Code and Codex, **`--global` configures the agent for every
project on your machine**; without it, only the project you pass with `--project` (default: the
current directory) is configured.

**Claude Code**
```bash
codeprism setup claude --global      # every project
codeprism setup claude               # this project only (writes .mcp.json)
```
Then restart Claude Code and run `/mcp` to check that `codeprism` is connected.

**Codex**
```bash
codeprism setup codex --global       # every project (~/.codex/config.toml)
codeprism setup codex                # this project only (.codex/config.toml, trusted projects)
```
Then start a new Codex session.

**Cursor**
```bash
codeprism setup cursor --project /path/to/your/project
```
Then restart Cursor. Cursor entries are tied to one project path, so run setup for each project.

Every setup also writes the CodePrism usage guide to `AGENTS.md` in the project, the shared
instructions file most coding agents read. Claude Code gets a thin `CLAUDE.md` that imports it.
Windsurf, Continue.dev and Zed are configured by hand, see **[INTEGRATIONS.md](INTEGRATIONS.md)**. Their `setup`
commands write to locations those tools don't document and can overwrite a config file they cannot parse
([#42](https://github.com/knight22-21/CodePrism/issues/42), [#43](https://github.com/knight22-21/CodePrism/issues/43)), so don't use them yet.

**Any MCP-compatible agent (manual)**
```bash
codeprism serve                      # serves the project containing the current directory
codeprism serve /path/to/your/project
```
This starts the MCP server on stdio. Point your agent's MCP config at that command.

### 2. Indexing is automatic

When the server starts in a project it builds the index in the background the first time, and
refreshes it on every later start. With a global setup it finds the project for you (the nearest
folder with a `.git` or `.codeprism.toml`); a project-level setup serves the path it was given. Ask `get_graph_stats`: `index_status` is `indexing`, then `ready`. Your home folder is
never indexed.

To build the index yourself, for CI or to pre-build a large repository:

```bash
codeprism index /path/to/your/project
```

Indexing this repository (123 files, about 20,000 lines) takes about a second. A synthetic
2-million-line monorepo takes about 40 seconds on a 12-core machine, and later runs only re-parse
changed files.

### 3. Use it

Your agent can now call tools like `get_context`, `get_impact`, `scan_diff`, and `record_write`
instead of reading raw files.

> Changes you make in your editor while a session is running are not seen by the server until it
> restarts, unless they go through CodePrism's own write tools ([#35](https://github.com/knight22-21/CodePrism/issues/35)).

---

## Integrations

CodePrism works with every major AI editor and agent framework via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io). Two transports are supported: **stdio** (local, default) and **SSE** (HTTP on `127.0.0.1`).

| Agent / Tool | Auto-setup | Transport |
|---|---|---|
| Claude Code | `codeprism setup claude` | stdio |
| Codex | `codeprism setup codex` | stdio |
| Cursor | `codeprism setup cursor` | stdio |
| Windsurf | manual config (see INTEGRATIONS.md) | stdio |
| Continue.dev | manual config (see INTEGRATIONS.md) | stdio |
| Zed | manual config (see INTEGRATIONS.md) | stdio |
| VS Code + GitHub Copilot | manual config | stdio |
| Cody (Sourcegraph) | manual config | stdio |
| Any HTTP agent | `codeprism serve --transport sse` | SSE |
| Python library | `from codeprism import CodePrism` | library |
| GitHub Actions / CI | `codeprism scan --diff` | CLI |
| Pre-commit hook | `.pre-commit-config.yaml` | CLI |

**Auto-setup for Claude Code, Codex and Cursor:**

```bash
codeprism setup claude --global     # or: setup codex --global / setup cursor --project .
# Restart your editor
```

**Python library (no MCP layer):**

```python
from codeprism import CodePrism, SecurityGate

async with CodePrism("/path/to/project") as prism:
    await prism.index()
    ctx = await prism.get_context("payments/processor.py", "process_payment")
    impact = await prism.get_impact("payments/processor.py", "process_payment")
    gate = SecurityGate()
    report = await gate.check_write("payments/processor.py", new_content)
    if report.is_blocked:
        raise ValueError(report.issues[0].description)
```

For detailed per-editor config, Docker Compose setup, CI pipelines, and OpenAI Agents SDK examples, see **[INTEGRATIONS.md](INTEGRATIONS.md)**.

---

## CLI Reference

### Connecting agents

```bash
# Register CodePrism with an agent: claude | codex | cursor | windsurf | continue | zed
codeprism setup claude --global              # user level, serves every project (claude, codex)
codeprism setup claude --project /path/repo  # one project
```

`setup` also writes `AGENTS.md` (and a thin `CLAUDE.md` for Claude Code) into the project. Only
the Claude Code and Codex user-level entries follow the project you open; the others are tied to
one path. Windsurf, Continue.dev and Zed: use the manual configuration in
[INTEGRATIONS.md](INTEGRATIONS.md).

### Indexing

```bash
# Index a project (first run, or refresh: only changed files are re-parsed)
codeprism index /path/to/project

# Only some languages (default: all ten)
codeprism index /path/to/project --languages python,typescript

# Re-parse everything / set parser processes (0 = one per CPU core, the default) / add embeddings
codeprism index /path/to/project --force
codeprism index /path/to/project --workers 4
codeprism index /path/to/project --embeddings
```

Files that git ignores are skipped. After an upgrade that changes the index format, the next run
re-parses everything once, automatically.

### Querying

```bash
# Get structured context for a symbol (paths are relative to --project, default: current directory)
codeprism context payments/processor.py::process_payment --depth 2

# Transitive impact analysis
codeprism impact payments/processor.py::process_payment

# Who calls this function?
codeprism callers payments/processor.py::process_payment

# Search for a symbol by name (substring), optionally by kind: function | class | variable
codeprism search "handle_authentication" --kind function

# File-level summary
codeprism summary payments/processor.py

# Graph statistics
codeprism stats
codeprism stats --verbose    # per-file breakdown
codeprism stats --json
```

Give enough of the path to be unique (`api/utils.py`, not `utils.py`). The MCP tools and CLI
`callers`, `context`, `impact` and `summary` commands report ambiguous paths with candidate files
and a hint to use a longer path. These CLI commands write lookup errors to stderr and exit with
status 1 for an unindexed file, ambiguous path or unknown symbol. `callers` prints "No callers
found" and exits successfully only when the symbol exists and has no callers.

### Visualization

```bash
# Write a self-contained, interactive HTML view of the graph
codeprism visualize /path/to/project --out graph.html
```

### Security scanning

```bash
# Scan a single file
codeprism scan payments/processor.py

# Scan every indexed file in the project
codeprism scan --all --project /path/to/project

# Scan only the files changed in a git range (no index needed)
codeprism scan . --diff HEAD~1..HEAD
codeprism scan . --diff main..feature-branch
codeprism scan . --diff HEAD              # everything changed since HEAD, including staged files
```

Exit codes: `0` = PASS or WARN, `2` = BLOCK (use in CI pipelines).

### Watch mode

```bash
# Keep the on-disk index in sync with file changes (foreground process)
codeprism watch /path/to/project
```

`watch` updates the index on disk. A running MCP server keeps its own in-memory copy and does not
reload it ([#35](https://github.com/knight22-21/CodePrism/issues/35)).

### MCP server

```bash
# stdio (Claude Code, Codex, Cursor, ...). With no path: the project containing the current directory
codeprism serve
codeprism serve /path/to/project

# Don't build/refresh the index in the background
codeprism serve --no-auto-index

# SSE transport
codeprism serve /path/to/project --transport sse --port 8765
```

The SSE server listens on `127.0.0.1` only and has no built-in authentication; there is no `--host`
option yet ([#45](https://github.com/knight22-21/CodePrism/issues/45)). To reach it from another machine, use an SSH tunnel or a reverse proxy on
the same host (see [INTEGRATIONS.md](INTEGRATIONS.md)).

---

## Security Gate

CodePrism scans every proposed file write before it reaches disk. The scanner runs six detector categories:

| Detector | What it catches | Severity |
|---|---|---|
| **Secrets** | Hardcoded passwords, API keys, AWS credentials, GitHub tokens, OpenAI keys | BLOCK |
| **Injection** | SQL injection via f-strings or string concat, `eval()`, `exec()`, `shell=True` | BLOCK / WARN |
| **Weak crypto** | MD5, SHA-1, DES, RC4, non-cryptographic `random` for secrets | WARN |
| **Env var exposure** | Printing or returning `os.environ` contents | WARN |
| **Unsafe dependencies** | `pickle`, unsafe `yaml.load`, `marshal`, dynamic `__import__` | BLOCK / WARN |
| **Code safety** | Bare `except:`, silent exception swallowing, debugger breakpoints | WARN |

Severity rules:
- **BLOCK** — write is rejected; content never reaches disk
- **WARN** — write proceeds but the issue is surfaced to the agent
- **INFO** — logged only

Scan a file manually:
```bash
codeprism scan payments/processor.py
```

Use in CI to block PRs that introduce new security issues:
```bash
codeprism scan --diff HEAD~1..HEAD || exit 1
```

---

## Use Cases

### AI pair programmer context

Your AI agent is editing a large payment processing module. Without CodePrism it reads 15 files to understand the call graph. With CodePrism:

```
Agent: get_context("payments/processor.py", "charge_card", depth=2)
← 340 tokens: the function signature, its 3 callers, its 6 callees, the types it uses
```

### Pre-write security check

Before the agent writes a file that handles user authentication:

```
Agent: scan_diff(original_content, proposed_content, "auth/login.py")
← status: BLOCK, issues: [Hardcoded API key on line 42]
```

The write is stopped before the key ever touches disk.

### Impact analysis before refactoring

Before renaming a core utility function:

```
Agent: get_impact("utils/hash.py", "compute_checksum")
← severity: HIGH, direct_dependents: 12 functions, affected_test_files: ["tests/test_payments.py", ...]
```

The agent knows the full blast radius before making any changes.

### Session-aware long agent chains

In a multi-step agentic workflow, the agent tracks what it has already read:

```
Agent: get_session_context("sess_abc123")
← "3 reads across 2 files, 1 write to payments/processor.py — no need to re-fetch"
```

---

## Supported Languages

| Language | Status | Features |
|---|---|---|
| Python | Full | Functions, classes, imports, type hints, async, decorators |
| JavaScript | Full | Functions, classes, ES modules, CommonJS require |
| TypeScript | Full | + interfaces, type aliases, generics |
| Go | Full | Functions, structs, interfaces, packages |
| Rust | Full | Functions, structs, traits, impl blocks |
| Java | Full | Classes, interfaces, methods, annotations, generics |
| C | Full | Functions, structs, typedefs, includes |
| C++ | Full | Classes, methods, inheritance, templates, namespaces |
| Ruby | Full | Modules, classes, instance/singleton methods, visibility |
| PHP | Full | Namespaces, classes, traits, interfaces, methods |

**Known limitations**

- Calls are linked by following imports for **Python** only; in other languages they are linked
  by name, so a common name (`stringify`, `Split`, `add`) can link to the wrong project
  function ([#37](https://github.com/knight22-21/CodePrism/issues/37)).
- Methods or class attributes with the same name in different classes of one Python file share
  one symbol ([#34](https://github.com/knight22-21/CodePrism/issues/34)).
- Python definitions inside `try` / `if` blocks are not indexed
  ([#39](https://github.com/knight22-21/CodePrism/issues/39)).

See [docs/architecture.md](docs/architecture.md#known-limitations) for the full list.

---

## Configuration

The MCP server reads an optional `.codeprism.toml` in the project root when it starts. The CLI
commands take their options as flags instead (`--languages`, `--workers`, ...).

```toml
[codeprism]
languages = ["python", "typescript"]   # default: all ten supported languages
respect_gitignore = true               # skip files git ignores (default: true)
auto_index = true                      # build/refresh the index in the background (default: true)
enable_embeddings = false              # semantic search (needs codeprism-ai[embeddings])

[codeprism.security]
# Glob patterns matched against each file's ABSOLUTE path
ignore_paths = ["*/tests/fixtures/*", "*.example.*"]

[codeprism.embeddings]
model = "all-MiniLM-L6-v2"
device = "cpu"
```

`ignore_paths` entries are glob patterns matched against the full path, so use a leading `*` for
folders: `"tests/fixtures/"` alone matches nothing.

The index lives in your platform's user-data directory (one SQLite file per project), not in the
repository.

Some keys that appeared in earlier examples (`enable_security_gate`, `watch_debounce_ms`,
`security.block_on_secrets`, `security.warn_on_weak_crypto`, `security.check_new_dependencies` and
the `[codeprism.mcp]` section) are accepted but currently have no effect.

---

## Benchmarks

CodePrism is benchmarked on token reduction and answer accuracy across real-world codebases. Token
numbers below were re-measured on v0.1.11; the LLM-judged accuracy figures are from v0.1.7.

| Corpus | Avg baseline | Avg CodePrism | Reduction |
|---|---:|---:|---:|
| Fixture project (tiny, 2 files) | 270 tokens | 206 tokens | 24% |
| psf/requests v2.32.3 | 6,406 tokens | 714 tokens | **88.9%** |
| pallets/flask 3.0.3 | 9,558 tokens | 805 tokens | **91.6%** |
| encode/httpx 0.27.2 | 12,685 tokens | 857 tokens | **93.2%** |

![Token reduction by corpus](docs/images/token_reduction_by_corpus.png)

![Accuracy: CodePrism vs baseline](docs/images/accuracy_comparison.png)

The fixture numbers are low because on tiny files (135–380 tokens), JSON response overhead can
exceed the raw file size. On real-world files (5k–17k token baselines) the savings are always
substantial — averaging **91% across 3 production codebases**.

Accuracy (LLM-as-judge, v0.1.7): CodePrism **matches or beats the baseline** on 2 of 3 corpora — requests CP **0.87** vs BL 0.86, httpx CP **0.70** vs BL 0.68. Flask gap (0.64 vs 0.77) is concentrated in 2 tasks with ground truth calibration issues.

**Call-link accuracy (Python, v0.1.11).** Cross-file call precision is **1.00** on requests, flask,
httpx and CodePrism (it was 0.68–0.96 before calls followed imports), with recall 0.90–1.00.
Calls within a single file are less exact: precision 0.67–0.85 and recall 0.72–0.81 on the three
corpora, mostly `self.method()` calls that need type information.

Full methodology, per-task breakdown, and reproduction instructions:
**[docs/benchmark-results.md](docs/benchmark-results.md)**

---

## Documentation

| Doc | What it covers |
|---|---|
| [docs/benchmark-results.md](docs/benchmark-results.md) | Benchmark methodology, all run results, reproduction steps |
| [docs/architecture.md](docs/architecture.md) | System design, data flow, key design decisions |
| [docs/changelog.md](docs/changelog.md) | Version history and release notes |
| [INTEGRATIONS.md](INTEGRATIONS.md) | Per-editor setup, Docker Compose, CI, OpenAI Agents SDK |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev environment, coding standards, PR process |

---

## Contributing

We welcome contributions of all kinds — bug fixes, new language parsers, additional security detectors, documentation improvements, and more.

Read the **[Contributing Guide](CONTRIBUTING.md)** for:
- How to set up the development environment
- Coding and testing standards
- The PR and review process
- How to add new security detectors or language parsers

Read the **[Code of Conduct](CODE_OF_CONDUCT.md)** before participating in any community space.

---

## License

MIT — see [LICENSE](LICENSE).

---

## Acknowledgements

Built on [tree-sitter](https://tree-sitter.github.io/tree-sitter/) for fast, accurate parsing; [NetworkX](https://networkx.org/) for graph operations; [FastMCP](https://github.com/jlowin/fastmcp) for the MCP server; and [Pydantic](https://docs.pydantic.dev/) for data validation. Security patterns informed by OWASP Top 10 and CWE.
