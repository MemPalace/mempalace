"""#387: default model per role and the escalation rule, as project settings."""

import pytest

from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._projects_helpers import ProjectsHub, projects_hub_fixture  # noqa: F401
from tests.test_orch_runner import BOARD, MODEL, PROVIDER, World, refused

STRONG = {"provider": PROVIDER, "model": "example-strong"}
RULE = {"after_failures": 2, "labels": ["security", "Concurrency"], "model": STRONG}


@pytest.fixture
def world(hub):
    return World(hub)


def put_rule(h, auth, slug="alpha", body=RULE, *, declare=True):
    return h.escalation(auth, slug, body, declare=declare)


# ------------------------------------------------------------ stored and shown


def test_role_defaults_and_rule_are_stored_and_read(projects_hub):
    h = projects_hub
    assert h.create_board(h.owner, "alpha")[0] == 201
    assert h.project(h.owner, "alpha")[1]["escalation"] is None
    cheap = {"provider": PROVIDER, "model": "example-cheap"}
    status, body = h.models(
        h.owner, "alpha", {"worker": cheap, "reviewer": cheap, "gate": STRONG}, declare=True
    )
    assert status == 200
    assert body["models"] == {"main": None, "worker": cheap, "reviewer": cheap, "gate": STRONG}
    assert {"provider": PROVIDER, "model": "example-cheap"} in body["model_catalog"]
    status, body = put_rule(h, h.owner)
    assert status == 200
    expected = {"after_failures": 2, "labels": ["security", "concurrency"], "model": STRONG}
    assert body["escalation"] == expected
    assert h.project(h.owner, "alpha")[1]["escalation"] == expected
    assert h.project(h.member, "alpha")[1]["escalation"] == expected  # visible, not editable
    assert h.project(h.owner, "beta")[0] == 404
    assert put_rule(h, h.owner, body=None)[1]["escalation"] is None
    assert h.project(h.owner, "alpha")[1]["escalation"] is None


def test_rule_is_project_scoped(projects_hub):
    h = projects_hub
    for slug in ("alpha", "beta"):
        h.create_board(h.owner, slug)
    assert put_rule(h, h.owner)[0] == 200
    assert h.project(h.owner, "beta")[1]["escalation"] is None
    assert h.project(h.other, "alpha")[0] == 404


def test_non_owner_cannot_change_the_rule(projects_hub):
    h = projects_hub
    h.create_board(h.owner, "alpha")
    assert put_rule(h, h.member)[0] == 403
    assert put_rule(h, {"Authorization": f"Bearer {h.a1}"})[0] == 403
    assert put_rule(h, {})[0] in (401, 403)
    assert h.project(h.owner, "alpha")[1]["escalation"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"after_failures": 0, "labels": [], "model": STRONG},
        {"after_failures": 11, "labels": [], "model": STRONG},
        {"after_failures": True, "labels": [], "model": STRONG},
        {"after_failures": None, "labels": [], "model": STRONG},  # no trigger at all
        {"after_failures": 2, "labels": ["bad label"], "model": STRONG},
        {"after_failures": 2, "labels": ["x"] * 11, "model": STRONG},
        {"after_failures": 2, "labels": "security", "model": STRONG},
        {"after_failures": 2, "labels": [], "model": {"provider": "p", "model": "x y"}},
        {"after_failures": 2, "labels": [], "model": None},
        {"after_failures": 2, "labels": [], "model": STRONG, "extra": 1},
        {"after_failures": 2, "model": STRONG},
    ],
)
def test_invalid_rule_is_refused_and_stores_nothing(projects_hub, body):
    h = projects_hub
    h.create_board(h.owner, "alpha")
    status, payload = put_rule(h, h.owner, body=body)
    assert status == 400 and "x y" not in str(payload)
    assert h.project(h.owner, "alpha")[1]["escalation"] is None


# ------------------------------------------------------- starts use the setting


def set_rule(world, **overrides):
    if not hasattr(world, "api"):  # the owner's session over the same workspace
        world.api = ProjectsHub(world.hub)
    api = world.api
    assert put_rule(api, api.owner, BOARD, {**RULE, **overrides})[0] == 200


def fail_round(world, card):
    status, job = world.person_start(card)
    assert status == 201
    with world.js.connection() as conn:
        conn.execute("UPDATE runner_jobs SET state='failed' WHERE id=?", (job["job_id"],))


def test_without_a_rule_a_start_keeps_the_default_model(world):
    card = world.card(labels=["security"])
    world.configure()
    for _ in range(3):
        fail_round(world, card)
    status, _ = world.person_start(card)
    assert status == 201
    [*_, last] = world.jobs_for(card)
    assert (last["model_provider"], last["model"]) == (PROVIDER, MODEL)
    assert last.get("model_reason") is None


def set_state(world, job_id, state):
    with world.js.connection() as conn:
        conn.execute("UPDATE runner_jobs SET state=? WHERE id=?", (state, job_id))


def test_start_after_failed_rounds_uses_the_escalation_model(world):
    card = world.card()
    world.configure()
    set_rule(world, labels=[])
    fail_round(world, card)
    _, second = world.person_start(card)  # one failed round: below the rule
    assert world.jobs_for(card)[-1]["model"] == MODEL
    set_state(world, second["job_id"], "cancelled")  # a cancelled round is no failure
    _, third = world.person_start(card)
    assert world.jobs_for(card)[-1]["model"] == MODEL
    set_state(world, third["job_id"], "failed")  # the second failed round
    status, _ = world.person_start(card)
    assert status == 201
    last = world.jobs_for(card)[-1]
    assert (last["model_provider"], last["model"]) == (PROVIDER, "example-strong")
    assert last["model_reason"] == "failed_rounds:2"
    assert world.lease()["modelSelection"] == {"instanceId": PROVIDER, "model": "example-strong"}
    other = world.card()  # the failures belong to the card, not to the project
    world.person_start(other)
    assert world.jobs_for(other)[-1]["model"] == MODEL


def test_label_escalates_on_the_first_start_and_the_run_records_why(world):
    card = world.card(labels=["Security"])
    world.configure()
    set_rule(world)
    lease = world.claim(world.a)
    reply = world.orch_start(world.a, lease, card, "s1")
    assert reply["model"] == {"provider": PROVIDER, "model": "example-strong"}
    assert reply["model_reason"] == "label:security"
    [row] = world.jobs_for(card)
    assert (row["model"], row["model_reason"]) == ("example-strong", "label:security")
    assert world.orch_start(world.a, lease, card, "s1")["model_reason"] == "label:security"


def test_default_model_and_rule_changes_apply_to_the_next_start(world):
    first, second = world.card(), world.card()
    world.configure()
    world.person_start(first)
    world.set_models("example-new", role="worker")
    set_rule(world, labels=["docs"])
    world.person_start(second)
    assert world.jobs_for(first)[-1]["model"] == MODEL
    assert world.jobs_for(second)[-1]["model"] == "example-new"
    set_rule(world, after_failures=None, labels=["docs"])
    docs = world.card(labels=["docs"])
    world.person_start(docs)
    assert world.jobs_for(docs)[-1]["model"] == "example-strong"


def test_escalation_never_turns_an_unmapped_role_into_a_start(world):
    card = world.card(labels=["security"])
    world.configure()
    world.set_models(None, role="worker")
    refused("model_unresolved", world.person_start, card)
    set_rule(world)
    refused("model_unresolved", world.person_start, card)
    assert world.jobs_for(card) == []


# ------------------------------------------------- escalation model is in the list


def test_escalation_model_must_be_in_the_workspace_model_list(projects_hub):
    h = projects_hub
    h.create_board(h.owner, "alpha")
    status, body = put_rule(h, h.owner, declare=False)
    assert (status, body.get("code")) == (400, "unknown_model")
    assert h.project(h.owner, "alpha")[1]["escalation"] is None
    assert h.project(h.owner, "alpha")[1]["model_catalog"] == []
    assert put_rule(h, h.owner)[0] == 200  # declared first: accepted
    other = {**RULE, "model": {"provider": PROVIDER, "model": "example-other"}}
    status, body = put_rule(h, h.owner, body=other, declare=False)
    assert (status, body.get("code")) == (400, "unknown_model")
    assert h.project(h.owner, "alpha")[1]["escalation"]["model"] == STRONG  # unchanged


# ------------------------------------------------- keyed retries replay the stored job


def test_keyed_person_retry_after_a_failed_round_replays_its_job(world):
    card = world.card()
    world.configure()
    set_rule(world, after_failures=1, labels=[])
    status, job = world.person_start(card, request_key="retry-1")
    assert status == 201 and world.jobs_for(card)[-1]["model"] == MODEL
    set_state(world, job["job_id"], "failed")  # now the rule applies to a new start
    assert world.person_start(card, request_key="retry-1") == (200, job)
    assert len(world.jobs_for(card)) == 1  # replayed, not a second job
    status, escalated = world.person_start(card, request_key="retry-2")
    assert status == 201
    assert world.jobs_for(card)[-1]["model"] == "example-strong"
    # The escalated job replays too, even once the rule no longer applies.
    set_state(world, escalated["job_id"], "cancelled")
    set_rule(world, after_failures=5, labels=[])
    assert world.person_start(card, request_key="retry-2") == (200, escalated)
    assert len(world.jobs_for(card)) == 2
    # A changed default model is still a different request, for either stored job.
    world.set_models("example-new", role="worker")
    refused("request_conflict", world.person_start, card, request_key="retry-1")
    refused("request_conflict", world.person_start, card, request_key="retry-2")
    assert len(world.jobs_for(card)) == 2


def test_changed_default_conflicts_for_a_job_that_ran_on_the_escalation_model(world):
    card, labelled = world.card(), world.card(labels=["security"])
    world.configure()
    set_rule(world, after_failures=1)
    status, first = world.person_start(card, request_key="a")
    set_state(world, first["job_id"], "failed")
    status, escalated = world.person_start(card, request_key="b")
    assert status == 201 and world.jobs_for(card)[-1]["model_reason"] == "failed_rounds:1"
    assert world.person_start(card, request_key="b") == (200, escalated)
    world.set_models("example-new", role="worker")  # the owner changes the default
    refused("request_conflict", world.person_start, card, request_key="b")
    # The same once the escalation no longer applies to a new start.
    set_state(world, escalated["job_id"], "cancelled")
    set_rule(world, after_failures=5, labels=[])
    refused("request_conflict", world.person_start, card, request_key="b")
    assert len(world.jobs_for(card)) == 2
    # A label-escalated job: replays after the rule is removed, conflicts after a default change.
    world.set_models(MODEL, role="worker")
    set_rule(world)
    status, tagged = world.person_start(labelled, request_key="c")
    assert world.jobs_for(labelled)[-1]["model_reason"] == "label:security"
    set_rule(world, after_failures=5, labels=["docs"])
    assert world.person_start(labelled, request_key="c") == (200, tagged)
    world.set_models("example-new", role="worker")
    refused("request_conflict", world.person_start, labelled, request_key="c")


def test_orch_step_retry_after_a_failed_round_or_a_new_label_replays(world):
    card, labelled = world.card(), world.card()
    world.configure()
    set_rule(world, after_failures=1, labels=["security"])
    lease = world.claim(world.a)
    first = world.orch_start(world.a, lease, card, "s1")
    assert first["model"]["model"] == MODEL and first["model_reason"] is None
    set_state(world, first["job"]["job_id"], "failed")
    again = world.orch_start(world.a, lease, card, "s1")
    assert again["status"] == 200 and again["job"]["job_id"] == first["job"]["job_id"]
    second = world.orch_start(world.a, lease, labelled, "s2")
    assert second["model"]["model"] == MODEL
    world.update(labelled, labels=["security"])  # the label arrives after the first start
    replay = world.orch_start(world.a, lease, labelled, "s2")
    assert replay["status"] == 200 and replay["job"]["job_id"] == second["job"]["job_id"]
    assert len(world.jobs_for(labelled)) == 1
    # A step whose job escalated replays after the rule is removed.
    third = world.card(labels=["security"])
    escalated = world.orch_start(world.a, lease, third, "s3")
    assert escalated["model_reason"] == "label:security"
    set_rule(world, after_failures=5, labels=["docs"])
    again = world.orch_start(world.a, lease, third, "s3")
    assert again["status"] == 200 and again["model_reason"] == "label:security"
    world.set_models("example-new", role="worker")  # a new default is a different request
    refused("step_conflict", world.orch_start, world.a, lease, card, "s1")
    refused("step_conflict", world.orch_start, world.a, lease, third, "s3")  # escalated step too


# ------------------------------- reviewer and gate gaps are fixable in the app


@pytest.mark.parametrize("role", ["reviewer", "gate"])
def test_reviewer_and_gate_needs_me_items_are_fixable_through_the_models_route(world, role):
    from commonplace.boards.service import BoardService

    def items():
        return BoardService(world.ctx(world.owner)).needs_me().get("model_roles", [])

    world.configure(model_role=role)
    [item] = [i for i in items() if i["role"] == role]
    assert item["action"]["type"] == "project_models" and item["action"]["role"] == role
    api = ProjectsHub(world.hub)
    choice = {"provider": PROVIDER, "model": MODEL}
    assert api.models(api.owner, BOARD, {role: choice}, declare=True)[0] == 200
    assert [i for i in items() if i["role"] == role] == []
    assert api.project(api.owner, BOARD)[1]["models"][role] == choice


# ------------------------------- jobs created before request_base_hash existed


def test_retry_of_a_job_without_a_base_digest_falls_back_to_its_stored_hash(world):
    """A job from before the column existed (NULL) was never escalated: its stored request hash
    is its pre-escalation digest, so a same-arguments retry replays and a changed default
    conflicts, for a keyed person start and for an orchestrator step."""

    def clear_base_hash():
        with world.js.connection() as conn:
            conn.execute("UPDATE runner_jobs SET request_base_hash=NULL")

    card, other = world.card(), world.card()
    world.configure()
    status, job = world.person_start(card, request_key="old-1")
    assert status == 201
    lease = world.claim(world.a)
    first = world.orch_start(world.a, lease, other, "old-s1")
    clear_base_hash()
    assert all(row["request_base_hash"] is None for row in world.all_jobs())
    assert world.person_start(card, request_key="old-1") == (200, job)
    again = world.orch_start(world.a, lease, other, "old-s1")
    assert again["status"] == 200 and again["job"]["job_id"] == first["job"]["job_id"]
    assert len(world.all_jobs()) == 2
    world.set_models("example-new", role="worker")
    refused("request_conflict", world.person_start, card, request_key="old-1")
    refused("step_conflict", world.orch_start, world.a, lease, other, "old-s1")
