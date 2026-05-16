# mempalace/ — Core Package

The Python package that powers MemPalace. All modules, all logic.

## Modules

| Module | What it does |
|--------|-------------|
| `cli.py` | CLI entry point — routes to mine, search, init, compress, wake-up |
| `config.py` | Configuration loading — `~/.mempalace/config.json`, env vars, defaults |
| `normalize.py` | Converts 5 chat formats (Claude Code JSONL, Claude.ai JSON, ChatGPT JSON, Slack JSON, plain text) to standard transcript format |
| `miner.py` | Project file ingest — scans directories, chunks by paragraph, stores to the vector store |
| `convo_miner.py` | Conversation ingest — chunks by exchange pair (Q+A), detects rooms from content |
| `searcher.py` | Semantic search — filters by wing/room, returns verbatim + scores |
| `layers.py` | 4-layer memory stack: L0 (identity), L1 (critical facts), L2 (room recall), L3 (deep search) |
| `dialect.py` | AAAK compression — entity codes, emotion markers, 30x lossless ratio |
| `knowledge_graph.py` | Temporal entity-relationship graph — SQLite, time-filtered queries, fact invalidation |
| `palace_graph.py` | Room-based navigation graph — BFS traversal, tunnel detection across wings |
| `mcp_server.py` | MCP server — 19 tools, AAAK auto-teach, Palace Protocol, agent diary |
| `onboarding.py` | Guided first-run setup — asks about people/projects, generates AAAK bootstrap + wing config |
| `entity_registry.py` | Entity code registry — maps names to AAAK codes, handles ambiguous names |
| `entity_detector.py` | Auto-detect people and projects from file content |
| `general_extractor.py` | Classifies text into 5 memory types (decision, preference, milestone, problem, emotional) |
| `room_detector_local.py` | Maps folders to room names using 70+ patterns — no API |
| `spellcheck.py` | Name-aware spellcheck — won't "correct" proper nouns in your entity registry |
| `split_mega_files.py` | Splits concatenated transcript files into per-session files |

## Architecture

```
User → CLI → miner/convo_miner → vector store (palace: ChromaDB or Qdrant)
                                     ↕
                              knowledge_graph (SQLite)
                                     ↕
User → MCP Server → searcher → results
                  → kg_query → entity facts
                  → diary    → agent journal
```

The palace (ChromaDB or Qdrant) stores verbatim content. The knowledge graph (SQLite) stores structured relationships. The MCP server exposes both to any AI tool.
