"""The design's keytest, ported to the real app (one case per key; see scenarios.SKIPS).

Each case replays its own steps from a fresh page, so a failure names one behaviour.
Selectors follow the shipped markup; expectations follow the approved design and the
plan. A case that cannot find a drawn control fails with the control's name.
"""

import json
import re

import pytest

from tests.workbench_e2e.scenarios import (
    DESKTOP,
    DETAILS_JS,
    PHONE,
    PUSH,
    SKIPS,
    STATE_JS,
    TABLET,
    card_selector,
    norm,
    settle,
)

CASES = {}


def case(key):
    def register(fn):
        assert key not in CASES, key
        CASES[key] = fn
        return fn

    return register


def ok(cond, label, info):
    assert cond, f"{label}: {json.dumps(info, ensure_ascii=False, default=str)}"


def st(p):
    return p.evaluate(STATE_JS)


def det(p):
    return p.evaluate(DETAILS_JS)


def row(seed, cid):
    return f'#chat [data-chat-card="{seed.number(cid)}"]'


def composer(p):
    return p.evaluate(
        "() => !!document.activeElement && !!document.activeElement.closest('#chat') && "
        "document.activeElement.matches('textarea, input')"
    )


def tap(p, selector):
    """Click like a pointer; if something covers the control, name what covers it."""
    target = p.locator(selector).first
    try:
        target.click(timeout=3000)
    except Exception as exc:
        if type(exc).__name__ != "TimeoutError":
            raise
        hit = target.evaluate(
            """(el) => { const r = el.getBoundingClientRect();
          const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
          return { box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
            covered_by: top ? top.tagName.toLowerCase() + (top.id ? '#' + top.id : '') +
              (top.className && typeof top.className === 'string' ? '.' + top.className.split(' ')[0] : '') : null }; }"""
        )
        raise AssertionError(f"{selector} cannot be clicked: {json.dumps(hit)}") from None


def open_chat(p):
    p.click("#chat-toggle")
    p.wait_for_selector("#chat:not([hidden]) .chat-stream", timeout=5000)
    settle(p, 300)


def pick_card(p, seed, cid):
    """Select a board card the way a person does: click its title."""
    p.click(f"{card_selector(seed, cid)} h3")
    p.wait_for_selector("#card-panel:not([hidden]) .detail-h", timeout=5000)
    settle(p, 250)


def all_expanded(state, value):
    values = [v for v in state["exp"].split(",") if v]
    return len(values) >= 1 and all(v == value for v in values)


EXPANDED_ALL_JS = (
    "() => [...document.querySelectorAll('[aria-controls=\"chat\"]')]"
    ".map((b) => b.getAttribute('aria-expanded'))"
)


def outlined(p):
    """Board cards a chat row points at: the drawn dashed outline (or is-linked)."""
    return p.evaluate(
        "() => [...document.querySelectorAll('.workbench-board .card[data-card]')]"
        ".filter((c) => c.classList.contains('is-linked') || "
        "getComputedStyle(c).outlineStyle === 'dashed')"
        ".map((c) => c.querySelector('.cid').textContent).join(',')"
    )


def modal_snapshot(p):
    return p.evaluate(
        """() => {
      const vis = (el) => el && el.getClientRects().length > 0 &&
        getComputedStyle(el).visibility !== 'hidden';
      const modals = [...document.querySelectorAll('[aria-modal="true"], dialog[open]')]
        .filter(vis).length;
      const scrims = [...document.querySelectorAll('.scrim, .tablet-scrim, .chat-scrim')]
        .filter(vis).length;
      const a = document.activeElement;
      return { modals, scrims, inert: document.querySelectorAll('[inert]').length,
        focus: a ? (a.id || a.tagName) : '' };
    }"""
    )


def trapped(p, scope='[aria-modal="true"], dialog[open]', presses=25):
    for _ in range(presses):
        p.keyboard.press("Tab")
        inside = p.evaluate(
            "(s) => !!document.activeElement && !!document.activeElement.closest(s)", scope
        )
        if not inside:
            return False
    return True


def dialog_case(p, opener, first, back, scope='[aria-modal="true"], dialog[open]'):
    """Initial focus, Tab trap, Esc, focus return (keytest.cjs:24-50)."""
    p.click(opener)
    settle(p, 400)
    initial = p.evaluate(
        "(sel) => !!document.activeElement && document.activeElement.matches(sel)", first
    )
    kept = trapped(p, scope)
    p.keyboard.press("Escape")
    settle(p, 200)
    after = modal_snapshot(p)
    back_ok = p.evaluate(
        "(sel) => !!document.activeElement && document.activeElement.matches(sel)", back
    )
    ok(
        initial
        and kept
        and after["modals"] == 0
        and after["scrims"] == 0
        and after["inert"] == 0
        and back_ok,
        "dialog",
        {"initial": initial, "trapped": kept, **after, "want_focus": back},
    )


# ---- Dialogs (keytest.cjs:6-50) ---------------------------------------------------------


@case("02.dialog")
def _(ui, seed):
    p = ui.open("#/board/atlas")
    dialog_case(p, "#new-project", '[role="dialog"] input, dialog[open] input', "#new-project")


@case("03c.dialog")
def _(ui, seed):
    p = ui.open("#/board/field-notes")
    dialog_case(
        p,
        '.empty-strip [data-action="connect-agent"]',
        '[aria-modal="true"] :is(input, button), dialog[open] :is(input, button)',
        '[data-action="connect-agent"]',
    )


def _connect_dialog(p):
    """03c: Connect an agent from the empty board. Returns whether that was a dialog.

    The shipped app opens project settings first; its Connect agent button then opens
    the setup dialog, so the later phases (03c2-03e) stay testable either way.
    """
    p.click('.empty-strip [data-action="connect-agent"]')
    settle(p, 500)
    direct = p.evaluate(
        "() => !!document.querySelector('[aria-modal=\"true\"]:not([hidden]), dialog[open]')"
    )
    if direct:
        # 03c's primary action makes the setup in the same dialog (03c2).
        p.locator("dialog[open] .btn.primary, [aria-modal='true'] .btn.primary").first.click()
        p.wait_for_selector("dialog[open] [data-project-token]", timeout=5000)
    else:
        p.locator("button", has_text="Connect agent").first.click()
    p.wait_for_selector("dialog[open], [aria-modal='true']:not([hidden])", timeout=5000)
    settle(p, 500)
    return direct


@case("03c2.dialog")
def _(ui, seed):
    p = ui.open("#/board/field-notes")
    _connect_dialog(p)
    waiting = p.evaluate(
        "() => /Waiting for the agent/.test(document.querySelector('dialog[open]')?.textContent || '')"
    )
    first = p.evaluate(
        "() => !!document.activeElement && !!document.activeElement.closest('dialog[open], [aria-modal=\"true\"]')"
        " && document.activeElement.matches('[data-project-token], [data-project-copy], .snippet button')"
    )
    kept = trapped(p)
    p.keyboard.press("Escape")
    settle(p, 200)
    after = modal_snapshot(p)
    ok(
        waiting and first and kept and after["modals"] == 0,
        "03c2 waiting: the setup has focus, Tab stays inside, Esc closes",
        {"waiting": waiting, "first": first, "trapped": kept, **after},
    )


@case("03d.dialog")
def _(ui, seed):
    p = ui.open("#/board/field-notes")
    _connect_dialog(p)
    token = p.evaluate("() => (document.querySelector('[data-project-token]') || {}).value || ''")
    ok(bool(token), "03c2 shows the one-time setup", {"token_field": bool(token)})
    seed.hub.rpc(token, "tools/list")
    del token
    done = p.wait_for_function(
        "() => /Orchestrator connected|connected/i.test("
        "(document.querySelector('#project-connect-title') || {}).textContent || '')",
        timeout=15000,
    )
    ok(bool(done), "03d done phase", {})
    first = p.evaluate(
        "() => !!document.activeElement && document.activeElement.matches('.btn.primary') && "
        "!!document.activeElement.closest('[aria-modal=\"true\"], dialog[open]')"
    )
    p.keyboard.press("Escape")
    settle(p, 200)
    after = modal_snapshot(p)
    ok(first and after["modals"] == 0, "03d connected dialog", {"first": first, **after})


@case("03e.dialog")
def _(ui, seed):
    p = ui.open("#/board/field-notes", clock=True)
    _connect_dialog(p)
    # fast_forward fires a due timer at most once, and the page re-arms its 2 s check only
    # after that check's request resolves. Step past 120 s one check at a time.
    p.clock.fast_forward(118_000)
    for _ in range(6):
        p.clock.fast_forward(2_100)
        try:
            p.wait_for_function(
                "() => /not connected/.test("
                "document.querySelector('#project-connect-title')?.textContent || '')",
                timeout=1500,
            )
            break
        except Exception as exc:
            if type(exc).__name__ != "TimeoutError":
                raise
    failed = p.evaluate(
        "() => (document.querySelector('#project-connect-title') || {}).textContent || ''"
    )
    first = p.evaluate(
        "() => !!document.activeElement && document.activeElement.matches('.btn.primary') && "
        "!!document.activeElement.closest('[aria-modal=\"true\"], dialog[open]')"
    )
    p.keyboard.press("Escape")
    settle(p, 200)
    after = modal_snapshot(p)
    ok(
        "not connected" in failed and first and after["modals"] == 0,
        "03e failed dialog",
        {"title": failed, "first": first, **after},
    )


def _disconnect(ui, seed, slug):
    p = ui.open(f"#/board/{slug}")
    native = []
    p.on("dialog", lambda d: (native.append(d.type), d.dismiss()))
    p.click('#head-actions [data-action="coordinator"]')
    p.wait_for_selector(".workbench-project", timeout=5000)
    settle(p, 400)
    button = p.locator(".workbench-project button", has_text=re.compile("Disconnect")).first
    button.click()
    settle(p, 400)
    modal = p.evaluate(
        "() => !!document.querySelector('[aria-modal=\"true\"]:not([hidden]), dialog[open]')"
    )
    initial = p.evaluate(
        "() => !!document.activeElement && !!document.activeElement.closest("
        "'[aria-modal=\"true\"], dialog[open]') && document.activeElement.matches('.btn, button')"
    )
    kept = trapped(p) if modal else False
    p.keyboard.press("Escape")
    settle(p, 200)
    after = modal_snapshot(p)
    ok(
        not native and modal and initial and kept and after["modals"] == 0,
        "disconnect confirm is a drawn dialog",
        {"native_dialogs": native, "modal": modal, "initial": initial, "trapped": kept, **after},
    )


@case("10c.dialog")
def _(ui, seed):
    _disconnect(ui, seed, "atlas")


@case("10d.dialog")
def _(ui, seed):
    _disconnect(ui, seed, "ledger")


@case("11.dialog")
def _(ui, seed):
    p = ui.open("#/board/atlas")
    dialog_case(p, "#cmdk", "dialog[open] input, .palette input", "#cmdk")


@case("11b.dialog")
def _(ui, seed):
    p = ui.open("#/board/atlas")
    pick_card(p, seed, "A-121")
    dialog_case(p, "#cmdk", "dialog[open] input, .palette input", "#cmdk")


@case("12.dialog")
def _(ui, seed):
    p = ui.open("#/board/atlas")
    dialog_case(
        p,
        "#shortcuts-link",
        '[role="switch"], .switch, [data-single-keys]',
        "#shortcuts-link",
    )


@case("04c.dialog")
def _(ui, seed):
    p = ui.open("#/board/atlas", PUSH)
    p.locator(card_selector(seed, "A-124")).focus()
    has = p.evaluate("() => !!document.getElementById('details-btn')")
    ok(has, "1024 px board header has the Details button (04c)", {"details_btn": has})
    dialog_case(p, "#details-btn", "#card-panel .hide", "#details-btn")


@case("m4.dialog")
def _(ui, seed):
    p = ui.open("#/board/atlas", PHONE)
    opener = '#fab, #head-actions [data-action="new-card"]'
    p.locator(opener).first.click()
    settle(p, 400)
    initial = p.evaluate(
        "() => !!document.activeElement && document.activeElement.matches('input') && "
        "!!document.activeElement.closest('[aria-modal=\"true\"], dialog[open]')"
    )
    kept = trapped(p)
    p.keyboard.press("Escape")
    settle(p, 200)
    after = modal_snapshot(p)
    back = p.evaluate(
        "(s) => !!document.activeElement && document.activeElement.matches(s)", opener
    )
    ok(
        initial and kept and after["modals"] == 0 and back,
        "phone add card is a sheet",
        {"initial": initial, "trapped": kept, **after, "back": back},
    )


def _needs_item(p, title):
    p.locator("[data-needs-select]", has_text=title).first.click()
    settle(p, 400)


@case("m5.dialog")
def _(ui, seed):
    p = ui.open("#/needs", PHONE)
    _needs_item(p, "Send beta 0.9")
    p.click("[data-needs-approve]")
    settle(p, 300)
    initial = p.evaluate(
        "() => !!document.activeElement && document.activeElement.matches('.btn, button') && "
        "!!document.activeElement.closest('[aria-modal=\"true\"]')"
    )
    kept = trapped(p, '[aria-modal="true"]')
    p.keyboard.press("Escape")
    settle(p, 200)
    back = p.evaluate(
        "() => !!document.activeElement && document.activeElement.matches('[data-needs-approve]')"
    )
    ok(
        initial and kept and back,
        "m5 confirm sheet",
        {"initial": initial, "trapped": kept, "back": back},
    )


@case("m9.dialog")
def _(ui, seed):
    p = ui.open("#/board/atlas", PHONE)
    p.click('#col-jump, [data-board-column="review"]')
    settle(p, 200)
    dialog_case(
        p, "#more-A-121", '[aria-modal="true"] :is(.opt, button, [role="menuitem"])', "#more-A-121"
    )


@case("m10.dialog")
def _(ui, seed):
    p = ui.open("#/board/atlas", PHONE)
    p.click("#m-chat-tab")
    settle(p, 500)
    initial = composer(p)
    kept = trapped(p, "#chat")
    p.keyboard.press("Escape")
    settle(p, 200)
    after = modal_snapshot(p)
    ok(
        initial and kept and after["focus"] == "m-chat-tab" and after["inert"] == 0,
        "m10 chat sheet: composer focus (workbench plan), trap, Esc to the tab",
        {"composer": initial, "trapped": kept, **after},
    )


# ---- Board, palette, Needs me keys (keytest.cjs:51-74) ------------------------------------


@case("04.board-arrows")
def _(ui, seed):
    p = ui.open("#/board/atlas")
    first = p.locator(".workbench-board #col-todo .card[data-card]").first
    first.focus()
    seq = []
    for key in ("ArrowDown", "ArrowRight", "ArrowRight", "ArrowRight", "ArrowLeft"):
        p.keyboard.press(key)
        seq.append(p.evaluate("() => document.activeElement.closest('.col')?.dataset.status || ''"))
    stops = p.evaluate(
        "() => document.querySelectorAll('.workbench-board .board [tabindex=\"0\"]').length"
    )
    columns = p.evaluate(
        "() => [...document.querySelectorAll('.workbench-board .col')]"
        ".filter((c) => c.querySelector('.card[data-card]')).length"
    )
    ok(
        seq[0] == "todo"
        and seq[1] == "doing"
        and seq[3] == "done"
        and seq[4] == "review"
        and stops == columns,
        "roving board keys, Done reachable",
        {"seq": seq, "stops": stops, "columns": columns},
    )


@case("11.palette-arrowdown")
def _(ui, seed):
    p = ui.open("#/board/atlas")
    p.click("#cmdk")
    settle(p, 300)
    p.keyboard.press("ArrowDown")
    got = p.evaluate(
        """() => { const i = document.querySelector('dialog[open] input, .palette input');
      const id = i && i.getAttribute('aria-activedescendant');
      const sel = document.querySelector('[role=option][aria-selected=true]');
      const opts = [...document.querySelectorAll('[role=option]')];
      return { id, sel: sel && sel.id, index: opts.indexOf(sel) }; }"""
    )
    ok(got["id"] and got["id"] == got["sel"] and got["index"] == 1, "palette ArrowDown", got)


@case("07.a-d-no-shortcut")
def _(ui, seed):
    p = ui.open("#/needs")
    holds = []
    p.on(
        "request",
        lambda r: holds.append(r.method) if "/api/holds" in r.url and r.method == "POST" else None,
    )
    p.keyboard.press("a")
    p.keyboard.press("d")
    settle(p, 300)
    shown = p.evaluate("() => !!document.querySelector('[data-needs-confirm]')")
    ok(
        not shown and not holds,
        "A and D do nothing on Needs me",
        {"confirm": shown, "posts": holds},
    )


# ---- The chat control on the board (keytest.cjs:75-108) ------------------------------------


def _board_with_details(ui, seed, size=DESKTOP):
    p = ui.open("#/board/atlas", size)
    pick_card(p, seed, "A-121")
    return p


@case("04.closed-at-load")
def _(ui, seed):
    p = _board_with_details(ui, seed)
    s = st(p)
    ok(not s["open"] and s["ctx"] and all_expanded(s, "false"), "04 closed at load", s)


@case("04.toggle-enter-opens")
def _(ui, seed):
    p = _board_with_details(ui, seed)
    p.focus("#chat-toggle")
    p.keyboard.press("Enter")
    settle(p, 400)
    s = st(p)
    ok(s["open"] and not s["ctx"] and all_expanded(s, "true") and s["inChat"], "04 Enter opens", s)


@case("04.esc-closes")
def _(ui, seed):
    p = _board_with_details(ui, seed)
    p.focus("#chat-toggle")
    p.keyboard.press("Enter")
    settle(p, 400)
    p.keyboard.press("Escape")
    settle(p, 300)
    s = st(p)
    ok(
        not s["open"] and s["ctx"] and all_expanded(s, "false") and s["focus"] == "chat-toggle",
        "04 Esc in the message box closes, focus back on the control",
        s,
    )


@case("04.slash-opens")
def _(ui, seed):
    p = _board_with_details(ui, seed)
    p.locator(card_selector(seed, "A-121")).focus()
    p.keyboard.press("/")
    settle(p, 400)
    s = st(p)
    ok(s["open"] and all_expanded(s, "true") and s["composer"], "04 '/' opens, box focused", s)


@case("04.close-button")
def _(ui, seed):
    p = _board_with_details(ui, seed)
    open_chat(p)
    p.click("#chat-close")
    settle(p, 300)
    s = st(p)
    ok(
        not s["open"] and s["ctx"] and all_expanded(s, "false") and s["focus"] == "chat-toggle",
        "04 Close chat",
        s,
    )


@case("04.toggle-again-closes")
def _(ui, seed):
    p = _board_with_details(ui, seed)
    p.focus("#chat-toggle")
    p.keyboard.press("Enter")
    settle(p, 400)
    p.focus("#chat-toggle")
    p.keyboard.press("Space")
    settle(p, 300)
    s = st(p)
    ok(
        not s["open"] and all_expanded(s, "false") and s["focus"] == "chat-toggle",
        "04 Space closes",
        s,
    )


@case("04.bracket-closes")
def _(ui, seed):
    p = ui.open("#/board/atlas")
    open_chat(p)
    p.focus("#chat-close")
    p.keyboard.press("]")
    settle(p, 300)
    s = st(p)
    ok(not s["open"] and all_expanded(s, "false"), "04 ] closes the chat", s)


# ---- Needs me and Memory openers ----------------------------------------------------------


def _needs_approval(ui, size=DESKTOP):
    p = ui.open("#/needs", size)
    _needs_item(p, "Send beta 0.9")
    return p


@case("07.ask-opens-chat")
def _(ui, seed):
    p = _needs_approval(ui)
    p.click("[data-needs-ask]")
    settle(p, 400)
    s = st(p)
    exp = p.evaluate(EXPANDED_ALL_JS)
    ok(
        s["open"]
        and not s["ctx"]
        and len(exp) >= 2
        and all(v == "true" for v in exp)
        and s["inChat"],
        "07 Ask a question opens the chat, both openers expanded",
        {**s, "all": exp},
    )


@case("07.back-to-approval")
def _(ui, seed):
    p = _needs_approval(ui)
    p.click("[data-needs-ask]")
    settle(p, 400)
    tap(p, "#chat .chat-about [data-close-chat]")
    settle(p, 300)
    s = st(p)
    exp = p.evaluate(EXPANDED_ALL_JS)
    on_ask = p.evaluate(
        "() => !!document.activeElement && document.activeElement.matches('[data-needs-ask]')"
    )
    ok(
        not s["open"] and s["ctx"] and all(v == "false" for v in exp) and on_ask,
        "07 Back to the approval",
        {**s, "all": exp, "on_ask": on_ask},
    )


def _memory_leon(ui, size=DESKTOP):
    p = ui.open("#/memory?q=limit", size)
    p.wait_for_selector("[data-memory-result]", timeout=8000)
    p.locator("[data-memory-result]", has_text="per-workspace limit, 600").first.click()
    settle(p, 400)
    return p


@case("09.open-in-chat-esc")
def _(ui, seed):
    p = _memory_leon(ui)
    p.click("#open-in-chat")
    settle(p, 500)
    p.keyboard.press("Escape")
    settle(p, 300)
    s = st(p)
    ok(not s["open"] and s["ctx"] and s["focus"] == "open-in-chat", "09 Open in chat, Esc", s)


# ---- Chat rows and Details (keytest.cjs:109-170) ------------------------------------------


@case("06c.row-outline.A-135")
def _(ui, seed):
    p = ui.open("#/board/atlas")
    open_chat(p)
    p.focus(row(seed, "A-135"))
    settle(p, 150)
    got = outlined(p)
    ok(got == "A-135", "06c focusing the A-135 row outlines its board card", {"outlined": got})


@case("06c.row-details.A-134")
def _(ui, seed):
    p = ui.open("#/board/atlas")
    open_chat(p)
    p.click(row(seed, "A-134"))
    settle(p, 600)
    d = det(p)
    ok(
        d["ctxId"] == "A-134"
        and d["selId"] == "A-134"
        and d["ctxTitle"] == d["selTitle"]
        and not d["chat"]
        and d["ctx"]
        and d["strip"]
        and d["focus"] == "H2",
        "06c A-134 row opens its Details with the Back to chat strip; board agrees",
        d,
    )


@case("06c.back-to-chat.A-134")
def _(ui, seed):
    p = ui.open("#/board/atlas")
    open_chat(p)
    p.click(row(seed, "A-134"))
    settle(p, 600)
    p.click("[data-back-chat]")
    settle(p, 500)
    d = det(p)
    ok(d["chat"] and d["focusCard"] == str(seed.number("A-134")), "06c Back to chat, same row", d)


@case("06c.reopen-keeps-details")
def _(ui, seed):
    p = ui.open("#/board/atlas")
    open_chat(p)
    p.focus(row(seed, "A-135"))
    p.keyboard.press("Enter")
    settle(p, 600)
    p.click("#chat-toggle")
    settle(p, 300)
    p.click("#chat-toggle")
    settle(p, 400)
    d = det(p)
    ok(
        d["ctxId"] == "A-135"
        and d["selId"] == "A-135"
        and not d["chat"]
        and d["ctx"]
        and not d["strip"],
        "06c open and close the chat again: still A-135, no strip",
        d,
    )


@case("06c.board-card-no-strip")
def _(ui, seed):
    p = ui.open("#/board/atlas")
    pick_card(p, seed, "A-124")
    d = det(p)
    ok(
        d["ctxId"] == "A-124" and d["selId"] == "A-124" and not d["strip"],
        "board card picked with the chat closed: its details, no strip",
        d,
    )


@case("06f.static-details.A-134")
def _(ui, seed):
    p = ui.open("#/board/atlas")
    open_chat(p)
    p.click(row(seed, "A-134"))
    settle(p, 600)
    d = det(p)
    ok(
        d["ctxId"] == "A-134"
        and d["selId"] == "A-134"
        and d["ctxTitle"] == d["selTitle"]
        and not d["chat"]
        and d["strip"],
        "06f A-134 details match the selected card",
        d,
    )


@case("06c.tablet-1024-row-beside.A-135")
def _(ui, seed):
    p = ui.open("#/board/atlas", PUSH)
    open_chat(p)
    p.click(row(seed, "A-135"))
    settle(p, 600)
    tv = p.evaluate(
        """() => { const c = document.getElementById('card-panel'), r = c.getBoundingClientRect();
      return { shown: !c.hidden && getComputedStyle(c).display !== 'none' && r.width > 300,
        id: (c.querySelector('.detail-h .cid') || {}).textContent,
        boardRight: document.querySelector('.center').getBoundingClientRect().right, ctxLeft: r.left }; }"""
    )
    ok(
        tv["shown"] and tv["id"] == "A-135" and tv["ctxLeft"] >= tv["boardRight"] - 1,
        "1024 px: A-135 row → its details beside the board",
        tv,
    )


@case("06c.pointer-outline.focus-A-135")
def _(ui, seed):
    p = ui.open("#/board/atlas")
    open_chat(p)
    p.focus(row(seed, "A-135"))
    settle(p, 150)
    got = outlined(p)
    ok(got == "A-135", "outline follows focus", {"outlined": got})


@case("06c.pointer-outline.focus-leave")
def _(ui, seed):
    p = ui.open("#/board/atlas")
    open_chat(p)
    p.focus(row(seed, "A-135"))
    settle(p, 150)
    before = outlined(p)
    p.focus("#chat-text")
    settle(p, 150)
    after = outlined(p)
    ok(
        before == "A-135" and after == "",
        "outline clears when focus leaves",
        {"focused": before, "after": after},
    )


@case("07.toggle-no-strip-r-strip")
def _(ui, seed):
    p = _needs_approval(ui)
    p.click("#chat-toggle")
    settle(p, 400)
    s1 = st(p)
    sa = p.evaluate(
        "() => { const a = document.querySelector('#chat .chat-about'); return !a || a.hidden; }"
    )
    p.keyboard.press("Escape")
    settle(p, 300)
    p.locator("#card-panel h2, #card-panel [data-needs-title]").first.focus()
    p.keyboard.press("r")
    settle(p, 400)
    s2 = st(p)
    sb = p.evaluate(
        "() => { const a = document.querySelector('#chat .chat-about'); return !!a && !a.hidden; }"
    )
    exp = p.evaluate(EXPANDED_ALL_JS)
    ok(
        s1["open"] and sa and s2["open"] and sb and all(v == "true" for v in exp),
        "07 the control opens without the approval strip; R opens it with the strip",
        {"s1": s1, "no_strip": sa, "s2": s2, "strip": sb, "all": exp},
    )


@case("09.toggle-no-strip-open-in-chat-strip")
def _(ui, seed):
    p = _memory_leon(ui)
    strips = (
        "() => [!!document.querySelector('#chat .chat-about:not([hidden])'), "
        "!!document.querySelector('#chat .chat-quote')]"
    )
    p.click("#chat-toggle")
    settle(p, 400)
    ma = p.evaluate(strips)
    p.keyboard.press("Escape")
    settle(p, 300)
    p.click("#open-in-chat")
    settle(p, 600)
    mb = p.evaluate(strips)
    ok(
        ma == [False, False] and mb == [True, True],
        "09 control: no strip, no quote; Open in chat: strip and quote",
        {"toggle": ma, "open_in_chat": mb},
    )


@case("03.starts-closed-shell-class")
def _(ui, seed):
    """03 starts closed; the chat takes the right-hand slot; Esc gives the board back."""
    p = ui.open("#/board/field-notes")
    lay = """() => { const c = document.getElementById('chat'), m = document.querySelector('.center');
      const cr = c && !c.hidden ? c.getBoundingClientRect() : null, mr = m.getBoundingClientRect();
      return { cls: document.querySelector('.shell').className, chat: !!cr,
        slot: !!cr && cr.left >= mr.right - 1 && cr.width >= 300, boardRight: Math.round(mr.right) }; }"""
    c0 = p.evaluate(lay)
    p.click("#chat-toggle")
    settle(p, 400)
    c1 = p.evaluate(lay)
    p.keyboard.press("Escape")
    settle(p, 300)
    c2 = p.evaluate(lay)
    ok(
        not c0["chat"]
        and c1["chat"]
        and c1["slot"]
        and "chat-open" in c1["cls"]
        and not c2["chat"]
        and "chat-open" not in c2["cls"]
        and c2["boardRight"] == c0["boardRight"],
        "03 starts closed; opens in the right-hand slot; closes back",
        {"c0": c0, "c1": c1, "c2": c2},
    )


# ---- Phone chat rows (keytest.cjs:193-211) ------------------------------------------------


def _phone_show(ui, seed, cid, column):
    p = ui.open("#/board/atlas", PHONE)
    p.click("#m-chat-tab")
    settle(p, 600)
    p.click(row(seed, cid))
    settle(p, 600)
    r = p.evaluate(
        """() => ({ sel: (document.querySelector('.workbench-board .card.is-selected .cid') || {}).textContent || '',
      col: (document.querySelector('[data-board-column][aria-pressed=true]') || {}).textContent || '',
      focus: (document.activeElement && document.activeElement.getAttribute('aria-label')) || '' })"""
    )
    back = p.locator("[data-back-chat], .m-frombar a").first
    back.click()
    settle(p, 500)
    focus = p.evaluate(
        "() => document.activeElement && (document.activeElement.dataset.chatCard || document.activeElement.id)"
    )
    ok(
        r["sel"] == cid
        and r["focus"].startswith(cid)
        and r["col"].strip().startswith(column)
        and focus == str(seed.number(cid)),
        f"m10 Show {cid} → its card in {column}; Back to chat → the same row",
        {**r, "back_focus": focus},
    )


@case("m10.show.A-134")
def _(ui, seed):
    _phone_show(ui, seed, "A-134", "Todo")


@case("m10.show.A-135")
def _(ui, seed):
    _phone_show(ui, seed, "A-135", "Todo")


@case("m10.back-to-board")
def _(ui, seed):
    p = ui.open("#/board/atlas", PHONE)
    p.click("#m-chat-tab")
    settle(p, 500)
    tap(p, "#m-chat-back, #chat-close")
    settle(p, 300)
    focus = p.evaluate("() => document.activeElement.id")
    ok(focus == "m-chat-tab", "m10 Back to board → focus on the Chat tab", {"focus": focus})


# ---- Round 3 -----------------------------------------------------------------------------

NAMES_JS = """() => ({
  label: (document.querySelector('#chat label[for="chat-text"]') || {}).textContent || '',
  head: (document.getElementById('chat-h') || {}).textContent || '',
  log: (document.querySelector('#chat [role="log"]') || { getAttribute: () => '' }).getAttribute('aria-label') || '',
  toggle: (document.getElementById('chat-toggle') || {}).textContent || '' })"""


def _names(p, project):
    n = p.evaluate(NAMES_JS)
    ok(
        n["label"] == f"Message to {project} chat"
        and n["head"] == f"{project} chat"
        and project in n["log"]
        and project in n["toggle"],
        f"composer, heading, log and control all name {project}",
        n,
    )


@case("07.names-atlas")
def _(ui, seed):
    p = _needs_approval(ui)
    p.click("#chat-toggle")
    settle(p, 500)
    _names(p, "Atlas")


@case("08.names-harbor")
def _(ui, seed):
    p = ui.open("#/needs")
    _needs_item(p, "Pick the homepage headline")
    p.click("#chat-toggle")
    settle(p, 500)
    _names(p, "Harbor")


def _board_pick_with_chat(ui, seed, cid):
    p = ui.open("#/board/atlas")
    open_chat(p)
    pick_card(p, seed, cid)
    return p


@case("06c.board-pick-strip.A-121")
def _(ui, seed):
    p = _board_pick_with_chat(ui, seed, "A-121")
    d = det(p)
    ok(
        d["ctxId"] == "A-121"
        and d["selId"] == "A-121"
        and d["strip"]
        and not d["chat"]
        and "on the board" in d["stripText"],
        "chat open, board pick A-121: its details, board strip",
        d,
    )


@case("06c.board-pick-back-to-chat")
def _(ui, seed):
    p = _board_pick_with_chat(ui, seed, "A-121")
    p.click("[data-back-chat]")
    settle(p, 500)
    s = st(p)
    ok(s["open"] and s["composer"], "board-origin Back to chat → chat, cursor in the box", s)


@case("06c.board-enter.A-124")
def _(ui, seed):
    p = ui.open("#/board/atlas")
    open_chat(p)
    p.locator(card_selector(seed, "A-124")).focus()
    p.keyboard.press("Enter")
    p.wait_for_selector("#card-panel:not([hidden]) .detail-h", timeout=5000)
    settle(p, 300)
    d = det(p)
    ok(
        d["ctxId"] == "A-124" and d["selId"] == "A-124" and d["strip"] and not d["chat"],
        "chat open, Enter on board card A-124 → details with strip",
        d,
    )


SHEET_JS = """() => { const c = document.getElementById('chat');
  return { modal: c.getAttribute('aria-modal'), role: c.getAttribute('role'),
    inertMain: document.querySelector('.center').hasAttribute('inert'),
    scrim: getComputedStyle(document.querySelector('.chat-scrim')).display }; }"""


@case("768.chat-sheet-modal")
def _(ui, seed):
    p = ui.open("#/board/atlas", TABLET)
    p.click("#chat-toggle")
    settle(p, 500)
    q0 = p.evaluate(SHEET_JS)
    kept = trapped(p, "#chat")
    p.keyboard.press("Escape")
    p.keyboard.press("Escape")
    settle(p, 300)
    q1 = modal_snapshot(p)
    is_open = st(p)["open"]
    ok(
        q0["modal"] == "true"
        and q0["role"] == "dialog"
        and q0["inertMain"]
        and q0["scrim"] == "block"
        and kept
        and not is_open
        and q1["inert"] == 0
        and q1["focus"] == "chat-toggle",
        "768 px: the chat is a modal sheet",
        {**q0, "trapped": kept, "open": is_open, **q1},
    )


@case("768.row-details-sheet")
def _(ui, seed):
    p = ui.open("#/board/atlas", TABLET)
    p.click("#chat-toggle")
    settle(p, 500)
    q2 = p.evaluate(SHEET_JS)
    p.click(row(seed, "A-134"))
    settle(p, 600)
    q3 = p.evaluate(
        """() => ({ ctxModal: document.getElementById('card-panel').getAttribute('aria-modal'),
      id: (document.querySelector('#card-panel .detail-h .cid') || {}).textContent,
      inert: document.querySelector('.center').hasAttribute('inert'), focus: document.activeElement.tagName })"""
    )
    p.keyboard.press("Escape")
    settle(p, 400)
    d = det(p)
    ok(
        q2["modal"] == "true"
        and q3["ctxModal"] == "true"
        and q3["id"] == "A-134"
        and q3["inert"]
        and q3["focus"] == "H2"
        and d["chat"]
        and d["focusCard"] == str(seed.number("A-134")),
        "768 px: a chat row opens its details as a modal sheet; Esc back to the row",
        {"q2": q2, "q3": q3, "q4": d},
    )


@case("390.chat-fullscreen")
def _(ui, seed):
    p = ui.open("#/board/atlas", PHONE)
    p.click("#chat-toggle")
    settle(p, 500)
    p0 = p.evaluate(
        """() => { const c = document.getElementById('chat'), r = c.getBoundingClientRect();
      return { top: r.top, left: r.left, w: r.width, h: r.height, modal: c.getAttribute('aria-modal'),
        boardInert: document.querySelector('.center').hasAttribute('inert') }; }"""
    )
    ok(
        p0["top"] == 0
        and p0["left"] == 0
        and p0["w"] == 390
        and p0["h"] >= 840
        and p0["modal"] == "true"
        and p0["boardInert"],
        "390 px: the chat is a full-screen modal sheet",
        p0,
    )


HERE_JS = """() => ({ hash: location.hash, focus: document.activeElement && document.activeElement.id,
  strip: (document.querySelector('#chat .chat-about:not([hidden])') || {}).textContent || '',
  back: (document.querySelector('#chat .chat-about:not([hidden]) [data-close-chat]') || {}).textContent || '' })"""


@case("m3.ask-strip-back")
def _(ui, seed):
    p = _needs_approval(ui, PHONE)
    p.click("[data-needs-ask]")
    settle(p, 500)
    a1 = p.evaluate(HERE_JS)
    tap(p, "#chat .chat-about [data-close-chat]")
    settle(p, 400)
    on_ask = p.evaluate("() => document.activeElement.matches('[data-needs-ask]')")
    ok(
        "You are asking about" in a1["strip"] and "Back to the approval" in a1["back"] and on_ask,
        "phone Ask a question: approval strip, Back to the approval → focus on Ask",
        {"a1": a1, "on_ask": on_ask},
    )


@case("m8.ask-released-strip")
def _(ui, seed):
    p = ui.open("#/needs", PHONE)
    _needs_item(p, "Publish the Harbor privacy page")
    p.locator("[data-needs-ask]", has_text="Ask the orchestrator").first.click()
    settle(p, 500)
    b1 = p.evaluate(HERE_JS)
    tap(p, "#chat .chat-about [data-close-chat]")
    settle(p, 400)
    back = p.evaluate("() => document.activeElement.matches('[data-needs-ask]')")
    ok(
        "can't be recalled" in b1["strip"] and back,
        "released approval: Ask the orchestrator → released strip; back → focus on the button",
        {"b1": b1, "back": back},
    )


@case("m2.chat-tab-back-to-needs")
def _(ui, seed):
    p = ui.open("#/needs", PHONE)
    p.click("#m-chat-tab")
    settle(p, 500)
    n1 = p.evaluate(HERE_JS)
    tap(p, "#chat .chat-about [data-close-chat]")
    settle(p, 400)
    n2 = p.evaluate(HERE_JS)
    ok(
        "Back to Needs me" in n1["back"]
        and n2["hash"] == "#/needs"
        and n2["focus"] == "m-chat-tab",
        "phone Needs me Chat tab → Back to Needs me → focus on the tab",
        {"n1": n1, "n2": n2},
    )


def _phone_settings(p):
    p.click("#m-settings")
    p.wait_for_selector("[data-project-back]", timeout=5000)
    settle(p, 400)
    return p.evaluate(
        """() => ({ back: document.querySelector('[data-project-back]').textContent.trim(),
      modal: !!document.querySelector('[data-project-back]').closest('[aria-modal="true"]') })"""
    )


@case("m1.settings-sheet")
def _(ui, seed):
    p = ui.open("#/board/atlas", PHONE)
    label = p.evaluate("() => document.getElementById('m-settings').textContent.trim()")
    d1 = _phone_settings(p)
    p.click("[data-project-back]")
    settle(p, 400)
    d2 = p.evaluate("() => ({ hash: location.hash, focus: document.activeElement.id })")
    ok(
        label == "Settings"
        and d1["modal"]
        and d2["hash"] == "#/board/atlas"
        and d2["focus"] == "m-settings",
        "phone Settings: a labelled sheet; back → focus on Settings",
        {"label": label, "d1": d1, "d2": d2},
    )


# ---- Round 4 -----------------------------------------------------------------------------

RESTART_JS = """() => ({ warn: (document.querySelector('.orch-card.warn') || {}).textContent || '',
  steps: document.querySelectorAll('.restart-box li, .restart li').length,
  check: !!document.querySelector('.restart-box .btn.primary'),
  back: (document.querySelector('[data-project-back]') || {}).textContent || '' })"""


def _restart_actions(p):
    return p.evaluate(
        "() => [...document.querySelectorAll('#chat .chat-recovery a, #chat .chat-recovery button')]"
        ".map((b) => b.textContent.trim())"
    )


@case("06d.restart-path")
def _(ui, seed):
    p = ui.open("#/board/ledger")
    open_chat(p)
    first = _restart_actions(p)
    p.locator("#chat .chat-recovery a", has_text="restart").first.click()
    p.wait_for_selector("[data-project-back]", timeout=5000)
    settle(p, 500)
    s10 = p.evaluate(RESTART_JS)
    p.click("[data-project-back]")
    settle(p, 600)
    back = p.evaluate(
        "() => ({ open: !document.getElementById('chat').hidden, focus: document.activeElement.textContent.trim() })"
    )
    ok(
        first[:2]
        and "restart" in first[0]
        and "Ask again" in first[1]
        and "Not answering since" in s10["warn"]
        and s10["steps"] == 3
        and s10["check"]
        and back["open"]
        and "restart" in back["focus"],
        "06d: restart first, Ask again second → amber card, 3 steps, Check again → back to the same button",
        {"first": first, "s10": s10, "back": back},
    )


@case("06g.board-pick-static.A-121")
def _(ui, seed):
    p = _board_pick_with_chat(ui, seed, "A-121")
    d = det(p)
    ok(
        d["ctxId"] == "A-121"
        and d["selId"] == "A-121"
        and d["ctxTitle"] == d["selTitle"]
        and d["strip"]
        and "picked A-121 on the board" in d["stripText"],
        "06g A-121 picked on the board",
        d,
    )


@case("07c.note-visible")
def _(ui, seed):
    p = _needs_approval(ui)
    p.click("[data-needs-approve]")
    settle(p, 400)
    nc = p.evaluate(
        """() => { const n = document.querySelector('#card-panel [data-needs-note]');
      if (!n) return { note: false };
      const r = n.getBoundingClientRect(), b = document.getElementById('card-panel').getBoundingClientRect();
      const confirm = document.querySelector('#card-panel .confirm');
      return { note: true, noteBottom: Math.round(r.bottom), noteTop: Math.round(r.top),
        bodyBottom: Math.round(Math.min(b.bottom, innerHeight)), confirm: !!confirm,
        confirmTop: confirm ? Math.round(confirm.getBoundingClientRect().top) : null }; }"""
    )
    ok(
        nc.get("note")
        and nc["confirm"]
        and nc["noteBottom"] <= nc["bodyBottom"]
        and nc["noteBottom"] <= nc["confirmTop"],
        "07c: the note field is fully visible above the confirm",
        nc,
    )


@case("1024.06f-pushed-slot")
def _(ui, seed):
    p = ui.open("#/board/atlas", PUSH)
    open_chat(p)
    p.click(row(seed, "A-134"))
    settle(p, 600)
    f = p.evaluate(
        """() => { const c = document.getElementById('card-panel'), r = c.getBoundingClientRect();
      return { shown: !c.hidden && getComputedStyle(c).display !== 'none', left: Math.round(r.left),
        boardRight: Math.round(document.querySelector('.center').getBoundingClientRect().right),
        modal: c.getAttribute('aria-modal'), id: (c.querySelector('.detail-h .cid') || {}).textContent }; }"""
    )
    ok(
        f["shown"]
        and f["id"] == "A-134"
        and f["left"] >= f["boardRight"] - 1
        and f["modal"] != "true",
        "1024 px: A-134's details in the pushed slot beside the board",
        f,
    )


@case("1024.details-button-sheet")
def _(ui, seed):
    p = ui.open("#/board/atlas", PUSH)
    p.locator(card_selector(seed, "A-124")).focus()
    has = p.evaluate("() => !!document.getElementById('details-btn')")
    ok(has, "1024 px board header has the Details button", {"details_btn": has})
    p.click("#details-btn")
    settle(p, 400)
    d_open = p.evaluate(
        """() => ({ shown: !document.getElementById('card-panel').hidden,
      modal: document.getElementById('card-panel').getAttribute('aria-modal'),
      focus: document.activeElement.className, exp: document.getElementById('details-btn').getAttribute('aria-expanded') })"""
    )
    p.keyboard.press("Escape")
    settle(p, 300)
    d_shut = modal_snapshot(p)
    ok(
        d_open["shown"]
        and d_open["modal"] == "true"
        and "hide" in d_open["focus"]
        and d_open["exp"] == "true"
        and d_shut["inert"] == 0
        and d_shut["focus"] == "details-btn",
        "1024 px: Details opens the sheet (focus on Hide); Esc closes back to Details",
        {"open": d_open, "shut": d_shut},
    )


@case("768.06f-scrim-taps")
def _(ui, seed):
    p = ui.open("#/board/atlas", TABLET)
    p.click("#chat-toggle")
    settle(p, 500)
    p.click(row(seed, "A-134"))
    settle(p, 600)
    f768 = p.evaluate(
        """() => ({ modal: document.getElementById('card-panel').getAttribute('aria-modal'),
      inert: document.querySelector('.center').hasAttribute('inert'),
      id: (document.querySelector('#card-panel .detail-h .cid') || {}).textContent })"""
    )
    p.mouse.click(60, 500)
    settle(p, 400)
    b = det(p)
    p.mouse.click(60, 500)
    settle(p, 400)
    c = modal_snapshot(p)
    is_open = st(p)["open"]
    ok(
        f768["modal"] == "true"
        and f768["inert"]
        and f768["id"] == "A-134"
        and b["chat"]
        and b["focusCard"] == str(seed.number("A-134"))
        and not is_open
        and c["inert"] == 0,
        "768 px: tapping the dimmed area goes back to the chat (same row), again closes it",
        {"f768": f768, "back": b, "closed": {**c, "open": is_open}},
    )


def _tab_text(p):
    return p.evaluate(
        "() => { const t = document.getElementById('m-chat-tab'); "
        "return (t.getAttribute('aria-label') || t.textContent).replace(/\\s+/g, ' ').trim(); }"
    )


SHEET_INFO_JS = """() => ({ head: (document.getElementById('chat-h') || {}).textContent || '',
  label: (document.querySelector('#chat label[for="chat-text"]') || {}).textContent || '',
  back: (document.querySelector('#chat .chat-about:not([hidden]) [data-close-chat]') || {}).textContent || '' })"""


@case("m2.tab-names-atlas")
def _(ui, seed):
    p = ui.open("#/needs", PHONE)
    settle(p, 600)
    ta = _tab_text(p)
    p.click("#m-chat-tab")
    settle(p, 500)
    sa = p.evaluate(SHEET_INFO_JS)
    tap(p, "#chat .chat-about [data-close-chat]")
    settle(p, 400)
    ra = p.evaluate("() => ({ hash: location.hash, focus: document.activeElement.id })")
    ok(
        ta == "Atlas chat 1 new reply"
        and sa["head"] == "Atlas chat"
        and sa["label"] == "Message to Atlas chat"
        and sa["back"] == "Back to Needs me"
        and ra["hash"] == "#/needs"
        and ra["focus"] == "m-chat-tab",
        "m2: the tab reads 'Atlas chat 1 new reply' → Atlas chat → back to Needs me",
        {"tab": ta, "sheet": sa, "back": ra},
    )


def _harbor_last(p):
    _needs_item(p, "Pick the homepage headline")
    close = p.locator("[data-needs-close]").first
    if close.count():
        close.click()
        settle(p, 300)


@case("m2b.tab-names-harbor")
def _(ui, seed):
    p = ui.open("#/needs", PHONE)
    _harbor_last(p)
    th = _tab_text(p)
    p.click("#m-chat-tab")
    settle(p, 500)
    sh = p.evaluate(SHEET_INFO_JS)
    tap(p, "#chat .chat-about [data-close-chat]")
    settle(p, 400)
    rh = p.evaluate("() => ({ hash: location.hash, focus: document.activeElement.id })")
    ok(
        th.startswith("Harbor chat")
        and sh["head"] == "Harbor chat"
        and sh["label"] == "Message to Harbor chat"
        and rh["focus"] == "m-chat-tab",
        "m2b: Harbor item last opened → Harbor chat",
        {"tab": th, "sheet": sh, "back": rh},
    )


@case("m12b.restart-path")
def _(ui, seed):
    p = ui.open("#/board/ledger", PHONE)
    p.click("#m-chat-tab")
    settle(p, 600)
    first = _restart_actions(p)
    p.locator("#chat .chat-recovery a", has_text="restart").first.click()
    p.wait_for_selector("[data-project-back]", timeout=5000)
    settle(p, 500)
    s13 = p.evaluate(RESTART_JS)
    p.click("[data-project-back]")
    settle(p, 600)
    back = p.evaluate(
        "() => ({ open: !document.getElementById('chat').hidden, focus: document.activeElement.textContent.trim() })"
    )
    ok(
        first[:2]
        and "restart" in first[0]
        and "Ask again" in first[1]
        and "Not answering since" in s13["warn"]
        and s13["steps"] == 3
        and s13["back"] == "Back to the chat"
        and back["open"]
        and "restart" in back["focus"],
        "m12b: restart → amber card, 3 steps, Back to the chat → the restart button",
        {"first": first, "s13": s13, "back": back},
    )


@case("m2.settings-back-to-needs")
def _(ui, seed):
    p = ui.open("#/needs", PHONE)
    d = _phone_settings(p)
    p.click("[data-project-back]")
    settle(p, 400)
    r = p.evaluate("() => ({ hash: location.hash, focus: document.activeElement.id })")
    ok(
        d["back"] == "Back to Needs me" and r["hash"] == "#/needs" and r["focus"] == "m-settings",
        "m2 Settings → Back to Needs me → focus on Settings",
        {"sheet": d, "after": r},
    )


@case("m2.unread-status-line")
def _(ui, seed):
    p = ui.open("#/needs", PHONE)
    settle(p, 800)
    live = p.evaluate(
        "() => (document.querySelector('.m-tabs [role=\"status\"]') || {}).textContent || ''"
    )
    ok(live == "1 new reply in Atlas chat", "the phone unread count is announced", {"status": live})


# ---- Round 5 -----------------------------------------------------------------------------


def _row_strip(ui, seed, cid):
    p = ui.open("#/board/atlas")
    open_chat(p)
    p.click(row(seed, cid))
    settle(p, 700)
    return p, det(p)


@case("06a.row-strip-quick-reply.A-121")
def _(ui, seed):
    _, d = _row_strip(ui, seed, "A-121")
    ok(
        d["ctxId"] == "A-121"
        and d["strip"]
        and re.search(r"\d\d:\d\d quick reply \(local model\)", d["stripText"])
        and "orchestrator" not in d["stripText"]
        and not d["chat"]
        and d["focus"] == "H2",
        "the A-121 row in the quick reply names that quick reply",
        d,
    )


@case("06c.row-strip-orchestrator.A-134")
def _(ui, seed):
    _, d = _row_strip(ui, seed, "A-134")
    ok(
        d["ctxId"] == "A-134" and re.search(r"orchestrator's \d\d:\d\d answer", d["stripText"]),
        "the A-134 row names the orchestrator's answer",
        d,
    )


@case("06f3.static-strip.A-121")
def _(ui, seed):
    _, d = _row_strip(ui, seed, "A-121")
    ok(
        d["ctxId"] == "A-121" and d["strip"] and "quick reply" in d["stripText"],
        "06f3 strip names the quick reply",
        d,
    )


@case("06f.hide-and-bracket-back")
def _(ui, seed):
    p, _ = _row_strip(ui, seed, "A-134")
    p.click("#card-panel .hide")
    settle(p, 500)
    q4 = det(p)
    p2, _ = _row_strip(ui, seed, "A-134")
    p2.focus("#card-panel .detail-h h2")
    p2.keyboard.press("]")
    settle(p2, 500)
    q5 = det(p2)
    want = str(seed.number("A-134"))
    ok(
        q4["chat"] and q4["focusCard"] == want and q5["chat"] and q5["focusCard"] == want,
        "Hide and ] on Details opened from the chat go back to the chat, focus on the row",
        {"hide": q4, "bracket": q5},
    )


def _esc_settings(p):
    p.keyboard.press("Escape")
    settle(p, 500)
    return p.evaluate(
        """() => ({ hash: location.hash, focus: document.activeElement.id || document.activeElement.textContent.trim(),
      chat: !!document.getElementById('chat') && !document.getElementById('chat').hidden,
      modals: [...document.querySelectorAll('[aria-modal="true"]')].filter((m) => m.getClientRects().length > 0).length })"""
    )


@case("m13.esc.error")
def _(ui, seed):
    p = ui.open("#/board/ledger", PHONE)
    p.click("#m-chat-tab")
    settle(p, 600)
    p.locator("#chat .chat-recovery a", has_text="restart").first.click()
    p.wait_for_selector("[data-project-back]", timeout=5000)
    settle(p, 400)
    r = _esc_settings(p)
    ok(
        r["chat"] and "restart" in r["focus"] and r["modals"] == 1,
        "Esc on Settings from the error returns like Back",
        r,
    )


@case("m13.esc.board")
def _(ui, seed):
    p = ui.open("#/board/atlas", PHONE)
    _phone_settings(p)
    r = _esc_settings(p)
    ok(
        r["hash"] == "#/board/atlas" and r["focus"] == "m-settings" and r["modals"] == 0,
        "Esc on Settings from the board returns like Back",
        r,
    )


@case("m13.esc.needs")
def _(ui, seed):
    p = ui.open("#/needs", PHONE)
    _phone_settings(p)
    r = _esc_settings(p)
    ok(
        r["hash"] == "#/needs" and r["focus"] == "m-settings" and r["modals"] == 0,
        "Esc on Settings from Needs me returns like Back",
        r,
    )


@case("m13.esc.needs-harbor")
def _(ui, seed):
    p = ui.open("#/needs", PHONE)
    _harbor_last(p)
    _phone_settings(p)
    r = _esc_settings(p)
    tab = _tab_text(p)
    ok(
        r["hash"] == "#/needs" and r["focus"] == "m-settings" and tab.startswith("Harbor chat"),
        "Esc on Settings from Needs me (Harbor last opened) returns to the Harbor state",
        {**r, "tab": tab},
    )


@case("m2b.settings-back-harbor")
def _(ui, seed):
    p = ui.open("#/needs", PHONE)
    _harbor_last(p)
    _phone_settings(p)
    p.click("[data-project-back]")
    settle(p, 400)
    r = p.evaluate("() => ({ hash: location.hash, focus: document.activeElement.id })")
    tab = _tab_text(p)
    ok(
        r["hash"] == "#/needs" and r["focus"] == "m-settings" and tab.startswith("Harbor chat"),
        "m2b Settings → Back returns to the Harbor Needs me state",
        {**r, "tab": tab},
    )


@case("390.log-above-composer")
def _(ui, seed):
    p = ui.open("#/board/atlas", PHONE)
    p.click("#chat-toggle")
    settle(p, 600)
    ov = p.evaluate(
        """() => { const log = document.querySelector('#chat [role=log]'); log.scrollTop = log.scrollHeight;
      const msgs = log.querySelectorAll('.chat-message'); const last = msgs[msgs.length - 1];
      const hint = [...document.querySelectorAll('#chat .chat-hint')].find((h) => /Enter to send/.test(h.textContent));
      return { lastBottom: Math.round(last.getBoundingClientRect().bottom),
        composerTop: Math.round(document.querySelector('#chat .chat-compose').getBoundingClientRect().top),
        scrolls: getComputedStyle(log).overflowY, hint: hint ? getComputedStyle(hint).display : 'none' }; }"""
    )
    ok(
        ov["lastBottom"] <= ov["composerTop"] and ov["scrolls"] == "auto" and ov["hint"] == "none",
        "390 px: the log scrolls, the last message ends above the composer, no keyboard hint",
        ov,
    )


@case("link.card-direct")
def _(ui, seed):
    """A shared card link opens the card over a drawn screen, not over a Loading state."""
    page = ui.chromium.new_context(viewport={"width": 1440, "height": 900})
    ui.contexts.append(page)
    page.add_cookies([{**seed.cookie, "url": seed.origin}])
    p = page.new_page()
    p.goto(f"{seed.origin}/app/#/card/121")
    p.wait_for_selector("#card-panel:not([hidden]) .detail-h", timeout=8000)
    settle(p, 2000)
    behind = p.evaluate(
        "() => ({ title: document.getElementById('title').textContent, "
        "content: document.getElementById('content').textContent.trim().slice(0, 40) })"
    )
    ok(
        not behind["content"].startswith("Loading"),
        "a direct card link draws the screen behind it",
        behind,
    )


# ---- New e2e for the adopted owner defaults: kept as cases so the SKIPS stay explicit ------

for _key in SKIPS:
    if _key not in CASES:

        def _skipped(ui, seed, _key=_key):
            pytest.skip(SKIPS[_key])

        CASES[_key] = _skipped


@pytest.mark.parametrize(
    "key",
    [
        pytest.param(k, marks=pytest.mark.skip(reason=SKIPS[k])) if k in SKIPS else k
        for k in sorted(CASES)
    ],
)
def test_key(key, ui, app):
    try:
        CASES[key](ui, app)
    finally:
        csp = ui.csp_violations()
    assert not csp, f"CSP violations on {key}: {json.dumps(csp, ensure_ascii=False)}"


def test_no_extra_skip_keys():
    """Every SKIPS key names a registered case; no stale entries."""
    assert set(SKIPS) <= set(CASES), sorted(set(SKIPS) - set(CASES))
    assert norm(" a  b ") == "a b"
