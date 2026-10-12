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


# ``from_agent`` means "who I am" on every other coordination call (append, ack, artifacts), and agents are told to
# pass their identity on every call. On list/wait it used to filter by WRITER, so an agent following that rule and
# asking for its task by correlation id saw only its own events and missed the request written to it (2026-09-29:
# windows:codex:blender got 0 events for a task.request from windows:claude:blender). The writer filter is now the
# explicit ``writer``; ``from_agent`` on list/wait is the caller's identity and never narrows the result.
_FROM_AGENT_NOTE = (
    "from_agent is your identity and does not filter event_list/event_wait. "
    "Filter by who wrote an event with writer=<agent>; for your inbox use to_agent=<you>."
)


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
    order: str = None,
    preview: bool = False,
    writer: str = None,
):
    """List coordination events with structured filters.

    Context-aware ordering default:
    When ``order`` is omitted or None:
    - If ``since_event_id`` is supplied (resuming from a cursor), defaults to ``'asc'``
      so events replay in forward chronological append order without skipping.
    - Otherwise (inbox sweeps, history checks), defaults to ``'desc'`` so the
      newest events are returned first rather than ancient events from far back.

    Explicit ``order='asc'`` or ``order='desc'`` (case-insensitive) always overrides.
    ``preview=True`` truncates each event's verbatim body to a short excerpt
    (marking ``body_truncated`` + ``body_length``) so scanning many events
    stays cheap.

    ``writer`` filters by the agent that wrote the event. ``from_agent`` is the
    caller's identity and does not filter (see ``_FROM_AGENT_NOTE``).
    """
    if order is None:
        resolved_order = "asc" if since_event_id else "desc"
    else:
        resolved_order = str(order).lower().strip()

    try:
        events = _call_logstream(
            lambda ls: ls.list_events(
                stream=stream,
                room=room,
                topic=topic,
                type=type,
                to_agent=to_agent,
                from_agent=writer,
                correlation_id=correlation_id,
                status=status,
                since_event_id=since_event_id,
                before_event_id=before_event_id,
                since_created_at=since_created_at,
                limit=limit,
                order=resolved_order,
            )
        )
    except ValueError as e:
        return {"error": str(e)}
    if preview:
        events = [_preview_event(e) for e in events]
    result = {"events": events, "count": len(events)}
    if from_agent and not writer:
        result["note"] = _FROM_AGENT_NOTE
    return result


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
    writer: str = None,
):
    """Block until a matching event exists or the timeout expires.

    ``limit`` mirrors ``event_list`` so the two tools accept the same
    filter set — agents kept tripping over wait rejecting a parameter
    that list accepts (reported by windows-codex during dogfood).
    ``writer`` filters by the agent that wrote the event; ``from_agent`` is the
    caller's identity and does not filter (see ``_FROM_AGENT_NOTE``).
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
                from_agent=writer,
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
    if from_agent and not writer:
        result["note"] = _FROM_AGENT_NOTE
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


# ==================== AGENT ROOMS (RFC 006) ====================
#
# Free-form discussion between agents, with the operator moderating. Open,
# read and say touch only logstream.sqlite3; close also files the transcript
# into the palace, so it is the one room tool that reaches Chroma.


def tool_room_open(project: str, from_agent: str, name: str, agenda: str = ""):
    """Open a room and return its id plus the line that brings agents in."""
    from ..agent_rooms import open_room

    try:
        result = _call_logstream(
            lambda ls: open_room(
                ls, project=project, from_agent=from_agent, name=name, agenda=agenda
            )
        )
    except ValueError as e:
        return {"success": False, "error": str(e)}
    return {"success": True, **result}


def tool_room_read(room_id: str, agent: str, limit: int = 50):
    """Everything in a room this agent has not read yet; the hub keeps its place."""
    from ..agent_rooms import read_room

    try:
        result = _call_logstream(
            lambda ls: read_room(ls, room_id=room_id, agent=agent, limit=limit)
        )
    except ValueError as e:
        return {"error": str(e)}
    result["count"] = len(result["events"])
    return result


def tool_room_say(room_id: str, from_agent: str, body: str, to_agent: str = None):
    """Post one message to a room, or to one agent in it."""
    from ..agent_rooms import say_in_room

    try:
        result = _call_logstream(
            lambda ls: say_in_room(
                ls, room_id=room_id, from_agent=from_agent, body=body, to_agent=to_agent
            )
        )
    except ValueError as e:
        return {"success": False, "error": str(e)}
    return {"success": True, **result}


def tool_room_close(room_id: str, from_agent: str, outcome: str = ""):
    """Close a room and file its transcript verbatim, one drawer per turn.

    Closing again re-files without appending a second close: drawer ids
    derive from content, and every drawer carries its event id, so a retry
    after a partial failure files only what is missing.
    """
    from ..agent_rooms import close_room, transcript_drawers

    try:
        result = _call_logstream(
            lambda ls: close_room(ls, room_id=room_id, from_agent=from_agent, outcome=outcome)
        )
    except ValueError as e:
        return {"success": False, "error": str(e)}

    room = result["room"]
    filed, already_filed, errors = 0, 0, []
    for content in transcript_drawers(result["transcript"]):
        filing = tool_add_drawer(
            wing=room["wing"],
            room=room["name"],
            content=content,
            source_file=room["room_id"],
            added_by=from_agent,
        )
        if not filing.get("success"):
            errors.append(filing.get("error") or "unknown error")
        elif filing.get("reason") == "already_exists":
            already_filed += 1
        else:
            filed += 1

    response = {
        "success": not errors,
        "room": room,
        "event": result["event"],
        "already_closed": result["already_closed"],
        "filed": filed,
        "already_filed": already_filed,
        "wing": room["wing"],
        "palace_room": room["name"],
    }
    if errors:
        response["errors"] = errors
        response["error"] = (
            f"room closed, but {len(errors)} transcript drawer(s) were not filed; "
            "call mempalace_room_close again to retry"
        )
    return response
