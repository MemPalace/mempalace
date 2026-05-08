# Contributing

PRs welcome. MemPalace is open source and we welcome contributions of all sizes — from typo fixes to new features.

## Getting Started

```bash
git clone https://github.com/MemPalace/mempalace.git
cd mempalace

# Recommended: uv (https://docs.astral.sh/uv/) manages the venv for you
uv sync --extra dev

# Or with pip in your own venv:
# pip install -e ".[dev]"
```

## Running Tests

```bash
uv run pytest tests/ -v
```

All tests must pass before submitting a PR. Tests should run without API keys or network access.

## Running Benchmarks

```bash
# Quick test (20 questions, ~30 seconds)
uv run python benchmarks/longmemeval_bench.py /path/to/longmemeval_s_cleaned.json --limit 20

# Full benchmark (500 questions, ~5 minutes)
uv run python benchmarks/longmemeval_bench.py /path/to/longmemeval_s_cleaned.json
```

See [Benchmarks](/reference/benchmarks) for data download instructions.

## PR Guidelines

1. Fork the repo and create a feature branch: `git checkout -b feat/my-thing`
2. Write your code
3. Add or update tests if applicable
4. Run `uv run pytest tests/ -v` — everything must pass
5. Commit with clear [conventional commits](https://www.conventionalcommits.org/):
   - `feat: add Notion export format`
   - `fix: handle empty transcript files`
   - `docs: update MCP tool descriptions`
   - `bench: add LoCoMo turn-level metrics`
6. Push to your fork and open a PR against `main`

## Code Style

- **Formatting**: [Ruff](https://docs.astral.sh/ruff/) with 100-char line limit
- **Naming**: `snake_case` for functions/variables, `PascalCase` for classes
- **Docstrings**: on all modules and public functions
- **Type hints**: where they improve readability
- **Dependencies**: minimize — ChromaDB + PyYAML only. Don't add new deps without discussion.

## Good First Issues

Check the [Issues](https://github.com/MemPalace/mempalace/issues) tab:

- **New chat formats** — add import support for Cursor, Copilot, or other AI tool exports
- **Room detection** — improve pattern matching in `room_detector_local.py`
- **Tests** — increase coverage, especially for `knowledge_graph.py` and `palace_graph.py`
- **Entity detection** — better name disambiguation in `entity_detector.py`
- **Docs** — improve examples, add tutorials

## Architecture Decisions

If you're planning a significant change, open an issue first. Key principles:

- **Verbatim first** — never summarize user content. Store exact words.
- **Local first** — everything runs on the user's machine. No cloud dependencies.
- **Zero API by default** — core features must work without any API key.
- **Palace structure is scoping, not magic** — wings, halls, and rooms act as metadata filters in the underlying vector store. They make scoping predictable when a palace holds many unrelated projects; they are not a novel retrieval mechanism.

## Community

- [Discord](https://discord.com/invite/ycTQQCu6kn)
- [GitHub Issues](https://github.com/MemPalace/mempalace/issues) — bug reports and feature requests
- [GitHub Discussions](https://github.com/MemPalace/mempalace/discussions) — questions and ideas

## License

MIT — your contributions will be released under the same license.
