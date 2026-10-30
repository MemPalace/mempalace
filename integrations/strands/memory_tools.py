"""Strands Agents adapter for MemPalace memory system.

Gives any Strands-based agent long-term memory via three tools:

    mp_kb_search     — semantic search in a knowledge base wing
    mp_memory_recall — recall past conversations (per-user isolated)
    mp_memory_store  — store user content verbatim for future recall

Usage:
    from memory_tools import MEMORY_TOOLS
    agent = Agent(model=your_model, tools=MEMORY_TOOLS)
    agent("remember I prefer weekly reports", user_id="alice")

Requirements:
    pip install strands-agents mempalace

Palace path resolved from (in order):
    1. MEMPALACE_PATH environment variable
    2. Default: ~/.mempalace/palace

User isolation: tools that accept ToolContext use
invocation_state["user_id"] to scope reads/writes to a per-user room.
The agent cannot override this — it is injected server-side by the
caller via agent(..., user_id="xxx").

License: MIT
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Palace path — configurable via environment, sensible default.
PALACE_PATH = os.environ.get("MEMPALACE_PATH", os.path.expanduser("~/.mempalace/palace"))

# ---------------------------------------------------------------------------
# Lazy loaders — avoid import cost on module load. MemPalace pulls in
# chromadb + numpy + grpcio which are heavy; only pay that cost when a
# tool is actually called.
# ---------------------------------------------------------------------------

_searcher = None
_collection = None


def _get_searcher():
    """Lazy-load mempalace search function."""
    global _searcher
    if _searcher is None:
        try:
            from mempalace.searcher import search_memories
            _searcher = search_memories
        except ImportError:
            logger.warning(
                "mempalace is not installed — memory tools will return errors. "
                "Install with: pip install mempalace"
            )
            _searcher = False
    return _searcher


def _get_collection():
    """Lazy-load palace collection for write operations."""
    global _collection
    if _collection is None:
        try:
            from mempalace.palace import get_collection
            _collection = get_collection(PALACE_PATH, create=True)
        except ImportError:
            logger.warning("mempalace is not installed — memory_store disabled.")
            _collection = False
        except Exception as exc:
            logger.error("Failed to open palace at %s: %s", PALACE_PATH, exc)
            _collection = False
    return _collection


# ---------------------------------------------------------------------------
# Tools — import strands lazily so this file can be read/linted without
# strands-agents installed.
# ---------------------------------------------------------------------------

try:
    from strands import tool, ToolContext
except ImportError:
    raise ImportError(
        "strands-agents is required for this integration. "
        "Install with: pip install strands-agents"
    )


@tool
def mp_kb_search(query: str, wing: str = "default") -> str:
    """Search the knowledge base for relevant information.

    Use when the user asks HOW to do something, needs documentation,
    or wants background knowledge on a topic.

    Args:
        query: Natural language search query.
        wing: Knowledge base wing to search (default: \"default\").
    """
    searcher = _get_searcher()
    if not searcher:
        return "Knowledge base unavailable (mempalace not installed)."

    try:
        result = searcher(
            query=query,
            palace_path=PALACE_PATH,
            wing=wing,
            n_results=5,
        )
    except Exception as exc:
        logger.error("mp_kb_search error: %s", exc)
        return f"Knowledge base search failed: {exc}"

    if "error" in result:
        return f"Search error: {result['error']}"

    hits = result.get("results", [])
    if not hits:
        return "No results found in the knowledge base for this query."

    passages = []
    for i, hit in enumerate(hits, 1):
        text = hit.get("text", "").strip()
        source = hit.get("source_file", "unknown")
        if text:
            passages.append(f"[{i}] ({source}):\n{text}")

    return "\n\n---\n\n".join(passages)


@tool(context=True)
def mp_memory_recall(query: str, tool_context: ToolContext) -> str:
    """Recall past conversations and user preferences from memory.

    Use at the start of interactions to check for relevant context,
    or when the user refers to something discussed previously.

    Args:
        query: What to recall (e.g. \"preferences\", \"last discussion\").
    """
    searcher = _get_searcher()
    if not searcher:
        return "Memory unavailable (mempalace not installed)."

    invocation_state = getattr(tool_context, "invocation_state", None) or {}
    user_id = invocation_state.get("user_id") or "default"

    try:
        result = searcher(
            query=query,
            palace_path=PALACE_PATH,
            wing="conversations",
            room=f"user-{user_id}" if user_id != "default" else None,
            n_results=3,
        )
    except Exception as exc:
        logger.error("mp_memory_recall error: %s", exc)
        return "Failed to access memory."

    if "error" in result:
        return "No saved memories found."

    hits = result.get("results", [])
    if not hits:
        return "No saved memories found for this query."

    memories = []
    for hit in hits:
        text = hit.get("text", "").strip()
        if text:
            memories.append(f"\u2022 {text}")

    return "From memory:\n" + "\n".join(memories)


@tool(context=True)
def mp_memory_store(content: str, tool_context: ToolContext) -> str:
    """Store user-provided content verbatim in their memory drawer.

    The content is filed exactly as provided — no summarization, no
    rewriting, no extraction. The stored drawer document will be
    byte-for-byte identical to the input.

    Use when the user expresses a preference, states an important fact,
    or explicitly asks you to remember something for future sessions.

    Args:
        content: Text to store verbatim in the user's memory drawer.
    """
    col = _get_collection()
    if col is False:
        return "Memory unavailable \u2014 content not stored."

    invocation_state = getattr(tool_context, "invocation_state", None) or {}
    user_id = invocation_state.get("user_id") or "default"

    try:
        from mempalace.palace import mine_lock, NORMALIZE_VERSION
    except ImportError:
        # Fallback if mine_lock not available (older mempalace versions)
        from contextlib import nullcontext as mine_lock
        NORMALIZE_VERSION = 2

    try:
        ts = datetime.now(timezone.utc).isoformat()
        drawer_id = hashlib.sha256(f"{content}{ts}".encode()).hexdigest()[:16]
        source_ref = f"strands_agent_memory/{user_id}"

        with mine_lock(source_ref):
            col.add(
                ids=[f"memory_{drawer_id}"],
                documents=[content],
                metadatas=[{
                    "wing": "conversations",
                    "room": f"user-{user_id}",
                    "source_file": source_ref,
                    "filed_at": ts,
                    "normalize_version": NORMALIZE_VERSION,
                    "type": "user_memory",
                }],
            )
        logger.info("Memory stored for user %s: %s", user_id, content[:50])
        return f"Stored verbatim: {content[:100]}"

    except Exception as exc:
        logger.error("mp_memory_store error: %s", exc)
        return f"Failed to store memory: {exc}"


# Barrel export \u2014 add to your Agent's tools list.
MEMORY_TOOLS = [mp_kb_search, mp_memory_recall, mp_memory_store]
