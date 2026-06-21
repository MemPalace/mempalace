# fact_checker — Behavior Specification

Source: `mempalace/fact_checker.py`

## Purpose

Verifies free text (AI responses, diary entries, new content) against two known-fact sources in the palace: the entity registry and the knowledge graph (KG). Detects three classes of issue: `similar_name`, `relationship_mismatch`, and `stale_fact` (mempalace/fact_checker.py:L1-L17). The component is fully offline — it performs no network access; its only inputs are an entity-registry JSON and a KG SQLite database (mempalace/fact_checker.py:L17-L17).

## Public surface

### `check_text(text, palace_path=None, config=None) -> list`

Returns a list of issue objects detected in `text`; an empty list means no contradictions were found (mempalace/fact_checker.py:L55-L78).

Resolution of optional inputs: if `config` is not supplied, a default configuration object is constructed (mempalace/fact_checker.py:L62-L65). If `palace_path` is not supplied, it is taken from the configuration's `palace_path` attribute (mempalace/fact_checker.py:L66-L67).

Edge case: if `text` is empty/falsy, an empty list is returned immediately with no further work (mempalace/fact_checker.py:L69-L70).

Otherwise it loads the raw known-entities registry, runs entity-confusion checks then KG-contradiction checks, concatenating both result lists in that order (mempalace/fact_checker.py:L72-L78). The registry is loaded via a shared mtime-cached loader so repeated calls do not re-read the registry file on every invocation (mempalace/fact_checker.py:L35-L37, L73).

## Issue object shapes (observable contract)

Each detected issue is an object with a `type` field. Three types exist.

### `similar_name`

Fields: `type` = `"similar_name"`; `detail` = a human-readable string of the form `'<name_a>' mentioned — did you mean '<name_b>'? (edit distance <d>)`; `names` = a two-element list `[name_a, name_b]`; `distance` = the integer edit distance (mempalace/fact_checker.py:L137-L147).

### `relationship_mismatch`

Fields: `type` = `"relationship_mismatch"`; `detail` = string `Text says '<span>' but KG records <subject> <kg_pred> <kg_object>`; `entity` = the claim subject; `claim` = object with `predicate` (lowercased claim predicate) and `object` (claim object); `kg_fact` = object with `predicate` (lowercased KG predicate) and `object` (KG object) (mempalace/fact_checker.py:L233-L250).

### `stale_fact`

Fields: `type` = `"stale_fact"`; `detail` = string `Text says '<span>' but KG marks this fact closed on <valid_to>`; `entity` = the claim subject; `valid_to` = the closing date value from the KG fact (mempalace/fact_checker.py:L265-L275).

## Entity-name confusion detection

The registry is flattened into a flat set of names. The registry may take either shape: `{category: [names]}` (each list contributes its non-empty string elements) or `{category: {name: meta}}` (each dict contributes its non-empty keys) (mempalace/fact_checker.py:L84-L93). If the flattened set is empty, no confusion issues are produced (mempalace/fact_checker.py:L106-L108).

Mentioned names are those registry names that appear in `text` as whole-word matches, case-insensitively (mempalace/fact_checker.py:L110-L114). If no registry name is mentioned, no confusion issues are produced (mempalace/fact_checker.py:L115-L116).

For each mentioned name, it is compared against every other registry name (mempalace/fact_checker.py:L120-L124). Comparison uses an unordered, case-insensitive pair key so a given pair is never reported more than once (mempalace/fact_checker.py:L125-L128). A candidate name that was itself mentioned in the text is skipped (the pair is recorded as seen) — the rule only flags a different registry entry that was NOT mentioned, so two real names both appearing in the text are not flagged against each other (mempalace/fact_checker.py:L129-L134). An issue is emitted only when the Levenshtein edit distance between the two lowercased names is greater than 0 and at most 2 (mempalace/fact_checker.py:L135-L148).

## Claim extraction

Two relationship surface forms are recognized via patterns over the text:

- `X is Y's Z` → captured as subject=X, possessor=Y, role=Z (mempalace/fact_checker.py:L47-L49, L167-L168).
- `X's Z is Y` → captured as possessor=X, role=Z, subject=Y (mempalace/fact_checker.py:L50-L51, L169-L170).

Both forms resolve to the triple subject=X-actor, predicate=role, object=possessor (mempalace/fact_checker.py:L155-L178). Name groups require an initial uppercase letter followed by word characters/hyphens; role/relationship words are lowercase, length 3 to 20 characters (mempalace/fact_checker.py:L47-L52). Each extracted claim is an object with `subject`, `predicate` (lowercased role), `object` (possessor), and `span` (the full matched substring) (mempalace/fact_checker.py:L171-L178).

## KG contradiction detection

If no claims are extracted from the text, no KG issues are produced (mempalace/fact_checker.py:L195-L197).

The KG is opened from the SQLite file at `<palace_path>/knowledge_graph.sqlite3` (mempalace/fact_checker.py:L199-L207). If the KG cannot be opened for any reason (new palace, corrupted DB, etc.), the KG check is skipped and returns no issues (mempalace/fact_checker.py:L208-L210).

For each claim, the subject's outgoing KG triples are queried (mempalace/fact_checker.py:L213-L218). If that lookup fails, the claim is skipped and the failure is logged at debug level; processing continues with the next claim (mempalace/fact_checker.py:L219-L221). If the subject has no facts, the claim is skipped (mempalace/fact_checker.py:L222-L223).

Subject matching to the KG is case-insensitive on normalized IDs (mempalace/fact_checker.py:L160-L161). Object matching between a KG fact object and a claim object treats both as strings compared after trimming and lowercasing; a null KG object or empty claim object never matches (mempalace/fact_checker.py:L280-L283).

### relationship_mismatch rule

Among the subject's facts marked `current`, for any fact whose object matches the claim's object, if the KG predicate is non-empty and differs (case-insensitively) from the claim predicate, a `relationship_mismatch` issue is emitted (mempalace/fact_checker.py:L225-L250).

### stale_fact rule

Among the subject's facts that are NOT `current`, for any fact whose predicate equals the claim predicate (case-insensitively) and whose object matches the claim object, if the fact's `valid_to` value is present and, compared as a string, is strictly less than today's UTC date in `YYYY-MM-DD` form, a `stale_fact` issue is emitted (mempalace/fact_checker.py:L252-L275). The "now" reference date is the current UTC calendar date rendered in ISO `YYYY-MM-DD` format (mempalace/fact_checker.py:L254-L254).

Multiple issues may be emitted per claim; both rules run over the same fact set independently (mempalace/fact_checker.py:L228-L275).

## Edit distance (invariant)

`_edit_distance(s1, s2)` computes standard Levenshtein distance with cost 1 for insertion, deletion, and substitution, and 0 for a matching character (mempalace/fact_checker.py:L289-L307). It is symmetric (operands are swapped so the longer is iterated) and returns the length of the non-empty string when one input is empty (mempalace/fact_checker.py:L291-L294).

## CLI

Invoked as a module entry point (`python -m mempalace.fact_checker`) (mempalace/fact_checker.py:L324-L355).

Arguments:
- positional `text` (optional): the text to check (mempalace/fact_checker.py:L335-L335).
- `--palace` (default `~/.mempalace/palace`, tilde-expanded): path to the palace directory (mempalace/fact_checker.py:L336-L340).
- `--stdin` (flag): read text from standard input instead of the positional argument (mempalace/fact_checker.py:L341-L341).

Input selection: with `--stdin`, all of stdin is read; otherwise the positional `text` is used; if neither is provided, the program errors out via the argument parser (mempalace/fact_checker.py:L344-L349).

Output and exit codes (observable contract): if any issues are found, the issue list is printed as JSON (2-space indented) and the process exits with code 1 (mempalace/fact_checker.py:L351-L354). If no issues are found, the literal line `No contradictions found.` is printed and the process exits 0 (the documented contract: exit 0 when none, 1 when one or more) (mempalace/fact_checker.py:L333-L333, L355-L355).

## Side effects and environment

- Filesystem reads only: the entity registry file (via the shared cached loader) and the KG SQLite file at `<palace_path>/knowledge_graph.sqlite3` (mempalace/fact_checker.py:L73-L73, L207-L207). No writes.
- No network access (mempalace/fact_checker.py:L17-L17).
- On Windows, standard I/O is reconfigured to UTF-8 before CLI processing; stdout/stderr use `replace` error handling so surrogate halves round-tripped from filenames do not raise on print, while stdin keeps `surrogateescape` (mempalace/fact_checker.py:L310-L321, L329-L329).
- Logging is emitted under the logger named `mempalace_mcp` (debug-level only, for failed KG lookups) (mempalace/fact_checker.py:L39-L39, L220-L220).
