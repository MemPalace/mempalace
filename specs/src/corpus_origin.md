# Spec: corpus_origin.py — Corpus Origin Detection

## Purpose

This module determines whether a corpus of text is a record of conversations with an AI agent and, if so, what platform it came from and what persona-names the user assigned to the agent(s). It exposes a two-tier detection scheme: a cheap heuristic (no external calls) and an LLM-assisted confirmation (mempalace/corpus_origin.py:L1-L33).

The default stance under thin evidence is "this IS an AI-dialogue corpus" because a false-negative is considered catastrophic for downstream classification while a false-positive is recoverable (mempalace/corpus_origin.py:L29-L33).

## Public Surface

### Result Type: `CorpusOriginResult`

A structured record with the following fields and types (mempalace/corpus_origin.py:L138-L160):

- `likely_ai_dialogue`: boolean — best hypothesis on whether this is AI-dialogue (mempalace/corpus_origin.py:L152).
- `confidence`: floating-point number in range 0.0 to 1.0 (mempalace/corpus_origin.py:L153).
- `primary_platform`: string or null, e.g. `"Claude Code (Anthropic CLI)"` (mempalace/corpus_origin.py:L154).
- `user_name`: string or null — the corpus author's name if identifiable, else null; defaults to null (mempalace/corpus_origin.py:L155).
- `agent_persona_names`: list of strings — names the user assigned to AI agent(s); MUST NOT include the user's own name; defaults to empty list (mempalace/corpus_origin.py:L156).
- `evidence`: list of human-readable reason strings; defaults to empty list (mempalace/corpus_origin.py:L157).

The result exposes a `to_dict()` conversion producing a plain key/value mapping of all fields (mempalace/corpus_origin.py:L159-L160).

### Function: `detect_origin_heuristic(samples) -> CorpusOriginResult`

Tier 1. Takes a list of text samples, performs grep-based detection with no external calls, and returns a `CorpusOriginResult` (mempalace/corpus_origin.py:L166-L174).

### Function: `detect_origin_llm(samples, provider) -> CorpusOriginResult`

Tier 2. Takes a list of text samples and an LLM provider object, returning the same result shape; falls back conservatively on any error and never raises (mempalace/corpus_origin.py:L374-L381).

## Term Vocabulary (Detection Inputs)

Detection uses three fixed term/pattern lists. All matching is case-insensitive (mempalace/corpus_origin.py:L58, L178-L179).

**Unambiguous AI terms** (always count toward AI evidence): includes brand/model identifiers such as `Anthropic`, `Claude Code`, `Claude 3`, `Claude 4`, `claude mcp`, `CLAUDE.md`, `.claude/`, `ChatGPT`, `GPT-4`, `GPT-3`, `GPT-5`, `OpenAI`, `gpt-4o`, `gpt-4-turbo`, `o1-preview`, `o3`, `gemini-pro`, `gemini-1.5`, `Google AI`, `Mixtral`, `Cohere`, and infrastructure terms `MCP`, `LLM`, `RAG`, `fine-tune`, `context window`, `embedding` (mempalace/corpus_origin.py:L60-L93).

**Ambiguous AI terms** (count only when an unambiguous co-signal is present): `Claude`, `Opus`, `Sonnet`, `Haiku`, `Gemini`, `Bard`, `Llama`, `Mistral` (mempalace/corpus_origin.py:L95-L111).

**Turn-marker patterns**: regex patterns for `user:`, `assistant:`, `human:`, `ai:`, `>>> User`, `>>> Assistant` (mempalace/corpus_origin.py:L114-L121).

### Brand-term Matching Rule

Each term is matched with a word boundary attached only on edges where the term itself starts or ends with an alphanumeric or underscore character. Terms beginning/ending with a non-word char (e.g. `.claude/`) do not get a boundary on that edge, so `.claude/` matches at the start of a string and `Claude` does not match inside `Claudette` (mempalace/corpus_origin.py:L124-L135).

## Tier 1 Behavior: `detect_origin_heuristic`

Samples are joined into one combined text using a blank-line separator; total character length is computed with a floor of 1 to avoid division by zero (mempalace/corpus_origin.py:L175-L176).

The function counts, case-insensitively: unambiguous term hits (per-term counts and total), ambiguous term hits (per-term counts and total), and turn-marker hits (total count plus the set of distinct pattern types matched) (mempalace/corpus_origin.py:L181-L207).

### Co-occurrence Rule

An "AI context" is present when there is at least one unambiguous hit OR at least one turn-marker hit. Ambiguous hits are counted toward brand evidence only when AI context is present; otherwise they are excluded from the counted total (mempalace/corpus_origin.py:L215-L216).

### Density and Thresholds

Brand density and turn density are each computed as counted hits divided by (total characters / 1000) (mempalace/corpus_origin.py:L221-L222).

### Evidence Construction

Evidence strings are built in this order (mempalace/corpus_origin.py:L224-L243):
- If any shown hits exist (unambiguous always shown; ambiguous shown only when AI context present), emit a line `AI brand terms: ...` listing the top 5 terms by descending count (mempalace/corpus_origin.py:L226-L231).
- Else if ambiguous hits exist but no AI context, emit a line stating ambiguous terms were present but suppressed, listing the top 3 by descending count (mempalace/corpus_origin.py:L232-L239).
- If any turn markers were found, append a line reporting total occurrences and the number of distinct pattern types (mempalace/corpus_origin.py:L240-L243).

### Decision Logic

The classification uses a meaningful-text floor of 150 characters (mempalace/corpus_origin.py:L254).

1. **Confident AI-dialogue**: if brand density >= 0.5 OR turn density >= 2.0, return `likely_ai_dialogue=true`, `confidence = min(0.95, 0.6 + 0.1 * (brand_density + turn_density))`, `primary_platform=null`, with the built evidence (mempalace/corpus_origin.py:L256-L262).

2. **Confident narrative**: if counted brand hits == 0 AND turn hits == 0 AND total chars >= 150, return `likely_ai_dialogue=false`, `confidence=0.9`, `primary_platform=null`, with the built evidence plus a line noting no unambiguous AI signal across the character count (pure narrative). Ambiguous-only corpora reach this branch since their counted total is zero without a co-signal (mempalace/corpus_origin.py:L263-L276).

3. **Default stance (weak/insufficient)**: otherwise return `likely_ai_dialogue=true`, `confidence=0.4`, `primary_platform=null`, with evidence appended by a reason of `"weak signal"` (if any counted brand or turn hits) or `"insufficient text"` (otherwise), recommending a Tier 2 LLM check (mempalace/corpus_origin.py:L277-L289).

## Tier 2 Behavior: `detect_origin_llm`

### Prompt Construction

A user prompt is built from at most the first 20 samples; each sample is truncated to its first 800 characters and labeled `[sample N]` (1-based index), joined by a `---` separator line. The combined prompt is prefixed with `CORPUS EXCERPTS:` and suffixed with an instruction to respond with JSON (mempalace/corpus_origin.py:L383-L387).

The provider is invoked via `provider.classify(system=..., user=..., json_mode=True)` with a fixed system prompt. The system prompt instructs the model to use its pretrained knowledge of AI platforms, to place user-assigned agent names in `agent_persona_names` while excluding the user's own name, to place the user's name in `user_name` (or null), and to respond with JSON only matching a fixed schema. It also restates the default stance (return ai-dialogue=true with low confidence on thin/mixed evidence) (mempalace/corpus_origin.py:L295-L329, L390).

### Response JSON Schema (Provider Contract)

The expected JSON object has fields: `is_ai_dialogue_corpus` (boolean), `confidence` (0.0-1.0), `primary_platform` (platform name or null), `user_name` (name or null), `agent_persona_names` (array of agent names, not the user's name), `evidence` (array of short strings) (mempalace/corpus_origin.py:L316-L324).

### JSON Extraction

The raw response text is parsed by first attempting a direct JSON parse; on failure it locates the first `{` and scans forward tracking brace depth and string/escape state to extract the first balanced `{...}` block, then parses that. Empty text, absence of `{`, or parse failure on the candidate yields no object (null) (mempalace/corpus_origin.py:L332-L371).

### Result Mapping and Fallbacks

- On any exception during the provider call, return `likely_ai_dialogue=true`, `confidence=0.3`, `primary_platform=null`, and an evidence entry describing the LLM provider error; never raises (mempalace/corpus_origin.py:L389-L398).
- If extraction yields no dictionary object, return `likely_ai_dialogue=true`, `confidence=0.3`, `primary_platform=null`, with evidence noting the response was not valid JSON (mempalace/corpus_origin.py:L400-L407).
- On success, fields are read defensively with defaults: `likely_ai_dialogue` defaults true, `confidence` defaults 0.5, `primary_platform` null if missing/empty, `user_name` null if missing/empty, `agent_persona_names` empty list if missing, `evidence` empty list if missing (mempalace/corpus_origin.py:L411-L422).
- If a `user_name` is present, any persona name equal to it (case-insensitive) is removed from `agent_persona_names`, enforcing the invariant that the user's name is never an agent persona (mempalace/corpus_origin.py:L411-L414).

## Side Effects

Tier 1 has no external side effects (pure computation over inputs) (mempalace/corpus_origin.py:L166-L289). Tier 2's only external interaction is the single call to the supplied provider's `classify` method; there is no filesystem, network, env, or process access performed directly by this module (mempalace/corpus_origin.py:L389-L390).
