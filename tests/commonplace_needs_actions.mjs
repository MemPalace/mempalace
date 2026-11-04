import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";

const app = await readFile(new URL("../commonplace/web/static/app.js", import.meta.url), "utf8");
function slice(start, end) {
  assert.ok(app.includes(start), `${start} missing`);
  assert.ok(app.includes(end), `${end} missing`);
  const at = app.indexOf(start), stop = app.indexOf(end, at);
  assert.ok(stop > at, `${end} must follow ${start}`);
  return app.slice(at, stop);
}
// Check new entry points explicitly, so the old implementation fails an assertion.
assert.ok(app.includes("async function approveProposal("), "versioned proposal helper missing");
assert.ok(app.includes('action === "need-accept"'), "two-step acceptance missing");
const source = [
  slice("function remember()", "async function signin("),
  slice("function decision(", "const clock ="),
  slice("function handoffForm(", "async function settingsView("),
  slice("async function sendDecide(", "async function route("),
  slice("function findCard(", 'root.addEventListener("input",'),
  slice('root.addEventListener("input",', 'root.addEventListener("change",'),
  slice('root.addEventListener("click",', 'root.addEventListener("submit",'),
  slice('root.addEventListener("submit",', "let drag = null;"),
].join("\n");
const plain = (value) => JSON.parse(JSON.stringify(value));
const e = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");

function fixture() {
  const row = { number: 5, version: 3, board: "work", body: "Row context", status_line: "" };
  const state = {
    card: { number: 99, version: 7, body: "Decoy context" },
    needs: { stale: [row] }, open: [row], cards: [], linked: [], base: "#/",
    drafts: new Map(), pending: new Set(),
  };
  const fields = [], handlers = {}, requests = [], errors = [], refreshes = [];
  const panel = { hidden: true, innerHTML: "" }, location = { hash: "#/" };
  const root = {
    querySelector: (selector) => selector === "#card-panel" ? panel : null,
    querySelectorAll(selector) {
      if (selector === "[data-draft]") return fields;
      if (selector === '[data-draft^="then-"]') return fields.filter((field) => field.dataset.draft.startsWith("then-"));
      const match = /^\[data-draft="([^"]+)"\]$/.exec(selector);
      return match ? fields.filter((field) => field.dataset.draft === match[1]) : [];
    },
    addEventListener: (kind, handler) => { handlers[kind] = handler; },
  };
  let beforeRefresh = () => {}, nextError = null;
  const context = {
    state, root, location, e, enc: encodeURIComponent, actor: (value) => value || "Worker",
    document: { activeElement: null }, CSS: { escape: (value) => value },
    voice: { stop() {}, remember() {}, view() {} },
    needsCards: () => Object.values(state.needs).flat(),
    data: (form) => form.payload(),
    api: async (url, options) => {
      requests.push(plain({ url, ...options }));
      if (nextError) { const error = nextError; nextError = null; throw error; }
      return { number: 5, version: 4 };
    },
    showError: (text) => errors.push(text),
    async refresh() {
      beforeRefresh(); // Success must clear the live select before remember can copy it.
      refreshes.push([...state.drafts]);
      context.remember();
    },
    askedRead: () => ({}), askedWrite() {}, RELOAD_ERRORS: new Set(),
    queueDraft: (number) => state.drafts.get(`reply-${number}`) || "",
    queueApply: () => { beforeRefresh(); context.remember(); },
  };
  runInNewContext(source, context);
  function field(key, initial, version) {
    let value = initial;
    const history = [];
    const input = {
      dataset: { draft: key }, history,
      get value() { return value; },
      set value(next) { history.push(next); value = next; },
      closest: (selector) => selector === "[data-version]" ? { dataset: { version: String(version) } } : null,
    };
    fields.push(input);
    return input;
  }
  async function click(action, { number = 5, version = 3, choice = "Proceed" } = {}) {
    const button = {
      dataset: { action, card: String(number), version: String(version), choice },
      closest: (selector) => selector === "[data-version]" ? { dataset: { version: String(version) } } : null,
    };
    await handlers.click({ target: { closest: () => button } });
    assert.deepEqual(errors, []);
  }
  async function submit(form) {
    await handlers.submit({ target: { closest: () => form }, preventDefault() {} });
  }
  const submitButton = { disabled: false, isConnected: true };
  function handoff() {
    // Read the real rendered fields; do not substitute the expected row/version.
    const html = panel.innerHTML;
    const card = /data-card="(\d+)"/.exec(html)?.[1];
    const version = /<input[^>]*name="version"[^>]*value="(\d+)"/.exec(html)?.[1];
    const to = /<input[^>]*name="to"[^>]*>/.exec(html)?.[0];
    assert.ok(card && version && to, "handoff must carry its own card and version");
    const values = { version, to: /value="([^"]*)"/.exec(to)?.[1] || "", note: "", options: "", context: "Row context" };
    return {
      values, dataset: { form: "handoff", card, connectionsOnly: /data-connections-only="true"/.test(html) ? "true" : "" },
      payload: () => ({ ...values }), querySelector: () => submitButton,
    };
  }
  function decision(version = 3) {
    fields.length = 0;
    const html = context.decision({ ...row, version, ask_options: ["Proceed"], return_to: "conn:worker" }, true);
    assert.match(html, /name="reply"[^>]*required/);
    assert.match(html, new RegExp(`data-version="${version}"`));
    const reply = field("reply-5", "", version), then = field("then-5", "return", version);
    const form = {
      dataset: { form: "reply", card: "5" },
      closest: () => ({ dataset: { version: String(version) } }),
      querySelector: () => submitButton,
      payload: () => ({ reply: reply.value, then: then.value }),
    };
    return { form, reply, then };
  }
  return { context, state, panel, location, requests, errors, refreshes, click, submit, handoff, decision,
    input: (target) => handlers.input({ target }),
    onRefresh(fn) { beforeRefresh = fn; }, failOnce(error) { nextError = error; } };
}

// Every row write uses the row's version even while a different card is open.
for (const [action, approve] of [["need-approve", true], ["need-reject", false]]) {
  const f = fixture();
  await f.click(action);
  assert.deepEqual(f.requests, [{ url: "/api/cards/5/approve", method: "POST", body: { approve, version: 3 } }]);
}
{
  const f = fixture();
  await f.click("need-park");
  assert.deepEqual(f.requests, [{ url: "/api/cards/5", method: "PATCH", body: { version: 3, status: "backlog" } }]);
}
for (const [action, initial, target] of [["need-requeue", "anyone", "anyone"], ["need-reassign", "", "conn:replacement"]]) {
  const f = fixture();
  await f.click(action);
  assert.equal(f.panel.hidden, false);
  assert.match(f.panel.innerHTML, /name="note" required/);
  assert.doesNotMatch(f.panel.innerHTML, /Decoy context/);
  const form = f.handoff();
  assert.equal(form.values.to, initial);
  assert.equal(form.values.version, "3");
  form.values.to = target;
  await f.submit(form); // Missing note cannot send, including a synthetic submit event.
  form.values.note = "   ";
  await f.submit(form);
  assert.deepEqual(f.requests, []);
  form.values.note = "Owner's exact <handoff> note";
  await f.submit(form);
  assert.deepEqual(f.requests, [{ url: "/api/cards/5/handoff", method: "POST", body: {
    version: 3, to: target, note: form.values.note, context: "Row context", options: [],
  } }]);
  assert.ok(f.requests.every((request) => !request.url.includes("99")));
}
{
  const f = fixture();
  await f.click("need-reassign");
  const form = f.handoff();
  form.values.to = "user:someone"; form.values.note = "Reassign to a worker";
  await f.submit(form);
  assert.deepEqual(f.requests, []); // A person target would create a new ask, not reassign work.
}

// Panel actions retain their own explicit number/version as well.
for (const [action, approve] of [["approve-proposal", true], ["reject-proposal", false]]) {
  const f = fixture();
  await f.click(action);
  assert.deepEqual(f.requests[0], { url: "/api/cards/99/approve", method: "POST", body: { approve, version: 7 } });
}

// Accept only prepares a version-bound draft. The owner must still reply.
{
  const f = fixture();
  await f.click("need-accept");
  assert.equal(f.state.drafts.get("then-5"), "done");
  assert.equal(String(f.state.drafts.get("then-version-5")), "3");
  assert.equal(f.location.hash, "#/card/5");
  assert.deepEqual(f.requests, []);
  const { form, reply, then } = f.decision();
  f.context.restore({});
  assert.equal(then.value, "done");
  await f.submit(form);
  assert.deepEqual(f.requests, []);
  assert.deepEqual(f.errors, ["Enter a reply."]);
  f.errors.length = 0;
  reply.value = "I checked the result and accept it.";
  let observed = false;
  f.onRefresh(() => {
    observed = true;
    assert.equal(then.value, "return");
    assert.equal(reply.value, "");
    for (const key of ["reply-5", "then-5", "then-version-5"]) assert.equal(f.state.drafts.has(key), false);
  });
  await f.submit(form);
  assert.equal(observed, true);
  assert.deepEqual(f.requests, [{ url: "/api/cards/5/decide", method: "POST", body: { version: 3, reply: "I checked the result and accept it.", then: "done" } }]);
  f.onRefresh(() => {});
  await f.click("decide");
  assert.equal(f.requests.at(-1).body.then, "return");
  reply.value = "Answer the next question.";
  await f.submit(form);
  assert.equal(f.requests.at(-1).body.then, "return");
}

// A refreshed version must never see the old done during the generic restore loop.
{
  const f = fixture();
  await f.click("need-accept");
  const { then } = f.decision(4);
  then.value = "done"; then.history.length = 0;
  f.context.restore({});
  assert.deepEqual(then.history, ["return"]);
  assert.equal(then.value, "return");
  assert.equal(f.state.drafts.has("then-5"), false);
  assert.equal(f.state.drafts.has("then-version-5"), false);
}

// An abandoned accept may affect an option only at exactly the accepted version.
for (const version of [3, 4]) {
  const f = fixture();
  await f.click("need-accept");
  f.state.drafts.set("reply-5", "Owner's option context");
  await f.click("decide", { version });
  assert.deepEqual(f.requests[0], { url: "/api/cards/5/decide", method: "POST", body: {
    version, choice: "Proceed", reply: "Owner's option context", then: version === 3 ? "done" : "return",
  } });
  assert.equal(f.state.drafts.has("then-version-5"), false);
}

// A stale open form still submits its displayed version and lets 409 refresh it.
{
  const f = fixture();
  await f.click("need-accept");
  const { form, reply } = f.decision();
  f.context.restore({}); reply.value = "Owner's acceptance";
  let refreshed;
  f.failOnce({ status: 409, message: "version mismatch" });
  f.onRefresh(() => { refreshed = f.decision(4); f.context.restore({}); });
  await f.submit(form);
  assert.equal(f.requests[0].body.version, 3);
  assert.equal(f.requests[0].body.then, "done");
  assert.equal(refreshed.then.value, "return");
  assert.deepEqual(f.errors, ["Card changed. Review it and try again."]);
  f.errors.length = 0; f.onRefresh(() => {});
  await f.click("decide", { version: 4 });
  assert.equal(f.requests.at(-1).body.then, "return");
}

// The queue sender has its own success path; it must reset any still-live panel too.
for (const activeQueue of [false, true]) {
  const f = fixture();
  await f.click("need-accept");
  const { reply, then, form } = f.decision();
  f.context.restore({}); reply.value = "Owner's queue reply";
  f.state.drafts.set("reply-5", reply.value);
  f.state.base = activeQueue ? "#/decide" : "#/";
  f.state.queue = activeQueue ? {} : null;
  f.onRefresh(() => {
    assert.equal(then.value, "return");
    assert.equal(reply.value, "");
    for (const key of ["reply-5", "then-5", "then-version-5"]) assert.equal(f.state.drafts.has(key), false);
  });
  await f.context.sendDecide({ number: 5, body: { version: 3, reply: reply.value, then: "return" } }, false);
  f.onRefresh(() => {});
  assert.equal(then.value, "return");
  assert.equal(reply.value, "");
  assert.notEqual(f.state.drafts.get("then-5"), "done");
  assert.equal(f.state.drafts.has("then-version-5"), false);
  f.context.remember(); // Would resurrect done if the sender cleared only the cache.
  await f.click("decide", { version: 4 });
  assert.equal(f.requests.at(-1).body.then, "return");
  reply.value = "Another owner reply";
  await f.submit(form);
  assert.equal(f.requests.at(-1).body.then, "return");
}

// Manually choosing Done in the existing panel also records the displayed version.
{
  const f = fixture();
  const { then } = f.decision();
  then.value = "done";
  f.input(then);
  assert.equal(f.state.drafts.get("then-5"), "done");
  assert.equal(String(f.state.drafts.get("then-version-5")), "3");
  await f.click("decide");
  assert.equal(f.requests[0].body.then, "done");
}

// Whitespace-only input must explain why no request was sent.
for (const kind of ["reply", "note", "to", "queue-reply"]) {
  const f = fixture();
  let form;
  if (kind === "queue-reply") {
    f.state.drafts.set("reply-5", "   ");
    form = { dataset: { form: "queue-reply", card: "5" } };
  } else if (kind === "reply") {
    const decision = f.decision(); form = decision.form; decision.reply.value = "   ";
  } else {
    await f.click("need-reassign"); form = f.handoff();
    form.values.to = "conn:replacement"; form.values.note = "Continue work";
    form.values[kind] = "   ";
  }
  await f.submit(form);
  assert.deepEqual(f.requests, []);
  assert.equal(f.errors.length, 1, `${kind} must show a validation error`);
  assert.equal(f.errors[0], kind.endsWith("reply") ? "Enter a reply." : "Enter a recipient and a note.");
}
