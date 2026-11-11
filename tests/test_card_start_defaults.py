"""Start an agent from the card panel: the owner sends the card alone (372-B).

Everything goes through the real routes with real sessions. The board ``acme`` has an
accepted ``[runner]`` config, the board ``plain`` has none; one machine is enrolled and
attested. Whoever may not start gets a plain refusal and no job is ever created.
"""

import pytest

from commonplace import sessions
from commonplace.runner import registry
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.runner_harness import http
from tests.test_orch_runner import BOARD, PLAIN, World


class Session:
    def __init__(self, world, email):
        self.world = world
        link = sessions.create_login_link(world.db, email, world.ws)
        self.cookie = f"{sessions.COOKIE}={sessions.redeem_login_link(world.db, link)}"

    def call(self, method, path, body=None):
        return http(self.world.hub.port, method, path, cookie=self.cookie, body=body)


@pytest.fixture
def world(hub):
    return World(hub)


@pytest.fixture
def owner(world):
    return Session(world, "owner@example.com")


@pytest.fixture
def member(world):
    email = "member@example.com"
    user = world.db.create_user(email)
    world.db.add_member(world.ws, user, "member", actor=world.hub.admin)
    return Session(world, email)


def options(who, card):
    return who.call("GET", f"/api/card-starts/options?task={card}")


# ---------------------------------------------------------------- the config fills in


def test_owner_starts_a_configured_card_by_sending_only_the_card(world, owner):
    card = world.card()
    world.configure()
    status, body = owner.call("POST", "/api/card-starts", {"task": card})
    assert status == 201, body
    [job] = world.all_jobs()
    assert (job["task_number"], job["project"], job["topic"], job["machine_id"]) == (
        card,
        "demo",
        "worker",
        world.machine_id,
    )
    status, runs = owner.call("GET", f"/api/card-starts?task={card}")
    assert status == 200 and [run["job"] for run in runs] == [body["job_id"]]


def test_owner_may_still_send_values_equal_to_the_config(world, owner):
    card = world.card()
    world.configure()
    sent = {"machine": world.machine_id, "project": "demo", "topic": "worker"}
    status, body = owner.call("POST", "/api/card-starts", {"task": card, **sent})
    assert status == 201, body


@pytest.mark.parametrize(
    "different",
    [{"project": "other"}, {"topic": "reviewer"}, {"machine": "mach_" + "0" * 16}],
)
def test_values_that_differ_from_the_config_are_refused_and_start_nothing(world, owner, different):
    card = world.card()
    world.configure()
    status, body = owner.call("POST", "/api/card-starts", {"task": card, **different})
    assert status in (403, 404), body
    assert body["error"]
    assert world.all_jobs() == []


def test_the_refusal_of_a_second_start_names_its_reason(world, owner):
    card = world.card()
    world.configure()
    assert owner.call("POST", "/api/card-starts", {"task": card})[0] == 201
    status, body = owner.call("POST", "/api/card-starts", {"task": card})
    assert status == 409, body
    assert body["error"].startswith("no: ")
    assert len(world.all_jobs()) == 1


def test_a_board_without_a_config_still_needs_every_value(world, owner):
    card = world.card(board=PLAIN)
    for body in ({"task": card}, {"task": card, "machine": world.machine_id, "project": "demo"}):
        status, payload = owner.call("POST", "/api/card-starts", body)
        assert status == 403 and payload["error"].endswith("is required"), payload
    assert world.all_jobs() == []
    status, payload = owner.call(
        "POST",
        "/api/card-starts",
        {"task": card, "machine": world.machine_id, "project": "demo", "topic": "worker"},
    )
    assert status == 201, payload


# ---------------------------------------------------------------- who may not start


def test_a_member_is_refused_with_a_clear_message_and_no_job(world, member):
    card = world.card()
    world.configure()
    for body in (
        {"task": card},
        {"task": card, "machine": world.machine_id, "project": "demo", "topic": "worker"},
    ):
        status, payload = member.call("POST", "/api/card-starts", body)
        assert status in (403, 404), payload
        assert payload["error"]
    status, payload = options(member, card)
    assert status == 403, (status, payload)
    assert "workspace owner" in payload["error"]
    assert world.all_jobs() == []


def test_an_agent_token_sending_only_the_card_is_sent_to_the_orchestrator_route(world):
    card = world.card()
    world.configure()
    token = world.hub.token(
        "requester",
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wings=["shared"],
        trust="trusted",
    )
    status, payload = http(
        world.hub.port, "POST", "/api/card-starts", token=token, body={"task": card}
    )
    assert status == 403 and payload.get("code") == "use_orch_card_start", payload
    assert world.all_jobs() == []
    status, payload = http(
        world.hub.port, "GET", f"/api/card-starts/options?task={card}", token=token
    )
    assert status == 403, payload
    assert world.all_jobs() == []


def test_an_untrusted_agent_token_cannot_start_or_read_the_options(world):
    card = world.card()
    world.configure()
    token = world.hub.token(
        "plain-agent",
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wings=["shared"],
        trust="standard",
    )
    for method, path, body in (
        ("POST", "/api/card-starts", {"task": card}),
        ("GET", f"/api/card-starts/options?task={card}", None),
    ):
        status, payload = http(world.hub.port, method, path, token=token, body=body)
        assert status in (403, 404), payload
    assert world.all_jobs() == []


def test_options_without_a_card_or_for_an_unknown_card(world, owner):
    assert options(owner, "")[0] == 400
    status, payload = owner.call("GET", "/api/card-starts/options?task=99999")
    assert status == 404, payload


# ---------------------------------------------------------------- what the panel offers


def test_options_on_a_configured_board_name_the_configs_choice(world, owner):
    card = world.card()
    world.configure()
    status, body = options(owner, card)
    assert status == 200, body
    assert body == {
        "configured": True,
        "machine": {"id": world.machine_id, "label": "runner-one"},
        "project": "demo",
        "topic": "worker",
        "refusal": None,
    }


def test_options_on_a_board_without_a_config_list_what_the_machine_allows(world, owner):
    card = world.card(board=PLAIN)
    status, body = options(owner, card)
    assert status == 200, body
    assert body["configured"] is False and body["refusal"] is None
    [machine] = body["machines"]
    assert machine["id"] == world.machine_id
    assert (machine["projects"], machine["topics"]) == (["demo"], ["worker"])
    # The offered choice is one the start really accepts.
    status, started = owner.call(
        "POST",
        "/api/card-starts",
        {"task": card, "machine": machine["id"], "project": "demo", "topic": "worker"},
    )
    assert status == 201, started


def test_options_refuse_plainly_when_no_machine_can_be_used(world, owner):
    card = world.card(board=PLAIN)
    registry.revoke(world.db, world.owner, world.machine_id)
    status, body = options(owner, card)
    assert status == 200
    assert body["configured"] is False and body["machines"] == []
    assert body["refusal"] == "no_machine"


def test_options_on_a_configured_board_report_a_machine_that_is_gone(world, owner):
    card = world.card()
    world.configure()
    registry.revoke(world.db, world.owner, world.machine_id)
    status, body = options(owner, card)
    assert status == 200
    assert body["configured"] is True and body["refusal"] == "machine_unavailable"


def test_options_say_when_the_card_cannot_be_started_at_all(world, owner):
    world.configure()
    for board in (PLAIN, BOARD):
        card = world.card(board=board)
        world.update(card, status="backlog")
        status, body = options(owner, card)
        assert status == 200 and body["refusal"] == "not_todo", body
