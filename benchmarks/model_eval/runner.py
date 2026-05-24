"""Run one (model, task, mode) triple. Output a single result row.

Usage:
    python -m benchmarks.model_eval.runner \\
        --model qwen3:4b-instruct-2507-q4_K_M \\
        --task room_classification \\
        --mode closed \\
        --dataset-dir benchmarks/model_eval/datasets

Designed to be called by orchestrator.py for matrix runs, but standalone
runnable for one-off debugging.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from mempalace.llm_client import LLMError, LLMResponse, get_provider

from .metrics import (
    HostInfo,
    TimingAggregate,
    TimingSample,
    VRAMPoller,
    aggregate_timings,
    embed_text,
    extract_timing,
    gather_host_info,
    strip_thinking_tokens,
    vram_resident_mb,
)
from .tasks.calibration import prompts as cal_prompts, score as cal_score
from .tasks.entity_extraction import prompts as ent_prompts, score as ent_score
from .tasks.memory_extraction import prompts as mem_prompts, score as mem_score
from .tasks.room_classification import prompts as rc_prompts, score as rc_score


@dataclass
class Result:
    model_tag: str
    task: str
    mode: str
    accuracy: float
    extras: dict
    timing: TimingAggregate
    vram_resident_mb: Optional[int]
    vram_peak_mb: Optional[int]
    host: HostInfo
    run_date: str
    n_samples: int
    error: Optional[str] = None


_EMBED_MODEL = "nomic-embed-text"

# Tasks that score via semantic similarity and require an embedding model.
_EMBED_TASKS: set[tuple[str, str]] = {
    ("memory_extraction", "default"),
    ("room_classification", "open"),
}


def _ensure_embed_model(endpoint: str, model: str = _EMBED_MODEL) -> None:
    """Verify the embedding model is available; pull it automatically if not.

    Raises RuntimeError with a clear message if the model cannot be made available.
    """
    if embed_text("ping", model=model, endpoint=endpoint) is not None:
        return

    print(f"  Embedding model '{model}' not found — pulling automatically...", file=sys.stderr, flush=True)
    # Pass OLLAMA_HOST so the pull targets the same endpoint being benchmarked,
    # not the default localhost:11434.
    env = {**os.environ, "OLLAMA_HOST": endpoint}
    try:
        result = subprocess.run(
            ["ollama", "pull", model],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"'ollama' not found on PATH. Install Ollama and ensure it is on PATH, "
            f"then run 'ollama pull {model}' manually."
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"Embedding model '{model}' is required for this task but could not be pulled. "
            f"Run 'ollama pull {model}' manually and retry.\n"
            f"ollama stderr: {result.stderr.strip()}"
        )

    if embed_text("ping", model=model, endpoint=endpoint) is None:
        raise RuntimeError(
            f"Embedding model '{model}' was pulled but is still not responding on {endpoint}. "
            f"Check Ollama logs."
        )

    print(f"  Embedding model '{model}' ready.", file=sys.stderr, flush=True)


def load_jsonl(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _classify_with_timing(
    provider, system: str, user: str, json_mode: bool
) -> tuple[Optional[LLMResponse], TimingSample, Optional[str]]:
    """Run one classify call. Always disables thinking on hybrid models.

    MemPalace classification tasks (room, entity, memory) never benefit
    from extended reasoning. Forcing think=False keeps hybrid Qwen 3
    models in fast-instruct mode, gives pure-instruct models a no-op,
    and ensures the benchmark measures the real production code path.
    """
    t0 = time.perf_counter()
    try:
        response = provider.classify(system=system, user=user, json_mode=json_mode, think=False)
    except LLMError as e:
        return None, TimingSample(0, 0, 0, 0, 0), str(e)
    elapsed = time.perf_counter() - t0
    timing = extract_timing(response.raw, elapsed)
    return response, timing, None


def _build_prompt(task: str, mode: str, sample: dict, label: dict) -> tuple[str, str, bool]:
    """Return (system, user, json_mode)."""
    if task == "calibration":
        return cal_prompts.SYSTEM, cal_prompts.build_user_prompt(sample["text"], sample["classes"]), False
    if task == "room_classification":
        if mode == "closed":
            return (
                rc_prompts.CLOSED_SYSTEM,
                rc_prompts.build_closed_user(sample["agent"], sample["session_summary"], sample["__rooms__"]),
                False,
            )
        return (
            rc_prompts.OPEN_SYSTEM,
            rc_prompts.build_open_user(sample["agent"], sample["session_summary"]),
            False,
        )
    if task == "entity_extraction":
        return ent_prompts.SYSTEM, ent_prompts.build_user(sample["text"]), True
    if task == "memory_extraction":
        return mem_prompts.SYSTEM, mem_prompts.build_user(sample["text"]), True
    raise ValueError(f"Unknown task: {task}")


def _score_one(task: str, mode: str, predicted: str, sample: dict, label: dict, endpoint: str) -> dict:
    if task == "calibration":
        return cal_score.score(predicted, label["label"], sample["classes"])
    if task == "room_classification":
        if mode == "closed":
            return rc_score.score_closed(predicted, label["closed_set_label"], sample["__rooms__"])
        return rc_score.score_open(predicted, label["preferred_open_label"], endpoint=endpoint)
    if task == "entity_extraction":
        return ent_score.score(predicted, label["entities"])
    if task == "memory_extraction":
        return mem_score.score(predicted, label["memories"], endpoint=endpoint)
    raise ValueError(f"Unknown task: {task}")


def _aggregate_accuracy(task: str, mode: str, scores: list[dict]) -> tuple[float, dict]:
    """Return (primary_accuracy, extras_dict)."""
    if not scores:
        return 0.0, {}

    if task == "calibration" or (task == "room_classification" and mode == "closed"):
        correct = sum(1 for s in scores if s.get("correct"))
        return correct / len(scores), {
            "correct": correct,
            "total": len(scores),
        }

    if task == "room_classification" and mode == "open":
        sims = [s.get("similarity", 0.0) for s in scores]
        exacts = sum(1 for s in scores if s.get("exact_match"))
        mean_sim = sum(sims) / len(sims)
        return mean_sim, {
            "mean_similarity": mean_sim,
            "exact_match_count": exacts,
            "high_similarity_count": sum(1 for s in sims if s >= 0.8),
            "low_similarity_count": sum(1 for s in sims if s < 0.5),
        }

    if task == "entity_extraction":
        f1 = sum(s.get("f1", 0.0) for s in scores) / len(scores)
        precision = sum(s.get("precision", 0.0) for s in scores) / len(scores)
        recall = sum(s.get("recall", 0.0) for s in scores) / len(scores)
        valid_json_rate = sum(1 for s in scores if s.get("valid_json")) / len(scores)
        return f1, {
            "mean_f1": f1,
            "mean_precision": precision,
            "mean_recall": recall,
            "valid_json_rate": valid_json_rate,
        }

    if task == "memory_extraction":
        coverage = sum(s.get("coverage", 0.0) for s in scores) / len(scores)
        hallucination = sum(s.get("hallucination_rate", 0.0) for s in scores) / len(scores)
        type_accuracy = sum(s.get("type_accuracy", 0.0) for s in scores) / len(scores)
        valid_json_rate = sum(1 for s in scores if s.get("valid_json")) / len(scores)
        return coverage, {
            "mean_coverage": coverage,
            "mean_hallucination_rate": hallucination,
            "mean_type_accuracy": type_accuracy,
            "valid_json_rate": valid_json_rate,
        }

    return 0.0, {}


def run(
    model_tag: str,
    task: str,
    mode: str,
    dataset_dir: Path,
    endpoint: str = "http://localhost:11434",
    warmup: int = 1,
    n_samples: Optional[int] = None,
    strip_thinking: bool = True,
) -> Result:
    """Run one (model, task, mode) triple. Returns a Result."""
    host = gather_host_info()
    run_date = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

    task_dir = dataset_dir / task
    samples = load_jsonl(task_dir / "dataset.jsonl")
    labels = load_jsonl(task_dir / "labels.jsonl")
    if len(samples) != len(labels):
        return Result(
            model_tag=model_tag, task=task, mode=mode,
            accuracy=0.0, extras={}, timing=aggregate_timings([]),
            vram_resident_mb=None, vram_peak_mb=None, host=host,
            run_date=run_date, n_samples=0,
            error=f"Sample/label count mismatch: {len(samples)} vs {len(labels)}",
        )

    if task == "room_classification":
        room_lists = load_jsonl(task_dir / "room_lists.jsonl")
        if len(room_lists) != len(samples):
            return Result(
                model_tag=model_tag, task=task, mode=mode,
                accuracy=0.0, extras={}, timing=aggregate_timings([]),
                vram_resident_mb=None, vram_peak_mb=None, host=host,
                run_date=run_date, n_samples=0,
                error=f"Room-list count mismatch: {len(room_lists)} vs {len(samples)}",
            )
        for s, rl in zip(samples, room_lists):
            s["__rooms__"] = rl["rooms"]

    if n_samples is not None:
        samples = samples[:n_samples]
        labels = labels[:n_samples]

    if (task, mode) in _EMBED_TASKS:
        try:
            _ensure_embed_model(endpoint)
        except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            return Result(
                model_tag=model_tag, task=task, mode=mode,
                accuracy=0.0, extras={}, timing=aggregate_timings([]),
                vram_resident_mb=None, vram_peak_mb=None, host=host,
                run_date=run_date, n_samples=0,
                error=f"Embedding model unavailable: {e}",
            )

    try:
        provider = get_provider("ollama", model=model_tag, endpoint=endpoint, timeout=180)
    except LLMError as e:
        return Result(
            model_tag=model_tag, task=task, mode=mode,
            accuracy=0.0, extras={}, timing=aggregate_timings([]),
            vram_resident_mb=None, vram_peak_mb=None, host=host,
            run_date=run_date, n_samples=0,
            error=f"Provider init failed: {e}",
        )

    if warmup > 0 and samples:
        s0, l0 = samples[0], labels[0]
        try:
            sys_p, user_p, json_mode = _build_prompt(task, mode, s0, l0)
            for _ in range(warmup):
                provider.classify(system=sys_p, user=user_p, json_mode=json_mode, think=False)
        except LLMError as e:
            return Result(
                model_tag=model_tag, task=task, mode=mode,
                accuracy=0.0, extras={}, timing=aggregate_timings([]),
                vram_resident_mb=None, vram_peak_mb=None, host=host,
                run_date=run_date, n_samples=0,
                error=f"Warmup failed: {e}",
            )

    poller = VRAMPoller()
    poller.start()

    timings: list[TimingSample] = []
    scores: list[dict] = []
    errors: list[str] = []

    for sample, label in zip(samples, labels):
        sys_p, user_p, json_mode = _build_prompt(task, mode, sample, label)
        response, timing, err = _classify_with_timing(provider, sys_p, user_p, json_mode)
        if err is not None:
            errors.append(err)
            continue
        text = response.text
        if strip_thinking:
            text = strip_thinking_tokens(text, response.raw)
        timings.append(timing)
        try:
            score_result = _score_one(task, mode, text, sample, label, endpoint)
            scores.append(score_result)
        except Exception as e:
            errors.append(f"score error: {e}")

    peak_vram = poller.stop()
    resident_vram = vram_resident_mb(model_tag, endpoint=endpoint)

    accuracy, extras = _aggregate_accuracy(task, mode, scores)
    if errors:
        extras["error_count"] = len(errors)
        extras["error_sample"] = errors[0]

    return Result(
        model_tag=model_tag,
        task=task,
        mode=mode,
        accuracy=accuracy,
        extras=extras,
        timing=aggregate_timings(timings),
        vram_resident_mb=resident_vram,
        vram_peak_mb=peak_vram,
        host=host,
        run_date=run_date,
        n_samples=len(scores),
    )


def main():
    parser = argparse.ArgumentParser(description="Run one (model, task, mode) benchmark triple")
    parser.add_argument("--model", required=True, help="Ollama model tag")
    parser.add_argument("--task", required=True, choices=["room_classification", "entity_extraction", "memory_extraction", "calibration"])
    parser.add_argument("--mode", default="closed", choices=["closed", "open", "default"], help="Mode: closed/open for room_classification, 'default' otherwise")
    parser.add_argument("--dataset-dir", required=True, type=Path, help="Path to the bench dataset root")
    parser.add_argument("--endpoint", default="http://localhost:11434")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--n", type=int, default=None, help="Limit to first N samples (for debugging)")
    parser.add_argument("--no-strip-thinking", action="store_true")
    args = parser.parse_args()

    if args.task != "room_classification" and args.mode in ("closed", "open"):
        args.mode = "default"

    result = run(
        model_tag=args.model,
        task=args.task,
        mode=args.mode,
        dataset_dir=args.dataset_dir,
        endpoint=args.endpoint,
        warmup=args.warmup,
        n_samples=args.n,
        strip_thinking=not args.no_strip_thinking,
    )

    print(json.dumps(_result_to_dict(result), indent=2))
    if result.error:
        sys.exit(1)


def _result_to_dict(r: Result) -> dict:
    return {
        "model_tag": r.model_tag,
        "task": r.task,
        "mode": r.mode,
        "n_samples": r.n_samples,
        "accuracy": round(r.accuracy, 4),
        "extras": r.extras,
        "timing": asdict(r.timing),
        "vram_resident_mb": r.vram_resident_mb,
        "vram_peak_mb": r.vram_peak_mb,
        "host": asdict(r.host),
        "run_date": r.run_date,
        "error": r.error,
    }


if __name__ == "__main__":
    main()
