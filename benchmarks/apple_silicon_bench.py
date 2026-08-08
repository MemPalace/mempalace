#!/usr/bin/env python3
"""
Apple Silicon embedding backend benchmark.

Compares embedding throughput across:
  - onnxruntime with default providers (current mempalace behavior)
  - onnxruntime CPU-only
  - onnxruntime CoreML-only (if available)
  - sentence-transformers CPU
  - sentence-transformers MPS (Metal Performance Shaders, Apple GPU)

Usage:
    python apple_silicon_bench.py                       # default 500 chunks
    python apple_silicon_bench.py --n-chunks 2000       # more samples
    python apple_silicon_bench.py --batch-size 64       # try batch sizes
    python apple_silicon_bench.py --source ~/.claude/projects  # use real data

Output: markdown table ready to paste into PR description.
"""

import argparse
import json
import platform
import random
import statistics
import time
from pathlib import Path
from typing import Callable, List

# ---- sample data --------------------------------------------------------
# Default: generate synthetic text chunks that look like conversation exchanges.
# With --source: sample real JSONL chunks from a user's ~/.claude/projects.


def synthetic_chunks(n: int, seed: int = 42) -> List[str]:
    """Generate realistic-length text chunks (100-400 chars, like real convo pairs)."""
    random.seed(seed)
    pool = [
        "The quick brown fox jumps over the lazy dog near the riverbank at sunset.",
        "When we debugged the auth token refresh flow last sprint, the race condition turned out to be in the middleware chain.",
        "Postgres versus SQLite for this workload — concurrent writes, dataset growing past 10GB, want to avoid Mongo.",
        "Here's the patch I applied to fix the memory leak: the reference cycle was between the event emitter and the state manager.",
        "Let's switch the embedding backend from default ONNX to sentence-transformers with MPS device for Apple Silicon machines.",
    ]
    out = []
    for i in range(n):
        # Simulate 2-5 sentences per chunk
        k = random.randint(2, 5)
        out.append(" ".join(random.choices(pool, k=k)) + f" [chunk_{i}]")
    return out


def real_chunks(source_dir: Path, n: int, seed: int = 42) -> List[str]:
    """Sample n chunks of conversation text from JSONL files."""
    random.seed(seed)
    jsonls = list(source_dir.rglob("*.jsonl"))
    random.shuffle(jsonls)
    chunks = []
    for path in jsonls:
        if len(chunks) >= n:
            break
        try:
            with path.open() as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        msg = entry.get("message", {})
                        if not isinstance(msg, dict):
                            continue
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            text = " ".join(
                                c.get("text", "") for c in content if isinstance(c, dict)
                            )
                        else:
                            text = str(content)
                        text = text.strip()
                        if 50 < len(text) < 2000:
                            chunks.append(text[:1500])
                            if len(chunks) >= n:
                                break
                    except Exception:
                        continue
        except Exception:
            continue
    return chunks[:n]


# ---- benchmark runners --------------------------------------------------


def time_it(fn: Callable[[], None], warmup: int = 1, runs: int = 3) -> dict:
    """Run fn() `runs` times after `warmup` warmups. Return timing stats."""
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return {
        "mean": statistics.mean(samples),
        "min": min(samples),
        "max": max(samples),
        "std": statistics.stdev(samples) if len(samples) > 1 else 0,
    }


def bench_onnx_default(chunks: List[str], batch_size: int) -> dict:
    """ChromaDB's ONNXMiniLM_L6_V2 with default providers (current mempalace)."""
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

    ef = ONNXMiniLM_L6_V2()
    # warmup downloads model + loads session
    _ = ef(chunks[:2])

    def fn():
        for i in range(0, len(chunks), batch_size):
            _ = ef(chunks[i : i + batch_size])

    return time_it(fn)


def bench_onnx_cpu_only(chunks: List[str], batch_size: int) -> dict:
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

    ef = ONNXMiniLM_L6_V2(preferred_providers=["CPUExecutionProvider"])
    _ = ef(chunks[:2])

    def fn():
        for i in range(0, len(chunks), batch_size):
            _ = ef(chunks[i : i + batch_size])

    return time_it(fn)


def bench_onnx_coreml(chunks: List[str], batch_size: int) -> dict:
    """Explicitly force CoreML — expected to raise or silently fall back."""
    try:
        import onnxruntime

        if "CoreMLExecutionProvider" not in onnxruntime.get_available_providers():
            return {"skipped": "CoreML provider not available"}
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import (
            ONNXMiniLM_L6_V2,
        )

        ef = ONNXMiniLM_L6_V2(
            preferred_providers=["CoreMLExecutionProvider", "CPUExecutionProvider"]
        )
        _ = ef(chunks[:2])

        def fn():
            for i in range(0, len(chunks), batch_size):
                _ = ef(chunks[i : i + batch_size])

        return time_it(fn)
    except Exception as e:
        return {"error": str(e)[:120]}


def bench_sentence_transformers(
    chunks: List[str], batch_size: int, device: str
) -> dict:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return {"skipped": "sentence-transformers not installed"}

    try:
        model = SentenceTransformer("all-MiniLM-L6-v2", device=device)
    except Exception as e:
        return {"error": f"load failed on {device}: {str(e)[:100]}"}

    # warmup
    _ = model.encode(chunks[:2], batch_size=batch_size, show_progress_bar=False)

    def fn():
        _ = model.encode(chunks, batch_size=batch_size, show_progress_bar=False)

    return time_it(fn)


# ---- main ---------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-chunks", type=int, default=500)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--source", type=str, default=None)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--skip",
        type=str,
        default="",
        help="Comma-separated backend names to skip (e.g. 'onnx_coreml,st_cuda')",
    )
    args = parser.parse_args()
    skip_set = {s.strip() for s in args.skip.split(",") if s.strip()}

    print(f"\n{'='*70}")
    print(f"  Apple Silicon Embedding Benchmark")
    print(f"{'='*70}")
    print(f"  Machine:    {platform.machine()} / {platform.processor()}")
    print(f"  System:     {platform.system()} {platform.release()}")
    print(f"  N chunks:   {args.n_chunks}")
    print(f"  Batch size: {args.batch_size}")
    print(f"  Runs/warm:  {args.runs}/1")
    print(f"{'-'*70}\n")

    if args.source:
        print(f"Loading real chunks from {args.source}...")
        chunks = real_chunks(Path(args.source).expanduser(), args.n_chunks)
        print(f"  loaded {len(chunks)} chunks")
    else:
        print("Generating synthetic chunks...")
        chunks = synthetic_chunks(args.n_chunks)
    if not chunks:
        print("ERROR: no chunks to benchmark")
        return

    backends = [
        ("onnx_default",       lambda: bench_onnx_default(chunks, args.batch_size)),
        ("onnx_cpu_only",      lambda: bench_onnx_cpu_only(chunks, args.batch_size)),
        ("onnx_coreml",        lambda: bench_onnx_coreml(chunks, args.batch_size)),
        ("st_cpu",             lambda: bench_sentence_transformers(chunks, args.batch_size, "cpu")),
        ("st_mps",             lambda: bench_sentence_transformers(chunks, args.batch_size, "mps")),
    ]

    results = {}
    for name, runner in backends:
        if name in skip_set:
            print(f"Running: {name:20}... SKIP (--skip)")
            results[name] = {"skipped": "user --skip"}
            continue
        print(f"Running: {name:20}...", end=" ", flush=True)
        try:
            results[name] = runner()
            r = results[name]
            if "skipped" in r:
                print(f"SKIP ({r['skipped']})")
            elif "error" in r:
                print(f"ERROR ({r['error']})")
            else:
                rate = len(chunks) / r["mean"]
                print(
                    f"mean {r['mean']:.2f}s  ({rate:.0f} chunks/s)  std {r['std']:.2f}"
                )
        except Exception as e:
            results[name] = {"error": str(e)[:120]}
            print(f"CRASH ({str(e)[:80]})")

    # --- markdown table ---
    print(f"\n{'='*70}")
    print("  Results (markdown, ready for PR)")
    print(f"{'='*70}\n")
    print(f"| Backend | Mean (s) | Rate (chunks/s) | vs baseline |")
    print(f"|---|---|---|---|")
    baseline = results.get("onnx_default", {}).get("mean")
    for name, r in results.items():
        if "skipped" in r or "error" in r:
            print(f"| {name} | — | — | {r.get('skipped') or r.get('error')} |")
            continue
        rate = len(chunks) / r["mean"]
        if baseline:
            speedup = baseline / r["mean"]
            print(
                f"| {name} | {r['mean']:.2f} | {rate:.0f} | {speedup:.2f}x |"
            )
        else:
            print(f"| {name} | {r['mean']:.2f} | {rate:.0f} | — |")


if __name__ == "__main__":
    main()
