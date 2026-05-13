#!/usr/bin/env bash
set -uo pipefail
DATA=/Users/macmini/Projects/metis-pair/benchmarks/data/longmemeval/longmemeval_s_cleaned.json
SPLIT=benchmarks/lme_split_50_450.json
OUTDIR=benchmarks/c_beta
LOG=$OUTDIR/sweep.log
CSV=$OUTDIR/sweep_results.csv

cd /Users/macmini/Projects/mempalace
mkdir -p "$OUTDIR"
: > "$LOG"
echo "mode,granularity,hybrid_weight,n_questions,wall_sec,recall_at_1,recall_at_5,recall_at_10,recall_at_30,ndcg_at_10,ndcg_at_30,out_file" > "$CSV"

run() {
  local mode=$1 gran=$2 hw=${3:-}
  local tag="${mode}_${gran}"
  local hw_arg=""
  [[ -n "$hw" ]] && { hw_arg="--hybrid-weight $hw"; tag="${tag}_hw${hw}"; }
  local out="$OUTDIR/${tag}.jsonl"
  local stdout="$OUTDIR/${tag}.stdout"

  echo "===== $tag =====" | tee -a "$LOG"
  local t0=$(date +%s)
  uv run python benchmarks/longmemeval_bench.py "$DATA" \
    --mode "$mode" --granularity "$gran" \
    --split-file "$SPLIT" --dev-only \
    $hw_arg --out "$out" > "$stdout" 2>&1
  local rc=$?
  local t1=$(date +%s)
  local wall=$((t1 - t0))

  if [[ $rc -ne 0 ]]; then
    echo "FAIL rc=$rc wall=${wall}s" | tee -a "$LOG"
    echo "$mode,$gran,${hw:-NA},FAIL,$wall,,,,,,$out" >> "$CSV"
    return
  fi

  # Parse session-level recall/ndcg from the "SESSION-LEVEL METRICS:" block.
  local r1 r5 r10 r30 n10 n30
  r1=$(awk  '/SESSION-LEVEL METRICS/{flag=1;next} flag && /Recall@ 1:/  {print $2; exit}' "$stdout")
  r5=$(awk  '/SESSION-LEVEL METRICS/{flag=1;next} flag && /Recall@ 5:/  {print $2; exit}' "$stdout")
  r10=$(awk '/SESSION-LEVEL METRICS/{flag=1;next} flag && /Recall@10:/  {print $2; exit}' "$stdout")
  r30=$(awk '/SESSION-LEVEL METRICS/{flag=1;next} flag && /Recall@30:/  {print $2; exit}' "$stdout")
  n10=$(awk '/SESSION-LEVEL METRICS/{flag=1;next} flag && /NDCG@10:/    {print $4; exit}' "$stdout")
  n30=$(awk '/SESSION-LEVEL METRICS/{flag=1;next} flag && /NDCG@30:/    {print $4; exit}' "$stdout")

  echo "OK wall=${wall}s R@1=$r1 R@5=$r5 R@10=$r10 R@30=$r30 NDCG@10=$n10 NDCG@30=$n30" | tee -a "$LOG"
  echo "$mode,$gran,${hw:-NA},50,$wall,$r1,$r5,$r10,$r30,$n10,$n30,$out" >> "$CSV"
}

# Baseline: raw, no hybrid_weight arg
run raw session
run raw turn

# Hybrid v4 sweep across hybrid_weight
for hw in 0.0 0.30 0.60; do
  run hybrid_v4 session "$hw"
  run hybrid_v4 turn    "$hw"
done

echo "===== DONE =====" | tee -a "$LOG"
echo "" | tee -a "$LOG"
column -ts, "$CSV" | tee -a "$LOG"
