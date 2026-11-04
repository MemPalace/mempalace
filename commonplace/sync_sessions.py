"""Person-only listing of the caller's native session groups."""

from . import continuity_read, continuity_schema


def register(ctx):
    continuity_schema.ensure_schema(ctx.control_db)

    def handle(handler):
        principal = ctx.gateway.principal_for(handler)
        if principal is None:
            handler._send_json(401, {"error": "Sign in"})
            return
        if not principal.person:
            handler._send_json(403, {"error": "Sign in to list your sessions"})
            return
        rows = continuity_read.sessions(
            ctx.control_db,
            ctx.store_for(principal.workspace_id),
            principal.workspace_id,
            principal.user_id,
        )
        handler._send_json(200, {"sessions": rows}, {"Cache-Control": "no-store"})

    ctx.gateway.add_route("GET", "/api/sync/sessions", handle)
