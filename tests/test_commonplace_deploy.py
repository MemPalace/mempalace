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


# The hook snapshots the running serve container through `compose exec` and must
# never displace it (no one-off container, no stop/start). These verbs replace,
# stop or freeze the serve container; neither the hook nor the nightly job may
# send them.
DISPLACING_VERBS = frozenset(
    {"stop", "start", "restart", "run", "up", "down", "rm", "kill", "create", "pause", "unpause"}
)


def test_hook_is_executable_and_uses_only_host_managed_restic():
    assert HOOK.stat().st_mode & 0o111
    text = HOOK.read_text()
    assert "set -Eeuo pipefail" in text
    assert 'docker compose --env-file "$env_file" -f "$compose_file"' in text
    # The snapshot runs inside the running serve container, bounded, without stdin.
    assert (
        'timeout -k 10 "$t" "${compose[@]}" exec -T "${snapshot_env[@]}" commonplace commonplace backup --online'
        ' --deadline "$deadline" "/data/.backup-staging/$stamp" </dev/null'
    ) in text
    assert 'install -d -o 10001 -g 10001 -m 0700 "$data_host/.backup-staging"' in text
    assert 'install -d -m 0700 -- "$backup_dir/.commonplace-staging"' in text
    assert "trap 'exit 143' TERM INT" in text
    assert "flock -n 9" in text
    assert "mv -T" in text
    commands = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "--volume" not in commands
    verbs = re.findall(r'"\$\{compose\[@\]\}"\s+(?:-\S+\s+)*([a-z-]+)', commands)
    assert "exec" in verbs
    assert not DISPLACING_VERBS & set(verbs), verbs
    assert not re.search(r"\brestic\s+(backup|forget|prune|init)\b", commands)


@pytest.mark.parametrize("arguments", [[], ["relative"], ["/nonexistent/commonplace-backups"]])
def test_hook_rejects_invalid_destination_with_one_failure_line(arguments):
    result = subprocess.run(["bash", str(HOOK), *arguments], capture_output=True, text=True)
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.count("\n") == 1
    assert result.stderr.startswith("commonplace backup failed: setup")


# This fake models only the external boundary: the Docker CLI and the root-only
# `install -o`. It holds one running serve container whose /data is a host
# directory (FAKE_DATA). Each test runs the actual hook, including its traps,
# locks, validation and atomic moves. Every call is logged at entry. Verbs that
# would displace serve fail loudly. `inspect --format` supports the Go-template
# subset a shell hook needs ({{.A.B}}, {{json .X}}, range, if eq, println).
FAKE_HOST_COMMANDS = r"""
import json, os, pathlib, re, shutil, stat, sys
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
try:
    st = os.fstat(0)
    devnull = os.stat(os.devnull)
    stdin = "devnull" if (stat.S_ISCHR(st.st_mode) and st.st_rdev == devnull.st_rdev) else "other"
except OSError:
    stdin = "closed"
with open(os.environ["FAKE_COMMANDS"], "a") as log:
    log.write(json.dumps({"command": name, "args": args, "stdin": stdin}) + "\n")
fail = os.environ.get("FAKE_FAIL", "")
state = pathlib.Path(os.environ["FAKE_STATE"])
data = pathlib.Path(os.environ["FAKE_DATA"])
DISPLACING = {"stop", "start", "restart", "run", "up", "down", "rm", "kill", "create",
              "pause", "unpause"}
VALUED = {"--env-file", "-f", "--file", "-p", "--project-name", "--project-directory",
          "--profile", "--ansi", "--progress", "--parallel"}

def die(code, message):
    print(f"fake {name}: {message}", file=sys.stderr)
    sys.exit(code)

def split_verb(words):
    index = 0
    while index < len(words) and words[index].startswith("-"):
        index += 2 if words[index] in VALUED and "=" not in words[index] else 1
    return (words[index], words[index + 1:]) if index < len(words) else ("", [])

def container():
    snapshotted = (state / "snapshotted").exists()
    started = "2026-01-01T00:00:00.000000000Z"
    if fail == "serve_changed" and snapshotted:
        started = "2026-01-01T00:09:00.000000000Z"
    return {
        "Id": "c" * 64,
        "Name": "/test-commonplace-1",
        "State": {"Status": "running", "Running": True, "StartedAt": started},
        "Mounts": [{"Type": "bind", "Source": str(data), "Destination": "/data", "RW": True}],
        "Config": {"Labels": {"com.docker.compose.service": "commonplace"}},
        "HostConfig": {"Init": True},
    }

def lookup(context, path):
    for part in [p for p in path.strip().lstrip(".").split(".") if p]:
        context = context[part]
    return context

def parse(tokens, index=0):
    nodes = []
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token.startswith("{{"):
            nodes.append(("text", token))
            continue
        action = token[2:-2].strip("- ").strip()
        if action == "end":
            return nodes, index
        if action.startswith(("range ", "if ")):
            body, index = parse(tokens, index)
            nodes.append((action, body))
        else:
            nodes.append(("expr", action))
    return nodes, index

def show(value):
    if isinstance(value, bool):
        return str(value).lower()
    return value if isinstance(value, str) else json.dumps(value)

def render(nodes, context):
    out = []
    for kind, value in nodes:
        if kind == "text":
            out.append(value)
        elif kind == "expr":
            if value == "println":
                out.append("\n")
            elif value.startswith("json "):
                out.append(json.dumps(lookup(context, value[5:])))
            elif value.startswith("."):
                out.append(show(lookup(context, value)))
            else:
                die(98, f"unsupported template action {value!r}")
        elif kind.startswith("range "):
            out.extend(render(value, item) for item in lookup(context, kind[6:]))
        else:
            condition = kind[3:].strip()
            match = re.fullmatch(r'eq (\.\S*) "([^"]*)"', condition)
            if match:
                ok = show(lookup(context, match[1])) == match[2]
            elif condition.startswith("."):
                ok = bool(lookup(context, condition))
            else:
                die(98, f"unsupported template condition {condition!r}")
            if ok:
                out.append(render(value, context))
    return "".join(out)

def inspect(words):
    template, targets, index = None, [], 0
    while index < len(words):
        word = words[index]
        if word in ("-f", "--format"):
            template, index = words[index + 1], index + 2
            continue
        if word.startswith("--format="):
            template = word.split("=", 1)[1]
        elif word == "--type":
            index += 1
        elif not word.startswith("-"):
            targets.append(word)
        index += 1
    serve = container()
    if fail == "serve_down" or not targets or any(
        t not in (serve["Id"], serve["Id"][:12], serve["Name"].lstrip("/")) for t in targets
    ):
        die(1, "Error: No such object")
    if template is None:
        print(json.dumps([serve for _ in targets], indent=4))
    else:
        nodes, _ = parse(re.split(r"({{.*?}})", template))
        for _ in targets:
            print(render(nodes, serve))

def write_snapshot(destination):
    if fail == "snapshot":
        die(9, "snapshot failed")
    if os.environ.get("FAKE_SNAPSHOT") == "real":
        # The production snapshot writer (format v1) on the container's /data,
        # minus the hook's own staging; published by one rename like the child.
        from commonplace import backup as module
        inventory = module._inventory
        def without_staging(root, *args, **kwargs):
            files, dirs = inventory(root, *args, **kwargs)
            keep = lambda p: p.relative_to(root).parts[0] not in (
                ".backup-staging", ".commonplace-fence.lock")
            return [p for p in files if keep(p)], [p for p in dirs if keep(p)]
        module._inventory = without_staging
        partial = state / f"snapshot-{destination.name}"
        module.backup(data, partial)
        partial.rename(destination)
    else:
        destination.mkdir()
        (destination / "control.sqlite3").write_bytes(b"consistent snapshot")
        if fail != "manifest":
            (destination / "manifest.json").write_text("invalid" if fail == "corrupt" else "{}")

def compose(words):
    verb, rest = split_verb(words)
    if verb in DISPLACING:
        die(97, f"displacing verb {verb!r}")
    if verb == "ps":
        if fail != "serve_down":
            print(container()["Id"])
    elif verb == "exec":
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in ("-u", "--user", "-e", "--env", "-w", "--workdir",
                                            "--index") else rest[1:]
        if rest[:3] != ["commonplace", "commonplace", "backup"]:
            die(10, f"unexpected exec {rest!r}")
        if fail == "serve_down":
            die(1, "service is not running")
        if "--online" in rest:
            target = rest[-1]
            if not target.startswith("/data/.backup-staging/"):
                die(2, f"bad snapshot destination {target!r}")
            (state / "snapshotted").touch()
            write_snapshot(data / target[len("/data/"):])
    elif verb != "config":
        die(10, f"unexpected compose verb {verb!r}")

if name == "install":
    pathlib.Path(args[-1]).mkdir(parents=True, exist_ok=True)
elif name == "docker":
    verb, rest = split_verb(args)
    if verb == "compose":
        compose(rest)
    elif verb == "inspect":
        inspect(rest)
    elif verb == "container" and rest[:1] == ["inspect"]:
        inspect(rest[1:])
    elif verb == "ps":
        pass  # no one-off containers exist
    elif verb in DISPLACING or (verb == "container" and rest[:1] and rest[0] in DISPLACING):
        die(97, f"displacing verb {verb!r}")
    else:
        die(10, f"unexpected docker verb {verb!r}")
elif name == "restic":
    if args[0] == "backup":
        from commonplace.backup import restore
        restore(args[-1], os.environ["FAKE_RESTORED"])
        if fail == "upload": sys.exit(3)
    elif args[0] == "forget":
        if fail == "retention": sys.exit(9)
    else:
        sys.exit(10)
elif name == "alert":
    if fail == "alert": sys.exit(9)
else:
    sys.exit(10)
"""


def install_fake_host(bin_dir, names=("docker", "install")):
    bin_dir.mkdir(exist_ok=True)
    for name in names:
        executable = bin_dir / name
        executable.write_text(f"#!{sys.executable}\n" + FAKE_HOST_COMMANDS)
        executable.chmod(0o755)


def docker_calls(calls):
    return [call["args"] for call in calls if call["command"] == "docker"]


def assert_serve_never_displaced(calls):
    """No call may stop, replace or freeze the serve container."""
    for args in docker_calls(calls):
        assert not DISPLACING_VERBS & set(args), args


def snapshot_calls(calls):
    return [args for args in docker_calls(calls) if "exec" in args and "--online" in args]


def run_hook(tmp_path, fail="", env_overrides=None):
    bin_dir = tmp_path / "bin"
    install_fake_host(bin_dir)
    backups = tmp_path / "backups"
    backups.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    fake_state = tmp_path / "fake-state"
    fake_state.mkdir()
    commands = tmp_path / "commands.jsonl"
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    for key in ("COMMONPLACE_DEPLOY_DIR", "COMMONPLACE_ENV_FILE", "COMPOSE_FILE"):
        env.pop(key, None)
    env.update(
        FAKE_COMMANDS=str(commands),
        FAKE_FAIL=fail,
        FAKE_DATA=str(data),
        FAKE_STATE=str(fake_state),
        # The test filesystem is small; the disk reserve and margin are the operator's.
        COMMONPLACE_BACKUP_RESERVE_BYTES="0",
        COMMONPLACE_BACKUP_MARGIN_BYTES="0",
    )
    env.update(env_overrides or {})
    result = subprocess.run(
        ["bash", str(HOOK), str(backups)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
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
    return result, backups, calls, data


def test_hook_publishes_snapshot_without_displacing_serve(tmp_path):
    result, backups, calls, data = run_hook(tmp_path)
    assert result.returncode == 0, result.stderr
    snapshots = list(backups.glob("commonplace-*"))
    assert len(snapshots) == 1
    stamp = snapshots[0].name.removeprefix("commonplace-")
    assert re.fullmatch(r"[0-9TZ.]+-[0-9]+", stamp)
    assert (snapshots[0] / "control.sqlite3").read_bytes() == b"consistent snapshot"
    assert json.loads((snapshots[0] / "manifest.json").read_text()) == {}
    assert list((backups / ".commonplace-staging").iterdir()) == []
    assert not [path for path in (data / ".backup-staging").iterdir() if stamp in path.name]
    assert_serve_never_displaced(calls)
    snapshot = snapshot_calls(calls)
    assert len(snapshot) == 1
    assert snapshot[0][snapshot[0].index("exec") :] == [
        "exec",
        "-T",
        "commonplace",
        "commonplace",
        "backup",
        "--online",
        "--deadline",
        snapshot[0][-2],
        f"/data/.backup-staging/{stamp}",
    ]
    assert snapshot[0][-2].isdigit()
    assert [call["stdin"] for call in calls if call["args"] == snapshot[0]] == ["devnull"]
    for args in docker_calls(calls):
        if args[0] == "compose":
            assert args[1:4] == ["--env-file", str(ROOT.parent / ".env"), "-f"]
            assert Path(args[4]) == ROOT.parent / "compose.yml"


@pytest.mark.parametrize("individual_paths", [False, True])
def test_hook_uses_external_operator_configuration(tmp_path, individual_paths):
    operator_dir = tmp_path / "operator config"
    env_file = operator_dir / "custom.env" if individual_paths else operator_dir / ".env"
    compose_file = operator_dir / "custom.yml" if individual_paths else operator_dir / "compose.yml"
    overrides = {"COMMONPLACE_DEPLOY_DIR": str(operator_dir)}
    if individual_paths:
        overrides.update(COMMONPLACE_ENV_FILE=str(env_file), COMPOSE_FILE=str(compose_file))
    result, _, calls, _ = run_hook(tmp_path, env_overrides=overrides)
    assert result.returncode == 0, result.stderr
    compose_calls = [args for args in docker_calls(calls) if args[0] == "compose"]
    assert compose_calls
    for args in compose_calls:
        assert args[:5] == ["compose", "--env-file", str(env_file), "-f", str(compose_file)]


@pytest.mark.parametrize("name", ["COMMONPLACE_DEPLOY_DIR", "COMMONPLACE_ENV_FILE", "COMPOSE_FILE"])
@pytest.mark.parametrize("value", ["relative/config", "~/config"])
def test_hook_rejects_relative_overrides_before_touching_gateway(tmp_path, name, value):
    result, backups, calls, _ = run_hook(tmp_path, env_overrides={name: value})
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.count("\n") == 1
    assert f"{name} must be an absolute path" in result.stderr
    assert calls == []
    assert list(backups.iterdir()) == []


@pytest.mark.parametrize("fail", ["serve_down", "snapshot", "manifest", "corrupt", "serve_changed"])
def test_hook_fails_closed_without_displacing_serve(tmp_path, fail):
    result, backups, calls, _ = run_hook(tmp_path, fail)
    assert result.returncode != 0
    assert result.stderr.count("\n") == 1
    assert result.stderr.startswith("commonplace backup failed:")
    assert list(backups.glob("commonplace-*")) == []
    assert_serve_never_displaced(calls)
    if fail == "serve_down":
        assert snapshot_calls(calls) == []
    else:
        assert len(snapshot_calls(calls)) == 1
    if fail == "serve_changed":
        assert "serve changed" in result.stderr


def test_backup_shell_syntax():
    result = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert shlex.split(HOOK.read_text().splitlines()[0][2:]) == ["/usr/bin/env", "bash"]


DEPLOY_SH = DEPLOY / "commonplace" / "deploy.sh"


def run_deploy(tmp_path, **fakes):
    import shutil

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in "jq date flock timeout bash rm kill sleep".split():
        executable = shutil.which(name)
        assert executable, f"required test dependency: {name}"
        if not (bin_dir / name).exists():
            (bin_dir / name).symlink_to(executable)
    stat = bin_dir / "stat"
    stat.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' " + shlex.quote(fakes.get("stat", "0 600")) + "\n"
    )
    stat.chmod(0o755)
    calls_file = tmp_path / "calls.jsonl"
    previous_calls = len(calls_file.read_text().splitlines()) if calls_file.exists() else 0
    log = f'jq -cn --arg cmd "$0" --args \'[$cmd | split("/")[-1]] + $ARGS.positional\' -- "$@" >> {shlex.quote(str(calls_file))}\n'
    bodies = {
        "id": f"echo {fakes.get('uid', 0)}",
        "systemctl": """# A masked timer holds no reference. Inactive unreferenced proof is lost.
if [[ GC == yes && -f "$HOME/started" && ( ! -f "$HOME/reference" || ! -f "$HOME/proof" ) && "$1" == show ]]; then
    case "$3" in InvocationID|Result|ExecMainStartTimestamp) exit 0 ;; esac
fi
case "$*" in
"stop "commonplace-deploy-proof-*.service)
    rm -f "$HOME/reference"
    [[ GC != yes ]] || rm -f "$HOME/proof"
    exit STOP_RC ;;
"show -p LoadState --value test-backup.service") echo loaded ;;
"show -p Type --value test-backup.service") echo oneshot ;;
"show -p RemainAfterExit --value test-backup.service") echo REMAIN ;;
"show -p TimeoutStartUSec --value test-backup.service") echo UNIT_TIMEOUT ;;
"show -p ExecMainStartTimestamp --value test-backup.service") date -u +%Y-%m-%dT%H:%M:%S.%NZ ;;
"is-active --quiet test-backup.service") exit ACTIVE ;;
"show -p InvocationID --value test-backup.service")
    if [[ -f "$HOME/started" ]]; then echo AFTER; else echo BEFORE; fi ;;
"start --wait test-backup.service")
    n=0; [[ ! -f "$HOME/started" ]] || n=$(< "$HOME/started")
    echo $((n + 1)) > "$HOME/started"
    : > "$HOME/proof"
    # Complete synchronously, before the client can sample a running unit.
    START_BODY
    exit START_RC ;;
"show -p Result --value test-backup.service") echo RESULT ;;
*) exit 97 ;;
esac""".replace(
            "date -u +%Y-%m-%dT%H:%M:%S.%NZ",
            fakes.get("execution", "date -u +'%a %Y-%m-%d %H:%M:%S UTC'"),
        )
        .replace("AFTER", fakes.get("after", '"invocation-$(< "$HOME/started")"'))
        .replace("BEFORE", "old")
        .replace("START_BODY", fakes.get("start_body", ":"))
        .replace("START_RC", str(fakes.get("start_rc", 0)))
        .replace("STOP_RC", str(fakes.get("stop_rc", 0)))
        .replace("GC", "yes" if fakes.get("gc") else "no")
        .replace("RESULT", fakes.get("result", "success"))
        .replace("REMAIN", fakes.get("remain", "no"))
        .replace("UNIT_TIMEOUT", fakes.get("unit_timeout", "45min"))
        .replace("ACTIVE", str(fakes.get("active", 1))),
        "systemd-run": """[[ "$*" == *"--property=After=test-backup.service"* ]]
[[ "$*" == *"--property=Type=exec"* ]]
[[ "$*" != *Wants=* && "$*" != *Requires=* && "$*" != *RemainAfterExit=* ]]
[[ "$*" == *"--collect"* && "$*" == *"sleep infinity"* ]]
printf '%s\\n' "$*" > "$HOME/reference"
HOLD_SIGNAL
exit HOLD_RC""".replace("HOLD_RC", str(fakes.get("hold_rc", 0))).replace(
            "HOLD_SIGNAL", fakes.get("hold_signal", ":")
        ),
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
        [json.loads(line) for line in calls_file.read_text().splitlines()[previous_calls:]]
        if calls_file.exists()
        else []
    )
    return result, calls


def without_reference_calls(calls):
    return [
        call
        for call in calls
        if call[0] != "systemd-run" and not (call[:2] == ["systemctl", "stop"])
    ]


@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
@pytest.mark.parametrize(
    "scenario",
    ["masked", "failure", "missing", "old", "twice", "fast", "timeout"],
)
def test_deploy_sh_holds_invocation_proof(tmp_path, scenario):
    # No enabled timer: the fake drops all execution properties on GC. A fast
    # oneshot completes inside start, before any post-start show can observe it.
    fakes = {"gc": True}
    if scenario == "masked":
        fakes["start_body"] = "sleep 0.05"
    elif scenario == "failure":
        fakes.update(start_rc=1, result="exit-code")
    elif scenario == "missing":
        fakes["after"] = ""
    elif scenario == "old":
        fakes["after"] = "old"
    elif scenario == "timeout":
        fakes.update(
            start_body="sleep 3",
            unit_timeout="500ms",
            env={"DEPLOY_BACKUP_TIMEOUT": "2"},
        )
    for _ in range(2 if scenario == "twice" else 1):
        result, calls = run_deploy(tmp_path, **fakes)
        holds = [call for call in calls if call[0] == "systemd-run"]
        assert len(holds) == 1, (result.stderr, calls)
        hold = holds[0]
        unit = next(arg.removeprefix("--unit=") for arg in hold if arg.startswith("--unit="))
        stop = ["systemctl", "stop", unit]
        assert stop in calls
        assert not (tmp_path / "reference").exists()
        assert not (tmp_path / "proof").exists()  # inactive unit is GC'd on release
        start = ["systemctl", "start", "--wait", "test-backup.service"]
        assert calls.index(hold) < calls.index(start) < calls.index(stop)
        before = ["systemctl", "show", "-p", "InvocationID", "--value", "test-backup.service"]
        assert calls.index(hold) < calls.index(before) < calls.index(start)
        assert not any(
            "timer" in arg or arg in {"unmask", "enable"} for call in calls for arg in call
        )
        if scenario in {"failure", "missing", "old", "timeout"}:
            assert result.returncode == 1, result.stderr
            assert result.stderr.startswith("[deploy] ERROR: backup:")
            assert not any(call[0] in {"latest", "check", "docker"} for call in calls)
        else:
            assert result.returncode == 0, result.stderr
            execution = [
                "systemctl",
                "show",
                "-p",
                "ExecMainStartTimestamp",
                "--value",
                "test-backup.service",
            ]
            assert calls.index(start) < calls.index(execution) < calls.index(["latest"])
            assert calls.index(["latest"]) < calls.index(["check"]) < calls.index(stop)
            assert calls.index(stop) < next(
                i for i, call in enumerate(calls) if call[0] == "docker"
            )
    assert (tmp_path / "started").read_text().strip() == ("2" if scenario == "twice" else "1")


@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
@pytest.mark.parametrize("signal", ["HUP", "INT", "TERM"])
def test_deploy_sh_releases_reference_on_signal(tmp_path, signal):
    result, calls = run_deploy(tmp_path, gc=True, hold_signal=f'kill -{signal} "$PPID"')
    assert result.returncode == {"HUP": 129, "INT": 130, "TERM": 143}[signal]
    assert not (tmp_path / "reference").exists()
    assert any(call[:2] == ["systemctl", "stop"] for call in calls)
    assert not any(call[0] == "docker" for call in calls)


@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_reference_failure_is_closed(tmp_path):
    result, calls = run_deploy(tmp_path, gc=True, hold_rc=1)
    assert result.returncode == 1
    assert "cannot hold backup unit reference" in result.stderr
    assert not (tmp_path / "reference").exists()
    assert not any(call[:2] == ["systemctl", "start"] or call[0] == "docker" for call in calls)


def assert_deploy_failure(result, calls, step, prefix):
    assert result.returncode == 1, result.stderr
    assert result.stderr.startswith(f"[deploy] ERROR: {step}:")
    assert result.stderr.count("\n") == 1
    assert [call[0] for call in without_reference_calls(calls)] == prefix
    assert not any(call[0] == "docker" for call in calls)


@pytest.mark.parametrize(
    "fakes,step,prefix",
    [
        ({"result": "exit-code"}, "backup", ["id"] + ["systemctl"] * 8),
        ({"check_rc": 1}, "check", ["id"] + ["systemctl"] * 9 + ["latest", "check"]),
        ({"missing_unit": True}, "config", ["id"]),
        ({"latest": "echo null"}, "latest", ["id"] + ["systemctl"] * 9 + ["latest"]),
        ({"latest": "exit 2"}, "latest", ["id"] + ["systemctl"] * 9 + ["latest"]),
        ({"uid": 1000}, "preflight", ["id"]),
        ({"start_rc": 1}, "backup", ["id"] + ["systemctl"] * 6),
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
    assert_deploy_failure(result, calls, "latest", ["id"] + ["systemctl"] * 9 + ["latest"])


@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_unchanged_invocation(tmp_path):
    assert DEPLOY_SH.is_file()
    result, calls = run_deploy(tmp_path, after="old")
    assert_deploy_failure(result, calls, "backup", ["id"] + ["systemctl"] * 7)


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
    assert_deploy_failure(result, calls, "latest", ["id"] + ["systemctl"] * 9 + ["latest"])


@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_success_order(tmp_path):
    assert DEPLOY_SH.is_file()
    result, calls = run_deploy(tmp_path)
    assert result.returncode == 0, result.stderr
    calls = without_reference_calls(calls)
    compose = [
        "docker",
        "compose",
        "-f",
        str(tmp_path / "compose.yml"),
        "--env-file",
        str(tmp_path / ".env"),
    ]
    assert [call[0] for call in calls] == ["id"] + ["systemctl"] * 9 + [
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


@pytest.mark.parametrize(
    "unit_timeout,client_timeout,message",
    [
        # A oneshot unit's default TimeoutStartSec is infinity: never deploy on it.
        ("infinity", None, "must be finite"),
        ("0", None, "must be finite"),
        ("", None, "must be finite"),
        ("soon", None, "must be finite"),
        ("3 h", None, "must be finite"),
        # The unit must end before the client gives up, so never equal or above.
        ("3h", None, "must be below"),
        ("1h", "3600", "must be below"),
        ("3h", "10800", "must be below"),
        ("1h 15min 30.500000s", "4531", "must be below"),
        ("2w", "999999", "must be below"),
    ],
)
@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_refuses_unbounded_or_outlasting_unit_timeout(
    tmp_path, unit_timeout, client_timeout, message
):
    env = {"DEPLOY_BACKUP_TIMEOUT": client_timeout} if client_timeout else {}
    result, calls = run_deploy(tmp_path, unit_timeout=unit_timeout, env=env)
    assert_deploy_failure(result, calls, "config", ["id"] + ["systemctl"] * 4)
    assert f"unit TimeoutStartSec {message}" in result.stderr
    assert not any(call[:2] == ["systemctl", "start"] for call in calls)


@pytest.mark.parametrize(
    "unit_timeout,client_timeout",
    [
        ("59min 59s", None),
        ("3h", "11400"),  # the documented rollout pair: unit 3h < client 11400 s
        ("1h 15min 30.500000s", "4532"),
        ("1min 30s", "91"),
        ("500ms", "2"),
    ],
)
@pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets GNU/Linux hosts")
def test_deploy_sh_accepts_unit_timeout_below_client(tmp_path, unit_timeout, client_timeout):
    env = {"DEPLOY_BACKUP_TIMEOUT": client_timeout} if client_timeout else {}
    result, calls = run_deploy(tmp_path, unit_timeout=unit_timeout, env=env)
    assert result.returncode == 0, result.stderr
    assert ["systemctl", "start", "--wait", "test-backup.service"] in calls


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
        result, calls, step, ["id"] + ["systemctl"] * 9 + (["latest"] if step == "check" else [])
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
    assert_deploy_failure(result, calls, "backup", ["id"] + ["systemctl"] * 7)
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
    assert_deploy_failure(result, calls, "backup", ["id"] + ["systemctl"] * 9)
    assert "missing execution time" in result.stderr


def test_deploy_env_example_matches_allowlist():
    example = DEPLOY_SH.with_name("deploy.env.example")
    assert example.is_file()
    allowlist = re.search(r"case \$key in\n\s+([A-Z_|]+)\) ;;", DEPLOY_SH.read_text())[1]
    keys = re.findall(r"^([A-Z_]+)=", example.read_text(), re.MULTILINE)
    assert len(keys) == len(set(keys))
    assert set(keys) == set(allowlist.split("|"))


def test_deploy_missing_source_names_key(tmp_path):
    result, calls = run_deploy(tmp_path, unset=["COMMONPLACE_SOURCE_DIR"])
    assert (
        "COMMONPLACE_SOURCE_DIR is required (absolute path), see deploy.env.example"
        in result.stderr
    )
    assert_deploy_failure(result, calls, "config", ["id"])


def test_deploy_relative_data_names_key(tmp_path):
    result, calls = run_deploy(tmp_path, env={"COMMONPLACE_DATA_DIR": "relative/data"})
    assert "COMMONPLACE_DATA_DIR must be an absolute path, see deploy.env.example" in result.stderr
    assert_deploy_failure(result, calls, "config", ["id"])


def test_deploy_reports_both_missing_paths(tmp_path):
    keys = ["COMMONPLACE_SOURCE_DIR", "COMMONPLACE_DATA_DIR"]
    result, calls = run_deploy(tmp_path, unset=keys)
    assert all(
        f"{key} is required (absolute path), see deploy.env.example" in result.stderr
        for key in keys
    )
    assert result.returncode == 1
    assert result.stderr.count("\n") == 2
    assert [call[0] for call in calls] == ["id"]
