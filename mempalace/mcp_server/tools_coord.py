# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


# ==================== LOGSTREAM TOOLS (RFC 003) ====================
#
# Agent coordination over the shared hub: durable append-only events plus
# exact artifacts, stored in logstream.sqlite3 in the active palace dir.
# No Chroma dependency and no vector index open — these handlers must never
# call _get_collection().


def tool_event_append(
    type: str,
    stream: str,
    room: str,
    from_agent: str,
    to_agent: str = None,
    correlation_id: str = None,
    branch: str = None,
    base_commit: str = None,
    status: str = None,
    body: str = "",
    metadata: dict = None,
    artifact_ids: list = None,
    topic: str = None,
):
    """Append one immutable coordination event."""
    try:
        event = _call_logstream(
            lambda ls: ls.append_event(
                type=type,
                stream=stream,
                room=room,
                from_agent=from_agent,
                to_agent=to_agent,
                correlation_id=correlation_id,
                branch=branch,
                base_commit=base_commit,
                status=status,
                body=body,
                metadata=metadata,
                artifact_ids=artifact_ids,
                topic=topic,
            )
        )
    except ValueError as e:
        return {"success": False, "error": str(e)}
    return {"success": True, "event": event}


def tool_task_create(
    project: str,
    from_agent: str,
    to_agent: str,
    goal: str,
    branch: str,
    base_commit: str,
    done: str,
):
    """Create one canonical task request for local or remote MCP clients."""
    from ..tasks import create_task

    try:
        result = _call_logstream(
            lambda ls: create_task(
                ls,
                project=project,
                from_agent=from_agent,
                to_agent=to_agent,
                goal=goal,
                branch=branch,
                base_commit=base_commit,
                done=done,
            )
        )
    except ValueError as e:
        return {"success": False, "error": str(e)}
    return {"success": True, **result}


_PREVIEW_BODY_CHARS = 200


def _preview_event(event: dict) -> dict:
    """Truncate a verbatim body to a scannable excerpt (preview mode).

    Event bodies are stored verbatim and fleet status updates run to several
    KB, so listing many events full-body is a large payload. Preview keeps all
    routing/metadata fields and trims only ``body`` — enough to scan the stream
    and decide which events to re-fetch in full. ``since_event_id`` is
    strictly *after* that id, so passing the truncated event's own id
    skips it; repeat the original filters with ``preview=false`` (and
    ``correlation_id`` / ``from_agent`` as needed) instead."""
    body = event.get("body") or ""
    if len(body) <= _PREVIEW_BODY_CHARS:
        return event
    out = dict(event)
    out["body"] = body[:_PREVIEW_BODY_CHARS]
    out["body_truncated"] = True
    out["body_length"] = len(body)
    return out


def tool_event_list(
    stream: str = None,
    room: str = None,
    topic: str = None,
    type: str = None,
    to_agent: str = None,
    from_agent: str = None,
    correlation_id: str = None,
    status: str = None,
    since_event_id: str = None,
    before_event_id: str = None,
    since_created_at: str = None,
    limit: int = 50,
    order: str = "asc",
    preview: bool = False,
):
    """List coordination events with structured filters.

    ``order='desc'`` returns newest events first (e.g. for sweeping recent inbox
    or checking recent project history in a single call). Default is ``'asc'``.
    ``preview=True`` truncates each event's verbatim body to a short excerpt
    (marking ``body_truncated`` + ``body_length``) so scanning many events
    stays cheap.
    """
    try:
        events = _call_logstream(
            lambda ls: ls.list_events(
                stream=stream,
                room=room,
                topic=topic,
                type=type,
                to_agent=to_agent,
                from_agent=from_agent,
                correlation_id=correlation_id,
                status=status,
                since_event_id=since_event_id,
                before_event_id=before_event_id,
                since_created_at=since_created_at,
                limit=limit,
                order=order,
            )
        )
    except ValueError as e:
        return {"error": str(e)}
    if preview:
        events = [_preview_event(e) for e in events]
    return {"events": events, "count": len(events)}


def tool_event_wait(
    stream: str = None,
    room: str = None,
    topic: str = None,
    type: str = None,
    to_agent: str = None,
    from_agent: str = None,
    correlation_id: str = None,
    status: str = None,
    since_event_id: str = None,
    since_created_at: str = None,
    timeout_ms: int = 60000,
    limit: int = 50,
):
    """Block until a matching event exists or the timeout expires.

    ``limit`` mirrors ``event_list`` so the two tools accept the same
    filter set — agents kept tripping over wait rejecting a parameter
    that list accepts (reported by windows-codex during dogfood).
    """
    try:
        result = _call_logstream(
            lambda ls: ls.wait_events(
                timeout_ms=timeout_ms,
                stream=stream,
                room=room,
                topic=topic,
                type=type,
                to_agent=to_agent,
                from_agent=from_agent,
                correlation_id=correlation_id,
                status=status,
                since_event_id=since_event_id,
                since_created_at=since_created_at,
                limit=limit,
            )
        )
    except ValueError as e:
        return {"error": str(e)}
    result["count"] = len(result["events"])
    return result


def tool_event_ack(
    event_id: str,
    from_agent: str,
    status: str = None,
    body: str = "",
    topic: str = None,
):
    """Append an event.ack referencing a prior event (never mutates it)."""
    try:
        event = _call_logstream(
            lambda ls: ls.ack_event(
                event_id, from_agent=from_agent, status=status, body=body, topic=topic
            )
        )
    except ValueError as e:
        return {"success": False, "error": str(e)}
    return {"success": True, "event": event}


def tool_artifact_put(kind: str, content: str, created_by: str, metadata: dict = None):
    """Store exact artifact content (patch, file, log, json, note)."""
    try:
        artifact = _call_logstream(
            lambda ls: ls.put_artifact(
                kind=kind, content=content, created_by=created_by, metadata=metadata
            )
        )
    except ValueError as e:
        return {"success": False, "error": str(e)}
    return {"success": True, "artifact": artifact}


def tool_artifact_get(artifact_id: str):
    """Fetch an artifact by id — exact content and metadata."""
    try:
        artifact = _call_logstream(lambda ls: ls.get_artifact(artifact_id))
    except ValueError as e:
        return {"error": str(e)}
    if artifact is None:
        return {"error": f"artifact {artifact_id!r} not found"}
    return {"artifact": artifact}


def tool_patch_submit(
    content: str,
    from_agent: str,
    stream: str,
    room: str = "patches",
    to_agent: str = None,
    correlation_id: str = None,
    branch: str = None,
    base_commit: str = None,
    body: str = "",
    metadata: dict = None,
    topic: str = None,
):
    """Store a patch artifact and append its patch.ready event in one call."""
    try:
        result = _call_logstream(
            lambda ls: ls.submit_patch(
                content=content,
                from_agent=from_agent,
                stream=stream,
                room=room,
                to_agent=to_agent,
                correlation_id=correlation_id,
                branch=branch,
                base_commit=base_commit,
                body=body,
                metadata=metadata,
                topic=topic,
            )
        )
    except ValueError as e:
        return {"success": False, "error": str(e)}
    return {"success": True, "artifact": result["artifact"], "event": result["event"]}
