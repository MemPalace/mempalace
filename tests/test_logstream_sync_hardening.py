"""A sync peer is trusted for what it relays, not for its shape.

``apply_remote_event`` and ``apply_remote_artifact`` run the same checks on a
peer's data that ``append_event`` and ``put_artifact`` run on a local author,
``sync_with_peer`` refuses a page that does not advance the cursor it asked
from, and ``sync_all`` reports any peer failure instead of letting it stop
the round.
"""

import os

import pytest

from mempalace import logsync
from mempalace.logstream import Logstream

pytestmark = pytest.mark.usefixtures("tokenless_http_hub")


@pytest.fixture
def ls_a(tmp_dir):
    path = os.path.join(tmp_dir, "palace_a")
    os.makedirs(path)
    ls = Logstream(db_path=os.path.join(path, "logstream.sqlite3"))
    yield ls
    ls.close()


@pytest.fixture
def ls_b(tmp_dir):
    path = os.path.join(tmp_dir, "palace_b")
    os.makedirs(path)
    ls = Logstream(db_path=os.path.join(path, "logstream.sqlite3"))
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


def _remote_event(src, **overrides):
    """One of ``src``'s own ops, as /sync/ops would serve it, with edits."""
    _append(src, body="relayed")
    event = dict(src.list_ops(src.replica_id, 0)[-1])
    event.update(overrides)
    return event


class TestRemoteEventValidation:
    def test_a_clean_remote_event_still_applies(self, ls_a, ls_b):
        assert ls_a.apply_remote_event(_remote_event(ls_b), expected_origin=ls_b.replica_id)

    @pytest.mark.parametrize(
        "field, value, match",
        [
            ("id", "evt_x\nid: forged", "control characters"),
            ("id", "x" * 300, "maximum length"),
            ("type", "Task Request", "not a valid event type"),
            ("stream", 7, "must be a non-empty string"),
            ("status", "owned", "status="),
            ("body", ["not", "text"], "body must be a string"),
            ("origin_seq", "10", "origin_seq="),
            ("origin_seq", True, "origin_seq="),
            ("origin_seq", 10**18, "origin_seq="),
            ("origin_seq", -1, "origin_seq="),
            ("hlc", "a\x00b", "control characters"),
            ("artifact_ids", "art_1", "artifact_ids must be a list"),
            ("artifact_ids", ["ok", 3], "artifact_id must be a non-empty string"),
            ("metadata", "nope", "metadata must be an object"),
        ],
    )
    def test_malformed_fields_are_refused(self, ls_a, ls_b, field, value, match):
        event = _remote_event(ls_b, **{field: value})
        with pytest.raises(ValueError, match=match):
            ls_a.apply_remote_event(event)
        assert ls_a.version_vector().get(ls_b.replica_id) is None

    def test_body_over_the_cap_is_refused(self, ls_a, ls_b):
        event = _remote_event(ls_b, body="x" * (ls_a.max_body_bytes + 1))
        with pytest.raises(ValueError, match="maximum is"):
            ls_a.apply_remote_event(event)

    def test_event_for_another_origin_is_refused(self, ls_a, ls_b):
        event = _remote_event(ls_b)
        with pytest.raises(ValueError, match="not the origin"):
            ls_a.apply_remote_event(event, expected_origin="rep_cccccccccccc")
        # The forged cursor never lands in the version vector.
        assert ls_b.replica_id not in ls_a.version_vector()

    def test_a_non_object_is_refused(self, ls_a):
        with pytest.raises(ValueError, match="must be an object"):
            ls_a.apply_remote_event(["not", "an", "event"])


class TestRemoteArtifactValidation:
    @staticmethod
    def _artifact(src, **overrides):
        art = src.put_artifact(kind="note", content="hello", created_by="agent-b")
        record = dict(src.get_artifact(art["id"]))
        record.update(overrides)
        return record

    def test_a_clean_remote_artifact_still_applies(self, ls_a, ls_b):
        assert ls_a.apply_remote_artifact(self._artifact(ls_b))

    @pytest.mark.parametrize(
        "field, value, match",
        [
            ("kind", "exe", "kind="),
            ("sha256", "nothex", "64 lowercase hex"),
            ("sha256", "A" * 64, "64 lowercase hex"),
            ("content", 42, "content must be a non-empty string"),
            ("content", "a\x00b", "null bytes"),
            ("id", "art\n1", "control characters"),
            ("created_by", "x" * 300, "maximum length"),
        ],
    )
    def test_malformed_fields_are_refused(self, ls_a, ls_b, field, value, match):
        with pytest.raises(ValueError, match=match):
            ls_a.apply_remote_artifact(self._artifact(ls_b, **{field: value}))

    def test_oversized_content_is_refused_before_hashing(self, ls_a, ls_b):
        big = "x" * (ls_a.max_artifact_bytes + 1)
        with pytest.raises(ValueError, match="maximum is"):
            ls_a.apply_remote_artifact(self._artifact(ls_b, content=big, sha256="0" * 64))


class _StubTransport:
    def __init__(self, peers):
        self._peers = peers

    def peers(self):
        return list(self._peers)


class TestHostilePeer:
    """The anti-entropy round survives a peer that lies about its ops."""

    @staticmethod
    def _run(dst, fake_get):
        original = logsync._peer_get
        logsync._peer_get = fake_get
        try:
            return logsync.sync_with_peer(dst, "fake://evil")
        finally:
            logsync._peer_get = original

    def test_page_that_does_not_advance_ends_the_round(self, ls_a, ls_b):
        stale = _remote_event(ls_b)
        calls = []

        def fake_get(base_url, token, path, params=None):
            calls.append(path)
            if path == "/sync/version_vector":
                return {"replica_id": "rep_evil", "version_vector": {ls_b.replica_id: 10**6}}
            if path == "/sync/ops":
                return {"events": [stale]}  # the same op, round after round
            raise AssertionError(path)

        ls_a.apply_remote_event(stale)  # we already hold it: its seq == after
        with pytest.raises(logsync.SyncPeerError, match="served origin_seq"):
            self._run(ls_a, fake_get)
        assert calls.count("/sync/ops") == 1

    @pytest.mark.parametrize("top", ["10", True, -5, 10**18, None])
    def test_version_vector_that_is_not_a_sequence_is_refused(self, ls_a, ls_b, top):
        def fake_get(base_url, token, path, params=None):
            return {"replica_id": "rep_evil", "version_vector": {ls_b.replica_id: top}}

        with pytest.raises(logsync.SyncPeerError, match="not a sequence number"):
            self._run(ls_a, fake_get)

    def test_non_object_answers_are_refused(self, ls_a):
        with pytest.raises(logsync.SyncPeerError, match="did not answer with an object"):
            self._run(ls_a, lambda *a, **k: ["nope"])
        with pytest.raises(logsync.SyncPeerError, match="is not an object"):
            self._run(ls_a, lambda *a, **k: {"version_vector": ["nope"]})

    def test_event_served_under_another_origin_is_refused(self, ls_a, ls_b):
        event = _remote_event(ls_b)

        def fake_get(base_url, token, path, params=None):
            if path == "/sync/version_vector":
                return {"replica_id": "rep_evil", "version_vector": {"rep_cccccccccccc": 5}}
            return {"events": [event]}

        with pytest.raises(ValueError, match="not the origin"):
            self._run(ls_a, fake_get)
        assert ls_b.replica_id not in ls_a.version_vector()
        assert "rep_cccccccccccc" not in ls_a.version_vector()

    def test_round_is_bounded_even_when_the_peer_always_has_more(self, ls_a, ls_b, monkeypatch):
        monkeypatch.setattr(logsync, "_MAX_BATCHES_PER_ORIGIN", 3)
        monkeypatch.setattr(logsync, "_PULL_BATCH", 1)
        for _ in range(6):
            _append(ls_b)
        served = []

        def fake_get(base_url, token, path, params=None):
            if path == "/sync/version_vector":
                return {"replica_id": ls_b.replica_id, "version_vector": {ls_b.replica_id: 10**6}}
            page = ls_b.list_ops(params["origin"], after_seq=int(params["after"]), limit=1)
            served.append(len(page))
            return {"events": page}

        stats = self._run(ls_a, fake_get)
        assert stats["pulled_events"] == 3
        assert len(served) == 3
        # The next round carries on from where this one stopped.
        stats = self._run(ls_a, fake_get)
        assert stats["pulled_events"] == 3

    def test_sync_all_reports_any_peer_failure_and_continues(self, ls_a, ls_b, monkeypatch):
        def fake_get(base_url, token, path, params=None):
            if base_url == "fake://evil":
                raise RuntimeError("peer sent garbage")
            if path == "/sync/version_vector":
                return {"replica_id": ls_b.replica_id, "version_vector": ls_b.version_vector()}
            if path == "/sync/ops":
                return {
                    "events": ls_b.list_ops(
                        params["origin"], after_seq=int(params["after"]), limit=int(params["limit"])
                    )
                }
            raise AssertionError(path)

        _append(ls_b)
        monkeypatch.setattr(logsync, "_peer_get", fake_get)
        transport = _StubTransport([{"name": "evil", "url": "fake://evil"}, {"url": "fake://b"}])
        results = logsync.sync_all(ls_a, "unused", transport=transport)
        assert results[0]["error"] == "peer sent garbage"
        assert results[1]["pulled_events"] == 1
