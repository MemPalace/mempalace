"""Deployment invariants and the host backup hook, without Docker or a network."""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
DOCKERFILE = DEPLOY / "commonplace" / "Dockerfile"
COMPOSE = DEPLOY / "compose.host.yml.example"
CADDY = DEPLOY / "commonplace.caddy.example"
HOOK = ROOT / "backup-hook.sh"


def test_image_installs_repo_and_runs_as_nonroot():
    text = DOCKERFILE.read_text()
    assert text.startswith("FROM python:3.12-slim\n")
    assert 'RUN pip install ".[commonplace]"\n' in text
    users = re.findall(r"^USER (.+)$", text, re.MULTILINE)
    assert users == ["10001:10001"]
    assert "COMMONPLACE_DATA=/data" in text
    assert 'VOLUME ["/data"]' in text
    command = json.loads(re.search(r"^CMD (.+)$", text, re.MULTILINE)[1])
    assert command == ["commonplace", "serve", "--host", "0.0.0.0", "--port", "8765"]


def test_model_is_preloaded_as_runtime_user_outside_data_volume():
    text = DOCKERFILE.read_text()
    assert "HOME=/home/commonplace" in text
    runtime_build = text.split("USER 10001:10001\n", 1)[1]
    assert "from mempalace.embedding import get_embedding_function" in runtime_build
    assert "device='cpu', model='minilm'" in runtime_build
    assert "input=['Commonplace build check']" in runtime_build
    assert "assert len(vectors[0]) == 384" in runtime_build
    assert "COPY . " not in text  # Do not send operator secrets into the image.


def test_compose_uses_repo_root_and_exact_loopback_publication():
    text = COMPOSE.read_text()
    assert re.search(r"\A(?:#[^\n]*\n)*services:\n  commonplace:\n", text)
    assert "context: ${COMMONPLACE_SOURCE_DIR:?" in text
    assert "dockerfile: deploy/commonplace/Dockerfile" in text
    ports = re.search(r"    ports:\n((?:      .+\n)+)", text)[1]
    assert ports.splitlines() == ['      - "127.0.0.1:8765:8765"']
    assert "network_mode:" not in text
    assert "privileged:" not in text


def test_compose_persists_data_limits_memory_and_loads_operator_env():
    text = COMPOSE.read_text()
    assert "${COMMONPLACE_DATA_DIR:?" in text
    assert "directory}:/data\n" in text
    assert "- ${COMMONPLACE_ENV_FILE:?" in text
    assert "COMMONPLACE_BASE_URL:-https://hub.example.com" in text
    assert "restart: unless-stopped\n" in text
    assert "mem_limit: 3g\n" in text
    assert "init: true\n" in text


def test_healthcheck_uses_python_stdlib_and_healthz_alias():
    text = COMPOSE.read_text().split("    healthcheck:\n", 1)[1]
    assert "- CMD\n        - python\n        - -c\n" in text
    assert "urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=5)" in text
    assert "assert r.status == 200" in text
    assert "curl" not in text
    assert "timeout: 10s" in text
    assert "retries: 3" in text


def test_caddy_preserves_api_paths_and_has_exclusive_static_fallback():
    active = "\n".join(line for line in CADDY.read_text().splitlines() if not line.startswith("#"))
    assert active.count("hub.example.com {") == 1
    paths = re.search(r"@backend path (.+)", active)[1].split()
    assert set(paths) == {
        "/app*",
        "/api/*",
        "/mcp",
        "/health",
        "/healthz",
        "/s/*",
        "/oauth/*",
        "/.well-known/*",
    }
    assert "handle_path" not in active
    backend, fallback = active.split("handle @backend {", 1)[1].split("\n\thandle {", 1)
    fallback = fallback.split("\n}\n", 1)[0]
    assert "bind 192.0.2.10" in active
    assert "reverse_proxy 127.0.0.1:8765" in backend
    assert "flush_interval -1" in backend
    assert "Content-Security-Policy" not in backend
    assert "root * /path/to/commonplace/app/landing/public" in fallback
    assert "file_server" in fallback
    assert "reverse_proxy" not in fallback
    assert "Content-Security-Policy" in fallback
    assert "cmmn.dev {" not in active


def test_caddy_security_headers_and_future_short_links():
    text = CADDY.read_text()
    for header in ("X-Content-Type-Options nosniff", "Referrer-Policy no-referrer", "-Server"):
        assert header in text
    commented = "\n".join(line[2:] for line in text.splitlines() if line.startswith("# "))
    destinations = {
        "gh": "https://github.com/140er/mempalace",
        "mempalace": "https://github.com/MemPalace/mempalace",
        "install": "https://commonplace.run/#install",
        "connect": "https://commonplace.run/connect/",
        "claude": "https://commonplace.run/connect/#claude-code",
        "codex": "https://commonplace.run/connect/#codex",
        "cursor": "https://commonplace.run/connect/#cursor",
        "antigravity": "https://commonplace.run/connect/#antigravity",
        "cloud": "https://commonplace.run/connect/#cloud",
        "local": "https://commonplace.run/connect/#local",
    }
    for name, destination in destinations.items():
        assert f"@{name} path /{name} /{name}/" in commented
        assert f"redir @{name} {destination} 302" in commented
    assert "redir https://commonplace.run/ 302" in commented
    assert "app.cmmn.dev {" in commented


def test_hook_is_executable_and_uses_only_host_managed_restic():
    assert HOOK.stat().st_mode & 0o111
    text = HOOK.read_text()
    assert "set -Eeuo pipefail" in text
    assert 'docker compose --env-file "$env_file" -f "$compose_file"' in text
    assert "exec -T commonplace commonplace backup --help" in text
    assert "stop --timeout 60 commonplace" in text
    assert '--volume "$staging_parent:/backup"' in text
    assert 'container_staging="/backup/$stamp"' in text
    assert "install -d -o 10001 -g 10001 -m 0700" in text
    assert "flock -n 9" in text
    assert 'mv -T -- "$host_staging" "$destination"' in text
    commands = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    assert not re.search(r"\brestic\s+(backup|forget|prune|init)\b", commands)


@pytest.mark.parametrize("arguments", [[], ["relative"], ["/nonexistent/commonplace-backups"]])
def test_hook_rejects_invalid_destination_with_one_failure_line(arguments):
    result = subprocess.run(["bash", str(HOOK), *arguments], capture_output=True, text=True)
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.count("\n") == 1
    assert result.stderr.startswith("commonplace backup failed: setup")


# This fake only models the external container/permission boundary. Each test
# runs the actual hook, including its traps, lock, validation and atomic move.
FAKE_DOCKER = """#!/usr/bin/python3
import json, os, pathlib, sys
args = sys.argv[1:]
if pathlib.Path(sys.argv[0]).name == 'install':
    pathlib.Path(args[-1]).mkdir(parents=True, exist_ok=True)
    sys.exit(0)
with open(os.environ['FAKE_COMMANDS'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
mode = os.environ.get('FAKE_FAIL', '')
if 'ps' in args:
    print('test-container')
elif 'exec' in args:
    if mode == 'preflight': sys.exit(9)
elif 'stop' in args:
    if mode == 'stop': sys.exit(9)
elif 'start' in args:
    if mode == 'restart': sys.exit(9)
elif 'run' in args:
    if mode == 'snapshot': sys.exit(9)
    mount = args[args.index('--volume') + 1]
    assert mount.endswith(':/backup'), mount
    destination = pathlib.Path(mount[:-len(':/backup')]) / pathlib.Path(args[-1]).name
    destination.mkdir()
    (destination / 'control.sqlite3').write_bytes(b'consistent snapshot')
    if mode != 'manifest':
        (destination / 'manifest.json').write_text('invalid' if mode == 'corrupt' else '{}')
else:
    sys.exit(10)
"""


def run_hook(tmp_path, fail="", env_overrides=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    (bin_dir / "install").symlink_to(docker)
    backups = tmp_path / "backups"
    backups.mkdir()
    commands = tmp_path / "commands.jsonl"
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    for key in ("COMMONPLACE_DEPLOY_DIR", "COMMONPLACE_ENV_FILE", "COMPOSE_FILE"):
        env.pop(key, None)
    env.update(FAKE_COMMANDS=str(commands), FAKE_FAIL=fail)
    env.update(env_overrides or {})
    result = subprocess.run(
        ["bash", str(HOOK), str(backups)], env=env, capture_output=True, text=True, timeout=10
    )
    calls = (
        [json.loads(line) for line in commands.read_text().splitlines()]
        if commands.exists()
        else []
    )
    # Failure logs are deliberately preserved by the production hook; clean
    # up this test's log after checking the diagnostic points to a real file.
    log_match = re.search(r"details: (/tmp/commonplace-backup\.[^)]+)", result.stderr)
    if log_match:
        log_path = Path(log_match[1])
        assert log_path.is_file()
        log_path.unlink()
    return result, backups, calls


def test_hook_publishes_complete_snapshot_after_restarting_gateway(tmp_path):
    result, backups, calls = run_hook(tmp_path)
    assert result.returncode == 0, result.stderr
    snapshots = list(backups.glob("commonplace-*"))
    assert len(snapshots) == 1
    assert (snapshots[0] / "control.sqlite3").read_bytes() == b"consistent snapshot"
    assert json.loads((snapshots[0] / "manifest.json").read_text()) == {}
    assert list((backups / ".commonplace-staging").iterdir()) == []
    actions = [
        next(word for word in call if word in {"ps", "exec", "stop", "run", "start"})
        for call in calls
    ]
    assert actions == ["ps", "exec", "stop", "run", "start"]
    for call in calls:
        assert call[:4] == ["compose", "--env-file", str(ROOT.parent / ".env"), "-f"]
        assert Path(call[4]) == ROOT.parent / "compose.yml"


@pytest.mark.parametrize("individual_paths", [False, True])
def test_hook_uses_external_operator_configuration(tmp_path, individual_paths):
    operator_dir = tmp_path / "operator config"
    env_file = operator_dir / "custom.env" if individual_paths else operator_dir / ".env"
    compose_file = operator_dir / "custom.yml" if individual_paths else operator_dir / "compose.yml"
    overrides = {"COMMONPLACE_DEPLOY_DIR": str(operator_dir)}
    if individual_paths:
        overrides.update(COMMONPLACE_ENV_FILE=str(env_file), COMPOSE_FILE=str(compose_file))
    result, _, calls = run_hook(tmp_path, env_overrides=overrides)
    assert result.returncode == 0, result.stderr
    assert calls
    for call in calls:
        assert call[:5] == ["compose", "--env-file", str(env_file), "-f", str(compose_file)]


@pytest.mark.parametrize("name", ["COMMONPLACE_DEPLOY_DIR", "COMMONPLACE_ENV_FILE", "COMPOSE_FILE"])
@pytest.mark.parametrize("value", ["relative/config", "~/config"])
def test_hook_rejects_relative_overrides_before_touching_gateway(tmp_path, name, value):
    result, backups, calls = run_hook(tmp_path, env_overrides={name: value})
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.count("\n") == 1
    assert f"{name} must be an absolute path" in result.stderr
    assert calls == []
    assert list(backups.iterdir()) == []


@pytest.mark.parametrize(
    "fail", ["preflight", "stop", "snapshot", "restart", "manifest", "corrupt"]
)
def test_hook_fails_closed_and_recovers_gateway(tmp_path, fail):
    result, backups, calls = run_hook(tmp_path, fail)
    assert result.returncode != 0
    assert result.stderr.count("\n") == 1
    assert result.stderr.startswith("commonplace backup failed:")
    assert list(backups.glob("commonplace-*")) == []
    if fail == "preflight":
        assert not any("stop" in call or "run" in call for call in calls)
    else:
        assert "start" in calls[-1]
    if fail == "snapshot":
        assert "snapshot" in result.stderr
    if fail == "restart":
        assert "gateway restart failed" in result.stderr


def test_backup_shell_syntax():
    result = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert shlex.split(HOOK.read_text().splitlines()[0][2:]) == ["/usr/bin/env", "bash"]


DEPLOY_SH = DEPLOY / "commonplace" / "deploy.sh"


def run_deploy(tmp_path, **fakes):
    import shutil

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in "jq date flock timeout bash".split():
        executable = shutil.which(name)
        assert executable, f"required test dependency: {name}"
        (bin_dir / name).symlink_to(executable)
    stat = bin_dir / "stat"
    stat.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' " + shlex.quote(fakes.get("stat", "0 600")) + "\n"
    )
    stat.chmod(0o755)
    calls_file = tmp_path / "calls.jsonl"
    log = f'jq -cn --arg cmd "$0" --args \'[$cmd | split("/")[-1]] + $ARGS.positional\' -- "$@" >> {shlex.quote(str(calls_file))}\n'
    bodies = {
        "id": f"echo {fakes.get('uid', 0)}",
        "systemctl": """case "$*" in
"show -p LoadState --value test-backup.service") echo loaded ;;
"show -p Type --value test-backup.service") echo oneshot ;;
"show -p RemainAfterExit --value test-backup.service") echo REMAIN ;;
"show -p ExecMainStartTimestamp --value test-backup.service") date -u +%Y-%m-%dT%H:%M:%S.%NZ ;;
"is-active --quiet test-backup.service") exit ACTIVE ;;
"show -p InvocationID --value test-backup.service")
    if [[ -f "$HOME/started" ]]; then echo AFTER; else echo BEFORE; fi ;;
"start --wait test-backup.service") : > "$HOME/started"; exit START_RC ;;
"show -p Result --value test-backup.service") echo RESULT ;;
*) exit 97 ;;
esac""".replace(
            "date -u +%Y-%m-%dT%H:%M:%S.%NZ",
            fakes.get("execution", "date -u +'%a %Y-%m-%d %H:%M:%S UTC'"),
        )
        .replace("AFTER", fakes.get("after", "new"))
        .replace("BEFORE", "old")
        .replace("START_RC", str(fakes.get("start_rc", 0)))
        .replace("RESULT", fakes.get("result", "success"))
        .replace("REMAIN", fakes.get("remain", "no"))
        .replace("ACTIVE", str(fakes.get("active", 1))),
        "docker": """if [[ "$*" == *"config --format json" ]]; then
 echo '{"services":{"commonplace":{"ports":[{"host_ip":"HOST_IP","published":"PUBLISHED"}]}}}'
fi""".replace("HOST_IP", fakes.get("host_ip", "127.0.0.1")).replace(
            "PUBLISHED", fakes.get("published", "8765")
        ),
        "curl": "echo 200",
        "latest": fakes.get(
            "latest",
            'jq -cn --arg time "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \'{id:("a" * 64),time:$time}\'',
        ),
        "check": f"exit {fakes.get('check_rc', 0)}",
        "--check": ': > "$HOME/shell-executed"',
    }
    for name, body in bodies.items():
        script = bin_dir / name
        script.write_text("#!/usr/bin/env bash\nset -eu\n" + log + body + "\n")
        script.chmod(0o755)
    (tmp_path / "compose.yml").touch()
    (tmp_path / ".env").write_text(fakes.get("compose_env", ""))
    config = tmp_path / "deploy.env"
    config.write_text(fakes.get("config", ""))
    # Construct from scratch: no inherited deploy settings or real command PATH.
    env = {
        "HOME": str(tmp_path),
        "LANG": "C",
        "PATH": str(bin_dir),
        "COMMONPLACE_ROOT": str(tmp_path),
        "COMMONPLACE_SOURCE_DIR": str(ROOT),
        "COMMONPLACE_ENV_FILE": str(tmp_path / ".env"),
        "COMMONPLACE_DATA_DIR": str(tmp_path / "data"),
        "COMMONPLACE_DEPLOY_ENV": str(config),
        "DEPLOY_BACKUP_UNIT": "test-backup.service",
        "DEPLOY_BACKUP_CHECK_CMD": str(bin_dir / "check"),
        "DEPLOY_BACKUP_LATEST_CMD": str(bin_dir / "latest"),
    }
    if fakes.get("missing_unit"):
        env.pop("DEPLOY_BACKUP_UNIT")
    env.update(fakes.get("env", {}))
    for key in fakes.get("unset", []):
        env.pop(key, None)
    result = subprocess.run(
        [str(bin_dir / "bash"), str(DEPLOY_SH), *fakes.get("args", [])],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    calls = (
        [json.loads(line) for line in calls_file.read_text().splitlines()]
        if calls_file.exists()
        else []
    )
    return result, calls


def assert_deploy_failure(result, calls, step, prefix):
    assert result.returncode == 1, result.stderr
    assert result.stderr.startswith(f"[deploy] ERROR: {step}:")
    assert result.stderr.count("\n") == 1
    assert [call[0] for call in calls] == prefix
    assert not any(call[0] == "docker" for call in calls)


@pytest.mark.parametrize(
    "fakes,step,prefix",
    [
        ({"result": "exit-code"}, "backup", ["id"] + ["systemctl"] * 7),
        ({"check_rc": 1}, "check", ["id"] + ["systemctl"] * 8 + ["latest", "check"]),
        ({"missing_unit": True}, "config", ["id"]),
        ({"latest": "echo null"}, "latest", ["id"] + ["systemctl"] * 8 + ["latest"]),
        ({"latest": "exit 2"}, "latest", ["id"] + ["systemctl"] * 8 + ["latest"]),
        ({"uid": 1000}, "preflight", ["id"]),
        ({"start_rc": 1}, "backup", ["id"] + ["systemctl"] * 5),
    ],
)
@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_fails_closed(tmp_path, fakes, step, prefix):
    assert DEPLOY_SH.is_file()
    result, calls = run_deploy(tmp_path, **fakes)
    assert_deploy_failure(result, calls, step, prefix)


@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_snapshot_before_start(tmp_path):
    assert DEPLOY_SH.is_file()
    import time

    snapshot = int(time.time()) - 5
    latest = f'jq -cn --arg time "$(date -u -d @{snapshot} +%Y-%m-%dT%H:%M:%S.%NZ)" \'{{id:("a" * 64),time:$time}}\''
    result, calls = run_deploy(tmp_path, latest=latest)
    assert_deploy_failure(result, calls, "latest", ["id"] + ["systemctl"] * 8 + ["latest"])


@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_unchanged_invocation(tmp_path):
    assert DEPLOY_SH.is_file()
    result, calls = run_deploy(tmp_path, after="old")
    assert_deploy_failure(result, calls, "backup", ["id"] + ["systemctl"] * 6)


@pytest.mark.parametrize("timestamp", ["now", "+1 hour", "", "future"])
@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_invalid_timestamp(tmp_path, timestamp):
    assert DEPLOY_SH.is_file()
    value = (
        "\"$(date -u -d '+2 hours' +%Y-%m-%dT%H:%M:%S.%NZ)\""
        if timestamp == "future"
        else shlex.quote(timestamp)
    )
    latest = f"jq -cn --arg time {value} '{{id:(\"a\" * 64),time:$time}}'"
    result, calls = run_deploy(tmp_path, latest=latest)
    assert_deploy_failure(result, calls, "latest", ["id"] + ["systemctl"] * 8 + ["latest"])


@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_success_order(tmp_path):
    assert DEPLOY_SH.is_file()
    result, calls = run_deploy(tmp_path)
    assert result.returncode == 0, result.stderr
    compose = [
        "docker",
        "compose",
        "-f",
        str(tmp_path / "compose.yml"),
        "--env-file",
        str(tmp_path / ".env"),
    ]
    assert [call[0] for call in calls] == ["id"] + ["systemctl"] * 8 + [
        "latest",
        "check",
        "systemctl",
        "docker",
        "docker",
        "docker",
        "curl",
    ]
    assert calls[-4:-1] == [
        compose + ["config", "--format", "json"],
        compose + ["up", "-d", "--build"],
        compose + ["ps"],
    ]


@pytest.mark.parametrize("fakes", [{"args": ["--skip-backup"]}, {"env": {"SKIP_BACKUP": "1"}}])
@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_has_no_skip(tmp_path, fakes):
    assert DEPLOY_SH.is_file()
    result, calls = run_deploy(tmp_path, **fakes)
    assert_deploy_failure(result, calls, "usage", ["id"])


@pytest.mark.parametrize(
    "start,snap,now,age,expected",
    [
        (100, 99, 105, 900, 1),
        (100, 100, 1001, 900, 1),
        (100, 161, 100, 900, 1),
        (100, 160, 100, 900, 0),
        (100, 100, 105, 900, 0),
    ],
)
@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_fresh_enough(start, snap, now, age, expected):
    assert DEPLOY_SH.is_file()
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; fresh_enough "$2" "$3" "$4" "$5"',
            "bash",
            str(DEPLOY_SH),
            str(start),
            str(snap),
            str(now),
            str(age),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == expected, result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_env_file_is_data_and_environment_wins(tmp_path):
    assert DEPLOY_SH.is_file()
    result, calls = run_deploy(
        tmp_path, config="DEPLOY_BACKUP_CHECK_CMD=/x --check\nDEPLOY_BACKUP_UNIT=wrong.service\n"
    )
    assert result.returncode == 0, result.stderr
    assert ["check"] in calls
    assert not (tmp_path / "shell-executed").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_rejects_remain_after_exit(tmp_path):
    assert DEPLOY_SH.is_file()
    result, calls = run_deploy(tmp_path, remain="yes")
    assert_deploy_failure(result, calls, "config", ["id"] + ["systemctl"] * 3)


@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
@pytest.mark.parametrize("mode", ["split", "payload", "missing", "active", "public", "multiline"])
def test_deploy_sh_additional_contracts(tmp_path, mode):
    assert DEPLOY_SH.is_file()
    fakes = {}
    if mode == "split":
        fakes = {
            "unset": ["DEPLOY_BACKUP_CHECK_CMD"],
            "config": f"DEPLOY_BACKUP_CHECK_CMD={tmp_path}/bin/check --check\n",
        }
    elif mode == "payload":
        fakes = {
            "unset": ["DEPLOY_BACKUP_CHECK_CMD"],
            "config": f'DEPLOY_BACKUP_CHECK_CMD=$(touch "{tmp_path}/marker")\n',
        }
    elif mode == "missing":
        fakes = {"unset": ["COMMONPLACE_DATA_DIR"]}
    elif mode == "active":
        fakes = {"active": 0}
    elif mode == "public":
        fakes = {"host_ip": "0.0.0.0"}
    else:
        fakes = {"latest": "echo null; echo null"}
    result, calls = run_deploy(tmp_path, **fakes)
    if mode == "split":
        assert result.returncode == 0, result.stderr
        assert ["check", "--check"] in calls
    else:
        step = {
            "payload": "check",
            "missing": "config",
            "active": "backup",
            "public": "deploy",
            "multiline": "latest",
        }[mode]
        assert result.returncode == 1
        assert result.stderr.startswith(f"[deploy] ERROR: {step}:")
        assert not any("up" in call for call in calls)
        if mode != "public":
            assert not any(call[0] == "docker" for call in calls)
    assert not (tmp_path / "marker").exists()


@pytest.mark.parametrize("precision", ["%SZ", "%S.001Z"])
@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_real_clock_precision(tmp_path, precision):
    assert DEPLOY_SH.is_file()
    latest = f'jq -cn --arg time "$(date -u +%Y-%m-%dT%H:%M:{precision})" \'{{id:("a" * 64),time:$time}}\''
    result, calls = run_deploy(
        tmp_path, latest=latest, execution="date -u +'%a %Y-%m-%d %H:%M:%S UTC'"
    )
    assert result.returncode == 0, result.stderr
    assert any("up" in call for call in calls)


@pytest.mark.parametrize(
    "key,value",
    [
        ("DEPLOY_BACKUP_UNIT", "bad unit.service"),
        ("DEPLOY_BACKUP_UNIT", "backup.timer"),
        ("DEPLOY_BACKUP_TIMEOUT", "0"),
        ("DEPLOY_BACKUP_TIMEOUT", "1000000"),
        ("DEPLOY_BACKUP_TIMEOUT", "abc"),
        ("DEPLOY_BACKUP_MAX_AGE", "59"),
        ("DEPLOY_BACKUP_MAX_AGE", "86401"),
        ("DEPLOY_BACKUP_MAX_AGE", "abc"),
        ("APP_PORT", "0"),
        ("APP_PORT", "65536"),
        ("APP_PORT", "abc"),
    ],
)
@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_invalid_config(tmp_path, key, value):
    assert DEPLOY_SH.is_file()
    result, calls = run_deploy(tmp_path, env={key: value})
    assert_deploy_failure(result, calls, "config", ["id"])


@pytest.mark.parametrize(
    "key,step", [("DEPLOY_BACKUP_CHECK_CMD", "check"), ("DEPLOY_BACKUP_LATEST_CMD", "latest")]
)
@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_whitespace_command(tmp_path, key, step):
    assert DEPLOY_SH.is_file()
    result, calls = run_deploy(tmp_path, env={key: "   "})
    assert_deploy_failure(
        result, calls, step, ["id"] + ["systemctl"] * 8 + (["latest"] if step == "check" else [])
    )


@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_env_file_ignores_bash_env(tmp_path):
    assert DEPLOY_SH.is_file()
    payload = tmp_path / "payload"
    payload.write_text(': > "$HOME/injected"\n')
    result, calls = run_deploy(tmp_path, config=f"BASH_ENV={payload}\n")
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "injected").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_wrong_published_port(tmp_path):
    assert DEPLOY_SH.is_file()
    result, calls = run_deploy(tmp_path, published="8766")
    assert result.returncode == 1
    assert result.stderr.startswith("[deploy] ERROR: deploy:")
    assert not any("up" in call for call in calls)


@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_unloaded_unit(tmp_path):
    assert DEPLOY_SH.is_file()
    result, calls = run_deploy(tmp_path, after="")
    assert_deploy_failure(result, calls, "backup", ["id"] + ["systemctl"] * 6)
    assert "backup unit was unloaded; keep it referenced" in result.stderr


@pytest.mark.parametrize("value", ['"/data"', "/data # comment", "/data/$HOME"])
@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_rejects_compose_path_grammar(tmp_path, value):
    assert DEPLOY_SH.is_file()
    result, calls = run_deploy(
        tmp_path,
        unset=["COMMONPLACE_DATA_DIR"],
        compose_env=f"COMMONPLACE_DATA_DIR={value}\n",
    )
    assert_deploy_failure(result, calls, "config", ["id"])


@pytest.mark.parametrize(
    "key", ["COMMONPLACE_SOURCE_DIR", "COMMONPLACE_ENV_FILE", "COMMONPLACE_DATA_DIR"]
)
@pytest.mark.parametrize(
    "value",
    [
        '"/data"',
        "/data # comment",
        "/data/$HOME",
        "/data/with space",
        "/data/`id`",
        "/data/\\escape",
        "/data/'quoted'",
    ],
)
@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_rejects_deploy_path_grammar(tmp_path, key, value):
    assert DEPLOY_SH.is_file()
    result, calls = run_deploy(tmp_path, unset=[key], config=f"{key}={value}\n")
    assert_deploy_failure(result, calls, "config", ["id"])
    assert "must be a literal path" in result.stderr


@pytest.mark.parametrize("ownership", ["1000 600", "0 620", "0 602", "0 666", "0 6770"])
@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_rejects_unsafe_config_permissions(tmp_path, ownership):
    assert DEPLOY_SH.is_file()
    result, calls = run_deploy(tmp_path, stat=ownership)
    assert_deploy_failure(result, calls, "config", ["id"])
    assert "owned by root and not group/world-writable" in result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_missing_execution_time(tmp_path):
    assert DEPLOY_SH.is_file()
    result, calls = run_deploy(tmp_path, execution="printf ''")
    assert_deploy_failure(result, calls, "backup", ["id"] + ["systemctl"] * 8)
    assert "missing execution time" in result.stderr
