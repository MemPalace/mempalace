"""Synthetic Northwind workspace for the workbench browser acceptance suite.

Everything here is invented: the Northwind workspace, the Atlas, Harbor, Ledger and
Field notes projects, the people Maya (owner) and Leon (member) and the agents Iris,
Otto and Juno. Projects are led by orchestrator agents named after the project.

Card numbers follow the drawn design (A-115 … A-135), so the ids a person reads in
the browser are the ids the design shows. Gaps are filled with Harbor cards.
"""

import json
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from commonplace import sessions
from commonplace.boards.service import BoardService
from commonplace.extensions import ToolContext

AGENT_SCOPES = ["memory:read", "memory:write"]
WINGS = ["atlas", "harbor", "ledger", "field"]

# The words a person reads; tests compare them byte for byte.
A121_CONTEXT = (
    "I need to know which rate limit applies to batch sync before I can size the queue. "
    "@Leon, is it the per-user or the per-workspace limit?"
)
A121_ASK = "Which rate limit applies to batch sync?"
APPROVAL_TITLE = "Send beta 0.9 to 40 testers?"
APPROVAL_BODY = (
    "Beta 0.9 is ready. A-118, A-134 and A-135 are done: the sign-up email arrives on all "
    "3 test domains, and Leon reviewed the release notes. If you approve, I'll send build "
    "0.9 (212 of 212 tests passed) to the 40 people in the Beta group and post the release "
    "notes. If you decline, nothing is sent."
)
REQUEST_TEXT = "Prepare the beta 0.9 release notes and check the sign-up email on 3 test domains."
STUCK_TEXT = "Is anything stuck on Atlas?"
QUICK_REPLY = (
    "From the board: one card is stuck. #121 is blocked; Otto is waiting on Leon about which "
    "rate limit applies to batch sync. That is in Leon's Needs me, not yours. Everything else "
    "is moving."
)
ORCH_ANSWER = (
    "I made two cards from your request, A-134 and A-135, and linked A-118, which fixes the "
    "sign-up email."
)
ORCH_ASKING = (
    "A-118, A-134 and A-135 are done. I've asked you in Needs me to approve sending beta 0.9 "
    "to the 40 testers."
)
LEON_RATE = (
    "Batch sync should use the per-workspace limit, 600 requests a minute. "
    "The per-user limit is only for the public API."
)
NOTE_TEXT = (
    "Keep one rate limit per workspace for internal traffic (sync, exports). "
    "Public API gets per-user limits."
)
HARBOR_QUESTION = "Pick the homepage headline"
HARBOR_OPTIONS = ["Plan your harbour stay in two minutes", "Every berth, one booking"]
HARBOR_CONTEXT = (
    "Both versions passed the readability check. I can't choose between them without you; "
    "it's a brand call. The page ships with whichever you pick."
)
LEDGER_TEXT = "Reconcile the October invoices and flag anything over 500."


@dataclass
class Seed:
    hub: object
    maya: str
    leon: str
    cookie: dict
    leon_cookie: dict
    cards: dict = field(default_factory=dict)
    messages: dict = field(default_factory=dict)
    replies: dict = field(default_factory=dict)
    agents: dict = field(default_factory=dict)

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.hub.port}"

    def number(self, cid):
        """'A-121' -> 121 (the seed keeps design numbers)."""
        return self.cards[cid]

    def person(self, user=None):
        return sessions.person_principal(self.hub.db, user or self.maya, self.hub.workspace)

    def service(self, principal=None):
        return BoardService(
            ToolContext(principal=principal or self.person(), gateway=self.hub.gateway)
        )

    def agent(self, name):
        return self.service(self.agents[name])


def _cookie(hub, email):
    link = sessions.create_login_link(hub.db, email, hub.workspace)
    session = sessions.redeem_login_link(hub.db, link)
    assert session, "login link did not redeem"
    return {"name": sessions.COOKIE, "value": session}


def _agent(hub, user, name):
    token = hub.token(
        name,
        user=user,
        profile="full",
        scopes=AGENT_SCOPES,
        read_wings=WINGS,
        write_wings=WINGS,
        trust="standard",
    )
    return hub.gateway.authenticate("Bearer " + token)


def _sql(service, statement, args=()):
    with service.transaction() as conn:
        conn.execute(statement, args)


def _post_at(service, principal, board, text, at):
    """Append a chat message with a past time (the table is append-only; no rewrite)."""
    with service.transaction() as conn:
        board_id = conn.execute("SELECT id FROM boards WHERE slug=?", (board,)).fetchone()[0]
        return conn.execute(
            "INSERT INTO space_messages(board_id,text,stamp_user,stamp_connection,stamp_trust,"
            "flags,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                board_id,
                text,
                principal.user_id,
                None if principal.person else principal.connection,
                principal.policy.trust,
                "[]",
                _iso(at),
            ),
        ).lastrowid


def _next(service):
    with service.transaction() as conn:
        return conn.execute("SELECT coalesce(max(id), 0) + 1 FROM tasks").fetchone()[0]


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build(hub, *, released_hold=True):
    """Seed the hub's workspace as Northwind and return a Seed."""
    db = hub.db
    with db._lock:
        db._conn.execute("UPDATE workspaces SET name='Northwind' WHERE id=?", (hub.workspace,))
        db._conn.execute("UPDATE users SET name='Maya' WHERE id=?", (hub.admin,))
    leon = db.create_user("leon@example.test", "Leon")
    db.add_member(hub.workspace, leon, "member", actor=hub.admin)
    seed = Seed(
        hub=hub,
        maya=hub.admin,
        leon=leon,
        cookie=_cookie(hub, "owner@example.com"),
        leon_cookie=_cookie(hub, "leon@example.test"),
    )
    for name in ("Iris", "Otto", "Juno", "atlas", "harbor", "ledger"):
        seed.agents[name.lower()] = _agent(hub, hub.admin, name)
    maya = seed.service()
    for slug, name in (("atlas", "Atlas"), ("harbor", "Harbor"), ("ledger", "Ledger")):
        maya.board_add(slug, name=name, wing=slug)
    maya.board_add("field-notes", name="Field notes", wing="field")
    for slug in ("atlas", "harbor", "ledger"):
        maya.board_set(slug, slug)
    people = {"maya": f"user:{seed.maya}", "leon": f"user:{leon}"}

    # Harbor fills the numbers below Atlas's first card and the gaps between them.
    def finish(number, svc=None):
        svc = svc or maya
        for status in ("doing", "done"):
            svc.task_update(number, version=svc.task_show(number)["version"], status=status)

    first = maya.task_add("Harbor kickoff notes", board="harbor")["number"]
    finish(first)
    _sql(
        maya,
        "INSERT INTO tasks (id, board_id, title, body, status, position, version, stamp_user, "
        "stamp_connection, stamp_trust, restricted_origin, created_at, updated_at, updated_by, "
        "done_at) SELECT 105, board_id, 'Harbor berth map', '', 'done', position + 1, 1, "
        "stamp_user, stamp_connection, stamp_trust, restricted_origin, created_at, updated_at, "
        "updated_by, done_at FROM tasks WHERE id=?",
        (first,),
    )
    filler = iter(
        [
            "Harbor price table",
            "Harbor booking emails",
            "Harbor map tiles",
            "Harbor photo shoot",
            "Harbor FAQ",
            "Harbor press kit",
            "Harbor footer links",
            "Harbor cookie banner",
            "Harbor terms page",
            "Harbor contact form",
            "Harbor team page",
            "Harbor sitemap",
            "Harbor 404 page",
            "Harbor fonts",
        ]
    )

    def add(number, title, *, by=None, status="todo", **kw):
        card = (by or maya).task_add(title, board="atlas", **kw)
        assert card["number"] == number, (card["number"], number)
        if status == "done":
            finish(number)
        return card

    def fill_to(number):
        while _next(maya) < number:
            finish(maya.task_add(next(filler), board="harbor")["number"])

    fill_to(107)
    for n, title in ((107, "App icon, 3 variants"),):
        seed.cards[f"A-{n}"] = add(n, title, status="done")["number"]
    fill_to(109)
    seed.cards["A-109"] = add(109, "Store listing text", status="done")["number"]
    seed.cards["A-110"] = add(110, "Crash reporting wired up", status="done")["number"]
    fill_to(112)
    seed.cards["A-112"] = add(112, "Beta sign-up form", status="done")["number"]
    fill_to(115)
    iris, otto, juno = (seed.agent(n) for n in ("iris", "otto", "juno"))
    a115 = add(115, "Push-notification opt-in screen", by=iris)
    seed.cards["A-115"] = 115
    a116 = add(
        116,
        "Cold start under 2 seconds",
        by=juno,
        body="Measured on the beta build: cold start is 1.7 seconds on the reference phone.",
    )
    seed.cards["A-116"] = 116
    # The approval: Atlas's orchestrator proposes it under its parent.
    lead = replace(seed.agents["atlas"], parent_agent="conn:ledger")
    approval = BoardService(ToolContext(principal=lead, gateway=hub.gateway)).task_add(
        APPROVAL_TITLE, body=APPROVAL_BODY, board="atlas"
    )
    assert approval["number"] == 117, approval["number"]
    assert approval["proposal_status"] == "pending", approval
    seed.cards["approval"] = 117
    seed.cards["A-118"] = add(
        118,
        "Fix sign-up email not arriving on some domains",
        by=seed.agent("atlas"),
        labels=["bug"],
        parent=117,
    )["number"]
    fill_to(121)
    a121 = add(
        121,
        "Offline mode: queue edits while disconnected",
        body=(
            "Edits made offline should be kept on the phone and sent when the connection "
            "returns. Conflicts: last write wins, but keep both versions in history."
        ),
        labels=["sync", "beta"],
    )
    seed.cards["A-121"] = 121
    fill_to(124)
    seed.cards["A-124"] = add(124, "Translate the settings screen (de, nl, fr)", by=juno)["number"]
    fill_to(126)
    seed.cards["A-126"] = add(
        126, "Choose the app icon variant for the beta", assignee=people["maya"]
    )["number"]
    fill_to(129)
    seed.cards["A-129"] = add(
        129,
        "Add dark-mode screenshots to the store listing",
        labels=["store"],
        assignee=people["leon"],
    )["number"]
    fill_to(131)
    seed.cards["A-131"] = add(131, "Write onboarding copy for the invite screen", labels=["copy"])[
        "number"
    ]
    seed.cards["A-132"] = add(132, "Collect crash logs from beta group 1")["number"]

    # Working states. Iris works on A-118, Otto on A-121 until he asks Leon, Juno on A-124.
    iris.task_take_next(board="atlas", task=118)
    otto.task_take_next(board="atlas", task=121)
    juno.task_take_next(board="atlas", task=124)
    card = otto.task_show(121)
    otto.task_handoff(
        121,
        people["leon"],
        A121_ASK,
        options=["Per-user", "Per-workspace"],
        version=card["version"],
        context=A121_CONTEXT,
    )
    iris.task_take_next(board="atlas", task=115)
    card = iris.task_show(115)
    iris.task_update(
        115,
        version=card["version"],
        status="review",
        question="Is the opt-in screen ready to ship as built?",
        options=["Ship it", "Change the copy first"],
        context="Two screenshots attached; the copy follows the store guidelines.",
    )
    _sql(maya, "UPDATE boards SET done_needs_review=1 WHERE slug='atlas'")
    juno.task_take_next(board="atlas", task=116)
    card = juno.task_show(116)
    juno.task_update(116, version=card["version"], status="done")
    _ = a115, a116, a121

    # Atlas chat. Leon's message from 28 Oct sits first, so memory can open the chat there.
    old = datetime(2026, 10, 28, 15, 9, tzinfo=timezone.utc)
    otto_p, leon_p = seed.agents["otto"], seed.person(leon)
    seed.messages["otto-1609"] = _post_at(
        maya,
        otto_p,
        "atlas",
        "Starting the sync design. Which limits apply to background traffic?",
        old,
    )
    seed.messages["leon-1612"] = _post_at(
        maya, leon_p, "atlas", LEON_RATE, old + timedelta(minutes=3)
    )
    seed.messages["otto-1613"] = _post_at(
        maya, otto_p, "atlas", "Noted.", old + timedelta(minutes=4)
    )
    seed.messages["stuck"] = maya.space_post("atlas", STUCK_TEXT)["id"]
    seed.messages["request"] = maya.space_post("atlas", REQUEST_TEXT)["id"]
    atlas = seed.agent("atlas")
    seed.messages["answer"] = atlas.space_post("atlas", ORCH_ANSWER)["id"]
    fill_to(134)
    seed.cards["A-134"] = atlas.task_add(
        "Draft beta 0.9 release notes",
        board="atlas",
        message_id=seed.messages["request"],
        parent=117,
    )["number"]
    seed.cards["A-135"] = atlas.task_add(
        "Check the sign-up email on 3 test domains",
        board="atlas",
        message_id=seed.messages["request"],
        parent=117,
    )["number"]
    assert (seed.cards["A-134"], seed.cards["A-135"]) == (134, 135), seed.cards
    seed.messages["asking"] = atlas.space_post("atlas", ORCH_ASKING)["id"]

    # The 12:41 quick reply under Maya's question names #121.
    from commonplace.chat.store import ReplyStore

    replies = ReplyStore(hub.gateway, hub.workspace)
    reply = replies.add(
        board="atlas",
        message_id=seed.messages["stuck"],
        for_user=seed.maya,
        kind="answer",
        code="answered",
        text=QUICK_REPLY,
        redacted=(),
        model="local:openai_compatible/fast-model",
    )
    seed.replies["quick"] = reply.id

    # Harbor: a decision for Maya, plus an approval released this morning.
    harbor = seed.agent("harbor")
    seed.messages["harbor-ask"] = harbor.space_post(
        "harbor",
        "Both homepage headlines passed the readability check. I've asked you in Needs me "
        "to pick one.",
    )["id"]
    seed.cards["harbor-decision"] = harbor.ask(
        HARBOR_QUESTION, options=HARBOR_OPTIONS, board="harbor", context=HARBOR_CONTEXT
    )["number"]
    if released_hold:
        hlead = replace(seed.agents["harbor"], parent_agent="conn:ledger")
        privacy = BoardService(ToolContext(principal=hlead, gateway=hub.gateway)).task_add(
            "Publish the Harbor privacy page",
            body="Publish the reviewed privacy page.",
            board="harbor",
        )
        seed.cards["harbor-privacy"] = privacy["number"]
        _release(seed, privacy)

    # Ledger's orchestrator read a work request six minutes ago and has not answered.
    seed.messages["ledger-ask"] = _post_at(
        maya,
        seed.person(),
        "ledger",
        LEDGER_TEXT,
        datetime.now(timezone.utc) - timedelta(minutes=6),
    )

    # Memory: one saved note in Atlas's wing (the chat memory is Leon's message above).
    _note(seed)
    return seed


def _release(seed, card):
    """Approve through a 60-second hold and let the worker release it."""
    status, body, _ = request(
        seed,
        "POST",
        "/api/holds",
        {
            "card": card["number"],
            "kind": "approve",
            "version": card["version"],
        },
    )
    assert status == 201, (status, body)
    worker = seed.hub.gateway.hold_worker
    assert worker.run_once(now=worker.now() + 61)


def _note(seed):
    from tests.test_commonplace_boards import mcp

    token = seed.hub.token(
        "maya-notes",
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=WINGS,
        write_wings=WINGS,
        trust="trusted",
    )
    ok, text = mcp(
        seed.hub,
        token,
        "mempalace_add_drawer",
        wing="atlas",
        room="api-design",
        content=NOTE_TEXT,
    )
    assert ok, text


def request(seed, method, path, body=None, cookie=None):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", seed.hub.port, timeout=30)
    jar = cookie or seed.cookie
    headers = {"Content-Type": "application/json", "Cookie": f"{jar['name']}={jar['value']}"}
    try:
        conn.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
        response = conn.getresponse()
        raw = response.read()
        try:
            return response.status, json.loads(raw), raw
        except ValueError:
            return response.status, None, raw
    finally:
        conn.close()


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()
