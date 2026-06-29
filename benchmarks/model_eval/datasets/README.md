# MemPalace Small-Model Benchmark Datasets

Synthetic, hand-curated evaluation datasets for testing small (≤4B parameter) Ollama models on the four classification and extraction tasks that matter for MemPalace's local-first memory pipeline.

- **Synthetic only.** No real-person names, no real organizations, no real PII.
- **Public-safe.** Intended to be committed to the public MemPalace open-source repository.
- **Generated 2026-05-10.**
- **211 samples** total across four tasks (100 synthetic + 1 real-format-flavored sample in room_classification).

## Tasks

| Task | Samples | Purpose |
|---|---|---|
| `room_classification/` | 101 | Given a session summary and a room list, pick the right room (or `"other"`). Tests the closed-set room-routing path used at filing time. 100 synthetic + 1 real-format-flavored (rc_101). |
| `entity_extraction/` | 50 | Given a conversation excerpt, extract typed entities (person / project / place / organization). Tests the entity detector that feeds the knowledge graph. |
| `memory_extraction/` | 40 | Given a snippet, extract memory-worthy items (decision / preference / fact / opinion / commitment). Tests the layer-promotion heuristic. |
| `calibration/` | 20 | Sentence-type classification (question / command / statement / exclamation / greeting). A trivially-easy baseline used to detect a broken model load before more meaningful evals run. |

## The five fictional personae

The 100 synthetic room-classification samples are split 20-per-agent across these synthetic agents (rc_101 is a separate real-format-flavored sample assigned to Solas). Each agent has a fixed room taxonomy; per-sample room lists are drawn as 5-10 room subsets that always include `general`.

### 1. Aria — research assistant (she/her)

Synthesizes academic papers.

Rooms: `about`, `projects/meta-cognition`, `projects/embedding-spaces`, `projects/topic-clustering`, `skills/latex`, `skills/python`, `skills/statistical-tests`, `daily-logs`, `general`

### 2. Solas — coding agent (they/them)

Writes and reviews code; focuses on systems work.

Rooms: `about`, `projects/distributed-tracing`, `projects/parser-combinators`, `projects/type-systems`, `skills/rust`, `skills/ocaml`, `skills/llvm`, `daily-logs`, `general`

### 3. Fenra — creative writing agent (she/her)

Drafts fiction and worldbuilds.

Rooms: `about`, `projects/world-building`, `projects/character-arcs`, `projects/dialogue-engine`, `skills/narrative-design`, `skills/etymology`, `skills/mythology`, `daily-logs`, `general`

### 4. Bramble — gardening agent (he/him)

Companion-plant and ecological-gardening advice.

Rooms: `about`, `projects/pollinator-paths`, `projects/soil-microbes`, `projects/native-species`, `skills/phenology`, `skills/plant-pathology`, `skills/propagation`, `daily-logs`, `general`

### 5. Thresh — finance/accounting agent (they/them)

Small-business bookkeeping.

Rooms: `about`, `projects/invoice-parsing`, `projects/tax-prep`, `projects/cashflow-models`, `skills/double-entry`, `skills/depreciation-rules`, `skills/ifrs`, `daily-logs`, `general`

## Distribution stats

### `room_classification/` (101 samples)

| Metric | Value |
|---|---|
| Samples per agent | Aria 20, Solas 20, Fenra 20, Bramble 20, Thresh 20 |
| Messy samples (`include_messy_features: true`) | 14 (target: ~15%) |
| Closed-set `"other"` | 18 (target: ~20%) |
| Closed-set `"general"` (escape hatch used as best fit) | 6 |
| Closed-set match to a non-`general` non-`other` room | 76 |

`"other"` distribution by agent: Aria 2, Solas 3, Fenra 3, Bramble 7, Thresh 3. Bramble skews higher because gardening conversations frequently span topics that don't cleanly fit a single skill room (e.g. seed-saving + plant pathology, soil chemistry, vegetable-pest control).

### `entity_extraction/` (50 samples)

| Metric | Value |
|---|---|
| Total entities | 247 |
| `person` | 114 |
| `organization` | 74 |
| `project` | 32 |
| `place` | 27 |
| Entities per sample (min / max / avg) | 3 / 9 / 4.9 |

Person-skew is intentional: real MemPalace sessions are heavily person-centric, and the entity detector's hardest job is disambiguating people (the dataset deliberately reuses some last names like "Halloran" across distinct individuals to test this).

### `memory_extraction/` (40 samples)

| Metric | Value |
|---|---|
| Total memories | 55 |
| `decision` | 12 |
| `preference` | 9 |
| `commitment` | 12 |
| `fact` | 15 |
| `opinion` | 7 |
| Memories per sample | 1 (25 samples) or 2 (15 samples) |

### `calibration/` (20 samples)

Exactly 4 samples per class: `question`, `command`, `statement`, `exclamation`, `greeting`. Designed to be unambiguous — any working model should hit ≥95% accuracy here. If a model fails calibration, it is broken or misconfigured and the more interesting tasks should not be run.

## Schema reference

### `room_classification/dataset.jsonl`

```json
{"id": "rc_001", "agent": "Aria", "session_summary": "...", "include_messy_features": false}
```

### `room_classification/labels.jsonl`

```json
{"id": "rc_001", "closed_set_label": "projects/meta-cognition", "preferred_open_label": "meta-cognition-research"}
```

`closed_set_label` is either a room from that sample's `rooms` list, or exactly `"other"`.
`preferred_open_label` is the slug a human annotator would invent if labeling freely (lowercase, hyphenated).

### `room_classification/room_lists.jsonl`

```json
{"id": "rc_001", "rooms": ["about", "projects/meta-cognition", "skills/python", "daily-logs", "general"]}
```

Always includes `"general"`. Always 5-10 rooms. Composition varies per sample so the model can't just memorize an agent's full taxonomy.

### `entity_extraction/dataset.jsonl` + `labels.jsonl`

```json
{"id": "ent_001", "text": "..."}
{"id": "ent_001", "entities": [{"name": "Aria", "type": "person"}, {"name": "Embedding Spaces", "type": "project"}]}
```

Entity types: `person`, `project`, `place`, `organization`.

### `memory_extraction/dataset.jsonl` + `labels.jsonl`

```json
{"id": "mem_001", "text": "..."}
{"id": "mem_001", "memories": [{"type": "decision", "content": "..."}]}
```

Memory types: `decision`, `preference`, `fact`, `opinion`, `commitment`.

### `calibration/dataset.jsonl` + `labels.jsonl`

```json
{"id": "cal_001", "text": "Could you fix the indentation on this Python file?", "classes": ["question", "command", "statement", "exclamation", "greeting"]}
{"id": "cal_001", "label": "command"}
```

The `classes` field is identical across all 20 samples, included in each line for self-contained downstream loading.

## Annotation conventions

- **Decision vs. commitment.** A *decision* changes how something will be done from now on (a policy or design choice); a *commitment* binds the speaker to a specific deliverable, often with a deadline. "Switching to Jaccard similarity" is a decision; "I'll deliver the draft Friday" is a commitment. When both apply to one sentence, both are emitted.
- **Fact vs. opinion.** A *fact* is a claim about the world that is checkable in principle. An *opinion* is signaled by hedge words ("I think", "honestly", "my read") or by being a value judgement. Borderline cases (e.g. an arguable empirical claim about a paper) lean to opinion when the speaker frames it as their own assessment.
- **Preference.** Preferences attach to a person and describe how that person likes things done. They are durable across conversations, unlike one-off decisions.
- **Closed-set "other".** Used when no listed room is a substantively better home for the content than `general`. If `general` is a clean fit (catch-all small talk, identity questions), the label is `general` rather than `other`.
- **Ambiguous closed-set samples** (~10% of room_classification) have two rooms that fit; the label picks the one that better matches the dominant content of the session. `preferred_open_label` may name the secondary topic when the closed-set choice is forced.

## Extending

To regenerate or extend:

1. **Add new personae.** Define a name, pronouns, role, and 7-9 rooms (always include `about`, `daily-logs`, `general`). Add 20 samples covering each room at least once, plus a few drift / messy / `"other"` cases.
2. **Add more samples.** Keep the per-agent balance even. Maintain ~70/20/10 distribution for closed-set / `"other"` / ambiguous.
3. **Add new tasks.** Pick a discriminative task that small models would plausibly fail in interesting ways (relation extraction, coreference, stance, etc.). Match the pattern of separate `dataset.jsonl` and `labels.jsonl` files keyed by `id`.

## Provenance

- Synthetic, generated 2026-05-10 for the MemPalace project.
- No real-person names. Fictional personae and place/organization names invented for this benchmark.
- Safe to commit to a public repository.
