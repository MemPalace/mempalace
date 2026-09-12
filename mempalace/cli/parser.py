# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


def _reconfigure_stdio_utf8_on_windows():
    """Decode stdio as UTF-8 on Windows for the primary `mempalace` CLI.

    Thin wrapper around the shared helper in ``mempalace._stdio``. The CLI
    overrides stdout/stderr to ``replace`` because ``mempalace search``
    prints verbatim drawer text that may carry surrogate halves
    round-tripped from filenames -- ``strict`` would crash mid-print and
    lose the rest of the search result block. stdin keeps the default
    ``surrogateescape`` so a redirected non-UTF-8 file does not kill the
    read on the first bad byte.
    """
    from .._stdio import reconfigure_stdio_utf8_on_windows

    reconfigure_stdio_utf8_on_windows(stdout_errors="replace", stderr_errors="replace")


def main():
    """CLI entry point for the ``mempalace`` console script.

    Side effect: pops ``PYTHONPATH`` from ``os.environ`` (see #1423) so
    any subprocess this CLI spawns inherits a clean env. Host applications
    that call ``main()`` programmatically should be aware that the parent
    process loses ``PYTHONPATH`` as well. Library imports
    (``import mempalace.searcher`` from a host app) do NOT trigger this
    side effect; only the CLI/MCP entry points pop the env var.
    """
    # Drop leaked PYTHONPATH so any subprocess the CLI spawns (mine workers,
    # repair tooling) starts with a clean env. The sys.path filter in
    # mempalace/__init__.py already protects this process from the same
    # ABI mismatch; here we extend the protection to children.
    os.environ.pop("PYTHONPATH", None)

    _reconfigure_stdio_utf8_on_windows()

    version_label = f"MemPalace {__version__}"
    parser = argparse.ArgumentParser(
        description="MemPalace — Give your AI a memory. No API key required.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"{version_label}\n\n{__doc__}",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=version_label,
        help="Show version and exit",
    )
    parser.add_argument(
        "--palace",
        default=None,
        help="Where the palace lives (default: from ~/.mempalace/config.json or ~/.mempalace/palace)",
    )
    parser.add_argument(
        "--backend",
        dest="global_backend",
        default=None,
        help="Storage backend to use for this command (default: config/env/detected/chroma)",
    )

    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Detect rooms from your folder structure")
    p_init.add_argument("dir", help="Project directory to set up")
    p_init.add_argument(
        "--backend",
        default=None,
        help="Storage backend to persist for this palace (default: chroma)",
    )
    p_init.add_argument(
        "--yes",
        action="store_true",
        help="Auto-accept all detected entities (non-interactive)",
    )
    p_init.add_argument(
        "--auto-mine",
        action="store_true",
        help=(
            "Skip the post-init mine prompt and run mine automatically. "
            "Combine with --yes for a fully non-interactive setup."
        ),
    )
    p_init.add_argument(
        "--lang",
        default=None,
        help=(
            "Comma-separated language codes for entity detection "
            "(e.g. 'en' or 'en,pt-br'). Defaults to value from config "
            "(MEMPALACE_ENTITY_LANGUAGES env var or config.json), or 'en'. "
            "When given, the value is also persisted to config.json."
        ),
    )
    p_init.add_argument(
        "--llm",
        action="store_true",
        help=(
            "DEPRECATED — LLM-assisted entity refinement is now ON by default. "
            "This flag is preserved for backward compatibility; pass --no-llm "
            "to opt out instead."
        ),
    )
    p_init.add_argument(
        "--no-llm",
        action="store_true",
        help=(
            "Disable LLM-assisted entity refinement. Run init in heuristics-only "
            "mode (no provider acquisition, no LLM calls). Use when running "
            "without a local LLM and you don't want the graceful-fallback message."
        ),
    )
    p_init.add_argument(
        "--llm-provider",
        default="ollama",
        choices=["ollama", "openai-compat", "anthropic"],
        help="LLM provider (default: ollama). Pass --no-llm to disable LLM-assisted refinement entirely.",
    )
    p_init.add_argument(
        "--llm-model",
        default="gemma4:e4b",
        help="Model name for the chosen provider (default: gemma4:e4b for Ollama).",
    )
    p_init.add_argument(
        "--llm-endpoint",
        default=None,
        help=(
            "Provider endpoint URL. Default for Ollama: http://localhost:11434. "
            "Required for openai-compat."
        ),
    )
    p_init.add_argument(
        "--llm-api-key",
        default=None,
        help=(
            "API key for the provider. For anthropic, defaults to $ANTHROPIC_API_KEY; "
            "for openai-compat, defaults to $OPENAI_API_KEY."
        ),
    )
    p_init.add_argument(
        "--accept-external-llm",
        action="store_true",
        help=(
            "Bypass the interactive consent prompt that fires when an external "
            "LLM is configured via an environment-variable API key (issue #26). "
            "Use this in CI / non-interactive runs where you've already decided "
            "the external send is acceptable."
        ),
    )

    # mine
    p_mine = sub.add_parser("mine", help="Mine files into the palace")
    p_mine.add_argument(
        "dir", help="Directory to mine, or one conversation file with --mode convos"
    )
    p_mine.add_argument(
        "--backend",
        default=None,
        help="Storage backend to use for this mine (default: config/env/detected/chroma)",
    )
    mine_source_group = p_mine.add_mutually_exclusive_group()
    mine_source_group.add_argument(
        "--mode",
        choices=["projects", "convos", "extract"],
        default=None,
        help=(
            "Ingest mode: 'projects' for code/docs (default), 'convos' for chat "
            "exports, 'extract' for office documents (PDF/DOCX/RTF/etc., requires "
            "mempalace[extract])"
        ),
    )
    mine_source_group.add_argument(
        "--source",
        default=None,
        metavar="ADAPTER",
        help=(
            "Use a registered source adapter. Cannot be combined with --mode; "
            "no --source preserves legacy projects-mode mining."
        ),
    )
    p_mine.add_argument("--wing", default=None, help="Wing name (default: directory name)")
    p_mine.add_argument(
        "--no-gitignore",
        action="store_true",
        help="Don't respect .gitignore files when scanning project files",
    )
    p_mine.add_argument(
        "--include-ignored",
        action="append",
        default=[],
        help="Always scan these project-relative paths even if ignored; repeat or pass comma-separated paths",
    )
    p_mine.add_argument(
        "--agent",
        default="mempalace",
        help="Your name — recorded on every drawer (default: mempalace)",
    )
    p_mine.add_argument("--limit", type=int, default=0, help="Max files to process (0 = all)")
    p_mine.add_argument(
        "--redetect-origin",
        action="store_true",
        help=(
            "Re-run corpus_origin detection on this directory and overwrite "
            "<palace>/.mempalace/origin.json. Useful when the corpus has grown "
            "since `mempalace init` and the stored origin may be stale. "
            "Heuristic-only (no LLM call) — re-run `mempalace init --llm` for "
            "Tier 2 refinement."
        ),
    )
    p_mine.add_argument(
        "--dry-run", action="store_true", help="Show what would be filed without filing"
    )
    p_mine.add_argument(
        "--daemon",
        action="store_true",
        help="Submit this mine to the opt-in local daemon queue",
    )
    p_mine.add_argument(
        "--background",
        action="store_true",
        help="With --daemon, return a job id immediately instead of waiting",
    )
    p_mine.add_argument(
        "--extract",
        choices=["exchange", "general"],
        default="exchange",
        help="Extraction strategy for convos mode: 'exchange' (default) or 'general' (5 memory types)",
    )

    p_mine.add_argument(
        "--max-chunks-per-file",
        type=int,
        default=None,
        metavar="N",
        help=(
            f"Per-file chunk cap; files producing more chunks are skipped with a "
            f"summary counter. Default {_CLI_MAX_CHUNKS_PER_FILE_DEFAULT} "
            f"(or MEMPALACE_MAX_CHUNKS_PER_FILE). Set 0 to disable. Lower this on "
            f"Windows if you hit ONNX bad_alloc (#1455)."
        ),
    )
    p_mine.add_argument(
        "--include-subagents",
        action="store_true",
        default=False,
        help=(
            "Also mine Claude Code subagent transcripts (subagents/ dirs). "
            "Excluded by default: these are short ephemeral exchanges "
            "(Explore/Plan/Grep agents) already summarized in the parent "
            "session, and on typical workspaces they dominate file counts."
        ),
    )

    # sweep
    p_sweep = sub.add_parser(
        "sweep",
        help="Tandem miner: catch anything the primary miner missed "
        "(message-level, timestamp-coordinated, idempotent)",
    )
    p_sweep.add_argument(
        "target",
        help="A .jsonl transcript file, or a directory to scan recursively",
    )

    # sync
    p_sync = sub.add_parser(
        "sync",
        help="Prune drawers whose source files are gitignored, deleted, or moved (#1252)",
    )
    p_sync.add_argument(
        "dir",
        nargs="?",
        default=None,
        help="Project root to sync (optional; auto-detects from drawer metadata)",
    )
    p_sync.add_argument("--wing", default=None, help="Limit to one wing")
    p_sync.add_argument(
        "--root",
        action="append",
        default=[],
        help="Additional project root (repeatable)",
    )
    p_sync.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Preview only (default)",
    )
    p_sync.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="Actually delete drawers (overrides --dry-run; requires --wing or a project root)",
    )
    p_sync.add_argument(
        "--daemon",
        action="store_true",
        help="Submit this sync to the opt-in local daemon queue",
    )
    p_sync.add_argument(
        "--background",
        action="store_true",
        help="With --daemon, return a job id immediately instead of waiting",
    )

    # search
    p_search = sub.add_parser("search", help="Find anything, exact words")
    p_search.add_argument("query", help="What to search for")
    p_search.add_argument(
        "--backend",
        default=None,
        help="Storage backend to use for this search (default: config/env/detected/chroma)",
    )
    p_search.add_argument("--wing", default=None, help="Limit to one project")
    p_search.add_argument("--room", default=None, help="Limit to one room")
    p_search.add_argument("--results", type=int, default=5, help="Number of results")
    p_search.add_argument(
        "--since",
        default=None,
        help=(
            "Only drawers filed on/after this ISO date/datetime (inclusive), "
            "e.g. 2026-04-01. Drawers without a filed_at are excluded while "
            "a date bound is set"
        ),
    )
    p_search.add_argument(
        "--before",
        default=None,
        help="Only drawers filed strictly before this ISO date/datetime (exclusive)",
    )

    # compress
    p_compress = sub.add_parser(
        "compress", help="Compress drawers using AAAK Dialect (~30x reduction)"
    )
    p_compress.add_argument("--wing", default=None, help="Wing to compress (default: all wings)")
    p_compress.add_argument(
        "--dry-run", action="store_true", help="Preview compression without storing"
    )
    p_compress.add_argument(
        "--config", default=None, help="Entity config JSON (e.g. entities.json)"
    )

    # wake-up
    p_wakeup = sub.add_parser("wake-up", help="Show L0 + L1 wake-up context (~600-900 tokens)")
    p_wakeup.add_argument("--wing", default=None, help="Wake-up for a specific project/wing")

    # split
    p_split = sub.add_parser(
        "split",
        help="Split concatenated transcript mega-files into per-session files (run before mine)",
    )
    p_split.add_argument("dir", help="Directory containing transcript files")
    p_split.add_argument(
        "--output-dir",
        default=None,
        help="Write split files here (default: same directory as source files)",
    )
    p_split.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be split without writing files",
    )
    p_split.add_argument(
        "--min-sessions",
        type=int,
        default=2,
        help="Only split files containing at least N sessions (default: 2)",
    )

    # hook
    p_hook = sub.add_parser(
        "hook",
        help="Run hook logic (reads JSON from stdin, outputs JSON to stdout)",
    )
    hook_sub = p_hook.add_subparsers(dest="hook_action")
    p_hook_run = hook_sub.add_parser("run", help="Execute a hook")
    p_hook_run.add_argument(
        "--hook",
        required=True,
        choices=["session-start", "stop", "session-end", "precompact"],
        help="Hook name to run",
    )
    p_hook_run.add_argument(
        "--harness",
        required=True,
        choices=["claude-code", "codex"],
        help="Harness type (determines stdin JSON format)",
    )

    # instructions
    p_instructions = sub.add_parser(
        "instructions",
        help="Output skill instructions to stdout",
    )
    instructions_sub = p_instructions.add_subparsers(dest="instructions_name")
    for instr_name in ["init", "search", "mine", "help", "status"]:
        instructions_sub.add_parser(instr_name, help=f"Output {instr_name} instructions")

    # rules
    p_rules = sub.add_parser(
        "rules",
        help=(
            "Output the canonical shared-brain agent rules block for a system "
            "prompt (CLAUDE.md, GEMINI.md, AGENTS.md, ...)"
        ),
    )
    p_rules.add_argument(
        "--agent",
        required=True,
        help="Stable agent identity to render into the rules, e.g. mac-claude",
    )

    # repair
    p_repair = sub.add_parser(
        "repair",
        help=(
            "Rebuild palace vector index (legacy mode) or un-poison max_seq_id rows "
            "(--mode max-seq-id)"
        ),
    )
    p_repair.add_argument(
        "--yes", action="store_true", help="Skip confirmation for destructive changes"
    )
    p_repair.add_argument(
        "repair_action",
        nargs="?",
        choices=["rebuild-index"],
        help=(
            "Re-embed the palace from SQLite using the current embedding model "
            "(alias for --mode from-sqlite --archive-existing)."
        ),
    )
    p_repair.add_argument(
        "--confirm-truncation-ok",
        action="store_true",
        help=(
            "Override the #1208 safety guard. Required when chromadb's collection-layer "
            "extraction returns exactly 10,000 drawers and the SQLite ground-truth check "
            "either matches or can't be read. Use only after independently confirming "
            "the palace really contains that count."
        ),
    )
    p_repair.add_argument(
        "--mode",
        choices=["legacy", "max-seq-id", "from-sqlite"],
        default="legacy",
        help=(
            "legacy: full-palace rebuild via the chromadb client (default). "
            "max-seq-id: un-poison max_seq_id rows corrupted by the legacy 0.6.x shim. "
            "from-sqlite: rebuild by reading rows directly from chroma.sqlite3, "
            "bypassing the chromadb client. Use when legacy mode bails because the "
            "chromadb client cannot open the collection."
        ),
    )
    p_repair.add_argument(
        "--source",
        default=None,
        help=(
            "Source palace path for --mode from-sqlite (defaults to --palace). "
            "Use when extracting from an archived corrupt palace into a new location."
        ),
    )
    p_repair.add_argument(
        "--archive-existing",
        action="store_true",
        help=(
            "For --mode from-sqlite when --source equals --palace: rename the "
            "existing palace to <palace>.pre-rebuild-<timestamp> before "
            "rebuilding so the corrupt copy is preserved."
        ),
    )
    p_repair.add_argument(
        "--segment",
        default=None,
        help="Segment UUID filter for --mode max-seq-id (repairs only that segment).",
    )
    p_repair.add_argument(
        "--from-sidecar",
        default=None,
        help=(
            "Path to a pre-corruption chroma.sqlite3 sidecar (for --mode max-seq-id); "
            "clean values are copied from its max_seq_id table verbatim."
        ),
    )
    p_repair.add_argument(
        "--backup",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Back up SQLite before mutation (default: on)",
    )
    p_repair.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what the repair would do and exit without modifying the palace",
    )

    # repair-status — read-only HNSW capacity health check (#1222)
    sub.add_parser(
        "repair-status",
        help="Compare sqlite vs HNSW element counts (read-only; never opens a chromadb client)",
    )

    # daemon
    p_daemon = sub.add_parser("daemon", help="Manage the opt-in long-lived daemon")
    daemon_sub = p_daemon.add_subparsers(dest="daemon_action")
    p_daemon_start = daemon_sub.add_parser("start", help="Start the daemon")
    p_daemon_start.add_argument(
        "--foreground",
        action="store_true",
        help="Run in the foreground for debugging or process supervisors",
    )
    p_daemon_start.add_argument(
        "--backend",
        default=None,
        help="Storage backend for this daemon (default: config/env/detected/chroma)",
    )
    daemon_sub.add_parser("stop", help="Stop the daemon")
    daemon_sub.add_parser("status", help="Show daemon status")
    p_daemon_jobs = daemon_sub.add_parser("jobs", help="List recent daemon jobs")
    p_daemon_jobs.add_argument("--limit", type=int, default=20, help="Max jobs to show")
    p_daemon_wait = daemon_sub.add_parser("wait", help="Wait for a daemon job")
    p_daemon_wait.add_argument("job_id", help="Job id returned by --background")

    # mcp
    p_mcp = sub.add_parser(
        "mcp",
        help="Show MCP setup command for connecting MemPalace to your AI client",
    )
    p_mcp.add_argument(
        "--backend",
        default=None,
        help="Storage backend to include in the MCP startup command",
    )

    # serve — turnkey remote HTTP MCP server (#1877)
    p_serve = sub.add_parser(
        "serve",
        help="Run a secure remote HTTP MCP server for a team to share one palace",
    )
    p_serve.add_argument(
        "--host", default="127.0.0.1", help="Bind address (use 0.0.0.0 for remote clients)"
    )
    p_serve.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    p_serve.add_argument(
        "--backend", default=None, help="Storage backend (default: config/env/detected)"
    )
    p_serve.add_argument("--palace", default=None, help="Palace path (overrides config/env)")
    p_serve.add_argument(
        "--token",
        default=None,
        help="Bearer token clients must present. Default: reuse/auto-generate one for "
        "non-loopback binds (stored 0600 under ~/.mempalace/server/).",
    )
    p_serve.add_argument("--tls-cert", default=None, help="PEM certificate to enable TLS")
    p_serve.add_argument("--tls-key", default=None, help="PEM private key matching --tls-cert")
    p_serve.add_argument(
        "--read-only",
        action="store_true",
        help="Expose recall only: tools that change state are hidden and refused",
    )
    p_serve.add_argument(
        "--allow-insecure",
        action="store_true",
        help="Permit a non-loopback bind with no token (only behind a trusted proxy)",
    )

    # status
    # migrate
    p_migrate = sub.add_parser(
        "migrate",
        help="Migrate palace from a different ChromaDB version (fixes 3.0.0 → 3.1.0 upgrade)",
    )
    p_migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without changing anything",
    )
    p_migrate.add_argument(
        "--yes", action="store_true", help="Skip confirmation for destructive changes"
    )

    # migrate-wings
    p_migrate_wings = sub.add_parser(
        "migrate-wings",
        help="Normalize legacy wing names (strip leading/trailing separators) so pre-#1675 palaces stay discoverable",
    )
    p_migrate_wings.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without modifying the palace",
    )
    p_migrate_wings.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    p_hallways = sub.add_parser("hallways", help="List entity hallways (associative graph)")
    p_hallways.add_argument("--wing", default=None, help="Filter to one wing")
    p_hallways.add_argument("--limit", type=int, default=50, help="Max hallways to show")
    p_status = sub.add_parser("status", help="Show what's been filed")
    p_status.add_argument(
        "--backend",
        default=None,
        help="Storage backend to use for status (default: config/env/detected/chroma)",
    )
    p_update = sub.add_parser("update", help="Opt-in release checks and upgrade planning")
    update_sub = p_update.add_subparsers(dest="update_action")
    p_update_configure = update_sub.add_parser("configure", help="Configure periodic checks")
    update_consent = p_update_configure.add_mutually_exclusive_group(required=True)
    update_consent.add_argument("--enable", dest="enabled", action="store_true")
    update_consent.add_argument("--disable", dest="enabled", action="store_false")
    p_update_configure.add_argument("--interval-days", type=int, default=7)
    p_update_configure.add_argument("--installer", choices=("uv-tool", "pipx", "pip"))
    update_sub.add_parser("check", help="Explicitly check the latest stable release")
    p_update_plan = update_sub.add_parser(
        "plan", help="Show the exact upgrade plan without applying it"
    )
    p_update_plan.add_argument("--installer", choices=("uv-tool", "pipx", "pip"))

    # logstream (RFC 003 agent coordination)
    p_logstream = sub.add_parser(
        "logstream",
        help="Agent coordination events — delegate work, wait for replies (RFC 003)",
    )
    logstream_sub = p_logstream.add_subparsers(dest="logstream_action")

    def _add_logstream_filters(p):
        p.add_argument("--stream", default=None, help="Stream, e.g. project/mempalace")
        p.add_argument("--room", default=None, help="Room, e.g. delegation, patches")
        p.add_argument("--topic", default=None, help="Topic, e.g. auth-v2")
        p.add_argument("--type", default=None, help="Event type, e.g. task.request")
        p.add_argument("--to-agent", default=None, help="Target agent (also matches '*')")
        p.add_argument("--from-agent", default=None, help="Writer agent")
        p.add_argument("--correlation-id", default=None, help="Task/conversation id")
        p.add_argument(
            "--status",
            default=None,
            help="open|claimed|ready|applied|blocked|failed|superseded",
        )
        p.add_argument("--since-event-id", default=None, help="Only events strictly after this id")
        p.add_argument(
            "--since-created-at",
            default=None,
            help="Only events at/after this time (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)",
        )

    p_ls_append = logstream_sub.add_parser("append", help="Append a coordination event")
    p_ls_append.add_argument("--type", required=True, help="Event type, e.g. task.request")
    p_ls_append.add_argument("--stream", required=True, help="Stream, e.g. project/mempalace")
    p_ls_append.add_argument("--room", required=True, help="Room, e.g. delegation")
    p_ls_append.add_argument("--topic", default=None, help="Topic name, e.g. auth-v2")
    p_ls_append.add_argument("--from-agent", required=True, help="Writer agent identity")
    p_ls_append.add_argument("--to-agent", default=None, help="Target agent or '*'")
    p_ls_append.add_argument("--correlation-id", default=None, help="Task/conversation id")
    p_ls_append.add_argument("--branch", default=None, help="Git branch")
    p_ls_append.add_argument("--base-commit", default=None, help="Git commit work started from")
    p_ls_append.add_argument(
        "--status",
        default=None,
        help="open|claimed|ready|applied|blocked|failed|superseded",
    )
    p_ls_append.add_argument("--body", default=None, help="Verbatim body text")
    p_ls_append.add_argument(
        "--body-file", default=None, help="Read body from file ('-' for stdin)"
    )
    p_ls_append.add_argument("--metadata", default=None, help="Extra fields as a JSON object")
    p_ls_append.add_argument(
        "--artifact-id",
        action="append",
        default=None,
        help="Reference an already-stored artifact (repeatable)",
    )
    p_ls_append.add_argument("--json", action="store_true", help="Machine-readable output")

    p_ls_list = logstream_sub.add_parser("list", help="List events")
    _add_logstream_filters(p_ls_list)
    p_ls_list.add_argument(
        "--before-event-id",
        default=None,
        help="Only events strictly before this id in append order",
    )
    p_ls_list.add_argument(
        "--order",
        choices=["asc", "desc"],
        default="asc",
        help="asc (oldest first, default) or desc (newest first)",
    )
    p_ls_list.add_argument("--limit", type=int, default=50, help="Max events (default 50)")
    p_ls_list.add_argument("--json", action="store_true", help="Machine-readable output")

    p_ls_wait = logstream_sub.add_parser(
        "wait", help="Block until a matching event exists (exit 2 on timeout)"
    )
    _add_logstream_filters(p_ls_wait)
    p_ls_wait.add_argument(
        "--timeout-ms",
        type=int,
        default=60000,
        help="How long to wait in ms (default 60000, max 300000)",
    )
    p_ls_wait.add_argument(
        "--limit", type=int, default=50, help="Max events to return on match (default 50)"
    )
    p_ls_wait.add_argument("--json", action="store_true", help="Machine-readable output")

    p_ls_watch = logstream_sub.add_parser(
        "watch",
        help="Background watcher: block until interesting events arrive, then wake (exit 2 on idle)",
    )
    p_ls_watch.add_argument(
        "--agent",
        default=None,
        help=(
            "Your identity. Shorthand for --to-agent <id> --exclude-from-agent <id>: "
            "wake for what is addressed to you (broadcasts included) but never for "
            "your own events"
        ),
    )
    p_ls_watch.add_argument(
        "--stream", action="append", default=None, help="Stream (repeatable; matches any)"
    )
    p_ls_watch.add_argument(
        "--room", action="append", default=None, help="Room (repeatable; matches any)"
    )
    p_ls_watch.add_argument(
        "--topic", action="append", default=None, help="Topic (repeatable; matches any)"
    )
    p_ls_watch.add_argument(
        "--type", action="append", default=None, help="Event type (repeatable; matches any)"
    )
    p_ls_watch.add_argument(
        "--status", action="append", default=None, help="Status (repeatable; matches any)"
    )
    p_ls_watch.add_argument(
        "--to-agent", action="append", default=None, help="Target agent (repeatable; '*' matches)"
    )
    p_ls_watch.add_argument(
        "--from-agent", action="append", default=None, help="Writer agent (repeatable)"
    )
    p_ls_watch.add_argument(
        "--exclude-from-agent",
        action="append",
        default=None,
        help="Never wake for events written by this agent (repeatable)",
    )
    p_ls_watch.add_argument(
        "--correlation-id", action="append", default=None, help="Correlation id (repeatable)"
    )
    p_ls_watch.add_argument(
        "--since-event-id",
        default=None,
        help="Start strictly after this event id (overrides --state-file)",
    )
    p_ls_watch.add_argument(
        "--state-file",
        default=None,
        help="Persist the cursor here so a restart resumes exactly where it stopped",
    )
    p_ls_watch.add_argument(
        "--from-start",
        action="store_true",
        help=(
            "Replay the log from the beginning when there is no cursor "
            "(default: start at the tip, like the SSE live-tail)"
        ),
    )
    p_ls_watch.add_argument(
        "--follow",
        action="store_true",
        help="Keep watching after a match instead of exiting on the first one",
    )
    p_ls_watch.add_argument(
        "--idle-exit-ms",
        type=int,
        default=0,
        help="Give up after this long with no match (0 = wait forever)",
    )
    p_ls_watch.add_argument(
        "--poll-timeout-ms",
        type=int,
        default=300000,
        help="Long-poll length per iteration (default 300000, the server maximum)",
    )
    p_ls_watch.add_argument(
        "--limit", type=int, default=50, help="Max events per poll (default 50)"
    )
    p_ls_watch.add_argument("--json", action="store_true", help="Machine-readable output")

    p_ls_ack = logstream_sub.add_parser(
        "ack", help="Acknowledge an event (appends event.ack, never mutates)"
    )
    p_ls_ack.add_argument("event_id", help="Event id to acknowledge")
    p_ls_ack.add_argument("--from-agent", required=True, help="Acknowledging agent identity")
    p_ls_ack.add_argument(
        "--status",
        default=None,
        help="open|claimed|ready|applied|blocked|failed|superseded",
    )
    p_ls_ack.add_argument(
        "--topic", default=None, help="Topic override (defaults to target event's topic)"
    )
    p_ls_ack.add_argument("--body", default=None, help="Verbatim ack notes")
    p_ls_ack.add_argument("--json", action="store_true", help="Machine-readable output")

    p_ls_sync = logstream_sub.add_parser(
        "sync", help="Pull missing events/artifacts from peer replicas (RFC 004)"
    )
    p_ls_sync.add_argument(
        "--peer", default=None, help="Peer base URL (default: all peers in peers.json)"
    )
    p_ls_sync.add_argument("--token", default=None, help="Bearer token for --peer")
    p_ls_sync.add_argument("--json", action="store_true", help="Machine-readable output")

    # task — guided logstream task lifecycle
    p_task = sub.add_parser("task", help="Create or run complete agent tasks over the logstream")
    task_sub = p_task.add_subparsers(dest="task_action")

    p_task_create = task_sub.add_parser(
        "create", help="Create a canonical task.request and print a pasteable handoff"
    )
    p_task_create.add_argument("--project", required=True, help="Project name for routing")
    p_task_create.add_argument("--from-agent", required=True, help="Requesting agent identity")
    p_task_create.add_argument("--to-agent", required=True, help="Worker agent identity")
    task_goal = p_task_create.add_mutually_exclusive_group(required=True)
    task_goal.add_argument("--goal", default=None, help="Verbatim task goal")
    task_goal.add_argument(
        "--goal-file", default=None, help="Read the task goal from a file ('-' for stdin)"
    )
    p_task_create.add_argument("--branch", required=True, help="Git branch for the work")
    p_task_create.add_argument(
        "--base-commit",
        required=True,
        help="Immutable hexadecimal commit id the worker must start from (not a branch or tag)",
    )
    task_done = p_task_create.add_mutually_exclusive_group(required=True)
    task_done.add_argument("--done", default=None, help="Verbatim definition of done")
    task_done.add_argument(
        "--done-file",
        default=None,
        help="Read the definition of done from a file ('-' for stdin)",
    )
    p_task_create.add_argument("--json", action="store_true", help="Machine-readable output")

    p_task_launch = task_sub.add_parser(
        "launch", help="Run an existing task through a supported headless coding agent"
    )
    task_source = p_task_launch.add_mutually_exclusive_group(required=True)
    task_source.add_argument("correlation_id", nargs="?", help="Task correlation id")
    task_source.add_argument(
        "--task-file",
        default=None,
        help="Exact task.request event JSON fetched through remote MCP",
    )
    p_task_launch.add_argument(
        "--runner",
        required=True,
        choices=tuple(_TASK_RUNNER_ADAPTERS),
        help="Headless agent runner",
    )
    p_task_launch.add_argument("--workspace", required=True, help="Trusted workspace directory")
    p_task_launch.add_argument(
        "--agent",
        default=None,
        help="Worker identity (required for broadcasts; must match addressed tasks)",
    )
    p_task_launch.add_argument("--json", action="store_true", help="Machine-readable errors")

    # artifact (RFC 003 exact content exchange)
    p_artifact = sub.add_parser(
        "artifact", help="Exact artifact exchange for agent handoffs (RFC 003)"
    )
    artifact_sub = p_artifact.add_subparsers(dest="artifact_action")

    p_art_put = artifact_sub.add_parser("put", help="Store exact artifact content")
    p_art_put.add_argument("--kind", required=True, help="patch|file|log|json|note")
    p_art_put.add_argument("--created-by", required=True, help="Writer agent identity")
    p_art_put.add_argument("--content", default=None, help="Inline content")
    p_art_put.add_argument(
        "--file", default=None, help="Read content from file ('-' for stdin; default stdin)"
    )
    p_art_put.add_argument("--metadata", default=None, help="Extra fields as a JSON object")
    p_art_put.add_argument("--json", action="store_true", help="Machine-readable output")

    p_art_get = artifact_sub.add_parser(
        "get", help="Fetch exact artifact content (stdout pipes into git apply)"
    )
    p_art_get.add_argument("artifact_id", help="Artifact id")
    p_art_get.add_argument("--out", default=None, help="Write content to this file instead")
    p_art_get.add_argument(
        "--json", action="store_true", help="Metadata as JSON (content omitted with --out)"
    )

    p_palace = sub.add_parser("palace", help="Palace maintenance commands")
    palace_sub = p_palace.add_subparsers(dest="palace_action")
    p_set_embedder = palace_sub.add_parser(
        "set-embedder",
        help="Record/override the palace's embedder identity (resolve 'unknown', or switch models)",
    )
    p_set_embedder.add_argument(
        "--model",
        default=None,
        help="Embedder model to record (default: current configured model). "
        "Records identity on the palace only; does not change the configured "
        "model (prints how to align MEMPALACE_EMBEDDING_MODEL if they differ).",
    )
    p_set_embedder.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing identity that names a different model "
        "(only if you know the stored vectors are compatible)",
    )
    p_set_embedder.add_argument(
        "--backend",
        default=None,
        help="Storage backend (default: config/env/detected/chroma)",
    )

    args = parser.parse_args()
    _apply_backend_arg(args)

    if not args.command:
        parser.print_help()
        return

    # Handle two-level subcommands
    if args.command == "hook":
        if not getattr(args, "hook_action", None):
            p_hook.print_help()
            return
        cmd_hook(args)
        return

    if args.command == "instructions":
        name = getattr(args, "instructions_name", None)
        if not name:
            p_instructions.print_help()
            return
        args.name = name
        cmd_instructions(args)
        return

    if args.command == "palace":
        if getattr(args, "palace_action", None) == "set-embedder":
            cmd_palace_set_embedder(args)
        else:
            p_palace.print_help()
        return

    if args.command == "logstream":
        if not getattr(args, "logstream_action", None):
            p_logstream.print_help()
            return
        cmd_logstream(args)
        return

    if args.command == "task":
        if not getattr(args, "task_action", None):
            p_task.print_help()
            return
        cmd_task(args)
        return

    if args.command == "artifact":
        if not getattr(args, "artifact_action", None):
            p_artifact.print_help()
            return
        cmd_artifact(args)
        return

    if args.command == "daemon":
        if not getattr(args, "daemon_action", None):
            p_daemon.print_help()
            return
        cmd_daemon(args)
        return

    dispatch = {
        "init": cmd_init,
        "rules": cmd_rules,
        "mine": cmd_mine,
        "split": cmd_split,
        "search": cmd_search,
        "sweep": cmd_sweep,
        "sync": cmd_sync,
        "mcp": cmd_mcp,
        "serve": cmd_serve,
        "compress": cmd_compress,
        "wake-up": cmd_wakeup,
        "repair": cmd_repair,
        "repair-status": cmd_repair_status,
        "migrate": cmd_migrate,
        "migrate-wings": cmd_migrate_wings,
        "hallways": cmd_hallways,
        "status": cmd_status,
        "update": cmd_update,
    }
    dispatch[args.command](args)
