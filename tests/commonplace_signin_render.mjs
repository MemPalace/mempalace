import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import nodeTest from "node:test";
import { createContext, runInContext } from "node:vm";

const selection = process.env.SIGNIN_CASE || "all";
const test = (name, fn) => {
  if (selection === "all" || (selection === "guards" ? !name.startsWith("RED ") : name.startsWith(`RED ${selection}:`))) nodeTest(name, fn);
};
const url = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const read = (name) => readFile(new URL(`../commonplace/web/static/${name}`, import.meta.url), "utf8");
const apiUrl = url(await read("api.js"));
const { escape } = await import(apiUrl);
const { signinView, signinMessage } = await import(url((await read("signin.js")).replace('"./api.js"', `"${apiUrl}"`)));
const appSource = (await read("app.js")).replace(/^import .*;\n/gm, "");
const providers = [
  ["google", "Google"], ["github", "GitHub"], ["apple", "Apple"], ["x", "X"], ["discord", "Discord"],
].map(([id, name]) => ({ id, name, url: `https://app.example/app/auth/${id}` }));
const emailForm = /<form data-form="email-login" action="\/app\/auth\/email" method="post">[\s\S]*?<\/form>/;

const legal = '<nav class="legal-links" aria-label="Legal"><a href="https://cmmn.dev/privacy">Privacy</a><a href="https://cmmn.dev/terms">Terms</a></nav>';

test("no providers preserves the email form and legal navigation without a message", () => {
  const html = signinView({});
  assert.match(html, /<h1>Sign in<\/h1>/);
  assert.match(html, emailForm);
  assert.ok(html.includes(legal));
  assert.doesNotMatch(html, /role="status"|class="provider"|Create your workspace|Your name/);
  for (const field of ['name="email"', 'type="email"', 'autocomplete="email"', 'maxlength="254"', 'required', '<input type="hidden" name="next" value="">', 'Email a sign-in link']) assert.ok(html.includes(field));
});

test("provider buttons precede the email form and use the supplied marks", () => {
  const html = signinView({ providers });
  assert.equal((html.match(/<a class="provider"/g) || []).length, 5);
  assert.equal((html.match(/<svg [^>]*aria-hidden="true"/g) || []).length, 5);
  assert.equal((html.match(/width="18" height="18"/g) || []).length, 5);
  assert.equal((html.match(/fill="currentColor"/g) || []).length, 4);
  assert.equal((html.match(/viewBox="0 0 24 24"/g) || []).length, 4);
  assert.match(html, /viewBox="0 0 48 48"/);
  for (const color of ["#EA4335", "#4285F4", "#FBBC05", "#34A853"]) assert.ok(html.includes(`fill="${color}"`));
  const buttons = [...html.matchAll(/<a class="provider" href="([^"]+)">.*?<span>Continue with (.*?)<\/span><\/a>/g)];
  assert.deepEqual(buttons.map((m) => [m[1], m[2]]), providers.map((p) => [p.url, p.name]));
  assert.ok(html.indexOf("</nav>") < html.search(emailForm));
  assert.match(html, /aria-label="Commonplace"/);
  assert.doesNotMatch(html.replace('<img class="brand-v2" src="/app/brand/logo-small-dark.svg" alt="Commonplace">', ""), /\sstyle=|xmlns=|<script|<style|<img|\son\w+=/);
});

test("provider names, URLs, messages and destinations cannot inject markup", () => {
  const attack = '"><script>alert("x")</script>&\'';
  const next = `/oauth/authorize?client_id=test&redirect_uri=${attack}`;
  const html = signinView({ providers: [{ id: attack, name: attack, url: `https://app.example/${attack}` }], error: attack, next });
  assert.ok(html.includes(`<span>Continue with ${escape(attack)}</span>`));
  assert.ok(html.includes(`href="${escape(`https://app.example/${attack}?next=${encodeURIComponent(next)}`)}"`));
  assert.ok(html.includes(`<div class="message" role="status">${escape(attack)}</div>`));
  assert.doesNotMatch(html, /<script|<style|<svg|\sstyle=/);
});

test("next is encoded once and appends to a provider URL's existing query", () => {
  const next = "/app/#/card/42?title=a & b";
  const html = signinView({ providers, next });
  for (const p of providers) assert.ok(html.includes(`href="${p.url}?next=${encodeURIComponent(next)}"`));
  const queried = signinView({ providers: [{ ...providers[0], url: `${providers[0].url}?prompt=account` }], next });
  assert.ok(queried.includes(`?prompt=account&amp;next=${encodeURIComponent(next)}"`));
  assert.doesNotMatch(signinView({ providers }), /[?&]next=/);
});

test("messages match the contract and unknown codes never get reflected", () => {
  const messages = {
    expired: "Sign-in expired. Try again.", cancelled: "Sign-in cancelled.", failed: "Sign-in failed. Try again.",
    email: "That account has no verified email.", account: "No account for that email.", disabled: "This account can't sign in.",
  };
  for (const [code, message] of Object.entries(messages)) assert.equal(signinMessage(code), message);
  for (const code of ["", "unknown", "<script>alert(1)</script>", "toString", "__proto__", null, undefined]) {
    assert.equal(signinMessage(code), messages.failed);
  }
});

const settle = () => new Promise((resolve) => setImmediate(resolve));
function app({ search = "", pathname = "/app/", hash = "", cookie = "", loadProviders = () => ({ providers }), me, answers = {}, elements = {} } = {}) {
  const listeners = new Map(), rootListeners = new Map(), calls = [], cleared = [], replacements = [];
  const voice = { resets: 0, stops: 0, reset() { this.resets++; }, stop() { this.stops++; }, load() {} };
  const root = { innerHTML: "", addEventListener: (name, handler) => rootListeners.set(name, handler), querySelector: (selector) => elements[selector] || null, querySelectorAll: () => [] };
  const location = { search, pathname, hash };
  const history = { state: { retained: true }, replaceState(state, unused, path) {
    replacements.push({ state, path });
    const next = new URL(path, "https://app.example");
    Object.assign(location, { search: next.search, pathname: next.pathname, hash: next.hash });
  } };
  const document = { cookie, querySelector: () => root, addEventListener: (name, handler) => listeners.set(name, handler), createElement: () => ({ setAttribute(name, value) { this[name] = value; } }) };
  const context = createContext({
    document, location, history, URLSearchParams, signinView, signinMessage, e: escape, calls,
    FormData: class extends Array { constructor(form) { super(...Object.entries(form.payload)); } },
    setPeople() {},
    window: { addEventListener() {} },
    digestSpeech: () => ({ stop() {} }),
    voiceInput: () => voice,
    rulesHandlers: () => ({ stale: false, force: false, busy: () => false, update() {} }),
    // Imports are stripped: sign-out clears the signed-in user's chat seen ids through this.
    clearSeen: (user) => cleared.push(user),
    setInterval() {}, setTimeout() {}, clearTimeout() {},
    async api(path) {
      calls.push(path);
      if (path === "/app/logout") { assert.equal(voice.stops, 1); return {}; }
      if (path === "/api/me") {
        if (me !== undefined) return me;
        listeners.get("session-expired")();
        throw Object.assign(new Error("Unauthorized"), { status: 401 });
      }
      if (Object.hasOwn(answers, path)) return answers[path];
      assert.equal(path, "/app/auth/providers");
      return loadProviders();
    },
  });
  const source = me === undefined ? appSource : appSource.replace("async function route() {", 'async function route() { calls.push("route:" + location.hash); return; } async function __route() {');
  runInContext(source, context);
  return { root, calls, cleared, replacements, location, context, voice, submit: (form) => rootListeners.get("submit")({ preventDefault() {}, target: { closest: () => form } }), expire: () => listeners.get("session-expired")(), logout: () => rootListeners.get("click")({ target: { closest: () => ({ dataset: { action: "logout" } }) } }) };
}

test("email request inserts a visible account-neutral confirmation after the form", async () => {
  const message = "If this email can sign in, a link is on its way.";
  const page = app({ answers: { "/app/auth/email": { message } } });
  await settle();
  const inserted = [];
  await page.submit({
    dataset: { form: "email-login" }, payload: { email: "reader@example.test", next: "" },
    querySelector: () => null, after: (element) => inserted.push(element),
  });
  assert.equal(page.calls.at(-1), "/app/auth/email");
  assert.equal(inserted.length, 1);
  assert.equal(inserted[0].role, "status");
  assert.equal(inserted[0].textContent, message);
});

test("boot maps callback errors, clears auth parameters and caches providers across expiry", async () => {
  const next = "/oauth/authorize?client_id=test&scope=memory:read";
  const page = app({ search: `?keep=yes&signin=email&next=${encodeURIComponent(next)}`, hash: "#/card/9" });
  await settle();
  assert.deepEqual(page.replacements, [{ state: { retained: true }, path: "/app/?keep=yes#/card/9" }]);
  assert.ok(page.root.innerHTML.includes("That account has no verified email."));
  assert.ok(page.root.innerHTML.includes(`?next=${encodeURIComponent(next)}`));
  assert.deepEqual(page.calls, ["/api/me", "/app/auth/providers"]);
  page.expire();
  await settle();
  assert.equal(page.calls.length, 2);
  assert.doesNotMatch(page.root.innerHTML, /role="status"/);
});

test("boot treats empty and unrecognized callback codes as failed", async () => {
  for (const code of ["", "<script>secret</script>", "toString"]) {
    const page = app({ search: `?signin=${encodeURIComponent(code)}` });
    await settle();
    assert.ok(page.root.innerHTML.includes("Sign-in failed. Try again."));
    assert.doesNotMatch(page.root.innerHTML, /secret|<script>|toString/);
    assert.equal(page.location.search, "");
  }
});

test("provider request failure leaves email sign-in usable and is cached", async () => {
  const page = app({ loadProviders: () => { throw new Error("Unavailable"); } });
  await settle();
  assert.equal(page.root.innerHTML, signinView({}));
  assert.equal(page.replacements.length, 0);
  page.expire();
  await settle();
  assert.deepEqual(page.calls, ["/api/me", "/app/auth/providers"]);
});

test("destinations fall back to the current path and hash, except the app root", async () => {
  const home = app();
  await settle();
  assert.doesNotMatch(home.root.innerHTML, /[?&]next=/);
  const page = app({ pathname: "/app/board/work", hash: "#/card/7" });
  await settle();
  assert.ok(page.root.innerHTML.includes(`?next=${encodeURIComponent("/app/board/work#/card/7")}`));
  page.location.hash = "#/card/8";
  page.expire();
  await settle();
  assert.ok(page.root.innerHTML.includes(`?next=${encodeURIComponent("/app/board/work#/card/8")}`));
});

test("a late provider response cannot replace an authenticated screen", async () => {
  let resolve;
  const page = app({ loadProviders: () => new Promise((done) => { resolve = done; }) });
  await settle();
  runInContext('state.me = { user: "test-user" };', page.context);
  page.root.innerHTML = "Authenticated screen";
  resolve({ providers });
  await settle();
  assert.equal(page.root.innerHTML, "Authenticated screen");
});

test("session expiry resets voice before the provider request finishes", async () => {
  let resolve;
  const page = app({ loadProviders: () => new Promise((done) => { resolve = done; }) });
  await settle();
  const resets = page.voice.resets;
  assert.ok(resets > 0);
  page.expire();
  assert.equal(page.voice.resets, resets + 1);
  resolve({ providers });
  await settle();
  assert.ok(page.root.innerHTML.includes('class="signin"'));
});

test("sign-out stops voice before the request and resets it on return to sign-in", async () => {
  const page = app();
  await settle();
  const resets = page.voice.resets;
  runInContext('state.me = { user: "test-user" }; state.drafts.set("message", "draft");', page.context);
  await page.logout();
  assert.equal(page.calls.at(-1), "/app/logout");
  assert.deepEqual(page.cleared, ["test-user"]);
  assert.equal(page.voice.stops, 1);
  assert.equal(page.voice.resets, resets + 1);
  assert.equal(runInContext("state.me === null && state.drafts.size === 0", page.context), true);
  assert.ok(page.root.innerHTML.includes('class="signin"'));
});

const paused = [{ id: "x", name: "X", until: "2026-11-01T00:00:00Z" }];
const localTime = new Date(paused[0].until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const pauseNotice = `Sign-in with X is paused until ${localTime}. Use another option with the same email.`;

test("paused code uses the provider name and local reset time, or the fallback", () => {
  assert.equal(signinMessage("paused", paused), pauseNotice);
  assert.equal(signinMessage("paused"), "This sign-in option is paused until tomorrow. Use another option with the same email.");
  assert.ok(signinView({ error: signinMessage("paused", paused) }).includes(escape(pauseNotice)));
});

test("returning users see one proactive notice only for their paused provider", () => {
  const html = signinView({ providers: providers.filter((p) => p.id !== "x"), paused, via: "x" });
  assert.ok(html.includes(escape(pauseNotice)));
  assert.equal((html.match(/role="status"/g) || []).length, 1);
  assert.doesNotMatch(html, /Continue with X/);
  for (const via of ["", "github", "unknown"]) assert.doesNotMatch(signinView({ paused, via }), /role="status"/);
  const explicit = signinView({ paused, via: "x", error: signinMessage("expired") });
  assert.ok(explicit.includes("Sign-in expired. Try again."));
  assert.doesNotMatch(explicit, /paused until/);
  assert.equal((explicit.match(/role="status"/g) || []).length, 1);
});

test("paused provider names and formatted times are escaped", () => {
  const attack = '<script>alert("x")</script>';
  const evil = [{ ...paused[0], name: attack }];
  const html = signinView({ paused: evil, via: "x" });
  assert.ok(html.includes(escape(attack)));
  assert.doesNotMatch(html, /<script>/);
  const format = Date.prototype.toLocaleTimeString;
  try {
    Date.prototype.toLocaleTimeString = () => attack;
    const formatted = signinView({ paused, via: "x" });
    assert.ok(formatted.includes(escape(attack)));
    assert.doesNotMatch(formatted, /<script>/);
  } finally {
    Date.prototype.toLocaleTimeString = format;
  }
});

test("boot reads only a validated provider hint and renders paused inventory", async () => {
  for (const cookie of ["cmmn_via=x", "other=value; cmmn_via=x; another=value"]) {
    const page = app({ cookie, loadProviders: () => ({ providers: [], paused }) });
    await settle();
    assert.ok(page.root.innerHTML.includes(escape(pauseNotice)));
  }
  for (const value of ["github", "X", "x%00", "x=", "x<script>", "x".repeat(21)]) {
    const entry = [{ ...paused[0], id: value }];
    const page = app({ cookie: `cmmn_via=${value}`, loadProviders: () => ({ paused: value === "github" ? paused : entry }) });
    await settle();
    assert.doesNotMatch(page.root.innerHTML, /role="status"/);
  }
});

test("boot resolves paused code after providers load and explicit errors win", async () => {
  const options = { cookie: "cmmn_via=x", loadProviders: () => ({ providers: [], paused }) };
  const page = app({ ...options, search: "?signin=paused" });
  await settle();
  assert.ok(page.root.innerHTML.includes(escape(pauseNotice)));
  assert.equal((page.root.innerHTML.match(/role="status"/g) || []).length, 1);
  const explicit = app({ ...options, search: "?signin=cancelled" });
  await settle();
  assert.ok(explicit.root.innerHTML.includes("Sign-in cancelled."));
  assert.doesNotMatch(explicit.root.innerHTML, /paused until/);
});

test("R1: sign-in preserves the tap workspace and card for every provider and email", async () => {
  const next = "/app/?w=W#/card/5";
  const page = app({ search: "?w=W", hash: "#/card/5" });
  await settle();
  assert.deepEqual(page.calls, ["/api/me", "/app/auth/providers"]);
  for (const provider of providers) {
    assert.ok(page.root.innerHTML.includes(`href="${provider.url}?next=${encodeURIComponent(next)}"`));
  }
  assert.ok(page.root.innerHTML.includes(`<input type="hidden" name="next" value="${escape(next)}">`));
});

async function tap(workspace) {
  const error = { hidden: true, innerHTML: "" };
  const page = app({ search: "?w=W", hash: "#/card/5", me: { workspace }, elements: { "#screen-error": error } });
  await settle();
  assert.ok(page.calls.includes("/api/me"));
  assert.equal(page.calls.filter((call) => call.startsWith("route:")).length, 1);
  return { ...page, error };
}

test("R2: a foreign workspace tap routes home and shows the notice after routing", async () => {
  const page = await tap("W2");
  assert.ok(page.calls.includes("route:#/"));
  assert.equal(page.replacements.at(-1)?.path, "/app/#/");
  assert.equal(page.error.hidden, false);
  assert.ok(page.error.innerHTML.includes("This card is in another workspace. Sign out, then sign in again to open it."));
});

test("R3a: a matching workspace tap keeps the card route", async () => {
  const page = await tap("W");
  assert.ok(page.calls.includes("route:#/card/5"));
});

test("R3b: a matching workspace tap removes the workspace query", async () => {
  const page = await tap("W");
  assert.ok(page.calls.includes("route:#/card/5"));
  assert.equal(page.replacements.at(-1)?.path, "/app/#/card/5");
});

test("R4: phone notifications render the four states with escaped values and acceptance labels", async () => {
  const page = app();
  await settle();
  assert.equal(runInContext("typeof phonePushView", page.context), "function");
  const view = (data) => {
    page.context.phonePushData = data;
    const html = runInContext("phonePushView(phonePushData)", page.context);
    assert.doesNotMatch(html, /deliver|type="checkbox"|>Save<|needs_you|blocked|category/i);
    return html;
  };
  const home = "Phone notifications cover only your home workspace. Cards in other workspaces never notify this phone.";
  const pause = "Notifications pause while you are signed out everywhere, including when a sign-in expires after 30 days. Nothing you miss is sent later.";
  const base = { available: true, eligible: true, subscription: null, test: null };
  const attack = '<script>"&\'topic</script>';
  const subscription = { server: `https://ntfy.example/${attack}`, topic: attack };
  const unavailable = view({ ...base, available: false });
  assert.ok(unavailable.includes("Not set up on this server."));
  assert.doesNotMatch(unavailable, />Turn on<|>Turn off</);
  const unavailableOn = view({ ...base, available: false, subscription });
  assert.ok(unavailableOn.includes(">Turn off<"));
  assert.doesNotMatch(unavailableOn, />Turn on<|>Test<|<script>/);
  const ineligible = view({ ...base, eligible: false });
  assert.ok(ineligible.includes(home));
  assert.doesNotMatch(ineligible, />Turn on<|>Turn off</);
  const off = view(base);
  assert.ok(off.includes(">Turn on<"));
  assert.ok(off.includes(pause));
  assert.doesNotMatch(off, />Turn off</);
  for (const [status, label] of [["accepted", "Accepted by your ntfy server"], ["pending", "Waiting"], ["not_accepted", "Not accepted"], [null, ""]]) {
    const on = view({ ...base, subscription, test: status ? { status } : null });
    for (const text of [escape(subscription.server), escape(subscription.topic), ">Test<", ">Turn off<", home, pause, label]) assert.ok(on.includes(text));
    assert.equal((on.match(/>Copy</g) || []).length, 1);
    assert.doesNotMatch(on, /<script>|>Turn on</);
    if (status === null) assert.doesNotMatch(on, /role="status"|Not accepted|Waiting|Accepted by your ntfy server/);
  }
});

async function onboarding() {
  const path = new URL("../commonplace/web/static/onboarding.js", import.meta.url);
  assert.ok(existsSync(path), "onboarding module missing");
  const module = await import(url((await readFile(path, "utf8")).replace('"./api.js"', `"${apiUrl}"`)));
  assert.equal(typeof module.onboardingView, "function");
  assert.equal(typeof module.mountOnboarding, "function");
  return module;
}

test("RED dialog: project creation is a labelled dialog and sign-in uses the approved panes", async () => {
  const { onboardingView } = await onboarding();
  const html = onboardingView({ first: true });
  assert.match(html, /<dialog[^>]*role="dialog"/);
  assert.match(html, /aria-labelledby="create-project-heading"/);
  assert.match(html, /id="create-project-heading">Create your first project/);
  const signin = signinView({});
  for (const selector of ['class="auth-l"', 'class="auth-r"', 'What happens next', 'Agent', 'Orchestrator']) assert.ok(signin.includes(selector));
  assert.doesNotMatch(signin, /Create your workspace|Your name/);
  const expired = signinView({ error: signinMessage("expired") });
  assert.match(expired, /<h1>Get a new link<\/h1>/);
  assert.match(expired, /Email me a new link/);
});

test("RED modal: project dialog is modal and supports focus, cancel and cleanup", async () => {
  const { onboardingView, mountOnboarding } = await onboarding();
  assert.match(onboardingView(), /aria-modal="true"/);
  const dom = dialogDOM();
  const ui = mountOnboarding(dom.mount, { opener: dom.opener, onClose: () => dom.events.push("closed") });
  assert.equal(typeof ui.close, "function");
  assert.deepEqual(dom.events, ["showModal", "focus:name"]);
  dom.mount.ownerDocument.activeElement = dom.submit;
  await dom.emit("keydown", { key: "Tab", shiftKey: false });
  assert.equal(dom.events.at(-1), "focus:name");
  dom.mount.ownerDocument.activeElement = dom.name;
  await dom.emit("keydown", { key: "Tab", shiftKey: true });
  assert.equal(dom.events.at(-1), "focus:submit");
  await dom.emit("click", { target: { closest: () => dom.cancel } });
  assert.deepEqual(dom.events.slice(-3), ["remove", "focus:opener", "closed"]);
  ui.close();
  assert.equal(dom.events.filter((e) => e === "closed").length, 1);
  const escapeDOM = dialogDOM();
  mountOnboarding(escapeDOM.mount, { opener: escapeDOM.opener });
  await escapeDOM.emit("cancel");
  assert.deepEqual(escapeDOM.events.slice(-2), ["remove", "focus:opener"]);
});

test("RED create: Create project posts only the name and slug, locks submission and recovers errors", async () => {
  const { onboardingView, mountOnboarding } = await onboarding();
  const html = onboardingView({ name: '<script>"&' });
  assert.match(html, /type="submit"[^>]*>Create project/);
  assert.match(html, /name="name"[^>]*required/);
  assert.ok(html.includes(escape('<script>"&')));
  assert.doesNotMatch(html, /What is it for|<textarea|<script>/);
  assert.match(onboardingView({ first: false }), /id="create-project-heading">Create project/);
  const dom = dialogDOM();
  const calls = [], created = [];
  let resolve;
  mountOnboarding(dom.mount, {
    api: async (path, options) => { calls.push({ path, options }); return new Promise((done) => { resolve = done; }); },
    onCreated: (board) => created.push(board), opener: dom.opener,
  });
  dom.name.value = "   ";
  await dom.emit("submit");
  assert.equal(calls.length, 0);
  dom.name.value = " Atlas Launch ";
  const pending = dom.emit("submit");
  assert.equal(dom.submit.disabled, true);
  await dom.emit("submit");
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], { path: "/api/boards", options: { method: "POST", body: { name: "Atlas Launch", slug: "atlas-launch" } } });
  resolve({ slug: "atlas-launch", name: "Atlas Launch" });
  await pending;
  assert.deepEqual(created, [{ slug: "atlas-launch", name: "Atlas Launch" }]);
  assert.ok(dom.events.includes("remove"));
  const failed = dialogDOM();
  mountOnboarding(failed.mount, { api: async () => { throw new Error("private detail"); } });
  await failed.emit("submit");
  assert.equal(failed.submit.disabled, false);
  assert.equal(failed.error.hidden, false);
  assert.match(failed.error.textContent, /Could not create/);
  assert.doesNotMatch(failed.error.textContent, /private detail/);
  assert.ok(!failed.events.includes("remove"));
});

function dialogDOM() {
  const events = [], handlers = new Map();
  const name = { value: "Atlas", focus: () => events.push("focus:name") };
  const submit = { disabled: false, focus: () => events.push("focus:submit") };
  const cancel = {};
  const error = { hidden: true, textContent: "" };
  const dialog = {
    open: false,
    querySelectorAll: () => [name, cancel, submit],
    showModal() { this.open = true; events.push("showModal"); },
    close() { this.open = false; },
    addEventListener: (kind, fn) => handlers.set(kind, fn),
    querySelector: (selector) => ({ '[name="name"]': name, '[type="submit"]': submit, '[role="alert"]': error })[selector],
  };
  const host = { innerHTML: "", querySelector: () => dialog, remove: () => events.push("remove") };
  const mount = { ownerDocument: { activeElement: null, createElement: () => host }, append() {} };
  const opener = { isConnected: true, focus: () => events.push("focus:opener") };
  return { mount, opener, events, name, submit, cancel, error, async emit(kind, props = {}) {
    assert.equal(typeof handlers.get(kind), "function", `${kind} handler missing`);
    return handlers.get(kind)({ preventDefault() {}, stopPropagation() {}, ...props });
  } };
}
