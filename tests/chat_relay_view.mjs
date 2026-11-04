import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function load() {
  const source = await readFile(new URL("../commonplace/web/static/chat.js", import.meta.url), "utf8");
  return import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
}
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const page = (model) => ({
  messages: [{ id: 1, actor: "user:owner", at: "2026-01-01T00:00:00Z", text: "Question" }],
  replies: [{ id: 1, message_id: 1, model, kind: "answer", text: "Answer" }],
});

test("runner answer labels and escaping", async () => {
  const ui = await load();
  assert.ok(Object.isFrozen(ui.CHAT_COPY));
  assert.equal(ui.CHAT_COPY.runnerAnswer, "via your {cli} login");
  assert.equal(ui.CHAT_COPY.runnerMode, "Answers via your {cli} login");
  assert.equal(ui.CHAT_COPY.runnerUsage, "{used}/{cap} today");
  assert.equal(ui.CHAT_COPY.runnerError, "Last error: {code}");
  const html = ui.chatHTML(page("runner:claude/claude-haiku-5-5"), { user: "owner" }, e);
  assert.match(html, /via your claude login · claude-haiku-5-5/);
  assert.doesNotMatch(html, /Orchestrator/);
  const escaped = ui.chatHTML(page("runner:claude/<b>"), { user: "owner" }, e);
  assert.match(escaped, /&lt;b&gt;/);
  assert.doesNotMatch(escaped, /<b>/);
  assert.match(ui.chatHTML(page("plain-model"), { user: "owner" }, e), /Orchestrator/);
});

test("runner status, daily use, errors and basic fallback", async () => {
  const ui = await load();
  const runner = { online: true, cli: "claude", last_error: null, used_today: 12, daily_cap: 200 };
  const status = { front: { mode: "runner", model: null, runner }, starts: [], needs_you: 0 };
  const html = ui.statusHTML(status, e);
  assert.match(html, /Answers via your claude login/);
  assert.match(html, /12\/200 today/);
  const failed = { ...status, front: { ...status.front, runner: { ...runner, last_error: "cli_timeout" } } };
  assert.match(ui.statusHTML(failed, e), /Last error: cli_timeout/);
  const basic = { ...failed, front: { ...failed.front, mode: "basic" } };
  const fallback = ui.statusHTML(basic, e);
  assert.ok(fallback.includes(ui.CHAT_COPY.basic));
  assert.match(fallback, /Last error: cli_timeout/);
});
