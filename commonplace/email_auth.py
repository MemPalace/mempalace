"""Public email challenges; accounts are provisioned only after verification."""

from __future__ import annotations

import logging
import re
import secrets
from datetime import timedelta

from . import mailer, ratelimit, sessions, urls
from .oauth.http import OAuthError, body, query, redirect, send, send_json
from .social import _next, accounts
from .social.providers import Profile, SignInError

logger = logging.getLogger(__name__)
MESSAGE = "If this email can sign in, a link is on its way."
EMAIL_LIMIT = ratelimit.Limit("email-signin", 3, 15 * 60)
SCHEMA = """
CREATE TABLE IF NOT EXISTS email_challenges (
    token_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    next TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""
_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
_CONFIRMATION = b"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in - Commonplace</title><link rel="stylesheet" href="/app/app.css">
<link rel="stylesheet" href="/app/ui.css">
</head><body><main class="signin"><a class="brand-home" href="/app/" aria-label="Commonplace">
<img class="brand-v2" src="/app/brand/logo-small-dark.svg" alt="Commonplace"></a>
<h1>Sign in</h1><form method="post"><button class="blockbtn email-confirm" name="confirm"
value="1">Sign in</button></form></main></body></html>"""


def _token(h):
    token = query(h.path).get("t", "")
    if h.command == "POST":
        media = (h.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        token = body(h, as_json=media == "application/json").get("t", token)
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise SignInError("expired")
    return token


def _email(value) -> str:
    if not isinstance(value, str):
        raise OAuthError("invalid_request", "Enter a valid email address")
    value = value.strip().lower()
    local = value.split("@", 1)[0]
    if (
        len(value) > 254
        or len(local) > 64
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or not _EMAIL.fullmatch(value)
    ):
        raise OAuthError("invalid_request", "Enter a valid email address")
    return value


class EmailAuth:
    def __init__(self, gateway):
        self.gateway = gateway
        self.db = gateway.db
        try:
            self.mailer = mailer.from_env()
        except mailer.MailError:
            logger.warning("Email sign-in mail is not configured")
            self.mailer = mailer.DisabledMailer()
        with self.db._lock:
            self.db._conn.executescript(SCHEMA)
        accounts.ensure_schema(self.db)

    def request(self, h):
        try:
            media = (h.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            data = body(h, as_json=media == "application/json")
            email = _email(data.get("email"))
            next_path = data.get("next", "")
            if not isinstance(next_path, str):
                raise OAuthError("invalid_request")
            next_path = _next(next_path)
        except OAuthError as exc:
            send_json(h, exc.status, {"error": "Enter a valid email address"})
            return
        if self.gateway.rate_limits:
            retry = self.gateway.limiter.check([(EMAIL_LIMIT, sessions._hash(email))])
            if retry:
                send(
                    h,
                    429,
                    b'{"error":"slow down"}',
                    headers=(("Retry-After", str(retry)),),
                )
                return
        with self.db._lock:
            user = self.db._conn.execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
        allowed = not user["disabled_at"] if user else accounts.signup_enabled()
        if allowed and self.mailer.configured:
            self._send(email, next_path)
        send_json(h, 202, {"message": MESSAGE})

    def _send(self, email, next_path):
        token = secrets.token_urlsafe(32)
        now = sessions._now()
        with self.db._lock, self.db._tx() as conn:
            conn.execute("DELETE FROM email_challenges WHERE expires_at <= ?", (now.isoformat(),))
            conn.execute(
                "INSERT INTO email_challenges(token_hash, email, next, expires_at) VALUES (?,?,?,?)",
                (
                    sessions._hash(token),
                    email,
                    next_path,
                    (now + timedelta(minutes=sessions.LINK_MINUTES)).isoformat(),
                ),
            )
        sent = False
        try:
            link = urls.app_link("/app/auth/email/verify?t=" + token)
            self.mailer.send(
                email,
                "Sign in to Commonplace",
                f"Sign in to Commonplace:\n\n{link}\n\n"
                f"This link works once and expires in {sessions.LINK_MINUTES} minutes.\n"
                "If you did not request it, you can ignore this email.",
                None,
            )
            sent = True
        except mailer.MailError:
            # Delivery errors may contain the message or recipient. Never log them.
            logger.warning("Email sign-in mail could not be sent")
        finally:
            if not sent:
                with self.db._lock:
                    self.db._conn.execute(
                        "DELETE FROM email_challenges WHERE token_hash = ?",
                        (sessions._hash(token),),
                    )

    def confirmation(self, h):
        try:
            token = _token(h)
            with self.db._lock:
                row = self.db._conn.execute(
                    "SELECT expires_at FROM email_challenges WHERE token_hash = ?",
                    (sessions._hash(token),),
                ).fetchone()
            if row is None or row["expires_at"] <= sessions._now().isoformat():
                raise SignInError("expired")
        except (OAuthError, SignInError):
            send(h, 400, b"Sign in link is invalid or expired", media="text/plain; charset=utf-8")
            return
        send(
            h,
            200,
            _CONFIRMATION,
            media="text/html; charset=utf-8",
            # Keep the token out of Referer while preserving a normal form Origin.
            referrer_policy="strict-origin",
            headers=(
                (
                    "Content-Security-Policy",
                    "default-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                ),
            ),
        )

    def verify(self, h):
        from .gateway import _same_origin_allowed

        if not _same_origin_allowed(self.gateway, h):
            send_json(h, 403, {"error": "same-origin request required"})
            return
        try:
            token = _token(h)
            with self.db._lock:
                # Consume before provisioning, so simultaneous requests cannot replay it.
                with self.db._tx() as conn:
                    row = conn.execute(
                        "SELECT * FROM email_challenges WHERE token_hash = ?",
                        (sessions._hash(token),),
                    ).fetchone()
                    conn.execute(
                        "DELETE FROM email_challenges WHERE token_hash = ?",
                        (sessions._hash(token),),
                    )
                if row is None or row["expires_at"] <= sessions._now().isoformat():
                    raise SignInError("expired")
                uid = accounts.resolve(
                    self.db,
                    "email",
                    Profile(row["email"], "", (row["email"],)),
                    signup=accounts.signup_enabled(),
                )
                workspace = sessions.home_workspace(self.db, uid)
                if workspace is None:
                    raise SignInError("account")
                login = sessions.create_login_link(self.db, self.db.user(uid)["email"], workspace)
                session = sessions.redeem_login_link(self.db, login)
                if not session or sessions.session_principal(self.db, session) is None:
                    raise SignInError("failed")
            next_path = row["next"]
            destination = (
                urls.AGENT_URL + next_path
                if next_path.startswith("/oauth/")
                else next_path or "/app/"
            )
            redirect(
                h,
                destination,
                status=303,
                headers=(
                    ("Set-Cookie", sessions.set_cookie(session, self.gateway.secure_cookies)),
                ),
            )
        except (OAuthError, SignInError):
            send(h, 400, b"Sign in link is invalid or expired", media="text/plain; charset=utf-8")


def install(gateway):
    if hasattr(gateway, "email_auth"):
        return
    gateway.email_auth = EmailAuth(gateway)
    gateway.add_route("POST", "/app/auth/email", gateway.email_auth.request)
    gateway.add_route("GET", "/app/auth/email/verify", gateway.email_auth.confirmation)
    gateway.add_route("POST", "/app/auth/email/verify", gateway.email_auth.verify)
