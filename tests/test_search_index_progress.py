"""Progress surfaces expose counts and require explicit capture enrolment."""

import json
from types import SimpleNamespace

import pytest

from commonplace import cli, search_index_api
from commonplace.control_db import ControlDB
from commonplace.handoff.models import ThreadMessage
from commonplace.workspace_store import open_threads, workspace_path


@pytest.mark.parametrize(
    "person,role,status",
    [(True, "owner", 200), (True, "member", 403), (False, "owner", 403), (False, "member", 403)],
)
def test_u6_a_owner_only_counts(person, role, status):
    calls = []
    routes = {}
    counts = dict(state="running", threads_indexed=2, threads_pending=3, units_written=7)

    def index_status(workspace, *, principal):
        assert principal.person and principal.role == "owner"
        calls.append(workspace)
        return dict(counts, text="hidden", thread_ids=["hidden"])

    gateway = SimpleNamespace(
        principal_for=lambda handler: SimpleNamespace(
            person=person, role=role, workspace_id="ws_one"
        ),
        handoff=SimpleNamespace(service=SimpleNamespace(index_status=index_status)),
        add_route=lambda method, path, handle: routes.update({(method, path): handle}),
    )
    replies = []
    search_index_api.register(gateway)
    routes["GET", "/api/search-index"](
        SimpleNamespace(_send_json=lambda *args: replies.append(args))
    )
    assert replies[0][0] == status
    assert calls == (["ws_one"] if status == 200 else [])
    if status == 200:
        assert replies[0][1] == counts
        assert replies[0][2] == {"Cache-Control": "no-store"}


@pytest.fixture
def local(tmp_path):
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    user = db.create_user("owner@example.test")
    workspace = db.create_workspace("sample", user)
    directory = workspace_path(tmp_path, workspace)
    directory.mkdir(parents=True, exist_ok=True)
    store = open_threads(directory)
    ids = {}
    for source in ("capture:one", "capture-fork:two", "import:three", "ordinary"):
        row = store.store(
            workspace,
            source,
            [
                ThreadMessage(
                    seq=0,
                    role="user",
                    kind="message",
                    text="synthetic words",
                    raw="synthetic words",
                )
            ],
            actor=user,
        )
        ids[source] = row.id
    yield tmp_path, workspace, store, ids
    store.close()
    db.close()


def test_u6_b_cli_status(local, capsys, monkeypatch):
    root, workspace, store, ids = local
    monkeypatch.setenv("COMMONPLACE_SEARCH_INDEX", "off")
    assert cli.main(["--data", str(root), "search-index", "status", "--workspace", "sample"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == dict(threads_indexed=0, threads_pending=1)


@pytest.mark.parametrize("captures", [False, True])
def test_u6_c_cli_run_capture_enrolment(local, monkeypatch, capsys, captures):
    from commonplace.handoff import service

    root, workspace, store, ids = local
    calls = []

    def queue(self, ws):
        calls.append(ws)
        enrolled = {
            source
            for source, stored_id in ids.items()
            if store.mirror_progress(stored_id) is not None
        }
        assert enrolled == ({"capture:one", "capture-fork:two"} if captures else set())

    monkeypatch.setattr(service.HandoffService, "index_later", queue)
    monkeypatch.setattr(
        service.HandoffService, "wait_for_mirrors", lambda self: calls.append("drained")
    )
    args = ["--data", str(root), "search-index", "run", "--workspace", workspace]
    assert cli.main(args + (["--captures"] if captures else [])) == 0
    assert calls == [workspace, "drained"]
    assert set(json.loads(capsys.readouterr().out)) == {"threads_indexed", "threads_pending"}
