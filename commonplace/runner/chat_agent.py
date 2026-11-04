"""Opt-in chat answers through the owner's configured local CLI and login."""

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from ..chat.types import MAX_REPLY_CHARS
from ..client import ClientError
from .agent import (
    _CHILD_KEYS,
    _config_failure,
    _ConfigError,
    _Hub,
    _HubError,
    _load,
    _read,
    _require,
    _url,
)
from .registry import CHAT_MODEL, PROVIDER

CONFIG_KEYS = frozenset({"hub_url", "token_env", "chat"})
CHAT_KEYS = frozenset({"default_cli", "context", "relay_always", "clis"})
CLI_KEYS = frozenset({"argv", "default_model", "tools"})
POLL_S = 3
CLI_TIMEOUT_S = 60
CHAT_CHILD_KEYS = _CHILD_KEYS + ("XDG_CONFIG_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR")
SAFE_ARGV = {
    "claude": {
        "any_of": ("-p", "--print"),
        "all_of": ("--strict-mcp-config", "--no-session-persistence"),
        "empty_value": "--tools",
        "deny": (
            "--dangerously-skip-permissions",
            "--allow-dangerously-skip-permissions",
            "--allowedTools",
            "--allowed-tools",
            "--mcp-config",
            "--add-dir",
            "--settings",
            "--plugin-dir",
            "--agents",
            "--agent",
            "--permission-mode",
        ),
    }
}
EGRESS_LINE = "chat: answers run your configured CLI under your own login; each prompt (your message, the project name and up to 15 trusted card titles) goes to that CLI's provider"
EGRESS_FULL_LINE = "chat: context full: prompts also carry recent chat lines from everyone in the project and up to 15 visible card titles"
TOOLS_LINE = "chat: {label} runs with its tools on (tools: true); chat text can steer those tools, including reading local files and sending them to the CLI's provider"


def validate_chat(chat: dict) -> dict:
    _require(isinstance(chat, dict) and not chat.keys() - CHAT_KEYS, "invalid chat block")
    context = chat.get("context", "minimal")
    relay_always = chat.get("relay_always", False)
    _require(context in ("minimal", "full"), "invalid chat context")
    _require(isinstance(relay_always, bool), "invalid relay_always")
    clis = chat.get("clis")
    _require(isinstance(clis, dict) and 1 <= len(clis) <= 8, "invalid chat clis")
    validated = {}
    for label, entry in clis.items():
        _require(isinstance(label, str) and PROVIDER.fullmatch(label), "invalid CLI label")
        _require(isinstance(entry, dict) and not entry.keys() - CLI_KEYS, "invalid CLI entry")
        argv = entry.get("argv")
        _require(
            isinstance(argv, list) and argv and all(isinstance(arg, str) for arg in argv),
            "invalid CLI argv",
        )
        try:
            exists = Path(argv[0]).is_absolute() and Path(argv[0]).is_file()
        except (OSError, ValueError):
            exists = False
        _require(exists and all("\x00" not in arg for arg in argv), "invalid CLI executable")
        _require(
            all("{model}" not in arg or arg == "{model}" for arg in argv),
            "model placeholder must be a whole argument",
        )
        model = entry.get("default_model")
        _require(
            isinstance(model, str) and CHAT_MODEL.fullmatch(model),
            "invalid default_model",
        )
        tools = entry.get("tools", False)
        _require(isinstance(tools, bool), "invalid tools acknowledgement")
        if not tools:
            _safe_argv(argv)
        validated[label] = {"argv": list(argv), "default_model": model, "tools": tools}
    default = chat.get("default_cli")
    _require(isinstance(default, str) and default in validated, "invalid default_cli")
    return {
        "default_cli": default,
        "context": context,
        "relay_always": relay_always,
        "clis": validated,
    }


def _safe_argv(argv):
    rules = SAFE_ARGV.get(Path(argv[0]).name)
    _require(rules is not None, "CLI requires tools: true")
    args = argv[1:]
    flags = {arg.split("=", 1)[0] for arg in args}
    empty = rules["empty_value"]
    _require(
        any(flag in flags for flag in rules["any_of"])
        and all(flag in flags for flag in rules["all_of"])
        and not flags.intersection(rules["deny"])
        and "--" not in args,
        "CLI tools-off flags required",
    )
    # Every occurrence must disable tools; a later argument must not enable them again.
    values = [
        arg == empty + "=" or (arg == empty and i + 1 < len(args) and args[i + 1] == "")
        for i, arg in enumerate(args)
        if arg.split("=", 1)[0] == empty
    ]
    _require(values and all(values), "CLI tools-off flags required")


def _check_credentials(config):
    token = os.environ.get(config["token_env"], "")
    _require(
        not token
        or not any(
            token in arg for entry in config["chat"]["clis"].values() for arg in entry["argv"]
        ),
        "credential in CLI arguments",
    )


def load(config_path) -> dict:
    config = _read(config_path)
    _require(isinstance(config, dict) and "chat" in config, "chat configuration required")
    if config.keys() != CONFIG_KEYS:
        # The card client's validator owns the full schema; U6 accepts the chat key.
        config = _load(config_path)
    try:
        _url(config["hub_url"])
    except ClientError:
        raise _ConfigError("invalid hub URL") from None
    _require(
        isinstance(config["token_env"], str)
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config["token_env"]),
        "invalid credential environment name",
    )
    config["chat"] = validate_chat(config["chat"])
    _check_credentials(config)
    return config


def check_lines(config: dict) -> list[str]:
    _check_credentials(config)
    chat = config["chat"]
    lines = [EGRESS_LINE]
    if chat.get("context", "minimal") == "full":
        lines.append(EGRESS_FULL_LINE)
    lines.extend(
        TOOLS_LINE.format(label=label)
        for label, entry in sorted(chat["clis"].items())
        if entry.get("tools", False)
    )
    return lines


def run(config_path, once: bool) -> int:
    try:
        config = load(config_path)
        return _run(config, once)
    except (OSError, ValueError, ClientError, TypeError, KeyError, RecursionError) as exc:
        return _config_failure(exc)


def _run(config, once):
    token = os.environ.get(config["token_env"], "")
    if not token or "\n" in token or "\r" in token:
        print("runner: credential environment variable required", file=sys.stderr)
        return 3
    for line in check_lines(config):
        print(line, file=sys.stderr)
    chat = config["chat"]
    hub = _Hub({"hub_url": config["hub_url"], "poll_seconds": POLL_S}, token)
    while True:
        try:
            job = hub.post(
                "lease",
                {
                    "clis": sorted(chat["clis"]),
                    "context": chat["context"],
                    "relay_always": chat["relay_always"],
                },
                prefix="/api/runner/chat/",
            )
            if job is not None:
                _reply(hub, config, job)
        except _HubError as exc:
            exc.report()
            if exc.kind == "credential":
                return 3
            if once and exc.kind != "fenced":
                return 3 if exc.status is None else 5
        if once:
            return 0
        time.sleep(hub.delay())


def _post_result(hub, action, body, deadline):
    for attempt in range(3):
        try:
            return hub.post(action, body, prefix="/api/runner/chat/")
        except _HubError as exc:
            remaining = deadline - time.monotonic()
            if exc.status is None or exc.status < 500 or attempt == 2 or remaining <= POLL_S:
                raise
            time.sleep(POLL_S)
            if time.monotonic() >= deadline:
                raise


def _reply(hub, config, job):
    deadline = time.monotonic() + job.get("expires_in", 0) if isinstance(job, dict) else 0
    if not (
        isinstance(job, dict)
        and isinstance(job.get("job"), str)
        and re.fullmatch(r"chat_[0-9a-f]{16}", job["job"])
        and isinstance(job.get("lease_token"), str)
        and job["lease_token"]
    ):
        raise _HubError("transient")
    body = {key: job[key] for key in ("job", "lease_token")}
    chat = config["chat"]
    cli = job.get("cli")
    model = job.get("model")
    code = None
    if not (
        isinstance(job.get("system"), str)
        and isinstance(job.get("prompt"), str)
        and (cli is None or isinstance(cli, str))
        and (model is None or isinstance(model, str) and CHAT_MODEL.fullmatch(model))
    ):
        code = "bad_output"
    elif cli is not None and cli not in chat["clis"]:
        code = "cli_not_configured"
    if code:
        _post_result(hub, "fail", {**body, "code": code}, deadline)
        return
    cli = cli if cli is not None else chat["default_cli"]
    model = model if model is not None else chat["clis"][cli]["default_model"]
    # The frozen CLI seam takes the chat block, so carry the credential name in
    # an in-memory copy, outside the owner-written configuration schema.
    local_chat = {**chat, "_token_env": config["token_env"]}
    outcome, value = _ask_cli(local_chat, cli, model, job["system"], job["prompt"])
    if outcome == "ok":
        _post_result(
            hub,
            "answer",
            {**body, "text": value, "cli": cli, "model": model},
            deadline,
        )
    else:
        _post_result(hub, "fail", {**body, "code": value}, deadline)


def _ask_cli(chat: dict, cli: str, model: str | None, system: str, prompt: str) -> tuple[str, str]:
    entry = chat["clis"][cli]
    model = model or entry["default_model"]
    argv = [model if arg == "{model}" else arg for arg in entry["argv"]]
    token_env = chat.get("_token_env")
    token = os.environ.get(token_env, "") if token_env else ""
    env = {
        key: os.environ[key] for key in CHAT_CHILD_KEYS if key in os.environ and key != token_env
    }
    if token and any(token in value for value in [*argv, *env.values()]):
        return "fail", "cli_failed"
    try:
        with TemporaryDirectory() as cwd:
            data = (system + "\n\n" + prompt).encode("utf-8")
            child = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=cwd,
                env=env,
            )
            output = bytearray()

            def read_output():
                # This thread owns the pipe: closing it from another thread can
                # block while a descendant still holds the other end open.
                with child.stdout:
                    output.extend(child.stdout.read(65_537))
                    if len(output) > 65_536:
                        child.kill()
                        child.wait()

            def write_input():
                try:
                    with child.stdin:
                        child.stdin.write(data)
                except (BrokenPipeError, OSError):
                    pass

            reader = Thread(target=read_output, daemon=True)
            writer = Thread(target=write_input, daemon=True)
            deadline = time.monotonic() + CLI_TIMEOUT_S
            reader.start()
            writer.start()
            try:
                child.wait(timeout=CLI_TIMEOUT_S)
                reader.join(max(0, deadline - time.monotonic()))
                writer.join(max(0, deadline - time.monotonic()))
                if reader.is_alive() or writer.is_alive():
                    raise subprocess.TimeoutExpired(argv, CLI_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
                reader.join(0.1)
                writer.join(0.1)
                return "fail", "cli_timeout"
            if len(output) > 65_536:
                return "fail", "bad_output"
            returncode = child.returncode
    except OSError:
        return "fail", "cli_failed"
    except UnicodeError:
        return "fail", "bad_output"
    if returncode != 0:
        return "fail", "cli_failed"
    try:
        text = output.decode("utf-8").strip()
    except UnicodeError:
        return "fail", "bad_output"
    if not text:
        return "fail", "bad_output"
    return "ok", text[:MAX_REPLY_CHARS]
