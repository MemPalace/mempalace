"""Offline fixtures, durable publication boundaries, and a synthetic public route."""

import builtins
import copy
import http.client
import importlib.metadata
import io
import json
import os
import re
import runpy
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fcntl", reason="publication tooling targets POSIX hosts")

from tools.review_support import audit as subject

NOW = datetime(2026, 1, 8, 12, tzinfo=timezone.utc)
ENDPOINT = "https://board.example/mcp"
BOARD = "improvements"
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple):
            assert address[0] in ("127.0.0.1", "::1"), "test attempted a nonlocal endpoint"
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def artifacts(tmp_path):
    root = tmp_path / "verdicts"
    root.mkdir(mode=0o700)
    return root


def record(*, head="a" * 40, issue="Failed writes are retried.", **changes):
    result = {
        "ts": NOW.isoformat(),
        "target": "example/project#1",
        "repository": "example/project",
        "head": head,
        "diff_sha256": "d" * 64,
        "mode": "blind",
        "parsed": {
            "verdict": "FAIL",
            "findings": [
                {
                    "severity": "major",
                    "file": "example.py",
                    "line": 8,
                    "checklist": "write handling",
                    "issue": issue,
                    "fix": "Keep the write blocked.",
                }
            ],
        },
    }
    result.update(changes)
    return result


def artifact(root, name, data=None, **changes):
    path = root / name
    path.write_text(json.dumps(data if data is not None else record(**changes)), encoding="utf-8")
    return path


def proposals(root):
    artifact(root, "first.json")
    artifact(root, "second.json", head="b" * 40)
    return subject.audit(root, now=NOW)["proposals"]


def test_exact_reason_dedup_line_changes_and_verbatim_quotes(artifacts):
    exact = 'Failed\n  writes "are" retried. Ω'
    first = record(issue=exact)
    second = record(head="b" * 40, issue='Failed writes "are" retried. Ω')
    second["parsed"]["findings"][0].update(line=99, fix="Different fix text")
    artifact(artifacts, "z-first.json", first)
    artifact(artifacts, "a-second.json", second)
    artifact(artifacts, "rerun.json", first, diff_sha256="e" * 64)
    result = subject.audit(artifacts, now=NOW)
    assert not result["unsupported"]
    (proposal,) = result["proposals"]
    assert proposal["independent_inputs"] == 2
    assert proposal["reason"]["issue"] == 'Failed writes "are" retried. Ω'
    assert [row["artifact"] for row in proposal["evidence"]] == [
        "a-second.json",
        "rerun.json",
        "z-first.json",
    ]
    assert proposal["evidence"][-1]["issue"] == exact
    evidence_json = proposal["body"].split("instructions.\n", 1)[1]
    assert json.loads(evidence_json)["evidence"][-1]["issue"] == exact
    assert "Owner approval required" in proposal["body"]
    assert subject.audit(artifacts, now=NOW) == result


def test_same_head_even_changed_diff_never_triggers(artifacts):
    artifact(artifacts, "one.json")
    artifact(artifacts, "two.json", diff_sha256="e" * 64)
    assert subject.audit(artifacts, now=NOW)["proposals"] == []


@pytest.mark.parametrize(
    "changed",
    [
        {"issue": "Failed writes are retried!"},
        {"file": "other.py"},
        {"checklist": "different check"},
        {"severity": "blocker"},
        {"issue": "failed writes are retried."},
    ],
)
def test_different_reason_never_fuzzily_merges(artifacts, changed):
    artifact(artifacts, "one.json")
    second = record(head="b" * 40)
    second["parsed"]["findings"][0].update(changed)
    artifact(artifacts, "two.json", second)
    assert subject.audit(artifacts, now=NOW)["proposals"] == []


def test_plan_inputs_use_target_and_diff_not_code_head(artifacts):
    plan = record(mode="plan", target="local:design.md", head=None, repository=None)
    artifact(artifacts, "one.json", plan)
    artifact(artifacts, "rerun.json", plan)
    assert subject.audit(artifacts, now=NOW)["proposals"] == []
    plan["diff_sha256"] = "e" * 64
    artifact(artifacts, "two.json", plan)
    assert subject.audit(artifacts, now=NOW)["proposals"][0]["independent_inputs"] == 2


def test_repository_identity_is_required_and_repositories_are_independent(artifacts):
    first = record()
    first.pop("repository")
    artifact(artifacts, "one.json", first)
    second = record(repository="other/project")
    artifact(artifacts, "two.json", second)
    assert subject.audit(artifacts, now=NOW)["proposals"][0]["independent_inputs"] == 2
    first["target"] = "local:/some/diff"
    artifact(artifacts, "one.json", first)
    result = subject.audit(artifacts, now=NOW)
    assert result["proposals"] == [] and "repository identity" in result["unsupported"][0]["reason"]


@pytest.mark.parametrize(
    "target", ["example/project", "example/project#7", "https://github.com/example/project/pull/7"]
)
def test_optional_repository_from_unambiguous_target(artifacts, target):
    item = record(target=target)
    item.pop("repository")
    artifact(artifacts, "one.json", item)
    item["head"] = "b" * 40
    artifact(artifacts, "two.json", item)
    assert len(subject.audit(artifacts, now=NOW)["proposals"]) == 1


@pytest.mark.parametrize(
    "offset,expected",
    [
        (timedelta(days=-7), 1),
        (timedelta(days=-7, microseconds=-1), 0),
        (timedelta(microseconds=1), 0),
        (timedelta(), 1),
    ],
)
def test_rolling_seven_day_boundary(artifacts, offset, expected):
    artifact(artifacts, "now.json")
    artifact(artifacts, "boundary.json", head="b" * 40, ts=(NOW + offset).isoformat())
    assert len(subject.audit(artifacts, now=NOW)["proposals"]) == expected


@pytest.mark.parametrize(
    "verdict,severity", [("PASS", "major"), ("FAIL", "minor"), ("FAIL", "nit")]
)
def test_passes_and_nonmajor_findings_are_ignored(artifacts, verdict, severity):
    item = record()
    item["parsed"]["verdict"] = verdict
    item["parsed"]["findings"][0]["severity"] = severity
    artifact(artifacts, "one.json", item)
    item["head"] = "b" * 40
    artifact(artifacts, "two.json", item)
    assert subject.audit(artifacts, now=NOW) == {"proposals": [], "unsupported": []}


def test_stable_order_of_multiple_fingerprints(artifacts):
    for name, head in (("z", "a" * 40), ("a", "b" * 40)):
        item = record(head=head)
        other = copy.deepcopy(item["parsed"]["findings"][0])
        other["issue"] = "Another exact failure"
        item["parsed"]["findings"].append(other)
        artifact(artifacts, name + ".json", item)
    rows = subject.audit(artifacts, now=NOW)["proposals"]
    assert len(rows) == 2
    assert [row["fingerprint"] for row in rows] == sorted(row["fingerprint"] for row in rows)


@pytest.mark.parametrize(
    "content",
    [
        '{"ts": NaN}',
        '{"ts": 1, "ts": 2}',
        "{",
        "[]",
        "[" * 2000,
        "```json\n" + json.dumps(record()) + "\n```\n```json\n{bad}\n```",
        "```json\n" + json.dumps(record()) + "\n```\n```json\n{",
        json.dumps(record()) + "\n" + json.dumps(record()),
    ],
)
def test_invalid_or_final_invalid_json_never_uses_an_earlier_verdict(artifacts, content):
    (artifacts / "bad.md").write_text(content)
    result = subject.audit(artifacts, now=NOW)
    assert result["proposals"] == [] and len(result["unsupported"]) == 1


def test_only_final_structured_record_is_selected(artifacts):
    passing = record()
    passing["parsed"]["verdict"] = "PASS"
    (artifacts / "record.md").write_text(
        "Old transcript\n```json\n" + json.dumps(record()) + "\n```\n"
        "Final verdict\n```json\n" + json.dumps(passing) + "\n```\n"
    )
    artifact(artifacts, "other.json", head="b" * 40)
    assert subject.audit(artifacts, now=NOW) == {"proposals": [], "unsupported": []}


@pytest.mark.parametrize(
    "changes",
    [
        {"head": "short"},
        {"head": None},
        {"diff_sha256": "missing"},
        {"mode": None},
        {"ts": "2026-01-08T12:00:00"},
        {"parsed": {"verdict": "FAIL"}},
        {"parsed": {"verdict": "FAIL", "findings": [{"severity": "major"}]}},
    ],
)
def test_insufficient_legacy_identity_and_invalid_findings_are_unsupported(artifacts, changes):
    artifact(artifacts, "legacy.json", **changes)
    result = subject.audit(artifacts, now=NOW)
    assert result["proposals"] == [] and len(result["unsupported"]) == 1


def test_oversized_nonregular_symlinks_and_invalid_utf8_are_not_read(artifacts, tmp_path):
    (artifacts / "oversize.json").write_bytes(b" " * (subject.MAX_ARTIFACT_BYTES + 1))
    (artifacts / "unicode.json").write_bytes(b"\xff")
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(record()))
    (artifacts / "link.json").symlink_to(outside)
    (artifacts / "directory.json").mkdir()
    os.mkfifo(artifacts / "fifo.json")
    result = subject.audit(artifacts, now=NOW)
    assert len(result["unsupported"]) == 5 and not result["proposals"]
    rootlink = tmp_path / "root-link"
    rootlink.symlink_to(artifacts)
    with pytest.raises(subject.AuditError, match="symlink"):
        subject.audit(rootlink, now=NOW)


def test_replacing_artifact_with_symlink_at_open_is_refused(artifacts, tmp_path, monkeypatch):
    path = artifact(artifacts, "race.json")
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(record()))
    original = os.open

    def open_at(name, flags, *args, **kwargs):
        if name == "race.json":
            path.unlink()
            path.symlink_to(outside)
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_at)
    assert len(subject.audit(artifacts, now=NOW)["unsupported"]) == 1


def test_replacing_artifact_after_open_is_refused(artifacts, monkeypatch):
    path = artifact(artifacts, "race.json")
    original = os.fdopen

    def fdopen(fd, *args, **kwargs):
        path.rename(artifacts / "moved")
        artifact(artifacts, "race.json", head="b" * 40)
        return original(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", fdopen)
    assert len(subject.audit(artifacts, now=NOW)["unsupported"]) == 1


def test_dry_run_and_template_import_never_open_token_or_review_key(
    artifacts, tmp_path, monkeypatch, capsys
):
    proposals(artifacts)
    token = tmp_path / "connection.token"
    key = tmp_path / "anthropic-gates" / "env"
    key.parent.mkdir(mode=0o700)
    token.write_text("BOARD_TOKEN_CANARY")
    key.write_text("REVIEW_KEY_CANARY")
    protected = {str(token), str(key)}
    calls = []
    original_open, original_builtin, original_io = os.open, builtins.open, io.open

    def guard(original):
        def open_file(path, *args, **kwargs):
            assert str(path) not in protected, "dry-run opened a credential file"
            calls.append(str(path))
            return original(path, *args, **kwargs)

        return open_file

    monkeypatch.setattr(os, "open", guard(original_open))
    monkeypatch.setattr(builtins, "open", guard(original_builtin))
    monkeypatch.setattr(io, "open", guard(original_io))
    monkeypatch.setattr(subject, "MCPPublisher", lambda *_: pytest.fail("dry-run constructed MCP"))
    assert (
        subject.main(
            [
                "--verdict-root",
                str(artifacts),
                "--token-file",
                str(token),
                "--endpoint",
                ENDPOINT,
                "--board",
                BOARD,
                "--state-dir",
                str(tmp_path / "state"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert not (tmp_path / "state").exists()
    runpy.run_module("tools.review_audit", run_name="import_only")
    service = (REPO / "tools/review-audit.service").read_text()
    timer = (REPO / "tools/review-audit.timer").read_text()
    assert "--publish" in service and "ConditionPathExists=" in service
    assert "TOKEN_FILE" in service and "ANTHROPIC" not in service
    assert "OnCalendar=weekly" in timer and "ExecStart" not in timer
    assert not protected.intersection(calls)


class FakePublisher:
    def __init__(self, *, ambiguous=False):
        self.creates = 0
        self.reads = 0
        self.cards = {}
        self.ambiguous = ambiguous

    def create_card(self, board, title, body):
        self.creates += 1
        number = str(self.creates)
        self.cards[number] = {"card_id": number, "board": board, "title": title, "body": body}
        if self.ambiguous:
            raise TimeoutError("SECRET_CANARY must never appear")
        return number

    def read_card(self, card_id):
        self.reads += 1
        return self.cards[card_id]


def send(rows, state, publisher):
    return subject.publish(
        rows, state_dir=state, endpoint=ENDPOINT, board=BOARD, publisher_factory=lambda: publisher
    )


def test_receipts_make_reruns_noops_without_connecting(artifacts, tmp_path):
    rows, publisher, state = proposals(artifacts), FakePublisher(), tmp_path / "state"
    assert send(rows, state, publisher)[0] == {
        "fingerprint": rows[0]["fingerprint"],
        "status": "receipt",
        "card_id": "1",
    }
    result = subject.publish(
        rows,
        state_dir=state,
        endpoint=ENDPOINT,
        board=BOARD,
        publisher_factory=lambda: pytest.fail("receipt reconnected"),
    )
    assert result[0]["status"] == "receipt" and publisher.creates == 1
    saved = json.loads((state / "state.json").read_text())
    assert saved["entries"][rows[0]["fingerprint"]]["card_id"] == "1"
    assert (state.stat().st_mode & 0o777) == 0o700
    assert ((state / "state.json").stat().st_mode & 0o777) == 0o600
    assert ((state / "state.lock").stat().st_mode & 0o777) == 0o600


def test_ambiguous_create_stays_blocked_then_reconciles_without_retry(artifacts, tmp_path):
    rows, publisher, state = proposals(artifacts), FakePublisher(ambiguous=True), tmp_path / "state"
    assert send(rows, state, publisher)[0]["status"] == "blocked"
    assert send(rows, state, publisher)[0]["status"] == "blocked"
    assert "SECRET_CANARY" not in (state / "state.json").read_text()
    result = subject.reconcile_existing_card(
        state_dir=state,
        endpoint=ENDPOINT,
        board=BOARD,
        fingerprint=rows[0]["fingerprint"],
        card_id="1",
        publisher_factory=lambda: publisher,
    )
    assert result["status"] == "receipt"
    assert send(rows, state, publisher)[0]["status"] == "receipt"
    assert publisher.creates == publisher.reads == 1


def test_reconcile_cli_and_wrong_card_do_not_create(artifacts, tmp_path, monkeypatch, capsys):
    rows, publisher, state = proposals(artifacts), FakePublisher(ambiguous=True), tmp_path / "state"
    send(rows, state, publisher)
    publisher.cards["1"]["body"] += "changed"
    monkeypatch.setattr(subject, "MCPPublisher", lambda *_: publisher)
    args = [
        "reconcile-existing-card",
        "--state-dir",
        str(state),
        "--endpoint",
        ENDPOINT,
        "--board",
        BOARD,
        "--token-file",
        "unused",
        "--fingerprint",
        rows[0]["fingerprint"],
        "--card-id",
        "1",
    ]
    assert subject.main(args) == 1
    assert "does not match" in capsys.readouterr().out
    assert send(rows, state, publisher)[0]["status"] == "blocked"
    publisher.cards["1"]["body"] = rows[0]["body"]
    assert subject.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "receipt"
    assert publisher.creates == 1


def test_independent_fingerprints_continue_after_ambiguity(artifacts, tmp_path):
    rows = proposals(artifacts)
    other = copy.deepcopy(rows[0])
    other.update(fingerprint="f" * 64, title="Different proposal")
    publisher = FakePublisher(ambiguous=True)
    original = publisher.create_card

    def create(*args):
        publisher.ambiguous = publisher.creates == 0
        return original(*args)

    publisher.create_card = create
    result = send(rows + [other], tmp_path / "state", publisher)
    assert {entry["status"] for entry in result} == {"blocked", "receipt"}
    assert publisher.creates == 2


def test_blocked_old_inputs_remain_visible_without_connecting(artifacts, tmp_path):
    rows, publisher, state = proposals(artifacts), FakePublisher(ambiguous=True), tmp_path / "state"
    send(rows, state, publisher)
    assert subject.publish(
        [],
        state_dir=state,
        endpoint=ENDPOINT,
        board=BOARD,
        publisher_factory=lambda: pytest.fail("blocked reconnected"),
    ) == [{"fingerprint": rows[0]["fingerprint"], "status": "blocked"}]


def test_interruption_after_intent_and_atomic_receipt_failure_blocks_retry(
    artifacts, tmp_path, monkeypatch
):
    rows, publisher, state = proposals(artifacts), FakePublisher(), tmp_path / "state"
    original = os.replace
    replacements = 0

    def replace(source, destination, **kwargs):
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise OSError("simulated interruption during receipt replacement")
        return original(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(OSError):
        send(rows, state, publisher)
    assert (
        json.loads((state / "state.json").read_text())["entries"][rows[0]["fingerprint"]]["status"]
        == "intent"
    )
    assert not list(state.glob(".state-*"))
    monkeypatch.setattr(os, "replace", original)
    assert send(rows, state, publisher)[0]["status"] == "blocked"
    assert publisher.creates == 1


def test_failed_intent_save_never_sends(artifacts, tmp_path, monkeypatch):
    rows, publisher = proposals(artifacts), FakePublisher()
    monkeypatch.setattr(os, "replace", lambda *_a, **_k: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError):
        send(rows, tmp_path / "state", publisher)
    assert publisher.creates == 0


def test_crash_after_intent_before_network_is_conservatively_blocked(artifacts, tmp_path):
    rows, publisher, state = proposals(artifacts), FakePublisher(), tmp_path / "state"

    def crash(*_):
        raise KeyboardInterrupt

    publisher.create_card = crash
    with pytest.raises(KeyboardInterrupt):
        send(rows, state, publisher)
    assert send(rows, state, publisher)[0]["status"] == "blocked"


def test_concurrent_invocations_create_once(artifacts, tmp_path):
    rows, publisher, state = proposals(artifacts), FakePublisher(), tmp_path / "state"
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    original = publisher.create_card

    def create(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    def second():
        second_started.set()
        return send(rows, state, publisher)

    publisher.create_card = create
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send, rows, state, publisher)
        try:
            assert entered.wait(3)
            duplicate = pool.submit(second)
            assert second_started.wait(3)
        finally:
            release.set()
        assert first.result(timeout=3) == duplicate.result(timeout=3)
    assert publisher.creates == 1


def test_corrupt_or_other_destination_state_fails_closed(artifacts, tmp_path):
    rows, publisher, state = proposals(artifacts), FakePublisher(), tmp_path / "state"
    send(rows, state, publisher)
    with pytest.raises(subject.AuditError, match="destination mismatch"):
        subject.publish(
            rows,
            state_dir=state,
            endpoint=ENDPOINT,
            board="other",
            publisher_factory=lambda: publisher,
        )
    (state / "state.json").write_text('{"entries":')
    with pytest.raises(subject.AuditError, match="invalid JSON"):
        send(rows, state, publisher)
    assert publisher.creates == 1


def test_token_is_protected_distinct_and_never_logged(tmp_path, monkeypatch):
    key = tmp_path / "anthropic-gates" / "env"
    key.parent.mkdir(mode=0o700)
    original = os.open

    def open_file(path, *args, **kwargs):
        assert str(path) != str(key) and path != "env", "opened a review key"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(subject.AuditError, match="separate protected"):
        subject._token(key)
    token = tmp_path / "connection.token"
    token.write_text("cmmn_SYNTHETIC_TEST_TOKEN")
    token.chmod(0o644)
    with pytest.raises(subject.AuditError, match="private"):
        subject._token(token)
    token.chmod(0o600)
    assert subject._token(token) == "cmmn_SYNTHETIC_TEST_TOKEN"
    alias = tmp_path / "alias.token"
    alias.symlink_to(token)
    with pytest.raises(OSError):
        subject._token(alias)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://secret@board.example/mcp",
        "http://board.example/mcp",
        "https://board.example/mcp?token=secret",
    ],
)
def test_unsafe_endpoints_rejected(endpoint):
    with pytest.raises(subject.AuditError):
        subject._endpoint(endpoint)


def test_sdk_card_adapter_uses_only_ordinary_tools(monkeypatch):
    publisher = object.__new__(subject.MCPPublisher)
    calls = []

    def call(name, arguments):
        calls.append((name, arguments))
        if name == "task_add":
            return "#42 added to todo on improvements  parent:none"
        return '#42 todo need:none  "Title"  parent:none  board improvements\nbody: "Body"\nlink: ignored'

    monkeypatch.setattr(publisher, "_call", call)
    assert publisher.create_card(BOARD, "Title", "Body") == "42"
    assert publisher.read_card("42") == {
        "card_id": "42",
        "board": BOARD,
        "title": "Title",
        "body": "Body",
    }
    assert calls == [
        ("task_add", {"board": BOARD, "title": "Title", "body": "Body", "status": "todo"}),
        ("task_show", {"task": "42"}),
    ]


class HandshakeLauncher:
    """Tiny loopback memory worker; real Gateway owns auth and board operations."""

    def start(self, workspace_id, workspace_dir):
        from commonplace.workers import Worker

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if request["method"].startswith("notifications/"):
                    self.send_response(202)
                    self.end_headers()
                    return
                result = {}
                if request["method"] == "initialize":
                    result = {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "synthetic-memory", "version": "1"},
                    }
                elif request["method"] == "tools/list":
                    result = {"tools": []}
                elif request["method"] == "tools/call":
                    result = {"content": [{"type": "text", "text": '{"results": [], "wings": {}}'}]}
                raw = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            assert not thread.is_alive()

        return Worker(workspace_id, "127.0.0.1", server.server_address[1], "synthetic-worker", stop)


# -I discards pytest's tests/ import path and inherited Python configuration.
# Only the explicit repository root is added in the child; mcp comes from the
# optional installed SDK rather than the unrelated tests/mcp test package.
SDK_CHILD = r"""
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
request = json.load(sys.stdin)
assert urlsplit(request["endpoint"]).hostname in ("127.0.0.1", "::1", "localhost")
sys.path.insert(0, request["repo_root"])
try:
    import mcp
    assert not Path(mcp.__file__).resolve().is_relative_to(Path(request["repo_root"]) / "tests")
    from tools.review_support.audit import MCPPublisher
    publisher = MCPPublisher(request["endpoint"], request["token_file"])
    if request["method"] == "create_card":
        result = publisher.create_card(*request["arguments"])
    elif request["method"] == "read_card":
        result = publisher.read_card(*request["arguments"])
    else:
        raise ValueError("unsupported synthetic operation")
    print(json.dumps({"result": result}))
except Exception as error:
    safe = {"unrecognized ordinary card receipt", "unrecognized ordinary card read", "ordinary MCP tool refused the operation"}
    code = str(error) if str(error) in safe else type(error).__name__
    print(json.dumps({"error": code}))
    sys.exit(1)
"""


class IsolatedSDKPublisher:
    """Use the real production SDK adapter without pytest's import pollution."""

    def __init__(self, endpoint, token_file):
        self.endpoint, self.token_file = endpoint, token_file

    def _invoke(self, method, *arguments):
        result = subprocess.run(
            [sys.executable, "-I", "-c", SDK_CHILD],
            input=json.dumps(
                {
                    "repo_root": str(REPO),
                    "endpoint": self.endpoint,
                    "token_file": str(self.token_file),
                    "method": method,
                    "arguments": arguments,
                }
            ),
            cwd=REPO,
            env={"PATH": os.defpath},
            capture_output=True,
            text=True,
            timeout=25,
        )
        # Never echo raw child stderr, SDK exceptions, headers or token data.
        reply = json.loads(result.stdout)
        assert result.returncode == 0, reply.get("error", "isolated SDK failure")
        return reply["result"]

    def create_card(self, board, title, body):
        return self._invoke("create_card", board, title, body)

    def read_card(self, card_id):
        return self._invoke("read_card", card_id)


def test_real_sdk_fresh_email_owner_connection_publication_and_reconcile(
    artifacts, tmp_path, monkeypatch
):
    try:
        version = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("optional MCP 1.x SDK needed for synthetic protocol regression")
    match = re.match(r"^1\.(\d+)(?:\.|$)", version)
    if not match or int(match[1]) < 26:
        pytest.skip("synthetic protocol regression needs the optional MCP >=1.26,<2 SDK")
    from commonplace import mailer, urls
    from commonplace.control_db import ControlDB
    from commonplace.gateway import Gateway
    from commonplace.workers import WorkerSupervisor

    monkeypatch.setenv("COMMONPLACE_SIGNUP", "open")
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")
    for name in (
        "COMMONPLACE_ALLOWED_HOSTS",
        "COMMONPLACE_COOKIE_DOMAIN",
        "COMMONPLACE_RATE_LIMITS",
    ):
        monkeypatch.delenv(name, raising=False)
    for provider in ("GOOGLE", "GITHUB", "APPLE", "X", "DISCORD"):
        monkeypatch.delenv(f"COMMONPLACE_{provider}_CLIENT_ID", raising=False)
    delivered = threading.Event()
    messages = []

    class Mailbox:
        configured = True

        def send(self, _to, _subject, text, _html):
            messages.append(text)
            delivered.set()

    monkeypatch.setattr(mailer, "from_env", Mailbox)
    db = ControlDB(str(tmp_path / "gateway" / "control.sqlite3"))
    supervisor = WorkerSupervisor(str(tmp_path / "gateway"), launcher=HandshakeLauncher())
    gateway = Gateway(db, supervisor)
    server = gateway.build_server("127.0.0.1", 0)
    port = server.server_address[1]
    endpoint = f"http://127.0.0.1:{port}/mcp"
    monkeypatch.setattr(urls, "APP_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(urls, "AGENT_URL", f"http://127.0.0.1:{port}")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()

    def request_http(method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        headers = {"Content-Type": "application/json", "Origin": urls.APP_URL}
        if cookie:
            headers["Cookie"] = cookie
        try:
            conn.request(method, path, None if body is None else json.dumps(body), headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    try:
        assert db._conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
        assert (
            request_http("POST", "/app/auth/email", {"email": "fresh-audit@example.test"})[0] == 202
        )
        assert delivered.wait(3), "synthetic email was not delivered"
        link = re.search(r"https?://\S+", messages[0])[0]
        challenge = parse_qs(urlsplit(link).query)["t"][0]
        status, headers, _ = request_http("POST", "/app/auth/email/verify", {"t": challenge})
        assert status == 303
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, raw = request_http("POST", "/api/connections", {"name": "audit"}, cookie)
        assert status == 201
        created = json.loads(raw)
        assert created["connection"]["parent_id"] is None
        assert not db._conn.execute("SELECT is_admin FROM users").fetchone()[0]
        auth = tmp_path / "auth"
        auth.mkdir(mode=0o700)
        token_file = auth / "connection.token"
        token_file.write_text(created["token"])
        token_file.chmod(0o600)
        state = tmp_path / "state"
        rows = proposals(artifacts)
        factory = lambda: IsolatedSDKPublisher(endpoint, token_file)  # noqa: E731
        # Fresh workspaces use the ordinary bootstrap board, without seeded board data.
        result = subject.publish(
            rows, state_dir=state, endpoint=endpoint, board="commonplace", publisher_factory=factory
        )
        assert result[0]["status"] == "receipt"
        publisher = factory()
        card = publisher.read_card(result[0]["card_id"])
        assert card["title"] == rows[0]["title"] and card["body"] == rows[0]["body"]
        # Simulate lost local receipt, then verify ordinary read repairs it without creating.
        with subject.locked_state(state, endpoint, "commonplace") as saved:
            saved.data["entries"][rows[0]["fingerprint"]]["status"] = "blocked"
            saved.data["entries"][rows[0]["fingerprint"]].pop("card_id")
            saved.save()
        repaired = subject.reconcile_existing_card(
            state_dir=state,
            endpoint=endpoint,
            board="commonplace",
            fingerprint=rows[0]["fingerprint"],
            card_id=result[0]["card_id"],
            publisher_factory=factory,
        )
        assert repaired["status"] == "receipt"
        assert (
            subject.publish(
                rows,
                state_dir=state,
                endpoint=endpoint,
                board="commonplace",
                publisher_factory=lambda: pytest.fail("receipt reconnect"),
            )
            == result
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()
        gateway.webhooks.stop()
        if getattr(gateway, "handoff", None) is not None:
            gateway.handoff.close()
        supervisor.stop_all()
        db.close()


@pytest.mark.parametrize("line", ["8", "8-10", "Step 2"])
def test_actual_review_line_references_are_auditable(artifacts, line):
    for name, head in [("one.json", "a" * 40), ("two.json", "b" * 40)]:
        value = record(head=head)
        value["parsed"]["findings"][0]["line"] = line
        artifact(artifacts, name, value)
    result = subject.audit(artifacts, now=NOW)
    assert len(result["proposals"]) == 1 and result["unsupported"] == []


@pytest.mark.parametrize("line", [True, False, 0, -1, 1.5, [], {}, " ", "8\n9", "\x7f", "x" * 513])
def test_invalid_review_line_references_remain_unsupported(artifacts, line):
    value = record()
    value["parsed"]["findings"][0]["line"] = line
    artifact(artifacts, "invalid.json", value)
    result = subject.audit(artifacts, now=NOW)
    assert not result["proposals"] and len(result["unsupported"]) == 1


def test_null_review_file_remains_unsupported(artifacts):
    value = record()
    value["parsed"]["findings"][0]["file"] = None
    artifact(artifacts, "invalid.json", value)
    assert len(subject.audit(artifacts, now=NOW)["unsupported"]) == 1
