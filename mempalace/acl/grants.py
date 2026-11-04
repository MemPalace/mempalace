"""The grants file (YAML or JSON, see docs/acl.md): token sha256s and each key's wings."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import stat
import sys
from dataclasses import dataclass, field

from . import ADMIN, Identity, Scope

logger = logging.getLogger(__name__)
_TOP_KEYS = {"version", "default_wing_mode", "levels", "wings", "keys"}
_KEY_KEYS = {"name", "token_sha256", "revoked", "wings", "read_wings", "clearance", "compartments"}
_MODES = ("open", "locked")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_EMPTY_TOKEN_HASH = hashlib.sha256(b"").hexdigest()


class GrantsError(ValueError):
    """The grants file is unusable. The server refuses to start."""


@dataclass(frozen=True)
class Key:
    name: str
    token_sha256: str
    revoked: bool = False
    wings: frozenset = frozenset()  # read and write
    read_wings: frozenset = frozenset()  # read only
    clearance: str = ""
    compartments: frozenset = frozenset()


@dataclass(frozen=True)
class GrantStore:
    keys: tuple
    default_wing_mode: str = "locked"
    wing_modes: dict = field(default_factory=dict)
    levels: tuple = ()

    def unenforced(self) -> list:
        checks = {
            "default_wing_mode: open": self.default_wing_mode == "open",
            "wings.<name>.mode: open": "open" in self.wing_modes.values(),
            "keys[].clearance": any(k.clearance for k in self.keys),
            "keys[].compartments": any(k.compartments for k in self.keys),
        }
        return [name for name, used in checks.items() if used]

    def scope_for(self, identity: Identity) -> Scope:
        if identity.source == "legacy":
            return ADMIN
        for key in self.keys:
            if key.name == identity.subject and not key.revoked:
                return Scope("locked", key.name, key.wings | key.read_wings, key.wings)
        return Scope("denied", identity.subject)


class TokenResolver:
    """Authorization header -> Identity; the IdentityResolver seam (``resolve(headers)``)."""

    def __init__(self, store: GrantStore, legacy_token: str = ""):
        self.store = store
        self.legacy_token = legacy_token

    def resolve(self, headers):
        auth = headers.get("Authorization", "") or ""
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token or token != token.strip():
            return None  # exactly "Bearer <token>": no padding, never an empty credential
        digest = hashlib.sha256(token.encode("utf-8", "replace")).hexdigest().encode()
        # every entry is compared, no early exit: flat timing
        hit = None
        for key in self.store.keys:
            if hmac.compare_digest(key.token_sha256.encode(), digest) and hit is None:
                hit = key
        want = f"Bearer {self.legacy_token}".encode()
        if self.legacy_token and hmac.compare_digest(auth.encode("utf-8", "replace"), want):
            return Identity("legacy", "legacy")
        return Identity(hit.name, "grants") if hit is not None and not hit.revoked else None


def _wing_set(values, where: str) -> frozenset:
    from ..config import normalize_wing_name

    if not isinstance(values or [], list):
        raise GrantsError(f"{where} must be a list of wing names")
    out = {}
    for raw in values or []:
        wing = normalize_wing_name(raw) if isinstance(raw, str) else ""
        if not wing:
            raise GrantsError(f"{where} has an invalid wing name: {raw!r}")
        if wing in out:
            raise GrantsError(f"{where}: {raw!r} and {out[wing]!r} are the same wing")
        out[wing] = raw
    return frozenset(out)


def _check_keys(entry, allowed: set, where: str) -> None:
    if not isinstance(entry, dict):
        raise GrantsError(f"{where} must be a mapping")
    if set(entry) - allowed:
        raise GrantsError(f"{where}: unknown key(s) {sorted(map(str, set(entry) - allowed))}")


def _parse_key(entry, levels, legacy_hash) -> Key:
    _check_keys(entry, _KEY_KEYS, "keys[]")
    name, digest = entry.get("name"), entry.get("token_sha256")
    if not isinstance(name, str) or not name.strip():
        raise GrantsError("every key needs a non-empty name")
    name = name.strip().lower()
    where = f"key {name!r}"
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise GrantsError(f"{where}: token_sha256 must be 64 lowercase hex characters")
    if digest in (legacy_hash, _EMPTY_TOKEN_HASH):
        raise GrantsError(f"{where}: its token is the server token or empty")
    revoked = entry.get("revoked", False)
    if not isinstance(revoked, bool):
        raise GrantsError(f"{where}: revoked must be true or false")
    wings = _wing_set(entry.get("wings"), f"{where} wings")
    read_wings = _wing_set(entry.get("read_wings"), f"{where} read_wings")
    if wings & read_wings:
        raise GrantsError(f"{where}: {sorted(wings & read_wings)} in both wings and read_wings")
    clearance = entry.get("clearance") or ""
    if clearance and clearance not in levels:
        raise GrantsError(f"{where}: clearance {clearance!r} is not in levels")
    comps = entry.get("compartments") or []
    if not isinstance(comps, list) or not all(isinstance(c, str) and c for c in comps):
        raise GrantsError(f"{where}: compartments must be a list of names")
    return Key(name, digest, revoked, wings, read_wings, clearance, frozenset(comps))


def parse_grants(data, legacy_token: str = "") -> GrantStore:
    from ..config import normalize_wing_name

    if not data:
        raise GrantsError("grants file is empty")
    _check_keys(data, _TOP_KEYS, "grants file")
    mode = data.get("default_wing_mode", "locked")
    levels = data.get("levels") or []
    if mode not in _MODES:
        raise GrantsError(f"default_wing_mode must be one of {_MODES}")
    if (
        not isinstance(levels, list)
        or not all(isinstance(v, str) for v in levels)
        or len(set(levels)) != len(levels)
    ):
        raise GrantsError("levels must be a list of unique names, lowest first")
    wing_entries = data.get("wings") or {}
    if not isinstance(wing_entries, dict):
        raise GrantsError("wings must be a mapping of wing name to settings")
    wing_modes = {}
    for raw, entry in wing_entries.items():
        _check_keys(entry or {}, {"mode"}, f"wings.{raw}")
        wing, wmode = normalize_wing_name(str(raw)), (entry or {}).get("mode", mode)
        if not wing or wing in wing_modes or wmode not in _MODES:
            raise GrantsError(f"wings.{raw}: invalid or duplicate wing, or mode not in {_MODES}")
        wing_modes[wing] = wmode
    legacy_hash = hashlib.sha256(legacy_token.encode()).hexdigest() if legacy_token else None
    keys, seen = [], set()
    for entry in data.get("keys") or []:
        key = _parse_key(entry, levels, legacy_hash)
        if {key.name, key.token_sha256} & seen:
            raise GrantsError(f"duplicate key name or token hash: {key.name!r}")
        seen |= {key.name, key.token_sha256}
        keys.append(key)
    if not keys:
        logger.warning("grants file lists no keys: only the server token can connect")
    return GrantStore(keys, mode, wing_modes, levels)


def _no_dupes(keys):
    if len(set(keys)) != len(keys):
        raise GrantsError("duplicate key in grants file")


def _load_yaml(text):
    import yaml

    class Strict(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        keys = [
            loader.construct_object(k, deep=True)
            for k, _ in node.value
            if k.tag != "tag:yaml.org,2002:merge"  # `<<: *base` may be overridden
        ]
        _no_dupes(keys)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    return yaml.load(text, Strict)


def load_grants(path: str, legacy_token: str = "") -> GrantStore:
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if sys.platform != "win32" and mode & 0o077:
            raise GrantsError(f"{path}: mode {oct(mode)} is too open, use 0600")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        try:
            data = json.loads(
                text,
                object_pairs_hook=lambda pairs: _no_dupes([k for k, _ in pairs]) or dict(pairs),
            )
        except json.JSONDecodeError:
            data = _load_yaml(text)
        return parse_grants(data, legacy_token)
    except GrantsError:
        raise
    except OSError as exc:
        raise GrantsError(f"cannot read grants file {path}: {exc.strerror}") from exc
    except Exception as exc:  # RecursionError, yaml.YAMLError, odd shapes: never a traceback
        raise GrantsError(f"{path}: not a valid grants file ({type(exc).__name__})") from exc


def load_for_server(path: str, legacy_token: str, tokenless: bool = False) -> GrantStore:
    """Load the file for ``mempalace serve`` and refuse every unsafe combination."""
    if not path.strip():
        raise GrantsError("--grants-file is set but blank")
    if tokenless:
        raise GrantsError("a grants file cannot be used in local_only_tokenless mode")
    if not legacy_token:
        raise GrantsError(
            "a grants file needs a server token (--token or MEMPALACE_MCP_HTTP_TOKEN)"
        )
    store = load_grants(path, legacy_token)
    if pending := store.unenforced():
        raise GrantsError(f"not enforced yet in this release, remove: {', '.join(pending)}")
    return store
