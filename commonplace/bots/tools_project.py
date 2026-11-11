"""Project tools: what a bot calls so its person can run a project in plain language.

Every tool acts through the board services as the person (a fresh principal per call), so what
the person may not do the tool cannot do either. Text a tool writes (a title, body, comment,
forwarded message, decision reply or project name) must be a verbatim span of the person's
current message (``Turn.words``). The engine audits routine and confirm tools; a service
refusal comes back as one plain sentence with the next step, never the service's own text.
"""

import functools
import re
from collections import Counter

from commonplace import redact
from commonplace.boards import priority as priorities
from commonplace.boards.service import (
    SpaceError,
    SpaceExists,
    SpaceForbidden,
    SpaceMissing,
    VersionClash,
)
from commonplace.bots import blocks, context
from commonplace.bots.tools import Result, Tool, register
from commonplace.chat import front
from commonplace.extensions import ToolError

LIST_LIMIT = 20
QUESTION_LIMIT = 12
OPTIONS_SHOWN = 6
# A tool result is cut at ``context.TOOL_RESULT_CHARS``; rows are added only while the whole
# serialised result stays under this, and the result says how many were left out.
RESULT_BUDGET = context.TOOL_RESULT_CHARS - 600
TITLE_LIMIT = 200
NOTES_SHOWN = 3
NOTE_CHARS = 500
STATUSES = ("queued", "working", "needs_you", "ready", "blocked", "done")

NOT_FOUND = "I cannot find that work in this project. Ask me to list the work to see what exists."
QUESTION_NOT_FOUND = (
    "I cannot find that question in this project. Ask me to list your questions first."
)
CHANGED = "That changed since you were asked. Ask me again to see where it stands now."
REFUSED = (
    "That was not done. You may not have permission for it here, or the work is in a state that "
    "does not allow it. Ask the project owner if you need it."
)
FORBIDDEN = "You are not allowed to do that here. Ask the project owner if you need it."
PRIORITY_OWNER_ONLY = "Only the project owner can set a priority."
BAD_POSITION = "Put it before or after another open task in the same column, not both."
BAD_NEIGHBOUR = "That position is not possible. Pick another task in the same column."
NAME_TAKEN = "There is already a project with a similar name. Pick another name."
NEW_BOT = "Project created. Its bot appears in your list of bots."
NAME_UNAVAILABLE = "That name cannot be used. Pick another name."
NAME_EMPTY = "Give the project a name with at least one letter or number."
CREATE_DENIED = "You are not allowed to create projects here. Ask the workspace owner."
NOTHING_CHANGED = "Say what to change: a priority, or a position before or after another task."
CHOICE_OR_REPLY = "Give either one of the offered answers or your own reply, not both."
NOT_AN_OPTION = "That is not one of the offered answers. Ask me to list your questions again."
NOT_OPEN = "That question is not open any more."

WAIT_TEXT = {
    "blocked_by": "wait for other work to finish",
    "waits_on_person": "wait for a person to answer",
    "claim_expired": "have an agent that stopped reporting",
    "plan_review": "wait for plan review",
    "not_released": "wait for release",
    "no_agent": "cannot start because no agent is connected",
    "project_limit": "wait for a free agent slot",
    "checks_pending": "wait for checks to finish",
}


class Plain(ToolError):
    """A refusal that is already one plain sentence with the next step."""


def _plain(exc) -> str:
    """One plain sentence for a service refusal, mapped on its type; anything unrecognised
    gets the neutral sentence. The service's own text never leaves here."""
    if isinstance(exc, Plain):
        return str(exc)
    if isinstance(exc, VersionClash):
        return CHANGED
    if isinstance(exc, SpaceMissing):
        return NOT_FOUND
    if isinstance(exc, SpaceForbidden):
        return FORBIDDEN
    return REFUSED


def _guarded(run):
    """Turn a refusal raised inside a tool into ``Result(False, say=<plain sentence>)``."""

    @functools.wraps(run)
    def guarded(turn, args):
        try:
            return run(turn, args)
        except ToolError as exc:
            return Result(False, say=_plain(exc))

    return guarded


def _clean(text) -> str:
    return redact.redact(text or "")[0]


def _clip(text, limit) -> str:
    text = " ".join(_clean(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _status(card) -> tuple[str, str]:
    try:
        return blocks.work_status(card)
    except (KeyError, ValueError):
        return "queued", ""


class _Words:
    """Status words as the work block shows them. Another module makes a queued item read
    ``working`` while an agent start for it is in flight, so queued items are looked up
    through the block hydrator (at most ``LOOKUPS`` per call; the rest keep the plain word)."""

    LOOKUPS = 40

    def __init__(self, turn):
        self.reader = blocks.Reader(turn.gateway, turn.principal())
        self.left = self.LOOKUPS

    def __call__(self, card) -> tuple[str, str]:
        status, note = _status(card)
        if status == "queued" and self.left > 0:
            self.left -= 1
            shown = blocks.hydrate(self.reader, [_work_block(card["number"])])[0]
            status = shown.get("status", status)
        return status, note


def _item(turn, board, ref, *, question=False) -> dict:
    """The work item as this person sees it, or a plain "not found": another project's item and
    one the person cannot see read the same, and nothing of its text is repeated."""
    try:
        card = turn.boards().task_show(ref)
    except ToolError:
        card = None
    if card is None or card.get("board") != board:
        raise Plain(QUESTION_NOT_FOUND if question else NOT_FOUND)
    return card


def _work_block(number) -> dict:
    return {"type": "work", "card": number}


def _first_line(text) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return text.strip()


def _orchestrator(turn, board) -> dict:
    """Whether the project's orchestrator is connected and would be woken, in plain words."""
    try:
        view = front.orchestrator(turn.gateway, turn.principal(), board)
    except (ToolError, LookupError):
        view = None
    if view is None:
        return {
            "state": "none",
            "wakeable": False,
            "note": "No orchestrator is set for this project.",
        }
    if view.connection_id is None:
        return {
            "state": "disconnected",
            "wakeable": False,
            "note": "The orchestrator is not connected right now.",
        }
    if view.wakeable:
        return {
            "state": "connected",
            "wakeable": True,
            "note": "The orchestrator is connected and is woken for new messages.",
        }
    return {
        "state": "connected",
        "wakeable": False,
        "note": "The orchestrator is connected but is not woken: it sees this when it next checks.",
    }


def _fits(data) -> bool:
    return len(context.tool_result(Result(True, data))) <= RESULT_BUDGET


def _take(rows, wrap) -> list:
    """The leading ``rows`` for which ``wrap(rows)`` still fits the result budget."""
    kept = []
    for row in rows:
        if not _fits(wrap([*kept, row])):
            break
        kept.append(row)
    return kept


def _items(turn, board) -> list[dict]:
    return turn.boards().task_list(board=board, all_statuses=True, top_level=True)


# Reading


@_guarded
def _project_status(turn, args):
    board = turn.board(args)
    service = turn.boards()
    entry = next((b for b in service.board_list() if b["slug"] == board), None)
    if entry is None:
        return Result(False, say=NOT_FOUND)
    counts = Counter({status: 0 for status in STATUSES})
    working = []
    words = _Words(turn)
    for card in _items(turn, board):
        status = words(card)[0]
        counts[status] += 1
        if status == "working" and len(working) < 5:
            working.append({"number": card["number"], "title": _clip(card["title"], 80)})
    waiting = [
        f"{note['count']} task{'' if note['count'] == 1 else 's'} {WAIT_TEXT[note['code']]}"
        for note in entry.get("waiting_notes", ())
        if note.get("code") in WAIT_TEXT
    ]
    return Result(
        True,
        {
            "project": _clip(entry["name"], 120),
            "tasks": dict(counts),
            "agents_working": entry.get("agents_working", 0),
            "working_on": working,
            "waiting": waiting,
            "orchestrator": _orchestrator(turn, board),
        },
    )


@_guarded
def _list_work(turn, args):
    board = turn.board(args)
    wanted = args.get("status")
    shown, total = [], 0
    words = _Words(turn)
    for card in _items(turn, board):
        status = words(card)[0]
        if (status != wanted) if wanted else (status == "done"):
            continue
        total += 1
        if len(shown) < LIST_LIMIT:
            shown.append(
                {"number": card["number"], "title": _clip(card["title"], 80), "status": status}
            )
    kept = _take(shown, lambda rows: {"work": rows, "left_out": 0})
    return Result(
        True,
        {"work": kept, "left_out": total - len(kept)},
        blocks=tuple(_work_block(item["number"]) for item in kept),
    )


@_guarded
def _work_detail(turn, args):
    board = turn.board(args)
    card = _item(turn, board, args["ref"])
    status, note = _Words(turn)(card)
    comments = [e for e in card.get("events", ()) if e.get("kind") == "comment" and e.get("text")]
    links = [{"label": "Pull request", "url": card["pr_url"]}] if card.get("pr_url") else []
    data = {
        "number": card["number"],
        "title": _clip(card["title"], 200),
        "status": status,
        "priority": card.get("priority"),
        "note": _clip(note or card.get("status_line"), NOTE_CHARS),
        "links": links,
    }
    newest = [
        {"text": _clip(e["text"], NOTE_CHARS), "at": e["at"]}
        for e in reversed(comments[-NOTES_SHOWN:])
    ]
    notes = _take(newest, lambda rows: {**data, "latest_notes": rows})
    data["latest_notes"] = notes[::-1]
    return Result(True, data, blocks=(_work_block(card["number"]),))


@_guarded
def _list_decisions(turn, args):
    board = turn.board(args)
    try:
        asks = turn.boards().needs_me()["asks"]
    except ToolError:
        asks = []  # a member without the needs-me tool has none waiting
    here = [ask for ask in asks if ask.get("board") == board]
    reader = blocks.Reader(turn.gateway, turn.principal())
    rows = []
    for ask in here[:QUESTION_LIMIT]:
        view = blocks.decision(reader, {"type": "decision", "card": ask["number"]})
        if view.get("gone") or not view["open"]:
            continue
        rows.append(
            {
                "ref": ask["number"],
                "question": _clip(view["question"], 200),
                "options": [_clip(option, 80) for option in view["options"][:OPTIONS_SHOWN]],
            }
        )
    kept = _take(rows, lambda part: {"questions": part, "left_out": 0})
    return Result(
        True,
        {"questions": kept, "left_out": len(here) - len(kept)},
        blocks=tuple({"type": "decision", "card": row["ref"]} for row in kept),
    )


# Filing and steering


def _filing(turn, args) -> tuple[str, str]:
    """``(title, body)``: the person's own words. Without arguments, the whole message."""
    body = turn.words(args["body"]) if "body" in args else turn.message["text"]
    title = turn.words(args["title"]) if "title" in args else _first_line(body)[:TITLE_LIMIT]
    return title, body


def _check_filing(turn, args):
    _filing(turn, args)


def _create_work_label(turn, args):
    title, _ = _filing(turn, args)
    return f"Filed “{_clip(title, 80)}”"


@_guarded
def _create_work(turn, args):
    board = turn.board(args)
    title, body = _filing(turn, args)
    extra, note = {}, None
    if args.get("priority") is not None:
        try:
            priorities.owner_value(turn.principal(), args["priority"])
            extra["priority"] = args["priority"]
        except ToolError:
            note = "Filed without the priority: only the project owner can set it."
    card = turn.boards().task_add(title=title, body=body, board=board, status="todo", **extra)
    turn.store().work_add(turn.bot["id"], turn.user_id, board, card["number"], turn.message["id"])
    data = {"number": card["number"], "title": _clip(card["title"], 200)}
    data["status"] = _Words(turn)(card)[0]
    if note:
        data["note"] = note
    return Result(True, data, blocks=(_work_block(card["number"]),))


@_guarded
def _prioritize_work(turn, args):
    board = turn.board(args)
    fields = {key: args[key] for key in ("priority", "before", "after") if key in args}
    if not fields:
        return Result(False, say=NOTHING_CHANGED)
    if "before" in fields and "after" in fields:
        return Result(False, say=BAD_POSITION)
    card = _item(turn, board, args["ref"])
    if "priority" in fields:
        try:
            priorities.owner_value(turn.principal(), fields["priority"])
        except ToolError:
            raise Plain(PRIORITY_OWNER_ONLY) from None
    for key in ("before", "after"):
        if key in fields:
            other = _item(turn, board, fields[key])
            if other["number"] == card["number"] or other["status"] != card["status"]:
                raise Plain(BAD_NEIGHBOUR)
    updated = turn.boards().task_update(args["ref"], version=card["version"], **fields)
    return Result(
        True,
        {"number": card["number"], "priority": updated.get("priority", card.get("priority"))},
        blocks=(_work_block(card["number"]),),
    )


@_guarded
def _comment_work(turn, args):
    board = turn.board(args)
    text = turn.words(args["text"]) if "text" in args else turn.message["text"]
    card = _item(turn, board, args["ref"])
    turn.boards().task_comment(card["number"], text)
    return Result(True, {"number": card["number"]}, blocks=(_work_block(card["number"]),))


@_guarded
def _tell_orchestrator(turn, args):
    board = turn.board(args)
    text = turn.words(args["text"]) if "text" in args else turn.message["text"]
    with front.skipping():  # the person already talks to this bot: no receipt, no fallback card
        posted = turn.boards().space_post(board, text)
    turn.store().forward_add(turn.bot["id"], turn.user_id, board, posted["id"], turn.message["id"])
    state = _orchestrator(turn, board)
    return Result(True, {"passed_on": True, **state})


# Answering and approving (confirm tools: the label is built, and checked, before the tap)


def _decision(turn, args, version=None):
    """``(card, choice, reply)`` for an answer that is still valid; plain refusals otherwise."""
    card = _item(turn, turn.board(args), args["ref"], question=True)
    if version is not None and card["version"] != version:
        raise Plain(CHANGED)
    if card.get("status") != "review" or card.get("review_kind") != "decision":
        raise Plain(NOT_OPEN)
    choice, reply = args.get("choice"), args.get("reply")
    if (choice is None) == (reply is None):
        raise Plain(CHOICE_OR_REPLY)
    if choice is not None and choice not in card["ask_options"]:
        raise Plain(NOT_AN_OPTION)
    return card, choice, turn.words(reply) if reply is not None else None


def _check_answer(turn, args):
    """Before a chip exists: the item, the choice and the words. At the tap (the frozen
    ``version`` is among the arguments) only the text is checked again: a refusal there leaves
    the chip open, while a changed item must close it, and ``run`` refuses that."""
    if args.get("version") is None:
        _decision(turn, args)
    elif (args.get("choice") is None) == (args.get("reply") is None):
        raise Plain(CHOICE_OR_REPLY)
    elif args.get("reply") is not None:
        turn.words(args["reply"])


def _answer_decision_label(turn, args):
    card, choice, reply = _decision(turn, args)
    question = card["ask"] or card["title"]
    return f"Answer “{_clip(choice or reply, 80)}” to “{_clip(question, 100)}”"


@_guarded
def _answer_decision(turn, args):
    version = args.get("version")
    if version is None:
        raise Plain(CHANGED)
    _, choice, reply = _decision(turn, args, version)
    turn.boards().decide(args["ref"], choice=choice, reply=reply, version=version)
    return Result(
        True,
        {"answered": True},
        blocks=({"type": "decision", "card": args["ref"]},),
        say="Your answer was sent.",
    )


def _check_approve(turn, args):
    if args.get("version") is None:  # at the tap, ``run`` checks the item and closes the chip
        _item(turn, turn.board(args), args["ref"])


def _approve_work_label(turn, args):
    card = _item(turn, turn.board(args), args["ref"])
    verb = "Approve" if args["approve"] else "Decline"
    return f"{verb} “{_clip(card['title'], 100)}”"


@_guarded
def _approve_work(turn, args):
    board = turn.board(args)
    card = _item(turn, board, args["ref"])
    version = args.get("version")
    if version is None or card["version"] != version:
        raise Plain(CHANGED)
    turn.boards().task_approve(card["number"], approve=args["approve"], version=version)
    return Result(
        True,
        {"approved": args["approve"]},
        blocks=(_work_block(card["number"]),),
        say="Approved." if args["approve"] else "Declined.",
    )


# Projects


def _slug(name) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60].strip("-")


def _check_project(turn, args):
    if not _slug(turn.words(args["name"])):
        raise Plain(NAME_EMPTY)


def _create_project_label(turn, args):
    name = turn.words(args["name"])
    return f"Create a new project “{_clip(name, 80)}”"


@_guarded
def _create_project(turn, args):
    name = turn.words(args["name"])
    slug = _slug(name)
    if not slug:
        raise Plain(NAME_EMPTY)
    try:
        turn.boards().board_add(slug=slug, name=name)
    except SpaceExists:
        raise Plain(NAME_TAKEN) from None
    except SpaceMissing:
        raise Plain(NAME_UNAVAILABLE) from None
    except SpaceForbidden:
        raise Plain(CREATE_DENIED) from None
    except SpaceError:
        raise Plain(REFUSED) from None
    return Result(
        True,
        {"created": True, "note": NEW_BOT},
        say=NEW_BOT,
    )


# Registration

REF = {"type": "integer", "minimum": 1}
PRIORITY = {"type": "string", "enum": list(priorities.PRIORITIES)}


def _schema(properties=None, required=()):
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }


def _static(text):
    return lambda turn, args: text


register(
    Tool(
        name="project_status",
        description=(
            "Task counts, who is working, what waits and why, and whether the orchestrator "
            "is connected and woken."
        ),
        schema=_schema(),
        kind="read",
        run=_project_status,
        label=_static("Looked at the project status"),
        scope="project",
    )
)
register(
    Tool(
        name="list_work",
        description=("List up to 20 tasks (open ones unless a status is given)."),
        schema=_schema({"status": {"type": "string", "enum": list(STATUSES)}}),
        kind="read",
        run=_list_work,
        label=_static("Listed the work"),
        scope="project",
    )
)
register(
    Tool(
        name="work_detail",
        description="One task: status, latest notes, links.",
        schema=_schema({"ref": REF}, ["ref"]),
        kind="read",
        run=_work_detail,
        label=lambda turn, args: f"Looked at work #{args['ref']}",
        scope="project",
    )
)
register(
    Tool(
        name="create_work",
        description=(
            "File new work as a task, in the person's own words, only on a clear request. "
            "Without arguments the whole message is filed; `title` and `body` must be copied "
            "verbatim from it (to file one part, pass that part). Never also call "
            "tell_orchestrator for it. `priority`: project owner only."
        ),
        schema=_schema(
            {
                "title": {"type": "string", "minLength": 1, "maxLength": TITLE_LIMIT},
                "body": {"type": "string", "minLength": 1, "maxLength": 8000},
                "priority": PRIORITY,
            }
        ),
        kind="routine",
        run=_create_work,
        label=_create_work_label,
        check=_check_filing,
        scope="project",
    )
)
register(
    Tool(
        name="prioritize_work",
        description=(
            "Change a task's priority (project owner only), or move it `before` or `after` "
            "another task in the same column."
        ),
        schema=_schema(
            {"ref": REF, "priority": PRIORITY, "before": REF, "after": REF},
            ["ref"],
        ),
        kind="routine",
        run=_prioritize_work,
        label=lambda turn, args: f"Reordered work #{args['ref']}",
        scope="project",
    )
)
register(
    Tool(
        name="comment_work",
        description=(
            "Comment on a task with the person's own words: `text` copied verbatim from the "
            "message, or the whole message."
        ),
        schema=_schema(
            {"ref": REF, "text": {"type": "string", "minLength": 1, "maxLength": 8000}}, ["ref"]
        ),
        kind="routine",
        run=_comment_work,
        label=lambda turn, args: f"Commented on work #{args['ref']}",
        scope="project",
    )
)
register(
    Tool(
        name="tell_orchestrator",
        description=(
            "Pass the person's own words to the orchestrator to steer the work: `text` copied "
            "verbatim from the message, or the whole message. Not for new work (use "
            "create_work). Say honestly whether it is woken; it is passed on, not done."
        ),
        schema=_schema({"text": {"type": "string", "minLength": 1, "maxLength": 8000}}),
        kind="routine",
        run=_tell_orchestrator,
        label=_static("Passed your message to the orchestrator"),
        scope="project",
    )
)
register(
    Tool(
        name="list_decisions",
        description="The open questions waiting for this person in the project.",
        schema=_schema(),
        kind="read",
        run=_list_decisions,
        label=_static("Listed your open questions"),
        scope="project",
    )
)
register(
    Tool(
        name="answer_decision",
        description=(
            "Answer an open question with exactly one of `choice` (an offered option, as "
            "listed) or `reply` (copied verbatim from the message). Waits for the person's tap."
        ),
        schema=_schema(
            {
                "ref": REF,
                "choice": {"type": "string", "minLength": 1, "maxLength": 500},
                "reply": {"type": "string", "minLength": 1, "maxLength": 8000},
            },
            ["ref"],
        ),
        kind="confirm",
        run=_answer_decision,
        label=_answer_decision_label,
        check=_check_answer,
        scope="project",
    )
)
register(
    Tool(
        name="approve_work",
        description=(
            "Approve a task that waits for approval, or decline it (`approve` false). Waits "
            "for the person's tap."
        ),
        schema=_schema({"ref": REF, "approve": {"type": "boolean"}}, ["ref", "approve"]),
        kind="confirm",
        run=_approve_work,
        label=_approve_work_label,
        check=_check_approve,
        scope="project",
    )
)
register(
    Tool(
        name="create_project",
        description=(
            "Create a new project; `name` copied verbatim from the message. Waits for the "
            "person's tap."
        ),
        schema=_schema({"name": {"type": "string", "minLength": 1, "maxLength": 80}}, ["name"]),
        kind="confirm",
        run=_create_project,
        label=_create_project_label,
        check=_check_project,
    )
)
