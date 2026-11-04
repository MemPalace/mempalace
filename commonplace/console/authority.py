"""Resolve the console's visible agents from the signed-in person's workspace."""

from datetime import datetime, timezone

from commonplace import control_db
from commonplace.console import settings, types


def agents(gateway, principal) -> list[types.AgentView]:
    if not principal.person:
        raise types.ConsoleDenied
    sql = (
        "SELECT c.* FROM connections c "
        "LEFT JOIN runner_connections r ON r.connection_id = c.id "
        "WHERE c.workspace_id = ? AND c.revoked_at IS NULL "
        "AND (r.kind IS NULL OR r.kind = 'worker')"
    )
    params = [principal.workspace_id]
    if principal.role != "owner":
        sql += " AND c.user_id = ?"
        params.append(principal.user_id)
    sql += " ORDER BY c.last_used_at IS NULL, c.last_used_at DESC, c.created_at LIMIT 500"
    with gateway.db._lock:
        rows = gateway.db._conn.execute(sql, params).fetchall()
    now = datetime.now(timezone.utc)
    rows = [
        row
        for row in rows
        if (expires := control_db._parse(row["expires_at"])) is None or expires > now
    ][:200]
    recording = settings.recording_ids(gateway.db, principal.workspace_id)
    return [
        types.AgentView(
            id=row["id"],
            name=row["name"],
            client=row["client"],
            parent_id=row["parent_id"],
            user_id=row["user_id"],
            own=row["user_id"] == principal.user_id,
            recording=(row["id"] in recording) if row["user_id"] == principal.user_id else None,
            last_used_at=row["last_used_at"],
        )
        for row in rows
    ]


def agent(gateway, principal, cid) -> types.AgentView:
    for view in agents(gateway, principal):
        if view.id == cid:
            return view
    raise LookupError("Agent not found")


def status(last_seen: str | None, now: datetime) -> str:
    seen = control_db._parse(last_seen)
    if seen is None:
        return "unused"
    age = (now - seen).total_seconds()
    if age <= 120:
        return "working"
    if age <= 30 * 60:
        return "idle"
    return "quiet"
