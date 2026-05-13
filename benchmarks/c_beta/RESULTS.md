# C-β v0.1, Results

50q dev-split, LongMemEval, default MiniLM, no LLM rerank.

## Sweep matrix

| mode      | granularity | hw   | R@10  | NDCG@10 |
|-----------|-------------|------|-------|---------|
| raw       | session     |, | 0.980 | 0.905   |
| raw       | turn        |, | 0.960 | 0.885   |
| hybrid_v4 | session     | 0.0  | 1.000 | 0.937   |
| hybrid_v4 | session     | 0.30 | 1.000 | 0.944   |
| hybrid_v4 | session     | 0.60 | 0.980 | 0.936   |
| hybrid_v4 | turn        | 0.0  | 0.980 | 0.924   |
| hybrid_v4 | turn        | 0.30 | 0.980 | 0.934   |
| hybrid_v4 | turn        | 0.60 | 0.980 | 0.931   |

All `hybrid_v4 turn` rows above are post-fix. Pre-fix turn rows were
silently session-level (defect, see below).

## Defect found mid-sweep

`build_palace_and_retrieve_hybrid_v4` accepted a `granularity` parameter
but never branched on it: the corpus loop always emitted one document per
session (`"\n".join(user_turns)`). With the defect in place,
`hybrid_v4 turn hw=0.0` and `hybrid_v4 session hw=0.0` produced bitwise
identical metrics. Originally framed as a curious anomaly; verifying the
code path showed it was a dead parameter.

Fix: branch the corpus build on `granularity`; emit one doc per user turn
when `turn` is requested, using `{sess_id}_turn_{i}` corpus IDs so the
existing `session_id_from_corpus_id` helper can roll turns up to sessions
during evaluation. Dedup logic in both the assistant-reference two-pass
and the main scoring path was changed to dedup by session id (so multiple
high-scoring turns of the same session collapse to a single ranked entry).
Synthetic preference docs remain session-aggregated; they now resolve to
the first turn of their session when a pref-hit drives the ranking.

## Hypothesis test

H0 (wash): keyword boost lift exists only because session-level text is
long enough for lexical overlap to land easily; at turn granularity the
lift should vanish or invert.

H1 (compound): lift survives at turn granularity; the keyword signal is
independent of doc length.

Lift (NDCG@10, hw=0.0 → hw=0.30):
- session: 0.937 → 0.944 = **+0.007**
- turn:    0.924 → 0.934 = **+0.010**

Turn lift is comparable to (slightly larger than) session lift, with the
same concave shape (peak at hw=0.30, drop at hw=0.60). **H0 rejected.**

Secondary observation: the session-over-turn NDCG gap shrinks as hybrid
weight grows (Δ = 0.013 → 0.010 → 0.005 across hw = 0.0/0.30/0.60). Reads
as the keyword boost partially compensating for lost surrounding context
at turn granularity, lexical signal carries more weight when semantic
context per doc is smaller.

## Follow-up: audit of other modes

After the `hybrid_v4` fix, audited the remaining retrieval functions for
the same dead-`granularity`-parameter defect:

| function   | granularity branched? | fix                                       |
|------------|----------------------|--------------------------------------------|
| raw / aaak / rooms / hybrid / full | yes (pre-existing) | none, already honored the flag |
| hybrid_v2  | no                    | same per-turn corpus + session-id dedup pattern |
| hybrid_v3  | no                    | same per-turn corpus + session-id dedup pattern |
| hybrid_v4  | no                    | (fixed in the primary commit)              |
| palace     | no                    | explicit `ValueError` on `granularity != "session"`, palace's hall classification, drawers, and preference wing are intrinsically session-keyed; a turn-level rewrite changes the algorithm |
| diary      | no                    | explicit `ValueError` on `granularity != "session"`, LLM topic layer is computed and cached per `sess_id` |

Smoke tests (5q dev split): hybrid_v2 turn hw=0.30 and hybrid_v3 turn
hw=0.30 both score 1.000 across R@k / NDCG@k on a small-n smoke set;
palace turn raises cleanly; palace session is unchanged from baseline.

Full sweep on hybrid_v2/v3 across the matrix was not run, out of scope
for this PR. Anyone who wants to test the wash hypothesis on those modes
can now run a turn-granularity sweep against them honestly.

## Caveats

- 50 q split. R@10 saturates at 0.980 across the turn sweep; NDCG@10
  differences are 0.005, 0.013 wide. Effect is directionally clear but
  the sample is small.
- The dev split was held fixed across runs (`benchmarks/lme_split_50_450.json`),
  so the runs are paired on questions but not bootstrap-resampled.
- Only `hybrid_v4` was fixed. `hybrid`, `hybrid_v2`, `hybrid_v3`, `palace`,
  `diary`, `aaak`, `rooms` likely have the same dead-parameter defect; not
  audited in this scope.
