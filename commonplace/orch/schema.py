"""Orchestrator setup as code: the ``commonplace.orchestrator/1`` TOML format.

This module is pure: it parses, validates, renders and diffs one config file
and applies the experiment rule. It reads no database and no workspace state;
the store, runner, checks and surfaces call it.

Public API (stable for the other orchestrator modules):

- ``parse(text, *, expected_sha256=None) -> Config``
- ``validate(data) -> Config`` (a dict already loaded from TOML)
- ``sha256_text(text) -> str`` and ``verify_sha256(text, expected) -> str``
- ``check_connections(config, known_names)`` (planner bindings vs workspace)
- ``template(name)``, ``expand_prompt(text)``, ``role_instructions(config, role)``
- ``render(config) -> str``
- ``diff(old, new) -> list[Change]`` and ``format_diff(changes) -> str``
- ``experiment_violations(live, exp) -> list[str]`` and ``check_experiment(live, exp)``

Every failure is a ``ConfigError`` with a stable ``code`` and a dotted
``path``. Messages are fixed text: no config value is ever placed in an
error, so an inline secret is refused without being echoed.
"""

from __future__ import annotations

import dataclasses
import difflib
import functools
import hashlib
import hmac
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from commonplace.redact import redact

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

SCHEMA_ID = "commonplace.orchestrator/1"
CONFIG_PATH = ".commonplace/orchestrator.toml"

KINDS = ("orchestrator", "sub-orchestrator", "planner", "builder", "reviewer")
HARNESSES = ("t3", "claude-code", "codex")
RUNNER_HARNESS = "t3"
# Same slots as commonplace.projects.types.MODEL_ROLES.
MODEL_ROLES = ("main", "worker", "reviewer", "gate")
RUNTIME_MODES = ("approval-required", "auto-accept-edits", "auto", "full-access")
TRIGGER_EVENTS = ("card.todo",)
TEMPLATES = ("design_before_code", "resume_protocol")
DESIGN_TEMPLATE = "design_before_code"

# Fields an experiment channel may change; everything else must equal live.
EXPERIMENT_ROLE_FIELDS = frozenset({"prompt", "workflow", "topic", "model_role", "skills"})
EXPERIMENT_TOP_LEVEL = frozenset({"workflows"})

MAX_CONFIG_BYTES = 128 * 1024
MAX_ROLES = 16
MAX_PROMPT_BYTES = 32 * 1024
MAX_LAUNCH_CHARS = 120_000  # runner thread launch message limit
SPAWN_RESERVE_CHARS = 1_024  # room kept for the runner's own SPAWN_MESSAGE
MAX_AGENTS_RANGE = (1, 20)  # same bounds as project_limits.max_agents
MAX_WORKFLOWS = 32
MAX_STEPS = 64
MAX_STEP_CHARS = 500
MAX_SKILLS = 32
MAX_MCPS = 16
MAX_CONNECTIONS = 16
MAX_TRIGGERS = 4

NAME = re.compile(r"[a-z0-9-]{1,32}")  # role, workflow and mcp names
PROJECT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")  # as commonplace.runner.registry.PROJECT
TOPIC = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")  # as registry.TOPIC
MACHINE_ID = re.compile(r"mach_[0-9a-f]{16}")  # as registry.MACHINE_ID
SECRET_REF = re.compile(r"env:[A-Z][A-Z0-9_]{1,63}")
SKILL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
CONNECTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,119}")  # as control_db
CONNECTION_ID = re.compile(r"conn_[0-9a-f]{16}")  # control_db._new_id("conn")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
INCLUDE = re.compile(r"\{\{include:([^{}]*)\}\}")

# Error codes (ConfigError.code). Callers map them; tests assert on them.
CODES = frozenset(
    {
        "config_too_large",  # > MAX_CONFIG_BYTES
        "not_utf8",
        "toml_invalid",
        "sha256_invalid",  # claimed sha256 is not 64 lowercase hex
        "sha256_mismatch",  # claimed sha256 differs from the text's
        "schema_unsupported",
        "unknown_key",
        "missing_key",
        "wrong_type",
        "invalid_value",  # pattern, range or enum failure
        "too_many",  # roles, workflows, steps, skills, mcps, connections, triggers
        "prompt_too_large",
        "launch_too_large",
        "unknown_template",
        "unknown_role",
        "unknown_workflow",
        "unknown_mcp",
        "parent_cycle",
        "area_duplicate",
        "area_kind",
        "area_parent",
        "area_main",
        "area_runner",
        "harness_not_allowed",
        "runner_harness_not_t3",
        "runner_role_incomplete",  # runner role lacks topic or model_role
        "limits_required",  # [runner] present without limits.max_agents
        "trigger_without_runner",
        "inline_secret",
        "planner_missing_design_before_code",
        "connections_not_planner",
        "connection_id_shaped",
        "connection_unknown",
        "experiment_not_allowed",
    }
)


class ConfigError(ValueError):
    """A config the orchestrator refuses. ``str()`` never contains a config value."""

    def __init__(self, code: str, path: str = "", message: str = ""):
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{code}: {path or '<config>'}: {message}" if message else code)


@dataclass(frozen=True)
class Skill:
    id: str
    version: int


@dataclass(frozen=True)
class Role:
    name: str
    kind: str
    parent: str | None = None
    harness: str | None = None
    topic: str | None = None
    model_role: str | None = None
    workflow: str | None = None
    prompt: str | None = None
    skills: tuple[Skill, ...] = ()
    mcps: tuple[str, ...] = ()
    connections: tuple[str, ...] = ()
    area: str | None = None


@dataclass(frozen=True)
class Workflow:
    name: str
    steps: tuple[str, ...]


@dataclass(frozen=True)
class Mcp:
    name: str
    url: str
    secret_ref: str | None = None


@dataclass(frozen=True)
class Runner:
    role: str
    machine: str
    project: str
    runtime_mode: str


@dataclass(frozen=True)
class Trigger:
    on: str


@dataclass(frozen=True)
class Config:
    """One validated config. ``sha256`` is set by ``parse`` (raw text), else ``None``."""

    schema: str
    project: str
    max_agents: int | None
    roles: Mapping[str, Role]
    workflows: Mapping[str, Workflow] = field(default_factory=dict)
    mcps: Mapping[str, Mcp] = field(default_factory=dict)
    runner: Runner | None = None
    triggers: tuple[Trigger, ...] = ()
    sha256: str | None = None

    @property
    def has_runner(self) -> bool:
        """True when the config has ``[runner]``: the board is "configured"."""
        return self.runner is not None

    @property
    def split_role(self) -> str | None:
        """The role a split start runs: ``main`` when the config has one, else the runner role."""
        if self.runner is None:
            return None
        return "main" if "main" in self.roles else self.runner.role

    @property
    def start_roles(self) -> tuple[str, ...]:
        """Roles of this config a start can run: the runner role and the split start role."""
        if self.runner is None:
            return ()
        names = dict.fromkeys((self.runner.role, self.split_role))
        return tuple(name for name in names if name in self.roles)

    @property
    def runner_role(self) -> Role | None:
        """The role the runner launches, or ``None`` without ``[runner]``."""
        return self.roles[self.runner.role] if self.runner else None

    @property
    def planner_connections(self) -> tuple[str, ...]:
        """Connection names of every planner role, in role order, deduplicated."""
        names = (c for r in self.roles.values() if r.kind == "planner" for c in r.connections)
        return tuple(dict.fromkeys(names))

    def to_dict(self) -> dict[str, Any]:
        """Canonical plain dict (no ``None`` fields, no sha256); input of ``diff``."""
        out: dict[str, Any] = {"schema": self.schema, "project": self.project}
        if self.max_agents is not None:
            out["limits"] = {"max_agents": self.max_agents}
        out["roles"] = {name: _role_dict(self.roles[name]) for name in sorted(self.roles)}
        if self.workflows:
            out["workflows"] = {
                name: {"steps": list(self.workflows[name].steps)} for name in sorted(self.workflows)
            }
        if self.mcps:
            out["mcps"] = {name: _mcp_dict(self.mcps[name]) for name in sorted(self.mcps)}
        if self.runner is not None:
            out["runner"] = dataclasses.asdict(self.runner)
        if self.triggers:
            out["triggers"] = [{"on": t.on} for t in self.triggers]
        return out


@dataclass(frozen=True)
class Change:
    """One diff entry. ``path`` is dotted (``roles.main.prompt``); op add/remove/change."""

    path: str
    op: str
    old: Any = None
    new: Any = None


TEMPLATE_DIR = Path(__file__).with_name("templates")

_TOP_KEYS = ("schema", "project", "limits", "roles", "workflows", "mcps", "runner", "triggers")
_ROLE_KEYS = (
    "kind",
    "area",
    "parent",
    "harness",
    "topic",
    "model_role",
    "workflow",
    "prompt",
    "skills",
    "mcps",
    "connections",
)
_RUNNER_KEYS = ("role", "machine", "project", "runtime_mode")
_SAFE_KEY = re.compile(r"[A-Za-z0-9_-]{1,40}")
_SECRET_KEY_NAME = re.compile(
    r"(?i)password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"credential|authorization|bearer|cookie"
)
_REDACT_WINDOW = 8_000
_REDACT_STEP = 7_000
_TOML_POSITION = re.compile(r"\(at (?:line \d+, column \d+|end of document)\)")
_MAX_URL_CHARS = 2_048


# ---------------------------------------------------------------- helpers


def _key_label(key: object) -> str:
    """A key as shown in a path; anything that is not a plain short name is hidden."""
    if isinstance(key, str) and _SAFE_KEY.fullmatch(key) and not redact(key)[1]:
        return key
    return "<key>"


def _join(path: str, key: object) -> str:
    label = _key_label(key)
    return f"{path}.{label}" if path else label


def _table(value: object, path: str, allowed: Iterable[str], required: Iterable[str] = ()) -> dict:
    if not isinstance(value, dict):
        raise ConfigError("wrong_type", path, "must be a table")
    allowed = tuple(allowed)
    for key in value:
        if key not in allowed:
            raise ConfigError("unknown_key", _join(path, key), "unknown key")
    for key in required:
        if key not in value:
            raise ConfigError("missing_key", _join(path, key), "required key is missing")
    return value


def _str(value: object, path: str, pattern: re.Pattern | None = None, choices=()) -> str:
    if not isinstance(value, str):
        raise ConfigError("wrong_type", path, "must be a string")
    if pattern is not None and not pattern.fullmatch(value):
        raise ConfigError("invalid_value", path, "does not match the allowed pattern")
    if choices and value not in choices:
        raise ConfigError("invalid_value", path, "must be one of: " + ", ".join(choices))
    return value


def _int(value: object, path: str, low: int, high: int) -> int:
    if type(value) is not int:
        raise ConfigError("wrong_type", path, "must be an integer")
    if not low <= value <= high:
        raise ConfigError("invalid_value", path, f"must be between {low} and {high}")
    return value


def _list(value: object, path: str, limit: int) -> list:
    if not isinstance(value, list):
        raise ConfigError("wrong_type", path, "must be an array")
    if len(value) > limit:
        raise ConfigError("too_many", path, f"at most {limit} entries")
    return value


def _names(table: dict, path: str, limit: int) -> None:
    if len(table) > limit:
        raise ConfigError("too_many", path, f"at most {limit} entries")
    for name in table:
        if not NAME.fullmatch(name):
            raise ConfigError("invalid_value", _join(path, name), "name must match [a-z0-9-]{1,32}")


def _looks_secret(text: str) -> bool:
    """True when ``commonplace.redact`` would redact any part of ``text``."""
    for line in text.splitlines() or [text]:
        if len(line) <= _REDACT_WINDOW:
            if redact(line)[1]:
                return True
            continue
        for start in range(0, len(line), _REDACT_STEP):
            if redact(line[start : start + _REDACT_WINDOW])[1]:
                return True
    return False


def _scan_secrets(value: object, path: str) -> None:
    """Refuse inline secrets anywhere in the document, without echoing them."""
    if isinstance(value, dict):
        for key, item in value.items():
            here = _join(path, key)
            if isinstance(key, str) and _looks_secret(key):
                raise ConfigError("inline_secret", here, "key looks like a credential")
            if (
                isinstance(key, str)
                and key != "secret_ref"
                and _SECRET_KEY_NAME.search(key)
                and not isinstance(item, dict)
            ):
                raise ConfigError(
                    "inline_secret", here, "inline credential; use secret_ref = 'env:NAME'"
                )
            _scan_secrets(item, here)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_secrets(item, f"{path}[{index}]")
    elif isinstance(value, str) and _looks_secret(value):
        raise ConfigError(
            "inline_secret", path, "value looks like a credential; use secret_ref = 'env:NAME'"
        )


# ---------------------------------------------------------------- sections


def _workflows(value: object) -> dict[str, Workflow]:
    table = _table(value, "workflows", value if isinstance(value, dict) else ())
    _names(table, "workflows", MAX_WORKFLOWS)
    out = {}
    for name, body in table.items():
        path = f"workflows.{name}"
        body = _table(body, path, ("steps",), ("steps",))
        steps = _list(body["steps"], f"{path}.steps", MAX_STEPS)
        if not steps:
            raise ConfigError("invalid_value", f"{path}.steps", "needs at least one step")
        for index, step in enumerate(steps):
            step_path = f"{path}.steps[{index}]"
            _str(step, step_path)
            if not step.strip() or len(step) > MAX_STEP_CHARS:
                raise ConfigError(
                    "invalid_value", step_path, f"step must be 1-{MAX_STEP_CHARS} characters"
                )
        out[name] = Workflow(name, tuple(steps))
    return out


def _url(value: object, path: str) -> str:
    url = _str(value, path)
    try:
        parts = urlsplit(url)
        credentials = parts.username is not None or parts.password is not None
    except ValueError:
        raise ConfigError("invalid_value", path, "not a valid URL") from None
    if credentials:
        raise ConfigError("inline_secret", path, "URL carries credentials; use secret_ref")
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or len(url) > _MAX_URL_CHARS
        or any(ch.isspace() for ch in url)
    ):
        raise ConfigError("invalid_value", path, "must be an http(s) URL")
    return url


def _mcps(value: object) -> dict[str, Mcp]:
    table = _table(value, "mcps", value if isinstance(value, dict) else ())
    _names(table, "mcps", MAX_MCPS)
    out = {}
    for name, body in table.items():
        path = f"mcps.{name}"
        body = _table(body, path, ("url", "auth"), ("url",))
        url = _url(body["url"], f"{path}.url")
        ref = None
        if "auth" in body:
            auth = _table(body["auth"], f"{path}.auth", ("secret_ref",), ("secret_ref",))
            ref_path = f"{path}.auth.secret_ref"
            if not isinstance(auth["secret_ref"], str) or not SECRET_REF.fullmatch(
                auth["secret_ref"]
            ):
                raise ConfigError(
                    "inline_secret", ref_path, "secret_ref must be env:NAME ([A-Z][A-Z0-9_]{1,63})"
                )
            ref = auth["secret_ref"]
        out[name] = Mcp(name, url, ref)
    return out


def _prompt(value: object, path: str) -> str:
    prompt = _str(value, path)
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ConfigError("prompt_too_large", path, f"at most {MAX_PROMPT_BYTES} bytes")
    for match in INCLUDE.finditer(prompt):
        if match.group(1) not in TEMPLATES:
            raise ConfigError("unknown_template", path, "include names an unknown template")
    return prompt


def _skills(value: object, path: str) -> tuple[Skill, ...]:
    out = []
    for index, item in enumerate(_list(value, path, MAX_SKILLS)):
        here = f"{path}[{index}]"
        item = _table(item, here, ("id", "version"), ("id", "version"))
        out.append(
            Skill(
                _str(item["id"], f"{here}.id", SKILL_ID),
                _int(item["version"], f"{here}.version", 1, 2**63 - 1),
            )
        )
    return tuple(dict.fromkeys(out))


def _connections(value: object, path: str) -> tuple[str, ...]:
    out = []
    for index, item in enumerate(_list(value, path, MAX_CONNECTIONS)):
        here = f"{path}[{index}]"
        _str(item, here)
        if CONNECTION_ID.fullmatch(item):
            raise ConfigError("connection_id_shaped", here, "use the connection name, not its id")
        _str(item, here, CONNECTION_NAME)
        out.append(item)
    return tuple(dict.fromkeys(out))


def _role(name: str, value: object, workflows: Mapping, mcps: Mapping) -> Role:
    path = f"roles.{name}"
    body = _table(value, path, _ROLE_KEYS, ("kind",))
    kind = _str(body["kind"], f"{path}.kind", choices=KINDS)
    prompt = _prompt(body["prompt"], f"{path}.prompt") if "prompt" in body else None
    if kind == "planner" and (
        prompt is None or DESIGN_TEMPLATE not in (m.group(1) for m in INCLUDE.finditer(prompt))
    ):
        raise ConfigError(
            "planner_missing_design_before_code",
            f"{path}.prompt",
            "a planner prompt must contain {{include:design_before_code}}",
        )
    workflow = None
    if "workflow" in body:
        workflow = _str(body["workflow"], f"{path}.workflow", NAME)
        if workflow not in workflows:
            raise ConfigError("unknown_workflow", f"{path}.workflow", "no such [workflows] entry")
    role_mcps = tuple(
        _str(m, f"{path}.mcps", NAME) for m in _list(body.get("mcps", []), f"{path}.mcps", MAX_MCPS)
    )
    if any(m not in mcps for m in role_mcps):
        raise ConfigError("unknown_mcp", f"{path}.mcps", "no such [mcps] entry")
    connections = ()
    if "connections" in body:
        if kind != "planner":
            raise ConfigError(
                "connections_not_planner",
                f"{path}.connections",
                "only planner roles bind connections",
            )
        connections = _connections(body["connections"], f"{path}.connections")
    harness = None
    if "harness" in body:
        harness = _str(body["harness"], f"{path}.harness")
        if harness not in HARNESSES:
            raise ConfigError(
                "harness_not_allowed", f"{path}.harness", "must be one of: " + ", ".join(HARNESSES)
            )
    area = None
    if "area" in body:
        area = _str(body["area"], f"{path}.area", NAME)
        if kind != "sub-orchestrator":
            raise ConfigError("area_kind", f"{path}.kind", "an area needs a sub-orchestrator")
        for required in ("parent", "topic"):
            if required not in body:
                raise ConfigError("missing_key", f"{path}.{required}", "required for an area")
    return Role(
        area=area,
        name=name,
        kind=kind,
        parent=_str(body["parent"], f"{path}.parent", NAME) if "parent" in body else None,
        harness=harness,
        topic=_str(body["topic"], f"{path}.topic", TOPIC) if "topic" in body else None,
        model_role=(
            _str(body["model_role"], f"{path}.model_role", choices=MODEL_ROLES)
            if "model_role" in body
            else None
        ),
        workflow=workflow,
        prompt=prompt,
        skills=_skills(body["skills"], f"{path}.skills") if "skills" in body else (),
        mcps=tuple(dict.fromkeys(role_mcps)),
        connections=connections,
    )


def _check_parents(roles: Mapping[str, Role]) -> None:
    for role in roles.values():
        if role.parent is not None and role.parent not in roles:
            raise ConfigError("unknown_role", f"roles.{role.name}.parent", "no such role")
    for role in roles.values():
        seen = {role.name}
        current = role
        while current.parent is not None:
            if current.parent in seen:
                raise ConfigError(
                    "parent_cycle",
                    f"roles.{role.name}.parent",
                    "parents form a cycle",
                )
            seen.add(current.parent)
            current = roles[current.parent]


def _check_areas(roles: Mapping[str, Role]) -> None:
    areas = [role for role in roles.values() if role.area is not None]
    if not areas:
        return
    seen = set()
    for role in areas:
        if role.area in seen:
            raise ConfigError(
                "area_duplicate", f"roles.{role.name}.area", "area is already declared"
            )
        seen.add(role.area)
    if len(areas) > 8:
        raise ConfigError("too_many", "roles", "at most 8 areas")
    mains = [role for role in roles.values() if role.kind == "orchestrator"]
    if len(mains) != 1 or mains[0].parent is not None:
        raise ConfigError("area_main", "roles", "areas need exactly one parentless orchestrator")
    for role in areas:
        if role.parent != mains[0].name:
            raise ConfigError(
                "area_parent", f"roles.{role.name}.parent", "area must be directly under main"
            )


def _roles(value: object, workflows: Mapping, mcps: Mapping) -> dict[str, Role]:
    table = _table(value, "roles", value if isinstance(value, dict) else ())
    if not table:
        raise ConfigError("invalid_value", "roles", "at least one role is required")
    _names(table, "roles", MAX_ROLES)
    roles = {name: _role(name, body, workflows, mcps) for name, body in table.items()}
    _check_parents(roles)
    _check_areas(roles)
    return roles


def _runner(value: object, roles: Mapping[str, Role]) -> Runner:
    body = _table(value, "runner", _RUNNER_KEYS, _RUNNER_KEYS)
    runner = Runner(
        role=_str(body["role"], "runner.role", NAME),
        machine=_str(body["machine"], "runner.machine", MACHINE_ID),
        project=_str(body["project"], "runner.project", PROJECT),
        runtime_mode=_str(body["runtime_mode"], "runner.runtime_mode", choices=RUNTIME_MODES),
    )
    role = roles.get(runner.role)
    if role is None:
        raise ConfigError("unknown_role", "runner.role", "no such role")
    if role.harness != RUNNER_HARNESS:
        raise ConfigError(
            "runner_harness_not_t3",
            f"roles.{role.name}.harness",
            "the runner role needs harness = 't3'",
        )
    if role.topic is None or role.model_role is None:
        raise ConfigError(
            "runner_role_incomplete",
            f"roles.{role.name}",
            "the runner role needs topic and model_role",
        )
    if role.area is not None:
        raise ConfigError("area_runner", "runner.role", "an area cannot be the runner")
    return runner


def _limits(value: object, runner: Runner | None) -> int | None:
    max_agents = None
    if value is not None:
        body = _table(value, "limits", ("max_agents",))
        if "max_agents" in body:
            max_agents = _int(body["max_agents"], "limits.max_agents", *MAX_AGENTS_RANGE)
    if runner is not None and max_agents is None:
        raise ConfigError("limits_required", "limits.max_agents", "required with [runner]")
    return max_agents


def _triggers(value: object, runner: Runner | None) -> tuple[Trigger, ...]:
    out = []
    for index, item in enumerate(_list(value, "triggers", MAX_TRIGGERS)):
        here = f"triggers[{index}]"
        item = _table(item, here, ("on",), ("on",))
        out.append(Trigger(_str(item["on"], f"{here}.on", choices=TRIGGER_EVENTS)))
    if out and runner is None:
        raise ConfigError("trigger_without_runner", "triggers", "triggers need [runner]")
    return tuple(dict.fromkeys(out))


# ---------------------------------------------------------------- public API


def sha256_text(text: str | bytes) -> str:
    """Lowercase hex sha256 of the UTF-8 bytes the owner approves."""
    raw = text if isinstance(text, bytes) else _encode(text)
    return hashlib.sha256(raw).hexdigest()


def _encode(text: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise ConfigError("not_utf8", "", "config text is not valid UTF-8") from None


def verify_sha256(text: str | bytes, expected: str) -> str:
    """Return the text's sha256; raise ``sha256_invalid`` / ``sha256_mismatch``."""
    if not isinstance(expected, str) or not SHA256_HEX.fullmatch(expected):
        raise ConfigError("sha256_invalid", "sha256", "must be 64 lowercase hex characters")
    actual = sha256_text(text)
    if not hmac.compare_digest(actual, expected):
        raise ConfigError(
            "sha256_mismatch", "sha256", "the text does not match the approved sha256"
        )
    return actual


def parse(text: str | bytes, *, expected_sha256: str | None = None) -> Config:
    """Size check, optional sha256 check (before parsing), TOML load, ``validate``."""
    raw = bytes(text) if isinstance(text, (bytes, bytearray)) else _encode(text)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError("config_too_large", "", f"at most {MAX_CONFIG_BYTES} bytes")
    sha = verify_sha256(raw, expected_sha256) if expected_sha256 is not None else sha256_text(raw)
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError("not_utf8", "", "config text is not valid UTF-8") from None
    try:
        data = tomllib.loads(decoded)
    except tomllib.TOMLDecodeError as exc:
        where = _TOML_POSITION.search(str(exc))
        message = "TOML syntax error " + (where.group(0) if where else "")
        raise ConfigError("toml_invalid", "", message.strip()) from None
    return dataclasses.replace(validate(data), sha256=sha)


def validate(data: Mapping[str, Any]) -> Config:
    """Validate a loaded TOML document and return the typed ``Config``."""
    if not isinstance(data, Mapping):
        raise ConfigError("wrong_type", "", "config must be a table")
    data = dict(data)
    _scan_secrets(data, "")
    doc = _table(data, "", _TOP_KEYS, ("schema", "project", "roles"))
    if doc["schema"] != SCHEMA_ID:
        raise ConfigError("schema_unsupported", "schema", f"must be {SCHEMA_ID!r}")
    project = _str(doc["project"], "project", PROJECT)
    workflows = _workflows(doc.get("workflows", {}))
    mcps = _mcps(doc.get("mcps", {}))
    roles = _roles(doc["roles"], workflows, mcps)
    runner = _runner(doc["runner"], roles) if "runner" in doc else None
    config = Config(
        schema=SCHEMA_ID,
        project=project,
        max_agents=_limits(doc.get("limits"), runner),
        roles=roles,
        workflows=workflows,
        mcps=mcps,
        runner=runner,
        triggers=_triggers(doc.get("triggers", []), runner),
    )
    if runner is not None:
        limit = MAX_LAUNCH_CHARS - SPAWN_RESERVE_CHARS
        if len(role_instructions(config, runner.role)) > limit:
            raise ConfigError(
                "launch_too_large", f"roles.{runner.role}", f"launch text over {limit} characters"
            )
    return config


def check_connections(config: Config, known_names: Iterable[str]) -> None:
    """Raise ``connection_unknown`` when a planner connection is not in ``known_names``."""
    known = set(known_names)
    for role in config.roles.values():
        for index, name in enumerate(role.connections):
            if name not in known:
                raise ConfigError(
                    "connection_unknown",
                    f"roles.{role.name}.connections[{index}]",
                    "not a connection name in this workspace",
                )


@functools.lru_cache(maxsize=None)
def template(name: str) -> str:
    """Text of a shipped template (``TEMPLATES``); ``unknown_template`` otherwise."""
    if name not in TEMPLATES:
        raise ConfigError("unknown_template", "", "no such template")
    return (TEMPLATE_DIR / f"{name}.md").read_text(encoding="utf-8")


def expand_prompt(text: str) -> str:
    """Replace every ``{{include:<name>}}`` with its template text (one pass)."""
    return INCLUDE.sub(lambda m: template(m.group(1)).strip(), text)


def role_instructions(config: Config, role: str) -> str:
    """Expanded prompt plus numbered workflow steps; the runner prepends SPAWN_MESSAGE."""
    item = config.roles.get(role)
    if item is None:
        raise ConfigError("unknown_role", "roles", "no such role")
    parts = []
    if item.prompt:
        parts.append(expand_prompt(item.prompt).strip())
    if item.workflow is not None:
        flow = config.workflows[item.workflow]
        steps = "\n".join(f"{n}. {step}" for n, step in enumerate(flow.steps, 1))
        parts.append(f"Workflow {flow.name}:\n{steps}")
    return "\n\n".join(p for p in parts if p)


def _role_dict(role: Role) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": role.kind}
    for key in ("parent", "harness", "topic", "model_role", "workflow", "prompt", "area"):
        value = getattr(role, key)
        if value is not None:
            out[key] = value
    if role.skills:
        out["skills"] = [{"id": s.id, "version": s.version} for s in role.skills]
    if role.mcps:
        out["mcps"] = list(role.mcps)
    if role.connections:
        out["connections"] = list(role.connections)
    return out


def _mcp_dict(mcp: Mcp) -> dict[str, Any]:
    out: dict[str, Any] = {"url": mcp.url}
    if mcp.secret_ref is not None:
        out["auth"] = {"secret_ref": mcp.secret_ref}
    return out


def _role_lines(role: Role) -> list[str]:
    facts = [
        f"{key}: `{value}`"
        for key in ("parent", "harness", "topic", "model_role", "workflow", "area")
        if (value := getattr(role, key)) is not None
    ]
    lines = [f"- {', '.join(facts)}"] if facts else []
    if role.skills:
        lines.append("- skills: " + ", ".join(f"`{s.id}@{s.version}`" for s in role.skills))
    if role.mcps:
        lines.append("- mcps: " + ", ".join(f"`{m}`" for m in role.mcps))
    if role.connections:
        lines.append("- connections: " + ", ".join(f"`{c}`" for c in role.connections))
    return lines


def render(config: Config) -> str:
    """Markdown bundle of the whole config for advisory sessions and ``orch render``."""
    lines = [f"# Orchestrator config `{config.project}`", "", f"- schema: `{config.schema}`"]
    if config.sha256:
        lines.append(f"- sha256: `{config.sha256}`")
    if config.max_agents is not None:
        lines.append(f"- max_agents: {config.max_agents}")
    if config.runner is not None:
        r = config.runner
        lines.append(
            f"- runner: role `{r.role}` on `{r.machine}`, project `{r.project}`, "
            f"runtime_mode `{r.runtime_mode}`"
        )
    if config.triggers:
        lines.append("- triggers: " + ", ".join(f"`{t.on}`" for t in config.triggers))
    for role in config.roles.values():
        lines += ["", f"## Role `{role.name}` ({role.kind})", "", *_role_lines(role)]
        text = role_instructions(config, role.name)
        if text:
            lines += ["", text]
    if config.mcps:
        lines += ["", "## MCP servers", ""]
        for mcp in config.mcps.values():
            auth = f" (auth: `{mcp.secret_ref}`)" if mcp.secret_ref else ""
            lines.append(f"- `{mcp.name}`: {mcp.url}{auth}")
    return "\n".join(lines) + "\n"


def _diff_into(old: Any, new: Any, path: str, out: list[Change]) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            here = f"{path}.{key}" if path else key
            if key not in old:
                out.append(Change(here, "add", None, new[key]))
            elif key not in new:
                out.append(Change(here, "remove", old[key], None))
            else:
                _diff_into(old[key], new[key], here, out)
    elif old != new:
        out.append(Change(path, "change", old, new))


def diff(old: Config, new: Config) -> list[Change]:
    """Structural diff of ``to_dict()`` forms, sorted by path; lists compare whole."""
    out: list[Change] = []
    _diff_into(old.to_dict(), new.to_dict(), "", out)
    return out


def _show(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def format_diff(changes: Iterable[Change]) -> str:
    """Human-readable diff (shown on accept); multi-line strings as unified diffs."""
    lines = []
    for change in changes:
        if change.op == "add":
            lines.append(f"+ {change.path} = {_show(change.new)}")
        elif change.op == "remove":
            lines.append(f"- {change.path} = {_show(change.old)}")
        elif (
            isinstance(change.old, str)
            and isinstance(change.new, str)
            and ("\n" in change.old or "\n" in change.new)
        ):
            lines.append(f"~ {change.path}")
            body = difflib.unified_diff(
                change.old.splitlines(), change.new.splitlines(), lineterm="", n=2
            )
            lines += [f"    {line}" for line in list(body)[2:]]
        else:
            lines.append(f"~ {change.path}: {_show(change.old)} -> {_show(change.new)}")
    return "\n".join(lines) + "\n" if lines else "no changes\n"


def _experiment_allowed(path: str) -> bool:
    parts = path.split(".")
    if parts[0] in EXPERIMENT_TOP_LEVEL:
        return True
    return parts[0] == "roles" and len(parts) == 3 and parts[2] in EXPERIMENT_ROLE_FIELDS


def experiment_violations(live: Config, exp: Config) -> list[str]:
    """Paths an experiment changes outside the allow-list; empty when allowed."""
    return [c.path for c in diff(live, exp) if not _experiment_allowed(c.path)]


def check_experiment(live: Config, exp: Config) -> None:
    """Raise ``experiment_not_allowed`` (path = first violation) unless allowed."""
    violations = experiment_violations(live, exp)
    if violations:
        raise ConfigError(
            "experiment_not_allowed",
            violations[0],
            "an experiment may change only prompt, workflow(s), topic, model_role and skills",
        )
