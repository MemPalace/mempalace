"""
test_opsync.py — Anti-entropy sync of the memory op-log (RFC 004 step 2a).

Covers two-replica convergence over a fake transport, idempotent
re-rounds, transitive propagation through a carrier replica (the memory
analogue of the logstream T1 test), and per-peer error isolation.
"""

import os

import pytest

from mempalace.oplog import OPLOG_DB_FILENAME, OpLog
from mempalace.opsync import sync_all_memops, sync_memops_with_peer
from mempalace.transport import Transport, TransportError


@pytest.fixture
def three_logs(tmp_dir):
    logs = {}
    for name in ("a", "b", "c"):
        path = os.path.join(tmp_dir, f"palace_{name}")
        os.makedirs(path)
        logs[name] = OpLog(db_path=os.path.join(path, OPLOG_DB_FILENAME))
    yield logs
    for log in logs.values():
        log.close()


class _OpLogTransport(Transport):
    """Serves /sync/memops* straight from in-process OpLogs, keyed by peer name."""

    def __init__(self, targets: dict):
        self.targets = targets

    def self_id(self):
        return "rep_transport0000"

    def peers(self):
        return [{"name": name, "url": f"fake://{name}"} for name in self.targets]

    def request(self, peer, path, params=None):
        target = self.targets.get(peer["name"])
        if target is None:
            raise TransportError(f"unknown peer {peer!r}")
        if path == "/sync/memops/version_vector":
            return {"replica_id": target.replica_id, "version_vector": target.version_vector()}
        if path == "/sync/memops":
            params = params or {}
            ops = target.list_ops(
                params["origin"], after_seq=params.get("after", 0), limit=params.get("limit", 500)
            )
            return {"origin": params["origin"], "ops": ops, "count": len(ops)}
        raise TransportError(f"unexpected path {path}")


def test_two_replica_convergence_and_idempotency(three_logs):
    a, b = three_logs["a"], three_logs["b"]
    for i in range(3):
        a.append("drawer.add", {"drawer_id": f"d{i}", "content": f"c{i}"})
    b.append("kg.assert", {"triple_id": "t1"})

    transport = _OpLogTransport({"a": a})
    stats = sync_memops_with_peer(b, transport, {"name": "a", "url": "fake://a"})
    assert stats["pulled_ops"] == 3
    assert stats["per_origin"] == {a.replica_id: 3}
    assert b.version_vector()[a.replica_id] == a.version_vector()[a.replica_id]

    # Verbatim fold: the op arrives with its origin stamps untouched.
    pulled = b.list_ops(a.replica_id)
    assert [op["payload"]["drawer_id"] for op in pulled] == ["d0", "d1", "d2"]
    assert all(op["origin_replica"] == a.replica_id for op in pulled)

    # Second round is a no-op.
    again = sync_memops_with_peer(b, transport, {"name": "a", "url": "fake://a"})
    assert again["pulled_ops"] == 0


def test_transitive_propagation_via_carrier(three_logs):
    """C pulls only from B and must still receive A-origin ops (T1 for memory)."""
    a, b, c = three_logs["a"], three_logs["b"], three_logs["c"]
    a.append("drawer.add", {"drawer_id": "origin_a", "content": "x"})

    sync_memops_with_peer(b, _OpLogTransport({"a": a}), {"name": "a", "url": "fake://a"})
    stats = sync_memops_with_peer(c, _OpLogTransport({"b": b}), {"name": "b", "url": "fake://b"})

    assert stats["per_origin"] == {a.replica_id: 1}
    carried = c.list_ops(a.replica_id)
    assert carried[0]["payload"]["drawer_id"] == "origin_a"
    assert carried[0]["origin_replica"] == a.replica_id


def test_sync_all_isolates_peer_errors(three_logs, tmp_dir):
    a, b = three_logs["a"], three_logs["b"]
    a.append("drawer.add", {"drawer_id": "d1"})

    class _HalfDeadTransport(_OpLogTransport):
        def peers(self):
            return [
                {"name": "dead", "url": "fake://dead"},
                {"name": "a", "url": "fake://a"},
            ]

    results = sync_all_memops(
        os.path.join(tmp_dir, "palace_b"),
        oplog=b,
        transport=_HalfDeadTransport({"a": a}),
    )
    by_name = {r["peer_name"]: r for r in results}
    assert "error" in by_name["dead"]
    assert by_name["a"]["pulled_ops"] == 1
