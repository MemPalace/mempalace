# Spec: `mempalace/sources/context.py` — `PalaceContext` facade

## Purpose

This module defines `PalaceContext`, a per-mine-invocation facade object passed
to source adapters during ingest. It bundles the palace-side surface an adapter
needs (drawer collection, optional closet collection, knowledge graph, palace
config, progress hooks) so that adapters do not couple directly to core palace
internals (`mempalace/sources/context.py:L1-L14`). At the time of writing no
in-tree code constructs a `PalaceContext`; it documents a stable contract that
third-party adapters target and that core's mine loop will construct in a later
change (`mempalace/sources/context.py:L8-L14`).

## Collaborator contracts (structural)

`PalaceContext` depends on two structural (duck-typed) collaborators, not
concrete classes. Any object satisfying the method shape is acceptable.

- A **collection-like** object must expose methods: `add`, `upsert`, `query`,
  `get`, `delete`, and `count` (returning an integer count)
  (`mempalace/sources/context.py:L24-L37`).
- A **knowledge-graph-like** object must expose `add_triple(subject, predicate,
  obj, ...)` (`mempalace/sources/context.py:L40-L41`).
- A **progress hook** is any callable invoked as `hook(event_name, **details)`
  returning nothing (`mempalace/sources/context.py:L44-L45`).

## `PalaceContext` fields

`PalaceContext` is a record with the following fields (required fields first,
then defaulted fields) (`mempalace/sources/context.py:L48-L85`):

- `drawer_collection` — collection-like; the palace's drawer collection
  (`mempalace/sources/context.py:L72-L72`).
- `knowledge_graph` — knowledge-graph-like; the palace's knowledge graph.
  Adapters advertising knowledge-graph-triple support call `add_triple` on it
  (`mempalace/sources/context.py:L57-L58`, `mempalace/sources/context.py:L73-L73`).
- `palace_path` — string; filesystem root of the palace
  (`mempalace/sources/context.py:L59-L60`, `mempalace/sources/context.py:L74-L74`).
- `closet_collection` — optional collection-like, default absent/none. Adapters
  should not write to it directly; core builds closets post-step
  (`mempalace/sources/context.py:L54-L56`, `mempalace/sources/context.py:L75-L75`).
- `config` — optional palace config object (hall keywords, rooms list, privacy
  floor, etc.), default absent/none
  (`mempalace/sources/context.py:L61-L62`, `mempalace/sources/context.py:L76-L76`).
- `adapter_name` — string, default empty; name of the adapter currently
  ingesting, populated by core
  (`mempalace/sources/context.py:L63-L64`, `mempalace/sources/context.py:L77-L77`).
- `adapter_version` — string, default empty; version of the current adapter
  (`mempalace/sources/context.py:L65-L65`, `mempalace/sources/context.py:L78-L78`).
- `progress_hooks` — list of progress hooks, default empty list
  (`mempalace/sources/context.py:L66-L66`, `mempalace/sources/context.py:L79-L79`).
- `_skip_requested` — internal boolean flag, default false; set by
  `skip_current_item` and checked by the core mine loop between yields. Not part
  of the adapter-facing contract (`mempalace/sources/context.py:L81-L85`).

## Method: `upsert_drawer(record)`

Persists a drawer record into the drawer collection. The input `record` carries
at minimum `content` (string), `source_file` (string), `chunk_index` (integer,
default 0), and `metadata` (a flat key/value map)
(`mempalace/sources/base.py:L109-L121`).

Behavior (`mempalace/sources/context.py:L91-L109`):

1. A copy of the record's metadata map is taken so the caller's map is not
   mutated (`mempalace/sources/context.py:L97-L97`).
2. The key `source_file` is set to the record's `source_file` only if not
   already present in the metadata
   (`mempalace/sources/context.py:L98-L98`).
3. The key `chunk_index` is set to the record's `chunk_index` only if not
   already present (`mempalace/sources/context.py:L99-L99`).
4. If `adapter_name` is non-empty, the key `adapter_name` is set to it only if
   not already present (`mempalace/sources/context.py:L100-L101`).
5. If `adapter_version` is non-empty, the key `adapter_version` is set to it
   only if not already present (`mempalace/sources/context.py:L102-L103`).
   These four steps apply spec-mandated stamps so adapters never populate them
   manually, and never overwrite a value the adapter already provided
   (`mempalace/sources/context.py:L93-L103`).
6. A drawer id is computed deterministically from the record (see below)
   (`mempalace/sources/context.py:L104-L104`).
7. The drawer is upserted into `drawer_collection` as a single-element batch:
   the document text is the record's `content`, the id is the computed drawer
   id, and the metadata is the augmented metadata map
   (`mempalace/sources/context.py:L105-L109`).

## Drawer id contract (`_build_drawer_id`)

The drawer id is deterministic and has the exact form
`<hexdigest>_<chunk_index>` where `<hexdigest>` is the first 24 hexadecimal
characters (96 bits) of the SHA-256 digest of the record's `source_file` encoded
as UTF-8, and `<chunk_index>` is the record's chunk index rendered as text
(`mempalace/sources/context.py:L128-L142`). The same `source_file` and
`chunk_index` always produce the same id, so re-ingesting overwrites the same
drawer rather than duplicating it (`mempalace/sources/context.py:L91-L109`,
`mempalace/sources/context.py:L128-L142`). Adapters needing a different id
scheme may bypass `upsert_drawer` and write through the drawer collection
directly (`mempalace/sources/context.py:L133-L137`).

## Method: `skip_current_item()`

Sets the internal skip flag to true, signaling to core that the current source
item is up-to-date and no drawers should be emitted for it. Core resets the flag
after advancing past the item (`mempalace/sources/context.py:L111-L115`).

## Method: `emit(event, **details)`

Invokes every registered progress hook in registration order, each called as
`hook(event, **details)` (`mempalace/sources/context.py:L117-L121`). Hook failures
are isolated: if a hook raises, the exception is caught and logged, and emission
continues — a failing progress hook never aborts the mine
(`mempalace/sources/context.py:L122-L125`). With no registered hooks, `emit` is a
no-op (`mempalace/sources/context.py:L119-L121`).

## Side effects

- The only persistence side effect is the upsert into the provided drawer
  collection inside `upsert_drawer` (`mempalace/sources/context.py:L105-L109`).
- `emit` produces log output on the logger named for this module only when a
  hook raises (`mempalace/sources/context.py:L122-L125`).
- No filesystem, network, process, or environment access occurs directly in this
  module beyond the collaborator calls described above
  (`mempalace/sources/context.py:L91-L142`).
