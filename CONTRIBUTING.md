# Contributing to MemPalace

Thanks for wanting to help. MemPalace is open source and we welcome contributions of all sizes — from typo fixes to new features.

## Getting Started

```bash
# Fork the repo on GitHub first, then clone your fork
git clone https://github.com/<your-username>/mempalace.git
cd mempalace
git remote add upstream https://github.com/MemPalace/mempalace.git

# Recommended: uv (https://docs.astral.sh/uv/) handles the venv for you
uv sync --extra dev

# Or with pip in your own venv:
# pip install -e ".[dev]"

# Install the commit and push hooks (one-time per clone; the worktrees share them)
python -m commonplace.guards.hooks install
```

Run the install after setup, including in existing clones. Do not use
`pre-commit install`: the hook it writes hard-codes the interpreter of the
worktree that ran it and needs `pre-commit` on PATH, so in a fresh linked
worktree every commit fails with "`pre-commit` not found".

The hooks installed here are two small scripts in the clone's shared hooks
directory. Each run uses the environment of the worktree it runs in
(`COMMONPLACE_PYTHON`, then the worktree's `.venv`, then an activated
`VIRTUAL_ENV`, then `python3`). A new worktree needs its own `uv sync --extra dev`;
if it has none, the hook stops and prints that command. `python -m
commonplace.guards.hooks status` shows what a worktree would use.

- **Commit** (through pre-commit, pinned to the same Ruff as the merge checks):
  fixes Python lint and formatting in the staged files.
- **Push** is a **quick check**, not the full test suite and not CI. It looks at the
  files the push changes and runs, within a time budget: `ruff check` and
  `ruff format --check` on them (plus `cargo fmt --check` for changed Rust files),
  `docs-check` against the base branch, and the fast tests whose names match the
  changed modules (`commonplace/guards/stacks.py` selects `tests/test_guards_stacks.py`
  and `tests/test_stacks*.py`; no marked-slow tests). Nothing is edited.
  A failed check blocks the push. The budget is 60 seconds for everything; set it
  with `COMMONPLACE_PREPUSH_BUDGET=120` or `--budget`. When it runs out, the
  step that was running is stopped, later steps are not started, and the run
  lists exactly what was skipped or cut off: that does not block the push, and it
  is never printed as a pass. Run the same check by hand against the base with
  `python -m commonplace.guards.prepush --manual --base origin/develop`.

## Running Tests

```bash
uv run pytest tests/ -v
```

All tests must pass before submitting a PR. Tests should run without API keys or network access.

For parallel runs, install the development extras and run behavior tests first,
then wall-clock budget tests serially:

```bash
uv sync --extra dev
uv run pytest tests/ --ignore=tests/benchmarks -m "not (benchmark or slow or stress or timing)" -n 6 --dist load
uv run pytest tests/ --ignore=tests/benchmarks -m "timing and not (benchmark or slow or stress)" -n 0
```

The default marker expression in `pyproject.toml` already excludes `timing`, so a
plain `uv run pytest tests/` runs the behavior tests only; the first command is its
parallel form. Wall-clock and memory measurements live in the `timing` lane: run the
second command explicitly, on an idle system. Timing results are informational and
never block a merge. Each worker has its own home, and configuration follows each test's
HOME. Benchmarks and performance budgets run separately.

### Property-based tests (optional)

`hypothesis` is available in the dev extras for property-based tests:

```python
from hypothesis import given, strategies as st

@given(st.text(min_size=1, max_size=40))
def test_function_never_fabricates_output_on_random_input(s):
    # ... property that must hold for ANY string in the strategy
```

Hypothesis generates hundreds of inputs per test and shrinks failing
cases to a minimal counterexample. Useful any time a function returns
`Optional[X]` or has a wide input domain — it catches the failure-space
gaps that hand-written positive tests miss.

## Running Benchmarks

```bash
# Quick test (20 questions, ~30 seconds)
uv run python benchmarks/longmemeval_bench.py /path/to/longmemeval_s_cleaned.json --limit 20

# Full benchmark (500 questions, ~5 minutes)
uv run python benchmarks/longmemeval_bench.py /path/to/longmemeval_s_cleaned.json
```

See [benchmarks/README.md](benchmarks/README.md) for data download instructions and reproduction guide.

## Project Structure

```
mempalace/          ← core package (see mempalace/README.md for module guide)
benchmarks/         ← reproducible benchmark runners
hooks/              ← Claude Code auto-save hooks
examples/           ← usage examples
tests/              ← test suite
assets/             ← logo + brand
```

## PR Guidelines

1. Fork the repo and create a feature branch: `git checkout -b feat/my-thing`
2. Write your code
3. Add or update tests if applicable
4. Run `uv run pytest tests/ -v` — everything must pass
5. Commit with a clear message following [conventional commits](https://www.conventionalcommits.org/):
   - `feat: add Notion export format`
   - `fix: handle empty transcript files`
   - `docs: update MCP tool descriptions`
   - `bench: add LoCoMo turn-level metrics`
6. Push to your fork and open a PR against `develop`

## Branching Model

- **Feature and fix PRs should target `develop`.** This is the integration branch where all day-to-day work lands.
- **`main` is reserved for tagged, stable releases.** Releases are promoted by merging `develop` into `main` (e.g., `v3.7.0`, `v3.6.0`).
- Only emergency hotfixes should branch directly off — and target — `main`.

## Checking release versions

Before preparing a release, run the same consistency check as CI:

```bash
python scripts/check_versions.py
```

The checker compares thirteen version values in eleven files, using
`mempalace/version.py` as the reference: `pyproject.toml`, the Claude, Codex and
DSH manifests, the OpenClaw skill frontmatter, the README version badge, the
editable MemPalace entry in `uv.lock`, the Cargo workspace version, and the three
MemPalace workspace-package entries in `Cargo.lock`. Other dependency versions
are outside this check.

It reports missing, ambiguous or mismatched entries and exits with a nonzero
status. It reads the release files without modifying them. On Python 3.10,
it uses the `tomli` dependency installed with MemPalace; newer Python
versions use the standard library TOML parser.

The Version Guard workflow runs this check when guarded files change and on
`v*` tag pushes. Stable tags must also match the reference version; prerelease
tags retain their existing exemption from that tag comparison. Release-version
edits remain a separate release-preparation step.

## Code Style

- **Formatting**: [Ruff](https://docs.astral.sh/ruff/) with 100-char line limit (configured in `pyproject.toml`)
- **Naming**: `snake_case` for functions/variables, `PascalCase` for classes
- **Docstrings**: on all modules and public functions
- **Type hints**: where they improve readability
- **Dependencies**: minimize. ChromaDB + PyYAML only. Don't add new deps without discussion.

## Good First Issues

Check the [Issues](https://github.com/MemPalace/mempalace/issues) tab. Great starting points:

- **New chat formats**: Add import support for Cursor, Copilot, or other AI tool exports
- **Room detection**: Improve pattern matching in `room_detector_local.py`
- **Tests**: Increase coverage — especially for `knowledge_graph.py` and `palace_graph.py`
- **Entity detection**: Better name disambiguation in `entity_detector.py`
- **Docs**: Improve examples, add tutorials

## Architecture Decisions

If you're planning a significant change, open an issue first to discuss the approach. Key principles:

- **Verbatim first**: Never summarize user content. Store exact words.
- **Local first**: Everything runs on the user's machine. No cloud dependencies.
- **Zero API by default**: Core features must work without any API key.
- **Palace structure is scoping, not magic**: Wings, halls, and rooms act as metadata filters in the underlying vector store. They keep retrieval predictable when a palace holds many unrelated projects or people. Respect the hierarchy — but don't present it as a novel retrieval mechanism.

## Community

- **Discord**: [Join us](https://discord.com/invite/ycTQQCu6kn)
- **Issues**: Bug reports and feature requests welcome
- **Questions and ideas**: Please use Discord or open an issue while GitHub Discussions are not enabled for this repo

## License

MIT — your contributions will be released under the same license.

## Git identity for contributions

Before pushing commits, verify that Git is configured with an email address that GitHub can associate with your account:

```bash
git config user.name
git config user.email
```

This is especially important when commits are created through agentic coding tools or automation, because those tools may not inherit your normal shell Git configuration. Avoid placeholder values such as `your@email.com` or localized template text; unresolved author emails can create avoidable provenance and SBOM review friction for downstream users.
