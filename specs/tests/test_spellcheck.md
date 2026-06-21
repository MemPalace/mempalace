# Behavior Spec: `tests/test_spellcheck.py`

This file is a test suite that pins the observable behavior of the `mempalace.spellcheck` module. The specs below describe the contract that the spellcheck unit MUST satisfy in order for these tests to pass; they are implementation-language-agnostic.

The module under test exposes six entry points: `_edit_distance`, `_get_system_words`, `_should_skip`, `spellcheck_transcript`, `spellcheck_transcript_line`, and `spellcheck_user_text` (tests/test_spellcheck.py:L5-L12).

## `_should_skip(token, known_names_set) -> bool`

Token-level predicate that decides whether a single token must be left untouched by spell correction. Returns a boolean (tests/test_spellcheck.py:L18-L19).

- Very short tokens are skipped (return `True`): tokens of length 2 such as `"hi"`, `"ok"`, and the single-character token `"I"` (tests/test_spellcheck.py:L21-L24).
- Tokens containing digits are skipped: `"3am"`, `"top10"`, `"bge-large-v1.5"` (tests/test_spellcheck.py:L26-L29).
- CamelCase / mixed-case tokens are skipped: `"ChromaDB"`, `"MemPalace"` (tests/test_spellcheck.py:L31-L33).
- All-caps tokens are skipped, including those with underscores: `"NDCG"`, `"MAX_RESULTS"` (tests/test_spellcheck.py:L35-L37).
- Technical-looking tokens containing hyphens or underscores are skipped: `"bge-large"`, `"train_test"` (tests/test_spellcheck.py:L39-L41).
- URLs are skipped: `"https://example.com"`, `"www.google.com"` (tests/test_spellcheck.py:L43-L45).
- Tokens with code/markup or emoji-style characters are skipped: backtick-wrapped `` `code` `` and double-asterisk `**bold**` (tests/test_spellcheck.py:L47-L49).
- A token that appears in the supplied `known_names` set is skipped; e.g. `"mempalace"` with set `{"mempalace"}` returns `True` (tests/test_spellcheck.py:L51-L52).
- A normal lowercase dictionary word that matches none of the above conditions is NOT skipped (returns `False`): `"hello"`, `"question"` (tests/test_spellcheck.py:L54-L56).

## `_edit_distance(a, b) -> int`

Computes the Levenshtein edit distance between two strings, counting single-character insertions, deletions, and substitutions, returning a non-negative integer (tests/test_spellcheck.py:L62-L77).

- Identical strings have distance 0: `_edit_distance("hello", "hello") == 0` (tests/test_spellcheck.py:L63-L64).
- Distance against the empty string equals the length of the other string: `("", "abc") == 3`, `("abc", "") == 3`, `("", "") == 0` (tests/test_spellcheck.py:L66-L69).
- A single substitution, insertion, or deletion each cost 1: `("cat","bat") == 1`, `("cat","cats") == 1`, `("cats","cat") == 1` (tests/test_spellcheck.py:L71-L74).
- Known reference case: `_edit_distance("kitten", "sitting") == 3` (tests/test_spellcheck.py:L76-L77).

## `_get_system_words() -> set`

Returns a set of words drawn from the host system's dictionary/word source. The return value MUST be a set (collection of unique strings); it may be empty (tests/test_spellcheck.py:L83-L85).

## `spellcheck_user_text(text, known_names=...) -> str`

Corrects misspelled words in a block of user text and returns the corrected text as a string.

- Passthrough when no speller backend is available: if the speller provider yields nothing (no autocorrect installed), the input text is returned completely unchanged, including all original misspellings (tests/test_spellcheck.py:L91-L95).
- When a speller backend is available, candidate words are passed to it and replaced with the speller's correction. Given a speller that maps `"knoe"->"know"` and `"befor"->"before"`, the output of `spellcheck_user_text("knoe the question befor")` contains `"know"` and `"before"` (tests/test_spellcheck.py:L98-L110).
- Technical terms are protected from correction even when a speller is active. Given a speller that would replace every word with `"WRONG"`, calling `spellcheck_user_text("ChromaDB bge-large", known_names=set())` leaves `"ChromaDB"` and `"bge-large"` intact and never introduces `"WRONG"` (tests/test_spellcheck.py:L113-L124). This protection is the `_should_skip` logic applied per token.
- The function consults a system-words source and a known-names source when deciding what to correct; these can be supplied/overridden (the `known_names` argument) and the system words via `_get_system_words` (tests/test_spellcheck.py:L106-L121).

## `spellcheck_transcript_line(line) -> str`

Corrects a single transcript line, treating only user turns as eligible for correction. A turn is identified by the leading `>` marker.

- Lines beginning with `>` are user turns: their message body is run through `spellcheck_user_text`, and the returned line reflects that correction. For input `"> hello world"` with `spellcheck_user_text` returning `"corrected"`, the result contains `"corrected"` (tests/test_spellcheck.py:L130-L134).
- Lines that do not begin with `>` are assistant turns (or other content) and are returned unchanged byte-for-byte: `"This is an assistant response"` is returned identical (tests/test_spellcheck.py:L137-L140).
- A user-turn marker with no message content, the line `"> "`, passes through unchanged (tests/test_spellcheck.py:L143-L146).

## `spellcheck_transcript(content) -> str`

Corrects an entire multi-line transcript, preserving line structure and only altering user-turn lines.

- The input is treated as newline-separated lines; the output preserves the same line ordering and line count (tests/test_spellcheck.py:L152-L160).
- Only lines beginning with `>` are corrected; all other lines pass through unchanged. For input `"Assistant line\n> user line\nAnother assistant line"` with `spellcheck_user_text` stubbed to return `"fixed"`: line 0 stays `"Assistant line"`, line 1 contains `"fixed"`, and line 2 stays `"Another assistant line"` (tests/test_spellcheck.py:L152-L160).

## Invariants summary

- Correction is non-destructive to non-eligible content: skipped tokens, assistant lines, and empty user turns are returned exactly as given (tests/test_spellcheck.py:L113-L124, L137-L146).
- Absence of a speller backend is a safe no-op, never an error (tests/test_spellcheck.py:L91-L95).
- Line and token ordering is preserved across transcript-level processing (tests/test_spellcheck.py:L156-L160).
