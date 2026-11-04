import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../commonplace/web/static/console.js", import.meta.url), "utf8");
const ui = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const COPY = {
  loading: "Loading agents…",
  noAgentsOwner: "No agents connected. Create one in Settings → Agents.",
  noAgentsMember: "No agents act as you in this workspace.",
  showTools: "Show tool calls",
  stopTools: "Stop showing tool calls",
  toolsOff: "Tool calls are off for this agent. Only you can see them when on.",
  toolsPrivate: "Tool calls and output are shown only to the person this agent acts for.",
  waiting: "Waiting for activity. Tool calls appear as they happen.",
  noBoard: "No board or memory actions yet.",
  outputHeading: "Output",
  activityHeading: "Activity",
  noOutput: "No replies or comments yet.",
  unavailable: "Console unavailable. Retrying…",
};
const agent = { id: "conn_a", name: "Alpha", client: "test", parent_id: null, own: true, recording: false, can_toggle: true, status: "working", last_seen: null };
const output = (text, extra = {}) => ({ id: "r1", at: "2026-11-04T09:00:00Z", source: "output", kind: "reply", board: "work", card: null, text, truncated: false, redacted: [], ...extra });
const state = (extra = {}) => ({ role: "owner", agents: [agent], selected: agent.id, items: [], output: [], tools: "off", error: "", loading: false, ...extra });
const contains = (html, text) => assert.ok(html.includes(e(text)), `Missing rendered text: ${text}`);

if (!process.env.C184_RENDER_CASE || process.env.C184_RENDER_CASE === "view") {
  test("view states, authority, escaping, markers and frozen copy", () => {
    assert.deepEqual(ui.CONSOLE_COPY, COPY);
    const view = (extra) => ui.consoleView(state(extra), e);
    contains(view({ loading: true, agents: [] }), COPY.loading);
    contains(view({ agents: [] }), COPY.noAgentsOwner);
    contains(view({ role: "member", agents: [] }), COPY.noAgentsMember);
    contains(view({ error: COPY.unavailable }), COPY.unavailable);
    contains(view({}), COPY.toolsOff);
    contains(view({ tools: "on" }), COPY.waiting);
    contains(view({}), COPY.noBoard);
    contains(view({}), COPY.outputHeading);
    contains(view({}), COPY.activityHeading);
    contains(view({}), COPY.noOutput);
    contains(view({ tools: "private", agents: [{ ...agent, own: false, can_toggle: false, recording: null }] }), COPY.toolsPrivate);
    const controls = ui.agentListHTML([agent], agent.id, e);
    contains(controls, COPY.showTools);
    assert.match(controls, /data-action="console-record"/);
    assert.match(controls, /data-record="1"/);
    const on = ui.agentListHTML([{ ...agent, recording: true }], agent.id, e);
    contains(on, COPY.stopTools);
    assert.match(on, /data-record="0"/);
    assert.doesNotMatch(ui.agentListHTML([{ ...agent, own: false, can_toggle: false }], agent.id, e), /data-action="console-record"/);
    assert.doesNotMatch(ui.agentListHTML([{ ...agent, can_toggle: false }], agent.id, e), /data-action="console-record"/);
    const attack = '<img src=x onerror="boom()"> & <script>boom()</script>';
    const agentHTML = ui.agentListHTML([{ ...agent, name: attack, client: attack }], agent.id, e);
    contains(agentHTML, attack);
    assert.doesNotMatch(agentHTML, /<img|<script/);
    const tool = ui.streamItemsHTML([{ id: "t1", at: "now", source: "tool", name: "task_list", outcome: "ok", ms: 12 }], e);
    assert.match(tool, /▸\s*task_list\s*·\s*ok\s*·\s*12 ms/);
    const board = ui.streamItemsHTML([{ id: "e1", at: "now", source: "board", kind: attack, text: attack, truncated: false, redacted: [] }], e);
    contains(board, attack);
    assert.doesNotMatch(board, /<img|<script/);
    const out = ui.outputItemsHTML([output(attack, { board: attack, kind: attack })], e);
    contains(out, attack);
    assert.doesNotMatch(out, /<img|<script/);
    const comment = ui.outputItemsHTML([output("Exact words", { kind: "comment", card: 12 })], e);
    assert.match(comment, /comment\s*·\s*#12/);
    assert.match(ui.outputItemsHTML([output("Exact words")], e), /reply\s*·\s*work/);
    for (const [render, rows] of [
      [ui.streamItemsHTML, [{ id: "e2", at: "now", source: "board", kind: "card.created", text: "[redacted:cmmn_token]", redacted: ["cmmn_token"] }]],
      [ui.outputItemsHTML, [output("[redacted:cmmn_token]")]],
    ]) {
      const html = render(rows, e);
      assert.match(html, /<mark class="redacted">\[redacted:cmmn_token\]<\/mark>/);
      assert.ok(!html.includes(" …"));
      assert.ok(render(rows.map((r) => ({ ...r, truncated: true })), e).includes(" …"));
    }
    const marked = ui.markRedactions(e("[redacted:<img src=x>][omitted:too_long]"));
    assert.doesNotMatch(marked, /<img/);
    assert.match(marked, /\[redacted:&lt;img src=x&gt;\]/);
    assert.match(marked, /<mark class="redacted">\[omitted:too_long\]<\/mark>/);
  });
}

// A minimal mount and controlled timers: all requests are fake and no browser runs.
function fixture() {
  const timers = new Map(), calls = [];
  let sequence = 0, hidden = false, fail = false, currentOutput = [output("FIRST-OUTPUT")];
  const mount = { innerHTML: "", querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, scrollTop: 0, scrollHeight: 0, clientHeight: 0 };
  const api = async (path, options) => {
    calls.push({ path, options });
    if (path === "/api/console") return { role: "owner", agents: [agent] };
    assert.ok(path.startsWith("/api/console/stream?"), path);
    assert.ok(path.includes("agent=conn_a"), path);
    if (fail) throw new Error("synthetic 500");
    return { items: [], cursor: "next-cursor", more: false, tools: "off", output: currentOutput };
  };
  const controller = ui.createConsole({
    mount: () => mount, api, e,
    setTimer(callback, delay) { const id = ++sequence; timers.set(id, { callback, delay }); return id; },
    clearTimer(id) { timers.delete(id); }, hidden: () => hidden,
  });
  const settle = async () => { for (let n = 0; n < 30; n++) await Promise.resolve(); };
  return {
    controller, mount, timers, calls, settle,
    hide(value) { hidden = value; }, fail(value) { fail = value; }, output(value) { currentOutput = value; },
    async tick() {
      assert.ok(timers.size, "The console must schedule another tick");
      const [id, timer] = timers.entries().next().value;
      timers.delete(id); await timer.callback(); await settle();
    },
  };
}

if (!process.env.C184_RENDER_CASE || process.env.C184_RENDER_CASE === "controller") {
  test("controller requests, private error line, hidden, stop, output replacement", async () => {
    const f = fixture();
    await f.controller.show(); await f.settle();
    assert.equal(f.calls[0].path, "/api/console");
    assert.ok(f.calls.some((c) => c.path.startsWith("/api/console/stream?")));
    contains(f.mount.innerHTML, "FIRST-OUTPUT");
    f.output(null); await f.tick();
    contains(f.mount.innerHTML, "FIRST-OUTPUT");
    f.output([output("REPLACEMENT-OUTPUT")]); await f.tick();
    contains(f.mount.innerHTML, "REPLACEMENT-OUTPUT");
    assert.ok(!f.mount.innerHTML.includes("FIRST-OUTPUT"));
    f.fail(true); await f.tick();
    contains(f.mount.innerHTML, COPY.unavailable);
    assert.ok([...f.timers.values()].some((t) => t.delay > 2000 && t.delay <= 30000));
    f.fail(false); await f.tick();
    assert.ok(!f.mount.innerHTML.includes(COPY.unavailable));
    f.hide(true);
    const before = f.calls.length;
    await f.tick();
    assert.equal(f.calls.length, before);
    f.controller.stop();
    assert.equal(f.timers.size, 0);
    const hidden = fixture(); hidden.hide(true);
    await hidden.controller.show(); await hidden.settle();
    assert.equal(hidden.calls.length, 0);
    hidden.controller.stop();
    assert.equal(hidden.timers.size, 0);
  });
}
