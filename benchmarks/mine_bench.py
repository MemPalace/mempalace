"""Mining throughput benchmark: per-chunk vs batched upsert, CPU vs GPU.

Compares the legacy per-chunk ``add_drawer`` loop against the batched
``collection.upsert`` path introduced in the "batched upsert + GPU" PR.
Runs both paths on an identical seeded synthetic corpus, reports
wall-clock time + drawers/sec, and prints a markdown table suitable
for pasting into a PR description.

Usage
-----

    # CPU (whatever onnxruntime is installed — CPU if you don't have
    # onnxruntime-gpu):
    uv run python benchmarks/mine_bench.py

    # GPU (NVIDIA):
    uv venv /tmp/gpu && source /tmp/gpu/bin/activate
    uv pip install -e '.[gpu]' 'nvidia-cudnn-cu12>=9,<10' \\
        'nvidia-cuda-runtime-cu12' 'nvidia-cublas-cu12'
    export LD_LIBRARY_PATH=$(python -c "import nvidia.cudnn, os; \\
        print(os.path.dirname(nvidia.cudnn.__file__)+'/lib')"):$LD_LIBRARY_PATH
    MEMPALACE_EMBEDDING_DEVICE=cuda python benchmarks/mine_bench.py

Flags
-----

    --device cpu|cuda|coreml|dml|auto   Override MEMPALACE_EMBEDDING_DEVICE
    --scenarios small,medium,large      Which scenarios to run
    --seed 42                           RNG seed for reproducibility
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import shutil
import string
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path


def build_corpus(dest: Path, n_files: int, paragraphs_per_file: int, seed: int) -> None:
    """Generate ``n_files`` markdown files of random words under ``dest``."""
    rng = random.Random(seed)
    dest.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        paragraphs = []
        for _ in range(paragraphs_per_file):
            words = [
                "".join(rng.choices(string.ascii_lowercase, k=rng.randint(3, 10)))
                for _ in range(12)
            ]
            paragraphs.append(" ".join(words))
        (dest / f"doc_{i:03d}.md").write_text("\n\n".join(paragraphs))
    (dest / "mempalace.yaml").write_text(
        "wing: bench\n"
        "rooms:\n"
        "  - name: general\n"
        "    description: all\n"
        "    keywords: [general]\n"
    )


def _process_file_unbatched(filepath, project_path, collection, wing, rooms, agent, closets_col):
    """Legacy per-chunk upsert path (pre-batching).

    Reproduces the exact loop shape the miner used before this PR so the
    comparison is apples-to-apples; only the upsert granularity differs.
    """
    from mempalace import miner
    from mempalace.palace import (
        build_closet_lines,
        file_already_mined,
        mine_lock,
        purge_file_closets,
        upsert_closet_lines,
    )

    source_file = str(filepath)
    if file_already_mined(collection, source_file, check_mtime=True):
        return 0, "general"
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, "general"
    content = content.strip()
    if len(content) < miner.MIN_CHUNK_SIZE:
        return 0, "general"
    room = miner.detect_room(filepath, content, rooms, project_path)
    chunks = miner.chunk_text(content, source_file)

    with mine_lock(source_file):
        if file_already_mined(collection, source_file, check_mtime=True):
            return 0, room
        try:
            collection.delete(where={"source_file": source_file})
        except Exception:
            pass
        drawers_added = 0
        for chunk in chunks:
            miner.add_drawer(
                collection=collection,
                wing=wing,
                room=room,
                content=chunk["content"],
                source_file=source_file,
                chunk_index=chunk["chunk_index"],
                agent=agent,
            )
            drawers_added += 1
        if closets_col and drawers_added > 0:
            drawer_ids = [
                f"drawer_{wing}_{room}_"
                f"{hashlib.sha256((source_file + str(c['chunk_index'])).encode()).hexdigest()[:24]}"
                for c in chunks
            ]
            closet_lines = build_closet_lines(source_file, drawer_ids, content, wing, room)
            closet_id_base = (
                f"closet_{wing}_{room}_"
                f"{hashlib.sha256(source_file.encode()).hexdigest()[:24]}"
            )
            closet_meta = {
                "wing": wing,
                "room": room,
                "source_file": source_file,
                "drawer_count": drawers_added,
                "filed_at": datetime.now().isoformat(),
                "normalize_version": miner.NORMALIZE_VERSION,
            }
            purge_file_closets(closets_col, source_file)
            upsert_closet_lines(closets_col, closet_id_base, closet_lines, closet_meta)
    return drawers_added, room


def mine_once(project_dir: str, palace_path: str, batched: bool) -> tuple[int, float]:
    """Mine a project dir with either the batched (new) or per-chunk (old) path."""
    from mempalace import miner
    from mempalace.miner import load_config, scan_project
    from mempalace.palace import get_closets_collection, get_collection

    project_path = Path(project_dir).resolve()
    config = load_config(project_dir)
    wing = config["wing"]
    rooms = config.get("rooms", [])
    files = scan_project(project_dir)
    collection = get_collection(palace_path)
    closets = get_closets_collection(palace_path)

    total = 0
    t0 = time.perf_counter()
    for filepath in files:
        if batched:
            drawers, _ = miner.process_file(
                filepath=filepath,
                project_path=project_path,
                collection=collection,
                wing=wing,
                rooms=rooms,
                agent="bench",
                dry_run=False,
                closets_col=closets,
            )
        else:
            drawers, _ = _process_file_unbatched(
                filepath, project_path, collection, wing, rooms, "bench", closets
            )
        total += drawers
    return total, time.perf_counter() - t0


def _reset_backend_caches() -> None:
    """Drop the in-process client cache so each run pays cold-open cost equally."""
    from mempalace.palace import _DEFAULT_BACKEND

    _DEFAULT_BACKEND._clients.clear()
    _DEFAULT_BACKEND._freshness.clear()


def run_scenario(label: str, n_files: int, paragraphs_per_file: int, seed: int) -> dict:
    """Run one scenario under both code paths and return a result dict."""
    print(f"\n=== {label}: {n_files} files × {paragraphs_per_file} paragraphs ===")
    results = {}
    for mode in ("unbatched", "batched"):
        tmp = Path(tempfile.mkdtemp(prefix=f"mp_{mode}_"))
        try:
            proj = tmp / "proj"
            palace = tmp / "palace"
            build_corpus(proj, n_files, paragraphs_per_file, seed=seed)
            _reset_backend_caches()
            drawers, dt = mine_once(str(proj), str(palace), batched=(mode == "batched"))
            rate = drawers / dt if dt > 0 else 0.0
            results[mode] = (drawers, dt, rate)
            print(f"  {mode:10} {drawers:5} drawers in {dt:6.2f}s  →  {rate:7.1f} drawers/sec")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    _, t_u, r_u = results["unbatched"]
    d_b, t_b, r_b = results["batched"]
    speedup = t_u / t_b if t_b > 0 else 0.0
    print(f"  speedup:   {speedup:.2f}× ({t_u:.2f}s → {t_b:.2f}s)")
    return {
        "label": label,
        "n_files": n_files,
        "paragraphs": paragraphs_per_file,
        "drawers": d_b,
        "unbatched_time": t_u,
        "unbatched_rate": r_u,
        "batched_time": t_b,
        "batched_rate": r_b,
        "speedup": speedup,
    }


SCENARIOS = {
    "small":  ("Small files (~50 paragraphs)",  10, 50),
    "medium": ("Medium files (~200 paragraphs)", 20, 200),
    "large":  ("Large files (~500 paragraphs)",  10, 500),
}


def _env_summary(device_label: str) -> list[str]:
    """Short hardware + version lines included with the printed table."""
    import platform

    try:
        import chromadb

        chromadb_v = chromadb.__version__
    except Exception:
        chromadb_v = "?"
    try:
        import onnxruntime as ort

        ort_v = ort.__version__
        providers = ",".join(p.replace("ExecutionProvider", "") for p in ort.get_available_providers())
    except Exception:
        ort_v = "?"
        providers = "?"

    return [
        f"device: **{device_label}** (onnxruntime {ort_v}, providers={providers})",
        f"chromadb {chromadb_v} · python {sys.version.split()[0]} · {platform.platform()}",
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--device",
        default=None,
        help="Override MEMPALACE_EMBEDDING_DEVICE (cpu|cuda|coreml|dml|auto)",
    )
    parser.add_argument(
        "--scenarios",
        default="small,medium,large",
        help="Comma-separated scenario names (default: all)",
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    if args.device:
        os.environ["MEMPALACE_EMBEDDING_DEVICE"] = args.device

    from mempalace.embedding import describe_device, get_embedding_function

    device_label = describe_device()
    print(f"Warming up ONNX model on device={device_label}...")
    ef = get_embedding_function()
    ef(["warmup sentence one", "warmup sentence two"])

    picked = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    results = []
    for key in picked:
        if key not in SCENARIOS:
            print(f"Unknown scenario {key!r}; choices: {sorted(SCENARIOS)}", file=sys.stderr)
            sys.exit(2)
        label, n_files, paras = SCENARIOS[key]
        results.append(run_scenario(label, n_files, paras, args.seed))

    print("\n\n## Mining benchmark\n")
    for line in _env_summary(device_label):
        print(line + "  ")
    print()
    print("| Scenario | Files | Drawers | Per-chunk (old) | Batched (new) | Speedup |")
    print("| --- | ---: | ---: | ---: | ---: | ---: |")
    for r in results:
        print(
            f"| {r['label']} | {r['n_files']} | {r['drawers']} | "
            f"{r['unbatched_time']:.2f}s · {r['unbatched_rate']:.0f} drw/s | "
            f"{r['batched_time']:.2f}s · {r['batched_rate']:.0f} drw/s | "
            f"**{r['speedup']:.2f}×** |"
        )


if __name__ == "__main__":
    main()
