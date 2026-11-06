"""Faithful fakes of ``docker``/``docker compose`` and ``install`` for backup hook tests.

The fake engine runs one Compose service, ``commonplace``. Its container is a real
``commonplace serve`` process on the test data directory: it answers ``/healthz``,
holds the shared data lock and (where the code has one) the gateway's admin socket.
The fake ``docker`` executable is this file run as a script. It behaves like the real
client where the backup hook can observe it:

* every invocation is logged at entry (argv, parsed verb, stdin target);
* ``compose exec`` runs the command detached from the client, in its own session, so
  killing the client (``timeout``, a killed hook) leaves the process running, exactly
  like a process inside a real container; stdin is not attached (``-T``);
* ``compose run`` records a one-off container at start; the record goes away only when
  the command completed and ``--rm`` was given (a killed client leaves it behind);
* ``stop``/``start``/``restart``/``up``/``down``/``rm``/``kill``/``create``/``pause``
  really stop, restart or replace the serve process (new ``StartedAt``, new pid, and a
  new ``Id`` where Docker would create a new container);
* ``inspect`` and ``ps`` answer with Docker's JSON shapes and a Go-template subset.

Container paths ``/data`` (and ``--volume`` targets) are translated to host paths.
Unsupported invocations exit 125 with a ``fake docker: unsupported`` message, so a gap
in the fake is loud instead of silently passing.

Fault injection for tests (``FakeEngine(hang=...)``, ``FakeEngine.snapshot_exit``):
a *hang* makes the snapshot command (``commonplace backup`` other than ``--help``,
``--check-online`` and ``--online-cleanup``) block forever on its first write into the
destination, or on opening a chosen source file, through a ``sitecustomize`` audit hook
inherited by forked and spawned Python children. The process stays killable.
``FakeEngine.hang_exec(flag)`` instead hangs the *client*: a ``compose exec`` whose
command contains ``flag`` (``--check-online``, ``--online-cleanup``) blocks in the fake
``docker`` process itself, like a client stuck on a daemon that never answers.

Only the standard library is used here; nothing imports product code.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKES = Path(__file__).resolve()
SERVICE = "commonplace"
PROJECT = "cpfake"
IMAGE = "commonplace:local"
NOT_SNAPSHOT_FLAGS = frozenset({"--help", "-h", "--check-online", "--online-cleanup"})
# Verbs that stop, start, replace or add a container. The backup hook must never use them.
DISPLACING_VERBS = frozenset(
    {"stop", "start", "restart", "up", "down", "rm", "kill", "create", "run", "pause", "unpause"}
)
MARK_ENV = "FAKE_CONTAINER"


# --------------------------------------------------------------------------- helpers


def _now_iso() -> str:
    # Docker reports RFC 3339 with nanoseconds.
    now = time.time_ns()
    base = datetime.fromtimestamp(now // 1_000_000_000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{now % 1_000_000_000:09d}Z"


def pid_alive(pid) -> bool:
    """Live means /proc/<pid>/stat exists and the state is not zombie or dead."""
    if not pid:
        return False
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
    except (OSError, ValueError):
        return False
    state = stat.rsplit(")", 1)[1].split()[0]
    return state not in ("Z", "X")


def _proc_env(pid) -> dict:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    env = {}
    for entry in raw.split(b"\0"):
        if b"=" in entry:
            key, value = entry.split(b"=", 1)
            env[os.fsdecode(key)] = os.fsdecode(value)
    return env


def _proc_sid(pid):
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[3])
    except (OSError, ValueError, IndexError):
        return None


def _cmdline(pid) -> list:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [os.fsdecode(a) for a in raw.split(b"\0") if a]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _signal_exit(returncode: int) -> int:
    # Docker reports a signalled container process as 128 + signal.
    return 128 - returncode if returncode < 0 else returncode


# --------------------------------------------------------------------------- state


class State:
    """Engine state shared by the test and every fake client process (flock'd JSON)."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.path = self.dir / "state.json"
        self.lock = self.dir / "state.lock"

    @contextmanager
    def edit(self):
        with open(self.lock, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            data = json.loads(self.path.read_text())
            yield data
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
            os.replace(tmp, self.path)

    def read(self) -> dict:
        with open(self.lock, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            return json.loads(self.path.read_text())

    def log(self, record: dict) -> None:
        line = json.dumps(record, sort_keys=True) + "\n"
        with open(self.dir / "calls.jsonl", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.write(line)


# --------------------------------------------------------------------------- container model


def container_env(state: dict, extra=None, *, host: bool = True) -> dict:
    """The environment a process inside the serve container sees."""
    env = {
        "PATH": os.pathsep.join(
            [str(Path(state["python"]).parent), "/usr/local/bin", "/usr/bin", "/bin"]
        ),
        "HOME": state["home"],
        "TMPDIR": state["tmp"],
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "COMMONPLACE_DATA": "/data",
        "COMMONPLACE_ADMIN_SOCKET": state["admin_socket"],
        "ANONYMIZED_TELEMETRY": "False",
        MARK_ENV: state["dir"],
    }
    env.update(state.get("env", {}))
    env.update(extra or {})
    if not host:
        return env
    return {key: translate(state, value) for key, value in env.items()}


def translate(state: dict, value: str, mounts=()) -> str:
    """Map container paths (/data, extra bind targets) to host paths.

    A container path is recognised anywhere it starts a path word: a whole argument,
    ``--flag=/data/x``, or inside an ``sh -c`` script. Host paths that merely contain
    ``/data`` as a later component are left alone.
    """
    if not isinstance(value, str):
        return value
    binds = sorted([("/data", state["data_host"]), *mounts], key=lambda bind: -len(bind[0]))
    pattern = "|".join(re.escape(target) for target, _ in binds)
    sources = dict(binds)
    return re.sub(
        rf"(?<![\w./-])({pattern})(?=/|[\s'\";:)]|$)",
        lambda match: sources[match.group(1)],
        value,
    )


def host_argv(state: dict, argv: list, mounts=()) -> list:
    argv = [translate(state, arg, mounts) for arg in argv]
    head = Path(argv[0]).name
    if head == "commonplace":
        return [state["python"], "-m", "commonplace", *argv[1:]]
    if head in ("python", "python3"):
        return [state["python"], *argv[1:]]
    return argv


def is_snapshot(argv: list) -> bool:
    return (
        len(argv) >= 2
        and Path(argv[0]).name == "commonplace"
        and argv[1] == "backup"
        and not NOT_SNAPSHOT_FLAGS.intersection(argv[2:])
    )


def _serve_argv(state: dict) -> list:
    return [
        state["python"],
        "-m",
        "commonplace",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(state["port"]),
    ]


def start_serve(state: dict) -> None:
    """Start the serve process as a fresh container start (new pid and StartedAt)."""
    container = state["container"]
    log = open(Path(state["dir"]) / "serve.log", "ab")
    proc = subprocess.Popen(
        _serve_argv(state),
        cwd=state["data_host"],
        env=container_env(state),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    container.update(
        {"running": True, "paused": False, "pid": proc.pid, "StartedAt": _now_iso(), "ExitCode": 0}
    )


def stop_serve(state: dict, timeout: float = 10.0, sig=signal.SIGTERM) -> None:
    container = state["container"]
    pid = container.get("pid")
    if container.get("running") and pid_alive(pid):
        try:
            if container.get("paused"):
                os.killpg(pid, signal.SIGCONT)
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        end = time.monotonic() + timeout
        while pid_alive(pid) and time.monotonic() < end:
            time.sleep(0.05)
        if pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            while pid_alive(pid):
                time.sleep(0.05)
    container.update(
        {"running": False, "paused": False, "pid": 0, "FinishedAt": _now_iso(), "ExitCode": 143}
    )


def _new_container(state: dict) -> None:
    state["container"] = {
        "Id": secrets.token_hex(32),
        "Created": _now_iso(),
        "running": False,
        "paused": False,
        "pid": 0,
        "StartedAt": "0001-01-01T00:00:00Z",
        "FinishedAt": "0001-01-01T00:00:00Z",
        "ExitCode": 0,
        "removed": False,
    }


def _labels(state: dict, oneoff: bool, number: int = 1) -> dict:
    return {
        "com.docker.compose.project": PROJECT,
        "com.docker.compose.service": SERVICE,
        "com.docker.compose.oneoff": "True" if oneoff else "False",
        "com.docker.compose.container-number": str(number),
        "com.docker.compose.project.config_files": state.get("compose_file", ""),
    }


def inspect_main(state: dict) -> dict:
    c = state["container"]
    status = "paused" if c.get("paused") else ("running" if c["running"] else "exited")
    return {
        "Id": c["Id"],
        "Created": c["Created"],
        "Path": "commonplace",
        "Args": ["serve", "--host", "0.0.0.0", "--port", "8765"],
        "State": {
            "Status": status,
            "Running": bool(c["running"]),
            "Paused": bool(c.get("paused")),
            "Restarting": False,
            "OOMKilled": False,
            "Dead": False,
            "Pid": c["pid"] if c["running"] else 0,
            "ExitCode": c["ExitCode"],
            "Error": "",
            "StartedAt": c["StartedAt"],
            "FinishedAt": c["FinishedAt"],
        },
        "Image": "sha256:" + "0" * 64,
        "Name": f"/{PROJECT}-{SERVICE}-1",
        "RestartCount": 0,
        "HostConfig": {"Init": True, "RestartPolicy": {"Name": "unless-stopped"}},
        "Mounts": [
            {
                "Type": "bind",
                "Source": state["data_host"],
                "Destination": "/data",
                "Mode": "rw",
                "RW": True,
                "Propagation": "rprivate",
            }
        ],
        "Config": {
            "Hostname": c["Id"][:12],
            "User": "10001:10001",
            "Env": [f"{k}={v}" for k, v in sorted(container_env(state, host=False).items())],
            "Cmd": ["commonplace", "serve", "--host", "0.0.0.0", "--port", "8765"],
            "Image": IMAGE,
            "WorkingDir": "/data",
            "Labels": _labels(state, False),
        },
        "NetworkSettings": {
            "Ports": {"8765/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(state["port"])}]}
        },
    }


def inspect_oneoff(state: dict, record: dict) -> dict:
    running = pid_alive(record.get("pid"))
    return {
        "Id": record["Id"],
        "Created": record["Created"],
        "Path": record["argv"][0] if record["argv"] else "",
        "Args": record["argv"][1:],
        "State": {
            "Status": "running" if running else "exited",
            "Running": running,
            "Paused": False,
            "Pid": record.get("pid", 0) if running else 0,
            "ExitCode": record.get("rc") or 0,
            "StartedAt": record["Created"],
            "FinishedAt": record.get("FinishedAt", "0001-01-01T00:00:00Z"),
        },
        "Name": "/" + record["Name"],
        "HostConfig": {"Init": True, "AutoRemove": False},
        "Mounts": [
            {"Type": "bind", "Source": src, "Destination": dst, "RW": True}
            for dst, src in [("/data", state["data_host"]), *record.get("mounts", [])]
        ],
        "Config": {"Image": IMAGE, "Labels": _labels(state, True, record["number"])},
    }


def all_containers(state: dict) -> list:
    out = []
    if not state["container"].get("removed"):
        out.append(inspect_main(state))
    out.extend(inspect_oneoff(state, record) for record in state["oneoffs"].values())
    return out


# --------------------------------------------------------------------------- go templates


_TOKEN = re.compile(r"\{\{(-?)\s*(.*?)\s*(-?)\}\}", re.S)


def _parse_template(text: str):
    pieces = []
    pos = 0
    for match in _TOKEN.finditer(text):
        literal = text[pos : match.start()]
        if match.group(1):
            literal = literal.rstrip()
        pieces.append(("text", literal))
        pieces.append(("action", match.group(2), bool(match.group(3))))
        pos = match.end()
    pieces.append(("text", text[pos:]))
    # Apply right trims.
    for index, piece in enumerate(pieces):
        if piece[0] == "action" and piece[2] and index + 1 < len(pieces):
            pieces[index + 1] = ("text", pieces[index + 1][1].lstrip())

    def block(i, stop):
        nodes = []
        while i < len(pieces):
            piece = pieces[i]
            if piece[0] == "text":
                nodes.append(("text", piece[1]))
                i += 1
                continue
            action = piece[1]
            word = action.split(None, 1)[0] if action else ""
            if word in stop:
                return nodes, i, word
            if word in ("range", "if", "with"):
                body, i, end = block(i + 1, {"else", "end"})
                other = []
                if end == "else":
                    other, i, end = block(i + 1, {"end"})
                if end != "end":
                    raise ValueError("unterminated template block")
                nodes.append((word, action.split(None, 1)[1], body, other))
                i += 1
                continue
            nodes.append(("expr", action))
            i += 1
        if stop:
            raise ValueError("unterminated template block")
        return nodes, i, None

    return block(0, set())[0]


def _split_args(expr: str) -> list:
    return shlex.split(expr, posix=True) if expr else []


def _lookup(dot, path: str):
    value = dot
    for part in [p for p in path.split(".") if p]:
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def _eval(expr: str, dot):
    tokens = _split_args(expr.replace("(", " ").replace(")", " "))
    if not tokens:
        return ""

    def arg(token):
        if token.startswith("."):
            return _lookup(dot, token)
        if token == "$":
            return dot
        if re.fullmatch(r"-?\d+", token):
            return int(token)
        return token

    name, rest = tokens[0], tokens[1:]
    if name == "json":
        return json.dumps(arg(rest[0]) if rest else dot)
    if name == "index":
        value = arg(rest[0])
        for key in rest[1:]:
            value = value.get(arg(key)) if isinstance(value, dict) else value[arg(key)]
        return value
    if name in ("eq", "ne"):
        left = arg(rest[0])
        same = any(left == arg(token) for token in rest[1:])
        return same if name == "eq" else not same
    if name in ("print", "println"):
        text = " ".join(_fmt(arg(t)) for t in rest)
        return text + ("\n" if name == "println" else "")
    if name in ("lower", "upper"):
        value = _fmt(arg(rest[0]))
        return value.lower() if name == "lower" else value.upper()
    if name.startswith(".") or name == "$":
        return arg(name)
    raise ValueError(f"unsupported template function {name!r}")


def _fmt(value) -> str:
    if value is None:
        return "<no value>"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _truthy(value) -> bool:
    return bool(value) and value != "<no value>"


def _render(nodes, dot) -> str:
    out = []
    for node in nodes:
        kind = node[0]
        if kind == "text":
            out.append(node[1])
        elif kind == "expr":
            value = _eval(node[1], dot)
            out.append(_fmt(value))
        elif kind == "range":
            items = _eval(node[1], dot)
            items = list(items.values()) if isinstance(items, dict) else list(items or [])
            out.append(
                "".join(_render(node[2], item) for item in items)
                if items
                else _render(node[3], dot)
            )
        elif kind == "if":
            out.append(_render(node[2] if _truthy(_eval(node[1], dot)) else node[3], dot))
        elif kind == "with":
            value = _eval(node[1], dot)
            out.append(_render(node[2], value) if _truthy(value) else _render(node[3], dot))
    return "".join(out)


def render_template(template: str, dot) -> str:
    if template == "json":
        return json.dumps(dot)
    return _render(_parse_template(template), dot)


# --------------------------------------------------------------------------- fake CLI


class Unsupported(Exception):
    pass


def _take(args: list, i: int, flag: str):
    if "=" in args[i] and args[i].startswith("--"):
        return args[i].split("=", 1)[1], i + 1
    if i + 1 >= len(args):
        raise Unsupported(f"{flag} needs a value")
    return args[i + 1], i + 2


def _compose_globals(args: list):
    opts = {}
    i = 0
    value_flags = {
        "--env-file": "env_file",
        "-f": "file",
        "--file": "file",
        "-p": "project",
        "--project-name": "project",
        "--project-directory": "project_dir",
        "--ansi": None,
        "--progress": None,
        "--profile": None,
        "--parallel": None,
    }
    while i < len(args) and args[i].startswith("-"):
        flag = args[i].split("=", 1)[0]
        if flag not in value_flags:
            raise Unsupported(f"compose option {args[i]}")
        value, i = _take(args, i, flag)
        if value_flags[flag]:
            opts.setdefault(value_flags[flag], []).append(value)
    return opts, args[i:]


def _verb(argv: list):
    """(kind, verb) of a docker invocation, for the call log."""
    if not argv:
        return "docker", ""
    if argv[0] == "compose":
        try:
            _, rest = _compose_globals(argv[1:])
        except Unsupported:
            rest = [a for a in argv[1:] if not a.startswith("-")]
        return "compose", rest[0] if rest else ""
    if argv[0] == "container" and len(argv) > 1:
        return "docker", {"ls": "ps", "list": "ps"}.get(argv[1], argv[1])
    return "docker", argv[0]


def _stdin_target() -> str:
    try:
        return os.readlink("/proc/self/fd/0")
    except OSError:
        return "closed"


def _run_detached(
    state_obj: State,
    state: dict,
    argv: list,
    env: dict,
    cwd: str,
    tag: str,
    snapshot: bool,
    on_start=None,
):
    """Run a container process in its own session; return its exit status (or None if it
    could not start). Output is relayed after it exits, like a non-TTY Docker client."""
    run_dir = Path(state["dir"]) / "procs"
    run_dir.mkdir(exist_ok=True)
    out_path = run_dir / f"{tag}.out"
    err_path = run_dir / f"{tag}.err"
    if snapshot and state.get("hang"):
        env = dict(env)
        dest = Path(argv[-1]) if argv else Path("/nonexistent")
        hang = state["hang"]
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [hang["dir"], env.get("PYTHONPATH")]))
        env["FAKE_HANG_DEST_PARENT"] = str(dest.parent)
        env["FAKE_HANG_DEST_NAME"] = dest.name
        env["FAKE_HANG_SOURCE"] = hang.get("source") or ""
        env["FAKE_HANG_MARK"] = hang["mark"]
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
        except OSError as exc:
            sys.stderr.write(f"OCI runtime exec failed: {exc}\n")
            return 126, None
    with state_obj.edit() as live:
        live["procs"][tag] = {"pid": proc.pid, "argv": argv, "snapshot": snapshot, "rc": None}
        if on_start:
            on_start(live, proc.pid)
    rc = _signal_exit(proc.wait())
    with state_obj.edit() as live:
        live["procs"][tag]["rc"] = rc
    sys.stdout.buffer.write(out_path.read_bytes())
    sys.stdout.flush()
    sys.stderr.buffer.write(err_path.read_bytes())
    sys.stderr.flush()
    return rc, proc.pid


def _exec_opts(args: list):
    opts = {"env": {}, "workdir": None, "user": None}
    i = 0
    while i < len(args) and args[i].startswith("-"):
        flag = args[i].split("=", 1)[0]
        if flag in ("-T", "--no-TTY", "-i", "--interactive", "--privileged", "-t", "--tty"):
            if flag in ("-t", "--tty"):
                raise Unsupported("a TTY is not available to the backup hook")
            opts[flag] = True
            i += 1
        elif flag in ("-e", "--env"):
            value, i = _take(args, i, flag)
            key, _, val = value.partition("=")
            opts["env"][key] = val if _ else os.environ.get(key, "")
        elif flag in ("-w", "--workdir"):
            opts["workdir"], i = _take(args, i, flag)
        elif flag in ("-u", "--user"):
            opts["user"], i = _take(args, i, flag)
        elif flag == "--index":
            _, i = _take(args, i, flag)
        elif flag in ("-d", "--detach"):
            raise Unsupported("detached exec")
        else:
            raise Unsupported(f"exec option {args[i]}")
    return opts, args[i:]


def cmd_exec(state_obj: State, args: list, by_container=False) -> int:
    opts, rest = _exec_opts(args)
    if len(rest) < 2:
        raise Unsupported("exec needs a service and a command")
    target, command = rest[0], rest[1:]
    state = state_obj.read()
    if by_container:
        if not _matches_main(state, target):
            sys.stderr.write(f"Error response from daemon: No such container: {target}\n")
            return 1
    elif target != SERVICE:
        sys.stderr.write(f"no such service: {target}\n")
        return 1
    c = state["container"]
    if c.get("removed") or not c["running"] or not pid_alive(c["pid"]):
        sys.stderr.write(f'service "{SERVICE}" is not running\n')
        return 1
    if c.get("paused"):
        sys.stderr.write(f"Error response from daemon: Container {c['Id']} is paused\n")
        return 1
    hang_flag = state.get("exec_hang")
    if hang_flag and hang_flag in command:
        fd = os.open(Path(state["dir"]) / "exec_hang.mark", os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        os.write(fd, f"{os.getpid()} {hang_flag}\n".encode())
        os.close(fd)
        while True:
            time.sleep(0.2)
    snapshot = is_snapshot(command)
    if snapshot and state.get("snapshot_exit") is not None:
        return int(state["snapshot_exit"])
    env = container_env(state, opts["env"])
    cwd = translate(state, opts["workdir"] or "/data")
    tag = f"exec-{time.time_ns()}-{os.getpid()}"
    rc, _ = _run_detached(state_obj, state, host_argv(state, command), env, cwd, tag, snapshot)
    return rc


def cmd_run(state_obj: State, args: list) -> int:
    opts = {"rm": False, "mounts": [], "env": {}, "workdir": None}
    i = 0
    while i < len(args) and args[i].startswith("-"):
        flag = args[i].split("=", 1)[0]
        if flag == "--rm":
            opts["rm"] = True
            i += 1
        elif flag in ("--no-deps", "-T", "--no-TTY", "-i", "--interactive", "--service-ports"):
            i += 1
        elif flag in ("-v", "--volume"):
            value, i = _take(args, i, flag)
            parts = value.split(":")
            if len(parts) < 2 or not parts[0].startswith("/"):
                raise Unsupported(f"volume {value}")
            opts["mounts"].append((parts[1], str(Path(parts[0]))))
        elif flag in ("-e", "--env"):
            value, i = _take(args, i, flag)
            key, eq, val = value.partition("=")
            opts["env"][key] = val if eq else os.environ.get(key, "")
        elif flag in ("-w", "--workdir"):
            opts["workdir"], i = _take(args, i, flag)
        elif flag in ("-u", "--user", "--name", "--entrypoint"):
            if flag == "--entrypoint":
                raise Unsupported("run --entrypoint")
            _, i = _take(args, i, flag)
        elif flag in ("-d", "--detach"):
            raise Unsupported("detached run")
        else:
            raise Unsupported(f"run option {args[i]}")
    rest = args[i:]
    if not rest or rest[0] != SERVICE:
        sys.stderr.write("no such service\n")
        return 1
    command = rest[1:] or ["commonplace", "serve", "--host", "0.0.0.0", "--port", "8765"]
    oneoff_id = secrets.token_hex(32)
    with state_obj.edit() as state:
        number = len(state["oneoffs"]) + 1
        state["oneoffs"][oneoff_id] = {
            "Id": oneoff_id,
            "Name": f"{PROJECT}-{SERVICE}-run-{oneoff_id[:12]}",
            "Created": _now_iso(),
            "argv": command,
            "mounts": opts["mounts"],
            "number": number,
            "pid": 0,
            "rc": None,
        }
        snapshot_state = dict(state)
    mounts = opts["mounts"]
    argv = host_argv(snapshot_state, command, mounts)
    env = container_env(snapshot_state, opts["env"])
    cwd = translate(snapshot_state, opts["workdir"] or "/data", mounts)
    tag = f"run-{oneoff_id[:12]}"
    snapshot = is_snapshot(command)
    if snapshot and snapshot_state.get("snapshot_exit") is not None:
        rc = int(snapshot_state["snapshot_exit"])
    else:

        def started(live, pid):
            if oneoff_id in live["oneoffs"]:
                live["oneoffs"][oneoff_id]["pid"] = pid

        rc, _ = _run_detached(state_obj, snapshot_state, argv, env, cwd, tag, snapshot, started)
    with state_obj.edit() as state:
        if opts["rm"]:
            state["oneoffs"].pop(oneoff_id, None)
        elif oneoff_id in state["oneoffs"]:
            state["oneoffs"][oneoff_id].update({"rc": rc, "FinishedAt": _now_iso()})
    return rc


def _service_args(args: list, value_flags=(), bool_flags=()):
    opts = {}
    services = []
    i = 0
    while i < len(args):
        arg = args[i]
        flag = arg.split("=", 1)[0]
        if flag in value_flags:
            opts[flag], i = _take(args, i, flag)
        elif flag in bool_flags:
            opts[flag] = True
            i += 1
        elif arg.startswith("-"):
            raise Unsupported(f"option {arg}")
        else:
            services.append(arg)
            i += 1
    if any(service != SERVICE for service in services):
        raise Unsupported(f"service {services}")
    return opts


def _ensure_container(state: dict) -> None:
    if state["container"].get("removed"):
        _new_container(state)


def cmd_lifecycle(state_obj: State, verb: str, args: list) -> int:
    with state_obj.edit() as state:
        c = state["container"]
        if verb == "stop":
            opts = _service_args(args, ("-t", "--timeout"))
            if not c.get("removed"):
                stop_serve(state, float(opts.get("-t", opts.get("--timeout", 10))))
        elif verb == "kill":
            _service_args(args, ("-s", "--signal"), ("--remove-orphans",))
            if not c.get("removed"):
                stop_serve(state, 0.0, signal.SIGKILL)
        elif verb == "start":
            _service_args(args)
            if c.get("removed"):
                sys.stderr.write(f"service {SERVICE!r} has no container to start\n")
                return 1
            if not c["running"]:
                start_serve(state)
        elif verb == "restart":
            opts = _service_args(args, ("-t", "--timeout"), ("--no-deps",))
            if c.get("removed"):
                return 0
            stop_serve(state, float(opts.get("-t", opts.get("--timeout", 10))))
            start_serve(state)
        elif verb in ("up", "create"):
            opts = _service_args(
                args,
                ("-t", "--timeout", "--pull", "--wait-timeout"),
                (
                    "-d",
                    "--detach",
                    "--no-deps",
                    "--force-recreate",
                    "--no-recreate",
                    "--build",
                    "--no-build",
                    "--remove-orphans",
                    "--wait",
                    "--no-start",
                    "--quiet-pull",
                ),
            )
            if opts.get("--force-recreate") and not c.get("removed"):
                stop_serve(state)
                c["removed"] = True
            _ensure_container(state)
            if verb == "up" and not opts.get("--no-start") and not state["container"]["running"]:
                start_serve(state)
        elif verb == "down":
            _service_args(
                args, ("-t", "--timeout", "--rmi"), ("-v", "--volumes", "--remove-orphans")
            )
            if not c.get("removed"):
                stop_serve(state)
                c["removed"] = True
            state["oneoffs"].clear()
        elif verb == "rm":
            opts = _service_args(args, (), ("-f", "--force", "-s", "--stop", "-v", "--volumes"))
            if c["running"] and not (opts.get("-s") or opts.get("--stop")):
                return 0
            stop_serve(state)
            c["removed"] = True
        elif verb in ("pause", "unpause"):
            _service_args(args)
            if c["running"] and pid_alive(c["pid"]):
                os.killpg(c["pid"], signal.SIGSTOP if verb == "pause" else signal.SIGCONT)
                c["paused"] = verb == "pause"
    return 0


def _main_ps_row(state: dict) -> dict:
    c = state["container"]
    status = "paused" if c.get("paused") else ("running" if c["running"] else "exited")
    return {
        "ID": c["Id"],
        "Name": f"{PROJECT}-{SERVICE}-1",
        "Names": f"{PROJECT}-{SERVICE}-1",
        "Image": IMAGE,
        "Command": '"commonplace serve --host 0.0.0.0 --port 8765"',
        "Project": PROJECT,
        "Service": SERVICE,
        "State": status,
        "Status": "Up 1 minute (healthy)" if status == "running" else "Exited (143)",
        "Health": "healthy" if status == "running" else "",
        "ExitCode": c["ExitCode"],
        "Labels": ",".join(f"{k}={v}" for k, v in _labels(state, False).items()),
        "Publishers": [
            {
                "URL": "127.0.0.1",
                "TargetPort": 8765,
                "PublishedPort": state["port"],
                "Protocol": "tcp",
            }
        ],
    }


def cmd_compose_ps(state_obj: State, args: list) -> int:
    opts = _service_args(
        args,
        ("--status", "--format", "--filter"),
        ("-q", "--quiet", "-a", "--all", "--services", "--no-trunc", "--orphans"),
    )
    state = state_obj.read()
    c = state["container"]
    rows = []
    if not c.get("removed"):
        row = _main_ps_row(state)
        if opts.get("--status") and row["State"] != opts["--status"]:
            pass
        elif (
            row["State"] == "running" or opts.get("-a") or opts.get("--all") or opts.get("--status")
        ):
            rows.append(row)
    if opts.get("--services"):
        print("\n".join(SERVICE for _ in rows[:1]))
        return 0
    if opts.get("-q") or opts.get("--quiet"):
        for row in rows:
            print(row["ID"])
        return 0
    fmt = opts.get("--format")
    if fmt:
        for row in rows:
            print(json.dumps(row) if fmt == "json" else render_template(fmt, row))
        return 0
    print("NAME                IMAGE               COMMAND             SERVICE   STATUS")
    for row in rows:
        print(f"{row['Name']}  {row['Image']}  {row['Command']}  {row['Service']}  {row['Status']}")
    return 0


def _matches_main(state: dict, ref: str) -> bool:
    c = state["container"]
    if c.get("removed") or not ref:
        return False
    name = f"{PROJECT}-{SERVICE}-1"
    return c["Id"].startswith(ref) or ref.lstrip("/") == name


def cmd_inspect(state_obj: State, args: list) -> int:
    fmt = None
    refs = []
    i = 0
    while i < len(args):
        flag = args[i].split("=", 1)[0]
        if flag in ("-f", "--format"):
            fmt, i = _take(args, i, flag)
        elif flag == "--type":
            kind, i = _take(args, i, flag)
            if kind != "container":
                raise Unsupported(f"inspect --type {kind}")
        elif flag in ("-s", "--size"):
            i += 1
        elif args[i].startswith("-"):
            raise Unsupported(f"inspect option {args[i]}")
        else:
            refs.append(args[i])
            i += 1
    state = state_obj.read()
    found, missing = [], []
    for ref in refs:
        objects = [
            o
            for o in all_containers(state)
            if o["Id"].startswith(ref) or o["Name"].lstrip("/") == ref.lstrip("/")
        ]
        if ref and len(objects) == 1:
            found.append(objects[0])
        else:
            missing.append(ref)
    if fmt is None:
        print(json.dumps(found, indent=4))
    else:
        for obj in found:
            print(render_template(fmt, obj))
    for ref in missing:
        sys.stderr.write(f"Error: No such object: {ref}\n")
    return 1 if missing else 0


def cmd_docker_ps(state_obj: State, args: list) -> int:
    opts = {"filters": []}
    i = 0
    while i < len(args):
        flag = args[i].split("=", 1)[0]
        if flag in ("-a", "--all", "-q", "--quiet", "--no-trunc"):
            opts[flag] = True
            i += 1
        elif flag in ("-f", "--filter"):
            value, i = _take(args, i, flag)
            opts["filters"].append(value)
        elif flag == "--format":
            opts["format"], i = _take(args, i, flag)
        else:
            raise Unsupported(f"ps option {args[i]}")
    state = state_obj.read()
    rows = []
    for obj in all_containers(state):
        if not obj["State"]["Running"] and not (opts.get("-a") or opts.get("--all")):
            continue
        keep = True
        for flt in opts["filters"]:
            key, _, value = flt.partition("=")
            labels = obj["Config"]["Labels"]
            if key == "label":
                lkey, eq, lval = value.partition("=")
                keep &= lkey in labels and (not eq or labels[lkey] == lval)
            elif key == "name":
                keep &= value in obj["Name"]
            elif key == "id":
                keep &= obj["Id"].startswith(value)
            elif key == "status":
                keep &= obj["State"]["Status"] == value
            else:
                raise Unsupported(f"ps filter {flt}")
        if keep:
            rows.append(obj)
    for obj in rows:
        short = obj["Id"] if opts.get("--no-trunc") else obj["Id"][:12]
        if opts.get("-q") or opts.get("--quiet"):
            print(short)
        elif opts.get("format"):
            row = {
                "ID": short,
                "Names": obj["Name"].lstrip("/"),
                "Image": obj["Config"]["Image"],
                "State": obj["State"]["Status"],
                "Status": obj["State"]["Status"],
                "Labels": ",".join(f"{k}={v}" for k, v in obj["Config"]["Labels"].items()),
            }
            print(
                json.dumps(row)
                if opts["format"] == "json"
                else render_template(opts["format"], row)
            )
        else:
            print(
                f"{short}   {obj['Config']['Image']}   {obj['State']['Status']}   {obj['Name'].lstrip('/')}"
            )
    return 0


def _command_of(verb: str, words: list):
    """The container command of a logged compose exec/run call, or None."""
    try:
        if verb == "exec":
            _, rest = _exec_opts(words)
        else:
            i = 0
            while i < len(words) and words[i].startswith("-"):
                flag = words[i].split("=", 1)[0]
                takes = flag in (
                    "-v",
                    "--volume",
                    "-e",
                    "--env",
                    "-w",
                    "--workdir",
                    "-u",
                    "--user",
                    "--name",
                    "--entrypoint",
                )
                i += 2 if takes and "=" not in words[i] else 1
            rest = words[i:]
    except Unsupported:
        return None
    return rest[1:] if rest and rest[0] == SERVICE else None


def docker_main(argv: list) -> int:
    state_dir = os.environ.get("FAKE_DOCKER_STATE")
    if not state_dir:
        sys.stderr.write("fake docker: FAKE_DOCKER_STATE is not set\n")
        return 125
    state_obj = State(state_dir)
    kind, verb = _verb(argv)
    state_obj.log(
        {
            "command": "docker",
            "argv": argv,
            "kind": kind,
            "verb": verb,
            "stdin": _stdin_target(),
            "pid": os.getpid(),
            "time": time.time(),
        }
    )
    try:
        if not argv:
            raise Unsupported("no command")
        if argv[0] == "compose":
            opts, rest = _compose_globals(argv[1:])
            for path in opts.get("file", []) + opts.get("env_file", []):
                if not Path(path).is_file():
                    sys.stderr.write(f"open {path}: no such file or directory\n")
                    return 1
            if not rest:
                raise Unsupported("compose without a command")
            sub, sub_args = rest[0], rest[1:]
            if sub == "exec":
                return cmd_exec(state_obj, sub_args)
            if sub == "run":
                return cmd_run(state_obj, sub_args)
            if sub == "ps":
                return cmd_compose_ps(state_obj, sub_args)
            if sub in DISPLACING_VERBS:
                return cmd_lifecycle(state_obj, sub, sub_args)
            if sub == "version":
                print("Docker Compose version v2.29.7")
                return 0
            raise Unsupported(f"compose {sub}")
        if argv[0] == "container" and len(argv) > 1:
            argv = [{"ls": "ps", "list": "ps"}.get(argv[1], argv[1]), *argv[2:]]
        if argv[0] == "inspect":
            return cmd_inspect(state_obj, argv[1:])
        if argv[0] == "ps":
            return cmd_docker_ps(state_obj, argv[1:])
        if argv[0] == "exec":
            return cmd_exec(state_obj, argv[1:], by_container=True)
        if argv[0] == "version":
            print("Docker version 27.3.1, build fake")
            return 0
        if argv[0] in DISPLACING_VERBS:
            # Plain docker lifecycle calls act on the serve container by id or name.
            refs = [a for a in argv[1:] if not a.startswith("-")]
            state = state_obj.read()
            if refs and all(_matches_main(state, ref) for ref in refs):
                return cmd_lifecycle(state_obj, argv[0], [])
        raise Unsupported(" ".join(argv[:2]))
    except Unsupported as exc:
        sys.stderr.write(f"fake docker: unsupported: {exc}\n")
        return 125


def install_main(argv: list) -> int:
    """GNU install subset: directories (``-d``) and file copies, owners ignored."""
    state_dir = os.environ.get("FAKE_DOCKER_STATE")
    if state_dir:
        State(state_dir).log(
            {"command": "install", "argv": argv, "pid": os.getpid(), "time": time.time()}
        )
    mode = 0o755
    directory = False
    paths = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            paths.extend(argv[i + 1 :])
            break
        if arg in ("-d", "--directory"):
            directory = True
        elif arg in ("-o", "-g", "--owner", "--group"):
            i += 1  # Ownership needs root; the fake container user is this uid.
        elif arg.startswith(("--owner=", "--group=")):
            pass
        elif arg in ("-m", "--mode"):
            mode = int(argv[i + 1], 8)
            i += 1
        elif arg.startswith("--mode="):
            mode = int(arg.split("=", 1)[1], 8)
        elif arg.startswith("-"):
            sys.stderr.write(f"install (fake): unsupported option {arg}\n")
            return 1
        else:
            paths.append(arg)
        i += 1
    if directory:
        for path in paths:
            Path(path).mkdir(parents=True, exist_ok=True)
            os.chmod(path, mode)
        return 0
    if len(paths) != 2:
        sys.stderr.write("install (fake): expected SOURCE DEST\n")
        return 1
    source, dest = Path(paths[0]), Path(paths[1])
    if dest.is_dir():
        dest = dest / source.name
    dest.write_bytes(source.read_bytes())
    os.chmod(dest, mode)
    return 0


# --------------------------------------------------------------------------- hang hook

HANG_SITECUSTOMIZE = r'''
"""Test-only fault injection: block the snapshot on its first destination write."""
import os
import sys
import time

_PARENT = os.environ.get("FAKE_HANG_DEST_PARENT", "")
_NAME = os.environ.get("FAKE_HANG_DEST_NAME", "")
_SOURCE = os.environ.get("FAKE_HANG_SOURCE", "")
_MARK = os.environ.get("FAKE_HANG_MARK", "")
_EVENTS = {"open", "os.mkdir", "shutil.copyfile", "shutil.copytree", "sqlite3.connect",
           "os.rename", "os.link", "os.symlink", "os.truncate"}


def _hit(value):
    if isinstance(value, (bytes, str)) or hasattr(value, "__fspath__"):
        try:
            path = os.path.abspath(os.fsdecode(value))
        except (TypeError, ValueError):
            return False
        if _SOURCE and path == _SOURCE:
            return True
        if _PARENT and path.startswith(_PARENT + os.sep):
            first, _, rest = path[len(_PARENT) + 1:].partition(os.sep)
            return bool(_NAME) and _NAME in first and bool(rest)
    return False


def _hook(event, args):
    if event in _EVENTS and any(_hit(arg) for arg in args):
        fd = os.open(_MARK, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.write(fd, f"{os.getpid()} {event}\n".encode())
        os.close(fd)
        while True:
            time.sleep(0.2)


if _PARENT or _SOURCE:
    sys.addaudithook(_hook)
'''


# --------------------------------------------------------------------------- test-side engine


class FakeEngine:
    """Owns the fake Docker host for one test."""

    def __init__(self, base, data_host, *, env=None, hang=False, hang_source=None):
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.data = Path(data_host).resolve()
        self.bin = self.base / "bin"
        self.bin.mkdir(exist_ok=True)
        self.state_dir = self.base / "engine"
        self.state_dir.mkdir(exist_ok=True)
        # AF_UNIX paths are short; keep the container's private /tmp socket dir short too.
        self.socket_dir = Path(tempfile.mkdtemp(prefix="cpfk-", dir="/tmp"))
        self.socket_dir.chmod(0o700)
        (self.base / "home").mkdir(exist_ok=True)
        (self.base / "tmp").mkdir(exist_ok=True)
        self.compose_file = self.base / "compose.yml"
        self.env_file = self.base / "operator.env"
        self.compose_file.write_text("services:\n  commonplace:\n    image: commonplace:local\n")
        self.env_file.write_text("COMMONPLACE_BASE_URL=http://127.0.0.1\n")
        self.state = State(self.state_dir)
        hang_cfg = None
        if hang:
            hang_dir = self.base / "hang"
            hang_dir.mkdir(exist_ok=True)
            (hang_dir / "sitecustomize.py").write_text(HANG_SITECUSTOMIZE)
            hang_cfg = {
                "dir": str(hang_dir),
                "mark": str(self.base / "hang.mark"),
                "source": str(self.data / hang_source) if hang_source else "",
            }
        initial = {
            "dir": str(self.state_dir),
            "data_host": str(self.data),
            "python": sys.executable,
            "home": str(self.base / "home"),
            "tmp": str(self.base / "tmp"),
            "admin_socket": str(self.socket_dir / "gateway.sock"),
            "port": free_port(),
            "env": dict(env or {}),
            "compose_file": str(self.compose_file),
            "hang": hang_cfg,
            "snapshot_exit": None,
            "oneoffs": {},
            "procs": {},
        }
        _new_container(initial)
        self.state.path.write_text(json.dumps(initial, indent=1, sort_keys=True))
        python = shlex.quote(sys.executable)
        for name in ("docker", "install"):
            wrapper = self.bin / name
            wrapper.write_text(
                f'#!/bin/sh\nexec {python} -I {shlex.quote(str(FAKES))} {name} "$@"\n'
            )
            wrapper.chmod(0o755)

    # -- lifecycle (not logged as client calls) ----------------------------------------
    def up(self, timeout: float = 60.0) -> None:
        subprocess.run(
            [sys.executable, "-I", str(FAKES), "engine-start", str(self.state_dir)],
            check=True,
            timeout=30,
        )
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.healthz() == 200:
                return
            if not self.serve_alive():
                break
            time.sleep(0.1)
        raise RuntimeError(f"fake serve did not become healthy:\n{self.serve_log()[-4000:]}")

    def stop_serve_directly(self) -> None:
        with self.state.edit() as state:
            stop_serve(state)

    def set_snapshot_exit(self, code) -> None:
        with self.state.edit() as state:
            state["snapshot_exit"] = code

    def hang_exec(self, flag) -> None:
        """Hang the client of every later ``compose exec`` whose command has ``flag``."""
        with self.state.edit() as state:
            state["exec_hang"] = flag

    def exec_hang_engaged(self) -> bool:
        return (self.state_dir / "exec_hang.mark").exists()

    # -- observation ------------------------------------------------------------------
    def hook_env(self) -> dict:
        return {
            "PATH": f"{self.bin}{os.pathsep}{os.defpath}",
            "FAKE_DOCKER_STATE": str(self.state_dir),
            "COMPOSE_FILE": str(self.compose_file),
            "COMMONPLACE_ENV_FILE": str(self.env_file),
            "COMMONPLACE_DEPLOY_DIR": str(self.base),
            "LANG": "C.UTF-8",
            "HOME": str(self.base / "home"),
        }

    def calls(self) -> list:
        path = self.state_dir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def docker_calls(self) -> list:
        return [call for call in self.calls() if call["command"] == "docker"]

    def snapshot_calls(self) -> list:
        out = []
        for call in self.docker_calls():
            argv = call["argv"]
            if call["kind"] != "compose" or call["verb"] not in ("exec", "run"):
                continue
            try:
                _, rest = _compose_globals(argv[1:])
            except Unsupported:
                continue
            command = _command_of(rest[0], rest[1:])
            if command is not None and is_snapshot(command):
                out.append(call)
        return out

    def displacing_calls(self) -> list:
        return [call for call in self.docker_calls() if call["verb"] in DISPLACING_VERBS]

    def identity(self) -> tuple:
        c = self.state.read()["container"]
        return (None if c.get("removed") else c["Id"], c["StartedAt"], c["pid"], c["running"])

    def serve_alive(self) -> bool:
        c = self.state.read()["container"]
        return bool(c["running"]) and pid_alive(c["pid"])

    def healthz(self, timeout: float = 2.0):
        port = self.state.read()["port"]
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except (OSError, ValueError) as exc:
            return f"{type(exc).__name__}: {exc}"

    def oneoffs(self) -> list:
        return list(self.state.read()["oneoffs"].values())

    def container_processes(self) -> list:
        """Live processes started inside the fake container other than serve's session."""
        state = self.state.read()
        serve_pid = state["container"]["pid"] if state["container"]["running"] else None
        serve_sid = _proc_sid(serve_pid) if serve_pid else None
        out = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == os.getpid() or not pid_alive(pid):
                continue
            if _proc_env(pid).get(MARK_ENV) != str(self.state_dir):
                continue
            if serve_sid is not None and _proc_sid(pid) == serve_sid:
                continue
            out.append({"pid": pid, "argv": _cmdline(pid)})
        return out

    def hang_engaged(self) -> bool:
        hang = self.state.read().get("hang")
        return bool(hang) and Path(hang["mark"]).exists()

    def serve_log(self) -> str:
        path = self.state_dir / "serve.log"
        return path.read_text(errors="replace") if path.exists() else ""

    def close(self) -> None:
        """Kill everything the fake host started; never leaves processes behind."""
        for _ in range(3):
            victims = [p["pid"] for p in self.container_processes()]
            state = self.state.read()
            if state["container"]["running"]:
                victims.append(state["container"]["pid"])
            victims = [pid for pid in victims if pid_alive(pid)]
            if not victims:
                break
            for pid in victims:
                for kill in (os.killpg, os.kill):
                    try:
                        kill(pid, signal.SIGCONT)
                        kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            end = time.monotonic() + 5
            while any(pid_alive(pid) for pid in victims) and time.monotonic() < end:
                time.sleep(0.05)
        for child in self.socket_dir.iterdir() if self.socket_dir.exists() else []:
            try:
                child.unlink()
            except OSError:
                pass
        try:
            self.socket_dir.rmdir()
        except OSError:
            pass


def _engine_start(state_dir: str) -> int:
    state_obj = State(state_dir)
    with state_obj.edit() as state:
        if state["container"].get("removed"):
            _new_container(state)
        if not state["container"]["running"]:
            start_serve(state)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return 2
    tool, rest = argv[0], argv[1:]
    if tool == "docker":
        return docker_main(rest)
    if tool == "install":
        return install_main(rest)
    if tool == "engine-start":
        return _engine_start(rest[0])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
