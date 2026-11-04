"""Machine-only chat relay routes under /api/runner/chat/: they carry redacted chat content to the
poster's own runner and answer text back. The card-runner actions in runner_api.py carry no user content.
"""

import json
import logging
import time
from urllib.parse import urlsplit

from commonplace.boards._http import HTTPError, send_json
from commonplace.http_util import log_failure

from . import chat_relay, registry, runner_api
from .registry import CHAT_MODEL

logger = logging.getLogger(__name__)

PREFIX = "/api/runner/chat/"
_BODY_KEYS = {
    "lease": {"clis", "context", "relay_always"},
    "answer": {"job", "lease_token", "text", "cli", "model"},
    "fail": {"job", "lease_token", "code"},
}


def _body(h, action):
    media = (h.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise HTTPError(415, "application/json required")
    try:
        length = int(h.headers.get("Content-Length", "0"))
    except ValueError:
        raise HTTPError(400, "invalid content length") from None
    if length < 0 or length > chat_relay.MAX_BODY_BYTES:
        raise HTTPError(413, "request too large")
    try:
        value = json.loads(h.rfile.read(length).decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPError(400, "invalid JSON") from None
    if not isinstance(value, dict) or value.keys() - _BODY_KEYS[action]:
        raise HTTPError(400, "invalid request body")
    required = {"clis"} if action == "lease" else _BODY_KEYS[action]
    if required - value.keys():
        raise HTTPError(400, "missing required argument")
    if action == "lease":
        clis = value["clis"]
        if (
            not isinstance(clis, list)
            or not 1 <= len(clis) <= 8
            or any(not isinstance(cli, str) or not registry.PROVIDER.fullmatch(cli) for cli in clis)
        ):
            raise HTTPError(400, "invalid clis")
        if value.get("context", "minimal") not in chat_relay.CONTEXTS:
            raise HTTPError(400, "invalid context")
        if not isinstance(value.get("relay_always", False), bool):
            raise HTTPError(400, "invalid relay_always")
    else:
        for key, item in value.items():
            if not isinstance(item, str) or not item:
                raise HTTPError(400, "invalid " + key)
        if action == "answer":
            try:
                size = len(value["text"].encode("utf-8"))
            except UnicodeError:
                raise HTTPError(400, "invalid text") from None
            if size > chat_relay.MAX_ANSWER_BYTES:
                raise HTTPError(400, "invalid text")
            if not registry.PROVIDER.fullmatch(value["cli"]):
                raise HTTPError(400, "invalid cli")
            if not CHAT_MODEL.fullmatch(value["model"]):
                raise HTTPError(400, "invalid model")
        elif value["code"] not in chat_relay.RUNNER_FAIL_CODES:
            raise HTTPError(400, "invalid code")
    return value


def register(gateway) -> None:
    def route(h):
        try:
            principal = gateway.principal_for(h)
            machine = runner_api._machine(gateway, principal)
            if machine is None:
                raise HTTPError(404, "not found")
            machine = dict(machine)
            url = urlsplit(h.path)
            action = url.path.removeprefix(PREFIX)
            if h.command != "POST" or action not in _BODY_KEYS:
                raise HTTPError(404, "not found")
            body = _body(h, action)
            if url.query:
                raise HTTPError(400, "invalid query")
            now = time.time()
            if action == "lease":
                payload = chat_relay.lease(
                    gateway,
                    machine,
                    body["clis"],
                    body.get("context", "minimal"),
                    body.get("relay_always", False),
                    now,
                )
                if payload is None:
                    h.send_response(204)
                    h.send_header("Content-Length", "0")
                    h.send_header("Cache-Control", "no-store")
                    h.end_headers()
                    return
            elif action == "answer":
                payload = chat_relay.answer(gateway, machine, **body, now=now)
            else:
                payload = chat_relay.fail(gateway, machine, **body, now=now)
            send_json(h, 200, payload)
        except HTTPError as exc:
            send_json(h, exc.status, {"error": str(exc)})
        except chat_relay.Fenced:
            send_json(h, 409, {"error": "job fenced"})
        except chat_relay.InvalidReply:
            send_json(h, 400, {"error": "invalid request body"})
        except chat_relay.ReplyBusy:
            send_json(h, 503, {"error": "reply storage unavailable"})
        except Exception as exc:
            log_failure(logger, "chat relay request failed", exc)
            send_json(h, 500, {"error": "internal error"})

    gateway.add_prefix_route("POST", PREFIX, route)
