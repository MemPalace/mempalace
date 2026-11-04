import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../commonplace/web/static/console.js", import.meta.url), "utf8");
const ui = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const agent = (id = "a", extra = {}) => ({ id, name: `Agent ${id}`, client: "test", parent_id: null, own: true, recording: false, can_toggle: true, status: "working", last_seen: "2026-11-04T09:00:00Z", ...extra });
const board = (id, text = `Board ${id}`, extra = {}) => ({ id, at: "2026-11-04T09:00:00Z", source: "board", kind: "card.claimed", text, truncated: false, redacted: [], ...extra });
const output = (id, text = `Output ${id}`, extra = {}) => ({ id, at: "2026-11-04T09:00:00Z", source: "output", kind: "reply", board: "work", card: null, text, truncated: false, redacted: [], ...extra });
const page = (extra = {}) => ({ items: [], cursor: "cursor", more: false, tools: "off", output: [], ...extra });
const element = (id, record) => ({ dataset: { id, record } });
const settle = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

// Each innerHTML assignment creates new nodes, as a browser does. The stream
// clamps scrollTop too: treating an assignment past the end as real would hide
// broken bottom detection after the following render.
function mountFixture() {
  let html = "", nodes = new Map(), buttons = [];
  return {
    get innerHTML() { return html; },
    set innerHTML(value) {
      html = value;
      nodes = new Map();
      buttons = [...html.matchAll(/data-action="console-record" data-id="([^"]*)"/g)].map((m) => ({ dataset: { id: m[1] }, disabled: false }));
      if (!html.includes('class="console-stream"')) return;
      let top = 0;
      const stream = {
        scrollHeight: 400 + 100 * (html.match(/<div class="console-item"/g) || []).length,
        clientHeight: 200,
        get scrollTop() { return top; },
        set scrollTop(value) { top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); },
      };
      nodes.set(".console-stream", stream);
      nodes.set(".console-output-items", { scrollTop: 0 });
      nodes.set(".console-agents", { scrollLeft: 0 });
      nodes.set('[data-action="console-bottom"]', { hidden: true });
    },
    querySelector(selector) { return nodes.get(selector) || null; },
    querySelectorAll(selector) { return selector === '[data-action="console-record"]' ? buttons : []; },
  };
}

function fixture(t, options = {}) {
  const mount = mountFixture(), timers = new Map(), calls = [];
  let sequence = 0, hidden = false;
  const f = {
    mount, timers, calls, agents: [agent(), agent("b")],
    list: () => ({ role: "owner", agents: f.agents }),
    stream: () => page(),
    post: (path, options) => {
      const id = decodeURIComponent(path.split("/").at(-1));
      f.agents = f.agents.map((a) => a.id === id ? { ...a, recording: options.body.record } : a);
      return f.agents.find((a) => a.id === id);
    },
    hide(value) { hidden = value; },
    async tick() {
      assert.equal(timers.size, 1, "one private timer must be scheduled");
      const [id, timer] = timers.entries().next().value;
      timers.delete(id);
      await timer.callback();
      await settle();
    },
    delay() { assert.equal(timers.size, 1); return [...timers.values()][0].delay; },
    streams() { return calls.filter((c) => c.path.startsWith("/api/console/stream?")); },
    lists() { return calls.filter((c) => c.path === "/api/console"); },
    posts() { return calls.filter((c) => c.options?.method === "POST"); },
  };
  Object.assign(f, options);
  f.controller = ui.createConsole({
    mount: () => mount, e, hidden: () => hidden,
    async api(path, options) {
      calls.push({ path, options });
      if (options?.method === "POST") return f.post(path, options);
      if (path === "/api/console") return f.list();
      assert.ok(path.startsWith("/api/console/stream?"));
      return f.stream(new URLSearchParams(path.split("?")[1]));
    },
    setTimer(callback, delay) { const id = ++sequence; timers.set(id, { callback, delay }); return id; },
    clearTimer(id) { timers.delete(id); },
  });
  t.after(() => f.controller.stop());
  return f;
}

test("view copy, empty states, output authority and controls", () => {
  assert.ok(Object.isFrozen(ui.CONSOLE_COPY));
  const view = (extra = {}) => ui.consoleView({ role: "owner", agents: [agent()], selected: "a", items: [], output: [], tools: "off", error: "", loading: false, ...extra }, e);
  for (const [state, keys] of [
    [{ agents: [], loading: true }, ["loading"]],
    [{ agents: [] }, ["noAgentsOwner"]],
    [{ agents: [], role: "member" }, ["noAgentsMember"]],
    [{}, ["showTools", "toolsOff", "outputHeading", "activityHeading", "noOutput", "noBoard"]],
    [{ tools: "on" }, ["waiting"]],
    [{ error: "Internal failure with private content" }, ["unavailable"]],
  ]) for (const key of keys) assert.ok(view(state).includes(e(ui.CONSOLE_COPY[key])), key);
  assert.ok(!view({ error: "NEVER-SHOW-THIS" }).includes("NEVER-SHOW-THIS"));
  assert.match(ui.agentListHTML([agent("a", { recording: true })], "a", e), /data-record="0"/);
  assert.match(ui.agentListHTML([agent()], "a", e), /data-record="1"/);
  for (const extra of [{ own: false }, { can_toggle: false }]) {
    assert.doesNotMatch(ui.agentListHTML([agent("a", extra)], "a", e), /console-record/);
  }
  const privateHTML = view({ agents: [agent("a", { own: false, can_toggle: false })], tools: "private", output: [output("r1", "PRIVATE-OUTPUT")], items: [board("e1"), { id: "t1", source: "tool", name: "PRIVATE-TOOL" }] });
  assert.ok(privateHTML.includes(e(ui.CONSOLE_COPY.toolsPrivate)));
  assert.doesNotMatch(privateHTML, /PRIVATE-OUTPUT|PRIVATE-TOOL|console-record/);
  assert.match(privateHTML, /Board e1/);
});

test("all display paths escape first and mark only fixed redaction shapes", () => {
  const attack = '<img src=x onerror="attack()"> & <script>attack()</script>';
  for (const html of [
    ui.agentListHTML([agent(attack, { name: attack, client: attack, status: attack })], attack, e),
    ui.streamItemsHTML([board(attack, attack, { kind: attack, at: attack })], e),
    ui.streamItemsHTML([{ id: attack, source: "tool", name: attack, outcome: attack, ms: attack, at: attack }], e),
    ui.outputItemsHTML([output(attack, attack, { kind: attack, board: attack, at: attack })], e),
  ]) {
    assert.ok(html.includes(e(attack)));
    assert.doesNotMatch(html, /<img|<script|onerror="attack/);
  }
  const markers = "[redacted:cmmn_token] [omitted:too_long] [redacted:<img>] [redacted:BAD]";
  const marked = ui.markRedactions(e(markers));
  assert.equal((marked.match(/<mark /g) || []).length, 2);
  assert.match(marked, /\[redacted:&lt;img&gt;\]/);
  for (const html of [ui.agentListHTML([agent("a", { name: markers })], "a", e), ui.streamItemsHTML([board("e1", markers)], e), ui.outputItemsHTML([output("r1", markers)], e)]) {
    assert.match(html, /<mark class="redacted">\[redacted:cmmn_token\]<\/mark>/);
    assert.match(html, /<mark class="redacted">\[omitted:too_long\]<\/mark>/);
  }
});

test("activity order, tool lines, output headings and truncation", () => {
  const stream = ui.streamItemsHTML([board("e1", "#12 Title"), { id: "t2", source: "tool", name: "task_list", outcome: "ok", ms: 12 }, board("e3", "Last", { truncated: true })], e);
  assert.match(stream, /card.claimed · #12 Title/);
  assert.match(stream, /▸ task_list · ok · 12 ms/);
  assert.ok(stream.indexOf('data-id="e1"') < stream.indexOf('data-id="t2"'));
  assert.ok(stream.indexOf('data-id="t2"') < stream.indexOf('data-id="e3"'));
  assert.match(stream, /Last …/);
  const html = ui.outputItemsHTML([output("r2", "Newest\nexact words"), output("c1", "Older", { kind: "comment", card: 12, truncated: true })], e);
  assert.match(html, /reply · work/);
  assert.match(html, /comment · #12/);
  assert.ok(html.indexOf("Newest\nexact words") < html.indexOf("Older …"));
});

test("one timer, idempotent show and agent list refresh every fifth tick", async (t) => {
  const f = fixture(t);
  await f.controller.show();
  assert.equal(f.calls[0].path, "/api/console");
  assert.equal(f.streams().length, 1);
  assert.equal(f.delay(), 2000);
  await f.controller.show();
  assert.equal(f.calls.length, 2);
  for (let i = 0; i < 4; i++) await f.tick();
  assert.equal(f.lists().length, 1);
  await f.tick();
  assert.equal(f.lists().length, 2);
  assert.equal(f.streams().length, 6);
  assert.equal(f.controller.click("other-action", element("a")), false);
});

test("more pages stop at three, resume the cursor and replace-or-keep output", async (t) => {
  let count = 0;
  const f = fixture(t, { stream: () => {
    count++;
    return page({ items: [board(`e${count}`)], cursor: `next+${count}&`, more: count < 5, output: count === 1 ? [output("r1", "FIRST")] : null });
  } });
  await f.controller.show();
  assert.equal(f.streams().length, 3);
  assert.ok(f.mount.innerHTML.includes("FIRST"));
  assert.equal(new URLSearchParams(f.streams()[1].path.split("?")[1]).get("after"), "next+1&");
  await f.tick();
  assert.equal(f.streams().length, 5);
  assert.equal(new URLSearchParams(f.streams()[3].path.split("?")[1]).get("after"), "next+3&");
  assert.ok(f.mount.innerHTML.includes("FIRST"));
  f.stream = () => page({ output: [output("r2", "REPLACEMENT")] });
  await f.tick();
  assert.match(f.mount.innerHTML, /REPLACEMENT/);
  assert.doesNotMatch(f.mount.innerHTML, /FIRST/);
  f.stream = () => page({ output: [] });
  await f.tick();
  assert.doesNotMatch(f.mount.innerHTML, /REPLACEMENT/);
  assert.ok(f.mount.innerHTML.includes(e(ui.CONSOLE_COPY.noOutput)));
});

test("switching agents resets panels and cursor and discards old in-flight pages", async (t) => {
  const old = deferred(), next = deferred();
  const f = fixture(t, { stream: () => page({ items: [board("e1", "OLD-ACTIVITY")], output: [output("r1", "OLD-OUTPUT")] }) });
  await f.controller.show();
  f.stream = (query) => query.get("agent") === "a" ? old.promise : next.promise;
  const running = f.tick();
  await settle();
  assert.equal(f.controller.click("console-select", element("b")), true);
  assert.doesNotMatch(f.mount.innerHTML, /OLD-ACTIVITY|OLD-OUTPUT/);
  await settle();
  const query = new URLSearchParams(f.streams().at(-1).path.split("?")[1]);
  assert.equal(query.get("agent"), "b");
  assert.equal(query.has("after"), false);
  next.resolve(page({ items: [board("e2", "B-ACTIVITY")], output: [output("r2", "B-OUTPUT")] }));
  await settle();
  old.resolve(page({ items: [board("e3", "STALE-ACTIVITY")], output: [output("r3", "STALE-OUTPUT")] }));
  await running;
  assert.match(f.mount.innerHTML, /B-ACTIVITY/);
  assert.match(f.mount.innerHTML, /B-OUTPUT/);
  assert.doesNotMatch(f.mount.innerHTML, /STALE|OLD-ACTIVITY|OLD-OUTPUT/);
  assert.equal(f.delay(), 2000);
});

test("agent removal clears old data and replacement uses a tail request", async (t) => {
  const f = fixture(t, { stream: () => page({ output: [output("r1", "REMOVED-OUTPUT")] }) });
  await f.controller.show();
  f.agents = [agent("b")];
  f.stream = () => page();
  for (let i = 0; i < 5; i++) await f.tick();
  assert.doesNotMatch(f.mount.innerHTML, /REMOVED-OUTPUT/);
  assert.equal(new URLSearchParams(f.streams().at(-1).path.split("?")[1]).has("after"), false);
  f.agents = [];
  for (let i = 0; i < 5; i++) await f.tick();
  assert.ok(f.mount.innerHTML.includes(e(ui.CONSOLE_COPY.noAgentsOwner)));
  const count = f.streams().length;
  await f.tick();
  assert.equal(f.streams().length, count);
});

test("hidden means no requests, including first show and between more pages", async (t) => {
  const f = fixture(t);
  f.hide(true);
  await f.controller.show();
  assert.equal(f.calls.length, 0);
  await f.tick();
  assert.equal(f.calls.length, 0);
  assert.equal(f.delay(), 2000);
  f.hide(false);
  f.stream = () => { f.hide(true); return page({ more: true }); };
  await f.tick();
  assert.equal(f.streams().length, 1);
  const count = f.calls.length;
  await f.tick();
  assert.equal(f.calls.length, count);
  assert.equal(f.controller.click("console-record", element("a", "1")), true);
  await settle();
  assert.equal(f.calls.length, count);
});

test("errors stay inside the view, backoff caps at 30s, and success recovers", async (t) => {
  const f = fixture(t, { stream: () => { throw new Error("PRIVATE-ERROR"); } });
  await f.controller.show();
  assert.equal(f.delay(), 4000);
  assert.ok(f.mount.innerHTML.includes(e(ui.CONSOLE_COPY.unavailable)));
  assert.doesNotMatch(f.mount.innerHTML, /PRIVATE-ERROR/);
  for (const delay of [8000, 16000, 30000, 30000]) { await f.tick(); assert.equal(f.delay(), delay); }
  f.stream = () => page();
  await f.tick();
  assert.equal(f.delay(), 2000);
  assert.ok(!f.mount.innerHTML.includes(e(ui.CONSOLE_COPY.unavailable)));
  f.controller.stop();
  assert.equal(f.timers.size, 0);
});

test("stop fences in-flight list and stream responses, including a new visit", async (t) => {
  const list = deferred();
  const f = fixture(t, { list: () => list.promise });
  const showing = f.controller.show();
  await settle();
  f.controller.stop();
  const stoppedHTML = f.mount.innerHTML;
  list.resolve({ role: "owner", agents: [agent("stale")] });
  await showing;
  assert.equal(f.mount.innerHTML, stoppedHTML);
  assert.equal(f.timers.size, 0);
  assert.equal(f.streams().length, 0);
  f.list = () => ({ role: "member", agents: [agent("new")] });
  const stream = deferred();
  f.stream = () => stream.promise;
  const second = f.controller.show();
  await settle();
  f.controller.stop();
  f.list = () => ({ role: "member", agents: [] });
  await f.controller.show();
  stream.resolve(page({ output: [output("r1", "STALE-VISIT")] }));
  await second;
  assert.doesNotMatch(f.mount.innerHTML, /STALE-VISIT/);
  assert.ok(f.mount.innerHTML.includes(e(ui.CONSOLE_COPY.noAgentsMember)));
  assert.equal(f.delay(), 2000);
});

test("toggle posts only explicit own-agent booleans, blocks duplicates, refreshes list", async (t) => {
  const posting = deferred();
  const f = fixture(t, { agents: [agent(), agent("private", { own: false }), agent("locked", { can_toggle: false })] });
  await f.controller.show();
  const post = f.post;
  f.post = async (path, options) => { await posting.promise; return post(path, options); };
  for (const [id, record] of [["private", "1"], ["locked", "1"], ["missing", "1"], ["a", "true"]]) f.controller.click("console-record", element(id, record));
  assert.equal(f.posts().length, 0);
  assert.equal(f.controller.click("console-record", element("a", "1")), true);
  f.controller.click("console-record", element("a", "1"));
  assert.equal(f.posts().length, 1);
  assert.equal(f.mount.querySelectorAll('[data-action="console-record"]')[0].disabled, true);
  assert.deepEqual(f.posts()[0], { path: "/api/console/agents/a", options: { method: "POST", body: { record: true } } });
  posting.resolve();
  await settle();
  assert.equal(f.lists().length, 2);
  assert.ok(f.mount.innerHTML.includes(e(ui.CONSOLE_COPY.stopTools)));
  assert.equal(f.mount.querySelectorAll('[data-action="console-record"]')[0].disabled, false);
  f.controller.click("console-record", element("a", "0"));
  await settle();
  assert.equal(f.posts().at(-1).options.body.record, false);
  assert.equal(f.lists().length, 3);
  assert.equal(f.delay(), 2000);
});

test("toggle errors recover and stopped toggles cannot resurrect the view", async (t) => {
  const f = fixture(t, { post: () => { throw new Error("PRIVATE-POST-ERROR"); } });
  await f.controller.show();
  f.controller.click("console-record", element("a", "1"));
  await settle();
  assert.ok(f.mount.innerHTML.includes(e(ui.CONSOLE_COPY.unavailable)));
  assert.doesNotMatch(f.mount.innerHTML, /PRIVATE-POST-ERROR/);
  assert.equal(f.delay(), 4000);
  await f.tick();
  assert.ok(!f.mount.innerHTML.includes(e(ui.CONSOLE_COPY.unavailable)));
  const posting = deferred();
  f.post = () => posting.promise;
  f.controller.click("console-record", element("a", "1"));
  await settle();
  f.controller.stop();
  const count = f.calls.length, html = f.mount.innerHTML;
  posting.resolve(agent("a", { recording: true }));
  await settle();
  assert.equal(f.calls.length, count);
  assert.equal(f.timers.size, 0);
  assert.equal(f.mount.innerHTML, html);
});

test("activity sticks only at the bottom and the quiet button resumes following", async (t) => {
  let count = 0;
  const f = fixture(t, { stream: () => page({ items: [board(`e${++count}`)] }) });
  await f.controller.show();
  const bottom = () => { const node = f.mount.querySelector(".console-stream"); return node.scrollHeight - node.clientHeight; };
  const stream = () => f.mount.querySelector(".console-stream");
  const button = () => f.mount.querySelector('[data-action="console-bottom"]');
  assert.equal(stream().scrollTop, bottom());
  await f.tick();
  assert.equal(stream().scrollTop, bottom());
  stream().scrollTop = 20;
  f.mount.querySelector(".console-output-items").scrollTop = 17;
  f.mount.querySelector(".console-agents").scrollLeft = 220;
  await f.tick();
  assert.equal(stream().scrollTop, 20);
  assert.equal(button().hidden, false);
  assert.equal(f.mount.querySelector(".console-output-items").scrollTop, 17);
  assert.equal(f.mount.querySelector(".console-agents").scrollLeft, 220);
  f.controller.click("console-bottom", element());
  assert.equal(stream().scrollTop, bottom());
  assert.equal(button().hidden, true);
  await f.tick();
  assert.equal(stream().scrollTop, bottom());
  stream().scrollTop = 0;
  await f.tick();
  assert.equal(button().hidden, false);
  stream().scrollTop = bottom();
  stream().onscroll();
  assert.equal(button().hidden, true);
  f.controller.click("console-select", element("b"));
  await settle();
  assert.equal(stream().scrollTop, bottom());
  assert.equal(button().hidden, true);
});

test("phone stylesheet keeps the chip strip and panels within one column", async () => {
  const css = await readFile(new URL("../commonplace/web/static/console.css", import.meta.url), "utf8");
  assert.match(css, /grid-template-columns: 240px minmax\(0, 1fr\)/);
  const phone = css.split("@media (max-width: 640px)")[1];
  assert.ok(phone);
  assert.match(phone, /grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(phone, /overflow-x: auto/);
  assert.match(phone, /\.console-agents ul \{ display: flex/);
  assert.match(phone, /min-height: 44px/);
  assert.match(css, /white-space: pre-wrap/);
});
