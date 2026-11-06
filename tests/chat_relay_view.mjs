import assert from "node:assert/strict";
import test from "node:test";

async function load() {
  return import("../commonplace/web/static/chat.js");
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
  assert.doesNotMatch(escaped, /login · <b>/);
  // Raw <b> tags come only from the template, never from data.
  const tags = (html) => (html.match(/<b>/g) || []).length;
  assert.equal(tags(escaped), tags(ui.chatHTML(page("runner:claude/plain"), { user: "owner" }, e)));
  const board = { slug: "atlas", name: "Atlas", coordinator: "conn:iris" };
  const agent = { ...page("plain-model"), project: board, messages: [...page("plain-model").messages, { id: 2, actor: "conn:iris", at: "2026-01-01T00:01:00Z", text: "On it" }] };
  assert.match(ui.chatHTML(agent, { user: "owner" }, e), /<b>Atlas orchestrator<\/b>/);
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

for (const model of ["anthropic/fast", "openai_compatible/fast", "runner:claude/fast"]) {
  test(`RED ${model} answer is labelled hosted`, async () => {
    const ui = await load(); assert.equal(typeof ui.chatHTML, "function");
    const html = ui.chatHTML(page(model), { user: "owner" }, e);
    assert.match(html, /Quick reply · hosted provider/);
    assert.doesNotMatch(html, /Quick reply · local model/);
  });
}

test("RED stored local marker controls the label and is never printed", async () => {
  const ui = await load(); assert.equal(typeof ui.chatHTML, "function");
  const html = ui.chatHTML(page("local:openai_compatible/fast"), { user: "owner" }, e);
  assert.match(html, /Quick reply · local model/);
  assert.doesNotMatch(html, /local:/);
  // A later local setting must never reclassify a stored hosted answer.
  const hosted = { ...page("openai_compatible/fast"), front: { base_url: "http://localhost:11434" } };
  assert.match(ui.chatHTML(hosted, { user: "owner" }, e), /Quick reply · hosted provider/);
});

test("RED inherited hosted setting requires this board's Turn on", async () => {
  const ui = await load(); assert.equal(typeof ui.statusHTML, "function");
  const html = ui.statusHTML({ front: { mode: "off", provider: "anthropic", inherited: true, applies: false } }, e);
  assert.match(html, /Hosted · set for the whole workspace/);
  assert.match(html, /Turn on in Settings for this board/);
  assert.doesNotMatch(html, /Answers by/);
});
