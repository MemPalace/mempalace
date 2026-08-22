#!/usr/bin/env python3
"""
MemPalace — Give your AI a memory. No API key required.

Three ways to ingest:
  Projects:      mempalace mine ~/projects/my_app                  (code, docs, notes)
  Conversations: mempalace mine <convo-dir> --mode convos          (Claude Code, Claude.ai, ChatGPT, Slack exports)
  Documents:     mempalace mine <docs-dir> --mode extract          (PDF, DOCX, PPTX, XLSX, RTF, EPUB — requires mempalace[extract])
  Adapters:      mempalace mine <source> --source <adapter-name>  (registered source adapters)

Same palace. Same search. Different ingest strategies.

Commands:
    mempalace init <dir>                  Detect rooms from folder structure
    mempalace split <dir>                 Split concatenated mega-files into per-session files
    mempalace mine <dir>                  Mine project files (default)
    mempalace mine <dir> --mode convos    Mine conversation exports
    mempalace mine <dir> --mode extract   Mine binary office documents (PDF/DOCX/etc.)
    mempalace mine <source> --source NAME Mine through a registered source adapter
    mempalace search "query"              Find anything, exact words
    mempalace mcp                         Show MCP setup command
    mempalace wake-up                     Show L0 + L1 wake-up context
    mempalace wake-up --wing my_app       Wake-up for a specific project
    mempalace status                      Show what's been filed

Examples:
    mempalace init ~/projects/my_app
    mempalace mine ~/projects/my_app
    mempalace mine ~/.claude/projects/-Users-you-Projects-my_app --mode convos --wing my_app
    mempalace search "why did we switch to GraphQL"
    mempalace search "pricing discussion" --wing my_app --room costs
"""

import argparse
import contextlib
import os
import shlex
import sys
import warnings
from pathlib import Path

from .config import MempalaceConfig
from .corpus_origin import detect_origin_heuristic, detect_origin_llm
from .llm_client import LLMError, get_provider
from .version import __version__


_MEMPALACE_PROJECT_FILES = ("mempalace.yaml", "entities.json")

# Pass 0 corpus-origin sampling caps. Tier 1 reads FULL file content (no
# front-bias sampling) but bounds total memory on enormous corpora. Tier 2
# trims to a smaller view because LLM context windows are finite.
_PASS_ZERO_MAX_FILES = 30
_PASS_ZERO_PER_FILE_CAP = 100_000  # 100KB per file is generous for prose
_PASS_ZERO_TOTAL_CAP = 5_000_000  # 5MB total ceiling — bounds memory
_PASS_ZERO_LLM_PER_SAMPLE = 2_000  # for Tier 2 LLM call only
_PASS_ZERO_LLM_MAX_SAMPLES = 20  # caps the LLM-tier sample count
_EXPLICIT_BACKEND_ENV = "MEMPALACE_BACKEND_EXPLICIT"

# Keep parser construction lightweight for --version and hook commands.
# This mirrors miner.MAX_CHUNKS_PER_FILE without importing miner here;
# importing miner pulls in Chroma dependencies before argparse can handle
# lightweight exits such as --version.
_CLI_MAX_CHUNKS_PER_FILE_DEFAULT = 50_000


def _backend_arg(args):
    """Return a CLI-selected backend from subcommand or global flags."""
    return getattr(args, "backend", None) or getattr(args, "global_backend", None)


def _apply_backend_arg(args) -> None:
    backend = _backend_arg(args)
    if not backend:
        return
    backend = str(backend).strip().lower()
    from .backends import get_backend_class

    get_backend_class(backend)
    os.environ[_EXPLICIT_BACKEND_ENV] = backend
    os.environ["MEMPALACE_BACKEND"] = backend


def _selected_backend_for_palace(palace_path: str) -> str:
    from .palace import resolve_backend_name

    return resolve_backend_name(palace_path, explicit=os.environ.get(_EXPLICIT_BACKEND_ENV))


def _maintenance_requires_chroma(palace_path: str, command_name: str) -> bool:
    try:
        backend_name = _selected_backend_for_palace(palace_path)
    except Exception as exc:  # noqa: BLE001 - user-facing guard before maintenance imports
        print(f"\n  {command_name} cannot resolve the palace backend: {exc}", file=sys.stderr)
        return False
    if backend_name == "chroma":
        return True
    print(
        f"\n  {command_name} is Chroma-only in this release (selected backend: {backend_name}).",
        file=sys.stderr,
    )
    return False


def _gather_origin_samples(project_dir) -> list:
    """Collect Tier-1 samples for corpus-origin detection.

    Reads FULL file content (capped at ``_PASS_ZERO_PER_FILE_CAP`` per file
    and ``_PASS_ZERO_TOTAL_CAP`` overall). No front-bias sampling — AI
    signal that lives past the first N chars of a file must still trip
    detection, so we read the whole file up to the cap.

    Skips mempalace's own per-project artifacts (``entities.json``,
    ``mempalace.yaml``) so a re-run of ``mempalace init`` produces the
    same classification result it did on the first run. Without this
    filter, the first run writes entities.json into the corpus, the
    second run picks it up as a sample, and the Tier-1 density math
    drifts (different total_chars). That makes init non-idempotent.

    Returns a list of strings (one per readable file). Empty list when
    the project has no readable text.
    """
    from .entity_detector import scan_for_detection

    files = scan_for_detection(project_dir, max_files=_PASS_ZERO_MAX_FILES)
    samples: list = []
    total_chars = 0
    for filepath in files:
        if filepath.name in _MEMPALACE_PROJECT_FILES:
            continue
        if total_chars >= _PASS_ZERO_TOTAL_CAP:
            break
        try:
            # ``scan_for_detection`` picks candidates by extension, so a FIFO
            # named ``notes.md`` reaches this loop; opening one for reading
            # blocks until a writer appears. ``is_file()`` stats instead.
            # It belongs inside the try: it raises PermissionError on an
            # unreadable directory, which the open below used to absorb.
            if not filepath.is_file():
                continue
            with open(filepath, encoding="utf-8", errors="replace") as f:
                content = f.read(_PASS_ZERO_PER_FILE_CAP)
        except OSError:
            continue
        if not content:
            continue
        samples.append(content)
        total_chars += len(content)
    return samples


def _trim_samples_for_llm(samples: list) -> list:
    """Reduce Tier-1 full-content samples to LLM-friendly size.

    Tier 2 hits an LLM with a finite context window — we trim each sample
    to ``_PASS_ZERO_LLM_PER_SAMPLE`` chars and cap the overall sample
    count at ``_PASS_ZERO_LLM_MAX_SAMPLES``.
    """
    return [s[:_PASS_ZERO_LLM_PER_SAMPLE] for s in samples[:_PASS_ZERO_LLM_MAX_SAMPLES]]


def _run_pass_zero(project_dir, palace_dir, llm_provider) -> dict:
    """Pass 0: detect whether the corpus is AI-dialogue and persist the
    result to ``<palace>/.mempalace/origin.json``.

    Returns the wrapped result dict (same shape as origin.json) on success,
    or ``None`` when there are no readable samples to detect from. The
    return value is what cmd_init forwards to ``discover_entities`` via
    the ``corpus_origin`` kwarg.

    File-write failures (e.g. read-only palace) are caught and reported on
    stderr; init never blocks on them.
    """
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    samples = _gather_origin_samples(project_dir)
    if not samples:
        print("  Skipping corpus-origin detection -- no readable samples.")
        return None

    # Tier 1 — always runs. Cheap regex grep, no API.
    result = detect_origin_heuristic(samples)

    # Tier 2 — runs only when an LLM provider is available. The provider
    # contract is best-effort: corpus_origin internally falls back to a
    # conservative default on transport/parse failure, so we don't need a
    # try/except here, but we still keep one for any unforeseen exception.
    #
    # MERGE-FIELDS, NOT REPLACE: Tier 2's persona/user/platform extraction
    # is the whole reason to run it, but a weak local model (e.g. Ollama
    # gemma4:e4b) can return a wrong likely_ai_dialogue/confidence call
    # that overrides a confident heuristic answer. Per @igorls's review of
    # PR #1211: keep the heuristic's likely_ai_dialogue + confidence
    # (don't let a weak LLM flip a confident regex answer), and merge in
    # LLM's persona-related fields + combined evidence.
    if llm_provider is not None:
        try:
            llm_result = detect_origin_llm(_trim_samples_for_llm(samples), llm_provider)
            # Heuristic owns: likely_ai_dialogue, confidence (do NOT touch).
            # LLM contributes: primary_platform, user_name, agent_persona_names
            # (heuristic doesn't extract any of these).
            if llm_result.primary_platform:
                result.primary_platform = llm_result.primary_platform
            if llm_result.user_name:
                result.user_name = llm_result.user_name
            if llm_result.agent_persona_names:
                result.agent_persona_names = list(llm_result.agent_persona_names)
            # Combine evidence — keep both signal trails for the audit record,
            # prefixed so the on-disk origin.json says which tier produced
            # each entry. Idempotent: re-prefixing an already-tagged entry
            # is a no-op.
            tier1_prefix = "Tier-1 heuristic: "
            tier2_prefix = "Tier-2 LLM: "
            heuristic_evidence = [
                s if s.startswith(tier1_prefix) else f"{tier1_prefix}{s}"
                for s in (str(e) for e in result.evidence)
            ]
            llm_evidence = [
                s if s.startswith(tier2_prefix) else f"{tier2_prefix}{s}"
                for s in (str(e) for e in llm_result.evidence)
            ]
            result.evidence = heuristic_evidence + llm_evidence
        except Exception as exc:  # noqa: BLE001 — never block init on LLM failure
            print(f"  LLM corpus-origin tier failed ({exc}); using heuristic only.")

    wrapped = {
        "schema_version": 1,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "result": result.to_dict(),
    }

    origin_path = Path(palace_dir).expanduser() / ".mempalace" / "origin.json"
    try:
        origin_path.parent.mkdir(parents=True, exist_ok=True)
        with open(origin_path, "w", encoding="utf-8") as f:
            json.dump(wrapped, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        print(f"  Could not write {origin_path}: {exc}", file=sys.stderr)
        # Return the wrapped dict anyway so the in-memory pipeline still
        # benefits from the detection result this run.
        return wrapped

    # Banner — one line, two-space indent matching existing init style.
    res = result
    if res.likely_ai_dialogue:
        platform = res.primary_platform or "AI dialogue (platform unidentified)"
        user = res.user_name or "—"
        agents = ", ".join(res.agent_persona_names) if res.agent_persona_names else "—"
        print(f"  Detected: {platform} (user: {user}, agents: {agents})")
    else:
        print(f"  Corpus origin: not AI-dialogue (confidence: {res.confidence:.2f})")

    return wrapped


def _ensure_mempalace_files_gitignored(project_dir) -> bool:
    """If project_dir is a git repo, ensure MemPalace's per-project files
    are listed in .gitignore so they don't get committed by accident.

    Returns True if .gitignore was updated, False otherwise. Issue #185:
    `mempalace init` writes mempalace.yaml + entities.json into the
    project root, where they previously had no protection against being
    staged into git.
    """
    from pathlib import Path

    project_path = Path(project_dir).expanduser().resolve()
    if not (project_path / ".git").exists():
        return False
    gitignore = project_path / ".gitignore"
    # ``exists()`` is true for a FIFO, and both the read below and the append
    # at the end of this function would block in the kernel on one. Decide by
    # type instead: an absent file still yields "" as before, a regular one
    # is read, and anything else is left untouched.
    if gitignore.exists() and not gitignore.is_file():
        return False
    # Force UTF-8: Windows defaults to GBK and chokes on non-ASCII .gitignore
    # comments, killing auto-init even though the file is valid UTF-8.
    existing = (
        gitignore.read_text(encoding="utf-8", errors="replace") if gitignore.is_file() else ""
    )
    existing_lines = {line.strip() for line in existing.splitlines()}
    missing = [p for p in _MEMPALACE_PROJECT_FILES if p not in existing_lines]
    if not missing:
        return False
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    block = prefix + "\n# MemPalace per-project files (issue #185)\n" + "\n".join(missing) + "\n"
    with open(gitignore, "a", encoding="utf-8") as f:
        f.write(block)
    print(f"  Added {', '.join(missing)} to {gitignore.name}")
    return True


def cmd_init(args):
    import json
    from pathlib import Path
    from .entity_detector import confirm_entities
    from .project_scanner import discover_entities
    from .room_detector_local import detect_rooms_local

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

            from .config import normalize_wing_name
            from .miner import add_to_known_entities

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
    from .miner import mine, scan_project

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


_HUB_FORWARD_ENV = "MEMPALACE_HUB_FORWARD"
_HUB_HEALTH_TIMEOUT_S = 0.75
# A backfill mine over a large transcript tree can legitimately run for many
# minutes inside the hub; the forwarder is a background/CLI process, not a
# hook-budgeted one, so it waits.
_HUB_MINE_TIMEOUT_S = 3600.0


def _hub_forward_disabled() -> bool:
    return os.environ.get(_HUB_FORWARD_ENV, "").strip().lower() in {"0", "false", "no", "off"}


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

    from . import server_registry

    if _hub_forward_disabled():
        return False
    info = server_registry.read_live_serverinfo(palace_path)
    if not info or info.get("read_only"):
        return False

    base_url = server_registry.client_base_url(info)
    headers = {"Content-Type": "application/json"}
    token = server_registry.load_server_token(palace_path)
    if token:
        headers["Authorization"] = f"Bearer {token}"

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
        request = urllib.request.Request(f"{base_url}/mcp", data=body, headers=headers)
        with urllib.request.urlopen(request, timeout=_HUB_MINE_TIMEOUT_S) as resp:
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


def cmd_mine(args):
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    mode = getattr(args, "mode", None) or "projects"
    source_adapter = getattr(args, "source", None)
    include_ignored = []
    for raw in args.include_ignored or []:
        include_ignored.extend(part.strip() for part in raw.split(",") if part.strip())

    if getattr(args, "background", False) and not getattr(args, "daemon", False):
        print("mempalace: --background requires --daemon", file=sys.stderr)
        sys.exit(2)

    if getattr(args, "daemon", False):
        payload = {
            "source": args.dir,
            "mode": mode,
            "wing": args.wing,
            "agent": args.agent,
            "limit": args.limit,
            "dry_run": args.dry_run,
            "extract": args.extract,
            "no_gitignore": args.no_gitignore,
            "include_ignored": include_ignored,
            "max_chunks_per_file": getattr(args, "max_chunks_per_file", None),
            "redetect_origin": getattr(args, "redetect_origin", False),
        }
        if source_adapter:
            payload["source_adapter"] = source_adapter
        _submit_daemon_cli_job("mine", payload, args, background=getattr(args, "background", False))
        return

    from .palace import MineAlreadyRunning, MineValidationError

    if source_adapter:
        try:
            drawers_written = mine_source_adapter(
                source_name=source_adapter,
                source_path=args.dir,
                palace_path=palace_path,
                dry_run=args.dry_run,
            )
        except (UnknownSourceAdapterError, UnsupportedSourceAdapterProtocolError) as exc:
            print(f"mempalace: {exc}", file=sys.stderr)
            sys.exit(2)
        except MineAlreadyRunning as exc:
            print(f"mempalace: {exc}", file=sys.stderr)
            sys.exit(1)
        suffix = " would be written" if args.dry_run else " written"
        print(f"  Source adapter {source_adapter!r}: {drawers_written} drawer(s){suffix}.")
        return

    # A live HTTP hub for this palace holds the MCP writer lease, so a
    # direct mine here would be refused. Hand the job to the hub instead —
    # this is how the save hooks keep capturing transcripts on a machine
    # that runs `mempalace serve`.
    if _mine_args_forwardable(args, include_ignored) and _forward_mine_to_hub(args, palace_path):
        return

    # --redetect-origin re-runs corpus_origin on the current corpus state
    # and overwrites <palace>/.mempalace/origin.json before mining proceeds.
    # Heuristic-only by design — full LLM detection lives on `mempalace init`.
    if getattr(args, "redetect_origin", False):
        _run_pass_zero(
            project_dir=args.dir,
            palace_dir=palace_path,
            llm_provider=None,
        )

    try:
        if mode == "convos":
            from .convo_miner import mine_convos

            mine_convos(
                convo_dir=args.dir,
                palace_path=palace_path,
                wing=args.wing,
                agent=args.agent,
                limit=args.limit,
                dry_run=args.dry_run,
                extract_mode=args.extract,
                include_subagents=getattr(args, "include_subagents", False),
            )
        elif mode == "extract":
            from .format_miner import mine_formats

            mine_formats(
                format_dir=args.dir,
                palace_path=palace_path,
                wing=args.wing,
                agent=args.agent,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        else:
            from .miner import mine

            mine(
                project_dir=args.dir,
                palace_path=palace_path,
                wing_override=args.wing,
                agent=args.agent,
                limit=args.limit,
                dry_run=args.dry_run,
                respect_gitignore=not args.no_gitignore,
                include_ignored=include_ignored,
                max_chunks_per_file=getattr(args, "max_chunks_per_file", None),
            )
    except MineAlreadyRunning as exc:
        # A live MCP server or another mine is already writing to this
        # palace. Surface the holder identity so the operator knows what
        # to wait for (or stop), and exit non-zero so wrappers like
        # nohup / scripts can detect the contention.
        print(f"mempalace: {exc}", file=sys.stderr)
        sys.exit(1)
    except MineValidationError as exc:
        # PRAGMA quick_check on chroma.sqlite3 returned errors at end of mine.
        # The corruption may pre-date the mine; we surface it here so automation
        # cannot proceed against a half-broken palace. Reuse cmd_repair's
        # recovery banner so the operator sees one consistent message regardless
        # of which command surfaces it.
        from .repair import print_sqlite_integrity_abort

        print_sqlite_integrity_abort(exc.palace_path, exc.errors)
        print(
            "\n  PRAGMA quick_check after this mine reported errors (the corruption\n"
            "  may pre-date the mine itself). Drawers may still be intact for direct\n"
            "  lookup; wing-filtered or full-text search will fail until the FTS5\n"
            "  index is rebuilt. `mempalace repair --yes` rebuilds the FTS5 virtual\n"
            "  table automatically (step 6 of the recovery above).",
            file=sys.stderr,
        )
        sys.exit(1)


class UnknownSourceAdapterError(ValueError):
    """Raised when an explicit ``--source`` name is absent from the registry."""


class UnsupportedSourceAdapterProtocolError(ValueError):
    """Raised when an adapter requires runner semantics not implemented yet."""


class _DryRunCollectionProxy:
    """Empty collection facade that records, but never persists, writes.

    Source adapters are deliberately allowed to access ``drawer_collection``
    directly.  A dry run must not open the real backend: even read-only-looking
    opens can create or repair backend artifacts (for example SQLite WAL files).
    """

    def __init__(self):
        self.operations = []

    def add(self, **kwargs):
        self.operations.append(("add", kwargs))

    def upsert(self, **kwargs):
        self.operations.append(("upsert", kwargs))

    def delete(self, **kwargs):
        self.operations.append(("delete", kwargs))

    def update(self, **kwargs):
        self.operations.append(("update", kwargs))

    def query(self, **kwargs):
        from .backends import QueryResult

        query_input = kwargs.get("query_texts", kwargs.get("query_embeddings"))
        num_queries = len(query_input) if isinstance(query_input, (list, tuple)) else 1
        include = kwargs.get("include") or []
        return QueryResult.empty(
            num_queries=num_queries,
            embeddings_requested="embeddings" in include,
        )

    def get(self, **kwargs):
        from .backends import GetResult

        return GetResult.empty()

    def count(self):
        return 0


class _DryRunKnowledgeGraphProxy:
    """Recording no-op facade for the KG mutation surface published to adapters."""

    def __init__(self):
        self.operations = []

    def add_entity(self, *args, **kwargs):
        self.operations.append(("add_entity", args, kwargs))

    def add_triple(self, *args, **kwargs):
        self.operations.append(("add_triple", args, kwargs))

    def invalidate(self, *args, **kwargs):
        self.operations.append(("invalidate", args, kwargs))

    def supersede(self, *args, **kwargs):
        self.operations.append(("supersede", args, kwargs))


def mine_source_adapter(
    *,
    source_name: str,
    source_path: str,
    palace_path: str,
    dry_run: bool = False,
) -> int:
    """Run an explicitly selected RFC 002 source adapter through ``PalaceContext``.

    This deliberately sits alongside, rather than inside, the legacy mode
    miners.  Until those miners are migrated to first-party adapters, no-flag
    and ``--mode`` calls must retain their established dispatch paths.
    """
    from .knowledge_graph import KnowledgeGraph
    from .palace import get_collection, mine_palace_lock
    from .sources import (
        DrawerRecord,
        PalaceContext,
        SourceRef,
        SourceItemMetadata,
        get_adapter,
        resolve_adapter_for_source,
    )

    adapter_name = resolve_adapter_for_source(explicit=source_name)
    try:
        adapter = get_adapter(adapter_name)
    except KeyError as exc:
        raise UnknownSourceAdapterError(
            f"unknown source adapter {adapter_name!r}; install its adapter package or "
            "check the adapter name with `mempalace mine --help`"
        ) from exc

    if "supports_incremental" in adapter.capabilities:
        raise UnsupportedSourceAdapterProtocolError(
            f"source adapter {adapter_name!r} requires incremental ingestion, which "
            "mempalace mine does not support yet"
        )

    # A dry run must never open a collection: backend opens can create or
    # repair storage even when requested as read-only.  Non-dry runs hold one
    # writer lease from handle creation through adapter iteration, including
    # direct KG mutations by adapters.
    lock = mine_palace_lock(palace_path) if not dry_run else contextlib.nullcontext()
    with lock:
        knowledge_graph = None
        try:
            if dry_run:
                drawer_collection = _DryRunCollectionProxy()
                knowledge_graph = _DryRunKnowledgeGraphProxy()
            else:
                drawer_collection = get_collection(palace_path)
                knowledge_graph = KnowledgeGraph(
                    db_path=os.path.join(palace_path, "knowledge_graph.sqlite3")
                )
            context = PalaceContext(
                drawer_collection=drawer_collection,
                knowledge_graph=knowledge_graph,
                palace_path=palace_path,
                config=MempalaceConfig(palace_path=palace_path),
                adapter_name=adapter.name,
                adapter_version=adapter.adapter_version,
            )
            drawers_written = 0
            for result in adapter.ingest(
                source=SourceRef(local_path=source_path),
                palace=context,
            ):
                if isinstance(result, SourceItemMetadata):
                    # Non-incremental adapters may report a cursor or version
                    # while still doing a complete re-extract.  Incremental
                    # adapters are rejected before ingest above, so accepting
                    # this avoids a late partial-ingest failure.
                    warnings.warn(
                        f"Source adapter {adapter_name!r} yielded non-incremental item "
                        "metadata; ignoring it during complete ingest",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                    continue
                if isinstance(result, DrawerRecord):
                    drawers_written += 1
                    context.upsert_drawer(result)
                    continue
                raise TypeError(
                    f"source adapter {adapter_name!r} yielded unsupported result type "
                    f"{type(result).__name__}"
                )
            return drawers_written
        finally:
            if knowledge_graph is not None and hasattr(knowledge_graph, "close"):
                knowledge_graph.close()


def cmd_sweep(args):
    """Sweep a transcript file or directory.

    The sweeper deduplicates against its own prior writes via
    deterministic drawer IDs + a timestamp cursor. It does NOT currently
    coordinate with the file-level miners (miner.py / convo_miner.py) —
    those produce char-chunked drawers without compatible message
    metadata, so running both miners may store overlapping content under
    different IDs.
    """
    from .sweeper import sweep, sweep_directory

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    target = os.path.expanduser(args.target)

    if os.path.isfile(target):
        result = sweep(target, palace_path)
        print(
            f"  Swept {target}: +{result['drawers_added']} new, "
            f"{result['drawers_already_present']} already present, "
            f"{result['drawers_skipped']} skipped (< cursor)."
        )
    elif os.path.isdir(target):
        result = sweep_directory(target, palace_path)
        print(
            f"  Swept {result['files_succeeded']}/{result['files_attempted']} "
            f"files from {target}: +{result['drawers_added']} new, "
            f"{result['drawers_already_present']} already present, "
            f"{result['drawers_skipped']} skipped (< cursor)."
        )
        failures = result.get("failures") or []
        if failures:
            print(
                f"  WARNING: {len(failures)} file(s) failed to sweep - see stderr / logs for details.",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        print(f"  ERROR: Not a file or directory: {target}", file=sys.stderr)
        sys.exit(1)


def cmd_sync(args):
    """Prune drawers whose source files are gitignored, deleted, or moved (#1252)."""
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path

    if getattr(args, "background", False) and not getattr(args, "daemon", False):
        print("mempalace: --background requires --daemon", file=sys.stderr)
        sys.exit(2)

    if getattr(args, "daemon", False):
        payload = {
            "dir": args.dir,
            "root": list(args.root or []),
            "wing": args.wing,
            "dry_run": args.dry_run,
        }
        _submit_daemon_cli_job("sync", payload, args, background=getattr(args, "background", False))
        return

    from .palace import MineAlreadyRunning
    from .wal import _wal_log
    from .backends import detect_backend_for_path
    from .palace import _backend_artifact_label, resolve_backend_name
    from .sync import sync_palace

    if not os.path.isdir(palace_path):
        print(f"\n  No palace found at {palace_path}")
        return
    try:
        backend_name = resolve_backend_name(palace_path)
    except Exception as exc:  # noqa: BLE001 - user-facing CLI guard
        print(f"\n  Could not resolve palace backend: {exc}", file=sys.stderr)
        return
    if detect_backend_for_path(palace_path) is None:
        print(
            f"\n  Palace dir at {palace_path} exists but has no "
            f"{_backend_artifact_label(backend_name)} yet."
        )
        print("  Run: mempalace mine <dir>")
        return

    project_dirs = []
    if args.dir:
        project_dirs.append(os.path.expanduser(args.dir))
    project_dirs.extend(os.path.expanduser(r) for r in args.root)
    project_dirs = project_dirs or None

    print(f"\n{'=' * 55}")
    print("  MemPalace Sync -- Gitignore-aware drawer prune")
    print(f"{'=' * 55}")
    print(f"  Palace:   {palace_path}")
    if args.wing:
        print(f"  Wing:     {args.wing}")
    if project_dirs:
        for p in project_dirs:
            print(f"  Project:  {p}")
    if args.dry_run:
        print("  Mode:     DRY RUN (no deletions)")
    else:
        print("  Mode:     APPLY (deleting drawers)")
    print(f"{'-' * 55}\n")

    try:
        report = sync_palace(
            palace_path=palace_path,
            project_dirs=project_dirs,
            wing=args.wing,
            dry_run=args.dry_run,
            wal_log=_wal_log,
        )
    except MineAlreadyRunning as exc:
        print(f"mempalace: {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"mempalace: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"mempalace: sync failed: {exc}", file=sys.stderr)
        sys.exit(1)

    removed_suffix = "(would remove)" if args.dry_run else "(removed)"
    print(f"  Scanned:        {report['scanned']}")
    print(f"  Kept:           {report['kept']}")
    print(f"  Gitignored:     {report['gitignored']}  {removed_suffix}")
    print(f"  Missing:        {report['missing']}  {removed_suffix}")
    print(f"  No source:      {report['no_source']}  (kept)")
    print(f"  Out of scope:   {report['out_of_scope']}  (kept)")

    by_source = report.get("by_source") or {}
    if by_source:
        top = sorted(by_source.items(), key=lambda kv: -kv[1])[:5]
        label = "Top sources to remove" if args.dry_run else "Top sources removed"
        print(f"\n  {label}:")
        for src, n in top:
            print(f"    {src}  ({n})")

    if args.dry_run:
        if report["gitignored"] + report["missing"] > 0:
            print("\n  Re-run with --apply to commit these deletions.")
    else:
        print(
            f"\n  Removed {report['removed_drawers']} drawers, {report['removed_closets']} closets."
        )

    print(f"\n{'=' * 55}\n")


def _submit_daemon_cli_job(kind: str, payload: dict, args, *, background: bool) -> None:
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    backend = _backend_arg(args)
    from .daemon import DaemonError, submit_job

    try:
        job = submit_job(
            kind,
            payload,
            palace_path=palace_path,
            backend=backend,
            wait=not background,
            auto_start=True,
            # A job refused the palace lock is deferred, not failed (#2014), so
            # it never becomes terminal while the holder lives. Waiting it out
            # would strand this terminal behind a peer that can outlive the
            # default hour; report the parked job instead. A job that is really
            # running (a long mine) is still waited out.
            stop_on_lock_deferral=not background,
        )
    except DaemonError as exc:
        print(f"mempalace: daemon submission failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if background:
        print(f"Submitted daemon job {job['id']} ({kind})")
        return

    from .daemon import job_deferred_by_lock

    if job_deferred_by_lock(job):
        reason = (job.get("error") or {}).get("message") or "the palace write lock is held"
        # --palace is global, so it has to be echoed back ahead of the
        # subcommand: without it the suggestion silently lists the DEFAULT
        # palace's queue (or nothing at all) instead of the one this job is
        # parked in -- a wrong answer that looks authoritative.
        # `daemon jobs` and not `daemon wait`: we just declined to wait out the
        # holder, so pointing the operator at a command that blocks on the very
        # state we could not wait for would undo the point of this branch.
        palace_flag = f"--palace {shlex.quote(args.palace)} " if args.palace else ""
        print(f"mempalace: {reason}", file=sys.stderr)
        print(
            f"mempalace: job {job['id']} is queued and runs when the holder exits "
            f"(check it with: mempalace {palace_flag}daemon jobs)",
            file=sys.stderr,
        )
        sys.exit(1)

    result = job.get("result") or {}
    from .service import print_job_result

    exit_code = print_job_result(result)
    if job.get("state") != "succeeded" and exit_code == 0:
        error = job.get("error") or {}
        print(
            f"mempalace: daemon job failed: {error.get('message', 'unknown error')}",
            file=sys.stderr,
        )
        exit_code = 1
    if exit_code:
        sys.exit(exit_code)


def cmd_daemon(args):
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    backend = _backend_arg(args)
    from .daemon import (
        TERMINAL_STATES,
        DaemonError,
        QueueStore,
        get_client_if_running,
        job_to_dict,
        queue_path,
        start_daemon,
        stop_daemon,
    )

    action = getattr(args, "daemon_action", None)
    try:
        if action == "start":
            if args.foreground:
                start_daemon(palace_path, backend=backend, foreground=True)
                return
            client = start_daemon(palace_path, backend=backend, foreground=False)
            health = client.health()
            print(f"MemPalace daemon running on 127.0.0.1:{client.port}")
            print(f"  Palace: {health.get('palace_path')}")
            print(f"  PID:    {health.get('pid')}")
            return

        if action == "stop":
            if stop_daemon(palace_path):
                print("MemPalace daemon stopping")
            else:
                print("MemPalace daemon is not running")
            return

        if action == "status":
            client = get_client_if_running(palace_path)
            if client is None:
                print("MemPalace daemon is not running")
                sys.exit(1)
            health = client.health()
            print("MemPalace daemon is running")
            print(f"  Palace: {health.get('palace_path')}")
            print(f"  PID:    {health.get('pid')}")
            print(f"  Active: {health.get('active_job_id') or '-'}")
            print(f"  Jobs:   {health.get('counts') or {}}")
            return

        if action == "jobs":
            client = get_client_if_running(palace_path)
            if client is not None:
                jobs = client.list_jobs(limit=args.limit)
            else:
                qpath = queue_path(palace_path)
                if not qpath.exists():
                    jobs = []
                else:
                    jobs = [
                        job_to_dict(job, include_payload=False)
                        for job in QueueStore(qpath).list(args.limit)
                    ]
            for job in jobs:
                print(f"{job['id']}  {job['state']:<9}  {job['kind']:<10}  {job['created_at']}")
            return

        if action == "wait":
            client = get_client_if_running(palace_path)
            if client is not None:
                job = client.wait(args.job_id)
            else:
                qpath = queue_path(palace_path)
                if not qpath.exists():
                    raise DaemonError("daemon is not running")
                job = job_to_dict(QueueStore(qpath).get(args.job_id))
                if job.get("state") not in TERMINAL_STATES:
                    raise DaemonError(f"daemon is not running; job {args.job_id} is {job['state']}")
            result = job.get("result") or {}
            from .service import print_job_result

            exit_code = print_job_result(result)
            if job.get("state") != "succeeded" and exit_code == 0:
                print(f"mempalace: daemon job failed: {job.get('error')}", file=sys.stderr)
                exit_code = 1
            if exit_code:
                sys.exit(exit_code)
            return
    except DaemonError as exc:
        print(f"mempalace: daemon error: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_search(args):
    from .searcher import search, SearchError

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    try:
        search(
            query=args.query,
            palace_path=palace_path,
            wing=args.wing,
            room=args.room,
            n_results=args.results,
            since=args.since,
            before=args.before,
        )
    except SearchError:
        sys.exit(1)


def cmd_wakeup(args):
    """Show L0 (identity) + L1 (essential story) — the wake-up context."""
    from .layers import MemoryStack

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    stack = MemoryStack(palace_path=palace_path)

    text = stack.wake_up(wing=args.wing)
    tokens = len(text) // 4
    print(f"Wake-up text (~{tokens} tokens):")
    print("=" * 50)
    print(text)


def cmd_split(args):
    """Split concatenated transcript mega-files into per-session files."""
    from .split_mega_files import main as split_main
    import sys

    # Rebuild argv for split_mega_files argparse
    # Expand ~ and resolve to absolute path so split_mega_files sees a real path
    argv = ["--source", str(Path(args.dir).expanduser().resolve())]
    if args.output_dir:
        argv += ["--output-dir", args.output_dir]
    if args.dry_run:
        argv.append("--dry-run")
    if args.min_sessions != 2:
        argv += ["--min-sessions", str(args.min_sessions)]

    old_argv = sys.argv
    sys.argv = ["mempalace split"] + argv
    try:
        split_main()
    finally:
        sys.argv = old_argv


def cmd_migrate(args):
    """Migrate palace from a different ChromaDB version."""
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    if not _maintenance_requires_chroma(palace_path, "migrate"):
        raise SystemExit(2)
    from .migrate import migrate

    migrate(
        palace_path=palace_path,
        dry_run=args.dry_run,
        confirm=getattr(args, "yes", False),
    )


def cmd_migrate_wings(args):
    """Normalize legacy wing names (strip leading/trailing separators)."""
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    from .migrate import migrate_wing_names

    migrate_wing_names(
        palace_path=palace_path,
        dry_run=args.dry_run,
        confirm=getattr(args, "yes", False),
    )


def cmd_hallways(args):
    """List within-wing entity hallways (the auto-built associative graph)."""
    from .hallways import list_hallways

    palace_path = (
        os.path.expanduser(args.palace)
        if getattr(args, "palace", None)
        else MempalaceConfig().palace_path
    )
    rows = list_hallways(
        getattr(args, "wing", None),
        config=MempalaceConfig(palace_path=palace_path),
    )
    if not rows:
        print("No hallways yet -- they are built from drawer entities when you mine.")
        return
    rows.sort(key=lambda h: h.get("co_occurrence_count", 0), reverse=True)
    print(f"  {len(rows)} hallway(s):")
    for h in rows[: max(0, args.limit)]:
        label = h.get("label") or f"{h.get('entity_a', '?')} <-> {h.get('entity_b', '?')}"
        print(f"    {label}")


def cmd_status(args):
    from .miner import status

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    status(palace_path=palace_path)


# ── Logstream (RFC 003 agent coordination) ────────────────────────────────


def _open_logstream(args):
    """Open the palace logstream database for a CLI command.

    Direct SQLite access is safe alongside a running hub: logstream.sqlite3
    is WAL-mode and independent of Chroma, so CLI writes are immediately
    visible to hub readers without the mine-style forwarding Chroma needs.
    """
    from .logstream import LOGSTREAM_DB_FILENAME, Logstream

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    return Logstream(db_path=os.path.join(palace_path, LOGSTREAM_DB_FILENAME))


def _logstream_fail(message: str, as_json: bool):
    import json

    if as_json:
        print(json.dumps({"error": message}))
    else:
        print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _read_stdin_exact() -> str:
    """Read stdin as bytes and decode — never through the text layer.

    ``sys.stdin.read()`` applies universal-newline translation, turning
    CRLF into LF before we ever see it. For a store addressed by sha256
    over the exact bytes, that is silent corruption: a patch piped in from
    a Windows agent would be stored as different content with a different
    digest than the one that produced it. ``.buffer`` is absent when stdin
    has been replaced by a plain StringIO, so fall back to the text read
    rather than crashing.
    """
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        return sys.stdin.read()
    return buffer.read().decode("utf-8")


def _write_stdout_exact(content: str) -> None:
    """Write content to stdout as bytes, bypassing newline translation.

    The counterpart to :func:`_read_stdin_exact` — ``mempalace artifact get
    ID | git apply`` must deliver the stored bytes, not a re-translated
    copy of them.
    """
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(content)
        return
    sys.stdout.flush()
    buffer.write(content.encode("utf-8"))
    buffer.flush()


def _read_text_arg(inline, file_arg, default=""):
    """Resolve inline text vs --*-file (with '-' meaning stdin).

    File and stdin reads are byte-exact (see :func:`_read_stdin_exact`):
    the logstream's contract is verbatim content, so line endings must
    reach the store exactly as the author wrote them.
    """
    if inline is not None and file_arg is not None:
        raise ValueError("pass inline text or a file, not both")
    if file_arg is not None:
        if file_arg == "-":
            return _read_stdin_exact()
        return Path(os.path.expanduser(file_arg)).read_bytes().decode("utf-8")
    if inline is not None:
        return inline
    return default


def _parse_metadata_arg(raw):
    import json

    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"--metadata is not valid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise ValueError("--metadata must be a JSON object")
    return value


def _print_event_line(event):
    target = event["to_agent"] or "*"
    corr = f" corr={event['correlation_id']}" if event["correlation_id"] else ""
    status = f" [{event['status']}]" if event["status"] else ""
    arts = f" artifacts={len(event['artifact_ids'])}" if event["artifact_ids"] else ""
    body = event["body"].replace("\n", " ")
    if len(body) > 80:
        body = body[:77] + "..."
    body = f" :: {body}" if body else ""
    print(
        f"  {event['id']}  {event['created_at']}  {event['type']}  "
        f"{event['stream']}/{event['room']}  {event['from_agent']}->{target}"
        f"{status}{corr}{arts}{body}"
    )


def _watch_json(payload, *, follow: bool) -> str:
    """Serialize one ``logstream watch`` record.

    A single-shot watch prints exactly one document, so it is pretty-printed
    and ``json.load``-able as-is. Under ``--follow`` there are many records
    on one stream: indented documents concatenated back-to-back are *not*
    valid JSON, and ``json.load`` / ``jq`` reject them with trailing data —
    which defeats the point of a machine-readable flag on the mode meant for
    daemons. Follow mode therefore emits NDJSON, one compact record per line.
    """
    import json

    return (
        json.dumps(payload, ensure_ascii=False)
        if follow
        else json.dumps(payload, indent=2, ensure_ascii=False)
    )


def _watch_spec(args, as_json) -> dict:
    """Validate ``logstream watch`` arguments and build its filter spec.

    Kept apart from the watch loop so every bad input is rejected before any
    polling, any cursor resolution, and any checkpoint write — several of the
    bugs on this path were arguments that only failed once the loop had
    already started and persisted state.
    """
    from .logstream import normalize_watch_values, sanitize_watch_spec

    if args.poll_timeout_ms is not None and args.poll_timeout_ms <= 0:
        # A configured zero would make watch_events' expired-deadline branch
        # yield forever without ever polling, burning a core.
        _logstream_fail("--poll-timeout-ms must be a positive number of milliseconds", as_json)
    if args.limit is not None and args.limit < 1:
        # argparse accepts it; list_events would raise mid-loop, where the
        # only handler is for KeyboardInterrupt — a traceback, and under
        # --json no error document at all.
        _logstream_fail("--limit must be a positive integer", as_json)
    if args.idle_exit_ms is not None and args.idle_exit_ms < 0:
        # Only 0 means "wait forever". A negative value arriving from config
        # or from timeout arithmetic would otherwise take that same branch
        # silently, leaving a harness waiting on a watcher it believes will
        # time out.
        _logstream_fail(
            "--idle-exit-ms must be zero (wait forever) or a positive number of milliseconds",
            as_json,
        )

    to_agents = list(args.to_agent or [])
    exclude = list(args.exclude_from_agent or [])
    if args.agent:
        # The whole point of --agent: to_agent=<me> also matches '*'
        # broadcasts, and your own broadcasts are broadcasts — so a watcher
        # without this exclusion wakes itself every time it posts a status.
        to_agents.append(args.agent)
        exclude.append(args.agent)
    spec = {
        "streams": normalize_watch_values(args.stream),
        "rooms": normalize_watch_values(args.room),
        "types": normalize_watch_values(args.type),
        "statuses": normalize_watch_values(args.status),
        "to_agents": normalize_watch_values(to_agents),
        "from_agents": normalize_watch_values(args.from_agent),
        "exclude_from_agents": normalize_watch_values(exclude),
        "correlation_ids": normalize_watch_values(args.correlation_id),
    }
    try:
        spec = sanitize_watch_spec(spec)
    except ValueError as exc:
        _logstream_fail(str(exc), as_json)

    return spec


def _logstream_watch(ls, args, as_json):
    """Run ``logstream watch`` — block until interesting events arrive.

    Split out of ``cmd_logstream`` so that dispatcher stays under the
    complexity gate: this branch carries cursor persistence, an idle timeout,
    and follow-vs-exit semantics that no other subcommand needs.

    Exit contract, chosen so a harness can background this process and treat
    its exit as a wake-up: return (0) when a match was printed, 2 when the
    idle timeout expired having seen nothing — the same convention
    ``logstream wait`` uses for a timeout.
    """
    import time

    from .logstream import (
        WATCH_STATE_ABSENT,
        WATCH_STATE_CORRUPT,
        read_watch_state,
        write_watch_cursor,
    )

    spec = _watch_spec(args, as_json)

    stored_cursor, state_condition = read_watch_state(args.state_file)
    # A missing cursor is four different facts, and only one of them may
    # start at the tip. Treating a corrupt file or an empty-log restart as a
    # first run skips everything that arrived since, then checkpoints past
    # it — the loss is silent and permanent.
    recovery_failed = state_condition == WATCH_STATE_CORRUPT
    cursor = args.since_event_id or stored_cursor
    skipped_from = None
    if cursor is None and not args.from_start and state_condition == WATCH_STATE_ABSENT:
        # Start at the tip, like the SSE live-tail does at connect time.
        # Starting from the beginning of a long fleet log means a fresh
        # watcher wakes holding weeks of history and cannot tell it is
        # stale — measured 41 events, the oldest 49 days old, on a real
        # shared brain. Backlog is the inbox sweep's job; a watcher is for
        # what arrives from now on. Never silent: say what was skipped.
        cursor = ls.latest_event_id()
        skipped_from = cursor

    # One place decides the starting position; one place records it. That
    # position can arrive three ways — an explicit --since-event-id, the tip
    # above, or None (an empty log, or --from-start) — and every one of them
    # needs the same immediate checkpoint when no state file exists yet.
    # Deferring to the first watch_events yield leaves a window of up to a
    # full poll timeout in which an interrupt leaves no file behind, and the
    # next launch calls itself a first run and jumps to the tip, skipping
    # whatever arrived in between. Unlike later checkpoints this one cannot
    # fail quietly: losing it costs a skipped event, not a replay.
    if cursor is not None:
        # Verify the anchor before it is written anywhere. list_events raises
        # on an unknown since_event_id, and the required startup write below
        # happens first — so a typo'd or stale --since-event-id would be
        # persisted, then crash the first poll, and every later run without
        # the flag would reload it and crash again until someone deleted the
        # file by hand. Fail cleanly instead, leaving no state behind.
        try:
            ls.list_events(since_event_id=cursor, limit=1)
        except ValueError as exc:
            if args.since_event_id:
                # Explicitly supplied and wrong: user error, so refuse
                # without leaving anything behind to reload next time.
                _logstream_fail(f"{exc}. Nothing was written to the state file.", as_json)
            # A *stored* cursor whose event has gone (log rebuilt, replica
            # reset) is corrupt state rather than user error, and refusing to
            # start would strand the watcher exactly as an unreadable file
            # would. Replay instead: a duplicate, never a missed delegation.
            print(
                f"Stored cursor {cursor} no longer exists in this log; replaying from the "
                "start rather than refusing to run.",
                file=sys.stderr,
            )
            cursor = None

    if args.state_file and state_condition == WATCH_STATE_ABSENT:
        try:
            write_watch_cursor(args.state_file, cursor, agent=args.agent, required=True)
        except OSError as exc:
            _logstream_fail(
                f"could not write the initial checkpoint to {args.state_file}: {exc}. "
                "Refusing to start: without it a restart would skip every event "
                "that arrives before then.",
                as_json,
            )
    if not as_json:
        where = args.agent or ", ".join(sorted(spec["to_agents"] or [])) or "everything"
        print(f"Watching {where} from {cursor or 'now'}; Ctrl-C to stop.", file=sys.stderr)
    if skipped_from:
        print(
            f"Starting at the tip ({skipped_from}); earlier events are not replayed. "
            "Use --from-start to replay them, or sweep with `mempalace logstream list`.",
            file=sys.stderr,
        )
    if recovery_failed:
        print(
            f"State file {args.state_file} is unreadable; replaying from the start "
            "rather than skipping to the tip, so nothing since the last good "
            "checkpoint is lost.",
            file=sys.stderr,
        )

    idle_s = args.idle_exit_ms / 1000.0 if args.idle_exit_ms and args.idle_exit_ms > 0 else None
    deadline = time.monotonic() + idle_s if idle_s else None
    matched_any = False

    def _poll_timeout_ms():
        if deadline is None:
            return args.poll_timeout_ms
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        return min(args.poll_timeout_ms, remaining_ms)

    try:
        for matched, cursor in ls.watch_events(
            cursor=cursor,
            poll_timeout_ms=_poll_timeout_ms,
            limit=args.limit,
            **spec,
        ):
            if matched:
                matched_any = True
                if idle_s:
                    deadline = time.monotonic() + idle_s
                if as_json:
                    print(
                        _watch_json(
                            {
                                "events": matched,
                                "count": len(matched),
                                "cursor": cursor,
                                "timed_out": False,
                            },
                            follow=args.follow,
                        ),
                        flush=True,
                    )
                else:
                    print(f"{len(matched)} event(s):")
                    for event in matched:
                        _print_event_line(event)
                    sys.stdout.flush()
                # Matched batches checkpoint *after* stdout so a kill or
                # broken pipe between the two replays the event instead of
                # skipping it. Unmatched advances (below) are safe immediately:
                # those events were examined and rejected.
                write_watch_cursor(args.state_file, cursor, agent=args.agent)
                if not args.follow:
                    return
                continue
            write_watch_cursor(args.state_file, cursor, agent=args.agent)
            if deadline is not None and time.monotonic() >= deadline:
                if as_json:
                    print(
                        _watch_json(
                            {"events": [], "count": 0, "cursor": cursor, "timed_out": True},
                            follow=args.follow,
                        )
                    )
                else:
                    print("Idle timeout; no matching events.")
                sys.exit(0 if matched_any else 2)
    except KeyboardInterrupt:
        # Exit 0 is the documented "a match was printed" signal, so an
        # interrupted watcher must not use it — a supervisor would report
        # mail that never arrived. 128 + SIGINT, the shell convention.
        if not as_json:
            print("Stopped.", file=sys.stderr)
        sys.exit(130)
    except ValueError as exc:
        # Backstop. Every known bad input is rejected before the loop
        # starts, but a validation error escaping mid-poll would otherwise
        # surface as a traceback — and under --json as no error document at
        # all, which a machine consumer cannot distinguish from a crash.
        _logstream_fail(str(exc), as_json)


def cmd_logstream(args):
    import json

    as_json = getattr(args, "json", False)
    try:
        ls = _open_logstream(args)
    except Exception as exc:
        _logstream_fail(str(exc), as_json)
    try:
        if args.logstream_action == "append":
            try:
                body = _read_text_arg(args.body, args.body_file)
                event = ls.append_event(
                    type=args.type,
                    stream=args.stream,
                    room=args.room,
                    from_agent=args.from_agent,
                    to_agent=args.to_agent,
                    correlation_id=args.correlation_id,
                    branch=args.branch,
                    base_commit=args.base_commit,
                    status=args.status,
                    body=body,
                    metadata=_parse_metadata_arg(args.metadata),
                    artifact_ids=args.artifact_id or None,
                )
            except (ValueError, OSError) as exc:
                _logstream_fail(str(exc), as_json)
            if as_json:
                print(json.dumps(event, indent=2, ensure_ascii=False))
            else:
                print("Appended:")
                _print_event_line(event)
        elif args.logstream_action in ("list", "wait"):
            filters = {
                "stream": args.stream,
                "room": args.room,
                "type": args.type,
                "to_agent": args.to_agent,
                "from_agent": args.from_agent,
                "correlation_id": args.correlation_id,
                "status": args.status,
                "since_event_id": args.since_event_id,
                "since_created_at": args.since_created_at,
            }
            try:
                if args.logstream_action == "list":
                    events = ls.list_events(limit=args.limit, **filters)
                    result = {"events": events, "count": len(events)}
                else:
                    result = ls.wait_events(timeout_ms=args.timeout_ms, limit=args.limit, **filters)
                    result["count"] = len(result["events"])
            except ValueError as exc:
                _logstream_fail(str(exc), as_json)
            if as_json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                if result.get("timed_out"):
                    print("Timed out; no matching events.")
                elif not result["events"]:
                    print("No matching events.")
                else:
                    print(f"{result['count']} event(s):")
                    for event in result["events"]:
                        _print_event_line(event)
            if result.get("timed_out"):
                sys.exit(2)
        elif args.logstream_action == "watch":
            _logstream_watch(ls, args, as_json)
        elif args.logstream_action == "sync":
            from .logsync import load_peers, sync_all, sync_with_peer

            palace_path = (
                os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
            )
            try:
                if args.peer:
                    results = [sync_with_peer(ls, args.peer, args.token or "")]
                else:
                    if not load_peers(palace_path):
                        _logstream_fail(
                            f"no peers configured ({palace_path}/peers.json) and no --peer given",
                            as_json,
                        )
                    results = sync_all(ls, palace_path)
            except Exception as exc:
                _logstream_fail(str(exc), as_json)
            if as_json:
                print(json.dumps(results, indent=2, ensure_ascii=False))
            else:
                for stats in results:
                    if stats.get("error"):
                        print(
                            f"  {stats.get('peer_name', stats['peer_url'])}: ERROR {stats['error']}"
                        )
                    else:
                        print(
                            f"  {stats.get('peer_name', stats['peer_url'])} "
                            f"({stats['peer_replica']}): +{stats['pulled_events']} events, "
                            f"+{stats['pulled_artifacts']} artifacts"
                        )
            if any(s.get("error") for s in results):
                sys.exit(1)
        elif args.logstream_action == "ack":
            try:
                event = ls.ack_event(
                    args.event_id,
                    from_agent=args.from_agent,
                    status=args.status,
                    body=args.body or "",
                )
            except ValueError as exc:
                _logstream_fail(str(exc), as_json)
            if as_json:
                print(json.dumps(event, indent=2, ensure_ascii=False))
            else:
                print("Acknowledged:")
                _print_event_line(event)
    finally:
        ls.close()


def cmd_artifact(args):
    import json

    as_json = getattr(args, "json", False)
    try:
        ls = _open_logstream(args)
    except Exception as exc:
        _logstream_fail(str(exc), as_json)
    try:
        if args.artifact_action == "put":
            try:
                content = _read_text_arg(args.content, args.file, default=None)
                if content is None:
                    content = _read_stdin_exact()
                artifact = ls.put_artifact(
                    kind=args.kind,
                    content=content,
                    created_by=args.created_by,
                    metadata=_parse_metadata_arg(args.metadata),
                )
            except (ValueError, OSError) as exc:
                _logstream_fail(str(exc), as_json)
            if as_json:
                print(json.dumps(artifact, indent=2, ensure_ascii=False))
            else:
                print(f"Stored {artifact['id']}  kind={artifact['kind']}")
                print(f"  sha256={artifact['sha256']}")
                print(f"  size={artifact['size_bytes']} bytes")
            # Warnings go to stderr in both modes so `--json | jq` stays
            # clean while interactive callers still can't miss them.
            for warning in artifact.get("warnings", []):
                print(f"Warning: {warning}", file=sys.stderr)
        elif args.artifact_action == "get":
            try:
                artifact = ls.get_artifact(args.artifact_id)
            except ValueError as exc:
                _logstream_fail(str(exc), as_json)
            if artifact is None:
                _logstream_fail(f"artifact {args.artifact_id!r} not found", as_json)
            if args.out:
                # write_bytes, not write_text: on Windows the text layer
                # expands LF back to CRLF, so the file on disk would not
                # match the sha256 the user is told to verify.
                Path(os.path.expanduser(args.out)).write_bytes(artifact["content"].encode("utf-8"))
            if as_json:
                if args.out:
                    artifact = {**artifact, "content_written_to": args.out}
                    artifact.pop("content")
                print(json.dumps(artifact, indent=2, ensure_ascii=False))
            elif args.out:
                print(f"Wrote {artifact['size_bytes']} bytes to {args.out}")
                print(f"  sha256={artifact['sha256']}")
            else:
                # Exact content on stdout so `mempalace artifact get ID | git apply`
                # works; metadata would corrupt the stream. Written through
                # .buffer because the text layer would re-translate newlines
                # on Windows — the pipe must carry the stored bytes.
                _write_stdout_exact(artifact["content"])
    finally:
        ls.close()


def cmd_palace_set_embedder(args):
    """Record (or force-override) a palace's embedder identity (RFC 001).

    Resolves the ``unknown`` state for a legacy palace, or records a specific
    model with ``--model``. It records identity on the palace only; it does not
    change the configured model — when the two differ it prints how to align
    ``MEMPALACE_EMBEDDING_MODEL``. ``--force`` overwrites an existing,
    differently-named identity.
    """
    from .backends.base import EmbedderIdentityMismatchError
    from .palace import set_palace_embedder_identity

    config = MempalaceConfig()
    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else config.palace_path
    )
    model = getattr(args, "model", None)
    try:
        old, new = set_palace_embedder_identity(
            palace_path,
            model=model,
            force=getattr(args, "force", False),
            backend=_backend_arg(args),
        )
    except EmbedderIdentityMismatchError as exc:
        print(f"  ✗ {exc}")
        raise SystemExit(2) from exc
    if old is None:
        print(f"  ✓ recorded embedder identity: {new.model_name} (dim={new.dimension})")
    elif old.model_name == new.model_name:
        print(f"  ✓ embedder identity unchanged: {new.model_name} (dim={new.dimension})")
    else:
        print(
            f"  ✓ embedder identity changed: {old.model_name} → {new.model_name} "
            f"(dim={new.dimension})"
        )
    # set-embedder records the palace's identity; it does not change the
    # configured model. If they differ, the next normal open would mismatch —
    # tell the user how to align them.
    configured = config.embedding_model
    if new.model_name and configured and new.model_name != configured:
        print(
            f"  ⚠ configured model is {configured!r}; set MEMPALACE_EMBEDDING_MODEL="
            f"{new.model_name} (or run onboarding) so normal opens of this palace match."
        )


def cmd_repair_status(args):
    """Read-only HNSW capacity health check (#1222)."""
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    if not _maintenance_requires_chroma(palace_path, "repair-status"):
        raise SystemExit(2)
    from .repair import status as repair_status

    repair_status(palace_path=palace_path)


def cmd_repair(args):
    """Rebuild palace vector index from SQLite metadata.

    On success the palace SQLite file is VACUUMed and the FTS5 index is
    rebuilt, so the next repair's integrity preflight reads a consistent
    database (#1747).
    """
    config = MempalaceConfig()
    collection_name = config.collection_name
    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else config.palace_path
    )
    if not _maintenance_requires_chroma(palace_path, "repair"):
        raise SystemExit(2)

    import shutil
    from .backends.chroma import ChromaBackend
    from .backups import copy_palace_dir
    from .migrate import confirm_destructive_action, contains_palace_database
    from .repair import (
        RebuildCollectionError,
        TruncationDetected,
        _close_chroma_handles,
        _extract_drawers,
        _post_rebuild_cleanup,
        _preview_legacy_repair,
        _promote_temp_collection,
        _rebuild_collection_via_temp,
        check_extraction_safety,
        index_read_recovery_guidance,
        maybe_repair_poisoned_max_seq_id_before_rebuild,
        print_sqlite_integrity_abort,
        resolve_repair_preflight_errors,
        sqlite_integrity_errors,
    )

    if getattr(args, "repair_action", None) == "rebuild-index":
        args.mode = "from-sqlite"
        args.archive_existing = True

    if getattr(args, "mode", "legacy") == "max-seq-id":
        from .repair import repair_max_seq_id

        repair_max_seq_id(
            palace_path,
            segment=getattr(args, "segment", None),
            from_sidecar=getattr(args, "from_sidecar", None),
            backup=getattr(args, "backup", True),
            dry_run=getattr(args, "dry_run", False),
            assume_yes=getattr(args, "yes", False),
        )
        return

    if getattr(args, "mode", "legacy") == "from-sqlite":
        from .migrate import confirm_destructive_action
        from .repair import RebuildCleanupError, RebuildPartialError, rebuild_from_sqlite

        source_path = getattr(args, "source", None)
        source_path = (
            os.path.abspath(os.path.expanduser(source_path)) if source_path else palace_path
        )
        archive_existing = getattr(args, "archive_existing", False)

        # Gate any path that touches the user's existing palace dir
        # behind confirm_destructive_action. The legacy mode already
        # gates; from-sqlite needs the same protection because:
        # (a) --archive-existing renames the existing palace,
        # (b) --source PATH writes into --palace dir which the user
        #     may not realize is also a palace.
        # No prompt when source != dest AND dest does not exist (pure
        # extract-into-fresh-dir case is non-destructive to existing
        # palaces).
        # A --dry-run only reads the source SQLite and prints a plan — it
        # never archives, creates, or writes — so it must not trip the
        # destructive-action confirmation (#2095, #2133).
        dry_run = getattr(args, "dry_run", False)
        is_destructive_to_dest = source_path == palace_path or os.path.exists(palace_path)
        if (
            not dry_run
            and is_destructive_to_dest
            and not confirm_destructive_action(
                "Rebuild from SQLite", palace_path, assume_yes=getattr(args, "yes", False)
            )
        ):
            return

        try:
            counts = rebuild_from_sqlite(
                source_palace=source_path,
                dest_palace=palace_path,
                archive_existing_dest=archive_existing,
                dry_run=dry_run,
            )
        except RebuildPartialError as exc:
            # The error itself was already printed by rebuild_from_sqlite
            # with recovery instructions; surface a non-zero exit so
            # scripts and CI gates see the failure.
            print(
                "\n  Rebuild partial — see message above. "
                f"Failed in collection: {exc.failed_collection}"
            )
            sys.exit(1)
        except RebuildCleanupError:
            # All rows may have landed, but rebuild_from_sqlite deliberately
            # withholds success until FTS5 rebuild, VACUUM, and quick_check are
            # clean. Its exception already includes the retained destination
            # and archive/source recovery paths.
            print("\n  Rebuild cleanup failed -- see recovery details above.")
            sys.exit(1)
        # An empty counts dict is rebuild_from_sqlite's documented signal
        # for a validation refusal (missing source, existing dest,
        # in-place without --archive-existing). The library already
        # printed an actionable message; exit non-zero so unattended
        # scripts/CI distinguish "invalid inputs" from a successful
        # rebuild that legitimately found zero rows (which still returns
        # a populated dict with 0-valued counts).
        if not counts:
            sys.exit(1)
        return

    db_path = os.path.join(palace_path, "chroma.sqlite3")

    if not os.path.isdir(palace_path):
        print(f"\n  No palace found at {palace_path}")
        return
    if not contains_palace_database(palace_path):
        print(f"\n No palace database found at {db_path}")
        return

    # Run the SQLite integrity preflight before any chromadb client open.
    # ChromaDB's rust binding raises pyo3_runtime.PanicException on a
    # malformed page, which is not a regular Exception subclass and
    # propagates past the try/except below — the user gets a 30-line
    # stack trace instead of the friendly abort message. Run quick_check
    # here so we can surface the clear recovery instructions and exit
    # cleanly before chromadb's compactor touches the disk.
    dry_run = getattr(args, "dry_run", False)
    # The FTS5 autoheal inside this call is a write, so a --dry-run predicts
    # its outcome instead of performing it (#1596 is auto-healable and must
    # not surface as an abort in a preview).
    sqlite_errors = resolve_repair_preflight_errors(
        palace_path, sqlite_integrity_errors(palace_path), dry_run=dry_run
    )
    if sqlite_errors:
        print_sqlite_integrity_abort(palace_path, sqlite_errors)
        sys.exit(1)

    preflight = maybe_repair_poisoned_max_seq_id_before_rebuild(
        palace_path,
        backup=getattr(args, "backup", True),
        dry_run=dry_run,
        assume_yes=getattr(args, "yes", False),
    )
    if preflight is not None:
        return

    print(f"\n{'=' * 55}")
    print(" MemPalace Repair")
    print(f"{'=' * 55}\n")
    print(f"  Palace: {palace_path}")

    if dry_run:
        # Return before the backend is used at all: the chromadb client this
        # path opens is itself a write to chroma.sqlite3 (measured — the file
        # hash changes on get_collection alone, before count()), so a preview
        # that reached it could not be inert. Staying off the chromadb layer
        # also keeps a dry run clear of the layer repair is separately reported
        # to segfault in on a large palace (#2113). Exit non-zero on an
        # unreadable count for parity with the from-sqlite preview above, so
        # `--dry-run && repair --yes` cannot walk into the destructive run
        # after a failed preview (#2095, #2133).
        if not _preview_legacy_repair(
            palace_path=palace_path,
            collection_name=collection_name,
            confirm_truncation_ok=getattr(args, "confirm_truncation_ok", False),
        ):
            sys.exit(1)
        return

    backend = ChromaBackend()

    # Try to read existing drawers
    try:
        col = backend.get_collection(palace_path, collection_name)
        total = col.count()
        print(f"  Drawers found: {total}")
    except Exception as e:
        print(f"  Error reading palace: {e}")
        print(index_read_recovery_guidance())
        return

    if total == 0:
        print("  Nothing to repair.")
        return

    if not confirm_destructive_action(
        "Repair", palace_path, assume_yes=getattr(args, "yes", False)
    ):
        return

    # Extract all drawers in batches
    print("\n  Extracting drawers...")
    batch_size = 5000
    all_ids, all_docs, all_metas = _extract_drawers(col, total, batch_size)
    print(f"  Extracted {len(all_ids)} drawers")

    # ── #1208 guard ──────────────────────────────────────────────────
    # Cross-check against the SQLite ground truth before doing anything
    # destructive. Catches the user-reported case where chromadb's
    # collection-layer get() silently caps at 10,000 rows even on much
    # larger palaces (e.g. after manual HNSW quarantine). Override with
    # --confirm-truncation-ok only after independently verifying the
    # extraction count is real.
    try:
        check_extraction_safety(
            palace_path,
            len(all_ids),
            confirm_truncation_ok=getattr(args, "confirm_truncation_ok", False),
            collection_name=collection_name,
        )
    except TruncationDetected as e:
        print(e.message)
        return

    palace_path = os.path.normpath(palace_path)
    backup_path = palace_path + ".backup"
    if os.path.exists(backup_path):
        if not contains_palace_database(backup_path):
            print(
                "  Backup validation failed: backup path exists but does not contain chroma.sqlite3. "
                f"Please remove or rename: {backup_path}"
            )
            return
        shutil.rmtree(backup_path)
    print(f"  Backing up to {backup_path}...")
    copy_palace_dir(palace_path, backup_path, log=print)

    try:
        filed = _rebuild_collection_via_temp(
            backend,
            palace_path,
            all_ids,
            all_docs,
            all_metas,
            batch_size,
            collection_name=collection_name,
            progress=print,
        )
    except RebuildCollectionError as e:
        print(f"  Repair failed: {e}")
        if getattr(e, "live_replaced", False):
            temp_name = f"{collection_name}__repair_tmp"
            print(f"  Attempting recovery: promoting verified copy from '{temp_name}'...")
            try:
                _close_chroma_handles(palace_path, backend=backend)
                _promote_temp_collection(
                    backend,
                    palace_path,
                    temp_name,
                    collection_name,
                    len(all_ids),
                    batch_size,
                    progress=print,
                )
                print("  Recovery succeeded: live collection restored from the verified temp copy.")
            except Exception as promote_error:
                print(f"  Automatic recovery failed: {promote_error}")
                print(
                    f"  The verified pre-swap copy still survives under '{temp_name}' -- do NOT "
                    f"delete it. Recover manually by promoting it, or restore the full-directory "
                    f"backup at: {backup_path}"
                )
        sys.exit(1)

    # The bulk delete + re-upsert cycle above leaves the FTS5 inverted index
    # inconsistent, which fails the next repair's integrity preflight (#1747).
    _post_rebuild_cleanup(palace_path, backend=backend, progress=print)

    print(f"\n  Repair complete. {filed} drawers rebuilt.")
    print(f"  Backup saved at {backup_path}")
    print(f"\n{'=' * 55}\n")


def cmd_hook(args):
    """Run hook logic: reads JSON from stdin, outputs JSON to stdout."""
    from .hooks_cli import run_hook

    run_hook(hook_name=args.hook, harness=args.harness)


def cmd_instructions(args):
    """Output skill instructions to stdout."""
    from .instructions_cli import run_instructions

    run_instructions(name=args.name)


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

    print("MemPalace MCP quick setup:")
    print(f"  claude mcp add mempalace -- {server_cmd}")
    print(f"  codex mcp add mempalace -- {server_cmd}")
    print("\nRun the server directly:")
    print(f"  {server_cmd}")

    if not args.palace:
        print("\nOptional custom palace:")
        print(f"  claude mcp add mempalace -- {base_server_cmd} --palace /path/to/palace")
        print(f"  codex mcp add mempalace -- {base_server_cmd} --palace /path/to/palace")
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
    from .server_registry import server_token_path

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
    from .dialect import Dialect
    from .palace import get_closets_collection

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
    from .palace import _open_collection_or_explain

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
    from ._stdio import reconfigure_stdio_utf8_on_windows

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

    # logstream (RFC 003 agent coordination)
    p_logstream = sub.add_parser(
        "logstream",
        help="Agent coordination events — delegate work, wait for replies (RFC 003)",
    )
    logstream_sub = p_logstream.add_subparsers(dest="logstream_action")

    def _add_logstream_filters(p):
        p.add_argument("--stream", default=None, help="Stream, e.g. project/mempalace")
        p.add_argument("--room", default=None, help="Room, e.g. delegation, patches")
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

    p_ls_list = logstream_sub.add_parser("list", help="List events, oldest first")
    _add_logstream_filters(p_ls_list)
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
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
