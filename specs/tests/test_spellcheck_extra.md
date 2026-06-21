# Behavior Spec: `tests/test_spellcheck_extra.py`

This file is a test suite asserting the externally observable behavior of two
functions in the spellcheck module: `_load_known_names` and
`spellcheck_user_text` (tests/test_spellcheck_extra.py:L1-L8). The behaviors
below are the contracts those functions MUST satisfy.

## Subject under test: surface

The spellcheck module exposes (at minimum) the symbols `_load_known_names` and
`spellcheck_user_text`, plus three replaceable dependency accessors used by the
spellchecker: `_get_speller`, `_get_system_words`, and `_load_known_names`
(tests/test_spellcheck_extra.py:L5-L8, L43-L45). `spellcheck_user_text` takes a
single text string and returns a corrected text string
(tests/test_spellcheck_extra.py:L46-L48).

## `_load_known_names`

### Contract: derive known names from the entity registry

`_load_known_names` takes no arguments and returns a set of strings
(tests/test_spellcheck_extra.py:L22-L25). It obtains its data by loading an
entity registry from the `entity_registry` module (the registry exposes a
`load` operation that returns a registry object holding a `_data` mapping)
(tests/test_spellcheck_extra.py:L20-L22).

The registry data is a mapping with an `entities` key whose value maps entity
ids to entity records. Each entity record has a `canonical` name string and an
`aliases` list of strings (tests/test_spellcheck_extra.py:L14-L19). The returned
set MUST contain both the canonical name and every alias of every entity, each
lowercased: e.g. canonical `"Alice"` yields `"alice"`, alias `"ali"` yields
`"ali"`, and canonical `"Bob"` yields `"bob"`
(tests/test_spellcheck_extra.py:L22-L25). Names are normalized to lowercase in
the output set (tests/test_spellcheck_extra.py:L23-L25).

### Contract: fail-safe to empty set

If loading the entity registry raises any error, `_load_known_names` MUST NOT
propagate the error; instead it returns an empty set
(tests/test_spellcheck_extra.py:L27-L33).

## `spellcheck_user_text`

`spellcheck_user_text` performs per-word spell correction over the input text,
preserving words it decides not to correct. It depends on three replaceable
collaborators: a speller (from `_get_speller`) that maps a word to a proposed
replacement, a system-words set (from `_get_system_words`), and a known-names
set (from `_load_known_names`)
(tests/test_spellcheck_extra.py:L43-L45, L56-L58, L68-L70).

### Contract: capitalized words are never corrected

A word that is capitalized (treated as a likely proper noun) is left unchanged
even when the speller proposes a replacement. Given input `"Alice went home"`
and a speller that always returns `"WRONG"`, the result contains `"Alice"` and
does not contain `"WRONG"` (tests/test_spellcheck_extra.py:L37-L48).

### Contract: words in the system dictionary are never corrected

A word present in the system-words set is left unchanged even when the speller
proposes a replacement. Given input `"coherently"`, a speller that always
returns `"WRONG"`, and a system-words set containing `"coherently"`, the result
contains `"coherently"` (tests/test_spellcheck_extra.py:L50-L60).

### Contract: corrections exceeding an edit-distance threshold are rejected

When the speller's proposed replacement differs from the original word by too
many edits, the proposal is rejected and the original word is preserved. Given
input `"hello"` and a speller returning `"completely_different_word"`, the
result contains `"hello"` (tests/test_spellcheck_extra.py:L62-L72).

### Invariants implied by the tests

In all spellcheck cases above, the system-words set and known-names set were
empty unless explicitly populated, isolating each rejection rule
(tests/test_spellcheck_extra.py:L44-L45, L57-L58, L69-L70). The three rejection
rules (capitalization, system-dictionary membership, edit-distance threshold)
each independently cause the original word to be preserved rather than replaced
by the speller's output (tests/test_spellcheck_extra.py:L47-L48, L60, L72).
