import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";

const source = await readFile(new URL("../commonplace/web/static/voice.js", import.meta.url), "utf8");
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const { languagePicker, pcm16, insertTranscript, voiceInput } = await import(`data:text/javascript;base64,${Buffer.from(source.replace(/^import[^\n]+\n/, `const e = ${e.toString()};\n`)).toString("base64")}`);
const saved = new Map(), storage = { getItem: (k) => saved.get(k), setItem: (k, v) => saved.set(k, v) };
const picker = languagePicker(storage);
assert.equal(picker.get(), "en"); picker.set("nl");
assert.equal(languagePicker(storage).get(), "nl"); picker.set("de");
assert.equal(picker.get(), "nl");
assert.equal(languagePicker({ getItem() { throw Error(); } }).get(), "en");
languagePicker({ setItem() { throw Error(); } }).set("nl");

const shorts = (buffer) => Array.from({ length: buffer.byteLength / 2 }, (_, i) => new DataView(buffer).getInt16(i * 2, true));
assert.deepEqual(shorts(pcm16(new Float32Array([-2, -1, 0, .5, 1, 2]), 16000)), [-32768, -32768, 0, 16384, 32767, 32767]);
assert.deepEqual(shorts(pcm16(new Float32Array([1, 0, -1, .5, .5, .5]), 48000)), [0, 16384]);
assert.deepEqual(shorts(pcm16(new Float32Array([1, -1, 0]), 24000)), [10922, -10923]);
assert.equal(pcm16(new Float32Array(44100).fill(.5), 44100).byteLength, 32000);
assert.equal(pcm16(new Float32Array(16000 * 61), 16000).byteLength, 1920000);
assert.equal(pcm16(new Float32Array(), 48000).byteLength, 0);
assert.deepEqual(Array.from(new Uint8Array(pcm16(new Float32Array([-1, 1]), 16000))), [0, 128, 255, 127]);

function field(value, caret = value.length) {
  return {
    value, selectionStart: caret, selectionEnd: caret, events: [], dataset: { draft: "space-work" },
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
    dispatchEvent(event) { this.events.push(event); },
  };
}
let input = field("helloWORLD", 5);
input.selectionEnd = 10;
insertTranscript(input, 'Café  "yes" <script>');
assert.equal(input.value, 'hello Café  "yes" <script> WORLD');
assert.equal(input.events.length, 1); assert.equal(input.events[0].type, "input"); assert.equal(input.events[0].bubbles, true);
assert.equal(input.selectionStart, input.value.indexOf("WORLD"));
for (const [before, text, expected] of [["", "yes", "yes"], ["hello ", "yes", "hello yes"], ["hello\n", "yes", "hello\nyes"], ["hello", "", "hello"]]) {
  input = field(before); insertTranscript(input, text); assert.equal(input.value, expected);
}
input = field("hello world", 5); insertTranscript(input, "say"); assert.equal(input.value, "hello say world");

// Exercise the real controller with replaceable inputs and a deterministic
// capture graph, timer, permission prompt, and HTTP response. No model download.
function fixture({ owner = true } = {}) {
  const handlers = {}, timers = new Map(), errors = [], requests = [];
  let nextTimer = 0, current = field("helloWORLD", 5), control = null, stopped = 0, closed = 0, node, permission, response;
  const document = { activeElement: null, createElement: () => ({ dataset: {}, setAttribute(name, value) { this[name] = value; }, remove() { control = null; } }), dispatchEvent(event) { this.event = event; } };
  const form = { querySelector(selector) {
    if (selector === ".voice-control") return control;
    if (selector === "[data-draft]") return current;
    if (selector.startsWith("button:not")) return { before(element) { control = element; } };
    return null;
  } };
  const bind = (f) => { f.form = form; f.closest = (selector) => selector === "#card-panel, #content" ? { id: "content" } : form; return f; };
  bind(current);
  let settingsRow = null;
  const settingsButton = { setAttribute(name, value) { this[name] = value; } };
  const settingsPage = {
    querySelector: () => settingsRow,
    insertAdjacentHTML(position, markup) { assert.equal(position, "afterbegin"); assert.match(markup, /data-voice-settings/); settingsRow = { remove() { settingsRow = null; } }; },
  };
  const root = {
    querySelector: (selector) => selector === ".settings" ? settingsPage : null,
    querySelectorAll(selector) { return selector.startsWith("form[") ? [current] : settingsRow ? [settingsButton] : []; },
    addEventListener(name, callback) { handlers[name] = callback; },
  };
  const browser = {
    document, localStorage: storage, Event, AbortController,
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    navigator: { mediaDevices: { getUserMedia: () => permission ? permission : Promise.resolve({ getTracks: () => [{ stop() { stopped++; } }] }) } },
    AudioContext: class {
      constructor() { this.sampleRate = 48000; this.state = "running"; this.audioWorklet = { addModule: async (path) => assert.equal(path, "/app/voice-worklet.js") }; }
      async resume() {}
      async close() { this.state = "closed"; closed++; }
      createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    },
    AudioWorkletNode: class {
      constructor(context, name) { assert.equal(name, "commonplace-voice"); node = this; this.port = { postMessage: (data) => { assert.equal(data, "stop"); this.port.onmessage({ data: new Float32Array([.5, .5, .5]) }); this.port.onmessage({ data: "stopped" }); } }; }
      connect() {} disconnect() {}
    },
    async fetch(path, options) { requests.push({ path, ...options }); return response ? response : { ok: true, status: 200, json: async () => ({ text: "said  verbatim" }) }; },
  };
  const configs = [];
  const api = async (path, options) => {
    configs.push([path, options]);
    // A partial PATCH response must retain availability, languages and ownership.
    return options ? { enabled: options.body.enabled } : { available: true, enabled: true, languages: ["en", "nl"], owner };
  };
  const voice = voiceInput(root, api, (text) => errors.push(text), browser);
  const press = async (action, language) => handlers.click({ preventDefault() {}, target: { closest: () => ({ dataset: { voice: action, language }, closest: () => form }) } });
  return {
    voice, press, errors, requests, configs, timers, browser, root, settingsButton,
    get settingsRow() { return settingsRow; },
    get input() { return current; }, get control() { return control; }, get stopped() { return stopped; }, get closed() { return closed; }, get node() { return node; },
    rerender() { voice.remember(); current = bind(field(current.value, 0)); control = null; voice.view(); },
    permission(promise) { permission = promise; }, response(promise) { response = promise; },
  };
}
const f = fixture();
assert.equal(f.voice.settings(), "");
assert.equal(f.settingsRow, null);
await f.voice.load(); assert.equal(f.configs.length, 1);
assert.match(f.voice.settings(), /<span>Voice<\/span>/);
assert.ok(f.settingsRow); // A late status response installs the row in Settings.
await f.press("enabled");
assert.ok(f.settingsRow); assert.equal(f.settingsButton.textContent, "Off");
assert.match(f.voice.settings(), /aria-pressed="false"/);
await f.press("enabled");
assert.ok(f.settingsRow); assert.equal(f.settingsButton.textContent, "On");
assert.match(f.voice.settings(), /aria-pressed="true"/);
const member = fixture({ owner: false }); await member.voice.load();
assert.equal(member.voice.settings(), ""); assert.equal(member.settingsRow, null);
assert.equal(f.control.dataset.state, "idle");
assert.match(f.control.innerHTML, /<svg[^>]+fill="currentColor"/);
assert.match(f.control.innerHTML, /<path d="M9 2h6v10H9z"\/>/);
assert.equal(Object.hasOwn(f.control.dataset, "ui"), false);
await f.press("language"); assert.match(f.control.innerHTML, /role="menu"/);
await f.press("choose", "en"); assert.equal(languagePicker(storage).get(), "en");
await f.press("mic"); assert.equal(f.control.dataset.state, "recording");
assert.match(f.control.innerHTML, /Stop recording/);
assert.equal([...f.timers.values()].filter((t) => t.delay === 60000).length, 1);
f.rerender(); assert.equal(f.control.dataset.state, "recording"); assert.equal(f.input.selectionStart, 5);
let respond;
f.response(new Promise((resolve) => { respond = resolve; }));
const transcribing = f.press("mic"); await new Promise(setImmediate);
assert.equal(f.control.dataset.state, "transcribing"); assert.equal(f.control["aria-busy"], "true");
assert.equal(f.stopped, 1); assert.equal(f.closed, 1);
f.input.value = "typed while waiting"; f.input.setSelectionRange(5, 5);
f.rerender(); assert.equal(f.control.dataset.state, "transcribing"); assert.equal(f.input.selectionStart, 5);
respond({ ok: true, status: 200, json: async () => ({ text: 'Café  "yes" <script>' }) });
await transcribing;
assert.equal(f.input.value, 'typed Café  "yes" <script> while waiting');
assert.equal(f.input.events[0].type, "input"); assert.equal(f.requests[0].method, "POST");
assert.equal(f.requests[0].path, "/api/voice/transcribe?lang=en");
assert.equal(f.requests[0].headers["Content-Type"], "application/octet-stream");
assert.deepEqual(shorts(f.requests[0].body), [16384]);
assert.equal(f.control.dataset.state, "idle");
assert.equal(f.errors.length, 0);

const cancel = fixture(); await cancel.voice.load(); await cancel.press("mic");
let resolveCancelled;
cancel.response(new Promise((resolve) => { resolveCancelled = resolve; }));
const pending = cancel.press("mic"); await new Promise(setImmediate);
cancel.voice.stop(); assert.equal(cancel.requests[0].signal.aborted, true);
resolveCancelled({ ok: true, status: 200, json: async () => ({ text: "cancelled" }) });
await pending; assert.equal(cancel.input.value, "helloWORLD");
assert.equal(cancel.control.dataset.state, "idle"); assert.equal(cancel.errors.length, 0);

const denied = fixture(); await denied.voice.load();
denied.permission(Promise.reject(Object.assign(new Error("denied"), { name: "NotAllowedError" })));
await denied.press("mic"); assert.deepEqual(denied.errors, ["Microphone access was denied."]); assert.equal(denied.closed, 1);
const late = fixture(); await late.voice.load();
let allow; late.permission(new Promise((resolve) => { allow = resolve; }));
const starting = late.press("mic"); await new Promise(setImmediate);
late.voice.reset(); let lateStopped = false;
allow({ getTracks: () => [{ stop() { lateStopped = true; } }] }); await starting;
assert.equal(lateStopped, true); assert.equal(late.closed, 1); assert.equal(late.control, null);
const error = fixture(); await error.voice.load(); await error.press("mic");
error.response({ ok: false, status: 503, json: async () => ({ error: "Model unavailable" }) });
await error.press("mic"); assert.deepEqual(error.errors, ["Model unavailable"]); assert.equal(error.input.value, "helloWORLD");
const auto = fixture(); await auto.voice.load(); await auto.press("mic");
[...auto.timers.values()].find((t) => t.delay === 60000).callback(); await new Promise(setImmediate);
assert.equal(auto.stopped, 1); assert.equal(auto.requests.length, 1);
auto.voice.stop();

// The actual worklet averages channels and flushes a partial block on stop.
let Capture;
const emitted = [];
const worklet = await readFile(new URL("../commonplace/web/static/voice-worklet.js", import.meta.url), "utf8");
runInNewContext(worklet, { AudioWorkletProcessor: class { constructor() { this.port = { postMessage(value) { emitted.push(value); } }; } }, registerProcessor(name, Klass) { assert.equal(name, "commonplace-voice"); Capture = Klass; } });
const capture = new Capture();
capture.process([[new Float32Array([1, .5]), new Float32Array([-1, .5])]]);
capture.port.onmessage({ data: "stop" });
assert.deepEqual(Array.from(emitted[0]), [0, .5]); assert.equal(emitted[1], "stopped");
capture.process([[new Float32Array([1])]]); assert.equal(emitted.length, 2);

// The app calls the controller at the draft/render and navigation boundaries.
const app = await readFile(new URL("../commonplace/web/static/app.js", import.meta.url), "utf8");
assert.match(app, /function remember\(\)\s*\{\s*voice\.remember\(\)/);
assert.match(app, /voice\.view\(\);\s*if \(saved\?\.view/);
assert.match(app, /if \(hash !== state\.route\) voice\.stop\(\)/);
assert.match(app, /function signin\([^)]*\)[\s\S]*?voice\.reset\(\)/);
assert.doesNotMatch(source, /SpeechRecognition|createObjectURL|\.submit\(|requestSubmit\(/);
// Boot must display the app without waiting for voice status. The real API's
// session-expired event must still replace that shell with sign-in on a late 401.
const apiSource = await readFile(new URL("../commonplace/web/static/api.js", import.meta.url), "utf8");
const bootSource = app.slice(app.indexOf("async function boot()"), app.lastIndexOf("\nboot();"));
const expiredSource = app.split("\n").find((line) => line.startsWith('document.addEventListener("session-expired"'));
function bootFixture() {
  const calls = [], document = new EventTarget(), state = { me: null, drafts: new Map(), error: "", people: {} };
  let resolveStatus;
  const pendingStatus = new Promise((resolve) => { resolveStatus = resolve; });
  const context = {
    document, state, Event, timer: null, clearTimeout() {},
    fetch: async (path) => path === "/api/voice" ? pendingStatus : { ok: true, status: 200, json: async () => ({ people: {} }) },
    shell() { calls.push("shell"); },
    async route() { calls.push("route"); },
    schedule() { calls.push("schedule"); },
    signin() { calls.push("signin"); context.voice.reset(); },
    // Expiry clears the signed-in user's chat state first (seen ids live on the device).
    clearWorkbenchChat() { calls.push("clear-chat"); },
    signinCode: null,
    showError(text) { calls.push(`error:${text}`); },
    URLSearchParams,
    location: { pathname: "/app/", search: "", hash: "" },
    history: { state: null, replaceState(_state, _title, url) { calls.push(`replace:${url}`); } },
  };
  // boot() runs in a bare vm context: every browser global or app function it
  // names must be provided here, or the test dies with a ReferenceError.
  for (const name of ["URLSearchParams", "URL", "location", "history", "window", "navigator", "localStorage", "sessionStorage", "showError", "signinCode"]) {
    if (new RegExp(`\\b${name}\\b`).test(bootSource)) assert.ok(name in context, `boot() uses ${name}; add it to the boot fixture`);
  }
  const { boot, api } = runInNewContext([apiSource.replace(/export /g, ""), expiredSource, bootSource, "({ boot, api })"].join("\n"), context);
  const root = { querySelector: () => null, querySelectorAll: () => [], addEventListener() {} };
  context.voice = voiceInput(root, api, assert.fail, { document });
  return { boot, context, calls, state, resolveStatus };
}
const booting = bootFixture();
await booting.boot();
assert.deepEqual(booting.calls, ["shell", "route", "schedule"]);
booting.resolveStatus({ ok: false, status: 401, json: async () => ({ error: "Sign in" }) });
await new Promise(setImmediate);
assert.equal(booting.state.me, null);
assert.deepEqual(booting.calls.slice(-2), ["clear-chat", "signin"]);
// A tapped card link for another workspace: boot drops ?w=, keeps the user on
// the board and explains why, without waiting for the voice status.
const otherWorkspace = bootFixture();
otherWorkspace.context.location = { pathname: "/app/", search: "?w=ws_other", hash: "#/card/7" };
await otherWorkspace.boot();
assert.deepEqual(otherWorkspace.calls, ["shell", "replace:/app/#/", "route", "error:This card is in another workspace. Sign out, then sign in again to open it.", "schedule"]);
const expiredAtLoad = bootFixture();
expiredAtLoad.context.voice = { load() { expiredAtLoad.state.me = null; } };
await expiredAtLoad.boot(); assert.deepEqual(expiredAtLoad.calls, ["shell"]);
const expiredAtRoute = bootFixture();
expiredAtRoute.context.voice = { load() {} };
expiredAtRoute.context.route = async () => { expiredAtRoute.calls.push("route"); expiredAtRoute.state.me = null; };
await expiredAtRoute.boot(); assert.deepEqual(expiredAtRoute.calls, ["shell", "route"]);
assert.match(app, /voice\.settings\(\)/);
assert.doesNotMatch(app, /voice\.settings\(connections/);
console.log("voice picker, PCM, capture, settings and sign-in lifecycle passed");
