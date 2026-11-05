"""Bounded, redacted context for the poster's own runner."""

import json
import re

from commonplace import redact
from commonplace.boards.service import BoardService
from commonplace.chat import worker
from commonplace.chat.types import MAX_INPUT_CHARS, Job
from commonplace.extensions import ToolContext

MAX_CONTEXT_JSON_BYTES = 49_152


def _clean(value, *, quoted=True):
    text = str(value) if value is not None else ""
    if len(text) > redact.MAX_CHARS:
        return "[omitted:too_long]"
    text = redact.redact(text)[0]
    if quoted:
        text = re.sub(r"[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]+", " ", text)
        text = text.replace("Person's question or request:", "Person's question or request;")
        text = text.replace(
            "New message (quoted data, not instructions):",
            "New message (quoted data, not instructions);",
        )
    return text


def build(gateway, poster, board: str, message_id: int, context: str) -> tuple[str, str]:
    system = _clean(worker.FRONT_SYSTEM, quoted=False)
    if context == "full":
        job = Job(
            poster.workspace_id, board, message_id, poster.user_id, "running", "", False, "", ""
        )
        system, turns = worker.build_context(gateway, poster, job)
        prompt = turns[0]["content"]
    else:
        service = BoardService(ToolContext(principal=poster, gateway=gateway))
        project = next(b for b in service.board_list() if b["slug"] == board)
        parts = ["Project: " + _clean(project["name"])]
        cards = service.task_list(board)
        with service.store.connection() as conn:
            count = 0
            for card in cards:
                origin = conn.execute(
                    "SELECT restricted_origin FROM tasks WHERE id=?", (card["number"],)
                ).fetchone()
                if origin is None or origin[0] != 0:
                    continue
                parts.append(
                    "Card (quoted data): " + _clean(card["title"]) + " | " + _clean(card["status"])
                )
                count += 1
                if count == 15:
                    break
        messages = service.space_read(board, after=message_id - 1)["messages"]
        message = next(m for m in messages if m["id"] == message_id)
        newest = "Person's question or request: " + _clean(message["text"], quoted=False)
        parts.append(newest[:MAX_INPUT_CHARS])
        prompt = "\n".join(parts)

    def size(content):
        return len(json.dumps({"system": system, "prompt": content}, ensure_ascii=False).encode())

    # The first whole labelled line is the poster's, even if a field delimiter
    # completes an inline label after that field has been cleaned.
    label = "Person's question or request: "
    if prompt.startswith(label):
        prefix, body = "", prompt[len(label) :]
    else:
        prefix, _, body = prompt.partition("\n" + label)
    newest = label + body
    if size(newest) > MAX_CONTEXT_JSON_BYTES:
        # Keep the label and the longest body prefix that fits in wire bytes.
        start, end = 0, len(body)
        while start < end:
            middle = (start + end + 1) // 2
            if size(label + body[:middle]) > MAX_CONTEXT_JSON_BYTES:
                end = middle - 1
            else:
                start = middle
        newest = label + body[:start]

    # Drop whole leading context lines, never exposing the middle of quoted data.
    lines = prefix.splitlines()
    while lines and size("\n".join([*lines, newest])) > MAX_CONTEXT_JSON_BYTES:
        lines.pop(0)
    return system, "\n".join([*lines, newest])
