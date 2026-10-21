"""Credential selection and safe retry for local clients of a palace Hub."""

import io
import urllib.error
import urllib.request

import pytest

from mempalace import server_registry
from mempalace.hub_client import HubClient


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


def test_process_token_is_fallback_without_palace_token(isolated_home, monkeypatch):
    palace = str(isolated_home / "palace")
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "process-token")

    assert server_registry.load_server_tokens(palace) == ("process-token",)


def test_duplicate_tokens_are_attempted_once(isolated_home, monkeypatch):
    palace = str(isolated_home / "palace")
    _write_palace_token(palace, "same-token")
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "same-token")

    assert server_registry.load_server_tokens(palace) == ("same-token",)


def test_no_local_credentials_returns_empty_candidates(isolated_home):
    palace = str(isolated_home / "palace")

    assert server_registry.load_server_tokens(palace) == ()


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def test_hub_retries_the_second_token_only_after_401(isolated_home, monkeypatch):
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
        return _Response(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    hub = HubClient(palace, {"host": "127.0.0.1", "port": 8765})
    assert hub.post({}, timeout=5) == {"ok": True}

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
def test_hub_does_not_retry_non_401_or_transport_failures(isolated_home, monkeypatch, error):
    palace = str(isolated_home / "palace")
    _write_palace_token(palace, "first-token")
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "second-token")
    attempted = []

    def fake_urlopen(request, timeout):
        attempted.append(request.get_header("Authorization"))
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(type(error)):
        HubClient(palace, {"host": "127.0.0.1", "port": 8765}).post({}, timeout=5)

    assert attempted == ["Bearer first-token"]
