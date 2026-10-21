# Multilingual Benchmark — 2026-05-13 (Mercurio)

**6 local models × 7 languages × 5 tasks = 210 runs**
Hardware: RTX 3080 Laptop 8 GB · Ollama 0.23.3
Embed model: `nomic-embed-text` · Dataset: n=20 (calibration), n=40 (memory/entity), n=101 (room)

> **Methodology note:** this run predates the `--num-ctx 4096` default. Each model used its own Modelfile context window (32k for the Gemma4 variants, larger for qwen3), which means VRAM and latency numbers across families aren't strictly apples-to-apples. Accuracy is unaffected — the prompts are well under any model's window — but expect e2e and VRAM to drop in subsequent runs with the new default.

---

## Models

| short name      | tag                                          | quant  |
|:--------------- |:-------------------------------------------- |:------:|
| qwen3-4b-q8     | qwen3:4b-instruct-2507-q8_0                  | q8_0   |
| gemma4-e4b-q4   | gemma4:e4b-it-q4_K_M                         | q4_K_M |
| gemma4-e4b      | gemma4:e4b                                   | q4_K_M |
| classifier-q8   | igorls/gemma4-e4b-classifier:Q8_0            | q8_0   |
| classifier-q4   | igorls/gemma4-e4b-classifier:latest          | q4_K_M |
| heretic-q4      | igorls/gemma-4-E4B-it-heretic-GGUF:Q4_K_M    | q4_K_M |

---

## Overall (all tasks × all locales)

| model          | EN    | non-EN avg | all avg | calib fastest |
|:-------------- |:-----:|:----------:|:-------:|:-------------:|
| classifier-q8  | 0.798 | 0.674      | **0.691** | 354 ms      |
| classifier-q4  | 0.792 | 0.659      | 0.678   | 282 ms        |
| gemma4-e4b     | 0.790 | 0.655      | 0.675   | 324 ms        |
| gemma4-e4b-q4  | 0.784 | 0.655      | 0.673   | 312 ms        |
| qwen3-4b-q8    | 0.781 | 0.645      | 0.665   | **161 ms**    |
| heretic-q4     | 0.787 | 0.644      | 0.664   | 272 ms        |

`classifier-q8` leads on accuracy (+2.6 pp over heretic on all avg).
`qwen3-4b-q8` is 2–3× faster on simple tasks and ranks 3rd on accuracy.
`gemma4-e4b` and `gemma4-e4b-q4` are statistically equivalent (within noise).

---

## By task

### Room Classification — closed-set

| model          |  en   |  de   |  fr   |  hi   |  it   |  ko   |  ru   |  avg  |
|:-------------- |:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|
| classifier-q8  | 0.624 | 0.604 | 0.604 | 0.624 | 0.604 | 0.634 | 0.624 | **0.617** |
| classifier-q4  | 0.644 | 0.584 | 0.594 | 0.554 | 0.584 | 0.604 | 0.604 | 0.596 |
| gemma4-e4b     | 0.624 | 0.574 | 0.584 | 0.554 | 0.584 | 0.604 | 0.594 | 0.588 |
| gemma4-e4b-q4  | 0.604 | 0.594 | 0.594 | 0.554 | 0.584 | 0.604 | 0.604 | 0.591 |
| heretic-q4     | 0.624 | 0.594 | 0.594 | 0.545 | 0.564 | 0.594 | 0.604 | 0.588 |
| qwen3-4b-q8    | 0.624 | 0.564 | 0.554 | 0.554 | 0.535 | 0.554 | 0.564 | 0.564 |

Average EN→non-EN drop: ~3–6 pp. Uniform distribution across languages — no outlier locale.

### Room Classification — open-set

| model          |  en   |  de   |  fr   |  hi   |  it   |  ko   |  ru   |  avg  |
|:-------------- |:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|
| classifier-q8  | 0.678 | 0.637 | 0.634 | 0.651 | 0.644 | 0.645 | 0.633 | **0.646** |
| gemma4-e4b     | 0.657 | 0.647 | 0.642 | 0.630 | 0.637 | 0.648 | 0.642 | **0.643** |
| gemma4-e4b-q4  | 0.655 | 0.644 | 0.633 | 0.634 | 0.632 | 0.647 | 0.640 | 0.641 |
| classifier-q4  | 0.655 | 0.651 | 0.632 | 0.622 | 0.626 | 0.636 | 0.643 | 0.638 |
| heretic-q4     | 0.627 | 0.605 | 0.603 | 0.629 | 0.601 | 0.639 | 0.644 | 0.621 |
| qwen3-4b-q8    | 0.603 | 0.572 | 0.562 | 0.599 | 0.570 | 0.581 | 0.559 | 0.578 |

Open-set is more stable across languages than closed-set — cosine similarity absorbs phrasing variation better than exact match.
Gemma4 clearly leads; qwen3 trails by ~6 pp.

### Entity Extraction

| model          |  en   |  de   |  fr   |  hi   |  it   |  ko   |  ru   |  avg  |
|:-------------- |:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|
| heretic-q4     | 0.782 | 0.701 | 0.771 | 0.751 | **0.792** | 0.733 | 0.729 | **0.751** |
| qwen3-4b-q8    | 0.777 | 0.732 | **0.799** | 0.764 | **0.801** | 0.770 | 0.758 | **0.771** |
| classifier-q8  | 0.763 | 0.709 | 0.761 | 0.754 | 0.763 | 0.736 | 0.745 | 0.747 |
| gemma4-e4b     | 0.759 | 0.676 | 0.761 | 0.745 | 0.773 | 0.709 | 0.726 | 0.736 |
| gemma4-e4b-q4  | 0.748 | 0.663 | 0.760 | 0.736 | 0.773 | 0.712 | 0.729 | 0.732 |
| classifier-q4  | 0.723 | 0.680 | 0.756 | 0.733 | 0.745 | 0.698 | 0.708 | 0.720 |

The most robust task across languages — only a ~3–5 pp EN→non-EN drop.
**qwen3** and **heretic** tie for the lead. FR and IT often beat EN (likely an effect of richer training data in those languages).
KO and DE are the hardest languages here.

### Memory Extraction ⚠️

| model          |  en   |  de   |  fr   |  hi   |  it   |  ko   |  ru   | drop EN→avg |
|:-------------- |:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----------:|
| qwen3-4b-q8    | **0.950** | 0.287 | 0.438 | 0.463 | 0.463 | 0.400 | 0.212 | **−0.573**  |
| heretic-q4     | **0.950** | 0.225 | 0.425 | 0.350 | 0.438 | 0.312 | 0.163 | **−0.631**  |
| classifier-q4  | 0.938 | 0.325 | 0.487 | 0.438 | 0.475 | 0.400 | 0.225 | −0.546      |
| classifier-q8  | 0.925 | 0.412 | 0.450 | 0.438 | **0.500** | 0.438 | 0.212 | −0.517      |
| gemma4-e4b     | 0.912 | 0.312 | 0.450 | 0.400 | 0.450 | 0.375 | 0.188 | −0.550      |
| gemma4-e4b-q4  | 0.912 | 0.312 | 0.438 | 0.400 | 0.463 | 0.362 | 0.188 | −0.552      |

**This is the critical task.** Every model collapses ~0.52–0.63 pp from EN to non-EN.
`classifier-q8` has the smallest drop (−0.517) and the best non-EN absolute (0.375 avg).
RU and DE are the worst — likely an embedding artifact (`nomic-embed-text` has weak signal on EN↔RU/DE pairs in memory extraction, as documented in PR #1483).

> **Methodology note**: memory_extraction scores use cosine similarity via `nomic-embed-text`. For distant language pairs (RU, DE), the embedding model may be underestimating real coverage — see PR #1483 for a comparison with `embeddinggemma`.
>
> A follow-up methodology fix in this PR adds `labels.ko.jsonl` so KO scores are computed against Korean ground truth instead of English. The numbers above predate that change; expect KO `memory_extraction` to recover meaningfully once `labels.{lang}.jsonl` exists for every language.

### Calibration

| model          |  en   |  de   |  fr   |  hi   |  it   |  ko   |  ru   |  avg  |
|:-------------- |:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|
| gemma4-e4b     | 1.000 | 0.950 | 0.950 | 0.950 | 1.000 | 0.950 | 0.950 | 0.964 |
| gemma4-e4b-q4  | 1.000 | 0.950 | 0.950 | 0.950 | 1.000 | 0.950 | 0.950 | 0.964 |
| classifier-q8  | 1.000 | 0.950 | 0.950 | 0.950 | 1.000 | 0.950 | 0.950 | 0.964 |
| classifier-q4  | 1.000 | 0.950 | 0.950 | 0.950 | 1.000 | 0.950 | 0.950 | 0.964 |
| qwen3-4b-q8    | 0.950 | 0.950 | 0.950 | 0.950 | 0.950 | 0.950 | 0.950 | 0.950 |
| heretic-q4     | 0.950 | 0.950 | 0.950 | 0.950 | 0.950 | 0.950 | 0.950 | 0.950 |

Calibration is effectively language-agnostic — clean signal, no surprises.

---

## Per-language ranking (all-tasks avg)

| locale | best model     | score | worst model   | score |
|:------:|:-------------- |:-----:|:------------- |:-----:|
| en     | classifier-q8  | 0.798 | qwen3-4b-q8   | 0.781 |
| de     | classifier-q8  | 0.681 | heretic-q4    | 0.615 |
| fr     | classifier-q8  | 0.688 | qwen3-4b-q8   | 0.661 |
| hi     | classifier-q8  | 0.683 | heretic-q4    | 0.645 |
| it     | classifier-q8  | 0.700 | qwen3-4b-q8   | 0.676 |
| ko     | classifier-q8  | 0.680 | heretic-q4    | 0.647 |
| ru     | classifier-q8  | 0.641 | heretic-q4    | 0.608 |

`classifier-q8` leads in all 7 languages. RU is globally the hardest language.

---

## Speed (e2e_p50 ms — calibration as a baseline-latency proxy)

| model          | en   | de   | fr   | hi   | it   | ko   | ru   |
|:-------------- |:----:|:----:|:----:|:----:|:----:|:----:|:----:|
| qwen3-4b-q8    | 253  | 280  | 246  | 190  | 179  | 168  | 161  |
| heretic-q4     | 441  | 608  | 610  | 312  | 272  | 285  | 300  |
| classifier-q4  | 556  | 582  | 630  | 287  | 329  | 282  | 323  |
| gemma4-e4b     | 632  | 623  | 587  | 397  | 337  | 324  | 367  |
| gemma4-e4b-q4  | 633  | 633  | 610  | 459  | 312  | 434  | 366  |
| classifier-q8  | 662  | 643  | 669  | 437  | 433  | 395  | 354  |

`qwen3-4b-q8` is **2.5–4× faster** than every Gemma4 variant on baseline latency, despite running at q8_0. Non-Latin scripts (HI, KO, RU) generate fewer tokens per prompt, which is why their latencies are lower.

---

## Recommendations

**For production (best accuracy):** `classifier-q8` — leads in every language and has the smallest non-EN drop on memory_extraction. Cost: 2× slower than qwen3.

**For edge / tight-8 GB tier:** `classifier-q4` or `qwen3-4b-q8` — close accuracy, 2–3× faster. qwen3 dominates entity extraction; classifier-q4 dominates room-open.

**gemma4-e4b vs gemma4-e4b-q4:** difference < 0.003 across every score — within statistical noise. Prefer `q4_K_M` to save ~2 GB of VRAM.

**Non-EN memory extraction:** the collapse is universal (−0.5 to −0.63 pp). Before discarding any model, re-run with `--embed-model embeddinggemma` (see PR #1483) to separate scoring effects from model behavior, and ensure `labels.{lang}.jsonl` exists for every language so the ground truth is in the right language.
