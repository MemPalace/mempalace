// Chat as drawn: a status line instead of a header row, quick replies without a model id,
// the composer box with an icon Send and key hints, and the no-answer block of design 06d.
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

const STATIC = new URL("../commonplace/web/static/", import.meta.url);
const read = (name) => readFileSync(new URL(name, STATIC), "utf8");
const pad = (n) => String(n).padStart(2, "0");
const local = (iso) => { const d = new Date(iso); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const settle = async () => { for (let i = 0; i < 60; i++) await Promise.resolve(); };

async function chat() {
  assert.ok(existsSync(new URL("chat.js", STATIC)), "chat.js missing");
  const ui = await import("../commonplace/web/static/chat.js");
  assert.equal(typeof ui.chatHTML, "function", "chatHTML missing");
  assert.equal(typeof ui.createChat, "function", "createChat missing");
  return ui;
}

const BOARD = { slug: "atlas", name: "Atlas", coordinator: "conn:atlas" };
const REPLY_AT = "2026-11-05T12:41:00Z";
const answerPage = () => ({
  project: BOARD,
  messages: [{ id: 1, actor: "user:maya", text: "Is anything stuck?", at: "2026-11-05T12:30:00Z" }, { id: 2, actor: "user:maya", text: "Draft the notes", at: "2026-11-05T14:02:00Z" }],
  replies: [{ id: 9, message_id: 1, for_user: "maya", kind: "answer", text: "A-121 is blocked.", model: "local:openai_compatible/fast-model", at: REPLY_AT }],
  cards: [{ number: 121, title: "Offline mode", board: "atlas", status: "doing" }],
  linked: [{ number: 134, title: "Draft notes", board: "atlas", message_id: 2 }, { number: 135, title: "Check email", board: "atlas", message_id: 2 }],
});

test("a quick reply shows its kind and local time, never the model id", async () => {
  const { chatHTML } = await chat();
  const html = chatHTML(answerPage(), { user: "maya" }, e);
  const stamp = html.match(/<article class="chat-reply"><div class="chat-stamp">(.*?)<\/div>/)?.[1] || "";
  assert.match(stamp, /Quick reply · local model/);
  assert.doesNotMatch(stamp, /openai_compatible|fast-model/, "no raw model id");
  assert.ok(stamp.includes(`<time datetime="${REPLY_AT}">${local(REPLY_AT)}</time>`), "the reply's local HH:MM");
});

test("card groups say what a row does and how many cards a request made", async () => {
  const { chatHTML } = await chat();
  const html = chatHTML(answerPage(), { user: "maya" }, e);
  assert.match(html, /<span id="answer-9">Cards in this answer<\/span><span class="chat-cards-note">select one to open its details<\/span>/);
  assert.match(html, /<span id="request-2">From this request · on the board now<\/span><span class="chat-cards-note">2 new<\/span>/);
  assert.match(html, /role="group" aria-labelledby="answer-9"/, "the group is still named by its heading");
});

// A small pane: the controller writes its markup into `host` and its regions into `nodes`.
function pane(ui, { page, status }) {
  const nodes = new Map(), field = { value: "", focus() {} }, form = { elements: { text: field }, querySelector: () => ({}) };
  const host = { innerHTML: "", querySelector(selector) {
    if (!this.innerHTML) return null;
    if (selector === ".chat-compose") return form;
    if (!nodes.has(selector)) nodes.set(selector, { innerHTML: "", textContent: "", dataset: {}, hidden: false, focus() {}, scrollIntoView() {}, setAttribute() {} });
    return nodes.get(selector);
  } };
  const api = async (path) => {
    if (path === "/api/chat") return { projects: [{ slug: "atlas", name: "Atlas" }], default: "atlas" };
    if (path.endsWith("/status")) return status;
    if (path.startsWith("/api/chat-settings")) return { front: {} };
    if (path.endsWith("/cards")) return { cards: [] };
    return page;
  };
  const controller = ui.createChat({ mount: () => host, api, e, me: { user: "maya" }, board: BOARD });
  return { controller, host, nodes };
}
function quietGlobals(t) {
  const saved = ["document", "setTimeout", "clearTimeout"].map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]);
  Object.defineProperty(globalThis, "document", { configurable: true, writable: true, value: { hidden: false, addEventListener() {}, removeEventListener() {}, querySelectorAll: () => [] } });
  t.mock.method(globalThis, "setTimeout", () => 0);
  t.mock.method(globalThis, "clearTimeout", () => {});
  t.after(() => { const [name, previous] = saved[0]; previous ? Object.defineProperty(globalThis, name, previous) : delete globalThis[name]; });
}

test("the composer is the drawn box: hidden label, icon Send, Enter and Esc hints", async (t) => {
  const ui = await chat();
  quietGlobals(t);
  const p = pane(ui, { page: { board: "atlas", me: { user: "maya" }, messages: [], replies: [], cursor: 0 }, status: { orchestrator: { name: "atlas", visible: true, status: "working" } } });
  await p.controller.show("atlas", { name: "Atlas" }); await settle();
  const html = p.host.innerHTML;
  assert.match(html, /<label class="sr" for="chat-text">Message to Atlas chat<\/label>/, "the label is for screen readers");
  assert.match(html, /<div class="chat-box"><textarea id="chat-text"[^>]*placeholder="Ask for something…"/);
  assert.match(html, /<button class="btn primary sm" type="submit" aria-label="Send"[^>]*><svg/, "an icon Send button");
  assert.match(html, /Enter to send · Shift\+Enter new line<\/span><span><kbd>Esc<\/kbd> closes the chat<\/span>/);
  p.controller.destroy();
});

test("no answer: local times, the reassurance line, restart as the primary action, and the status line", async (t) => {
  const ui = await chat();
  quietGlobals(t);
  const sent = new Date(Date.now() - 20 * 60_000).toISOString(), active = new Date(Date.now() - 19 * 60_000).toISOString();
  const page = { board: "atlas", me: { user: "maya" }, messages: [{ id: 7, actor: "user:maya", text: "Reconcile the October invoices", at: sent }], replies: [], cursor: 7, reply_cursor: 0 };
  const status = { orchestrator: { name: "atlas", visible: true, status: "working", last_seen: active } };
  const p = pane(ui, { page, status });
  assert.equal(typeof p.controller.watchHeader, "function", "the chat offers its status line to the dock");
  const lines = [];
  p.controller.watchHeader((text) => lines.push(text));
  await p.controller.show("atlas", { name: "Atlas" }); await settle();
  const block = p.nodes.get("[data-chat-recovery]")?.innerHTML || "";
  assert.match(block, /No answer yet\./);
  assert.ok(block.includes(`It has not answered your message from ${local(sent)}.`), block);
  assert.ok(block.includes(`Its agent was last active at ${local(active)}.`), "local time, not ISO");
  assert.doesNotMatch(block, /\d{4}-\d\d-\d\dT/, "no raw ISO timestamp");
  assert.match(block, /Your message is kept, and nothing changed on the board\./);
  assert.match(block, /<a class="btn sm primary" href="#\/board\/atlas\/settings">Show how to restart the agent<\/a>/);
  assert.match(block, /<button class="btn sm" type="button" data-chat-ask-again>Ask again<\/button>/);
  assert.equal(lines.at(-1), `Connected, not answering since ${local(sent)}`);
  p.controller.destroy();
});

test("a connected orchestrator reads 'Orchestrator connected'", async (t) => {
  const ui = await chat();
  quietGlobals(t);
  const page = { board: "atlas", me: { user: "maya" }, messages: [{ id: 1, actor: "conn:atlas", text: "Hello", at: new Date().toISOString() }], replies: [], cursor: 1 };
  const p = pane(ui, { page, status: { orchestrator: { name: "atlas", visible: true, status: "idle" } } });
  assert.equal(typeof p.controller.watchHeader, "function");
  const lines = [];
  p.controller.watchHeader((text) => lines.push(text));
  await p.controller.show("atlas", { name: "Atlas" }); await settle();
  assert.equal(lines.at(-1), "Orchestrator connected");
  p.controller.destroy();
});

test("styles: no header row in the docked chat, status line, composer box, stacked no-answer actions", () => {
  const dock = read("chatdock.css"), css = read("chat.css");
  assert.match(dock, /#chat-content \.chat-header \{ display: none; \}/);
  assert.match(dock, /\.chat-head \.ch-status \{[^}]*color: var\(--text-2\)/);
  assert.match(css, /\.chat-box \{[^}]*border: 1px solid var\(--line-strong\)/);
  assert.match(css, /\.chat-recovery \.chat-acts \{ flex-direction: column;/);
  assert.match(css, /\.chat-recovery button:not\(\.btn\), \.chat-recovery a:not\(\.btn\)/, "the drawn buttons keep their .btn look");
});
