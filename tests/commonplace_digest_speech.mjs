import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
const source = await readFile(new URL("../commonplace/web/static/digest.js", import.meta.url), "utf8");
const { digestSpeech } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
let spoken, cancelled = 0, state, error, resolve;
const local = { localService: true, lang: "en-US" };
const browser = {
  SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } },
  speechSynthesis: {
    getVoices: () => [{ localService: false, lang: "en-GB" }, local],
    speak: (utterance) => { spoken = utterance; utterance.onstart(); },
    cancel: () => { cancelled++; },
  },
};
const speech = digestSpeech(() => Promise.resolve({ text: 'Café "yes" <script>' }), (s) => { state = s; }, (e) => { error = e; }, browser);
assert.equal(speech.supported, true);
await speech.toggle();
assert.equal(spoken.voice, local);
assert.equal(state.text, 'Café "yes" <script>');
const previous = spoken;
await speech.toggle();
assert.equal(cancelled, 1);
assert.deepEqual(state, { active: false, text: "" });
await speech.toggle();
previous.onend(); // An old callback cannot clear the new captions.
assert.equal(state.active, true);
spoken.onend();
assert.deepEqual(state, { active: false, text: "" });
await speech.toggle();
spoken.onerror();
assert.equal(error, "Could not speak the digest.");
assert.equal(state.active, false);
const pending = digestSpeech(() => new Promise((r) => { resolve = r; }), (s) => { state = s; }, assert.fail, browser);
const fetch = pending.toggle();
await pending.toggle();
spoken = null;
resolve({ text: "Cancelled" });
await fetch;
assert.equal(spoken, null);
assert.equal(state.active, false);
const broken = digestSpeech(() => Promise.reject(new Error("Offline")), () => {}, (e) => { error = e; }, browser);
await broken.toggle();
assert.equal(error, "Offline");
browser.speechSynthesis.getVoices = () => [{ localService: false, lang: "en-US" }];
await speech.toggle();
assert.equal(error, "No local speech voice is installed.");
assert.equal(spoken, null);
// Some browsers load voices only after the first request.
let voicesChanged;
browser.speechSynthesis.getVoices = () => [];
browser.speechSynthesis.addEventListener = (_, callback) => { voicesChanged = callback; };
browser.speechSynthesis.removeEventListener = () => {};
const loading = speech.toggle();
await Promise.resolve();
browser.speechSynthesis.getVoices = () => [local];
voicesChanged();
await loading;
assert.equal(spoken.voice, local);
speech.stop();
const missing = digestSpeech(assert.fail, () => {}, assert.fail, {});
assert.equal(missing.supported, false);
await missing.toggle();
missing.stop();

// Exercise the app's actual render functions: a live refresh must retain the
// focused Stop node, while speechView updates its action label and captions.
const app = await readFile(new URL("../commonplace/web/static/app.js", import.meta.url), "utf8");
const functionSource = (start, end) => app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)));
const document = { activeElement: null, title: "" };
let button = null, buttonsCreated = 0;
// The Speak button may live in the header actions or in the screen content; either container
// recreates it when redrawn, and redrawing its container drops the old node's focus.
const container = () => {
  const node = {
    set innerHTML(markup) {
      if (button?.owner === node && document.activeElement === button) document.activeElement = null;
      if (markup.includes('data-action="speak-digest"')) {
        button = {
          owner: node, dataset: {}, attributes: {},
          setAttribute(name, value) { this.attributes[name] = value; },
          focus() { document.activeElement = this; },
          replaceWith(next) { button = next; },
        };
        buttonsCreated++;
      } else if (button?.owner === node) button = null;
    },
    querySelector: () => null,
  };
  return node;
};
const elements = { "#title": {}, "#head-actions": container(), "#content": container(), "#card-panel": { hidden: true }, "#digest-captions": {} };
const context = {
  document, root: { querySelector: (selector) => selector === '[data-action="speak-digest"]' ? button : elements[selector] },
  speech: { supported: true }, speechState: { active: false, text: "" },
  state: { needs: {}, open: [], boards: [], route: "#/needs", base: "#/needs" }, needsCards: () => [],
  homeView: ({ speak }) => (speak ? '<button data-action="speak-digest">Speak</button>' : ""),
  dot: () => "", taskRow: () => "", children: () => "",
  createNeeds: () => ({ show: async () => {}, stop() {} }), api() {}, singleKeysEnabled: () => true,
};
const view = runInNewContext([
  functionSource("function speechView()", "\nconst prLink"),
  functionSource("function title(", "\nfunction decision("),
  functionSource("function needsView()", "\nfunction boardView()"),
  "({ needsView, speechView })",
].join("\n"), context);
view.needsView();
const focused = button;
focused.focus();
context.speechState = { active: true, text: "Three things need you." };
view.speechView();
view.needsView();
assert.equal(buttonsCreated, 2); // Redrawn, then swapped back for the original node.
assert.equal(button, focused);
assert.equal(document.activeElement, focused);
assert.equal(button.textContent, "Stop");
assert.equal(button.attributes["aria-label"], "Stop speaking");
assert.equal(button.dataset.active, "true");
assert.equal(Object.hasOwn(button.attributes, "aria-pressed"), false);
assert.equal(elements["#digest-captions"].textContent, "Three things need you.");
context.speechState = { active: false, text: "" };
view.speechView();
assert.equal(button.textContent, "Speak");
assert.equal(button.dataset.active, "false");
assert.equal(elements["#digest-captions"].hidden, true);
