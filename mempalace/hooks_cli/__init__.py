"""
mempalace.hooks_cli — session-start, stop, session-end, and precompact hooks.

Reads JSON from stdin, outputs JSON to stdout.
Supported hooks: session-start, stop, session-end, precompact
Supported harnesses: claude-code, codex (extensible to cursor, gemini, etc.).
Public import path is ``mempalace.hooks_cli``.
"""

# The public import path remains ``mempalace.hooks_cli``. Implementation is
# split across this package so PRs can target mine spawn, write routing,
# ingest, or hook handlers without colliding. Fragments exec into this
# package's globals (the same namespace as the historical module).

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from mempalace.config import MempalaceConfig
from mempalace.write_routing import (
    ResolvedWriteRoutingPolicy,
    WriteRoutingDecision,
    WriteRoutingError,
    WriteRoutingPolicy,
    choose_write_route,
)

SAVE_INTERVAL = 15
STATE_DIR = Path.home() / ".mempalace" / "hook_state"
PALACE_ROOT = Path.home() / ".mempalace"

_FRAGMENT_DIR = Path(__file__).resolve().parent
# Load order is a dependency sequence, not a catalog: later files use names
# defined earlier (process before mine_spawn, ingest before handlers).
# Do not reorder this tuple.
_FRAGMENTS = (
    "process.py",
    "mine_spawn.py",
    "routing.py",
    "ingest.py",
    "harness.py",
    "handlers.py",
)


def _exec_fragment(filename: str) -> None:
    path = _FRAGMENT_DIR / filename
    source = path.read_text(encoding="utf-8")
    exec(compile(source, str(path), "exec"), globals())


for _fragment in _FRAGMENTS:
    _exec_fragment(_fragment)
del _fragment
