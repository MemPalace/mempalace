"""Event numbering and cursor recovery preserve restricted visibility."""

# ruff: noqa: F811
import json
import subprocess
from pathlib import Path

import pytest

from commonplace import events, sessions, webhooks
from commonplace.boards.service import BoardService, open_store
from commonplace.extensions import ToolContext
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_person_private_scope import PRIVATE, world  # noqa: F401


def service(w):
    return BoardService(
        ToolContext(
            principal=sessions.person_principal(w.hub.db, w.hub.admin, w.hub.workspace),
            gateway=w.hub.gateway,
        )
    )


def emit(w, board, title):
    return events.emit(
        w.hub.db, w.hub.workspace, "card.updated", data={"board": board, "title": title}
    )


def poll(w, cursor=0, epoch=""):
    status, body = w.http(w.member, f"/api/events?after={cursor}&epoch={epoch}")
    assert status == 200
    return body


def test_member_webhook_http_numbering_has_no_hidden_gaps(world, monkeypatch):
    w = world
    webhooks.stop(w.hub.gateway)
    monkeypatch.setattr(webhooks, "validate_url", lambda url: None)
    svc = service(w)
    svc.board_add(slug="visible", wing="shared")
    svc.board_add(slug="hidden", wing="private_notes")
    hooks = []
    for cookie in (w.member, w.owner):
        status, hook = w.http(
            cookie, "/api/webhooks", {"url": "https://example.test/hook", "events": ["card.*"]}
        )
        assert status == 201
        hooks.append(hook["id"])
    first = emit(w, "visible", "first")
    emit(w, "hidden", PRIVATE)
    w.insert_private()
    last = emit(w, "visible", "last")
    dispatcher = webhooks.Dispatcher(w.hub.db, gateway=w.hub.gateway)
    dispatcher.reconcile()
    status, listing = w.http(w.member, f"/api/webhooks/{hooks[0]}/deliveries")
    assert status == 200
    assert len(listing["deliveries"]) == 2
    assert all("event_seq" not in row for row in listing["deliveries"]), (
        "restricted webhook exposes event numbers"
    )
    sent = []
    monkeypatch.setattr(
        webhooks, "_post", lambda url, body, *a, **kw: sent.append(json.loads(body)) or 200
    )
    with w.hub.db._lock:
        rows = w.hub.db._conn.execute(
            "SELECT * FROM webhook_deliveries WHERE webhook_id=? ORDER BY rowid", (hooks[0],)
        ).fetchall()
    for row in rows:
        dispatcher._deliver(row)
    assert len(sent) == 2 and all("seq" not in p for p in sent)
    assert len({p["id"] for p in sent}) == 2
    assert all(p["id"].startswith("evt_") and len(p["id"]) == 36 for p in sent)
    assert PRIVATE not in json.dumps(sent)
    owner = w.http(w.owner, f"/api/webhooks/{hooks[1]}/deliveries")[1]
    assert {first["seq"], last["seq"]}.issubset({d["event_seq"] for d in owner["deliveries"]})
    with w.hub.db._lock:
        w.hub.db._conn.execute("UPDATE meta SET value='0' WHERE key='webhook_dispatch_seq'")
    dispatcher.reconcile()
    assert len(w.http(w.member, f"/api/webhooks/{hooks[0]}/deliveries")[1]["deliveries"]) == 2


@pytest.mark.parametrize("change", ["wing", "archive"])
def test_readable_set_change_resets_without_replaying(world, change):
    w = world
    svc = service(w)
    svc.board_add(slug="visible", wing="shared")
    svc.board_add(slug="gate", wing="shared")
    emit(w, "visible", "first")
    emit(w, "gate", "gate first")
    initial = poll(w)
    epoch = initial.get("epoch", "")
    first = poll(w, 0, epoch)
    store = open_store(
        w.hub.gateway.workspace_dir(w.hub.workspace), db=w.hub.db, workspace_id=w.hub.workspace
    )
    for value in ("private_notes", "shared") if change == "wing" else (1, 0):
        with store.transaction() as conn:
            conn.execute(
                f"UPDATE boards SET {'wing' if change == 'wing' else 'archived'}=? WHERE slug='gate'",
                (value,),
            )
        reset = poll(w, first["seq"], epoch)
        assert reset.get("reset") is True, "readable-set change silently reuses shifted ordinals"
        assert reset["events"] == [] and reset["epoch"] != epoch
        epoch, first = reset["epoch"], reset
    emit(w, "visible", "next")
    page = poll(w, first["seq"], epoch)
    assert [e["data"]["title"] for e in page["events"]] == ["next"]


@pytest.mark.parametrize("invalid", ["missing", "mismatch", "above"])
def test_invalid_member_cursor_requests_explicit_reset(world, invalid):
    w = world
    service(w).board_add(slug="visible", wing="shared")
    emit(w, "visible", "new readable")
    first = poll(w)
    epoch = first.get("epoch", "")
    response = poll(
        w,
        first["seq"] + (100 if invalid == "above" else 0),
        "" if invalid == "missing" else "wrong" if invalid == "mismatch" else epoch,
    )
    assert response.get("reset") is True, (
        "stale or unversioned cursor silently skips readable state"
    )
    assert response["events"] == [] and response["epoch"]
    emit(w, "visible", "after reset")
    page = poll(w, response["seq"], response["epoch"])
    assert [e["data"]["title"] for e in page["events"]] == ["after reset"]


def test_private_save_link_error_includes_unreadable_notice(world):
    w = world
    service(w).board_add(slug="visible", wing="shared")
    card = service(w).task_add(title="shared card", board="visible")
    status, body = w.http(
        w.member, "/api/memory", {"text": "hospital private health note", "task": card["number"]}
    )
    assert status >= 400 and "not linked" in json.dumps(body)
    assert "Saved privately. You cannot read this note for now." in json.dumps(body), (
        "saved-but-not-linked note has no privacy notice"
    )


def test_poll_reset_refreshes_open_card_and_adopts_epoch():
    script = Path("commonplace/web/static/app.js").read_text()
    poll_source = script[
        script.index("async function poll() {") : script.index(
            'document.addEventListener("visibilitychange"', script.index("async function poll() {")
        )
    ]
    harness = (
        """
const state={me:{},seq:99,eventEpoch:"old",base:"#/card/1",route:"#/card/1",pollFails:0};
let polling=false,retryDelay=2000,called=[];
const api=async url=>{called.push(url);return {reset:true,events:[],seq:2,epoch:"new"}};
const refresh=async options=>{called.push(options);return true};
const root={querySelector:()=>null}; const queueNoteOffline=()=>{}; const schedule=()=>{};
"""
        + poll_source
        + """
(async()=>{await poll();if(!called.some(x=>x && x.background===false)||state.seq!==2||state.eventEpoch!=="new"||!called[0].includes("epoch=old"))process.exit(1)})();
"""
    )
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
    assert result.returncode == 0, (
        "reset did not refetch the open screen and adopt its epoch: " + result.stderr
    )


def test_member_event_pages_use_one_board_snapshot_and_do_not_skip(world, monkeypatch):
    from commonplace.boards import service as board_service

    w = world
    service(w).board_add(slug="visible", wing="shared")
    with w.hub.db._lock, w.hub.db._tx():
        for index in range(501):
            events.record(
                w.hub.db,
                w.hub.workspace,
                "card.updated",
                data={"board": "visible", "title": f"page {index}"},
            )
    original = board_service.open_store
    calls = []

    def open_once(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(board_service, "open_store", open_once)
    first = poll(w)
    assert len(calls) == 1, "member poll queries the board store once per event"
    cursor, titles = 0, []
    while cursor < first["seq"]:
        calls.clear()
        page = poll(w, cursor, first["epoch"])
        assert len(calls) == 1 and 0 < len(page["events"]) <= 200
        assert page["reset"] is False
        assert page["seq"] == cursor + len(page["events"])
        titles.extend(e["data"]["title"] for e in page["events"] if e["kind"] == "card.updated")
        cursor = page["seq"]
    assert titles == [f"page {index}" for index in range(501)]
