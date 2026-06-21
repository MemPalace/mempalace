# Behavior Spec: Corpus-Origin Integration Tests

This is a test/specification module. It is a behavioral contract on the
`corpus_origin` feature: a two-tier ("Tier 1" heuristic + "Tier 2" LLM) detection
pass that classifies whether a project corpus is AI-dialogue and, if so, extracts
agent persona names so that downstream entity detection reclassifies those names
out of the "people" bucket. The tests pin the observable behavior of `cmd_init`,
`cmd_mine`, `detect_entities`, `discover_entities`, `refine_entities`, and
`_run_pass_zero`. This document describes those contracts as they must hold in any
implementation. (tests/test_corpus_origin_integration.py:L1-L23)

## Shared Test Fixtures

### AI-dialogue corpus fixture
A single project directory containing exactly one file `session_log.md` whose
content is a synthetic Claude Code transcript. The transcript contains a user name
"Jordan" and three agent persona names "Echo", "Sparrow", "Cipher" embedded in
dialogue markers, action verbs, and pronoun proximity — patterns that the plain
entity detector treats as person-evidence. Every name in the fixture is plausibly a
real human name. (tests/test_corpus_origin_integration.py:L43-L94)

### Corpus-origin result wrapper shape
The canonical wrapper object (`origin.json` shape) has these top-level keys:
`schema_version` (integer, value 1), `detected_at` (ISO-8601 UTC timestamp string),
and `result` (an object). The `result` object has keys: `likely_ai_dialogue`
(boolean), `confidence` (number), `primary_platform` (string or null), `user_name`
(string or null), `agent_persona_names` (array of strings), and `evidence` (array
of strings). (tests/test_corpus_origin_integration.py:L97-L111)

## Entity Detection With Corpus Origin

### Baseline (no corpus_origin)
`scan_for_detection(project_dir)` returns the files to scan; `detect_entities(files)`
returns an object with at least `people` and `uncertain` lists, each element an
object with a `name` field. Without corpus_origin context, the persona names "Echo",
"Sparrow", "Cipher" appear in the detected output (in `people` and/or `uncertain`),
and no `agent_personas` key exists on the returned object.
(tests/test_corpus_origin_integration.py:L117-L149)

### With corpus_origin
`detect_entities(files, corpus_origin=<wrapper>)` returns an object that includes a
new `agent_personas` list. All persona names from the wrapper's
`agent_persona_names` ("Echo", "Sparrow", "Cipher") appear in `agent_personas`
(elements have a `name` field), and none of them remain in the `people` list.
(tests/test_corpus_origin_integration.py:L155-L189)

### discover_entities threads corpus_origin
`discover_entities(project_dir, corpus_origin=<wrapper>)` (the higher-level entry
point) produces the same reclassification: persona names land in `agent_personas`
and none leak into `people`, regardless of how candidates were discovered.
(tests/test_corpus_origin_integration.py:L195-L222)

When `corpus_origin` is omitted, `discover_entities(project_dir)` must NOT include an
`agent_personas` key — the return shape is unchanged from the no-feature baseline.
(tests/test_corpus_origin_integration.py:L225-L240)

### Persona/user name collision
When a malformed wrapper has a `user_name` that also appears in
`agent_persona_names` (e.g. both equal "Claude"), the current consumer behavior
moves the colliding name into `agent_personas` (the name is in `agent_personas` OR
not in `people`); the split must not be ambiguous (name absent from both buckets is
a failure). This pins current loud-on-malformation behavior, not user-precedence
logic. (tests/test_corpus_origin_integration.py:L964-L1031)

### Topics and agent_personas coexist
When corpus_origin is provided, `detect_entities` must surface BOTH a `topics`
bucket (which may be empty) and the `agent_personas` bucket with the persona names.
The two buckets are additive and orthogonal; applying corpus_origin must preserve
keys it does not own. (tests/test_corpus_origin_integration.py:L1127-L1151)

## cmd_init — Pass 0 (corpus-origin detection)

### Writes origin.json
`cmd_init(args)` with `args.dir` pointing at a corpus, `args.yes=True`, and
`args.no_llm=True` must run corpus-origin detection BEFORE entity detection and
persist the result to `<palace_path>/.mempalace/origin.json`. The on-disk JSON has
`schema_version == 1`, a `detected_at` key, and a `result` key whose
`likely_ai_dialogue` is a boolean. For the AI-dialogue fixture, `likely_ai_dialogue`
is `true`. (tests/test_corpus_origin_integration.py:L258-L297)

### Threads corpus_origin to discover_entities
The Pass 0 result must be passed to `discover_entities` via a `corpus_origin` keyword
argument. The supplied value is non-null, has `schema_version == 1`, and contains a
`result` key. (tests/test_corpus_origin_integration.py:L300-L338)

### Skip when no readable files
When the project directory is empty (no readable samples), `cmd_init` must NOT write
`origin.json` and must complete without raising — writing a "cannot decide" result
would be misleading. (tests/test_corpus_origin_integration.py:L341-L365)

### Full-content reading (not front-sampling)
Tier 1 detection must read full file content, not sample only the first N characters.
A file whose first ~5400 chars are AI-signal-free narrative followed by heavy
AI-dialogue signal must still produce `result.likely_ai_dialogue == true`.
(tests/test_corpus_origin_integration.py:L368-L413)

### Pass ordering
`cmd_init` runs five passes in this exact order: (0) corpus-origin / `_run_pass_zero`,
(1) `discover_entities`, (2) `detect_rooms_local`, (3) gitignore protection
(`_ensure_mempalace_files_gitignored`), (4) `_maybe_run_mine_after_init`. Pass 0 must
produce `origin.json` before Pass 1 reads it; the mine prompt (Pass 4) must come
after gitignore protection. (tests/test_corpus_origin_integration.py:L1059-L1124)

### entities.json on-disk contract (default LLM-enabled path)
On the default (LLM-enabled) path, `cmd_init` writes `<project_dir>/entities.json`.
This file has a `people` list (array of name strings) and a `topics` key (present
even if empty). Persona names ("Echo", "Sparrow", "Cipher") must NOT appear in the
`people` list. The written `origin.json` `result.agent_persona_names` equals
`["Echo", "Sparrow", "Cipher"]` and `result.user_name` equals "Jordan" (sourced from
the Tier 2 LLM result). (tests/test_corpus_origin_integration.py:L806-L879;
tests/test_corpus_origin_integration.py:L1154-L1210)

### add_to_known_entities wing kwarg
When `cmd_init` calls `add_to_known_entities`, it must pass a `wing` keyword argument
whose value equals the project directory's base name.
(tests/test_corpus_origin_integration.py:L1213-L1258)

## cmd_init — LLM provider default and fallback

### LLM on by default
`cmd_init` with no `--no-llm` and no `--llm` must attempt LLM provider acquisition
(it calls `get_provider` exactly once). The legacy explicit `--llm` flag still
triggers acquisition and must not error. (tests/test_corpus_origin_integration.py:L648-L694;
tests/test_corpus_origin_integration.py:L778-L800)

### --no-llm opt-out
`cmd_init` with `no_llm=True` must NOT call `get_provider`; it runs in
heuristics-only mode. (tests/test_corpus_origin_integration.py:L697-L717)

### Graceful fallback — provider unavailable
When the acquired provider's `check_available()` returns `(False, message)`,
`cmd_init` must NOT raise/exit; it prints a one-line message that references
`--no-llm` (case-insensitive) and proceeds without an LLM.
(tests/test_corpus_origin_integration.py:L720-L748)

### Graceful fallback — provider construction error
When `get_provider` raises an `LLMError` (e.g. missing API key), `cmd_init` must
catch it, continue with heuristics, and print a one-line message referencing
`--no-llm`. (tests/test_corpus_origin_integration.py:L751-L775)

### --no-llm degradation classification
On the `--no-llm` path, `origin.json` is still written with
`result.likely_ai_dialogue == true`, but `result.agent_persona_names` is an empty
array (Tier 1 does not extract personas). Consequently at least one persona name
appears in `entities.json` `people` (v3.3.3-equivalent classification).
(tests/test_corpus_origin_integration.py:L882-L928)

### Idempotent re-init
Running `cmd_init` twice on the same project (with `--no-llm`) overwrites
`origin.json`; the `result` payload is identical between runs and `schema_version`
stays 1. (tests/test_corpus_origin_integration.py:L931-L961)

## cmd_mine — --redetect-origin flag

`cmd_mine` reads args including `redetect_origin` (boolean), plus `dir`, `palace`,
`mode`, `wing`, `no_gitignore`, `include_ignored`, `agent`, `limit`, `dry_run`,
`extract`. (tests/test_corpus_origin_integration.py:L511-L528)

Default `cmd_mine` with `redetect_origin=False` must NOT run corpus-origin detection
(`_run_pass_zero` is not called) and must NOT create `origin.json`.
(tests/test_corpus_origin_integration.py:L531-L548)

`cmd_mine` with `redetect_origin=True` re-runs detection and writes
`<palace>/.mempalace/origin.json` with `schema_version == 1` and
`result.likely_ai_dialogue == true` for the AI-dialogue fixture.
(tests/test_corpus_origin_integration.py:L551-L572)

When `origin.json` already exists, `--redetect-origin` overwrites it: a stale file
with `likely_ai_dialogue: false` and `detected_at: "2026-04-01T00:00:00Z"` becomes a
fresh result with `likely_ai_dialogue == true` and a different `detected_at`.
(tests/test_corpus_origin_integration.py:L575-L611)

`--redetect-origin` uses the same full-content reader as Pass 0: AI signal past
~5400 chars must still yield `likely_ai_dialogue == true`.
(tests/test_corpus_origin_integration.py:L614-L642)

## llm_refine — corpus-origin context in LLM prompt

`refine_entities(detected, corpus_text=..., provider=..., show_progress=False,
corpus_origin=<wrapper>)` invokes `provider.classify(system, user, json_mode=...)`.
When corpus_origin is supplied, the combined system+user prompt must contain the
`user_name` ("Jordan"), every persona name ("Echo", "Sparrow", "Cipher"), and the
platform marker ("Claude"). (tests/test_corpus_origin_integration.py:L419-L467)

When corpus_origin is omitted, the `system` prompt passed to `provider.classify` must
equal the unmodified module-level `SYSTEM_PROMPT` constant — no corpus-origin
preamble drift. (tests/test_corpus_origin_integration.py:L470-L505)

`TOPIC` must remain in the module's `VALID_LABELS`. When corpus_origin is supplied,
the system prompt must contain BOTH the `TOPIC` label instructions and a
`CORPUS CONTEXT` preamble (the preamble appends, it does not replace).
(tests/test_corpus_origin_integration.py:L1261-L1312)

## _run_pass_zero — Tier 1 / Tier 2 field merge

`_run_pass_zero(project_dir=..., palace_dir=..., llm_provider=...)` returns a wrapper
object (the `origin.json` shape) or null when no samples exist. When an LLM provider
is supplied, the Tier 2 LLM result (`detect_origin_llm`) is MERGED with the Tier 1
heuristic result field-by-field rather than replacing it:

- `likely_ai_dialogue` — KEEP the heuristic's value (do not let a weak LLM flip a
  confident regex answer). (tests/test_corpus_origin_integration.py:L1432-L1484)
- `confidence` — KEEP the heuristic's value; it equals
  `detect_origin_heuristic(samples).confidence` regardless of the LLM's confidence.
  (tests/test_corpus_origin_integration.py:L1485-L1497;
  tests/test_corpus_origin_integration.py:L1638-L1686)
- `primary_platform` — TAKE the LLM's value. (tests/test_corpus_origin_integration.py:L1505-L1505)
- `user_name` — TAKE the LLM's value. (tests/test_corpus_origin_integration.py:L1504-L1504)
- `agent_persona_names` — TAKE the LLM's value. (tests/test_corpus_origin_integration.py:L1499-L1503)
- `evidence` — COMBINE both tiers' evidence lines.
  (tests/test_corpus_origin_integration.py:L1607-L1618)

When both tiers agree NOT-AI-dialogue (heuristic false, LLM false with empty
personas), the merged `likely_ai_dialogue` is false, `agent_persona_names` is empty,
and `confidence` is the heuristic's narrative-branch value `0.9` (not the LLM's).
(tests/test_corpus_origin_integration.py:L1508-L1560)

### Evidence tier-prefix contract
Every evidence entry in the merged result must carry a tier prefix: heuristic lines
start with `"Tier-1 heuristic: "` and LLM lines start with `"Tier-2 LLM: "`. At least
one of each prefix must be present when both tiers ran, and no untagged entries may
exist. (tests/test_corpus_origin_integration.py:L1620-L1635)

### No LLM provider
When `_run_pass_zero` is called with `llm_provider=None`, the result is
heuristic-only: no merge fires, `likely_ai_dialogue` reflects the heuristic
(`true` for AI-dialogue samples), and `agent_persona_names` is empty, `user_name` is
null, `primary_platform` is null. (tests/test_corpus_origin_integration.py:L1689-L1718)

## External-API privacy warning

When `cmd_init` acquires a provider whose `is_external_service` is `true`, output
must contain the text `EXTERNAL API`, must reference `--no-llm`, and must include
wording clarifying MemPalace does not control the provider's downstream handling (one
of: "does not control", "not responsible", "logs", "retains").
(tests/test_corpus_origin_integration.py:L1733-L1775)

When the provider's `is_external_service` is `false` (local provider), the
`EXTERNAL API` warning must NOT be printed. With `--no-llm`, no provider is acquired
and the warning must not appear. (tests/test_corpus_origin_integration.py:L1778-L1830)

## Consent gate for stray env-fallback API keys

A consent gate triggers a blocking interactive prompt (a call to `input()`) only when
ALL of: the provider's `is_external_service` is `true`, the provider's
`api_key_source` is `"env"`, and `--accept-external-llm` is not set.
(tests/test_corpus_origin_integration.py:L1856-L1880)

At the consent prompt: input `"y"` proceeds with the LLM (`provider.classify` is
called); any non-`"y"` input (e.g. `"n"`) drops the LLM and falls back to heuristics
(`provider.classify` is NOT called). (tests/test_corpus_origin_integration.py:L1883-L1930)

The consent prompt must NOT fire when: `api_key_source == "flag"` (explicit
`--llm-api-key`, user already opted in); `--accept-external-llm` is set (CI bypass —
and the LLM still runs); or `is_external_service == false` (local endpoint, even with
`api_key_source == "env"`). (tests/test_corpus_origin_integration.py:L1933-L2022)

## Meta-test — no internal-coordination jargon

A test scans every `*.py` file under `mempalace/` and `tests/` and fails if any line
matches the regex `(Phase ?[12]|Igor's review|Igor's spec)` (case-insensitive) or the
section-marker regex `§ ?[0-9]`. The section-marker check is allow-listed for paths
beginning with `mempalace/sources/`, `mempalace/backends/`, `mempalace/knowledge_graph.py`,
`mempalace/i18n/`, `tests/test_sources.py`, `tests/test_i18n_lang_case.py`. The scanning
test file itself is excluded. Failure lists up to 20 offending `path:line` entries.
(tests/test_corpus_origin_integration.py:L1334-L1389)
