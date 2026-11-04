"""Shared console values; this module has no console dependencies."""

from dataclasses import dataclass


class ConsoleDenied(Exception):
    """The caller cannot change this agent's console settings."""


@dataclass(frozen=True)
class AgentView:
    id: str
    name: str
    client: str
    parent_id: str | None
    user_id: str
    own: bool
    recording: bool | None
    last_used_at: str | None
