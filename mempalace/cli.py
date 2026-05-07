#!/usr/bin/env python3
"""
MemPalace — Give your AI a memory. No API key required.

Two ways to ingest:
  Projects:      mempalace mine ~/projects/my_app          (code, docs, notes)
  Conversations: mempalace mine <convo-dir> --mode convos     (Claude Code, Claude.ai, ChatGPT, Slack exports)

Same palace. Same search. Different ingest strategies.

Commands:
    mempalace init <dir>                  Detect rooms from folder structure
    mempalace split <dir>                 Split concatenated mega-files into per-session files
    mempalace mine <dir>                  Mine project files (default)
    mempalace mine <dir> --mode convos    Mine conversation exports
    mempalace search "query"              Find anything, exact words
    mempalace mcp                         Show MCP setup command
    mempalace wake-up                     Show L0 + L1 wake-up context
    mempalace wake-up --wing my_app       Wake-up for a specific project
    mempalace status                      Show what's been filed
    mempalace mined                       List mined source files grouped by wing
    mempalace purge --source-file <path>  Remove drawers mined from a specific file

Examples:
    mempalace init ~/projects/my_app
    mempalace mine ~/projects/my_app
    mempalace mine ~/.claude/projects/-Users-you-Projects-my_app --mode convos --wing my_app
    mempalace search "why did we switch to GraphQL"
    mempalace search "pricing discussion" --wing my_app --room costs
"""

import os
import sys
import json
import shlex
import argparse
from pathlib import Path

from .config import MempalaceConfig
from .corpus_origin import detect_origin_heuristic, detect_origin_llm
from .llm_client import LLMError, get_provider
from .version import __version__


# ==================== DAEMON ROUTING ====================
# When ``PALACE_DAEMON_URL`` is set, palace-daemon is the single writer
# for the canonical palace and high-traffic CLI subcommands route there
# instead of opening a local chromadb client. Mirrors the gate already
# in ``mempalace.hooks_cli`` (mining side) and
# ``mempalace.mcp_server`` (MCP dispatch). Currently routes ``status``,
# ``search``, and ``mine``; the remaining subcommands (``repair``,
# ``export``, ``sweep``, ``init``) still need on-host filesystem access
# and stay local. When the daemon URL is unset, all paths run locally
# unchanged.

_DAEMON_TIMEOUT_DEFAULT = 120  # seconds; tune via PALACE_MCP_TIMEOUT


class DaemonError(RuntimeError):
    """Raised when a daemon HTTP call fails or returns a JSON-RPC error."""


def _daemon_strict() -> bool:
    """True when ``PALACE_DAEMON_URL`` is set and strict mode is enabled.

    Set ``PALACE_DAEMON_STRICT=0`` to opt out and force the local-palace
    path even when the daemon URL is configured.
    """
    return (
        os.environ.get("PALACE_DAEMON_URL", "").strip() != ""
        and os.environ.get("PALACE_DAEMON_STRICT", "1") != "0"
    )


def _daemon_url() -> str:
    return os.environ.get("PALACE_DAEMON_URL", "").strip().rstrip("/")


def _daemon_timeout() -> int:
    raw = os.environ.get("PALACE_MCP_TIMEOUT", str(_DAEMON_TIMEOUT_DEFAULT))
    try:
        return int(raw)
    except ValueError:
        return _DAEMON_TIMEOUT_DEFAULT


def _call_daemon_tool(name: str, arguments: dict) -> dict:
    """JSON-RPC ``tools/call`` against the daemon's ``/mcp`` endpoint.

    Returns the inner tool result already parsed from the JSON text
    payload (the ``content[0].text`` envelope MCP wraps every tool
    response in). Raises :class:`DaemonError` on network failure or a
    JSON-RPC error response — the CLI must surface failures to the
    caller, never silently fall back to local (that would re-introduce
    the split-brain that daemon-strict was created to prevent).
    """
    import urllib.error
    import urllib.request

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    headers = {"content-type": "application/json"}
    api_key = os.environ.get("PALACE_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(
        f"{_daemon_url()}/mcp",
        data=json.dumps(request).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_daemon_timeout()) as resp:
            body = resp.read()
        envelope = json.loads(body.decode("utf-8", errors="replace"))
    except (urllib.error.URLError, ConnectionError, OSError, json.JSONDecodeError) as e:
        raise DaemonError(f"daemon unreachable at {_daemon_url()}: {e}") from e
    if "error" in envelope:
        err = envelope["error"]
        raise DaemonError(f"daemon error {err.get('code')}: {err.get('message')}")
    content = (envelope.get("result") or {}).get("content") or []
    if not content:
        return {}
    text = content[0].get("text") or ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Non-JSON tool output (rare). Return as-is for the caller to
        # decide what to do.
        return {"_raw": text}


def _post_daemon_mine_cli(directory: str, wing: str, mode: str = "convos") -> bool:
    """POST a mine request to the daemon's ``/mine`` endpoint.

    CLI-shaped variant of :func:`mempalace.hooks_cli._post_daemon_mine`:
    on failure, prints to stderr and returns ``False`` so the caller can
    ``sys.exit(1)``. Hooks_cli's version logs to a file and swallows
    silently because a missed-mine isn't worth crashing a hook over;
    here, the user invoked `mempalace mine` and expects to see errors.
    """
    import urllib.error
    import urllib.request

    headers = {"content-type": "application/json"}
    api_key = os.environ.get("PALACE_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(
        f"{_daemon_url()}/mine",
        data=json.dumps({"dir": directory, "wing": wing, "mode": mode}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_daemon_timeout()) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        print(f"  Daemon mine accepted: {body[:200]}")
        return True
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        print(f"  ERROR: daemon mine failed: {e}", file=sys.stderr)
        return False


def _print_daemon_status(data: dict) -> None:
    """Format ``mempalace_status`` JSON for human reading.

    The daemon's status tool returns a flat ``wings: {name: count}``
    dict (not the wing×room nested shape that ``miner.status`` builds
    from raw metadata). We print the daemon's shape rather than
    over-fetching to reconstruct the local layout — the daemon URL is
    visible in the header so the reader knows which view they're
    looking at.
    """
    import json as _json

    total = data.get("total_drawers", 0)
    wings = data.get("wings") or {}
    print(f"\n{'=' * 55}")
    print(f"  MemPalace Status — {total} drawers")
    print(f"  via palace-daemon @ {_daemon_url()}")
    print(f"{'=' * 55}\n")
    if isinstance(wings, dict) and wings:
        for wing, count in sorted(wings.items(), key=lambda kv: kv[1], reverse=True):
            if isinstance(count, dict):
                # Defensive: if a future daemon returns wing×room nested,
                # still render something useful.
                inner = count.get("total", sum((count.get("rooms") or {}).values()))
                print(f"  WING: {wing:30} {inner:>6} drawers")
            else:
                print(f"  WING: {wing:30} {count:>6} drawers")
    elif "error" in data:
        print(f"  daemon reported error: {data['error']}")
    else:
        # Surface unexpected shapes verbatim.
        print(_json.dumps(data, indent=2))
    print(f"\n{'=' * 55}\n")


def _print_daemon_search(query: str, data: dict, wing: str = None, room: str = None) -> None:
    """Format ``mempalace_search`` JSON; mirrors ``searcher.search`` output."""
    if "error" in data and not data.get("results"):
        print(f"\n  {data['error']}")
        if "hint" in data:
            print(f"  {data['hint']}")
        raise DaemonError(data["error"])

    hits = data.get("results") or []
    warnings = data.get("warnings") or []

    if not hits:
        print(f'\n  No results found for: "{query}"')
        for w in warnings:
            print(f"  ! {w}")
        return

    print(f"\n{'=' * 60}")
    print(f'  Results for: "{query}"')
    if wing:
        print(f"  Wing: {wing}")
    if room:
        print(f"  Room: {room}")
    if data.get("available_in_scope") is not None:
        print(f"  Scope has: {data['available_in_scope']} drawers matching filter")
    if warnings:
        for w in warnings:
            print(f"  ! {w}")
    print(f"  via palace-daemon @ {_daemon_url()}")
    print(f"{'=' * 60}\n")

    for i, hit in enumerate(hits, 1):
        print(f"  [{i}] {hit.get('wing', '?')} / {hit.get('room', '?')}")
        print(f"      Source: {hit.get('source_file', '?')}")
        sim = hit.get("similarity")
        bm25 = hit.get("bm25_score")
        if sim is not None and bm25 is not None:
            print(f"      Match:  cosine={sim}  bm25={bm25}")
        elif sim is not None:
            print(f"      Match:  {sim}")
        elif bm25 is not None:
            print(f"      BM25:   {bm25}  (matched_via: {hit.get('matched_via', 'drawer')})")
        print()
        for line in (hit.get("text") or "").strip().split("\n"):
            print(f"      {line}")
        print()
        print(f"  {'─' * 56}")
    print()


_MEMPALACE_PROJECT_FILES = ("mempalace.yaml", "entities.json")

# Pass 0 corpus-origin sampling caps. Tier 1 reads FULL file content (no
# front-bias sampling) but bounds total memory on enormous corpora. Tier 2
# trims to a smaller view because LLM context windows are finite.
_PASS_ZERO_MAX_FILES = 30
_PASS_ZERO_PER_FILE_CAP = 100_000  # 100KB per file is generous for prose
_PASS_ZERO_TOTAL_CAP = 5_000_000  # 5MB total ceiling — bounds memory
_PASS_ZERO_LLM_PER_SAMPLE = 2_000  # for Tier 2 LLM call only
_PASS_ZERO_LLM_MAX_SAMPLES = 20  # caps the LLM-tier sample count


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
        print("  Skipping corpus-origin detection — no readable samples.")
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
    existing = gitignore.read_text() if gitignore.exists() else ""
    existing_lines = {line.strip() for line in existing.splitlines()}
    missing = [p for p in _MEMPALACE_PROJECT_FILES if p not in existing_lines]
    if not missing:
        return False
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    block = prefix + "\n# MemPalace per-project files (issue #185)\n" + "\n".join(missing) + "\n"
    with open(gitignore, "a") as f:
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
        print("  No entities detected — proceeding with directory-based rooms.")

    # Pass 2: detect rooms from folder structure
    detect_rooms_local(project_dir=args.dir, yes=getattr(args, "yes", False))
    cfg.init()

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


def cmd_mine(args):
    if _daemon_strict() and not args.palace:
        # Daemon-strict: route to /mine. The daemon owns the canonical
        # palace and its filesystem layout, and translates client-side
        # paths via PALACE_DAEMON_PATH_MAP. Flags that only make sense
        # on the local FS (--redetect-origin, --include-ignored,
        # --no-gitignore, --dry-run) are warned about but not passed —
        # the daemon's /mine endpoint does not expose them.
        ignored_flags = []
        if getattr(args, "redetect_origin", False):
            ignored_flags.append("--redetect-origin")
        if getattr(args, "dry_run", False):
            ignored_flags.append("--dry-run")
        if getattr(args, "no_gitignore", False):
            ignored_flags.append("--no-gitignore")
        if getattr(args, "include_ignored", None):
            ignored_flags.append("--include-ignored")
        if ignored_flags:
            print(
                f"  WARN: daemon-strict mode ignores these local-only flags: {', '.join(ignored_flags)}",
                file=sys.stderr,
            )

        directory = os.path.abspath(os.path.expanduser(args.dir))
        wing = args.wing
        if not wing:
            # Match local-mine semantics: derive wing from directory name
            # the same way miner / convo_miner do when --wing is omitted.
            from .config import normalize_wing_name

            wing = normalize_wing_name(Path(directory).name)
        ok = _post_daemon_mine_cli(directory, wing=wing, mode=args.mode)
        sys.exit(0 if ok else 1)

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    include_ignored = []
    for raw in args.include_ignored or []:
        include_ignored.extend(part.strip() for part in raw.split(",") if part.strip())

    # --redetect-origin re-runs corpus_origin on the current corpus state
    # and overwrites <palace>/.mempalace/origin.json before mining proceeds.
    # Heuristic-only by design — full LLM detection lives on `mempalace init`.
    if getattr(args, "redetect_origin", False):
        _run_pass_zero(
            project_dir=args.dir,
            palace_dir=palace_path,
            llm_provider=None,
        )

    if args.mode == "convos":
        from .convo_miner import mine_convos

        mine_convos(
            convo_dir=args.dir,
            palace_path=palace_path,
            wing=args.wing,
            agent=args.agent,
            limit=args.limit,
            dry_run=args.dry_run,
            extract_mode=args.extract,
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
        )


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


def cmd_search(args):
    if _daemon_strict() and not args.palace:
        arguments = {"query": args.query, "max_results": args.results}
        if args.wing:
            arguments["wing"] = args.wing
        if args.room:
            arguments["room"] = args.room
        try:
            data = _call_daemon_tool("mempalace_search", arguments)
            _print_daemon_search(args.query, data, wing=args.wing, room=args.room)
        except DaemonError as e:
            print(f"\n  ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        return

    from .searcher import search, SearchError

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    try:
        search(
            query=args.query,
            palace_path=palace_path,
            wing=args.wing,
            room=args.room,
            n_results=args.results,
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


def cmd_export(args):
    from .exporter import export_palace

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    output_dir = os.path.expanduser(args.output)

    print(f"\n{'=' * 55}")
    print("  MemPalace Export")
    print(f"{'=' * 55}\n")
    print(f"  Palace: {palace_path}")
    print(f"  Output: {output_dir}\n")

    export_palace(palace_path=palace_path, output_dir=output_dir)

    print(f"\n{'=' * 55}\n")


def cmd_migrate(args):
    """Migrate palace from a different ChromaDB version."""
    from .migrate import migrate

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    migrate(
        palace_path=palace_path,
        dry_run=args.dry_run,
        confirm=getattr(args, "yes", False),
    )


def cmd_purge(args):
    """Delete drawers by wing and/or room.

    Uses ``collection.delete(where=...)`` — chromadb's filter-delete path
    doesn't go through ``updatePoint`` / ``repairConnectionsForUpdate``,
    which is the upsert-only race from #521 that an earlier draft of this
    command tried to side-step with a nuke-and-rebuild. The simpler path
    works without losing drawers if the process is interrupted, without
    re-embedding the survivors under a default model, and without
    bypassing the backend abstraction.

    ``--room`` without ``--wing`` purges that room across ALL wings.
    Not idempotent — running purge twice on the same criteria prints
    "No drawers found" the second time.
    """
    from .backends.chroma import ChromaBackend
    from .migrate import confirm_destructive_action, contains_palace_database

    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    )

    if not os.path.isdir(palace_path) or not contains_palace_database(palace_path):
        print(f"\n  No palace found at {palace_path}")
        return

    source_file = getattr(args, "source_file", None)
    clauses = []
    if args.wing:
        clauses.append({"wing": args.wing})
    if args.room:
        clauses.append({"room": args.room})
    if source_file:
        clauses.append({"source_file": source_file})

    if not clauses:
        print("  Error: specify at least one of --wing, --room, --source-file")
        return
    where = clauses[0] if len(clauses) == 1 else {"$and": clauses}

    backend = ChromaBackend()
    try:
        col = backend.get_collection(palace_path, "mempalace_drawers")
    except Exception as e:
        print(f"\n  Error reading palace: {e}")
        return

    # Probe match count via a `where=`-filtered get with no payload.
    # ChromaCollection.get returns the typed result; we only need ids.
    try:
        matched = col.get(where=where, include=[])
    except Exception as e:
        print(f"\n  Error querying drawers: {e}")
        return

    match_ids = matched.get("ids") if isinstance(matched, dict) else getattr(matched, "ids", [])
    match_ids = match_ids or []
    match_count = len(match_ids)

    label_parts = []
    if args.wing:
        label_parts.append(f"wing={args.wing}")
    if args.room:
        label_parts.append(f"room={args.room}")
    if source_file:
        label_parts.append(f"source-file={source_file}")
    label = " ".join(label_parts)

    if match_count == 0:
        print(f"\n  No drawers found matching {label}\n")
        return

    print(f"\n  Found {match_count:,} drawers matching {label}")

    if not args.yes:
        if not confirm_destructive_action(f"Purge of {match_count:,} drawers", palace_path):
            return

    print("  Deleting matching drawers...")
    try:
        col.delete(where=where)
    except Exception as e:
        print(f"\n  Delete failed: {e}\n")
        return

    remaining = col.count()
    print(f"\n  Purged {match_count:,} drawers. Remaining: {remaining:,}\n")


def cmd_status(args):
    if _daemon_strict() and not args.palace:
        # --palace overrides routing: an explicit local-path argument
        # means the user wants to inspect THAT palace, not the daemon.
        try:
            data = _call_daemon_tool("mempalace_status", {})
        except DaemonError as e:
            print(f"\n  ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        _print_daemon_status(data)
        return

    from .miner import status

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    status(palace_path=palace_path)


def cmd_mined(args):
    """List mined source files grouped by wing.

    Companion to ``status`` (which groups by wing × room) — answers "which
    files have I mined into this wing?" so an operator can pick targets
    for ``mempalace purge --source-file <path>``.

    Skips drawers without a ``source_file`` metadata key (typically
    diary entries, kg drawers, manually-added entries).
    """
    from collections import defaultdict

    from .backends.chroma import ChromaBackend
    from .migrate import contains_palace_database

    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    )

    if not os.path.isdir(palace_path) or not contains_palace_database(palace_path):
        print(f"\n  No palace found at {palace_path}")
        return

    backend = ChromaBackend()
    try:
        col = backend.get_collection(palace_path, "mempalace_drawers")
    except Exception as e:
        print(f"\n  Error reading palace: {e}")
        return

    # Wing-by-source aggregation. Pagination mirrors miner.status so
    # palaces with hundreds of thousands of drawers don't trip SQLite's
    # max-variable limit on a single col.get(limit=total).
    #
    # When --wing is given, push the filter into each batch's where=
    # so we never scan unrelated wings. The previous version called
    # col.get(where=..., include=[]) without pagination to size the loop;
    # ChromaDB returns at most ~10K ids on a single get() call, silently
    # truncating beyond that — so the inner loop's `offset < total` would
    # have undercounted on a wing > 10K drawers, missing late entries.
    # Drop the upfront sizing entirely; the inner loop reads until it
    # gets back an empty batch (Copilot finding on jphein/mempalace#7).
    where = {"wing": args.wing} if args.wing else None
    wing_sources: dict = defaultdict(lambda: defaultdict(int))
    batch_size = 5000
    offset = 0
    while True:
        kwargs = {"limit": batch_size, "offset": offset, "include": ["metadatas"]}
        if where is not None:
            kwargs["where"] = where
        r = col.get(**kwargs)
        batch = r.get("metadatas") if isinstance(r, dict) else getattr(r, "metadatas", [])
        if not batch:
            break
        for m in batch:
            m = m or {}
            src = m.get("source_file")
            if not src:
                continue
            wing = m.get("wing", "?")
            wing_sources[wing][src] += 1
        offset += len(batch)
        # Defensive: if the backend returned fewer than batch_size, we're
        # past the last page. Saves one trailing empty col.get on palaces
        # whose wing-count is an exact multiple of batch_size (rare but
        # cheap to handle).
        if len(batch) < batch_size:
            break

    if not wing_sources:
        scope = f" in wing={args.wing}" if args.wing else ""
        print(f"\n  No mined source files found{scope}.\n")
        return

    print(f"\n{'=' * 55}")
    print("  MemPalace Mined — sources by wing")
    print(f"{'=' * 55}\n")
    for wing in sorted(wing_sources):
        sources = sorted(wing_sources[wing].items(), key=lambda x: x[1], reverse=True)
        print(f"  WING: {wing}  ({len(sources)} sources, {sum(c for _, c in sources)} drawers)")
        shown = sources if args.limit == 0 else sources[: args.limit]
        for src, count in shown:
            print(f"    {count:5}  {src}")
        if args.limit and len(sources) > args.limit:
            print(f"    ... {len(sources) - args.limit} more (use --limit 0 to show all)")
        print()
    print(f"{'=' * 55}\n")


def cmd_repair_status(args):
    """Read-only HNSW capacity health check (#1222)."""
    from .repair import status as repair_status

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    repair_status(palace_path=palace_path)


def cmd_repair(args):
    """Repair palace state.

    Default mode is full HNSW rebuild via extract + re-upsert
    (``--mode rebuild`` / ``--mode legacy``, synonyms). Also handles
    ``--mode max-seq-id`` for un-poisoning ``max_seq_id`` rows
    corrupted by the legacy 0.6.x → 1.5.x chromadb migration shim
    (#1208 / #1288 family). The earlier ``reorganize`` mode was
    retired alongside the recovery collection (PR #8 / row 32).

    Closes Copilot finding on jphein/mempalace#8: docstring claimed
    only "rebuild" while the function continued to dispatch
    ``max-seq-id`` based on ``args.mode``.
    """
    import shutil
    from .backends.chroma import ChromaBackend
    from .migrate import confirm_destructive_action, contains_palace_database
    from .repair import TruncationDetected, check_extraction_safety

    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    )

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

    db_path = os.path.join(palace_path, "chroma.sqlite3")

    if not os.path.isdir(palace_path):
        print(f"\n  No palace found at {palace_path}")
        return
    if not contains_palace_database(palace_path):
        print(f"\n  No palace database found at {db_path}")
        return

    print(f"\n{'=' * 55}")
    print("  MemPalace Repair")
    print(f"{'=' * 55}\n")
    print(f"  Palace: {palace_path}")

    backend = ChromaBackend()

    # Try to read existing drawers
    try:
        col = backend.get_collection(palace_path, "mempalace_drawers")
        total = col.count()
        print(f"  Drawers found: {total}")
    except Exception as e:
        print(f"  Error reading palace: {e}")
        print("  Cannot recover — palace may need to be re-mined from source files.")
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
    all_ids = []
    all_docs = []
    all_metas = []
    offset = 0
    while offset < total:
        batch = col.get(limit=batch_size, offset=offset, include=["documents", "metadatas"])
        if not batch["ids"]:
            break
        all_ids.extend(batch["ids"])
        all_docs.extend(batch["documents"])
        all_metas.extend(batch["metadatas"])
        offset += len(batch["ids"])
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
        )
    except TruncationDetected as e:
        print(e.message)
        return

    # Backup and rebuild
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
    shutil.copytree(palace_path, backup_path)

    print("  Rebuilding collection...")
    backend.delete_collection(palace_path, "mempalace_drawers")
    new_col = backend.create_collection(palace_path, "mempalace_drawers")

    filed = 0
    for i in range(0, len(all_ids), batch_size):
        batch_ids = all_ids[i : i + batch_size]
        batch_docs = all_docs[i : i + batch_size]
        batch_metas = all_metas[i : i + batch_size]
        new_col.add(documents=batch_docs, ids=batch_ids, metadatas=batch_metas)
        filed += len(batch_ids)
        print(f"  Re-filed {filed}/{len(all_ids)} drawers...")

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

    if args.palace:
        resolved_palace = str(Path(args.palace).expanduser())
        server_cmd = f"{base_server_cmd} --palace {shlex.quote(resolved_palace)}"
    else:
        server_cmd = base_server_cmd

    print("MemPalace MCP quick setup:")
    print(f"  claude mcp add mempalace -- {server_cmd}")
    print("\nRun the server directly:")
    print(f"  {server_cmd}")

    if not args.palace:
        print("\nOptional custom palace:")
        print(f"  claude mcp add mempalace -- {base_server_cmd} --palace /path/to/palace")
        print(f"  {base_server_cmd} --palace /path/to/palace")


def cmd_compress(args):
    """Compress drawers in a wing using AAAK Dialect."""
    from .backends.chroma import ChromaBackend
    from .dialect import Dialect

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path

    # Load dialect (with optional entity config)
    config_path = args.config
    if not config_path:
        for candidate in ["entities.json", os.path.join(palace_path, "entities.json")]:
            if os.path.exists(candidate):
                config_path = candidate
                break

    if config_path and os.path.exists(config_path):
        dialect = Dialect.from_config(config_path)
        print(f"  Loaded entity config: {config_path}")
    else:
        dialect = Dialect()

    # Connect to palace
    backend = ChromaBackend()
    try:
        col = backend.get_collection(palace_path, "mempalace_drawers")
    except Exception:
        print(f"\n  No palace found at {palace_path}")
        print("  Run: mempalace init <dir> then mempalace mine <dir>")
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
            comp_col = backend.get_or_create_collection(palace_path, "mempalace_closets")
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


def main():
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

    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Detect rooms from your folder structure")
    p_init.add_argument("dir", help="Project directory to set up")
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
    p_mine.add_argument("dir", help="Directory to mine")
    p_mine.add_argument(
        "--mode",
        choices=["projects", "convos"],
        default="projects",
        help="Ingest mode: 'projects' for code/docs (default), 'convos' for chat exports",
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
        "--extract",
        choices=["exchange", "general"],
        default="exchange",
        help="Extraction strategy for convos mode: 'exchange' (default) or 'general' (5 memory types)",
    )
    p_mine.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Parallel workers for file processing (default: min(8, cpu_count); 1 = sequential)",
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

    # search
    p_search = sub.add_parser("search", help="Find anything, exact words")
    p_search.add_argument("query", help="What to search for")
    p_search.add_argument("--wing", default=None, help="Limit to one project")
    p_search.add_argument("--room", default=None, help="Limit to one room")
    p_search.add_argument("--results", type=int, default=5, help="Number of results")

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

    # export
    p_export = sub.add_parser("export", help="Export palace as browsable markdown files")
    p_export.add_argument(
        "--output",
        "-o",
        default="./palace-export",
        help="Output directory (default: ./palace-export)",
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
        choices=["session-start", "stop", "precompact"],
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
        "--mode",
        choices=["rebuild", "legacy", "max-seq-id"],
        default="legacy",
        help=(
            "rebuild/legacy: full-palace HNSW rebuild via extract + re-upsert (default; "
            "rebuild and legacy are synonyms). "
            "max-seq-id: un-poison max_seq_id rows corrupted by the legacy 0.6.x shim."
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
        help="Print detected poisoned rows and exit without mutation (--mode max-seq-id only)",
    )

    # repair-status — read-only HNSW capacity health check (#1222)
    sub.add_parser(
        "repair-status",
        help="Compare sqlite vs HNSW element counts (read-only; never opens a chromadb client)",
    )

    # mcp
    sub.add_parser(
        "mcp",
        help="Show MCP setup command for connecting MemPalace to your AI client",
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

    p_purge = sub.add_parser(
        "purge",
        help="Delete drawers by wing, room, and/or source-file (filtered delete via chromadb)",
    )
    p_purge.add_argument("--wing", help="Wing to purge")
    p_purge.add_argument("--room", help="Room to purge (without --wing, purges across ALL wings)")
    p_purge.add_argument(
        "--source-file",
        help="Source-file path to purge (matches metadata.source_file exactly)",
    )
    p_purge.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    sub.add_parser("status", help="Show what's been filed")

    p_mined = sub.add_parser(
        "mined",
        help="List mined source files grouped by wing (companion to status, which groups by room)",
    )
    p_mined.add_argument("--wing", help="Show only this wing")

    def _nonneg_int(value: str) -> int:
        # Reject negative --limit values; argparse's bare type=int would
        # silently accept e.g. -1 and produce nonsensical "... -2 more"
        # output (Copilot finding on jphein/mempalace#4).
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(f"expected non-negative integer, got {value!r}")
        if n < 0:
            raise argparse.ArgumentTypeError(f"--limit must be >= 0 (got {n})")
        return n

    p_mined.add_argument(
        "--limit",
        type=_nonneg_int,
        default=50,
        help="Show at most this many sources per wing (default 50; 0 means show all)",
    )

    args = parser.parse_args()

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

    dispatch = {
        "init": cmd_init,
        "mine": cmd_mine,
        "split": cmd_split,
        "search": cmd_search,
        "export": cmd_export,
        "sweep": cmd_sweep,
        "mcp": cmd_mcp,
        "compress": cmd_compress,
        "wake-up": cmd_wakeup,
        "repair": cmd_repair,
        "repair-status": cmd_repair_status,
        "migrate": cmd_migrate,
        "purge": cmd_purge,
        "status": cmd_status,
        "mined": cmd_mined,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
