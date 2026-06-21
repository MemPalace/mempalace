# Behavior Spec: `fact_checker` (derived from `tests/test_fact_checker.py`)

This spec describes the observable contract of the `fact_checker` module as
pinned by its regression + integration test suite. It defines the public
surface, input/output shapes, invariants, error handling, side effects, and
externally observable contracts (exit codes, stdio reconfiguration). Every
claim cites the test that asserts it.

## Public surface

The module exposes these callable units, all imported and exercised by tests
(`tests/test_fact_checker.py:L26-L33`):

- `_extract_claims(text)` — parse relationship claims from prose.
- `_check_entity_confusion(text, registry)` — flag similarly-named entities.
- `_edit_distance(a, b)` — Levenshtein edit distance.
- `_flatten_names(registry)` — collect candidate names from a registry.
- `check_text(text, palace_path)` — end-to-end fact check returning an issue list.
- `_reconfigure_stdio_utf8_on_windows()` — platform-conditional stdio reconfiguration.
- A `__main__` entry point usable as a CLI (`tests/test_fact_checker.py:L278-L284`).

## Claim extraction — `_extract_claims(text) -> list[dict]`

Recognizes possessive-role relationship sentences in two shapes and returns one
claim dict per recognized relationship. Each claim is a dict with keys
`subject`, `predicate`, `object`, and `span` (the matched source substring)
(`tests/test_fact_checker.py:L40-L48`).

- Shape "X is Y's Z": `"Bob is Alice's brother"` yields exactly one claim
  `{subject:"Bob", predicate:"brother", object:"Alice", span:"Bob is Alice's brother"}`
  (`tests/test_fact_checker.py:L40-L48`).
- Shape "Y's Z is X": `"Alice's brother is Bob"` yields one claim with
  `subject="Bob"`, `predicate="brother"`, `object="Alice"`
  (`tests/test_fact_checker.py:L50-L55`). Note: in both shapes, the named person
  filling the role becomes `subject` and the possessive owner becomes `object`.
- Sentences with no possessive-role construction produce an empty list
  (e.g. `"Bob drove to the store today"`, `"Just some prose without relationships"`)
  (`tests/test_fact_checker.py:L57-L59`).
- Multiple sentences each contribute their own claim; `"Bob is Alice's brother.
  Carol is Dave's sister."` yields claims whose subject set is `{"Bob","Carol"}`
  (`tests/test_fact_checker.py:L61-L64`).

## Entity confusion — `_check_entity_confusion(text, registry) -> list[dict]`

Detects when text mentions one of two registry names that are near-duplicates
(small edit distance), suggesting the user may have confused them. Returns a
list of issue dicts.

- When exactly one of two near-identical registered names is mentioned, emit one
  issue of `type == "similar_name"`. For registry `{"people":["Milla","Mila"]}`
  and text mentioning only `"Mila"`, the issue has `names` equal to the set
  `{"Mila","Milla"}` and `distance == 1` (`tests/test_fact_checker.py:L71-L79`).
- No issue is emitted when both near-names appear in the text — the user clearly
  distinguishes them (`tests/test_fact_checker.py:L81-L86`).
- Empty registry, or a registry with an empty `people` list, yields no issues
  (`tests/test_fact_checker.py:L88-L90`).
- If no registered name is mentioned in the text, yields no issues
  (`tests/test_fact_checker.py:L92-L94`).
- A registry whose category is a dict (`{"people":{"Milla":{...},"Mila":{}}}`)
  is supported; the dict keys are treated as candidate names, so confusion is
  still surfaced (`tests/test_fact_checker.py:L96-L101`).

### Performance invariant

Edit-distance comparison MUST be scoped to names actually mentioned in the text,
not the full registry. With 500 registered names and zero mentions, the call
returns an empty list and completes in under 0.2 seconds — no pairwise
comparison should even begin (`tests/test_fact_checker.py:L115-L134`).

## Edit distance — `_edit_distance(a, b) -> int`

Standard Levenshtein distance over the two strings.

- `("kitten","sitting") == 3`, `("mila","milla") == 1`, `("abc","abc") == 0`
  (`tests/test_fact_checker.py:L105-L108`).
- Empty-string handling: `("","") == 0`, `("abc","") == 3`, `("","abc") == 3`
  (`tests/test_fact_checker.py:L110-L113`).

## Name flattening — `_flatten_names(registry) -> set[str]`

Collects all candidate names across registry categories into a set.

- List-valued category: `{"people":["Ada","Bob"]}` -> `{"Ada","Bob"}`
  (`tests/test_fact_checker.py:L141-L142`).
- Dict-valued category: `{"people":{"Ada":{},"Bob":{}}}` -> `{"Ada","Bob"}`
  (`tests/test_fact_checker.py:L144-L145`).
- Falsy entries (empty string, null) are skipped:
  `{"people":["Ada","",None,"Bob"]}` -> `{"Ada","Bob"}`
  (`tests/test_fact_checker.py:L147-L148`).

## Knowledge-graph integration — `check_text(text, palace_path) -> list[dict]`

### KG location and call signature contract

The knowledge graph lives at `<palace>/knowledge_graph.sqlite3` and is opened
with the `db_path` argument (not a `palace_path` argument)
(`tests/test_fact_checker.py:L154-L166`). The constructor accepts `db_path` and
the in-memory value `":memory:"`; the graph object exposes a callable
`query_entity` method and does NOT expose a `query` method — fact_checker must
call `query_entity` (`tests/test_fact_checker.py:L170-L181`). Triples are stored
via `add_triple(subject, predicate, object, valid_from=..., valid_to=...)`
(`tests/test_fact_checker.py:L186`, `L210-L216`, `L225`).

### Relationship mismatch

For a claim that shares the same `(subject, object)` pair as a KG triple but
disagrees on predicate, emit an issue of `type == "relationship_mismatch"`.

- With KG triple `("Bob","husband_of","Alice", valid_from="2020-01-01")`, the
  text `"Bob is Alice's husband_of"` (matching predicate and object) produces NO
  `relationship_mismatch` issue (`tests/test_fact_checker.py:L186-L190`).
- The text `"Bob is Alice's brother"` against that same KG produces a mismatch
  whose `entity == "Bob"`, `claim["predicate"] == "brother"`, and
  `kg_fact["predicate"] == "husband_of"` (`tests/test_fact_checker.py:L192-L198`).
- If the KG has no facts about the subject, no mismatch fires; result is `[]`
  (`tests/test_fact_checker.py:L200-L203`).

### Stale fact

A claim matching a KG triple whose validity window is closed (a `valid_to` in
the past) is flagged with `type == "stale_fact"`.

- KG triple `("Bob","brother","Alice", valid_from="2010-01-01",
  valid_to="2023-06-01")` plus text `"Bob is Alice's brother"` yields at least
  one `stale_fact` issue with `entity == "Bob"` and `valid_to` beginning with
  `"2023"` (`tests/test_fact_checker.py:L205-L221`).
- An open-window triple (no `valid_to`) matching the same claim is NOT flagged;
  text `"Bob is Alice's brother"` against `("Bob","brother","Alice",
  valid_from="2010-01-01")` returns `[]` (`tests/test_fact_checker.py:L223-L227`).

### Missing palace robustness

When the palace path does not exist (no KG file present), `check_text` returns
`[]` rather than raising or hanging (`tests/test_fact_checker.py:L229-L233`).

## End-to-end `check_text` contract

- Empty input text returns `[]` (`tests/test_fact_checker.py:L240-L241`).
- The registry-confusion path is independent of the KG path: if a registry file
  is present but the KG/palace is missing, the `similar_name` path still fires.
  The registry is sourced through `miner._ENTITY_REGISTRY_PATH` (a JSON file
  whose shape is `{"people":[...]}`) with a cache object
  `miner._ENTITY_REGISTRY_CACHE` carrying keys `mtime`, `names`, `raw`; resetting
  the cache (`mtime=None`, empty `names`, empty `raw`) forces a reload. With
  registry `{"people":["Milla","Mila"]}` and text `"Chatted with Mila."` against
  a nonexistent palace, at least one issue has `type == "similar_name"`
  (`tests/test_fact_checker.py:L243-L256`).

## CLI contract (`__main__`)

Invoked as a module entry point. Argument vector form:
`[prog, <text>, "--palace", <palace_path>]` (`tests/test_fact_checker.py:L274-L282`).

- When issues are found, the process exits with code `1`
  (`tests/test_fact_checker.py:L278-L284`).
- The detected issue types are printed to standard output; e.g. `"similar_name"`
  appears in stdout when a similar-name issue is found
  (`tests/test_fact_checker.py:L285-L286`).

## Windows stdio reconfiguration — `_reconfigure_stdio_utf8_on_windows()`

On Windows (`sys.platform == "win32"`), the helper reconfigures the three
standard streams to UTF-8 with per-stream error policies
(`tests/test_fact_checker.py:L290-L327`):

- `stdin` is reconfigured with `encoding="utf-8", errors="surrogateescape"` so a
  stray malformed byte from a redirected input file does not crash the read
  (`tests/test_fact_checker.py:L313-L325`).
- `stdout` is reconfigured with `encoding="utf-8", errors="replace"`
  (`tests/test_fact_checker.py:L326`).
- `stderr` is reconfigured with `encoding="utf-8", errors="replace"` so an
  extracted fact carrying a surrogate half does not crash mid-print
  (`tests/test_fact_checker.py:L320-L327`).

On non-Windows platforms (e.g. `sys.platform == "linux"`), the helper is a
no-op and must not call `reconfigure` on any stream
(`tests/test_fact_checker.py:L329-L351`).
