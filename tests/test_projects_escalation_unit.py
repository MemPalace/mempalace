"""#387: the escalation rule's module contracts (pick, the owner guard)."""

import pytest

from commonplace import sessions
from commonplace.projects import escalation, models
from commonplace.projects.types import ProjectDenied
from tests._projects_helpers import projects_hub_fixture  # noqa: F401

PROVIDER, MODEL = "example-provider", "example-model"
STRONG = {"provider": PROVIDER, "model": "example-strong"}
RULE = {"after_failures": 2, "labels": ["security"], "model": STRONG}


def test_module_refuses_a_non_owner_and_a_token_principal(projects_hub):
    h = projects_hub
    h.create_board(h.owner, "alpha")
    gateway = h.hub.gateway
    token = gateway.authenticate(f"Bearer {h.a1}")
    with pytest.raises(ProjectDenied):
        escalation.put(h.db, token, "alpha", RULE)
    member = sessions.person_principal(h.db, h.member_user, h.workspace)
    with pytest.raises(ProjectDenied):
        escalation.put(h.db, member, "alpha", RULE)
    assert escalation.get(h.db, h.workspace, "alpha") is None
    owner = sessions.person_principal(h.db, h.admin, h.workspace)
    with pytest.raises(models.UnknownModel):  # not in the workspace's list yet
        escalation.put(h.db, owner, "alpha", RULE)
    models.catalog_add(h.db, owner, "alpha", STRONG)
    escalation.put(h.db, owner, "alpha", RULE)
    assert escalation.get(h.db, h.workspace, "alpha").after_failures == 2
    assert escalation.get(h.db, h.workspace, "beta") is None
    assert escalation.get(h.db, h.other_workspace, "alpha") is None
    assert models.get(h.db, h.workspace, "alpha")["gate"] is None


def test_pick_escalates_on_failed_rounds_or_label_for_the_builder_and_reviewer_only():
    strong = models.ModelChoice(PROVIDER, "example-strong")
    base = models.ModelChoice(PROVIDER, MODEL)
    rule = escalation.Rule(2, ("security",), strong)
    pick = escalation.pick
    assert pick(rule, "worker", base, failed=1, labels=[]) == (base, None)
    assert pick(rule, "worker", base, failed=2, labels=[]) == (strong, "failed_rounds:2")
    assert pick(rule, "reviewer", base, failed=5, labels=[]) == (strong, "failed_rounds:5")
    assert pick(rule, "worker", base, failed=0, labels=["Security"]) == (strong, "label:security")
    assert pick(rule, "worker", base, failed=0, labels=["docs"]) == (base, None)
    assert pick(rule, "main", base, failed=9, labels=["security"]) == (base, None)
    assert pick(rule, "gate", base, failed=9, labels=["security"]) == (base, None)
    assert pick(None, "worker", base, failed=9, labels=["security"]) == (base, None)
    only_labels = escalation.Rule(None, ("security",), strong)
    assert pick(only_labels, "worker", base, failed=9, labels=[]) == (base, None)
