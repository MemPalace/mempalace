"""A service entrypoint leaves MEMPALACE_PALACE_PATH as it found it."""

from __future__ import annotations

import os

import pytest

from mempalace import service

_ENV = "MEMPALACE_PALACE_PATH"


@pytest.fixture(autouse=True)
def _restore_env():
    before = os.environ.get(_ENV)
    yield
    if before is None:
        os.environ.pop(_ENV, None)
    else:
        os.environ[_ENV] = before


@pytest.mark.parametrize("entrypoint", ["run_mine", "run_sync", "run_diary_write"])
def test_entrypoint_restores_the_palace_path_it_stamped(entrypoint, tmp_path, monkeypatch):
    """`config.py` reads this variable with priority OVER the config file, so a stamp left
    standing makes the first call's palace the second caller's. One call per process never
    notices; any process making two does.

    The entrypoint is expected to fail on the throwaway path — the assertion covers what the
    environment looks like AFTER it returns or raises, which is the property at issue.
    """
    monkeypatch.setenv(_ENV, "/palace/the-caller-already-had")
    fn = getattr(service, entrypoint)

    try:
        fn({"palace_path": str(tmp_path / "throwaway"), "source": str(tmp_path)})
    except Exception:  # noqa: BLE001 - the call's outcome is not what this asserts
        pass

    assert os.environ.get(_ENV) == "/palace/the-caller-already-had"


def test_an_entrypoint_that_found_no_variable_leaves_none_behind(tmp_path, monkeypatch):
    """The unset case matters as much: a stamp left where nothing stood makes every later
    resolution in the process read a palace the caller never named."""
    monkeypatch.delenv(_ENV, raising=False)

    try:
        service.run_mine({"palace_path": str(tmp_path / "throwaway"), "source": str(tmp_path)})
    except Exception:  # noqa: BLE001
        pass

    assert _ENV not in os.environ
