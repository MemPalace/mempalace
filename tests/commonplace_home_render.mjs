// Needs me in the shell: the sliced needsView mounts the Needs me module; Home's tiles and table are gone.
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";

const file = (name) => new URL(`../commonplace/web/static/${name}`, import.meta.url);
const read = (name) => readFile(file(name), "utf8");
const which = process.argv[2];
const app = await read("app.js");
const slice = (start, end) => {
  const at = app.indexOf(start), stop = app.indexOf(end, at);
  assert.ok(at >= 0 && stop > at, `Missing slice: ${start} → ${end}`);
  return app.slice(at, stop);
};
assert.ok(existsSync(file("home.js")), "home.js missing");
const home = await import(file("home.js"));
assert.equal(typeof home.needItems, "function", "needItems missing");

const at = "2026-10-01T12:00:00Z";
const card = (number, fields) => ({ number, version: 3, board: "atlas", title: `Card ${number}`, status: "review", need: "you", updated_at: at, ...fields });
const proposal = card(21, { title: "Send the <beta>?", status: "backlog", need: "idle", proposal_status: "pending", body: "Exact\r\n  words <b>&", why: "Atlas orchestrator proposes a card.", actions: ["approve", "reject"], events: [{ kind: "add", actor: "conn:lead" }] });
const ask = card(22, { title: "Which headline?", ask: "Which one?\nSecond line.", ask_options: ["A", "B"], why: "Worker asks you: Which one?", actions: ["answer"] });
const stale = card(23, { need: "idle", status: "doing", why: "Worker has a claim that ran out.", actions: ["requeue", "reassign", "park"] });

if (which === "need_items") {
  // Guard: the queue order and reasons the shell still uses for its lookups.
  const needs = { asks: [ask], backlog_from_agents: [proposal], stale: [stale] };
  assert.deepEqual(home.needItems(needs).map((item) => [item.card.number, item.reason]), [[22, ask.why], [21, proposal.why], [23, stale.why]]);
  const legacy = { ...ask }; delete legacy.why;
  assert.deepEqual(home.needItems({ asks: [legacy] }).map((item) => item.reason), ["asks: Which one?"]);
  assert.deepEqual(home.needItems({}), []);
} else if (which === "origin_shell") {
  const { createNeeds } = await import(file("needs.js"));
  const rail = await import(file("rail.js"));
  const origin = card(7, { board: "atlas", title: "Release this origin", status: "backlog", proposal_status: "pending", need: "release", approval_kind: "origin", approval_role: "workspace owner", can_approve: true, actions: ["approve"], why: "Origin release needs your reason.", body: "Original words" });
  const plan = card(1, { need: "plan", status: "todo", labels: ["plan"] });
  const question = { ...ask, board: "harbor" };
  const needs = { approvals: [origin], asks: [question], plans: [plan], count: 3 };
  const node = (extra = {}) => ({ innerHTML: "", hidden: true, dataset: {}, contains: () => false, querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {}, setAttribute() {}, focus() {}, ...extra });
  const mount = node();
  const content = node({ querySelector: (selector) => selector === "[data-needs-mount]" && content.innerHTML.includes("data-needs-mount") ? mount : null });
  const panel = node();
  const nodes = Object.fromEntries(["#board-nav", "#phone-bar", "#phone-tabs", ".shell", "#title", "#head-actions"].map((selector) => [selector, node()]));
  Object.assign(nodes, { "#content": content, "#card-panel": panel });
  const state = { me: { actor: "user:maya", workspace: "work" }, needs, boards: [{ slug: "atlas", name: "Atlas" }, { slug: "harbor", name: "Harbor" }], selected: origin.number, route: "#/needs", base: "#/needs" };
  const shows = [];
  const context = {
    state, ...rail, needItems: home.needItems,
    document: { activeElement: null, title: "" }, root: { querySelector: (selector) => nodes[selector] || null },
    localStorage: { getItem: () => null }, speech: { supported: false }, speechView() {},
    api: async (path) => {
      if (path === "/api/holds") return { holds: [] };
      const number = Number(/^\/api\/cards\/(\d+)$/.exec(path)?.[1]);
      const found = [origin, plan, question].find((item) => item.number === number);
      assert.ok(found, `Unexpected API request: ${path}`);
      return { ...found, events: [] };
    },
    createNeeds: (options) => {
      const ui = createNeeds({ ...options, setInterval: () => 1, clearInterval() {} });
      const show = ui.show;
      ui.show = (data) => { shows.push(data.selected); return show(data); };
      return ui;
    },
    singleKeysEnabled: () => true, matchMedia: () => ({ matches: false }), mountWorkbenchChat() {}, closeCardPanel() {}, refresh() {},
  };
  runInNewContext([
    slice("function currentBoard()", "function closeCardPanel()"),
    slice("function navigation()", "function showError"),
    slice("function title(", "function decision("),
    slice("function needsCards()", "const hhmm"),
  ].join("\n"), context);
  const flush = async () => { for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve)); };
  try {
    context.navigation();
    const project = nodes["#board-nav"].outerHTML.match(/<a class="proj" href="#\/board\/atlas"[^>]*>[\s\S]*?<\/a>/);
    assert.ok(project, "Atlas rail entry missing");
    assert.match(project[0], />2<span class="sr"> need you<\/span>/, "Owner origin release must count in its project's rail badge");
    assert.deepEqual(home.needItems(needs).map(({ card }) => card.number), [22, 7, 1], "Release must rank after you and before plan, regardless of card number");
    context.needsView(); await flush();
    assert.equal(shows.at(-1), origin.number, "Origin approval must remain selected when opening Needs me");
    assert.ok(panel.innerHTML.includes(origin.title), "Selected origin detail must be shown");
    origin.version++;
    context.needsView(); await flush();
    assert.equal(shows.length, 2, "Changed payload must exercise the refresh selection gate");
    assert.equal(shows.at(-1), origin.number, "Origin approval selection must survive refresh");
    assert.equal(state.selected, origin.number);
    assert.ok(panel.innerHTML.includes(origin.title), "Refresh must keep the origin detail open");
    state.needs = { asks: [question], plans: [plan], count: 2 };
    context.needsView(); await flush();
    assert.equal(shows.at(-1), undefined, "A departed origin must not be passed through the shell selection gate");
  } finally { state.needsUI?.stop(); }
} else if (which === "home_removed") {
  // RED: Home's tiles and open-task table are removed with their stylesheet link.
  assert.equal(home.homeView, undefined, "homeView must be removed");
  const html = await read("index.html");
  assert.ok(!html.includes("/app/home.css"), "home.css link must be dropped");
  assert.ok(html.includes("/app/workbench.css") && html.includes("/app/chatdock.css"));
  assert.doesNotMatch(app, /homeView|home-tasks|home-spaces/);
} else if (which === "needs_view") {
  // RED: the shell's needsView renders the Needs me module (class="needs-me"), not Home.
  assert.ok(existsSync(file("needs.js")), "needs.js missing");
  const needsModule = await import(file("needs.js"));
  assert.equal(typeof needsModule.createNeeds, "function", "createNeeds missing");
  const { setPeople } = await import(file("api.js"));
  setPeople({ "user:maya": "Maya", "conn:lead": "Lead" });
  const node = (extra = {}) => ({ innerHTML: "", hidden: true, dataset: {}, contains: () => false, querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {}, setAttribute() {}, focus() {}, ...extra });
  const mount = node();
  const content = node({ querySelector: (selector) => selector === "[data-needs-mount]" && content.innerHTML.includes("data-needs-mount") ? mount : null });
  const panel = node();
  const nodes = { "#content": content, "#card-panel": panel, "#title": node(), "#head-actions": node(), "#digest-captions": node() };
  const requests = [];
  const controllers = [];
  const state = { me: { actor: "user:maya" }, needs: { backlog_from_agents: [proposal], asks: [ask], count: 2 }, boards: [{ slug: "atlas", name: "Atlas", coordinator: "conn:lead" }], open: [], selected: null, route: "#/needs", base: "#/needs" };
  const context = {
    state, document: { activeElement: null, title: "" }, root: { querySelector: (selector) => nodes[selector] || null },
    speech: { supported: false }, speechView() {}, needsCards: () => home.needItems(state.needs).map((item) => item.card),
    homeView: home.homeView, dot: () => "", taskRow: () => "", children: () => "",
    api: async (path, options = {}) => {
      requests.push({ path, ...options });
      if (path === "/api/holds") return { holds: [] };
      const number = Number(/^\/api\/cards\/(\d+)$/.exec(path)?.[1]);
      const found = [proposal, ask].find((item) => item.number === number);
      if (found) { const { actions, ...rest } = found; return { ...rest, can_approve: true }; }
      throw Object.assign(new Error("Not found"), { status: 404 });
    },
    createNeeds: (options) => { const ui = needsModule.createNeeds({ ...options, setInterval: () => 1, clearInterval() {} }); controllers.push(ui); return ui; },
    singleKeysEnabled: () => true, matchMedia: () => ({ matches: false }), mountWorkbenchChat() {}, closeCardPanel() {}, refresh() {},
  };
  runInNewContext([slice("function title(", "function decision("), slice("function needsView()", "function boardView()")].join("\n"), context);
  context.needsView();
  for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve));
  const rendered = content.innerHTML + mount.innerHTML;
  assert.ok(rendered.includes('class="needs-me"'), "needsView must render the Needs me module");
  assert.ok(!rendered.includes("home-tasks") && !rendered.includes("home-spaces"), "Home tiles and table must be gone");
  assert.ok(mount.innerHTML.includes("Send the &lt;beta&gt;?") && mount.innerHTML.includes("Which headline?"));
  assert.ok(content.innerHTML.includes('id="digest-captions"'), "spoken digest captions stay on Needs me");
  // The first item opens in the shared side panel with its exact words; the header keeps the chat toggle.
  assert.ok(panel.innerHTML.includes('class="needs-detail"') && panel.innerHTML.includes("Context (exact words)"));
  assert.ok(panel.innerHTML.includes("Exact&#13;\n  words &lt;b&gt;&amp;"));
  assert.equal(panel.hidden, false);
  assert.ok(nodes["#head-actions"].innerHTML.includes('id="chat-toggle"'));
  assert.ok(requests.some((r) => r.path === "/api/holds") && !requests.some((r) => r.method));
  // A refresh with the same queue keeps the mounted component and its selection.
  const reads = requests.length;
  context.needsView();
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setImmediate(resolve));
  assert.equal(controllers.length, 1);
  assert.equal(requests.length, reads);
  for (const ui of controllers) ui.stop();
} else assert.fail(`Unknown case: ${which}`);
console.log(`${which}: PASS`);
