"""A7–A8: per-project model storage and resolver contracts."""

import pytest

from commonplace import sessions
from tests._console_helpers import optional
from tests._projects_helpers import projects_hub_fixture  # noqa: F401

CHOICES = {
    "main": {"provider": "claude", "model": "claude-haiku-5-5"},
    "worker": {"provider": "codex", "model": "gpt-5.5"},
}


def test_owner_sets_models_per_project(projects_hub):
    h = projects_hub
    created = h.create_board(h.owner, "alpha")
    status, body = h.models(h.owner, "alpha", CHOICES, declare=True)
    assert status == 200  # RED
    assert created[0] == 201
    assert body["models"] == CHOICES and body["can_configure"] is True
    assert h.project(h.owner, "alpha")[1]["models"] == CHOICES
    models = optional("commonplace.projects.models")
    types = optional("commonplace.projects.types")
    assert models.resolve(h.db, h.workspace, "alpha", "main") == types.ModelChoice(
        **CHOICES["main"]
    )
    assert models.resolve(h.db, h.workspace, "beta", "main") is None
    assert h.get(h.owner, "/api/chat-settings")[1]["front"]["provider"] == "none"
    assert h.models(h.owner, "alpha", {"main": None}, declare=True)[1]["models"] == {
        "main": None,
        "worker": CHOICES["worker"],
    }
    assert h.models(h.owner, "alpha", {"area:ui": CHOICES["main"]}, declare=True)[0] == 400
    invalid = h.models(
        h.owner, "alpha", {"main": {"provider": "claude", "model": "x y"}}, declare=True
    )
    assert invalid[0] == 400 and "x y" not in str(invalid[1])
    invalid_provider = h.models(
        h.owner, "alpha", {"main": {"provider": "bad provider", "model": "valid"}}, declare=True
    )
    assert invalid_provider[0] == 400 and "bad provider" not in str(invalid_provider[1])
    assert h.models(h.member, "alpha", CHOICES, declare=True)[0] == 403
    assert h.models(h.a1, "alpha", CHOICES, declare=True)[0] == 403
    with h.db._lock:
        row = h.db._conn.execute(
            "SELECT * FROM chat_model_settings WHERE workspace_id=? AND scope='alpha' AND role='worker'",
            (h.workspace,),
        ).fetchone()
    assert row["enabled"] == 1 and row["base_url"] is None and row["key_ref"] is None
    assert row["updated_by"] == h.admin and row["updated_at"]


def test_models_are_project_scoped(projects_hub):
    h = projects_hub
    created = [h.create_board(h.owner, slug) for slug in ("alpha", "beta")]
    status, _ = h.models(h.owner, "alpha", {"main": CHOICES["main"]}, declare=True)
    assert status == 200  # RED
    assert all(r[0] == 201 for r in created)
    assert h.project(h.owner, "beta")[1]["models"] == {"main": None, "worker": None}
    assert h.models(h.owner, "beta", {"main": CHOICES["worker"]}, declare=True)[0] == 200
    assert h.project(h.owner, "alpha")[1]["models"]["main"] == CHOICES["main"]
    assert h.project(h.owner, "beta")[1]["models"]["main"] == CHOICES["worker"]
    models = optional("commonplace.projects.models")
    assert models.resolve(h.db, h.other_workspace, "alpha", "main") is None
    assert h.project(h.other, "alpha")[0] == 404


def test_404_unknown_model_cannot_be_saved_until_it_is_in_the_workspace_list(projects_hub):
    h = projects_hub
    h.create_board(h.owner, "alpha")
    status, body = h.models(h.owner, "alpha", {"main": CHOICES["main"]}, declare=False)
    assert (status, body.get("code")) == (400, "unknown_model")
    assert h.project(h.owner, "alpha")[1]["models"] == {"main": None, "worker": None}
    assert h.project(h.owner, "alpha")[1]["model_catalog"] == []
    # An owner adds it; members and agents cannot.
    assert h.catalog_add(h.member, "alpha", CHOICES["main"])[0] == 403
    assert h.catalog_add(h.a1, "alpha", CHOICES["main"])[0] == 403
    assert h.catalog_add(h.owner, "alpha", {"provider": "claude", "model": "x y"})[0] == 400
    # The model-level guard holds without the route: a member principal is refused.
    models = optional("commonplace.projects.models")
    types = optional("commonplace.projects.types")
    member = sessions.person_principal(h.db, h.member_user, h.workspace)
    with pytest.raises(types.ProjectDenied):
        models.catalog_add(h.db, member, "alpha", CHOICES["main"])
    assert models.catalog(h.db, h.workspace, "alpha") == []
    status, view = h.catalog_add(h.owner, "alpha", CHOICES["main"])
    assert status == 200 and view["model_catalog"] == [CHOICES["main"]]
    assert h.catalog_add(h.owner, "alpha", CHOICES["main"])[1]["model_catalog"] == [CHOICES["main"]]
    assert h.models(h.owner, "alpha", {"main": CHOICES["main"]}, declare=False)[0] == 200
    # The list outlives the choice, and a different id is still refused.
    assert h.models(h.owner, "alpha", {"main": None}, declare=False)[0] == 200
    assert h.models(h.owner, "alpha", {"main": CHOICES["main"]}, declare=False)[0] == 200
    other = {"provider": "claude", "model": "claude-other"}
    assert h.models(h.owner, "alpha", {"main": other}, declare=False)[0] == 400
    assert h.project(h.owner, "alpha")[1]["models"]["main"] == CHOICES["main"]
    # Another workspace never sees or uses this list.
    assert h.project(h.other, "alpha")[0] == 404
    assert models.catalog(h.db, h.other_workspace, "alpha") == []


def test_404_writes_are_owner_only_at_the_model_and_the_route(projects_hub):
    h = projects_hub
    h.create_board(h.owner, "alpha")
    models = optional("commonplace.projects.models")
    types = optional("commonplace.projects.types")
    member = sessions.person_principal(h.db, h.member_user, h.workspace)
    # The model layer refuses a member without the route in front of it. The model is listed
    # first, so the owner check is the only refusal left.
    assert h.catalog_add(h.owner, "alpha", CHOICES["main"])[0] == 200
    with pytest.raises(types.ProjectDenied):
        models.put(h.db, member, "alpha", {"main": CHOICES["main"]})
    assert models.get(h.db, h.workspace, "alpha") == {"main": None, "worker": None}
    # The route refuses a member for every write, before any module runs.
    status, _, _ = h.request(h.member, "PUT", "/api/projects/alpha/limits", {"max_agents": 1})
    assert status == 403
    assert h.project(h.owner, "alpha")[1]["limits"].get("max_agents") != 1


def test_404_model_list_is_board_scoped_and_fails_closed(projects_hub):
    h = projects_hub
    h.create_board(h.owner, "alpha")
    h.create_board(h.owner, "beta")
    beta_only = {"provider": "codex", "model": "beta-private-model"}
    assert h.models(h.owner, "beta", {"main": beta_only}, declare=True)[0] == 200
    own = {"provider": "claude", "model": "alpha-model"}
    assert h.catalog_add(h.owner, "alpha", own)[0] == 200
    for reader in (h.owner, h.member):
        status, view = h.project(reader, "alpha")[0], h.project(reader, "alpha")[1]
        assert status == 200 and view["model_catalog"] == [own], reader
    # The other board's model is not a valid choice here either.
    status, body = h.models(h.owner, "alpha", {"main": beta_only}, declare=False)
    assert (status, body.get("code")) == (400, "unknown_model")
    # Workspace defaults (scope '*') are shared by every board.
    default = {"provider": "claude", "model": "workspace-default"}
    with h.db._lock:
        h.db._conn.execute(
            "INSERT INTO chat_model_settings (workspace_id, scope, role, enabled, provider, "
            "model, updated_by, updated_at) VALUES (?, '*', 'main', 1, ?, ?, 'x', 'now')",
            (h.workspace, default["provider"], default["model"]),
        )
    assert default in h.project(h.member, "alpha")[1]["model_catalog"]
    assert beta_only not in h.project(h.member, "alpha")[1]["model_catalog"]


def test_404_list_covers_every_model_role(projects_hub):
    h = projects_hub
    h.create_board(h.owner, "alpha")
    models = optional("commonplace.projects.models")
    expected = {}
    for role in ("main", "worker", "reviewer", "gate", "front", "area:ui"):
        entry = {"provider": "claude", "model": f"model-{role.replace(':', '-')}"}
        expected[role] = entry
        with h.db._lock:
            h.db._conn.execute(
                "INSERT INTO chat_model_settings (workspace_id, scope, role, enabled, provider, "
                "model, updated_by, updated_at) VALUES (?, 'alpha', ?, 1, ?, ?, 'x', 'now')",
                (h.workspace, role, entry["provider"], entry["model"]),
            )
    listed = {item.model for item in models.catalog(h.db, h.workspace, "alpha")}
    assert listed == {expected[r]["model"] for r in ("main", "worker", "reviewer", "gate")}


def test_404_models_added_by_a_save_are_audited(projects_hub):
    h = projects_hub
    h.create_board(h.owner, "alpha")

    def audited():
        with h.db._lock:
            rows = h.db._conn.execute(
                "SELECT action FROM audit_log WHERE workspace_id=? AND action='project.model_catalog'",
                (h.workspace,),
            ).fetchall()
        return len(rows)

    def audited_models():
        with h.db._lock:
            rows = h.db._conn.execute(
                "SELECT target_ids FROM audit_log WHERE workspace_id=? "
                "AND action='project.model_catalog' ORDER BY seq",
                (h.workspace,),
            ).fetchall()
        return [row["target_ids"] for row in rows]

    assert audited() == 0
    assert h.models(h.owner, "alpha", {"main": CHOICES["main"]}, declare=True)[0] == 200
    assert audited_models() == ['["alpha", "claude/claude-haiku-5-5"]']
    after_declare = audited()
    assert after_declare == 1  # the explicit add; the save of a listed model adds nothing new
    assert h.models(h.owner, "alpha", {"worker": CHOICES["worker"]}, declare=False)[0] == 400
    # Saving a model the board already lists writes no further catalog event.
    assert h.models(h.owner, "alpha", {"main": CHOICES["main"]}, declare=False)[0] == 200
    assert audited() == after_declare
    # A model that reaches the list only through a save is recorded.
    with h.db._lock:
        h.db._conn.execute(
            "INSERT INTO chat_model_settings (workspace_id, scope, role, enabled, provider, "
            "model, updated_by, updated_at) VALUES (?, 'alpha', 'worker', 1, ?, ?, 'x', 'now')",
            (h.workspace, CHOICES["worker"]["provider"], CHOICES["worker"]["model"]),
        )
    assert h.models(h.owner, "alpha", {"worker": CHOICES["worker"]}, declare=False)[0] == 200
    assert audited() == after_declare + 1
    assert audited_models()[-1] == '["alpha", "codex/gpt-5.5"]'  # board and model, no text


def test_404_unmapped_roles_outside_the_slots_do_not_break_the_report(projects_hub):
    """A start role whose model_role is not a configurable slot (reviewer, gate) is reported
    after the slots, by name, instead of raising from the sort."""
    from types import SimpleNamespace

    h = projects_hub
    h.create_board(h.owner, "alpha")
    models = optional("commonplace.projects.models")
    types = optional("commonplace.projects.types")
    roles = {
        "lead": SimpleNamespace(model_role="main"),
        "checker": SimpleNamespace(model_role="reviewer"),
        "release": SimpleNamespace(model_role="gate"),
        "loose": SimpleNamespace(model_role=None),
    }
    config = SimpleNamespace(roles=roles, start_roles=tuple(roles))
    missing = models.missing_roles(h.db, h.workspace, "alpha", config)
    assert [(item["role"], item["config_roles"]) for item in missing] == [
        ("main", ["lead"]),
        ("gate", ["release"]),
        ("reviewer", ["checker"]),
        (None, ["loose"]),
    ]
    assert h.models(h.owner, "alpha", {"main": CHOICES["main"]}, declare=True)[0] == 200
    assert [item["role"] for item in models.missing_roles(h.db, h.workspace, "alpha", config)] == [
        "gate",
        "reviewer",
        None,
    ]
    # The configurable slots and the list's roles stay consistent for later slices.
    assert set(types.MODEL_ROLES) <= set(types.CATALOG_ROLES)
    assert {"main", "worker", "reviewer", "gate"} <= set(types.CATALOG_ROLES)


def test_404_catalog_add_audits_only_a_row_it_adds(projects_hub):
    h = projects_hub
    h.create_board(h.owner, "alpha")

    def audited():
        with h.db._lock:
            return h.db._conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE workspace_id=? "
                "AND action='project.model_catalog'",
                (h.workspace,),
            ).fetchone()[0]

    assert audited() == 0
    assert h.catalog_add(h.owner, "alpha", CHOICES["main"])[0] == 200
    assert audited() == 1
    assert h.catalog_add(h.owner, "alpha", CHOICES["main"])[0] == 200  # already listed
    assert audited() == 1
    assert h.catalog_add(h.owner, "alpha", CHOICES["worker"])[0] == 200
    assert audited() == 2


def test_404_existing_control_databases_gain_the_model_list_table_with_its_scope(projects_hub):
    """A control database from develop has no project_model_catalog: the normal schema step
    creates it, scope column included (additive; no existing table changes)."""
    from commonplace.chat import settings

    h = projects_hub
    with h.db._lock:
        h.db._conn.execute("DROP TABLE project_model_catalog")
    settings.ensure_schema(h.db)
    with h.db._lock:
        columns = [row[1] for row in h.db._conn.execute("PRAGMA table_info(project_model_catalog)")]
        other = [row[1] for row in h.db._conn.execute("PRAGMA table_info(chat_model_settings)")]
    assert columns == ["workspace_id", "scope", "provider", "model", "added_by", "added_at"]
    assert "scope" in other  # the pre-existing table is untouched
    h.create_board(h.owner, "alpha")
    assert h.catalog_add(h.owner, "alpha", CHOICES["main"])[0] == 200
