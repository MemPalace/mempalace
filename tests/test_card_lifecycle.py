"""Done is final: Archive (hide, never delete), carry-on follow-ups and the Pocket."""

import pytest

from commonplace import sessions
from commonplace.extensions import ToolError
from tests.test_commonplace_boards import (
    api,
    env as env,
    hub_fixture as hub_fixture,
    service,
)

TABLES = ("tasks", "task_events", "card_archive", "card_followups", "card_pocket")


@pytest.fixture
def member(hub, env):
    user = hub.db.create_user("lifecycle-member@example.com")
    hub.db.add_member(hub.workspace, user, "member", actor=hub.admin)
    session = sessions.redeem_login_link(
        hub.db, sessions.create_login_link(hub.db, "lifecycle-member@example.com")
    )
    person = sessions.person_principal(hub.db, user, hub.workspace)
    return service(hub, person), {"Cookie": f"{sessions.COOKIE}={session}"}


def done_card(owner, title="Ship it"):
    card = owner.task_add(title, board="work")
    doing = owner.task_update(card["number"], version=card["version"], status="doing")
    return owner.task_update(card["number"], version=doing["version"], status="done")


def counts(owner):
    with owner.store.connection() as conn:
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def numbers(cards):
    return [c["number"] for c in cards]


def test_done_is_final_and_open_cards_are_not(env):
    *_, owner = env
    done = done_card(owner)
    assert done["final"] is True and done["archived"] is False
    assert owner.task_add("Open", board="work")["final"] is False


def test_archive_hides_but_keeps_everything_readable_and_stored(env):
    *_, owner = env
    done = done_card(owner)
    keep = done_card(owner, "Stays")
    before = counts(owner)
    archived = owner.task_archive(done["number"], version=done["version"])
    assert archived["archived"] is True and archived["version"] == done["version"] + 1
    assert archived["archived_by"] == owner.principal.actor and archived["can_restore"] is True
    assert done["number"] not in numbers(owner.task_list(board="work", all_statuses=True))
    assert keep["number"] in numbers(owner.task_list(board="work", all_statuses=True))
    assert numbers(owner.task_list(board="work", archived=True)) == [done["number"]]
    shown = owner.task_show(done["number"])
    assert shown["archived"] is True and shown["title"] == "Ship it"
    assert shown["events"][-1]["text"] == "Archived"
    board = next(b for b in owner.board_list() if b["slug"] == "work")
    assert board["counts"]["done"] == 1 and board["archived"] == 1
    after = counts(owner)
    assert after["tasks"] == before["tasks"] and after["card_archive"] == 1
    assert after["task_events"] > before["task_events"]


def test_restore_returns_the_card_to_done_unchanged(env):
    *_, owner = env
    done = done_card(owner)
    archived = owner.task_archive(done["number"])
    restored = owner.task_restore(done["number"], version=archived["version"])
    assert restored["archived"] is False and restored["status"] == "done"
    assert restored["title"] == done["title"] and restored["body"] == done["body"]
    assert done["number"] in numbers(owner.task_list(board="work", all_statuses=True))
    assert owner.task_list(board="work", archived=True) == []
    assert [e["text"] for e in owner.task_show(done["number"])["events"][-2:]] == [
        "Archived",
        "Restored from the archive",
    ]
    # Archive again after a restore: the same row is reused, never duplicated.
    owner.task_archive(done["number"])
    assert counts(owner)["card_archive"] == 1


def test_archive_and_restore_are_idempotent(env):
    *_, owner = env
    done = done_card(owner)
    assert owner.task_restore(done["number"])["version"] == done["version"]
    first = owner.task_archive(done["number"])
    again = owner.task_archive(done["number"])
    assert again["version"] == first["version"]
    assert counts(owner)["task_events"] == len(owner.task_show(done["number"])["events"])


def test_only_a_done_card_can_be_archived_or_carried_on(env):
    *_, owner = env
    card = owner.task_add("Not yet", board="work")
    with pytest.raises(ToolError, match="only a done card can be archived"):
        owner.task_archive(card["number"])
    with pytest.raises(ToolError, match="only a done card can be carried on"):
        owner.task_follow_up(card["number"])
    assert card["number"] in numbers(owner.task_list(board="work"))
    assert owner.task_show(card["number"])["can_archive"] is False


def test_archived_card_is_read_only_until_restored(env):
    *_, owner = env
    done = done_card(owner)
    owner.task_archive(done["number"])
    current = owner.task_show(done["number"])
    for change in ({"status": "todo"}, {"title": "Renamed"}):
        with pytest.raises(ToolError, match="archived; restore it first"):
            owner.task_update(done["number"], version=current["version"], **change)
    with pytest.raises(ToolError, match="archived; restore it first"):
        owner.pocket_pin(done["number"], "A fact")
    owner.task_restore(done["number"])
    assert owner.pocket_pin(done["number"], "A fact")["pocket"][0]["text"] == "A fact"


def test_stale_version_is_a_clash_and_changes_nothing(env):
    *_, owner = env
    done = done_card(owner)
    with pytest.raises(ToolError):
        owner.task_archive(done["number"], version=done["version"] + 5)
    assert owner.task_show(done["number"])["archived"] is False


def test_follow_up_is_a_new_backlog_card_linked_to_the_original(env):
    *_, owner = env
    done = done_card(owner, "Phase one")
    result = owner.task_follow_up(done["number"], version=done["version"])
    follow = result["card"]
    assert follow["status"] == "backlog" and follow["follow_up_of"] == done["number"]
    assert follow["title"] == "Carry on: Phase one"
    assert follow["body"] == f"Carry on from #{done['number']} Phase one"
    assert follow["final"] is False and follow["number"] != done["number"]
    assert result["origin"]["follow_ups"] == [follow["number"]]
    shown = owner.task_show(done["number"])
    assert shown["follow_ups"] == [follow["number"]] and shown["status"] == "done"
    assert shown["events"][-1]["text"] == f"Carry-on card #{follow['number']} created"
    second = owner.task_follow_up(done["number"], title="Phase two", body="Verbatim body ✓")
    assert second["card"]["title"] == "Phase two" and second["card"]["body"] == "Verbatim body ✓"
    assert second["origin"]["follow_ups"] == [follow["number"], second["card"]["number"]]


def test_follow_up_is_optional_and_works_from_an_archived_card(env):
    *_, owner = env
    done = done_card(owner)
    assert owner.task_show(done["number"])["follow_ups"] == []
    owner.task_archive(done["number"])
    follow = owner.task_follow_up(done["number"])["card"]
    assert follow["number"] in numbers(owner.task_list(board="work", all_statuses=True))
    assert owner.task_show(done["number"])["archived"] is True


def test_pocket_pins_verbatim_facts_and_unpin_keeps_the_row(env):
    *_, owner = env
    card = done_card(owner)
    text = '  Staging key: "ab-12"\nline two — ünï  '
    pinned = owner.pocket_pin(card["number"], text)
    (fact,) = pinned["pocket"]
    assert fact["text"] == text and fact["pinned_by"] == owner.principal.actor
    with pytest.raises(ToolError, match="already pinned"):
        owner.pocket_pin(card["number"], text)
    with pytest.raises(ToolError, match="fact"):
        owner.pocket_pin(card["number"], "")
    other = owner.pocket_pin(card["number"], "Second fact")
    assert [f["text"] for f in other["pocket"]] == [text, "Second fact"]
    removed = owner.pocket_unpin(card["number"], fact["id"], version=other["version"])
    assert [f["text"] for f in removed["pocket"]] == ["Second fact"]
    with owner.store.connection() as conn:
        rows = conn.execute("SELECT text, unpinned_at FROM card_pocket ORDER BY id").fetchall()
    assert [r["text"] for r in rows] == [text, "Second fact"]
    assert rows[0]["unpinned_at"] is not None and rows[1]["unpinned_at"] is None
    with pytest.raises(ToolError, match="not pinned"):
        owner.pocket_unpin(card["number"], fact["id"])
    assert owner.pocket_pin(card["number"], text)["pocket"][-1]["text"] == text


def test_pocket_is_bounded_and_per_card(env):
    *_, owner = env
    card, other = done_card(owner), done_card(owner, "Other")
    for i in range(25):
        owner.pocket_pin(card["number"], f"fact {i}")
    with pytest.raises(ToolError, match="at most 25"):
        owner.pocket_pin(card["number"], "one too many")
    with pytest.raises(ToolError, match="fact"):
        owner.pocket_pin(other["number"], "x" * 2001)
    assert owner.pocket_pin(other["number"], "fact 0")["pocket"][0]["text"] == "fact 0"
    with pytest.raises(ToolError, match="not pinned"):
        owner.pocket_unpin(other["number"], owner.task_show(card["number"])["pocket"][0]["id"])


def test_pocket_works_on_open_cards_too(env):
    *_, owner = env
    card = owner.task_add("Working", board="work")
    assert owner.pocket_pin(card["number"], "Key fact")["can_pocket"] is True


ACTIONS = (
    ("archive", lambda s, n: s.task_archive(n)),
    ("restore", lambda s, n: s.task_restore(n)),
    ("follow-up", lambda s, n: s.task_follow_up(n)),
    ("pocket", lambda s, n: s.pocket_pin(n, "fact")),
    ("unpocket", lambda s, n: s.pocket_unpin(n, 1)),
)


@pytest.mark.parametrize(("name", "act"), ACTIONS, ids=[a[0] for a in ACTIONS])
def test_connections_cannot_use_lifecycle_actions(hub, env, name, act):
    _token, agent, _person, _cookie, owner = env
    done = done_card(owner)
    before = counts(owner)
    with pytest.raises(ToolError, match="only a signed-in person"):
        act(service(hub, agent), done["number"])
    assert counts(owner) == before
    assert owner.task_show(done["number"])["can_archive"] is True
    shown = service(hub, agent).task_show(done["number"])
    assert not any(shown[k] for k in ("can_archive", "can_restore", "can_follow_up", "can_pocket"))


def test_member_can_archive_restore_carry_on_and_pin(env, member):
    *_, owner = env
    person, _cookie = member
    done = done_card(owner)
    assert person.task_archive(done["number"])["archived_by"] == person.principal.actor
    assert person.task_restore(done["number"])["archived"] is False
    assert person.task_follow_up(done["number"])["card"]["follow_up_of"] == done["number"]
    assert person.pocket_pin(done["number"], "Member fact")["pocket"][0]["text"] == "Member fact"


def test_rest_routes_for_owner_and_member(hub, env, member):
    _token, _agent, _person, cookie, owner = env
    _person_service, member_cookie = member
    done = done_card(owner)
    n = done["number"]
    status, card = api(hub, cookie, "POST", f"/api/cards/{n}/archive", {"version": done["version"]})
    assert status == 200 and card["archived"] is True
    status, listed = api(hub, cookie, "GET", "/api/boards/work/cards")
    assert status == 200 and n not in numbers(listed["cards"])
    status, listed = api(hub, cookie, "GET", "/api/boards/work/cards?archived=1")
    assert status == 200 and numbers(listed["cards"]) == [n]
    assert api(hub, member_cookie, "GET", f"/api/cards/{n}")[1]["archived"] is True
    status, card = api(hub, member_cookie, "POST", f"/api/cards/{n}/restore", {})
    assert status == 200 and card["archived"] is False
    status, made = api(hub, member_cookie, "POST", f"/api/cards/{n}/follow-up", {"title": "Next"})
    assert status == 201 and made["card"]["follow_up_of"] == n
    status, card = api(hub, cookie, "POST", f"/api/cards/{n}/pocket", {"text": "Fact"})
    assert status == 200
    pin = card["pocket"][0]["id"]
    status, card = api(hub, cookie, "POST", f"/api/cards/{n}/unpocket", {"pin": pin})
    assert status == 200 and card["pocket"] == []


def test_rest_refuses_connections_and_anonymous_callers(hub, env):
    token, _agent, _person, _cookie, owner = env
    done = done_card(owner)
    n = done["number"]
    for action, body in (
        ("archive", {}),
        ("restore", {}),
        ("follow-up", {}),
        ("pocket", {"text": "x"}),
        ("unpocket", {"pin": 1}),
    ):
        status, _ = api(hub, token, "POST", f"/api/cards/{n}/{action}", body)
        assert status == 403, action
        status, _ = api(hub, None, "POST", f"/api/cards/{n}/{action}", body)
        assert status in (401, 403), action
    assert owner.task_show(n)["archived"] is False and owner.task_show(n)["pocket"] == []
    assert counts(owner)["card_archive"] == 0
