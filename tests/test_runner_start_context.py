"""The start prompt carries the real card: verbatim, in priority order, inside a budget.

``test_start_prompt_carries_the_card`` is the measurement: it starts a card that has a
body, comments, a handoff note and a linked memory, leases the job as the runner does
and lists which of those the launch text holds. Before the lease carried
``card_context`` every entry was missing: the prompt named the card number only.
"""

import json

import pytest

from commonplace import memory_tools
from commonplace.boards import goals
from commonplace.boards.service import BoardService
from commonplace.runner import agent, start_context
from commonplace.runner.agent import SPAWN_MESSAGE
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._split_helpers import blockers, world
from tests.test_orch_runner import BOARD, PLAIN, PROMPT, World

TITLE = 'Import the ferry timetable ("summer" season)'
BODY = (
    "Read the operator's CSV and store one row per sailing.\n\n"
    "Done when:\n- every sailing of the sample file is stored\n"
    "- a rerun adds nothing\n\tNaïve times are refused."
)
COMMENTS = [
    "The operator publishes a new file every Monday.",
    "Line two of the sample has a broken date.\nKeep it as a test case.",
    "Use the harbour codes, not the names.",
]
HANDOFF = "Parser is done; the rerun check is still missing."
MEMORY = "Sample file: 14 sailings, harbour codes HBR-A to HBR-D.\nSecond line."


def _launch_text(payload):
    """The message the runner launches with for a lease payload."""
    return agent._launch_text(payload)[0]


def _rich(w, board):
    number = w.card(board, title=TITLE, body=BODY, labels=["import"])
    for text in COMMENTS:
        w.boards.task_comment(number, text)
    with w.boards.store.transaction() as conn:
        conn.execute(
            "INSERT INTO task_events(task_id,kind,text,stamp_user,stamp_trust,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (number, "handoff", HANDOFF, "user:example", "trusted", "2030-01-01T00:00:00Z"),
        )
    saved = memory_tools.memory_save(
        w.ctx(w.a), {"text": MEMORY, "theme": "shared", "task": number}
    )
    return number, saved.split()[1]


def _leased(w, number, board):
    if board == BOARD:
        w.configure()
    status, body = w.person_start(number)
    assert status == 201, body
    return w.lease()


@pytest.mark.parametrize("board", [PLAIN, BOARD])
def test_start_prompt_carries_the_card(hub, board):
    w = World(hub)
    number, memory_id = _rich(w, board)
    text = _launch_text(_leased(w, number, board))
    wanted = {
        "title": json.dumps(TITLE),
        "body": json.dumps(BODY, ensure_ascii=False),
        "labels": '"import"',
        **{f"comment {i}": json.dumps(c) for i, c in enumerate(COMMENTS)},
        "handoff note": json.dumps(HANDOFF),
        "linked memory id": memory_id,
        "how to fetch the rest": f"task_show(task={number})",
    }
    missing = [name for name, needle in wanted.items() if needle not in text]
    assert not missing, f"missing from the start prompt: {missing}"


@pytest.mark.parametrize("board", [PLAIN, BOARD])
def test_small_card_prompt_is_the_old_prompt_plus_the_context(hub, board, monkeypatch):
    w = World(hub)
    number = w.card(board, title="Small card", body="One line.")
    payload = _leased(w, number, board)
    context = payload["card_context"]
    old = SPAWN_MESSAGE.format(n=number, job_id=payload["job_id"])
    if board == BOARD:
        old = payload["message"]
        assert old.startswith(SPAWN_MESSAGE.format(n=number, job_id=payload["job_id"]))
        assert PROMPT in old and "Small card" not in old
    assert _launch_text(payload) == old + "\n\n" + context
    assert "[cut" not in context
    lines = context.splitlines()
    assert lines[2:4] == ['Title: "Small card"', 'Body: "One line."']
    assert lines[-1] == (
        f"Not shown here (edits, moves, claims and later changes): task_show(task={number})."
    )
    # Without the context field the launch text is exactly what it was.
    payload.pop("card_context")
    assert _launch_text(payload) == old


def test_budget_zero_switches_the_context_off(hub, monkeypatch):
    monkeypatch.setenv(start_context.BUDGET_ENV, "0")
    w = World(hub)
    number = w.card(PLAIN, title="Small card")
    assert "card_context" not in _leased(w, number, PLAIN)


def test_very_large_card_holds_the_budget_and_marks_the_cut(hub, monkeypatch):
    monkeypatch.setenv(start_context.BUDGET_ENV, "1500")
    w = World(hub)
    number, _memory = _rich(w, PLAIN)
    big = [f"Comment {i}: " + "detail " * 400 for i in range(40)]
    for text in big:
        w.boards.task_comment(number, text)
    context = _leased(w, number, PLAIN)["card_context"]
    assert start_context.tokens(context) <= 1500
    assert json.dumps(TITLE) in context and json.dumps(BODY, ensure_ascii=False) in context
    # Whole items only: a comment is either there to its last byte or absent.
    kept = [text for text in [*COMMENTS, *big] if json.dumps(text) in context]
    assert kept and len(kept) < 43
    shown = [
        line.split(" comment: ", 1)[1] for line in context.splitlines() if " comment: " in line
    ]
    assert [json.loads(text) for text in shown] == kept[::-1]
    marker = f"[cut: {43 - len(kept)} of 43 comments left out to fit the start budget; "
    assert marker + f"read them with task_show(task={number})]" in context
    # The newest comment is tried first; older ones are kept only if they still fit.
    assert kept[-1] == big[-1]


def test_context_is_what_the_worker_may_read(hub):
    """The card is read under the worker's policy: a memory outside its wings stays a
    pointer the worker cannot see, and never becomes text in the prompt."""
    w = World(hub)
    number = w.card(PLAIN, title="Small card")
    w.boards.store.link_drawer(
        number, "drawer_private", w.owner.actor, title="Private note", theme="elsewhere"
    )
    context = _leased(w, number, PLAIN)["card_context"]
    assert "drawer_private" not in context and "Private note" not in context


def test_linked_memory_text_follows_the_readers_memory_access(hub):
    """A connection that may read memories gets the linked text, verbatim. The default
    worker has card tools only, so its context keeps the pointer and no text."""
    w = World(hub)
    number, memory_id = _rich(w, PLAIN)
    card = start_context.load(w.ctx(w.a), number)
    assert card["linked_text"] == {memory_id: MEMORY}
    line = f"- memory {json.dumps(memory_id)}: {json.dumps(MEMORY)}"
    assert line in start_context.assemble(card)
    context = _leased(w, number, PLAIN)["card_context"]
    assert memory_id in context and json.dumps(MEMORY) not in context


def test_failed_read_never_fails_the_lease(hub, monkeypatch):
    w = World(hub)
    number = w.card(PLAIN, title="Small card")

    def broken(self, task):
        raise RuntimeError("boards are away")

    monkeypatch.setattr(BoardService, "task_show", broken)
    payload = _leased(w, number, PLAIN)
    assert payload["card_context"] == (
        f"Card context is unavailable; read the card with task_show(task={number})."
    )


def test_blockers_and_related_cards_are_named(hub):
    w = world(hub)
    first = w.card(title="Agree on the file format")
    number = w.card(title="Small card")
    blockers(w, number, first)
    card = start_context.load(w.ctx(w.a), number)
    assert card["blocked_by"] == [first]
    assert card["related"] == {first: {"title": "Agree on the file format", "status": "todo"}}
    context = start_context.assemble(card)
    assert f'- blocked by #{first} "Agree on the file format" (todo)' in context


def test_started_card_carries_its_goal_chain(hub):
    w = world(hub)
    number = w.card(title="Small card")
    plain = start_context.assemble(start_context.load(w.ctx(w.a), number))
    assert "oal" not in plain.split("Body:")[0] and "Goals above" not in plain
    with w.boards.store.transaction() as conn:
        top = goals.put(conn, "user:example", "2030-01-01", title="Ship the ferry app")["id"]
        mid = goals.put(conn, "user:example", "2030-01-01", title="Timetables", parent=top)["id"]
        leaf = goals.put(
            conn, "user:example", "2030-01-01", title="Import works", detail="Weekly", parent=mid
        )["id"]
        goals.point(conn, "user:example", "2030-01-01", number, leaf)
    context = start_context.assemble(start_context.load(w.ctx(w.a), number))
    lines = context.splitlines()
    assert lines[4] == 'Goal of this card: "Import works" - "Weekly"'
    assert lines[lines.index("Goals above it (nearest first):") + 1 :][:2] == [
        '- "Timetables"',
        '- "Ship the ferry app"',
    ]
    assert "[cut" not in context


# ------------------------------------------------------------- pure assembly


def _card(**fields):
    return {
        "number": 7,
        "board": "acme",
        "title": "Example card",
        "body": "Example body.",
        "status": "todo",
        "events": [],
        "linked": [],
        **fields,
    }


def _comment(text, at="2030-01-01T00:00:00Z"):
    return {"kind": "comment", "actor": "user:example", "text": text, "at": at}


def test_sections_come_in_priority_order():
    card = _card(
        events=[
            _comment("first"),
            {
                "kind": "decide",
                "actor": "user:example",
                "text": "Go with B",
                "choice": "B",
                "at": "2030-01-02T00:00:00Z",
            },
            _comment("second"),
        ],
        blocked_by=[3],
        related={3: {"title": "Earlier card", "status": "doing"}},
        pr_url="https://example.test/pr/1",
        linked=[{"drawer_id": "drawer_a", "title": "Note", "invalidated": False}],
        linked_text={"drawer_a": "Note text"},
    )
    context = start_context.assemble(card)
    order = [
        'Title: "Example card"',
        'Body: "Example body."',
        "Asks, decisions and handoff notes (newest first):",
        '- 2030-01-02T00:00:00Z user:example decide choice="B": "Go with B"',
        "Comments (newest first):",
        '- 2030-01-01T00:00:00Z user:example comment: "second"',
        '- 2030-01-01T00:00:00Z user:example comment: "first"',
        "Blockers and related cards:",
        '- blocked by #3 "Earlier card" (doing)',
        "Linked evidence:",
        '- work so far: pr_url="https://example.test/pr/1"',
        '- linked memory "drawer_a" titled "Note"',
        "Text of linked memories:",
        '- memory "drawer_a": "Note text"',
        "Not shown here",
    ]
    positions = [context.index(part) for part in order]
    assert positions == sorted(positions)
    assert "[cut" not in context


def test_higher_priority_sections_are_filled_first():
    card = _card(
        events=[_comment("c" * 600) for _ in range(5)],
        linked_text={f"drawer_{i}": "m" * 600 for i in range(5)},
    )
    context = start_context.assemble(card, budget_tokens=700)
    assert start_context.tokens(context) <= 700
    assert context.count("c" * 600) >= 2
    assert "m" * 600 not in context
    assert "[cut: 5 of 5 linked memory texts left out to fit the start budget; " in context
    assert "read them with memory_get(id=<memory id>)]" in context


def _goal_card(count, **fields):
    chain = [{"id": i, "title": f"G{i} " + "t" * 90, "detail": "d" * 300} for i in range(count)]
    return _card(goals={"chain": chain, "omitted": 0}, **fields)


def test_goal_section_is_cut_from_the_top_and_the_own_goal_stays():
    card = _goal_card(4)
    own = start_context.goal_head(card)[0]
    assert own.startswith('Goal of this card: "G3 ')
    # room for the heading, one ancestor and a marker, not for all three
    context = start_context.assemble(card, budget_tokens=330)
    assert start_context.tokens(context) <= 330
    assert own in context
    assert '"G2 ' in context and '"G1 ' not in context and '"G0 ' not in context
    assert (
        "[cut: 2 of 3 goals left out to fit the start budget; read them with goal_list]" in context
    )


def test_goal_chain_that_was_cut_on_the_card_says_so():
    card = _goal_card(3)
    card["goals"]["omitted"] = 2
    context = start_context.assemble(card)
    assert "- (2 higher goals are not shown)" in context


def test_a_card_without_goals_gets_no_goal_text():
    context = start_context.assemble(_card())
    assert "oal" not in context
    assert start_context.assemble(_card(goals=None)) == context


def test_title_and_body_survive_a_budget_smaller_than_the_body():
    body = 'Line with ünïcode and "quotes".\n' * 200
    card = _card(body=body, events=[_comment("small")])
    context = start_context.assemble(card, budget_tokens=50)
    assert f"Body: {json.dumps(body, ensure_ascii=False)}" in context
    assert json.loads(context.split("Body: ", 1)[1].splitlines()[0]) == body
    assert "[cut: 1 of 1 comments left out to fit the start budget; " in context
    assert '"small"' not in context


def test_launch_limit_cuts_the_body_whole_and_says_so():
    card = _card(body="x" * 5000, events=[_comment("small")])
    context = start_context.assemble(card, max_chars=600)
    assert len(context) <= 600
    assert 'Title: "Example card"' in context and "xxx" not in context
    assert "[cut: the body (5000 characters) and the rest of the card do not fit" in context
    assert "task_show(task=7)" in context
    assert start_context.assemble(card, max_chars=40) == ""


def test_added_section_gets_the_same_budget_and_marker():
    """The extension point: a builder inserted in the order is budgeted like the rest."""

    def notes(card):
        return start_context.Section(
            "Notes:", tuple(card["notes"]), "notes", "task_show(task=<note>)"
        )

    card = _card(notes=["- " + "g" * 400, "- " + "h" * 400], events=[_comment("small")])
    order = [notes, *start_context.SECTIONS]
    context = start_context.assemble(card, sections=order)
    assert context.index("Notes:") < context.index("Comments (newest first):")
    tight = start_context.assemble(card, sections=order, budget_tokens=250)
    assert start_context.tokens(tight) <= 250
    assert "[cut: 1 of 2 notes left out to fit the start budget; " in tight


def test_budget_setting(monkeypatch):
    monkeypatch.delenv(start_context.BUDGET_ENV, raising=False)
    assert start_context.budget_tokens() == start_context.DEFAULT_BUDGET_TOKENS
    for value, expected in (("250", 250), ("0", 0), ("-5", 6000), ("many", 6000), ("", 6000)):
        monkeypatch.setenv(start_context.BUDGET_ENV, value)
        assert start_context.budget_tokens() == expected


# --------------------------------------------------------------- runner side


def _job(**fields):
    job_id = "job_" + "0" * 16
    return {"task_number": 7, "job_id": job_id, **fields}


def test_runner_appends_the_context_to_both_kinds_of_launch():
    base = SPAWN_MESSAGE.format(n=7, job_id="job_" + "0" * 16)
    assert agent._launch_text(_job()) == (base, None)
    assert agent._launch_text(_job(card_context="Card context")) == (
        base + "\n\nCard context",
        None,
    )
    model = {"instanceId": "example-provider", "model": "example-model"}
    message = base + "\n\nRole prompt"
    assert agent._launch_text(
        _job(message=message, modelSelection=model, card_context="Card context")
    ) == (message + "\n\nCard context", model)


@pytest.mark.parametrize("context", ["", 5, ["text"], "x" * agent.MAX_MESSAGE_CHARS])
def test_runner_refuses_a_malformed_or_oversized_context(context):
    with pytest.raises(ValueError):
        agent._launch_text(_job(card_context=context))
