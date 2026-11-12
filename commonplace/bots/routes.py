"""``/api/bots``: a person's bots, their threads and memory. Person sessions, or the person's own
app holding ``bots:chat`` (``bots:confirm`` for confirm).

    GET    /api/bots                       {bots, model:{ready,label,code}, can_configure}
    POST   /api/bots                       {name, job?, instructions?, board?} -> 201 {bot}
    PATCH  /api/bots/<id>                  {name?, job?, instructions?, archived?} -> {bot}
    GET    /api/bots/<id>/messages         ?after=&before=&limit= -> {bot, messages, waiting, running}
    POST   /api/bots/<id>/messages         {text, request_id} -> text/event-stream
                                           (?stream=0 -> {message, reply} when the turn ends)
    POST   /api/bots/<id>/stop             {request_id} -> {ok}
    POST   /api/bots/<id>/confirm          {pending, accept} -> {message}
    POST   /api/bots/<id>/read             {upto} -> {ok}
    GET    /api/bots/<id>/memory           {memories}
    POST   /api/bots/<id>/memory           {text} -> {memory}
    DELETE /api/bots/<id>/memory/<mid>     {ok}

A bot id that is unknown or belongs to someone else is 404. ``HANDLERS`` maps (method, path
tail) to a handler, so a later route (sending a message) is one more entry.
"""

import json
import queue
import time

from commonplace import bots_grant, redact
from commonplace.boards.service import BoardService
from commonplace.bots import auth, blocks, limits, modelcfg, store, turn, updates
from commonplace.extensions import ToolContext, ToolError
from commonplace.http_util import path_parts, query, read_json, send_json

PREFIX = "/api/bots/"
PREVIEW_CHARS = 200
PING_S = 10  # SSE heartbeat
WRITE_TIMEOUT_S = 10  # one blocked socket write ends the relay, never the turn


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


def _running(principal, bot_id) -> bool:
    return limits.REGISTRY.running(principal.workspace_id, bot_id) is not None


def _working(gateway, principal, bots) -> set[str]:
    """Bots with a turn running, or whose filed work is in progress."""
    return updates.working_bots(gateway, principal, bots) | limits.REGISTRY.running_bots(
        principal.workspace_id, principal.user_id
    )


def _bot(gateway, principal, bot_id) -> dict:
    bots = _bots(gateway, principal)
    return _view(
        bots.roster(principal.user_id, bot_id)[0],
        _asks(gateway, principal),
        _working(gateway, principal, bots),
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
    working = _working(gateway, principal, bots)
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
    turn.recover(gateway, principal.workspace_id, principal.user_id, bot_id)
    page = bots.messages(bot_id, principal.user_id, **cursors)
    reader = blocks.Reader(gateway, principal)
    asks = _asks(gateway, principal)
    bot = bots.roster(principal.user_id, bot_id)[0]
    waiting = [
        {"type": "decision", "card": ask["number"]}
        for ask in asks
        if bot["board"] in (None, ask["board"])
    ]
    running = _running(principal, bot_id)
    send_json(
        h,
        200,
        {
            "bot": _view(bot, asks, _working(gateway, principal, bots)),
            "messages": [blocks.message_view(reader, m) for m in page],
            "waiting": blocks.hydrate(reader, waiting),
            "running": running,
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


# Talking to a bot


def _frame(event, data) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _relay(h, frames) -> None:
    """Write an SSE response from ``frames`` (a list, or a turn queue ending in None).

    A broken pipe or a write blocked for ``WRITE_TIMEOUT_S`` ends only this relay.
    """
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Accel-Buffering", "no")
    h.end_headers()
    try:
        h.connection.settimeout(WRITE_TIMEOUT_S)
        h.wfile.flush()
        items = iter(frames) if isinstance(frames, list) else None
        while True:
            if items is not None:
                item = next(items, None)
            else:
                try:
                    item = frames.get(timeout=PING_S)
                except queue.Empty:
                    h.wfile.write(b": ping\n\n")
                    h.wfile.flush()
                    continue
            if item is None:
                return
            h.wfile.write(_frame(*item))
            h.wfile.flush()
    except (OSError, ValueError):
        return  # the client went away; the turn goes on and its reply is stored


def _final(events) -> dict:
    return next(data["message"] for event, data in events if event in ("done", "error"))


def _send(gateway, h, principal, bot_id, rest):
    started = time.monotonic()
    stream = query(h).get("stream", "1") != "0"
    body = _body(h, {"text", "request_id"})
    if body is None:
        return
    text, request_id = body.get("text"), body.get("request_id")
    if not isinstance(text, str) or len(text) > store.TEXT_MAX or not text.strip():
        raise store.Invalid("text")
    if not isinstance(request_id, str) or store.REQUEST_ID.fullmatch(request_id) is None:
        raise store.Invalid("request_id")
    bots = _bots(gateway, principal)
    bot = bots.get(bot_id, principal.user_id)
    seen = bots.message_for_request(bot_id, principal.user_id, request_id)
    if seen is not None:
        if seen["text"] != text:
            raise store.Conflict()
        _replay(gateway, h, principal, bot_id, seen, stream)
        return
    run = turn.TurnRun(gateway, principal, bot, request_id, started)
    try:
        limits.REGISTRY.admit(run)
    except limits.Busy as busy:
        _error(h, busy.status, "busy", "Busy, try again")
        return
    try:
        # A same-id request may have finished between the first look and admission.
        seen = bots.message_for_request(bot_id, principal.user_id, request_id)
        if seen is None:
            bots.lease_start(bot_id, principal.user_id, request_id)
            message = bots.add_user_message(bot_id, principal.user_id, text, request_id)
    except BaseException:
        bots.lease_end(bot_id, request_id)
        limits.REGISTRY.release(run)
        raise
    if seen is not None:
        limits.REGISTRY.release(run)
        if seen["text"] != text:
            raise store.Conflict()
        _replay(gateway, h, principal, bot_id, seen, stream)
        return
    run.start(message)
    if stream:
        _relay(h, run.queue)
        return
    run.finished.wait()
    if run.reply is None:  # the reply could not be stored; the stream would say the same
        text = turn.SENTENCES["model_error"]
        send_json(h, 500, {"error": text, "code": "model_error", "message": run.view(message)})
        return
    send_json(h, 200, {"message": run.view(message), "reply": run.view(run.reply)})


def _replay(gateway, h, principal, bot_id, message, stream):
    events = turn.replay(gateway, principal, bot_id, message)
    if stream:
        _relay(h, events)
        return
    running = limits.REGISTRY.running(principal.workspace_id, bot_id)
    if events[-1][0] == "running" and running is not None:
        running.finished.wait()
        events = turn.replay(gateway, principal, bot_id, message)
    if events[-1][0] == "running":
        _error(h, 409, "busy", "Still running")
        return
    send_json(h, 200, {"message": events[0][1]["message"], "reply": _final(events)})


def _stop(gateway, h, principal, bot_id, rest):
    body = _body(h, {"request_id"})
    if body is None:
        return
    _bots(gateway, principal).get(bot_id, principal.user_id)
    running = limits.REGISTRY.running(principal.workspace_id, bot_id)
    stopping = running is not None and running.request_id == body.get("request_id")
    if stopping:
        running.cancel.set()
    send_json(h, 200, {"ok": stopping})


def _confirm(gateway, h, principal, bot_id, rest):
    body = _body(h, {"pending", "accept"})
    if body is None:
        return
    if not isinstance(body.get("pending"), str) or type(body.get("accept")) is not bool:
        raise store.Invalid("pending")
    try:
        message = turn.confirm(gateway, principal, bot_id, body["pending"], body["accept"])
    except turn.Refused as refused:
        if refused.code == "expired":
            _error(h, 409, "expired", "This request expired. Ask again.")
        else:
            _error(h, 409, "conflict", "This was already answered.")
        return
    reader = blocks.Reader(gateway, principal)
    send_json(h, 200, {"message": blocks.message_view(reader, message)})


# (method, first segment after the id) -> handler(gateway, h, principal, bot_id, rest)
HANDLERS = {
    ("PATCH", None): _patch,
    ("GET", "messages"): _messages,
    ("POST", "messages"): _send,
    ("POST", "stop"): _stop,
    ("POST", "confirm"): _confirm,
    ("POST", "read"): _read,
    ("GET", "memory"): _memory_list,
    ("POST", "memory"): _memory_add,
    ("DELETE", "memory"): _memory_remove,
}


def _audit(gateway, h, app, tool, status):
    """One row per write request made through an app connection: ids and the outcome only."""
    gateway.db.audit(
        action=bots_grant.AUDIT_WRITE,
        decision="ok" if status < 400 else "refused" if status < 500 else "error",
        workspace_id=app.workspace_id,
        user_id=app.user_id,
        connection_id=app.connection_id,
        auth=app.auth,
        tool=tool,
    )


def _guarded(gateway, h, run, tool="", scope=None):
    principal = auth.person(gateway, h)
    if principal is None:
        return
    app = auth.app(h)
    recorded = []
    if app is not None and h.command != "GET":
        send_response = h.send_response

        def remember(code, *args):
            # Recorded when the status is sent, so the row exists before the client sees it.
            if not recorded:
                recorded.append(code)
                _audit(gateway, h, app, tool, code)
            return send_response(code, *args)

        h.send_response = remember
    try:
        if app is not None and scope and not app.policy.has_scope(scope):
            _error(h, 403, "scope_required", "This app needs another permission for that.")
            return
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
    finally:
        if app is not None and h.command != "GET" and not recorded:
            recorded.append(500)  # died before answering; the gateway's 500 adds no second row
            _audit(gateway, h, app, tool, 500)


# Tails that need more than bots:chat from an app connection.
SCOPES = {("POST", "confirm"): bots_grant.CONFIRM_SCOPE}


def install(gateway) -> None:
    def dispatch(method):
        def handle(h):
            parts = path_parts(h, PREFIX)
            tail = parts[1] if len(parts) > 1 else None
            handler = HANDLERS.get((method, tail)) if parts else None
            if handler is None:
                return _guarded(
                    gateway, h, lambda _principal: _error(h, 404, "not_found", "Not found")
                )
            _guarded(
                gateway,
                h,
                lambda principal: handler(gateway, h, principal, parts[0], parts[2:]),
                tool=f"{method} {tail or 'bot'}",
                scope=SCOPES.get((method, tail)),
            )

        return handle

    gateway.add_route(
        "GET", "/api/bots", lambda h: _guarded(gateway, h, lambda p: _listing(gateway, h, p))
    )
    gateway.add_route(
        "POST",
        "/api/bots",
        lambda h: _guarded(gateway, h, lambda p: _create(gateway, h, p), tool="POST bots"),
    )
    for method in ("GET", "POST", "PATCH", "DELETE"):
        gateway.add_prefix_route(method, PREFIX, dispatch(method))
