"""Upload one local conversation or a ChatGPT/Claude.ai conversation array."""

import argparse
import hashlib
import json
import os
import sys
from io import StringIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


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
    failed = False
    try:
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
            request = Request(
                args.url.rstrip("/") + "/api/import",
                data=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            with opener.open(request, timeout=60) as response:
                report = json.load(response)
            print(json.dumps(report, ensure_ascii=False), flush=True)
            failed |= bool(report["errors"])
    except (HTTPError, URLError, OSError):
        print(
            "Import request failed; completed conversations are safe to reimport", file=sys.stderr
        )
        return 1
    except ValueError as error:
        # Validation errors above are fixed messages, never export contents.
        message = "Invalid JSON export" if isinstance(error, json.JSONDecodeError) else str(error)
        print(message, file=sys.stderr)
        return 1
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
