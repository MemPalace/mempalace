"""Frozen chat contracts
control records never contain conversation text."""

import re
from dataclasses import dataclass

from commonplace import ratelimit

PROVIDERS = ("none", "openai_compatible", "anthropic")
DEFAULT_MODELS = {"anthropic": "claude-haiku-5-5"}
RECEIPT_CODES = ("forwarded", "forwarded_no_wake", "no_orchestrator", "orchestrator_gone")
HANDOFF_CODES = ("filed", "file_failed", "claimed", "no_access")
HANDOFF_STATES = ("waiting", "filing", "claimed", "filed", "failed")
JOB_CODES = (
    "ok",
    "superseded",
    "stale",
    "no_access",
    "off",
    "key_missing",
    "key_not_allowed",
    "daily_limit",
    "user_daily_limit",
    "rate_limit",
    "busy",
    "timeout",
    "http_error",
    "bad_response",
    "interrupted",
)
MODEL_TIMEOUT_S = 10
MAX_OUTPUT_TOKENS = 300
MAX_INPUT_CHARS = 12_000
MAX_REPLY_CHARS = 2_000
MAX_RESPONSE_BYTES = 262_144
ANTHROPIC_VERSION = "2023-06-01"
CONTEXT_MESSAGES = 10
STALE_AFTER_S = 600
MAX_PENDING_PER_WORKSPACE = 20
DAILY_LIMIT_DEFAULT = 200
DAILY_LIMIT_MAX = 2000
USER_DAILY_LIMIT_DEFAULT = 50
ACK_WAIT_S = 300
HANDOFF_TITLE_CHARS = 200
LINKED_CARDS_MAX = 20
USER_RATE = ratelimit.Limit("chat-front", 20, 60)
KEY_REF = re.compile(r"env:COMMONPLACE_CHAT_KEY_[A-Z0-9_]{1,40}")
KEY_WORKSPACES_ENV = "COMMONPLACE_CHAT_KEY_WORKSPACES"  # comma-separated workspace ids
KEY_HOSTS_ENV = "COMMONPLACE_CHAT_KEY_HOSTS"  # comma-separated hostnames (openai_compatible)
NOT_WORK_WORDS = frozenset(
    "so much very there morning afternoon evening night good hi hello hey thanks thank you ok okay cool great nice yes no yep nope bye cheers hoi hallo dank je bedankt top prima ja nee oké".split()
)
STATUS_PREFIXES = (
    "status",
    "progress",
    "update",
    "news",
    "any news",
    "how is",
    "how's",
    "is the",
    "did the",
    "where is",
    "what's the status",
    "what is the status",
    "where are we",
    "any update",
    "is it done",
    "hoe staat",
    "wat is de status",
    "is het klaar",
)
RECEIPT_COPY = {
    "forwarded": {
        role: "Passed to {name}, this project's orchestrator ({status}). Its replies and card changes appear here."
        for role in ("owner", "member")
    },
    "forwarded_no_wake": {
        role: "Passed to {name}, this project's orchestrator ({status}). {name} has no wake-up hook, so it sees this on its next check."
        for role in ("owner", "member")
    },
    "no_orchestrator": {
        "owner": "This project's orchestrator isn't connected yet. Use Connect above.",
        "member": "This project's orchestrator isn't connected yet. Ask the workspace owner to connect it.",
    },
    "orchestrator_gone": {
        "owner": "The orchestrator for this project is no longer connected. Use Connect above to connect it again.",
        "member": "The orchestrator for this project is no longer connected. Ask the workspace owner to connect it again.",
    },
    "_handoff": {
        role: "If nobody picks this up within {wait} minutes, I'll put it on the board as a card for you."
        for role in ("owner", "member")
    },
}
HANDOFF_COPY = {
    "filed": "Nobody picked this up within {wait} minutes. I put it on the board as card #{number} for {name}.",
    "filed_no_orchestrator": "I put this on the board as card #{number}. Ask the workspace owner to choose an orchestrator.",
    "file_failed": "I couldn't put this on the board. Please ask the workspace owner for help.",
}


class ChatDenied(Exception):
    pass


class ModelError(Exception):
    def __init__(self, code: str):
        if code not in ("timeout", "http_error", "bad_response"):
            raise ValueError("Invalid model error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FrontSettings:
    workspace_id: str
    enabled: bool
    provider: str
    model: str
    base_url: str | None
    key_ref: str | None
    daily_limit: int
    user_daily_limit: int
    updated_by: str | None
    updated_at: str | None

    def public(self) -> dict:
        from commonplace.chat.settings import resolve_key

        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "key_set": bool(resolve_key(self)),
            "daily_limit": self.daily_limit,
            "user_daily_limit": self.user_daily_limit,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class OrchestratorView:
    actor: str
    connection_id: str | None
    name: str
    visible: bool
    status: str | None
    last_seen: str | None
    wakeable: bool

    def public(self) -> dict:
        return {
            "name": self.name,
            "visible": self.visible,
            "status": self.status,
            "last_seen": self.last_seen,
            "wakeable": self.wakeable,
            "console": "#/console" if self.visible else None,
        }


@dataclass(frozen=True)
class Reply:
    id: int
    board: str
    message_id: int
    for_user: str
    kind: str
    code: str
    text: str
    redacted: tuple[str, ...]
    model: str | None
    at: str
    task: int | None = None

    def public(self) -> dict:
        return {
            "id": self.id,
            "message_id": self.message_id,
            "kind": self.kind,
            "code": self.code,
            "text": self.text,
            "redacted": list(self.redacted),
            "model": self.model,
            "at": self.at,
            "task": self.task,
        }


@dataclass(frozen=True)
class Job:
    workspace_id: str
    board: str
    message_id: int
    user_id: str
    state: str
    code: str
    called: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Handoff:
    workspace_id: str
    board: str
    message_id: int
    user_id: str
    coordinator: str | None
    state: str
    code: str
    task: int | None
    due_at: str
    created_at: str
    updated_at: str
