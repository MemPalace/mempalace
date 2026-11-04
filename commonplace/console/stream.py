"""Bounded, read-only console pages for an already authorized agent."""

import base64
import binascii
import json
import re
import sqlite3

from commonplace import redact
from commonplace.console import output, store, types
from mempalace.sqlite_crypto import operational_error_type

_MAX_POSITION = (1 << 63) - 1
_BUDGET = 128 * 1024
_CURSOR_ERROR = "Invalid console cursor"


def _positions(values):
    values = tuple(values)
    if any(type(value) is not int or not 0 <= value <= _MAX_POSITION for value in values):
        raise ValueError(_CURSOR_ERROR)
    return values


def encode_cursor(a: int, e: int, r: int, c: int) -> str:
    _positions((a, e, r, c))
    payload = json.dumps(dict(v=3, a=a, e=e, r=r, c=c), separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_CURSOR_ERROR)
        result[key] = value
    return result


def decode_cursor(text: str) -> tuple[int, int, int, int]:
    if (
        not isinstance(text, str)
        or not 1 <= len(text) <= 192
        or re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", text) is None
    ):
        raise ValueError(_CURSOR_ERROR)
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
        canonical = base64.urlsafe_b64encode(raw).decode()
        if text not in (canonical, canonical.rstrip("=")):
            raise ValueError(_CURSOR_ERROR)
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_keys)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"v", "a", "e", "r", "c"}
            or type(payload["v"]) is not int
            or payload["v"] != 3
        ):
            raise ValueError(_CURSOR_ERROR)
        return _positions(payload[key] for key in ("a", "e", "r", "c"))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError(_CURSOR_ERROR) from exc


def _clean(text):
    # Never scan a prefix of a long secret.
    if len(text) > redact.MAX_CHARS:
        return "[omitted:too_long]", ["too_long"]
    return redact.redact(text)


def _board_item(row):
    try:
        data = json.loads(row["data"])
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    fields = [row["subject"], data.get("title")]
    transition = isinstance(data.get("from"), str) and isinstance(data.get("to"), str)
    if transition:
        fields.extend((data["from"], data["to"]))
    parts, kinds = [], []
    omitted = False
    for field in fields:
        if isinstance(field, str):
            clean, found = _clean(field)
            parts.append(clean)
            kinds.extend(found)
            omitted |= len(field) > redact.MAX_CHARS
        else:
            parts.append("")
    text = " · ".join(part for part in parts[:2] if part)
    if transition:
        text = " · ".join(part for part in (text, f"{parts[2]} → {parts[3]}") if part)
    # Joining fields can create a new secret pattern (amendment N4).
    text, found = _clean(text)
    kinds.extend(found)
    kind, found = _clean(row["kind"])
    kinds.extend(found)
    truncated = omitted or "too_long" in kinds or len(text) > 4000
    cut = 4000
    if len(text) > cut:
        marker = max(text.rfind("[redacted:", 0, cut), text.rfind("[omitted:", 0, cut))
        if marker >= 0 and "]" not in text[marker:cut]:
            cut = marker
    return {
        "id": f"e{row['seq']}",
        "at": row["at"],
        "source": "board",
        "kind": kind,
        "text": text[:cut],
        "truncated": truncated,
        "redacted": list(dict.fromkeys(kinds)),
    }


def _events(db, ws, name, seq, count, forward):
    # Board events carry the hub's actor convention "conn:<name>". Connection names are
    # UNIQUE per workspace and connections are only revoked, never deleted, so a name
    # identifies exactly one connection here (pinned by
    # test_board_actor_name_identifies_one_connection).
    order = "ASC" if forward else "DESC"
    with db._lock:
        return db._conn.execute(
            "SELECT seq, at, kind, subject, data FROM events "
            "WHERE seq IN (SELECT seq FROM events WHERE workspace_id=? AND seq>? "
            "ORDER BY seq DESC LIMIT 2000) AND workspace_id=? AND actor=? "
            f"ORDER BY seq {order} LIMIT ?",
            (ws, seq, ws, f"conn:{name}", count),
        ).fetchall()


def _bounded(items):
    result, size = [], 2  # JSON list brackets; send_json uses default separators.
    for item in items:
        extra = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        extra += 2 if result else 0
        if size + extra > _BUDGET:
            break
        result.append(item)
        size += extra
    return result


def _read_output(gateway, agent, r, c, forward):
    if not agent.own:
        return [], 0, 0
    try:
        head = output.latest_ids(gateway, agent)
        if head is None:
            return None, r, c
        if forward and head == (r, c):
            return None, r, c
        recent = output.recent(gateway, agent)
        if recent is None:
            return None, r, c
        return _bounded(recent[:20]), *head
    except ValueError:
        # Broken generation pointer: keep the panel and retry on the next poll.
        return None, r, c
    except (sqlite3.OperationalError, operational_error_type(None)) as exc:
        if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
            raise
        return None, r, c


def page(gateway, principal, agent: types.AgentView, cursor: str | None, limit: int | None) -> dict:
    forward = cursor is not None
    a, e, r, c = decode_cursor(cursor) if forward else (0, 0, 0, 0)
    if limit is None:
        limit = 100 if forward else 50
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid limit")
    count = limit + 1 if forward else limit
    activity = None
    pointer_broken = False
    if agent.own:
        try:
            activity = store.peek(gateway, principal.workspace_id)
        except ValueError:
            pointer_broken = True
    tool_rows = []
    if activity is not None:
        tool_rows = (
            activity.after(agent.id, a, count) if forward else activity.tail(agent.id, count)
        )
    board_rows = _events(gateway.db, principal.workspace_id, agent.name, e, count, forward)
    candidates = []
    for row in tool_rows:
        name, _ = _clean(row["name"])
        item = {
            "id": f"t{row['seq']}",
            "at": row["at"],
            "source": "tool",
            "name": name,
            "outcome": row["outcome"],
            "ms": row["ms"],
        }
        candidates.append((row["at"], 0, row["seq"], item))
    candidates.extend((row["at"], 1, row["seq"], _board_item(row)) for row in board_rows)
    candidates.sort(key=lambda entry: entry[:3])
    ordered = [entry[3] for entry in candidates]
    if forward:
        items = _bounded(ordered[:limit])
        for item in items:
            if item["source"] == "tool":
                a = int(item["id"][1:])
            else:
                e = int(item["id"][1:])
    else:
        # Tail pages retain the newest items, including under the byte budget.
        items = list(reversed(_bounded(reversed(ordered[-limit:]))))
        a = max((row["seq"] for row in tool_rows), default=0)
        e = max((row["seq"] for row in board_rows), default=0)
    if pointer_broken:
        panel = None
    else:
        panel, r, c = _read_output(gateway, agent, r, c, forward)
    return {
        "items": items,
        "cursor": encode_cursor(a, e, r, c),
        "more": len(ordered) > len(items),
        "tools": ("on" if agent.recording else "off") if agent.own else "private",
        "output": panel,
    }
