# CLAUDE.md

## The Mission

Memory is identity. When an AI forgets everything between conversations, it cannot build real understanding — of you, your work, your people, your life.

MemPalace exists to solve this. It is a memory system — not a search engine, not a RAG pipeline, not a vector database wrapper. It treats every word you have shared as sacred, stores it verbatim, and makes it instantly available. Your data never leaves your machine. We never summarize. We never paraphrase. We return your exact words.

100% recall is the design requirement — the target every search path is measured against. Anything less means forgetting, and forgetting means starting over.

The name comes from the ancient "method of loci" — the memory palace technique used for thousands of years to organize and recall vast amounts of information by placing it in imagined rooms of an imagined building. We were also inspired by the Zettelkasten method (created by German sociologist Niklas Luhmann) — small cross-referenced index cards that point to each other. We apply both ideas to AI memory:

- **Wings** for broad categories (people, projects, topics)
- **Rooms** for time-based groupings (days, sessions)
- **Drawers** for full verbatim content (your exact words)
- **Closets** for the index layer — compact pointer lines (`topic|entities|→drawer_ids`) that the project miner writes next to the drawers, so search knows which drawer to open
- **AAAK** (`dialect.py`) — an experimental, lossy abbreviation format (entity codes, emotion markers) that any LLM reads without a decoder. Opt-in via `mempalace compress`; not the storage default, and never a stand-in for the verbatim drawer

## Design Principles

These are non-negotiable. Every PR, every feature, every refactor must honor them.

- **Verbatim always** — Never summarize, paraphrase, or lossy-compress user data. The system searches the index and returns the original words. If a user said it, we store exactly what they said. This is the foundational promise.
- **Incremental only** — Append-only ingest after initial build. Never destroy existing data to rebuild. A crash mid-operation must leave the existing palace untouched.
- **Entity-first** — Everything is keyed by real names with disambiguation by DOB, ID, or context. People matter more than topics.
- **Local-first, zero external API by default** — All extraction, chunking, embedding, and LLM-assisted refinement happens on the user's machine by default, using locally-hosted runtimes (Ollama, LM Studio, llama.cpp, vLLM, unsloth studio, etc.). External providers (Anthropic, OpenAI, Google) are supported via BYOK but are never required and never enabled silently. The system never sends user content to a service the user has not explicitly configured. "Local LLM" is not an external API — Ollama and equivalents running on localhost are part of the user's machine. External BYOK is always a deliberate user choice, never a default and never a silent fallback.
- **Performance budgets** — Hooks under 500ms. Startup injection under 100ms. Memory should feel instant.
- **Privacy by architecture** — The system physically cannot send your data because it never leaves your machine. No telemetry, no phone-home, no external service dependencies for core operations.
- **Background everything** — Filing, indexing, timestamps, and pipeline work happen via hooks in the background. Nothing interrupts the user's conversation. Zero tokens spent on bookkeeping in the chat window.

## Contributing

We welcome bug fixes, performance improvements, new language support, better entity disambiguation, documentation, and test coverage.

We do not accept summarization of user content, cloud storage/sync features, telemetry or analytics, features requiring API keys for core memory, or shortcuts that bypass verbatim storage.

## Setup

```bash
uv sync --extra dev   # recommended; or: pip install -e ".[dev]"
```

## Commands

```bash
# Run tests
uv run pytest tests/ -v --ignore=tests/benchmarks

# Run tests with coverage
uv run pytest tests/ -v --ignore=tests/benchmarks --cov=mempalace --cov-report=term-missing

# Lint
uv run ruff check .

# Format
uv run ruff format .

# Format check (CI mode)
uv run ruff format --check .
```

## Project Structure

```
mempalace/
├── mcp_server/          # MCP server package — tools, schemas, protocol, transports
├── cli/                 # CLI package — commands, parser; public import is still mempalace.cli
├── config.py            # Configuration + input validation
├── miner.py             # Project file miner
├── convo_miner.py       # Conversation transcript miner
├── searcher/            # Hybrid search package; public import is still mempalace.searcher
├── knowledge_graph.py   # Temporal entity-relationship graph (SQLite)
├── palace/              # Palace ops package; public import is still mempalace.palace
├── palace_graph.py      # Room traversal + cross-wing tunnels
├── backends/            # Pluggable storage backends (ChromaDB default)
│   ├── base.py          # Abstract interface — implement this for new backends
│   └── chroma.py        # ChromaDB implementation
├── dialect.py           # AAAK dialect (experimental, lossy, opt-in)
├── normalize.py         # Transcript format detection + normalization
├── entity_detector.py   # Auto-detect people/projects from content
├── entity_registry.py   # Entity storage and disambiguation
├── layers.py            # L0-L3 memory wake-up stack
├── repair.py            # Palace repair and consistency checks
├── migrate.py           # ChromaDB version migration
├── spellcheck.py        # Auto-correct user messages
├── exporter.py          # Palace data export
├── hooks_cli.py         # Hook management CLI
├── query_sanitizer.py   # Prompt contamination prevention
├── split_mega_files.py  # Split concatenated transcript files
└── version.py           # Single source of truth for version

hooks/                   # Claude Code hook scripts
├── mempal_save_hook.sh        # Stop: triggers diary save
└── mempal_precompact_hook.sh  # PreCompact: saves state before compression
```

## Conventions

- **Python style**: snake_case for functions/variables, PascalCase for classes
- **Linter**: ruff with E/F/W rules
- **Formatter**: ruff format, double quotes
- **Commits**: conventional commits (`fix:`, `feat:`, `test:`, `docs:`, `ci:`)
- **Tests**: `tests/test_*.py`, fixtures in `tests/conftest.py`
- **Coverage**: pytest reads the gate from `pyproject.toml` (`[tool.coverage.report] fail_under`; on Windows, `[tool.mempalace.coverage] fail_under_windows`, because ChromaDB file-lock cleanup leaves lines unexecuted).

## Architecture

```
User → CLI / MCP Server → Storage Backend (ChromaDB default, pluggable)
                        → SQLite (knowledge graph)

Palace structure:
  WING (person/project)
    └── ROOM (day/topic)
          └── DRAWER (verbatim text chunk)

Index layer (closets, regex-built by the project miner):
  Pointer lines (topic|entities|→drawer_ids) → DRAWER locations
  Search always queries drawers; closet hits boost ranking, never gate it

AAAK (dialect.py, opt-in via `mempalace compress`):
  Lossy abbreviation for LLM reading; drawers stay verbatim

Knowledge Graph:
  ENTITY → PREDICATE → ENTITY (with valid_from / valid_to dates)
```

## Key Files for Common Tasks

- **Adding an MCP tool**: handler in `mempalace/mcp_server/tools_*.py` + `TOOLS` entry in `mempalace/mcp_server/schemas.py` (public import path is still `mempalace.mcp_server`)
- **Adding a CLI command**: handler in `mempalace/cli/cmd_*.py` + argparse in `mempalace/cli/parser.py` (public import path is still `mempalace.cli`)
- **Changing search**: `mempalace/searcher/` (ranking, sqlite BM25, pipeline, CLI); public import is still `mempalace.searcher`
- **Modifying mining**: `mempalace/miner.py` (project files) or `mempalace/convo_miner.py` (transcripts)
- **Adding a storage backend**: subclass `mempalace/backends/base.py`, register in `backends/__init__.py`
- **Input validation**: `mempalace/config.py` — `sanitize_name()` / `sanitize_content()`
- **Tests**: mirror source structure in `tests/test_<module>.py`
