"""MemPalace memory provider for Hermes.

Implements the Hermes ``MemoryProvider`` ABC (``agent/memory_provider.py``)
so MemPalace can be selected as ``memory.provider: mempalace`` in
``~/.hermes/config.yaml``.

Design notes
------------

* ChromaDB access goes through ``mempalace.backends.chroma.ChromaBackend``
  rather than a raw ``chromadb.PersistentClient``. This ensures the
  embedding function returned by ``mempalace.embedding.get_embedding_function``
  is bound to the collection, fixing the embedding-dimension mismatch that
  silently broke the three earlier Hermes-side PRs (NousResearch/hermes-agent
  #5671, #12203, #9761) on existing palaces.

* Per-turn writes go through a bounded background queue. The agent loop
  never blocks on ChromaDB or SQLite.

* ``sync_turn`` is the **sole** filing path. ``on_session_end`` and
  ``on_pre_compress`` intentionally file nothing: re-filing the raw
  message list duplicates every turn ``sync_turn`` already stored —
  ``filed_at`` is hashed into the drawer id, so upserts cannot collapse
  the copies. Any future safety net here must first scan what is
  already filed and add only what is missing.

* The provider is **inactive** under ``agent_context in {"cron", "flush"}``
  or ``platform == "cron"``. Cron-context turns are system-generated and
  would otherwise corrupt the user's representation.

* Configuration precedence: ``$HERMES_HOME/mempalace.json`` is read
  first, then env vars override (``MEMPALACE_PALACE_PATH``,
  ``MEMPALACE_IDENTITY_PATH``, ``MEMPALACE_WING``). An empty env var
  is ignored — ``export MEMPALACE_WING=`` is intent to unset. A palace
  still unset after that defers to mempalace's own config
  (``~/.mempalace/config.json``) before falling back to the default
  location. The resolved palace is then published to
  ``MEMPALACE_PALACE_PATH`` (see ``_bridge_palace_env``) so the
  ``mempalace.mcp_server`` passthrough tools operate on the same palace
  as live filing and search — never a config-file-vs-provider split.
  ``collection_name`` follows mempalace's own config for the same
  reason: it is what ``search_memories`` (used by ``prefetch`` and
  ``_tool_search``) and the mcp_server passthrough read, so live writes
  land in the collection recall actually searches. It is intentionally
  not configurable on the Hermes side — a second knob would let the
  write and read sides diverge silently again.

* ``~/.mempalace/identity.txt`` (L0) and ``~/.mempalace/wing_config.json``
  are loaded if present but never created here. Run
  ``mempalace init <project-dir>`` to generate them.

* All palace state (ChromaDB, knowledge graph, diary, identity) lives
  under ``~/.mempalace/`` by design — the palace is the user's central
  memory shared across agents, not per-agent Hermes state. This means
  ``hermes backup`` (which archives only ``$HERMES_HOME``) does NOT
  cover it; users must back up ``~/.mempalace/`` separately. Hermes'
  ``MemoryProvider`` ABC currently offers no hook for contributing
  external paths to its backup.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from mempalace.backends.chroma import ChromaBackend
from mempalace.config import (
    MempalaceConfig,
    sanitize_iso_temporal,
    sanitize_kg_value,
    sanitize_name,
)
from mempalace.convo_miner import file_conversation_exchange
from mempalace.knowledge_graph import KnowledgeGraph
from mempalace.layers import Layer0
from mempalace.searcher import search_memories

# When this plugin is loaded by Hermes, ``agent.memory_provider`` is on the
# import path. When mempalace's own test suite imports this module (or a tool
# inspects it without Hermes installed) the import fails — fall back to a stub
# so the module still imports cleanly. ``isinstance(provider, MemoryProvider)``
# checks remain meaningful because the real ABC binds at plugin-load time.
try:
    from agent.memory_provider import MemoryProvider  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Hermes not installed

    class MemoryProvider:  # type: ignore[no-redef]
        """Stub used when ``agent.memory_provider`` cannot be imported."""


logger = logging.getLogger("mempalace.hermes")


def _l1_story_from_collection(
    collection, wing: str = "", max_drawers: int = 15, max_chars: int = 2400
) -> str:
    """Build Layer-1 essential-story text from an *already open* collection.

    ``layers.Layer1`` opens a second ``PersistentClient`` via
    ``palace.get_collection``. Concurrent with this provider's long-lived
    client (and its background filing worker) that second client races
    Chroma's local SQLite and surfaces as disk I/O errors / "Failed to get
    segments". Keep all Chroma access on the single client the provider owns.
    """
    from collections import defaultdict
    from pathlib import Path as _Path

    docs: list = []
    metas: list = []
    batch = 500
    offset = 0
    max_scan = 2000
    while True:
        kwargs = {"include": ["documents", "metadatas"], "limit": batch, "offset": offset}
        if wing:
            kwargs["where"] = {"wing": wing}
        try:
            chunk = collection.get(**kwargs)
        except Exception:
            break
        batch_docs = chunk.get("documents") or []
        batch_metas = chunk.get("metadatas") or []
        if not batch_docs:
            break
        docs.extend(batch_docs)
        metas.extend(batch_metas)
        offset += len(batch_docs)
        if len(batch_docs) < batch or len(docs) >= max_scan:
            break

    if not docs:
        return "## L1 -- No memories yet."

    scored = []
    for doc, meta in zip(docs, metas):
        meta = meta or {}
        doc = doc or ""
        importance = 3.0
        for key in ("importance", "emotional_weight", "weight"):
            val = meta.get(key)
            if val is not None:
                try:
                    importance = float(val)
                except (TypeError, ValueError):
                    pass
                break
        recency = str(meta.get("filed_at") or "")
        scored.append((importance, recency, meta, doc))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    top = [(imp, meta, doc) for imp, _r, meta, doc in scored[:max_drawers]]

    by_room: dict = defaultdict(list)
    for imp, meta, doc in top:
        by_room[meta.get("room", "general")].append((imp, meta, doc))

    lines = ["## L1 -- ESSENTIAL STORY"]
    total = 0
    for room, entries in sorted(by_room.items()):
        room_line = f"\n[{room}]"
        lines.append(room_line)
        total += len(room_line)
        for _imp, meta, doc in entries:
            source = _Path(meta.get("source_file", "")).name if meta.get("source_file") else ""
            snippet = (doc or "").strip().replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:197] + "..."
            entry = f"  - {snippet}"
            if source:
                entry += f"  ({source})"
            if total + len(entry) > max_chars:
                lines.append("  ... (more in L3 search)")
                return "\n".join(lines)
            lines.append(entry)
            total += len(entry)
    return "\n".join(lines)


# The palace path this provider last bridged into ``MEMPALACE_PALACE_PATH``
# (None when the variable is user-set or unset). ``initialize`` must be able
# to tell its own bridge write apart from user intent: a stale bridge from a
# previous session would otherwise override a freshly edited hermes-side
# config forever, because both this provider's ``_load_config`` and
# ``MempalaceConfig`` give the env var top precedence.
_ENV_PALACE_BRIDGED: Optional[str] = None


def _clear_stale_palace_bridge() -> None:
    """Drop our own previous bridge write so config re-resolution is fresh.

    Only removes the env var when it still holds exactly the value we set —
    a user-set value (even one set after our bridge) never matches the
    recorded sentinel and is left untouched.
    """
    global _ENV_PALACE_BRIDGED
    if _ENV_PALACE_BRIDGED and os.environ.get("MEMPALACE_PALACE_PATH") == _ENV_PALACE_BRIDGED:
        del os.environ["MEMPALACE_PALACE_PATH"]
    _ENV_PALACE_BRIDGED = None


def _bridge_palace_env(palace_path: str) -> None:
    """Publish the provider's resolved palace to ``MEMPALACE_PALACE_PATH``.

    ``mempalace.mcp_server`` resolves its palace from this variable on every
    config access (its ``--palace`` flag works by setting exactly this
    variable), so bridging it makes the passthrough tools operate on the
    same palace the provider writes and searches. When the variable already
    points at the same palace (user-set), ownership is NOT claimed, so a
    later ``_clear_stale_palace_bridge`` leaves the user's value alone.
    """
    global _ENV_PALACE_BRIDGED
    bridged = os.path.abspath(os.path.expanduser(palace_path))
    current = os.environ.get("MEMPALACE_PALACE_PATH")
    if current and os.path.abspath(os.path.expanduser(current)) == bridged:
        return
    os.environ["MEMPALACE_PALACE_PATH"] = bridged
    _ENV_PALACE_BRIDGED = bridged


def _match_wing_by_keywords(text: str, wing_config: Dict[str, Any]) -> str:
    """Return the first wing whose keywords match a whole word in ``text``.

    Word boundaries matter — bare substring matching routes turns mentioning
    ``said`` into a wing whose keyword is ``ai``. Fall back to ``wing_general``.

    Lives at module scope so ``backfill.py`` can import and delegate to it —
    one routing implementation shared by live and historical ingest.
    """
    if not wing_config:
        return "wing_general"
    text_lower = text.lower()
    for wing_name, wing_def in wing_config.items():
        keywords = wing_def.get("keywords", []) if isinstance(wing_def, dict) else []
        for kw in keywords:
            # The isinstance guard keeps a hand-edited wing_config.json
            # (numbers / nulls in a keyword list) from raising inside
            # _file_turn's try/except and silently dropping every turn.
            if not kw or not isinstance(kw, str):
                continue
            pattern = r"\b" + re.escape(kw.lower()) + r"\b"
            if re.search(pattern, text_lower):
                return wing_name
    return "wing_general"


def _normalize_content(content: Any) -> str:
    """Flatten an Anthropic/OpenAI ``content`` field to a plain string.

    Hermes turns frequently carry ``content`` as a list of typed parts
    (``[{"type": "text", "text": "..."}, {"type": "tool_use", ...}]``).
    A naive ``f"User: {content}"`` would persist the literal ``repr`` of
    the list and corrupt semantic search recall over the palace. Concatenate
    the ``text`` blocks (and surface a tool-use marker so search hits still
    say a tool was called) instead.
    """
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    parts.append(text)
            elif btype == "tool_use":
                name = block.get("name", "?")
                parts.append(f"[tool_use: {name}]")
            elif btype == "tool_result":
                result = block.get("content")
                parts.append(f"[tool_result] {_normalize_content(result)}")
            else:
                # Unknown block type — fall back to text field or skip.
                text = block.get("text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format; no `handler` field — dispatch
# happens via ``MempalaceProvider.handle_tool_call``).
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "mempalace_search",
        "description": "Semantic search across the palace. Returns verbatim drawers ranked by relevance.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language search query."},
                "wing": {"type": "string", "description": "Limit results to one wing (optional)."},
                "room": {
                    "type": "string",
                    "description": "Limit results to one room within a wing (optional).",
                },
                "n_results": {
                    "type": "integer",
                    "description": "Number of results (1-50, default 5).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "mempalace_status",
        "description": "Palace overview: total drawers, per-wing counts, palace path.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "mempalace_list_wings",
        "description": "List all wings with their drawer counts.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "mempalace_list_rooms",
        "description": "List rooms (and counts) within a wing.",
        "parameters": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "Wing name."},
            },
            "required": ["wing"],
        },
    },
    {
        "name": "mempalace_kg_query",
        "description": "Query the knowledge graph for relationships involving an entity, with optional time filtering.",
        "parameters": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity name."},
                "since": {"type": "string", "description": "ISO date lower bound (optional)."},
            },
            "required": ["entity"],
        },
    },
    {
        "name": "mempalace_kg_add",
        "description": "Add a (subject, predicate, object) fact to the knowledge graph.",
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "predicate": {"type": "string"},
                "object": {"type": "string"},
            },
            "required": ["subject", "predicate", "object"],
        },
    },
    {
        "name": "mempalace_diary_write",
        "description": "Append an AAAK diary entry.",
        "parameters": {
            "type": "object",
            "properties": {
                "entry": {"type": "string", "description": "Diary entry text."},
            },
            "required": ["entry"],
        },
    },
    {
        "name": "mempalace_diary_read",
        "description": "Read the most recent diary entries.",
        "parameters": {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "Number of entries to return (default 10).",
                },
            },
        },
    },
    {
        "name": "mempalace_add_drawer",
        "description": (
            "File a verbatim drawer into the palace. Use for explicit "
            "structured content the user dictates or you decide to "
            "persist — the per-turn auto-filing happens separately."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "Wing name."},
                "room": {"type": "string", "description": "Room within the wing."},
                "content": {"type": "string", "description": "Verbatim drawer content."},
                "source_file": {
                    "type": "string",
                    "description": "Optional source-file annotation.",
                },
            },
            "required": ["wing", "room", "content"],
        },
    },
    {
        "name": "mempalace_update_drawer",
        "description": (
            "Edit an existing drawer in place. Prefer adding a new "
            "drawer that supersedes the old one — mempalace is "
            "append-first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "drawer_id": {"type": "string"},
                "content": {"type": "string", "description": "New verbatim content (optional)."},
                "wing": {"type": "string", "description": "New wing (optional)."},
                "room": {"type": "string", "description": "New room (optional)."},
            },
            "required": ["drawer_id"],
        },
    },
    {
        "name": "mempalace_delete_drawer",
        "description": (
            "Remove a drawer. Reserve for PII cleanup or correcting a "
            "wrong filing — mempalace's design prefers superseding adds."
        ),
        "parameters": {
            "type": "object",
            "properties": {"drawer_id": {"type": "string"}},
            "required": ["drawer_id"],
        },
    },
    {
        "name": "mempalace_list_drawers",
        "description": "List drawers in a wing/room with their previews.",
        "parameters": {
            "type": "object",
            "properties": {
                "wing": {"type": "string"},
                "room": {"type": "string"},
                "limit": {"type": "integer", "description": "Default 20."},
                "offset": {"type": "integer", "description": "Default 0."},
            },
        },
    },
    {
        "name": "mempalace_get_drawer",
        "description": "Fetch a drawer's full verbatim content by id.",
        "parameters": {
            "type": "object",
            "properties": {"drawer_id": {"type": "string"}},
            "required": ["drawer_id"],
        },
    },
    {
        "name": "mempalace_check_duplicate",
        "description": (
            "Check whether content similar to the given text already "
            "exists in the palace before filing. Returns closest match "
            "and similarity score."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "threshold": {"type": "number", "description": "Default 0.9."},
            },
            "required": ["content"],
        },
    },
    {
        "name": "mempalace_kg_invalidate",
        "description": (
            "Mark a (subject, predicate, object) fact as no longer valid "
            "from a given date — per the palace protocol's step 5."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "predicate": {"type": "string"},
                "object": {"type": "string"},
                "ended": {
                    "type": "string",
                    "description": (
                        "ISO date the fact stopped being true (optional, defaults to now)."
                    ),
                },
            },
            "required": ["subject", "predicate", "object"],
        },
    },
    {
        "name": "mempalace_kg_timeline",
        "description": "Full temporal timeline for an entity in the knowledge graph.",
        "parameters": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Entity name (optional — all entities if omitted).",
                },
            },
        },
    },
    {
        "name": "mempalace_kg_stats",
        "description": "Knowledge graph summary statistics.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "mempalace_get_taxonomy",
        "description": "Full wing → room → drawer-count tree of the palace.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "mempalace_get_aaak_spec",
        "description": (
            "Return the full AAAK compression dialect specification "
            "(also injected in the wake-up block)."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "mempalace_traverse",
        "description": "Traverse the room graph from a starting room, following hallway links.",
        "parameters": {
            "type": "object",
            "properties": {
                "start_room": {"type": "string"},
                "max_hops": {"type": "integer", "description": "Default 2."},
            },
            "required": ["start_room"],
        },
    },
    {
        "name": "mempalace_graph_stats",
        "description": "Palace graph statistics — rooms, hallways, cross-wing tunnels.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "mempalace_find_tunnels",
        "description": (
            "Find cross-wing tunnels — direct semantic links between rooms in different wings."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "wing_a": {"type": "string"},
                "wing_b": {"type": "string"},
            },
        },
    },
    {
        "name": "mempalace_create_tunnel",
        "description": (
            "Create a tunnel between two rooms across wings to bridge cross-cutting entities."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "wing_a": {"type": "string"},
                "room_a": {"type": "string"},
                "wing_b": {"type": "string"},
                "room_b": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["wing_a", "room_a", "wing_b", "room_b"],
        },
    },
    {
        "name": "mempalace_list_tunnels",
        "description": "List tunnels, optionally scoped to a wing.",
        "parameters": {
            "type": "object",
            "properties": {"wing": {"type": "string"}},
        },
    },
    {
        "name": "mempalace_delete_tunnel",
        "description": "Remove a tunnel by id.",
        "parameters": {
            "type": "object",
            "properties": {"tunnel_id": {"type": "string"}},
            "required": ["tunnel_id"],
        },
    },
    {
        "name": "mempalace_follow_tunnels",
        "description": (
            "Follow tunnels outward from a (wing, room) pair to discover connected rooms."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "wing": {"type": "string"},
                "room": {"type": "string"},
            },
            "required": ["wing", "room"],
        },
    },
    {
        "name": "mempalace_memories_filed_away",
        "description": "Show drawers filed (with metadata) during the current session.",
        "parameters": {"type": "object", "properties": {}},
    },
]


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class MempalaceProvider(MemoryProvider):  # type: ignore[misc]
    """Hermes memory provider backed by MemPalace."""

    DEFAULT_COLLECTION_NAME = "mempalace_drawers"
    DEFAULT_PALACE_PATH = "~/.mempalace/palace"
    DEFAULT_IDENTITY_PATH = "~/.mempalace/identity.txt"
    WORKER_QUEUE_MAX = 500
    # Cap for status-style metadata scans. On large palaces (200k+ drawers)
    # an unbounded ``col.get(include=["metadatas"])`` would materialize every
    # row into Python memory just to compute counts — multi-second hangs and
    # OOM risk on small hosts. Above this cap, breakdowns are sampled from
    # the first ``STATUS_SCAN_LIMIT`` drawers and the response carries
    # ``truncated: True`` plus a ``scanned`` count so the caller knows
    # exactly how partial the view is and can compute coverage against the
    # palace total it already has.
    STATUS_SCAN_LIMIT = 5000

    def __init__(self) -> None:
        # Config + lifecycle state
        self._config: Dict[str, Any] = {}
        self._palace_path: str = ""
        self._collection_name: str = self.DEFAULT_COLLECTION_NAME
        self._wing_config: Dict[str, Any] = {}
        self._identity: str = ""
        self._wake_up_cache: str = ""
        self._initialized = False
        self._cron_skipped = False

        # Per-session bookkeeping
        self._session_id: str = ""
        self._hermes_home: str = ""
        self._turn_count = 0

        # ChromaDB access through mempalace's own backend (matches embedding
        # function, fixes the dim-mismatch bug from prior PRs).
        self._backend = None
        self._collection = None
        self._collection_lock = threading.Lock()

        # Background worker for non-blocking writes.
        self._worker_queue: queue.Queue = queue.Queue(maxsize=self.WORKER_QUEUE_MAX)
        self._worker_thread: Optional[threading.Thread] = None
        self._worker_stop = threading.Event()
        self._wake_up_done = threading.Event()
        self._wake_up_done.set()  # no refresh in flight initially

        # initialize() must be serialised against concurrent re-entries so we
        # don't spawn two worker threads sharing one queue.
        self._init_lock = threading.Lock()

    # ----- Required ABC ------------------------------------------------------

    @property
    def name(self) -> str:
        return "mempalace"

    def is_available(self) -> bool:
        """Always True — module-level imports prove mempalace is installed.

        If mempalace were missing, ``import`` of this module would have failed
        before Hermes' plugin loader called ``is_available``. The check is
        kept for ABC conformance and so a future config flag can disable the
        provider here without surgery elsewhere.
        """
        return True

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        # System-generated contexts (cron, flush) would corrupt user representation.
        agent_context = kwargs.get("agent_context", "")
        platform = kwargs.get("platform", "cli")
        if agent_context in {"cron", "flush"} or platform == "cron":
            logger.debug(
                "MemPalace inactive: agent_context=%s, platform=%s",
                agent_context,
                platform,
            )
            with self._init_lock:
                self._cron_skipped = True
            return

        # Serialise the rest: producers all read _initialized / _collection /
        # _worker_thread; a re-entrant initialize() must not duplicate the
        # worker or leave torn-up state visible to a parallel sync_turn.
        with self._init_lock:
            # Clear the cron-skip flag — a previous cron-context initialize on
            # the same instance must not leave the provider permanently inert.
            self._cron_skipped = False

            self._session_id = session_id or ""
            self._hermes_home = str(kwargs.get("hermes_home", "") or "")

            # A bridge write from a previous initialize must not masquerade
            # as a user-set env override during re-resolution.
            _clear_stale_palace_bridge()
            self._config = self._load_config()
            # No hermes-side palace configured → defer to mempalace's own
            # config (env var > ~/.mempalace/config.json > default) so this
            # provider agrees with `mempalace init`-managed setups instead
            # of hardcoding the default location.
            mempalace_config = MempalaceConfig()
            configured = self._config.get("palace_path") or mempalace_config.palace_path
            self._palace_path = str(Path(configured).expanduser())
            # Publish the resolved palace so the mcp_server passthrough
            # tools (drawer CRUD, duplicate check, tunnels, taxonomy)
            # read and write the SAME palace live filing and search use.
            _bridge_palace_env(self._palace_path)
            # Collection name follows mempalace's own config — the same
            # source ``search_memories`` (prefetch / _tool_search) and the
            # mcp_server passthrough read — so live writes land in the
            # collection recall actually searches. Intentionally NOT
            # configurable on the Hermes side: a second knob would let the
            # write and read sides diverge silently, making the provider
            # look mute.
            self._collection_name = mempalace_config.collection_name

            self._load_wing_config()
            self._load_identity()

            # Backend init: failures (slow disk, locked SQLite, missing palace)
            # must not hang Hermes startup. The agent runs without palace
            # context until the next successful initialize().
            backend_ready = False
            try:
                self._backend = ChromaBackend()
                with self._collection_lock:
                    self._collection = self._backend.get_or_create_collection(
                        self._palace_path,
                        self._collection_name,
                    )
                logger.info(
                    "MemPalace: collection '%s' ready (palace=%s)",
                    self._collection_name,
                    self._palace_path,
                )
                backend_ready = True
            except Exception as exc:
                logger.warning("MemPalace backend init failed: %s", exc)
                with self._collection_lock:
                    self._collection = None

            # Background worker for filing — only when the backend opened.
            # Starting a worker against a None collection invites the
            # on_pre_compress / sync_turn data-loss path where the hint
            # promises persistence the worker can't deliver.
            if backend_ready and (
                self._worker_thread is None or not self._worker_thread.is_alive()
            ):
                self._worker_stop.clear()
                self._worker_thread = threading.Thread(
                    target=self._background_worker,
                    daemon=True,
                    name="mempalace-worker",
                )
                self._worker_thread.start()

            # Warm the wake-up cache without blocking startup, but only if the
            # backend is up. Uses the long-lived collection (no second client).
            if backend_ready:
                self._wake_up_done.clear()
                threading.Thread(
                    target=self._refresh_wake_up_cache,
                    daemon=True,
                    name="mempalace-wakeup",
                ).start()

            # ``_initialized`` reflects readiness — get_tool_schemas /
            # handle_tool_call / prefetch / sync_turn / on_pre_compress key
            # off this. A failed backend init leaves it False so callers see
            # a uniformly inactive provider rather than half-broken state.
            self._initialized = backend_ready

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Schemas describe the *interface*, not runtime readiness. Hermes'
        # ``agent.memory_manager._register_provider`` snapshots schemas at
        # registration time (BEFORE ``initialize()`` runs) to build its
        # tool-name → provider routing table; if we returned ``[]`` there,
        # the dispatcher would never learn our tool names and every later
        # call would hit ``"Unknown tool: <name>"`` from the dispatcher
        # without reaching ``handle_tool_call`` at all. Backend readiness
        # is checked at call time in ``handle_tool_call``.
        if self._cron_skipped:
            return []
        return list(TOOL_SCHEMAS)

    # ----- Optional: prompt + recall ----------------------------------------

    def system_prompt_block(self) -> str:
        if self._cron_skipped or not self._initialized:
            return ""
        if not self._identity and not self._wake_up_cache:
            return ""
        parts = ["# MemPalace context"]
        if self._identity:
            parts.append(self._identity)
        if self._wake_up_cache:
            parts.append(self._wake_up_cache)
        return "\n\n".join(parts)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._cron_skipped or not self._initialized or not query:
            return ""
        try:
            n = max(1, min(int(self._config.get("n_prefetch", 3)), 20))
            result = search_memories(
                query,
                palace_path=self._palace_path,
                n_results=n,
            )
            hits = result.get("results", []) if isinstance(result, dict) else []
            if not hits:
                return ""
            lines = ["## MemPalace — relevant context"]
            for r in hits:
                wing = r.get("wing", "")
                room = r.get("room", "")
                tag = f"[{wing}/{room}] " if wing else ""
                text = (r.get("text") or "").strip()
                if text:
                    lines.append(f"{tag}{text}")
            return "\n\n".join(lines)
        except Exception as exc:
            logger.debug("MemPalace prefetch error: %s", exc)
            return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if self._cron_skipped or not self._initialized:
            return
        user_text = _normalize_content(user_content)
        assistant_text = _normalize_content(assistant_content)
        if not user_text and not assistant_text:
            return
        try:
            self._worker_queue.put_nowait(
                (
                    "file_turn",
                    {
                        "user": user_text,
                        "assistant": assistant_text,
                        "session_id": session_id or self._session_id,
                    },
                )
            )
        except queue.Full:
            # Loud, not silent: the verbatim invariant is what mempalace sells.
            # If the queue saturates we want operators to see it.
            logger.warning(
                "MemPalace worker queue full (maxsize=%d) — turn dropped; "
                "writes likely stalled on disk or ChromaDB",
                self.WORKER_QUEUE_MAX,
            )

    # ----- Optional lifecycle hooks ----------------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        self._turn_count = turn_number

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if self._cron_skipped or not self._initialized:
            return
        # Intentionally no filing here. ``sync_turn`` has already filed every
        # completed turn, and re-filing the message list mints duplicate
        # drawers: ``filed_at`` is part of the drawer-id hash, so the upsert
        # cannot collapse the re-file into the original.
        # Regenerate the AAAK wake-up cache for the next session.
        self._wake_up_done.clear()
        threading.Thread(
            target=self._refresh_wake_up_cache,
            daemon=True,
            name="mempalace-wakeup",
        ).start()

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        # Repoint subsequent writes at the new session. /reset and /new flush
        # per-session counters; /resume and /branch keep them.
        self._session_id = new_session_id or ""
        if reset:
            self._turn_count = 0

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Intentionally a no-op that returns no hint.

        Blind-filing the compression window duplicates every turn
        ``sync_turn`` already filed. Returning ``""`` keeps the
        summarizer on its default conservative discarding — a hint must
        never promise persistence this provider hasn't performed.
        """
        return ""

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        # Hermes' memory tool has two targets: "user" (who the user is) and
        # "memory" (the agent's own notes — environment facts, conventions,
        # lessons). "memory" is the tool's DEFAULT when the model omits the
        # field, so filtering to target == "user" would silently drop the
        # majority of writes. Both mirror into the knowledge graph, under
        # distinct subjects (see _mirror_mem_write).
        if (
            self._cron_skipped
            or not self._initialized  # worker isn't running; queueing leaks
            or action != "add"
            or target not in ("user", "memory")
            or not content
        ):
            return
        try:
            self._worker_queue.put_nowait(
                (
                    "mem_write",
                    {
                        "content": content,
                        "target": target,
                        "metadata": dict(metadata or {}),
                    },
                )
            )
        except queue.Full:
            logger.warning("MemPalace queue full at memory_write — entry dropped")

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs: Any,
    ) -> None:
        # Record the (task, result) pair as a synthetic turn so the parent
        # session's recall surfaces the delegated work.
        self.sync_turn(
            f"[delegated task]\n{task}",
            f"[subagent {child_session_id} returned]\n{result}",
            session_id=self._session_id,
        )

    # ----- Tool dispatch ---------------------------------------------------

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        if self._cron_skipped:
            return json.dumps({"error": "MemPalace not active (cron context)."})
        if not self._initialized:
            return json.dumps({"error": "MemPalace not initialized."})
        args = args or {}
        try:
            if tool_name == "mempalace_search":
                return json.dumps(
                    self._tool_search(
                        query=args.get("query", ""),
                        wing=args.get("wing"),
                        room=args.get("room"),
                        n_results=int(args.get("n_results", 5)),
                    )
                )
            if tool_name == "mempalace_status":
                return json.dumps(self._tool_status())
            if tool_name == "mempalace_list_wings":
                return json.dumps(self._tool_list_wings())
            if tool_name == "mempalace_list_rooms":
                return json.dumps(self._tool_list_rooms(args.get("wing", "")))
            if tool_name == "mempalace_kg_query":
                return json.dumps(
                    self._tool_kg_query(
                        entity=args.get("entity", ""),
                        since=args.get("since"),
                    )
                )
            if tool_name == "mempalace_kg_add":
                return json.dumps(
                    self._tool_kg_add(
                        subject=args.get("subject", ""),
                        predicate=args.get("predicate", ""),
                        obj=args.get("object", ""),
                    )
                )
            if tool_name == "mempalace_kg_invalidate":
                return json.dumps(
                    self._tool_kg_invalidate(
                        subject=args.get("subject", ""),
                        predicate=args.get("predicate", ""),
                        obj=args.get("object", ""),
                        ended=args.get("ended"),
                    )
                )
            if tool_name == "mempalace_kg_timeline":
                return json.dumps(self._tool_kg_timeline(args.get("entity")))
            if tool_name == "mempalace_kg_stats":
                return json.dumps(self._tool_kg_stats())
            if tool_name == "mempalace_diary_write":
                return json.dumps(self._tool_diary_write(args.get("entry", "")))
            if tool_name == "mempalace_diary_read":
                return json.dumps(self._tool_diary_read(int(args.get("n", 10))))

            # Tools that delegate directly to ``mempalace.mcp_server``'s
            # public ``tool_*`` entry points. mcp_server resolves its palace
            # from ``MEMPALACE_PALACE_PATH`` on every config access, and
            # ``initialize`` bridges this provider's resolved palace into
            # that variable (see ``_bridge_palace_env``) — so passthrough
            # reads and writes land in the same palace as live filing and
            # search. KG tools are NOT passed through: mcp_server resolves
            # its KG from ``DEFAULT_KG_PATH`` unless its own ``--palace``
            # CLI flag was given, which no env bridge can influence.
            # New tools (everything that has a matching ``tool_*`` in
            # mempalace.mcp_server) dispatch by name derivation. One
            # mempalace asymmetry to remap: ``mempalace_traverse`` maps to
            # ``tool_traverse_graph`` on the mcp_server side.
            result = self._dispatch_mcp_passthrough(tool_name, args)
            if result is not None:
                return result

            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        except Exception as exc:
            logger.exception("MemPalace tool %s failed", tool_name)
            return json.dumps({"error": f"{tool_name} failed: {exc}"})

    # Tools that need name-remapping when dispatching to
    # ``mempalace.mcp_server.tool_*``. Everything else uses
    # ``tool_name.replace("mempalace_", "tool_", 1)`` straight up.
    _MCP_FUNC_REMAP: Dict[str, str] = {
        "mempalace_traverse": "tool_traverse_graph",
    }

    # Allowlist of tools that route through the mcp_server passthrough.
    # Anything NOT here either has explicit handling above (the original
    # eight ``_tool_*`` methods) or returns ``"Unknown tool"``. Using an
    # allowlist (rather than ``getattr(_mp_mcp, name, None)`` only) keeps
    # mempalace's admin / internal tool_* functions hidden from this
    # plugin's surface even if a future caller passes their names.
    _MCP_PASSTHROUGH_TOOLS = frozenset(
        {
            "mempalace_add_drawer",
            "mempalace_update_drawer",
            "mempalace_delete_drawer",
            "mempalace_list_drawers",
            "mempalace_get_drawer",
            "mempalace_check_duplicate",
            "mempalace_get_taxonomy",
            "mempalace_get_aaak_spec",
            "mempalace_traverse",
            "mempalace_graph_stats",
            "mempalace_find_tunnels",
            "mempalace_create_tunnel",
            "mempalace_list_tunnels",
            "mempalace_delete_tunnel",
            "mempalace_follow_tunnels",
            "mempalace_memories_filed_away",
        }
    )

    def _dispatch_mcp_passthrough(self, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
        """Forward an allowlisted tool to its mempalace.mcp_server entry.

        Returns the JSON-encoded result, or ``None`` if the tool isn't in
        the allowlist (so the caller can fall through to the standard
        ``"Unknown tool"`` error).
        """
        if tool_name not in self._MCP_PASSTHROUGH_TOOLS:
            return None
        from mempalace import mcp_server as _mp_mcp

        func_name = self._MCP_FUNC_REMAP.get(tool_name, tool_name.replace("mempalace_", "tool_", 1))
        func = getattr(_mp_mcp, func_name, None)
        if func is None:
            return json.dumps({"error": f"{tool_name}: mempalace.mcp_server.{func_name} not found"})
        # ``add_drawer`` is the only tool that needs a client-side default
        # — tag agent-originated drawers so they're distinguishable from
        # miner-ingested ones.
        if tool_name == "mempalace_add_drawer":
            args.setdefault("added_by", "hermes")
        return json.dumps(func(**args))

    # ----- Setup wizard integration ----------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "palace_path",
                "description": "Path to palace directory.",
                "default": self.DEFAULT_PALACE_PATH,
            },
            {
                "key": "identity_path",
                "description": "Path to identity.txt (L0 wake-up layer).",
                "default": self.DEFAULT_IDENTITY_PATH,
            },
            {
                "key": "wing",
                "description": "Default wing for filing (omit to auto-classify via wing_config.json).",
            },
            {
                "key": "n_prefetch",
                "description": "Number of search results to inject per turn.",
                "default": 3,
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        config_path = Path(hermes_home) / "mempalace.json"
        existing: Dict[str, Any] = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
            except Exception as exc:
                logger.debug("MemPalace config read failed: %s", exc)
        existing.update(values)
        config_path.write_text(json.dumps(existing, indent=2) + "\n")

    def post_setup(self, hermes_home: str, config: Dict[str, Any]) -> None:
        print()
        print("MemPalace provider installed. To finish setup:")
        print("  1. mempalace init <your-project-dir>     # generates ~/.mempalace/")
        print("  2. (optional) edit ~/.mempalace/identity.txt to seed L0 wake-up context")
        print()
        print("Note: palace data lives under ~/.mempalace/, outside $HERMES_HOME.")
        print("`hermes backup` does not include it — back up ~/.mempalace/ separately.")
        print()

    # ----- Shutdown --------------------------------------------------------

    def shutdown(self) -> None:
        self._worker_stop.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5.0)
            if self._worker_thread.is_alive():
                logger.warning("MemPalace worker did not drain within shutdown timeout")

    # ----- Internal: config + wing routing + identity ---------------------

    def _load_config(self) -> Dict[str, Any]:
        config: Dict[str, Any] = {}
        if self._hermes_home:
            config_path = Path(self._hermes_home) / "mempalace.json"
            if config_path.exists():
                try:
                    config.update(json.loads(config_path.read_text()))
                except Exception as exc:
                    logger.debug("MemPalace config load failed: %s", exc)
        for env_key, conf_key in (
            ("MEMPALACE_PALACE_PATH", "palace_path"),
            ("MEMPALACE_IDENTITY_PATH", "identity_path"),
            ("MEMPALACE_WING", "wing"),
        ):
            # Only honor a non-empty env var. ``export MEMPALACE_WING=`` (e.g.
            # from a deactivation script) is intent to *unset*, not to set the
            # wing to the empty string.
            value = os.environ.get(env_key)
            if value:
                config[conf_key] = value
        return config

    def _load_wing_config(self) -> None:
        wing_config_path = Path(self._palace_path).parent / "wing_config.json"
        if not wing_config_path.exists():
            self._wing_config = {}
            logger.debug("MemPalace: no wing_config.json — run `mempalace init` to configure wings")
            return
        try:
            with open(wing_config_path) as f:
                self._wing_config = (json.load(f) or {}).get("wings", {})
        except Exception as exc:
            logger.warning("MemPalace wing config load failed: %s", exc)
            self._wing_config = {}

    def _load_identity(self) -> None:
        identity_path = Path(
            self._config.get("identity_path", self.DEFAULT_IDENTITY_PATH)
        ).expanduser()
        if not identity_path.exists():
            self._identity = ""
            return
        try:
            self._identity = identity_path.read_text(encoding="utf-8").strip()
        except Exception as exc:
            logger.warning("MemPalace identity load failed: %s", exc)
            self._identity = ""

    def _refresh_wake_up_cache(self) -> None:
        """Rebuild L0+L1 using the long-lived collection only.

        Opening a second PersistentClient (via MemoryStack/Layer1) while this
        provider already holds one races local Chroma SQLite — disk I/O errors
        and "Failed to get segments" on the filing worker and subsequent reads.
        """
        try:
            identity = self._identity or ""
            if not identity:
                try:
                    identity = Layer0().render()
                except Exception:
                    identity = ""
            wing = str(self._config.get("wing") or "")
            with self._collection_lock:
                col = self._collection
                if col is None:
                    self._wake_up_cache = identity
                    return
                l1 = _l1_story_from_collection(col, wing=wing)
            parts = []
            if identity:
                parts.append(identity)
                parts.append("")
            parts.append(l1)
            self._wake_up_cache = "\n".join(parts)
        except Exception as exc:
            logger.debug("MemPalace wake-up refresh error: %s", exc)
            self._wake_up_cache = ""
        finally:
            self._wake_up_done.set()

    def _classify_wing(self, text: str) -> str:
        # If the user pinned a default wing in config, honor it without running
        # keyword classification. The config field's whole purpose is "don't
        # auto-classify this profile's turns" — silently keyword-routing on
        # top of it would make the setting functionally dead.
        forced = self._config.get("wing")
        if forced:
            return str(forced)
        return _match_wing_by_keywords(text, self._wing_config)

    # ----- Internal: filing + background worker --------------------------

    def _file_turn(self, payload: Dict[str, Any]) -> None:
        user_msg = payload.get("user", "") or ""
        assistant_msg = payload.get("assistant", "") or ""
        if not user_msg and not assistant_msg:
            return
        # Classify outside the lock — pure CPU / config, no Chroma access.
        text = f"User: {user_msg}\n\nAssistant: {assistant_msg}".strip()
        wing = self._classify_wing(text)
        session_id = payload.get("session_id") or ""
        extra: Dict[str, Any] = {"source": "hermes"}
        if session_id:
            extra["session_id"] = session_id
        # Hold the collection lock across the whole upsert so wake-up L1
        # scans (same client) never interleave with writes mid-compactor.
        try:
            with self._collection_lock:
                col = self._collection
                if col is None:
                    return
                # Always file under a stable room name ("conversations"). Using
                # session_id here would mint one room per session — pollutes
                # ``mempalace_list_rooms`` and splits live writes from backfill
                # drawers (which also write to "conversations"). The session id
                # stays available on the dedicated metadata field below.
                file_conversation_exchange(
                    col,
                    wing=wing,
                    room="conversations",
                    text=text,
                    source_file=f"hermes-session:{session_id or 'unknown'}",
                    agent="hermes",
                    extra_metadata=extra,
                )
        except Exception as exc:
            logger.debug("MemPalace _file_turn error: %s", exc)

    def _mirror_mem_write(self, payload: Dict[str, Any]) -> None:
        # target "user" carries facts about the user; target "memory" carries
        # the agent's own notes. Distinct subjects keep the graph honest about
        # WHO the fact describes — `kg_query("user")` must not surface tool
        # quirks the agent noted about its environment.
        if payload.get("target") == "memory":
            subject, predicate = "hermes", "noted"
        else:
            subject, predicate = "user", "asserted"
        kg: Optional[KnowledgeGraph] = None
        try:
            kg = KnowledgeGraph(db_path=self._kg_db_path())
            kg.add_triple(
                subject=subject,
                predicate=predicate,
                obj=payload.get("content", ""),
            )
        except Exception as exc:
            logger.debug("MemPalace _mirror_mem_write error: %s", exc)
        finally:
            if kg is not None:
                try:
                    kg.close()
                except Exception:
                    pass

    def _background_worker(self) -> None:
        # Drain pending items even after the stop signal — otherwise turns
        # queued just before shutdown would be lost. The compound condition
        # exits only when both: (a) stop signal raised, (b) queue empty.
        while not self._worker_stop.is_set() or not self._worker_queue.empty():
            try:
                task, payload = self._worker_queue.get(timeout=1.0)
            except queue.Empty:
                if self._worker_stop.is_set():
                    break
                continue
            try:
                if task == "file_turn":
                    self._file_turn(payload)
                elif task == "mem_write":
                    self._mirror_mem_write(payload)
            except Exception as exc:
                logger.debug("MemPalace worker task %s error: %s", task, exc)
            finally:
                try:
                    self._worker_queue.task_done()
                except ValueError:
                    pass

    # ----- Tool handlers (delegate to mempalace internals) ----------------

    def _tool_search(
        self,
        query: str,
        wing: Optional[str] = None,
        room: Optional[str] = None,
        n_results: int = 5,
    ) -> Dict[str, Any]:
        if not query:
            return {"error": "Missing required parameter: query"}
        n = max(1, min(int(n_results or 5), 50))
        data = search_memories(
            query,
            palace_path=self._palace_path,
            wing=wing or "",
            room=room or "",
            n_results=n,
        )
        if isinstance(data, dict) and "error" in data:
            return data
        hits = data.get("results", []) if isinstance(data, dict) else []
        return {"results": hits, "count": len(hits)}

    def _scan_metadatas(
        self, col: Any, where: Optional[Dict[str, Any]] = None
    ) -> tuple[List[Dict[str, Any]], bool]:
        """Pull at most ``STATUS_SCAN_LIMIT`` metadata records.

        Returns ``(metas, truncated)``. ``truncated`` is True when the
        underlying collection holds more rows than the cap so the caller
        can surface that to the model.
        """
        cap = self.STATUS_SCAN_LIMIT
        # Fetch one row beyond the cap: it's the only way to tell "exactly
        # cap rows, view is complete" from "more than cap rows, view is
        # partial" — comparing against ``col.count()`` can't answer that
        # for the ``where``-filtered calls (chroma's count() is unfiltered).
        kwargs: Dict[str, Any] = {"include": ["metadatas"], "limit": cap + 1}
        if where:
            kwargs["where"] = where
        try:
            result = col.get(**kwargs)
        except TypeError:
            # Very old chroma versions might not accept ``limit`` on
            # ``get``; fall back to the full scan path.
            result = col.get(include=["metadatas"], **({"where": where} if where else {}))
        metas = result.get("metadatas") or []
        truncated = len(metas) > cap
        return metas[:cap], truncated

    def _tool_status(self) -> Dict[str, Any]:
        with self._collection_lock:
            col = self._collection
        if col is None:
            return {"error": "MemPalace not initialized"}
        total = col.count()
        metas, truncated = self._scan_metadatas(col)
        wings: Dict[str, int] = {}
        for m in metas:
            # Legacy palaces / raw writers can leave None metadata entries.
            w = (m or {}).get("wing", "unknown")
            wings[w] = wings.get(w, 0) + 1
        out: Dict[str, Any] = {
            "total_drawers": total,
            "wings": wings,
            "palace_path": self._palace_path,
        }
        if truncated:
            # ``total_drawers`` already gives the model the 100% reference;
            # ``scanned`` lets it compute coverage = scanned / total_drawers
            # and qualify any wing claim accordingly.
            out["truncated"] = True
            out["scanned"] = len(metas)
        return out

    def _tool_list_wings(self) -> Dict[str, Any]:
        with self._collection_lock:
            col = self._collection
        if col is None:
            return {"error": "MemPalace not initialized"}
        metas, truncated = self._scan_metadatas(col)
        wings: Dict[str, int] = {}
        for m in metas:
            w = (m or {}).get("wing", "unknown")
            wings[w] = wings.get(w, 0) + 1
        out: Dict[str, Any] = {"wings": wings}
        if truncated:
            out["truncated"] = True
            out["scanned"] = len(metas)
            # Palace total is the model's 100% reference — same shape as
            # ``_tool_status`` so a coverage ratio can be computed without
            # an additional tool call.
            out["total_drawers"] = col.count()
        return out

    def _tool_list_rooms(self, wing: str) -> Dict[str, Any]:
        if not wing:
            return {"error": "Missing required parameter: wing"}
        with self._collection_lock:
            col = self._collection
        if col is None:
            return {"error": "MemPalace not initialized"}
        metas, truncated = self._scan_metadatas(col, where={"wing": wing})
        rooms: Dict[str, int] = {}
        for m in metas:
            r = (m or {}).get("room", "unknown")
            rooms[r] = rooms.get(r, 0) + 1
        out: Dict[str, Any] = {"wing": wing, "rooms": rooms}
        if truncated:
            out["truncated"] = True
            out["scanned"] = len(metas)
            # ChromaDB's ``count()`` doesn't support ``where=`` filtering
            # in the versions mempalace pins, so we can't cheaply give an
            # exact wing total. The model can still see this view is partial
            # via the truncated/scanned pair.
        return out

    def _kg_db_path(self) -> str:
        """The provider's knowledge-graph DB — sibling of the palace dir.

        Single source of truth for every KG access in this plugin; the KG
        tools must never fall back to mempalace's global ``DEFAULT_KG_PATH``
        or they would read a different graph than the one live turns feed.
        """
        return str(Path(self._palace_path).parent / "knowledge_graph.sqlite3")

    def _tool_kg_query(self, entity: str, since: Optional[str] = None) -> Dict[str, Any]:
        if not entity:
            return {"error": "Missing required parameter: entity"}
        kg = KnowledgeGraph(db_path=self._kg_db_path())
        try:
            relations = kg.query_entity(entity, as_of=since or "")
        finally:
            try:
                kg.close()
            except Exception:
                pass
        return {"entity": entity, "relations": relations}

    def _tool_kg_add(self, subject: str, predicate: str, obj: str) -> Dict[str, Any]:
        if not (subject and predicate and obj):
            return {"error": "subject, predicate, object are all required"}
        kg = KnowledgeGraph(db_path=self._kg_db_path())
        try:
            kg.add_triple(subject=subject, predicate=predicate, obj=obj)
        finally:
            try:
                kg.close()
            except Exception:
                pass
        return {"status": "ok", "triple": [subject, predicate, obj]}

    def _tool_kg_invalidate(
        self,
        subject: str,
        predicate: str,
        obj: str,
        ended: Optional[str] = None,
    ) -> Dict[str, Any]:
        # Same validation semantics as mcp_server.tool_kg_invalidate, but
        # against the provider's own KG (see _kg_db_path).
        try:
            subject = sanitize_kg_value(subject, "subject")
            predicate = sanitize_name(predicate, "predicate")
            obj = sanitize_kg_value(obj, "object")
            ended = sanitize_iso_temporal(ended, "ended")
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        resolved_ended = ended or date.today().isoformat()
        kg = KnowledgeGraph(db_path=self._kg_db_path())
        try:
            kg.invalidate(subject, predicate, obj, ended=resolved_ended)
        finally:
            try:
                kg.close()
            except Exception:
                pass
        return {
            "success": True,
            "fact": f"{subject} → {predicate} → {obj}",
            "ended": resolved_ended,
        }

    def _tool_kg_timeline(self, entity: Optional[str] = None) -> Dict[str, Any]:
        if entity is not None:
            try:
                entity = sanitize_kg_value(entity, "entity")
            except ValueError as exc:
                return {"error": str(exc)}
        kg = KnowledgeGraph(db_path=self._kg_db_path())
        try:
            results = kg.timeline(entity) if entity else kg.timeline()
        finally:
            try:
                kg.close()
            except Exception:
                pass
        return {"entity": entity or "all", "timeline": results, "count": len(results)}

    def _tool_kg_stats(self) -> Dict[str, Any]:
        kg = KnowledgeGraph(db_path=self._kg_db_path())
        try:
            return kg.stats()
        finally:
            try:
                kg.close()
            except Exception:
                pass

    def _tool_diary_write(self, entry: str) -> Dict[str, Any]:
        if not entry:
            return {"error": "Missing required parameter: entry"}
        diary_path = Path(self._palace_path).parent / "diary.jsonl"
        diary_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now(timezone.utc).isoformat(), "entry": entry}
        with open(diary_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return {"status": "ok"}

    def _tool_diary_read(self, n: int = 10) -> Dict[str, Any]:
        if n <= 0:
            return {"entries": []}
        diary_path = Path(self._palace_path).parent / "diary.jsonl"
        if not diary_path.exists():
            return {"entries": []}
        # Stream the file and keep only the trailing ``n`` lines. Avoids
        # loading multi-megabyte diaries into memory just to discard the
        # head.
        with open(diary_path, encoding="utf-8") as f:
            tail = deque(f, maxlen=n)
        recent: List[Dict[str, Any]] = []
        for raw_line in tail:
            try:
                recent.append(json.loads(raw_line))
            except json.JSONDecodeError:
                logger.debug("MemPalace: skipping malformed diary line")
        return {"entries": recent}


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:
    """Register the MemPalace memory provider with Hermes."""
    ctx.register_memory_provider(MempalaceProvider())
