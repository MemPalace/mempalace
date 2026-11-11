// Coordinator status block of Project settings (web/static/orchstatus.js). Run: node tests/orchstatus.mjs <case>
import assert from "node:assert/strict";

const ui = await import(new URL("../commonplace/web/static/orchstatus.js", import.meta.url));
const e = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const NOW = Date.parse("2030-01-01T12:00:00Z");
const iso = seconds => new Date(NOW + seconds * 1000).toISOString().replace(".000", "");
const owner = {board: "atlas", name: "Atlas", can_configure: true, orchestration: {default_max_agents: 3}};
const member = {...owner, can_configure: false};
const step = (key, kind, state, extra = {}) => ({step_key: key, kind, state, card: null, detail: null, voided: false, ...extra});
const view = (extra = {}) => ({
  board: "atlas", role: "main", config: {version: 3, sha256: "x"}, open: [], in_doubt: [], needs_person: [], unstarted: [],
  lease: {epoch: 2, holder: "conn:builder-bot", expires_at: iso(180), live: true}, ...extra,
});

// A fake DOM: only what the module touches. Controls are found by their data-* attribute in the markup.
class Host {
  isConnected = true; hidden = false; attrs = {}; html = ""; nodes = []; nextElementSibling = null;
  setAttribute(name, value) { this.attrs[name] = value; }
  hasAttribute(name) { return name in this.attrs; }
  set innerHTML(html) {
    this.html = html;
    this.nodes = [...html.matchAll(/<(button|p|span|details|div)\b([^>]*)>/g)].map(([, tag, attrs]) => ({
      tag, attributes: Object.fromEntries([...attrs.matchAll(/([\w-]+)(?:="([^"]*)")?/g)].map(m => [m[1], m[2] ?? ""])),
      disabled: /\bdisabled\b/.test(attrs),
    }));
  }
  get innerHTML() { return this.html; }
  replaceChildren() { this.html = ""; this.nodes = []; }
  querySelector(selector) {
    const name = selector.match(/^\[([\w-]+)\]$/)?.[1];
    assert.ok(name, `unsupported selector ${selector}`);
    return this.nodes.find(n => name in n.attributes) || null;
  }
  get text() { return this.html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim(); }
}
class Anchor {
  nextElementSibling = null;
  ownerDocument = {createElement: () => new Host()};
  after(node) { this.nextElementSibling = node; }
}
const click = async (host, name) => {
  const node = host.querySelector(`[data-orch-${name}]`);
  assert.ok(node, `data-orch-${name} missing`);
  assert.equal(node.disabled, false, `data-orch-${name} disabled`);
  await node.onclick();
};
const has = (host, name) => host.querySelector(`[data-orch-${name}]`) !== null;

/** `responses`: the checkpoint answers in order (the last one repeats); an Error is thrown as is. */
async function mount({project = owner, responses = [view()], post = {epoch: 2, released: true}} = {}) {
  const anchor = new Anchor(), calls = [];
  let reads = 0;
  const api = async (path, options = {}) => {
    calls.push({path, method: options.method || "GET", body: options.body});
    if (options.method === "POST") { if (post instanceof Error) throw post; return post; }
    const next = responses[Math.min(reads++, responses.length - 1)];
    if (next instanceof Error) throw next;
    return next;
  };
  await ui.mountOrchStatus(anchor, {project, api, e, now: () => NOW});
  return {anchor, host: anchor.nextElementSibling, calls};
}
const refusal = status => Object.assign(new Error("refused"), {status, payload: {error: "no"}});
const posts = calls => calls.filter(c => c.method === "POST");

const cases = {
  words() {
    assert.equal(ui.when(iso(-180), NOW), "3 min ago");
    assert.equal(ui.when(iso(120), NOW), "in 2 min");
    assert.equal(ui.when(iso(-2), NOW), "just now");
    assert.equal(ui.when("not a time", NOW), "");
    assert.equal(ui.leaseSentence(null, NOW), "Nobody is coordinating this project.");
  },
  async live_lease() {
    const {host, calls} = await mount({responses: [view({open: [step("m1", "merge", "started")], in_doubt: [step("t1", "thread", "in_doubt", {card: 7})], unstarted: [{id: 1}, {id: 2}]})]});
    assert.equal(calls[0].path, "/api/orch/boards/atlas/roles/main/checkpoint");
    assert.match(host.text, /Held by builder-bot\. The lease runs out in 3 min unless the agent renews it\./);
    assert.match(host.text, /1 step open · 1 in doubt · 2 Todo cards not started\. Accepted config: version 3\./);
    assert.match(host.text, /thread for card #7/);
    assert.ok(has(host, "end"));
    assert.equal(has(host, "stays"), false);
    assert.equal(posts(calls).length, 0);
  },
  async expired_lease() {
    const {host} = await mount({responses: [view({lease: {epoch: 2, holder: "conn:builder-bot", expires_at: iso(-180), live: false}, open: [step("m1", "merge", "started")]})]});
    assert.match(host.text, /Nobody is coordinating this project\. builder-bot held the lease until it ran out 3 min ago\./);
    assert.equal(has(host, "end"), false);
    assert.ok(has(host, "stays"));
  },
  async released_lease() {
    const {host} = await mount({responses: [view({lease: {epoch: 2, holder: "conn:builder-bot", expires_at: iso(180), live: false}})]});
    assert.match(host.text, /Nobody is coordinating this project\. builder-bot gave the lease up\./);
    assert.equal(has(host, "end"), false);
  },
  async no_lease() {
    const {host} = await mount({responses: [view({lease: null, config: null})]});
    assert.match(host.text, /Nobody is coordinating this project\./);
    assert.match(host.text, /No orchestrator config is accepted yet\./);
    assert.equal(has(host, "end"), false);
  },
  async member_sees_state_without_button() {
    const {host, calls} = await mount({project: member});
    assert.match(host.text, /Held by builder-bot/);
    assert.equal(has(host, "end"), false);
    assert.equal(has(host, "refresh"), true);
    assert.equal(posts(calls).length, 0);
  },
  async no_orchestration_shows_nothing() {
    const {anchor, calls} = await mount({project: {...owner, orchestration: undefined}});
    assert.equal(anchor.nextElementSibling, null);
    assert.equal(calls.length, 0);
  },
  async unreadable_board_hides_the_block() {
    for (const status of [403, 404]) {
      const {host} = await mount({responses: [refusal(status)]});
      assert.equal(host.hidden, true);
      assert.equal(host.html, "");
    }
  },
  async failed_read_says_so() {
    const {host} = await mount({responses: [refusal(500)]});
    assert.equal(host.text, "Could not load the coordinator status.");
  },
  async end_needs_a_confirm() {
    const {host, calls} = await mount();
    await click(host, "end");
    assert.ok(has(host, "confirm"));
    assert.match(host.text, /End builder-bot's lease\?/);
    assert.match(host.text, /keeps running and stays listed below/);
    assert.equal(posts(calls).length, 0);
    await click(host, "end-cancel");
    assert.equal(has(host, "confirm"), false);
    assert.ok(has(host, "end"));
    assert.equal(posts(calls).length, 0);
  },
  async end_lease_leaves_unfinished_work_listed() {
    const work = {open: [step("m1", "merge", "started")], in_doubt: [step("t1", "thread", "in_doubt", {detail: "builder running"})]};
    const ended = view({...work, lease: {epoch: 2, holder: "conn:builder-bot", expires_at: iso(180), live: false}});
    const {host, calls} = await mount({responses: [view(work), ended]});
    await click(host, "end");
    await click(host, "end-confirm");
    const sent = posts(calls);
    assert.equal(sent.length, 1);
    assert.equal(sent[0].path, "/api/orch/boards/atlas/roles/main/force-release");
    assert.deepEqual(sent[0].body, {});
    assert.match(host.text, /Nobody is coordinating this project\./);
    assert.match(host.text, /Lease ended\. Unfinished work stays listed below/);
    assert.match(host.text, /1 step open · 1 in doubt/);
    assert.match(host.text, /thread t1 — builder running/);
    assert.equal(has(host, "end"), false);
    assert.ok(has(host, "stays"));
  },
  async end_lease_once() {
    const {host, calls} = await mount();
    await click(host, "end");
    const node = host.querySelector("[data-orch-end-confirm]");
    const first = node.onclick();
    await node.onclick();
    await first;
    assert.equal(posts(calls).length, 1);
  },
  async nothing_to_end() {
    const {host} = await mount({responses: [view(), view({lease: {epoch: 2, holder: "conn:builder-bot", expires_at: iso(-5), live: false}})], post: {epoch: 2, released: false}});
    await click(host, "end");
    await click(host, "end-confirm");
    assert.match(host.text, /The lease had already ended\./);
  },
  async refusal_is_shown_and_the_lease_stays() {
    for (const [status, text] of [[403, "Only the workspace owner can end a lease."], [404, "This project is no longer available."], [500, "Could not end the lease. Nothing was changed. Try again."]]) {
      const {host} = await mount({post: refusal(status)});
      await click(host, "end");
      await click(host, "end-confirm");
      assert.match(host.text, new RegExp(text.replace(/\./g, "\\.")));
      assert.match(host.text, /Held by builder-bot/);
      assert.ok(has(host, "end"));
    }
  },
  async refresh_rereads() {
    const {host, calls} = await mount({responses: [view(), view({lease: null})]});
    await click(host, "refresh");
    assert.equal(calls.filter(c => c.method === "GET").length, 2);
    assert.match(host.text, /Nobody is coordinating this project\./);
  },
  async names_are_escaped() {
    const {host} = await mount({responses: [view({lease: {epoch: 1, holder: "conn:<img src=x onerror=alert(1)>", expires_at: iso(60), live: true}, in_doubt: [step("k", "thread", "in_doubt", {detail: "<b>x</b>"})]})]});
    assert.equal(host.html.includes("<img"), false);
    assert.equal(host.html.includes("<b>x"), false);
    assert.ok(host.html.includes("&lt;img"));
  },
  async stale_mount_draws_nothing() {
    const anchor = new Anchor();
    const api = async () => view();
    await ui.mountOrchStatus(anchor, {project: owner, api, e, current: () => false, now: () => NOW});
    assert.equal(anchor.nextElementSibling.html, "");
  },
};

const name = process.argv[2];
assert.ok(cases[name], `unknown case ${name}`);
await cases[name]();
