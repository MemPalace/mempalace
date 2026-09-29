# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


_HUB_FORWARD_ENV = "MEMPALACE_HUB_FORWARD"
_HUB_HEALTH_TIMEOUT_S = 0.75
# A backfill mine over a large transcript tree can legitimately run for many
# minutes inside the hub; the forwarder is a background/CLI process, not a
# hook-budgeted one, so it waits.
_HUB_MINE_TIMEOUT_S = 3600.0
_HUB_SEARCH_TIMEOUT_S = 600.0
_HUB_SEARCH_MAX_RESULTS = 100
_SEARCH_OVERRIDE_ENV_VARS = (
    "MEMPALACE_BACKEND",
    "MEMPALACE_BACKEND_EXPLICIT",
    "MEMPALACE_EMBEDDING_API_KEY",
    "MEMPALACE_EMBEDDING_API_MODEL",
    "MEMPALACE_EMBEDDING_API_URL",
    "MEMPALACE_EMBEDDING_DEVICE",
    "MEMPALACE_EMBEDDING_MODEL",
    "MEMPALACE_EMBEDDING_THREADS",
    "MEMPALACE_LANG",
    "MEMPAL_LANG",
    "MEMPALACE_MILVUS_CONSISTENCY_LEVEL",
    "MEMPALACE_MILVUS_DB_NAME",
    "MEMPALACE_MILVUS_NAMESPACE",
    "MEMPALACE_MILVUS_TOKEN",
    "MEMPALACE_MILVUS_URI",
    "MEMPALACE_PGVECTOR_DSN",
    "MEMPALACE_PGVECTOR_NAMESPACE",
    "MEMPALACE_QDRANT_API_KEY",
    "MEMPALACE_QDRANT_NAMESPACE",
    "MEMPALACE_QDRANT_TIMEOUT",
    "MEMPALACE_QDRANT_URL",
)


def _hub_forward_disabled() -> bool:
    return os.environ.get(_HUB_FORWARD_ENV, "").strip().lower() in {"0", "false", "no", "off"}


def _search_args_forwardable(args) -> bool:
    """Return whether ``mempalace_search`` preserves this CLI search exactly."""
    if _backend_arg(args) or not 1 <= args.results <= _HUB_SEARCH_MAX_RESULTS:
        return False
    if any(os.environ.get(name, "").strip() for name in _SEARCH_OVERRIDE_ENV_VARS):
        return False

    # The MCP tool sanitizes queries longer than its safe passthrough window.
    # Keep any query it would rewrite on the direct CLI path so forwarding
    # never changes the user's search text silently.
    from ..config import sanitize_name, strip_lone_surrogates
    from ..query_sanitizer import SAFE_QUERY_LENGTH

    cleaned = strip_lone_surrogates(args.query.strip())
    if cleaned != args.query or len(cleaned) > SAFE_QUERY_LENGTH:
        return False

    for field_name in ("wing", "room"):
        value = getattr(args, field_name, None)
        if value is None:
            continue
        try:
            if sanitize_name(value, field_name) != value:
                return False
        except ValueError:
            return False
    return True


def _print_hub_search_result(args, result: dict) -> bool:
    """Render an MCP search result using the CLI's human-readable shape."""
    if not isinstance(result, dict):
        return False

    error = result.get("error")
    if error:
        details = result.get("details")
        message = f"{error}: {details}" if details else str(error)
        print(f"mempalace: hub search failed: {message}", file=sys.stderr)
        return False

    cli_output = result.get("cli_output")
    if isinstance(cli_output, str):
        cli_error_output = result.get("cli_error_output")
        if isinstance(cli_error_output, str):
            print(cli_error_output, end="", file=sys.stderr)
        print(cli_output, end="")
        return True

    hits = result.get("results")
    if not isinstance(hits, list):
        return False

    if result.get("vector_disabled") or result.get("fallback") == "bm25_only_via_sqlite":
        print(
            "\n  NOTICE: vector search disabled — showing BM25-only results.\n"
            "          Run `mempalace repair` to restore vector search.\n"
        )

    if not hits:
        print(f'\n  No results found for: "{args.query}"')
        return True

    print(f"\n{'=' * 60}")
    print(f'  Results for: "{args.query}"')
    if args.wing:
        print(f"  Wing: {args.wing}")
    if args.room:
        print(f"  Room: {args.room}")
    if args.since:
        print(f"  Since: {args.since}")
    if args.before:
        print(f"  Before: {args.before}")
    print(f"{'=' * 60}\n")

    for index, hit in enumerate(hits, 1):
        wing = hit.get("wing", "?")
        room = hit.get("room", "?")
        source = hit.get("source_file", "?")
        similarity = hit.get("similarity")
        bm25 = hit.get("bm25_score", 0.0)

        print(f"  [{index}] {wing} / {room}")
        print(f"      Source: {source}")
        if similarity is None:
            print(f"      Match:  bm25={bm25}  (vector disabled)")
        else:
            print(f"      Match:  similarity={similarity}  bm25={bm25}")
        print()
        for line in (hit.get("text") or "").strip().split("\n"):
            print(f"      {line}")
        print()
        print(f"  {'-' * 56}")

    print()
    return True


def _forward_search_to_hub(args, palace_path: str) -> bool:
    """Run a CLI search in the palace's HTTP hub, if one is alive.

    A local Chroma search cold-loads the palace's full HNSW index. Reusing the
    long-lived hub prevents every shell command or agent worker from holding a
    private copy. If the hub accepts the request but fails mid-flight, do not
    fall back locally: that would recreate the memory spike this path avoids.
    """
    import json
    import urllib.error
    import urllib.request

    from .. import server_registry

    if _hub_forward_disabled():
        return False
    info = server_registry.read_live_serverinfo(palace_path)
    if not info:
        return False
    if "search_cli_compatible" not in info.get("capabilities", []):
        return False
    current_config = MempalaceConfig(palace_path=palace_path)
    if info.get("search_config_fingerprint") != current_config.search_config_fingerprint:
        return False

    base_url = server_registry.client_base_url(info)
    headers = {"Content-Type": "application/json"}

    try:
        health = urllib.request.Request(f"{base_url}/healthz", headers=headers)
        with urllib.request.urlopen(health, timeout=_HUB_HEALTH_TIMEOUT_S) as resp:
            if resp.status != 200:
                return False
    except (urllib.error.URLError, OSError, ValueError):
        return False

    arguments = {
        "query": args.query,
        "limit": args.results,
        "cli_compatible": True,
    }
    for name in ("wing", "room", "since", "before"):
        value = getattr(args, name, None)
        if value:
            arguments[name] = value

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mempalace_search", "arguments": arguments},
        },
        ensure_ascii=False,
    ).encode("utf-8")

    try:
        with server_registry.urlopen_with_server_tokens(
            palace_path,
            f"{base_url}/mcp",
            data=body,
            headers=headers,
            timeout=_HUB_SEARCH_TIMEOUT_S,
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            # Authentication failed before the Hub accepted the search, so
            # direct execution is still safe. This covers Hubs started with
            # an explicit/env token that is intentionally not persisted in
            # the per-palace token file, as well as a stale local token.
            return False
        print(f"mempalace: hub rejected search ({exc.code} {exc.reason})", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        print(
            f"mempalace: hub at {base_url} did not complete the search ({exc}); "
            "not retrying directly because that would load another full index. "
            f"Set {_HUB_FORWARD_ENV}=0 to force a direct search.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"mempalace: forwarding search to palace hub {base_url} (pid {info.get('pid')})",
        file=sys.stderr,
    )

    if payload.get("error"):
        err = payload["error"]
        print(
            f"mempalace: hub refused search: {err.get('message', 'unknown error')}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        result = json.loads(payload["result"]["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        print("mempalace: hub returned an unrecognized search response", file=sys.stderr)
        sys.exit(1)

    if not _print_hub_search_result(args, result):
        sys.exit(1)
    return True


def _mine_args_forwardable(args, include_ignored) -> bool:
    """Only forward mines the ``mempalace_mine`` MCP tool can express.

    Flags the tool has no parameters for (kg-extract, gitignore handling,
    chunking overrides, origin redetection, explicit backend) keep the
    direct path — where a held writer lease still surfaces as the existing
    MineAlreadyRunning error rather than being silently dropped.
    """
    if getattr(args, "kg_extract", False) or getattr(args, "redetect_origin", False):
        return False
    if args.no_gitignore or include_ignored:
        return False
    if getattr(args, "max_chunks_per_file", None) is not None:
        return False
    if _backend_arg(args):
        return False
    return True


def _forward_mine_to_hub(args, palace_path: str) -> bool:
    """Run this mine inside the palace's HTTP hub, if one is alive.

    A long-lived hub (``mempalace serve``) holds the MCP writer lease for
    its whole lifetime, so a direct mine from this process — including the
    background save hooks, which spawn exactly this CLI — would be refused
    and transcript capture would silently stop on the hub machine. The hub
    itself may mine (the palace lock is process-re-entrant there, #1859),
    so the fix is to hand it the job over HTTP.

    Returns True when the hub handled the mine (this function has already
    printed the outcome and exited non-zero on failure). Returns False when
    there is no usable hub — the caller proceeds with the direct path.
    Once the hub has accepted the request there is no fallback: the job may
    already be running, and re-mining directly would race it.
    """
    import json
    import urllib.error
    import urllib.request

    from .. import server_registry

    if _hub_forward_disabled():
        return False
    info = server_registry.read_live_serverinfo(palace_path)
    if not info or info.get("read_only"):
        return False

    base_url = server_registry.client_base_url(info)
    headers = {"Content-Type": "application/json"}

    try:
        health = urllib.request.Request(f"{base_url}/healthz", headers=headers)
        with urllib.request.urlopen(health, timeout=_HUB_HEALTH_TIMEOUT_S) as resp:
            if resp.status != 200:
                return False
    except (urllib.error.URLError, OSError, ValueError):
        return False

    arguments = {
        "source": os.path.abspath(os.path.expanduser(args.dir)),
        "mode": args.mode,
        "agent": args.agent,
        "limit": args.limit or 0,
        "dry_run": bool(args.dry_run),
        "extract": args.extract,
    }
    if args.wing:
        arguments["wing"] = args.wing
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mempalace_mine", "arguments": arguments},
        }
    ).encode("utf-8")

    print(f"mempalace: forwarding mine to palace hub {base_url} (pid {info.get('pid')})")
    try:
        with server_registry.urlopen_with_server_tokens(
            palace_path,
            f"{base_url}/mcp",
            data=body,
            headers=headers,
            timeout=_HUB_MINE_TIMEOUT_S,
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # The hub answered — the request reached it, so no direct fallback.
        print(f"mempalace: hub rejected mine ({exc.code} {exc.reason})", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(
            f"mempalace: hub at {base_url} did not complete the mine ({exc}); "
            "not retrying directly — the hub may still be running the job. "
            f"Set {_HUB_FORWARD_ENV}=0 to force direct mines.",
            file=sys.stderr,
        )
        sys.exit(1)

    if payload.get("error"):
        err = payload["error"]
        print(
            f"mempalace: hub refused mine: {err.get('message', 'unknown error')}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        result = json.loads(payload["result"]["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        print("mempalace: hub returned an unrecognized mine response", file=sys.stderr)
        sys.exit(1)

    output = result.get("output")
    if output:
        print(output)
    if not result.get("success", False):
        print(f"mempalace: hub mine failed: {result.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)
    return True
