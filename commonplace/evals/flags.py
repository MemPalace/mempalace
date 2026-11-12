"""Privacy flags for eval runs: signals only, never a gate or a redaction.

The scanner is imported lazily, only by archive, list, show and read, so a
broken ``commonplace.privacy`` affects no other command and never purge or
preflight. Every failure (import, version digest, scan) is ``eval_scan_failed``.
"""

from __future__ import annotations

import hashlib
import importlib
import json

from . import EvalError

SCANNER_FIELDS = ("scanner_codes", "scanner_version")


class Scanner:
    def __init__(self, scan, version):
        self._scan = scan
        self.version = version

    def codes(self, strings) -> list[str]:
        found = set()
        try:
            for text in strings:
                found.update(self._scan(text))
        except Exception:
            raise EvalError("eval_scan_failed") from None
        return sorted(found)


def load() -> Scanner:
    """Import the scanner, version it by the loaded file's bytes, and probe it once.

    The probe makes a scanner that cannot run fail every command that loads
    it, including list/show/read whose stored version needs no rescan.
    """
    try:
        privacy = importlib.import_module("commonplace.privacy")
        with open(privacy.__file__, "rb") as handle:
            version = hashlib.sha256(handle.read()).hexdigest()[:16]
        if not isinstance(privacy.scan(""), list):
            raise TypeError("scanner result")
        return Scanner(privacy.scan, version)
    except Exception:
        raise EvalError("eval_scan_failed") from None


def _values(value):
    """Every string in a decoded JSON value: dict keys and values, list items."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _leaves(text):
    # Tool-call text and raw lines are JSON: escaped quotes would hide
    # quoted assignments, so scan each decoded string leaf on its own.
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return
    yield from _values(value)


def run_strings(rows, artifacts, manifest):
    """Each string of a run, scanned on its own, never the serialized ``rows``.

    ``rows`` are ``[seq, role, kind, name, call_id, at, text, raw]`` lists,
    ``artifacts`` ``(name, text)`` pairs and ``manifest`` the dict without
    its scanner fields.
    """
    for row in rows:
        yield from row[1:]
        yield from _leaves(row[6])
        yield from _leaves(row[7])
    for name, text in artifacts:
        yield name
        yield text
    yield from _values({k: v for k, v in manifest.items() if k not in SCANNER_FIELDS})
