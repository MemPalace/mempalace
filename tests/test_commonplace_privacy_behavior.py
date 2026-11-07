"""Keep large-input privacy correctness checks independent of host speed."""

import runpy
from pathlib import Path
from types import SimpleNamespace


def test_near_miss_behavior_is_independent_of_clock(monkeypatch):
    checks = runpy.run_path(str(Path(__file__).with_name("test_commonplace_privacy.py")))
    behavior = checks["test_speed_on_10kb_and_delimiter_heavy_near_misses"]
    ticks = iter((0.0, 1.0))
    # Replace only this test module's clock, leaving pytest's clock untouched.
    monkeypatch.setitem(
        behavior.__globals__, "time", SimpleNamespace(perf_counter=lambda: next(ticks))
    )
    behavior()
    assert all(mark.name != "timing" for mark in getattr(behavior, "pytestmark", []))
