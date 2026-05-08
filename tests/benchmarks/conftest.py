"""Benchmark-specific pytest configuration, fixtures, and CLI options."""

import json
import os
import tempfile

import pytest


SCALE_OPTIONS = ["small", "medium", "large", "stress"]


def pytest_addoption(parser):
    parser.addoption(
        "--bench-scale",
        default="small",
        choices=SCALE_OPTIONS,
        help="Scale level for benchmark tests: small (1K), medium (10K), large (50K), stress (100K)",
    )
    parser.addoption(
        "--bench-report",
        default=None,
        help="Path for JSON benchmark report output",
    )


@pytest.fixture(scope="session")
def bench_scale(request):
    """The configured benchmark scale level."""
    return request.config.getoption("--bench-scale")


@pytest.fixture(scope="session")
def bench_report_path(request):
    """Path for JSON report output, or None."""
    return request.config.getoption("--bench-report")


@pytest.fixture
def palace_dir(tmp_path):
    """Isolated palace directory for a single test."""
    p = tmp_path / "palace"
    p.mkdir()
    return str(p)


@pytest.fixture
def kg_db(tmp_path):
    """Isolated KG SQLite path for a single test."""
    return str(tmp_path / "test_kg.sqlite3")


@pytest.fixture
def config_dir(tmp_path):
    """Isolated config directory for monkeypatching MempalaceConfig."""
    d = tmp_path / "config"
    d.mkdir()
    config = {"palace_path": str(tmp_path / "palace"), "collection_name": "mempalace_drawers"}
    with open(d / "config.json", "w") as f:
        json.dump(config, f)
    return str(d)


@pytest.fixture
def project_dir(tmp_path):
    """Temporary project directory for mining tests."""
    d = tmp_path / "project"
    d.mkdir()
    return d


# ── Session-scoped result collector ──────────────────────────────────────


class BenchmarkResults:
    """Collect benchmark metrics across all tests in a session."""

    def __init__(self):
        self.results = {}

    def record(self, category: str, metric: str, value):
        if category not in self.results:
            self.results[category] = {}
        self.results[category][metric] = value


@pytest.fixture(scope="session")
def bench_results():
    """Session-scoped results collector shared by all benchmark tests."""
    return BenchmarkResults()


def pytest_terminal_summary(terminalreporter, config):
    """Write JSON benchmark report after all tests complete."""
    report_path = config.getoption("--bench-report", default=None)
    if not report_path:
        return

    # Collect results written by individual tests via record_metric()
    import platform
    import subprocess

    try:
        git_sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        git_sha = "unknown"

    try:
        import chromadb

        chromadb_version = chromadb.__version__
    except Exception:
        chromadb_version = "unknown"

    report = {
        "timestamp": __import__("datetime").datetime.now().isoformat(),
        "git_sha": git_sha,
        "python_version": platform.python_version(),
        "chromadb_version": chromadb_version,
        "scale": config.getoption("--bench-scale", default="small"),
        "system": {
            "os": platform.system().lower(),
            "cpu_count": os.cpu_count(),
            "platform": platform.platform(),
        },
        "results": {},
    }

    # Read results from the temp file written by record_metric()
    results_file = os.path.join(tempfile.gettempdir(), "mempalace_bench_results.json")
    if os.path.exists(results_file):
        try:
            with open(results_file) as f:
                report["results"] = json.load(f)
            os.unlink(results_file)
        except Exception:
            pass

    os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    terminalreporter.write_line(f"\nBenchmark report written to: {report_path}")
