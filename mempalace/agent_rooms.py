"""Agent rooms (RFC 006): free-form discussion between agents on the logstream.

A room is one correlation on the project stream. Opening it appends
``room.open``; every turn is a ``room.message``; closing appends
``room.close``. The hub keeps each agent's read position
(:meth:`Logstream.read_correlation`), so a participant reads, maybe speaks,
and never carries a cursor, pages by hand, or learns an event vocabulary.

Not to be confused with palace rooms (``mempalace/rooms.py``). The two meet
on purpose when a room closes: its transcript is filed into the palace room
of the same name.
"""

import inspect
import re
import secrets

from .config import sanitize_content
from .logstream import EventConflict
from .tasks import task_slug

ROOMS_LOGSTREAM_ROOM = "rooms"
ROOM_OPEN = "room.open"
ROOM_MESSAGE = "room.message"
ROOM_CLOSE = "room.close"
# Event types whose bodies make up the transcript filed on close.
TRANSCRIPT_TYPES = (ROOM_OPEN, ROOM_MESSAGE, ROOM_CLOSE)

_TRANSCRIPT_PAGE = 500

# Every turn is filed as one drawer on close, and a drawer holds at most
# ``sanitize_content``'s limit. A turn that cannot be filed must be refused
# when it is posted: once stored, it would fail every close. The margin
# covers the locator line (two routing fields of up to 256 characters each,
# the event id, the type and the timestamp).
_DRAWER_CONTENT_LIMIT = inspect.signature(sanitize_content).parameters["max_length"].default
MAX_ROOM_BODY_CHARS = _DRAWER_CONTENT_LIMIT - 1_000


def room_slug(name: str) -> str:
    """Short kebab-case room name, valid both as a routing value and as a palace room."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("room name must not be empty")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:40].rstrip("-") or "room"


def room_handoff(room_id: str) -> str:
    """The one line an operator pastes into an agent's chat to bring it in."""
    # Names both server shapes: the line is pasted into chats the opener
    # cannot see, some on the full server and some on the light one.
    return (
        f"Join MemPalace room {room_id}: read it as your agent identity "
        "(mempalace_room_read, or palace_coordinate ROOM READ), then post "
        "(mempalace_room_say, or ROOM SAY) only if you add something new."
    )


def _check_room_id(room_id) -> str:
    if not isinstance(room_id, str) or not room_id.strip().startswith("room_"):
        raise ValueError(f"room_id={room_id!r} is not a room id (expected room_<name>_<hex>)")
    return room_id.strip()


def _check_body(body, what: str, required: bool) -> str:
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise ValueError(f"room {what} must be a string")
    if required and not body.strip():
        raise ValueError(f"room {what} must not be empty")
    if len(body) > MAX_ROOM_BODY_CHARS:
        raise ValueError(
            f"room {what} is {len(body)} characters; a room turn holds at most "
            f"{MAX_ROOM_BODY_CHARS} so it can be filed as one drawer"
        )
    return body


def _canonical_close(logstream, room_id: str):
    """The room's close, or None. With replicas, two closes can exist; the
    earliest by HLC is the one every replica agrees on."""
    closes = logstream.list_events(correlation_id=room_id, type=ROOM_CLOSE, limit=500)
    return min(closes, key=lambda e: e["hlc"]) if closes else None


def _compact(event: dict) -> dict:
    """The fields a participant needs; routing noise stays in the logstream."""
    return {
        "id": event["id"],
        "type": event["type"],
        "from_agent": event["from_agent"],
        "to_agent": event["to_agent"],
        "body": event["body"],
        "created_at": event["created_at"],
    }


def get_room(logstream, room_id: str) -> dict:
    """The room's state, derived from its open and close events."""
    room_id = _check_room_id(room_id)
    opened = logstream.list_events(correlation_id=room_id, type=ROOM_OPEN, limit=1, order="asc")
    if not opened:
        raise ValueError(f"room {room_id!r} not found")
    open_event = opened[0]
    close = _canonical_close(logstream, room_id)
    meta = open_event["metadata"]
    room = {
        "room_id": room_id,
        "name": meta.get("name") or room_slug(room_id),
        "project": meta.get("project"),
        "wing": meta.get("wing"),
        "stream": open_event["stream"],
        "opened_by": open_event["from_agent"],
        "opened_at": open_event["created_at"],
        "agenda": open_event["body"],
        "closed": close is not None,
    }
    if close is not None:
        room["closed_by"] = close["from_agent"]
        room["closed_at"] = close["created_at"]
        room["close_event_id"] = close["id"]
    return room


def open_room(logstream, *, project: str, from_agent: str, name: str, agenda: str = "") -> dict:
    """Open a room and return it with the handoff line for the participants."""
    if not isinstance(project, str) or not project.strip():
        raise ValueError("room project must not be empty")
    slug = room_slug(name)
    agenda = _check_body(agenda, "agenda", required=False)
    project_slug = task_slug(project, fallback="project")
    room_id = f"room_{slug}_{secrets.token_hex(4)}"
    event = logstream.append_event(
        type=ROOM_OPEN,
        stream=f"project/{project_slug}",
        room=ROOMS_LOGSTREAM_ROOM,
        from_agent=from_agent,
        to_agent="*",
        correlation_id=room_id,
        body=agenda,
        metadata={"name": slug, "project": project_slug, "wing": project_slug},
    )
    return {
        "room": get_room(logstream, room_id),
        "event": _compact(event),
        "handoff": room_handoff(room_id),
    }


def read_room(logstream, *, room_id: str, agent: str, limit: int = 50) -> dict:
    """What ``agent`` has not read yet, oldest first; the hub keeps its place."""
    room = get_room(logstream, room_id)
    page = logstream.read_correlation(room["room_id"], agent, limit=limit)
    return {
        "room": room,
        "events": [_compact(e) for e in page["events"]],
        "more": page["more"],
    }


def say_in_room(
    logstream, *, room_id: str, from_agent: str, body: str, to_agent: str = None
) -> dict:
    """Post one message; reports how much from others the speaker has not read."""
    body = _check_body(body, "message body", required=True)
    room = get_room(logstream, room_id)
    closed = ValueError(f"room {room['room_id']!r} is closed")
    if room["closed"]:
        raise closed
    try:
        # Guarded: a close that lands after the check above still wins.
        event = logstream.append_event(
            type=ROOM_MESSAGE,
            stream=room["stream"],
            room=ROOMS_LOGSTREAM_ROOM,
            from_agent=from_agent,
            to_agent=to_agent or "*",
            correlation_id=room["room_id"],
            body=body,
            unless_correlation_has=ROOM_CLOSE,
        )
    except EventConflict:
        raise closed from None
    return {
        "event": _compact(event),
        "unread": logstream.unread_count(room["room_id"], event["from_agent"]),
    }


def close_room(logstream, *, room_id: str, from_agent: str, outcome: str = "") -> dict:
    """Close a room (once) and return everything up to the close, in order.

    Closing a closed room appends nothing and returns the transcript again,
    so a caller whose filing failed can simply close again. So does a close
    that loses a race with another one.
    """
    outcome = _check_body(outcome, "outcome", required=False)
    room = get_room(logstream, room_id)
    already_closed = room["closed"]
    if not already_closed:
        try:
            logstream.append_event(
                type=ROOM_CLOSE,
                stream=room["stream"],
                room=ROOMS_LOGSTREAM_ROOM,
                from_agent=from_agent,
                to_agent="*",
                correlation_id=room["room_id"],
                body=outcome,
                unless_correlation_has=ROOM_CLOSE,
            )
        except EventConflict:
            already_closed = True
    close_event = _canonical_close(logstream, room["room_id"])
    room = get_room(logstream, room["room_id"])
    return {
        "room": room,
        "event": _compact(close_event),
        "already_closed": already_closed,
        "transcript": room_transcript(logstream, room["room_id"], close_event["hlc"]),
    }


def room_transcript(logstream, room_id: str, upto_hlc: str) -> list:
    """Every event in the room at or before ``upto_hlc``, in HLC order.

    The boundary is the close's HLC, not its local arrival order: HLC is the
    same on every replica, so a turn written before the close on another
    replica belongs to the transcript even when it arrives here after the
    close, and closing again files it.
    """
    events = []
    cursor = None
    while True:
        page = logstream.list_events(
            correlation_id=room_id,
            since_event_id=cursor,
            limit=_TRANSCRIPT_PAGE,
            order="asc",
        )
        events.extend(e for e in page if e["hlc"] <= upto_hlc)
        if len(page) < _TRANSCRIPT_PAGE:
            return sorted(events, key=lambda e: e["hlc"])
        cursor = page[-1]["id"]


def transcript_drawers(events: list) -> list:
    """One drawer text per body-bearing turn: a locator line, then the body verbatim.

    The locator carries the event id, so two identical turns stay two
    drawers under the content-derived drawer id.
    """
    drawers = []
    for event in events:
        if event["type"] not in TRANSCRIPT_TYPES or not event["body"].strip():
            continue
        to = event.get("to_agent")
        header = f"[{event['type']} {event['id']} from={event['from_agent']}"
        if to and to != "*":
            header += f" to={to}"
        header += f" at={event['created_at']}]"
        drawers.append(f"{header}\n{event['body']}")
    return drawers
