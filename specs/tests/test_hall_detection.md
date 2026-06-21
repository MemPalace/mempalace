# Behavior Spec: Hall Detection in Miners

This is a TDD test suite that defines the required behavior of "hall detection" — routing text content to a named topical category ("hall") and persisting that category as drawer metadata. The tests were written before the implementation and constitute the contract (tests/test_hall_detection.py:L1-L4).

## Public Surface Under Test

- `detect_hall(text)` in module `mempalace.miner` — a callable that classifies a text string into a single hall name string (tests/test_hall_detection.py:L14-L17).
- `add_drawer(...)` in `mempalace.miner` — creates a drawer and persists metadata (tests/test_hall_detection.py:L80-L91).
- `mine(project_dir, palace_dir, wing_override, agent)` in `mempalace.miner` — full project mine pipeline (tests/test_hall_detection.py:L153-L168).
- `mine_convos(convo_dir, palace_dir, wing, agent)` in `mempalace.convo_miner` — conversation transcript miner (tests/test_hall_detection.py:L103-L116).
- `get_collection(palace_path[, create])` in `mempalace.palace` — returns a collection handle whose `.get(...)` returns a results object (tests/test_hall_detection.py:L79-L92).
- Module-level cache variable `_HALL_KEYWORDS_CACHE` in `mempalace.miner` (tests/test_hall_detection.py:L132-L146).

## detect_hall: Classification Contract

`detect_hall` MUST exist and be callable (tests/test_hall_detection.py:L14-L17).

Input: a single text string. Output: a hall name string. The classification maps representative phrasing to exactly these hall names:

- Technical content (e.g. "Fixed the python script bug in the error handler code") → `"technical"` (tests/test_hall_detection.py:L19-L23).
- Emotional content (e.g. "I feel so happy today, tears of joy, I love this") → `"emotions"` (tests/test_hall_detection.py:L25-L29).
- Family content (e.g. "The kids had a great day, my daughter was amazing") → `"family"` (tests/test_hall_detection.py:L31-L35).
- Memory content (e.g. "I remember when we archived all those files, recall the conversation") → `"memory"` (tests/test_hall_detection.py:L37-L41).
- Creative content (e.g. "The game design for the player app looks great") → `"creative"` (tests/test_hall_detection.py:L43-L47).
- Identity content (e.g. "Who am I really? My identity and persona and sense of self") → `"identity"` (tests/test_hall_detection.py:L49-L53).
- Consciousness content (e.g. "Am I conscious? Is this awareness real? Does my soul exist?") → `"consciousness"` (tests/test_hall_detection.py:L55-L59).

Fallback: content matching no hall keywords (e.g. "The weather is nice today in California") → `"general"` (tests/test_hall_detection.py:L61-L65).

Scoring / tie-break invariant: classification is keyword-score based, and the hall with the highest score wins. When a text contains keywords from multiple halls, the hall with the greater number of matching keywords is selected; e.g. a text with more technical keywords than emotional keywords (containing both "python/bug/code/script" and "felt happy") resolves to `"technical"` (tests/test_hall_detection.py:L67-L72).

## detect_hall: Config Caching Contract

`detect_hall` loads hall keyword configuration on first use and caches it to avoid re-reading from disk on every drawer. The cache is exposed as module attribute `_HALL_KEYWORDS_CACHE` (tests/test_hall_detection.py:L127-L135).

After the first call to `detect_hall`, `_HALL_KEYWORDS_CACHE` MUST be non-null (populated) (tests/test_hall_detection.py:L138-L139). Subsequent calls MUST reuse the same cached object identity — a second call does not replace or rebuild the cache (tests/test_hall_detection.py:L142-L146). Setting `_HALL_KEYWORDS_CACHE` to null forces a reload on the next call (tests/test_hall_detection.py:L135-L138).

## Drawer Metadata Contract

When a drawer is created, its persisted metadata MUST include a `"hall"` field (tests/test_hall_detection.py:L75-L94).

`add_drawer` accepts named parameters: `collection`, `wing`, `room`, `content`, `source_file`, `chunk_index`, and `agent` (tests/test_hall_detection.py:L83-L91). The `"hall"` value stored in metadata MUST be the result of classifying the drawer's `content`; e.g. content "Fixed the python script bug in the error handler code" yields `meta["hall"] == "technical"` (tests/test_hall_detection.py:L92-L95).

Metadata is retrievable via the collection's `get(limit, include=["metadatas"])`, which returns an object with a `"metadatas"` list (tests/test_hall_detection.py:L92-L94).

## Conversation Miner Contract

`mine_convos(convo_dir, palace_dir, wing=..., agent=...)` mines conversation transcript files from `convo_dir` into a palace at `palace_dir` (tests/test_hall_detection.py:L101-L116).

Input transcript format (observable): a plain text file where lines beginning with `> ` denote user prompts and following lines denote responses; the test fixture writes such a `session.txt` with multiple prompt/response turns (tests/test_hall_detection.py:L110-L114).

After mining, at least one drawer MUST exist (`results["ids"]` non-empty) (tests/test_hall_detection.py:L118-L121). Every drawer whose metadata field `ingest_mode == "convos"` MUST include a `"hall"` field in its metadata (tests/test_hall_detection.py:L122-L124).

## Project Mine Pipeline Contract

`mine(project_dir, palace_dir, wing_override=..., agent=...)` runs the full project mine pipeline (tests/test_hall_detection.py:L152-L168).

Project configuration is read from a `mempalace.yaml` file in `project_dir` containing at least `wing` and a `rooms` list of `{name, description}` entries (tests/test_hall_detection.py:L160-L163). Source files in the project (e.g. a `.py` file) are mined into drawers (tests/test_hall_detection.py:L164-L168).

Invariant: every drawer produced by the mine pipeline MUST include a `"hall"` field in its metadata (tests/test_hall_detection.py:L170-L173).

## Side Effects and Test Setup Observations

- Tests create a palace directory and a project/convo directory under a temp directory, then read drawers back from the collection opened with `create=False` (tests/test_hall_detection.py:L105-L118, tests/test_hall_detection.py:L156-L170).
- Drawer metadata is the externally observable contract surface: callers inspect `results["metadatas"]` and `results["ids"]` lists returned by `collection.get(...)` (tests/test_hall_detection.py:L92-L94, tests/test_hall_detection.py:L119-L124, tests/test_hall_detection.py:L171-L173).
- Fixtures `palace_path` and `tmp_dir` are supplied by the test harness (defined externally, e.g. conftest) and provide writable directory paths (tests/test_hall_detection.py:L78-L78, tests/test_hall_detection.py:L101-L101).
