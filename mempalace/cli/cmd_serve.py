# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


def cmd_hook(args):
    """Run hook logic: reads JSON from stdin, outputs JSON to stdout."""
    from ..hooks_cli import run_hook

    run_hook(hook_name=args.hook, harness=args.harness)


def cmd_instructions(args):
    """Output skill instructions to stdout."""
    from ..instructions_cli import run_instructions

    run_instructions(name=args.name)


def cmd_rules(args):
    """Output the shared-brain agent rules block for a given agent identity."""
    from ..instructions_cli import run_rules

    run_rules(agent_id=args.agent)


def cmd_mcp(args):
    """Show how to wire MemPalace into MCP-capable hosts."""
    base_server_cmd = "mempalace-mcp"
    cmd_parts = [base_server_cmd]

    if args.palace:
        resolved_palace = str(Path(args.palace).expanduser())
        cmd_parts.extend(["--palace", shlex.quote(resolved_palace)])
    backend = _backend_arg(args)
    if backend:
        cmd_parts.extend(["--backend", shlex.quote(str(backend).strip().lower())])
    server_cmd = " ".join(cmd_parts)

    light_parts = ["mempalace-light-mcp"]
    if args.palace:
        light_parts.extend(["--palace", shlex.quote(resolved_palace)])
    if backend:
        light_parts.extend(["--backend", shlex.quote(str(backend).strip().lower())])
    light_cmd = " ".join(light_parts)

    print("MemPalace MCP quick setup:")
    print(f"  claude mcp add mempalace -- {server_cmd}")
    print(f"  codex mcp add mempalace -- {server_cmd}")
    print("\nLightweight MCP setup (3 consolidated tools, ~92% fewer schema tokens):")
    print(f"  claude mcp add mempalace-light -- {light_cmd}")
    print(f"  codex mcp add mempalace-light -- {light_cmd}")
    print("\nRun the server directly:")
    print(f"  {server_cmd}")

    if not args.palace:
        print("\nOptional custom palace:")
        print(f"  claude mcp add mempalace -- {base_server_cmd} --palace /path/to/palace")
        print(f"  codex mcp add mempalace -- {base_server_cmd} --palace /path/to/palace")
        print("  claude mcp add mempalace-light -- mempalace-light-mcp --palace /path/to/palace")
        print(f"  {base_server_cmd} --palace /path/to/palace")


_SERVER_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_SERVER_BIND_ALL_HOSTS = {"0.0.0.0", "::", "[::]"}


def _server_is_loopback(host: str) -> bool:
    return (host or "").strip().lower() in _SERVER_LOOPBACK_HOSTS


def _server_token_path(palace_path: str) -> Path:
    """Per-palace location for the auto-generated server bearer token.

    Distinct from the daemon's token dir; keyed by the canonical palace path so
    one server per palace reuses a stable token across restarts. Delegates to
    ``server_registry`` so the token and the hub serverinfo record share one
    directory convention.
    """
    from ..server_registry import server_token_path

    return server_token_path(palace_path)


def _load_or_create_server_token(palace_path: str) -> tuple[str, bool]:
    """Return (token, created). Reuse an existing 0600 token or mint a new one."""
    import secrets

    token_path = _server_token_path(palace_path)
    if token_path.exists():
        existing = token_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing, False
    token = secrets.token_urlsafe(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(token_path.parent), 0o700)
    except OSError:
        pass
    # O_CREAT with 0600 so the token is never briefly world-readable on disk.
    fd = os.open(str(token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token, True


def cmd_serve(args):
    """Run a secure remote HTTP MCP server for a team to share one palace (#1877).

    A turnkey wrapper over ``mempalace-mcp --transport http``: it resolves a
    bearer token (auto-generating a strong one for non-loopback binds), prints a
    ready-to-paste client config, then execs the real server in the foreground so
    Docker/systemd own the process lifecycle. The token is passed via the
    environment, never argv, so it can't leak through ``ps``.
    """
    host = args.host
    port = int(args.port)
    loopback = _server_is_loopback(host)
    palace_path = (
        os.path.abspath(os.path.expanduser(args.palace))
        if args.palace
        else MempalaceConfig().palace_path
    )
    backend = _backend_arg(args)

    tls_cert = os.path.expanduser(args.tls_cert) if args.tls_cert else None
    tls_key = os.path.expanduser(args.tls_key) if args.tls_key else None
    if bool(tls_cert) != bool(tls_key):
        print("mempalace: --tls-cert and --tls-key must be given together", file=sys.stderr)
        sys.exit(2)
    for label, path in (("--tls-cert", tls_cert), ("--tls-key", tls_key)):
        if path and not os.path.isfile(path):
            print(f"mempalace: {label} file not found: {path}", file=sys.stderr)
            sys.exit(2)
    scheme = "https" if tls_cert else "http"

    # Token resolution. Explicit flag > existing env > (non-loopback) auto-generated.
    token = (args.token or os.environ.get("MEMPALACE_MCP_HTTP_TOKEN", "")).strip()
    token_created = False
    if not token and not loopback and not args.allow_insecure:
        token, token_created = _load_or_create_server_token(palace_path)

    # Build the child environment. Token rides in the env (never argv) so it
    # stays out of the process table.
    env = dict(os.environ)
    env["MEMPALACE_PALACE_PATH"] = palace_path
    if backend:
        env["MEMPALACE_BACKEND"] = str(backend).strip().lower()
    if token:
        env["MEMPALACE_MCP_HTTP_TOKEN"] = token
    if args.allow_insecure:
        env["MEMPALACE_MCP_HTTP_ALLOW_INSECURE_NO_TOKEN"] = "1"

    child = [
        sys.executable,
        "-m",
        "mempalace.mcp_server",
        "--transport",
        "http",
        "--host",
        host,
        "--port",
        str(port),
    ]
    if backend:
        child += ["--backend", str(backend).strip().lower()]
    child += ["--palace", palace_path]
    if tls_cert:
        child += ["--tls-cert", tls_cert, "--tls-key", tls_key]
    if args.read_only:
        child.append("--read-only")

    # Client-facing address: 0.0.0.0/:: means "all interfaces" — clients dial a
    # real reachable host, so show a placeholder rather than the bind wildcard.
    client_host = "YOUR_SERVER_HOST" if host.strip().lower() in _SERVER_BIND_ALL_HOSTS else host
    url = f"{scheme}://{client_host}:{port}/mcp"

    print("Starting MemPalace remote MCP server")
    print(f"  palace   : {palace_path}")
    print(f"  backend  : {(backend or 'default').strip().lower() if backend else 'default'}")
    print(f"  bind     : {host}:{port}  ({'loopback' if loopback else 'network-exposed'})")
    print(f"  tls      : {'on' if tls_cert else 'off (plaintext -- terminate TLS at a proxy)'}")
    print(f"  read-only: {'yes' if args.read_only else 'no'}")
    if token_created:
        print("\n  A new bearer token was generated and stored 0600 at:")
        print(f"    {_server_token_path(palace_path)}")
        print("  Store it securely -- clients need it to connect:")
        print(f"    {token}")
    print("\nConnect a client:")
    if token:
        print(
            f"  claude mcp add --transport http mempalace {url} "
            f'--header "Authorization: Bearer {token if token_created else "$MEMPALACE_MCP_HTTP_TOKEN"}"'
        )
    else:
        print(f"  claude mcp add --transport http mempalace {url}")
    print(f"  curl {scheme}://{client_host}:{port}/healthz   # liveness (no auth)\n")
    sys.stdout.flush()

    # Foreground: hand the process to the real server so signals (SIGTERM from
    # Docker/systemd) reach it directly. exec on POSIX; subprocess on Windows
    # (no exec semantics) propagating the exit code.
    if os.name == "posix":
        os.execve(sys.executable, child, env)
    else:
        import subprocess

        completed = subprocess.run(child, env=env)
        sys.exit(completed.returncode)


def cmd_compress(args):
    """Compress drawers in a wing using AAAK Dialect."""
    from ..dialect import Dialect
    from ..palace import get_closets_collection

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path

    # Load dialect (with optional entity config)
    config_path = args.config
    if not config_path:
        # ``isfile`` rather than ``exists``: the latter is true for a FIFO,
        # and ``Dialect.from_config`` opens whatever it is handed, which
        # blocks in the kernel on a pipe named entities.json in the cwd.
        for candidate in ["entities.json", os.path.join(palace_path, "entities.json")]:
            if os.path.isfile(candidate):
                config_path = candidate
                break

    if config_path and os.path.isfile(config_path):
        dialect = Dialect.from_config(config_path)
        print(f"  Loaded entity config: {config_path}")
    else:
        dialect = Dialect()

    # State-aware open: distinguish "no palace" from "initialized but empty"
    # from "corrupt" via the shared helper (#1498). MCP and library callers
    # catch the backend exceptions directly; CLI gets the friendly print.
    from ..palace import _open_collection_or_explain

    col = _open_collection_or_explain(palace_path, collection_name="mempalace_drawers")
    if col is None:
        sys.exit(1)

    # Query drawers in batches to avoid SQLite variable limit (~999)
    where = {"wing": args.wing} if args.wing else None
    _BATCH = 500
    docs, metas, ids = [], [], []
    offset = 0
    while True:
        try:
            kwargs = {
                "include": ["documents", "metadatas"],
                "limit": _BATCH,
                "offset": offset,
            }
            if where:
                kwargs["where"] = where
            batch = col.get(**kwargs)
        except Exception as e:
            if not docs:
                print(f"\n  Error reading drawers: {e}")
                sys.exit(1)
            break
        batch_docs = batch.get("documents", [])
        if not batch_docs:
            break
        docs.extend(batch_docs)
        metas.extend(batch.get("metadatas", []))
        ids.extend(batch.get("ids", []))
        offset += len(batch_docs)
        if len(batch_docs) < _BATCH:
            break

    if not docs:
        wing_label = f" in wing '{args.wing}'" if args.wing else ""
        print(f"\n  No drawers found{wing_label}.")
        return

    print(
        f"\n  Compressing {len(docs)} drawers"
        + (f" in wing '{args.wing}'" if args.wing else "")
        + "..."
    )
    print()

    total_original = 0
    total_compressed = 0
    compressed_entries = []

    for doc, meta, doc_id in zip(docs, metas, ids):
        compressed = dialect.compress(doc, metadata=meta)
        stats = dialect.compression_stats(doc, compressed)

        total_original += stats["original_chars"]
        total_compressed += stats["summary_chars"]

        compressed_entries.append((doc_id, compressed, meta, stats))

        if args.dry_run:
            wing_name = meta.get("wing", "?")
            room_name = meta.get("room", "?")
            source = Path(meta.get("source_file", "?")).name
            print(f"  [{wing_name}/{room_name}] {source}")
            print(
                f"    {stats['original_tokens_est']}t -> {stats['summary_tokens_est']}t ({stats['size_ratio']:.1f}x)"
            )
            print(f"    {compressed}")
            print()

    # Store compressed versions (unless dry-run)
    if not args.dry_run:
        try:
            # Route through palace.get_closets_collection so the shared
            # _DEFAULT_BACKEND is reused (avoids a redundant ChromaBackend
            # instance and its potential WAL-lock contention on Windows).
            comp_col = get_closets_collection(palace_path, create=True)
            for doc_id, compressed, meta, stats in compressed_entries:
                comp_meta = dict(meta)
                comp_meta["compression_ratio"] = round(stats["size_ratio"], 1)
                comp_meta["original_tokens"] = stats["original_tokens_est"]
                comp_col.upsert(
                    ids=[doc_id],
                    documents=[compressed],
                    metadatas=[comp_meta],
                )
            print(
                f"  Stored {len(compressed_entries)} compressed drawers in 'mempalace_closets' collection."
            )
        except Exception as e:
            print(f"  Error storing compressed drawers: {e}")
            sys.exit(1)

    # Summary
    ratio = total_original / max(total_compressed, 1)
    # Estimate tokens from char count (~3.8 chars/token for English text)
    orig_tokens = max(1, int(total_original / 3.8))
    comp_tokens = max(1, int(total_compressed / 3.8))
    print(f"  Total: {orig_tokens:,}t -> {comp_tokens:,}t ({ratio:.1f}x compression)")
    if args.dry_run:
        print("  (dry run -- nothing stored)")
