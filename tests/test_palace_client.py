"""palace_client unit tests: env resolution, unreachable-server
structured errors, and the hard per-operation timeout.

These tests never need a running Chroma server — the unreachable cases use
port 1 (nothing listens there; connect fails fast) and the timeout cases use
plain slow callables.
"""

import time

import pytest

from mempalace import palace_client
from mempalace.palace_client import (
    PalaceBackendUnreachableError,
    PalaceOperationTimeoutError,
    TimeoutProxy,
    chroma_host,
    chroma_port,
    http_mode,
    make_http_client,
    probe_backend,
    run_with_timeout,
)

DEAD_PORT = "1"


class TestEnvResolution:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("CHROMA_HOST", raising=False)
        monkeypatch.delenv("CHROMA_PORT", raising=False)
        assert chroma_host() == "127.0.0.1"
        assert chroma_port() == 8801
        assert palace_client.backend_address() == "127.0.0.1:8801"

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("CHROMA_HOST", "10.0.0.5")
        monkeypatch.setenv("CHROMA_PORT", "8805")
        assert chroma_host() == "10.0.0.5"
        assert chroma_port() == 8805
        assert palace_client.backend_address() == "10.0.0.5:8805"

    def test_invalid_port_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CHROMA_PORT", "not-a-port")
        assert chroma_port() == 8801

    def test_embedded_is_default_and_http_opts_in(self, monkeypatch):
        monkeypatch.delenv("MEMPALACE_CHROMA_MODE", raising=False)
        assert http_mode() is False
        monkeypatch.setenv("MEMPALACE_CHROMA_MODE", "http")
        assert http_mode() is True
        monkeypatch.setenv("MEMPALACE_CHROMA_MODE", "HTTP")
        assert http_mode() is True
        monkeypatch.setenv("MEMPALACE_CHROMA_MODE", "embedded")
        assert http_mode() is False

    def test_op_timeout_env_override(self, monkeypatch):
        monkeypatch.delenv("MEMPALACE_OP_TIMEOUT", raising=False)
        assert palace_client.op_timeout_s() == palace_client.DEFAULT_OP_TIMEOUT_S
        monkeypatch.setenv("MEMPALACE_OP_TIMEOUT", "2.5")
        assert palace_client.op_timeout_s() == 2.5
        monkeypatch.setenv("MEMPALACE_OP_TIMEOUT", "garbage")
        assert palace_client.op_timeout_s() == palace_client.DEFAULT_OP_TIMEOUT_S


class TestUnreachableServer:
    def test_probe_raises_structured_error_fast(self, monkeypatch):
        monkeypatch.setenv("CHROMA_HOST", "127.0.0.1")
        monkeypatch.setenv("CHROMA_PORT", DEAD_PORT)
        start = time.monotonic()
        with pytest.raises(PalaceBackendUnreachableError) as excinfo:
            probe_backend()
        elapsed = time.monotonic() - start
        assert elapsed < 4.0, f"probe took {elapsed:.1f}s; must fail within the 3s ceiling"
        assert "palace backend unreachable at 127.0.0.1:1" in str(excinfo.value)

    def test_make_http_client_raises_unreachable(self, monkeypatch):
        monkeypatch.setenv("CHROMA_HOST", "127.0.0.1")
        monkeypatch.setenv("CHROMA_PORT", DEAD_PORT)
        with pytest.raises(PalaceBackendUnreachableError):
            make_http_client()

    def test_backend_make_client_routes_http_and_surfaces_unreachable(self, monkeypatch):
        from mempalace.backends.chroma import ChromaBackend

        monkeypatch.setenv("MEMPALACE_CHROMA_MODE", "http")
        monkeypatch.setenv("CHROMA_HOST", "127.0.0.1")
        monkeypatch.setenv("CHROMA_PORT", DEAD_PORT)
        with pytest.raises(PalaceBackendUnreachableError):
            ChromaBackend.make_client("/tmp/does-not-matter-in-http-mode")


class TestOperationTimeout:
    def test_timeout_fires_and_names_operation(self):
        start = time.monotonic()
        with pytest.raises(PalaceOperationTimeoutError) as excinfo:
            run_with_timeout("collection.query", time.sleep, 5, timeout_s=0.2)
        assert time.monotonic() - start < 2.0
        assert "collection.query" in str(excinfo.value)

    def test_result_passthrough(self):
        assert run_with_timeout("ok", lambda: 42) == 42

    def test_exception_passthrough_unchanged(self):
        def boom():
            raise ValueError("original error")

        with pytest.raises(ValueError, match="original error"):
            run_with_timeout("boom", boom)


class TestTimeoutProxy:
    class _FakeCollection:
        name = "fake"

        def query(self):
            return "queried"

        def upsert(self):
            return "upserted"

        def get(self):
            return "got"

        def count(self):
            return 7

        def slow(self):
            time.sleep(5)
            return "late"

    def test_attributes_pass_through_and_methods_work(self):
        proxy = TimeoutProxy(self._FakeCollection(), label="t")
        assert proxy.name == "fake"
        assert proxy.count() == 7

    def test_slow_method_hits_ceiling(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_OP_TIMEOUT", "0.2")
        proxy = TimeoutProxy(self._FakeCollection(), label="t")
        with pytest.raises(PalaceOperationTimeoutError) as excinfo:
            proxy.slow()
        assert "t.slow" in str(excinfo.value)

    def test_collection_shaped_returns_get_wrapped(self):
        fake = self._FakeCollection()

        class FakeClient:
            def get_collection(self, name):
                return fake

        proxy = TimeoutProxy(FakeClient(), label="chroma")
        col = proxy.get_collection("fake")
        assert isinstance(col, TimeoutProxy)
        assert col.query() == "queried"
