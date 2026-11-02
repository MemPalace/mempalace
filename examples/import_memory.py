"""Upload one local conversation or a ChatGPT/Claude.ai conversation array."""

import argparse
import hashlib
import json
import os
import re
import sys
from io import StringIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_IMPORT_BYTES = 64 * 1024 * 1024
_RECORD_END = re.compile(rb"[\r\n]")


def serialize(source, identity, fmt, transcript, part=None):
    item = {"thread_id": identity, "transcript": transcript, "format": fmt}
    if part is not None:
        item["part"] = part
    return json.dumps({"source": source, "threads": [item]}, ensure_ascii=False).encode("utf-8")


def binary_records(stream, maximum, block_size=65536):
    """Bounded physical-record scanner; CRLF is indivisible even across reads."""
    pending = bytearray()
    while True:
        chunk = stream.read(min(block_size, maximum + 1))
        pending.extend(chunk)
        start = position = 0
        while match := _RECORD_END.search(pending, position):
            end = match.end()
            if pending[end - 1] == 13:
                if end == len(pending) and chunk:
                    break
                if pending[end : end + 1] == b"\n":
                    end += 1
            if end - start > maximum:
                raise ValueError("record_too_large")
            yield bytes(pending[start:end])
            start = position = end
        del pending[:start]
        if len(pending) > maximum:
            raise ValueError("record_too_large")
        if not chunk:
            if pending:
                yield bytes(pending)
            return


def escaped_size(text):
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


def record_format(value):
    if isinstance(value, dict) and isinstance(value.get("type"), str):
        return "codex" if "payload" in value else "claude-code"
    return None


def descriptor(size, digest, index, offset, prefix, part_hash, final=False):
    return dict(
        version=1,
        session_bytes=size,
        session_sha256=digest,
        index=index,
        offset=offset,
        prefix_sha256=prefix,
        sha256=part_hash,
        final=final,
    )


def native_plan(stream, source, identity, fmt="auto", *, budget=MAX_IMPORT_BYTES, block_size=65536):
    """Freeze hashes/spans before HTTP; buffers are bounded by a record/body cap."""
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 512:
        raise ValueError("Invalid original thread ID")
    if source != "t3" and fmt not in ("auto", source):
        raise ValueError("Unsupported native format")
    stream.seek(0)
    first_hash = hashlib.sha256()
    size = escaped = 0
    resolved = source if source != "t3" else (None if fmt == "auto" else fmt)
    for raw in binary_records(stream, budget, block_size):
        text = raw.decode("utf-8")
        first_hash.update(raw)
        size += len(raw)
        escaped += escaped_size(text)
        if resolved is None and text.strip():
            try:
                resolved = record_format(json.loads(text))
            except ValueError:
                pass  # Legacy-fitting live tails keep the server's parser policy.
    fingerprint = first_hash.hexdigest()
    base = dict(
        source=source,
        thread_id=identity,
        format=fmt,
        session_bytes=size,
        session_sha256=fingerprint,
        budget=budget,
        version=1,
    )
    if len(serialize(source, identity, fmt, "")) + escaped <= budget:
        return dict(base, legacy=True, parts=[dict(offset=0, size=size, sha256=fingerprint)])
    if resolved not in ("claude-code", "codex") or not 0 < size <= 2**63 - 1:
        raise ValueError("Unrecognized native session")
    # The *last* planning pass hashes the entire input again before any HTTP.
    stream.seek(0)
    whole = hashlib.sha256()
    parts = []
    offset = start = cost = 0
    prefix = whole.hexdigest()
    part_hash = hashlib.sha256()
    recognized = False

    def finish():
        parts.append(
            dict(
                offset=start,
                size=offset - start,
                sha256=part_hash.hexdigest(),
                part=descriptor(
                    size,
                    fingerprint,
                    len(parts),
                    start,
                    prefix,
                    part_hash.hexdigest(),
                    offset == size,
                ),
            )
        )

    for raw in binary_records(stream, budget, block_size):
        text = raw.decode("utf-8")
        if text.strip():
            try:
                recognized |= record_format(json.loads(text)) == resolved
            except ValueError:
                raise ValueError("Invalid complete native JSON record") from None
        extra = escaped_size(text)
        empty = descriptor(size, fingerprint, len(parts), start, prefix, "0" * 64)
        overhead = len(serialize(source, identity, resolved, "", empty))
        if overhead + cost + extra > budget and offset > start:
            finish()
            start = offset
            prefix = whole.hexdigest()
            cost = 0
            part_hash = hashlib.sha256()
            empty = descriptor(size, fingerprint, len(parts), start, prefix, "0" * 64)
            overhead = len(serialize(source, identity, resolved, "", empty))
        if overhead + extra > budget:
            raise ValueError("record_too_large")
        whole.update(raw)
        part_hash.update(raw)
        offset += len(raw)
        cost += extra
    if offset != size or whole.hexdigest() != fingerprint:
        raise ValueError("Source changed during preflight")
    if not recognized:
        raise ValueError("Unrecognized native session")
    if offset > start:
        finish()
    return dict(base, format=resolved, legacy=False, parts=parts)


def planned_body(stream, plan, span):
    stream.seek(span["offset"])
    raw = stream.read(span["size"])
    if len(raw) != span["size"] or hashlib.sha256(raw).hexdigest() != span["sha256"]:
        raise ValueError("Source changed after preflight")
    body = serialize(
        plan["source"], plan["thread_id"], plan["format"], raw.decode("utf-8"), span.get("part")
    )
    if len(body) > plan["budget"]:
        raise ValueError("Serialized request exceeds the request limit")
    return body


def acknowledge(report, plan, span):
    """HTTP 200 alone is never a part acknowledgement (including old servers)."""

    def refused():
        raise ValueError("Import part not acknowledged; multipart support is required")

    if (
        not isinstance(report, dict)
        or type(report.get("errors")) is not int
        or report["errors"] != 0
        or report.get("source") != plan["source"]
    ):
        refused()
    results = report.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        refused()
    row = results[0]
    part = span["part"]
    returned_part = row.get("part")
    if (
        not isinstance(returned_part, dict)
        or returned_part.keys() != part.keys()
        or any(
            type(returned_part[key]) is not type(value) or returned_part[key] != value
            for key, value in part.items()
        )
    ):
        refused()
    end = span["offset"] + span["size"]
    cursor = row.get("offset")
    next_index = row.get("next_index")
    if (
        row.get("status") not in ("imported", "duplicate")
        or row.get("unit") != "part"
        or row.get("thread_id") != plan["thread_id"]
        or row.get("format") != plan["format"]
        or row.get("part") != part
        or not isinstance(row.get("stored_thread_id"), str)
        or not row["stored_thread_id"]
        or type(cursor) is not int
        or not end <= cursor <= plan["session_bytes"]
        or type(next_index) is not int
        or not part["index"] < next_index <= len(plan["parts"])
    ):
        refused()
    # Cursor must match a boundary/hash in this frozen plan, even on stale retry.
    boundary = plan["parts"][next_index - 1]
    expected_offset = boundary["offset"] + boundary["size"]
    expected_hash = (
        plan["session_sha256"]
        if next_index == len(plan["parts"])
        else plan["parts"][next_index]["part"]["prefix_sha256"]
    )
    if (
        cursor != expected_offset
        or row.get("prefix_sha256") != expected_hash
        or row.get("session_state")
        != ("complete" if cursor == plan["session_bytes"] else "incomplete")
    ):
        refused()
    return row


def upload_native(stream, source, identity, fmt, send, *, budget=MAX_IMPORT_BYTES):
    plan = native_plan(stream, source, identity, fmt, budget=budget)
    acknowledged = 0
    stored_id = None
    for span in plan["parts"]:
        report = send(planned_body(stream, plan, span))
        if plan["legacy"]:
            print(json.dumps(report, ensure_ascii=False), flush=True)
            return bool(report["errors"])
        row = acknowledge(report, plan, span)
        if stored_id is not None and stored_id != row["stored_thread_id"]:
            raise ValueError("Import part identity changed")
        stored_id = row["stored_thread_id"]
        acknowledged += span["size"]  # each planned interval exactly once, never cursor sums
        print(
            json.dumps(
                dict(
                    report,
                    progress={
                        "thread_id": identity,
                        "plan_version": plan["version"],
                        "part_index": span["part"]["index"],
                        "planned_parts": len(plan["parts"]),
                        "start": span["offset"],
                        "end": span["offset"] + span["size"],
                        "part_sha256": span["sha256"],
                        "session_sha256": plan["session_sha256"],
                        "stored_thread_id": stored_id,
                        "status": row["status"],
                        "acknowledged_bytes": acknowledged,
                        "server_offset": row["offset"],
                        "session_state": row["session_state"],
                    },
                ),
                ensure_ascii=False,
            ),
            flush=True,
        )
    return False


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def conversations(text, source, thread_id):
    if source in ("chatgpt", "claude-ai") and text.lstrip().startswith("["):
        # Keep each conversation object's exact JSON spelling, not a dump of
        # parsed objects. Each completed HTTP response is a progress checkpoint.
        decoder = json.JSONDecoder()
        json.loads(text)  # validate the whole export before uploading anything
        position = text.index("[") + 1
        while True:
            while position < len(text) and text[position].isspace():
                position += 1
            if text[position] == "]":
                return
            value, end = decoder.raw_decode(text, position)
            identity = value.get("id") or value.get("uuid") if isinstance(value, dict) else None
            if not isinstance(identity, str) or not identity:
                raise ValueError("Each conversation needs an id or uuid")
            yield identity, text[position:end]
            position = end
            while text[position].isspace():
                position += 1
            if text[position] == ",":
                position += 1
    else:
        if not thread_id:
            raise ValueError("--thread-id is required for a single conversation")
        yield thread_id, text


def grok_facts(text):
    # A stable identity per original fact permits overlapping export retries.
    for raw in StringIO(text, newline=""):
        if raw.strip():
            yield hashlib.sha256(raw.encode("utf-8")).hexdigest(), raw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument(
        "--source",
        required=True,
        choices=(
            "claude-code",
            "codex",
            "cursor",
            "t3",
            "grok-bot",
            "chatgpt",
            "claude-ai",
        ),
    )
    parser.add_argument("--thread-id")
    parser.add_argument(
        "--format",
        default="auto",
        choices=("auto", "messages", "claude-code", "codex", "grok-facts"),
    )
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    args = parser.parse_args(argv)
    target = urlsplit(args.url)
    if (
        target.scheme != "https"
        and not (target.scheme == "http" and target.hostname in ("localhost", "127.0.0.1", "::1"))
        or target.username
        or target.password
        or target.query
        or target.fragment
    ):
        parser.error("Use HTTPS, or HTTP on loopback, with no URL credentials or query")
    token = os.environ.get("COMMONPLACE_TOKEN")
    if not token:
        parser.error("Set COMMONPLACE_TOKEN to your scoped connection token")
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def send(body):
        request = Request(
            args.url.rstrip("/") + "/api/import",
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with opener.open(request, timeout=60) as response:
            return json.load(response)

    failed = False
    try:
        if args.source in ("claude-code", "codex", "t3") and args.format in (
            "auto",
            "claude-code",
            "codex",
        ):
            with args.file.open("rb") as stream:
                return int(
                    upload_native(
                        stream,
                        args.source,
                        args.thread_id,
                        args.format,
                        send,
                        budget=MAX_IMPORT_BYTES,
                    )
                )
        with args.file.open(encoding="utf-8", newline="") as stream:
            text = stream.read()
        records = (
            grok_facts(text)
            if args.source == "grok-bot" and args.format in ("auto", "grok-facts")
            else conversations(text, args.source, args.thread_id)
        )
        for identity, transcript in records:
            body = json.dumps(
                {
                    "source": args.source,
                    "threads": [
                        {
                            "thread_id": identity,
                            "transcript": transcript,
                            "format": args.format,
                        }
                    ],
                },
                ensure_ascii=False,
            ).encode("utf-8")
            if len(body) > 64 * 1024 * 1024:
                raise ValueError("Conversation exceeds the 64 MiB request limit")
            report = send(body)
            print(json.dumps(report, ensure_ascii=False), flush=True)
            failed |= bool(report["errors"])
    except (HTTPError, URLError, OSError):
        print(
            "Import request failed; completed conversations are safe to reimport", file=sys.stderr
        )
        return 1
    except (TypeError, KeyError, RecursionError):
        print("Invalid import export or response", file=sys.stderr)
        return 1
    except ValueError as error:
        # Validation errors above are fixed messages, never export contents.
        message = (
            "Invalid JSON export"
            if isinstance(error, json.JSONDecodeError)
            else "Invalid UTF-8 export"
            if isinstance(error, UnicodeError)
            else str(error)
        )
        print(message, file=sys.stderr)
        return 1
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
