# MemPalace Benchmarks — Reproduction Guide

Run the exact same benchmarks we report. Clone, install, run.

## Setup

```bash
git clone https://github.com/MemPalace/mempalace.git
cd mempalace
uv sync --extra dev   # or: pip install -e ".[dev]"
```

## Benchmark 1: LongMemEval (500 questions)

Tests retrieval across ~53 conversation sessions per question. The standard benchmark for AI memory.

```bash
# Download data
mkdir -p /tmp/longmemeval-data
curl -fsSL -o /tmp/longmemeval-data/longmemeval_s_cleaned.json \
  https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json

# Run (raw mode — our headline 96.6% result)
python benchmarks/longmemeval_bench.py /tmp/longmemeval-data/longmemeval_s_cleaned.json

# Run with AAAK compression (84.2%)
python benchmarks/longmemeval_bench.py /tmp/longmemeval-data/longmemeval_s_cleaned.json --mode aaak

# Run with room-based boosting (89.4%)
python benchmarks/longmemeval_bench.py /tmp/longmemeval-data/longmemeval_s_cleaned.json --mode rooms

# Quick test on 20 questions first
python benchmarks/longmemeval_bench.py /tmp/longmemeval-data/longmemeval_s_cleaned.json --limit 20

# Turn-level granularity
python benchmarks/longmemeval_bench.py /tmp/longmemeval-data/longmemeval_s_cleaned.json --granularity turn
```

**Expected output (raw mode, full 500):**
```
Recall@5:  0.966
Recall@10: 0.982
NDCG@10:   0.889
Time:      ~5 minutes on Apple Silicon
```

## Benchmark 2: LoCoMo (1,986 QA pairs)

Tests multi-hop reasoning across 10 long conversations (19-32 sessions each, 400-600 dialog turns).

```bash
# Clone LoCoMo
git clone https://github.com/snap-research/locomo.git /tmp/locomo

# Run (session granularity — our 60.3% result)
python benchmarks/locomo_bench.py /tmp/locomo/data/locomo10.json --granularity session

# Dialog granularity (harder — 48.0%)
python benchmarks/locomo_bench.py /tmp/locomo/data/locomo10.json --granularity dialog

# Higher top-k (77.8% at top-50)
python benchmarks/locomo_bench.py /tmp/locomo/data/locomo10.json --top-k 50

# Quick test on 1 conversation
python benchmarks/locomo_bench.py /tmp/locomo/data/locomo10.json --limit 1
```

**Expected output (session, top-10, full 10 conversations):**
```
Avg Recall: 0.603
Temporal:   0.692
Time:       ~2 minutes
```

## Benchmark 3: ConvoMem (Salesforce, 75K+ QA pairs)

Tests six categories of conversational memory. Downloads from HuggingFace automatically.

```bash
# Run all categories, 50 items each (our 92.9% result)
python benchmarks/convomem_bench.py --category all --limit 50

# Single category
python benchmarks/convomem_bench.py --category user_evidence --limit 100

# Quick test
python benchmarks/convomem_bench.py --category user_evidence --limit 10
```

**Categories available:** `user_evidence`, `assistant_facts_evidence`, `changing_evidence`, `abstention_evidence`, `preference_evidence`, `implicit_connection_evidence`

**Expected output (all categories, 50 each):**
```
Avg Recall: 0.929
Assistant Facts: 1.000
User Facts:      0.980
Time:            ~2 minutes
```

## What Each Benchmark Tests

| Benchmark | What it measures | Why it matters |
|---|---|---|
| **LongMemEval** | Can you find a fact buried in 53 sessions? | Tests basic retrieval quality — the "needle in a haystack" |
| **LoCoMo** | Can you connect facts across conversations over weeks? | Tests multi-hop reasoning and temporal understanding |
| **ConvoMem** | Does your memory system work at scale? | Tests all memory types: facts, preferences, changes, abstention |

## Results Files

Raw results are in `benchmarks/results_*.jsonl` and `benchmarks/results_*.json`. Each file contains every question, every retrieved document, and every score — fully auditable.

## Requirements

- Python 3.9+
- `chromadb` (the only dependency)
- ~300MB disk for LongMemEval data
- ~5 minutes for each full benchmark run
- No API key. No internet during benchmark (after data download). No GPU.

## Next Benchmarks (Planned)

- **Scale testing** — ConvoMem at 50/100/300 conversations per item
- **Hybrid AAAK** — search raw text, deliver AAAK-compressed results
- **End-to-end QA** — retrieve + generate answer + measure F1 (needs LLM API key)
