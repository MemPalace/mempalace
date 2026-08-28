from mempalace.backends.qdrant import _QdrantConfig, _QdrantRESTClient
from mempalace.version import __version__


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b"{}"


def test_qdrant_rest_client_sends_versioned_user_agent(monkeypatch):
    import mempalace.backends.qdrant as qdrant

    captured = {}

    def fake_urlopen(request, timeout):
        captured["user_agent"] = request.get_header("User-agent")
        captured["content_type"] = request.get_header("Content-type")
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(qdrant.urlrequest, "urlopen", fake_urlopen)

    client = _QdrantRESTClient(_QdrantConfig(url="https://qdrant.example.invalid", timeout=7.5))

    assert client.request("GET", "/collections") == {}
    assert captured["user_agent"] == f"mempalace/{__version__}"
    assert captured["content_type"] == "application/json"
    assert captured["timeout"] == 7.5
