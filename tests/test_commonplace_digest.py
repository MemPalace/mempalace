"""Voice digest wording, shared queue semantics, scopes and both transports."""

import json
import shutil
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest

from commonplace.boards.digest import digest, ssml
from tests._commonplace_helpers import (
    REVIEW_CONTEXT,
    REVIEW_OPTIONS,
    hub_fixture,  # noqa: F401
)
from tests.test_commonplace_boards import api, env, mcp, service  # noqa: F401
from tests.test_commonplace_web import request


def item(number=1, title="Pick a plan", need="you", board="work"):
    return {"number": number, "title": title, "need": need, "board": board}


def test_empty_and_single_wording():
    assert digest({"asks": [], "count": 0}) == {
        "text": "Nothing needs you today.",
        "count": 0,
        "items": [],
        "remaining": 0,
    }
    card = item(title='Café 日本語 “yes” "no" <script>')
    assert digest({"asks": [card]}) == {
        "text": 'One thing needs you.\nCafé 日本語 “yes” "no" <script> on work.',
        "count": 1,
        "items": [card],
        "remaining": 0,
    }


def test_many_priority_deduplication_and_tail():
    cards = [
        item(n, need=need)
        for n, need in enumerate(
            ["idle", "merge", "plan", "you", "you", "you", "idle", "idle", "idle"], 1
        )
    ]
    groups = {"stale": cards, "asks": [cards[3]], "count": 9}
    result = digest(groups)
    assert result["text"].startswith("Nine things need you.\n")
    assert result["text"].endswith("\nAnd 4 more.")
    assert [c["number"] for c in result["items"]] == [4, 5, 6, 3, 2]
    assert result["count"] == 9 and result["remaining"] == 4
    assert digest({"asks": list(reversed(cards))}) == result


def test_all_priorities_and_three_wording():
    cards = [item(1, need="idle"), item(2, need="merge"), item(3, need="plan")]
    result = digest({"stale": cards})
    assert result["text"] == (
        "Three things need you.\nPick a plan on work.\nPick a plan on work.\nPick a plan on work."
    )
    assert [c["need"] for c in result["items"]] == ["plan", "merge", "idle"]


def test_owner_token_questions_lead_without_changing_their_derived_need():
    question = {**item(3, need="idle"), "ask": "Ship?"}
    labelled = {**item(4, need="plan"), "ask": "Which plan?"}
    result = digest(
        {
            "asks": [labelled, question],
            "done_review": [question],
            "ready_to_merge": [item(1, need="merge")],
            "stale": [item(2, need="idle")],
        }
    )
    assert [c["number"] for c in result["items"]] == [3, 4, 1, 2]
    assert result["items"][0]["need"] == "idle"


@pytest.mark.parametrize("count", [0, 1, 9])
def test_ssml_keeps_titles_and_boards_as_xml_text(count):
    title = 'a</s><break time="10s"/>&'
    board = "b <room> & \"quotes\" and 'apostrophes'"
    result = digest({"asks": [item(n, title, board=board) for n in range(count)]})
    encoded = ssml(result)
    root = ElementTree.fromstring(encoded)
    assert root.tag == "speak" and list(root) == []
    assert root.text == result["text"]
    if count:
        assert title in root.text and board in root.text
        assert "&quot;" in encoded and "&apos;" in encoded
        assert "&lt;break" in encoded and "&amp;" in encoded


def test_endpoint_and_mcp_scoped_verbatim(hub, env):  # noqa: F811
    token, agent, person, cookie, owner = env
    worker = service(hub, agent)
    title = f'Café “ship” 日本語 "now" {person.actor}'
    asked = worker.ask(title, board="work", context=REVIEW_CONTEXT, options=REVIEW_OPTIONS)
    ready = worker.task_add(title="Ready", board="work")
    worker.task_take_next(task=ready["number"])
    worker.task_update(ready["number"], pr=8, checks="passing")
    with owner.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET heartbeat_at='2000-01-01T00:00:00Z' WHERE id=?", (ready["number"],)
        )
    worker.task_add(title="Agent backlog", board="work", status="backlog")
    # A plan for another person is not in this viewer's queue.
    other = hub.db.create_user("other@example.com")
    hub.db.add_member(hub.workspace, other, "member", actor=hub.admin)
    theirs = worker.ask(
        "Another person's question", board="work", context=REVIEW_CONTEXT, options=REVIEW_OPTIONS
    )
    with owner.store.transaction() as conn:
        conn.execute("UPDATE tasks SET ask_to=? WHERE id=?", (f"user:{other}", theirs["number"]))
    owner.board_add(slug="secret", wing="private.secret")
    owner.seed_card(board="secret", title="SECRET CANARY", status="review")
    scoped = hub.token(
        "digest-reader", profile="full", scopes=["memory:read"], read_wings=["shared"]
    )
    expected = worker.needs_me(format="digest")
    assert expected["count"] == 2  # Includes the secret for the unrestricted owner.
    status, payload = api(hub, scoped, "GET", "/api/needs-me/digest")
    assert status == 200 and payload["count"] == 1
    assert payload["items"][0] == item(asked["number"], title, need="idle")
    assert "SECRET CANARY" not in json.dumps(payload)
    assert "Another person's question" not in json.dumps(payload)
    ok, output = mcp(hub, scoped, "needs_me", format="digest")
    assert ok and json.loads(output) == payload
    assert title in payload["text"]
    session_digest = api(hub, cookie, "GET", "/api/needs-me/digest")[1]
    assert session_digest == owner.needs_me(format="digest")
    assert session_digest["items"][0]["need"] == "you"
    assert session_digest["text"] == expected["text"]
    assert api(hub, token, "GET", "/api/needs-me/digest")[1] == expected
    status, headers, text = request(
        hub, "/api/needs-me/digest?format=text", headers={"Authorization": f"Bearer {scoped}"}
    )
    assert status == 200 and headers["Content-Type"] == "text/plain; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert text.decode() == payload["text"]
    status, headers, encoded = request(
        hub, "/api/needs-me/digest?format=ssml", headers={"Authorization": f"Bearer {scoped}"}
    )
    assert status == 200 and headers["Content-Type"] == "application/ssml+xml; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert ElementTree.fromstring(encoded).text == payload["text"]
    assert api(hub, None, "GET", "/api/needs-me/digest")[0] == 401
    assert api(hub, cookie, "GET", "/api/needs-me/digest?format=oops")[0] == 400
    assert not mcp(hub, scoped, "needs_me", format="oops")[0]
    member = hub.token(
        "member-digest", user=other, profile="full", scopes=["memory:read"], read_wings=["shared"]
    )
    assert api(hub, member, "GET", "/api/needs-me/digest")[0] == 403
    assert not mcp(hub, member, "needs_me", format="digest")[0]


def test_ssml_endpoint_does_not_execute_title_markup(hub, env):  # noqa: F811
    token, agent, _person, cookie, _owner = env
    title = 'a</s><break time="10s"/>&'
    service(hub, agent).ask(title, board="work", context=REVIEW_CONTEXT, options=REVIEW_OPTIONS)
    for auth in (cookie, {"Authorization": f"Bearer {token}"}):
        status, _headers, encoded = request(hub, "/api/needs-me/digest?format=ssml", headers=auth)
        root = ElementTree.fromstring(encoded)
        assert status == 200 and root.tag == "speak" and list(root) == []
        assert root.text == f"One thing needs you.\n{title} on work."


def test_speech_lifecycle():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed for browser speech controller checks")
    script = Path(__file__).with_name("commonplace_digest_speech.mjs")
    subprocess.run([node, str(script)], check=True, timeout=10)
