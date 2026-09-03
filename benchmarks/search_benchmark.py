"""Private, text-free evaluation primitives for Palace search algorithms.

The benchmark seam deliberately deals only in query cases and ranked drawer
IDs. Search adapters may inspect drawer text while retrieving, but neither the
runner nor its JSON-compatible report accepts or persists document content.
"""

from __future__ import annotations

import json
import math
import os
import random
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence


_ALLOWED_FILTERS = frozenset({"wing", "room", "source_file", "since", "before"})


def _restrict_private_path(path: Path, *, directory: bool) -> None:
    """Restrict a path to the current user, or fail before publishing data."""

    if os.name == "nt":
        identity_result = subprocess.run(["whoami"], capture_output=True, text=True, check=True)
        identity = identity_result.stdout.strip()
        if not identity:
            raise PermissionError("could not resolve the current Windows identity")
        grant = f"{identity}:{'(OI)(CI)' if directory else ''}F"
        for arguments in (("/reset",), ("/inheritance:r",), ("/grant:r", grant)):
            subprocess.run(
                ["icacls", str(path), *arguments],
                capture_output=True,
                text=True,
                check=True,
            )
        acl = subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True, check=True
        ).stdout
        acl_folded = acl.casefold()
        acl_lines = [line.strip() for line in acl.splitlines() if ":(" in line]
        principals = set()
        path_text = str(path)
        for line in acl_lines:
            if line.casefold().startswith(path_text.casefold()):
                line = line[len(path_text) :].lstrip()
            principals.add(line.split(":(", 1)[0].strip().casefold())
        if principals != {identity.casefold()} or "(i)" in acl_folded:
            raise PermissionError(f"could not verify owner-only ACL for {path}")
        return

    path.chmod(0o700 if directory else 0o600)
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"could not establish owner-only permissions for {path}")


def _write_private_text(path: Path, text: str, *, refuse_existing: bool = False) -> None:
    """Atomically publish a private benchmark artifact with owner-only access."""

    if refuse_existing and path.exists():
        raise FileExistsError(f"refusing to overwrite existing dataset: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _restrict_private_path(path.parent, directory=True)

    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        _restrict_private_path(Path(temp_name), directory=False)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if refuse_existing and path.exists():
            raise FileExistsError(f"refusing to overwrite existing dataset: {path}")
        os.replace(temp_name, path)
        _restrict_private_path(path, directory=False)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True)
class BenchmarkCase:
    """One private retrieval question and its graded drawer judgments."""

    id: str
    query: str
    judgments: Mapping[str, int] = field(default_factory=dict)
    filters: Mapping[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    expect_no_results: bool = False


@dataclass(frozen=True)
class SearchHit:
    """The only result shape visible to the benchmark runner."""

    drawer_id: str
    score: float


class SearchAlgorithm(Protocol):
    """Adapter interface for one search implementation under evaluation."""

    name: str
    latency_scope: str

    def search(self, case: BenchmarkCase, limit: int) -> Sequence[SearchHit]: ...


class SearchSource(Protocol):
    """Raw Palace ranking signals used by controlled benchmark algorithms."""

    def vector_ranking(self, case: BenchmarkCase, depth: int) -> Sequence[SearchHit]: ...

    def lexical_ranking(self, case: BenchmarkCase, depth: int) -> Sequence[SearchHit]: ...

    def current_ranking(
        self,
        case: BenchmarkCase,
        limit: int,
        *,
        candidate_strategy: str,
    ) -> Sequence[SearchHit]: ...

    def current_latency_scope(self, *, candidate_strategy: str) -> str: ...


class LocalPalaceSearchSource:
    """Read-only local Palace adapter for baseline and fusion experiments."""

    def __init__(
        self,
        palace_path: str,
        *,
        collection_name: str | None = None,
        backend: str | None = None,
        max_distance: float = 0.0,
        allow_direct_product_path: bool = False,
        collection=None,
        current_search=None,
        collection_opener=None,
    ):
        from mempalace.palace import get_collection
        from mempalace.searcher import search_memories

        self.palace_path = palace_path
        self.collection_name = collection_name
        self.backend = backend
        self.max_distance = max_distance
        self._allow_direct_product_path = allow_direct_product_path
        self._collection_opener = collection_opener or get_collection
        self._collection = collection or self._open_collection()
        self._direct_current_search = current_search or search_memories
        self._injected_current_search = current_search
        self._hub_current_search = None if current_search else _live_hub_search(palace_path)

    def _open_collection(self):
        from mempalace.palace import resolve_backend_name

        backend_name = resolve_backend_name(self.palace_path, explicit=self.backend)
        if backend_name != "sqlite_exact":
            raise RuntimeError(
                "controlled vector/BM25 baselines require backend=sqlite_exact because it is "
                "the only built-in backend with an enforced read-only open; use the live hub "
                "for current-product replay, or rerun against a sqlite_exact Palace"
            )
        return self._collection_opener(
            self.palace_path,
            collection_name=self.collection_name,
            create=False,
            backend=self.backend,
            read_only=True,
        )

    def snapshot_metadata(self, *, refresh: bool = False) -> dict:
        """Return non-content corpus state for reproducibility checks."""

        collection = self._open_collection() if refresh else self._collection
        try:
            maintenance = collection.maintenance_state()
        except Exception:
            maintenance = {}
        return {
            "drawer_count": collection.count(),
            "distance_metric": getattr(collection, "distance_metric", "unknown"),
            "maintenance": maintenance,
            "read_only_requested": True,
        }

    def vector_ranking(self, case: BenchmarkCase, depth: int) -> Sequence[SearchHit]:
        from mempalace.date_window import filed_at_in_window, parse_window
        from mempalace.query_sanitizer import sanitize_query
        from mempalace.searcher import (
            _distance_to_similarity,
            _result_drawer_id,
            build_where_filter,
        )

        query = sanitize_query(case.query)["clean_query"]
        where = build_where_filter(
            case.filters.get("wing"),
            case.filters.get("room"),
            case.filters.get("source_file"),
        )
        since_dt, before_dt = parse_window(
            case.filters.get("since"),
            case.filters.get("before"),
        )
        fetch_depth = max(depth * 3, depth)
        if since_dt is not None or before_dt is not None:
            fetch_depth = max(fetch_depth, min(depth * 15, 500))
        kwargs = {
            "query_texts": [query],
            "n_results": fetch_depth,
            "include": ["metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        try:
            result = self._collection.query(**kwargs)
        except (ImportError, ModuleNotFoundError, ValueError) as exc:
            if "onnxruntime" not in str(exc).lower():
                raise
            raise RuntimeError(
                "vector benchmark cannot embed queries because this Python environment lacks "
                "onnxruntime; use the same environment as the running Palace or install the "
                "matching local embedding runtime"
            ) from exc
        ids = _first_nested(result, "ids")
        metadatas = _first_nested(result, "metadatas")
        distances = _first_nested(result, "distances")
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for stored_id, metadata, distance in zip(ids, metadatas, distances):
            metadata = metadata or {}
            if self.max_distance > 0 and distance > self.max_distance:
                continue
            if (since_dt is not None or before_dt is not None) and not filed_at_in_window(
                metadata.get("filed_at"), since_dt, before_dt
            ):
                continue
            drawer_id = _result_drawer_id(metadata, stored_id)
            if not drawer_id or drawer_id in seen:
                continue
            seen.add(drawer_id)
            hits.append(
                SearchHit(
                    drawer_id=drawer_id,
                    score=_distance_to_similarity(distance, self._collection.distance_metric),
                )
            )
            if len(hits) >= depth:
                break
        return hits

    def lexical_ranking(self, case: BenchmarkCase, depth: int) -> Sequence[SearchHit]:
        from mempalace.date_window import filed_at_in_window, parse_window
        from mempalace.query_sanitizer import sanitize_query
        from mempalace.searcher import _result_drawer_id, build_where_filter

        query = sanitize_query(case.query)["clean_query"]
        where = build_where_filter(
            case.filters.get("wing"),
            case.filters.get("room"),
            case.filters.get("source_file"),
        )
        since_dt, before_dt = parse_window(
            case.filters.get("since"),
            case.filters.get("before"),
        )
        fetch_depth = max(depth * 3, depth)
        if since_dt is not None or before_dt is not None:
            fetch_depth = max(fetch_depth, min(depth * 15, 500))
        result = self._collection.lexical_search(
            query=query,
            n_results=fetch_depth,
            where=where or None,
        )
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for hit in result.hits:
            metadata = hit.metadata or {}
            if (since_dt is not None or before_dt is not None) and not filed_at_in_window(
                metadata.get("filed_at"), since_dt, before_dt
            ):
                continue
            drawer_id = _result_drawer_id(metadata, hit.id)
            if not drawer_id or drawer_id in seen:
                continue
            seen.add(drawer_id)
            hits.append(SearchHit(drawer_id=drawer_id, score=float(hit.score)))
            if len(hits) >= depth:
                break
        return hits

    def current_ranking(
        self,
        case: BenchmarkCase,
        limit: int,
        *,
        candidate_strategy: str,
    ) -> Sequence[SearchHit]:
        from mempalace.query_sanitizer import sanitize_query

        query = sanitize_query(case.query)["clean_query"]
        if self._injected_current_search is not None:
            current_search = self._injected_current_search
        elif candidate_strategy == "vector" and self._hub_current_search is not None:
            current_search = self._hub_current_search
        elif self._allow_direct_product_path:
            current_search = self._direct_current_search
        else:
            profile = "union" if candidate_strategy == "union" else "current"
            raise RuntimeError(
                f"{profile} requires the live Palace MCP hub for exact product replay; "
                "union is not exposed by MCP, so run it only in a write-quiescent window "
                "with --allow-direct-product-path"
            )

        result = current_search(
            query,
            self.palace_path,
            wing=case.filters.get("wing"),
            room=case.filters.get("room"),
            source_file=case.filters.get("source_file"),
            since=case.filters.get("since"),
            before=case.filters.get("before"),
            n_results=limit,
            max_distance=self.max_distance,
            candidate_strategy=candidate_strategy,
            collection_name=self.collection_name,
        )
        if result.get("error"):
            raise RuntimeError(str(result["error"]))
        hits: list[SearchHit] = []
        for rank, row in enumerate(result.get("results", []), 1):
            drawer_id = row.get("drawer_id")
            if not drawer_id:
                continue
            score = row.get("similarity")
            hits.append(
                SearchHit(
                    drawer_id=drawer_id,
                    score=float(score) if isinstance(score, (int, float)) else 1.0 / rank,
                )
            )
        return hits

    def current_latency_scope(self, *, candidate_strategy: str) -> str:
        """Describe the boundary timed by ``current_ranking`` for this source."""

        if (
            self._injected_current_search is None
            and candidate_strategy == "vector"
            and self._hub_current_search is not None
        ):
            return "product_boundary"
        return "direct_product_path"


class HubSearchClient:
    """Exact product-search replay through the live local MCP hub."""

    def __init__(self, base_url: str, headers: Mapping[str, str]):
        self._base_url = base_url
        self._headers = dict(headers)

    def __call__(self, query: str, palace_path: str, **kwargs) -> dict:
        from mempalace.hub_client import forward_json_rpc

        if kwargs.get("candidate_strategy") != "vector":
            raise RuntimeError(
                "the MCP search interface does not expose candidate_strategy='union'"
            )
        arguments = {
            "query": query,
            "limit": kwargs["n_results"],
            "max_distance": kwargs["max_distance"],
        }
        for name in ("wing", "room", "source_file", "since", "before"):
            if kwargs.get(name) is not None:
                arguments[name] = kwargs[name]
        payload = forward_json_rpc(
            self._base_url,
            self._headers,
            {
                "jsonrpc": "2.0",
                "id": f"private-search-benchmark-{time.time_ns()}",
                "method": "tools/call",
                "params": {"name": "mempalace_search", "arguments": arguments},
            },
            timeout=120,
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Palace MCP search returned an empty response")
        if payload.get("error"):
            raise RuntimeError(f"Palace MCP search failed: {payload['error']}")
        content = payload.get("result", {}).get("content", [])
        text_block = next(
            (block.get("text") for block in content if block.get("type") == "text"),
            None,
        )
        if not text_block:
            raise RuntimeError("Palace MCP search returned no JSON text result")
        result = json.loads(text_block)
        if not isinstance(result, dict):
            raise RuntimeError("Palace MCP search returned a non-object result")
        return result


def _live_hub_search(palace_path: str):
    from mempalace.hub_client import discover_hub

    target = discover_hub(palace_path)
    if target is None:
        return None
    return HubSearchClient(*target)


class _SourceAlgorithm:
    def __init__(self, name: str, source: SearchSource, candidate_depth: int):
        self.name = name
        self.latency_scope = "in_process"
        if name in {"current", "union"}:
            strategy = "union" if name == "union" else "vector"
            scope = getattr(source, "current_latency_scope", None)
            self.latency_scope = (
                scope(candidate_strategy=strategy)
                if callable(scope)
                else ("direct_product_path" if name == "union" else "product_boundary")
            )
        self._source = source
        self._candidate_depth = candidate_depth

    def search(self, case: BenchmarkCase, limit: int) -> Sequence[SearchHit]:
        if self.name == "vector":
            return self._source.vector_ranking(case, max(limit, self._candidate_depth))[:limit]
        if self.name == "bm25":
            return self._source.lexical_ranking(case, max(limit, self._candidate_depth))[:limit]
        strategy = "union" if self.name == "union" else "vector"
        return self._source.current_ranking(case, limit, candidate_strategy=strategy)[:limit]


class _FusionAlgorithm:
    def __init__(
        self,
        name: str,
        source: SearchSource,
        candidate_depth: int,
        rank_constant: int,
        weights: Mapping[str, float] | None,
    ):
        self.name = name
        self.latency_scope = "in_process"
        self._source = source
        self._candidate_depth = candidate_depth
        self._rank_constant = rank_constant
        self._weights = weights

    def search(self, case: BenchmarkCase, limit: int) -> Sequence[SearchHit]:
        depth = max(limit, self._candidate_depth)
        vector = self._source.vector_ranking(case, depth)
        lexical = self._source.lexical_ranking(case, depth)
        return reciprocal_rank_fusion(
            {
                "vector": [hit.drawer_id for hit in vector],
                "bm25": [hit.drawer_id for hit in lexical],
            },
            rank_constant=self._rank_constant,
            weights=self._weights,
        )[:limit]


def build_search_algorithms(
    source: SearchSource,
    *,
    names: Sequence[str] = ("vector", "bm25", "current", "union", "rrf", "weighted_rrf"),
    candidate_depth: int = 50,
    rank_constant: int = 60,
    vector_weight: float = 0.6,
    bm25_weight: float = 0.4,
) -> list[SearchAlgorithm]:
    """Build the fixed algorithm matrix over one Palace search source."""

    supported = frozenset({"vector", "bm25", "current", "union", "rrf", "weighted_rrf"})
    unknown = sorted(set(names) - supported)
    if unknown:
        raise ValueError(f"unknown search algorithms: {', '.join(unknown)}")
    if candidate_depth <= 0:
        raise ValueError("candidate_depth must be positive")
    if rank_constant < 0:
        raise ValueError("rank_constant must be non-negative")

    algorithms: list[SearchAlgorithm] = []
    for name in names:
        if name in {"vector", "bm25", "current", "union"}:
            algorithms.append(_SourceAlgorithm(name, source, candidate_depth))
        else:
            weights = (
                None
                if name == "rrf"
                else {"vector": float(vector_weight), "bm25": float(bm25_weight)}
            )
            algorithms.append(
                _FusionAlgorithm(
                    name,
                    source,
                    candidate_depth,
                    rank_constant,
                    weights,
                )
            )
    return algorithms


def load_benchmark_cases(path: Path) -> list[BenchmarkCase]:
    """Load and validate the private JSONL benchmark dataset."""

    cases: list[BenchmarkCase] = []
    seen: set[str] = set()
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"line {line_number}: expected a JSON object")

            case_id = _required_text(row, "id", line_number)
            query = _required_text(row, "query", line_number)
            if case_id in seen:
                raise ValueError(f"line {line_number}: duplicate case id {case_id!r}")
            seen.add(case_id)

            judgments = row.get("judgments", {})
            if not isinstance(judgments, dict):
                raise ValueError(f"line {line_number}: judgments must be an object")
            checked_judgments: dict[str, int] = {}
            for drawer_id, relevance in judgments.items():
                if not isinstance(drawer_id, str) or not drawer_id.strip():
                    raise ValueError(
                        f"line {line_number}: judgment drawer ids must be non-empty strings"
                    )
                if (
                    isinstance(relevance, bool)
                    or not isinstance(relevance, int)
                    or relevance < 0
                    or relevance > 3
                ):
                    raise ValueError(
                        f"line {line_number}: relevance for {drawer_id!r} must be an integer 0..3"
                    )
                checked_judgments[drawer_id] = relevance

            filters = row.get("filters", {})
            if not isinstance(filters, dict):
                raise ValueError(f"line {line_number}: filters must be an object")
            unknown_filters = set(filters) - _ALLOWED_FILTERS
            if unknown_filters:
                names = ", ".join(sorted(unknown_filters))
                raise ValueError(f"line {line_number}: unsupported filters: {names}")
            checked_filters: dict[str, str] = {}
            for key, value in filters.items():
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"line {line_number}: filter {key!r} must be a non-empty string"
                    )
                checked_filters[key] = value

            tags = row.get("tags", [])
            if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                raise ValueError(f"line {line_number}: tags must be an array of strings")
            expect_no_results = row.get("expect_no_results", False)
            if not isinstance(expect_no_results, bool):
                raise ValueError(f"line {line_number}: expect_no_results must be a boolean")
            if expect_no_results and any(gain > 0 for gain in checked_judgments.values()):
                raise ValueError(
                    f"line {line_number}: expect_no_results cannot have positive judgments"
                )

            cases.append(
                BenchmarkCase(
                    id=case_id,
                    query=query,
                    judgments=checked_judgments,
                    filters=checked_filters,
                    tags=tuple(tags),
                    expect_no_results=expect_no_results,
                )
            )
    if not cases:
        raise ValueError("benchmark dataset contains no cases")
    return cases


def _required_text(row: dict, key: str, line_number: int) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"line {line_number}: {key} must be a non-empty string")
    return value


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[str]],
    *,
    rank_constant: int = 60,
    weights: Mapping[str, float] | None = None,
) -> list[SearchHit]:
    """Fuse ranked drawer IDs using RRF or weighted RRF.

    Rank positions are one-based. Duplicate IDs inside one input ranking count
    only at their first position. Scores from the source algorithms are never
    compared or normalized.
    """

    if rank_constant < 0:
        raise ValueError("rank_constant must be non-negative")

    fused_scores: dict[str, float] = {}
    best_ranks: dict[str, int] = {}
    for source, ranking in rankings.items():
        weight = 1.0 if weights is None else float(weights.get(source, 1.0))
        if not math.isfinite(weight) or weight < 0:
            raise ValueError(f"weight for {source!r} must be finite and non-negative")
        seen: set[str] = set()
        for rank, drawer_id in enumerate(ranking, 1):
            if not drawer_id or drawer_id in seen:
                continue
            seen.add(drawer_id)
            fused_scores[drawer_id] = fused_scores.get(drawer_id, 0.0) + (
                weight / (rank_constant + rank)
            )
            best_ranks[drawer_id] = min(best_ranks.get(drawer_id, rank), rank)

    ordered = sorted(
        fused_scores,
        key=lambda drawer_id: (-fused_scores[drawer_id], best_ranks[drawer_id], drawer_id),
    )
    return [SearchHit(drawer_id=drawer_id, score=fused_scores[drawer_id]) for drawer_id in ordered]


def evaluate_ranking(
    case: BenchmarkCase,
    ranking: Sequence[str],
    *,
    ks: Sequence[int] = (1, 5, 10),
    relevance_threshold: int = 2,
) -> dict[str, float]:
    """Evaluate one ranking against binary and graded relevance judgments."""

    if relevance_threshold < 1 or relevance_threshold > 3:
        raise ValueError("relevance_threshold must be in the range 1..3")
    clean_ranking = _unique_ids(ranking)
    if case.expect_no_results:
        metrics: dict[str, float] = {}
        for k in _validated_ks(ks):
            top = clean_ranking[:k]
            metrics[f"no_result_accuracy@{k}"] = float(not top)
            metrics[f"false_positive_count@{k}"] = float(len(top))
        return metrics

    positive = {
        drawer_id for drawer_id, gain in case.judgments.items() if gain >= relevance_threshold
    }
    if not positive:
        raise ValueError(f"case {case.id!r} has no positive relevance judgments")
    metrics: dict[str, float] = {}
    for k in _validated_ks(ks):
        top = clean_ranking[:k]
        relevant_count = sum(drawer_id in positive for drawer_id in top)
        metrics[f"hit_rate@{k}"] = float(relevant_count > 0)
        metrics[f"pooled_recall@{k}"] = relevant_count / len(positive)
        metrics[f"judgment_coverage@{k}"] = (
            sum(drawer_id in case.judgments for drawer_id in top) / len(top) if top else 0.0
        )

        first_relevant = next(
            (rank for rank, drawer_id in enumerate(top, 1) if drawer_id in positive),
            None,
        )
        metrics[f"mrr@{k}"] = 0.0 if first_relevant is None else 1.0 / first_relevant

        dcg = sum(
            ((2 ** case.judgments.get(drawer_id, 0)) - 1) / math.log2(rank + 1)
            for rank, drawer_id in enumerate(top, 1)
        )
        ideal_gains = sorted((gain for gain in case.judgments.values() if gain > 0), reverse=True)[
            :k
        ]
        ideal_dcg = sum(
            ((2**gain) - 1) / math.log2(rank + 1) for rank, gain in enumerate(ideal_gains, 1)
        )
        metrics[f"ndcg@{k}"] = dcg / ideal_dcg if ideal_dcg else 0.0
    return metrics


def run_benchmark(
    cases: Sequence[BenchmarkCase],
    algorithms: Sequence[SearchAlgorithm],
    *,
    limit: int = 10,
    ks: Sequence[int] = (1, 5, 10),
    warmups: int = 1,
    repeats: int = 5,
    seed: int = 0,
    relevance_threshold: int = 2,
    clock: Callable[[], float] = time.perf_counter,
) -> dict:
    """Run search adapters in randomized order and return a text-free report."""

    if not cases:
        raise ValueError("at least one benchmark case is required")
    if not algorithms:
        raise ValueError("at least one search algorithm is required")
    if limit <= 0:
        raise ValueError("limit must be positive")
    if warmups < 0 or repeats <= 0:
        raise ValueError("warmups must be non-negative and repeats must be positive")
    checked_ks = _validated_ks(ks)
    if max(checked_ks) > limit:
        raise ValueError("every evaluation k must be less than or equal to limit")

    names = [algorithm.name for algorithm in algorithms]
    if len(names) != len(set(names)):
        raise ValueError("algorithm names must be unique")
    for case in cases:
        if not case.expect_no_results and not any(
            gain >= relevance_threshold for gain in case.judgments.values()
        ):
            raise ValueError(f"case {case.id!r} has no positive relevance judgments")

    rng = random.Random(seed)
    for _ in range(warmups):
        schedule = [(algorithm, case) for algorithm in algorithms for case in cases]
        rng.shuffle(schedule)
        for algorithm, case in schedule:
            algorithm.search(case, limit)

    timings: dict[str, list[float]] = {name: [] for name in names}
    rankings: dict[str, dict[str, list[str]]] = {name: {} for name in names}
    for _ in range(repeats):
        schedule = [(algorithm, case) for algorithm in algorithms for case in cases]
        rng.shuffle(schedule)
        for algorithm, case in schedule:
            started = clock()
            hits = algorithm.search(case, limit)
            elapsed_ms = (clock() - started) * 1000.0
            timings[algorithm.name].append(elapsed_ms)
            if case.id not in rankings[algorithm.name]:
                rankings[algorithm.name][case.id] = _unique_ids(hit.drawer_id for hit in hits)[
                    :limit
                ]

    algorithm_reports: dict[str, dict] = {}
    for algorithm in algorithms:
        per_case = [
            evaluate_ranking(
                case,
                rankings[algorithm.name][case.id],
                ks=checked_ks,
                relevance_threshold=relevance_threshold,
            )
            for case in cases
        ]
        metric_names = sorted({name for metrics in per_case for name in metrics})
        quality = {
            name: sum(metrics[name] for metrics in per_case if name in metrics)
            / sum(name in metrics for metrics in per_case)
            for name in metric_names
        }
        samples = timings[algorithm.name]
        algorithm_reports[algorithm.name] = {
            "latency_scope": getattr(algorithm, "latency_scope", "in_process"),
            "quality": quality,
            "latency_ms": {
                "samples": len(samples),
                "mean": sum(samples) / len(samples),
                "p50": _percentile(samples, 0.50),
                "p95": _percentile(samples, 0.95),
                "p99": _percentile(samples, 0.99),
            },
            "rankings": rankings[algorithm.name],
        }

    return {
        "schema_version": 1,
        "case_count": len(cases),
        "limit": limit,
        "ks": list(checked_ks),
        "warmups": warmups,
        "repeats": repeats,
        "seed": seed,
        "relevance_threshold": relevance_threshold,
        "positive_case_count": sum(not case.expect_no_results for case in cases),
        "negative_case_count": sum(case.expect_no_results for case in cases),
        "algorithms": algorithm_reports,
        "latency_comparison_groups": _latency_comparison_groups(algorithms),
    }


def _latency_comparison_groups(algorithms: Sequence[SearchAlgorithm]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for algorithm in algorithms:
        scope = getattr(algorithm, "latency_scope", "in_process")
        groups.setdefault(scope, []).append(algorithm.name)
    return groups


def build_blind_pool(
    cases: Sequence[BenchmarkCase],
    algorithms: Sequence[SearchAlgorithm],
    *,
    pool_depth: int = 20,
    seed: int = 0,
) -> list[dict]:
    """Pool unique candidates without exposing source algorithm or query text."""

    if pool_depth <= 0:
        raise ValueError("pool_depth must be positive")
    rng = random.Random(seed)
    pool: list[dict] = []
    for case in cases:
        drawer_ids = _unique_ids(
            hit.drawer_id for algorithm in algorithms for hit in algorithm.search(case, pool_depth)
        )
        rng.shuffle(drawer_ids)
        pool.append({"id": case.id, "drawer_ids": drawer_ids})
    return pool


def _validated_ks(ks: Sequence[int]) -> tuple[int, ...]:
    checked = tuple(sorted(set(ks)))
    if not checked or any(isinstance(k, bool) or not isinstance(k, int) or k <= 0 for k in checked):
        raise ValueError("ks must contain positive integers")
    return checked


def _unique_ids(drawer_ids) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for drawer_id in drawer_ids:
        if drawer_id and drawer_id not in seen:
            seen.add(drawer_id)
            unique.append(drawer_id)
    return unique


def _first_nested(result, field: str) -> list:
    value = getattr(result, field, None)
    if value is None and isinstance(result, dict):
        value = result.get(field)
    return list(value[0]) if value and value[0] else []


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def main(argv: Sequence[str] | None = None) -> int:
    """CLI for initializing, pooling, and running a private Palace benchmark."""

    import argparse

    parser = argparse.ArgumentParser(
        description="Benchmark Palace search algorithms locally without persisting drawer text."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create a private JSONL query template")
    init_parser.add_argument("--dataset", type=Path, required=True)

    for command in ("pool", "run"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--dataset", type=Path, required=True)
        command_parser.add_argument("--out", type=Path, required=True)
        command_parser.add_argument("--palace")
        command_parser.add_argument("--collection")
        command_parser.add_argument("--backend")
        command_parser.add_argument(
            "--algorithms",
            default="vector,bm25,current,rrf,weighted_rrf",
        )
        command_parser.add_argument("--candidate-depth", type=int, default=50)
        command_parser.add_argument("--rank-constant", type=int, default=60)
        command_parser.add_argument("--vector-weight", type=float, default=0.6)
        command_parser.add_argument("--bm25-weight", type=float, default=0.4)
        command_parser.add_argument("--max-distance", type=float, default=1.5)
        command_parser.add_argument(
            "--allow-direct-product-path",
            action="store_true",
            help="allow current/union to open the product path directly; use only without live writes",
        )
        command_parser.add_argument("--tag", help="only include cases carrying this tag")
        command_parser.add_argument("--seed", type=int, default=42)

    pool_parser = subparsers.choices["pool"]
    pool_parser.add_argument("--pool-depth", type=int, default=20)

    run_parser = subparsers.choices["run"]
    run_parser.add_argument("--limit", type=int, default=10)
    run_parser.add_argument("--ks", default="1,5,10")
    run_parser.add_argument("--warmups", type=int, default=1)
    run_parser.add_argument("--repeats", type=int, default=7)
    run_parser.add_argument("--relevance-threshold", type=int, default=2)

    args = parser.parse_args(argv)
    if args.command == "init":
        return _init_dataset(args.dataset)

    from mempalace.config import MempalaceConfig

    config = MempalaceConfig()
    cases = load_benchmark_cases(args.dataset)
    if args.command == "run":
        if args.tag not in {"dev", "test"}:
            parser.error("run requires --tag dev or --tag test")
        overlapping = [case.id for case in cases if {"dev", "test"}.issubset(case.tags)]
        if overlapping:
            parser.error("cases cannot belong to both dev and test: " + ", ".join(overlapping[:5]))
    if args.tag:
        cases = [case for case in cases if args.tag in case.tags]
        if not cases:
            parser.error(f"no cases carry tag {args.tag!r}")
    palace_path = args.palace or config.palace_path
    collection_name = args.collection or config.collection_name
    source = LocalPalaceSearchSource(
        palace_path,
        collection_name=collection_name,
        backend=args.backend,
        max_distance=args.max_distance,
        allow_direct_product_path=args.allow_direct_product_path,
    )
    algorithm_names = tuple(name.strip() for name in args.algorithms.split(",") if name.strip())
    algorithms = build_search_algorithms(
        source,
        names=algorithm_names,
        candidate_depth=args.candidate_depth,
        rank_constant=args.rank_constant,
        vector_weight=args.vector_weight,
        bm25_weight=args.bm25_weight,
    )

    start_state = source.snapshot_metadata()
    if args.command == "pool":
        output = build_blind_pool(
            cases,
            algorithms,
            pool_depth=args.pool_depth,
            seed=args.seed,
        )
    else:
        try:
            ks = tuple(int(value.strip()) for value in args.ks.split(",") if value.strip())
        except ValueError as exc:
            parser.error(f"--ks must be comma-separated integers: {exc}")
        output = run_benchmark(
            cases,
            algorithms,
            limit=args.limit,
            ks=ks,
            warmups=args.warmups,
            repeats=args.repeats,
            seed=args.seed,
            relevance_threshold=args.relevance_threshold,
        )
        output["algorithm_config"] = {
            "names": list(algorithm_names),
            "candidate_depth": args.candidate_depth,
            "rank_constant": args.rank_constant,
            "vector_weight": args.vector_weight,
            "bm25_weight": args.bm25_weight,
            "max_distance": args.max_distance,
        }

    end_state = source.snapshot_metadata(refresh=True)
    changed = corpus_changed(start_state, end_state)
    if isinstance(output, dict):
        output["corpus"] = {
            "start": start_state,
            "end": end_state,
            "changed_during_run": changed,
        }
        output["valid"] = not changed
    _write_private_text(args.out, json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {args.command} output to {args.out}")
    if changed:
        print("Benchmark invalid: Palace changed during the run")
        return 3
    return 0


def corpus_changed(start: Mapping, end: Mapping) -> bool:
    """Return whether public backend state indicates writes during a run."""

    if start.get("drawer_count") != end.get("drawer_count"):
        return True
    return start.get("maintenance") != end.get("maintenance")


def _init_dataset(path: Path) -> int:
    example = {
        "id": "q-001",
        "query": "Replace this with a real question you would ask your Palace",
        "judgments": {},
        "filters": {},
        "tags": ["dev", "replace-me"],
        "expect_no_results": False,
    }
    _write_private_text(
        path,
        json.dumps(example, ensure_ascii=False) + "\n",
        refuse_existing=True,
    )
    print(f"Wrote private benchmark template to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
