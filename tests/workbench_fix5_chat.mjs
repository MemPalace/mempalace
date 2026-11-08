// Chat review fixes: one unsent message per board, offline delivery that outlives the pane,
// and seen ids that live on the device.
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";

const STATIC = new URL("../commonplace/web/static/", import.meta.url);
const settle = async () => { for (let i = 0; i < 60; i++) await Promise.resolve(); };
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function modules() {
  for (const name of ["chat.js", "chatstate.js", "api.js"]) assert.ok(existsSync(new URL(name, STATIC)), `${name} missing`);
  const chat = await import("../commonplace/web/static/chat.js");
  const state = await import("../commonplace/web/static/chatstate.js");
  const { api } = await import("../commonplace/web/static/api.js");
  assert.equal(typeof chat.createChat, "function", "createChat missing");
  assert.equal(typeof chat.chatHTML, "function", "chatHTML missing");
  assert.equal(typeof state.seenStore, "function", "seenStore missing");
  return { ...chat, ...state, api };
}

function memoryStorage(data = new Map()) {
  return { data, get length() { return data.size; }, key: (i) => [...data.keys()][i] ?? null, getItem: (k) => data.get(k) ?? null, setItem: (k, v) => data.set(k, String(v)), removeItem: (k) => data.delete(k) };
}

// A browser-free page: globals, a fake server and a small DOM double for one chat pane.
async function setup(t, { online = true, post, denied = false } = {}) {
  const ui = await modules();
  // Listener registries keyed by event and function, as a browser keeps them.
  const registry = () => {
    const all = new Map();
    return {
      add: (n, fn) => { if (!all.has(n)) all.set(n, new Set()); all.get(n).add(fn); },
      remove: (n, fn) => { all.get(n)?.delete(fn); if (!all.get(n)?.size) all.delete(n); },
      has: (n) => !!all.get(n)?.size,
      get: (n) => all.get(n)?.size ? (event = {}) => { for (const fn of [...all.get(n)]) fn(event); } : undefined,
    };
  };
  const listeners = registry(), events = registry(), calls = [];
  const session = memoryStorage(), device = memoryStorage();
  // Denied storage: every call throws, as a locked-down browser does.
  const refuse = () => { throw new Error("denied"); };
  const deniedStorage = { get length() { return refuse(); }, key: refuse, getItem: refuse, setItem: refuse, removeItem: refuse };
  const doc = { hidden: false, addEventListener: (n, fn) => listeners.add(n, fn), removeEventListener: (n, fn) => listeners.remove(n, fn), dispatchEvent: (event) => listeners.get(event.type)?.(event) };
  const restore = [];
  const globals = { document: doc, navigator: { onLine: online }, sessionStorage: denied ? deniedStorage : session, localStorage: device, addEventListener: (n, fn) => events.add(n, fn), removeEventListener: (n, fn) => events.remove(n, fn) };
  for (const [name, value] of Object.entries(globals)) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, { configurable: true, value, writable: true });
    restore.push(() => previous ? Object.defineProperty(globalThis, name, previous) : delete globalThis[name]);
  }
  const timers = new Map(); let nextTimer = 0;
  t.mock.method(globalThis, "setTimeout", (fn) => { timers.set(++nextTimer, fn); return nextTimer; });
  t.mock.method(globalThis, "clearTimeout", (id) => timers.delete(id));
  const initial = { board: "work", me: { user: "me", role: "owner" }, messages: [{ id: 1, actor: "user:me", at: "2026-01-01T00:00:00Z", text: "Please draft release notes" }], replies: [], cursor: 1, reply_cursor: 0, oldest: 1, has_more: false };
  async function serve(path, options = {}) {
    calls.push({ path, ...options });
    if (options.method === "POST") return post ? post(path, options) : { id: 2 };
    if (path === "/api/chat") return { projects: [{ slug: "work", name: "Work" }, { slug: "other", name: "Other" }], default: "work" };
    if (path.startsWith("/api/chat-settings?")) return { front: {} };
    if (path.endsWith("/cards")) return { cards: [] };
    if (path.endsWith("/status")) return { front: { mode: "basic" }, orchestrator: null };
    return initial;
  }
  const previousFetch = Object.getOwnPropertyDescriptor(globalThis, "fetch");
  Object.defineProperty(globalThis, "fetch", { configurable: true, writable: true, value: async (path, options = {}) => {
    try {
      const payload = await serve(path, { ...options, body: options.body ? JSON.parse(options.body) : undefined });
      return { ok: true, status: options.method === "POST" ? 201 : 200, json: async () => payload };
    } catch (error) {
      if (!["500", "401"].includes(error.message)) throw error;
      return { ok: false, status: Number(error.message), json: async () => ({ error: "Could not send" }) };
    }
  } });
  restore.push(() => previousFetch ? Object.defineProperty(globalThis, "fetch", previousFetch) : delete globalThis.fetch);
  const panes = [];
  function pane() {
    const field = { value: "", focus() {} }, submit = { disabled: false };
    const form = { elements: { text: field }, querySelector: () => submit };
    const nodes = new Map();
    const host = { innerHTML: "", querySelector(selector) {
      if (!this.innerHTML) return null;
      if (selector === ".chat-compose") return form;
      if (!nodes.has(selector)) nodes.set(selector, { innerHTML: "", dataset: {}, hidden: false, focus() {}, scrollIntoView() {} });
      return nodes.get(selector);
    } };
    const controller = ui.createChat({ mount: () => host, api: ui.api, e, me: { user: "me" }, board: { slug: "work", name: "Work" } });
    panes.push(controller);
    const send = async (text) => { field.value = text; await form.onsubmit({ preventDefault() {}, currentTarget: form }); };
    return { controller, host, field, submit, nodes, send };
  }
  t.after(() => { for (const c of panes) c.destroy(); for (const undo of restore.reverse()) undo(); });
  const posts = () => calls.filter((c) => c.method === "POST" && c.path.endsWith("/chat"));
  const goOnline = () => { globalThis.navigator.onLine = true; const fire = events.get("online"); assert.equal(typeof fire, "function", "nobody listens for the device coming online"); fire(); };
  return { ui, pane, posts, goOnline, events, listeners, session, device, calls };
}

// MAJOR 1 ---------------------------------------------------------------------------------

test("a second offline send keeps the first message and the new draft", async (t) => {
  const h = await setup(t, { online: false });
  const p = h.pane(); await p.controller.show("work");
  await p.send("First words");
  await p.send("Second words");
  assert.equal(h.posts().length, 0);
  assert.equal(h.session.getItem("cmmn.unsent.me.work"), "First words", "the first message is still kept");
  assert.match(p.nodes.get("[data-chat-unsent]")?.innerHTML || "", /First words/, "the bubble still shows the first message");
  assert.equal(p.field.value, "Second words", "the new draft stays in the composer");
  assert.equal(p.submit.disabled, true, "Send waits for the earlier message");
  assert.equal(p.nodes.get("[data-chat-wait]")?.hidden, false, "the composer says it is waiting");
  h.goOnline(); h.events.get("online")?.(); await settle();
  assert.deepEqual(h.posts().map((c) => c.body.text), ["First words"], "the first message is posted exactly once");
  assert.equal(p.field.value, "Second words", "the second draft is not lost");
  assert.equal(p.submit.disabled, false);
  await p.send("Second words"); await settle();
  assert.deepEqual(h.posts().map((c) => c.body.text), ["First words", "Second words"], "no duplicate, nothing lost");
});

test("discarding the unsent message frees the composer", async (t) => {
  const h = await setup(t, { online: false });
  const p = h.pane(); await p.controller.show("work");
  await p.send("Drop me");
  assert.match(p.nodes.get("[data-chat-unsent]")?.innerHTML || "", /data-chat-discard/, "a Discard control is drawn");
  await p.host.onclick({ target: { closest: () => ({ dataset: {}, hasAttribute: (n) => n === "data-chat-discard" }) }, preventDefault() {} });
  assert.equal(h.session.getItem("cmmn.unsent.me.work"), null);
  assert.equal(p.nodes.get("[data-chat-unsent]").innerHTML, "");
  globalThis.navigator.onLine = true; h.events.get("online")?.(); await settle();
  assert.equal(h.posts().length, 0, "a discarded message is never sent");
});

// MAJOR 2 ---------------------------------------------------------------------------------

test("an offline send outlives the pane and posts once to its board", async (t) => {
  const h = await setup(t, { online: false });
  const p = h.pane(); await p.controller.show("work");
  await p.send("Sent after the route changed");
  p.controller.destroy();
  h.goOnline(); await settle();
  h.events.get("online")?.(); await settle();
  assert.equal(h.posts().length, 1, "exactly one POST");
  assert.equal(h.posts()[0].path, "/api/boards/work/chat");
  assert.equal(h.posts()[0].body.text, "Sent after the route changed");
  assert.equal(h.session.getItem("cmmn.unsent.me.work"), null, "acknowledged text leaves storage");
  assert.equal(h.events.has("online"), false, "the listener goes once the queue is empty");
});

test("a new pane on another board does not send the queued message twice", async (t) => {
  const h = await setup(t, { online: false });
  const first = h.pane(); await first.controller.show("work");
  await first.send("Board one request");
  first.controller.destroy();
  const second = h.pane(); await second.controller.show("other");
  second.field.value = "Other board draft";
  h.goOnline(); h.events.get("online")?.(); await settle();
  assert.deepEqual(h.posts().map((c) => [c.path, c.body.text]), [["/api/boards/work/chat", "Board one request"]]);
  assert.equal(second.field.value, "Other board draft");
});

test("an ambiguous delivery after the pane closed is kept for an explicit retry", async (t) => {
  const h = await setup(t, { online: false, post: async () => { throw new Error("500"); } });
  const p = h.pane(); await p.controller.show("work");
  await p.send("Maybe sent");
  p.controller.destroy();
  h.goOnline(); await settle();
  assert.equal(h.posts().length, 1, "one attempt");
  assert.ok(h.calls.some((c) => c.path === "/api/chat/work?after=1"), "it read for its own echo");
  assert.equal(h.session.getItem("cmmn.unsent.me.work"), "Maybe sent", "the words are kept");
  h.events.get("online")?.(); await settle();
  assert.equal(h.posts().length, 1, "no automatic resend");
});

test("signing out drops a queued offline send", async (t) => {
  const h = await setup(t, { online: false });
  const p = h.pane(); await p.controller.show("work");
  await p.send("Never after sign-out");
  p.controller.destroy();
  const signout = h.listeners.get("signout");
  assert.equal(typeof signout, "function", "the queue listens for sign-out");
  signout();
  assert.equal(h.session.getItem("cmmn.unsent.me.work"), null);
  globalThis.navigator.onLine = true; h.events.get("online")?.(); await settle();
  assert.equal(h.posts().length, 0, "no POST after sign-out");
});

// MAJOR 3 ---------------------------------------------------------------------------------

const agent = (id) => ({ id, actor: "conn:atlas", text: `Agent ${id}`, at: "2026-01-01T00:00:00Z" });
const seenPage = (messages) => ({ me: { user: "maya" }, messages, replies: [], cursor: messages.at(-1)?.id || 0, reply_cursor: 0 });

function devicePair(t) {
  const device = memoryStorage();
  const saved = ["localStorage", "sessionStorage"].map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]);
  t.after(() => { for (const [name, previous] of saved) previous ? Object.defineProperty(globalThis, name, previous) : delete globalThis[name]; });
  const tab = () => {
    for (const [name, value] of [["localStorage", device], ["sessionStorage", memoryStorage()]]) Object.defineProperty(globalThis, name, { configurable: true, value, writable: true });
  };
  return { device, tab };
}

test("a new tab after an earlier read counts the new agent message", async (t) => {
  const ui = await modules();
  const { tab } = devicePair(t);
  const before = seenPage(Array.from({ length: 10 }, (_, i) => agent(i + 1)));
  tab(); ui.seenStore("maya", "atlas").markSeen(before);
  // A new tab or browser session on the same device; one agent message arrived meanwhile.
  tab();
  const after = seenPage([...before.messages, agent(11)]);
  const seen = ui.seenStore("maya", "atlas").read(after);
  assert.equal(seen.message, 10, "the earlier read is remembered on the device");
  assert.equal(ui.deriveChatState(after, {}, { slug: "atlas" }, Date.now(), seen).unread, 1);
  const html = ui.chatHTML({ ...after, project: { slug: "atlas", name: "Atlas", coordinator: "conn:atlas" }, seen }, { user: "maya" }, e);
  assert.match(html, /New since you last looked/);
  assert.ok(html.indexOf("Agent 10") < html.indexOf("New since") && html.indexOf("New since") < html.indexOf("Agent 11"));
});

test("true first sight on a device still baselines to the newest ids", async (t) => {
  const ui = await modules();
  const { device, tab } = devicePair(t);
  tab();
  const p = seenPage(Array.from({ length: 20 }, (_, i) => agent(i + 1)));
  const seen = ui.seenStore("maya", "atlas").read(p);
  assert.equal(ui.deriveChatState(p, {}, { slug: "atlas" }, Date.now(), seen).unread, 0);
  assert.equal(device.getItem("cmmn.chat.seen.maya.atlas"), '{"message":20,"reply":0}', "only the two ids, on the device");
});

test("device seen ids stay per user and board, and sign-out clears only that user", async (t) => {
  const ui = await modules();
  const { device, tab } = devicePair(t);
  tab();
  ui.seenStore("maya", "atlas").markSeen(seenPage([agent(5)]));
  assert.equal(device.getItem("cmmn.chat.seen.maya.atlas"), '{"message":5,"reply":0}');
  assert.deepEqual(ui.seenStore("leon", "atlas").read(seenPage([])), { message: 0, reply: 0 });
  assert.deepEqual(ui.seenStore("maya", "harbor").read(seenPage([])), { message: 0, reply: 0 });
  ui.clearSeen("maya");
  assert.equal(device.getItem("cmmn.chat.seen.maya.atlas"), null);
  assert.equal(device.getItem("cmmn.chat.seen.maya.harbor"), null);
  assert.equal(device.getItem("cmmn.chat.seen.leon.atlas"), '{"message":0,"reply":0}');
});

// Round 2: a send stays tracked until it settles -----------------------------------------

function deferred() {
  let settle;
  const promise = new Promise((resolve, reject) => { settle = { resolve, reject }; });
  return { promise, ...settle };
}
const click = (p, name) => p.host.onclick({ target: { closest: () => ({ dataset: {}, hasAttribute: (n) => n === name }) }, preventDefault() {} });

test("a pane reopened during a background send shows it sending and cannot resend or discard it", async (t) => {
  const pending = deferred();
  t.after(() => pending.resolve({ id: 9 }));
  const h = await setup(t, { online: false, post: () => pending.promise });
  const first = h.pane(); await first.controller.show("work");
  await first.send("On its way");
  first.controller.destroy();
  h.goOnline(); await settle();
  assert.equal(h.posts().length, 1, "the background send started");
  const again = h.pane(); await again.controller.show("work");
  const bubble = again.nodes.get("[data-chat-unsent]")?.innerHTML || "";
  assert.match(bubble, /Sending…/, "the reopened pane shows the message on its way");
  assert.doesNotMatch(bubble, /data-chat-retry/, "no Try again while it is in flight");
  assert.doesNotMatch(bubble, /data-chat-discard/, "no Discard while it is in flight");
  assert.equal(again.submit.disabled, true);
  await click(again, "data-chat-retry"); await click(again, "data-chat-discard"); await settle();
  assert.equal(h.posts().length, 1, "no second POST");
  assert.equal(h.session.getItem("cmmn.unsent.me.work"), "On its way", "discard did nothing while in flight");
  pending.resolve({ id: 2 }); await settle();
  assert.equal(again.nodes.get("[data-chat-unsent]").innerHTML, "", "the bubble clears once it is acknowledged");
  assert.equal(h.session.getItem("cmmn.unsent.me.work"), null);
  assert.equal(h.posts().length, 1);
});

test("signing out during a background send keeps nothing and reads nothing more", async (t) => {
  const pending = deferred();
  t.after(() => pending.resolve({ id: 9 }));
  const h = await setup(t, { online: false, post: () => pending.promise });
  const p = h.pane(); await p.controller.show("work");
  await p.send("Private words");
  p.controller.destroy();
  h.goOnline(); await settle();
  assert.equal(h.posts().length, 1);
  const signout = h.listeners.get("signout");
  assert.equal(typeof signout, "function", "the in-flight send listens for sign-out");
  signout();
  assert.equal(h.session.getItem("cmmn.unsent.me.work"), null, "the draft leaves the device at sign-out");
  const before = h.calls.length;
  pending.reject(new Error("500")); await settle();
  assert.equal(h.calls.length, before, "no echo read after sign-out");
  assert.equal(h.session.getItem("cmmn.unsent.me.work"), null);
});

test("a session that expires during a background send keeps nothing and reads nothing more", async (t) => {
  const pending = deferred();
  t.after(() => pending.resolve({ id: 9 }));
  const h = await setup(t, { online: false, post: () => pending.promise });
  const p = h.pane(); await p.controller.show("work");
  await p.send("Expired words");
  p.controller.destroy();
  h.goOnline(); await settle();
  const before = h.calls.length;
  pending.reject(new Error("401")); await settle();
  assert.equal(h.calls.length, before, "no echo read after the 401");
  assert.equal(h.session.getItem("cmmn.unsent.me.work"), null, "the draft leaves the device on expiry");
});

// Round 2: app.js sign-out, restart settings and dialog keys --------------------------------

const app = readFileSync(new URL("app.js", STATIC), "utf8");
function slice(start, end) {
  const at = app.indexOf(start), stop = app.indexOf(end, at + start.length);
  assert.ok(at >= 0 && stop > at, `Missing slice: ${start} → ${end}`);
  return app.slice(at, stop);
}

function seenDevice(t) {
  const device = memoryStorage();
  for (const [key, value] of [["cmmn.chat.seen.maya.atlas", '{"message":5,"reply":0}'], ["cmmn.chat.seen.maya.harbor", '{"message":2,"reply":0}'], ["cmmn.chat.seen.leon.atlas", '{"message":9,"reply":0}'], ["other.setting", "keep"]]) device.setItem(key, value);
  const previous = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: device, writable: true });
  t.after(() => previous ? Object.defineProperty(globalThis, "localStorage", previous) : delete globalThis.localStorage);
  return device;
}
async function shellContext() {
  const { clearSeen } = await modules();
  const docListeners = new Map();
  return { state: { me: { user: "maya", actor: "user:maya" }, drafts: new Map(), queue: null, error: "" }, chatUI: null, adminUI: null, clearSeen, Event,
    document: { dispatchEvent() {}, addEventListener: (n, fn) => docListeners.set(n, fn) }, signin() {}, clearTimeout() {}, timer: null, docListeners };
}
const keptOnly = (device) => {
  assert.equal(device.getItem("cmmn.chat.seen.maya.atlas"), null, "Maya's Atlas seen ids are gone");
  assert.equal(device.getItem("cmmn.chat.seen.maya.harbor"), null, "Maya's Harbor seen ids are gone");
  assert.equal(device.getItem("cmmn.chat.seen.leon.atlas"), '{"message":9,"reply":0}', "Leon's ids stay");
  assert.equal(device.getItem("other.setting"), "keep");
};

test("signing out with no board service (Settings) clears this user's seen ids only", async (t) => {
  const device = seenDevice(t);
  const context = await shellContext();
  runInNewContext(slice("function clearWorkbenchChat() {", "function consoleScreen()") + "\nclearWorkbenchChat();", context);
  keptOnly(device);
});

test("a session expiring off-board clears this user's seen ids before forgetting who it was", async (t) => {
  const device = seenDevice(t);
  const context = await shellContext();
  runInNewContext(slice("function clearWorkbenchChat() {", "function consoleScreen()") + slice('document.addEventListener("session-expired",', "\n"), context);
  const expired = context.docListeners.get("session-expired");
  assert.equal(typeof expired, "function");
  expired();
  keptOnly(device);
  assert.equal(context.state.me, null);
});

test("the restart settings close button goes back to the chat", () => {
  assert.match(slice("Back to the chat", ">×</button>"), /^Back to the chat"$/);
  const markup = app.slice(app.lastIndexOf("<button", app.indexOf('aria-label="Back to the chat"')), app.indexOf('aria-label="Back to the chat"'));
  const action = markup.match(/data-action="([^"]+)"/)?.[1];
  assert.ok(action, "the × has an action");
  assert.notEqual(action, "close-panel", "the × does more than hide the panel");
  const branch = slice(`else if (action === "${action}") {`, "\n    }");
  const calls = [];
  runInNewContext(branch.replace(/^else if \([^)]*\) \{/, ""), { state: { workbenchChat: { dock: { backToChat: () => calls.push("back") } } }, closeCardPanel: () => calls.push("close") });
  assert.deepEqual(calls, ["back"]);
});

test("board single keys wait while a project dialog is open", () => {
  const listener = slice('document.addEventListener("keydown", (event) => {', "\n});\n") + "\n});";
  const handled = [];
  let dialogOpen = true, keydown = null;
  const context = {
    root: { querySelector: () => null }, state: { me: { user: "maya" }, base: "#/board/alpha" }, Element: class {},
    document: { addEventListener: (type, fn) => { if (type === "keydown") keydown = fn; }, querySelector: (selector) => selector === "dialog[open]" && dialogOpen ? {} : null },
    singleKeysEnabled: () => true, workbenchKey: (event) => { handled.push(event.key); return true; }, shellMenu() {}, closeCardPanel() {}, location: {},
  };
  runInNewContext(listener, context);
  assert.equal(typeof keydown, "function");
  const press = (key) => keydown({ key, target: {}, defaultPrevented: false, repeat: false, preventDefault() {} });
  press("c");
  assert.deepEqual(handled, [], "no board shortcut behind the dialog");
  dialogOpen = false; press("c");
  assert.deepEqual(handled, ["c"], "the shortcut works without a dialog");
});

// Round 3: storage denied — the shared item is the only record ------------------------------

test("with storage denied a new pane shows the queued message, holds the next one and sends it once", async (t) => {
  const h = await setup(t, { online: false, denied: true });
  const first = h.pane(); await first.controller.show("work");
  await first.send("Kept without storage");
  first.controller.destroy();
  const again = h.pane(); await again.controller.show("work");
  assert.match(again.nodes.get("[data-chat-unsent]")?.innerHTML || "", /Kept without storage/, "the new pane shows the queued message");
  await again.send("Second message");
  assert.equal(h.posts().length, 0);
  assert.match(again.nodes.get("[data-chat-unsent]")?.innerHTML || "", /Kept without storage/, "the queued message is not replaced");
  assert.equal(again.field.value, "Second message", "the new draft stays in the composer");
  h.goOnline(); await settle();
  assert.deepEqual(h.posts().map((c) => c.body.text), ["Kept without storage"], "exactly one POST of the queued message");
});

test("with storage denied a new pane during an in-flight send cannot send it again", async (t) => {
  const pending = deferred();
  t.after(() => pending.resolve({ id: 9 }));
  const h = await setup(t, { online: false, denied: true, post: () => pending.promise });
  const first = h.pane(); await first.controller.show("work");
  await first.send("Only once");
  first.controller.destroy();
  h.goOnline(); await settle();
  assert.equal(h.posts().length, 1, "the background send started");
  const again = h.pane(); await again.controller.show("work");
  // Not awaited: a second POST (the bug) would wait on the same deferred response.
  void again.send("Only once"); await settle();
  assert.equal(h.posts().length, 1, "no second POST of the same message");
  assert.match(again.nodes.get("[data-chat-unsent]")?.innerHTML || "", /Sending…/, "the new pane shows it on its way");
  pending.resolve({ id: 2 }); await settle();
  assert.equal(h.posts().length, 1);
  assert.equal(again.nodes.get("[data-chat-unsent]").innerHTML, "", "acknowledged");
});
