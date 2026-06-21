# Behavior Spec: tests/test_sources.py

This file is a test suite verifying the RFC 002 source-adapter scaffolding (tests/test_sources.py:L1-L1). It does not itself constitute production behavior; instead each test pins an externally observable contract of the `mempalace.sources` package, its `transforms` submodule, `mempalace.sources.context`, and `mempalace.knowledge_graph`. This spec records those pinned contracts so they can be re-implemented in any language. Each claim below cites the test that enforces it.

## Public Surface Under Test

The `mempalace.sources` package exports these names: `AdapterSchema`, `BaseSourceAdapter`, `DrawerRecord`, `FieldSpec`, `PalaceContext`, `RouteHint`, `SourceItemMetadata`, `SourceRef`, `SourceSummary`, `available_adapters`, `get_adapter`, `get_adapter_class`, `register`, `reset_adapters`, `resolve_adapter_for_source`, and `unregister` (tests/test_sources.py:L8-L25). The `mempalace.sources.transforms` submodule exports `RESERVED_TRANSFORMATIONS`, `blank_line_drop`, `get_transformation`, `line_join_spaces`, `line_trim`, `newline_normalize`, `utf8_replace_invalid`, `whitespace_collapse_internal`, and `whitespace_trim` (tests/test_sources.py:L26-L36).

## Test Fixture: registry isolation

An auto-applied fixture runs after each test: it calls `reset_adapters()` and then `unregister(name)` for every name currently returned by `available_adapters()`, leaving the adapter registry empty between tests (tests/test_sources.py:L63-L68). Implication: `reset_adapters()` clears cached instances and `unregister(name)` removes a registration; together they fully empty the registry.

## BaseSourceAdapter (abstract base)

`BaseSourceAdapter` is abstract: a subclass that does not implement the required methods (e.g. one that only sets `name`) cannot be instantiated — construction raises an error (tests/test_sources.py:L76-L82). A subclass is "conforming" when it defines class attributes `name`, `adapter_version`, `capabilities`, `supported_modes`, `declared_transformations`, `default_privacy_class`, and implements `ingest(self, *, source, palace)` and `describe_schema(self)` (tests/test_sources.py:L44-L60).

`ingest` is keyword-only on `source` and `palace` and yields a sequence of typed records (a generator). For the trivial conforming adapter it yields exactly two records in order: first a `SourceItemMetadata`, then a `DrawerRecord` (tests/test_sources.py:L52-L54, L85-L91). The `DrawerRecord` carries the content string it was constructed with (tests/test_sources.py:L91).

`is_current(self, *, item, existing_metadata)` defaults to returning `False` regardless of arguments — both when `existing_metadata` is absent and when it matches the item's version — meaning the default policy always re-extracts (tests/test_sources.py:L94-L98).

`source_summary(self, *, source)` returns a `SourceSummary` whose `description` defaults to the adapter's `name` attribute when not overridden (tests/test_sources.py:L101-L105).

`describe_schema()` returns an `AdapterSchema` with a `version` string and a `fields` mapping from field name to `FieldSpec`; `FieldSpec` carries `type`, `required`, and `description` (tests/test_sources.py:L56-L60).

## SourceRef

`SourceRef(uri=...)` is a frozen value object with an `options` mapping that defaults to a fresh empty dict per instance. Mutating one instance's `options` MUST NOT affect another instance's `options` (no shared default) (tests/test_sources.py:L108-L113). `uri` is readable on the record (tests/test_sources.py:L53-L54, L87).

## SourceItemMetadata and DrawerRecord

`SourceItemMetadata` is constructed with `source_file` and `version` (tests/test_sources.py:L53, L96). `DrawerRecord` is constructed with `content`, `source_file`, `chunk_index`, and an optional `metadata` mapping (tests/test_sources.py:L54, L266-L271).

## RouteHint

`RouteHint` is a frozen value object constructed with `wing`, `room`, and `hall`, all readable as attributes (tests/test_sources.py:L342-L344).

## transforms submodule

`RESERVED_TRANSFORMATIONS` enumerates exactly these 13 names, no more and no fewer: `utf8_replace_invalid`, `newline_normalize`, `whitespace_trim`, `whitespace_collapse_internal`, `line_trim`, `line_join_spaces`, `blank_line_drop`, `strip_tool_chrome`, `tool_result_truncate`, `tool_result_omitted`, `spellcheck_user`, `synthesized_marker`, `speaker_role_assignment` (tests/test_sources.py:L121-L137).

Transformation behaviors (each takes input and returns the transformed value):

- `utf8_replace_invalid` accepts raw bytes and decodes to a string, replacing any invalid UTF-8 byte with the Unicode replacement character U+FFFD: `b"ok \xff end"` -> `"ok � end"` (tests/test_sources.py:L140-L142).
- `newline_normalize` converts CRLF (`\r\n`) and lone CR (`\r`) to LF (`\n`): `"a\r\nb\rc\nd"` -> `"a\nb\nc\nd"` (tests/test_sources.py:L145-L146).
- `whitespace_trim` strips leading and trailing whitespace from the whole string: `"  hello\n\n"` -> `"hello"` (tests/test_sources.py:L149-L150).
- `whitespace_collapse_internal` caps runs of internal blank lines at two blank lines (three consecutive newlines maximum): `"a\n\n\n\n\nb"` -> `"a\n\n\nb"` (tests/test_sources.py:L153-L156).
- `line_trim` strips leading/trailing whitespace from each line independently: `"  a  \n\t b \n c"` -> `"a\nb\nc"` (tests/test_sources.py:L159-L160).
- `line_join_spaces` joins consecutive non-blank lines within a paragraph using single spaces, while preserving paragraph breaks (blank lines): `"foo\nbar\nbaz\n\nqux\nquux"` -> `"foo bar baz\n\nqux quux"` (tests/test_sources.py:L163-L165).
- `blank_line_drop` removes blank lines only, leaving non-blank lines joined by single newlines: `"a\n\nb\n\n\nc"` -> `"a\nb\nc"` (tests/test_sources.py:L168-L169).

`get_transformation(name)` resolves a reserved transformation name to its implementation (e.g. `"newline_normalize"` resolves to the `newline_normalize` function) and raises a key-lookup error for an unknown name (tests/test_sources.py:L172-L175).

## registry (register/get/unregister/resolve)

`register(name, adapter_class)` records an adapter class under a string name; afterward `name` appears in `available_adapters()` (tests/test_sources.py:L183-L185). `get_adapter(name)` returns an instance of the registered class, and the instance is cached: repeated calls with the same name return the identical instance (tests/test_sources.py:L186-L189). `get_adapter_class(name)` returns the registered class itself, not an instance (tests/test_sources.py:L192-L194). `get_adapter(name)` for an unknown name raises a key-lookup error (tests/test_sources.py:L197-L199).

`unregister(name)` removes the registration and the cached instance: afterward the name is absent from `available_adapters()` and `get_adapter(name)` raises a key-lookup error (tests/test_sources.py:L202-L208).

`resolve_adapter_for_source(explicit=..., config_value=...)` selects an adapter name by priority: an explicit value wins over a config value (`explicit="cursor", config_value="git"` -> `"cursor"`); a config value wins over the default (`config_value="git"` -> `"git"`); with no arguments the default is `"filesystem"`, preserving existing `mempalace mine <path>` behavior (tests/test_sources.py:L211-L217).

`available_adapters()` returns a list and MUST NOT raise even when no first-party adapters are registered; the result may be empty or contain zero-or-more entries (tests/test_sources.py:L455-L460). The `mempalace.sources` entry-point group is declared so third-party packages can register adapters even though no in-tree adapters exist yet (tests/test_sources.py:L455-L458).

## PalaceContext

`PalaceContext` is constructed with keyword arguments `drawer_collection`, `knowledge_graph`, `palace_path`, and optionally `adapter_name`, `adapter_version`, and `progress_hooks` (tests/test_sources.py:L259-L265, L307-L312, L329-L334).

`upsert_drawer(record)` writes exactly one upsert to the drawer collection. The upsert payload contains: `documents` equal to a single-element list of the record content (`["hello"]`); `ids` equal to a single-element list; and `metadatas` equal to a single-element list whose metadata mapping merges the record's own metadata (e.g. `wing="proj"`) with stamped adapter provenance — `adapter_name` and `adapter_version` from the context, plus `source_file` and `chunk_index` copied from the record (tests/test_sources.py:L256-L283).

Drawer id construction (`mempalace.sources.context._build_drawer_id(record)`): the id is the first 24 hex characters (96 bits) of the SHA-256 of the record's `source_file` (UTF-8 encoded), followed by `_` and the record's `chunk_index`. For source `"/an/absolute/path/to/a/file.txt"` with `chunk_index=3`, the id equals `sha256(src)[:24] + "_3"`. The id MUST NOT use the older SHA-1 first-16-hex (64-bit) scheme — the SHA-1-based id differs from the produced id (tests/test_sources.py:L286-L304).

`skip_current_item()` sets an internal skip flag: `_skip_requested` starts `False` and becomes `True` after the call (tests/test_sources.py:L307-L315).

`emit(event, **details)` dispatches the event and detail keyword arguments to every registered progress hook in order. Each hook is invoked as `hook(event, **details)`. A hook that raises is still invoked (its side effects occur) but the raised error is swallowed so that subsequent processing continues; for hooks `[good_hook, bad_hook]`, `emit("mined_file", path="a.txt", bytes=42)` records the call in both hooks and the bad hook's exception does not propagate (tests/test_sources.py:L318-L337).

## KnowledgeGraph provenance (RFC 002 §5.5)

`KnowledgeGraph(db_path=...)` opens or creates a SQLite database at the given path and must be closed via `close()` (tests/test_sources.py:L352-L356, L375-L376).

`add_triple(subject, predicate, object, ...)` accepts optional keyword arguments `valid_from`, `source_file`, `source_drawer_id`, and `adapter_name`, and returns a non-null triple id (tests/test_sources.py:L357-L366). The triple is persisted into a `triples` table; querying `SELECT source_drawer_id, adapter_name FROM triples WHERE id=?` returns the values that were passed in (`source_drawer_id="abc123_0"`, `adapter_name="git"`) (tests/test_sources.py:L368-L374).

A freshly created database has the `source_drawer_id` and `adapter_name` columns present in the `triples` table directly from table creation (verified via `PRAGMA table_info(triples)`), not added by a later migration (tests/test_sources.py:L379-L390).

A legacy `triples` table created without the new columns (columns: `id`, `subject`, `predicate`, `object`, `valid_from`, `valid_to`, `confidence`, `source_closet`, `source_file`, `extracted_at`) is auto-migrated on open: after `KnowledgeGraph` opens the database, `source_drawer_id` and `adapter_name` appear in `PRAGMA table_info(triples)`, and a subsequent `add_triple` using the new columns succeeds (tests/test_sources.py:L395-L435).

`add_triple` remains backward compatible: callers that omit all RFC 002 keyword arguments (e.g. `add_triple("Max", "likes", "trains")`) still receive a non-null triple id (tests/test_sources.py:L438-L447).

### On-disk contract: triples table columns

The `triples` table must expose at minimum these columns after open (fresh or migrated): the legacy set `id`, `subject`, `predicate`, `object`, `valid_from`, `valid_to`, `confidence`, `source_closet`, `source_file`, `extracted_at` (tests/test_sources.py:L407-L418) plus the RFC 002 additions `source_drawer_id` and `adapter_name` (tests/test_sources.py:L388-L390, L428-L430).
