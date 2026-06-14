<div align="center">

<img src="assets/mempalace_logo.png" alt="MemPalace" width="240">

# MemPalace

Local-first AI memory. Verbatim storage, pluggable backend, 96.6% R@5 raw on LongMemEval — zero API calls.

[![][version-shield]][release-link]
[![][python-shield]][python-link]
[![][license-shield]][license-link]
[![][discord-shield]][discord-link]

</div>

> [!CAUTION]
> **Beware of impostor sites.** MemPalace has no other official websites. The **only** official sources are this **[GitHub repository](https://github.com/MemPalace/mempalace)**, the **[PyPI package](https://pypi.org/project/mempalace/)**, and the docs at **[mempalaceofficial.com](https://mempalaceofficial.com)**. Any other domain (including `.tech`, `.net`, or other `.com` variants) is an impostor and may distribute malware. Details and timeline: [docs/HISTORY.md](docs/HISTORY.md).

> [!IMPORTANT]
> **Claude Code sessions expire in 30 days without auto-save hooks wired.** [Read this →](https://github.com/MemPalace/mempalace/discussions/1388)
>
> Need the shortest recovery/setup path? Use the [Claude Code retention setup checklist](https://mempalaceofficial.com/guide/claude-code-retention.html).

---

## What it is

MemPalace stores your conversation history as verbatim text and retrieves
it with semantic search. It does not summarize, extract, or paraphrase.
The index is structured — people and projects become *wings*, topics
become *rooms*, and original content lives in *drawers* — so searches
can be scoped rather than run against a flat corpus.

The retrieval layer is pluggable. The current default is ChromaDB; the
interface is defined in [`mempalace/backends/base.py`](mempalace/backends/base.py)
and alternative backends can be dropped in without touching the rest of
the system.

Nothing leaves your machine unless you opt in.

Architecture, concepts, and mining flows:
[mempalaceofficial.com/concepts/the-palace](https://mempalaceofficial.com/concepts/the-palace.html).

---

## Install

MemPalace ships a CLI, so install it in an isolated environment to avoid
PEP 668 errors on Debian/Ubuntu/Homebrew Pythons and to keep mempalace's
deps (`chromadb`, `numpy`, `grpcio`, …) from conflicting with anything
else in your global site-packages.

We recommend [`uv`](https://docs.astral.sh/uv/) — `uv tool install` puts
the `mempalace` CLI in an isolated environment on your PATH:

```bash
uv tool install mempalace
mempalace init ~/projects/myapp
```

[`pipx`](https://pipx.pypa.io/) works the same way if you prefer it:
`pipx install mempalace`.

Prefer plain `pip` only inside an activated virtualenv where you
explicitly want `import mempalace` available:

```bash
python -m venv .venv && source .venv/bin/activate
pip install mempalace
```

### Docker

A container image is also available for running the MCP server or the CLI
without a local Python toolchain. Everything persists under `/data` (palace,
config, and the cached embedding model), so mount a volume there.

```bash
# Build the image (CPU; bundles the `extract` + `spellcheck` extras)
docker build -t mempalace .

# MCP server over stdio — note the `-i` flag (JSON-RPC needs stdin)
docker run -i --rm -v mempalace-data:/data mempalace

# Run any CLI command instead (mount the host directory you want to mine)
docker run --rm -v mempalace-data:/data -v /path/to/project:/work mempalace mine /work
docker run --rm -v mempalace-data:/data mempalace search "why GraphQL"
```

Wire it into an MCP client (e.g. Claude Code) as a stdio server:

```json
{
  "mcpServers": {
    "mempalace": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "-v", "mempalace-data:/data", "mempalace"]
    }
  }
}
```

`docker compose run --rm mcp` works too (see `docker-compose.yml`). For
CUDA-accelerated embeddings, build the GPU variant with
`docker build -f Dockerfile.gpu -t mempalace:gpu .` and run it with
`--gpus all`. Customise the bundled extras at build time, e.g.
`docker build --build-arg EXTRAS="extract,spellcheck" -t mempalace .`.

## Storage backends

ChromaDB is the default. For the pluggable-backend preview, MemPalace also
ships `sqlite_exact` for local exact-vector correctness checks, and two opt-in
external service backends — `qdrant` (REST) and `pgvector` (Postgres). The two
external backends exercise the storage contract on different substrates (a
REST/dict store and a SQL/JSONB store), so it is not accidentally shaped around
one vendor.

```bash
# local no-service backend
mempalace mine ~/projects/myapp --backend sqlite_exact

# Qdrant backend, defaulting to http://localhost:6333
MEMPALACE_QDRANT_URL=http://localhost:6333 \
  mempalace mine ~/projects/myapp --backend qdrant

# Postgres + pgvector backend, defaulting to postgresql://localhost:5432/mempalace
#   needs the optional driver: pip install mempalace[pgvector]
#   and the `vector` extension available on the server
MEMPALACE_PGVECTOR_DSN=postgresql://localhost:5432/mempalace \
  mempalace mine ~/projects/myapp --backend pgvector
```

Qdrant can also be configured with `MEMPALACE_QDRANT_API_KEY`,
`MEMPALACE_QDRANT_NAMESPACE`, and `MEMPALACE_QDRANT_TIMEOUT`; pgvector with
`MEMPALACE_PGVECTOR_NAMESPACE`. Both external backends isolate tenants by
namespace (advertised via the `supports_namespace_isolation` capability) and
write a local marker (`qdrant_backend.json` / `pgvector_backend.json`) to guard
against silently opening a palace against the wrong server.

When `MEMPALACE_QDRANT_URL` or `MEMPALACE_PGVECTOR_DSN` points anywhere other
than your own local or trusted self-hosted service, MemPalace will send and
store verbatim drawer text and metadata there. That is an explicit opt-in
backend choice, never the default.

## Quickstart

```bash
# Mine content into the palace
mempalace mine ~/projects/myapp                    # project files
mempalace mine ~/.claude/projects/ --mode convos   # Claude Code sessions (scope with --wing per project)

# Search
mempalace search "why did we switch to GraphQL"

# Load context for a new session
mempalace wake-up
```

For Claude Code, Gemini CLI, [Antigravity](https://mempalaceofficial.com/guide/antigravity.html),
MCP-compatible tools, and local models, see
[mempalaceofficial.com/guide/getting-started](https://mempalaceofficial.com/guide/getting-started.html).

---

## Benchmarks

All numbers below are reproducible from this repository with the commands
in [`benchmarks/BENCHMARKS.md`](benchmarks/BENCHMARKS.md). Full
per-question result files are committed under `benchmarks/results_*`.

**LongMemEval — retrieval recall (R@5, 500 questions):**

| Mode | R@5 | LLM required |
|---|---|---|
| Raw (semantic search, no heuristics, no LLM) | **96.6%** | None |
| Hybrid v4, held-out 450q (tuned on 50 dev, not seen during training) | **98.4%** | None |
| Hybrid v4 + LLM rerank (full 500) | ≥99% | Any capable model |

The raw 96.6% requires no API key, no cloud, and no LLM at any stage. The
hybrid pipeline adds keyword boosting, temporal-proximity boosting, and
preference-pattern extraction; the held-out 98.4% is the honest
generalisable figure.

The rerank pipeline promotes the best candidate out of the top-20
retrieved sessions using an LLM reader. It works with any reasonably
capable model — we have reproduced it with Claude Haiku, Claude Sonnet,
and minimax-m2.7 via Ollama Cloud (no Anthropic dependency). The gap
between raw and reranked is model-agnostic; we do not headline a "100%"
number because the last 0.6% was reached by inspecting specific wrong
answers, which `benchmarks/BENCHMARKS.md` flags as teaching to the test.

**Other benchmarks (full results in [`benchmarks/BENCHMARKS.md`](benchmarks/BENCHMARKS.md)):**

| Benchmark | Metric | Score | Notes |
|---|---|---|---|
| LoCoMo (session, top-10, no rerank) | R@10 | 60.3% | 1,986 questions |
| LoCoMo (hybrid v5, top-10, no rerank) | R@10 | 88.9% | Same set |
| ConvoMem (all categories, 250 items) | Avg recall | 92.9% | 50 per category |
| MemBench (ACL 2025, 8,500 items) | R@5 | 80.3% | All categories |

We deliberately do not include a side-by-side comparison against Mem0,
Mastra, Hindsight, Supermemory, or Zep. Those projects publish different
metrics on different splits, and placing retrieval recall next to
end-to-end QA accuracy is not an honest comparison. See each project's
own research page for their published numbers.

**Reproducing every result:**

```bash
git clone https://github.com/MemPalace/mempalace.git
cd mempalace
uv sync --extra dev   # or: pip install -e ".[dev]"
# see benchmarks/README.md for dataset download commands
uv run python benchmarks/longmemeval_bench.py /path/to/longmemeval_s_cleaned.json
```

---

## Knowledge graph

MemPalace includes a temporal entity-relationship graph with validity
windows — add, query, invalidate, timeline — backed by local SQLite.
Usage and tool reference:
[mempalaceofficial.com/concepts/knowledge-graph](https://mempalaceofficial.com/concepts/knowledge-graph.html).

## MCP server

29 MCP tools cover palace reads/writes, knowledge-graph operations,
cross-wing navigation, drawer management, and agent diaries. Installation
and the full tool list:
[mempalaceofficial.com/reference/mcp-tools](https://mempalaceofficial.com/reference/mcp-tools.html).

## Agents

Each specialist agent gets its own wing and diary in the palace.
Discoverable at runtime via `mempalace_list_agents` — no bloat in your
system prompt:
[mempalaceofficial.com/concepts/agents](https://mempalaceofficial.com/concepts/agents.html).

## Auto-save hooks

Auto-save hooks for **Claude Code, Codex CLI, and Cursor IDE** save
periodically and before context compression:

- Claude Code + Codex →
  [mempalaceofficial.com/guide/hooks](https://mempalaceofficial.com/guide/hooks.html)
- Cursor IDE (adds session-start recall and a transcript snapshot before
  compaction) →
  [mempalaceofficial.com/guide/cursor-hooks](https://mempalaceofficial.com/guide/cursor-hooks.html)

If you are installing under time pressure, start with the
[Claude Code retention setup checklist](https://mempalaceofficial.com/guide/claude-code-retention.html):
wire the hooks, back up existing JSONL transcripts, and backfill them with
`mempalace mine ~/.claude/projects/ --mode convos`.

For per-message recall on top of the file-level chunks the hooks produce,
run `mempalace sweep <transcript-dir>` periodically — it stores one
verbatim drawer per user/assistant message, idempotent and resume-safe.

---

## Requirements

- Python 3.9+
- A vector-store backend (ChromaDB by default)
- ~300 MB disk for the embedding model. Onboarding (`python -m mempalace.onboarding`) offers `embeddinggemma-300m` (multilingual, 100+ languages, recommended) or `all-MiniLM-L6-v2` (English-only, ~30 MB). See the docstring at [`mempalace/embedding.py`](mempalace/embedding.py) for details and migration notes.

No API key is required for the core benchmark path.

## Docs

- Getting started → [mempalaceofficial.com/guide/getting-started](https://mempalaceofficial.com/guide/getting-started.html)
- CLI reference → [mempalaceofficial.com/reference/cli](https://mempalaceofficial.com/reference/cli.html)
- Python API → [mempalaceofficial.com/reference/python-api](https://mempalaceofficial.com/reference/python-api.html)
- Full benchmark methodology → [benchmarks/BENCHMARKS.md](benchmarks/BENCHMARKS.md)
- Release notes → [CHANGELOG.md](CHANGELOG.md)
- Corrections and public notices → [docs/HISTORY.md](docs/HISTORY.md)

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).

<!-- Link Definitions -->
[version-shield]: https://img.shields.io/badge/version-3.4.1-4dc9f6?style=flat-square&labelColor=0a0e14
[release-link]: https://github.com/MemPalace/mempalace/releases
[python-shield]: https://img.shields.io/badge/python-3.9+-7dd8f8?style=flat-square&labelColor=0a0e14&logo=python&logoColor=7dd8f8
[python-link]: https://www.python.org/
[license-shield]: https://img.shields.io/badge/license-MIT-b0e8ff?style=flat-square&labelColor=0a0e14
[license-link]: https://github.com/MemPalace/mempalace/blob/main/LICENSE
[discord-shield]: https://img.shields.io/badge/discord-join-5865F2?style=flat-square&labelColor=0a0e14&logo=discord&logoColor=5865F2
[discord-link]: https://discord.com/invite/ycTQQCu6kn
