"""Machine-only HTTP endpoints; no launch commands or user content cross this API."""

import json
import sqlite3
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


def _config_job_check(store, card, row):
    """Config-driven jobs: the card must not carry ``plan`` at ``starting``, and
    the pinned config version must still be readable and match its sha256."""
    from commonplace.orch import runner_bridge

    if "plan" in runner_bridge._labels(card):
        return "plan_pending"
    with store.connection() as conn:
        return runner_bridge.config_problem(conn, row)


def _starting_principal(gateway, machine, row):
    ref = row["requested_by"]
    try:
        if ref.startswith("user:"):
            return person_principal(gateway.db, ref[5:], machine["workspace_id"])
        if ref.startswith("conn:"):
            return gateway.db.authenticate_connection(ref[5:])
    except ControlError:
        pass
    return None


def _starting_check(gateway, machine, row):
    """Control authority only; safe inside the runner transaction/control lock."""
    from commonplace.projects import orchestration as orch

    try:
        # Re-read the switch here so a read failure still refuses the transition.
        orch.enabled(gateway.db, machine["workspace_id"])
    except orch.SettingsUnavailable:
        return "settings_unavailable"
    if not machine["enabled"] or machine["revoked_at"]:
        return "machine_disabled"
    if machine["binding_state"] != "owner_attested":
        return "binding_not_attested"
    problem = jobs.binding_problem(machine, row, starting=True)
    if problem:
        return problem
    principal = _starting_principal(gateway, machine, row)
    if principal is None or not predicate.requester_ok(gateway, principal, machine):
        return "requester_invalid"
    if (
        row["project"] not in _items(machine, "projects")
        or row["topic"] not in _items(machine, "topics")
        or row["runtime_mode"] not in jobs.MODES
        or jobs.MODES.index(row["runtime_mode"]) > jobs.MODES.index(machine["max_runtime_mode"])
    ):
        return "grant_changed"
    return None


def _starting_board_check(gateway, machine, row, store):
    """Build services and validate cards before acquiring the runner transaction."""
    from commonplace.projects import orchestration as orch

    on = orch.enabled(gateway.db, machine["workspace_id"])
    principal = _starting_principal(gateway, machine, row)
    if principal is None:
        return "requester_invalid"
    service = BoardService(ToolContext(principal=principal, gateway=gateway))
    try:
        with service.store.connection() as conn:
            card = task_row(conn, row["task_number"])
            require_visible(card, principal)
            board = service._board(conn, card["board"])
            if on and row["config_version"] is None:
                refusal = orch.requester_refusal(gateway, principal, board)
                if refusal:
                    return refusal
            if row["config_version"] is not None:
                refusal = _config_job_check(store, card, row)
                if refusal is not None:
                    return refusal
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
            from commonplace.projects import orchestration as orch

            principal = gateway.principal_for(h)
            if principal is None:
                raise HTTPError(404, "not found")
            # Fail closed on settings, then release control before runner/board work.
            # Starting and lease acquire runner first and retain fresh control authority
            # through their guarded write and runner commit.
            orch.enabled(gateway.db, principal.workspace_id)
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
                    with store.connection() as conn:
                        candidate = jobs._fenced(conn, machine["id"], body["lease_token"])
                    board_refusal = _starting_board_check(gateway, machine, candidate, store)

                    def validate(row):
                        with gateway.db._lock:
                            current = registry.get_machine(
                                gateway.db, principal.workspace_id, machine["id"]
                            )
                            return _starting_check(gateway, current, row) or board_refusal

                    row = jobs.starting(
                        store,
                        machine,
                        **body,
                        validate=validate,
                    )
                else:
                    row = jobs.result(store, machine, **body)
                payload = jobs.job_object(row, tokens=action == "lease")
                if action == "lease" and row["config_version"] is not None:
                    from commonplace.orch import runner_bridge

                    # Pinned prompt, workflow and model of a config-driven start.
                    with store.connection() as conn:
                        payload.update(runner_bridge.launch_fields(conn, row))
            send_json(h, 200, payload)
        except HTTPError as exc:
            send_json(h, exc.status, {"error": str(exc)})
        except sqlite3.OperationalError as exc:
            orch.require_lock_timeout(exc)
            send_json(h, 409, {"error": "busy"})
        except jobs.Conflict as exc:
            send_json(h, 409, {"error": str(exc)})
        except ToolError as exc:
            message = str(exc).removeprefix("no: ")
            send_json(
                h,
                getattr(exc, "status", 404 if message == "start not found" else 403),
                {"error": message},
            )
        except ValueError as exc:
            send_json(h, 400, {"error": str(exc)})

    for method in ("GET", "POST"):
        gateway.add_prefix_route(method, "/api/runner/", route)
