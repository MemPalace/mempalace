import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";

const path = new URL("../commonplace/web/static/chatstate.js", import.meta.url);
assert.ok(existsSync(path), "Chat state module missing");
const source = readFileSync(path, "utf8");
const chat = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
for (const name of ["deriveChatState", "seenStore", "clearSeen"]) {
  assert.equal(typeof chat[name], "function", `${name} export missing`);
}
const { deriveChatState, seenStore, clearSeen } = chat;
const now = Date.parse("2026-01-02T12:00:00Z");
const board = { slug: "atlas", coordinator: "conn:Iris" };
const status = { orchestrator: { visible: true, last_seen: "2026-01-02T11:50:00Z", status: "offline" } };
const message = (id = 10, age = 360, extra = {}) => ({
  id, actor: "user:maya", text: "Build a search panel", at: new Date(now - age * 1000).toISOString(), ...extra,
});
const reply = (id = 1, kind = "receipt", code = "forwarded", extra = {}) => ({
  id, message_id: 10, kind, code, text: "Exact reply", ...extra,
});
const page = (messages = [message()], replies = []) => ({ me: { user: "maya" }, messages, replies });
const derive = (p = page(), s = status, b = board, seen = { message: 0, reply: 0 }) => deriveChatState(p, s, b, now, seen);
function memoryStorage() {
  const values = new Map();
  return {
    get length() { return values.size; },
    key(index) { return [...values.keys()][index] ?? null; },
    getItem(key) { return values.get(key) ?? null; },
    setItem(key, value) { values.set(key, String(value)); },
    removeItem(key) { values.delete(key); },
  };
}
function store(user = "maya", slug = "atlas", storage = memoryStorage()) {
  const result = seenStore(user, slug, storage);
  assert.ok(result && typeof result === "object", "Seen store missing");
  for (const name of ["read", "markSeen", "clearSeen"]) {
    assert.equal(typeof result[name], "function", `${name} method missing`);
  }
  return result;
}
const checks = {
  none() {
    assert.deepEqual(derive(page([])), { state: "none", unread: 0, message_id: null, last_seen: status.orchestrator.last_seen });
    assert.equal(deriveChatState().state, "none");
  },
  waiting() {
    for (const code of ["no_orchestrator", "orchestrator_gone"]) {
      assert.equal(derive(page([message()], [reply(1, "receipt", code)])).state, "waiting");
      assert.equal(derive(page([message(10, 360, { text: "Hi!" })], [reply(1, "receipt", code)])).state, "waiting");
    }
    assert.equal(derive(page(), { orchestrator: null }, { slug: "atlas", coordinator: null }).state, "waiting");
    // A receipt for an older message cannot override the current request.
    assert.equal(derive(page([message(11, 10)], [reply(1, "receipt", "no_orchestrator")])).state, "working");
  },
  working() {
    const result = derive(page([message(10, 30)], [reply()]));
    assert.equal(result.state, "working");
    assert.equal(result.message_id, 10);
  },
  deadline() {
    assert.equal(derive(page([message(10, 300)])).state, "working");
    assert.equal(derive(page([message(10, 300.001)])).state, "no_answer");
    assert.equal(derive(page([message(10, -10)])).state, "working");
  },
  no_answer() {
    const result = derive();
    assert.equal(result.state, "no_answer");
    assert.equal(result.message_id, 10);
    assert.equal(result.last_seen, status.orchestrator.last_seen);
  },
  acknowledged() {
    for (const actor of ["conn:Iris", "conn:Otto"]) {
      assert.equal(derive(page([message(11, 5, { actor }), message()])).state, "ok");
    }
  },
  older_agent() {
    assert.equal(derive(page([message(9, 500, { actor: "conn:Iris" }), message()])).state, "no_answer");
    assert.equal(derive(page([message(), message(11, 1, { actor: "user:leon", text: "Hi!" })])).state, "no_answer");
  },
  quick_reply() {
    // Quick replies and filing receipts do not prove an agent read the request.
    assert.equal(derive(page([message()], [reply(1, "answer", "ok")])).state, "no_answer");
    assert.equal(derive(page([message()], [reply(1, "handoff", "filed")])).state, "no_answer");
  },
  non_work() {
    for (const text of ["", "Hi!", "Thank you so much", "status", "Any news?", "Hoe staat het?", "Is it done?"]) {
      assert.equal(derive(page([message(10, 600, { text })])).state, "ok", text);
    }
    assert.equal(derive(page([message(10, 600, { actor: "user:leon" })])).state, "no_answer");
    assert.equal(derive(page([message(10, 600, { actor: "conn:Iris" })])).state, "ok");
  },
  latest_request() {
    assert.equal(derive(page([message(12, 3), message(10, 600), message(11, 400, { actor: "conn:Iris" })])).state, "working");
    assert.equal(derive(page([message(), message(11, 5, { text: "Thanks!" })])).state, "no_answer");
  },
  private_status() {
    const privateStatus = { orchestrator: { visible: false, last_seen: "private", status: null } };
    assert.equal(derive(page(), privateStatus).last_seen, null);
    assert.equal(derive(page(), privateStatus).state, "no_answer");
    assert.equal(derive(page(), null).last_seen, null);
    assert.equal(derive(page([message(10, 360, { at: "invalid" })])).state, "working");
  },
  people_unread() {
    assert.equal(derive(page([message(), message(11, 1, { actor: "user:leon" })])).unread, 0);
  },
  agents_unread() {
    assert.equal(derive(page([message(11, 1, { actor: "conn:Otto" })])).unread, 1);
    assert.equal(derive(page([message(11, 1, { actor: "system" })])).unread, 0);
  },
  answers_unread() {
    assert.equal(derive(page([], [reply(2, "answer", "ok")])).unread, 1);
    assert.equal(derive(page([], [reply(2, "answer", "ok", { for_user: "leon" })])).unread, 0);
  },
  handoffs_unread() {
    for (const code of ["filed", "file_failed", "claimed", "no_access"]) {
      assert.equal(derive(page([], [reply(2, "handoff", code)])).unread, 1, code);
    }
  },
  receipts_unread() {
    assert.equal(derive(page([], [reply(1), reply(2, "error", "timeout")])).unread, 0);
  },
  seen_boundaries() {
    const p = page([message(11, 1, { actor: "conn:Iris" }), message(12, 0, { actor: "conn:Otto" })], [reply(4, "answer"), reply(5, "handoff")]);
    assert.equal(derive(p, status, board, { message: 11, reply: 4 }).unread, 2);
    assert.equal(derive(p, status, board, { message: 12, reply: 5 }).unread, 0);
  },
  duplicates() {
    const m = message(11, 0, { actor: "conn:Iris" }), r = reply(3, "answer");
    assert.equal(derive(page([m, m], [r, r])).unread, 2);
  },
  baseline() {
    const storage = memoryStorage(), seen = store("maya", "atlas", storage);
    const history = Array.from({ length: 20 }, (_, n) => message(n + 1, 600, { actor: "conn:Iris" }));
    const p = page(history, [reply(5, "answer")]);
    assert.deepEqual(seen.read(p), { message: 20, reply: 5 });
    assert.equal(derive(p, status, board, seen.read(p)).unread, 0);
    const added = page([...history, message(21, 0, { actor: "conn:Otto" })]);
    assert.equal(derive(added, status, board, seen.read(added)).unread, 1);
    added.messages.push(message(22, 0, { actor: "user:leon" }));
    assert.equal(derive(added, status, board, seen.read(added)).unread, 1);
  },
  baseline_cursors() {
    const seen = store();
    assert.deepEqual(seen.read({ ...page([]), cursor: 40, reply_cursor: 70 }), { message: 40, reply: 70 });
    assert.deepEqual(seen.markSeen(page([message(50)], [reply(80)])), { message: 50, reply: 80 });
    assert.deepEqual(seen.markSeen(page([message(2)], [reply(3)])), { message: 50, reply: 80 });
    assert.deepEqual(store().read(page([])), { message: 0, reply: 0 });
  },
  ids_only() {
    const storage = memoryStorage(), seen = store("maya", "atlas", storage);
    seen.read(page());
    seen.markSeen(page([message(12, 0, { text: "Private exact words" })], [reply(5)]));
    assert.equal(storage.length, 1);
    assert.equal(storage.key(0), "cmmn.chat.seen.maya.atlas");
    assert.deepEqual(JSON.parse(storage.getItem(storage.key(0))), { message: 12, reply: 5 });
    assert.ok(!storage.getItem(storage.key(0)).includes("text"));
    const returned = seen.read(page()); returned.message = 999;
    assert.equal(seen.read(page()).message, 12);
  },
  reload() {
    const storage = memoryStorage();
    // Seen ids live on the device (localStorage), never only in the tab's session.
    globalThis.localStorage = storage;
    globalThis.sessionStorage = { getItem() { assert.fail("Seen ids must not be session-only"); }, setItem() { assert.fail("Seen ids must not be session-only"); } };
    seenStore("maya", "atlas").read(page());
    assert.equal(storage.getItem("cmmn.chat.seen.maya.atlas"), '{"message":10,"reply":0}');
    const p = page([message(11, 0, { actor: "conn:Iris" })]);
    assert.equal(derive(p, status, board, seenStore("maya", "atlas").read(p)).unread, 1);
    delete globalThis.sessionStorage; delete globalThis.localStorage;
  },
  isolation() {
    const storage = memoryStorage();
    store("maya", "atlas", storage).read(page());
    assert.deepEqual(store("leon", "atlas", storage).read(page([])), { message: 0, reply: 0 });
    assert.deepEqual(store("maya", "harbor", storage).read(page([])), { message: 0, reply: 0 });
    store("a.b", "c", storage).read(page());
    assert.deepEqual(store("a", "b.c", storage).read(page([])), { message: 0, reply: 0 });
  },
  clear_board() {
    const storage = memoryStorage(), seen = store("maya", "atlas", storage);
    seen.read(page()); seen.clearSeen();
    assert.equal(storage.length, 0);
    assert.deepEqual(seen.read(page([])), { message: 0, reply: 0 });
  },
  clear_user() {
    const storage = memoryStorage();
    for (const slug of ["atlas", "harbor"]) store("maya", slug, storage).read(page());
    store("leon", "atlas", storage).read(page());
    storage.setItem("other.setting", "keep");
    clearSeen("maya", storage);
    assert.equal(storage.length, 2);
    assert.equal(storage.getItem("cmmn.chat.seen.leon.atlas"), '{"message":10,"reply":0}');
    assert.equal(storage.getItem("other.setting"), "keep");
  },
  storage_denied() {
    const denied = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); }, removeItem() { throw new Error("denied"); } };
    const seen = store("maya", "atlas", denied);
    assert.deepEqual(seen.read(page()), { message: 10, reply: 0 });
    assert.deepEqual(seen.read(page([message(11)])), { message: 10, reply: 0 });
    assert.deepEqual(seen.markSeen(page([message(12)])), { message: 12, reply: 0 });
    seen.clearSeen(); clearSeen("maya", denied);
    assert.deepEqual(seen.read(page([])), { message: 0, reply: 0 });
    const full = store("maya", "atlas", { ...denied, getItem() { return null; } });
    full.read(page());
    assert.deepEqual(full.read(page([message(11)])), { message: 10, reply: 0 });
    Object.defineProperty(globalThis, "localStorage", { configurable: true, get() { throw new Error("denied"); } });
    const unavailable = store();
    // Explicit storage works even when accessing the browser's storage is denied.
    assert.deepEqual(unavailable.read(page()), { message: 10, reply: 0 });
    const missing = seenStore("maya", "atlas");
    assert.equal(typeof missing.read, "function");
    assert.deepEqual(missing.read(page()), { message: 10, reply: 0 });
    assert.deepEqual(missing.read(page([message(11)])), { message: 10, reply: 0 });
    delete globalThis.localStorage;
  },
  malformed_store() {
    for (const value of ["broken", "null", "[]", '{"message":-1,"reply":2}', '{"message":"7","reply":2}', '{"text":"private"}']) {
      const storage = memoryStorage(); storage.setItem("cmmn.chat.seen.maya.atlas", value);
      assert.deepEqual(store("maya", "atlas", storage).read(page()), { message: 10, reply: 0 });
    }
  },
  receipt_newest() {
    // An acknowledged work request, then a non-work message whose receipt says nobody is there.
    const result = derive(page(
      [message(10, 360), message(11, 300, { actor: "conn:Iris" }), message(12, 10, { text: "hi" })],
      [reply(1, "receipt", "no_orchestrator", { message_id: 12 })],
    ));
    assert.equal(result.state, "waiting");
    assert.equal(result.message_id, 12);
  },
  peek() {
    const s = store();
    assert.equal(typeof s.peek, "function", "peek method missing");
    assert.equal(s.peek(), null);
    s.read(page());
    assert.deepEqual(s.peek(), { message: 10, reply: 0 });
  },
  pure() {
    const p = page([message(), message(11, 1, { actor: "conn:Iris" })], [reply(2, "answer")]);
    const seen = { message: 9, reply: 1 };
    const before = JSON.stringify([p, status, board, seen]);
    for (const collection of [p.messages, p.replies]) { collection.forEach(Object.freeze); Object.freeze(collection); }
    for (const object of [p, p.me, status.orchestrator, status, board, seen]) Object.freeze(object);
    const result = derive(p, status, board, seen);
    assert.deepEqual(derive(p, status, board, seen), result);
    assert.equal(JSON.stringify([p, status, board, seen]), before);
    assert.equal(result.state, "ok");
  },
};
assert.ok(Object.hasOwn(checks, process.argv[2]), "Unknown chat state check");
checks[process.argv[2]]();
console.log(`${process.argv[2]}: PASS`);
