#!/usr/bin/env python3
"""chunk_strategy_ablation.py — A/B/C three chunking strategies on mempalace's own corpus.

Hypothesis (per TDS "Your Chunks Failed Your RAG in Production" 2026 and
dev.to/ayanarshad02 2026): fixed-size paragraph-aware chunking is
strategy-agnostic; matching strategy to file type (heading-aware for
markdown, AST-aware for Python) should improve retrieval.

This script implements three strategies:

    A: current paragraph-aware fixed-size (the baseline already in main)
    B: A + heading-aware splitting for ``.md`` / ``.markdown`` / ``.rst``
    C: B + AST-aware splitting for ``.py``

It mines the mempalace package directory three times into temp palaces
(one per strategy), then runs a hand-curated probe set and reports
per-query Mean Reciprocal Rank, Recall@5, and chunk count.

Usage::

    python scripts/chunk_strategy_ablation.py
    python scripts/chunk_strategy_ablation.py --corpus /path/to/dir
    python scripts/chunk_strategy_ablation.py --probes /path/to/probes.yaml

The script uses monkey-patching on ``mempalace.miner.chunk_text`` to
swap in alternate strategies without modifying production code, so the
mine logic, embedding pipeline, and search path are otherwise identical
across the three runs. The output is intended to be pasted into an
upstream RFC issue or a fork-side ADR.
"""
from __future__ import annotations

import ast
import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, List, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── Chunk strategies ───────────────────────────────────────────────────


def _chunk_paragraph_aware(
    content: str,
    source_file: str,
    chunk_size: int = 800,
    chunk_overlap: int = 100,
    min_chunk_size: int = 50,
) -> list:
    """Strategy A — current behavior (mempalace.miner.chunk_text on main)."""
    content = content.strip()
    if not content:
        return []
    chunks = []
    start = 0
    chunk_index = 0
    while start < len(content):
        end = min(start + chunk_size, len(content))
        if end < len(content):
            newline_pos = content.rfind("\n\n", start, end)
            if newline_pos > start + chunk_size // 2:
                end = newline_pos
            else:
                newline_pos = content.rfind("\n", start, end)
                if newline_pos > start + chunk_size // 2:
                    end = newline_pos
        chunk = content[start:end].strip()
        if len(chunk) >= min_chunk_size:
            chunks.append({"content": chunk, "chunk_index": chunk_index})
            chunk_index += 1
        start = end - chunk_overlap if end < len(content) else end
    return chunks


_HEADING_RE = re.compile(r"(?m)^(#{1,6})\s+(.+)$")


def _split_markdown_by_heading(content: str) -> list:
    """Return ``[(heading_text, section_content), ...]``.

    Section content includes the heading line itself so chunks remain
    self-describing (a chunk that says "Configuration" but has no
    heading marker would be hard to interpret in isolation).
    """
    matches = list(_HEADING_RE.finditer(content))
    if not matches:
        return [("", content)]
    sections = []
    # Anything before the first heading goes in a "preamble" section.
    if matches[0].start() > 0:
        sections.append(("", content[: matches[0].start()].strip()))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        sections.append((m.group(2).strip(), content[start:end].strip()))
    return [(h, c) for h, c in sections if c]


def _chunk_markdown_heading_aware(
    content: str,
    source_file: str,
    chunk_size: int = 800,
    chunk_overlap: int = 100,
    min_chunk_size: int = 50,
) -> list:
    """Strategy B — split markdown at heading boundaries; oversize sections
    fall back to paragraph-aware chunking.
    """
    content = content.strip()
    if not content:
        return []
    sections = _split_markdown_by_heading(content)
    chunks = []
    chunk_index = 0
    for heading, section in sections:
        if len(section) <= chunk_size and len(section) >= min_chunk_size:
            chunks.append({"content": section, "chunk_index": chunk_index})
            chunk_index += 1
        elif len(section) > chunk_size:
            # Oversized section — fall back to paragraph-aware splitting,
            # but prepend the heading to the first sub-chunk so the
            # heading context is preserved in the embedding.
            sub_chunks = _chunk_paragraph_aware(
                section, source_file, chunk_size, chunk_overlap, min_chunk_size
            )
            for sc in sub_chunks:
                chunks.append({"content": sc["content"], "chunk_index": chunk_index})
                chunk_index += 1
    return chunks


def _chunk_python_ast(
    content: str,
    source_file: str,
    chunk_size: int = 800,
    chunk_overlap: int = 100,
    min_chunk_size: int = 50,
) -> list:
    """Strategy C — split Python at function/class boundaries via ``ast``.

    Each top-level FunctionDef / ClassDef / AsyncFunctionDef becomes one
    chunk (split if over chunk_size). Module-level prose between
    definitions (imports, constants, comments) is kept with the
    *preceding* span so the chunk has context. If the file fails to
    parse, falls back to paragraph-aware.
    """
    content_stripped = content.strip()
    if not content_stripped:
        return []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return _chunk_paragraph_aware(
            content, source_file, chunk_size, chunk_overlap, min_chunk_size
        )

    lines = content.splitlines(keepends=True)
    cuts = sorted(
        {n.lineno - 1 for n in tree.body if isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )}
    )
    if not cuts:
        return _chunk_paragraph_aware(
            content, source_file, chunk_size, chunk_overlap, min_chunk_size
        )
    # Always include the preamble (imports, module docstring) as the
    # first cut.
    if cuts[0] != 0:
        cuts = [0] + cuts
    spans = []
    for i, start in enumerate(cuts):
        end = cuts[i + 1] if i + 1 < len(cuts) else len(lines)
        spans.append("".join(lines[start:end]).strip())

    chunks = []
    chunk_index = 0
    for span in spans:
        if not span:
            continue
        if len(span) <= chunk_size and len(span) >= min_chunk_size:
            chunks.append({"content": span, "chunk_index": chunk_index})
            chunk_index += 1
        elif len(span) > chunk_size:
            for sc in _chunk_paragraph_aware(
                span, source_file, chunk_size, chunk_overlap, min_chunk_size
            ):
                chunks.append({"content": sc["content"], "chunk_index": chunk_index})
                chunk_index += 1
    return chunks


# ── Strategy dispatch ──────────────────────────────────────────────────


def _make_strategy(name: str, chunk_size: int) -> Callable:
    """Build a strategy closure that respects ``chunk_size``.

    chunk_size matters because all-MiniLM-L6-v2 silently truncates at
    256 tokens (~512 chars); see issue #390. Running each ablation at
    both 400 and 800 chars isolates the strategy effect from the
    truncation-fit effect.
    """
    overlap = max(0, chunk_size // 8)  # 12.5% — matches the 800/100 ratio in the default
    min_size = 50

    def _A(content, source_file):
        return _chunk_paragraph_aware(content, source_file, chunk_size, overlap, min_size)

    def _B(content, source_file):
        if source_file.lower().endswith((".md", ".markdown", ".rst")):
            return _chunk_markdown_heading_aware(content, source_file, chunk_size, overlap, min_size)
        return _chunk_paragraph_aware(content, source_file, chunk_size, overlap, min_size)

    def _C(content, source_file):
        if source_file.lower().endswith((".md", ".markdown", ".rst")):
            return _chunk_markdown_heading_aware(content, source_file, chunk_size, overlap, min_size)
        if source_file.lower().endswith(".py"):
            return _chunk_python_ast(content, source_file, chunk_size, overlap, min_size)
        return _chunk_paragraph_aware(content, source_file, chunk_size, overlap, min_size)

    return {"A": _A, "B": _B, "C": _C}[name]


def build_strategies(chunk_size: int) -> List[Tuple[str, Callable]]:
    return [
        (f"A_paragraph_aware__cs{chunk_size}", _make_strategy("A", chunk_size)),
        (f"B_heading_aware_md__cs{chunk_size}", _make_strategy("B", chunk_size)),
        (f"C_plus_ast_python__cs{chunk_size}", _make_strategy("C", chunk_size)),
    ]


# ── Probe set ──────────────────────────────────────────────────────────
# Each probe: (query, expected_source_file_basename, why).
# Hand-curated against the mempalace package layout. The "expected" file
# is the one a developer would point at to answer the query.


PROBES: List[Tuple[str, str, str]] = [
    ("How does the stop hook save diary entries?", "hooks_cli.py",
     "primary save path lives in hooks_cli; mcp_server's tool_diary_write is the API surface"),
    ("ChromaDB HNSW segment quarantine for stale indexes", "backends/chroma.py",
     "quarantine_stale_hnsw lives in the chroma backend"),
    ("BM25 tokenization with None-document safety", "searcher.py",
     "_tokenize and _bm25_scores are searcher-internal"),
    ("How is a wing name normalized — hyphens vs underscores?", "config.py",
     "normalize_wing_name lives at module top of config.py"),
    ("Validate chunk_size to prevent the infinite loop hazard", "miner.py",
     "chunk_text guard or its analog"),
    ("Pre-PreCompact transcript auto-mining via daemon /mine endpoint", "hooks_cli.py",
     "_post_daemon_mine lives in hooks_cli; daemon side is palace-daemon repo"),
    ("Topic-routing branch routing checkpoint diary writes to a separate collection", "palace.py",
     "_CHECKPOINT_TOPICS / get_session_recovery_collection (note: removed in fork; baseline corpus has it)"),
    ("Knowledge-graph entity validation that allows commas and parentheses", "config.py",
     "sanitize_kg_value"),
    ("Closet-boost rank constants used to re-order vector hits", "searcher.py",
     "CLOSET_RANK_BOOSTS and the rank loop"),
    ("Hybrid rank combining BM25 with cosine similarity", "searcher.py",
     "_hybrid_rank"),
    ("Wing assignment from Claude Code transcript path regex", "convo_miner.py",
     "_wing_from_transcript_path"),
    ("Detect HNSW capacity divergence and quarantine the segment", "backends/chroma.py",
     "quarantine_stale_hnsw + cold-start gate"),
    ("Spellcheck user prose before writing to drawers", "spellcheck.py",
     "spellcheck_user_text is the entry point"),
    ("Slack JSON export normalization with positional speaker roles", "normalize.py",
     "_try_slack_json + provenance footer"),
    ("Build the where filter for wing/room scoping in chromadb queries", "searcher.py",
     "build_where_filter — small but probe a known target"),
]


# ── Mine + query runner ────────────────────────────────────────────────


def _build_palace(corpus_dir: Path, palace_dir: Path, strategy_fn: Callable) -> int:
    """Mine ``corpus_dir`` into ``palace_dir`` using ``strategy_fn``.
    Returns the total drawer count after mining.
    """
    from mempalace import miner as miner_mod
    from mempalace.config import MempalaceConfig

    # Monkey-patch chunk_text for this run only.
    original = miner_mod.chunk_text
    miner_mod.chunk_text = strategy_fn
    try:
        # mempalace.miner.mine() expects a wing argument and reads
        # MempalaceConfig — set MEMPALACE_PALACE_PATH so it points at our
        # temp palace.
        import os
        os.environ["MEMPALACE_PALACE_PATH"] = str(palace_dir)
        # Force a fresh MempalaceConfig read.
        cfg = MempalaceConfig()
        assert cfg.palace_path == str(palace_dir)
        files = miner_mod.scan_project(str(corpus_dir))
        miner_mod.mine(
            project_dir=str(corpus_dir),
            palace_path=str(palace_dir),
            files=files,
        )
        from mempalace.palace import get_collection
        col = get_collection(str(palace_dir))
        return col.count()
    finally:
        miner_mod.chunk_text = original


def _query_palace(palace_dir: Path, query: str, n_results: int = 10) -> list:
    """Return list of (rank, source_file_basename, similarity) tuples."""
    import os
    os.environ["MEMPALACE_PALACE_PATH"] = str(palace_dir)
    from mempalace.searcher import search_memories
    result = search_memories(query, str(palace_dir), n_results=n_results)
    out = []
    for rank, hit in enumerate(result.get("results") or [], start=1):
        sf = hit.get("source_file") or ""
        out.append((rank, Path(sf).name if sf else "?", hit.get("similarity")))
    return out


def _mrr_for_probe(hits: list, expected: str) -> Tuple[float, int | None]:
    """Mean Reciprocal Rank contribution for a single probe.
    Returns (1/rank, rank) where rank is 1-indexed of the first hit
    whose source_file basename equals ``expected``; (0.0, None) if no
    match in top-K.

    Match is exact basename — e.g. an expected of ``backends/chroma.py``
    matches a hit with source_file basename ``chroma.py``.
    """
    target_basename = Path(expected).name
    for rank, basename, _sim in hits:
        if basename == target_basename:
            return (1.0 / rank, rank)
    return (0.0, None)


def _recall_at_k(hits: list, expected: str, k: int) -> int:
    target_basename = Path(expected).name
    return int(any(b == target_basename for _r, b, _ in hits[:k]))


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Three-way A/B/C chunking strategy ablation."
    )
    parser.add_argument(
        "--corpus",
        default=str(_REPO_ROOT / "mempalace"),
        help="Directory to mine (default: this repo's mempalace package).",
    )
    parser.add_argument(
        "--n-results",
        type=int,
        default=10,
        help="Top-K to retrieve per probe (default: 10).",
    )
    parser.add_argument(
        "--keep-palaces",
        action="store_true",
        help="Don't delete the temp palaces after the run.",
    )
    parser.add_argument(
        "--chunk-sizes",
        default="400,800",
        help=(
            "Comma-separated chunk_size values to sweep. 400 is within the "
            "all-MiniLM-L6-v2 256-token window (issue #390); 800 is "
            "mempalace's current default. Defaults to '400,800' so the "
            "report covers both."
        ),
    )
    parser.add_argument(
        "--out",
        default="",
        help="Write JSON results to this path (in addition to stdout).",
    )
    args = parser.parse_args(argv)

    corpus = Path(args.corpus).resolve()
    if not corpus.is_dir():
        print(f"corpus not found: {corpus}", file=sys.stderr)
        return 2

    chunk_sizes = [int(s) for s in args.chunk_sizes.split(",") if s.strip()]
    strategies: List[Tuple[str, Callable]] = []
    for cs in chunk_sizes:
        strategies.extend(build_strategies(cs))

    print(f"Corpus:    {corpus}")
    print(f"Probes:    {len(PROBES)}")
    print(f"Top-K:     {args.n_results}")
    print(f"chunk_sizes: {chunk_sizes}")
    print(f"Strategies: {', '.join(name for name, _ in strategies)}")
    print()

    workdir = Path(tempfile.mkdtemp(prefix="chunk_ablation_"))
    print(f"Workdir:   {workdir}")
    print()

    summary = {
        "corpus": str(corpus),
        "n_probes": len(PROBES),
        "n_results": args.n_results,
        "chunk_sizes": chunk_sizes,
        "strategies": {},
    }

    for name, fn in strategies:
        palace = workdir / name
        palace.mkdir()
        print(f"=== Strategy: {name} ===")
        t0 = time.time()
        drawer_count = _build_palace(corpus, palace, fn)
        mine_secs = time.time() - t0
        print(f"  drawers: {drawer_count}  mine_secs: {mine_secs:.1f}")

        rrs = []
        recall_at_5 = 0
        recall_at_10 = 0
        per_probe = []
        for query, expected, _why in PROBES:
            hits = _query_palace(palace, query, n_results=args.n_results)
            rr, rank = _mrr_for_probe(hits, expected)
            r5 = _recall_at_k(hits, expected, 5)
            r10 = _recall_at_k(hits, expected, 10)
            rrs.append(rr)
            recall_at_5 += r5
            recall_at_10 += r10
            per_probe.append({
                "query": query,
                "expected": expected,
                "rank": rank,
                "rr": round(rr, 4),
                "top3": [b for _r, b, _ in hits[:3]],
            })
        mrr = sum(rrs) / len(rrs)
        r5_pct = 100 * recall_at_5 / len(PROBES)
        r10_pct = 100 * recall_at_10 / len(PROBES)
        print(f"  MRR: {mrr:.3f}  Recall@5: {r5_pct:.1f}%  Recall@10: {r10_pct:.1f}%")
        print()
        summary["strategies"][name] = {
            "drawer_count": drawer_count,
            "mine_secs": round(mine_secs, 2),
            "mrr": round(mrr, 4),
            "recall_at_5_pct": round(r5_pct, 2),
            "recall_at_10_pct": round(r10_pct, 2),
            "probes": per_probe,
        }

    # Per-chunk_size summary tables.
    for cs in chunk_sizes:
        cs_strategies = [(n, fn) for (n, fn) in strategies if f"__cs{cs}" in n]
        col_w = 22
        print("=" * (24 + col_w * len(cs_strategies)))
        header = f"chunk_size={cs:<14}"
        for name, _ in cs_strategies:
            short = name.split("__")[0]
            header += f"{short:>{col_w}}"
        print(header)
        print("-" * (24 + col_w * len(cs_strategies)))
        for metric in ("drawer_count", "mrr", "recall_at_5_pct", "recall_at_10_pct"):
            line = f"  {metric:<22}"
            for name, _ in cs_strategies:
                v = summary["strategies"][name][metric]
                line += f"{v:>{col_w}}"
            print(line)
        print("=" * (24 + col_w * len(cs_strategies)))
        print()

    # Per-probe rank deltas across all (strategy, chunk_size) cells.
    print("Per-probe rank table ('-' = not in top-K).")
    print("Columns:", ", ".join(name.replace("__", " ") for name, _ in strategies))
    for i, (q, expected, _) in enumerate(PROBES):
        ranks = []
        for name, _ in strategies:
            r = summary["strategies"][name]["probes"][i]["rank"]
            ranks.append(str(r) if r is not None else "-")
        print(f"  [{','.join(f'{x:>3}' for x in ranks)}]  expects={Path(expected).name:<22} q={q[:55]}")

    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2))
        print(f"\nFull results: {args.out}")

    if not args.keep_palaces:
        shutil.rmtree(workdir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
