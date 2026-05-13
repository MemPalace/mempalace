# C-β v0.1, Results

50q dev-split, LongMemEval, default MiniLM, no LLM rerank. All numbers
below are **post-fix** (the dead-`granularity`-parameter defect documented
in the "Defect" section was discovered mid-sweep and fixed; every row in
the matrix was re-run against the fixed code).

## Sweep matrix

| mode      | granularity | hw   | R@1   | R@5   | R@10  | NDCG@10 | wall  |
|-----------|-------------|------|-------|-------|-------|---------|-------|
| raw       | session     |, | 0.840 | 0.960 | 0.980 | 0.905   | 62s   |
| raw       | turn        |, | 0.880 | 0.940 | 0.960 | 0.885   | 257s  |
| hybrid_v4 | session     | 0.0  | 0.880 | 0.980 | 1.000 | 0.937   | 81s   |
| hybrid_v4 | session     | 0.30 | 0.880 | 0.960 | 1.000 | 0.944   | 85s   |
| hybrid_v4 | session     | 0.60 | 0.880 | 0.960 | 0.980 | 0.936   | 90s   |
| hybrid_v4 | turn        | 0.0  | 0.900 | 0.980 | 1.000 | 0.944   | 283s  |
| hybrid_v4 | turn        | 0.30 | 0.920 | 0.960 | 1.000 | **0.954** | 288s |
| hybrid_v4 | turn        | 0.60 | 0.920 | 0.960 | 1.000 | 0.951   | 285s  |

Best NDCG@10 on the matrix: `hybrid_v4 turn hw=0.30` at 0.954.

## Defect found mid-sweep

`build_palace_and_retrieve_hybrid_v4` accepted a `granularity` parameter
but never branched on it: the corpus loop always emitted one document per
session (`"\n".join(user_turns)`). With the defect in place,
`hybrid_v4 turn hw=0.0` and `hybrid_v4 session hw=0.0` produced bitwise
identical metrics. Originally framed as a curious anomaly; verifying the
code path showed it was a dead parameter.

Fix shape:
- Branch the corpus build on `granularity`; emit one doc per user turn
  in turn mode, using `{sess_id}_turn_{i}` corpus IDs so the existing
  `session_id_from_corpus_id` helper rolls turns up to sessions during
  evaluation.
- In turn mode, `corpus_full[i]` mirrors the full session text (not the
  single user turn) so the assistant-reference two-pass still has
  assistant content to query in Pass 2. `corpus_user[i]` stays the
  granular per-turn signal.
- Dedup by session id in both the assistant-reference two-pass and the
  main scoring path: multiple high-scoring turns of the same session
  collapse to a single ranked entry.
- Synthetic preference docs remain session-aggregated and resolve to the
  first user-turn index of their session when a pref-hit drives the rank.

The same defect was present in `hybrid_v2` and `hybrid_v3`; they got the
same fix. `palace` and `diary` are intrinsically session-keyed (hall
classification, drawers, preference wing, LLM topic cache), so they now
raise `ValueError` on `granularity != "session"` rather than silently
ignoring the flag.

## Hypothesis test

**H0 (wash):** keyword-boost lift exists only because session-level text
is long enough for lexical overlap to land easily; at turn granularity
the lift should vanish or invert.

**H1 (compound):** lift survives at turn granularity; the keyword signal
is independent of doc length.

NDCG@10 lift hw=0.0 → hw=0.30:
- session: 0.937 → 0.944 = **+0.007**
- turn:    0.944 → 0.954 = **+0.010**

NDCG@10 from hw=0.30 → hw=0.60:
- session: 0.944 → 0.936 = −0.008
- turn:    0.954 → 0.951 = −0.003

Both granularities show the same concave shape (peak at hw=0.30, drop at
hw=0.60). Turn lift is slightly larger than session lift in absolute
terms. **H0 rejected.** The keyword boost is not a session-length artifact.

## Secondary observation: turn ≥ session across the whole hybrid_v4 sweep

Post-fix, `hybrid_v4 turn` matches or beats `hybrid_v4 session` on
NDCG@10 at every hw tested (Δ = +0.007, +0.010, +0.015 for hw =
0.0/0.30/0.60). The pre-fix table showed the inverse because turn mode
was silently producing session-level metrics minus the assistant
content. Now that Pass 2 has access to full session text via
`corpus_full` while the primary retrieval signal stays per-turn, the
combination of granular ranking and context-rich rerank dominates the
session baseline on this split.

Sample is small (50q), so this is directional. A bootstrap-resampled run
across the full 500q would be the honest next step before claiming turn
mode as a default.

## Caveats

- 50 q dev split. R@10 saturates at 1.000 across the `hybrid_v4` matrix;
  the discrimination is in NDCG@10, with deltas in the 0.005, 0.015 range.
- The dev split was held fixed across runs
  (`benchmarks/lme_split_50_450.json`), so runs are paired on questions
  but not bootstrap-resampled.
- `aaak`, `rooms`, `hybrid`, `full` were spot-checked to already honor
  `--granularity` and were not part of this sweep. A separate sweep
  would be needed to test the wash hypothesis on those modes.
- Full sweep on `hybrid_v2` / `hybrid_v3` across the matrix was not run
  (out of scope here); only 5q smokes at hw=0.30 turn, both at 1.000.
