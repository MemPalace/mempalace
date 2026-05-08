> [!CAUTION]
> **Scam alert.** The only official sources for MemPalace are this
> [GitHub repository](https://github.com/MemPalace/mempalace), the
> [PyPI package](https://pypi.org/project/mempalace/), and the docs site at
> **[mempalaceofficial.com](https://mempalaceofficial.com)**. Any other
> domain — including `mempalace.tech` — is an impostor and may distribute
> malware. Details and timeline: [docs/HISTORY.md](docs/HISTORY.md).

> [!IMPORTANT]
> **🚨 Claude Code sessions expire in 30 days w/out auto-save hooks wired!** **[Read this →](https://github.com/MemPalace/mempalace/discussions/1388)**


<div align="center">

<img src="assets/mempalace_logo.png" alt="MemPalace" width="240">

# MemPalace

Local-first AI memory. Verbatim storage, pluggable backend, 96.6% R@5 raw on LongMemEval — zero API calls.

[![][version-shield]][release-link]
[![][python-shield]][python-link]
[![][license-shield]][license-link]
[![][discord-shield]][discord-link]

</div>

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

We recommend [`uv`](https://docs.astral.sh/uv/) — `uv tool install` puts
the `mempalace` CLI in an isolated environment on your PATH:

```bash
uv tool install mempalace
mempalace init ~/projects/myapp
```

If you prefer pip, `pip install mempalace` still works.

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

For Claude Code, Gemini CLI, MCP-compatible tools, and local models, see
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

Two Claude Code hooks save periodically and before context compression:
[mempalaceofficial.com/guide/hooks](https://mempalaceofficial.com/guide/hooks.html).

For per-message recall on top of the file-level chunks the hooks produce,
run `mempalace sweep <transcript-dir>` periodically — it stores one
verbatim drawer per user/assistant message, idempotent and resume-safe.

---

## Requirements

- Python 3.9+
- A vector-store backend (ChromaDB by default)
- ~300 MB disk for the default embedding model

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
[version-shield]: https://img.shields.io/badge/version-3.3.4-4dc9f6?style=flat-square&labelColor=0a0e14
[release-link]: https://github.com/MemPalace/mempalace/releases
[python-shield]: https://img.shields.io/badge/python-3.9+-7dd8f8?style=flat-square&labelColor=0a0e14&logo=python&logoColor=7dd8f8
[python-link]: https://www.python.org/
[license-shield]: https://img.shields.io/badge/license-MIT-b0e8ff?style=flat-square&labelColor=0a0e14
[license-link]: https://github.com/MemPalace/mempalace/blob/main/LICENSE
[discord-shield]: https://img.shields.io/badge/discord-join-5865F2?style=flat-square&labelColor=0a0e14&logo=discord&logoColor=5865F2
[discord-link]: https://discord.com/invite/ycTQQCu6kn
