"""Owner-only search index progress, without stored content or identifiers."""

FIELDS = ("state", "threads_indexed", "threads_pending", "units_written")


def progress(service, workspace_id):
    status = service.index_status(workspace_id)
    return {key: status[key] for key in FIELDS}


def register(gateway):
    def handle(handler):
        principal = gateway.principal_for(handler)
        if principal is None:
            handler._send_json(401, {"error": "Sign in"})
            return
        if not principal.person or principal.role != "owner":
            handler._send_json(403, {"error": "Workspace owner required"})
            return
        handler._send_json(
            200,
            progress(gateway.handoff.service, principal.workspace_id),
            {"Cache-Control": "no-store"},
        )

    gateway.add_route("GET", "/api/search-index", handle)
