"""Tests for ``read_secret_env``: secrets from ``<NAME>_FILE`` or the environment (#2626)."""

import sys

import pytest

import mempalace.secret_env as secret_env_module
from mempalace.secret_env import SecretEnvError, read_secret_env, secret_file_env

NAME = "MEMPALACE_TEST_SECRET"
FILE_VAR = NAME + "_FILE"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(NAME, raising=False)
    monkeypatch.delenv(FILE_VAR, raising=False)


def _secret_file(tmp_path, data: bytes, name="secret"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_file_var_name():
    assert secret_file_env(NAME) == FILE_VAR


def test_neither_set_returns_none():
    assert read_secret_env(NAME) is None


def test_env_value_returned_unchanged_without_file(monkeypatch):
    monkeypatch.setenv(NAME, " value-with-spaces ")
    assert read_secret_env(NAME) == " value-with-spaces "


@pytest.mark.parametrize("data", [b"s3cret", b"s3cret\n", b"s3cret\r\n", b"s3cret\n\n"])
def test_file_value_used_and_trailing_newlines_dropped(tmp_path, monkeypatch, data):
    monkeypatch.setenv(FILE_VAR, str(_secret_file(tmp_path, data)))
    assert read_secret_env(NAME) == "s3cret"


def test_file_value_keeps_inner_content(tmp_path, monkeypatch):
    monkeypatch.setenv(FILE_VAR, str(_secret_file(tmp_path, b"a b\tc\n")))
    assert read_secret_env(NAME) == "a b\tc"


def test_blank_file_var_counts_as_unset(monkeypatch):
    monkeypatch.setenv(FILE_VAR, "   ")
    monkeypatch.setenv(NAME, "from-env")
    assert read_secret_env(NAME) == "from-env"


def test_blank_env_beside_file_is_not_a_conflict(tmp_path, monkeypatch):
    # A compose file that always defines `NAME: ${NAME:-}` sets NAME to "".
    monkeypatch.setenv(NAME, "")
    monkeypatch.setenv(FILE_VAR, str(_secret_file(tmp_path, b"from-file\n")))
    assert read_secret_env(NAME) == "from-file"


def test_both_set_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(NAME, "from-env")
    monkeypatch.setenv(FILE_VAR, str(_secret_file(tmp_path, b"from-file")))
    with pytest.raises(SecretEnvError) as excinfo:
        read_secret_env(NAME)
    message = str(excinfo.value)
    assert NAME in message and FILE_VAR in message
    assert "from-env" not in message and "from-file" not in message


def test_unreadable_file_is_an_error_not_a_fallback(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    monkeypatch.setenv(FILE_VAR, str(missing))
    with pytest.raises(SecretEnvError) as excinfo:
        read_secret_env(NAME)
    assert FILE_VAR in str(excinfo.value)
    # The message shows the path as repr, which doubles Windows backslashes.
    assert repr(str(missing)) in str(excinfo.value)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows drops trailing spaces from a path before opening it",
)
def test_file_path_is_used_exactly(tmp_path, monkeypatch):
    # The path is not trimmed: a stray space names a different file, and the
    # error shows it (repr) rather than silently opening the trimmed name.
    _secret_file(tmp_path, b"s3cret", name="token")
    path = str(tmp_path / "token") + " "
    monkeypatch.setenv(FILE_VAR, path)
    with pytest.raises(SecretEnvError) as excinfo:
        read_secret_env(NAME)
    assert f"{FILE_VAR}={path!r} could not be read" in str(excinfo.value)


def test_file_path_is_passed_to_open_untrimmed(monkeypatch):
    # The test above needs a filesystem that keeps trailing spaces; this one
    # checks the argument handed to open() itself, so it runs on Windows too.
    path = "/run/secrets/token "
    opened = []

    def fake_open(file, mode="r", *args, **kwargs):
        opened.append((file, mode))
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(secret_env_module, "open", fake_open, raising=False)
    monkeypatch.setenv(FILE_VAR, path)
    with pytest.raises(SecretEnvError):
        read_secret_env(NAME)
    assert opened == [(path, "rb")]


def test_directory_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv(FILE_VAR, str(tmp_path))
    with pytest.raises(SecretEnvError):
        read_secret_env(NAME)


@pytest.mark.parametrize("data", [b"", b"\n", b"  \r\n"])
def test_empty_file_is_an_error(tmp_path, monkeypatch, data):
    monkeypatch.setenv(FILE_VAR, str(_secret_file(tmp_path, data)))
    with pytest.raises(SecretEnvError, match="empty"):
        read_secret_env(NAME)


def test_non_utf8_file_error_does_not_quote_the_secret(tmp_path, monkeypatch):
    monkeypatch.setenv(FILE_VAR, str(_secret_file(tmp_path, b"ok-prefix-\xff\xfe")))
    with pytest.raises(SecretEnvError, match="UTF-8") as excinfo:
        read_secret_env(NAME)
    assert "ok-prefix" not in str(excinfo.value)
    assert "0xff" not in str(excinfo.value)
    # No chained decode error, which would hold the secret's bytes.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_error_is_a_value_error():
    # Startup paths that already report a bad setting cleanly catch ValueError.
    assert issubclass(SecretEnvError, ValueError)
