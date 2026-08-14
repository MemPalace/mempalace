"""Opt-in: hooks mine into a cwd-derived wing instead of the ``wing_api`` default.

Claude Code's Stop / PreCompact payloads carry ``cwd`` — the directory the
user was actually working in. The hooks currently drop it, and run
``mempalace mine --mode convos`` with no ``--wing``. Because the transcript
lives under ``~/.claude/projects``, ``convo_miner._resolve_wing`` takes its
AI-tool-path branch and files everything into the shared ``wing_api``
bucket.

That is a sensible default for a user with no per-project wings. For a user
who *does* keep per-project wings, it means automatic mining lands in a wing
their recall never queries — the writes and the reads never meet.

Opting in via ``MEMPALACE_HOOKS_WING_FROM_CWD`` (or ``hooks.wing_from_cwd``
in ``config.json``) makes both hooks pass an explicit ``--wing`` derived from
``cwd``. Explicit ``--wing`` is priority 1 in ``_resolve_wing``, so it beats
the AI-tool-path default. Default stays off; unset behavior is unchanged.

Written BEFORE the implementation.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from mempalace.config import MempalaceConfig, normalize_wing_name, wing_from_path
from mempalace.hook_shell import parse_precompact_payload, parse_stop_payload

REPO_ROOT = Path(__file__).resolve().parent.parent
SAVE_HOOK = REPO_ROOT / "hooks" / "mempal_save_hook.sh"
PRECOMPACT_HOOK = REPO_ROOT / "hooks" / "mempal_precompact_hook.sh"

# Verbatim from a real captured Stop payload
# (~/.mempalace/hook_state/last_input.log), not a hand-authored shape.
REAL_CWD = "/home/daedalus/linux/nexus"
REAL_WING = "home_daedalus_linux_nexus"


class TestWingFromPath:
    """``wing_from_path`` turns a filesystem path into a wing slug."""

    def test_converts_posix_path_to_slug(self):
        assert wing_from_path(REAL_CWD) == REAL_WING

    def test_agrees_with_normalize_wing_name_on_the_encoded_dirname(self):
        """The two routes into a wing name must produce the same string.

        Claude Code encodes the project dir as ``-home-daedalus-linux-nexus``;
        the miner's basename fallback runs that through
        ``normalize_wing_name``. If the cwd route disagrees by even one
        character, mining lands in a wing that recall never queries — the
        exact failure this feature exists to fix.
        """
        assert wing_from_path(REAL_CWD) == normalize_wing_name("-home-daedalus-linux-nexus")

    def test_trailing_separator_does_not_change_the_slug(self):
        assert wing_from_path(REAL_CWD + "/") == REAL_WING

    def test_windows_path_yields_a_sanitize_name_safe_slug(self):
        """Drive colons and backslashes must not survive into the slug.

        ``sanitize_name`` rejects ``/``, ``\\`` and any character outside
        ``[\\w .'-]``, so an unconverted ``C:\\Users\\me`` would raise at
        write time on Windows.
        """
        from mempalace.config import sanitize_name

        slug = wing_from_path(r"C:\Users\me\proj")
        assert "\\" not in slug and ":" not in slug
        assert sanitize_name(slug) == slug

    def test_empty_path_yields_empty_slug(self):
        assert wing_from_path("") == ""


class TestHooksWingFromCwdSetting:
    """The feature is opt-in: env var, then config.json, then off."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch):
        monkeypatch.delenv("MEMPALACE_HOOKS_WING_FROM_CWD", raising=False)

    def test_defaults_to_false(self, tmp_path):
        assert MempalaceConfig(config_dir=tmp_path).hooks_wing_from_cwd is False

    def test_enabled_by_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMPALACE_HOOKS_WING_FROM_CWD", "true")
        assert MempalaceConfig(config_dir=tmp_path).hooks_wing_from_cwd is True

    def test_env_var_false_wins_over_enabled_config_file(self, tmp_path, monkeypatch):
        (tmp_path / "config.json").write_text(json.dumps({"hooks": {"wing_from_cwd": True}}))
        monkeypatch.setenv("MEMPALACE_HOOKS_WING_FROM_CWD", "false")
        assert MempalaceConfig(config_dir=tmp_path).hooks_wing_from_cwd is False

    def test_enabled_by_config_file(self, tmp_path):
        (tmp_path / "config.json").write_text(json.dumps({"hooks": {"wing_from_cwd": True}}))
        assert MempalaceConfig(config_dir=tmp_path).hooks_wing_from_cwd is True


class TestPayloadParsersExposeCwd:
    """``cwd`` is in the payload; the parsers must stop discarding it."""

    def test_parse_stop_payload_returns_cwd(self):
        payload = {
            "session_id": "abc12345",
            "stop_hook_active": False,
            "transcript_path": "/tmp/t.jsonl",
            "cwd": REAL_CWD,
        }
        assert parse_stop_payload(payload)[3] == REAL_CWD

    def test_parse_precompact_payload_returns_cwd(self):
        payload = {
            "session_id": "abc12345",
            "transcript_path": "/tmp/t.jsonl",
            "cwd": REAL_CWD,
        }
        assert parse_precompact_payload(payload)[2] == REAL_CWD

    def test_missing_cwd_is_empty_not_an_error(self):
        assert parse_stop_payload({"session_id": "a"})[3] == ""
        assert parse_precompact_payload({"session_id": "a"})[2] == ""


# ── Shell-facing contract ────────────────────────────────────────────────
# hook_shell prints one sanitized value per line and the hooks read them
# back with fixed ``sed -n 'Np'`` offsets. The wing is appended as the LAST
# line of each parser's output so existing offsets do not shift.

pytestmark_posix = pytest.mark.skipif(
    os.name == "nt", reason="bash hook scripts are POSIX-only"
)


def _run_hook_shell(command: str, payload: dict, home: Path, env_extra: dict | None = None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("MEMPALACE_HOOKS_WING_FROM_CWD", None)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run(
        ["python3", "-m", "mempalace.hook_shell", command],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert p.returncode == 0, f"rc={p.returncode} stderr={p.stderr!r}"
    return p.stdout.splitlines()


class TestHookShellEmitsWingLine:
    """The wing line is emitted only when the feature is enabled."""

    def _payload(self):
        return {
            "session_id": "abc12345",
            "stop_hook_active": False,
            "transcript_path": "/tmp/t.jsonl",
            "cwd": REAL_CWD,
        }

    def test_parse_stop_appends_wing_when_enabled(self, tmp_path):
        lines = _run_hook_shell(
            "parse-stop",
            self._payload(),
            tmp_path,
            {"MEMPALACE_HOOKS_WING_FROM_CWD": "true"},
        )
        assert lines[0] == "__MEMPAL_PARSE_OK__"
        assert lines[4] == REAL_WING

    def test_parse_stop_wing_line_is_empty_when_disabled(self, tmp_path):
        lines = _run_hook_shell("parse-stop", self._payload(), tmp_path)
        assert lines[4] == ""

    def test_existing_line_offsets_are_unchanged(self, tmp_path):
        """Regression: the hooks read fields by fixed line number."""
        lines = _run_hook_shell(
            "parse-stop",
            self._payload(),
            tmp_path,
            {"MEMPALACE_HOOKS_WING_FROM_CWD": "true"},
        )
        assert lines[1] == "abc12345"
        assert lines[2] == "False"
        assert lines[3] == "/tmp/t.jsonl"

    def test_parse_precompact_appends_wing_when_enabled(self, tmp_path):
        lines = _run_hook_shell(
            "parse-precompact",
            {"session_id": "abc12345", "transcript_path": "/tmp/t.jsonl", "cwd": REAL_CWD},
            tmp_path,
            {"MEMPALACE_HOOKS_WING_FROM_CWD": "true"},
        )
        assert lines[1] == "abc12345"
        assert lines[2] == "/tmp/t.jsonl"
        assert lines[3] == REAL_WING


# ── End-to-end: the hooks must actually pass --wing to `mempalace mine` ──


def _stub_python(tmp_path: Path, record: Path) -> Path:
    """A python shim that records `-m mempalace mine` calls, delegates the rest."""
    stub = tmp_path / "stub_python"
    stub.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f'    *"-m mempalace mine"*) echo "$*" >> "{record}" ; exit 0 ;;\n'
        '    *) exec python3 "$@" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _transcript_with_human_messages(path: Path, count: int) -> None:
    path.write_text(
        "".join(
            json.dumps({"message": {"role": "user", "content": f"m{i}"}}) + "\n"
            for i in range(count)
        ),
        encoding="utf-8",
    )


def _poll(record: Path, timeout: float = 15.0) -> str:
    """The save hook backgrounds its mine call, so the record appears late."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if record.is_file() and record.read_text().strip():
            return record.read_text()
        time.sleep(0.1)
    return record.read_text() if record.is_file() else ""


@pytestmark_posix
class TestSaveHookPassesWing:
    def _run(self, tmp_path: Path, env_extra: dict) -> str:
        home = tmp_path / "home"
        home.mkdir()
        # The hook mines the transcript's PARENT dir, and _is_ai_tool_path
        # keys off `.claude/projects` — reproduce that real layout.
        projects = home / ".claude" / "projects" / "-home-daedalus-linux-nexus"
        projects.mkdir(parents=True)
        transcript = projects / "session.jsonl"
        _transcript_with_human_messages(transcript, 20)

        record = tmp_path / "mine_calls.txt"
        env = {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "MEMPAL_PYTHON": str(_stub_python(tmp_path, record)),
        }
        env.update(env_extra)
        p = subprocess.run(
            ["bash", str(SAVE_HOOK)],
            input=json.dumps(
                {
                    "session_id": "abc12345",
                    "stop_hook_active": False,
                    "transcript_path": str(transcript),
                    "cwd": REAL_CWD,
                }
            ),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert p.returncode == 0, f"rc={p.returncode} stderr={p.stderr!r}"
        return _poll(record)

    def test_passes_cwd_derived_wing_when_enabled(self, tmp_path):
        calls = self._run(tmp_path, {"MEMPALACE_HOOKS_WING_FROM_CWD": "true"})
        assert "mine" in calls, f"hook never invoked the miner; got {calls!r}"
        assert f"--wing {REAL_WING}" in calls, (
            f"expected --wing {REAL_WING} in the mine call; got {calls!r}"
        )

    def test_omits_wing_when_disabled(self, tmp_path):
        calls = self._run(tmp_path, {})
        assert "mine" in calls, f"hook never invoked the miner; got {calls!r}"
        assert "--wing" not in calls, (
            f"default behavior must be unchanged (no --wing); got {calls!r}"
        )
