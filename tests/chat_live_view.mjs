import assert from "node:assert/strict";
import test from "node:test";

const live = await import("../commonplace/web/static/chatlive.js");
const ui = await import("../commonplace/web/static/chat.js");
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const owner = { user: "me", role: "owner" }, member = { user: "me", role: "member" };
const at = "2026-11-11T09:00:00Z";
const base = { message_id: 7, state: "delivered", reason: "wake_sent", agent: "a1", at, seen_at: null, answer_id: null };
const settle = async () => { for (let i = 0; i < 60; i++) await Promise.resolve(); };

test("every state the server reports has its own visible text", () => {
  const cases = [
    [{ reason: "wake_sent" }, /Delivered to a1/, "status"],
    [{ reason: "wake_pending" }, /Delivering to a1/, "status"],
    [{ reason: "wake_retrying" }, /did not work yet/, "status"],
    [{ reason: "wake_none" }, /no wake-up hook/, "status"],
    [{ state: "working", reason: null, seen_at: at }, /a1 is working on this/, "status"],
    [{ state: "no_agent", reason: "none", agent: null }, /No agent connected/, "alert"],
    [{ state: "no_agent", reason: "gone" }, /no longer connected/, "alert"],
    [{ state: "no_answer", reason: "not_picked_up" }, /a1 did not answer.*has not picked up/s, "alert"],
    [{ state: "no_answer", reason: "wake_failed" }, /wake-up call to a1 failed/, "alert"],
    [{ state: "no_answer", reason: "no_reply", seen_at: at }, /has not replied/, "alert"],
  ];
  const seen = new Set();
  for (const [change, text, role] of cases) {
    const html = live.liveHTML({ ...base, ...change }, owner, e);
    assert.match(html, text);
    assert.ok(html.includes(`role="${role}"`), html);
    assert.ok(html.includes(`data-live-state="${change.state || "delivered"}"`));
    seen.add(html);
  }
  assert.equal(seen.size, cases.length);
});

test("nothing is drawn without a message, or once the agent answered", () => {
  assert.equal(live.liveHTML(undefined, owner, e), "");
  assert.equal(live.liveHTML({ ...base, message_id: null, state: "idle" }, owner, e), "");
  assert.equal(live.liveHTML({ ...base, state: "answered", answer_id: 8 }, owner, e), "");
  assert.equal(live.liveHTML({ ...base, state: "later-version-state" }, owner, e), "");
  assert.equal(live.liveLine({ ...base, state: "answered" }), "");
});

test("an owner is told to act, a member to ask the owner", () => {
  for (const change of [{ state: "no_agent", reason: "none" }, { state: "no_answer", reason: "not_picked_up" }]) {
    const forOwner = live.liveHTML({ ...base, ...change }, owner, e), forMember = live.liveHTML({ ...base, ...change }, member, e);
    assert.match(forMember, /Ask the workspace owner/);
    assert.doesNotMatch(forOwner, /Ask the workspace owner/);
    assert.doesNotMatch(forMember, /in Settings|Check that the agent/);
  }
  assert.match(live.liveHTML({ ...base, state: "no_agent", reason: "none" }, owner, e), /Connect an agent in Settings/);
});

test("agent names are escaped", () => {
  const html = live.liveHTML({ ...base, agent: "<img src=x>" }, owner, e);
  assert.doesNotMatch(html, /<img/);
  assert.ok(html.includes("&lt;img"));
});

test("the pane reads faster only while a fresh message is on its way", () => {
  const now = Date.parse(at) + 5_000;
  assert.equal(live.livePending(base, now), true);
  assert.equal(live.livePending({ ...base, state: "working" }, now), true);
  assert.equal(live.livePending(base, now + 120_000), false);
  for (const state of ["idle", "answered", "no_agent", "no_answer"]) assert.equal(live.livePending({ ...base, state }, now), false);
  assert.equal(live.livePending(undefined, now), false);
});

// A small DOM double: one persistent node per selector, as the pane's regions are.
function pane(t, pages) {
  const doc = { hidden: false, addEventListener() {}, removeEventListener() {} };
  const restore = [];
  for (const [name, value] of Object.entries({ document: doc, navigator: { onLine: true }, sessionStorage: undefined, localStorage: undefined, addEventListener() {}, removeEventListener() {} })) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, { configurable: true, value, writable: true });
    restore.push(() => previous ? Object.defineProperty(globalThis, name, previous) : delete globalThis[name]);
  }
  const timers = new Map(); let next = 0;
  t.mock.method(globalThis, "setTimeout", (fn, delay) => { timers.set(++next, { fn, delay }); return next; });
  t.mock.method(globalThis, "clearTimeout", (id) => timers.delete(id));
  const field = { value: "", focus() {} }, form = { elements: { text: field }, querySelector: () => ({}) };
  const nodes = new Map();
  const host = { innerHTML: "", querySelector(selector) {
    if (!this.innerHTML) return null;
    if (selector === ".chat-compose") return form;
    if (!nodes.has(selector)) nodes.set(selector, { innerHTML: "", dataset: {}, focus() {}, setAttribute() {} });
    return nodes.get(selector);
  } };
  const reads = [], header = [];
  let step = 0;
  const controller = ui.createChat({ mount: () => host, e, me: owner, board: { slug: "work", name: "Work" }, async api(path) {
    if (path === "/api/chat") return { projects: [{ slug: "work", name: "Work" }], default: "work" };
    if (path.endsWith("/status")) return { front: { mode: "basic" }, orchestrator: { name: "a1", visible: true, status: "working" } };
    if (path.startsWith("/api/chat-settings?")) return { front: {} };
    if (path.endsWith("/cards")) return { cards: [] };
    reads.push(path);
    return pages[Math.min(step++, pages.length - 1)];
  } });
  controller.watchHeader((text) => header.push(text));
  t.after(() => { controller.destroy(); for (const undo of restore.reverse()) undo(); });
  const region = (selector) => nodes.get(selector)?.innerHTML || "";
  const poll = async () => { const [id, timer] = [...timers.entries()].at(-1); timers.delete(id); timer.fn(); await settle(); };
  return { controller, host, reads, header, timers, region, poll };
}

const now = () => new Date().toISOString();
const mine = (stamp) => ({ id: 7, actor: "user:me", at: stamp, text: "Please fix the login bug" });
const first = (stamp, state) => ({ board: "work", me: owner, orchestrator: { name: "a1", visible: true }, messages: [mine(stamp)], replies: [], cursor: 7, reply_cursor: 0, oldest: 7, has_more: false, live: { ...base, at: stamp, ...state } });
const next = (stamp, state, messages = [], cursor = 7) => ({ ...first(stamp, state), messages, cursor });

test("delivery, working and the answer appear through the poll, with no reload", async (t) => {
  const stamp = now();
  const answer = { id: 8, actor: "conn:a1", at: stamp, text: "On it: card 7." };
  const h = pane(t, [
    first(stamp, { reason: "wake_pending" }),
    next(stamp, { reason: "wake_sent" }),
    next(stamp, { state: "working", reason: null, seen_at: stamp }),
    next(stamp, { state: "answered", reason: null, answer_id: 8 }, [answer], 8),
  ]);
  await h.controller.show("work"); await settle();
  assert.match(h.region("[data-chat-live]"), /Delivering to a1/);
  // The server's state replaces the timed "Working on it…" guess.
  assert.equal(h.region("[data-chat-recovery]"), "");
  assert.equal([...h.timers.values()].at(-1).delay, 1000);
  await h.poll();
  assert.match(h.region("[data-chat-live]"), /Delivered to a1/);
  assert.equal(h.header.at(-1), "Message delivered");
  await h.poll();
  assert.match(h.region("[data-chat-live]"), /a1 is working on this/);
  assert.equal(h.header.at(-1), "Orchestrator is working on your message");
  await h.poll();
  assert.equal(h.region("[data-chat-live]"), "");
  assert.ok(h.region(".chat-stream").includes("On it: card 7."));
  assert.equal(h.header.at(-1), "Orchestrator connected");
  assert.equal([...h.timers.values()].at(-1).delay, 2000);
  // Every later read continued from the pane's cursors: the page was never loaded again.
  assert.ok(h.reads.slice(1).every((path) => /\?after=\d+&replies_after=\d+$/.test(path)), h.reads.join(" "));
  assert.match(h.reads.at(-1), /after=7&/);
});

test("no agent connected and no answer are alerts in the pane", async (t) => {
  const stamp = now();
  const h = pane(t, [
    first(stamp, { state: "no_agent", reason: "none", agent: null }),
    next(stamp, { state: "no_answer", reason: "not_picked_up" }),
  ]);
  await h.controller.show("work"); await settle();
  assert.match(h.region("[data-chat-live]"), /role="alert".*No agent connected.*Connect an agent in Settings/s);
  assert.equal(h.region("[data-chat-recovery]"), "");
  assert.equal(h.header.at(-1), "No agent connected");
  // Nothing is on its way, so the pane keeps its normal pace.
  assert.equal([...h.timers.values()].at(-1).delay, 2000);
  await h.poll();
  assert.match(h.region("[data-chat-live]"), /role="alert".*a1 did not answer/s);
  assert.match(h.header.at(-1), /^Not answering since \d\d:\d\d$/);
});

test("a page without a live state keeps the pane as it was", async (t) => {
  const stamp = now();
  const page = first(stamp, {});
  delete page.live;
  const h = pane(t, [page]);
  await h.controller.show("work"); await settle();
  assert.equal(h.region("[data-chat-live]"), "");
  assert.match(h.region("[data-chat-recovery]"), /Working on it…/);
  assert.equal([...h.timers.values()].at(-1).delay, 2000);
});

test("with a no-agent receipt the pane's own waiting block, with its actions, stands alone", async (t) => {
  const stamp = now();
  const page = first(stamp, { state: "no_agent", reason: "none", agent: null });
  page.orchestrator = null;
  page.replies = [{ id: 1, message_id: 7, kind: "receipt", code: "no_orchestrator", text: "Not connected yet.", redacted: [], model: null, task: null }];
  page.reply_cursor = 1;
  const h = pane(t, [page, { ...page, messages: [], live: { ...page.live, state: "delivered", reason: "wake_sent", agent: "a1" } }]);
  await h.controller.show("work"); await settle();
  assert.match(h.region("[data-chat-recovery]"), /Waiting for an agent.*Make it a card now/s);
  assert.equal(h.region("[data-chat-live]"), "");
  // An agent connects later: the old receipt no longer holds the pane in "waiting".
  await h.poll();
  assert.equal(h.region("[data-chat-recovery]"), "");
  assert.match(h.region("[data-chat-live]"), /Delivered to a1/);
});
