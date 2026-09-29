# Benchmarks

Curated summary of MemPalace's reproducible benchmark results. For the
complete progression with every experiment, see
[`benchmarks/BENCHMARKS.md`](https://github.com/MemPalace/mempalace/blob/main/benchmarks/BENCHMARKS.md).
All headline numbers on this page are reproducible from the committed
repository — datasets, scripts, and per-question result JSONLs are all
checked in.

## The Core Finding

MemPalace's benchmarked raw baseline stores the source text and searches
it with the vector store's default embeddings. No extraction or
summarisation step is required for that baseline, and it reproduces at
**96.6% R@5** on LongMemEval with no LLM at any stage.

## LongMemEval — Retrieval Recall

Retrieval recall asks: is the labelled session for this question inside
the top-K retrieved sessions? It is not the same metric as end-to-end QA
accuracy; a system can have perfect retrieval recall and poor QA answer
quality, and vice versa.

**Full 500 questions:**

| Mode | R@5 | LLM required | Cost/query |
|---|---|---|---|
| Raw — vector search over verbatim sessions | **96.6%** | None | $0 |
| Hybrid v4 — keyword/temporal/preference boosts, no LLM | 98.6% | None | $0 |
| Hybrid v4 + LLM rerank (minimax-m2.7 via Ollama) | 99.2% | Any capable model | $0 local / varies cloud |

**Held-out set (450 questions, never used during `hybrid_v4` development):**

| Mode | R@5 | R@10 | NDCG@10 |
|---|---|---|---|
| Hybrid v4 | **98.4%** | 99.8% | 0.938 |

The held-out figure is the honest generalisable number. The full-500
scores are higher but include the 50 "dev" questions that hybrid_v4's
three targeted fixes (quoted-phrase boost, person-name boost, nostalgia
patterns) were developed against. `benchmarks/BENCHMARKS.md` calls this
"teaching to the test" and the held-out 98.4% is the clean number to
quote when a single R@5 figure is needed for the hybrid pipeline.

### Per-category breakdown (raw, 96.6%)

| Question type | R@5 | Count |
|---|---|---|
| Knowledge update | 99.0% | 78 |
| Multi-session | 98.5% | 133 |
| Temporal reasoning | 96.2% | 133 |
| Single-session user | 95.7% | 70 |
| Single-session preference | 93.3% | 30 |
| Single-session assistant | 92.9% | 56 |

## LoCoMo — Retrieval Recall

LoCoMo contains 1,986 questions across 10 long conversations (19–32
sessions each).

| Mode | R@10 | LLM required |
|---|---|---|
| Session, no rerank, top-10 | 60.3% | None |
| Hybrid v5 (keyword + predicate boosts), top-10 | 88.9% | None |

We do not publish a "100% R@10" headline for LoCoMo. A reported 100% in
earlier drafts used `top_k=50`, which exceeds the per-conversation
session count (19–32) — so the retrieval stage returns every session in
every conversation by construction. That number measures an LLM's
reading comprehension over the whole conversation, not retrieval. The
honest retrieval-recall number for LoCoMo is the top-10 figure.

## Other Benchmarks

**ConvoMem** (Salesforce; 50 items per category × 5 categories = 250
items): MemPalace raw retrieval reaches **92.9% avg recall**. Strongest
categories: Assistant Facts 100%, User Facts 98%. Weakest: Preferences
86%. The Salesforce dataset contains ~75K items in total; our headline
number is from the 250-item sample the benchmark script was designed
around.

**MemBench** (ACL 2025; 8,500 items, all topics): MemPalace hybrid
top-5 reaches **80.3% R@5 overall**. Strongest: aggregative 99.3%,
comparative 98.4%, lowlevel_rec 99.8%. Weakest: noisy 43.4%
(distractor-heavy by design), conditional 57.3%.

## Why We Don't Publish a Cross-System Comparison Table

Previous versions of this page placed MemPalace's retrieval recall (R@5)
next to other projects' end-to-end QA accuracy figures under a single
"LongMemEval R@5" column. Those are different metrics and are not
comparable. A system can have 100% retrieval recall and 40% QA
accuracy, and vice versa.

If you are evaluating memory systems against MemPalace and want a fair
comparison, use the retrieval-recall numbers above and the benchmark
scripts in the repo; or pick the metric the other project publishes and
compare on that. Each project's published source is the correct
reference:

- [Mastra — Observational Memory](https://mastra.ai/research/observational-memory)
  (their published metric is binary QA accuracy with GPT-5-mini)
- [Mem0 — Research](https://mem0.ai/research)
  (their published LoCoMo metric is end-to-end QA accuracy, not retrieval recall)
- [Supermemory — ASMR post](https://supermemory.ai/blog/we-broke-the-frontier-in-agent-memory-introducing-99-sota-memory-system/)
  (their published metric is QA accuracy; authors explicitly frame the
  ensemble as an experimental proof-of-concept, not production)

## Reproducing Results

Every benchmark runs deterministically from this repository.

```bash
git clone https://github.com/MemPalace/mempalace.git
cd mempalace
uv sync --extra dev   # or: pip install -e ".[dev]"

# LongMemEval — raw (96.6%)
curl -fsSL -o /tmp/longmemeval_s_cleaned.json \
  https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
python benchmarks/longmemeval_bench.py /tmp/longmemeval_s_cleaned.json

# LongMemEval — hybrid v4 on the held-out 450 (98.4%)
python benchmarks/longmemeval_bench.py /tmp/longmemeval_s_cleaned.json \
  --mode hybrid_v4 --held-out --split-file benchmarks/lme_split_50_450.json

# LoCoMo — session, top-10 (60.3%)
git clone https://github.com/snap-research/locomo.git /tmp/locomo
python benchmarks/locomo_bench.py /tmp/locomo/data/locomo10.json \
  --granularity session --top-k 10

# LongMemEval — hybrid v4 + rerank, any OpenAI-compatible endpoint
python benchmarks/longmemeval_bench.py /tmp/longmemeval_s_cleaned.json \
  --mode hybrid_v4 --llm-rerank \
  --llm-backend ollama --llm-model <your-model-tag>
```

::: tip
Results are deterministic: same data, same script, same split seed →
same score. The committed `benchmarks/results_*.jsonl` files include
every question, every retrieved corpus id, and every score, so every
individual answer is auditable — not just the aggregate.
:::

For the complete progression (hybrid v1 → v4, diary mode, palace mode,
LoCoMo architecture iterations, methodology integrity notes), see
[`benchmarks/BENCHMARKS.md`](https://github.com/MemPalace/mempalace/blob/main/benchmarks/BENCHMARKS.md).
