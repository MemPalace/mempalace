// Chat fixes found by the browser acceptance run: row sources, local times, the board
// outline, project names, the dock's opener after a re-render, and the phone layout.
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

const STATIC = new URL("../commonplace/web/static/", import.meta.url);
const read = (name) => readFileSync(new URL(name, STATIC), "utf8");
const pad = (n) => String(n).padStart(2, "0");
const local = (iso) => { const d = new Date(iso); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function chatModule() {
  assert.ok(existsSync(new URL("chat.js", STATIC)), "chat.js missing");
  const ui = await import("../commonplace/web/static/chat.js");
  assert.equal(typeof ui.chatHTML, "function", "chatHTML missing");
  return ui;
}

const ME = { user: "maya" };
const BOARD = { slug: "atlas", name: "Atlas", coordinator: "conn:atlas" };
const REQUEST_AT = "2026-11-05T12:40:00Z", ANSWER_AT = "2026-11-05T12:44:00Z", REPLY_AT = "2026-11-05T12:41:00Z";
const page = () => ({
  project: BOARD,
  messages: [
    { id: 1, actor: "user:maya", text: "Why is A-121 stuck?", at: "2026-11-05T12:39:00Z" },
    { id: 2, actor: "user:maya", text: "Get beta 0.9 out", at: REQUEST_AT },
    { id: 3, actor: "conn:atlas", text: "I made two cards from your request.", at: ANSWER_AT },
  ],
  replies: [{ id: 9, message_id: 1, for_user: "maya", kind: "answer", text: "#121 waits on Leon.", model: "local:openai_compatible/fast-model", at: REPLY_AT }],
  cards: [{ number: 121, title: "Offline mode", board: "atlas", status: "review" }],
  linked: [
    { number: 134, title: "Draft beta 0.9 release notes", board: "atlas", message_id: 2 },
    { number: 135, title: "Check the sign-up email", board: "atlas", message_id: 2 },
  ],
});
const item = (html, id) => {
  const start = html.indexOf(`id="chat-message-${id}"`);
  assert.ok(start >= 0, `message ${id} missing`);
  const end = html.indexOf('id="chat-message-', start + 1);
  return html.slice(start, end < 0 ? undefined : end);
};
const unescape = (value) => value?.replace(/&(amp|lt|gt|quot|#39);/g, (_, name) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" })[name]);
const srcOf = (html, number) => unescape(html.match(new RegExp(`data-chat-card="${number}"[^>]*data-src="([^"]*)"`))?.[1]);

test("request rows sit under the orchestrator's answer and name it", async () => {
  const { chatHTML } = await chatModule();
  const html = chatHTML(page(), ME, e);
  const answer = item(html, 3), request = item(html, 2);
  assert.match(answer, /From this request · on the board now/, "the answer's item carries the request's cards");
  assert.match(answer, /data-chat-card="134"/);
  assert.match(answer, /data-chat-card="135"/);
  assert.doesNotMatch(request, /data-chat-card="13[45]"/, "the request item no longer repeats them");
  assert.equal(srcOf(html, 134), `the orchestrator's ${local(ANSWER_AT)} answer`);
  assert.equal(srcOf(html, 135), `the orchestrator's ${local(ANSWER_AT)} answer`);
});

test("an unanswered request keeps its rows and names the person's message", async () => {
  const { chatHTML } = await chatModule();
  const p = page(); p.messages = p.messages.slice(0, 2);
  const html = chatHTML(p, ME, e);
  assert.match(item(html, 2), /data-chat-card="134"/);
  assert.equal(srcOf(html, 134), `your ${local(REQUEST_AT)} message`);
});

test("a local quick reply row names the quick reply, never the model id", async () => {
  const { chatHTML } = await chatModule();
  const src = srcOf(chatHTML(page(), ME, e), 121);
  assert.ok(src, "the quick reply row has a data-src");
  assert.match(src, /^the \d\d:\d\d quick reply \(local model\)$/);
  assert.equal(src, `the ${local(REPLY_AT)} quick reply (local model)`);
  assert.doesNotMatch(src, /openai_compatible|fast-model/);
});

test("a hosted quick reply row says hosted provider", async () => {
  const { chatHTML } = await chatModule();
  const p = page(); p.replies[0].model = "anthropic/fast";
  assert.equal(srcOf(chatHTML(p, ME, e), 121), `the ${local(REPLY_AT)} quick reply (hosted provider)`);
});

test("message times read local HH:MM and keep the ISO stamp", async () => {
  const { chatHTML } = await chatModule();
  const html = chatHTML(page(), ME, e);
  assert.ok(html.includes(`<time datetime="${ANSWER_AT}">${local(ANSWER_AT)}</time>`), "local HH:MM text");
  assert.ok(!html.includes(`>${ANSWER_AT}</time>`), "no raw ISO text");
});

// A small DOM: enough for the dock's real event handlers and the chat controller's listeners.
class Events {
  listeners = new Map();
  addEventListener(name, fn) { if (!this.listeners.has(name)) this.listeners.set(name, new Set()); this.listeners.get(name).add(fn); }
  removeEventListener(name, fn) { this.listeners.get(name)?.delete(fn); }
  emit(name, event = {}) {
    event.preventDefault = () => { event.defaultPrevented = true; };
    for (const fn of this.listeners.get(name) || []) fn(event);
    return event;
  }
}
class Node extends Events {
  constructor(tag, doc) {
    super();
    Object.assign(this, { tagName: tag.toUpperCase(), ownerDocument: doc, attrs: new Map(), children: [], dataset: {}, parentElement: null, hidden: false, disabled: false, textContent: "" });
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
  get attributes() { return [...this.attrs.keys(), ...Object.keys(this.dataset).map((k) => `data-${k.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`)].map((name) => ({ name })); }
  get tabIndex() { return this.attrs.has("tabindex") ? Number(this.attrs.get("tabindex")) : /^(BUTTON|TEXTAREA)$/.test(this.tagName) ? 0 : -1; }
  get isConnected() { return this.ownerDocument.body.contains(this); }
  set innerHTML(value) { this.html = value; this.replaceChildren(); }
  get innerHTML() { return this.html || ""; }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  getAttribute(name) {
    if (name.startsWith("data-")) return this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] ?? this.attrs.get(name) ?? null;
    if (name === "hidden" && this.hidden) return "";
    return this.attrs.get(name) ?? null;
  }
  hasAttribute(name) { return this.getAttribute(name) !== null; }
  removeAttribute(name) { this.attrs.delete(name); }
  append(...nodes) { for (const node of nodes) { node.remove(); node.parentElement = this; this.children.push(node); } }
  replaceChildren(...nodes) { for (const node of this.children) node.parentElement = null; this.children = []; this.append(...nodes); }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((n) => n !== this); this.parentElement = null; }
  contains(node) { return this === node || this.children.some((child) => child.contains(node)); }
  matches(selector) {
    return selector.split(/,\s*/).some((part) => {
      let no = false;
      part = part.replace(/:not\(([^)]+)\)/g, (_, inside) => { if (this.matches(inside)) no = true; return ""; });
      if (no) return false;
      const last = part.trim().split(/\s+/).at(-1);
      const tag = last.match(/^[a-z][a-z0-9]*/i)?.[0];
      if (tag && tag.toUpperCase() !== this.tagName) return false;
      const id = last.match(/#([\w-]+)/)?.[1];
      if (id && id !== this.id) return false;
      if ([...last.matchAll(/\.([\w-]+)/g)].some((m) => !this.classList.contains(m[1]))) return false;
      return [...last.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)].every((m) => this.hasAttribute(m[1]) && (m[2] === undefined || this.getAttribute(m[1]) === m[2]));
    });
  }
  closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
  querySelectorAll(selector) { return this.children.flatMap((n) => [...(n.matches(selector) ? [n] : []), ...n.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getClientRects() { return this.closest("[hidden]") ? [] : [{}]; }
  focus() { if (!this.closest("[hidden], [inert]")) this.ownerDocument.activeElement = this; }
  scrollIntoView() {}
}
function dom() {
  const win = new Events(); win.innerWidth = 1440;
  const doc = new Events(); doc.defaultView = win;
  doc.createElement = (tag) => new Node(tag, doc);
  doc.body = doc.createElement("body"); doc.activeElement = doc.body;
  doc.querySelector = (s) => doc.body.querySelector(s);
  doc.querySelectorAll = (s) => doc.body.querySelectorAll(s);
  doc.getElementById = (id) => doc.body.querySelector(`#${id}`);
  const add = (parent, tag, cls = "", id = "") => { const el = doc.createElement(tag); el.className = cls; if (id) el.id = id; parent.append(el); return el; };
  return { win, doc, add };
}

async function dockFixture() {
  assert.ok(existsSync(new URL("chatdock.js", STATIC)), "chatdock.js missing");
  const { createDock } = await import("../commonplace/web/static/chatdock.js");
  assert.equal(typeof createDock, "function", "createDock missing");
  const { win, doc, add } = dom();
  const observers = [];
  win.MutationObserver = class { constructor(fn) { this.fn = fn; this.on = false; observers.push(this); } observe() { this.on = true; } disconnect() { this.on = false; } };
  const mutate = () => { for (const o of observers) if (o.on) o.fn([]); };
  const shell = add(doc.body, "div", "shell");
  const main = add(shell, "main", "center", "main");
  const header = add(main, "header", "bar");
  const detail = add(main, "section", "detail");
  const shows = [];
  const chat = {
    show(slug, options) { shows.push([slug, options]); if (!shell.querySelector("textarea")) add(shell.querySelector("#chat-content"), "textarea", "", "composer"); },
    focusComposer() { shell.querySelector("textarea")?.focus(); },
    setQuote() {}, reopened() {}, setOpen() {}, stop() {},
    watchHeader(fn) { chat.header = fn; },
  };
  const dock = createDock({ root: shell, chat, api: () => assert.fail("the dock does not fetch") });
  dock.mount(header, { board: { ...BOARD }, label: "Atlas chat" });
  // A view that redraws its opener while the chat is open (Needs me, Memory, the phone tabs).
  const button = (attrs) => {
    detail.replaceChildren();
    const el = add(detail, "button");
    for (const [name, value] of Object.entries(attrs)) {
      if (name === "id") el.id = value; else el.dataset[name.replace(/^data-/, "").replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
    }
    return el;
  };
  return { doc, shell, dock, shows, button, mutate, chat };
}

test("closing the chat finds an opener redrawn by id", async () => {
  const f = await dockFixture();
  const first = f.button({ id: "open-in-chat" });
  await f.dock.open({ from: first });
  const again = f.button({ id: "open-in-chat" });
  assert.ok(!first.isConnected && again.isConnected);
  f.dock.close();
  assert.equal(f.doc.activeElement, again, "focus returns to the redrawn opener, not the toggle");
});

test("closing the chat finds an opener redrawn by its data attribute", async () => {
  const f = await dockFixture();
  await f.dock.open({ from: f.button({ "data-needs-ask": "" }) });
  const again = f.button({ "data-needs-ask": "" });
  f.dock.close();
  assert.equal(f.doc.activeElement, again, "focus returns to Ask a question");
});

test("focus returns to the same row after the list is rebuilt", async () => {
  const f = await dockFixture();
  const list = f.doc.createElement("section"); f.shell.append(list);
  const row = (n) => { const b = f.doc.createElement("a"); b.dataset.needsSelect = String(n); list.append(b); return b; };
  row(1); const second = row(2); row(3);
  second.focus(); await f.dock.open({ from: second });
  list.replaceChildren(); row(1); row(2); row(3);
  f.dock.close();
  assert.equal(f.doc.activeElement?.dataset?.needsSelect, "2", "focus went to another row");
});

test("a redraw just after the close gives focus back to the opener", async () => {
  const f = await dockFixture();
  await f.dock.open({ from: f.button({ id: "m-chat-tab" }) });
  f.dock.close();
  // The view redraws its tabs after the close: the focused tab is gone.
  const redrawn = f.button({ id: "m-chat-tab" });
  f.doc.activeElement = f.doc.body;
  f.mutate();
  assert.equal(f.doc.activeElement, redrawn, "focus is back on the redrawn tab");
  // Once the person points elsewhere, a later redraw leaves focus alone.
  f.doc.emit("pointerdown", { type: "pointerdown", target: f.shell });
  f.button({ id: "m-chat-tab" });
  f.doc.activeElement = f.doc.body;
  f.mutate();
  assert.equal(f.doc.activeElement, f.doc.body, "no focus pulled back after the person moved on");
});

test("the dock shows the chat's status line under its heading", async () => {
  const f = await dockFixture();
  const line = f.shell.querySelector("#chat .chat-head .ch-status");
  assert.ok(line, "a status line under the heading");
  assert.equal(typeof f.chat.header, "function", "the dock listens for the chat's status");
  assert.equal(line.hidden, true, "empty until the chat knows");
  f.chat.header("Orchestrator connected");
  assert.equal(line.textContent, "Orchestrator connected");
  assert.equal(line.hidden, false);
  assert.equal(f.shell.querySelector("#chat-h").textContent, "Atlas chat", "the heading keeps only the name");
});

test("the dock hands the board's name to the chat", async () => {
  const f = await dockFixture();
  await f.dock.open();
  assert.equal(f.shows[0]?.[0], "atlas");
  assert.equal(f.shows[0]?.[1]?.name, "Atlas");
});

test("a focused or hovered chat row outlines its board card", async () => {
  const { createChat } = await chatModule();
  assert.equal(typeof createChat, "function", "createChat missing");
  const { doc, add } = dom();
  const saved = globalThis.document;
  let chat = null;
  globalThis.document = doc;
  try {
    const board = add(doc.body, "div", "workbench-board");
    const card = add(board, "article", "card"); card.dataset.card = "135";
    const host = add(doc.body, "div", "", "chat-content");
    chat = createChat({ mount: host, api: () => Promise.reject(new Error("offline")), e, me: ME });
    await chat.show("atlas", { name: "Atlas" });
    const row = add(host, "a"); row.dataset.chatCard = "135";
    const text = add(host, "textarea");
    assert.ok(host.listeners.get("focusin")?.size, "the chat listens for focus on its rows");
    host.emit("focusin", { target: row });
    assert.ok(card.classList.contains("is-linked"), "focus outlines A-135");
    host.emit("focusout", { target: row, relatedTarget: text });
    host.emit("focusin", { target: text });
    assert.ok(!card.classList.contains("is-linked"), "focus leaving clears it");
    host.emit("mouseover", { target: row });
    assert.ok(card.classList.contains("is-linked"), "the pointer outlines A-135");
    host.emit("mouseout", { target: row, relatedTarget: host });
    assert.ok(!card.classList.contains("is-linked"), "the pointer leaving clears it");
    host.emit("mouseover", { target: row });
    chat.setOpen(false);
    assert.ok(!card.classList.contains("is-linked"), "closing the chat clears it");
  } finally {
    chat?.destroy();
    globalThis.document = saved;
  }
});

test("styles: outline, in-flow docked pane, no phone key hint", () => {
  const chatCss = read("chat.css"), dockCss = read("chatdock.css");
  assert.match(chatCss, /\.workbench-board \.card\.is-linked\s*\{[^}]*outline:\s*2px dashed var\(--accent\)/);
  const phone = chatCss.slice(chatCss.indexOf("@media (max-width: 699px)"));
  assert.match(phone, /\.chat-compose > p\.chat-hint\s*\{\s*display:\s*none/);
  assert.match(dockCss, /#chat-content > \.chat-view\s*\{[^}]*position:\s*static/);
  assert.match(dockCss, /#chat-content \.chat-header > h2, #chat-content \.chat-header > \[data-close-chat\]\s*\{\s*display:\s*none/);
});
