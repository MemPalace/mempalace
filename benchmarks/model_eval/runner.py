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
import re
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
    language: str = "en"


_EMBED_MODEL = "embeddinggemma"

# Language code validator. Accepts ISO-639-style codes with optional region
# subtag (en, pt-BR, zh-CN, fr_CA, etc.). Strict pattern is required because
# `language` is interpolated into the dataset filename — without validation a
# caller could pass values containing path separators or `..` and the loader
# would read files outside the task directory.
_LANGUAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)?$")

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


def _score_one(task: str, mode: str, predicted: str, sample: dict, label: dict, endpoint: str, embed_model: str = _EMBED_MODEL) -> dict:
    if task == "calibration":
        return cal_score.score(predicted, label["label"], sample["classes"])
    if task == "room_classification":
        if mode == "closed":
            return rc_score.score_closed(predicted, label["closed_set_label"], sample["__rooms__"])
        return rc_score.score_open(predicted, label["preferred_open_label"], embed_model=embed_model, endpoint=endpoint)
    if task == "entity_extraction":
        return ent_score.score(predicted, label["entities"])
    if task == "memory_extraction":
        return mem_score.score(predicted, label["memories"], embed_model=embed_model, endpoint=endpoint)
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
    llm_provider: str = "ollama",
    embed_endpoint: str = "http://localhost:11434",
    embed_model: str = _EMBED_MODEL,
    language: str = "en",
    num_ctx: Optional[int] = None,
) -> Result:
    """Run one (model, task, mode) triple. Returns a Result.

    When language != "en", loads `dataset.{language}.jsonl` instead of `dataset.jsonl`.
    Labels and room_lists are always loaded from the English files — non-English inputs
    are scored against the same English ground truth (cross-lingual mapping test).
    """
    host = gather_host_info()
    run_date = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

    if not _LANGUAGE_RE.match(language):
        return Result(
            model_tag=model_tag, task=task, mode=mode,
            accuracy=0.0, extras={}, timing=aggregate_timings([]),
            vram_resident_mb=None, vram_peak_mb=None, host=host,
            run_date=run_date, n_samples=0, language=language,
            error=f"Invalid language code: {language!r}. Expected pattern like 'en', 'pt-BR', 'zh-CN'.",
        )

    task_dir = dataset_dir / task
    dataset_file = "dataset.jsonl" if language == "en" else f"dataset.{language}.jsonl"
    dataset_path = task_dir / dataset_file
    # Belt-and-suspenders: even with the regex guard above, confirm the resolved
    # path lives inside task_dir before opening anything on disk.
    try:
        if not dataset_path.resolve().is_relative_to(task_dir.resolve()):
            raise ValueError("resolved path escapes task_dir")
    except (ValueError, OSError) as e:
        return Result(
            model_tag=model_tag, task=task, mode=mode,
            accuracy=0.0, extras={}, timing=aggregate_timings([]),
            vram_resident_mb=None, vram_peak_mb=None, host=host,
            run_date=run_date, n_samples=0, language=language,
            error=f"Refused to load dataset outside task_dir: {dataset_path} ({e})",
        )
    if not dataset_path.exists():
        return Result(
            model_tag=model_tag, task=task, mode=mode,
            accuracy=0.0, extras={}, timing=aggregate_timings([]),
            vram_resident_mb=None, vram_peak_mb=None, host=host,
            run_date=run_date, n_samples=0, language=language,
            error=f"Dataset not found: {dataset_path}",
        )
    samples = load_jsonl(dataset_path)

    # Prefer language-specific labels when present (memory_extraction scoring
    # compares model output against ground truth — when the model extracts in
    # the input language but the ground truth stays English, cosine similarity
    # collapses to noise). Fall back to English with an explicit log so the
    # source of any score gap is visible to whoever reads results.
    lang_labels = task_dir / f"labels.{language}.jsonl"
    if language != "en" and lang_labels.exists():
        labels_path = lang_labels
    else:
        labels_path = task_dir / "labels.jsonl"
        if language != "en":
            print(
                f"  info: language={language} labels=labels.jsonl "
                f"(no labels.{language}.jsonl found — scoring against English ground truth)",
                file=sys.stderr, flush=True,
            )
    labels = load_jsonl(labels_path)
    if len(samples) != len(labels):
        return Result(
            model_tag=model_tag, task=task, mode=mode,
            accuracy=0.0, extras={}, timing=aggregate_timings([]),
            vram_resident_mb=None, vram_peak_mb=None, host=host,
            run_date=run_date, n_samples=0, language=language,
            error=f"Sample/label count mismatch: {len(samples)} vs {len(labels)}",
        )

    if task == "room_classification":
        room_lists = load_jsonl(task_dir / "room_lists.jsonl")
        if len(room_lists) != len(samples):
            return Result(
                model_tag=model_tag, task=task, mode=mode,
                accuracy=0.0, extras={}, timing=aggregate_timings([]),
                vram_resident_mb=None, vram_peak_mb=None, host=host,
                run_date=run_date, n_samples=0, language=language,
                error=f"Room-list count mismatch: {len(room_lists)} vs {len(samples)}",
            )
        for s, rl in zip(samples, room_lists):
            s["__rooms__"] = rl["rooms"]

    if n_samples is not None:
        samples = samples[:n_samples]
        labels = labels[:n_samples]

    if (task, mode) in _EMBED_TASKS:
        try:
            _ensure_embed_model(embed_endpoint, model=embed_model)
        except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            return Result(
                model_tag=model_tag, task=task, mode=mode,
                accuracy=0.0, extras={}, timing=aggregate_timings([]),
                vram_resident_mb=None, vram_peak_mb=None, host=host,
                run_date=run_date, n_samples=0, language=language,
                error=f"Embedding model unavailable: {e}",
            )

    try:
        provider_kwargs: dict = {}
        if num_ctx is not None:
            provider_kwargs["num_ctx"] = num_ctx
        provider = get_provider(llm_provider, model=model_tag, endpoint=endpoint, timeout=180, **provider_kwargs)
    except LLMError as e:
        return Result(
            model_tag=model_tag, task=task, mode=mode,
            accuracy=0.0, extras={}, timing=aggregate_timings([]),
            vram_resident_mb=None, vram_peak_mb=None, host=host,
            run_date=run_date, n_samples=0, language=language,
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
                run_date=run_date, n_samples=0, language=language,
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
            score_result = _score_one(task, mode, text, sample, label, embed_endpoint, embed_model=embed_model)
            scores.append(score_result)
        except Exception as e:
            errors.append(f"score error: {e}")

    peak_vram = poller.stop()
    # vram_resident_mb queries Ollama's /api/ps; meaningless for non-Ollama providers.
    if llm_provider == "ollama":
        resident_vram = vram_resident_mb(model_tag, endpoint=endpoint)
    else:
        resident_vram = None

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
        language=language,
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
    parser.add_argument("--llm-provider", default="ollama",
                        choices=["ollama", "openai-compat", "anthropic"],
                        help="LLM provider (default: ollama)")
    parser.add_argument("--embed-endpoint", default=None,
                        help="Endpoint for the embedding model (always Ollama). "
                             "When omitted: defaults to --endpoint if --llm-provider=ollama, "
                             "else http://localhost:11434.")
    parser.add_argument("--language", default="en",
                        help="Dataset language suffix. 'en' loads dataset.jsonl; "
                             "other values load dataset.{lang}.jsonl (e.g. pt-BR, es, zh).")
    parser.add_argument("--embed-model", default=_EMBED_MODEL,
                        help=f"Embedding model for semantic-similarity scoring "
                             f"(memory_extraction, room_classification:open). "
                             f"Default: {_EMBED_MODEL}.")
    parser.add_argument("--num-ctx", type=int, default=4096,
                        help="Ollama context window per request (sent as options.num_ctx). "
                             "Defaults to 4096 so every candidate runs at the same window regardless "
                             "of its Modelfile default — without this, a 32k-default model pre-allocates "
                             "KV cache that a 4k-default model doesn't, and accuracy/latency/VRAM stop "
                             "being comparable. Pass a larger value if a task prompt exceeds 4k tokens.")
    args = parser.parse_args()

    if args.task != "room_classification" and args.mode in ("closed", "open"):
        args.mode = "default"

    # Resolve --embed-endpoint default: it lives on Ollama always, but should
    # follow --endpoint when the LLM provider is Ollama so a remote benchmark
    # run scores against the same host.
    if args.embed_endpoint is None:
        args.embed_endpoint = args.endpoint if args.llm_provider == "ollama" else "http://localhost:11434"

    result = run(
        model_tag=args.model,
        task=args.task,
        mode=args.mode,
        dataset_dir=args.dataset_dir,
        endpoint=args.endpoint,
        warmup=args.warmup,
        n_samples=args.n,
        strip_thinking=not args.no_strip_thinking,
        llm_provider=args.llm_provider,
        embed_endpoint=args.embed_endpoint,
        language=args.language,
        embed_model=args.embed_model,
        num_ctx=args.num_ctx,
    )

    print(json.dumps(_result_to_dict(result), indent=2))
    if result.error:
        sys.exit(1)


def _result_to_dict(r: Result) -> dict:
    return {
        "model_tag": r.model_tag,
        "task": r.task,
        "mode": r.mode,
        "language": r.language,
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
