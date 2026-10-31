"""Offline tests for the LongMemEval benchmark's retrieval metrics."""

import importlib.util
import math
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).parents[1] / "benchmarks" / "longmemeval_bench.py"
_SPEC = importlib.util.spec_from_file_location("longmemeval_benchmark_metrics", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
longmemeval_bench = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = longmemeval_bench
_SPEC.loader.exec_module(longmemeval_bench)

_CORPUS_IDS = ["answer-a", "unrelated", "answer-b", "answer-c"]
_CORRECT_IDS = {"answer-a", "answer-b", "answer-c"}


@pytest.mark.parametrize("rankings", [[0, 1], [0], [0, 1, 2, 3]])
def test_ndcg_penalizes_unretrieved_evidence(rankings):
    # Three relevant documents exist, so an ideal top-2 contains two of them.
    expected = 1.0 / (1.0 + 1.0 / math.log2(3))

    assert longmemeval_bench.ndcg(rankings, _CORRECT_IDS, _CORPUS_IDS, 2) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("rankings", "k", "expected"),
    [
        ([2, 0, 3, 1], 3, 1.0),
        ([2, 0, 1, 3], 2, 1.0),
        ([0, 2, 3, 1], 10, 1.0),
        ([1, 0, 2, 3], 1, 0.0),
        (
            [1, 0, 2, 3],
            3,
            (1.0 / math.log2(3) + 0.5) / (1.0 + 1.0 / math.log2(3) + 0.5),
        ),
        ([1], 2, 0.0),
        ([], 2, 0.0),
        ([0, 2, 3, 1], 0, 0.0),
    ],
)
def test_ndcg_rank_discounts_and_cutoffs(rankings, k, expected):
    assert longmemeval_bench.ndcg(rankings, _CORRECT_IDS, _CORPUS_IDS, k) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("correct_ids", "expected"),
    [
        ({"answer-a", "absent"}, 1.0),
        ({"absent"}, 0.0),
        (set(), 0.0),
    ],
)
def test_ndcg_ideal_only_counts_relevant_documents_in_corpus(correct_ids, expected):
    assert longmemeval_bench.ndcg([0, 1], correct_ids, _CORPUS_IDS, 2) == expected


def test_ndcg_empty_corpus():
    assert longmemeval_bench.ndcg([], {"absent"}, [], 2) == 0.0


def test_evaluate_retrieval_propagates_corrected_ndcg():
    recall_any, recall_all, ndcg = longmemeval_bench.evaluate_retrieval(
        [0, 1], _CORRECT_IDS, _CORPUS_IDS, 2
    )

    assert recall_any == 1.0
    assert recall_all == 0.0
    assert ndcg == pytest.approx(1.0 / (1.0 + 1.0 / math.log2(3)))
