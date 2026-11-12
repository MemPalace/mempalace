import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

// Import lazily from the file itself, so the module's relative imports resolve.
async function load(name) {
  return import(new URL(`../commonplace/web/static/${name}.js`, import.meta.url));
}
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const project = {
  board: "alpha", name: "Alpha", can_configure: true,
  main: { role: "main", name: "orchestrator:alpha", source: "seat", connected: false, status: null, wakeable: false, connectable: true, seen: false },
  models: { main: { provider: "claude", model: "claude-haiku-5-5" }, worker: { provider: "codex", model: "gpt-5.5" } },
};

test("main shows the automatic seat and owner Connect without a picker", async () => {
  const ui = await load("projects");
  assert.ok(Object.isFrozen(ui.PROJECT_COPY));
  const html = ui.mainHTML(project, e);
  assert.match(html, /orchestrator:alpha/);
  assert.match(html, /not connected/i);
  assert.match(html, /<button\b[^>]*>[\s\S]*?Connect agent/i);
  assert.doesNotMatch(html, /<select\b/);
  const member = ui.mainHTML({ ...project, can_configure: false, main: { ...project.main, connectable: false } }, e);
  assert.doesNotMatch(member, /<button\b[^>]*>[\s\S]*?Connect agent/i);
  const waiting = ui.mainHTML({ ...project, main: { ...project.main, connected: true } }, e);
  assert.match(waiting, /no agent has used it yet/i);
  assert.doesNotMatch(waiting, /Connected/);
  const seen = ui.mainHTML({ ...project, main: { ...project.main, connected: true, seen: true, status: "working" } }, e);
  assert.match(seen, /working/);
});

test("Connect result shows the one-time token and URL escaped", async () => {
  const ui = await load("projects");
  const html = ui.connectResultHTML({ token: "synthetic<unsafe>", mcp_url: "http://localhost/mcp?<unsafe>", board: "alpha", wing: "alpha", connection: { name: "orchestrator:alpha" }, setup: { webhook_add: { events: "space_message", board: "alpha" } } }, e);
  assert.ok(html.includes("synthetic&lt;unsafe&gt;"));
  assert.ok(html.includes("http://localhost/mcp?&lt;unsafe&gt;"));
  assert.match(html, /copy/i);
  assert.doesNotMatch(html, /<unsafe>/);
  const named = ui.mainHTML({ ...project, main: { ...project.main, name: "<unsafe>" } }, e);
  assert.ok(named.includes("&lt;unsafe&gt;"));
  assert.doesNotMatch(named, /<unsafe>/);
});

test("model fields show both roles and the chat copy points to Connect", async () => {
  const ui = await load("projects");
  const html = ui.modelsHTML(project, e);
  assert.match(html, /main/i);
  assert.match(html, /worker/i);
  assert.match(html, /claude-haiku-5-5/);
  assert.match(html, /gpt-5\.5/);
  assert.match(html, /<(?:input|select)\b/);
  // The no-orchestrator copy now lives in the chat pane's recovery strip, not in CHAT_COPY.
  const chat = await readFile(new URL("../commonplace/web/static/chat.js", import.meta.url), "utf8");
  assert.match(chat, /Connect an agent in Settings\./);
});

test("project memory controls preserve owner consent and escape the memory name", async () => {
  const ui = await load("projects");
  assert.equal(typeof ui.memoryHTML, "function");
  assert.equal(typeof ui.sharedWingHTML, "function");
  const html = ui.memoryHTML({...project, memory: {shared: false, wing: "<section>"}}, e);
  assert.match(html, /&lt;section&gt;/);
  assert.doesNotMatch(html, /data-project-own-wing/);
  assert.doesNotMatch(ui.sharedWingHTML({}, e, {allowShared: false}), /data-wing-choice="shared"/);
});

test("model fields cover the four roles and the escalation rule picks from the workspace list", async () => {
  const ui = await load("projects");
  const strong = { provider: "p", model: "strong<x>" }, other = { provider: "p", model: "other" };
  const rule = { after_failures: 2, labels: ["security"], model: strong };
  const owner = { ...project, can_configure: true, escalation: rule, model_catalog: [other, strong] };
  const html = ui.modelsHTML(owner, e);
  for (const role of ["main", "worker", "reviewer", "gate"]) assert.match(html, new RegExp(`<select name="${role}_model"`));
  for (const label of ["Orchestrator", "Builder", "Reviewer", "Gate"]) assert.match(html, new RegExp(label));
  // One picker for the rule, built from the same list; no free-text provider or model field for it.
  assert.match(html, /<select name="escalation_model"/);
  assert.doesNotMatch(html, /name="escalation_provider"/);
  const form = ui.escalationHTML(owner, e);
  assert.match(form, /Save escalation/);
  assert.match(form, /value="2"/);
  assert.match(form, /value="security"/);
  assert.match(form, /<option value="p strong&lt;x&gt;" selected>/);
  assert.match(form, /<option value="p other">/);
  assert.ok(form.includes("strong&lt;x&gt;"));
  assert.doesNotMatch(form, /strong<x>/);
  // A stored rule whose model left the list still shows as stored.
  assert.match(ui.escalationHTML({ ...owner, model_catalog: [] }, e), /<option value="p strong&lt;x&gt;" selected>/);
  const member = ui.escalationHTML({ ...owner, can_configure: false }, e);
  assert.doesNotMatch(member, /Save escalation/);
  assert.match(member, /<fieldset disabled>/);
  assert.match(ui.escalationHTML({ ...project, can_configure: true, escalation: null }, e), /No escalation rule/);
  const read = (fields) => (name) => fields[name] ?? "";
  assert.deepEqual(ui.escalationBody(read({})), { rule: null });
  assert.deepEqual(ui.escalationBody(read({ after_failures: "2", labels: "security, concurrency", escalation_model: "p m" })), { rule: { after_failures: 2, labels: ["security", "concurrency"], model: { provider: "p", model: "m" } } });
  assert.equal(ui.escalationBody(read({ after_failures: "2" })), null);
  assert.equal(ui.escalationBody(read({ escalation_model: "p m" })), null);
});
