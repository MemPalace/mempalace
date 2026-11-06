"""Project search contracts over synthetic verbatim data and real HTTP."""

import importlib.util
import json
from dataclasses import replace
from urllib.parse import urlencode

import pytest

from commonplace import sessions
from commonplace.chat import store
from tests._chat_helpers import chat_environment, chat_hub_fixture  # noqa: F401
from tests._console_helpers import examples
from tests.test_commonplace_boards import service


def search(h, auth=None, **params):
    return h.get(auth or h.owner, "/api/chat/atlas/search?" + urlencode({"q": "rate", **params}))[
        :2
    ]


@pytest.fixture
def seeded(chat_hub):
    h = chat_hub
    owner = service(h, h.person(h.admin))
    owner.board_add(slug="atlas", wing="shared")
    owner.space_post("atlas", "  RATE limit\r\nexact words — café  ")
    owner.space_post("atlas", "rate alone")
    owner.space_post("atlas", r"literal rate%_\ marker")
    owner.space_post("work", "rate OTHER BOARD")
    card = owner.task_add("rate title", body="rate body", board="atlas", status="backlog")
    owner.task_comment(card["number"], "rate comment")
    replies = store.open_replies(h.gateway, h.workspace)
    for mid, user, kind, text in (
        (101, h.admin, "answer", "rate answer exact\n  words"),
        (102, h.member_user, "answer", "rate OTHER USER"),
        (103, h.admin, "receipt", "rate RECEIPT"),
    ):
        replies.add(
            board="atlas",
            message_id=mid,
            for_user=user,
            kind=kind,
            code="ok",
            text=text,
            redacted=(),
            model="provider/model",
        )
    return h, owner, card


def test_search_verbatim_sources_and_shape(seeded):
    h, _, card = seeded
    status, body = search(h)
    assert status == 200  # RED
    assert "results" in body and "truncated" in body
    rows = body["results"]
    assert {r["kind"] for r in rows} == {"chat", "reply", "card"}
    assert any(r["text"] == "  RATE limit\r\nexact words — café  " for r in rows)
    assert all(
        set(r) == set("kind id board at actor model number field text".split()) for r in rows
    )
    assert all(r["board"] == "atlas" for r in rows)
    assert {r["number"] for r in rows if r["kind"] == "card"} == {card["number"]}
    assert "OTHER" not in json.dumps(body) and "RECEIPT" not in json.dumps(body)


def test_all_words_and_literal_escaping(seeded):
    h, _, _ = seeded
    status, body = search(h, q="rate LIMIT")
    assert status == 200  # RED
    assert len(body["results"]) == 1
    status, body = search(h, q="rate%_\\")
    assert status == 200
    assert len(body["results"]) == 1
    assert body["results"][0]["text"] == r"literal rate%_\ marker"
    assert search(h, q="RATE café")[1]["results"]
    assert search(h, q="rate CAFÉ")[1]["results"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"q": ""},
        {"q": "a b c d e f g h i"},
        {"q": "x" * 101},
        {"q": "a" * 100 + " " + "b" * 100},
        {"source": "unknown"},
        {"since": "yesterday"},
    ],
)
def test_invalid_queries(seeded, params):
    h, _, _ = seeded
    assert search(h, **params)[0] == 400  # RED


def test_filters_and_private_answers(seeded):
    h, _, _ = seeded
    status, body = search(h, source="reply")
    assert status == 200  # RED
    assert [r["text"] for r in body["results"]] == ["rate answer exact\n  words"]
    assert body["results"][0]["model"] == "provider/model"
    assert search(h, source="chat", **{"from": "conn:missing"})[1]["results"] == []
    rows = search(h, source="chat", **{"from": "user:" + h.admin})[1]["results"]
    assert len(rows) == 3 and all(r["actor"] == "user:" + h.admin for r in rows)
    assert search(h, since="2999-01-01T00:00:00Z")[1]["results"] == []
    assert search(h, since="2000-01-01T00:00:00Z")[1]["results"]


def test_card_list_parity_restricted_origin_and_read_only(seeded):
    h, owner, card = seeded
    with owner.store.connection() as conn:
        conn.execute("UPDATE tasks SET restricted_origin=1 WHERE id=?", (card["number"],))
    allowed = {c["number"] for c in owner.task_list(board="atlas", all_statuses=True)}
    with owner.store.connection() as conn:
        before = list(conn.iterdump())
    seq = h.seq()
    status, body = search(h, source="card")
    assert status == 200  # RED
    assert {r["number"] for r in body["results"]} == allowed
    assert {r["field"] for r in body["results"]} == {"title", "body", "comment"}
    with owner.store.connection() as conn:
        assert list(conn.iterdump()) == before
    assert h.seq() == seq


def test_bounded_newest_first(seeded):
    h, owner, _ = seeded
    for n in range(22):
        owner.space_post("atlas", f"rate newest {n}")
    status, body = search(h, source="chat")
    assert status == 200  # RED
    assert body["truncated"] is True and len(body["results"]) == 20
    assert body["results"][0]["text"] == "rate newest 21"
    assert [r["id"] for r in body["results"]] == sorted(
        (r["id"] for r in body["results"]), reverse=True
    )


def test_redaction_for_all_sources(seeded):
    h, owner, _ = seeded
    _, text, secrets = examples()[0]
    owner.space_post("atlas", "rate " + text)
    owner.task_add("rate secret", body="rate " + text, board="atlas")
    status, body = search(h)
    assert status == 200  # RED
    assert all(value not in json.dumps(body) for value in secrets)


def test_guard_non_reader_and_missing_scope(seeded, monkeypatch):
    h, _, _ = seeded
    original = sessions.person_principal

    def limited(db, user, workspace):
        p = original(db, user, workspace)
        if p is None:
            return p
        return replace(p, policy=replace(p.policy, read_wings=("elsewhere",)))

    monkeypatch.setattr(sessions, "person_principal", limited)
    status, body = search(h)
    assert status == 404
    assert not any(word in json.dumps(body) for word in ("rate", "answer", "title", "comment"))
    monkeypatch.setattr(
        sessions,
        "person_principal",
        lambda db, user, ws: replace(
            original(db, user, ws), policy=replace(original(db, user, ws).policy, scopes=())
        ),
    )
    assert search(h)[0] == 403  # RED


def test_guard_non_member_and_agent_disclosure(seeded):
    h, _, _ = seeded
    for auth, expected in ((h.other, 404), (h.t1, 403)):
        status, body = search(h, auth)
        assert status == expected
        assert not any(word in json.dumps(body) for word in ("rate", "answer", "title", "comment"))


def test_search_method_existence():
    assert importlib.util.find_spec("commonplace.chat.search") is not None  # RED
    assert hasattr(store.ReplyStore, "search")
