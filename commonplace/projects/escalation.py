"""Project escalation rule: a stronger model for hard cards, a cheap default otherwise.

The default model of each role lives in ``projects.models`` (``chat_model_settings``).
This module stores the one rule that swaps it: after ``after_failures`` failed rounds of
a card, or for a card carrying one of ``labels``, the builder and reviewer roles run on
the rule's model. With no rule a start resolves exactly as before.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from commonplace.projects import models
from commonplace.projects.types import (
    ESCALATABLE_ROLES,
    LABEL,
    MAX_ESCALATION_LABELS,
    MODEL,
    PROVIDER,
    ModelChoice,
    ProjectDenied,
)

MAX_FAILURES = 10
_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_model_escalation (
    workspace_id TEXT NOT NULL,
    scope TEXT NOT NULL,                      -- board slug
    after_failures INTEGER CHECK (after_failures BETWEEN 1 AND 10),
    labels TEXT NOT NULL DEFAULT '[]',        -- JSON list of card labels
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, scope)
);
"""


@dataclass(frozen=True)
class Rule:
    after_failures: int | None
    labels: tuple[str, ...]
    model: ModelChoice

    def public(self) -> dict:
        return {
            "after_failures": self.after_failures,
            "labels": list(self.labels),
            "model": self.model.public(),
        }


def ensure_schema(db) -> None:
    with db._lock:
        db._conn.executescript(_SCHEMA)


def get(db, workspace_id: str, board: str) -> Rule | None:
    with db._lock:
        if not db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_model_escalation'"
        ).fetchone():
            return None
        row = db._conn.execute(
            "SELECT after_failures, labels, provider, model FROM project_model_escalation "
            "WHERE workspace_id=? AND scope=?",
            (workspace_id, board),
        ).fetchone()
    if row is None:
        return None
    return Rule(
        row["after_failures"],
        tuple(json.loads(row["labels"])),
        ModelChoice(row["provider"], row["model"]),
    )


def _checked(body) -> Rule | None:
    """``None`` clears the rule; anything malformed is a ``ValueError``."""
    if body is None:
        return None
    if not isinstance(body, dict) or set(body) != {"after_failures", "labels", "model"}:
        raise ValueError("Invalid escalation rule")
    after, labels, choice = body["after_failures"], body["labels"], body["model"]
    if after is not None and (type(after) is not int or not 1 <= after <= MAX_FAILURES):
        raise ValueError("Invalid escalation rule")
    if (
        not isinstance(labels, list)
        or len(labels) > MAX_ESCALATION_LABELS
        or any(not isinstance(x, str) or LABEL.fullmatch(x) is None for x in labels)
    ):
        raise ValueError("Invalid escalation rule")
    if after is None and not labels:
        raise ValueError("Invalid escalation rule")
    if (
        not isinstance(choice, dict)
        or set(choice) != {"provider", "model"}
        or not isinstance(choice["provider"], str)
        or PROVIDER.fullmatch(choice["provider"]) is None
        or not isinstance(choice["model"], str)
        or MODEL.fullmatch(choice["model"]) is None
    ):
        raise ValueError("Invalid escalation rule")
    unique = tuple(dict.fromkeys(x.lower() for x in labels))
    return Rule(after, unique, ModelChoice(choice["provider"], choice["model"]))


def put(db, principal, board: str, body) -> Rule | None:
    """Validate an owner's update, then atomically store and audit it."""
    if not principal.person or principal.role != "owner":
        raise ProjectDenied("Only a signed-in workspace owner can change project escalation")
    rule = _checked(body)
    if rule is not None and rule.model not in models.catalog(db, principal.workspace_id, board):
        raise models.UnknownModel("Unknown escalation model")
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db._lock, db._tx() as conn:
        if rule is None:
            conn.execute(
                "DELETE FROM project_model_escalation WHERE workspace_id=? AND scope=?",
                (principal.workspace_id, board),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO project_model_escalation "
                "(workspace_id, scope, after_failures, labels, provider, model, "
                "updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    principal.workspace_id,
                    board,
                    rule.after_failures,
                    json.dumps(list(rule.labels)),
                    rule.model.provider,
                    rule.model.model,
                    principal.user_id,
                    updated_at,
                ),
            )
        db.audit(
            action="project.escalation",
            decision="ok",
            workspace_id=principal.workspace_id,
            user_id=principal.user_id,
            target_ids=(board,),
        )
    return rule


def pick(
    rule: Rule | None, role: str, base: ModelChoice | None, *, failed: int, labels
) -> tuple[ModelChoice | None, str | None]:
    """The model a start runs and why it was escalated (``None`` when it was not)."""
    if rule is None or base is None or role not in ESCALATABLE_ROLES:
        return base, None  # an unmapped role stays unmapped (Needs me), escalation or not
    if rule.after_failures is not None and failed >= rule.after_failures:
        return rule.model, f"failed_rounds:{failed}"
    wanted = {str(x).lower() for x in labels}
    for label in rule.labels:
        if label in wanted:
            return rule.model, f"label:{label}"
    return base, None
