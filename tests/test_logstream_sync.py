"""
Tests for RFC 004 step 0 — logstream multi-master replication.

Covers: HLC ordering, replica identity minting, schema migration/backfill of
pre-replication logs, origin stamping, sync primitives (version vector,
list_ops, idempotent apply), the /sync/* HTTP endpoints, the anti-entropy
engine's two-replica convergence (including a partition with duplicate
claims), and the CLI sync command.
"""

import http.client
import json
import os
import sqlite3
import threading

import pytest

from mempalace import logsync
from mempalace.hlc import HybridLogicalClock, parse, render
from mempalace.logstream import Logstream
from mempalace.replica import get_replica_id


@pytest.fixture
def palace_a(tmp_dir):
    p = os.path.join(tmp_dir, "palace_a")
    os.makedirs(p)
    return p


@pytest.fixture
def palace_b(tmp_dir):
    p = os.path.join(tmp_dir, "palace_b")
    os.makedirs(p)
    return p


@pytest.fixture
def ls_a(palace_a):
    ls = Logstream(db_path=os.path.join(palace_a, "logstream.sqlite3"))
    yield ls
    ls.close()


@pytest.fixture
def ls_b(palace_b):
    ls = Logstream(db_path=os.path.join(palace_b, "logstream.sqlite3"))
    yield ls
    ls.close()


def _append(ls, body="x", **overrides):
    fields = dict(
        type="task.request",
        stream="project/mempalace",
        room="delegation",
        from_agent="agent-a",
        body=body,
    )
    fields.update(overrides)
    return ls.append_event(**fields)


def _wire_sync(dst, src):
    """Run one anti-entropy round dst←src with the wire simulated in-process."""

    def fake_peer_get(base_url, token, path, params=None):
        if path == "/sync/version_vector":
            return {"replica_id": src.replica_id, "version_vector": src.version_vector()}
        if path == "/sync/ops":
            return {
                "events": src.list_ops(
                    params["origin"], after_seq=int(params["after"]), limit=int(params["limit"])
                )
            }
        if path == "/sync/artifact":
            return {"artifact": src.get_artifact(params["id"])}
        raise AssertionError(path)

    original = logsync._peer_get
    logsync._peer_get = fake_peer_get
    try:
        return logsync.sync_with_peer(dst, "fake://peer")
    finally:
        logsync._peer_get = original


# ── HLC ───────────────────────────────────────────────────────────────────


class TestHLC:
    def test_tick_is_strictly_monotonic_within_one_ms(self):
        clock = HybridLogicalClock("rep_aaaaaaaaaaaa", now_ms=lambda: 1000)
        stamps = [clock.tick() for _ in range(5)]
        assert stamps == sorted(stamps)
        assert len(set(stamps)) == 5

    def test_tick_survives_clock_regression(self):
        times = iter([2000, 1500, 1500])
        clock = HybridLogicalClock("rep_aaaaaaaaaaaa", now_ms=lambda: next(times))
        first = clock.tick()
        second = clock.tick()
        third = clock.tick()
        assert first < second < third

    def test_observe_absorbs_remote_instant(self):
        clock = HybridLogicalClock("rep_aaaaaaaaaaaa", now_ms=lambda: 1000)
        clock.observe(render(9999, 5, "rep_bbbbbbbbbbbb"))
        assert clock.tick() > render(9999, 5, "rep_bbbbbbbbbbbb")

    def test_restart_seeding_preserves_monotonicity(self):
        stamp = render(5000, 3, "rep_aaaaaaaaaaaa")
        clock = HybridLogicalClock("rep_aaaaaaaaaaaa", last=stamp, now_ms=lambda: 1000)
        assert clock.tick() > stamp

    def test_parse_render_round_trip(self):
        stamp = render(1783038849123, 7, "rep_ab12cd34ef56")
        assert parse(stamp) == (1783038849123, 7, "rep_ab12cd34ef56")

    def test_malformed_observe_is_ignored(self):
        clock = HybridLogicalClock("rep_aaaaaaaaaaaa", now_ms=lambda: 1000)
        clock.observe("garbage")
        assert clock.tick().startswith("0000000001000-")


# ── Replica identity ──────────────────────────────────────────────────────


class TestReplicaIdentity:
    def test_mint_persist_stable(self, palace_a):
        first = get_replica_id(palace_a)
        assert first.startswith("rep_")
        assert get_replica_id(palace_a) == first
        assert json.load(open(os.path.join(palace_a, "replica.json")))["replica_id"] == first

    def test_corrupt_file_fails_loudly(self, palace_a):
        with open(os.path.join(palace_a, "replica.json"), "w") as f:
            f.write("not json")
        with pytest.raises(ValueError, match="corrupt"):
            get_replica_id(palace_a)

    def test_logstream_adopts_palace_identity(self, palace_a, ls_a):
        assert ls_a.replica_id == get_replica_id(palace_a)


# ── Migration / backfill ──────────────────────────────────────────────────


class TestMigration:
    def test_pre_replication_log_is_backfilled(self, palace_a):
        db = os.path.join(palace_a, "logstream.sqlite3")
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE events (
                id TEXT PRIMARY KEY, type TEXT NOT NULL, stream TEXT NOT NULL,
                room TEXT NOT NULL, from_agent TEXT NOT NULL, to_agent TEXT,
                correlation_id TEXT, branch TEXT, base_commit TEXT, status TEXT,
                body TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE artifacts (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, content TEXT NOT NULL,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE event_artifacts (
                event_id TEXT NOT NULL, artifact_id TEXT NOT NULL,
                PRIMARY KEY (event_id, artifact_id)
            );
        """)
        for i in range(3):
            conn.execute(
                "INSERT INTO events (id, type, stream, room, from_agent, created_at)"
                " VALUES (?, 'task.request', 's', 'r', 'a', ?)",
                (f"evt_old_{i}", f"2026-07-01T10:00:0{i}Z"),
            )
        conn.commit()
        conn.close()

        ls = Logstream(db_path=db)
        try:
            events = ls.list_events(limit=10)
            assert len(events) == 3
            assert all(e["origin_replica"] == ls.replica_id for e in events)
            assert [e["origin_seq"] for e in events] == [1, 2, 3]
            hlcs = [e["hlc"] for e in events]
            assert hlcs == sorted(hlcs)
            # New appends continue seamlessly after the backfill.
            fresh = _append(ls)
            assert fresh["origin_seq"] == 4
            assert fresh["hlc"] > hlcs[-1]
            assert ls.version_vector() == {ls.replica_id: 4}
        finally:
            ls.close()


# ── Sync primitives ───────────────────────────────────────────────────────


class TestHttpTimeout:
    def test_default_and_env_override(self, monkeypatch):
        from mempalace.logsync import _HTTP_TIMEOUT_S, _http_timeout_s

        monkeypatch.delenv("MEMPALACE_SYNC_HTTP_TIMEOUT", raising=False)
        assert _http_timeout_s() == float(_HTTP_TIMEOUT_S)
        monkeypatch.setenv("MEMPALACE_SYNC_HTTP_TIMEOUT", "180")
        assert _http_timeout_s() == 180.0
        # Garbage degrades to the default rather than wedging every sync.
        monkeypatch.setenv("MEMPALACE_SYNC_HTTP_TIMEOUT", "not-a-number")
        assert _http_timeout_s() == float(_HTTP_TIMEOUT_S)


class TestSyncPrimitives:
    def test_append_stamps_origin_fields(self, ls_a):
        event = _append(ls_a)
        assert event["origin_replica"] == ls_a.replica_id
        assert event["origin_seq"] == event["seq"]
        parse(event["hlc"])  # valid HLC

    def test_list_ops_paginates_in_author_order(self, ls_a):
        ids = [_append(ls_a, body=f"e{i}")["id"] for i in range(5)]
        first = ls_a.list_ops(ls_a.replica_id, after_seq=0, limit=3)
        rest = ls_a.list_ops(ls_a.replica_id, after_seq=first[-1]["origin_seq"], limit=3)
        assert [e["id"] for e in first + rest] == ids

    def test_apply_remote_event_is_idempotent(self, ls_a, ls_b):
        event = _append(ls_a)
        assert ls_b.apply_remote_event(event) is True
        assert ls_b.apply_remote_event(event) is False
        assert ls_b.list_events()[0]["id"] == event["id"]

    def test_own_echo_is_skipped(self, ls_a):
        event = _append(ls_a)
        assert ls_a.apply_remote_event(event) is False

    def test_remote_event_with_missing_artifact_is_rejected(self, ls_a, ls_b):
        artifact = ls_a.put_artifact(kind="note", content="n", created_by="a")
        event = _append(ls_a, type="patch.ready", artifact_ids=[artifact["id"]])
        with pytest.raises(ValueError, match="not yet applied"):
            ls_b.apply_remote_event(event)

    def test_apply_remote_artifact_verifies_hash(self, ls_a, ls_b):
        artifact = ls_a.put_artifact(kind="note", content="genuine", created_by="a")
        full = ls_a.get_artifact(artifact["id"])
        tampered = {**full, "content": "tampered"}
        with pytest.raises(ValueError, match="hash verification"):
            ls_b.apply_remote_artifact(tampered)
        assert ls_b.apply_remote_artifact(full) is True
        assert ls_b.apply_remote_artifact(full) is False

    def test_applied_remote_hlc_is_observed(self, ls_a, ls_b):
        remote = _append(ls_a)
        ls_b.apply_remote_event(remote)
        local_after = _append(ls_b)
        assert local_after["hlc"] > remote["hlc"]

    def test_remote_events_are_verbatim(self, ls_a, ls_b):
        artifact = ls_a.put_artifact(kind="note", content="payload", created_by="a")
        event = _append(
            ls_a,
            type="patch.ready",
            artifact_ids=[artifact["id"]],
            metadata={"k": "v"},
            body="line1\nline2",
        )
        ls_b.apply_remote_artifact(ls_a.get_artifact(artifact["id"]))
        ls_b.apply_remote_event(event)
        copy = ls_b.list_events(type="patch.ready")[0]
        for key in (
            "id",
            "type",
            "stream",
            "room",
            "from_agent",
            "body",
            "created_at",
            "metadata",
            "origin_replica",
            "origin_seq",
            "hlc",
            "artifact_ids",
        ):
            assert copy[key] == event[key], key
        assert ls_b.get_artifact(artifact["id"])["content"] == "payload"


# ── Convergence (engine over simulated wire) ─────────────────────────────


class TestConvergence:
    def _event_ids(self, ls):
        return {e["id"] for e in ls.list_events(limit=500)}

    def test_two_replicas_converge_bidirectionally(self, ls_a, ls_b):
        _append(ls_a, body="from a1")
        submitted = ls_a.submit_patch(
            content="diff --git a/f b/f\n+1\n",
            from_agent="agent-a",
            stream="project/mempalace",
        )
        _append(ls_b, body="from b1", from_agent="agent-b")

        stats_b = _wire_sync(ls_b, ls_a)  # b pulls a
        stats_a = _wire_sync(ls_a, ls_b)  # a pulls b

        assert stats_b["pulled_events"] == 2
        assert stats_b["pulled_artifacts"] == 1
        assert stats_a["pulled_events"] == 1
        assert self._event_ids(ls_a) == self._event_ids(ls_b)
        assert ls_a.version_vector() == ls_b.version_vector()
        assert (
            ls_b.get_artifact(submitted["artifact"]["id"])["sha256"]
            == (submitted["artifact"]["sha256"])
        )
        # A second round is a no-op — convergence is stable.
        assert _wire_sync(ls_b, ls_a)["pulled_events"] == 0

    def test_partition_with_duplicate_claims_converges(self, ls_a, ls_b):
        """R3: both replicas claim the same task during a partition; after
        merge both claims exist and the earliest HLC deterministically wins
        on both sides."""
        request = _append(ls_a, correlation_id="task_dup")
        _wire_sync(ls_b, ls_a)

        claim_a = ls_a.ack_event(request["id"], from_agent="agent-a", status="claimed")
        claim_b = ls_b.ack_event(request["id"], from_agent="agent-b", status="claimed")

        _wire_sync(ls_b, ls_a)
        _wire_sync(ls_a, ls_b)

        for ls in (ls_a, ls_b):
            claims = [
                e for e in ls.list_events(correlation_id="task_dup") if e["status"] == "claimed"
            ]
            assert len(claims) == 2
            winner = min(claims, key=lambda e: e["hlc"])
            assert winner["id"] == min((claim_a, claim_b), key=lambda e: e["hlc"])["id"]


# ── HTTP endpoints + CLI (real wire) ─────────────────────────────────────


class TestSyncOverHttp:
    @pytest.fixture
    def server(self, monkeypatch, config, palace_path):
        from mempalace import mcp_server as mcp

        monkeypatch.setattr(mcp, "_config", config)
        monkeypatch.setattr(mcp, "_logstream_by_path", {})
        httpd = mcp._build_http_server("127.0.0.1", 0)
        port = httpd.server_address[1]
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            yield port, mcp
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
            for ls in mcp._logstream_by_path.values():
                ls.close()

    def _get(self, port, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")
        finally:
            conn.close()

    def test_endpoints_roundtrip(self, server):
        port, mcp = server
        appended = mcp.tool_event_append(
            type="task.request",
            stream="s",
            room="r",
            from_agent="a",
            body="hello",
        )["event"]

        status, vector = self._get(port, "/sync/version_vector")
        assert status == 200
        assert vector["version_vector"] == {appended["origin_replica"]: appended["origin_seq"]}

        status, ops = self._get(
            port, f"/sync/ops?origin={appended['origin_replica']}&after=0&limit=10"
        )
        assert status == 200
        assert [e["id"] for e in ops["events"]] == [appended["id"]]

        status, missing = self._get(port, "/sync/artifact?id=art_nope")
        assert status == 404
        assert "not found" in missing["error"]

        status, bad = self._get(port, "/sync/ops?origin=&after=0")
        assert status == 400

    def test_cli_sync_pulls_from_peer_over_http(self, server, tmp_dir, capsys):
        port, mcp = server
        mcp.tool_event_append(
            type="task.request", stream="s", room="r", from_agent="a", body="pull me"
        )

        from types import SimpleNamespace

        from mempalace.cli import cmd_logstream

        local_palace = os.path.join(tmp_dir, "local_replica")
        os.makedirs(local_palace)
        cmd_logstream(
            SimpleNamespace(
                palace=local_palace,
                logstream_action="sync",
                peer=f"http://127.0.0.1:{port}",
                token=None,
                json=True,
            )
        )
        results = json.loads(capsys.readouterr().out)
        assert results[0]["pulled_events"] == 1

        local = Logstream(db_path=os.path.join(local_palace, "logstream.sqlite3"))
        try:
            assert local.list_events()[0]["body"] == "pull me"
        finally:
            local.close()
