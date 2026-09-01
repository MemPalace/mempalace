# Distilled Knowledge Benchmark

Tests retrieval of **distilled operational knowledge** — engineering constraints,
architectural decisions, and system invariants captured as prose documents.

## What this benchmark tests

All existing MemPal benchmarks (LongMemEval, LoCoMo, MemBench, ConvoMem) test
**episodic memory**: *"what did the user say in session 3?"*

This benchmark tests **semantic memory**: *"what is the correct behaviour for X?"*
— the kind of knowledge engineers distill into runbooks, architecture decision
records, and internal wikis.

Real production systems accumulate hundreds of these constraints. When a new agent
or engineer asks "why do we do it this way?", the answer must be retrievable
from prose, not from code comments or chat history.

## Corpus

30 documents covering cross-cutting engineering constraints:

| Topic area | Example entries |
|---|---|
| Concurrency / locking | Cross-process file lock vs asyncio.Lock; GPU serialization |
| Database gotchas | UUID driver behaviour; partitioned index limits |
| Security gates | Package validation pipeline; audit field requirements |
| Process isolation | Sandbox detonation protocol; privilege token mechanism |
| Lifecycle management | Work-item states; deliverable verification |
| Error handling | Empty TimeoutError string; boolean env var pitfall |

Documents are written as plain prose — no headers, no bullet points, no code
blocks — to test semantic retrieval rather than structural pattern matching.

## QA pairs

30 paraphrased queries. The question wording deliberately avoids the exact
vocabulary in the target document. For example:

| Question | Ground-truth fragment | Notes |
|---|---|---|
| "how do we stop two AI models from loading at the same time?" | `fcntl.flock` | 'stop' vs 'prevent', 'loading' vs 'simultaneously' |
| "what crash threshold automatically kills a work item mid-run?" | `hallucination_count` | 'crash threshold' vs 'block threshold' |
| "what prevents a confidential client entry from being read by the wrong agent?" | `authorized_ring` | 'wrong agent' vs 'restricted agents' |

A hit is scored if the ground-truth fragment appears (case-insensitive substring
match) anywhere in the top-k retrieved documents.

## Results on MemPal raw ChromaDB baseline

Measured 2026-04-22 with MemPal v3.3.x, ChromaDB ephemeral client, default
`all-MiniLM-L6-v2` embeddings:

| Metric | Score | Count |
|---|---|---|
| Recall@1 | **76.7%** | 23/30 |
| Recall@5 | **100%** | 30/30 |

Recall@5 is perfect — every correct document appears in the top-5. The 23pp gap
to Recall@1 indicates the correct document is consistently retrieved but not
always ranked first. Reranking or hybrid retrieval would likely push Recall@1
into the 90s — consistent with the pattern seen on LongMemEval and LoCoMo.

## Usage

```bash
# From repo root — no external data download required
python benchmarks/distilled_knowledge_bench.py
python benchmarks/distilled_knowledge_bench.py --top-k 1
python benchmarks/distilled_knowledge_bench.py --verbose
```

No API keys, no external datasets, no model downloads. Runs entirely offline
with ChromaDB's default sentence-transformers embeddings.

## Methodology notes

**Corpus construction:** documents were written to represent the kind of
constraint that is important enough to document but easy to forget — the "why"
behind a design decision, not the decision itself. Each document is 3–5
sentences.

**QA construction:** for each document, one query was written that a developer
would realistically ask. The query was then paraphrased to avoid lexical overlap
with the target document's distinctive terms. A `paraphrase_note` field in the
code records the specific vocabulary shift for reproducibility.

**Scoring:** substring fragment match is intentionally strict — it requires the
retrieved document to contain a specific technical term or phrase from the
ground truth. Fuzzy scoring was rejected because it cannot distinguish "retrieved
the right document" from "retrieved a related document."

**What this benchmark does not test:** multi-hop reasoning, temporal ordering,
or conversational context. It is a point-in-time retrieval test on static
documents.

## Relation to existing benchmarks

| Benchmark | Memory type | Data source | Query style |
|---|---|---|---|
| LongMemEval | Episodic | Synthetic conversations | "What did X say about Y?" |
| LoCoMo | Episodic | Real multi-session conversations | "When did X happen?" |
| MemBench | Episodic | Multi-category conversational QA | "What is my preference for X?" |
| ConvoMem | Episodic | Conversational fact recall | "What is X's Y?" |
| **This benchmark** | **Semantic** | **Prose constraint documents** | **"How does X work?"** |

The distilled-knowledge benchmark is complementary, not critical, of the existing
suite. It fills a gap — episodic recall is well-covered; semantic/procedural
recall is not tested at all.
