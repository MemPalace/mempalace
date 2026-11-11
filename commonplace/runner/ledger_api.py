"""Machine route for run usage reports: ``POST /api/runner/usage``."""

import json
import sqlite3

from commonplace.boards._http import HTTPError, send_json

from . import jobs, ledger, runner_api

MAX_BODY_BYTES = 4096


def _body(h):
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
        return json.loads(h.rfile.read(length).decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPError(400, "invalid JSON") from None


def register(gateway):
    def route(h):
        try:
            # Like every machine route: anyone but an enabled machine sees 404.
            machine = runner_api._machine(gateway, gateway.principal_for(h))
            if machine is None:
                raise HTTPError(404, "not found")
            fields = ledger.clean(_body(h))
            store = jobs.open_store(gateway, machine["workspace_id"])
            with store.transaction() as conn:
                row = ledger.report(conn, machine["id"], fields, jobs._now())
            send_json(h, 200, {"job_id": row["job_id"], **ledger.view(row)})
        except HTTPError as exc:
            send_json(h, exc.status, {"error": str(exc)})
        except ledger.ReportError as exc:
            send_json(h, exc.status, {"error": str(exc)})
        except ValueError as exc:
            send_json(h, 400, {"error": str(exc)})
        except sqlite3.OperationalError:
            send_json(h, 409, {"error": "busy"})

    # An exact route, so it is found before the machine prefix route of runner_api.
    gateway.add_route("POST", "/api/runner/usage", route)
