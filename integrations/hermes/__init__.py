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

* The provider is **inactive** under ``agent_context in {"cron", "flush"}``
  or ``platform == "cron"``. Cron-context turns are system-generated and
  would otherwise corrupt the user's representation.

* Configuration is read from ``$HERMES_HOME/mempalace.json`` first, then
  env vars (``MEMPALACE_PALACE_PATH``, ``MEMPALACE_IDENTITY_PATH``,
  ``MEMPALACE_WING``, ``MEMPALACE_COLLECTION_NAME``), then defaults.

* ``~/.mempalace/identity.txt`` (L0) and ``~/.mempalace/wing_config.json``
  are loaded if present but never created here. Run
  ``mempalace init <project-dir>`` to generate them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from mempalace.backends.chroma import ChromaBackend
from mempalace.knowledge_graph import KnowledgeGraph
from mempalace.layers import MemoryStack
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

            self._config = self._load_config()
            self._palace_path = str(
                Path(self._config.get("palace_path", self.DEFAULT_PALACE_PATH)).expanduser()
            )
            # Collection name is intentionally **not** configurable. ``_file_turn``
            # writes through ``self._collection``; ``prefetch`` / ``_tool_search``
            # go through ``search_memories``, which reads its own configured
            # collection name from ``~/.mempalace/config.json``. Exposing two
            # ways to set the name invites write-here, read-there mismatches
            # that silently make the provider look mute.
            self._collection_name = self.DEFAULT_COLLECTION_NAME

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
            # backend is up — otherwise MemoryStack reads a half-set palace.
            if backend_ready:
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
        if self._cron_skipped or not self._initialized:
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
        if self._cron_skipped:
            return
        try:
            self._worker_queue.put_nowait(
                (
                    "session_end",
                    {"messages": list(messages or []), "session_id": self._session_id},
                )
            )
        except queue.Full:
            pass
        # Regenerate the AAAK wake-up cache for the next session.
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
        """File the messages about to be discarded and signal verbatim persistence.

        The hint is **only** returned when the worker can actually persist the
        payload — if the backend never came up or the queue is saturated, we
        say nothing so the summarizer falls back to its default conservative
        discarding rather than acting on a false promise.
        """
        if self._cron_skipped or not self._initialized:
            return ""
        try:
            self._worker_queue.put_nowait(
                (
                    "pre_compress",
                    {"messages": list(messages or []), "session_id": self._session_id},
                )
            )
        except queue.Full:
            logger.warning(
                "MemPalace queue full at pre_compress — %d messages will not be filed",
                len(messages or []),
            )
            return ""
        return (
            "MemPalace has filed every message in this window verbatim. "
            "Compressed content remains searchable via the `mempalace_search` "
            "tool — the summarizer can be aggressive about discarding raw turns."
        )

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self._cron_skipped or action != "add" or target != "user" or not content:
            return
        try:
            self._worker_queue.put_nowait(
                (
                    "mem_write",
                    {"content": content, "metadata": dict(metadata or {})},
                )
            )
        except queue.Full:
            pass

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
            if tool_name == "mempalace_diary_write":
                return json.dumps(self._tool_diary_write(args.get("entry", "")))
            if tool_name == "mempalace_diary_read":
                return json.dumps(self._tool_diary_read(int(args.get("n", 10))))
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        except Exception as exc:
            logger.exception("MemPalace tool %s failed", tool_name)
            return json.dumps({"error": f"{tool_name} failed: {exc}"})

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
        try:
            stack = MemoryStack(palace_path=self._palace_path)
            wing = self._config.get("wing") or ""
            self._wake_up_cache = stack.wake_up(wing=wing) or ""
        except Exception as exc:
            logger.debug("MemPalace wake-up refresh error: %s", exc)
            self._wake_up_cache = ""

    def _classify_wing(self, text: str) -> str:
        if not self._wing_config:
            return "wing_general"
        text_lower = text.lower()
        for wing_name, wing_def in self._wing_config.items():
            keywords = wing_def.get("keywords", []) if isinstance(wing_def, dict) else []
            if any(kw.lower() in text_lower for kw in keywords):
                return wing_name
        return "wing_general"

    # ----- Internal: filing + background worker --------------------------

    def _file_turn(self, payload: Dict[str, Any]) -> None:
        user_msg = payload.get("user", "") or ""
        assistant_msg = payload.get("assistant", "") or ""
        if not user_msg and not assistant_msg:
            return
        with self._collection_lock:
            col = self._collection
        if col is None:
            return
        try:
            text = f"User: {user_msg}\n\nAssistant: {assistant_msg}".strip()
            wing = self._classify_wing(text)
            room = payload.get("session_id") or "conversations"
            ts = datetime.now(timezone.utc).isoformat()
            # SHA-256 over (timestamp + full content). Earlier audits of this
            # plugin found that a prefix hash caused silent collisions on long
            # repeated turns.
            doc_id = hashlib.sha256(f"{ts}:{text}".encode("utf-8")).hexdigest()[:32]
            col.upsert(
                ids=[doc_id],
                documents=[text],
                metadatas=[
                    {
                        "wing": wing,
                        "room": room,
                        "source": "hermes",
                        "ts": ts,
                    }
                ],
            )
        except Exception as exc:
            logger.debug("MemPalace _file_turn error: %s", exc)

    def _mine_session(self, payload: Dict[str, Any]) -> None:
        messages = payload.get("messages", []) or []
        session_id = payload.get("session_id", "") or ""
        try:
            for idx, msg in enumerate(messages):
                if msg.get("role") != "user":
                    continue
                content = msg.get("content", "") or ""
                if not content:
                    continue
                assistant_content = ""
                if idx + 1 < len(messages) and messages[idx + 1].get("role") == "assistant":
                    assistant_content = messages[idx + 1].get("content", "") or ""
                self._file_turn(
                    {
                        "user": content,
                        "assistant": assistant_content,
                        "session_id": session_id,
                    }
                )
        except Exception as exc:
            logger.debug("MemPalace _mine_session error: %s", exc)

    def _mirror_mem_write(self, payload: Dict[str, Any]) -> None:
        db_path = str(Path(self._palace_path).parent / "knowledge_graph.sqlite3")
        kg: Optional[KnowledgeGraph] = None
        try:
            kg = KnowledgeGraph(db_path=db_path)
            kg.add_triple(
                subject="user",
                predicate="asserted",
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
                elif task == "session_end":
                    self._mine_session(payload)
                elif task == "pre_compress":
                    # Pair adjacent (user, assistant) messages into turns and
                    # file each pair. Filing only role==user would silently
                    # drop assistant content the ``on_pre_compress`` hint
                    # promised the summarizer was searchable.
                    msgs = payload.get("messages", []) or []
                    session_id = payload.get("session_id", "") or ""
                    i = 0
                    while i < len(msgs):
                        msg = msgs[i]
                        if msg.get("role") != "user":
                            # Lone non-user message (orphan tool result, etc.)
                            # — file under user= empty so we don't lose it.
                            self._file_turn(
                                {
                                    "user": "",
                                    "assistant": _normalize_content(msg.get("content")),
                                    "session_id": session_id,
                                }
                            )
                            i += 1
                            continue
                        user_content = _normalize_content(msg.get("content"))
                        assistant_content = ""
                        if i + 1 < len(msgs) and msgs[i + 1].get("role") == "assistant":
                            assistant_content = _normalize_content(msgs[i + 1].get("content"))
                            i += 2
                        else:
                            i += 1
                        self._file_turn(
                            {
                                "user": user_content,
                                "assistant": assistant_content,
                                "session_id": session_id,
                            }
                        )
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

    def _tool_status(self) -> Dict[str, Any]:
        with self._collection_lock:
            col = self._collection
        if col is None:
            return {"error": "MemPalace not initialized"}
        count = col.count()
        metas = col.get(include=["metadatas"]).get("metadatas") or []
        wings: Dict[str, int] = {}
        for m in metas:
            w = m.get("wing", "unknown")
            wings[w] = wings.get(w, 0) + 1
        return {
            "total_drawers": count,
            "wings": wings,
            "palace_path": self._palace_path,
        }

    def _tool_list_wings(self) -> Dict[str, Any]:
        with self._collection_lock:
            col = self._collection
        if col is None:
            return {"error": "MemPalace not initialized"}
        metas = col.get(include=["metadatas"]).get("metadatas") or []
        wings: Dict[str, int] = {}
        for m in metas:
            w = m.get("wing", "unknown")
            wings[w] = wings.get(w, 0) + 1
        return {"wings": wings}

    def _tool_list_rooms(self, wing: str) -> Dict[str, Any]:
        if not wing:
            return {"error": "Missing required parameter: wing"}
        with self._collection_lock:
            col = self._collection
        if col is None:
            return {"error": "MemPalace not initialized"}
        metas = col.get(where={"wing": wing}, include=["metadatas"]).get("metadatas") or []
        rooms: Dict[str, int] = {}
        for m in metas:
            r = m.get("room", "unknown")
            rooms[r] = rooms.get(r, 0) + 1
        return {"wing": wing, "rooms": rooms}

    def _tool_kg_query(self, entity: str, since: Optional[str] = None) -> Dict[str, Any]:
        if not entity:
            return {"error": "Missing required parameter: entity"}
        db_path = str(Path(self._palace_path).parent / "knowledge_graph.sqlite3")
        kg = KnowledgeGraph(db_path=db_path)
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
        db_path = str(Path(self._palace_path).parent / "knowledge_graph.sqlite3")
        kg = KnowledgeGraph(db_path=db_path)
        try:
            kg.add_triple(subject=subject, predicate=predicate, obj=obj)
        finally:
            try:
                kg.close()
            except Exception:
                pass
        return {"status": "ok", "triple": [subject, predicate, obj]}

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
