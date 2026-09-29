"""Credential selection and safe retry for local clients of a palace Hub."""

import io
import urllib.error
import urllib.request

import pytest

from mempalace import server_registry


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MEMPALACE_MCP_HTTP_TOKEN", raising=False)
    return tmp_path


def _write_palace_token(palace: str, token: str) -> None:
    path = server_registry.server_token_path(palace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")


def test_palace_token_precedes_distinct_process_token(isolated_home, monkeypatch):
    palace = str(isolated_home / "palace")
    _write_palace_token(palace, "palace-token")
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "process-token")

    assert server_registry.load_server_tokens(palace) == (
        "palace-token",
        "process-token",
    )
    assert server_registry.load_server_token(palace) == "palace-token"


def test_process_token_is_fallback_without_palace_token(isolated_home, monkeypatch):
    palace = str(isolated_home / "palace")
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "process-token")

    assert server_registry.load_server_tokens(palace) == ("process-token",)
    assert server_registry.load_server_token(palace) == "process-token"


def test_duplicate_tokens_are_attempted_once(isolated_home, monkeypatch):
    palace = str(isolated_home / "palace")
    _write_palace_token(palace, "same-token")
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "same-token")

    assert server_registry.load_server_tokens(palace) == ("same-token",)


def test_no_local_credentials_returns_empty_candidates(isolated_home):
    palace = str(isolated_home / "palace")

    assert server_registry.load_server_tokens(palace) == ()
    assert server_registry.load_server_token(palace) == ""


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def test_urlopen_retries_the_second_token_only_after_401(isolated_home, monkeypatch):
    palace = str(isolated_home / "palace")
    _write_palace_token(palace, "stale-token")
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "current-token")
    attempted = []

    def fake_urlopen(request, timeout):
        attempted.append((request.get_header("Authorization"), timeout))
        if len(attempted) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                {},
                io.BytesIO(),
            )
        return _Response(b"ok")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with server_registry.urlopen_with_server_tokens(
        palace,
        "http://127.0.0.1:8765/mcp",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        timeout=5,
    ) as response:
        assert response.read() == b"ok"

    assert attempted == [
        ("Bearer stale-token", 5),
        ("Bearer current-token", 5),
    ]


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError(
            "http://127.0.0.1:8765/mcp",
            500,
            "boom",
            {},
            io.BytesIO(),
        ),
        urllib.error.URLError("connection reset"),
    ],
)
def test_urlopen_does_not_retry_non_401_or_transport_failures(isolated_home, monkeypatch, error):
    palace = str(isolated_home / "palace")
    _write_palace_token(palace, "first-token")
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "second-token")
    attempted = []

    def fake_urlopen(request, timeout):
        attempted.append(request.get_header("Authorization"))
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(type(error)):
        server_registry.urlopen_with_server_tokens(
            palace,
            "http://127.0.0.1:8765/mcp",
            data=b"{}",
            timeout=5,
        )

    assert attempted == ["Bearer first-token"]
