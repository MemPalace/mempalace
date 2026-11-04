"""A7–A8: per-project model storage and resolver contracts."""

from tests._console_helpers import optional
from tests._projects_helpers import projects_hub_fixture  # noqa: F401

CHOICES = {
    "main": {"provider": "claude", "model": "claude-haiku-5-5"},
    "worker": {"provider": "codex", "model": "gpt-5.5"},
}


def test_owner_sets_models_per_project(projects_hub):
    h = projects_hub
    created = h.create_board(h.owner, "alpha")
    status, body = h.models(h.owner, "alpha", CHOICES)
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
    assert h.models(h.owner, "alpha", {"main": None})[1]["models"] == {
        "main": None,
        "worker": CHOICES["worker"],
    }
    assert h.models(h.owner, "alpha", {"area:ui": CHOICES["main"]})[0] == 400
    invalid = h.models(h.owner, "alpha", {"main": {"provider": "claude", "model": "x y"}})
    assert invalid[0] == 400 and "x y" not in str(invalid[1])
    invalid_provider = h.models(
        h.owner, "alpha", {"main": {"provider": "bad provider", "model": "valid"}}
    )
    assert invalid_provider[0] == 400 and "bad provider" not in str(invalid_provider[1])
    assert h.models(h.member, "alpha", CHOICES)[0] == 403
    assert h.models(h.a1, "alpha", CHOICES)[0] == 403
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
    status, _ = h.models(h.owner, "alpha", {"main": CHOICES["main"]})
    assert status == 200  # RED
    assert all(r[0] == 201 for r in created)
    assert h.project(h.owner, "beta")[1]["models"] == {"main": None, "worker": None}
    assert h.models(h.owner, "beta", {"main": CHOICES["worker"]})[0] == 200
    assert h.project(h.owner, "alpha")[1]["models"]["main"] == CHOICES["main"]
    assert h.project(h.owner, "beta")[1]["models"]["main"] == CHOICES["worker"]
    models = optional("commonplace.projects.models")
    assert models.resolve(h.db, h.other_workspace, "alpha", "main") is None
    assert h.project(h.other, "alpha")[0] == 404
