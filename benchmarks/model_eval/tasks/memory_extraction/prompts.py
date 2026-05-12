"""Memory extraction prompts. JSON output."""
from __future__ import annotations


SYSTEM = """You extract memory-worthy items from agent conversations. Return ONLY valid JSON:

{"memories": [{"type": "...", "content": "..."}]}

Memory types: decision, preference, fact, opinion, commitment.

- decision: a choice made between alternatives
- preference: a stated like/dislike or way-of-doing
- fact: an objective piece of information about a person/project/world
- opinion: a subjective judgment
- commitment: a promise, plan, or scheduled action

Only include items that would be useful to remember. Skip pleasantries, greetings, and generic discussion."""


def build_user(text: str) -> str:
    return f"""Text:
{text}

Extract memory items."""
