"""Offline boundaries and reporting for the fresh-user release journey.

Only mail delivery and the remote social/voice providers are synthetic. Accounts,
sessions, connections and workspace content must be created through public routes.
"""

from __future__ import annotations

import re
import secrets
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field, replace
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit


class Blocked(Exception):
    """A required public capability or prerequisite is absent."""


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class Mailbox:
    configured = True

    def __init__(self):
        self.messages = []
        self.changed = threading.Condition()

    def send(self, to, subject, text, html=None):
        with self.changed:
            self.messages.append((to, text))
            self.changed.notify_all()

    def login_link(self, email, origin, *, timeout=3, after=0):
        def matching_link():
            for recipient, text in reversed(self.messages[after:]):
                if recipient != email:
                    continue
                for link in re.findall(r'https?://[^\s<>"\']+', text):
                    parsed = urlsplit(link)
                    if (
                        parsed.netloc == urlsplit(origin).netloc
                        and parsed.scheme == urlsplit(origin).scheme
                        and parsed.path == "/app/auth/email/verify"
                        and parse_qs(parsed.query).get("t")
                    ):
                        return link
            return None

        with self.changed:
            link = self.changed.wait_for(matching_link, timeout=timeout)
        if link:
            return link
        raise Blocked(
            "Email request did not deliver a same-instance /app/auth/email/verify?t= link"
        )


class SyntheticVoice:
    """Replace decoding, retaining the real route, permissions and usage meter."""

    @staticmethod
    def available():
        return True

    @staticmethod
    def transcribe(language, pcm):
        return "Synthetic release transcription"


class SyntheticProvider:
    id, name = "github", "GitHub"
    daily_cap = None

    def __init__(self):
        self.origin = ""
        self.codes = {}

    def authorize_url(self, state, verifier, nonce):
        return self.origin + "/authorize?" + urlencode({"state": state})

    def profile(self, code, verifier, nonce, user):
        from commonplace.social.providers import Profile

        email = self.codes.pop(code)
        return Profile(subject=email, emails=(email,), name="Release person")


@dataclass
class Instance:
    origin: str
    mail: Mailbox
    gateway: object


@contextmanager
def throwaway_instance(*, storage_limit=None, voice_limit=None):
    """An empty production gateway; never seed a user, token or content store.

    Plan sizes are fixture configuration, not usage counters. Quota tests still
    have to make real authenticated requests and exhaust the configured budget.
    """
    from commonplace import mailer, plans, urls, voice, webhooks
    from commonplace.control_db import ControlDB
    from commonplace.gateway import Gateway
    from commonplace.social import SignIn
    from commonplace.workers import WorkerSupervisor

    mailbox, provider = Mailbox(), SyntheticProvider()
    # Never inherit real social credentials or send email/voice to a provider.
    env = {
        "COMMONPLACE_SIGNUP": "open",
        "COMMONPLACE_PLANS": "on",
        "COMMONPLACE_ALLOWED_HOSTS": "127.0.0.1",
        "COMMONPLACE_RATE_LIMITS": "off",
        "COMMONPLACE_RULES_CHECK": "on",
        "COMMONPLACE_RULES_INJECT": "on",
        "COMMONPLACE_APP_URL": "http://127.0.0.1",
        "COMMONPLACE_BASE_URL": "http://127.0.0.1",
        "COMMONPLACE_AGENT_URL": "http://127.0.0.1",
        "COMMONPLACE_HANDOFF_ENABLED": "off",
    }
    for name in ("GOOGLE", "GITHUB", "APPLE", "X", "DISCORD"):
        for key in ("CLIENT_ID", "CLIENT_SECRET", "PRIVATE_KEY", "TEAM_ID", "KEY_ID"):
            env[f"COMMONPLACE_{name}_{key}"] = ""
    plan = replace(
        plans.FREE,
        storage_bytes=storage_limit if storage_limit is not None else plans.FREE.storage_bytes,
        transcription_seconds=(
            voice_limit if voice_limit is not None else plans.FREE.transcription_seconds
        ),
    )
    with TemporaryDirectory(prefix="commonplace-release-") as directory, ExitStack() as stack:
        stack.enter_context(patch.dict("os.environ", env))
        stack.enter_context(patch.object(plans, "FREE", plan))
        stack.enter_context(patch.dict(plans.PLANS, {"free": plan}))
        stack.enter_context(patch.object(mailer, "from_env", return_value=mailbox))
        stack.enter_context(patch.object(voice, "engine", return_value=SyntheticVoice()))
        db = ControlDB(str(Path(directory) / "control.sqlite3"))
        stack.callback(db.close)
        supervisor = WorkerSupervisor(directory)
        stack.callback(supervisor.stop_all)
        gateway = Gateway(db, supervisor)
        stack.callback(webhooks.stop, gateway)
        gateway.sign_in = SignIn(db=db, providers=[provider], signup=True)
        server = gateway.build_server("127.0.0.1", 0)
        stack.callback(server.server_close)
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        for name in ("APP_URL", "AGENT_URL", "SITE_URL"):
            stack.enter_context(patch.object(urls, name, origin))
        stack.enter_context(
            patch.dict(
                "os.environ", {"COMMONPLACE_APP_URL": origin, "COMMONPLACE_BASE_URL": origin}
            )
        )

        class ProviderHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # Authorization codes and email addresses stay in memory.

            def do_GET(self):
                state = parse_qs(urlsplit(self.path).query).get("state", [""])[0]
                body = (
                    '<form method="post"><label>Email<input name="email" type="email" '
                    'required></label><input type="hidden" name="state" value="'
                    + escape(state, quote=True)
                    + '"><button>Continue</button></form>'
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                values = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                code = secrets.token_urlsafe(24)
                provider.codes[code] = values["email"][0]
                self.send_response(302)
                self.send_header(
                    "Location",
                    origin
                    + "/app/auth/github/callback?"
                    + urlencode({"state": values["state"][0], "code": code}),
                )
                self.send_header("Content-Length", "0")
                self.end_headers()

        remote = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
        stack.callback(remote.server_close)
        provider.origin = f"http://127.0.0.1:{remote.server_address[1]}"
        for httpd in (remote, server):
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            stack.callback(thread.join, 5)
            stack.callback(httpd.shutdown)
        yield Instance(origin, mailbox, gateway)


def checked_json(status, body, method, path, expected=200):
    """Keep credentials and response bodies out of unexpected-status errors."""
    if status != expected:
        if status in (404, 405):
            raise Blocked(f"{method} {path.split('?')[0]} returned HTTP {status}")
        raise AssertionError(f"{method} {path.split('?')[0]} expected {expected}, got {status}")
    return body


@dataclass
class Result:
    name: str
    status: str = "BLOCKED"
    detail: str = "Prerequisite did not complete"
    screenshot: str = ""
    evidence_kind: str = "browser and protocol assertions"
    screenshots: list[str] = field(default_factory=list)


class Report:
    def __init__(self, names, *, revision=""):
        self.results = {name: Result(name) for name in names}
        self.revision = revision
        self.runner_error = ""

    @property
    def passed(self):
        return not self.runner_error and all(row.status == "PASS" for row in self.results.values())

    def payload(self):
        return {
            "passed": self.passed,
            "revision": self.revision,
            "runner_error": self.runner_error,
            "fixtures": ["captured mail", "synthetic GitHub identity", "synthetic voice decoder"],
            "real_social_providers": "NOT TESTED: no real provider credentials configured",
            "checks": [asdict(row) for row in self.results.values()],
        }

    def markdown(self):
        lines = [
            "# Fresh-user release gate",
            "",
            f"Verdict: {'PASS' if self.passed else 'FAIL'}",
            f"Revision: {self.revision or 'unknown'}",
            "",
        ]
        if self.runner_error:
            lines.extend([self.runner_error, ""])
        for row in self.results.values():
            lines.append(f"- **{row.status}** {row.name}: {row.detail}")
            lines.append(f"  Evidence: {row.evidence_kind}")
            images = list(
                dict.fromkeys(([row.screenshot] if row.screenshot else []) + row.screenshots)
            )
            for index, screenshot in enumerate(images, 1):
                lines.append(f"  [Screenshot {index}]({screenshot})")
            if not images:
                lines.append("  No screenshot; see the recorded assertions.")
        lines.extend(
            [
                "",
                "Mail delivery, GitHub identity and voice decoding use synthetic fixtures.",
                "Real social providers: NOT TESTED (no real provider credentials configured).",
                "Nonzero exit on any failed or blocked required check. No skipped checks count as PASS.",
            ]
        )
        return "\n".join(lines) + "\n"
