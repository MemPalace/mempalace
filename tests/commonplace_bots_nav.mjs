import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const read = (name) => readFile(new URL(`../commonplace/web/static/${name}`, import.meta.url), "utf8");
const { resolveRoute, boardRoute } = await import("../commonplace/web/static/routes.js");
const { waitingTotal } = await import("../commonplace/web/static/bots.js");
const { renderRail, renderPhoneBar, renderPhoneTabs } = await import("../commonplace/web/static/rail.js");
const boards = [{ slug: "atlas" }, { slug: "harbor" }];
const hrefs = (html) => [...html.matchAll(/<a\b[^>]*href="([^"]*)"/g)].map((m) => m[1]);

test("a bare visit lands on the bots; first run keeps the start screen", () => {
  assert.equal(resolveRoute("#/", { boards, last: "harbor" }), "#/bots");
  assert.equal(resolveRoute("", { boards }), "#/bots");
  assert.equal(resolveRoute("#/", { boards: [] }), "#/start");
  assert.equal(resolveRoute("#/start", { boards }), "#/start");
  assert.equal(boardRoute({ boards, last: "harbor" }), "#/board/harbor");
});

test("every existing deep link still resolves and is still dispatched by the shell", async () => {
  const app = await read("app.js");
  // The shell keeps a hash as its screen only when this pattern accepts it; anything else falls back to the landing route.
  const source = /"#\/decide" : \(\/(\^[^\n]*?\$)\/\.test\(hash\)/.exec(app)?.[1];
  assert.ok(source, "screen pattern found in app.js");
  const screen = new RegExp(source);
  const links = ["#/board/atlas", "#/board/atlas/chat", "#/space/atlas", "#/boards", "#/start", "#/needs", "#/console", "#/console?agent=a1", "#/memory", "#/memory?q=exact%20words", "#/rules", "#/settings", "#/admin", "#/bots", "#/bots/3f2a9c"];
  for (const link of links) {
    assert.ok(screen.test(link), `${link} stays a screen`);
    assert.equal(resolveRoute(link, { boards }), link, `${link} is not rewritten`);
  }
  assert.equal(resolveRoute("#/chat/atlas", { boards }), "#/board/atlas/chat");
  assert.equal(resolveRoute("#/chat/atlas?at=3", { boards }), "#/board/atlas/chat?at=3");
  assert.equal(resolveRoute("#/card/121", { boards }), "#/card/121");
  assert.match(app, /\^#\\\/card\\\/\[1-9\]\\d\*\$/, "card links keep their own branch");
  assert.ok(app.includes('hash === "#/chat" ? boardRoute('), "bare #/chat still opens a project chat");
  assert.ok(app.includes("state.base = boardRoute("), "a shared card link and admin exit keep a project screen behind them");
});

test("the Bots badge is the waiting total and hides at zero", () => {
  const bot = (kind, waiting) => ({ kind, waiting });
  assert.equal(waitingTotal([bot("project", 2), bot("project", 1), bot("custom", 0)]), 3);
  assert.equal(waitingTotal([bot("general", 3), bot("project", 2), bot("project", 1)]), 3, "General already covers every project");
  assert.equal(waitingTotal([bot("project", -4), bot("project", 1.5), bot("project", "2"), {}, null]), 0);
  assert.equal(waitingTotal([]), 0);
  assert.equal(waitingTotal(undefined), 0);
  const count = (html) => /href="#\/bots"[^>]*>[^]*?<\/a>/.exec(html)[0];
  assert.match(count(renderRail({ botsWaiting: waitingTotal([bot("project", 2)]) })), /class="count need">2</);
  assert.match(count(renderPhoneTabs({ botsWaiting: 2 })), /class="count need">2</);
  assert.doesNotMatch(count(renderRail({ botsWaiting: waitingTotal([bot("project", 0)]) })), /count/);
  assert.doesNotMatch(count(renderPhoneTabs({ botsWaiting: 0 })), /count/);
});

test("the rail has exactly Bots and Memory; Workspace holds Projects, Needs me and Console; phone tabs are Bots and Memory", () => {
  const rail = renderRail({ can_admin: true });
  assert.deepEqual(hrefs(rail.slice(0, rail.indexOf("rail-foot"))), ["#/bots", "#/memory"]);
  const menu = rail.slice(rail.indexOf("user-links"));
  assert.match(menu, /role="group" aria-label="Workspace"/);
  assert.deepEqual(hrefs(menu).slice(0, 3), ["#/boards", "#/needs", "#/console"]);
  assert.match(menu, />Projects</);
  assert.deepEqual(hrefs(renderPhoneTabs({})), ["#/bots", "#/memory"]);
  assert.deepEqual(hrefs(renderPhoneBar({})).slice(0, 3), ["#/boards", "#/needs", "#/console"]);
  assert.doesNotMatch(rail + renderPhoneBar({}) + renderPhoneTabs({}), /\bBoards?\b/);
});

test("the shell reads the roster for the badge on other screens and takes it from the bots screen otherwise", async () => {
  const app = await read("app.js");
  assert.ok(app.includes('state.base.startsWith("#/bots") ? null : api("/api/bots")'), "no second read on the bots screen");
  assert.ok(app.includes("onRoster: (bots) => setBotsWaiting(waitingTotal(bots))"));
  assert.ok(app.includes("botsWaiting: state.botsWaiting"));
});
