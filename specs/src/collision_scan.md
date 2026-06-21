# Behavior Specification: `collision_scan`

Pre-mining defense that runs immediately before a batched storage-backend upsert. It computes the union of incoming drawer IDs and existing drawer IDs that share an ID with the batch, and aborts the operation when any drawer ID would map to two or more conflicting chunk identities (`mempalace/collision_scan.py:L1-L22`).

## Purpose and Contract

The scan exists to surface two failure modes as actionable errors rather than silent overwrites: (1) upstream bugs that emit duplicate chunk-identity pairs in the same batch with conflicting content, where a last-write-wins backend would silently discard data; and (2) astronomically rare hash collisions producing the same drawer ID for genuinely different chunks (`mempalace/collision_scan.py:L10-L17`). The scan MUST NOT fire on idempotent re-mines: when an incoming drawer matches an existing one with the SAME chunk identity, that is normal re-write behavior and is not a collision (`mempalace/collision_scan.py:L19-L21`, `mempalace/collision_scan.py:L76-L78`).

## Public Surface

### `CollisionError` (exception type)

An error type raised when the scan detects a drawer ID that would silently overwrite existing content, or that duplicates within a batch with conflicting metadata (`mempalace/collision_scan.py:L29-L38`). Its message enumerates every colliding drawer ID and the full set of chunk-identity pairs producing each one, so that fixing one collision does not require re-running the mine to discover the next (`mempalace/collision_scan.py:L34-L37`).

### `assert_no_collisions(proposed, collection) -> None`

Inputs:
- `proposed`: an ordered list of `(drawer_id, metadata)` pairs for the chunks about to be upserted. `drawer_id` is a string; `metadata` is a key/value mapping that MUST carry at least `source_file`, and MAY carry `chunk_index` (`mempalace/collision_scan.py:L53-L64`).
- `collection`: a storage handle exposing a lookup `get(ids=..., include=["metadatas"])` that returns, for the requested IDs, only the rows actually present — keyed result with `ids` and `metadatas` fields. Missing IDs are silently absent from the result (`mempalace/collision_scan.py:L65-L66`, `mempalace/collision_scan.py:L83-L89`).

Output: returns nothing on success (no collision). It has no side effects on the collection — it only reads via `get` (`mempalace/collision_scan.py:L57-L58`, `mempalace/collision_scan.py:L87-L87`).

Errors: raises `CollisionError` when a drawer ID maps to two or more distinct chunk-identity tuples in the union of incoming and existing rows (`mempalace/collision_scan.py:L68-L71`, `mempalace/collision_scan.py:L97-L99`).

## Chunk-Identity Discrimination

Two metadata mappings are considered "the same chunk" if and only if their identity tuples match. The identity tuple is `(source_file, chunk_index)` when `chunk_index` is present, and falls back to the single-element tuple `(source_file,)` when `chunk_index` is absent — e.g. diary entries and sentinels (`mempalace/collision_scan.py:L41-L50`). A `chunk_index` value of "absent/none" triggers the single-element fallback; the check is specifically whether `chunk_index` is none/missing, not whether it is falsy (`mempalace/collision_scan.py:L48-L50`).

## Algorithm and Invariants

1. If `proposed` is empty, return immediately with no lookup performed (`mempalace/collision_scan.py:L73-L74`).
2. Build a map from each incoming `drawer_id` to the SET of its identity tuples. Using a set collapses the case where the same chunk identity appears twice in the batch, so that duplicate-but-identical entries are not flagged (`mempalace/collision_scan.py:L76-L81`).
3. Query the collection for existing rows matching the incoming drawer IDs. Only rows whose IDs are present are returned; absent IDs are omitted (`mempalace/collision_scan.py:L83-L89`). If the result is not subscriptable, existing IDs are treated as an empty list (defensive fallback) (`mempalace/collision_scan.py:L88-L88`). Existing metadata is only read when there is at least one existing ID (`mempalace/collision_scan.py:L89-L89`).
4. Merge each existing row's identity tuple into the same drawer-ID map. A null/missing existing metadata is treated as an empty mapping (yielding `(None,)` after fallback) (`mempalace/collision_scan.py:L91-L95`).
5. A collision is any `drawer_id` whose merged set of identity tuples has more than one element. If any exist, raise `CollisionError` with the formatted message; otherwise return (`mempalace/collision_scan.py:L91-L99`).

Invariant: a drawer ID with exactly one distinct identity tuple across incoming + existing rows is never a collision, regardless of how many times it appears (`mempalace/collision_scan.py:L97-L97`).

## Error Message Format (observable contract)

The `CollisionError` message is a newline-joined block (`mempalace/collision_scan.py:L102-L122`):

- A header line: `Pre-mining collision scan detected <N> colliding drawer_id<s>:` where `<N>` is the number of colliding drawer IDs and the trailing `s` is present only when `N != 1` (`mempalace/collision_scan.py:L105-L108`).
- For each colliding drawer ID, in ascending sorted order of drawer ID (`mempalace/collision_scan.py:L109-L109`): a line `  <drawer_id>:` (two-space indent), followed by one line per identity tuple, sorted by the string representation of the tuple parts (`mempalace/collision_scan.py:L110-L111`). Each tuple line is four-space indented and rendered as either `    source_file=<repr>` for single-element tuples, or `    source_file=<repr>, chunk_index=<repr>` for two-element tuples, where `<repr>` is the quoted/escaped literal form of the value (`mempalace/collision_scan.py:L112-L115`).
- A trailing guidance line explaining that each colliding drawer ID would cause the second upsert to silently overwrite the first, advising the user to fix the upstream chunker/miner to emit distinct keys or investigate a hash collision (`mempalace/collision_scan.py:L116-L121`).

## Side Effects

None on the filesystem, network, process, or environment. The only external interaction is a read-only lookup against the provided collection handle (`mempalace/collision_scan.py:L87-L87`).
