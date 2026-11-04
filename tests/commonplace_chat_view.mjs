import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../commonplace/web/static/chat.js", import.meta.url), "utf8");
const ui = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
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
