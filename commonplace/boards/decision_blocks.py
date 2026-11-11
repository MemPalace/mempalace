"""Structured decision blocks: an ask whose options carry a detail line and a text preview.

A plain ask offers a list of labels. A structured ask offers the same labels, and any option
may add a short ``detail`` and a plain-text ``preview`` (a mockup, a snippet, sample copy) that
the app shows side by side. The labels stay the only thing a person can answer with, so the
decide path and its refusal of options the asker did not offer are unchanged.
"""

import json

from commonplace.extensions import ToolError

DETAIL_LIMIT = 280
PREVIEW_LIMIT = 1200
PREVIEW_LINES = 24
KEYS = {"label", "detail", "preview"}
REFUSAL = (
    "no: a structured option is a label or {label, detail, preview}: 2 to 6 distinct labels, "
    f"detail up to {DETAIL_LIMIT} characters, preview up to {PREVIEW_LIMIT} characters and "
    f"{PREVIEW_LINES} lines of plain text"
)

# The MCP input shape of one option: a plain label or a structured block.
OPTION_SCHEMA = {
    "anyOf": [
        {"type": "string"},
        {
            "type": "object",
            "properties": {
                "label": {"type": "string", "maxLength": 120},
                "detail": {"type": "string", "maxLength": DETAIL_LIMIT},
                "preview": {"type": "string", "maxLength": PREVIEW_LIMIT},
            },
            "required": ["label"],
            "additionalProperties": False,
        },
    ]
}
OPTIONS_SCHEMA = {"type": "array", "maxItems": 6, "items": OPTION_SCHEMA}


def _text(item, key, limit):
    value = item.get(key)
    if value is None and key not in item:
        return None
    printable = isinstance(value, str) and all(c in "\n\t" or c.isprintable() for c in value)
    if not printable or not value.strip() or len(value) > limit:
        raise ToolError(REFUSAL)
    return value


def parse(options):
    """Split offered options into ``(labels, stored blocks or None)``.

    A list of plain labels, and anything that is not a list, passes through untouched for the
    existing option checks. A list with at least one structured option is checked here.
    """
    if not isinstance(options, list) or all(isinstance(item, str) for item in options):
        return options, None
    from commonplace.boards.service import validate_choice

    if not 2 <= len(options) <= 6:
        raise ToolError(REFUSAL)
    blocks = []
    for item in options:
        if isinstance(item, str):
            item = {"label": item}
        if not isinstance(item, dict) or not set(item) <= KEYS or "label" not in item:
            raise ToolError(REFUSAL)
        block = {"label": validate_choice(item["label"], "option")}
        detail = _text(item, "detail", DETAIL_LIMIT)
        preview = _text(item, "preview", PREVIEW_LIMIT)
        if preview is not None and len(preview.splitlines()) > PREVIEW_LINES:
            raise ToolError(REFUSAL)
        if detail is not None:
            block["detail"] = detail
        if preview is not None:
            block["preview"] = preview
        blocks.append(block)
    labels = [block["label"] for block in blocks]
    if len({label.strip().casefold() for label in labels}) != len(labels):
        raise ToolError(REFUSAL)
    return labels, json.dumps(blocks, ensure_ascii=False, separators=(",", ":"))


def public(row):
    """The blocks of the open ask, one per offered label in order; empty for a plain ask."""
    stored = row["ask_blocks"] if "ask_blocks" in row.keys() else None
    if not stored:
        return []
    blocks = json.loads(stored)
    offered = json.loads(row["ask_options"] or "[]")
    return blocks if [block["label"] for block in blocks] == offered else []


def lines(card):
    """Text rows for an agent reading the card: each option's detail and preview."""
    rows = []
    for block in card.get("ask_blocks") or []:
        if "detail" in block:
            rows.append(f"  {block['label']}: {block['detail']}")
        if "preview" in block:
            rows.append(f"  {block['label']} preview:")
            rows.extend(f"    {text}" for text in block["preview"].splitlines())
    return rows
