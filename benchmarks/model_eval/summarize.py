"""Render a benchmark CSV as a readable markdown report.

Usage:
    python -m benchmarks.model_eval.summarize \\
        --csv benchmarks/model_eval/results/2026-05-10-host.csv \\
        --output benchmarks/model_eval/reports/2026-05-10-host.md

The report includes:
- Per-task accuracy rankings
- Per-task speed rankings (e2e p50, TPS p50)
- VRAM consumption table
- Combined production recommendation (accuracy ≥ 0.8 AND e2e_p50 < 500ms)
- Open-set viability (does any model meet the discover-mode threshold?)
- Instruct vs reasoning comparison for qwen3:4b pair
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Optional


def load_rows(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def fmt(v, decimals: int = 2, default: str = "—") -> str:
    if v is None or v == "":
        return default
    try:
        f = float(v)
        return f"{f:.{decimals}f}"
    except (ValueError, TypeError):
        return str(v)


def rank_by(rows: list[dict], task: str, mode: str, key: str, reverse: bool = True) -> list[dict]:
    filtered = [r for r in rows if r["task"] == task and r["mode"] == mode and not r.get("error")]
    return sorted(filtered, key=lambda r: float(r.get(key) or 0), reverse=reverse)


def render_accuracy_table(rows: list[dict], task: str, mode: str, primary_key: str = "accuracy") -> str:
    ranked = rank_by(rows, task, mode, primary_key, reverse=True)
    if not ranked:
        return f"_No successful runs for {task}/{mode}._\n"

    lines = []
    extras_keys = sorted({k for r in ranked for k in json.loads(r.get("extras_json") or "{}").keys()})
    extras_keys = [k for k in extras_keys if k not in {"correct", "total", "error_count", "error_sample"}][:4]

    header = ["Rank", "Model", primary_key]
    if extras_keys:
        header.extend(extras_keys)
    header.extend(["e2e p50 ms", "TPS p50", "VRAM resident MB"])
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    for i, r in enumerate(ranked, 1):
        extras = json.loads(r.get("extras_json") or "{}")
        row = [str(i), r["model_tag"], fmt(r[primary_key], 3)]
        for k in extras_keys:
            row.append(fmt(extras.get(k), 2))
        row.extend([fmt(r.get("e2e_p50_ms"), 1), fmt(r.get("tps_p50"), 1), r.get("vram_resident_mb") or "—"])
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def render_speed_table(rows: list[dict], task: str, mode: str) -> str:
    valid = [r for r in rows if r["task"] == task and r["mode"] == mode and not r.get("error") and r.get("e2e_p50_ms")]
    ranked = sorted(valid, key=lambda r: float(r.get("e2e_p50_ms") or 99999))
    if not ranked:
        return ""

    lines = ["| Rank | Model | e2e p50 ms | e2e p95 ms | TTFT p50 ms | TPS p50 | TPS p95 |",
             "|---|---|---|---|---|---|---|"]
    for i, r in enumerate(ranked, 1):
        lines.append(
            f"| {i} | {r['model_tag']} | "
            f"{fmt(r['e2e_p50_ms'], 1)} | {fmt(r['e2e_p95_ms'], 1)} | "
            f"{fmt(r['ttft_p50_ms'], 1)} | {fmt(r['tps_p50'], 1)} | {fmt(r['tps_p95'], 1)} |"
        )
    return "\n".join(lines) + "\n"


def render_vram_table(rows: list[dict]) -> str:
    by_model: dict[str, dict] = {}
    for r in rows:
        if r.get("error"):
            continue
        tag = r["model_tag"]
        if tag not in by_model:
            by_model[tag] = {"resident": r.get("vram_resident_mb"), "peak": r.get("vram_peak_mb")}
        else:
            cur_peak = by_model[tag]["peak"]
            new_peak = r.get("vram_peak_mb")
            if new_peak and (not cur_peak or int(new_peak) > int(cur_peak)):
                by_model[tag]["peak"] = new_peak
            if not by_model[tag]["resident"] and r.get("vram_resident_mb"):
                by_model[tag]["resident"] = r.get("vram_resident_mb")

    rows_sorted = sorted(
        by_model.items(),
        key=lambda kv: int(kv[1]["resident"] or 0),
    )
    lines = ["| Model | Resident MB | Peak MB | Delta MB |", "|---|---|---|---|"]
    for tag, vram in rows_sorted:
        resident = vram["resident"]
        peak = vram["peak"]
        try:
            delta = int(peak) - int(resident) if resident and peak else None
        except (TypeError, ValueError):
            delta = None
        lines.append(f"| {tag} | {resident or '—'} | {peak or '—'} | {delta if delta is not None else '—'} |")
    return "\n".join(lines) + "\n"


def render_production_picks(rows: list[dict], min_acc: float = 0.80, max_e2e_ms: float = 500) -> str:
    """Models that meet a quality threshold AND a speed threshold across all tasks."""
    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("error"):
            continue
        by_model[r["model_tag"]].append(r)

    picks = []
    for tag, model_rows in by_model.items():
        primary_metrics = []
        e2e_max = 0.0
        for r in model_rows:
            if r["task"] == "memory_extraction":
                # coverage is the primary; mean_coverage is in extras
                extras = json.loads(r.get("extras_json") or "{}")
                primary_metrics.append(extras.get("mean_coverage", 0.0))
            elif r["task"] == "entity_extraction":
                primary_metrics.append(float(r.get("accuracy") or 0))
            elif r["task"] == "room_classification" and r["mode"] == "open":
                # similarity is the primary
                primary_metrics.append(float(r.get("accuracy") or 0))
            else:
                primary_metrics.append(float(r.get("accuracy") or 0))
            e2e = float(r.get("e2e_p50_ms") or 0)
            if e2e > e2e_max:
                e2e_max = e2e
        if not primary_metrics:
            continue
        avg_metric = sum(primary_metrics) / len(primary_metrics)
        meets_acc = all(m >= min_acc for m in primary_metrics)
        meets_speed = e2e_max <= max_e2e_ms
        if meets_acc and meets_speed:
            picks.append((tag, avg_metric, e2e_max))

    picks.sort(key=lambda x: -x[1])

    if not picks:
        return f"_No model met both thresholds (min_acc={min_acc}, max_e2e={max_e2e_ms}ms across all tasks)._\n"

    lines = [f"Models meeting min_accuracy ≥ {min_acc} on every task AND e2e p50 ≤ {max_e2e_ms}ms:\n"]
    lines.append("| Rank | Model | Avg primary metric | Worst e2e p50 ms |")
    lines.append("|---|---|---|---|")
    for i, (tag, avg, e2e) in enumerate(picks, 1):
        lines.append(f"| {i} | {tag} | {avg:.3f} | {e2e:.1f} |")
    return "\n".join(lines) + "\n"


def render_instruct_vs_reasoning(rows: list[dict]) -> str:
    instruct = [r for r in rows if r["model_tag"].startswith("qwen3:4b-instruct-2507") and not r.get("error")]
    reasoning = [r for r in rows if r["model_tag"].startswith("qwen3:4b-thinking") and not r.get("error")]

    if not instruct or not reasoning:
        return "_Not enough qwen3:4b paired results to compare._\n"

    inst_q4 = next((r for r in instruct if r["model_tag"] == "qwen3:4b-instruct-2507-q4_K_M"), None)
    reas_q4 = next((r for r in reasoning if r["model_tag"] == "qwen3:4b-thinking-2507-q4_K_M"), None)

    if not inst_q4 or not reas_q4:
        return "_qwen3:4b-instruct-2507-q4_K_M vs qwen3:4b-thinking-2507-q4_K_M comparison unavailable._\n"

    by_pair: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for r in instruct + reasoning:
        if r["model_tag"] not in {"qwen3:4b-instruct-2507-q4_K_M", "qwen3:4b-thinking-2507-q4_K_M"}:
            continue
        by_pair[(r["task"], r["mode"])][r["model_tag"]] = r

    lines = ["Direct comparison: qwen3:4b instruct vs reasoning at q4_K_M.\n"]
    lines.append("| Task | Mode | Instruct accuracy | Reasoning accuracy | Instruct e2e p50 | Reasoning e2e p50 |")
    lines.append("|---|---|---|---|---|---|")
    for (task, mode), pair in sorted(by_pair.items()):
        inst = pair.get("qwen3:4b-instruct-2507-q4_K_M")
        reas = pair.get("qwen3:4b-thinking-2507-q4_K_M")
        if not inst or not reas:
            continue
        lines.append(
            f"| {task} | {mode} | "
            f"{fmt(inst['accuracy'], 3)} | {fmt(reas['accuracy'], 3)} | "
            f"{fmt(inst['e2e_p50_ms'], 1)} | {fmt(reas['e2e_p50_ms'], 1)} |"
        )
    return "\n".join(lines) + "\n"


def render_open_set_viability(rows: list[dict], min_similarity: float = 0.7) -> str:
    open_runs = [r for r in rows if r["task"] == "room_classification" and r["mode"] == "open" and not r.get("error")]
    if not open_runs:
        return "_No open-set runs available._\n"

    qualified = [r for r in open_runs if float(r.get("accuracy") or 0) >= min_similarity]
    lines = [f"Open-set discovery viability. Threshold: mean cosine similarity ≥ {min_similarity}.\n"]

    if not qualified:
        lines.append(f"**No model met the threshold.** Best score: {max(float(r.get('accuracy') or 0) for r in open_runs):.3f}.")
        lines.append("\nRecommendation: do NOT ship `mempalace mine --mode discover`. Closed-set classification stays required.\n")
        return "\n".join(lines)

    lines.append(f"**{len(qualified)} model(s) met the threshold.**\n")
    lines.append("| Model | Mean similarity | Exact match count | High-sim (≥0.8) | Low-sim (<0.5) |")
    lines.append("|---|---|---|---|---|")
    for r in sorted(qualified, key=lambda r: -float(r.get("accuracy") or 0)):
        extras = json.loads(r.get("extras_json") or "{}")
        lines.append(
            f"| {r['model_tag']} | "
            f"{fmt(r['accuracy'], 3)} | "
            f"{extras.get('exact_match_count', '—')} | "
            f"{extras.get('high_similarity_count', '—')} | "
            f"{extras.get('low_similarity_count', '—')} |"
        )
    lines.append(f"\nRecommendation: ship `mempalace mine --mode discover` with the top model as default.\n")
    return "\n".join(lines)


def render_report(rows: list[dict]) -> str:
    if not rows:
        return "# Empty results\n\nNo rows in the CSV.\n"

    sample = next((r for r in rows if not r.get("error")), rows[0])
    host = sample.get("host", "unknown")
    gpu = sample.get("gpu", "unknown")
    ollama_v = sample.get("ollama_version", "unknown")
    run_date = sample.get("run_date", "unknown")

    n_models = len({r["model_tag"] for r in rows})
    n_runs = len(rows)
    n_errors = sum(1 for r in rows if r.get("error"))

    sections = [
        f"# Model evaluation report",
        "",
        f"- Host: `{host}`",
        f"- GPU: `{gpu}`",
        f"- Ollama: `{ollama_v}`",
        f"- Run date: {run_date}",
        f"- Runs: {n_runs} ({n_models} models × task/mode pairs); errors: {n_errors}",
        "",
        "## Production picks",
        "",
        render_production_picks(rows),
        "",
        "## Open-set discovery viability",
        "",
        render_open_set_viability(rows),
        "",
        "## Instruct vs reasoning (qwen3:4b)",
        "",
        render_instruct_vs_reasoning(rows),
        "",
        "## Per-task rankings",
        "",
        "### Calibration (sentence-type, exact match)",
        "",
        render_accuracy_table(rows, "calibration", "default"),
        "",
        "### Room classification — closed-set (exact match)",
        "",
        render_accuracy_table(rows, "room_classification", "closed"),
        "",
        "### Room classification — open-set (cosine similarity)",
        "",
        render_accuracy_table(rows, "room_classification", "open"),
        "",
        "### Entity extraction (mean F1)",
        "",
        render_accuracy_table(rows, "entity_extraction", "default"),
        "",
        "### Memory extraction (mean coverage)",
        "",
        render_accuracy_table(rows, "memory_extraction", "default"),
        "",
        "## Speed (calibration, smallest task — most stable timing)",
        "",
        render_speed_table(rows, "calibration", "default"),
        "",
        "## VRAM",
        "",
        render_vram_table(rows),
        "",
    ]

    errored = [r for r in rows if r.get("error")]
    if errored:
        sections.extend([
            "## Errors",
            "",
            "| Model | Task | Mode | Error |",
            "|---|---|---|---|",
        ])
        for r in errored:
            err_short = r["error"][:120].replace("|", "\\|")
            sections.append(f"| {r['model_tag']} | {r['task']} | {r['mode']} | {err_short} |")
        sections.append("")

    return "\n".join(sections)


def main():
    parser = argparse.ArgumentParser(description="Render benchmark CSV as a markdown report")
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    rows = load_rows(args.csv)
    report = render_report(rows)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8", newline="\n") as f:
            f.write(report)
        print(f"Wrote {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
