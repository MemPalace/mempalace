"""Project model choices and workspace-default resolution."""

from datetime import datetime, timezone

from commonplace.projects.types import MODEL, MODEL_ROLES, PROVIDER, ModelChoice, ProjectDenied


def get(db, workspace_id: str, board: str) -> dict[str, ModelChoice | None]:
    """Return explicit project choices; inherited choices belong to resolve()."""
    choices: dict[str, ModelChoice | None] = dict.fromkeys(MODEL_ROLES)
    with db._lock:
        rows = db._conn.execute(
            "SELECT role, provider, model FROM chat_model_settings "
            "WHERE workspace_id=? AND scope=? AND role IN ('main', 'worker')",
            (workspace_id, board),
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
    for choice in body.values():
        if choice is None:
            continue
        if (
            not isinstance(choice, dict)
            or set(choice) != {"provider", "model"}
            or not isinstance(choice["provider"], str)
            or PROVIDER.fullmatch(choice["provider"]) is None
            or not isinstance(choice["model"], str)
            or MODEL.fullmatch(choice["model"]) is None
        ):
            raise ValueError("Invalid project models")

    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db._lock, db._tx() as conn:
        for role, choice in body.items():
            if choice is None:
                conn.execute(
                    "DELETE FROM chat_model_settings WHERE workspace_id=? AND scope=? AND role=?",
                    (principal.workspace_id, board, role),
                )
            else:
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
