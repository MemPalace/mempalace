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
import re
import sqlite3
import threading

import pytest

from mempalace import logsync
from mempalace.hlc import MAX_FUTURE_DRIFT_MS, HybridLogicalClock, parse, render
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

    def test_observe_absorbs_remote_instant_within_drift(self):
        clock = HybridLogicalClock("rep_aaaaaaaaaaaa", now_ms=lambda: 1000)
        remote = render(1000 + MAX_FUTURE_DRIFT_MS, 5, "rep_bbbbbbbbbbbb")
        clock.observe(remote)
        assert clock.tick() > remote

    def test_observe_ignores_absurd_future_instant(self):
        clock = HybridLogicalClock("rep_aaaaaaaaaaaa", now_ms=lambda: 1000)
        remote = render(1000 + MAX_FUTURE_DRIFT_MS + 1, 0, "rep_bbbbbbbbbbbb")
        clock.observe(remote)
        assert clock.tick() < remote

    def test_restart_seeding_preserves_monotonicity(self):
        stamp = render(5000, 3, "rep_aaaaaaaaaaaa")
        clock = HybridLogicalClock("rep_aaaaaaaaaaaa", last=stamp, now_ms=lambda: 1000)
        assert clock.tick() > stamp

    def test_restart_seeding_ignores_absurd_future_instant(self):
        stamp = render(1000 + MAX_FUTURE_DRIFT_MS + 1, 3, "rep_aaaaaaaaaaaa")
        clock = HybridLogicalClock("rep_aaaaaaaaaaaa", last=stamp, now_ms=lambda: 1000)
        assert clock.tick() < stamp

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
        assert re.match(r"^rep_[0-9a-f]{32}$", first)
        assert get_replica_id(palace_a) == first
        assert json.load(open(os.path.join(palace_a, "replica.json")))["replica_id"] == first

    def test_legacy_short_replica_id_stays_valid(self, palace_a):
        with open(os.path.join(palace_a, "replica.json"), "w", encoding="utf-8") as f:
            json.dump({"replica_id": "rep_aaaabbbbcccc"}, f)

        assert get_replica_id(palace_a) == "rep_aaaabbbbcccc"

    def test_invalid_replica_id_fails_loudly(self, palace_a):
        with open(os.path.join(palace_a, "replica.json"), "w", encoding="utf-8") as f:
            json.dump({"replica_id": "rep_aaaabbbbccccdddd"}, f)

        with pytest.raises(ValueError, match="invalid replica_id"):
            get_replica_id(palace_a)

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
        from mempalace.transport import _HTTP_TIMEOUT_S, _http_timeout_s

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


@pytest.fixture
def server(monkeypatch, config, palace_path):
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


def _http_get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")
    finally:
        conn.close()


class TestSyncOverHttp:
    def _get(self, port, path):
        return _http_get(port, path)

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

    def test_sync_peers_estate_endpoint(self, server, palace_path):
        """/sync/peers: names + reachability + vectors, NEVER the token,
        and transitively-known origins surface as unnamed."""
        port, mcp = server
        with open(os.path.join(palace_path, "peers.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "peers": [
                        {"name": "windows", "url": "http://peer.example:8765", "token": "S3CRET"}
                    ]
                },
                f,
            )
        mcp._PEER_SYNC_STATE.clear()
        mcp._record_peer_sync(
            {
                "peer_name": "windows",
                "peer_url": "http://peer.example:8765",
                "peer_replica": "rep_bbbbbbbbbbbb",
                "pulled_events": 3,
                "pulled_artifacts": 1,
                "remote_version_vector": {"rep_bbbbbbbbbbbb": 49},
            }
        )
        local = mcp.tool_event_append(
            type="status.update", stream="s", room="r", from_agent="a", body="mine"
        )["event"]
        # A third replica this node never configured — known only because
        # its op arrived through a carrier (the blade-via-windows case).
        mcp._call_logstream(
            lambda ls: ls.apply_remote_event(
                {
                    "id": "evt_foreign_1",
                    "type": "status.update",
                    "stream": "s",
                    "room": "r",
                    "from_agent": "b",
                    "created_at": "2026-07-02T00:00:00Z",
                    "origin_replica": "rep_cccccccccccc",
                    "origin_seq": 5,
                    "hlc": "1783000000000-000000-rep_cccccccccccc",
                }
            )
        )

        status, payload = self._get(port, "/sync/peers")
        assert status == 200
        assert payload["self"]["replica_id"] == local["origin_replica"]
        assert payload["self"]["name"]
        assert payload["self"]["version_vector"][local["origin_replica"]] == local["origin_seq"]

        (peer,) = payload["peers"]
        assert peer["name"] == "windows"
        assert peer["url"] == "http://peer.example:8765"
        assert peer["replica_id"] == "rep_bbbbbbbbbbbb"
        assert peer["reachable"] is True
        assert peer["remote_version_vector"] == {"rep_bbbbbbbbbbbb": 49}

        assert payload["unnamed_origins"] == ["rep_cccccccccccc"]
        assert "S3CRET" not in json.dumps(payload)

    def test_record_peer_sync_error_preserves_last_known_state(self):
        from mempalace import mcp_server as mcp

        mcp._PEER_SYNC_STATE.clear()
        mcp._record_peer_sync(
            {
                "peer_name": "w",
                "peer_url": "http://peer.example:8765",
                "peer_replica": "rep_bbbbbbbbbbbb",
                "pulled_events": 0,
                "pulled_artifacts": 0,
                "remote_version_vector": {"rep_bbbbbbbbbbbb": 7},
            }
        )
        succeeded = dict(mcp._PEER_SYNC_STATE["w"])
        mcp._record_peer_sync({"peer_name": "w", "error": "connection refused"})
        entry = mcp._PEER_SYNC_STATE["w"]
        assert entry["reachable"] is False
        assert entry["last_error"] == "connection refused"
        assert entry["last_success_at"] == succeeded["last_success_at"]
        assert entry["replica_id"] == "rep_bbbbbbbbbbbb"
        assert entry["remote_version_vector"] == {"rep_bbbbbbbbbbbb": 7}
        assert entry["url"] == "http://peer.example:8765"
        mcp._PEER_SYNC_STATE.clear()

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


class TestPublishedEstate:
    """The estate has to be readable from processes that do not sync.

    The peer sync loop only runs under ``--transport http``, but
    ``mempalace_mesh_peers`` is available in every transport -- including the
    stdio servers agents actually connect through. Those processes have a
    permanently empty ``_PEER_SYNC_STATE``, so before the hub published its
    estate they answered with peers carrying a name and a url and nothing
    else, and ``origin_profiles`` holding only this node, while the hub next
    door knew reachability, vectors and profiles for every peer.
    """

    @pytest.fixture(autouse=True)
    def _isolated_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

    @pytest.fixture(autouse=True)
    def _clean_estate_state(self):
        from mempalace import mcp_server as mcp

        mcp._KNOWN_PROFILES.clear()
        mcp._node_profile_cache.clear()
        mcp._PEER_SYNC_STATE.clear()
        yield
        mcp._KNOWN_PROFILES.clear()
        mcp._node_profile_cache.clear()
        mcp._PEER_SYNC_STATE.clear()

    def _write_peers(self, palace_path, name="w", url="http://peer.example:8765"):
        with open(os.path.join(palace_path, "peers.json"), "w", encoding="utf-8") as f:
            json.dump({"peers": [{"name": name, "url": url, "token": "S3CRET"}]}, f)

    def _sync_round(self, mcp, palace_path, name="w", url="http://peer.example:8765"):
        """One successful round, then publish, exactly as the loop does."""
        mcp._record_peer_sync(
            {
                "peer_name": name,
                "peer_url": url,
                "peer_replica": "rep_bbbbbbbbbbbb",
                "pulled_events": 3,
                "pulled_artifacts": 0,
                "remote_version_vector": {"rep_bbbbbbbbbbbb": 7},
                "remote_profile": {"roles": ["replica"], "advertised_at": "2026-07-03T01:00:00Z"},
                "remote_profiles": {
                    "rep_cccccccccccc": {
                        "roles": ["agents"],
                        "advertised_at": "2026-07-03T00:30:00Z",
                    }
                },
            }
        )
        mcp._publish_mesh_state(palace_path)

    def test_non_syncing_process_sees_the_published_estate(self, server, palace_path):
        """The regression: a process with no sync loop must still report status."""
        _port, mcp = server
        self._write_peers(palace_path)
        self._sync_round(mcp, palace_path)

        # Become a process that never syncs — the stdio server's situation.
        mcp._PEER_SYNC_STATE.clear()
        mcp._KNOWN_PROFILES.clear()

        payload = mcp._mesh_peers_payload()
        (peer,) = payload["peers"]
        assert peer["reachable"] is True
        assert peer["replica_id"] == "rep_bbbbbbbbbbbb"
        assert peer["remote_version_vector"] == {"rep_bbbbbbbbbbbb": 7}
        assert peer["last_pulled_events"] == 3
        assert payload["origin_profiles"]["rep_bbbbbbbbbbbb"]["roles"] == ["replica"]
        assert payload["origin_profiles"]["rep_cccccccccccc"]["roles"] == ["agents"]
        assert payload["estate_source"]["in_process"] is False
        assert payload["estate_source"]["writer_alive"] is True
        assert payload["estate_source"]["published_at"]

    def test_published_estate_names_origins_that_would_read_as_transitive(
        self, server, palace_path
    ):
        """A peer whose replica_id is only known via the estate is not 'unnamed'.

        ``unnamed_origins`` means "seen in the log but not configured as a
        peer". Without the published estate a non-syncing process cannot map
        a configured peer to its replica_id, so a perfectly ordinary peer was
        reported as an origin known only transitively.
        """
        _port, mcp = server
        self._write_peers(palace_path)
        self._sync_round(mcp, palace_path)
        mcp._PEER_SYNC_STATE.clear()
        mcp._KNOWN_PROFILES.clear()

        mcp._call_logstream(
            lambda ls: ls.append_event(
                type="status.update", stream="s", room="r", from_agent="a", body="x"
            )
        )
        payload = mcp._mesh_peers_payload()
        assert "rep_bbbbbbbbbbbb" not in payload["unnamed_origins"]

    def test_in_process_state_wins_over_the_published_file(self, server, palace_path):
        """The syncing process trusts itself: its own round is fresher than disk."""
        _port, mcp = server
        self._write_peers(palace_path)
        self._sync_round(mcp, palace_path)

        # The hub's own next round finds the peer down. The stale file still
        # says reachable; the live process must report what it just observed.
        mcp._record_peer_sync({"peer_name": "w", "error": "connection refused"})
        payload = mcp._mesh_peers_payload()
        (peer,) = payload["peers"]
        assert peer["reachable"] is False
        assert peer["last_error"] == "connection refused"
        assert payload["estate_source"]["in_process"] is True

    def test_published_estate_never_carries_peer_tokens(self, server, palace_path):
        """peers.json tokens must not reach a file other processes read."""
        from mempalace import server_registry

        _port, mcp = server
        self._write_peers(palace_path)
        self._sync_round(mcp, palace_path)

        raw = server_registry.mesh_state_path(palace_path).read_text(encoding="utf-8")
        assert "S3CRET" not in raw
        mcp._PEER_SYNC_STATE.clear()
        assert "S3CRET" not in json.dumps(mcp._mesh_peers_payload())

    def test_published_estate_is_private_and_atomic(self, server, palace_path):
        """0600, and no partial file left behind for a concurrent reader."""
        from mempalace import server_registry

        _port, mcp = server
        self._write_peers(palace_path)
        self._sync_round(mcp, palace_path)

        path = server_registry.mesh_state_path(palace_path)
        if os.name != "nt":  # Windows does not carry POSIX mode bits
            assert oct(path.stat().st_mode & 0o777) == "0o600"
        leftovers = [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_dead_writer_is_reported_as_not_alive(self, server, palace_path):
        """A crashed hub leaves a last-known-good estate, flagged as stale."""
        from mempalace import server_registry

        _port, mcp = server
        self._write_peers(palace_path)
        self._sync_round(mcp, palace_path)

        path = server_registry.mesh_state_path(palace_path)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["pid"] = 2**31 - 1  # a pid that cannot be running
        path.write_text(json.dumps(record), encoding="utf-8")

        mcp._PEER_SYNC_STATE.clear()
        payload = mcp._mesh_peers_payload()
        (peer,) = payload["peers"]
        # Still shown — "last seen as" beats a blank node — but marked stale.
        assert peer["reachable"] is True
        assert payload["estate_source"]["writer_alive"] is False

    def test_missing_or_malformed_estate_degrades_quietly(self, server, palace_path):
        """No hub has ever published, or the file is corrupt: no traceback."""
        from mempalace import server_registry

        _port, mcp = server
        self._write_peers(palace_path)

        payload = mcp._mesh_peers_payload()
        (peer,) = payload["peers"]
        assert peer["name"] == "w"
        assert payload["estate_source"]["published_at"] is None

        path = server_registry.mesh_state_path(palace_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        payload = mcp._mesh_peers_payload()
        assert payload["peers"][0]["name"] == "w"
        assert payload["estate_source"]["writer_alive"] is False


class TestNodeProfile:
    """The self-described node profile (estate truth, never UI guesses):
    pure derivation, advertisement on /sync/version_vector, transit relay
    via profiles, and the mempalace_mesh_peers tool as the same payload."""

    @pytest.fixture(autouse=True)
    def _clean_profile_state(self):
        from mempalace import mcp_server as mcp

        mcp._KNOWN_PROFILES.clear()
        mcp._node_profile_cache.clear()
        mcp._PEER_SYNC_STATE.clear()
        yield
        mcp._KNOWN_PROFILES.clear()
        mcp._node_profile_cache.clear()
        mcp._PEER_SYNC_STATE.clear()

    def _profile(self, origin, advertised_at, roles=None):
        return {
            "roles": roles or ["replica"],
            "accelerator": {"provider": "CUDA", "embedder": "minilm"},
            "drawers": 42,
            "hardware": f"test-{origin}",
            "advertised_at": advertised_at,
        }

    def test_self_profile_is_pure_derivation(self, server):
        port, mcp = server
        mcp.tool_event_append(
            type="status.update", stream="s", room="r", from_agent="a", body="authored"
        )
        profile = mcp._node_profile()
        assert "agents" in profile["roles"]  # locally-authored events exist
        assert profile["hardware"]
        assert profile["advertised_at"]
        # Cached within the TTL: same object, no recompute per request.
        assert mcp._node_profile() is profile

    def test_version_vector_advertises_profile_and_relays(self, server):
        port, mcp = server
        mcp.tool_event_append(type="status.update", stream="s", room="r", from_agent="a", body="x")
        mcp._merge_known_profiles({"rep_cccccccccccc": self._profile("c", "2026-07-03T00:00:00Z")})
        _status, payload = _http_get(port, "/sync/version_vector")
        assert isinstance(payload["profile"]["roles"], list)
        self_id = payload["replica_id"]
        assert payload["profiles"][self_id] == payload["profile"]
        assert payload["profiles"]["rep_cccccccccccc"]["hardware"] == "test-c"

    def test_merge_known_profiles_is_lww_by_advertised_at(self):
        from mempalace import mcp_server as mcp

        newer = self._profile("b", "2026-07-03T02:00:00Z")
        older = self._profile("b", "2026-07-03T01:00:00Z")
        mcp._merge_known_profiles({"rep_b": newer})
        mcp._merge_known_profiles({"rep_b": older})
        assert mcp._KNOWN_PROFILES["rep_b"] == newer
        mcp._merge_known_profiles({"rep_b": self._profile("b", "2026-07-03T03:00:00Z")})
        assert mcp._KNOWN_PROFILES["rep_b"]["advertised_at"] == "2026-07-03T03:00:00Z"

    def test_estate_carries_peer_and_relayed_profiles(self, server, palace_path):
        port, mcp = server
        with open(os.path.join(palace_path, "peers.json"), "w", encoding="utf-8") as f:
            json.dump(
                {"peers": [{"name": "w", "url": "http://peer.example:8765", "token": "S3CRET"}]},
                f,
            )
        peer_profile = self._profile("b", "2026-07-03T01:00:00Z", roles=["replica", "compute"])
        relayed = self._profile("c", "2026-07-03T00:30:00Z")
        mcp._record_peer_sync(
            {
                "peer_name": "w",
                "peer_url": "http://peer.example:8765",
                "peer_replica": "rep_bbbbbbbbbbbb",
                "pulled_events": 0,
                "pulled_artifacts": 0,
                "remote_version_vector": {"rep_bbbbbbbbbbbb": 7},
                "remote_profile": peer_profile,
                "remote_profiles": {"rep_cccccccccccc": relayed},
            }
        )
        status, payload = _http_get(port, "/sync/peers")
        assert status == 200
        (peer,) = payload["peers"]
        assert peer["profile"] == peer_profile
        assert payload["origin_profiles"]["rep_bbbbbbbbbbbb"] == peer_profile
        assert payload["origin_profiles"]["rep_cccccccccccc"] == relayed
        assert (
            payload["origin_profiles"][payload["self"]["replica_id"]]
            == (payload["self"]["profile"])
        )
        assert "S3CRET" not in json.dumps(payload)

        # Unreachable peers keep their last advertised profile.
        mcp._record_peer_sync({"peer_name": "w", "error": "connection refused"})
        status, payload = _http_get(port, "/sync/peers")
        (peer,) = payload["peers"]
        assert peer["reachable"] is False
        assert peer["profile"] == peer_profile

    def test_mesh_peers_tool_is_the_endpoint_payload(self, server, palace_path):
        port, mcp = server
        mcp.tool_event_append(type="status.update", stream="s", room="r", from_agent="a", body="x")
        status, endpoint_payload = _http_get(port, "/sync/peers")
        assert status == 200
        assert mcp.tool_mesh_peers() == endpoint_payload

    def test_sync_with_peer_captures_remote_profile(self, server, tmp_dir):
        port, mcp = server
        mcp.tool_event_append(type="status.update", stream="s", room="r", from_agent="a", body="x")
        from mempalace.logsync import sync_with_peer

        local = Logstream(db_path=os.path.join(tmp_dir, "local", "logstream.sqlite3"))
        try:
            stats = sync_with_peer(local, f"http://127.0.0.1:{port}")
        finally:
            local.close()
        assert isinstance(stats["remote_profile"]["roles"], list)
        assert stats["remote_profiles"][stats["peer_replica"]] == stats["remote_profile"]

    def test_mesh_peers_is_exempt_from_the_integrity_gate(self):
        # The estate is observability: it must answer while the palace
        # index is corrupt and under repair (caught live on the blade).
        from mempalace import mcp_server as mcp

        assert "mempalace_mesh_peers" in mcp._SQLITE_INTEGRITY_ALLOWED_TOOLS


class TestPeerSyncThreadStartup:
    """The loop must not latch membership at startup.

    Joining the mesh is "write peers.json", and the guide has users start
    the hub before writing it. A thread that returns early when the file
    is missing leaves that hub permanently non-syncing with no error —
    the failure mode is silence, which is why it needs a test.
    """

    def _start(self, tmp_path, monkeypatch, interval="0.05"):
        import threading

        from mempalace import mcp_server as mcp

        monkeypatch.setenv("MEMPALACE_SYNC_INTERVAL", interval)
        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))

        # Compare thread objects, not names: earlier tests in this class
        # leave their own daemon loop running under the same name.
        before = set(threading.enumerate())
        mcp._start_peer_sync_thread()
        return [
            t for t in threading.enumerate() if t.name == "mempalace-logsync" and t not in before
        ]

    def test_thread_starts_without_peers_json(self, tmp_path, monkeypatch):
        assert not (tmp_path / "peers.json").exists()
        assert self._start(tmp_path, monkeypatch), (
            "peer sync thread must start even with no peers.json — otherwise a "
            "peers.json written after the hub boots is silently ignored forever"
        )

    def test_peers_written_after_startup_are_picked_up(self, tmp_path, monkeypatch):
        import time as _time

        from mempalace import mcp_server as mcp

        calls = []

        def _fake_sync_all(ls, palace_path, transport=None):
            calls.append(palace_path)
            return []

        monkeypatch.setattr("mempalace.logsync.sync_all", _fake_sync_all)
        monkeypatch.setattr(mcp, "_get_logstream", lambda: object())

        assert self._start(tmp_path, monkeypatch)

        # peers.json appears only now — after the thread is already running.
        (tmp_path / "peers.json").write_text(
            json.dumps({"peers": [{"name": "a", "url": "http://x", "token": "t"}]}),
            encoding="utf-8",
        )

        deadline = _time.monotonic() + 5
        while _time.monotonic() < deadline and not calls:
            _time.sleep(0.05)
        assert calls, "sync round never ran; membership was latched at startup"

    def test_disabled_by_zero_interval(self, tmp_path, monkeypatch):
        assert not self._start(tmp_path, monkeypatch, interval="0")
