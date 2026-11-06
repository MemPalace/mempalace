import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";

const path = new URL("../commonplace/web/static/chatdock.js", import.meta.url);
assert.ok(existsSync(path), "Chat dock module missing");
const module = await import(`data:text/javascript;base64,${Buffer.from(readFileSync(path, "utf8")).toString("base64")}`);
for (const name of ["createDock", "toggleLabel", "layoutMode", "nextFocus", "inertTargets"]) {
  assert.equal(typeof module[name], "function", `${name} export missing`);
}
const { createDock, toggleLabel, layoutMode, nextFocus, inertTargets } = module;

// Small DOM fixture: exercises real dock event handlers and element state, without a layout engine.
class Events {
  listeners = new Map();
  addEventListener(name, fn) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(fn);
  }
  removeEventListener(name, fn) { this.listeners.get(name)?.delete(fn); }
  emit(name, event = {}) {
    event.preventDefault = () => { event.defaultPrevented = true; };
    for (const fn of this.listeners.get(name) || []) fn(event);
    return event;
  }
}
class Element extends Events {
  constructor(tag, doc) {
    super();
    this.tagName = tag.toUpperCase(); this.ownerDocument = doc;
    this.attrs = new Map(); this.children = []; this.dataset = {}; this.parentElement = null;
    this.hidden = false; this.disabled = false; this.textContent = ""; this.value = "";
    const self = this;
    this.classList = {
      contains: (name) => self.className.split(/\s+/).includes(name),
      add: (...names) => { self.className = [...new Set([...self.className.split(/\s+/), ...names])].filter(Boolean).join(" "); },
      remove: (...names) => { self.className = self.className.split(/\s+/).filter((name) => !names.includes(name)).join(" "); },
      toggle: (name, force) => { const on = force ?? !self.classList.contains(name); self.classList[on ? "add" : "remove"](name); return on; },
    };
  }
  get className() { return this.attrs.get("class") || ""; }
  set className(value) { this.attrs.set("class", value); }
  get id() { return this.attrs.get("id") || ""; }
  set id(value) { this.attrs.set("id", value); }
  get tabIndex() {
    if (this.attrs.has("tabindex")) return Number(this.attrs.get("tabindex"));
    return /^(BUTTON|INPUT|TEXTAREA|SELECT)$/.test(this.tagName) || this.tagName === "A" && this.hasAttribute("href") ? 0 : -1;
  }
  get isConnected() { return this.ownerDocument.body.contains(this); }
  get isContentEditable() { return this.getAttribute("contenteditable") === "true" || !!this.parentElement?.isContentEditable; }
  set innerHTML(value) { this.html = value; this.replaceChildren(); }
  get innerHTML() { return this.html || ""; }
  setAttribute(name, value) { this.attrs.set(name, String(value)); if (name === "hidden") this.hidden = true; }
  getAttribute(name) {
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      return this.dataset[key] ?? this.attrs.get(name) ?? null;
    }
    if (name === "hidden" && this.hidden) return "";
    return this.attrs.get(name) ?? null;
  }
  hasAttribute(name) { return this.getAttribute(name) !== null; }
  removeAttribute(name) { this.attrs.delete(name); if (name === "hidden") this.hidden = false; }
  append(...nodes) { for (const node of nodes) { node.remove(); node.parentElement = this; this.children.push(node); } }
  replaceChildren(...nodes) { for (const node of this.children) node.parentElement = null; this.children = []; this.append(...nodes); }
  remove() {
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((node) => node !== this);
    this.parentElement = null;
  }
  contains(node) { return this === node || this.children.some((child) => child.contains(node)); }
  matches(selector) {
    return selector.split(/,\s*/).some((part) => {
      let no = false;
      part = part.replace(/:not\(([^)]+)\)/g, (_, inside) => { if (this.matches(inside)) no = true; return ""; });
      if (no) return false;
      const tag = part.match(/^[a-z][a-z0-9]*/i)?.[0];
      if (tag && tag.toUpperCase() !== this.tagName) return false;
      const id = part.match(/#([\w-]+)/)?.[1];
      if (id && id !== this.id) return false;
      if ([...part.matchAll(/\.([\w-]+)/g)].some((m) => !this.classList.contains(m[1]))) return false;
      return [...part.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)].every((m) => this.hasAttribute(m[1]) && (m[2] === undefined || this.getAttribute(m[1]) === m[2]));
    });
  }
  closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
  querySelectorAll(selector) { return this.children.flatMap((node) => [...(node.matches(selector) ? [node] : []), ...node.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getClientRects() { return this.closest("[hidden]") ? [] : [{}]; }
  focus() { if (!this.closest("[hidden], [inert]")) this.ownerDocument.activeElement = this; }
  scrollIntoView() { this.scrolled = true; }
}
function fixture(width = 1440) {
  const win = new Events(); win.innerWidth = width;
  const doc = new Events(); doc.defaultView = win;
  doc.createElement = (tag) => new Element(tag, doc);
  doc.body = doc.createElement("body"); doc.activeElement = doc.body;
  doc.querySelector = (s) => doc.body.querySelector(s);
  doc.querySelectorAll = (s) => doc.body.querySelectorAll(s);
  const add = (parent, tag, cls = "", id = "") => {
    const el = doc.createElement(tag); el.className = cls; el.id = id; parent.append(el); return el;
  };
  const shell = add(doc.body, "div", "shell");
  const rail = add(shell, "nav", "rail");
  const main = add(shell, "main", "center", "main");
  const header = add(main, "header", "bar");
  const ctx = add(shell, "aside", "context", "ctx");
  const detailsTitle = add(ctx, "h2"); detailsTitle.setAttribute("data-details-title", ""); detailsTitle.setAttribute("tabindex", "-1");
  const tabs = add(shell, "nav", "m-tabs");
  const ask = add(main, "button", "", "ask-question"); ask.setAttribute("aria-controls", "chat");
  const calls = { show: [], focus: 0, stop: 0, quotes: [], reopened: 0, open: [] };
  const chat = {
    show(slug, options) {
      calls.show.push([slug, options]);
      if (!shell.querySelector("textarea")) add(shell.querySelector("#chat-content"), "textarea", "", "composer");
    },
    focusComposer() { calls.focus++; shell.querySelector("textarea")?.focus(); },
    setQuote(quote) { calls.quotes.push(quote); },
    reopened() { calls.reopened++; },
    setOpen(value) { calls.open.push(value); },
    stop() { calls.stop++; },
  };
  const dock = createDock({ root: shell, chat, api: () => assert.fail("Dock must not fetch") });
  for (const name of ["mount", "open", "close", "backToChat", "isOpen", "update", "destroy"]) assert.equal(typeof dock[name], "function", `${name} missing`);
  const board = { slug: "atlas", name: "Atlas", coordinator: "conn:Iris" };
  const toggle = dock.mount(header, { board, label: "Chat" });
  const pane = shell.querySelector("#chat"); assert.ok(pane, "Chat pane missing");
  const key = (key, target = doc.activeElement, extras = {}) => doc.emit("keydown", { key, target, ...extras });
  const click = (target) => shell.emit("click", { target });
  return { win, doc, add, shell, rail, main, header, ctx, detailsTitle, tabs, ask, calls, chat, dock, board, toggle, pane, key, click };
}
const which = process.argv[2];
switch (which) {
  case "label_plain": assert.equal(toggleLabel(), "Chat"); break;
  case "label_unread":
    assert.equal(toggleLabel({ unread: 1 }), "Chat 1 new reply");
    assert.equal(toggleLabel({ unread: 3 }), "Chat 3 new replies");
    for (const unread of [-1, NaN, "3", 0.4]) assert.equal(toggleLabel({ unread }), "Chat");
    assert.equal(toggleLabel({ unread: 3, open: true }), "Chat"); break;
  case "label_missing":
    assert.equal(toggleLabel({ hasOrchestrator: false, unread: 3 }), "Chat · no orchestrator yet");
    assert.equal(toggleLabel({ hasOrchestrator: false, open: true }), "Chat · no orchestrator yet"); break;
  case "label_project":
    assert.equal(toggleLabel({ label: "Atlas chat" }), "Atlas chat");
    assert.equal(toggleLabel({ label: "Harbor chat", unread: 2 }), "Harbor chat 2 new replies"); break;
  case "slot": assert.equal(layoutMode(1440), "slot"); break;
  case "push": assert.equal(layoutMode(1024), "push"); break;
  case "sheet": assert.equal(layoutMode(768), "sheet"); break;
  case "phone": assert.equal(layoutMode(390), "phone"); break;
  case "boundaries":
    for (const [width, mode] of [[1100, "slot"], [1099, "push"], [1023, "sheet"], [700, "sheet"], [699, "phone"], [320, "phone"]]) assert.equal(layoutMode(width), mode); break;
  case "focus_wrap": {
    const a = {}, b = {}, c = {};
    assert.equal(nextFocus([a, b, c], c), a);
    assert.equal(nextFocus([a, b, c], a), b);
    assert.equal(nextFocus([], a), null); break;
  }
  case "focus_reverse": {
    const a = {}, b = {};
    assert.equal(nextFocus([a, b], a, true), b);
    assert.equal(nextFocus([a], a, true), a);
    assert.equal(nextFocus([a, b], {}), a);
    assert.equal(nextFocus([a, b], {}, true), b); break;
  }
  case "inert_regions":
    for (const mode of ["sheet", "phone"]) {
      const selectors = inertTargets(mode);
      assert.ok(selectors.includes(".rail")); assert.ok(selectors.includes("#main"));
    }
    for (const mode of ["slot", "push"]) assert.deepEqual(inertTargets(mode), []); break;
  case "open_focus": {
    const f = fixture(); await f.dock.open();
    assert.equal(f.dock.isOpen(), true); assert.equal(f.pane.hidden, false);
    assert.equal(f.doc.activeElement.id, "composer"); assert.equal(f.ctx.hidden, true);
    assert.equal(f.toggle.getAttribute("aria-expanded"), "true");
    assert.equal(f.ask.getAttribute("aria-expanded"), "true");
    assert.equal(f.pane.getAttribute("aria-labelledby"), "chat-h"); break;
  }
  case "close_focus": {
    const f = fixture(); await f.dock.open(); const box = f.shell.querySelector("textarea"); box.value = "Exact draft\nkept here";
    f.dock.close(); assert.equal(f.doc.activeElement, f.toggle); assert.equal(f.dock.isOpen(), false);
    assert.equal(f.ctx.hidden, false); assert.equal(f.toggle.getAttribute("aria-expanded"), "false");
    assert.equal(f.ask.getAttribute("aria-expanded"), "false"); assert.equal(box.value, "Exact draft\nkept here");
    assert.equal(f.calls.stop, 0); await f.dock.open(); assert.equal(f.calls.show.length, 1); break;
  }
  case "context_focus": {
    const f = fixture(768); await f.dock.open({ from: f.ask }); f.dock.close(); assert.equal(f.doc.activeElement, f.ask);
    await f.dock.open({ from: f.ask }); f.ask.remove(); f.dock.close(); assert.equal(f.doc.activeElement, f.toggle); break;
  }
  case "modal": {
    for (const width of [768, 390]) {
      const f = fixture(width); f.rail.setAttribute("inert", ""); await f.dock.open();
      assert.equal(f.pane.getAttribute("role"), "dialog"); assert.equal(f.pane.getAttribute("aria-modal"), "true");
      assert.ok(f.main.hasAttribute("inert")); assert.ok(f.rail.hasAttribute("inert")); assert.ok(f.tabs.hasAttribute("inert"));
      assert.ok(!f.pane.hasAttribute("inert")); f.dock.close();
      assert.ok(!f.main.hasAttribute("inert")); assert.ok(f.rail.hasAttribute("inert")); assert.ok(!f.tabs.hasAttribute("inert"));
      assert.equal(f.pane.getAttribute("role"), null); assert.equal(f.pane.getAttribute("aria-modal"), null);
    } break;
  }
  case "desktop": {
    for (const width of [1440, 1024]) {
      const f = fixture(width); await f.dock.open(); assert.equal(f.pane.getAttribute("aria-modal"), null);
      assert.ok(!f.main.hasAttribute("inert")); assert.equal(f.pane.dataset.layout, layoutMode(width));
    } break;
  }
  case "resize": {
    const f = fixture(); await f.dock.open();
    f.win.innerWidth = 768; f.win.emit("resize"); assert.equal(f.pane.getAttribute("aria-modal"), "true");
    f.win.innerWidth = 1024; f.win.emit("resize"); assert.equal(f.pane.getAttribute("aria-modal"), null);
    assert.ok(!f.main.hasAttribute("inert")); assert.ok(f.dock.isOpen()); break;
  }
  case "tab_trap": {
    const f = fixture(768); await f.dock.open(); const box = f.shell.querySelector("textarea"); const close = f.shell.querySelector("#chat-close");
    const hidden = f.add(f.pane, "button"); hidden.hidden = true;
    const disabled = f.add(f.pane, "button"); disabled.disabled = true;
    box.focus(); assert.ok(f.key("Tab").defaultPrevented); assert.equal(f.doc.activeElement, close);
    f.key("Tab", close, { shiftKey: true }); assert.equal(f.doc.activeElement, box);
    f.key("Tab", box, { shiftKey: true }); assert.equal(f.doc.activeElement, close); break;
  }
  case "keys": {
    const f = fixture(); assert.ok(f.key("/", f.toggle).defaultPrevented); assert.ok(f.dock.isOpen());
    f.key("]", f.toggle); assert.ok(!f.dock.isOpen());
    await f.dock.open(); f.key("Escape", f.main); assert.ok(f.dock.isOpen());
    f.key("Escape", f.shell.querySelector("textarea")); assert.ok(!f.dock.isOpen()); assert.equal(f.doc.activeElement, f.toggle);
    f.click(f.toggle); assert.ok(f.dock.isOpen()); f.click(f.shell.querySelector("#chat-close")); assert.ok(!f.dock.isOpen()); break;
  }
  case "typing": {
    const f = fixture();
    for (const tag of ["input", "textarea", "select"]) {
      const field = f.add(f.main, tag); f.key("/", field); assert.ok(!f.dock.isOpen());
    }
    const editor = f.add(f.main, "div"); editor.setAttribute("contenteditable", "true"); const text = f.add(editor, "span");
    f.key("/", text); assert.ok(!f.dock.isOpen());
    for (const flag of ["ctrlKey", "metaKey", "altKey", "isComposing", "defaultPrevented"]) { f.key("/", f.toggle, { [flag]: true }); assert.ok(!f.dock.isOpen()); }
    await f.dock.open(); f.key("]", f.shell.querySelector("textarea")); assert.ok(f.dock.isOpen()); break;
  }
  case "disabled_keys": {
    const f = fixture(); f.shell.dataset.singleKeys = "off"; f.key("/", f.toggle); assert.ok(!f.dock.isOpen());
    f.click(f.toggle); assert.ok(f.dock.isOpen()); f.key("Escape", f.shell.querySelector("textarea")); assert.ok(!f.dock.isOpen());
    f.shell.dataset.singleKeys = "on"; const sw = f.add(f.main, "button"); sw.setAttribute("data-single-keys", ""); sw.setAttribute("aria-checked", "false");
    f.key("/", f.toggle); assert.ok(!f.dock.isOpen()); f.header.remove(); sw.remove(); f.key("/", f.main); assert.ok(!f.dock.isOpen()); break;
  }
  case "nested_dialog": {
    const f = fixture(); await f.dock.open(); const modal = f.add(f.doc.body, "dialog"); modal.setAttribute("aria-modal", "true");
    f.key("Escape", modal); f.key("]", modal); assert.ok(f.dock.isOpen()); modal.remove();
    f.key("Escape", f.shell.querySelector("textarea")); assert.ok(!f.dock.isOpen()); break;
  }
  case "strips": {
    const f = fixture(); const text = 'You are asking about <an exact title>.\nApprove or decline it in Needs me.';
    await f.dock.open({ from: f.ask, strip: { text, backLabel: "Back to the approval" } });
    const strip = f.pane.querySelector(".chat-about"); assert.ok(strip); assert.equal(strip.hidden, false);
    assert.equal(strip.querySelector("span").textContent, text); assert.equal(strip.querySelector("button").textContent, "Back to the approval");
    f.click(strip.querySelector("button")); assert.equal(f.doc.activeElement, f.ask);
    await f.dock.open(); assert.equal(strip.hidden, true); break;
  }
  case "details_row": {
    const f = fixture(768); await f.dock.open({ from: f.ask, strip: { text: "Exact context" } });
    const row = f.add(f.pane.querySelector("#chat-content"), "button"); row.dataset.src = "the 12:41 quick reply (local model)";
    f.dock.close({ toDetails: true, row }); assert.ok(!f.dock.isOpen()); assert.equal(f.ctx.hidden, false);
    assert.ok(f.shell.classList.contains("ctx-pushed")); assert.equal(f.ctx.getAttribute("aria-modal"), "true"); assert.equal(f.doc.activeElement, f.detailsTitle);
    assert.ok(f.main.hasAttribute("inert")); await f.dock.backToChat();
    assert.equal(f.doc.activeElement, row); assert.ok(row.scrolled); assert.equal(f.ctx.hidden, true);
    assert.equal(f.ctx.getAttribute("aria-modal"), null);
    assert.equal(f.pane.querySelector(".chat-about").querySelector("span").textContent, "Exact context");
    f.dock.close(); assert.equal(f.doc.activeElement, f.ask); break;
  }
  case "details_board": {
    const f = fixture(768); await f.dock.open(); f.dock.close({ toDetails: true });
    f.key("]", f.detailsTitle); assert.ok(f.dock.isOpen());
    f.dock.close({ toDetails: true }); f.key("Escape", f.detailsTitle); assert.ok(f.dock.isOpen());
    f.dock.close({ toDetails: true }); const hide = f.add(f.ctx, "button"); hide.setAttribute("data-action", "close-panel"); f.click(hide); assert.ok(f.dock.isOpen()); break;
  }
  case "scrim": {
    const f = fixture(768); await f.dock.open(); f.click(f.shell.querySelector(".chat-scrim")); assert.ok(!f.dock.isOpen()); assert.equal(f.doc.activeElement, f.toggle);
    await f.dock.open(); f.dock.close({ toDetails: true }); f.click(f.shell.querySelector(".chat-scrim")); assert.ok(f.dock.isOpen()); break;
  }
  case "announcement": {
    const f = fixture(); f.dock.update({ unread: 2 });
    const line = f.header.querySelector('[role="status"]'); assert.ok(line); assert.equal(line.textContent, "2 new replies in Atlas chat");
    assert.equal(f.toggle.getAttribute("aria-label"), "Chat 2 new replies");
    await f.dock.open(); assert.equal(line.textContent, ""); assert.equal(f.toggle.getAttribute("aria-label"), "Chat");
    f.dock.close(); f.dock.update({ hasOrchestrator: false }); assert.equal(line.textContent, "");
    assert.equal(f.toggle.getAttribute("aria-label"), "Chat · no orchestrator yet"); break;
  }
  case "quote_jump": {
    const f = fixture(); const quote = { text: "Exact\nwords", author: "Leon" };
    await f.dock.open({ from: f.ask, at: 7, quote }); assert.deepEqual(f.calls.show[0], ["atlas", { at: 7, quote, name: "Atlas" }]);
    await f.dock.open({ quote }); assert.equal(f.calls.quotes[0], quote);
    f.dock.close({ toDetails: true }); await f.dock.backToChat(); assert.equal(f.calls.quotes.length, 1);
    f.dock.close(); await f.dock.open(); assert.equal(f.calls.quotes.at(-1), null); break;
  }
  case "async_close": {
    const f = fixture(); let resolve; const original = f.chat.show;
    f.chat.show = (...args) => { original(...args); return new Promise((done) => { resolve = done; }); };
    const pending = f.dock.open(); f.dock.close(); resolve(); await pending;
    assert.equal(f.doc.activeElement, f.toggle); assert.ok(!f.dock.isOpen()); break;
  }
  case "remount": {
    const f = fixture(); f.ctx.hidden = true; f.shell.classList.add("two"); await f.dock.open(); f.dock.close();
    assert.equal(f.ctx.hidden, true); assert.ok(f.shell.classList.contains("two"));
    await f.dock.open(); const header = f.add(f.main, "header"); f.dock.mount(header, { board: { slug: "harbor", name: "Harbor" }, label: "Harbor chat" });
    assert.equal(f.header.querySelector("#chat-toggle"), null); assert.equal(f.calls.stop, 1);
    await f.dock.open(); assert.equal(f.calls.show.at(-1)[0], "harbor");
    const heading = f.pane.querySelector("h2"); assert.ok(heading, "Chat heading missing");
    assert.equal(heading.textContent, "Harbor chat"); assert.equal(f.shell.querySelectorAll("#chat").length, 1); break;
  }
  case "destroy": {
    const f = fixture(768); await f.dock.open(); f.dock.destroy(); assert.ok(!f.main.hasAttribute("inert"));
    assert.equal(f.shell.querySelector("#chat"), null); assert.equal(f.doc.listeners.get("keydown").size, 0);
    assert.equal(f.win.listeners.get("resize").size, 0); assert.equal(f.shell.listeners.get("click").size, 0); assert.equal(f.calls.stop, 1); break;
  }
  case "css": {
    const path = new URL("../commonplace/web/static/chatdock.css", import.meta.url); assert.ok(existsSync(path), "Dock stylesheet missing");
    const css = readFileSync(path, "utf8");
    for (const value of ["var(--surface", "var(--text)", "var(--accent)", "min-height: 44px", "100dvh", "1023px", "699px", ".chatpane[hidden]", ".chat-about", ".chat-dock-content"]) assert.ok(css.includes(value), value);
    break;
  }
  case "reopen_baseline": {
    // First open shows the board; a later reopen (not a return from Details) moves the divider.
    const f = fixture(); await f.dock.open(); assert.equal(f.calls.reopened, 0);
    f.dock.close(); await f.dock.open(); assert.equal(f.calls.reopened, 1);
    f.dock.close({ toDetails: true }); await f.dock.backToChat(); assert.equal(f.calls.reopened, 1); break;
  }
  case "open_state": {
    // The controller learns every open and close, so a closed pane never marks chat seen.
    const f = fixture(); await f.dock.open(); assert.deepEqual(f.calls.open, [true]);
    f.dock.close(); assert.deepEqual(f.calls.open, [true, false]);
    await f.dock.open(); f.dock.close({ toDetails: true }); await f.dock.backToChat();
    assert.deepEqual(f.calls.open, [true, false, true, false, true]); break;
  }
  case "open_promise": {
    const f = fixture(); f.toggle.remove();
    const result = f.dock.open(); assert.ok(result instanceof Promise, "open must return a Promise"); await result; break;
  }
  default: assert.fail(`Unknown case: ${which}`);
}
