"""Run the full matrix: candidates × (task, mode) → CSV.

Usage:
    python -m benchmarks.model_eval.orchestrator \\
        --candidates tier1 \\
        --tasks all \\
        --dataset-dir benchmarks/model_eval/datasets \\
        --output benchmarks/model_eval/results/$(date -u +%Y-%m-%d)-$(hostname).csv

The matrix per default:
    candidates: all `tier1` entries from candidates.yaml
    tasks: room_classification (closed + open), entity_extraction,
           memory_extraction, calibration
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

import yaml

from .runner import Result, _result_to_dict, run


TASK_MODES = [
    ("room_classification", "closed"),
    ("room_classification", "open"),
    ("entity_extraction", "default"),
    ("memory_extraction", "default"),
    ("calibration", "default"),
]


CSV_COLUMNS = [
    "model_tag",
    "task",
    "mode",
    "n_samples",
    "accuracy",
    "ttft_p50_ms",
    "ttft_p95_ms",
    "tps_p50",
    "tps_p95",
    "e2e_p50_ms",
    "e2e_p95_ms",
    "vram_resident_mb",
    "vram_peak_mb",
    "host",
    "gpu",
    "ollama_version",
    "run_date",
    "error",
    "extras_json",
]


def load_candidates(path: Path, tier: str) -> list[dict]:
    with open(path, "r") as f:
        data = yaml.safe_load(f)
    candidates = data.get("candidates", [])
    if tier == "all":
        return candidates
    if tier == "cloud":
        return [c for c in candidates if c.get("tier") == "cloud" or c.get("cloud")]
    if tier == "local":
        return [c for c in candidates if c.get("tier") != "cloud" and not c.get("cloud")]
    if tier == "modern":
        return [c for c in candidates if c.get("tier") == "modern"]
    if tier == "community":
        return [c for c in candidates if c.get("tier") == "community"]
    if tier.startswith("tier<="):
        try:
            n = int(tier.split("<=")[1])
        except (ValueError, IndexError):
            return []
        return [c for c in candidates if isinstance(c.get("tier"), int) and c["tier"] <= n]
    if tier.startswith("tier"):
        try:
            n = int(tier[4:])
        except ValueError:
            return []
        return [c for c in candidates if c.get("tier") == n]
    return [c for c in candidates if c.get("tag") == tier]


def parse_tasks(arg: str) -> list[tuple[str, str]]:
    if arg == "all":
        return TASK_MODES
    out = []
    for piece in arg.split(","):
        piece = piece.strip()
        if ":" in piece:
            t, m = piece.split(":", 1)
            out.append((t, m))
        else:
            for t, m in TASK_MODES:
                if t == piece:
                    out.append((t, m))
    return out


def result_to_row(result: Result) -> dict:
    return {
        "model_tag": result.model_tag,
        "task": result.task,
        "mode": result.mode,
        "n_samples": result.n_samples,
        "accuracy": round(result.accuracy, 4),
        "ttft_p50_ms": round(result.timing.ttft_p50_ms, 1),
        "ttft_p95_ms": round(result.timing.ttft_p95_ms, 1),
        "tps_p50": round(result.timing.tps_p50, 1),
        "tps_p95": round(result.timing.tps_p95, 1),
        "e2e_p50_ms": round(result.timing.e2e_p50_ms, 1),
        "e2e_p95_ms": round(result.timing.e2e_p95_ms, 1),
        "vram_resident_mb": result.vram_resident_mb if result.vram_resident_mb else "",
        "vram_peak_mb": result.vram_peak_mb if result.vram_peak_mb else "",
        "host": result.host.hostname,
        "gpu": result.host.gpu,
        "ollama_version": result.host.ollama_version.split("\n")[0] if result.host.ollama_version else "",
        "run_date": result.run_date,
        "error": result.error or "",
        "extras_json": json.dumps(result.extras, separators=(",", ":")),
    }


def main():
    parser = argparse.ArgumentParser(description="Run benchmark matrix across candidates")
    parser.add_argument("--candidates-file", type=Path, default=Path(__file__).parent / "candidates.yaml")
    parser.add_argument("--candidates", default="tier1", help="tier1, tier2, tier3, all, tier<=2, or a specific model tag")
    parser.add_argument("--tasks", default="all", help="all, or comma-separated list (e.g. 'room_classification:closed,calibration')")
    parser.add_argument("--dataset-dir", required=True, type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--endpoint", default="http://localhost:11434")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--n", type=int, default=None, help="Limit each task to first N samples (debug mode)")
    parser.add_argument(
        "--continue-on-error",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Continue running remaining (model, task) pairs after a failure (default). Use --no-continue-on-error to abort on first failure.",
    )
    args = parser.parse_args()

    candidates = load_candidates(args.candidates_file, args.candidates)
    if not candidates:
        print(f"No candidates matched: {args.candidates}", file=sys.stderr)
        sys.exit(1)

    task_modes = parse_tasks(args.tasks)
    if not task_modes:
        print(f"No tasks matched: {args.tasks}", file=sys.stderr)
        sys.exit(1)

    print(f"Running {len(candidates)} candidates × {len(task_modes)} task/mode pairs = {len(candidates) * len(task_modes)} runs")
    print(f"Output: {args.output}")

    args.output.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    total = len(candidates) * len(task_modes)
    i = 0
    start = time.time()

    # Open the CSV once, write header, and flush after each row so a
    # crash or Ctrl-C preserves partial progress. Long matrix runs (60+
    # min for the local tier) make this important.
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        f.flush()

        for candidate in candidates:
            tag = candidate["tag"]
            for task, mode in task_modes:
                i += 1
                print(f"[{i}/{total}] {tag}  {task}  {mode}", flush=True)
                try:
                    result = run(
                        model_tag=tag,
                        task=task,
                        mode=mode,
                        dataset_dir=args.dataset_dir,
                        endpoint=args.endpoint,
                        warmup=args.warmup,
                        n_samples=args.n,
                    )
                except Exception as e:
                    if not args.continue_on_error:
                        raise
                    print(f"  ERROR: {e}", file=sys.stderr)
                    continue
                row = result_to_row(result)
                rows.append(row)
                writer.writerow(row)
                f.flush()
                print(f"  acc={row['accuracy']}  e2e_p50={row['e2e_p50_ms']}ms  vram={row['vram_resident_mb']}", flush=True)

    elapsed = time.time() - start
    print(f"\nDone in {elapsed/60:.1f}min. Wrote {len(rows)} rows to {args.output}")


def write_csv(path: Path, rows: list[dict]):
    """Batch-write helper, kept for callers that already have all rows in memory."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
