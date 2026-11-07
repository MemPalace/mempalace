"""Workspace-local task labels and read-only acquisition eligibility."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from commonplace.extensions import ToolError
from mempalace.sqlite_crypto import connect

UNSET = object()
SLUG = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")


def validate(value, *, empty=False):
    if not isinstance(value, str) or not (empty and value == "" or SLUG.fullmatch(value)):
        raise ToolError("no: task type must be a lowercase slug of at most 48 characters")
    return value


def owner_value(principal, value):
    if not principal.person or principal.role != "owner":
        raise ToolError("no: only a signed-in workspace owner can set a task type")
    return validate(value, empty=True)


def canonical(values):
    if not isinstance(values, list):
        raise ToolError("no: allowed_types must be a list")
    result = sorted({validate(value) for value in values})
    if len(result) > 128:
        raise ToolError("no: at most 128 distinct task types are allowed")
    return result


def allowed(conn, connection_id):
    """None means unreadable/corrupt; an empty set means no typed grants."""
    try:
        labels = [
            r[0]
            for r in conn.execute(
                "SELECT task_type FROM connection_task_types WHERE connection_id=?",
                (connection_id,),
            )
        ]
        return frozenset(canonical(labels))
    except (sqlite3.Error, ToolError):
        return None


def eligible(conn, label, holder, *, labels=UNSET):
    if label == "":
        return True
    try:
        validate(label)
    except ToolError:
        return False
    if holder is None:
        return False
    if holder.person:
        return True
    if labels is UNSET:
        labels = allowed(conn, holder.connection_id)
    return labels is not None and label in labels


def require(conn, label, holder, *, labels=UNSET):
    if not eligible(conn, label, holder, labels=labels):
        raise ToolError("no: task type is not allowed for the holding connection")


def read_eligible(directory, label, holder):
    """Runner admission reads grants without creating or migrating a store."""
    if label == "":
        return True
    try:
        target = (Path(directory) / "boards.sqlite3").resolve().as_uri() + "?mode=ro"
        with closing(connect(target, uri=True, timeout=0.2)) as conn:
            return eligible(conn, label, holder)
    except (sqlite3.Error, OSError):
        return False
