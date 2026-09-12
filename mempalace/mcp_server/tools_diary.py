# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


# ==================== AGENT DIARY ====================


def tool_diary_write(agent_name: str, entry: str, topic: str = "general", wing: str = ""):
    """
    Write a diary entry for this agent. Entries are timestamped and
    accumulate over time in a diary room.

    This is the agent's personal journal — observations, thoughts,
    what it worked on, what it noticed, what it thinks matters.

    Note: ``agent_name`` is normalized to lowercase before storage so
    that diary reads are case-insensitive (see #1243). "Claude",
    "claude", and "CLAUDE" all resolve to the same agent.
    """
    from ..daemon import LOCK_REFUSAL_ERROR_CLASS
    from ..palace import MineAlreadyRunning

    try:
        agent_name = sanitize_name(agent_name, "agent_name").lower()
        entry = sanitize_content(entry)
        topic = sanitize_name(topic, "topic")
    except ValueError as e:
        return {"success": False, "error": str(e)}

    if wing:
        wing = sanitize_name(wing)
    else:
        wing = f"wing_{agent_name.replace(' ', '_')}"
    room = "diary"
    col = _get_collection(create=True)
    if not col:
        return _collection_error_or_no_palace()

    now = datetime.now()
    entry_id = (
        f"diary_{wing}_{now.strftime('%Y%m%d_%H%M%S%f')}_"
        f"{hashlib.sha256(entry.encode()).hexdigest()[:12]}"
    )

    _wal_log(
        "diary_write",
        {
            "agent_name": agent_name,
            "topic": topic,
            "entry_id": entry_id,
            "entry_preview": entry[:200],
        },
    )

    try:
        # TODO: Future versions should expand AAAK before embedding to improve
        # semantic search quality. For now, store raw AAAK in metadata so it's
        # preserved, and keep the document as-is for embedding (even though
        # compressed AAAK degrades embedding quality).
        base_metadata = {
            "wing": wing,
            "room": room,
            "hall": "hall_diary",
            "topic": topic,
            "type": "diary_entry",
            "agent": agent_name,
            "filed_at": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
        }
        chunk_size = _config.chunk_size
        if len(entry) <= chunk_size:
            col.add(
                ids=[entry_id],
                documents=[entry],
                metadatas=[{**base_metadata, "chunk_index": 0}],
            )
            logger.info(f"Diary entry: {entry_id} -> {wing}/diary/{topic}")
            return {
                "success": True,
                "entry_id": entry_id,
                "agent": agent_name,
                "topic": topic,
                "timestamp": now.isoformat(),
                "chunks": 1,
            }

        # Oversized entry: split into bounded per-chunk drawers so the
        # embedding model never sees a document above ``chunk_size``.
        # Every chunk carries ``chunk_index`` for ordered reconstruction
        # and the group id under BOTH ``parent_entry_id`` (the original
        # #1539 key, kept so existing readers and palaces are unaffected)
        # and ``parent_drawer_id`` (the key the logical-id read paths were
        # built around in #1782) -- see ``_PARENT_ID_KEYS`` (#2185).
        # Note on ``entry_id`` in the return value: for the chunked path
        # the returned ``entry_id`` is the LOGICAL group handle -- no
        # drawer is stored under that exact id, but it resolves through
        # ``mempalace_get_drawer`` / ``update_drawer`` / ``delete_drawer``
        # to the whole entry, exactly as an oversized ``add_drawer`` id
        # does. The physical drawer ids remain available in ``chunk_ids``.
        # Use a single batched ``add`` so the embedding pass either
        # commits all chunks or none — avoids a half-written palace
        # if the embedding model fails mid-loop. ``col.add`` (not
        # ``upsert``) is intentional here: ``entry_id`` is timestamp-
        # based with microsecond precision, so every call generates a
        # fresh id and a duplicate is by definition a same-microsecond
        # clash that should surface as an error rather than silently
        # overwrite the prior entry (cf. ``tool_add_drawer`` whose
        # content-hash ids are deliberately idempotent and use upsert).
        chunk_ids: list[str] = []
        chunk_docs: list[str] = []
        chunk_metas: list[dict] = []
        for i in range(0, len(entry), chunk_size):
            chunk_idx = i // chunk_size
            chunk_ids.append(f"{entry_id}_chunk_{chunk_idx:06d}")
            chunk_docs.append(entry[i : i + chunk_size])
            chunk_metas.append(
                {
                    **base_metadata,
                    "chunk_index": chunk_idx,
                    "parent_entry_id": entry_id,
                    "parent_drawer_id": entry_id,
                }
            )
        col.add(ids=chunk_ids, documents=chunk_docs, metadatas=chunk_metas)
        logger.info(f"Diary entry: {entry_id} -> {wing}/diary/{topic} ({len(chunk_ids)} chunks)")
        return {
            "success": True,
            "entry_id": entry_id,
            "agent": agent_name,
            "topic": topic,
            "timestamp": now.isoformat(),
            "chunks": len(chunk_ids),
            "chunk_ids": chunk_ids,
        }
    except MineAlreadyRunning as e:
        # Order matters: this typed handler precedes the bare Exception below,
        # mirroring tool_mine / tool_sync. The lock wraps ``col.add`` itself, so
        # a peer holding it means no entry was filed -- a refusal, not a write
        # failure -- and the daemon defers such a job rather than dead-lettering
        # it (#2014). Swallowed into the generic branch the refusal loses
        # ``error_class``, becomes indistinguishable from a genuine write error,
        # and the queued diary entry is dropped. The daemon's constant, not a
        # literal: the two sides are a wire contract, and drift on either end
        # silently un-fixes #2014.
        return {
            "success": False,
            "error": f"another mine is in progress: {e}",
            "error_class": LOCK_REFUSAL_ERROR_CLASS,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_diary_read(agent_name: str, last_n: int = 10, wing: str = ""):
    """
    Read an agent's recent diary entries. Returns the last N entries
    in chronological order — the agent's personal journal.

    When ``wing`` is provided, reads only from that wing. When ``wing``
    is empty or omitted, returns entries from every wing this agent has
    written to. Diary writes from hooks land in project-derived wings
    (``wing_<project>``), so requiring a specific wing on read would
    silo those entries from agent-initiated reads.

    Note: ``agent_name`` is normalized to lowercase before filtering so
    that reads are case-insensitive (see #1243). Entries written under
    pre-fix mixed-case agent names will not match the lowercase filter;
    use ``mempalace repair`` to migrate legacy data if needed.
    """
    try:
        agent_name = sanitize_name(agent_name, "agent_name").lower()
        if wing:
            wing = sanitize_name(wing)
    except ValueError as e:
        return {"error": str(e)}
    last_n = max(1, min(last_n, 100))
    col = _get_collection()
    if not col:
        return _collection_error_or_no_palace()

    # Build filter: always scope by agent + room=diary. Wing is optional —
    # when empty, return entries across all wings for this agent (matches
    # the #1097 empty-string-as-no-filter convention for LLM ergonomics).
    conditions = [{"room": "diary"}, {"agent": agent_name}]
    if wing:
        conditions.insert(0, {"wing": wing})

    try:
        where = {"$and": conditions}
        entries = []
        total = 0
        offset = 0

        while True:
            results = col.get(
                where=where,
                include=["documents", "metadatas"],
                limit=_DIARY_READ_PAGE_SIZE,
                offset=offset,
            )
            batch_ids = _chroma_field(results, "ids", []) or []
            if not batch_ids:
                break

            documents = _chroma_field(results, "documents", []) or []
            metadatas = _chroma_field(results, "metadatas", []) or []
            total += len(batch_ids)

            for index in range(len(batch_ids)):
                doc = documents[index] if index < len(documents) else ""
                meta = _safe_meta(metadatas[index] if index < len(metadatas) else None)
                entries.append(
                    {
                        "date": meta.get("date", ""),
                        "timestamp": meta.get("filed_at", ""),
                        "topic": meta.get("topic", ""),
                        "content": doc,
                    }
                )

            # Keep memory bounded while scanning: only candidates for the
            # final newest-N result need to survive into the next page.
            entries.sort(key=lambda x: x["timestamp"], reverse=True)
            del entries[last_n:]

            offset += len(batch_ids)
            if len(batch_ids) < _DIARY_READ_PAGE_SIZE:
                break

        if total == 0:
            return {"agent": agent_name, "entries": [], "message": "No diary entries yet."}

        return {
            "agent": agent_name,
            "entries": entries,
            "total": total,
            "showing": len(entries),
        }
    except Exception:
        logger.exception("diary_read failed")
        return {"error": "Failed to read diary entries"}


def tool_hook_settings(silent_save: bool = None, desktop_toast: bool = None):
    """
    Get or set hook behavior settings.

    - silent_save: True = stop hook saves directly (no MCP clutter),
      False = legacy blocking MCP calls. Default: True.
    - desktop_toast: True = show notify-send desktop toast on save,
      False = terminal-only notification. Default: False.

    Call with no arguments to see current settings.
    """
    from ..config import MempalaceConfig

    try:
        config = MempalaceConfig()
    except Exception as e:
        return {"success": False, "error": str(e)}

    changed = []
    if silent_save is not None:
        config.set_hook_setting("silent_save", silent_save)
        changed.append(f"silent_save → {silent_save}")
    if desktop_toast is not None:
        config.set_hook_setting("desktop_toast", desktop_toast)
        changed.append(f"desktop_toast → {desktop_toast}")

    # Re-read to return current state
    try:
        config = MempalaceConfig()
    except Exception:
        logger.debug("Could not re-read config after update", exc_info=True)

    result = {
        "success": True,
        "settings": {
            "silent_save": config.hook_silent_save,
            "desktop_toast": config.hook_desktop_toast,
        },
    }
    if changed:
        result["updated"] = changed
    return result


def tool_memories_filed_away():
    """Acknowledge the latest silent checkpoint. Returns a short summary."""
    state_dir = Path.home() / ".mempalace" / "hook_state"
    ack_file = state_dir / "last_checkpoint"
    if not ack_file.is_file():
        return {
            "status": "quiet",
            "message": "No recent journal entry",
            "count": 0,
            "timestamp": None,
        }
    try:
        data = json.loads(ack_file.read_text(encoding="utf-8"))
        ack_file.unlink(missing_ok=True)
        msgs = data.get("msgs", 0)
        return {
            "status": "ok",
            "message": f"\u2726 {msgs} messages tucked into drawers",
            "count": msgs,
            "timestamp": data.get("ts", None),
        }
    except (json.JSONDecodeError, OSError):
        ack_file.unlink(missing_ok=True)
        return {
            "status": "error",
            "message": "\u2726 Journal entry filed in the palace",
            "count": 0,
            "timestamp": None,
        }


# ==================== SETTINGS TOOLS ====================


def _attach_stale_library_warning(result: dict) -> dict:
    """Stamp the stale-library write-guard state onto a reconnect result.

    Reconnect reopens the database but cannot reload Python modules, so it
    never clears the stale-library gate (#899). Without this, a reconnect
    after an upgrade answers "success: Reconnected to palace" while every
    write keeps failing — the caller has no way to tell the two states
    apart from the reconnect result alone.
    """
    payload = _stale_library_payload()
    if payload.get("stale") and "gate_disabled_by" not in payload:
        described = ", ".join(
            f"{entry['package']} {entry['serving']} -> {entry['installed']}"
            for entry in payload.get("packages", [])
        )
        result["library_versions"] = payload
        result["restart_required"] = True
        result["warning"] = (
            f"Reconnected, but this server is still running superseded code ({described}); "
            "writes stay refused until the MCP server (or the host application that "
            "spawned it) is restarted — reconnect cannot reload Python modules."
        )
    return result


def tool_reconnect():
    """Force the MCP server to drop cached ChromaDB + KnowledgeGraph state.

    Use after external scripts or CLI commands modify the palace database
    or replace ``knowledge_graph.sqlite3`` directly, which can leave the
    in-memory HNSW index stale or pin a closed-on-disk SQLite connection.
    """
    global \
        _client_cache, \
        _collection_cache, \
        _collection_cache_backend, \
        _collection_cache_palace, \
        _collection_open_error, \
        _palace_db_inode, \
        _palace_db_mtime, \
        _vector_disabled, \
        _vector_disabled_reason
    from .. import palace as palace_module

    close_errors = []
    palace_ref = PalaceRef(id=_config.palace_path, local_path=_config.palace_path)
    closed_backend_names = set()
    cached_backend_name = _collection_cache_backend
    try:
        backend = palace_module.get_backend_for_palace(_config.palace_path)
        backend.close_palace(palace_ref)
        if getattr(backend, "name", None):
            closed_backend_names.add(backend.name)
    except Exception as exc:
        logger.debug("Failed to close shared palace backend during reconnect", exc_info=True)
        close_errors.append(f"backend close_palace failed: {exc}")
    if cached_backend_name and cached_backend_name not in closed_backend_names:
        try:
            from ..backends import get_backend

            get_backend(cached_backend_name).close_palace(palace_ref)
            closed_backend_names.add(cached_backend_name)
        except Exception as exc:
            logger.debug(
                "Failed to close previously cached %s backend during reconnect",
                cached_backend_name,
                exc_info=True,
            )
            close_errors.append(f"cached {cached_backend_name} close_palace failed: {exc}")
    if _client_cache is not None:
        try:
            close = getattr(_client_cache, "close", None)
            if callable(close):
                close()
        except Exception as exc:
            logger.debug("Failed to close MCP-local Chroma client during reconnect", exc_info=True)
            close_errors.append(f"local Chroma client close failed: {exc}")
    if _is_chroma_backend():
        try:
            from chromadb.api.client import SharedSystemClient

            clear_system_cache = getattr(SharedSystemClient, "clear_system_cache", None)
            if callable(clear_system_cache):
                clear_system_cache()
            else:
                logger.debug(
                    "SharedSystemClient.clear_system_cache is unavailable; skipping shared Chroma cache clear during reconnect"
                )
        except Exception as exc:
            logger.debug(
                "Failed to clear Chroma shared system cache during reconnect",
                exc_info=True,
            )
            close_errors.append(f"shared Chroma cache clear failed: {exc}")
    _client_cache = None
    _collection_cache = None
    _collection_cache_backend = None
    _collection_cache_palace = None
    _collection_open_error = None
    _palace_db_inode = 0
    _palace_db_mtime = 0.0
    ChromaBackend._quarantined_paths.discard(_config.palace_path)
    # Force probe re-run on next _get_client by clearing the flag now;
    # _refresh_vector_disabled_flag will re-set it if the divergence
    # still applies after the reconnect. The probe keeps its own cache
    # (#1471), so drop that too — otherwise the "re-run" would be served
    # from the verdict this reconnect is meant to discard.
    reset_hnsw_capacity_cache()
    _vector_disabled = False
    _vector_disabled_reason = ""
    # Drain the per-path KnowledgeGraph cache so a replaced sqlite file is
    # reopened on the next tool call rather than served from a stale handle.
    with _kg_cache_lock:
        for kg in _kg_by_path.values():
            try:
                kg.close()
            except Exception:
                pass
        _kg_by_path.clear()
    with _logstream_cache_lock:
        for ls in _logstream_by_path.values():
            try:
                ls.close()
            except Exception:
                pass
        _logstream_by_path.clear()
    _refresh_sqlite_integrity_status()
    if _sqlite_integrity_errors:
        result = {
            "success": False,
            "message": "SQLite integrity check failed after reconnect",
            "sqlite_integrity": _sqlite_integrity_payload(),
            "vector_disabled": _vector_disabled,
            "vector_disabled_reason": _vector_disabled_reason,
            "hint": (
                "Stop all MemPalace MCP clients/writers, back up the palace, "
                "repair the SQLite/FTS5 corruption offline, then run "
                "mempalace_reconnect or restart the MCP server."
            ),
        }
        if close_errors:
            result["error"] = "; ".join(close_errors)
        return result

    try:
        col = _get_collection()
        if col is None:
            open_error = _collection_error_or_no_palace()
            result = {
                "success": False,
                "message": open_error.get("error", "No palace found after reconnect"),
                "drawers": 0,
                "vector_disabled": _vector_disabled,
            }
            if "details" in open_error:
                result["details"] = open_error["details"]
            if "hint" in open_error:
                result["hint"] = open_error["hint"]
            if close_errors:
                result["error"] = "; ".join(close_errors)
            return result
        if close_errors:
            return _attach_stale_library_warning(
                {
                    "success": False,
                    "message": "Reconnect reopened the palace but failed to fully reset cached handles",
                    "drawers": col.count(),
                    "vector_disabled": _vector_disabled,
                    "vector_disabled_reason": _vector_disabled_reason,
                    "error": "; ".join(close_errors),
                }
            )
        return _attach_stale_library_warning(
            {
                "success": True,
                "message": "Reconnected to palace",
                "drawers": col.count(),
                "vector_disabled": _vector_disabled,
                "vector_disabled_reason": _vector_disabled_reason,
            }
        )
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_checkpoint(items, diary=None, dedup_threshold=0.9, added_by=None):
    """Batch session save in a single call.

    Semantic-dedups each item, files the non-duplicates as drawers, then
    writes one diary entry. Collapses the per-item ``check_duplicate`` /
    ``add_drawer`` / ``diary_write`` sequence into one MCP request so the
    host UI renders a single tool-call card (and keeps its spinner up for
    the whole save) instead of one card per underlying call.

    ``items`` is a list of ``{"wing", "room", "content"}`` dicts. ``diary``
    is an optional ``{"agent_name", "entry", "topic"?, "wing"?}`` dict.
    ``added_by`` attributes the filed drawers; when omitted it falls back to
    the diary's ``agent_name`` (and then to ``"checkpoint"``), so the agent
    that filed the session is recorded instead of a generic label.
    Reuses the existing single-item handlers so dedup/idempotency/WAL
    behaviour is identical to calling them directly.
    """
    # Inputs come from MCP clients and handle_request does not validate
    # nested schemas, so guard every field here. A single malformed item
    # must record an error and be skipped, never raise and abort the whole
    # batch (the already-filed items in this call would otherwise be lost
    # from the response).
    try:
        dedup_threshold = float(dedup_threshold)
    except (ValueError, TypeError):
        return {"error": "dedup_threshold must be a number"}

    out = {"added": [], "duplicates": [], "errors": []}
    if not isinstance(items, list):
        return {"error": "items must be a list of {wing, room, content} objects"}
    # Drawer attribution: an explicit ``added_by`` wins; otherwise fall back to
    # the diary's ``agent_name`` (the agent filing this session); otherwise the
    # legacy ``"checkpoint"`` label. A blank, whitespace-only, or non-string
    # value counts as unspecified at each step, so an empty explicit argument
    # still defers to the diary instead of masking it. The chosen name is stored
    # verbatim (tool_add_drawer strips lone surrogates but does not case-fold),
    # matching how every other caller records ``added_by``; the diary index
    # lowercases the same name separately for case-insensitive reads.
    resolved_added_by = added_by if isinstance(added_by, str) and added_by.strip() else None
    if resolved_added_by is None and isinstance(diary, dict):
        agent = diary.get("agent_name")
        resolved_added_by = agent if isinstance(agent, str) and agent.strip() else None
    if resolved_added_by is None:
        resolved_added_by = "checkpoint"
    for item in items:
        if not isinstance(item, dict):
            out["errors"].append({"item": item, "error": "item must be an object"})
            continue
        wing = item.get("wing")
        room = item.get("room")
        content = item.get("content")
        # Non-empty strings only: a non-string here would raise deep in
        # sanitize_content / strip_lone_surrogates.
        if not all(isinstance(v, str) and v for v in (wing, room, content)):
            out["errors"].append(
                {"item": item, "error": "wing, room, content must be non-empty strings"}
            )
            continue
        dup = tool_check_duplicate(content, threshold=dedup_threshold)
        if dup.get("is_duplicate"):
            out["duplicates"].append({"room": room, "matches": dup.get("matches", [])})
            continue
        # On a dedup error (genuine index failure — content is guaranteed a
        # string by the guard above) we still file rather than drop the
        # memory: verbatim recall is the priority and add_drawer's own
        # idempotency blocks exact duplicates.
        res = tool_add_drawer(wing=wing, room=room, content=content, added_by=resolved_added_by)
        if res.get("success"):
            out["added"].append(res)
        else:
            out["errors"].append(res)
    if diary is not None:
        if not isinstance(diary, dict):
            out["errors"].append({"diary": diary, "error": "diary must be an object"})
        else:
            entry = diary.get("entry") or diary.get("content")
            if not isinstance(entry, str) or not entry:
                out["errors"].append(
                    {"diary": diary, "error": "diary entry must be a non-empty string"}
                )
            else:
                out["diary"] = tool_diary_write(
                    agent_name=diary.get("agent_name", "cursor-ide"),
                    entry=entry,
                    topic=diary.get("topic", "session-checkpoint"),
                    wing=diary.get("wing", ""),
                )
    return out
