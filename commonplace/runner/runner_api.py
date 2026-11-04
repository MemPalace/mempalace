"""Machine-only HTTP endpoints; no launch commands or user content cross this API."""

import json
from urllib.parse import parse_qs, urlsplit

from commonplace.boards._http import HTTPError, send_json
from commonplace.boards.service import BoardService
from commonplace.boards.store import require_visible, task_row
from commonplace.control_db import ControlError
from commonplace.extensions import ToolContext, ToolError
from commonplace.sessions import person_principal

from . import jobs, predicate, registry

MAX_BODY_BYTES = 4096
BODY_KEYS = {
    "lease": set(),
    "renew": {"lease_token"},
    "starting": {"lease_token"},
    "result": {"lease_token", "outcome", "thread_id", "code"},
}


def _body(h, action):
    media = (h.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise HTTPError(415, "application/json required")
    try:
        length = int(h.headers.get("Content-Length", "0"))
    except ValueError:
        raise HTTPError(400, "invalid content length") from None
    if length < 0 or length > MAX_BODY_BYTES:
        raise HTTPError(413, "request too large")
    try:
        value = json.loads(h.rfile.read(length).decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPError(400, "invalid JSON") from None
    if not isinstance(value, dict) or value.keys() - BODY_KEYS[action]:
        raise HTTPError(400, "invalid request body")
    required = set() if action == "lease" else {"lease_token"}
    if action == "result":
        required.add("outcome")
    if required - value.keys():
        raise HTTPError(400, "missing required argument")
    for key, item in value.items():
        if not isinstance(item, str) or not item or len(item) > 256:
            raise HTTPError(400, "invalid " + key)
    return value


def _machine(gateway, principal):
    if principal is None or getattr(principal, "runner", None) != "machine":
        raise HTTPError(404, "not found")
    return next(
        (
            m
            for m in registry.machines(gateway.db, principal.workspace_id)
            if m["machine_connection_id"] == principal.connection_id
            and m["enabled"]
            and not m["revoked_at"]
        ),
        None,
    )


def _items(machine, key):
    value = machine[key]
    return json.loads(value) if isinstance(value, str) else value


def _starting_check(gateway, machine, row):
    if not machine["enabled"] or machine["revoked_at"]:
        return "machine_disabled"
    if machine["binding_state"] != "owner_attested":
        return "binding_not_attested"
    ref = row["requested_by"]
    try:
        if ref.startswith("user:"):
            principal = person_principal(gateway.db, ref[5:], machine["workspace_id"])
        elif ref.startswith("conn:"):
            principal = gateway.db.authenticate_connection(ref[5:])
        else:
            principal = None
    except ControlError:
        principal = None
    if principal is None or not predicate.requester_ok(gateway, principal, machine):
        return "requester_invalid"
    if (
        row["project"] not in _items(machine, "projects")
        or row["topic"] not in _items(machine, "topics")
        or row["runtime_mode"] not in jobs.MODES
        or jobs.MODES.index(row["runtime_mode"]) > jobs.MODES.index(machine["max_runtime_mode"])
    ):
        return "grant_changed"
    service = BoardService(ToolContext(principal=principal, gateway=gateway))
    try:
        with service.store.connection() as conn:
            card = task_row(conn, row["task_number"])
            require_visible(card, principal)
            board = service._board(conn, card["board"])
            service._write(board, row=card)
            predicate.startable(gateway, machine, card, board)
    except (ToolError, predicate.Refused):
        return "card_not_startable"
    return None


def _listing(store, machine, query):
    params = parse_qs(query, keep_blank_values=True)
    if params.keys() - {"state"} or len(params.get("state", [])) > 1:
        raise HTTPError(400, "invalid query")
    states = params.get("state", ["starting,uncertain"])[0].split(",")
    allowed = {"queued", "leased", "starting", "started", "failed", "uncertain", "cancelled"}
    if not states or any(state not in allowed for state in states):
        raise HTTPError(400, "invalid state")
    jobs.sweep(store, store.gateway.db, store.workspace_id)
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM runner_jobs WHERE machine_id=? AND state IN ("
            + ",".join("?" for _ in states)
            + ") ORDER BY created_at,rowid",
            (machine["id"], *states),
        ).fetchall()
    return {
        "jobs": [
            dict(
                jobs.job_object(row, tokens=False),
                **{key: row[key] for key in ("state", "thread_id", "outcome_code", "updated_at")},
            )
            for row in rows
        ]
    }


def register(gateway):
    def route(h):
        try:
            # The control lock keeps the grant stable through the guarded transition.
            with gateway.db._lock:
                principal = gateway.principal_for(h)
                machine = _machine(gateway, principal)
                if machine is None:
                    raise HTTPError(404, "not found")
                url = urlsplit(h.path)
                action = url.path.removeprefix("/api/runner/")
                if (h.command, action) not in {
                    ("GET", "jobs"),
                    *(("POST", key) for key in BODY_KEYS),
                }:
                    raise HTTPError(404, "not found")
                store = jobs.open_store(gateway, principal.workspace_id)
                if h.command == "GET":
                    payload = _listing(store, machine, url.query)
                else:
                    body = _body(h, action)
                    if url.query:
                        raise HTTPError(400, "invalid query")
                    if action == "lease":
                        row = jobs.lease(store, machine)
                        if row is None:
                            h.send_response(204)
                            h.send_header("Content-Length", "0")
                            h.send_header("Cache-Control", "no-store")
                            h.end_headers()
                            return
                    elif action == "renew":
                        row = jobs.renew(store, machine, **body)
                    elif action == "starting":
                        row = jobs.starting(
                            store,
                            machine,
                            **body,
                            validate=lambda row: _starting_check(gateway, machine, row),
                        )
                    else:
                        row = jobs.result(store, machine, **body)
                    payload = jobs.job_object(row, tokens=action == "lease")
            send_json(h, 200, payload)
        except HTTPError as exc:
            send_json(h, exc.status, {"error": str(exc)})
        except jobs.Conflict as exc:
            send_json(h, 409, {"error": str(exc)})
        except ToolError as exc:
            message = str(exc).removeprefix("no: ")
            send_json(h, 404 if message == "start not found" else 403, {"error": message})
        except ValueError as exc:
            send_json(h, 400, {"error": str(exc)})

    for method in ("GET", "POST"):
        gateway.add_prefix_route(method, "/api/runner/", route)
