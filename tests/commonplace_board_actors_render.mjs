import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
const url = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const read = (name) => readFile(new URL(`../commonplace/web/static/${name}`, import.meta.url), "utf8");
const apiUrl = url(await read("api.js"));
const decideUrl = url((await read("decide.js")).replace('"./api.js"', `"${apiUrl}"`));
const load = async (name) => import(url((await read(name)).replace('"./api.js"', `"${apiUrl}"`).replace('"./decide.js"', `"${decideUrl}"`)));
const { childProgress } = await load("children.js");
const dot = (c) => `<i class="status-dot ${c.need}"></i>`;
const card = {
  number: 4, board: "work", title: "Top actor", need: "working", status: "doing",
  updated_at: new Date().toISOString(), children: { done: 2, total: 4 }, children_need_you: true,
  child_cards: ["done", "done", "review", "todo"].map((status, i) => ({ number: i + 5, title: 'Original <script> & "words"', status, need: status === "review" ? "you" : "idle" })),
};
const collapsed = childProgress(card, false, dot);
assert.match(collapsed, /2\/4 done/);
assert.match(collapsed, /aria-expanded="false"/);
assert.match(collapsed, /status-dot you/);
assert.doesNotMatch(collapsed, /Original/);
const expanded = childProgress(card, true, dot);
assert.match(expanded, /aria-expanded="true"/);
assert.equal((expanded.match(/<li>/g) || []).length, 4);
assert.match(expanded, /#5<\/a>.*Original &lt;script&gt; &amp; &quot;words&quot;/);
assert.match(expanded, /<span>review<\/span>/);
assert.doesNotMatch(expanded, /<script>/);
assert.equal(childProgress({ number: 1 }, false, dot), "");
assert.doesNotMatch(childProgress({ ...card, children_need_you: false }, false, dot), /status-dot you/);

// Exercise the app's actual chat map and hydration with a child absent from the Tasks list.
const app = await read("app.js");
// Home is gone: the project's task list and the chat each draw the progress under their own id.
const html = childProgress(card, true, dot, "space") + childProgress(card, true, dot, "chat");
assert.match(html, /id="children-space-4"/);
assert.match(html, /id="children-chat-4"/);
assert.equal((html.match(/2\/4 done/g) || []).length, 2);
assert.match(app, /children\(card, inline \? "chat" : "space"\)/);
const source = (start, end) => app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)));
const linkedTop = { ...card, message_id: 20, version: 1 };
const linkedChild = { number: 8, parent: 4, message_id: 20, version: 3, status: "review", ask: "Ship <original>?", ask_to: "user:owner", return_to: "conn:worker", ask_options: ["Ship", "Wait"], updated_at: card.updated_at };
const state = { screen: 1, base: "#/space/work", route: "#/space/work", chatBoard: "work", spaceTab: "chat", boards: [], cards: [linkedTop], linked: [linkedTop, linkedChild], chat: [{ id: 20 }], chatVisible: new Set([20]), chatTasks: new Map(), chatTaskRequests: new Map(), pending: new Set() };
const inline = {}, sidebar = {}, count = {}, chat = { dataset: { board: "work" } }, coordinator = {};
const message = { nextElementSibling: null, querySelector: () => inline };
const older = { nextElementSibling: message };
const stream = { querySelector: (selector) => selector === "[data-older]" ? older : message };
const content = {
  querySelector: (selector) => ({ ".space": chat, "[data-coordinator]": coordinator, ".space-stream": stream, ".space-tasks .stack": sidebar })[selector],
  querySelectorAll: (selector) => selector === "[data-task-count]" ? [count] : [],
};
const rendered = [], requests = [];
const { escape } = await import(apiUrl);
const context = {
  state, root: { querySelector: () => content }, enc: encodeURIComponent, e: escape, actor: (value) => value,
  title: () => {}, remember: () => ({}), restore: () => {}, capture: () => () => {},
  showError: (message) => { throw Error(message); },
  syncSpaceTasks: (target, cards) => rendered.push([target, Array.from(cards, (c) => c.number)]),
  api: async (path) => {
    requests.push(path);
    const current = state.linked.find((c) => path === `/api/cards/${c.number}`);
    return { ...current, events: current.ask ? [{ actor: current.return_to, target: current.ask_to, text: current.ask, at: current.updated_at, options: current.ask_options }] : [] };
  },
};
const view = runInNewContext([
  source("function decision(", "\nfunction needsCards()"),
  source("async function hydrateSpaceTasks()", "\nfunction mergeChat("),
  source("function spaceView()", "\nfunction boardsView()"),
  "({ hydrateSpaceTasks, spaceView, conversation })",
].join("\n"), context);
await view.hydrateSpaceTasks();
assert.ok(state.chatTasks.has(linkedChild.number));
assert.deepEqual(rendered.find(([target]) => target === inline)[1], [4, 8]);
assert.deepEqual(rendered.find(([target]) => target === sidebar)[1], [4]);
assert.equal(count.textContent, 1);
const askHtml = view.conversation(state.chatTasks.get(linkedChild.number));
assert.match(askHtml, /data-action="decide" data-card="8"/);
assert.match(askHtml, /Ship &lt;original&gt;\?/);
state.linked[1] = { ...linkedChild, version: 4 };
await view.hydrateSpaceTasks();
assert.equal(state.chatTasks.get(8).version, 4);
assert.equal(requests.filter((path) => path === "/api/cards/8").length, 2);
