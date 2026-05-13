#!/usr/bin/env bash
set -uo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
cd "$REPO_ROOT"
DATA="${DATA:-$HOME/Projects/metis-pair/benchmarks/data/longmemeval/longmemeval_s_cleaned.json}"
if [[ ! -f "$DATA" ]]; then
  echo "ERROR: dataset not found at $DATA" >&2
  exit 2
fi
SPLIT=benchmarks/lme_split_50_450.json
OUTDIR=benchmarks/c_beta
LOG=$OUTDIR/turn_sweep.log

: > "$LOG"

for HW in 0.0 0.30 0.60; do
  TAG="hybrid_v4_turn_fixed_hw${HW}"
  OUT="$OUTDIR/${TAG}.jsonl"
  STDOUT="$OUTDIR/${TAG}.stdout"
  echo "===== $TAG =====" | tee -a "$LOG"
  START=$(date +%s)
  uv run python benchmarks/longmemeval_bench.py "$DATA" \
    --mode hybrid_v4 --granularity turn --hybrid-weight "$HW" \
    --split-file "$SPLIT" --dev-only --out "$OUT" \
    > "$STDOUT" 2>&1
  RC=$?
  END=$(date +%s)
  WALL=$((END - START))
  if [[ $RC -eq 0 ]]; then
    R10=$(awk '/SESSION-LEVEL METRICS/{f=1;next} f && /Recall@10:/{print $2; exit}' "$STDOUT")
    N10=$(awk '/SESSION-LEVEL METRICS/{f=1;next} f && /NDCG@10:/{print $4; exit}' "$STDOUT")
    echo "OK wall=${WALL}s R@10=$R10 NDCG@10=$N10" | tee -a "$LOG"
  else
    echo "FAIL rc=$RC wall=${WALL}s" | tee -a "$LOG"
  fi
done
echo "DONE" | tee -a "$LOG"
