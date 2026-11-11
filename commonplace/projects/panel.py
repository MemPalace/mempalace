"""Project panel: a few highlight lines and action links a project declares as data.

The document is untrusted text from a repo, an agent or an owner. It is checked strictly
(known keys only, hard size limits, plain text, http/https links) before it is stored, and
the app renders it as text, never as markup.

Format ``commonplace.panel/1`` (JSON, kept in the repo as ``.commonplace/panel.json``)::

    {
      "format": "commonplace.panel/1",
      "title": "Atlas",                                   (optional, 60 characters)
      "highlights": ["Beta ships Friday", "3 open risks"],  (at most 6 lines, 200 characters)
      "links": [{"label": "Runbook", "url": "https://example.com/runbook"}]  (at most 6)
    }
"""

import json
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlsplit

FORMAT = "commonplace.panel/1"
MAX_BYTES = 4096
MAX_TITLE = 60
MAX_HIGHLIGHTS = 6
MAX_HIGHLIGHT = 200
MAX_LINKS = 6
MAX_LABEL = 40
MAX_URL = 500
LINK_SCHEMES = ("http", "https")

_TAG = re.compile(r"<\s*[A-Za-z/!?]")
_SCRIPT_URL = re.compile(r"(?i)\b(?:javascript|vbscript|data)\s*:")
_KEYS = {"format", "title", "highlights", "links"}


class PanelInvalid(ValueError):
    """The panel document is refused; the message says what to change."""

    code = "invalid_panel"
    status = 400


class PanelTooLarge(PanelInvalid):
    code = "panel_too_large"
    status = 413


def _text(value, where, limit, *, required=True):
    if not isinstance(value, str):
        raise PanelInvalid(f"{where} must be text")
    value = value.strip()
    if required and not value:
        raise PanelInvalid(f"{where} must not be empty")
    if len(value) > limit:
        raise PanelInvalid(f"{where} is longer than {limit} characters")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Zl", "Zp") for c in value):
        raise PanelInvalid(f"{where} must be plain text without control characters")
    if _TAG.search(value):
        raise PanelInvalid(f"{where} must be plain text: HTML and script are not allowed")
    return value


def safe_url(value, where="link url"):
    """Return the URL when it is an absolute http(s) link, else raise PanelInvalid."""
    if not isinstance(value, str):
        raise PanelInvalid(f"{where} must be text")
    if len(value) > MAX_URL:
        raise PanelInvalid(f"{where} is longer than {MAX_URL} characters")
    if value != value.strip() or any(
        c.isspace() or unicodedata.category(c) in ("Cc", "Cf") for c in value
    ):
        raise PanelInvalid(f"{where} must not contain spaces or control characters")
    try:
        parts = urlsplit(value)
        host, _ = parts.hostname, parts.port
    except ValueError:
        raise PanelInvalid(f"{where} is not a valid web address") from None
    if parts.scheme.lower() not in LINK_SCHEMES:
        raise PanelInvalid(f"{where} must start with http:// or https://")
    if not host or parts.username is not None or parts.password is not None:
        raise PanelInvalid(f"{where} needs a host and no user name or password")
    if "\\" in value or "<" in value or ">" in value or '"' in value:
        raise PanelInvalid(f"{where} must not contain markup or backslashes")
    return value


def validate(doc):
    """Return the normalized panel for a decoded document, or raise PanelInvalid."""
    if not isinstance(doc, dict):
        raise PanelInvalid("the panel must be a JSON object")
    if doc.get("format") != FORMAT:
        raise PanelInvalid(f'format must be "{FORMAT}"')
    unknown = sorted(set(doc) - _KEYS)
    if unknown:
        raise PanelInvalid("unknown field: " + ", ".join(str(k)[:40] for k in unknown))
    panel = {"format": FORMAT}
    if "title" in doc:
        panel["title"] = _text(doc["title"], "title", MAX_TITLE)
    highlights = doc.get("highlights", [])
    if not isinstance(highlights, list):
        raise PanelInvalid("highlights must be a list of text lines")
    if len(highlights) > MAX_HIGHLIGHTS:
        raise PanelInvalid(f"at most {MAX_HIGHLIGHTS} highlights are allowed")
    panel["highlights"] = [
        _text(line, f"highlight {n}", MAX_HIGHLIGHT) for n, line in enumerate(highlights, 1)
    ]
    links = doc.get("links", [])
    if not isinstance(links, list):
        raise PanelInvalid("links must be a list")
    if len(links) > MAX_LINKS:
        raise PanelInvalid(f"at most {MAX_LINKS} links are allowed")
    panel["links"] = []
    for n, link in enumerate(links, 1):
        if not isinstance(link, dict) or set(link) != {"label", "url"}:
            raise PanelInvalid(f'link {n} must have exactly "label" and "url"')
        label = _text(link["label"], f"link {n} label", MAX_LABEL)
        if _SCRIPT_URL.search(label):
            raise PanelInvalid(f"link {n} label must be plain text")
        panel["links"].append({"label": label, "url": safe_url(link["url"], f"link {n} url")})
    if not panel["highlights"] and not panel["links"]:
        raise PanelInvalid("the panel needs at least one highlight or link")
    if len(json.dumps(panel, ensure_ascii=False).encode()) > MAX_BYTES:
        raise PanelTooLarge(f"the panel is larger than {MAX_BYTES} bytes")
    return panel


def parse(text):
    """Validate the text of a panel file (as kept in a repo)."""
    raw = text if isinstance(text, bytes) else str(text).encode()
    if len(raw) > MAX_BYTES * 2:
        raise PanelTooLarge(f"the panel is larger than {MAX_BYTES} bytes")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise PanelInvalid("the panel is not valid JSON") from None
    return validate(doc)


def get(conn, board_id):
    """The stored panel for a board, or None; reading never creates the table."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_panels'"
    ).fetchone():
        return None
    row = conn.execute(
        "SELECT doc, updated_at, updated_by FROM project_panels WHERE board_id=?", (board_id,)
    ).fetchone()
    if row is None:
        return None
    return {"panel": json.loads(row["doc"]), "updated_at": row["updated_at"]}


def put(conn, board_id, doc, actor):
    """Validate and store a panel; ``None`` clears it. Returns what GET would return."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS project_panels ("
        "board_id INTEGER PRIMARY KEY REFERENCES boards(id), doc TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, updated_by TEXT)"
    )
    if doc is None:
        conn.execute("DELETE FROM project_panels WHERE board_id=?", (board_id,))
        return None
    panel = validate(doc)
    conn.execute(
        "INSERT OR REPLACE INTO project_panels(board_id, doc, updated_at, updated_by) "
        "VALUES (?,?,?,?)",
        (
            board_id,
            json.dumps(panel, ensure_ascii=False),
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            actor,
        ),
    )
    return get(conn, board_id)
