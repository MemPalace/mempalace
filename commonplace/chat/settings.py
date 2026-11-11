"""Workspace front settings and operator-controlled key access."""

import ipaddress
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

from commonplace import webhooks
from commonplace.chat import types
from commonplace.chat.types import FrontSettings
from commonplace.extensions import ToolError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_model_settings (
    workspace_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '*',          -- '*' = workspace; #192: board slug
    role TEXT NOT NULL DEFAULT 'front',       -- #187 accepts only 'front'; #192: 'main','area:<name>','worker'
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    provider TEXT NOT NULL,                   -- #187: 'none'|'openai_compatible'|'anthropic'
    model TEXT NOT NULL DEFAULT '',
    base_url TEXT,                            -- openai_compatible only (required there); NULL otherwise
    key_ref TEXT,                             -- 'env:COMMONPLACE_CHAT_KEY_<NAME>' only; operator-allowlisted workspaces only; references visible to owners
    daily_limit INTEGER NOT NULL DEFAULT 200 CHECK (daily_limit BETWEEN 0 AND 2000),
    user_daily_limit INTEGER NOT NULL DEFAULT 50 CHECK (user_daily_limit BETWEEN 0 AND 2000),  -- m11; validated <= daily_limit
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, scope, role)
);
CREATE TABLE IF NOT EXISTS project_model_catalog (
    workspace_id TEXT NOT NULL,
    scope TEXT NOT NULL,                      -- board slug; '*' = workspace-wide
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    added_by TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, scope, provider, model)
);
"""


def ensure_schema(db) -> None:
    with db._lock:
        db._conn.executescript(_SCHEMA)


class LocalProviderMismatch(ValueError):
    pass


class NotLoopback(ValueError):
    pass


def loopback_host(host):
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback and getattr(address, "ipv4_mapped", None) is None
    except ValueError:
        return False


def is_local(config):
    try:
        return config.provider == "openai_compatible" and loopback_host(
            urlsplit(config.base_url or "").hostname
        )
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class ResolvedFront(FrontSettings):
    inherited: bool = False
    applies: bool = True

    def public(self):
        return {**super().public(), "inherited": self.inherited, "applies": self.applies}


def front(db, workspace_id: str, board: str | None = None) -> FrontSettings:
    with db._lock:
        row = db._conn.execute(
            "SELECT * FROM chat_model_settings WHERE workspace_id=? AND scope IN (?, '*') "
            "AND role='front' ORDER BY (scope=?) DESC LIMIT 1",
            (workspace_id, board or "*", board or "*"),
        ).fetchone()
    if row is None:
        return ResolvedFront(
            workspace_id,
            True,
            "none",
            "",
            None,
            None,
            types.DAILY_LIMIT_DEFAULT,
            types.USER_DAILY_LIMIT_DEFAULT,
            None,
            None,
        )
    config = ResolvedFront(
        workspace_id,
        bool(row["enabled"]),
        row["provider"],
        row["model"],
        row["base_url"],
        row["key_ref"],
        row["daily_limit"],
        row["user_daily_limit"],
        row["updated_by"],
        row["updated_at"],
        inherited=board is not None and row["scope"] == "*",
    )
    return replace(
        config,
        applies=not (config.inherited and config.provider != "none" and not is_local(config)),
    )


def set_front(db, principal, body: dict) -> FrontSettings:
    if not principal.person or principal.role != "owner":
        raise types.ChatDenied("Only a signed-in workspace owner can change chat settings")
    allowed = {
        "enabled",
        "provider",
        "model",
        "base_url",
        "key_ref",
        "daily_limit",
        "user_daily_limit",
    }
    if (
        not isinstance(body, dict)
        or "front" not in body
        or set(body) - {"front", "scope", "mode"}
        or not isinstance(body["front"], dict)
        or set(body["front"]) - allowed
    ):
        raise ValueError("Invalid chat settings")
    scope = body.get("scope", "*")
    mode = body.get("mode")
    if not isinstance(scope, str) or not scope or mode not in (None, "local", "hosted"):
        raise ValueError("Invalid chat scope or mode")
    data = body["front"]
    enabled = data.get("enabled", True)
    provider = data.get("provider", "none")
    if (
        type(enabled) is not bool
        or not isinstance(provider, str)
        or provider not in types.PROVIDERS
    ):
        raise ValueError("Invalid chat provider or enabled flag")
    model = data.get("model", types.DEFAULT_MODELS.get(provider, ""))
    base_url = data.get("base_url")
    key_ref = data.get("key_ref")
    if (
        not isinstance(model, str)
        or len(model) > 120
        or re.fullmatch(r"[A-Za-z0-9._:/-]*", model) is None
        or (provider != "none" and not model)
    ):
        raise ValueError("Invalid chat model")
    if key_ref is not None and (
        not isinstance(key_ref, str) or types.KEY_REF.fullmatch(key_ref) is None
    ):
        raise ValueError("Invalid chat key reference")
    if mode == "local":
        if provider != "openai_compatible":
            raise LocalProviderMismatch()
        if base_url is None:
            base_url = "http://127.0.0.1:11434"
        try:
            local = provider == "openai_compatible" and loopback_host(urlsplit(base_url).hostname)
        except (ValueError, TypeError):
            local = False
        if not local:
            raise NotLoopback()
    if provider == "openai_compatible":
        try:
            webhooks.validate_url(base_url)
            parts = urlsplit(base_url)
            if (
                "?" in base_url
                or "#" in base_url
                or parts.username is not None
                or parts.password is not None
                or parts.path.rstrip("/").endswith("/v1")
            ):
                raise ValueError()
        except (ToolError, ValueError, TypeError):
            raise ValueError("Invalid chat base URL") from None
    elif base_url is not None:
        raise ValueError("Chat base URL is only allowed for openai_compatible")
    if provider == "anthropic" and key_ref is None:
        raise ValueError("Anthropic requires a chat key reference")
    daily_limit = data.get("daily_limit", types.DAILY_LIMIT_DEFAULT)
    user_daily_limit = data.get("user_daily_limit", types.USER_DAILY_LIMIT_DEFAULT)
    if (
        type(daily_limit) is not int
        or type(user_daily_limit) is not int
        or not 0 <= user_daily_limit <= daily_limit <= types.DAILY_LIMIT_MAX
    ):
        raise ValueError("Invalid chat daily limits")
    settings = ResolvedFront(
        principal.workspace_id,
        enabled,
        provider,
        model,
        base_url,
        key_ref,
        daily_limit,
        user_daily_limit,
        principal.user_id,
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    if scope != "*" and settings.provider != "none" and not is_local(settings) and mode != "hosted":
        raise ValueError("Turn on is required for a board's hosted provider")
    if key_ref is not None and not key_allowed(settings):
        raise types.ChatDenied("key references need operator approval")
    with db._lock, db._tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chat_model_settings "
            "(workspace_id, scope, role, enabled, provider, model, base_url, key_ref, "
            "daily_limit, user_daily_limit, updated_by, updated_at) "
            "VALUES (?, ?, 'front', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                settings.workspace_id,
                scope,
                int(enabled),
                provider,
                model,
                base_url,
                key_ref,
                daily_limit,
                user_daily_limit,
                settings.updated_by,
                settings.updated_at,
            ),
        )
        db.audit(
            action="chat.settings",
            reason=mode or "legacy",
            decision="ok",
            workspace_id=principal.workspace_id,
            user_id=principal.user_id,
        )
    return settings


def resolve_key(settings: FrontSettings) -> str | None:
    if not key_allowed(settings):
        return None
    return os.environ.get(settings.key_ref.removeprefix("env:")) or None


def key_allowed(settings: FrontSettings) -> bool:
    if (
        not isinstance(settings.key_ref, str)
        or types.KEY_REF.fullmatch(settings.key_ref) is None
        or settings.workspace_id not in _allowlist(types.KEY_WORKSPACES_ENV)
    ):
        return False
    if settings.provider == "anthropic":
        return settings.base_url is None
    if settings.provider != "openai_compatible" or not settings.base_url:
        return False
    try:
        host = urlsplit(settings.base_url).hostname
    except ValueError:
        return False
    return host is not None and host.lower() in _allowlist(types.KEY_HOSTS_ENV, lower=True)


def _allowlist(name: str, *, lower: bool = False) -> set[str]:
    entries = {entry.strip() for entry in os.environ.get(name, "").split(",") if entry.strip()}
    return {entry.lower() for entry in entries} if lower else entries


def hosted_options(workspace_id):
    if workspace_id not in _allowlist(types.KEY_WORKSPACES_ENV):
        return []
    refs = sorted("env:" + name for name in os.environ if types.KEY_REF.fullmatch("env:" + name))
    hosts = sorted(_allowlist(types.KEY_HOSTS_ENV, lower=True))
    return [
        {
            "provider": provider,
            "keys": [
                {"ref": ref, "label": f"Saved key {i} · {provider}"}
                for i, ref in enumerate(refs, 1)
            ],
            "base_urls": ["https://" + host for host in hosts]
            if provider == "openai_compatible"
            else [],
        }
        for provider in ("anthropic", "openai_compatible")
    ]
