# Spec: tests/test_readme_claims.py

## Purpose

A test suite that verifies documentation claims (README, website docs, badges)
against the actual shipped code and configuration. Each test cross-checks a
specific documented claim and fails if docs and code disagree, treating code as
the source of truth (tests/test_readme_claims.py:L3-L9).

## Path and File Anchors

All file locations are computed relative to a repository root defined as the
parent of the directory containing the test file (tests/test_readme_claims.py:L22-L23).
The following anchors are used (tests/test_readme_claims.py:L22-L26):
- repo root = two levels above the test file
- package dir = `<repo>/mempalace`
- README = `<repo>/README.md`
- MCP tools doc = `<repo>/website/reference/mcp-tools.md`
- modules doc = `<repo>/website/reference/modules.md`

## Helpers (internal contracts)

- File reading: any referenced file is read as UTF-8 text, replacing undecodable
  bytes rather than erroring (tests/test_readme_claims.py:L29-L34).
- Tool name discovery from code: tool names are extracted from `mempalace/mcp_server.py`
  by matching keys of the form `"mempalace_<word>": {` (a string key beginning with
  `mempalace_` followed by `:` and an opening brace). The source is parsed as text
  rather than imported, because importing the server triggers heavy initialization
  (tests/test_readme_claims.py:L37-L42).
- Documented tool name discovery: tool names are extracted from the MCP tools doc
  by matching level-3 Markdown headings of the form ``### `mempalace_<word>` ``
  at line start (tests/test_readme_claims.py:L45-L53).

## Test Behaviors

### Tool count consistency (README vs code)
For every occurrence in README of the pattern `<digits> tools`, the claimed number
must equal the count of tool keys found in `mcp_server.py`. Any mismatch fails
(tests/test_readme_claims.py:L64-L78). Separately, if README mentions `N tools`
more than once, all occurrences must be the same number (tests/test_readme_claims.py:L734-L742).

### Documented tools must exist in code
Every tool name documented in the MCP tools doc must be present as a tool key in
`mcp_server.py`. The doc must yield at least one tool heading (else fail), and any
documented tool absent from code fails the test (tests/test_readme_claims.py:L89-L109).

### No undocumented tools
Every tool key found in `mcp_server.py` must also appear in the MCP tools doc.
Tools present in code but missing from docs fail the test (tests/test_readme_claims.py:L120-L132).

### Closets feature presence
`palace.py` source must contain a `get_closets_collection(` definition
(tests/test_readme_claims.py:L143-L150), and that symbol must be importable and
callable from the palace module (tests/test_readme_claims.py:L152-L156).

### Closet-aware search
`searcher.py` source must contain the token `CLOSET_RANK_BOOSTS`
(tests/test_readme_claims.py:L167-L174) and must reference `get_closets_collection`
(tests/test_readme_claims.py:L176-L182).

### BM25 / hybrid search presence
`searcher.py` (lowercased) must contain at least one of the tokens: `bm25`,
`_bm25_score`, `_hybrid_rank`, `hybrid_search`, `bm25_score`, `rank_bm25`
(tests/test_readme_claims.py:L193-L211).

### Entity extraction in pipeline
Either `palace.py` contains both the substrings `entities` and `_ENTITY_STOPLIST`,
or `miner.py` contains `extract_entities`; at least one condition must hold
(tests/test_readme_claims.py:L222-L234).

### Noise stripping
`normalize.py` source must define `strip_noise(` (tests/test_readme_claims.py:L245-L252),
and `strip_noise` must be importable and callable from the normalize module
(tests/test_readme_claims.py:L254-L258).

### Module existence and importability
Each of these modules must exist as a file under the package and be importable
without error:
- `diary_ingest.py` (tests/test_readme_claims.py:L269-L285)
- `fact_checker.py` (tests/test_readme_claims.py:L296-L312)
- `closet_llm.py` (tests/test_readme_claims.py:L360-L376)

### Palace graph functions
`palace_graph.py` source must define `find_tunnels(`, `traverse(`, and
`graph_stats(` (tests/test_readme_claims.py:L323-L341), and all three must be
importable and callable from the palace_graph module (tests/test_readme_claims.py:L343-L349).

### Mine lock
`palace.py` must define `mine_lock(` (tests/test_readme_claims.py:L387-L394),
`mine_lock` must be importable and callable (tests/test_readme_claims.py:L396-L400),
and the source must show it is usable as a context manager — indicated by the
presence of either a `@contextlib.contextmanager` decorator or a `def __enter__`
method (tests/test_readme_claims.py:L402-L410).

### Version consistency (version.py vs pyproject.toml)
The version string parsed from `version.py` (the value of `__version__ = "..."`)
must exactly equal the version parsed from `pyproject.toml` (a line-start
`version = "..."`). Failure to parse either, or a mismatch, fails the test
(tests/test_readme_claims.py:L421-L437).

### Version badge (README vs version.py)
README must contain a shields.io badge URL of form `shields.io/badge/version-<v>-`,
and that `<v>` must equal `__version__` from `version.py`. Missing badge or
mismatch fails (tests/test_readme_claims.py:L448-L465).

### AAAK lossy disclaimers
- The first 1000 characters of `dialect.py` must contain `NOT lossless` or
  (case-insensitively) `lossy` (tests/test_readme_claims.py:L476-L485).
- After removing occurrences of `NOT lossless` from that same 1000-character
  region, no remaining `lossless` (case-insensitive) may appear
  (tests/test_readme_claims.py:L487-L497).
- In the modules doc, at least one line must mention `dialect.py`, and no line
  mentioning `dialect.py` may contain `lossless` (case-insensitive)
  (tests/test_readme_claims.py:L514-L528).

### Hall metadata
- `config.py` must contain `DEFAULT_HALL_KEYWORDS` (tests/test_readme_claims.py:L539-L545).
- At least one of `miner.py` or `convo_miner.py` must reference a `hall` metadata
  field — detected by the literal `"hall"` or `'hall'` appearing in either source
  (tests/test_readme_claims.py:L547-L569).
- A third hall-name consistency check is a no-op placeholder that asserts nothing
  (tests/test_readme_claims.py:L571-L585).

### Backend abstraction
- `backends/base.py` must exist as a file and its source must contain `ABC` or
  `abstractmethod` (tests/test_readme_claims.py:L596-L606).
- `backends/chroma.py` must exist as a file and its source must contain
  `BaseCollection` or `base` (tests/test_readme_claims.py:L608-L616).
- `BaseCollection` (from backends.base) and `ChromaBackend` (from backends.chroma)
  must both be importable and non-null (tests/test_readme_claims.py:L618-L624).

### Internationalization
- An `i18n` directory must exist under the package (tests/test_readme_claims.py:L635-L638).
- That directory must contain at least 8 files matching `*.json`
  (tests/test_readme_claims.py:L640-L648).
- `i18n/en.json` must exist as a file (tests/test_readme_claims.py:L650-L655).

### Wake-up token cost
`layers.py` must contain `~600-900 tokens` or `600-900`
(tests/test_readme_claims.py:L678-L681). If README contains any occurrence of
`~170 tokens` or `170 tokens`, the test fails unconditionally, on the rule that
README must not claim 170 tokens when the code documents 600-900
(tests/test_readme_claims.py:L684-L696).

### README pyproject version claim (conditional)
If README contains text matching `pyproject.toml ... v<major.minor.patch>`, the
captured version must equal the actual `pyproject.toml` version; if README has no
such mention, the test passes without assertion (tests/test_readme_claims.py:L707-L723).

### AAAK spec tool handler (conditional)
If the tool key `mempalace_get_aaak_spec` is among the discovered tool keys, then
`mcp_server.py` must define `tool_get_aaak_spec(`; otherwise no assertion is made
(tests/test_readme_claims.py:L753-L761).

## Side Effects and External Contracts

- The suite only reads files and imports modules; it writes nothing. It reads
  README, two website docs, `pyproject.toml`, and multiple package source files
  and data directories under the repo root (tests/test_readme_claims.py:L22-L42).
- Import-based tests cause real module import side effects for the imported
  modules (e.g. closets, normalize, palace_graph, palace, backends, diary_ingest,
  fact_checker, closet_llm) (tests/test_readme_claims.py:L154-L156, L256-L258, L345-L349, L398-L400, L620-L624).
- Observable doc/code contracts enforced: tool-name format `mempalace_<word>` as
  code keys and as ``### `...` `` doc headings; version string equality across
  `version.py`, `pyproject.toml`, and README badge; the shields.io badge URL shape
  (tests/test_readme_claims.py:L37-L53, L421-L465).
