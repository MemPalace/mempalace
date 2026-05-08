"""
Benchmark report utilities — JSON output and regression detection.

Each test records metrics via record_metric(). At session end, the
conftest.py pytest_terminal_summary hook writes the collected results.
"""

import json
import os
import tempfile


RESULTS_FILE = os.path.join(tempfile.gettempdir(), "mempalace_bench_results.json")


def record_metric(category: str, metric: str, value):
    """Append a metric to the session results file (JSON on disk)."""
    results = {}
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE) as f:
                results = json.load(f)
        except (json.JSONDecodeError, OSError):
            results = {}

    if category not in results:
        results[category] = {}
    results[category][metric] = value

    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)


def check_regression(current_report: str, baseline_report: str, threshold: float = 0.2):
    """
    Compare current benchmark results against a baseline.

    Returns a list of regression descriptions. Empty list = no regressions.

    threshold: fractional degradation allowed (0.2 = 20% worse is OK).
    """
    with open(current_report) as f:
        current = json.load(f)
    with open(baseline_report) as f:
        baseline = json.load(f)

    regressions = []
    # Keywords for metric direction — checked in order, first match wins.
    # "improvement" is checked before "latency" so that composite names
    # like "latency_improvement_pct" are classified correctly.
    _higher_is_better_kw = [
        "improvement",
        "recall",
        "throughput",
        "per_sec",
        "files_per_sec",
        "drawers_per_sec",
        "triples_per_sec",
        "speedup",
    ]
    _higher_is_worse_kw = [
        "latency",
        "rss",
        "memory",
        "oom",
        "lock_failures",
        "elapsed",
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "rss_delta_mb",
        "peak_rss_mb",
        "errors",
        "failures",
    ]

    def _metric_direction(name: str) -> str:
        """Return 'higher_better', 'higher_worse', or 'unknown'."""
        low = name.lower()
        for kw in _higher_is_better_kw:
            if kw in low:
                return "higher_better"
        for kw in _higher_is_worse_kw:
            if kw in low:
                return "higher_worse"
        return "unknown"

    for category in baseline.get("results", {}):
        if category not in current.get("results", {}):
            continue
        for metric, base_val in baseline["results"][category].items():
            if metric not in current["results"][category]:
                continue
            curr_val = current["results"][category][metric]
            if not isinstance(base_val, (int, float)) or not isinstance(curr_val, (int, float)):
                continue
            if base_val == 0:
                continue

            direction = _metric_direction(metric)

            if direction == "higher_worse":
                # Higher is worse — check if current exceeds baseline by threshold
                if curr_val > base_val * (1 + threshold):
                    pct = ((curr_val - base_val) / base_val) * 100
                    regressions.append(
                        f"{category}/{metric}: {base_val:.2f} -> {curr_val:.2f} ({pct:+.1f}%, threshold {threshold * 100:.0f}%)"
                    )
            elif direction == "higher_better":
                # Lower is worse — check if current is below baseline by threshold
                if curr_val < base_val * (1 - threshold):
                    pct = ((curr_val - base_val) / base_val) * 100
                    regressions.append(
                        f"{category}/{metric}: {base_val:.2f} -> {curr_val:.2f} ({pct:+.1f}%, threshold {threshold * 100:.0f}%)"
                    )

    return regressions
