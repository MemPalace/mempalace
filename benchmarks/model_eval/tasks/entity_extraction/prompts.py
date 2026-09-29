"""Entity extraction prompts. Output is JSON; we use json_mode=True."""
from __future__ import annotations


SYSTEM = """You extract entities from text. Return ONLY valid JSON in this exact shape:

{"entities": [{"name": "...", "type": "..."}]}

Entity types are limited to: person, project, place, organization.

Do not invent entities not in the text. Do not include common nouns or generic concepts. Only proper-noun entities."""


def build_user(text: str) -> str:
    return f"""Text:
{text}

Extract all entities."""
