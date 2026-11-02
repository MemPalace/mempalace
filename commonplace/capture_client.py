"""One bounded sync pass over explicitly selected native transcript files."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import stat
import sys
from pathlib import Path
from urllib.parse import urlencode, urlsplit

CHUNK_BYTES = 512 * 1024
MAX_RECORD_BYTES = 1024 * 1024


class CaptureError(Exception):
    """Fixed diagnostics only: never include paths, credentials or source text."""


def endpoint(url):
    try:
        target = urlsplit(url)
        if (
            target.scheme != "https"
            and not (
                target.scheme == "http" and target.hostname in ("localhost", "127.0.0.1", "::1")
            )
            or not target.hostname
            or target.username
            or target.password
            or target.query
            or target.fragment
            or target.path not in ("", "/")
        ):
            raise ValueError
        _ = target.port
    except (ValueError, TypeError):
        raise CaptureError("Use an explicit HTTPS gateway, or loopback HTTP") from None
    return target


def request(target, token, method, route, body=None):
    connection_type = (
        http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(target.hostname, target.port, timeout=10)
    try:
        connection.request(
            method,
            route,
            None if body is None else json.dumps(body, ensure_ascii=False).encode(),
            {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        raw = response.read(65537)
        if response.status == 429:
            return {"status": "deferred"}
        if response.status != 200 or len(raw) > 65536:
            raise CaptureError(f"Capture request refused (HTTP {response.status}); saved data kept")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (OSError, http.client.HTTPException, ValueError, UnicodeError):
        raise CaptureError("Capture request unavailable; retry the same files") from None
    finally:
        connection.close()


def native_id(stream, source):
    """Use native identity across rename/rotation; never transmit a local path."""
    consumed = 0
    while consumed < MAX_RECORD_BYTES:
        raw = stream.readline(MAX_RECORD_BYTES + 1)
        if not raw:
            break
        consumed += len(raw)
        if len(raw) > MAX_RECORD_BYTES:
            break
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
            if source == "codex" and record.get("type") == "session_meta":
                identity = record["payload"]["id"]
            elif source == "claude-code":
                identity = record.get("sessionId")
                agent = record.get("agentId")
                if agent:
                    identity = f"{identity}:agent:{agent}" if identity else None
            else:
                identity = None
            if isinstance(identity, str) and identity.strip() and len(identity) <= 512:
                return identity
        except (ValueError, TypeError, AttributeError, KeyError):
            break
    raise CaptureError("Native session identity missing; file left untouched")


def sync_file(target, token, source, path):
    # Follow no final symlink. Scans also reject symlinked ancestors.
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise CaptureError("Symlinked transcript refused")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise CaptureError("Transcript must be a regular file")
        identity = native_id(stream, source)
        stream.seek(0)
        state = request(
            target,
            token,
            "GET",
            "/api/capture?" + urlencode({"source": source, "thread_id": identity}),
        )
        if state.get("status") == "deferred":
            return state
        if (
            type(state.get("offset")) is not int
            or state["offset"] < 0
            or not isinstance(state.get("prefix_sha256"), str)
        ):
            raise CaptureError("Invalid capture cursor; saved prefix kept")
        offset, digest = state["offset"], hashlib.sha256()
        remaining = offset
        while remaining:
            raw = stream.read(min(remaining, CHUNK_BYTES))
            if not raw:
                raise CaptureError("Transcript shrank; saved prefix kept")
            digest.update(raw)
            remaining -= len(raw)
        if digest.hexdigest() != state["prefix_sha256"]:
            raise CaptureError("Transcript prefix changed; saved prefix kept")
        # At most one bounded suffix per file/pass. Partial final JSON records
        # remain local until flushed, including an incomplete UTF-8 character.
        suffix = stream.read(CHUNK_BYTES)
        while (
            suffix and not suffix.endswith(b"\n") and len(suffix) <= CHUNK_BYTES + MAX_RECORD_BYTES
        ):
            extra = stream.readline(MAX_RECORD_BYTES + 1)
            if not extra:
                break
            suffix += extra
        suffix = suffix[: suffix.rfind(b"\n") + 1]
        if len(suffix) > CHUNK_BYTES + MAX_RECORD_BYTES:
            raise CaptureError("Native record too large; file left for a later manual import")
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise CaptureError("Transcript changed during read; retry after flush")
        if not suffix:
            return {"status": "unchanged"}
        try:
            text = suffix.decode("utf-8")
        except UnicodeError:
            raise CaptureError("Native transcript is not UTF-8") from None
        return request(
            target,
            token,
            "POST",
            "/api/capture",
            {
                "source": source,
                "thread_id": identity,
                "offset": offset,
                "prefix_sha256": state["prefix_sha256"],
                "transcript": text,
            },
        )


def files(root):
    if any(part.is_symlink() for part in (root, *root.parents)) or not root.is_dir():
        raise CaptureError("Select a regular transcript directory")
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not (Path(directory) / name).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            if path.suffix == ".jsonl" and not path.is_symlink():
                yield path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("COMMONPLACE_URL"))
    parser.add_argument("--source", required=True, choices=("claude-code", "codex"))
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--root", type=Path)
    selection.add_argument("--file", type=Path)
    args = parser.parse_args(argv)
    token = os.environ.get("COMMONPLACE_TOKEN", "")
    if not token or not args.url:
        parser.error("Set COMMONPLACE_URL and COMMONPLACE_TOKEN explicitly")
    failed = False
    try:
        target = endpoint(args.url)
        setting = request(target, token, "GET", "/api/settings/capture")
        if not setting.get("enabled"):
            print(json.dumps({"status": "disabled"}))
            return 0
        paths = files(args.root) if args.root else [args.file]
        for path in paths:
            try:
                result = sync_file(target, token, args.source, path)
                print(json.dumps(result), flush=True)
            except (CaptureError, OSError) as error:
                # Fixed diagnostic messages above never contain source paths.
                print(
                    str(error)
                    if isinstance(error, CaptureError)
                    else "Transcript unavailable; retry later",
                    file=sys.stderr,
                )
                failed = True
    except (CaptureError, OSError) as error:
        print(
            str(error) if isinstance(error, CaptureError) else "Capture unavailable",
            file=sys.stderr,
        )
        return 1
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
