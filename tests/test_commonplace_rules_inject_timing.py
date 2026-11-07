"""The initialization benchmark must tolerate the documented deadline fallback."""

import runpy
from pathlib import Path

from commonplace.rules import checks, render, service
from tests.test_commonplace_rules_inject import (  # noqa: F401
    LEGACY,
    local,
    rules_surface_only,
)


def test_500_rule_timing_tolerates_expired_injection(local, monkeypatch):  # noqa: F811
    benchmark = runpy.run_path(str(Path(__file__).with_name("test_commonplace_rules_inject.py")))[
        "test_500_rule_initialize_p95_over_empty_store"
    ]
    now, rendered, responses = [0.0], [], []
    monkeypatch.setattr(checks, "monotonic", lambda: now[0])
    instructions = render.instructions
    initialize = benchmark.__globals__["initialize"]

    def expired(*args, **kwargs):
        text = instructions(*args, **kwargs)
        rendered.append(text)
        now[0] += service.INSTRUCTIONS_TIMEOUT + 0.001
        return text

    def tracked(local, worker=None):
        response = initialize(local, worker)
        responses.append(response)
        return response

    monkeypatch.setattr(render, "instructions", expired)
    monkeypatch.setitem(benchmark.__globals__, "initialize", tracked)
    # Isolate the benchmark's behavior from its informational p95 assertion.
    monkeypatch.setitem(benchmark.__globals__, "perf_counter", lambda: 0.0)
    benchmark(local, lambda *_: None)
    assert rendered and all("incomplete" in text for text in rendered)
    assert responses and all(response == LEGACY for response in responses)
