# Behavior Spec: Cross-Wing Tunnels (`palace_graph`)

This spec is derived from the test suite `tests/test_palace_graph_tunnels.py`, which
pins the observable contract of the cross-wing "tunnel" subsystem of the palace graph.
A tunnel is a persisted link between two `(wing, room)` endpoints. Three kinds exist:
`explicit` (user-created), `topic` (auto-derived from shared topic words), and `entity`
(auto-derived from shared entity hallways).

The subsystem is imported as `palace_graph` and its storage backend (chroma collection)
is treated as an injectable/optional dependency in tests (tests/test_palace_graph_tunnels.py:L11-L12).

---

## 1. Tunnel storage file resolution

A tunnel file path is resolved from configuration via `_get_tunnel_file(config)`, and a
separate legacy-path probe exists via `_legacy_tunnel_file()` (tests/test_palace_graph_tunnels.py:L26-L31).

The tunnel file lives as a sibling of the configured palace path, named `tunnels.json`.
With the default configuration the tunnel file resolves to `<dir of DEFAULT_PALACE_PATH>/tunnels.json`
(i.e. `~/.mempalace/tunnels.json`), and `config.tunnel_file` and `_get_tunnel_file(config)`
must agree (tests/test_palace_graph_tunnels.py:L514-L524). When the configured `palace_path`
is customized, the tunnel file follows it: for a config whose `palace_path` is `<tmp>/custom-palace`,
the tunnel file is `<tmp>/tunnels.json` — the parent dir of the palace, not a hardcoded legacy
location (tests/test_palace_graph_tunnels.py:L526-L535).

---

## 2. Load and save (`_load_tunnels`, `_save_tunnels`)

`_load_tunnels()` returns a list of tunnel records (objects). It returns an empty list
when the configured tunnel file is missing (tests/test_palace_graph_tunnels.py:L37-L39), and
also returns an empty list when the file exists but contains corrupt/invalid JSON
(tests/test_palace_graph_tunnels.py:L41-L44). It never raises on these conditions.

`_save_tunnels(tunnels)` then `_load_tunnels()` is a lossless round-trip: the loaded list
equals the saved list exactly, preserving all fields including `id`, `source.wing`,
`source.room`, `target.wing`, `target.room`, and `label` (tests/test_palace_graph_tunnels.py:L46-L57).

### File permissions (POSIX only)
After `_save_tunnels`, the on-disk `tunnels.json` must have file mode exactly `0o600`
(owner read/write only) and its parent directory must have mode exactly `0o700`
(owner-only). This prevents cross-wing link disclosure on multi-user systems
(tests/test_palace_graph_tunnels.py:L63-L84). This permission contract is POSIX-only and
not asserted on Windows (tests/test_palace_graph_tunnels.py:L59-L62).

### Legacy file handling
When the configured tunnel file is missing but a legacy tunnel file exists at a *different*
resolved path, `_load_tunnels()` returns `[]` and logs exactly one WARNING (logger name
`mempalace_graph`) that names BOTH the legacy path and the configured path. It must NOT
auto-migrate/merge the legacy data — silent merging risks clobbering newer data
(tests/test_palace_graph_tunnels.py:L537-L559). When the configured and legacy paths resolve
to the same path (default install) and the file simply does not exist yet, no
"Legacy tunnels file" warning is emitted (tests/test_palace_graph_tunnels.py:L561-L572).

---

## 3. Wing name normalization (`_normalize_wing`)

`_normalize_wing(value)` applies the shared wing-slug rule: it trims surrounding whitespace,
lowercases, and converts hyphens to underscores. For example `" Mempalace-Public "` →
`"mempalace_public"` (tests/test_palace_graph_tunnels.py:L88-L89). It returns `None` for a
whitespace-only string and for `None` (tests/test_palace_graph_tunnels.py:L90-L91), and also
returns `None` (rather than raising) for non-string inputs such as an integer or a list,
keeping read-path filters robust against corrupt/hand-edited records
(tests/test_palace_graph_tunnels.py:L92-L95).

---

## 4. `create_tunnel` — creating explicit tunnels

Signature observed: `create_tunnel(source_wing, source_room, target_wing, target_room,
label=..., kind=..., target_drawer_id=...)`. Returns a tunnel record object with at least
`id`, `source` (`{wing, room}`), `target` (`{wing, room}`), `label`, `kind`, `created_at`,
and L7 dynamics fields (see §10) (tests/test_palace_graph_tunnels.py:L102-L113, L859-L875).

### Canonical-ID dedup, symmetric ordering
Each tunnel has a canonical ID computed from its two endpoints, order-independent. Creating
a tunnel `A↔B` and then creating `B↔A` (endpoints reversed) yields the SAME `id`, and only
one stored record results. The second create updates the label, preserves the original
`created_at`, and adds an `updated_at` field (tests/test_palace_graph_tunnels.py:L97-L113). A
canonical-id helper `_canonical_tunnel_id(source_wing, source_room, target_wing, target_room)`
exists and yields the dedup id (tests/test_palace_graph_tunnels.py:L914).

### Empty-name rejection
`create_tunnel` raises `ValueError` when any endpoint name is empty (e.g. an empty
source wing) (tests/test_palace_graph_tunnels.py:L115-L119).

### Slug preservation (regression #1504)
`create_tunnel` stores the supplied wing slugs VERBATIM — it does not normalize them at
write time. A hyphenated wing name like `"my-project"` is stored as `"my-project"` in
`source.wing`/`target.wing` (tests/test_palace_graph_tunnels.py:L423-L434). Read-path matching
(§5, §6) handles slug-form differences at query time.

### Default kind
When `kind` is not supplied, the created tunnel's kind is `"explicit"`
(tests/test_palace_graph_tunnels.py:L360-L367).

---

## 5. `list_tunnels` — listing/filtering

`list_tunnels()` with no argument returns all stored tunnels (tests/test_palace_graph_tunnels.py:L121-L127).
`list_tunnels(wing)` filters to tunnels touching that wing on EITHER side (source or target):
two tunnels both targeting `wing_people` are both returned by `list_tunnels("wing_people")`,
while `list_tunnels("wing_code")` returns only the one touching `wing_code`
(tests/test_palace_graph_tunnels.py:L121-L129).

### Slug-form-insensitive matching
Filtering normalizes BOTH the stored wing value AND the query at comparison time, so a
tunnel stored as `mempalace_public` matches queries `"mempalace-public"` and
`"mempalace_public"` (tests/test_palace_graph_tunnels.py:L404-L410), and a tunnel stored verbatim
as `my-project` matches queries `"my-project"` and `"my_project"`
(tests/test_palace_graph_tunnels.py:L433-L434). Records written before #1504 in normalized
underscore form remain findable by either hyphen or underscore query
(tests/test_palace_graph_tunnels.py:L475-L496).

### Robustness to corrupt records
`list_tunnels(wing)` must skip records whose `source` or `target` is `null` rather than
crash the iteration; given one broken record (null endpoints) and one good record, it
returns only the good record (tests/test_palace_graph_tunnels.py:L444-L471).

---

## 6. `follow_tunnels` — traversing from a room

`follow_tunnels(wing, room, col=...)` returns the list of connections reachable from the
given `(wing, room)` endpoint. Each connection object carries `tunnel_id`, `direction`
(`"outgoing"` when the queried endpoint is the source, `"incoming"` when it is the target),
`connected_wing`, `connected_room`, and (when available) `drawer_id` and `drawer_preview`
(tests/test_palace_graph_tunnels.py:L141-L171).

### Direction semantics
For a tunnel created `wing_code/auth → wing_people/users`, following from `wing_code/auth`
yields one connection with `direction == "outgoing"`, `connected_wing == "wing_people"`,
`connected_room == "users"`. Following from `wing_people/users` yields one connection with
`direction == "incoming"`, `connected_wing == "wing_code"`
(tests/test_palace_graph_tunnels.py:L160-L171).

### Drawer preview
When the tunnel carries a `target_drawer_id` and the collection lookup succeeds, the
connection includes `drawer_id` equal to that id and a `drawer_preview` string that is
exactly 300 characters long (truncated from the underlying document, here a 400-char
document) (tests/test_palace_graph_tunnels.py:L153-L166). The collection is queried via
`col.get(...)` returning a `{"ids", "documents", "metadatas"}` shape
(tests/test_palace_graph_tunnels.py:L154-L158).

### Collection-failure tolerance
If the collection lookup raises (e.g. `col.get` throws), `follow_tunnels` still returns the
connection(s) but omits the `drawer_preview` field — it does not fail the whole call
(tests/test_palace_graph_tunnels.py:L173-L192).

### Slug-form-insensitive matching
Like `list_tunnels`, `follow_tunnels` matches the queried wing against the stored wing
under normalization, so a tunnel stored as `mempalace_public` is followable via both
`"mempalace-public"` and `"mempalace_public"` (tests/test_palace_graph_tunnels.py:L412-L421,
L498-L499).

### Robustness to corrupt records
`follow_tunnels` skips records with `null` endpoints and returns only valid connections
(tests/test_palace_graph_tunnels.py:L472-L473).

---

## 7. `delete_tunnel`

`delete_tunnel(tunnel_id)` removes the stored tunnel with that id and returns
`{"deleted": <tunnel_id>}`. After deletion `list_tunnels()` returns an empty list
(tests/test_palace_graph_tunnels.py:L131-L139).

---

## 8. `find_tunnels`

`find_tunnels(wing)` returns a list (empty when no tunnels are discoverable). When the
result is empty, it logs a WARNING on logger `mempalace_graph` whose text contains
"No tunnels found" (tests/test_palace_graph_tunnels.py:L436-L442).

---

## 9. Topic tunnels (`compute_topic_tunnels`, `topic_tunnels_for_wing`)

Topic tunnels are auto-derived: when two wings share confirmed topic labels above a
threshold, a symmetric tunnel is created between them, routed through `create_tunnel`
storage so they share dedup and persistence (tests/test_palace_graph_tunnels.py:L195-L201).

### `compute_topic_tunnels(topics_by_wing, min_count=...)`
Input is a mapping of wing → list of topic strings. For each pair of wings, the count of
shared topics must be `>= min_count` to create tunnels. Returns the list of created tunnel
records (tests/test_palace_graph_tunnels.py:L204-L211).

- One shared topic between two wings with `min_count=1` creates exactly one tunnel
  (tests/test_palace_graph_tunnels.py:L204-L211).
- With only one shared topic and `min_count=2`, no tunnel is created; the return is `[]`
  and nothing is persisted (tests/test_palace_graph_tunnels.py:L229-L238).
- When the shared-topic count meets the threshold, a SEPARATE tunnel is created per shared
  topic (e.g. two shared topics → two tunnels, one per topic)
  (tests/test_palace_graph_tunnels.py:L240-L251).
- Topic overlap is case-insensitive: `"openapi"` in one wing and `"OpenAPI"` in another
  count as the same shared topic (tests/test_palace_graph_tunnels.py:L253-L260).
- Empty input is a no-op: an empty mapping `{}` returns `[]`, and a mapping with an
  empty topic list returns `[]`; nothing is persisted
  (tests/test_palace_graph_tunnels.py:L262-L266).
- For three wings all sharing one topic, all pairwise tunnels are created: C(3,2) = 3
  tunnels covering all three unordered wing pairs
  (tests/test_palace_graph_tunnels.py:L268-L285).

### Endpoint room namespacing
Topic-tunnel endpoints use the synthetic room id `topic:<Name>`, where the topic's
original casing is preserved for display. So the source and target rooms are e.g.
`"topic:OpenAPI"` (tests/test_palace_graph_tunnels.py:L214-L218). The `kind` field is
`"topic"` (tests/test_palace_graph_tunnels.py:L219). The `label` carries the human-readable
topic name WITHOUT the `topic:` prefix (label contains `"OpenAPI"` but not `"topic:OpenAPI"`)
(tests/test_palace_graph_tunnels.py:L220-L222).

### Collision avoidance with literal rooms
A literal folder-room named `"Angular"` and a topic tunnel for `"Angular"` must resolve to
distinct endpoints. `follow_tunnels(wing, "Angular")` surfaces only the explicit/literal
link; `follow_tunnels(wing, "topic:Angular")` surfaces only the topic link
(tests/test_palace_graph_tunnels.py:L334-L358).

### Wing-key canonicalization (auto-generator only)
Unlike `create_tunnel` (which preserves verbatim slugs), the topic-tunnel auto-generator
canonicalizes wing slugs so two mining runs with mixed forms (`my-wing` then `my_wing`)
produce a single deduped record. After both runs there is exactly one stored tunnel whose
wings are `{"my_wing", "wing_people"}` (tests/test_palace_graph_tunnels.py:L369-L387).

### Idempotence / dedup on recompute
Running `compute_topic_tunnels` twice on the same input yields the same tunnel id and does
not multiply stored tunnels — only one stored tunnel results
(tests/test_palace_graph_tunnels.py:L321-L332).

### Retrievability and `kind` mixing
Created topic tunnels are retrievable via the standard `list_tunnels()` API, matching the
ids returned by `compute_topic_tunnels` (tests/test_palace_graph_tunnels.py:L224-L227). When
both an explicit and a topic tunnel exist, `list_tunnels()` reports both, with `kind`
values `["explicit", "topic"]` (tests/test_palace_graph_tunnels.py:L360-L367).

### `topic_tunnels_for_wing(wing, topics_by_wing)`
This is the incremental, single-wing variant. It creates only tunnels that INCLUDE the
given wing. If `wing_a` shares topic `foo` with `wing_b` and `bar` with `wing_c`, computing
for `wing_a` creates `wing_a↔wing_b` and `wing_a↔wing_c` only — NOT `wing_b↔wing_c`; total
stored is 2 (tests/test_palace_graph_tunnels.py:L287-L302). A wing that does not appear in the
mapping is a no-op returning `[]` with nothing persisted
(tests/test_palace_graph_tunnels.py:L304-L308). The wing argument and the `topics_by_wing` keys
may carry different slug forms (hyphen vs underscore); the lookup resolves through wing-name
normalization, so passing `"my-wing"` against a key `"my_wing"` still wires up the tunnel
(tests/test_palace_graph_tunnels.py:L310-L319).

---

## 10. Entity tunnels (`entity_tunnels_for_wing`)

Entity tunnels bridge wings that share an entity, derived from within-wing "hallway"
records. They use the same storage and dedup as other tunnels; the substrate is hallway
records (entity-grounded) rather than topic words
(tests/test_palace_graph_tunnels.py:L696-L713).

### `entity_tunnels_for_wing(wing, hallways)`
Input `hallways` is a list of records each with `wing`, `entity_a`, `entity_b`
(tests/test_palace_graph_tunnels.py:L721-L724). An entity is considered "present in a wing" if
it appears in EITHER pair position (`entity_a` or `entity_b`) of any hallway for that wing
(tests/test_palace_graph_tunnels.py:L752-L764).

- When an entity (e.g. "Ben") has a hallway in two wings, computing for one of those wings
  creates exactly one entity tunnel between the two wings, anchored on that entity
  (tests/test_palace_graph_tunnels.py:L715-L731).
- An entity present in only one wing produces no tunnel
  (tests/test_palace_graph_tunnels.py:L742-L750).
- For an entity present in three wings, computing for the focus wing creates only the
  pairwise tunnels INCLUDING the focus wing (focus↔other1 and focus↔other2), NOT the
  tunnel between the two non-focus wings (tests/test_palace_graph_tunnels.py:L766-L786).
- Empty hallways list → `[]`, no crash (tests/test_palace_graph_tunnels.py:L816-L819).
- A focus wing that appears in no hallway → `[]` (tests/test_palace_graph_tunnels.py:L821-L828).

### Endpoint room namespacing
Entity-tunnel endpoints use the synthetic room id `entity:<Name>` with the entity's casing
preserved, for both source and target (e.g. `"entity:Ben"`)
(tests/test_palace_graph_tunnels.py:L732-L736). The `kind` field is `"entity"`
(tests/test_palace_graph_tunnels.py:L737). The `label` carries the entity name WITHOUT the
`entity:` prefix (label contains `"Ben"` but not `"entity:Ben"`)
(tests/test_palace_graph_tunnels.py:L738-L740).

### Idempotence and retrievability
Re-running on identical hallway data does not duplicate tunnels — only one entity tunnel is
stored after two passes (tests/test_palace_graph_tunnels.py:L788-L802). Created entity tunnels
are retrievable via the standard `list_tunnels()` API, matching the created ids
(tests/test_palace_graph_tunnels.py:L804-L814).

### Collision avoidance with literal rooms
A literal folder room `"Ben"` (from an explicit tunnel) and the synthetic `"entity:Ben"`
endpoint produce distinct tunnels with different canonical ids — they are NOT deduped
together. After both, `list_tunnels()` reports two tunnels with kinds `{"explicit", "entity"}`
(tests/test_palace_graph_tunnels.py:L830-L849).

---

## 11. Explicit-tunnel endpoint validation (regression #1468)

For `kind == "explicit"` tunnels, `create_tunnel` validates that BOTH endpoint rooms
actually exist in the chroma collection before persisting. The collection is obtained via
`_get_collection(config=None)` and queried with a where-clause of shape
`{"$and": [{"wing": W}, {"room": R}]}`; a room is considered existing when the query returns
at least one id (tests/test_palace_graph_tunnels.py:L578-L607).

- If the target room does not exist, `create_tunnel` raises `ValueError` whose message
  contains the offending room name and its wing (e.g. `"phantom"` and `"wing_people"`), and
  nothing is persisted (tests/test_palace_graph_tunnels.py:L609-L620).
- If the source room does not exist, it raises `ValueError` whose message contains the
  source room and source wing (tests/test_palace_graph_tunnels.py:L622-L631).
- When both rooms exist, the tunnel is created and persisted with the supplied label
  (tests/test_palace_graph_tunnels.py:L633-L642).

### Fail-open tolerance
Validation is best-effort, not fail-closed:
- When `_get_collection` returns `None` (chroma unreachable / palace not yet created),
  validation is skipped and the tunnel is created anyway
  (tests/test_palace_graph_tunnels.py:L644-L655).
- When the collection's `get` raises (permission/transient fault), validation falls back to
  "allow" and the tunnel is still created (tests/test_palace_graph_tunnels.py:L657-L670).

### Topic tunnels skip validation
Endpoint-existence validation runs only for `kind == "explicit"`. Topic tunnels (synthetic
`topic:<name>` rooms that don't map to real chroma rooms) skip it entirely: with an empty
collection that would reject any real room, `compute_topic_tunnels` still persists topic
tunnels, all carrying `kind == "topic"`, and the validation query is never invoked
(tests/test_palace_graph_tunnels.py:L672-L693).

---

## 12. L7 dynamics fields on tunnels

Every tunnel record produced by `create_tunnel` carries L7 dynamics fields: `strength`,
`stability`, `last_activated`, and `access_count`
(tests/test_palace_graph_tunnels.py:L852-L857).

### New-tunnel defaults
A newly created tunnel has `strength == DEFAULT_STRENGTH`, `stability == DEFAULT_STABILITY`
(from the dynamics module), `access_count == 0`, and a `last_activated` field whose value
equals `created_at` (so decay begins at creation)
(tests/test_palace_graph_tunnels.py:L859-L875).

### Dynamics preserved on recreate (dedup path)
When `create_tunnel` is called again with the same canonical endpoints (e.g. to update the
label), it preserves accumulated dynamics rather than resetting them. If a stored tunnel had
`strength = 2.7`, `access_count = 12`, `stability = 1.5`, a recreate with a new label keeps
the same `id`, applies the new label, and preserves all three dynamics values
(tests/test_palace_graph_tunnels.py:L877-L904).

### Backfill for legacy records
If an existing tunnel record predates L7 (no dynamics fields present), a recreate event
backfills the defaults: `strength == DEFAULT_STRENGTH`, `stability == DEFAULT_STABILITY`,
`access_count == 0`, and a `last_activated` field is added
(tests/test_palace_graph_tunnels.py:L906-L931).

---

## On-disk contract summary

`tunnels.json` is a JSON array of tunnel objects. Each object has fields: `id` (canonical,
endpoint-order-independent), `source` (`{wing, room}` or `null`), `target` (`{wing, room}`
or `null`), `label` (string), `kind` (`"explicit"` | `"topic"` | `"entity"`), `created_at`
(ISO-8601 timestamp), optional `updated_at`, and L7 dynamics fields `strength`, `stability`,
`last_activated`, `access_count` (tests/test_palace_graph_tunnels.py:L444-L468, L913-L921,
L859-L875). The file is owner-only (`0o600`) within an owner-only directory (`0o700`) on
POSIX systems (tests/test_palace_graph_tunnels.py:L78-L84).
