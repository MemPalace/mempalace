# Spec: Hybrid Closet+Drawer Retrieval (`test_hybrid_search.py`)

This file is a behavioral test suite for the hybrid retrieval path of `search_memories`. It encodes the contract that closets (the compressed index layer) can only **help** ranking, never **hide** drawers that a direct drawer search would have surfaced (tests/test_hybrid_search.py:L1-L8, L55-L55).

## System Under Test

`search_memories(query, palace_path, n_results)` performs retrieval that queries drawers directly (the "floor") AND closets, applying a rank-based boost to drawers whose `source_file` appears among the top closet hits. This prevents low-signal closets (produced by regex extraction over narrative content) from suppressing drawers that direct search would have found (tests/test_hybrid_search.py:L1-L7, L10-L15).

## Test Data Setup

### Drawer seeding (`_seed_drawers`)

Inserts exactly 4 drawers into the palace's main collection, created on demand if absent (tests/test_hybrid_search.py:L18-L21). Each drawer has a deterministic id, a verbatim text document, and metadata (tests/test_hybrid_search.py:L21-L35):

- `D1`: "We switched the auth service to use JWT tokens with a 24h expiry." — metadata `{wing: backend, room: auth, source_file: fixture_D1.md}` (tests/test_hybrid_search.py:L24, L30).
- `D2`: "Database migration to PostgreSQL 15 completed last Tuesday." — `{wing: backend, room: db, source_file: fixture_D2.md}` (tests/test_hybrid_search.py:L25, L31).
- `D3`: "The frontend team is debating whether to adopt TanStack Query." — `{wing: frontend, room: state, source_file: fixture_D3.md}` (tests/test_hybrid_search.py:L26, L32).
- `D4`: "Kafka consumer rebalance timeout set to 45 seconds after incident." — `{wing: backend, room: queue, source_file: fixture_D4.md}` (tests/test_hybrid_search.py:L27, L33).

Each drawer's metadata MUST carry a `wing`, `room`, and `source_file` key (tests/test_hybrid_search.py:L29-L34).

### Closet seeding (`_seed_strong_closet_for`)

Inserts a closet into the palace's separate closets collection that strongly overlaps with the query keywords (tests/test_hybrid_search.py:L38-L40). Closet content is a list of lines, one per supplied topic, in the on-disk line format `<topic>||→<drawer_id>` (a topic string, the literal separator `||→`, then the target drawer id) (tests/test_hybrid_search.py:L41). The closet is keyed by a base id derived from the target drawer (`closet_<drawer_id>`) and carries metadata `{wing, room, source_file, generated_by}` where `generated_by` is `"test"` (tests/test_hybrid_search.py:L42-L52).

## Result Contract

`search_memories` returns an object with a `results` field that is an ordered list of hits (tests/test_hybrid_search.py:L63-L64, L80-L81, L98-L99). Each hit exposes at least:

- `source_file` — the originating drawer file identifier (tests/test_hybrid_search.py:L64, L121).
- `matched_via` — provenance string; `"drawer"` for a direct-only drawer hit, `"drawer+closet"` for a hit that was also matched/boosted by a closet (tests/test_hybrid_search.py:L102, L122, L133).
- `closet_boost` — a numeric boost value; strictly greater than 0 when a closet contributed to the hit, and exactly `0.0` for drawer-only hits (tests/test_hybrid_search.py:L103, L123, L135).
- `closet_preview` — present only when the hit was boosted by a closet; absent (key not present) for drawer-only hits (tests/test_hybrid_search.py:L124, L134).

Results are ordered by relevance; index `0` is the top-ranked hit (tests/test_hybrid_search.py:L100-L101, L120).

## Behavioral Invariants

### 1. No closets degrades to direct drawer search

With drawers seeded but no closets created, searching `"Kafka rebalance timeout"` with `n_results=3` MUST return a non-empty result list, and the Kafka drawer `fixture_D4.md` MUST appear among the `source_file` values — direct drawer search alone surfaces it (tests/test_hybrid_search.py:L59-L66).

### 2. Weak/misleading closets must not hide direct drawer hits

With drawers seeded plus a misleading closet whose topics (`"Kafka queue tuning"`, `"consumer rebalance config"`) match a generic phrase but point at the wrong drawer `D3`, searching `"Kafka consumer rebalance timeout"` with `n_results=5` MUST still include `fixture_D4.md` in the results. A closet pointing at D3 may only boost D3; it must never suppress D4, which direct drawer search would rank first (tests/test_hybrid_search.py:L68-L85).

### 3. Agreeing closet boosts the matching drawer to rank 1

With drawers seeded plus a closet for `D1` whose topics (`"JWT auth tokens"`, `"session expiry"`, `"authentication service"`) agree with direct search, searching `"JWT auth tokens expiry"` with `n_results=3` MUST rank `fixture_D1.md` at index 0. That top hit MUST have `matched_via == "drawer+closet"` and `closet_boost > 0` (tests/test_hybrid_search.py:L87-L103).

## Closet Metadata Exposure

### Closet preview exposed when boosted

With the `D1` closet seeded (same topics as above), searching `"JWT auth tokens expiry"` with `n_results=2` yields a top hit whose `source_file` is `fixture_D1.md`, `matched_via` is `"drawer+closet"`, `closet_boost > 0`, and which contains a `closet_preview` key (tests/test_hybrid_search.py:L110-L124).

### Drawer-only hits carry no closet preview

With drawers seeded and no closets, searching `"TanStack Query"` with `n_results=2` returns a non-empty result list in which every hit has `matched_via == "drawer"`, no `closet_preview` key, and `closet_boost == 0.0` (tests/test_hybrid_search.py:L126-L135).

## Side Effects

Each test operates on an isolated palace directory under a per-test temporary path (`<tmp>/palace`); collections are created lazily on first write (tests/test_hybrid_search.py:L60-L61, L20-L21). No network or external service is involved; all storage is local to the palace path (tests/test_hybrid_search.py:L10-L15).
