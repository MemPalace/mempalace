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


# ── Watch (background watchers) ───────────────────────────────────────────


class TestWatchFilters:
    """The multi-valued and negative filters ``list_events`` cannot express."""

    def _event(self, **overrides):
        base = {
            "stream": "project/mempalace",
            "room": "delegation",
            "type": "task.request",
            "status": "open",
            "to_agent": "mac-claude",
            "from_agent": "windows-grok",
            "correlation_id": "task_1",
        }
        base.update(overrides)
        return base

    def test_normalize_treats_blank_as_absent_not_impossible(self):
        from mempalace.logstream import normalize_watch_values

        assert normalize_watch_values(None) is None
        assert normalize_watch_values("a") == {"a"}
        assert normalize_watch_values(["a", "b"]) == {"a", "b"}
        # A blank filter must mean "any", never "match nothing" — otherwise a
        # stray empty flag silently deafens the watcher forever.
        assert normalize_watch_values(["", None]) is None

    def test_own_broadcast_is_excluded(self):
        """The bug that motivated --agent.

        ``to_agent=<me>`` also matches '*' broadcasts, and an agent's own
        broadcasts are broadcasts — so without the exclusion a watcher wakes
        itself every time it posts a status.
        """
        from mempalace.logstream import event_matches_watch

        own = self._event(from_agent="mac-claude", to_agent="*")
        assert not event_matches_watch(
            own, to_agents={"mac-claude"}, exclude_from_agents={"mac-claude"}
        )
        # The same broadcast from anyone else still reaches us.
        other = self._event(from_agent="windows-grok", to_agent="*")
        assert event_matches_watch(
            other, to_agents={"mac-claude"}, exclude_from_agents={"mac-claude"}
        )

    def test_exclusion_beats_a_direct_address(self):
        from mempalace.logstream import event_matches_watch

        addressed = self._event(from_agent="noisy", to_agent="mac-claude")
        assert not event_matches_watch(
            addressed, to_agents={"mac-claude"}, exclude_from_agents={"noisy"}
        )

    def test_multi_valued_field_is_an_or(self):
        from mempalace.logstream import event_matches_watch

        evt = self._event(type="patch.ready")
        assert event_matches_watch(evt, types={"task.request", "patch.ready"})
        assert not event_matches_watch(evt, types={"task.request", "status.update"})

    def test_absent_filter_matches_anything(self):
        from mempalace.logstream import event_matches_watch

        assert event_matches_watch(self._event(), types=None, streams=None)

    def test_pushdown_only_takes_single_valued_filters(self):
        from mempalace.logstream import pushdown_watch_filters

        spec = {"types": {"task.request"}, "streams": {"a", "b"}, "rooms": None}
        pushed = pushdown_watch_filters(spec)
        # Multi-valued filters must stay client-side; pushing one arbitrary
        # value would silently drop the others.
        assert pushed == {"type": "task.request"}


class TestWatchEvents:
    def test_wakes_only_on_a_match_and_advances_cursor(self, logstream):
        noise = _append(logstream, type="status.update", from_agent="windows-grok")
        wanted = _append(logstream, type="patch.ready", from_agent="windows-grok")
        watcher = logstream.watch_events(
            poll_timeout_ms=200,
            poll_interval_s=0.01,
            types={"patch.ready"},
        )
        matched, cursor = next(watcher)
        assert [e["id"] for e in matched] == [wanted["id"]]
        # The cursor passes the rejected event too — re-judging it after a
        # restart would be pure waste.
        assert cursor == wanted["id"]
        assert noise["id"] != cursor
        watcher.close()

    def test_idle_poll_yields_so_callers_can_time_out(self, logstream):
        watcher = logstream.watch_events(
            poll_timeout_ms=50, poll_interval_s=0.01, correlation_id=None, types={"nothing"}
        )
        matched, cursor = next(watcher)
        assert matched == []
        assert cursor is None
        watcher.close()

    def test_cursor_resumes_without_replaying(self, logstream):
        first = _append(logstream)
        watcher = logstream.watch_events(poll_timeout_ms=100, poll_interval_s=0.01)
        matched, cursor = next(watcher)
        assert [e["id"] for e in matched] == [first["id"]]
        watcher.close()

        second = _append(logstream)
        resumed = logstream.watch_events(cursor=cursor, poll_timeout_ms=100, poll_interval_s=0.01)
        matched, _ = next(resumed)
        assert [e["id"] for e in matched] == [second["id"]]
        resumed.close()

    def test_self_broadcast_does_not_wake_the_author(self, logstream):
        """End-to-end version of the --agent bug, through the real store."""
        _append(logstream, from_agent="mac-claude", to_agent="*", type="status.update")
        watcher = logstream.watch_events(
            poll_timeout_ms=50,
            poll_interval_s=0.01,
            to_agents={"mac-claude"},
            exclude_from_agents={"mac-claude"},
        )
        matched, cursor = next(watcher)
        assert matched == []
        # Still examined, so the cursor moved past it.
        assert cursor is not None
        watcher.close()


class TestWatchCursorFile:
    def test_roundtrip(self, tmp_path):
        from mempalace.logstream import read_watch_cursor, write_watch_cursor

        path = str(tmp_path / "nested" / "cursor.json")
        write_watch_cursor(path, "evt_abc", agent="mac-claude")
        assert read_watch_cursor(path) == "evt_abc"

    def test_missing_or_corrupt_file_is_not_fatal(self, tmp_path):
        """A truncated state file costs a replay; refusing to start costs
        every event after it."""
        from mempalace.logstream import read_watch_cursor

        assert read_watch_cursor(str(tmp_path / "absent.json")) is None
        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("{not json", encoding="utf-8")
        assert read_watch_cursor(str(corrupt)) is None
        assert read_watch_cursor(None) is None

    def test_write_leaves_no_temp_file_behind(self, tmp_path):
        from mempalace.logstream import write_watch_cursor

        path = str(tmp_path / "cursor.json")
        write_watch_cursor(path, "evt_abc")
        assert [p.name for p in tmp_path.iterdir()] == ["cursor.json"]

    def test_non_object_json_is_treated_as_corrupt(self, tmp_path):
        """Valid JSON that is not an object must degrade, not raise.

        ``json.load(...).get()`` on ``null`` / ``[]`` / a bare string raises
        AttributeError, which would stop the watcher from starting — the
        exact opposite of the recovery contract.
        """
        from mempalace.logstream import read_watch_cursor

        for payload in ("null", "[]", '"evt_abc"', "42"):
            path = tmp_path / f"cursor_{abs(hash(payload))}.json"
            path.write_text(payload, encoding="utf-8")
            assert read_watch_cursor(str(path)) is None

    def test_conditions_distinguish_absent_empty_and_corrupt(self, tmp_path):
        """ "No cursor" is four facts; only ``absent`` may start at the tip."""
        from mempalace.logstream import (
            WATCH_STATE_ABSENT,
            WATCH_STATE_CORRUPT,
            WATCH_STATE_EMPTY,
            WATCH_STATE_OK,
            read_watch_state,
            write_watch_cursor,
        )

        assert read_watch_state(str(tmp_path / "nope.json")) == (None, WATCH_STATE_ABSENT)
        assert read_watch_state(None) == (None, WATCH_STATE_ABSENT)

        good = tmp_path / "good.json"
        write_watch_cursor(str(good), "evt_abc")
        assert read_watch_state(str(good)) == ("evt_abc", WATCH_STATE_OK)

        # Empty-log sentinel: the file exists and says so explicitly.
        empty = tmp_path / "empty.json"
        write_watch_cursor(str(empty), None)
        assert read_watch_state(str(empty)) == (None, WATCH_STATE_EMPTY)

        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        assert read_watch_state(str(broken)) == (None, WATCH_STATE_CORRUPT)
        broken.write_text("null", encoding="utf-8")
        assert read_watch_state(str(broken)) == (None, WATCH_STATE_CORRUPT)
        broken.write_text('{"other": 1}', encoding="utf-8")
        assert read_watch_state(str(broken)) == (None, WATCH_STATE_CORRUPT)

    def test_unreachable_file_is_corrupt_not_absent(self, tmp_path, monkeypatch):
        """A checkpoint we cannot open must replay, never restart at the tip.

        ``os.path.exists`` answers False both for "no such file" and for
        "cannot traverse the parent directory", so a preflight check turns a
        momentarily unreachable checkpoint into a fake first run and skips
        every event since the stored cursor.
        """
        from mempalace.logstream import (
            WATCH_STATE_ABSENT,
            WATCH_STATE_CORRUPT,
            read_watch_state,
        )

        # A directory where a file is expected: open() raises OSError on
        # every platform (IsADirectoryError on POSIX, PermissionError on NT).
        as_dir = tmp_path / "cursor.json"
        as_dir.mkdir()
        assert read_watch_state(str(as_dir)) == (None, WATCH_STATE_CORRUPT)

        # Permission denied, simulated so the test is platform-independent.
        real_open = open

        def denied(path, *a, **k):
            if str(path).endswith("locked.json"):
                raise PermissionError(13, "Permission denied")
            return real_open(path, *a, **k)

        monkeypatch.setattr("builtins.open", denied)
        assert read_watch_state(str(tmp_path / "locked.json")) == (None, WATCH_STATE_CORRUPT)

        # A genuinely missing file is still absent, i.e. a real first run.
        monkeypatch.undo()
        assert read_watch_state(str(tmp_path / "gone.json")) == (None, WATCH_STATE_ABSENT)

    def test_required_checkpoint_raises_instead_of_swallowing(self, tmp_path, monkeypatch):
        """Best effort is safe only when a lost checkpoint costs a replay.

        For the first checkpoint of a fresh watch it costs a skip instead, so
        that one must surface the failure.
        """
        from mempalace.logstream import write_watch_cursor

        target = str(tmp_path / "sub" / "cursor.json")

        def denied(path, *a, **k):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr("builtins.open", denied)
        # Ordinary checkpoint: swallowed, the watcher keeps running.
        write_watch_cursor(target, "evt_abc")
        # Initial checkpoint: raised, so the caller can refuse to start.
        with pytest.raises(OSError):
            write_watch_cursor(target, "evt_abc", required=True)


# ── Topic routing, query ordering, and migration ──────────────────────────


class TestTopicAndOrder:
    def test_migration_pre_topic_db(self, palace_path):
        """Pre-topic database upgrades idempotently and preserves existing rows."""
        db_path = os.path.join(palace_path, "logstream.sqlite3")
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE events (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                stream TEXT NOT NULL,
                room TEXT NOT NULL,
                from_agent TEXT NOT NULL,
                to_agent TEXT,
                correlation_id TEXT,
                branch TEXT,
                base_commit TEXT,
                status TEXT,
                body TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE artifacts (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE event_artifacts (
                event_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                PRIMARY KEY (event_id, artifact_id)
            );
            INSERT INTO events (id, type, stream, room, from_agent, created_at)
            VALUES ('evt_pre_migration', 'task.request', 'project/legacy', 'delegation', 'agent-old', '2026-08-01T12:00:00Z');
        """)
        conn.commit()
        conn.close()

        ls = Logstream(db_path=db_path)
        try:
            events = ls.list_events(stream="project/legacy")
            assert len(events) == 1
            assert events[0]["id"] == "evt_pre_migration"
            assert events[0]["topic"] is None

            # Topic column and index exist
            raw_conn = sqlite3.connect(db_path)
            cols = {r[1] for r in raw_conn.execute("PRAGMA table_info(events)").fetchall()}
            assert "topic" in cols
            indices = {
                r[1]
                for r in raw_conn.execute(
                    "SELECT * FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            assert "events_topic_created_idx" in indices
            raw_conn.close()

            # Appending topic-bearing event works
            new_evt = _append(ls, topic="auth-upgrade")
            assert new_evt["topic"] == "auth-upgrade"
            filtered = ls.list_events(topic="auth-upgrade")
            assert len(filtered) == 1
            assert filtered[0]["id"] == new_evt["id"]
        finally:
            ls.close()

        # Reopen is idempotent
        reopened = Logstream(db_path=db_path)
        try:
            all_events = reopened.list_events(limit=10)
            assert len(all_events) == 2
        finally:
            reopened.close()

    def test_topic_append_and_list_filtering(self, logstream):
        e1 = _append(logstream, topic="auth-v2", body="Auth work")
        e2 = _append(logstream, topic="ui-v2", body="UI work")
        e3 = _append(logstream, topic=None, body="General work")

        assert e1["topic"] == "auth-v2"
        assert e2["topic"] == "ui-v2"
        assert e3["topic"] is None

        auth_events = logstream.list_events(topic="auth-v2")
        assert [e["id"] for e in auth_events] == [e1["id"]]

        ui_events = logstream.list_events(topic="ui-v2")
        assert [e["id"] for e in ui_events] == [e2["id"]]

        none_events = logstream.list_events(topic="nonexistent")
        assert none_events == []

    def test_ack_topic_inheritance_and_override(self, logstream):
        e = _append(logstream, topic="compiler-team")
        ack1 = logstream.ack_event(e["id"], from_agent="windows-codex", status="claimed")
        assert ack1["topic"] == "compiler-team"

        ack2 = logstream.ack_event(
            e["id"], from_agent="windows-codex", status="claimed", topic="custom-override"
        )
        assert ack2["topic"] == "custom-override"

    def test_submit_patch_with_topic(self, logstream):
        res = logstream.submit_patch(
            content="diff --git a/a b/b\n",
            from_agent="agent-a",
            stream="project/mempalace",
            topic="fast-path",
        )
        assert res["event"]["topic"] == "fast-path"
        assert logstream.list_events(topic="fast-path")[0]["id"] == res["event"]["id"]

    def test_order_asc_and_desc(self, logstream):
        e1 = _append(logstream, body="First")
        e2 = _append(logstream, body="Second")
        e3 = _append(logstream, body="Third")

        asc = logstream.list_events(order="asc")
        assert [e["id"] for e in asc] == [e1["id"], e2["id"], e3["id"]]

        desc = logstream.list_events(order="desc")
        assert [e["id"] for e in desc] == [e3["id"], e2["id"], e1["id"]]

        tail = logstream.list_events(order="desc", limit=2)
        assert [e["id"] for e in tail] == [e3["id"], e2["id"]]

        with pytest.raises(ValueError, match="order='sideways'"):
            logstream.list_events(order="sideways")

    def test_since_event_id_cursor_invariance_with_order(self, logstream):
        e1 = _append(logstream, body="1")
        e2 = _append(logstream, body="2")
        e3 = _append(logstream, body="3")

        # since_event_id is strictly after e1 (rowid > e1["rowid"])
        asc = logstream.list_events(since_event_id=e1["id"], order="asc")
        assert [e["id"] for e in asc] == [e2["id"], e3["id"]]

        desc = logstream.list_events(since_event_id=e1["id"], order="desc")
        assert [e["id"] for e in desc] == [e3["id"], e2["id"]]

    def test_before_event_id_filtering(self, logstream):
        e1 = _append(logstream, body="1")
        e2 = _append(logstream, body="2")
        e3 = _append(logstream, body="3")

        before_asc = logstream.list_events(before_event_id=e3["id"], order="asc")
        assert [e["id"] for e in before_asc] == [e1["id"], e2["id"]]

        before_desc = logstream.list_events(before_event_id=e3["id"], order="desc")
        assert [e["id"] for e in before_desc] == [e2["id"], e1["id"]]

        with pytest.raises(ValueError, match="before_event_id 'evt_nope' not found"):
            logstream.list_events(before_event_id="evt_nope")

    def test_watch_topic_spec_and_matching(self):
        from mempalace.logstream import (
            event_matches_watch,
            pushdown_watch_filters,
            sanitize_watch_spec,
        )

        spec = sanitize_watch_spec({"topics": {" auth ", "ui "}})
        assert spec["topics"] == {"auth", "ui"}

        pushdown_single = pushdown_watch_filters({"topics": {"auth"}})
        assert pushdown_single == {"topic": "auth"}

        pushdown_multi = pushdown_watch_filters({"topics": {"auth", "ui"}})
        assert "topic" not in pushdown_multi

        event_auth = {"stream": "p", "room": "r", "topic": "auth", "from_agent": "a"}
        event_ui = {"stream": "p", "room": "r", "topic": "ui", "from_agent": "a"}
        event_other = {"stream": "p", "room": "r", "topic": "other", "from_agent": "a"}
        event_none = {"stream": "p", "room": "r", "topic": None, "from_agent": "a"}

        assert event_matches_watch(event_auth, topics={"auth", "ui"}) is True
        assert event_matches_watch(event_ui, topics={"auth", "ui"}) is True
        assert event_matches_watch(event_other, topics={"auth", "ui"}) is False
        assert event_matches_watch(event_none, topics={"auth", "ui"}) is False
