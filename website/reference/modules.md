# Module Map

Complete source file reference for the MemPalace codebase.

## Project Structure

```
mempalace/
├── README.md                  ← project documentation
├── mempalace/                 ← core package
│   ├── cli.py                 ← CLI entry point
│   ├── mcp_server.py          ← MCP server (29 tools)
│   ├── knowledge_graph.py     ← temporal entity graph
│   ├── palace_graph.py        ← room navigation graph
│   ├── dialect.py             ← AAAK compression
│   ├── miner.py               ← project file ingest
│   ├── convo_miner.py         ← conversation ingest
│   ├── searcher.py            ← semantic search
│   ├── layers.py              ← 4-layer memory stack
│   ├── onboarding.py          ← guided setup
│   ├── config.py              ← configuration loading
│   ├── normalize.py           ← chat format converter
│   ├── entity_detector.py     ← auto-detect people/projects
│   ├── entity_registry.py     ← entity code registry
│   ├── room_detector_local.py ← room detection from directories
│   ├── general_extractor.py   ← 5-type memory extraction
│   ├── split_mega_files.py    ← transcript splitting
│   ├── spellcheck.py          ← optional spell checking
│   ├── hooks_cli.py           ← hook logic
│   ├── instructions_cli.py    ← skill instructions
│   └── version.py             ← version string
├── benchmarks/                ← reproducible benchmark runners
│   ├── BENCHMARKS.md          ← full results + methodology
│   ├── longmemeval_bench.py   ← LongMemEval runner
│   ├── locomo_bench.py        ← LoCoMo runner
│   ├── membench_bench.py      ← MemBench runner
│   └── convomem_bench.py      ← ConvoMem runner
├── hooks/                     ← Claude Code auto-save hooks
│   ├── mempal_save_hook.sh    ← save every N messages
│   └── mempal_precompact_hook.sh ← emergency save
├── examples/                  ← usage examples
│   ├── basic_mining.py
│   ├── convo_import.py
│   ├── mcp_setup.md
│   └── gemini_cli_setup.md
├── tests/                     ← test suite
├── assets/                    ← logo + brand
└── pyproject.toml             ← package config
```

## Core Modules

### `cli.py` — CLI Entry Point

Argparse-based CLI with subcommands: `init`, `mine`, `split`, `search`, `compress`, `wake-up`, `repair`, `status`, `hook`, `instructions`. Dispatches to the corresponding module.

### `mcp_server.py` — MCP Server

JSON-RPC over stdin/stdout. Implements the MCP protocol with 29 tools covering palace read/write, drawer CRUD, knowledge graph, navigation, tunnels, agent diary, and system operations. Includes the Memory Protocol and AAAK Spec in status responses.

### `searcher.py` — Semantic Search

Two functions: `search()` for CLI output and `search_memories()` for programmatic use. Both query ChromaDB with optional wing/room filters and return verbatim drawer content with similarity scores.

### `layers.py` — Memory Stack

Four classes (`Layer0` through `Layer3`) and the unified `MemoryStack`. Layer 0 reads identity, Layer 1 auto-generates from top drawers, Layer 2 does filtered retrieval, Layer 3 does semantic search.

### `knowledge_graph.py` — Temporal KG

SQLite-backed entity-relationship graph with temporal validity windows. Supports add, invalidate, query, timeline, and stats. Auto-creates entities on triple insertion.

### `palace_graph.py` — Navigation Graph

Builds a graph from ChromaDB metadata where nodes = rooms and edges = tunnels (rooms spanning multiple wings). Supports BFS traversal and tunnel finding.

### `dialect.py` — AAAK Compression

Lossy abbreviation system with entity encoding, emotion detection, topic extraction, and flag identification. Works on both plain text and structured zettel data.

## Ingest Modules

### `miner.py` — Project Ingest

Scans project directories for code and doc files. Respects `.gitignore`. Files content as drawers tagged with wing/room metadata.

### `convo_miner.py` — Conversation Ingest

Imports conversation exports (Claude, ChatGPT, Slack, Markdown, plaintext). Chunks by exchange pair. Supports `exchange` and `general` extraction modes.

### `normalize.py` — Format Converter

Converts 5 chat formats to a standard transcript format before mining.

### `general_extractor.py` — Memory Type Extraction

Classifies conversation content into decisions, preferences, milestones, problems, and emotional context.

## Detection Modules

### `entity_detector.py` — Entity Detection

Scans file content to auto-detect people and projects using regex patterns and heuristics.

### `entity_registry.py` — Entity Registry

Manages entity name → code mappings for AAAK dialect.

### `room_detector_local.py` — Room Detection

Detects rooms from folder structure during `mempalace init`.

## Utility Modules

### `config.py` — Configuration

Loads settings from `~/.mempalace/config.json` and environment variables.

### `split_mega_files.py` — Transcript Splitting

Splits concatenated transcripts into per-session files based on session boundary detection.

### `onboarding.py` — Guided Setup

Interactive setup wizard for `mempalace init`. Generates AAAK bootstrap and wing config.

### `spellcheck.py` — Spell Checking

Optional spell checking utility (requires `autocorrect` package).
