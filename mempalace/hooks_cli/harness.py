# Loaded into mempalace.hooks_cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.hooks_cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.hooks_cli")


SUPPORTED_HARNESSES = {"claude-code", "codex"}


def _diary_agent_for_harness(harness: str) -> str:
    """Return the diary ``agent_name`` a session in ``harness`` reads under.

    Stop-hook checkpoints must be filed beside the agent's own entries so
    ``mempalace_diary_read(agent_name=...)`` surfaces them. The old code filed
    them under a fixed ``"session-hook"`` identity that no reader ever queried,
    hiding every checkpoint (#1693). A ``claude-code`` session reads its diary
    as ``"claude"``; every other harness already reads under its own name, so
    returning the harness name keeps a newly supported harness discoverable
    instead of silently invisible again.
    """
    return "claude" if harness == "claude-code" else harness


def _parse_harness_input(data: dict, harness: str) -> dict:
    """Parse stdin JSON according to the harness type."""
    if harness not in SUPPORTED_HARNESSES:
        print(f"Unknown harness: {harness}", file=sys.stderr)
        sys.exit(1)
    return {
        "session_id": _sanitize_session_id(str(data.get("session_id", "unknown"))),
        "stop_hook_active": data.get("stop_hook_active", False),
        "transcript_path": str(data.get("transcript_path", "")),
    }


# Common parent-dir tokens stripped from the encoded folder when no
# explicit ``-Projects-`` segment is present. Order matters: only the
# first match strips. These cover the bulk of Unix layouts; cwd-from-JSONL
# (the primary path) handles the long tail correctly without heuristics.
_ENCODED_PARENT_PREFIXES = (
    "git-",
    "dev-",
    "projects-",
    "Projects-",
    "src-",
    "code-",
    "work-",
    "Documents-",
)


def _safe_wing_slug(name: str) -> str:
    """Normalize a project directory name into a wing slug ``sanitize_name`` accepts.

    Builds on the historical space/hyphen handling: map characters outside
    ``sanitize_name``'s set to ``_`` while keeping ``.`` and ``'`` so existing wings
    for names like ``my.app`` are preserved (renaming a wing would orphan diary
    entries already filed under it), collapse ``..`` which the validator rejects as
    path traversal, trim edge separators it won't accept, and cap the length so
    ``wing_<slug>`` stays within ``sanitize_name``'s 128-character limit. Without
    this a folder containing e.g. a leading ``+`` produced ``wing_+project``, which
    ``sanitize_name`` rejects — silently breaking diary auto-save. Falls back to
    ``sessions`` when a name reduces to nothing (e.g. ``+``).
    """
    slug = name.lower().replace(" ", "_").replace("-", "_")
    slug = re.sub(r"[^\w.']+", "_", slug)
    slug = re.sub(r"\.{2,}", ".", slug)
    slug = slug[:120].strip("_.'")
    return slug or "sessions"


def _wing_from_jsonl_cwd(transcript_path: str) -> Optional[str]:
    """Read ``cwd`` from the first JSONL line that records it.

    Claude Code stores the absolute working directory on most message
    types (tool_use, tool_result, user/assistant turns), but not all
    (e.g. queue-operation lines lack it). Scan up to 200 lines to find
    the first record that includes a non-empty cwd, then derive the
    wing from its leaf path segment. Returns ``None`` if the file is
    unreadable, empty, or contains no cwd.
    """
    try:
        path = Path(transcript_path).expanduser()
        if not path.is_file():
            return None
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= 200:
                    break
                line = line.strip()
                if not line or '"cwd"' not in line:
                    continue
                try:
                    data = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                cwd = data.get("cwd")
                if not cwd or not isinstance(cwd, str):
                    continue
                cwd_norm = cwd.replace("\\", "/").rstrip("/")
                if not cwd_norm:
                    continue
                # A cwd inside "<project>/.claude/worktrees/<wt>" (a git
                # worktree) belongs to <project>, not the ephemeral worktree
                # directory -- otherwise every worktree spawns its own wing.
                _wt_marker = "/.claude/worktrees/"
                if _wt_marker in cwd_norm:
                    cwd_norm = cwd_norm.split(_wt_marker, 1)[0]
                project = cwd_norm.rsplit("/", 1)[-1]
                if project:
                    return f"wing_{_safe_wing_slug(project)}"
    except OSError:
        pass
    return None


def _wing_from_transcript_path(transcript_path: str) -> str:
    """Derive a project wing name from a Claude Code transcript path.

    Strategy (in priority order):

    1. PRIMARY — Read ``cwd`` from the JSONL transcript. Claude Code records
       the absolute working directory on most message types, so the project
       name is whatever the leaf path segment of cwd is. This is the
       canonical answer when present.

    2. FALLBACK — Decode the encoded folder under ``.claude/projects/``.
       Claude Code flattens path separators to dashes (``/Users/me/code/foo``
       → ``-Users-me-code-foo``), so the original directory boundaries are
       lost. We strip the platform user-home prefix (``Users-<user>-`` or
       ``home-<user>-``) and one common parent-dir token (``git-``, ``dev-``,
       ``projects-``, etc.), then convert the remaining dashes to
       underscores. Unlike the previous "last token only" heuristic, this
       never silently truncates a hyphenated project folder name like
       ``claude-code``, ``react-native``, or ``customer-portal``.

    3. LEGACY — Match an explicit ``-Projects-<name>`` segment for
       transcripts not under the standard Claude Code projects dir.

    4. DEFAULT — ``wing_sessions``.

    Closes #1410.
    """
    # 1. Primary — cwd from JSONL is the canonical source of truth
    cwd_wing = _wing_from_jsonl_cwd(transcript_path)
    if cwd_wing:
        return cwd_wing

    # Normalize path separators for cross-platform (Windows backslashes)
    normalized = transcript_path.replace("\\", "/")

    # 2. Fallback — encoded project folder under .claude/projects/
    match = re.search(r"/\.claude/projects/-([^/]+)", normalized)
    if match:
        encoded = match.group(1)
        # "<project>/.claude/worktrees/<wt>" flattens to "-<project>--claude-worktrees-<wt>"
        # here; collapse it to <project> like _wing_from_jsonl_cwd already does for cwd,
        # or every worktree spawns its own wing.
        _wt_marker = "-claude-worktrees-"
        if _wt_marker in encoded:
            encoded = encoded.split(_wt_marker, 1)[0]
        # Strip platform user-home prefix so the wing isn't dominated by
        # /Users/<user>/ or /home/<user>/.
        m = re.match(r"(?:Users|home)-[^-]+-(.+)", encoded)
        if m:
            encoded = m.group(1)
        # Strip one common parent-dir token if present, keeping the rest as
        # the project path. Hyphens become underscores to preserve
        # uniqueness for hyphenated project folder names.
        for prefix in _ENCODED_PARENT_PREFIXES:
            if encoded.startswith(prefix):
                encoded = encoded[len(prefix) :]
                break
        return f"wing_{_safe_wing_slug(encoded)}"

    # 3. Legacy — explicit -Projects-<name> segment
    match = re.search(r"-Projects-([^/]+?)(?:/|$)", normalized)
    if match:
        return f"wing_{_safe_wing_slug(match.group(1))}"

    # 4. Default
    return "wing_sessions"
