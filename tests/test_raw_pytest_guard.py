"""The shared-host guard in conftest: raw pytest runs are refused only where the host opts in."""

from conftest import _raw_pytest_refusal


def _home(tmp_path, variable):
    if variable is not None:
        (tmp_path / ".config" / "commonplace").mkdir(parents=True)
        (tmp_path / ".config" / "commonplace" / "require-test-runner").write_text(variable + "\n")
    return str(tmp_path)


def test_no_marker_allows_a_raw_run(tmp_path):
    assert _raw_pytest_refusal({}, _home(tmp_path, None)) is None


def test_marker_refuses_a_run_without_the_runner_variable(tmp_path):
    refusal = _raw_pytest_refusal({}, _home(tmp_path, "RUNNER_SLOT"))
    assert refusal is not None and "test runner" in refusal


def test_marker_allows_a_run_the_runner_admitted(tmp_path):
    assert _raw_pytest_refusal({"RUNNER_SLOT": "1"}, _home(tmp_path, "RUNNER_SLOT")) is None


def test_xdist_workers_and_the_override_pass(tmp_path):
    home = _home(tmp_path, "RUNNER_SLOT")
    assert _raw_pytest_refusal({"PYTEST_XDIST_WORKER": "gw0"}, home) is None
    assert _raw_pytest_refusal({"COMMONPLACE_ALLOW_RAW_PYTEST": "1"}, home) is None
    assert _raw_pytest_refusal({"COMMONPLACE_ALLOW_RAW_PYTEST": "yes"}, home) is not None


def test_an_empty_marker_refuses_nothing(tmp_path):
    assert _raw_pytest_refusal({}, _home(tmp_path, "")) is None
