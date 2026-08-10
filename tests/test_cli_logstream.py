"""Tests for the RFC 003 logstream/artifact CLI commands.

Covers cmd_logstream (append/list/wait/ack) and cmd_artifact (put/get):
JSON and human output, exact-content stdout piping, timeout exit code,
and error exits. Uses SimpleNamespace args like the rest of test_cli.py.
"""

import json
import sys
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
