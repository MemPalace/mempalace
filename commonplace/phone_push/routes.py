"""Phone notification route registration."""

import secrets
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit

from commonplace.http_util import read_json, require_principal, send_json

from . import config, eligibility, sender
from .dispatcher import SeedRace


def _active(db, principal):
    return db._conn.execute(
        "SELECT * FROM phone_push_subscriptions "
        "WHERE workspace_id = ? AND user_id = ? AND active = 1",
        (principal.workspace_id, principal.user_id),
    ).fetchone()


def _view(gateway, principal):
    with gateway.db._lock:
        row = _active(gateway.db, principal)
        test = None
        if row is not None:
            latest = gateway.db._conn.execute(
                "SELECT status FROM phone_push_deliveries "
                "WHERE subscription_id = ? AND category = 'test' ORDER BY rowid DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            if latest is not None:
                status = latest["status"]
                test = {
                    "status": "pending"
                    if status in ("pending", "sending", "retry")
                    else "accepted"
                    if status == "accepted"
                    else "not_accepted"
                }
    cfg = config.load() if row is not None else None
    return {
        "available": getattr(gateway, "phone_push", None) is not None,
        "eligible": eligibility.eligible(gateway.db, principal),
        "subscription": {
            "server": cfg.url if cfg else None,
            "topic": row["topic"],
            "created_at": row["created_at"],
        }
        if row is not None
        else None,
        "test": test,
    }


def register(gateway):
    def route(h):
        principal = require_principal(gateway, h)
        if principal is None:
            return

        def error(status, text):
            send_json(h, status, {"error": text})

        if not principal.person:
            error(403, "no: phone notifications need a signed-in person")
            return
        if "?" in h.path:
            error(400, "no: no query parameters")
            return
        if h.headers.get("Content-Length", "0") != "0":
            try:
                data = read_json(h)
            except ValueError:
                return
            if data:
                error(400, "no: no options")
                return

        db = gateway.db
        dispatcher = getattr(gateway, "phone_push", None)
        if h.command == "POST":
            if not eligibility.eligible(db, principal):
                error(
                    409, "no: phone notifications are only for the owner, in their home workspace"
                )
                return
            if dispatcher is None:
                error(409, "no: phone notifications are not set up on this server")
                return

        if h.command == "GET":
            send_json(h, 200, _view(gateway, principal))
        elif h.command == "DELETE":
            with db._lock, db._tx() as conn:
                row = _active(db, principal)
                if row is not None:
                    conn.execute(
                        "UPDATE phone_push_subscriptions SET active = 0, ended_at = ? WHERE id = ?",
                        (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), row["id"]),
                    )
                    conn.execute(
                        "UPDATE phone_push_deliveries SET status = 'cancelled', error = NULL "
                        "WHERE subscription_id = ? AND status IN ('pending', 'retry')",
                        (row["id"],),
                    )
                    conn.execute(
                        "DELETE FROM phone_push_state WHERE subscription_id = ?", (row["id"],)
                    )
            if row is None:
                error(404, "no: phone notifications are off")
            else:
                send_json(h, 200, {"ok": True})
        elif urlsplit(h.path).path == "/api/phone-push/test":
            with db._lock:
                row = _active(db, principal)
                if row is not None:
                    sender.enqueue_test(db, row["id"])
            if row is None:
                error(404, "no: phone notifications are off")
                return
            dispatcher.wake()
            send_json(h, 202, {"status": "pending"})
        else:
            try:
                with dispatcher.work:
                    with db._lock:
                        row = _active(db, principal)
                    if row is None:
                        dispatcher.seed(principal, "cp" + secrets.token_urlsafe(30))
            except SeedRace:
                error(503, "no: phone notifications changed; retry")
                return
            except sqlite3.IntegrityError:
                with db._lock:
                    if _active(db, principal) is None:
                        raise
            send_json(h, 200, _view(gateway, principal))

    for method in ("GET", "POST", "DELETE"):
        gateway.add_route(method, "/api/phone-push", route)
    gateway.add_route("POST", "/api/phone-push/test", route)
