# RFC 002 — Source Adapter Plugin Specification

- **Status:** Draft
- **Tracking issue:** [#989](https://github.com/MemPalace/mempalace/issues/989)
- **Related:** [#274](https://github.com/MemPalace/mempalace/issues/274), [#23](https://github.com/MemPalace/mempalace/pull/23), [#169](https://github.com/MemPalace/mempalace/pull/169), [#232](https://github.com/MemPalace/mempalace/pull/232), [#567](https://github.com/MemPalace/mempalace/pull/567), [#98](https://github.com/MemPalace/mempalace/pull/98), [#591](https://github.com/MemPalace/mempalace/pull/591), [#592](https://github.com/MemPalace/mempalace/pull/592), [#702](https://github.com/MemPalace/mempalace/pull/702), [#981](https://github.com/MemPalace/mempalace/issues/981), [#244](https://github.com/MemPalace/mempalace/pull/244), [#419](https://github.com/MemPalace/mempalace/pull/419), [#300](https://github.com/MemPalace/mempalace/pull/300), [#952](https://github.com/MemPalace/mempalace/pull/952), [#389](https://github.com/MemPalace/mempalace/pull/389), [#434](https://github.com/MemPalace/mempalace/pull/434)
- **Sibling spec:** [RFC 001 — Storage Backend Plugin Specification](001-storage-backend-plugin-spec.md)
- **Spec version:** `1.0`

## Summary

A formal contract for MemPalace source adapters so third parties can ship `pip install mempalace-source-<name>` packages (Cursor, OpenCode, git, Slack, Notion, email, calendar, Whisper transcripts, …) that drop into `mempalace mine` without patching core. The spec defines the adapter interface, record shape, metadata schema contract, privacy class, entry-point registration, incremental-ingest semantics, closet integration, a declared-transformation model that replaces the informal "verbatim" promise with a verifiable one, conformance tests, and the refactor of the existing file and conversation miners into first-party adapters on the same contract.

RFC 001 formalized the write side (where drawers are stored). This RFC formalizes the read side (where content comes from). Both are required for MemPalace to function as a durable daemon managing heterogeneous palaces across many source types.

## Motivation

Six source ingesters are currently in flight, each solving the same problem a different way:

| PR / Issue | Source | Mechanism |
|---|---|---|
| [#274](https://github.com/MemPalace/mempalace/issues/274) | Cursor | `workspaceStorage/*.vscdb` SQLite extraction |
| [#23](https://github.com/MemPalace/mempalace/pull/23) | OpenCode | SQLite session database |
| [#169](https://github.com/MemPalace/mempalace/pull/169) | Pi agent | JSONL session normalizer |
| [#232](https://github.com/MemPalace/mempalace/pull/232) | Cursor (JSONL variant) | JSONL normalizer |
| [#567](https://github.com/MemPalace/mempalace/pull/567), [#98](https://github.com/MemPalace/mempalace/pull/98) | Git | `git log` + `gh pr view` with structured diff summary |
| [#591](https://github.com/MemPalace/mempalace/pull/591), [#592](https://github.com/MemPalace/mempalace/pull/592) | Delphi Oracle | Real-time intelligence signals |
| [#702](https://github.com/MemPalace/mempalace/pull/702) | Cursor + factory.ai | Combined session miners |

Plus three ingesters already grafted into core:

- `mempalace/miner.py` — filesystem project miner, fixed char-window chunking, keyword hall routing
- `mempalace/convo_miner.py` — chat transcript miner with exchange-pair chunking
- `mempalace/normalize.py` — format detection for four chat-export shapes (Claude Code JSONL, Codex JSONL, Claude.ai / ChatGPT / Slack JSON)

Plus one open proposal for a different ingest semantic:

- [#981](https://github.com/MemPalace/mempalace/issues/981) — path-level descriptions: mine metadata-as-content instead of raw bytes for matched paths. This is a legitimate third ingest mode (alongside chunked-content and whole-record) that the current architecture has no home for.

Each contributor has reinvented source discovery, source-item identity, incremental-ingest bookkeeping, metadata shape, and chunking strategy. Format detection for new chat exports lands in `normalize.py` as one more branch in an `if` chain. There is no shared abstraction, no conformance suite, and no contract new adapter authors can build against.

This is the same situation RFC 001 addresses for storage backends: a pattern that emerged organically, now needs a specification so the community can contribute cleanly and enterprises can build against a stable surface.

### Why this matters beyond developer tooling

The adapter pattern is source-agnostic. What has so far shown up as "Cursor transcripts" and "git commits" generalizes to:

- **Knowledge work** — Notion, Obsidian, Logseq, Google Docs, iA Writer, Zettlr
- **Communications** — Slack, Discord, Teams, Signal backups, mbox/eml email, iMessage
- **Research** — arXiv PDFs, Zotero libraries, bookmarked articles, Kindle highlights, web archives
- **Creator workflows** — YouTube captions, podcast transcripts (Whisper/Deepgram), Descript projects
- **Regulated domains** — medical records, legal filings, financial statements (all gated on §6 privacy class)

Enterprises key on their own domain metadata — `repo/PR/SHA` for engineering, `patient/encounter/CPT` for healthcare, `case/docket/jurisdiction` for legal. The schema lives in the adapter; the content lives in the drawer. This is how structured-data use cases are served without violating the byte-preservation commitments adapters make.

## Goals

1. A source adapter ships as a standalone Python package; `pip install mempalace-source-<name>` is sufficient to use it.
2. `mempalace mine` and the MCP mine tool are source-agnostic — all extraction goes through registered adapters. No `if source_type == 'foo'` branches in core.
3. Content transformations are **declared** (§1.4): each adapter advertises the set of transformations it applies to source bytes. Byte-preserving adapters declare the empty set. Consumers can programmatically determine what happened to their data.
4. Incremental ingest is cheap and correct: re-running mine only touches items whose source-side version changed, using the palace itself as the cursor (no sidecar).
5. Each adapter declares a structured metadata schema. Enterprises index and filter on that schema. Core is schema-agnostic beyond the universal fields in §5.1.
6. The existing `miner.py` and `convo_miner.py` become the first two first-party adapters on the new contract. Drawer metadata fields and field names are preserved — the spec adds fields, does not rename them.
7. A privacy class is declarable at the adapter boundary so sensitive sources (medical, financial, personal comms) are handled with explicit policy rather than implicit trust.

## Non-goals

- Defining chunking. Each adapter owns its chunking strategy — tree-sitter for code, exchange-pair for chat, whole-record for a PR. Core does not impose a chunk size.
- Defining live-stream / webhook shapes (the Delphi Oracle pattern of continuous signal ingestion). That is a separate future RFC; v1 is pull-mode.
- Defining LLM-based structured extraction. Adapters MAY use an LLM; the spec does not mandate or standardize this.
- Defining cross-adapter dedup. When the same content appears via two adapters (e.g., a PR body mined via `git` and as a conversation quote mined via `claude-code`), both drawers land. Deduplication policy is a separate concern handled at query time by `searcher.py`.
- Defining closet construction. Core continues to build closets from adapter-yielded drawers (§1.7); the closet-building algorithm itself is not part of this spec.

---

## 1. Source adapter contract

### 1.1 Required method

All adapters implement `BaseSourceAdapter` with a single kwargs-only ingest method:

```python
class BaseSourceAdapter(ABC):
    @abstractmethod
    def ingest(
        self,
        *,
        source: SourceRef,
        palace: PalaceContext,
    ) -> Iterator[IngestResult]:
        """Enumerate and extract content from a source.

        Yields a stream of IngestResult values. Lazy adapters yield
        `SourceItemMetadata` ahead of the drawers for that item, so core
        can report progress and check `is_current` before the adapter
        commits to the fetch. Adapters with no lazy-fetch benefit may
        interleave `SourceItemMetadata` and `DrawerRecord` items freely.
        """

    @abstractmethod
    def describe_schema(self) -> AdapterSchema:
        """Declare the structured metadata this adapter attaches.

        Returned value is stable for a given adapter version. Enterprises
        index on this schema; core uses it to validate adapter output.
        """
```

The single-method `ingest()` contract was chosen over a `discover` / `extract` split. Most current ingesters have no meaningful laziness benefit (filesystem walking is cheap, transcript normalizing is cheap). Adapters that do (git-mine's `gh pr list` vs `gh pr view`; hypothetical Slack/Notion API) express laziness by yielding `SourceItemMetadata` first and deferring fetch until core confirms staleness via `is_current()`.

### 1.2 Optional methods (default implementations on the ABC)

```python
def is_current(
    self,
    *,
    item: SourceItemMetadata,
    existing_metadata: dict | None,
) -> bool:
    """Return True if the palace already has an up-to-date copy.

    Called by core after querying the palace for existing drawers with
    matching source_file. The adapter compares its version token against
    the stored metadata and returns True to skip extraction.

    Default implementation: returns False (always re-extract). Adapters
    advertising `supports_incremental` override this.
    """
    return False

def source_summary(self, *, source: SourceRef) -> SourceSummary:
    """Describe a source without extracting (e.g., 'git repo mempalace,
    847 commits, 132 PRs'). Default: returns empty summary."""
    return SourceSummary(description=self.name)

def close(self) -> None:
    return None
```

Core's incremental loop (pseudocode):

```python
for result in adapter.ingest(source=source, palace=ctx):
    if isinstance(result, SourceItemMetadata):
        existing = ctx.collection.get(where={"source_file": result.source_file}, limit=1)
        if adapter.is_current(item=result, existing_metadata=existing):
            ctx.skip_current_item()   # adapter stops yielding drawers for this item
    elif isinstance(result, DrawerRecord):
        ctx.upsert_drawer(result)
```

### 1.3 Typed records

```python
@dataclass(frozen=True)
class SourceRef:
    """A handle to the source a user wants to ingest.

    local_path is for filesystem-rooted sources (project dir, mbox file).
    uri is for URL-like references (github.com/org/repo, slack://workspace/channel).
    options carries adapter-specific config (non-secret values only; §M2).
    """
    local_path: str | None = None
    uri: str | None = None
    options: dict = field(default_factory=dict)

@dataclass(frozen=True)
class SourceItemMetadata:
    """Lightweight pointer yielded before drawers for lazy-fetch adapters."""
    source_file: str                 # Logical identity — filesystem path, PR URI, etc.
    version: str                     # Source-side version token (mtime, commit SHA, ETag, rev id).
    size_hint: int | None = None     # Bytes, if known. Used for progress reporting.
    route_hint: RouteHint | None = None

@dataclass(frozen=True)
class DrawerRecord:
    """One drawer's worth of content plus metadata."""
    content: str                     # Subject to §1.4 declared transformations.
    source_file: str                 # Foreign key to SourceItemMetadata.source_file.
    chunk_index: int = 0             # 0 for single-drawer items; 0..N-1 for chunked items.
    metadata: dict = field(default_factory=dict)  # Flat: str/int/float/bool only. Must conform to adapter schema.
    route_hint: RouteHint | None = None

@dataclass(frozen=True)
class RouteHint:
    wing: str | None = None
    room: str | None = None
    hall: str | None = None

@dataclass(frozen=True)
class SourceSummary:
    description: str
    item_count: int | None = None

# IngestResult is the union type adapters yield.
IngestResult = SourceItemMetadata | DrawerRecord

# PalaceContext carries collection handles, palace config, and progress hooks
# into the adapter. Full definition in §9 (cleanup prerequisite).
```

### 1.4 Declared transformations

Adapters cannot silently alter content. Every adapter declares the set of transformations it applies:

```python
class BaseSourceAdapter(ABC):
    declared_transformations: ClassVar[frozenset[str]] = frozenset()
```

The invariant: **no transformation is applied that is not declared in this set**. Adapters declaring `frozenset()` are byte-preserving end-to-end (modulo the read, which may itself involve `utf8_replace_invalid` — see below).

Reserved transformation names (v1):

| Name | Meaning |
|---|---|
| `utf8_replace_invalid` | Undecodable bytes replaced with U+FFFD on read (equivalent to `open(..., errors="replace")`). |
| `newline_normalize` | CRLF / CR converted to LF. |
| `whitespace_trim` | Leading / trailing whitespace stripped at a record boundary. |
| `whitespace_collapse_internal` | Runs of three or more blank lines collapsed to two. |
| `line_trim` | Each line individually stripped of leading / trailing whitespace. |
| `line_join_spaces` | Adjacent lines joined with single spaces, newlines discarded. |
| `blank_line_drop` | Empty lines between non-empty lines dropped. |
| `strip_tool_chrome` | System tags, hook output, tool UI chrome removed (see `normalize.strip_noise`). |
| `tool_result_truncate` | Tool output heads/tails kept; middle replaced with a marker string. |
| `spellcheck_user` | User turns rewritten by spellcheck. |
| `synthesized_marker` | Adapter inserts its own strings (e.g., `[N lines omitted]`, `[registry] …`, Slack provenance footer). |
| `speaker_role_assignment` | Multi-party speakers alternately assigned `user` / `assistant` roles (Slack). |
| `tool_result_omitted` | Some tool outputs fully omitted from transcript (e.g., Read/Edit/Write results in `normalize._format_tool_result`). |

Adapters MAY define their own transformation names for behaviors the reserved list does not cover. Third-party names SHOULD be prefixed with the adapter name to avoid collisions (e.g., `cursor.composer_ordering`).

**Capability derivation:**
- `byte_preserving` — declared_transformations is empty AND output bytes equal input bytes for any source the adapter can read. Advertised via the `byte_preserving` capability (§2.1). MUST be verified by §7.2 round-trip test.
- `declared_lossy` — declared_transformations is non-empty. The adapter's output is reproducible from source by applying *only* the declared transformations. MUST be verified by §7.3 declared-transformation test.

**Existing code mapping (for the cleanup PR):**

| Module | Declared transformations |
|---|---|
| `filesystem` (current `miner.py`) | `utf8_replace_invalid`, `whitespace_trim` |
| `conversations` (current `convo_miner.py` + `normalize.py`) | `utf8_replace_invalid`, `newline_normalize`, `line_trim`, `line_join_spaces`, `blank_line_drop`, `whitespace_collapse_internal`, `strip_tool_chrome`, `tool_result_truncate`, `tool_result_omitted`, `spellcheck_user`, `synthesized_marker`, `speaker_role_assignment` |

The filesystem adapter is nearly byte-preserving today; the conversations adapter is extensively transformed. Both are honest after this spec lands because both are fully declared.

This replaces the MISSION.md promise of "verbatim always" with a stronger one: every adapter publishes what it does to your data, and the conformance suite verifies it hasn't lied. "Verbatim" becomes a capability some adapters hold (byte_preserving), not a global claim about a lossy pipeline.

### 1.5 Three ingest modes

A single adapter declares one or more of three modes via a class attribute:

```python
class BaseSourceAdapter(ABC):
    supported_modes: ClassVar[frozenset[Literal["chunked_content", "whole_record", "metadata_only"]]]
```

| Mode | Content origin |
|---|---|
| `chunked_content` | Source bytes, split into chunks the adapter chooses (current filesystem behavior). |
| `whole_record` | Source bytes, one drawer per source item (e.g., PR → 1 drawer). |
| `metadata_only` | Synthesized description of a source item (absorbs #981). The description bytes are authored by the user or adapter, not the source. Declared transformations (§1.4) do not apply — content is not derived from source bytes. |

`metadata_only` resolves #981: description-mode matches a path pattern and produces one drawer whose content is the user-authored description rather than the file contents. Conformance tests (§7.2, §7.3) skip `metadata_only` records.

An adapter MAY support multiple modes and select per-item; the per-item mode is recorded in `metadata["ingest_mode"]` (§5.1). This field already exists on conversation drawers (`convo_miner.py:346`) and is the only existing field whose semantics this spec extends rather than preserves.

### 1.6 Chunking delegation

Core does not impose chunking. `miner.py`'s 800-character sliding window is the filesystem adapter's default for unknown file types — not a contract. Adapter authors choose what makes sense:

- Code files → tree-sitter function/class boundaries (future enhancement to the filesystem adapter).
- Chat transcripts → exchange pairs (current `convo_miner.py` behavior).
- PRs → whole-record (current `git-mine` behavior in #567).
- PDFs → page or section.
- Voice transcripts → speaker turn.

The sole cross-adapter requirement for `chunked_content` mode: chunks for a given `source_file`, re-assembled in `chunk_index` order and accounting for declared transformations in §1.4, reproduce the adapter's internal representation of the source. The conformance suite verifies this.

### 1.7 Closet integration

Closets are the AAAK-compressed index layer (`palace.build_closet_lines`, `upsert_closet_lines`) that points to drawer content and enables LLM-scale scanning without reading every drawer. Closet-building is not an adapter concern:

- **Core builds closets** from adapter-yielded drawers as a post-step, via the existing `palace.py` helpers. Adapters do not call these APIs.
- **Adapters MAY emit closet hints** in drawer metadata via a flat `;`-joined string:
  ```python
  metadata["closet_hints"] = "decided GraphQL; migrated to Postgres; fixed PR-567"
  ```
  Core splits on `;` and feeds these as candidate topics alongside the content-scanned ones in `build_closet_lines`. The git adapter can hint decision-signal quotes that raw content-scanning would miss; the conversations adapter can hint section headers; the filesystem adapter has no need and omits the field.
- **metadata_only drawers get closets too.** Core builds them from the synthesized description content the same way it builds closets for any other drawer. This is how #981's path-level descriptions become searchable.
- **Closet purging** remains keyed on `source_file` (`purge_file_closets` in `palace.py:221`). Adapters' source_file values must be stable so purge is correct on re-ingest.

Current `convo_miner.py` does not build closets for conversation drawers — an existing gap. The cleanup PR (§9) routes the conversations adapter through the same post-step closet builder as filesystem, closing the gap as a side effect.

---

## 2. Adapter contract

### 2.1 Identity and capabilities

```python
class BaseSourceAdapter(ABC):
    name: ClassVar[str]                    # "filesystem", "cursor", "git", "slack", ...
    spec_version: ClassVar[str] = "1.0"
    adapter_version: ClassVar[str]         # Independent of spec_version; recorded on every drawer.
    capabilities: ClassVar[frozenset[str]]
    supported_modes: ClassVar[frozenset[str]]             # Per §1.5.
    declared_transformations: ClassVar[frozenset[str]]    # Per §1.4.
    default_privacy_class: ClassVar[str]                  # Per §6.
```

Defined capability tokens (v1):

| Token | Meaning |
|---|---|
| `byte_preserving` | `declared_transformations` is empty AND extracted content equals source bytes. |
| `supports_incremental` | Implements `is_current()` meaningfully; `ingest()` respects `ctx.skip_current_item()`. |
| `supports_structured_metadata` | Attaches fields beyond §5.1 universals. |
| `supports_entity_hints` | Emits entity hints via `metadata["entity_hints_json"]` (§5.4). |
| `supports_kg_triples` | Writes knowledge-graph triples directly to the SQLite KG (§5.5). |
| `supports_closet_hints` | Emits `metadata["closet_hints"]` (§1.7). |
| `requires_auth` | Needs credentials at runtime (env vars — §4.2). |
| `requires_external_service` | Needs a running service (Slack API, email server). |
| `requires_local_tool` | Needs a local binary (`gh`, `rg`, `whisper`). |
| `adapter_owns_routing` | Returns authoritative `RouteHint` values from `ingest()` that core uses as-is (§G3 / §2.5). |
| `respects_privacy_class` | Honors §6 privacy-class filtering. |

Capability tokens are free-form strings; third-party adapters MAY declare novel tokens for their ecosystem. Core only inspects the above.

### 2.2 Source references

See `SourceRef` in §1.3. The shape is deliberately open — adapters parse `uri` and `options` as they see fit. Core does not canonicalize URIs.

**Secrets in `SourceRef.options`:** credentials MUST NOT be placed in `options`. The spec reserves `options` for non-secret values (paths, filters, date ranges). Secrets come from env vars per §4.2. An adapter that reads a credential from `options` violates the spec and MUST be rejected by the conformance suite.

### 2.3 Lifecycle

1. `__init__`: lightweight. No I/O, no network, no credential fetch.
2. First call to `ingest`: may open resources. All I/O is lazy.
3. `close()`: releases all resources. After `close()`, further calls MUST raise `AdapterClosedError`.

### 2.4 Concurrency

An adapter instance is long-lived and serves many mine operations. Adapters MUST be thread-safe for concurrent `ingest` calls across different `SourceRef` values. MemPalace core serializes calls within a single `SourceRef` unless an adapter advertises `supports_parallel_ingest` (not in v1 — reserved for v1.1).

### 2.5 Routing

Routing is the adapter's responsibility. The filesystem adapter reads `mempalace.yaml` (hall keywords, rooms list) via `MempalaceConfig()` and returns `RouteHint(wing=..., room=..., hall=...)` on each drawer. This relocates `detect_room()` and `detect_hall()` (currently in `miner.py` and `convo_miner.py`) into their respective adapters.

Order of precedence for routing:
1. Explicit `--wing` / `--room` CLI flags → passed through `SourceRef.options` → adapter honors verbatim.
2. Palace config match (`mempalace.yaml` hall keywords, room keywords) → adapter computes.
3. Adapter-internal fallback (e.g., filesystem adapter falls back to `"general"` room).

Adapters advertising `adapter_owns_routing` return the final answer; core uses it verbatim. Adapters not advertising it return None and core applies a generic fallback router (writing to wing `default`, room `general`, hall `general`). Absent any adapter, this is how `mempalace mine` behaves today.

### 2.6 Incremental ingest

`is_current()` is the incremental-ingest primitive. The palace itself is the cursor — no separate persisted state. Correctness requirements:

- The adapter's `SourceItemMetadata.source_file` MUST be stable across re-ingests of the same logical item. Filesystem adapter uses the absolute path (as today). Git adapter uses a URI shape like `github.com/org/repo#pr=567` or `github.com/org/repo#commit=abc123`.
- `is_current()` returns True when the stored metadata matches the adapter's current version token. The default implementation returns False (always re-extract) — adapters advertising `supports_incremental` override.
- Deletion tombstones: an adapter MAY yield a `SourceItemMetadata(source_file=..., version="__deleted__")` entry — core purges drawers with matching `source_file` and builds no new drawers for that item. Advertised via `supports_deletion_tombstones`.
- Adapters without `supports_incremental` ignore `is_current()` and fully re-extract. Core logs a warning.

### 2.7 Errors

- `SourceNotFoundError` — the `SourceRef` does not resolve.
- `AuthRequiredError` — adapter needs credentials; raises with a message describing which env vars to set.
- `AdapterClosedError` — method called after `close()`.
- `TransformationViolationError` — conformance suite raises this when the content round-trip requires an undeclared transformation.
- `SchemaConformanceError` — a `DrawerRecord.metadata` is missing required fields declared in `describe_schema()` or violates declared types.

---

## 3. Registration and discovery

### 3.1 Entry points (primary mechanism)

Third-party adapters ship as installable packages:

```toml
# pyproject.toml of mempalace-source-cursor
[project.entry-points."mempalace.sources"]
cursor = "mempalace_source_cursor:CursorAdapter"
```

MemPalace discovers adapters at process start via `importlib.metadata.entry_points(group="mempalace.sources")`.

### 3.2 In-tree registry (secondary)

```python
from mempalace.sources.registry import register

register("my-experimental-adapter", MyAdapter)
```

Entry-point discovery and explicit `register()` populate the same registry. Explicit registration wins on name conflict.

### 3.3 Selection (explicit only — no auto-detect)

Unlike storage backends (RFC 001 §3.3), source adapters are never auto-detected. The user selects the adapter explicitly:

```bash
mempalace mine --source cursor ~/                      # explicit adapter
mempalace mine --source git /path/to/repo              # explicit adapter
mempalace mine --source filesystem /path/to/project    # explicit adapter
mempalace mine /path/to/project                        # implicit: filesystem (default)
```

The default when no `--source` is given is `filesystem`, preserving current `mempalace mine <path>` behavior.

**Backwards compatibility with `--mode`.** Current `cli.py:517-519` exposes `--mode {projects,convos}`. This spec maps:
- `--mode projects` → `--source filesystem` (the new default)
- `--mode convos` → `--source conversations`

`--mode` stays as a deprecated alias through v4.x with a deprecation warning on use; removed in v5.0.

Auto-detection would be hostile — a directory containing a `.git` folder, a `workspaceStorage/` subdir, and an `mbox` file is not a signal of user intent.

---

## 4. Configuration

### 4.1 Shape

```json
{
  "sources": {
    "my-cursor": {
      "type": "cursor",
      "workspace_storage": "~/Library/Application Support/Cursor/User/workspaceStorage"
    },
    "my-git": {
      "type": "git",
      "repos": ["/projects/mempalace", "/projects/site"]
    }
  },
  "palaces": {
    "work": {
      "sources": ["my-git"],
      "privacy_floor": "internal"
    },
    "personal": {
      "sources": ["my-cursor"]
    }
  }
}
```

Single-user local mode: config is optional. `mempalace mine <path>` with no config uses the `filesystem` adapter and defaults.

### 4.2 Environment variables

- `MEMPALACE_SOURCE_<NAME>_*` — per-adapter secrets and connection info. Examples: `MEMPALACE_SOURCE_SLACK_TOKEN`, `MEMPALACE_SOURCE_NOTION_API_KEY`, `MEMPALACE_SOURCE_GIT_GITHUB_TOKEN`.
- Secrets MUST be readable from env vars; config files carry structure, env vars carry credentials. Same rule as RFC 001 §4.2.

### 4.3 Adapter-specific options

`SourceRef.options` is a free-form dict of non-secret values (§2.2). Each adapter documents its accepted keys. Unknown keys MUST be ignored (forward compatibility); the adapter MAY log a warning.

---

## 5. Metadata schema contract

### 5.1 Universal fields

Existing drawer metadata fields are preserved — the spec adds the following:

| New field | Type | Added by | Purpose |
|---|---|---|---|
| `adapter_name` | `str` | core, from `BaseSourceAdapter.name` | Which registered source produced this drawer. |
| `adapter_version` | `str` | adapter | Adapter's own version (distinct from palace `normalize_version`). Enables re-extract workflows targeted at drawers from a known-buggy adapter version. |
| `privacy_class` | `str` | adapter default, config override | Per §6. |

Existing fields retain their current semantics (verified against `miner.py:542-561` and `convo_miner.py:338-350`):

| Existing field | Role in the spec |
|---|---|
| `source_file` | Functions as the adapter's source-item identifier. Adapter defines the shape — a filesystem path for filesystem, a URI like `github.com/org/repo#pr=123` for git. MUST be stable across re-ingests of the same logical item. |
| `source_mtime` | Functions as the source-item version for filesystem. Adapters without mtime semantics MAY omit this field and use a different version discriminator (e.g., commit SHA in a separate `metadata["commit_sha"]` field); the spec only requires that `is_current()` can decide staleness from the stored metadata. |
| `filed_at` | When the record was written. ISO-8601 string. |
| `added_by` | Agent name (e.g., `lumi`, `claude-code`). Orthogonal to `adapter_name` — the agent is *who* triggered mining; the adapter is *how* data was extracted. |
| `wing`, `room`, `hall` | Palace routing. Populated by adapter per §2.5. |
| `chunk_index` | Per §1.6. Always 0 for `whole_record` / `metadata_only`. |
| `normalize_version` | Palace-wide schema version (currently `palace.py:50`). Unchanged. Separate from `adapter_version`. |
| `entities` | Semicolon-joined candidate entity names. Already flat; kept flat (§5.4 replacement). |
| `ingest_mode` | Per §1.5. Already on conversation drawers; added to filesystem drawers by the cleanup PR. |
| `extract_mode` | Conversation-adapter-specific (`exchange` vs `general`). Moves into the conversations adapter's declared schema per §5.2. |

**Nothing is renamed. Nothing is removed.** The spec formalizes the shape ingesters already converge on. Existing `where={"source_file": ...}` queries in `searcher.py`, `palace.py`, and callers keep working.

**Chroma metadata constraint:** all metadata values MUST be `str | int | float | bool`. No lists, no nested dicts. This matches RFC 001 §1.4 and the underlying ChromaDB contract. Structured side-data goes to the SQLite knowledge graph (§5.5) or to a declared flat JSON-encoded string field (§5.4).

### 5.2 Adapter schemas

Each adapter returns an `AdapterSchema` from `describe_schema()`:

```python
@dataclass(frozen=True)
class AdapterSchema:
    fields: dict[str, FieldSpec]   # Keyed by metadata key.
    version: str

@dataclass(frozen=True)
class FieldSpec:
    type: Literal["string", "int", "float", "bool", "delimiter_joined_string", "json_string"]
    required: bool
    description: str
    indexed: bool = False           # Hint to backends that can build indexes (RFC 001 §2.1).
    # delimiter_joined_string: the delimiter character (default ";").
    delimiter: str = ";"
    # json_string: the JSON schema of the encoded object (informational only).
    json_schema: dict | None = None
```

`delimiter_joined_string` covers the `entities` shape (current `;`-joined list of names). `json_string` is the escape hatch for adapters needing to pack nested data — the value stored is still a single flat `str` from Chroma's perspective, but the adapter is allowed to document its parsed shape.

Example for a hypothetical `slack` adapter:

```python
AdapterSchema(
    version="1.0",
    fields={
        "channel_name": FieldSpec(type="string", required=True, description="Slack channel name", indexed=True),
        "channel_id": FieldSpec(type="string", required=True, description="Slack channel ID"),
        "thread_ts": FieldSpec(type="string", required=False, description="Thread root timestamp"),
        "author_id": FieldSpec(type="string", required=True, description="Slack user ID", indexed=True),
        "author_name": FieldSpec(type="string", required=True, description="Display name at extraction time"),
        "reactions": FieldSpec(type="delimiter_joined_string", required=False, description="Emoji shortcodes"),
    },
)
```

### 5.3 Enterprise keying

The adapter schema is the stable surface enterprises filter on. A support team querying the palace for `channel_id = "C01234"` does not care about ChromaDB's internal representation. The schema field is declared by the adapter, indexed by the backend (RFC 001 §2.1 `supports_metadata_filters`), and exposed through the existing `where=` clause.

This is how "structured data" serves company use cases without breaking transformation guarantees: declared-transformation content in the drawer, structured fields in the metadata, schema declared by the adapter, filtering done by the backend.

### 5.4 Entity hints (optional)

Adapters with `supports_entity_hints` MAY include:

```python
metadata["entity_hints_json"] = '[{"type":"person","name":"Milla Jovovich","confidence":0.95,"offset":120},{"type":"project","name":"MemPalace","confidence":1.0,"offset":0}]'
```

The value is a JSON-encoded string (type `json_string` in the adapter schema). Core parses on read and feeds into `mempalace/entity_detector.py` as a prior: hints with `confidence >= 0.9` bypass the heuristic detector; lower-confidence hints feed into it as candidates.

This is additive to the existing flat `entities` field — entity_hints carries structure (type, confidence, offset); `entities` remains the Chroma-indexable flat string. An adapter that produces entity_hints MUST also populate `entities` as the flat name-only projection, so existing filter queries keep working.

### 5.5 Knowledge-graph triples (optional)

Adapters with `supports_kg_triples` write directly to the SQLite knowledge graph via `mempalace/knowledge_graph.py` — **not** to drawer metadata. Chroma cannot store structured triples; the KG already exists for this purpose.

The adapter calls the existing `KnowledgeGraph.add_triple()` (signature verified against `mempalace/knowledge_graph.py:130`):

```python
palace.kg.add_triple(
    subject="Ben",
    predicate="committed",
    obj="PR-567",                    # `object` is a Python builtin — the API uses `obj`.
    valid_from="2026-03-12",
    confidence=1.0,
    source_file=drawer.source_file,  # Existing provenance parameter.
)
```

Drawer metadata includes a flat counter — `metadata["kg_triples_count"]: int` — so search consumers can see at a glance that KG side-data exists for a drawer without hitting SQLite.

The existing API has `source_closet` and `source_file` provenance parameters but no `source_drawer_id` or `adapter_name`. The cleanup PR (§9) should add these two optional parameters to `add_triple()` so adapter-written triples can be traced back to (a) the specific drawer that produced them and (b) the adapter that authored them — necessary for re-extraction workflows. Until that lands, adapters use `source_file` as the provenance key and record adapter authorship via a separate table or a predicate naming convention (e.g., `adapter:git:committed`).

This aligns with the existing architecture in `CLAUDE.md` ("Knowledge Graph: ENTITY → PREDICATE → ENTITY with valid_from / valid_to dates") — the RFC formalizes the adapter-side write path.

### 5.6 Source encoding and newline

Current ingesters handle encoding lossily (`errors="replace"` in `miner.py:595` and `normalize.py:124`) and do not record original encoding. The spec does **not** require per-drawer `source_encoding` / `source_newline` — most runs are uniform UTF-8 / LF, and storing the same value on every drawer wastes bytes.

Instead: adapters that handle non-UTF-8 or non-LF sources record the values once on the adapter's `SourceSummary` and per-drawer only when a specific drawer diverges from the adapter default. The `utf8_replace_invalid` declared transformation (§1.4) already communicates that lossy decoding happened; specific drawer-level provenance is opt-in.

---

## 6. Privacy class

### 6.1 Defined levels

| Level | Meaning | Example sources |
|---|---|---|
| `public` | Content intended for public consumption. | arXiv papers, public GitHub repos, published blogs. |
| `internal` | Organizational content, not for public disclosure. | Corporate Slack, internal Notion, private git repos. |
| `pii_potential` | May contain personally identifiable information. | Email, iMessage, Claude/ChatGPT transcripts. |
| `sensitive` | Known to contain PII, financial, or health data. | Medical records, financial statements, legal filings. |
| `secrets_possible` | May contain credentials or secrets. | Git history, environment dumps, CI logs. |

An adapter declares a default on `BaseSourceAdapter.default_privacy_class`. Users MAY override per-source in config.

### 6.2 Enforcement

- Each palace declares a `privacy_floor`. Drawers above the floor (equal to or laxer) are admitted; drawers below are rejected at write time and surfaced in a `rejected` list on the CLI and MCP tool.
- **Default floor: none** — v1 accepts all levels unless the palace explicitly configures a floor. This keeps the single-user local default low-friction (users who run `mempalace mine` on a git repo expect `secrets_possible` drawers to land). Enterprise deployments MUST set a floor; docs for regulated-domain setup will recommend starting strict and relaxing as needed.
- Search results surface `privacy_class` in result metadata. MCP tool wrappers MAY redact results above a caller-declared ceiling.
- `secrets_possible` drawers SHOULD pass through a secrets-scan pre-index hook when one is available. PR #389 (sensitive content scanner) is the expected enforcement mechanism for v1; until it lands, `secrets_possible` is a label without automated scanning. The label is still useful — it enables floor-based rejection and alerts downstream consumers.
- The privacy class is recorded in drawer metadata and cannot be downgraded without a migration log entry, matching RFC 001's embedder-identity pattern.

Privacy class is how a regulated-domain deployment (medical, legal, financial) can use MemPalace safely. Without it, flexible ingest becomes a liability; with it, ingest is scoped by policy.

---

## 7. Testing contract

### 7.1 The abstract suite

MemPalace ships `mempalace.sources.testing.AbstractSourceAdapterContractSuite` — a pytest mixin. Every adapter package ships a concrete subclass:

```python
from mempalace.sources.testing import AbstractSourceAdapterContractSuite

class TestCursorAdapter(AbstractSourceAdapterContractSuite):
    @pytest.fixture
    def adapter(self):
        return CursorAdapter()

    @pytest.fixture
    def fixture_source(self, tmp_path):
        """Build a minimal Cursor workspaceStorage fixture."""
        ...
        return SourceRef(local_path=str(tmp_path))

    @pytest.fixture
    def canonical_source_bytes(self, fixture_source):
        """Return a mapping of source_file -> authoritative bytes.

        For filesystem sources: the file's raw bytes.
        For SQLite sources: the extracted value column bytes for each row.
        For API sources: the canonical HTTP response body bytes.

        Adapter-defined — the adapter knows what its 'source bytes' are.
        """
        ...
```

The suite covers:

- `ingest` yields items with stable `source_file` and well-formed `version`.
- `is_current()` returns True when metadata matches, False when it differs.
- `close()` releases resources; subsequent calls raise `AdapterClosedError`.
- Unicode content and unicode identifiers are preserved end-to-end.
- Large-source handling: 10k+ items ingest without loading all into memory.
- Error paths: `SourceNotFoundError`, `AuthRequiredError` raise with correct types.
- `SourceRef.options` MUST NOT contain secrets — the adapter raises if it detects a value matching a common-secret pattern (GitHub token prefix, Slack token prefix, etc.). Advisory test, not blocking.

### 7.2 Byte-preserving round-trip (for `byte_preserving` adapters only)

Required for adapters advertising `byte_preserving`:

```python
def test_byte_preserving_round_trip(self, adapter, fixture_source, canonical_source_bytes):
    """Concatenated chunks must equal the canonical source bytes.

    For each source_file in the fixture:
      1. Read canonical_source_bytes[source_file].
      2. Collect all DrawerRecords for that source_file from adapter.ingest(...).
         Skip metadata_only drawers (§1.5).
      3. Sort by chunk_index.
      4. Concatenate record.content values.
      5. Assert equality with the canonical bytes (UTF-8 decoded).
    """
```

Failure raises `TransformationViolationError`.

### 7.3 Declared-transformation round-trip (for `declared_lossy` adapters)

Required for adapters with non-empty `declared_transformations`:

```python
def test_declared_transformation_round_trip(self, adapter, fixture_source, canonical_source_bytes):
    """Adapter output must be reproducible by applying ONLY declared transformations.

    1. For each source_file, read canonical_source_bytes.
    2. Apply each declared transformation in declared_transformations to the bytes,
       in the order declared by the adapter, using the reference implementations
       in mempalace.sources.transforms.
    3. Compare the result to the concatenated record.content values.
    4. If they differ, the adapter has applied a transformation it did not declare.
       Raise TransformationViolationError.
    """
```

For transformations not in the reserved list (§1.4) — adapter-custom names — the adapter MUST provide a reference implementation callable under `mempalace.sources.transforms.<adapter_name>_<transform_name>`. The conformance suite imports and applies it. Undiscoverable custom transforms fail the test.

### 7.4 Schema conformance

A generator-based property test validates that every record yielded by `ingest` across the fixture source has metadata matching `describe_schema()`. Missing required fields, wrong types, or (in strict mode) undeclared fields fail the test.

### 7.5 Note on current corpus

No existing test in `tests/` asserts byte-preservation or declared-transformation correctness (verified via grep of `tests/` for `verbatim|byte.?preserv|round.?trip`). This RFC's conformance suite introduces the first such coverage. The existing MISSION.md claim of "verbatim always" is a social contract until this lands; afterward it becomes a machine-verified property of adapters that declare `byte_preserving`.

---

## 8. Versioning and compatibility

- `BaseSourceAdapter.spec_version` declares which spec version an adapter implements.
- MemPalace refuses to load an adapter declaring a different major spec version.
- Minor spec versions are additive: new optional methods, new capability tokens, new reserved transformation names, new universal metadata fields with sensible defaults.
- Adapters MAY declare their own `adapter_version` independent of the spec version; this is recorded on every drawer (§5.1) and enables "this drawer was extracted by cursor-adapter 0.3; 0.4 fixed a parsing bug; re-extract affected drawers" workflows.
- This is spec v1.0.

---

## 9. Cleanup prerequisite (not in this spec, but gating)

The existing in-tree ingesters are not adapter-shaped. Before RFC 002 can be enforced, the following refactor lands in a separate PR:

- Introduce `mempalace/sources/base.py` defining `BaseSourceAdapter`, the typed records, and the registry.
- Introduce `mempalace/sources/transforms.py` with reference implementations of every reserved transformation in §1.4. Adapters and the conformance suite both consume these.
- `mempalace/miner.py` → `mempalace/sources/filesystem.py` implementing `BaseSourceAdapter`. Current behavior preserved: 800-char chunking becomes the adapter's default; `READABLE_EXTENSIONS` moves to the adapter; `detect_room()` and `detect_hall()` move to the adapter per §2.5. `declared_transformations = frozenset({"utf8_replace_invalid", "whitespace_trim"})`.
- `mempalace/convo_miner.py` → `mempalace/sources/conversations.py`. Exchange-pair chunking stays. The format-detection logic in `normalize.py` becomes per-format plugins the conversations adapter composes (one for Claude Code JSONL, one for Codex JSONL, one for ChatGPT mapping trees, one for Claude.ai JSON, one for Slack JSON) — each small and independently testable, eliminating the `if source_type` chain. `declared_transformations` enumerates every transformation `normalize.py` and `convo_miner._chunk_by_exchange` actually perform (see §1.4 "Existing code mapping").
- Closet-building wired into the conversations adapter's post-step (currently missing, per §1.7) — side effect of routing through the unified core post-step.
- `mempalace/cli.py` subcommand `mine` routes through the `mempalace.sources` registry. `--mode {projects,convos}` becomes a deprecated alias for `--source {filesystem,conversations}`.
- `mempalace/mcp_server.py` `mempalace_mine` tool accepts a `source` parameter.
- `mempalace/palace.py` exposes `PalaceContext` — a per-mine-invocation facade that bundles the drawer collection, closet collection, knowledge graph, palace config, and progress hooks. Adapters receive this; they do not import `palace.py` directly.
- `NORMALIZE_VERSION` (currently a module-level constant in `palace.py:50`) stays. It is the palace-wide schema version, orthogonal to per-adapter `adapter_version`.
- `KnowledgeGraph.add_triple()` (`knowledge_graph.py:130`) gains two optional parameters: `source_drawer_id: str = None` and `adapter_name: str = None`. Existing callers are unaffected; adapters advertising `supports_kg_triples` (§5.5) populate both. Backwards-compatible change.

This cleanup is substantial — comparable to RFC 001 §10's chroma-import removal — and should land before any new third-party adapter PR merges. Each new adapter is easier after the cleanup, not harder.

---

## 10. Impact on in-flight PRs

| PR / Issue | Effort to align |
|---|---|
| [#274](https://github.com/MemPalace/mempalace/issues/274) Cursor SQLite | Becomes `mempalace-source-cursor` third-party package. Author has a working prototype on Windows; needs `describe_schema()`, `declared_transformations`, and the conformance suite. Prior #287 (closed unmerged) is predecessor work. |
| [#23](https://github.com/MemPalace/mempalace/pull/23) OpenCode SQLite | Becomes `mempalace-source-opencode`. Same shape as Cursor. |
| [#169](https://github.com/MemPalace/mempalace/pull/169) Pi agent | Becomes `mempalace-source-pi` or a format plugin under the conversations adapter (depending on format similarity). |
| [#232](https://github.com/MemPalace/mempalace/pull/232) Cursor JSONL | Deprecated in favor of #274's SQLite path; or a second mode of `mempalace-source-cursor`. |
| [#567](https://github.com/MemPalace/mempalace/pull/567), [#98](https://github.com/MemPalace/mempalace/pull/98) git-mine | Closest existing work to what the spec envisions. Becomes first-party `mempalace/sources/git.py`. Exercises `whole_record` mode, `supports_structured_metadata`, `supports_closet_hints` (decision-signal quotes), `supports_kg_triples` (commit authorship, PR review relationships). |
| [#591](https://github.com/MemPalace/mempalace/pull/591), [#592](https://github.com/MemPalace/mempalace/pull/592) Delphi Oracle | Deferred. The live-stream pattern is out of scope for v1 (§Non-goals). A v1.1 addition will specify webhook/stream adapters. |
| [#702](https://github.com/MemPalace/mempalace/pull/702) Cursor + factory.ai | Splits into two adapter packages. |
| [#981](https://github.com/MemPalace/mempalace/issues/981) path-level descriptions | Absorbed by §1.5 `metadata_only` mode + §5.1 `ingest_mode`. A new first-party `descriptions` adapter or a second mode on `filesystem`. |
| [#244](https://github.com/MemPalace/mempalace/pull/244) Cursor memory-first MCP workflow docs | Points at `mempalace-source-cursor` once the adapter lands. |
| [#419](https://github.com/MemPalace/mempalace/pull/419), [#300](https://github.com/MemPalace/mempalace/pull/300), [#952](https://github.com/MemPalace/mempalace/pull/952) language-extension additions to `READABLE_EXTENSIONS` | Becomes per-language config on the filesystem adapter. Contributors can publish domain-specific adapters without touching core. |
| [#389](https://github.com/MemPalace/mempalace/pull/389) sensitive content scanner | Expected enforcement mechanism for the `secrets_possible` privacy class (§6.2). Not a blocker for this spec, but a natural consumer. |
| [#434](https://github.com/MemPalace/mempalace/pull/434) auto-populate KG from drawers | Complementary: post-hoc derivation of KG triples from drawer content. Adapters with `supports_kg_triples` provide the up-front path; #434 handles everything else. |

---

## 11. Open questions

1. **Cross-adapter dedup.** When a PR body is mined via `git` AND shows up as a conversation quote mined via `claude-code`, both drawers land. Is query-time dedup in `searcher.py` sufficient, or should core maintain a content-hash index across adapters? Declared non-goal in v1 but worth revisiting if user feedback demands it.
2. **Live-stream pattern.** Delphi Oracle (#591/592) and potentially Slack/Discord real-time ingestion need a push-mode contract. This is a v1.1 addition (streaming adapter trait + webhook surface), not blocking.
3. **LLM-assisted structured extraction.** Some adapters will want to call an LLM to extract structured fields. The spec does not standardize this — should it? Argument for: conformance test for LLM-driven fields, consistent caching. Argument against: local-first / zero-API is a core promise; LLM dependencies are opt-in per adapter.
4. **Adapter-vs-format split for conversations.** §9 proposes format plugins composed under a single conversations adapter. Alternative: one adapter per format (claude-code, chatgpt, codex, cursor-jsonl, slack). The trade-off is discoverability (one adapter is easier to find) vs. encapsulation (format plugins are simpler to test). Preference leans toward the single-adapter + plugin model; open to counter-argument.
5. **Default `privacy_floor`.** v1 defaults to none (§6.2) so single-user local mining is frictionless. An argument exists for defaulting to `pii_potential` — forces regulated-domain users to opt in to sensitive levels rather than opt out. Open to changing the default before v1 ships.
6. **`canonical_source_bytes` for API-backed adapters.** §7.1 defines this as adapter-declared. For API-backed adapters (Slack, Notion), what constitutes "canonical bytes" in a conformance test — the fixture's captured HTTP response? A serialized representation of the parsed object? Leaves to the adapter; may need a follow-up spec for common conventions.
7. **`adapter_version` bump semantics.** When does an adapter bump `adapter_version`? On any behavior change? On declared-transformation changes only? Suggests a follow-up doc on adapter SemVer conventions for the community to agree on.

---

## 12. Rollout

1. Land the cleanup PR (§9): introduce `mempalace/sources/`, refactor `miner.py` → filesystem adapter, `convo_miner.py` → conversations adapter, route CLI and MCP through the sources registry. Behavior preserved end-to-end. Closets get built for conversation drawers as a side effect.
2. Land this spec as-is. Add `AbstractSourceAdapterContractSuite`, entry-point discovery, `AdapterSchema` validation, privacy-class enforcement (floor-gated writes), declared-transformation reference implementations in `mempalace/sources/transforms.py`.
3. Land `mempalace/sources/git.py` as the first-party adapter absorbing #567. Exercises `whole_record`, `supports_structured_metadata`, `supports_closet_hints`, `supports_kg_triples` together.
4. Encourage the Cursor (#274), OpenCode (#23), and Pi (#169) authors to publish as third-party packages under `mempalace-source-*`. Offer review help against the spec.
5. Publish adapter-authoring docs at [mempalaceofficial.com/guide/authoring-sources](https://mempalaceofficial.com/guide/authoring-sources.html).
6. Update [ROADMAP.md](../../ROADMAP.md) with spec v1.0 adoption under v4.0.0-alpha.
