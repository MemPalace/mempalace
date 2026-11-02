import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { createContext, runInContext } from "node:vm";

const url = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const read = (name) => readFile(new URL(`../commonplace/web/static/${name}`, import.meta.url), "utf8");
const apiUrl = url(await read("api.js"));
const { escape } = await import(apiUrl);
const { signinView, signinMessage } = await import(url((await read("signin.js")).replace('"./api.js"', `"${apiUrl}"`)));
const appSource = (await read("app.js")).replace(/^import .*;\n/gm, "");
const providers = [
  ["google", "Google"], ["github", "GitHub"], ["apple", "Apple"], ["x", "X"], ["discord", "Discord"],
].map(([id, name]) => ({ id, name, url: `https://app.example/app/auth/${id}` }));
const emailForm = '<form data-form="email-login" action="/app/auth/email" method="post"><input type="hidden" name="next" value=""><input name="email" type="email" aria-label="Email" placeholder="Email" autocomplete="email" maxlength="254" required><button class="blockbtn">Email a sign-in link</button></form>';
const brand = '<a class="brand-home" href="/app/" aria-label="Commonplace"><img class="brand-v2" src="/app/brand/logo-small-dark.svg" alt="Commonplace"></a>';

test("no providers preserves the email form without a nav or message", () => {
  assert.equal(signinView({}), `<main class="signin">${brand}<h1>Sign in</h1>${emailForm}</main>`);
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
  assert.ok(html.indexOf("</nav>") < html.indexOf(emailForm));
  assert.ok(html.includes(brand));
  assert.doesNotMatch(html.replace(brand, ""), /\sstyle=|xmlns=|<script|<img|\son\w+=/);
});

test("provider names, URLs, messages and destinations cannot inject markup", () => {
  const attack = '"><script>alert("x")</script>&\'';
  const next = `/oauth/authorize?client_id=test&redirect_uri=${attack}`;
  const html = signinView({ providers: [{ id: attack, name: attack, url: `https://app.example/${attack}` }], error: attack, next });
  assert.ok(html.includes(`<span>Continue with ${escape(attack)}</span>`));
  assert.ok(html.includes(`href="${escape(`https://app.example/${attack}?next=${encodeURIComponent(next)}`)}"`));
  assert.ok(html.endsWith(`<div class="message" role="status">${escape(attack)}</div></main>`));
  assert.doesNotMatch(html, /<script|<svg|\sstyle=/);
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
function app({ search = "", pathname = "/app/", hash = "", cookie = "", loadProviders = () => ({ providers }) } = {}) {
  const listeners = new Map(), rootListeners = new Map(), calls = [], replacements = [];
  const voice = { resets: 0, stops: 0, reset() { this.resets++; }, stop() { this.stops++; } };
  const root = { innerHTML: "", addEventListener: (name, handler) => rootListeners.set(name, handler), querySelector: () => null, querySelectorAll: () => [] };
  const location = { search, pathname, hash };
  const history = { state: { retained: true }, replaceState(state, unused, path) {
    replacements.push({ state, path });
    const next = new URL(path, "https://app.example");
    Object.assign(location, { search: next.search, pathname: next.pathname, hash: next.hash });
  } };
  const document = { cookie, querySelector: () => root, addEventListener: (name, handler) => listeners.set(name, handler) };
  const context = createContext({
    document, location, history, URLSearchParams, signinView, signinMessage, e: escape,
    window: { addEventListener() {} },
    digestSpeech: () => ({ stop() {} }),
    voiceInput: () => voice,
    rulesHandlers: () => ({ stale: false, force: false, busy: () => false, update() {} }),
    setInterval() {}, setTimeout() {}, clearTimeout() {},
    async api(path) {
      calls.push(path);
      if (path === "/app/logout") { assert.equal(voice.stops, 1); return {}; }
      if (path === "/api/me") {
        listeners.get("session-expired")();
        throw Object.assign(new Error("Unauthorized"), { status: 401 });
      }
      assert.equal(path, "/app/auth/providers");
      return loadProviders();
    },
  });
  runInContext(appSource, context);
  return { root, calls, replacements, location, context, voice, expire: () => listeners.get("session-expired")(), logout: () => rootListeners.get("click")({ target: { closest: () => ({ dataset: { action: "logout" } }) } }) };
}

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
