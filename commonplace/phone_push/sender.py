"""Phone notification delivery helpers."""

import http.client
import json
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from commonplace import urls, webhooks

from . import config, eligibility, evaluate


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _claim(db, delivery_id):
    with db._lock, db._tx() as conn:
        row = conn.execute(
            "SELECT * FROM phone_push_deliveries WHERE id=? "
            "AND status IN ('pending','retry') AND next_at<=?",
            (delivery_id, time.time()),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE phone_push_deliveries SET status='sending', attempt=attempt+1 WHERE id=?",
            (delivery_id,),
        )
        return dict(row) | {"status": "sending", "attempt": row["attempt"] + 1}


def _recipient(dispatcher, row, cache):
    db = dispatcher.db
    with db._lock:
        subscription = db._conn.execute(
            "SELECT * FROM phone_push_subscriptions WHERE id=?", (row["subscription_id"],)
        ).fetchone()
        if (
            subscription is None
            or not subscription["active"]
            or subscription["workspace_id"] != row["workspace_id"]
        ):
            return None
        if (
            row["category"] != "test"
            and not db._conn.execute(
                "SELECT 1 FROM phone_push_state WHERE subscription_id=? AND number=? AND category=?",
                (row["subscription_id"], row["number"], row["category"]),
            ).fetchone()
        ):
            return None
        seq = db._conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
    owner = eligibility.owner_principal(db, subscription["user_id"], row["workspace_id"])
    if owner is None or eligibility.paused(db, subscription["user_id"], row["workspace_id"]):
        return None
    if row["category"] != "test":
        cached = cache.get(row["subscription_id"])
        if cached is None or cached[0] != seq:
            cached = (seq, evaluate.current(dispatcher.gateway, owner))
            cache[row["subscription_id"]] = cached
        if not evaluate.applies(
            dispatcher.gateway, owner, row["number"], row["category"], cur=cached[1]
        ):
            return None
    return subscription


def _outcome(dispatcher, row, status, http_status, error):
    if status == "retry" and row["attempt"] >= dispatcher.max_attempts:
        status = "dead"
    delay = dispatcher.schedule[min(row["attempt"] - 1, len(dispatcher.schedule) - 1)]
    with dispatcher.db._lock, dispatcher.db._tx() as conn:
        conn.execute(
            "UPDATE phone_push_deliveries SET status=?,http_status=?,error=?,next_at=?,"
            "accepted_at=? WHERE id=? AND status='sending' AND attempt=?",
            (
                status,
                http_status,
                error,
                time.time() + delay,
                _now() if status == "accepted" else None,
                row["id"],
                row["attempt"],
            ),
        )


def drain(dispatcher) -> int:
    """Attempt each due delivery once, rechecking immediately before transport."""
    settings = config.load()
    if settings is None:
        return 0
    db = dispatcher.db
    with db._lock:
        due = db._conn.execute(
            "SELECT id FROM phone_push_deliveries WHERE status IN ('pending','retry') "
            "AND next_at<=? ORDER BY next_at,rowid",
            (time.time(),),
        ).fetchall()
    cache = {}
    attempted = 0
    for delivery in due:
        if dispatcher._stop.is_set():
            break
        with dispatcher.work:
            if dispatcher._stop.is_set():
                break
            row = _claim(db, delivery["id"])
            if row is None:
                continue
            attempted += 1
            try:
                subscription = _recipient(dispatcher, row, cache)
                click = None
                if subscription is not None:
                    if row["category"] == "test":
                        body = "Test from Commonplace"
                    else:
                        suffix = {
                            "needs_you": "needs you",
                            "blocked": "is blocked",
                            "done": "is done",
                        }[row["category"]]
                        body = f"#{row['number']} {suffix}"
                        ws = row["workspace_id"]
                        click = urls.share_link(db, ws, f"/app/?w={ws}#/card/{row['number']}")
            except Exception:
                # Rechecking failed before HTTP: release without spending an attempt.
                with db._lock, db._tx() as conn:
                    conn.execute(
                        "UPDATE phone_push_deliveries SET status='pending',attempt=attempt-1,"
                        "next_at=?,http_status=NULL,error=NULL WHERE id=? "
                        "AND status='sending' AND attempt=?",
                        (time.time() + 5, row["id"], row["attempt"]),
                    )
                continue
            if subscription is None:
                result = ("cancelled", None, None)
            else:
                try:
                    result = post(
                        settings,
                        subscription["topic"],
                        body,
                        priority="high" if row["category"] == "needs_you" else "default",
                        click=click,
                    )
                except Exception:
                    # Transport exceptions can contain credentials or private text.
                    result = ("retry", None, "delivery connection failed")
            _outcome(dispatcher, row, *result)
    return attempted


def recover(db, max_attempts):
    """Recover counted claims interrupted before their outcome was committed."""
    with db._lock, db._tx() as conn:
        conn.execute(
            "UPDATE phone_push_deliveries SET status='retry',next_at=? "
            "WHERE status='sending' AND attempt<?",
            (time.time(), max_attempts),
        )
        conn.execute(
            "UPDATE phone_push_deliveries SET status='dead',error='delivery interrupted' "
            "WHERE status='sending' AND attempt>=?",
            (max_attempts,),
        )


def enqueue_test(db, subscription_id):
    delivery_id = secrets.token_hex(16)
    with db._lock, db._tx() as conn:
        conn.execute(
            "INSERT INTO phone_push_deliveries "
            "(id,subscription_id,workspace_id,number,category,status,attempt,next_at,created_at) "
            "SELECT ?,id,workspace_id,NULL,'test','pending',0,?,? "
            "FROM phone_push_subscriptions WHERE id=? AND active=1",
            (delivery_id, time.time(), _now(), subscription_id),
        )
    return delivery_id


def post(config, topic, body, *, priority, click) -> tuple:
    """Return only a fixed outcome; never retain the response or exception text."""
    conn = None
    try:
        parts = urlsplit(config.url)
        cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = cls(parts.hostname, parts.port, timeout=webhooks.HTTP_TIMEOUT)
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": "Commonplace",
            "Priority": priority,
        }
        if click is not None:
            headers["Click"] = click
        if config.token is not None:
            headers["Authorization"] = "Bearer " + config.token
        # http.client uses neither environment proxies nor redirect handling.
        conn.request("POST", "/" + topic, body=body.encode("utf-8"), headers=headers)
        response = conn.getresponse()
        status = response.status
        if not 200 <= status < 300:
            retry = status == 429 or 500 <= status < 600
            return ("retry" if retry else "dead", status, f"HTTP {status}")
        content = response.read(4096)
        try:
            data = json.loads(content)
        except (ValueError, UnicodeError, RecursionError):
            return "dead", status, "unexpected response"
        message_id = data.get("id") if isinstance(data, dict) else None
        if isinstance(message_id, str) and re.fullmatch(r"[A-Za-z0-9]{1,32}", message_id):
            return "accepted", status, None
        return "dead", status, "unexpected response"
    except Exception:
        return "retry", None, "delivery connection failed"
    finally:
        if conn is not None:
            conn.close()
