"""One wing slug for every producer, and the aliases that keep old wings reachable."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mempalace.config import WORKSTATION_WINGS, normalize_wing_name, sanitize_name, wing_aliases

HOOKS = Path(__file__).resolve().parent.parent / "hooks"

# Directory names the shell hooks must slug exactly like Python.
SHELL_CORPUS = [
    "myproject",
    "MyApp",
    "my-app",
    "my app",
    "React Native",
    "claude-code",
    "my.app",
    "v1.2.3",
    "o'brien",
    "my..app",
    "..hidden..",
    "+project",
    "foo+bar",
    "a - b",
    "__init__",
    "C++ Tool",
    "@my-org",
    "My Project (v2)",
    "x" * 200,
    # Non-ASCII names go through Python itself.
    "Café Notes",
    "日本語",
    "日本語-プロジェクト",
    "émigré café",
]


@pytest.mark.parametrize(
    "name, slug",
    [
        ("myapp", "myapp"),
        ("My App", "my_app"),
        ("claude-code", "claude_code"),
        ("-home-user-proj", "home_user_proj"),
        ("my.app", "my.app"),
        ("o'brien", "o'brien"),
        ("my..app", "my.app"),
        ("+project", "project"),
        ("foo+bar", "foo_bar"),
        ("+", ""),
    ],
)
def test_normalize_wing_name(name, slug):
    assert normalize_wing_name(name) == slug


@pytest.mark.parametrize("name", SHELL_CORPUS)
def test_every_slug_is_a_valid_wing(name):
    slug = normalize_wing_name(name)
    if slug:
        assert sanitize_name(f"wing_{slug}") == f"wing_{slug}"


def test_aliases_cover_the_old_hook_cursor_and_agent_spellings():
    aliases = wing_aliases("my_app")
    assert aliases[0] == "my_app"
    assert {"my_app", "wing_my_app", "my-app", "wing_my-app"} <= set(aliases)
    # The old spelling a client still uses resolves to the same set.
    assert set(wing_aliases("wing_my_app")) >= {"my_app", "wing_my_app", "my-app"}
    assert set(wing_aliases("my-app")) >= {"my_app", "wing_my_app", "my-app"}
    # Cursor turned dots into underscores.
    assert "my_app" in wing_aliases("my.app")


def test_aliases_of_a_plain_wing_are_only_its_prefixed_twin():
    assert wing_aliases("backend") == ["backend", "wing_backend"]


def test_aliases_cover_the_old_cursor_bytes_rule():
    # The Cursor hooks turned each non-ASCII byte into `_` and squeezed runs.
    assert "caf_notes" in wing_aliases("café_notes")
    assert "caf_-notes" in wing_aliases("café_notes")


def test_aliases_skip_the_cursor_rule_when_it_drops_a_whole_word():
    # `日本_api` would reduce to `api`: an unrelated project, or a category wing.
    assert not {"api", "wing_api"} & set(wing_aliases("日本_api"))
    assert not {"x", "wing_x"} & set(wing_aliases("日本-x"))
    assert set(wing_aliases("日本_api")) == {
        "日本_api",
        "wing_日本_api",
        "日本-api",
        "wing_日本-api",
    }


def test_aliases_of_a_workstation_wing_include_the_old_home_diary_wing():
    home = normalize_wing_name(Path.home().name)
    for wing in WORKSTATION_WINGS:
        assert (f"wing_{home}" in wing_aliases(wing)) == bool(home)


def test_aliases_add_known_wings_that_slug_the_same():
    known = {"c++tool", "wing_C++Tool", "c-tools", "other"}
    assert wing_aliases("c_tool", known)[-2:] == ["c++tool", "wing_C++Tool"]
    assert "c-tools" not in wing_aliases("c_tool", known)


def _shell_slug(lib: Path, name: str) -> str:
    out = subprocess.run(
        ["bash", "-c", '. "$1" >/dev/null 2>&1; mempal_infer_wing "/work/$2"', "_", str(lib), name],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "MEMPAL_PYTHON": sys.executable},
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.mark.skipif(os.name == "nt", reason="bash hook scripts are POSIX-only")
@pytest.mark.parametrize(
    "harness, fallback", [("cursor", "cursor_session"), ("antigravity", "sessions")]
)
@pytest.mark.parametrize("name", SHELL_CORPUS)
def test_shell_hooks_slug_like_python(harness, fallback, name):
    expected = normalize_wing_name(name) or fallback
    assert _shell_slug(HOOKS / harness / "lib" / "common.sh", name) == expected


@pytest.mark.skipif(os.name == "nt", reason="bash hook scripts are POSIX-only")
@pytest.mark.parametrize(
    "harness, fallback", [("cursor", "cursor_session"), ("antigravity", "sessions")]
)
@pytest.mark.parametrize("name", SHELL_CORPUS)
def test_shell_hooks_parse_stdin_preserves_paths_and_wing_slugs(harness, fallback, name, tmp_path):
    lib = HOOKS / harness / "lib" / "common.sh"
    workspace = f"/Users/me/My Projects (Café)/{name}"
    transcript = f"{workspace}/session.jsonl"
    if harness == "cursor":
        payload = json.dumps(
            {
                "conversation_id": "conv-1",
                "transcript_path": transcript,
                "workspace_roots": [workspace],
            }
        )
        script = (
            '. "$1" >/dev/null 2>&1; mempal_parse_stdin "$2"; '
            'wing=$(mempal_infer_wing "$MEMPAL_WORKSPACE"); '
            'printf "%s\\n%s" "$MEMPAL_TRANSCRIPT" "$wing"'
        )
    else:
        payload = json.dumps(
            {
                "conversationId": "conv-1",
                "transcriptPath": transcript,
                "workspacePaths": [workspace],
                "invocationNum": 1,
            }
        )
        script = (
            '. "$1" >/dev/null 2>&1; parsed=$(mempal_parse_stdin "$2"); '
            'tp=$(printf "%s\\n" "$parsed" | sed -n "3p"); '
            'wp=$(printf "%s\\n" "$parsed" | sed -n "4p"); '
            'wing=$(mempal_infer_wing "$wp"); '
            'printf "%s\\n%s" "$tp" "$wing"'
        )

    out = subprocess.run(
        ["bash", "-c", script, "_", str(lib), payload],
        capture_output=True,
        encoding="utf-8",
        env={
            **os.environ,
            "MEMPAL_PYTHON": sys.executable,
            "MEMPAL_STATE_DIR": str(tmp_path),
        },
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    parsed_transcript, parsed_wing = out.stdout.split("\n", 1)
    assert parsed_transcript == transcript
    assert parsed_wing == (normalize_wing_name(name) or fallback)


@pytest.mark.skipif(os.name == "nt", reason="bash hook scripts are POSIX-only")
@pytest.mark.parametrize("harness", ["cursor", "antigravity"])
def test_shell_hooks_parse_stdin_strips_control_chars_without_shifting_lines(harness, tmp_path):
    lib = HOOKS / harness / "lib" / "common.sh"
    if harness == "cursor":
        payload = (
            '{"conversation_id":"conv-1\\nevil",'
            '"loop_count":4,'
            '"transcript_path":"~/a\\r\\nb\\ud800/t.jsonl",'
            '"workspace_roots":["C:\\\\Users\\\\me\\\\MyProj\\\\"],'
            '"status":"completed"}'
        )
        script = (
            '. "$1" >/dev/null 2>&1; mempal_parse_stdin "$2"; '
            'wing=$(mempal_infer_wing "$MEMPAL_WORKSPACE"); '
            'printf "%s|%s|%s|%s" "$MEMPAL_LOOP_COUNT" "$MEMPAL_TRANSCRIPT" "$wing" "$MEMPAL_STATUS"'
        )
        expected = f"4|{tmp_path}/ab/t.jsonl|myproj|completed"
    else:
        payload = (
            '{"conversationId":"conv-1\\nevil",'
            '"transcriptPath":"/tmp/a\\r\\nb\\ud800/t.jsonl",'
            '"workspacePaths":["C:\\\\Users\\\\me\\\\MyProj\\\\"],'
            '"invocationNum":7}'
        )
        script = (
            '. "$1" >/dev/null 2>&1; parsed=$(mempal_parse_stdin "$2"); '
            'tp=$(printf "%s\\n" "$parsed" | sed -n "3p"); '
            'wp=$(printf "%s\\n" "$parsed" | sed -n "4p"); '
            'wing=$(mempal_infer_wing "$wp"); '
            'inv=$(printf "%s\\n" "$parsed" | sed -n "9p"); '
            'printf "%s|%s|%s" "$tp" "$wing" "$inv"'
        )
        expected = "/tmp/ab/t.jsonl|myproj|7"

    out = subprocess.run(
        ["bash", "-c", script, "_", str(lib), payload],
        capture_output=True,
        encoding="utf-8",
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "MEMPAL_PYTHON": sys.executable,
            "MEMPAL_STATE_DIR": str(tmp_path),
        },
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == expected


@pytest.mark.skipif(os.name == "nt", reason="bash hook scripts are POSIX-only")
@pytest.mark.parametrize("harness", ["cursor", "antigravity"])
@pytest.mark.parametrize("name", ["Café Notes", "日本語-プロジェクト", "émigré café"])
def test_shell_hooks_non_ascii_slug_fallback_without_mempalace_import(harness, name, tmp_path):
    lib = HOOKS / harness / "lib" / "common.sh"
    # Poison cwd with re.py and json.py and unset PYTHONSAFEPATH so the
    # test verifies Python 3.10 sys.path[0] removal before stdlib imports.
    (tmp_path / "re.py").write_text("raise RuntimeError('cwd re.py imported')\n", encoding="utf-8")
    (tmp_path / "json.py").write_text(
        "raise RuntimeError('cwd json.py imported')\n", encoding="utf-8"
    )
    stub_py = tmp_path / "python3_no_site"
    stub_py.write_text(
        f'#!/bin/sh\nunset PYTHONSAFEPATH PYTHONPATH\nexec "{sys.executable}" -S "$@"\n',
        encoding="utf-8",
    )
    stub_py.chmod(0o755)
    out = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1" >/dev/null 2>&1; mempal_infer_wing "/work/$2"',
            "_",
            str(lib),
            name,
        ],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        env={
            **os.environ,
            "MEMPAL_PYTHON": str(stub_py),
            "MEMPAL_STATE_DIR": str(tmp_path),
        },
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == normalize_wing_name(name)
