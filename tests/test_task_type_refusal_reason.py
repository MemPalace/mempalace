"""A refused typed pickup says which type was refused and who can change it."""

import pytest

from commonplace.extensions import ToolError
from tests.test_commonplace_boards import api, env as env, hub_fixture as hub_fixture, service
from tests.test_commonplace_task_types import configure, create


def reason(call):
    with pytest.raises(ToolError) as caught:
        call()
    return str(caught.value)


def test_matching_agent_takes_and_other_type_is_refused_with_reason(hub, env):
    _, agent, _, cookie, _ = env
    configure(hub, cookie, agent, ["code"])
    worker = service(hub, agent)
    mine = create(hub, cookie, task_type="code")
    other = create(hub, cookie, task_type="design")
    assert worker.task_take_next(task=mine["number"])["assignee"] == agent.actor
    message = reason(lambda: worker.task_take_next(task=other["number"]))
    assert "'design'" in message and "Settings > Agents" in message
    assert worker.task_show(other["number"])["assignee"] is None


def test_refusal_covers_start_by_status_and_names_the_type(hub, env):
    _, agent, _, cookie, _ = env
    worker = service(hub, agent)
    typed = create(hub, cookie, task_type="review")
    message = reason(lambda: worker.task_update(typed["number"], status="doing"))
    assert "'review'" in message and "Settings > Agents" in message


def test_automatic_take_skips_other_types_and_untyped_cards_are_unchanged(hub, env):
    _, agent, _, cookie, _ = env
    configure(hub, cookie, agent, ["code"])
    worker = service(hub, agent)
    create(hub, cookie, task_type="design")
    plain = create(hub, cookie)
    assert worker.task_take_next(board="work")["number"] == plain["number"]


def test_owner_sets_and_clears_the_type_over_rest(hub, env):
    _, _, _, cookie, _ = env
    card = create(hub, cookie)
    status, typed = api(
        hub, cookie, "PATCH", f"/api/cards/{card['number']}",
        {"version": card["version"], "task_type": "code"},
    )  # fmt: skip
    assert status == 200 and typed["task_type"] == "code" and typed["can_prioritize"] is True
    status, cleared = api(
        hub, cookie, "PATCH", f"/api/cards/{card['number']}",
        {"version": typed["version"], "task_type": ""},
    )  # fmt: skip
    assert status == 200 and cleared["task_type"] == ""
