from __future__ import annotations

# Temporary owner-controlled CI driver. It deletes itself after verification.

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "mempalace" / "hooks_cli.py"
CHANGED = [
    "mempalace/config.py",
    "mempalace/hooks_cli.py",
    "tests/test_hooks_cli.py",
]


def run(*args: str) -> None:
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
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def main() -> None:
    text = HOOKS.read_text(encoding="utf-8")
    old = "    wing: str | None = None,\n"
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one Python-3.9-incompatible annotation, found {count}")
    HOOKS.write_text(
        text.replace(old, "    wing: Optional[str] = None,\n", 1),
        encoding="utf-8",
    )

    run("ruff", "format", *CHANGED)
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
    run("ruff", "check", *CHANGED)
    run("ruff", "format", "--check", *CHANGED)
    run(sys.executable, "-m", "compileall", "-q", *CHANGED)
    run("git", "diff", "--check")


if __name__ == "__main__":
    main()
