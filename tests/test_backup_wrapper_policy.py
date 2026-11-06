"""Host backup wrapper policy: a failed upload must not block the next night.

The operator's host wrapper lives outside this repository. These tests run two
reduced, synthetic copies of it: ``host_backup_wrapper_current.sh`` keeps its
staging on error, ``host_backup_wrapper.sh`` is the required policy (remove the
plaintext snapshot and the hook's staging, keep the private log). The hook is a
stand-in that writes a snapshot and applies the hook's disk precheck, which
counts whatever an earlier run left in the staging directory.
"""

import difflib
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="host wrappers are Linux shell")

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXED = FIXTURES / "host_backup_wrapper.sh"
CURRENT = FIXTURES / "host_backup_wrapper_current.sh"
SNAPSHOT_BYTES = 4096

# Stand-in for the backup hook: clear its own host-side staging, refuse when the
# free space (budget minus earlier leftovers) cannot hold one more snapshot, else
# publish one snapshot directory into the staging directory it was given.
HOOK = r"""#!/usr/bin/env bash
set -Eeuo pipefail
dir=$1
rm -rf -- "${dir:?}/.commonplace-staging"
install -d -m 0700 -- "$dir/.commonplace-staging"
leftover=$(find "$dir" -type f -printf '%s\n' | awk '{ s += $1 } END { print s + 0 }')
if (( leftover + SNAPSHOT_BYTES > FREE_BYTES )); then
    printf 'commonplace backup failed: disk precheck (%s bytes left over)\n' "$leftover" >&2
    exit 1
fi
stamp=$(date -u +%Y%m%dT%H%M%S.%NZ)-$$
mkdir -m 0700 -- "$dir/.commonplace-staging/$stamp"
head -c "$SNAPSHOT_BYTES" /dev/zero > "$dir/.commonplace-staging/$stamp/control.sqlite3"
printf '{}\n' > "$dir/.commonplace-staging/$stamp/manifest.json"
mv -T -- "$dir/.commonplace-staging/$stamp" "$dir/commonplace-$stamp"
"""

# Fake restic: logs each call; `backup` fails while the fail counter is positive.
RESTIC = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$RESTIC_CALLS"
if [[ $1 == backup ]]; then
    remaining=$(cat "$RESTIC_FAILS")
    if (( remaining > 0 )); then
        echo $(( remaining - 1 )) > "$RESTIC_FAILS"
        echo 'Fatal: unable to save snapshot' >&2
        exit 1
    fi
fi
"""


@pytest.fixture
def host(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("backup-hook", HOOK), ("restic", RESTIC)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    stage = tmp_path / "stage"
    logs = tmp_path / "logs"
    logs.mkdir()
    fails = tmp_path / "restic-fails"
    calls = tmp_path / "restic-calls"
    env = {
        "PATH": f"{bin_dir}:{os.defpath}",
        "BACKUP_HOOK": str(bin_dir / "backup-hook"),
        "STAGE": str(stage),
        "LOG_DIR": str(logs),
        "BACKUP_ID": "test-deployment",
        "RESTIC_FAILS": str(fails),
        "RESTIC_CALLS": str(calls),
        "SNAPSHOT_BYTES": str(SNAPSHOT_BYTES),
        # Room for one snapshot plus slack, not for a retained one as well.
        "FREE_BYTES": str(SNAPSHOT_BYTES + SNAPSHOT_BYTES // 2),
    }

    def run(wrapper, restic_fails=0):
        fails.write_text(f"{restic_fails}\n")
        calls.write_text("")
        result = subprocess.run(
            ["bash", str(wrapper)],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result, calls.read_text().splitlines()

    return run, stage, logs


def plaintext(stage):
    return sorted(
        path.name for path in stage.iterdir() if path.name.startswith(("commonplace-", "."))
    )


def test_wrapper_restic_failure_then_next_precheck_passes(host):
    run, stage, logs = host
    result, calls = run(FIXED, restic_fails=1)
    assert result.returncode != 0
    assert [call.split()[0] for call in calls] == ["backup"]
    # No plaintext snapshot or hook staging survives the failed upload ...
    assert plaintext(stage) == []
    # ... but the private diagnostic log does.
    kept = list(logs.iterdir())
    assert len(kept) == 1
    assert kept[0].stat().st_mode & 0o777 == 0o600
    assert f"details: {kept[0]}" in result.stderr
    assert "unable to save snapshot" in kept[0].read_text()

    result, calls = run(FIXED)
    assert result.returncode == 0, result.stderr
    assert [call.split()[0] for call in calls] == ["backup", "forget"]
    assert plaintext(stage) == []


def test_current_wrapper_keeps_staging_so_next_precheck_refuses(host):
    """Why the wrapper cleanup is a required rollout step."""
    run, stage, logs = host
    result, _ = run(CURRENT, restic_fails=1)
    assert result.returncode != 0
    assert [name for name in plaintext(stage) if name.startswith("commonplace-")]

    result, calls = run(CURRENT)
    assert result.returncode != 0
    assert calls == []
    assert "disk precheck" in "".join(path.read_text() for path in logs.iterdir())


@pytest.mark.parametrize("wrapper", [FIXED, CURRENT], ids=["fixed", "current"])
def test_wrapper_success_leaves_no_plaintext_or_log(host, wrapper):
    run, stage, logs = host
    result, calls = run(wrapper)
    assert result.returncode == 0, result.stderr
    assert [call.split()[0] for call in calls] == ["backup", "forget"]
    assert plaintext(stage) == []
    assert list(logs.iterdir()) == []


def test_fixed_wrapper_changes_only_the_error_cleanup():
    """The operator applies a small diff: one cleanup line in on_error."""
    current = CURRENT.read_text().splitlines()
    fixed = FIXED.read_text().splitlines()
    changed = [
        line
        for line in difflib.unified_diff(current, fixed, lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    code = [line for line in changed if not line[1:].lstrip().startswith("#")]
    assert code == [
        '+        rm -rf -- "$STAGE"/commonplace-* "$STAGE/.commonplace-staging" || status=1'
    ]
    body = FIXED.read_text().split("on_error() {", 1)[1].split("\n}\n", 1)[0]
    assert '"$STAGE"/commonplace-*' in body
    assert 'rm -f -- "$log"' in body  # the log is removed on success only
