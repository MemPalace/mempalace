"""
test_logstream.py — Tests for the RFC 003 agent coordination logstream.

Covers the durable SQLite core in mempalace/logstream.py: schema init,
append/list round trips, structured filters, cursor semantics, wait
(immediate, timeout, and cross-thread), exact artifact storage, ack
immutability, and size-limit errors.
"""

import hashlib
import os
import sqlite3
import threading

import pytest

from mempalace.logstream import (
    DEFAULT_MAX_ARTIFACT_BYTES,
    DEFAULT_MAX_BODY_BYTES,
    MAX_WAIT_TIMEOUT_MS,
    Logstream,
)


@pytest.fixture
def logstream(palace_path):
    """An isolated Logstream inside an empty palace dir."""
    ls = Logstream(db_path=os.path.join(palace_path, "logstream.sqlite3"))
    yield ls
    ls.close()


def _append(ls, **overrides):
    """Append a minimal valid event, overridable per test."""
    fields = {
        "type": "task.request",
        "stream": "project/mempalace",
        "room": "delegation",
        "from_agent": "mac-codex",
        "to_agent": "windows-codex",
        "correlation_id": "task_123",
        "body": "Please fix search echo ranking.",
    }
    fields.update(overrides)
    return ls.append_event(**fields)


# ── Schema / init ─────────────────────────────────────────────────────────


class TestInit:
    def test_schema_initializes_in_empty_palace_dir(self, palace_path):
        db_path = os.path.join(palace_path, "logstream.sqlite3")
        ls = Logstream(db_path=db_path)
        try:
            assert os.path.exists(db_path)
            conn = sqlite3.connect(db_path)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            conn.close()
            assert {"events", "artifacts", "event_artifacts"} <= tables
        finally:
            ls.close()

    def test_init_creates_missing_parent_dirs(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "nested", "palace", "logstream.sqlite3")
        ls = Logstream(db_path=db_path)
        try:
            assert os.path.exists(db_path)
        finally:
            ls.close()

    def test_reopen_preserves_events(self, palace_path):
        db_path = os.path.join(palace_path, "logstream.sqlite3")
        ls = Logstream(db_path=db_path)
        evt = _append(ls)
        ls.close()

        reopened = Logstream(db_path=db_path)
        try:
            events = reopened.list_events(stream="project/mempalace")
            assert [e["id"] for e in events] == [evt["id"]]
        finally:
            reopened.close()


# ── Append / list round trip ──────────────────────────────────────────────


class TestAppendList:
    def test_append_list_round_trip(self, logstream):
        evt = _append(
            logstream,
            branch="feat/shared-brain-dogfood",
            base_commit="2668053",
            status="open",
            metadata={"priority": "high"},
        )
        assert evt["id"].startswith("evt_")
        assert evt["created_at"].endswith("Z")

        events = logstream.list_events(stream="project/mempalace")
        assert len(events) == 1
        stored = events[0]
        assert stored == evt
        assert stored["type"] == "task.request"
        assert stored["room"] == "delegation"
        assert stored["from_agent"] == "mac-codex"
        assert stored["to_agent"] == "windows-codex"
        assert stored["correlation_id"] == "task_123"
        assert stored["branch"] == "feat/shared-brain-dogfood"
        assert stored["base_commit"] == "2668053"
        assert stored["status"] == "open"
        assert stored["body"] == "Please fix search echo ranking."
        assert stored["metadata"] == {"priority": "high"}

    def test_body_stored_verbatim(self, logstream):
        body = "line one\n  indented\ttabbed\nunicode: héllo ✓ 中文\n"
        evt = _append(logstream, body=body)
        assert logstream.list_events(correlation_id="task_123")[0]["body"] == body
        assert evt["body"] == body

    def test_events_are_ordered_by_append_order(self, logstream):
        ids = [_append(logstream, body=f"event {i}")["id"] for i in range(5)]
        events = logstream.list_events(stream="project/mempalace", limit=10)
        assert [e["id"] for e in events] == ids
        assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)

    def test_limit_and_default(self, logstream):
        for i in range(7):
            _append(logstream, body=f"event {i}")
        assert len(logstream.list_events(limit=3)) == 3
        assert len(logstream.list_events()) == 7

    def test_invalid_inputs_rejected(self, logstream):
        with pytest.raises(ValueError, match="type"):
            _append(logstream, type="Not A Type!")
        with pytest.raises(ValueError, match="stream"):
            _append(logstream, stream="")
        with pytest.raises(ValueError, match="from_agent"):
            _append(logstream, from_agent=None)
        with pytest.raises(ValueError, match="status"):
            _append(logstream, status="bogus")
        with pytest.raises(ValueError, match="metadata"):
            _append(logstream, metadata={"bad": object()})
        with pytest.raises(ValueError, match="control"):
            _append(logstream, room="del\negation")

    def test_unknown_artifact_id_rejected(self, logstream):
        with pytest.raises(ValueError, match="unknown artifact"):
            _append(logstream, artifact_ids=["art_missing"])
        # The failed append must not leave a partial event behind.
        assert logstream.list_events() == []


# ── Filters ───────────────────────────────────────────────────────────────


class TestFilters:
    @pytest.fixture
    def seeded(self, logstream):
        _append(logstream, type="task.request", room="delegation", correlation_id="task_a")
        _append(
            logstream,
            type="patch.ready",
            room="patches",
            from_agent="windows-codex",
            to_agent="mac-codex",
            correlation_id="task_a",
            status="ready",
        )
        _append(
            logstream,
            type="task.request",
            stream="shared_agent_brain",
            room="delegation",
            to_agent="*",
            correlation_id="task_b",
        )
        return logstream

    def test_filter_by_stream(self, seeded):
        assert len(seeded.list_events(stream="project/mempalace")) == 2
        assert len(seeded.list_events(stream="shared_agent_brain")) == 1

    def test_filter_by_room(self, seeded):
        assert len(seeded.list_events(room="patches")) == 1
        assert len(seeded.list_events(room="delegation")) == 2

    def test_filter_by_type(self, seeded):
        assert len(seeded.list_events(type="patch.ready")) == 1
        assert len(seeded.list_events(type="task.request")) == 2

    def test_filter_by_from_agent(self, seeded):
        assert len(seeded.list_events(from_agent="windows-codex")) == 1

    def test_filter_by_correlation_id(self, seeded):
        assert len(seeded.list_events(correlation_id="task_a")) == 2
        assert len(seeded.list_events(correlation_id="task_b")) == 1

    def test_filter_by_status(self, seeded):
        assert len(seeded.list_events(status="ready")) == 1

    def test_to_agent_filter_includes_broadcast(self, seeded):
        # windows-codex sees its direct event plus the '*' broadcast.
        events = seeded.list_events(to_agent="windows-codex")
        assert {e["to_agent"] for e in events} == {"windows-codex", "*"}

    def test_combined_filters(self, seeded):
        events = seeded.list_events(
            stream="project/mempalace", type="patch.ready", to_agent="mac-codex"
        )
        assert len(events) == 1
        assert events[0]["status"] == "ready"

    def test_since_event_id_cursor_is_exclusive(self, seeded):
        all_events = seeded.list_events()
        after_first = seeded.list_events(since_event_id=all_events[0]["id"])
        assert [e["id"] for e in after_first] == [e["id"] for e in all_events[1:]]
        assert seeded.list_events(since_event_id=all_events[-1]["id"]) == []

    def test_since_event_id_unknown_raises(self, seeded):
        with pytest.raises(ValueError, match="not found"):
            seeded.list_events(since_event_id="evt_nope")

    def test_since_created_at_is_inclusive(self, seeded):
        first = seeded.list_events()[0]
        events = seeded.list_events(since_created_at=first["created_at"])
        assert first["id"] in {e["id"] for e in events}

    def test_since_created_at_rejects_junk(self, seeded):
        with pytest.raises(ValueError, match="since_created_at"):
            seeded.list_events(since_created_at="yesterday")

    def test_latest_event_id_tracks_newest(self, logstream):
        assert logstream.latest_event_id() is None
        _append(logstream, body="first")
        newest = _append(logstream, body="second")
        assert logstream.latest_event_id() == newest["id"]


# ── Wait ──────────────────────────────────────────────────────────────────


class TestWait:
    def test_wait_returns_immediately_when_event_exists(self, logstream):
        evt = _append(logstream)
        result = logstream.wait_events(
            timeout_ms=5_000, correlation_id="task_123", type="task.request"
        )
        assert result["timed_out"] is False
        assert [e["id"] for e in result["events"]] == [evt["id"]]

    def test_wait_times_out_cleanly(self, logstream):
        result = logstream.wait_events(
            timeout_ms=150, poll_interval_s=0.02, correlation_id="task_none"
        )
        assert result == {"timed_out": True, "events": []}

    def test_wait_timeout_is_clamped_to_max(self, logstream):
        _append(logstream)
        # An over-max timeout must not error; the pre-existing event
        # returns immediately regardless.
        result = logstream.wait_events(
            timeout_ms=MAX_WAIT_TIMEOUT_MS * 100, correlation_id="task_123"
        )
        assert result["timed_out"] is False

    def test_wait_rejects_negative_timeout(self, logstream):
        with pytest.raises(ValueError, match="timeout_ms"):
            logstream.wait_events(timeout_ms=-1)

    def test_concurrent_waiter_sees_appended_event(self, logstream):
        """Integration: one thread waits, another appends, waiter returns."""
        results = {}

        def waiter():
            results["wait"] = logstream.wait_events(
                timeout_ms=10_000,
                poll_interval_s=0.02,
                correlation_id="task_threaded",
                type="patch.ready",
            )

        t = threading.Thread(target=waiter)
        t.start()
        _append(
            logstream,
            type="patch.ready",
            from_agent="windows-codex",
            to_agent="mac-codex",
            correlation_id="task_threaded",
            status="ready",
        )
        t.join(timeout=15)
        assert not t.is_alive()
        assert results["wait"]["timed_out"] is False
        assert results["wait"]["events"][0]["correlation_id"] == "task_threaded"


# ── Artifacts ─────────────────────────────────────────────────────────────


class TestArtifacts:
    PATCH = "diff --git a/mempalace/searcher.py b/mempalace/searcher.py\n+fixed\n"

    def test_put_get_preserves_exact_content(self, logstream):
        content = self.PATCH + "trailing spaces  \n\ttabs\nunicode ✓\n"
        artifact = logstream.put_artifact(kind="patch", content=content, created_by="windows-codex")
        fetched = logstream.get_artifact(artifact["id"])
        assert fetched["content"] == content
        assert fetched["kind"] == "patch"
        assert fetched["created_by"] == "windows-codex"

    def test_artifact_hash_and_size_are_stable(self, logstream):
        artifact = logstream.put_artifact(
            kind="patch", content=self.PATCH, created_by="windows-codex"
        )
        expected = hashlib.sha256(self.PATCH.encode("utf-8")).hexdigest()
        assert artifact["sha256"] == expected
        assert artifact["size_bytes"] == len(self.PATCH.encode("utf-8"))
        fetched = logstream.get_artifact(artifact["id"])
        assert fetched["sha256"] == expected
        assert fetched["size_bytes"] == artifact["size_bytes"]

    def test_get_missing_artifact_returns_none(self, logstream):
        assert logstream.get_artifact("art_nope") is None

    def test_invalid_kind_rejected(self, logstream):
        with pytest.raises(ValueError, match="kind"):
            logstream.put_artifact(kind="binary", content="x", created_by="a")

    def test_event_references_artifact(self, logstream):
        artifact = logstream.put_artifact(
            kind="patch", content=self.PATCH, created_by="windows-codex"
        )
        evt = _append(logstream, type="patch.ready", artifact_ids=[artifact["id"]])
        assert evt["artifact_ids"] == [artifact["id"]]
        listed = logstream.list_events(type="patch.ready")
        assert listed[0]["artifact_ids"] == [artifact["id"]]


class TestPatchContentWarnings:
    """Advisory guards for unappliable diffs, found in the first dogfood:
    a patch stored without its trailing newline is rejected by git apply."""

    def test_patch_without_trailing_newline_warns(self, logstream):
        artifact = logstream.put_artifact(
            kind="patch",
            content="diff --git a/x b/x\n+no trailing newline",
            created_by="windows-codex",
        )
        assert any("trailing newline" in w for w in artifact["warnings"])
        # Content is still stored verbatim — the warning never mutates it.
        assert logstream.get_artifact(artifact["id"])["content"].endswith("newline")

    def test_patch_with_crlf_warns(self, logstream):
        artifact = logstream.put_artifact(
            kind="patch",
            content="diff --git a/x b/x\r\n+crlf\r\n",
            created_by="windows-codex",
        )
        assert any("carriage returns" in w for w in artifact["warnings"])

    def test_clean_patch_has_no_warnings_key(self, logstream):
        artifact = logstream.put_artifact(
            kind="patch", content=TestArtifacts.PATCH, created_by="windows-codex"
        )
        assert "warnings" not in artifact

    def test_non_patch_kinds_never_warn(self, logstream):
        artifact = logstream.put_artifact(
            kind="log", content="no trailing newline", created_by="windows-codex"
        )
        assert "warnings" not in artifact

    def test_submit_patch_propagates_warnings(self, logstream):
        result = logstream.submit_patch(
            content="diff --git a/x b/x\n+truncated",
            from_agent="windows-codex",
            stream="project/mempalace",
        )
        assert any("trailing newline" in w for w in result["artifact"]["warnings"])


# ── Ack ───────────────────────────────────────────────────────────────────


class TestAck:
    def test_ack_creates_new_event_and_does_not_mutate_target(self, logstream):
        target = _append(logstream, status="open")
        ack = logstream.ack_event(
            target["id"], from_agent="windows-codex", status="applied", body="Done."
        )
        assert ack["id"] != target["id"]
        assert ack["type"] == "event.ack"
        assert ack["correlation_id"] == target["correlation_id"]
        assert ack["to_agent"] == target["from_agent"]
        assert ack["status"] == "applied"
        assert ack["metadata"] == {"ack_of": target["id"]}

        original = logstream.list_events(type="task.request")[0]
        assert original["status"] == "open"
        assert original["body"] == target["body"]

    def test_ack_falls_back_to_target_id_as_correlation(self, logstream):
        target = _append(logstream, correlation_id=None)
        ack = logstream.ack_event(target["id"], from_agent="windows-codex")
        assert ack["correlation_id"] == target["id"]

    def test_ack_unknown_event_raises(self, logstream):
        with pytest.raises(ValueError, match="not found"):
            logstream.ack_event("evt_nope", from_agent="mac-codex")


# ── Patch submit ──────────────────────────────────────────────────────────


class TestSubmitPatch:
    def test_submit_patch_stores_artifact_and_event(self, logstream):
        result = logstream.submit_patch(
            content=TestArtifacts.PATCH,
            from_agent="windows-codex",
            stream="project/mempalace",
            to_agent="mac-codex",
            correlation_id="task_123",
            branch="feat/shared-brain-dogfood",
            base_commit="2668053",
            body="Search ranking patch is ready.",
        )
        event = result["event"]
        artifact = result["artifact"]
        assert event["type"] == "patch.ready"
        assert event["status"] == "ready"
        assert event["room"] == "patches"
        assert event["artifact_ids"] == [artifact["id"]]
        fetched = logstream.get_artifact(artifact["id"])
        assert fetched["content"] == TestArtifacts.PATCH
        assert fetched["sha256"] == artifact["sha256"]

    def test_listed_patch_events_never_dangle(self, logstream):
        """Every artifact id visible on a listed event must resolve."""
        for i in range(3):
            logstream.submit_patch(
                content=f"diff --git a/f{i} b/f{i}\n",
                from_agent="windows-codex",
                stream="project/mempalace",
                correlation_id=f"task_{i}",
            )
        for event in logstream.list_events(type="patch.ready"):
            for artifact_id in event["artifact_ids"]:
                assert logstream.get_artifact(artifact_id) is not None


# ── Size limits ───────────────────────────────────────────────────────────


class TestSizeLimits:
    def test_oversized_body_rejected(self, logstream):
        big = "x" * (DEFAULT_MAX_BODY_BYTES + 1)
        with pytest.raises(ValueError, match="bytes"):
            _append(logstream, body=big)

    def test_oversized_artifact_rejected(self, logstream):
        big = "x" * (DEFAULT_MAX_ARTIFACT_BYTES + 1)
        with pytest.raises(ValueError, match="bytes"):
            logstream.put_artifact(kind="file", content=big, created_by="a")

    def test_limits_measure_utf8_bytes_not_chars(self, palace_path):
        ls = Logstream(
            db_path=os.path.join(palace_path, "logstream.sqlite3"),
            max_body_bytes=10,
        )
        try:
            with pytest.raises(ValueError, match="bytes"):
                _append(ls, body="éééééé")  # 6 chars, 12 UTF-8 bytes
        finally:
            ls.close()

    def test_body_at_limit_accepted(self, palace_path):
        ls = Logstream(
            db_path=os.path.join(palace_path, "logstream.sqlite3"),
            max_body_bytes=10,
        )
        try:
            evt = _append(ls, body="x" * 10)
            assert evt["body"] == "x" * 10
        finally:
            ls.close()
