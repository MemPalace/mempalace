#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
cd "$REPO_ROOT"
OUTDIR=benchmarks/c_beta
CSV=$OUTDIR/sweep_results.csv

echo "mode,granularity,hybrid_weight,n_questions,recall_at_1,recall_at_5,recall_at_10,recall_at_30,ndcg_at_10,ndcg_at_30,out_file" > "$CSV"

for f in "$OUTDIR"/*.stdout; do
  base=$(basename "$f" .stdout)
  # filename pattern: <mode>_<gran>[_hwX.XX]
  mode="" gran="" hw="NA"
  if [[ "$base" == hybrid_v4_* ]]; then
    mode=hybrid_v4
    rest=${base#hybrid_v4_}
  elif [[ "$base" == raw_* ]]; then
    mode=raw
    rest=${base#raw_}
  else
    continue
  fi
  if [[ "$rest" == *_hw* ]]; then
    gran=${rest%%_hw*}
    hw=${rest#*_hw}
  else
    gran=$rest
  fi

  # SESSION-LEVEL block. "Recall@ 1: 0.x" has space → $3. "Recall@10: 0.x" no space → $2.
  r1=$(awk  '/SESSION-LEVEL METRICS/{flag=1;next} flag && /Recall@ 1:/  {print $3; exit}' "$f")
  r5=$(awk  '/SESSION-LEVEL METRICS/{flag=1;next} flag && /Recall@ 5:/  {print $3; exit}' "$f")
  r10=$(awk '/SESSION-LEVEL METRICS/{flag=1;next} flag && /Recall@10:/  {print $2; exit}' "$f")
  r30=$(awk '/SESSION-LEVEL METRICS/{flag=1;next} flag && /Recall@30:/  {print $2; exit}' "$f")
  # NDCG@10 / NDCG@30 share the line with Recall: "Recall@10: 0.x    NDCG@10: 0.y"
  n10=$(awk '/SESSION-LEVEL METRICS/{flag=1;next} flag && /NDCG@10:/    {print $4; exit}' "$f")
  n30=$(awk '/SESSION-LEVEL METRICS/{flag=1;next} flag && /NDCG@30:/    {print $4; exit}' "$f")
  out="$OUTDIR/${base}.jsonl"

  echo "$mode,$gran,$hw,50,$r1,$r5,$r10,$r30,$n10,$n30,$out" >> "$CSV"
done

echo
column -ts, "$CSV"
