"""Project model choices and workspace-default resolution."""

from datetime import datetime, timezone

from commonplace.projects.types import (
    CATALOG_ROLES,
    MODEL,
    MODEL_ROLES,
    PROVIDER,
    ModelChoice,
    ProjectDenied,
)


class UnknownModel(ValueError):
    """A choice names a model that is not in the workspace's list."""


def _shape(choice) -> ModelChoice:
    if (
        not isinstance(choice, dict)
        or set(choice) != {"provider", "model"}
        or not isinstance(choice["provider"], str)
        or PROVIDER.fullmatch(choice["provider"]) is None
        or not isinstance(choice["model"], str)
        or MODEL.fullmatch(choice["model"]) is None
    ):
        raise ValueError("Invalid project models")
    return ModelChoice(choice["provider"], choice["model"])


def catalog(db, workspace_id: str, board: str) -> list[ModelChoice]:
    """The models a project can pick: the board's own list and choices, and workspace defaults.

    Rows of other boards never appear, so a reader of one project sees no other project's labels."""
    marks = ", ".join("?" for _ in CATALOG_ROLES)
    with db._lock:
        rows = db._conn.execute(
            "SELECT provider, model FROM project_model_catalog "
            "WHERE workspace_id=? AND scope IN (?, '*') "
            "UNION SELECT provider, model FROM chat_model_settings "
            f"WHERE workspace_id=? AND scope IN (?, '*') AND role IN ({marks}) "
            "AND provider!='none' AND model!='' ORDER BY provider, model",
            (workspace_id, board, workspace_id, board, *CATALOG_ROLES),
        ).fetchall()
    return [ModelChoice(row["provider"], row["model"]) for row in rows]


def catalog_add(db, principal, board: str, choice: dict) -> list[ModelChoice]:
    """An owner adds one model to a project list (validated, audited, idempotent)."""
    if not principal.person or principal.role != "owner":
        raise ProjectDenied("Only a signed-in workspace owner can change project models")
    entry = _shape(choice)
    added_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db._lock, db._tx() as conn:
        added = conn.execute(
            "INSERT OR IGNORE INTO project_model_catalog "
            "(workspace_id, scope, provider, model, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                principal.workspace_id,
                board,
                entry.provider,
                entry.model,
                principal.user_id,
                added_at,
            ),
        )
        if added.rowcount:
            db.audit(
                action="project.model_catalog",
                decision="ok",
                workspace_id=principal.workspace_id,
                user_id=principal.user_id,
                target_ids=(board, f"{entry.provider}/{entry.model}"),
            )
    return catalog(db, principal.workspace_id, board)


def missing_roles(db, workspace_id: str, board: str, config) -> list[dict]:
    """Roles a start of this config runs that cannot get a model, in slot order.

    One entry per model role: ``role`` (``None`` when a config role names no ``model_role``,
    which no setting can fix) and the ``config_roles`` that use it. The roles are the ones
    ``prepare()`` selects, so Needs me and a refused start agree."""
    grouped: dict = {}
    for name in config.start_roles:
        grouped.setdefault(config.roles[name].model_role, []).append(name)
    missing = [
        {"role": role, "config_roles": names}
        for role, names in grouped.items()
        if role is None or _unresolved(db, workspace_id, board, role)
    ]
    return sorted(missing, key=lambda item: _slot_order(item["role"]))


def _unresolved(db, workspace_id: str, board: str, role: str) -> bool:
    """A role outside the configurable slots has no model to resolve: it counts as unset."""
    try:
        return resolve(db, workspace_id, board, role) is None
    except ValueError:
        return True


def _slot_order(role):
    """Slot order first, any other role after it by name, a missing model_role last."""
    if role is None:
        return (len(MODEL_ROLES) + 1, "")
    return (MODEL_ROLES.index(role) if role in MODEL_ROLES else len(MODEL_ROLES), role)


def get(db, workspace_id: str, board: str) -> dict[str, ModelChoice | None]:
    """Return explicit project choices; inherited choices belong to resolve()."""
    choices: dict[str, ModelChoice | None] = dict.fromkeys(MODEL_ROLES)
    with db._lock:
        rows = db._conn.execute(
            "SELECT role, provider, model FROM chat_model_settings "
            f"WHERE workspace_id=? AND scope=? AND role IN ({', '.join('?' for _ in MODEL_ROLES)})",
            (workspace_id, board, *MODEL_ROLES),
        ).fetchall()
    for row in rows:
        choices[row["role"]] = ModelChoice(row["provider"], row["model"])
    return choices


def put(db, principal, board: str, body: dict) -> dict[str, ModelChoice | None]:
    """Validate an owner's partial update, then atomically store and audit it."""
    if not principal.person or principal.role != "owner":
        raise ProjectDenied("Only a signed-in workspace owner can change project models")
    if not isinstance(body, dict) or set(body) - set(MODEL_ROLES):
        raise ValueError("Invalid project models")
    known = None
    for choice in body.values():
        if choice is None:
            continue
        entry = _shape(choice)
        if known is None:
            known = set(catalog(db, principal.workspace_id, board))
        if entry not in known:
            raise UnknownModel("Unknown project model")

    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db._lock, db._tx() as conn:
        for role, choice in body.items():
            if choice is None:
                conn.execute(
                    "DELETE FROM chat_model_settings WHERE workspace_id=? AND scope=? AND role=?",
                    (principal.workspace_id, board, role),
                )
            else:
                added = conn.execute(
                    "INSERT OR IGNORE INTO project_model_catalog "
                    "(workspace_id, scope, provider, model, added_by, added_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        principal.workspace_id,
                        board,
                        choice["provider"],
                        choice["model"],
                        principal.user_id,
                        updated_at,
                    ),
                )
                if added.rowcount:
                    db.audit(
                        action="project.model_catalog",
                        decision="ok",
                        workspace_id=principal.workspace_id,
                        user_id=principal.user_id,
                        target_ids=(board, f"{choice['provider']}/{choice['model']}"),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO chat_model_settings "
                    "(workspace_id, scope, role, enabled, provider, model, base_url, key_ref, "
                    "updated_by, updated_at) VALUES (?, ?, ?, 1, ?, ?, NULL, NULL, ?, ?)",
                    (
                        principal.workspace_id,
                        board,
                        role,
                        choice["provider"],
                        choice["model"],
                        principal.user_id,
                        updated_at,
                    ),
                )
        db.audit(
            action="project.models",
            decision="ok",
            workspace_id=principal.workspace_id,
            user_id=principal.user_id,
            target_ids=(board,),
        )
        return get(db, principal.workspace_id, board)


def resolve(db, workspace_id: str, board: str, role: str) -> ModelChoice | None:
    """Use the project choice, then the workspace choice, then the runner default."""
    if role not in MODEL_ROLES:
        raise ValueError("Invalid project model role")
    with db._lock:
        row = db._conn.execute(
            "SELECT provider, model FROM chat_model_settings "
            "WHERE workspace_id=? AND scope IN (?, '*') AND role=? "
            "ORDER BY CASE WHEN scope=? THEN 0 ELSE 1 END LIMIT 1",
            (workspace_id, board, role, board),
        ).fetchone()
    return ModelChoice(row["provider"], row["model"]) if row is not None else None
