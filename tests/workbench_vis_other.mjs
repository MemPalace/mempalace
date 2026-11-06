import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// A small document: enough markup parsing and selector matching for the project dialogs.
const VOID = new Set(["input", "br", "img", "hr", "meta", "link"]);
const unescape = (value) => value.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");

class Text {
  constructor(text) { this.text = text; this.parent = null; }
  get textContent() { return this.text; }
}

class El {
  constructor(tag, doc) {
    Object.assign(this, { localName: tag.toLowerCase(), doc, attrs: new Map(), children: [], parent: null, listeners: {}, _value: null });
  }
  get tagName() { return this.localName.toUpperCase(); }
  get isConnected() { let node = this; while (node.parent) node = node.parent; return node === this.doc.documentElement; }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  hasAttribute(name) { return this.attrs.has(name); }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  removeAttribute(name) { this.attrs.delete(name); }
  toggleAttribute(name, on) { if (on) this.setAttribute(name, ""); else this.removeAttribute(name); }
  get id() { return this.getAttribute("id") || ""; }
  set id(value) { this.setAttribute("id", value); }
  get className() { return this.getAttribute("class") || ""; }
  set className(value) { this.setAttribute("class", value); }
  get classList() {
    const names = () => this.className.split(/\s+/).filter(Boolean);
    return { contains: (name) => names().includes(name), add: (name) => { if (!names().includes(name)) this.className = [...names(), name].join(" "); }, remove: (name) => { this.className = names().filter((n) => n !== name).join(" "); } };
  }
  get dataset() {
    return new Proxy({}, { get: (_, key) => this.getAttribute(`data-${String(key).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`) ?? undefined });
  }
  get disabled() { return this.hasAttribute("disabled"); }
  set disabled(value) { this.toggleAttribute("disabled", !!value); }
  get hidden() { return this.hasAttribute("hidden"); }
  set hidden(value) { this.toggleAttribute("hidden", !!value); }
  get value() {
    if (this._value !== null) return this._value;
    if (this.localName === "select") { const options = this.querySelectorAll("option"); return (options.find((o) => o.hasAttribute("selected")) || options[0])?.value ?? ""; }
    return this.getAttribute("value") ?? "";
  }
  set value(value) { this._value = String(value); }
  get textContent() { return this.children.map((child) => child.textContent).join(""); }
  set textContent(value) { this.replaceChildren(new Text(String(value))); }
  set innerHTML(html) { this.replaceChildren(...parse(html, this.doc)); }
  get innerHTML() { return this.textContent; }
  append(...nodes) {
    for (const node of nodes) {
      node.parent?.children.splice(node.parent.children.indexOf(node), 1);
      node.parent = this; this.children.push(node);
    }
  }
  replaceChildren(...nodes) { for (const child of this.children) child.parent = null; this.children = []; this.append(...nodes); }
  remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } }
  contains(node) { for (let n = node; n; n = n.parent) if (n === this) return true; return false; }
  elements() { return this.children.filter((c) => c instanceof El).flatMap((c) => [c, ...c.elements()]); }
  querySelectorAll(selector) { return this.elements().filter((el) => matches(el, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  matches(selector) { return matches(this, selector); }
  closest(selector) { for (let n = this; n instanceof El; n = n.parent) if (matches(n, selector)) return n; return null; }
  getClientRects() { return this.isConnected && !this.closest("[hidden]") ? [{}] : []; }
  focus() { if (this.isConnected) this.doc.activeElement = this; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); }
  dispatch(type, extra = {}) {
    const event = { type, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}, stopImmediatePropagation() {}, ...extra };
    for (const fn of this.listeners[type] || []) fn(event);
    return event;
  }
  get open() { return this.hasAttribute("open"); }
  showModal() { this.setAttribute("open", ""); }
  close() { this.removeAttribute("open"); for (const fn of this.listeners.close || []) fn({}); }
  click() { return this.onclick?.({ currentTarget: this, target: this, preventDefault() {}, stopPropagation() {} }); }
}

function parse(html, doc) {
  const root = new El("root", doc), stack = [root];
  for (const m of html.matchAll(/<!--[\s\S]*?-->|<\/([a-zA-Z0-9]+)\s*>|<([a-zA-Z0-9]+)((?:\s+[^\s=>/]+(?:="[^"]*")?)*)\s*\/?>|([^<]+)/g)) {
    const [, close, open, attrs, text] = m;
    if (text) stack.at(-1).append(new Text(unescape(text)));
    else if (close) { while (stack.length > 1 && stack.pop().localName !== close.toLowerCase()); }
    else if (open) {
      const el = new El(open, doc);
      for (const a of (attrs || "").matchAll(/([^\s=]+)(?:="([^"]*)")?/g)) el.setAttribute(a[1], unescape(a[2] ?? ""));
      stack.at(-1).append(el);
      if (!VOID.has(el.localName)) stack.push(el);
    }
  }
  const nodes = [...root.children]; root.replaceChildren(); return nodes;
}

function compound(el, part) {
  let rest = part;
  const tag = rest.match(/^[a-z0-9]+/i);
  if (tag) { if (el.localName !== tag[0].toLowerCase()) return false; rest = rest.slice(tag[0].length); }
  while (rest) {
    let m;
    if ((m = rest.match(/^:not\(([^()]*)\)/))) { if (compound(el, m[1])) return false; }
    else if ((m = rest.match(/^#([\w-]+)/))) { if (el.id !== m[1]) return false; }
    else if ((m = rest.match(/^\.([\w-]+)/))) { if (!el.classList.contains(m[1])) return false; }
    else if ((m = rest.match(/^\[([\w-]+)(?:="([^"]*)")?\]/))) { if (!el.hasAttribute(m[1]) || (m[2] !== undefined && el.getAttribute(m[1]) !== m[2])) return false; }
    else throw new Error(`unsupported selector ${part}`);
    rest = rest.slice(m[0].length);
  }
  return true;
}

function matches(el, selector) {
  return selector.split(",").some((one) => {
    const parts = one.trim().replace(/\s*>\s*/g, " > ").split(/\s+/);
    const test = (node, i) => {
      if (!(node instanceof El) || !compound(node, parts[i])) return false;
      if (i === 0) return true;
      if (parts[i - 1] === ">") return test(node.parent, i - 2);
      for (let up = node.parent; up instanceof El; up = up.parent) if (test(up, i - 1)) return true;
      return false;
    };
    return test(el, parts.length - 1);
  });
}

function makeDocument() {
  const doc = { listeners: {}, adoptedStyleSheets: [] };
  doc.documentElement = new El("html", doc);
  doc.body = new El("body", doc); doc.documentElement.append(doc.body);
  doc.activeElement = doc.body;
  doc.createElement = (tag) => new El(tag, doc);
  doc.querySelector = (s) => doc.documentElement.querySelector(s);
  doc.querySelectorAll = (s) => doc.documentElement.querySelectorAll(s);
  doc.addEventListener = (type, fn) => (doc.listeners[type] ||= []).push(fn);
  doc.removeEventListener = (type, fn) => { doc.listeners[type] = (doc.listeners[type] || []).filter((f) => f !== fn); };
  doc.key = (key, extra = {}) => {
    const event = { key, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}, stopImmediatePropagation() {}, ...extra };
    for (const fn of [...(doc.listeners.keydown || [])]) fn(event);
    return event;
  };
  return doc;
}


const doc = makeDocument();
globalThis.document = doc;
globalThis.window = { addEventListener() {}, removeEventListener() {} };
globalThis.CSSStyleSheet = class { replaceSync() {} };
const staticURL = new URL("../commonplace/web/static/", import.meta.url);
const read = (name) => readFileSync(new URL(name, staticURL), "utf8");
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const project = { board: "atlas", name: "Atlas", can_configure: true, main: { name: "orchestrator:atlas", connected: true, seen: true, status: "working", connectable: true }, limits: { max_agents: 3 } };

const cases = {
  // M8: the shortcuts sheet scrolls its table and keeps its footer in view; "Always on" is its own column (m14).
  async shortcuts_pinned() {
    const palette = await import(new URL("palette.js", staticURL));
    const dialog = doc.createElement("dialog"); doc.body.append(dialog);
    const ui = palette.createPalette({ dialog, storage: { getItem: () => null, setItem() {} }, onCommand() {} });
    ui.shortcuts();
    const body = dialog.querySelector(".shortcut-body");
    assert.ok(body, "shortcut sheet");
    const scroll = body.querySelector(".shortcut-scroll");
    assert.ok(scroll, "the sheet has a scrolling body");
    assert.ok(scroll.querySelector("table.kt"), "the table scrolls inside the body");
    const acts = body.querySelector(".acts");
    assert.ok(acts && !scroll.contains(acts), "the footer is outside the scrolling body");
    assert.ok(acts.querySelector('[data-palette-action="done"]'), "Done stays in the footer");
    const always = body.querySelectorAll("td.always").filter((td) => td.textContent === "Always on");
    assert.equal(always.length, 2, "⌘K and Esc say Always on in their own column");
    assert.ok(!body.querySelectorAll("td").some((td) => /· Always on/.test(td.textContent)), "the action text has no Always on suffix");
    const css = read("palette.js");
    assert.match(css, /\.shortcut-scroll \{[^}]*overflow:auto/, "the body scrolls");
    assert.match(css, /\.shortcut-body \{[^}]*max-height:calc\(100dvh - 112px\)/, "the sheet fits the dialog");
  },
  // m15: palette rows are flat; the legacy boxed .opt button look is reset.
  async palette_rows_flat() {
    const css = read("palette.js");
    const rule = /\.workbench-palette \.opt \{([^}]*)\}/.exec(css)?.[1] || "";
    for (const part of ["border:0", "box-shadow:none", "background:transparent", "font-weight:400"]) assert.ok(rule.includes(part), `palette row: ${part}`);
  },
  // m13: memory results show a readable local time and keep the ISO stamp in datetime.
  async memory_time() {
    const memory = await import(new URL("memory.js", staticURL));
    assert.equal(typeof memory.memoryTime, "function", "memoryTime missing");
    const at = "2026-10-28T16:12:00Z", local = new Date(at), hhmm = local.toTimeString().slice(0, 5);
    const day = local.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric" }).replace(",", "");
    assert.equal(memory.memoryTime(at, { now: new Date("2026-11-06T09:00:00Z") }), `${day}, ${hhmm}`);
    assert.equal(memory.memoryTime(at, { now: local }), `today, ${hhmm}`);
    assert.equal(memory.memoryTime(at, { dateOnly: true }), day);
    const row = { kind: "chat", id: 1, board: "atlas", text: "rate limit", author: "Leon", at: "2026-11-05T23:23:44.696457+00:00", sourceLabel: "Atlas chat" };
    const html = memory.renderMemory({ boards: [], results: [row, { ...row, id: 2, kind: "note" }], filters: { query: "rate" }, searched: true });
    assert.ok(html.includes(`datetime="${row.at}"`), "the ISO stamp stays in datetime");
    assert.ok(!html.includes(`>${row.at}<`), "no raw ISO stamp is shown");
  },
  // m13: filters look like chips (no native box) and the result list is not clipped by legacy CSS.
  async memory_chips() {
    const css = read("map.css");
    const select = /\.memory-search \.chip select \{([^}]*)\}/.exec(css)?.[1] || "";
    for (const part of ["appearance: none", "border: 0", "box-shadow: none"]) assert.ok(select.includes(part), `chip select: ${part}`);
    assert.match(css, /\.memory-search \.chip \{[^}]*border-radius: 999px/, "chips are pills");
    assert.match(css, /\.memory-search \.memory-results \{[^}]*max-height: none/, "the result list is not capped at 40%");
    assert.match(css, /\.memory-search \.memory-toolbar \{[^}]*box-shadow: none/, "no legacy toolbar frame");
  },
  // m16: settings header (name, "Project settings", save status) and the orchestrator row.
  async settings_header() {
    const ui = await import(new URL("projects.js", staticURL));
    const html = ui.projectSettingsHTML(project, e, { origin: "#/board/atlas" });
    const host = doc.createElement("div"); host.innerHTML = html;
    assert.equal(host.querySelector("#project-settings-title")?.textContent, "Atlas Project settings");
    assert.equal(host.querySelector("[data-project-saved]")?.textContent, "Changes save automatically");
    const back = host.querySelector("[data-project-back]");
    assert.equal(back.textContent, "Back to board", "the label stays exact; arrow and keys are drawn by CSS");
    assert.equal(back.getAttribute("data-keys"), "G B");
    host.innerHTML = ui.mainHTML(project, e);
    assert.ok(host.querySelector(".orch-row .av"), "avatar");
    assert.equal(host.querySelector(".orch-text b")?.textContent, "Atlas orchestrator");
    assert.equal(host.querySelector(".tag.ok")?.textContent, "Connected");
    assert.match(host.querySelector(".orch-text small")?.textContent || "", /connected · working · reads chat, makes cards/);
    assert.equal(ui.lastActive(new Date(Date.now() - 3 * 60000).toISOString()), "3 min ago");
    assert.equal(ui.lastActive(null), "Not seen yet");
  },
  // m16: every autosave updates the header status with its time.
  async saved_status() {
    const ui = await import(new URL("projects.js", staticURL));
    const host = doc.createElement("div"); doc.body.append(host);
    let saved = 0;
    ui.mountAgentLimit(host, { project, e, onSaved: () => saved++, api: async (_path, options) => ({ ...project, limits: options.body }) });
    await host.querySelector('[data-limit-step="1"]').click();
    assert.equal(saved, 1, "a saved limit reports to the header");
  },
};

assert.ok(process.argv[2] in cases, "unknown case");
await cases[process.argv[2]]();
process.stdout.write(`PASS ${process.argv[2]}\n`);
