"""Frozen Phase A project contracts."""

import re
from dataclasses import asdict, dataclass

SEAT_CLIENT = "commonplace-orchestrator"
SEAT_PREFIX = "orchestrator:"
TOKEN_DAYS = 90
SEAT_IDLE_S = 86400
# main = orchestrator, worker = builder; reviewer and gate are their own choices.
MODEL_ROLES = ("main", "worker", "reviewer", "gate")
ESCALATABLE_ROLES = ("worker", "reviewer")  # roles an escalation rule may swap
MAX_ESCALATION_LABELS = 10
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}")
# Every role whose choices belong in the model list.
CATALOG_ROLES = tuple(dict.fromkeys((*MODEL_ROLES, "reviewer", "gate")))
PROVIDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
MODEL = re.compile(r"[A-Za-z0-9._:/-]{1,120}")


class ProjectDenied(Exception):
    """The caller cannot configure this project."""


@dataclass(frozen=True)
class Seat:
    workspace_id: str
    board: str
    role: str
    name: str
    connection_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ModelChoice:
    provider: str
    model: str

    def public(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MainView:
    role: str
    name: str
    source: str
    connected: bool
    status: str | None
    wakeable: bool
    connectable: bool
    seen: bool

    def public(self) -> dict:
        return asdict(self)
