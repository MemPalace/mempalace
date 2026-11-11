"""A card's link to its transcript must never reveal a thread the viewer cannot read."""

import json
from urllib.parse import quote

import pytest

from commonplace.handoff.models import HandoffRequest, ThreadMeta
from tests.test_handoff_adversarial_security import SecurityHub

READABLE_TEXT = "card_log_readable_6f5e1c"
PRIVATE_TEXT = "card_log_private_9a3d7b"


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    hub = SecurityHub(tmp_path_factory.mktemp("card-log-links"))
    try:
        yield hub
    finally:
        hub.close()


def handoff(hub, ref, text, *, wing=None, unsaved=False):
    result = hub.ctx.service.run(
        hub.principal,
        HandoffRequest(
            ThreadMeta(f"log-{ref}", task_ref=ref),
            json.dumps([{"role": "user", "content": text}]),
            transcript_format="messages",
        ),
    )
    record = result.handoff
    store = hub.ctx.store_for(hub.workspace)
    if wing:
        store._conn.execute(
            "UPDATE threads SET wing = ? WHERE id = ?", (wing, record.stored_thread_id)
        )
    if unsaved:
        hub.ctx.handoff_db._conn.execute(
            "UPDATE handoffs SET stored_thread_id = '' WHERE id = ?", (record.id,)
        )
    return record


def page(hub, cookie, route):
    return hub.request("GET", route, token=None, headers={"Cookie": cookie})


def card_route(ref):
    return "/app/cards/" + quote(ref, safe="")


def listing(hub, cookie, ref):
    status, body, _ = page(hub, cookie, "/api/handoffs?task=" + quote(ref, safe=""))
    return status, json.loads(body)


def test_reader_reaches_the_transcript_from_the_card(hub):
    record = handoff(hub, "#501", READABLE_TEXT)
    for user in ("owner", "member"):
        _, cookie, _ = hub.login(user)
        status, rows = listing(hub, cookie, "#501")
        assert status == 200 and [row["id"] for row in rows] == [record.id]
        status, body, _ = page(hub, cookie, card_route("#501"))
        assert status == 200 and f"/app/threads/{record.stored_thread_id}" in body
        status, body, _ = page(hub, cookie, f"/app/threads/{record.stored_thread_id}")
        assert status == 200 and READABLE_TEXT in body


def test_non_reader_sees_neither_link_target_nor_thread(hub):
    record = handoff(hub, "#502", PRIVATE_TEXT, wing="private_hidden")
    absent_card = "#599"
    # The owner's session reads every wing; a member's does not read this one.
    _, owner, _ = hub.login("owner")
    assert listing(hub, owner, "#502")[1][0]["id"] == record.id
    assert page(hub, owner, card_route("#502"))[0] == 200
    _, cookie, _ = hub.login("member")
    # The list behind the card's link is empty, so the panel shows no link.
    assert listing(hub, cookie, "#502") == (200, []) == listing(hub, cookie, absent_card)
    # The page a hand-typed link would open answers exactly as for a card with no handoff.
    found = page(hub, cookie, card_route("#502"))
    assert found[:2] == page(hub, cookie, card_route(absent_card))[:2]
    assert found[0] == 404
    # So does the thread itself, and nothing in either answer names it.
    thread = page(hub, cookie, f"/app/threads/{record.stored_thread_id}")
    assert thread[:2] == page(hub, cookie, "/app/threads/thr_deadbeef")[:2]
    assert thread[0] == 404
    for _, body, _ in (found, thread):
        assert record.stored_thread_id not in body and PRIVATE_TEXT not in body


def test_unsaved_handoff_of_another_connection_stays_owner_only(hub):
    record = handoff(hub, "#503", PRIVATE_TEXT, unsaved=True)
    _, owner = hub.login("owner")[:2]
    assert [row["id"] for row in listing(hub, owner, "#503")[1]] == [record.id]
    assert page(hub, owner, card_route("#503"))[0] == 200
    _, member, _ = hub.login("member")
    assert listing(hub, member, "#503") == (200, [])
    assert page(hub, member, card_route("#503"))[:2] == page(hub, member, card_route("#599"))[:2]
