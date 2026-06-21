# Behavior Specification: `mempalace/ids.py`

## Purpose

This module is the single source of truth for constructing drawer IDs, registry sentinel IDs, and knowledge-graph triple IDs. All identifiers are content-addressed using a SHA-256 hash over inputs joined by a collision-safe delimiter. No other module should inline raw `hash(a + b)` patterns; all ID construction routes through the helpers defined here (mempalace/ids.py:L1-L13).

## Exported Constants

- `ID_RECIPE` — a string constant with value `"v3"`. It is the recipe tag written to every drawer's metadata by call sites using these helpers. Drawers lacking this tag are treated by audits as legacy `v1` (pre-delimiter); drawers tagged `v2` are collision-safe within the v2 generation. Call sites reference this constant rather than a literal string (mempalace/ids.py:L20-L25).

## Internal Hashing Contract

All ID-building functions delegate to a single hashing primitive with these observable properties (mempalace/ids.py:L40-L53):

- The hash input is built from an ordered tuple of parts. Each part is first coerced to its string representation. Coercion mirrors plain string interpolation: a `None` part becomes the literal string `"None"`, and numeric parts become their decimal string form — no error is raised for `None` or numeric inputs (mempalace/ids.py:L47-L52).
- Each stringified part is length-prefixed and joined with no separating bytes between encoded segments. Specifically the byte string fed to the hash is the concatenation, for each part, of `<length-of-part-string>:<part-string>` (mempalace/ids.py:L52).
- The result is the SHA-256 hex digest of that byte string, truncated to the first `N` hex characters where `N` is supplied per call site (mempalace/ids.py:L53).
- This length-prefixed encoding is the collision-safety mechanism: it guarantees that two different ordered tuples of parts cannot produce the same hash input string, preventing the defect where `s1 + str(i1) == s2 + str(i2)` for distinct `(s1, i1)`, `(s2, i2)` (mempalace/ids.py:L3-L9, L52).

### Truncation lengths (observable contract)

- Drawer and sentinel IDs truncate the hex digest to 24 characters (mempalace/ids.py:L36).
- Knowledge-graph triple IDs truncate the hex digest to 12 characters (mempalace/ids.py:L37).

### Delimiter

The conceptual delimiter is the pipe character `|`, chosen because it is reserved in Windows filenames and cannot appear in source paths on supported platforms, making it safer than `:` (which appears in Windows drive letters and URL ports) (mempalace/ids.py:L27-L31). Note: the actual hash-input encoding uses the length-prefixed scheme above rather than literally inserting `|`; the docstrings describe the inputs as `|`-joined for human readability (mempalace/ids.py:L52, L59-L60).

## Public Functions

### `make_drawer_id_from_chunk(wing, room, source_file, chunk_index)`

- Inputs: `wing` (string), `room` (string), `source_file` (string), `chunk_index` (integer).
- Output: a string of the form `drawer_{wing}_{room}_{hash24}`, where `hash24` is the 24-character truncated SHA-256 over the ordered parts `(source_file, str(chunk_index))` (mempalace/ids.py:L56-L68).
- Used by the project / format miner paths (mempalace/ids.py:L57).

### `make_drawer_id_from_content(wing, room, content)`

- Inputs: `wing` (string), `room` (string), `content` (string).
- Output: a string of the form `drawer_{wing}_{room}_{hash24}`, where `hash24` is the 24-character truncated SHA-256 over the ordered parts `(wing, room, content)` (mempalace/ids.py:L71-L80).
- Used by the MCP `add_drawer` tool path (mempalace/ids.py:L72).

### `make_convo_drawer_id(wing, room, source_file, extract_mode, chunk_index)`

- Inputs: `wing` (string), `room` (string), `source_file` (string), `extract_mode` (string), `chunk_index` (integer).
- Output: a string of the form `drawer_{wing}_{room}_{hash24}`, where `hash24` is the 24-character truncated SHA-256 over the ordered parts `(source_file, extract_mode, str(chunk_index))` (mempalace/ids.py:L83-L97).
- Used by the conversation miner path. Migrated from a pre-v2 `:` delimiter to remove Windows-path / URL-source edge cases (mempalace/ids.py:L86-L92).

### `make_convo_sentinel_id(source_file, extract_mode)`

- Inputs: `source_file` (string), `extract_mode` (string).
- Output: a string of the form `_reg_{hash24}`, where `hash24` is the 24-character truncated SHA-256 over the ordered parts `(source_file, extract_mode)` (mempalace/ids.py:L100-L108).
- Used as the sentinel registry ID for the conversation miner's zero-chunk-file path (mempalace/ids.py:L101).

### `make_triple_id(sub_id, predicate, obj_id, valid_from, recorded_at)`

- Inputs: `sub_id` (string), `predicate` (string), `obj_id` (string), `valid_from` (string), `recorded_at` (string).
- Output: a string of the form `t_{sub_id}_{predicate}_{obj_id}_{hash12}`, where `hash12` is the 12-character truncated SHA-256 over the ordered parts `(valid_from, recorded_at)` (mempalace/ids.py:L111-L128).
- Used for knowledge-graph triple insertion. Only `valid_from` and `recorded_at` contribute to the hash; `sub_id`, `predicate`, and `obj_id` appear verbatim in the prefix (mempalace/ids.py:L125-L127).

## Invariants

- All drawer-producing helpers share the literal prefix `drawer_` followed by `wing`, `room`, and the hash, separated by underscores (mempalace/ids.py:L65-L68, L80, L94-L97).
- The sentinel helper uses the literal prefix `_reg_` (mempalace/ids.py:L108).
- The triple helper uses the literal prefix `t_` (mempalace/ids.py:L126).
- IDs are deterministic: identical ordered inputs always produce identical IDs (pure hashing over coerced inputs, no time, randomness, or external state) (mempalace/ids.py:L40-L53).

## Side Effects

None. The module performs no filesystem, network, process, or environment access; every function is a pure transformation from inputs to a string (mempalace/ids.py:L16-L128).
