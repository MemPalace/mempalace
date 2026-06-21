# Spec: Source Adapter Contract (`mempalace/sources/base.py`)

Defines the read-side contract every source adapter must implement: the value
objects passed across the boundary, the error taxonomy, and the abstract adapter
base class. A source adapter extracts content from a specific origin (filesystem,
git, Slack, Cursor, etc.) and yields typed records that core routes into the
palace (mempalace/sources/base.py:L1-L16). This module is contract scaffolding;
first-party miners are migrated onto it separately (mempalace/sources/base.py:L9-L15).

## Error taxonomy

All adapter errors derive from a single base error type, `SourceAdapterError`
(mempalace/sources/base.py:L33-L34). The following distinct error categories exist,
each a subtype of the base:

- A "source not found" error is raised when a source reference does not resolve to
  a readable source (mempalace/sources/base.py:L37-L38).
- An "auth required" error is raised when an adapter needs credentials that were
  not provided; the error message MUST name the environment variables (or other
  supported credential mechanism) the operator must set (mempalace/sources/base.py:L41-L46).
- An "adapter closed" error is raised when any adapter method is called after the
  adapter has been closed (mempalace/sources/base.py:L49-L50).
- A "transformation violation" error is raised by the conformance suite when
  round-tripping a drawer would require an undeclared transformation
  (mempalace/sources/base.py:L53-L55).
- A "schema conformance" error is raised when a drawer record's metadata violates
  the schema the adapter declared (mempalace/sources/base.py:L58-L60).

## Value objects (immutable records)

All value objects below are immutable once constructed (frozen) (mempalace/sources/base.py:L68-L68, L84-L84, L93-L93, L108-L108, L124-L124, L135-L135, L147-L147).

### SourceRef — a handle to a source to ingest
Fields, all optional with defaults (mempalace/sources/base.py:L68-L81):
- `local_path` (string, default absent): filesystem-rooted sources such as a
  project directory or mbox file (mempalace/sources/base.py:L72-L73, L79-L79).
- `uri` (string, default absent): URL-like references such as `github.com/org/repo`
  or `slack://workspace/channel` (mempalace/sources/base.py:L73-L74, L80-L80).
- `options` (map, default empty): adapter-specific non-secret config. Secrets MUST
  NOT be placed here (mempalace/sources/base.py:L75-L81).

### RouteHint — adapter-supplied routing hint
Three optional string fields: `wing`, `room`, `hall`, all defaulting to absent
(mempalace/sources/base.py:L84-L90).

### SourceItemMetadata — lightweight pointer yielded before drawers
A pointer emitted ahead of drawers by lazy-fetch adapters so core can decide
whether to skip extraction by inspecting `version` (mempalace/sources/base.py:L93-L100).
Fields:
- `source_file` (string, required) (mempalace/sources/base.py:L102-L102).
- `version` (string, required) (mempalace/sources/base.py:L103-L103).
- `size_hint` (int, optional, default absent) (mempalace/sources/base.py:L104-L104).
- `route_hint` (RouteHint, optional, default absent) (mempalace/sources/base.py:L105-L105).

When an adapter reports an item as current, it stops yielding drawers for that item
and moves to the next (mempalace/sources/base.py:L96-L100).

### DrawerRecord — one drawer of extracted content plus flat metadata
Fields (mempalace/sources/base.py:L108-L121):
- `content` (string, required) (mempalace/sources/base.py:L117-L117).
- `source_file` (string, required) (mempalace/sources/base.py:L118-L118).
- `chunk_index` (int, default 0) (mempalace/sources/base.py:L119-L119).
- `metadata` (map, default empty) (mempalace/sources/base.py:L120-L120).
- `route_hint` (RouteHint, optional, default absent) (mempalace/sources/base.py:L121-L121).

Contract: `metadata` values MUST be flat scalars — string, int, float, or bool.
Nested data belongs on the knowledge graph or in a declared `json_string` field
(mempalace/sources/base.py:L110-L115).

### SourceSummary — high-level source description
Fields: `description` (string, required) and `item_count` (int, optional, default
absent) (mempalace/sources/base.py:L124-L129).

### IngestMode — allowed ingest modes
An ingest mode is exactly one of the literal values `chunked_content`,
`whole_record`, or `metadata_only` (mempalace/sources/base.py:L132-L132).

### FieldSpec — declared shape of one metadata field
Fields (mempalace/sources/base.py:L135-L144):
- `type` (required): one of `string`, `int`, `float`, `bool`,
  `delimiter_joined_string`, `json_string` (mempalace/sources/base.py:L139-L139).
- `required` (bool, required) (mempalace/sources/base.py:L140-L140).
- `description` (string, required) (mempalace/sources/base.py:L141-L141).
- `indexed` (bool, default false) (mempalace/sources/base.py:L142-L142).
- `delimiter` (string, default `;`) (mempalace/sources/base.py:L143-L143).
- `json_schema` (map, optional, default absent) (mempalace/sources/base.py:L144-L144).

### AdapterSchema — per-adapter metadata schema
Fields: `fields` (map from field name to FieldSpec) and `version` (string)
(mempalace/sources/base.py:L147-L152).

### IngestResult — yielded union type
The type yielded from `ingest`; intentionally broad, with runtime type checks
performed in core (mempalace/sources/base.py:L155-L156).

## Adapter base class

The adapter base class is abstract and long-lived, serving many source-reference
invocations (mempalace/sources/base.py:L164-L165). Construction is lightweight: no
I/O, no network, no credential fetch — all work is deferred to `ingest`
(mempalace/sources/base.py:L166-L168). Instances are thread-safe for concurrent
`ingest` calls across different source references; within a single source reference,
v1 serializes (mempalace/sources/base.py:L168-L170).

### Identity / class-level attributes
- `name` (string, no default): stable adapter name used for registration and drawer
  metadata (mempalace/sources/base.py:L172-L174, L187-L187).
- `spec_version` (string, default `"1.0"`) (mempalace/sources/base.py:L188-L188).
- `adapter_version` (string, default `"0.0.0"`): the adapter's own version,
  independent of `spec_version`; recorded on every drawer so re-extract workflows
  can target a known-buggy version (mempalace/sources/base.py:L175-L178, L189-L189).
- `capabilities` (set of strings, default empty): free-form tokens; core inspects a
  documented subset (mempalace/sources/base.py:L178-L179, L190-L190).
- `supported_modes` (set of strings, default `{chunked_content}`): subset of
  `chunked_content`, `whole_record`, `metadata_only`
  (mempalace/sources/base.py:L179-L181, L191-L191).
- `declared_transformations` (set of strings, default empty): transformation names
  the adapter applies to source bytes; the empty set marks a byte-preserving adapter
  (mempalace/sources/base.py:L181-L183, L192-L192).
- `default_privacy_class` (string, default `"pii_potential"`): privacy class applied
  unless palace config overrides it (mempalace/sources/base.py:L183-L185, L193-L193).

### Required methods (must be implemented)

- `ingest(source, palace)` — keyword arguments: a source reference and a palace
  context; returns an iterator/stream of `SourceItemMetadata` and `DrawerRecord`
  values (mempalace/sources/base.py:L199-L212). Ordering contract: lazy adapters
  MUST yield each item's `SourceItemMetadata` ahead of that item's drawers so core
  can check currency before committing to the fetch; eager adapters MAY interleave
  freely (mempalace/sources/base.py:L208-L211).

- `describe_schema()` — returns the structured metadata schema the adapter attaches.
  The schema MUST be stable for a given `adapter_version`; core uses it to validate
  adapter output (mempalace/sources/base.py:L214-L220).

### Optional methods (default implementations provided)

- `is_current(item, existing_metadata)` — returns true if the palace already has an
  up-to-date copy of the item. Default behavior: always returns false (re-extract
  every time). Adapters advertising incremental support MUST override
  (mempalace/sources/base.py:L226-L237).

- `source_summary(source)` — describes a source without extracting. Default behavior:
  returns a summary whose `description` is the adapter's `name`
  (mempalace/sources/base.py:L239-L241).

- `close()` — releases any held resources. Default behavior: no-op
  (mempalace/sources/base.py:L243-L245).

## Side effects
This module is a contract definition only. It defines errors, immutable value
objects, and an abstract class; it performs no I/O, no network access, and no
filesystem or process effects at module load or object construction
(mempalace/sources/base.py:L166-L168).
