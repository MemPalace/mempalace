"""Opt-in local launch client with fenced starts and durable result replay."""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from ..client import ClientError, _endpoint
from ..handoff.hook import _atomic_text, _NoRedirect
from ..mailer import _loopback

WORKSPACE_STRATEGY = {"type": "root"}  # UNVERIFIED until D2
SPAWN_MESSAGE = (
    "You were started for Commonplace card #{n} only "
    "(job marker commonplace-job:{job_id}). "
    "First claim it with task_take_next(task={n}). "
    "If that claim is refused, stop and do nothing else. "
    "Touch no other card: do not claim, update, comment on or hand off "
    "any card except #{n}."
)

_MODES = ("approval-required", "auto-accept-edits", "auto", "full-access")
_CHILD_KEYS = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "T3CODE_HOME",
)
_CONFIG_KEYS = {
    "hub_url",
    "token_env",
    "t3_grant_env",
    "t3_mcp_endpoint",
    "t3_executable",
    "t3_timeout_seconds",
    "poll_seconds",
    "journal_dir",
    "topics",
    "binding_acks",
    "projects",
}
_PROJECT_KEYS = {"project_id", "shared_checkout_ack", "runtime_ceiling", "runtime_ack"}


class _ConfigError(ValueError):
    """Contains only a fixed diagnostic or a validated project label."""


def _require(condition, message="invalid configuration"):
    if not condition:
        raise _ConfigError(message)


def _strings(value):
    return isinstance(value, list) and all(isinstance(v, str) and v for v in value)


def _url(value):
    scheme, host, _, _ = _endpoint(value)
    _require(scheme == "https" or _loopback(host), "endpoints require https or loopback http")


def _projects(config):
    projects = config["projects"]
    _require(isinstance(projects, dict) and 1 <= len(projects) <= 32, "invalid projects")
    ids = set()
    for label, project in projects.items():
        _require(re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", label), "invalid project label")
        _require(isinstance(project, dict) and not project.keys() - _PROJECT_KEYS)
        ack = f"{label}:shared-checkout"
        _require(
            project.get("shared_checkout_ack") == ack,
            f'project {label} needs shared_checkout_ack "{ack}"',
        )
        project_id = project.get("project_id")
        _require(isinstance(project_id, str) and project_id.strip(), "invalid project_id")
        _require(project_id not in ids, "project labels must use distinct project_id values")
        ids.add(project_id)
        ceiling = project.get("runtime_ceiling", "approval-required")
        _require(ceiling in _MODES, "invalid runtime_ceiling")
        if ceiling != "approval-required":
            _require(project.get("runtime_ack") == f"{label}:{ceiling}", "runtime_ack required")
        for topic in config["topics"]:
            _require(f"{label}:{topic}" in config["binding_acks"], "binding_ack required")


def _read(config_path) -> dict:
    path = Path(config_path).expanduser()
    info = path.stat()
    if os.name == "posix":
        _require(
            stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.getuid(),
            "config must be owner-written with mode 0600",
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _load(config_path):
    config = _read(config_path)
    _require(isinstance(config, dict) and config.keys() - {"chat"} == _CONFIG_KEYS)
    _url(config["hub_url"])
    _url(config["t3_mcp_endpoint"])
    for key in ("token_env", "t3_grant_env"):
        _require(
            isinstance(config[key], str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config[key]),
            "invalid credential environment name",
        )
    _require(config["token_env"] != config["t3_grant_env"], "credentials must be separate")
    argv = config["t3_executable"]
    _require(_strings(argv) and argv, "invalid t3_executable")
    _require(Path(argv[0]).is_absolute() and Path(argv[0]).is_file(), "invalid t3_executable")
    for key, low, high in (("t3_timeout_seconds", 2, 60), ("poll_seconds", 15, 300)):
        _require(type(config[key]) is int and low <= config[key] <= high, f"invalid {key}")
    _require(_strings(config["topics"]) and config["topics"], "topics must not be empty")
    _require(
        all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}", t) for t in config["topics"]),
        "invalid topic",
    )
    _require(_strings(config["binding_acks"]), "invalid binding_acks")
    _require(
        isinstance(config["journal_dir"], str) and config["journal_dir"], "invalid journal_dir"
    )
    _projects(config)
    if "chat" in config:
        from . import chat_agent

        config["chat"] = chat_agent.validate_chat(config["chat"])
    return config


def _environment(config):
    token = os.environ.get(config["token_env"], "")
    grant = os.environ.get(config["t3_grant_env"], "")
    _require(
        token and grant and "\n" not in token + grant and "\r" not in token + grant,
        "credential environment variables required",
    )
    env = {
        key: os.environ[key]
        for key in _CHILD_KEYS
        if key in os.environ and key not in (config["token_env"], config["t3_grant_env"])
    }
    env.update(
        T3_ACP_MCP_ENDPOINT=config["t3_mcp_endpoint"], T3_ACP_MCP_AUTHORIZATION="Bearer " + grant
    )
    _require(
        not any(token in value for value in env.values()), "machine credential in child environment"
    )
    _require(
        not any(token in arg or grant in arg for arg in config["t3_executable"]),
        "credential in executable arguments",
    )
    return token, grant, env


def validate_job(job) -> None:
    """Validate untrusted identifiers before using them in any agent text."""
    if not isinstance(job, dict):
        raise ValueError("job_invalid")
    number, job_id = job.get("task_number"), job.get("job_id")
    if (
        type(number) is not int
        or not 1 <= number <= 10**9
        or not isinstance(job_id, str)
        or not re.fullmatch(r"job_[0-9a-f]{16}", job_id)
        or job.get("title_marker") != "commonplace-job:" + job_id
    ):
        raise ValueError("job_invalid")


def _job_code(config, job):
    try:
        validate_job(job)
    except ValueError:
        return "job_invalid"
    project = job.get("project")
    if not isinstance(project, str) or project not in config["projects"]:
        return "project_not_configured"
    topic = job.get("topic")
    if topic not in config["topics"]:
        return "topic_not_allowed"
    if f"{project}:{topic}" not in config["binding_acks"]:
        return "binding_not_acked"
    mode = job.get("runtime_mode")
    ceiling = config["projects"][project].get("runtime_ceiling", "approval-required")
    if mode not in _MODES or _MODES.index(mode) > _MODES.index(ceiling):
        return "mode_not_allowed"
    if not Path(config["t3_executable"][0]).is_file():
        return "t3_missing"
    return None


class _HubError(Exception):
    def __init__(self, kind, status=None):
        self.kind, self.status = kind, status
        super().__init__(kind)

    def report(self):
        if self.kind == "credential":
            message = "credential revoked or expired"
        elif self.kind == "fenced":
            message = "job fenced"
        elif self.kind == "refused":
            message = "hub refused (404)"
        else:
            message = f"hub unavailable ({self.status if self.status is not None else 'network'})"
        print("runner: " + message, file=sys.stderr)


class _Hub:
    def __init__(self, config, token):
        self.url = config["hub_url"].rstrip("/")
        self.token = token
        self.poll = config["poll_seconds"]
        self.failures = 0
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def delay(self):
        return min(self.poll * 2 ** min(self.failures, 5), 300)

    def post(self, route, body, *, prefix="/api/runner/"):
        status = None
        try:
            request = Request(
                self.url + prefix + route,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Content-Type": "application/json",
                },
            )
            with self.opener.open(request, timeout=15) as response:
                status = response.status
                if status == 204:
                    result = None
                else:
                    raw = response.read(65537)
                    if len(raw) > 65536:
                        raise ValueError
                    result = json.loads(raw)
                    if not isinstance(result, dict):
                        raise ValueError
                    if route == "lease" and (
                        not isinstance(result.get("lease_token"), str) or not result["lease_token"]
                    ):
                        raise ValueError
        except HTTPError as exc:
            status = exc.code
            exc.close()
            kind = (
                "credential"
                if status == 401
                else "refused"
                if status == 404
                else "fenced"
                if status == 409 and route in {"starting", "result", "answer", "fail"}
                else "transient"
            )
        except (
            OSError,
            URLError,
            http.client.HTTPException,
            ValueError,
            UnicodeError,
            RecursionError,
        ):
            kind = "transient"
        else:
            self.failures = 0
            return result
        self.failures += 1
        raise _HubError(kind, status)


def _parse_launch(result, secrets):
    uncertain = {"outcome": "uncertain", "code": "launch_unknown"}
    if result.returncode != 0:
        return uncertain
    try:
        data = json.loads(result.stdout)
    except (ValueError, UnicodeError, RecursionError):
        return uncertain
    if not isinstance(data, dict) or data.get("isError", False) is not False:
        return uncertain
    content = data.get("structuredContent")
    if not isinstance(content, dict):
        return uncertain
    thread = content.get("threadId")
    if (
        not isinstance(thread, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}", thread)
        or not isinstance(content.get("runId"), str)
        or not content["runId"]
        or content.get("status") in ("failed", "cancelled", "interrupted", "rolled_back")
        or any(secret in thread for secret in secrets)
    ):
        return uncertain
    return {"outcome": "started", "thread_id": thread}


def _spawn(config, job, env, secrets):
    launch = {
        "title": f"Card #{job['task_number']} [commonplace-job:{job['job_id']}]",
        "projectId": config["projects"][job["project"]]["project_id"],
        "message": SPAWN_MESSAGE.format(n=job["task_number"], job_id=job["job_id"]),
        "runtimeMode": job["runtime_mode"],
        "topic": job["topic"],
        "workspaceStrategy": WORKSPACE_STRATEGY,
    }
    _require(not any(secret in json.dumps(launch) for secret in secrets), "credential in launch")
    try:
        result = subprocess.run(
            [*config["t3_executable"], "acp-mcp-call", "t3_thread_launch", json.dumps(launch)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=config["t3_timeout_seconds"],
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"outcome": "uncertain", "code": "launch_unknown"}
    return _parse_launch(result, secrets)


def _save(path, entry):
    _atomic_text(path, json.dumps(entry))


def _deliver(hub, path, entry, replay=False):
    body = {
        key: entry[key] for key in ("lease_token", "outcome", "thread_id", "code") if key in entry
    }
    if body["outcome"] == "spawning":
        body.update(outcome="uncertain", code="restart_unknown")
    try:
        hub.post("result", body)
    except _HubError as exc:
        if exc.kind != "fenced" and not (replay and exc.kind == "refused"):
            raise
        exc.report()
    path.unlink()


def _start(hub, config, job, journal, env, secrets):
    if not (
        isinstance(job, dict) and isinstance(job.get("lease_token"), str) and job["lease_token"]
    ):
        raise _HubError("transient")
    code = _job_code(config, job)
    if code:
        # Invalid ids must never become local paths. The fence identifies the job.
        hub.post("result", {"lease_token": job["lease_token"], "outcome": "failed", "code": code})
        return
    for attempt in range(2):
        try:
            hub.post("starting", {"lease_token": job["lease_token"]})
        except _HubError as exc:
            if exc.kind == "fenced":
                exc.report()
                return
            if exc.kind != "transient" or attempt:
                raise
            exc.report()
            time.sleep(hub.delay())
        else:
            break
    path = journal / (job["job_id"] + ".json")
    # Never overwrite an earlier launch journal, including after an interrupted run.
    if path.exists():
        return
    entry = {"lease_token": job["lease_token"], "outcome": "spawning"}
    _save(path, entry)
    entry.update(_spawn(config, job, env, secrets))
    _save(path, entry)
    _deliver(hub, path, entry)


def _run(config, once):
    token, grant, env = _environment(config)
    journal = Path(config["journal_dir"]).expanduser()
    journal.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        journal.chmod(0o700)
    hub = _Hub(config, token)
    while True:
        try:
            for path in sorted(journal.glob("*.json")):
                try:
                    entry = json.loads(path.read_text(encoding="utf-8"))
                    if (
                        not isinstance(entry, dict)
                        or not isinstance(entry.get("lease_token"), str)
                        or not entry["lease_token"]
                        or entry.get("outcome")
                        not in ("spawning", "started", "uncertain", "failed")
                    ):
                        raise ValueError
                except (ValueError, UnicodeError, RecursionError):
                    path.rename(path.with_name(path.name + f".quarantine-{time.time_ns()}"))
                    print("runner: invalid journal quarantined", file=sys.stderr)
                    continue
                _deliver(hub, path, entry, replay=True)
            job = hub.post("lease", {})
            if job is not None:
                _start(hub, config, job, journal, env, (token, grant))
        except _HubError as exc:
            exc.report()
            if exc.kind == "credential":
                return 3
            if exc.kind != "fenced" and once:
                return 5
        else:
            if once:
                return 0
        if once:
            return 0
        time.sleep(hub.delay())


def _config_failure(exc):
    message = str(exc) if isinstance(exc, _ConfigError) else "invalid configuration or local files"
    print("runner: config_invalid: " + message, file=sys.stderr)
    return 4


def run(config_path, once) -> int:
    try:
        return _run(_load(config_path), once)
    except (OSError, ValueError, ClientError, TypeError, KeyError) as exc:
        # Never display raw local-file, transport, parser or subprocess diagnostics.
        return _config_failure(exc)


def check(config_path) -> int:
    from . import chat_agent

    try:
        config = _read(config_path)
        chat_only = isinstance(config, dict) and config.keys() == chat_agent.CONFIG_KEYS
        config = chat_agent.load(config_path) if chat_only else _load(config_path)
        if (
            not chat_only
            and os.environ.get(config["token_env"])
            and os.environ.get(config["t3_grant_env"])
        ):
            _environment(config)
        chat_lines = chat_agent.check_lines(config) if "chat" in config else []
    except (OSError, ValueError, ClientError, TypeError, KeyError) as exc:
        return _config_failure(exc)
    if not chat_only:
        print("launch shape uncaptured (D2 open); attestation is not verification")
        print("workspace strategy root (unverified, D2); started agents share the project checkout")
        for label in config["projects"]:
            print(f"project {label}: shared checkout acknowledged; one agent per project per hour")
    for line in chat_lines:
        print(line)
    return 0
