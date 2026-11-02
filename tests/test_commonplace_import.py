"""Synthetic imports through fresh signed-in users' real gateway HTTP routes."""

import http.client
import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace import plans, sessions
from commonplace.control_db import ControlDB
from commonplace.gateway import Gateway
from commonplace.handoff.thread_store import ThreadStorageLimit, ThreadStore
from commonplace.import_sources import SOURCES
from commonplace.policy import Policy
from commonplace.workers import WorkerSupervisor
from commonplace.workspace_store import thread_path, workspace_path

WORDS = "  café 🦉\r\nKeep  every word.\t"
AT = "2026-10-20T12:34:56Z"


class NoWorker:
    def start(self, *_):
        raise AssertionError("Import must not start a model or memory worker")


@pytest.fixture(params=("v1", "compact"))
def surface(tmp_path, monkeypatch, request):
    monkeypatch.setenv("COMMONPLACE_RATE_LIMITS", "off")
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")
    data = tmp_path / "data"
    data.mkdir()
    db = ControlDB(str(data / "control.sqlite3"))
    supervisor = WorkerSupervisor(str(data), launcher=NoWorker())
    gateway = Gateway(db, supervisor)
    user = db.create_user("new-person@example.com")
    workspace = db.create_workspace("new-workspace", user, personal=True)
    plans.assign_signup_plan(db, user, workspace, via="test")
    directory = workspace_path(data, workspace)
    directory.mkdir(parents=True)
    if request.param == "compact":
        (directory / "workspace.json").write_text('{"store":"compact"}')
    cookie = sessions.start_session(db, user, workspace)
    server = gateway.build_server("127.0.0.1", 0)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    obj = SimpleNamespace(
        db=db,
        gateway=gateway,
        data=data,
        workspace=workspace,
        user=user,
        directory=directory,
        cookie=cookie,
        port=server.server_address[1],
    )
    try:
        yield obj
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        gateway.handoff.close()
        supervisor.stop_all()
        db.close()


def api(surface, method, path, body=None, *, cookie=True, token=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", surface.port, timeout=5)
    request_headers = {"Content-Type": "application/json"}
    if cookie:
        request_headers["Cookie"] = f"{sessions.COOKIE}={surface.cookie}"
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    request_headers.update(headers or {})
    try:
        connection.request(
            method, path, json.dumps(body) if body is not None else None, headers=request_headers
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def token(surface, **overrides):
    options = dict(
        profile="full",
        scopes=["memory:read", "memory:write", "threads:read"],
        read_wings=["threads"],
        write_wings=["threads"],
        trust="standard",
    )
    options.update(overrides)
    surface.token_count = getattr(surface, "token_count", 0) + 1
    _, value = surface.db.create_token(
        workspace_ref=surface.workspace,
        user_ref=surface.user,
        name=f"importer-{surface.token_count}",
        client="test",
        policy=Policy.build(**options),
        actor=surface.user,
    )
    return value


def fixture(source):
    if source == "claude-code":
        return json.dumps(
            {"type": "user", "timestamp": AT, "message": {"role": "user", "content": WORDS}}
        ) + "\r\n", "auto"
    if source in ("codex", "t3"):
        return json.dumps(
            {
                "type": "response_item",
                "timestamp": AT,
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": WORDS}],
                },
            }
        ) + "\r\n", "auto"
    if source == "chatgpt":
        return json.dumps(
            {
                "id": "native-id",
                "title": "Exact title",
                "mapping": {
                    "node": {
                        "message": {
                            "author": {"role": "user"},
                            "create_time": 1792499696,
                            "content": {"parts": [WORDS]},
                        }
                    },
                },
            }
        ), "auto"
    if source == "claude-ai":
        return json.dumps(
            {
                "uuid": "native-id",
                "created_at": AT,
                "chat_messages": [
                    {"sender": "human", "text": WORDS, "created_at": AT},
                ],
            }
        ), "auto"
    if source == "grok-bot":
        return json.dumps(
            {
                "source": "grok-bot:example-assistant",
                "memory_type": "shared_profile",
                "learned_date": AT,
                "text": WORDS,
            }
        ) + "\r\n", "auto"
    return " \n" + json.dumps(
        [{"role": "user", "content": WORDS, "timestamp": AT}]
    ) + "\r\n", "messages"


def item(source="cursor", identity="original"):
    transcript, fmt = fixture(source)
    return {"thread_id": identity, "transcript": transcript, "format": fmt}


def upload(surface, source="cursor", threads=None, **kwargs):
    return api(
        surface,
        "POST",
        "/api/import",
        {"source": source, "threads": threads if threads is not None else [item(source)]},
        **kwargs,
    )


@pytest.mark.parametrize("source", SOURCES)
def test_fresh_signed_in_import_verbatim_provenance_dedup_and_privacy(surface, source):
    # No pre-existing workspace data, admin authority or handoff setting.
    assert not surface.db.user(surface.user)["is_admin"]
    assert not surface.gateway.handoff.has_threads(surface.workspace)
    status, report = upload(surface, source)
    assert status == 200
    assert (
        report["source"],
        report["processed"],
        report["total"],
        report["imported"],
        report["duplicates"],
        report["errors"],
    ) == (source, 1, 1, 1, 0, 0)
    stored_id = report["results"][0]["stored_thread_id"]
    status, record = api(surface, "GET", f"/api/threads/{stored_id}")
    assert status == 200
    assert record["source_thread_id"] == f"import:{source}:original"
    assert record["created_by"] == f"user:{surface.user}"
    assert record["workspace_id"] == surface.workspace
    status, messages = api(surface, "GET", f"/api/threads/{stored_id}/messages")
    assert status == 200
    assert messages[0]["raw"] == fixture(source)[0]
    assert messages[1]["text"] == WORDS
    assert messages[1]["at"]  # source timestamp retained, not import time
    if source != "chatgpt":
        assert messages[1]["at"] == AT
    assert messages[1]["provenance"]["approval_status"] == "proposal"
    assert Path(thread_path(surface.directory)).stat().st_mode & 0o777 == 0o600
    before = thread_path(surface.directory).stat().st_size
    _, again = upload(surface, source)
    assert (again["imported"], again["duplicates"], again["errors"]) == (0, 1, 0)
    assert again["results"][0]["stored_thread_id"] == stored_id
    assert thread_path(surface.directory).stat().st_size == before

    other_user = surface.db.create_user("another-person@example.com")
    other_workspace = surface.db.create_workspace("other", other_user, personal=True)
    other_cookie = sessions.start_session(surface.db, other_user, other_workspace)
    assert (
        api(
            surface,
            "GET",
            f"/api/threads/{stored_id}",
            headers={"Cookie": f"{sessions.COOKIE}={other_cookie}"},
        )[0]
        == 404
    )


def test_scoped_bearer_import_and_read(surface):
    bearer = token(surface)
    _, report = upload(surface, cookie=False, token=bearer)
    assert report["imported"] == 1
    stored_id = report["results"][0]["stored_thread_id"]
    assert api(surface, "GET", f"/api/threads/{stored_id}", cookie=False, token=bearer)[0] == 200
    restricted = token(surface, scopes=["memory:write"], read_wings=["inbox"])
    assert (
        api(surface, "GET", f"/api/threads/{stored_id}", cookie=False, token=restricted)[0] == 404
    )


@pytest.mark.parametrize(
    "options",
    [
        {"scopes": ["threads:read"]},
        {"scopes": ["memory:write"]},
        {"scopes": ["memory:write", "threads:read"]},
        {"read_wings": ["inbox"]},
        {"write_wings": ["inbox"]},
        {"trust": "untrusted", "write_wing": "threads"},
    ],
)
def test_refuses_restricted_connections_before_writing(surface, options):
    assert upload(surface, cookie=False, token=token(surface, **options))[0] == 403
    assert not surface.gateway.handoff.has_threads(surface.workspace)


def test_no_auth_cross_origin_and_invalid_envelopes(surface):
    assert upload(surface, cookie=False)[0] == 401
    assert upload(surface, headers={"Origin": "https://another.example"})[0] == 403
    for body in (
        {"source": "unknown", "threads": [item()]},
        {"source": "cursor", "threads": []},
        {"source": "cursor", "threads": [item()] * 51},
        {"source": "cursor", "threads": [item()], "workspace_id": "other"},
    ):
        assert api(surface, "POST", "/api/import", body)[0] == 400
    assert not surface.gateway.handoff.has_threads(surface.workspace)


def test_session_thread_access_keeps_origin_isolation_and_revocation(surface):
    _, report = upload(surface)
    stored_id = report["results"][0]["stored_thread_id"]
    assert api(surface, "GET", f"/api/threads/{stored_id}")[0] == 200
    assert (
        api(
            surface,
            "POST",
            f"/api/threads/{stored_id}/recall",
            {"query": "every"},
            headers={"Origin": "https://another.example"},
        )[0]
        == 403
    )
    other = surface.db.create_user("isolated-person@example.com")
    workspace = surface.db.create_workspace("isolated", other, personal=True)
    cookie = sessions.start_session(surface.db, other, workspace)
    assert (
        api(
            surface,
            "GET",
            f"/api/threads/{stored_id}",
            headers={"Cookie": f"{sessions.COOKIE}={cookie}"},
        )[0]
        == 404
    )
    sessions.end_session(surface.db, surface.cookie)
    assert api(surface, "GET", f"/api/threads/{stored_id}")[0] == 401


def test_unexpected_thread_failure_keeps_progress_and_retry_safe(surface, monkeypatch, caplog):
    store_type = type(surface.gateway.handoff.store_for(surface.workspace))
    original = store_type.store
    failing = True

    def store(self, workspace, source_id, *args, **kwargs):
        if failing and source_id.endswith(":temporary"):
            raise RuntimeError("private transcript and storage details")
        return original(self, workspace, source_id, *args, **kwargs)

    monkeypatch.setattr(store_type, "store", store)
    threads = [item(identity=name) for name in ("before", "temporary", "after")]
    status, report = upload(surface, threads=threads)
    assert status == 200
    assert (report["processed"], report["imported"], report["errors"]) == (3, 2, 1)
    assert report["status"] == "completed_with_errors"
    assert [row["status"] for row in report["results"]] == ["imported", "error", "imported"]
    assert report["results"][1]["code"] == "import_unavailable"
    assert "private transcript" not in json.dumps(report) + caplog.text
    failing = False
    _, retried = upload(surface, threads=threads)
    assert (retried["imported"], retried["duplicates"], retried["errors"]) == (1, 2, 0)
    assert retried["results"][0]["stored_thread_id"] == report["results"][0]["stored_thread_id"]
    assert retried["results"][2]["stored_thread_id"] == report["results"][2]["stored_thread_id"]


def test_in_memory_budget_does_not_stat_current_directory(monkeypatch):
    def no_stat(*_args, **_kwargs):
        raise AssertionError("In-memory storage must not stat filesystem paths")

    store = ThreadStore(":memory:")
    try:
        monkeypatch.setattr(Path, "stat", no_stat)
        store._check_storage_budget(1024 * 1024)
    finally:
        store.close()


def test_partial_progress_conflict_and_retry(surface):
    first = item()
    status, report = upload(
        surface, threads=[first, {"thread_id": "broken", "transcript": "private garbage"}]
    )
    assert status == 200
    assert (report["processed"], report["imported"], report["errors"]) == (2, 1, 1)
    assert report["status"] == "completed_with_errors"
    assert report["results"][1]["code"] == "invalid_transcript"
    assert "private garbage" not in json.dumps(report)
    _, report = upload(
        surface,
        threads=[first, {**first, "transcript": first["transcript"].replace("every", "changed")}],
    )
    assert (report["duplicates"], report["errors"]) == (1, 1)
    assert report["results"][1]["code"] == "conflict"
    store = surface.gateway.handoff.store_for(surface.workspace)
    stored = store.by_source(surface.workspace, "import:cursor:original")
    assert store.messages(stored.id)[1].text == WORDS


def test_all_chatgpt_branches_and_claude_tool_content_retained(surface):
    conversation = json.loads(fixture("chatgpt")[0])
    alternate = {
        "author": {"role": "assistant"},
        "create_time": 1792499697,
        "content": {"parts": ["  first part\n", "\tsecond part  ", {"asset_pointer": "image"}]},
    }
    conversation["mapping"]["other-branch"] = {"message": alternate}
    _, report = upload(
        surface, "chatgpt", [{"thread_id": "branching", "transcript": json.dumps(conversation)}]
    )
    store = surface.gateway.handoff.store_for(surface.workspace)
    stored_id = report["results"][0]["stored_thread_id"]
    messages = store.messages(stored_id)
    assert [m.text for m in messages[1:4]] == [WORDS, "  first part\n", "\tsecond part  "]
    assert "asset_pointer" in messages[4].text
    native = {
        "chat_messages": [
            {
                "sender": "assistant",
                "created_at": AT,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "example",
                        "input": {"text": WORDS},
                    },
                    {"type": "tool_result", "tool_use_id": "call-1", "content": WORDS},
                ],
            }
        ]
    }
    _, report = upload(
        surface, "claude-ai", [{"thread_id": "tools", "transcript": json.dumps(native)}]
    )
    messages = store.messages(report["results"][0]["stored_thread_id"])
    assert [m.kind for m in messages] == ["other", "tool_call", "tool_result"]
    assert messages[2].text == WORDS


def test_import_over_default_request_cap_and_explicit_unsupported_format(surface):
    # The ordinary route's 1 MiB cap must not truncate valid transcript imports.
    large = item()
    large["transcript"] = json.dumps([{"role": "user", "content": "x" * (1024 * 1024 + 1)}])
    assert upload(surface, threads=[large])[1]["imported"] == 1
    _, report = upload(surface, threads=[{**item(identity="unsupported"), "format": "auto"}])
    assert report["results"][0]["code"] == "invalid_transcript"


def test_source_namespace_is_part_of_dedup_identity(surface):
    assert upload(surface, "cursor")[1]["imported"] == 1
    assert upload(surface, "grok-bot")[1]["imported"] == 1


def test_grok_facts_preserve_all_types_sources_dates_and_exact_lines(surface):
    facts = [
        {
            "source": f"grok-bot:assistant-{index % 2}",
            "memory_type": memory_type,
            "learned_date": "2026-10-20",
            "text": WORDS + "\u2028",
        }
        for index, memory_type in enumerate(
            ("profile", "journal", "shared_profile", "shared_journal")
        )
    ]
    lines = [json.dumps(fact, ensure_ascii=False) + "\r\n" for fact in facts]
    body = {
        "thread_id": "facts-export",
        "transcript": "\n" + "".join(lines),
        "format": "grok-facts",
    }
    _, report = upload(surface, "grok-bot", [body])
    assert report["imported"] == 1 and report["facts"] == 4
    assert report["results"][0]["source_counts"] == {
        "grok-bot:assistant-0": 2,
        "grok-bot:assistant-1": 2,
    }
    stored_id = report["results"][0]["stored_thread_id"]
    status, messages = api(surface, "GET", f"/api/threads/{stored_id}/messages")
    assert status == 200
    assert messages[0]["raw"] == body["transcript"]
    assert [m["raw"] for m in messages[1:]] == lines
    assert [m["text"] for m in messages[1:]] == [f["text"] for f in facts]
    assert all(m["at"] == "2026-10-20" for m in messages[1:])
    assert all(m["provenance"]["approval_status"] == "proposal" for m in messages)
    assert upload(surface, "grok-bot", [body])[1]["duplicates"] == 1
    invalid = {**facts[0], "memory_type": "unsupported"}
    _, report = upload(
        surface, "grok-bot", [{"thread_id": "invalid", "transcript": json.dumps(invalid)}]
    )
    assert report["results"][0]["code"] == "invalid_transcript"
    # A malformed line makes the entire submitted snapshot atomic.
    _, report = upload(
        surface, "grok-bot", [{"thread_id": "partial", "transcript": lines[0] + "broken\n"}]
    )
    assert report["results"][0]["code"] == "invalid_transcript"
    assert (
        surface.gateway.handoff.store_for(surface.workspace).by_source(
            surface.workspace, "import:grok-bot:partial"
        )
        is None
    )


def test_free_quota_new_and_existing_stores_and_noop_at_limit(surface):
    # Sparse file: exercise the real 250 MiB meter without allocating the data.
    padding = surface.directory / "existing.bin"
    with padding.open("wb") as stream:
        stream.truncate(plans.FREE.storage_bytes - 16 * 1024)
    _, report = upload(surface)
    assert report["results"][0]["code"] == "storage_limit"
    assert "250 MB" in report["results"][0]["error"]
    assert not surface.gateway.handoff.has_threads(surface.workspace)
    padding.unlink()
    _, report = upload(surface)
    stored_id = report["results"][0]["stored_thread_id"]
    used = plans.StorageMeter._walk(surface.directory)
    with padding.open("wb") as stream:
        stream.truncate(plans.FREE.storage_bytes - used)
    assert upload(surface)[1]["duplicates"] == 1
    _, report = upload(surface, threads=[item(identity="new")])
    assert report["results"][0]["code"] == "storage_limit"
    store = surface.gateway.handoff.store_for(surface.workspace)
    assert store.by_source(surface.workspace, "import:cursor:new") is None
    assert store.messages(stored_id)[1].text == WORDS
    assert report["storage_bytes"] <= plans.FREE.storage_bytes


def test_atomic_rollback_when_commit_budget_is_refused(surface, monkeypatch):
    store = surface.gateway.handoff.store_for(surface.workspace)
    check = store._check_storage_budget

    def refuse_commit(maximum, messages=()):
        if not messages:
            raise ThreadStorageLimit()
        return check(maximum, messages)

    monkeypatch.setattr(store, "_check_storage_budget", refuse_commit)
    assert upload(surface)[1]["results"][0]["code"] == "storage_limit"
    assert store.by_source(surface.workspace, "import:cursor:original") is None


@pytest.mark.parametrize("mode", ["paid", "self-host"])
def test_paid_and_unmetered_self_host_import(surface, monkeypatch, mode):
    if mode == "paid":
        plans.set_plan(surface.db, surface.user, "paid", actor=surface.user)
    else:
        monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    _, report = upload(surface)
    assert report["storage_limit_bytes"] is None
    assert report["imported"] == 1


def test_example_client_authenticates_and_reports_each_conversation(
    surface, tmp_path, monkeypatch, capsys
):
    path = Path(__file__).parents[1] / "examples" / "import_memory.py"
    spec = importlib.util.spec_from_file_location("import_memory_example", path)
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    monkeypatch.setenv("COMMONPLACE_TOKEN", token(surface))
    first = fixture("chatgpt")[0]
    second = first.replace("native-id", "another-id")
    export = tmp_path / "conversations.json"
    export.write_text("[\n" + first + ",\n" + second + "\n]")
    args = [str(export), "--source", "chatgpt", "--url", f"http://127.0.0.1:{surface.port}"]
    assert client.main(args) == 0
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(reports) == 2 and sum(r["imported"] for r in reports) == 2
    assert client.main(args) == 0
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert sum(r["duplicates"] for r in reports) == 2
    store = surface.gateway.handoff.store_for(surface.workspace)
    stored = store.by_source(surface.workspace, "import:chatgpt:native-id")
    assert store.messages(stored.id)[0].raw == first

    grok_export = tmp_path / "synthetic-facts.jsonl"
    raw_fact = fixture("grok-bot")[0]
    grok_export.write_text(raw_fact + raw_fact, newline="")
    args = [str(grok_export), "--source", "grok-bot", "--url", f"http://127.0.0.1:{surface.port}"]
    assert client.main(args) == 0
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(reports) == 2
    assert sum(r["imported"] for r in reports) == 1
    assert sum(r["duplicates"] for r in reports) == 1
    assert all(r["facts"] == 1 for r in reports)
