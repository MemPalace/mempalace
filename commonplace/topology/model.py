"""The typed topology model: node kinds, link methods, site routes and the secret filter.

Everything a writer may store is named here. A value outside these allow-lists is
refused, and so is any text that looks like a key, token, password or private key:
the store holds aliases, hostnames, runner ids and wings, never credentials.
The contract for callers is in ``docs/topology.md``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from commonplace import privacy, redact

TOPOLOGY_WING = "topology"

NODE_KINDS = ("pc", "mac", "phone", "vm", "server", "router", "cloud", "website", "container")
LINK_METHODS = (
    "direct",
    "jump_host",
    "tailscale",
    "relay",
    "dns",
    "proxy",
    "reverse_proxy",
    "hosts",
)
LINK_STATUSES = ("up", "degraded", "down", "unknown")
OBSERVER_KINDS = ("agent", "runner", "owner")
RESOURCE_KEYS = ("cpu", "cores", "memory_gb", "storage_gb", "gpu")

# Fact states. ``current`` is the value reads return; the others are history.
FACT_STATES = ("current", "contender", "superseded", "rejected", "withdrawn", "retracted")
CONFLICT_STATES = ("open", "accepted", "rejected", "withdrawn", "replaced")
WRITE_ACTIONS = ("added", "refreshed", "superseded", "conflict")

DEFAULT_TTL_SECONDS = 30 * 24 * 3600
STATUS_TTL_SECONDS = 15 * 60
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 365 * 24 * 3600

MAX_TEXT = 200
MAX_EVIDENCE = 4000
MAX_LIST = 32
MAX_HOPS = 16

KEY = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?")
HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
RUNNER_ID = re.compile(r"mach_[0-9a-f]{16}")
WING = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
OBSERVER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}")
_SPOKEN_SECRET = re.compile(
    r"\b(?:pass(?:word|wd|phrase)|secret|token|api[ _-]?key|private[ _-]?key)\s*(?:is|=|:)\s*\S",
    re.IGNORECASE,
)

SECRET_RULE = (
    "topology stores aliases, hostnames, runner ids and wings, "
    "never keys, tokens, passwords or private key material"
)


class TopologyError(ValueError):
    """A refused topology write or an invalid read argument."""


class SecretRefused(TopologyError):
    """The write held secret-looking material; nothing was stored."""


@dataclass(frozen=True)
class Observer:
    """Who observed a fact: an agent connection, a runner machine or the owner."""

    kind: str
    id: str

    def __post_init__(self):
        if self.kind not in OBSERVER_KINDS:
            raise TopologyError(f"no: observer kind must be one of {', '.join(OBSERVER_KINDS)}")
        if not isinstance(self.id, str) or not OBSERVER_ID.fullmatch(self.id):
            raise TopologyError("no: observer id must be a connection, machine or user id")
        check_secret("observer id", self.id)


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def instant(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def clock(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise TopologyError("no: now must be a timezone-aware datetime")
    return now.astimezone(timezone.utc).replace(microsecond=0)


def expiry(predicate: str, now: datetime, ttl_seconds: int | None = None) -> datetime:
    """When an observation made at ``now`` stops counting as fresh."""
    if ttl_seconds is None:
        ttl_seconds = STATUS_TTL_SECONDS if predicate == "status" else DEFAULT_TTL_SECONDS
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise TopologyError("no: ttl_seconds must be a whole number of seconds")
    if not MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS:
        raise TopologyError(
            f"no: ttl_seconds must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS}"
        )
    return now + timedelta(seconds=ttl_seconds)


# ── Secret filter ─────────────────────────────────────────────────────────
def secret_kinds(text: str) -> list[str]:
    """Names of the secret shapes found in ``text``; never the text itself."""
    kinds = [flag.split(":", 1)[1] for flag in privacy.scan(text) if flag.startswith("secret:")]
    kinds += [kind for kind in redact.redact(text)[1] if kind not in kinds]
    if _SPOKEN_SECRET.search(text) and not kinds:
        kinds.append("named_secret")
    return kinds


def check_secret(field: str, text: str) -> None:
    """Refuse secret-looking text. The message names the field, never the value."""
    kinds = secret_kinds(text)
    if kinds:
        raise SecretRefused(
            f"no: {field} looks like it holds a secret ({', '.join(kinds)}); {SECRET_RULE}"
        )


def check_field_name(field: str, allowed) -> None:
    if not isinstance(field, str) or field not in allowed:
        if isinstance(field, str) and redact._SECRET_KEY.fullmatch(field):
            raise SecretRefused(f"no: a field named like a secret is refused; {SECRET_RULE}")
        raise TopologyError(f"no: unknown field; allowed fields are {', '.join(allowed)}")


# ── Field validators ──────────────────────────────────────────────────────
def node_key(value, field="node key") -> str:
    if not isinstance(value, str) or not KEY.fullmatch(value):
        raise TopologyError(
            f"no: {field} must be lowercase letters, digits, dots, dashes or underscores"
        )
    check_secret(field, value)
    return value


def _text(field, value, limit=MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise TopologyError(f"no: {field} must be text of 1 to {limit} characters")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise TopologyError(f"no: {field} must not hold control characters")
    check_secret(field, value)
    return value


def _line(field, value) -> str:
    value = _text(field, value)
    if "\n" in value or "\t" in value:
        raise TopologyError(f"no: {field} must be one line")
    return value


def _pattern(pattern, what):
    def check(field, value):
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise TopologyError(f"no: {field} must be {what}")
        check_secret(field, value)
        return value

    return check


def _choice(options):
    def check(field, value):
        if not isinstance(value, str) or value not in options:
            raise TopologyError(f"no: {field} must be one of {', '.join(options)}")
        return value

    return check


def _listed(item):
    def check(field, value):
        if not isinstance(value, (list, tuple)) or len(value) > MAX_LIST:
            raise TopologyError(f"no: {field} must be a list of at most {MAX_LIST} entries")
        return sorted({item(field, entry) for entry in value})

    return check


def _port(field, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise TopologyError(f"no: {field} must be a port number from 1 to 65535")
    return value


def _resources(field, value) -> dict:
    if not isinstance(value, dict):
        raise TopologyError(f"no: {field} must be an object")
    clean = {}
    for key, entry in value.items():
        check_field_name(key, RESOURCE_KEYS)
        if key in ("cores", "memory_gb", "storage_gb"):
            if isinstance(entry, bool) or not isinstance(entry, (int, float)) or entry < 0:
                raise TopologyError(f"no: {field}.{key} must be a number of 0 or more")
            clean[key] = entry
        else:
            clean[key] = _line(f"{field}.{key}", entry)
    return clean


_hostname = _pattern(HOSTNAME, "a hostname or address")
_alias = _pattern(ALIAS, "an alias of letters, digits, dots, dashes or underscores")

# The allow-lists: field name -> validator. A node's first write needs ``kind``.
NODE_FIELDS = {
    "kind": _choice(NODE_KINDS),
    "name": _line,
    "role": _line,
    "hostname": _hostname,
    "aliases": _listed(_alias),
    "tailscale_name": _hostname,
    "runner_id": _pattern(RUNNER_ID, "a runner machine id"),
    "wings": _listed(_pattern(WING, "a wing name")),
    "hardware": _line,
    "os": _line,
    "resources": _resources,
}
# ``method`` is the link's identity and always equals the method in its id.
LINK_FIELDS = {
    "method": _choice(LINK_METHODS),
    "status": _choice(LINK_STATUSES),
    "via": node_key,
    "alias": _alias,
    "port": _port,
}
ROUTE_FIELDS = ("hops",)


def node_fields(fields) -> dict:
    return _fields(fields, NODE_FIELDS, "node")


def link_fields(fields) -> dict:
    return _fields(fields, LINK_FIELDS, "link")


def _fields(fields, allowed, what) -> dict:
    if not isinstance(fields, dict) or not fields:
        raise TopologyError(f"no: a {what} write needs at least one field")
    clean = {}
    for name, value in fields.items():
        check_field_name(name, tuple(allowed))
        if allowed[name] is node_key:
            clean[name] = node_key(value, f"{what} field {name}")
        else:
            clean[name] = allowed[name](f"{what} field {name}", value)
    return clean


def evidence_text(value) -> str:
    """Evidence is kept verbatim, so it is refused rather than cleaned."""
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_EVIDENCE:
        raise TopologyError(f"no: evidence must be text of 1 to {MAX_EVIDENCE} characters")
    check_secret("evidence", value)
    return value


# ── Subjects ──────────────────────────────────────────────────────────────
def node_subject(key) -> str:
    return "node:" + node_key(key)


def link_id(source, target, method) -> str:
    """``<from>><to>:<method>``; two machines may be linked by several methods."""
    source, target = node_key(source, "link from"), node_key(target, "link to")
    if source == target:
        raise TopologyError("no: a link needs two different nodes")
    return f"{source}>{target}:{_choice(LINK_METHODS)('link method', method)}"


def parse_link_id(value) -> tuple[str, str, str]:
    if not isinstance(value, str) or value.count(">") != 1 or value.count(":") != 1:
        raise TopologyError("no: a link id looks like <from>><to>:<method>")
    ends, method = value.split(":")
    source, target = ends.split(">")
    link_id(source, target, method)
    return source, target, method


def link_subject(source, target, method) -> str:
    return "link:" + link_id(source, target, method)


def route_subject(site) -> str:
    return "route:" + node_key(site, "site")


def route_hops(site, hops) -> list[str]:
    """An ordered chain of link ids that starts at the site and never breaks."""
    site = node_key(site, "site")
    if not isinstance(hops, (list, tuple)) or not 1 <= len(hops) <= MAX_HOPS:
        raise TopologyError(f"no: a route needs 1 to {MAX_HOPS} hops")
    chain, at = [], site
    for hop in hops:
        source, target, method = parse_link_id(hop)
        if source != at:
            raise TopologyError(
                "no: route hops must form one chain: each hop starts where the last ended, "
                "and the first starts at the site"
            )
        chain.append(link_id(source, target, method))
        at = target
    return chain


def encode(value) -> str:
    """Canonical JSON, so equal values compare equal as stored text."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def decode(text: str):
    return json.loads(text)


# ── Writes as facts ───────────────────────────────────────────────────────
def facts_for(items) -> list[tuple[str, str, object]]:
    """Validate write items and flatten them to ``(subject, predicate, value)`` facts.

    An item is one of ``{"node": key, "fields": {...}}``,
    ``{"link": [from, to, method], "fields": {...}}`` (fields optional) or
    ``{"route": site, "hops": [link ids]}``. Raises before anything is stored, so a
    proposal can be checked with the same rules as a write.
    """
    if not isinstance(items, (list, tuple)) or not items:
        raise TopologyError("no: a topology write needs at least one item")
    facts = []
    for item in items:
        if not isinstance(item, dict):
            raise TopologyError("no: each item must be an object")
        named = [name for name in ("node", "link", "route") if name in item]
        if len(named) != 1:
            raise TopologyError("no: each item names exactly one of node, link or route")
        check_field_name_set(item, {"node": ("node", "fields"), "link": ("link", "fields")})
        if named == ["node"]:
            subject = node_subject(item["node"])
            values = node_fields(item.get("fields"))
        elif named == ["link"]:
            ends = item["link"]
            if not isinstance(ends, (list, tuple)) or len(ends) != 3:
                raise TopologyError("no: link must be [from, to, method]")
            subject = link_subject(*ends)
            values = link_fields(item["fields"]) if item.get("fields") else {}
            if values.setdefault("method", ends[2]) != ends[2]:
                raise TopologyError("no: a link's method field must match its id")
        else:
            subject = route_subject(item["route"])
            values = {"hops": route_hops(item["route"], item.get("hops"))}
        facts.extend((subject, predicate, value) for predicate, value in values.items())
    return facts


def check_field_name_set(item, shapes) -> None:
    allowed = next(
        (keys for name, keys in shapes.items() if name in item),
        ("route", "hops"),
    )
    for name in item:
        check_field_name(name, allowed)
