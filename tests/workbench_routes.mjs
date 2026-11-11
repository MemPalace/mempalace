import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../commonplace/web/static/routes.js", import.meta.url), "utf8");
const routes = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
assert.equal(typeof routes.resolveRoute, "function", "resolveRoute export missing");
assert.equal(typeof routes.boardRoute, "function", "boardRoute export missing");
const { resolveRoute, boardRoute } = routes;
const boards = Object.freeze([Object.freeze({slug: "atlas"}), Object.freeze({slug: "harbor"})]);
const context = Object.freeze({boards, last: "harbor"});
const checks = {
  root() {
    // A person who is set up lands on their bots, whatever board they visited last.
    assert.equal(resolveRoute("#/", context), "#/bots");
    assert.equal(resolveRoute("#/", {boards}), "#/bots");
    assert.equal(resolveRoute("#/", {boards, last: "removed"}), "#/bots");
    assert.equal(resolveRoute("", context), "#/bots");
    assert.equal(resolveRoute("#/", {boards: [{slug: "a b"}]}), "#/bots");
  },
  board() {
    // The old landing board is still what a shared card link and "back from admin" sit on.
    assert.equal(boardRoute(context), "#/board/harbor");
    assert.equal(boardRoute({boards}), "#/board/atlas");
    assert.equal(boardRoute({boards, last: "removed"}), "#/board/atlas");
    assert.equal(boardRoute({boards: [{slug: "a b"}]}), "#/board/a%20b");
    assert.equal(boardRoute({boards: []}), "#/start");
    assert.equal(boardRoute(), "#/start");
  },
  bots() {
    for (const route of ["#/bots", "#/bots/3f2a9c", "#/bots/a_b-c"]) assert.equal(resolveRoute(route, context), route);
  },
  empty() {
    assert.equal(resolveRoute("#/", {boards: [], last: "removed"}), "#/start");
    assert.equal(resolveRoute("#/"), "#/start");
    assert.equal(resolveRoute("#/start", context), "#/start");
  },
  chat() {
    assert.equal(resolveRoute("#/chat/atlas", context), "#/board/atlas/chat");
    assert.equal(resolveRoute("#/chat/a%20b?at=12", context), "#/board/a%20b/chat?at=12");
    assert.equal(resolveRoute("#/board/atlas/chat", context), "#/board/atlas/chat");
  },
  space() {
    assert.equal(resolveRoute("#/space/atlas", context), "#/space/atlas");
    assert.equal(resolveRoute("#/space/atlas?at=12", context), "#/space/atlas?at=12");
  },
  needs() {
    for (const route of ["#/needs", "#/memory?q=exact%20words", "#/card/121", "#/decide?list=1", "#/rules", "#/console", "#/console?agent=a1", "#/settings", "#/admin", "#/boards", "#/board/atlas", "#/board/atlas/chat", "#/unknown"]) {
      assert.equal(resolveRoute(route, context), route);
    }
  },
};
assert.ok(Object.hasOwn(checks, process.argv[2]), "Unknown route check");
checks[process.argv[2]]();
console.log(`${process.argv[2]}: PASS`);
