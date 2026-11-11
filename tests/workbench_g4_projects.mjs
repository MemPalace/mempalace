import assert from "node:assert/strict";

// A small document: enough markup parsing and selector matching for the project dialogs.
const VOID = new Set(["input", "br", "img", "hr", "meta", "link"]);
const unescape = (value) => value.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");

class Text {
  constructor(text) { this.text = text; this.parent = null; }
  get textContent() { return this.text; }
}

class El {
  constructor(tag, doc) {
    Object.assign(this, { localName: tag.toLowerCase(), doc, attrs: new Map(), children: [], parent: null, listeners: {}, _value: null });
  }
  get tagName() { return this.localName.toUpperCase(); }
  get isConnected() { let node = this; while (node.parent) node = node.parent; return node === this.doc.documentElement; }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  hasAttribute(name) { return this.attrs.has(name); }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  removeAttribute(name) { this.attrs.delete(name); }
  toggleAttribute(name, on) { if (on) this.setAttribute(name, ""); else this.removeAttribute(name); }
  get id() { return this.getAttribute("id") || ""; }
  set id(value) { this.setAttribute("id", value); }
  get className() { return this.getAttribute("class") || ""; }
  set className(value) { this.setAttribute("class", value); }
  get classList() { return { contains: (name) => this.className.split(/\s+/).includes(name) }; }
  get dataset() {
    return new Proxy({}, { get: (_, key) => this.getAttribute(`data-${String(key).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`) ?? undefined });
  }
  get disabled() { return this.hasAttribute("disabled"); }
  set disabled(value) { this.toggleAttribute("disabled", !!value); }
  get hidden() { return this.hasAttribute("hidden"); }
  set hidden(value) { this.toggleAttribute("hidden", !!value); }
  get value() {
    if (this._value !== null) return this._value;
    if (this.localName === "select") { const options = this.querySelectorAll("option"); return (options.find((o) => o.hasAttribute("selected")) || options[0])?.value ?? ""; }
    return this.getAttribute("value") ?? "";
  }
  set value(value) { this._value = String(value); }
  get textContent() { return this.children.map((child) => child.textContent).join(""); }
  set textContent(value) { this.replaceChildren(new Text(String(value))); }
  set innerHTML(html) { this.replaceChildren(...parse(html, this.doc)); }
  get innerHTML() { return this.textContent; }
  append(...nodes) {
    for (const node of nodes) {
      node.parent?.children.splice(node.parent.children.indexOf(node), 1);
      node.parent = this; this.children.push(node);
    }
  }
  replaceChildren(...nodes) { for (const child of this.children) child.parent = null; this.children = []; this.append(...nodes); }
  remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } }
  contains(node) { for (let n = node; n; n = n.parent) if (n === this) return true; return false; }
  elements() { return this.children.filter((c) => c instanceof El).flatMap((c) => [c, ...c.elements()]); }
  querySelectorAll(selector) { return this.elements().filter((el) => matches(el, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  matches(selector) { return matches(this, selector); }
  closest(selector) { for (let n = this; n instanceof El; n = n.parent) if (matches(n, selector)) return n; return null; }
  getClientRects() { return this.isConnected && !this.closest("[hidden]") ? [{}] : []; }
  focus() { if (this.isConnected) this.doc.activeElement = this; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); }
  dispatch(type, extra = {}) {
    const event = { type, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}, stopImmediatePropagation() {}, ...extra };
    for (const fn of this.listeners[type] || []) fn(event);
    return event;
  }
  showModal() { this.setAttribute("open", ""); }
  click() { return this.onclick?.({ currentTarget: this, target: this, preventDefault() {}, stopPropagation() {} }); }
}

function parse(html, doc) {
  const root = new El("root", doc), stack = [root];
  for (const m of html.matchAll(/<!--[\s\S]*?-->|<\/([a-zA-Z0-9]+)\s*>|<([a-zA-Z0-9]+)((?:\s+[^\s=>/]+(?:="[^"]*")?)*)\s*\/?>|([^<]+)/g)) {
    const [, close, open, attrs, text] = m;
    if (text) stack.at(-1).append(new Text(unescape(text)));
    else if (close) { while (stack.length > 1 && stack.pop().localName !== close.toLowerCase()); }
    else if (open) {
      const el = new El(open, doc);
      for (const a of (attrs || "").matchAll(/([^\s=]+)(?:="([^"]*)")?/g)) el.setAttribute(a[1], unescape(a[2] ?? ""));
      stack.at(-1).append(el);
      if (!VOID.has(el.localName)) stack.push(el);
    }
  }
  const nodes = [...root.children]; root.replaceChildren(); return nodes;
}

function compound(el, part) {
  let rest = part;
  const tag = rest.match(/^[a-z0-9]+/i);
  if (tag) { if (el.localName !== tag[0].toLowerCase()) return false; rest = rest.slice(tag[0].length); }
  while (rest) {
    let m;
    if ((m = rest.match(/^:not\(([^()]*)\)/))) { if (compound(el, m[1])) return false; }
    else if ((m = rest.match(/^#([\w-]+)/))) { if (el.id !== m[1]) return false; }
    else if ((m = rest.match(/^\.([\w-]+)/))) { if (!el.classList.contains(m[1])) return false; }
    else if ((m = rest.match(/^\[([\w-]+)(?:="([^"]*)")?\]/))) { if (!el.hasAttribute(m[1]) || (m[2] !== undefined && el.getAttribute(m[1]) !== m[2])) return false; }
    else throw new Error(`unsupported selector ${part}`);
    rest = rest.slice(m[0].length);
  }
  return true;
}

function matches(el, selector) {
  return selector.split(",").some((one) => {
    const parts = one.trim().replace(/\s*>\s*/g, " > ").split(/\s+/);
    const test = (node, i) => {
      if (!(node instanceof El) || !compound(node, parts[i])) return false;
      if (i === 0) return true;
      if (parts[i - 1] === ">") return test(node.parent, i - 2);
      for (let up = node.parent; up instanceof El; up = up.parent) if (test(up, i - 1)) return true;
      return false;
    };
    return test(el, parts.length - 1);
  });
}

function makeDocument() {
  const doc = { listeners: {}, adoptedStyleSheets: [] };
  doc.documentElement = new El("html", doc);
  doc.body = new El("body", doc); doc.documentElement.append(doc.body);
  doc.activeElement = doc.body;
  doc.createElement = (tag) => new El(tag, doc);
  doc.querySelector = (s) => doc.documentElement.querySelector(s);
  doc.querySelectorAll = (s) => doc.documentElement.querySelectorAll(s);
  doc.addEventListener = (type, fn) => (doc.listeners[type] ||= []).push(fn);
  doc.removeEventListener = (type, fn) => { doc.listeners[type] = (doc.listeners[type] || []).filter((f) => f !== fn); };
  doc.key = (key, extra = {}) => {
    const event = { key, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}, stopImmediatePropagation() {}, ...extra };
    for (const fn of [...(doc.listeners.keydown || [])]) fn(event);
    return event;
  };
  return doc;
}

const doc = makeDocument();
globalThis.document = doc;
globalThis.window = { addEventListener() {}, removeEventListener() {} };
globalThis.location = { hash: "" };
globalThis.CSSStyleSheet = class { replaceSync() {} };
globalThis.CSS = { escape: (value) => String(value) };
globalThis.confirm = () => { throw new Error("window.confirm must not be used"); };
// Clipboard and console are recorded so a test can check what was copied and that no credential is logged.
const clipboard = [];
Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { writeText: async (text) => { clipboard.push(text); } } } });
const logged = [];
for (const level of ["log", "info", "warn", "error", "debug"]) console[level] = (...args) => logged.push(args.map(String).join(" "));
const say = (text) => process.stdout.write(`${text}\n`);

const ui = await import(new URL("../commonplace/web/static/projects.js", import.meta.url));
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const tick = async (n = 10) => { for (let i = 0; i < n; i++) await new Promise((resolve) => setImmediate(resolve)); };
const openDialog = () => doc.querySelector("dialog[open]");
const project = { board: "ledger", name: "Ledger", can_configure: true, main: { name: "orchestrator:ledger", connected: true, seen: false, connectable: true } };
const result = { token: "synthetic-token", mcp_url: "http://localhost/mcp", setup: { webhook_add: { board: "ledger" } } };

function opener(label = "opener") {
  const button = doc.createElement("button");
  button.setAttribute("data-project-connect", ""); button.textContent = label;
  doc.body.append(button); button.focus();
  return button;
}

function assertTrapped(dialog) {
  const controls = dialog.querySelectorAll("a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary");
  assert.ok(controls.length >= 2, "the dialog has controls to cycle");
  controls.at(-1).focus();
  assert.equal(doc.key("Tab").defaultPrevented, true, "Tab on the last control stays in the dialog");
  assert.equal(doc.activeElement, controls[0]);
  assert.equal(doc.key("Tab", { shiftKey: true }).defaultPrevented, true, "Shift+Tab on the first control stays in the dialog");
  assert.equal(doc.activeElement, controls.at(-1));
  doc.activeElement = doc.body;
  doc.key("Tab");
  assert.ok(dialog.contains(doc.activeElement), "Tab from outside lands inside the dialog");
}

const cases = {
  // Review MAJOR 2: "Create and copy setup" copies the MCP setup the agent app reads (address and
  // Bearer key in the Settings > Agents format), not the bare token; nothing is logged.
  async setup_clipboard() {
    const { clientConfig } = await import(new URL("../commonplace/web/static/agents.js", import.meta.url));
    const button = opener();
    const realTimeout = globalThis.setTimeout;
    globalThis.setTimeout = () => 0;
    clipboard.length = 0; logged.length = 0;
    try {
      ui.openConnectDialog({ slug: "ledger", e, opener: button, api: async (path, options = {}) => options.method === "POST" ? result : { ...project, main: { ...project.main, connected: false } } });
      const dialog = openDialog();
      assert.match(dialog.textContent, /MCP address/, "the dialog says what the setup holds");
      await dialog.querySelector("[data-project-create]").click();
      await tick(20);
      assert.equal(clipboard.length, 1, "Create and copy setup writes the clipboard once");
      const copied = clipboard[0];
      assert.notEqual(copied, result.token, "the clipboard holds more than the bare token");
      assert.equal(copied, clientConfig("claude-code", result.mcp_url, result.token), "the setup is the Settings > Agents format");
      assert.ok(copied.includes(result.mcp_url) && copied.includes(`Bearer ${result.token}`), "address and Bearer key are in the setup");
      assert.match(dialog.querySelector("[data-project-copy-status]").textContent, /Setup copied for Claude Code/);
      const app = dialog.querySelector("[data-project-app]");
      app.value = "codex"; app.onchange();
      await dialog.querySelector("[data-project-copy-setup]").click();
      await tick(20);
      assert.equal(clipboard.at(-1), clientConfig("codex", result.mcp_url, result.token), "the chosen agent app's format is copied");
      assert.ok(!logged.some((line) => line.includes(result.token)), "the credential is never logged");
      dialog.dispatch("cancel");
    } finally { globalThis.setTimeout = realTimeout; button.remove(); }
  },
  // Review MAJOR 1/4: Disconnect revokes the connection workspace-wide. The dialog names known
  // assignments elsewhere, never claims "no other project" (wing access is not visible here), and
  // always keeps the line that revoking stops its access in every project.
  async disconnect_scope() {
    const boards = [{ slug: "ledger", name: "Ledger", coordinator: null }, { slug: "harbor", name: "Harbor", coordinator: null }, { slug: "atlas", name: "Atlas", coordinator: null }];
    const cardsOf = { ledger: [{ number: 1, assignee: "conn:worker-1" }], harbor: [{ number: 2, assignee: "conn:worker-1" }], atlas: [{ number: 3, assignee: "conn:other" }] };
    const generic = /stops all of its access in every project of this workspace/;
    for (const mode of ["assigned", "none", "unreadable"]) {
      const readable = mode !== "unreadable";
      const cards = mode === "none" ? { ...cardsOf, harbor: [] } : cardsOf;
      const host = doc.createElement("div"); doc.body.append(host);
      const api = async (path) => {
        if (path === "/api/connections") return { connections: [{ id: "c-9", name: "worker-1", last_used_at: null }] };
        if (path === "/api/boards") { if (!readable) throw { status: 500 }; return { boards }; }
        const slug = path.match(/^\/api\/boards\/([^/]+)\/cards$/)?.[1];
        if (slug) return { cards: cards[slug], linked: [], children: [] };
        return project;
      };
      await ui.mountProjectAgents(host, { project, api, e, current: () => true, onUpdate() {} });
      const pending = host.querySelector('[data-project-disconnect="c-9"]').click();
      await tick();
      const dialog = openDialog();
      assert.ok(dialog, "a drawn dialog is open");
      assert.match(dialog.textContent, /for the whole workspace/, "the dialog names the workspace-wide scope");
      assert.match(dialog.textContent, generic, `${mode}: the workspace-wide access line is always shown`);
      assert.doesNotMatch(dialog.textContent, /works in no other project/i, `${mode}: no exhaustive negative claim`);
      assert.doesNotMatch(dialog.textContent, /Atlas/, `${mode}: a board without its assignment is not named`);
      if (mode === "assigned") assert.match(dialog.textContent, /also assigned in Harbor\./, "the known assignment is named");
      if (mode === "none") assert.match(dialog.textContent, /No known assignments in other projects\./);
      if (mode === "unreadable") assert.match(dialog.textContent, /could not be checked/);
      dialog.dispatch("cancel");
      await pending;
      host.remove();
    }
  },
  // 03c: the empty board's Connect an agent is a modal dialog; Esc returns to the opener.
  async connect_dialog() {
    assert.equal(typeof ui.openConnectDialog, "function", "openConnectDialog missing");
    const button = opener();
    const calls = [];
    const dismiss = ui.openConnectDialog({ slug: "ledger", api: async (path, options) => { calls.push({ path, ...options }); return { ...project, main: { ...project.main, connected: false } }; }, e, opener: button });
    assert.equal(typeof dismiss, "function");
    const dialog = openDialog();
    assert.ok(dialog, "a dialog is open");
    assert.equal(dialog.getAttribute("aria-modal"), "true");
    assert.ok(doc.activeElement.matches("[data-project-create]"), "the primary action has focus");
    await tick();
    assert.match(dialog.querySelector("#project-connect-title").textContent, /Connect an agent to Ledger/);
    assertTrapped(dialog);
    dialog.dispatch("cancel");
    assert.equal(openDialog(), null);
    assert.equal(doc.activeElement, button);
    assert.ok(!calls.some((c) => c.method === "POST"), "Cancel creates no setup");
    button.remove();
  },
  // 03c → 03c2: the primary action makes the setup in the same dialog.
  async connect_continues() {
    assert.equal(typeof ui.openConnectDialog, "function", "openConnectDialog missing");
    const button = opener();
    const calls = [];
    const api = async (path, options = {}) => {
      calls.push({ path, ...options });
      if (options.method === "POST") return result;
      return { ...project, main: { ...project.main, connected: false } };
    };
    const later = [];
    const realTimeout = globalThis.setTimeout;
    globalThis.setTimeout = (fn) => { later.push(fn); return 0; };
    try {
      ui.openConnectDialog({ slug: "ledger", api, e, opener: button });
      const dialog = openDialog();
      await dialog.querySelector("[data-project-create]").click();
      await tick();
      assert.equal(openDialog(), dialog, "the same dialog continues");
      assert.deepEqual(calls.filter((c) => c.method === "POST").map((c) => c.path), ["/api/projects/ledger/orchestrator/connect"]);
      assert.equal(dialog.querySelector("[data-project-token]").value, "synthetic-token");
      assert.ok(doc.activeElement.matches("[data-project-token]"), "the setup has focus");
      assert.match(dialog.textContent, /Waiting for the agent/);
      dialog.dispatch("cancel");
      assert.equal(openDialog(), null);
      assert.equal(doc.activeElement, button);
    } finally { globalThis.setTimeout = realTimeout; button.remove(); }
  },
  // 03c2: Tab stays inside the setup dialog; Esc closes it and focus returns to Connect.
  async setup_trap() {
    assert.equal(typeof ui.openConnectDialog, "function", "openConnectDialog missing");
    const button = opener();
    const realTimeout = globalThis.setTimeout;
    globalThis.setTimeout = () => 0;
    try {
      ui.showConnectResult(result, e, { api: async () => project, path: "/api/projects/ledger", project, opener: button });
      const dialog = openDialog();
      assert.ok(doc.activeElement.matches("[data-project-token]"));
      assertTrapped(dialog);
      assert.equal(doc.key("Escape").defaultPrevented, true, "the dialog takes Escape");
      assert.equal(openDialog(), null);
      assert.equal(doc.activeElement, button);
    } finally { globalThis.setTimeout = realTimeout; button.remove(); }
  },
  // 03d: the connected phase moves focus to its primary button.
  async done_focus() {
    assert.equal(typeof ui.openConnectDialog, "function", "openConnectDialog missing");
    const button = opener();
    ui.showConnectResult(result, e, { api: async () => ({ ...project, main: { ...project.main, seen: true } }), path: "/api/projects/ledger", project, opener: button });
    await tick();
    const dialog = openDialog();
    assert.match(dialog.querySelector("#project-connect-title").textContent, /Orchestrator connected/);
    assert.ok(doc.activeElement.matches(".btn.primary") && dialog.contains(doc.activeElement), `focus: ${doc.activeElement.tagName}`);
    dialog.dispatch("cancel");
    assert.equal(openDialog(), null);
    button.remove();
  },
  // 03e: after 120 s the failed phase names the dialog and focuses its primary button.
  async failed_focus() {
    assert.equal(typeof ui.openConnectDialog, "function", "openConnectDialog missing");
    const button = opener();
    const realNow = Date.now, realTimeout = globalThis.setTimeout;
    let now = 1_000_000, later = null;
    Date.now = () => now;
    globalThis.setTimeout = (fn) => { later = fn; return 0; };
    try {
      ui.showConnectResult(result, e, { api: async () => project, path: "/api/projects/ledger", project, opener: button });
      await tick();
      assert.ok(later, "the next check is scheduled");
      now += 121_000;
      later(); await tick();
      const dialog = openDialog();
      assert.match(doc.querySelector("#project-connect-title").textContent, /not connected/);
      assert.equal(doc.querySelectorAll("#project-connect-title").length, 1, "one element names the dialog");
      assert.ok(doc.activeElement.matches(".btn.primary") && dialog.contains(doc.activeElement), `focus: ${doc.activeElement.tagName}`);
      dialog.dispatch("cancel");
      assert.equal(openDialog(), null);
    } finally { Date.now = realNow; globalThis.setTimeout = realTimeout; button.remove(); }
  },
  // 10c/10d: Disconnect asks in a drawn dialog with Cancel focused; window.confirm is never used.
  async disconnect_drawn() {
    assert.equal(typeof ui.openConnectDialog, "function", "openConnectDialog missing");
    assert.equal(typeof ui.mountProjectAgents, "function", "mountProjectAgents missing");
    const host = doc.createElement("div"); doc.body.append(host);
    const calls = [];
    const api = async (path, options = {}) => {
      calls.push({ path, ...options });
      if (path === "/api/connections") return { connections: [{ id: "c-1", name: "orchestrator:ledger", last_used_at: null }] };
      if (path.endsWith("/cards")) return { cards: [], linked: [] };
      return { ...project, main: { ...project.main, connected: false } };
    };
    await ui.mountProjectAgents(host, { project, api, e, current: () => true, onUpdate() {} });
    const disconnect = host.querySelector('[data-project-disconnect="c-1"]');
    disconnect.focus();
    const pending = disconnect.click();
    await tick();
    const dialog = doc.querySelector('[aria-modal="true"], dialog[open]');
    assert.ok(dialog, "a drawn dialog is open");
    assert.match(dialog.textContent, /Disconnect Ledger orchestrator\?/);
    assert.match(dialog.textContent, /What happens:/);
    assert.match(dialog.textContent, /It is the only agent in Ledger/);
    assert.ok(doc.activeElement.matches('[data-confirm="cancel"]'), "Cancel has focus");
    assertTrapped(dialog);
    assert.equal(doc.key("Escape").defaultPrevented, true, "the dialog takes Escape");
    await pending;
    assert.equal(openDialog(), null);
    assert.equal(doc.activeElement, host.querySelector('[data-project-disconnect="c-1"]'));
    assert.ok(!calls.some((c) => c.method === "DELETE"), "Esc disconnects nothing");
    const again = host.querySelector('[data-project-disconnect="c-1"]').click();
    await tick();
    openDialog().querySelector('[data-confirm="ok"]').click();
    await again; await tick();
    assert.deepEqual(calls.filter((c) => c.method === "DELETE").map((c) => c.path), ["/api/connections/c-1"]);
    host.remove();
  },
  // 10f: settings opened to restart the agent draw the amber heading with the times.
  async restart_heading() {
    assert.equal(typeof ui.openConnectDialog, "function", "openConnectDialog missing");
    const { deriveChatState } = await import(new URL("../commonplace/web/static/chatstate.js", import.meta.url));
    const html = ui.projectSettingsHTML(project, e, { origin: "#/board/ledger/chat", restart: true });
    assert.match(html, /class="orch-card warn"/);
    assert.doesNotMatch(ui.projectSettingsHTML(project, e, { origin: "#/board/ledger" }), /orch-card warn/);
    const asked = "2026-01-05T14:02:00", seen = "2026-01-05T14:03:00";
    const page = { me: { user: "user:maya" }, messages: [{ id: 7, actor: "user:maya", text: "Please draft the quarterly summary", at: asked }] };
    const chat = deriveChatState(page, { orchestrator: { visible: true, last_seen: seen } }, {}, Date.parse(asked) + 600_000);
    const text = ui.notAnsweringText(project, chat, page);
    const hhmm = (value) => new Date(value).toTimeString().slice(0, 5);
    assert.equal(text, `Not answering since ${hhmm(asked)} · connected · last active ${hhmm(seen)}`);
    assert.match(ui.mainHTML(project, e, { notAnswering: text }), /Not answering since/);
  },
  // Card 422: the models form saves even while the page's slow optional reads are still pending.
  async models_submit_before_loads() {
    const calls = [], stalled = new Promise(() => {}), field = (el, name) => el.querySelector(`[name="${name}"]`);
    let stored = { main: null, worker: null };
    const api = async (path, options = {}) => {
      calls.push({ path, method: options.method || "GET", body: options.body });
      if (path.startsWith("/api/chat-settings") || path === "/api/connections" || path.endsWith("/cards")) return stalled;
      if (path.startsWith("/api/chat/")) return path.endsWith("/status") ? { orchestrator: null } : { messages: [], replies: [] };
      if (options.method === "PUT") stored = { ...stored, ...options.body };
      return { ...project, can_configure: true, limits: { max_agents: 3 }, models: stored };
    };
    const host = doc.createElement("div"); doc.body.append(host);
    void ui.mountProject(host, { slug: "ledger", api, e, settings: true, origin: "#/board/ledger" });
    for (let turn = 0; turn < 20 && !host.querySelector("[data-project-models]"); turn++) await new Promise((r) => setTimeout(r, 0));
    const form = host.querySelector("[data-project-models]");
    assert.ok(form, "settings draw the models form");
    assert.ok(calls.some((c) => c.path === "/api/connections"), "the slow read is still pending");
    field(form, "main_provider").value = "anthropic";
    field(form, "main_model").value = "model-a";
    assert.equal(typeof form.onsubmit, "function", "without a handler the browser submits the form as a GET and drops the values");
    const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} };
    await form.onsubmit(event);
    assert.equal(event.defaultPrevented, true);
    const puts = calls.filter((c) => c.method === "PUT");
    assert.equal(puts.length, 1);
    assert.equal(puts[0].path, "/api/projects/ledger/models");
    assert.deepEqual(puts[0].body, { main: { provider: "anthropic", model: "model-a" }, worker: null });
    assert.match(host.querySelector("[data-project-model-status]").textContent, /saved/);
    // A reload draws the stored choice back into the fields.
    const again = doc.createElement("div"); doc.body.append(again);
    void ui.mountProject(again, { slug: "ledger", api, e, settings: true, origin: "#/board/ledger" });
    for (let turn = 0; turn < 20 && !again.querySelector("[data-project-models]"); turn++) await new Promise((r) => setTimeout(r, 0));
    assert.equal(field(again, "main_provider").getAttribute("value"), "anthropic");
    assert.equal(field(again, "main_model").getAttribute("value"), "model-a");
    host.remove(); again.remove();
  },
  // Card 422: the handler is the same one once the slow reads fail or answer late, and it still saves.
  async models_submit_after_reads_settle() {
    for (const outcome of ["reject", "late"]) {
      const calls = [], gates = [], field = (el, name) => el.querySelector(`[name="${name}"]`);
      const slow = (value) => new Promise((resolve, reject) => gates.push(() => (outcome === "reject" ? reject(new Error("slow read failed")) : resolve(value))));
      const api = async (path, options = {}) => {
        calls.push({ path, method: options.method || "GET", body: options.body });
        if (path.startsWith("/api/chat-settings")) return slow({ front: { provider: "none" }, local_models: [], hosted: [] });
        if (path === "/api/connections") return slow({ connections: [] });
        if (path.endsWith("/cards")) return slow({ cards: [], linked: [] });
        if (path.startsWith("/api/chat/")) return path.endsWith("/status") ? { orchestrator: null } : { messages: [], replies: [] };
        return { ...project, can_configure: true, limits: { max_agents: 3 }, models: options.method === "PUT" ? options.body : {} };
      };
      const host = doc.createElement("div"); doc.body.append(host);
      const mounted = ui.mountProject(host, { slug: "ledger", api, e, settings: true, origin: "#/board/ledger" });
      for (let turn = 0; turn < 20 && !host.querySelector("[data-project-models]"); turn++) await new Promise((r) => setTimeout(r, 0));
      const form = host.querySelector("[data-project-models]"), handler = form.onsubmit;
      assert.equal(typeof handler, "function", `${outcome}: bound before the reads settle`);
      for (let turn = 0; turn < 20 && gates.length < 3; turn++) await new Promise((r) => setTimeout(r, 0));
      for (const settle of gates) settle();
      await mounted;
      assert.equal(host.querySelector("[data-project-models]"), form, `${outcome}: the form is not redrawn`);
      assert.equal(form.onsubmit, handler, `${outcome}: the handler is still attached`);
      field(form, "worker_provider").value = "local"; field(form, "worker_model").value = "model-w";
      await form.onsubmit({ preventDefault() {}, stopPropagation() {} });
      const puts = calls.filter((c) => c.method === "PUT");
      assert.equal(puts.length, 1, `${outcome}: one save`);
      assert.deepEqual(puts[0].body, { main: null, worker: { provider: "local", model: "model-w" } });
      host.remove();
    }
  },
  // I1: settings hand Back and Esc to the caller's onBack.
  async on_back() {
    assert.equal(typeof ui.openConnectDialog, "function", "openConnectDialog missing");
    const api = async (path) => {
      if (path.startsWith("/api/chat-settings")) return { front: { provider: "none" }, local_models: [], hosted: [] };
      if (path === "/api/connections") return { connections: [] };
      if (path.endsWith("/cards")) return { cards: [], linked: [] };
      if (path.startsWith("/api/chat/")) return path.endsWith("/status") ? { orchestrator: null } : { messages: [], replies: [] };
      return { ...project, limits: { max_agents: 3 }, models: {} };
    };
    for (const trigger of ["link", "escape"]) {
      const host = doc.createElement("div"); doc.body.append(host);
      const backs = [];
      await ui.mountProject(host, { slug: "ledger", api, e, settings: true, origin: "#/board/ledger/chat", onBack: (href) => backs.push(href) });
      const link = host.querySelector("[data-project-back]");
      assert.ok(link, "settings draw the back link");
      assert.equal(link.textContent, "Back to the chat");
      if (trigger === "link") link.click();
      else assert.equal(doc.key("Escape").defaultPrevented, true, "settings take Escape");
      assert.deepEqual(backs, ["#/board/ledger/chat"], `${trigger}: onBack runs with the origin instead of the default navigation`);
      host.remove();
      doc.key("Escape");
    }
  },
};

assert.ok(process.argv[2] in cases, "unknown case");
await cases[process.argv[2]]();
say(`PASS ${process.argv[2]}`);
