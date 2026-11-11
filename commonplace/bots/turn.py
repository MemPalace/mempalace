"""One bot turn: the model streams, tools run as the person, the reply is stored once.

A turn runs on its own thread and reports through its queue; the HTTP handler only relays that
queue, so a dropped client never stops a turn. No lock, admission gate or database handle is
held while the provider is awaited. Every tool call is journaled in ``bot_actions`` (``started``
before it runs, finished after), so a crash leaves a record and nothing is ever rerun.

Queue items are ``(event, data)`` pairs, then ``None`` once the reply is stored.
"""

import contextlib
import hashlib
import json
import logging
import queue
import re
import threading
import time

from commonplace import redact, sessions
from commonplace.boards.service import BoardService
from commonplace.bots import blocks, context, limits, modelcfg, store, tools
from commonplace.chat import model_stream
from commonplace.chat.types import ChatDenied, ModelError
from commonplace.extensions import ToolContext, ToolError
from commonplace.http_util import log_failure

logger = logging.getLogger(__name__)

# Fixed sentences per outcome; provider text never reaches the person.
SENTENCES = {
    "needs_configuration": "No chat model is set up for this bot yet.",
    "limit": "The daily limit for model requests is used up. Try again tomorrow.",
    "rate": "Too many requests in the last minute. Try again in a moment.",
    "model_error": "The chat model did not answer properly. Try again.",
    "timeout": "The chat model took too long to answer. Try again.",
    "stopped": "Stopped.",
    "interrupted": "This reply was interrupted. Check what was done before you ask again.",
    "steps": "That needed too many steps, so I stopped here.",
    "too_long": "That is too much for the chat model to read at once. Send a shorter message.",
}
REFUSALS = {
    "not_your_words": "That text is not in your message. Only your own words can be written.",
    "invalid": "That request was not valid.",
    "gone": "You no longer have access to that.",
    "steps": "Step limit reached; this was not done.",
    "stopped": "Stopped before this ran.",
    "failed": "That did not work.",
    "unreadable": "I could not read that item, so nothing is waiting for your OK.",
}
LEASE_S = 20  # a turn whose lease is older than this, in any process, is not running
BEAT_S = 5
# A line carrying one of these may hold a secret whose value spans spaces (a header, a
# key assignment, a quoted value, a private key block): it is streamed only once complete.
_HOLD_LINE = re.compile(
    r"(?i)-----|[:=]\s{0,4}[\"']|(?:authorization|cookie|x-api-key|x-auth-token|private-token)"
    r"\s{0,4}:|" + redact.KEY + r"\\?[\"']?\s{0,4}[:=]"
)


def safe_end(text) -> int:
    """How much of redacted ``text`` can be streamed now without a later change to it.

    Only whole words go out (no redaction pattern crosses a space without a label), and a
    line with a label is held until its end.
    """
    line = text.rfind("\n") + 1
    if _HOLD_LINE.search(text, line):
        return line
    return max(text.rfind(" "), text.rfind("\t"), text.rfind("\n")) + 1


def _hash(args) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()


def _action(tool, label, ok, code=None) -> dict:
    block = {"type": "action", "tool": tool, "label": label, "ok": ok}
    if code:
        block["code"] = code
    return block


def code_of(state) -> str:
    return state.split(":", 1)[1] if state.startswith("failed:") else state


class TurnRun:
    """A running turn. Create, :meth:`admit` through the registry, then :meth:`start`."""

    def __init__(self, gateway, principal, bot, request_id, started=None):
        self.gateway = gateway
        self.workspace_id = principal.workspace_id
        self.user_id = principal.user_id
        self.bot = bot
        self.bot_id = bot["id"]
        self.request_id = request_id
        self.started = time.monotonic() if started is None else started
        self.queue = queue.Queue()
        self.cancel = threading.Event()
        self.finished = threading.Event()
        self.message = None
        self.reply = None
        self.first_token_ms = None
        self._text = []
        self._sent = ""  # redacted reply text already streamed
        self._held = False  # the redacted text changed under what was sent; send no more
        self._blocks = []
        self._model = None
        self._reader = blocks.Reader(gateway, principal)

    def _put(self, event, data) -> None:
        self.queue.put((event, data))

    def view(self, message) -> dict:
        return blocks.message_view(self._reader, message)

    def start(self, message) -> None:
        """``ack`` is queued here, before any model work, then the turn thread starts."""
        self.message = message
        self._put("ack", {"message": self.view(message)})
        threading.Thread(target=self._run, name="bot-turn", daemon=True).start()

    # The turn

    def _beat(self) -> None:
        bots = store.open_bots(self.gateway, self.workspace_id)
        while not self.finished.wait(BEAT_S):
            try:
                bots.lease_beat(self.bot_id, self.request_id)
            except Exception as exc:
                log_failure(logger, "Bot turn lease not renewed", exc)

    def _run(self) -> None:
        threading.Thread(target=self._beat, name="bot-turn-lease", daemon=True).start()
        outcome = "model_error"
        try:
            outcome = self._turn()
        except Exception as exc:
            log_failure(logger, "Bot turn failed", exc)
        try:
            self._finish(outcome)
        except Exception as exc:
            log_failure(logger, "Bot reply not stored", exc)
            self._put("error", {"code": "model_error", "text": SENTENCES["model_error"]})
        finally:
            try:
                store.open_bots(self.gateway, self.workspace_id).lease_end(
                    self.bot_id, self.request_id
                )
            except Exception as exc:
                log_failure(logger, "Bot turn lease not ended", exc)
            limits.REGISTRY.release(self)
            self.finished.set()
            self.queue.put(None)

    def _turn(self) -> str:
        bots = store.open_bots(self.gateway, self.workspace_id)
        config, key, setup = modelcfg.resolve(self.gateway.db, self.workspace_id, self.bot["board"])
        if setup:
            self._blocks.append({"type": "setup", "code": setup})
            return "needs_configuration"
        self._model = f"{config.provider}/{config.model}"
        principal = sessions.person_principal(self.gateway.db, self.user_id, self.workspace_id)
        if principal is None:
            return "interrupted"
        service = BoardService(ToolContext(principal=principal, gateway=self.gateway))
        projects = None if self.bot["board"] else [b["slug"] for b in service.board_list()]
        turn = tools.Turn(
            self.gateway,
            self.workspace_id,
            self.user_id,
            self.bot,
            self.message,
            self.request_id,
            projects=projects,
        )
        history = bots.messages(self.bot_id, self.user_id, before=self.message["id"])
        memory = bots.memory_list(self.bot_id, self.user_id)
        ctx = context.build(turn, principal, service, history, memory)
        tail = []  # this turn's tool rounds, fed back on later requests
        offered = {tool.name: tool for tool in tools.offered(turn)}
        specs = [
            {"name": t.name, "description": t.description, "schema": tools.schema_for(t, turn)}
            for t in offered.values()
        ]
        deadline = self.started + limits.BOT_TURN_TIMEOUT_S
        steps = 0
        while True:
            if self.cancel.is_set():
                return "stopped"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            messages = ctx.messages(specs, tail)
            if messages is None:
                return "too_long"  # before the first request: nothing was sent
            refused, day = limits.take(self.gateway, config, self.workspace_id, self.user_id)
            if refused:
                return refused
            sent = []
            try:
                calls, said = self._model_call(
                    config, key, ctx.system, messages, specs, remaining, sent
                )
            except ModelError as exc:
                return {"stopped": "stopped", "timeout": "timeout"}.get(exc.code, "model_error")
            except ChatDenied:
                self._blocks.append({"type": "setup", "code": "key_missing"})
                return "needs_configuration"
            except ValueError:
                return "model_error"  # input over the bound, or the provider is unusable
            finally:
                if not sent:  # the unit is for a request sent; this one never was
                    limits.give_back(self.gateway, self.workspace_id, self.user_id, day)
            if not calls:
                return ""
            tail.append({"role": "assistant", "content": said, "tool_calls": calls})
            over = False
            for call in calls:
                if self.cancel.is_set():
                    result, code = tools.Result(False, say=REFUSALS["stopped"]), "stopped"
                elif steps >= limits.BOT_MAX_STEPS:
                    result, code, over = tools.Result(False, say=REFUSALS["steps"]), "steps", True
                else:
                    steps += 1
                    turn.step = steps
                    result, code = self._tool(turn, offered[call["name"]], call["arguments"])
                tail.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": context.tool_result(result, code),
                    }
                )
            if over:
                return "steps"

    def _model_call(self, config, key, system, messages, specs, remaining, sent):
        events = model_stream.stream(
            config,
            key,
            system,
            messages,
            tools=specs,
            timeout=min(limits.BOT_CALL_TIMEOUT_S, remaining),
            max_tokens=limits.BOT_MAX_OUTPUT_TOKENS,
            max_input_chars=limits.BOT_MAX_INPUT_CHARS,
            max_response_bytes=limits.BOT_MAX_RESPONSE_BYTES,
            on_sent=lambda: sent.append(True),
            cancel=self.cancel,
        )
        said, calls = [], []
        with contextlib.closing(events):
            for kind, value in events:
                if kind == "text":
                    self._delta(value)
                    said.append(value)
                elif kind == "tool_call":
                    calls.append(value)
        return calls, "".join(said)

    def _delta(self, text) -> None:
        if text:
            self._text.append(text)
            self._flush()

    def _redacted(self) -> str:
        return redact.redact("".join(self._text)[: limits.BOT_MAX_REPLY_CHARS])[0]

    def _flush(self, final=False) -> None:
        """Stream the part of the redacted reply that cannot change any more.

        The whole text is redacted each time, so a secret split across chunks is caught; at
        the end the client holds exactly the stored reply text.
        """
        if self._held:
            return
        text = self._redacted()
        safe = text if final else text[: safe_end(text)]
        if not safe.startswith(self._sent):
            self._held = True
            logger.warning("Bot reply stream held back (code=redaction_shift)")
            return
        if len(safe) > len(self._sent):
            if self.first_token_ms is None:
                self.first_token_ms = int((time.monotonic() - self.started) * 1000)
            self._put("delta", {"text": safe[len(self._sent) :]})
            self._sent = safe

    def _tool(self, turn, tool, args):
        """Run one tool call: journal, events, audit. ``(Result, refusal code or None)``."""
        bots = turn.store()
        step = turn.step
        bots.action_start(self.bot_id, self.user_id, self.request_id, step, tool.name, _hash(args))
        label, code = f"Used {tool.name}", None
        try:
            args = tools.validate(tools.schema_for(tool, turn), args)
            if tool.scope == "project":
                turn.board(args)
            if tool.check is not None:
                tool.check(turn, args)  # before a pending action, a chip or any write
            label = tool.label(turn, args)
            self._put(
                "action", {"step": step, "tool": tool.name, "label": label, "state": "running"}
            )
            if tool.kind == "confirm":
                result, code = self._pending(turn, tool, args, label)
            else:
                result = tool.run(turn, args)
        except tools.NotYourWords:
            result, code = tools.Result(False, say=REFUSALS["not_your_words"]), "not_your_words"
        except tools.Invalid:
            result, code = tools.Result(False, say=REFUSALS["invalid"]), "invalid"
        except tools.Gone:
            result, code = tools.Result(False, say=REFUSALS["gone"]), "gone"
        except ToolError as exc:
            result, code = tools.Result(False, say=redact.redact(str(exc))[0][:500]), "refused"
        except Exception as exc:
            log_failure(logger, "Bot tool failed", exc)
            result, code = tools.Result(False, say=REFUSALS["failed"]), "failed"
        if tool.kind == "routine" or (tool.kind == "confirm" and not result.ok):
            turn.audit(tool.name, "ok" if result.ok else "deny")
        shown = list(result.blocks)
        if tool.kind != "confirm" or not result.ok:
            shown.insert(0, _action(tool.name, label, result.ok, code))
        bots.action_finish(
            self.bot_id,
            self.user_id,
            self.request_id,
            step,
            "ok" if result.ok else "refused",
            shown,
        )
        event = {"step": step, "tool": tool.name, "label": label}
        self._put("action", {**event, "state": "ok" if result.ok else "refused", "code": code})
        for block in result.blocks:
            self._put("block", self._hydrated(block))
        self._blocks.extend(shown)
        return result, code

    def _hydrated(self, block) -> dict:
        try:
            return blocks.hydrate(self._reader, [block])[0]
        except Exception as exc:
            log_failure(logger, "Bot block not hydrated", exc)
            return block

    def _pending(self, turn, tool, args, label):
        """A confirm tool: freeze the call (with the item's version) and wait for a tap."""
        version = None
        if type(args.get("ref")) is int:
            try:
                version = turn.boards().task_show(args["ref"])["version"]
            except ToolError:
                return tools.Result(False, say=REFUSALS["unreadable"]), "unreadable"
        frozen = {
            "args": args,
            "version": version,
            "message": self.message["id"],
            "request_id": self.request_id,
            "step": turn.step,
        }
        pending = turn.store().pending_create(self.bot_id, self.user_id, tool.name, frozen, label)
        return tools.Result(
            True,
            {"waiting_for_confirmation": True},
            blocks=({"type": "confirm", "pending": pending["id"]},),
        ), None

    def _finish(self, outcome) -> None:
        self._flush(final=True)
        text = self._redacted()
        code = outcome
        if outcome and outcome != "stopped":
            text = (text + "\n\n" if text else "") + SENTENCES[outcome]
        state = "ok" if not outcome else "stopped" if outcome == "stopped" else f"failed:{code}"
        bots = store.open_bots(self.gateway, self.workspace_id)
        try:
            self.reply = bots.add_bot_message(
                self.bot_id,
                self.user_id,
                self.message["id"],
                text,
                self._blocks,
                model=self._model,
                state=state,
            )
        except store.Conflict:
            # Another process closed this request (as interrupted) first; report what is stored.
            logger.warning("Bot reply arrived after its request was closed (code=late_reply)")
            self.reply = bots.reply_to(self.bot_id, self.user_id, self.message["id"])
            if self.reply is None:
                raise
            outcome = code = "" if self.reply["state"] == "ok" else code_of(self.reply["state"])
        view = self.view(self.reply)
        if not outcome:
            total = int((time.monotonic() - self.started) * 1000)
            timing = {"first_token_ms": self.first_token_ms, "total_ms": total}
            self._put("done", {"message": view, "timing": timing})
        else:
            text = SENTENCES.get(outcome, SENTENCES["model_error"])
            self._put("error", {"code": code, "text": text, "message": view})


# Replay and interrupted turns


def interrupted(gateway, workspace_id, user_id, bot_id, message) -> dict | None:
    """Store ``failed:interrupted`` for a request with no reply and no running turn.

    A turn is running while this process holds it or its store lease is fresh (another
    process may be running it).

    Never reruns anything: the journaled actions are reported, a ``started`` one as
    "outcome unknown". Returns the reply (an existing one if another writer won), or None
    while the turn is still running.
    """
    bots = store.open_bots(gateway, workspace_id)
    if limits.REGISTRY.holding(workspace_id, bot_id, message["request_id"]) or bots.leased(
        bot_id, message["request_id"], LEASE_S
    ):
        return None  # running here, or in another process that still renews its lease
    shown = []
    for action in bots.actions(bot_id, user_id, message["request_id"]):
        if action["state"] == "started":
            shown.append(
                _action(action["tool"], f"Outcome unknown: {action['tool']}", False, "unknown")
            )
        else:
            shown.extend(action["blocks"])
    try:
        return bots.add_bot_message(
            bot_id,
            user_id,
            message["id"],
            SENTENCES["interrupted"],
            shown,
            state="failed:interrupted",
        )
    except store.Conflict:
        return bots.reply_to(bot_id, user_id, message["id"])


def recover(gateway, workspace_id, user_id, bot_id) -> None:
    """Report every interrupted request of this bot (on reading the thread)."""
    bots = store.open_bots(gateway, workspace_id)
    for message in bots.unanswered(bot_id, user_id):
        interrupted(gateway, workspace_id, user_id, bot_id, message)


def replay(gateway, principal, bot_id, message) -> list[tuple[str, dict]]:
    """The events of a request already seen: ``ack`` then ``done``, ``error`` or ``running``."""
    reader = blocks.Reader(gateway, principal)
    bots = store.open_bots(gateway, principal.workspace_id)
    events = [("ack", {"message": blocks.message_view(reader, message)})]
    reply = bots.reply_to(bot_id, principal.user_id, message["id"])
    if reply is None:
        reply = interrupted(gateway, principal.workspace_id, principal.user_id, bot_id, message)
    if reply is None:
        return [*events, ("running", {})]
    view = blocks.message_view(reader, reply)
    if reply["state"] == "ok":
        timing = {"first_token_ms": None, "total_ms": None}
        return [*events, ("done", {"message": view, "timing": timing})]
    code = code_of(reply["state"])
    text = SENTENCES.get(code, SENTENCES["model_error"])
    return [*events, ("error", {"code": code, "text": text, "message": view})]


# Confirmed actions


class Refused(Exception):
    """A confirm that cannot run: ``code`` is ``expired`` or ``conflict``."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def confirm(gateway, principal, bot_id, pending_id, accept) -> dict:
    """Run exactly the stored action as the person (or cancel it); the stored event message.

    ``open -> done`` (or ``cancelled``) is a compare-and-set, so it runs at most once.
    """
    bots = store.open_bots(gateway, principal.workspace_id)
    pending = bots.pending_get(bot_id, principal.user_id, pending_id)
    if pending is None or pending["user_id"] != principal.user_id:
        raise store.Missing()
    tool = tools.REGISTRY.get(pending["tool"])
    if not accept:
        if not bots.pending_resolve(bot_id, principal.user_id, pending_id, "cancelled"):
            raise Refused(_closed(bots, bot_id, principal.user_id, pending_id))
        block = _action(pending["tool"], pending["label"], False, "cancelled")
        return bots.add_event_message(bot_id, principal.user_id, "Cancelled.", [block])
    if not bots.pending_resolve(bot_id, principal.user_id, pending_id, "done"):
        raise Refused(_closed(bots, bot_id, principal.user_id, pending_id))
    frozen = pending["args"]
    bot = bots.get(bot_id, principal.user_id)
    message = next(
        (
            m
            for m in bots.messages(bot_id, principal.user_id, after=frozen["message"] - 1, limit=1)
            if m["id"] == frozen["message"]
        ),
        None,
    )
    turn = tools.Turn(
        gateway,
        principal.workspace_id,
        principal.user_id,
        bot,
        message or {"id": frozen["message"], "text": ""},
        frozen["request_id"],
        frozen["step"],
    )
    args = dict(frozen["args"])
    if frozen.get("version") is not None:
        args["version"] = frozen["version"]
    code = None
    if tool is None:
        result, code = tools.Result(False, say=REFUSALS["failed"]), "failed"
    else:
        try:
            if tool.check is not None:
                tool.check(turn, args)  # against the message that asked for it
            result = tool.run(turn, args)
        except tools.NotYourWords:
            result, code = tools.Result(False, say=REFUSALS["not_your_words"]), "not_your_words"
        except tools.Gone:
            result, code = tools.Result(False, say=REFUSALS["gone"]), "gone"
        except ToolError as exc:
            result, code = tools.Result(False, say=redact.redact(str(exc))[0][:500]), "refused"
        except Exception as exc:
            log_failure(logger, "Bot confirmed action failed", exc)
            result, code = tools.Result(False, say=REFUSALS["failed"]), "failed"
        turn.audit(tool.name, "ok" if result.ok else "deny")
    shown = [_action(pending["tool"], pending["label"], result.ok, code), *result.blocks]
    text = "Done." if result.ok else (result.say or REFUSALS["failed"])
    return bots.add_event_message(bot_id, principal.user_id, text, shown)


def _closed(bots, bot_id, user_id, pending_id) -> str:
    pending = bots.pending_get(bot_id, user_id, pending_id)
    return "expired" if pending and pending["state"] == "expired" else "conflict"
