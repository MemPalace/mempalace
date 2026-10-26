import logging

import pytest

from mempalace._env import (
    env_bool,
    env_flag,
    env_float,
    env_int,
    env_list,
    env_str,
    parse_bool,
    parse_number,
)

VAR = "MEMPALACE_TEST_ENV_VAR"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "Y", "on"])
def test_truthy_vocabulary(monkeypatch, raw):
    monkeypatch.setenv(VAR, raw)
    assert env_bool(VAR, False) is True


@pytest.mark.parametrize("raw", ["0", "false", "No", " n ", "OFF"])
def test_falsy_vocabulary(monkeypatch, raw):
    monkeypatch.setenv(VAR, raw)
    assert env_bool(VAR, True) is False


@pytest.mark.parametrize("default", [True, False])
def test_unset_and_blank_use_default(monkeypatch, default):
    assert env_bool(VAR, default) is default
    monkeypatch.setenv(VAR, "   ")
    assert env_bool(VAR, default) is default
    assert env_flag(VAR) is None


def test_unrecognised_flag_warns_once_and_uses_default(monkeypatch, caplog):
    monkeypatch.setenv(VAR, "maybe")
    with caplog.at_level(logging.WARNING, logger="mempalace._env"):
        assert env_bool(VAR, True) is True
        assert env_bool(VAR, False) is False
        assert env_flag(VAR) is None
    warnings = [r for r in caplog.records if VAR in r.getMessage()]
    assert len(warnings) == 1
    assert "not recognized" in warnings[0].getMessage()


def test_numbers(monkeypatch):
    monkeypatch.setenv(VAR, " 42 ")
    assert env_int(VAR, 7) == 42
    assert env_float(VAR, 1.5) == 42.0
    monkeypatch.setenv(VAR, "2.5")
    assert env_int(VAR, 7) == 7
    assert env_float(VAR, 1.5) == 2.5


@pytest.mark.parametrize("raw", ["soon", "inf", "-inf", "nan", ""])
def test_bad_float_uses_default(monkeypatch, raw):
    monkeypatch.setenv(VAR, raw)
    assert env_float(VAR, 3.0) == 3.0


def test_bad_number_warns_once(monkeypatch, caplog):
    monkeypatch.setenv(VAR, "lots")
    with caplog.at_level(logging.WARNING, logger="mempalace._env"):
        env_int(VAR, 1)
        env_int(VAR, 1)
    assert len([r for r in caplog.records if VAR in r.getMessage()]) == 1


def test_env_str(monkeypatch):
    assert env_str(VAR) is None
    assert env_str(VAR, "x") == "x"
    monkeypatch.setenv(VAR, "  ")
    assert env_str(VAR, "x") == "x"
    monkeypatch.setenv(VAR, " value ")
    assert env_str(VAR) == "value"


def test_env_list(monkeypatch):
    assert env_list(VAR) == []
    monkeypatch.setenv(VAR, " a, ,b ,, c ")
    assert env_list(VAR) == ["a", "b", "c"]


def test_parse_helpers():
    assert parse_bool(True) is True
    assert parse_bool(False) is False
    assert parse_bool(None) is None
    assert parse_bool("On") is True
    assert parse_bool("2") is None
    assert parse_number(True) is None
    assert parse_number("12", int) == 12
    assert parse_number("1e3") == 1000.0
    assert parse_number(float("inf")) is None
    assert parse_number("x") is None
