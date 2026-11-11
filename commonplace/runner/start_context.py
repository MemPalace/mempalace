"""Card context of a start: the real card, verbatim, inside a token budget.

A started agent used to get the card number only (``SPAWN_MESSAGE``, then the pinned
role prompt of a config-driven start) and had to rebuild the task from tool calls. The
runner lease now also carries ``card_context``: the card as the machine's worker
connection may read it (``task_show`` under the worker's own policy), appended to the
launch message by the runner.

``assemble`` is the one place that turns a card into that text:

- The title, the body and the card's fields are always present.
- Then each builder in ``SECTIONS`` adds one section, in priority order: asks, decisions
  and handoff notes; comments; blockers and related cards; linked evidence; the text of
  linked memories. A section lists its items most important first.
- An item is kept only while the whole block stays inside the budget. Nothing is ever
  shortened or summarised: an item that does not fit is left out whole, and its section
  ends with a ``[cut: ...]`` line that counts what is missing and names the call that
  returns it.
- User text is JSON-quoted, as ``memory_get`` does: every byte stays recoverable and
  stored text cannot pose as structure of the launch message.

The budget is ``COMMONPLACE_START_CONTEXT_TOKENS`` (default 6000, ``0`` switches the
context off); one token is counted as four UTF-8 bytes. Only a title and body that are
larger than the budget by themselves exceed it. The launch message limit of the runner
is a second, hard bound: when even the title and body do not fit there, the body is cut
whole and the marker says so.

To add context (for example a goals chain), put the data on the card dict in ``load``
and insert a builder in ``SECTIONS`` at its priority; ``assemble`` does the budgeting
and the cut markers.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Callable, Optional

from commonplace.extensions import ToolContext, ToolError
from commonplace.http_util import log_failure

logger = logging.getLogger(__name__)

BUDGET_ENV = "COMMONPLACE_START_CONTEXT_TOKENS"
DEFAULT_BUDGET_TOKENS = 6_000
TOKEN_BYTES = 4
MAX_LINKED_TEXTS = 5  # memory texts read per start; further links stay pointers
FIELD = "card_context"  # lease payload key
SEPARATOR = "\n\n"  # between the launch message and the context

HEADER = (
    "Card context for #{n}, read from the board when this start was leased. "
    "Text in JSON quotes is quoted card data, verbatim and unshortened."
)
TAIL = "Not shown here (edits, moves, claims and later changes): task_show(task={n})."
BODY_CUT = (
    "[cut: the body ({chars} characters) and the rest of the card do not fit the "
    "launch message; read them with task_show(task={n}) before you start]"
)
UNAVAILABLE = "Card context is unavailable; read the card with task_show(task={n})."


@dataclass(frozen=True)
class Section:
    """One optional part of the context. ``items`` are finished lines, most important
    first; ``noun`` and ``fetch`` word the cut marker."""

    heading: str
    items: tuple
    noun: str
    fetch: str

    def marker(self, cut: int) -> str:
        return (
            f"[cut: {cut} of {len(self.items)} {self.noun} left out to fit the start "
            f"budget; read them with {self.fetch}]"
        )


def quote(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _size(line: str) -> int:
    return len(line.encode("utf-8", "surrogatepass")) + 1  # the line and its newline


def tokens(text: str) -> int:
    """The budget's token estimate of ``text``."""
    return -(-len(text.encode("utf-8", "surrogatepass")) // TOKEN_BYTES)


def budget_tokens() -> int:
    """The configured budget; a missing or invalid value is the default."""
    value = os.environ.get(BUDGET_ENV, "").strip()
    if not value.isascii() or not value.isdigit():
        return DEFAULT_BUDGET_TOKENS
    return int(value)


# ------------------------------------------------------------------ sections


def _event(event) -> str:
    parts = [str(event.get("at") or ""), str(event.get("actor") or ""), event["kind"]]
    for key in ("choice", "target", "reason", "context"):
        if event.get(key):
            parts.append(f"{key}={quote(event[key])}")
    if event.get("options"):
        parts.append(f"options={quote(event['options'])}")
    line = "- " + " ".join(part for part in parts if part)
    return line + (f": {quote(event['text'])}" if event.get("text") else "")


def _events(card, kinds):
    return tuple(_event(e) for e in reversed(card.get("events") or []) if e["kind"] in kinds)


def decisions(card) -> Optional[Section]:
    """What people asked and decided on the card, and what the last holder handed over."""
    items = _events(card, ("ask", "decide", "handoff"))
    if card.get("ask"):
        open_ask = f"- open ask to {card.get('ask_to') or 'the owner'}: {quote(card['ask'])}"
        if card.get("ask_options"):
            open_ask += f" options={quote(card['ask_options'])}"
        if card.get("review_context"):
            open_ask += f" context={quote(card['review_context'])}"
        items = (open_ask, *items)
    return Section(
        "Asks, decisions and handoff notes (newest first):",
        items,
        "asks, decisions and handoff notes",
        f"task_show(task={card['number']})",
    )


def comments(card) -> Optional[Section]:
    return Section(
        "Comments (newest first):",
        _events(card, ("comment",)),
        "comments",
        f"task_show(task={card['number']})",
    )


def _related(card, number) -> str:
    known = (card.get("related") or {}).get(number)
    return f"#{number} {quote(known['title'])} ({known['status']})" if known else f"#{number}"


def blockers(card) -> Optional[Section]:
    items = [f"- blocked by {_related(card, n)}" for n in card.get("blocked_by") or []]
    if card.get("waiting"):
        items.append(f"- waiting: {quote(card['waiting'])}")
    if card.get("parent") is not None:
        items.append(f"- child of {_related(card, card['parent'])}")
    items += [f"- blocks {_related(card, n)}" for n in card.get("blocks") or []]
    items += [
        f"- child card #{c['number']} {quote(c['title'])} ({c['status']})"
        for c in card.get("child_cards") or []
    ]
    return Section(
        "Blockers and related cards:",
        tuple(items),
        "blockers and related cards",
        "task_show(task=<number>)",
    )


_PR_FIELDS = ("repo", "branch", "pr_url", "pr_state", "pr_head", "checks", "status_line")


def evidence(card) -> Optional[Section]:
    items = []
    pr = [f"{key}={quote(card[key])}" for key in _PR_FIELDS if card.get(key)]
    if pr:
        items.append("- work so far: " + " ".join(pr))
    for link in card.get("linked") or []:
        line = f"- linked memory {quote(link['drawer_id'])}"
        if link.get("title"):
            line += f" titled {quote(link['title'])}"
        items.append(line + (" (marked outdated)" if link.get("invalidated") else ""))
    if card.get("callstack"):
        items.append("- a call tree of the pull request is stored on the card")
    return Section(
        "Linked evidence:", tuple(items), "evidence entries", f"task_show(task={card['number']})"
    )


def linked_text(card) -> Optional[Section]:
    texts = card.get("linked_text") or {}
    return Section(
        "Text of linked memories:",
        tuple(f"- memory {quote(key)}: {quote(text)}" for key, text in texts.items()),
        "linked memory texts",
        "memory_get(id=<memory id>)",
    )


# Priority order. A builder takes the card dict of ``load`` and returns a Section or None.
SECTIONS: list[Callable[[dict], Optional[Section]]] = [
    decisions,
    comments,
    blockers,
    evidence,
    linked_text,
]


# ------------------------------------------------------------------ assembly


def _fields(card) -> str:
    parts = [f"Card: #{card['number']} on board {quote(card.get('board'))}"]
    for key in ("status", "priority", "assignee", "created_by", "created_at"):
        if card.get(key):
            parts.append(f"{key.replace('_', ' ')} {card[key]}")
    for key in ("task_type", "labels", "flags"):
        if card.get(key):
            parts.append(f"{key.replace('_', ' ')} {quote(card[key])}")
    if card.get("restricted"):
        parts.append("restricted origin")
    return "; ".join(parts)


def assemble(
    card: dict,
    *,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    max_chars: Optional[int] = None,
    sections=None,
) -> str:
    """The context block of ``card`` (the dict of ``load``), or ``""``.

    ``budget_tokens`` bounds the block; ``max_chars`` is the room left in the launch
    message. See the module doc for the order and the cut rules."""
    if budget_tokens <= 0:
        return ""
    number = card["number"]
    head = [
        HEADER.format(n=number),
        _fields(card),
        f"Title: {quote(card['title'])}",
        f"Body: {quote(card.get('body') or '')}",
    ]
    tail = TAIL.format(n=number)
    built = [build(card) for build in (SECTIONS if sections is None else sections)]
    built = [section for section in built if section is not None and section.items]
    # Headings and worst-case markers are reserved first, so a marker always fits.
    used = sum(map(_size, head)) + _size(tail)
    used += sum(_size(s.heading) + _size(s.marker(len(s.items))) for s in built)
    hard = float("inf") if max_chars is None else max_chars
    if used > hard:
        lines = [*head[:3], BODY_CUT.format(chars=len(card.get("body") or ""), n=number)]
        return "\n".join(lines) if sum(map(_size, lines)) <= hard else ""
    allowed = min(budget_tokens * TOKEN_BYTES, hard)
    lines = list(head)
    for section in built:
        kept = []
        for item in section.items:
            if used + _size(item) <= allowed:
                used += _size(item)
                kept.append(item)
        lines += [section.heading, *kept]
        if len(kept) < len(section.items):
            lines.append(section.marker(len(section.items) - len(kept)))
    return "\n".join([*lines, tail])


# ---------------------------------------------------------------------- read


def load(ctx: ToolContext, number: int) -> dict:
    """The card as ``ctx.principal`` may read it: ``task_show``, plus ``related``
    (title and status of readable blocker and parent cards) and ``linked_text`` (the
    text of the first readable linked memories)."""
    from commonplace.boards.service import BoardService
    from commonplace.boards.store import require_visible, task_row

    service = BoardService(ctx)
    card = service.task_show(number)
    numbers = [*(card.get("blocked_by") or []), *(card.get("blocks") or [])]
    if card.get("parent") is not None:
        numbers.append(card["parent"])
    related = {}
    if numbers:
        with service.store.connection() as conn:
            for other in numbers:
                try:
                    row = task_row(conn, other)
                    require_visible(row, ctx.principal)
                except ToolError:
                    continue
                related[other] = {"title": row["title"], "status": row["status"]}
    texts = {}
    current = [link for link in card.get("linked") or [] if not link.get("invalidated")]
    for link in current[:MAX_LINKED_TEXTS]:
        try:
            record = ctx.memory("mempalace_get_drawer", {"drawer_id": link["drawer_id"]})
        except ToolError:
            continue  # not readable for this connection: the pointer line stays
        if isinstance(record, dict) and isinstance(record.get("content"), str):
            texts[link["drawer_id"]] = record["content"]
    return {**card, "related": related, "linked_text": texts}


def lease_fields(gateway, machine, row, message: Optional[str] = None) -> dict:
    """Lease-payload addition of one job: ``{"card_context": text}`` or ``{}``.

    ``message`` is the launch message of a config-driven job; other jobs launch with
    ``SPAWN_MESSAGE``. The card is read as the machine's worker connection, the one the
    started agent works under. The lease is already committed here, so a failed read
    never fails the lease: the agent is told to read the card itself."""
    from commonplace.runner.agent import MAX_MESSAGE_CHARS, SPAWN_MESSAGE

    budget = budget_tokens()
    if budget <= 0:
        return {}
    number = row["task_number"]
    if message is None:
        message = SPAWN_MESSAGE.format(n=number, job_id=row["id"])
    room = MAX_MESSAGE_CHARS - len(message) - len(SEPARATOR)
    try:
        worker = gateway.db.authenticate_connection(machine["worker_connection_id"])
        if worker is None or worker.workspace_id != machine["workspace_id"]:
            return {}
        card = load(ToolContext(principal=worker, gateway=gateway), number)
        text = assemble(card, budget_tokens=budget, max_chars=room)
    except Exception as exc:
        log_failure(logger, "start context unavailable", exc)
        text = UNAVAILABLE.format(n=number)
        if len(text) > room:
            return {}
    return {FIELD: text} if text else {}
