"""Recall benchmark for sub-window embedding of long drawers (#390).

Runs offline with the local default embedder (MiniLM, 256 tokens) on the
deterministic corpus in ``subwindow_data.py``: facts planted in the **tail**
of long drawers (past token 300, which today's drawer vector never reads)
and in the **head**, each asked once as a low-overlap paraphrase and once as
a lexical control. Every query runs four ways:

* ``vector`` — drawer vectors only (the vector half of search), flag off;
* ``vector+windows`` — the same with sub-window max-sim folding;
* ``hybrid`` — ``search_memories`` as shipped (vector + BM25), flag off;
* ``hybrid+windows`` — ``search_memories`` with ``embedding_subwindows`` on.

A second palace with the same head facts but fact-free long drawers in place
of the tail facts isolates what windows alone do to head queries.

    uv run pytest tests/benchmarks/test_subwindow_recall.py -m benchmark -s

Prints a table and records metrics under ``subwindow_recall`` (see
``--bench-report``). Asserts only that the vector path gains on tail queries;
the rest is reported, not gated.
"""

import os
import time

import pytest

from tests.benchmarks.report import record_metric
from tests.benchmarks.subwindow_data import build_corpus, head_control

pytestmark = [pytest.mark.benchmark, pytest.mark.slow]

K = 10
SIZES = {
    "small": dict(n_tail=60, n_head=60, n_short=120),
    "medium": dict(n_tail=150, n_head=150, n_short=200),
    "large": dict(n_tail=400, n_head=400, n_short=1200),
    "stress": dict(n_tail=1000, n_head=1000, n_short=4000),
}


@pytest.fixture(autouse=True)
def _stable_embedding_function_for_tests():
    """Override the suite-wide stable embedder: this benchmark needs real MiniLM."""
    yield


@pytest.fixture
def minilm(monkeypatch):
    from mempalace.embedding import get_embedding_function

    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "minilm")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_DEVICE", "cpu")
    ef = get_embedding_function(device="cpu", model="minilm")
    try:
        ef(["probe"])
    except Exception as exc:  # pragma: no cover - offline without the model
        pytest.skip(f"MiniLM unavailable: {exc}")
    from mempalace.subwindows import window_tokenizer

    return ef, window_tokenizer(ef, "minilm")


def _dir_bytes(path):
    return sum(
        os.path.getsize(os.path.join(root, name))
        for root, _, names in os.walk(path)
        for name in names
    )


def _fill(palace_path, corpus):
    from mempalace.palace import get_collection

    col = get_collection(palace_path, create=True)
    drawers = corpus.drawers
    for start in range(0, len(drawers), 200):
        batch = drawers[start : start + 200]
        col.upsert(
            ids=[d.drawer_id for d in batch],
            documents=[d.text for d in batch],
            metadatas=[
                {"wing": "bench", "room": d.position, "source_file": f"/bench/{d.drawer_id}.txt"}
                for d in batch
            ],
        )
    return col


def _vector_ranking(col, palace_path, query, folded):
    from mempalace.backends.embedding_wrapper import _embed_texts
    from mempalace.searcher import _fold_subwindow_hits, _metric_for_collection

    pool = K * 4
    vectors = _embed_texts([query], query=True)
    results = col.query(
        query_embeddings=vectors, n_results=pool, include=["documents", "metadatas", "distances"]
    )
    if folded:
        results = _fold_subwindow_hits(
            col,
            results,
            palace_path=palace_path,
            where={},
            pool_size=pool,
            metric=_metric_for_collection(col),
            query_vectors=vectors,
        )
    return list(results["ids"][0])[:K]


def _hybrid_ranking(palace_path, query):
    from mempalace.searcher import search_memories

    return [h["drawer_id"] for h in search_memories(query, palace_path, n_results=K)["results"]]


def _score(ranked_lists, queries):
    groups = {}
    for query, ranked in zip(queries, ranked_lists):
        rank = ranked.index(query.target) + 1 if query.target in ranked else None
        for key in ((query.position, query.style), ("all", "all")):
            groups.setdefault(key, []).append(rank)
    out = {}
    for key, ranks in groups.items():
        n = len(ranks)
        out["/".join(key)] = {
            "n": n,
            "R@1": sum(1 for r in ranks if r and r <= 1) / n,
            "R@5": sum(1 for r in ranks if r and r <= 5) / n,
            "R@10": sum(1 for r in ranks if r and r <= 10) / n,
            "MRR@10": sum(1 / r for r in ranks if r) / n,
        }
    return out


def _run(palace_path, corpus, monkeypatch):
    from mempalace import subwindows

    col = _fill(palace_path, corpus)
    queries = corpus.queries
    monkeypatch.setenv("MEMPALACE_EMBEDDING_SUBWINDOWS", "0")
    before = {
        "vector": _score(
            [_vector_ranking(col, palace_path, q.text, False) for q in queries], queries
        ),
        "hybrid": _score([_hybrid_ranking(palace_path, q.text) for q in queries], queries),
    }
    bytes_before = _dir_bytes(palace_path)
    t0 = time.perf_counter()
    stats = subwindows.build_windows(palace_path)
    build_seconds = time.perf_counter() - t0
    monkeypatch.setenv("MEMPALACE_EMBEDDING_SUBWINDOWS", "1")
    after = {
        "vector+windows": _score(
            [_vector_ranking(col, palace_path, q.text, True) for q in queries], queries
        ),
        "hybrid+windows": _score([_hybrid_ranking(palace_path, q.text) for q in queries], queries),
    }
    t0 = time.perf_counter()
    for q in queries[:50]:
        _hybrid_ranking(palace_path, q.text)
    on_latency = (time.perf_counter() - t0) / min(50, len(queries))
    monkeypatch.setenv("MEMPALACE_EMBEDDING_SUBWINDOWS", "0")
    t0 = time.perf_counter()
    for q in queries[:50]:
        _hybrid_ranking(palace_path, q.text)
    off_latency = (time.perf_counter() - t0) / min(50, len(queries))
    cost = {
        "drawers": stats["drawers"],
        "long_drawers": stats["long_drawers"],
        "window_rows": stats["window_rows"],
        "vectors_ratio": (stats["drawers"] + stats["window_rows"]) / stats["drawers"],
        "build_seconds": round(build_seconds, 2),
        "palace_bytes_before": bytes_before,
        "palace_bytes_after": _dir_bytes(palace_path),
        "search_ms_off": round(off_latency * 1000, 1),
        "search_ms_on": round(on_latency * 1000, 1),
    }
    return {**before, **after}, cost


def _print(title, scores, cost):
    print(f"\n== {title}")
    modes = ["vector", "vector+windows", "hybrid", "hybrid+windows"]
    for group in sorted(next(iter(scores.values()))):
        n = scores["vector"][group]["n"]
        cells = "  ".join(
            f"{m}: R@1 {scores[m][group]['R@1']:.3f} R@10 {scores[m][group]['R@10']:.3f} "
            f"MRR {scores[m][group]['MRR@10']:.3f}"
            for m in modes
        )
        print(f"  {group:<18} n={n:<4} {cells}")
    print(f"  cost: {cost}")


def test_subwindow_recall(tmp_path, monkeypatch, minilm, bench_scale):
    _, tokenizer = minilm

    def count(text):
        return len(tokenizer.offsets(text))

    size = SIZES[bench_scale]
    corpus = build_corpus(**size, count_tokens=count)
    scores, cost = _run(str(tmp_path / "mixed"), corpus, monkeypatch)
    _print("mixed palace: head + tail facts", scores, cost)

    control = head_control(corpus)
    control_scores, control_cost = _run(str(tmp_path / "control"), control, monkeypatch)
    _print("control: head facts + fact-free long drawers", control_scores, control_cost)

    for mode, groups in scores.items():
        for group, metrics in groups.items():
            for name, value in metrics.items():
                record_metric("subwindow_recall", f"mixed.{mode}.{group}.{name}", value)
    for mode, groups in control_scores.items():
        for group, metrics in groups.items():
            for name, value in metrics.items():
                record_metric("subwindow_recall", f"control.{mode}.{group}.{name}", value)
    for name, value in cost.items():
        record_metric("subwindow_recall", f"cost.{name}", value)

    tail = "tail/paraphrase"
    assert scores["vector+windows"][tail]["R@10"] > scores["vector"][tail]["R@10"]
