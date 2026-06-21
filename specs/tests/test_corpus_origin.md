# Spec: corpus-origin detection

Derived from the test suite `tests/test_corpus_origin.py`, which exercises the
public surface of a `corpus_origin` module. The module answers one foundational
question before downstream classification: is a corpus a record of AI-agent
dialogue, and if so which platform and what persona-names the user assigned to
the agent (tests/test_corpus_origin.py:L1-L22).

The default stance is "this IS an AI-dialogue corpus" unless there is strong
evidence otherwise; a false negative (missing an AI corpus) is treated as
catastrophic, a false positive as recoverable (tests/test_corpus_origin.py:L17-L20).

## Public surface

Three symbols are exported and consumed by callers: a result type
`CorpusOriginResult`, a heuristic detector `detect_origin_heuristic`, and an
LLM-assisted detector `detect_origin_llm` (tests/test_corpus_origin.py:L24-L28).

## `CorpusOriginResult` type

`CorpusOriginResult` is constructed with the keyword fields
`likely_ai_dialogue` (boolean), `confidence` (number), `primary_platform`
(string or null), `agent_persona_names` (list of strings), and `evidence`
(list of strings). Each field is readable back as set (tests/test_corpus_origin.py:L371-L382).

The result exposes a `to_dict()` method producing a mapping with at least the
keys `likely_ai_dialogue`, `primary_platform`, and `agent_persona_names`,
preserving the stored values including a null `primary_platform` and an empty
`agent_persona_names` list (tests/test_corpus_origin.py:L384-L395).

The result may also carry a `user_name` field (string or null) used by the LLM
path; see below (tests/test_corpus_origin.py:L324-L324, tests/test_corpus_origin.py:L362-L362).

## Tier 1 — `detect_origin_heuristic(samples)`

Input is a list of text strings (drawer-text samples); output is a
`CorpusOriginResult`. This tier uses content-aware heuristics only (no LLM /
no network) — pattern matching over known AI brand terms and conversational
turn markers (tests/test_corpus_origin.py:L9-L12, tests/test_corpus_origin.py:L31-L45).

### Positive detection

A corpus with abundant AI-brand references plus turn markers (e.g. Claude /
Opus / Sonnet / Haiku / MCP and `user:` / `assistant:` / `human:` lines) is
detected with `likely_ai_dialogue is True` and `confidence >= 0.8`, and the
evidence list contains the matched brand term (here "Claude", case-insensitive)
(tests/test_corpus_origin.py:L35-L50).

A GPT/ChatGPT/OpenAI corpus is likewise detected as AI-dialogue, with at least
one evidence entry mentioning "GPT", "ChatGPT", or "OpenAI"
(tests/test_corpus_origin.py:L52-L61).

Strong turn-marker presence alone is sufficient to flag AI-dialogue even with
no AI brand mention; markers include `user:`/`assistant:` and `human:`/`ai:`
pairs (tests/test_corpus_origin.py:L90-L98).

Brand-term matching is case-insensitive: lowercase forms such as
`claude code`, `chatgpt`, `gemini-pro`, `mcp`, `anthropic`, `haiku` must match
the same as proper-cased forms, with no reliance on a turn-marker fallback. In
such a corpus the result is `likely_ai_dialogue is True`, and the evidence
strings reflect multiple distinct case-insensitive brand matches (at least two
of `chatgpt`, `anthropic`, `haiku`, `gemini-pro` appear in evidence)
(tests/test_corpus_origin.py:L102-L125).

### Word-sense disambiguation (negative detection)

Pure narrative / journal prose with no AI signals is flagged
`likely_ai_dialogue is False` with `confidence >= 0.8`
(tests/test_corpus_origin.py:L63-L74).

An astrology corpus with high "Gemini" density but zero unambiguous AI signals
(no MCP/LLM/ChatGPT/turn markers) must NOT be flagged as AI-dialogue:
Gemini-the-zodiac-sign is distinguished from Gemini-the-AI-platform
(tests/test_corpus_origin.py:L127-L143).

A French novel where "Claude" is a character name must NOT trip AI-dialogue
detection; disambiguation is by context, not by the mere presence of the word
(tests/test_corpus_origin.py:L145-L160).

A poetry/music corpus with high "haiku", "sonnet", "opus" density but no AI
infrastructure terms must NOT be flagged as AI-dialogue
(tests/test_corpus_origin.py:L162-L177).

### Word-boundary matching

Brand-term matching uses word boundaries: embedded substrings inside larger
words (e.g. "Claudette" containing "Claude", "opuscule" containing "opus",
"sonneteer" containing "sonnet", "llamas" containing "llama", "Bardic"
containing "bard") must NOT be counted as brand hits. Such false matches must
not appear in the evidence audit trail — for each embedded term the quoted form
`'<term>'` is absent from the evidence, and the corpus is classified
`likely_ai_dialogue is False` (tests/test_corpus_origin.py:L179-L213).

### Co-occurrence rule

When an ambiguous brand term ("Gemini") co-occurs in the same corpus with an
unambiguous AI signal (turn markers, MCP, ChatGPT, Claude Code, gemini-pro),
the ambiguous hits count and the corpus is flagged
`likely_ai_dialogue is True` (tests/test_corpus_origin.py:L215-L229).

### Default / low-signal stance

When evidence is thin or mixed, the heuristic defaults to assuming AI-dialogue:
`likely_ai_dialogue is True` but with low `confidence <= 0.6`, and the evidence
list contains a marker string whose lowercased form includes "default-stance"
(tests/test_corpus_origin.py:L76-L88).

## Tier 2 — `detect_origin_llm(samples, provider)`

Input is a list of text samples plus an LLM provider object; output is a
`CorpusOriginResult`. This tier sends a sample of drawer texts to an LLM to
confirm the platform and extract agent persona-names
(tests/test_corpus_origin.py:L12-L15, tests/test_corpus_origin.py:L254-L272).

### Provider contract

The provider exposes a `classify(system, user, json_mode=True)` call that
returns an object with a `.text` attribute carrying the model's raw response
string, and a `check_available()` call returning an availability tuple. The
detector invokes `classify` with system and user prompts
(tests/test_corpus_origin.py:L235-L251).

### Response shape (JSON contract)

The LLM is expected to return JSON with fields: `is_ai_dialogue_corpus`
(boolean), `confidence` (number), `primary_platform` (string or null),
`agent_persona_names` (list of strings), `evidence` (list of strings), and
optionally `user_name` (string) (tests/test_corpus_origin.py:L256-L266,
tests/test_corpus_origin.py:L280-L286, tests/test_corpus_origin.py:L313-L320).

### Mapping to result

On a valid AI-dialogue response, `likely_ai_dialogue is True`, the parsed
`confidence` is surfaced (e.g. `>= 0.9`), the extracted persona names appear in
`agent_persona_names`, and `primary_platform` carries the platform string
(e.g. contains "Claude") (tests/test_corpus_origin.py:L255-L277).

On a negative response, `likely_ai_dialogue is False`, `agent_persona_names`
is the empty list, and `primary_platform` is null
(tests/test_corpus_origin.py:L279-L292).

### user_name extraction and persona filtering

When the response includes `user_name`, it is surfaced as `result.user_name`
(e.g. "Jordan", "Sarah") (tests/test_corpus_origin.py:L322-L324,
tests/test_corpus_origin.py:L361-L363).

The user's name must be stripped from `agent_persona_names` if it appears in
both fields. The user is the human author, not an agent persona. Real personas
are preserved while the user's name is removed
(tests/test_corpus_origin.py:L307-L329).

This filtering is case-insensitive: all case-variants of the user's name
(e.g. "jordan", "JORDAN" when `user_name` is "Jordan") are removed from
`agent_persona_names`, leaving the remaining personas in order (e.g.
`["Echo", "Cipher"]`) (tests/test_corpus_origin.py:L331-L346).

### Malformed-response fallback

If the LLM returns text that is not valid JSON, the detector falls back
gracefully to the conservative default: `likely_ai_dialogue is True` with low
`confidence <= 0.5`, and the evidence list contains a string whose lowercased
form includes "fallback" or "error"
(tests/test_corpus_origin.py:L294-L305).
