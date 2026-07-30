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
import socket
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

import yaml

from .runner import Result, _EMBED_MODEL, _result_to_dict, run


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
    "language",
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
    # Specific tag, or comma-separated list of tags. Synthesize minimal entries
    # for tags not present in candidates.yaml so ad-hoc models can be evaluated
    # without editing the yaml.
    requested_tags = [t.strip() for t in tier.split(",") if t.strip()]
    known_by_tag = {c["tag"]: c for c in candidates}
    return [known_by_tag.get(t, {"tag": t}) for t in requested_tags]


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
        "language": result.language,
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
    output_group = parser.add_mutually_exclusive_group(required=True)
    output_group.add_argument("--output", type=Path, default=None,
                              help="Single CSV output (all languages in one file).")
    output_group.add_argument("--output-dir", type=Path, default=None,
                              help="Output directory; writes <dir>/<lang>/YYYY-MM-DD-<host>.csv "
                                   "per language so results stay grouped by locale.")
    parser.add_argument("--endpoint", default="http://localhost:11434")
    parser.add_argument("--llm-provider", default="ollama",
                        choices=["ollama", "openai-compat", "anthropic"],
                        help="LLM provider (default: ollama)")
    parser.add_argument("--embed-endpoint", default=None,
                        help="Endpoint for the embedding model (always Ollama). "
                             "When omitted: defaults to --endpoint if --llm-provider=ollama, "
                             "else http://localhost:11434.")
    parser.add_argument("--embed-model", default=_EMBED_MODEL,
                        help=f"Embedding model for semantic-similarity scoring. Default: {_EMBED_MODEL}.")
    parser.add_argument("--num-ctx", type=int, default=4096,
                        help="Ollama context window per request (sent as options.num_ctx). "
                             "Defaults to 4096 so every candidate runs at the same window regardless "
                             "of its Modelfile default — without this, a 32k-default model pre-allocates "
                             "KV cache that a 4k-default model doesn't, and accuracy/latency/VRAM stop "
                             "being comparable. Pass a larger value if a task prompt exceeds 4k tokens.")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--n", type=int, default=None, help="Limit each task to first N samples (debug mode)")
    parser.add_argument(
        "--languages",
        default="en",
        help="Comma-separated dataset languages. 'en' uses dataset.jsonl; "
             "other values use dataset.{lang}.jsonl. Example: 'en,pt-BR,es,zh'.",
    )
    parser.add_argument(
        "--continue-on-error",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Continue running remaining (model, task) pairs after a failure (default). Use --no-continue-on-error to abort on first failure.",
    )
    args = parser.parse_args()

    # Resolve --embed-endpoint default: it lives on Ollama always, but should
    # follow --endpoint when the LLM provider is Ollama so a remote benchmark
    # run scores against the same host.
    if args.embed_endpoint is None:
        args.embed_endpoint = args.endpoint if args.llm_provider == "ollama" else "http://localhost:11434"

    candidates = load_candidates(args.candidates_file, args.candidates)
    if not candidates:
        print(f"No candidates matched: {args.candidates}", file=sys.stderr)
        sys.exit(1)

    task_modes = parse_tasks(args.tasks)
    if not task_modes:
        print(f"No tasks matched: {args.tasks}", file=sys.stderr)
        sys.exit(1)

    languages = [lang.strip() for lang in args.languages.split(",") if lang.strip()]
    if not languages:
        languages = ["en"]

    n_runs = len(candidates) * len(task_modes) * len(languages)
    print(f"Running {len(candidates)} candidates × {len(task_modes)} task/mode × {len(languages)} languages = {n_runs} runs")
    print(f"Languages: {languages}")

    # Resolve output path(s). --output-dir writes one CSV per language so results
    # from long multilingual runs stay grouped by locale and are easier to diff.
    # --output writes everything to one CSV (one shared file handle to avoid
    # interleaved buffer corruption across languages).
    _host = socket.gethostname()
    _date = time.strftime("%Y-%m-%d")

    def _csv_path(language: str) -> Path:
        if args.output_dir:
            return args.output_dir / language / f"{_date}-{_host}.csv"
        return args.output

    lang_files: dict[str, tuple] = {}
    if args.output_dir:
        # One file per language — no path collisions possible.
        for lang in languages:
            p = _csv_path(lang)
            p.parent.mkdir(parents=True, exist_ok=True)
            fh = open(p, "w", newline="", encoding="utf-8")
            w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            w.writeheader()
            fh.flush()
            lang_files[lang] = (fh, w)
            print(f"  {lang} → {p}")
    else:
        # Single shared file — open once, share the same (fh, writer) for every
        # language so writes don't fight over independent buffer offsets.
        p = args.output
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = open(p, "w", newline="", encoding="utf-8")
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        fh.flush()
        for lang in languages:
            lang_files[lang] = (fh, w)
        print(f"  output → {p}")

    rows = []
    total = n_runs
    i = 0
    start = time.time()

    try:
        for candidate in candidates:
            tag = candidate["tag"]
            for task, mode in task_modes:
                for language in languages:
                    fh, writer = lang_files[language]
                    i += 1
                    print(f"[{i}/{total}] {tag}  {task}  {mode}  lang={language}", flush=True)
                    try:
                        result = run(
                            model_tag=tag,
                            task=task,
                            mode=mode,
                            dataset_dir=args.dataset_dir,
                            endpoint=args.endpoint,
                            warmup=args.warmup,
                            n_samples=args.n,
                            llm_provider=args.llm_provider,
                            embed_endpoint=args.embed_endpoint,
                            embed_model=args.embed_model,
                            language=language,
                            num_ctx=args.num_ctx,
                        )
                    except Exception as e:
                        if not args.continue_on_error:
                            raise
                        print(f"  ERROR: {e}", file=sys.stderr)
                        continue
                    row = result_to_row(result)
                    rows.append(row)
                    writer.writerow(row)
                    fh.flush()
                    print(f"  acc={row['accuracy']}  e2e_p50={row['e2e_p50_ms']}ms  vram={row['vram_resident_mb']}", flush=True)
    finally:
        # In --output mode every language maps to the same fh; dedupe by id to
        # avoid double-close.
        seen_fhs: set[int] = set()
        for fh, _ in lang_files.values():
            if id(fh) in seen_fhs:
                continue
            seen_fhs.add(id(fh))
            fh.close()

    elapsed = time.time() - start
    output_summary = args.output or args.output_dir
    print(f"\nDone in {elapsed/60:.1f}min. Wrote {len(rows)} rows to {output_summary}")


def write_csv(path: Path, rows: list[dict]):
    """Batch-write helper, kept for callers that already have all rows in memory."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
