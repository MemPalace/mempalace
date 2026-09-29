"""
test_transport.py — The RFC 004 transport seam.

Covers the seam contract (pull half implemented, push half explicitly
deferred to the MeshGuard binding), the HTTPS-bearer implementation's
peers/self_id/request wiring, the factory swap point, and the
compatibility surface old importers rely on (logsync re-exports,
SyncPeerError identity).
"""

import json
import os

import pytest

from mempalace.transport import (
    HttpsBearerTransport,
    Transport,
    TransportError,
    get_transport,
    load_peers,
)


@pytest.fixture
def peered_palace(palace_path):
    with open(os.path.join(palace_path, "peers.json"), "w", encoding="utf-8") as f:
        json.dump(
            {"peers": [{"name": "windows", "url": "http://peer.example:8765", "token": "tok"}]},
            f,
        )
    return palace_path


class TestSeamContract:
    def test_base_is_abstract(self):
        with pytest.raises(TypeError):
            Transport()

    def test_push_half_defers_to_meshguard_binding(self, peered_palace):
        transport = HttpsBearerTransport(peered_palace)
        peer = transport.peers()[0]
        for call in (
            lambda: transport.open_stream(peer, "/logstream/stream"),
            lambda: transport.on_presence_change(lambda *_: None),
            lambda: transport.on_inbound("/sync/ops", lambda *_: None),
        ):
            with pytest.raises(NotImplementedError, match="MeshGuard"):
                call()


class TestHttpsBearerTransport:
    def test_self_id_is_the_replica_id(self, palace_path):
        from mempalace.replica import get_replica_id

        transport = HttpsBearerTransport(palace_path)
        assert transport.self_id() == get_replica_id(palace_path)

    def test_peers_reads_peers_json(self, peered_palace):
        transport = HttpsBearerTransport(peered_palace)
        assert transport.peers() == load_peers(peered_palace)
        assert transport.peers()[0]["name"] == "windows"

    def test_peers_empty_without_config(self, palace_path):
        assert HttpsBearerTransport(palace_path).peers() == []

    def test_request_routes_through_wire_primitive(self, peered_palace, monkeypatch):
        import mempalace.transport as transport_mod

        calls = []

        def fake_http_request(url, token, path, params=None):
            calls.append((url, token, path, params))
            return {"ok": True}

        monkeypatch.setattr(transport_mod, "http_request", fake_http_request)
        transport = HttpsBearerTransport(peered_palace)
        peer = transport.peers()[0]
        assert transport.request(peer, "/sync/version_vector", {"a": 1}) == {"ok": True}
        assert calls == [("http://peer.example:8765", "tok", "/sync/version_vector", {"a": 1})]

    def test_request_unreachable_raises_transport_error(self, peered_palace):
        transport = HttpsBearerTransport(peered_palace)
        with pytest.raises(TransportError):
            transport.request({"url": "http://127.0.0.1:1", "token": ""}, "/sync/version_vector")


class TestFactorySwapPoint:
    def test_default_is_https(self, palace_path, monkeypatch):
        monkeypatch.delenv("MEMPALACE_TRANSPORT", raising=False)
        assert isinstance(get_transport(palace_path), HttpsBearerTransport)

    def test_meshguard_is_reserved_and_loud(self, palace_path, monkeypatch):
        # A user who asked for mesh-identity auth must never silently
        # run on bearer tokens instead.
        monkeypatch.setenv("MEMPALACE_TRANSPORT", "meshguard")
        with pytest.raises(NotImplementedError, match="[Mm]esh[Gg]uard"):
            get_transport(palace_path)

    def test_unknown_transport_rejected(self, palace_path, monkeypatch):
        monkeypatch.setenv("MEMPALACE_TRANSPORT", "carrier-pigeon")
        with pytest.raises(ValueError, match="carrier-pigeon"):
            get_transport(palace_path)


class TestCompatSurface:
    def test_logsync_reexports_survive(self):
        from mempalace.logsync import (
            PEERS_FILENAME,
            SyncPeerError,
            _peer_get,
            load_peers,
            sync_all,
            sync_with_peer,
        )

        assert PEERS_FILENAME == "peers.json"
        assert callable(_peer_get) and callable(load_peers)
        assert callable(sync_all) and callable(sync_with_peer)
        # The historical error name IS the seam error — one class, so
        # every existing `except SyncPeerError` catches transport failures.
        assert SyncPeerError is TransportError

    def test_sync_all_accepts_injected_transport(self, palace_path):
        from mempalace.logstream import Logstream
        from mempalace.logsync import sync_all

        class _EmptyTransport(Transport):
            def self_id(self):
                return "rep_000000000000"

            def peers(self):
                return []

            def request(self, peer, path, params=None):
                raise AssertionError("no peers, no requests")

        ls = Logstream(db_path=os.path.join(palace_path, "logstream.sqlite3"))
        try:
            assert sync_all(ls, palace_path, transport=_EmptyTransport()) == []
        finally:
            ls.close()
