"""Shared browser helpers, the SKIPS dict and the layout scenarios."""

import json
import os
import re
from pathlib import Path

# Every case that may skip, with the plan's reason (documented SKIPS). Any other skip fails.
SKIPS = {
    "06c.row-outline.A-118": "8.3 no 'Linked in chat': A-118 has no chat row",
    "06c.pointer-outline.hover-A-118": "8.3 no 'Linked in chat': A-118 has no chat row",
    "06c.row-details.A-118": "8.3 no 'Linked in chat': A-118 has no chat row",
    "06f2.static-details.A-118": "8.3 no 'Linked in chat': A-118 has no chat row",
    "m11c.phone-row.A-118": "8.3 no 'Linked in chat': A-118 has no chat row",
    "04.tag-linked-in-chat.A-118": "8.3 no 'Linked in chat' tag",
    "05b.undo": "8.2 no Undo after Add card",
    "05.due": "8.5 no Due field",
    "04.details-due": "8.5 no Due field",
    "04.remind": "8.1 Remind hidden",
    "m9.remind-option": "8.1 Remind hidden",
    "06c.starts-after.A-135": "8.7 no dependency field",
    "11b.palette-starts-after": "8.7 no dependency field",
    "04.a121-in-doing": "8.6 an ask needs review status, A-121 sits in Review",
    "03d.delivered-at-time": "8.8 no delivery time source",
    "04.built-by-duration": "8.8 no build duration source",
    "10e.saved-key-name": "8.8 key names show as 'Saved key N'",
}


def out_dir(name):
    root = os.environ.get("CP_E2E_OUT")
    if not root:
        return None
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def append_log(name, record):
    path = out_dir(name)
    if path is not None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


CSP = ("Content Security Policy", "Refused to apply inline style", "Refused to execute")
PHONE = (390, 844)
TABLET = (768, 1024)
PUSH = (1024, 768)
DESKTOP = (1440, 900)


class Browser:
    """One browser context per page, signed in by cookie; console messages are kept."""

    def __init__(self, chromium, seed, test_id):
        self.chromium, self.seed, self.test_id = chromium, seed, test_id
        self.contexts, self.console = [], []

    def open(self, hash_="#/board/atlas", size=DESKTOP, *, user="maya", unread=True, clock=False):
        context = self.chromium.new_context(viewport={"width": size[0], "height": size[1]})
        # A missing control fails fast instead of waiting out Playwright's 30 s default.
        context.set_default_timeout(4000)
        context.set_default_navigation_timeout(10000)
        self.contexts.append(context)
        jar = self.seed.cookie if user == "maya" else self.seed.leon_cookie
        context.add_cookies([{**jar, "url": self.seed.origin}])
        if user == "maya":
            context.add_init_script(seen_script(self.seed, unread))
        page = context.new_page()
        if clock:
            page.clock.install()
        label = f"{hash_}@{size[0]}"
        page.on("console", lambda msg: self._console(label, msg.type, msg.text))
        page.on("pageerror", lambda err: self._console(label, "pageerror", str(err)))
        page.goto(f"{self.seed.origin}/app/{hash_}")
        ready(page)
        return page

    def _console(self, where, kind, text):
        if kind in ("error", "warning", "pageerror"):
            self.console.append(
                {"test": self.test_id, "page": where, "type": kind, "text": text[:500]}
            )

    def csp_violations(self):
        return [r for r in self.console if any(marker in r["text"] for marker in CSP)]

    def errors(self):
        return [r for r in self.console if r["type"] in ("error", "pageerror")]

    def close(self):
        for context in self.contexts:
            context.close()


def seen_script(seed, unread):
    """Seed the device (localStorage) seen ids before the app runs (only when absent)."""
    user, values = seed.maya, {}
    newest = max(seed.messages.values())
    for board in ("atlas", "harbor", "ledger", "field-notes"):
        values[board] = {"message": newest, "reply": 0 if board != "atlas" else 0}
    if unread:
        values["atlas"] = {"message": newest, "reply": seed.replies["quick"] - 1}
    else:
        values["atlas"] = {"message": newest, "reply": seed.replies["quick"]}
    entries = {
        f"cmmn.chat.seen.{_part(user)}.{_part(board)}": json.dumps(v) for board, v in values.items()
    }
    return (
        "(() => { const v = "
        + json.dumps(entries)
        + "; for (const [k, x] of Object.entries(v)) "
        + "{ try { if (localStorage.getItem(k) === null) localStorage.setItem(k, x); } "
        + "catch (e) {} } })();"
    )


def _part(value):
    from urllib.parse import quote

    return quote(str(value), safe="-_!~*'()").replace(".", "%2E")


def ready(page, timeout=8000):
    """The shell is drawn and the first screen has left its Loading state."""
    page.wait_for_selector(".shell, main.signin", timeout=timeout)
    page.wait_for_function(
        "() => !document.querySelector('#content .message') || "
        "!/Loading/.test(document.querySelector('#content').textContent)",
        timeout=timeout,
    )
    page.wait_for_timeout(150)


def settle(page, ms=200):
    page.wait_for_timeout(ms)


def card_selector(seed, cid):
    return f'.workbench-board .card[data-card="{seed.number(cid)}"]'


def chat_row(cid_number):
    return f'#chat [data-chat-card="{cid_number}"]'


STATE_JS = """() => {
  const chat = document.getElementById('chat');
  const panel = document.getElementById('card-panel');
  const a = document.activeElement;
  return {
    open: !!chat && !chat.hidden,
    ctx: !!panel && !panel.hidden,
    exp: [...document.querySelectorAll('[aria-controls="chat"]')]
      .filter((b) => b.getClientRects().length).map((b) => b.getAttribute('aria-expanded')).join(','),
    focus: a ? (a.id || a.tagName) : '',
    inChat: !!(a && a.closest('#chat')),
    composer: !!(a && a.closest('#chat') && a.matches('textarea, input')),
  };
}"""

DETAILS_JS = """() => {
  const sel = document.querySelector('.workbench-board .card.is-selected');
  const panel = document.getElementById('card-panel');
  const chat = document.getElementById('chat');
  const strip = panel && panel.querySelector('.from-chat');
  const a = document.activeElement;
  return {
    chat: !!chat && !chat.hidden,
    ctx: !!panel && !panel.hidden,
    ctxId: (panel && panel.querySelector('.detail-h .cid') || {}).textContent || null,
    ctxTitle: ((panel && panel.querySelector('.detail-h h2')) || {}).textContent?.trim() || null,
    selId: sel ? sel.querySelector('.cid').textContent : null,
    selTitle: sel ? sel.querySelector('h3').textContent.trim() : null,
    strip: !!strip && !strip.closest('[hidden]'),
    stripText: strip ? (strip.querySelector('.fc-text') || strip).textContent.trim() : '',
    focus: a ? a.tagName + (a.dataset && a.dataset.chatCard ? ':' + a.dataset.chatCard : '') : '',
    focusCard: a && a.dataset ? (a.dataset.chatCard || a.dataset.card || a.id || '') : '',
  };
}"""

# Layout scenarios: (name, hash, size class, setup step, design screen for compare.html).
# Setup steps are names understood by test_layout.setup().
SCENARIOS = [
    ("board", "#/board/atlas", "all", None, "04-board"),
    ("board-empty", "#/board/field-notes", "all", None, "03-board-empty"),
    ("board-chat", "#/board/atlas", "all", "open-chat", "06c-chat-answer"),
    ("board-card", "#/board/atlas", "all", "pick-a121", "06g-chat-board-pick"),
    ("board-add", "#/board/atlas", "all", "add-card", "05-add-card"),
    ("chat-no-answer", "#/board/ledger", "all", "open-chat", "06d-chat-error"),
    ("needs", "#/needs", "all", None, "07-needs-me"),
    ("needs-confirm", "#/needs", "all", "approve", "07c-approve-confirm"),
    ("memory", "#/memory?q=rate", "all", None, "09-memory"),
    ("settings", "#/board/atlas", "all", "settings", "10-settings"),
    ("palette", "#/board/atlas", "desktop", "palette", "11-command-palette"),
    ("shortcuts", "#/board/atlas", "desktop", "shortcuts", "12-shortcuts"),
    ("create-project", "#/board/atlas", "desktop", "create-project", "02-create-project"),
    ("legacy-rules", "#/rules", "legacy", None, None),
    ("legacy-console", "#/console", "legacy", None, None),
    ("legacy-space", "#/space/atlas", "legacy", None, None),
    ("legacy-decide", "#/decide", "legacy", None, None),
    ("legacy-workspace-settings", "#/settings", "legacy", None, None),
]
WIDTHS = [(1440, 900), (1366, 768), (1280, 800), (1024, 768), (768, 1024), (390, 844)]
PHONE_EXTRA = [(320, 640)]
SHOT_WIDTHS = {1440: 900, 1024: 768, 768: 1024, 390: 844}


def renders():
    out = []
    for name, _hash, kind, _setup, _design in SCENARIOS:
        sizes = WIDTHS if kind != "desktop" else [s for s in WIDTHS if s[0] >= 768]
        if kind in ("all", "legacy"):
            sizes = sizes + PHONE_EXTRA
        out += [f"{name}@{w}x{h}" for w, h in sizes]
    return out


def scenario(name):
    return next(s for s in SCENARIOS if s[0] == name)


def parse_render(render):
    name, size = render.split("@")
    w, h = (int(x) for x in size.split("x"))
    return scenario(name), (w, h)


def norm(text):
    return re.sub(r"\s+", " ", text or "").strip()
