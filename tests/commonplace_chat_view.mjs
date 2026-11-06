import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const ui = await import("../commonplace/web/static/chat.js");
const { api: request } = await import("../commonplace/web/static/api.js");
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const page = {
  board: "work", orchestrator: null, cursor: 2, oldest: 1, has_more: false, reply_cursor: 13,
  messages: [
    { id: 1, text: "FIRST <script>bad</script> [redacted:token]", actor: "user:me", at: "2026-11-04T09:00:00Z" },
    { id: 2, text: "SECOND", actor: "user:me", at: "2026-11-04T09:01:00Z" },
  ],
  replies: [
    { id: 11, message_id: 1, kind: "receipt", code: "no_orchestrator", text: "RECEIPT", redacted: [], model: null, task: null },
    { id: 12, message_id: 1, kind: "answer", code: "ok", text: "ANSWER <img src=x> [redacted:key]", redacted: ["key"], model: "local/fast", task: null },
    { id: 13, message_id: 2, kind: "handoff", code: "filed", text: "Filed #42", redacted: [], model: null, task: 42 },
  ],
};
const status = { board: "work", orchestrator: { name: "Agent <unsafe>", visible: true, status: "working", last_seen: null, wakeable: true, console: "#/console" }, starts: [], needs_you: 3, front: { mode: "basic", model: null } };
const settle = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };

test("shell routes both desktop and mobile to Chat", async () => {
  const app = await readFile(new URL("../commonplace/web/static/app.js", import.meta.url), "utf8");
  assert.match(app, /#\/chat/);
  assert.ok(Object.isFrozen(ui.CHAT_COPY));
  assert.equal(ui.CHAT_COPY.title, "Chat");
});

test("messages, receipts, answers and handoff links stay interleaved and escaped", () => {
  const html = ui.chatHTML(page, { user: "me", role: "owner" }, e);
  for (const text of ["FIRST", "RECEIPT", "ANSWER", "SECOND", "Filed #42"]) assert.ok(html.includes(text), text);
  assert.ok(html.indexOf("FIRST") < html.indexOf("RECEIPT"));
  assert.ok(html.indexOf("RECEIPT") < html.indexOf("ANSWER"));
  assert.ok(html.indexOf("ANSWER") < html.indexOf("SECOND"));
  assert.ok(html.indexOf("SECOND") < html.indexOf("Filed #42"));
  assert.doesNotMatch(html, /<script>|<img src=x>/);
  assert.ok(html.includes("&lt;script&gt;"));
  assert.match(html, /<mark\b[^>]*>\[redacted:token\]<\/mark>/);
  assert.match(html, /<mark\b[^>]*>\[redacted:key\]<\/mark>/);
  assert.match(html, /href="[^\"]*(?:#\/card\/42|#\/board\/work[^\"]*42)[^\"]*"/);
});

test("status uses console authority and Decisions link", () => {
  const html = ui.statusHTML(status, e);
  assert.match(html, /href="#\/console"/);
  assert.match(html, /href="#\/decide"/);
  assert.ok(html.includes("3"));
  assert.doesNotMatch(html, /<unsafe>/);
  const hidden = ui.statusHTML({ ...status, orchestrator: { name: "a1", visible: false, status: null, last_seen: null, wakeable: false, console: null } }, e);
  assert.match(hidden, /status private/i);
  assert.doesNotMatch(hidden, /href="#\/console"/);
});

test("controller uses project API and stop clears its polling timer", async (t) => {
  const originalDocument = globalThis.document;
  globalThis.document = { hidden: false, addEventListener() {}, removeEventListener() {} };
  t.after(() => { if (originalDocument === undefined) delete globalThis.document; else globalThis.document = originalDocument; });
  const timers = new Map(), calls = [];
  let next = 0;
  t.mock.method(globalThis, "setTimeout", (fn, delay) => { const id = ++next; timers.set(id, { fn, delay }); return id; });
  t.mock.method(globalThis, "clearTimeout", (id) => timers.delete(id));
  const mount = { innerHTML: "", querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {} };
  const controller = ui.createChat({ mount: () => mount, e, async api(path) {
    calls.push(path);
    if (path === "/api/chat") return { projects: [{ slug: "work", name: "Work", orchestrator: null }], default: "work", front: { provider: "none" } };
    if (path.endsWith("/status")) return status;
    if (path.startsWith("/api/chat-settings?")) return { front: {} };
    if (path === "/api/boards/work/cards") return { cards: [] };
    assert.match(path, /^\/api\/chat\/work(?:\?|$)/);
    return page;
  } });
  try {
    await controller.show("work");
    await settle();
    assert.ok(calls.some((p) => p.startsWith("/api/chat/work")));
    assert.equal(timers.size, 1);
    assert.ok(mount.innerHTML.includes("FIRST"));
    controller.stop();
    assert.equal(timers.size, 0);
  } finally { controller.stop(); }
});

// A small DOM double keeps these controller checks browser-free. Every selector
// used by a RED is asserted against the emitted markup before its handler runs.
function setup(t, { post, echo = false, online = true, stored, page: supplied, onCard, onState, get } = {}) {
  // Listeners are registered per event and function, as a browser keeps them.
  const registry = () => {
    const all = new Map();
    return {
      add: (n, fn) => { if (!all.has(n)) all.set(n, new Set()); all.get(n).add(fn); },
      remove: (n, fn) => { all.get(n)?.delete(fn); if (!all.get(n)?.size) all.delete(n); },
      has: (n) => !!all.get(n)?.size,
      get: (n) => all.get(n)?.size ? (event) => { for (const fn of [...all.get(n)]) fn(event); } : undefined,
    };
  };
  const listeners = registry(), events = registry(), calls = [], data = stored || new Map();
  const store = { get length() { return data.size; }, key: (i) => [...data.keys()][i], getItem: (k) => data.get(k) ?? null, setItem: (k, v) => data.set(k, v), removeItem: (k) => data.delete(k) };
  const doc = { hidden: false, addEventListener: (n, fn) => listeners.add(n, fn), removeEventListener: (n, fn) => listeners.remove(n, fn) };
  const restoreGlobals = [];
  for (const [name, value] of Object.entries({ document: doc, navigator: { onLine: online }, sessionStorage: store, localStorage: store, addEventListener: (n, fn) => events.add(n, fn), removeEventListener: (n, fn) => events.remove(n, fn) })) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, { configurable: true, value, writable: true });
    restoreGlobals.push(() => previous ? Object.defineProperty(globalThis, name, previous) : delete globalThis[name]);
  }
  const timers = new Map(); let nextTimer = 0;
  t.mock.method(globalThis, "setTimeout", (fn) => { timers.set(++nextTimer, fn); return nextTimer; });
  t.mock.method(globalThis, "clearTimeout", (id) => timers.delete(id));
  const field = { value: "", focus() { this.focused = true; } }, submit = {};
  const form = { elements: { text: field }, querySelector: () => submit };
  const nodes = new Map();
  const host = { innerHTML: "", querySelector(selector) {
    if (!this.innerHTML) return null;
    if (selector === ".chat-compose") return form;
    if (selector === ".chat-compose textarea") return field;
    if (!nodes.has(selector)) nodes.set(selector, { innerHTML: "", dataset: {}, focus() {}, scrollIntoView() { this.scrolled = true; } });
    return nodes.get(selector);
  } };
  const initial = supplied || { board: "work", me: { user: "me", role: "owner" }, messages: [{ id: 1, actor: "user:me", at: "2026-01-01T00:00:00Z", text: "Please draft release notes" }], replies: [], cursor: 1, reply_cursor: 0, oldest: 1, has_more: true };
  let serverEcho = null;
  async function serve(path, options = {}) {
    calls.push({ path, ...options });
    if (options.method === "POST") {
      if (get) { const response = get(path); if (response) return response; }
    if (path.startsWith("/api/chat-settings?")) return { front: {} };
    if (path.endsWith("/cards")) return { number: 42, title: options.body.title, message_id: options.body.message_id };
      serverEcho = echo ? { id: 2, actor: "user:me", text: options.body.text } : null;
      return post ? post(path, options) : { id: 2 };
    }
    if (path === "/api/chat") return { projects: [{ slug: "work", name: "Work" }, { slug: "other", name: "Other" }], default: "work" };
    if (get) { const response = get(path); if (response) return response; }
    if (path.startsWith("/api/chat-settings?")) return { front: {} };
    if (path.endsWith("/cards")) return { cards: [] };
    if (path.endsWith("/status")) return { front: { mode: "basic" }, orchestrator: null };
    return serverEcho && path.includes("after=") ? { ...initial, messages: [serverEcho], cursor: 2 } : initial;
  }
  const previousFetch = Object.getOwnPropertyDescriptor(globalThis, "fetch");
  Object.defineProperty(globalThis, "fetch", { configurable: true, writable: true, value: async (path, options) => {
    try {
      const payload = await serve(path, { ...options, body: options.body ? JSON.parse(options.body) : undefined });
      return { ok: true, status: options.method === "POST" ? 201 : 200, json: async () => payload };
    } catch (error) {
      if (error.message !== "500") throw error;
      return { ok: false, status: 500, json: async () => ({ error: "Could not send" }) };
    }
  } });
  restoreGlobals.push(() => previousFetch ? Object.defineProperty(globalThis, "fetch", previousFetch) : delete globalThis.fetch);
  const controller = ui.createChat({ mount: () => host, api: request, e, me: { user: "me" }, board: { slug: "work", name: "Work" }, onCard, onState });
  t.after(() => { controller.stop(); for (const restore of restoreGlobals.reverse()) restore(); });
  const send = async (text) => { field.value = text; await form.onsubmit({ preventDefault() {}, currentTarget: form }); };
  const requireSelector = (name) => { assert.ok(host.innerHTML.includes(name), `${name} missing`); };
  const click = async (name, dataset = {}) => {
    requireSelector(name);
    assert.equal(typeof host.onclick, "function");
    const button = { dataset, hasAttribute: (n) => n === name };
    await host.onclick({ target: { closest: () => button }, preventDefault() {} });
  };
  const posts = () => calls.filter((c) => c.method === "POST" && c.path.endsWith("/chat"));
  return { controller, host, field, form, nodes, calls, data, events, listeners, doc, send, click, requireSelector, posts, timers };
}

const requireMethod = (controller, name) => assert.equal(typeof controller[name], "function", `${name} missing`);

test("RED offline bubble then online sends exactly once", async (t) => {
  const h = setup(t, { online: false }); await h.controller.show("work");
  h.requireSelector("data-chat-unsent");
  await h.send("Exact\nwords <here>");
  assert.equal(h.posts().length, 0);
  assert.match(h.nodes.get("[data-chat-unsent]").innerHTML, /Not sent\. This device is offline/);
  assert.equal(h.field.value, "Exact\nwords <here>");
  assert.equal(h.data.get("cmmn.unsent.me.work"), "Exact\nwords <here>");
  navigator.onLine = true;
  assert.equal(typeof h.events.get("online"), "function");
  h.events.get("online")(); h.events.get("online")(); await settle();
  assert.equal(h.posts().length, 1);
  assert.equal(h.field.value, ""); assert.equal(h.data.has("cmmn.unsent.me.work"), false);
});

test("RED ambiguous failure without echo offers retry without resending", async (t) => {
  const h = setup(t, { post: async () => { throw new Error("500"); } }); await h.controller.show("work");
  h.requireSelector("data-chat-unsent"); await h.send("Keep this");
  assert.equal(h.posts().length, 1);
  assert.match(h.nodes.get("[data-chat-unsent]").innerHTML, /Not sent\. Try again/);
  assert.ok(h.calls.some((c) => c.path === "/api/chat/work?after=1"));
  h.events.get("online")(); await settle();
  assert.equal(h.posts().length, 1); assert.equal(h.field.value, "Keep this");
});

test("RED ambiguous failure with own exact echo counts as sent", async (t) => {
  const h = setup(t, { echo: true, post: async () => { throw new Error("500"); } }); await h.controller.show("work");
  h.requireSelector("data-chat-unsent"); await h.send("Echo me");
  assert.equal(h.posts().length, 1); assert.equal(h.field.value, "");
  assert.equal(h.nodes.get("[data-chat-unsent]").innerHTML, "");
  assert.equal(h.data.has("cmmn.unsent.me.work"), false);
});

test("RED signout clears this user's unsent keys across boards", async (t) => {
  const data = new Map([["cmmn.unsent.me.work", "kept"], ["cmmn.unsent.me.other", "also kept"], ["cmmn.unsent.someone.work", "private"]]);
  const h = setup(t, { stored: data }); await h.controller.show("work");
  requireMethod(h.controller, "clearUnsent");
  assert.equal(typeof h.listeners.get("session-expired"), "function");
  h.listeners.get("session-expired")();
  assert.deepEqual([...data], [["cmmn.unsent.someone.work", "private"]]);
  assert.equal(h.field.value, ""); assert.equal(h.timers.size, 0);
});

test("RED Enter sends and Shift Enter preserves a newline", async (t) => {
  const h = setup(t); await h.controller.show("work");
  assert.equal(typeof h.field.onkeydown, "function");
  h.field.value = "One"; let prevented = 0;
  h.field.onkeydown({ key: "Enter", shiftKey: true, preventDefault() { prevented++; } }); await settle();
  assert.equal(h.posts().length, 0); assert.equal(prevented, 0);
  h.field.onkeydown({ key: "Enter", shiftKey: false, preventDefault() { prevented++; } }); await settle();
  assert.equal(h.posts().length, 1); assert.equal(prevented, 1); assert.equal(h.posts()[0].body.text, "One");
});

test("RED Make it a card now includes message_id and exact title", async (t) => {
  const h = setup(t); await h.controller.show("work");
  h.requireSelector("data-chat-recovery");
  assert.match(h.nodes.get("[data-chat-recovery]").innerHTML, /data-chat-make-card/);
  // Recovery lives in a separate region of the persistent pane.
  assert.equal(typeof h.host.onclick, "function");
  await h.host.onclick({ target: { closest: () => ({ dataset: {}, hasAttribute: (n) => n === "data-chat-make-card" }) } });
  const request = h.calls.find((c) => c.method === "POST" && c.path.endsWith("/cards"));
  assert.ok(request); assert.deepEqual(request.body, { title: "Please draft release notes", message_id: 1 });
});

test("RED jump loads source neighbours then today's messages", async (t) => {
  const message = (id) => ({ id, actor: "conn:main", text: `message ${id}` });
  const h = setup(t, { get: (path) => {
    if (path.includes("before=8")) return { messages: [5, 6, 7].map(message), oldest: 5, cursor: 7 };
    if (path.endsWith("after=7")) return { messages: [8, 9, 10, 11].map(message), cursor: 11 };
    if (path.endsWith("last=20")) return { me: { user: "me" }, messages: [20, 21].map(message), cursor: 21, oldest: 20 };
  } }); requireMethod(h.controller, "setQuote");
  await h.controller.show("work", { at: 7 });
  const paths = h.calls.map((c) => c.path);
  const at = paths.indexOf("/api/chat/work?before=8&last=3");
  assert.ok(at >= 0); assert.equal(paths[at + 1], "/api/chat/work?after=7"); assert.equal(paths[at + 2], "/api/chat/work?last=20");
  assert.match(h.nodes.get("[data-chat-source]").innerHTML, /Back to the source/);
  const html = h.nodes.get(".chat-stream").innerHTML;
  assert.match(html, /message 7/); assert.match(html, /message 9/); assert.doesNotMatch(html, /message 10/);
  assert.match(html, /Today/);
});

test("RED quote is posted byte exact and removable", async (t) => {
  const h = setup(t); await h.controller.show("work"); requireMethod(h.controller, "setQuote");
  const quote = { author: "Léon", sourceLabel: "Work chat", date: "Wed, 16:12", text: ' spaces \n“quotes” & <tags>\n' };
  h.controller.setQuote(quote);
  assert.match(h.nodes.get("[data-chat-quote]").innerHTML, /Quoting Léon/);
  await h.send(" What about this? ");
  assert.equal(h.posts()[0].body.text, ' What about this? \n\nLéon, Work chat, Wed, 16:12, exact words:\n“ spaces \n“quotes” & <tags>\n”');
  assert.equal(h.nodes.get("[data-chat-quote]").innerHTML, "");
  h.controller.setQuote(quote); h.controller.setQuote(null); await h.send("Just this");
  assert.equal(h.posts()[1].body.text, "Just this");
});

test("RED restored draft is one bubble with explicit retry and no automatic send", async (t) => {
  const h = setup(t, { stored: new Map([["cmmn.unsent.me.work", "Restored\nwords"]]) }); await h.controller.show("work");
  h.requireSelector("data-chat-unsent");
  assert.match(h.nodes.get("[data-chat-unsent]").innerHTML, /Restored\nwords/);
  assert.match(h.nodes.get("[data-chat-unsent]").innerHTML, /data-chat-retry/);
  assert.equal(h.field.value, "Restored\nwords"); h.events.get("online")(); await settle(); assert.equal(h.posts().length, 0);
});

test("RED focusComposer focuses the retained composer", async (t) => {
  const h = setup(t); await h.controller.show("work"); requireMethod(h.controller, "focusComposer");
  h.field.focused = false; h.controller.focusComposer(); assert.equal(h.field.focused, true);
});

test("RED Show earlier uses the oldest cursor", async (t) => {
  const h = setup(t); await h.controller.show("work"); await h.click("data-chat-earlier");
  assert.ok(h.calls.some((c) => c.path === "/api/chat/work?before=1&last=20"));
});

test("RED Ask again reposts the original text", async (t) => {
  const h = setup(t, { page: { board: "work", me: { user: "me" }, orchestrator: { actor: "conn:main" }, messages: [{ id: 1, actor: "user:me", text: "Please fix exactly\n this", at: "2026-01-01T00:00:00Z" }], replies: [], cursor: 1 } });
  await h.controller.show("work");
  h.requireSelector("data-chat-recovery"); assert.match(h.nodes.get("[data-chat-recovery]").innerHTML, /data-chat-ask-again/);
  assert.equal(typeof h.host.onclick, "function");
  await h.host.onclick({ target: { closest: () => ({ dataset: {}, hasAttribute: (n) => n === "data-chat-ask-again" }) } });
  assert.equal(h.posts()[0].body.text, "Please fix exactly\n this");
});

test("RED chat rows invoke onCard with the same row", async (t) => {
  let result;
  const h = setup(t, { onCard: (...args) => { result = args; } }); await h.controller.show("work");
  assert.equal(typeof h.host.onclick, "function");
  const row = { dataset: { chatCard: "42" } };
  await h.host.onclick({ target: { closest: () => row }, preventDefault() {} });
  assert.deepEqual(result, [42, row]);
});

test("RED IME Enter does not send", async (t) => {
  const h = setup(t); await h.controller.show("work"); assert.equal(typeof h.field.onkeydown, "function");
  h.field.value = "言葉"; h.field.onkeydown({ key: "Enter", shiftKey: false, isComposing: true, preventDefault() { assert.fail("composition was submitted"); } });
  await settle(); assert.equal(h.posts().length, 0);
});

test("RED composer keeps text until acknowledgement and preserves later edits", async (t) => {
  let resolve;
  const h = setup(t, { post: () => new Promise((r) => { resolve = r; }) }); await h.controller.show("work");
  h.requireSelector("data-chat-unsent");
  const sending = h.send("First draft"); await settle();
  assert.equal(h.field.value, "First draft"); h.field.value = "Next draft";
  assert.equal(typeof resolve, "function"); resolve({ id: 2 }); await sending;
  assert.equal(h.field.value, "Next draft"); assert.equal(h.posts().length, 1);
});

test("RED board switch discards stale renders and preserves the new composer", async (t) => {
  let resolve;
  const h = setup(t, { post: () => new Promise((r) => { resolve = r; }) }); await h.controller.show("work");
  h.requireSelector("data-chat-unsent");
  const sending = h.send("First board"); await settle(); await h.controller.show("other");
  h.field.value = "Second board"; resolve({ id: 2 }); await sending;
  assert.equal(h.field.value, "Second board"); assert.equal(h.data.has("cmmn.unsent.me.work"), false);
});

test("RED hidden panes suspend polling until visibility resumes", async (t) => {
  const h = setup(t); await h.controller.show("work"); requireMethod(h.controller, "focusComposer");
  h.doc.hidden = true; h.listeners.get("visibilitychange")(); assert.equal(h.timers.size, 0);
  const count = h.calls.length; h.doc.hidden = false; h.listeners.get("visibilitychange")(); await settle();
  assert.ok(h.calls.length > count); assert.equal(h.timers.size, 1);
});

test("RED offline quote clears the original composer on acknowledgement", async (t) => {
  const h = setup(t, { online: false }); await h.controller.show("work"); requireMethod(h.controller, "setQuote");
  h.controller.setQuote({ author: "Leon", sourceLabel: "Work chat", date: "Today", text: "Exact words" });
  await h.send("Question"); navigator.onLine = true;
  assert.equal(typeof h.events.get("online"), "function"); h.events.get("online")(); await settle();
  assert.equal(h.posts().length, 1); assert.equal(h.field.value, "");
  assert.equal(h.posts()[0].body.text, "Question\n\nLeon, Work chat, Today, exact words:\n“Exact words”");
});

test("RED status reads the effective board settings without sending consent", async (t) => {
  const h = setup(t, { get: (path) => path === "/api/chat-settings?board=work" ? { front: { provider: "anthropic", model: "fast", inherited: true, applies: false } } : null });
  await h.controller.show("work"); h.requireSelector("data-chat-status");
  assert.match(h.nodes.get("[data-chat-status]").innerHTML, /Hosted · set for the whole workspace/);
  assert.match(h.nodes.get("[data-chat-status]").innerHTML, /Turn on/);
  assert.equal(h.calls.filter((c) => c.method === "PUT").length, 0);
});


test("RED offline send survives closing the pane in the same page", async (t) => {
  const h = setup(t, { online: false }); await h.controller.show("work"); h.requireSelector("data-chat-unsent");
  await h.send("Closed pane request"); h.controller.stop(); navigator.onLine = true;
  assert.equal(typeof h.events.get("online"), "function"); h.events.get("online")(); h.events.get("online")(); await settle();
  assert.equal(h.posts().length, 1); assert.equal(h.data.has("cmmn.unsent.me.work"), false);
});

test("RED offline send keeps its board after switching projects", async (t) => {
  const h = setup(t, { online: false }); await h.controller.show("work"); h.requireSelector("data-chat-unsent");
  await h.send("First board request"); await h.controller.show("other"); h.field.value = "Other board draft";
  navigator.onLine = true; assert.equal(typeof h.events.get("online"), "function"); h.events.get("online")(); await settle();
  assert.equal(h.posts().length, 1); assert.equal(h.posts()[0].path, "/api/boards/work/chat");
  assert.equal(h.field.value, "Other board draft"); assert.equal(h.data.has("cmmn.unsent.me.work"), false);
});


// Fix round: each test name is selected on its own by the pytest wrapper.
const askPage = { board: "work", me: { user: "me" }, orchestrator: { actor: "conn:main" }, messages: [{ id: 1, actor: "user:me", text: "Please fix exactly\n this", at: "2026-01-01T00:00:00Z" }], replies: [], cursor: 1 };
const fire = async (h) => {
  const [id, fn] = [...h.timers].at(-1) || [];
  assert.equal(typeof fn, "function", "no poll scheduled");
  h.timers.delete(id); fn(); await settle();
};

test("RED fix ask_keeps_draft", async (t) => {
  const h = setup(t, { page: askPage }); await h.controller.show("work"); requireMethod(h.controller, "setQuote");
  h.controller.setQuote({ author: "Leon", sourceLabel: "Work chat", date: "Today", text: "Exact words" });
  h.field.value = "my half-written reply";
  assert.match(h.nodes.get("[data-chat-recovery]").innerHTML, /data-chat-ask-again/);
  await h.host.onclick({ target: { closest: () => ({ dataset: {}, hasAttribute: (n) => n === "data-chat-ask-again" }) } });
  assert.equal(h.posts().length, 1); assert.equal(h.posts()[0].body.text, "Please fix exactly\n this");
  assert.equal(h.field.value, "my half-written reply");
  assert.match(h.nodes.get("[data-chat-quote]").innerHTML, /Quoting Leon/);
});

test("RED fix unchanged_poll_keeps_focus", async (t) => {
  const h = setup(t, { page: askPage }); await h.controller.show("work");
  // Writing a region's markup replaces its children, so focus inside it falls back to the body.
  for (const node of h.nodes.values()) {
    let html = node.innerHTML;
    Object.defineProperty(node, "innerHTML", { configurable: true, get: () => html, set(value) {
      html = value; if (h.doc.activeElement?.owner === node) h.doc.activeElement = "body";
    } });
  }
  for (const selector of ["[data-chat-recovery]", ".chat-stream", "[data-chat-status]"]) {
    const focused = { owner: h.nodes.get(selector) }; h.doc.activeElement = focused;
    await fire(h);
    assert.equal(h.doc.activeElement, focused, `${selector} was rewritten by an unchanged poll`);
  }
});

test("RED fix reopen_moves_divider", async (t) => {
  const states = [];
  const page = { board: "work", me: { user: "me" }, orchestrator: { actor: "conn:main" }, cursor: 6, reply_cursor: 0, messages: [{ id: 5, actor: "user:me", text: "Please draft", at: "2026-01-01T00:00:00Z" }, { id: 6, actor: "conn:main", text: "On it", at: "2026-01-01T00:01:00Z" }], replies: [] };
  const h = setup(t, { page, stored: new Map([["cmmn.chat.seen.me.work", '{"message":5,"reply":0}']]), onState: (s) => states.push(s) });
  await h.controller.show("work");
  assert.match(h.nodes.get(".chat-stream").innerHTML, /New since you last looked/);
  assert.equal(JSON.parse(h.data.get("cmmn.chat.seen.me.work")).message, 6);
  // Close and reopen: the store already holds message 6, so nothing above it is new.
  await fire(h);
  assert.equal(states.at(-1).unread, 0);
  requireMethod(h.controller, "reopened"); h.controller.reopened(); await fire(h);
  assert.doesNotMatch(h.nodes.get(".chat-stream").innerHTML, /New since you last looked/);
  assert.equal(states.at(-1).unread, 0);
});

test("RED fix sending_is_pending", async (t) => {
  let resolve;
  const h = setup(t, { post: () => new Promise((r) => { resolve = r; }) }); await h.controller.show("work");
  const sending = h.send("In flight"); await settle();
  const bubble = h.nodes.get("[data-chat-unsent]").innerHTML;
  assert.match(bubble, /Sending…/); assert.doesNotMatch(bubble, /Not sent/); assert.doesNotMatch(bubble, /data-chat-retry/);
  assert.equal(typeof resolve, "function"); resolve({ id: 2 }); await sending;
  assert.equal(h.nodes.get("[data-chat-unsent]").innerHTML, "");
});

test("RED fix destroy_removes_listeners", async (t) => {
  const h = setup(t); await h.controller.show("work");
  for (const name of ["session-expired", "signout", "click"]) assert.equal(typeof h.listeners.get(name), "function", name);
  assert.equal(typeof h.events.get("online"), "function");
  requireMethod(h.controller, "destroy"); h.controller.destroy();
  for (const name of ["session-expired", "signout", "click", "visibilitychange"]) assert.equal(h.listeners.has(name), false, name);
  assert.equal(h.events.has("online"), false); assert.equal(h.timers.size, 0);
});

test("RED fix slow_endpoints_every_15s", async (t) => {
  let now = Date.parse("2026-11-05T12:00:00Z");
  t.mock.method(Date, "now", () => now);
  const h = setup(t); await h.controller.show("work");
  const slow = (from) => h.calls.slice(from).filter((c) => /\/status$|\/cards$|^\/api\/chat-settings\?/.test(c.path)).length;
  let from = h.calls.length; now += 2000; await fire(h);
  assert.ok(h.calls.slice(from).some((c) => c.path === "/api/chat/work?after=1&replies_after=0"));
  assert.equal(slow(from), 0, "status, cards and settings were refetched within 15 s");
  from = h.calls.length; now += 13000; await fire(h);
  assert.equal(slow(from), 3);
});

// 150 agent messages; a stored seen entry at 100 means 101–150 are new.
const longChat = (count) => {
  const rows = Array.from({ length: count }, (_, i) => ({ id: i + 1, actor: "conn:main", text: `Row ${i + 1}`, at: "2026-01-01T00:00:00Z" }));
  const base = { board: "work", me: { user: "me" }, orchestrator: { actor: "conn:main" }, replies: [], linked: [] };
  const slice = (picked) => ({ ...base, messages: picked, cursor: picked.at(-1)?.id ?? 0, reply_cursor: 0, oldest: picked[0]?.id ?? 0, has_more: (picked[0]?.id ?? 1) > 1 });
  return (path) => {
    if (!path.startsWith("/api/chat/work?")) return undefined;
    const query = new URLSearchParams(path.split("?")[1]);
    if (query.has("after")) return slice(rows.filter((row) => row.id > Number(query.get("after"))).slice(0, 100));
    return slice(rows.slice(-Number(query.get("last") || 20)));
  };
};
const seenKey = "cmmn.chat.seen.me.work";
const dividerAbove = (html, id) => new RegExp(`New since you last looked</li><li class="chat-message" id="chat-message-${id}"`).test(html);

test("RED fix open_reads_after_seen", async (t) => {
  const h = setup(t, { get: longChat(150), stored: new Map([[seenKey, '{"message":100,"reply":0}']]) });
  await h.controller.show("work");
  assert.ok(h.calls.some((c) => c.path === "/api/chat/work?after=100&replies_after=0"), "the first read must start after the stored seen id");
  const html = h.nodes.get(".chat-stream").innerHTML;
  for (const id of [101, 130, 150]) assert.match(html, new RegExp(`id="chat-message-${id}"`), `message ${id} missing`);
  assert.ok(dividerAbove(html, 101), "the divider must sit above message 101");
  assert.equal(JSON.parse(h.data.get(seenKey)).message, 150);
});

test("RED fix open_pages_full", async (t) => {
  const h = setup(t, { get: longChat(150), stored: new Map([[seenKey, '{"message":20,"reply":0}']]) });
  await h.controller.show("work");
  assert.ok(h.calls.some((c) => c.path === "/api/chat/work?after=120&replies_after=0"), "a full page must be followed");
  const html = h.nodes.get(".chat-stream").innerHTML;
  for (const id of [21, 120, 121, 150]) assert.match(html, new RegExp(`id="chat-message-${id}"`), `message ${id} missing`);
  assert.ok(dividerAbove(html, 21), "the divider must sit above message 21");
});

test("RED fix closed_keeps_divider", async (t) => {
  const { deriveChatState } = await import("../commonplace/web/static/chatstate.js");
  const base = { board: "work", me: { user: "me" }, orchestrator: { actor: "conn:main" }, replies: [], linked: [] };
  const rows = [{ id: 5, actor: "user:me", text: "Please draft", at: "2026-01-01T00:00:00Z" }, { id: 6, actor: "conn:main", text: "On it", at: "2026-01-01T00:01:00Z" }];
  const get = (path) => {
    if (!path.startsWith("/api/chat/work?")) return undefined;
    const after = Number(new URLSearchParams(path.split("?")[1]).get("after") ?? 0);
    const picked = rows.filter((row) => row.id > after);
    return { ...base, messages: picked, cursor: picked.at(-1)?.id ?? after, reply_cursor: 0 };
  };
  const h = setup(t, { get, stored: new Map([[seenKey, '{"message":6,"reply":0}']]) });
  await h.controller.show("work");
  assert.equal(JSON.parse(h.data.get(seenKey)).message, 6);
  // The dock closes the pane on a visible tab; then the agent replies.
  requireMethod(h.controller, "setOpen"); h.controller.setOpen(false);
  rows.push({ id: 7, actor: "conn:main", text: "Done, see the card", at: "2026-01-01T00:05:00Z" });
  for (const [id, fn] of [...h.timers]) { h.timers.delete(id); fn(); await settle(); }
  assert.equal(JSON.parse(h.data.get(seenKey)).message, 6, "a closed pane must not mark the reply seen");
  const closed = deriveChatState({ ...base, messages: rows, cursor: 7 }, { orchestrator: null }, { slug: "work", coordinator: "conn:main" }, Date.now(), JSON.parse(h.data.get(seenKey)));
  assert.equal(closed.unread, 1);
  // Reopen as the dock does: the divider sits above the reply that arrived while closed.
  h.controller.reopened(); h.controller.setOpen(true); await settle();
  assert.ok(dividerAbove(h.nodes.get(".chat-stream").innerHTML, 7), "the divider must sit above message 7 after reopening");
  assert.equal(JSON.parse(h.data.get(seenKey)).message, 7);
});

// A server whose rows can grow; the first chat page can be held back or failed.
const growingChat = (rows, first = null) => {
  const base = { board: "work", me: { user: "me" }, orchestrator: { actor: "conn:main" }, replies: [], linked: [] };
  const slice = (picked, after = 0) => ({ ...base, messages: picked, cursor: picked.at(-1)?.id ?? after, reply_cursor: 0, oldest: picked[0]?.id ?? 0, has_more: (picked[0]?.id ?? 1) > 1 });
  let reads = 0;
  return (path) => {
    if (!path.startsWith("/api/chat/work?")) return undefined;
    const query = new URLSearchParams(path.split("?")[1]);
    const page = () => query.has("after") ? slice(rows.filter((row) => row.id > Number(query.get("after"))).slice(0, 100), Number(query.get("after"))) : slice(rows.slice(-Number(query.get("last") || 20)));
    return ++reads === 1 && first ? first(page) : page();
  };
};
const agentRows = (from, to) => Array.from({ length: to - from + 1 }, (_, i) => ({ id: from + i, actor: "conn:main", text: `Row ${from + i}`, at: "2026-01-01T00:00:00Z" }));
const settled = async () => { for (let i = 0; i < 10; i++) await settle(); };
const startsAfterZero = (h) => h.calls.some((c) => /^\/api\/chat\/work\?after=0(?:&|$)/.test(c.path));

test("RED fix reopen_before_first_page", async (t) => {
  let release;
  const get = growingChat(agentRows(1, 150), (page) => new Promise((resolve) => { release = () => resolve(page()); }));
  const h = setup(t, { get });
  const shown = h.controller.show("work"); await settled();
  assert.equal(typeof release, "function", "the first page was not requested");
  // The dock closes and reopens while the first page is still in flight.
  h.controller.setOpen(false); h.controller.reopened(); h.controller.setOpen(true);
  release(); await shown; await settled();
  assert.ok(!startsAfterZero(h), "a reopen before the first page must not read from 0");
  assert.ok(!dividerAbove(h.nodes.get(".chat-stream").innerHTML, 1), "first sight must not mark the whole history new");
  assert.equal(JSON.parse(h.data.get(seenKey)).message, 150);
});

test("RED fix reopen_after_failed_first_page", async (t) => {
  const get = growingChat(agentRows(1, 150), () => { throw new Error("500"); });
  const h = setup(t, { get });
  await h.controller.show("work"); await settled();
  assert.equal(h.data.get(seenKey), undefined, "a failed first page must not store seen ids");
  h.controller.setOpen(false); h.controller.reopened(); h.controller.setOpen(true); await settled();
  assert.ok(!startsAfterZero(h), "a reopen after a failed first page must not read from 0");
  assert.ok(!dividerAbove(h.nodes.get(".chat-stream").innerHTML, 1), "first sight must not mark the whole history new");
  assert.equal(JSON.parse(h.data.get(seenKey)).message, 150);
});

test("RED fix reopen_catches_up", async (t) => {
  const rows = agentRows(1, 6);
  const h = setup(t, { get: growingChat(rows), stored: new Map([[seenKey, '{"message":6,"reply":0}']]) });
  await h.controller.show("work"); await settled();
  h.controller.setOpen(false);
  rows.push(...agentRows(7, 256));
  h.controller.reopened(); h.controller.setOpen(true); await settled();
  const html = h.nodes.get(".chat-stream").innerHTML;
  for (const id of [7, 106, 107, 206, 207, 256]) assert.match(html, new RegExp(`id="chat-message-${id}"`), `message ${id} missing after one reopen tick`);
  assert.ok(dividerAbove(html, 7), "the divider must sit above message 7");
});
