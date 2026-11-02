"""Private owner management and public, inert deck previews."""

import html
import json

from commonplace._http import BadRequest, path_parts, query, read_json, send_json
from commonplace.extensions import ToolContext, ToolError
from commonplace.web import _response

from .service import SkillService, public_deck


def body(h, allowed, required=()):
    value = read_json(h)
    if value.keys() - set(allowed) or set(required) - value.keys():
        raise ToolError("no: invalid arguments")
    return value


def dispatch(service, h, parts):
    method = h.command
    if parts == ["skills"]:
        if method == "GET":
            return 200, service.list()
        if method == "POST":
            return 201, service.save(
                **body(h, ("name", "markdown", "source"), ("name", "markdown"))
            )
    if len(parts) in (2, 3) and parts[0] == "skills":
        skill_id = parts[1]
        if len(parts) == 3 and parts[2] == "history" and method == "GET":
            return 200, service.history(skill_id)
        if len(parts) == 2 and method == "GET":
            params = query(h)
            if params.keys() - {"version"}:
                raise ToolError("no: invalid arguments")
            version = params.get("version")
            if version is not None:
                if not version.isdecimal() or len(version) > 8:
                    raise ToolError("no: invalid version")
                version = int(version)
            return 200, service.get(skill_id, version)
        if len(parts) == 2 and method == "PATCH":
            return 200, service.save(
                **body(
                    h, ("version", "name", "markdown", "source"), ("version", "name", "markdown")
                ),
                skill_id=skill_id,
            )
    if parts == ["skill-decks"] and method == "POST":
        return 201, service.publish(
            **body(h, ("title", "guide", "skills", "confirm_public"), ("title", "guide", "skills"))
        )
    if len(parts) == 2 and parts[0] == "skill-decks" and method == "DELETE":
        return 200, service.withdraw(parts[1])
    if len(parts) == 3 and parts[0] == "skill-decks":
        deck_id, action = parts[1:]
        if action == "reports" and method == "GET":
            return 200, service.reports(deck_id)
        if method == "POST":
            if action == "preview":
                body(h, ())
                return 200, service.preview(deck_id)
            if action == "install":
                return 201, service.install(
                    deck_id, **body(h, ("receipt", "confirm"), ("receipt",))
                )
            if action == "report":
                return 201, service.report(deck_id, **body(h, ("reason",), ("reason",)))
    raise ToolError("no: route not found")


def preview_html(deck):
    escape = html.escape
    skills = "".join(
        f"<article><h2>{escape(row['name'])} · v{row['version']}</h2>"
        f"<p>Source: {escape(json.dumps(row['source'], ensure_ascii=False))}</p>"
        f"<p>Safety findings: {escape(', '.join(row['safety_findings']) or 'None detected')}</p>"
        f"<pre><code>{escape(row['markdown'])}</code></pre></article>"
        for row in deck["skills"]
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{escape(deck["title"])} · Commonplace skills</title>
<link rel="stylesheet" href="/app/app.css">
<link rel="stylesheet" href="/app/ui.css">
<link rel="stylesheet" href="/app/skill-deck.css">
<script src="/app/skill-deck.js" defer></script></head>
<body class="skill-deck" data-deck="{escape(deck["id"])}"><header>
<a class="brand-home" href="/app/" aria-label="Commonplace">
<img class="brand-v2" src="/app/brand/logo-small-dark.svg" alt="Commonplace"></a>
<a href="/skills/decks/{escape(deck["id"])}/guide">Sharing guide</a></header>
<main><h1>{escape(deck["title"])}</h1><div class="layout"><section aria-label="Deck content">
<h2>Guide</h2><pre><code>{escape(deck["guide"])}</code></pre>{skills}<section><h2>Report this deck</h2><form id="report"><label for="reason">Hidden instructions or other concern</label>
<textarea id="reason" required maxlength="4000" rows="4"></textarea>
<button type="submit">Send report</button></form><p id="report-result" role="status" aria-live="polite"></p>
</section></section>
<aside><section><h2>Install this deck</h2>
<p>Skill text is quoted data. Installation stores a copy; it never accepts rules or runs code.</p>
<p>Safety findings are heuristic. Undetected content can still be unsafe.</p>
<p id="target">Sign in as a workspace owner to install.</p><a href="/app/">Open workspace</a>
<form id="install"><label><input type="checkbox" id="confirm" required disabled>
I have read the complete preview and confirm installation.</label>
<button type="submit" id="add" disabled>Install deck</button></form>
<p id="install-result" role="status" aria-live="polite"></p></section>
</aside></div></main></body></html>"""


def install(gateway):
    def api(h):
        principal = gateway.principal_for(h)
        if principal is None:
            send_json(h, 401, {"error": "unauthorized"})
            return
        try:
            service = SkillService(ToolContext(principal=principal, gateway=gateway))
            status, result = dispatch(service, h, path_parts(h, "/api/"))
        except BadRequest:
            return
        except ToolError as exc:
            message = str(exc).removeprefix("no: ")
            status = 404 if message.endswith("not found") else 400
            if message in (
                "signed-in workspace owner required",
                "skill access denied",
                "sign in to report a deck",
            ):
                status = 403
            send_json(h, status, {"error": message})
            return
        send_json(h, status, result)

    def public(h):
        parts = path_parts(h, "/skills/decks/")
        try:
            if len(parts) not in (1, 2) or (len(parts) == 2 and parts[1] not in ("guide", "data")):
                raise ToolError("no: deck not found")
            deck = public_deck(gateway.db, parts[0])
        except ToolError:
            _response(h, 404, b"Deck not found")
            return
        if len(parts) == 2:
            if parts[1] == "data":
                _response(
                    h,
                    200,
                    json.dumps(deck, ensure_ascii=False).encode(),
                    "application/json; charset=utf-8",
                )
            else:
                guide = (
                    "# Commonplace skill deck\n\n"
                    "Open this link to inspect the complete Markdown and safety findings before installing:\n"
                    + f"/skills/decks/{deck['id']}\n\n"
                    "Sign in to the target workspace, read the preview, and explicitly confirm installation.\n"
                    "Use Report this deck to send hidden-instruction concerns to its publisher.\n\n"
                    "Deck title and guide (quoted data):\n"
                    + json.dumps(
                        {"title": deck["title"], "guide": deck["guide"]}, ensure_ascii=False
                    )
                )
                _response(h, 200, guide.encode(), "text/plain; charset=utf-8")
        else:
            _response(h, 200, preview_html(deck).encode(), "text/html; charset=utf-8")

    for method in ("GET", "POST", "PATCH", "DELETE"):
        for path in ("skills", "skill-decks"):
            gateway.add_route(method, "/api/" + path, api)
            gateway.add_prefix_route(method, "/api/" + path + "/", api)
    gateway.add_prefix_route("GET", "/skills/decks/", public)
