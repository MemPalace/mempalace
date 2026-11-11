"""/api/bots over real HTTP: defaults, management, threads, unread, memory and isolation."""

import pytest

from tests._bots_helpers import bots_hub_fixture  # noqa: F401
from tests.test_commonplace_boards import service

REQ = "request-0001"
QUESTION = "Should this change ship now or wait for the next release?"
CONTEXT = "The implementation is complete and the affected tests have passed."
OPTIONS = ["Ship this change", "Wait for the next release"]


def seed(hub, auth_user):
    """A bot of ``auth_user`` with one user message and its reply, written through the store."""
    bots = hub.store()
    bot = bots.create(auth_user, "Seeded")
    ask = bots.add_user_message(bot["id"], auth_user, "hello there", REQ)
    reply = bots.add_bot_message(bot["id"], auth_user, ask["id"], "hi back")
    return bot["id"], ask, reply


def test_listing_creates_a_general_bot_and_one_per_readable_board(bots_hub):
    hub = bots_hub
    hub.add_board("garden", "Garden")
    body = hub.bots(hub.owner)
    assert sorted((b["kind"], b["board"]) for b in body["bots"]) == [
        ("general", None),
        ("project", "garden"),
        ("project", "work"),
    ]
    garden = next(b for b in body["bots"] if b["board"] == "garden")
    assert garden["name"] == "Garden"
    assert {"id", "name", "job", "instructions", "kind", "board", "last", "unread"} <= set(garden)
    assert (garden["last"], garden["unread"], garden["waiting"], garden["state"]) == (
        None,
        0,
        0,
        "idle",
    )
    ids = {b["id"] for b in body["bots"]}
    assert {b["id"] for b in hub.bots(hub.owner)["bots"]} == ids


def test_archived_board_gets_no_bot_and_an_archived_default_is_not_recreated(bots_hub):
    hub = bots_hub
    hub.add_board("old", "Old")
    hub.archive_board("old")
    bots = hub.by_board(hub.owner)
    assert "old" not in bots and "work" in bots
    status, body, _ = hub.request(
        hub.owner, "PATCH", f"/api/bots/{bots['work']['id']}", {"archived": True}
    )
    assert (status, body["bot"]["archived"]) == (200, True)
    assert "work" not in hub.by_board(hub.owner)


def test_each_person_has_their_own_bots(bots_hub):
    hub = bots_hub
    owner, member, other = (
        hub.by_board(hub.owner),
        hub.by_board(hub.member),
        hub.by_board(hub.other),
    )
    assert set(owner) == set(member) == {"general", "work"}
    assert not {b["id"] for b in owner.values()} & {b["id"] for b in member.values()}
    assert set(other) == {"general"}


def test_model_state_and_who_can_configure(bots_hub):
    hub = bots_hub
    owner, member = hub.bots(hub.owner), hub.bots(hub.member)
    assert owner["model"] == {"ready": False, "label": "", "code": "needs_configuration"}
    assert (owner["can_configure"], member["can_configure"]) == (True, False)
    status, body, _ = hub.request(
        hub.owner,
        "PUT",
        "/api/chat-settings",
        {
            "front": {
                "provider": "openai_compatible",
                "model": "fast-model",
                "base_url": "http://127.0.0.1:9",
            }
        },
    )
    assert status == 200, body
    assert hub.bots(hub.member)["model"] == {
        "ready": True,
        "label": "openai_compatible/fast-model",
        "code": "",
    }


def test_create_rename_and_archive_over_http(bots_hub):
    hub = bots_hub
    status, body, _ = hub.post(
        hub.owner, "/api/bots", {"name": "Reading list", "job": "Track books"}
    )
    assert status == 201 and body["bot"]["kind"] == "custom" and body["bot"]["job"] == "Track books"
    bot_id = body["bot"]["id"]
    status, body, _ = hub.request(
        hub.owner, "PATCH", f"/api/bots/{bot_id}", {"name": "Books", "instructions": "Be brief"}
    )
    assert (status, body["bot"]["name"], body["bot"]["instructions"]) == (200, "Books", "Be brief")
    assert hub.request(hub.owner, "PATCH", f"/api/bots/{bot_id}", {"archived": True})[0] == 200
    assert bot_id not in {b["id"] for b in hub.bots(hub.owner)["bots"]}
    assert (
        hub.request(hub.owner, "PATCH", f"/api/bots/{bot_id}", {"archived": False})[1]["bot"][
            "archived"
        ]
        is False
    )


def test_a_custom_bot_may_be_scoped_to_a_readable_project_only(bots_hub):
    hub = bots_hub
    status, body, _ = hub.post(hub.owner, "/api/bots", {"name": "Work helper", "board": "work"})
    assert (status, body["bot"]["board"], body["bot"]["kind"]) == (201, "work", "custom")
    status, body, _ = hub.post(hub.owner, "/api/bots", {"name": "Ghost", "board": "nowhere"})
    assert (status, body["code"]) == (400, "invalid")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "x" * 61},
        {"name": "ok", "job": "x" * 201},
        {"name": "ok", "instructions": "x" * 4001},
        {"name": "ok", "other": 1},
        {"name": 5},
    ],
)
def test_create_rejects_out_of_bound_input(bots_hub, body):
    status, reply, _ = bots_hub.post(bots_hub.owner, "/api/bots", body)
    assert (status, reply["code"]) == (400, "invalid")


def test_patch_rejects_unknown_fields_and_bad_values(bots_hub):
    hub = bots_hub
    bot_id = hub.post(hub.owner, "/api/bots", {"name": "Ok"})[1]["bot"]["id"]
    for body in ({"kind": "general"}, {"name": ""}, {"archived": "yes"}, {"job": "x" * 201}):
        status, reply, _ = hub.request(hub.owner, "PATCH", f"/api/bots/{bot_id}", body)
        assert (status, reply["code"]) == (400, "invalid"), body


def test_at_most_fifty_live_bots_per_person(bots_hub):
    hub = bots_hub
    live = len(hub.bots(hub.owner)["bots"])
    for i in range(50 - live):
        assert hub.post(hub.owner, "/api/bots", {"name": f"Bot {i}"})[0] == 201
    status, body, _ = hub.post(hub.owner, "/api/bots", {"name": "Too many"})
    assert (status, body["code"]) == (409, "limit")
    assert hub.post(hub.member, "/api/bots", {"name": "Member bot"})[0] == 201


def test_messages_page_and_hydrate_blocks(bots_hub):
    hub = bots_hub
    bot_id, ask, reply = seed(hub, hub.hub.admin)
    status, body, _ = hub.get(hub.owner, f"/api/bots/{bot_id}/messages")
    assert status == 200 and body["running"] is False and body["waiting"] == []
    assert [(m["id"], m["role"], m["text"]) for m in body["messages"]] == [
        (ask["id"], "user", "hello there"),
        (reply["id"], "bot", "hi back"),
    ]
    assert {"id", "role", "text", "blocks", "at", "state", "reply_to"} <= set(body["messages"][0])
    assert body["bot"]["id"] == bot_id and body["bot"]["last"]["text"] == "hi back"
    status, body, _ = hub.get(hub.owner, f"/api/bots/{bot_id}/messages?after={ask['id']}&limit=1")
    assert [m["id"] for m in body["messages"]] == [reply["id"]]
    status, body, _ = hub.get(hub.owner, f"/api/bots/{bot_id}/messages?before={reply['id']}")
    assert [m["id"] for m in body["messages"]] == [ask["id"]]
    for query in ("limit=101", "limit=0", "after=x", "after=1&before=2", "limit="):
        status, body, _ = hub.get(hub.owner, f"/api/bots/{bot_id}/messages?{query}")
        assert (status, body["code"]) == (400, "invalid"), query


def test_work_and_decision_blocks_show_the_readers_view_and_missing_items_as_gone(bots_hub):
    hub = bots_hub
    owner = service(hub.hub, hub.person(hub.hub.admin))
    card = owner.task_add(title="Fix the login page", board="work", claim=True)["number"]
    asked = owner.task_add(title="Pick a release", board="work", claim=True)["number"]
    owner.task_update(asked, status="review", question=QUESTION, context=CONTEXT, options=OPTIONS)
    bots = hub.store()
    bot = bots.create(hub.hub.admin, "Blocks")["id"]
    ask = bots.add_user_message(bot, hub.hub.admin, "show me", REQ)
    bots.add_bot_message(
        bot,
        hub.hub.admin,
        ask["id"],
        "here",
        blocks=[
            {"type": "work", "card": card},
            {"type": "work", "card": 99999},
            {"type": "decision", "card": asked},
            {"type": "action", "tool": "create_work", "label": "Filed it", "ok": True},
            {"type": "setup", "code": "needs_configuration"},
            {"type": "from_a_later_slice", "x": 1},
        ],
    )
    blocks = hub.get(hub.owner, f"/api/bots/{bot}/messages")[1]["messages"][-1]["blocks"]
    work, gone, decision, action, setup, unknown = blocks
    assert (work["title"], work["status"], work["card"]) == ("Fix the login page", "working", card)
    assert {"note", "links", "version"} <= set(work)
    assert gone == {"type": "work", "card": 99999, "gone": True}
    assert (decision["question"], decision["options"], decision["open"], decision["answer"]) == (
        QUESTION,
        OPTIONS,
        True,
        None,
    )
    assert decision["context"].startswith(CONTEXT[:20]) and "version" in decision
    assert (action["label"], action["ok"]) == ("Filed it", True)
    assert setup["can_configure"] is True
    assert unknown == {"type": "from_a_later_slice", "x": 1}
    owner.decide(asked, choice=OPTIONS[0])
    decision = hub.get(hub.owner, f"/api/bots/{bot}/messages")[1]["messages"][-1]["blocks"][2]
    assert (decision["open"], decision["answer"]) == (False, OPTIONS[0])


@pytest.mark.parametrize(
    ("status", "expected"),
    [("backlog", "queued"), ("todo", "queued"), ("doing", "working"), ("done", "done")],
)
def test_work_status_words(status, expected):
    from commonplace.bots import blocks

    card = {"status": status, "waiting": None}
    assert blocks.work_status(card) == (expected, "")


def test_work_status_review_blocked_and_needs_you():
    from commonplace.bots import blocks

    done_review = {"status": "review", "review_kind": "completion"}
    assert blocks.work_status(done_review) == ("ready", "")
    question = {"status": "review", "review_kind": "decision", "need": "you"}
    assert blocks.work_status(question) == ("needs_you", "")
    elsewhere = {"status": "review", "review_kind": "decision", "need": "agent"}
    assert blocks.work_status(elsewhere)[0] == "blocked"
    waiting = {"status": "todo", "waiting": {"code": "waits_on_person"}}
    assert blocks.work_status(waiting) == ("blocked", "Waiting on a person")
    blockers = {
        "status": "doing",
        "waiting": {"code": "blocked_by", "blockers": [{"number": 4, "title": "Fix"}]},
    }
    assert blocks.work_status(blockers) == ("blocked", "Waiting on #4: Fix")
    assert blocks.work_status({"status": "doing"}) == ("working", "")


def test_waiting_decisions_count_per_project_and_all_for_general(bots_hub):
    hub = bots_hub
    hub.add_board("garden", "Garden")
    owner = service(hub.hub, hub.person(hub.hub.admin))
    for board in ("work", "work", "garden"):
        number = owner.task_add(title=f"Question on {board}", board=board, claim=True)["number"]
        owner.task_update(
            number, status="review", question=QUESTION, context=CONTEXT, options=OPTIONS
        )
    bots = hub.by_board(hub.owner)
    assert (bots["work"]["waiting"], bots["garden"]["waiting"], bots["general"]["waiting"]) == (
        2,
        1,
        3,
    )
    assert {b["state"] for b in bots.values()} == {"needs_you"}
    body = hub.get(hub.owner, f"/api/bots/{bots['work']['id']}/messages")[1]
    assert [b["type"] for b in body["waiting"]] == ["decision", "decision"]
    assert all(b["open"] and b["question"] == QUESTION for b in body["waiting"])
    assert len(hub.get(hub.owner, f"/api/bots/{bots['general']['id']}/messages")[1]["waiting"]) == 3
    assert hub.bots(hub.member)["bots"][0]["waiting"] == 0


def test_read_marks_the_thread_read(bots_hub):
    hub = bots_hub
    bot_id, ask, reply = seed(hub, hub.hub.admin)
    row = next(b for b in hub.bots(hub.owner)["bots"] if b["id"] == bot_id)
    assert row["unread"] == 1
    assert hub.post(hub.owner, f"/api/bots/{bot_id}/read", {"upto": reply["id"]})[1] == {"ok": True}
    row = next(b for b in hub.bots(hub.owner)["bots"] if b["id"] == bot_id)
    assert row["unread"] == 0
    for body in ({}, {"upto": "x"}, {"upto": -1}, {"upto": 1, "x": 1}):
        status, reply_body, _ = hub.post(hub.owner, f"/api/bots/{bot_id}/read", body)
        assert (status, reply_body["code"]) == (400, "invalid"), body


def test_memory_add_list_and_remove_with_a_tombstone(bots_hub):
    hub = bots_hub
    bot_id = hub.bots(hub.owner)["bots"][0]["id"]
    first = hub.post(hub.owner, f"/api/bots/{bot_id}/memory", {"text": "prefers short answers"})[1][
        "memory"
    ]
    second = hub.post(hub.owner, f"/api/bots/{bot_id}/memory", {"text": "lives on a boat"})[1][
        "memory"
    ]
    assert {"id", "text", "at"} == set(first)
    listed = hub.get(hub.owner, f"/api/bots/{bot_id}/memory")[1]["memories"]
    assert [m["text"] for m in listed] == ["prefers short answers", "lives on a boat"]
    status, body, _ = hub.request(hub.owner, "DELETE", f"/api/bots/{bot_id}/memory/{second['id']}")
    assert (status, body) == (200, {"ok": True})
    assert [m["id"] for m in hub.get(hub.owner, f"/api/bots/{bot_id}/memory")[1]["memories"]] == [
        first["id"]
    ]
    assert hub.request(hub.owner, "DELETE", f"/api/bots/{bot_id}/memory/{second['id']}")[0] == 404
    assert hub.request(hub.owner, "DELETE", f"/api/bots/{bot_id}/memory/nope")[0] == 404
    for text in ("", "x" * 2001, 5):
        status, body, _ = hub.post(hub.owner, f"/api/bots/{bot_id}/memory", {"text": text})
        assert (status, body["code"]) == (400, "invalid")
    stored = (
        hub.store()
        ._connection()
        .execute("SELECT text,removed_at FROM bot_memory WHERE id=?", (second["id"],))
        .fetchone()
    )
    assert stored["text"] == "lives on a boat" and stored["removed_at"]


def test_messages_are_redacted_on_display_but_stored_verbatim(bots_hub):
    hub = bots_hub
    secret = "sk-" + "a1B2c3D4" * 5
    bot_id, ask, reply = seed(hub, hub.hub.admin)
    hub.store().add_event_message(bot_id, hub.hub.admin, f"token {secret}")
    shown = hub.get(hub.owner, f"/api/bots/{bot_id}/messages")[1]["messages"][-1]["text"]
    assert secret not in shown
    assert hub.store().messages(bot_id, hub.hub.admin)[-1]["text"] == f"token {secret}"


def raw_request(hub, method, path, raw):
    """Send ``raw`` as the JSON body, which ``hub.request`` cannot for non-object or odd text."""
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", hub.hub.port, timeout=30)
    try:
        conn.request(method, path, raw.encode(), {"Content-Type": "application/json", **hub.owner})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def routes_for(bot_id):
    return [
        ("GET", f"/api/bots/{bot_id}/messages", None),
        ("POST", f"/api/bots/{bot_id}/read", {"upto": 1}),
        ("PATCH", f"/api/bots/{bot_id}", {"name": "Taken"}),
        ("GET", f"/api/bots/{bot_id}/memory", None),
        ("POST", f"/api/bots/{bot_id}/memory", {"text": "planted"}),
        ("DELETE", f"/api/bots/{bot_id}/memory/1", None),
    ]


def test_another_person_gets_404_for_every_route_of_a_bot_id(bots_hub):
    hub = bots_hub
    bot_id, _ask, _reply = seed(hub, hub.hub.admin)
    hub.store().memory_add(bot_id, hub.hub.admin, "private note")
    for who in (hub.member, hub.other):
        for method, path, body in routes_for(bot_id):
            status, reply, _ = hub.request(who, method, path, body)
            assert (status, reply.get("code")) == (404, "not_found"), (method, path)
    assert hub.request(hub.owner, "GET", f"/api/bots/{bot_id}/messages")[0] == 200
    assert hub.store().get(bot_id, hub.hub.admin)["name"] == "Seeded"
    assert [m["text"] for m in hub.store().memory_list(bot_id, hub.hub.admin)] == ["private note"]
    assert hub.request(hub.owner, "GET", "/api/bots/no-such-bot/messages")[0] == 404


def test_agent_tokens_get_404_everywhere_and_anonymous_is_asked_to_sign_in(bots_hub):
    hub = bots_hub
    bot_id, _ask, _reply = seed(hub, hub.hub.admin)
    everything = [
        ("GET", "/api/bots", None),
        ("POST", "/api/bots", {"name": "Agent bot"}),
        *routes_for(bot_id),
    ]
    for token in (hub.t1, hub.tm):
        for method, path, body in everything:
            status, reply, _ = hub.request(token, method, path, body)
            assert status == 404, (method, path)
            assert "bots" not in reply and "bot" not in reply
    for method, path, body in everything:
        assert hub.request(None, method, path, body)[0] == 401, (method, path)
    assert len(hub.store().roster(hub.hub.admin)) == 1


def test_unknown_bot_routes_are_not_served(bots_hub):
    hub = bots_hub
    bot_id = hub.bots(hub.owner)["bots"][0]["id"]
    assert hub.post(hub.owner, f"/api/bots/{bot_id}/elsewhere", {"text": "x"})[0] == 404
    assert hub.get(hub.owner, f"/api/bots/{bot_id}")[0] == 404


def test_threads_survive_a_store_reopen(bots_hub):
    hub = bots_hub
    bot_id, ask, reply = seed(hub, hub.hub.admin)
    hub.store().close()
    body = hub.get(hub.owner, f"/api/bots/{bot_id}/messages")[1]
    assert [m["id"] for m in body["messages"]] == [ask["id"], reply["id"]]


@pytest.mark.parametrize(
    ("front", "key", "code"),
    [
        (dict(enabled=True, provider="none"), None, "needs_configuration"),
        (dict(enabled=False, provider="openai_compatible"), None, "needs_configuration"),
        (dict(enabled=True, provider="anthropic", applies=False), None, "needs_turn_on"),
        (dict(enabled=True, provider="anthropic", key_ref="env:K"), None, "key_missing"),
        (dict(enabled=True, provider="anthropic", key_ref="env:K"), "secret", ""),
        (dict(enabled=True, provider="openai_compatible", key_ref=None), None, ""),
    ],
)
def test_model_resolution_codes(monkeypatch, front, key, code):
    from types import SimpleNamespace

    from commonplace.bots import modelcfg

    config = SimpleNamespace(**{"applies": True, "key_ref": None, **front})
    monkeypatch.setattr(modelcfg.chat_settings, "front", lambda *args: config)
    monkeypatch.setattr(modelcfg.chat_settings, "resolve_key", lambda _config: key)
    settings, resolved, got = modelcfg.resolve(None, "ws", "board")
    assert got == code
    assert (settings is config) == (code == "") and resolved == (key if code == "" else None)


def test_memory_ids_beyond_the_integer_range_are_404_not_a_crash(bots_hub):
    hub = bots_hub
    bot_id = hub.bots(hub.owner)["bots"][0]["id"]
    for memory_id in (2**63, 10**30, 2**64 + 5, "9" * 5000):
        status, body, _ = hub.request(hub.owner, "DELETE", f"/api/bots/{bot_id}/memory/{memory_id}")
        assert (status, body["code"]) == (404, "not_found"), memory_id


@pytest.mark.parametrize("raw", ["[]", "5", "null", '"text"', "true"])
def test_bodies_that_are_not_objects_are_400(bots_hub, raw):
    hub = bots_hub
    bot_id = hub.bots(hub.owner)["bots"][0]["id"]
    for method, path in (
        ("POST", "/api/bots"),
        ("PATCH", f"/api/bots/{bot_id}"),
        ("POST", f"/api/bots/{bot_id}/read"),
        ("POST", f"/api/bots/{bot_id}/memory"),
    ):
        status, body = raw_request(hub, method, path, raw)
        assert status == 400, (method, path, raw)


@pytest.mark.parametrize("board", [[], {}, ["work"], 5, True, "", None])
def test_create_rejects_a_board_that_is_not_a_project_slug(bots_hub, board):
    status, body, _ = bots_hub.post(bots_hub.owner, "/api/bots", {"name": "Odd", "board": board})
    if board is None:
        assert status == 201
    else:
        assert (status, body["code"]) == (400, "invalid")


def test_text_that_cannot_be_stored_is_400(bots_hub):
    hub = bots_hub
    bot_id = hub.bots(hub.owner)["bots"][0]["id"]
    status, body = raw_request(hub, "POST", f"/api/bots/{bot_id}/memory", '{"text": "a\\ud800b"}')
    assert status == 400
    status, body = raw_request(hub, "POST", "/api/bots", '{"name": "a\\ud800b"}')
    assert status == 400


def test_padded_memory_text_over_the_bound_is_400(bots_hub):
    hub = bots_hub
    bot_id = hub.bots(hub.owner)["bots"][0]["id"]
    status, body, _ = hub.post(hub.owner, f"/api/bots/{bot_id}/memory", {"text": " " + "m" * 2000})
    assert (status, body["code"]) == (400, "invalid")
    assert hub.get(hub.owner, f"/api/bots/{bot_id}/memory")[1]["memories"] == []


def test_a_cross_origin_write_on_a_person_session_is_refused(bots_hub):
    hub = bots_hub
    bot_id = hub.bots(hub.owner)["bots"][0]["id"]
    foreign = {**hub.owner, "Origin": "http://attacker.example"}
    for method, path, body in (
        ("POST", "/api/bots", {"name": "Planted"}),
        ("PATCH", f"/api/bots/{bot_id}", {"name": "Taken"}),
        ("POST", f"/api/bots/{bot_id}/read", {"upto": 1}),
        ("POST", f"/api/bots/{bot_id}/memory", {"text": "planted"}),
        ("DELETE", f"/api/bots/{bot_id}/memory/1", None),
    ):
        assert hub.request(foreign, method, path, body)[0] == 403, (method, path)
    names = [b["name"] for b in hub.bots(hub.owner)["bots"]]
    assert "Planted" not in names and "Taken" not in names
    assert hub.get(hub.owner, f"/api/bots/{bot_id}/memory")[1]["memories"] == []
