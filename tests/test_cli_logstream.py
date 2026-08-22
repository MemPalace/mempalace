"""Tests for the RFC 003 logstream/artifact CLI commands.

Covers cmd_logstream (append/list/wait/ack) and cmd_artifact (put/get):
JSON and human output, exact-content stdout piping, timeout exit code,
and error exits. Uses SimpleNamespace args like the rest of test_cli.py.
"""

import json
import sys
import time
from types import SimpleNamespace

import pytest

from mempalace.cli import cmd_artifact, cmd_logstream, main


def _append_args(palace, **overrides):
    fields = dict(
        palace=palace,
        logstream_action="append",
        type="task.request",
        stream="project/mempalace",
        room="delegation",
        from_agent="mac-fable",
        to_agent="windows-codex",
        correlation_id="task_cli",
        branch=None,
        base_commit=None,
        status=None,
        body="Please fix the thing.",
        body_file=None,
        metadata=None,
        artifact_id=None,
        json=True,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _list_args(palace, **overrides):
    fields = dict(
        palace=palace,
        logstream_action="list",
        stream=None,
        room=None,
        type=None,
        to_agent=None,
        from_agent=None,
        correlation_id=None,
        status=None,
        since_event_id=None,
        since_created_at=None,
        limit=50,
        json=True,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _wait_args(palace, **overrides):
    args = _list_args(palace, **overrides)
    args.logstream_action = "wait"
    if not hasattr(args, "timeout_ms"):
        args.timeout_ms = 100
    return args


def _put_args(palace, content, **overrides):
    fields = dict(
        palace=palace,
        artifact_action="put",
        kind="patch",
        created_by="windows-codex",
        content=content,
        file=None,
        metadata=None,
        json=True,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


class TestLogstreamCli:
    def test_append_then_list_json_round_trip(self, palace_path, capsys):
        cmd_logstream(_append_args(palace_path))
        appended = json.loads(capsys.readouterr().out)
        assert appended["id"].startswith("evt_")

        cmd_logstream(_list_args(palace_path, correlation_id="task_cli"))
        listed = json.loads(capsys.readouterr().out)
        assert listed["count"] == 1
        assert listed["events"][0]["id"] == appended["id"]
        assert listed["events"][0]["body"] == "Please fix the thing."

    def test_append_human_output(self, palace_path, capsys):
        cmd_logstream(_append_args(palace_path, json=False))
        out = capsys.readouterr().out
        assert "Appended:" in out
        assert "task.request" in out
        assert "project/mempalace/delegation" in out
        assert "mac-fable->windows-codex" in out

    def test_append_invalid_status_exits_1(self, palace_path, capsys):
        with pytest.raises(SystemExit) as exc:
            cmd_logstream(_append_args(palace_path, status="bogus"))
        assert exc.value.code == 1
        assert "status" in json.loads(capsys.readouterr().out)["error"]

    def test_append_invalid_metadata_exits_1(self, palace_path, capsys):
        with pytest.raises(SystemExit) as exc:
            cmd_logstream(_append_args(palace_path, metadata="not json"))
        assert exc.value.code == 1
        assert "metadata" in json.loads(capsys.readouterr().out)["error"]

    def test_body_file_stdin(self, palace_path, capsys, monkeypatch):
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("body from stdin\n"))
        cmd_logstream(_append_args(palace_path, body=None, body_file="-"))
        appended = json.loads(capsys.readouterr().out)
        assert appended["body"] == "body from stdin\n"

    def test_wait_existing_event_returns_immediately(self, palace_path, capsys):
        cmd_logstream(_append_args(palace_path))
        capsys.readouterr()
        cmd_logstream(_wait_args(palace_path, correlation_id="task_cli", timeout_ms=5000))
        result = json.loads(capsys.readouterr().out)
        assert result["timed_out"] is False
        assert result["count"] == 1

    def test_wait_timeout_exits_2(self, palace_path, capsys):
        with pytest.raises(SystemExit) as exc:
            cmd_logstream(_wait_args(palace_path, correlation_id="task_never", timeout_ms=100))
        assert exc.value.code == 2
        result = json.loads(capsys.readouterr().out)
        assert result["timed_out"] is True

    def test_ack_round_trip(self, palace_path, capsys):
        cmd_logstream(_append_args(palace_path))
        appended = json.loads(capsys.readouterr().out)
        cmd_logstream(
            SimpleNamespace(
                palace=palace_path,
                logstream_action="ack",
                event_id=appended["id"],
                from_agent="windows-codex",
                status="applied",
                body="Done.",
                json=True,
            )
        )
        ack = json.loads(capsys.readouterr().out)
        assert ack["type"] == "event.ack"
        assert ack["to_agent"] == "mac-fable"
        assert ack["correlation_id"] == "task_cli"


class TestArtifactCli:
    PATCH = "diff --git a/x b/x\n+cli\n"

    def test_put_then_get_stdout_is_exact(self, palace_path, capsys):
        cmd_artifact(_put_args(palace_path, self.PATCH))
        artifact = json.loads(capsys.readouterr().out)
        assert artifact["id"].startswith("art_")

        cmd_artifact(
            SimpleNamespace(
                palace=palace_path,
                artifact_action="get",
                artifact_id=artifact["id"],
                out=None,
                json=False,
            )
        )
        # Exact content, nothing else — must survive `| git apply`.
        assert capsys.readouterr().out == self.PATCH

    def test_get_out_writes_file(self, palace_path, tmp_dir, capsys):
        cmd_artifact(_put_args(palace_path, self.PATCH))
        artifact = json.loads(capsys.readouterr().out)
        out_path = f"{tmp_dir}/fetched.patch"
        cmd_artifact(
            SimpleNamespace(
                palace=palace_path,
                artifact_action="get",
                artifact_id=artifact["id"],
                out=out_path,
                json=True,
            )
        )
        meta = json.loads(capsys.readouterr().out)
        assert "content" not in meta
        assert meta["content_written_to"] == out_path
        assert open(out_path, encoding="utf-8").read() == self.PATCH

    def test_get_missing_exits_1(self, palace_path, capsys):
        with pytest.raises(SystemExit) as exc:
            cmd_artifact(
                SimpleNamespace(
                    palace=palace_path,
                    artifact_action="get",
                    artifact_id="art_nope",
                    out=None,
                    json=True,
                )
            )
        assert exc.value.code == 1
        assert "not found" in json.loads(capsys.readouterr().out)["error"]

    def test_put_reads_stdin_by_default(self, palace_path, capsys, monkeypatch):
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(self.PATCH))
        cmd_artifact(_put_args(palace_path, None))
        artifact = json.loads(capsys.readouterr().out)
        assert artifact["size_bytes"] == len(self.PATCH.encode("utf-8"))

    def test_event_can_reference_cli_artifact(self, palace_path, capsys):
        cmd_artifact(_put_args(palace_path, self.PATCH))
        artifact = json.loads(capsys.readouterr().out)
        cmd_logstream(_append_args(palace_path, type="patch.ready", artifact_id=[artifact["id"]]))
        event = json.loads(capsys.readouterr().out)
        assert event["artifact_ids"] == [artifact["id"]]


class TestMainDispatch:
    def test_main_dispatches_logstream_list(self, palace_path, capsys, monkeypatch):
        monkeypatch.setattr(
            sys,
            "argv",
            ["mempalace", "--palace", palace_path, "logstream", "list", "--json"],
        )
        main()
        result = json.loads(capsys.readouterr().out)
        assert result == {"events": [], "count": 0}

    def test_main_dispatches_artifact_put(self, palace_path, capsys, monkeypatch):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "mempalace",
                "--palace",
                palace_path,
                "artifact",
                "put",
                "--kind",
                "note",
                "--created-by",
                "mac-fable",
                "--content",
                "hello",
                "--json",
            ],
        )
        main()
        artifact = json.loads(capsys.readouterr().out)
        assert artifact["kind"] == "note"
        assert artifact["size_bytes"] == 5


class TestVerbatimNewlines:
    """CRLF content must survive the CLI byte-for-byte.

    Every read/write path here used Python text mode, whose universal-newline
    translation rewrites \\r\\n to \\n on read and (on Windows) \\n back to
    \\r\\n on write. For a store whose whole contract is verbatim bytes
    addressed by sha256, that is silent corruption: a patch produced by a
    Windows agent arrives on another machine as different bytes with a
    different digest.

    It also disarmed the CRLF warning in put_artifact — the \\r it looks for
    was already stripped before the content got there, so the one check meant
    to catch unappliable diffs could never fire on the platform that produces
    them.
    """

    CRLF_PATCH = "diff --git a/x b/x\r\n--- a/x\r\n+++ b/x\r\n@@ -1 +1 @@\r\n-old\r\n+new\r\n"

    def _stdin(self, monkeypatch, text):
        """Stand in for a real console stdin: a text layer with universal
        newlines (what Windows gives you) over a .buffer holding the true
        bytes. Reading the text layer translates; reading .buffer does not.
        """
        import io

        raw = io.BytesIO(text.encode("utf-8"))
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline=None)
        monkeypatch.setattr(sys, "stdin", stream)

    def test_put_from_file_preserves_crlf(self, palace_path, tmp_dir, capsys):
        src = f"{tmp_dir}/in.patch"
        with open(src, "wb") as fh:
            fh.write(self.CRLF_PATCH.encode("utf-8"))

        cmd_artifact(_put_args(palace_path, None, file=src))
        artifact = json.loads(capsys.readouterr().out)

        assert artifact["size_bytes"] == len(self.CRLF_PATCH.encode("utf-8"))
        assert artifact["sha256"] == _sha256(self.CRLF_PATCH)

    def test_put_from_stdin_preserves_crlf(self, palace_path, monkeypatch, capsys):
        self._stdin(monkeypatch, self.CRLF_PATCH)
        cmd_artifact(_put_args(palace_path, None))
        artifact = json.loads(capsys.readouterr().out)
        assert artifact["sha256"] == _sha256(self.CRLF_PATCH)

    def test_put_from_stdin_dash_preserves_crlf(self, palace_path, monkeypatch, capsys):
        self._stdin(monkeypatch, self.CRLF_PATCH)
        cmd_artifact(_put_args(palace_path, None, file="-"))
        artifact = json.loads(capsys.readouterr().out)
        assert artifact["sha256"] == _sha256(self.CRLF_PATCH)

    def test_crlf_warning_actually_fires(self, palace_path, tmp_dir, capsys):
        """The CRLF warning is the reason this bug mattered — prove it fires."""
        src = f"{tmp_dir}/in.patch"
        with open(src, "wb") as fh:
            fh.write(self.CRLF_PATCH.encode("utf-8"))

        cmd_artifact(_put_args(palace_path, None, file=src, json=False))
        captured = capsys.readouterr()
        assert "carriage returns" in captured.err

    def test_get_out_is_byte_identical(self, palace_path, tmp_dir, capsys):
        src = f"{tmp_dir}/in.patch"
        with open(src, "wb") as fh:
            fh.write(self.CRLF_PATCH.encode("utf-8"))
        cmd_artifact(_put_args(palace_path, None, file=src))
        artifact = json.loads(capsys.readouterr().out)

        out_path = f"{tmp_dir}/out.patch"
        cmd_artifact(
            SimpleNamespace(
                palace=palace_path,
                artifact_action="get",
                artifact_id=artifact["id"],
                out=out_path,
                json=True,
            )
        )
        with open(out_path, "rb") as fh:
            written = fh.read()
        assert written == self.CRLF_PATCH.encode("utf-8")

    def test_append_body_file_preserves_crlf(self, palace_path, tmp_dir, capsys):
        src = f"{tmp_dir}/body.txt"
        with open(src, "wb") as fh:
            fh.write("line one\r\nline two\r\n".encode("utf-8"))

        cmd_logstream(_append_args(palace_path, body=None, body_file=src))
        event = json.loads(capsys.readouterr().out)
        assert event["body"] == "line one\r\nline two\r\n"


def _sha256(text):
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── logstream watch ───────────────────────────────────────────────────────


def _watch_args(palace, **overrides):
    fields = dict(
        palace=palace,
        logstream_action="watch",
        agent=None,
        stream=None,
        room=None,
        type=None,
        status=None,
        to_agent=None,
        from_agent=None,
        exclude_from_agent=None,
        correlation_id=None,
        since_event_id=None,
        state_file=None,
        # These cases seed events and then watch for them, so they opt into
        # the replay. The tip default is exercised explicitly by the
        # first-run tests below.
        from_start=True,
        follow=False,
        idle_exit_ms=400,
        poll_timeout_ms=60,
        limit=50,
        json=True,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _watch_payload(capsys):
    out = capsys.readouterr().out.strip()
    assert out, "watch printed nothing"
    return json.loads(out)


class TestLogstreamWatch:
    def test_wakes_on_a_matching_event_and_exits_zero(self, palace_path, capsys):
        cmd_logstream(_append_args(palace_path, to_agent="mac-claude", from_agent="windows-grok"))
        capsys.readouterr()

        # No SystemExit: a clean return is exit 0, the "you have mail" signal
        # a harness backgrounds this process to receive.
        cmd_logstream(_watch_args(palace_path, agent="mac-claude"))
        payload = _watch_payload(capsys)
        assert payload["count"] == 1
        assert payload["timed_out"] is False
        assert payload["cursor"]

    def test_idle_timeout_exits_two(self, palace_path, capsys):
        with pytest.raises(SystemExit) as exc:
            cmd_logstream(_watch_args(palace_path, agent="nobody-home"))
        assert exc.value.code == 2
        assert _watch_payload(capsys)["timed_out"] is True

    def test_agent_shorthand_does_not_wake_on_your_own_broadcast(self, palace_path, capsys):
        """--agent exists for this case.

        to_agent=<me> also matches '*' broadcasts, and your own broadcasts are
        broadcasts, so a watcher without the exclusion wakes itself every time
        it posts a status.
        """
        cmd_logstream(
            _append_args(
                palace_path,
                from_agent="mac-claude",
                to_agent="*",
                type="status.update",
            )
        )
        capsys.readouterr()

        with pytest.raises(SystemExit) as exc:
            cmd_logstream(_watch_args(palace_path, agent="mac-claude"))
        assert exc.value.code == 2, "watcher woke itself on its own broadcast"

    def test_explicit_to_agent_still_sees_broadcasts(self, palace_path, capsys):
        """Without --agent there is no exclusion, so '*' still reaches you."""
        cmd_logstream(_append_args(palace_path, from_agent="mac-claude", to_agent="*"))
        capsys.readouterr()

        cmd_logstream(_watch_args(palace_path, to_agent=["mac-claude"]))
        assert _watch_payload(capsys)["count"] == 1

    def test_type_filter_is_an_or_and_ignores_the_rest(self, palace_path, capsys):
        for event_type in ("status.update", "status.update", "patch.ready"):
            cmd_logstream(
                _append_args(
                    palace_path,
                    type=event_type,
                    from_agent="windows-grok",
                    to_agent="mac-claude",
                )
            )
        capsys.readouterr()

        cmd_logstream(
            _watch_args(
                palace_path,
                agent="mac-claude",
                type=["task.request", "patch.ready"],
            )
        )
        payload = _watch_payload(capsys)
        assert [e["type"] for e in payload["events"]] == ["patch.ready"]

    def test_state_file_persists_the_cursor_and_prevents_replay(
        self, palace_path, tmp_path, capsys
    ):
        state = str(tmp_path / "watch.json")
        cmd_logstream(_append_args(palace_path, to_agent="mac-claude", from_agent="windows-grok"))
        capsys.readouterr()

        cmd_logstream(_watch_args(palace_path, agent="mac-claude", state_file=state))
        first = _watch_payload(capsys)
        assert first["count"] == 1
        assert json.load(open(state, encoding="utf-8"))["cursor"] == first["cursor"]

        # Second run resumes from the file: the same event must not replay.
        with pytest.raises(SystemExit) as exc:
            cmd_logstream(_watch_args(palace_path, agent="mac-claude", state_file=state))
        assert exc.value.code == 2

    def test_since_event_id_overrides_the_state_file(self, palace_path, tmp_path, capsys):
        state = str(tmp_path / "watch.json")
        cmd_logstream(_append_args(palace_path, to_agent="mac-claude", from_agent="windows-grok"))
        first_id = json.loads(capsys.readouterr().out)["id"]

        cmd_logstream(_watch_args(palace_path, agent="mac-claude", state_file=state))
        capsys.readouterr()

        cmd_logstream(_append_args(palace_path, to_agent="mac-claude", from_agent="windows-grok"))
        capsys.readouterr()
        # Rewind explicitly: the flag wins over the stored cursor.
        cmd_logstream(
            _watch_args(
                palace_path,
                agent="mac-claude",
                state_file=state,
                since_event_id=first_id,
            )
        )
        assert _watch_payload(capsys)["count"] == 1

    def test_idle_deadline_caps_the_poll(self, palace_path, capsys):
        """--idle-exit-ms shorter than --poll-timeout-ms must not wait the poll."""
        t0 = time.monotonic()
        with pytest.raises(SystemExit) as exc:
            cmd_logstream(
                _watch_args(
                    palace_path,
                    agent="nobody-home",
                    idle_exit_ms=200,
                    poll_timeout_ms=5000,
                )
            )
        elapsed = time.monotonic() - t0
        assert exc.value.code == 2
        assert elapsed < 1.5, f"idle 200ms waited {elapsed:.2f}s (poll was 5s)"

    def test_match_does_not_checkpoint_if_output_fails(
        self, palace_path, tmp_path, capsys, monkeypatch
    ):
        """A broken pipe after a match must not advance the cursor past it.

        Asserting "no state file" would be pinning the symptom: since the
        starting position is now checkpointed before the first poll, a file
        legitimately exists. What must hold is that its cursor is still the
        pre-match position, so the undelivered event replays on restart —
        a duplicate, never a skip.
        """
        state = tmp_path / "watch.json"
        cmd_logstream(_append_args(palace_path, to_agent="mac-claude", from_agent="windows-grok"))
        matched_id = json.loads(capsys.readouterr().out)["id"]

        def boom(*_a, **_k):
            raise OSError("broken pipe")

        monkeypatch.setattr("json.dumps", boom)
        with pytest.raises(OSError, match="broken pipe"):
            cmd_logstream(_watch_args(palace_path, agent="mac-claude", state_file=str(state)))

        monkeypatch.undo()
        stored = json.loads(state.read_text(encoding="utf-8"))["cursor"]
        assert stored != matched_id, "cursor advanced past an event that was never delivered"
        assert stored is None, "cursor should still be the pre-match starting position"

    def test_fresh_watch_starts_at_the_tip_not_the_beginning(self, palace_path, capsys):
        """A first watch must not replay the whole log.

        Measured on a real shared brain, a cursorless watch woke holding 41
        events, the oldest 49 days old — and nothing in the payload tells the
        agent they are stale, so week-old task.requests read as new work.
        The SSE live-tail already starts at the tip; the watcher now matches.
        Backlog belongs to the inbox sweep.
        """
        cmd_logstream(_append_args(palace_path, to_agent="mac-claude", from_agent="windows-grok"))
        capsys.readouterr()

        with pytest.raises(SystemExit) as exc:
            cmd_logstream(_watch_args(palace_path, agent="mac-claude", from_start=False))
        assert exc.value.code == 2, "fresh watch replayed a pre-existing event"

    def test_from_start_opts_back_into_the_replay(self, palace_path, capsys):
        cmd_logstream(_append_args(palace_path, to_agent="mac-claude", from_agent="windows-grok"))
        capsys.readouterr()

        cmd_logstream(_watch_args(palace_path, agent="mac-claude", from_start=True))
        assert _watch_payload(capsys)["count"] == 1

    def test_tip_default_does_not_override_an_explicit_cursor(self, palace_path, capsys):
        """--since-event-id and a state file must still win over the tip."""
        cmd_logstream(_append_args(palace_path, to_agent="mac-claude", from_agent="windows-grok"))
        first_id = json.loads(capsys.readouterr().out)["id"]
        cmd_logstream(_append_args(palace_path, to_agent="mac-claude", from_agent="windows-grok"))
        capsys.readouterr()

        cmd_logstream(
            _watch_args(palace_path, agent="mac-claude", since_event_id=first_id, from_start=False)
        )
        assert _watch_payload(capsys)["count"] == 1

    def test_interrupted_watch_does_not_report_mail(self, palace_path, monkeypatch, capsys):
        """Ctrl-C must not exit 0.

        Exit 0 is the documented "a match was printed" signal, so a
        supervisor that SIGINTs a watcher would otherwise be told it has
        mail that never arrived.
        """
        import mempalace.logstream as logstream_module

        def interrupt(*_a, **_k):
            raise KeyboardInterrupt

        monkeypatch.setattr(logstream_module.Logstream, "watch_events", interrupt)
        with pytest.raises(SystemExit) as exc:
            cmd_logstream(_watch_args(palace_path, agent="mac-claude"))
        assert exc.value.code == 130

    def test_nonpositive_poll_timeout_is_rejected(self, palace_path, capsys):
        """A configured zero must be rejected up front, not merely survived.

        With no idle deadline it would spin watch_events' expired-deadline
        branch forever without ever polling. Asserting only "nonzero exit"
        would pass for the wrong reason, since an idle timeout also exits
        nonzero — so this pins the validation error itself.
        """
        with pytest.raises(SystemExit) as exc:
            cmd_logstream(
                _watch_args(palace_path, agent="mac-claude", poll_timeout_ms=0, idle_exit_ms=200)
            )
        assert exc.value.code == 1
        assert "poll-timeout-ms" in json.loads(capsys.readouterr().out)["error"]

    def test_unreadable_state_file_replays_instead_of_skipping(self, palace_path, tmp_path, capsys):
        """A failed cursor read must not be treated as a first run.

        read_watch_cursor returns None for both "no state file yet" and
        "state file exists but is corrupt". Jumping to the tip on the second
        silently skips everything since the last good checkpoint, and the
        next checkpoint makes that loss permanent — the opposite of the
        documented "a corrupt state file costs a replay".
        """
        state = tmp_path / "watch.json"
        state.write_text("null", encoding="utf-8")  # valid JSON, not an object
        cmd_logstream(_append_args(palace_path, to_agent="mac-claude", from_agent="windows-grok"))
        capsys.readouterr()

        cmd_logstream(
            _watch_args(palace_path, agent="mac-claude", state_file=str(state), from_start=False)
        )
        payload = _watch_payload(capsys)
        assert payload["count"] == 1, "corrupt state file skipped the backlog instead of replaying"

    def test_follow_json_is_ndjson_not_concatenated_documents(self, palace_path, tmp_path, capsys):
        """--follow --json must be parseable.

        Repeated indented documents on one stream are not valid JSON; jq and
        json.load reject them with trailing data, which defeats the point of
        a machine-readable flag on the mode intended for daemons.
        """
        for _ in range(2):
            cmd_logstream(
                _append_args(palace_path, to_agent="mac-claude", from_agent="windows-grok")
            )
        capsys.readouterr()

        with pytest.raises(SystemExit):
            cmd_logstream(
                _watch_args(palace_path, agent="mac-claude", follow=True, idle_exit_ms=300)
            )
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert lines, "follow mode printed nothing"
        for line in lines:
            json.loads(line)  # every line stands alone — that is the contract

    def test_empty_log_first_run_persists_a_starting_position(self, palace_path, tmp_path, capsys):
        """A stateful watch against an empty log must leave a file behind.

        latest_event_id() is None on an empty log, so without persisting that
        the next launch looks like a first run, jumps to the tip, and skips
        the event that arrived while the watcher was stopped — permanently,
        because it then checkpoints past it.
        """
        state = tmp_path / "watch.json"
        with pytest.raises(SystemExit) as exc:
            cmd_logstream(
                _watch_args(
                    palace_path, agent="mac-claude", state_file=str(state), from_start=False
                )
            )
        assert exc.value.code == 2
        assert state.exists(), "empty-log watch left no state file"
        assert json.loads(state.read_text(encoding="utf-8"))["cursor"] is None

    def test_event_arriving_while_stopped_is_not_skipped(self, palace_path, tmp_path, capsys):
        """The full sequence Codex described, end to end."""
        state = tmp_path / "watch.json"
        # 1. First watch against an empty log; idles out.
        with pytest.raises(SystemExit):
            cmd_logstream(
                _watch_args(
                    palace_path, agent="mac-claude", state_file=str(state), from_start=False
                )
            )
        capsys.readouterr()

        # 2. An event arrives while nothing is watching.
        cmd_logstream(_append_args(palace_path, to_agent="mac-claude", from_agent="windows-grok"))
        capsys.readouterr()

        # 3. Relaunch must deliver it, not skip past it.
        cmd_logstream(
            _watch_args(palace_path, agent="mac-claude", state_file=str(state), from_start=False)
        )
        assert _watch_payload(capsys)["count"] == 1, "event that arrived while stopped was skipped"

    def test_refuses_to_start_when_the_initial_checkpoint_cannot_be_written(
        self, palace_path, tmp_path, monkeypatch, capsys
    ):
        """Continuing after a failed first checkpoint guarantees a later skip."""
        import mempalace.logstream as logstream_module

        def denied(*_a, **kw):
            if kw.get("required"):
                raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(logstream_module, "write_watch_cursor", denied)
        with pytest.raises(SystemExit) as exc:
            cmd_logstream(
                _watch_args(
                    palace_path,
                    agent="mac-claude",
                    state_file=str(tmp_path / "nope" / "watch.json"),
                    from_start=False,
                )
            )
        assert exc.value.code == 1
        assert "initial checkpoint" in json.loads(capsys.readouterr().out)["error"]

    def test_starting_cursor_is_checkpointed_before_the_first_poll(
        self, palace_path, tmp_path, monkeypatch, capsys
    ):
        """Every entry path must checkpoint before polling, not after.

        The starting position arrives three ways — an explicit
        --since-event-id, the tip, or None (empty log / --from-start) — and
        all three need the file on disk *before* the first poll. Deferring to
        the first watch_events yield leaves a window of up to a full poll
        timeout; an interrupt inside it leaves no file, so the next launch
        calls itself a first run and jumps to the tip.

        watch_events is stubbed to interrupt immediately, which is what makes
        this pin the startup write. Letting the real loop run would pass on
        the loop's own checkpoint instead, since the test poll timeout is
        milliseconds rather than the five-minute default.
        """
        import mempalace.logstream as logstream_module

        cmd_logstream(_append_args(palace_path, to_agent="mac-claude", from_agent="windows-grok"))
        first_id = json.loads(capsys.readouterr().out)["id"]

        def interrupt_before_yielding(*_a, **_k):
            raise KeyboardInterrupt

        monkeypatch.setattr(logstream_module.Logstream, "watch_events", interrupt_before_yielding)

        for label, overrides, expected in (
            ("explicit cursor", {"since_event_id": first_id}, first_id),
            ("tip", {}, first_id),
            ("from-start", {"from_start": True}, None),
        ):
            state = tmp_path / f"{label.replace(' ', '_')}.json"
            kwargs = {
                "agent": "mac-claude",
                "state_file": str(state),
                "from_start": False,
                **overrides,
            }
            with pytest.raises(SystemExit) as exc:
                cmd_logstream(_watch_args(palace_path, **kwargs))
            assert exc.value.code == 130
            assert state.exists(), f"{label}: interrupted before any checkpoint landed"
            assert json.loads(state.read_text(encoding="utf-8"))["cursor"] == expected, label

    def test_bad_cursor_fails_cleanly_without_poisoning_the_state_file(
        self, palace_path, tmp_path, capsys
    ):
        """A typo'd --since-event-id must not be persisted.

        The startup checkpoint happens before the first poll, and list_events
        raises on an unknown anchor — so without validation the bad id lands
        on disk, the poll crashes, and every later run without the flag
        reloads it and crashes again until someone deletes the file by hand.
        """
        state = tmp_path / "watch.json"
        with pytest.raises(SystemExit) as exc:
            cmd_logstream(
                _watch_args(
                    palace_path,
                    agent="mac-claude",
                    state_file=str(state),
                    since_event_id="evt_does_not_exist",
                    from_start=False,
                )
            )
        assert exc.value.code == 1
        assert "evt_does_not_exist" in json.loads(capsys.readouterr().out)["error"]
        assert not state.exists(), "an invalid cursor was persisted"

    def test_negative_idle_timeout_is_rejected_not_read_as_forever(self, palace_path, capsys):
        """Only 0 means "wait forever".

        A negative value took the same branch, silently disabling the idle
        deadline and leaving a harness waiting on a watcher it believed would
        time out.
        """
        with pytest.raises(SystemExit) as exc:
            cmd_logstream(_watch_args(palace_path, agent="mac-claude", idle_exit_ms=-1))
        assert exc.value.code == 1
        assert "idle-exit-ms" in json.loads(capsys.readouterr().out)["error"]

    def test_stored_cursor_that_vanished_replays_instead_of_refusing(
        self, palace_path, tmp_path, capsys
    ):
        """A stored cursor whose event is gone is corrupt state, not user error.

        Refusing to start would strand the watcher exactly as an unreadable
        file would. Only an explicitly supplied --since-event-id is treated
        as user error worth refusing.
        """
        state = tmp_path / "watch.json"
        state.write_text(json.dumps({"cursor": "evt_from_a_rebuilt_log"}), encoding="utf-8")
        cmd_logstream(_append_args(palace_path, to_agent="mac-claude", from_agent="windows-grok"))
        capsys.readouterr()

        cmd_logstream(
            _watch_args(palace_path, agent="mac-claude", state_file=str(state), from_start=False)
        )
        assert _watch_payload(capsys)["count"] == 1, "stranded instead of replaying"

    def test_repeated_filters_are_validated_like_single_ones(self, palace_path, capsys):
        """Validation must not depend on how many values you passed.

        A single-valued filter is pushed down to list_events and sanitized
        for free; a repeated one is not, so it was compared raw. That made
        `--type Task.Request` an error alone but silently accepted alongside
        a second value — and then it matched nothing, so the watcher waited
        forever for an event type that cannot exist.
        """
        with pytest.raises(SystemExit) as exc:
            cmd_logstream(
                _watch_args(
                    palace_path,
                    agent="mac-claude",
                    type=["Task.Request", "patch.ready"],
                )
            )
        assert exc.value.code == 1
        assert "Task.Request" in json.loads(capsys.readouterr().out)["error"]

    def test_repeated_status_and_routing_are_validated_too(self, palace_path, capsys):
        with pytest.raises(SystemExit) as exc:
            cmd_logstream(
                _watch_args(palace_path, agent="mac-claude", status=["open", "not_a_status"])
            )
        assert exc.value.code == 1
        assert "not_a_status" in json.loads(capsys.readouterr().out)["error"]

    def test_repeated_routing_values_are_normalized(self, palace_path, capsys):
        """Whitespace in a repeated value must not quietly stop it matching."""
        cmd_logstream(
            _append_args(
                palace_path,
                to_agent="mac-claude",
                from_agent="windows-grok",
                stream="project/mempalace",
            )
        )
        capsys.readouterr()
        cmd_logstream(
            _watch_args(
                palace_path,
                agent="mac-claude",
                stream=["  project/mempalace  ", "project/other"],
            )
        )
        assert _watch_payload(capsys)["count"] == 1

    def test_invalid_limit_is_a_cli_error_not_a_traceback(self, palace_path, capsys):
        """--json consumers must get an error document, never a stack trace."""
        with pytest.raises(SystemExit) as exc:
            cmd_logstream(_watch_args(palace_path, agent="mac-claude", limit=0))
        assert exc.value.code == 1
        assert "limit" in json.loads(capsys.readouterr().out)["error"]
