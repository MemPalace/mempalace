from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "mempalace" / "config.py"
HOOKS = ROOT / "mempalace" / "hooks_cli.py"
TESTS = ROOT / "tests" / "test_hooks_cli.py"


def run(*args: str, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(args),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    print(f"$ {' '.join(args)}")
    if proc.stdout:
        print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if expect_success and proc.returncode != 0:
        raise SystemExit(proc.returncode)
    return proc


def require_once(text: str, needle: str, *, label: str) -> None:
    count = text.count(needle)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")


def patch_tests() -> None:
    text = TESTS.read_text(encoding="utf-8")

    import_marker = "from mempalace.config import sanitize_name\n"
    require_once(text, import_marker, label="config import")
    text = text.replace(
        import_marker,
        "from mempalace.config import MempalaceConfig, sanitize_name\n",
        1,
    )

    ingest_import_marker = '''    _get_mine_targets,
    _hooks_daemon_enabled,
    _log,
'''
    require_once(text, ingest_import_marker, label="hook helper imports")
    text = text.replace(
        ingest_import_marker,
        '''    _get_mine_targets,
    _hooks_daemon_enabled,
    _ingest_transcript,
    _log,
''',
        1,
    )

    insertion_marker = "\n# --- hook_session_start ---\n"
    require_once(text, insertion_marker, label="issue 2057 test insertion")
    tests = r'''


def _write_ingest_transcript(path: Path, *, cwd: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {"type": "user", "cwd": cwd, "content": "x" * 80},
        {"message": {"role": "user", "content": "y" * 80}},
    ]
    path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries),
        encoding="utf-8",
    )


def test_hook_transcript_wing_defaults_to_sessions(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPALACE_HOOK_TRANSCRIPT_WING", raising=False)
    assert MempalaceConfig(config_dir=tmp_path).hook_transcript_wing == "sessions"


def test_hook_transcript_wing_reads_project_config(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPALACE_HOOK_TRANSCRIPT_WING", raising=False)
    (tmp_path / "config.json").write_text(
        json.dumps({"hooks": {"transcript_wing": "project"}}),
        encoding="utf-8",
    )
    assert MempalaceConfig(config_dir=tmp_path).hook_transcript_wing == "project"


def test_hook_transcript_wing_env_overrides_config(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(
        json.dumps({"hooks": {"transcript_wing": "sessions"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MEMPALACE_HOOK_TRANSCRIPT_WING", "project")
    assert MempalaceConfig(config_dir=tmp_path).hook_transcript_wing == "project"


def test_hook_transcript_wing_invalid_value_fails_safe_to_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPALACE_HOOK_TRANSCRIPT_WING", "surprise")
    assert MempalaceConfig(config_dir=tmp_path).hook_transcript_wing == "sessions"


def test_ingest_transcript_keeps_sessions_by_default(tmp_path):
    transcript = tmp_path / "project" / "session.jsonl"
    _write_ingest_transcript(transcript, cwd="/Users/eddie/git/Starframe-PC-Control")
    config = MagicMock()
    config.hook_transcript_wing = "sessions"
    routing = MagicMock(blocked=False, use_daemon=False)

    with (
        patch("mempalace.hooks_cli.MempalaceConfig", return_value=config),
        patch("mempalace.hooks_cli._current_hook_write_routing", return_value=routing),
        patch("mempalace.hooks_cli._mempalace_python", return_value="/venv/python"),
        patch("mempalace.hooks_cli._spawn_mine") as spawn,
    ):
        _ingest_transcript(str(transcript))

    cmd = spawn.call_args.args[0]
    assert cmd[cmd.index("--wing") + 1] == "sessions"


def test_ingest_transcript_project_mode_uses_stable_cwd_wing(tmp_path):
    transcript = tmp_path / ".claude" / "projects" / "-Users-eddie-Starframe-PC-Control" / "session.jsonl"
    _write_ingest_transcript(transcript, cwd="/Users/eddie/git/Starframe-PC-Control")
    config = MagicMock()
    config.hook_transcript_wing = "project"
    routing = MagicMock(blocked=False, use_daemon=False)

    with (
        patch("mempalace.hooks_cli.MempalaceConfig", return_value=config),
        patch("mempalace.hooks_cli._current_hook_write_routing", return_value=routing),
        patch("mempalace.hooks_cli._mempalace_python", return_value="/venv/python"),
        patch("mempalace.hooks_cli._spawn_mine") as spawn,
    ):
        _ingest_transcript(str(transcript))

    cmd = spawn.call_args.args[0]
    assert cmd[cmd.index("--wing") + 1] == "wing_starframe_pc_control"


def test_ingest_transcript_project_mode_routes_daemon_and_dedupe_by_wing(tmp_path):
    transcript = tmp_path / ".claude" / "projects" / "-Users-eddie-Starframe-PC-Control" / "session.jsonl"
    _write_ingest_transcript(transcript, cwd="/Users/eddie/git/Starframe-PC-Control")
    config = MagicMock()
    config.hook_transcript_wing = "project"
    routing = MagicMock(blocked=False, use_daemon=True)

    with (
        patch("mempalace.hooks_cli.MempalaceConfig", return_value=config),
        patch("mempalace.hooks_cli._current_hook_write_routing", return_value=routing),
        patch("mempalace.hooks_cli._submit_daemon_job") as submit,
    ):
        _ingest_transcript(str(transcript))

    submit.assert_called_once()
    payload = submit.call_args.args[1]
    assert payload["wing"] == "wing_starframe_pc_control"
    dedupe_key = submit.call_args.kwargs["dedupe_key"]
    assert ":convos:wing_starframe_pc_control:" in dedupe_key

'''
    text = text.replace(insertion_marker, tests + insertion_marker, 1)
    TESTS.write_text(text, encoding="utf-8")


def patch_config() -> None:
    text = CONFIG.read_text(encoding="utf-8")
    marker = '''    @property
    def hook_silent_save(self):
        """Whether the stop hook saves directly (True) or blocks for MCP calls (False)."""
'''
    require_once(text, marker, label="hook transcript wing config insertion")
    replacement = '''    @property
    def hook_transcript_wing(self) -> str:
        """Destination policy for hook-driven transcript mining.

        ``sessions`` preserves the historical shared wing. ``project`` derives a
        stable per-project wing from the transcript JSONL cwd/path. Invalid values
        fail safe to ``sessions`` so upgrades cannot silently relocate memories.
        """

        env_val = os.environ.get("MEMPALACE_HOOK_TRANSCRIPT_WING")
        hooks = self._file_config.get("hooks", {})
        raw = env_val if env_val is not None else hooks.get("transcript_wing", "sessions")
        value = str(raw or "").strip().lower()
        return value if value in {"sessions", "project"} else "sessions"

    @property
    def hook_silent_save(self):
        """Whether the stop hook saves directly (True) or blocks for MCP calls (False)."""
'''
    CONFIG.write_text(text.replace(marker, replacement, 1), encoding="utf-8")


def patch_hooks() -> None:
    text = HOOKS.read_text(encoding="utf-8")

    dedupe_marker = '''def _daemon_mine_dedupe_key(source: str, mode: str) -> str:
    try:
        source_key = str(Path(source).expanduser().resolve())
    except OSError:
        source_key = str(Path(source).expanduser())
    return f"hook:mine:{mode}:{source_key}"
'''
    require_once(text, dedupe_marker, label="wing-aware daemon dedupe")
    text = text.replace(
        dedupe_marker,
        '''def _daemon_mine_dedupe_key(
    source: str,
    mode: str,
    wing: str | None = None,
) -> str:
    try:
        source_key = str(Path(source).expanduser().resolve())
    except OSError:
        source_key = str(Path(source).expanduser())
    wing_key = str(wing or "").strip()
    if wing_key:
        return f"hook:mine:{mode}:{wing_key}:{source_key}"
    return f"hook:mine:{mode}:{source_key}"
''',
        1,
    )

    config_marker = '''    try:
        MempalaceConfig()  # validate config loads
    except Exception:
        return
'''
    require_once(text, config_marker, label="transcript config capture")
    text = text.replace(
        config_marker,
        '''    try:
        config = MempalaceConfig()
    except Exception:
        return
''',
        1,
    )

    function_marker = '''def _ingest_transcript(transcript_path: str):
    """Mine a Claude Code session transcript into the palace as a conversation."""
'''
    require_once(text, function_marker, label="transcript wing helper insertion")
    helper = '''def _transcript_ingest_wing(path: Path, config: MempalaceConfig) -> str:
    """Resolve the hook transcript destination without breaking old installs."""

    policy = str(getattr(config, "hook_transcript_wing", "sessions") or "").strip().lower()
    if policy != "project":
        return "sessions"
    derived = _wing_from_transcript_path(str(path))
    return derived if derived != "wing_sessions" else "sessions"


'''
    text = text.replace(function_marker, helper + function_marker, 1)

    routing_marker = '''    routing = _current_hook_write_routing()
    if routing.blocked:
        _log_hook_write_blocked(routing, "transcript ingest")
        return

    try:
        if routing.use_daemon:
'''
    require_once(text, routing_marker, label="transcript wing resolution")
    text = text.replace(
        routing_marker,
        '''    routing = _current_hook_write_routing()
    if routing.blocked:
        _log_hook_write_blocked(routing, "transcript ingest")
        return

    transcript_wing = _transcript_ingest_wing(path, config)

    try:
        if routing.use_daemon:
''',
        1,
    )

    daemon_marker = '''                        "source": str(path.parent),
                        "mode": "convos",
                        "wing": "sessions",
                        "agent": "mempalace",
                    },
                    dedupe_key=_daemon_mine_dedupe_key(str(path.parent), "convos"),
'''
    require_once(text, daemon_marker, label="daemon transcript wing routing")
    text = text.replace(
        daemon_marker,
        '''                        "source": str(path.parent),
                        "mode": "convos",
                        "wing": transcript_wing,
                        "agent": "mempalace",
                    },
                    dedupe_key=_daemon_mine_dedupe_key(
                        str(path.parent),
                        "convos",
                        transcript_wing,
                    ),
''',
        1,
    )

    direct_marker = '''                "--mode",
                "convos",
                "--wing",
                "sessions",
            ]
'''
    require_once(text, direct_marker, label="direct transcript wing routing")
    text = text.replace(
        direct_marker,
        '''                "--mode",
                "convos",
                "--wing",
                transcript_wing,
            ]
''',
        1,
    )

    HOOKS.write_text(text, encoding="utf-8")


def main() -> None:
    patch_tests()

    red = run(
        sys.executable,
        "-m",
        "pytest",
        "tests/test_hooks_cli.py",
        "-k",
        "hook_transcript_wing or ingest_transcript_keeps_sessions or ingest_transcript_project_mode",
        "-q",
        expect_success=False,
    )
    red_output = f"{red.stdout}\n{red.stderr}"
    if red.returncode == 0:
        raise RuntimeError("TDD red phase failed: issue 2057 behavior already passed")
    if "FAILED" not in red_output:
        raise RuntimeError("TDD red phase failed for an unexpected reason")

    patch_config()
    patch_hooks()

    run(
        sys.executable,
        "-m",
        "pytest",
        "tests/test_hooks_cli.py",
        "-k",
        "hook_transcript_wing or ingest_transcript_keeps_sessions or ingest_transcript_project_mode",
        "-q",
    )
    run(
        sys.executable,
        "-m",
        "pytest",
        "tests/test_hooks_cli.py",
        "tests/test_hook_write_routing.py",
        "-q",
    )
    run(
        "ruff",
        "check",
        "mempalace/config.py",
        "mempalace/hooks_cli.py",
        "tests/test_hooks_cli.py",
    )
    run(
        sys.executable,
        "-m",
        "compileall",
        "-q",
        "mempalace/config.py",
        "mempalace/hooks_cli.py",
        "tests/test_hooks_cli.py",
    )
    run("git", "diff", "--check")


if __name__ == "__main__":
    main()
