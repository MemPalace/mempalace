#!/usr/bin/env python3
"""
MemPalace MCP Server — read/write palace access for Claude Code
================================================================
Install: claude mcp add mempalace -- mempalace-mcp [--palace /path/to/palace]

The public import path remains ``mempalace.mcp_server``. Implementation is
split across this package so PRs can target a domain file instead of one
8 000-line module. Fragment files are executed into this package's globals
(the same namespace as the historical module), which preserves:

* ``from mempalace.mcp_server import tool_status, TOOLS, handle_request, main``
* test monkeypatches on ``mempalace.mcp_server._config`` / ``_get_kg`` / ...
* ``python -m mempalace.mcp_server``
* stdio protection running before chromadb import

Tools (read):
  mempalace_status          — total drawers, wing/room breakdown
  mempalace_list_wings      — all wings with drawer counts
  mempalace_list_rooms      — rooms within a wing
  mempalace_get_taxonomy    — full wing → room → count tree
  mempalace_search          — semantic search, optional wing/room/source_file filter
  mempalace_check_duplicate — check if content already exists before filing

Tools (write):
  mempalace_add_drawer      — file verbatim content into a wing/room
  mempalace_delete_drawer   — remove a drawer by ID
  mempalace_delete_by_source — bulk-remove all drawers mined from one source_file

Tools (maintenance):
  mempalace_reconnect       — force cache invalidation and reconnect after external writes
"""

import os
import sys

# --- MCP stdio protection (issue #225) -----------------------------------
# The MCP protocol multiplexes JSON-RPC over stdio: stdout MUST carry only
# valid JSON-RPC messages, stderr is for human-readable logs. Some
# transitive dependencies (chromadb → onnxruntime, posthog telemetry) print
# banners and error messages directly to stdout — sometimes at C level —
# which breaks Claude Desktop's JSON parser. Redirect stdout → stderr at
# both the Python and file-descriptor level before heavy imports, then
# restore the real stdout in main() before entering the protocol loop.
#
# Preserved across importlib.reload (#2485): once redirected, sys.stdout points
# to sys.stderr. If a reload executes unconditionally, it captures sys.stderr
# into _REAL_STDOUT and duplicates the already-redirected fd 1, permanently
# breaking stdout and leaking file descriptors.
_REAL_STDOUT = globals().get("_REAL_STDOUT")
if _REAL_STDOUT is None:
    _REAL_STDOUT = sys.stdout

_REAL_STDOUT_FD = globals().get("_REAL_STDOUT_FD")
if _REAL_STDOUT_FD is None:
    try:
        _REAL_STDOUT_FD = os.dup(1)
        os.dup2(2, 1)
    except (OSError, AttributeError):
        # Environments without fd-level stdio (embedded interpreters, some test
        # harnesses). The Python-level redirect below still applies.
        pass
sys.stdout = sys.stderr

import argparse  # noqa: E402  (deferred until after stdio protection above)
import contextlib  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import re  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import sqlite3  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from datetime import date, datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Optional  # noqa: E402
from urllib.parse import urlparse  # noqa: E402

from ..config import (  # noqa: E402
    MempalaceConfig,
    sanitize_kg_value,
    sanitize_name,
    sanitize_content,
    sanitize_iso_temporal,
    sqlite_read_uri,
    strip_lone_surrogates,
)
from ..version import __version__  # noqa: E402
from chromadb.errors import NotFoundError as _ChromaNotFoundError  # noqa: E402

from ..backends.chroma import (  # noqa: E402
    ChromaBackend,
    ChromaCollection,
    _HNSW_WRITE_DEFAULTS,
    _pin_hnsw_threads,
    hnsw_capacity_status,
    reset_hnsw_capacity_cache,
)
from ..backends import BackendMismatchError, PalaceRef, detect_backend_for_path  # noqa: E402
from ..date_window import filed_at_in_window, parse_date_bound  # noqa: E402
from ..query_sanitizer import sanitize_query  # noqa: E402
from ..searcher import (  # noqa: E402
    SearchError,
    _distance_to_similarity,
    _metric_for_collection,
    search as cli_search,
    search_memories,
)
from ..palace_graph import (  # noqa: E402
    traverse,
    find_tunnels,
    graph_stats,
    create_tunnel,
    list_tunnels,
    delete_tunnel,
    follow_tunnels,
    _load_tunnels as _load_graph_tunnels,
)
from ..hallways import (  # noqa: E402
    list_hallways,
    delete_hallway,
)

from ..knowledge_graph import KnowledgeGraph, DEFAULT_KG_PATH  # noqa: E402
from ..logstream import LOGSTREAM_DB_FILENAME, Logstream  # noqa: E402
from ..collision_scan import assert_no_collisions  # noqa: E402
from ..ids import ID_RECIPE, make_drawer_id_from_content  # noqa: E402

# ==================== WRITE-AHEAD LOG ====================
# Every write operation is logged to a JSONL file before execution.
# This provides an audit trail for detecting memory poisoning and
# enables review/rollback of writes from external or untrusted sources.
#
# The implementation lives in mempalace.wal — a side-effect-free module — so the
# CLI sync path and the daemon service layer can audit writes without importing
# this module, whose import installs MCP stdio protection (os.dup2(2, 1) and
# sys.stdout = sys.stderr) that would misroute their output.
from ..wal import _wal_log  # noqa: E402

_FRAGMENT_DIR = Path(__file__).resolve().parent
# Load order is a dependency sequence, not a catalog: later files use names
# defined earlier (_guards before tools_*, tools_* before schemas, schemas
# before protocol). Do not reorder this tuple.
_FRAGMENTS = (
    "_logging.py",
    "_guards.py",
    "_session.py",
    "tools_read.py",
    "tools_write.py",
    "tools_kg.py",
    "tools_diary.py",
    "tools_coord.py",
    "schemas.py",
    "protocol.py",
    "http.py",
    "runtime.py",
)


def _exec_fragment(filename: str) -> None:
    path = _FRAGMENT_DIR / filename
    source = path.read_text(encoding="utf-8")
    exec(compile(source, str(path), "exec"), globals())


for _fragment in _FRAGMENTS:
    _exec_fragment(_fragment)
del _fragment
