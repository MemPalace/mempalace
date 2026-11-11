// The Deck look: the per-browser switch, the bar and tabs it draws in place of the rail, the
// screen registry and the shell wiring. Run by tests/test_commonplace_deck.py with node --test.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const STATIC = new URL("../commonplace/web/static/", import.meta.url);
const read = (name) => readFileSync(new URL(name, STATIC), "utf8");
const deck = await import(new URL("deck.js", STATIC));
const rail = await import(new URL("rail.js", STATIC));

function storage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return { getItem: (k) => (data.has(k) ? data.get(k) : null), setItem: (k, v) => data.set(k, String(v)), removeItem: (k) => data.delete(k), data };
}
const doc = () => ({ documentElement: { dataset: {} }, listeners: [], addEventListener(type, fn) { this.listeners.push([type, fn]); } });

test("the switch is off by default, survives a broken storage and is one key", () => {
  assert.equal(deck.deckOn(storage()), false);
  assert.equal(deck.deckOn(null), false);
  assert.equal(deck.deckOn({ getItem() { throw new Error("denied"); } }), false);
  const s = storage();
  deck.setDeck(true, s);
  assert.deepEqual([...s.data], [[deck.UI_KEY, "deck"]]);
  assert.equal(deck.deckOn(s), true);
  deck.setDeck(false, s);
  assert.equal(s.data.size, 0);
  deck.setDeck(true, { setItem() { throw new Error("full"); } });
});

test("applyDeck marks the document and honours ?ui= once, keeping the rest of the address", () => {
  const s = storage(), d = doc(), calls = [];
  const hist = { state: { a: 1 }, replaceState: (...args) => calls.push(args) };
  assert.equal(deck.applyDeck({ doc: d, loc: { search: "?ui=deck&x=1", pathname: "/app/", hash: "#/bots" }, hist, storage: s }), true);
  assert.equal(d.documentElement.dataset.ui, "deck");
  assert.deepEqual(calls, [[{ a: 1 }, "", "/app/?x=1#/bots"]]);
  assert.equal(deck.applyDeck({ doc: d, loc: { search: "?ui=classic", pathname: "/app/", hash: "" }, hist, storage: s }), false);
  assert.equal("ui" in d.documentElement.dataset, false);
  assert.equal(calls[1][2], "/app/");
  deck.applyDeck({ doc: d, loc: { search: "?ui=neon", pathname: "/app/", hash: "" }, hist, storage: s });
  assert.equal(calls.length, 2, "an unknown value changes nothing");
  assert.equal(deck.deckOn(s), false);
});

test("the bar offers Bots and Settings, and a Deck screen only once it is registered", () => {
  const bare = deck.renderDeckBar({ hash: "#/bots/abc", screens: {} });
  assert.ok(bare.startsWith('<nav class="rail"'), "the shell gives this element its id");
  assert.deepEqual([...bare.matchAll(/data-nav="([a-z]+)"/g)].map((m) => m[1]), ["bots", "settings"]);
  assert.equal((bare.match(/aria-current="page"/g) || []).length, 1);
  assert.match(bare, /href="#\/bots" data-nav="bots" aria-current="page"/);
  const full = deck.renderDeckBar({ hash: "#/today", needs: 3, screens: { today() {}, compute() {} } });
  assert.deepEqual([...full.matchAll(/data-nav="([a-z]+)"/g)].map((m) => m[1]), ["today", "bots", "compute", "settings"]);
  assert.match(full, /data-nav="today" aria-current="page">Today<span class="count need">3/);
  assert.match(full, /class="deck-brand" href="#\/today"/);
  assert.equal(deck.deckHome({}), "#/bots");
  assert.equal(deck.deckHome({ today() {} }), "#/today");
  for (const [hash, section] of [["#/rules", "settings"], ["#/admin", "settings"], ["#/settings", "settings"], ["#/compute", "compute"], ["#/board/x", ""], ["#/memory?q=a", ""], ["", ""]]) assert.equal(deck.deckSection(hash), section, hash);
});

test("the bar keeps the shell's actions, escapes names and shows Admin to admins only", () => {
  const html = deck.renderDeckBar({ workspace: '<b>"W"', user: "<i>Maya", can_admin: true, rulesWaiting: 2, screens: {} });
  for (const action of ["palette", "switch-workspace", "shortcuts", "logout"]) assert.equal((html.match(new RegExp(`data-action="${action}"`, "g")) || []).length, 1, action);
  assert.match(html, /id="cmdk"/);
  assert.doesNotMatch(html, /<b>"W"|<i>Maya/);
  assert.match(html, /&lt;b&gt;&quot;W&quot;/);
  for (const href of ["#/needs", "#/boards", "#/memory", "#/console", "#/rules", "#/admin"]) assert.equal((html.match(new RegExp(`href="${href}"`, "g")) || []).length, 1, href);
  for (const can_admin of [false, undefined, "true", 1]) assert.doesNotMatch(deck.renderDeckBar({ can_admin, screens: {} }), /#\/admin/);
  assert.doesNotMatch(html, /<style|style="/);
});

test("phone: a title bar and one tab per offered section", () => {
  const tabs = deck.renderDeckTabs({ needs: 2, hash: "#/settings", screens: { compute() {} } });
  assert.deepEqual([...tabs.matchAll(/href="#\/([a-z]+)"/g)].map((m) => m[1]), ["bots", "compute", "settings"]);
  assert.match(tabs, /data-count="3"/);
  assert.match(tabs, /data-nav="settings" aria-current="page"/);
  assert.match(tabs, /Bots<span class="count need">2/);
  assert.match(deck.renderDeckTabs({ needs: 2, botsWaiting: 5, screens: {} }), /Bots<span class="count need">5/);
  assert.match(deck.renderDeckBar({ needs: 2, botsWaiting: 5, screens: {} }), /Bots<span class="count need">5/);
  assert.match(deck.renderDeckTop({ hash: "#/bots" }), /<b>Bots<\/b>/);
  assert.match(deck.renderDeckTop({ hash: "#/board/x" }), /<b>Commonplace<\/b>/);
  assert.match(deck.renderDeckTop(), /data-action="palette"/);
});

test("the shell gets the Deck bar, title and tabs only while the switch is on", () => {
  const s = storage(), ui = deck.createDeck({ root: {}, context: () => ({}), title() {}, screens: { today() {} }, storage: s, doc: doc() });
  assert.deepEqual([ui.bar({ workspace: "Northwind" }), ui.top(), ui.tabs({ needs: 1 })], ["", "", ""]);
  assert.doesNotMatch(rail.renderRail({ view: "bots" }) + rail.renderPhoneBar() + rail.renderPhoneTabs(), /data-deck/);
  s.setItem(deck.UI_KEY, "deck");
  assert.match(ui.bar({ workspace: "Northwind", needs: 2, boards: [], view: "bots" }), /^<nav class="rail" data-deck="bar"[\s\S]*data-nav="today"[\s\S]*Northwind/);
  assert.match(ui.top(), /data-deck="top"/);
  assert.match(ui.tabs({ needs: 1 }), /data-deck="tabs" data-count="3"/);
});

test("createDeck mounts a registered screen once, refreshes it, and stops it on leave", async () => {
  const s = storage({ [deck.UI_KEY]: "deck" }), d = doc(), log = [];
  const content = { id: "content" };
  let epoch = 4;
  const screens = { today: async () => ({ TITLE: "Today", mount(ctx) { log.push(["mount", ctx.root, ctx.me, typeof ctx.api, typeof ctx.e, typeof ctx.go]); return { refresh: (ctx) => log.push(["refresh", ctx.needs]), stop: () => log.push(["stop"]) }; } }) };
  const ui = deck.createDeck({ root: { querySelector: (q) => (q === "#content" ? content : null) }, api: () => {}, context: () => ({ me: "maya", boards: [], needs: { count: 1 }, go() {} }), title: (t) => log.push(["title", t]), screen: () => epoch, screens, storage: s, doc: d });
  assert.equal(ui.on(), true);
  assert.equal(ui.home(), "#/today");
  assert.equal(ui.owns("#/today"), true);
  assert.equal(ui.owns("#/compute"), false, "not registered");
  assert.equal(ui.owns("#/today/x"), false);
  await ui.show("#/today", { background: true, epoch: 4 });
  assert.deepEqual(log, [], "a background refresh never opens a screen");
  await ui.show("#/today", { epoch: 3 });
  assert.deepEqual(log, [], "a screen the person already left is not mounted");
  await ui.show("#/today", { epoch: 4 });
  assert.deepEqual(log, [["mount", content, "maya", "function", "function", "function"], ["title", "Today"]]);
  await ui.show("#/today", { background: true, epoch: 4 });
  assert.deepEqual(log.at(-1), ["refresh", { count: 1 }]);
  ui.leave(); ui.leave();
  assert.deepEqual(log.filter(([k]) => k === "stop").length, 1);
  s.removeItem(deck.UI_KEY);
  assert.equal(ui.owns("#/today"), false, "Deck screens do not exist while the switch is off");
});

test("the Look switch is added to Settings once and flips the look", () => {
  const s = storage(), d = doc();
  let reloads = 0, html = "";
  const box = { querySelector: (q) => (q === "#look" && html.includes('id="look"') ? {} : null), insertAdjacentHTML: (where, markup) => { assert.equal(where, "beforeend"); html += markup; } };
  const content = { querySelector: (q) => (q === ".settings" ? box : null) };
  const ui = deck.createDeck({ root: {}, context: () => ({}), title() {}, reload: () => reloads++, screens: {}, storage: s, doc: d });
  ui.settings(content); ui.settings(content); ui.settings(null); ui.settings({ querySelector: () => null });
  assert.equal((html.match(/id="look"/g) || []).length, 1);
  assert.match(html, /data-deck-action="toggle" aria-pressed="false">Try the Deck look/);
  assert.match(deck.lookHTML(true), /aria-pressed="true">Go back to the classic look/);
  const [type, click] = d.listeners[0];
  assert.equal(type, "click");
  click({ target: { closest: () => null } });
  assert.equal(reloads, 0);
  click({ target: { closest: (q) => (q === '[data-deck-action="toggle"]' ? {} : null) } });
  assert.equal(ui.on(), true);
  assert.equal(d.documentElement.dataset.ui, "deck");
  assert.equal(reloads, 1);
  ui.toggle();
  assert.equal(ui.on(), false);
  assert.equal("ui" in d.documentElement.dataset, false);
});

test("Settings carries the workspace tools in the Deck look only, with Admin for admins", () => {
  const make = (on, me) => {
    const s = storage(on ? { [deck.UI_KEY]: "deck" } : {});
    const parts = [];
    const box = { querySelector: (q) => (parts.some(([, m]) => m.includes(`id="${q.slice(1)}"`)) ? {} : null), insertAdjacentHTML: (where, markup) => parts.push([where, markup]) };
    const ui = deck.createDeck({ root: {}, context: () => ({ me, needs: { count: 3 } }), title() {}, screens: {}, storage: s, doc: doc() });
    ui.settings({ querySelector: (q) => (q === ".settings" ? box : null) });
    ui.settings({ querySelector: (q) => (q === ".settings" ? box : null) });
    return parts;
  };
  assert.deepEqual(make(false, { can_admin: true }).map(([where]) => where), ["beforeend"], "classic look: the Look switch only");
  const on = make(true, { can_admin: true });
  assert.deepEqual(on.map(([where]) => where), ["afterbegin", "beforeend"]);
  assert.deepEqual([...on[0][1].matchAll(/href="(#\/[a-z]+)"/g)].map((m) => m[1]), ["#/needs", "#/boards", "#/memory", "#/console", "#/rules", "#/admin"]);
  assert.match(on[0][1], /Needs me<span class="count need">3/);
  for (const can_admin of [false, undefined, "true"]) assert.doesNotMatch(make(true, { can_admin })[0][1], /#\/admin/);
  assert.doesNotMatch(deck.toolsHTML(), /<style|style="/);
});

test("the shell is wired through state.deck and the stylesheet is inert without the switch", () => {
  const app = read("app.js"), index = read("index.html"), css = read("deck.css");
  assert.match(app, /import \{ applyDeck, createDeck \} from "\.\/deck\.js";/);
  assert.match(app, /^applyDeck\(\);$/m);
  assert.match(app, /^state\.deck = createDeck\(\{$/m);
  const allow = app.split("\n").find((line) => line.includes("state.base = start"));
  const pattern = new RegExp(/\(\/(\^.+\$)\/\.test\(hash\)/.exec(allow)[1]);
  for (const hash of ["#/today", "#/compute", "#/bots", "#/settings"]) assert.match(hash, pattern);
  for (const hash of ["#/today/x", "#/computer"]) assert.doesNotMatch(hash, pattern);
  // Sliced shell functions run in other tests without deck.js: they may reach it only through state.
  for (const name of ["deckOn", "renderDeckBar", "deckHome"]) assert.doesNotMatch(app, new RegExp(`\\b${name}\\b`), name);
  // Other tests load rail.js on its own, so it must not import the Deck module.
  assert.doesNotMatch(read("rail.js"), /deck/i);
  assert.ok(index.indexOf('href="/app/deck.css"') > index.indexOf('href="/app/bots.css"'));
  assert.ok(index.indexOf('href="/app/deck.css"') < index.indexOf('src="/app/app.js"'));
  const rules = css.replace(/\/\*[\s\S]*?\*\//g, "").split("\n").map((line) => line.trim()).filter((line) => line && line !== "}");
  for (const line of rules) {
    if (line.startsWith("@font-face") || line.startsWith("@media")) continue;
    const head = line.split("{")[0];
    if (/^--|^color-scheme/.test(head) || !head.trim()) continue;
    // Commas inside :is(...) or :not(...) belong to one selector.
    for (const selector of head.split(/,(?![^()]*\))/)) assert.match(selector.trim(), /^:root\[data-ui="deck"\]/, line);
  }
  assert.match(css, /url\("fonts\/inter-latin\.woff2"\)/);
  assert.doesNotMatch(css, /https?:\/\//);
});
