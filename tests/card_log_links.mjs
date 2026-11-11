import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const dir = new URL("../commonplace/web/static/", import.meta.url);
const links = await import(new URL("cardlinks.js", dir));
const details = await import(new URL("details.js", dir));
const rail = await import(`data:text/javascript;base64,${Buffer.from((await readFile(new URL("rail.js", dir), "utf8")).replace("'./api.js'", JSON.stringify(new URL("api.js", dir).href))).toString("base64")}`);
const consoleSource = await readFile(new URL("console.js", dir), "utf8");
const consoleUI = await import(`data:text/javascript;base64,${Buffer.from(consoleSource).toString("base64")}`);

const agent = (id, name, extra = {}) => ({ id, name, client: "test", parent_id: null, own: true, recording: false, can_toggle: true, status: "working", last_seen: "2026-11-04T09:00:00Z", ...extra });
const card = { number: 21, board: "atlas", version: 1, title: "Queue edits", body: "", status: "doing", assignee: "conn:otto", events: [] };
const settle = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
const router = ({ agents = [agent("c1", "otto")], handoffs = [{ id: "hof_1" }], calls = [] } = {}) => async (path) => {
  calls.push(path);
  if (path === "/api/console") { if (agents instanceof Error) throw agents; return { role: "member", agents }; }
  if (path.startsWith("/api/handoffs?task=")) { if (handoffs instanceof Error) throw handoffs; return handoffs; }
  throw new Error(`unexpected ${path}`);
};

test("an agent link needs exactly one visible agent with the assignee's name", () => {
  assert.equal(links.agentForAssignee("conn:otto", [agent("c1", "otto")]), "c1");
  assert.equal(links.agentForAssignee("conn:otto", []), null);
  assert.equal(links.agentForAssignee("conn:otto", [agent("c1", "other")]), null);
  assert.equal(links.agentForAssignee("conn:otto", [agent("c1", "otto"), agent("c2", "otto")]), null);
  assert.equal(links.agentForAssignee("user:maya", [agent("c1", "maya")]), null);
  assert.equal(links.agentForAssignee(null, [agent("c1", "otto")]), null);
  assert.equal(links.agentForAssignee("conn:otto", undefined), null);
});

test("a reader gets both links, pointing at the console and the stored transcript", async () => {
  const calls = [];
  const found = await links.loadCardLinks(card, router({ calls }));
  assert.deepEqual(found, { agent: "c1", transcript: true, card: 21 });
  assert.deepEqual(calls.sort(), ["/api/console", "/api/handoffs?task=%2321"]);
  const html = links.cardLinksHTML(found);
  assert.match(html, /href="#\/console\?agent=c1"[^>]*>Agent activity</);
  assert.match(html, /href="\/app\/cards\/%2321" target="_blank" rel="noopener">Transcript and handoffs</);
});

test("a viewer who cannot read the agent or any transcript gets no link, id or title", async () => {
  // The console lists only the viewer's readable agents and the handoff list only readable threads.
  const hidden = { agents: [agent("c9", "someone-else")], handoffs: [] };
  const found = await links.loadCardLinks(card, router(hidden));
  assert.deepEqual(found, { agent: null, transcript: false, card: 21 });
  assert.equal(links.cardLinksHTML(found), "");
  // A refusal or an outage reads the same: nothing is offered.
  for (const failing of [new Error("404"), new Error("401")]) {
    const result = await links.loadCardLinks(card, router({ agents: failing, handoffs: failing }));
    assert.equal(links.cardLinksHTML(result), "");
  }
  // Only the transcript, only the agent.
  assert.match(links.cardLinksHTML({ agent: null, transcript: true, card: 21 }), /Transcript and handoffs/);
  assert.doesNotMatch(links.cardLinksHTML({ agent: null, transcript: true, card: 21 }), /Agent activity|#\/console/);
  assert.doesNotMatch(links.cardLinksHTML({ agent: "c1", transcript: false, card: 21 }), /Transcript|\/app\//);
});

test("a card without an agent assignee never asks the console", async () => {
  const calls = [];
  const found = await links.loadCardLinks({ ...card, assignee: "user:maya" }, router({ calls }));
  assert.equal(found.agent, null);
  assert.deepEqual(calls, ["/api/handoffs?task=%2321"]);
});

test("link markup escapes the agent id", () => {
  const html = links.cardLinksHTML({ agent: '"><img src=x onerror=attack()>', transcript: false, card: 21 });
  assert.doesNotMatch(html, /<img/);
  assert.ok(html.includes("%22%3E%3Cimg"));
});

function detailsMount() {
  const state = { html: "", inserted: [], removed: 0 };
  const fields = { insertAdjacentHTML: (where, html) => { assert.equal(where, "afterend"); state.inserted.push(html); } };
  const mount = {
    get innerHTML() { return state.html; },
    set innerHTML(value) { state.html = value; state.inserted = []; },
    addEventListener() {}, removeEventListener() {},
    querySelector: (selector) => selector === ".fields" && state.html.includes('class="fields"') ? fields
      : selector === "[data-card-links]" && state.inserted.length ? { remove: () => { state.removed++; state.inserted = []; } } : null,
  };
  return { mount, state };
}

test("the card panel adds the links beside the fields once they are known", async () => {
  const { mount, state } = detailsMount();
  const component = details.createDetails({ mount, api: router() });
  await component.show(card, { me: "user:maya" });
  await settle();
  assert.equal(state.inserted.length, 1);
  assert.match(state.inserted[0], /Agent activity/);
  // A refresh of the same card keeps the links in the very first draw, without a gap.
  await component.show({ ...card, title: "Refreshed" }, { me: "user:maya" });
  assert.equal(state.inserted.length, 1);
  await settle();
  assert.equal(state.inserted.length, 1);
  component.stop();
});

test("links of one card never appear on the next card", async () => {
  const { mount, state } = detailsMount();
  let release;
  const slow = (path) => path === "/api/console" ? new Promise((done) => { release = () => done({ agents: [agent("c1", "otto")] }); }) : Promise.resolve([{ id: "hof_1" }]);
  const component = details.createDetails({ mount, api: slow });
  await component.show(card, { me: "user:maya" });
  await component.show({ ...card, number: 22, assignee: null }, { me: "user:maya" });
  release();
  await settle();
  assert.equal(state.inserted.some((html) => html.includes("Agent activity")), false);
  component.stop();
});

test("a failed lookup leaves the panel without links and without an error", async () => {
  const { mount, state } = detailsMount();
  const component = details.createDetails({ mount, api: async () => { throw new Error("offline"); } });
  await component.show(card, { me: "user:maya" });
  await settle();
  assert.deepEqual(state.inserted, []);
  component.stop();
});

function consoleFixture(agents, options = {}) {
  let html = "";
  const calls = [], selected = [], timers = new Map();
  const mount = { get innerHTML() { return html; }, set innerHTML(value) { html = value; }, querySelector: () => null, querySelectorAll: () => [] };
  const controller = consoleUI.createConsole({
    mount: () => mount, e: (value) => String(value ?? ""), hidden: () => false,
    onSelect: (id) => selected.push(id),
    setTimer: (callback, delay) => { timers.set(timers.size + 1, { callback, delay }); return timers.size; }, clearTimer() {},
    async api(path) {
      calls.push(path);
      if (path === "/api/console") return { role: "member", agents };
      return { items: [], cursor: "c", more: false, tools: "off", output: [] };
    },
    ...options,
  });
  return { controller, calls, selected, mount, streams: () => calls.filter((path) => path.startsWith("/api/console/stream?")).map((path) => new URLSearchParams(path.split("?")[1]).get("agent")) };
}

test("the console opens on the requested agent and falls back to the first for one it does not list", async () => {
  const listed = consoleFixture([agent("a", "alpha"), agent("b", "beta")]);
  await listed.controller.show("b");
  assert.deepEqual(listed.streams(), ["b"]);
  assert.match(listed.mount.innerHTML, /aria-pressed="true"/);
  listed.controller.stop();
  // An agent the viewer may not read is not in the list: nothing about it is requested or shown.
  const unlisted = consoleFixture([agent("a", "alpha")]);
  await unlisted.controller.show("secret-agent");
  assert.deepEqual(unlisted.streams(), ["a"]);
  assert.doesNotMatch(unlisted.mount.innerHTML, /secret-agent/);
  assert.equal(unlisted.calls.some((path) => path.includes("secret-agent")), false);
  unlisted.controller.stop();
});

test("picking another agent reports it for the address bar; the automatic choice does not", async () => {
  const f = consoleFixture([agent("a", "alpha"), agent("b", "beta")]);
  await f.controller.show("a");
  assert.deepEqual(f.selected, []);
  assert.equal(f.controller.click("console-select", { dataset: { id: "b" } }), true);
  assert.deepEqual(f.selected, ["b"]);
  f.controller.click("console-select", { dataset: { id: "ghost" } });
  assert.deepEqual(f.selected, ["b"]);
  await settle();
  // Following a link to a third agent while the console is open moves to it.
  await f.controller.show("a");
  await settle();
  assert.equal(f.streams().at(-1), "a");
  assert.deepEqual(f.selected, ["b"]);
  f.controller.stop();
});

test("the rail lists Console, the account menu no longer does", () => {
  const html = rail.renderRail({ user: "Maya", board: { slug: "atlas" }, boards: [{ slug: "atlas" }], view: "console" });
  const nav = html.match(/<a class="nav" href="#\/console"[^>]*>/);
  assert.ok(nav, "rail link missing");
  assert.match(nav[0], /aria-current="page"/);
  assert.equal((html.match(/href="#\/console"/g) || []).length, 1);
  assert.doesNotMatch(html.match(/<div class="user-links">[\s\S]*?<\/div>/)[0], /Console/);
  assert.doesNotMatch(rail.renderRail({ view: "memory" }), /href="#\/console"[^>]*aria-current/);
});
