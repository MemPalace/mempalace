"""Tests for the local, text-free private Palace benchmark."""

import json
import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).parents[1] / "benchmarks" / "search_benchmark.py"
_SPEC = importlib.util.spec_from_file_location("private_palace_search_benchmark", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
search_benchmark = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = search_benchmark
_SPEC.loader.exec_module(search_benchmark)

BenchmarkCase = search_benchmark.BenchmarkCase
HubSearchClient = search_benchmark.HubSearchClient
LocalPalaceSearchSource = search_benchmark.LocalPalaceSearchSource
SearchHit = search_benchmark.SearchHit
build_search_algorithms = search_benchmark.build_search_algorithms
build_blind_pool = search_benchmark.build_blind_pool
corpus_changed = search_benchmark.corpus_changed
evaluate_ranking = search_benchmark.evaluate_ranking
load_benchmark_cases = search_benchmark.load_benchmark_cases
reciprocal_rank_fusion = search_benchmark.reciprocal_rank_fusion
run_benchmark = search_benchmark.run_benchmark


def test_load_benchmark_cases_validates_and_preserves_private_labels(tmp_path):
    dataset = tmp_path / "private-search.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "id": "q-001",
                "query": "Which database did we choose?",
                "judgments": {"drawer-postgres": 3, "drawer-options": 1},
                "filters": {"wing": "project"},
                "tags": ["decision", "paraphrase"],
                "expect_no_results": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    cases = load_benchmark_cases(dataset)

    assert cases == [
        BenchmarkCase(
            id="q-001",
            query="Which database did we choose?",
            judgments={"drawer-postgres": 3, "drawer-options": 1},
            filters={"wing": "project"},
            tags=("decision", "paraphrase"),
            expect_no_results=False,
        )
    ]


def test_load_benchmark_cases_rejects_duplicate_ids(tmp_path):
    dataset = tmp_path / "duplicate.jsonl"
    row = json.dumps({"id": "same", "query": "q", "judgments": {"drawer": 1}})
    dataset.write_text(f"{row}\n{row}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate case id 'same'"):
        load_benchmark_cases(dataset)


def test_reciprocal_rank_fusion_has_independently_worked_order():
    rankings = {
        "vector": ["a", "b", "c"],
        "bm25": ["b", "d", "a"],
    }

    fused = reciprocal_rank_fusion(rankings, rank_constant=0)

    assert [hit.drawer_id for hit in fused] == ["b", "a", "d", "c"]
    assert fused[0].score == pytest.approx(1.5)
    assert fused[1].score == pytest.approx(4 / 3)


def test_weighted_rrf_can_favor_vector_rank_without_score_normalization():
    rankings = {
        "vector": ["a", "b", "c"],
        "bm25": ["b", "d", "a"],
    }

    fused = reciprocal_rank_fusion(
        rankings,
        rank_constant=0,
        weights={"vector": 0.6, "bm25": 0.4},
    )

    assert [hit.drawer_id for hit in fused[:2]] == ["a", "b"]
    assert fused[0].score == pytest.approx(0.6 + (0.4 / 3))
    assert fused[1].score == pytest.approx((0.6 / 2) + 0.4)


def test_evaluate_ranking_uses_graded_relevance():
    case = BenchmarkCase(
        id="q",
        query="private query",
        judgments={"a": 3, "b": 2},
    )

    metrics = evaluate_ranking(case, ["b", "x", "a"], ks=(1, 3))

    assert metrics["hit_rate@1"] == 1.0
    assert metrics["pooled_recall@1"] == 0.5
    assert metrics["pooled_recall@3"] == 1.0
    assert metrics["mrr@3"] == 1.0
    assert metrics["ndcg@3"] == pytest.approx(6.5 / (7 + (3 / 1.5849625007)))


class _FakeAlgorithm:
    def __init__(self, name, rankings):
        self.name = name
        self._rankings = rankings

    def search(self, case, limit):
        return [
            SearchHit(drawer_id=item, score=1.0 / rank)
            for rank, item in enumerate(self._rankings[case.id], 1)
        ][:limit]


def test_run_benchmark_reports_quality_and_latency_without_drawer_text():
    cases = [
        BenchmarkCase(id="q1", query="secret first query", judgments={"a": 3}),
        BenchmarkCase(id="q2", query="secret second query", judgments={"b": 2}),
    ]
    algorithms = [
        _FakeAlgorithm("good", {"q1": ["a", "x"], "q2": ["b", "x"]}),
        _FakeAlgorithm("bad", {"q1": ["x", "a"], "q2": ["x", "b"]}),
    ]

    report = run_benchmark(
        cases,
        algorithms,
        limit=2,
        ks=(1, 2),
        warmups=1,
        repeats=2,
        seed=7,
    )

    assert report["schema_version"] == 1
    assert report["case_count"] == 2
    assert report["algorithms"]["good"]["quality"]["hit_rate@1"] == 1.0
    assert report["algorithms"]["bad"]["quality"]["hit_rate@1"] == 0.0
    assert report["algorithms"]["good"]["latency_ms"]["samples"] == 4
    assert report["algorithms"]["good"]["latency_ms"]["p95"] >= 0.0
    assert report["algorithms"]["good"]["latency_scope"] == "in_process"
    assert report["latency_comparison_groups"] == {"in_process": ["good", "bad"]}
    assert report["algorithms"]["good"]["rankings"] == {"q1": ["a", "x"], "q2": ["b", "x"]}

    serialized = json.dumps(report)
    assert "secret first query" not in serialized
    assert "secret second query" not in serialized
    assert "document" not in serialized


def test_run_benchmark_requires_judgments_for_quality():
    cases = [BenchmarkCase(id="unjudged", query="q")]

    with pytest.raises(ValueError, match="has no positive relevance judgments"):
        run_benchmark(cases, [_FakeAlgorithm("fake", {"unjudged": []})])


def test_run_benchmark_scores_expected_no_result_cases():
    cases = [BenchmarkCase(id="negative", query="nothing should match", expect_no_results=True)]
    algorithms = [
        _FakeAlgorithm("quiet", {"negative": []}),
        _FakeAlgorithm("noisy", {"negative": ["false-positive"]}),
    ]

    report = run_benchmark(cases, algorithms, limit=1, ks=(1,), warmups=0, repeats=1)

    assert report["algorithms"]["quiet"]["quality"]["no_result_accuracy@1"] == 1.0
    assert report["algorithms"]["noisy"]["quality"]["no_result_accuracy@1"] == 0.0
    assert report["algorithms"]["noisy"]["quality"]["false_positive_count@1"] == 1.0


class _FakeSearchSource:
    def vector_ranking(self, case, depth):
        return [SearchHit("vector-first", 0.9), SearchHit("shared", 0.8)][:depth]

    def lexical_ranking(self, case, depth):
        return [SearchHit("shared", 12.0), SearchHit("lexical-first", 8.0)][:depth]

    def current_ranking(self, case, limit, *, candidate_strategy):
        prefix = "current-union" if candidate_strategy == "union" else "current-vector"
        return [SearchHit(prefix, 1.0)][:limit]


def test_build_search_algorithms_exposes_baselines_and_fusion_without_scores_crossing_seam():
    algorithms = {
        algorithm.name: algorithm
        for algorithm in build_search_algorithms(
            _FakeSearchSource(),
            names=("vector", "bm25", "current", "union", "rrf", "weighted_rrf"),
            candidate_depth=10,
            rank_constant=0,
            vector_weight=0.75,
            bm25_weight=0.25,
        )
    }
    case = BenchmarkCase(id="q", query="private", judgments={"shared": 3})

    assert [hit.drawer_id for hit in algorithms["vector"].search(case, 5)] == [
        "vector-first",
        "shared",
    ]
    assert [hit.drawer_id for hit in algorithms["bm25"].search(case, 5)] == [
        "shared",
        "lexical-first",
    ]
    assert algorithms["current"].search(case, 5)[0].drawer_id == "current-vector"
    assert algorithms["union"].search(case, 5)[0].drawer_id == "current-union"
    assert algorithms["rrf"].search(case, 5)[0].drawer_id == "shared"
    assert algorithms["weighted_rrf"].search(case, 5)[0].drawer_id == "vector-first"
    assert algorithms["current"].latency_scope == "product_boundary"
    assert algorithms["union"].latency_scope == "direct_product_path"
    assert algorithms["rrf"].latency_scope == "in_process"


def test_build_search_algorithms_rejects_unknown_name():
    with pytest.raises(ValueError, match="unknown search algorithms: mystery"):
        build_search_algorithms(_FakeSearchSource(), names=("mystery",))


class _QueryResult:
    ids = [["chunk-a", "physical-b"]]
    metadatas = [[{"parent_drawer_id": "logical-a"}, {}]]
    distances = [[0.1, 0.3]]


class _LexicalHit:
    def __init__(self, drawer_id, metadata, score):
        self.id = drawer_id
        self.metadata = metadata
        self.score = score
        self.document = "must never reach the benchmark report"


class _LexicalResult:
    hits = [
        _LexicalHit("chunk-a", {"parent_drawer_id": "logical-a"}, 10.0),
        _LexicalHit("physical-c", {}, 7.0),
    ]


class _FakeCollection:
    distance_metric = "cosine"

    def __init__(self):
        self.query_include = None

    def query(self, **kwargs):
        self.query_include = kwargs["include"]
        return _QueryResult()

    def lexical_search(self, **kwargs):
        return _LexicalResult()

    def count(self):
        return 2

    def maintenance_state(self):
        return {"row_count": 2, "consistency_token": "before"}


def test_local_palace_source_uses_canonical_ids_and_discards_documents():
    collection = _FakeCollection()
    source = LocalPalaceSearchSource(
        "C:/private-palace",
        collection=collection,
        current_search=lambda *args, **kwargs: {"results": []},
    )
    case = BenchmarkCase(id="q", query="find it", judgments={"logical-a": 3})

    vector = source.vector_ranking(case, 10)
    lexical = source.lexical_ranking(case, 10)

    assert collection.query_include == ["metadatas", "distances"]
    assert [(hit.drawer_id, hit.score) for hit in vector] == [
        ("logical-a", pytest.approx(0.9)),
        ("physical-b", pytest.approx(0.7)),
    ]
    assert [(hit.drawer_id, hit.score) for hit in lexical] == [
        ("logical-a", 10.0),
        ("physical-c", 7.0),
    ]


def test_local_palace_source_refuses_backend_without_enforced_read_only(tmp_path):
    opened = []

    with pytest.raises(RuntimeError, match="require backend=sqlite_exact"):
        LocalPalaceSearchSource(
            str(tmp_path),
            backend="chroma",
            collection_opener=lambda *args, **kwargs: opened.append((args, kwargs)),
        )

    assert opened == []


def test_date_filtered_lexical_uses_same_widened_cohort_as_vector():
    class CapturingCollection(_FakeCollection):
        def __init__(self):
            super().__init__()
            self.lexical_limit = None

        def lexical_search(self, **kwargs):
            self.lexical_limit = kwargs["n_results"]
            return _LexicalResult()

    collection = CapturingCollection()
    source = LocalPalaceSearchSource(
        "C:/private-palace",
        collection=collection,
        current_search=lambda *args, **kwargs: {"results": []},
    )
    case = BenchmarkCase(id="q", query="find it", filters={"since": "2026-01-01"})

    source.lexical_ranking(case, 10)

    assert collection.lexical_limit == 150


def test_local_palace_source_replays_current_strategy_with_case_filters():
    calls = []

    def current_search(query, palace_path, **kwargs):
        calls.append((query, palace_path, kwargs))
        return {"results": [{"drawer_id": "answer", "similarity": 0.75}]}

    source = LocalPalaceSearchSource(
        "C:/private-palace",
        collection=_FakeCollection(),
        current_search=current_search,
        collection_name="drawers",
        max_distance=1.5,
    )
    case = BenchmarkCase(
        id="q",
        query="find it",
        judgments={"answer": 3},
        filters={"wing": "project", "room": "search", "since": "2026-01-01"},
    )

    hits = source.current_ranking(case, 5, candidate_strategy="union")

    assert hits == [SearchHit("answer", 0.75)]
    assert calls == [
        (
            "find it",
            "C:/private-palace",
            {
                "wing": "project",
                "room": "search",
                "source_file": None,
                "since": "2026-01-01",
                "before": None,
                "n_results": 5,
                "max_distance": 1.5,
                "candidate_strategy": "union",
                "collection_name": "drawers",
            },
        )
    ]


def test_build_blind_pool_deduplicates_and_hides_algorithm_provenance():
    cases = [BenchmarkCase(id="q", query="private question")]
    algorithms = [
        _FakeAlgorithm("first", {"q": ["a", "b"]}),
        _FakeAlgorithm("second", {"q": ["b", "c"]}),
    ]

    pool = build_blind_pool(cases, algorithms, pool_depth=2, seed=11)

    assert pool[0]["id"] == "q"
    assert sorted(pool[0]["drawer_ids"]) == ["a", "b", "c"]
    serialized = json.dumps(pool)
    assert "private question" not in serialized
    assert "first" not in serialized
    assert "second" not in serialized


def test_local_palace_source_explains_missing_embedding_runtime():
    class MissingRuntimeCollection(_FakeCollection):
        def query(self, **kwargs):
            raise ValueError("The onnxruntime python package is not installed")

    source = LocalPalaceSearchSource(
        "C:/private-palace",
        collection=MissingRuntimeCollection(),
        current_search=lambda *args, **kwargs: {"results": []},
    )
    case = BenchmarkCase(id="q", query="find it", judgments={"answer": 3})

    with pytest.raises(RuntimeError, match="vector benchmark cannot embed queries.*onnxruntime"):
        source.vector_ranking(case, 10)


def test_hub_search_client_reuses_authenticated_transport_and_parses_tool_result(monkeypatch):
    calls = []

    def fake_forward(base_url, headers, request, *, timeout):
        calls.append((base_url, headers, request, timeout))
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"results": [{"drawer_id": "answer", "similarity": 0.8}]}
                        ),
                    }
                ]
            },
        }

    monkeypatch.setattr("mempalace.hub_client.forward_json_rpc", fake_forward)
    client = HubSearchClient(
        "http://127.0.0.1:9",
        {"Content-Type": "application/json", "Authorization": "Bearer local-secret"},
    )

    result = client(
        "private query",
        "C:/private-palace",
        n_results=5,
        max_distance=1.5,
        candidate_strategy="vector",
        wing="project",
        room=None,
        source_file=None,
        since=None,
        before=None,
    )

    assert result["results"][0]["drawer_id"] == "answer"
    base_url, headers, request, timeout = calls[0]
    assert base_url == "http://127.0.0.1:9"
    assert headers["Authorization"] == "Bearer local-secret"
    assert timeout == 120
    assert request["params"] == {
        "name": "mempalace_search",
        "arguments": {
            "query": "private query",
            "limit": 5,
            "max_distance": 1.5,
            "wing": "project",
        },
    }


def test_local_palace_source_refuses_uncontrolled_direct_product_replay(monkeypatch):
    monkeypatch.setattr(search_benchmark, "_live_hub_search", lambda _: None)
    source = LocalPalaceSearchSource("C:/private-palace", collection=_FakeCollection())
    case = BenchmarkCase(id="q", query="private", judgments={"answer": 3})

    with pytest.raises(RuntimeError, match="--allow-direct-product-path"):
        source.current_ranking(case, 5, candidate_strategy="union")


def test_current_latency_scope_reflects_direct_fallback_when_explicitly_allowed(monkeypatch):
    monkeypatch.setattr(search_benchmark, "_live_hub_search", lambda _: None)
    source = LocalPalaceSearchSource(
        "C:/private-palace",
        collection=_FakeCollection(),
        allow_direct_product_path=True,
    )

    algorithm = build_search_algorithms(source, names=("current",))[0]

    assert algorithm.latency_scope == "direct_product_path"


def test_refreshed_public_backend_state_invalidates_changed_corpus(monkeypatch):
    class ChangedCollection(_FakeCollection):
        def maintenance_state(self):
            return {"row_count": 2, "consistency_token": "after"}

    opened = []

    def opener(*args, **kwargs):
        opened.append((args, kwargs))
        return ChangedCollection()

    source = LocalPalaceSearchSource(
        "C:/private-palace",
        backend="sqlite_exact",
        collection=_FakeCollection(),
        collection_opener=opener,
        current_search=lambda *args, **kwargs: {"results": []},
    )

    start = source.snapshot_metadata()
    end = source.snapshot_metadata(refresh=True)

    assert corpus_changed(start, end) is True
    assert opened[0][1]["read_only"] is True


@pytest.mark.parametrize("tag", [None, "invalid"])
def test_run_cli_requires_dev_or_test_tag(tmp_path, tag):
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "id": "q",
                "query": "private",
                "judgments": {"answer": 3},
                "tags": ["dev"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    argv = ["run", "--dataset", str(dataset), "--out", str(tmp_path / "out.json")]
    if tag is not None:
        argv.extend(["--tag", tag])

    with pytest.raises(SystemExit) as exc:
        search_benchmark.main(argv)

    assert exc.value.code == 2


def test_run_cli_rejects_case_in_both_dev_and_test_sets(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "id": "q",
                "query": "private",
                "judgments": {"answer": 3},
                "tags": ["dev", "test"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        search_benchmark.main(
            [
                "run",
                "--dataset",
                str(dataset),
                "--out",
                str(tmp_path / "out.json"),
                "--tag",
                "dev",
            ]
        )

    assert exc.value.code == 2


def test_private_artifacts_are_owner_only_and_replace_broad_existing_mode(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o755)
    target = directory / "report.json"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o644)

    search_benchmark._write_private_text(target, "new\n")

    assert target.read_text(encoding="utf-8") == "new\n"
    if os.name == "nt":
        import subprocess

        identity = subprocess.run(
            ["whoami"], capture_output=True, text=True, check=True
        ).stdout.strip()
        for path in (directory, target):
            acl = subprocess.run(
                ["icacls", str(path)], capture_output=True, text=True, check=True
            ).stdout.casefold()
            assert identity.casefold() in acl
            assert "(i)" not in acl
    else:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(directory.glob(f".{target.name}.*.tmp"))


def test_windows_acl_verification_rejects_extra_explicit_principal(monkeypatch, tmp_path):
    class Result:
        def __init__(self, stdout=""):
            self.stdout = stdout

    target = tmp_path / "private.json"
    target.touch()
    identity = "WORKSTATION\\owner"

    def fake_run(arguments, **_kwargs):
        if arguments[0] == "whoami":
            return Result(f"{identity}\n")
        if len(arguments) == 2:
            return Result(f"{target} {identity}:(F)\n        Everyone:(R)\n")
        return Result()

    monkeypatch.setattr(search_benchmark.os, "name", "nt")
    monkeypatch.setattr(search_benchmark.subprocess, "run", fake_run)

    with pytest.raises(PermissionError, match="owner-only ACL"):
        search_benchmark._restrict_private_path(target, directory=False)
