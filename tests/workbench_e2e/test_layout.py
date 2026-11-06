"""The design's layoutcheck, ported to the real app, plus AA contrast and target size.

One test per scenario renders it at every design width (README: 1440, 1366, 1280, 1024,
768 and 390 px, plus 320 px for screens that have a phone form) and reports every issue.
Legacy routes (rules, console, space, decide, workspace settings) also get the AA
contrast and target-size checks (24 px, 44 px below 700 px).
"""

import json

import pytest

from tests.workbench_e2e import shots
from tests.workbench_e2e.scenarios import PHONE_EXTRA, SCENARIOS, WIDTHS, append_log, settle

LAYOUT_JS = """([w, desktopOnly]) => {
  const out = [];
  const vis = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && !el.closest('[hidden], .sr, [inert] .sr'); };
  const name = (el) => (el.id ? '#' + el.id : el.className && typeof el.className === 'string'
    ? el.tagName.toLowerCase() + '.' + el.className.split(' ')[0] : el.tagName.toLowerCase())
    + ' “' + (el.textContent || '').trim().slice(0, 30) + '”';
  // 1. Clipped short labels.
  for (const el of document.querySelectorAll('kbd, .tag, .ct-chip, .btn, .chip, [role=tab], .cid, .chat-toggle, .badge, .count, h1, h2, h3')) {
    if (!vis(el)) continue;
    const cs = getComputedStyle(el);
    if (cs.textOverflow === 'ellipsis') continue;
    if (el.scrollWidth > el.clientWidth + 1 && cs.overflowX !== 'visible') { out.push('clipped in own box: ' + name(el)); continue; }
    let a = el.parentElement;
    while (a && a !== document.body) {
      const acs = getComputedStyle(a);
      if (acs.overflowX !== 'visible' && acs.overflowX !== 'auto' && acs.overflowX !== 'scroll') {
        const r = el.getBoundingClientRect(), ar = a.getBoundingClientRect();
        if (r.right > ar.right + 1 || r.left < ar.left - 1) out.push('clipped by ' + (a.id || String(a.className).split(' ')[0]) + ': ' + name(el));
        break;
      }
      if (acs.overflowX === 'auto' || acs.overflowX === 'scroll') break;
      a = a.parentElement;
    }
  }
  // 2. The last chat item ends above the composer once the log is scrolled to the end.
  const log = document.querySelector('#chat:not([hidden]) [role=log]'), comp = document.querySelector('#chat:not([hidden]) .chat-compose');
  if (log && comp && vis(log) && vis(comp)) {
    log.scrollTop = log.scrollHeight;
    const items = [...log.querySelectorAll('.chat-message, .chat-reply, .chat-empty, button')].filter(vis);
    const last = items[items.length - 1];
    if (last && last.getBoundingClientRect().bottom > comp.getBoundingClientRect().top + 1) out.push('last chat item runs under the composer');
  }
  // 2b. Reflow at phone widths.
  if (w <= 390 && !desktopOnly) for (const el of document.querySelectorAll('button, a, [role=tab], h1, h2, h3, p, .tag, .badge')) {
    if (!vis(el) || el.closest('.sr')) continue;
    if (el.getBoundingClientRect().right > innerWidth + 1) out.push('runs off the screen at ' + w + ' px: ' + name(el));
  }
  if (w <= 390 && !desktopOnly && document.documentElement.scrollWidth > innerWidth + 1) out.push('page scrolls sideways at ' + w + ' px');
  // 3. Headers at 1024 px and up: title, Chat and the primary action share the first row.
  if (w >= 1024) for (const bar of document.querySelectorAll('.center > .bar, main > .bar')) {
    if (!vis(bar)) continue;
    const h1 = bar.querySelector('h1');
    if (!h1) continue;
    const top = h1.getBoundingClientRect().top;
    for (const el of bar.querySelectorAll('#chat-toggle, #add-card, [data-action="new-card"], a.btn.ghost'))
      if (vis(el) && Math.abs(el.getBoundingClientRect().top - top) > 14) out.push('header: ' + name(el) + ' drops below the title row');
  }
  return out;
}"""

AA_JS = """(w) => {
  const out = [];
  const vis = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0 && r.left < innerWidth && cs.visibility !== 'hidden' && cs.opacity !== '0' && !el.closest('[hidden], .sr, [inert], [aria-hidden="true"]'); };
  const name = (el) => (el.id ? '#' + el.id : el.tagName.toLowerCase() + (typeof el.className === 'string' && el.className ? '.' + el.className.split(' ')[0] : ''))
    + ' “' + (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 30) + '”';
  const rgba = (s) => { const m = s.match(/rgba?\\(([^)]+)\\)/); if (!m) return null;
    const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
  const lum = ([r, g, b]) => { const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const background = (el) => {
    const layers = [];
    for (let a = el; a; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (cs.backgroundImage && cs.backgroundImage !== 'none') return null;
      const c = rgba(cs.backgroundColor);
      if (c && c[3] > 0) { layers.push(c); if (c[3] >= 1) break; }
    }
    let base = [255, 255, 255];
    for (const [r, g, b, a] of layers.reverse()) base = [r * a + base[0] * (1 - a), g * a + base[1] * (1 - a), b * a + base[2] * (1 - a)];
    return base;
  };
  // AA contrast for text a person reads.
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const el = n.parentElement;
    if (!el || seen.has(el) || !n.textContent.trim() || !vis(el)) continue;
    seen.add(el);
    if (el.closest('button:disabled, [aria-disabled="true"], input:disabled, select:disabled, option')) continue;
    const cs = getComputedStyle(el), fg = rgba(cs.color), bg = background(el);
    if (!fg || !bg) continue;
    const mix = [fg[0] * fg[3] + bg[0] * (1 - fg[3]), fg[1] * fg[3] + bg[1] * (1 - fg[3]), fg[2] * fg[3] + bg[2] * (1 - fg[3])];
    const [hi, lo] = [lum(mix), lum(bg)].sort((a, b) => b - a), ratio = (hi + 0.05) / (lo + 0.05);
    const size = parseFloat(cs.fontSize), large = size >= 24 || (size >= 18.66 && Number(cs.fontWeight) >= 700);
    if (ratio < (large ? 3 : 4.5)) out.push('contrast ' + ratio.toFixed(2) + ':1: ' + name(el));
  }
  // Target size (WCAG 2.5.8; 44 px on phones). Inline links in running text are exempt.
  const min = w < 700 ? 44 : 24;
  for (const el of document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=tab]')) {
    if (!vis(el)) continue;
    if (el.matches('input[type=checkbox], input[type=radio]') && el.closest('label')) continue;
    if (el.tagName === 'A' && [...el.parentElement.childNodes].some((c) => c !== el && c.nodeType === 3 && c.textContent.trim())) continue;
    const r = el.getBoundingClientRect();
    if (r.width < min - 0.5 || r.height < min - 0.5) out.push('target ' + Math.round(r.width) + '×' + Math.round(r.height) + ' < ' + min + ': ' + name(el));
  }
  return out;
}"""


def setup(page, step):
    if step is None:
        return
    if step == "open-chat":
        page.click("#chat-toggle")
        page.wait_for_selector("#chat:not([hidden]) .chat-stream", timeout=5000)
    elif step == "pick-a121":
        # Phones show one column at a time; A-121 sits in Review.
        jump = page.locator('[data-board-column="review"]')
        if jump.is_visible():
            jump.click()
        page.click('.workbench-board .card[data-card="121"] h3')
        page.wait_for_selector("#card-panel:not([hidden]) .detail-h", timeout=5000)
    elif step == "add-card":
        page.locator('#head-actions [data-action="new-card"]').first.click()
    elif step == "approve":
        # From 700 px the first item (the approval) is already open beside the list.
        approve = page.locator("[data-needs-approve]").first
        if not approve.is_visible():
            page.locator("[data-needs-select]", has_text="Send beta 0.9").first.click()
            settle(page, 300)
        approve.click()
    elif step == "settings":
        page.locator('[data-action="coordinator"]:visible').first.click()
        page.wait_for_selector(".workbench-project", timeout=5000)
    elif step == "palette":
        page.click("#cmdk")
    elif step == "shortcuts":
        page.click("#shortcuts-link")
    elif step == "create-project":
        page.click("#new-project")
    settle(page, 500)


def sizes(kind):
    if kind == "desktop":
        return [s for s in WIDTHS if s[0] >= 768]
    return WIDTHS + PHONE_EXTRA


@pytest.mark.parametrize("render", [s[0] for s in SCENARIOS])
def test_layout(render, ui, app):
    name, hash_, kind, step, _design = next(s for s in SCENARIOS if s[0] == render)
    found = {}
    for width, height in sizes(kind):
        page = ui.open(hash_, (width, height))
        setup(page, step)
        desktop_only = kind == "desktop"
        issues = page.evaluate(LAYOUT_JS, [width, desktop_only])
        if kind == "legacy":
            issues += page.evaluate(AA_JS, width)
        issues = sorted(set(issues))
        shots.capture(page, name, width)
        append_log("layout.jsonl", {"render": f"{name}@{width}", "issues": issues})
        if issues:
            found[width] = issues
    csp = ui.csp_violations()
    assert not csp, f"CSP violations on {name}: {json.dumps(csp, ensure_ascii=False)}"
    assert not found, f"{name}: {json.dumps(found, ensure_ascii=False)}"
