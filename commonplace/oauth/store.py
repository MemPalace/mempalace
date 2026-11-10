"""OAuth grants in the control DB. Transactions serialize consumption and rotation.

Only credential hashes persist. Consumed refresh records remain so replay can
revoke the connection and every descendant token, including after a restart.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from datetime import datetime, timezone

from .. import urls
from ..control_db import Principal
from ..policy import Policy
from .http import OAuthError

ACCESS_SECONDS = 3600
REFRESH_SECONDS = 90 * 86400
CODE_SECONDS = 300
REQUEST_SECONDS = 600
SCOPES = ("memory:read", "memory:write", "memory:import", "memory:import:read")
OAUTH_DEFAULT_SCOPES = ("memory:read", "memory:write")

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_name TEXT NOT NULL,
    redirect_uris TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_requests (
    request_hash TEXT PRIMARY KEY,
    parameters TEXT NOT NULL,
    authorize_path TEXT NOT NULL,
    browser_hash TEXT NOT NULL,
    session_hash TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES oauth_clients(client_id),
    connection_id TEXT NOT NULL REFERENCES connections(id),
    redirect_uri TEXT NOT NULL,
    resource TEXT NOT NULL,
    challenge TEXT NOT NULL,
    scope TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at INTEGER
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('access', 'refresh')),
    client_id TEXT NOT NULL REFERENCES oauth_clients(client_id),
    connection_id TEXT NOT NULL REFERENCES connections(id),
    resource TEXT NOT NULL,
    scope TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at INTEGER,
    revoked_at INTEGER
);
CREATE INDEX IF NOT EXISTS oauth_tokens_connection ON oauth_tokens(connection_id);
"""


def now() -> int:
    return int(time.time())


def iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def ensure_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)


def client(db, client_id: str):
    if not isinstance(client_id, str) or not client_id or len(client_id) > 200:
        raise OAuthError("invalid_client")
    with db._lock:
        row = db._conn.execute(
            "SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)
        ).fetchone()
    if row is None:
        raise OAuthError("invalid_client")
    return row


def resource(parameters: dict) -> str:
    if "resource" in parameters and parameters["resource"] != urls.mcp_url():
        raise OAuthError("invalid_target", "Unknown resource")
    return urls.mcp_url()


def scope(value: str | None) -> str:
    requested = OAUTH_DEFAULT_SCOPES if value is None else value.split()
    if not requested or set(requested) - set(SCOPES):
        raise OAuthError("invalid_scope")
    return " ".join(sorted(set(requested)))


def _live_connection(conn, connection_id):
    row = conn.execute(
        "SELECT c.*, u.email AS user_email, u.disabled_at AS user_disabled, m.role "
        "FROM connections c JOIN users u ON u.id = c.user_id "
        "LEFT JOIN memberships m ON m.workspace_id = c.workspace_id AND m.user_id = c.user_id "
        "WHERE c.id = ? AND c.auth = 'oauth'",
        (connection_id,),
    ).fetchone()
    if (
        row is None
        or row["revoked_at"]
        or row["user_disabled"]
        or row["role"] is None
        or (row["expires_at"] and row["expires_at"] <= iso())
    ):
        return None
    return row


def _revoke(conn, connection_id):
    conn.execute(
        "UPDATE connections SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
        (iso(), connection_id),
    )
    conn.execute(
        "UPDATE oauth_tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE connection_id = ?",
        (now(), connection_id),
    )


def _issue(conn, grant) -> dict:
    access = "cmmn_at_" + secrets.token_urlsafe(32)
    refresh = "cmmn_rt_" + secrets.token_urlsafe(48)
    for secret, kind, lifetime in (
        (access, "access", ACCESS_SECONDS),
        (refresh, "refresh", REFRESH_SECONDS),
    ):
        conn.execute(
            "INSERT INTO oauth_tokens(token_hash, kind, client_id, connection_id, resource, "
            "scope, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                digest(secret),
                kind,
                grant["client_id"],
                grant["connection_id"],
                grant["resource"],
                grant["scope"],
                now(),
                now() + lifetime,
            ),
        )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_SECONDS,
        "refresh_token": refresh,
        "scope": grant["scope"],
        "resource": grant["resource"],
    }


def _guess_client(client_row) -> str:
    from urllib.parse import urlsplit

    name = client_row["client_name"].lower()
    hosts = [urlsplit(uri).hostname or "" for uri in json.loads(client_row["redirect_uris"])]
    if "claude" in name or any(h == "claude.ai" or h.endswith(".claude.ai") for h in hosts):
        return "claude-ai"
    if (
        "chatgpt" in name
        or "openai" in name
        or any(h in ("chatgpt.com", "chat.openai.com") or h.endswith(".chatgpt.com") for h in hosts)
    ):
        return "chatgpt"
    return "other"


def create_code(conn, client_row, principal, parameters) -> str:
    cid = "conn_" + secrets.token_hex(8)
    slug = re.sub(r"[^a-z0-9]+", "-", client_row["client_name"].lower()).strip("-")[:80] or "client"
    while True:
        name = f"oauth:{slug}-{secrets.token_hex(2)}"
        if not conn.execute(
            "SELECT 1 FROM connections WHERE workspace_id = ? AND name = ?",
            (principal.workspace_id, name),
        ).fetchone():
            break
    policy = Policy.build(
        profile="slim",
        trust="standard",
        write_wing="inbox",
        read_wings=["shared", "inbox", "project_*"],
        scopes=parameters["scope"].split(),
    )
    conn.execute(
        "INSERT INTO connections(id, workspace_id, user_id, name, client, auth, policy, "
        "created_by, created_at) VALUES (?,?,?,?,?,'oauth',?,?,?)",
        (
            cid,
            principal.workspace_id,
            principal.user_id,
            name,
            _guess_client(client_row),
            policy.to_json(),
            principal.user_id,
            iso(),
        ),
    )
    code = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO oauth_codes(code_hash, client_id, connection_id, redirect_uri, resource, "
        "challenge, scope, expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            digest(code),
            client_row["client_id"],
            cid,
            parameters["redirect_uri"],
            parameters["resource"],
            parameters["code_challenge"],
            parameters["scope"],
            now() + CODE_SECONDS,
        ),
    )
    return code


def exchange(db, parameters: dict) -> dict:
    client_row = client(db, parameters.get("client_id", ""))
    resource(parameters)
    verifier = parameters.get("code_verifier", "")
    if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
        raise OAuthError("invalid_grant")
    code = parameters.get("code", "")
    if not code or len(code) > 200:
        raise OAuthError("invalid_grant")
    result = None
    with db._lock, db._tx() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_codes WHERE code_hash = ?", (digest(code),)
        ).fetchone()
        if (
            row is None
            or row["client_id"] != client_row["client_id"]
            or row["redirect_uri"] != parameters.get("redirect_uri")
            or row["resource"] != urls.mcp_url()
            or not secrets.compare_digest(row["challenge"], challenge(verifier))
        ):
            raise OAuthError("invalid_grant")
        if row["used_at"] is not None:
            _revoke(conn, row["connection_id"])
        elif row["expires_at"] > now() and _live_connection(conn, row["connection_id"]):
            conn.execute(
                "UPDATE oauth_codes SET used_at = ? WHERE code_hash = ?", (now(), row["code_hash"])
            )
            result = _issue(conn, row)
    if result is None:
        raise OAuthError("invalid_grant")
    return result


def rotate(db, parameters: dict) -> dict:
    client_row = client(db, parameters.get("client_id", ""))
    resource(parameters)
    token = parameters.get("refresh_token", "")
    if not token or len(token) > 200:
        raise OAuthError("invalid_grant")
    result = None
    with db._lock, db._tx() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = 'refresh'", (digest(token),)
        ).fetchone()
        if (
            row is None
            or row["client_id"] != client_row["client_id"]
            or row["resource"] != urls.mcp_url()
        ):
            raise OAuthError("invalid_grant")
        if row["used_at"] is not None:
            _revoke(conn, row["connection_id"])
        elif (
            not row["revoked_at"]
            and row["expires_at"] > now()
            and _live_connection(conn, row["connection_id"])
        ):
            # Scope narrowing is supported without ever widening the connection policy.
            grant = dict(row)
            if "scope" in parameters:
                narrowed = scope(parameters["scope"])
                if set(narrowed.split()) - set(row["scope"].split()):
                    raise OAuthError("invalid_scope")
                grant["scope"] = narrowed
            conn.execute(
                "UPDATE oauth_tokens SET used_at = ? WHERE token_hash = ?",
                (now(), row["token_hash"]),
            )
            result = _issue(conn, grant)
    if result is None:
        raise OAuthError("invalid_grant")
    return result


def revoke(db, parameters: dict) -> None:
    client_row = client(db, parameters.get("client_id", ""))
    resource(parameters)
    token = parameters.get("token", "")
    if not token or len(token) > 200:
        raise OAuthError("invalid_request")
    with db._lock, db._tx() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_tokens WHERE token_hash = ?", (digest(token),)
        ).fetchone()
        if row is not None and row["client_id"] == client_row["client_id"]:
            _revoke(conn, row["connection_id"])


class OAuthAuthenticator:
    def __init__(self, db):
        self.db = db

    def authenticate(self, bearer: str) -> Principal | None:
        if not bearer.startswith("cmmn_at_") or len(bearer) > 200:
            return None
        with self.db._lock:
            token = self.db._conn.execute(
                "SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = 'access'",
                (digest(bearer),),
            ).fetchone()
            if (
                token is None
                or token["revoked_at"]
                or token["expires_at"] <= now()
                or token["resource"] != urls.mcp_url()
            ):
                return None
            row = _live_connection(self.db._conn, token["connection_id"])
            if row is None:
                return None
            self.db._touch(row, datetime.now(timezone.utc))
            policy = Policy.from_json(row["policy"])
            # A narrowed refresh must also narrow what its access token can do.
            policy_data = json.loads(policy.to_json())
            policy_data["scopes"] = [s for s in policy.scopes if s in token["scope"].split()]
            policy = Policy.from_json(json.dumps(policy_data)).for_role(row["role"])
            return Principal(
                user_id=row["user_id"],
                user_email=row["user_email"],
                workspace_id=row["workspace_id"],
                connection_id=row["id"],
                connection=row["name"],
                client=row["client"],
                auth="oauth",
                role=row["role"],
                policy=policy,
            )
