"""Room classification prompts. Two modes:

- closed: room list provided, model picks one or "other"
- open: no list, model invents a slug
"""
from __future__ import annotations


CLOSED_SYSTEM = """You are a room classifier for an AI agent's memory palace. Given a session excerpt and a list of rooms, choose the best-fitting room.

Respond with EXACTLY one room slug from the list, copied verbatim (including any "/" or "-" characters), or the literal word "other" if no room fits well. No explanation, no quotes, no extra text."""


OPEN_SYSTEM = """You are a room classifier for an AI agent's memory palace. Given a session excerpt and the agent's identity, INVENT a short room slug that captures the session's topic.

Respond with EXACTLY one slug: lowercase, hyphenated, no spaces, no punctuation, no quotes. Example formats: "project-alpha", "code-review", "daily-logs"."""


def build_closed_user(agent: str, session_summary: str, rooms: list[str]) -> str:
    rooms_block = "\n".join(f"- {r}" for r in rooms)
    return f"""Agent: {agent}

Available rooms:
{rooms_block}

Session excerpt:
{session_summary}

Room:"""


def build_open_user(agent: str, session_summary: str) -> str:
    return f"""Agent: {agent}

Session excerpt:
{session_summary}

Room slug:"""
