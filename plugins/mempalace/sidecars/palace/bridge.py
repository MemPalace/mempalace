#!/usr/bin/env python3
"""Persistent JSON-lines stdio bridge for the MemPalace Antigravity Sidecar UI.

Engineered for massive palaces (400k+ drawers) and low memory footprint:
- Hub-first forwarding: when a live MemPalace hub (`mempalace serve`) is
  running, overview, search, drawer, KG, tunnel, diary and logstream requests
  proxy over HTTP without importing `mempalace.mcp_server` or holding a second
  embedder / vector matrix in RAM.
- Lazy local fallback: when no hub is running, `mempalace.mcp_server` is
  imported only on demand, FTS5 lexical search (~6ms) and overview/browse paths
  run without loading vectors, and vector/hybrid state unloads after an idle
  timeout (`MEMPALACE_SIDECAR_IDLE_UNLOAD_SECONDS`, default 300s).
- Opt-in warm-up: background vector pre-warming runs only when explicitly
  enabled via `MEMPALACE_SIDECAR_WARMUP=1` (or `MEMPALACE_EAGER_WARMUP=1`) and
  no hub is serving the palace.
- On-disk + in-memory overview/taxonomy cache keyed by palace DB fingerprint (<5ms cold start)
- Taxonomy-derived graph_stats in 0.1ms (eliminates duplicate 2.1s full-table SQLite scan)
- Indexed SQLite drawer pagination via col.get_recent / col.get
"""

from __future__ import annotations

import gc
import json
import os
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict
from typing import Any

# Allow an explicit repo root override when running from a source checkout.
_repo_override = os.environ.get("MEMPALACE_REPO_ROOT", "").strip()
if _repo_override and os.path.isdir(_repo_override) and _repo_override not in sys.path:
    sys.path.insert(0, _repo_override)

from mempalace._env import env_bool, env_float  # noqa: E402
from mempalace.config import MempalaceConfig  # noqa: E402
from mempalace.hub_client import (  # noqa: E402
    FORWARD_ERRORS,
    HubClient,
    search_fingerprint_matches,
    search_settings_overridden,
    tool_result,
)
from mempalace.version import __version__  # noqa: E402

# `mempalace.mcp_server` is imported lazily on demand (and exposed as `bridge.mp`
# via PEP 562 module `__getattr__`) so hub-proxied runs never load chromadb,
# onnxruntime, tokenizers, or a duplicate vector matrix, while callers/tests
# accessing `bridge.mp` receive the real module object.
_MP_MOD: Any = None

_OUT: Any = sys.__stdout__ if sys.__stdout__ is not None else sys.stdout
_STDIO_PROTECTED: bool = False

_BRIDGE_LOCK = threading.RLock()
_OUT_LOCK = threading.Lock()
_UNLOAD_TIMER_LOCK = threading.Lock()

_CONFIG_CACHE: MempalaceConfig | None = None
_MEM_OVERVIEW_CACHE: dict[str, Any] | None = None
_MEM_OVERVIEW_FP: str = ""
_VECTOR_WARM: bool = False
_VECTOR_WARMING: bool = False
_WARMUP_MS: int | None = None
_LAST_VECTOR_USE: float = 0.0
_UNLOAD_TIMER: threading.Timer | None = None
_FIRST_OVERVIEW_DONE = threading.Event()

# Threshold above which drawer pagination uses indexed col.get / col.get_recent
# instead of scanning and collapsing all rows in Python.
_LARGE_LOCUS_THRESHOLD = 1500
_DEFAULT_IDLE_UNLOAD_SECONDS = 300.0
_HUB_CALL_TIMEOUT_S = 30.0
_HUB_SEARCH_TIMEOUT_S = 120.0


class _HubFallback(Exception):
    """Raised internally when a hub call cannot complete and local fallback is safe."""


def _init_stdio() -> None:
    """Redirect fd 1 -> fd 2 and keep the real stdout fd on `_OUT`."""
    global _OUT, _STDIO_PROTECTED
    if _STDIO_PROTECTED:
        return
    try:
        real_fd = os.dup(1)
        os.dup2(2, 1)
        _OUT = os.fdopen(real_fd, "w", encoding="utf-8", errors="replace", closefd=False)
        sys.stdout = sys.stderr
        _STDIO_PROTECTED = True
    except (OSError, AttributeError):
        _OUT = sys.__stdout__ if sys.__stdout__ is not None else sys.stdout
    for stream in (sys.stdin, _OUT):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _import_mcp_server():
    """Import ``mempalace.mcp_server`` preserving stdout if stdio is not redirected."""
    saved_fd = None
    saved_stdout = sys.stdout
    if not _STDIO_PROTECTED:
        try:
            saved_fd = os.dup(1)
        except OSError:
            saved_fd = None
    try:
        import mempalace.mcp_server as _mp

        return _mp
    finally:
        if not _STDIO_PROTECTED:
            if saved_fd is not None:
                try:
                    os.dup2(saved_fd, 1)
                finally:
                    os.close(saved_fd)
            sys.stdout = saved_stdout


def _mp_loaded() -> bool:
    if "mp" in globals():
        return globals()["mp"] is not None
    return _MP_MOD is not None


def _get_mp():
    """Import `mempalace.mcp_server` on demand for local fallback."""
    global _MP_MOD
    if "mp" in globals() and globals()["mp"] is not None:
        return globals()["mp"]
    if _MP_MOD is None:
        _MP_MOD = _import_mcp_server()
        globals()["mp"] = _MP_MOD
    return _MP_MOD


def __getattr__(name: str):
    if name == "mp":
        return _get_mp()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _get_config() -> MempalaceConfig:
    global _CONFIG_CACHE
    if _mp_loaded():
        local_mp = _get_mp()
        if getattr(local_mp, "_config", None) is not None:
            return local_mp._config
    if _CONFIG_CACHE is None:
        _CONFIG_CACHE = MempalaceConfig()
    return _CONFIG_CACHE


def _palace_path() -> str:
    return str(getattr(_get_config(), "palace_path", "") or "")


def _collection_name() -> str:
    return str(getattr(_get_config(), "collection_name", "") or "")


def _config_dir() -> str:
    return str(getattr(_get_config(), "config_dir", "") or "")


def _configured_backend_name() -> str:
    if _mp_loaded():
        local_mp = _get_mp()
        if hasattr(local_mp, "_selected_backend_name"):
            try:
                return str(local_mp._selected_backend_name())
            except Exception:
                pass
    cfg = _get_config()
    return str(getattr(cfg, "backend", "") or "sqlite_exact")


def _emit(payload: dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with _OUT_LOCK:
        _OUT.write(line + "\n")
        _OUT.flush()


def _warmup_enabled() -> bool:
    return env_bool("MEMPALACE_SIDECAR_WARMUP", False) or env_bool("MEMPALACE_EAGER_WARMUP", False)


def _idle_unload_seconds() -> float:
    val = env_float("MEMPALACE_SIDECAR_IDLE_UNLOAD_SECONDS", _DEFAULT_IDLE_UNLOAD_SECONDS)
    return val if val >= 0 else _DEFAULT_IDLE_UNLOAD_SECONDS


def _discover_hub(palace_path: str | None = None) -> HubClient | None:
    target = palace_path if palace_path is not None else _palace_path()
    if not target:
        return None
    return HubClient.discover(target, include_self=True)


def _hub_call(
    hub: HubClient,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    timeout: float = _HUB_CALL_TIMEOUT_S,
) -> dict[str, Any]:
    """Call an MCP tool on the live hub, raising `_HubFallback` if unreachable."""
    try:
        payload = hub.call_tool(tool_name, arguments or {}, timeout=timeout)
    except FORWARD_ERRORS as exc:
        raise _HubFallback(str(exc)) from exc
    if not isinstance(payload, dict):
        raise _HubFallback(f"hub returned non-dict response for {tool_name}")
    err = payload.get("error")
    if isinstance(err, dict):
        return {"error": err.get("message") or f"hub error in {tool_name}"}
    try:
        result = tool_result(payload)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise _HubFallback(f"unrecognized hub response for {tool_name}: {exc}") from exc
    if not isinstance(result, dict):
        raise _HubFallback(f"non-mapping hub result for {tool_name}")
    return result


def _cancel_idle_unload_timer() -> None:
    global _UNLOAD_TIMER
    with _UNLOAD_TIMER_LOCK:
        if _UNLOAD_TIMER is not None:
            _UNLOAD_TIMER.cancel()
            _UNLOAD_TIMER = None


def _schedule_idle_unload() -> None:
    global _UNLOAD_TIMER
    timeout = _idle_unload_seconds()
    if timeout <= 0:
        return
    with _UNLOAD_TIMER_LOCK:
        if _UNLOAD_TIMER is not None:
            _UNLOAD_TIMER.cancel()
        timer = threading.Timer(timeout, _idle_unload_callback)
        timer.daemon = True
        timer.name = "mempalace-sidecar-idle-unload"
        timer.start()
        _UNLOAD_TIMER = timer


def _idle_unload_callback() -> None:
    try:
        _unload_vector_stack(force=False)
    except Exception:
        pass


def _unload_vector_stack(*, force: bool = False) -> bool:
    """Release cached vector matrices, backend handles, and embedder models."""
    global _VECTOR_WARM, _WARMUP_MS
    timeout = _idle_unload_seconds()
    with _BRIDGE_LOCK:
        if not force:
            if not _VECTOR_WARM:
                return False
            if timeout <= 0:
                return False
            if (time.monotonic() - _LAST_VECTOR_USE) < timeout:
                return False

        _cancel_idle_unload_timer()

        if _mp_loaded():
            local_mp = _get_mp()
            if hasattr(local_mp, "tool_reconnect"):
                try:
                    local_mp.tool_reconnect()
                except Exception:
                    pass

        emb_mod = sys.modules.get("mempalace.embedding")
        if emb_mod is not None and hasattr(emb_mod, "clear_embedding_cache"):
            try:
                emb_mod.clear_embedding_cache()
            except Exception:
                pass

        gc.collect()
        _VECTOR_WARM = False
        _WARMUP_MS = None
        return True


def _palace_fingerprint() -> str:
    p = _palace_path()
    if not p:
        return "none"
    parts = [p, _collection_name()]
    for fname in (
        "sqlite_exact.sqlite3",
        "sqlite_exact.sqlite3-wal",
        "chroma.sqlite3",
        "chroma.sqlite3-wal",
    ):
        fpath = os.path.join(p, fname)
        try:
            st = os.stat(fpath)
            parts.append(f"{fname}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            pass
    return "|".join(parts)


def _disk_cache_path() -> str:
    state_dir = os.path.join(os.path.expanduser("~"), ".mempalace", "hook_state")
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "agy_sidecar_overview_cache.json")


def _read_cached_structure(fp: str) -> dict[str, Any] | None:
    global _MEM_OVERVIEW_CACHE, _MEM_OVERVIEW_FP
    if _MEM_OVERVIEW_CACHE is not None and _MEM_OVERVIEW_FP == fp:
        return _MEM_OVERVIEW_CACHE
    cache_file = _disk_cache_path()
    if os.path.isfile(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                disk_data = json.load(f)
            if isinstance(disk_data, dict) and disk_data.get("fingerprint") == fp:
                _MEM_OVERVIEW_CACHE = disk_data["payload"]
                _MEM_OVERVIEW_FP = fp
                return _MEM_OVERVIEW_CACHE
        except Exception:
            pass
    return None


def _write_cached_structure(payload: dict[str, Any]) -> None:
    global _MEM_OVERVIEW_CACHE, _MEM_OVERVIEW_FP
    fp = _palace_fingerprint()
    _MEM_OVERVIEW_CACHE = payload
    _MEM_OVERVIEW_FP = fp
    cache_file = _disk_cache_path()
    try:
        tmp_file = f"{cache_file}.tmp.{os.getpid()}"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump({"fingerprint": fp, "payload": payload}, f, default=str)
        os.replace(tmp_file, cache_file)
    except Exception:
        pass


def _structure_has_error(status: Any, taxonomy: Any) -> bool:
    return (isinstance(status, dict) and "error" in status) or (
        isinstance(taxonomy, dict) and "error" in taxonomy
    )


def _derive_graph_stats_from_taxonomy(
    taxonomy_map: dict[str, dict[str, int]], *, explicit_count: int | None = None
) -> dict[str, Any]:
    """Compute graph_stats in <0.2ms directly from the wing->room->count taxonomy."""
    room_wings: dict[str, set[str]] = defaultdict(set)
    room_counts: Counter[str] = Counter()
    rooms_per_wing: Counter[str] = Counter()
    total_instances = 0

    for wing, rooms in (taxonomy_map or {}).items():
        if not isinstance(rooms, dict):
            continue
        for room, cnt in rooms.items():
            n = int(cnt or 0)
            if n <= 0:
                continue
            total_instances += 1
            room_wings[room].add(wing)
            room_counts[room] += n
            rooms_per_wing[wing] += 1

    tunnel_rooms = 0
    total_edges = 0
    multi_wing_rooms = []
    for room, wings_set in room_wings.items():
        nw = len(wings_set)
        if nw >= 2:
            tunnel_rooms += 1
            total_edges += nw * (nw - 1) // 2
            multi_wing_rooms.append(
                {
                    "room": room,
                    "wings": sorted(wings_set),
                    "count": room_counts[room],
                }
            )

    multi_wing_rooms.sort(key=lambda item: (-len(item["wings"]), -item["count"], item["room"]))
    top_tunnels = multi_wing_rooms[:12]

    if explicit_count is None:
        if _mp_loaded():
            local_mp = _get_mp()
            try:
                explicit_count = len(local_mp._load_graph_tunnels(local_mp._config))
            except Exception:
                explicit_count = 0
        else:
            explicit_count = 0

    return {
        "total_rooms": len(room_wings),
        "total_room_instances": total_instances,
        "tunnel_rooms": tunnel_rooms,
        "passive_tunnel_rooms": tunnel_rooms,
        "explicit_tunnels": explicit_count,
        "total_edges": total_edges,
        "total_connections": total_edges + explicit_count,
        "rooms_per_wing": dict(rooms_per_wing.most_common()),
        "top_tunnels": top_tunnels,
    }


def _get_palace_structure(force_refresh: bool = False) -> dict[str, Any]:
    """Return {status, taxonomy, graph_stats} using RAM or disk cache when fingerprint matches."""
    fp = _palace_fingerprint()
    if not force_refresh:
        cached = _read_cached_structure(fp)
        if cached is not None:
            return cached

    local_mp = _get_mp()
    status = local_mp.tool_status()
    taxonomy = local_mp.tool_get_taxonomy()
    tax_map = (taxonomy.get("taxonomy") if isinstance(taxonomy, dict) else None) or {}
    graph_stats = _derive_graph_stats_from_taxonomy(tax_map)

    payload = {
        "status": status,
        "taxonomy": taxonomy,
        "graph_stats": graph_stats,
    }
    if not _structure_has_error(status, taxonomy):
        _write_cached_structure(payload)
    return payload


def _get_palace_structure_via_hub(hub: HubClient, force_refresh: bool = False) -> dict[str, Any]:
    """Return {status, taxonomy, graph_stats} via cache or the live hub."""
    fp = _palace_fingerprint()
    if not force_refresh:
        cached = _read_cached_structure(fp)
        if cached is not None:
            return cached

    status = _hub_call(hub, "mempalace_status")
    taxonomy = _hub_call(hub, "mempalace_get_taxonomy")
    explicit_count = 0
    try:
        tunnels_res = _hub_call(hub, "mempalace_list_tunnels")
        if isinstance(tunnels_res, dict) and isinstance(tunnels_res.get("tunnels"), list):
            explicit_count = len(tunnels_res["tunnels"])
    except _HubFallback:
        explicit_count = 0
    tax_map = (taxonomy.get("taxonomy") if isinstance(taxonomy, dict) else None) or {}
    graph_stats = _derive_graph_stats_from_taxonomy(tax_map, explicit_count=explicit_count)

    payload = {
        "status": status,
        "taxonomy": taxonomy,
        "graph_stats": graph_stats,
    }
    if not _structure_has_error(status, taxonomy):
        _write_cached_structure(payload)
    return payload


def _background_warmup() -> None:
    """Opt-in background vector warm-up when no hub is running."""
    global _VECTOR_WARM, _VECTOR_WARMING, _WARMUP_MS, _LAST_VECTOR_USE
    if not _warmup_enabled() or _discover_hub() is not None:
        return
    _VECTOR_WARMING = True
    try:
        with _BRIDGE_LOCK:
            _get_palace_structure(force_refresh=False)
        # Wait for the initial UI overview request (or up to 2.0s) so vector warmup
        # never holds _BRIDGE_LOCK while the Aux Pane is rendering its first paint.
        _FIRST_OVERVIEW_DONE.wait(timeout=2.0)
        time.sleep(0.15)
        if _discover_hub() is not None:
            return
        local_mp = _get_mp()
        t0 = time.perf_counter()
        with _BRIDGE_LOCK:
            local_mp.tool_search(query="mempalace", limit=1, candidate_strategy="vector")
            _LAST_VECTOR_USE = time.monotonic()
            _VECTOR_WARM = True
        _WARMUP_MS = int((time.perf_counter() - t0) * 1000)
        _schedule_idle_unload()
    except Exception:
        pass
    finally:
        _VECTOR_WARMING = False


def _infer_workspace_wing(workspace_path: str, tax_map: dict[str, Any]) -> str:
    if not workspace_path or not isinstance(tax_map, dict):
        return ""
    norm = workspace_path.replace("\\", "/").rstrip("/")
    base = norm.rsplit("/", 1)[-1].strip().lower()
    if not base:
        return ""
    for w in tax_map:
        if str(w).lower() == base:
            return str(w)
    return ""


def _build_overview_response(
    structure: dict[str, Any],
    kg_stats: Any,
    events_res: Any,
    *,
    workspace_path: str,
    backend_name: str,
    hub_connected: bool,
    hub_pid: Any = None,
) -> dict[str, Any]:
    tax_map = (structure.get("taxonomy") or {}).get("taxonomy") or {}
    workspace_wing = _infer_workspace_wing(workspace_path, tax_map)
    events_list = (events_res.get("events") or []) if isinstance(events_res, dict) else []
    events_tip = events_res.get("tip") if isinstance(events_res, dict) else None
    status_obj = dict(structure.get("status") or {})
    status_obj.setdefault("backend", backend_name)
    out = {
        "version": __version__,
        "backend": backend_name,
        "hub_connected": hub_connected,
        "vector_warm": True if hub_connected else _VECTOR_WARM,
        "vector_warming": False if hub_connected else _VECTOR_WARMING,
        "warmup_ms": None if hub_connected else _WARMUP_MS,
        "workspace_wing": workspace_wing,
        "palace_path": _palace_path(),
        "config_dir": _config_dir(),
        "status": status_obj,
        "taxonomy": structure["taxonomy"],
        "graph_stats": structure["graph_stats"],
        "kg_stats": kg_stats,
        "recent_events": events_list,
        "events_tip": events_tip,
    }
    if hub_pid is not None:
        out["hub_pid"] = hub_pid
    return out


def _handle_overview(params: dict[str, Any]) -> dict[str, Any]:
    reconnect = bool(params.get("reconnect"))
    force_refresh = reconnect or bool(params.get("force_refresh") or params.get("forceRefresh"))
    workspace_path = str(params.get("workspace_path") or params.get("workspacePath") or "").strip()

    hub = _discover_hub()
    if hub is not None:
        try:
            if reconnect:
                _hub_call(hub, "mempalace_reconnect")
            structure = _get_palace_structure_via_hub(hub, force_refresh=force_refresh)
            kg_stats = _hub_call(hub, "mempalace_kg_stats")
            events_res = _hub_call(hub, "mempalace_event_list", {"limit": 15, "preview": True})
            status_obj = structure.get("status") or {}
            backend_name = str(status_obj.get("backend") or _configured_backend_name())
            _FIRST_OVERVIEW_DONE.set()
            return _build_overview_response(
                structure,
                kg_stats,
                events_res,
                workspace_path=workspace_path,
                backend_name=backend_name,
                hub_connected=True,
                hub_pid=hub.pid,
            )
        except _HubFallback:
            pass

    local_mp = _get_mp()
    with _BRIDGE_LOCK:
        if reconnect:
            local_mp.tool_reconnect()
        structure = _get_palace_structure(force_refresh=force_refresh)
        kg_stats = local_mp.tool_kg_stats()
        events_res = local_mp.tool_event_list(limit=15, preview=True)
        backend_name = local_mp._selected_backend_name()
    _FIRST_OVERVIEW_DONE.set()
    return _build_overview_response(
        structure,
        kg_stats,
        events_res,
        workspace_path=workspace_path,
        backend_name=backend_name,
        hub_connected=False,
    )


def _search_via_hub(
    hub: HubClient,
    *,
    query: str,
    limit: int,
    wing: str | None,
    room: str | None,
    strategy: str,
) -> dict[str, Any] | None:
    palace = _palace_path()
    if strategy in ("vector", "union"):
        if search_settings_overridden() or not search_fingerprint_matches(hub.info, palace):
            return None
        hub_strategy = strategy
    else:
        # Older hubs without search_lexical run union (vectors are already warm on the hub)
        # so the sidecar never opens a second local process or embedder next to the hub.
        hub_strategy = "lexical" if "search_lexical" in hub.capabilities else "union"

    args: dict[str, Any] = {
        "query": query,
        "limit": limit,
        "candidate_strategy": hub_strategy,
    }
    if wing:
        args["wing"] = wing
    if room:
        args["room"] = room
    res = _hub_call(hub, "mempalace_search", args, timeout=_HUB_SEARCH_TIMEOUT_S)
    res.setdefault("strategy", hub_strategy)
    res["hub_connected"] = True
    res["vector_warm"] = True
    return res


def _handle_search(params: dict[str, Any]) -> dict[str, Any]:
    global _VECTOR_WARM, _LAST_VECTOR_USE
    query = str(params.get("query") or "").strip()
    if not query:
        return {"error": "Query is required", "results": []}
    limit = max(1, min(int(params.get("limit") or 20), 50))
    wing = str(params.get("wing") or "").strip() or None
    room = str(params.get("room") or "").strip() or None
    strategy = str(params.get("candidate_strategy") or params.get("strategy") or "union").strip()
    if strategy not in ("lexical", "vector", "union"):
        strategy = "union"

    hub = _discover_hub()
    if hub is not None:
        try:
            hub_res = _search_via_hub(
                hub, query=query, limit=limit, wing=wing, room=room, strategy=strategy
            )
            if hub_res is not None:
                return hub_res
        except _HubFallback:
            pass

    local_mp = _get_mp()

    # Fast FTS5 lexical search (~6ms on 413k drawers, zero embedding overhead)
    if strategy == "lexical":
        with _BRIDGE_LOCK:
            col = local_mp._get_collection()
            if not col:
                return local_mp._collection_error_or_no_palace()
            known = local_mp._palace_known_wings() if wing else frozenset()
            where = local_mp.build_where_filter(wing, room, known=known) or None
            lex = col.lexical_search(query=query, n_results=limit, where=where)
            results = []
            for hit in getattr(lex, "hits", []) or []:
                meta = local_mp._safe_meta(getattr(hit, "metadata", None))
                if local_mp.is_bookkeeping(meta, getattr(hit, "id", "")):
                    continue
                results.append(
                    {
                        "drawer_id": hit.id,
                        "text": hit.document or "",
                        "wing": meta.get("wing", "unknown"),
                        "room": meta.get("room", "general"),
                        "hall": meta.get("hall", ""),
                        "source_file": meta.get("source_file", ""),
                        "filed_at": meta.get("filed_at", ""),
                        "bm25_score": round(float(getattr(hit, "score", 0.0) or 0.0), 2),
                        "similarity": None,
                        "match_type": "fts5",
                    }
                )
            return {
                "query": query,
                "results": results,
                "strategy": "lexical",
                "hub_connected": False,
                "vector_warm": _VECTOR_WARM,
            }

    with _BRIDGE_LOCK:
        res = local_mp.tool_search(
            query=query,
            limit=limit,
            wing=wing,
            room=room,
            candidate_strategy=strategy,
        )
        _VECTOR_WARM = True
        _LAST_VECTOR_USE = time.monotonic()
    _schedule_idle_unload()
    if isinstance(res, dict):
        res.setdefault("hub_connected", False)
        res.setdefault("vector_warm", _VECTOR_WARM)
    return res


def _locus_total_from_cache(wing: str | None, room: str | None) -> int:
    fp = _palace_fingerprint()
    cached = _read_cached_structure(fp)
    if cached is not None:
        structure = cached
    else:
        hub = _discover_hub()
        if hub is not None:
            try:
                structure = _get_palace_structure_via_hub(hub, force_refresh=False)
            except _HubFallback:
                structure = _get_palace_structure(force_refresh=False)
        else:
            structure = _get_palace_structure(force_refresh=False)
    tax = (structure.get("taxonomy") or {}).get("taxonomy") or {}
    if wing and room:
        return int((tax.get(wing) or {}).get(room) or 0)
    if wing:
        return sum(int(v or 0) for v in (tax.get(wing) or {}).values())
    if room:
        return sum(int((rooms or {}).get(room) or 0) for rooms in tax.values())
    return int((structure.get("status") or {}).get("total_drawers") or 0)


def _recent_drawer_page(col, *, where, limit: int, offset: int) -> Any:
    """Slice one newest-first ordering, then hydrate only the selected page."""
    from mempalace.backends.base import GetResult

    capabilities = getattr(getattr(col, "_backend", None), "capabilities", ())
    exact_recency = "supports_recency_order" in (capabilities or ())
    if exact_recency and offset == 0:
        return col.get_recent(limit=limit, where=where)

    # get_recent has no offset argument. Select the ordered ID prefix, not
    # a storage-order page. The base implementation sorts only its scanned
    # window, so backends without exact recency must scan all metadata.
    scan_limit = offset + limit if exact_recency else col.count()
    ordered = col.get_recent(limit=scan_limit, where=where, include=[])
    page_ids = ordered.ids[offset : offset + limit]
    if not page_ids:
        return GetResult.empty()

    page = col.get(ids=page_ids, where=where, include=["documents", "metadatas"])
    positions = {doc_id: i for i, doc_id in enumerate(page.ids)}
    # get(ids=...) can return storage order. Reapply the selected order and
    # tolerate a record disappearing between selection and hydration.
    order = [positions[doc_id] for doc_id in page_ids if doc_id in positions]
    return GetResult(
        ids=[page.ids[i] for i in order],
        documents=[page.documents[i] for i in order],
        metadatas=[page.metadatas[i] for i in order],
    )


def _normalize_hub_drawer(item: dict[str, Any], *, fast_path: bool = False) -> dict[str, Any]:
    """Normalize `mempalace_list_drawers` payload to the sidecar card shape."""
    meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    has_full_content = item.get("content") is not None or item.get("text") is not None
    content = item.get("content")
    if content is None:
        content = item.get("text")
    if content is None:
        content = item.get("content_preview") or ""
    text_val = str(content)
    doc_id = item.get("id") or item.get("drawer_id") or ""
    chunk_count = int(item.get("chunk_count") or item.get("chunks") or meta.get("chunks") or 1)
    is_truncated = item.get("is_truncated")
    if is_truncated is None:
        if has_full_content:
            is_truncated = fast_path and (chunk_count > 1 or len(text_val) >= 790)
        else:
            is_truncated = chunk_count > 1 or (
                bool(item.get("content_preview"))
                and (text_val.endswith("...") or len(text_val) >= 195)
            )
    return {
        "id": doc_id,
        "drawer_id": doc_id,
        "wing": item.get("wing") or meta.get("wing") or "unknown",
        "room": item.get("room") or meta.get("room") or "general",
        "hall": item.get("hall") or meta.get("hall") or "",
        "source_file": item.get("source_file") or meta.get("source_file") or "",
        "filed_at": item.get("filed_at") or meta.get("filed_at") or "",
        "chunk_count": chunk_count,
        "total_chars": int(item.get("total_chars") or len(text_val)),
        "is_truncated": bool(is_truncated),
        "content": text_val,
    }


def _hydrate_missing_hub_contents(hub: HubClient, raw_drawers: list[Any]) -> None:
    """Batch-fetch full verbatim text from older hubs lacking `include_content`."""
    missing_ids = [
        str(d.get("id") or d.get("drawer_id") or "")
        for d in raw_drawers
        if isinstance(d, dict) and d.get("content") is None and d.get("text") is None
    ]
    missing_ids = [did for did in missing_ids if did]
    if not missing_ids:
        return
    try:
        bulk = _hub_call(hub, "mempalace_get_drawers", {"drawer_ids": missing_ids})
    except _HubFallback:
        return
    full_by_id: dict[str, dict[str, Any]] = {}
    for entry in (bulk.get("results") or []) if isinstance(bulk, dict) else []:
        if isinstance(entry, dict) and entry.get("drawer_id") and entry.get("content") is not None:
            full_by_id[str(entry["drawer_id"])] = entry
    if not full_by_id:
        return
    for d in raw_drawers:
        if not isinstance(d, dict):
            continue
        did = str(d.get("id") or d.get("drawer_id") or "")
        hit = full_by_id.get(did)
        if hit is not None:
            d["content"] = hit["content"]
            if hit.get("total_chars") is not None:
                d["total_chars"] = hit["total_chars"]


def _handle_drawers(params: dict[str, Any]) -> dict[str, Any]:
    wing = str(params.get("wing") or "").strip() or None
    room = str(params.get("room") or "").strip() or None
    since = str(params.get("since") or "").strip() or None
    before = str(params.get("before") or "").strip() or None
    sort_order = str(params.get("sort") or "recent").strip()
    sort_norm = "storage" if sort_order.lower() == "storage" else "recent"
    limit = max(1, min(int(params.get("limit") or 30), 200))
    offset = max(0, int(params.get("offset") or 0))

    hub = _discover_hub()
    if hub is not None:
        try:
            hub_limit = min(limit, 100)
            args: dict[str, Any] = {"limit": hub_limit, "offset": offset}
            if wing:
                args["wing"] = wing
            if room:
                args["room"] = room
            if since:
                args["since"] = since
            if before:
                args["before"] = before
            if "list_drawers_recent" in hub.capabilities:
                args["sort"] = sort_norm
                args["include_content"] = True
            res = _hub_call(hub, "mempalace_list_drawers", args)
            if "error" in res and "drawers" not in res:
                return res
            raw_drawers = [dict(d) for d in (res.get("drawers") or []) if isinstance(d, dict)]
            if "list_drawers_recent" not in hub.capabilities:
                _hydrate_missing_hub_contents(hub, raw_drawers)
            is_fast = bool(res.get("fast_path"))
            drawers = [_normalize_hub_drawer(d, fast_path=is_fast) for d in raw_drawers]
            total = int(res.get("total") if res.get("total") is not None else len(drawers))
            out: dict[str, Any] = {
                "drawers": drawers,
                "total": total,
                "count": len(drawers),
                "offset": offset,
                "limit": hub_limit,
                "has_more": (offset + len(drawers)) < total,
                "hub_connected": True,
            }
            if is_fast:
                out["fast_path"] = True
            return out
        except _HubFallback:
            pass

    local_mp = _get_mp()
    with _BRIDGE_LOCK:
        est_total = _locus_total_from_cache(wing, room)

        # For large loci (>1,500 drawers) without custom date window filters,
        # use indexed collection reads where the backend supports them.
        if not since and not before and est_total > _LARGE_LOCUS_THRESHOLD:
            col = local_mp._get_collection()
            if col is not None:
                known = local_mp._palace_known_wings() if wing else frozenset()
                where = local_mp.build_where_filter(wing, room, known=known) or None
                try:
                    if sort_order == "recent" and hasattr(col, "get_recent"):
                        raw = _recent_drawer_page(col, where=where, limit=limit, offset=offset)
                    else:
                        raw = col.get(
                            where=where,
                            limit=limit,
                            offset=offset,
                            include=["documents", "metadatas"],
                        )
                    ids = raw.get("ids") or []
                    docs = raw.get("documents") or []
                    metas = raw.get("metadatas") or []
                    drawers = []
                    for i, doc_id in enumerate(ids):
                        meta = local_mp._safe_meta(metas[i] if i < len(metas) else None)
                        if local_mp.is_bookkeeping(meta, doc_id):
                            continue
                        doc = docs[i] if i < len(docs) else ""
                        text_val = doc or ""
                        chunk_count = int(meta.get("chunk_count") or meta.get("total_chunks") or 1)
                        drawers.append(
                            {
                                "id": doc_id,
                                "wing": meta.get("wing", "unknown"),
                                "room": meta.get("room", "general"),
                                "hall": meta.get("hall", ""),
                                "source_file": meta.get("source_file", ""),
                                "filed_at": meta.get("filed_at", ""),
                                "chunk_count": chunk_count,
                                "total_chars": len(text_val),
                                "is_truncated": chunk_count > 1 or len(text_val) >= 790,
                                "content": text_val,
                            }
                        )
                    return {
                        "drawers": drawers,
                        "total": est_total,
                        "count": len(drawers),
                        "offset": offset,
                        "limit": limit,
                        "has_more": (offset + limit) < est_total,
                        "fast_path": True,
                    }
                except Exception:
                    pass

        return local_mp.tool_list_drawers(
            wing=wing,
            room=room,
            since=since,
            before=before,
            limit=limit,
            offset=offset,
        )


def _handle_drawer(params: dict[str, Any]) -> dict[str, Any]:
    drawer_id = str(params.get("drawer_id") or "").strip()
    if not drawer_id:
        return {"error": "drawer_id is required"}

    hub = _discover_hub()
    if hub is not None:
        try:
            return _hub_call(hub, "mempalace_get_drawer", {"drawer_id": drawer_id})
        except _HubFallback:
            pass

    local_mp = _get_mp()
    with _BRIDGE_LOCK:
        return local_mp.tool_get_drawer(drawer_id=drawer_id)


def _handle_tunnels(params: dict[str, Any]) -> dict[str, Any]:
    start_room = str(params.get("start_room") or "").strip()
    wing_a = str(params.get("wing_a") or "").strip()
    wing_b = str(params.get("wing_b") or "").strip()
    wing = str(params.get("wing") or "").strip() or None
    limit = max(1, min(int(params.get("limit") or 60), 200))

    hub = _discover_hub()
    if hub is not None:
        try:
            if start_room:
                max_hops = max(1, min(int(params.get("max_hops") or 2), 5))
                return _hub_call(
                    hub, "mempalace_traverse", {"start_room": start_room, "max_hops": max_hops}
                )
            if wing_a and wing_b:
                res = _hub_call(hub, "mempalace_find_tunnels", {"wing_a": wing_a, "wing_b": wing_b})
            else:
                res = _hub_call(hub, "mempalace_list_tunnels", {"wing": wing} if wing else {})
            if isinstance(res, dict) and isinstance(res.get("tunnels"), list):
                total = len(res["tunnels"])
                res = {**res, "total": total, "tunnels": res["tunnels"][:limit]}
            return res
        except _HubFallback:
            pass

    local_mp = _get_mp()
    with _BRIDGE_LOCK:
        if start_room:
            max_hops = max(1, min(int(params.get("max_hops") or 2), 5))
            return local_mp.tool_traverse_graph(start_room=start_room, max_hops=max_hops)
        if wing_a and wing_b:
            res = local_mp.tool_find_tunnels(wing_a=wing_a, wing_b=wing_b)
        else:
            res = local_mp.tool_list_tunnels(wing=wing)
    if isinstance(res, dict) and isinstance(res.get("tunnels"), list):
        total = len(res["tunnels"])
        res = {**res, "total": total, "tunnels": res["tunnels"][:limit]}
    return res


def _extract_kg_entities_sample(timeline: Any) -> list[str]:
    entities_seen: list[str] = []
    seen_set: set[str] = set()
    for item in (timeline.get("timeline") or []) if isinstance(timeline, dict) else []:
        for key in ("subject", "object"):
            val = str(item.get(key) or "").strip()
            if val and val not in seen_set:
                seen_set.add(val)
                entities_seen.append(val)
                if len(entities_seen) >= 24:
                    return entities_seen
    return entities_seen


def _handle_kg(params: dict[str, Any]) -> dict[str, Any]:
    entity = str(params.get("entity") or "").strip()
    as_of = str(params.get("as_of") or "").strip() or None
    direction = str(params.get("direction") or "both").strip()
    if direction not in ("outgoing", "incoming", "both"):
        direction = "both"
    limit = max(1, min(int(params.get("limit") or 80), 200))
    offset = max(0, int(params.get("offset") or 0))

    hub = _discover_hub()
    if hub is not None:
        try:
            stats = _hub_call(hub, "mempalace_kg_stats")
            tl_args: dict[str, Any] = {"limit": limit, "offset": offset}
            if entity:
                tl_args["entity"] = entity
            timeline = _hub_call(hub, "mempalace_kg_timeline", tl_args)
            query_result = None
            if entity:
                q_args: dict[str, Any] = {"entity": entity, "direction": direction}
                if as_of:
                    q_args["as_of"] = as_of
                query_result = _hub_call(hub, "mempalace_kg_query", q_args)
            return {
                "stats": stats,
                "timeline": timeline,
                "query_result": query_result,
                "entities_sample": _extract_kg_entities_sample(timeline),
                "hub_connected": True,
            }
        except _HubFallback:
            pass

    local_mp = _get_mp()
    with _BRIDGE_LOCK:
        stats = local_mp.tool_kg_stats()
        timeline = local_mp.tool_kg_timeline(entity=entity or None, limit=limit, offset=offset)
        query_result = None
        if entity:
            query_result = local_mp.tool_kg_query(entity=entity, as_of=as_of, direction=direction)

    return {
        "stats": stats,
        "timeline": timeline,
        "query_result": query_result,
        "entities_sample": _extract_kg_entities_sample(timeline),
    }


def _handle_events(params: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(params.get("limit") or 50), 200))
    preview = bool(params.get("preview", False))
    kwargs: dict[str, Any] = {
        "limit": limit,
        "preview": preview,
    }
    for key in (
        "stream",
        "room",
        "topic",
        "type",
        "to_agent",
        "writer",
        "correlation_id",
        "status",
        "since_event_id",
    ):
        val = str(params.get(key) or "").strip()
        if val:
            kwargs[key] = val

    hub = _discover_hub()
    if hub is not None:
        try:
            return _hub_call(hub, "mempalace_event_list", kwargs)
        except _HubFallback:
            pass

    local_mp = _get_mp()
    with _BRIDGE_LOCK:
        return local_mp.tool_event_list(**kwargs)


def _handle_artifact(params: dict[str, Any]) -> dict[str, Any]:
    artifact_id = str(params.get("artifact_id") or params.get("sha256") or "").strip()
    if not artifact_id:
        return {"error": "sha256 is required"}

    hub = _discover_hub()
    if hub is not None:
        try:
            return _hub_call(hub, "mempalace_artifact_get", {"artifact_id": artifact_id})
        except _HubFallback:
            pass

    local_mp = _get_mp()
    with _BRIDGE_LOCK:
        return local_mp.tool_artifact_get(artifact_id=artifact_id)


def _handle_diary(params: dict[str, Any]) -> dict[str, Any]:
    agent_name = str(params.get("agent_name") or "").strip()
    wing = str(params.get("wing") or "").strip()
    last_n = max(1, min(int(params.get("last_n") or 25), 100))

    diary_drawers = _handle_drawers(
        {"room": "diary", "wing": wing, "limit": last_n, "offset": 0, "sort": "recent"}
    )
    drawers_list = (diary_drawers.get("drawers") or []) if isinstance(diary_drawers, dict) else []

    if agent_name:
        read_res = None
        hub = _discover_hub()
        if hub is not None:
            try:
                args: dict[str, Any] = {"agent_name": agent_name, "last_n": last_n}
                if wing:
                    args["wing"] = wing
                read_res = _hub_call(hub, "mempalace_diary_read", args)
            except _HubFallback:
                read_res = None
        if read_res is None:
            local_mp = _get_mp()
            with _BRIDGE_LOCK:
                read_res = local_mp.tool_diary_read(agent_name=agent_name, last_n=last_n, wing=wing)
        return {
            "mode": "agent",
            "agent": agent_name,
            "result": read_res,
            "recent_diary_drawers": drawers_list,
        }

    entries = []
    for d in drawers_list:
        entries.append(
            {
                "drawer_id": d.get("id", ""),
                "wing": d.get("wing", ""),
                "date": (d.get("filed_at") or "")[:10],
                "timestamp": d.get("filed_at") or "",
                "topic": d.get("hall") or "diary",
                "content": d.get("content") or "",
            }
        )
    return {
        "mode": "all",
        "agent": "*",
        "result": {
            "agent": "*",
            "entries": entries,
            "total": diary_drawers.get("total", len(entries))
            if isinstance(diary_drawers, dict)
            else len(entries),
            "showing": len(entries),
        },
    }


def _handle_ping(_params: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "vector_warm": _VECTOR_WARM,
        "hub_connected": _discover_hub() is not None,
    }


def _handle_unload(_params: dict[str, Any]) -> dict[str, Any]:
    unloaded = _unload_vector_stack(force=True)
    return {"ok": True, "unloaded": unloaded, "vector_warm": _VECTOR_WARM}


_HANDLERS = {
    "ping": _handle_ping,
    "overview": _handle_overview,
    "search": _handle_search,
    "drawers": _handle_drawers,
    "drawer": _handle_drawer,
    "tunnels": _handle_tunnels,
    "kg": _handle_kg,
    "events": _handle_events,
    "artifact": _handle_artifact,
    "diary": _handle_diary,
    "unload": _handle_unload,
}


def main() -> None:
    _init_stdio()
    _emit({"type": "ready", "version": __version__})

    # Opt-in warm-up only: off by default and skipped when a hub is reachable.
    if _warmup_enabled() and _discover_hub() is None:
        warmup_thread = threading.Thread(
            target=_background_warmup, name="mempalace-warmup", daemon=True
        )
        warmup_thread.start()

    for raw_line in sys.stdin:
        line = raw_line.lstrip("\ufeff").strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            action = str(req.get("action") or "").strip()
            params = req.get("params") or {}
            if not isinstance(params, dict):
                params = {}
            handler = _HANDLERS.get(action)
            if handler is None:
                _emit({"id": req_id, "ok": False, "error": f"Unknown action: {action!r}"})
                continue
            t0 = time.perf_counter()
            result = handler(params)
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            if isinstance(result, dict):
                result["_bridge_ms"] = elapsed_ms
            _emit({"id": req_id, "ok": True, "data": result, "elapsed_ms": elapsed_ms})
        except Exception as exc:
            _emit(
                {
                    "id": req_id,
                    "ok": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )


if __name__ == "__main__":
    main()
