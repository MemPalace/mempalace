"""``/api/bots``: a person's bots, their threads and memory. Person sessions only.

    GET    /api/bots                       {bots, model:{ready,label,code}, can_configure}
    POST   /api/bots                       {name, job?, instructions?, board?} -> 201 {bot}
    PATCH  /api/bots/<id>                  {name?, job?, instructions?, archived?} -> {bot}
    GET    /api/bots/<id>/messages         ?after=&before=&limit= -> {bot, messages, waiting, running}
    POST   /api/bots/<id>/read             {upto} -> {ok}
    GET    /api/bots/<id>/memory           {memories}
    POST   /api/bots/<id>/memory           {text} -> {memory}
    DELETE /api/bots/<id>/memory/<mid>     {ok}

A bot id that is unknown or belongs to someone else is 404. ``HANDLERS`` maps (method, path
tail) to a handler, so a later route (sending a message) is one more entry.
"""

from commonplace import redact
from commonplace.boards.service import BoardService
from commonplace.bots import auth, blocks, modelcfg, store, updates
from commonplace.extensions import ToolContext, ToolError
from commonplace.http_util import path_parts, query, read_json, send_json

PREFIX = "/api/bots/"
PREVIEW_CHARS = 200


def _error(h, status, code, text):
    send_json(h, status, {"error": text, "code": code})


def _bots(gateway, principal):
    return store.open_bots(gateway, principal.workspace_id)


def _asks(gateway, principal):
    """This person's open decisions across every project they can read."""
    try:
        return BoardService(ToolContext(principal=principal, gateway=gateway)).needs_me()["asks"]
    except ToolError:
        return []  # a member without the needs-me tool has none waiting


def _view(bot, asks, working=()) -> dict:
    waiting = sum(1 for ask in asks if bot["board"] in (None, ask["board"]))
    last = bot["last"]
    return {
        **{key: bot[key] for key in ("id", "name", "job", "instructions", "kind", "board")},
        "archived": bot["archived"],
        "last": None
        if last is None
        else {
            "role": last["role"],
            "text": redact.redact(last["text"])[0][:PREVIEW_CHARS],
            "at": last["at"],
        },
        "unread": bot["unread"],
        "waiting": waiting,
        "state": "needs_you" if waiting else "working" if bot["id"] in working else "idle",
    }


def _bot(gateway, principal, bot_id) -> dict:
    bots = _bots(gateway, principal)
    return _view(
        bots.roster(principal.user_id, bot_id)[0],
        _asks(gateway, principal),
        updates.working_bots(gateway, principal, bots),
    )


def _model(gateway, principal) -> dict:
    config, _key, code = modelcfg.resolve(gateway.db, principal.workspace_id)
    return {
        "ready": not code,
        "label": f"{config.provider}/{config.model}" if config else "",
        "code": code,
    }


def _listing(gateway, h, principal):
    boards = BoardService(ToolContext(principal=principal, gateway=gateway)).board_list()
    bots = _bots(gateway, principal)
    bots.ensure_defaults(principal.user_id, [(b["slug"], b["name"]) for b in boards])
    asks = _asks(gateway, principal)
    working = updates.working_bots(gateway, principal, bots)
    send_json(
        h,
        200,
        {
            "bots": [_view(bot, asks, working) for bot in bots.roster(principal.user_id)],
            "model": _model(gateway, principal),
            "can_configure": principal.role == "owner",
        },
    )


def _body(h, allowed):
    """The JSON body, or None after a 400 for a malformed one; unknown fields are invalid."""
    try:
        body = read_json(h)
    except ValueError:
        return None
    if set(body) - allowed:
        raise store.Invalid("unknown field")
    return body


def _create(gateway, h, principal):
    body = _body(h, {"name", "job", "instructions", "board"})
    if body is None:
        return
    board = body.get("board")
    if board is not None:
        if not isinstance(board, str):
            raise store.Invalid("invalid board")
        service = BoardService(ToolContext(principal=principal, gateway=gateway))
        if board not in {b["slug"] for b in service.board_list()}:
            _error(h, 400, "invalid", "Unknown project")
            return
    bot = _bots(gateway, principal).create(
        principal.user_id,
        body.get("name"),
        body.get("job", ""),
        body.get("instructions", ""),
        board=board,
    )
    send_json(h, 201, {"bot": _bot(gateway, principal, bot["id"])})


def _patch(gateway, h, principal, bot_id, rest):
    if rest:
        _error(h, 404, "not_found", "Not found")
        return
    body = _body(h, {"name", "job", "instructions", "archived"})
    if body is None:
        return
    _bots(gateway, principal).update(bot_id, principal.user_id, **body)
    send_json(h, 200, {"bot": _bot(gateway, principal, bot_id)})


def _messages(gateway, h, principal, bot_id, rest):
    params = query(h)
    try:
        cursors = {key: int(params[key]) for key in ("after", "before", "limit") if key in params}
    except ValueError:
        raise store.Invalid("invalid cursor") from None
    bots = _bots(gateway, principal)
    page = bots.messages(bot_id, principal.user_id, **cursors)
    reader = blocks.Reader(gateway, principal)
    asks = _asks(gateway, principal)
    bot = bots.roster(principal.user_id, bot_id)[0]
    waiting = [
        {"type": "decision", "card": ask["number"]}
        for ask in asks
        if bot["board"] in (None, ask["board"])
    ]
    send_json(
        h,
        200,
        {
            "bot": _view(bot, asks, updates.working_bots(gateway, principal, bots)),
            "messages": [
                {
                    "id": m["id"],
                    "role": m["role"],
                    "text": redact.redact(m["text"])[0],
                    "blocks": blocks.hydrate(reader, m["blocks"]),
                    "at": m["at"],
                    "state": m["state"],
                    "reply_to": m["reply_to"],
                }
                for m in page
            ],
            "waiting": blocks.hydrate(reader, waiting),
            "running": False,
        },
    )


def _read(gateway, h, principal, bot_id, rest):
    body = _body(h, {"upto"})
    if body is None:
        return
    _bots(gateway, principal).mark_read(bot_id, principal.user_id, body.get("upto"))
    send_json(h, 200, {"ok": True})


def _note(memory) -> dict:
    return {**memory, "text": redact.redact(memory["text"])[0]}


def _memory_list(gateway, h, principal, bot_id, rest):
    notes = _bots(gateway, principal).memory_list(bot_id, principal.user_id)
    send_json(h, 200, {"memories": [_note(n) for n in notes]})


def _memory_add(gateway, h, principal, bot_id, rest):
    body = _body(h, {"text"})
    if body is None:
        return
    memory = _bots(gateway, principal).memory_add(bot_id, principal.user_id, body.get("text"))
    send_json(h, 200, {"memory": _note(memory)})


def _memory_remove(gateway, h, principal, bot_id, rest):
    try:
        memory_id = int(rest[0]) if len(rest) == 1 else None
    except ValueError:
        memory_id = None
    if not _bots(gateway, principal).memory_remove(bot_id, principal.user_id, memory_id):
        _error(h, 404, "not_found", "Not found")
        return
    send_json(h, 200, {"ok": True})


# (method, first segment after the id) -> handler(gateway, h, principal, bot_id, rest)
HANDLERS = {
    ("PATCH", None): _patch,
    ("GET", "messages"): _messages,
    ("POST", "read"): _read,
    ("GET", "memory"): _memory_list,
    ("POST", "memory"): _memory_add,
    ("DELETE", "memory"): _memory_remove,
}


def _guarded(gateway, h, run):
    principal = auth.person(gateway, h)
    if principal is None:
        return
    try:
        run(principal)
    except store.Missing:
        _error(h, 404, "not_found", "Not found")
    except store.Invalid:
        _error(h, 400, "invalid", "Invalid request")
    except store.Limit:
        _error(h, 409, "limit", f"At most {store.MAX_LIVE_BOTS} bots")
    except store.Conflict:
        _error(h, 409, "conflict", "Conflict")
    except TimeoutError:
        _error(h, 429, "busy", "Busy, try again")


def install(gateway) -> None:
    def dispatch(method):
        def handle(h):
            parts = path_parts(h, PREFIX)
            handler = (
                HANDLERS.get((method, parts[1] if len(parts) > 1 else None)) if parts else None
            )
            if handler is None:
                return _guarded(
                    gateway, h, lambda _principal: _error(h, 404, "not_found", "Not found")
                )
            _guarded(
                gateway, h, lambda principal: handler(gateway, h, principal, parts[0], parts[2:])
            )

        return handle

    gateway.add_route(
        "GET", "/api/bots", lambda h: _guarded(gateway, h, lambda p: _listing(gateway, h, p))
    )
    gateway.add_route(
        "POST", "/api/bots", lambda h: _guarded(gateway, h, lambda p: _create(gateway, h, p))
    )
    for method in ("GET", "POST", "PATCH", "DELETE"):
        gateway.add_prefix_route(method, PREFIX, dispatch(method))
