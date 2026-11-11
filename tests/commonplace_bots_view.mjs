import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const stream = await import("../commonplace/web/static/botstream.js");
const ui = await import("../commonplace/web/static/bots.js");
const read = (name) => readFile(new URL(`../commonplace/web/static/${name}`, import.meta.url), "utf8");
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const text = (html) => html.replace(/<[^>]*>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">");
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise((resolve) => setImmediate(resolve)); };
const HOSTILE = "<img src=x onerror=alert(1)>";

// ---------- stream parser ----------

const WIRE = [
  ": ping\n\n",
  'event: ack\ndata: {"message":{"id":7}}\n\n',
  'event: delta\r\ndata: {"text":"Hel\\nlo"}\r\n\r\n',
  "event: delta\ndata: {\"a\":1,\ndata: \"b\":2}\n\n",
  ": ping\n",
  "\n",
  "event: other\ndata: {}\n\n",
  'data: {"plain":true}\n\n',
].join("");
const FRAMES = [
  { event: "ack", data: '{"message":{"id":7}}' },
  { event: "delta", data: '{"text":"Hel\\nlo"}' },
  { event: "delta", data: '{"a":1,\n"b":2}' },
  { event: "other", data: "{}" },
  { event: "message", data: '{"plain":true}' },
];

test("frame parser yields the same frames when cut at every byte position", () => {
  const whole = [];
  const parser = stream.createFrameParser((f) => whole.push(f));
  parser.push(WIRE); parser.end();
  assert.deepEqual(whole, FRAMES);
  const bytes = new TextEncoder().encode(WIRE);
  for (let cut = 0; cut <= bytes.length; cut++) {
    const got = [], decoder = new TextDecoder(), p = stream.createFrameParser((f) => got.push(f));
    p.push(decoder.decode(bytes.slice(0, cut), { stream: true }));
    p.push(decoder.decode(bytes.slice(cut), { stream: true }));
    p.end();
    assert.deepEqual(got, FRAMES, `cut at ${cut}`);
  }
  const single = [], p = stream.createFrameParser((f) => single.push(f));
  for (const ch of WIRE) p.push(ch);
  p.end();
  assert.deepEqual(single, FRAMES);
});

test("frame parser drops an unterminated frame and comment-only input", () => {
  const got = [], p = stream.createFrameParser((f) => got.push(f));
  p.push(": ping\n\n: ping\n\nevent: delta\ndata: {\"text\":\"x\"}");
  p.end();
  assert.deepEqual(got, []);
});

// ---------- sendMessage ----------

const enc = new TextEncoder();
function body(chunks, { failAfter = false } = {}) {
  let i = 0;
  return new ReadableStream({
    pull(controller) {
      if (i < chunks.length) controller.enqueue(enc.encode(chunks[i++]));
      else if (failAfter) controller.error(new Error("reset"));
      else controller.close();
    },
  });
}
const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
function fakeFetch(respond) {
  const calls = [];
  const fn = async (url, init) => { calls.push({ url, init }); return respond(url, init); };
  fn.calls = calls;
  return fn;
}
const json = (status, payload) => new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });

test("sendMessage posts JSON with credentials and maps every stream event", async () => {
  const seen = [];
  const f = fakeFetch(() => new Response(body([
    ": ping\n\n", frame("ack", { message: { id: 5 } }),
    frame("delta", { text: "Hi" }).slice(0, 12), frame("delta", { text: "Hi" }).slice(12),
    frame("action", { step: 0, tool: "create_work", label: "Filing", state: "running" }),
    frame("mystery", { x: 1 }),
    frame("block", { type: "work", card: 3, title: "T" }),
    frame("done", { message: { id: 6 }, timing: { first_token_ms: 1, total_ms: 2 } }),
    frame("delta", { text: "late" }),
  ]), { status: 200, headers: { "Content-Type": "text/event-stream" } }));
  const result = await stream.sendMessage({ botId: "b 1", text: "hello", requestId: "req_12345678", onEvent: (n, d) => seen.push([n, d]), fetchImpl: f });
  assert.deepEqual(result, { state: "done", acked: true });
  assert.deepEqual(seen.map(([n]) => n), ["ack", "delta", "action", "block", "done"]);
  assert.equal(seen[1][1].text, "Hi");
  assert.equal(f.calls[0].url, "/api/bots/b%201/messages");
  assert.equal(f.calls[0].init.method, "POST");
  assert.equal(f.calls[0].init.credentials, "same-origin");
  assert.deepEqual(JSON.parse(f.calls[0].init.body), { text: "hello", request_id: "req_12345678" });
});

for (const [status, code, error] of [[400, "invalid", "Text is too long."], [404, "not_found", "No such bot."], [409, "busy", "Another turn is running."], [409, "conflict", "Different text."], [429, "busy", "Too many streams."], [429, "rate", "Slow down."]]) {
  test(`sendMessage turns a pre-stream ${status} ${code} into a SendError`, async () => {
    const f = fakeFetch(() => json(status, { error, code }));
    await assert.rejects(stream.sendMessage({ botId: "b", text: "x", requestId: "req_12345678", fetchImpl: f }), (err) => {
      assert.ok(err instanceof stream.SendError);
      assert.equal(err.status, status); assert.equal(err.code, code); assert.equal(err.message, error);
      return true;
    });
  });
}

test("sendMessage: a bare 429, a dead network and an abort before ack are SendErrors", async () => {
  await assert.rejects(stream.sendMessage({ botId: "b", text: "x", requestId: "req_12345678", fetchImpl: fakeFetch(() => new Response("", { status: 429 })) }), (err) => err.code === "rate" && err.status === 429 && /minute/.test(err.message));
  await assert.rejects(stream.sendMessage({ botId: "b", text: "x", requestId: "req_12345678", fetchImpl: async () => { throw new TypeError("fail"); } }), (err) => err.code === "network" && err.status === 0);
  const ac = new AbortController(); ac.abort();
  await assert.rejects(stream.sendMessage({ botId: "b", text: "x", requestId: "req_12345678", signal: ac.signal, fetchImpl: async () => { throw new DOMException("x", "AbortError"); } }), (err) => err.code === "aborted");
});

test("sendMessage: error and running events end the stream; a drop after ack is not a failure", async () => {
  const err = await stream.sendMessage({ botId: "b", text: "x", requestId: "req_12345678", fetchImpl: fakeFetch(() => new Response(body([frame("ack", { message: { id: 1 } }), frame("error", { code: "limit", text: "Limit", message: { id: 2 } })]))) });
  assert.deepEqual(err, { state: "error", acked: true });
  const running = await stream.sendMessage({ botId: "b", text: "x", requestId: "req_12345678", fetchImpl: fakeFetch(() => new Response(body([frame("ack", { message: { id: 1 } }), frame("running", {})]))) });
  assert.deepEqual(running, { state: "running", acked: true });
  const seen = [];
  const dropped = await stream.sendMessage({ botId: "b", text: "x", requestId: "req_12345678", onEvent: (n) => seen.push(n), fetchImpl: fakeFetch(() => new Response(body([frame("ack", { message: { id: 1 } }), frame("delta", { text: "a" })], { failAfter: true }))) });
  assert.deepEqual(dropped, { state: "dropped", acked: true });
  assert.deepEqual(seen, ["ack", "delta"]);
  const ended = await stream.sendMessage({ botId: "b", text: "x", requestId: "req_12345678", fetchImpl: fakeFetch(() => new Response(body([frame("ack", { message: { id: 1 } })]))) });
  assert.deepEqual(ended, { state: "dropped", acked: true });
  await assert.rejects(stream.sendMessage({ botId: "b", text: "x", requestId: "req_12345678", fetchImpl: fakeFetch(() => new Response(body([": ping\n\n"], { failAfter: true }))) }), (e2) => e2.code === "network");
});

// ---------- view: markup ----------

test("roster rows: initial, name, last line or job, unread, one state dot, server order", () => {
  const bots = [
    { id: "a", name: "Atlas", job: "Atlas project", last: { role: "bot", text: "Filed it.\n[redacted:token]", at: "2026-11-11T08:00:00Z" }, unread: 3, waiting: 1, state: "needs_you" },
    { id: "b", name: "General", job: "Anything else", last: null, unread: 0, waiting: 0, state: "idle" },
    { id: "c", name: "Harbor", job: "", last: { role: "user", text: "status?", at: "2026-11-11T08:00:00Z" }, unread: 0, waiting: 0, state: "working" },
  ];
  const html = ui.rosterHTML({ bots, current: "b" }, e);
  assert.ok(html.indexOf("Atlas") < html.indexOf("General") && html.indexOf("General") < html.indexOf("Harbor"));
  assert.match(html, /href="#\/bots\/a"/);
  assert.match(html, /<span class="bot-av" aria-hidden="true">A<\/span>/);
  assert.match(html, /bot-dot needs_you" role="img" aria-label="Needs you"/);
  assert.match(html, /bot-dot working" role="img" aria-label="Working"/);
  assert.equal((html.match(/class="bot-dot/g) || []).length, 2);
  assert.match(html, /bot-unread">3</);
  assert.match(html, /Anything else/);
  assert.match(html, /You: status\?/);
  assert.match(html, /<mark class="redacted">\[redacted:token\]<\/mark>/);
  assert.match(html, /<a class="bot-row" href="#\/bots\/b"[^>]*aria-current="page"/);
});

test("hostile names, messages and block titles are escaped everywhere", () => {
  const html = ui.rosterHTML({ bots: [{ id: "a", name: HOSTILE, job: HOSTILE, last: { role: "bot", text: HOSTILE, at: "x" }, unread: 0, state: "idle" }] }, e)
    + ui.headHTML({ name: HOSTILE, job: HOSTILE }, e) + ui.composerHTML({ name: HOSTILE }, e)
    + ui.messageHTML({ id: 1, role: "bot", text: HOSTILE, state: "ok", blocks: [
      { type: "work", card: 1, title: HOSTILE, status: "ready", note: HOSTILE, links: [{ label: HOSTILE, url: "javascript:alert(1)" }, { label: HOSTILE, url: "https://example.com/?a=1&b=<x>" }] },
      { type: "decision", card: 2, question: HOSTILE, context: HOSTILE, options: [HOSTILE], version: HOSTILE, open: true },
      { type: "confirm", pending: HOSTILE, label: HOSTILE, state: "open" },
      { type: "action", label: HOSTILE, ok: true },
    ] }, { open: new Set(["work:1:1"]), busy: new Set() }, e)
    + ui.sheetHTML({ kind: "bot", memories: [{ id: HOSTILE, text: HOSTILE }] }, { name: HOSTILE, job: HOSTILE, instructions: HOSTILE }, e)
    + ui.modelNoteHTML({ error: HOSTILE }, e);
  assert.doesNotMatch(html, /<img/);
  assert.ok(html.includes("&lt;img src=x onerror=alert(1)&gt;"));
  assert.doesNotMatch(html, /javascript:/);
  assert.match(html, /href="https:\/\/example\.com\/\?a=1&amp;b=%3Cx%3E"/);
});

const wording = (html) => assert.doesNotMatch(text(html), /\b(cards?|boards?|tickets?)\b/i);

test("every block type renders: action grouping, work chip and detail, decision, confirm, setup, gone", () => {
  const blocks = [
    { type: "action", tool: "create_work", label: "Filed “Fix login”", ok: true },
    { type: "action", tool: "tell_orchestrator", label: "Told the orchestrator", ok: true },
    { type: "action", tool: "start_work", label: "Could not start", ok: false },
    { type: "work", card: 57, title: "Fix login", status: "needs_you", note: "Waiting on a key", links: [{ label: "Pull request", url: "https://example.com/pr/1" }, { label: "Bad", url: "ftp://x" }], version: 2 },
    { type: "work", card: 58, gone: true },
    { type: "decision", card: 60, question: "Ship it?", context: "Tests pass", options: ["Yes", "No"], version: 4, open: true, answer: null },
    { type: "decision", card: 61, question: "Old?", options: ["A"], version: 1, open: false, answer: "A" },
    { type: "confirm", pending: "p1", label: "Start work on “Fix login”", state: "open" },
    { type: "confirm", pending: "p2", label: "Stopped a start", state: "done" },
    { type: "setup", code: "needs_configuration", can_configure: true },
    { type: "unknown", x: 1 },
  ];
  const collapsed = ui.messageHTML({ id: 9, role: "bot", text: "Done.", blocks, state: "ok" }, { open: new Set(), busy: new Set() }, e);
  assert.equal((collapsed.match(/<p class="acts">/g) || []).length, 1, "actions share one line");
  assert.match(collapsed, /<p class="acts">[^]*Filed [^]*Told the orchestrator[^]*act no">Could not start/);
  assert.match(collapsed, /chip needs_you">Needs you</);
  assert.doesNotMatch(collapsed, /Waiting on a key|Pull request/, "detail stays folded until opened");
  assert.match(collapsed, /aria-expanded="false"/);
  assert.equal((collapsed.match(/class="bb-gone"/g) || []).length, 1);
  assert.match(collapsed, /data-bots-action="decide" data-work="60" data-version="4" data-choice="Yes"/);
  assert.match(collapsed, /data-bots-form="reply" data-work="60" data-version="4"/);
  assert.match(collapsed, /Answered: A/);
  assert.match(collapsed, /data-bots-action="confirm" data-pending="p1" data-accept="1"/);
  assert.match(collapsed, /data-accept="0"/);
  assert.match(collapsed, /Stopped a start · Done/);
  assert.match(collapsed, /Open settings/);
  const open = ui.messageHTML({ id: 9, role: "bot", text: "Done.", blocks, state: "ok" }, { open: new Set(["work:57:9"]), busy: new Set() }, e);
  assert.match(open, /Waiting on a key/);
  assert.match(open, /href="https:\/\/example\.com\/pr\/1"[^>]*rel="noopener noreferrer"/);
  assert.doesNotMatch(open, /ftp:/);
  wording(open);
  for (const status of ["queued", "working", "needs_you", "ready", "blocked", "done"]) {
    assert.match(ui.workHTML({ card: 1, title: "t", status }, { open: new Set() }, e), new RegExp(`chip ${status}">(Queued|Working|Needs you|Ready|Blocked|Done)<`));
  }
});

test("setup text differs for owner and member and names one step", () => {
  for (const code of ["needs_configuration", "needs_turn_on", "key_missing"]) {
    const owner = ui.setupHTML({ code, can_configure: true }, e), member = ui.setupHTML({ code, can_configure: false }, e);
    assert.match(owner, /href="#\/settings"/);
    assert.doesNotMatch(member, /href=/);
    assert.match(text(member), /Ask the workspace owner/);
    assert.doesNotMatch(text(owner), /Ask the workspace owner/);
    wording(owner); wording(member);
  }
});

test("stopped and failed replies carry one plain note; redaction markers become marks", () => {
  const ctx = { open: new Set(), busy: new Set() };
  assert.match(ui.messageHTML({ id: 1, role: "bot", text: "Part", state: "stopped", blocks: [] }, ctx, e), /Stopped\./);
  assert.match(ui.messageHTML({ id: 1, role: "bot", text: "", state: "failed:interrupted", blocks: [] }, ctx, e), /interrupted/);
  const user = ui.messageHTML({ id: 2, role: "user", text: "key [redacted:secret]", blocks: [] }, ctx, e);
  assert.match(user, /class="bm me"/);
  assert.match(user, /<mark class="redacted">\[redacted:secret\]<\/mark>/);
});

test("an event with an orchestrator block carries an Orchestrator label and escaped text", () => {
  const ctx = { open: new Set(), busy: new Set() };
  const reply = ui.messageHTML({ id: 4, role: "event", text: `Done <img src=x onerror=alert(1)> [redacted:key]`, blocks: [{ type: "orchestrator" }], state: "ok" }, ctx, e);
  assert.match(reply, /<p class="bm-from">Orchestrator<\/p>/);
  assert.doesNotMatch(reply, /<img/);
  assert.match(reply, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(reply, /<mark class="redacted">\[redacted:key\]<\/mark>/);
  assert.doesNotMatch(reply, /bb /, "the marker block draws nothing of its own");
  const plain = ui.messageHTML({ id: 5, role: "event", text: "“Fix” is done.", blocks: [{ type: "work", card: 1, title: "Fix", status: "done" }], state: "ok" }, ctx, e);
  assert.doesNotMatch(plain, /Orchestrator/);
  const bot = ui.messageHTML({ id: 6, role: "bot", text: "hi", blocks: [{ type: "orchestrator" }], state: "ok" }, ctx, e);
  assert.doesNotMatch(bot, /Orchestrator/, "only event messages are labelled");
});

test("server keys that match inherited object members get the default text, never a function or a throw", () => {
  const ctx = { open: new Set(), busy: new Set() };
  for (const key of ["constructor", "__proto__", "toString", "hasOwnProperty"]) {
    assert.match(ui.setupHTML({ code: key, can_configure: true }, e), /Bots need a chat model/, `setup ${key}`);
    assert.equal(ui.setupText(key, false), ui.setupText("needs_configuration", false));
    assert.doesNotMatch(ui.workHTML({ card: 1, title: "t", status: key }, ctx, e), /chip|function|native code/, `status ${key}`);
    const row = ui.rosterHTML({ bots: [{ id: "a", name: "A", job: "", last: null, unread: 0, state: key }] }, e);
    assert.doesNotMatch(row, /bot-dot|function|native code/, `dot ${key}`);
    const note = ui.messageHTML({ id: 1, role: "bot", text: "x", state: `failed:${key}`, blocks: [] }, ctx, e);
    assert.match(note, /The reply failed\./, `fail ${key}`);
    assert.doesNotMatch(note, /function|native code/);
    assert.doesNotMatch(ui.confirmHTML({ pending: "p", label: "L", state: key }, ctx, e), /function|native code| · /, `confirm ${key}`);
  }
});

// ---------- view: controller ----------

function dom() {
  const regions = {};
  const node = (name) => {
    const n = {
      name, _html: "", writes: 0, scrollTop: 0, scrollHeight: 400, clientHeight: 400,
      get innerHTML() { return this._html; }, set innerHTML(v) { this._html = v; this.writes++; },
      setAttribute() {}, querySelector: () => null,
    };
    regions[name] = n;
    return n;
  };
  for (const name of ["model", "roster", "head", "messages", "waiting", "notice", "composer", "send", "sheet"]) node(name);
  const box = { name: "text", value: "", style: {}, scrollHeight: 20, focus() {}, closest: () => ({}) };
  const live = { innerHTML: "" };
  regions.composer.querySelector = () => box;
  regions.messages.querySelector = (sel) => sel === "[data-stream]" && regions.messages._html.includes("data-stream") ? live : null;
  const bots = { attrs: {}, addEventListener() {}, setAttribute(k, v) { this.attrs[k] = v; } };
  const mountObj = {
    shell: "", set innerHTML(v) { this.shell = v; }, get innerHTML() { return this.shell; },
    querySelector(sel) { if (sel === ".bots") return bots; const m = /data-region="(\w+)"/.exec(sel); return m ? regions[m[1]] : null; },
  };
  return { mount: mountObj, regions, box, live, bots };
}

const msg = (id, role, extra = {}) => ({ id, role, text: "", blocks: [], at: "2026-11-11T08:00:00Z", state: "ok", reply_to: null, ...extra });
const botRow = (id, name, extra = {}) => ({ id, name, job: `${name} job`, instructions: "", kind: "custom", board: null, last: null, unread: 0, waiting: 0, state: "idle", ...extra });

function harness({ bots = [botRow("b1", "Atlas")], messages = [], waiting = [], running = false, model = { ready: true, label: "local", code: "" }, canConfigure = true, send, hidden = false, phone = false, onRoster } = {}) {
  const d = dom();
  const calls = [];
  const timers = new Map();
  let nextTimer = 0;
  const state = { bots, messages, waiting, running, hidden, phone, extra: {} };
  const api = async (path, options = {}) => {
    const method = options.method || "GET";
    calls.push({ method, path, body: options.body });
    if (state.extra[`${method} ${path}`]) return state.extra[`${method} ${path}`](options.body);
    for (const [key, handler] of Object.entries(state.extra)) if (key.endsWith("*") && `${method} ${path}`.startsWith(key.slice(0, -1))) return handler(options.body);
    if (method === "GET" && path === "/api/bots") return { bots: state.bots, model, can_configure: canConfigure };
    const page = /^\/api\/bots\/([^/?]+)\/messages\?(.*)$/.exec(path);
    if (method === "GET" && page) {
      const params = new URLSearchParams(page[2]);
      const after = Number(params.get("after") || 0);
      return { bot: state.bots.find((b) => b.id === page[1]) || state.bots[0], messages: state.messages.filter((m) => m.id > after), waiting: state.waiting, running: state.running };
    }
    if (method === "POST" && /\/read$/.test(path)) return { ok: true };
    throw Object.assign(new Error(`unexpected ${method} ${path}`), { status: 500 });
  };
  const gone = [];
  const controller = ui.createBots({
    mount: () => d.mount, api, e, send, makeId: (() => { let n = 0; return () => `req_auto_${++n}00000`; })(),
    setTimer: (fn, delay) => { const id = ++nextTimer; timers.set(id, { fn, delay }); return id; },
    clearTimer: (id) => timers.delete(id),
    hidden: () => state.hidden, phone: () => state.phone, go: (hash) => gone.push(hash), viewport: null, onRoster,
  });
  const fire = async (delay) => { for (const [id, t] of [...timers]) if (t.delay === delay) { timers.delete(id); await t.fn(); } await settle(); };
  return { d, calls, timers, state, controller, fire, gone, api };
}
const el = (dataset, extra = {}) => ({ dataset, ...extra });
const form = (dataset, fields) => ({ dataset, elements: Object.fromEntries(Object.entries(fields).map(([k, v]) => [k, { value: v }])) });

test("roster paints on show; the roster is polled every 15 s while visible and not when the tab is hidden", async () => {
  const h = harness({ bots: [botRow("b1", "Atlas", { state: "working" }), botRow("b2", "General")] });
  await h.controller.show(null);
  assert.match(h.d.regions.roster.innerHTML, /Atlas/);
  assert.match(h.d.regions.roster.innerHTML, /bot-dot working/);
  assert.match(h.d.regions.messages.innerHTML, /Choose a bot/);
  assert.equal(h.d.bots.attrs["data-pane"], "roster");
  assert.deepEqual([...h.timers.values()].map((t) => t.delay), [15000]);
  const before = h.calls.length;
  h.state.hidden = true;
  await h.fire(15000);
  assert.equal(h.calls.length, before, "no request while hidden");
  h.state.hidden = false;
  h.state.bots = [botRow("b1", "Atlas"), botRow("b3", "Harbor")];
  await h.fire(15000);
  assert.match(h.d.regions.roster.innerHTML, /Harbor/);
  h.controller.stop();
  assert.equal(h.timers.size, 0);
});

test("a roster failure shows a retry line; no usable model shows the setup note for owners and members", async () => {
  const h = harness({ model: { ready: false, label: "", code: "needs_configuration" }, canConfigure: false });
  await h.controller.show(null);
  assert.match(h.d.regions.model.innerHTML, /Ask the workspace owner/);
  const o = harness({ model: { ready: false, label: "", code: "needs_configuration" }, canConfigure: true });
  await o.controller.show(null);
  assert.match(o.d.regions.model.innerHTML, /Open settings/);
  const f = harness();
  f.state.extra["GET /api/bots"] = async () => { throw new Error("Request failed (500)"); };
  await f.controller.show(null);
  assert.match(f.d.regions.model.innerHTML, /Request failed \(500\)[^]*data-bots-action="retry"/);
});

test("opening a thread loads messages, marks them read, polls with after= every 5 s and pauses when hidden", async () => {
  const h = harness({ messages: [msg(1, "user", { text: "hi" }), msg(2, "bot", { text: "hello", reply_to: 1 })] });
  await h.controller.show("b1");
  assert.equal(h.d.bots.attrs["data-pane"], "thread");
  assert.match(h.d.regions.messages.innerHTML, /hi[^]*hello/);
  assert.match(h.d.regions.head.innerHTML, /Atlas/);
  assert.deepEqual(h.calls.filter((c) => c.path.endsWith("/read")).map((c) => c.body), [{ upto: 2 }]);
  assert.ok(h.timers.size === 2 && [...h.timers.values()].some((t) => t.delay === 5000));
  h.state.messages.push(msg(3, "bot", { text: "new one" }));
  await h.fire(5000);
  assert.ok(h.calls.some((c) => c.path === "/api/bots/b1/messages?after=2&limit=50"));
  assert.match(h.d.regions.messages.innerHTML, /new one/);
  assert.deepEqual(h.calls.filter((c) => c.path.endsWith("/read")).map((c) => c.body), [{ upto: 2 }, { upto: 3 }]);
  const n = h.calls.length;
  h.state.hidden = true;
  await h.fire(5000);
  assert.equal(h.calls.length, n);
});

test("optimistic echo, then the bot bubble opens at once and fills from deltas without repainting the thread", async () => {
  let gate;
  const h = harness({
    send: async ({ botId, text: sent, requestId, onEvent }) => {
      assert.equal(botId, "b1"); assert.equal(sent, "Do the thing"); assert.match(requestId, /^req_auto_/);
      await new Promise((resolve) => { gate = resolve; });
      onEvent("ack", { message: msg(10, "user", { text: sent }) });
      onEvent("delta", { text: "Sure, " });
      const writes = h.d.regions.messages.writes;
      onEvent("delta", { text: "on it <b>" });
      assert.equal(h.d.regions.messages.writes, writes, "deltas update the open bubble in place");
      assert.equal(h.d.live.innerHTML, "Sure, on it &lt;b&gt;");
      onEvent("action", { step: 0, tool: "create_work", label: "Filing", state: "running" });
      onEvent("action", { step: 0, tool: "create_work", label: "Filed “Do the thing”", state: "ok" });
      onEvent("action", { step: 1, tool: "tell_orchestrator", label: "Told the orchestrator", state: "ok" });
      onEvent("block", { type: "work", card: 5, title: "Do the thing", status: "queued", version: 1 });
      onEvent("done", { message: msg(11, "bot", { text: "Sure, on it <b>", reply_to: 10, blocks: [{ type: "action", label: "Filed “Do the thing”", ok: true }, { type: "work", card: 5, title: "Do the thing", status: "queued" }] }), timing: {} });
      return { state: "done", acked: true };
    },
  });
  await h.controller.show("b1");
  h.d.box.value = "Do the thing";
  const sending = h.controller.submit({ dataset: { botsForm: "send" }, elements: { text: h.d.box } });
  await settle();
  assert.match(h.d.regions.messages.innerHTML, /class="bm me echo"[^]*Do the thing/, "the person's message shows at once");
  assert.match(h.d.regions.messages.innerHTML, /class="bm bot live"[^]*data-stream/, "an empty bot bubble is already open");
  assert.equal(h.d.box.value, "", "composer cleared");
  assert.match(h.d.regions.send.innerHTML, /data-bots-action="stop"/);
  gate();
  await sending;
  await settle();
  const html = h.d.regions.messages.innerHTML;
  assert.doesNotMatch(html, /echo|live/);
  assert.equal((html.match(/Do the thing/g) || []).length >= 2, true);
  assert.match(html, /Sure, on it &lt;b&gt;/);
  assert.match(html, /<p class="acts">/);
  assert.match(h.d.regions.send.innerHTML, /type="submit"/);
});

test("action lines of a turn in flight share one line and show the running one", async () => {
  let gate, release;
  const h = harness({
    send: async ({ onEvent }) => {
      onEvent("ack", { message: msg(10, "user", { text: "go" }) });
      onEvent("action", { step: 0, tool: "a", label: "Looking", state: "ok" });
      onEvent("action", { step: 1, tool: "b", label: "Filing", state: "running" });
      gate = h.d.regions.messages.innerHTML;
      await new Promise((resolve) => { release = resolve; });
      return { state: "dropped", acked: true };
    },
  });
  await h.controller.show("b1");
  h.d.box.value = "go";
  void h.controller.submit({ dataset: { botsForm: "send" }, elements: { text: h.d.box } });
  await settle();
  assert.equal((gate.match(/<p class="acts">/g) || []).length, 1);
  assert.match(gate, /Looking[^]*act run">Filing/);
  release();
  h.controller.stop();
});

test("a decision button calls the decide route with the hydrated version and then refreshes", async () => {
  const decision = { type: "decision", card: 60, question: "Ship it?", options: ["Yes", "No"], version: 4, open: true, answer: null };
  const h = harness({ messages: [msg(1, "bot", { text: "Question", blocks: [decision] })], waiting: [decision] });
  h.state.extra["POST /api/cards/60/decide"] = async () => { h.state.messages = [msg(1, "bot", { text: "Question", blocks: [{ ...decision, open: false, answer: "Yes" }] })]; h.state.waiting = []; return { ok: true }; };
  await h.controller.show("b1");
  assert.match(h.d.regions.waiting.innerHTML, /Waiting on you \(1\)/);
  h.controller.click(el({ botsAction: "decide", work: "60", version: "4", choice: "Yes" }));
  await settle();
  const decide = h.calls.find((c) => c.path === "/api/cards/60/decide");
  assert.deepEqual([decide.method, decide.body], ["POST", { version: 4, choice: "Yes" }]);
  assert.match(h.d.regions.messages.innerHTML, /Answered: Yes/);
  assert.equal(h.d.regions.waiting.innerHTML, "");
  // The reply field sends the person's own words.
  await h.controller.submit(form({ botsForm: "reply", work: "60", version: "4" }, { reply: "  later, please " }));
  await settle();
  assert.deepEqual(h.calls.filter((c) => c.path === "/api/cards/60/decide").at(-1).body, { version: 4, reply: "later, please" });
});

test("a decision that changed meanwhile says so and reloads", async () => {
  const decision = { type: "decision", card: 60, question: "Ship it?", options: ["Yes"], version: 4, open: true };
  const h = harness({ messages: [msg(1, "bot", { blocks: [decision] })], waiting: [decision] });
  h.state.extra["POST /api/cards/60/decide"] = async () => { throw Object.assign(new Error("Version changed"), { status: 409 }); };
  await h.controller.show("b1");
  h.controller.click(el({ botsAction: "decide", work: "60", version: "4", choice: "Yes" }));
  await settle();
  assert.match(h.d.regions.notice.innerHTML, /That question changed/);
  assert.ok(h.calls.filter((c) => c.path.startsWith("/api/bots/b1/messages?limit=50")).length >= 2);
});

test("the waiting strip is one line with a count and opens to the questions", async () => {
  const blocks = [60, 61].map((card) => ({ type: "decision", card, question: `Q${card}`, options: ["Yes"], version: 1, open: true }));
  const h = harness({ waiting: blocks });
  await h.controller.show("b1");
  const strip = h.d.regions.waiting.innerHTML;
  assert.match(strip, /Waiting on you \(2\)/);
  assert.equal((strip.match(/class="wait-toggle"/g) || []).length, 1);
  assert.doesNotMatch(strip, /Q60/);
  h.controller.click(el({ botsAction: "waiting" }));
  assert.match(h.d.regions.waiting.innerHTML, /Q60[^]*Q61/);
});

test("a confirm chip posts the pending id and accept flag, then shows the settled state", async () => {
  const chip = (state) => ({ type: "confirm", pending: "p1", label: "Start work on “Fix login”", state });
  const h = harness({ messages: [msg(1, "bot", { text: "Shall I?", blocks: [chip("open")] })] });
  h.state.extra["POST /api/bots/b1/confirm"] = async () => { h.state.messages = [msg(1, "bot", { text: "Shall I?", blocks: [chip("done")] }), msg(2, "event", { text: "", blocks: [{ type: "action", label: "Started", ok: true }] })]; return { message: h.state.messages[1] }; };
  await h.controller.show("b1");
  assert.match(h.d.regions.messages.innerHTML, /data-pending="p1" data-accept="1"/);
  h.controller.click(el({ botsAction: "confirm", pending: "p1", accept: "1" }));
  await settle();
  const post = h.calls.find((c) => c.path === "/api/bots/b1/confirm");
  assert.deepEqual([post.method, post.body], ["POST", { pending: "p1", accept: true }]);
  assert.match(h.d.regions.messages.innerHTML, /Start work on “Fix login” · Done/);
  assert.match(h.d.regions.messages.innerHTML, /Started/);
  h.controller.click(el({ botsAction: "confirm", pending: "p9", accept: "0" }));
  await settle();
  assert.deepEqual(h.calls.filter((c) => c.path === "/api/bots/b1/confirm").at(-1).body, { pending: "p9", accept: false });
});

test("a work block expands to its note and links on tap", async () => {
  const work = { type: "work", card: 57, title: "Fix login", status: "working", note: "Halfway", links: [{ label: "PR", url: "https://example.com/1" }] };
  const h = harness({ messages: [msg(1, "bot", { blocks: [work] })] });
  await h.controller.show("b1");
  assert.doesNotMatch(h.d.regions.messages.innerHTML, /Halfway/);
  h.controller.click(el({ botsAction: "toggle", key: "work:57:1" }));
  assert.match(h.d.regions.messages.innerHTML, /Halfway[^]*example\.com\/1/);
  h.controller.click(el({ botsAction: "toggle", key: "work:57:1" }));
  assert.doesNotMatch(h.d.regions.messages.innerHTML, /Halfway/);
});

test("Stop replaces Send while a turn runs and posts the request id", async () => {
  let release;
  const h = harness({ send: async ({ onEvent }) => { onEvent("ack", { message: msg(10, "user", { text: "go" }) }); await new Promise((resolve) => { release = resolve; }); onEvent("error", { code: "stopped", text: "Stopped", message: msg(11, "bot", { text: "Part", state: "stopped", reply_to: 10 }) }); return { state: "error", acked: true }; } });
  h.state.extra["POST /api/bots/b1/stop"] = async () => ({ ok: true });
  await h.controller.show("b1");
  assert.match(h.d.regions.send.innerHTML, /type="submit"/);
  h.d.box.value = "go";
  const sending = h.controller.submit({ dataset: { botsForm: "send" }, elements: { text: h.d.box } });
  await settle();
  assert.match(h.d.regions.send.innerHTML, /data-bots-action="stop"[^>]*>Stop/);
  assert.doesNotMatch(h.d.regions.send.innerHTML, /disabled/);
  h.controller.click(el({ botsAction: "stop" }));
  await settle();
  assert.deepEqual(h.calls.find((c) => c.path === "/api/bots/b1/stop").body, { request_id: "req_auto_100000" });
  release();
  await sending;
  assert.match(h.d.regions.messages.innerHTML, /Part[^]*Stopped\./);
  assert.match(h.d.regions.send.innerHTML, /type="submit"/);
});

test("a failed send keeps the text and retries with the same request id; edited text gets a new one", async () => {
  const ids = [];
  let fail = true;
  const h = harness({ send: async ({ requestId, text: sent, onEvent }) => {
    ids.push(requestId);
    if (fail) throw new stream.SendError(0, "network", "Could not reach the server. Your text is kept.");
    onEvent("ack", { message: msg(10, "user", { text: sent }) });
    onEvent("done", { message: msg(11, "bot", { text: "ok", reply_to: 10 }) });
    return { state: "done", acked: true };
  } });
  await h.controller.show("b1");
  h.d.box.value = "Please file this";
  await h.controller.submit({ dataset: { botsForm: "send" }, elements: { text: h.d.box } });
  assert.equal(h.d.box.value, "Please file this", "text kept in the composer");
  assert.match(h.d.regions.notice.innerHTML, /Could not reach the server[^]*Send again/);
  assert.doesNotMatch(h.d.regions.messages.innerHTML, /echo/);
  await h.controller.submit({ dataset: { botsForm: "send" }, elements: { text: h.d.box } });
  assert.equal(ids[0], ids[1], "same text retries with the same request id");
  h.d.box.value = "Edited text";
  await h.controller.submit({ dataset: { botsForm: "send" }, elements: { text: h.d.box } });
  assert.notEqual(ids[1], ids[2]);
  fail = false;
  h.d.box.value = "Edited text";
  await h.controller.submit({ dataset: { botsForm: "send" }, elements: { text: h.d.box } });
  assert.equal(ids[2], ids[3], "a retry after the edit reuses that edit's id");
  await settle();
  assert.equal(h.d.regions.notice.innerHTML, "");
  assert.match(h.d.regions.messages.innerHTML, /ok/);
});

test("pre-stream refusals keep the text and say why", async () => {
  for (const [status, code, message] of [[409, "busy", "Another turn is running."], [429, "rate", "Slow down."], [400, "invalid", "Text is empty."]]) {
    const h = harness({ send: async () => { throw new stream.SendError(status, code, message); } });
    await h.controller.show("b1");
    h.d.box.value = "hello";
    await h.controller.submit({ dataset: { botsForm: "send" }, elements: { text: h.d.box } });
    assert.equal(h.d.box.value, "hello");
    assert.ok(h.d.regions.notice.innerHTML.includes(message), code);
  }
});

test("a connection dropped after ack polls with after= until the turn is no longer running", async () => {
  const h = harness({ send: async ({ onEvent }) => { onEvent("ack", { message: msg(10, "user", { text: "go" }) }); onEvent("delta", { text: "Part" }); return { state: "dropped", acked: true }; } });
  await h.controller.show("b1");
  h.state.running = true;
  h.state.messages = [msg(10, "user", { text: "go" })];
  h.d.box.value = "go";
  await h.controller.submit({ dataset: { botsForm: "send" }, elements: { text: h.d.box } });
  await settle();
  assert.match(h.d.regions.send.innerHTML, /data-bots-action="stop"/);
  assert.match(h.d.regions.messages.innerHTML, /class="bm bot live"[^]*Part/);
  assert.ok(h.calls.some((c) => c.path === "/api/bots/b1/messages?after=10&limit=50"));
  assert.ok([...h.timers.values()].some((t) => t.delay === 1500), "polls quickly while the turn runs");
  h.state.running = false;
  h.state.messages.push(msg(11, "bot", { text: "Final answer", reply_to: 10 }));
  await h.fire(1500);
  assert.match(h.d.regions.messages.innerHTML, /Final answer/);
  assert.doesNotMatch(h.d.regions.messages.innerHTML, /bm bot live/);
  assert.match(h.d.regions.send.innerHTML, /type="submit"/);
});

test("the setup block shows for an owner and a member inside the thread", async () => {
  const owner = harness({ messages: [msg(1, "bot", { text: "", state: "failed:needs_configuration", blocks: [{ type: "setup", code: "needs_configuration", can_configure: true }] })] });
  await owner.controller.show("b1");
  assert.match(owner.d.regions.messages.innerHTML, /Choose one in Workspace settings[^]*href="#\/settings"/);
  assert.doesNotMatch(owner.d.regions.messages.innerHTML, /bm-note/, "no duplicate line next to the setup block");
  const member = harness({ messages: [msg(1, "bot", { blocks: [{ type: "setup", code: "needs_configuration", can_configure: false }] })] });
  await member.controller.show("b1");
  assert.match(member.d.regions.messages.innerHTML, /Ask the workspace owner/);
});

test("new bot sheet posts name, job and instructions, then opens the bot", async () => {
  const h = harness();
  h.state.extra["POST /api/bots"] = async (body) => { h.state.bots = [...h.state.bots, botRow("n1", body.name)]; return { bot: botRow("n1", body.name) }; };
  await h.controller.show(null);
  h.controller.click(el({ botsAction: "new" }));
  assert.match(h.d.regions.sheet.innerHTML, /New bot[^]*How to work/);
  await h.controller.submit(form({ botsForm: "create" }, { name: " Notes ", job: "Keep notes", instructions: "" }));
  assert.deepEqual(h.calls.find((c) => c.method === "POST" && c.path === "/api/bots").body, { name: "Notes", job: "Keep notes" });
  assert.deepEqual(h.gone, ["#/bots/n1"]);
  assert.equal(h.d.regions.sheet.innerHTML, "");
});

test("memory sheet lists, adds and removes notes; rename and archive live in the same sheet", async () => {
  const h = harness();
  let notes = [{ id: 1, text: "Likes short answers", at: "x" }];
  h.state.extra["GET /api/bots/b1/memory"] = async () => ({ memories: notes });
  h.state.extra["POST /api/bots/b1/memory"] = async (body) => { const memory = { id: 2, text: body.text, at: "y" }; notes = [...notes, memory]; return { memory }; };
  h.state.extra["DELETE /api/bots/b1/memory/1"] = async () => { notes = notes.filter((n) => n.id !== 1); return { ok: true }; };
  h.state.extra["PATCH /api/bots/b1"] = async (body) => ({ bot: { ...botRow("b1", body.name || "Atlas"), ...body } });
  await h.controller.show("b1");
  h.controller.click(el({ botsAction: "sheet" }));
  await settle();
  assert.match(h.d.regions.sheet.innerHTML, /Likes short answers/);
  await h.controller.submit(form({ botsForm: "memory" }, { text: " Prefers Dutch " }));
  assert.deepEqual(h.calls.find((c) => c.method === "POST" && c.path.endsWith("/memory")).body, { text: "Prefers Dutch" });
  assert.match(h.d.regions.sheet.innerHTML, /Prefers Dutch/);
  h.controller.click(el({ botsAction: "forget", id: "1" }));
  await settle();
  assert.ok(h.calls.some((c) => c.method === "DELETE" && c.path === "/api/bots/b1/memory/1"));
  assert.doesNotMatch(h.d.regions.sheet.innerHTML, /Likes short answers/);
  await h.controller.submit(form({ botsForm: "save" }, { name: "Atlas Prime", job: "Atlas job", instructions: "" }));
  assert.deepEqual(h.calls.find((c) => c.method === "PATCH").body, { name: "Atlas Prime" });
  assert.match(h.d.regions.head.innerHTML, /Atlas Prime/);
  h.controller.click(el({ botsAction: "sheet" }));
  await settle();
  h.controller.click(el({ botsAction: "archive" }));
  assert.ok(!h.calls.some((c) => c.method === "PATCH" && c.body.archived), "first tap only asks");
  h.controller.click(el({ botsAction: "archive" }));
  await settle();
  assert.deepEqual(h.calls.filter((c) => c.method === "PATCH").at(-1).body, { archived: true });
  assert.deepEqual(h.gone, ["#/bots"]);
});

test("earlier messages load with before= and keep the newest page", async () => {
  const page = Array.from({ length: 50 }, (_, i) => msg(i + 51, "bot", { text: `m${i + 51}` }));
  const h = harness({ messages: page });
  h.state.extra["GET /api/bots/b1/messages?before=51&limit=50"] = async () => ({ messages: [msg(50, "bot", { text: "older" })], waiting: [], running: false });
  await h.controller.show("b1");
  assert.match(h.d.regions.messages.innerHTML, /data-bots-action="earlier"/);
  h.controller.click(el({ botsAction: "earlier" }));
  await settle();
  assert.match(h.d.regions.messages.innerHTML, /older[^]*m51/);
  assert.doesNotMatch(h.d.regions.messages.innerHTML, /data-bots-action="earlier"/);
});

test("a thread that is gone says so; switching threads aborts a local stream", async () => {
  const h = harness();
  h.state.extra["GET /api/bots/zz/messages?limit=50"] = async () => { throw Object.assign(new Error("No such bot"), { status: 404 }); };
  await h.controller.show("zz");
  assert.match(h.d.regions.messages.innerHTML, /no longer available/);
  let signal;
  const g = harness({ send: async (args) => { signal = args.signal; args.onEvent("ack", { message: msg(10, "user", { text: "go" }) }); await new Promise(() => {}); } });
  await g.controller.show("b1");
  g.d.box.value = "go";
  void g.controller.submit({ dataset: { botsForm: "send" }, elements: { text: g.d.box } });
  await settle();
  g.state.bots = [botRow("b1", "Atlas"), botRow("b2", "Harbor")];
  await g.controller.show("b2");
  assert.equal(signal.aborted, true);
});

test("copy never says card, board or ticket", async () => {
  const h = harness({ messages: [msg(1, "bot", { text: "Hi", blocks: [{ type: "setup", code: "needs_turn_on", can_configure: true }] })] });
  await h.controller.show("b1");
  h.controller.click(el({ botsAction: "sheet" }));
  await settle();
  for (const html of [h.d.mount.shell, h.d.regions.roster.innerHTML, h.d.regions.messages.innerHTML, h.d.regions.sheet.innerHTML, h.d.regions.composer.innerHTML]) wording(html);
  for (const value of Object.values(ui.BOTS_COPY)) wording(typeof value === "function" ? value("Atlas") : value);
});

// ---------- glue ----------

test("each roster load reports the bots to the shell, so the rail badge needs no second poll", async () => {
  const seen = [];
  const h = harness({ bots: [botRow("b1", "Atlas", { waiting: 2 }), botRow("b2", "General", { kind: "general", waiting: 3 })], onRoster: (bots) => seen.push(ui.waitingTotal(bots)) });
  await h.controller.show(null);
  assert.deepEqual(seen, [3]);
  h.state.bots = [botRow("b1", "Atlas", { waiting: 0 }), botRow("b2", "General", { kind: "general", waiting: 0 })];
  await h.fire(15000);
  assert.deepEqual(seen, [3, 0]);
});

test("the shell routes #/bots and #/bots/<id>, loads the stylesheet and lists Bots once in rail and tabs", async () => {
  const app = await read("app.js"), html = await read("index.html"), rail = await read("rail.js");
  assert.match(html, /<link rel="stylesheet" href="\/app\/bots\.css">/);
  assert.match(app, /import \{ createBots, waitingTotal \} from "\.\/bots\.js"/);
  assert.ok(app.includes("bots(?:\\/[A-Za-z0-9_-]+)?"), "route regex");
  assert.match(app, /botsScreen\(\)/);
  const { renderRail, renderPhoneTabs } = await import("../commonplace/web/static/rail.js");
  assert.equal((renderRail({ view: "bots" }).match(/href="#\/bots"/g) || []).length, 1);
  assert.match(renderRail({ view: "bots" }), /href="#\/bots"[^>]*aria-current="page"/);
  assert.equal((renderPhoneTabs({}).match(/href="#\/bots"/g) || []).length, 1);
  assert.match(rail, /Bots/);
  const { resolveRoute } = await import("../commonplace/web/static/routes.js");
  assert.equal(resolveRoute("#/", { boards: [{ slug: "a" }] }), "#/bots", "a set-up person lands on the bots");
  assert.equal(resolveRoute("#/bots/abc"), "#/bots/abc");
});

test("the stylesheet covers the three layouts, safe areas, tap targets, keyboard and reduced motion", async () => {
  const css = await read("bots.css");
  assert.match(css, /@media \(max-width: 699px\)/);
  assert.match(css, /@media \(min-width: 700px\) and \(max-width: 1023px\)/);
  assert.match(css, /min-width: 1024px|grid-template-columns: 320px/);
  assert.match(css, /env\(safe-area-inset-bottom\)/);
  assert.match(css, /--bots-vh/);
  assert.match(css, /prefers-reduced-motion: reduce/);
  assert.match(css, /min-height: 44px/);
  assert.doesNotMatch(css.replace(/rgba?\([^)]*\)/g, ""), /#[0-9a-fA-F]{3,8}\b/, "colours come from the tokens");
});
