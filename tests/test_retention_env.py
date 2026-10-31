# -*- coding: utf-8 -*-
"""Regression test for #2673 (proposed).

Pins the load-bearing contract: `import mempalace.daemon` must never raise on
a non-integer MEMPALACE_DAEMON_RETENTION_DAYS. The module-level
JOB_RETENTION_DAYS read must fall back to the 7-day default for bad values,
while honoring intentional operator windows (0, -1, signed/whitespace ints).
"""

import importlib

import pytest

import mempalace.daemon as _daemon

VAR = "MEMPALACE_DAEMON_RETENTION_DAYS"


def _reload():
    return importlib.reload(_daemon)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    yield
    importlib.reload(_daemon)  # restore original module-level read


@pytest.mark.parametrize(
    "raw,val",
    [
        ("abc", 7),  # non-numeric typo -> default (was: ValueError)
        ("12.7", 7),  # float -> int fallback (was: ValueError)
        ("30d", 7),  # unit-suffixed -> default (issue example)
        (" ", 7),  # whitespace-only (issue: trailing space in .env)
        ("not-a-float", 7),
        ("0", 0),  # intentional: keep terminal jobs
        ("-1", -1),  # intentional: keep terminal jobs
        (" 42 ", 42),  # leading/trailing whitespace is fine
    ],
)
def test_retention_days_module_read_never_crashes(monkeypatch, raw, val):
    monkeypatch.setenv(VAR, raw)
    d = _reload()
    assert d.JOB_RETENTION_DAYS == val


def test_unset_uses_default(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    d = _reload()
    assert d.JOB_RETENTION_DAYS == 7
