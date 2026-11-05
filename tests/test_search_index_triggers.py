"""Best-effort import, capture and startup index scheduling."""

from unittest.mock import Mock

from test_commonplace_capture import connection, enable, native, send
from test_commonplace_import import api, surface as surface

from commonplace import continuity_read
from commonplace.gateway import Gateway
from commonplace.handoff.service import HandoffService
from commonplace.handoff.wiring import HandoffContext


def test_u4_a_import_enrolls_stored_holder_and_fork(surface, monkeypatch):
    s = surface
    ctx = s.gateway.handoff
    schedule = Mock()
    monkeypatch.setattr(ctx.service, "index_later", schedule)
    monkeypatch.setattr(continuity_read, "content_threads", lambda *args: ["holder", "fork"])
    status, result = api(
        s,
        "POST",
        "/api/import",
        {
            "source": "claude-code",
            "threads": [
                {"thread_id": "synthetic", "format": "claude-code", "transcript": native()}
            ],
        },
    )
    assert status == 200
    stored_id = result["results"][0]["stored_thread_id"]
    schedule.assert_called_once_with(s.workspace, [stored_id, "holder", "fork"])
    schedule.side_effect = RuntimeError("unavailable")
    assert (
        api(
            s,
            "POST",
            "/api/import",
            {
                "source": "claude-code",
                "threads": [
                    {"thread_id": "synthetic", "format": "claude-code", "transcript": native()}
                ],
            },
        )[0]
        == 200
    )


def test_u4_b_capture_after_commit_and_only_success(surface, monkeypatch):
    s = surface
    assert enable(s)[0] == 200
    bearer = connection(s)
    calls = []

    def schedule(ws, ids):
        assert not s.db._conn.in_transaction
        assert ids
        calls.append((ws, ids))
        raise RuntimeError("unavailable")

    monkeypatch.setattr(s.gateway.handoff.service, "index_later", schedule)
    assert send(s, bearer)[1]["status"] == "appended"
    assert send(s, bearer)[1]["status"] == "duplicate"
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert send(s, bearer, prefix="0" * 64)[0] == 409
    assert api(s, "GET", "/api/capture?source=claude-code&thread_id=session-1")[0] == 200
    assert len(calls) == 2


def test_u4_c_startup_orders_workspaces_with_threads(surface, monkeypatch):
    s = surface
    second = s.db.create_workspace("synthetic-second", s.user, personal=False)
    empty = s.db.create_workspace("synthetic-empty", s.user, personal=False)
    calls = []
    monkeypatch.setattr(HandoffContext, "has_threads", lambda self, ws: ws != empty)
    monkeypatch.setattr(HandoffService, "index_later", lambda self, ws: calls.append(ws))
    gateway = Gateway(s.db, s.gateway.supervisor)
    try:
        assert calls == sorted([s.workspace, second])
    finally:
        gateway.handoff.close()
        gateway.webhooks.stop()
