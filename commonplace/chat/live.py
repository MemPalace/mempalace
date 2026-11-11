"""Live delivery state of a person's newest chat message, read from recorded facts.

Nothing here calls a model or an outside service. A message reaches the board's
orchestrator through the existing wake webhook and the agent's own ``space_read``;
this module only records which event carried the message and how far the
orchestrator has read, and projects both into one state for the poster.
"""

import logging
import time

from commonplace import control_db
from commonplace.boards.service import BoardService
from commonplace.chat.types import ACK_WAIT_S
from commonplace.extensions import ToolContext
from commonplace.http_util import log_failure

logger = logging.getLogger(__name__)

STATES = ("idle", "delivered", "working", "answered", "no_agent", "no_answer")
REASONS = (
    "wake_sent",
    "wake_pending",
    "wake_retrying",
    "wake_none",
    "none",
    "gone",
    "wake_failed",
    "not_picked_up",
    "no_reply",
)
PICKUP_WAIT_S = 60  # a woken agent that has not read the message by now is not answering
RECENT_MESSAGES = 50  # only a message this near the end of the chat is still live

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_live_posts (            -- the event that carried a person's message
    workspace_id TEXT NOT NULL,
    board TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    event_seq INTEGER NOT NULL,               -- events.seq; webhook_deliveries.event_seq
    coordinator TEXT,                         -- boards.coordinator actor at post time, or NULL
    PRIMARY KEY (workspace_id, board, message_id)
);
CREATE TABLE IF NOT EXISTS chat_live_reads (            -- newest message an orchestrator has read
    workspace_id TEXT NOT NULL,
    board TEXT NOT NULL,
    actor TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    read_at TEXT NOT NULL,                    -- UTC ISO-8601, when the cursor last advanced
    PRIMARY KEY (workspace_id, board, actor)
);
"""


def ensure_schema(db) -> None:
    with db._lock:
        db._conn.executescript(_SCHEMA)


def record_post(db, event: dict) -> None:
    """Remember which stored event carried a person's message (ids only, no text)."""
    data = event["data"]
    with db._lock:
        db._conn.execute(
            "INSERT OR IGNORE INTO chat_live_posts "
            "(workspace_id,board,message_id,event_seq,coordinator) VALUES (?,?,?,?,?)",
            (
                event["workspace_id"],
                data["board"],
                data["message_id"],
                event["seq"],
                data.get("coordinator"),
            ),
        )


def mark_read(gateway, principal, board, messages) -> None:
    """The board's orchestrator read these messages: advance its cursor, never move it back."""
    if (
        principal.person
        or not messages
        or board["coordinator"] != principal.actor
        or not getattr(gateway, "chat_installed", False)
    ):
        return
    try:
        db = gateway.db
        key = (principal.workspace_id, board["slug"], principal.actor)
        newest = max(message["id"] for message in messages)
        stamp = control_db._iso(control_db._now())
        with db._lock:
            conn = db._conn
            conn.execute(
                "INSERT OR IGNORE INTO chat_live_reads "
                "(workspace_id,board,actor,message_id,read_at) VALUES (?,?,?,0,?)",
                (*key, stamp),
            )
            conn.execute(
                "UPDATE chat_live_reads SET message_id=?,read_at=? "
                "WHERE workspace_id=? AND board=? AND actor=? AND message_id<?",
                (newest, stamp, *key, newest),
            )
    except Exception as exc:
        log_failure(logger, "chat read cursor failed", exc)


def _wake(db, workspace_id: str, board: str, message_id: int, actor: str) -> str:
    """How the wake for this message went: sent, pending, retrying, failed or none."""
    with db._lock:
        post = db._conn.execute(
            "SELECT event_seq,coordinator FROM chat_live_posts "
            "WHERE workspace_id=? AND board=? AND message_id=?",
            (workspace_id, board, message_id),
        ).fetchone()
        # A wake goes to the orchestrator of the moment the message was posted.
        if post is None or post["coordinator"] != actor:
            return "none"
        statuses = {
            row["status"]
            for row in db._conn.execute(
                "SELECT d.status FROM webhook_deliveries d JOIN webhooks w ON w.id=d.webhook_id "
                "WHERE d.event_seq=? AND w.workspace_id=? AND w.owner_actor=?",
                (post["event_seq"], workspace_id, actor),
            )
        }
    for status, wake in (("delivered", "sent"), ("pending", "pending"), ("retry", "retrying")):
        if status in statuses:
            return wake
    return "failed" if statuses else "none"


def _read(db, workspace_id: str, board: str, actor: str):
    with db._lock:
        return db._conn.execute(
            "SELECT message_id,read_at FROM chat_live_reads "
            "WHERE workspace_id=? AND board=? AND actor=?",
            (workspace_id, board, actor),
        ).fetchone()


def snapshot(gateway, principal, board: str, view, now: float | None = None) -> dict:
    """State of the caller's own newest message on this board.

    ``view`` is the caller's :func:`commonplace.chat.front.orchestrator` view.
    Only the caller's own message is described; the agent's activity time is
    included only when the caller may see that agent.
    """
    service = BoardService(ToolContext(principal=principal, gateway=gateway))
    with service.store.connection() as conn:
        b = service._board(conn, board)
        mine = conn.execute(
            "SELECT id,created_at FROM (SELECT * FROM space_messages WHERE board_id=? "
            "ORDER BY id DESC LIMIT ?) WHERE stamp_user=? AND stamp_connection IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (b["id"], RECENT_MESSAGES, principal.user_id),
        ).fetchone()
        answer = (
            conn.execute(
                "SELECT id FROM space_messages WHERE board_id=? AND id>? "
                "AND stamp_connection IS NOT NULL ORDER BY id LIMIT 1",
                (b["id"], mine["id"]),
            ).fetchone()
            if mine is not None
            else None
        )
    result = {
        "message_id": mine["id"] if mine is not None else None,
        "state": "idle",
        "reason": None,
        "agent": view.name if view is not None else None,
        "at": mine["created_at"] if mine is not None else None,
        "seen_at": None,
        "answer_id": None,
    }
    if mine is None:
        return result
    if answer is not None:
        return {**result, "state": "answered", "answer_id": answer["id"]}
    if view is None:
        return {**result, "state": "no_agent", "reason": "none"}
    if view.connection_id is None:
        return {**result, "state": "no_agent", "reason": "gone"}

    posted = control_db._parse(mine["created_at"])
    age = (time.time() if now is None else now) - posted.timestamp() if posted else 0
    db, workspace_id = gateway.db, principal.workspace_id
    read = _read(db, workspace_id, b["slug"], view.actor)
    if read is not None and read["message_id"] >= mine["id"]:
        seen = {"seen_at": read["read_at"] if view.visible else None}
        if age > ACK_WAIT_S:
            return {**result, **seen, "state": "no_answer", "reason": "no_reply"}
        return {**result, **seen, "state": "working"}
    wake = _wake(db, workspace_id, b["slug"], mine["id"], view.actor)
    if wake == "failed":
        return {**result, "state": "no_answer", "reason": "wake_failed"}
    # Without a wake-up hook the agent reads on its own schedule, so it gets the long wait.
    if age > (ACK_WAIT_S if wake == "none" else PICKUP_WAIT_S):
        return {**result, "state": "no_answer", "reason": "not_picked_up"}
    return {**result, "state": "delivered", "reason": f"wake_{wake}"}
