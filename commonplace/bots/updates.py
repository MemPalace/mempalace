"""Updates between turns: what happens to a bot's work reaches its thread without being asked.

One listener on the hub event log. For a card a bot filed, it posts one ``event`` message when
the item needs the person, is ready for review or is done; for an orchestrator reply to a post a
bot forwarded, it posts that reply as it was written. Only the bot that filed or forwarded the
item gets anything, and only while its owner can still see the item (the owner's person
principal is resolved fresh and checked the way project chat checks it).

The listener is cheap on the emitting thread: one indexed lookup in ``bots.sqlite3`` (skipped
when the workspace has no such file) filters out every event no bot cares about, and it never
raises. Failures are logged as a code and a stack location, never as text. Exactly-once is kept
in the store: a work item posts only when its status word changes, and a forwarded post is
answered by the first coordinator reply after it, marked in the same write as the message.

There is no event for a start that failed to launch, so that update is not built here.
"""

import functools
import logging
from pathlib import Path

from commonplace import events, redact, sessions, webhooks
from commonplace.boards.service import BoardService
from commonplace.bots import blocks, store
from commonplace.extensions import ToolContext, ToolError
from commonplace.http_util import log_failure

logger = logging.getLogger(__name__)

TITLE_CHARS = 120
ANNOUNCED = {
    "needs_you": ("A question about “{title}” needs you.", "decision"),
    "ready": ("“{title}” is ready for you to look at.", "work"),
    "done": ("“{title}” is done.", "work"),
}


def _existing(gateway, workspace_id):
    """The workspace's bot store, or None when no bot was ever made there (nothing is created)."""
    directory = gateway.supervisor.workspace_dir(workspace_id)
    if not (Path(directory) / "bots.sqlite3").exists():
        return None
    return store.open_bots(gateway, workspace_id)


def _person(gateway, user_id, event):
    """The owner's fresh principal when they may still receive this event, else None."""
    principal = sessions.person_principal(gateway.db, user_id, event["workspace_id"])
    if principal is None or not webhooks.can_receive_event(gateway.db, gateway, principal, event):
        return None
    return principal


def _title(card) -> str:
    title = " ".join(redact.redact(str(card["title"]))[0].split())
    return title if len(title) <= TITLE_CHARS else title[: TITLE_CHARS - 1] + "…"


def _card_event(gateway, event, bots, links):
    data = event["data"]
    board, number = data["board"], data["number"]
    for link in links:
        principal = _person(gateway, link["user_id"], event)
        if principal is None:
            continue
        try:
            card = BoardService(ToolContext(principal=principal, gateway=gateway)).task_show(number)
        except ToolError:
            continue  # the owner cannot see this item (any more)
        word = blocks.work_status(card)[0]
        text = kind = None
        if word in ANNOUNCED:
            template, kind = ANNOUNCED[word]
            text = template.format(title=_title(card))
        bots.work_changed(
            link["bot_id"],
            link["user_id"],
            board,
            number,
            event["seq"],
            word,
            text,
            [{"type": kind, "card": number}] if kind else (),
        )


def _first_reply(service, board, post, coordinator, upto):
    """Id of the coordinator's first message after ``post`` (looked for up to ``upto``), or None."""
    after = post
    while after < upto:
        page = service.space_read(board, after=after)
        for message in page["messages"]:
            if message["actor"] == coordinator:
                return message["id"]
        if not page["messages"]:
            return None
        after = page["cursor"]
    return None


def _reply_event(gateway, event, bots, forwards):
    data = event["data"]
    board, reply_id = data["board"], data["message_id"]
    answered = {}  # bot -> (user, the reply, the posts it answers)
    for forward in forwards:
        principal = _person(gateway, forward["user_id"], event)
        if principal is None:
            continue
        service = BoardService(ToolContext(principal=principal, gateway=gateway))
        try:
            page = service.space_read(board, after=reply_id - 1)
            message = next((m for m in page["messages"] if m["id"] == reply_id), None)
            # Only the orchestrator answers a post, and only its first reply after it: a reply
            # that arrives out of order must not take a post an earlier reply answers.
            if (
                message is None
                or not page["coordinator"]
                or _first_reply(
                    service, board, forward["space_message_id"], page["coordinator"], reply_id
                )
                != reply_id
            ):
                continue
        except ToolError:
            continue
        answered.setdefault(forward["bot_id"], (forward["user_id"], message["text"], []))[2].append(
            forward["space_message_id"]
        )
    for bot_id, (user_id, text, posts) in answered.items():
        bots.forward_answered(
            bot_id, user_id, board, reply_id, posts, text, [{"type": "orchestrator"}]
        )


def on_event(gateway, event: dict) -> None:
    try:
        kind, data = event.get("kind", ""), event.get("data") or {}
        if kind == "space_replied":
            lookup, handle = "reply", _reply_event
        elif kind.startswith("card.") and kind != "card.commented":
            lookup, handle = "card", _card_event
        else:
            return
        board = data.get("board")
        item = data.get("message_id" if lookup == "reply" else "number")
        if not isinstance(board, str) or type(item) is not int:
            return
        bots = _existing(gateway, event["workspace_id"])
        if bots is None:
            return
        found = (
            bots.unanswered_forwards(board, item)
            if lookup == "reply"
            else bots.bots_for_card(board, item)
        )
        if found and events.is_stored(gateway.db, event):
            handle(gateway, event, bots, found)
    except Exception as exc:
        log_failure(logger, "bot update failed", exc)


def working_bots(gateway, principal, bots) -> set[str]:
    """Ids of this person's bots that filed a card now in progress."""
    filed = bots.work_by_bot(principal.user_id)
    if not filed:
        return set()
    service = BoardService(ToolContext(principal=principal, gateway=gateway))
    doing = {}
    for board in {board for cards in filed.values() for board, _ in cards}:
        try:
            doing[board] = {c["number"] for c in service.task_list(board=board, status="doing")}
        except ToolError:
            doing[board] = set()
    return {
        bot_id
        for bot_id, cards in filed.items()
        if any(card in doing[board] for board, card in cards)
    }


def install(gateway) -> None:
    if getattr(gateway, "bots_listener", None) is not None:
        return
    gateway.bots_listener = functools.partial(on_event, gateway)
    events.add_listener(gateway.bots_listener)
    gateway.db.add_pre_close_callback(lambda: events.remove_listener(gateway.bots_listener))
