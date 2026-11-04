"""Deterministic receipts and person-message event handling."""

import functools
import logging
import time
from datetime import datetime, timezone

from commonplace import control_db, events, redact, sessions, webhooks
from commonplace.boards.service import BoardService
from commonplace.chat import handoff, settings, store
from commonplace.chat.types import ACK_WAIT_S, RECEIPT_COPY, OrchestratorView
from commonplace.console import authority
from commonplace.extensions import ToolContext
from commonplace.http_util import log_failure

logger = logging.getLogger(__name__)


def orchestrator(gateway, principal, board: str) -> OrchestratorView | None:
    service = BoardService(ToolContext(principal=principal, gateway=gateway))
    with service.store.connection() as conn:
        actor = service._board(conn, board)["coordinator"]
    if not actor:
        return None

    name = actor.removeprefix("conn:")
    with gateway.db._lock:
        connection = gateway.db._conn.execute(
            "SELECT * FROM connections WHERE workspace_id=? AND name=?",
            (principal.workspace_id, name),
        ).fetchone()
    now = datetime.now(timezone.utc)
    expires = control_db._parse(connection["expires_at"]) if connection else None
    active = (
        actor.startswith("conn:")
        and connection is not None
        and not connection["revoked_at"]
        and (expires is None or expires > now)
    )
    name = redact.redact(name)[0]
    if not active:
        return OrchestratorView(actor, None, name, False, None, None, False)

    cid = connection["id"]
    try:
        view = authority.agent(gateway, principal, cid)
    except LookupError:
        view = None
    visible = view is not None
    last_seen = view.last_used_at if visible else None
    return OrchestratorView(
        actor=actor,
        connection_id=cid,
        name=name,
        visible=visible,
        status=authority.status(last_seen, now) if visible else None,
        last_seen=last_seen,
        wakeable=webhooks.has_space_subscription(
            gateway.db, gateway, principal.workspace_id, actor, board
        ),
    )


def receipt(gateway, principal, board: str, *, work: bool) -> tuple[str, str, tuple[str, ...]]:
    view = orchestrator(gateway, principal, board)
    if view is None:
        code = "no_orchestrator"
    elif view.connection_id is None:
        code = "orchestrator_gone"
    else:
        code = "forwarded" if view.wakeable else "forwarded_no_wake"
    role = "owner" if principal.role == "owner" else "member"
    values = {
        "name": view.name if view else "",
        "status": view.status if view and view.visible else "status private",
        "project": board,
        "wait": ACK_WAIT_S // 60,
    }
    text = RECEIPT_COPY[code][role].format(**values)
    if work:
        text += " " + RECEIPT_COPY["_handoff"][role].format(**values)
    text, kinds = redact.redact(text)
    kinds = set(kinds) | {kind for kind in redact.KINDS if f"[redacted:{kind}]" in text}
    return code, text, tuple(sorted(kinds))


def on_event(gateway, event: dict) -> None:
    try:
        if event.get("kind") != "space_message":
            return
        db = gateway.db
        with db._lock:
            stored = db._conn.execute(
                "SELECT * FROM events WHERE seq=?", (event.get("seq"),)
            ).fetchone()
            if stored is None or events._row(stored) != event:
                return
        actor = event.get("actor", "")
        if not actor.startswith("user:"):
            return
        user_id = actor.removeprefix("user:")
        workspace_id = event["workspace_id"]
        principal = sessions.person_principal(db, user_id, workspace_id)
        if principal is None:
            return
        board = event["data"]["board"]
        message_id = event["data"]["message_id"]
        service = BoardService(ToolContext(principal=principal, gateway=gateway))
        page = service.space_read(board, after=message_id - 1)
        message = next((m for m in page["messages"] if m["id"] == message_id), None)
        if message is None:
            return
        work = handoff.is_work_request(message["text"])
        code, text, kinds = receipt(gateway, principal, board, work=work)
        wake = False
        if work:
            wake = handoff.open(
                db, workspace_id, board, message_id, user_id, page["coordinator"], time.time()
            )
        front = settings.front(db, workspace_id)
        if front.enabled and front.provider != "none":
            wake = store.enqueue(db, workspace_id, board, message_id, user_id) or wake
        if wake:
            gateway.chat_worker.wake()
        store.open_replies(gateway, workspace_id).add(
            board=board,
            message_id=message_id,
            for_user=user_id,
            kind="receipt",
            code=code,
            text=text,
            redacted=tuple(kinds),
            model=None,
        )

    except Exception as exc:
        log_failure(logger, "chat front event failed", exc)


def install_listener(gateway) -> None:
    if getattr(gateway, "chat_listener", None) is not None:
        return
    gateway.chat_listener = functools.partial(on_event, gateway)
    events.add_listener(gateway.chat_listener)
    gateway.db.add_pre_close_callback(lambda: events.remove_listener(gateway.chat_listener))
