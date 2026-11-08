"""Keep the Admin-only missing-runner exception explicit in its documentation."""

from pathlib import Path

from commonplace.projects.orchestration import usage

ROOT = Path(__file__).resolve().parents[1]


def test_module_promises_do_not_allow_missing_runner_for_normal_admission():
    modules = (ROOT / "MODULES.md").read_text()
    assert "An absent runner connection or optional jobs/config table supplies" not in modules
    assert "Admission defaults retain strict runner input and shared accounting." in modules


def test_usage_docstring_limits_missing_runner_to_explicit_opt_in():
    doc = " ".join(usage.__doc__.split())
    assert (
        "With allow_missing_runner=True, absent runner input supplies no jobs/config override; "
        "otherwise absent runner input raises."
    ) in doc
