# Spec: spellcheck

Spell-corrects user-authored message text before it is filed into the palace. The
module corrects genuine typos in lowercase flowing text while deliberately
preserving technical terms, identifiers, proper nouns, URLs, paths, and short
tokens (mempalace/spellcheck.py:L1-L21).

## Public surface

Four public functions:

- `spellcheck_user_text(text: string, known_names: set<string> | null = null) -> string` — corrects a single message (mempalace/spellcheck.py:L161-L172).
- `spellcheck_transcript_line(line: string) -> string` — corrects one transcript line, only if it is a user turn (mempalace/spellcheck.py:L215-L220).
- `spellcheck_transcript(content: string) -> string` — corrects all user turns in a multi-line transcript (mempalace/spellcheck.py:L235-L239).
- `_edit_distance(a, b) -> int` — Levenshtein distance helper used by the over-correction guard (mempalace/spellcheck.py:L136-L150).

## `spellcheck_user_text`

Inputs: raw message `text`; optional `known_names`, a set of lowercase
names/terms to preserve. Output: corrected text, or the original text unchanged
when the speller is unavailable (mempalace/spellcheck.py:L161-L175).

### Speller availability contract

If the underlying spell-correction engine is not installed/available, the
function returns the input text unchanged (pass-through). Availability is probed
once and cached (mempalace/spellcheck.py:L36-L46, mempalace/spellcheck.py:L173-L175).

### Known-names resolution

When `known_names` is `null`, the function attempts to load known names from the
entity registry. On any failure it falls back to an empty set, so correction
still proceeds (mempalace/spellcheck.py:L115-L128, mempalace/spellcheck.py:L177-L178). Names
loaded include each entity's canonical name (lowercased) and every alias
(lowercased) (mempalace/spellcheck.py:L121-L126).

### Tokenization and whitespace preservation

The text is split on runs of non-whitespace; each whitespace-delimited token is
processed independently and all original whitespace between tokens is preserved
exactly (mempalace/spellcheck.py:L158-L159, mempalace/spellcheck.py:L180-L212).

### Per-token correction rules (in order)

For each token:

1. Trailing punctuation in the set `.,!?;:'")` is stripped before checking and
   re-attached after correction; the stripped suffix is preserved verbatim
   (mempalace/spellcheck.py:L185-L187, mempalace/spellcheck.py:L210).
2. If the stripped token is empty, or matches any skip rule (see below), the
   original token is returned unchanged (mempalace/spellcheck.py:L189-L190).
3. If the stripped token's first character is uppercase, it is left unchanged
   (treated as a likely proper noun) (mempalace/spellcheck.py:L192-L194).
4. If the stripped token (lowercased) appears in the system word list, it is left
   unchanged — already-valid English words are never altered
   (mempalace/spellcheck.py:L196-L198).
5. Otherwise the speller produces a candidate correction
   (mempalace/spellcheck.py:L200).
6. Over-correction guard: if the candidate differs from the original, compute the
   edit distance. The maximum allowed edits is 2 when the stripped token length
   is <= 7, otherwise 3. If the distance exceeds the max, the original token is
   returned unchanged (mempalace/spellcheck.py:L204-L208).
7. Otherwise the corrected word plus the preserved trailing punctuation is
   returned (mempalace/spellcheck.py:L210).

### Skip rules (`_should_skip`)

A stripped token is preserved as-is if ANY of these hold, checked in order
(mempalace/spellcheck.py:L88-L107):

- Token length is less than 4 characters (mempalace/spellcheck.py:L85, mempalace/spellcheck.py:L90-L91).
- Token contains any digit (e.g. `3am`, `top-10`) (mempalace/spellcheck.py:L66, mempalace/spellcheck.py:L92-L93).
- Token is CamelCase — a lowercase run followed by an uppercase letter after an initial uppercase (e.g. `ChromaDB`, `MemPalace`) (mempalace/spellcheck.py:L69, mempalace/spellcheck.py:L94-L95).
- Token is entirely uppercase letters / underscores / listed symbols (e.g. `NDCG`, `MAX_RESULTS`) (mempalace/spellcheck.py:L72, mempalace/spellcheck.py:L96-L97).
- Token contains a hyphen or underscore (technical token, e.g. `bge-large`, `train_test`) (mempalace/spellcheck.py:L75, mempalace/spellcheck.py:L98-L99).
- Token looks URL-like or path-like: contains `http://`/`https://`, `www.`, `/Users/`, `~/`, or ends with a `.xx`–`.xxxx` extension (case-insensitive) (mempalace/spellcheck.py:L78, mempalace/spellcheck.py:L100-L101).
- Token contains code/markdown/emoji-marker characters from the set `` ` * _ # { } [ ] \ `` (mempalace/spellcheck.py:L81, mempalace/spellcheck.py:L102-L103).
- Token (lowercased) is in `known_names` (mempalace/spellcheck.py:L104-L106).

## System word list (side effect: filesystem read)

The system dictionary is read once from the absolute path `/usr/share/dict/words`
and cached. Each non-blank line is trimmed and lowercased to form a set. If the
file does not exist, the word set is empty (and correction proceeds without the
already-valid-word guard) (mempalace/spellcheck.py:L31-L33, mempalace/spellcheck.py:L49-L58).

## `_edit_distance`

Standard Levenshtein distance between two strings. Returns 0 when equal; returns
the length of the other string when one is empty; otherwise the minimum number of
single-character insertions, deletions, or substitutions (mempalace/spellcheck.py:L136-L150).

## Transcript handling

`spellcheck_transcript_line` only modifies lines whose first non-whitespace
character is `>` (user turns); all other lines (assistant turns) are returned
unchanged (mempalace/spellcheck.py:L215-L223). For a user line, the marker `>` plus one
following character (the `'> '` prefix) are skipped, and the remaining message is
corrected. The leading whitespace plus prefix is preserved exactly; if the
message portion is empty/whitespace-only, the line is returned unchanged
(mempalace/spellcheck.py:L221-L232).

`spellcheck_transcript` splits content on `\n`, applies the per-line correction to
each line, and rejoins with `\n`. The number of lines and line ordering are
preserved (mempalace/spellcheck.py:L235-L241).

## CLI / direct-execution behavior

When run as a script, it prints a header `Spell-check test` followed by a `=`×50
separator, then for each built-in test case prints the input (`IN:  <msg>`) and
either `OUT: <result> ← CHANGED` when the result differs or `OUT: (unchanged)`
when it does not. The test invocation passes `known_names={"riley","sam",
"mempalace"}` (mempalace/spellcheck.py:L248-L269).

## Invariants

- Whitespace between tokens and original line count/ordering are always preserved
  (mempalace/spellcheck.py:L158-L159, mempalace/spellcheck.py:L240-L241).
- Capitalized words and already-valid dictionary words are never altered
  (mempalace/spellcheck.py:L192-L198).
- Corrections that would change a token by more than the allowed edit budget are
  rejected (mempalace/spellcheck.py:L204-L208).
- No network or process side effects; the only external side effect is reading
  `/usr/share/dict/words` and the entity registry (mempalace/spellcheck.py:L49-L58, mempalace/spellcheck.py:L115-L128).
