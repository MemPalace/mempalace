"""Summary correctness must be independent of injection deadline headroom."""

import runpy
from pathlib import Path

from commonplace.rules import checks, render, service
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_rule_summaries import fresh  # noqa: F401


def test_owner_summary_update_survives_scheduler_delay(hub, fresh, monkeypatch):  # noqa: F811
    functional = runpy.run_path(
        str(Path(__file__).with_name("test_commonplace_rule_summaries.py"))
    )["test_owner_rest_summary_updates_leave_injection_and_acceptance_unchanged"]
    now, rendered, responses = [0.0], [], []
    monkeypatch.setattr(checks, "monotonic", lambda: now[0])
    instructions = render.instructions
    instructions_for = functional.__globals__["instructions_for"]

    def delayed(*args, **kwargs):
        text = instructions(*args, **kwargs)
        rendered.append(text)
        now[0] += service.INSTRUCTIONS_TIMEOUT + 0.001
        return text

    def tracked(ctx):
        text = instructions_for(ctx)
        responses.append(text)
        return text

    monkeypatch.setattr(render, "instructions", delayed)
    monkeypatch.setitem(functional.__globals__, "instructions_for", tracked)
    if "monkeypatch" in functional.__code__.co_varnames:
        functional(hub, fresh, monkeypatch)
    else:
        functional(hub, fresh)
    quoted = render.quote("Keep the exact rule text.\n  Preserve its spacing and context.")
    assert len(rendered) == len(responses) == 2
    assert responses == rendered
    assert all(quoted in text for text in responses)
