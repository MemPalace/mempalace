"""An explicit browser grant, separate from GitHub sign-in and agent authority."""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import replace
from urllib.parse import quote, urlsplit

from .. import sessions, urls
from ..boards.service import BoardService
from ..extensions import ToolContext, ToolError
from ..oauth.http import OAuthError, body, query, redirect, send_json
from ..oauth.store import digest
from ..social import accounts
from ..social.providers import SignInError
from . import client
from .store import Store

COOKIE = "cmmn_github_connect"
CALLBACK = "/app/auth/github/callback"
ACCESS = "https://github.com/settings/applications"
TTL = 600
WORKER_LEASE_TTL = 30
WORKER_HEARTBEAT = 5


def enabled():
    return os.environ.get("COMMONPLACE_GITHUB_CONNECT") == "on"


def csrf(session):
    return hashlib.sha256(("github-consent-v1:" + session).encode()).hexdigest()


def scopes(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z_:,\s-]*", value):
        raise SignInError("github_scope")
    return frozenset(value.replace(",", " ").split())


class GitHub:
    def __init__(self, gateway):
        self.gateway = gateway
        self.db = gateway.db
        self.store = Store(self.db)
        self._live = 0
        self._owner = secrets.token_hex(32)
        self._stopped = threading.Event()
        self._close_lock = threading.Lock()
        self._closed = False
        self._heartbeat_thread = None
        self._owns_lease = False
        self.db.add_pre_close_callback(self.close)
        self._claim()
        if not self._owns_lease:
            logging.getLogger(__name__).warning("GitHub worker waiting for another serve's lease")
        self._heartbeat_thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._heartbeat_thread.start()

    def _claim(self):
        # Only a never-owner may recover: a former owner may have remote work
        # still in flight and must remain fenced until the process restarts.
        with self.db._lock, self.db._tx() as conn:
            if self._stopped.is_set():
                return
            now = time.time()
            lease = conn.execute("SELECT expires_at FROM github_worker_lease WHERE id=1").fetchone()
            if lease is None or lease["expires_at"] <= now:
                conn.execute(
                    "INSERT OR REPLACE INTO github_worker_lease VALUES (1,?,?)",
                    (self._owner, now + WORKER_LEASE_TTL),
                )
                self._recover_rows(conn)
                self._owns_lease = True

    @property
    def ready(self):
        if not self._owns_lease or self._stopped.is_set():
            return False
        with self.db._lock:
            return (
                self.db._conn.execute(
                    "SELECT 1 FROM github_worker_lease WHERE id=1 AND owner=? AND expires_at>?",
                    (self._owner, time.time()),
                ).fetchone()
                is not None
            )

    def _fence(self, conn):
        # Check alongside each mutation: an expired owner must not renew itself
        # or settle the counters of its successor after a delayed remote reply.
        now = time.time()
        if (
            self._stopped.is_set()
            or conn.execute(
                "UPDATE github_worker_lease SET expires_at=? WHERE id=1 AND owner=? AND expires_at>?",
                (now + WORKER_LEASE_TTL, self._owner, now),
            ).rowcount
            != 1
        ):
            raise SignInError("github_pending")

    def _heartbeat(self):
        while not self._stopped.wait(WORKER_HEARTBEAT):
            try:
                if not self._owns_lease:
                    self._claim()
                else:
                    with self.db._lock, self.db._tx() as conn:
                        self._fence(conn)
            except SignInError:
                logging.getLogger(__name__).warning("GitHub worker lost its lease; writes fenced")
                return
            except sqlite3.Error:
                # Retry on the next tick; an expired lease still fails closed.
                continue

    def close(self):
        # Server and database teardown can both close us, including concurrently.
        with self._close_lock:
            if self._closed:
                return
            self._stopped.set()
            if self._heartbeat_thread is not None:
                self._heartbeat_thread.join()
            with self.db._lock, self.db._tx() as conn:
                conn.execute("DELETE FROM github_worker_lease WHERE owner=?", (self._owner,))
            self._closed = True

    def provider(self):
        return self.gateway.sign_in.providers.get("github") if self.ready else None

    def principal(self, h):
        session = sessions.cookie_value(h.headers.get("Cookie"))
        principal = sessions.session_principal(self.db, session or "")
        if h.headers.get("Authorization") or principal is None or not principal.person:
            raise OAuthError(
                "browser_session_required", status=403 if self.gateway.principal_for(h) else 401
            )
        return principal, session

    def visible(self, principal, card):
        if type(card) is not int or card <= 0:
            raise SignInError("github_card")
        try:
            return BoardService(ToolContext(principal=principal, gateway=self.gateway)).task_show(
                card
            )
        except ToolError:
            raise SignInError("github_card") from None

    def binding(self, conn, row, session, principal):
        session_row = conn.execute(
            "SELECT * FROM sessions WHERE id_hash=? AND revoked_at IS NULL AND expires_at>?",
            (digest(session), sessions._now().strftime(sessions._FMT)),
        ).fetchone()
        live = (
            sessions.person_principal(self.db, session_row["user_id"], session_row["workspace_id"])
            if session_row
            else None
        )
        lifecycle = self.store.lifecycle(conn, principal.user_id)
        if (
            live is None
            or live.user_id != row["user_id"]
            or live.workspace_id != row["workspace_id"]
            or digest(session) != row["session_hash"]
            or row["expires_at"] <= time.time()
            or lifecycle["generation"] != row["generation"]
            or lifecycle["disconnect_pending"]
            or lifecycle["deleting"]
        ):
            raise SignInError("github_expired")
        self.visible(live, row["card"])
        return lifecycle

    def handle(self, h):
        try:
            principal, session = self.principal(h)
            if h.command == "GET":
                return send_json(h, 200, self.status(principal, session))
            if h.headers.get_all("Origin", []) != [urls.APP_URL]:
                raise OAuthError("origin_required", status=403)
            data = body(h, as_json=True)
            if not isinstance(data.get("csrf"), str) or not hmac.compare_digest(
                data["csrf"], csrf(session)
            ):
                raise OAuthError("csrf_required", status=403)
            if not self.ready:
                raise OAuthError("github_pending", status=409)
            if h.command != "DELETE" and (not enabled() or self.provider() is None):
                raise OAuthError("github_unconfigured", status=409)
            path = urlsplit(h.path).path
            if path == "/api/github/connect":
                return self.start(h, principal, session, data)
            if path == "/api/github/confirm":
                return self.confirm(h, principal, session, data)
            return send_json(h, 200, self.disconnect(principal.user_id, principal.workspace_id))
        except OAuthError as exc:
            send_json(h, exc.status, {"error": exc.error})
        except SignInError as exc:
            send_json(h, 409, {"error": exc.code, "app_access_url": ACCESS})

    def status(self, principal, session):
        uid = principal.user_id
        result = {
            "state": "absent",
            "connect_enabled": enabled() and self.provider() is not None,
            "csrf": csrf(session),
            "app_access_url": ACCESS,
            "disclosure": "Disconnect revokes this app's GitHub login authorization too. Your current session survives; future login requires authorization again.",
        }
        with self.db._lock:
            conn = self.db._conn
            life = self.store.lifecycle(conn, uid)
            result["acknowledge_required"] = bool(life["unconfirmed"])
            result["pending"] = bool(
                life["disconnect_pending"] or life["active_exchanges"] or life["deleting"]
            )
            grant = conn.execute(
                "SELECT * FROM github_grants WHERE user_id=? AND workspace_id=?",
                (uid, principal.workspace_id),
            ).fetchone()
            if grant and not grant["disabled"] and grant["expires_at"] > time.time():
                # Unlock on reads too: missing keys or tampering never shows a usable connection.
                self.store.decrypt(conn, uid, "grant", grant["secret"])
                result.update(state="connected", login=grant["login"])
            elif grant:
                result.update(
                    state="expired",
                    message="Expired locally; review GitHub app access before reconnecting.",
                )
            if grant is None:
                elsewhere = conn.execute(
                    "SELECT workspace_id FROM github_grants WHERE user_id=?", (uid,)
                ).fetchone()
                if elsewhere:
                    result.update(state="other_workspace", workspace=elsewhere["workspace_id"])
            if result["state"] == "absent" and not result["connect_enabled"]:
                result["state"] = "unconfigured"
            pending = conn.execute(
                "SELECT * FROM github_confirmations WHERE user_id=? AND session_hash=? AND workspace_id=?",
                (uid, digest(session), principal.workspace_id),
            ).fetchone()
            if pending and pending["expires_at"] > time.time():
                value = self.store.decrypt(conn, uid, "confirmation", pending["secret"])
                result.update(
                    state="confirm",
                    login=value["login"],
                    subject=value["subject"],
                    inherited_scopes=value["scopes"],
                )
        return result

    def start(self, h, principal, session, data):
        origin = urlsplit(urls.APP_URL)
        if origin.scheme != "https" and not (
            origin.scheme == "http" and origin.hostname in ("localhost", "127.0.0.1", "::1")
        ):
            raise SignInError("github_https")
        self.visible(principal, data.get("card"))
        uid = principal.user_id
        # Expired confirmations are cleaned on the next consent write; GET stays local.
        self.expire(uid)
        state = "gc1_" + secrets.token_urlsafe(32)
        browser, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.db._lock, self.db._tx() as conn:
            self._fence(conn)
            life = self.store.lifecycle(conn, uid)
            grant = conn.execute("SELECT * FROM github_grants WHERE user_id=?", (uid,)).fetchone()
            if (
                grant
                and grant["expires_at"] <= time.time()
                and not life["active_exchanges"]
                and not life["disconnect_pending"]
            ):
                conn.execute("DELETE FROM github_grants WHERE user_id=?", (uid,))
                conn.execute(
                    "UPDATE github_lifecycle SET generation=generation+1 WHERE user_id=?",
                    (uid,),
                )
                life = self.store.lifecycle(conn, uid)
                grant = None
            if (
                life["disconnect_pending"]
                or life["active_exchanges"]
                or life["deleting"]
                or grant
                or conn.execute(
                    "SELECT 1 FROM github_connect_flows WHERE user_id=?", (uid,)
                ).fetchone()
                or conn.execute(
                    "SELECT 1 FROM github_confirmations WHERE user_id=?", (uid,)
                ).fetchone()
            ):
                raise SignInError("github_pending")
            if life["unconfirmed"] and data.get("acknowledge") is not True:
                raise SignInError("github_acknowledge")
            secret = self.store.encrypt(conn, uid, "verifier", verifier)
            conn.execute(
                "INSERT INTO github_connect_flows(state_hash,user_id,session_hash,browser_hash,workspace_id,card,generation,expires_at,secret,next) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    digest(state),
                    uid,
                    digest(session),
                    digest(browser),
                    principal.workspace_id,
                    data["card"],
                    life["generation"],
                    time.time() + TTL,
                    secret,
                    f"/app/?github_card={data['card']}#/needs",
                ),
            )
            conn.execute("UPDATE github_lifecycle SET unconfirmed=0 WHERE user_id=?", (uid,))
        cookie = f"{COOKIE}={browser}; Path={CALLBACK}; HttpOnly; SameSite=Lax; Max-Age=600"
        if self.gateway.secure_cookies:
            cookie += "; Secure"
        authorize = replace(self.provider(), scope="").authorize_url(state, verifier, "")
        import json

        from ..oauth.http import send

        send(
            h,
            200,
            json.dumps({"authorize_url": authorize}).encode(),
            headers=(("Set-Cookie", cookie),),
        )

    def callback(self, h):
        token = None
        uid = None
        active = False
        landing = "/app/"
        try:
            if h.command != "GET" or not enabled() or self.provider() is None:
                raise SignInError("github_disabled")
            parameters = query(h.path)
            principal, session = self.principal(h)
            uid = principal.user_id
            with self.db._lock, self.db._tx() as conn:
                self._fence(conn)
                row = conn.execute(
                    "SELECT * FROM github_connect_flows WHERE state_hash=?",
                    (digest(parameters.get("state", "")),),
                ).fetchone()
                if row is None:
                    raise SignInError("github_expired")
                # Delete and validate in one committed transaction: invalid callbacks also consume state.
                conn.execute(
                    "DELETE FROM github_connect_flows WHERE state_hash=?", (row["state_hash"],)
                )
            with self.db._lock, self.db._tx() as conn:
                self._fence(conn)
                browser = sessions.cookie_value(h.headers.get("Cookie"), COOKIE)
                if (
                    not browser
                    or digest(browser) != row["browser_hash"]
                    or row["purpose"] != "connect"
                ):
                    raise SignInError("github_expired")
                self.binding(conn, row, session, principal)
                if "error" in parameters:
                    raise SignInError("github_denied")
                code = parameters.get("code")
                if not isinstance(code, str) or not 0 < len(code) <= 2048:
                    raise SignInError("github_code")
                verifier = self.store.decrypt(conn, uid, "verifier", row["secret"])
                conn.execute(
                    "UPDATE github_lifecycle SET active_exchanges=active_exchanges+1 WHERE user_id=?",
                    (uid,),
                )
                active = True
                self._live += 1
                landing = row["next"]
            provider = self.provider()
            status, _, value = client.request(
                "POST",
                provider.token,
                form={
                    "grant_type": "authorization_code",
                    "client_id": provider.credentials["CLIENT_ID"],
                    "client_secret": provider.client_secret(),
                    "redirect_uri": provider.redirect_uri(),
                    "code": code,
                    "code_verifier": verifier,
                },
            )
            if not isinstance(value, dict):
                raise SignInError("github_token")
            token = value.get("access_token")
            if not isinstance(token, str) or not token or len(token) > 8192:
                token = None
                raise SignInError("github_token")
            if status != 200 or "error" in value:
                raise SignInError("github_token")
            granted = scopes(value.get("scope"))
            if not granted <= {"read:user", "user:email"}:
                raise SignInError("github_scope")
            expiry = value.get("expires_in")
            if expiry is None:
                # Non-expiring app acceptance is deliberately operator-attested, dated, and app-bound.
                evidence = os.environ.get("COMMONPLACE_GITHUB_NONEXPIRING_EVIDENCE", "")
                if not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}\|OAuth App\|"
                    + re.escape(provider.credentials["CLIENT_ID"])
                    + r"\|expiration disabled",
                    evidence,
                ):
                    raise SignInError("github_expiry")
                expires_at = 253402300799
            elif type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0:
                raise SignInError("github_expiry")
            else:
                expires_at = time.time() + expiry
            status, headers, profile = client.request(
                "GET", "https://api.github.com/user", token=token
            )
            evidence = (
                headers.get_all("X-OAuth-Scopes", [])
                if hasattr(headers, "get_all")
                else [v for k, v in headers.items() if k.lower() == "x-oauth-scopes"]
            )
            if any(scopes(value) != granted for value in evidence):
                raise SignInError("github_scope")
            if (
                status != 200
                or not isinstance(profile, dict)
                or type(profile.get("id")) is not int
                or profile["id"] <= 0
                or not isinstance(profile.get("login"), str)
                or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", profile["login"])
            ):
                raise SignInError("github_profile")
            candidate = {
                "token": token,
                "subject": str(profile["id"]),
                "login": profile["login"],
                "scopes": sorted(granted),
                "expires_at": expires_at,
            }
            with self.db._lock, self.db._tx() as conn:
                self._fence(conn)
                self.binding(conn, row, session, principal)
                known = conn.execute(
                    "SELECT subject FROM user_identities WHERE provider='github' AND user_id=? AND subject=?",
                    (uid, candidate["subject"]),
                ).fetchone()
                if known and not granted:
                    self.commit(conn, uid, row, candidate)
                else:
                    secret = self.store.encrypt(conn, uid, "confirmation", candidate)
                    conn.execute(
                        "INSERT INTO github_confirmations VALUES (?,?,?,?,?,?,?)",
                        (
                            uid,
                            row["session_hash"],
                            row["workspace_id"],
                            row["card"],
                            row["generation"],
                            row["expires_at"],
                            secret,
                        ),
                    )
                token = None
        except (OAuthError, SignInError):
            pass
        finally:
            try:
                if token:
                    self.cleanup_token(uid, token)
            finally:
                if active:
                    self.finish_exchange(uid)
        redirect(
            h,
            landing,
            status=303,
            headers=(
                (
                    "Set-Cookie",
                    f"{COOKIE}=; Path={CALLBACK}; HttpOnly; SameSite=Lax; Max-Age=0"
                    + ("; Secure" if self.gateway.secure_cookies else ""),
                ),
            ),
        )

    def commit(self, conn, uid, row, candidate):
        accounts.reserve_github_owner(conn, candidate["subject"], uid)
        secret = self.store.encrypt(conn, uid, "grant", {"token": candidate["token"]})
        conn.execute(
            "INSERT INTO github_grants VALUES (?,?,?,?,?,?,0)",
            (
                uid,
                row["workspace_id"],
                candidate["subject"],
                candidate["login"],
                candidate["expires_at"],
                secret,
            ),
        )

    def confirm(self, h, principal, session, data):
        uid = principal.user_id
        candidate = None
        accepted = False
        try:
            with self.db._lock, self.db._tx() as conn:
                self._fence(conn)
                row = conn.execute(
                    "SELECT * FROM github_confirmations WHERE user_id=?", (uid,)
                ).fetchone()
                if row is None:
                    raise SignInError("github_expired")
                # Only the bound session may consume this confirmation.
                if (
                    row["session_hash"] != digest(session)
                    or row["workspace_id"] != principal.workspace_id
                ):
                    raise SignInError("github_expired")
                candidate = self.store.decrypt(conn, uid, "confirmation", row["secret"])
                conn.execute("DELETE FROM github_confirmations WHERE user_id=?", (uid,))
                conn.execute(
                    "UPDATE github_lifecycle SET active_exchanges=active_exchanges+1 WHERE user_id=?",
                    (uid,),
                )
                self._live += 1
            with self.db._lock, self.db._tx() as conn:
                self._fence(conn)
                self.binding(conn, row, session, principal)
                if data.get("accept") is True:
                    self.commit(conn, uid, row, candidate)
                    accepted = True
        finally:
            if candidate:
                try:
                    if not accepted:
                        self.cleanup_token(uid, candidate["token"])
                finally:
                    self.finish_exchange(uid)
        send_json(h, 200, {"state": "connected" if accepted else "absent"})

    def cleanup_token(self, uid, token):
        try:
            return self.revoke_token(uid, token)
        except Exception:
            with self.db._lock, self.db._tx() as conn:
                self._fence(conn)
                self.db._conn.execute(
                    "UPDATE github_lifecycle SET unconfirmed=1 WHERE user_id=?", (uid,)
                )
            raise

    def revoke_token(self, uid, token):
        # Never revoke a token identical to the currently stored grant.
        with self.db._lock, self.db._tx() as conn:
            self._fence(conn)
            grant = self.db._conn.execute(
                "SELECT * FROM github_grants WHERE user_id=?", (uid,)
            ).fetchone()
            if grant:
                try:
                    if (
                        self.store.decrypt(self.db._conn, uid, "grant", grant["secret"])["token"]
                        == token
                    ):
                        return False
                except SignInError:
                    self.db._conn.execute(
                        "UPDATE github_lifecycle SET unconfirmed=1 WHERE user_id=?", (uid,)
                    )
                    return False
        ok = self.revoke(token, grant=False)
        if not ok:
            with self.db._lock, self.db._tx() as conn:
                self._fence(conn)
                self.db._conn.execute(
                    "UPDATE github_lifecycle SET unconfirmed=1 WHERE user_id=?", (uid,)
                )
        return ok

    def revoke(self, token, *, grant):
        provider = self.provider()
        if provider is None:
            return False
        try:
            status, _, _ = client.request(
                "DELETE",
                "https://api.github.com/applications/"
                + quote(provider.credentials["CLIENT_ID"], safe="")
                + ("/grant" if grant else "/token"),
                json_body={"access_token": token},
                credentials=(provider.credentials["CLIENT_ID"], provider.client_secret()),
            )
            return status == 204
        except SignInError:
            return False

    def expire(self, uid):
        candidate = None
        with self.db._lock, self.db._tx() as conn:
            self._fence(conn)
            conn.execute(
                "DELETE FROM github_connect_flows WHERE user_id=? AND expires_at<=?",
                (uid, time.time()),
            )
            row = conn.execute(
                "SELECT * FROM github_confirmations WHERE user_id=? AND expires_at<=?",
                (uid, time.time()),
            ).fetchone()
            if row:
                candidate = self.store.decrypt(conn, uid, "confirmation", row["secret"])
                conn.execute("DELETE FROM github_confirmations WHERE user_id=?", (uid,))
                conn.execute(
                    "UPDATE github_lifecycle SET active_exchanges=active_exchanges+1 WHERE user_id=?",
                    (uid,),
                )
                self._live += 1
        if candidate:
            try:
                ok = self.cleanup_token(uid, candidate["token"])
            finally:
                self.finish_exchange(uid)
            if not ok:
                with self.db._lock, self.db._tx() as conn:
                    self._fence(conn)
                    conn.execute(
                        "UPDATE github_lifecycle SET unconfirmed=1 WHERE user_id=?", (uid,)
                    )

    def disconnect(self, uid, workspace=None, *, deleting=False):
        tokens = []
        grant = None
        grant_token = None
        readable = True
        with self.db._lock, self.db._tx() as conn:
            self._fence(conn)
            life = self.store.lifecycle(conn, uid)
            if life["disconnect_pending"]:
                raise SignInError("github_pending")
            grant = conn.execute("SELECT * FROM github_grants WHERE user_id=?", (uid,)).fetchone()
            conn.execute(
                "UPDATE github_lifecycle SET generation=generation+1,disconnect_pending=1,remote_work=remote_work+1,deleting=max(deleting,?) WHERE user_id=?",
                (int(deleting), uid),
            )
            conn.execute("DELETE FROM github_connect_flows WHERE user_id=?", (uid,))
            pending = conn.execute(
                "SELECT * FROM github_confirmations WHERE user_id=?", (uid,)
            ).fetchone()
            if pending:
                try:
                    tokens.append(
                        self.store.decrypt(conn, uid, "confirmation", pending["secret"])["token"]
                    )
                except SignInError:
                    readable = False
                conn.execute("DELETE FROM github_confirmations WHERE user_id=?", (uid,))
            if grant:
                conn.execute("UPDATE github_grants SET disabled=1 WHERE user_id=?", (uid,))
                if grant["expires_at"] > time.time():
                    try:
                        grant_token = self.store.decrypt(conn, uid, "grant", grant["secret"])[
                            "token"
                        ]
                    except SignInError:
                        readable = False
        ok = self.revoke(grant_token, grant=True) if grant_token else grant is None
        ok = ok and readable
        for token in tokens:
            if token != grant_token:
                ok = self.revoke(token, grant=False) and ok
        with self.db._lock, self.db._tx() as conn:
            self._fence(conn)
            conn.execute("DELETE FROM github_grants WHERE user_id=?", (uid,))
            conn.execute(
                "UPDATE github_lifecycle SET unconfirmed=max(unconfirmed,?),remote_work=max(0,remote_work-1),disconnect_pending=CASE WHEN active_exchanges>0 OR remote_work>1 THEN 1 ELSE 0 END WHERE user_id=?",
                (int(not ok), uid),
            )
            life = self.store.lifecycle(conn, uid)
            if deleting and not life["active_exchanges"] and not life["remote_work"]:
                self.release_owner(conn, uid)
        return {
            "state": "absent",
            "message": "Disconnected"
            if ok
            else "Disconnected locally; GitHub revocation unconfirmed",
            "app_access_url": ACCESS,
        }

    def release_owner(self, conn, uid):
        conn.execute("DELETE FROM user_identities WHERE provider='github' AND user_id=?", (uid,))
        conn.execute("DELETE FROM github_account_owners WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM github_keys WHERE user_id=?", (uid,))

    def delete_user(self, uid):
        """Fence and remove GitHub state; the caller owns account deletion in other stores."""
        return self.disconnect(uid, deleting=True)

    def finish_exchange(self, uid):
        with self.db._lock:
            self._live -= 1
        with self.db._lock, self.db._tx() as conn:
            self._fence(conn)
            conn.execute(
                "UPDATE github_lifecycle SET active_exchanges=max(0,active_exchanges-1) WHERE user_id=?",
                (uid,),
            )
            life = self.store.lifecycle(conn, uid)
            if (
                not life["active_exchanges"]
                and not life["remote_work"]
                and not conn.execute(
                    "SELECT 1 FROM github_grants WHERE user_id=? AND disabled=1", (uid,)
                ).fetchone()
            ):
                conn.execute(
                    "UPDATE github_lifecycle SET disconnect_pending=0 WHERE user_id=?", (uid,)
                )
                if life["deleting"]:
                    self.release_owner(conn, uid)

    def recover(self):
        if self._live:
            raise SignInError("github_pending")
        with self.db._lock, self.db._tx() as conn:
            self._fence(conn)
            self._recover_rows(conn)

    def _recover_rows(self, conn):
        conn.execute(
            "UPDATE github_lifecycle SET generation=generation+1,unconfirmed=1 WHERE user_id IN (SELECT user_id FROM github_connect_flows UNION SELECT user_id FROM github_confirmations UNION SELECT user_id FROM github_grants WHERE disabled=1) OR disconnect_pending=1 OR active_exchanges>0 OR remote_work>0"
        )
        conn.execute("DELETE FROM github_connect_flows")
        conn.execute("DELETE FROM github_confirmations")
        conn.execute("DELETE FROM github_grants WHERE disabled=1")
        conn.execute(
            "UPDATE github_lifecycle SET disconnect_pending=0,active_exchanges=0,remote_work=0"
        )


def install(gateway):
    gateway.github = GitHub(gateway)
    for method, path in (
        ("GET", "/api/github"),
        ("POST", "/api/github/connect"),
        ("POST", "/api/github/confirm"),
        ("DELETE", "/api/github"),
    ):
        gateway.add_route(method, path, gateway.github.handle)
