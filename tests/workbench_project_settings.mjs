import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../commonplace/web/static/projects.js", import.meta.url), "utf8");
const ui = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const e = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const project = { board: "atlas", name: "Atlas", can_configure: true, main: { name: "orchestrator:atlas", connected: true, seen: true, connectable: true }, limits: {max_agents: 3} };
const local = {provider: "openai_compatible", base_url: "http://localhost:9000", model: "small", enabled: true, applies: true};
const hosted = [{provider: "anthropic", keys: [{ref: "env:COMMONPLACE_CHAT_KEY_SAMPLE", label: "Saved key 1 · anthropic"}], base_urls: []}];
function exists(name) { assert.equal(typeof ui[name], "function", `${name} missing`); return ui[name]; }
// A small form fixture: nodes are made only from controls present in the rendered HTML.
class Host {
  isConnected = true;
  nodes = [];
  set innerHTML(html) {
    this.html = html; this.nodes = [];
    // Inputs have no closing tag; discover every opening tag independently.
    for (const match of html.matchAll(/<(input|select|button|output|p|a|div|span)\b([^>]*)>/g)) {
      const [, tag, attrs] = match;
      const attributes = Object.fromEntries([...attrs.matchAll(/([\w-]+)(?:="([^"]*)")?/g)].map(m => [m[1], m[2] ?? ""]));
      const content = html.slice(match.index + match[0].length).split(`</${tag}>`)[0];
      const option = content.match(/<option\b[^>]*value="([^"]*)"[^>]*selected/) || content.match(/<option\b[^>]*value="([^"]*)"/);
      this.nodes.push({tag, attributes, disabled: "disabled" in attributes, checked: "checked" in attributes, value: tag === "select" ? option?.[1] || "" : attributes.value || "", textContent: content.replace(/<[^>]+>/g, ""), focus() {}, getAttribute(name) { return this.attributes[name]; }, setAttribute(name, value) { this.attributes[name] = value; }});
    }
  }
  get innerHTML() { return this.html; }
  querySelectorAll(selector) {
    const m = selector.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
    assert.ok(m, `unsupported fixture selector ${selector}`);
    return this.nodes.filter(n => m[1] in n.attributes && (m[2] === undefined || n.attributes[m[1]] === m[2]));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest() { return null; }
}
const node = (host, selector) => { const n = host.querySelector(selector); assert.ok(n, `${selector} missing`); return n; };
const click = async (host, selector) => { const n = node(host, selector); assert.equal(n.disabled, false); assert.equal(typeof n.onclick, "function"); await n.onclick({currentTarget: n, preventDefault() {}, stopPropagation() {}}); };
const change = async (host, selector, value) => { const n = node(host, selector); assert.equal(n.disabled, false); if (value !== undefined) n.value = value; assert.equal(typeof n.onchange, "function"); await n.onchange({currentTarget: n}); };
async function chat({front = local, error, models = ["small"], owner = true} = {}) {
  const mount = exists("mountChatAnswers"), host = new Host(), calls = [];
  const api = async (path, options = {}) => {
    calls.push({path, ...options});
    if (options.method === "PUT") { if (error) throw error; return {front: {...options.body.front, applies: true}}; }
    return {front, local_models: models, local_reachable: true, hosted};
  };
  await mount(host, {project: {...project, can_configure: owner}, api, e});
  const puts = () => calls.filter(c => c.method === "PUT");
  return {host, calls, puts};
}
const cases = {
  async hosted_panel() {
    const {host, calls} = await chat(); const before = calls.length;
    await change(host, '[data-chat-mode="hosted"]');
    assert.equal(calls.length, before); node(host, "[data-chat-turn-on]");
  },
  async hosted_turn_on() {
    const {host, puts} = await chat(); await change(host, '[data-chat-mode="hosted"]');
    await change(host, "[data-chat-provider]", "anthropic");
    await change(host, "[data-chat-key]", "0");
    await change(host, "[data-chat-hosted-model]", "answer-model");
    await click(host, "[data-chat-turn-on]");
    assert.equal(puts().length, 1); assert.equal(puts()[0].body.mode, "hosted"); assert.equal(puts()[0].body.scope, "atlas");
    assert.equal(puts()[0].body.front.key_ref, hosted[0].keys[0].ref);
    assert.match(node(host, "[data-chat-status]").textContent, /saved \d\d:\d\d/);
  },
  async local_400() {
    const {host, puts} = await chat({error: {status: 400, payload: {error: "private server detail"}}});
    await change(host, '[data-chat-mode="local"]'); assert.equal(puts().length, 1);
    assert.doesNotMatch(node(host, "[data-chat-status]").textContent, /saved/);
    assert.match(host.innerHTML, /Could not save chat settings/); assert.doesNotMatch(host.innerHTML, /private server detail/);
    assert.equal(node(host, '[data-chat-mode="local"]').checked, true);
    assert.equal(host.querySelector("[data-chat-turn-on]"), null);
  },
  async disclosure() {
    const {host} = await chat(); await change(host, '[data-chat-mode="hosted"]');
    assert.match(host.innerHTML, /Every chat message in Atlas/); assert.match(host.innerHTML, /Cards, memory and other projects are not sent/);
    assert.match(host.innerHTML, /Nothing is sent until you press Turn on/);
    assert.doesNotMatch(host.innerHTML, /COMMONPLACE_CHAT_KEY/);
  },
  async phone_escape() {
    const bind = exists("bindSettingsNavigation");
    for (const [origin, label] of [["#/board/atlas", "Back to board"], ["#/board/atlas/chat", "Back to the chat"], ["#/needs", "Back to Needs me"]]) {
      const listeners = new Map(), doc = {addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name)};
      const host = new Host(); host.innerHTML = '<a data-project-back></a>'; let returned = null;
      const stop = bind(host, {slug: "atlas", origin, document: doc, onBack: value => returned = value});
      assert.equal(node(host, "[data-project-back]").textContent, label);
      assert.ok(listeners.has("keydown"));
      listeners.get("keydown")({key: "Escape", preventDefault() {}, stopImmediatePropagation() {}});
      assert.equal(returned, origin); stop(); assert.equal(listeners.size, 0);
    }
  },
  async invalid_local_provider() {
    const {host, puts} = await chat({error: {status: 400, payload: {error: "invalid_local_provider"}}});
    await change(host, '[data-chat-mode="local"]'); assert.equal(puts().length, 1);
    assert.equal(node(host, '[data-chat-mode="local"]').checked, true);
    assert.equal(host.querySelector("[data-chat-turn-on]"), null);
    assert.match(host.innerHTML, /Could not save chat settings/); assert.doesNotMatch(node(host, "[data-chat-status]").textContent, /saved/);
  },
  async basic() {
    const {host, puts} = await chat(); await change(host, '[data-chat-mode="basic"]');
    assert.deepEqual(puts().map(c => c.body), [{scope: "atlas", front: {provider: "none"}}]);
  },
  async local_urls() {
    for (const [front, expected] of [[local, local.base_url], [{provider: "none"}, "http://127.0.0.1:11434"], [{provider: "anthropic", model: "remote"}, "http://127.0.0.1:11434"], [{...local, base_url: "https://service.example"}, "http://127.0.0.1:11434"]]) {
      const {host, puts} = await chat({front}); await change(host, '[data-chat-mode="local"]');
      assert.equal(puts().length, 1); assert.equal(puts()[0].body.mode, "local");
      assert.equal(puts()[0].body.front.base_url, expected); assert.equal(puts()[0].body.front.provider, "openai_compatible");
      assert.equal(puts()[0].body.front.model, "small");
    }
  },
  async missing_model() {
    const {host, puts} = await chat({front: {provider: "none"}, models: []});
    await change(host, '[data-chat-mode="local"]'); assert.equal(puts().length, 0);
    assert.match(host.innerHTML, /Pick a model/); assert.equal(node(host, '[data-chat-mode="local"]').checked, true);
  },
  async not_loopback() {
    const {host, puts} = await chat({error: {status: 400, payload: {error: "not_loopback"}}});
    await change(host, '[data-chat-mode="local"]'); assert.equal(puts().length, 1);
    assert.match(host.innerHTML, /That address is not on this computer/); node(host, "[data-chat-turn-on]");
    assert.doesNotMatch(node(host, "[data-chat-status]").textContent, /saved/);
  },
  async inherited() {
    const {host, puts} = await chat({front: {provider: "anthropic", model: "remote", inherited: true, applies: false}});
    assert.match(host.innerHTML, /Hosted · set for the whole workspace/); assert.match(host.innerHTML, /Off until you press Turn on/);
    node(host, "[data-chat-turn-on]"); assert.equal(puts().length, 0);
  },
  async single_flight() {
    const mount = exists("mountChatAnswers"), host = new Host(), calls = []; let release;
    await mount(host, {project, e, api: async (_path, options = {}) => {
      if (!options.method) return {front: local, local_models: ["small"], hosted};
      calls.push(options); return new Promise(resolve => release = () => resolve({front: options.body.front}));
    }});
    await change(host, '[data-chat-mode="hosted"]'); await change(host, "[data-chat-provider]", "anthropic");
    await change(host, "[data-chat-key]", "0"); await change(host, "[data-chat-hosted-model]", "answer-model");
    const button = node(host, "[data-chat-turn-on]"), first = button.onclick(); await button.onclick();
    assert.equal(calls.length, 1); assert.ok(release); release(); await first;
  },
  async limits() {
    const mount = exists("mountAgentLimit"), host = new Host(), calls = [];
    await mount(host, {project, e, api: async (path, options) => {calls.push({path, ...options}); return {...project, limits: options.body};}});
    await click(host, '[data-limit-step="1"]'); assert.equal(calls[0].body.max_agents, 4);
    assert.equal(calls[0].path, "/api/projects/atlas/limits"); assert.equal(calls[0].method, "PUT");
    assert.match(node(host, "[data-limit-status]").textContent, /saved \d\d:\d\d/);
    await mount(host, {project: {...project, limits: {max_agents: 20}}, e, api: async () => assert.fail("over cap")});
    assert.equal(node(host, '[data-limit-step="1"]').disabled, true);
    await mount(host, {project: {...project, limits: {max_agents: 1}}, e, api: async () => assert.fail("under cap")});
    assert.equal(node(host, '[data-limit-step="-1"]').disabled, true);
  },
  async limits_failure() {
    const mount = exists("mountAgentLimit"), host = new Host();
    await mount(host, {project, e, api: async () => { throw {status: 400}; }});
    await click(host, '[data-limit-step="1"]'); assert.equal(node(host, "[data-limit-value]").textContent, "3");
    assert.doesNotMatch(node(host, "[data-limit-status]").textContent, /saved/);
    await mount(host, {project: {...project, limits: {max_agents: null}}, e, api: async (_p, o) => ({...project, limits: o.body})});
    assert.equal(node(host, "[data-limit-value]").textContent, "No limit");
    await click(host, '[data-limit-step="1"]'); assert.equal(node(host, "[data-limit-value]").textContent, "1");
  },
  async phases() {
    const render = exists("connectPhaseHTML");
    assert.match(render("waiting", project, e), /Waiting for the agent/);
    assert.match(render("done", project, e), /Delivered to the orchestrator/);
    const failed = render("failed", project, e); assert.match(failed, /Try again/); assert.match(failed, /120/);
  },
  async settings_screen() {
    const render = exists("projectSettingsHTML");
    const html = render(project, e, {origin: "#/needs"});
    for (const text of ["Atlas", "Orchestrator", "Chat answers", "Agents", "Connect an agent on a computer", "Project memory"]) assert.ok(html.includes(text), text);
    assert.match(html, /data-project-back/); assert.match(html, /data-project-chat/); assert.match(html, /data-project-limit/);
    const restart = render(project, e, {origin: "#/board/atlas/chat", restart: true});
    assert.match(restart, /How to restart the agent/); assert.match(restart, /Check again/);
  },
  guard_own_memory() {
    assert.equal(typeof ui.memoryHTML, "function");
    assert.match(ui.memoryHTML({...project, memory: {shared: true, wing: "shared"}}, e), /data-project-own-wing/);
    assert.doesNotMatch(ui.memoryHTML({...project, can_configure: false, memory: {shared: true, wing: "shared"}}, e), /data-project-own-wing/);
  },
  guard_fixed_errors() {
    assert.equal(typeof ui.ownWingError, "function");
    assert.equal(ui.ownWingError({error: "wing_taken"}), ui.PROJECT_COPY.wingTaken);
    assert.doesNotMatch(ui.ownWingError({error: "private server detail"}), /private server detail/);
  },
  guard_shared_consent() {
    assert.equal(typeof ui.sharedWingHTML, "function"); const html = ui.sharedWingHTML({}, e);
    assert.match(html, /data-wing-choice="own"/); assert.match(html, /data-wing-choice="shared"/); assert.match(html, /data-wing-choice="cancel"/);
  },
};
assert.ok(process.argv[2] in cases, "unknown case");
await cases[process.argv[2]]();
console.log(`PASS ${process.argv[2]}`);
