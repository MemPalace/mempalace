"""Cross-platform end-to-end tests for the Antigravity Node.js hooks & standalone exporter.

Unlike `test_antigravity_hooks_shell.py` (which tests the POSIX `.sh` hooks and skips on
Windows), this suite exercises `plugins/mempalace/scripts/{save-hook,wake-hook,hook-common}.mjs`
and `hooks/antigravity/export_standalone.py` across Windows, macOS, and Linux.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from mempalace.config import normalize_wing_name
from tests.test_wing_slug import SHELL_CORPUS

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins" / "mempalace"
SCRIPTS_DIR = PLUGIN_DIR / "scripts"
HOOK_COMMON = SCRIPTS_DIR / "hook-common.mjs"
SAVE_HOOK = SCRIPTS_DIR / "save-hook.mjs"
WAKE_HOOK = SCRIPTS_DIR / "wake-hook.mjs"
EXPORTER_PATH = REPO_ROOT / "hooks" / "antigravity" / "export_standalone.py"

NODE_BIN = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE_BIN is None, reason="Node.js runtime not found on PATH")


def _load_exporter():
    spec = importlib.util.spec_from_file_location("export_standalone", EXPORTER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_node_hook(
    script: Path,
    stdin_payload: dict | str,
    *,
    home: Path,
    state_dir: Path,
    extra_env: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    stdin_text = (
        json.dumps(stdin_payload) if isinstance(stdin_payload, dict) else str(stdin_payload)
    )
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["MEMPAL_STATE_DIR"] = str(state_dir)
    for k in (
        "MEMPAL_DISABLE_HOOK",
        "MEMPALACE_HOOKS_AUTO_SAVE",
        "MEMPAL_SAVE_INTERVAL",
        "MEMPALACE_CONFIG_DIR",
    ):
        env.pop(k, None)
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [str(NODE_BIN), str(script)],
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(PLUGIN_DIR),
        env=env,
        timeout=timeout,
    )


def _poll_log(log_path: Path, needle: str, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.is_file():
            body = log_path.read_text(encoding="utf-8", errors="replace")
            if needle in body:
                return True
        time.sleep(0.05)
    return False


# ── Wing slug parity across full SHELL_CORPUS + Windows paths ─────────


def test_node_infer_wing_matches_python_corpus() -> None:
    """`inferWing` in hook-common.mjs must match `normalize_wing_name` on every corpus name."""
    probe_code = (
        f"import {{ inferWing }} from {json.dumps(HOOK_COMMON.as_uri())};"
        "const names = JSON.parse(process.argv[1]);"
        "const out = names.map((n) => inferWing('/work/' + n, 'sessions'));"
        "process.stdout.write(JSON.stringify(out));"
    )
    proc = subprocess.run(
        [str(NODE_BIN), "--input-type=module", "-e", probe_code, json.dumps(SHELL_CORPUS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    actual = json.loads(proc.stdout)
    expected = [normalize_wing_name(name) or "sessions" for name in SHELL_CORPUS]
    assert actual == expected


def test_node_parse_stdin_preserves_windows_drive_colons(tmp_path: Path) -> None:
    """Windows paths like `C:\\Users\\me\\transcript.jsonl` and `p:/MemPalace/mempalace`
    must keep their drive colons intact (regression test for bash colon-stripping bug).
    """
    payload = {
        "conversationId": "win-conv-42",
        "transcriptPath": r"C:\Users\me\brain\conv-42\transcript_full.jsonl",
        "workspacePaths": [r"p:\MemPalace\mempalace\\"],
        "fullyIdle": True,
        "invocationNum": 1,
    }
    probe_code = (
        f"import {{ parseHookStdin, inferWing }} from {json.dumps(HOOK_COMMON.as_uri())};"
        "const p = parseHookStdin(process.argv[1], process.argv[2]);"
        "process.stdout.write(JSON.stringify({ ...p, wing: inferWing(p.workspacePath) }));"
    )
    proc = subprocess.run(
        [
            str(NODE_BIN),
            "--input-type=module",
            "-e",
            probe_code,
            json.dumps(payload),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout)
    assert res["transcriptPath"] == r"C:\Users\me\brain\conv-42\transcript_full.jsonl"
    assert res["workspacePath"] == r"p:\MemPalace\mempalace\\"
    assert res["wing"] == "mempalace"


# ── Kill switches ─────────────────────────────────────────────────────


def test_palace_nuke_leaves_zero_footprint(tmp_path: Path) -> None:
    """When ~/.mempalace does not exist, neither hook creates state files or logs."""
    home = tmp_path / "empty_home"
    home.mkdir()
    state_dir = home / ".mempalace" / "hook_state"

    res_save = _run_node_hook(
        SAVE_HOOK,
        {"conversationId": "c1", "fullyIdle": True},
        home=home,
        state_dir=state_dir,
    )
    assert res_save.returncode == 0
    assert res_save.stdout.strip() == "{}"
    assert not state_dir.exists()

    res_wake = _run_node_hook(
        WAKE_HOOK,
        {"conversationId": "c1", "invocationNum": 1},
        home=home,
        state_dir=state_dir,
    )
    assert res_wake.returncode == 0
    assert res_wake.stdout.strip() == "{}"
    assert not state_dir.exists()


@pytest.mark.parametrize(
    "extra_env,config_body",
    [
        ({"MEMPAL_DISABLE_HOOK": "1"}, None),
        ({"MEMPALACE_HOOKS_AUTO_SAVE": "false"}, None),
        ({}, {"hooks": {"auto_save": False}}),
    ],
)
def test_kill_switches_short_circuit_both_hooks(
    tmp_path: Path, extra_env: dict[str, str], config_body: dict | None
) -> None:
    home = tmp_path / "home"
    palace_dir = home / ".mempalace"
    palace_dir.mkdir(parents=True)
    if config_body is not None:
        (palace_dir / "config.json").write_text(json.dumps(config_body), encoding="utf-8")
    state_dir = tmp_path / "state"

    res = _run_node_hook(
        SAVE_HOOK,
        {
            "conversationId": "conv-ks",
            "fullyIdle": True,
            "terminationReason": "model_stop",
            "transcriptPath": str(tmp_path / "t.jsonl"),
        },
        home=home,
        state_dir=state_dir,
        extra_env={**extra_env, "MEMPAL_SAVE_INTERVAL": "1"},
    )
    assert res.returncode == 0
    assert res.stdout.strip() == "{}"
    assert not (state_dir / "antigravity_save_count_conv-ks").exists()


# ── Save hook (Stop event) ────────────────────────────────────────────


def test_save_hook_defers_until_fully_idle_and_interval_reached(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".mempalace").mkdir(parents=True)
    state_dir = tmp_path / "state"
    transcript_dir = tmp_path / "conv_logs"
    transcript_dir.mkdir()
    transcript = transcript_dir / "transcript.jsonl"
    transcript.write_text('{"step_index":0}\n', encoding="utf-8")

    # Stub Python runner that records CLI invocations without needing full Chroma cold start
    marker_file = tmp_path / "mine_invoked.json"
    stub_py = tmp_path / "stub_py.py"
    stub_py.write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['-m', 'mempalace', '--version']:\n"
        "    print('3.12.0')\n"
        "    sys.exit(0)\n"
        f"pathlib.Path({str(marker_file)!r}).write_text(json.dumps(args), encoding='utf-8')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )

    # Create a tiny wrapper script/cmd or invoke via sys.executable?
    # Note: save-hook.mjs executes `pythonBin -m mempalace ...`.
    # We can provide a fake `mempalace` package in PYTHONPATH with sys.executable!
    fake_pkg = tmp_path / "fake_site" / "mempalace"
    fake_pkg.mkdir(parents=True)
    (fake_pkg / "__init__.py").write_text("", encoding="utf-8")
    (fake_pkg / "__main__.py").write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        "    print('3.12.0')\n"
        "    sys.exit(0)\n"
        f"pathlib.Path({str(marker_file)!r}).write_text(json.dumps(args), encoding='utf-8')\n",
        encoding="utf-8",
    )

    env = {
        "MEMPAL_PYTHON": sys.executable,
        "MEMPAL_SAVE_INTERVAL": "2",
        "PYTHONPATH": str(tmp_path / "fake_site"),
    }

    # Non-idle turn (fullyIdle is False) defers without touching counter
    r1 = _run_node_hook(
        SAVE_HOOK,
        {
            "conversationId": "conv-save",
            "fullyIdle": False,
            "terminationReason": "no_tool_call",
            "workspacePaths": [str(tmp_path / "MyRepo")],
            "transcriptPath": str(transcript),
        },
        home=home,
        state_dir=state_dir,
        extra_env=env,
    )
    assert r1.returncode == 0 and r1.stdout.strip() == "{}"
    assert not (state_dir / "antigravity_save_count_conv-save").exists()

    # 2. First idle Stop -> count = 1 (below interval 2)
    r2 = _run_node_hook(
        SAVE_HOOK,
        {
            "conversationId": "conv-save",
            "fullyIdle": True,
            "terminationReason": "model_stop",
            "workspacePaths": [str(tmp_path / "MyRepo")],
            "transcriptPath": str(transcript),
        },
        home=home,
        state_dir=state_dir,
        extra_env=env,
    )
    assert r2.returncode == 0 and r2.stdout.strip() == "{}"
    assert (state_dir / "antigravity_save_count_conv-save").read_text(encoding="utf-8") == "1"
    assert not marker_file.exists()

    # 3. Second idle Stop -> count = 2 -> triggers detached mine!
    r3 = _run_node_hook(
        SAVE_HOOK,
        {
            "conversationId": "conv-save",
            "fullyIdle": True,
            "terminationReason": "model_stop",
            "workspacePaths": [str(tmp_path / "MyRepo")],
            "transcriptPath": str(transcript),
        },
        home=home,
        state_dir=state_dir,
        extra_env=env,
    )
    assert r3.returncode == 0
    assert r3.stdout.strip() == "{}"
    assert "continue" not in r3.stdout

    log_path = state_dir / "antigravity_hook.log"
    assert _poll_log(log_path, "background mine finished wing=myrepo")
    recorded_args = json.loads(marker_file.read_text(encoding="utf-8"))
    assert recorded_args == ["mine", str(transcript_dir), "--mode", "convos", "--wing", "myrepo"]


# ── Wake hook (PreInvocation event) ───────────────────────────────────


def test_wake_hook_injects_verbatim_once_per_conversation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".mempalace").mkdir(parents=True)
    state_dir = tmp_path / "state"

    verbatim_memory = "## Palace Wake-Up\nExact verbatim fact: port=5432 & token='literal'"
    fake_pkg = tmp_path / "fake_site" / "mempalace"
    fake_pkg.mkdir(parents=True)
    (fake_pkg / "__init__.py").write_text("", encoding="utf-8")
    (fake_pkg / "__main__.py").write_text(
        "import sys\n"
        "if sys.argv[1:3] == ['wake-up', '--wing']:\n"
        f"    sys.stdout.write({verbatim_memory!r} + '\\n')\n",
        encoding="utf-8",
    )
    env = {
        "MEMPAL_PYTHON": sys.executable,
        "PYTHONPATH": str(tmp_path / "fake_site"),
    }

    # Second turn (invocationNum == 2) must be ignored
    r_turn2 = _run_node_hook(
        WAKE_HOOK,
        {
            "conversationId": "conv-wake",
            "invocationNum": 2,
            "workspacePaths": [str(tmp_path / "MyProject")],
        },
        home=home,
        state_dir=state_dir,
        extra_env=env,
    )
    assert r_turn2.returncode == 0
    assert r_turn2.stdout.strip() == "{}"

    # First turn (invocationNum == 1) injects verbatim memory
    r_first = _run_node_hook(
        WAKE_HOOK,
        {
            "conversationId": "conv-wake",
            "invocationNum": 1,
            "workspacePaths": [str(tmp_path / "MyProject")],
        },
        home=home,
        state_dir=state_dir,
        extra_env=env,
    )
    assert r_first.returncode == 0
    parsed = json.loads(r_first.stdout)
    assert parsed == {"injectSteps": [{"ephemeralMessage": verbatim_memory}]}
    assert "decision" not in parsed

    # Repeated invocationNum=1 for the same conversation is blocked by the atomic mkdir guard
    r_repeat = _run_node_hook(
        WAKE_HOOK,
        {
            "conversationId": "conv-wake",
            "invocationNum": 1,
            "workspacePaths": [str(tmp_path / "MyProject")],
        },
        home=home,
        state_dir=state_dir,
        extra_env=env,
    )
    assert r_repeat.returncode == 0
    assert r_repeat.stdout.strip() == "{}"


# ── Standalone lightweight repository exporter ────────────────────────


def test_export_standalone_bundle_creates_clean_verified_archive(tmp_path: Path) -> None:
    exporter = _load_exporter()
    out_dir = tmp_path / "mempalace-antigravity"
    zip_out = tmp_path / "mempalace-antigravity.zip"

    summary = exporter.export_standalone_bundle(
        out_dir,
        repo_url="https://github.com/MemPalace/mempalace-antigravity",
        init_git=False,
        zip_path=zip_out,
        validate=True,
    )

    assert out_dir.is_dir()
    assert zip_out.is_file()
    assert "plugin.json" in summary["files"]
    assert "hooks.json" in summary["files"]
    assert "scripts/save-hook.mjs" in summary["files"]
    assert "scripts/wake-hook.mjs" in summary["files"]
    assert "sidecars/palace/sidecar.json" in summary["files"]

    exported_manifest = json.loads((out_dir / "plugin.json").read_text(encoding="utf-8"))
    assert exported_manifest["repository"] == "https://github.com/MemPalace/mempalace-antigravity"

    # Verify zip archive preserves 0755 executable permissions on scripts/*
    with zipfile.ZipFile(zip_out, "r") as zf:
        save_info = zf.getinfo("mempalace/scripts/save-hook.mjs")
        unix_mode = stat.S_IMODE(save_info.external_attr >> 16)
        assert unix_mode == 0o755
