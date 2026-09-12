# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


def cmd_init(args):
    import json
    from pathlib import Path
    from ..entity_detector import confirm_entities
    from ..project_scanner import discover_entities
    from ..room_detector_local import detect_rooms_local

    # Honor --palace (issue #1313): without this, init silently ignored the
    # flag and always used ~/.mempalace. Mirror the env-var pattern used by
    # mcp_server.py so every downstream read of ``cfg.palace_path`` (Pass 0,
    # cfg.init(), the post-init mine) routes to the user-specified location.
    if getattr(args, "palace", None):
        os.environ["MEMPALACE_PALACE_PATH"] = os.path.abspath(os.path.expanduser(args.palace))

    cfg = MempalaceConfig()

    # Resolve entity-detection languages: --lang overrides config.
    lang_arg = getattr(args, "lang", None)
    if lang_arg:
        languages = [s.strip() for s in lang_arg.split(",") if s.strip()] or ["en"]
        cfg.set_entity_languages(languages)
    else:
        languages = cfg.entity_languages
    languages_tuple = tuple(languages)

    # --llm is ON by default. --no-llm is the explicit opt-out. Provider
    # precedence is unchanged (Ollama localhost first, then openai-compat,
    # then anthropic). Never block init on a missing LLM: when no provider
    # responds, print a one-line message pointing at --no-llm and fall
    # through to heuristics-only.
    llm_provider = None
    if not getattr(args, "no_llm", False):
        provider_name = getattr(args, "llm_provider", "ollama") or "ollama"
        provider_model = getattr(args, "llm_model", "gemma4:e4b") or "gemma4:e4b"
        try:
            candidate = get_provider(
                name=provider_name,
                model=provider_model,
                endpoint=getattr(args, "llm_endpoint", None),
                api_key=getattr(args, "llm_api_key", None),
            )
            if (
                provider_name == "openai-compat"
                and getattr(candidate, "api_key_source", None) == "env"
                and candidate.is_external_service
            ):
                ok = False
                msg = "external openai-compat init requires explicit --llm-api-key"
                print(f"  LLM skipped: {msg}")
            else:
                ok, msg = candidate.check_available()
            if ok:
                llm_provider = candidate
                print(f"  LLM enabled: {provider_name}/{provider_model}")
                # Privacy warning (issue #24): if the configured endpoint
                # sends data off the user's machine/network, surface that
                # before init proceeds. URL-based — Ollama on localhost,
                # LM Studio on LAN, etc. won't trigger; Anthropic /
                # cloud OpenAI-compat / any non-local endpoint will.
                if candidate.is_external_service:
                    print(
                        f"  ⚠ {provider_name} is an EXTERNAL API. Your folder "
                        f"content will be sent to the provider during init. "
                        f"MemPalace does not control how the provider logs, "
                        f"retains, or uses your data. Pass --no-llm to keep "
                        f"init fully local."
                    )
                    # Consent gate (issue #26): block init when the api_key
                    # was acquired via env-fallback (stray credential in
                    # shell env). Explicit --llm-api-key (api_key_source ==
                    # "flag") means the user already opted in.
                    # --accept-external-llm bypasses for CI / non-interactive.
                    api_key_source = getattr(candidate, "api_key_source", None)
                    accept_flag = getattr(args, "accept_external_llm", False)
                    if api_key_source == "env" and not accept_flag:
                        try:
                            answer = (
                                input(
                                    "  Your API key was loaded from the environment "
                                    "(not passed via --llm-api-key). Continue with "
                                    "external LLM? [y/N] "
                                )
                                .strip()
                                .lower()
                            )
                        except EOFError:
                            answer = ""
                        if answer != "y":
                            print(
                                "  Declined — falling back to heuristics-only. "
                                "Pass --llm-api-key explicitly or "
                                "--accept-external-llm to skip this prompt."
                            )
                            llm_provider = None
            else:
                print(
                    f"  No LLM provider reachable ({msg}). "
                    f"Running heuristics-only — pass --no-llm to silence this."
                )
        except LLMError as e:
            print(
                f"  LLM init failed ({e}). Running heuristics-only — pass --no-llm to silence this."
            )

    # Pass 0: detect whether the corpus is AI-dialogue. Writes
    # <palace>/.mempalace/origin.json and supplies corpus context to the
    # entity classifier so it can correctly handle agent persona names
    # (e.g. "Echo", "Sparrow") without misclassifying them as people.
    corpus_origin = _run_pass_zero(
        project_dir=args.dir,
        palace_dir=cfg.palace_path,
        llm_provider=llm_provider,
    )

    # Pass 1: discover entities — manifests + git authors first, prose detection
    # as supplement for names mentioned only in docs/notes. Optional phase-2
    # LLM refinement runs inside discover_entities when llm_provider is given.
    print(f"\n  Scanning for entities in: {args.dir}")
    if languages_tuple != ("en",):
        print(f"  Languages: {', '.join(languages_tuple)}")
    detected = discover_entities(
        args.dir,
        languages=languages_tuple,
        llm_provider=llm_provider,
        corpus_origin=corpus_origin,
    )
    total = (
        len(detected["people"])
        + len(detected["projects"])
        + len(detected.get("topics", []))
        + len(detected["uncertain"])
    )
    if total > 0:
        confirmed = confirm_entities(detected, yes=getattr(args, "yes", False))
        # Save confirmed entities to <project>/entities.json (per-project
        # audit trail — user can inspect or hand-edit) AND merge into the
        # global registry the miner reads at mine time. Topics are kept
        # separately so the miner can later compute cross-wing tunnels
        # from shared topics (see palace_graph.compute_topic_tunnels).
        if confirmed["people"] or confirmed["projects"] or confirmed.get("topics"):
            project_path = Path(args.dir).expanduser().resolve()
            entities_path = project_path / "entities.json"
            # Opening a pre-existing FIFO for writing blocks in the kernel
            # until a reader appears. Only a regular file is a valid target
            # for the per-project audit trail; the global registry merge
            # below is unaffected either way.
            if entities_path.exists() and not entities_path.is_file():
                print(
                    f"  ! Not writing entities: {entities_path} is not a regular file",
                    file=sys.stderr,
                )
            else:
                with open(entities_path, "w", encoding="utf-8") as f:
                    json.dump(confirmed, f, indent=2, ensure_ascii=False)
                print(f"  Entities saved: {entities_path}")

            from ..config import normalize_wing_name
            from ..miner import add_to_known_entities

            # Match the slug ``room_detector_local`` writes into
            # ``mempalace.yaml`` so the miner's tunnel lookup hits the
            # same key in ``topics_by_wing`` at mine time (issue #1194 —
            # without this, hyphenated dirnames silently lose tunnels).
            wing = normalize_wing_name(project_path.name)
            registry_path = add_to_known_entities(confirmed, wing=wing)
            print(f"  Registry updated: {registry_path}")
    else:
        print("  No entities detected -- proceeding with directory-based rooms.")

    # Pass 2: detect rooms from folder structure
    try:
        detect_rooms_local(project_dir=args.dir, yes=getattr(args, "yes", False))
    except OSError as exc:
        # Writing mempalace.yaml is the point of init; a target it cannot
        # write (a pre-existing pipe, a full disk) is a hard failure, and a
        # message beats the traceback this used to produce.
        print(f"\n  ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    cfg.init()
    backend = _backend_arg(args)
    if backend:
        cfg.set_backend(backend)

    # Pass 3: protect git repos from accidentally committing per-project files
    _ensure_mempalace_files_gitignored(args.dir)

    # Pass 4: offer to run mine immediately. The directory just had its
    # rooms + entities set up, so 99% of users will mine next anyway —
    # asking here removes the "remember to type the next command" friction.
    # `--auto-mine` skips the prompt and mines automatically; `--yes` is
    # SCOPED to entity auto-accept and does NOT imply mining.
    _maybe_run_mine_after_init(args, cfg)


def _format_size_mb(num_bytes: int) -> str:
    """Render a byte count as a human-readable size for the mine estimate.

    < 1 MB rounds up to ``<1 MB`` so users never see a misleading ``0 MB``
    on small projects. Otherwise reports an integer megabyte count.
    """
    if num_bytes <= 0:
        return "<1 MB"
    mb = num_bytes / (1024 * 1024)
    if mb < 1:
        return "<1 MB"
    return f"{mb:.0f} MB"


def _maybe_run_mine_after_init(args, cfg) -> None:
    """Prompt the user to mine the directory just initialised, or auto-mine
    when ``--auto-mine`` was passed. Extracted so the prompt path is
    unit-testable.

    Behaviour matrix:

    - default (no flags) — prompt, default Yes, mine in-process if accepted
    - ``--yes`` — entity auto-accept only; STILL prompts for the mine step
    - ``--auto-mine`` — skip the mine prompt and mine directly
    - ``--yes --auto-mine`` — fully non-interactive

    Mine errors are surfaced (not swallowed): a failing mine exits with a
    non-zero status via :func:`sys.exit` so downstream scripts can see it.
    The pre-scan that produces the file-count estimate is reused as the
    mine input so we never walk the corpus twice.
    """
    from ..miner import mine, scan_project

    project_dir = args.dir
    auto_mine = bool(getattr(args, "auto_mine", False))

    # Single corpus walk: this scan feeds BOTH the "what would be mined"
    # estimate the user sees in the prompt AND the file list mine() will
    # process. We pass the result into mine() via the `files` kwarg so it
    # doesn't re-walk the tree.
    try:
        scanned_files = scan_project(project_dir)
        file_count = len(scanned_files)
        total_bytes = 0
        for fp in scanned_files:
            try:
                total_bytes += fp.stat().st_size
            except OSError:
                # Skip files that vanished between scan and stat — mine()
                # will skip them too.
                continue
        size_str = _format_size_mb(total_bytes)
    except Exception:
        scanned_files = None
        file_count = None
        size_str = None

    # Show the scope estimate BEFORE the prompt so the user knows what
    # they are agreeing to. On a real corpus mine takes minutes; hitting
    # Enter on a default-Y prompt with no size cue is a footgun.
    if isinstance(file_count, int):
        if size_str:
            print(f"  ~{file_count} files (~{size_str}) would be mined into this palace.\n")
        else:
            print(f"  ~{file_count} files would be mined into this palace.\n")

    if not auto_mine:
        try:
            answer = input("  Mine this directory now? [Y/n] ").strip().lower()
        except EOFError:
            # Non-interactive stdin (e.g. piped) — treat like decline so
            # we don't block. User can re-run with --auto-mine to opt in.
            answer = "n"
        if answer not in ("", "y", "yes"):
            print(f"\n  Skipped. Run `mempalace mine {shlex.quote(project_dir)}` when ready.")
            return

    palace_path = cfg.palace_path
    try:
        mine(
            project_dir=project_dir,
            palace_path=palace_path,
            files=scanned_files,
        )
    except KeyboardInterrupt:
        # mine() handles its own SIGINT summary + sys.exit(130); re-raise
        # any KeyboardInterrupt that escapes (shouldn't happen) so the
        # shell still sees a clean interrupt rather than a swallowed one.
        raise
    except Exception as e:
        print(f"\n  ERROR: mine failed: {e}", file=sys.stderr)
        sys.exit(1)
