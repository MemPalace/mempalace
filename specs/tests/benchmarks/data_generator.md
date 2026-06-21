# Behavior Spec: `tests/benchmarks/data_generator.py`

Deterministic data factory for MemPalace scale benchmarks. Generates realistic project files, conversation transcripts, drawer content, knowledge-graph triples, and search queries at configurable scale levels, with all randomness driven by a seeded RNG for reproducibility. Planted "needle" drawers enable recall measurement without an LLM judge (tests/benchmarks/data_generator.py:L1-L8).

## Scale Configurations

There are four named scale levels: `small`, `medium`, `large`, `stress`. Each maps to a config record with integer fields: `drawers`, `wings`, `rooms_per_wing`, `kg_entities`, `kg_triples`, `needles`, `search_queries`. Concrete values are: small = 1000/3/5/50/200/20/20; medium = 10000/8/12/200/2000/50/50; large = 50000/15/20/500/10000/100/100; stress = 100000/25/30/1000/50000/200/200 (tests/benchmarks/data_generator.py:L22-L59).

## Vocabulary Banks (fixed contract data)

Fixed ordered lists are defined and consumed by prefix/index: `WING_NAMES` (25 entries) (tests/benchmarks/data_generator.py:L63-L89), `ROOM_NAMES` (30 entries) (tests/benchmarks/data_generator.py:L91-L122), `TECH_TERMS` (tests/benchmarks/data_generator.py:L124-L167), `CODE_SNIPPETS` (5 code blocks) (tests/benchmarks/data_generator.py:L169-L175), `PROSE_TEMPLATES` (5 templates with named placeholders) (tests/benchmarks/data_generator.py:L177-L183), `ENTITY_NAMES` (24 names) (tests/benchmarks/data_generator.py:L185-L210), `ENTITY_TYPES` (6 types) (tests/benchmarks/data_generator.py:L212-L212), and `PREDICATES` (16 relation names) (tests/benchmarks/data_generator.py:L214-L231).

## Public Surface: `PalaceDataGenerator`

### Construction `__init__(seed=42, scale="small")`

Initializes a seeded random generator from `seed`, stores `scale`, and resolves `cfg` from `SCALE_CONFIGS[scale]`. The selected wings are the first `cfg["wings"]` entries of `WING_NAMES` (in declaration order) (tests/benchmarks/data_generator.py:L237-L241). For each wing, a `rooms_by_wing` mapping is built by sampling `min(rooms_per_wing, len(ROOM_NAMES))` distinct room names from `ROOM_NAMES` using the seeded RNG (tests/benchmarks/data_generator.py:L242-L246). It then generates planted needles into `self.needles` during construction (tests/benchmarks/data_generator.py:L247-L249). An unknown `scale` value causes a lookup failure (no defined fallback) (tests/benchmarks/data_generator.py:L240-L240).

Determinism contract: identical `seed` and `scale` produce identical wings, room assignments, needles, and all subsequent generated artifacts, because every random choice draws from the single seeded RNG (tests/benchmarks/data_generator.py:L238-L238).

### Needle generation `_generate_needles()`

Iterates `i` from 0 to `cfg["needles"]-1`. For each, selects a topic by `topics[i % 20]` from a fixed 20-element topic list (tests/benchmarks/data_generator.py:L253-L276). Picks a random wing from `self.wings` and a random room from that wing's room list (tests/benchmarks/data_generator.py:L277-L278). The needle id has the zero-padded form `NEEDLE_<i:04d>` (tests/benchmarks/data_generator.py:L279-L279). Content is exactly `"<needle_id>: <topic>. This is a unique planted needle for recall benchmarking at scale."` (tests/benchmarks/data_generator.py:L280-L280). Each needle record holds `id`, `content`, `wing`, `room`, and a `query` derived from the topic: the substring before `" uses "` if present, else the substring before `" set to "` if present, else the first 60 characters of the topic (tests/benchmarks/data_generator.py:L281-L293).

### Random text `_random_text(min_chars=600, max_chars=900)`

Returns a text block. A target length is drawn uniformly in `[min_chars, max_chars]` (tests/benchmarks/data_generator.py:L298-L299). It appends chunks until accumulated character count reaches the target; per chunk a uniform random value `r` selects: `r<0.3` a code snippet; `0.3<=r<0.7` a filled prose template (placeholders filled from the vocabulary banks and random dates/percents/efforts); otherwise a synthesized line of joined tech terms (tests/benchmarks/data_generator.py:L300-L344). The result is the chunks joined by newline characters, then truncated to at most `max_chars` characters (tests/benchmarks/data_generator.py:L345-L345). Output length is therefore bounded by `max_chars`; minimum can fall below `min_chars` only after truncation logic but accumulation continues until `total >= target` (tests/benchmarks/data_generator.py:L300-L345).

### `generate_project_tree(base_path, wing=None, rooms=None, n_files=50)`

Creates `base_path` (including parents) (tests/benchmarks/data_generator.py:L355-L356). If `wing` is omitted, picks a random wing; if `rooms` omitted, uses that wing's room list or `["general"]` fallback (tests/benchmarks/data_generator.py:L357-L358). Writes a `mempalace.yaml` file in `base_path` containing a mapping `{wing: <wing>, rooms: [{name, description}, ...]}` where each room's description is `"<room> code and docs"` (tests/benchmarks/data_generator.py:L361-L363). Writes `n_files` files distributed round-robin across room subdirectories (`rooms[i % len(rooms)]`), creating each room directory as needed (tests/benchmarks/data_generator.py:L366-L370). Each file is named `file_<i:04d><ext>` where `ext` is randomly one of `.py .js .md .ts .yaml`, with body from `_random_text(400, 2000)`, written UTF-8 (tests/benchmarks/data_generator.py:L372-L376). Returns the 4-tuple `(str(base), wing, rooms, files_written)` where `files_written == n_files` (tests/benchmarks/data_generator.py:L366-L378).

Side effects: filesystem writes under `base_path` (directories, `mempalace.yaml`, and per-room files) (tests/benchmarks/data_generator.py:L355-L376).

### `generate_conversation_files(base_path, wing=None, n_files=20)`

Creates `base_path` (with parents) and picks a random wing if omitted (tests/benchmarks/data_generator.py:L385-L386). Writes `n_files` transcript files named `convo_<i:04d>.txt` (tests/benchmarks/data_generator.py:L388-L398). Each file contains a random number of exchanges in `[5, 20]`; each exchange emits a user line beginning with the literal prefix `"> User: "` followed by templated tech-term questions, then an AI line produced by `_random_text(200, 600)`, then a blank line (tests/benchmarks/data_generator.py:L390-L396). Lines are joined by newlines and written UTF-8 (tests/benchmarks/data_generator.py:L398-L398). Returns the 2-tuple `(str(base), wing)` (tests/benchmarks/data_generator.py:L400-L400).

### `populate_palace_directly(palace_path, n_drawers=None, include_needles=True)`

Inserts drawers directly into a ChromaDB persistent store, bypassing the mining pipeline (tests/benchmarks/data_generator.py:L404-L412). `n_drawers` defaults to `cfg["drawers"]` (tests/benchmarks/data_generator.py:L413-L413). Creates `palace_path` directory, opens a persistent client there, and gets-or-creates the collection named `"mempalace_drawers"` (tests/benchmarks/data_generator.py:L414-L416).

When `include_needles` is true, each needle is inserted first. The drawer id contract is `drawer_<wing>_<room>_<first16hexofMD5(needle_id)>` (tests/benchmarks/data_generator.py:L427-L427). The document is the needle content; metadata is a record with `wing`, `room`, `source_file = "needle_<needle_id>.txt"`, `chunk_index = 0`, `added_by = "benchmark"`, `filed_at = <current ISO timestamp>` (tests/benchmarks/data_generator.py:L429-L439). For each inserted needle, a `needle_info` record `{id, query, wing, room}` is collected for return (tests/benchmarks/data_generator.py:L440-L447).

Remaining drawers count is `n_drawers - len(already-queued docs)` (tests/benchmarks/data_generator.py:L450-L450). For `i` in `[0, remaining)`: wing is `self.wings[i % len(self.wings)]`, room is that wing's `rooms[i % len(rooms)]`, content is `_random_text(400, 800)`, drawer id is `drawer_<wing>_<room>_<first16hexofMD5("gen_<i>")>` (tests/benchmarks/data_generator.py:L451-L456). Metadata fields: `wing`, `room`, `source_file = "generated_<i:06d>.txt"`, `chunk_index = i % 10`, `added_by = "benchmark"`, `filed_at = <ISO timestamp>` (tests/benchmarks/data_generator.py:L458-L469).

Ordering/batching: documents are flushed to the collection in batches whenever the pending buffer reaches 500 items, and any remainder is flushed at the end (tests/benchmarks/data_generator.py:L418-L478). Needles are always queued before generated drawers, so they enter the store first (tests/benchmarks/data_generator.py:L423-L451). Returns the 3-tuple `(client, collection, needle_info)` (tests/benchmarks/data_generator.py:L480-L480).

Side effects: filesystem directory creation and a persistent ChromaDB store at `palace_path` (tests/benchmarks/data_generator.py:L414-L416).

### `generate_kg_triples(n_entities=None, n_triples=None)`

Defaults to `cfg["kg_entities"]` and `cfg["kg_triples"]` (tests/benchmarks/data_generator.py:L492-L493). Produces `n_entities` entities as `(name, type)` pairs: for index `i < len(ENTITY_NAMES)` the name is `ENTITY_NAMES[i]`, otherwise the synthesized name `Entity_<i:04d>`; the type is a random pick from `ENTITY_TYPES` (tests/benchmarks/data_generator.py:L496-L505). Produces `n_triples` triples `(subject, predicate, object, valid_from, valid_to)`. Subject and object are random entity names with the invariant that object never equals subject (re-sampled until distinct) (tests/benchmarks/data_generator.py:L508-L514). Predicate is a random `PREDICATES` value (tests/benchmarks/data_generator.py:L515-L515). `valid_from` is `2024-01-01` plus a random offset of 0..730 days, formatted `YYYY-MM-DD` (tests/benchmarks/data_generator.py:L509-L517). With 30% probability `valid_to` is set to `valid_from`'s base offset plus an additional 30..365 days (formatted `YYYY-MM-DD`); otherwise `valid_to` is null (tests/benchmarks/data_generator.py:L519-L524). Returns the 2-tuple `(entities, triples)` (tests/benchmarks/data_generator.py:L527-L527).

### `generate_search_queries(n_queries=None)`

Defaults to `cfg["search_queries"]` (tests/benchmarks/data_generator.py:L538-L538). Builds two halves. The needle half has count `min(n_queries // 2, len(self.needles))`; each record is `{query, expected_wing, expected_room, needle_id, is_needle=True}` sourced from the first N needles, providing known-good answers for recall (tests/benchmarks/data_generator.py:L541-L552). The generic half has count `n_queries - n_needle`; each record is `{query = "<tech_term> <tech_term>", expected_wing=None, expected_room=None, needle_id=None, is_needle=False}` (tests/benchmarks/data_generator.py:L554-L565). The full combined list is shuffled in place using the seeded RNG before return, so needle and generic queries are interleaved (tests/benchmarks/data_generator.py:L567-L568). Returns the list of query records (tests/benchmarks/data_generator.py:L568-L568).

## Externally Observable Contracts

- Drawer id format: `drawer_<wing>_<room>_<16-hex>` where the hex is the first 16 characters of the MD5 hex digest of either `needle_id` (needles) or `"gen_<i>"` (generated drawers) (tests/benchmarks/data_generator.py:L427-L427, tests/benchmarks/data_generator.py:L456-L456).
- ChromaDB collection name is exactly `mempalace_drawers` (tests/benchmarks/data_generator.py:L416-L416).
- `mempalace.yaml` structure: top-level `wing` string and `rooms` list of `{name, description}` (tests/benchmarks/data_generator.py:L361-L363).
- Generated filenames: project files `file_<i:04d>.<ext>`, conversation files `convo_<i:04d>.txt`, drawer source_file metadata `generated_<i:06d>.txt` / `needle_<id>.txt` (tests/benchmarks/data_generator.py:L373-L373, tests/benchmarks/data_generator.py:L398-L398, tests/benchmarks/data_generator.py:L434-L434, tests/benchmarks/data_generator.py:L464-L464).
- Conversation user lines always begin with the literal marker `> User: ` (tests/benchmarks/data_generator.py:L392-L392).
- KG date strings are always `YYYY-MM-DD`; `valid_to` is nullable (tests/benchmarks/data_generator.py:L517-L524).
